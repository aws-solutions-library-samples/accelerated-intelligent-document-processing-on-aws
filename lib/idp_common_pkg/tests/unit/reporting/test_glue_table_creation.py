# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the Glue table creation feature in SaveReportingData class.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from idp_common.models import Document, Section
from idp_common.reporting.save_reporting_data import SaveReportingData


@pytest.mark.unit
class TestGlueTableCreation:
    """Test cases for Glue table creation functionality."""

    @pytest.fixture
    def mock_glue_client(self):
        """Create a mock Glue client."""
        with patch("boto3.client") as mock_client:
            mock_glue = MagicMock()

            def client_factory(service_name, *args, **kwargs):
                if service_name == "glue":
                    return mock_glue
                return MagicMock()

            mock_client.side_effect = client_factory
            yield mock_glue

    @pytest.fixture
    def reporter_with_database(self, mock_glue_client):
        """Create a SaveReportingData instance with database name."""
        return SaveReportingData("test-bucket", database_name="test_database")

    @pytest.fixture
    def document_with_new_sections(self):
        """Create a test document with sections of new types."""
        sections = [
            Section(
                section_id="section_1",
                classification="new_invoice_type",
                confidence=0.95,
                page_ids=["page_1"],
                extraction_result_uri="s3://test-bucket/doc1/sections/section_1/result.json",
            ),
            Section(
                section_id="section_2",
                classification="new_receipt_type",
                confidence=0.87,
                page_ids=["page_2"],
                extraction_result_uri="s3://test-bucket/doc1/sections/section_2/result.json",
            ),
            Section(
                section_id="section_3",
                classification="new_invoice_type",  # Duplicate type
                confidence=0.92,
                page_ids=["page_3"],
                extraction_result_uri="s3://test-bucket/doc1/sections/section_3/result.json",
            ),
        ]

        doc = Document(
            id="test_document_glue",
            input_key="documents/test_document_glue.pdf",
            initial_event_time=datetime.now().isoformat() + "Z",
            sections=sections,
            num_pages=3,
        )
        return doc

    def test_convert_schema_to_glue_columns(self, reporter_with_database):
        """Test conversion of PyArrow schema to Glue columns."""

        # Create a schema with various types
        schema = pa.schema(
            [
                pa.field("string_field", pa.string()),
                pa.field("int32_field", pa.int32()),
                pa.field("int64_field", pa.int64()),
                pa.field("float32_field", pa.float32()),
                pa.field("float64_field", pa.float64()),
                pa.field("bool_field", pa.bool_()),
                pa.field("timestamp_field", pa.timestamp("ms")),
            ]
        )

        # Convert to Glue columns
        columns = reporter_with_database._convert_schema_to_glue_columns(schema)

        # Create a dict for easier testing
        columns_dict = {col["Name"]: col["Type"] for col in columns}

        # Test mappings
        assert columns_dict["string_field"] == "string"
        assert columns_dict["int32_field"] == "int"
        assert columns_dict["int64_field"] == "bigint"
        assert columns_dict["float32_field"] == "float"
        assert columns_dict["float64_field"] == "double"
        assert columns_dict["bool_field"] == "boolean"
        assert columns_dict["timestamp_field"] == "timestamp"

    def test_create_or_update_glue_table_new_table(
        self, reporter_with_database, mock_glue_client
    ):
        """Test creating a new Glue table when it doesn't exist."""
        # Setup mock to simulate table not existing
        mock_glue_client.get_table.side_effect = Exception("EntityNotFoundException")

        # Create a simple schema
        schema = pa.schema(
            [
                pa.field("document_id", pa.string()),
                pa.field("section_id", pa.string()),
                pa.field("amount", pa.float64()),
                pa.field("is_valid", pa.bool_()),
                pa.field("timestamp", pa.timestamp("ms")),  # Use ms instead of ns
            ]
        )

        # Call the method
        reporter_with_database._create_or_update_glue_table("test_section", schema)

        # Verify get_table was called to check existence
        mock_glue_client.get_table.assert_called_once_with(
            DatabaseName="test_database", Name="document_sections_test_section"
        )

        # Verify create_table was called with correct parameters
        mock_glue_client.create_table.assert_called_once()
        call_args = mock_glue_client.create_table.call_args[1]

        assert call_args["DatabaseName"] == "test_database"
        assert call_args["TableInput"]["Name"] == "document_sections_test_section"
        assert call_args["TableInput"]["TableType"] == "EXTERNAL_TABLE"
        assert call_args["TableInput"]["PartitionKeys"] == [
            {"Name": "date", "Type": "string"}
        ]

        # Verify columns
        columns = call_args["TableInput"]["StorageDescriptor"]["Columns"]
        assert len(columns) == 5
        assert {"Name": "document_id", "Type": "string"} in columns
        assert {"Name": "section_id", "Type": "string"} in columns
        assert {"Name": "amount", "Type": "double"} in columns
        assert {"Name": "is_valid", "Type": "boolean"} in columns
        assert {"Name": "timestamp", "Type": "timestamp"} in columns

        # Verify storage descriptor settings
        storage = call_args["TableInput"]["StorageDescriptor"]
        assert storage["Location"] == "s3://test-bucket/document_sections/test_section/"
        assert (
            storage["InputFormat"]
            == "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
        )
        assert (
            storage["OutputFormat"]
            == "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
        )

    def test_create_or_update_glue_table_existing_table(
        self, reporter_with_database, mock_glue_client
    ):
        """Test updating an existing Glue table."""
        # Setup mock to simulate table already exists
        mock_glue_client.get_table.return_value = {
            "Table": {
                "Name": "document_sections_test_section",
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "document_id", "Type": "string"},
                        {"Name": "section_id", "Type": "string"},
                    ]
                },
            }
        }

        # Create a schema with additional columns
        schema = pa.schema(
            [
                pa.field("document_id", pa.string()),
                pa.field("section_id", pa.string()),
                pa.field("new_field", pa.string()),  # New field
            ]
        )

        # Call the method
        reporter_with_database._create_or_update_glue_table("test_section", schema)

        # Verify get_table was called
        mock_glue_client.get_table.assert_called_once()

        # Verify update_table was called
        mock_glue_client.update_table.assert_called_once()
        call_args = mock_glue_client.update_table.call_args[1]

        assert call_args["DatabaseName"] == "test_database"
        assert call_args["TableInput"]["Name"] == "document_sections_test_section"

        # Verify the new column was added
        columns = call_args["TableInput"]["StorageDescriptor"]["Columns"]
        assert {"Name": "new_field", "Type": "string"} in columns

    def test_create_or_update_glue_table_error_handling(
        self, reporter_with_database, mock_glue_client
    ):
        """Test error handling in Glue table creation."""
        # Setup mock to raise an unexpected error (not EntityNotFoundException)
        mock_glue_client.get_table.side_effect = Exception("Unexpected error")

        schema = pa.schema([pa.field("test", pa.string())])

        # Should log error but not raise exception
        result = reporter_with_database._create_or_update_glue_table(
            "test_section", schema
        )

        # Should return False since it couldn't determine if table exists
        assert not result

        # Verify it did NOT attempt to create the table (since error wasn't EntityNotFoundException)
        mock_glue_client.create_table.assert_not_called()

    @patch("idp_common.reporting.save_reporting_data.get_json_content")
    def test_save_document_sections_creates_glue_tables(
        self,
        mock_get_json,
        reporter_with_database,
        mock_glue_client,
        document_with_new_sections,
    ):
        """Test that save_document_sections creates Glue tables for new section types."""
        # Mock extraction data
        mock_get_json.return_value = {
            "invoice_number": "INV-001",
            "amount": 100.50,
            "is_paid": True,
        }

        # Setup mock Glue client to simulate tables don't exist
        mock_glue_client.get_table.side_effect = Exception("EntityNotFoundException")

        # Mock S3 client
        with patch("boto3.client") as mock_client:
            mock_s3 = MagicMock()

            def client_factory(service_name, *args, **kwargs):
                if service_name == "glue":
                    return mock_glue_client
                elif service_name == "s3":
                    return mock_s3
                return MagicMock()

            mock_client.side_effect = client_factory

            # Call save_document_sections
            result = reporter_with_database.save_document_sections(
                document_with_new_sections
            )

        # Verify successful processing
        assert result["statusCode"] == 200
        assert "Successfully saved 3 document sections" in result["body"]

        # Verify Glue tables were created for unique section types (2 unique types)
        assert mock_glue_client.create_table.call_count == 2

        # Get the table names that were created
        created_tables = []
        for call in mock_glue_client.create_table.call_args_list:
            table_name = call[1]["TableInput"]["Name"]
            created_tables.append(table_name)

        # Verify correct tables were created
        assert "document_sections_new_invoice_type" in created_tables
        assert "document_sections_new_receipt_type" in created_tables

    @patch("idp_common.reporting.save_reporting_data.get_json_content")
    def test_save_document_sections_without_database_name(
        self, mock_get_json, mock_glue_client, document_with_new_sections
    ):
        """Test that save_document_sections works without database name (no Glue table creation)."""
        # Create reporter without database name
        reporter = SaveReportingData("test-bucket")  # No database_name parameter

        # Mock extraction data
        mock_get_json.return_value = {"test": "data"}

        # Mock S3 client
        with patch("boto3.client") as mock_client:
            mock_s3 = MagicMock()
            mock_client.return_value = mock_s3

            # Call save_document_sections
            result = reporter.save_document_sections(document_with_new_sections)

        # Verify successful processing
        assert result["statusCode"] == 200

        # Verify no Glue operations were attempted
        mock_glue_client.get_table.assert_not_called()
        mock_glue_client.create_table.assert_not_called()
        mock_glue_client.update_table.assert_not_called()

    def test_create_or_update_glue_table_with_already_exists_exception(
        self, reporter_with_database, mock_glue_client
    ):
        """Test handling of AlreadyExistsException when creating a table."""
        # Setup mock to simulate table doesn't exist on get, but exists on create (race condition)
        mock_glue_client.get_table.side_effect = Exception("EntityNotFoundException")

        # Import the specific exception type
        from botocore.exceptions import ClientError

        already_exists_error = ClientError(
            {"Error": {"Code": "AlreadyExistsException"}}, "CreateTable"
        )
        mock_glue_client.create_table.side_effect = already_exists_error

        schema = pa.schema([pa.field("test", pa.string())])

        # Should handle gracefully and not raise exception
        reporter_with_database._create_or_update_glue_table("test_section", schema)

        # Verify it attempted to create the table
        mock_glue_client.create_table.assert_called_once()

    def test_uppercase_section_types_converted_to_lowercase(
        self, reporter_with_database, mock_glue_client
    ):
        """Test that uppercase section types like 'W2' are converted to lowercase for both Glue table and S3 paths."""
        # Test with uppercase section type (like 'W2')
        mock_glue_client.get_table.side_effect = Exception("EntityNotFoundException")

        schema = pa.schema(
            [
                pa.field("document_id", pa.string()),
                pa.field("section_id", pa.string()),
            ]
        )

        # Call with uppercase section type
        reporter_with_database._create_or_update_glue_table("W2", schema)

        # Verify the table was created with lowercase name
        mock_glue_client.create_table.assert_called_once()
        call_args = mock_glue_client.create_table.call_args[1]

        # Check table name is lowercase
        assert call_args["TableInput"]["Name"] == "document_sections_w2"

        # Check S3 location is lowercase
        assert (
            call_args["TableInput"]["StorageDescriptor"]["Location"]
            == "s3://test-bucket/document_sections/w2/"
        )

        # Check partition projection template also uses lowercase
        assert (
            call_args["TableInput"]["Parameters"]["storage.location.template"]
            == "s3://test-bucket/document_sections/w2/date=${date}/"
        )

    @patch("idp_common.reporting.save_reporting_data.get_json_content")
    @patch("boto3.client")
    def test_save_document_sections_uppercase_section_type(
        self, mock_boto_client, mock_get_json, reporter_with_database, mock_glue_client
    ):
        """Test that save_document_sections correctly handles uppercase section types like 'W2'."""
        # Create a document with uppercase section type
        sections = [
            Section(
                section_id="section_1",
                classification="W2",  # Uppercase classification
                confidence=0.95,
                page_ids=["page_1"],
                extraction_result_uri="s3://test-bucket/doc1/sections/section_1/result.json",
            ),
        ]

        doc = Document(
            id="test_w2_document",
            input_key="documents/test_w2.pdf",
            initial_event_time=datetime.now().isoformat() + "Z",
            sections=sections,
            num_pages=1,
        )

        # Mock extraction data
        mock_get_json.return_value = {"field1": "value1", "field2": "value2"}

        # Setup mock Glue client
        mock_glue_client.get_table.side_effect = Exception("EntityNotFoundException")

        # Mock S3 client
        mock_s3 = MagicMock()

        def client_factory(service_name, *args, **kwargs):
            if service_name == "glue":
                return mock_glue_client
            elif service_name == "s3":
                return mock_s3
            return MagicMock()

        mock_boto_client.side_effect = client_factory

        # Create a new reporter instance that will use our mocked clients
        reporter = SaveReportingData("test-bucket", database_name="test_database")

        # Call save_document_sections
        result = reporter.save_document_sections(doc)

        # Verify successful processing
        assert result["statusCode"] == 200

        # Verify S3 put_object was called with lowercase path
        mock_s3.put_object.assert_called_once()
        s3_call_args = mock_s3.put_object.call_args[1]
        s3_key = s3_call_args["Key"]

        # Check that the S3 key uses lowercase 'w2' not 'W2'
        assert "/w2/" in s3_key
        assert "/W2/" not in s3_key
        assert s3_key.startswith("document_sections/w2/date=")
