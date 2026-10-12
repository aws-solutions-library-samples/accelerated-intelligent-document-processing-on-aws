# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every EvaluationStatus the evaluation function writes must end a document's
wait in the readers that decide whether a test run is finished.

The universe is read from the writer's own ``EvaluationStatus`` enum rather than
restated here, so a status added there fails this module until both readers
classify it. A reader that does not recognise a finished status counts the
document as still evaluating, which holds the whole run in EVALUATING with no
way out: an EVALUATING run cannot be aborted.
"""

import ast
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
EVALUATION_FUNCTION = REPO_ROOT / "patterns/unified/src/evaluation_function/index.py"
RESULTS_RESOLVER = (
    REPO_ROOT / "nested/api-resolvers/src/lambda/test_results_resolver/index.py"
)
ABORT_TEST_RUNS = REPO_ROOT / "nested/api-resolvers/src/lambda/abort_test_runs/index.py"

IN_PROGRESS = {"RUNNING"}


def _evaluation_statuses_written():
    tree = ast.parse(EVALUATION_FUNCTION.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "EvaluationStatus":
            return {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            }
    raise AssertionError(f"EvaluationStatus enum not found in {EVALUATION_FUNCTION}")


WRITTEN = _evaluation_statuses_written()
FINISHED = sorted(WRITTEN - IN_PROGRESS)


def _load(path, name):
    with patch("boto3.resource"), patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


resolver = _load(RESULTS_RESOLVER, "evaluation_status_readers_results_resolver")
abort_runs = _load(ABORT_TEST_RUNS, "evaluation_status_readers_abort_test_runs")


def _tracking_table(run_id, eval_statuses):
    files = [f"doc{i}.pdf" for i in range(len(eval_statuses))]
    metadata = {
        "PK": f"testrun#{run_id}",
        "SK": "metadata",
        "Status": "RUNNING",
        "Files": files,
        "FilesCount": len(files),
    }
    docs = {
        f"doc#{run_id}/{name}": {
            "ObjectStatus": "COMPLETED",
            "EvaluationStatus": status,
            "CompletionTime": "2026-09-30T16:15:01.406703+00:00",
        }
        for name, status in zip(files, eval_statuses)
    }

    def get_item(Key):
        if Key["PK"] == metadata["PK"]:
            return {"Item": metadata}
        return {"Item": docs[Key["PK"]]}

    table = Mock()
    table.get_item.side_effect = get_item
    return table


def _run_status(eval_statuses):
    run_id = "RealKIE-FCC-Verified-20260930-153416"
    table = _tracking_table(run_id, eval_statuses)
    with (
        patch.dict("os.environ", {"TRACKING_TABLE": "tracking"}),
        patch.object(resolver.dynamodb, "Table", return_value=table),
        patch.object(resolver, "_queue_cache_update"),
    ):
        result = resolver.get_test_run_status(run_id)
    return result, table


@pytest.mark.unit
def test_the_writer_enum_was_read():
    assert {"RUNNING", "COMPLETED", "FAILED", "TIMED_OUT"} <= WRITTEN


@pytest.mark.unit
@pytest.mark.parametrize("status", FINISHED)
def test_run_status_counts_every_finished_evaluation_as_finished(status):
    result, _ = _run_status([status])

    assert result["evaluatingFiles"] == 0
    assert result["completedFiles"] + result["failedFiles"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", sorted(IN_PROGRESS))
def test_run_status_still_waits_on_a_running_evaluation(status):
    result, _ = _run_status([status])

    assert result["evaluatingFiles"] == 1


@pytest.mark.unit
def test_a_timed_out_evaluation_ends_the_run_partially_complete():
    result, table = _run_status(["COMPLETED", "TIMED_OUT"])

    assert result["completedFiles"] == 1
    assert result["failedFiles"] == 1
    written = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert written[":status"] == "PARTIAL_COMPLETE"
    assert written[":failedFiles"] == 1
    assert written[":completedAt"] == "2026-09-30T16:15:01.406703Z"


def _abort_polls(status):
    table = Mock()
    table.table_name = "tracking"
    key = "run/doc.pdf"
    item = {"ObjectStatus": "COMPLETED", "EvaluationStatus": status}
    with (
        patch.object(
            abort_runs, "_batch_get_document_items", return_value={key: item}
        ) as batch_get,
        patch.object(abort_runs.time, "sleep"),
    ):
        abort_runs._wait_for_documents_terminal_state(
            table, "run", [key], max_wait_time=0.05
        )
    return batch_get.call_count


@pytest.mark.unit
@pytest.mark.parametrize("status", FINISHED)
def test_abort_does_not_wait_on_a_finished_evaluation(status):
    assert _abort_polls(status) == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", sorted(IN_PROGRESS))
def test_abort_waits_on_a_running_evaluation(status):
    assert _abort_polls(status) > 1
