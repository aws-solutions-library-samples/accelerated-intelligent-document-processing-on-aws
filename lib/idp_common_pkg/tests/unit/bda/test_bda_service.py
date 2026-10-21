# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the BdaService class.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_service import BdaService


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_bda_service_init_with_defaults(mock_boto3, monkeypatch):
    """Test BdaService initialization with default values."""
    # Setup mock
    mock_session = MagicMock()
    mock_boto3.Session.return_value = mock_session

    mock_sts_client = MagicMock()
    mock_session.client.return_value = mock_sts_client

    mock_identity = {"Account": "123456789012"}
    mock_sts_client.get_caller_identity.return_value = mock_identity

    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    # Restored after the test, so later tests on this worker keep their region.
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    # Call the constructor
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Verify
    mock_boto3.Session.assert_called_once()
    mock_session.client.assert_called_once_with("sts")
    mock_sts_client.get_caller_identity.assert_called_once()
    mock_boto3.client.assert_called_once_with("bedrock-data-automation-runtime")

    assert service._output_s3_uri == "s3://output-bucket/output-path"
    assert service._dataAutomationProjectArn is None
    assert (
        service._dataAutomationProfileArn
        == "arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/us.data-automation-v1"
    )
    assert service._bda_client == mock_bda_client


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_bda_service_init_with_all_params(mock_boto3):
    """Test BdaService initialization with all parameters provided."""
    # Setup mock
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    # Call the constructor
    service = BdaService(
        output_s3_uri="s3://output-bucket/output-path",
        dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:data-automation-project/project-id",
        dataAutomationProfileArn="arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile",
    )

    # Verify
    mock_boto3.client.assert_called_once_with("bedrock-data-automation-runtime")

    assert service._output_s3_uri == "s3://output-bucket/output-path"
    assert (
        service._dataAutomationProjectArn
        == "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/project-id"
    )
    assert (
        service._dataAutomationProfileArn
        == "arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile"
    )
    assert service._bda_client == mock_bda_client


@pytest.mark.unit
@patch("idp_common.bda.bda_service.uuid")
@patch("idp_common.bda.bda_service.boto3")
def test_invoke_data_automation_async_with_blueprint(mock_boto3, mock_uuid):
    """Test invoke_data_automation_async with blueprint ARN."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    mock_uuid.uuid4.return_value = "test-uuid"

    mock_response = {"invocationArn": "test-invocation-arn"}
    mock_bda_client.invoke_data_automation_async.return_value = mock_response

    # Create service
    service = BdaService(
        output_s3_uri="s3://output-bucket/output-path",
        dataAutomationProfileArn="arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile",
    )

    # Call the method
    result = service.invoke_data_automation_async(
        input_s3_uri="s3://input-bucket/input-path/document.pdf",
        blueprintArn="arn:aws:bedrock:us-west-2:123456789012:blueprint/blueprint-id",
    )

    # Verify
    mock_uuid.uuid4.assert_called_once()

    expected_payload = {
        "clientToken": "test-uuid",
        "inputConfiguration": {"s3Uri": "s3://input-bucket/input-path/document.pdf"},
        "outputConfiguration": {"s3Uri": "s3://output-bucket/output-path"},
        "notificationConfiguration": {
            "eventBridgeConfiguration": {"eventBridgeEnabled": True}
        },
        "dataAutomationProfileArn": "arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile",
        "blueprints": [
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/blueprint-id"
            }
        ],
    }

    mock_bda_client.invoke_data_automation_async.assert_called_once_with(
        **expected_payload
    )
    assert result == mock_response


@pytest.mark.unit
@patch("idp_common.bda.bda_service.uuid")
@patch("idp_common.bda.bda_service.boto3")
def test_invoke_data_automation_async_with_project(mock_boto3, mock_uuid):
    """Test invoke_data_automation_async with project ARN."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    mock_uuid.uuid4.return_value = "test-uuid"

    mock_response = {"invocationArn": "test-invocation-arn"}
    mock_bda_client.invoke_data_automation_async.return_value = mock_response

    # Create service
    service = BdaService(
        output_s3_uri="s3://output-bucket/output-path",
        dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:data-automation-project/project-id",
        dataAutomationProfileArn="arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile",
    )

    # Call the method
    result = service.invoke_data_automation_async(
        input_s3_uri="s3://input-bucket/input-path/document.pdf"
    )

    # Verify
    mock_uuid.uuid4.assert_called_once()

    expected_payload = {
        "clientToken": "test-uuid",
        "inputConfiguration": {"s3Uri": "s3://input-bucket/input-path/document.pdf"},
        "outputConfiguration": {"s3Uri": "s3://output-bucket/output-path"},
        "notificationConfiguration": {
            "eventBridgeConfiguration": {"eventBridgeEnabled": True}
        },
        "dataAutomationProfileArn": "arn:aws:bedrock:us-west-2:123456789012:data-automation-profile/custom-profile",
        "dataAutomationConfiguration": {
            "dataAutomationProjectArn": "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/project-id",
            "stage": "LIVE",
        },
    }

    mock_bda_client.invoke_data_automation_async.assert_called_once_with(
        **expected_payload
    )
    assert result == mock_response
