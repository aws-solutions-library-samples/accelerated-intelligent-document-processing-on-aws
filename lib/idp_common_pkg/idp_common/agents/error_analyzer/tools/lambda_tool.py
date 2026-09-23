# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda tools for document context extraction.
"""

import json
import logging
import os
from collections.abc import Hashable
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
from strands import tool

from ..config import create_error_response

logger = logging.getLogger(__name__)


@tool
def retrieve_document_context(document_id: str) -> Dict[str, Any]:
    """
    Retrieve comprehensive document processing context via Lambda lookup function.

    Invokes the lookup Lambda function to gather execution context, timing information,
    and Step Function details for a specific document. Provides essential data for
    targeted error analysis and log searching.

    Use this tool to:
    - Get complete document processing timeline and status
    - Extract Lambda request IDs for CloudWatch log correlation
    - Identify failed functions and execution context
    - Obtain precise processing time windows for analysis

    Alternative: If you only need basic document metadata (status, timestamps, execution ARN)
    without detailed execution events and Lambda request IDs, consider using fetch_document_record
    which provides faster access to DynamoDB tracking data.

    Example usage:
    - "Get processing context for report.pdf"
    - "Retrieve execution details for lending_package.pdf"
    - "Show me the processing timeline for document ABC123"
    - "Get Lambda request IDs for failed document processing"

    Args:
        document_id: Document ObjectKey to analyze (e.g., "report.pdf", "lending_package.pdf")

    Returns:
        Dict containing document context, execution details, and timing information
    """
    try:
        lambda_client = boto3.client("lambda")
        function_name = get_lookup_function_name()

        logger.info(
            f"Invoking lookup function: {function_name} for document: {document_id}"
        )

        # Invoke lookup function
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"object_key": document_id}),
        )

        # Parse response
        payload = json.loads(response["Payload"].read().decode("utf-8"))

        if payload.get("status") == "NOT_FOUND":
            return create_error_response(
                "Document not found in tracking database",
                document_found=False,
                document_id=document_id,
            )

        if payload.get("status") == "ERROR":
            return create_error_response(
                payload.get("message", "Unknown error from lookup function"),
                document_found=False,
                document_id=document_id,
            )

        # Extract execution context
        processing_detail = payload.get("processingDetail", {})
        execution_arn = processing_detail.get("executionArn")
        execution_events = processing_detail.get("events", [])

        # Extract Lambda request IDs and function mapping from execution events
        request_context = extract_lambda_request_ids(execution_events)
        request_ids = request_context.get("all_request_ids", [])
        function_request_map = request_context.get("function_request_map", {})
        failed_functions = request_context.get("failed_functions", [])
        primary_failed_function = request_context.get("primary_failed_function")

        # Get timestamps for precise time windows
        timestamps = payload.get("timing", {}).get("timestamps", {})

        # Calculate processing time window
        start_time = None
        end_time = None

        if timestamps.get("WorkflowStartTime"):
            start_time = datetime.fromisoformat(
                timestamps["WorkflowStartTime"].replace("Z", "+00:00")
            )

        if timestamps.get("CompletionTime"):
            end_time = datetime.fromisoformat(
                timestamps["CompletionTime"].replace("Z", "+00:00")
            )

        response = {
            "document_found": True,
            "document_id": document_id,
            "document_status": payload.get("status"),
            "execution_arn": execution_arn,
            "lambda_request_ids": request_ids,
            "function_request_map": function_request_map,
            "failed_functions": failed_functions,
            "primary_failed_function": primary_failed_function,
            "timestamps": timestamps,
            "processing_start_time": start_time,
            "processing_end_time": end_time,
            "execution_events_count": len(execution_events),
            "lookup_function_response": payload,
        }

        logger.info(f"Document context response for {document_id}: {response}")
        return response

    except Exception as e:
        logger.error(f"Error getting document context for {document_id}: {e}")
        return create_error_response(
            str(e), document_found=False, document_id=document_id
        )


def get_lookup_function_name() -> str:
    """
    Retrieve the Lambda lookup function name from environment configuration.
    Checks for LOOKUP_FUNCTION_NAME environment variable with fallback to
    AWS_STACK_NAME-based naming convention.

    Returns:
        Lambda function name string

    Raises:
        ValueError: If neither environment variable is configured
    """
    function_name = os.environ.get("LOOKUP_FUNCTION_NAME")
    if function_name:
        return function_name

    raise ValueError("LOOKUP_FUNCTION_NAME environment variable not set")


#: Event types whose detail carries a ``resource`` naming what was invoked, and the
#: detail key each uses. Measured against the Step Functions API model in the
#: installed botocore, not assumed: ``LambdaFunctionFailedEventDetails`` and
#: ``LambdaFunctionTimedOutEventDetails`` hold only ``cause`` and ``error``, and
#: ``StateEnteredEventDetails`` only ``input``, ``inputDetails`` and ``name``. The
#: failing function's identity simply is not in the failure event.
_RESOURCE_BEARING_EVENTS = {
    "LambdaFunctionScheduled": "lambdaFunctionScheduledEventDetails",
    "TaskScheduled": "taskScheduledEventDetails",
}

#: The events this parser reads, and the detail key each one carries its payload in.
#: One mapping rather than a membership list plus an `or` chain, so "absent key" and
#: "present but empty" stay distinguishable — see the note at the lookup.
_OUTCOME_DETAIL_KEYS = {
    "LambdaFunctionSucceeded": "lambdaFunctionSucceededEventDetails",
    "LambdaFunctionFailed": "lambdaFunctionFailedEventDetails",
    "LambdaFunctionTimedOut": "lambdaFunctionTimedOutEventDetails",
    # ⚠️ The *Failed-to-even-start family, which is what a THROTTLE or a permission
    # failure at invoke time produces -- precisely the errors the workflow's retry
    # ladders enumerate. Omitting these left `failed_functions` empty for that whole
    # class of failure, which is the one an operator is most likely to be looking at.
    # Their scheduling predecessor exists, so the causal walk resolves them.
    "LambdaFunctionScheduleFailed": "lambdaFunctionScheduleFailedEventDetails",
    "LambdaFunctionStartFailed": "lambdaFunctionStartFailedEventDetails",
    "TaskSucceeded": "taskSucceededEventDetails",
    "TaskFailed": "taskFailedEventDetails",
    "TaskTimedOut": "taskTimedOutEventDetails",
    "TaskStartFailed": "taskStartFailedEventDetails",
    "TaskSubmitFailed": "taskSubmitFailedEventDetails",
    "TaskStateEntered": "stateEnteredEventDetails",
    "TaskStateExited": "stateExitedEventDetails",
}

#: Events that carry a STATE's identity rather than an invocation's, and so must
#: never be resolved causally. See the note at the resolution site: such an event
#: precedes its own scheduling event, so a backwards walk can only find another
#: task's.
_STATE_TRANSITION_EVENTS = frozenset({"TaskStateEntered", "TaskStateExited"})

#: Failure events that should contribute a failed function name. Both families are
#: needed because this workflow uses **both** Lambda integration styles: 15 task
#: states name a function ARN directly (giving ``LambdaFunction*`` events) and 9 go
#: through ``arn:<partition>:states:::lambda:invoke`` (giving ``Task*`` events), so
#: reading either family alone leaves a third of the pipeline unattributable.
_FAILURE_EVENTS_WITH_A_FUNCTION = (
    "LambdaFunctionFailed",
    "LambdaFunctionTimedOut",
    "LambdaFunctionScheduleFailed",
    "LambdaFunctionStartFailed",
    "TaskFailed",
    "TaskTimedOut",
    "TaskStartFailed",
    "TaskSubmitFailed",
)

#: How far back along ``previousEventId`` to look for the scheduling event.
#:
#: The real chains are short: ``LambdaFunctionFailed -> LambdaFunctionStarted ->
#: LambdaFunctionScheduled`` is two hops, and ``TaskFailed -> TaskStarted ->
#: TaskScheduled`` is two. A retry re-schedules, so the walk lands on that attempt's
#: own scheduling rather than the first one. The bound exists so a failure whose
#: chain is broken stops at "unknown" instead of walking back through the whole
#: history and attributing itself to some unrelated earlier invocation — a wrong
#: function name is worse here than none, because it selects the log group the agent
#: goes on to search.
_MAX_CAUSAL_HOPS = 6


def _function_name_from_arn(resource: Optional[str]) -> Optional[str]:
    """The function name in a Lambda **function** ARN, or None.

    Requires the ``:function:`` marker rather than splitting positionally. A Lambda
    ARN that is not a function ARN — a layer version, an event-source mapping, a
    code-signing config — has a layer name or a uuid in that position, and for an
    optimized integration ``resource`` is the integration verb (``invoke``) rather
    than any ARN at all.
    """
    if resource and ":function:" in resource:
        return resource.split(":function:")[-1] or None
    return None


def _function_name_from_task_parameters(parameters: Any) -> Optional[str]:
    """The function an optimized ``lambda:invoke`` task was pointed at.

    For that integration the scheduling event's ``resource`` is ``invoke`` and the
    target is in the task's ``parameters`` under ``FunctionName``, which the caller
    may have given as a full ARN or as a bare name.
    """
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(parameters, dict):
        return None
    target = parameters.get("FunctionName")
    if not isinstance(target, str) or not target:
        return None
    # A bare name is accepted; anything containing a colon that is not a function
    # ARN is refused rather than guessed at. That refuses a QUALIFIED bare name —
    # `MyFunction:PROD` or `MyFunction:3`, a real function plus an alias or
    # version. Unreachable from this workflow, whose nine optimized states all pass
    # full ARNs, and refusing is the safe direction: the alternative is splitting on
    # a colon and hoping, which is the positional read #1128 removed.
    return _function_name_from_arn(target) or (target if ":" not in target else None)


def _index_invoked_functions(
    execution_events: List[Dict[str, Any]],
) -> Dict[Any, str]:
    """Map each scheduling event's ``id`` to the function name it names."""
    invoked: Dict[Any, str] = {}
    for event in execution_events:
        detail_key = _RESOURCE_BEARING_EVENTS.get(event.get("type", ""))
        if not detail_key:
            continue
        detail = event.get(detail_key) or {}
        if not isinstance(detail, dict):
            continue
        name = _function_name_from_arn(detail.get("resource"))
        if name is None:
            name = _function_name_from_task_parameters(detail.get("parameters"))
        if name is not None and event.get("id") is not None:
            invoked[event["id"]] = name
    return invoked


