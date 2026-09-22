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

        # Build analysis summary
        analysis_summary = _build_analysis_summary(
            execution_metadata["status"], timeline_analysis
        )

        # Generate recommendations
        recommendations = _generate_recommendations(timeline_analysis)

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
    ``_analyze_execution_timeline`` normalises the order it is given.
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
    execution_status: str, timeline_analysis: Dict[str, Any]
) -> str:
    """
    Build human-readable analysis summary.
    """
    analysis_summary = f"Step Function execution {execution_status}"

    if timeline_analysis.get("failure_point"):
        failure_point = timeline_analysis["failure_point"]
        analysis_summary += f" at state '{failure_point.get('state', 'Unknown')}'"
        if failure_point.get("details", {}).get("error"):
            analysis_summary += f": {failure_point['details']['error']}"

    return analysis_summary


def _generate_recommendations(timeline_analysis: Dict[str, Any]) -> List[str]:
    """
    Generate actionable recommendations based on analysis.
    """
    return [
        "Check the failure point state for specific error details",
        "Review Lambda function logs if failure occurred in Lambda task",
        "Verify input data format if failure occurred early in workflow",
        "Consider timeout adjustments if execution timed out",
    ]


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

    failure_events = [
        "ExecutionFailed",
        "TaskFailed",
        "LambdaFunctionFailed",
        "TaskTimedOut",
        "ExecutionTimedOut",
    ]

    if event_type not in failure_events:
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
    Return one page of Step Functions history events oldest-first.

    ``get_execution_history`` returns a page in either direction depending on
    ``reverseOrder``, and the page is totally ordered either way, so the direction
    can be read off the two ends and corrected by reversing. Reversing rather than
    sorting on the timestamp matters: event timestamps have millisecond resolution
    and adjacent events routinely share one, and a stable sort would leave every
    such tie in the order it arrived -- backwards, for a newest-first page.

    A list whose ends carry no comparable timestamp is returned untouched; there is
    nothing to read the direction from, and guessing would be worse than leaving it.
    """
    if len(events) < 2:
        return events

    first = events[0].get("timestamp")
    last = events[-1].get("timestamp")
    try:
        newest_first = first is not None and last is not None and first > last
    except TypeError:
        return events

    return list(reversed(events)) if newest_first else events


def _analyze_execution_timeline(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze Step Function execution timeline to identify failure patterns and state transitions.
    Processes execution events chronologically to build a timeline of state transitions
    and identify the exact point of failure with context.

    Events may arrive in either direction -- the caller fetches them newest-first so
    that a capped page covers the failure -- and are normalised to oldest-first here.
    The chronological walk is what makes the analysis correct: the failing state is
    the state most recently entered *before* the failure event, so seeing the failure
    first would report no state at all.

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

    for event in events:
        timestamp = event.get("timestamp")
        event_type = event.get("type", "")

        # Track state transitions
        if event_type == "StateEntered":
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

        elif event_type == "StateExited":
            state_name = event.get("stateExitedEventDetails", {}).get("name", "Unknown")
            timeline.append(
                {
                    "timestamp": timestamp,
                    "event": f"Exited state: {state_name}",
                    "state": state_name,
                }
            )

        # Identify failure point
        failure_details = _extract_failure_details(event)
        if failure_details and not failure_point:
            failure_point = {
                "timestamp": timestamp,
                "event_type": event_type,
                "state": last_successful_state,
                "details": failure_details,
            }

    return {
        "timeline": timeline[-max_timeline_events:],
        "failure_point": failure_point,
        "last_successful_state": last_successful_state,
    }
