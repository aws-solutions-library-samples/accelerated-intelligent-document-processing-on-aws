# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the shared Step Functions history rule (:mod:`idp_common.stepfunctions_history`).

Every history double here is built by :class:`_History`, which validates each event
against the **botocore service model** for Step Functions rather than against anyone's
memory of it: the event ``type`` must be a real ``HistoryEventType``, the detail key
must be a real ``HistoryEvent`` member, and every member the model marks *required* on
that detail shape is filled in. A hand-written double encodes a belief about the
service, and a belief is exactly what was wrong here — the whole defect (#1139, #1168)
rests on ``FailStateEntered`` being a real event type that matches a ``StateEntered``
suffix, which is easy to disbelieve and impossible to miss once the enum is read.

``TestTheFixtureIsDerivedFromTheServiceModel`` asserts that derivation, so the builder
cannot quietly stop checking anything.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from idp_common.stepfunctions_history import (
    EXECUTION_LEVEL_FAILURE_EVENTS,
    FAILURE_EVENTS,
    STATE_ENTERED_SUFFIX,
    TASK_LEVEL_FAILURE_EVENTS,
    failing_state,
    failing_state_is_resolvable,
    to_chronological,
)

_T0 = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)


def _service_model():
    """The bundled Step Functions model. Read once per call; botocore caches the JSON."""
    import botocore.session

    return botocore.session.get_session().get_service_model("stepfunctions")


def _detail_key_for(event_type: str) -> Optional[str]:
    """The ``HistoryEvent`` member carrying this event type's detail, or None.

    Most types name their own key — ``TaskFailed`` -> ``taskFailedEventDetails``. The
    state transitions do NOT: all eight of them share ``stateEnteredEventDetails`` /
    ``stateExitedEventDetails``, with no per-state-type spelling. That asymmetry is the
    reason a bare ``StateEntered`` looks like a plausible event type — it is the detail
    key's name, not an event type — so it is derived here rather than assumed.
    """
    if event_type.endswith(STATE_ENTERED_SUFFIX):
        return "stateEnteredEventDetails"
    if event_type.endswith("StateExited"):
        return "stateExitedEventDetails"
    candidate = f"{event_type[0].lower()}{event_type[1:]}EventDetails"
    members = _service_model().shape_for("HistoryEvent").members
    return candidate if candidate in members else None


class _History:
    """A list of history events, each checked against the service model as it is added."""

    #: Required members the rule under test does not care about, given a value so the
    #: double satisfies the model. Keyed by member name because the same names recur
    #: across shapes (``resource`` is required on ten of them).
    _FILLERS = {
        "resource": "arn:aws:lambda:us-east-1:123456789012:function:placeholder",
        "resourceType": "lambda",
        "region": "us-east-1",
        "parameters": "{}",
        "state": "PlaceholderState",
        "name": "PlaceholderState",
    }

    def __init__(self) -> None:
        self.model = _service_model()
        self.event_types = set(self.model.shape_for("HistoryEventType").enum)
        self.event_members = set(self.model.shape_for("HistoryEvent").members)
        self.events: List[Dict[str, Any]] = []
        self.checked_shapes: set[str] = set()

    def add(self, event_type: str, **detail: Any) -> "_History":
        assert event_type in self.event_types, (
            f"{event_type!r} is not a HistoryEventType in the service model; "
            "a fixture using it is testing a service that does not exist"
        )
        event_id = len(self.events) + 1
        event: Dict[str, Any] = {
            "id": event_id,
            "type": event_type,
            "timestamp": _T0 + timedelta(seconds=event_id),
        }
        key = _detail_key_for(event_type)
        if key is not None:
            assert key in self.event_members, key
            shape_name = f"{key[0].upper()}{key[1:]}"
            shape = self.model.shape_for(shape_name)
            self.checked_shapes.add(shape_name)
            for member in detail:
                assert member in shape.members, (
                    f"{shape_name} has no member {member!r}; the model lists "
                    f"{sorted(shape.members)}"
                )
            body = dict(detail)
            for member in shape.metadata.get("required") or []:
                body.setdefault(member, self._FILLERS[member])
            event[key] = body
        self.events.append(event)
        return self

    def reversed(self) -> List[Dict[str, Any]]:
        """The same page as ``reverseOrder=True`` returns it: newest first."""
        return list(reversed(self.events))


