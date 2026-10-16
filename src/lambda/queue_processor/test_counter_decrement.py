# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The compensating decrement has to actually reach DynamoDB (issue #1358).

``update_counter`` built one ``update_item`` argument dict for both directions and
always put ``:max`` in ``ExpressionAttributeValues``, while only the increment
path's ``ConditionExpression`` referenced it. DynamoDB rejects a request carrying
an expression value no expression mentions, so **every decrement** failed with
``ValidationException: ... unused in expressions: keys: {:max}``. Both
compensating-decrement paths in ``process_message`` catch and log that, so each
one leaked a slot: reported live as a duplicate EventBridge delivery walking
``active_count`` up to ``MaxConcurrentWorkflows`` one SQS redelivery at a time
until the stack refused every new document.

Nothing caught it, and the reason is the shape of the existing coverage rather
than a missing case:

* ``test_idempotent_start.py`` and ``test_slot_ownership.py`` drive the
  compensating-decrement paths, but replace ``update_counter`` with a
  ``MagicMock``, so they assert that it was *called* with ``increment=False`` —
  which it was.
* ``test_reconcile_counter.py`` exercises the real ``update_counter``, but its
  mocked-table tests hand ``update_item`` a ``MagicMock``, which accepts any
  arguments, and its moto-backed tests only ever increment.

So these tests use a real (moto) table AND the real ``update_counter``, and
assert the value the table is left holding. ``ADD active_count :inc`` is the one
operation whose outcome a mock cannot stand in for.

