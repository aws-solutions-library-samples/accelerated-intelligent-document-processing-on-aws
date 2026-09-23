# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for idp_common.monitoring.stepfunctions_service
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import idp_common.monitoring.stepfunctions_service as sf_module
from idp_common.monitoring.stepfunctions_service import (
    analyze_execution_timeline,
    extract_failure_details,
    get_execution_arn_from_document,
    get_execution_data,
)


# ---------------------------------------------------------------------------
# Reset module-level boto3 client cache between tests.
# _sf_clients is a dict keyed by region; clear it between tests.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_sf_client():
    """Reset the module-level _sf_clients dict before every test."""
    sf_module._sf_clients.clear()
    yield
    sf_module._sf_clients.clear()


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

_EXEC_ARN = (
    "arn:aws:states:us-east-1:123:execution:MY-STACK-DocumentProcessingWorkflow:exec-1"
)

_START_TS = "2026-03-01T10:00:00+00:00"
_STOP_TS = "2026-03-01T10:02:00+00:00"  # 2 minutes later


def _make_sf_client(status="SUCCEEDED", events=None):
    """Build a mock Step Functions client."""
    mock = MagicMock()
    mock.exceptions.ExecutionDoesNotExist = type(
        "ExecutionDoesNotExist", (Exception,), {}
    )

    start_dt = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    stop_dt = datetime(2026, 3, 1, 10, 2, 0, tzinfo=timezone.utc)

    mock.describe_execution.return_value = {
        "status": status,
        "startDate": start_dt,
        "stopDate": stop_dt,
        "input": '{"key": "value"}',
    }

    paginator = MagicMock()
    paginator.paginate.return_value = [{"events": events or []}]
    mock.get_paginator.return_value = paginator

    return mock


def _make_events(include_failure=False, failure_type="ExecutionFailed"):
    """Build a minimal list of Step Functions execution history events."""
    events = [
        {"id": 1, "type": "ExecutionStarted", "timestamp": _START_TS},
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": "2026-03-01T10:00:01+00:00",
            "stateEnteredEventDetails": {"name": "ClassifyDocument"},
        },
        {
            "id": 3,
            "type": "TaskStateExited",
            "timestamp": "2026-03-01T10:01:00+00:00",
            "stateExitedEventDetails": {"name": "ClassifyDocument"},
        },
        {
            "id": 4,
            "type": "TaskStateEntered",
            "timestamp": "2026-03-01T10:01:00+00:00",
            "stateEnteredEventDetails": {"name": "ExtractDocument"},
        },
    ]

    if include_failure:
        if failure_type == "ExecutionFailed":
            events.append(
                {
                    "id": 5,
                    "type": "ExecutionFailed",
                    "timestamp": _STOP_TS,
                    "executionFailedEventDetails": {
                        "error": "States.TaskFailed",
                        "cause": "Lambda returned an error",
                    },
                }
            )
        elif failure_type == "TaskFailed":
            events.append(
                {
                    "id": 5,
                    "type": "TaskFailed",
                    "timestamp": _STOP_TS,
                    "taskFailedEventDetails": {
                        "error": "ThrottlingException",
                        "cause": "Rate exceeded",
                        "resource": "arn:aws:lambda:::function:fn",
                    },
                }
            )
        elif failure_type == "LambdaFunctionFailed":
            events.append(
                {
                    "id": 5,
                    "type": "LambdaFunctionFailed",
                    "timestamp": _STOP_TS,
                    "lambdaFunctionFailedEventDetails": {
                        "error": "RuntimeError",
                        "cause": "Division by zero",
                    },
                }
            )
        elif failure_type == "TaskTimedOut":
            events.append(
                {
                    "id": 5,
                    "type": "TaskTimedOut",
                    "timestamp": _STOP_TS,
                    "taskTimedOutEventDetails": {
                        "error": "States.Timeout",
                        "cause": "Task exceeded timeout",
                    },
                }
            )
        elif failure_type == "LambdaFunctionTimedOut":
            events.append(
                {
                    "id": 5,
                    "type": "LambdaFunctionTimedOut",
                    "timestamp": _STOP_TS,
                    "lambdaFunctionTimedOutEventDetails": {
                        "error": "States.Timeout",
                        "cause": "Lambda timed out",
                    },
                }
            )
        elif failure_type == "ActivityFailed":
            events.append(
                {
                    "id": 5,
                    "type": "ActivityFailed",
                    "timestamp": _STOP_TS,
                    "activityFailedEventDetails": {
                        "error": "ActivityError",
                        "cause": "Activity failed",
                    },
                }
            )
    return events


