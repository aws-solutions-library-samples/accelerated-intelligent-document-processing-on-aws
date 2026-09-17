# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metering key names the model that was invoked, so it carries no ``:1m``.

The suffix is not part of the model ID sent to Bedrock — the client strips it and
sends the ``context-1m-2025-08-07`` beta header instead — and it names no separate
price, because the 1M context window is billed at the model's standard per-token
rates. Keeping it in the key therefore bought a second pricing entry for the same
rates, and that is exactly where a premium rate card was reintroduced and charged
on every request; see ``tests/unit/reporting/test_long_context_pricing.py`` and
issue #899.

A service tier is the opposite case and must be kept: ``:flex`` / ``:priority``
re-price every request made in them and have their own
``config_library/pricing.yaml`` entries.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.bedrock.client import BedrockClient
from idp_common.bedrock.model_utils import metering_model_id

pytestmark = pytest.mark.unit

_IDP_COMMON = Path(__file__).resolve().parents[3] / "idp_common"

# Every place a Bedrock metering key is built. A new one must either route the
# model ID through metering_model_id() or be listed here with the reason it need
# not — an unexplained omission is how the defect returns for one code path.
# The value is (reason, expected number of raw sites in that file), so a NEW raw
# site added to an exempt file is still caught -- the exemption covers the sites
# audited when it was written, not the filename forever.
METERING_KEY_EXEMPT = {
    "bedrock/openai_responses.py": (
        "OpenAI Responses models have no :1m variant (the 1M-context suffix is "
        "an Anthropic beta), so there is no suffix to strip",
        2,
    ),
}


class TestMeteringModelId:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("us.anthropic.claude-sonnet-5:1m", "us.anthropic.claude-sonnet-5"),
            ("eu.anthropic.claude-sonnet-4-6:1m", "eu.anthropic.claude-sonnet-4-6"),
            ("global.anthropic.claude-opus-5:1m", "global.anthropic.claude-opus-5"),
            ("us.anthropic.claude-opus-4-6-v1:1m", "us.anthropic.claude-opus-4-6-v1"),
        ],
    )
    def test_the_long_context_suffix_is_dropped(self, model_id, expected):
        assert metering_model_id(model_id) == expected

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            # A service tier is priced differently for the whole request and has
            # its own pricing entry, so it stays in the key.
            "us.amazon.nova-2-lite-v1:0:flex",
            "us.amazon.nova-2-lite-v1:0:priority",
            "us.amazon.nova-lite-v1:0",
            "openai.gpt-6-astra",
            "arn:aws:bedrock:us-west-2:123456789012:inference-profile/foo",
            "",
        ],
    )
    def test_everything_else_is_returned_unchanged(self, model_id):
        assert metering_model_id(model_id) == model_id

    def test_none_is_tolerated(self):
        assert metering_model_id(None) is None


class TestClientMeteringKey:
    """The real invoke_model path, mocked only at the boto3 boundary."""

    @pytest.fixture
    def response(self):
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {
                "inputTokens": 34_015,
                "outputTokens": 500,
                "totalTokens": 34_515,
            },
        }

    @pytest.fixture
    def client(self):
        client = BedrockClient(region="us-west-2", metrics_enabled=False)
        client._client = MagicMock()
        return client

    def test_a_1m_model_meters_under_the_plain_model_id(self, client, response):
        client._client.converse.return_value = response

        result = client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5:1m",
            system_prompt="test",
            content=[{"text": "test"}],
            context="Extraction",
        )

        # The suffix is not sent to Bedrock, and the beta header takes its place.
        call = client._client.converse.call_args
        assert call.kwargs["modelId"] == "us.anthropic.claude-sonnet-5"
        assert call.kwargs["additionalModelRequestFields"]["anthropic_beta"] == [
            "context-1m-2025-08-07"
        ]

        assert list(result["metering"]) == [
            "Extraction/bedrock/us.anthropic.claude-sonnet-5"
        ]
        assert (
            result["metering"]["Extraction/bedrock/us.anthropic.claude-sonnet-5"][
                "inputTokens"
            ]
            == 34_015
        )

    def test_a_service_tier_suffix_stays_in_the_key(self, client, response):
        """It is priced differently per token, so it names a different rate."""
        client._client.converse.return_value = response

        result = client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0:flex",
            system_prompt="test",
            content=[{"text": "test"}],
            context="Classification",
        )

        assert list(result["metering"]) == [
            "Classification/bedrock/us.amazon.nova-2-lite-v1:0:flex"
        ]


# Any f-string building a "<step>/bedrock/<model>" metering key. The step part is
# matched as any identifier rather than the literal name ``context``, so renaming
# that variable at a new site does not silently opt it out of this guard.
_METERING_KEY_FSTRING = re.compile(r'f"\{\w+\}/bedrock/\{([^}]+)\}')


def test_every_bedrock_metering_key_site_strips_the_suffix() -> None:
    """Guard the two emission sites, and any third one added later."""
    offenders = []
    exempt_counts: dict[str, int] = {}
    for path in sorted(_IDP_COMMON.rglob("*.py")):
        rel = path.relative_to(_IDP_COMMON).as_posix()
        for match in _METERING_KEY_FSTRING.finditer(path.read_text(encoding="utf-8")):
            if "metering_model_id(" in match.group(1):
                continue
            if rel in METERING_KEY_EXEMPT:
                exempt_counts[rel] = exempt_counts.get(rel, 0) + 1
                continue
            offenders.append(f"{rel}: {match.group(0)}")

    assert not offenders, (
        "these Bedrock metering keys use the raw model ID, so a ':1m' suffix "
        "would reach cost reporting and could pick up a rate card of its own on "
        f"every request: {offenders}. Wrap the model ID in metering_model_id(), "
        "or add the file to METERING_KEY_EXEMPT with the reason."
    )

    # An exempt file is exempt for the sites that were audited, not for any
    # number of them.
    for rel, (reason, expected) in METERING_KEY_EXEMPT.items():
        assert exempt_counts.get(rel, 0) == expected, (
            f"{rel} is exempt for {expected} raw metering key site(s) "
            f"({reason}), but {exempt_counts.get(rel, 0)} were found. Review the "
            "new site and either wrap it or update the expected count."
        )


def test_the_agentic_extraction_path_is_covered_by_that_guard() -> None:
    """The path the defect was measured on: it has no per-call usage at all.

    Strands reports ``accumulated_usage``, already summed across every turn of the
    agent loop. That does not matter for ``:1m``, which has no size-dependent
    price, but it is why this path could not implement one for a model that does
    (GPT-6 Astra) without new per-call instrumentation.
    """
    source = (_IDP_COMMON / "extraction" / "agentic_idp.py").read_text(encoding="utf-8")
    assert 'f"{context}/bedrock/{metering_model_id(model_id)}"' in source
