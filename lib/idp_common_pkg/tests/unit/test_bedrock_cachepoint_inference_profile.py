# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for BedrockClient cachepoint support with inference profiles."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common.bedrock.client import (
    _CACHEPOINT_BASE_MODELS,
    CACHEPOINT_SUPPORTED_MODELS,
    BedrockClient,
    _inference_profile_cachepoint_cache,
)
from idp_common.bedrock.model_utils import REGION_PREFIXES


@pytest.mark.unit
class TestCachepointBaseModelsSet:
    """Test that _CACHEPOINT_BASE_MODELS is built correctly from CACHEPOINT_SUPPORTED_MODELS."""

    def test_base_models_set_not_empty(self):
        """Base models set should contain entries derived from the supported models list."""
        assert len(_CACHEPOINT_BASE_MODELS) > 0

    def test_base_models_strip_region_prefix(self):
        """Base models should have every geo prefix stripped, not just us./eu./global."""
        for base_model in _CACHEPOINT_BASE_MODELS:
            for prefix in REGION_PREFIXES:
                assert not base_model.startswith(f"{prefix}."), (
                    f"{base_model!r} still carries the {prefix!r} prefix"
                )

    def test_base_models_contain_known_models(self):
        """Known foundation model names should be in the base models set."""
        assert "anthropic.claude-sonnet-4-6" in _CACHEPOINT_BASE_MODELS
        assert "amazon.nova-pro-v1:0" in _CACHEPOINT_BASE_MODELS
        assert "amazon.nova-lite-v1:0" in _CACHEPOINT_BASE_MODELS
        assert "amazon.nova-2-lite-v1:0" in _CACHEPOINT_BASE_MODELS

    def test_base_models_strip_tier_suffixes(self):
        """Tier suffixes (:priority, :flex) should be stripped from base models."""
        for base_model in _CACHEPOINT_BASE_MODELS:
            assert not base_model.endswith(":priority")
            assert not base_model.endswith(":flex")

    def test_base_models_preserve_version_suffixes(self):
        """Version suffixes (:0, :1m) should be preserved in base models."""
        assert "amazon.nova-pro-v1:0" in _CACHEPOINT_BASE_MODELS
        assert "anthropic.claude-sonnet-4-6:1m" in _CACHEPOINT_BASE_MODELS

    def test_no_geo_prefix_is_silently_ignored(self):
        """Every geo-prefixed entry must contribute its base name to the set.

        The derivation loop tested a local ``("us", "eu", "global")`` tuple, so an
        entry carrying any other geo — ``ca.``, ``apac.``, ``au.``, ``jp.``,
        ``us-gov.`` — added nothing here. ``_is_model_cachepoint_supported``
        resolves an inference-profile ARN to its foundation model and looks the
        result up in this set, so such a model reads as cachePoint-unsupported
        when it is named by ARN and quietly loses prompt caching.

        This passes under both the old tuple and the shared ``REGION_PREFIXES``
        today, because the one geo-only entry (``ca.amazon.nova-lite-v1:0``) has a
        ``us.`` twin seeding the same base name. It is the first entry with NO
        twin — a model offered only under ``ca.``, ``jp.`` or ``us-gov.`` — that
        this exists to catch, and the loop now reads the shared list so that entry
        needs no edit in ``client.py``.
        """
        for model_id in CACHEPOINT_SUPPORTED_MODELS:
            prefix, _, rest = model_id.partition(".")
            if not rest or prefix not in REGION_PREFIXES:
                continue
            base = rest
            if base.endswith((":priority", ":flex")):
                base = base.rsplit(":", 1)[0]
            assert base in _CACHEPOINT_BASE_MODELS, (
                f"{model_id} carries the geo prefix {prefix!r}, but its base name "
                f"{base!r} is absent from _CACHEPOINT_BASE_MODELS, so an "
                "inference-profile ARN resolving to it reads as "
                "cachePoint-unsupported"
            )


