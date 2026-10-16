"""Unit tests for queue_processor.check_circuit_breaker()."""

import importlib.util
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from moto import mock_aws

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_index_under_test"


@pytest.fixture
def index_module(monkeypatch):
    """Import index with idp_common + boto3 mocked out."""
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
        "MAX_CONCURRENT": "5",
        "CIRCUIT_BREAKER_ENABLED": "true",
        "DOCUMENT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/test-queue",
        "RECOVERY_TIMEOUT_SECONDS": "300",
    }

    fake_idp_common = MagicMock()
    fake_models = MagicMock()
    fake_models.Document = MagicMock()
    fake_models.Status = MagicMock()
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_config = MagicMock()

    fake_xray_core = MagicMock()
    fake_xray_core.xray_recorder = MagicMock()
    fake_xray_core.patch_all = MagicMock()

    module_patches = {
        "idp_common": fake_idp_common,
        "idp_common.models": fake_models,
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": fake_config,
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": fake_xray_core,
    }
    for name, mod in module_patches.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with patch.dict(os.environ, env_vars, clear=False), \
         patch("boto3.resource") as mock_resource, \
         patch("boto3.client") as mock_client:
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_client.return_value = MagicMock()

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.concurrency_table = mock_table
        module.sqs = MagicMock()
        yield module
        sys.modules.pop(_MODULE_NAME, None)


