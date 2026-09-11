# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for LambdaHook custom LLM inference feature.

Tests cover:
- BedrockClient routing to Lambda when model_id == "LambdaHook"
- ARN validation (GENAIIDP- prefix enforcement)
- Image-to-S3 conversion for payload size management
- Lambda invocation retry logic
- Response parsing and metering
- Region filtering preservation of LambdaHook
- Pydantic config model with model_lambda_hook_arn
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bedrock.client import (
    LAMBDA_HOOK_MODEL_ID,
    BedrockClient,
)


class TestLambdaHookModelId:
    """Test LAMBDA_HOOK_MODEL_ID constant."""

    def test_lambda_hook_model_id_value(self):
        assert LAMBDA_HOOK_MODEL_ID == "LambdaHook"


class TestLambdaHookClientConfig:
    """The invoke client must outwait a hook and never silently retry it.

    boto3's defaults (60s read timeout, botocore retries on) abandoned an
    in-flight hook after 60s and re-invoked it, re-running whatever the hook
    charges for while the first call was still going. Measured on the Cohere
    Parse hook (75s median per page), one 5-page document produced 44
    invocations and zero completed pages.
    """

    def test_read_timeout_outlasts_a_hook_but_not_the_caller(self):
        client = BedrockClient(region="us-east-1")
        with patch("boto3.client") as mock_boto:
            _ = client.lambda_client
        config = mock_boto.call_args.kwargs["config"]
        # Far above boto3's 60s default, so a slow hook is waited out...
        assert config.read_timeout >= 600
        # ...but strictly below the 900s cap of the calling Lambda (OCRFunction),
        # so the SDK raises inside the caller instead of the caller being killed
        # and Step Functions retrying the whole task, re-running paid work.
        assert config.read_timeout < 900

    def test_botocore_retries_are_disabled(self):
        """Retrying an invocation re-runs paid work, so the library owns it.

        `_invoke_lambda_hook_with_retry` backs off, logs, and knows which
        errors are worth another attempt; botocore retrying underneath it
        duplicates the spend invisibly.
        """
        client = BedrockClient(region="us-east-1")
        with patch("boto3.client") as mock_boto:
            _ = client.lambda_client
        config = mock_boto.call_args.kwargs["config"]
        assert config.retries["max_attempts"] == 1

    def test_connection_pool_fits_the_ocr_worker_fanout(self):
        """OCR worker threads share this client; the default pool of 10 serializes them."""
        client = BedrockClient(region="us-east-1")
        with patch("boto3.client") as mock_boto:
            _ = client.lambda_client
        config = mock_boto.call_args.kwargs["config"]
        assert config.max_pool_connections >= 20


class TestBedrockClientRouting:
    """Test that BedrockClient routes to Lambda when model_id is LambdaHook."""

    def test_invoke_model_routes_to_lambda_hook(self):
        """When model_id is LambdaHook, should call _invoke_lambda_hook."""
        client = BedrockClient(region="us-east-1")
        with patch.object(
            client, "_invoke_lambda_hook", return_value={"response": {}, "metering": {}}
        ) as mock_hook:
            client.invoke_model(
                model_id="LambdaHook",
                system_prompt="test",
                content=[{"text": "test"}],
                model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
            )
            mock_hook.assert_called_once()

    def test_invoke_model_routes_to_bedrock_for_normal_model(self):
        """When model_id is a normal model, should NOT call _invoke_lambda_hook."""
        client = BedrockClient(region="us-east-1")
        with patch.object(client, "_invoke_lambda_hook") as mock_hook:
            with patch.object(
                client,
                "_invoke_with_retry",
                return_value={"response": {}, "metering": {}},
            ):
                client.invoke_model(
                    model_id="us.amazon.nova-pro-v1:0",
                    system_prompt="test",
                    content=[{"text": "test"}],
                )
                mock_hook.assert_not_called()

    def test_callable_passes_lambda_hook_arn(self):
        """__call__ should pass model_lambda_hook_arn to invoke_model."""
        client = BedrockClient(region="us-east-1")
        with patch.object(
            client, "invoke_model", return_value={"response": {}, "metering": {}}
        ) as mock_invoke:
            client(
                model_id="LambdaHook",
                system_prompt="test",
                content=[{"text": "test"}],
                model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
            )
            call_kwargs = mock_invoke.call_args[1]
            assert (
                call_kwargs["model_lambda_hook_arn"]
                == "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"
            )


