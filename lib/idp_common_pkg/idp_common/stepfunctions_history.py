# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One reading of a Step Functions execution history: which state failed.

Four very different readers need this answer — the error-analyzer agent tool that
explains a failed document to an operator
(``idp_common.agents.error_analyzer.tools.stepfunction_tool``), the CodeBuild
deployment harness that snapshots a failed execution before it tears the stack down
(``scripts/sdlc/codebuild_deployment.py``), the monitoring timeline
(``idp_common.monitoring.stepfunctions_service``) and the web UI's execution viewer
(``nested/api-resolvers/.../get_stepfunction_execution_resolver``). Each had arrived at
the reading independently and each had arrived at a *different* wrong answer. Three
attributed a failure routed through a ``Catch`` to the handler the workflow moved into
rather than to the state that failed: two whenever the handler was a ``Fail`` state
(#1139, #1168), and the monitoring timeline's detail extractor whenever it was a ``Task``
state, which it was believed to be immune to. And two reported the **earliest** failure in
the history rather than the one the execution ended on, which after a retry the workflow
survived names a state that succeeded (#1185). Fixing one copy left the
others wrong, which is why the rule lives here and not in any caller.

This module imports nothing beyond the standard library, deliberately. The deployment
harness runs in CodeBuild before anything in ``idp_common``'s optional dependency sets
is guaranteed usable, and the agent tool already pays for ``strands`` and ``boto3`` on
its own account; a shared rule that dragged either of those in could not be shared.

**The trap that makes the wrong answer look right.** ``FailStateEntered`` is a real
``HistoryEventType`` and so matches a ``StateEntered`` suffix, and the terminal
``ExecutionFailed`` arrives *after* the ``Catch`` has already transitioned. So the
history of a caught failure reads, oldest-first::

    TaskStateEntered:  Extraction      <- the state that actually failed
    TaskFailed
    FailStateEntered:  <handler>
    ExecutionFailed

"the last state entered before the failure" names the handler, and in reverse order
"the first state entered we see" names it too. Both spellings are wrong, and both read
as obviously right. The rule that works is to keep the two apart: the state comes from
the last **task-level** failure, and the error text from the terminal event.

The limitation is worth knowing before relying on the answer: attribution infers
causality from **adjacency**, so inside a concurrent ``Map`` — whose iterations share
one execution history and therefore interleave — it can name a sibling iteration's
state. The exact fix is to walk ``previousEventId``, which gives the causal chain
rather than the neighbouring event, and it is a larger change than either caller has
made. Note that such a walk must start from an **outcome** event: a
``TaskStateEntered`` event precedes its own ``TaskScheduled``, so walking back from a
state-transition event reaches the *previous* state's events, not its own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Failure event types, split by whether the failure is attributable to a STATE or to
# the execution as a whole. The split is what lets a caller report the state that
# failed rather than the Catch handler the workflow moved into afterwards; the union is
# what callers match on to find a failure at all, so the two readings are one list and
# cannot drift apart.
#
# ⚠️ **Membership is a rule about capability, not a list of spellings.** An event type
# belongs here exactly when its own detail shape in the Step Functions service model
# declares both ``error`` and ``cause`` — that is what it means for an event to carry a
# failure — and the axis it lands on is whether the thing that failed was a state doing
# work or the execution itself. The spelling rule anyone writes first, "the name ends in
# ``Failed``, ``TimedOut`` or ``Aborted``", is **wrong in both directions** and is the
# reason four readers of this history arrived at four different vocabularies. It admits
# nine container and transition events that carry no error text at all, and on its own it
# gives no reason to keep ``ExecutionAborted`` out. The classification is asserted
# against the bundled botocore model for **every** member of ``HistoryEventType``, so a
# type the service adds later has to be placed deliberately rather than defaulting into
# or out of the vocabulary.
#
# The literals are spelled out rather than computed because this module imports nothing
# outside the standard library (see the module docstring); the derivation lives in
# `tests/unit/test_stepfunctions_history.py`, which reads the model and fails if these
# sets and the model disagree.
#
# Task-level: a state was doing work and that work failed. Thirteen types, covering the
# three task integrations (`Task`, Lambda, Activity) at each of the four points work can
# fail — scheduling it, starting it, running it, and waiting for it — plus a distributed
# `Map` run and a JSONata/intrinsic evaluation.
TASK_LEVEL_FAILURE_EVENTS = frozenset(
    {
        "ActivityFailed",
        "ActivityScheduleFailed",
        "ActivityTimedOut",
        "EvaluationFailed",
        "LambdaFunctionFailed",
        "LambdaFunctionScheduleFailed",
        "LambdaFunctionStartFailed",
        "LambdaFunctionTimedOut",
        "MapRunFailed",
        "TaskFailed",
        "TaskStartFailed",
        "TaskSubmitFailed",
        "TaskTimedOut",
    }
)
# Execution-level: the execution ended. By this point a caught failure has already
# transitioned into its handler, so these events say nothing about which state failed
# — only why the execution stopped. Their error text is still the text to report.
EXECUTION_LEVEL_FAILURE_EVENTS = frozenset({"ExecutionFailed", "ExecutionTimedOut"})
FAILURE_EVENTS = TASK_LEVEL_FAILURE_EVENTS | EXECUTION_LEVEL_FAILURE_EVENTS

#: The one event type that carries ``error`` and ``cause`` and is still **not** treated
#: as a failure here. An abort is an externally requested stop, not something a state
#: did: ``describe_execution`` reports it as the ``ABORTED`` status, and a caller that
#: wants to explain one has the status to key on.
#:
#: What the exclusion actually changes, measured rather than argued: an execution
#: cancelled while it was running healthily has no task-level failure in its history, so
#: with ``ExecutionAborted`` held out both functions here answer "no failure". With it
#: admitted they report a failure whose error text is the cancellation and whose state is
#: whichever state the execution was in — measured, a **confident wrong answer** rather
#: than an admitted unknown, which is the costlier of the two. On an execution aborted **after** a state had genuinely failed it
#: changes the **state** named not at all — the task-level event is matched on its own
#: account either way — while the ``event_type``, ``error`` and ``cause`` reported do
#: change, from the task failure to the cancellation. The state is the part that matters
#: here and the part that is unaffected; the error text is not.
#:
#: Listed rather than merely omitted so the closure test over ``HistoryEventType`` can
#: tell a deliberate exclusion from an unclassified new type.
NOT_A_FAILURE_DESPITE_ERROR_DETAIL = frozenset({"ExecutionAborted"})

#: The subjects a task-level failure can belong to. Every member of
#: :data:`TASK_LEVEL_FAILURE_EVENTS` is named ``<Subject><Outcome>``, and the closure test
#: fails on a failure type belonging to none of these — so a subject the service adds
#: cannot arrive without someone deciding whether its failures can be recovered.
FAILURE_SUBJECTS = frozenset(
    {"Task", "LambdaFunction", "Activity", "MapRun", "Evaluation"}
)

#: The events that say a failed subject's work **later succeeded**, so a failure already
#: recorded against it was recovered rather than terminal.
#:
#: A caller rendering per-instance status needs these, and it needs them rather than the
#: state's ``*StateExited`` transition. A ``Retry`` re-runs the work without re-entering
#: the state, so a recovered failure and a terminal one are distinguished only by whether
#: a later attempt succeeded — and a reader that records a failure and never reconsiders it
#: reports a step as failed on an execution that succeeded.
#:
#: ⚠️ **Keying on the success event rather than on ``*StateExited`` is a fail-safe choice,
#: and that is the whole reason for it.** Whether a failure routed through a ``Catch`` also
#: emits a state-exit transition for the state that failed is a property of the service,
#: not of the model, and cannot be established offline. If it does, clearing on
#: ``*StateExited`` would mark a genuinely failed state as succeeded — walking #1139 back.
#: Clearing on a success event cannot: work that failed terminally emits no success.
#:
#: **Derived, not judged.** It is ``<Subject>Succeeded`` for every subject in
#: :data:`FAILURE_SUBJECTS` the service declares one for, which is four of the five —
#: ``EvaluationSucceeded`` is not a ``HistoryEventType`` at all, because an expression that
#: failed to evaluate is not retried. An earlier form of this set asked instead whether a
#: subject's work was "an attempt the service can re-run", and answered no for ``MapRun``
#: on the stated ground that a distributed map run is not re-run into success inside one
#: history. That is false of this repository's own workflow: ``ExtractionShardMap`` is a
#: ``DISTRIBUTED`` map carrying a ``Retry`` on the **Map state**, so a second ``MapRun``
#: starts in the same parent history and a recovered map run showed as a permanently failed
#: step on an execution that succeeded. Asking the model which success events exist needs
#: no such belief.
#:
#: ⚠️ ``MapRunSucceeded`` carries **no detail member at all**, so it is not usable with
#: :func:`failure_detail_key`. Only the event type matters to a caller clearing a failure.
FAILURE_RECOVERY_EVENTS = frozenset(
    {"TaskSucceeded", "LambdaFunctionSucceeded", "ActivitySucceeded", "MapRunSucceeded"}
)

#: Suffix shared by every state-transition event. The history API prefixes each one
#: with the state's own type — ``TaskStateEntered``, ``ChoiceStateEntered``,
#: ``FailStateEntered`` and five more — and never emits a bare ``StateEntered``; that
#: spelling belongs to the *detail* field, ``stateEnteredEventDetails``. Matching the
#: suffix covers all of them, including any the service adds later.
STATE_ENTERED_SUFFIX = "StateEntered"

#: The single detail shape in the service model that names a state **itself** rather than
#: leaving it to be inferred from the surrounding events: ``EvaluationFailedEventDetails``
#: carries ``state`` alongside ``error`` and ``cause``. Where it is present it is the
#: authority and adjacency is not consulted, which matters most inside a concurrent
#: ``Map``, where the neighbouring transition can belong to a sibling iteration. The
#: closure test asserts this is still the only such shape, so a second one the service
#: adds is handled rather than silently falling back to adjacency.
SELF_NAMING_FAILURE_EVENTS = frozenset({"EvaluationFailed"})


def failure_detail_key(event_type: str) -> str:
    """The ``HistoryEvent`` member that holds this event type's own detail.

    For every type in :data:`FAILURE_EVENTS` and
    :data:`NOT_A_FAILURE_DESPITE_ERROR_DETAIL` the model names the member after the event
    type with the first letter lowercased and ``EventDetails`` appended — ``TaskFailed`` →
    ``taskFailedEventDetails``. :data:`FAILURE_RECOVERY_EVENTS` is deliberately **not** in
    that list: ``MapRunSucceeded`` declares no detail member at all, and a caller clearing
    a recovered failure reads only the event type. This is computed rather than carried as a hand-written
    mapping because two of the four readers of this history carried such a mapping, each
    covering only the types its author had thought of, and a type missing from the mapping
    loses its error text silently: the event is recognised as a failure and reports an
    empty error.

    ⚠️ **The convention is not universal across ``HistoryEventType``, so do not use this
    as a general event-to-detail lookup.** 27 of the 62 types have no member of this name,
    and they split cleanly in two: 15 are the ``*StateEntered`` and ``*StateExited``
    transitions, which share ``stateEnteredEventDetails`` / ``stateExitedEventDetails``
    between them with no per-type spelling, and the other 12 declare no detail member at
    all — ``MapRunSucceeded`` and ``MapStateFailed`` among them. The four ``MapIteration*``
    types are in neither group: they do follow the convention. Every call site here is
    reached only after membership of one of the classified sets has been checked, and the
    closure test asserts the convention holds for all of those — and, in the other
    direction, that no detail member carrying ``error`` and ``cause`` is unreachable from
    some event type through it.
    """
    return event_type[:1].lower() + event_type[1:] + "EventDetails"


def state_named_by(event: Dict[str, Any]) -> Optional[str]:
    """The state an event names outright, or ``None`` if it does not name one.

    Only :data:`SELF_NAMING_FAILURE_EVENTS` do. Where one does, this is the authority and
    adjacency must not override it — including for a caller that is labelling individual
    timeline rows rather than answering for the execution as a whole, which is where the
    two readings in :mod:`idp_common.monitoring.stepfunctions_service` had drifted apart.
    """
    if event.get("type", "") not in SELF_NAMING_FAILURE_EVENTS:
        return None
    detail = event.get(failure_detail_key(event["type"]), {})
    return (detail.get("state") or None) if isinstance(detail, dict) else None


def to_chronological(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return history events oldest-first, whatever order they arrive in.

    ``get_execution_history`` returns a page newest-first or oldest-first depending on
    ``reverseOrder``, and the rule below wants chronological. The ordering key is the
    event ``id``, which the history API models as a **required** integer numbered
    sequentially from one, so it is a total order with no ties and no direction to
    infer. ``timestamp`` is deliberately *not* used: it has millisecond resolution,
    adjacent events routinely share a value, and any tie-prone key leaves a sort
    dependent on the arrival order it is supposed to be correcting.

    A list whose events do not all carry an integer ``id`` is not a history page from
    the API. The direction is then read from the two ends and corrected by reversing,
    which is exact when it applies but cannot see a tie between them -- so the fallback
    is logged rather than silent.
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


def failing_state(events: List[Dict[str, Any]]) -> Optional[str]:
    """The state the execution's terminal failure is attributable to, or ``None``.

    ``events`` may arrive in either direction; it is ordered here.

    Two choices carry the correctness, and both are the opposite of the obvious one:

    * The **last** failure in the history wins, not the first. The unified workflow
      retries throttles, service exceptions and timeouts in many places, so a
      recovered attempt leaves a ``TaskFailed`` behind that the execution went on to
      survive. Taking the earliest failure names a state that succeeded.
    * A **task-level** failure decides the state; an execution-level one does not. See
      the module docstring for why — by the time ``ExecutionFailed`` arrives, a caught
      failure has already entered its handler.

    ``None`` means the events hold no failure, or hold one with no preceding state
    transition (a window that starts after the failing state was entered). Callers
    render their own placeholder rather than being handed a confident-looking guess.
    """
    last_entered: Optional[str] = None
    last_task_failure_state: Optional[str] = None
    attributed: Optional[str] = None

    for event in to_chronological(events):
        event_type = event.get("type", "")
        if event_type.endswith(STATE_ENTERED_SUFFIX):
            # `name` is a required member of StateEnteredEventDetails, so a real page
            # always has one; treat a missing or empty name as unknown rather than
            # reporting the empty string as a state.
            last_entered = event.get("stateEnteredEventDetails", {}).get("name") or None
        if event_type in FAILURE_EVENTS:
            if event_type in TASK_LEVEL_FAILURE_EVENTS:
                # An event that names its own state is the authority; adjacency is the
                # fallback, not the other way round.
                last_task_failure_state = state_named_by(event) or last_entered
            attributed = last_task_failure_state or last_entered

    return attributed


def terminal_failure(events: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The failure that ended the execution, or ``None`` if the events hold none.

    ``events`` may arrive in either direction; it is ordered here.

    This is :func:`failing_state`'s answer plus the error text to show beside it, and it
    exists because the two are read from **different events** and every reader that
    derived them from one event got one of them wrong. The state comes from the last
    task-level failure; the error text comes from the last failure event of any kind,
    which on an execution that ended is the terminal execution-level one. Reading both
    from the terminal event names the ``Catch`` handler (#1139, #1168); reading both from
    the task failure discards the error the workflow actually ended on.

    Returns ``{"event_type", "error", "cause", "state"}``, all strings. ``state`` is
    ``""`` when the state is not knowable from these events — the caller renders its own
    placeholder rather than being handed a guess. ``event_type`` is the event the error
    text came from, which is the one a caller should name if it explains where it looked.

    ⚠️ **A recovered failure still counts here when the execution ended in failure, and
    that is the intended reading.** What this does *not* do is decide whether an
    execution failed at all: a history whose only failure was retried successfully still
    yields a result, because these events alone cannot distinguish "recovered and went on
    to succeed" from "recovered and failed later outside this window". A caller that
    knows the execution's status — from ``describe_execution`` rather than from the
    history — should consult it first and not ask this question of a successful
    execution.
    """
    ordered = to_chronological(events)

    text_source: Optional[Dict[str, Any]] = None
    for event in ordered:
        if event.get("type", "") in FAILURE_EVENTS:
            text_source = event
    if text_source is None:
        return None

    event_type = text_source.get("type", "")
    detail = text_source.get(failure_detail_key(event_type), {})
    if not isinstance(detail, dict):
        detail = {}

    return {
        "event_type": event_type,
        # `error` and `cause` are the model's spellings. The capitalised variants are
        # accepted because a `Fail` state's own configuration uses `Error`/`Cause`, and
        # callers of this module have been handed hand-built events using those.
        "error": detail.get("error") or detail.get("Error") or "",
        "cause": detail.get("cause") or detail.get("Cause") or "",
        "state": failing_state(ordered) or "",
    }


def failing_state_is_resolvable(
    events: List[Dict[str, Any]], *, more_pages: bool
) -> bool:
    """Can :func:`failing_state` name the state from the events fetched so far?

    This is the stop condition for a caller paginating **backwards from the failure**:
    one page holds the failure event, but the failing state's ``StateEntered`` is the
    earlier of the two and can sit outside it.

    ``events`` is newest-first, which is how such a caller requests the history, so
    "older than" means "at a higher index".

    ⚠️ **An execution-level failure alone is NOT sufficient while pages remain**, and
    that exception is the whole reason this takes ``more_pages``. On a caught failure
    the history reads, newest first::

        ExecutionFailed
        FailStateEntered: <handler>      <- the nearest older StateEntered
        TaskFailed
        TaskStateEntered: <the state that failed>

    so "the picked failure has an older ``StateEntered``" is satisfied by the Catch
    handler's own transition. Stopping there reports the handler — the misattribution
    :func:`failing_state` exists to avoid — and, worse, reports it as fully resolved,
    removing the one signal that the answer might be wrong. Continuing instead reaches
    the ``TaskFailed`` and names the real state; if the pages run out first, the caller
    still knows the answer is unresolved and can say so.

    Returns True when there is no failure at all: nothing is being explained, so there
    is nothing further back worth fetching.
    """
    task_level_index = None
    for index, event in enumerate(events):
        if event.get("type", "") in TASK_LEVEL_FAILURE_EVENTS:
            task_level_index = index
            break

    failure_index = task_level_index
    if failure_index is None:
        for index, event in enumerate(events):
            if event.get("type", "") in FAILURE_EVENTS:
                failure_index = index
                break
    if failure_index is None:
        return True

    if task_level_index is None and more_pages:
        return False

    return any(
        event.get("type", "").endswith(STATE_ENTERED_SUFFIX)
        for event in events[failure_index + 1 :]
    )
