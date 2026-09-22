# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Step Function tools for document-specific workflow execution analysis.
"""

import logging
from typing import Any, Dict, List, Optional

import boto3
from strands import tool

from ..config import get_ea_param

logger = logging.getLogger(__name__)

# Failure event types, split by whether the failure is attributable to a STATE or to
# the execution as a whole. The split is what lets the analysis report the state that
# failed rather than the Catch handler the workflow moved into afterwards; the union
# is what `_extract_failure_details` matches on, so the two readings are one list and
# cannot drift apart.
#
# Task-level: a state was doing work and that work failed.
_TASK_LEVEL_FAILURE_EVENTS = frozenset(
    {"TaskFailed", "LambdaFunctionFailed", "TaskTimedOut"}
)
# Execution-level: the execution ended. By this point a caught failure has already
# transitioned into its handler, so these events say nothing about which state failed
# — only why the execution stopped. Their error text is still the text to report.
_EXECUTION_LEVEL_FAILURE_EVENTS = frozenset({"ExecutionFailed", "ExecutionTimedOut"})
_FAILURE_EVENTS = _TASK_LEVEL_FAILURE_EVENTS | _EXECUTION_LEVEL_FAILURE_EVENTS


@tool
def analyze_workflow_execution(document_id: str = "") -> Dict[str, Any]:
    """
    Analyze Step Function workflow execution to identify failures and state transitions.

    Performs comprehensive analysis of document processing workflow executions by
    retrieving execution history, analyzing state transitions, identifying failure
    points, and providing actionable recommendations. Essential for troubleshooting
    document processing failures and understanding workflow behavior.

    Use this tool when:
    - Document processing failed and you need workflow analysis
    - Need to understand where in the workflow a failure occurred
    - Investigating workflow performance or timeout issues
    - Analyzing state transitions and execution timeline
    - User reports document processing stuck or failed

    Example usage:
    - "Analyze the workflow execution for document report.pdf"
    - "What went wrong in the Step Function execution for lending_package.pdf?"
    - "Show me the workflow timeline and failure point for document ABC123"
    - "Why did the document processing workflow fail for my_document.pdf?"
    - "Trace the execution flow and identify issues for this document"

    Args:
        document_id: Document filename/S3 object key (e.g., "report.pdf", "lending_package.pdf")

    Returns:
        Dict with keys:
        - execution_status (str): Overall execution status (SUCCEEDED, FAILED, TIMED_OUT, etc.)
        - duration_seconds (float): Total execution duration if completed
        - timeline_analysis (dict): Detailed timeline with state transitions and failure point
        - analysis_summary (str): Human-readable summary of execution and failure
        - recommendations (list): Actionable next steps for investigation
    """
    try:
        if not document_id:
            return _build_response(
                execution_status=None,
                analysis_summary="No document ID provided",
                recommendations=[
                    "Use search_cloudwatch_logs or fetch_recent_records for general troubleshooting"
                ],
            )

        # Get execution ARN from document record
        execution_arn = _get_execution_arn_from_document(document_id)
        if not execution_arn:
            return _build_response(
                execution_status=None,
                analysis_summary=f"No execution ARN found for document {document_id}",
                recommendations=[
                    "Use search_cloudwatch_logs for detailed error information",
                    "Verify document exists using fetch_document_record",
                ],
            )

        # Get execution data from Step Functions
        execution_data = _get_execution_data(execution_arn)

        # Analyze timeline and failures
        timeline_analysis = _analyze_execution_timeline(execution_data["events"])

        # Extract execution metadata
        execution_metadata = _extract_execution_metadata(
            execution_data["execution_response"]
        )

        history_truncated = bool(execution_data.get("history_truncated"))
        if history_truncated:
            timeline_analysis["history_truncated"] = True

        # Build analysis summary
        analysis_summary = _build_analysis_summary(
            execution_metadata["status"], timeline_analysis, history_truncated
        )

        # Generate recommendations
        recommendations = _generate_recommendations(
            timeline_analysis, history_truncated
        )

        return _build_response(
            execution_status=execution_metadata["status"],
            duration_seconds=execution_metadata["duration_seconds"],
            timeline_analysis=timeline_analysis,
            analysis_summary=analysis_summary,
            recommendations=recommendations,
        )

    except Exception as e:
        logger.error(
            f"Error analyzing workflow execution for document {document_id}: {e}"
        )
        return _build_response(
            execution_status=None,
            analysis_summary=f"Failed to analyze workflow execution: {str(e)}",
            recommendations=[
                "Use search_cloudwatch_logs for detailed error information"
            ],
        )


def _get_execution_data(execution_arn: str) -> Dict[str, Any]:
    """
    Retrieve execution details and history from Step Functions.

    The history is requested newest-first and returned newest-first. That order is
    deliberate rather than incidental: a single un-paginated page is capped at 100
    events, and on a long execution the newest 100 are the ones around the failure,
    which is the half worth having. Oldest-first would keep the beginning of the
    workflow and might not reach the failure at all.

    Consumers must therefore not assume chronological order --
    ``_analyze_execution_timeline`` orders the events it is given.

    The page is **not** paginated, so a long execution is analysed from its newest
    100 events only. ``history_truncated`` reports that, because the truncation is
    not harmless: the failure event survives the window but the failing state's
    ``StateEntered`` may not, and the analysis then reports no state at all. Without
    the flag that is indistinguishable from an execution where nothing failed.
    """
    stepfunctions_client = boto3.client("stepfunctions")

    execution_response = stepfunctions_client.describe_execution(
        executionArn=execution_arn
    )

    history_response = stepfunctions_client.get_execution_history(
        executionArn=execution_arn,
        maxResults=100,
        reverseOrder=True,  # Most recent events first; see the docstring above.
    )

    return {
        "execution_response": execution_response,
        "events": history_response.get("events", []),
        "history_truncated": bool(history_response.get("nextToken")),
    }


def _extract_execution_metadata(execution_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract execution metadata including status and duration.
    """
    execution_status = execution_response.get("status", "UNKNOWN")
    start_date = execution_response.get("startDate")
    stop_date = execution_response.get("stopDate")

    duration_seconds = None
    if start_date and stop_date:
        duration_seconds = (stop_date - start_date).total_seconds()

    return {"status": execution_status, "duration_seconds": duration_seconds}


