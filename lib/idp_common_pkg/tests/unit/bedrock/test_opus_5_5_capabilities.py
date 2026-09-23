# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claude Opus 5.5's capability answers, and the two ways they are easy to get wrong.

Every fact asserted here was measured against ``us.anthropic.claude-opus-5-5`` on
bedrock-runtime Converse in us-west-2 on 2026-09-23, not read off a model card:

======================================  =========================================
Request                                 Result
======================================  =========================================
plain Converse                          ``stopReason: end_turn``
``inferenceConfig.temperature``         rejected, "`temperature` is deprecated"
``inferenceConfig.topP``                rejected, "`top_p` is deprecated"
``output_config.effort`` low / xhigh    accepted
``thinking: {"type": "disabled"}``      rejected, use adaptive + effort
``toolChoice: {"auto": {}}``            ``stopReason: tool_use``, toolUse emitted
``toolChoice: {"any": {}}``             rejected
``toolChoice: {"tool": {...}}``         rejected
explicit ``cachePoint``                 accepted, 7,204 cache-write tokens
``document`` content block              accepted
``anthropic_beta: context-1m-...``      accepted (this is what ``:1m`` becomes)
======================================  =========================================

⚠️ The last row is the one to read carefully, because the obvious way to test ``:1m``
tests nothing. ``us.anthropic.claude-opus-5-5:1m`` is **not a model id Bedrock knows** —
sending it returns *"The provided model identifier is invalid"*, and so does
``us.anthropic.claude-opus-5:1m``, and so does every other ``:1m`` id in this repository.
The suffix is a local convention: ``invoke_model`` strips it and sets
``additionalModelRequestFields.anthropic_beta = ["context-1m-2025-08-07"]`` instead (see
``LONG_CONTEXT_SUFFIX`` and ``metering_model_id``). So a probe that puts the suffix on the
wire measures the convention, not the model, and "invalid identifier" from one is not
evidence about the other. What was actually verified is the header, on the base id.

Two failure modes this file exists to catch, both arising from the same fact —
``"claude-opus-5"`` is a **substring** of ``"claude-opus-5-5"``, while some gates
match exactly and others by substring or prefix:

1. **A gate that matches exactly silently excludes Opus 5.5.** The sampling-param
   set is the dangerous one: missing from it, ``temperature`` and ``top_p`` go on
   the wire and every request 400s. The set is keyed on the full base name, so
   Opus 5's entry does not cover it.
2. **A gate that matches by substring silently includes Opus 5.5.** That happens
   to be the right answer everywhere it occurs today (effort, the vision tier, the
   cache minimum, the limits patterns), which is exactly why it needs pinning: a
   future Opus 5.x for which one of those answers differs would inherit the wrong
   one with nothing failing.
"""

from __future__ import annotations

import pytest

from idp_common.bedrock.client import (
    CACHEPOINT_SUPPORTED_MODELS,
    forced_tool_choice_unsupported_reason,
    is_claude_4_7_model,
    is_claude_effort_model,
    strips_sampling_params,
    supports_document_blocks,
    supports_forced_tool_choice,
    supports_tool_config,
    thinking_can_be_disabled,
)
from idp_common.bedrock.model_utils import (
    get_model_max_input_tokens,
    get_model_max_output_tokens,
    visual_token_cap_for_model,
)
from idp_common.bedrock.prompt_cache import min_cacheable_prefix_tokens

BASE = "us.anthropic.claude-opus-5-5"
ARN = (
    "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
    "us.anthropic.claude-opus-5-5"
)
ALL_FORMS = [
    BASE,
    "eu.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
    f"{BASE}:1m",
    ARN,
]


class TestSamplingParameters:
    """Failure mode 1: the set that matches exactly."""

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_sampling_params_are_stripped(self, model_id):
        """Live: `temperature` is deprecated for this model / `top_p` is
        deprecated for this model. Both arrive as a ValidationException, so leaving
        Opus 5.5 out of the set makes every request fail, not degrade."""
        assert is_claude_4_7_model(model_id) is True
        assert strips_sampling_params(model_id) is True

    def test_the_answer_is_not_inherited_from_opus_5(self):
        """The set is matched on the whole base name. If that ever changes to a
        prefix match, this test keeps passing while the entry becomes removable —
        so assert the entry itself is what carries the answer."""
        from idp_common.bedrock.client import _CLAUDE_4_7_BASE_NAMES

        assert "anthropic.claude-opus-5-5" in _CLAUDE_4_7_BASE_NAMES


class TestThinkingAndEffort:
    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_effort_is_accepted(self, model_id):
        """The ARN form is in this sweep deliberately. An account-scoped
        inference-profile ARN is the only way to name a model in GovCloud, and
        losing effort there is not a small degradation on this model: effort is the
        only thinking control it has."""
        assert is_claude_effort_model(model_id) is True

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_thinking_cannot_be_disabled(self, model_id):
        """Live: '"thinking.type.disabled" is not supported for this model. Use
        "thinking.type.adaptive" and "output_config.effort" to control thinking
        behavior.' Lower effort is the only lever."""
        assert thinking_can_be_disabled(model_id) is False

    def test_opus_5_can_still_disable_thinking(self):
        """Opus 5 accepts a disabled thinking block; this is the one capability
        where 5.5 is more restricted than 5, so the two must not share an answer."""
        assert thinking_can_be_disabled("us.anthropic.claude-opus-5") is True
        assert is_claude_effort_model("us.anthropic.claude-opus-5") is True


