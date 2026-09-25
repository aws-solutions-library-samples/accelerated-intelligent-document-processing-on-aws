"""Which state `get_workflow_failure_details` names as the one that failed (#1168).

The deploy harness snapshots a FAILED Step Functions execution before teardown and
feeds it to the failure summary as the authoritative root cause, so the state name it
reports is the one whoever is diagnosing a pipeline failure goes looking for logs in.
It used to report the `Catch` handler: `FailStateEntered` matches a `StateEntered`
suffix, and in the reverse-order page the handler's transition is the newest one.

The rule is now shared with the error analyzer, which had the identical defect filed
separately as #1139 — so the tests that pin the *rule* live beside it, in
`lib/idp_common_pkg/tests/unit/test_stepfunctions_history.py`. What is asserted here is
this module's own half: that it calls the shared rule, that it pages far enough back to
reach the state, and that it says "unknown" rather than guessing when it cannot.

Event shapes are validated against the **botocore service model** rather than written
from memory; `TestTheHistoryDoubleIsCheckedAgainstTheServiceModel` asserts that the
validation is live, because a double that encodes a belief about the service is what
made the original reading look correct.
"""

import json

import pytest

# --------------------------------------------------------------------------- #
# history doubles, checked against the bundled Step Functions model
# --------------------------------------------------------------------------- #

_EXEC_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp-sm:abc123"
_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:idp-sm"


def _model():
    import botocore.session

    return botocore.session.get_session().get_service_model("stepfunctions")


def _event(event_id, event_type, detail_key=None, **detail):
    """One history event, refused unless the model says it can exist."""
    model = _model()
    assert event_type in set(model.shape_for("HistoryEventType").enum), (
        f"{event_type!r} is not a HistoryEventType; a fixture using it tests a "
        "service that does not exist"
    )
    event = {"id": event_id, "type": event_type}
    if detail_key is not None:
        assert detail_key in model.shape_for("HistoryEvent").members, detail_key
        shape = model.shape_for(f"{detail_key[0].upper()}{detail_key[1:]}")
        for member in detail:
            assert member in shape.members, f"{shape.name} has no member {member!r}"
        body = dict(detail)
        # `resource`/`resourceType` are required on TaskFailedEventDetails; a double
        # without them is not a response the service can return.
        for member in shape.metadata.get("required") or []:
            body.setdefault(member, "placeholder")
        event[detail_key] = body
    return event


def _entered(event_id, name, kind="Task"):
    return _event(
        event_id,
        f"{kind}StateEntered",
        detail_key="stateEnteredEventDetails",
        name=name,
    )


def _caught_failure_history():
    """`Extraction` failed, a Catch routed to a Fail state, the execution ended.

    Oldest-first here; the harness requests `reverseOrder=True`, so the handler's
    transition is what it sees first.
    """
    return [
        _event(1, "ExecutionStarted"),
        _entered(2, "OCR"),
        _event(3, "TaskStateExited", detail_key="stateExitedEventDetails", name="OCR"),
        _entered(4, "Extraction"),
        _event(
            5,
            "TaskFailed",
            detail_key="taskFailedEventDetails",
            error="ExtractionBoom",
            cause="Traceback: ValueError",
        ),
        _entered(6, "ExtractionShardMapFailed", kind="Fail"),
        _event(
            7,
            "ExecutionFailed",
            detail_key="executionFailedEventDetails",
            error="States.TaskFailed",
            cause="propagated",
        ),
    ]


class _FakeSfn:
    """A Step Functions client serving one execution's history in pages.

    Pages newest-first, the way `reverseOrder=True` does, and records every call so the
    pagination assertions have something to measure.
    """

    def __init__(self, events, page_size=None, execution_input=None):
        self._newest_first = list(reversed(events))
        self._page_size = page_size
        self.history_calls = []
        self._input = execution_input if execution_input is not None else "{}"

    def list_executions(self, **kwargs):
        return {"executions": [{"executionArn": _EXEC_ARN, "name": "exec-1"}]}

    def describe_execution(self, **kwargs):
        return {"input": self._input}

    def get_execution_history(self, **kwargs):
        self.history_calls.append(kwargs)
        assert kwargs.get("reverseOrder") is True, (
            "the window is taken newest-first so a capped read covers the failure"
        )
        size = self._page_size or kwargs["maxResults"]
        start = int(kwargs.get("nextToken") or 0)
        page = self._newest_first[start : start + size]
        end = start + len(page)
        result = {"events": page}
        if end < len(self._newest_first):
            result["nextToken"] = str(end)
        return result


