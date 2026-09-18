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


class TestCancellationReasonDisambiguation:
    """``CancellationReasons`` ordering is the subtle part of the transaction.

    ``TransactWriteItems`` reports one reason per item, positionally, and
    ``_apply_decrement`` sends ``[{"Put": marker}, {"Update": counter}]`` — so
    index 0 is the marker's ``attribute_not_exists`` and index 1 is the counter's
    ``active_count > 0``. Reading them the other way round swaps "duplicate" for
    "underflow": two outcomes with different log lines, different metrics and
    different alarms.

    Measured, so the value of these tests is not overstated: a WHOLESALE swap of
    the two indices is already caught indirectly by three tests in
    ``TestFloorAtZero`` / ``TestIdempotentPerExecution``. What nothing covered is
    the PRECEDENCE when both conditions fail at once — keep each index's meaning
    correct but test the counter first, and every one of those 28 tests still
    passes while a correctly-suppressed redelivery is reported as an underflow and
    raises ``ConcurrencyCounterUnderflowAlarm``. Only
    ``test_both_conditions_failing_reports_a_duplicate_not_an_underflow`` and
    ``test_the_metric_follows_the_disambiguation`` fail on it.

    These pin the mapping through real DynamoDB (moto), which is the only thing
    that produces the reason list in the first place.
    """

    def test_a_new_execution_against_a_held_slot_is_applied(self, tracker):
        """The control case: neither condition fails, no CancellationReasons."""
        _seed(tracker, 2)

        marker = f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}"
        assert tracker._apply_decrement(marker) == "applied"

    def test_an_existing_marker_reports_a_duplicate(self, tracker):
        """Reason 0 (the marker) failed, reason 1 (the counter) did not."""
        _seed(tracker, 2)
        marker = f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}"
        assert tracker._apply_decrement(marker) == "applied"

        assert tracker._apply_decrement(marker) == "duplicate"
        # The slot was NOT subtracted a second time.
        assert _counter(tracker) == 1

    def test_a_fresh_marker_against_a_zero_counter_reports_an_underflow(self, tracker):
        """Reason 1 (the counter's floor) failed, reason 0 (the marker) did not.

        Read in the wrong order this returns "duplicate", which would suppress
        ``ConcurrencyCounterUnderflow`` — the only signal that a slot was released
        twice — and emit ``ConcurrencyDecrementSuppressed`` in its place.
        """
        _seed(tracker, 0)

        assert (
            tracker._apply_decrement(f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}")
            == "underflow"
        )

    def test_both_conditions_failing_reports_a_duplicate_not_an_underflow(
        self, tracker
    ):
        """The ambiguous case, and the reason the marker is checked FIRST: if this
        execution's slot was already released, the counter reaching 0 afterwards is
        someone else's business. Calling it an underflow would raise
        ``ConcurrencyCounterUnderflowAlarm`` on a correctly-suppressed redelivery.
        """
        _seed(tracker, 1)
        marker = f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}"
        assert tracker._apply_decrement(marker) == "applied"
        assert _counter(tracker) == 0

        # Marker exists AND the counter is at its floor: both conditions fail.
        assert tracker._apply_decrement(marker) == "duplicate"

    def test_the_metric_follows_the_disambiguation(self, tracker):
        """End to end through ``decrement_counter``: the both-failed case must
        publish ``ConcurrencyDecrementSuppressed`` and NOT
        ``ConcurrencyCounterUnderflow``."""
        _seed(tracker, 1)
        tracker.decrement_counter(EXEC_ARN)
        tracker.cloudwatch.put_metric_data.reset_mock()

        tracker.decrement_counter(EXEC_ARN)

        published = _metrics(tracker)
        assert "ConcurrencyDecrementSuppressed" in published
        assert "ConcurrencyCounterUnderflow" not in published

    def test_an_underflow_leaves_no_marker(self, tracker):
        """Records the accepted residual, so a change that alters it is deliberate.

        The transaction is all-or-nothing, so a refused decrement also refuses the
        marker: the execution is NOT deduplicated afterwards, and a later
        redelivery against a non-zero counter subtracts a slot belonging to a
        different, live workflow. It is narrow (the underflow path returns 200, so
        EventBridge does not redeliver) and bounded to one spurious subtraction
        with the floor still holding. Writing the marker outside the transaction to
        close it would trade this for the strictly worse failure the transaction
        prevents — a marker that lands while the decrement does not, losing that
        execution's slot permanently.
        """
        _seed(tracker, 0)
        assert tracker.decrement_counter(EXEC_ARN) == 0

        assert (
            "Item"
            not in tracker.concurrency_table.get_item(
                Key={"counter_id": f"{tracker.DECREMENT_MARKER_PREFIX}{EXEC_ARN}"}
            )
        )

        # The residual, demonstrated: the same execution's event now subtracts.
        _seed(tracker, 3)
        assert tracker.decrement_counter(EXEC_ARN) == 2