class TestLambdaHookArnValidation:
    """Test Lambda ARN validation in _invoke_lambda_hook."""

    def test_missing_arn_raises_value_error(self):
        """Should raise ValueError when model_lambda_hook_arn is None."""
        client = BedrockClient(region="us-east-1")
        with pytest.raises(ValueError, match="model_lambda_hook_arn is required"):
            client._invoke_lambda_hook(
                lambda_arn=None,
                system_prompt="test",
                content=[{"text": "test"}],
            )

    def test_empty_arn_raises_value_error(self):
        """Should raise ValueError when model_lambda_hook_arn is empty string."""
        client = BedrockClient(region="us-east-1")
        with pytest.raises(ValueError, match="model_lambda_hook_arn is required"):
            client._invoke_lambda_hook(
                lambda_arn="",
                system_prompt="test",
                content=[{"text": "test"}],
            )

    def test_invalid_function_name_raises_value_error(self):
        """Should raise ValueError when function name doesn't start with GENAIIDP-."""
        client = BedrockClient(region="us-east-1")
        with pytest.raises(ValueError, match="must start with 'GENAIIDP-'"):
            client._invoke_lambda_hook(
                lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:my-bad-function",
                system_prompt="test",
                content=[{"text": "test"}],
            )

    def test_valid_arn_accepted(self):
        """Should accept valid GENAIIDP- prefixed function ARN."""
        client = BedrockClient(region="us-east-1")
        # Mock the Lambda client by setting the private attribute directly
        mock_lambda = MagicMock()
        mock_response = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "output": {
                                "message": {
                                    "role": "assistant",
                                    "content": [{"text": "test"}],
                                }
                            },
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                                "totalTokens": 15,
                            },
                        }
                    ).encode("utf-8")
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response
        client._lambda_client = mock_lambda

        result = client._invoke_lambda_hook(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-my-extractor",
            system_prompt="test",
            content=[{"text": "test"}],
        )
        assert "response" in result
        assert "metering" in result

    def test_arn_with_alias_accepted(self):
        """Should accept ARN with alias/version suffix."""
        client = BedrockClient(region="us-east-1")
        mock_lambda = MagicMock()
        mock_response = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "output": {
                                "message": {
                                    "role": "assistant",
                                    "content": [{"text": "ok"}],
                                }
                            },
                        }
                    ).encode("utf-8")
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response
        client._lambda_client = mock_lambda

        # Should not raise - function:GENAIIDP-test:PROD is valid
        result = client._invoke_lambda_hook(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test:PROD",
            system_prompt="test",
            content=[{"text": "test"}],
        )
        assert result is not None


