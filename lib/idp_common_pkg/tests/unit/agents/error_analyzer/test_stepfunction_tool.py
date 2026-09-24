# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the error analyzer's Step Functions tool.

`analyze_workflow_execution` answers the question the error analyzer exists to
answer: which workflow state failed, and why. Its output is not a log dump for a
human to read — `failure_point["state"]` is interpolated straight into the
`analysis_summary` the agent reports, and `recommendations` tells the user to go and
look at that state. So the tests assert the identified state, not merely that some
analysis came back.

Three properties carry the weight here, and #1081 needed all three to be wrong
before the tool could name the failing state.

**The event type names.** A `HistoryEvent`'s `type` is always prefixed with the
state's own type — `TaskStateEntered`, `ChoiceStateExited`, and six more — and is
never a bare `StateEntered`; that spelling belongs to the `stateEnteredEventDetails`
field. Matching the bare form against `type` reads no transition on any real
execution, so the failing state is `None` whatever the order. This is asserted over
the prefixed forms the service sends, because fixtures built on the bare spelling
cannot see it — a test suite validating against an input that cannot occur.

**The direction of the walk.** The walk is only correct oldest-first — the failing
state is the state most recently entered *before* the failure — while the caller
fetches newest-first (`reverseOrder=True`) so a capped page covers the failure. Both
directions are exercised and must agree. Ordering keys off the event `id`, which the
API documents as a required sequential integer, so the tie and direction-inference
cases a timestamp key would be exposed to are pinned too.

**Which failure is the failure.** An execution can survive several failure events,
because the workflow retries throttles, service exceptions and timeouts in 25 places.
The terminal failure is the one that explains the outcome; the earliest is often a
recovered attempt, and naming it gives a state that went on to succeed. So the
retry shape is pinned explicitly rather than relying on a single-failure fixture,
which cannot tell first-wins from last-wins.

Everything is offline; the Step Functions client is stubbed and the DynamoDB lookup
is patched at its import site.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from idp_common.agents.error_analyzer.tools.stepfunction_tool import (
    _analyze_execution_timeline,
    _build_analysis_summary,
    _build_response,
    _extract_execution_metadata,
    _extract_failure_details,
    _generate_recommendations,
    _get_execution_arn_from_document,
    _get_execution_data,
    analyze_workflow_execution,
)

MODULE = "idp_common.agents.error_analyzer.tools.stepfunction_tool"
EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp-sm:abc123"


def _repo_root() -> Path:
    """The checkout this test file belongs to.

    Derived from __file__ rather than from the working directory, so the walk cannot
    read a sibling worktree's state machine.
    """
    return Path(__file__).resolve().parents[6]


# The `type` field of a Step Functions HistoryEvent never carries a bare
# "StateEntered"/"StateExited" -- every transition is prefixed with the state's own
# type, and it is the *detail* field that is unprefixed. Fixtures that used the bare
# spelling matched nothing in production and so could not see whether the analyser
# read state transitions at all. `id` is a required sequential integer on every real
# event and is what the ordering keys off, so fixtures carry it too.
def _entered(name: str, timestamp: int, kind: str = "Task") -> dict[str, Any]:
    return {
        "id": timestamp,
        "timestamp": timestamp,
        "type": f"{kind}StateEntered",
        "stateEnteredEventDetails": {"name": name},
    }


def _exited(name: str, timestamp: int, kind: str = "Task") -> dict[str, Any]:
    return {
        "id": timestamp,
        "timestamp": timestamp,
        "type": f"{kind}StateExited",
        "stateExitedEventDetails": {"name": name},
    }


def _task_failed(timestamp: int, error: str = "Boom") -> dict[str, Any]:
    return {
        "id": timestamp,
        "timestamp": timestamp,
        "type": "TaskFailed",
        "taskFailedEventDetails": {
            "error": error,
            "cause": "stack trace",
            "resource": "lambda:invoke",
        },
    }


def _execution_failed(timestamp: int, error: str = "Boom") -> dict[str, Any]:
    """The terminal event of a failed execution; always the newest in the history."""
    return {
        "id": timestamp,
        "timestamp": timestamp,
        "type": "ExecutionFailed",
        "executionFailedEventDetails": {"error": error, "cause": "propagated"},
    }


#: OCR and Classification succeeded; Extraction failed. "Extraction" is the answer
#: any correct analysis must give.
CHRONOLOGICAL_HISTORY = [
    _entered("OCR", 1),
    _exited("OCR", 2),
    _entered("Classification", 3),
    _exited("Classification", 4),
    _entered("Extraction", 5),
    _task_failed(6),
]


def _fixed_timeline_cap(cap: int = 50, pages: int | None = None):
    """Pin the timeline cap without pinning anything else.

    `get_ea_param` serves several fields, so a stand-in that ignores its argument
    also sets the history page cap — which made a test asserting the timeline cap
    quietly fetch 50 pages. Every other field falls through to its real default
    unless `pages` names one.
    """

    def _param(field: str, default: Any):
        if field == "max_stepfunction_timeline_events":
            return cap
        if field == "max_stepfunction_history_pages" and pages is not None:
            return pages
        return default

    return patch(f"{MODULE}.get_ea_param", side_effect=_param)


@pytest.mark.unit
class TestExtractFailureDetails:
    """_extract_failure_details: one parser per failure event type."""

    def test_a_non_failure_event_yields_nothing(self):
        # Returning a details dict for a successful event would make the first
        # StateEntered look like the failure point.
        assert _extract_failure_details(_entered("OCR", 1)) is None

    def test_an_unknown_event_type_yields_nothing(self):
        assert _extract_failure_details({"type": "ExecutionStarted"}) is None

    def test_an_event_with_no_type_yields_nothing(self):
        assert _extract_failure_details({}) is None

    def test_execution_failed_carries_error_and_cause(self):
        details = _extract_failure_details(
            {
                "type": "ExecutionFailed",
                "executionFailedEventDetails": {
                    "error": "States.Runtime",
                    "cause": "c",
                },
            }
        )
        assert details == {"error": "States.Runtime", "cause": "c"}

    def test_task_failed_also_carries_the_resource(self):
        # The resource names the Lambda or service that failed, which is what
        # selects the log group to look in next.
        details = _extract_failure_details(_task_failed(1))
        assert details["error"] == "Boom"
        assert details["resource"] == "lambda:invoke"

    def test_lambda_function_failed_carries_error_and_cause(self):
        details = _extract_failure_details(
            {
                "type": "LambdaFunctionFailed",
                "lambdaFunctionFailedEventDetails": {
                    "error": "Unhandled",
                    "cause": "Traceback",
                },
            }
        )
        assert details == {"error": "Unhandled", "cause": "Traceback"}

    @pytest.mark.parametrize("event_type", ["TaskTimedOut", "ExecutionTimedOut"])
    def test_a_timeout_names_itself_as_the_error(self, event_type):
        details = _extract_failure_details({"type": event_type})
        assert details["error"] == event_type
        assert "timeout" in details["cause"].lower()

    def test_a_timeout_cause_is_used_when_present(self):
        details = _extract_failure_details(
            {
                "type": "ExecutionTimedOut",
                "executionTimedOutEventDetails": {"cause": "60s exceeded"},
            }
        )
        assert details["cause"] == "60s exceeded"

    @pytest.mark.parametrize(
        "event_type,detail_key",
        [
            ("ExecutionFailed", "executionFailedEventDetails"),
            ("TaskFailed", "taskFailedEventDetails"),
            ("LambdaFunctionFailed", "lambdaFunctionFailedEventDetails"),
        ],
    )
    def test_a_failure_with_no_detail_object_still_reports_something(
        self, event_type, detail_key
    ):
        # A failure with no explanation is still a failure, and reporting None here
        # would make the event look successful.
        details = _extract_failure_details({"type": event_type})
        assert details["error"]
        assert details["cause"]
        assert detail_key  # named for documentation


