# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the agent DynamoDB message logger and tracker.

This is the path that puts an agent's conversation in front of a user: the analytics
UI reads `agent_messages` off the job record. Two properties matter more than the
rest, and both are read-modify-write hazards rather than anything the type system
catches.

**Appending must not lose earlier messages.** `_write_message_to_dynamodb` reads the
existing JSON string, appends one entry, and writes the whole array back, so every
read outcome that yields `[]` — a malformed string, a missing item — would discard
everything written so far, and invisibly, because the write that follows succeeds.
Each such read outcome is asserted separately for exactly that reason. The write is
guarded by a version attribute read alongside the transcript, so an overlapping
writer is rejected and retried rather than overwritten, and a read that *failed*
never becomes an empty transcript.

**A failure to log must not fail the agent.** Every write happens on a thread pool
and every handler swallows its exceptions, so the tests assert the swallowing
explicitly: a lost transcript is a bad outcome, an aborted agent request is a worse
one.

`boto3.resource` is stubbed throughout and the executor is replaced with a
synchronous stand-in where a test needs the write to have happened by the time it
asserts.

⚠️ **`_InlineExecutor` is blind to the concurrency properties of this module.** It
runs `submit` on the calling thread, which is exactly `max_workers=1` — the one
configuration in which overlapping appends cannot happen. It is kept because it is
what makes the ~60 content assertions here deterministic and fast, but no green run
of one of them says anything about whether concurrent appends are safe. That
property is covered by `TestConcurrentAppends`, which uses a **real**
`ThreadPoolExecutor` and a table that forces every writer to read the same state
before any of them writes, and by `TestConflictRetry`, which drives the retry
mechanism with no threads at all. A concurrency regression has to fail one of those
two; it will not fail any of the rest.
"""

import json
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.agents.common.dynamodb_logger import (
    DynamoDBMessageLogger,
    DynamoDBMessageTracker,
)

MODULE = "idp_common.agents.common.dynamodb_logger"


class _InlineExecutor:
    """A ThreadPoolExecutor stand-in that runs the work immediately."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.shutdown_calls: list[bool] = []

    def submit(self, fn, *args, **kwargs):
        future = MagicMock()
        try:
            future.result.return_value = fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - surfaced via future.result
            future.result.side_effect = exc
        future.add_done_callback.side_effect = lambda cb: cb(future)
        return future

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls.append(wait)


def _client_error(code: str):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "m"}}, "GetItem"
    )


def _logger(
    existing: Any = "[]",
    *,
    get_error: Exception | None = None,
    version: Any = None,
):
    """A DynamoDBMessageLogger whose table returns `existing` on get_item."""
    table = MagicMock()
    if get_error is not None:
        table.get_item.side_effect = get_error
    elif existing is None:
        table.get_item.return_value = {}
    else:
        item: dict[str, Any] = {"agent_messages": existing}
        if version is not None:
            item["agent_messages_version"] = version
        table.get_item.return_value = {"Item": item}
    with (
        patch(f"{MODULE}.boto3.resource") as resource,
        patch(f"{MODULE}.ThreadPoolExecutor", _InlineExecutor),
    ):
        resource.return_value.Table.return_value = table
        instance = DynamoDBMessageLogger("agent-table")
    return instance, table


def _written_messages(table: MagicMock) -> list[dict[str, Any]]:
    """The array that was written back, decoded."""
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    return json.loads(values[":messages"])


@pytest.mark.unit
class TestLoggerConstruction:
    """__init__: table binding and the worker pool."""

    def test_the_named_table_is_bound(self):
        with (
            patch(f"{MODULE}.boto3.resource") as resource,
            patch(f"{MODULE}.ThreadPoolExecutor", _InlineExecutor),
        ):
            DynamoDBMessageLogger("agent-table")
        resource.return_value.Table.assert_called_once_with("agent-table")

    def test_the_worker_count_is_configurable(self):
        with (
            patch(f"{MODULE}.boto3.resource"),
            patch(f"{MODULE}.ThreadPoolExecutor") as pool,
        ):
            DynamoDBMessageLogger("agent-table", max_workers=7)
        assert pool.call_args.kwargs["max_workers"] == 7

    def test_the_sequence_counter_starts_at_zero(self):
        instance, _ = _logger()
        assert instance.sequence_counter == 0


