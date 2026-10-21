# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the save_reporting_data Lambda function.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from index import handler


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
@patch("index.ConfigurationManager")
@patch("index.get_config")
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_success(
    mock_document_from_dict,
    mock_save_reporting_data,
    mock_get_config,
    mock_config_manager,
):
    """Test successful handler execution."""
    # Mock document
    mock_document = MagicMock()
    mock_document_from_dict.return_value = mock_document

    # Mock configuration load (handler resolves a config object from the table)
    mock_config = MagicMock()
    mock_get_config.return_value = mock_config
    mock_config_manager.return_value.get_merged_pricing.return_value = None

    # Mock SaveReportingData
    mock_reporter = MagicMock()
    mock_reporter.save.return_value = ["result1", "result2"]
    mock_save_reporting_data.return_value = mock_reporter

    # Create test event
    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
    }

    # Call handler
    response = handler(event, {})

    # Verify response
    assert response["statusCode"] == 200
    assert "Successfully saved data to reporting bucket" in response["body"]

    # Handler resolves a config OBJECT from the table and passes it (not the
    # table name string) to SaveReportingData.
    mock_save_reporting_data.assert_called_once_with(
        "test-reporting-bucket",
        "test-stack-reporting-db",
        mock_config,
    )
    # No review-queue info on the event: the reporter is told so explicitly.
    mock_reporter.save.assert_called_once_with(
        mock_document, ["evaluation_results"], review=None
    )


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
@patch("index.ConfigurationManager")
@patch("index.get_config")
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_passes_validated_review_info(
    mock_document_from_dict,
    mock_save_reporting_data,
    mock_get_config,
    mock_config_manager,
):
    """A routed document's review info reaches the reporter as the model."""
    from idp_common.review_queues.models import ReviewReportingInfo

    mock_config_manager.return_value.get_merged_pricing.return_value = None
    mock_reporter = mock_save_reporting_data.return_value
    mock_reporter.save.return_value = ["result1"]
    review = {
        "cycle_id": "c-1",
        "review_status": "APPROVED",
        "profile": "lending",
        "queue": "default",
    }
    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["document_sections"],
        "review": review,
    }

    assert handler(event, {})["statusCode"] == 200

    passed = mock_reporter.save.call_args.kwargs["review"]
    assert isinstance(passed, ReviewReportingInfo)
    assert passed == ReviewReportingInfo.model_validate(review)


@pytest.mark.unit
def test_handler_missing_document():
    """Test handler with missing document."""
    event = {
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
    }

    response = handler(event, {})

    assert response["statusCode"] == 400
    assert "No document data provided in the event" in response["body"]


@pytest.mark.unit
def test_handler_missing_reporting_bucket():
    """Test handler with missing reporting bucket."""
    event = {"document": {"id": "test-doc"}, "data_to_save": ["evaluation_results"]}

    response = handler(event, {})

    assert response["statusCode"] == 400
    assert "No reporting bucket specified in the event" in response["body"]


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
def test_handler_no_data_to_save():
    """Test handler with no data_to_save."""
    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
    }

    response = handler(event, {})

    assert response["statusCode"] == 200
    assert "No data_to_save specified in the event, nothing to do" in response["body"]


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_no_results_processed(
    mock_document_from_dict, mock_save_reporting_data
):
    """Test handler when no data is processed."""
    # Mock document
    mock_document = MagicMock()
    mock_document_from_dict.return_value = mock_document

    # Mock SaveReportingData to return empty results
    mock_reporter = MagicMock()
    mock_reporter.save.return_value = []
    mock_save_reporting_data.return_value = mock_reporter

    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
    }

    response = handler(event, {})

    assert response["statusCode"] == 200
    assert "No data was processed - check data_to_save parameter" in response["body"]


@pytest.mark.unit
@patch.dict(os.environ, {})  # No environment variables set
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_no_config_table(
    mock_document_from_dict, mock_save_reporting_data, caplog
):
    """Test handler with no configuration table name."""
    # Mock document
    mock_document = MagicMock()
    mock_document_from_dict.return_value = mock_document

    # Mock SaveReportingData
    mock_reporter = MagicMock()
    mock_reporter.save.return_value = ["result1"]
    mock_save_reporting_data.return_value = mock_reporter

    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
    }

    response = handler(event, {})

    assert response["statusCode"] == 200
    assert "Successfully saved data to reporting bucket" in response["body"]

    # Verify warning was logged
    assert "No configuration table name provided" in caplog.text

    # With no config table, the handler passes config=None to SaveReportingData.
    mock_save_reporting_data.assert_called_once_with(
        "test-reporting-bucket",
        None,  # No database name since no STACK_NAME
        None,  # No config resolved (no config table)
    )


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
@patch("index.ConfigurationManager")
@patch("index.get_config")
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_with_database_name_in_event(
    mock_document_from_dict,
    mock_save_reporting_data,
    mock_get_config,
    mock_config_manager,
):
    """Test handler with database_name provided in event."""
    # Mock document
    mock_document = MagicMock()
    mock_document_from_dict.return_value = mock_document

    # Mock configuration load
    mock_config = MagicMock()
    mock_get_config.return_value = mock_config
    mock_config_manager.return_value.get_merged_pricing.return_value = None

    # Mock SaveReportingData
    mock_reporter = MagicMock()
    mock_reporter.save.return_value = ["result1"]
    mock_save_reporting_data.return_value = mock_reporter

    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
        "database_name": "custom-database-name",
    }

    response = handler(event, {})

    assert response["statusCode"] == 200
    assert "Successfully saved data to reporting bucket" in response["body"]

    # Custom database name from the event; resolved config object (not the string).
    mock_save_reporting_data.assert_called_once_with(
        "test-reporting-bucket",
        "custom-database-name",
        mock_config,
    )


@pytest.mark.unit
@patch.dict(
    os.environ,
    {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "test-stack"},
)
@patch("index.SaveReportingData")
@patch("index.Document.from_dict")
def test_handler_exception(mock_document_from_dict, mock_save_reporting_data):
    """Test handler with exception."""
    # Mock document
    mock_document = MagicMock()
    mock_document_from_dict.return_value = mock_document

    # Mock SaveReportingData to raise exception
    mock_save_reporting_data.side_effect = Exception("Test exception")

    event = {
        "document": {"id": "test-doc", "input_key": "test.pdf"},
        "reporting_bucket": "test-reporting-bucket",
        "data_to_save": ["evaluation_results"],
    }

    response = handler(event, {})

    assert response["statusCode"] == 500
    assert "Error saving data to reporting bucket: Test exception" in response["body"]