def _caught_failure() -> _History:
    """The shape at the centre of #1139 and #1168.

    ``Extraction`` failed; a ``Catch`` routed to a ``Fail`` state; the execution then
    ended. The handler's transition sits BETWEEN the task failure and the terminal
    event, which is what makes both "the last state entered" and "the first state
    entered seen in reverse order" name the handler.
    """
    return (
        _History()
        .add("ExecutionStarted")
        .add("TaskStateEntered", name="OCR")
        .add("TaskStateExited", name="OCR")
        .add("TaskStateEntered", name="Extraction")
        .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
        .add("FailStateEntered", name="ExtractionShardMapFailed")
        .add("ExecutionFailed", error="States.TaskFailed", cause="propagated")
    )


@pytest.mark.unit
class TestTheFixtureIsDerivedFromTheServiceModel:
    """What the builder above actually checks. Without these it could check nothing.

    Each assertion here is a fact about the service that the rule under test depends
    on, measured from the bundled model instead of restated.
    """

    def test_the_enum_was_read_and_is_not_empty(self):
        history = _History()
        assert len(history.event_types) > 50, (
            "HistoryEventType came back nearly empty, so the type assertions in the "
            "builder are vacuous"
        )

    def test_fail_state_entered_is_a_real_event_type_matching_the_suffix(self):
        """The fact the whole defect rests on."""
        history = _History()
        assert "FailStateEntered" in history.event_types
        assert "FailStateEntered".endswith(STATE_ENTERED_SUFFIX)

    def test_there_is_no_bare_state_entered_event_type(self):
        """``StateEntered`` is a DETAIL key, not an event type.

        A fixture using the bare spelling matches nothing the API emits, so a walk
        keyed on it looks tested and is not — which is how the earlier version of this
        analysis reported no state at all.
        """
        history = _History()
        assert "StateEntered" not in history.event_types
        assert "StateExited" not in history.event_types
        assert "stateEnteredEventDetails" in history.event_members

    def test_every_state_transition_shares_one_detail_key(self):
        """Derived, because it is the asymmetry that makes the bare spelling plausible."""
        history = _History()
        entered = {t for t in history.event_types if t.endswith(STATE_ENTERED_SUFFIX)}
        assert len(entered) >= 8, entered
        for event_type in entered:
            assert _detail_key_for(event_type) == "stateEnteredEventDetails"
            per_type = f"{event_type[0].lower()}{event_type[1:]}EventDetails"
            assert per_type not in history.event_members

    def test_the_ordering_key_is_a_required_integer(self):
        """``to_chronological`` sorts on ``id``; this is why that is a total order."""
        history = _History()
        event_shape = history.model.shape_for("HistoryEvent")
        assert "id" in (event_shape.metadata.get("required") or [])
        assert event_shape.members["id"].type_name == "long"

    def test_a_state_transition_always_carries_a_name(self):
        """So ``failing_state`` returning None means "no transition in the window",
        never "a transition with no name"."""
        history = _History()
        shape = history.model.shape_for("StateEnteredEventDetails")
        assert "name" in (shape.metadata.get("required") or [])

    def test_the_builder_fills_required_members_the_rule_ignores(self):
        """The double is model-complete even where the rule does not look.

        ``TaskFailedEventDetails`` requires ``resource`` and ``resourceType``; a double
        carrying only ``error``/``cause`` is not a response the service can return.
        """
        history = _caught_failure()
        assert "TaskFailedEventDetails" in history.checked_shapes
        task_failed = next(e for e in history.events if e["type"] == "TaskFailed")
        required = (
            history.model.shape_for("TaskFailedEventDetails").metadata.get("required")
            or []
        )
        assert required, "the shape declares no required members; this test is vacuous"
        for member in required:
            assert task_failed["taskFailedEventDetails"].get(member)

    def test_an_unknown_event_type_is_refused(self):
        """The builder's own guard, exercised — otherwise it could be a no-op."""
        with pytest.raises(AssertionError, match="not a HistoryEventType"):
            _History().add("TaskWentBang")

    def test_an_unknown_detail_member_is_refused(self):
        with pytest.raises(AssertionError, match="has no member"):
            _History().add("TaskFailed", reason="not a member of this shape")

    def test_every_name_in_the_vocabulary_is_a_real_event_type(self):
        """A typo in either set silently stops matching; the model is the spell-check."""
        history = _History()
        assert FAILURE_EVENTS
        assert FAILURE_EVENTS <= history.event_types, (
            FAILURE_EVENTS - history.event_types
        )

    def test_the_execution_level_set_is_exactly_what_the_model_implies(self):
        """Derived rather than listed: an execution-scoped failure or timeout.

        This is the half that must NOT decide the state, so getting it from the model
        means a new spelling joins it instead of being read as task-level by default.
        """
        history = _History()
        derived = {
            t
            for t in history.event_types
            if t.startswith("Execution")
            and (t.endswith("Failed") or t.endswith("TimedOut"))
        }
        assert derived == set(EXECUTION_LEVEL_FAILURE_EVENTS), derived

    def test_the_two_classes_partition_the_matched_set(self):
        """A new failure event type must be classified, not silently execution-level."""
        assert FAILURE_EVENTS == (
            TASK_LEVEL_FAILURE_EVENTS | EXECUTION_LEVEL_FAILURE_EVENTS
        )
        assert not (TASK_LEVEL_FAILURE_EVENTS & EXECUTION_LEVEL_FAILURE_EVENTS)

    def test_the_task_level_set_is_a_subset_of_the_non_execution_failures(self):
        """Bounded from both sides, and the residual is named rather than implied.

        The model has more non-execution failure types than this vocabulary matches
        (activity failures, task submit/start failures, Map and Parallel state
        failures). The unified workflow uses none of the constructs that emit them —
        there are no Activities, no ``.sync`` submit/start pairs — so widening the
        vocabulary would change behaviour with nothing to measure it against. What this
        asserts is the direction: everything claimed task-level really is a
        non-execution failure.
        """
        history = _History()
        non_execution_failures = {
            t
            for t in history.event_types
            if (t.endswith("Failed") or t.endswith("TimedOut"))
            and not t.startswith("Execution")
        }
        assert TASK_LEVEL_FAILURE_EVENTS <= non_execution_failures
        assert TASK_LEVEL_FAILURE_EVENTS < non_execution_failures, (
            "the vocabulary now covers every non-execution failure type; if that is "
            "intended this assertion should go, but check the Map/Parallel state "
            "failure types are really wanted as state attribution sources"
        )