class _FakeCfn:
    def __init__(self, state_machine_arn=_STATE_MACHINE_ARN):
        self._arn = state_machine_arn

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 spelling
        outputs = (
            [{"OutputKey": "StateMachineArn", "OutputValue": self._arn}]
            if self._arn
            else []
        )
        return {"Stacks": [{"Outputs": outputs}]}


def _details(cbd, monkeypatch, sfn, cfn=None):
    def _factory(service, *a, **kw):
        if service == "cloudformation":
            return cfn or _FakeCfn()
        if service == "stepfunctions":
            return sfn
        raise AssertionError(f"unexpected client: {service}")

    monkeypatch.setattr(cbd.boto3, "client", _factory)
    return cbd.get_workflow_failure_details("idp-citest-stack")


# --------------------------------------------------------------------------- #
# the fixture's own guard
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestTheHistoryDoubleIsCheckedAgainstTheServiceModel:
    def test_an_unknown_event_type_is_refused(self):
        with pytest.raises(AssertionError, match="not a HistoryEventType"):
            _event(1, "TaskWentBang")

    def test_an_unknown_detail_member_is_refused(self):
        with pytest.raises(AssertionError, match="has no member"):
            _event(1, "TaskFailed", detail_key="taskFailedEventDetails", reason="x")

    def test_fail_state_entered_is_a_real_event_type(self):
        """The fact the defect rests on: the handler's transition is indistinguishable
        from any other by suffix, so "newest StateEntered" names it."""
        assert "FailStateEntered" in set(_model().shape_for("HistoryEventType").enum)
        assert _caught_failure_history()[5]["type"] == "FailStateEntered"

    def test_required_detail_members_are_filled(self):
        """Otherwise the double is a response the API cannot produce."""
        task_failed = _caught_failure_history()[4]["taskFailedEventDetails"]
        required = (
            _model().shape_for("TaskFailedEventDetails").metadata.get("required") or []
        )
        assert required, "the shape declares nothing required; this test is vacuous"
        for member in required:
            assert task_failed.get(member)


