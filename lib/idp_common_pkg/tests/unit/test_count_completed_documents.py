# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for _count_completed_documents in test_results_resolver.

The documents live in a moto table and are read through the real
``DynamoDBClient.batch_get_items``, not a double of it: its UnprocessedKeys retry
is what the count relies on. A partial response is produced at moto's service
handler, which withholds chosen keys in UnprocessedKeys, the shape DynamoDB
returns under throttling.
"""

import importlib.util
import json
import os
from unittest.mock import Mock, patch

import boto3
import pytest
from moto import mock_aws
from moto.dynamodb.exceptions import ResourceNotFoundException
from moto.dynamodb.responses import DynamoHandler

from idp_common.dynamodb.client import DynamoDBError, DynamoDBUnprocessedKeysError

REGION = "us-east-1"
TABLE = "tracking"
RUN = "test-run-123"

# The module builds boto3 clients at import, which needs a region.
with patch("boto3.resource"), patch("boto3.client"):
    _spec = importlib.util.spec_from_file_location(
        "count_completed_index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/test_results_resolver/index.py",
        ),
    )
    assert _spec is not None and _spec.loader is not None
    index = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(index)


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        yield ddb.create_table(
            TableName=TABLE,
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


def _put_docs(table, statuses):
    """One document per file; ``None`` writes a document with no status."""
    files = []
    for i, status in enumerate(statuses):
        name = f"file{i}.pdf"
        item = {"PK": f"doc#{RUN}/{name}", "SK": "none", "ObjectStatus": "COMPLETED"}
        if status is not None:
            item["EvaluationStatus"] = status
        table.put_item(Item=item)
        files.append(name)
    return files


class _Withholding:
    """Make moto's BatchGetItem leave chosen keys in UnprocessedKeys.

    Patched at moto's service handler, so it is independent of how the code
    under test reaches BatchGetItem (low-level client, resource, or the shared
    helper). ``withhold_calls`` is how many leading calls withhold the keys;
    every call after that is answered in full. ``calls`` counts the service
    calls, and backoff sleeps are recorded instead of slept. More than
    ``CALL_CAP`` calls fails the test, so a reader that never stops retrying
    ends here rather than running unbounded.
    """

    CALL_CAP = 50

    def __init__(self, withheld_pks, withhold_calls):
        self.withheld = set(withheld_pks)
        self.withhold_calls = withhold_calls
        self.calls = 0
        self.sleeps = []

    def _handler(self, handler_self):
        self.calls += 1
        if self.calls > self.CALL_CAP:
            raise AssertionError(f"BatchGetItem called more than {self.CALL_CAP} times")
        body = json.loads(self._real(handler_self))
        if self.calls > self.withhold_calls:
            return json.dumps(body)
        for name, request in handler_self.body["RequestItems"].items():
            held = [k for k in request["Keys"] if k["PK"]["S"] in self.withheld]
            if not held:
                continue
            body["Responses"][name] = [
                item
                for item in body["Responses"].get(name, [])
                if item["PK"]["S"] not in self.withheld
            ]
            body["UnprocessedKeys"][name] = {"Keys": held}
        return json.dumps(body)

    def __enter__(self):
        self._real = DynamoHandler.batch_get_item
        outer = self
        self._patches = [
            patch.object(
                DynamoHandler,
                "batch_get_item",
                lambda handler_self: outer._handler(handler_self),
            ),
            patch(
                "idp_common.dynamodb.client.time.sleep",
                side_effect=self.sleeps.append,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


@pytest.mark.unit
def test_counts_settled_statuses_and_nothing_else(table):
    files = _put_docs(
        table,
        ["COMPLETED", "NO_BASELINE", "DISABLED", "FAILED", "RUNNING", None],
    )
    files.append("never-written.pdf")
    with _Withholding(set(), 0) as w:
        assert index._count_completed_documents(table, RUN, files) == 3
    assert w.calls == 1


@pytest.mark.unit
def test_status_is_matched_case_insensitively(table):
    files = _put_docs(table, ["completed", "Completed", "failed"])
    with _Withholding(set(), 0):
        assert index._count_completed_documents(table, RUN, files) == 2


@pytest.mark.unit
def test_a_status_that_is_not_a_string_is_not_counted(table):
    """Only a string can be a stored status; anything else is not settled, and
    must not stop the documents after it being counted."""
    files = _put_docs(table, [7, {"COMPLETED"}, "COMPLETED"])
    with _Withholding(set(), 0):
        assert index._count_completed_documents(table, RUN, files) == 1


@pytest.mark.unit
def test_unprocessed_keys_are_retried_and_counted(table):
    """A key DynamoDB leaves unprocessed once is read on the retry, so the count
    covers every document. Before the helper this returned 2 of 3."""
    files = _put_docs(table, ["COMPLETED", "COMPLETED", "COMPLETED"])
    with _Withholding({f"doc#{RUN}/file1.pdf"}, withhold_calls=1) as w:
        assert index._count_completed_documents(table, RUN, files) == 3
    assert w.calls == 2
    assert len(w.sleeps) == 1  # backed off before the retry


@pytest.mark.unit
def test_more_than_one_batch_of_documents_is_counted_in_full(table):
    """BatchGetItem takes at most 100 keys; a run larger than that is read in
    several calls, and a key withheld in one of them is still counted."""
    files = _put_docs(table, ["COMPLETED"] * 150)
    with _Withholding({f"doc#{RUN}/file120.pdf"}, withhold_calls=2) as w:
        assert index._count_completed_documents(table, RUN, files) == 150
    assert w.calls == 3


@pytest.mark.unit
def test_keys_left_unprocessed_raise_rather_than_undercount(table):
    files = _put_docs(table, ["COMPLETED", "COMPLETED", "COMPLETED"])
    with _Withholding({f"doc#{RUN}/file1.pdf"}, withhold_calls=10**6) as w:
        with pytest.raises(DynamoDBUnprocessedKeysError):
            index._count_completed_documents(table, RUN, files)
    # The helper's default attempt budget, then it gives up.
    assert w.calls == 6


@pytest.mark.unit
def test_a_service_error_raises_rather_than_counting_zero(table):
    files = _put_docs(table, ["COMPLETED"])

    def failing(handler_self):
        raise ResourceNotFoundException()

    with patch.object(DynamoHandler, "batch_get_item", failing):
        with pytest.raises(DynamoDBError):
            index._count_completed_documents(table, RUN, files)


@pytest.mark.unit
def test_no_files_reads_nothing(table):
    with _Withholding(set(), 0) as w:
        assert index._count_completed_documents(table, RUN, []) == 0
    assert w.calls == 0


def _aborted_metadata_table(files):
    """Mock tracking-table resource whose metadata row is an uncounted ABORTED
    run. Documents are read from moto through the helper, by table name."""
    mock_table = Mock()
    mock_table.table_name = TABLE
    mock_table.get_item.return_value = {
        "Item": {
            "PK": f"testrun#{RUN}",
            "SK": "metadata",
            "Status": "ABORTED",
            "Files": files,
            "FilesCount": len(files),
            "testRunResult": {
                "splitClassificationMetrics": {},
                "confusionMatrix": {},
                "fieldMetrics": {},
                "gradedPacketMetrics": {},
                "excludedDocumentCount": 0,
                "classificationErrors": {},
                "weightedOverallScores": {},
            },
        }
    }
    return mock_table


def _get_results(mock_table):
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": TABLE}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "_get_test_run_config", return_value=None),
        patch.object(index, "_queue_cache_update"),
    ):
        return index.get_test_results(RUN)


@pytest.mark.unit
def test_aborted_run_persists_a_complete_count(table):
    files = _put_docs(table, ["COMPLETED", "COMPLETED", "FAILED"])
    mock_table = _aborted_metadata_table(files)
    with _Withholding({f"doc#{RUN}/file0.pdf"}, withhold_calls=1):
        result = _get_results(mock_table)
    assert result["completedFiles"] == 2
    values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values == {":completed_files": 2, ":counted": True}


@pytest.mark.unit
def test_aborted_run_does_not_persist_a_partial_count(table):
    """Persisting sets CompletedFilesCounted, after which the figure is never
    recounted, so an undercount written here would be permanent. The request
    fails instead and nothing is written; the next one counts again."""
    files = _put_docs(table, ["COMPLETED", "COMPLETED", "COMPLETED"])
    mock_table = _aborted_metadata_table(files)
    with _Withholding({f"doc#{RUN}/file2.pdf"}, withhold_calls=10**6):
        with pytest.raises(DynamoDBUnprocessedKeysError):
            _get_results(mock_table)
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_comparison_path_returns_no_count_and_saves_nothing(table):
    files = _put_docs(table, ["COMPLETED", "COMPLETED", "COMPLETED"])
    mock_table = _aborted_metadata_table(files)
    with _Withholding({f"doc#{RUN}/file2.pdf"}, withhold_calls=10**6):
        with (
            patch.dict(os.environ, {"TRACKING_TABLE": TABLE}),
            patch.object(index.dynamodb, "Table", return_value=mock_table),
            patch.object(index, "_get_test_run_config", return_value=None),
            patch.object(index, "_queue_cache_update"),
        ):
            result = index.get_test_results(RUN, unreadable_count_as_none=True)
    assert result["status"] == "ABORTED"
    assert result["completedFiles"] is None
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_compare_survives_one_run_whose_count_cannot_be_read(table):
    """One aborted run whose count cannot be read must not fail the comparison
    of the others; it comes back with no count, and nothing is saved."""
    files = _put_docs(table, ["COMPLETED", "COMPLETED"])
    mock_table = _aborted_metadata_table(files)
    with _Withholding({f"doc#{RUN}/file1.pdf"}, withhold_calls=10**6):
        with (
            patch.dict(os.environ, {"TRACKING_TABLE": TABLE}),
            patch.object(index.dynamodb, "Table", return_value=mock_table),
            patch.object(index, "_get_test_run_config", return_value={}),
            patch.object(index, "_queue_cache_update"),
            patch.object(index, "_load_sample_attribute_methods", return_value={}),
        ):
            out = index.compare_test_runs([RUN, RUN])
    assert out["metrics"][RUN]["status"] == "ABORTED"
    assert out["metrics"][RUN]["completedFiles"] is None
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_comparison_path_degrades_a_service_error_too(table):
    """Every DynamoDBError, not only exhausted UnprocessedKeys."""
    files = _put_docs(table, ["COMPLETED"])
    mock_table = _aborted_metadata_table(files)

    def failing(handler_self):
        raise ResourceNotFoundException()

    with (
        patch.object(DynamoHandler, "batch_get_item", failing),
        patch.dict(os.environ, {"TRACKING_TABLE": TABLE}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "_get_test_run_config", return_value=None),
        patch.object(index, "_queue_cache_update"),
    ):
        result = index.get_test_results(RUN, unreadable_count_as_none=True)
    assert result["completedFiles"] is None
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_comparison_path_does_not_swallow_other_errors(table):
    """Only a failed read is degraded; a defect in the count still surfaces."""
    files = _put_docs(table, ["COMPLETED"])
    mock_table = _aborted_metadata_table(files)
    with (
        patch.object(index, "_count_completed_documents", side_effect=TypeError("bug")),
        patch.dict(os.environ, {"TRACKING_TABLE": TABLE}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "_get_test_run_config", return_value=None),
        patch.object(index, "_queue_cache_update"),
    ):
        with pytest.raises(TypeError):
            index.get_test_results(RUN, unreadable_count_as_none=True)
    mock_table.update_item.assert_not_called()