class TestToolUse:
    """The capability that splits one gate into two."""

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_a_toolconfig_is_supported(self, model_id):
        """Live: toolChoice auto returns stopReason tool_use and a toolUse block.
        Opus 5.5 is fully tool-capable — the restriction is only on forcing."""
        assert supports_tool_config(model_id) is True

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_forcing_is_not_supported(self, model_id):
        """Live: 'tool_choice: type "tool" and "any" are not supported for this
        model.' for both forcing modes."""
        assert supports_forced_tool_choice(model_id) is False
        reason = forced_tool_choice_unsupported_reason(model_id)
        assert reason and "toolChoice" in reason

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-opus-5",
            "us.anthropic.claude-opus-5:1m",
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-4-8",
            "us.amazon.nova-2-lite-v1:0",
        ],
    )
    def test_every_other_converse_family_still_forces(self, model_id):
        """The non-vacuity check in the other direction: if this gate started
        answering False for everything, the forced-tool path would be dead code and
        nothing above would notice."""
        assert supports_forced_tool_choice(model_id) is True

    def test_the_forced_tool_path_degrades_rather_than_raising(self):
        """A user who selects Opus 5.5 with forced-tool enabled gets the prose
        schema and an audit reason, not a 400 and not a silent no-op."""
        from idp_common.extraction.forced_tool import should_force_tool

        force, reason = should_force_tool(
            BASE, True, {"type": "object", "properties": {"a": {"type": "string"}}}
        )
        assert force is False
        assert reason and "forced toolChoice" in reason

    def test_the_client_refuses_a_forced_choice_before_the_network(self):
        """A forced choice must not reach Bedrock: the 400 arrives only after the
        retry budget is spent and its message does not say what to do instead."""
        from idp_common.bedrock.client import BedrockClient

        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": "t",
                        "inputSchema": {"json": {"type": "object", "properties": {}}},
                    }
                }
            ]
        }
        for choice in ({"any": {}}, {"tool": {"name": "t"}}):
            with pytest.raises(ValueError, match="forced toolChoice"):
                BedrockClient._resolve_tool_config(BASE, tool_config, choice)

        # auto is unaffected and must still go through.
        merged = BedrockClient._resolve_tool_config(BASE, tool_config, {"auto": {}})
        assert merged["toolChoice"] == {"auto": {}}


