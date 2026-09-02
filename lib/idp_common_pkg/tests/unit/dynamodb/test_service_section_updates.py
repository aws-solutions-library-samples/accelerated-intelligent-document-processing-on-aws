# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for DynamoDB service section-level update methods.

Tests for the new lightweight update methods:
- update_document_status: Minimal status-only updates
- update_document_section: Atomic section-level updates
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import ProcessingIssue, Section, Status


@pytest.mark.unit
class TestDocumentDynamoDBServiceStatusUpdates:
    """Tests for the update_document_status method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def test_update_document_status_running(self):
        """Test lightweight status update with RUNNING status (EXTRACTING)."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "PK": "doc#test-document.pdf",
                "SK": "none",
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "EXTRACTING",
                "WorkflowStatus": "RUNNING",
            }
        }

        # Test
        result = self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.EXTRACTING,
        )

        # Verify
        self.mock_client.update_item.assert_called_once()
        call_kwargs = self.mock_client.update_item.call_args[1]

        # Check key
        assert call_kwargs["key"]["PK"] == "doc#test-document.pdf"
        assert call_kwargs["key"]["SK"] == "none"

        # Check expression values
        assert (
            call_kwargs["expression_attribute_values"][":ObjectStatus"] == "EXTRACTING"
        )
        assert (
            call_kwargs["expression_attribute_values"][":WorkflowStatus"] == "RUNNING"
        )

        # Check no WorkflowExecutionArn in expression
        assert ":WorkflowExecutionArn" not in call_kwargs["expression_attribute_values"]

        # Check result
        assert result["ObjectStatus"] == "EXTRACTING"

    def test_update_document_status_completed(self):
        """Test lightweight status update with COMPLETED status."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "COMPLETED",
                "WorkflowStatus": "SUCCEEDED",
            }
        }

        # Test
        self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.COMPLETED,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        assert (
            call_kwargs["expression_attribute_values"][":ObjectStatus"] == "COMPLETED"
        )
        assert (
            call_kwargs["expression_attribute_values"][":WorkflowStatus"] == "SUCCEEDED"
        )

    def test_update_document_status_failed(self):
        """Test lightweight status update with FAILED status."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "FAILED",
                "WorkflowStatus": "FAILED",
            }
        }

        # Test
        self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.FAILED,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        assert call_kwargs["expression_attribute_values"][":ObjectStatus"] == "FAILED"
        assert call_kwargs["expression_attribute_values"][":WorkflowStatus"] == "FAILED"

    def test_update_document_status_aborted(self):
        """Test lightweight status update with ABORTED status."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "ABORTED",
                "WorkflowStatus": "ABORTED",
            }
        }

        # Test
        self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.ABORTED,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        assert call_kwargs["expression_attribute_values"][":ObjectStatus"] == "ABORTED"
        assert (
            call_kwargs["expression_attribute_values"][":WorkflowStatus"] == "ABORTED"
        )

    def test_update_document_status_with_workflow_arn(self):
        """Test lightweight status update with workflow execution ARN."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "EXTRACTING",
                "WorkflowStatus": "RUNNING",
                "WorkflowExecutionArn": "arn:aws:states:us-west-2:123456789012:execution:workflow:test",
            }
        }

        # Test
        self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.EXTRACTING,
            workflow_execution_arn="arn:aws:states:us-west-2:123456789012:execution:workflow:test",
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]

        # Check WorkflowExecutionArn is included
        assert (
            call_kwargs["expression_attribute_values"][":WorkflowExecutionArn"]
            == "arn:aws:states:us-west-2:123456789012:execution:workflow:test"
        )
        assert "#WorkflowExecutionArn" in call_kwargs["expression_attribute_names"]

        # Check update expression includes WorkflowExecutionArn
        assert (
            "#WorkflowExecutionArn = :WorkflowExecutionArn"
            in call_kwargs["update_expression"]
        )

    def test_update_document_status_assessing(self):
        """Test lightweight status update with ASSESSING status."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "ASSESSING",
                "WorkflowStatus": "RUNNING",
            }
        }

        # Test
        self.service.update_document_status(
            document_id="test-document.pdf",
            status=Status.ASSESSING,
        )

        # Verify - ASSESSING is not a terminal status, so WorkflowStatus should be RUNNING
        call_kwargs = self.mock_client.update_item.call_args[1]
        assert (
            call_kwargs["expression_attribute_values"][":ObjectStatus"] == "ASSESSING"
        )
        assert (
            call_kwargs["expression_attribute_values"][":WorkflowStatus"] == "RUNNING"
        )


@pytest.mark.unit
class TestDocumentDynamoDBServiceSectionUpdates:
    """Tests for the update_document_section method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def test_update_document_section_basic(self):
        """Test atomic section-level update with basic section data."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "ObjectStatus": "EXTRACTING",
                "Sections": [
                    {
                        "Id": "section-1",
                        "PageIds": [1, 2],
                        "Class": "Invoice",
                        "OutputJSONUri": "s3://bucket/sections/section-1/result.json",
                    }
                ],
            }
        }

        # Create test section
        section = Section(
            section_id="section-1",
            classification="Invoice",
            page_ids=["1", "2"],
            extraction_result_uri="s3://bucket/sections/section-1/result.json",
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=0,
            section=section,
        )

        # Verify
        self.mock_client.update_item.assert_called_once()
        call_kwargs = self.mock_client.update_item.call_args[1]

        # Check key
        assert call_kwargs["key"]["PK"] == "doc#test-document.pdf"
        assert call_kwargs["key"]["SK"] == "none"

        # Check update expression uses index-based update
        assert "SET #Sections[0] = :section" in call_kwargs["update_expression"]

        # Check section data
        section_data = call_kwargs["expression_attribute_values"][":section"]
        assert section_data["Id"] == "section-1"
        assert section_data["Class"] == "Invoice"
        assert section_data["PageIds"] == [1, 2]
        assert (
            section_data["OutputJSONUri"]
            == "s3://bucket/sections/section-1/result.json"
        )

    def test_update_document_section_different_index(self):
        """Test section update at different array index."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section
        section = Section(
            section_id="section-3",
            classification="W2",
            page_ids=["5", "6", "7"],
            extraction_result_uri="s3://bucket/sections/section-3/result.json",
        )

        # Test with index 2
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=2,
            section=section,
        )

        # Verify update expression uses correct index
        call_kwargs = self.mock_client.update_item.call_args[1]
        assert "SET #Sections[2] = :section" in call_kwargs["update_expression"]

        # Check page IDs converted to integers
        section_data = call_kwargs["expression_attribute_values"][":section"]
        assert section_data["PageIds"] == [5, 6, 7]

    def test_update_document_section_with_confidence_alerts(self):
        """Test section update with confidence threshold alerts."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section with alerts
        section = Section(
            section_id="section-2",
            classification="W2",
            page_ids=["3"],
            extraction_result_uri="s3://bucket/sections/section-2/result.json",
            confidence_threshold_alerts=[
                {
                    "attribute_name": "employer_name",
                    "confidence": 0.75,
                    "confidence_threshold": 0.85,
                },
                {
                    "attribute_name": "wages",
                    "confidence": 0.60,
                    "confidence_threshold": 0.80,
                },
            ],
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=1,
            section=section,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        section_data = call_kwargs["expression_attribute_values"][":section"]

        assert "ConfidenceThresholdAlerts" in section_data
        alerts = section_data["ConfidenceThresholdAlerts"]
        assert len(alerts) == 2

        # Check first alert
        assert alerts[0]["attributeName"] == "employer_name"
        # Note: Decimal conversion for DynamoDB
        assert alerts[0]["confidence"] == Decimal("0.75")
        assert alerts[0]["confidenceThreshold"] == Decimal("0.85")

        # Check second alert
        assert alerts[1]["attributeName"] == "wages"
        assert alerts[1]["confidence"] == Decimal("0.6")
        assert alerts[1]["confidenceThreshold"] == Decimal("0.8")

    def test_update_document_section_persists_instance_count(self):
        """A multi-document section's instance count must reach DynamoDB.

        Extraction persists through this path for immediate UI visibility, so if
        the count is dropped here the UI never shows the multi-instance badge.
        """
        self.mock_client.update_item.return_value = {
            "Attributes": {"ObjectKey": "test-document.pdf", "Sections": []}
        }

        section = Section(
            section_id="section-1",
            classification="patient_demographics",
            page_ids=["1", "2"],
            extraction_result_uri="s3://bucket/sections/section-1/result.json",
            instance_count=3,
        )

        self.service.update_document_section(
            document_id="test-document.pdf", section_index=0, section=section
        )

        section_data = self.mock_client.update_item.call_args[1][
            "expression_attribute_values"
        ][":section"]
        assert section_data["InstanceCount"] == 3

    def test_update_document_section_omits_instance_count_when_undetermined(self):
        """Undetermined (0) is omitted so items match what older code wrote."""
        self.mock_client.update_item.return_value = {
            "Attributes": {"ObjectKey": "test-document.pdf", "Sections": []}
        }

        section = Section(section_id="section-1", classification="W2", page_ids=["1"])

        self.service.update_document_section(
            document_id="test-document.pdf", section_index=0, section=section
        )

        section_data = self.mock_client.update_item.call_args[1][
            "expression_attribute_values"
        ][":section"]
        assert "InstanceCount" not in section_data

    def test_update_document_section_persists_processing_issues(self):
        """Regression: this writer used to omit ProcessingIssues entirely.

        The update is ``SET #Sections[i] = :section``, which REPLACES the whole
        section map — so omitting the key did not merely skip it, it ERASED any
        issues an earlier stage had written, and left the section status icon
        blank until the collate step rewrote the full document.
        """
        self.mock_client.update_item.return_value = {
            "Attributes": {"ObjectKey": "test-document.pdf", "Sections": []}
        }

        section = Section(
            section_id="section-1",
            classification="patient_demographics",
            page_ids=["1"],
            processing_issues=[
                ProcessingIssue(
                    stage="extraction",
                    severity="warning",
                    code="extraction_multi_instance_detected",
                    message="This section contains 3 separate documents.",
                    section_id="section-1",
                )
            ],
        )

        self.service.update_document_section(
            document_id="test-document.pdf", section_index=0, section=section
        )

        section_data = self.mock_client.update_item.call_args[1][
            "expression_attribute_values"
        ][":section"]
        assert "ProcessingIssues" in section_data
        assert len(section_data["ProcessingIssues"]) == 1
        assert (
            section_data["ProcessingIssues"][0]["code"]
            == "extraction_multi_instance_detected"
        )

    def test_update_document_section_empty_extraction_uri(self):
        """Test section update with no extraction result URI."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section without extraction result
        section = Section(
            section_id="section-1",
            classification="Unknown",
            page_ids=["1"],
            extraction_result_uri=None,
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=0,
            section=section,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        section_data = call_kwargs["expression_attribute_values"][":section"]

        # Empty string used for None extraction_result_uri
        assert section_data["OutputJSONUri"] == ""

    def test_update_document_section_single_page(self):
        """Test section update with single page."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section with single page
        section = Section(
            section_id="section-1",
            classification="Receipt",
            page_ids=["1"],
            extraction_result_uri="s3://bucket/result.json",
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=0,
            section=section,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        section_data = call_kwargs["expression_attribute_values"][":section"]

        assert section_data["PageIds"] == [1]

    def test_update_document_section_no_alerts(self):
        """Test section update without confidence alerts doesn't include empty array."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section without alerts
        section = Section(
            section_id="section-1",
            classification="Invoice",
            page_ids=["1", "2"],
            extraction_result_uri="s3://bucket/result.json",
            confidence_threshold_alerts=None,
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=0,
            section=section,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        section_data = call_kwargs["expression_attribute_values"][":section"]

        # No ConfidenceThresholdAlerts key when None
        assert "ConfidenceThresholdAlerts" not in section_data

    def test_update_document_section_empty_alerts_list(self):
        """Test section update with empty confidence alerts list."""
        # Setup mock response
        self.mock_client.update_item.return_value = {
            "Attributes": {
                "ObjectKey": "test-document.pdf",
                "Sections": [],
            }
        }

        # Create test section with empty alerts list
        section = Section(
            section_id="section-1",
            classification="Invoice",
            page_ids=["1"],
            extraction_result_uri="s3://bucket/result.json",
            confidence_threshold_alerts=[],
        )

        # Test
        self.service.update_document_section(
            document_id="test-document.pdf",
            section_index=0,
            section=section,
        )

        # Verify
        call_kwargs = self.mock_client.update_item.call_args[1]
        section_data = call_kwargs["expression_attribute_values"][":section"]

        # Empty list should not add ConfidenceThresholdAlerts
        assert "ConfidenceThresholdAlerts" not in section_data
