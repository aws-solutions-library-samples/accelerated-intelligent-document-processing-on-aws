# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Integration tests for BedrockClient serviceTier with model ID suffixes."""

import pytest

from idp_common.bedrock.client import BedrockClient


@pytest.mark.integration
class TestBedrockClientServiceTierIntegration:
    """Integration tests for service tier with real Bedrock API calls."""

    @pytest.fixture
    def bedrock_client(self):
        """Create BedrockClient instance for us-west-2."""
        return BedrockClient(region="us-west-2", metrics_enabled=False)

    def test_model_id_with_flex_suffix(self, bedrock_client):
        """Test model ID with :flex suffix."""
        response = bedrock_client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0:flex",
            system_prompt="You are a helpful assistant.",
            content=[{"text": "What is 2+2? Answer in one word."}],
            max_tokens=10,
        )

        assert response is not None
        assert "output" in response
        assert "message" in response["output"]

    def test_model_id_with_priority_suffix(self, bedrock_client):
        """Test model ID with :priority suffix."""
        response = bedrock_client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0:priority",
            system_prompt="You are a helpful assistant.",
            content=[{"text": "Say 'hello' in one word."}],
            max_tokens=5,
        )

        assert response is not None
        assert "output" in response

    def test_model_id_without_suffix(self, bedrock_client):
        """Test model ID without suffix (uses standard/default tier)."""
        response = bedrock_client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0",
            system_prompt="You are a helpful assistant.",
            content=[{"text": "Count to 3."}],
            max_tokens=20,
        )

        assert response is not None
        assert "output" in response

    def test_service_tier_parameter_fallback(self, bedrock_client):
        """Test service_tier parameter still works as fallback."""
        response = bedrock_client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0",
            system_prompt="You are a helpful assistant.",
            content=[{"text": "Say yes."}],
            service_tier="flex",
            max_tokens=5,
        )

        assert response is not None
        assert "output" in response

    def test_suffix_precedence_over_parameter(self, bedrock_client):
        """Test model ID suffix takes precedence over service_tier parameter."""
        response = bedrock_client.invoke_model(
            model_id="us.amazon.nova-2-lite-v1:0:priority",
            system_prompt="You are a helpful assistant.",
            content=[{"text": "Say no."}],
            service_tier="flex",
            max_tokens=5,
        )

        assert response is not None
        assert "output" in response

    def test_global_model_with_flex_suffix(self, bedrock_client):
        """Test global model ID with :flex suffix.

        The skip covers the *invocation* only. It used to wrap the assertions
        as well, and `except Exception` catches `AssertionError`, so a response
        that came back without an `output` key was reported as a skip -- which
        reads as green (#1129).
        """
        try:
            response = bedrock_client.invoke_model(
                model_id="global.amazon.nova-2-lite-v1:0:flex",
                system_prompt="You are a helpful assistant.",
                content=[{"text": "Say hi."}],
                max_tokens=5,
            )
        except Exception as e:
            pytest.skip(f"Global model not available: {e}")

        assert response is not None
        assert "output" in response
