# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for xAI Grok support on the Converse path.

Grok is the first non-Anthropic/non-Nova family to reach Converse, so these
tests pin the behaviors that differ from the Claude/Nova assumptions the client
was originally written against. Every expectation here was verified live against
``us.xai.grok-4.6`` on bedrock-runtime Converse in us-west-2 on 2026-09-02 — see
the "xAI Grok" comment block in ``idp_common/bedrock/client.py``.
"""

from unittest.mock import MagicMock

import pytest

from idp_common.bedrock.client import (
    CACHEPOINT_SUPPORTED_MODELS,
    GROK_EFFORT_LEVELS,
    BedrockClient,
    document_blocks_unsupported_reason,
    is_claude_4_7_model,
    is_claude_effort_model,
    is_grok_model,
    strips_sampling_params,
    supports_document_blocks,
    supports_tool_config,
)

GROK_US = "us.xai.grok-4.6"
GROK_GLOBAL = "global.xai.grok-4.6"


@pytest.fixture
def mock_bedrock_response():
    """Minimal Converse response, with the leading reasoningContent block Grok
    always emits."""
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
class TestGrokIdentification:
    @pytest.mark.parametrize("model_id", [GROK_US, GROK_GLOBAL, "xai.grok-4.6"])
    def test_grok_ids_recognized(self, model_id):
        assert is_grok_model(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-4-8:1m",
            "us.amazon.nova-2-lite-v1:0",
            "openai.gpt-5.6-sol",
            "LambdaHook",
            "",
        ],
    )
    def test_other_families_not_grok(self, model_id):
        assert is_grok_model(model_id) is False

    def test_grok_is_not_mistaken_for_a_claude_variant(self):
        """Grok must not pick up Claude-only request fields."""
        assert is_claude_4_7_model(GROK_US) is False
        assert is_claude_effort_model(GROK_US) is False

    def test_grok_effort_vocabulary_differs_from_claude(self):
        """Grok adds 'none' and REJECTS 'max' — do not reuse CLAUDE_EFFORT_LEVELS."""
        assert "none" in GROK_EFFORT_LEVELS
        assert "max" not in GROK_EFFORT_LEVELS


@pytest.mark.unit
class TestGrokInferenceProfileArns:
    """docs/configuration.md recommends inference-profile ARNs for cost
    allocation, and an ARN is the only form available in GovCloud. Grok's
    rejections are unconditional, so a gate that misses the ARN form fails 100%
    of requests rather than degrading quietly."""

    ARNS = [
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.xai.grok-4.6",
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.xai.grok-4.6",
        "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/us.xai.grok-4.6",
        "arn:aws:bedrock:us-east-1::foundation-model/xai.grok-4.6",
    ]

    @pytest.mark.parametrize("arn", ARNS)
    def test_grok_recognized_through_an_arn(self, arn):
        assert is_grok_model(arn) is True
        assert strips_sampling_params(arn) is True
        assert supports_document_blocks(arn) is False

    def test_claude_arn_is_not_grok_but_still_strips_params(self):
        """Regression guard for the adjacent pre-existing gap: Sonnet 5 rejects
        temperature, and must be recognized through an ARN too."""
        arn = (
            "arn:aws:bedrock:us-west-2:123456789012:"
            "inference-profile/us.anthropic.claude-sonnet-5"
        )
        assert is_grok_model(arn) is False
        assert strips_sampling_params(arn) is True
        assert supports_document_blocks(arn) is True

    def test_opaque_application_profile_is_a_known_limitation(self):
        """An application-inference-profile ARN cannot be resolved offline (it
        needs GetInferenceProfile), so the gates cannot see through it. Pinned so
        the limitation is explicit rather than a surprise."""
        arn = (
            "arn:aws:bedrock:us-west-2:123456789012:"
            "application-inference-profile/abc123uuid"
        )
        assert is_grok_model(arn) is False
        assert supports_document_blocks(arn) is True


@pytest.mark.unit
class TestGrokCapabilityGates:
    def test_grok_reaches_converse_so_tool_config_is_supported(self):
        """Unlike GPT-5.x, Grok can carry a toolConfig — this is what makes
        agentic extraction available to it."""
        assert supports_tool_config(GROK_US) is True

    @pytest.mark.parametrize("model_id", [GROK_US, GROK_GLOBAL])
    def test_grok_cannot_take_document_blocks(self, model_id):
        assert supports_document_blocks(model_id) is False
        reason = document_blocks_unsupported_reason(model_id)
        assert reason is not None
        assert "document" in reason.lower()

    @pytest.mark.parametrize(
        "model_id", ["us.anthropic.claude-sonnet-5", "us.amazon.nova-pro-v1:0"]
    )
    def test_claude_and_nova_still_take_document_blocks(self, model_id):
        assert supports_document_blocks(model_id) is True
        assert document_blocks_unsupported_reason(model_id) is None

    def test_grok_is_not_in_the_cachepoint_allowlist(self):
        """Explicit cachePoint blocks raise AccessDeniedException for Grok."""
        assert GROK_US not in CACHEPOINT_SUPPORTED_MODELS
        assert GROK_GLOBAL not in CACHEPOINT_SUPPORTED_MODELS
        assert not any("xai" in m for m in CACHEPOINT_SUPPORTED_MODELS)


@pytest.mark.unit
class TestGrokSamplingParams:
    def test_grok_strips_sampling_params(self):
        assert strips_sampling_params(GROK_US) is True

    def test_nova_still_receives_sampling_params(self):
        assert strips_sampling_params("us.amazon.nova-pro-v1:0") is False

    def test_temperature_and_top_p_are_not_sent(
        self, bedrock_client, mock_bedrock_response
    ):
        """Grok returns a 400 naming temperature/topP, so neither may be sent
        even when the config supplies them."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=GROK_US,
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
            model_id=GROK_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            top_k=5,
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"] or {}
        assert "top_k" not in amrf
        assert "inferenceConfig" not in amrf


