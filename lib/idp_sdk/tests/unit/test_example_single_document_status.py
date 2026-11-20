# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two examples that report one document's status as a batch count it once.

`examples/lambda_function.py` and `examples/workflow_control.py` turn a single
`DocumentStatus` into a `BatchStatus`. They compared the status with a hand-picked
list that held `PROCESSING`, which no document carries, and none of the step
statuses, so a document at OCR or EXTRACTING was counted nowhere and never read as
complete (#2199). Every `DocumentState` is driven through both examples here.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from idp_sdk import DocumentBucket, DocumentState, DocumentStatus

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"example_{name}", EXAMPLES / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_returning(state):
    client = Mock()
    client.document.get_status.return_value = DocumentStatus(
        document_id="b/a.pdf", status=state
    )
    return client


def _counts(body):
    return {
        DocumentBucket.COMPLETED: body["completed"],
        DocumentBucket.FAILED: body["failed"],
        DocumentBucket.RUNNING: body["in_progress"],
        DocumentBucket.QUEUED: body["queued"],
    }


def _lambda_status(state):
    module = _load("lambda_function")
    with patch("idp_sdk.IDPClient", return_value=_client_returning(state)):
        response = module.get_status({"document_id": "b/a.pdf"}, "stack", None)
    assert response["statusCode"] == 200
    return json.loads(response["body"])


@pytest.mark.unit
@pytest.mark.parametrize("state", list(DocumentState))
def test_the_lambda_example_counts_every_status_exactly_once(state):
    body = _lambda_status(state)
    counted = sum(_counts(body).values())
    # The lambda body has no awaiting_review field, so a held document is the one
    # state counted under none of the four it reports.
    from idp_sdk import classify_document_state

    bucket = classify_document_state(state)
    assert counted == (0 if bucket == DocumentBucket.AWAITING_REVIEW else 1)
    if bucket != DocumentBucket.AWAITING_REVIEW:
        assert _counts(body)[bucket] == 1
    assert body["success_rate"] == (1.0 if bucket == DocumentBucket.COMPLETED else 0.0)
    # BatchStatus.all_complete: true once no document is queued or running.
    assert body["all_complete"] is (
        bucket not in (DocumentBucket.QUEUED, DocumentBucket.RUNNING)
    )


@pytest.mark.unit
def test_a_document_at_a_processing_step_is_in_progress_in_the_lambda_example():
    body = _lambda_status(DocumentState.EXTRACTING)

    assert body["in_progress"] == 1
    assert body["all_complete"] is False


def _workflow_control_report(state, capsys):
    module = _load("workflow_control")
    argv = ["workflow_control.py", "--stack-name", "stack", "status"]
    argv += ["--document-id", "b/a.pdf"]
    with patch.object(module, "IDPClient", return_value=_client_returning(state)):
        with patch("sys.argv", argv):
            module.main()
    report = {}
    for line in capsys.readouterr().out.splitlines():
        key, sep, value = line.strip().partition(": ")
        if sep and key in {
            "Completed",
            "Failed",
            "In Progress",
            "Queued",
            "Success Rate",
            "All Complete",
        }:
            report.setdefault(key, value)
    return report


@pytest.mark.unit
@pytest.mark.parametrize("state", list(DocumentState))
def test_the_workflow_control_example_counts_every_status_exactly_once(state, capsys):
    from idp_sdk import classify_document_state

    bucket = classify_document_state(state)
    report = _workflow_control_report(state, capsys)

    expected = {
        "Completed": DocumentBucket.COMPLETED,
        "Failed": DocumentBucket.FAILED,
        "In Progress": DocumentBucket.RUNNING,
        "Queued": DocumentBucket.QUEUED,
    }
    for label, counted_bucket in expected.items():
        assert report[label] == ("1" if bucket == counted_bucket else "0"), label
    assert report["Success Rate"] == (
        "100.0%" if bucket == DocumentBucket.COMPLETED else "0.0%"
    )
    assert report["All Complete"] == str(
        bucket not in (DocumentBucket.QUEUED, DocumentBucket.RUNNING)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "state, label, value",
    [
        (DocumentState.OCR, "In Progress", "1"),
        (DocumentState.EXTRACTING, "In Progress", "1"),
        (DocumentState.REJECTED, "Failed", "1"),
        (DocumentState.HITL_IN_PROGRESS, "All Complete", "True"),
    ],
)
def test_the_workflow_control_example_counts_a_step_status(state, label, value, capsys):
    assert _workflow_control_report(state, capsys)[label] == value