def _build_analysis_summary(
    execution_status: str,
    timeline_analysis: Dict[str, Any],
    history_truncated: bool = False,
) -> str:
    """
    Build human-readable analysis summary.

    When the state could not be identified and the history window was truncated, the
    summary says so. "at state 'None'" on its own reads as a finding; the two facts
    together read as the measurement limit it actually is.
    """
    analysis_summary = f"Step Function execution {execution_status}"

    if timeline_analysis.get("failure_point"):
        failure_point = timeline_analysis["failure_point"]
        state = failure_point.get("state", "Unknown")
        analysis_summary += f" at state '{state}'"
        if failure_point.get("details", {}).get("error"):
            analysis_summary += f": {failure_point['details']['error']}"
        if state is None and history_truncated:
            analysis_summary += (
                " (the failing state could not be identified: only the most recent "
                "100 history events were read, and the state was entered before them)"
            )

    return analysis_summary


def _generate_recommendations(
    timeline_analysis: Dict[str, Any], history_truncated: bool = False
) -> List[str]:
    """
    Generate actionable recommendations based on analysis.
    """
    recommendations = [
        "Check the failure point state for specific error details",
        "Review Lambda function logs if failure occurred in Lambda task",
        "Verify input data format if failure occurred early in workflow",
        "Consider timeout adjustments if execution timed out",
    ]

    if history_truncated:
        recommendations.insert(
            0,
            "Only the most recent 100 execution history events were read, so an "
            "unidentified state means the window did not reach it rather than that "
            "no state failed -- inspect the full execution history in the console",
        )

    return recommendations


