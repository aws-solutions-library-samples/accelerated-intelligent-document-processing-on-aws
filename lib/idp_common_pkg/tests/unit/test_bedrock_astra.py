# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for OpenAI GPT-6 Astra support on the Converse path.

Astra is the accelerator's first OpenAI model that is NOT served by the
``bedrock-mantle`` Responses API: it reaches ordinary ``bedrock-runtime``
Converse, so it behaves like xAI Grok rather than like its GPT-5.x stablemates.
These tests pin that split — most importantly that Astra is never routed onto the
mantle path — plus the three-way effort-vocabulary difference between Claude,
Grok and Astra.

Every expectation here was verified live against ``us.openai.gpt-6-astra`` on
bedrock-runtime Converse in us-west-2 on 2026-09-10, and cross-checked against
the model card — see the "OpenAI GPT-6 Astra" comment block in
``idp_common/bedrock/client.py``.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from idp_common.bedrock.client import (
    ASTRA_EFFORT_LEVELS,
    CACHEPOINT_SUPPORTED_MODELS,
    CLAUDE_EFFORT_LEVELS,
    GROK_EFFORT_LEVELS,
    BedrockClient,
    document_blocks_unsupported_reason,
    is_astra_model,
    is_claude_4_7_model,
    is_claude_effort_model,
    is_grok_model,
    strips_sampling_params,
    supports_document_blocks,
    supports_tool_config,
)
from idp_common.bedrock.model_utils import get_model_max_output_tokens
from idp_common.bedrock.openai_responses import is_openai_responses_model

ASTRA_US = "us.openai.gpt-6-astra"
ASTRA_GLOBAL = "global.openai.gpt-6-astra"
ASTRA_BARE = "openai.gpt-6-astra"


