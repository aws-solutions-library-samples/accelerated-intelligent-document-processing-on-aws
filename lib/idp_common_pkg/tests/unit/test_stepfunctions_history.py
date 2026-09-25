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
    FAILURE_RECOVERY_EVENTS,
    FAILURE_SUBJECTS,
    NOT_A_FAILURE_DESPITE_ERROR_DETAIL,
    SELF_NAMING_FAILURE_EVENTS,
    STATE_ENTERED_SUFFIX,
    TASK_LEVEL_FAILURE_EVENTS,
    failing_state,
    failing_state_is_resolvable,
    failure_detail_key,
    terminal_failure,
    to_chronological,
)
from idp_common.stepfunctions_history import (
    SELF_NAMING_FAILURE_EVENTS as _SELF_NAMING,
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

        The model still has more non-execution failure-*named* types than this vocabulary
        matches, and every one of them is a container or transition event carrying no error
        text at all — ``MapStateFailed``, ``ParallelStateFailed``, ``MapIterationFailed``
        and the five ``*Aborted`` transitions. They are excluded on the capability rule
        rather than because the workflow happens not to emit them; the partition is
        asserted in full above, and admitting one would move attribution outward from the
        state that failed to the state containing it.

        What this asserts is the direction: everything claimed task-level really is a
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


@pytest.mark.unit
class TestTheVocabularyIsDerivedFromTheServiceModel:
    """Which event types count as failures, measured against the model, not listed.

    The vocabulary is the design decision in #1185: four readers of this history each
    carried a private list, the widest covered six of the sixteen types that can carry a
    failure, and a type missing from a list is a failure that names no state at all.

    The rule is **capability**: an event type can carry a failure exactly when its own
    detail shape declares both ``error`` and ``cause``. The rule anyone writes first —
    the type name ends in ``Failed``, ``TimedOut`` or ``Aborted`` — is wrong in both
    directions, and the second test here measures by how much.
    """

    def _partition(self):
        """Every ``HistoryEventType``, split by whether it can carry a failure.

        ⚠️ Capability is resolved through this module's own ``_detail_key_for``, **not**
        through ``failure_detail_key``. The production helper implements only the
        ``lowerCamel + EventDetails`` convention, which 27 of the 62 types do not follow —
        the state transitions share two members between them. Deriving the universe with it
        made this closure narrower than it reads: a new capable type whose detail member is
        spelled any other way landed in ``incapable`` and defaulted **out** of the
        vocabulary with every assertion here green. Both routes were demonstrated against a
        doctored model. ``_detail_key_for`` knows the shared spellings, so the universe is
        the one the enum actually describes.
        """
        model = _service_model()
        members = model.shape_for("HistoryEvent").members
        capable, incapable = set(), set()
        for event_type in model.shape_for("HistoryEventType").enum:
            key = _detail_key_for(event_type)
            shape = members.get(key) if key else None
            if (
                shape is not None
                and "error" in shape.members
                and "cause" in shape.members
            ):
                capable.add(event_type)
            else:
                incapable.add(event_type)
        return capable, incapable

    def test_no_error_carrying_detail_member_is_unreachable_from_an_event_type(self):
        """The closure from the MEMBER side, which the enum-side one cannot see.

        The enum-side check asks "is every capable type classified". This asks the reverse:
        is every detail member carrying ``error`` and ``cause`` reachable from some event
        type through the production helper? A member no type maps onto is a failure the
        vocabulary can never name, and it would arrive silently — nothing else here
        iterates the members.
        """
        members = _service_model().shape_for("HistoryEvent").members
        carrying = {
            name
            for name, shape in members.items()
            if hasattr(shape, "members")
            and "error" in shape.members
            and "cause" in shape.members
        }
        assert carrying, (
            "no detail member carries error and cause; this check is vacuous"
        )
        reachable = {
            failure_detail_key(event_type)
            for event_type in _service_model().shape_for("HistoryEventType").enum
        }
        assert carrying - reachable == set(), (
            "these detail members carry error and cause but no event type maps onto them "
            "through failure_detail_key, so a failure carried on one could never be named"
        )

    def test_the_production_key_helper_covers_every_classified_type(self):
        """``failure_detail_key`` is used at production call sites only after membership of
        one of the three sets has been checked, so what has to hold is that it is right for
        those — not for the whole enum, which it is not and does not claim to be."""
        members = _service_model().shape_for("HistoryEvent").members
        # Deliberately NOT including FAILURE_RECOVERY_EVENTS: `MapRunSucceeded` carries no
        # detail member at all, and a caller clearing a failure reads only the event type.
        classified = FAILURE_EVENTS | NOT_A_FAILURE_DESPITE_ERROR_DETAIL
        for event_type in classified:
            assert failure_detail_key(event_type) in members, event_type
            assert failure_detail_key(event_type) == _detail_key_for(event_type), (
                f"{event_type}: the convention and the model disagree"
            )

    def test_the_convention_is_not_universal_and_the_helper_says_so(self):
        """Measured, because ``failure_detail_key``'s docstring states this as the reason it
        must not be used as a general lookup. If the model became uniform the warning would
        be stale."""
        members = _service_model().shape_for("HistoryEvent").members
        enum = set(_service_model().shape_for("HistoryEventType").enum)
        off_convention = {t for t in enum if failure_detail_key(t) not in members}
        assert len(off_convention) > 10, off_convention
        assert {"TaskStateEntered", "TaskStateExited"} <= off_convention

    def test_the_enum_is_fully_classified_in_both_directions(self):
        """The closure. A type the service adds must be placed, not defaulted.

        Every type that CAN carry a failure is either in the vocabulary or named as a
        deliberate exclusion, and no type that CANNOT carry one is in the vocabulary.
        Without the second half the vocabulary could quietly grow a container event that
        moves attribution outward from the state that failed to the state holding it.
        """
        capable, incapable = self._partition()
        classified = FAILURE_EVENTS | NOT_A_FAILURE_DESPITE_ERROR_DETAIL

        assert capable - classified == set(), (
            "these event types carry `error` and `cause` but are neither treated as a "
            "failure nor listed as a deliberate exclusion"
        )
        assert FAILURE_EVENTS & incapable == set(), (
            "these are in the failure vocabulary but their detail shape carries no "
            "error text, so they would name a state and show nothing beside it"
        )
        assert classified - capable == set(), (
            "these are classified but the model gives them no error/cause detail"
        )

    def test_the_name_based_rule_is_wrong_in_both_directions(self):
        """Why the vocabulary is not `"Failed" in event_type`.

        Measured rather than asserted, because "it ends in Failed" is the rule a reader
        assumes is in force and it is the one this module deliberately does not use.
        """
        capable, _ = self._partition()
        by_name = {
            event_type
            for event_type in _service_model().shape_for("HistoryEventType").enum
            if any(word in event_type for word in ("Failed", "TimedOut", "Aborted"))
        }
        # Admits container and transition events with no error text at all.
        assert by_name - capable, "the name rule would admit nothing extra"
        for event_type in by_name - capable:
            assert event_type not in FAILURE_EVENTS, event_type
        # And misses nothing here only because every capable type happens to be named
        # that way — so the two rules agree on the easy direction and not on this one.
        assert capable - by_name == set()

    def test_no_failure_type_is_on_both_sides_of_the_state_execution_split(self):
        """The split is what keeps a `Catch` handler from being reported, so it has to
        be a partition rather than two overlapping lists."""
        assert TASK_LEVEL_FAILURE_EVENTS & EXECUTION_LEVEL_FAILURE_EVENTS == set()
        assert (
            TASK_LEVEL_FAILURE_EVENTS | EXECUTION_LEVEL_FAILURE_EVENTS == FAILURE_EVENTS
        )
        assert FAILURE_EVENTS & NOT_A_FAILURE_DESPITE_ERROR_DETAIL == set()

    def test_the_excluded_type_is_a_real_one_shielding_a_real_decision(self):
        """A non-vacuity check on the exclusion: `ExecutionAborted` exists, carries error
        text, and is therefore genuinely being held out rather than merely absent."""
        capable, _ = self._partition()
        assert NOT_A_FAILURE_DESPITE_ERROR_DETAIL
        assert NOT_A_FAILURE_DESPITE_ERROR_DETAIL <= capable

    def test_the_detail_key_derivation_matches_every_member_the_model_declares(self):
        """`failure_detail_key` replaces a hand-written mapping in two modules. If the
        naming convention has an exception anywhere, this is where it shows up."""
        members = _service_model().shape_for("HistoryEvent").members
        for event_type in FAILURE_EVENTS | NOT_A_FAILURE_DESPITE_ERROR_DETAIL:
            assert failure_detail_key(event_type) in members, event_type

    def test_evaluation_failed_is_the_only_event_that_names_its_own_state(self):
        """`SELF_NAMING_FAILURE_EVENTS` is derived, and `state` is REQUIRED on that
        shape — so where the event exists the name is always there and adjacency is
        never needed for it. A second such shape must be handled, not ignored."""
        model = _service_model()
        members = model.shape_for("HistoryEvent").members
        self_naming = {
            event_type
            for event_type in model.shape_for("HistoryEventType").enum
            if (shape := members.get(failure_detail_key(event_type))) is not None
            and "state" in shape.members
        }
        assert self_naming == set(SELF_NAMING_FAILURE_EVENTS)
        assert "state" in (
            model.shape_for("EvaluationFailedEventDetails").metadata.get("required")
            or []
        )


def _retried_then_succeeded() -> _History:
    """`OCR` failed once, was retried, succeeded — then `Extraction` failed terminally.

    This is the history that tells a terminal-failure rule apart from a first-failure
    one, and nothing in the suite had it. Note the shape of a `Retry`: it re-runs the
    **task**, not the state, so there is exactly one `TaskStateEntered` for `OCR` and the
    second attempt appears as another schedule/start pair under it. A fixture that
    entered the state twice would be testing a history the service does not produce.
    """
    return (
        _History()
        .add("ExecutionStarted")
        .add("TaskStateEntered", name="OCR")
        .add("TaskScheduled")
        .add("TaskStarted")
        .add("TaskFailed", error="ThrottlingException", cause="rate exceeded")
        .add("TaskScheduled")
        .add("TaskStarted")
        .add("TaskSucceeded")
        .add("TaskStateExited", name="OCR")
        .add("TaskStateEntered", name="Extraction")
        .add("TaskScheduled")
        .add("TaskStarted")
        .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
        .add("ExecutionFailed", error="States.TaskFailed", cause="propagated")
    )


@pytest.mark.unit
class TestARecoveredRetryIsNotTheFailureReported:
    """The first failure in a history is routinely one the execution survived."""

    def test_the_terminal_failure_names_the_state_not_the_recovered_one(self):
        assert failing_state(_retried_then_succeeded().events) == "Extraction"

    def test_the_answer_does_not_depend_on_the_page_direction(self):
        assert failing_state(_retried_then_succeeded().reversed()) == "Extraction"

    def test_the_recovered_state_is_the_one_a_first_failure_rule_would_name(self):
        """The fixture discriminates. Without this the test above could pass against a
        rule that simply named the last state entered, or the only state there is."""
        events = _retried_then_succeeded().events
        first_failure = next(e for e in events if e["type"] in FAILURE_EVENTS)
        entered_before = [
            e["stateEnteredEventDetails"]["name"]
            for e in events
            if e["type"].endswith(STATE_ENTERED_SUFFIX)
            and e["id"] < first_failure["id"]
        ]
        assert entered_before[-1] == "OCR"
        assert failing_state(events) != "OCR"


@pytest.mark.unit
class TestTerminalFailure:
    """`terminal_failure` reads the state and the error text from DIFFERENT events."""

    def test_no_failure_returns_none(self):
        history = (
            _History().add("TaskStateEntered", name="OCR").add("ExecutionSucceeded")
        )
        assert terminal_failure(history.events) is None

    def test_state_from_the_task_failure_and_text_from_the_terminal_event(self):
        """The #1139 shape. Reading both from one event gets one of them wrong: the
        terminal event names the handler, the task failure lacks the final error."""
        result = terminal_failure(_caught_failure().events)
        assert result == {
            "event_type": "ExecutionFailed",
            "error": "States.TaskFailed",
            "cause": "propagated",
            "state": "Extraction",
        }

    def test_a_recovered_retry_is_not_the_reported_failure(self):
        result = terminal_failure(_retried_then_succeeded().events)
        assert result is not None
        assert result["state"] == "Extraction"
        assert result["error"] == "States.TaskFailed"

    def test_either_page_direction_gives_the_same_answer(self):
        history = _caught_failure()
        assert terminal_failure(history.reversed()) == terminal_failure(history.events)

    def test_a_task_failure_with_no_terminal_event_still_reports(self):
        """A window that ends before the execution did: the text comes from the task
        failure because that is the last failure there is."""
        history = (
            _History()
            .add("TaskStateEntered", name="Extraction")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
        )
        assert terminal_failure(history.events) == {
            "event_type": "TaskFailed",
            "error": "ExtractionBoom",
            "cause": "a traceback",
            "state": "Extraction",
        }

    def test_an_unknowable_state_is_empty_rather_than_a_guess(self):
        history = _History().add("TaskFailed", error="Boom", cause="c")
        result = terminal_failure(history.events)
        assert result is not None
        assert result["state"] == ""

    def test_a_widened_vocabulary_member_names_a_state(self):
        """`LambdaFunctionTimedOut` is one of the ten task-level types the shared set did
        not hold. It must name the state, not fall through as "no failure"."""
        history = (
            _History()
            .add("TaskStateEntered", name="Assessment")
            .add("LambdaFunctionScheduled")
            .add("LambdaFunctionStarted")
            .add("LambdaFunctionTimedOut", error="States.Timeout", cause="900s")
        )
        assert failing_state(history.events) == "Assessment"
        result = terminal_failure(history.events)
        assert result is not None
        assert result["event_type"] == "LambdaFunctionTimedOut"
        assert result["error"] == "States.Timeout"


@pytest.mark.unit
class TestEvaluationFailedNamesItsOwnState:
    """The one failure event that carries the state name is believed over adjacency."""

    def test_the_events_own_state_wins_over_the_neighbouring_transition(self):
        """Discriminating by construction: the neighbouring transition names a
        DIFFERENT state, so an adjacency-only rule returns the wrong one."""
        history = (
            _History()
            .add("TaskStateEntered", name="NotTheFailingState")
            .add(
                "EvaluationFailed",
                state="BuildSectionInput",
                error="States.QueryEvaluationError",
                cause="bad path",
            )
        )
        assert failing_state(history.events) == "BuildSectionInput"
        result = terminal_failure(history.events)
        assert result is not None
        assert result["state"] == "BuildSectionInput"


@pytest.mark.unit
class TestAnAbortIsNotAFailure:
    """`ExecutionAborted` carries `error` and `cause` and is still held out.

    The behaviour the exclusion buys, and its exact boundary — both measured, because the
    first draft of the reasoning beside the constant claimed an effect it does not have.
    """

    def test_a_healthy_execution_that_was_cancelled_reports_no_failure(self):
        """The case the exclusion exists for: nothing failed, someone stopped it."""
        history = (
            _History()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="OCR")
            .add("ExecutionAborted", error="Stopped", cause="cancelled by an operator")
        )
        assert failing_state(history.events) is None
        assert terminal_failure(history.events) is None

    def test_an_abort_after_a_real_failure_still_names_the_state(self):
        """The boundary. The exclusion changes nothing here — the task-level event is
        matched on its own account — and a reader of the constant should not expect it
        to suppress this."""
        history = (
            _History()
            .add("TaskStateEntered", name="Extraction")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
            .add("ExecutionAborted", error="Stopped", cause="cancelled by an operator")
        )
        assert failing_state(history.events) == "Extraction"
        result = terminal_failure(history.events)
        assert result is not None
        # The text comes from the last event in the vocabulary, which is the task
        # failure: the abort is not in it.
        assert result["event_type"] == "TaskFailed"