@pytest.fixture
def ddb_module(index_module, monkeypatch):
    """``index_module`` with a REAL DynamoDB table behind ``concurrency_table``.

    The probe budget is enforced entirely by DynamoDB's evaluation of a
    ``ConditionExpression``, so a ``MagicMock`` table proves nothing about it:
    every conditional write "succeeds" against a mock, which is exactly the
    unconditional admission being fixed. ``moto`` is used because it evaluates
    the expressions for real and raises a real
    ``ConditionalCheckFailedException`` — a hand-rolled fake would be asserting
    this suite's own reading of the conditions back at itself.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        # A Session, not the module-level ``boto3.resource``: the
        # ``index_module`` fixture has that one patched for the duration, so
        # calling it here hands back the MagicMock whose conditional writes all
        # "succeed" — the very thing this fixture exists to replace.
        session = boto3.session.Session(region_name="us-east-1")
        table = session.resource("dynamodb").create_table(
            TableName="test-concurrency",
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "counter_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        index_module.concurrency_table = table
        index_module.CIRCUIT_BREAKER_ENABLED = True
        yield index_module


def _put_breaker(module, **attrs) -> None:
    """Write the breaker item the circuit-breaker manager would have written."""
    module.concurrency_table.put_item(
        Item={"counter_id": module.CIRCUIT_BREAKER_ID, **attrs}
    )


def _breaker_item(module) -> dict:
    response = module.concurrency_table.get_item(
        Key={"counter_id": module.CIRCUIT_BREAKER_ID}
    )
    return response.get("Item", {})


def _probes_used(module) -> int:
    item = _breaker_item(module)
    return int(item.get(module.HALF_OPEN_PROBE_COUNT_ATTR, 0))


class TestCheckCircuitBreaker:
    """Covers all branches of check_circuit_breaker()."""

    def test_disabled_returns_allowed(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = False
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "DISABLED"
        index_module.concurrency_table.get_item.assert_not_called()

    def test_item_missing_returns_closed(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {}
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "CLOSED"

    def test_state_open_blocks(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == "OPEN"

    def test_state_half_open_claims_a_probe_slot_before_admitting(self, index_module):
        """HALF_OPEN must not admit without claiming — the claim IS the bound.

        Against a mock table the claim always succeeds, so this asserts the
        attempt rather than the outcome; ``TestHalfOpenProbeBudget`` asserts
        the outcome against a table that can refuse.
        """
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN"}
        }
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "HALF_OPEN"
        assert index_module.concurrency_table.update_item.called, (
            "HALF_OPEN returned True without attempting to claim a probe slot, "
            "which is the unconditional admission of issue #934 item 4b"
        )


class TestHalfOpenProbeBudget:
    """HALF_OPEN admits a BOUNDED number of probes (#934 item 4b).

    Both directions are asserted throughout. A gate that refuses in HALF_OPEN
    unconditionally is as broken as one that admits unconditionally, and it is
    the easier of the two to ship by accident: a suite written only around the
    refusal passes on it.
    """

    def test_budget_remaining_admits_and_records_the_probe(self, ddb_module):
        _put_breaker(ddb_module, state="HALF_OPEN")
        allowed, state = ddb_module.check_circuit_breaker()
        assert allowed is True, "the first probe of a window must be admitted"
        assert state == "HALF_OPEN"
        assert _probes_used(ddb_module) == 1

    def test_budget_exhausted_refuses(self, ddb_module):
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            **{
                ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR: ddb_module._probe_window(),
                ddb_module.HALF_OPEN_PROBE_COUNT_ATTR: (
                    ddb_module.HALF_OPEN_PROBE_BUDGET
                ),
            },
        )
        allowed, state = ddb_module.check_circuit_breaker()
        assert allowed is False, (
            "HALF_OPEN with its probe budget spent must refuse; admitting here "
            "is what let a sustained outage run at full throughput"
        )
        assert state == "HALF_OPEN", (
            "the refusal must still report HALF_OPEN — the caller distinguishes "
            "states, and a refused probe is not an error"
        )
        assert _probes_used(ddb_module) == ddb_module.HALF_OPEN_PROBE_BUDGET, (
            "a refused claim must not increment the counter"
        )

    def test_a_count_already_above_the_budget_still_refuses(self, ddb_module):
        """The cap has to be "fewer than the budget", not "not the budget".

        A count above the budget is reachable without anything going wrong:
        lower ``HALF_OPEN_PROBE_BUDGET`` in a release and any window in flight
        across the deploy carries a count the new budget has already passed. A
        ``!=`` comparison admits in exactly that state and keeps admitting,
        incrementing away from the bound it was supposed to enforce.
        """
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            **{
                ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR: ddb_module._probe_window(),
                ddb_module.HALF_OPEN_PROBE_COUNT_ATTR: (
                    ddb_module.HALF_OPEN_PROBE_BUDGET + 3
                ),
            },
        )
        allowed, _ = ddb_module.check_circuit_breaker()
        assert allowed is False
        assert _probes_used(ddb_module) == ddb_module.HALF_OPEN_PROBE_BUDGET + 3

    # Lopsided on purpose: 1 and 2 are not each other's mirror image, so an
    # inverted or off-by-one comparison cannot map the fixture to itself.
    @pytest.mark.parametrize("budget", [1, 2])
    def test_exactly_the_budget_is_admitted_then_every_further_probe_refused(
        self, ddb_module, monkeypatch, budget
    ):
        monkeypatch.setattr(ddb_module, "HALF_OPEN_PROBE_BUDGET", budget)
        _put_breaker(ddb_module, state="HALF_OPEN")

        verdicts = [ddb_module.check_circuit_breaker()[0] for _ in range(budget + 3)]

        assert verdicts[:budget] == [True] * budget, (
            f"the first {budget} probe(s) must be admitted, got {verdicts}"
        )
        assert verdicts[budget:] == [False] * 3, (
            f"every probe past the budget of {budget} must be refused, got {verdicts}"
        )
        assert _probes_used(ddb_module) == budget

    def test_the_budget_replenishes_in_the_next_window(self, ddb_module, monkeypatch):
        """Without this the fix would deadlock the breaker permanently.

        Nothing moves HALF_OPEN back to OPEN — the manager's health check only
        promotes OPEN -> HALF_OPEN, and its alarm handler needs a fresh ALARM
        transition, which cannot arrive while the alarm is already in ALARM —
        and HALF_OPEN -> CLOSED needs a workflow to succeed. So a budget that
        never replenished would refuse every document for good once spent, and
        the service recovering would change nothing.
        """
        monkeypatch.setattr(ddb_module, "HALF_OPEN_PROBE_BUDGET", 1)
        _put_breaker(ddb_module, state="HALF_OPEN")
        assert ddb_module.check_circuit_breaker()[0] is True
        assert ddb_module.check_circuit_breaker()[0] is False

        # Advance only the handler's view of the clock. Patching the real
        # ``time`` module would also move it for moto and for the thread
        # machinery the concurrency test relies on.
        later = time.time() + ddb_module.HALF_OPEN_PROBE_WINDOW_SECONDS
        monkeypatch.setattr(
            ddb_module, "time", SimpleNamespace(time=lambda: later), raising=True
        )
        assert ddb_module.check_circuit_breaker()[0] is True, (
            "a new window must hand out a fresh budget"
        )
        assert _probes_used(ddb_module) == 1, "the new window restarts the count"

    def test_a_lagging_clock_cannot_pull_the_window_backwards(
        self, ddb_module, monkeypatch
    ):
        """Two Lambda instances need not agree on the time to the second.

        If an invocation whose clock is behind could reset the recorded window,
        it would hand out a second full budget inside one real window. The
        conditional write compares with ``<``, not ``<>``, so it cannot.
        """
        monkeypatch.setattr(ddb_module, "HALF_OPEN_PROBE_BUDGET", 1)
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            **{
                # A window one tick AHEAD of this invocation's clock, already full.
                ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR: ddb_module._probe_window() + 1,
                ddb_module.HALF_OPEN_PROBE_COUNT_ATTR: 1,
            },
        )
        allowed, _ = ddb_module.check_circuit_breaker()
        assert allowed is False
        assert (
            int(_breaker_item(ddb_module)[ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR])
            == ddb_module._probe_window() + 1
        ), "the recorded window must not have been rewound"

    def test_no_slot_is_claimed_once_the_state_has_moved_on(
        self, ddb_module, monkeypatch
    ):
        """The claim is conditional on the state too, not only on the count.

        The state read and the claim are two round trips, so the breaker can
        re-OPEN in between. Admitting then would walk work into an outage that
        had just been re-detected.
        """
        _put_breaker(ddb_module, state="OPEN")
        real_get_item = ddb_module.concurrency_table.get_item
        monkeypatch.setattr(
            ddb_module.concurrency_table,
            "get_item",
            lambda **kwargs: {"Item": {"state": "HALF_OPEN"}},
        )
        allowed, state = ddb_module.check_circuit_breaker()
        assert allowed is False
        assert state == "HALF_OPEN"
        monkeypatch.setattr(ddb_module.concurrency_table, "get_item", real_get_item)
        assert ddb_module.HALF_OPEN_PROBE_COUNT_ATTR not in _breaker_item(ddb_module)

    @pytest.mark.parametrize("budget,callers", [(1, 4), (2, 5)])
    def test_concurrent_invocations_cannot_exceed_the_budget(
        self, ddb_module, monkeypatch, budget, callers
    ):
        """The race the budget exists to survive, driven rather than described.

        This handler runs with many concurrent invocations, so the defect to
        avoid is read-then-write: each invocation reads "no slots used" and all
        proceed. A barrier on the state read holds every caller there until all
        of them have read, so none can have seen another's write — which is the
        interleaving a sequence of calls never produces. Only then are the
        claims released. A ``get``-then-``put`` implementation admits all
        ``callers`` here; a conditional write admits exactly ``budget``.

        ⚠️ The ``UpdateItem`` calls are serialized by a lock, and that is
        modelling DynamoDB rather than weakening the test. DynamoDB evaluates a
        ``ConditionExpression`` and applies its update as one serialized
        operation per item; ``moto`` does not — it read-modify-writes its
        backing dict with no lock, so without this two concurrent claims can
        both pass ``attribute_not_exists`` and this test measures ``moto``'s
        thread safety instead of the handler's atomicity. Measured: it admits
        3 probes against a budget of 2 unserialized. The barrier is what
        creates the race; the lock is what makes the store behave like the one
        this code runs against.
        """
        monkeypatch.setattr(ddb_module, "HALF_OPEN_PROBE_BUDGET", budget)
        _put_breaker(ddb_module, state="HALF_OPEN")

        all_have_read = threading.Barrier(callers, timeout=30)
        real_get_item = ddb_module.concurrency_table.get_item
        real_update_item = ddb_module.concurrency_table.update_item
        item_lock = threading.Lock()

        def get_item_then_wait(**kwargs):
            response = real_get_item(**kwargs)
            all_have_read.wait()
            return response

        def update_item_serialized(**kwargs):
            with item_lock:
                return real_update_item(**kwargs)

        monkeypatch.setattr(
            ddb_module.concurrency_table, "get_item", get_item_then_wait
        )
        monkeypatch.setattr(
            ddb_module.concurrency_table, "update_item", update_item_serialized
        )

        verdicts: list[bool] = []
        verdicts_lock = threading.Lock()

        def invoke():
            allowed, _ = ddb_module.check_circuit_breaker()
            with verdicts_lock:
                verdicts.append(allowed)

        threads = [threading.Thread(target=invoke) for _ in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a caller never returned"

        monkeypatch.setattr(ddb_module.concurrency_table, "get_item", real_get_item)
        monkeypatch.setattr(
            ddb_module.concurrency_table, "update_item", real_update_item
        )
        assert len(verdicts) == callers
        assert sum(verdicts) == budget, (
            f"{callers} invocations that all read the state before any of them "
            f"wrote admitted {sum(verdicts)} probes against a budget of "
            f"{budget}; the claim is not atomic"
        )
        assert _probes_used(ddb_module) == budget, (
            "the persisted count must match what was handed out, or a later "
            "window inherits a wrong starting point"
        )

    def test_a_transient_fault_on_the_claim_refuses(self, ddb_module, monkeypatch):
        """A failed claim is classified by the same split #1025 settled.

        The claim is a second DynamoDB round trip and so a second place the
        table can fail. Routing it through the existing classifier is what
        keeps a throttle from reading as "no budget, carry on" — or worse, as
        an admission.
        """
        _put_breaker(ddb_module, state="HALF_OPEN")
        monkeypatch.setattr(
            ddb_module.concurrency_table,
            "update_item",
            _raiser(_ddb_error("ThrottlingException", "UpdateItem")),
        )
        with patch("boto3.client"):
            allowed, state = ddb_module.check_circuit_breaker()
        assert allowed is False
        assert state == ddb_module.CB_STATE_ERROR_TRANSIENT

    def test_a_terminal_fault_on_the_claim_admits(self, ddb_module, monkeypatch):
        """Same reasoning as the state read: a permissions problem cannot be
        retried away, so refusing would drain the backlog into the DLQ."""
        _put_breaker(ddb_module, state="HALF_OPEN")
        monkeypatch.setattr(
            ddb_module.concurrency_table,
            "update_item",
            _raiser(_ddb_error("AccessDeniedException", "UpdateItem")),
        )
        with patch("boto3.client"):
            allowed, state = ddb_module.check_circuit_breaker()
        assert allowed is True
        assert state == ddb_module.CB_STATE_ERROR_TERMINAL

    def test_a_disabled_breaker_claims_nothing(self, ddb_module):
        """The blast radius stays opt-in: CircuitBreakerEnabled defaults to
        "false", and on that default the table is never touched."""
        ddb_module.CIRCUIT_BREAKER_ENABLED = False
        _put_breaker(ddb_module, state="HALF_OPEN")
        assert ddb_module.check_circuit_breaker() == (True, "DISABLED")
        assert ddb_module.HALF_OPEN_PROBE_COUNT_ATTR not in _breaker_item(ddb_module)

    @pytest.mark.parametrize("state", ["CLOSED", "OPEN"])
    def test_no_other_state_touches_the_probe_counter(self, ddb_module, state):
        """CLOSED and OPEN must not consume or create probe state — otherwise
        a half-open episode starts from a count somebody else moved."""
        _put_breaker(ddb_module, state=state)
        ddb_module.check_circuit_breaker()
        item = _breaker_item(ddb_module)
        assert ddb_module.HALF_OPEN_PROBE_COUNT_ATTR not in item
        assert ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR not in item

    def test_the_budget_is_a_bound_and_not_a_switch(self, ddb_module):
        """Pin the range the two numbers have to stay inside.

        Below the floor the breaker deadlocks: a budget of zero refuses every
        probe, so nothing can ever succeed and nothing can ever close the
        breaker again. Above the ceiling it stops being a bound: a probe budget
        is a count of recovery probes, and a value in the tens is a throughput
        setting wearing the name — the number that made this a no-op gate in
        the first place was effectively unbounded.
        """
        assert ddb_module.HALF_OPEN_PROBE_BUDGET >= 1, (
            "a budget below 1 admits no probes at all, so nothing can ever "
            "close the breaker again"
        )
        assert ddb_module.HALF_OPEN_PROBE_BUDGET <= 10, (
            "a budget in the tens is a throughput setting, not a probe count; "
            "raise it only with a measurement that says recovery detection "
            "needs more than a handful of probes per window"
        )
        # The window needs a range too, and for symmetrical reasons. Short
        # enough and the same budget is handed out so often that it stops
        # being a bound — at a one-second window this gate admits five
        # workflows a second. Long enough and the probe cadence is slower than
        # the mechanism that produced the half-open state in the first place,
        # so the breaker lingers there for reasons that have nothing to do
        # with the dependency it is testing.
        assert 10 <= ddb_module.HALF_OPEN_PROBE_WINDOW_SECONDS <= (
            ddb_module.RECOVERY_TIMEOUT_SECONDS
        ), (
            "the probe window must bound the admission RATE and must not be "
            "slower than the breaker's own recovery timeout"
        )
        assert isinstance(ddb_module._probe_window(), int)

    @pytest.mark.parametrize(
        "claim", ["_take_probe_slot", "_open_probe_window"]
    )
    def test_a_non_conditional_fault_propagates_out_of_each_claim(
        self, ddb_module, monkeypatch, claim
    ):
        """Each helper must re-raise what is not a conditional-check failure.

        Swallowing it as "no slot" would be wrong in the terminal direction:
        an access-denied on ``UpdateItem`` would read as a spent budget and
        refuse every message for as long as the misconfiguration lasted, which
        is the dead-letter-queue drain #1025 rejected. Asserted per helper,
        because with only the end-to-end check in place one of the two can
        swallow it and the other still raise, leaving the overall verdict right
        by accident.
        """
        monkeypatch.setattr(
            ddb_module.concurrency_table,
            "update_item",
            _raiser(_ddb_error("AccessDeniedException", "UpdateItem")),
        )
        with pytest.raises(ClientError):
            getattr(ddb_module, claim)(ddb_module._probe_window())

    @pytest.mark.parametrize("budget,expected", [(1, False), (2, True)])
    def test_losing_the_open_window_race_retries_for_a_remaining_slot(
        self, ddb_module, monkeypatch, budget, expected
    ):
        """Losing the race to open a window must not refuse a free slot.

        The winner of that race consumes exactly one slot, so with a budget
        above 1 there is still room and refusing would throttle below the
        configured budget. The barrier-driven concurrency test above cannot
        pin this — whether a caller reaches its claim before or after the
        winner's write is a matter of thread timing — so the race is staged:
        the window is opened underneath this caller and the attempt is then
        told it lost.

        Lopsided parameters on purpose. Budget 1 must still refuse, which is
        what stops the retry from becoming a second, unbudgeted slot.
        """
        monkeypatch.setattr(ddb_module, "HALF_OPEN_PROBE_BUDGET", budget)
        _put_breaker(ddb_module, state="HALF_OPEN")
        real_open = ddb_module._open_probe_window

        def open_then_lose(window: int) -> bool:
            real_open(window)  # a concurrent caller got there first
            return False

        monkeypatch.setattr(ddb_module, "_open_probe_window", open_then_lose)
        assert ddb_module._claim_half_open_probe() is expected
        assert _probes_used(ddb_module) == (2 if expected else 1)


def _raiser(exc: Exception):
    def _raise(**kwargs):
        raise exc

    return _raise


def _ddb_error(code: str, operation: str = "GetItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


class TestCheckCircuitBreakerStateReadFailures:
    """A failed state read is judged by whether a retry can fix it (#934 item 4).

    Transient -> REFUSE admission: the message goes back on DocumentQueue, whose
    500 x 60 s redrive budget absorbs roughly 8 hours of this, so a blip cannot
    reach the dead-letter queue. Terminal -> ALLOW admission: it cannot be
    retried away, so refusing would send the whole backlog to the DLQ for a
    misconfiguration while the pipeline is otherwise healthy.
    """

    # Codes DynamoDB really returns, one per class. The transient ones are
    # classified by idp_common.utils.transient_errors, which the conftest loads
    # for real precisely so this split is not asserted against a mock's opinion.
    TRANSIENT_CODES = (
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "InternalServerError",
        "RequestLimitExceeded",
    )
    TERMINAL_CODES = (
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ValidationException",
        "UnrecognizedClientException",
    )

    @pytest.mark.parametrize("code", TRANSIENT_CODES)
    def test_transient_ddb_error_refuses_admission(self, index_module, code):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(code)
        with patch("boto3.client") as mock_client:
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False, (
            f"{code} is retryable, so admission must be refused rather than "
            f"letting a coincident DynamoDB fault switch the breaker off"
        )
        assert state == index_module.CB_STATE_ERROR_TRANSIENT
        _assert_failure_metric(mock_client, "TRANSIENT")

    @pytest.mark.parametrize("code", TERMINAL_CODES)
    def test_terminal_ddb_error_admits_but_reports(self, index_module, code):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(code)
        with patch("boto3.client") as mock_client:
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is True, (
            f"{code} cannot be retried away; refusing every message would drain "
            f"the backlog into the dead-letter queue"
        )
        assert state == index_module.CB_STATE_ERROR_TERMINAL
        _assert_failure_metric(mock_client, "TERMINAL")

    def test_network_failure_is_transient(self, index_module):
        """BotoCoreError bypasses ClientError and carries no error code."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.example"
        )
        with patch("boto3.client"):
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == index_module.CB_STATE_ERROR_TRANSIENT

    def test_unclassifiable_failure_is_treated_as_terminal(self, index_module):
        """An error the classifier has no verdict for must not stall the queue."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = BotoCoreError()
        with patch("boto3.client"):
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == index_module.CB_STATE_ERROR_TERMINAL

    def test_metric_failure_does_not_change_the_decision(self, index_module):
        """Telemetry is best effort; it must not raise out of the check."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "ThrottlingException"
        )
        with patch("boto3.client") as mock_client:
            mock_client.return_value.put_metric_data.side_effect = RuntimeError("no")
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == index_module.CB_STATE_ERROR_TRANSIENT

    def test_disabled_breaker_never_reads_the_table(self, index_module):
        """The blast radius of everything above is opt-in deployments only:
        CircuitBreakerEnabled defaults to "false" in template.yaml, and on that
        default the table is never touched, so no DynamoDB fault can be
        classified either way."""
        index_module.CIRCUIT_BREAKER_ENABLED = False
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "AccessDeniedException"
        )
        allowed, state = index_module.check_circuit_breaker()
        assert (allowed, state) == (True, index_module.CB_STATE_DISABLED)
        index_module.concurrency_table.get_item.assert_not_called()