def _pricing_rows():
    """The Astra rows from the repo's config_library/pricing.yaml.

    Walks up for ``config_library/`` rather than hard-coding a parent count, so
    the tests keep working if this file moves within tests/.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config_library" / "pricing.yaml"
        if candidate.exists():
            return yaml.safe_load(candidate.read_text())["pricing"]
    pytest.skip("config_library/pricing.yaml not available in this checkout")


@pytest.fixture
def mock_bedrock_response():
    """Minimal Converse response, with the leading reasoningContent block a
    reasoning model emits."""
    return {
        "output": {
            "message": {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                    {"text": "test response"},
                ]
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
    }


@pytest.fixture
def bedrock_client():
    client = BedrockClient(region="us-west-2", metrics_enabled=False)
    client._client = MagicMock()
    return client


def _converse_kwargs(client):
    """The kwargs the client passed to boto3 converse()."""
    assert client._client.converse.called, "converse() was not invoked"
    return client._client.converse.call_args.kwargs


@pytest.mark.unit
class TestAstraIdentification:
    @pytest.mark.parametrize("model_id", [ASTRA_US, ASTRA_GLOBAL, ASTRA_BARE])
    def test_astra_ids_recognized(self, model_id):
        assert is_astra_model(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-4-8:1m",
            "us.amazon.nova-2-lite-v1:0",
            "openai.gpt-5.6-sol",
            "openai.gpt-5.4",
            "us.xai.grok-4.6",
            "LambdaHook",
            "",
        ],
    )
    def test_other_families_not_astra(self, model_id):
        assert is_astra_model(model_id) is False

    def test_astra_is_not_mistaken_for_a_claude_variant(self):
        """Astra must not pick up Claude-only request fields."""
        assert is_claude_4_7_model(ASTRA_US) is False
        assert is_claude_effort_model(ASTRA_US) is False

    def test_astra_is_not_mistaken_for_grok(self):
        """The two share a carrier but not a vocabulary, so the predicates must
        stay distinct."""
        assert is_grok_model(ASTRA_US) is False
        assert is_astra_model("us.xai.grok-4.6") is False


@pytest.mark.unit
class TestAstraIsNotOnTheMantlePath:
    """The single most important guard in this file.

    ``is_openai_responses_model`` matches ``openai.gpt-5`` and would silently
    steal Astra onto the bedrock-mantle Responses API if that prefix were ever
    widened to ``openai.``. Routing Astra there fails outright — the mantle
    endpoint answers 404 ``"The model 'openai.gpt-6-astra' does not exist"`` in
    the regions the accelerator configures for GPT-5.x (verified live), and the
    only region where mantle does serve Astra (us-west-2) is not in that map.
    """

    @pytest.mark.parametrize("model_id", [ASTRA_US, ASTRA_GLOBAL, ASTRA_BARE])
    def test_astra_is_not_a_responses_api_model(self, model_id):
        assert is_openai_responses_model(model_id) is False

    def test_gpt_5_models_are_still_on_the_mantle_path(self):
        """Regression guard for the other direction: excluding Astra must not
        exclude the models that genuinely need that route."""
        assert is_openai_responses_model("openai.gpt-5.6-sol") is True
        assert is_openai_responses_model("openai.gpt-5.4") is True

    def test_astra_reaches_converse_so_tool_config_is_supported(self):
        """Astra emits toolUse under a forced toolChoice, which is what makes
        agentic and forced-tool extraction available to it — unlike GPT-5.x."""
        assert supports_tool_config(ASTRA_US) is True
        assert supports_tool_config("openai.gpt-5.6-sol") is False


@pytest.mark.unit
class TestAstraInferenceProfileArns:
    """docs/configuration.md recommends inference-profile ARNs for cost
    allocation, and an ARN is the only form available in GovCloud. Astra's
    rejections are unconditional, so a gate that misses the ARN form fails 100%
    of requests rather than degrading quietly."""

    ARNS = [
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.openai.gpt-6-astra",
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.openai.gpt-6-astra",
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-6-astra",
    ]

    @pytest.mark.parametrize("arn", ARNS)
    def test_astra_recognized_through_an_arn(self, arn):
        assert is_astra_model(arn) is True
        assert strips_sampling_params(arn) is True
        assert supports_document_blocks(arn) is False

    def test_opaque_application_profile_is_a_known_limitation(self):
        """An application-inference-profile ARN cannot be resolved offline (it
        needs GetInferenceProfile), so the gates cannot see through it. Pinned so
        the limitation is explicit rather than a surprise."""
        arn = (
            "arn:aws:bedrock:us-west-2:123456789012:"
            "application-inference-profile/abc123uuid"
        )
        assert is_astra_model(arn) is False
        assert supports_document_blocks(arn) is True


@pytest.mark.unit
class TestAstraCapabilityGates:
    @pytest.mark.parametrize("model_id", [ASTRA_US, ASTRA_GLOBAL])
    def test_astra_cannot_take_document_blocks(self, model_id):
        """Discovery sends whole PDFs as document blocks; Astra rejects them
        ("This model doesn't support the document field for user messages")."""
        assert supports_document_blocks(model_id) is False
        reason = document_blocks_unsupported_reason(model_id)
        assert reason is not None
        assert "document" in reason.lower()
        assert "astra" in reason.lower()

    def test_astra_is_not_in_the_cachepoint_allowlist(self):
        """Explicit cachePoint blocks raise AccessDeniedException for Astra. Its
        IMPLICIT caching needs no request change, so the allowlist would only
        break requests."""
        assert ASTRA_US not in CACHEPOINT_SUPPORTED_MODELS
        assert ASTRA_GLOBAL not in CACHEPOINT_SUPPORTED_MODELS
        assert not any("astra" in m for m in CACHEPOINT_SUPPORTED_MODELS)

    def test_cachepoint_markers_are_stripped_not_translated(
        self, bedrock_client, mock_bedrock_response
    ):
        """<<CACHEPOINT>> must be removed from the text rather than turned into a
        cachePoint block (rejected) or left in place as literal prompt text."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "before<<CACHEPOINT>>after"}],
        )
        blocks = _converse_kwargs(bedrock_client)["messages"][0]["content"]
        assert not any("cachePoint" in b for b in blocks)
        joined = "".join(b.get("text", "") for b in blocks)
        assert "<<CACHEPOINT>>" not in joined
        assert joined == "beforeafter"