@pytest.mark.unit
class TestSequenceNumbering:
    """log_message_async: the sequence number the UI orders by."""

    def test_the_first_message_is_numbered_one(self):
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert _written_messages(table)[-1]["sequence_number"] == 1

    def test_numbering_increments_per_message(self):
        instance, table = _logger()
        for _ in range(3):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert instance.sequence_counter == 3

    def test_the_sequence_number_is_stamped_onto_the_callers_dict(self):
        # The caller keeps a local copy of this dict (DynamoDBMessageTracker appends
        # it to self.messages before the write), so the number has to land on the
        # same object rather than on a copy, or the local transcript is unnumbered.
        instance, _ = _logger()
        message = {"role": "user"}
        instance.log_message_async("job-1", "user-1", message)
        assert message["sequence_number"] == 1


@pytest.mark.unit
class TestAppendSemantics:
    """_write_message_to_dynamodb: the read-modify-write that must not lose history."""

    def test_the_key_is_derived_from_the_user_and_the_job(self):
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.get_item.call_args.kwargs["Key"] == {
            "PK": "agent#user-1",
            "SK": "job-1",
        }
        assert table.update_item.call_args.kwargs["Key"] == {
            "PK": "agent#user-1",
            "SK": "job-1",
        }

    def test_an_existing_message_is_preserved_alongside_the_new_one(self):
        instance, table = _logger(json.dumps([{"role": "user", "sequence_number": 1}]))
        instance.log_message_async("job-1", "user-1", {"role": "assistant"})
        written = _written_messages(table)
        assert len(written) == 2
        assert written[0]["role"] == "user"
        assert written[1]["role"] == "assistant"

    def test_a_long_history_is_preserved_in_order(self):
        history = [{"role": "user", "sequence_number": i} for i in range(1, 6)]
        instance, table = _logger(json.dumps(history))
        instance.log_message_async("job-1", "user-1", {"role": "assistant"})
        written = _written_messages(table)
        assert [m["sequence_number"] for m in written[:5]] == [1, 2, 3, 4, 5]

    def test_an_absent_attribute_starts_a_fresh_array(self):
        instance, table = _logger(existing=None)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert len(_written_messages(table)) == 1

    def test_malformed_existing_json_starts_fresh_rather_than_raising(self):
        # A corrupted attribute loses the history either way; raising here would
        # additionally stop every later message from being written.
        instance, table = _logger("not json at all")
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert len(_written_messages(table)) == 1

    def test_a_json_scalar_where_an_array_was_expected_starts_fresh(self):
        instance, table = _logger(json.dumps({"not": "a list"}))
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert len(_written_messages(table)) == 1

    def test_a_missing_table_on_read_abandons_the_write_entirely(self):
        # ResourceNotFoundException on the read means the job record does not
        # exist, so there is nothing to append to and writing would create a
        # partial record with one message and no job.
        instance, table = _logger(get_error=_client_error("ResourceNotFoundException"))
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        table.update_item.assert_not_called()

    def test_a_read_that_keeps_failing_writes_nothing_at_all(self):
        # A throttled read yields no transcript, and appending to the empty array
        # that stands in for one would overwrite every message already stored --
        # invisibly, because that write reports success. So a failed read is
        # retried and, if it never succeeds, the one message is dropped instead.
        instance, table = _logger(
            get_error=_client_error("ProvisionedThroughputExceededException")
        )
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        table.update_item.assert_not_called()

    def test_a_read_that_recovers_appends_to_the_history_it_then_sees(self):
        instance, table = _logger()
        history = json.dumps([{"role": "user", "sequence_number": 1}])
        table.get_item.side_effect = [
            _client_error("ProvisionedThroughputExceededException"),
            {"Item": {"agent_messages": history}},
        ]
        instance.log_message_async("job-1", "user-1", {"role": "assistant"})
        assert [m["role"] for m in _written_messages(table)] == ["user", "assistant"]

    def test_the_whole_array_is_written_as_one_json_string(self):
        # The attribute is typed String in the GraphQL schema and JSON.parse'd by
        # the UI, so the array stays a serialized string rather than becoming a
        # native DynamoDB list.
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert isinstance(values[":messages"], str)

    def test_the_write_sets_the_transcript_and_its_version_together(self):
        # The guard is only a guard if it advances in the same write it protects:
        # a separate write would leave a window in which the version is stale.
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        expression = table.update_item.call_args.kwargs["UpdateExpression"]
        assert "agent_messages = :messages" in expression
        assert "agent_messages_version = :next_version" in expression

    def test_the_write_asks_for_no_return_values(self):
        # The returned item would be the whole transcript on every message, which
        # is read capacity spent on something nobody reads.
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.update_item.call_args.kwargs["ReturnValues"] == "NONE"


