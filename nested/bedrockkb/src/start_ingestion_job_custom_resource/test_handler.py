# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the StartIngestionJobFunction custom resource handler.

Run with::

    cd nested/bedrockkb/src/start_ingestion_job_custom_resource
    python -m pytest test_handler.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Stub out cfnresponse before importing the handler: the vendored
# cfnresponse.py beside it would PUT to a real URL.
sys.modules.setdefault("cfnresponse", MagicMock(SUCCESS="SUCCESS", FAILED="FAILED"))

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def mock_client():
    """Patch the module-level bedrock-agent client."""
    import handler

    client = MagicMock()
    client.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    client.exceptions.ConflictException = type("ConflictException", (Exception,), {})
    with patch.object(handler, "CLIENT", client):
        yield client


@pytest.fixture(autouse=True)
def fast_sleep():
    """Make the polling loop instant."""
    with patch("time.sleep"):
        yield


def _list_jobs_pages(jobs):
    return [{"ingestionJobSummaries": jobs}]


def test_delete_with_no_running_jobs_succeeds(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([])
    mock_client.get_paginator.return_value = paginator

    handler.stop_running_ingestion_jobs("kb-1", "ds-1")

    mock_client.stop_ingestion_job.assert_not_called()


def test_delete_stops_in_progress_job_and_polls_to_terminal(mock_client):
    import handler

    paginator = MagicMock()

    def paginate(knowledgeBaseId, dataSourceId, filters):
        status = filters[0]["values"][0]
        if status == "IN_PROGRESS":
            return _list_jobs_pages([{"ingestionJobId": "job-1"}])
        return _list_jobs_pages([])

    paginator.paginate.side_effect = paginate
    mock_client.get_paginator.return_value = paginator

    mock_client.get_ingestion_job.side_effect = [
        {"ingestionJob": {"status": "STOPPING"}},
        {"ingestionJob": {"status": "STOPPED"}},
    ]

    handler.stop_running_ingestion_jobs("kb-1", "ds-1")

    mock_client.stop_ingestion_job.assert_called_once_with(
        knowledgeBaseId="kb-1", dataSourceId="ds-1", ingestionJobId="job-1"
    )
    assert mock_client.get_ingestion_job.call_count == 2


def test_delete_tolerates_resource_not_found_on_stop(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([{"ingestionJobId": "job-1"}])
    mock_client.get_paginator.return_value = paginator

    mock_client.stop_ingestion_job.side_effect = mock_client.exceptions.ResourceNotFoundException()
    mock_client.get_ingestion_job.side_effect = mock_client.exceptions.ResourceNotFoundException()

    handler.stop_running_ingestion_jobs("kb-1", "ds-1")


def test_delete_tolerates_conflict_on_stop(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([{"ingestionJobId": "job-1"}])
    mock_client.get_paginator.return_value = paginator

    mock_client.stop_ingestion_job.side_effect = mock_client.exceptions.ConflictException()
    mock_client.get_ingestion_job.return_value = {"ingestionJob": {"status": "STOPPED"}}

    handler.stop_running_ingestion_jobs("kb-1", "ds-1")


def test_delete_does_not_raise_when_list_fails(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.side_effect = RuntimeError("API unavailable")
    mock_client.get_paginator.return_value = paginator

    handler.stop_running_ingestion_jobs("kb-1", "ds-1")

    mock_client.stop_ingestion_job.assert_not_called()


def test_delete_times_out_without_raising(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([{"ingestionJobId": "job-1"}])
    mock_client.get_paginator.return_value = paginator

    mock_client.get_ingestion_job.return_value = {"ingestionJob": {"status": "STOPPING"}}

    monotonic = iter([0.0, 0.0] + [handler.MAX_WAIT_SECONDS + 1] * 100)

    with patch("time.monotonic", side_effect=lambda: next(monotonic)):
        handler.stop_running_ingestion_jobs("kb-1", "ds-1")


def test_lambda_handler_delete_always_sends_success(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([])
    mock_client.get_paginator.return_value = paginator

    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": "kb-1/ds-1",
        "ResourceProperties": {"knowledgeBaseId": "kb-1", "dataSourceId": "ds-1"},
    }
    context = MagicMock(log_stream_name="test-log-stream")

    with patch.object(handler, "cfnresponse") as mock_cfn:
        mock_cfn.SUCCESS = "SUCCESS"
        mock_cfn.FAILED = "FAILED"
        handler.lambda_handler(event, context)
        mock_cfn.send.assert_called_once()
        args = mock_cfn.send.call_args.args
        assert args[2] == "SUCCESS"


def test_lambda_handler_delete_recovers_ids_from_physical_resource_id(mock_client):
    import handler

    paginator = MagicMock()
    paginator.paginate.return_value = _list_jobs_pages([])
    mock_client.get_paginator.return_value = paginator

    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": "kb-from-physid/ds-from-physid",
        "ResourceProperties": {},
    }
    context = MagicMock(log_stream_name="test-log-stream")

    with patch.object(handler, "cfnresponse") as mock_cfn:
        mock_cfn.SUCCESS = "SUCCESS"
        mock_cfn.FAILED = "FAILED"
        handler.lambda_handler(event, context)

    # Confirm we attempted to list jobs against the parsed-out ids.
    paginator.paginate.assert_any_call(
        knowledgeBaseId="kb-from-physid",
        dataSourceId="ds-from-physid",
        filters=[{"attribute": "STATUS", "operator": "EQ", "values": ["STARTING"]}],
    )


def test_lambda_handler_create_starts_job(mock_client):
    import handler

    event = {
        "RequestType": "Create",
        "ResourceProperties": {"knowledgeBaseId": "kb-1", "dataSourceId": "ds-1"},
    }
    context = MagicMock(log_stream_name="test-log-stream")

    with patch.object(handler, "cfnresponse") as mock_cfn:
        mock_cfn.SUCCESS = "SUCCESS"
        mock_cfn.FAILED = "FAILED"
        handler.lambda_handler(event, context)

    mock_client.start_ingestion_job.assert_called_once()
