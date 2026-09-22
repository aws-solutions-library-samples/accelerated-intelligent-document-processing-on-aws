# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB integration for logging agent messages asynchronously.
"""

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
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
# The cap is deliberately short. These writes run on a pool that nothing drains
# in production (see the `max_workers` docstring), so a long backoff widens the
# window in which a retrying write is abandoned when the execution environment
# freezes. Trading a longer tail for a lower conflict rate would be paying in the
# same currency the retry is trying to save.
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


# Emitted once per message that could not be stored. The log line names the job
# and sequence number, but a log line is not alarmable, and not being alarmable is
# how the original defect stayed invisible for as long as it did. Alarm on this in
# the stack's own metric namespace.
_DROPPED_MESSAGE_METRIC = "AgentTranscriptMessageDropped"


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
                may be selected -- so four is today's practical width. It is not a
                ceiling: each entry in the external-MCP credentials secret adds
                another registered agent, and ``agent_processor`` retries the whole
                workflow up to three times, building fresh loggers each attempt
                whose predecessors are still in flight. That puts the realistic
                worst case near twelve concurrent writers, which is why the
                measurement above runs out that far.

                ⚠️ Nothing drains this pool in production. ``shutdown`` does
                (``executor.shutdown(wait=True)``) and ``DynamoDBMessageTracker``
                calls it, but no production caller reaches the tracker's
                ``shutdown``: it is constructed and registered as a hook, and
                ``IDPAgent.__exit__`` closes only the MCP client. Queued and
                retrying writes are therefore abandoned when the execution
                environment freezes. That is a second way to lose a message on
                this attribute, tracked separately in issue #1110, and it is the
                reason the retry backoff below is capped short rather than long.
        """
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(table_name)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.sequence_counter = 0

        logger.info(f"DynamoDB message logger initialized for table: {table_name}")

    def log_message_async(
        self, job_id: str, user_id: str, message_data: Dict[str, Any]
    ) -> None:
        """
        Asynchronously log a message to DynamoDB.

        Args:
            job_id: The analytics job ID
            user_id: The user ID who owns the job
            message_data: The message data to log
        """
        # Add sequence number
        self.sequence_counter += 1
        message_data["sequence_number"] = self.sequence_counter

        # Submit the write operation to the thread pool
        future = self.executor.submit(
            self._write_message_to_dynamodb, job_id, user_id, message_data
        )

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

    def _count_dropped_message(self, job_id: str) -> None:
        """
        Record that a message could not be stored, on an alarmable metric.

        The ``logger.error`` beside every call names the job and sequence number,
        which is what an operator needs once they are already looking. It is not
        something they can be paged on, and the transcript losing entries with
        nothing to notice it is the whole reason #1098 survived as long as it did.

        Emitted with no dimensions and a value of 1, matching the convention of the
        other failure counters in the stack's namespace (``StaleOutputPurgeFailed``,
        ``AssessmentConfidenceUnavailable``). Swallows everything: this runs on the
        drop path of a best-effort logger, and a telemetry failure here must not add
        a second failure to the one being reported.

        Args:
            job_id: The analytics job the dropped message belonged to
        """
        try:
            from idp_common import metrics

            metrics.put_metric(_DROPPED_MESSAGE_METRIC, 1)
        except Exception as e:
            logger.warning(
                f"Could not publish {_DROPPED_MESSAGE_METRIC} for job {job_id}: {e}. "
                f"The message is still dropped and still logged above."
            )

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
                            f"after {_MAX_READ_ATTEMPTS} attempts; sequence "
                            f"{message_data.get('sequence_number')} was not "
                            f"persisted: {e}"
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

                if self._append_conditionally(
                    pk, sk, existing_messages + [message_data], version
                ):
                    logger.debug(
                        f"Successfully logged message for job {job_id}, sequence {message_data.get('sequence_number')}"
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
                f"{_MAX_APPEND_ATTEMPTS} attempts; sequence "
                f"{message_data.get('sequence_number')} was not persisted"
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
            "sequence_number": None,  # Will be set in log_message_async
        }

        # Log using the existing message logging mechanism
        self.log_message_async(job_id, user_id, mock_message_data)

    def _handle_write_result(self, future) -> None:
        """
        Handle the result of an async write operation.

        Args:
            future: The completed Future object
        """
        try:
            future.result()  # This will raise any exception that occurred
        except Exception as e:
            logger.error(f"Async DynamoDB write failed: {e}")

    def shutdown(self) -> None:
        """
        Shutdown the thread pool executor.
        """
        logger.info("Shutting down DynamoDB message logger")
        self.executor.shutdown(wait=True)


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
            # executor.submit raising — after shutdown, say — and since
            # DynamoDBMessageTracker.shutdown has no production caller today, that is
            # currently unreachable. The guard is here for symmetry with the method it
            # replaces rather than for a failure anyone has seen. The local append
            # above is the copy that matters and has already happened.
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

    def shutdown(self) -> None:
        """Shutdown the tracker and its resources."""
        if self.db_logger:
            self.db_logger.shutdown()