def _resolve_invoked_function(
    event: Dict[str, Any],
    events_by_id: Dict[Any, Dict[str, Any]],
    invoked_by_id: Dict[Any, str],
) -> Optional[str]:
    """Follow ``previousEventId`` back to this event's own scheduling event.

    Causal, not positional. Taking "the nearest preceding scheduling event" instead
    would misattribute inside a concurrent ``Map``: ``ProcessSections`` runs at
    ``MaxConcurrency`` 10 and the shard ``Map`` at 5, iterations share one history,
    so the event physically before a failure routinely belongs to a different
    iteration and a different function.
    """
    # No visited-set. The `for` bounds the walk by construction, so a cycle in
    # `previousEventId` terminates at the bound and returns None — which is the same
    # answer a visited-set gives, a few iterations later. A visited-set was here, and
    # removing it changed no test result in either direction: with the bound present
    # there is no input that distinguishes the two. Defensive code no test can tell
    # apart from its own absence is code nobody can maintain or safely change, so it
    # is gone rather than kept with a test that only appears to cover it.
    current: Optional[Dict[str, Any]] = event
    for _ in range(_MAX_CAUSAL_HOPS + 1):
        if current is None:
            return None
        current_id = current.get("id")
        if current_id in invoked_by_id:
            return invoked_by_id[current_id]
        previous_id = current.get("previousEventId")
        if previous_id in (None, 0):
            return None
        current = events_by_id.get(previous_id)
    return None