The decrement's **floor** is pinned here for the same reason. It is the other
half of the repair rather than a separate change: an unconditional
``ADD active_count -1`` that now lands can drive the counter NEGATIVE, and
because admission is ``active_count < MaxConcurrentWorkflows`` every unit below
zero permanently raises the effective ceiling — the mirror-image outage, and the
one the tracker's decrement already guards against (issue #915).
"""

import importlib.util
import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

#: Applied at module scope rather than per class. These are hermetic unit tests —
#: moto stands in for DynamoDB and every other client is a mock — and without the
#: marker a `pytest -m unit` selection runs none of them, which is the one
#: invocation shape the repository's testing standard names.
pytestmark = pytest.mark.unit

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_counter_decrement_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test-Workflow"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/test-DocumentQueue"
MESSAGE_ID = "9c1b7a44-1e2f-4d3a-8b5c-0a6e7f8d9c01"

#: The deployed default. The ceiling tests drive the counter to it directly
#: rather than the suite lowering it, so every other test here sits well below
#: it — an increment refused by the ceiling diverts ``process_message`` into
#: ``reconcile_counter``, which is a different subject entirely.
MAX_CONCURRENT = 100


def _already_exists() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
        "StartExecution",
    )


@pytest.fixture
def index_module(monkeypatch):
    """The handler with a real DynamoDB table behind it and a real
    ``update_counter``. Step Functions, SQS and the document service stay mocked:
    the subject here is the counter, not the workflow."""
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": SM_ARN,
        "MAX_CONCURRENT": str(MAX_CONCURRENT),
        "METRIC_NAMESPACE": "TestStack",
        "DOCUMENT_QUEUE_URL": QUEUE_URL,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_xray_core = MagicMock()
    fake_xray_core.xray_recorder.capture.return_value = lambda fn: fn
    for name, mod in {
        "idp_common": MagicMock(),
        "idp_common.models": MagicMock(),
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": MagicMock(),
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": fake_xray_core,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # WORKING_BUCKET is removed rather than merely left unset: `start_workflow`
    # branches on it, so a host that happens to export one sends these tests down
    # the serialize-to-S3 path instead of the inline one. `clear=False` inherits
    # whatever the machine has, which is the shape of a suite that passes in CI
    # and fails on somebody's laptop.
    monkeypatch.delenv("WORKING_BUCKET", raising=False)

    with patch.dict(os.environ, env_vars, clear=False), mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test-concurrency",
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "counter_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        doc = MagicMock()
        doc.input_key = "input/statements/bank statement (march).pdf"
        doc.id = doc.input_key
        doc.config_version = "default"
        doc.config_revision = 3
        doc.workflow_execution_arn = None
        doc.to_dict = MagicMock(return_value={"id": doc.id})
        module.Document.load_document = MagicMock(return_value=doc)
        module.document_service.get_document = MagicMock(return_value=None)
        module.document_service.update_document = MagicMock(return_value=doc)
        module.check_circuit_breaker = MagicMock(return_value=(True, "CLOSED"))
        module.sfn = MagicMock()
        module.sfn.start_execution = MagicMock(
            return_value={
                "executionArn": f"{SM_ARN.replace('stateMachine', 'execution')}:e1"
            }
        )
        module.sqs = MagicMock()
        # A real (empty) page, not a MagicMock: ``_count_running_executions``
        # pages on ``nextToken``, and a MagicMock's attribute is truthy
        # forever, so a default mock turns any path that reaches reconciliation
        # into an infinite loop rather than a failed assertion.
        module.sfn.list_executions = MagicMock(return_value={"executions": []})
        module._doc = doc
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _record(message_id=MESSAGE_ID, receipt="receipt-1"):
    return {
        "messageId": message_id,
        "receiptHandle": receipt,
        "body": json.dumps(
            {"input_key": "input/statements/bank statement (march).pdf"}
        ),
    }


def _active(module):
    item = module.concurrency_table.get_item(
        Key={"counter_id": module.COUNTER_ID}, ConsistentRead=True
    ).get("Item")
    return None if item is None else int(item["active_count"])


def _set_active(module, value):
    module.concurrency_table.put_item(
        Item={"counter_id": module.COUNTER_ID, "active_count": value}
    )


def _record_update_items(module):
    """Capture every ``update_item`` request while still issuing it for real.

    A proxy rather than a ``MagicMock`` with a ``side_effect``: ``get_item`` has
    to keep reaching the real table, or the assertions about the stored value
    would be reading a mock back.
    """
    real_table = module.concurrency_table
    calls = []

    class _Recorder:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def update_item(self, **kwargs):
            calls.append(kwargs)
            return real_table.update_item(**kwargs)

    module.concurrency_table = _Recorder()
    return calls


class TestDecrementReachesTheTable:
    def test_a_decrement_lowers_the_persisted_counter(self, index_module):
        _set_active(index_module, 5)

        assert index_module.update_counter(increment=False) is True

        assert _active(index_module) == 4

    def test_a_decrement_does_not_raise(self, index_module):
        """The regression itself: this raised ``ValidationException`` for every
        decrement, which both callers in ``process_message`` swallow — so the
        slot was leaked with nothing failing."""
        _set_active(index_module, 1)

        index_module.update_counter(increment=False)  # must not raise

    def test_increment_then_decrement_is_a_round_trip(self, index_module):
        _set_active(index_module, 2)

        assert index_module.update_counter(increment=True) is True
        assert index_module.update_counter(increment=False) is True

        assert _active(index_module) == 2


class TestTheDecrementHasAFloor:
    """The other half of the repair.

    Until the ``:max`` defect was fixed this write never landed, so nothing about
    it was reachable. It lands now, and ``ADD active_count :inc`` is
    unconditional arithmetic — so a hand-back racing a ``reconcile_counter``
    reset, or one the counter has already accounted for, takes it below zero.
    That is the mirror-image outage: admission is
    ``active_count < MaxConcurrentWorkflows``, so a counter at −N runs N
    workflows above the configured ceiling, and only a later increment notices
    (``_repair_if_counter_was_negative``). The tracker's decrement has carried
    this floor since #915; this one now matches it.
    """

    def test_a_decrement_at_zero_is_refused(self, index_module):
        _set_active(index_module, 0)

        assert index_module.update_counter(increment=False) is False

        assert _active(index_module) == 0

    def test_a_refused_decrement_never_persists_a_negative(self, index_module):
        """The property, rather than the mechanism: however many redundant
        hand-backs arrive, the counter does not go below zero."""
        _set_active(index_module, 1)

        assert index_module.update_counter(increment=False) is True
        for _ in range(3):
            assert index_module.update_counter(increment=False) is False

        assert _active(index_module) == 0

    def test_a_negative_counter_is_not_driven_further_down(self, index_module):
        """A counter already negative — from a release before the floor, or from
        the tracker's own path — must not be made worse by a hand-back."""
        _set_active(index_module, -2)

        assert index_module.update_counter(increment=False) is False

        assert _active(index_module) == -2

    def test_the_floor_is_a_condition_and_not_a_read(self, index_module):
        """Compare-and-swap rather than read-then-write: two invocations handing
        back the last slot concurrently would both pass a prior read."""
        _set_active(index_module, 0)
        calls = _record_update_items(index_module)

        assert index_module.update_counter(increment=False) is False

        assert calls[-1]["ConditionExpression"] == "active_count > :zero"
        assert calls[-1]["ExpressionAttributeValues"][":zero"] == 0

    def test_a_refused_decrement_is_not_reported_as_a_full_counter(
        self, index_module, caplog
    ):
        """The two paths now refuse for opposite reasons, and one log line is
        what an operator reads when intake stops. Reporting a counter at its
        FLOOR as a counter at its CEILING points the diagnosis the wrong way."""
        _set_active(index_module, 0)

        with caplog.at_level("WARNING"):
            assert index_module.update_counter(increment=False) is False

        assert "Concurrency limit reached" not in caplog.text
        assert "not above" in caplog.text

    def test_the_increment_still_says_limit_reached(self, index_module, caplog):
        _set_active(index_module, MAX_CONCURRENT)

        with caplog.at_level("WARNING"):
            assert index_module.update_counter(increment=True) is False

        assert "Concurrency limit reached" in caplog.text

    def test_a_refused_decrement_emits_the_underflow_metric(self, index_module):
        """A log line is not a signal. The tracker's identical condition publishes
        ``ConcurrencyCounterUnderflow``, which ``ConcurrencyCounterUnderflowAlarm``
        fires on at the first occurrence — and this path could not occur at all
        before the floor existed, so without the metric the one new refusal in the
        system would be visible only to a log search."""
        _set_active(index_module, 0)
        published = []
        index_module.boto3 = MagicMock()
        index_module.boto3.client.return_value.put_metric_data = lambda **kwargs: (
            published.append(kwargs)
        )

        assert index_module.update_counter(increment=False) is False

        assert len(published) == 1
        datum = published[0]["MetricData"][0]
        assert datum["MetricName"] == "ConcurrencyCounterUnderflow"
        assert (datum["Value"], datum["Unit"]) == (1, "Count")
        assert published[0]["Namespace"] == "TestStack"

    def test_a_successful_decrement_emits_no_underflow_metric(self, index_module):
        _set_active(index_module, 2)
        published = []
        index_module.boto3 = MagicMock()
        index_module.boto3.client.return_value.put_metric_data = lambda **kwargs: (
            published.append(kwargs)
        )

        assert index_module.update_counter(increment=False) is True

        assert published == []

    def test_a_failed_metric_never_changes_the_decrement(self, index_module):
        """Telemetry is never allowed to affect the write it reports on."""
        _set_active(index_module, 0)
        index_module.boto3 = MagicMock()
        index_module.boto3.client.return_value.put_metric_data.side_effect = (
            RuntimeError("cloudwatch down")
        )

        assert index_module.update_counter(increment=False) is False

        assert _active(index_module) == 0