class TestCachingAndWindows:
    """Failure mode 2: answers that arrive by substring and must stay pinned."""

    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_explicit_cachepoints_are_supported(self, prefix):
        """Live: an explicit cachePoint block returned
        cacheWriteInputTokens=7204 with a 5m TTL — so this is a real discount, not
        an aspirational listing."""
        assert f"{prefix}.anthropic.claude-opus-5-5" in CACHEPOINT_SUPPORTED_MODELS
        assert f"{prefix}.anthropic.claude-opus-5-5:1m" in CACHEPOINT_SUPPORTED_MODELS

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_the_minimum_cacheable_prefix_is_512(self, model_id):
        assert min_cacheable_prefix_tokens(model_id) == 512

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_the_vision_tier_is_high_resolution(self, model_id):
        """Opus 5.5 shares the Opus 4.7+ tokenizer, so an image costs up to 4,784
        tokens rather than 1,568."""
        assert visual_token_cap_for_model(model_id) == 4784

    def test_the_windows_match_the_model_card(self):
        assert get_model_max_output_tokens(BASE) == 128000
        assert get_model_max_input_tokens(BASE) == 200000
        assert get_model_max_input_tokens(f"{BASE}:1m") == 1000000

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_document_blocks_are_accepted(self, model_id):
        """Live: a PDF `document` content block was accepted (1,605 input tokens),
        so Opus 5.5 is usable for discovery and whole-PDF extraction."""
        assert supports_document_blocks(model_id) is True

    def test_the_1m_suffix_becomes_a_header_and_never_reaches_bedrock(self):
        """``:1m`` is a local convention, not a Bedrock model id.

        Live: ``anthropic_beta: ["context-1m-2025-08-07"]`` is accepted on
        ``us.anthropic.claude-opus-5-5``, which is the request the product actually
        makes. Sending the suffixed id instead returns "The provided model identifier
        is invalid" — on Opus 5, Opus 4.8 and Sonnet 5 as well, so that error says
        nothing about this model.

        Asserted through ``invoke_model`` rather than through ``metering_model_id``
        alone because the substitution has three halves that can disagree: the id on
        the wire, the header that replaces the suffix, and the metering key. A test
        of only the third would pass while the request went out unsuffixed and
        without the header — i.e. silently capped at the short window.
        """
        from unittest.mock import MagicMock

        from idp_common.bedrock.client import BedrockClient

        client = BedrockClient(region="us-west-2", metrics_enabled=False)
        client._client = MagicMock()
        client._client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12},
        }

        result = client.invoke_model(
            model_id=f"{BASE}:1m",
            system_prompt="test",
            content=[{"text": "test"}],
            context="Extraction",
        )

        call = client._client.converse.call_args
        assert call.kwargs["modelId"] == BASE
        assert call.kwargs["additionalModelRequestFields"]["anthropic_beta"] == [
            "context-1m-2025-08-07"
        ]
        # One price per model: the 1M window carries no premium, so the suffix must
        # not create a second metering key (#899).
        assert list(result["metering"]) == [f"Extraction/bedrock/{BASE}"]


class TestPricing:
    """The cache-read rate is the one number that cannot be derived."""

    @staticmethod
    def _units(name):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[5]
        data = yaml.safe_load((root / "config_library" / "pricing.yaml").read_text())
        for entry in data["pricing"]:
            if entry["name"] == name:
                return {u["name"]: float(u["price"]) for u in entry["units"]}
        raise AssertionError(f"no pricing entry for {name}")

    @pytest.mark.parametrize(
        "name",
        [
            "bedrock/us.anthropic.claude-opus-5-5",
            "bedrock/us.anthropic.claude-opus-5-5:1m",
            "bedrock/eu.anthropic.claude-opus-5-5",
            "bedrock/eu.anthropic.claude-opus-5-5:1m",
            "bedrock/global.anthropic.claude-opus-5-5",
            "bedrock/global.anthropic.claude-opus-5-5:1m",
        ],
    )
    def test_the_published_rates(self, name):
        units = self._units(name)
        assert units["inputTokens"] == 4.0e-6
        assert units["outputTokens"] == 2.0e-5
        assert units["cacheWriteInputTokens"] == 5.0e-6  # 1.25x input, as usual
        # 0.05x input, NOT the 0.1x every other model in the file uses. Deriving it
        # would bill cached reads at 2x the real rate.
        assert units["cacheReadInputTokens"] == 2.0e-7

    def test_the_cache_read_multiplier_is_the_exception_it_claims_to_be(self):
        """Pin the comparison rather than the claim: if Opus 5's cache read ever
        moves to 0.05x too, the note in pricing.yaml stops being true."""
        five_five = self._units("bedrock/us.anthropic.claude-opus-5-5")
        five = self._units("bedrock/us.anthropic.claude-opus-5")
        assert five_five["cacheReadInputTokens"] / five_five[
            "inputTokens"
        ] == pytest.approx(0.05)
        assert five["cacheReadInputTokens"] / five["inputTokens"] == pytest.approx(0.1)