def _assert_failure_metric(mock_client: MagicMock, classification: str) -> None:
    """The degraded read must be visible in CloudWatch, with which half it was."""
    mock_client.assert_any_call("cloudwatch")
    calls = mock_client.return_value.put_metric_data.call_args_list
    assert len(calls) == 1, f"expected one PutMetricData call, got {len(calls)}"
    datum = calls[0].kwargs["MetricData"][0]
    assert datum["MetricName"] == "CircuitBreakerCheckFailed"
    assert datum["Dimensions"] == [{"Name": "Classification", "Value": classification}]


class TestCircuitBreakerAdmissionTable:
    """Guard against the admission decision being inverted, or a new state
    silently inheriting whichever branch it happens to fall into.

    The table is the contract. It is compared against every ``CB_STATE_*``
    constant the module defines, in both directions, so adding a state without
    deciding what it admits fails here rather than at 3am.
    """

    #: state -> may the queue processor start a workflow in it? ``"BOUNDED"``
    #: means the answer depends on the probe budget and BOTH halves are
    #: asserted below, because "HALF_OPEN: True" is what the unconditional
    #: admission of #934 item 4b looked like from here.
    EXPECTED_ADMISSION = {
        "CLOSED": True,
        "OPEN": False,
        "HALF_OPEN": "BOUNDED",
        "DISABLED": True,
        "ERROR_TRANSIENT": False,
        "ERROR_TERMINAL": True,
    }

    def test_every_declared_state_is_in_the_table(self, index_module):
        declared = {
            value
            for name, value in vars(index_module).items()
            if name.startswith("CB_STATE_") and isinstance(value, str)
        }
        assert declared == set(self.EXPECTED_ADMISSION), (
            "a circuit-breaker state was added or renamed without deciding "
            "whether it admits work; add it to EXPECTED_ADMISSION"
        )

    @pytest.mark.parametrize(
        "state,expected",
        sorted(
            (s, e) for s, e in EXPECTED_ADMISSION.items() if e != "BOUNDED"
        ),
    )
    def test_admission_matches_the_table(self, index_module, state, expected):
        index_module.CIRCUIT_BREAKER_ENABLED = state != "DISABLED"
        if state == "ERROR_TRANSIENT":
            index_module.concurrency_table.get_item.side_effect = _ddb_error(
                "ThrottlingException"
            )
        elif state == "ERROR_TERMINAL":
            index_module.concurrency_table.get_item.side_effect = _ddb_error(
                "AccessDeniedException"
            )
        else:
            index_module.concurrency_table.get_item.return_value = {
                "Item": {"state": state}
            }

        with patch("boto3.client"):
            allowed, reported = index_module.check_circuit_breaker()

        assert allowed is expected, (
            f"circuit breaker state {state} must "
            f"{'allow' if expected else 'block'} new workflows"
        )
        assert reported == state

    @pytest.mark.parametrize("claim_succeeds,expected", [(True, True), (False, False)])
    def test_the_bounded_state_admits_only_when_a_slot_is_claimed(
        self, index_module, claim_succeeds, expected
    ):
        """HALF_OPEN's entry is ``"BOUNDED"``, and this is what that means.

        Parametrised over both halves deliberately: the table above cannot
        express "it depends", and a single-valued entry here is precisely how
        the unconditional admission survived — ``True`` described the broken
        behaviour perfectly.
        """
        bounded = [s for s, e in self.EXPECTED_ADMISSION.items() if e == "BOUNDED"]
        assert bounded == ["HALF_OPEN"], (
            "a state was marked BOUNDED without a rule saying what bounds it"
        )
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN"}
        }
        if not claim_succeeds:
            index_module.concurrency_table.update_item.side_effect = _ddb_error(
                "ConditionalCheckFailedException", "UpdateItem"
            )
        with patch("boto3.client"):
            allowed, reported = index_module.check_circuit_breaker()
        assert allowed is expected
        assert reported == "HALF_OPEN", (
            "a refused probe is a breaker decision, not a read failure, so it "
            "must not be reported as one of the ERROR_* states"
        )