@pytest.mark.unit
class TestIsModelCachepointSupported:
    """Test _is_model_cachepoint_supported method with inference profile resolution."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear the inference profile cache before each test."""
        _inference_profile_cachepoint_cache.clear()
        yield
        _inference_profile_cachepoint_cache.clear()

    @pytest.fixture
    def bedrock_client(self):
        """Create BedrockClient with mocked clients."""
        client = BedrockClient(region="us-east-1", metrics_enabled=False)
        client._client = MagicMock()
        client._bedrock_control_client = MagicMock()
        return client

    def test_standard_model_direct_match(self, bedrock_client):
        """Standard model IDs in CACHEPOINT_SUPPORTED_MODELS should return True."""
        assert (
            bedrock_client._is_model_cachepoint_supported(
                "us.anthropic.claude-sonnet-4-6"
            )
            is True
        )

    def test_unsupported_standard_model(self, bedrock_client):
        """Model IDs not in the list and not inference profiles should return False."""
        assert (
            bedrock_client._is_model_cachepoint_supported("some.unknown.model-v1")
            is False
        )

    def test_non_inference_profile_arn(self, bedrock_client):
        """ARNs that don't contain 'inference-profile' should return False without API call."""
        result = bedrock_client._is_model_cachepoint_supported(
            "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
        )
        assert result is False
        bedrock_client._bedrock_control_client.get_inference_profile.assert_not_called()

    def test_application_inference_profile_supported_model(self, bedrock_client):
        """Application inference profile wrapping a supported model should return True."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                },
                {
                    "modelArn": "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6"
                },
            ],
            "inferenceProfileId": "app-profile-abc123",
            "status": "ACTIVE",
        }

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is True
        bedrock_client._bedrock_control_client.get_inference_profile.assert_called_once_with(
            inferenceProfileIdentifier=profile_arn
        )

    def test_application_inference_profile_unsupported_model(self, bedrock_client):
        """Application inference profile wrapping an unsupported model should return False."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/xyz789"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/some.unsupported-model-v1"
                },
            ],
            "inferenceProfileId": "app-profile-xyz789",
            "status": "ACTIVE",
        }

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False

    def test_system_inference_profile_supported(self, bedrock_client):
        """System-defined inference profiles should also be resolved."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-6"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                },
            ],
            "inferenceProfileId": "us.anthropic.claude-sonnet-4-6",
            "status": "ACTIVE",
        }

        # Note: system inference profiles like "us.anthropic.claude-sonnet-4-6" are already
        # in CACHEPOINT_SUPPORTED_MODELS and would be caught by the fast path.
        # This test covers the case where the full ARN is used instead of the short ID.
        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is True

    def test_result_is_cached(self, bedrock_client):
        """Resolved results should be cached to avoid repeated API calls."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/cached123"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                },
            ],
        }

        # First call - makes API call
        result1 = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result1 is True
        assert (
            bedrock_client._bedrock_control_client.get_inference_profile.call_count == 1
        )

        # Second call - uses cache, no additional API call
        result2 = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result2 is True
        assert (
            bedrock_client._bedrock_control_client.get_inference_profile.call_count == 1
        )

    def test_empty_models_list(self, bedrock_client):
        """Profile with empty models list should return False."""
        profile_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/empty"
        )
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [],
        }

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False

    def test_unparseable_model_arn(self, bedrock_client):
        """Profile with model ARN lacking 'foundation-model/' should return False."""
        profile_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/weird"
        )
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::custom-model/my-fine-tuned-model"
                },
            ],
        }

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False

    def test_client_error_returns_false(self, bedrock_client):
        """API errors should return False and cache the result."""
        profile_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/error"
        )
        bedrock_client._bedrock_control_client.get_inference_profile.side_effect = (
            ClientError(
                {
                    "Error": {
                        "Code": "ResourceNotFoundException",
                        "Message": "Profile not found",
                    }
                },
                "GetInferenceProfile",
            )
        )

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False
        # Should be cached so second call doesn't hit API
        assert profile_arn in _inference_profile_cachepoint_cache
        assert _inference_profile_cachepoint_cache[profile_arn] is False

    def test_access_denied_returns_false(self, bedrock_client):
        """AccessDeniedException should return False gracefully (missing GetInferenceProfile permission)."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/noperm"
        bedrock_client._bedrock_control_client.get_inference_profile.side_effect = (
            ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "Not authorized",
                    }
                },
                "GetInferenceProfile",
            )
        )

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False

    def test_unexpected_exception_returns_false(self, bedrock_client):
        """Unexpected exceptions should return False gracefully."""
        profile_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/crash"
        )
        bedrock_client._bedrock_control_client.get_inference_profile.side_effect = (
            RuntimeError("boom")
        )

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is False

    def test_nova_model_via_inference_profile(self, bedrock_client):
        """Application inference profile wrapping Nova model should return True."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/nova123"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
                },
            ],
        }

        result = bedrock_client._is_model_cachepoint_supported(profile_arn)
        assert result is True