@pytest.mark.unit
class TestAnalyzeExecutionTimelineChronological:
    """_analyze_execution_timeline fed oldest-first: the logic that is correct."""

    def test_no_events_reports_an_error_rather_than_an_empty_analysis(self):
        # Zero events means the history could not be read, which is a different
        # finding from "the workflow ran and nothing failed".
        assert _analyze_execution_timeline([]) == {
            "error": "No execution events available"
        }

    def test_the_failing_state_is_the_state_that_was_running(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(CHRONOLOGICAL_HISTORY)
        assert result["failure_point"]["state"] == "Extraction"
        assert result["failure_point"]["event_type"] == "TaskFailed"
        assert result["failure_point"]["details"]["error"] == "Boom"

    def test_state_transitions_appear_in_the_timeline(self):
        with _fixed_timeline_cap():
            timeline = _analyze_execution_timeline(CHRONOLOGICAL_HISTORY)["timeline"]
        assert [entry["state"] for entry in timeline] == [
            "OCR",
            "OCR",
            "Classification",
            "Classification",
            "Extraction",
        ]

    def test_entered_and_exited_are_distinguishable(self):
        with _fixed_timeline_cap():
            timeline = _analyze_execution_timeline(
                [_entered("OCR", 1), _exited("OCR", 2)]
            )["timeline"]
        assert timeline[0]["event"].startswith("Entered")
        assert timeline[1]["event"].startswith("Exited")

    def test_a_state_with_no_name_reads_as_unknown(self):
        with _fixed_timeline_cap():
            timeline = _analyze_execution_timeline(
                [{"timestamp": 1, "type": "StateEntered"}]
            )["timeline"]
        assert timeline[0]["state"] == "Unknown"

    def test_a_clean_execution_has_no_failure_point(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [_entered("OCR", 1), _exited("OCR", 2)]
            )
        assert result["failure_point"] is None
        assert result["last_successful_state"] == "OCR"

    @pytest.mark.parametrize(
        "kind",
        ["Task", "Choice", "Pass", "Map", "Parallel", "Wait", "Succeed", "Fail"],
    )
    def test_every_real_state_transition_type_is_recognised(self, kind):
        # The history API prefixes each transition with the state's own type and never
        # emits a bare "StateEntered". Matching the bare spelling against `type` reads
        # no transition at all, which reports the failing state as None on every real
        # execution -- so this is parametrised over the prefixed forms the service
        # actually sends rather than over the one the fixtures used to build.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [_entered("Extraction", 1, kind=kind), _task_failed(2)]
            )
        assert result["last_successful_state"] == "Extraction"
        assert result["failure_point"]["state"] == "Extraction"
        assert [entry["state"] for entry in result["timeline"]] == ["Extraction"]

    def test_a_non_transition_event_type_is_not_read_as_a_transition(self):
        # The other direction of the suffix match: "ExecutionStarted" and
        # "TaskScheduled" end in neither suffix and must not enter the timeline.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [
                    {"id": 1, "timestamp": 1, "type": "ExecutionStarted"},
                    {"id": 2, "timestamp": 2, "type": "TaskScheduled"},
                ]
            )
        assert result["timeline"] == []
        assert result["last_successful_state"] is None

    def test_the_terminal_failure_is_reported_not_a_recovered_retry(self):
        # An execution can survive several failure events. `workflow.asl.json` has 25
        # Retry blocks covering Lambda throttles, service exceptions and timeouts, so
        # a recovered throttle in the history is routine on a Bedrock-heavy workload.
        # Naming the earliest failure points at a state that went on to succeed, with
        # an error nobody needs to act on -- so the summary would be confidently
        # wrong rather than merely unhelpful.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [
                    _entered("OCR", 1),
                    # Throttled, retried in place, succeeded: a Retry does not
                    # re-enter the state, so the history keeps the failed attempt.
                    _task_failed(2, error="Lambda.TooManyRequestsException"),
                    _exited("OCR", 3),
                    _entered("Extraction", 4),
                    _task_failed(5, error="ValidationException"),
                    _execution_failed(6, error="ValidationException"),
                ]
            )
        assert result["failure_point"]["state"] == "Extraction"
        assert result["failure_point"]["details"]["error"] == "ValidationException"

    def test_the_terminal_execution_failed_event_is_what_the_walk_ends_on(self):
        # Taking the last failure prefers ExecutionFailed for free, because it is the
        # final event of a failed execution. Pinned so that a future change back to
        # first-failure-wins cannot look correct on a single-failure fixture.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [
                    _entered("Extraction", 1),
                    _task_failed(2, error="ValidationException"),
                    _execution_failed(3, error="States.TaskFailed"),
                ]
            )
        assert result["failure_point"]["event_type"] == "ExecutionFailed"
        assert result["failure_point"]["state"] == "Extraction"

    def test_the_timeline_is_truncated_to_the_configured_cap(self):
        history = [_entered(f"S{i}", i) for i in range(10)]
        with _fixed_timeline_cap(3):
            timeline = _analyze_execution_timeline(history)["timeline"]
        assert len(timeline) == 3

    def test_truncation_keeps_the_most_recent_events(self):
        # The events near the failure are the useful ones; keeping the beginning of
        # a long workflow discards exactly the part being investigated.
        history = [_entered(f"S{i}", i) for i in range(10)]
        with _fixed_timeline_cap(3):
            timeline = _analyze_execution_timeline(history)["timeline"]
        assert [entry["state"] for entry in timeline] == ["S7", "S8", "S9"]

    def test_the_configured_cap_is_honoured_rather_than_a_hardcoded_fifty(self):
        history = [_entered(f"S{i}", i) for i in range(10)]
        with _fixed_timeline_cap(5):
            assert len(_analyze_execution_timeline(history)["timeline"]) == 5