@pytest.mark.unit
class TestFailingStateIsTheStateThatFailed:
    """#1139 / #1168: a ``Catch`` handler is not the state that failed."""

    def test_the_failing_state_is_reported_not_the_catch_handler(self):
        assert failing_state(_caught_failure().events) == "Extraction", (
            "the Catch handler is not the state that failed; reporting it sends "
            "whoever reads the answer to the wrong log group"
        )

    def test_the_answer_does_not_depend_on_the_arrival_order(self):
        """Both callers fetch ``reverseOrder=True``; one used to walk it in that order.

        The reverse-order reading is where #1168 lived, so the same page in both
        directions must give the same answer.
        """
        history = _caught_failure()
        assert failing_state(history.reversed()) == failing_state(history.events)
        assert failing_state(history.reversed()) == "Extraction"

    def test_a_recovered_earlier_failure_does_not_win(self):
        """A survived ``TaskFailed`` is ordinary — the workflow retries in many places.

        The LAST task-level failure explains the end of the execution; an earlier
        recovered one names a state that went on to succeed.
        """
        history = (
            _History()
            .add("TaskStateEntered", name="OCR")
            .add("TaskFailed", error="ThrottledOnce")
            .add("TaskStateExited", name="OCR")
            .add("TaskStateEntered", name="Extraction")
            .add("TaskFailed", error="ExtractionBoom")
            .add("FailStateEntered", name="FailState")
            .add("ExecutionFailed")
        )
        assert failing_state(history.events) == "Extraction"

    def test_an_execution_level_failure_alone_reports_the_last_state_entered(self):
        """Nothing better exists, and naming it beats naming nothing."""
        history = (
            _History()
            .add("TaskStateEntered", name="OCR")
            .add("TaskStateExited", name="OCR")
            .add("TaskStateEntered", name="Extraction")
            .add("ExecutionFailed", error="States.Timeout")
        )
        assert failing_state(history.events) == "Extraction"

    def test_an_uncaught_task_failure_names_its_own_state(self):
        history = (
            _History()
            .add("TaskStateEntered", name="Extraction")
            .add("TaskFailed", error="Boom")
            .add("ExecutionFailed")
        )
        assert failing_state(history.events) == "Extraction"

    def test_a_history_with_no_failure_has_no_failing_state(self):
        history = (
            _History()
            .add("TaskStateEntered", name="OCR")
            .add("TaskStateExited", name="OCR")
            .add("ExecutionSucceeded")
        )
        assert failing_state(history.events) is None

    def test_a_window_that_starts_after_the_transition_reports_unknown(self):
        """Honest ``None`` rather than a confident wrong name.

        A caller whose page begins inside the failure has nothing to attribute to, and
        the placeholder it renders is the signal that the window was too short.
        """
        truncated = [
            e
            for e in _caught_failure().events
            if e["type"] in ("TaskFailed", "ExecutionFailed")
        ]
        assert failing_state(truncated) is None

    def test_a_concurrent_map_still_misattributes_and_that_is_recorded(self):
        """The known limitation, pinned so it is a recorded gap rather than prose.

        Two ``Map`` iterations share one execution history, so above ``MaxConcurrency``
        1 their events interleave and adjacency stops implying causality. Asserted at
        the WRONG answer deliberately: the rule this replaced gave the same answer on
        this history, so it is not a regression, and asserting the aspiration would
        fail for a fix nobody has made. Walking ``previousEventId`` is the exact route;
        when someone takes it this should assert ``ExtractionStep``.
        """
        history = (
            _History()
            .add("MapStateEntered", name="ProcessSections")
            .add("TaskStateEntered", name="ExtractionStep")  # iteration A
            .add("TaskStateEntered", name="AssessmentStep")  # iteration B
            .add("TaskFailed", error="Boom")  # A's task fails
            .add("ExecutionFailed")
        )
        assert failing_state(history.events) == "AssessmentStep", (
            "if this now reports ExtractionStep, the causal-chain fix has landed and "
            "this test should assert that instead"
        )