@pytest.mark.unit
class TestAMalformedDetailDoesNotRaise:
    """These readers are handed events from four places, one of which builds them by
    hand. A detail member that is not an object must degrade to "no error text" rather
    than raising `AttributeError` inside a monitoring path.
    """

    def test_a_non_object_detail_yields_empty_text(self):
        events = [
            {
                "id": 1,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "Extraction"},
            },
            {"id": 2, "type": "TaskFailed", "taskFailedEventDetails": "not an object"},
        ]
        assert terminal_failure(events) == {
            "event_type": "TaskFailed",
            "error": "",
            "cause": "",
            "state": "Extraction",
        }

    def test_a_non_object_detail_on_a_self_naming_event_falls_back_to_adjacency(self):
        events = [
            {
                "id": 1,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "Extraction"},
            },
            {"id": 2, "type": "EvaluationFailed", "evaluationFailedEventDetails": None},
        ]
        assert failing_state(events) == "Extraction"

    def test_a_self_naming_event_with_no_state_falls_back_to_adjacency(self):
        events = [
            {
                "id": 1,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "Extraction"},
            },
            {
                "id": 2,
                "type": "EvaluationFailed",
                "evaluationFailedEventDetails": {"state": "", "error": "E"},
            },
        ]
        assert failing_state(events) == "Extraction"

    def test_the_capitalised_error_spellings_are_accepted(self):
        """A `Fail` state's own configuration uses `Error`/`Cause`, and hand-built events
        in this repository have used those spellings."""
        events = [
            {
                "id": 1,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {"name": "Extraction"},
            },
            {
                "id": 2,
                "type": "TaskFailed",
                "taskFailedEventDetails": {"Error": "Boom", "Cause": "why"},
            },
        ]
        result = terminal_failure(events)
        assert result is not None
        assert (result["error"], result["cause"]) == ("Boom", "why")