@pytest.mark.unit
class TestAnalyzeExecutionTimelineReverseOrder:
    """
    The same function fed newest-first, which is what the caller actually supplies.

    `_get_execution_data` passes `reverseOrder=True`, so this is the direction the
    analyser actually receives in production. Each case here is one of the three
    answers the direction decides (issue #1081): the failing state, the last
    successful state, and which events survive truncation. The ordering and the
    ordering *key* are covered separately, because a key that ties is a way of
    getting the direction wrong again.
    """

    def test_the_failing_state_is_identified_from_a_newest_first_history(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(CHRONOLOGICAL_HISTORY)))
        assert result["failure_point"]["state"] == "Extraction"

    def test_the_last_successful_state_is_correct_from_a_newest_first_history(self):
        # "OCR" is the distinguishing wrong answer here: walking a newest-first list
        # ends on the FIRST state of the execution, so this reads as a plausible
        # early-stage failure rather than as a parse error.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(CHRONOLOGICAL_HISTORY)))
        assert result["last_successful_state"] == "Extraction"

    def test_truncation_keeps_the_events_near_the_failure(self):
        # `timeline[-N:]` is only the most recent N if the walk was chronological.
        # A cap of 2 is what makes this distinguishing: it is small enough that the
        # beginning and the end of the execution share no entries.
        with _fixed_timeline_cap(2):
            timeline = _analyze_execution_timeline(
                list(reversed(CHRONOLOGICAL_HISTORY))
            )["timeline"]
        assert "Extraction" in [entry["state"] for entry in timeline]

    def test_the_tool_identifies_the_failing_state_from_a_real_fetch(self):
        # Stubs the Step Functions CLIENT rather than _get_execution_data, so the
        # reverseOrder=True fetch actually runs and its output reaches the analyser.
        # Nothing else in this suite observes that boundary.
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        with (
            patch(f"{MODULE}.boto3.client") as factory,
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            _fixed_timeline_cap(),
        ):
            client = factory.return_value
            client.describe_execution.return_value = {
                "status": "FAILED",
                "startDate": start,
                "stopDate": start + timedelta(seconds=30),
            }
            # What AWS returns for reverseOrder=True: newest event first.
            client.get_execution_history.return_value = {
                "events": list(reversed(CHRONOLOGICAL_HISTORY))
            }
            result = analyze_workflow_execution("report.pdf")

        assert result["timeline_analysis"]["failure_point"]["state"] == "Extraction"
        # The exact interpolation, not merely the substring: this string is the
        # headline the operator reads, and "at state 'None'" is what it said while
        # the order went uncorrected.
        assert "at state 'Extraction'" in result["analysis_summary"]

    def test_tied_timestamps_do_not_disturb_the_order(self):
        # Timestamps have millisecond resolution and adjacent events routinely share
        # one, so ordering on the timestamp would be tie-prone: a stable sort leaves a
        # tie in arrival order, which for a newest-first page is backwards, and reading
        # the direction off the two ends cannot see a tie between them at all. The
        # event id is sequential and unique, so neither failure mode applies. Here
        # every event shares one timestamp and only the ids distinguish them --
        # including the two ends, which is the case a two-ended heuristic misses.
        tied = [
            _entered("OCR", 1),
            _exited("OCR", 2),
            _entered("Extraction", 3),
            _task_failed(4),
        ]
        for event in tied:
            event["timestamp"] = 7
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(tied)))
        assert result["failure_point"]["state"] == "Extraction"
        assert result["last_successful_state"] == "Extraction"

    def test_the_id_order_is_used_even_when_the_timestamps_disagree(self):
        # If the two keys ever disagree the id is authoritative: it is the field the
        # API documents as sequential, and this repo's execution-history resolver
        # already correlates steps by it. Timestamps here descend as the ids ascend,
        # so anything ordering on the timestamp reports OCR rather than Extraction.
        events = [
            _entered("OCR", 1),
            _exited("OCR", 2),
            _entered("Extraction", 3),
            _task_failed(4),
        ]
        for offset, event in enumerate(events):
            event["timestamp"] = 100 - offset
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(events)
        assert result["failure_point"]["state"] == "Extraction"

    def test_events_without_an_id_still_get_the_direction_corrected(self):
        # Not a shape the API produces -- id is required on a HistoryEvent -- but the
        # analyser is reachable from anywhere, so the fallback is exercised rather
        # than assumed. Descending timestamps at the two ends identify a newest-first
        # page without any id, and it is corrected. The fallback's own blind spot (two
        # ends sharing a timestamp) is why it logs; the id path above is what
        # production actually takes.
        newest_first = [
            {"timestamp": 3, "type": "TaskFailed", "taskFailedEventDetails": {}},
            {
                "timestamp": 2,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "Extraction"},
            },
            {
                "timestamp": 1,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "OCR"},
            },
        ]
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(newest_first)
        assert result["failure_point"]["state"] == "Extraction"

    def test_a_truncated_window_says_so_rather_than_reporting_a_null_state_bare(self):
        # The residual the 100-event cap leaves: a newest-first window always keeps
        # the failure event, but on a long execution the failing state was entered
        # before the window and cannot be named. Reporting "at state 'None'" with
        # nothing else is the same confidently-wrong shape as the ordering defect --
        # it reads as a finding. Saying the window was truncated makes it a
        # measurement limit, which is what it is.
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        with (
            patch(f"{MODULE}.boto3.client") as factory,
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            _fixed_timeline_cap(pages=2),
        ):
            client = factory.return_value
            client.describe_execution.return_value = {
                "status": "FAILED",
                "startDate": start,
                "stopDate": start + timedelta(seconds=30),
            }
            # The window reached the failure but not the StateEntered before it, and
            # every page looks the same, so the walk stops at its cap.
            client.get_execution_history.return_value = {
                "events": [_execution_failed(900), _task_failed(899)],
                "nextToken": "opaque",
            }
            result = analyze_workflow_execution("report.pdf")

        assert result["timeline_analysis"]["failure_point"]["state"] is None
        assert result["timeline_analysis"]["state_unresolved_due_to_truncation"] is True
        assert "could not be identified" in result["analysis_summary"]
        assert any(
            "longer than this tool reads" in r for r in result["recommendations"]
        )

    def test_an_untruncated_null_state_is_not_explained_away_as_truncation(self):
        # The other direction: without a nextToken the analysis must not offer the
        # truncation explanation, or every unidentified state acquires a false cause.
        assert "could not be identified" not in _build_analysis_summary(
            "FAILED", {"failure_point": {"state": None, "details": {}}}, False
        )

    def test_the_failure_details_themselves_survive_the_order_correction(self):
        # The error and cause come from the failure event itself rather than from
        # surrounding context, so they are correct in either direction. Worth pinning
        # separately: it is why the summary read as plausible while it named no
        # state, and so why the defect was not obvious from the output.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(CHRONOLOGICAL_HISTORY)))
        assert result["failure_point"]["details"]["error"] == "Boom"