# --------------------------------------------------------------------------- #
# get_workflow_failure_details
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestGetWorkflowFailureDetails:
    def test_the_failing_state_is_reported_not_the_catch_handler(
        self, cbd, monkeypatch
    ):
        """#1168. `ExtractionShardMapFailed` is the Fail state the Catch moved into."""
        sfn = _FakeSfn(_caught_failure_history())
        details = _details(cbd, monkeypatch, sfn)
        assert len(details) == 1
        assert details[0]["failed_state"] == "Extraction", (
            "reporting the Catch handler sends the reader of this summary to the "
            "wrong log group"
        )

    def test_the_error_text_still_comes_from_the_terminal_event(self, cbd, monkeypatch):
        """The state and the error come from different events, deliberately: the
        terminal event says why the execution ended, the task failure says where."""
        sfn = _FakeSfn(_caught_failure_history())
        details = _details(cbd, monkeypatch, sfn)
        assert details[0]["error"] == "States.TaskFailed"
        assert "propagated" in details[0]["cause"]

    def test_a_recovered_earlier_failure_does_not_win(self, cbd, monkeypatch):
        """The workflow retries in many places, so a survived TaskFailed is ordinary."""
        events = [
            _entered(1, "OCR"),
            _event(2, "TaskFailed", detail_key="taskFailedEventDetails", error="Thr"),
            _event(
                3, "TaskStateExited", detail_key="stateExitedEventDetails", name="OCR"
            ),
            _entered(4, "Extraction"),
            _event(5, "TaskFailed", detail_key="taskFailedEventDetails", error="Boom"),
            _entered(6, "FailState", kind="Fail"),
            _event(
                7,
                "ExecutionFailed",
                detail_key="executionFailedEventDetails",
                error="E",
            ),
        ]
        details = _details(cbd, monkeypatch, _FakeSfn(events))
        assert details[0]["failed_state"] == "Extraction"

    def test_the_window_pages_back_to_reach_the_transition(self, cbd, monkeypatch):
        """One page holds the failure; the failing state's transition is older.

        Served two events at a time, the first page is `ExecutionFailed` +
        `FailStateEntered` — the shape that satisfies "the failure has an older state
        transition" with the handler and would stop a naive walk at the wrong answer.
        """
        sfn = _FakeSfn(_caught_failure_history(), page_size=2)
        details = _details(cbd, monkeypatch, sfn)
        assert details[0]["failed_state"] == "Extraction"
        assert len(sfn.history_calls) > 1, "the walk never asked for a second page"

    def test_the_walk_stops_once_the_state_is_resolved(self, cbd, monkeypatch):
        """Bounded: the history of a large document is thousands of events and this runs
        for every failed execution in the summary."""
        sfn = _FakeSfn(_caught_failure_history())
        _details(cbd, monkeypatch, sfn)
        assert len(sfn.history_calls) == 1
        assert sfn.history_calls[0]["maxResults"] == cbd._FAILURE_HISTORY_PAGE_SIZE

    def test_the_page_walk_is_capped(self, cbd, monkeypatch):
        """A history that never yields the transition must not page forever."""
        never_resolves = [
            _event(i, "TaskFailed", detail_key="taskFailedEventDetails", error="Boom")
            for i in range(1, 40)
        ]
        sfn = _FakeSfn(never_resolves, page_size=1)
        details = _details(cbd, monkeypatch, sfn)
        assert len(sfn.history_calls) == cbd._FAILURE_HISTORY_MAX_PAGES
        assert details[0]["failed_state"] == "(unknown state)"

    def test_an_unresolvable_state_is_reported_as_unknown_not_guessed(
        self, cbd, monkeypatch
    ):
        """A window that begins inside the failure has nothing to attribute to.

        Saying so is the point: the placeholder is what tells the reader the name is
        missing, where a handler's name reads as an answer.
        """
        events = [
            _event(1, "TaskFailed", detail_key="taskFailedEventDetails", error="Boom"),
            _event(
                2,
                "ExecutionFailed",
                detail_key="executionFailedEventDetails",
                error="States.TaskFailed",
            ),
        ]
        details = _details(cbd, monkeypatch, _FakeSfn(events))
        assert details[0]["failed_state"] == "(unknown state)"
        assert details[0]["error"] == "States.TaskFailed"

    def test_a_history_read_failure_keeps_the_execution_in_the_report(
        self, cbd, monkeypatch
    ):
        """Losing the whole entry would lose the execution ARN too, which is the one
        thing that still lets someone look the failure up by hand."""

        class _Boom(_FakeSfn):
            def get_execution_history(self, **kwargs):
                raise RuntimeError("Throttling")

        details = _details(cbd, monkeypatch, _Boom(_caught_failure_history()))
        assert details[0]["execution_arn"] == _EXEC_ARN
        assert "could not read execution history" in details[0]["cause"]
        assert details[0]["failed_state"] == "(unknown state)"

    def test_the_deliberate_hook_fail_probe_is_still_excluded(self, cbd, monkeypatch):
        """Step 14 fails one document on purpose; naming it as a root cause would
        misattribute a later step's failure. Asserted here because the pagination
        change moved the code around this filter."""
        probe_input = json.dumps(
            {"document": {"config_version": cbd._HOOK_FAIL_CONFIG_VERSION}}
        )
        sfn = _FakeSfn(_caught_failure_history(), execution_input=probe_input)
        assert _details(cbd, monkeypatch, sfn) == []
        assert sfn.history_calls == []

    def test_a_stack_with_no_state_machine_reports_nothing(self, cbd, monkeypatch):
        sfn = _FakeSfn(_caught_failure_history())
        details = _details(cbd, monkeypatch, sfn, cfn=_FakeCfn(state_machine_arn=""))
        assert details == []
