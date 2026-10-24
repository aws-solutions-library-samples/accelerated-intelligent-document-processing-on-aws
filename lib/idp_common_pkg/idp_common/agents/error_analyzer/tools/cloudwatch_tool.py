# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
CloudWatch tools for error analysis.

Public Strands tool functions (:func:`search_cloudwatch_logs`,
:func:`search_performance_issues`) delegate all heavy lifting to
:mod:`idp_common.monitoring.cloudwatch_logs_service`, keeping this module
as a thin orchestration layer.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from strands import tool

from idp_common.config import get_config
from idp_common.monitoring.cloudwatch_logs_service import (
    get_stack_log_groups,
    prioritize_performance_log_groups,
    search_by_document_fallback,
    search_by_request_ids,
    search_log_group,
    search_stack_wide,
)

from ..config import create_error_response
from .dynamodb_tool import fetch_document_record
from .xray_tool import extract_lambda_request_ids

logger = logging.getLogger(__name__)

#: Upper bounds the tool descriptions advertise. A configured default above one of
#: them is clamped to it: the configuration model accepts any positive value, and a
#: saved profile carrying a larger one must still load.
MAX_HOURS_BACK = 168
MAX_LOG_GROUPS = 50


def _get_params() -> Dict[str, Any]:
    """
    Load error-analyzer parameters from IDPConfig.

    Returns a dict with all ``ErrorAnalyzerParameters`` fields so callers
    can use simple dict lookups without catching AttributeError everywhere.
    ``lookback_hours`` is the one entry read from the agent's own section
    rather than from ``parameters``.
    """
    try:
        config = get_config(as_model=True)
        error_analyzer = config.agents.error_analyzer
        p = error_analyzer.parameters
        return {
            "lookback_hours": error_analyzer.lookback_hours,
            "max_log_events": p.max_log_events,
            "max_log_groups": p.max_log_groups,
            "max_log_message_length": p.max_log_message_length,
            "settings_cache_ttl_seconds": p.settings_cache_ttl_seconds,
        }
    except (AttributeError, KeyError) as exc:
        logger.warning(
            "Could not load error analyzer parameters, using defaults: %s", exc
        )
        return {
            "lookback_hours": 24,
            "max_log_events": 5,
            "max_log_groups": 20,
            "max_log_message_length": 400,
            "settings_cache_ttl_seconds": 300,
        }


# =============================================================================
# PUBLIC TOOL FUNCTIONS
# =============================================================================