@pytest.mark.unit
class TestGrokMaxTokensCarrier:
    def test_max_tokens_rides_in_inference_config(
        self, bedrock_client, mock_bedrock_response
    ):
        """Grok honors inferenceConfig.maxTokens and SILENTLY IGNORES Claude's
        additionalModelRequestFields.max_tokens, so the value must land in
        inferenceConfig or output goes uncapped."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=GROK_US,
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
            model_id=GROK_US, system_prompt="sys", content=[{"text": "hello"}]
        )
        assert (
            _converse_kwargs(bedrock_client)["inferenceConfig"]["maxTokens"] == 524288
        )

    def test_claude_keeps_its_own_carrier(self, bedrock_client, mock_bedrock_response):
        """Regression guard: generalizing the carrier must not move Claude's."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=[{"text": "hello"}],
            max_tokens=999,
        )
        kwargs = _converse_kwargs(bedrock_client)
        assert kwargs["additionalModelRequestFields"]["max_tokens"] == 999
        assert "maxTokens" not in kwargs["inferenceConfig"]

    def test_over_limit_retry_can_clamp_grok(self):
        """_apply_max_tokens_limit must reach the inferenceConfig carrier."""
        params = {"inferenceConfig": {"maxTokens": 524288}}
        assert BedrockClient._apply_max_tokens_limit(params, 64000) is True
        assert params["inferenceConfig"]["maxTokens"] == 64000


@pytest.mark.unit
class TestGrokReasoningEffort:
    @pytest.mark.parametrize("effort", GROK_EFFORT_LEVELS)
    def test_effort_uses_the_reasoning_carrier(
        self, bedrock_client, mock_bedrock_response, effort
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=GROK_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort=effort,
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"]
        assert amrf["reasoning"] == {"effort": effort}
        # Claude's carrier must not be used — Grok silently ignores it.
        assert "output_config" not in amrf

    @pytest.mark.parametrize("effort", ["max", "bogus", "MINIMAL"])
    def test_unsupported_effort_is_dropped_not_forwarded(
        self, bedrock_client, mock_bedrock_response, effort
    ):
        """Grok rejects 'max' with a 400, and unknown additionalModelRequestFields
        keys are silently ignored — so an invalid value must be dropped rather
        than passed through."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=GROK_US,
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
            model_id=GROK_US,
            system_prompt="sys",
            content=[{"text": "hello"}],
            reasoning_effort="  XHIGH ",
        )
        amrf = _converse_kwargs(bedrock_client)["additionalModelRequestFields"]
        assert amrf["reasoning"] == {"effort": "xhigh"}


@pytest.mark.unit
class TestGrokCachepointMarkers:
    def test_cachepoint_markers_are_stripped_not_translated(
        self, bedrock_client, mock_bedrock_response
    ):
        """Grok rejects explicit cachePoint blocks, so <<CACHEPOINT>> must be
        removed from the text rather than turned into a cachePoint block or left
        in place as literal prompt text."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        bedrock_client.invoke_model(
            model_id=GROK_US,
            system_prompt="sys",
            content=[{"text": "before<<CACHEPOINT>>after"}],
        )
        messages = _converse_kwargs(bedrock_client)["messages"]
        blocks = messages[0]["content"]
        assert not any("cachePoint" in b for b in blocks)
        joined = "".join(b.get("text", "") for b in blocks)
        assert "<<CACHEPOINT>>" not in joined
        assert joined == "beforeafter"


@pytest.mark.unit
class TestGrokAgenticPredicate:
    """The Strands/agentic path consults ``strips_sampling_params`` to decide
    whether to forward temperature/top_p. A live run caught that path forwarding
    ``top_p`` to Grok (a 400), so pin the predicate here — unconditionally,
    because the agentic module itself can only be imported when strands is
    installed (see tests/unit/extraction/test_agentic_idp_unit.py for the wiring
    tests that exercise _get_inference_params / _build_model_config directly)."""

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            (GROK_US, True),
            (GROK_GLOBAL, True),
            ("us.anthropic.claude-opus-4-8", True),
            ("us.anthropic.claude-sonnet-5", True),
            ("us.amazon.nova-pro-v1:0", False),
            ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", False),
        ],
    )
    def test_sampling_param_strip_decision(self, model_id, expected):
        assert strips_sampling_params(model_id) is expected


@pytest.mark.unit
class TestGrokResponseParsing:
    def test_text_is_extracted_past_the_reasoning_block(
        self, bedrock_client, mock_bedrock_response
    ):
        """Grok always emits reasoningContent before the answer, so content[0]
        is never the text."""
        assert (
            bedrock_client.extract_text_from_response(mock_bedrock_response)
            == "test response"
        )

    def test_reasoning_only_response_yields_empty_string(self, bedrock_client):
        """A maxTokens-truncated Grok response can contain ONLY reasoningContent."""
        response = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "thinking"}}}
                    ]
                }
            },
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 32, "outputTokens": 64, "totalTokens": 96},
        }
        assert bedrock_client.extract_text_from_response(response) == ""

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
