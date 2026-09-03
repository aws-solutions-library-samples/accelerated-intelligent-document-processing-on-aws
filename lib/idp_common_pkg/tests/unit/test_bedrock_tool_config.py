# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Converse toolConfig / toolChoice plumbing in BedrockClient."""

from unittest.mock import MagicMock

import pytest

from idp_common.bedrock.client import (
    BedrockClient,
    supports_tool_config,
    tool_config_unsupported_reason,
)

_TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "extract_fields",
                "description": "Return the extracted fields.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {"account_number": {"type": "string"}},
                    }
                },
            }
        }
    ]
}

_TOOL_CHOICE = {"tool": {"name": "extract_fields"}}


@pytest.fixture
def mock_bedrock_response():
    """Minimal Converse response."""
    return {
        "output": {"message": {"content": [{"text": "test response"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
    }


@pytest.fixture
def bedrock_client():
    """BedrockClient with a mocked boto3 client (no live AWS calls)."""
    client = BedrockClient(region="us-west-2", metrics_enabled=False)
    client._client = MagicMock()
    return client


@pytest.mark.unit
class TestToolConfigCapabilityGate:
    """The gate is 'does this model reach Converse at all'."""

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-4-8:1m",
            "us.anthropic.claude-3-5-haiku-20241022-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-2-lite-v1:0:flex",
            "eu.anthropic.claude-sonnet-4-6",
        ],
    )
    def test_converse_models_supported(self, model_id):
        assert supports_tool_config(model_id) is True
        assert tool_config_unsupported_reason(model_id) is None

    def test_lambda_hook_not_supported(self):
        assert supports_tool_config("LambdaHook") is False
        reason = tool_config_unsupported_reason("LambdaHook")
        assert reason is not None and "LambdaHook" in reason

    @pytest.mark.parametrize(
        "model_id", ["openai.gpt-5.4", "openai.gpt-5.6-sol", "us.openai.gpt-5.5"]
    )
    def test_openai_responses_models_not_supported(self, model_id):
        assert supports_tool_config(model_id) is False
        reason = tool_config_unsupported_reason(model_id)
        assert reason is not None and "Responses API" in reason


@pytest.mark.unit
class TestInvokeModelToolConfigPlumbing:
    """toolConfig must be absent by default and passed through when supplied."""

    def test_no_tool_config_by_default(self, bedrock_client, mock_bedrock_response):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.amazon.nova-pro-v1:0",
            system_prompt="test",
            content=[{"text": "test"}],
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert "toolConfig" not in kwargs

    def test_tool_config_passed_through(self, bedrock_client, mock_bedrock_response):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config=_TOOL_CONFIG,
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert kwargs["toolConfig"] == _TOOL_CONFIG
        assert "toolChoice" not in kwargs["toolConfig"]

    def test_tool_choice_merged_into_tool_config(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config=_TOOL_CONFIG,
            tool_choice=_TOOL_CHOICE,
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert kwargs["toolConfig"]["toolChoice"] == _TOOL_CHOICE
        assert kwargs["toolConfig"]["tools"] == _TOOL_CONFIG["tools"]

    def test_callers_tool_config_not_mutated(
        self, bedrock_client, mock_bedrock_response
    ):
        """Callers reuse one per-class toolConfig; it must not be mutated."""
        bedrock_client._client.converse.return_value = mock_bedrock_response
        caller_config = {"tools": _TOOL_CONFIG["tools"]}

        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config=caller_config,
            tool_choice=_TOOL_CHOICE,
        )

        assert "toolChoice" not in caller_config

    def test_tool_choice_param_overrides_embedded_choice(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config={**_TOOL_CONFIG, "toolChoice": {"auto": {}}},
            tool_choice=_TOOL_CHOICE,
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert kwargs["toolConfig"]["toolChoice"] == _TOOL_CHOICE

    def test_embedded_tool_choice_alone_is_honored(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config={**_TOOL_CONFIG, "toolChoice": {"any": {}}},
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert kwargs["toolConfig"]["toolChoice"] == {"any": {}}

    def test_other_request_params_unchanged_with_tool_config(
        self, bedrock_client, mock_bedrock_response
    ):
        """Adding a toolConfig must not perturb the rest of the request."""
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id="us.amazon.nova-pro-v1:0",
            system_prompt="test",
            content=[{"text": "test"}],
        )
        baseline = dict(bedrock_client._client.converse.call_args.kwargs)

        bedrock_client._client.converse.reset_mock()
        bedrock_client.invoke_model(
            model_id="us.amazon.nova-pro-v1:0",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config=_TOOL_CONFIG,
            tool_choice=_TOOL_CHOICE,
        )
        with_tools = dict(bedrock_client._client.converse.call_args.kwargs)

        assert set(with_tools) - set(baseline) == {"toolConfig"}
        for key, value in baseline.items():
            assert with_tools[key] == value

    def test_callable_interface_forwards_tool_config(
        self, bedrock_client, mock_bedrock_response
    ):
        """`invoke_model` is exported as the callable client instance."""
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            tool_config=_TOOL_CONFIG,
            tool_choice=_TOOL_CHOICE,
        )

        kwargs = bedrock_client._client.converse.call_args.kwargs
        assert kwargs["toolConfig"]["toolChoice"] == _TOOL_CHOICE

    def test_metering_unchanged_with_tool_config(
        self, bedrock_client, mock_bedrock_response
    ):
        bedrock_client._client.converse.return_value = mock_bedrock_response

        result = bedrock_client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="test",
            content=[{"text": "test"}],
            context="Extraction",
            tool_config=_TOOL_CONFIG,
        )

        assert result["metering"] == {
            "Extraction/bedrock/us.anthropic.claude-sonnet-5": {
                "inputTokens": 100,
                "outputTokens": 50,
                "totalTokens": 150,
                "requests": 1,
            }
        }


@pytest.mark.unit
class TestInvokeModelToolConfigErrors:
    """Unsupported routes must fail loudly, never silently drop the schema."""

    def test_lambda_hook_with_tool_config_raises(self, bedrock_client):
        with pytest.raises(ValueError, match="not supported for model 'LambdaHook'"):
            bedrock_client.invoke_model(
                model_id="LambdaHook",
                system_prompt="test",
                content=[{"text": "test"}],
                model_lambda_hook_arn="arn:aws:lambda:us-west-2:123456789012:function:f",
                tool_config=_TOOL_CONFIG,
            )
        bedrock_client._client.converse.assert_not_called()

    def test_lambda_hook_with_tool_choice_only_raises(self, bedrock_client):
        with pytest.raises(ValueError, match="not supported for model 'LambdaHook'"):
            bedrock_client.invoke_model(
                model_id="LambdaHook",
                system_prompt="test",
                content=[{"text": "test"}],
                model_lambda_hook_arn="arn:aws:lambda:us-west-2:123456789012:function:f",
                tool_choice=_TOOL_CHOICE,
            )

    def test_openai_responses_model_with_tool_config_raises(self, bedrock_client):
        with pytest.raises(ValueError, match="Responses API"):
            bedrock_client.invoke_model(
                model_id="openai.gpt-5.6",
                system_prompt="test",
                content=[{"text": "test"}],
                tool_config=_TOOL_CONFIG,
            )
        bedrock_client._client.converse.assert_not_called()

    def test_tool_choice_without_tool_config_raises(self, bedrock_client):
        with pytest.raises(ValueError, match="without tool_config"):
            bedrock_client.invoke_model(
                model_id="us.anthropic.claude-sonnet-5",
                system_prompt="test",
                content=[{"text": "test"}],
                tool_choice=_TOOL_CHOICE,
            )
        bedrock_client._client.converse.assert_not_called()


@pytest.mark.unit
class TestExtractToolUseFromResponse:
    """The extractor must tolerate reasoningContent and text-only answers."""

    def _wrap(self, content, stop_reason="tool_use"):
        return {
            "response": {
                "output": {"message": {"role": "assistant", "content": content}},
                "stopReason": stop_reason,
            },
            "metering": {},
        }

    def test_plain_tool_use_block(self, bedrock_client):
        response = self._wrap(
            [
                {
                    "toolUse": {
                        "name": "extract_fields",
                        "input": {"account_number": "123"},
                    }
                }
            ]
        )
        assert bedrock_client.extract_tool_use_from_response(response) == {
            "account_number": "123"
        }

    def test_reasoning_content_precedes_tool_use(self, bedrock_client):
        """Sonnet 5 / 4.6+ emit reasoningContent first — content[0] is not the answer."""
        response = self._wrap(
            [
                {"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
                {
                    "toolUse": {
                        "name": "extract_fields",
                        "input": {"account_number": "9"},
                    }
                },
            ]
        )
        assert bedrock_client.extract_tool_use_from_response(response) == {
            "account_number": "9"
        }

    def test_text_only_response_returns_none(self, bedrock_client):
        response = self._wrap([{"text": '{"account_number": "123"}'}], "end_turn")
        assert bedrock_client.extract_tool_use_from_response(response) is None
        # The text path still works, so callers can fall back.
        assert (
            bedrock_client.extract_text_from_response(response)
            == '{"account_number": "123"}'
        )

    def test_multiple_content_blocks(self, bedrock_client):
        response = self._wrap(
            [
                {"reasoningContent": {"reasoningText": {"text": "step 1"}}},
                {"text": "Here you go:"},
                {"toolUse": {"name": "extract_fields", "input": {"a": 1}}},
                {"text": "trailing commentary"},
            ]
        )
        assert bedrock_client.extract_tool_use_from_response(response) == {"a": 1}

    def test_tool_name_filter_selects_matching_block(self, bedrock_client):
        response = self._wrap(
            [
                {"toolUse": {"name": "other_tool", "input": {"wrong": True}}},
                {"toolUse": {"name": "extract_fields", "input": {"right": True}}},
            ]
        )
        assert bedrock_client.extract_tool_use_from_response(
            response, tool_name="extract_fields"
        ) == {"right": True}
        assert (
            bedrock_client.extract_tool_use_from_response(
                response, tool_name="missing_tool"
            )
            is None
        )

    def test_raw_unwrapped_response(self, bedrock_client):
        raw = {
            "output": {
                "message": {"content": [{"toolUse": {"name": "t", "input": {"b": 2}}}]}
            },
            "stopReason": "tool_use",
        }
        assert bedrock_client.extract_tool_use_from_response(raw) == {"b": 2}

    def test_empty_content_returns_none(self, bedrock_client):
        assert bedrock_client.extract_tool_use_from_response(self._wrap([])) is None

    def test_malformed_response_returns_none(self, bedrock_client):
        assert bedrock_client.extract_tool_use_from_response({"response": {}}) is None

    def test_non_dict_tool_input_returns_none(self, bedrock_client):
        response = self._wrap([{"toolUse": {"name": "t", "input": "not-a-dict"}}])
        assert bedrock_client.extract_tool_use_from_response(response) is None
