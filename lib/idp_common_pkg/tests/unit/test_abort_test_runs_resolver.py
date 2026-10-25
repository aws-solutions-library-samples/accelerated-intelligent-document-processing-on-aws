# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for abort_test_runs resolver Lambda function
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


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


# The handler pauses for `_SETTLE_SECONDS` after queueing the test-result cache
# update, so the metrics Lambda has started reading before the run is marked
# ABORTED. Served for real, that is two seconds of this suite's wall clock in
# every test that reaches it, and nothing asserted the pause existed at all --
# so the four tests below paid for it and proved nothing about it. They patch it
# and assert it instead (#1380).
_SETTLE_SECONDS = 2


def _assert_settled(settle):
    """The handler paused for the settle interval, once per request."""
    assert settle.call_args_list, (
        "the handler did not pause after queueing the cache update, so the "
        "metrics Lambda can be asked to read a run already marked ABORTED"
    )
    assert all(call.args == (_SETTLE_SECONDS,) for call in settle.call_args_list), (
        f"the settle is no longer {_SETTLE_SECONDS}s: {settle.call_args_list}"
    )


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
    with (
        patch.object(index, "_wait_for_documents_terminal_state"),
        # Patched, not served: see the note above `_assert_settled`.
        patch("time.sleep") as settle,
    ):
        result = index.lambda_handler(event, None)
    _assert_settled(settle)
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
    with (
        patch.object(index, "_wait_for_documents_terminal_state"),
        # Patched, not served: see the note above `_assert_settled`.
        patch("time.sleep") as settle,
    ):
        result = index.lambda_handler(event, None)
    _assert_settled(settle)
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
    with (
        patch.object(index, "_wait_for_documents_terminal_state"),
        # Patched, not served: see the note above `_assert_settled`.
        patch("time.sleep") as settle,
    ):
        index.lambda_handler(event, None)
    _assert_settled(settle)
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
    with (
        patch.object(index, "_wait_for_documents_terminal_state"),
        # Patched, not served: see the note above `_assert_settled`.
        patch("time.sleep") as settle,
    ):
        result = index.lambda_handler(event, None)
    _assert_settled(settle)
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
            return_value=(
                {
                    "run/a.pdf": {
                        "ObjectStatus": "COMPLETED",
                        "EvaluationStatus": stored,
                    }
                },
                set(),
            ),
        ),
        patch.object(index.time, "sleep") as slept,
    ):
        index._wait_for_documents_terminal_state(
            mock_dynamodb, "run", ["run/a.pdf"], max_wait_time=25
        )

    # Returned on the first poll, rather than sleeping out the budget waiting
    # for a document that had already finished.
    slept.assert_not_called()


# --- BatchGetItem UnprocessedKeys ------------------------------------------------
#
# The wait loop reads every document's status with BatchGetItem, which may hold
# keys back as UnprocessedKeys under throttling. Those are resubmitted with backoff
# a bounded number of times; a key still held back after that is reported as
# unknown, never as "not found". The table is moto's; only the hold-back is
# injected, by a wrapper around the real resource that caps its own call count so
# an unbounded loop fails instead of hanging.

_TABLE = "test-tracking-table"
_STUB_CALL_CAP = 200


def _load_abort_index():
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
    return index


class _HoldBackResource:
    """A real moto resource whose batch_get_item holds items back on chosen calls."""

    def __init__(self, real, hold):
        self._real = real
        self._hold = hold
        self.calls = []

    def batch_get_item(self, RequestItems):
        self.calls.append(len(RequestItems[_TABLE]["Keys"]))
        if len(self.calls) > _STUB_CALL_CAP:
            raise AssertionError(
                f"batch_get_item called more than {_STUB_CALL_CAP} times"
            )
        response = self._real.batch_get_item(RequestItems=RequestItems)
        items = response["Responses"][_TABLE]
        held = min(self._hold(len(self.calls), len(items)), len(items))
        if held:
            kept, back = items[: len(items) - held], items[len(items) - held :]
            response["Responses"][_TABLE] = kept
            response["UnprocessedKeys"] = {
                _TABLE: {"Keys": [{"PK": i["PK"], "SK": i["SK"]} for i in back]}
            }
        return response