@pytest.mark.unit
class TestExtractExecutionMetadata:
    """_extract_execution_metadata: status and duration."""

    def test_status_and_duration_are_computed_from_the_two_dates(self):
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        metadata = _extract_execution_metadata(
            {
                "status": "FAILED",
                "startDate": start,
                "stopDate": start + timedelta(seconds=90),
            }
        )
        assert metadata["status"] == "FAILED"
        assert metadata["duration_seconds"] == 90.0

    def test_a_running_execution_has_no_duration(self):
        # A duration computed from a missing stop date would either raise or invent
        # a number; None is the honest answer for an execution still in flight.
        metadata = _extract_execution_metadata(
            {"status": "RUNNING", "startDate": datetime.now(timezone.utc)}
        )
        assert metadata["status"] == "RUNNING"
        assert metadata["duration_seconds"] is None

    def test_an_empty_response_reports_unknown_rather_than_raising(self):
        metadata = _extract_execution_metadata({})
        assert metadata["status"] == "UNKNOWN"
        assert metadata["duration_seconds"] is None


@pytest.mark.unit
class TestBuildAnalysisSummary:
    """_build_analysis_summary: the sentence the agent reports."""

    def test_a_clean_execution_summary_names_only_the_status(self):
        assert _build_analysis_summary("SUCCEEDED", {}) == (
            "Step Function execution SUCCEEDED"
        )

    def test_a_failure_summary_names_the_state_and_the_error(self):
        summary = _build_analysis_summary(
            "FAILED",
            {"failure_point": {"state": "Extraction", "details": {"error": "Boom"}}},
        )
        assert "FAILED" in summary
        assert "Extraction" in summary
        assert "Boom" in summary

    def test_a_failure_with_no_error_text_still_names_the_state(self):
        summary = _build_analysis_summary(
            "FAILED", {"failure_point": {"state": "Extraction", "details": {}}}
        )
        assert "Extraction" in summary

    def test_a_null_state_is_interpolated_literally_rather_than_defaulted(self):
        # `.get("state", "Unknown")` does not help when the key is PRESENT with
        # value None, so the user-visible sentence reads "at state 'None'" rather
        # than "at state 'Unknown'". Worth pinning on its own: this is the shape
        # that turned an unidentified state into a confident wrong answer in #1081,
        # and it would do so again for any other cause of a null state.
        summary = _build_analysis_summary(
            "FAILED", {"failure_point": {"state": None, "details": {"error": "Boom"}}}
        )
        assert "'None'" in summary

    def test_a_missing_state_key_does_fall_back_to_unknown(self):
        summary = _build_analysis_summary("FAILED", {"failure_point": {"details": {}}})
        assert "Unknown" in summary


@pytest.mark.unit
class TestBuildResponse:
    """_build_response: the shape every return path shares."""

    def test_all_five_keys_are_always_present(self):
        response = _build_response(execution_status=None)
        assert set(response) == {
            "execution_status",
            "duration_seconds",
            "timeline_analysis",
            "analysis_summary",
            "recommendations",
        }

    def test_absent_sections_default_to_empty_containers_not_null(self):
        # The agent iterates recommendations and reads timeline_analysis; None in
        # either is a crash rather than an empty result.
        response = _build_response(execution_status=None)
        assert response["timeline_analysis"] == {}
        assert response["recommendations"] == []

    def test_supplied_values_are_carried_through(self):
        response = _build_response(
            execution_status="FAILED",
            duration_seconds=12.5,
            timeline_analysis={"failure_point": None},
            analysis_summary="s",
            recommendations=["r"],
        )
        assert response["execution_status"] == "FAILED"
        assert response["duration_seconds"] == 12.5
        assert response["recommendations"] == ["r"]


@pytest.mark.unit
class TestGenerateRecommendations:
    """_generate_recommendations: currently a fixed list."""

    def test_recommendations_are_always_returned(self):
        assert _generate_recommendations({})

    def test_the_list_does_not_depend_on_the_analysis(self):
        # Unlike the X-Ray tool's equivalent, this one ignores its argument. Pinned
        # so that making it conditional is a deliberate, visible change rather than
        # something that silently alters what every failure report advises.
        assert _generate_recommendations({}) == _generate_recommendations(
            {"failure_point": {"state": "Extraction"}}
        )


@pytest.mark.unit
class TestGetExecutionArnFromDocument:
    """_get_execution_arn_from_document: the DynamoDB lookup and its two field names."""

    def _lookup(self, response):
        return patch(
            "idp_common.agents.error_analyzer.tools.dynamodb_tool.fetch_document_record",
            return_value=response,
        )

    def test_the_workflow_execution_arn_field_is_preferred(self):
        with self._lookup(
            {
                "document_found": True,
                "document": {
                    "WorkflowExecutionArn": EXECUTION_ARN,
                    "ExecutionArn": "arn:aws:states:::execution:other:zzz",
                },
            }
        ):
            assert _get_execution_arn_from_document("report.pdf") == EXECUTION_ARN

    def test_the_legacy_execution_arn_field_is_used_as_a_fallback(self):
        # Older tracking records use ExecutionArn; dropping the fallback would make
        # workflow analysis unavailable for every document processed before the
        # rename.
        with self._lookup(
            {"document_found": True, "document": {"ExecutionArn": EXECUTION_ARN}}
        ):
            assert _get_execution_arn_from_document("report.pdf") == EXECUTION_ARN

    def test_a_document_that_is_not_found_yields_nothing(self):
        with self._lookup({"document_found": False}):
            assert _get_execution_arn_from_document("report.pdf") is None

    def test_a_document_with_no_execution_arn_yields_nothing(self):
        with self._lookup({"document_found": True, "document": {}}):
            assert _get_execution_arn_from_document("report.pdf") is None

    def test_a_lookup_failure_yields_nothing_rather_than_propagating(self):
        with patch(
            "idp_common.agents.error_analyzer.tools.dynamodb_tool.fetch_document_record",
            side_effect=RuntimeError("AccessDenied"),
        ):
            assert _get_execution_arn_from_document("report.pdf") is None


