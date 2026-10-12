# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for abort_test_runs resolver Lambda function
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables"""
    monkeypatch.setenv("TRACKING_TABLE_NAME", "test-tracking-table")
    monkeypatch.setenv("ABORT_WORKFLOW_FUNCTION_NAME", "test-abort-workflow-function")
    monkeypatch.setenv(
        "TEST_RESULT_CACHE_UPDATE_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
    )


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource"""
    with patch("boto3.resource") as mock:
        mock_table = MagicMock()
        mock.return_value.Table.return_value = mock_table
        yield mock_table


@pytest.fixture
def mock_lambda_client():
    """Mock Lambda client"""
    with patch("boto3.client") as mock:
        mock_client = MagicMock()
        mock.return_value = mock_client
        yield mock_client


@pytest.mark.unit
def test_abort_single_test_run_success(mock_env, mock_dynamodb, mock_lambda_client):
    """Test successful abort of a single test run"""
    # Import after environment setup
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    # Mock test run metadata
    mock_dynamodb.get_item.return_value = {
        "Item": {
            "Status": "RUNNING",
            "Files": ["file1.pdf", "file2.pdf"],
            "FilesCount": 2,
        }
    }
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["test-run-1"]},
    }
    with patch.object(index, "_wait_for_documents_terminal_state"):
        result = index.lambda_handler(event, None)
    assert result["success"] is True
    assert result["abortedCount"] == 1
    assert result["failedCount"] == 0
    assert mock_dynamodb.update_item.called


@pytest.mark.unit
def test_abort_test_run_not_found(mock_env, mock_dynamodb):
    """Test abort when test run does not exist"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    # Mock test run not found
    mock_dynamodb.get_item.return_value = {}
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["non-existent-test-run"]},
    }
    result = index.lambda_handler(event, None)
    assert result["failedCount"] == 1
    assert result["abortedCount"] == 0
    assert "Test run not found" in result["errors"][0]


@pytest.mark.unit
def test_abort_cannot_abort_completed(mock_env, mock_dynamodb):
    """Test that completed test runs cannot be aborted"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    # Mock test run with COMPLETE status
    mock_dynamodb.get_item.return_value = {
        "Item": {"Status": "COMPLETE", "Files": ["file1.pdf"], "FilesCount": 1}
    }
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["completed-test-run"]},
    }
    result = index.lambda_handler(event, None)
    assert result["failedCount"] == 1
    assert result["abortedCount"] == 0
    assert "Cannot abort test run with status COMPLETE" in result["errors"][0]


@pytest.mark.unit
def test_abort_queued_test_run(mock_env, mock_dynamodb, mock_lambda_client):
    """Test that QUEUED test runs can be aborted"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    mock_dynamodb.get_item.return_value = {
        "Item": {"Status": "QUEUED", "Files": ["file1.pdf"], "FilesCount": 1}
    }
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["queued-test-run"]},
    }
    with patch.object(index, "_wait_for_documents_terminal_state"):
        result = index.lambda_handler(event, None)
    assert result["success"] is True
    assert result["abortedCount"] == 1


@pytest.mark.unit
def test_wait_for_documents_all_complete():
    """Test waiting for documents when all reach terminal state quickly"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    mock_table = MagicMock()
    mock_table.table_name = "test-tracking-table"

    # Mock the module-level dynamodb.batch_get_item
    mock_dynamodb = MagicMock()
    mock_dynamodb.batch_get_item.return_value = {
        "Responses": {
            "test-tracking-table": [
                {
                    "PK": "doc#test-run-123/file1.pdf",
                    "ObjectStatus": "COMPLETED",
                    "EvaluationStatus": "COMPLETED",
                },
                {
                    "PK": "doc#test-run-123/file2.pdf",
                    "ObjectStatus": "COMPLETED",
                    "EvaluationStatus": "COMPLETED",
                },
            ]
        }
    }

    test_run_id = "test-run-123"
    object_keys = ["test-run-123/file1.pdf", "test-run-123/file2.pdf"]
    with patch.object(index, "dynamodb", mock_dynamodb):
        with patch("time.sleep"):
            index._wait_for_documents_terminal_state(
                mock_table, test_run_id, object_keys, max_wait_time=10
            )
    assert mock_dynamodb.batch_get_item.call_count >= 1


@pytest.mark.unit
def test_wait_for_documents_mixed_statuses():
    """Test waiting for documents with mixed terminal states"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    mock_table = MagicMock()
    mock_table.table_name = "test-tracking-table"

    # Mock the module-level dynamodb.batch_get_item with mixed statuses
    mock_dynamodb = MagicMock()
    mock_dynamodb.batch_get_item.return_value = {
        "Responses": {
            "test-tracking-table": [
                {
                    "PK": "doc#test-run-123/file1.pdf",
                    "ObjectStatus": "COMPLETED",
                    "EvaluationStatus": "COMPLETED",
                },
                {"PK": "doc#test-run-123/file2.pdf", "ObjectStatus": "ABORTED"},
            ]
        }
    }

    test_run_id = "test-run-123"
    object_keys = ["test-run-123/file1.pdf", "test-run-123/file2.pdf"]
    with patch.object(index, "dynamodb", mock_dynamodb):
        with patch("time.sleep"):
            index._wait_for_documents_terminal_state(
                mock_table, test_run_id, object_keys, max_wait_time=10
            )
    assert mock_dynamodb.batch_get_item.call_count >= 1


@pytest.mark.unit
def test_abort_updates_completed_at_timestamp(
    mock_env, mock_dynamodb, mock_lambda_client
):
    """Test that abort sets CompletedAt timestamp"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)
    mock_dynamodb.get_item.return_value = {
        "Item": {"Status": "RUNNING", "Files": ["file1.pdf"], "FilesCount": 1}
    }
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["test-run-123"]},
    }
    with patch.object(index, "_wait_for_documents_terminal_state"):
        index.lambda_handler(event, None)
    # Verify CompletedAt was set
    update_call = mock_dynamodb.update_item.call_args
    completed_at = update_call[1]["ExpressionAttributeValues"][":completed_at"]
    # Should be valid ISO 8601 timestamp with Z suffix
    assert completed_at.endswith("Z")
    assert "T" in completed_at
    # Should be recent (within last minute)
    timestamp = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert (now - timestamp).total_seconds() < 60