class TestASwallowedFailureStillMoves:
    """The remaining way for a hand-back to be lost with nothing moving.

    Both callers in ``process_message`` catch a failed decrement, log it and carry
    on, which is right — the execution exists, or the message is being retried. It
    is also what left #1358 UNALARMED rather than unlogged: both call sites log at
    ERROR with a traceback, but a ``ValidationException`` on every decrement
    produced no datapoint, so nothing anyone watches moved. The floor's refusal is now visible via
    ``ConcurrencyCounterUnderflow``; this covers the write that RAISES.
    """

    @staticmethod
    def _capture(module):
        published = []
        module.boto3 = MagicMock()
        module.boto3.client.return_value.put_metric_data = lambda **kwargs: (
            published.append(kwargs)
        )
        return published

    @staticmethod
    def _names(published):
        return [d["MetricName"] for p in published for d in p["MetricData"]]

    def test_a_raising_decrement_on_the_duplicate_path_emits_a_metric(
        self, index_module
    ):
        _set_active(index_module, 3)
        index_module.sfn.start_execution.side_effect = _already_exists()
        published = self._capture(index_module)
        index_module.update_counter = MagicMock(
            side_effect=[True, RuntimeError("ddb down")]
        )

        success, _ = index_module.process_message(_record())

        assert success is True, "the duplicate must still be acked"
        assert "ConcurrencyCounterUpdateFailed" in self._names(published)

    def test_a_raising_decrement_on_the_failed_start_path_emits_a_metric(
        self, index_module
    ):
        _set_active(index_module, 3)
        index_module.sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "StartExecution",
        )
        published = self._capture(index_module)
        index_module.update_counter = MagicMock(
            side_effect=[True, RuntimeError("ddb down")]
        )

        success, _ = index_module.process_message(_record())

        assert success is False, "the message must still be retried"
        assert "ConcurrencyCounterUpdateFailed" in self._names(published)

    def test_a_successful_hand_back_emits_no_failure_metric(self, index_module):
        _set_active(index_module, 3)
        index_module.sfn.start_execution.side_effect = _already_exists()
        published = self._capture(index_module)

        assert index_module.process_message(_record())[0] is True

        assert "ConcurrencyCounterUpdateFailed" not in self._names(published)

    def test_a_failed_metric_never_changes_the_outcome(self, index_module):
        """Telemetry on a path that already failed must not fail it differently."""
        _set_active(index_module, 3)
        index_module.sfn.start_execution.side_effect = _already_exists()
        index_module.boto3 = MagicMock()
        index_module.boto3.client.return_value.put_metric_data.side_effect = (
            RuntimeError("cloudwatch down")
        )
        index_module.update_counter = MagicMock(
            side_effect=[True, RuntimeError("ddb down")]
        )

        assert index_module.process_message(_record())[0] is True