@tool
def search_cloudwatch_logs(
    document_id: str = "",
    filter_pattern: str = "ERROR",
    hours_back: Optional[int] = None,
    max_log_events: int = 10,
    max_log_groups: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Search CloudWatch logs for errors and failures in the IDP system.

    Use this tool when:
    - User reports document processing failures or errors
    - Need to investigate why a specific document failed to process
    - Looking for system-wide errors or patterns across the stack
    - Troubleshooting Lambda function failures or timeouts
    - Analyzing error trends or recurring issues

    Args:
        document_id: Document filename (e.g., "report.pdf"). When provided, performs targeted
                    search for that specific document using processing timestamps and Lambda
                    request IDs. When omitted, searches across all recent system logs.
        filter_pattern: Error pattern to search for - "ERROR", "Exception", "Failed", "TIMEOUT" (default: "ERROR")
        hours_back: Hours to look back from now (default: the configured lookback_hours,
                   24 unless changed; max: 168). Only used for system-wide searches when
                   document_id is not provided. Omit it unless the user asked for a window.
        max_log_events: Maximum error events to return per log group (default: 10, max: 50)
        max_log_groups: Maximum log groups to search (default: the configured
                   max_log_groups, 20 unless changed; max: 50)

    Returns:
        Dict containing error events, search metadata, and processing context
    """
    try:
        params = _get_params()
        effective_hours_back = (
            min(params["lookback_hours"], MAX_HOURS_BACK)
            if hours_back is None
            else hours_back
        )
        effective_max_log_groups = max_log_groups or min(
            params["max_log_groups"], MAX_LOG_GROUPS
        )
        effective_max_log_events = max_log_events or params["max_log_events"]
        msg_max_len = params["max_log_message_length"]
        ttl = params["settings_cache_ttl_seconds"]

        if document_id:
            return _search_document_logs(
                document_id,
                filter_pattern,
                effective_max_log_events,
                effective_max_log_groups,
                msg_max_len,
                ttl,
            )
        else:
            return _search_stack_logs(
                filter_pattern,
                effective_hours_back,
                effective_max_log_events,
                effective_max_log_groups,
                msg_max_len,
                ttl,
            )

    except Exception as e:
        logger.error("CloudWatch log search failed: %s", e)
        return create_error_response(str(e), document_id=document_id, events_found=0)


@tool
def search_performance_issues(
    issue_type: str = "performance",
    hours_back: Optional[int] = None,
    max_log_events: int = 10,
) -> Dict[str, Any]:
    """
    Search CloudWatch logs for system performance and infrastructure issues.

    Use this tool when:
    - System is running slowly or unresponsively
    - Investigating throttling or rate limiting issues
    - Checking for timeout or capacity problems
    - Analyzing infrastructure performance bottlenecks
    - Looking for capacity or concurrency constraints

    Args:
        issue_type: Type of performance issue to search for:
                   "performance" (default) - General performance issues
                   "throttling" - Rate limiting and throttling
                   "timeout" - Timeout-related issues
                   "capacity" - Capacity and concurrency limits
        hours_back: Hours to look back from now (default: the configured lookback_hours,
                   24 unless changed; max: 168). Omit it unless the user asked for a window.
        max_log_events: Maximum events to return per log group (default: 10, max: 50)

    Returns:
        Dict containing performance events, search metadata, and infrastructure context
    """
    try:
        params = _get_params()
        effective_hours_back = (
            min(params["lookback_hours"], MAX_HOURS_BACK)
            if hours_back is None
            else hours_back
        )
        msg_max_len = params["max_log_message_length"]
        ttl = params["settings_cache_ttl_seconds"]

        search_patterns = _get_performance_patterns(issue_type)
        stack_name = _get_stack_name()

        raw_log_groups = get_stack_log_groups(ttl_seconds=ttl)
        log_groups = prioritize_performance_log_groups(raw_log_groups)

        if not log_groups:
            return {
                "analysis_type": "performance_issues",
                "issue_type": issue_type,
                "stack_name": stack_name,
                "events_found": 0,
                "message": "No log groups found",
            }

        combined_pattern = " OR ".join(search_patterns)
        logger.info("Performance search — combined pattern: %s", combined_pattern)

        all_results: List[Dict[str, Any]] = []
        total_events = 0

        for log_group in log_groups:
            search_result = search_log_group(
                log_group_name=log_group["name"],
                filter_pattern=combined_pattern,
                hours_back=effective_hours_back,
                max_events=max_log_events,
                log_message_max_length=msg_max_len,
            )

            if search_result.get("events_found", 0) > 0:
                returned_events = search_result.get("events", [])
                all_results.append(
                    {
                        "log_group": log_group["name"],
                        "search_pattern": combined_pattern,
                        "events_found": len(returned_events),
                        "events": returned_events,
                        "performance_indicators": [
                            p
                            for p in search_patterns
                            if p.lower()
                            in " ".join(
                                [e.get("message", "") for e in returned_events]
                            ).lower()
                        ],
                    }
                )
                total_events += len(returned_events)

        response = {
            "analysis_type": "performance_issues",
            "issue_type": issue_type,
            "stack_name": stack_name,
            "search_patterns_used": search_patterns,
            "total_events_found": total_events,
            "log_groups_searched": len(all_results),
            "results": all_results,
        }
        logger.info("Performance search response: %s", response)
        return response

    except Exception as e:
        logger.error("Performance search failed: %s", e)
        return create_error_response(str(e), events_found=0)


# =============================================================================
# PRIVATE HELPERS
# =============================================================================


def _get_performance_patterns(issue_type: str) -> List[str]:
    performance_patterns = {
        "performance": ["timeout", "slow"],
        "throttling": ["throttl", "limit exceeded"],
        "timeout": ["timeout", "timed out"],
        "capacity": ["concurrent", "limit"],
    }
    return performance_patterns.get(issue_type, performance_patterns["performance"])


def _get_stack_name(document_record: Optional[Dict[str, Any]] = None) -> str:
    if document_record:
        try:
            extracted = _extract_stack_name(document_record)
            if extracted:
                return extracted
        except Exception as exc:
            logger.warning("Failed to extract stack name from document ARN: %s", exc)

    env_stack_name = os.environ.get("AWS_STACK_NAME", "")
    if env_stack_name:
        return env_stack_name

    raise ValueError("No stack name available from document context or environment")


def _search_document_logs(
    document_id: str,
    filter_pattern: str,
    max_log_events: int,
    max_log_groups: int,
    log_message_max_length: int,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """Document-specific search with DynamoDB context and X-Ray tracing."""
    context = _get_document_context(document_id)
    if "error" in context:
        return context

    actual_stack_name = _get_stack_name(context.get("document_record"))
    logger.info(
        "Document search for '%s' using stack: %s", document_id, actual_stack_name
    )

    document_status = context["document_record"].get("ObjectStatus") or context[
        "document_record"
    ].get("WorkflowStatus")
    logger.info("Document status for log prioritisation: %s", document_status)

    log_groups = get_stack_log_groups(
        document_status=document_status, ttl_seconds=ttl_seconds
    )
    if not log_groups:
        return {
            "document_id": document_id,
            "events_found": 0,
            "message": f"No log groups found for stack {actual_stack_name}",
        }

    groups_to_search = log_groups[:max_log_groups]
    logger.info(
        "Searching %d log groups (max: %d): %s",
        len(groups_to_search),
        max_log_groups,
        [lg["name"] for lg in groups_to_search],
    )

    time_window = _get_processing_time_window(context["document_record"])
    request_ids_info = _prioritize_request_ids(
        context["document_record"], context["lambda_function_to_request_id_map"]
    )

    search_results = search_by_request_ids(
        request_ids_info=request_ids_info,
        lambda_function_to_request_id_map=context["lambda_function_to_request_id_map"],
        groups_to_search=groups_to_search,
        time_window=time_window,
        max_log_events=max_log_events,
        max_log_groups=max_log_groups,
        log_message_max_length=log_message_max_length,
    )

    if search_results["total_events"] == 0:
        search_results = search_by_document_fallback(
            document_id=document_id,
            groups_to_search=groups_to_search,
            time_window=time_window,
            max_log_events=max_log_events,
            log_message_max_length=log_message_max_length,
        )

    return _build_response(
        document_id,
        context["document_record"],
        context["xray_trace_id"],
        actual_stack_name,
        context["lambda_function_to_request_id_map"],
        search_results,
    )


def _search_stack_logs(
    filter_pattern: str,
    hours_back: int,
    max_log_events: int,
    max_log_groups: int,
    log_message_max_length: int,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """Stack-wide search across all stack log groups."""
    stack_name = _get_stack_name()
    logger.info("System-wide search using stack: %s", stack_name)

    results = search_stack_wide(
        filter_pattern=filter_pattern,
        hours_back=hours_back,
        max_log_events=max_log_events,
        max_log_groups=max_log_groups,
        log_message_max_length=log_message_max_length,
        ttl_seconds=ttl_seconds,
    )

    if results["log_groups_searched"] == 0:
        return {
            "stack_name": stack_name,
            "events_found": 0,
            "message": "No log groups found",
        }

    return {
        "analysis_type": "system_wide",
        "stack_name": stack_name,
        "filter_pattern": filter_pattern,
        "total_events_found": results["total_events"],
        "log_groups_searched": results["log_groups_searched"],
        "results": results["all_results"],
    }


def _get_document_context(document_id: str) -> Dict[str, Any]:
    """Get document context from DynamoDB and extract X-Ray information."""
    dynamodb_response = fetch_document_record(document_id)
    if not dynamodb_response.get("document_found"):
        return {
            "analysis_type": "document_not_found",
            "document_id": document_id,
            "error": dynamodb_response.get("reason", "Document not found"),
            "events_found": 0,
        }

    document_record = dynamodb_response.get("document", {})
    xray_trace_id = document_record.get("TraceId")
    lambda_function_to_request_id_map: Dict[str, str] = {}

    if xray_trace_id:
        try:
            lambda_function_to_request_id_map = extract_lambda_request_ids(
                xray_trace_id
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract Lambda request IDs from X-Ray trace %s: %s",
                xray_trace_id,
                exc,
            )

    return {
        "document_record": document_record,
        "xray_trace_id": xray_trace_id,
        "lambda_function_to_request_id_map": lambda_function_to_request_id_map,
    }


def _extract_stack_name(document_record: Dict[str, Any]) -> str:
    """Extract stack name from Step Functions execution ARN."""
    arn = document_record.get("WorkflowExecutionArn") or document_record.get(
        "ExecutionArn"
    )
    if arn:
        parts = arn.split(":")
        if len(parts) >= 7:
            state_machine_name = parts[6].split("-DocumentProcessingWorkflow")[0]
            if state_machine_name:
                return state_machine_name
    return ""


def _get_processing_time_window(document_record: Dict[str, Any]) -> Dict[str, Any]:
    """Extract processing time window based on document status."""
    logger.info("Document record: %s", document_record)

    document_status = document_record.get("ObjectStatus") or document_record.get(
        "WorkflowStatus"
    )
    logger.info("Document status: %s", document_status)

    start_time = None
    end_time = None

    if document_record.get("InitialEventTime"):
        start_time = datetime.fromisoformat(
            document_record["InitialEventTime"].replace("Z", "+00:00")
        )
    if document_record.get("CompletionTime"):
        end_time = datetime.fromisoformat(
            document_record["CompletionTime"].replace("Z", "+00:00")
        )

    if document_status == "FAILED" and end_time:
        buffer = timedelta(minutes=2.5)
        return {"start_time": end_time - buffer, "end_time": end_time + buffer}
    elif document_status == "IN_PROGRESS" or not end_time:
        now = datetime.now()
        return {"start_time": now - timedelta(minutes=30), "end_time": now}
    elif start_time and end_time:
        buffer = timedelta(seconds=30)
        return {"start_time": start_time - buffer, "end_time": end_time + buffer}

    return {"start_time": start_time, "end_time": end_time}


def _prioritize_request_ids(
    document_record: Dict[str, Any],
    lambda_function_to_request_id_map: Dict[str, str],
) -> Dict[str, Any]:
    """Prioritise request IDs based on document failure status."""
    document_status = document_record.get("ObjectStatus") or document_record.get(
        "WorkflowStatus"
    )
    request_ids_to_search = list(lambda_function_to_request_id_map.values())

    if document_status == "FAILED" and lambda_function_to_request_id_map:
        primary_failed_function = list(lambda_function_to_request_id_map.keys())[-1]
        primary_failed_request_id = lambda_function_to_request_id_map[
            primary_failed_function
        ]
        request_ids_to_search = [primary_failed_request_id] + [
            rid for rid in request_ids_to_search if rid != primary_failed_request_id
        ]

    return {
        "document_status": document_status,
        "request_ids_to_search": request_ids_to_search,
    }


def _build_response(
    document_id: str,
    document_record: Dict[str, Any],
    xray_trace_id: str,
    actual_stack_name: str,
    lambda_function_to_request_id_map: Dict[str, str],
    search_results: Dict[str, Any],
) -> Dict[str, Any]:
    """Build final response with all search results and metadata."""
    document_status = document_record.get("ObjectStatus") or document_record.get(
        "WorkflowStatus"
    )
    execution_arn = document_record.get("WorkflowExecutionArn") or document_record.get(
        "ExecutionArn"
    )
    log_groups_searched = len(
        set(result.get("log_group", "") for result in search_results["all_results"])
    )

    response = {
        "document_id": document_id,
        "document_status": document_status,
        "execution_arn": execution_arn,
        "xray_trace_id": xray_trace_id,
        "stack_name_used": actual_stack_name,
        "search_method_used": search_results["search_method_used"],
        "lambda_functions_found": list(lambda_function_to_request_id_map.keys()),
        "log_groups_searched": log_groups_searched,
        "total_events_found": search_results["total_events"],
        "results": search_results["all_results"],
    }
    logger.info("CloudWatch document logs response: %s", response)
    return response
