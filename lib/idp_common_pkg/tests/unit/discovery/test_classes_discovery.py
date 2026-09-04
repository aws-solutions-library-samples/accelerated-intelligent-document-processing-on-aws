# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the ClassesDiscovery class.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Import standard library modules first
import json
from unittest.mock import MagicMock, patch, call

# Import third-party modules

# Import application modules
from idp_common.discovery.classes_discovery import ClassesDiscovery
from idp_common.config.models import IDPConfig, DiscoveryConfig, DiscoveryModelConfig


@pytest.mark.unit
class TestClassesDiscovery:
    """Tests for the ClassesDiscovery class."""

    @pytest.fixture
    def mock_config(self):
        """Fixture providing a mock configuration."""
        return IDPConfig(
            discovery=DiscoveryConfig(
                without_ground_truth=DiscoveryModelConfig(
                    model_id="anthropic.claude-3-sonnet-20240229-v1:0",
                    temperature=1.0,
                    top_p=0.1,
                    max_tokens=10000,
                ),
                with_ground_truth=DiscoveryModelConfig(
                    model_id="anthropic.claude-3-sonnet-20240229-v1:0",
                    temperature=1.0,
                    top_p=0.1,
                    max_tokens=10000,
                ),
            )
        )

    @pytest.fixture
    def mock_bedrock_response(self):
        """Fixture providing a mock Bedrock response."""
        return {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "$schema": "http://json-schema.org/draft-07/schema#",
                                        "$id": "w4",
                                        "type": "object",
                                        "title": "W-4",
                                        "description": "Employee's Withholding Certificate form",
                                        "properties": {
                                            "PersonalInformation": {
                                                "type": "object",
                                                "description": "Personal information of employee",
                                                "properties": {
                                                    "FirstName": {
                                                        "type": "string",
                                                        "description": "First Name of Employee",
                                                    },
                                                    "LastName": {
                                                        "type": "string",
                                                        "description": "Last Name of Employee",
                                                    },
                                                },
                                            }
                                        },
                                    }
                                )
                            }
                        ]
                    }
                }
            },
            "metering": {"tokens": 500},
        }

    @pytest.fixture
    def mock_ground_truth_data(self):
        """Fixture providing mock ground truth data."""
        return {
            "employee_name": "John Doe",
            "ssn": "123-45-6789",
            "address": {
                "street": "123 Main St",
                "city": "Anytown",
                "state": "CA",
                "zip": "12345",
            },
            "filing_status": "Single",
        }

    @pytest.fixture
    def mock_configuration_item(self):
        """Fixture providing a mock configuration item."""
        return IDPConfig(
            classes=[
                {
                    "name": "W-4",
                    "description": "Employee's Withholding Certificate form",
                    "attributes": [
                        {
                            "name": "PersonalInformation",
                            "description": "Personal information of employee",
                            "attributeType": "group",
                        }
                    ],
                }
            ]
        )

    @pytest.fixture
    def service(self, mock_config):
        """Fixture providing a ClassesDiscovery instance."""
        with (
            patch("boto3.resource") as mock_dynamodb,
            patch("idp_common.bedrock.BedrockClient") as mock_bedrock_client,
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationManager"
            ) as mock_config_manager,
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            # Mock DynamoDB table
            mock_table = MagicMock()
            mock_dynamodb.return_value.Table.return_value = mock_table

            # Mock BedrockClient
            mock_client = MagicMock()
            mock_bedrock_client.return_value = mock_client

            # Mock the ConfigurationReader to return the mock config
            mock_reader_instance = mock_config_reader.return_value
            mock_reader_instance.get_merged_configuration.return_value = mock_config

            # Mock the ConfigurationManager
            mock_manager_instance = mock_config_manager.return_value
            mock_manager_instance.get_configuration.return_value = None
            mock_manager_instance.update_configuration.return_value = None

            service = ClassesDiscovery(
                input_bucket="test-bucket",
                input_prefix="test-document.pdf",
                region="us-west-2",
                version="test-version",
            )

            # Store mocks for access in tests
            service._mock_table = mock_table
            service._mock_bedrock_client = mock_client
            service.config_manager = mock_manager_instance

            return service

    def test_init(self, mock_config):
        """Test initialization of ClassesDiscovery."""
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient") as mock_bedrock_client,
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            # Mock the ConfigurationReader to return the mock config
            mock_reader_instance = mock_config_reader.return_value
            mock_reader_instance.get_merged_configuration.return_value = mock_config

            service = ClassesDiscovery(
                input_bucket="test-bucket",
                input_prefix="test-document.pdf",
                region="us-west-2",
            )

            assert service.input_bucket == "test-bucket"
            assert service.input_prefix == "test-document.pdf"
            # Verify config is loaded correctly
            assert (
                service.without_gt_config.model_id
                == "anthropic.claude-3-sonnet-20240229-v1:0"
            )
            assert (
                service.with_gt_config.model_id
                == "anthropic.claude-3-sonnet-20240229-v1:0"
            )
            assert service.region == "us-west-2"

            # Verify BedrockClient was initialized with correct region
            mock_bedrock_client.assert_called_once_with(region="us-west-2")

    def test_init_target_version_missing_falls_back_to_active(self, mock_config):
        """A not-yet-created target version (e.g. a fresh 'quickstart') must not
        fail init: load the active/default config for discovery settings while
        keeping the target version as the write target."""
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient"),
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            mock_reader_instance = mock_config_reader.return_value
            # version="quickstart" doesn't exist yet -> ValueError; the None
            # (active/default) fallback then succeeds.
            mock_reader_instance.get_merged_configuration.side_effect = [
                ValueError("No Version quickstart configuration found"),
                mock_config,
            ]

            service = ClassesDiscovery(
                input_bucket="test-bucket",
                input_prefix="test-document.pdf",
                region="us-west-2",
                version="quickstart",
            )

            # Write target is preserved so the class is saved into quickstart.
            assert service.version == "quickstart"
            # Settings came from the fallback (active/default) config.
            assert (
                service.without_gt_config.model_id
                == "anthropic.claude-3-sonnet-20240229-v1:0"
            )
            calls = mock_reader_instance.get_merged_configuration.call_args_list
            assert calls[0].kwargs["version"] == "quickstart"
            assert calls[1].kwargs["version"] is None

    def test_init_with_default_region(self, mock_config):
        """Test initialization with default region from environment."""
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient"),
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch.dict(
                "os.environ",
                {"AWS_REGION": "us-east-1", "CONFIGURATION_TABLE_NAME": "test-table"},
            ),
        ):
            # Mock the ConfigurationReader to return the mock config
            mock_reader_instance = mock_config_reader.return_value
            mock_reader_instance.get_merged_configuration.return_value = mock_config

            service = ClassesDiscovery(
                input_bucket="test-bucket",
                input_prefix="test-document.pdf",
                region=None,  # Explicitly pass None to trigger environment lookup
            )

            assert service.region == "us-east-1"

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_discovery_classes_with_document_success(
        self,
        mock_extract_text,
        mock_get_bytes,
        service,
        mock_bedrock_response,
        mock_configuration_item,
    ):
        """Test successful document class discovery."""
        # Mock S3 file content
        mock_file_content = b"fake_pdf_content"
        mock_get_bytes.return_value = mock_file_content

        # Mock Bedrock response with JSON Schema format
        service._mock_bedrock_client.return_value = mock_bedrock_response
        mock_extract_text.return_value = json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$id": "w4",
                "type": "object",
                "title": "W-4",
                "description": "Employee's Withholding Certificate form",
                "x-aws-idp-document-type": "W-4",
                "properties": {
                    "PersonalInformation": {
                        "type": "object",
                        "description": "Personal information of employee",
                        "properties": {
                            "FirstName": {
                                "type": "string",
                                "description": "First Name of Employee",
                            },
                            "LastName": {
                                "type": "string",
                                "description": "Last Name of Employee",
                            },
                        },
                    }
                },
            }
        )

        # Mock configuration retrieval for Default and Custom
        service.config_manager.get_configuration.return_value = mock_configuration_item
        service.config_manager.get_raw_configuration.return_value = (
            None  # No existing Custom
        )

        # Call the method
        result = service.discovery_classes_with_document(
            "test-bucket", "test-document.pdf"
        )

        # Verify result
        assert result["status"] == "SUCCESS"

        # Verify S3 was called
        mock_get_bytes.assert_called_once_with(
            bucket="test-bucket", key="test-document.pdf"
        )

        # Verify Bedrock was called
        service._mock_bedrock_client.invoke_model.assert_called_once()

        # Verify configuration was saved via raw configuration (sparse delta pattern)
        service.config_manager.save_raw_configuration.assert_called_once()
        call_args = service.config_manager.save_raw_configuration.call_args
        assert call_args[0][0] == "Config"  # First arg is config type
        assert "classes" in call_args[0][1]  # Second arg is config dict with classes

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    def test_discovery_classes_with_document_s3_error(self, mock_get_bytes, service):
        """Test handling of S3 error during document discovery."""
        mock_get_bytes.side_effect = Exception("S3 access denied")

        with pytest.raises(
            Exception, match="Failed to process document test-document.pdf"
        ):
            service.discovery_classes_with_document("test-bucket", "test-document.pdf")

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_discovery_classes_with_document_bedrock_error(
        self, mock_extract_text, mock_get_bytes, service
    ):
        """Test handling of Bedrock error during document discovery."""
        mock_get_bytes.return_value = b"fake_content"
        service._mock_bedrock_client.side_effect = Exception("Bedrock error")

        with pytest.raises(
            Exception, match="Failed to process document test-document.pdf"
        ):
            service.discovery_classes_with_document("test-bucket", "test-document.pdf")

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_discovery_classes_with_document_invalid_json(
        self, mock_extract_text, mock_get_bytes, service
    ):
        """Test handling of invalid JSON response from Bedrock."""
        mock_get_bytes.return_value = b"fake_content"
        service._mock_bedrock_client.return_value = {"response": {}, "metering": {}}
        mock_extract_text.return_value = "Invalid JSON response"

        with pytest.raises(
            Exception, match="Failed to process document test-document.pdf"
        ):
            service.discovery_classes_with_document("test-bucket", "test-document.pdf")

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_discovery_classes_with_document_and_ground_truth_success(
        self,
        mock_extract_text,
        mock_get_bytes,
        service,
        mock_ground_truth_data,
        mock_configuration_item,
    ):
        """Test successful document class discovery with ground truth."""
        # Mock S3 file content
        mock_file_content = b"fake_pdf_content"
        mock_ground_truth_content = json.dumps(mock_ground_truth_data).encode()
        mock_get_bytes.side_effect = [mock_ground_truth_content, mock_file_content]

        # Mock Bedrock response with JSON Schema format
        service._mock_bedrock_client.return_value = {
            "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
            "metering": {"tokens": 500},
        }
        mock_extract_text.return_value = json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$id": "w4",
                "type": "object",
                "title": "W-4",
                "description": "Employee's Withholding Certificate form",
                "x-aws-idp-document-type": "W-4",
                "properties": {},
            }
        )

        # Mock configuration retrieval
        service.config_manager.get_configuration.return_value = mock_configuration_item

        # Call the method
        result = service.discovery_classes_with_document_and_ground_truth(
            "test-bucket", "test-document.pdf", "ground-truth.json"
        )

        # Verify result
        assert result["status"] == "SUCCESS"

        # Verify S3 was called twice (ground truth + document)
        assert mock_get_bytes.call_count == 2
        mock_get_bytes.assert_has_calls(
            [
                call(bucket="test-bucket", key="ground-truth.json"),
                call(bucket="test-bucket", key="test-document.pdf"),
            ]
        )

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    def test_load_ground_truth_success(
        self, mock_get_bytes, service, mock_ground_truth_data
    ):
        """Test successful loading of ground truth data."""
        mock_get_bytes.return_value = json.dumps(mock_ground_truth_data).encode()

        result = service._load_ground_truth("test-bucket", "ground-truth.json")

        assert result == mock_ground_truth_data
        mock_get_bytes.assert_called_once_with(
            bucket="test-bucket", key="ground-truth.json"
        )

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    def test_load_ground_truth_invalid_json(self, mock_get_bytes, service):
        """Test loading invalid JSON ground truth data."""
        mock_get_bytes.return_value = b"Invalid JSON content"

        with pytest.raises(Exception):
            service._load_ground_truth("test-bucket", "ground-truth.json")

    @patch("idp_common.utils.s3util.S3Util.get_bytes")
    def test_load_ground_truth_s3_error(self, mock_get_bytes, service):
        """Test handling S3 error when loading ground truth."""
        mock_get_bytes.side_effect = Exception("S3 error")

        with pytest.raises(Exception):
            service._load_ground_truth("test-bucket", "ground-truth.json")

    @patch("idp_common.image.prepare_bedrock_image_attachment")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_extract_data_from_document_success(
        self, mock_extract_text, mock_prepare_image, service
    ):
        """Test successful data extraction from document."""
        mock_document_content = b"fake_image_content"
        # Return valid JSON Schema
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": "w4",
            "type": "object",
            "description": "Test document",
            "x-aws-idp-document-type": "W-4",
            "properties": {},
        }
        mock_extract_text.return_value = json.dumps(schema)
        service._mock_bedrock_client.return_value = {
            "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
            "metering": {"tokens": 500},
        }

        # Mock the image preparation
        mock_prepare_image.return_value = {
            "image": {
                "format": "jpeg",
                "source": {"bytes": "base64_encoded_image_data"},
            }
        }

        result = service._extract_data_from_document(mock_document_content, "jpg")

        # Verify JSON Schema format
        assert result["$id"] == "w4"
        assert result["description"] == "Test document"
        assert result["$schema"] == "http://json-schema.org/draft-07/schema#"

        # Verify Bedrock was called with correct parameters
        service._mock_bedrock_client.invoke_model.assert_called_once()
        call_args = service._mock_bedrock_client.invoke_model.call_args[1]
        assert call_args["model_id"] == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert call_args["temperature"] == 1.0
        assert call_args["top_p"] == 0.1
        assert call_args["max_tokens"] == 10000
        assert call_args["context"] == "ClassesDiscovery"

    @patch("idp_common.bedrock.extract_text_from_response")
    def test_extract_data_from_document_pdf(self, mock_extract_text, service):
        """Test data extraction from PDF document."""
        mock_document_content = b"fake_pdf_content"
        mock_extract_text.return_value = json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$id": "form",
                "type": "object",
                "title": "Form",
                "description": "Generic form",
                "x-aws-idp-document-type": "Form",
                "properties": {},
            }
        )
        service._mock_bedrock_client.return_value = {
            "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
            "metering": {"tokens": 500},
        }

        result = service._extract_data_from_document(mock_document_content, "pdf")

        assert result is not None

        # Verify the content structure for PDF
        call_args = service._mock_bedrock_client.invoke_model.call_args[1]
        content = call_args["content"]
        assert len(content) == 2
        assert "document" in content[0]
        assert content[0]["document"]["format"] == "pdf"
        assert "text" in content[1]

    def test_extract_data_from_document_bedrock_error(self, service):
        """Test handling of Bedrock error during data extraction."""
        service._mock_bedrock_client.side_effect = Exception("Bedrock error")

        result = service._extract_data_from_document(b"fake_content", "jpg")

        assert result is None

    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_create_content_list_image(self, mock_prepare_image, service):
        """Test creating content list for image document."""
        mock_content = b"fake_image_content"
        prompt = "Test prompt"

        # Mock the image preparation
        mock_prepare_image.return_value = {
            "image": {"format": "jpg", "source": {"bytes": "base64_encoded_image_data"}}
        }

        result = service._create_content_list(prompt, mock_content, "jpg")

        assert len(result) == 2
        assert "image" in result[0]
        mock_prepare_image.assert_called_once_with(mock_content)
        assert result[0]["image"]["format"] == "jpg"
        assert "source" in result[0]["image"]
        assert "bytes" in result[0]["image"]["source"]
        assert result[1]["text"] == prompt

    def test_create_content_list_pdf(self, service):
        """Test creating content list for PDF document."""
        mock_content = b"fake_pdf_content"
        prompt = "Test prompt"

        result = service._create_content_list(prompt, mock_content, "pdf")

        assert len(result) == 2
        assert "document" in result[0]
        assert result[0]["document"]["format"] == "pdf"
        assert result[0]["document"]["name"] == "document_messages"
        assert result[0]["document"]["source"]["bytes"] == mock_content
        assert result[1]["text"] == prompt

    @patch("idp_common.image.prepare_bedrock_image_attachment")
    @patch("idp_common.bedrock.extract_text_from_response")
    def test_extract_data_from_document_with_ground_truth_success(
        self, mock_extract_text, mock_prepare_image, service, mock_ground_truth_data
    ):
        """Test successful data extraction with ground truth."""
        mock_document_content = b"fake_image_content"
        # Return valid JSON Schema
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": "w4",
            "type": "object",
            "description": "Test document",
            "x-aws-idp-document-type": "W-4",
            "properties": {},
        }
        mock_extract_text.return_value = json.dumps(schema)
        service._mock_bedrock_client.return_value = {
            "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
            "metering": {"tokens": 500},
        }

        # Mock the image preparation
        mock_prepare_image.return_value = {
            "format": "jpeg",
            "source": {"bytes": "base64_encoded_image_data"},
        }

        result = service._extract_data_from_document_with_ground_truth(
            mock_document_content, "jpg", mock_ground_truth_data
        )

        # Verify JSON Schema format
        assert result["$id"] == "w4"
        assert result["description"] == "Test document"

        # Verify Bedrock was called with ground truth context
        service._mock_bedrock_client.invoke_model.assert_called_once()
        call_args = service._mock_bedrock_client.invoke_model.call_args[1]
        assert call_args["context"] == "ClassesDiscoveryWithGroundTruth"

    def test_extract_data_from_document_with_ground_truth_error(
        self, service, mock_ground_truth_data
    ):
        """Test handling of error during ground truth extraction."""
        service._mock_bedrock_client.side_effect = Exception("Bedrock error")

        result = service._extract_data_from_document_with_ground_truth(
            b"fake_content", "jpg", mock_ground_truth_data
        )

        assert result is None

    def test_prompt_classes_discovery_with_ground_truth(
        self, service, mock_ground_truth_data
    ):
        """Test prompt generation with ground truth data."""
        result = service._prompt_classes_discovery_with_ground_truth(
            mock_ground_truth_data
        )

        assert "GROUND_TRUTH_REFERENCE" in result
        assert json.dumps(mock_ground_truth_data, indent=2) in result
        # Now generates JSON Schema format
        assert "$schema" in result
        assert "$id" in result
        # JSON Schema uses "description" not "document_description"
        assert "description" in result

    def test_prompt_classes_discovery(self, service):
        """Test basic prompt generation for classes discovery."""
        result = service._prompt_classes_discovery()

        assert "forms data" in result
        # Now generates JSON Schema format
        assert "$schema" in result
        assert "$id" in result
        assert "properties" in result
        assert "JSON Schema" in result

    def test_sample_output_format(self, service):
        """Test sample output format generation."""
        result = service._sample_output_format()

        # Now generates JSON Schema format
        assert "$schema" in result
        assert "$id" in result
        assert "description" in result
        assert "properties" in result
        assert "PersonalInformation" in result
        assert "FirstName" in result
        assert "Age" in result

    def test_discovery_classes_with_document_updates_existing_class(
        self, service, mock_configuration_item
    ):
        """Test that discovery updates existing class in the target version only."""
        with (
            patch("idp_common.utils.s3util.S3Util.get_bytes") as mock_get_bytes,
            patch("idp_common.bedrock.extract_text_from_response") as mock_extract_text,
        ):
            mock_get_bytes.return_value = b"fake_content"
            mock_extract_text.return_value = json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$id": "w4",
                    "type": "object",
                    "title": "W-4",
                    "description": "Updated description",
                    "x-aws-idp-document-type": "W-4",
                    "properties": {},
                }
            )
            service._mock_bedrock_client.return_value = {
                "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
                "metering": {"tokens": 500},
            }
            # Target version already has an old W-4 and another class
            service.config_manager.get_raw_configuration.return_value = {
                "classes": [
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "$id": "w4",
                        "type": "object",
                        "title": "W-4",
                        "description": "Old description",
                        "x-aws-idp-document-type": "W-4",
                        "properties": {},
                    },
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "$id": "other_form",
                        "type": "object",
                        "title": "Other-Form",
                        "description": "Other form",
                        "x-aws-idp-document-type": "Other-Form",
                        "properties": {},
                    },
                ]
            }

            result = service.discovery_classes_with_document(
                "test-bucket", "test-document.pdf"
            )

            assert result["status"] == "SUCCESS"

            # Verify that configuration manager was called with save_raw_configuration
            service.config_manager.save_raw_configuration.assert_called_once()
            call_args = service.config_manager.save_raw_configuration.call_args
            assert call_args[0][0] == "Config"  # Config type
            updated_classes = call_args[0][1]["classes"]  # Classes from saved config

            # Should have 2 classes (existing Other-Form + updated W-4 from version)
            assert len(updated_classes) == 2

            # Find the W-4 class and verify it was updated (by $id)
            w4_class = next(
                (cls for cls in updated_classes if cls.get("$id") == "w4"), None
            )
            assert w4_class is not None
            assert w4_class["description"] == "Updated description"

            # Verify Other-Form is preserved from the version
            other_class = next(
                (cls for cls in updated_classes if cls.get("$id") == "other_form"), None
            )
            assert other_class is not None
            assert other_class["description"] == "Other form"

    def test_discovery_does_not_pull_default_classes_into_version(self, service):
        """Test that discovery does NOT inject default config classes into the target version.

        When a user runs discovery on a version, only the discovered class should be
        added. Classes from the 'default' config version should NOT be merged in.
        """
        with (
            patch("idp_common.utils.s3util.S3Util.get_bytes") as mock_get_bytes,
            patch("idp_common.bedrock.extract_text_from_response") as mock_extract_text,
        ):
            mock_get_bytes.return_value = b"fake_content"
            mock_extract_text.return_value = json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$id": "w4",
                    "type": "object",
                    "title": "W-4",
                    "description": "Discovered W-4",
                    "x-aws-idp-document-type": "W-4",
                    "properties": {},
                }
            )
            service._mock_bedrock_client.return_value = {
                "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
                "metering": {"tokens": 500},
            }
            # Target version has NO classes yet (empty version)
            service.config_manager.get_raw_configuration.return_value = {}

            result = service.discovery_classes_with_document(
                "test-bucket", "test-document.pdf"
            )

            assert result["status"] == "SUCCESS"

            # Verify only the discovered class is saved — no default classes injected
            service.config_manager.save_raw_configuration.assert_called_once()
            call_args = service.config_manager.save_raw_configuration.call_args
            updated_classes = call_args[0][1]["classes"]

            assert len(updated_classes) == 1
            assert updated_classes[0]["$id"] == "w4"
            assert updated_classes[0]["description"] == "Discovered W-4"

            # Verify get_configuration was NOT called (no default config reading)
            service.config_manager.get_configuration.assert_not_called()

    def test_discovery_classes_with_document_no_existing_config(self, service):
        """Test discovery when no existing configuration exists."""
        with (
            patch("idp_common.utils.s3util.S3Util.get_bytes") as mock_get_bytes,
            patch("idp_common.bedrock.extract_text_from_response") as mock_extract_text,
        ):
            mock_get_bytes.return_value = b"fake_content"
            mock_extract_text.return_value = json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$id": "w4",
                    "type": "object",
                    "title": "W-4",
                    "description": "New form",
                    "x-aws-idp-document-type": "W-4",
                    "properties": {},
                }
            )
            service._mock_bedrock_client.return_value = {
                "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
                "metering": {"tokens": 500},
            }
            # No Default config
            service.config_manager.get_configuration.return_value = None
            # No Custom config
            service.config_manager.get_raw_configuration.return_value = None

            result = service.discovery_classes_with_document(
                "test-bucket", "test-document.pdf"
            )

            assert result["status"] == "SUCCESS"

            # Verify configuration was saved via save_raw_configuration
            service.config_manager.save_raw_configuration.assert_called_once()
            call_args = service.config_manager.save_raw_configuration.call_args
            assert call_args[0][0] == "Config"  # Config type
            updated_classes = call_args[0][1]["classes"]  # Classes from saved config

            # Should have 1 class (just the new one)
            assert len(updated_classes) == 1
            assert updated_classes[0]["$id"] == "w4"
            assert updated_classes[0]["description"] == "New form"