class TestImageToS3Conversion:
    """Test _convert_images_to_s3_refs method."""

    def test_text_only_content_unchanged(self):
        """Text-only content should pass through unchanged."""
        client = BedrockClient(region="us-east-1")
        content = [{"text": "hello"}, {"text": "world"}]
        result = client._convert_images_to_s3_refs(content)
        assert result == content

    def test_no_working_bucket_warns_and_returns_original(self):
        """Without WORKING_BUCKET env var, should warn and return original content."""
        client = BedrockClient(region="us-east-1")
        content = [
            {"text": "test"},
            {"image": {"format": "jpeg", "source": {"bytes": b"fake_image_data"}}},
        ]
        # Ensure WORKING_BUCKET is not set
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("WORKING_BUCKET", None)
            result = client._convert_images_to_s3_refs(content)
            # Should return original content unchanged
            assert len(result) == 2

    def test_s3_location_images_pass_through(self):
        """Images that already have s3Location should pass through unchanged."""
        client = BedrockClient(region="us-east-1")
        content = [
            {
                "image": {
                    "format": "jpeg",
                    "source": {"s3Location": {"uri": "s3://bucket/key"}},
                }
            },
        ]
        with patch.dict(os.environ, {"WORKING_BUCKET": "test-bucket"}):
            result = client._convert_images_to_s3_refs(content)
            assert result == content

    def test_inline_images_converted_to_s3_refs(self):
        """Inline image bytes should be uploaded to S3 and replaced with s3Location."""
        client = BedrockClient(region="us-east-1")
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}
        client._s3_client = mock_s3

        content = [
            {"text": "analyze this"},
            {"image": {"format": "jpeg", "source": {"bytes": b"fake_image_bytes"}}},
        ]

        with patch.dict(
            os.environ,
            {"WORKING_BUCKET": "test-bucket", "AWS_ACCOUNT_ID": "123456789012"},
        ):
            result = client._convert_images_to_s3_refs(content)

            # First item should be text (unchanged)
            assert result[0] == {"text": "analyze this"}

            # Second item should be converted to s3Location
            assert "image" in result[1]
            assert "s3Location" in result[1]["image"]["source"]
            assert result[1]["image"]["source"]["s3Location"]["uri"].startswith(
                "s3://test-bucket/temp/lambdahook/"
            )

            # S3 put_object should have been called
            mock_s3.put_object.assert_called_once()

    def test_mixed_content_handled_correctly(self):
        """Mix of text, inline images, and s3 images should be handled correctly."""
        client = BedrockClient(region="us-east-1")
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}
        client._s3_client = mock_s3

        content = [
            {"text": "text1"},
            {"image": {"format": "jpeg", "source": {"bytes": b"img1"}}},
            {"text": "text2"},
            {
                "image": {
                    "format": "png",
                    "source": {"s3Location": {"uri": "s3://existing/image.png"}},
                }
            },
            {"image": {"format": "jpeg", "source": {"bytes": b"img2"}}},
        ]

        with patch.dict(os.environ, {"WORKING_BUCKET": "test-bucket"}):
            result = client._convert_images_to_s3_refs(content)

            assert len(result) == 5
            assert result[0] == {"text": "text1"}
            assert "s3Location" in result[1]["image"]["source"]  # Converted
            assert result[2] == {"text": "text2"}
            assert (
                result[3]["image"]["source"]["s3Location"]["uri"]
                == "s3://existing/image.png"
            )  # Unchanged
            assert "s3Location" in result[4]["image"]["source"]  # Converted
            assert mock_s3.put_object.call_count == 2


class TestCachepointStripping:
    """Test that <<CACHEPOINT>> tags are stripped for Lambda hooks."""

    def test_cachepoint_tags_stripped(self):
        """<<CACHEPOINT>> tags should be removed from content for Lambda hooks."""
        client = BedrockClient(region="us-east-1")

        with patch.object(
            client,
            "_invoke_lambda_hook_with_retry",
            return_value={"response": {}, "metering": {}},
        ) as mock_retry:
            with patch.object(
                client, "_convert_images_to_s3_refs", side_effect=lambda x: x
            ):
                client._invoke_lambda_hook(
                    lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
                    system_prompt="test",
                    content=[{"text": "before <<CACHEPOINT>> after"}],
                )

                # Check the payload that was passed to _invoke_lambda_hook_with_retry
                call_args = mock_retry.call_args
                payload = call_args[1]["lambda_payload"]
                message_text = payload["messages"][0]["content"][0]["text"]
                assert "<<CACHEPOINT>>" not in message_text
                assert "before  after" in message_text