@pytest.mark.unit
class TestCachepointProcessingWithInferenceProfiles:
    """Test that invoke_model correctly applies or strips cachepoint tags for inference profiles."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear the inference profile cache before each test."""
        _inference_profile_cachepoint_cache.clear()
        yield
        _inference_profile_cachepoint_cache.clear()

    @pytest.fixture
    def mock_bedrock_response(self):
        """Mock Bedrock API response."""
        return {
            "output": {"message": {"content": [{"text": "test response"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        }

    @pytest.fixture
    def bedrock_client(self):
        """Create BedrockClient with mocked clients."""
        client = BedrockClient(region="us-east-1", metrics_enabled=False)
        client._client = MagicMock()
        client._bedrock_control_client = MagicMock()
        return client

    def test_cachepoint_applied_for_supported_inference_profile(
        self, bedrock_client, mock_bedrock_response
    ):
        """Cachepoint tags should be processed (not stripped) for supported inference profiles."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/supported"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                },
            ],
        }
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id=profile_arn,
            system_prompt="test",
            content=[{"text": "static content<<CACHEPOINT>>dynamic content"}],
        )

        # Verify cachePoint elements were inserted (not stripped)
        call_args = bedrock_client._client.converse.call_args
        message_content = call_args.kwargs["messages"][0]["content"]
        has_cachepoint = any("cachePoint" in item for item in message_content)
        assert has_cachepoint, (
            "cachePoint should be inserted for supported inference profile"
        )

    def test_cachepoint_stripped_for_unsupported_inference_profile(
        self, bedrock_client, mock_bedrock_response
    ):
        """Cachepoint tags should be stripped for unsupported inference profiles."""
        profile_arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/unsupported"
        bedrock_client._bedrock_control_client.get_inference_profile.return_value = {
            "models": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/some.unsupported-model"
                },
            ],
        }
        bedrock_client._client.converse.return_value = mock_bedrock_response

        bedrock_client.invoke_model(
            model_id=profile_arn,
            system_prompt="test",
            content=[{"text": "static<<CACHEPOINT>>dynamic"}],
        )

        # Verify cachePoint elements were NOT inserted (tags stripped)
        call_args = bedrock_client._client.converse.call_args
        message_content = call_args.kwargs["messages"][0]["content"]
        has_cachepoint = any("cachePoint" in item for item in message_content)
        assert not has_cachepoint, (
            "cachePoint should NOT be inserted for unsupported inference profile"
        )
        # But the text content should still be there (just without the tags)
        full_text = "".join(item.get("text", "") for item in message_content)
        assert "static" in full_text
        assert "dynamic" in full_text
        assert "<<CACHEPOINT>>" not in full_text
