# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The concurrency decrement must never subtract a slot it does not hold.

``test_decrement_durability.py`` covers the *under*-release direction: a lost
decrement leaks a slot and eventually wedges the stack. This suite covers the
*over*-release direction, which was left unguarded for as long and is strictly
worse. The queue processor admits work while ``active_count < MaxConcurrentWorkflows``,
so a counter driven below zero raises the effective ceiling by exactly that much,
indefinitely, and nothing errors: documents process, queues drain, every graph looks
healthy, and the stack simply spends more on Bedrock and Textract than it was told
to (issues #915 and #916).

Two guards, both exercised here against a real (moto) DynamoDB table rather than a
mock, because both of them ARE DynamoDB semantics — a ConditionExpression and the
atomicity of TransactWriteItems:

* a floor at zero, so an extra decrement is refused and reported as
  ``ConcurrencyCounterUnderflow`` instead of going negative;
* a ``dec#<executionArn>`` marker written in the same transaction as the
  decrement, so a redelivered terminal event (the rule allows
  ``MaximumRetryAttempts: 3``) cannot subtract a second slot no matter where the
  previous attempt died.

For the record, and as in the sibling suite: this is a latent gap being closed, not
the cause of the leak that motivated the work. That investigation found ZERO
decrement failures across the entire retained log history and exactly 177
decrements for 177 terminal executions through the incident window.
"""

import importlib.util
import os
import sys
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "workflow_tracker_floor_under_test"

CONCURRENCY_TABLE = "test-concurrency"
EXEC_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp:doc-1"
OTHER_EXEC_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp:doc-2"


@pytest.fixture
def tracker(monkeypatch):
    """Load the tracker against a moto-backed concurrency table.

    boto3 is deliberately NOT patched here: the behaviour under test is
    DynamoDB's, so the clients the module builds at import time must be real ones
    talking to moto. Only CloudWatch is a mock, so the tests can assert on which
    metric was published.
    """
    env_vars = {
        "CONCURRENCY_TABLE": CONCURRENCY_TABLE,
        "METRIC_NAMESPACE": "TEST_NS",
        "DECREMENT_MAX_ATTEMPTS": "4",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    for name, mod in {
        "idp_common": MagicMock(),
        "idp_common.models": MagicMock(),
        "idp_common.docs_service": fake_docs_service,
        "idp_common.document_versions": MagicMock(),
        "idp_common.delete_documents": MagicMock(),
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with patch.dict(os.environ, env_vars, clear=False), mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=CONCURRENCY_TABLE,
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
        module.cloudwatch = MagicMock()
        monkeypatch.setattr(module.time, "sleep", lambda *_: None)
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _seed(tracker, active):
    tracker.concurrency_table.put_item(
        Item={"counter_id": tracker.COUNTER_ID, "active_count": active}
    )


def _counter(tracker):
    item = tracker.concurrency_table.get_item(
        Key={"counter_id": tracker.COUNTER_ID}, ConsistentRead=True
    )["Item"]
    return int(item["active_count"])


def _metrics(tracker):
    return [
        m["MetricName"]
        for call in tracker.cloudwatch.put_metric_data.call_args_list
        for m in call.kwargs["MetricData"]
    ]


class TestFloorAtZero:
    def test_double_decrement_stops_at_zero(self, tracker):
        """The bug: ``ADD active_count :dec`` with no condition. One document,
        two terminal events, and the counter is at -1 forever — which is one
        extra workflow admitted above the ceiling for as long as it stays there."""
        _seed(tracker, 1)

        assert tracker.decrement_counter() == 0
        assert tracker.decrement_counter() == 0

        assert _counter(tracker) == 0

    def test_underflow_emits_the_alarm_metric(self, tracker):
        """The underflow is the only trace of a duplicate release, and nothing
        else reports it: both the counter and the document end up correct."""
        _seed(tracker, 0)

        tracker.decrement_counter()

        assert "ConcurrencyCounterUnderflow" in _metrics(tracker)

    def test_underflow_does_not_raise(self, tracker):
        """The document is already terminal here. Failing the tracker would
        retry the whole event and abandon the rest of the terminal handling —
        strictly worse than absorbing a decrement with nothing to subtract."""
        _seed(tracker, 0)

        assert tracker.decrement_counter(EXEC_ARN) == 0
        assert _counter(tracker) == 0

    def test_a_healthy_decrement_emits_no_underflow(self, tracker):
        _seed(tracker, 3)

        assert tracker.decrement_counter() == 2
        assert "ConcurrencyCounterUnderflow" not in _metrics(tracker)


class TestIdempotentPerExecution:
    """Flooring stops the arithmetic going negative; it does NOT stop a
    redelivered event from double-subtracting a slot another workflow still
    legitimately holds. Only the per-execution marker does that (#916)."""

    def test_repeated_decrement_for_the_same_execution_is_a_no_op(self, tracker):
        _seed(tracker, 5)

        assert tracker.decrement_counter(EXEC_ARN) == 4
        assert tracker.decrement_counter(EXEC_ARN) is None
        assert tracker.decrement_counter(EXEC_ARN) is None

        # Four slots are still held by workflows that have not ended. Without
        # the marker this would read 2, and those two slots would be handed out
        # on top of MaxConcurrentWorkflows.
        assert _counter(tracker) == 4

    def test_a_suppressed_duplicate_is_reported(self, tracker):
        _seed(tracker, 5)
        tracker.decrement_counter(EXEC_ARN)
        tracker.cloudwatch.put_metric_data.reset_mock()

        tracker.decrement_counter(EXEC_ARN)

        assert "ConcurrencyDecrementSuppressed" in _metrics(tracker)

    def test_distinct_executions_each_release_their_own_slot(self, tracker):
        """The marker must key on the execution, not merely exist."""
        _seed(tracker, 5)

        assert tracker.decrement_counter(EXEC_ARN) == 4
        assert tracker.decrement_counter(OTHER_EXEC_ARN) == 3

    def test_the_marker_expires(self, tracker):
        """One marker per completed document would otherwise grow the counter
        table forever. TTL reaps them, and the table has TTL enabled on
        ExpiresAfter."""
        _seed(tracker, 1)
        tracker.decrement_counter(EXEC_ARN)

        marker = tracker.concurrency_table.get_item(
            Key={"counter_id": f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}"}
        )["Item"]
        expires_after = int(marker["ExpiresAfter"])
        # Well past any redelivery window (EventBridge retries for up to 24h),
        # and not so far out that markers accumulate for a meaningful time.
        assert expires_after > int(time.time()) + 2 * 24 * 60 * 60
        assert expires_after < int(time.time()) + 30 * 24 * 60 * 60

    def test_the_counter_item_itself_never_gets_a_ttl(self, tracker):
        """TTL is now enabled on the table the counter lives in. If a write ever
        put ExpiresAfter on the counter, DynamoDB would silently delete
        concurrency control itself."""
        _seed(tracker, 1)
        tracker.decrement_counter(EXEC_ARN)

        item = tracker.concurrency_table.get_item(
            Key={"counter_id": tracker.COUNTER_ID}
        )["Item"]
        assert "ExpiresAfter" not in item


class TestWithoutAnExecutionArn:
    def test_still_floored_when_there_is_nothing_to_deduplicate_on(self, tracker):
        """A direct invocation, or an event with no executionArn, cannot be
        deduplicated — but it must still not push the counter negative."""
        _seed(tracker, 0)

        assert tracker.decrement_counter(None) == 0
        assert _counter(tracker) == 0

    def test_applies_normally_when_a_slot_is_held(self, tracker):
        _seed(tracker, 2)

        assert tracker.decrement_counter(None) == 1
