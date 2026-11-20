# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for batch processor module
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_sdk._core.batch_processor import BatchProcessor


class TestBatchProcessor:
    """Test batch processing functionality"""

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_init_success(self, mock_resource, mock_client, mock_stack_info_class):
        """Test successful initialization"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        processor = BatchProcessor("test-stack")

        assert processor.stack_name == "test-stack"
        assert processor.resources["InputBucket"] == "input-bucket"

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_init_invalid_stack(
        self, mock_resource, mock_client, mock_stack_info_class
    ):
        """Test initialization with invalid stack"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = False
        mock_stack_info_class.return_value = mock_stack_info

        with pytest.raises(ValueError, match="not in a valid state"):
            BatchProcessor("test-stack")

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("idp_sdk._core.batch_processor.parse_manifest")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_process_batch(
        self, mock_resource, mock_client, mock_parse_manifest, mock_stack_info_class
    ):
        """Test batch processing"""
        # Setup mocks
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        # Mock manifest documents - new format with full S3 URI
        mock_parse_manifest.return_value = [
            {
                "path": "s3://input-bucket/doc1.pdf",
                "type": "s3",
                "filename": "doc1.pdf",
                "baseline_source": None,
            }
        ]

        # Mock S3 client
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1024}  # Validate exists

        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        result = processor.process_batch(
            manifest_path="test.csv", output_prefix="test-batch"
        )

        # Verify results
        assert "batch_id" in result
        assert len(result["document_ids"]) == 1
        # Document ID now includes batch prefix for organization
        assert "doc1.pdf" in result["document_ids"][0]  # S3 key includes batch prefix
        assert result["document_ids"][0].endswith("doc1.pdf")
        assert result["queued"] == 1
        assert result["failed"] == 0

        # Note: S3 copy_object is called for S3 URIs (not head_object)

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_upload_local_file(self, mock_resource, mock_client, mock_stack_info_class):
        """Test local file upload with path preservation"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        mock_s3 = MagicMock()
        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        doc = {
            "document_id": "doc1",
            "path": "/tmp/test.pdf",  # nosec B108 - mock test data, no file created
            "filename": "test.pdf",
            "type": "local",
        }

        # Use the replacement method _upload_local_file_with_path
        s3_key = processor._upload_local_file_with_path(doc, "batch-123")

        # Verify upload was called
        mock_s3.upload_file.assert_called_once()
        assert "batch-123" in s3_key
        assert "test.pdf" in s3_key

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_validate_s3_key_exists(
        self, mock_resource, mock_client, mock_stack_info_class
    ):
        """Test S3 key validation when file exists"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1024}
        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        # Should not raise exception
        processor._validate_s3_key("doc1.pdf")
        mock_s3.head_object.assert_called_once()

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_validate_s3_key_not_found(
        self, mock_resource, mock_client, mock_stack_info_class
    ):
        """Test S3 key validation when file doesn't exist"""
        from botocore.exceptions import ClientError

        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        mock_s3 = MagicMock()
        # Create proper ClientError
        error = ClientError(
            error_response={"Error": {"Code": "404", "Message": "Not Found"}},
            operation_name="HeadObject",
        )
        mock_s3.head_object.side_effect = error
        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        with pytest.raises(ValueError, match="Document not found"):
            processor._validate_s3_key("nonexistent.pdf")

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_generate_batch_id(self, mock_resource, mock_client, mock_stack_info_class):
        """Test batch ID generation"""
        import re

        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        processor = BatchProcessor("test-stack")

        # No sleep: the random suffix keeps two ids from the same second apart.
        batch_id1 = processor._generate_batch_id("test-prefix")
        batch_id2 = processor._generate_batch_id("test-prefix")
        assert batch_id1 != batch_id2

        # Format: prefix-YYYYMMDD-HHMMSS-<8 hex>; the prefix may itself hold hyphens
        assert re.fullmatch(r"test-prefix-\d{8}-\d{6}-[0-9a-f]{8}", batch_id1)

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_store_and_retrieve_batch_metadata(
        self, mock_resource, mock_client, mock_stack_info_class
    ):
        """Test storing and retrieving batch metadata"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        mock_s3 = MagicMock()

        # Mock get_object to return stored metadata
        stored_metadata = {
            "batch_id": "test-batch-123",
            "document_ids": ["doc1", "doc2"],
            "queued": 2,
        }
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(stored_metadata).encode())
        }

        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        # Store metadata
        processor._store_batch_metadata("test-batch-123", stored_metadata)

        # Verify put_object was called
        mock_s3.put_object.assert_called_once()

        # Retrieve metadata
        retrieved = processor.get_batch_info("test-batch-123")

        assert retrieved == stored_metadata

    @patch("idp_sdk._core.batch_processor.StackInfo")
    @patch("boto3.client")
    @patch("boto3.resource")
    def test_get_batch_info_not_found(
        self, mock_resource, mock_client, mock_stack_info_class
    ):
        """Test retrieving non-existent batch metadata"""
        mock_stack_info = MagicMock()
        mock_stack_info.validate_stack.return_value = True
        mock_stack_info.get_resources.return_value = {
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        mock_stack_info_class.return_value = mock_stack_info

        mock_s3 = MagicMock()
        mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        mock_s3.get_object.side_effect = mock_s3.exceptions.NoSuchKey()
        mock_client.return_value = mock_s3

        processor = BatchProcessor("test-stack")

        result = processor.get_batch_info("nonexistent-batch")

        assert result is None