class TestCallerHonoursTheRefusal:
    """A decision nobody acts on is the defect this whole area keeps producing,
    so assert the refusal reaches SQS rather than only the return value."""

    @staticmethod
    def _record() -> dict:
        return {
            "body": json.dumps({"id": "doc-1"}),
            "messageId": "msg-1",
            "receiptHandle": "receipt-1",
        }

    def test_transient_read_failure_starts_no_workflow(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.sfn = MagicMock()
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "ThrottlingException"
        )
        with patch("boto3.client"):
            success, message_id = index_module.process_message(self._record())

        assert success is False, "the message must be reported as a batch failure"
        assert message_id == "msg-1"
        index_module.sfn.start_execution.assert_not_called()
        index_module.concurrency_table.update_item.assert_not_called()
        # Only OPEN extends visibility: a transient read failure should be gone
        # by the next 60 s delivery, and a 300 s push would add five minutes of
        # latency per document for a fault that lasted milliseconds.
        index_module.sqs.change_message_visibility.assert_not_called()
        # Nothing was acked, so the message stays on DocumentQueue.
        index_module.sqs.delete_message.assert_not_called()

    def test_a_refused_probe_starts_no_workflow_and_keeps_its_60s_retry(
        self, ddb_module
    ):
        """The refusal has to reach Step Functions and SQS, not just the tuple.

        Visibility is deliberately NOT extended here. The next probe window is
        at most ``HALF_OPEN_PROBE_WINDOW_SECONDS`` away, which the queue's 60 s
        visibility timeout already matches; pushing the message out to
        ``RECOVERY_TIMEOUT_SECONDS`` the way an OPEN refusal does would
        throttle probes to one every five minutes and make recovery detection
        slower than the budget allows for.
        """
        ddb_module.sfn = MagicMock()
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            **{
                ddb_module.HALF_OPEN_PROBE_WINDOW_ATTR: ddb_module._probe_window(),
                ddb_module.HALF_OPEN_PROBE_COUNT_ATTR: (
                    ddb_module.HALF_OPEN_PROBE_BUDGET
                ),
            },
        )
        with patch("boto3.client"):
            success, message_id = ddb_module.process_message(self._record())

        assert success is False, "the message must be reported as a batch failure"
        assert message_id == "msg-1"
        ddb_module.sfn.start_execution.assert_not_called()
        ddb_module.sqs.change_message_visibility.assert_not_called()
        ddb_module.sqs.delete_message.assert_not_called()
        assert _probes_used(ddb_module) == ddb_module.HALF_OPEN_PROBE_BUDGET, (
            "a refused message must not have consumed a probe slot"
        )

    def test_an_open_refusal_extends_visibility_to_the_recovery_timeout(
        self, ddb_module
    ):
        """The other side of the test above, and what reopening buys (#1437).

        The two refusals are deliberately priced differently: a refused probe
        keeps the queue's 60 s visibility because the next probe window is
        seconds away, while a refused document in OPEN is pushed out to
        ``RECOVERY_TIMEOUT_SECONDS`` because the pause is expected to last that
        long. Against ``maxReceiveCount`` 500 that is the difference between
        spending the redrive budget in roughly 8 hours and roughly 41, so the
        state a sustained outage settles in decides how soon documents reach
        the dead-letter queue. The circuit-breaker manager's
        ``handle_half_open_timeout`` is what puts a failed half-open episode
        back into this branch.
        """
        ddb_module.sfn = MagicMock()
        _put_breaker(ddb_module, state="OPEN")
        with patch("boto3.client"):
            success, message_id = ddb_module.process_message(self._record())

        assert success is False, "the message must be reported as a batch failure"
        assert message_id == "msg-1"
        ddb_module.sfn.start_execution.assert_not_called()
        ddb_module.sqs.delete_message.assert_not_called()
        ddb_module.sqs.change_message_visibility.assert_called_once()
        kwargs = ddb_module.sqs.change_message_visibility.call_args.kwargs
        assert kwargs["VisibilityTimeout"] == ddb_module.RECOVERY_TIMEOUT_SECONDS


class TestExtendVisibilityForOutage:
    """OPEN-state messages should have their SQS visibility pushed out so
    maxReceiveCount isn't burned during a long Bedrock outage."""

    def test_extends_visibility_to_recovery_timeout(self, index_module):
        index_module.extend_visibility_for_outage("receipt-abc")
        index_module.sqs.change_message_visibility.assert_called_once_with(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/test-queue",
            ReceiptHandle="receipt-abc",
            VisibilityTimeout=300,
        )

    def test_noop_when_queue_url_missing(self, index_module):
        index_module.DOCUMENT_QUEUE_URL = ""
        index_module.extend_visibility_for_outage("receipt-abc")
        index_module.sqs.change_message_visibility.assert_not_called()

    def test_sqs_failure_is_non_fatal(self, index_module):
        """A failed visibility update should be logged, not raised - the
        message just retries on the default 30s timeout."""
        index_module.sqs.change_message_visibility.side_effect = ClientError(
            {"Error": {"Code": "ReceiptHandleIsInvalid", "Message": "nope"}},
            "ChangeMessageVisibility",
        )
        index_module.extend_visibility_for_outage("receipt-abc")