@pytest.mark.unit
class TestToChronological:
    def test_a_reverse_order_page_is_restored_by_id(self):
        history = _caught_failure()
        assert [e["id"] for e in to_chronological(history.reversed())] == [
            e["id"] for e in history.events
        ]

    def test_an_already_chronological_page_is_unchanged(self):
        history = _caught_failure()
        assert to_chronological(history.events) == history.events

    def test_incomparable_timestamps_leave_the_order_alone(self):
        """Not an edge case invented for coverage: the two readers carry timestamps in
        different types.

        `idp_common.monitoring.stepfunctions_service` serialises them to ISO strings
        while the error-analyzer tool keeps the `datetime` objects boto3 returns, so a
        list assembled from both compares `str` to `datetime` and raises `TypeError`. The
        order is then genuinely unknowable, and leaving it as given is the only honest
        answer — reversing on a failed comparison would corrupt a page that was already
        chronological. This branch is in the one implementation both readers now share,
        so an uncovered branch here is uncovered for both.
        """
        mixed = [
            {"type": "TaskStateEntered", "timestamp": "2026-03-01T10:00:00+00:00"},
            {"type": "ExecutionFailed", "timestamp": _T0 + timedelta(seconds=9)},
        ]
        assert [event["type"] for event in to_chronological(mixed)] == [
            "TaskStateEntered",
            "ExecutionFailed",
        ]
        # And the reverse arrangement is equally left alone, because nothing was learned.
        assert [event["type"] for event in to_chronological(list(reversed(mixed)))] == [
            "ExecutionFailed",
            "TaskStateEntered",
        ]

    def test_a_page_without_ids_falls_back_to_the_two_timestamps(self):
        """Not a real API page, so the direction can only be inferred."""
        newest_first = [
            {"type": "ExecutionFailed", "timestamp": _T0 + timedelta(seconds=9)},
            {"type": "TaskStateEntered", "timestamp": _T0},
        ]
        assert [e["type"] for e in to_chronological(newest_first)] == [
            "TaskStateEntered",
            "ExecutionFailed",
        ]

    def test_a_single_event_needs_no_ordering(self):
        assert to_chronological([{"id": 7}]) == [{"id": 7}]


