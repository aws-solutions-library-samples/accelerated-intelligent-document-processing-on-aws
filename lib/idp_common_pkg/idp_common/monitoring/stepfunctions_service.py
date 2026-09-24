# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Step Functions service for IDP execution analysis.

Provides reusable functions to retrieve and analyse the processing history
of a document through AWS Step Functions, including a timeline of every step
the document went through and where it failed.

This module is a pure library — it contains no ``@tool`` decorators and makes
no assumptions about agent frameworks.  Agent tool wrappers that call these
functions live in ``agents/error_analyzer/tools/stepfunction_tool.py``.

Usage::

    from idp_common.monitoring.stepfunctions_service import (
        get_execution_arn_from_document,
        get_execution_data,
        analyze_execution_timeline,
        extract_failure_details,
    )

    arn = get_execution_arn_from_document(document_record)
    data = get_execution_data(arn)
    timeline = analyze_execution_timeline(arn)
    failure = extract_failure_details(data["events"])
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

from idp_common.stepfunctions_history import (
    EXECUTION_LEVEL_FAILURE_EVENTS,
    STATE_ENTERED_SUFFIX,
    TASK_LEVEL_FAILURE_EVENTS,
    failing_state,
    state_named_by,
    terminal_failure,
    to_chronological,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy boto3 client cache, keyed by region so that callers passing
# an explicit region always get the correct client, even if a default-region
# client was already initialised first.
# ---------------------------------------------------------------------------
_sf_clients: Dict[Optional[str], Any] = {}


def _get_sf_client(region: Optional[str] = None) -> Any:
    """Return (and lazily create) a per-region Step Functions boto3 client."""
    if region not in _sf_clients:
        _sf_clients[region] = boto3.client("stepfunctions", region_name=region)
    return _sf_clients[region]


# The failure vocabulary and the rule for reading it are shared — see
# `idp_common.stepfunctions_history`. This module used to carry its own copy of both: a
# six-type event set and a hand-written map from each type to its detail key. That set was
# the widest of the four copies in the repository and still left eight of the thirteen
# task-level types unrecognised; the timeline loop below matched only three, so nine
# members of the vocabulary were recognised by neither rule in this file. The two rules
# also disagreed with each other for the same execution.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_execution_arn_from_document(
    doc_record: Any,
) -> str:
    """
    Extract the Step Functions execution ARN from a DynamoDB document record.

    Accepts either a plain ``dict`` (raw DynamoDB item) or a
    :class:`~idp_common.monitoring.models.DocumentRecord` dataclass.

    Args:
        doc_record: DynamoDB document record dict or ``DocumentRecord``.

    Returns:
        Execution ARN string, or ``""`` if not available.
    """
    if isinstance(doc_record, dict):
        return (
            doc_record.get("WorkflowExecutionArn", "")
            or doc_record.get("ExecutionArn", "")
            or doc_record.get("workflow_execution_arn", "")
            or ""
        )
    # Handle DocumentRecord dataclass (or any object with the attribute)
    return (
        getattr(doc_record, "workflow_execution_arn", "")
        or getattr(doc_record, "WorkflowExecutionArn", "")
        or ""
    )


