# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Additional unit tests for the BdaService class.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from idp_common.bda.bda_service import BdaService


@pytest.mark.unit
@patch("idp_common.bda.bda_service.time")
@patch("idp_common.bda.bda_service.boto3")
def test_wait_data_automation_invocation_success(mock_boto3, mock_time):
    """Test wait_data_automation_invocation with successful completion."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    # First call returns 'InProgress', second call returns 'Success'
    mock_bda_client.get_data_automation_status.side_effect = [
        {"status": "InProgress"},
        {"status": "Success"},
    ]

    # Create service
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Call the method
    service.wait_data_automation_invocation(
        invocationArn="test-invocation-arn", sleep_seconds=5
    )

    # Verify
    assert mock_bda_client.get_data_automation_status.call_count == 2
    mock_bda_client.get_data_automation_status.assert_has_calls(
        [
            call(invocationArn="test-invocation-arn"),
            call(invocationArn="test-invocation-arn"),
        ]
    )
    mock_time.sleep.assert_called_once_with(5)


@pytest.mark.unit
@patch("idp_common.bda.bda_service.time")
@patch("idp_common.bda.bda_service.boto3")
def test_wait_data_automation_invocation_error(mock_boto3, mock_time):
    """Test wait_data_automation_invocation with error completion."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    # First call returns 'InProgress', second call returns 'ServiceError'
    mock_bda_client.get_data_automation_status.side_effect = [
        {"status": "InProgress"},
        {"status": "ServiceError"},
    ]

    # Create service
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Call the method
    service.wait_data_automation_invocation(
        invocationArn="test-invocation-arn", sleep_seconds=5
    )

    # Verify
    assert mock_bda_client.get_data_automation_status.call_count == 2
    mock_bda_client.get_data_automation_status.assert_has_calls(
        [
            call(invocationArn="test-invocation-arn"),
            call(invocationArn="test-invocation-arn"),
        ]
    )
    mock_time.sleep.assert_called_once_with(5)


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_get_data_automation_invocation_success(mock_boto3):
    """Test get_data_automation_invocation with successful status."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    mock_bda_client.get_data_automation_status.return_value = {
        "status": "Success",
        "outputConfiguration": {"s3Uri": "s3://output-bucket/output-path/job-123"},
    }

    # Create service
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Call the method
    result = service.get_data_automation_invocation(invocationArn="test-invocation-arn")

    # Verify
    mock_bda_client.get_data_automation_status.assert_called_once_with(
        invocationArn="test-invocation-arn"
    )

    assert result == {
        "status": "success",
        "output_location": "s3://output-bucket/output-path/job-123",
    }


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_get_data_automation_invocation_failed(mock_boto3):
    """Test get_data_automation_invocation with failed status."""
    # Setup mocks
    mock_bda_client = MagicMock()
    mock_boto3.client.return_value = mock_bda_client

    mock_bda_client.get_data_automation_status.return_value = {
        "status": "ServiceError",
        "errorType": "ValidationError",
        "errorMessage": "Invalid input format",
    }

    # Create service
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Call the method
    result = service.get_data_automation_invocation(invocationArn="test-invocation-arn")

    # Verify
    mock_bda_client.get_data_automation_status.assert_called_once_with(
        invocationArn="test-invocation-arn"
    )

    assert result == {
        "status": "failed",
        "error_type": "ValidationError",
        "error_message": "Invalid input format",
    }


@pytest.mark.unit
@patch("idp_common.bda.bda_service.BdaService.get_data_automation_invocation")
@patch("idp_common.bda.bda_service.BdaService.wait_data_automation_invocation")
@patch("idp_common.bda.bda_service.BdaService.invoke_data_automation_async")
@patch("idp_common.bda.bda_service.boto3")
def test_invoke_data_automation(
    mock_boto3, mock_invoke_async, mock_wait, mock_get_invocation
):
    """Test invoke_data_automation which combines async, wait, and get methods."""
    # Setup mocks
    mock_invoke_async.return_value = {"invocationArn": "test-invocation-arn"}
    mock_get_invocation.return_value = {
        "status": "success",
        "output_location": "s3://output-bucket/output-path/job-123",
    }

    # Create service
    service = BdaService(output_s3_uri="s3://output-bucket/output-path")

    # Call the method
    result = service.invoke_data_automation(
        input_s3_uri="s3://input-bucket/input-path/document.pdf",
        blueprintArn="arn:aws:bedrock:us-west-2:123456789012:blueprint/blueprint-id",
        sleep_seconds=15,
    )

    # Verify
    mock_invoke_async.assert_called_once_with(
        input_s3_uri="s3://input-bucket/input-path/document.pdf",
        blueprintArn="arn:aws:bedrock:us-west-2:123456789012:blueprint/blueprint-id",
    )

    mock_wait.assert_called_once_with(
        invocationArn="test-invocation-arn", sleep_seconds=15
    )

    mock_get_invocation.assert_called_once_with(invocationArn="test-invocation-arn")

    assert result == {
        "status": "success",
        "output_location": "s3://output-bucket/output-path/job-123",
    }


# ---------------------------------------------------------------------------
# Partition correctness of the auto-derived data-automation profile ARN.
#
# BdaService used to build this ARN itself with a literal `arn:aws:` and a naive
# region.split("-")[0]. A live GovCloud deployment reported EVERY BDA invoke
# failing with "The provided ARN is invalid" as a result (issue #527): account
# IDs do not span partitions, so `arn:aws:...` is meaningless in aws-us-gov. The
# naive prefix was also wrong for Asia Pacific (ap-* needs the geo `apac`).
# It now delegates to bda_ocr.build_profile_arn with the partition taken from the
# caller identity.
# ---------------------------------------------------------------------------


def _service_with_identity(mock_boto3, *, region, caller_arn, account="111122223333"):
    """Build a BdaService with STS returning the given caller ARN."""
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {
        "Account": account,
        "Arn": caller_arn,
    }
    mock_boto3.Session.return_value = session
    with patch.dict("os.environ", {"AWS_REGION": region}, clear=False):
        return BdaService(output_s3_uri="s3://output-bucket/output-path")


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_profile_arn_uses_govcloud_partition(mock_boto3):
    """The reported defect: GovCloud must not get an arn:aws: profile ARN."""
    service = _service_with_identity(
        mock_boto3,
        region="us-gov-west-1",
        caller_arn="arn:aws-us-gov:sts::111122223333:assumed-role/r/s",
    )
    arn = service._dataAutomationProfileArn
    assert arn is not None
    assert arn.startswith("arn:aws-us-gov:bedrock:us-gov-west-1:"), arn
    assert "arn:aws:" not in arn


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_profile_arn_uses_apac_geo_prefix(mock_boto3):
    """ap-* regions need the `apac` geo prefix, not `ap`."""
    service = _service_with_identity(
        mock_boto3,
        region="ap-southeast-2",
        caller_arn="arn:aws:sts::111122223333:assumed-role/r/s",
    )
    assert service._dataAutomationProfileArn.endswith("apac.data-automation-v1")


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_profile_arn_unchanged_in_commercial(mock_boto3):
    """Commercial behaviour is byte-identical to before the fix."""
    service = _service_with_identity(
        mock_boto3,
        region="us-west-2",
        caller_arn="arn:aws:sts::111122223333:assumed-role/r/s",
    )
    assert service._dataAutomationProfileArn == (
        "arn:aws:bedrock:us-west-2:111122223333:"
        "data-automation-profile/us.data-automation-v1"
    )


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_profile_arn_falls_back_to_commercial_without_caller_arn(mock_boto3):
    """A caller identity with no Arn must not crash or emit 'arn:None:'."""
    service = _service_with_identity(mock_boto3, region="us-west-2", caller_arn=None)
    assert service._dataAutomationProfileArn.startswith("arn:aws:bedrock:us-west-2:")


@pytest.mark.unit
@patch("idp_common.bda.bda_service.boto3")
def test_explicit_profile_arn_is_never_overridden(mock_boto3):
    """An explicitly supplied profile ARN short-circuits derivation entirely."""
    explicit = "arn:aws-us-gov:bedrock:us-gov-west-1:1:data-automation-profile/custom"
    service = BdaService(output_s3_uri="s3://b/o", dataAutomationProfileArn=explicit)
    assert service._dataAutomationProfileArn == explicit