@pytest.mark.unit
class TestDiscoveryRejectsOpenAI:
    """The discovery guard rejects OpenAI Responses models (PDF document blocks
    are unsupported by the bedrock-mantle Responses API)."""

    def test_reject_helper_raises_for_gpt5(self):
        from idp_common.discovery.classes_discovery import (
            _reject_model_without_document_blocks,
        )

        for model in ("openai.gpt-5.4", "openai.gpt-5.5"):
            with pytest.raises(ValueError, match="not supported for discovery"):
                _reject_model_without_document_blocks(model)

    def test_reject_helper_raises_for_grok(self):
        """xAI Grok reaches Converse but rejects ``document`` blocks outright
        ("This model doesn't support documents"), so discovery must refuse it
        for the same reason it refuses GPT-5.x."""
        from idp_common.discovery.classes_discovery import (
            _reject_model_without_document_blocks,
        )

        for model in ("us.xai.grok-4.6", "global.xai.grok-4.6"):
            with pytest.raises(ValueError, match="not supported for discovery"):
                _reject_model_without_document_blocks(model)

    def test_reject_helper_allows_supported_models(self):
        from idp_common.discovery.classes_discovery import (
            _reject_model_without_document_blocks,
        )

        # Should not raise.
        _reject_model_without_document_blocks("us.anthropic.claude-opus-4-8")
        _reject_model_without_document_blocks("us.amazon.nova-pro-v1:0")
        _reject_model_without_document_blocks(None)