# ---------------------------------------------------------------------------
# get_execution_arn_from_document
# ---------------------------------------------------------------------------


class TestGetExecutionArnFromDocument:
    def test_from_dict_workflow_execution_arn(self):
        doc = {"WorkflowExecutionArn": _EXEC_ARN}
        assert get_execution_arn_from_document(doc) == _EXEC_ARN

    def test_from_dict_execution_arn_fallback(self):
        doc = {"ExecutionArn": _EXEC_ARN}
        assert get_execution_arn_from_document(doc) == _EXEC_ARN

    def test_from_dict_snake_case_fallback(self):
        doc = {"workflow_execution_arn": _EXEC_ARN}
        assert get_execution_arn_from_document(doc) == _EXEC_ARN

    def test_from_empty_dict_returns_empty(self):
        assert get_execution_arn_from_document({}) == ""

    def test_from_dataclass_like_object(self):
        class FakeRecord:
            workflow_execution_arn = _EXEC_ARN

        assert get_execution_arn_from_document(FakeRecord()) == _EXEC_ARN

    def test_from_dataclass_returns_empty_if_missing(self):
        class FakeRecord:
            pass

        assert get_execution_arn_from_document(FakeRecord()) == ""


# ---------------------------------------------------------------------------
# get_execution_data
# ---------------------------------------------------------------------------


class TestGetExecutionData:
    def test_returns_correct_status(self):
        sf = _make_sf_client(status="SUCCEEDED", events=_make_events())
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            result = get_execution_data(_EXEC_ARN)

        assert result["status"] == "SUCCEEDED"
        assert result["execution_arn"] == _EXEC_ARN

    def test_paginates_events(self):
        events = _make_events()
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            result = get_execution_data(_EXEC_ARN)

        assert len(result["events"]) == len(events)

    def test_timestamps_serialised_to_strings(self):
        sf = _make_sf_client(status="SUCCEEDED", events=_make_events())
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            result = get_execution_data(_EXEC_ARN)

        assert isinstance(result["start_date"], str)
        assert isinstance(result["stop_date"], str)

    def test_handles_missing_execution_gracefully(self):
        sf = MagicMock()
        sf.exceptions.ExecutionDoesNotExist = type(
            "ExecutionDoesNotExist", (Exception,), {}
        )
        sf.describe_execution.side_effect = sf.exceptions.ExecutionDoesNotExist(
            "not found"
        )

        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            result = get_execution_data(_EXEC_ARN)

        # ExecutionDoesNotExist → NOT_FOUND (not UNKNOWN, which is for API errors)
        assert result["status"] == "NOT_FOUND"
        assert result["events"] == []

    def test_generic_api_error_returns_unknown_status(self):
        """A transient API error must leave status as UNKNOWN, not NOT_FOUND."""
        sf = MagicMock()
        sf.exceptions.ExecutionDoesNotExist = type(
            "ExecutionDoesNotExist", (Exception,), {}
        )
        sf.describe_execution.side_effect = Exception("Service unavailable")

        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            result = get_execution_data(_EXEC_ARN)

        assert result["status"] == "UNKNOWN"
        assert result["events"] == []


# ---------------------------------------------------------------------------
# analyze_execution_timeline
# ---------------------------------------------------------------------------


