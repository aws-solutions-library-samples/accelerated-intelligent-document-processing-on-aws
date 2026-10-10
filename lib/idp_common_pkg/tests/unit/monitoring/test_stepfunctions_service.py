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
    """This module is immune to #1139/#1168, and the immunity is now intentional.

    Both attribution rules here come from `idp_common.stepfunctions_history`, whose rule
    keeps two things apart: the **state** comes from the last task-level failure, the
    **error text** from the terminal event. That split is what carries the immunity, and it
    is what makes matching the `StateEntered` *suffix* safe — the handler's transition is
    seen, and then not used, because an execution-level event never overrides a state a
    task-level failure already named.

    The distinction matters to anyone editing either rule. Matching `TaskStateEntered`
    exactly would also pass these assertions, and it was how this module used to read, but
    it is immunity by luck: it works only because a `Fail` state emits
    `FailStateEntered`, and it fails the moment a `Catch` routes to a `Task` handler —
    which emits `TaskStateEntered` like any other task — or a failure occurs inside a
    `Map` or `Parallel`, whose transitions it cannot see at all. Keep the split; the exact
    match is not a substitute for it.
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

    def test_a_catch_routed_to_a_task_handler_still_names_the_state_that_failed(self):
        """The case the old `TaskStateEntered`-exact rule got wrong, measured.

        A `Catch` does not have to route to a `Fail` state. Routing to a `Task` — a Lambda
        that records the error, say — emits `TaskStateEntered` for the handler, so the
        exact-match rule sees it, treats it as the most recently entered state, and
        attributes the terminal `ExecutionFailed` to the handler. The task/execution split
        is what prevents that, not the choice of filter.
        """
        events = [
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
                "type": "TaskStateEntered",
                "timestamp": "2026-03-01T10:00:03+00:00",
                "stateEnteredEventDetails": {"name": "RecordFailureHandler"},
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
        sf = _make_sf_client(status="FAILED", events=events)
        with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
            mock_boto3.client.return_value = sf
            timeline = analyze_execution_timeline(_EXEC_ARN)

        assert timeline["failed_state"] == "ExtractDocument"
        assert timeline["failure_details"]["failed_state"] == "ExtractDocument"

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


# ---------------------------------------------------------------------------
# #1185 — the terminal failure, not the first one; and the two rules agree.
#
# Every history below comes from the model-validated builder the shared rule's own
# suite uses, so an event type or detail member that does not exist fails at
# construction rather than quietly testing a service that cannot produce it.
# ---------------------------------------------------------------------------

from tests.unit.test_stepfunctions_history import (  # noqa: E402
    _History,
    _retried_then_succeeded,
)


def _timeline_for(events, status="FAILED", **kwargs):
    sf = _make_sf_client(status=status, events=events)
    with patch("idp_common.monitoring.stepfunctions_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = sf
        return analyze_execution_timeline(_EXEC_ARN, **kwargs)


@pytest.mark.unit
class TestTheTerminalFailureIsReportedNotTheFirst:
    """A failed execution routinely carries an earlier failure it recovered from.

    The pipeline retries throttles, service exceptions and timeouts in many places, so
    the earliest failure in a history is regularly a state that went on to succeed.
    """

    def test_a_retried_then_succeeded_state_is_not_reported(self):
        """The discriminating case. `OCR` failed, was retried and succeeded;
        `Extraction` then failed and ended the execution."""
        timeline = _timeline_for(_retried_then_succeeded().events)
        assert timeline["failed_state"] == "Extraction"

    def test_the_recovered_state_is_what_a_first_failure_rule_names(self):
        """Proves the fixture discriminates rather than having one possible answer:
        `OCR` is present, is the first failure, and must not be the answer."""
        timeline = _timeline_for(_retried_then_succeeded().events)
        assert "OCR" in [state["name"] for state in timeline["states"]]
        assert timeline["failed_state"] != "OCR"

    def test_the_two_rules_in_this_module_agree(self):
        """They are both read from the one shared rule now. They disagreed before: the
        timeline took the first failure and the detail extractor the terminal one, for
        the same execution."""
        timeline = _timeline_for(_retried_then_succeeded().events)
        assert timeline["failed_state"] == timeline["failure_details"]["failed_state"]
        assert timeline["failure_details"]["error"] == "States.TaskFailed"


@pytest.mark.unit
class TestReEnteringAStateDoesNotMisnameTheFailure:
    """`state_starts` is keyed by state NAME, so re-assigning a key leaves it where it
    was first inserted. "The last key" is therefore the state entered longest ago among
    those still to be re-entered, not the one most recently entered.

    A `Retry` does **not** reach this: it re-runs the task without re-entering the state.
    What reaches it is a genuine second entry — a loop back through a `Choice`, or a
    `Map`. So the fixture has to re-enter, and a retry fixture would pass either way.

    ⚠️ The intervening state has to be a **Task** state. The first version of this fixture
    looped through a `Choice`, and it passed against the unmodified code: the old rule
    recorded `TaskStateEntered` only, so a `Choice` never entered the dict, leaving
    `ProcessPage` as the single key and the stale-key answer accidentally right.
    """

    @staticmethod
    def _loop_history():
        return (
            _History()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="ProcessPage")
            .add("TaskStateExited", name="ProcessPage")
            .add("TaskStateEntered", name="CheckForMorePages")
            .add("TaskStateExited", name="CheckForMorePages")
            .add("TaskStateEntered", name="ProcessPage")
            .add("TaskScheduled")
            .add("TaskStarted")
            .add("TaskFailed", error="PageBoom", cause="a traceback")
            .add("ExecutionFailed", error="States.TaskFailed", cause="propagated")
        )

    def test_the_re_entered_state_is_named(self):
        timeline = _timeline_for(self._loop_history().events)
        assert timeline["failed_state"] == "ProcessPage"

    def test_the_intervening_state_is_what_the_stale_key_named(self):
        """The discriminator: `CheckForMorePages` was entered after `ProcessPage` FIRST
        was, so it is the last key in the dict, and it is the wrong answer."""
        timeline = _timeline_for(self._loop_history().events)
        assert timeline["failed_state"] != "CheckForMorePages"
        failure_rows = [state for state in timeline["states"] if state["is_failure"]]
        assert failure_rows
        assert all(row["name"] == "ProcessPage" for row in failure_rows)


@pytest.mark.unit
class TestTheEventCapDoesNotDropTheFailure:
    """`max_events` used to slice from the chronological HEAD, so on a history longer
    than the cap the terminal failure was outside the window and the function reported
    no failure at all — on exactly the long executions most worth asking about."""

    @staticmethod
    def _long_history(noise_pairs=80):
        # Task states, not Pass states: the old rule ignored every transition but a
        # Task's, so Pass-state noise produced no timeline rows and the assertions about
        # WHICH end of the history was kept could not tell the two behaviours apart.
        history = _History().add("ExecutionStarted")
        for index in range(noise_pairs):
            history.add("TaskStateEntered", name=f"Step{index}")
            history.add("TaskStateExited", name=f"Step{index}")
        return (
            history.add("TaskStateEntered", name="Extraction")
            .add("TaskScheduled")
            .add("TaskStarted")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
            .add("ExecutionFailed", error="States.TaskFailed", cause="propagated")
        )

    def test_the_failure_is_found_beyond_the_cap(self):
        history = self._long_history()
        assert len(history.events) > 50, (
            "the fixture must exceed the cap to discriminate"
        )
        timeline = _timeline_for(history.events, max_events=50)
        assert timeline["failed_state"] == "Extraction"
        assert timeline["failure_details"]["error"] == "States.TaskFailed"

    def test_the_cap_still_bounds_the_returned_timeline(self):
        """The cap is not simply ignored — it still limits what is returned, from the
        newest end, so the response size it exists to bound stays bounded."""
        history = self._long_history()
        timeline = _timeline_for(history.events, max_events=50)
        assert len(timeline["states"]) <= 50
        assert len(timeline["states"]) < len(history.events)

    def test_a_zero_cap_returns_no_rows_rather_than_all_of_them(self):
        """`events[-0:]` is the whole list, which is the trap in writing this fix."""
        timeline = _timeline_for(self._long_history().events, max_events=0)
        assert timeline["states"] == []
        assert timeline["failed_state"] == "Extraction"

    def test_the_newest_events_are_the_ones_kept(self):
        timeline = _timeline_for(self._long_history().events, max_events=6)
        assert "Step0" not in [state["name"] for state in timeline["states"]]


@pytest.mark.unit
class TestTheWidenedFailureVocabulary:
    """Nine members of the shared vocabulary this module recognised in neither rule.

    Counted rather than asserted: the old `extract_failure_details` set held six types and
    the old timeline loop three, and nine of the fifteen vocabulary members were in
    neither. `LambdaFunctionTimedOut` was in the six, so it is a case of the two rules
    disagreeing rather than of a type being unknown to both.
    """

    def test_a_lambda_timeout_names_a_state(self):
        """Nothing else in the history says a failure happened, so a vocabulary that
        omits `LambdaFunctionTimedOut` reports none."""
        history = (
            _History()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Assessment")
            .add("LambdaFunctionScheduled")
            .add("LambdaFunctionStarted")
            .add("LambdaFunctionTimedOut", error="States.Timeout", cause="900s elapsed")
        )
        timeline = _timeline_for(history.events, status="FAILED")
        assert timeline["failed_state"] == "Assessment"
        assert timeline["failure_details"]["event_type"] == "LambdaFunctionTimedOut"
        assert timeline["failure_details"]["error"] == "States.Timeout"

    def test_an_activity_failure_names_a_state(self):
        history = (
            _History()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="HumanReview")
            .add("ActivityScheduled")
            .add("ActivityFailed", error="ActivityBoom", cause="worker died")
        )
        timeline = _timeline_for(history.events, status="FAILED")
        assert timeline["failed_state"] == "HumanReview"

    def test_a_failure_row_is_recorded_for_the_widened_type(self):
        """Not just the summary field: the timeline row has to show it too, or the
        state reads as still running."""
        history = (
            _History()
            .add("TaskStateEntered", name="Assessment")
            .add("LambdaFunctionTimedOut", error="States.Timeout", cause="900s")
        )
        timeline = _timeline_for(history.events, status="FAILED")
        rows = [state for state in timeline["states"] if state["is_failure"]]
        assert [row["name"] for row in rows] == ["Assessment"]


@pytest.mark.unit
class TestAPageInEitherDirectionGivesTheSameAnswer:
    """`get_execution_data` paginates forward today, which is its choice rather than a
    guarantee to this function, so the ordering is established here."""

    def test_a_newest_first_page_reads_the_same(self):
        history = _retried_then_succeeded()
        assert (
            _timeline_for(history.reversed())["failed_state"]
            == _timeline_for(history.events)["failed_state"]
            == "Extraction"
        )


@pytest.mark.unit
class TestTheTimelineRowsAgreeWithTheSummaryField:
    """`failed_state` and the `states` rows must name the same state.

    For `EvaluationFailed` they did not: the summary came from the shared rule, which reads
    the state the event names itself, while the rows were labelled from the adjacent
    transition. Two answers about one execution disagreeing is the defect class #1185 is
    about, so it is not enough for the headline field to be right.
    """

    @staticmethod
    def _evaluation_failure_inside_a_map():
        return (
            _History()
            .add("ExecutionStarted")
            .add("MapStateEntered", name="ProcessSections")
            .add("TaskStateEntered", name="NotTheFailingState")
            .add(
                "EvaluationFailed",
                state="BuildSectionInput",
                error="States.QueryEvaluationError",
                cause="bad JSONata path",
            )
            .add("ExecutionFailed", error="States.QueryEvaluationError", cause="x")
        )

    def test_the_rows_name_the_state_the_event_names_itself(self):
        timeline = _timeline_for(self._evaluation_failure_inside_a_map().events)
        assert timeline["failed_state"] == "BuildSectionInput"
        failure_rows = [s for s in timeline["states"] if s["is_failure"]]
        assert failure_rows
        assert all(row["name"] == "BuildSectionInput" for row in failure_rows), (
            "a failure row names a state the summary field does not"
        )

    def test_the_adjacent_transition_is_a_different_state(self):
        """The discriminator: adjacency gives `NotTheFailingState`, so a row labelled from
        it is visibly wrong rather than coincidentally right."""
        timeline = _timeline_for(self._evaluation_failure_inside_a_map().events)
        assert "NotTheFailingState" not in [
            row["name"] for row in timeline["states"] if row["is_failure"]
        ]

    def test_the_rows_still_agree_when_the_cap_truncates(self):
        """⚠️ The window can start part-way through the history, so the state a failure is
        attributable to may be entered outside it. Labelling from an empty tracker gave
        `"Unknown"` on the row while `failed_state` named the state — a third reading of one
        history in the function whose two readings this unified. Swept across caps because
        the divergence only appears once the cap actually bites.
        """
        history = _History().add("ExecutionStarted")
        for index in range(15):
            history.add("TaskStateEntered", name=f"Step{index}")
            history.add("TaskStateExited", name=f"Step{index}")
        history.add("TaskStateEntered", name="Extraction").add(
            "TaskFailed", error="Boom", cause="a traceback"
        ).add("ExecutionFailed", error="States.TaskFailed", cause="propagated")

        assert len(history.events) > 30, "the fixture must exceed the caps swept below"
        for cap in (1000, 10, 5, 3, 2, 1):
            timeline = _timeline_for(history.events, max_events=cap)
            assert timeline["failed_state"] == "Extraction", cap
            rows = [r["name"] for r in timeline["states"] if r["is_failure"]]
            assert rows, cap
            assert all(name == "Extraction" for name in rows), (cap, rows)