# ---------------------------------------------------------------------------
# Regression: a discovered class id must be usable by every downstream feature.
#
# Discovery's schema prompt asks for "no spaces", but nothing enforced it, and
# the multi-section auto-detect prompt actively suggested a label WITH a space
# ("W2 Form") which was then injected as the class name. The resulting ids
# ("Task cards", "Blank page") persisted fine and only failed much later, in
# BDA sync, where a blueprint name must match [a-zA-Z0-9-_]+.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoveredClassIdNormalization:
    """Tests for class-id normalization on the discovery write path."""

    @pytest.fixture
    def service(self):
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient"),
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            mock_config_reader.return_value.get_merged_configuration.return_value = (
                IDPConfig()
            )
            svc = ClassesDiscovery(
                input_bucket="b", input_prefix="d.pdf", region="us-west-2"
            )
            svc.config_manager = MagicMock()
            svc.config_manager.get_raw_configuration.return_value = {}
            return svc

    def _saved_classes(self, service):
        service.config_manager.save_raw_configuration.assert_called_once()
        return service.config_manager.save_raw_configuration.call_args[0][1]["classes"]

    def _saved_class(self, service):
        classes = self._saved_classes(service)
        assert len(classes) == 1
        return classes[0]

    def test_class_id_with_space_is_normalized_on_save(self, service):
        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {},
            }
        )

        saved = self._saved_class(service)
        assert saved["$id"] == "Task-cards"
        assert saved["x-aws-idp-document-type"] == "Task-cards"
        # The readable original is preserved rather than discarded.
        assert saved["description"] == "Task cards"

    def test_existing_description_is_not_overwritten(self, service):
        service._merge_and_save_class(
            {
                "$id": "Blank page",
                "x-aws-idp-document-type": "Blank page",
                "description": "An intentionally blank separator page",
                "type": "object",
                "properties": {},
            }
        )

        saved = self._saved_class(service)
        assert saved["$id"] == "Blank-page"
        assert saved["description"] == "An intentionally blank separator page"

    def test_valid_class_id_is_left_untouched(self, service):
        """Underscores and hyphens are legal — normalizing them would rename
        classes that already work and orphan their BDA blueprints."""
        service._merge_and_save_class(
            {
                "$id": "Bank_Statement",
                "x-aws-idp-document-type": "Bank_Statement",
                "type": "object",
                "properties": {},
            }
        )

        saved = self._saved_class(service)
        assert saved["$id"] == "Bank_Statement"
        assert saved["x-aws-idp-document-type"] == "Bank_Statement"
        # No description invented for a class that needed no rename.
        assert "description" not in saved

    def test_dedup_uses_the_normalized_id(self, service):
        """Re-discovering the same document must update its class, not add a
        second one under a differently-spelled id."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Task-cards",
                    "x-aws-idp-document-type": "Task-cards",
                    "description": "first pass",
                    "type": "object",
                    "properties": {},
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        )

        saved = self._saved_class(service)
        assert saved["$id"] == "Task-cards"
        assert saved["properties"] == {"a": {"type": "string"}}

    def test_stale_unnormalized_id_is_replaced_not_duplicated(self, service):
        """The upgrade path: a version saved before this fix still holds the
        spaced spelling, and that is exactly the config that hit #624.

        Keying the merge on the raw id would leave 'Task cards' in place and
        add 'Task-cards' beside it — two classes composing the same BDA
        blueprint name prefix, fighting over one blueprint on every sync.
        """
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Task cards",
                    "x-aws-idp-document-type": "Task cards",
                    "description": "saved before normalization",
                    "type": "object",
                    "properties": {},
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        )

        saved = self._saved_class(service)  # asserts exactly one class remains
        assert saved["$id"] == "Task-cards"
        assert saved["properties"] == {"a": {"type": "string"}}

    def test_unrelated_classes_are_not_collapsed_by_normalization(self, service):
        """Only the class being written may be re-keyed.

        Two curated classes can normalize to the same id ('Invoice (Final)'
        and 'Invoice-Final'). Re-keying every existing entry on its sanitized
        id would silently drop one of them while saving an unrelated class.
        """
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {"$id": "Invoice (Final)", "type": "object", "properties": {}},
                {"$id": "Invoice-Final", "type": "object", "properties": {}},
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {},
            }
        )

        saved_ids = [c["$id"] for c in self._saved_classes(service)]
        assert saved_ids == ["Invoice (Final)", "Invoice-Final", "Task-cards"]

    def test_unusable_class_id_is_left_alone_rather_than_invented(self, service):
        """Nothing valid remains in '???'. Inventing a name would present a
        fabricated class as if the model had produced it."""
        service._merge_and_save_class(
            {
                "$id": "???",
                "x-aws-idp-document-type": "???",
                "type": "object",
                "properties": {},
            }
        )

        assert self._saved_class(service)["$id"] == "???"

    def test_auto_detect_prompt_does_not_suggest_labels_with_spaces(self, service):
        """The default auto-detect prompt's own example became the class name,
        so the example itself has to be a valid id."""
        import inspect

        source = inspect.getsource(ClassesDiscovery.auto_detect_sections)
        assert '"W2 Form"' not in source
        assert '"W2-Form"' in source

    def test_class_name_hint_is_sanitized_before_injection(self, service):
        """An auto-detected section label reaches the prompt as an explicit
        instruction that overrides the schema prompt's own 'no spaces' rule."""
        with (
            patch("idp_common.utils.s3util.S3Util.get_bytes", return_value=b"x"),
            patch(
                "idp_common.bedrock.extract_text_from_response",
                return_value=json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "$id": "W2-Form",
                        "x-aws-idp-document-type": "W2-Form",
                        "type": "object",
                        "properties": {"a": {"type": "string"}},
                    }
                ),
            ),
        ):
            service.bedrock_client.invoke_model = MagicMock(
                return_value={
                    "response": {"output": {"message": {"content": [{"text": "{}"}]}}},
                    "metering": {},
                }
            )

            service._extract_data_from_document(
                document_content=b"x",
                file_extension="pdf",
                class_name_hint="W2 Form",
            )

            content = service.bedrock_client.invoke_model.call_args.kwargs["content"]
            prompt = next(part["text"] for part in content if "text" in part)
            assert '"W2-Form" as the document class name' in prompt
            assert "W2 Form" not in prompt