@pytest.mark.unit
class TestTheFailureRecoveryVocabulary:
    """`FAILURE_RECOVERY_EVENTS` is what tells a recovered failure from a terminal one.

    A `Retry` re-runs the work without re-entering the state, so the only thing in the
    history that distinguishes the two is whether a later attempt succeeded. A reader that
    records a failure and never reconsiders it shows a red step on an execution that
    succeeded.
    """

    def test_it_is_derived_from_the_failure_subjects(self):
        """The rule, not a list: `<Subject>Succeeded` for every failure subject the service
        declares one for. Asking the model which success events exist needs no belief about
        which kinds of work can be re-run — and an earlier form of this set, which did ask
        that, got `MapRun` wrong.
        """
        enum = set(_service_model().shape_for("HistoryEventType").enum)
        assert FAILURE_RECOVERY_EVENTS == {
            f"{subject}Succeeded"
            for subject in FAILURE_SUBJECTS
            if f"{subject}Succeeded" in enum
        }
        assert FAILURE_RECOVERY_EVENTS

    def test_map_run_is_included_because_a_map_state_can_be_retried(self):
        """⚠️ The member that was wrongly excluded. `ExtractionShardMap` in
        `patterns/unified` is a DISTRIBUTED map carrying a `Retry` on the Map state, so a
        second `MapRun` starts in the same parent history; excluding this left a recovered
        map run showing as a permanently failed step on an execution that succeeded."""
        assert "MapRunSucceeded" in FAILURE_RECOVERY_EVENTS
        assert "MapRunFailed" in TASK_LEVEL_FAILURE_EVENTS

    def test_the_only_subject_without_one_is_the_one_the_service_omits(self):
        """`Evaluation` is excluded by the model rather than by judgement."""
        enum = set(_service_model().shape_for("HistoryEventType").enum)
        missing = {
            subject for subject in FAILURE_SUBJECTS if f"{subject}Succeeded" not in enum
        }
        assert missing == {"Evaluation"}

    def test_every_member_is_a_real_event_type(self):
        enum = set(_service_model().shape_for("HistoryEventType").enum)
        for event_type in FAILURE_RECOVERY_EVENTS:
            assert event_type in enum, event_type

    def test_map_run_succeeded_carries_no_detail_member(self):
        """Why the recovery set is kept out of the detail-key assertions. Measured, because
        adding it there is the obvious tidy-up and it would fail."""
        members = _service_model().shape_for("HistoryEvent").members
        assert failure_detail_key("MapRunSucceeded") not in members
        for event_type in FAILURE_RECOVERY_EVENTS - {"MapRunSucceeded"}:
            shape = members.get(failure_detail_key(event_type))
            assert shape is not None and "output" in shape.members, event_type

    def test_every_task_level_failure_belongs_to_a_named_subject(self):
        """The closure. A subject the service adds forces a decision about whether its
        failures can be recovered, instead of defaulting to "cannot" — which is silent, and
        shows a red step on an execution that succeeded.
        """
        for event_type in TASK_LEVEL_FAILURE_EVENTS:
            assert any(event_type.startswith(s) for s in FAILURE_SUBJECTS), (
                f"{event_type} belongs to no subject in FAILURE_SUBJECTS: add it, and "
                "decide whether the service declares a <Subject>Succeeded for it"
            )

    def test_every_named_subject_actually_has_a_failure_type(self):
        """Non-vacuity in the other direction: a subject naming nothing in the vocabulary
        is dead, and pre-classifies whatever next matches its prefix."""
        for subject in FAILURE_SUBJECTS:
            assert any(t.startswith(subject) for t in TASK_LEVEL_FAILURE_EVENTS), (
                subject
            )

    def test_every_succeeded_event_in_the_model_is_classified(self):
        """Closure from the other direction: the set is exactly the ``*Succeeded`` events
        whose subject has a task-level failure type.

        The enum holds eight ``*Succeeded`` events and four are in the set. The other four —
        ``ExecutionSucceeded``, ``MapIterationSucceeded``, ``MapStateSucceeded``,
        ``ParallelStateSucceeded`` — are out because their subjects have **no** task-level
        failure type, so there is nothing for them to clear: the three container failures are
        excluded by the capability rule and ``Execution*`` is execution-level. Without this,
        adding one of the four would be caught only by an equality test that reads as a
        restatement of the code.
        """
        enum = set(_service_model().shape_for("HistoryEventType").enum)
        succeeded = {t for t in enum if t.endswith("Succeeded")}
        assert len(succeeded) > 4, succeeded
        for event_type in succeeded:
            subject = event_type[: -len("Succeeded")]
            has_failure = any(f.startswith(subject) for f in TASK_LEVEL_FAILURE_EVENTS)
            assert (event_type in FAILURE_RECOVERY_EVENTS) is has_failure, (
                f"{event_type}: in the recovery set = "
                f"{event_type in FAILURE_RECOVERY_EVENTS}, but its subject having a "
                f"task-level failure type = {has_failure}"
            )

    def test_a_recovery_event_is_not_also_a_failure_event(self):
        assert FAILURE_RECOVERY_EVENTS & FAILURE_EVENTS == set()

    def test_the_self_naming_set_is_still_reachable_under_its_alias(self):
        """Guards the import alias above from going stale."""
        assert _SELF_NAMING == SELF_NAMING_FAILURE_EVENTS