class TestAnalyzeExecutionTimeline:
    def test_calculates_correct_state_durations(self):
        events = _make_events(include_failure=False)
        sf = _make_sf_client(status="SUCCEEDED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        # ClassifyDocument entered at T+1s, exited at T+60s → 59 seconds
        classify_state = next(
            (s for s in timeline["states"] if s["name"] == "ClassifyDocument"), None
        )
        assert classify_state is not None
        assert classify_state["duration_ms"] == pytest.approx(59000.0, abs=100)
        assert classify_state["is_failure"] is False

    def test_identifies_failed_state(self):
        events = _make_events(include_failure=True, failure_type="ExecutionFailed")
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        assert timeline["overall_status"] == "FAILED"
        # ExecutionFailed maps to the last entered state: ExtractDocument
        assert timeline["failed_state"] == "ExtractDocument"

    def test_total_duration_ms_computed(self):
        events = _make_events()
        sf = _make_sf_client(status="SUCCEEDED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        # start=10:00:00, stop=10:02:00 → 120 seconds = 120000 ms
        assert timeline["total_duration_ms"] == pytest.approx(120000.0, abs=100)

    def test_empty_events_returns_empty_states(self):
        sf = _make_sf_client(status="SUCCEEDED", events=[])
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        assert timeline["states"] == []
        assert timeline["failed_state"] == ""

    def test_task_failed_event_records_state_name_from_prior_entered(self):
        """
        TaskFailed does not carry stateExitedEventDetails, so the state
        name must be inferred from the last TaskStateEntered event.
        """
        events = _make_events(include_failure=True, failure_type="TaskFailed")
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        # The last entered state before TaskFailed is ExtractDocument
        assert timeline["failed_state"] == "ExtractDocument"
        failed_entries = [s for s in timeline["states"] if s["is_failure"]]
        assert len(failed_entries) == 1
        assert failed_entries[0]["name"] == "ExtractDocument"
        assert failed_entries[0]["name"] != ""  # must not be empty string

    def test_task_timed_out_event_records_state_name(self):
        """
        TaskTimedOut does not carry stateExitedEventDetails either.
        """
        events = _make_events(include_failure=True, failure_type="TaskTimedOut")
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        assert timeline["failed_state"] == "ExtractDocument"
        failed_entries = [s for s in timeline["states"] if s["is_failure"]]
        assert failed_entries[0]["name"] == "ExtractDocument"


# ---------------------------------------------------------------------------
# extract_failure_details — all 6 failure event types
# ---------------------------------------------------------------------------


class TestExtractFailureDetails:
    def test_execution_failed(self):
        events = _make_events(include_failure=True, failure_type="ExecutionFailed")
        result = extract_failure_details(events)
        assert result["error"] == "States.TaskFailed"
        assert result["cause"] == "Lambda returned an error"
        assert result["event_type"] == "ExecutionFailed"

    def test_task_failed(self):
        events = _make_events(include_failure=True, failure_type="TaskFailed")
        result = extract_failure_details(events)
        assert result["error"] == "ThrottlingException"
        assert result["cause"] == "Rate exceeded"
        assert result["event_type"] == "TaskFailed"

    def test_lambda_function_failed(self):
        events = _make_events(include_failure=True, failure_type="LambdaFunctionFailed")
        result = extract_failure_details(events)
        assert result["error"] == "RuntimeError"
        assert result["event_type"] == "LambdaFunctionFailed"

    def test_task_timed_out(self):
        events = _make_events(include_failure=True, failure_type="TaskTimedOut")
        result = extract_failure_details(events)
        assert result["error"] == "States.Timeout"
        assert result["event_type"] == "TaskTimedOut"

    def test_lambda_function_timed_out(self):
        events = _make_events(
            include_failure=True, failure_type="LambdaFunctionTimedOut"
        )
        result = extract_failure_details(events)
        assert result["error"] == "States.Timeout"
        assert result["event_type"] == "LambdaFunctionTimedOut"

    def test_activity_failed(self):
        events = _make_events(include_failure=True, failure_type="ActivityFailed")
        result = extract_failure_details(events)
        assert result["error"] == "ActivityError"
        assert result["event_type"] == "ActivityFailed"

    def test_no_failure_events_returns_empty(self):
        events = _make_events(include_failure=False)
        result = extract_failure_details(events)
        assert result["error"] == ""
        assert result["cause"] == ""
        assert result["event_type"] == ""

    def test_failed_state_traced_from_prior_entered_event(self):
        events = _make_events(include_failure=True, failure_type="TaskFailed")
        result = extract_failure_details(events)
        # The last TaskStateEntered before id=5 is ExtractDocument (id=4)
        assert result["failed_state"] == "ExtractDocument"


# ---------------------------------------------------------------------------
# The Catch-handler misattribution (#1139, #1168) does NOT arise here — pinned.
# ---------------------------------------------------------------------------


def _caught_failure_events():
    """`ExtractDocument` failed, a Catch routed to a Fail state, the execution ended.

    `FailStateEntered` is a real `HistoryEventType` and matches a `StateEntered`
    suffix, and it sits between the task failure and the terminal event — which is
    what made two other readers of this history report the handler.
    """
    return [
        {
            "id": 1,
            "type": "TaskStateEntered",
            "timestamp": "2026-03-01T10:00:01+00:00",
            "stateEnteredEventDetails": {"name": "ExtractDocument"},
        },
        {
            "id": 2,
            "type": "TaskFailed",
            "timestamp": "2026-03-01T10:00:02+00:00",
            "taskFailedEventDetails": {
                "error": "ExtractionBoom",
                "cause": "Traceback",
                "resource": "placeholder",
                "resourceType": "lambda",
            },
        },
        {
            "id": 3,
            "type": "FailStateEntered",
            "timestamp": "2026-03-01T10:00:03+00:00",
            "stateEnteredEventDetails": {"name": "ExtractionShardMapFailed"},
        },
        {
            "id": 4,
            "type": "ExecutionFailed",
            "timestamp": "2026-03-01T10:00:04+00:00",
            "executionFailedEventDetails": {
                "error": "States.TaskFailed",
                "cause": "propagated",
            },
        },
    ]


class TestCatchHandlerIsNotReportedAsTheFailingState:
    """This module is immune to #1139/#1168, and the immunity is incidental.

    Both attribution rules here filter to `TaskStateEntered` exactly, and a `Fail`
    state never emits one — so the handler's transition is skipped and the real state
    is found. That is luck rather than intent: broadening either filter to a
    `StateEntered` *suffix* match, which reads like a strict improvement and is how the
    other two readers of this history were written, reintroduces the defect. These
    assertions are here so that change fails instead of shipping.

    The rule those two readers now share lives in
    `idp_common.stepfunctions_history`; unifying this module onto it as well is
    separate work, because its failure vocabulary is wider (it recognises activity and
    Lambda-timeout failures the shared set does not).
    """

    def test_extract_failure_details_skips_the_handler(self):
        result = extract_failure_details(_caught_failure_events())
        assert result["failed_state"] == "ExtractDocument"
        assert result["error"] == "States.TaskFailed"

    def test_analyze_execution_timeline_skips_the_handler(self):
        events = _caught_failure_events()
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        assert timeline["failed_state"] == "ExtractDocument"
        assert "ExtractionShardMapFailed" not in [
            state["name"] for state in timeline["states"]
        ]

    def test_a_fail_state_transition_is_not_a_task_state_transition(self):
        """The fact the immunity rests on, read from the service model rather than
        assumed: the eight state-transition event types share one detail key, so only
        the event `type` distinguishes a `Fail` entry from a `Task` entry."""
        import botocore.session

        model = botocore.session.get_session().get_service_model("stepfunctions")
        types = set(model.shape_for("HistoryEventType").enum)
        assert {"FailStateEntered", "TaskStateEntered"} <= types
        assert (
            "failStateEnteredEventDetails"
            not in model.shape_for("HistoryEvent").members
        )