class TestTheIncrementStillHasItsCeiling:
    """``:max`` must still be supplied where it IS referenced. Dropping it from
    both paths would also make the decrement work — and would silently remove
    admission control, which is the more expensive defect of the two."""

    def test_an_increment_at_the_ceiling_is_refused(self, index_module):
        _set_active(index_module, MAX_CONCURRENT)

        assert index_module.update_counter(increment=True) is False

        assert _active(index_module) == MAX_CONCURRENT

    def test_an_increment_below_the_ceiling_is_admitted(self, index_module):
        _set_active(index_module, MAX_CONCURRENT - 1)

        assert index_module.update_counter(increment=True) is True

        assert _active(index_module) == MAX_CONCURRENT

    def test_the_condition_is_what_refuses_it(self, index_module):
        """Pinned against the arguments as well as the outcome, because a
        ``ConditionExpression`` that is absent and a counter that happens not to
        move look the same from the stored value alone."""
        _set_active(index_module, MAX_CONCURRENT)
        calls = _record_update_items(index_module)

        assert index_module.update_counter(increment=True) is False

        assert calls[-1]["ConditionExpression"] == "active_count < :max"
        assert calls[-1]["ExpressionAttributeValues"][":max"] == MAX_CONCURRENT


class TestNoValueIsSentThatNoExpressionUses:
    """DynamoDB's rule, asserted on the request rather than inferred from the
    response, so this still fails if the backing store stops enforcing it."""

    _PLACEHOLDER = re.compile(r":[A-Za-z0-9_]+")

    @pytest.mark.parametrize("increment", [True, False])
    def test_every_supplied_placeholder_is_referenced(self, index_module, increment):
        _set_active(index_module, 1)
        calls = _record_update_items(index_module)

        index_module.update_counter(increment=increment)

        assert calls, "update_counter issued no UpdateItem"
        kwargs = calls[-1]

        referenced = set()
        for key in ("UpdateExpression", "ConditionExpression"):
            referenced |= set(self._PLACEHOLDER.findall(kwargs.get(key, "")))
        supplied = set(kwargs["ExpressionAttributeValues"])

        assert supplied - referenced == set(), (
            f"increment={increment} sent expression value(s) no expression "
            f"references; DynamoDB rejects the whole request with "
            f"ValidationException"
        )


class TestTheDuplicateDeliveryIncidentEndToEnd:
    """The reported sequence, through the real entry point.

    One PutObject produced two ``Object Created`` events; the first started the
    workflow and the second arrived after it. The deterministic execution name
    turns that into ``ExecutionAlreadyExists``, and ``process_message`` hands the
    slot back. That hand-back is the write that always failed.
    """

    def test_a_redelivery_leaves_the_counter_where_it_started(self, index_module):
        _set_active(index_module, 7)
        index_module.sfn.start_execution.side_effect = _already_exists()

        success, message_id = index_module.process_message(_record())

        assert (success, message_id) == (True, MESSAGE_ID)
        assert _active(index_module) == 7, (
            "the duplicate delivery kept the slot it took for a start that did "
            "not happen"
        )

    def test_repeated_redeliveries_do_not_walk_the_counter_up(self, index_module):
        """The outage shape: one message redelivered ~100 times reached
        ``MaxConcurrentWorkflows`` and blocked admission for every document."""
        _set_active(index_module, 0)
        index_module.sfn.start_execution.side_effect = _already_exists()

        for _ in range(MAX_CONCURRENT + 2):
            assert index_module.process_message(_record())[0] is True

        assert _active(index_module) == 0
        # And admission still works afterwards.
        assert index_module.update_counter(increment=True) is True

    def test_a_failed_start_hands_its_slot_back(self, index_module):
        """The other compensating-decrement path: StartExecution failed, so no
        execution owns the slot and the message will be redelivered."""
        _set_active(index_module, 4)
        index_module.sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "StartExecution",
        )

        success, _ = index_module.process_message(_record())

        assert success is False
        assert _active(index_module) == 4

    def test_a_successful_start_keeps_its_slot(self, index_module):
        """The counterpart that must not change: once the execution exists the
        workflow tracker owns the decrement, so ``process_message`` must leave
        the counter raised."""
        _set_active(index_module, 1)

        success, _ = index_module.process_message(_record())

        assert success is True
        assert _active(index_module) == 2
