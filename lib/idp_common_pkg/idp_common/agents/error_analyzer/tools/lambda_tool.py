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
from typing import Any, Dict, List, NamedTuple, Optional

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
        Dict containing document context, execution details, and timing information.

        Two pairs of keys name the failure, and they are **not** interchangeable:

        - ``failed_functions`` / ``primary_failed_function`` are Lambda **function**
          names. A function name is what a log group is named after, so these are the
          values to use when choosing which log group to search for a request id.
        - ``failed_states`` / ``primary_failed_state`` are Step Functions **state**
          names, and they carry the failures whose history holds no function ARN
          anywhere to name. An invoke-time permission denial is the common case: Step
          Functions emits a schedule *failure* instead of the scheduling event, so the
          function it would have invoked never appears in the history at all. A state
          name identifies the pipeline step that could not invoke — which is usually
          enough for an operator to find the function — but it is **not** a function
          name and **not** a log group name. Map it to the step's function through the
          workflow definition (``analyze_workflow_execution`` reports the same state
          names) before searching logs, rather than searching for the state name.

        A single failed attempt contributes to one pair or the other, never both.
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
        failed_states = request_context.get("failed_states", [])
        primary_failed_state = request_context.get("primary_failed_state")

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
            "failed_states": failed_states,
            "primary_failed_state": primary_failed_state,
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


#: Event types whose detail carries a ``resource`` naming the **Lambda function** that
#: was to be invoked, and the detail key each uses. Measured against the Step Functions
#: API model in the installed botocore, not assumed: ``LambdaFunctionFailedEventDetails``
#: and ``LambdaFunctionTimedOutEventDetails`` hold only ``cause`` and ``error``, and
#: ``StateEnteredEventDetails`` only ``input``, ``inputDetails`` and ``name``. The
#: failing function's identity simply is not in the failure event.
#:
#: ``ActivityScheduled`` is deliberately absent even though it declares a required
#: ``resource``: that resource is an activity ARN, which is never a function name. So
#: the walk below passes through one rather than stopping at it, and an activity failure
#: is answered with its state's name — the only identity such a history holds.
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
    "LambdaFunctionScheduleFailed": "lambdaFunctionScheduleFailedEventDetails",
    "LambdaFunctionStartFailed": "lambdaFunctionStartFailedEventDetails",
    "TaskSucceeded": "taskSucceededEventDetails",
    "TaskFailed": "taskFailedEventDetails",
    "TaskTimedOut": "taskTimedOutEventDetails",
    "TaskStartFailed": "taskStartFailedEventDetails",
    "TaskSubmitFailed": "taskSubmitFailedEventDetails",
    # The Activity family. This solution declares no Step Functions Activity -- every
    # `Resource` in `patterns/unified/statemachine/workflow.asl.json` is either a Lambda
    # function ARN or `states:::lambda:invoke[.waitForTaskToken]`, and the file contains
    # no `states:::activity` at all -- so these are read for closure over the event
    # vocabulary rather than for a case in this tree. They are the third way a task
    # attempt can be reported as failed, the Activity family is the only other one the
    # type enumeration holds a `*ScheduleFailed` for, and an activity's history carries
    # no function ARN anywhere, so the state fallback below is the only identity one can
    # ever be given -- for *every* activity failure, not only a schedule failure, since
    # `ActivityScheduled` is transparent to the walk. `ActivitySucceeded` is deliberately
    # not here: the closure being completed is over the ways an attempt can FAIL, and a
    # success is read only to harvest a request id, which an activity does not have.
    "ActivityFailed": "activityFailedEventDetails",
    "ActivityScheduleFailed": "activityScheduleFailedEventDetails",
    "ActivityTimedOut": "activityTimedOutEventDetails",
    "TaskStateEntered": "stateEnteredEventDetails",
    "TaskStateExited": "stateExitedEventDetails",
}

