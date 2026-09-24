# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One reading of a Step Functions execution history: which state failed.

Two very different readers need this answer — the error-analyzer agent tool that
explains a failed document to an operator
(``idp_common.agents.error_analyzer.tools.stepfunction_tool``) and the CodeBuild
deployment harness that snapshots a failed execution before it tears the stack down
(``scripts/sdlc/codebuild_deployment.py``). Each had arrived at the same reading of
the history independently, and each had arrived at the same wrong answer
independently: a failure routed through a ``Catch`` was attributed to the handler the
workflow moved into rather than to the state that failed (#1139, #1168). Fixing one
copy left the other wrong, which is why the rule lives here now and not in either
caller.

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
# Task-level: a state was doing work and that work failed.
TASK_LEVEL_FAILURE_EVENTS = frozenset(
    {"TaskFailed", "LambdaFunctionFailed", "TaskTimedOut"}
)
# Execution-level: the execution ended. By this point a caught failure has already
# transitioned into its handler, so these events say nothing about which state failed
# — only why the execution stopped. Their error text is still the text to report.
EXECUTION_LEVEL_FAILURE_EVENTS = frozenset({"ExecutionFailed", "ExecutionTimedOut"})
FAILURE_EVENTS = TASK_LEVEL_FAILURE_EVENTS | EXECUTION_LEVEL_FAILURE_EVENTS

#: Suffix shared by every state-transition event. The history API prefixes each one
#: with the state's own type — ``TaskStateEntered``, ``ChoiceStateEntered``,
#: ``FailStateEntered`` and five more — and never emits a bare ``StateEntered``; that
#: spelling belongs to the *detail* field, ``stateEnteredEventDetails``. Matching the
#: suffix covers all of them, including any the service adds later.
STATE_ENTERED_SUFFIX = "StateEntered"


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
                last_task_failure_state = last_entered
            attributed = last_task_failure_state or last_entered

    return attributed


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