@pytest.mark.unit
class TestConflictGuard:
    """The version attribute the conditional write is guarded on."""

    def _condition(self, table: MagicMock) -> str:
        return table.update_item.call_args.kwargs["ConditionExpression"]

    def _values(self, table: MagicMock) -> dict[str, Any]:
        return table.update_item.call_args.kwargs["ExpressionAttributeValues"]

    def test_the_write_is_conditional_on_the_version_that_was_read(self):
        instance, table = _logger(version=7)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert "agent_messages_version = :expected_version" in self._condition(table)
        assert self._values(table)[":expected_version"] == 7

    def test_the_stored_version_advances_by_one(self):
        instance, table = _logger(version=7)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert self._values(table)[":next_version"] == 8

    def test_a_record_written_before_the_guard_existed_is_still_appendable(self):
        # Such a record has agent_messages but no version attribute, so an
        # equality condition alone would reject every append to it forever.
        instance, table = _logger(json.dumps([{"sequence_number": 1}]))
        instance.log_message_async("job-1", "user-1", {"role": "assistant"})
        assert "attribute_not_exists(agent_messages_version)" in self._condition(table)
        assert len(_written_messages(table)) == 2

    def test_the_first_append_to_an_unversioned_record_starts_the_count_at_one(self):
        instance, table = _logger(json.dumps([{"sequence_number": 1}]))
        instance.log_message_async("job-1", "user-1", {"role": "assistant"})
        assert self._values(table)[":next_version"] == 1

    def test_a_decimal_version_is_compared_as_an_integer(self):
        # boto3 hands numbers back as Decimal, and Decimal("7") + 1 would be a
        # Decimal the next reader compares against an int.
        from decimal import Decimal

        instance, table = _logger(version=Decimal("7"))
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert self._values(table)[":next_version"] == 8
        assert isinstance(self._values(table)[":next_version"], int)


@pytest.mark.unit
class TestConflictRetry:
    """A rejected append is rebuilt on a fresh read, with no threads involved.

    This is the mechanism the concurrency fix rests on, asserted directly: the
    conflict is injected rather than raced for, so these tests are as
    deterministic as any other in this module.
    """

    def test_a_rejected_append_is_retried_and_the_message_survives(self):
        instance, table = _logger()
        table.update_item.side_effect = [
            _client_error("ConditionalCheckFailedException"),
            None,
        ]
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.update_item.call_count == 2
        assert len(_written_messages(table)) == 1

    def test_the_retry_re_reads_rather_than_re_sending_the_same_array(self):
        # The whole point of the retry: the winner's message has to be picked up,
        # so the second attempt must be built on a second read, not on the array
        # the first attempt already computed.
        instance, table = _logger()
        table.get_item.side_effect = [
            {"Item": {"agent_messages": "[]"}},
            {
                "Item": {
                    "agent_messages": json.dumps([{"role": "other-writer"}]),
                    "agent_messages_version": 1,
                }
            },
        ]
        table.update_item.side_effect = [
            _client_error("ConditionalCheckFailedException"),
            None,
        ]
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.get_item.call_count == 2
        written = _written_messages(table)
        assert [m["role"] for m in written] == ["other-writer", "user"]

    def test_the_retry_is_bounded_rather_than_looping_forever(self):
        instance, table = _logger()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.update_item.call_count == 10

    def test_exhausting_the_retries_reports_the_message_it_dropped(self, caplog):
        # Dropping one message is the right trade against overwriting the whole
        # transcript, but it must not be silent -- silence is what made the
        # original defect invisible.
        import logging

        instance, table = _logger()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        with caplog.at_level(logging.ERROR):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("job-1" in m and "not persisted" in m for m in errors)

    def test_a_non_conflict_write_error_is_not_retried(self):
        # Retrying a validation or throughput failure ten times spends capacity
        # on a write that cannot succeed.
        instance, table = _logger()
        table.update_item.side_effect = _client_error("ValidationException")
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.update_item.call_count == 1