class TestLambdaResponseParsing:
    """Test Lambda response parsing and metering."""

    def test_response_with_usage_data(self):
        """Lambda response with usage data should be properly parsed."""
        client = BedrockClient(region="us-east-1")
        lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"

        response_payload = {
            "output": {
                "message": {"role": "assistant", "content": [{"text": "result"}]}
            },
            "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        }

        mock_lambda = MagicMock()
        mock_response = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(response_payload).encode("utf-8")
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response
        client._lambda_client = mock_lambda

        result = client._invoke_lambda_hook(
            lambda_arn=lambda_arn,
            system_prompt="test",
            content=[{"text": "test"}],
        )

        assert result["response"] == response_payload
        metering_key = f"Unspecified/lambda_hook/{lambda_arn}"
        assert metering_key in result["metering"]
        assert result["metering"][metering_key]["inputTokens"] == 100
        assert result["metering"][metering_key]["outputTokens"] == 50
        assert result["metering"][metering_key]["requests"] == 1

    def test_response_without_usage_defaults_to_zero(self):
        """Lambda response without usage data should default to zero tokens."""
        client = BedrockClient(region="us-east-1")
        lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"

        response_payload = {
            "output": {
                "message": {"role": "assistant", "content": [{"text": "result"}]}
            },
        }

        mock_lambda = MagicMock()
        mock_response = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(response_payload).encode("utf-8")
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response
        client._lambda_client = mock_lambda

        result = client._invoke_lambda_hook(
            lambda_arn=lambda_arn,
            system_prompt="test",
            content=[{"text": "test"}],
        )

        metering_key = f"Unspecified/lambda_hook/{lambda_arn}"
        assert result["metering"][metering_key]["inputTokens"] == 0
        assert result["metering"][metering_key]["outputTokens"] == 0


class TestRegionFiltering:
    """Test that LambdaHook is preserved during region filtering.

    Note: The actual filter_models_by_region and swap_model_ids functions live in
    src/lambda/update_configuration/index.py which can't be imported directly
    because 'lambda' is a Python keyword. We use importlib to load the module.
    """

    @staticmethod
    def _load_update_config_module():
        """Load the update_configuration module using importlib (avoids 'lambda' keyword issue)."""
        import importlib
        import sys
        from pathlib import Path

        # Add the src/lambda/update_configuration directory to sys.path
        module_dir = (
            Path(__file__).resolve().parents[4]
            / "src"
            / "lambda"
            / "update_configuration"
        )
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))

        # Import the module directly by file path
        spec = importlib.util.spec_from_file_location(
            "update_configuration_index", module_dir / "index.py"
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        raise ImportError("Could not load update_configuration/index.py")

    def test_filter_models_preserves_lambda_hook(self):
        """LambdaHook should survive region filtering for both US and EU."""
        try:
            mod = self._load_update_config_module()
            filter_models_by_region = mod.filter_models_by_region
        except (ImportError, ModuleNotFoundError) as e:
            pytest.skip(f"Cannot import update_configuration module: {e}")

        data = {
            "model": {
                "enum": [
                    "LambdaHook",
                    "us.amazon.nova-pro-v1:0",
                    "eu.amazon.nova-pro-v1:0",
                ]
            }
        }

        # Filter for US region
        us_result = filter_models_by_region(data, "us")
        assert "LambdaHook" in us_result["model"]["enum"]
        assert "us.amazon.nova-pro-v1:0" in us_result["model"]["enum"]
        assert "eu.amazon.nova-pro-v1:0" not in us_result["model"]["enum"]

        # Filter for EU region
        eu_result = filter_models_by_region(data, "eu")
        assert "LambdaHook" in eu_result["model"]["enum"]
        assert "eu.amazon.nova-pro-v1:0" in eu_result["model"]["enum"]
        assert "us.amazon.nova-pro-v1:0" not in eu_result["model"]["enum"]

    def test_swap_model_ids_preserves_lambda_hook(self):
        """LambdaHook should not be swapped during model ID swapping."""
        try:
            mod = self._load_update_config_module()
            swap_model_ids = mod.swap_model_ids
        except (ImportError, ModuleNotFoundError) as e:
            pytest.skip(f"Cannot import update_configuration module: {e}")

        data = {"classification": {"model": "LambdaHook"}}

        # Should not swap LambdaHook
        result = swap_model_ids(data, "eu")
        assert result["classification"]["model"] == "LambdaHook"


class TestPydanticConfigModels:
    """Test Pydantic config models with model_lambda_hook_arn."""

    def test_extraction_config_with_lambda_hook(self):
        from idp_common.config.models import ExtractionConfig

        config = ExtractionConfig(
            model="LambdaHook",
            model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
        )
        assert config.model == "LambdaHook"
        assert (
            config.model_lambda_hook_arn
            == "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"
        )

    def test_extraction_config_without_lambda_hook(self):
        from idp_common.config.models import ExtractionConfig

        config = ExtractionConfig(model="us.amazon.nova-pro-v1:0")
        assert config.model == "us.amazon.nova-pro-v1:0"
        assert config.model_lambda_hook_arn is None

    def test_classification_config_with_lambda_hook(self):
        from idp_common.config.models import ClassificationConfig

        config = ClassificationConfig(
            model="LambdaHook",
            model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-classifier",
        )
        assert config.model == "LambdaHook"
        assert config.model_lambda_hook_arn is not None

    def test_confidence_config_with_lambda_hook(self):
        # v0.6: confidence assessment config is extraction.confidence
        from idp_common.config.models import ConfidenceConfig

        config = ConfidenceConfig(
            model="LambdaHook",
            model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-assessor",
        )
        assert config.model == "LambdaHook"
        assert config.model_lambda_hook_arn is not None

    def test_summarization_config_with_lambda_hook(self):
        from idp_common.config.models import SummarizationConfig

        config = SummarizationConfig(
            model="LambdaHook",
            model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-summarizer",
        )
        assert config.model == "LambdaHook"

    def test_ocr_config_with_lambda_hook(self):
        from idp_common.config.models import OCRConfig

        config = OCRConfig(
            backend="bedrock",
            model_id="LambdaHook",
            model_lambda_hook_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-ocr",
        )
        assert config.model_id == "LambdaHook"
        assert config.model_lambda_hook_arn is not None

    def test_idp_config_roundtrip_with_lambda_hook(self):
        """Test that IDPConfig can serialize/deserialize with LambdaHook config."""
        from idp_common.config.models import IDPConfig

        config = IDPConfig(
            extraction={
                "model": "LambdaHook",
                "model_lambda_hook_arn": "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
            },
        )
        config_dict = config.to_dict()
        assert config_dict["extraction"]["model"] == "LambdaHook"
        assert (
            config_dict["extraction"]["model_lambda_hook_arn"]
            == "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"
        )

        # Roundtrip
        restored = IDPConfig(**config_dict)
        assert restored.extraction.model == "LambdaHook"
        assert (
            restored.extraction.model_lambda_hook_arn
            == "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test"
        )


class TestLambdaHookFunctionError:
    """Test error handling for Lambda function errors."""

    def test_function_error_raises_runtime_error(self):
        """Lambda function errors should raise RuntimeError."""
        client = BedrockClient(region="us-east-1")

        mock_lambda = MagicMock()
        mock_response = {
            "StatusCode": 200,
            "FunctionError": "Handled",
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "errorMessage": "Something went wrong",
                            "errorType": "ValueError",
                        }
                    ).encode("utf-8")
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response
        client._lambda_client = mock_lambda

        with pytest.raises(RuntimeError, match="LambdaHook function error"):
            client._invoke_lambda_hook(
                lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-test",
                system_prompt="test",
                content=[{"text": "test"}],
            )
