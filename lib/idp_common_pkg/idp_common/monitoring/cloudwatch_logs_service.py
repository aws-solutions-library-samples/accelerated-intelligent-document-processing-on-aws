# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
CloudWatch Logs service for the IDP monitoring foundation library.

Provides reusable, testable helper functions for searching CloudWatch log groups.
These functions are consumed by the error analyzer tools layer and can be used
independently by other monitoring components.

Key capabilities:
- TTL-cached SSM settings lookup via :class:`SettingsCache`
- Configurable limits (events, log groups, message length) via ``ErrorAnalyzerParameters``
- Log message truncation instead of silent dropping
- Targeted search by Lambda request IDs across log groups
- Status-aware log group prioritisation

Usage::

    from idp_common.monitoring.cloudwatch_logs_service import (
        get_stack_log_groups,
        search_log_group,
        search_by_request_ids,
        search_by_document_fallback,
        search_stack_wide,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import boto3

from idp_common.monitoring.settings_cache import SettingsCache

logger = logging.getLogger(__name__)

#: The range the CloudWatch Logs ``FilterLogEvents`` service model declares for
#: ``limit`` (its ``EventsLimit`` shape). A test asserts these against botocore.
FILTER_LOG_EVENTS_MIN_LIMIT = 1
FILTER_LOG_EVENTS_MAX_LIMIT = 10000

# ---------------------------------------------------------------------------
# Module-level SettingsCache singleton (replaced in tests via reset_settings_cache)
#
# TODO: This module maintains its own _settings_cache singleton in parallel
# with the one in idp_common.monitoring.settings_cache. Both are backed by
# the same SSM parameter but are separate Python objects, so a cold Lambda
# invocation may issue two SSM GetParameter calls instead of one.
# Unifying these into a single shared cache is tracked as a follow-up task.
# ---------------------------------------------------------------------------
_settings_cache: Optional[SettingsCache] = None


def _get_settings_cache(ttl_seconds: int = 300) -> SettingsCache:
    """Return the module-level :class:`SettingsCache`, creating it on first access."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = SettingsCache(ttl_seconds=ttl_seconds)
    return _settings_cache


def reset_settings_cache(
    ttl_seconds: int = 300,
    ssm_client: Optional[Any] = None,
) -> None:
    """
    Replace the module-level :class:`SettingsCache` with a fresh instance.

    **For testing only.** Use this in test fixtures to inject a mock SSM client
    or reset state between test cases.

    Args:
        ttl_seconds: TTL for the new cache (default: 300).
        ssm_client:  Optional pre-built SSM mock client to inject.
    """
    global _settings_cache
    _settings_cache = SettingsCache(ttl_seconds=ttl_seconds, ssm_client=ssm_client)


# ---------------------------------------------------------------------------
# Settings / log-group discovery
# ---------------------------------------------------------------------------


def get_cloudwatch_log_groups(ttl_seconds: int = 300) -> List[str]:
    """
    Return the list of CloudWatch log group names from the SSM settings cache.

    Uses a module-level :class:`SettingsCache` so that repeated calls within a
    Lambda invocation do not incur additional SSM API calls unless the TTL has
    expired.

    Args:
        ttl_seconds: TTL used when creating the cache on first access.

    Returns:
        List of log group name strings, empty if not configured.
    """
    return _get_settings_cache(ttl_seconds=ttl_seconds).get_cloudwatch_log_groups()


def get_stack_log_groups(
    document_status: Optional[str] = None,
    ttl_seconds: int = 300,
) -> List[Dict[str, str]]:
    """
    Return prioritised log groups from SSM settings, optionally status-aware.

    Args:
        document_status: DynamoDB document status string (e.g. "FAILED",
                         "IN_PROGRESS") used to choose the prioritisation
                         strategy.  ``None`` uses the default strategy.
        ttl_seconds:     SSM cache TTL (passed through to :func:`get_cloudwatch_log_groups`).

    Returns:
        List of ``{"name": "<log_group_name>"}`` dicts ordered by priority.
    """
    raw_groups = get_cloudwatch_log_groups(ttl_seconds=ttl_seconds)
    if not raw_groups:
        logger.warning("CloudWatchLogGroups not found in SSM Settings")
        return []

    log_groups = [{"name": lg} for lg in raw_groups]
    logger.info(
        "Log groups from settings: [%d] %s",
        len(log_groups),
        [lg["name"] for lg in log_groups],
    )
    prioritised = _prioritize_log_groups(log_groups, document_status)
    logger.info("Prioritised log groups: %s", [lg["name"] for lg in prioritised])
    return prioritised


def _prioritize_log_groups(
    log_groups: List[Dict[str, str]],
    document_status: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Prioritise log groups by business-logic importance.

    - ``FAILED``: focus on Classification / Extraction functions first
    - ``IN_PROGRESS``: focus on QueueProcessor / Workflow first
    - default/``COMPLETED``: standard ordering
    """
    if document_status == "FAILED":
        priority_patterns = [
            "Classification",
            "Extraction",
            "Function",
            "Processor",
            "Workflow",
            "QueueSender",
        ]
    elif document_status == "IN_PROGRESS":
        priority_patterns = [
            "Processor",
            "Workflow",
            "Function",
            "QueueSender",
        ]
    else:
        priority_patterns = [
            "Function",
            "Workflow",
            "Processor",
            "QueueSender",
        ]

    prioritised: List[Dict[str, str]] = []
    remaining = list(log_groups)

    for pattern in priority_patterns:
        matching = [lg for lg in remaining if pattern.lower() in lg["name"].lower()]
        prioritised.extend(matching)
        remaining = [lg for lg in remaining if lg not in matching]

    prioritised.extend(remaining)
    return prioritised


def prioritize_performance_log_groups(
    log_groups: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    Prioritise log groups for performance-issue analysis.

    Focuses on infrastructure components commonly responsible for bottlenecks.

    Args:
        log_groups: Full list of log group dicts.

    Returns:
        Re-ordered list with performance-relevant groups first.
    """
    logger.info("Performance log prioritisation — using infrastructure-first strategy")
    performance_priority_patterns = [
        "Processor",
        "Workflow",
        "Function",
        "QueueSender",
    ]

    prioritised: List[Dict[str, str]] = []
    remaining = list(log_groups)

    for pattern in performance_priority_patterns:
        matching = [lg for lg in remaining if pattern.lower() in lg["name"].lower()]
        prioritised.extend(matching)
        remaining = [lg for lg in remaining if lg not in matching]

    prioritised.extend(remaining)
    return prioritised


# ---------------------------------------------------------------------------
# Low-level CloudWatch API wrapper
# ---------------------------------------------------------------------------


def search_log_group(
    log_group_name: str,
    filter_pattern: str = "",
    hours_back: int = 24,
    max_events: int = 10,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    request_id: str = "",
    log_message_max_length: int = 400,
) -> Dict[str, Any]:
    """
    Search a single CloudWatch log group for matching events.

    Uses ``filter_log_events`` with a combined CloudWatch filter pattern.
    Long messages are **truncated** rather than silently dropped, preserving
    the first ``log_message_max_length`` characters of every matched event.

    Args:
        log_group_name:        Target log group name.
        filter_pattern:        CloudWatch filter pattern string (empty = no filter).
        hours_back:            Look-back window used when no explicit time range
                               is provided.
        max_events:            Maximum matching events to return.
        start_time:            Explicit window start (overrides ``hours_back``).
        end_time:              Explicit window end (overrides ``hours_back``).
        request_id:            When non-empty, only return events containing
                               this Lambda request ID.
        log_message_max_length: Maximum characters to keep per message before
                                appending ``"... [truncated]"``.

    Returns:
        Dict with keys ``log_group``, ``events_found``, ``events``, and
        ``filter_pattern``.
    """
    try:
        client = boto3.client("logs")

        # Resolve time window
        if start_time is not None and end_time is not None:
            search_start = start_time
            search_end = end_time
        else:
            search_end = datetime.now()
            search_start = search_end - timedelta(hours=hours_back)

        # Fetch more raw events to compensate for post-processing filters
        search_limit = (
            int(max_events) * 5
            if filter_pattern
            in ["[ERROR]", "[WARN]", "ERROR:", "WARN:", "Exception", "Failed"]
            else int(max_events)
        )

        # Keep the request inside the range the FilterLogEvents service model
        # declares. Below the minimum, botocore rejects the call before sending it
        # and the handler below reports the rejection as a group with no events.
        # botocore does not check the maximum, so a larger value would reach the
        # service; the cap keeps the request within the declared range regardless.
        search_limit = min(
            max(search_limit, FILTER_LOG_EVENTS_MIN_LIMIT), FILTER_LOG_EVENTS_MAX_LIMIT
        )

        params: Dict[str, Any] = {
            "logGroupName": log_group_name,
            "startTime": int(search_start.timestamp() * 1000),
            "endTime": int(search_end.timestamp() * 1000),
            "limit": search_limit,
        }

        final_filter_pattern = _build_filter_pattern(filter_pattern, request_id)
        if final_filter_pattern:
            params["filterPattern"] = final_filter_pattern

        logger.debug("CloudWatch API params: %s", params)

        try:
            response = client.filter_log_events(**params)
            logger.debug(
                "CloudWatch API returned %d raw events for %s",
                len(response.get("events", [])),
                log_group_name,
            )
        except client.exceptions.ResourceNotFoundException:
            logger.warning("Log group %s not found", log_group_name)
            return {
                "log_group": log_group_name,
                "events_found": 0,
                "events": [],
                "filter_pattern": final_filter_pattern,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("CloudWatch API error for %s: %s", log_group_name, exc)
            return {
                "log_group": log_group_name,
                "events_found": 0,
                "events": [],
                "error": str(exc),
            }

        events: List[Dict[str, Any]] = []
        for raw_event in response.get("events", []):
            message: str = raw_event["message"]

            if _should_exclude_log_event(message, filter_pattern):
                continue

            # When using request ID search, keep only events containing that ID
            if request_id and request_id not in message:
                continue

            # Truncate long messages instead of skipping them — the tail of a
            # long stack trace often contains the actual root cause.
            truncated_message = _truncate_message(message, log_message_max_length)

            events.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        raw_event["timestamp"] / 1000
                    ).isoformat(),
                    "message": truncated_message,
                    "log_stream": raw_event.get("logStreamName", ""),
                }
            )
            if len(events) >= max_events:
                break

        return {
            "log_group": log_group_name,
            "events_found": len(events),
            "events": events,
            "filter_pattern": final_filter_pattern,
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error searching log group %s: %s", log_group_name, exc)
        return {
            "log_group": log_group_name,
            "events_found": 0,
            "events": [],
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Multi-group search helpers
# ---------------------------------------------------------------------------


def search_by_request_ids(
    request_ids_info: Dict[str, Any],
    lambda_function_to_request_id_map: Dict[str, str],
    groups_to_search: List[Dict[str, Any]],
    time_window: Dict[str, Any],
    max_log_events: int,
    max_log_groups: int,
    log_message_max_length: int = 400,
) -> Dict[str, Any]:
    """
    Search logs using Lambda request IDs with function-specific targeting.

    Iterates over the prioritised request IDs and searches log groups that
    match the corresponding Lambda function type.  Search continues across
    **all** functions in the failed set; it stops early only when the
    primary failed function's log group has been searched (avoids masking
    root causes from secondary functions).

    Args:
        request_ids_info:                  Dict with ``document_status`` and
                                           ``request_ids_to_search`` list.
        lambda_function_to_request_id_map: Map of function name → request ID.
        groups_to_search:                  Pre-sliced log group list.
        time_window:                       Dict with ``start_time`` / ``end_time``.
        max_log_events:                    Per-group event cap.
        max_log_groups:                    Total log group cap (respected by caller
                                           but enforced here too).
        log_message_max_length:            Passed through to :func:`search_log_group`.

    Returns:
        Dict with ``all_results``, ``total_events``, and ``search_method_used``.
    """
    all_results: List[Dict[str, Any]] = []
    total_events = 0
    search_method_used = "none"
    primary_function_searched = False

    request_ids_to_search = request_ids_info.get("request_ids_to_search", [])
    primary_request_id = request_ids_to_search[0] if request_ids_to_search else None

    for request_id in request_ids_to_search:
        if total_events > 0 and primary_function_searched:
            # We have results from the primary failed function — stop here to
            # avoid polluting the root cause with secondary function noise.
            logger.info(
                "Primary function searched with results, stopping request ID search"
            )
            break

        function_name = next(
            (
                func
                for func, rid in lambda_function_to_request_id_map.items()
                if rid == request_id
            ),
            "Unknown",
        )

        function_type = _extract_function_type(function_name)
        matching_log_groups = (
            [
                lg
                for lg in groups_to_search
                if function_type and function_type in lg["name"]
            ]
            if function_type
            else []
        )

        if matching_log_groups:
            for log_group in matching_log_groups:
                search_result = search_log_group(
                    log_group_name=log_group["name"],
                    filter_pattern="ERROR",
                    max_events=max_log_events * 3,
                    start_time=time_window.get("start_time"),
                    end_time=time_window.get("end_time"),
                    request_id=request_id,
                    log_message_max_length=log_message_max_length,
                )

                if search_result.get("events_found", 0) > 0:
                    search_method_used = "lambda_request_id"
                    logger.info(
                        "Found %d error events in %s for %s (request_id=%s)",
                        search_result["events_found"],
                        log_group["name"],
                        function_name,
                        request_id,
                    )
                    all_results.append(
                        {
                            "log_group": log_group["name"],
                            "lambda_function_name": function_name,
                            "request_id": request_id,
                            "search_method": "lambda_request_id",
                            "events_found": search_result["events_found"],
                            "events": search_result["events"],
                        }
                    )
                    total_events += search_result["events_found"]
        else:
            logger.info(
                "No matching log group for Lambda function %s (%s)",
                function_name,
                function_type,
            )

        # Mark primary function as searched after its first pass
        if request_id == primary_request_id:
            primary_function_searched = True

    logger.info("Request-ID search completed: %d total events", total_events)
    return {
        "all_results": all_results,
        "total_events": total_events,
        "search_method_used": search_method_used,
    }


def search_by_document_fallback(
    document_id: str,
    groups_to_search: List[Dict[str, Any]],
    time_window: Dict[str, Any],
    max_log_events: int,
    log_message_max_length: int = 400,
) -> Dict[str, Any]:
    """
    Fallback log search using the document filename as the filter pattern.

    Used when no Lambda request IDs are available from X-Ray.  Searches
    each provided log group for events containing the document identifier
    and retains only those that look like genuine errors.

    Args:
        document_id:            Document filename or S3 key.
        groups_to_search:       Log groups to scan (caller controls the slice).
        time_window:            Dict with ``start_time`` / ``end_time``.
        max_log_events:         Per-group event cap.
        log_message_max_length: Passed through to :func:`search_log_group`.

    Returns:
        Dict with ``all_results``, ``total_events``, and ``search_method_used``.
    """
    all_results: List[Dict[str, Any]] = []
    total_events = 0
    search_method_used = "none"

    filename = document_id.split("/")[-1]
    doc_identifier = filename.replace(".pdf", "").replace(".", "-")

    for log_group in groups_to_search:
        search_result = search_log_group(
            log_group_name=log_group["name"],
            filter_pattern=doc_identifier,
            max_events=max_log_events,
            start_time=time_window.get("start_time"),
            end_time=time_window.get("end_time"),
            log_message_max_length=log_message_max_length,
        )

        if search_result.get("events_found", 0) > 0:
            error_events = [
                e
                for e in search_result.get("events", [])
                if any(
                    term in e.get("message", "").upper()
                    for term in ["ERROR", "EXCEPTION", "FAILED", "TIMEOUT"]
                )
            ]

            if error_events:
                search_method_used = "document_specific_fallback"
                logger.info(
                    "Found %d document-specific error events in %s (fallback)",
                    len(error_events),
                    log_group["name"],
                )
                all_results.append(
                    {
                        "log_group": log_group["name"],
                        "search_method": "document_specific_fallback",
                        "events_found": len(error_events),
                        "events": error_events,
                    }
                )
                total_events += len(error_events)
                break  # One group with results is sufficient for fallback

    logger.info("Fallback search completed: %d total events", total_events)
    return {
        "all_results": all_results,
        "total_events": total_events,
        "search_method_used": search_method_used,
    }


def search_stack_wide(
    filter_pattern: str,
    hours_back: int,
    max_log_events: int,
    max_log_groups: int,
    log_message_max_length: int = 400,
    ttl_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Search all stack log groups for a given filter pattern.

    Does **not** stop early — searches every group up to *max_log_groups*
    to ensure systemic patterns across multiple services are captured.

    Args:
        filter_pattern:         CloudWatch filter pattern string.
        hours_back:             Look-back window in hours.
        max_log_events:         Per-group event cap.
        max_log_groups:         Maximum log groups to search.
        log_message_max_length: Passed through to :func:`search_log_group`.
        ttl_seconds:            SSM settings cache TTL.

    Returns:
        Dict with ``all_results``, ``total_events``, ``log_groups_searched``,
        and ``groups_to_search`` (list of group names actually searched).
    """
    log_groups = get_stack_log_groups(ttl_seconds=ttl_seconds)
    groups_to_search = log_groups[:max_log_groups]
    logger.info(
        "Stack-wide search across %d log groups (max: %d): %s",
        len(groups_to_search),
        max_log_groups,
        [lg["name"] for lg in groups_to_search],
    )

    all_results: List[Dict[str, Any]] = []
    total_events = 0

    for log_group in groups_to_search:
        search_result = search_log_group(
            log_group_name=log_group["name"],
            filter_pattern=filter_pattern,
            hours_back=hours_back,
            max_events=max_log_events,
            log_message_max_length=log_message_max_length,
        )

        if search_result.get("events_found", 0) > 0:
            logger.info(
                "Found %d events in %s",
                search_result["events_found"],
                log_group["name"],
            )
            all_results.append(
                {
                    "log_group": log_group["name"],
                    "events_found": search_result["events_found"],
                    "events": search_result["events"],
                }
            )
            total_events += search_result["events_found"]

    logger.info("Stack-wide search completed: %d total events", total_events)
    return {
        "all_results": all_results,
        "total_events": total_events,
        "log_groups_searched": len(groups_to_search),
        "groups_to_search": [lg["name"] for lg in groups_to_search],
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_filter_pattern(base_pattern: str, request_id: str = "") -> str:
    """
    Build a CloudWatch filter pattern string.

    When a request ID is supplied we fall back to an ERROR pattern and
    apply the request ID filter in post-processing (``search_log_group``)
    because combining the two in a single CloudWatch pattern is unreliable.
    """
    if request_id:
        return base_pattern if base_pattern else "ERROR"
    return base_pattern or ""


def _should_exclude_log_event(message: str, filter_pattern: str = "") -> bool:
    """
    Return ``True`` for log events that are pure noise.

    Only discards well-known INFO / Lambda lifecycle prefixes when an error
    filter pattern is active.  Long messages are **not** discarded here —
    they are truncated in the caller (:func:`search_log_group`) to preserve
    root-cause details.
    """
    message_stripped = message.strip()

    if filter_pattern and message_stripped.startswith(
        (
            "[INFO]",
            "INIT_START",
            "START",
            "END",
            "REPORT",
            "Config:",
            "Debug:",
            "Trace:",
        )
    ):
        return True

    return any(
        pattern in message
        for pattern in ['"sample_json"', "Processing event:", "Initialized", "Starting"]
    )


def _truncate_message(message: str, max_length: int = 400) -> str:
    """
    Truncate *message* to *max_length* characters, appending ``"... [truncated]"``.

    Unlike the previous ``_should_exclude_log_event`` behaviour of silently
    skipping messages longer than 1000 characters, this function always
    preserves the first ``max_length`` characters so the LLM receives the
    actual error text.
    """
    if len(message) <= max_length:
        return message
    return message[:max_length] + "... [truncated]"


def _extract_function_type(lambda_function_name: str) -> str:
    """
    Extract function type from a Lambda function name by scanning hyphen-separated parts.

    Examples::

        DEV-P2-EA8-PATTERN2STACK-1H-ClassificationFunction-dSp68ELdR85C
            → "ClassificationFunction"
        DEV-P2-EA8-QueueProcessor-JweFNlBa4vkV
            → "QueueProcessor"
    """
    if not lambda_function_name:
        return ""
    for part in lambda_function_name.split("-"):
        if part.endswith(("Function", "Processor")) and len(part) > 8:
            return part
    return ""