def get_execution_data(
    execution_arn: str,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch full Step Functions execution history for a given execution ARN.

    Paginates through the ``get_execution_history`` API to retrieve all events.

    Args:
        execution_arn: Full ARN of the Step Functions execution.
        region:        AWS region name.  Defaults to the region inferred by
                       boto3 from the environment.

    Returns:
        ``{
            "execution_arn": str,
            "status": str,      # SUCCEEDED | FAILED | RUNNING | ABORTED | TIMED_OUT
            "start_date": str,  # ISO 8601 or ""
            "stop_date": str,   # ISO 8601 or ""
            "events": list,     # Raw execution history events (timestamps serialised)
            "input": str,       # Execution input JSON string
        }``
    """
    sf = _get_sf_client(region)
    result: Dict[str, Any] = {
        "execution_arn": execution_arn,
        "status": "UNKNOWN",
        "start_date": "",
        "stop_date": "",
        "events": [],
        "input": "",
    }

    try:
        desc = sf.describe_execution(executionArn=execution_arn)
        result["status"] = desc.get("status", "UNKNOWN")

        start = desc.get("startDate")
        stop = desc.get("stopDate")
        result["start_date"] = _serialize_datetime(start)
        result["stop_date"] = _serialize_datetime(stop)
        result["input"] = desc.get("input", "")

        # Paginate execution history
        paginator = sf.get_paginator("get_execution_history")
        events: List[Dict[str, Any]] = []
        for page in paginator.paginate(
            executionArn=execution_arn,
            includeExecutionData=True,
        ):
            events.extend(page.get("events", []))

        # Serialise all timestamp fields so the result is JSON-safe
        for event in events:
            ts = event.get("timestamp")
            if ts is not None and not isinstance(ts, str):
                event["timestamp"] = _serialize_datetime(ts)

        result["events"] = events

    except sf.exceptions.ExecutionDoesNotExist:
        # Use a distinct status so callers can tell "not found" from a
        # transient API error (which leaves status as "UNKNOWN").
        logger.warning("Execution not found: %s", execution_arn)
        result["status"] = "NOT_FOUND"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get execution data for %s: %s", execution_arn, exc)

    return result


def analyze_execution_timeline(
    execution_arn: str,
    max_events: int = 200,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyse a Step Functions execution and produce a structured timeline.

    Returns the sequence of states with their durations, identifying which
    state failed and extracting the error details.

    Args:
        execution_arn: Full ARN of the Step Functions execution.
        max_events:    Maximum history events to process (default: 200).
        region:        AWS region name.

    Returns:
        ``{
            "execution_arn": str,
            "overall_status": str,
            "total_duration_ms": float,
            "states": [
                {
                    "name": str,
                    "status": str,       # SUCCEEDED | FAILED
                    "start_time": str,
                    "end_time": str,
                    "duration_ms": float,
                    "is_failure": bool,
                }
            ],
            "failed_state": str,
            "failure_details": dict,
        }``

    ``max_events`` bounds the ``states`` timeline only, and it drops the **oldest**
    events. Attribution — ``failed_state`` and ``failure_details`` — reads the whole
    history however long it is, because a cap taken from the other end silently answers
    "nothing failed" for exactly the long executions most worth asking about. The cost of
    the cap is that a state entered before the window shows a zero **duration**, which is
    visible in the row rather than being a wrong answer about the failure. The row's
    **name** is not a cost: the state tracker is seeded from the dropped events, so a
    failure row inside the window names the same state ``failed_state`` does however hard
    the cap bites.

    ⚠️ **This names the failure the history holds; it does not decide whether the
    execution failed.** ``overall_status`` is the authority on that, and the two can
    legitimately disagree: a ``Retry`` that recovered leaves a real ``TaskFailed`` behind
    on an execution that went on to succeed, so a ``SUCCEEDED`` execution can carry a
    ``failed_state``. Check ``overall_status`` first if that distinction matters to the
    caller.
    """
    exec_data = get_execution_data(execution_arn, region=region)
    # Order before doing anything else: every rule below reads the history in sequence,
    # and `get_execution_data` paginates forward today but that is its choice, not this
    # function's guarantee.
    all_events: List[Dict[str, Any]] = to_chronological(exec_data.get("events", []))
    if max_events <= 0:
        logger.warning(
            "Execution %s: max_events=%d returns no states timeline at all. Failure "
            "attribution still reads the whole history.",
            execution_arn,
            max_events,
        )
    elif len(all_events) > max_events:
        logger.warning(
            "Execution %s has %d events; the returned states timeline covers the most "
            "recent %d. Failure attribution reads all of them.",
            execution_arn,
            len(all_events),
            max_events,
        )
    # Keep the NEWEST events, which is where a terminal failure is. `[-max_events:]` is
    # the whole list when `max_events` is 0, so the degenerate cap is spelled out.
    events = all_events[-max_events:] if max_events > 0 else []

    timeline: Dict[str, Any] = {
        "execution_arn": execution_arn,
        "overall_status": exec_data.get("status", "UNKNOWN"),
        "total_duration_ms": 0.0,
        "states": [],
        "failed_state": "",
        "failure_details": {},
    }

    state_starts: Dict[str, str] = {}  # state_name → start_timestamp
    states: List[Dict[str, Any]] = []
    # The most recently ENTERED state, tracked in event order. `state_starts` cannot
    # answer this: it is keyed by state name, and re-assigning an existing key leaves it
    # where it was first inserted, so `list(state_starts)[-1]` is the state entered
    # longest ago among those still to be re-entered. A loop or a `Map` that re-enters a
    # state therefore made the old fallback name a different state entirely — a `Retry`
    # does not trigger it, because a retried task does not re-enter its state.
    #
    # ⚠️ Seeded from the events BEFORE the window, not from empty. The window can begin
    # part-way through the history, and a failure inside it is then attributable to a state
    # entered outside it — labelling the row from an empty tracker produced "Unknown" while
    # `failed_state` named the state correctly, which is a third reading of one history in
    # the very function whose two readings this unified.
    last_entered: str = ""
    # The state the last task-level failure is attributable to. An execution-level event
    # must not be attributed to `last_entered`: by the time it arrives a caught failure
    # has already entered its handler, which is #1139/#1168.
    last_task_failure_state: str = ""

    dropped = all_events[: len(all_events) - len(events)] if events else all_events
    for event in dropped:
        event_type = event.get("type", "")
        if event_type.endswith(STATE_ENTERED_SUFFIX):
            entered = event.get("stateEnteredEventDetails", {}).get("name", "")
            if entered:
                last_entered = entered
        elif event_type in TASK_LEVEL_FAILURE_EVENTS:
            last_task_failure_state = state_named_by(event) or last_entered or "Unknown"

    for event in events:
        event_type: str = event.get("type", "")
        timestamp: str = event.get("timestamp", "")

        # Every state-transition event type shares the `StateEntered` suffix, so this
        # tracks entry into a `Map`, `Parallel` or `Choice` state as well as a `Task`.
        # Only `Task` states are timed below; attribution needs all of them.
        if event_type.endswith(STATE_ENTERED_SUFFIX):
            entered = event.get("stateEnteredEventDetails", {}).get("name", "")
            if entered:
                last_entered = entered
            if event_type == "TaskStateEntered" and entered:
                state_starts[entered] = timestamp
            continue

        # --- State-completion and failure events that produce a timeline entry ---
        if event_type == "TaskStateExited":
            state_name = event.get("stateExitedEventDetails", {}).get("name", "")
            is_failure = False
            status = "SUCCEEDED"

        elif event_type in TASK_LEVEL_FAILURE_EVENTS:
            # Attributed to the state most recently entered — unless the event names its
            # own state, which only `EvaluationFailed` does, and then that is the
            # authority. Labelling the ROW from `last_entered` alone is what made this
            # function's rows disagree with its own `failed_state` for such an event.
            state_name = state_named_by(event) or last_entered or "Unknown"
            last_task_failure_state = state_name
            is_failure = True
            status = "FAILED"

        elif event_type in EXECUTION_LEVEL_FAILURE_EVENTS:
            # Prefer the state a task-level failure already named. Falling straight back
            # to `last_entered` is what names a `Catch` handler.
            state_name = last_task_failure_state or last_entered or "Unknown"
            is_failure = True
            status = "FAILED"

        else:
            # Ignore all other event types (ExecutionStarted, LambdaScheduled, etc.)
            continue

        start = state_starts.get(state_name, "")
        duration_ms = _compute_duration_ms(start, timestamp)

        states.append(
            {
                "name": state_name,
                "status": status,
                "start_time": start,
                "end_time": timestamp,
                "duration_ms": round(duration_ms, 1),
                "is_failure": is_failure,
            }
        )

    timeline["states"] = states
    # Both answers come from the shared rule, read over the WHOLE history rather than the
    # capped window, so they agree with each other and name the failure the execution
    # ended on. Taking the first failure instead — which is what `if is_failure and not
    # timeline["failed_state"]` did here — names the state of a retry the workflow
    # survived, and an error nobody needs to act on.
    timeline["failed_state"] = failing_state(all_events) or ""
    timeline["failure_details"] = extract_failure_details(all_events)

    # Total execution duration
    start_date = exec_data.get("start_date", "")
    stop_date = exec_data.get("stop_date", "")
    if start_date and stop_date:
        timeline["total_duration_ms"] = round(
            _compute_duration_ms(start_date, stop_date), 1
        )

    return timeline


def extract_failure_details(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Parse Step Functions execution history events to extract failure information.

    Recognises every failure event type the Step Functions service model declares —
    fifteen of them — rather than a hand-kept subset; see
    :mod:`idp_common.stepfunctions_history` for what qualifies and why. ``events`` may
    arrive in either direction and is ordered here.

    Args:
        events: List of raw execution history event dicts (timestamps may be
                strings or ``datetime`` objects).

    Returns:
        ``{
            "error": str,        # Error type (e.g. "ThrottlingException")
            "cause": str,        # Error cause message (may be a JSON string)
            "failed_state": str, # Name of the state that failed
            "event_type": str,   # The Step Functions event type
        }``
        All fields are empty strings if no failure event is found.

    The state and the error text come from **different events** — the last task-level
    failure and the terminal one respectively — which is why this delegates rather than
    reading both off whichever event it stopped at.
    """
    failure = terminal_failure(events)
    if failure is None:
        return {"error": "", "cause": "", "failed_state": "", "event_type": ""}

    return {
        "error": failure["error"],
        "cause": failure["cause"],
        "failed_state": failure["state"],
        "event_type": failure["event_type"],
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _serialize_datetime(dt: Any) -> str:
    """Convert a datetime object (or None) to an ISO 8601 string."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _compute_duration_ms(start: str, end: str) -> float:
    """
    Compute the duration in milliseconds between two ISO 8601 timestamp strings.

    Returns 0.0 if either string is empty or unparseable.
    """
    if not start or not end:
        return 0.0
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (t1 - t0).total_seconds() * 1000
    except (ValueError, TypeError) as exc:
        logger.debug(
            "Could not compute duration between '%s' and '%s': %s", start, end, exc
        )
        return 0.0