# ---------------------------------------------------------------------------
# Regression (#764): re-running Discovery on a class that already exists must
# not erase the class-level settings an author configured on it.
#
# The write path replaced the class dict wholesale, so x-aws-idp-extraction-model,
# -confidence-threshold, -document-name-regex, -multi-instance, -examples and
# every other class-level key vanished. Discovery reported success and the class
# looked right; the regression surfaced in the NEXT document processed, as a
# different model, a missing escalation, a re-included class or dropped records.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRediscoveryPreservesAuthoredSettings:
    """Tests for the merge (not replace) behavior on the discovery write path."""

    @pytest.fixture
    def service(self):
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient"),
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as mock_config_reader,
            patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            mock_config_reader.return_value.get_merged_configuration.return_value = (
                IDPConfig()
            )
            svc = ClassesDiscovery(
                input_bucket="b", input_prefix="d.pdf", region="us-west-2"
            )
            svc.config_manager = MagicMock()
            svc.config_manager.get_raw_configuration.return_value = {}
            return svc

    def _saved_class(self, service, class_id):
        classes = service.config_manager.save_raw_configuration.call_args[0][1][
            "classes"
        ]
        matching = [c for c in classes if c.get("$id") == class_id]
        assert len(matching) == 1, f"expected exactly one {class_id}, got {classes}"
        return matching[0]

    def test_class_level_settings_survive_rediscovery(self, service):
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Pay-Statement",
                    "x-aws-idp-document-type": "Pay-Statement",
                    "type": "object",
                    "properties": {"EmployeeName": {"type": "string"}},
                    "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
                    "x-aws-idp-extraction-escalation-model": "us.amazon.nova-premier-v1:0",
                    "x-aws-idp-confidence-threshold": 0.95,
                    "x-aws-idp-multi-instance": True,
                    "x-aws-idp-document-name-regex": r"paystub.*\.pdf",
                    "x-aws-idp-exclude-from-processing": False,
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Pay-Statement",
                "x-aws-idp-document-type": "Pay-Statement",
                "type": "object",
                "properties": {
                    "EmployeeName": {"type": "string"},
                    "CheckNumber": {"type": "string"},
                },
            }
        )

        saved = self._saved_class(service, "Pay-Statement")
        # Discovery's contribution is kept...
        assert set(saved["properties"]) == {"EmployeeName", "CheckNumber"}
        # ...and nothing the author set is lost.
        assert saved["x-aws-idp-extraction-model"] == "us.amazon.nova-pro-v1:0"
        assert (
            saved["x-aws-idp-extraction-escalation-model"]
            == "us.amazon.nova-premier-v1:0"
        )
        assert saved["x-aws-idp-confidence-threshold"] == 0.95
        assert saved["x-aws-idp-multi-instance"] is True
        assert saved["x-aws-idp-document-name-regex"] == r"paystub.*\.pdf"
        assert saved["x-aws-idp-exclude-from-processing"] is False

    def test_settings_survive_the_stale_id_rename_too(self, service):
        """The rename path deletes the old entry, so it has to carry its
        settings across first — otherwise normalizing an id doubles as a
        silent reset."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Task cards",
                    "x-aws-idp-document-type": "Task cards",
                    "type": "object",
                    "properties": {},
                    "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
                    "x-aws-idp-multi-instance": True,
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        )

        saved = self._saved_class(service, "Task-cards")
        assert saved["properties"] == {"a": {"type": "string"}}
        assert saved["x-aws-idp-extraction-model"] == "us.amazon.nova-pro-v1:0"
        assert saved["x-aws-idp-multi-instance"] is True

    def test_authored_description_beats_the_one_synthesized_by_the_rename(
        self, service
    ):
        """Renaming stores the original id in ``description`` only as a
        fallback. It must not overwrite a description the author wrote."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Task-cards",
                    "x-aws-idp-document-type": "Task-cards",
                    "description": "Maintenance task cards, one job per card",
                    "type": "object",
                    "properties": {},
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {},
            }
        )

        saved = self._saved_class(service, "Task-cards")
        assert saved["description"] == "Maintenance task cards, one job per card"

    def test_a_brand_new_class_is_unaffected(self, service):
        """No existing class means nothing to carry: the discovered class is
        saved exactly as produced (after id normalization)."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [{"$id": "Invoice", "type": "object", "properties": {}}]
        }

        service._merge_and_save_class(
            {
                "$id": "Pay-Statement",
                "x-aws-idp-document-type": "Pay-Statement",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        )

        saved = self._saved_class(service, "Pay-Statement")
        assert saved == {
            "$id": "Pay-Statement",
            "x-aws-idp-document-type": "Pay-Statement",
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }

    def test_settings_are_not_leaked_from_an_unrelated_class(self, service):
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Invoice",
                    "type": "object",
                    "properties": {},
                    "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Pay-Statement",
                "x-aws-idp-document-type": "Pay-Statement",
                "type": "object",
                "properties": {},
            }
        )

        assert "x-aws-idp-extraction-model" not in self._saved_class(
            service, "Pay-Statement"
        )
        assert (
            self._saved_class(service, "Invoice")["x-aws-idp-extraction-model"]
            == "us.amazon.nova-pro-v1:0"
        )

    def test_a_dangling_instance_array_does_not_break_the_save(self, service):
        """A re-discovered class whose instance-array property is gone must still
        save. Carrying the pointer would fail IDPConfig validation inside
        save_raw_configuration, losing every class in the run instead of one
        setting — and a shipped preset (ocr-benchmark/BANK_CHECK) sets it."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "BANK_CHECK",
                    "x-aws-idp-document-type": "BANK_CHECK",
                    "x-aws-idp-instance-array": "checks",
                    "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
                    "type": "object",
                    "properties": {
                        "checks": {"type": "array", "items": {"type": "object"}}
                    },
                }
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "BANK_CHECK",
                "x-aws-idp-document-type": "BANK_CHECK",
                "type": "object",
                "properties": {"AccountNumber": {"type": "string"}},
            }
        )

        saved = self._saved_class(service, "BANK_CHECK")
        assert "x-aws-idp-instance-array" not in saved
        # The settings that are NOT coupled to properties still survive.
        assert saved["x-aws-idp-extraction-model"] == "us.amazon.nova-pro-v1:0"
        from idp_common.config.models import IDPConfig

        IDPConfig(**{"classes": [saved]})  # the save path does this; must not raise

    def test_two_stale_spellings_pick_a_deterministic_settings_source(self, service):
        """Both normalize to the same id, so both are removed (pre-existing), but
        only one can supply the settings. Sorted, so the choice does not depend on
        DynamoDB's ordering, and the other is named in a warning rather than
        dropped in silence."""
        service.config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Task.cards",
                    "type": "object",
                    "properties": {},
                    "x-aws-idp-confidence-threshold": 0.42,
                },
                {
                    "$id": "Task cards",
                    "type": "object",
                    "properties": {},
                    "x-aws-idp-confidence-threshold": 0.99,
                },
            ]
        }

        service._merge_and_save_class(
            {
                "$id": "Task cards",
                "x-aws-idp-document-type": "Task cards",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        )

        saved = self._saved_class(service, "Task-cards")
        # sorted(["Task cards", "Task.cards"]) -> "Task cards" wins, every time.
        assert saved["x-aws-idp-confidence-threshold"] == 0.99
