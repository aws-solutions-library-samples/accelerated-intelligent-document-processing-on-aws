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

**Both of `TestConcurrentAppends`' helpers assert that the pool they got is a real
`ThreadPoolExecutor`, and those assertions are load-bearing rather than defensive.**
Substituting the synchronous stand-in into one of them is the regression that
*caused* #1098, it is a two-line edit, and without the check it leaves the whole
module green except for a single test — so the control guarding the blind spot would
itself be one test wide. With the check, every test built on the substituted helper
fails. Measured by writing each mutation into a throwaway copy of this file: of the
ten tests in the class, the stand-in in `_run` fails 6 and the stand-in in
`_run_separate_loggers` fails 4, which between them is all ten. The rejection count
is asserted inside the parametrised tests for the same reason, so every width
carries it rather than one width carrying it for the others.

The two helpers measure different defects and neither covers the other. `_run` puts
`workers` threads in **one** logger's pool, which is the write race (#1098):
overlapping appends on one record. `_run_separate_loggers` builds `count` **separate**
loggers, which is the production shape — one per sub-agent — and is what #1106 was
about, since an ordinal counted per instance came out unique within any single
logger and so could only collide across them.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.agents.common.dynamodb_logger import (
    _MAX_APPEND_ATTEMPTS,
    _MAX_READ_ATTEMPTS,
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


def _accumulating_logger(item: dict[str, Any] | None = None):
    """A logger over a table that *keeps* what is written to it.

    `_logger` above answers every read with the same canned array, which is what
    most assertions here want but cannot show anything about a value derived from
    the stored transcript: every write would see the same length. `_ConditionalTable`
    at one party is a real accumulating store with the version condition modelled,
    and its barrier of one releases immediately, so no threads are involved.
    """
    table = _ConditionalTable(parties=1, item=item)
    with (
        patch(f"{MODULE}.boto3.resource") as resource,
        patch(f"{MODULE}.ThreadPoolExecutor", _InlineExecutor),
    ):
        resource.return_value.Table.return_value = table
        instance = DynamoDBMessageLogger("agent-table")
    return instance, table


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

    def test_no_ordinal_is_counted_on_the_instance(self):
        # The absence is the fix for #1106. An ordinal counted here restarts at one
        # for every logger, and each sub-agent in a turn builds its own on the same
        # record, so the same value named several different messages in one
        # transcript. Asserted rather than left implicit, because reinstating a
        # counter is a one-line change that every other test in this file would
        # tolerate.
        instance, _ = _logger()
        assert not hasattr(instance, "sequence_counter")


@pytest.mark.unit
class TestSequenceNumbering:
    """The ordinal each stored message carries, derived from the stored array."""

    def test_the_first_message_is_numbered_one(self):
        instance, table = _logger()
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert _written_messages(table)[-1]["sequence_number"] == 1

    def test_numbering_follows_the_stored_position(self):
        instance, table = _accumulating_logger()
        for _ in range(3):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.sequence_numbers() == [1, 2, 3]

    def test_the_ordinal_continues_from_an_existing_transcript(self):
        # An ordinal derived from the array cannot restart, which is the whole
        # point: a second logger joining a record part-way through picks up where
        # the stored transcript leaves off rather than at one.
        existing = [{"role": "user"}, {"role": "assistant"}]
        instance, table = _accumulating_logger({"agent_messages": json.dumps(existing)})
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.sequence_numbers()[-1] == 3

    def test_the_callers_dict_is_left_alone(self):
        # The ordinal is computed on a worker thread and recomputed if the append
        # loses a conflict, so it must not be written onto the object the caller
        # still holds -- DynamoDBMessageTracker keeps that object in self.messages.
        instance, _ = _logger()
        message = {"role": "user"}
        instance.log_message_async("job-1", "user-1", message)
        assert message == {"role": "user"}

    def test_a_rebuilt_append_renumbers_rather_than_keeping_a_stale_ordinal(self):
        # A rejected append re-reads, and the array it re-reads is one longer. The
        # ordinal has to follow, or the winner's message and this one share a value.
        instance, table = _logger()
        table.get_item.side_effect = [
            {"Item": {"agent_messages": "[]", "agent_messages_version": 0}},
            {
                "Item": {
                    "agent_messages": json.dumps([{"role": "other"}]),
                    "agent_messages_version": 1,
                }
            },
        ]
        table.update_item.side_effect = [
            _client_error("ConditionalCheckFailedException"),
            None,
        ]
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert [m["sequence_number"] for m in _written_messages(table)[-1:]] == [2]


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
        # ResourceNotFoundException from get_item means the *table* does not exist,
        # so no write can succeed and retrying would only spend the budget. A
        # missing *item* is a different outcome and is not this one: get_item
        # answers an absent key with an empty response and no error, which is
        # covered by test_an_absent_item_is_appended_to_rather_than_abandoned.
        instance, table = _logger(get_error=_client_error("ResourceNotFoundException"))
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        table.update_item.assert_not_called()

    def test_an_absent_item_is_appended_to_rather_than_abandoned(self):
        # The distinction the test above rests on, asserted rather than assumed: an
        # empty get_item response is the record not being there yet, and the
        # conditional write is what creates it. Reading this as "nothing to append
        # to" would drop the first message of every job.
        instance, table = _logger(existing=None)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        table.update_item.assert_called_once()
        assert [m["role"] for m in _written_messages(table)] == ["user"]

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
        assert table.update_item.call_count == _MAX_APPEND_ATTEMPTS

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

    def test_losing_a_conflict_backs_off_before_re_reading(self):
        # Without a pause the loser re-enters the same race immediately and in
        # phase with every other loser, which is why a condition alone plateaus
        # short of full retention. The interval is asserted to exist, not its
        # length: it is a jittered draw, so a threshold would be a flaky test.
        instance, table = _logger()
        table.update_item.side_effect = [
            _client_error("ConditionalCheckFailedException"),
            None,
        ]
        with patch(f"{MODULE}.time.sleep") as sleep:
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert sleep.call_count == 1
        assert 0 <= sleep.call_args.args[0] <= 0.05

    def test_the_backoff_is_jittered_rather_than_a_fixed_interval(self):
        # Equal sleeps move a collision instead of breaking it up, so two writers
        # that back off for the same duration collide again. Drawing distinct
        # values is the property; asserted over enough draws that a fixed
        # implementation cannot pass by coincidence.
        from idp_common.agents.common.dynamodb_logger import _sleep_before_retry

        with patch(f"{MODULE}.time.sleep") as sleep:
            for _ in range(25):
                _sleep_before_retry(3)
        drawn = {call.args[0] for call in sleep.call_args_list}
        assert len(drawn) > 1


@pytest.mark.unit
class TestReadFailureBudget:
    """Read failures have their own budget, and permanent ones spend none of it.

    Two unrelated failures shared one budget before: a run of failing reads could
    exhaust the append budget having attempted no write at all, and a mixture of
    read failures and conflicts could drop a message that neither alone would have.
    """

    def test_a_failing_read_does_not_consume_the_conflict_budget(self):
        # The mixture case. Read failures up to the read budget, then conflicts up
        # to the append budget: if the two shared one counter the write attempts
        # would stop early, so the count of writes is what discriminates.
        instance, table = _logger()
        table.get_item.side_effect = [
            _client_error("ProvisionedThroughputExceededException")
        ] * (_MAX_READ_ATTEMPTS - 1) + [{"Item": {"agent_messages": "[]"}}] * (
            _MAX_APPEND_ATTEMPTS + 5
        )
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        with patch(f"{MODULE}.time.sleep"):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.update_item.call_count == _MAX_APPEND_ATTEMPTS

    def test_the_read_budget_is_bounded_well_below_the_append_budget(self):
        # A read that never succeeds should stop after its own few attempts rather
        # than running the full append budget: every one of those is a round trip
        # spent on a record it cannot read.
        instance, table = _logger(
            get_error=_client_error("ProvisionedThroughputExceededException")
        )
        with patch(f"{MODULE}.time.sleep"):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.get_item.call_count == _MAX_READ_ATTEMPTS
        table.update_item.assert_not_called()

    @pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException"])
    def test_a_permanent_read_error_is_not_retried_at_all(self, code):
        # Neither answer changes on a retry, so retrying spends the budget and logs
        # the same warning once per attempt. One read, one report.
        instance, table = _logger(get_error=_client_error(code))
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert table.get_item.call_count == 1
        table.update_item.assert_not_called()

    def test_a_permanent_read_error_is_reported_once_and_not_per_attempt(self, caplog):
        import logging

        instance, _ = _logger(get_error=_client_error("AccessDeniedException"))
        with caplog.at_level(logging.WARNING):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    def test_exhausting_the_read_budget_reports_the_message_it_dropped(self, caplog):
        # The same visibility the conflict path has. A read that never succeeds
        # drops the message just as surely as a conflict that never clears.
        import logging

        instance, _ = _logger(
            get_error=_client_error("ProvisionedThroughputExceededException")
        )
        with caplog.at_level(logging.ERROR), patch(f"{MODULE}.time.sleep"):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("job-1" in m and "not persisted" in m for m in errors)


@pytest.mark.unit
class TestDroppedMessageMetric:
    """A dropped message is counted on a metric, not only written to a log.

    The log line names the job and the sequence number, which is what an operator
    needs once they are already looking at the logs. Nothing can be alarmed on it,
    and the transcript losing entries with nothing to notice is the reason #1098
    survived as long as it did.
    """

    def _drop_by_conflict(self):
        instance, table = _logger()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        with patch(f"{MODULE}.time.sleep"):
            with patch("idp_common.metrics.put_metric") as put_metric:
                instance.log_message_async("job-1", "user-1", {"role": "user"})
        return put_metric

    def test_a_message_dropped_after_exhausting_the_conflicts_is_counted(self):
        put_metric = self._drop_by_conflict()
        put_metric.assert_called_once_with("AgentTranscriptMessageDropped", 1)

    def test_a_message_dropped_after_a_failing_read_is_counted(self):
        instance, _ = _logger(
            get_error=_client_error("ProvisionedThroughputExceededException")
        )
        with patch(f"{MODULE}.time.sleep"):
            with patch("idp_common.metrics.put_metric") as put_metric:
                instance.log_message_async("job-1", "user-1", {"role": "user"})
        put_metric.assert_called_once_with("AgentTranscriptMessageDropped", 1)

    def test_a_message_that_was_stored_is_not_counted(self):
        # A counter that also fires on success cannot be alarmed on either.
        instance, _ = _logger()
        with patch("idp_common.metrics.put_metric") as put_metric:
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        put_metric.assert_not_called()

    def test_a_metric_that_cannot_be_published_does_not_raise(self):
        # This runs on the drop path of a best-effort logger. A telemetry failure
        # here must not add a second failure to the one being reported -- and the
        # namespace this resolves to is denied by IAM on some callers, so the
        # failure is a live possibility rather than a hypothetical.
        instance, table = _logger()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        with patch(f"{MODULE}.time.sleep"):
            with patch(
                "idp_common.metrics.put_metric", side_effect=RuntimeError("denied")
            ):
                instance.log_message_async("job-1", "user-1", {"role": "user"})

    def test_a_metric_failure_still_leaves_the_drop_reported_in_the_log(self, caplog):
        import logging

        instance, table = _logger()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        with caplog.at_level(logging.WARNING), patch(f"{MODULE}.time.sleep"):
            with patch(
                "idp_common.metrics.put_metric", side_effect=RuntimeError("denied")
            ):
                instance.log_message_async("job-1", "user-1", {"role": "user"})
        messages = [r.getMessage() for r in caplog.records]
        assert any("not persisted" in m for m in messages)
        assert any("Could not publish" in m for m in messages)


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

    These are not timing assertions, but the scope of what they establish is
    narrower than "safe under concurrency" and worth stating exactly. The barrier
    in `_ConditionalTable` produces **one** interleaving -- every writer reads the
    same state before any of them writes -- and that is the worst case for this
    defect, not a sample of the space. What holds under every interleaving is the
    *rejection*, which DynamoDB decides; whether a rejected append then finds a gap
    within `_MAX_APPEND_ATTEMPTS` is probabilistic, and the measurement behind that
    constant is in the module's own comment rather than asserted here. So: no
    submitted message is missing *under the interleaving the barrier produces*.
    There is no sleep, no wall-clock threshold and no dependence on the scheduler,
    so the only way to fail is for an append to actually be lost.

    What is asserted is the *set* of messages stored, not which message got which
    ordinal. Under real overlap the stored order is the order the writes committed
    in, which the scheduler does decide; pinning a message to an ordinal would be the
    flaky, timing-dependent assertion this class exists to avoid. What does not
    depend on the scheduler is that the ordinals are 1..N with no duplicate, because
    each is the length of the array its write committed against.
    """

    def _run(self, workers: int, item: dict[str, Any] | None = None):
        table = _ConditionalTable(parties=workers, item=item)
        with patch(f"{MODULE}.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            instance = DynamoDBMessageLogger("agent-table", max_workers=workers)
        # The one assertion this class cannot do without. Substituting a
        # synchronous executor is what made #1098 invisible, and it is a two-line
        # edit that leaves every content assertion in this module green -- so
        # without this check the substitution could come back here too, and these
        # tests would keep passing while measuring nothing. Asserted on the
        # instance rather than by patching, because the point is what the
        # production constructor built.
        assert isinstance(instance.executor, ThreadPoolExecutor), (
            "these tests measure nothing unless the writes run on a real pool"
        )
        for index in range(workers):
            instance.log_message_async("job-1", "user-1", {"role": f"r{index}"})
        instance.shutdown()  # drains the pool, so the assertions see every write
        return table

    def _run_separate_loggers(self, count: int):
        """The production shape: one logger per sub-agent, all on one record.

        `_run` above puts `count` threads in a single pool, which covers the write
        race but not #1106 -- within one instance an ordinal counted on the instance
        would still come out unique. Separate instances are what made the ordinals
        collide, since each counted from zero. Same table, same barrier, same
        real-pool assertion; the only difference is how many loggers there are.
        """
        table = _ConditionalTable(parties=count)
        with patch(f"{MODULE}.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            instances = [DynamoDBMessageLogger("agent-table") for _ in range(count)]
        for instance in instances:
            # Carried here too: a synchronous stand-in reduces this to `count`
            # sequential writes, which cannot collide and so cannot fail.
            assert isinstance(instance.executor, ThreadPoolExecutor), (
                "these tests measure nothing unless the writes run on a real pool"
            )
        for index, instance in enumerate(instances):
            instance.log_message_async("job-1", "user-1", {"role": f"agent{index}"})
        for instance in instances:
            instance.shutdown()
        return table

    @pytest.mark.parametrize("workers", [2, 4, 8])
    def test_no_message_is_lost_when_every_writer_overlaps(self, workers):
        table = self._run(workers)
        assert sorted(table.sequence_numbers()) == list(range(1, workers + 1))
        # Carried at every width rather than only at four, so that a change which
        # quietly stops the writers overlapping fails all three parametrisations
        # instead of one test elsewhere. The barrier releases `workers` readers on
        # the same state, so all but the winner must be rejected at least once.
        assert table.conditional_rejections >= workers - 1

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
        # the appends that add it must not drop what is already stored. The stored
        # message occupies position one, so the four appended ones take 2..5 -- an
        # ordinal is a position in the array, and the pre-existing entry's `0` is a
        # value this code would no longer write.
        existing = {"agent_messages": json.dumps([{"sequence_number": 0}])}
        table = self._run(4, item=existing)
        assert sorted(table.sequence_numbers()) == [0, 2, 3, 4, 5]

    @pytest.mark.parametrize("loggers", [2, 4, 12])
    def test_separate_loggers_on_one_record_do_not_repeat_an_ordinal(self, loggers):
        # #1106 itself. Every sub-agent in a turn builds its own logger on the same
        # PK/SK, and an ordinal counted per instance gave all of them 1. Twelve is
        # carried as the widest fan-out the module's measurement covers.
        table = self._run_separate_loggers(loggers)
        stored = table.sequence_numbers()
        # Length first: an empty array has no duplicates either, so a run that
        # stored nothing would pass the uniqueness assertion on its own.
        assert len(stored) == loggers
        assert sorted(stored) == list(range(1, loggers + 1))

    def test_the_separate_loggers_really_overlapped(self):
        # Without a rejection none of them raced, and unique ordinals would follow
        # from the writes having been sequential rather than from the fix.
        table = self._run_separate_loggers(4)
        assert table.conditional_rejections >= 3


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
        # The event was detected earlier than it is logged, and this field is what
        # says when -- both on screen and in the log line that names a message the
        # write could not persist.
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
    """shutdown: a bounded drain that reports what it could not finish.

    These use a **real** pool, for the same reason `TestConcurrentAppends` does and
    with a sharper edge: with a synchronous stand-in every write has already
    happened by the time `shutdown` is called, so a `shutdown` that drained nothing
    at all would pass. What is asserted is that a write which had *not* run did run,
    which is the property #1110 is about.
    """

    def _real_pool_logger(self, gate: threading.Event | None = None):
        """A logger on a real width-1 pool whose writes can be held open."""
        table = MagicMock()
        table.get_item.return_value = {"Item": {"agent_messages": "[]"}}
        if gate is not None:
            table.get_item.side_effect = lambda **_: (
                gate.wait(timeout=30),
                {"Item": {"agent_messages": "[]"}},
            )[1]
        with patch(f"{MODULE}.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            instance = DynamoDBMessageLogger("agent-table")
        assert isinstance(instance.executor, ThreadPoolExecutor), (
            "a drain test on a synchronous stand-in measures nothing"
        )
        return instance, table

    def test_a_queued_write_runs_before_shutdown_returns(self):
        # The assertion is on the write, not on shutdown having been called: a
        # message still queued when the execution environment freezes is the loss.
        gate = threading.Event()
        instance, table = self._real_pool_logger(gate)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        gate.set()
        assert instance.shutdown() == 0
        assert table.update_item.called

    def test_the_drain_is_bounded_and_reports_what_it_left(self):
        # An unbounded wait here would hold a sub-agent's exit open behind a stuck
        # write while the user waits on the orchestrator's answer.
        gate = threading.Event()
        instance, table = self._real_pool_logger(gate)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        try:
            with patch("idp_common.metrics.put_metric") as put_metric:
                assert instance.shutdown(timeout=0.05) == 1
            assert not table.update_item.called
            # Counted, not only logged: an operator cannot be paged on a log line.
            put_metric.assert_called_once_with("AgentTranscriptDrainIncomplete", 1)
        finally:
            gate.set()

    def test_an_unfinished_drain_is_not_counted_as_a_dropped_message(self):
        # The metric the retry loop uses means "this message is gone". Stopping the
        # wait does not cancel the write -- the assertion below shows it committing
        # after the hold is lifted -- so counting it there would page whoever alarmed
        # on a destroyed transcript every time an environment froze and thawed.
        gate = threading.Event()
        instance, table = self._real_pool_logger(gate)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        with patch("idp_common.metrics.put_metric") as put_metric:
            instance.shutdown(timeout=0.05)
        published = {call.args[0] for call in put_metric.call_args_list}
        assert "AgentTranscriptMessageDropped" not in published
        gate.set()
        # The write the drain gave up on: it was abandoned, not cancelled.
        instance.executor.shutdown(wait=True)
        assert table.update_item.called

    def test_one_datum_carries_the_count_rather_than_one_call_per_message(self):
        # Reporting is on the far side of the deadline, and `put_metric` is a
        # synchronous CloudWatch call under a module lock, so a call per message
        # would add to the latency the bound exists to cap. Sum is the same either
        # way.
        gate = threading.Event()
        instance, _ = self._real_pool_logger(gate)
        for _ in range(3):
            instance.log_message_async("job-1", "user-1", {"role": "user"})
        try:
            with patch("idp_common.metrics.put_metric") as put_metric:
                assert instance.shutdown(timeout=0.05) == 3
            put_metric.assert_called_once_with("AgentTranscriptDrainIncomplete", 3)
        finally:
            gate.set()

    def test_a_drain_with_nothing_queued_returns_immediately(self):
        instance, _ = self._real_pool_logger()
        assert instance.shutdown() == 0

    def test_shutdown_is_repeatable(self):
        # Called from IDPAgent.__exit__, which a caller may reach twice.
        instance, _ = self._real_pool_logger()
        instance.shutdown()
        assert instance.shutdown() == 0

    def test_a_second_shutdown_does_not_re_report_the_same_write(self):
        # `shutdown` is public and is now the call people reach for, so a repeat is
        # plausible -- and double-counting would inflate the one number that is meant
        # to say how many transcripts are at risk.
        gate = threading.Event()
        instance, _ = self._real_pool_logger(gate)
        instance.log_message_async("job-1", "user-1", {"role": "user"})
        try:
            with patch("idp_common.metrics.put_metric") as put_metric:
                assert instance.shutdown(timeout=0.05) == 1
                assert instance.shutdown(timeout=0.05) == 0
            assert put_metric.call_count == 1
        finally:
            gate.set()

    def test_submitting_after_shutdown_raises_rather_than_silently_dropping(self):
        # The submit-time failure the tracker's guards absorb. It is reachable
        # precisely because shutdown now has a production caller.
        instance, _ = self._real_pool_logger()
        instance.shutdown()
        with pytest.raises(RuntimeError):
            instance.log_message_async("job-1", "user-1", {"role": "user"})


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
        # after shutdown — which is the exposure the missing guard had, and which
        # IDPAgent.__exit__ calling shutdown makes reachable: a throttling event that
        # arrives after the agent's context has closed lands here.
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

    def test_shutdown_drains_the_logger_with_a_bound(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        tracker.shutdown()
        # The bound is the part worth pinning. Passing no timeout through would
        # restore an unbounded wait at a sub-agent's exit, with the user waiting.
        assert logger_cls.return_value.shutdown.call_args.kwargs["timeout"] is not None

    def test_shutdown_reports_what_the_drain_could_not_finish(self):
        with patch(f"{MODULE}.DynamoDBMessageLogger") as logger_cls:
            tracker = DynamoDBMessageTracker("job-1", "user-1", table_name="t")
        logger_cls.return_value.shutdown.return_value = 3
        assert tracker.shutdown() == 3

    def test_shutdown_is_safe_on_a_disabled_tracker(self):
        assert DynamoDBMessageTracker("job-1", "user-1", enabled=False).shutdown() == 0