def extract_lambda_request_ids(
    execution_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Extract Lambda request IDs from Step Functions execution event history with function mapping.
    Enhanced to extract request IDs from multiple event fields and map them to specific Lambda functions.

    Args:
        execution_events: List of Step Function execution events

    Returns:
        Dict containing request IDs mapped to functions and failure information
    """
    function_request_map = {}
    failed_functions = []
    all_request_ids = []

    # Built once. The function's identity lives in the SCHEDULING event, and a
    # failure reaches it by following `previousEventId` -- see
    # `_resolve_invoked_function`. Reading `resource` off the failure event itself,
    # which is what this did, could never work: no Lambda failure detail carries
    # that field, so `failed_functions` was always empty and
    # `primary_failed_function` always None in production (#1171).
    invoked_by_id = _index_invoked_functions(execution_events)
    # `Hashable` rather than only `is not None`: an unhashable `id` would raise while
    # building this dict, and the caller's broad `except` turns any raise here into
    # `document_found: False`, discarding every request id and failed-function name
    # already gathered. Unreachable from a real history, where `id` is an integer — but
    # the cost of being wrong is total, and the guard is one predicate.
    events_by_id = {
        event["id"]: event
        for event in execution_events
        if event.get("id") is not None and isinstance(event.get("id"), Hashable)
    }

    for event in execution_events:
        event_type = event.get("type", "")

        # Extract function name from various event types
        function_name = None
        request_id = None

        if event_type in _OUTCOME_DETAIL_KEYS:
            # Looked up by the event's own type rather than selected by an `or`
            # chain over every possible key. The chain could not distinguish "this
            # key is absent" from "this key is present and empty", because both are
            # falsy: an event whose detail is `{}` fell through every branch to the
            # final `.get` and arrived as None, so the event was discarded and its
            # failure lost. A `LambdaFunctionFailed` detail holds only `cause` and
            # `error`, either of which a service can omit.
            event_detail = event.get(_OUTCOME_DETAIL_KEYS[event_type])

            if event_detail is not None:
                if event_type in _STATE_TRANSITION_EVENTS:
                    # ⚠️ A state-transition event is NEVER resolved causally, and the
                    # reason is structural rather than a tuning choice: a
                    # `TaskStateEntered` event *precedes* its own scheduling event, so
                    # walking backwards from it can only ever find somebody else's.
                    # In a linear history the service points a state's
                    # `TaskStateEntered` at the previous state's `TaskStateExited`, so
                    # the walk leaves the state entirely and lands on the PREVIOUS
                    # task's `LambdaFunctionScheduled`. Measured: a request id carried
                    # in `ClassificationStep`'s `stateEnteredEventDetails.input` was
                    # keyed under `OCRFunction`, overwriting OCRFunction's own correct
                    # id — worse than keying it under the state name, which is what
                    # this did before.
                    #
                    # The state name is the only identity such an event carries, and
                    # it is the right key for a request id found in the state's input
                    # or output.
                    function_name = event_detail.get("name") or None
                else:
                    # An outcome event follows its own scheduling event, so the walk
                    # runs in the direction where the answer exists.
                    function_name = _resolve_invoked_function(
                        event, events_by_id, invoked_by_id
                    )

                # Extract request ID from multiple fields
                for field_name, field_value in event_detail.items():
                    if field_value:
                        request_id = _extract_request_id_from_json(str(field_value))
                        if not request_id:
                            request_id = _extract_request_id_from_string(
                                str(field_value)
                            )
                        if request_id:
                            break

                # Track failed functions
                if event_type in _FAILURE_EVENTS_WITH_A_FUNCTION and function_name:
                    failed_functions.append(function_name)

                # Map function to request ID
                if function_name and request_id:
                    function_request_map[function_name] = request_id
                    all_request_ids.append(request_id)

        # Also check top-level event fields for request IDs
        if not request_id:
            for field_name, field_value in event.items():
                if (
                    field_name not in ["type", "timestamp", "id", "previousEventId"]
                    and field_value
                ):
                    request_id = _extract_request_id_from_string(str(field_value))
                    if request_id and function_name:
                        function_request_map[function_name] = request_id
                        all_request_ids.append(request_id)
                        break

    result = {
        "function_request_map": function_request_map,
        "failed_functions": list(set(failed_functions)),
        "all_request_ids": list(set(all_request_ids)),
        "primary_failed_function": failed_functions[0] if failed_functions else None,
    }

    if not all_request_ids:
        logger.info("No request ids extracted from step functions events")
    return result


def _extract_request_id_from_json(json_string: str) -> Optional[str]:
    """
    Extract request ID from JSON string in various formats.

    Args:
        json_string: JSON string that may contain request ID

    Returns:
        Request ID string if found, None otherwise
    """
    if not json_string:
        return None

    try:
        data = json.loads(json_string)
        # Check common request ID field names
        for field in [
            "requestId",
            "request_id",
            "RequestId",
            "awsRequestId",
            "lambdaRequestId",
        ]:
            if field in data and data[field]:
                return str(data[field])
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _extract_request_id_from_string(text: str) -> Optional[str]:
    """
    Extract Lambda request ID from string using UUID pattern matching.

    Args:
        text: String that may contain a UUID request ID

    Returns:
        Request ID string if found, None otherwise
    """
    import re

    if not text:
        return None

    # Pattern for UUID
    uuid_pattern = r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"

    matches = re.findall(uuid_pattern, text, re.IGNORECASE)

    if matches:
        return matches[0]

    return None