class _ConditionalTable:
    """A single-item table stand-in that forces every writer to overlap.

    Two behaviours make this a faithful stand-in, and the defect this guards
    against is invisible without both:

    * ``get_item`` snapshots the item **at call time**. A double that reads its
      own stored state after a simulated delay observes the previous write and
      shows no loss at all at any worker count.
    * ``update_item`` evaluates ``ConditionExpression`` under a lock and raises
      ConditionalCheckFailedException when it does not hold, as DynamoDB does for
      a single-item write. An *absent* condition always holds, also as DynamoDB
      does -- so an unguarded write is accepted here and loses the overlapping
      message, which is what makes these tests fail on the defect itself rather
      than on a missing keyword argument.

    Overlap is produced by a one-shot barrier rather than by a sleep: the first
    ``parties`` readers each take their snapshot and then block until all of them
    have, so every writer provably starts from the same state and the scheduler
    has no say in it. Retried reads pass straight through, so the barrier cannot
    deadlock the retry loop.
    """

    _CONDITION = (
        "attribute_not_exists(agent_messages_version) "
        "OR agent_messages_version = :expected_version"
    )

    def __init__(self, parties: int, item: dict[str, Any] | None = None):
        self.item: dict[str, Any] = dict(item) if item else {}
        self.conditional_rejections = 0
        self._parties = parties
        self._barrier = threading.Barrier(parties)
        self._synchronised = 0
        self._lock = threading.Lock()

    def get_item(self, Key):
        snapshot = dict(self.item)  # snapshot at call time, as a real read does
        with self._lock:
            first_round = self._synchronised < self._parties
            if first_round:
                self._synchronised += 1
        if first_round:
            # A timeout rather than an indefinite wait: a writer that never
            # arrives should fail the assertion, not hang the suite.
            self._barrier.wait(timeout=30)
        return {"Item": snapshot} if snapshot else {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ReturnValues=None,
        ConditionExpression=None,
    ):
        if ConditionExpression not in (None, self._CONDITION):
            raise AssertionError(f"unmodelled condition: {ConditionExpression!r}")
        with self._lock:
            held = (
                ConditionExpression is None
                or "agent_messages_version" not in self.item
                or self.item["agent_messages_version"]
                == ExpressionAttributeValues[":expected_version"]
            )
            if not held:
                self.conditional_rejections += 1
                raise _client_error("ConditionalCheckFailedException")
            self.item["agent_messages"] = ExpressionAttributeValues[":messages"]
            if ":next_version" in ExpressionAttributeValues:
                self.item["agent_messages_version"] = ExpressionAttributeValues[
                    ":next_version"
                ]

    def sequence_numbers(self) -> list:
        stored = self.item.get("agent_messages", "[]")
        return [m.get("sequence_number") for m in json.loads(stored)]