#: Detail keys that carry a STATE's name rather than an invocation's.
#:
#: Matched on the **detail key**, never on a list of event-type spellings. The history
#: API prefixes every transition with the state's own type -- ``TaskStateEntered``,
#: ``ChoiceStateEntered``, ``MapStateExited`` and eleven more -- while the service model
#: declares exactly one detail member per direction covering all of them, so asking
#: which detail key an event carries answers "does this event name a state" for every
#: spelling in the enumeration and for any the service adds later. A rule written as a
#: set of spellings is one the next spelling walks straight past.
_STATE_ENTERED_DETAIL_KEY = "stateEnteredEventDetails"
_STATE_EXITED_DETAIL_KEY = "stateExitedEventDetails"
_STATE_NAMING_DETAIL_KEYS = frozenset(
    {_STATE_ENTERED_DETAIL_KEY, _STATE_EXITED_DETAIL_KEY}
)

#: Every way one **task attempt** can be reported as having failed.
#:
#: Derived from the ``HistoryEventType`` enumeration in the Step Functions service model
#: rather than written from memory. The rule, which
#: ``TestTheFailureVocabularyIsDerivedFromTheServiceModel`` recomputes and enforces: a
#: type belongs here when its detail shape declares both ``error`` and ``cause`` *and*
#: its family can be **scheduled** -- that is, some ``<Family>Scheduled`` type exists in
#: the enumeration. That second clause is what excludes the four error-bearing types
#: that are not one attempt at invoking something: ``ExecutionFailed``,
#: ``ExecutionAborted`` and ``ExecutionTimedOut`` (by the time one of those arrives a
#: caught failure has already entered its handler, so attributing them names the
#: handler, which is #1139), ``MapRunFailed`` (an aggregate over a distributed Map run)
#: and ``EvaluationFailed`` (an expression evaluation, whose detail carries its own
#: required ``state`` member and so needs none of the machinery here).
#:
#: All three families are needed. This workflow uses **both** Lambda integration styles
#: -- 15 task states name a function ARN directly (giving ``LambdaFunction*`` events)
#: and 9 go through ``arn:<partition>:states:::lambda:invoke``, one of them
#: ``.waitForTaskToken`` (giving ``Task*`` events, which is what makes
#: ``TaskSubmitFailed`` reachable) -- and the Activity family completes the closure.
_TASK_ATTEMPT_FAILURE_EVENTS = frozenset(
    {
        "LambdaFunctionFailed",
        "LambdaFunctionTimedOut",
        "LambdaFunctionScheduleFailed",
        "LambdaFunctionStartFailed",
        "TaskFailed",
        "TaskTimedOut",
        "TaskStartFailed",
        "TaskSubmitFailed",
        "ActivityFailed",
        "ActivityScheduleFailed",
        "ActivityTimedOut",
    }
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
#:
#: The same bound governs the walk to the enclosing state's entry, which for a schedule
#: failure is one hop. A retried failure is further back -- each attempt adds a link and
#: no new state transition, so this workflow's deepest ladder (``MaxAttempts`` 8) puts
#: the last attempt outside the bound. That costs nothing: the *first* attempt is one
#: hop from the state entry and resolves, and both ``failed_states`` and
#: ``primary_failed_state`` are answered from it.
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


class _Attribution(NamedTuple):
    """What a history can be made to say about one failed attempt.

    At most one of the two is ever set, and they are **different kinds of identity**
    rather than a confident and a hedged spelling of the same one:

    * ``function`` is a Lambda function name, read from the scheduling event the
      attempt's causal chain leads back to. It names something that has a log group.
    * ``state`` is a Step Functions state name, set only when that chain reaches no
      scheduling event **that names a function** -- see :func:`_attribute_attempt` for
      the two ways that happens. It names a step in the workflow definition, which is
      neither a function name nor a log group name.

    Keeping them apart in the return value is what lets ``failed_functions`` and
    ``failed_states`` be separate fields, so a state name reached this way is never
    reported as the function that failed.
    """

    function: Optional[str] = None
    state: Optional[str] = None


def _state_named_by(event: Dict[str, Any]) -> Optional[str]:
    """The state name a state-**entered** event carries, or None.

    ``name`` is a required member of ``StateEnteredEventDetails``, so a real page always
    has one.

    An *empty* name comes back as the empty string rather than as None, and the one
    caller rejects it on truthiness. A guard here as well was measured redundant -- no
    test result changed in either direction with it present or absent -- and this file's
    own history is the reason not to keep it anyway: the removed visited-set was exactly
    that shape, defensive code no test could tell apart from its own absence.
    """
    detail = event.get(_STATE_ENTERED_DETAIL_KEY)
    if not isinstance(detail, dict):
        return None
    name = detail.get("name")
    return name if isinstance(name, str) else None


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


def _attribute_attempt(
    event: Dict[str, Any],
    events_by_id: Dict[Any, Dict[str, Any]],
    invoked_by_id: Dict[Any, str],
) -> _Attribution:
    """Follow ``previousEventId`` back to whatever identifies this attempt.

    Causal, not positional. Taking "the nearest preceding scheduling event" instead
    would misattribute inside a concurrent ``Map``: ``ProcessSections`` runs at
    ``MaxConcurrency`` 10 and the shard ``Map`` at 5, iterations share one history,
    so the event physically before a failure routinely belongs to a different
    iteration and a different function.

    The walk stops at the **first** thing that identifies something, and which of the
    four it reaches decides the answer:

    * a scheduling event that named a function -- that function (#1171);
    * a **function-naming** scheduling event (one whose type is in
      :data:`_RESOURCE_BEARING_EVENTS`) that named nothing this module will read --
      **nothing**. The history answered badly rather than not at all, and guessing past
      it is how a layer name or an integration verb comes to be reported as the function
      that failed (#1128). It is also what keeps the state fallback below from becoming a
      general one: a service-integration task that is not a Lambda invoke stops here;
    * the enclosing state's entry -- that state's name. Reaching it means the chain named
      no function, in one of exactly two ways: the scheduling event was never emitted
      (``LambdaFunctionScheduleFailed`` is emitted *instead of*
      ``LambdaFunctionScheduled``, which is what a ``lambda:InvokeFunction`` denial
      produces, so no function ARN appears anywhere in that history), or the family's
      scheduling event never names a function in the first place and is therefore
      transparent to this walk, which is the Activity family (#1183);
    * a state **exit** -- nothing. The walk has left the state this attempt belonged to,
      so every state name from there back belongs to an earlier one, and naming an
      earlier state is the cross-attribution #1171 and #1139 were both about. It applies
      to a chain that reaches an exit *before* any entry, which is what
      ``test_a_state_that_has_already_exited_is_never_named`` builds; on an ordinary
      mid-pipeline history the entry is examined first, so removing this branch changes
      that answer not at all -- measured.

    ⚠️ **The two state branches removed a wrong answer, not a missing one.** Before them,
    a schedule failure anywhere but the first state walked entry -> exit -> succeeded ->
    started -> scheduled -- five hops, inside the bound with one to spare -- and reported
    the **previous, successful** function: measured, ``['OCRFunction']`` for a denial on
    ``ClassificationStep``. Either branch alone suppresses that, since both terminate the
    walk before it gets there; only the entry branch also says which step it was. See
    ``test_a_denial_mid_pipeline_no_longer_names_the_previous_function``, which measures
    all four combinations.
    """
    # No visited-set. The `for` bounds the walk by construction, so a cycle in
    # `previousEventId` terminates at the bound and identifies nothing — which is the
    # same answer a visited-set gives, a few iterations later. A visited-set was here, and
    # removing it changed no test result in either direction: with the bound present
    # there is no input that distinguishes the two. Defensive code no test can tell
    # apart from its own absence is code nobody can maintain or safely change, so it
    # is gone rather than kept with a test that only appears to cover it.
    current: Optional[Dict[str, Any]] = event
    for _ in range(_MAX_CAUSAL_HOPS + 1):
        if current is None:
            return _Attribution()
        current_id = current.get("id")
        if current_id in invoked_by_id:
            return _Attribution(function=invoked_by_id[current_id])
        if current.get("type", "") in _RESOURCE_BEARING_EVENTS:
            return _Attribution()
        if _STATE_ENTERED_DETAIL_KEY in current:
            return _Attribution(state=_state_named_by(current))
        if _STATE_EXITED_DETAIL_KEY in current:
            return _Attribution()
        previous_id = current.get("previousEventId")
        if previous_id in (None, 0):
            return _Attribution()
        current = events_by_id.get(previous_id)
    return _Attribution()


def extract_lambda_request_ids(
    execution_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Extract Lambda request IDs from Step Functions execution event history with function mapping.
    Enhanced to extract request IDs from multiple event fields and map them to specific Lambda functions.

    Args:
        execution_events: List of Step Function execution events

    Returns:
        Dict containing request IDs mapped to functions and failure information.
        ``failed_functions``/``primary_failed_function`` name Lambda **functions**;
        ``failed_states``/``primary_failed_state`` name Step Functions **states** and
        carry the failures whose history holds no function ARN to name -- see
        :func:`_attribute_attempt`. The two are disjoint: one failed attempt contributes
        to one pair or the other, never both.
    """
    function_request_map = {}
    failed_functions = []
    failed_states = []
    all_request_ids = []

    # Built once. The function's identity lives in the SCHEDULING event, and a
    # failure reaches it by following `previousEventId` -- see
    # `_attribute_attempt`. Reading `resource` off the failure event itself,
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
        state_name = None
        request_id = None

        if event_type in _OUTCOME_DETAIL_KEYS:
            # Looked up by the event's own type rather than selected by an `or`
            # chain over every possible key. The chain could not distinguish "this
            # key is absent" from "this key is present and empty", because both are
            # falsy: an event whose detail is `{}` fell through every branch to the
            # final `.get` and arrived as None, so the event was discarded and its
            # failure lost. A `LambdaFunctionFailed` detail holds only `cause` and
            # `error`, either of which a service can omit.
            detail_key = _OUTCOME_DETAIL_KEYS[event_type]
            event_detail = event.get(detail_key)

            # `isinstance(..., dict)` rather than `is not None`, for the same reason
            # `_index_invoked_functions` checks it: both the `.get("name")` and the
            # `.items()` below raise on anything else, and a raise anywhere in this loop
            # leaves `extract_lambda_request_ids` altogether -- reaching
            # `retrieve_document_context`'s broad `except` as `document_found: False`,
            # which discards every request id and failed identity already gathered rather
            # than just the one malformed event. Measured while adding the
            # `stateEnteredEventDetails` cases below: a detail of `"OCRStep"` instead of
            # `{"name": "OCRStep"}` raised `AttributeError` here.
            if isinstance(event_detail, dict):
                if detail_key in _STATE_NAMING_DETAIL_KEYS:
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
                    #
                    # The condition is on the *detail key* rather than on a list of
                    # event types, so a `ChoiceStateEntered` or `MapStateExited` added
                    # to the map above is covered without being named again here.
                    function_name = event_detail.get("name") or None
                else:
                    # An outcome event follows its own scheduling event, so the walk
                    # runs in the direction where the answer exists.
                    attribution = _attribute_attempt(event, events_by_id, invoked_by_id)
                    function_name = attribution.function
                    state_name = attribution.state

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

                # Track the failure under whichever identity the history could give
                # it. `elif`, not a second `if`: one attempt is one entry, and an
                # attempt that named its function has no business also being reported
                # as an unattributed state.
                if event_type in _TASK_ATTEMPT_FAILURE_EVENTS:
                    if function_name:
                        failed_functions.append(function_name)
                    elif state_name:
                        failed_states.append(state_name)

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
        # `dict.fromkeys` rather than `set`, so the order is the history's. The line
        # above keeps `set` deliberately: it is an existing output, and re-ordering it
        # would change what an operator sees on every document that fails today, for no
        # defect. A new field can simply be right from the start.
        "failed_states": list(dict.fromkeys(failed_states)),
        "all_request_ids": list(set(all_request_ids)),
        "primary_failed_function": failed_functions[0] if failed_functions else None,
        "primary_failed_state": failed_states[0] if failed_states else None,
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
