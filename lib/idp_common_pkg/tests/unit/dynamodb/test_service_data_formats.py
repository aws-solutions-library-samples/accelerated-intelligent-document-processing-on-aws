# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test suite for DynamoDB service data format handling.

This test focuses on reproducing and fixing issues with inconsistent data formats
between JSON strings and native DynamoDB objects, particularly for the metering field
and other potentially affected fields.
"""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Status


class TestDynamoDBServiceDataFormats:
    """Test suite for DynamoDB data format conversion issues."""

    def setup_method(self):
        """Set up test fixtures."""
        # Mock the DynamoDBClient
        self.mock_client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def create_base_dynamodb_item(self):
        """Create a base DynamoDB item structure for testing."""
        return {
            "PK": "doc#lending_package.pdf",
            "SK": "none",
            "ObjectKey": "lending_package.pdf",
            "ObjectStatus": "COMPLETED",
            "PageCount": 6,
            "QueuedTime": "2025-09-25T14:14:54.819Z",
            "WorkflowStartTime": "2025-09-25T14:15:00.000Z",
            "CompletionTime": "2025-09-25T14:16:30.000Z",
            "Pages": [
                {
                    "Id": 1,
                    "Class": "W2",
                    "ImageUri": "s3://bucket/page1.jpg",
                    "TextUri": "s3://bucket/page1.json",
                },
                {
                    "Id": 2,
                    "Class": "Bank Statement",
                    "ImageUri": "s3://bucket/page2.jpg",
                    "TextUri": "s3://bucket/page2.json",
                },
            ],
            "Sections": [
                {
                    "Id": "1",
                    "Class": "W2",
                    "PageIds": [1],
                    "OutputJSONUri": "s3://bucket/section1.json",
                }
            ],
        }

    def test_metering_as_native_dict_works(self):
        """Test that metering as native dict now works correctly (bug was fixed)."""
        # The code now handles both dict and JSON string formats
        mock_item = self.create_base_dynamodb_item()
        mock_item["Metering"] = {
            "tokens_used": 150,
            "processing_cost": Decimal("0.05"),
            "model_calls": 3,
        }

        # This should work now without raising an error
        document = self.service._dynamodb_item_to_document(mock_item)

        # Verify the metering data was properly loaded
        assert document.metering is not None
        assert "tokens_used" in document.metering

    def test_metering_as_json_string_works(self):
        """Test that metering as JSON string works correctly."""
        mock_item = self.create_base_dynamodb_item()
        metering_data = {"tokens_used": 150, "processing_cost": 0.05, "model_calls": 3}
        mock_item["Metering"] = json.dumps(metering_data)

        # This should work without errors
        document = self.service._dynamodb_item_to_document(mock_item)

        assert document.metering == metering_data
        assert document.metering["tokens_used"] == 150
        assert document.metering["processing_cost"] == 0.05

    def test_metering_null_or_missing(self):
        """Test handling of null or missing metering data."""
        mock_item = self.create_base_dynamodb_item()

        # Test with None
        mock_item["Metering"] = None
        document = self.service._dynamodb_item_to_document(mock_item)
        assert document.metering == {}

        # Test with missing field
        del mock_item["Metering"]
        document = self.service._dynamodb_item_to_document(mock_item)
        assert document.metering == {}

    def test_metering_empty_string(self):
        """Test handling of empty string metering data."""
        mock_item = self.create_base_dynamodb_item()
        mock_item["Metering"] = ""

        document = self.service._dynamodb_item_to_document(mock_item)
        assert document.metering == {}

    def test_metering_malformed_json_string(self):
        """Test handling of malformed JSON string in metering."""
        mock_item = self.create_base_dynamodb_item()
        mock_item["Metering"] = '{"invalid": json, "missing": quote}'

        # Should handle gracefully without crashing
        document = self.service._dynamodb_item_to_document(mock_item)
        # Should fall back to empty dict or log warning
        assert document.metering == {}

    def test_complete_document_conversion_with_mixed_formats(self):
        """Test complete document conversion with various data formats."""
        mock_item = self.create_base_dynamodb_item()

        # Add metering as native dict (the problematic case)
        mock_item["Metering"] = {
            "total_tokens": 500,
            "cost_usd": Decimal("0.15"),
            "processing_steps": ["ocr", "classification", "extraction"],
        }

        # Add sections with confidence threshold alerts
        mock_item["Sections"][0]["ConfidenceThresholdAlerts"] = [
            {
                "attributeName": "account_number",
                "confidence": Decimal("0.85"),
                "confidenceThreshold": Decimal("0.90"),
            }
        ]

        # This should fail with current implementation, but pass after fix
        try:
            document = self.service._dynamodb_item_to_document(mock_item)
            # If we get here, the fix is working
            assert document.input_key == "lending_package.pdf"
            assert document.status == Status.COMPLETED
            assert document.num_pages == 6
            assert len(document.pages) == 2
            assert len(document.sections) == 1
            assert document.metering["total_tokens"] == 500
        except TypeError as e:
            if "JSON object must be str, bytes or bytearray, not dict" in str(e):
                pytest.fail(
                    "The fix for metering data format handling is not yet implemented"
                )
            else:
                raise

    # A `test_identify_all_json_parsing_fields` used to sit here: a one-row
    # table of (field, value) pairs, looped over, with a `print` in every arm
    # and no assertion anywhere, plus a comment inviting the next reader to add
    # rows. It could not fail, and it was not the cover for its own subject
    # either -- removing the native-dict branch from
    # `_dynamodb_item_to_document` reddens five tests in this file, including
    # `test_metering_as_native_dict_works` and the `{"tokens": 100}` id of
    # `test_metering_format_variations`, which assert exactly the case its one
    # row exercised. It was deleted rather than repaired because repairing it
    # would have produced a sixth copy of an already-asserted case (#1129).

    def test_decimal_handling_in_native_objects(self):
        """Test that Decimal values in native objects are handled correctly."""
        mock_item = self.create_base_dynamodb_item()
        mock_item["Metering"] = {
            "cost": Decimal("12.34"),
            "confidence": Decimal("0.95"),
            "nested": {"price": Decimal("5.67")},
        }

        # After fix, this should work
        try:
            document = self.service._dynamodb_item_to_document(mock_item)
            # Verify Decimal values are preserved or converted appropriately
            assert isinstance(document.metering["cost"], (Decimal, float))
        except TypeError:
            pytest.fail("Fix needed: Should handle Decimal values in native objects")

    @pytest.mark.parametrize(
        "metering_value,expected_result",
        [
            ('{"tokens": 100}', {"tokens": 100}),  # JSON string
            ({"tokens": 100}, {"tokens": 100}),  # Native dict
            ("", {}),  # Empty string
            (None, {}),  # None value
            ("invalid json", {}),  # Invalid JSON
        ],
    )
    def test_metering_format_variations(self, metering_value, expected_result):
        """Parameterized test for various metering data formats."""
        mock_item = self.create_base_dynamodb_item()
        if metering_value is not None:
            mock_item["Metering"] = metering_value
        else:
            # Test missing field
            mock_item.pop("Metering", None)

        # This test will fail until we implement the fix
        try:
            document = self.service._dynamodb_item_to_document(mock_item)
            assert document.metering == expected_result
        except TypeError as e:
            if "JSON object must be str, bytes or bytearray, not dict" in str(e):
                if isinstance(metering_value, dict):
                    pytest.fail(f"Fix needed for native dict format: {metering_value}")
            raise


class TestDynamoDBServiceDataFormatsFixed:
    """
    Test suite that should pass AFTER implementing the fix.
    These tests validate the robust data format handling.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def create_base_item(self):
        """Create base test item."""
        return {
            "PK": "doc#test.pdf",
            "SK": "none",
            "ObjectKey": "test.pdf",
            "ObjectStatus": "COMPLETED",
            "PageCount": 1,
        }

    def test_robust_metering_handling_after_fix(self):
        """Test that the fixed implementation handles all metering formats robustly."""
        base_item = self.create_base_item()

        test_cases = [
            # (input_value, expected_output, description)
            ('{"tokens": 100}', {"tokens": 100}, "JSON string"),
            ({"tokens": 100}, {"tokens": 100}, "Native dict"),
            ({"cost": Decimal("1.23")}, {"cost": Decimal("1.23")}, "Dict with Decimal"),
            ("", {}, "Empty string"),
            (None, {}, "None value"),
            ("invalid", {}, "Invalid JSON"),
        ]

        for input_val, expected, desc in test_cases:
            item = base_item.copy()
            if input_val is not None:
                item["Metering"] = input_val

            document = self.service._dynamodb_item_to_document(item)
            assert document.metering == expected, f"Failed for {desc}: {input_val}"