def _build_response(
    execution_status: Optional[str],
    duration_seconds: Optional[float] = None,
    timeline_analysis: Optional[Dict[str, Any]] = None,
    analysis_summary: str = "",
    recommendations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build unified workflow analysis response with logging.
    """
    response = {
        "execution_status": execution_status,
        "duration_seconds": duration_seconds,
        "timeline_analysis": timeline_analysis or {},
        "analysis_summary": analysis_summary,
        "recommendations": recommendations or [],
    }

    logger.info(f"Workflow analysis response: {response}")
    return response


def _extract_failure_details(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Failure parser: Extracts detailed error information from Step Function events.

    Extract detailed failure information from Step Function execution events.

    Parses different types of failure events to extract error messages, causes,
    and resource information for comprehensive failure analysis.

    Args:
        event: Step Function execution event dictionary

    Returns:
        Dict containing failure details or None if event is not a failure
    """
    event_type = event.get("type", "")

    if event_type not in _FAILURE_EVENTS:
        return None

    details = {}

    # Extract error details based on event type
    if event_type == "ExecutionFailed":
        failure_detail = event.get("executionFailedEventDetails", {})
        details = {
            "error": failure_detail.get("error", "Unknown execution error"),
            "cause": failure_detail.get("cause", "No cause provided"),
        }
    elif event_type == "TaskFailed":
        failure_detail = event.get("taskFailedEventDetails", {})
        details = {
            "error": failure_detail.get("error", "Unknown task error"),
            "cause": failure_detail.get("cause", "No cause provided"),
            "resource": failure_detail.get("resource", "Unknown resource"),
        }
    elif event_type == "LambdaFunctionFailed":
        failure_detail = event.get("lambdaFunctionFailedEventDetails", {})
        details = {
            "error": failure_detail.get("error", "Lambda function failed"),
            "cause": failure_detail.get("cause", "No cause provided"),
        }
    elif "TimedOut" in event_type:
        timeout_detail = event.get("executionTimedOutEventDetails") or event.get(
            "taskTimedOutEventDetails", {}
        )
        details = {
            "error": f"{event_type.replace('EventDetails', '')}",
            "cause": timeout_detail.get("cause", "Execution exceeded timeout limit"),
        }

    return details


def _get_execution_arn_from_document(document_id: str) -> Optional[str]:
    """
    Get execution ARN from document record using fetch_document_record.
    """
    from .dynamodb_tool import fetch_document_record

    try:
        doc_response = fetch_document_record(document_id)

        if not doc_response.get("document_found"):
            logger.warning(f"Document {document_id} not found in tracking table")
            return None

        document = doc_response.get("document", {})
        execution_arn = document.get("WorkflowExecutionArn") or document.get(
            "ExecutionArn"
        )

        if not execution_arn:
            logger.warning(
                f"No execution ARN found in document record for {document_id}"
            )
            return None

        return execution_arn

    except Exception as e:
        logger.error(f"Error retrieving execution ARN for document {document_id}: {e}")
        return None


def _to_chronological(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return Step Functions history events oldest-first, whatever order they arrive in.

    ``get_execution_history`` returns a page newest-first or oldest-first depending
    on ``reverseOrder``, and every consumer here wants chronological. The ordering
    key is the event ``id``, which the history API documents as a required integer
    numbered sequentially from one, so it is a total order with no ties and no
    direction to infer. ``timestamp`` is deliberately *not* used: it has millisecond
    resolution, adjacent events routinely share a value, and any tie-prone key
    leaves a sort dependent on the arrival order it is supposed to be correcting.

    A list whose events do not all carry an integer ``id`` is not a history page
    from the API. The direction is then read from the two ends and corrected by
    reversing, which is exact when it applies but cannot see a tie between them --
    so the fallback is logged rather than silent.
    """
    if len(events) < 2:
        return events

    if all(isinstance(event.get("id"), int) for event in events):
        return sorted(events, key=lambda event: event["id"])

    first = events[0].get("timestamp")
    last = events[-1].get("timestamp")
    try:
        newest_first = first is not None and last is not None and first > last
    except TypeError:
        newest_first = False

    logger.debug(
        "Step Functions history events carry no usable 'id'; ordering was inferred "
        "from the first and last timestamps (newest_first=%s). A page whose two "
        "ends share a timestamp cannot be told apart this way.",
        newest_first,
    )
    return list(reversed(events)) if newest_first else events


def _analyze_execution_timeline(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze Step Function execution timeline to identify failure patterns and state transitions.
    Processes execution events chronologically to build a timeline of state transitions
    and identify the exact point of failure with context.

    Events may arrive in either direction -- the caller fetches them newest-first so
    that a capped page covers the failure -- and are ordered oldest-first here. The
    chronological walk is what makes the analysis correct: the failing state is the
    state most recently entered *before* the failure event, so seeing the failure
    first would report no state at all.

    The failure reported is the **last** one in the history, not the first. Retries
    mean an execution can survive several failure events, and the terminal one is the
    only one that explains why it ended.

    Args:
        events: List of Step Function execution events, in either direction

    Returns:
        Dict containing timeline analysis, failure point, and state information
    """
    if not events:
        return {"error": "No execution events available"}

    events = _to_chronological(events)

    max_timeline_events = get_ea_param("max_stepfunction_timeline_events", 50)

    timeline = []
    failure_point = None
    last_successful_state = None
    # The state a TASK-level failure happened in, which is not the same thing as the
    # last state entered. See the comment at the failure branch below.
    last_task_failure_state = None

    for event in events:
        timestamp = event.get("timestamp")
        event_type = event.get("type", "")

        # Track state transitions. The history API prefixes every transition with
        # the state's own type -- TaskStateEntered, ChoiceStateEntered,
        # MapStateExited and six more -- and never emits a bare "StateEntered";
        # that spelling belongs to the *detail* field, stateEnteredEventDetails.
        # Matching the suffix covers all of them, including any the service adds.
        if event_type.endswith("StateEntered"):
            state_name = event.get("stateEnteredEventDetails", {}).get(
                "name", "Unknown"
            )
            timeline.append(
                {
                    "timestamp": timestamp,
                    "event": f"Entered state: {state_name}",
                    "state": state_name,
                }
            )
            last_successful_state = state_name

        elif event_type.endswith("StateExited"):
            state_name = event.get("stateExitedEventDetails", {}).get("name", "Unknown")
            timeline.append(
                {
                    "timestamp": timestamp,
                    "event": f"Exited state: {state_name}",
                    "state": state_name,
                }
            )

        # Identify the failure point. The LAST failure in the walk wins, not the
        # first: the workflow retries throttles, service exceptions and timeouts in
        # 25 places, so a recovered attempt leaves a TaskFailed in the history that
        # the execution went on to survive. Reporting the earliest one names a state
        # that succeeded and an error nobody needs to act on. The terminal failure is
        # the one the execution actually ended on, and because ExecutionFailed is the
        # last event of a failed execution, taking the last failure prefers it
        # naturally.
        failure_details = _extract_failure_details(event)
        if failure_details:
            # A TASK-level failure is attributable to the state that was doing the
            # work, so remember which state that was. An execution-level one
            # (ExecutionFailed, ExecutionTimedOut) is not: by the time it arrives the
            # workflow has usually transitioned into a Catch handler, and the last
            # state entered is that handler rather than the state that failed.
            #
            # This workflow makes that the normal case rather than an edge one: it
            # carries eleven Catch blocks, ten of them `States.ALL`, and five of the
            # seven targets are `Fail` states. So the history reads
            #
            #     TaskStateEntered: Extraction
            #     TaskFailed
            #     FailStateEntered: <handler>
            #     ExecutionFailed
            #
            # and taking the last state entered names the handler. Keeping the two
            # apart is what puts the terminal event's error text next to the state
            # that actually failed, which is the pair an operator needs to pick a log
            # group.
            if event_type in _TASK_LEVEL_FAILURE_EVENTS:
                last_task_failure_state = last_successful_state
            failure_point = {
                "timestamp": timestamp,
                "event_type": event_type,
                "state": last_task_failure_state or last_successful_state,
                "details": failure_details,
            }

    return {
        "timeline": timeline[-max_timeline_events:],
        "failure_point": failure_point,
        "last_successful_state": last_successful_state,
    }