@pytest.mark.unit
class TestConcurrentAppends:
    """The property `_InlineExecutor` cannot see, on a real thread pool.

    These are not timing assertions. The barrier in `_ConditionalTable` makes the
    overlap itself deterministic -- every writer is guaranteed to have read the
    same state before any of them writes -- and what is asserted afterwards is an
    invariant that holds under every interleaving: no submitted message is
    missing. There is no sleep, no wall-clock threshold and no dependence on the
    scheduler, so the only way to fail is for an append to actually be lost.

    What is asserted is the *set* of messages stored, not their order. Under real
    overlap the stored order is the order the writes committed in, which the
    scheduler does decide; asserting it would be the flaky, timing-dependent
    assertion this class exists to avoid. Order is not lost by that -- every
    message carries the `sequence_number` the UI sorts on.
    """

    def _run(self, workers: int, item: dict[str, Any] | None = None):
        table = _ConditionalTable(parties=workers, item=item)
        with patch(f"{MODULE}.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            instance = DynamoDBMessageLogger("agent-table", max_workers=workers)
        for index in range(workers):
            instance.log_message_async("job-1", "user-1", {"role": f"r{index}"})
        instance.shutdown()  # drains the pool, so the assertions see every write
        return table

    @pytest.mark.parametrize("workers", [2, 4, 8])
    def test_no_message_is_lost_when_every_writer_overlaps(self, workers):
        table = self._run(workers)
        assert sorted(table.sequence_numbers()) == list(range(1, workers + 1))

    def test_the_stored_sequence_numbers_are_contiguous(self):
        # The observable symptom of the defect was a gap in this series: four
        # messages submitted, `[2, 4]` stored. Contiguity is what its absence
        # looks like.
        stored = sorted(self._run(8).sequence_numbers())
        # Asserted before contiguity: an empty array is trivially contiguous, so
        # a run that stored nothing at all would otherwise pass this.
        assert len(stored) == 8
        assert stored == list(range(1, len(stored) + 1))

    def test_the_overlap_really_happened(self):
        # Without this the test above could pass by never racing at all, which is
        # how a concurrency test comes to prove nothing. The barrier guarantees
        # all four writers read the same state, so exactly three of them must be
        # rejected once and rebuilt -- a count, not a race.
        table = self._run(4)
        assert table.conditional_rejections >= 3

    def test_an_unversioned_record_keeps_its_history_through_the_overlap(self):
        # The upgrade case: existing transcripts carry no version attribute, and
        # the appends that add it must not drop what is already stored.
        existing = {"agent_messages": json.dumps([{"sequence_number": 0}])}
        table = self._run(4, item=existing)
        assert sorted(table.sequence_numbers()) == [0, 1, 2, 3, 4]


@pytest.mark.unit
class TestWriteFailuresAreSwallowed:
    """A logging failure must never abort the agent's own request."""

    @pytest.mark.parametrize(
        "code",
        [
            "ResourceNotFoundException",
            "ConditionalCheckFailedException",
            "ProvisionedThroughputExceededException",
        ],
    )
    def test_a_client_error_on_the_write_does_not_propagate(self, code):
        instance, table = _logger()
        table.update_item.side_effect = _client_error(code)
        instance.log_message_async("job-1", "user-1", {"role": "user"})

    def test_an_unexpected_error_on_the_write_does_not_propagate(self):
        instance, table = _logger()
        table.update_item.side_effect = RuntimeError("boom")
        instance.log_message_async("job-1", "user-1", {"role": "user"})

    def test_the_done_callback_absorbs_an_exception_from_the_future(self):
        # _handle_write_result calls future.result(), which re-raises. It runs on a
        # pool thread, where an escaped exception is logged by the interpreter and
        # lost, so it is caught here instead.
        instance, _ = _logger()
        future = MagicMock()
        future.result.side_effect = RuntimeError("write failed")
        instance._handle_write_result(future)

    def test_the_done_callback_logs_nothing_on_success(self, caplog):
        # Asserting the absence, not just the absence of a crash: this method's only
        # job on the happy path is to stay silent, and an ERROR per successful write
        # would bury the real failures it exists to report.
        import logging

        instance, _ = _logger()
        future = MagicMock()
        future.result.return_value = None
        caplog.clear()  # drop the constructor's own INFO record
        with caplog.at_level(logging.WARNING):
            instance._handle_write_result(future)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.unit
class TestThrottlingEventLogging:
    """log_throttling_event_async: a throttle rendered as a system message."""

    def test_the_event_is_written_as_a_system_message(self):
        instance, table = _logger()
        instance.log_throttling_event_async(
            "job-1",
            "user-1",
            {"error_code": "ThrottlingException", "error_message": "rate exceeded"},
        )
        written = _written_messages(table)[-1]
        assert written["role"] == "system"
        assert written["message_type"] == "throttling_event"

    def test_the_content_names_the_code_and_the_message_for_the_ui(self):
        instance, table = _logger()
        instance.log_throttling_event_async(
            "job-1",
            "user-1",
            {"error_code": "ThrottlingException", "error_message": "rate exceeded"},
        )
        content = _written_messages(table)[-1]["content"]
        assert "ThrottlingException" in content
        assert "rate exceeded" in content

    def test_the_raw_details_are_kept_alongside_the_rendered_text(self):
        instance, table = _logger()
        details = {"error_code": "ThrottlingException", "error_message": "m"}
        instance.log_throttling_event_async("job-1", "user-1", details)
        assert _written_messages(table)[-1]["throttling_details"] == details

    def test_an_event_with_no_fields_still_produces_a_usable_message(self):
        instance, table = _logger()
        instance.log_throttling_event_async("job-1", "user-1", {})
        written = _written_messages(table)[-1]
        assert written["content"]
        assert written["timestamp"]

    def test_a_supplied_timestamp_is_preferred_over_now(self):
        # The event was detected earlier than it is logged, and the UI orders by
        # this field.
        instance, table = _logger()
        instance.log_throttling_event_async(
            "job-1", "user-1", {"timestamp": "2026-01-01T00:00:00"}
        )
        assert _written_messages(table)[-1]["timestamp"] == "2026-01-01T00:00:00"

    def test_a_throttling_event_takes_a_sequence_number_like_any_message(self):
        instance, table = _logger()
        instance.log_throttling_event_async("job-1", "user-1", {})
        assert _written_messages(table)[-1]["sequence_number"] == 1


@pytest.mark.unit
class TestShutdown:
    """shutdown: the pool must drain, not be dropped."""

    def test_shutdown_waits_for_in_flight_writes(self):
        # Returning before the queue drains loses the last messages of a
        # conversation, which are the ones a user is waiting to see.
        instance, _ = _logger()
        instance.shutdown()
        assert instance.executor.shutdown_calls == [True]


@pytest.mark.unit
class TestTrackerConstruction:
    """DynamoDBMessageTracker: the disabled and misconfigured paths."""

    def test_disabled_construction_creates_no_logger_and_no_monitor(self):
        tracker = DynamoDBMessageTracker("job-1", "user-1", enabled=False)
        assert tracker.enabled is False
        assert tracker.db_logger is None
        assert tracker.throttling_monitor is None

    def test_no_table_name_and_no_environment_variable_disables_the_tracker(
        self, monkeypatch
    ):
        # Disabling is the right failure mode: the alternative is every message
        # raising against a table name of None.
        monkeypatch.delenv("AGENT_TABLE", raising=False)
        tracker = DynamoDBMessageTracker("job-1", "user-1")
        assert tracker.enabled is False
        assert tracker.db_logger is None

    def test_the_table_name_is_read_from_the_environment_when_not_given(
        self, monkeypatch
    ):
        monkeypatch.setenv("AGENT_TABLE", "from-env")
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1")
        logger_cls.assert_called_once_with("from-env")
        assert tracker.enabled is True

    def test_an_explicit_table_name_overrides_the_environment(self, monkeypatch):
        monkeypatch.setenv("AGENT_TABLE", "from-env")
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            DynamoDBMessageTracker("job-1", "user-1", table_name="explicit")
        logger_cls.assert_called_once_with("explicit")

    def test_a_throttling_monitor_is_created_sharing_the_same_logger(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        assert tracker.throttling_monitor is not None
        assert tracker.throttling_monitor.db_logger is logger_cls.return_value

    def test_a_logger_that_cannot_be_built_disables_the_tracker(self):
        # A missing table or a denied role must degrade to "no transcript" rather
        # than to a broken agent.
        with patch(
            f"{MODULE}.DynamoDBMessageLogger", side_effect=RuntimeError("AccessDenied")
        ):
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        assert tracker.enabled is False
        assert tracker.db_logger is None
        assert tracker.throttling_monitor is None


@pytest.mark.unit
class TestTrackerMessageHandling:
    """on_message_added: local transcript plus the async write."""

    def _tracker(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        return tracker, logger_cls.return_value

    def _event(self, message):
        event = MagicMock()
        event.message = message
        return event

    def test_a_message_is_recorded_locally_and_written(self):
        tracker, db_logger = self._tracker()
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        assert len(tracker.get_messages()) == 1
        db_logger.log_message_async.assert_called_once()

    def test_the_write_carries_the_job_and_user(self):
        tracker, db_logger = self._tracker()
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        args = db_logger.log_message_async.call_args.args
        assert args[0] == "job-1"
        assert args[1] == "user-1"

    def test_the_message_is_reduced_through_the_shared_preview_logic(self):
        # The tracker reuses AgentMonitor._get_message_preview so the transcript in
        # DynamoDB and the transcript in the logs describe a message the same way.
        tracker, _ = self._tracker()
        tracker.on_message_added(
            self._event(
                {
                    "role": "user",
                    "content": [{"toolResult": {"status": "success", "content": []}}],
                }
            )
        )
        assert tracker.get_messages()[0]["role"] == "tool"

    def test_raw_tool_output_is_withheld_by_default(self):
        tracker, _ = self._tracker()
        tracker.on_message_added(
            self._event({"role": "user", "content": [{"toolResult": {"status": "ok"}}]})
        )
        assert "debug_tool_output" not in tracker.get_messages()[0]

    def test_raw_tool_output_is_included_when_the_tracker_is_asked_to(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger"):
            tracker = DynamoDBMessageTracker(
                "job-1", "user-1", table_name="t", include_debug_tool_output=True
            )
        tracker.on_message_added(
            self._event({"role": "user", "content": [{"toolResult": {"status": "ok"}}]})
        )
        assert "debug_tool_output" in tracker.get_messages()[0]

    def test_a_disabled_tracker_records_nothing(self):
        tracker = DynamoDBMessageTracker("job-1", "user-1", enabled=False)
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        assert tracker.get_messages() == []

    def test_a_write_failure_does_not_propagate(self):
        tracker, db_logger = self._tracker()
        db_logger.log_message_async.side_effect = RuntimeError("boom")
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))

    def test_the_message_is_kept_locally_even_when_the_write_fails(self):
        # The local list is what get_messages returns to the caller, and it is the
        # only copy if DynamoDB is unavailable.
        tracker, db_logger = self._tracker()
        db_logger.log_message_async.side_effect = RuntimeError("boom")
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        assert len(tracker.get_messages()) == 1

    def test_a_content_list_longer_than_a_hundred_blocks_is_written_then_logged_badly(
        self,
    ):
        # The log-preview line slices `content` and concatenates "...", which works
        # for a string and raises TypeError for a list of more than 100 blocks. The
        # append and the DynamoDB write both happen BEFORE that line, so the
        # transcript is intact and only the CloudWatch log line is lost. Pinned
        # because the swallowed error reads "Error tracking message", which
        # overstates what went wrong.
        tracker, db_logger = self._tracker()
        tracker.on_message_added(
            self._event({"role": "user", "content": [{"text": "x"}] * 101})
        )
        assert len(tracker.get_messages()) == 1
        db_logger.log_message_async.assert_called_once()

    def test_the_returned_messages_are_a_copy(self):
        tracker, _ = self._tracker()
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        tracker.get_messages().clear()
        assert len(tracker.get_messages()) == 1

    def test_clearing_removes_the_local_transcript(self):
        tracker, _ = self._tracker()
        tracker.on_message_added(self._event({"role": "user", "content": "hi"}))
        tracker.clear_messages()
        assert tracker.get_messages() == []


@pytest.mark.unit
class TestTrackerThrottlingHandling:
    """_handle_throttling_with_agent_message: the tracker's own classifier."""

    def _tracker(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        return tracker, logger_cls.return_value

    def test_a_throttling_client_error_is_recorded_with_its_code(self):
        tracker, db_logger = self._tracker()
        tracker._handle_throttling_with_agent_message(
            botocore.exceptions.ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "rate exceeded"}},
                "Converse",
            )
        )
        recorded = tracker.get_messages()[0]
        assert recorded["role"] == "exception"
        assert recorded["message_type"] == "throttling_exception"
        assert recorded["throttling_details"]["error_code"] == "ThrottlingException"
        db_logger.log_message_async.assert_called_once()

    @pytest.mark.parametrize(
        "name",
        [
            "ThrottlingException",
            "ModelThrottledException",
            "ServiceQuotaExceededException",
            "RequestLimitExceeded",
        ],
    )
    def test_each_known_code_is_recovered_from_a_non_client_error(self, name):
        # Strands raises these as plain exceptions, so the code has to come out of
        # the message text. Unlike ThrottlingMonitor, this handler does look there.
        tracker, _ = self._tracker()
        tracker._handle_throttling_with_agent_message(
            RuntimeError(f"{name}: slow down")
        )
        assert tracker.get_messages()[0]["throttling_details"]["error_code"] == name

    def test_an_unrecognised_throttle_falls_back_to_the_exception_class_name(self):
        tracker, _ = self._tracker()
        tracker._handle_throttling_with_agent_message(
            type("SomeThrottleError", (Exception,), {})("throttled hard")
        )
        details = tracker.get_messages()[0]["throttling_details"]
        assert details["error_code"] == "SomeThrottleError"

    def test_a_non_throttling_exception_is_not_recorded_at_all(self):
        # This handler replaces ThrottlingMonitor's, so recording a generic error
        # here would put "Throttling Event" in front of a user for a bug.
        tracker, db_logger = self._tracker()
        tracker._handle_throttling_with_agent_message(RuntimeError("connection reset"))
        assert tracker.get_messages() == []
        db_logger.log_message_async.assert_not_called()

    def test_the_job_and_user_are_recorded_on_the_event(self):
        tracker, _ = self._tracker()
        tracker._handle_throttling_with_agent_message(RuntimeError("throttled"))
        details = tracker.get_messages()[0]["throttling_details"]
        assert details["job_id"] == "job-1"
        assert details["user_id"] == "user-1"

    def test_a_failure_submitting_the_write_does_not_propagate(self):
        # This handler is installed over ThrottlingMonitor._handle_model_exception,
        # which wraps its own DynamoDB write in try/except. Without the same guard,
        # attaching the DynamoDB logger would *remove* one.
        #
        # Note what is being simulated. The DynamoDB write itself cannot reach this
        # frame: log_message_async submits to a thread pool, and the write's errors are
        # absorbed by _handle_write_result's own try around future.result(). So the
        # side effect below stands for a failure to *submit* — executor.submit raising
        # after shutdown — which is the only exposure the missing guard had, and is
        # unreachable today because nothing in production calls shutdown. Asserted
        # anyway: the guard's value is that this handler behaves like the one it
        # replaces, and that should not depend on shutdown staying uncalled.
        tracker, db_logger = self._tracker()
        db_logger.log_message_async.side_effect = RuntimeError("boom")
        tracker._handle_throttling_with_agent_message(RuntimeError("throttled"))

    def test_the_event_is_kept_locally_even_when_the_submit_fails(self):
        # get_messages is the only copy of the transcript if DynamoDB is unavailable,
        # and the append happens before the write.
        tracker, db_logger = self._tracker()
        db_logger.log_message_async.side_effect = RuntimeError("boom")
        tracker._handle_throttling_with_agent_message(RuntimeError("throttled"))
        assert len(tracker.get_messages()) == 1


