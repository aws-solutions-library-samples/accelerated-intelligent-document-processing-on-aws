# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the ClassificationService class.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Import standard library modules first
import json
from textwrap import dedent
from unittest.mock import ANY, MagicMock, patch

# PIL is now used directly - no mocking needed

from botocore.exceptions import ClientError
from idp_common.classification.models import (
    DocumentClassification,
    PageClassification,
)
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page, Status


@pytest.mark.unit
class TestClassificationService:
    """Tests for the ClassificationService class."""

    @pytest.fixture
    def mock_config(self):
        """Fixture providing a mock configuration in JSON Schema format."""
        return {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "invoice",
                    "x-aws-idp-document-type": "invoice",
                    "type": "object",
                    "description": "An invoice document",
                    "properties": {},
                },
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "receipt",
                    "x-aws-idp-document-type": "receipt",
                    "type": "object",
                    "description": "A receipt document",
                    "properties": {},
                },
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "letter",
                    "x-aws-idp-document-type": "letter",
                    "type": "object",
                    "description": "A letter document",
                    "properties": {},
                },
            ],
            "classification": {
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a document classification assistant.",
                "task_prompt": dedent("""
                    Classify the following document text into one of the available classes:
                    {CLASS_NAMES_AND_DESCRIPTIONS}
                    
                    Document text:
                    {DOCUMENT_TEXT}
                    
                    Document image:
                    {DOCUMENT_IMAGE}
                    
                    Respond with a JSON object with a single field "class" containing the document type.
                """),
                "classificationMethod": "multimodalPageLevelClassification",
            },
        }

    @pytest.fixture
    def single_class_config(self):
        """Fixture providing a mock configuration with only one class defined."""
        return {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "invoice",
                    "x-aws-idp-document-type": "invoice",
                    "type": "object",
                    "description": "An invoice document",
                    "properties": {},
                },
            ],
            "classification": {
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "system_prompt": "You are a document classification assistant.",
                "task_prompt": dedent("""
                    Classify the following document text into one of the available classes:
                    {CLASS_NAMES_AND_DESCRIPTIONS}
                    
                    Document text:
                    {DOCUMENT_TEXT}
                    
                    Document image:
                    {DOCUMENT_IMAGE}
                    
                    Respond with a JSON object with a single field "class" containing the document type.
                """),
                "classificationMethod": "multimodalPageLevelClassification",
            },
        }

    @pytest.fixture
    def service(self, mock_config):
        """Fixture providing a ClassificationService instance."""
        with patch("boto3.Session"):
            return ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

    def test_init_with_bedrock_backend(self, mock_config):
        """Test initialization with Bedrock backend."""
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            assert service.backend == "bedrock"
            assert service.bedrock_model == "anthropic.claude-3-sonnet-20240229-v1:0"
            assert service.classification_method == "multimodalPageLevelClassification"
            assert len(service.document_types) == 3
            assert service.valid_doc_types == {"invoice", "receipt", "letter"}

    def test_init_with_sagemaker_backend(self, mock_config):
        """Test initialization with SageMaker backend."""
        with (
            patch("boto3.Session"),
            patch("boto3.client") as mock_client,
            patch.dict("os.environ", {"SAGEMAKER_ENDPOINT_NAME": "test-endpoint"}),
        ):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="sagemaker"
            )

            assert service.backend == "sagemaker"
            assert service.sagemaker_endpoint == "test-endpoint"
            mock_client.assert_called_once_with(
                "sagemaker-runtime", region_name="us-west-2"
            )

    def test_init_with_invalid_backend(self, mock_config):
        """Test initialization with invalid backend falls back to Bedrock."""
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="invalid"
            )

            assert service.backend == "bedrock"

    def test_load_document_types(self, service):
        """Test loading document types from configuration."""
        doc_types = service._load_document_types()

        assert len(doc_types) == 3
        assert doc_types[0].type_name == "invoice"
        assert doc_types[0].description == "An invoice document"
        assert doc_types[1].type_name == "receipt"
        assert doc_types[2].type_name == "letter"

    def test_load_document_types_empty_config(self):
        """Test loading document types with empty configuration."""
        with (
            patch("boto3.Session"),
            patch(
                "idp_common.classification.service.ClassificationService.__init__",
                return_value=None,
            ),
        ):
            # Create service instance without calling __init__
            service = ClassificationService()

            # Set minimal config using IDPConfig model
            from idp_common.config.models import IDPConfig

            service.config = IDPConfig()

            # Call the method directly
            doc_types = service._load_document_types()

            assert len(doc_types) == 1
            assert doc_types[0].type_name == "unclassified"

    def test_format_classes_list(self, service):
        """Test formatting document classes as a list."""
        formatted = service._format_classes_list()

        assert "invoice" in formatted
        assert "receipt" in formatted
        assert "letter" in formatted
        assert "An invoice document" in formatted
        assert "A receipt document" in formatted
        assert "A letter document" in formatted

    def test_get_classification_config(self, service):
        """Test getting and validating classification configuration."""
        config = service._get_classification_config()

        assert config["model_id"] == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert config["temperature"] == 0.0
        assert config["top_k"] == 5.0
        assert config["system_prompt"] == "You are a document classification assistant."
        assert "Classify the following document text" in config["task_prompt"]

    def test_get_classification_config_missing_prompts(self, mock_config):
        """Test getting classification config with missing prompts."""
        # Remove required prompts
        mock_config["classification"].pop("system_prompt")

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            with pytest.raises(ValueError, match="No system_prompt found"):
                service._get_classification_config()

    def test_prepare_prompt_from_template(self, service):
        """Test preparing prompt from template."""
        template = "Classify this document: {DOCUMENT_TEXT}\nClasses: {CLASS_LIST}"
        substitutions = {
            "DOCUMENT_TEXT": "This is an invoice for $100",
            "CLASS_LIST": "invoice, receipt, letter",
        }

        with patch("idp_common.bedrock.format_prompt", return_value="Formatted prompt"):
            result = service._prepare_prompt_from_template(
                template,
                substitutions,
                required_placeholders=["DOCUMENT_TEXT", "CLASS_LIST"],
            )

            assert result == "Formatted prompt"

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_classify_page_bedrock_success(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """Test successful page classification with Bedrock."""
        # Mock responses
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = {
            "response": {
                "output": {"message": {"content": [{"text": '{"class": "invoice"}'}]}}
            },
            "metering": {"tokens": 100},
        }

        # Call the method
        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        # Verify results
        assert result.page_id == "1"
        assert result.classification.doc_type == "invoice"
        # The model returned no confidence, so the page is NOT SCORED rather
        # than credited with a fabricated 1.0 (GitHub #673).
        assert result.classification.confidence is None
        assert result.classification.metadata["metering"] == {"tokens": 100}
        assert result.classification.metadata["document_boundary"] == "continue"
        assert result.image_uri == "s3://bucket/image.jpg"
        assert result.text_uri == "s3://bucket/text.txt"

        # Verify calls
        mock_get_text.assert_called_once_with("s3://bucket/text.txt")
        mock_prepare_image.assert_called_once_with("s3://bucket/image.jpg", None, None)
        mock_prepare_bedrock_image.assert_called_once_with(b"image_data")
        mock_invoke.assert_called_once()

    @staticmethod
    def _bedrock_response(class_value, **extra):
        """Helper to build a mocked _invoke_bedrock_model return value.

        ``extra`` goes into the JSON body alongside ``class``, for the optional
        keys the prompt may ask for (``confidence``, ``classification_reason``,
        ``document_boundary``).
        """
        body = {"class": class_value, **extra}
        return {
            "response": {
                "output": {"message": {"content": [{"text": json.dumps(body)}]}}
            },
            "metering": {"bedrock": {"inputTokens": 100, "outputTokens": 10}},
        }

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_model_reported_confidence_and_reason_are_kept(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """A confidence and reason the model returns must survive (GitHub #673).

        Both were parsed by nobody before this: `confidence` was documented and
        discarded, and `classification_reason` is asked for by the DEFAULT prompt,
        so its output tokens were bought on every page and dropped.
        """
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = self._bedrock_response(
            "invoice",
            confidence=0.72,
            classification_reason="Invoice number and remittance block present",
        )

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.confidence == 0.72
        assert result.classification.metadata["classification_reason"] == (
            "Invoice number and remittance block present"
        )

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_percentage_confidence_is_normalized(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """Models asked for a probability often answer 95 rather than 0.95."""
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = self._bedrock_response("invoice", confidence=95)

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.confidence == 0.95

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_fallback_class_discards_the_rejected_prediction_confidence(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """The fallback class is ours, so the model's score does not describe it."""
        mock_get_text.return_value = "Some ambiguous content"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = self._bedrock_response(
            "bogus_class", confidence=0.99, classification_reason="looks bogus"
        )

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.doc_type == "unclassified"
        assert result.classification.confidence is None
        assert "classification_reason" not in result.classification.metadata

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_retry_does_not_carry_the_rejected_attempt_confidence(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """A retry's accepted class must not inherit the rejected one's score."""
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.side_effect = [
            self._bedrock_response("not_a_real_class", confidence=0.99),
            self._bedrock_response("invoice"),  # valid, but reports no confidence
        ]

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.doc_type == "invoice"
        assert result.classification.confidence is None

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_enforce_valid_classes_retry_then_valid(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """Out-of-vocabulary prediction is corrected on retry."""
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}

        # First call returns an invalid class, second returns a valid one.
        mock_invoke.side_effect = [
            self._bedrock_response("not_a_real_class"),
            self._bedrock_response("invoice"),
        ]

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.doc_type == "invoice"
        assert "validation_error" not in result.classification.metadata
        assert mock_invoke.call_count == 2
        # Metering aggregated across both attempts.
        assert (
            result.classification.metadata["metering"]["bedrock"]["inputTokens"] == 200
        )

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_enforce_valid_classes_exhausted_uses_fallback(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """When all retries fail, the fallback class and error metadata are set."""
        mock_get_text.return_value = "Some ambiguous content"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}

        # Always returns an invalid class. Default maxValidationRetries=2 -> 3 calls.
        mock_invoke.return_value = self._bedrock_response("bogus_class")

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.doc_type == "unclassified"
        assert "validation_error" in result.classification.metadata
        assert "bogus_class" in result.classification.metadata["validation_error"]
        assert mock_invoke.call_count == 3  # initial + 2 retries

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_enforce_valid_classes_valid_first_attempt_no_retry(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        service,
    ):
        """A valid first prediction triggers no extra invocations."""
        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = self._bedrock_response("invoice")

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        assert result.classification.doc_type == "invoice"
        mock_invoke.assert_called_once()

    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_enforcement_disabled_uses_invalid_class_as_is(
        self,
        mock_prepare_bedrock_image,
        mock_invoke,
        mock_prepare_image,
        mock_get_text,
        mock_config,
    ):
        """Legacy behavior: with enforcement off, invalid class is used as-is."""
        mock_config["classification"]["enforceValidClasses"] = False
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

        mock_get_text.return_value = "This is an invoice for $100"
        mock_prepare_image.return_value = b"image_data"
        mock_prepare_bedrock_image.return_value = {"image": "base64_encoded_image"}
        mock_invoke.return_value = self._bedrock_response("not_a_real_class")

        result = service.classify_page_bedrock(
            page_id="1",
            text_uri="s3://bucket/text.txt",
            image_uri="s3://bucket/image.jpg",
        )

        # Invalid class is used as-is, no retry, no validation_error.
        assert result.classification.doc_type == "not_a_real_class"
        assert "validation_error" not in result.classification.metadata
        mock_invoke.assert_called_once()

    @patch("idp_common.s3.get_text_content")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    def test_classify_page_bedrock_no_content(
        self, mock_invoke, mock_get_text, service
    ):
        """Test page classification with no content."""
        # Mock responses
        mock_get_text.side_effect = Exception("Failed to load text")

        # Call the method
        result = service.classify_page_bedrock(
            page_id="1", text_uri="s3://bucket/text.txt"
        )

        # Verify results
        assert result.page_id == "1"
        assert result.classification.doc_type == "unclassified"
        assert result.classification.confidence == 0.0
        assert (
            result.classification.metadata["error"]
            == "No content available for classification"
        )

        # Verify no model invocation
        mock_invoke.assert_not_called()

    @pytest.mark.skip(reason="Temporarily disabled due to exception handling issues")
    @patch("idp_common.s3.get_text_content")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    def test_classify_page_bedrock_model_error(
        self, mock_invoke, mock_get_text, service
    ):
        """Test page classification with model error."""
        # Mock responses
        mock_get_text.return_value = "This is an invoice for $100"
        mock_invoke.side_effect = Exception("Model error")

        # Call the method and expect exception to be raised
        with pytest.raises(Exception, match="Model error"):
            service.classify_page_bedrock(page_id="1", text_uri="s3://bucket/text.txt")

    @patch("boto3.client")
    def test_classify_page_sagemaker_success(self, mock_boto_client, mock_config):
        """Test successful page classification with SageMaker."""
        # Setup mock SageMaker client
        mock_sm_client = MagicMock()
        mock_boto_client.return_value = mock_sm_client

        # Mock SageMaker response
        mock_response = {"Body": MagicMock()}
        mock_response["Body"].read.return_value = json.dumps(
            {"prediction": "invoice"}
        ).encode()
        mock_sm_client.invoke_endpoint.return_value = mock_response

        # Create service with SageMaker backend
        with patch.dict("os.environ", {"SAGEMAKER_ENDPOINT_NAME": "test-endpoint"}):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="sagemaker"
            )

            # Call the method
            result = service.classify_page_sagemaker(
                page_id="1",
                image_uri="s3://bucket/image.jpg",
                raw_text_uri="s3://bucket/raw.json",
            )

            # Verify results
            assert result.page_id == "1"
            assert result.classification.doc_type == "invoice"
            # The UDOP endpoint returns a prediction with no score.
            assert result.classification.confidence is None
            assert (
                "Classification/sagemaker/invoke_endpoint"
                in result.classification.metadata["metering"]
            )

            # Verify SageMaker call
            mock_sm_client.invoke_endpoint.assert_called_once_with(
                EndpointName="test-endpoint", ContentType="application/json", Body=ANY
            )

    @patch("boto3.client")
    def test_classify_page_sagemaker_missing_uris(self, mock_boto_client, mock_config):
        """Test SageMaker classification with missing URIs."""
        # Create service with SageMaker backend
        with patch.dict("os.environ", {"SAGEMAKER_ENDPOINT_NAME": "test-endpoint"}):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="sagemaker"
            )

            # Call the method without required URIs
            result = service.classify_page_sagemaker(
                page_id="1",
                image_uri=None,  # Missing required URI
                raw_text_uri=None,  # Missing required URI
            )

            # Verify results
            assert result.page_id == "1"
            assert result.classification.doc_type == "unclassified"
            assert result.classification.confidence == 0.0
            assert (
                "Missing required image_uri or raw_text_uri"
                in result.classification.metadata["error"]
            )

    @patch("boto3.client")
    def test_classify_page_sagemaker_throttling_retry(
        self, mock_boto_client, mock_config
    ):
        """Test SageMaker classification with throttling and retry."""
        # Import the ClientError directly in the test

        # Setup mock SageMaker client
        mock_sm_client = MagicMock()
        mock_boto_client.return_value = mock_sm_client

        # Create a real ClientError with throttling info
        throttling_error_response = {
            "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"},
            "ResponseMetadata": {
                "RequestId": "1234567890ABCDEF",
                "HTTPStatusCode": 429,
            },
        }
        throttling_error = ClientError(throttling_error_response, "InvokeEndpoint")

        # Create a mock response body for the successful call
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"prediction": "invoice"}).encode()

        # Set up side effect sequence - first a throttling error, then success
        mock_sm_client.invoke_endpoint.side_effect = [
            throttling_error,  # First call raises ClientError
            {"Body": mock_body},  # Second call succeeds
        ]

        # Create service with SageMaker backend and patch sleep
        with (
            patch.dict("os.environ", {"SAGEMAKER_ENDPOINT_NAME": "test-endpoint"}),
            patch("time.sleep"),
        ):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="sagemaker"
            )

            # Call the method
            result = service.classify_page_sagemaker(
                page_id="1",
                image_uri="s3://bucket/image.jpg",
                raw_text_uri="s3://bucket/raw.json",
            )

            # Verify results
            assert result.page_id == "1"
            assert result.classification.doc_type == "invoice"

            # Verify SageMaker was called twice (error + retry)
            assert mock_sm_client.invoke_endpoint.call_count == 2

    def test_extract_json(self, service):
        """Test extracting JSON from text."""
        from idp_common.utils import extract_json_from_text

        # Test with code block format
        text = 'Here is the result:\n```json\n{"class": "invoice"}\n```\nEnd of result.'
        result = extract_json_from_text(text)
        assert result == '{"class": "invoice"}'

        # Test with simple JSON
        text = 'The classification is {"class": "receipt"} based on the content.'
        result = extract_json_from_text(text)
        assert result == '{"class": "receipt"}'

        # Test with nested JSON
        text = 'Result: {"class": "letter", "metadata": {"confidence": 0.9}}'
        result = extract_json_from_text(text)
        assert result == '{"class": "letter", "metadata": {"confidence": 0.9}}'

        # Test with no JSON
        text = "No JSON here"
        result = extract_json_from_text(text)
        assert result == "No JSON here"

    def test_extract_class_from_text(self, service):
        """Test extracting class from text when JSON parsing fails."""
        # Test various patterns
        assert service._extract_class_from_text("The class: invoice") == "invoice"
        assert service._extract_class_from_text("document type: receipt") == "receipt"
        assert (
            service._extract_class_from_text("Document class: letter\nwith details")
            == "letter"
        )
        assert (
            service._extract_class_from_text('Classification: "invoice"') == "invoice"
        )
        assert service._extract_class_from_text("Type: 'receipt'") == "receipt"

        # Test with no match
        assert service._extract_class_from_text("No class information") == ""

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    @patch("idp_common.utils.merge_metering_data")
    def test_classify_document_page_by_page(
        self, mock_merge_metering, mock_classify_page, service
    ):
        """Test document classification using page-by-page method."""
        # Create a test document
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )

        # Add pages
        doc.pages["1"] = Page(page_id="1", image_uri="s3://bucket/image1.jpg")
        doc.pages["2"] = Page(page_id="2", image_uri="s3://bucket/image2.jpg")

        # Mock page classification results
        mock_classify_page.side_effect = [
            PageClassification(
                page_id="1",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"metering": {"tokens": 100}}
                ),
            ),
            PageClassification(
                page_id="2",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"metering": {"tokens": 120}}
                ),
            ),
        ]

        # Mock metering merge
        mock_merge_metering.return_value = {"tokens": 220}

        # Call the method
        result = service.classify_document(doc)

        # Verify results
        assert result.status != Status.FAILED
        assert len(result.sections) == 1
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1", "2"]
        assert result.pages["1"].classification == "invoice"
        assert result.pages["2"].classification == "invoice"

        # Verify metering was merged
        assert mock_merge_metering.called

        # Set the metering directly since we mocked the merge function
        result.metering = {"tokens": 220}
        assert result.metering == {"tokens": 220}

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_classify_document_with_different_page_types(
        self, mock_classify_page, service
    ):
        """Test document classification with different page types."""
        # Create a test document
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )

        # Add pages
        doc.pages["1"] = Page(page_id="1", image_uri="s3://bucket/image1.jpg")
        doc.pages["2"] = Page(page_id="2", image_uri="s3://bucket/image2.jpg")
        doc.pages["3"] = Page(page_id="3", image_uri="s3://bucket/image3.jpg")

        # Mock page classification results
        mock_classify_page.side_effect = [
            PageClassification(
                page_id="1", classification=DocumentClassification(doc_type="invoice")
            ),
            PageClassification(
                page_id="2", classification=DocumentClassification(doc_type="receipt")
            ),
            PageClassification(
                page_id="3", classification=DocumentClassification(doc_type="invoice")
            ),
        ]

        # Call the method
        result = service.classify_document(doc)

        # Verify results
        assert len(result.sections) == 3  # Should have 3 sections due to type changes
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1"]
        assert result.sections[1].classification == "receipt"
        assert result.sections[1].page_ids == ["2"]
        assert result.sections[2].classification == "invoice"
        assert result.sections[2].page_ids == ["3"]

    def test_classify_document_single_class_optimization(self, single_class_config):
        """The single-class backend skip applies when no boundary detection is needed.

        This test used to assert the skip unconditionally, which encoded the #686
        bug: a multi-page single-class document under the default
        `sectionSplitting: llm_determined` was given one all-pages section
        WITHOUT ever asking the model where the boundaries were. The skip is now
        scoped to the cases where the model genuinely has nothing to contribute —
        here, `sectionSplitting: disabled`.
        """
        config = json.loads(json.dumps(single_class_config))
        config["classification"]["sectionSplitting"] = "disabled"
        with (
            patch("boto3.Session"),
            patch("logging.Logger.info") as mock_log_info,
        ):
            service = ClassificationService(
                region="us-west-2", config=config, backend="bedrock"
            )

            # Create a test document
            doc = Document(
                id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
            )

            # Add pages
            doc.pages["1"] = Page(page_id="1", image_uri="s3://bucket/image1.jpg")
            doc.pages["2"] = Page(page_id="2", image_uri="s3://bucket/image2.jpg")
            doc.pages["3"] = Page(page_id="3", image_uri="s3://bucket/image3.jpg")

            # Use a spy to verify that classify_page is never called
            with patch.object(
                service, "classify_page", wraps=service.classify_page
            ) as spy_classify_page:
                # Call the method
                result = service.classify_document(doc)

                # Verify classify_page was never called
                spy_classify_page.assert_not_called()

            # Verify results
            assert len(result.sections) == 1
            assert result.sections[0].classification == "invoice"
            assert result.sections[0].page_ids == ["1", "2", "3"]
            assert result.pages["1"].classification == "invoice"
            assert result.pages["2"].classification == "invoice"
            assert result.pages["3"].classification == "invoice"

            # Verify log message was emitted
            info_messages = [
                call.args[0] for call in mock_log_info.call_args_list if call.args
            ]
            assert any(
                "Only one document class 'invoice' is defined" in msg
                and "without calling backend" in msg
                for msg in info_messages
            ), f"no single-class skip message logged: {info_messages}"

    @patch("idp_common.s3.get_text_content")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    @patch("idp_common.utils.merge_metering_data")
    def test_holistic_classify_document(
        self, mock_merge_metering, mock_invoke, mock_get_text, service
    ):
        """Test holistic document classification."""
        # Create a test document
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )

        # Add pages
        doc.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/text1.txt")
        doc.pages["2"] = Page(page_id="2", parsed_text_uri="s3://bucket/text2.txt")

        # Mock text content
        mock_get_text.side_effect = ["Page 1 content", "Page 2 content"]

        # Mock Bedrock response
        mock_invoke.return_value = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "segments": [
                                            {
                                                "ordinal_start_page": 1,
                                                "ordinal_end_page": 2,
                                                "type": "invoice",
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            },
            "metering": {"tokens": 250},
        }

        # Mock metering merge
        mock_merge_metering.return_value = {"tokens": 250}

        # Set classification method to holistic
        service.classification_method = service.TEXTBASED_HOLISTIC

        # Call the method
        result = service.holistic_classify_document(doc)

        # Verify results
        assert len(result.sections) == 1
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1", "2"]
        assert result.pages["1"].classification == "invoice"
        assert result.pages["2"].classification == "invoice"

        # Verify metering was merged
        assert mock_merge_metering.called

        # Set the metering directly since we mocked the merge function
        result.metering = {"tokens": 250}
        assert result.metering == {"tokens": 250}

        # Verify Bedrock was called once for the whole document
        mock_invoke.assert_called_once()

    def test_holistic_classify_document_single_class_optimization(
        self, single_class_config
    ):
        """The holistic backend skip also applies only when nothing needs detecting.

        See the page-level counterpart: asserting the skip unconditionally was
        what encoded #686. Holistic classification returns segment RANGES, which
        is boundary detection, so `llm_determined` must reach the model.
        """
        config = json.loads(json.dumps(single_class_config))
        config["classification"]["sectionSplitting"] = "disabled"
        with (
            patch("boto3.Session"),
            patch("logging.Logger.info") as mock_log_info,
        ):
            service = ClassificationService(
                region="us-west-2", config=config, backend="bedrock"
            )

            # Set classification method to holistic
            service.classification_method = service.TEXTBASED_HOLISTIC

            # Create a test document
            doc = Document(
                id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
            )

            # Add pages
            doc.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/text1.txt")
            doc.pages["2"] = Page(page_id="2", parsed_text_uri="s3://bucket/text2.txt")
            doc.pages["3"] = Page(page_id="3", parsed_text_uri="s3://bucket/text3.txt")

            # Use a spy to verify that holistic_classify_document is never fully executed
            with patch.object(
                service, "_invoke_bedrock_model", wraps=service._invoke_bedrock_model
            ) as spy_invoke:
                # Call the method
                result = service.holistic_classify_document(doc)

                # Verify invoke_bedrock_model was never called
                spy_invoke.assert_not_called()

            # Verify results
            assert len(result.sections) == 1
            assert result.sections[0].classification == "invoice"
            assert result.sections[0].page_ids == ["1", "2", "3"]
            assert result.pages["1"].classification == "invoice"
            assert result.pages["2"].classification == "invoice"
            assert result.pages["3"].classification == "invoice"

            # Verify log message was emitted
            info_messages = [
                call.args[0] for call in mock_log_info.call_args_list if call.args
            ]
            assert any(
                "Only one document class 'invoice' is defined" in msg
                and "without calling backend" in msg
                for msg in info_messages
            ), f"no single-class skip message logged: {info_messages}"

    # ------------------------------------------------------------------
    # GitHub issue #686 - a single-class config must still honor
    # sectionSplitting. Before the fix, all three strategies produced one
    # section spanning every page, so `sectionSplitting: page` was silently
    # ignored and multi-record packets collapsed by construction.
    # ------------------------------------------------------------------

    @staticmethod
    def _single_class_service(single_class_config, section_splitting):
        """Build a single-class service with an explicit sectionSplitting."""
        config = json.loads(json.dumps(single_class_config))
        config["classification"]["sectionSplitting"] = section_splitting
        with patch("boto3.Session"):
            return ClassificationService(
                region="us-west-2", config=config, backend="bedrock"
            )

    @staticmethod
    def _three_page_document():
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )
        for page_id in ("1", "2", "3"):
            doc.pages[page_id] = Page(
                page_id=page_id,
                image_uri=f"s3://bucket/image{page_id}.jpg",
                parsed_text_uri=f"s3://bucket/text{page_id}.txt",
            )
        return doc

    def test_single_class_section_splitting_page(self, single_class_config):
        """`page` yields one section per page - the #686 escape hatch."""
        service = self._single_class_service(single_class_config, "page")
        doc = self._three_page_document()

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            # Still no backend call - the class decision remains predetermined.
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)
        assert all(p.classification == "invoice" for p in result.pages.values())

    def test_single_class_section_splitting_disabled(self, single_class_config):
        """`disabled` keeps the original one-section-over-all-pages behavior."""
        service = self._single_class_service(single_class_config, "disabled")
        doc = self._three_page_document()

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 1
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1", "2", "3"]

    def test_single_class_llm_determined_actually_detects_boundaries(
        self, single_class_config
    ):
        """`llm_determined` performs real boundary detection, single class or not.

        A knob that asks for LLM boundary detection has to perform it. Returning
        one all-pages section instead — which is what the single-class
        short-circuit used to do — is the #686 bug, and for a multi-record packet
        it silently discarded every record after the first.
        """
        service = self._single_class_service(single_class_config, "llm_determined")
        doc = self._three_page_document()

        # Three pages, each its own document: start / start / start.
        def fake_classify_page(page_id, *args, **kwargs):
            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type="invoice",
                    confidence=1.0,
                    metadata={"document_boundary": "start"},
                ),
            )

        with patch.object(
            service, "classify_page", side_effect=fake_classify_page
        ) as spy_classify_page:
            result = service.classify_document(doc)
            # The backend IS called now — that is the whole point.
            assert spy_classify_page.call_count == 3

        assert len(result.sections) == 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        # And the boundary signal is persisted, not discarded.
        assert [p.document_boundary for p in result.pages.values()] == [
            "start",
            "start",
            "start",
        ]

    def test_single_class_llm_determined_merges_when_model_says_continue(
        self, single_class_config
    ):
        """The model's judgement is respected in both directions."""
        service = self._single_class_service(single_class_config, "llm_determined")
        doc = self._three_page_document()

        boundaries = {"1": "start", "2": "continue", "3": "start"}

        def fake_classify_page(page_id, *args, **kwargs):
            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type="invoice",
                    confidence=1.0,
                    metadata={"document_boundary": boundaries[page_id]},
                ),
            )

        with patch.object(service, "classify_page", side_effect=fake_classify_page):
            result = service.classify_document(doc)

        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]

    def test_single_class_llm_determined_skips_backend_for_one_page(
        self, single_class_config
    ):
        """One page cannot be split, so inference would be pure waste.

        This is what keeps the common "one document per file" single-class
        deployment at zero classification cost — the thing the original
        short-circuit was really protecting.
        """
        service = self._single_class_service(single_class_config, "llm_determined")
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )
        doc.pages["1"] = Page(
            page_id="1",
            image_uri="s3://bucket/image1.jpg",
            parsed_text_uri="s3://bucket/text1.txt",
        )

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 1
        assert result.sections[0].page_ids == ["1"]
        assert result.sections[0].classification == "invoice"

    def test_holistic_single_class_section_splitting_page(self, single_class_config):
        """The holistic short-circuit honors `page` too."""
        service = self._single_class_service(single_class_config, "page")
        service.classification_method = service.TEXTBASED_HOLISTIC
        doc = self._three_page_document()

        with patch.object(service, "_invoke_bedrock_model") as spy_invoke:
            result = service.holistic_classify_document(doc)
            spy_invoke.assert_not_called()

        assert len(result.sections) == 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_multi_class_section_splitting_unchanged(
        self, mock_classify_page, mock_config
    ):
        """Regression: multi-class configs must not touch the single-class path.

        Same 3-page shape as the single-class tests, but with a second class
        defined the full boundary-detection path has to run and produce the
        boundary-derived sections it always did.
        """
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )
        assert not service.has_single_class

        doc = self._three_page_document()
        mock_classify_page.side_effect = [
            PageClassification(
                page_id="1",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "start"}
                ),
            ),
            PageClassification(
                page_id="2",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "continue"}
                ),
            ),
            PageClassification(
                page_id="3",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "start"}
                ),
            ),
        ]

        with patch.object(service, "_create_single_class_sections") as spy_single_class:
            result = service.classify_document(doc)
            spy_single_class.assert_not_called()

        # Pages 1-2 merge (continue), page 3 starts a new document.
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]

    # ------------------------------------------------------------------
    # GitHub issue #705 - the document-name-regex short-circuit is the same
    # defect as #686 reached by a different trigger: it hard-coded ONE section
    # spanning every page and never consulted sectionSplitting. A filename
    # tells you WHAT the packet holds, never WHERE each record starts, so the
    # match cannot stand in for boundary detection.
    # ------------------------------------------------------------------

    @staticmethod
    def _regex_service(mock_config, section_splitting):
        """Multi-class service where the `invoice` class matches document names.

        Deliberately multi-class: `has_single_class` is False, so the only
        short-circuit under test is the document-name-regex one.
        """
        config = json.loads(json.dumps(mock_config))
        config["classification"]["sectionSplitting"] = section_splitting
        config["classes"][0]["x-aws-idp-document-name-regex"] = "(?i).*invoice.*"
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=config, backend="bedrock"
            )
        assert not service.has_single_class
        return service

    @staticmethod
    def _regex_named_document(page_count=3):
        """A document whose NAME matches the `invoice` class regex."""
        doc = Document(
            id="ACME-invoice-packet.pdf",
            input_key="ACME-invoice-packet.pdf",
            status=Status.CLASSIFYING,
        )
        for page_id in (str(i) for i in range(1, page_count + 1)):
            doc.pages[page_id] = Page(
                page_id=page_id,
                image_uri=f"s3://bucket/image{page_id}.jpg",
                parsed_text_uri=f"s3://bucket/text{page_id}.txt",
            )
        return doc

    def test_regex_match_section_splitting_page(self, mock_config):
        """`page` yields one section per page - and still calls no backend."""
        service = self._regex_service(mock_config, "page")
        doc = self._regex_named_document()

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            # The class came from the name, and `page` needs no boundary signal.
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)
        assert all(p.classification == "invoice" for p in result.pages.values())

    def test_regex_match_section_splitting_disabled(self, mock_config):
        """`disabled` keeps the original one-section-over-all-pages behavior."""
        service = self._regex_service(mock_config, "disabled")
        doc = self._regex_named_document()

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 1
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1", "2", "3"]
        assert all(p.confidence == 1.0 for p in result.pages.values())

    def test_regex_match_llm_determined_actually_detects_boundaries(self, mock_config):
        """`llm_determined` runs real boundary detection on a name-matched packet.

        The name fixes the class, not the boundaries. Returning one all-pages
        section instead is the #705 bug, and for a packet of several invoices it
        silently discarded every record after the first. The model's own class
        guess is overridden by the regex match, as the feature documents - only
        its per-page boundary signal is used.
        """
        service = self._regex_service(mock_config, "llm_determined")
        doc = self._regex_named_document()

        # Three pages, each its own invoice. The model reports a *different*
        # class to prove the regex match stays authoritative.
        def fake_classify_page(page_id, *args, **kwargs):
            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type="receipt",
                    confidence=0.42,
                    metadata={"document_boundary": "start"},
                ),
            )

        with patch.object(
            service, "classify_page", side_effect=fake_classify_page
        ) as spy_classify_page:
            result = service.classify_document(doc)
            # The backend IS called now - that is the whole point.
            assert spy_classify_page.call_count == 3

        assert len(result.sections) == 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        # Class from the regex, boundaries from the model.
        assert all(s.classification == "invoice" for s in result.sections)
        assert all(p.classification == "invoice" for p in result.pages.values())
        assert all(p.confidence == 1.0 for p in result.pages.values())
        assert [p.document_boundary for p in result.pages.values()] == [
            "start",
            "start",
            "start",
        ]

    def test_regex_match_llm_determined_merges_when_model_says_continue(
        self, mock_config
    ):
        """The model's judgement is respected in both directions."""
        service = self._regex_service(mock_config, "llm_determined")
        doc = self._regex_named_document()

        boundaries = {"1": "start", "2": "continue", "3": "start"}

        def fake_classify_page(page_id, *args, **kwargs):
            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type="invoice",
                    confidence=1.0,
                    metadata={"document_boundary": boundaries[page_id]},
                ),
            )

        with patch.object(service, "classify_page", side_effect=fake_classify_page):
            result = service.classify_document(doc)

        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)

    def test_regex_match_llm_determined_skips_backend_for_one_page(self, mock_config):
        """One page cannot be split, so inference would be pure waste.

        This keeps the headline use of the regex feature - name the file, skip
        the LLM - at zero cost for single-page documents.
        """
        service = self._regex_service(mock_config, "llm_determined")
        doc = self._regex_named_document(page_count=1)

        with patch.object(service, "classify_page") as spy_classify_page:
            result = service.classify_document(doc)
            spy_classify_page.assert_not_called()

        assert len(result.sections) == 1
        assert result.sections[0].page_ids == ["1"]
        assert result.sections[0].classification == "invoice"

    @patch("idp_common.s3.get_text_content")
    def test_regex_match_holistic_llm_determined_uses_segment_ranges(
        self, mock_get_text, mock_config
    ):
        """Holistic segment RANGES are the boundary detection; the class is pinned."""
        service = self._regex_service(mock_config, "llm_determined")
        service.classification_method = service.TEXTBASED_HOLISTIC
        doc = self._regex_named_document()
        mock_get_text.side_effect = ["Page 1", "Page 2", "Page 3"]

        segments = {
            "segments": [
                # Model's types are wrong for a name-matched document; the ranges
                # are what it was invoked for.
                {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "receipt"},
                {"ordinal_start_page": 3, "ordinal_end_page": 3, "type": "letter"},
            ]
        }
        with patch.object(service, "_invoke_bedrock_model") as mock_invoke:
            mock_invoke.return_value = {
                "response": {
                    "output": {"message": {"content": [{"text": json.dumps(segments)}]}}
                },
                "metering": {"tokens": 300},
            }
            result = service.classify_document(doc)
            assert mock_invoke.call_count == 1

        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)
        assert all(p.classification == "invoice" for p in result.pages.values())

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_document_boundary_signals_are_logged(
        self, mock_classify_page, mock_config
    ):
        """The per-page boundary signal is not persisted anywhere, so the
        llm_determined path logs the whole map - and reports an omitted signal
        distinctly from an explicit "continue"."""
        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

        doc = self._three_page_document()
        mock_classify_page.side_effect = [
            PageClassification(
                page_id="1",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "start"}
                ),
            ),
            PageClassification(
                page_id="2",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "continue"}
                ),
            ),
            # Model omitted the field entirely - the code defaults it to
            # "continue", which is exactly the accidental-merge case that was
            # expensive to diagnose in #565.
            PageClassification(
                page_id="3",
                classification=DocumentClassification(doc_type="invoice", metadata={}),
            ),
        ]

        with patch("logging.Logger.info") as mock_log_info:
            service.classify_document(doc)

        mock_log_info.assert_any_call(
            "Page document_boundary signals: "
            "{'1': 'start', '2': 'continue', '3': '(absent)'}"
        )

    @patch("idp_common.s3.get_text_content")
    @patch(
        "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
    )
    def test_holistic_classify_document_multiple_segments(
        self, mock_invoke, mock_get_text, service
    ):
        """Test holistic document classification with multiple segments."""
        # Create a test document
        doc = Document(
            id="test-doc", input_key="test-document.pdf", status=Status.CLASSIFYING
        )

        # Add pages
        doc.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/text1.txt")
        doc.pages["2"] = Page(page_id="2", parsed_text_uri="s3://bucket/text2.txt")
        doc.pages["3"] = Page(page_id="3", parsed_text_uri="s3://bucket/text3.txt")

        # Mock text content
        mock_get_text.side_effect = [
            "Page 1 content",
            "Page 2 content",
            "Page 3 content",
        ]

        # Mock Bedrock response with multiple segments
        mock_invoke.return_value = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "segments": [
                                            {
                                                "ordinal_start_page": 1,
                                                "ordinal_end_page": 1,
                                                "type": "invoice",
                                            },
                                            {
                                                "ordinal_start_page": 2,
                                                "ordinal_end_page": 3,
                                                "type": "receipt",
                                            },
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            },
            "metering": {"tokens": 300},
        }

        # Set classification method to holistic
        service.classification_method = service.TEXTBASED_HOLISTIC

        # Call the method
        result = service.holistic_classify_document(doc)

        # Verify results
        assert len(result.sections) == 2
        assert result.sections[0].classification == "invoice"
        assert result.sections[0].page_ids == ["1"]
        assert result.sections[1].classification == "receipt"
        assert result.sections[1].page_ids == ["2", "3"]

        # Verify page classifications
        assert result.pages["1"].classification == "invoice"
        assert result.pages["2"].classification == "receipt"
        assert result.pages["3"].classification == "receipt"

    def test_group_consecutive_pages_with_boundary(self, service):
        """Pages with boundary flag start new sections even with same doc type."""
        results = [
            PageClassification(
                page_id="1",
                classification=DocumentClassification(
                    doc_type="invoice",
                    metadata={"document_boundary": "start"},
                ),
            ),
            PageClassification(
                page_id="2",
                classification=DocumentClassification(
                    doc_type="invoice",
                    metadata={"document_boundary": "continue"},
                ),
            ),
            PageClassification(
                page_id="3",
                classification=DocumentClassification(
                    doc_type="invoice",
                    metadata={"document_boundary": "start"},
                ),
            ),
        ]

        sections = service._group_consecutive_pages(results)
        assert len(sections) == 2
        assert [p.page_id for p in sections[0].pages] == ["1", "2"]
        assert [p.page_id for p in sections[1].pages] == ["3"]

    def test_section_splitting_disabled(self, mock_config):
        """Test sectionSplitting=disabled creates single section with all pages using majority voting."""
        # Update config with disabled splitting
        mock_config["classification"]["sectionSplitting"] = "disabled"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1")
            doc.pages["2"] = Page(page_id="2")
            doc.pages["3"] = Page(page_id="3")

            # Create mock page results with different classifications
            # invoice appears twice, receipt once -> invoice should win by voting
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(doc_type="receipt"),
                ),
                PageClassification(
                    page_id="3",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Single section with all pages, using majority-voted class (invoice wins 2-1)
            assert len(result.sections) == 1
            assert result.sections[0].classification == "invoice"  # Majority wins
            assert result.sections[0].page_ids == ["1", "2", "3"]

            # Verify all pages assigned the same class
            assert result.pages["1"].classification == "invoice"
            assert result.pages["2"].classification == "invoice"
            assert result.pages["3"].classification == "invoice"

    def test_section_splitting_disabled_excludes_blank_pages_from_voting(
        self, mock_config
    ):
        """Test sectionSplitting=disabled excludes blank/unclassifiable pages from voting.

        This addresses GitHub Issue #167 where blank pages could incorrectly determine
        the document classification.
        """
        mock_config["classification"]["sectionSplitting"] = "disabled"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document (6 pages like the issue)
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            for i in range(1, 7):
                doc.pages[str(i)] = Page(page_id=str(i))

            # Create mock page results matching Issue #167 scenario:
            # 5 pages classified as DRILLING_PLAN_GEOLOGIC, 1 blank page
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(
                        doc_type="DRILLING_PLAN_GEOLOGIC"
                    ),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(
                        doc_type="DRILLING_PLAN_GEOLOGIC"
                    ),
                ),
                PageClassification(
                    page_id="3",
                    classification=DocumentClassification(
                        doc_type="DRILLING_PLAN_GEOLOGIC"
                    ),
                ),
                PageClassification(
                    page_id="4",
                    classification=DocumentClassification(
                        doc_type="DRILLING_PLAN_GEOLOGIC"
                    ),
                ),
                PageClassification(
                    page_id="5",
                    classification=DocumentClassification(
                        doc_type="DRILLING_PLAN_GEOLOGIC"
                    ),
                ),
                PageClassification(
                    page_id="6",
                    classification=DocumentClassification(
                        doc_type="unclassifiable_blank_page"
                    ),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Blank page is excluded from voting, so DRILLING_PLAN_GEOLOGIC wins
            assert len(result.sections) == 1
            assert result.sections[0].classification == "DRILLING_PLAN_GEOLOGIC"
            assert result.sections[0].page_ids == ["1", "2", "3", "4", "5", "6"]

    def test_section_splitting_disabled_voting_tie_uses_first_page(self, mock_config):
        """Test sectionSplitting=disabled uses first page's class as tie-breaker."""
        mock_config["classification"]["sectionSplitting"] = "disabled"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1")
            doc.pages["2"] = Page(page_id="2")

            # Create mock page results with a tie: invoice and receipt each appear once
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(doc_type="receipt"),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Tie-breaker uses first page (invoice)
            assert len(result.sections) == 1
            assert result.sections[0].classification == "invoice"

    def test_section_splitting_disabled_all_pages_unclassifiable(self, mock_config):
        """Test sectionSplitting=disabled handles all pages being unclassifiable."""
        mock_config["classification"]["sectionSplitting"] = "disabled"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1")
            doc.pages["2"] = Page(page_id="2")

            # Create mock page results where all pages are unclassifiable
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(
                        doc_type="unclassifiable_blank_page"
                    ),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(doc_type="blank"),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Falls back to first page's classification when all are unclassifiable
            assert len(result.sections) == 1
            assert result.sections[0].classification == "unclassifiable_blank_page"

    def test_section_splitting_disabled_handles_completion_order_bug(self, mock_config):
        """Test that voting fixes the completion-order bug from Issue #167.

        The original bug: page_results came in completion order (not page order),
        so blank pages that finish fast would end up at index 0 and be used as
        the document classification.
        """
        mock_config["classification"]["sectionSplitting"] = "disabled"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            for i in range(1, 5):
                doc.pages[str(i)] = Page(page_id=str(i))

            # Simulate completion order bug: blank page completed first and is at index 0
            # even though it's actually page 4
            page_results = [
                PageClassification(
                    page_id="4",  # Blank page completed first
                    classification=DocumentClassification(
                        doc_type="unclassifiable_blank_page"
                    ),
                ),
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
                PageClassification(
                    page_id="3",
                    classification=DocumentClassification(doc_type="invoice"),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Voting correctly selects invoice (3 votes) despite blank page
            # being at index 0 in the unsorted results
            assert len(result.sections) == 1
            assert result.sections[0].classification == "invoice"

    def test_section_splitting_page(self, mock_config):
        """Test sectionSplitting=page creates one section per page."""
        # Update config with per-page splitting
        mock_config["classification"]["sectionSplitting"] = "page"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1")
            doc.pages["2"] = Page(page_id="2")
            doc.pages["3"] = Page(page_id="3")

            # Create mock page results with mixed classifications
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(
                        doc_type="invoice", confidence=0.95
                    ),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(
                        doc_type="invoice", confidence=0.90
                    ),
                ),
                PageClassification(
                    page_id="3",
                    classification=DocumentClassification(
                        doc_type="receipt", confidence=0.85
                    ),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: One section per page, each preserving its classification
            assert len(result.sections) == 3

            assert result.sections[0].section_id == "1"
            assert result.sections[0].classification == "invoice"
            assert result.sections[0].page_ids == ["1"]
            assert result.sections[0].confidence == 0.95

            assert result.sections[1].section_id == "2"
            assert result.sections[1].classification == "invoice"
            assert result.sections[1].page_ids == ["2"]
            assert result.sections[1].confidence == 0.90

            assert result.sections[2].section_id == "3"
            assert result.sections[2].classification == "receipt"
            assert result.sections[2].page_ids == ["3"]
            assert result.sections[2].confidence == 0.85

            # Verify pages maintain their individual classifications
            assert result.pages["1"].classification == "invoice"
            assert result.pages["2"].classification == "invoice"
            assert result.pages["3"].classification == "receipt"

    def test_section_splitting_llm_determined(self, mock_config):
        """Test sectionSplitting=llm_determined uses boundary metadata."""
        # Update config with LLM-determined splitting (default)
        mock_config["classification"]["sectionSplitting"] = "llm_determined"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1")
            doc.pages["2"] = Page(page_id="2")
            doc.pages["3"] = Page(page_id="3")
            doc.pages["4"] = Page(page_id="4")

            # Create mock page results with boundary metadata
            # Scenario: 2 invoices (same type but boundary="start" separates them)
            page_results = [
                PageClassification(
                    page_id="1",
                    classification=DocumentClassification(
                        doc_type="invoice",
                        metadata={"document_boundary": "start"},
                    ),
                ),
                PageClassification(
                    page_id="2",
                    classification=DocumentClassification(
                        doc_type="invoice",
                        metadata={"document_boundary": "continue"},
                    ),
                ),
                PageClassification(
                    page_id="3",
                    classification=DocumentClassification(
                        doc_type="invoice",
                        metadata={"document_boundary": "start"},
                    ),
                ),
                PageClassification(
                    page_id="4",
                    classification=DocumentClassification(
                        doc_type="invoice",
                        metadata={"document_boundary": "continue"},
                    ),
                ),
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: Two sections (separated by boundary="start")
            assert len(result.sections) == 2

            assert result.sections[0].section_id == "1"
            assert result.sections[0].classification == "invoice"
            assert result.sections[0].page_ids == ["1", "2"]

            assert result.sections[1].section_id == "2"
            assert result.sections[1].classification == "invoice"
            assert result.sections[1].page_ids == ["3", "4"]

    def test_section_splitting_default_value(self, mock_config):
        """Test that default sectionSplitting value is llm_determined."""
        # Don't specify sectionSplitting - should default to llm_determined
        mock_config["classification"].pop("sectionSplitting", None)

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Verify default value
            assert service.config.classification.sectionSplitting == "llm_determined"

    def test_section_splitting_invalid_value(self, mock_config):
        """Test that invalid sectionSplitting value falls back to llm_determined."""
        # Set invalid value
        mock_config["classification"]["sectionSplitting"] = "invalid_value"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Verify fallback to default
            assert service.config.classification.sectionSplitting == "llm_determined"

    def test_section_splitting_page_prevents_same_type_joining(self, mock_config):
        """Test that page splitting prevents joining of same-type documents."""
        # This is the critical use case from GitHub Issue #146
        mock_config["classification"]["sectionSplitting"] = "page"

        with patch("boto3.Session"):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )

            # Create document with multiple W-2 forms (same type)
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            for i in range(1, 7):  # 6 pages
                doc.pages[str(i)] = Page(page_id=str(i))

            # All pages classified as W2
            page_results = [
                PageClassification(
                    page_id=str(i),
                    classification=DocumentClassification(doc_type="W2"),
                )
                for i in range(1, 7)
            ]

            # Apply strategy
            result = service._apply_section_splitting_strategy(doc, page_results)

            # Verify: 6 separate sections (prevents unwanted joining)
            assert len(result.sections) == 6
            for i, section in enumerate(result.sections, start=1):
                assert section.section_id == str(i)
                assert section.classification == "W2"
                assert section.page_ids == [str(i)]

    def test_holistic_classification_with_section_splitting(self, mock_config):
        """Test holistic classification respects sectionSplitting config."""
        mock_config["classification"]["sectionSplitting"] = "page"

        with (
            patch("boto3.Session"),
            patch("idp_common.s3.get_text_content") as mock_get_text,
            patch(
                "idp_common.classification.service.ClassificationService._invoke_bedrock_model"
            ) as mock_invoke,
        ):
            service = ClassificationService(
                region="us-west-2", config=mock_config, backend="bedrock"
            )
            service.classification_method = service.TEXTBASED_HOLISTIC

            # Create test document
            doc = Document(id="test-doc", status=Status.CLASSIFYING)
            doc.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/text1.txt")
            doc.pages["2"] = Page(page_id="2", parsed_text_uri="s3://bucket/text2.txt")

            # Mock responses
            mock_get_text.side_effect = ["Page 1", "Page 2"]
            mock_invoke.return_value = {
                "response": {
                    "output": {
                        "message": {
                            "content": [
                                {
                                    "text": json.dumps(
                                        {
                                            "segments": [
                                                {
                                                    "ordinal_start_page": 1,
                                                    "ordinal_end_page": 2,
                                                    "type": "invoice",
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                },
                "metering": {},
            }

            # Call the method
            result = service.holistic_classify_document(doc)

            # With sectionSplitting=page, should create 2 sections (one per page)
            assert len(result.sections) == 2
            assert result.sections[0].page_ids == ["1"]
            assert result.sections[1].page_ids == ["2"]
            assert result.sections[0].classification == "invoice"
            assert result.sections[1].classification == "invoice"
