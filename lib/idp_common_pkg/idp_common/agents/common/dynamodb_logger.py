# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB integration for logging agent messages asynchronously.
"""

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

from idp_common.ddb_numbers import coerce_int

logger = logging.getLogger(__name__)

# Guards the stored transcript against a concurrent append. Every writer reads
# this value, appends one message, and writes both attributes back under a
# condition on the value it read, so a write that overlapped another writer's
# fails instead of overwriting it. Absent on records written before the guard
# existed; the first append to such a record adds it.
_VERSION_ATTRIBUTE = "agent_messages_version"

# Every stored message carries a `sequence_number`, and it is its 1-based position
# in the stored transcript: the writer sets it to `len(stored) + 1` from the array
# it just read, inside the conditional-write loop below.
#
# Deriving it there is what makes it unique per job rather than per writer. Each
# sub-agent in a turn builds its own logger on the same record (see the
# `max_workers` docstring), so an ordinal counted on the instance restarts at one
# for each of them and the same value names several different messages in one
# transcript. There is no instance counter here for that reason.
#
# It costs no extra round trip and needs no second source of truth, because the
# read it derives from is the read the conditional write already has to do, and the
# condition is what makes the length exact: a write commits only if nobody appended
# between the read and the write, so no two committed messages can compute the same
# length. A rejected append recomputes it on the fresh read, and a dropped message
# consumes no ordinal -- so among messages written by *this* code the stored series is
# 1..N with no duplicates and no gaps.
#
# Two ways a stored transcript can still hold a duplicate or a gap, neither of them a
# defect in the derivation. A transcript written before this change carries whatever
# its per-logger counters produced, and appends continue from its length rather than
# repairing it. And during the Lambda version rollover -- the same window the
# conditional write's own guard cannot close -- a writer still running pre-upgrade code
# appends without touching the version attribute, so the condition still holds for a
# new writer and the two can land on the same ordinal or skip one. That window closes
# without intervention.
#
# This is deliberately *not* a total order the transcript is sorted by. Nothing
# sorts on it: the writer appends, so the stored array is already in commit order,
# and the UI renders that order as it is (`AgentMessagesDisplay.tsx` maps over the
# parsed array). What the field is for is naming one message unambiguously -- in a
# log line, in a support question, or by a future consumer -- and a position in the
# stored array is the strongest available version of that. It is a position in the
# stored array and not a row number on screen: the UI drops messages with empty
# content and splits a mixed text-plus-tool-use assistant message into several rows,
# so rendered rows and stored entries are not in general one to one.
_SEQUENCE_ATTRIBUTE = "sequence_number"

# How many times one message may rebuild its append after losing a conflict.
#
# This is a probability, not a guarantee. Writers are not in lockstep rounds: a
# loser re-reads while the others are mid-cycle, so each attempt races the same
# uniform field and succeeds with probability roughly 1/N for N concurrent
# writers. The chance of exhausting the budget is therefore about (1-1/N)**N_ATTEMPTS
# rather than zero, and re-reading does not lower it -- a round trip paces the
# loop in wall-clock terms but puts the loser back into the same race.
#
# Jittered backoff is what lowers it, by de-phasing the losers so they do not
# re-enter together. Measured on a harness that models the production shape --
# one logger per sub-agent, each at width 1, all on one record, 5 ms symmetric
# latency on get_item and update_item, reads snapshotted at call time and the
# condition evaluated atomically. Appends kept out of those submitted, median of
# the repeats:
#
#     writers   submitted   no condition   condition, no backoff   this code
#           2          12              6                      12          12
#           3          18              6                      17          18
#           4          24              6                      22          24
#           6          30              5                      25          30
#           8          32              4                      25          32
#          12          36              3                      25          36
#
# The middle column is what a condition alone buys: it ends the silent overwrite
# outright -- the left column is not a smaller loss but a different failure, where
# all but one writer's work is discarded -- and then plateaus near 25 as the
# writers keep colliding in phase. The right column is 5/5 repeats at every width.
#
# Jitter closes the residual outright up to eight writers. At twelve -- the
# realistic worst case, see the `max_workers` docstring below -- ten attempts left
# a message short in half of ten repeats (33-36 kept); fifteen kept all 36 in
# 10/10, and twenty added nothing over fifteen. Raising the backoff cap instead of
# the bound did not close it (100 ms cap, ten attempts: 7/10), which is the
# expected shape, since the cap bounds how long a loser waits and the bound is how
# many chances it gets. Hence fifteen. Extra attempts cost nothing when there is
# no contention: the loop exits on the first successful write.
_MAX_APPEND_ATTEMPTS = 15

# Full-jitter backoff after a lost conflict: sleep for a uniform draw from
# [0, min(cap, base * 2**(attempt-1))). Full jitter rather than a fixed sleep
# because de-phasing is the entire point -- equal sleeps would move the collision
# rather than break it up.
#
# The cap is deliberately short, and it is what makes the bounded drain below
# affordable. A retrying write is only safe for as long as something waits for it:
# `shutdown` waits `_DRAIN_TIMEOUT_SECONDS`, and a full ladder of fifteen attempts has
# to fit inside that with room for the queue behind it. Summing the per-attempt bounds
# over the fourteen conflict attempts that sleep -- the fifteenth does not -- gives
# 0.575 s, so it does. Trading a longer tail for a lower conflict rate would buy the
# lower rate with writes the drain then abandons, which is paying in the currency the
# retry is trying to save.
_BACKOFF_BASE_SECONDS = 0.005
_BACKOFF_CAP_SECONDS = 0.050

# A read failure gets its own budget, kept separate from the conflict budget
# above, because the two failure modes are unrelated and sharing one budget lets
# either starve the other: ten fast-failing reads would exhaust it having
# attempted no write at all, and five read failures plus five conflicts would
# drop a message that neither alone would have.
_MAX_READ_ATTEMPTS = 3

# Read errors that cannot succeed on a retry, so the append stops rather than
# spending its read budget and logging the same warning three times. A denylist
# rather than an allowlist of retryable codes: an unrecognised code may well be
# transient, and boto3's own retry handler has already exhausted its attempts on
# the known-transient ones (throttling, 5xx) before a ClientError surfaces here,
# which is why the local read budget above is small.
_PERMANENT_READ_ERRORS = frozenset(
    {
        "AccessDeniedException",
        "ValidationException",
        "SerializationException",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "ExpiredTokenException",
    }
)


# Emitted once per message the append gave up on: the conflict retries running out,
# or the read failing repeatedly. In both cases the message is gone. The log line
# beside each names the job and the message, but a log line is not alarmable, and not
# being alarmable is how the original defect stayed invisible for as long as it did.
# Alarm on this in the stack's own metric namespace.
_DROPPED_MESSAGE_METRIC = "AgentTranscriptMessageDropped"

# Emitted for writes the bounded drain in `shutdown` could not finish, and kept
# **separate** from the metric above rather than folded into it, because the two do
# not mean the same thing and one alarm cannot carry both.
#
# `shutdown` stops waiting; it does not cancel. `executor.shutdown(wait=False)` leaves
# queued work in place, and Lambda resumes unfinished background work if that
# execution environment is thawed for another invocation -- so a write counted here
# frequently *does* commit, just later than the session reading the transcript. It is
# lost only if the environment is reclaimed instead of reused. Counting that on
# `AgentTranscriptMessageDropped` would page whoever alarmed on "messages were
# destroyed" every time an environment froze and thawed.
_DRAIN_INCOMPLETE_METRIC = "AgentTranscriptDrainIncomplete"

# How long `shutdown` waits for queued and in-flight writes before giving up on
# them and reporting what is left. A bound rather than `wait=True`, because this
# runs at an agent's context-manager exit -- for a sub-agent, mid-turn while the
# user waits -- and an unbounded wait there puts a stuck write in front of the
# response.
#
# Sized from what is actually outstanding. The pool is width 1 and its queue is a
# `SimpleQueue`, so depth is unbounded in principle but bounded in practice by the
# producer: a message reaches the logger once per `MessageAddedEvent`, which follows
# a model or tool round trip, while a write costs two DynamoDB round trips. The
# consumer is the faster of the two by orders of magnitude, so the steady-state
# queue is empty and what a drain waits for is the write in flight plus anything
# that arrived during it. Two seconds covers a full contended ladder for that write
# -- fifteen attempts is thirty round trips (each attempt reads and writes) and at
# most 0.590 s of jittered backoff, being the sum of the per-attempt bounds for the
# fourteen conflict attempts that sleep plus the two read attempts that do -- with
# room for a few queued behind it.
#
# ⚠️ Two seconds bounds the *wait*, not the whole call. Reporting what the wait left
# behind publishes one metric datum, and `metrics.put_metric` is a synchronous
# `put_metric_data` under a module lock, so a drain that times out costs the two
# seconds plus that one CloudWatch round trip. It is one call and not one per message
# deliberately: the value published is the count, which the alarm sums identically.
_DRAIN_TIMEOUT_SECONDS = 2.0


def _describe(message_data: Dict[str, Any]) -> str:
    """
    Name a message in a log line about a write that did not commit.

    Not by ``sequence_number``: that is the message's position in the *stored*
    transcript, derived from the array the successful write appended to, so a
    message that was never stored has none. Role and timestamp are what it carries
    from the moment it is submitted.

    Args:
        message_data: The message the log line is about

    Returns:
        A short ``role=... timestamp=...`` fragment for interpolation.
    """
    return (
        f"role={message_data.get('role', 'unknown')} "
        f"timestamp={message_data.get('timestamp', 'unknown')}"
    )


class _TranscriptReadFailed(Exception):
    """A read of the stored transcript that may succeed if tried again.

    Raised so that the append retries instead of falling through. A read that
    failed cannot be treated as an empty transcript: appending to an assumed
    empty array overwrites every message already stored, and the write that
    does it reports success.

    Errors in ``_PERMANENT_READ_ERRORS`` are not raised as this, because no
    number of retries changes their answer.
    """


def _sleep_before_retry(attempt: int) -> None:
    """
    Sleep a jittered interval before rebuilding an append that lost a conflict.

    Full jitter -- a uniform draw from [0, bound) rather than the bound itself --
    because the purpose is to de-phase writers that collided, and a fixed sleep
    would move their collision instead of breaking it up.

    ``idp_common.utils.calculate_backoff`` is deliberately not reused here: it
    returns ``bound + uniform(0, 0.1 * bound)``, which is the right shape for
    easing off a service that is throttling (where the goal is to shed load) and
    the wrong one here, where a fixed set of contenders has to be spread out and a
    10% spread leaves them essentially in phase.

    Args:
        attempt: The attempt that just failed, 1-based.
    """
    bound = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    time.sleep(random.uniform(0, bound))  # nosec B311  # retry jitter, not a secret


class DynamoDBMessageLogger:
    """
    Asynchronous DynamoDB logger for agent messages.

    This class handles writing agent conversation messages to DynamoDB
    in a non-blocking manner to avoid impacting agent performance.
    """

    def __init__(self, table_name: str, max_workers: int = 1):
        """
        Initialize the DynamoDB message logger.

        Args:
            table_name: Name of the DynamoDB table
            max_workers: Maximum number of worker threads for async operations.
                Defaults to 1. One logger instance serves one (job, user) pair
                and so writes exactly one DynamoDB item, and DynamoDB serialises
                writes to a single item anyway, so additional workers buy no
                throughput and only turn overlapping appends into conflict
                retries. What the pool is for is keeping the round trip off the
                agent's critical path: ``submit`` returns immediately at any
                width, and a queue of depth one still does that. Correctness does
                not rest on this value: the append is guarded by a conditional
                write, which holds for any pool width and across processes.

                Narrowing the pool is not by itself a fix, and the numbers that
                matter are the ones across logger instances rather than within
                one. Every sub-agent builds its own tracker, its own logger and
                its own width-1 pool (``idp_agent.py`` ``_setup_monitoring`` ->
                ``DynamoDBMessageTracker`` -> this class), all keyed on the same
                ``PK``/``SK``, and ``strands``' ``ConcurrentToolExecutor`` starts
                one task per tool call with no concurrency limit of its own. So
                the concurrent-writer count is the number of sub-agents in the
                turn, not this argument.

                **Nothing bounds that number.** Four sub-agents are registered in
                ``agents/factory/registry.py``, the UI offers a "Select All
                Agents" control, and the request handler does not limit how many
                may be selected -- so four is today's practical width, and each
                entry in the external-MCP credentials secret adds another
                registered agent on top of it. The measurement above runs out to
                twelve for that headroom rather than because twelve is reachable
                by a known route: ``agent_processor`` retries the whole workflow up
                to three times, but each attempt's agents are entered with ``with``
                and so drain on the way out (see ``shutdown``), which is what stops
                a later attempt's loggers contending with an earlier attempt's.

                ⚠️ **Only the analytics path has loggers at all.** A tracker needs
                ``job_id`` and ``user_id`` -- ``_setup_monitoring`` returns early
                without them -- and ``AGENT_TABLE`` to write to. Agent chat passes
                neither identifier, and ``AGENT_TABLE`` is set on exactly two
                functions in ``template.yaml``: ``AgentProcessorFunction``, which is
                this path, and ``AgentCoreMCPHandlerFunction``, which builds no agent.
                So everything here concerns ``agent_processor`` -- its top-level agent
                and the sub-agents the orchestrator runs for it -- and agent chat
                stores no transcript to lose.

                The pool is drained at the boundary where the agent is finished
                with: ``IDPAgent.__exit__`` calls the tracker's ``shutdown``, which
                calls this class's. Both places an ``IDPAgent`` is entered on the
                analytics path reach it -- the ``with agent:`` in ``agent_processor``
                and a sub-agent's ``with specialized_agent:`` in the orchestrator. The
                drain is **bounded** (``_DRAIN_TIMEOUT_SECONDS``), and anything still
                outstanding when the bound expires is reported on the
                ``AgentTranscriptDrainIncomplete`` metric rather than waited on
                indefinitely.
        """
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(table_name)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        # Writes submitted but not yet finished, so that `shutdown` can wait for
        # exactly those and name whatever it could not finish. The pool's own queue
        # cannot answer that question: `ThreadPoolExecutor.shutdown` offers
        # `wait=True` or nothing, and there is no way to ask it what is left.
        #
        # A Condition rather than a poll loop: the done callback notifies, so a
        # drain that completes returns as soon as the last write does instead of at
        # the end of a sleep interval.
        self._pending: Dict[Future, tuple[str, Dict[str, Any]]] = {}
        self._pending_changed = threading.Condition()

        logger.info(f"DynamoDB message logger initialized for table: {table_name}")

    def log_message_async(
        self, job_id: str, user_id: str, message_data: Dict[str, Any]
    ) -> None:
        """
        Asynchronously log a message to DynamoDB.

        The message's ``sequence_number`` is **not** assigned here. It is its
        position in the stored transcript and is computed by the write from the
        array it reads, so that it is unique across every logger writing this job
        rather than counted per instance (see ``_SEQUENCE_ATTRIBUTE``). The caller's
        dict is therefore left untouched and the write works on a copy -- assigning
        the ordinal on the shared object would mutate it from a worker thread, and
        would do so more than once if the append lost a conflict and rebuilt.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            message_data: The message data to log
        """
        # Submit the write operation to the thread pool
        queued = dict(message_data)
        future = self.executor.submit(
            self._write_message_to_dynamodb, job_id, user_id, queued
        )

        # Recorded before the callback is attached: with a synchronous executor the
        # callback fires during add_done_callback, and an entry added afterwards
        # would never be removed.
        with self._pending_changed:
            self._pending[future] = (job_id, queued)

        # Add error callback
        future.add_done_callback(self._handle_write_result)

    def _read_transcript(
        self, job_id: str, user_id: str, pk: str, sk: str
    ) -> Optional[tuple[list, int]]:
        """
        Read the stored transcript and the version guarding it.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            pk: The partition key of the job record
            sk: The sort key of the job record

        Returns:
            A ``(messages, version)`` pair, where ``version`` is 0 if the record
            carries no version attribute yet, or ``None`` in place of the pair
            when the table itself is gone and there is nothing to append to.

            Note that a *missing item* is not that case and does not come back as
            ``None``: ``get_item`` answers a key that is not present with an empty
            response and no error, which reads here as an empty transcript and
            version 0, and the conditional write then creates the record.
            ``ResourceNotFoundException`` from ``get_item`` means the **table**
            does not exist.

        Raises:
            _TranscriptReadFailed: the read failed in a way that may not recur, so
                the append must retry rather than continue with an empty
                transcript. Codes in ``_PERMANENT_READ_ERRORS`` are re-raised
                instead, since retrying cannot change their answer.
        """
        try:
            response = self.table.get_item(Key={"PK": pk, "SK": sk})
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                # get_item answers a missing *item* with an empty response, so this
                # is the table being absent, not the job record.
                logger.warning(
                    f"DynamoDB table {self.table_name} does not exist; "
                    f"cannot store messages for job {job_id}, user {user_id}"
                )
                return None
            if error_code in _PERMANENT_READ_ERRORS:
                # Retrying spends the read budget and logs the same warning each
                # time for an answer that cannot change. Let it out to the outer
                # handler, which logs it once and drops the message.
                raise
            raise _TranscriptReadFailed(str(e)) from e

        item = response.get("Item", {})
        existing_messages_str = item.get("agent_messages", "[]")

        # Parse existing messages
        try:
            existing_messages = json.loads(existing_messages_str)
            if not isinstance(existing_messages, list):
                existing_messages = []
        except json.JSONDecodeError:
            logger.warning(
                f"Invalid JSON in agent_messages for job {job_id}, starting fresh"
            )
            existing_messages = []

        return existing_messages, coerce_int(item.get(_VERSION_ATTRIBUTE))

    def _append_conditionally(
        self, pk: str, sk: str, messages: list, version: int
    ) -> bool:
        """
        Write the transcript back, but only if nobody else wrote since the read.

        Args:
            pk: The partition key of the job record
            sk: The sort key of the job record
            messages: The full transcript to store, new message included
            version: The version observed by the read this write is based on

        Returns:
            True if the write committed, False if another writer got there first
            and the append has to be rebuilt on a fresh read.
        """
        try:
            self.table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=(
                    f"SET agent_messages = :messages, "
                    f"{_VERSION_ATTRIBUTE} = :next_version"
                ),
                # The first clause admits a record written before the guard
                # existed; two writers racing to add it still conflict, because
                # whichever commits first makes the attribute exist.
                ConditionExpression=(
                    f"attribute_not_exists({_VERSION_ATTRIBUTE}) "
                    f"OR {_VERSION_ATTRIBUTE} = :expected_version"
                ),
                ExpressionAttributeValues={
                    ":messages": json.dumps(messages),
                    ":expected_version": version,
                    ":next_version": version + 1,
                },
                ReturnValues="NONE",
            )
            return True
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return False
            raise

    def _publish_failure_metric(self, metric: str, job_id: str, count: int = 1) -> None:
        """
        Record a transcript-write failure on an alarmable metric.

        The ``logger.error`` beside every call names the job and identifies the
        message by role and timestamp, which is what an operator needs once they are
        already looking. It is not something they can be paged on, and the transcript
        losing entries with nothing to notice it is the whole reason #1098 survived as
        long as it did.

        Emitted with no dimensions, matching the convention of the other failure
        counters in the stack's namespace (``StaleOutputPurgeFailed``,
        ``AssessmentConfidenceUnavailable``). Swallows everything: this runs on the
        failure path of a best-effort logger, and a telemetry failure here must not add
        a second failure to the one being reported.

        Args:
            metric: ``_DROPPED_MESSAGE_METRIC`` for a message the append gave up on,
                ``_DRAIN_INCOMPLETE_METRIC`` for one the drain could not finish. They
                are separate because only the first means the message is gone.
            job_id: The analytics job the affected message belonged to
            count: How many messages this datum stands for. One call carrying the
                count rather than ``count`` calls carrying 1, because ``put_metric``
                is synchronous and the alarm sums the datum either way.
        """
        try:
            from idp_common import metrics

            metrics.put_metric(metric, count)
        except Exception as e:
            logger.warning(
                f"Could not publish {metric} for job {job_id}: {e}. "
                f"The outcome it reports is unchanged and is logged above."
            )

    def _count_dropped_message(self, job_id: str) -> None:
        """
        Record that a message could not be stored at all.

        Args:
            job_id: The analytics job the dropped message belonged to
        """
        self._publish_failure_metric(_DROPPED_MESSAGE_METRIC, job_id)

    def _write_message_to_dynamodb(
        self, job_id: str, user_id: str, message_data: Dict[str, Any]
    ) -> None:
        """
        Append a message to the transcript stored as a JSON string.

        The transcript is one attribute holding the whole array, so appending
        means reading it, growing it by one and writing it back. That is only
        safe if an overlapping writer is detected, so the write is conditional
        on a version attribute read alongside the transcript: a write whose
        version no longer matches is rejected by DynamoDB and rebuilt on a fresh
        read, rather than silently replacing the other writer's message. Rejection
        is exact for any number of threads and for concurrent processes; what is
        probabilistic is whether a rejected append finds a gap within its budget,
        which is what the backoff is for and what the measurement above quantifies.

        The message's ``sequence_number`` is assigned here rather than at submit
        time, as ``len(stored) + 1`` of the array this attempt read. The condition is
        what makes that exact: the write commits only if nothing appended in
        between, so no two committed messages can have computed the same length, and
        the ordinal is unique across every logger writing this record instead of
        restarting at one for each of them. It is recomputed on a rebuilt attempt,
        which is why the write works on its own copy of the message.

        A message that cannot be appended is dropped, logged and counted on the
        ``AgentTranscriptMessageDropped`` metric. Overwriting the stored transcript
        to force it through would trade one lost message for all of them.

        Two budgets, not one. Conflicts and read failures are unrelated failures,
        and a shared budget lets either starve the other: ten fast-failing reads
        would exhaust it having attempted no write, and a mixture would drop a
        message that neither alone would have.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            message_data: The message data to log
        """
        try:
            pk = f"agent#{user_id}"
            sk = job_id
            # Two counters rather than one loop variable, precisely so that a read
            # failure cannot spend a conflict attempt. A `for attempt in
            # range(...)` with `continue` on a read failure would: `continue`
            # advances the iteration, which is the shared budget this avoids. Each
            # counter alone bounds the loop, so it always terminates.
            conflicts_left = _MAX_APPEND_ATTEMPTS
            reads_left = _MAX_READ_ATTEMPTS

            while conflicts_left > 0:
                try:
                    read = self._read_transcript(job_id, user_id, pk, sk)
                except _TranscriptReadFailed as e:
                    reads_left -= 1
                    read_attempt = _MAX_READ_ATTEMPTS - reads_left
                    if reads_left <= 0:
                        logger.error(
                            f"Could not read existing messages for job {job_id} "
                            f"after {_MAX_READ_ATTEMPTS} attempts; message "
                            f"({_describe(message_data)}) was not persisted: {e}"
                        )
                        self._count_dropped_message(job_id)
                        return
                    logger.warning(
                        f"Could not read existing messages for job {job_id} "
                        f"(read attempt {read_attempt} of {_MAX_READ_ATTEMPTS}), "
                        f"retrying: {e}"
                    )
                    # Backed off like a conflict is: a read that failed because the
                    # item is hot should not be retried in phase with every other
                    # writer's.
                    _sleep_before_retry(read_attempt)
                    continue

                if read is None:
                    return

                existing_messages, version = read

                # The ordinal is the message's position in the array just read, and
                # the condition on the write below is what makes it unique: a write
                # that commits proves nothing appended since the read.
                message_data[_SEQUENCE_ATTRIBUTE] = len(existing_messages) + 1

                if self._append_conditionally(
                    pk, sk, existing_messages + [message_data], version
                ):
                    logger.debug(
                        f"Successfully logged message for job {job_id}, sequence "
                        f"{message_data[_SEQUENCE_ATTRIBUTE]}"
                    )
                    return

                # Another writer committed between the read and the write. Back off
                # a jittered interval so the losers do not re-read in phase, then
                # loop to re-read so that the winner's message survives alongside
                # this one.
                conflicts_left -= 1
                attempt = _MAX_APPEND_ATTEMPTS - conflicts_left
                logger.debug(
                    f"Concurrent append detected for job {job_id} "
                    f"(attempt {attempt}), retrying"
                )
                if conflicts_left > 0:
                    _sleep_before_retry(attempt)

            logger.error(
                f"Gave up appending message for job {job_id} after "
                f"{_MAX_APPEND_ATTEMPTS} attempts; message "
                f"({_describe(message_data)}) was not persisted"
            )
            self._count_dropped_message(job_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                logger.warning(
                    f"DynamoDB table {self.table_name} not found for job {job_id}"
                )
            else:
                logger.error(f"DynamoDB error logging message for job {job_id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error logging message for job {job_id}: {e}")

    def log_throttling_event_async(
        self, job_id: str, user_id: str, throttling_info: Dict[str, Any]
    ) -> None:
        """
        Asynchronously log a throttling event to DynamoDB as a mock agent message.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            throttling_info: The throttling event information
        """
        # Create a mock agent message for throttling events
        # This allows the UI to identify and display throttling issues
        mock_message_data = {
            "timestamp": throttling_info.get("timestamp", datetime.now().isoformat()),
            "role": "system",
            "content": f"⚠️ Throttling Event: {throttling_info.get('error_code', 'Unknown')} - {throttling_info.get('error_message', 'Throttling occurred')}",
            "message_type": "throttling_event",
            "throttling_details": throttling_info,
        }

        # Log using the existing message logging mechanism
        self.log_message_async(job_id, user_id, mock_message_data)

    def _handle_write_result(self, future) -> None:
        """
        Handle the result of an async write operation.

        Also retires the future from the pending set and wakes any drain waiting on
        it. Both happen whether the write succeeded or raised, because a drain is
        waiting for the write to be *over*, not for it to have worked -- a write that
        raised has already been reported by the handlers inside it.

        Args:
            future: The completed Future object
        """
        with self._pending_changed:
            self._pending.pop(future, None)
            self._pending_changed.notify_all()

        try:
            future.result()  # This will raise any exception that occurred
        except Exception as e:
            logger.error(f"Async DynamoDB write failed: {e}")

    def shutdown(self, timeout: Optional[float] = _DRAIN_TIMEOUT_SECONDS) -> int:
        """
        Stop accepting writes and wait, for a bounded time, for the queued ones.

        This is the only thing that waits for a queued write. Writes are handed to a
        thread pool so that a DynamoDB round trip is not on the agent's critical path,
        and when a Lambda invocation returns the execution environment is **frozen**
        rather than shut down: the process is not signalled and does not exit, so
        nothing runs at that point on its own.

        **Why an interpreter-exit hook is not an alternative.** The mechanism that
        would flush the pool is not ``atexit.register`` but
        ``threading._register_atexit(_python_exit)`` in
        ``concurrent/futures/thread.py``, whose comment says it is used *instead of*
        ``atexit.register``; ``_python_exit`` wakes every live pool and then ``join``s
        every worker **with no timeout at all**. So the stdlib's own answer to this
        problem is an unbounded join, which is precisely what must not happen at an
        agent boundary. Reaching it needs an orderly interpreter shutdown, and neither
        end of a Lambda environment's life provides one: a freeze is not a process
        exit, and Python's *default* ``SIGTERM`` disposition terminates without running
        exit hooks of any kind -- so even a function whose extension earns it a
        ``SIGTERM`` would flush nothing without installing a handler.

        That last point is what makes the argument independent of which functions carry
        an extension, and worth keeping that way: ``AgentProcessorFunction`` carries one
        plain library layer today, but ``ChatStreamProcessorFunction`` attaches the AWS
        Lambda Web Adapter, which *is* an external extension, so a transcript logger
        extended onto the streaming chat path would get a ``SIGTERM`` and a 2000 ms
        shutdown budget. It still would not flush this pool.

        A frozen write is not immediately destroyed -- Lambda resumes unfinished
        background work if that environment is thawed for another invocation -- but
        that is not a guarantee of anything a user sees: the transcript stays
        incomplete for the session that is reading it now, and the work is lost
        outright if the environment is reclaimed instead of reused, which is the
        certain outcome for the last invocation before a scale-down.

        Bounded rather than ``wait=True``, because the caller is ``IDPAgent.__exit__``
        and for a sub-agent that is mid-turn, with the user waiting on the
        orchestrator's answer. What is left when the bound expires is logged and
        counted on ``AgentTranscriptDrainIncomplete`` -- **not** on
        ``AgentTranscriptMessageDropped``, because this call stops waiting without
        cancelling and those writes often commit on the next thaw, so counting them as
        dropped messages would page whoever alarmed on a destroyed transcript every
        time an environment froze.

        Closing the pool is the second thing this does, and it matters even when
        nothing is queued: a ``ThreadPoolExecutor``'s worker threads are not daemon
        threads and idle on the work queue until shutdown, so a logger that is never
        shut down leaves one live thread per sub-agent for the remaining life of a
        warm execution environment.

        After this returns, ``executor.submit`` raises ``RuntimeError``, which is the
        submit-time failure the guards in ``DynamoDBMessageTracker`` absorb. A
        message emitted by an agent after its context has exited is therefore logged
        and kept in the tracker's local list rather than raised into the agent. That
        is also why the drain is here and not on ``AfterInvocationEvent``, which the
        hook registry does offer for teardown: an agent that is called twice would
        have its transcript silently stop after the first call.

        Args:
            timeout: Seconds to wait for outstanding writes. ``None`` waits
                indefinitely, which is the right choice only for a caller that owns
                the process and has nothing waiting on it.

        Returns:
            The number of writes still outstanding when the wait ended -- 0 for a
            complete drain. Each is reported once: a second ``shutdown`` returns 0
            rather than re-reporting the same writes.
        """
        logger.info("Shutting down DynamoDB message logger")

        # wait=False so that the wait below is the bounded one. Queued work keeps
        # running; what this call does immediately is refuse new submissions.
        self.executor.shutdown(wait=False)

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._pending_changed:
            while self._pending:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                self._pending_changed.wait(remaining)
            # Taken out of `_pending` under the same lock that read it, so that a
            # second shutdown neither re-logs nor re-counts these. `shutdown` is
            # public and is now the thing callers reach for, and a write reported
            # twice would inflate the alarm that is supposed to say how many
            # transcripts are at risk.
            unfinished = list(self._pending.values())
            self._pending.clear()

        for job_id, message_data in unfinished:
            logger.error(
                f"Agent transcript write for job {job_id} did not finish within "
                f"{timeout}s of the agent closing; message "
                f"({_describe(message_data)}) may not be persisted"
            )
        if unfinished:
            # One datum carrying the count, published after the log lines: the wait is
            # what the timeout bounds, and a synchronous CloudWatch call per message
            # would add to the very latency the bound exists to cap.
            self._publish_failure_metric(
                _DRAIN_INCOMPLETE_METRIC, unfinished[0][0], len(unfinished)
            )

        return len(unfinished)


class DynamoDBMessageTracker:
    """
    Message tracker that integrates with DynamoDB logging.

    This class combines the MessageTracker functionality with
    DynamoDB logging capabilities and throttling monitoring.
    """

    def __init__(
        self,
        job_id: str,
        user_id: str,
        table_name: Optional[str] = None,
        enabled: bool = True,
        include_debug_tool_output: bool = False,
    ):
        """
        Initialize the DynamoDB message tracker.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            table_name: DynamoDB table name (defaults to ANALYTICS_TABLE env var)
            enabled: Whether monitoring is enabled
            include_debug_tool_output: Whether to include debug_tool_output in tool result messages
        """
        self.job_id = job_id
        self.user_id = user_id
        self.enabled = enabled
        self.include_debug_tool_output = include_debug_tool_output
        self.messages = []
        self.logger = logging.getLogger(f"{__name__}.DynamoDBMessageTracker")

        if not enabled:
            self.logger.info("Agent monitoring disabled")
            self.db_logger = None
            self.throttling_monitor = None
            return

        # Get table name from environment if not provided
        if table_name is None:
            table_name = os.environ.get("AGENT_TABLE")

        if not table_name:
            self.logger.error(
                "No DynamoDB table name provided and AGENT_TABLE env var not set"
            )
            self.enabled = False
            self.db_logger = None
            self.throttling_monitor = None
            return

        try:
            self.db_logger = DynamoDBMessageLogger(table_name)

            # Initialize throttling monitor with the same DB logger
            from .monitoring import ThrottlingMonitor

            self.throttling_monitor = ThrottlingMonitor(
                job_id=job_id, user_id=user_id, db_logger=self.db_logger
            )

            self.logger.info(f"DynamoDB message tracker initialized for job {job_id}")
        except Exception as e:
            self.logger.error(f"Failed to initialize DynamoDB logger: {e}")
            self.enabled = False
            self.db_logger = None
            self.throttling_monitor = None

    def register_hooks(self, registry, **kwargs: Any) -> None:
        """Register the message tracking and throttling monitoring callbacks."""
        if not self.enabled:
            return

        from strands.hooks.events import MessageAddedEvent

        # Register message tracking
        registry.add_callback(MessageAddedEvent, self.on_message_added)

        # Register throttling monitoring if available
        if self.throttling_monitor:
            self.throttling_monitor.register_hooks(registry, **kwargs)
            # Override the throttling monitor's exception handler to also log to DynamoDB as agent messages
            self.throttling_monitor._handle_model_exception = (
                self._handle_throttling_with_agent_message
            )

    def _handle_throttling_with_agent_message(self, exception: Exception) -> None:
        """Handle throttling exceptions and log them as agent messages."""
        from botocore.exceptions import ClientError

        # Initialize variables for error details
        error_code = "Unknown"
        error_message = str(exception)

        if isinstance(exception, ClientError):
            error_code = exception.response.get("Error", {}).get("Code", "")
            error_message = exception.response.get("Error", {}).get("Message", "")
        else:
            # For non-ClientError exceptions, extract error info from string
            exception_str = str(exception)

            # Try to extract error code from the exception string
            if "ThrottlingException" in exception_str:
                error_code = "ThrottlingException"
            elif "ModelThrottledException" in exception_str:
                error_code = "ModelThrottledException"
            elif "ServiceQuotaExceededException" in exception_str:
                error_code = "ServiceQuotaExceededException"
            elif "RequestLimitExceeded" in exception_str:
                error_code = "RequestLimitExceeded"
            else:
                error_code = type(exception).__name__

        # Check if this is a throttling-related error
        throttling_patterns = [
            "ThrottlingException",
            "ModelThrottledException",
            "ServiceQuotaExceededException",
            "RequestLimitExceeded",
            "Too many requests",
            "throttl",  # catch variations
        ]

        exception_str = str(exception)
        is_throttling = any(
            pattern.lower() in exception_str.lower() for pattern in throttling_patterns
        )

        if is_throttling:
            # Create a mock agent message for the throttling event
            throttling_message_data = {
                "timestamp": datetime.now().isoformat(),
                "role": "exception",
                "content": f"⚠️ Throttling Event: {error_code} - {error_message}",
                "message_type": "throttling_exception",
                "throttling_details": {
                    "error_code": error_code,
                    "error_message": error_message,
                    "exception_type": type(exception).__name__,
                    "job_id": self.job_id,
                    "user_id": self.user_id,
                },
            }

            # Store locally
            self.messages.append(throttling_message_data)

            # Log to DynamoDB asynchronously. Guarded, because this handler is
            # installed over ThrottlingMonitor._handle_model_exception, which guards
            # its own write: without this, attaching the DynamoDB logger would remove
            # a guard rather than add a feature.
            #
            # What the guard covers is failure to *submit* the write, not the write
            # itself: log_message_async hands the work to a ThreadPoolExecutor, and the
            # write's own errors are already absorbed by _handle_write_result, which
            # calls future.result() inside its own try. So the exposure is
            # executor.submit raising, and the way it raises is RuntimeError after
            # shutdown — which IDPAgent.__exit__ calls, so a throttling event arriving
            # after this agent's context has closed lands here. The local append above
            # is the copy that matters and has already happened.
            if self.db_logger:
                try:
                    self.db_logger.log_message_async(
                        self.job_id, self.user_id, throttling_message_data
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to log throttling event to DynamoDB for job {self.job_id}: {e}"
                    )

            # Log to CloudWatch
            self.logger.warning(
                f"🚫 Throttling detected for job {self.job_id}: {error_code} - {error_message}"
            )
            self.logger.info(
                f"Throttling event logged to DynamoDB as agent message for job {self.job_id}"
            )

    def on_message_added(self, event) -> None:
        """Handle message added events."""
        if not self.enabled or not self.db_logger:
            return

        try:
            from .monitoring import AgentMonitor

            # Create a temporary monitor instance to use its message parsing logic
            temp_monitor = AgentMonitor(
                include_debug_tool_output=self.include_debug_tool_output
            )
            message_data = {
                "timestamp": datetime.now().isoformat(),
                **temp_monitor._get_message_preview(event.message),
            }

            # Store locally
            self.messages.append(message_data)

            # Log to DynamoDB asynchronously
            self.db_logger.log_message_async(self.job_id, self.user_id, message_data)

            # Log the message
            role = message_data["role"]
            content_preview = (
                message_data["content"][:100] + "..."
                if len(message_data["content"]) > 100
                else message_data["content"]
            )
            self.logger.info(
                f"📝 Message tracked: [{role}] {content_preview} (Total: {len(self.messages)})"
            )

        except Exception as e:
            self.logger.error(f"Error tracking message: {e}")

    def get_messages(self) -> list:
        """Get all tracked messages."""
        return self.messages.copy()

    def get_throttling_events(self) -> list:
        """Get all throttling events if throttling monitor is available."""
        if self.throttling_monitor:
            return self.throttling_monitor.get_throttling_events()
        return []

    def clear_messages(self) -> None:
        """Clear all tracked messages."""
        self.messages.clear()

    def shutdown(self, timeout: Optional[float] = _DRAIN_TIMEOUT_SECONDS) -> int:
        """
        Drain the transcript writes and release the tracker's resources.

        Called from ``IDPAgent.__exit__``, which is the boundary at which the agent
        this tracker belongs to is finished with. See
        ``DynamoDBMessageLogger.shutdown`` for why a bounded drain there is what
        makes a queued write reliable at all.

        Args:
            timeout: Seconds to wait for outstanding writes, or ``None`` to wait
                indefinitely.

        Returns:
            The number of writes still outstanding when the wait ended, and 0 for a
            tracker with no logger (monitoring disabled, or its setup failed).
        """
        if self.db_logger:
            return self.db_logger.shutdown(timeout=timeout)
        return 0