class _Clock:
    """Stands in for the module's ``time``: sleeping advances the clock."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        # As time.sleep does: a negative length is an error, not a no-op, so a
        # caller that computes one fails here as it would in production.
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def abort_table(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        real = boto3.resource("dynamodb", region_name="us-east-1")
        table = real.create_table(
            TableName=_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        keys = [f"run/doc-{n:03d}.pdf" for n in range(120)]
        for key in keys:
            table.put_item(
                Item={
                    "PK": f"doc#{key}",
                    "SK": "none",
                    "ObjectStatus": "ABORTED",
                }
            )
        index = _load_abort_index()
        clock = _Clock()
        monkeypatch.setattr(index, "time", clock)

        def install(hold):
            stub = _HoldBackResource(real, hold)
            monkeypatch.setattr(index, "dynamodb", stub)
            return stub

        yield index, table, keys, install, clock


def _cap(attempt):
    return min(1.0, 0.05 * 2**attempt)


@pytest.mark.unit
def test_the_abort_wait_resubmits_keys_held_back_by_batch_get(
    abort_table, caplog, monkeypatch
):
    """A transient hold-back is retried inside the poll, not left to the next one."""
    index, table, keys, install, clock = abort_table
    drawn = []
    real_uniform = index.random.uniform
    monkeypatch.setattr(
        index.random,
        "uniform",
        lambda low, high: drawn.append((low, high)) or real_uniform(low, high),
    )
    # Chunk 1 (100 keys) is held back on calls 1 and 2, chunk 2 (20) on call 4.
    stub = install(lambda call, n: 25 if call in (1, 2, 4) else 0)

    with caplog.at_level("WARNING"):
        index._wait_for_documents_terminal_state(table, "run", keys, max_wait_time=25)

    assert stub.calls == [100, 25, 25, 20, 20]
    # Only backoff sleeps, each within its attempt's cap: the documents were
    # all terminal, so the wait returned on its first poll.
    assert len(clock.sleeps) == 3
    for attempt, slept in zip([1, 2, 1], clock.sleeps):
        assert 0 <= slept <= _cap(attempt)
    # Full jitter: each delay is drawn uniformly from zero to that attempt's cap.
    assert drawn == [(0, _cap(a)) for a in [1, 2, 1]]
    assert "not found" not in caplog.text


@pytest.mark.unit
def test_keys_that_stay_unprocessed_are_unknown_not_missing(
    abort_table, caplog, monkeypatch
):
    """Persistent throttling: bounded per poll, and reported as unknown."""
    index, table, keys, install, clock = abort_table
    drawn = []
    real_uniform = index.random.uniform
    monkeypatch.setattr(
        index.random,
        "uniform",
        lambda low, high: drawn.append((low, high)) or real_uniform(low, high),
    )
    stub = install(lambda call, n: 1 if n else 0)

    with caplog.at_level("WARNING"):
        index._wait_for_documents_terminal_state(
            table, "run", keys[:3], max_wait_time=25
        )

    # The held-back document was never read, so it is unknown -- not "not found",
    # which it is not -- and the wait did not treat it as terminal.
    assert "not found" not in caplog.text
    assert f"Status of document {keys[2]} is unknown" in caplog.text
    assert "1 unknown" in caplog.text
    poll_sleeps = [s for s in clock.sleeps if s == 2]
    polls = len(poll_sleeps)
    assert polls >= 2, clock.sleeps
    # Each poll makes at most BATCH_GET_MAX_ATTEMPTS calls for its one chunk.
    # Every poll followed by a full interval made all of them; the last one may
    # be cut short by the end of the wait (#1606), so it makes at most that many.
    attempts = index.BATCH_GET_MAX_ATTEMPTS
    assert polls * attempts <= len(stub.calls) <= (polls + 1) * attempts
    # The backoff grows to, and stops at, one second (0.05 * 2**5 would be 1.6).
    assert [high for _, high in drawn[:5]] == [0.1, 0.2, 0.4, 0.8, 1.0]


@pytest.mark.unit
def test_a_refused_batch_marks_its_documents_unknown(abort_table, caplog, monkeypatch):
    """A service error on a batch is an unread batch, not missing documents."""
    from botocore.exceptions import ClientError

    index, table, keys, install, clock = abort_table

    class _Refusing:
        calls = 0

        def batch_get_item(self, RequestItems):
            type(self).calls += 1
            if type(self).calls > _STUB_CALL_CAP:
                raise AssertionError("unbounded")
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}},
                "BatchGetItem",
            )

    monkeypatch.setattr(index, "dynamodb", _Refusing())

    with caplog.at_level("WARNING"):
        index._wait_for_documents_terminal_state(
            table, "run", keys[:2], max_wait_time=5
        )

    assert "not found" not in caplog.text
    assert f"Status of document {keys[0]} is unknown" in caplog.text


# ---------------------------------------------------------------------------
# The whole request fits inside the REST dispatcher's read timeout (#1606).
#
# The dispatcher stops reading a resolver after _RESOLVER_READ_TIMEOUT_SECONDS
# and answers 504 although the resolver runs on and completes the abort. These
# drive the real handler against a moto table, with the module's clock replaced
# so that sleeping and every BatchGetItem call advance it: the elapsed figure is
# the handler's own wall clock, measured, not a sum of constants.

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[4]
_DISPATCHER = (
    _REPO_ROOT / "nested/api-resolvers/src/lambda/http_api_dispatcher/index.py"
)
_UI_ABORT_MUTATION = (
    _REPO_ROOT / "src/ui/src/graphql/operations/mutations/AbortTestRuns.graphql"
)
# Each BatchGetItem call costs this much of the fake clock.
_CALL_LATENCY = 0.05


_INVOKE_OUTPUT_SHAPE = None  # the Lambda model's Invoke output, loaded once


def _workflow_answer(**kwargs):
    """What a synchronous abort-workflow invoke returns when it stops its batch.

    The abort resolver reads the answer (#1609), so the doubles below return one:
    the abortWorkflow handler's success result, in the Invoke response shape,
    checked against the Lambda service model's output shape. The documents'
    tracking records are left as they are -- these tests are about documents
    that never settle.
    """
    import io

    import botocore.session
    from botocore.response import StreamingBody
    from botocore.validate import validate_parameters

    global _INVOKE_OUTPUT_SHAPE
    if _INVOKE_OUTPUT_SHAPE is None:
        model = botocore.session.get_session().get_service_model("lambda")
        _INVOKE_OUTPUT_SHAPE = model.operation_model("Invoke").output_shape
    keys = json.loads(kwargs["Payload"])["arguments"]["objectKeys"]
    body = json.dumps(
        {
            "success": True,
            "message": f"Aborted {len(keys)} document(s)",
            "abortedCount": len(keys),
            "failedCount": 0,
            "errors": None,
        }
    ).encode()
    response = {
        "StatusCode": 200,
        "Payload": StreamingBody(io.BytesIO(body), len(body)),
    }
    validate_parameters(response, _INVOKE_OUTPUT_SHAPE)
    return response


def _dispatcher_read_timeout():
    """The dispatcher's read timeout, read from its source rather than restated."""
    import re

    found = re.findall(
        r"^_RESOLVER_READ_TIMEOUT_SECONDS\s*=\s*(\d+)\s*$",
        _DISPATCHER.read_text(),
        re.MULTILINE,
    )
    assert len(found) == 1, "the dispatcher's read timeout could not be read"
    return int(found[0])


class _SlowResource(_HoldBackResource):
    """The hold-back resource, with a clock that every call advances."""

    def __init__(self, real, hold, clock, cap=2_000):
        super().__init__(real, hold)
        self._clock = clock
        self._cap = cap

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource method
        return self._real.Table(name)

    def batch_get_item(self, RequestItems):
        self._clock.now += _CALL_LATENCY
        if len(self.calls) >= self._cap:
            raise AssertionError(f"batch_get_item called more than {self._cap} times")
        self.calls.append(len(RequestItems[_TABLE]["Keys"]))
        response = self._real.batch_get_item(RequestItems=RequestItems)
        items = response["Responses"][_TABLE]
        held = min(self._hold(len(self.calls), len(items)), len(items))
        if held:
            kept, back = items[: len(items) - held], items[len(items) - held :]
            response["Responses"][_TABLE] = kept
            response["UnprocessedKeys"] = {
                _TABLE: {"Keys": [{"PK": i["PK"], "SK": i["SK"]} for i in back]}
            }
        return response


@pytest.fixture
def in_flight_runs(abort_table, monkeypatch):
    """Three RUNNING runs of 120 documents each, none of which ever settles."""
    index, table, _keys, _install, clock = abort_table
    run_ids = ["run-a", "run-b", "run-c"]
    files = [f"doc-{n:03d}.pdf" for n in range(120)]
    for run_id in run_ids:
        table.put_item(
            Item={
                "PK": f"testrun#{run_id}",
                "SK": "metadata",
                "Status": "RUNNING",
                "Files": files,
            }
        )
        for name in files:
            table.put_item(
                Item={
                    "PK": f"doc#{run_id}/{name}",
                    "SK": "none",
                    "ObjectStatus": "RUNNING",
                }
            )
    monkeypatch.setenv("TRACKING_TABLE_NAME", _TABLE)
    monkeypatch.setenv("ABORT_WORKFLOW_FUNCTION_NAME", "abort-workflow")
    monkeypatch.setenv("TEST_RESULT_CACHE_UPDATE_QUEUE_URL", "https://queue")
    monkeypatch.setattr(
        index, "lambda_client", MagicMock(**{"invoke.side_effect": _workflow_answer})
    )
    monkeypatch.setattr(index, "sqs_client", MagicMock())
    # Every backoff draws its cap: deterministic, and the worst case.
    monkeypatch.setattr(index.random, "uniform", lambda low, high: high)

    def install(hold):
        import boto3

        real = boto3.resource("dynamodb", region_name="us-east-1")
        stub = _SlowResource(real, hold, clock)
        monkeypatch.setattr(index, "dynamodb", stub)
        return stub

    return index, table, run_ids, install, clock


def _ui_event(run_ids):
    return {
        "arguments": {"testRunIds": run_ids},
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
    }


class _RecordingTable:
    """The real table, recording on the fake clock when each run is marked ABORTED."""

    def __init__(self, real, clock, aborted):
        self._real = real
        self._clock = clock
        self._aborted = aborted
        self.table_name = real.table_name
        # Run id -> exception raised instead of serving that call, per method.
        self.get_item_errors = {}
        self.update_item_errors = {}

    def get_item(self, **kwargs):
        self._clock.now += _CALL_LATENCY
        run_id = kwargs["Key"]["PK"].split("#", 1)[1]
        if run_id in self.get_item_errors:
            raise self.get_item_errors[run_id]
        return self._real.get_item(**kwargs)

    def update_item(self, **kwargs):
        self._clock.now += _CALL_LATENCY
        run_id = kwargs["Key"]["PK"].split("#", 1)[1]
        if run_id in self.update_item_errors:
            raise self.update_item_errors[run_id]
        response = self._real.update_item(**kwargs)
        self._aborted[run_id] = self._clock.now
        return response


@pytest.fixture
def timed_runs(in_flight_runs, monkeypatch):
    """``in_flight_runs``, recording each run's dispatch and ABORTED times.

    Every API call the handler makes -- each run read, abort-workflow invoke,
    metrics message and status update -- costs one call latency of the fake
    clock, like every BatchGetItem call. ``install`` returns the recording table
    as well as the resource stub.
    """
    index, table, run_ids, install, clock = in_flight_runs
    dispatched = {}
    aborted = {}
    queued = []  # fake-clock time of each metrics message

    lock = __import__("threading").Lock()

    def invoke(**kwargs):
        # The invokes are made from worker threads, so the shared clock and the
        # record of each run's first dispatch are updated under a lock.
        with lock:
            clock.now += _CALL_LATENCY
            first_key = json.loads(kwargs["Payload"])["arguments"]["objectKeys"][0]
            run_id = first_key.split("/", 1)[0]
            dispatched[run_id] = min(dispatched.get(run_id, clock.now), clock.now)
        return _workflow_answer(**kwargs)

    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = invoke
    monkeypatch.setattr(index, "lambda_client", lambda_client)

    def send_message(**kwargs):
        clock.now += _CALL_LATENCY
        queued.append(clock.now)

    sqs_client = MagicMock()
    sqs_client.send_message.side_effect = send_message
    monkeypatch.setattr(index, "sqs_client", sqs_client)

    def install_timed(hold):
        stub = install(hold)
        recording = _RecordingTable(table, clock, aborted)
        monkeypatch.setattr(stub, "Table", lambda name: recording)
        stub.recording = recording
        stub.queued = queued
        stub.first_read = None
        batch_get_item = stub.batch_get_item

        def first_read_recorded(RequestItems):
            if stub.first_read is None:
                stub.first_read = clock.now
            return batch_get_item(RequestItems=RequestItems)

        monkeypatch.setattr(stub, "batch_get_item", first_read_recorded)
        return stub

    return index, table, run_ids, install_timed, clock, dispatched, aborted


# How much of the dispatcher's read timeout the handler must leave unused, for
# what the fake clock cannot see: cold start, real API latency, and the trip
# back through the dispatcher. ABORT_WAIT_BUDGET_SECONDS is chosen against it.
# Held here rather than beside the budget in the resolver, so that a change to
# the resolver cannot also loosen the test that bounds it.
_DISPATCHER_HEADROOM_SECONDS = 5

# The shortest settle wait the documents are given, the other side of the same
# choice: shrinking the budget to make room would mark runs ABORTED before
# documents that settle in a few seconds have done so.
_MIN_SETTLE_WAIT_SECONDS = 10


@pytest.mark.unit
@pytest.mark.parametrize("n_runs", [1, 2, 3])
@pytest.mark.parametrize(
    "hold",
    [
        pytest.param(lambda call, n: 0, id="every-read-succeeds"),
        pytest.param(lambda call, n: n, id="every-read-throttled"),
    ],
)
def test_an_abort_of_runs_that_never_settle_returns_inside_the_dispatcher_timeout(
    timed_runs, hold, n_runs
):
    """Never-settling runs, one request: one budget, and every run gets the wait.

    Every run's documents must get the settle wait, and its metrics must be
    queued with the pause taken, before the run is marked ABORTED -- not only the
    first run's: an early ABORTED lets the first read of the run persist its
    completed-file count, and its metrics are not computed again. So every run
    is dispatched before any document is read, every run's window from dispatch
    to ABORTED is at least _MIN_SETTLE_WAIT_SECONDS, no run is marked ABORTED
    before the request's deadline, and every metrics message is sent before
    the first run is marked ABORTED.
    """
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    run_ids = run_ids[:n_runs]
    stub = install(hold)
    started = clock.now

    result = index.lambda_handler(_ui_event(run_ids), None)

    elapsed = clock.now - started
    read_timeout = _dispatcher_read_timeout()
    assert elapsed <= read_timeout - _DISPATCHER_HEADROOM_SECONDS, (
        elapsed,
        read_timeout,
    )
    # The documents were polled at all (the bound is not met by skipping work).
    assert stub.calls
    assert set(dispatched) == set(run_ids)
    assert set(aborted) == set(run_ids)
    assert (
        index.ABORT_WAIT_BUDGET_SECONDS - index.METRICS_START_PAUSE_SECONDS
        >= _MIN_SETTLE_WAIT_SECONDS
    )
    # Every run is dispatched before the one settle wait begins.
    assert max(dispatched.values()) <= stub.first_read, (dispatched, stub.first_read)
    deadline = started + index.ABORT_WAIT_BUDGET_SECONDS
    for run_id in run_ids:
        window = aborted[run_id] - dispatched[run_id]
        assert window >= _MIN_SETTLE_WAIT_SECONDS, (run_id, window)
        assert aborted[run_id] >= deadline, (run_id, aborted[run_id] - started)
    # One metrics message per run, each naming its own run, all before any
    # run is marked ABORTED.
    assert max(stub.queued) < min(aborted.values()), (stub.queued, aborted)
    messages = [
        json.loads(call.kwargs["MessageBody"])["testRunId"]
        for call in index.sqs_client.send_message.call_args_list
    ]
    assert messages == run_ids, messages
    assert result["success"] is True
    assert result["abortedCount"] == len(run_ids)
    assert result["failedCount"] == 0
    for run_id in run_ids:
        item = table.get_item(Key={"PK": f"testrun#{run_id}", "SK": "metadata"})
        assert item["Item"]["Status"] == "ABORTED"


@pytest.mark.unit
def test_the_response_still_carries_every_field_the_ui_reads(in_flight_runs):
    """The UI's abort mutation selects fields the bounded handler still returns.

    The selection set is read from the UI's own operation document, and the run
    is ABORTED in the table by the time the response exists, which is what the
    UI's refetch after a successful abort displays.
    """
    import re

    index, table, run_ids, install, _clock = in_flight_runs
    install(lambda call, n: 0)

    result = index.lambda_handler(_ui_event(run_ids[:1]), None)

    body = _UI_ABORT_MUTATION.read_text()
    selection = body[body.index("abortTestRuns(") :]
    selection = selection[selection.index("{") + 1 : selection.index("}")]
    selected = set(re.findall(r"[A-Za-z_]\w*", selection))
    assert selected >= {"success", "abortedCount", "failedCount", "message"}, selected
    assert selected <= set(result), selected - set(result)
    assert result["success"] is True
    assert result["abortedCount"] == 1
    assert result["failedCount"] == 0
    assert result["message"] == "Aborted 1 test run(s)"
    item = table.get_item(Key={"PK": f"testrun#{run_ids[0]}", "SK": "metadata"})
    assert item["Item"]["Status"] == "ABORTED"


@pytest.mark.unit
@pytest.mark.parametrize(
    "hold",
    [
        pytest.param(lambda call, n: 0, id="every-read-succeeds"),
        pytest.param(lambda call, n: n, id="every-read-throttled"),
    ],
)
def test_the_settle_wait_itself_ends_at_its_deadline(in_flight_runs, hold, caplog):
    """The wait, measured alone: no poll, retry or sleep carries it past the deadline.

    Measured without the handler around it, because there the pause after
    queueing metrics comes out of the same deadline and absorbs an overshoot of
    up to its own length -- which would leave an unbounded retry or a full final
    poll interval invisible. Three runs' documents in one wait makes four chunks
    of BatchGetItem, so a chunk started past the deadline shows up as more than
    one call's latency of overshoot.
    """
    index, table, run_ids, install, clock = in_flight_runs
    install(hold)
    keys = [f"{run_id}/doc-{n:03d}.pdf" for run_id in run_ids for n in range(120)]
    started = clock.now
    deadline = started + 4

    with caplog.at_level("WARNING"):
        index._wait_for_documents_terminal_state(
            table, "run-a", keys, deadline=deadline
        )

    assert clock.now <= deadline + _CALL_LATENCY, clock.now - started
    # A chunk left unread for want of time is unknown, never "not found".
    assert "not found" not in caplog.text


@pytest.mark.unit
def test_one_run_that_never_settles_still_gets_its_pause_before_aborted(
    in_flight_runs,
):
    """The wait stops early enough to leave the metrics pause inside the deadline."""
    index, table, run_ids, install, clock = in_flight_runs
    install(lambda call, n: 0)

    index.lambda_handler(_ui_event(run_ids[:1]), None)

    assert clock.sleeps[-1] == index.METRICS_START_PAUSE_SECONDS, clock.sleeps[-3:]


@pytest.mark.unit
def test_the_settle_wait_with_no_bound_given_stops_at_the_budget(in_flight_runs):
    """With neither max_wait_time nor deadline, the wait is the request budget."""
    index, table, run_ids, install, clock = in_flight_runs
    install(lambda call, n: 0)
    keys = [f"{run_ids[0]}/doc-{n:03d}.pdf" for n in range(120)]
    started = clock.now

    index._wait_for_documents_terminal_state(table, run_ids[0], keys)

    elapsed = clock.now - started
    budget = index.ABORT_WAIT_BUDGET_SECONDS
    assert budget <= elapsed <= budget + _CALL_LATENCY, elapsed


@pytest.mark.unit
def test_a_dispatch_that_outlasts_the_budget_still_marks_the_runs_aborted(
    timed_runs,
):
    """No time left for the wait or the pause: the abort proceeds without them.

    The pause is clamped at zero rather than computed negative, which would
    raise from time.sleep outside any per-run handling and report every run as
    failed with none of them marked ABORTED.
    """
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    stub = install(lambda call, n: 0)
    invoke = index.lambda_client.invoke.side_effect

    def slow_invoke(**kwargs):
        answer = invoke(**kwargs)
        clock.now += index.ABORT_WAIT_BUDGET_SECONDS
        return answer

    index.lambda_client.invoke.side_effect = slow_invoke

    result = index.lambda_handler(_ui_event(run_ids), None)

    assert result["abortedCount"] == len(run_ids), result
    assert result["failedCount"] == 0, result
    assert not stub.calls
    assert all(seconds == 0 for seconds in clock.sleeps), clock.sleeps
    assert set(aborted) == set(run_ids)


@pytest.mark.unit
def test_a_run_named_twice_is_aborted_once(timed_runs):
    """A repeated id is refused rather than dispatched and waited on twice."""
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    install(lambda call, n: 0)

    result = index.lambda_handler(_ui_event([run_ids[0], run_ids[0]]), None)

    assert result["abortedCount"] == 1, result
    assert result["failedCount"] == 1, result
    # One run's 120 documents, in batches, once.
    assert index.lambda_client.invoke.call_count == -(-120 // index.ABORT_BATCH_SIZE)
    assert set(aborted) == {run_ids[0]}


@pytest.mark.unit
def test_a_settle_wait_that_raises_is_handled_as_one_that_ran_out(
    timed_runs, monkeypatch
):
    """The documents have been told to stop, so a failed wait does not fail the abort."""
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    install(lambda call, n: 0)

    def broken_wait(*args, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(index, "_wait_for_documents_terminal_state", broken_wait)

    result = index.lambda_handler(_ui_event(run_ids), None)

    assert result["abortedCount"] == len(run_ids), result
    assert result["failedCount"] == 0, result
    assert set(aborted) == set(run_ids)


@pytest.mark.unit
@pytest.mark.parametrize(
    "settled_first", [True, False], ids=["settled-first", "settled-last"]
)
def test_every_run_waits_for_its_own_documents_not_only_the_first_runs(
    timed_runs, settled_first
):
    """The one settle wait covers every run's documents.

    One run's documents are already settled and the other's never settle, so a
    wait over the settled run's documents alone -- whichever end of the request
    it is at -- would return at once and mark the other run ABORTED a moment
    after its dispatch.
    """
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    install(lambda call, n: 0)
    settled, unsettled = run_ids[:2]
    order = [settled, unsettled] if settled_first else [unsettled, settled]
    for n in range(120):
        table.update_item(
            Key={"PK": f"doc#{settled}/doc-{n:03d}.pdf", "SK": "none"},
            UpdateExpression="SET ObjectStatus = :s",
            ExpressionAttributeValues={":s": "ABORTED"},
        )

    result = index.lambda_handler(_ui_event(order), None)

    assert result["abortedCount"] == 2, result
    window = aborted[unsettled] - dispatched[unsettled]
    assert window >= _MIN_SETTLE_WAIT_SECONDS, window


@pytest.mark.unit
def test_every_document_is_told_to_stop_once_in_batches(
    timed_runs,
):
    """Each of a run's documents is in exactly one abort-workflow invoke.

    The invokes are concurrent, so the order they arrive in is not asserted.
    """
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    install(lambda call, n: 0)

    index.lambda_handler(_ui_event(run_ids[:1]), None)

    batches = [
        json.loads(call.kwargs["Payload"])["arguments"]["objectKeys"]
        for call in index.lambda_client.invoke.call_args_list
    ]
    assert all(0 < len(batch) <= index.ABORT_BATCH_SIZE for batch in batches), [
        len(b) for b in batches
    ]
    sent = sorted(key for batch in batches for key in batch)
    expected = [f"{run_ids[0]}/doc-{n:03d}.pdf" for n in range(120)]
    assert sent == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                "GetItem",
            ),
            "Database error: ",
            id="client-error",
        ),
        pytest.param(RuntimeError("connection reset"), "connection reset", id="other"),
    ],
)
def test_a_run_that_cannot_be_read_fails_alone(timed_runs, error, expected):
    """One run's read failing does not fail the runs around it."""
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    stub = install(lambda call, n: 0)
    stub.recording.get_item_errors[run_ids[1]] = error

    result = index.lambda_handler(_ui_event(run_ids), None)

    assert result["abortedCount"] == 2, result
    assert result["failedCount"] == 1, result
    assert len(result["errors"]) == 1, result
    assert result["errors"][0].startswith(f"{run_ids[1]}: {expected}"), result
    assert set(aborted) == {run_ids[0], run_ids[2]}
    assert run_ids[1] not in dispatched


