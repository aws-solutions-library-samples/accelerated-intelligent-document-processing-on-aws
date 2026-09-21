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

The central thing these tests establish is a **direction-of-order** defect, filed as
#1081. `_analyze_execution_timeline` is correct when fed events oldest-first, and its
caller fetches them newest-first (`reverseOrder=True`). Read in isolation the
function is right; read with its caller it reports `None` as the failing state and
the *first* state of the execution as the last successful one. Both orders are
therefore exercised explicitly here: the chronological cases pin the logic that
works, and the reverse-order cases are strict `xfail`s that pin what the caller
actually produces. That pairing is what makes the defect legible rather than looking
like a flaky assertion.

Everything is offline; the Step Functions client is stubbed and the DynamoDB lookup
is patched at its import site.
"""

from datetime import datetime, timedelta, timezone
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


def _entered(name: str, timestamp: int) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "StateEntered",
        "stateEnteredEventDetails": {"name": name},
    }


def _exited(name: str, timestamp: int) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "StateExited",
        "stateExitedEventDetails": {"name": name},
    }


def _task_failed(timestamp: int, error: str = "Boom") -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "TaskFailed",
        "taskFailedEventDetails": {
            "error": error,
            "cause": "stack trace",
            "resource": "lambda:invoke",
        },
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


def _fixed_timeline_cap(cap: int = 50):
    return patch(f"{MODULE}.get_ea_param", side_effect=lambda field, default: cap)


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

    def test_only_the_first_failure_is_recorded(self):
        # Later failures are usually consequences of the first, and the summary
        # names exactly one state.
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(
                [
                    _entered("OCR", 1),
                    _task_failed(2, error="First"),
                    _entered("Retry", 3),
                    _task_failed(4, error="Second"),
                ]
            )
        assert result["failure_point"]["details"]["error"] == "First"
        assert result["failure_point"]["state"] == "OCR"

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

    `_get_execution_data` passes `reverseOrder=True`, so these three cases describe
    the values the agent reports today. All are wrong, and all are issue #1081.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="The history is fetched newest-first and analysed as if oldest-first, "
        "so the failure event is seen before any StateEntered and the failing state "
        "is reported as None. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1081",
    )
    def test_the_failing_state_is_identified_from_a_newest_first_history(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(CHRONOLOGICAL_HISTORY)))
        assert result["failure_point"]["state"] == "Extraction"

    @pytest.mark.xfail(
        strict=True,
        reason="With a newest-first history the reversed walk ends on the FIRST state "
        "of the execution, so last_successful_state reports OCR rather than "
        "Extraction. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1081",
    )
    def test_the_last_successful_state_is_correct_from_a_newest_first_history(self):
        with _fixed_timeline_cap():
            result = _analyze_execution_timeline(list(reversed(CHRONOLOGICAL_HISTORY)))
        assert result["last_successful_state"] == "Extraction"

    @pytest.mark.xfail(
        strict=True,
        reason="timeline[-N:] takes the tail of a newest-first list, which is the "
        "BEGINNING of the execution, so a truncated timeline contains nothing near "
        "the failure. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1081",
    )
    def test_truncation_keeps_the_events_near_the_failure(self):
        with _fixed_timeline_cap(2):
            timeline = _analyze_execution_timeline(
                list(reversed(CHRONOLOGICAL_HISTORY))
            )["timeline"]
        assert "Extraction" in [entry["state"] for entry in timeline]

    def test_the_failure_details_themselves_survive_the_wrong_order(self):
        # The error and cause come from the failure event itself rather than from
        # surrounding context, so they are correct in either order. Worth pinning:
        # it is why the reported summary looks plausible despite naming no state.
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
        # value None, which is exactly what #1081 produces. This is why the
        # user-visible sentence reads "at state 'None'".
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
        # This is the flag that makes #1081 happen. Pinned deliberately: whoever
        # fixes that issue has to decide between reversing the list and dropping
        # this flag, and dropping it changes which events survive maxResults.
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
                    # Chronological, so the analysis is the correct one. The tool's own
                    # fetch supplies the reverse; see #1081.
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