@pytest.mark.unit
class TestGetExecutionData:
    """_get_execution_data: the two Step Functions calls."""

    def test_both_calls_target_the_execution_arn(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.return_value = {"events": [_task_failed(1)]}
            data = _get_execution_data(EXECUTION_ARN)
        assert (
            client.describe_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
        )
        assert (
            client.get_execution_history.call_args.kwargs["executionArn"]
            == EXECUTION_ARN
        )
        assert data["execution_response"] == {"status": "FAILED"}
        assert len(data["events"]) == 1

    def test_the_history_is_requested_newest_first(self):
        # Pinned deliberately. #1081 came down to a choice between correcting the
        # order and dropping this flag, and the flag stays: with a cap of 100 and no
        # pagination, newest-first is what keeps the events around the failure.
        # Dropping it would change which events survive the cap, and on a long
        # execution the surviving page might not reach the failure at all.
        with patch(f"{MODULE}.boto3.client") as factory:
            client = factory.return_value
            client.describe_execution.return_value = {}
            client.get_execution_history.return_value = {"events": []}
            _get_execution_data(EXECUTION_ARN)
        kwargs = client.get_execution_history.call_args.kwargs
        assert kwargs["reverseOrder"] is True
        assert kwargs["maxResults"] == 100

    def test_a_history_with_no_events_gives_an_empty_list(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            client = factory.return_value
            client.describe_execution.return_value = {}
            client.get_execution_history.return_value = {}
            assert _get_execution_data(EXECUTION_ARN)["events"] == []

    def test_a_nextToken_in_the_response_is_reported_as_truncation(self):
        # The fetch is a single un-paginated page, so a nextToken means the execution
        # had more history than was read. That has to reach the caller: the failure
        # event survives a newest-first window but the failing state's StateEntered
        # need not, and an unidentified state then looks like a clean execution.
        with patch(f"{MODULE}.boto3.client") as factory:
            client = factory.return_value
            client.describe_execution.return_value = {}
            client.get_execution_history.return_value = {
                "events": [_task_failed(1)],
                "nextToken": "opaque",
            }
            assert (
                _get_execution_data(EXECUTION_ARN)["state_unresolved_due_to_truncation"]
                is True
            )

    def test_a_complete_history_is_not_reported_as_truncated(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            client = factory.return_value
            client.describe_execution.return_value = {}
            client.get_execution_history.return_value = {"events": [_task_failed(1)]}
            assert (
                _get_execution_data(EXECUTION_ARN)["state_unresolved_due_to_truncation"]
                is False
            )


@pytest.mark.unit
class TestAnalyzeWorkflowExecution:
    """analyze_workflow_execution: the tool as the agent calls it."""

    def test_an_empty_document_id_is_rejected_before_any_aws_call(self):
        with patch(f"{MODULE}.boto3.client") as factory:
            result = analyze_workflow_execution("")
        assert result["execution_status"] is None
        assert "No document ID provided" in result["analysis_summary"]
        assert result["recommendations"]
        factory.assert_not_called()

    def test_no_execution_arn_names_the_document_and_suggests_alternatives(self):
        # This is the common case for a document that never started processing, and
        # the recommendations are the only thing that gets the user unstuck.
        with patch(f"{MODULE}._get_execution_arn_from_document", return_value=None):
            result = analyze_workflow_execution("report.pdf")
        assert result["execution_status"] is None
        assert "report.pdf" in result["analysis_summary"]
        assert any("fetch_document_record" in r for r in result["recommendations"])

    def test_a_failed_execution_is_analysed_end_to_end(self):
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        with (
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            patch(
                f"{MODULE}._get_execution_data",
                return_value={
                    "execution_response": {
                        "status": "FAILED",
                        "startDate": start,
                        "stopDate": start + timedelta(seconds=30),
                    },
                    # Chronological. The real fetch supplies the reverse, which the
                    # reverse-order class above covers; this case stubs the seam out
                    # to isolate status and duration.
                    "events": CHRONOLOGICAL_HISTORY,
                },
            ),
            _fixed_timeline_cap(),
        ):
            result = analyze_workflow_execution("report.pdf")
        assert result["execution_status"] == "FAILED"
        assert result["duration_seconds"] == 30.0
        assert result["timeline_analysis"]["failure_point"]["state"] == "Extraction"
        assert "Extraction" in result["analysis_summary"]
        assert result["recommendations"]

    def test_a_succeeded_execution_reports_no_failure_point(self):
        with (
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            patch(
                f"{MODULE}._get_execution_data",
                return_value={
                    "execution_response": {"status": "SUCCEEDED"},
                    "events": [_entered("OCR", 1), _exited("OCR", 2)],
                },
            ),
            _fixed_timeline_cap(),
        ):
            result = analyze_workflow_execution("report.pdf")
        assert result["execution_status"] == "SUCCEEDED"
        assert result["timeline_analysis"]["failure_point"] is None

    def test_a_step_functions_failure_becomes_an_analysis_summary_not_an_exception(
        self,
    ):
        # The agent framework surfaces a raised exception as a tool crash; a
        # populated response lets the agent say what happened and try another tool.
        with (
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            patch(
                f"{MODULE}._get_execution_data",
                side_effect=RuntimeError("ExecutionDoesNotExist"),
            ),
        ):
            result = analyze_workflow_execution("report.pdf")
        assert result["execution_status"] is None
        assert "ExecutionDoesNotExist" in result["analysis_summary"]
        assert result["recommendations"]

    def test_every_return_path_produces_the_same_five_keys(self):
        # The agent reads these fields unconditionally, so a path that omits one
        # would fail at the point of reporting rather than at the point of failure.
        expected = {
            "execution_status",
            "duration_seconds",
            "timeline_analysis",
            "analysis_summary",
            "recommendations",
        }
        assert set(analyze_workflow_execution("").keys()) == expected
        with patch(f"{MODULE}._get_execution_arn_from_document", return_value=None):
            assert set(analyze_workflow_execution("a.pdf").keys()) == expected
        with (
            patch(
                f"{MODULE}._get_execution_arn_from_document", return_value=EXECUTION_ARN
            ),
            patch(f"{MODULE}._get_execution_data", side_effect=RuntimeError("x")),
        ):
            assert set(analyze_workflow_execution("a.pdf").keys()) == expected


def _fail_state_entered(name: str, timestamp: int) -> dict[str, Any]:
    """A `Fail` state being entered by a Catch. `FailStateEntered` is a real event type
    and so matches the `StateEntered` suffix, which is how it came to overwrite the
    tracked state."""
    return _entered(name, timestamp, kind="Fail")


@pytest.mark.unit
class TestACaughtFailureIsAttributedToTheStateThatFailed:
    """#1139: a Catch handler is not the state that failed.

    Every state that routes a failure to a `Fail` state produces a history of the
    shape below, and the terminal `ExecutionFailed` arrives *after* the handler has
    been entered. Reporting the last state entered therefore names the handler.

    The fix keeps the two apart: the state comes from the last **task-level** failure,
    the error text from the terminal event. Both halves are asserted, because taking
    the state from the task failure and the error from it too would lose the
    propagated cause an operator reads.
    """

    #: Extraction failed; the Catch routed to a handler; the execution then ended.
    _CAUGHT = [
        _entered("OCR", 1),
        _exited("OCR", 2),
        _entered("Extraction", 3),
        _task_failed(4, error="ExtractionBoom"),
        _fail_state_entered("ExtractionShardMapFailed", 5),
        _execution_failed(6, error="States.TaskFailed"),
    ]

    def test_the_failing_state_is_reported_not_the_catch_handler(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(self._CAUGHT)
        assert result["failure_point"]["state"] == "Extraction", (
            "the Catch handler is not the state that failed; reporting it sends an "
            "operator to the wrong log group"
        )

    def test_the_error_text_still_comes_from_the_terminal_event(self):
        """The state and the error come from different events, deliberately."""
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(self._CAUGHT)
        fp = result["failure_point"]
        assert fp["event_type"] == "ExecutionFailed"
        assert fp["details"]["error"] == "States.TaskFailed"
        assert fp["details"]["cause"] == "propagated"

    def test_a_recovered_earlier_failure_does_not_win(self):
        """The workflow retries in many places, so a survived TaskFailed is ordinary.

        The LAST task-level failure is the one that explains the end of the execution;
        an earlier recovered one names a state that went on to succeed.
        """
        history = [
            _entered("OCR", 1),
            _task_failed(2, error="ThrottledOnce"),
            _exited("OCR", 3),
            _entered("Extraction", 4),
            _task_failed(5, error="ExtractionBoom"),
            _fail_state_entered("FailState", 6),
            _execution_failed(7),
        ]
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(history)
        assert result["failure_point"]["state"] == "Extraction"

    def test_an_execution_level_failure_alone_still_reports_the_last_state(self):
        """No task failed, so there is nothing better than the last state entered.

        This is the case the change must NOT alter: an execution that times out or is
        failed without a task-level event has only one candidate, and reporting it is
        better than reporting nothing.
        """
        history = [
            _entered("OCR", 1),
            _exited("OCR", 2),
            _entered("Extraction", 3),
            _execution_failed(4, error="States.Timeout"),
        ]
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(history)
        assert result["failure_point"]["state"] == "Extraction"

    def test_an_uncaught_task_failure_is_unchanged(self):
        """The shape that already worked: no handler between the failure and the end."""
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(CHRONOLOGICAL_HISTORY)
        assert result["failure_point"]["state"] == "Extraction"
        assert result["failure_point"]["event_type"] == "TaskFailed"

    def test_a_concurrent_map_still_misattributes_and_that_is_recorded(self):
        """The known limitation, pinned so it is a recorded gap rather than prose.

        Two Map iterations share one execution history, so at `MaxConcurrency` above 1
        their events interleave. Attribution infers causality from adjacency, so the
        state reported is whichever iteration entered a state most recently before the
        failure — not necessarily the one that failed.

        Asserted at the WRONG answer deliberately: the previous rule gave the same
        answer on this history, so this is not a regression, and writing the
        aspiration here would make the test fail for a fix nobody has made. Walking
        `previousEventId` is the exact route, and when someone takes it this test
        should flip to `ExtractionStep`.
        """
        history = [
            _entered("ProcessSections", 1, kind="Map"),
            _entered("ExtractionStep", 2),  # iteration A
            _entered("AssessmentStep", 3),  # iteration B
            _task_failed(4),  # A's task fails
            _execution_failed(5),
        ]
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(history)
        assert result["failure_point"]["state"] == "AssessmentStep", (
            "if this now reports ExtractionStep, the causal-chain fix has landed and "
            "this test should assert that instead"
        )

    def test_the_two_failure_classes_partition_the_matched_set(self):
        """A new failure event type must be classified, not silently execution-level.

        `_extract_failure_details` matches the union, so an event type added to one set
        and not considered for the other would change which events can set the state.
        """
        from idp_common.agents.error_analyzer.tools import stepfunction_tool as m

        assert m._FAILURE_EVENTS == (
            m._TASK_LEVEL_FAILURE_EVENTS | m._EXECUTION_LEVEL_FAILURE_EVENTS
        )
        assert not (m._TASK_LEVEL_FAILURE_EVENTS & m._EXECUTION_LEVEL_FAILURE_EVENTS)


@pytest.mark.unit
class TestTheMisattributionPopulationIsDerivedFromTheWorkflow:
    """How many states can be misattributed, computed from the state machine.

    The scope of #1139 was left as "any failure routed through a `States.ALL` Catch"
    because the enumeration kept failing. Two reasons it did, both worth recording so
    the next reader does not repeat them:

    1. **`workflow.asl.json` is not valid JSON.** It is a CloudFormation-substituted
       template, and a `${Token}` sits in value position unquoted, so `json.load`
       raises at the first one. Substituting only the UNQUOTED form is the spelling to
       use, and it is already gated: `scripts/tests/test_asl_placeholder_substitution.py`
       enumerates the substitution sites in this repository, asserts that enumeration is
       not vacuous, and asserts each one preserves the task `Resource` ARNs -- a
       substitution that also rewrote quoted values would corrupt those. This module's
       pattern is one of the sites it covers.
    2. **States nest, and `Next` resolves within its own state map.** A Map's
       `ItemProcessor.States` and a Parallel's `Branches[].States` each hold a separate
       namespace, so a top-level-only walk sees 41 of 55 states, and a *global* name
       lookup reports `ExtractionShardMapFailed` missing because it exists only inside
       `ProcessSections`' iterator.

    What the measurement then shows is that the population is keyed on the catch's
    **target being a `Fail` state**, not on the breadth of its `ErrorEquals`: only two
    of the nine route via `States.ALL`, and the other seven match narrowly. A catch
    whose target is a `Task` or `Pass` state is a recovery path — the execution
    continues, no `ExecutionFailed` follows, and there is nothing to misattribute.
    """

    @staticmethod
    def _scopes():
        import json
        import re

        asl = _repo_root() / "patterns/unified/statemachine/workflow.asl.json"
        doc = json.loads(re.sub(r"\$\{[^}]+\}", "0", asl.read_text(encoding="utf-8")))
        scopes: list[tuple[str, dict]] = []

        def collect(states: dict, path: str = "") -> None:
            scopes.append((path, states))
            for name, d in states.items():
                if not isinstance(d, dict):
                    continue
                for key in ("ItemProcessor", "Iterator"):
                    inner = d.get(key)
                    if isinstance(inner, dict) and isinstance(
                        inner.get("States"), dict
                    ):
                        collect(inner["States"], f"{path}{name}/{key}/")
                for i, branch in enumerate(d.get("Branches") or []):
                    if isinstance(branch, dict) and isinstance(
                        branch.get("States"), dict
                    ):
                        collect(branch["States"], f"{path}{name}/Branches[{i}]/")

        collect(doc["States"])
        return scopes

    @classmethod
    def _catches(cls):
        out = []
        for scope, states in cls._scopes():
            for name, defn in states.items():
                if not isinstance(defn, dict):
                    continue
                for c in defn.get("Catch") or []:
                    nxt = c.get("Next")
                    target = states.get(nxt) or {}
                    out.append(
                        {
                            "state": f"{scope}{name}",
                            "errors": tuple(c.get("ErrorEquals") or []),
                            "target": nxt,
                            "target_type": target.get("Type", "UNRESOLVED"),
                        }
                    )
        return out

    def test_every_catch_target_resolves(self):
        """The check that makes the rest of this class trustworthy.

        An unresolved target would silently drop a state from the population, which is
        exactly how the original enumeration under-counted.
        """
        unresolved = [c for c in self._catches() if c["target_type"] == "UNRESOLVED"]
        assert not unresolved, unresolved

    def test_the_nested_state_maps_are_walked(self):
        """Not vacuous: a top-level-only walk would see far fewer states."""
        scopes = self._scopes()
        assert len(scopes) > 1, "no nested state map found; the walk is not descending"
        total = sum(len(states) for _, states in scopes)
        assert total > len(scopes[0][1]), (
            "the nested maps contributed nothing, so this walk is top-level only"
        )

    def test_the_population_is_catches_targeting_a_fail_state(self):
        """Count-pinned, so a new Fail-targeting catch shows up here.

        The number is the point: a state added with a catch onto a `Fail` state joins
        the population silently otherwise, and the analysis would misattribute it.
        """
        catches = self._catches()
        to_fail = sorted({c["state"] for c in catches if c["target_type"] == "Fail"})
        assert len(to_fail) == 9, (
            "the set of states whose caught failure ends the execution changed; "
            f"if that is intended, update this count. Currently: {to_fail}"
        )
        # And the breadth of the match is NOT what selects them.
        wide = {
            c["state"]
            for c in catches
            if c["target_type"] == "Fail" and "States.ALL" in c["errors"]
        }
        assert 0 < len(wide) < len(to_fail), (
            "the population is keyed on the catch TARGET being a Fail state, not on "
            f"States.ALL; {len(wide)} of {len(to_fail)} match broadly"
        )

    def test_a_recovery_catch_is_not_in_the_population(self):
        """A catch onto a Task or Pass state continues the execution, so no terminal
        failure follows it and nothing is misattributed. Asserted so the population is
        bounded from both sides rather than only counted."""
        kinds = {c["target_type"] for c in self._catches()}
        assert kinds & {"Task", "Pass"}, (
            "no recovery catches found; if every catch now ends the execution, the "
            "distinction this class draws no longer exists"
        )


@pytest.mark.unit
class TestTheHistoryWalkPaginatesBackwardsFromTheFailure:
    """#1127: one page holds the failure; the failing state can be further back.

    `reverseOrder=True` guarantees the failure event is in the first page, which is
    why a single page was a defensible starting point. It does not guarantee the
    `StateEntered` that names the failing state, and that event is the earlier of the
    two. The walk therefore follows `nextToken` — but backwards from the failure and
    only as far as it needs, because the whole history of a large document is
    thousands of events, nearly all irrelevant.
    """

    @staticmethod
    def _client(factory, pages: list[dict]):
        client = factory.return_value
        client.describe_execution.return_value = {"status": "FAILED"}
        client.get_execution_history.side_effect = pages
        return client

    def test_it_follows_next_token_until_the_state_is_reachable(self):
        """The defect: the state sits on the second page, so one page cannot name it."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = self._client(
                factory,
                [
                    # Newest page: the failure, and the handler it was caught by.
                    {
                        "events": [_execution_failed(900), _task_failed(899)],
                        "nextToken": "page2",
                    },
                    # Older page: the transition that names the failing state.
                    {"events": [_entered("Extraction", 898)]},
                ],
            )
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 2
        assert data["state_unresolved_due_to_truncation"] is False
        with _fixed_timeline_cap():
            assert (
                _analyze_execution_timeline(data["events"])["failure_point"]["state"]
                == "Extraction"
            )

    def test_it_stops_as_soon_as_the_state_is_reachable(self):
        """Bounded cost: the first page already explains the failure, so stop there.

        Asserted on the call count rather than on the result, because a walk that
        fetched the whole history would produce the same answer and a much larger bill
        on a document with thousands of events.
        """
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = self._client(
                factory,
                [
                    {
                        "events": [
                            _execution_failed(900),
                            _task_failed(899),
                            _entered("Extraction", 898),
                        ],
                        "nextToken": "more-exists-but-is-not-needed",
                    },
                    {"events": [_entered("OCR", 1)]},
                ],
            )
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 1
        assert data["state_unresolved_due_to_truncation"] is False

    def test_more_pages_existing_is_not_reported_as_a_limitation(self):
        """The reason the flag is not `history_truncated` any more.

        After pagination, "a nextToken came back" is true of almost every large
        execution, and one consumer reads the flag unconditionally — so the old
        meaning would attach a truncation warning to runs whose state was identified
        perfectly well. The flag now means the walk could not resolve the state.
        """
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            self._client(
                factory,
                [
                    {
                        "events": [
                            _execution_failed(900),
                            _task_failed(899),
                            _entered("Extraction", 898),
                        ],
                        "nextToken": "opaque",
                    }
                ],
            )
            data = _get_execution_data(EXECUTION_ARN)
        assert data["state_unresolved_due_to_truncation"] is False

    def test_the_page_cap_bounds_the_walk_and_is_reported(self):
        """A history that never yields the transition stops, and says it stopped."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap(pages=3):
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.return_value = {
                "events": [_task_failed(899)],
                "nextToken": "always-more",
            }
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 3
        assert data["state_unresolved_due_to_truncation"] is True

    def test_a_history_read_to_completion_is_not_reported_as_unresolved(self):
        """No nextToken means nothing was withheld, so an unnamed state is a fact about
        the execution rather than about the window — and must not be explained away as
        truncation."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            self._client(factory, [{"events": [_task_failed(899)]}])
            data = _get_execution_data(EXECUTION_ARN)
        assert data["state_unresolved_due_to_truncation"] is False

    def test_an_execution_with_no_failure_fetches_one_page(self):
        """Nothing is being explained, so there is nothing further back worth paying
        for. A succeeded execution is the common case for this."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = self._client(
                factory,
                [
                    {
                        "events": [_exited("WorkflowComplete", 900)],
                        "nextToken": "older",
                    },
                    {"events": [_entered("OCR", 1)]},
                ],
            )
            _get_execution_data(EXECUTION_ARN)
        assert client.get_execution_history.call_count == 1

    def test_the_first_page_carries_no_next_token_parameter(self):
        """`nextToken` is only sent once one has been received; sending an empty one is
        an API error rather than a first page."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = self._client(factory, [{"events": [_task_failed(1)]}])
            _get_execution_data(EXECUTION_ARN)
        first_call = client.get_execution_history.call_args_list[0]
        assert "nextToken" not in first_call.kwargs
        assert first_call.kwargs["reverseOrder"] is True
        assert first_call.kwargs["maxResults"] == 100


@pytest.mark.unit
class TestACatchHandlersOwnTransitionDoesNotEndTheWalk:
    """The window is not sufficient just because SOME older StateEntered is in it.

    On a caught failure the history reads, newest first:

        ExecutionFailed
        FailStateEntered: <handler>        <- the nearest older StateEntered
        TaskFailed
        TaskStateEntered: <the real state>

    so a rule of "the picked failure has an older StateEntered" is satisfied by the
    handler's own transition. Stopping there reports the handler AND reports it with
    the truncation flag false, which is worse than not paginating at all: the answer is
    equally wrong and the one signal that it might be wrong is gone. These tests pin
    both halves — that the walk continues, and that when it cannot, the flag says so.
    """

    @staticmethod
    def _fail_entered(name, ts):
        return _entered(name, ts, kind="Fail")

    def test_it_pages_past_the_handler_and_names_the_real_state(self):
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.side_effect = [
                # Newest page: the terminal failure and the Catch handler only.
                {
                    "events": [
                        _execution_failed(900),
                        self._fail_entered("ExtractionShardMapFailed", 899),
                    ],
                    "nextToken": "page2",
                },
                # Older page: the task failure and the state that actually failed.
                {"events": [_task_failed(898), _entered("Extraction", 897)]},
            ]
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 2, (
            "the walk stopped at the handler's own FailStateEntered"
        )
        assert data["state_unresolved_due_to_truncation"] is False
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(data["events"])
        assert result["failure_point"]["state"] == "Extraction"

    def test_when_the_pages_run_out_the_flag_says_the_state_is_unresolved(self):
        """The reviewer's measured case: no page 2 to reach, so the answer is still the
        handler — but the truncation flag must be TRUE so the summary says the state
        could not be identified, rather than presenting the handler as the finding."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap(pages=1):
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.return_value = {
                "events": [
                    _execution_failed(900),
                    self._fail_entered("ExtractionShardMapFailed", 899),
                ],
                "nextToken": "more-we-cannot-reach",
            }
            data = _get_execution_data(EXECUTION_ARN)

        assert data["state_unresolved_due_to_truncation"] is True, (
            "the handler's transition made the window look sufficient, so the answer "
            "is reported without the caveat that it may be the wrong state"
        )

    def test_an_execution_level_failure_with_the_history_complete_still_stops(self):
        """The control. With no nextToken there is nothing further back, so the last
        state entered is the only candidate and reporting it is right — this must not
        become an extra fetch or a false truncation flag."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.return_value = {
                "events": [_execution_failed(900), _entered("Extraction", 899)]
            }
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 1
        assert data["state_unresolved_due_to_truncation"] is False

    def test_a_task_level_failure_on_page_one_still_stops_at_one_page(self):
        """The other control: a task-level failure with its own transition in the
        window is sufficient, pages remaining or not, so the cheap case stays cheap."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.return_value = {
                "events": [
                    _execution_failed(900),
                    _task_failed(899),
                    _entered("Extraction", 898),
                ],
                "nextToken": "older",
            }
            data = _get_execution_data(EXECUTION_ARN)

        assert client.get_execution_history.call_count == 1
        assert data["state_unresolved_due_to_truncation"] is False


@pytest.mark.unit
class TestThePageCapIsAConfigurableFieldAndNotJustAReadKey:
    """A `get_ea_param` read is not a configuration knob on its own.

    `ErrorAnalyzerParameters.model_config` is empty, so pydantic's default
    `extra="ignore"` applies: a key nothing declares is silently dropped, and
    `get_ea_param` then returns the literal default forever. A test that pins the value
    through a mocked `get_ea_param` verifies the code path honours a configured value
    while being blind to nothing being able to supply one — the control-never-consulted
    shape, inside the test for the control. So the field is asserted on the MODEL.
    """

    FIELD = "max_stepfunction_history_pages"

    @staticmethod
    def _params():
        from idp_common.config.models import ErrorAnalyzerParameters

        return ErrorAnalyzerParameters

    def test_the_field_is_declared(self):
        assert self.FIELD in self._params().model_fields, (
            f"{self.FIELD} is read by _get_execution_data but declared on no model, so "
            "extra='ignore' drops any configured value and the default is the only "
            "reachable one"
        )
        assert self.FIELD in self._params()().model_dump()

    def test_a_configured_value_survives_the_model(self):
        """Including the string form, which is how the configuration table stores every
        numeric scalar."""
        assert getattr(self._params()(**{self.FIELD: 3}), self.FIELD) == 3
        assert getattr(self._params()(**{self.FIELD: "3"}), self.FIELD) == 3

    def test_it_is_bounded_above_as_well_as_below(self):
        """`max(1, int(x))` in the caller bounds below only, so an absurd configured
        value would attempt that many API calls. The model is where that is refused."""
        import pydantic

        for bad in (0, -1, 1_000_000):
            with pytest.raises(pydantic.ValidationError):
                self._params()(**{self.FIELD: bad})

    def test_it_shares_the_siblings_coercion(self):
        """Its eleven siblings map None and blank to the field default rather than
        raising; being absent from that validator list would have made this field the
        only one that raises on an empty configuration value."""
        for empty in (None, ""):
            assert getattr(self._params()(**{self.FIELD: empty}), self.FIELD) == 10


@pytest.mark.unit
class TestAFailedLaterPageKeepsWhatWasGathered:
    """Partial degradation, in the direction that matters.

    A page beyond the first is fetched only BECAUSE page one could not resolve the
    state — which is exactly the population that used to get a usable partial plus a
    truncation warning. Letting a throttle on page two propagate would turn that into
    no analysis at all, and pagination multiplies the chance of hitting one, so the
    regression would land hardest on the executions this walk exists to serve.
    """

    def test_a_throttle_on_the_second_page_keeps_the_first(self):
        from botocore.exceptions import ClientError

        throttle = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "GetExecutionHistory",
        )
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.side_effect = [
                {
                    "events": [
                        _execution_failed(900),
                        _entered("ExtractionShardMapFailed", 899, kind="Fail"),
                    ],
                    "nextToken": "page2",
                },
                throttle,
            ]
            data = _get_execution_data(EXECUTION_ARN)

        assert len(data["events"]) == 2, "page one's events were discarded"
        assert data["state_unresolved_due_to_truncation"] is True, (
            "a partial read must report the state as unresolved, or the handler is "
            "presented as the finding with no caveat"
        )

    def test_the_pagination_token_is_never_assigned_a_literal(self):
        """``next_token`` holds only what the service returned; the partial-read state
        has its own flag.

        This has to be a **source** assertion, and the reason is worth stating because
        the behavioural version of it is vacuous. Overloading ``next_token`` with a
        sentinel string was invisible at runtime: the assignment is immediately followed
        by ``break``, so the made-up value was never sent anywhere, and a test asserting
        that every ``nextToken`` sent came from a response passes identically on both
        spellings — measured, not assumed. So no input distinguishes them, and the only
        thing that does is the text.

        What the overload cost was legibility and a false secret-scanner hit: one
        variable carried two unrelated meanings (a token to send back, and "stop and
        report unresolved"), and a name containing "token" bound to a constant is what a
        credential scanner is built to flag. Pinning it here keeps the separation without
        adding a suppression, which would have shielded the whole line from that scanner
        for good.
        """
        import ast
        import inspect

        from idp_common.agents.error_analyzer.tools import stepfunction_tool

        tree = ast.parse(inspect.getsource(stepfunction_tool))
        literal_assignments = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(
                isinstance(t, ast.Name) and t.id == "next_token" for t in node.targets
            )
        ]
        assert not literal_assignments, (
            f"next_token is assigned a string literal at line(s) {literal_assignments}; "
            "it must only ever hold a token the service returned, with any other state "
            "kept in its own variable"
        )

    def test_a_failure_on_the_very_first_page_still_propagates(self):
        """With no events there is nothing to analyse, so the caller's own handler
        should report the read failure rather than an empty timeline."""
        with patch(f"{MODULE}.boto3.client") as factory, _fixed_timeline_cap():
            client = factory.return_value
            client.describe_execution.return_value = {"status": "FAILED"}
            client.get_execution_history.side_effect = RuntimeError("no history")
            with pytest.raises(RuntimeError):
                _get_execution_data(EXECUTION_ARN)