@pytest.mark.unit
def test_a_status_update_that_fails_is_counted_as_a_failure(timed_runs):
    """The conditional ABORTED update failing for one run fails that run only."""
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    stub = install(lambda call, n: 0)
    stub.recording.update_item_errors[run_ids[0]] = RuntimeError("condition")

    result = index.lambda_handler(_ui_event(run_ids), None)

    assert result["success"] is True
    assert result["abortedCount"] == 2, result
    assert result["failedCount"] == 1, result
    assert result["errors"] == [f"{run_ids[0]}: Failed to update status: condition"]
    assert result["message"] == "Aborted 2 test run(s), 1 failed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("read_latency", "waited_for"),
    [
        pytest.param(0.0, "all", id="fast"),
        pytest.param(0.3, "all", id="0.3s-per-run"),
        # The cut falls among the invokes: the first are waited for, the rest
        # are sent asynchronously (each invoke costs one call latency here).
        pytest.param(0.6, "some", id="0.6s-per-run"),
        pytest.param(3.0, "none", id="3s-per-run"),
    ],
)
def test_every_synchronous_invoke_is_bounded_to_end_before_the_pause(
    timed_runs, read_latency, waited_for
):
    """The abort-workflow answers are waited for only inside the budget (#1609).

    Every synchronous invoke must start early enough that its connect and read
    bounds -- which botocore enforces, and which the module's client is asserted
    to carry -- end by the time the metrics pause begins; a batch reached later is
    sent asynchronously instead. The run reads here each cost ``read_latency`` of
    the fake clock, so the later cases leave less of the budget when the invokes
    begin. Whatever the split, every batch is sent, and the request still ends
    inside the dispatcher's read timeout.
    """
    index, table, run_ids, install, clock, dispatched, aborted = timed_runs
    stub = install(lambda call, n: 0)
    recording = stub.recording
    get_item = recording.get_item

    def slow_get_item(**kwargs):
        clock.now += read_latency
        return get_item(**kwargs)

    recording.get_item = slow_get_item
    starts = []
    invoke = index.lambda_client.invoke.side_effect

    def recording_invoke(**kwargs):
        starts.append((kwargs["InvocationType"], clock.now))
        return invoke(**kwargs)

    index.lambda_client.invoke.side_effect = recording_invoke
    started = clock.now

    result = index.lambda_handler(_ui_event(run_ids), None)

    bound = (
        index.ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS
        + index.ABORT_INVOKE_READ_TIMEOUT_SECONDS
    )
    pause_starts = (
        started + index.ABORT_WAIT_BUDGET_SECONDS - index.METRICS_START_PAUSE_SECONDS
    )
    sync = [at for kind, at in starts if kind == "RequestResponse"]
    late = [at for kind, at in starts if kind == "Event"]
    assert set(kind for kind, _ in starts) <= {"RequestResponse", "Event"}
    assert all(at + bound <= pause_starts for at in sync), (sync, pause_starts)
    assert all(at + bound > pause_starts for at in late), (late, pause_starts)
    assert len(starts) == len(run_ids) * -(-120 // index.ABORT_BATCH_SIZE)
    if waited_for == "all":
        assert sync and not late and result["errors"] is None, (starts, result)
    else:
        assert late and bool(sync) == (waited_for == "some"), starts
        assert result["errors"], result
        assert all("sent without waiting" in e for e in result["errors"]), result
    assert clock.now - started <= (
        _dispatcher_read_timeout() - _DISPATCHER_HEADROOM_SECONDS
    ), clock.now - started
    assert set(aborted) == set(run_ids)