@pytest.mark.unit
class TestAstraSamplingParams:
    def test_astra_strips_sampling_params(self):
        assert strips_sampling_params(ASTRA_US) is True
        assert strips_sampling_params(ASTRA_GLOBAL) is True

    def test_temperature_and_top_p_are_not_sent(
        self, bedrock_client, mock_bedrock_response
    ):
        """Astra returns a 400 naming temperature/topP, so neither may be sent
        even when the config supplies them."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            temperature=0.5,
            top_p=0.9,
            top_k=5,
        )
        inference_config = _converse_kwargs(bedrock_client)["inferenceConfig"]
        assert "temperature" not in inference_config
        assert "topP" not in inference_config

    def test_top_k_is_not_forwarded(self, bedrock_client, mock_bedrock_response):
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            top_k=5,
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"] or {}
        assert "top_k" not in amrf
        assert "inferenceConfig" not in amrf


@pytest.mark.unit
class TestAstraMaxTokensCarrier:
    def test_max_tokens_rides_in_inference_config(
        self, bedrock_client, mock_bedrock_response
    ):
        """inferenceConfig.maxTokens is the carrier Astra honors; Claude's
        additionalModelRequestFields.max_tokens must not be used."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            max_tokens=1234,
        )
        kwargs = _converse_kwargs(bedrock_client)
        assert kwargs["inferenceConfig"]["maxTokens"] == 1234
        assert "max_tokens" not in (kwargs["additionalModelRequestFields"] or {})

    def test_max_tokens_defaults_to_the_model_limit(
        self, bedrock_client, mock_bedrock_response
    ):
        """When unset, the client requests the model's cap from
        model_config_limits.yaml rather than letting Bedrock truncate."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US, system_prompt="sys", content=[{"text": "hello"}]
        )
        assert (
            _converse_kwargs(bedrock_client)["inferenceConfig"]["maxTokens"] == 128000
        )

    @pytest.mark.parametrize("model_id", [ASTRA_US, ASTRA_GLOBAL, ASTRA_BARE])
    def test_limits_lookup_resolves_for_every_form(self, model_id):
        """The model_config_limits.yaml pattern must match all three ID forms —
        a miss falls back to an unset cap and Bedrock truncates at its default."""
        assert get_model_max_output_tokens(model_id) == 128000


@pytest.mark.unit
class TestAstraReasoningEffort:
    @pytest.mark.parametrize("effort", ASTRA_EFFORT_LEVELS)
    def test_effort_uses_the_reasoning_carrier(
        self, bedrock_client, mock_bedrock_response, effort
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort=effort,
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"]
        assert amrf["reasoning"] == {"effort": effort}
        # Claude's carrier is REJECTED by Astra ("Unknown parameter:
        # 'output_config'"), not merely ignored, so it must never be sent.
        assert "output_config" not in amrf

    @pytest.mark.parametrize("effort", ["minimal", "bogus", ""])
    def test_unsupported_effort_is_dropped_not_forwarded(
        self, bedrock_client, mock_bedrock_response, effort
    ):
        """Astra 400s on an unknown effort value naming the supported set, so an
        out-of-vocabulary value must be dropped rather than passed through.
        'minimal' is the trap: it is valid for GPT-5.x on the Responses API and
        rejected here."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort=effort,
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"] or {}
        assert "reasoning" not in amrf

    def test_effort_is_case_and_whitespace_tolerant(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort="  MAX ",
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"]
        assert amrf["reasoning"] == {"effort": "max"}

    def test_three_effort_vocabularies_are_distinct(self):
        """Astra is CLAUDE_EFFORT_LEVELS + 'none', and differs from Grok at the
        other end. Reusing either constant would send a rejected value."""
        assert "none" in ASTRA_EFFORT_LEVELS
        assert "max" in ASTRA_EFFORT_LEVELS
        assert "minimal" not in ASTRA_EFFORT_LEVELS
        # vs Grok: same carrier, but Grok 400s on 'max'
        assert "max" not in GROK_EFFORT_LEVELS
        # vs Claude: same values plus 'none', but a different carrier
        assert set(CLAUDE_EFFORT_LEVELS) | {"none"} == set(ASTRA_EFFORT_LEVELS)

    def test_max_is_accepted_for_astra_but_not_for_grok(
        self, bedrock_client, mock_bedrock_response
    ):
        """The concrete consequence of the vocabulary split, pinned end to end."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort="max",
        )
        assert _converse_kwargs(bedrock_client)["additionalModelRequestFields"][
            "reasoning"
        ] == {"effort": "max"}

        bedrock_client._client.converse.reset_mock()
        bedrock_client.invoke_model(
            model_id="us.xai.grok-4.6",
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort="max",
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"] or {}
        assert "reasoning" not in amrf


@pytest.mark.unit
class TestAstraMeteringAndPricing:
    """Astra's implicit caching arrives without any request change, so the only
    thing the accelerator has to get right is that the usage it reports lines up
    with a pricing row."""

    def test_metering_key_matches_a_pricing_row(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response
        result = bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            context="Extraction",
        )
        metering_keys = list(result["metering"].keys())
        assert metering_keys == [f"Extraction/bedrock/{ASTRA_US}"]

        priced = {row["name"] for row in _pricing_rows()}
        # The metering key is "<context>/bedrock/<model_id>"; pricing rows are
        # keyed "bedrock/<model_id>".
        assert f"bedrock/{ASTRA_US}" in priced
        assert f"bedrock/{ASTRA_GLOBAL}" in priced

    def test_implicit_cache_read_tokens_flow_into_metering(
        self, bedrock_client, mock_bedrock_response
    ):
        """A live repeated prefix billed inputTokens=2 with
        cacheReadInputTokens=2707. The discount only reaches cost reports if the
        whole usage dict is carried through, so pin that."""
        mock_bedrock_response["usage"] = {
            "inputTokens": 2,
            "outputTokens": 5,
            "totalTokens": 2714,
            "cacheReadInputTokens": 2707,
        }
        bedrock_client._client.converse.return_value = mock_bedrock_response
        result = bedrock_client.invoke_model(
            model_id=ASTRA_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            context="Extraction",
        )
        usage = result["metering"][f"Extraction/bedrock/{ASTRA_US}"]
        assert usage["cacheReadInputTokens"] == 2707
        assert usage["inputTokens"] == 2
        assert usage["requests"] == 1

    def test_pricing_rows_use_the_short_context_rates(self):
        """The rows deliberately record the <=272K band. If someone switches them
        to the long band, the comment in pricing.yaml must be updated too — this
        pins which band is in the file so the docs can't silently drift."""
        rows = {
            row["name"]: {u["name"]: float(u["price"]) for u in row["units"]}
            for row in _pricing_rows()
            if "astra" in row["name"]
        }
        assert rows[f"bedrock/{ASTRA_US}"]["inputTokens"] == pytest.approx(11.0 / 1e6)
        assert rows[f"bedrock/{ASTRA_US}"]["outputTokens"] == pytest.approx(55.0 / 1e6)
        assert rows[f"bedrock/{ASTRA_GLOBAL}"]["inputTokens"] == pytest.approx(
            10.0 / 1e6
        )
        assert rows[f"bedrock/{ASTRA_GLOBAL}"]["outputTokens"] == pytest.approx(
            50.0 / 1e6
        )
        # Global CRIS must stay cheaper than US geo CRIS, or the "prefer global"
        # guidance in the docs is wrong.
        assert (
            rows[f"bedrock/{ASTRA_GLOBAL}"]["inputTokens"]
            < rows[f"bedrock/{ASTRA_US}"]["inputTokens"]
        )


@pytest.mark.unit
class TestAstraResponseParsing:
    def test_text_is_extracted_past_the_reasoning_block(
        self, bedrock_client, mock_bedrock_response
    ):
        """Astra is a reasoning model and can emit reasoningContent before the
        answer, so content[0] is not necessarily the text."""
        assert (
            bedrock_client.extract_text_from_response(mock_bedrock_response)
            == "test response"
        )

    def test_tool_use_is_found_past_the_reasoning_block(self, bedrock_client):
        response = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                        {
                            "toolUse": {
                                "name": "extract_fields",
                                "input": {"account_number": "123"},
                            }
                        },
                    ]
                }
            },
            "stopReason": "tool_use",
        }
        assert bedrock_client.extract_tool_use_from_response(response) == {
            "account_number": "123"
        }