@pytest.mark.unit
class TestTrackerHooksAndShutdown:
    """register_hooks and shutdown."""

    def test_a_disabled_tracker_registers_nothing(self):
        registry = MagicMock()
        DynamoDBMessageTracker("job-1", "user-1", enabled=False).register_hooks(
            registry
        )
        registry.add_callback.assert_not_called()

    def test_registration_overrides_the_throttling_monitors_own_handler(self):
        # The override is what routes a throttle into the agent transcript rather
        # than only into the separate throttling-event list, so it is the whole
        # point of the tracker wrapping the monitor.
        with patch(f"{MODULE}.DynamoDBMessageLogger"):
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        tracker.register_hooks(MagicMock())
        assert (
            tracker.throttling_monitor._handle_model_exception
            == tracker._handle_throttling_with_agent_message
        )

    def test_throttling_events_come_from_the_monitor_when_one_exists(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger"):
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        tracker.throttling_monitor.throttling_events.append({"error_code": "x"})
        assert tracker.get_throttling_events() == [{"error_code": "x"}]

    def test_throttling_events_are_empty_when_there_is_no_monitor(self):
        tracker = DynamoDBMessageTracker("job-1", "user-1", enabled=False)
        assert tracker.get_throttling_events() == []

    def test_shutdown_drains_the_logger(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        tracker.shutdown()
        logger_cls.return_value.shutdown.assert_called_once()

    def test_shutdown_is_safe_on_a_disabled_tracker(self):
        DynamoDBMessageTracker("job-1", "user-1", enabled=False).shutdown()