@pytest.mark.unit
class TestFailingStateIsResolvable:
    """The stop condition for a caller paging backwards from the failure."""

    def _newest_first(self, *types_and_names):
        history = _History()
        for event_type, name in types_and_names:
            history.add(event_type, **({"name": name} if name else {}))
        return history.reversed()

    def test_a_window_holding_the_task_failure_and_an_older_transition_is_enough(self):
        assert failing_state_is_resolvable(
            _caught_failure().reversed(), more_pages=False
        )

    def test_the_catch_handlers_own_transition_does_not_count_as_resolved(self):
        """The exception that ``more_pages`` exists for.

        Newest-first, the page holds ``ExecutionFailed`` then the handler's
        ``FailStateEntered``. "The failure has an older StateEntered" is satisfied — by
        the handler — so stopping here would report the handler AND report it as
        resolved, removing the one signal that the answer is suspect.
        """
        page = [
            e
            for e in _caught_failure().reversed()
            if e["type"] in ("ExecutionFailed", "FailStateEntered")
        ]
        assert not failing_state_is_resolvable(page, more_pages=True)

    def test_with_no_pages_left_the_same_window_stops_looking(self):
        """Not resolvable is not the same as not finished: there is nowhere else to go."""
        page = [
            e
            for e in _caught_failure().reversed()
            if e["type"] in ("ExecutionFailed", "FailStateEntered")
        ]
        assert failing_state_is_resolvable(page, more_pages=False)

    def test_a_failure_with_nothing_older_is_not_resolvable(self):
        page = [e for e in _caught_failure().reversed() if e["type"] == "TaskFailed"]
        assert not failing_state_is_resolvable(page, more_pages=True)

    def test_no_failure_at_all_stops_the_walk(self):
        """Nothing is being explained, so there is nothing further back worth fetching."""
        history = (
            _History().add("TaskStateEntered", name="OCR").add("ExecutionSucceeded")
        )
        assert failing_state_is_resolvable(history.reversed(), more_pages=True)

    def test_the_predicate_agrees_with_the_rule_it_gates(self):
        """The two must not disagree: a window called resolvable that yields no state
        would page forever or report unknown with the flag clear."""
        history = _caught_failure()
        page = history.reversed()
        for cut in range(1, len(page) + 1):
            window = page[:cut]
            if failing_state_is_resolvable(window, more_pages=True):
                assert failing_state(window) is not None or not any(
                    e["type"] in FAILURE_EVENTS for e in window
                ), window