@pytest.mark.unit
def test_abort_multiple_test_runs_mixed_results(
    mock_env, mock_dynamodb, mock_lambda_client
):
    """Test aborting multiple test runs with mixed results"""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)

    def mock_get_item(Key):
        test_run_id = Key["PK"].replace("testrun#", "")
        if test_run_id == "test-run-1":
            return {
                "Item": {"Status": "RUNNING", "Files": ["file1.pdf"], "FilesCount": 1}
            }
        elif test_run_id == "test-run-2":
            return {
                "Item": {"Status": "COMPLETE", "Files": ["file2.pdf"], "FilesCount": 1}
            }
        return {}

    mock_dynamodb.get_item.side_effect = mock_get_item
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"testRunIds": ["test-run-1", "test-run-2", "test-run-3"]},
    }
    with patch.object(index, "_wait_for_documents_terminal_state"):
        result = index.lambda_handler(event, None)
    assert result["abortedCount"] == 1  # test-run-1 succeeds
    assert result["failedCount"] == 2  # test-run-2 (complete), test-run-3 (not found)


@pytest.mark.unit
def test_abort_rejects_viewer(mock_env, mock_dynamodb):
    """Defense-in-depth: a Viewer must not be able to abort test runs."""
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)

    event = {
        "identity": {"claims": {"cognito:groups": ["Viewer"]}},
        "arguments": {"testRunIds": ["test-run-1"]},
    }
    # RBAC denials raise PermissionError (not a 200 dict) so the dispatcher maps
    # them to 403/Unauthorized.
    with pytest.raises(PermissionError, match="Admin or Author"):
        index.lambda_handler(event, None)
    assert not mock_dynamodb.update_item.called


@pytest.mark.unit
def test_abort_allows_direct_lambda_invocation(mock_env, mock_dynamodb):
    """Direct Lambda invocations (no 'identity') bypass Cognito RBAC.

    Internal/automation callers (e.g. the IDP SDK / autotune agent) invoke this
    resolver directly with a payload that has no 'identity' field. Those callers
    are gated by IAM (lambda:InvokeFunction), not Cognito groups, so the RBAC
    check must not reject them.
    """
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)

    # Test run in an abortable state so the handler proceeds past auth into abort.
    mock_dynamodb.get_item.return_value = {
        "Item": {"Status": "RUNNING", "Files": ["file1.pdf"], "FilesCount": 1}
    }

    # No 'identity' field -> direct invoke; must NOT raise PermissionError.
    event = {
        "info": {"fieldName": "abortTestRuns"},
        "arguments": {"testRunIds": ["test-run-1"]},
    }
    with patch.object(index, "_wait_for_documents_terminal_state"):
        result = index.lambda_handler(event, None)

    assert result["success"] is True
    assert result["abortedCount"] == 1
    assert mock_dynamodb.update_item.called


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored", ["COMPLETED", " COMPLETED ", "completed", "DISABLED"]
)
def test_the_abort_wait_reads_a_status_the_way_the_run_status_reader_does(
    mock_env, mock_dynamodb, stored
):
    """A padded or lower-cased status must not be read as work in progress.

    This function waits for every document to settle before the abort proceeds,
    and anything it does not recognise as terminal costs the whole 25-second
    budget. The run-status resolver normalizes case *and* whitespace for the
    same attribute; a difference here is invisible to the test that compares the
    two readers, because that one compares their status *sets* rather than how
    each reads the value — so it is pinned on this side directly.
    """
    import importlib.util
    import os
    import sys

    spec = importlib.util.spec_from_file_location(
        "index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/abort_test_runs/index.py",
        ),
    )
    index = importlib.util.module_from_spec(spec)
    sys.modules["index"] = index
    spec.loader.exec_module(index)

    mock_dynamodb.table_name = "test-tracking-table"
    with (
        patch.object(
            index,
            "_batch_get_document_items",
            return_value={
                "run/a.pdf": {"ObjectStatus": "COMPLETED", "EvaluationStatus": stored}
            },
        ),
        patch.object(index.time, "sleep") as slept,
    ):
        index._wait_for_documents_terminal_state(
            mock_dynamodb, "run", ["run/a.pdf"], max_wait_time=25
        )

    # Returned on the first poll, rather than sleeping out the budget waiting
    # for a document that had already finished.
    slept.assert_not_called()
