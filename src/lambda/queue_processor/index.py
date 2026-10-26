# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder
from botocore.exceptions import BotoCoreError, ClientError

from idp_common.config import ConfigurationManager
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status
from idp_common.utils.execution_names import (  # noqa: F401 — re-exported
    _EXECUTION_NAME_MAX_LEN,
    _EXECUTION_NAME_UNSAFE,
    execution_name_for,
)
from idp_common.utils.execution_names import execution_arn_for as _execution_arn_for
from idp_common.utils.log_sanitizer import sanitize_event_for_logging
from idp_common.utils.transient_errors import is_transient_error

patch_all()

# Same pattern as idp_common.review_queues.finalize.FINALIZE_TOKEN_RE; kept local
# here so the handler (and its hermetic tests, which stub idp_common) needs no
# review_queues import on its hot path.
_FINALIZE_TOKEN_RE = re.compile(r"^rvw-[0-9a-f]{32}$")

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

sfn = boto3.client("stepfunctions")
sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")
document_service = create_document_service()
concurrency_table = dynamodb.Table(os.environ["CONCURRENCY_TABLE"])
state_machine_arn = os.environ["STATE_MACHINE_ARN"]
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
COUNTER_ID = "workflow_counter"
CIRCUIT_BREAKER_ID = "circuit_breaker"
CIRCUIT_BREAKER_ENABLED = (
    os.environ.get("CIRCUIT_BREAKER_ENABLED", "false").lower() == "true"
)
# The three states the circuit-breaker manager writes to the ConcurrencyTable
# (src/lambda/circuit_breaker_manager/index.py), plus the three this function
# reports when it never got as far as reading one. Named because the admission
# decision for each is asserted from this exact set in
# test_check_circuit_breaker.py: a new state that nobody decided about fails
# that test rather than inheriting whichever branch it happens to fall into.
CB_STATE_CLOSED = "CLOSED"
CB_STATE_OPEN = "OPEN"
CB_STATE_HALF_OPEN = "HALF_OPEN"
CB_STATE_DISABLED = "DISABLED"
#: The state read failed with something a retry can fix (throttle, timeout,
#: dropped connection, 5xx). Admission is REFUSED — see check_circuit_breaker.
CB_STATE_ERROR_TRANSIENT = "ERROR_TRANSIENT"
#: The state read failed with something a retry cannot fix (access denied, table
#: gone, malformed request). Admission is ALLOWED — see check_circuit_breaker.
CB_STATE_ERROR_TERMINAL = "ERROR_TERMINAL"
DOCUMENT_QUEUE_URL = os.environ.get("DOCUMENT_QUEUE_URL", "")
#: ``DynamoDBError.error_code`` for a refused condition. Spelled here rather than
#: imported from ``idp_common.dynamodb.client`` so the hermetic tests, which stub
#: ``idp_common``, compare against the real string.
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("RECOVERY_TIMEOUT_SECONDS", "300"))
# How many workflows HALF_OPEN admits per window, and how long that window is.
# A half-open breaker is supposed to admit a BOUNDED number of probes and keep
# everything else out; these two numbers are that bound. See
# ``_claim_half_open_probe`` for why the budget is per rolling window rather
# than per half-open episode, and ``check_circuit_breaker`` for why these values.
HALF_OPEN_PROBE_BUDGET = 5
HALF_OPEN_PROBE_WINDOW_SECONDS = 60
#: Attributes ``_claim_half_open_probe`` maintains on the breaker item. Only
#: this function reads or writes them; the manager's
#: ``update_circuit_breaker_state`` writes named attributes only, so it never
#: clobbers them, and the API resolver projects an explicit field list, so they
#: are inert to the UI.
HALF_OPEN_PROBE_COUNT_ATTR = "half_open_probes"
HALF_OPEN_PROBE_WINDOW_ATTR = "half_open_probe_window"
# How long a suspected counter leak must persist across two independent samples
# before it is corrected. Must comfortably exceed the window between an
# increment and its execution becoming visible to ListExecutions, since during
# that window fewer executions are RUNNING than the counter says — legitimately.
RECONCILE_GRACE_SECONDS = int(os.environ.get("RECONCILE_GRACE_SECONDS", "300"))
# Beyond this age a recorded drift sample is treated as belonging to a previous
# episode and discarded rather than acted on. Reconciliation only runs when an
# increment is refused, so a sample can outlive the condition that produced it.
RECONCILE_SAMPLE_MAX_AGE_SECONDS = int(
    os.environ.get("RECONCILE_SAMPLE_MAX_AGE_SECONDS", str(RECONCILE_GRACE_SECONDS * 4))
)
# Clamp: a correction requires GRACE <= sample age <= MAX_AGE. If MAX_AGE were
# the smaller of the two, that window is EMPTY — every sample gets discarded as
# stale (the `age > MAX_AGE` branch) before it can ever mature past GRACE, so
# `reconcile_counter` can never correct and a leaked counter is permanent. That
# is precisely the un-self-healing state this whole mechanism exists to escape,
# so a misconfiguration must not be able to reintroduce it silently.
#
# The default (GRACE * 4) is safe, but both are independent env vars and an
# operator raising one without the other is an easy mistake to make.
if RECONCILE_SAMPLE_MAX_AGE_SECONDS < RECONCILE_GRACE_SECONDS * 2:
    _clamped = RECONCILE_GRACE_SECONDS * 2
    logger.warning(
        f"RECONCILE_SAMPLE_MAX_AGE_SECONDS={RECONCILE_SAMPLE_MAX_AGE_SECONDS} is "
        f"below 2x RECONCILE_GRACE_SECONDS={RECONCILE_GRACE_SECONDS}, which would "
        f"leave no window in which a drift sample can mature — the counter could "
        f"never be reconciled. Clamping to {_clamped}s."
    )
    RECONCILE_SAMPLE_MAX_AGE_SECONDS = _clamped
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP")
# The workflow tracker's decrement is a TransactWriteItems on this same
# `workflow_counter` item, so a plain UpdateItem here can lose the race with
# TransactionConflictException — a code botocore's DynamoDB retry policy does not
# cover. Bounded retry, same shape as the tracker's DECREMENT_MAX_ATTEMPTS /
# 0.2s-doubling backoff (worst case 0.2 + 0.4 + 0.8 = 1.4s of sleep), because the
# real conflict rate is unmeasured. See update_counter.
COUNTER_CONFLICT_MAX_ATTEMPTS = int(
    os.environ.get("COUNTER_CONFLICT_MAX_ATTEMPTS", "4")
)
COUNTER_CONFLICT_BASE_DELAY_SECONDS = float(
    os.environ.get("COUNTER_CONFLICT_BASE_DELAY_SECONDS", "0.2")
)


class ExecutionAlreadyStarted(Exception):
    """StartExecution was refused because this message already started a workflow.

    Raised when Step Functions answers ``ExecutionAlreadyExists`` for the
    deterministic name derived from the SQS message: an earlier delivery of the
    same message already started the execution, so this delivery must be acked
    as a no-op rather than start a second one.
    """

    def __init__(self, execution_name: str, execution_arn: str):
        super().__init__(
            f"Execution {execution_name} already exists as {execution_arn}"
        )
        self.execution_name = execution_name
        self.execution_arn = execution_arn


class DocumentAbortedBeforeStart(Exception):
    """The document was aborted after this message read it, before its start.

    Raised by ``start_workflow`` when ``claim_start`` finds the document ABORTED
    at the moment the execution would have been started, so that no execution
    is created; ``process_message`` hands back the concurrency slot it took and
    acks the message.
    """


def claim_start(object_key: str) -> bool:
    """Whether the document may be started now: False only if it is ABORTED.

    ``process_message`` checks for ABORTED when it reads the message, but that
    read is eventually consistent and is taken before the circuit breaker, the
    counter increment and the configuration reads. ``abortWorkflow`` stops a
    QUEUED document by writing ABORTED and nothing else, so an abort landing
    after that read used to be started anyway, and its success then overwrote
    ABORTED (#1612). This is the same decision taken again, at the last moment,
    by a conditional write DynamoDB evaluates against the current item.

    Every other outcome allows the start, exactly as before this check existed:
    a document with no tracking record, and a claim or re-read that fails. A
    document that was not aborted is never held back by it.
    """
    try:
        if document_service.claim_for_execution(object_key):
            return True
    except Exception as e:
        logger.warning(
            f"Could not confirm that {object_key} was not aborted before starting "
            f"it: {e}. Starting it.",
            exc_info=True,
        )
        return True
    # Refused: the document is ABORTED, or it has no tracking record. Read it to
    # tell which -- consistently, since the write that refused the claim may be
    # the one an eventually consistent read would miss.
    try:
        current = document_service.get_document(object_key, consistent_read=True)
    except Exception as e:
        logger.warning(
            f"Start claim for {object_key} was refused and the document could not "
            f"be re-read: {e}. Starting it.",
            exc_info=True,
        )
        return True
    return not (current is not None and current.status == Status.ABORTED)


def aborted_now(object_key: str) -> bool:
    """Whether a consistent read finds the document ABORTED; False if it cannot.

    Used after a post-start write that failed for a reason other than its
    condition, which therefore said nothing about an abort in the window.
    """
    try:
        current = document_service.get_document(object_key, consistent_read=True)
    except Exception as e:
        logger.error(
            f"Could not re-read {object_key} after its post-start write failed: "
            f"{e}. If it was aborted while starting, its execution is not stopped.",
            exc_info=True,
        )
        return False
    return current is not None and current.status == Status.ABORTED


def stop_aborted_start(object_key: str, execution_arn: Optional[str]) -> bool:
    """Stop an execution whose document was aborted after its start claim.

    Called when the post-start tracking write is refused because the document
    is ABORTED. The execution exists and owns its concurrency slot, which the
    workflow tracker releases on the ABORTED event this stop produces, so
    nothing here touches the counter, and the document is left ABORTED.

    Returns whether the stop request was accepted. A failure is logged at ERROR
    and not retried: the message is already acked, and the document then reads
    ABORTED while its execution runs, which is the state #1612 described.
    """
    if not execution_arn:
        logger.error(
            f"Document {object_key} was aborted after its start claim, but the "
            f"execution ARN is unknown, so it could not be stopped."
        )
        return False
    try:
        sfn.stop_execution(
            executionArn=execution_arn,
            error="DocumentAborted",
            cause="The document was aborted while its workflow was being started",
        )
    except Exception as e:
        logger.error(
            f"Document {object_key} was aborted after its start claim, and "
            f"stopping its execution {execution_arn} failed: {e}. It reads "
            f"ABORTED while that execution runs.",
            exc_info=True,
        )
        return False
    logger.info(
        f"Document {object_key} was aborted while its start was in flight; "
        f"stopped execution {execution_arn} and left it ABORTED"
    )
    return True


def execution_arn_for(execution_name: str) -> str:
    """The ARN Step Functions gives an execution of our state machine by name."""
    return _execution_arn_for(state_machine_arn, execution_name)


def ack_message(receipt_handle: str) -> bool:
    """Delete one message as soon as its workflow exists.

    Lambda only deletes the successful messages of a batch when the invocation
    returns. If it times out first, SQS gets no response at all and redelivers
    the whole batch, including messages whose executions were already started.
    Deleting each message right after its StartExecution closes that window
    (the later batch-level delete of an already-deleted message is a no-op).
    Non-fatal: if this fails the deterministic execution name still makes the
    redelivery harmless.
    """
    if not DOCUMENT_QUEUE_URL or not receipt_handle:
        return False
    try:
        sqs.delete_message(QueueUrl=DOCUMENT_QUEUE_URL, ReceiptHandle=receipt_handle)
        return True
    except (ClientError, BotoCoreError) as e:
        logger.warning(f"Could not delete message after starting its workflow: {e}")
        return False


def update_counter(increment: bool = True) -> bool:
    """
    Update the concurrency counter

    Retries ``TransactionConflictException`` with bounded backoff. The workflow
    tracker's decrement is now a ``TransactWriteItems`` (so it can carry its
    per-execution marker atomically), which makes ``workflow_counter`` a
    transactional target: DynamoDB fails a plain ``UpdateItem`` that collides
    with an in-flight transaction on the same item with
    ``TransactionConflictException``, and botocore's DynamoDB retry policy does
    NOT cover that code (it covers ``ReplicatedWriteConflictException``,
    ``TransactionInProgressException`` and crc32 only). On the increment path a
    raise is harmless — the outer handler returns False without touching the
    counter, so SQS redelivers — but the two COMPENSATING-DECREMENT paths in
    ``process_message`` catch and log it, which would LEAK a slot: exactly the
    failure class the floor and the marker exist to prevent. Retried here with
    the same shape the tracker's decrement uses, and deliberately conservative:
    the real conflict rate is unmeasured (this change is not deploy-validated).

    Both directions are conditional, and on DIFFERENT conditions: the increment
    refuses to cross ``MAX_CONCURRENT`` and the decrement refuses to cross zero.
    Each supplies only the expression value its own condition references — see
    the comment below, which is the defect that made that worth spelling out.

    Two guards cover this function, and they prove different things.
    ``scripts/tests/test_dynamodb_unused_expression_values.py`` reads the pairing
    below out of the source: a value supplied in a branch needs an expression
    referencing it in that branch or above it, so hoisting either
    ``ExpressionAttributeValues`` line out of its ``if`` fails that gate (#1461 —
    it used to decline this shape, which left the call site it was written for
    outside its reach). ``test_counter_decrement.py`` drives both directions
    against a real expression engine, which is what catches a defect no source
    reading can see — the condition itself being wrong, or the wrong value
    reaching it.

    Args:
        increment: Whether to increment (True) or decrement (False) the counter

    Returns:
        bool: True if the write landed. False if its condition refused it: at
        ``MAX_CONCURRENT`` for an increment, at zero for a decrement. Callers
        act on the increment's answer (it is the admission decision); a refused
        decrement is logged, because the slot it would have released is not held
        and there is nothing for the caller to do differently.

    Raises:
        ClientError: If DynamoDB operation fails
    """
    logger.info(f"Updating counter: increment={increment}, max={MAX_CONCURRENT}")
    update_args: Dict[str, Any] = {
        "Key": {"counter_id": COUNTER_ID},
        "UpdateExpression": "ADD active_count :inc",
        "ExpressionAttributeValues": {":inc": 1 if increment else -1},
        "ReturnValues": "UPDATED_NEW",
    }

    # ``:max`` is supplied ONLY on the path that references it. DynamoDB rejects
    # an UpdateItem carrying an expression value no expression mentions, with
    # ``ValidationException: Value provided in ExpressionAttributeValues unused
    # in expressions``. Supplying it unconditionally therefore made every
    # DECREMENT fail — the increment path was fine, because its
    # ``ConditionExpression`` referenced it — so both compensating decrements in
    # ``process_message`` leaked a slot each time they ran, and a repeatedly
    # redelivered message could walk the counter up to ``MAX_CONCURRENT`` and
    # block admission entirely until ``reconcile_counter`` or an operator
    # corrected it (issue #1358).
    if increment:
        update_args["ConditionExpression"] = "active_count < :max"
        update_args["ExpressionAttributeValues"][":max"] = MAX_CONCURRENT
    else:
        # The same floor the tracker's decrement carries (issue #915), and it
        # matters here for the first time: until the ``:max`` defect above was
        # fixed this write never landed at all. ``ADD active_count :inc`` with
        # ``:inc = -1`` is unconditional arithmetic, so a hand-back racing a
        # ``reconcile_counter`` reset — or any decrement the counter has already
        # accounted for — drives it NEGATIVE. Admission is ``active_count <
        # :max``, so every unit below zero permanently raises the effective
        # ceiling above MaxConcurrentWorkflows, and the only thing that notices
        # is ``_repair_if_counter_was_negative`` on some later increment.
        # Refusing the write is the safe direction: the slot stays released.
        update_args["ConditionExpression"] = "active_count > :zero"
        update_args["ExpressionAttributeValues"][":zero"] = 0

    last_conflict: Optional[ClientError] = None
    for attempt in range(1, COUNTER_CONFLICT_MAX_ATTEMPTS + 1):
        try:
            logger.info(f"Counter update args: {update_args}")
            response = concurrency_table.update_item(**update_args)
            logger.info(f"Counter update response: {response}")
            if increment:
                # The ONLY place a negative counter is observable in production —
                # see _repair_if_counter_was_negative. Never raises.
                _repair_if_counter_was_negative(response)
            return True

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                # Direction-aware: the two paths now carry DIFFERENT conditions,
                # and the same message for both would report a counter at its
                # floor as a counter at its ceiling — the opposite reading, on
                # the one log line an operator goes looking for when intake
                # stops.
                if increment:
                    logger.warning("Concurrency limit reached")
                else:
                    # "not above zero" rather than "already 0": the condition
                    # also fails on a counter that is NEGATIVE (from a release
                    # before this floor existed, or from the tracker's path) and
                    # on an absent item or attribute. All three mean the same
                    # thing to the reader — there is no slot here to release.
                    logger.warning(
                        "Counter decrement refused: active_count is not above "
                        "zero, so the slot this hand-back would release is not "
                        "held. Leaving it rather than driving the counter "
                        "negative, which would admit work above "
                        "MaxConcurrentWorkflows. Investigate duplicate delivery "
                        "of this message, or a counter edited out from under the "
                        "stack."
                    )
                    _emit_concurrency_event_metric("ConcurrencyCounterUnderflow")
                return False
            if code == "TransactionConflictException":
                last_conflict = e
                logger.warning(
                    f"Counter update collided with an in-flight transaction on "
                    f"the counter item (attempt {attempt}/"
                    f"{COUNTER_CONFLICT_MAX_ATTEMPTS}): {e}"
                )
                if attempt < COUNTER_CONFLICT_MAX_ATTEMPTS:
                    time.sleep(
                        COUNTER_CONFLICT_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    )
                continue
            logger.error(f"Error updating counter: {e}")
            raise

    logger.error(
        f"Counter {'increment' if increment else 'decrement'} still conflicting "
        f"after {COUNTER_CONFLICT_MAX_ATTEMPTS} attempts: {last_conflict}. "
        f"Raising rather than reporting a write that never landed as done.",
        exc_info=True,
    )
    raise last_conflict if last_conflict else RuntimeError("counter update failed")


def _repair_if_counter_was_negative(response: Dict[str, Any]) -> None:
    """Detect, from the increment just applied, that the counter was NEGATIVE.

    **This is the only path in the deployed system that can observe a negative
    counter.** ``reconcile_counter`` — and with it ``_repair_negative_counter``
    and ``ConcurrencyCounterNegativeAlarm`` — is reached from exactly one place:
    the ``if not update_counter(increment=True)`` branch, i.e. only when the
    increment's ``active_count < :max`` condition FAILED. A negative counter
    satisfies that condition, so the increment always succeeds and the reconciler
    is never consulted. Without this hook the repair is unreachable and the alarm
    cannot fire, and the repair's own tests cannot show that because they call
    ``reconcile_counter()`` directly rather than going through admission.

    Detection is free: the increment already asks for ``ReturnValues``, and
    ``active_count`` after adding 1 is ``<= 0`` exactly when it was negative
    before. We hold one slot whose execution does not exist yet — ListExecutions
    cannot see it — hence ``claimed=1``, so the repair does not write a value that
    excludes our own claim.

    Never allowed to change the admission decision: the increment has landed and
    the caller is about to start a workflow, so every failure here is swallowed.
    """
    try:
        raw = (response.get("Attributes") or {}).get("active_count")
        if raw is None:
            return
        post = int(raw)
        if post > 0:
            return
        logger.error(
            f"Concurrency counter was NEGATIVE ({post - 1}) before this "
            f"increment. Admission is `active_count < MaxConcurrentWorkflows`, so "
            f"the stack has been admitting up to {1 - post} workflow(s) above the "
            f"configured ceiling, with nothing erroring. Repairing."
        )
        _repair_negative_counter(post, claimed=1)
    except Exception as e:
        logger.warning(
            f"Could not check the concurrency counter for an underflow: {e}",
            exc_info=True,
        )


def _count_running_executions() -> Optional[int]:
    """Count RUNNING executions of the document-processing state machine.

    Returns None if the count could not be established (API error) OR if the
    count has definitively surpassed ``MAX_CONCURRENT`` — at that point the
    counter (itself capped at ``MAX_CONCURRENT``) mathematically cannot have
    leaked, so there is nothing to reconcile.

    Round-15 review fix: the previous hardcoded ``limit=1000`` broke on large
    stacks where the operator set ``MAX_CONCURRENT`` above 1000. In that
    regime, a real leak could pin the counter high while actual running
    executions were >1000, and this probe would return None → reconciliation
    refused to act → the leak was permanent. Comparing against
    ``MAX_CONCURRENT`` (with a small margin to survive races between listing
    and the counter probe) makes the cap correct for any customer-configured
    limit.
    """
    # +100 margin: an execution may complete between the ListExecutions page
    # and the subsequent counter read, so the counter can briefly be higher
    # than the running count without being leaked. The margin absorbs that
    # honest race so it doesn't trigger a spurious "already leak-proof" skip.
    ceiling = MAX_CONCURRENT + 100
    try:
        total = 0
        token = None
        while True:
            kwargs = {
                "stateMachineArn": state_machine_arn,
                "statusFilter": "RUNNING",
                "maxResults": 100,
            }
            if token:
                kwargs["nextToken"] = token
            page = sfn.list_executions(**kwargs)
            total += len(page.get("executions", []))
            if total > ceiling:
                # Running > MAX_CONCURRENT + margin ⇒ counter can't be leaked
                # (it is capped at MAX_CONCURRENT). Nothing to reconcile.
                logger.info(
                    f"Running executions > MAX_CONCURRENT + margin "
                    f"({ceiling}); counter can't be leaked — skipping reconcile"
                )
                return None
            token = page.get("nextToken")
            if not token:
                return total
    except (ClientError, BotoCoreError) as e:
        # Round-16 review fix: BotoCoreError subclasses (endpoint /
        # timeout / connection reset) bypass ClientError-only handlers
        # and used to propagate, killing message processing on transient
        # network blips. Same "return None → caller declines to act"
        # semantics as the ClientError path.
        logger.warning(f"Could not list executions for reconciliation: {e}")
        return None


def reconcile_counter() -> Optional[int]:
    """Correct a leaked concurrency counter, conservatively.

    The counter is incremented before ``StartExecution`` and decremented by the
    workflow tracker on the completion event. If a decrement is ever missed the
    counter drifts upward permanently, and once it reaches MAX_CONCURRENT the
    stack stops admitting documents **forever** — there is no self-healing path.
    Observed live: ``active_count`` pinned at 100 with only 29 executions
    actually running, 2,532 messages held in flight, and no document started for
    hours.

    Called only when an increment was refused, i.e. only when the drift would
    actually be hurting. Deliberately cautious in three ways, because wrongly
    lowering the counter over-admits work:

    1. **Two samples.** An increment happens before its execution exists, so a
       single sample legitimately sees fewer running executions than the counter.
       We record a sample and only act if a *previous* sample, at least
       RECONCILE_GRACE_SECONDS old, agreed that the counter was too high.
    2. **Never raises, only lowers**, and only as far as the larger of the two
       observed running counts — never below what we know is in flight.
    3. **Conditional write** on the exact value we sampled, so a concurrent
       increment or decrement makes this a no-op instead of clobbering it.

    A NEGATIVE counter is the one case that gets none of that caution, because it
    is the opposite failure: see ``_repair_negative_counter``. It used to get no
    treatment at all — an ``active <= 0`` early return here declined to act on
    exactly the state that needed correcting (issue #915).

    Note that the negative branch below is **defensive depth, not the live path**.
    This function only runs when the increment's ``active_count < :max`` condition
    failed, and a negative counter satisfies that condition, so in a normally
    configured stack a negative counter is detected on the *successful* increment
    instead (``_repair_if_counter_was_negative``). The branch here still covers
    the residue: a MaxConcurrentWorkflows of 0 or below, where a negative counter
    can also fail the admission condition.

    Returns the corrected value, or None if no correction was made.
    """
    now = int(time.time())
    try:
        item = (
            concurrency_table.get_item(
                Key={"counter_id": COUNTER_ID}, ConsistentRead=True
            ).get("Item")
            or {}
        )
    except (ClientError, BotoCoreError) as e:
        # Round-16 review fix — see _count_running_executions above.
        logger.warning(f"Could not read counter for reconciliation: {e}")
        return None

    active = int(item.get("active_count", 0))
    if active < 0:
        # An underflowed counter raises the effective ceiling instead of lowering
        # it, so it is repaired on sight rather than sampled twice.
        return _repair_negative_counter(active)
    if active == 0:
        # No slots claimed, so there is nothing to reconcile and no reason to pay
        # for a ListExecutions sweep.
        return None

    running = _count_running_executions()
    if running is None:
        return None

    if running >= active:
        # No drift. Clear any stale suspicion so a later real leak needs two
        # fresh samples of its own. Round-18 review fix (#202): pass the
        # observed timestamp so the REMOVE is CaS-guarded — a concurrent
        # refusal path that just wrote a FRESH drift sample must NOT be
        # clobbered by our clear.
        prev_at = item.get("drift_observed_at")
        if prev_at:
            _put_drift_sample(None, None, replace_older_than=prev_at)
        return None

    drift = active - running
    _emit_drift_metric(drift, active, running)

    prev_at = int(item.get("drift_observed_at") or 0)
    prev_running = item.get("drift_running")

    if not prev_at:
        # First observation: start the clock and wait for a second, independent look.
        logger.warning(
            f"Concurrency counter may have leaked: active_count={active} but "
            f"{running} executions are RUNNING (drift={drift}). Recording a "
            f"sample; will correct if still true in {RECONCILE_GRACE_SECONDS}s."
        )
        _put_drift_sample(now, running)
        return None

    age = now - prev_at
    if age > RECONCILE_SAMPLE_MAX_AGE_SECONDS:
        # The existing sample is from an OLD episode. Reconciliation only runs on
        # a refused increment, so once capacity returns the sample stops being
        # revisited and can linger indefinitely. Trusting a stale one would let a
        # LATER leak be corrected on its very first observation, defeating the
        # two-sample safeguard — so start the clock again instead.
        logger.info(
            f"Discarding stale concurrency drift sample ({age}s old) and "
            f"restarting the observation window."
        )
        # Round-17 review fix: pass replace_older_than so the CaS write
        # can overwrite this specific stale sample. Without it, the
        # round-16 ``attribute_not_exists`` guard silently no-oped and
        # the stale sample sat forever.
        _put_drift_sample(now, running, replace_older_than=prev_at)
        return None

    if age < RECONCILE_GRACE_SECONDS:
        # A sample already exists and the grace window has not elapsed. Do NOT
        # re-record it: rewriting the timestamp on every refusal RESETS the clock,
        # and under load refusals arrive far more often than the grace period, so
        # the window would never elapse and the counter would never be corrected.
        # Found in live verification — the counter sat at its ceiling while the
        # sample timestamp advanced on every invocation.
        logger.info(
            f"Concurrency counter drift still present (active_count={active}, "
            f"{running} RUNNING); {RECONCILE_GRACE_SECONDS - age}s left before "
            f"correcting."
        )
        return None

    # Two independent samples, GRACE apart, both saw the counter too high.
    # max(..., 0) is belt-and-braces: both inputs are counts, so the corrected
    # value can never be the negative counter this function now also repairs.
    target = max(running, int(prev_running or 0), 0)
    if target >= active:
        return None

    try:
        concurrency_table.update_item(
            Key={"counter_id": COUNTER_ID},
            UpdateExpression=(
                "SET active_count = :new REMOVE drift_observed_at, drift_running"
            ),
            ConditionExpression="active_count = :expected",
            ExpressionAttributeValues={":new": target, ":expected": active},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Someone incremented/decremented meanwhile — the counter is moving,
            # so it is not stuck. Try again on a later invocation.
            logger.info("Counter changed during reconciliation; skipping correction")
            return None
        logger.error(f"Failed to reconcile counter: {e}")
        return None

    logger.warning(
        f"RECONCILED leaked concurrency counter: {active} -> {target} "
        f"(running executions: {running}, previous sample: {prev_running}). "
        f"{active - target} slot(s) had been held by workflows that already ended."
    )
    return target


def _repair_negative_counter(active: int, claimed: int = 0) -> Optional[int]:
    """Raise a NEGATIVE counter back to what is actually running.

    A negative ``active_count`` is not drift, it is arithmetic that ran past its
    floor, and it fails in the opposite and worse direction: the admission gate is
    ``active_count < MAX_CONCURRENT``, so every unit below zero is one more
    workflow admitted above MaxConcurrentWorkflows, with no error anywhere
    (issue #915). The workflow tracker's decrement is now floored and deduplicated
    so it cannot get here, but a counter that already went negative — or one edited
    by hand — still needs a way back.

    The two-sample caution in ``reconcile_counter`` exists because *lowering* the
    counter over-admits work. Raising it out of a negative value only ever
    tightens admission, so this acts on the first observation. It still writes
    conditionally on the exact value read, and can never write below zero.

    Args:
        active: The counter value as currently written — what the conditional
            write below must still find, so a concurrent change makes this a
            no-op rather than a clobber.
        claimed: Slots this caller has ALREADY added to ``active`` for executions
            that do not exist yet, and which ListExecutions therefore cannot see.
            The admission path (``_repair_if_counter_was_negative``) passes 1, so
            the repair neither drops its own claim nor misreports the negative
            value for the alarm. ``reconcile_counter`` runs only on a *refused*
            increment — nothing was claimed — so it passes 0.
    """
    running = _count_running_executions()
    # None means the probe failed, or that it stopped counting past the ceiling.
    # Either way zero is a safe floor: it is much closer to the truth than a
    # negative counter, and it cannot admit MORE work than we already are.
    target = (max(running, 0) if running is not None else 0) + claimed

    # Publish the value the counter actually HELD, before the write below and even
    # if that write no-ops. Two reasons it is `active - claimed` rather than
    # `active`: ConcurrencyCounterNegativeAlarm is Minimum < 0, and on the
    # admission path our own increment has already moved the counter (a counter of
    # -1 reads 0 by the time we detect it), so publishing `active` would leave the
    # alarm unable to fire on the very case it exists for. And nothing else records
    # an underflow that was silently cleaned up — an operator needs to know the
    # ceiling was breached, not just that it is fixed.
    _emit_counter_active_metric(active - claimed)

    try:
        concurrency_table.update_item(
            Key={"counter_id": COUNTER_ID},
            UpdateExpression=(
                "SET active_count = :new REMOVE drift_observed_at, drift_running"
            ),
            ConditionExpression="active_count = :expected",
            ExpressionAttributeValues={":new": target, ":expected": active},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # The counter moved under us; re-read it on a later invocation.
            logger.info("Counter changed during underflow repair; skipping")
            return None
        logger.error(f"Failed to repair negative concurrency counter: {e}")
        return None

    held = active - claimed
    logger.warning(
        f"REPAIRED a NEGATIVE concurrency counter: it held {held}, now {target} "
        f"(running executions: {running}; slots claimed but not yet started: "
        f"{claimed}). While it was negative the stack could admit up to {-held} "
        f"workflows ABOVE MaxConcurrentWorkflows."
    )
    return target


def _put_drift_sample(
    when: Optional[int],
    running: Optional[int],
    replace_older_than: Optional[int] = None,
) -> None:
    """Record (or clear) the observation that the counter looks too high.

    Round-17 review fix: the round-16 ``attribute_not_exists`` guard on
    the SET path silently blocked the round-14 stale-discard flow
    (``if age > RECONCILE_SAMPLE_MAX_AGE_SECONDS: _put_drift_sample(now, running)``)
    because that path REQUIRES overwriting the existing stale sample.
    Now the caller can optionally pass ``replace_older_than`` — the
    timestamp of the stale sample it already read — and the write
    fires if the sample is either absent OR still the exact stale one
    the caller observed (compare-and-swap). A concurrent fresh sample
    from another refusal path fails the condition and is left alone.
    """
    try:
        if when is None:
            # Round-18 review fix (#202): compare-and-swap on the observed
            # timestamp when clearing. Between the caller's get_item and
            # this REMOVE, a concurrent refusal path can write a FRESH
            # drift sample — the round-16 ``attribute_exists`` guard
            # doesn't distinguish "the sample I saw is still there" from
            # "some sample is there", so the clear would wipe the fresh
            # one. Passing ``replace_older_than`` (repurposed here as
            # "the sample the caller observed") makes the REMOVE fire
            # only when that specific sample is still present.
            expr_values: Dict[str, Any] = {}
            condition = "attribute_exists(drift_observed_at)"
            if replace_older_than is not None:
                condition = "drift_observed_at = :prev"
                expr_values[":prev"] = replace_older_than
            kwargs_: Dict[str, Any] = {
                "Key": {"counter_id": COUNTER_ID},
                "UpdateExpression": "REMOVE drift_observed_at, drift_running",
                "ConditionExpression": condition,
            }
            if expr_values:
                kwargs_["ExpressionAttributeValues"] = expr_values
            concurrency_table.update_item(**kwargs_)
        elif replace_older_than is not None:
            # Compare-and-swap overwrite: replace ONLY if the existing
            # sample is still the one the caller saw. Guards against a
            # concurrent fresh sample from another refusal path.
            concurrency_table.update_item(
                Key={"counter_id": COUNTER_ID},
                UpdateExpression=(
                    "SET drift_observed_at = :at, drift_running = :running"
                ),
                ExpressionAttributeValues={
                    ":at": when,
                    ":running": running,
                    ":prev": replace_older_than,
                },
                ConditionExpression="drift_observed_at = :prev",
            )
        else:
            # Round-16 review fix: two concurrent refused-message paths
            # racing would previously clobber each other's sample. Only
            # WRITE a fresh sample if none exists yet (respecting the
            # existing "never overwrite" invariant from the round-3
            # feedback-clock-reset regression). Any existing sample is
            # kept — reconciliation logic already handles that path.
            concurrency_table.update_item(
                Key={"counter_id": COUNTER_ID},
                UpdateExpression=(
                    "SET drift_observed_at = :at, drift_running = :running"
                ),
                ExpressionAttributeValues={":at": when, ":running": running},
                ConditionExpression="attribute_not_exists(drift_observed_at)",
            )
    except ClientError as e:
        # ConditionalCheckFailedException here is EXPECTED (idempotent
        # no-op path — another writer already recorded the sample or
        # the sample doesn't exist to clear). On any other code we
        # warn and emit ``ConcurrencyDriftSampleWriteFailed`` — a
        # persistent write failure surfaces as an alarmable metric
        # (never as a raised exception, because drift-sample recording
        # is telemetry and must not affect message processing).
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            logger.debug(f"drift-sample write no-op (condition guard): {e}")
            return
        logger.warning(f"Could not record concurrency drift sample: {e}")
        # Emit a metric so a stuck reconciliation is visible instead of
        # silently invisible.
        try:
            boto3.client("cloudwatch").put_metric_data(
                Namespace=METRIC_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "ConcurrencyDriftSampleWriteFailed",
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception:
            pass  # telemetry must not affect message processing
    except BotoCoreError as e:
        # Round-16 review fix: connection / timeout / endpoint errors
        # bypass ClientError. Same handling: warn + metric.
        logger.warning(f"Concurrency drift sample write hit BotoCoreError: {e}")
        try:
            boto3.client("cloudwatch").put_metric_data(
                Namespace=METRIC_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "ConcurrencyDriftSampleWriteFailed",
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception:
            pass


def _emit_drift_metric(drift: int, active: int, running: int) -> None:
    """Publish the drift so an alarm can fire before a human notices a stall.

    A stuck counter is otherwise invisible: the queue simply stops draining and
    every other metric looks idle rather than broken.
    """
    try:
        cloudwatch = boto3.client("cloudwatch")
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "ConcurrencyCounterDrift",
                    "Value": drift,
                    "Unit": "Count",
                },
                {
                    "MetricName": "ConcurrencyCounterActive",
                    "Value": active,
                    "Unit": "Count",
                },
                {
                    "MetricName": "WorkflowsRunning",
                    "Value": running,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as e:  # never let telemetry break message processing
        logger.warning(f"Could not emit concurrency drift metric: {e}")


def _emit_counter_active_metric(active: int) -> None:
    """Publish the counter value on its own, without a drift sample.

    Used by the underflow-repair path: there is no meaningful drift to report
    there, but the negative value itself has to reach CloudWatch so
    ConcurrencyCounterNegativeAlarm can fire on it.
    """
    try:
        cloudwatch = boto3.client("cloudwatch")
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "ConcurrencyCounterActive",
                    "Value": active,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:  # never let telemetry break message processing
        logger.warning(f"Could not emit concurrency counter metric: {e}")


def _emit_concurrency_event_metric(metric_name: str) -> None:
    """Publish a single count for an exceptional concurrency-counter event.

    Deliberately the same shape as the workflow tracker's ``_emit_event_metric``:
    namespace is the stack name and there are no dimensions, so an alarm declared
    without dimensions fires whichever function published. Two conditions use it.

    ``ConcurrencyCounterUnderflow`` — a hand-back refused because the counter held
    no slot. The tracker publishes the same name for the same condition on its own
    decrement, so one alarm covers both and the operator question is identical
    either way: a slot was released that nobody held.

    ``ConcurrencyCounterUpdateFailed`` — the counter write RAISED, and the caller
    logged it and carried on. #1358 was not unlogged — both call sites log at
    ERROR with a traceback — it was UNALARMED: a ``ValidationException`` on every
    decrement produced a log line and no datapoint, so nothing anyone watches
    moved while the counter climbed. The refusal case above is now visible; this
    makes the raising case visible too, which is the shape of the next variant
    rather than of this one.

    Never allowed to affect the write it reports on.
    """
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:  # never let telemetry break message processing
        logger.warning(f"Could not emit {metric_name} metric: {e}")


def _emit_circuit_breaker_check_failure(classification: str) -> None:
    """Record that the breaker's state could not be read, and how it was judged.

    Both outcomes of a failed read are a degradation an operator has to be able
    to see. ``TERMINAL`` in particular means the breaker is **not protecting
    anything** while the stack keeps admitting work, and nothing else in the
    system would say so: the manager Lambda publishes only on transitions it
    makes, and a read that never reached the table is not a transition.
    ``PutMetricData`` needs no resource permissions and the function already
    holds it for the counter metrics, so this costs no IAM change.
    """
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "CircuitBreakerCheckFailed",
                    "Dimensions": [{"Name": "Classification", "Value": classification}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:  # never let telemetry break message processing
        logger.warning(f"Could not emit circuit breaker check-failure metric: {e}")


def _probe_window(now: Optional[float] = None) -> int:
    """The rolling window a probe claimed at ``now`` belongs to.

    An integer bucket rather than a timestamp, so the comparison in the
    conditional writes below is a cheap ``<`` on a number that only ever
    increases.
    """
    return int((time.time() if now is None else now) // HALF_OPEN_PROBE_WINDOW_SECONDS)


def _take_probe_slot(window: int) -> bool:
    """Claim a slot in a window another invocation already opened.

    The whole decision is the ``ConditionExpression``: DynamoDB evaluates it
    and applies the increment as one serialized operation on the item, so N
    concurrent invocations cannot all read "0 slots used" and all proceed. The
    ``#state`` term is part of that — a probe cannot be claimed against a
    breaker that re-OPENED or CLOSED between the read above and this write.

    Returns:
        True if this invocation now owns a probe slot. False means the
        condition did not hold, which this function deliberately does not try
        to attribute: slots exhausted, a different window, or the state moved
        are all "no slot for you".
    """
    try:
        concurrency_table.update_item(
            Key={"counter_id": CIRCUIT_BREAKER_ID},
            UpdateExpression=(
                f"SET {HALF_OPEN_PROBE_COUNT_ATTR} = "
                f"{HALF_OPEN_PROBE_COUNT_ATTR} + :one"
            ),
            ConditionExpression=(
                "#state = :half_open"
                f" AND {HALF_OPEN_PROBE_WINDOW_ATTR} = :window"
                f" AND {HALF_OPEN_PROBE_COUNT_ATTR} < :budget"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":one": 1,
                ":half_open": CB_STATE_HALF_OPEN,
                ":window": window,
                ":budget": HALF_OPEN_PROBE_BUDGET,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        # Anything else (throttle, access denied, table gone) is for
        # check_circuit_breaker's classifier to judge, exactly as a failed
        # state read is.
        raise


def _open_probe_window(window: int) -> bool:
    """Claim the FIRST slot of a window nobody has opened yet.

    The condition is ``<`` and not ``<>`` on purpose. The window number is
    monotonic, so a laggard invocation — a cold start whose clock is behind, a
    message that sat in the queue — must never be able to pull the window
    *backwards*, which would hand out a second full budget inside one window.
    An older recorded window (left over from a previous half-open episode, or
    from before the breaker last closed) is replaced, which is what makes the
    budget replenish without anything having to clean up after itself.

    Returns:
        True if this invocation opened the window and owns its first slot.
    """
    try:
        concurrency_table.update_item(
            Key={"counter_id": CIRCUIT_BREAKER_ID},
            UpdateExpression=(
                f"SET {HALF_OPEN_PROBE_COUNT_ATTR} = :one,"
                f" {HALF_OPEN_PROBE_WINDOW_ATTR} = :window"
            ),
            ConditionExpression=(
                "#state = :half_open"
                f" AND (attribute_not_exists({HALF_OPEN_PROBE_WINDOW_ATTR})"
                f" OR {HALF_OPEN_PROBE_WINDOW_ATTR} < :window)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":one": 1,
                ":half_open": CB_STATE_HALF_OPEN,
                ":window": window,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _claim_half_open_probe() -> bool:
    """Try to claim one of HALF_OPEN's bounded probe slots.

    **The budget is per rolling window, not per half-open episode**, and that
    is the load-bearing design choice rather than an approximation of one. The
    trickle a window budget allows is what detects recovery: the first probe
    that completes successfully closes the breaker
    (``workflow_tracker.notify_circuit_breaker``), and nothing in this state
    reports a probe *failure*, so the only way to find out that the service is
    back is to keep trying. A per-episode budget stops the trying — once spent
    with no success, nothing further is admitted for the rest of the episode —
    and a window budget keeps it going at a fixed, bounded rate for as long as
    the breaker is half-open.

    How bad "the rest of the episode" is depends on what ends an episode, and
    that is why this reads as a trade rather than a deadlock: the manager's
    ``handle_half_open_timeout`` returns an episode that no probe closed to
    OPEN (issue #1437), so an episode is bounded in time. Without that bound a
    spent per-episode budget would have refused every document indefinitely,
    with the service free to recover and nothing left to notice. A per-episode
    budget is therefore a viable alternative now, and switching to one is a
    separate decision: this one needs no coordination with the episode length,
    and the two numbers below replenish without anything cleaning up after an
    episode.

    Three writes at most, and only in HALF_OPEN:

    1. Take a slot in a window that is already open.
    2. If there is no such window, open this one and take its first slot.
    3. If *that* lost the race, retry (1) — the winner of (2) only consumed
       one slot, so a budget above 1 still has room and refusing here would
       throttle below the configured budget.

    Returns:
        True if a slot was claimed and the workflow may start.

    Raises:
        ClientError / BotoCoreError: anything that is not a conditional-check
            failure, for ``check_circuit_breaker`` to classify.
    """
    window = _probe_window()
    return (
        _take_probe_slot(window)
        or _open_probe_window(window)
        or _take_probe_slot(window)
    )


def check_circuit_breaker() -> tuple[bool, str]:
    """
    Check if the circuit breaker allows new workflows.

    **A failed state read is split by whether a retry can fix it** (issue #934,
    item 4), because the two halves have opposite best answers and answering
    both the same way is wrong either way round.

    A *transient* failure — throttling, a read timeout, a dropped connection, a
    DynamoDB 5xx — **refuses admission**. Refusing is nearly free here: the
    message is reported as a batch item failure, reappears on
    ``DocumentQueue.VisibilityTimeout`` (60 s) and has ``maxReceiveCount`` 500
    to spend, so the queue absorbs roughly 8 hours of this before anything
    reaches ``DocumentQueueDLQ`` — two orders of magnitude more than a DynamoDB
    blip lasts. It is also self-limiting in the one case that matters:
    ConcurrencyTable throttling is caused by this function's own admission rate,
    and a throttle is most likely during exactly the kind of wide service event
    the breaker exists for. Admitting work here is the failure mode being chosen
    against: a breaker that a coincident DynamoDB blip switches off is not a
    breaker. Visibility is deliberately NOT extended the way the ``OPEN`` path
    extends it — that exists because a Bedrock outage lasts as long as
    ``RECOVERY_TIMEOUT_SECONDS``, whereas a transient read failure is expected
    to be gone on the next delivery, and a 300 s push would add five minutes of
    latency to every document in flight for a fault that lasted milliseconds.

    **That 8 hours is not fresh headroom.** The 500 deliveries are the SAME
    budget a message spends waiting at the concurrency gate (see
    ``DocumentQueue``'s ``RedrivePolicy``), so a refusal here consumes a
    delivery that a saturated stack may also need. And because this path leaves
    visibility at 60 s while the ``OPEN`` path pushes it to
    ``RECOVERY_TIMEOUT_SECONDS`` (300 s by default, ~41 h of budget), transient
    refusals spend it about five times faster than an outage pause does. Both
    are still comfortably clear of a real DynamoDB blip; a fault lasting hours,
    under a queue already deep from a saturated gate, is where the two add up.

    **The classifier is conservative in the admitting direction**, and that is
    worth knowing when reading an incident. Several genuinely transient
    transport failures are judged terminal — ``botocore.exceptions.SSLError``, a
    bare botocore ``ConnectionError``, an opaque ``HTTPClientError``, and the
    ``Throttling`` / ``ThrottledException`` / ``RequestThrottled`` /
    ``InternalFailure`` code spellings (DynamoDB itself returns
    ``ThrottlingException`` and ``ProvisionedThroughputExceededException``, which
    ARE recognised) — so they take the admitting branch. Widening the shared
    vocabulary in ``idp_common.utils.transient_errors`` to cover them would
    change retry behaviour for every task handler that consults it, which is not
    this function's call to make. The bound below is what makes it tolerable:
    unless the fault is single-shot, ``update_counter`` fails the message anyway.

    A *terminal* failure — ``AccessDeniedException``, ``ResourceNotFoundException``,
    a malformed request — **allows admission**, loudly. It cannot be retried
    away, so refusing would refuse every message on every poll for as long as
    the misconfiguration lasts: the entire backlog burns its 500 deliveries and
    lands in the dead-letter queue, needing a manual redrive, while the pipeline
    is otherwise healthy. That converts a monitoring degradation into guaranteed
    stalled work, and the breaker guards spend and throttling during a Bedrock
    outage rather than any correctness or security property, so it is the wrong
    trade. The degradation is reported instead — an ``ERROR`` log and the
    ``CircuitBreakerCheckFailed`` metric, dimension ``Classification=TERMINAL``.
    An exception the classifier does not recognise is treated as terminal.

    Note that a terminal ConcurrencyTable fault does not actually let work
    through: ``update_counter`` runs a few lines later on the same table and
    re-raises anything that is not a conditional-check failure, so the message
    fails there instead. The case this branch really keeps alive is a fault
    specific to *this read* — a permissions boundary or scoped-down policy that
    allows ``UpdateItem`` and not ``GetItem``, say.

    **HALF_OPEN admits a bounded number of probes** (issue #934, item 4b), not
    everything. It used to return ``True`` unconditionally with a log line
    calling the traffic "probe traffic", which made the state a no-op gate: the
    breaker spent a whole outage admitting 100% of documents into the failing
    dependency, which is the opposite of what it exists for. HALF_OPEN is also
    bounded in *time* by the manager's ``handle_half_open_timeout``, which
    returns an episode that no probe closed to OPEN (issue #1437), so it is a
    window the breaker passes through rather than a state it rests in — which
    is what makes a bounded trickle the right admission rate for it.

    ``HALF_OPEN_PROBE_BUDGET`` workflows per ``HALF_OPEN_PROBE_WINDOW_SECONDS``
    are admitted and the rest are refused, claimed atomically — see
    ``_claim_half_open_probe``, including why the budget is per window rather
    than per half-open episode.

    **Why 5 per 60 s.** One probe per window would be enough to *detect*
    recovery, because a single successful workflow closes the breaker, but it
    makes that detection hostage to one document: a probe that fails for its
    own reasons (a malformed PDF, an extraction error) would cost a whole
    window. Five gives recovery several independent chances a minute while
    still bounding admitted work to roughly 5 documents a minute against a
    default ``MaxConcurrentWorkflows`` of 100 continuously in flight. The cost
    of the bound is paid only while the breaker is half-open, and only until
    one probe completes — on the order of one document's latency — after which
    the breaker closes and the cap is gone.

    A refused probe is NOT given extended visibility the way an ``OPEN``
    refusal is. The next window is at most ``HALF_OPEN_PROBE_WINDOW_SECONDS``
    away, which the queue's 60 s visibility timeout already matches, and
    pushing refusals to ``RECOVERY_TIMEOUT_SECONDS`` would throttle probes far
    below the budget and slow recovery detection to that interval.

    Nothing here runs on a default deployment: ``CircuitBreakerEnabled``
    defaults to ``"false"``, which makes ``CIRCUIT_BREAKER_ENABLED`` false and
    returns before the table is touched at all.

    Returns:
        Tuple of (allowed: bool, state: str)
        - allowed: True if workflows should proceed, False if blocked
        - state: CLOSED, OPEN, HALF_OPEN, DISABLED, ERROR_TRANSIENT or
          ERROR_TERMINAL
    """
    if not CIRCUIT_BREAKER_ENABLED:
        return True, CB_STATE_DISABLED

    try:
        response = concurrency_table.get_item(
            Key={"counter_id": CIRCUIT_BREAKER_ID},
            ProjectionExpression="#state",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = response.get("Item")

        if not item:
            return True, CB_STATE_CLOSED

        state = item.get("state", CB_STATE_CLOSED)

        if state == CB_STATE_OPEN:
            logger.warning("Circuit breaker is OPEN - blocking new workflows")
            return False, state
        elif state == CB_STATE_HALF_OPEN:
            if _claim_half_open_probe():
                logger.info(
                    f"Circuit breaker is HALF_OPEN - admitting this workflow as one "
                    f"of at most {HALF_OPEN_PROBE_BUDGET} probes per "
                    f"{HALF_OPEN_PROBE_WINDOW_SECONDS}s"
                )
                return True, state
            logger.warning(
                f"Circuit breaker is HALF_OPEN and its probe budget "
                f"({HALF_OPEN_PROBE_BUDGET} per {HALF_OPEN_PROBE_WINDOW_SECONDS}s) "
                f"is spent - blocking this workflow; the message returns to "
                f"DocumentQueue and retries"
            )
            return False, state

        return True, state

    except (ClientError, BotoCoreError) as e:
        # BotoCoreError (connect/timeout/endpoint) bypasses ClientError, and
        # both are classified by the same shared helper the pipeline's task
        # handlers use, so DynamoDB's transient codes do not have to be
        # enumerated a second time here.
        if is_transient_error(e):
            logger.error(
                f"Could not read circuit breaker state ({e}). Treating as a "
                f"transient fault and REFUSING admission; the message returns "
                f"to DocumentQueue and retries.",
                exc_info=True,
            )
            _emit_circuit_breaker_check_failure("TRANSIENT")
            return False, CB_STATE_ERROR_TRANSIENT

        logger.error(
            f"Could not read circuit breaker state ({e}), and the failure is "
            f"not retryable. The circuit breaker is NOT protecting this stack "
            f"until it is fixed; admitting work anyway rather than sending the "
            f"whole backlog to the dead-letter queue. Check the queue "
            f"processor's DynamoDB permissions on the ConcurrencyTable.",
            exc_info=True,
        )
        _emit_circuit_breaker_check_failure("TERMINAL")
        return True, CB_STATE_ERROR_TERMINAL


def extend_visibility_for_outage(receipt_handle: str) -> None:
    """Push SQS visibility to RECOVERY_TIMEOUT_SECONDS so OPEN-state retries
    don't burn through the queue's maxReceiveCount during a long Bedrock outage.

    Non-fatal: if the call fails, the message still reappears on the
    ``DocumentQueue.VisibilityTimeout`` (60s — raised from 30s in the
    per-message ack fix for #904) and the next invocation handles the retry.
    """
    if not DOCUMENT_QUEUE_URL:
        return
    try:
        sqs.change_message_visibility(
            QueueUrl=DOCUMENT_QUEUE_URL,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=RECOVERY_TIMEOUT_SECONDS,
        )
        logger.info(
            f"Circuit breaker OPEN - pushed visibility to {RECOVERY_TIMEOUT_SECONDS}s"
        )
    except ClientError as e:
        logger.warning(f"Failed to extend visibility for OPEN-state message: {e}")


def start_workflow(
    document: Document,
    execution_name: Optional[str] = None,
    review_finalize_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Start Step Functions workflow

    Args:
        document: The Document object to process
        execution_name: Deterministic name for the execution (see
            ``execution_name_for``). None lets Step Functions pick one, which
            disables duplicate protection.
        review_finalize_token: A review-queue finalize token; adds
            ``review.finalizeToken`` to the input so the tracker recognises the
            finalize run. None (every other message) leaves the input as before.

    Returns:
        Dict containing execution details

    Raises:
        DocumentAbortedBeforeStart: If the document reads ABORTED immediately
            before the start (see ``claim_start``); no execution is created
        ExecutionAlreadyStarted: If an execution with this name already exists,
            i.e. an earlier delivery of the same message already started it
        ClientError: If Step Functions operation fails
    """
    # Update document status and timing
    document.status = Status.RUNNING
    document.start_time = datetime.now(timezone.utc).isoformat()

    # Pin the configuration version BEFORE compressing, so every downstream
    # consumer reads one explicitly-recorded value instead of re-resolving the
    # active version for itself.
    #
    # This is the single chokepoint every execution passes through, which makes
    # it the right place to make the pin non-optional. Historically the pin was
    # only set when the uploader supplied `config-version` S3 metadata (or when
    # queue_sender managed to resolve it), so a document could reach the workflow
    # unpinned — and then each consumer resolved the active version again, on its
    # own, with its own filtered-scan bug. That is exactly how issue #599
    # presented: the queue sender failed to stamp a version, so the pipeline-hooks
    # dispatcher fell back to its own (broken) scan and silently read
    # Config#default, disabling every registered hook.
    #
    # Deliberately NOT fatal when nothing is active: a freshly deployed stack
    # writes Config#default with no IsActive attribute, so "no active version"
    # is a normal state and resolve_active_version() returns 'default' for it.
    # Failing here would reject documents that process correctly today.
    #
    # The REVISION of that profile is pinned here too. Without it a save made
    # while a document is in flight would change the configuration under it
    # mid-pipeline — extraction on r7 and assessment on r8 — and the result would
    # not correspond to any single configuration. A revision of None means the
    # profile has no history (an older deployment, or untouched since the
    # upgrade), and consumers fall back to the profile head as before.
    config_table_name = os.environ.get("CONFIG_TABLE")
    needs_version = not document.config_version
    needs_revision = document.config_revision is None
    if config_table_name and (needs_version or needs_revision):
        try:
            manager = ConfigurationManager(table_name=config_table_name)
            if needs_version:
                document.config_version = manager.resolve_active_version()
                logger.info(
                    f"Pinned config version '{document.config_version}' for document "
                    f"{document.id}"
                )
            if document.config_revision is None and document.config_version:
                # Coerced inline (not via the shared helper) because this value is
                # about to be serialized into the Step Functions input: anything
                # non-numeric must degrade to "no pin" rather than break every
                # workflow start.
                published = manager.resolve_published_revision(document.config_version)
                try:
                    document.config_revision = (
                        int(published) if published is not None else None
                    )
                except (TypeError, ValueError):
                    document.config_revision = None
                if document.config_revision is not None:
                    logger.info(
                        f"Pinned revision r{document.config_revision} of "
                        f"'{document.config_version}' for document {document.id}"
                    )
        except Exception as e:
            logger.warning(
                f"Could not pin a config version for {document.id}: {e}. "
                f"Downstream steps will resolve it themselves.",
                exc_info=True,
            )

    # Compress document for Step Functions to handle large documents
    working_bucket = os.environ.get("WORKING_BUCKET")
    if working_bucket:
        # Use document compression (always compress with default 0KB threshold)
        compressed_document = document.serialize_document(
            working_bucket, "workflow_start", logger
        )
        logger.info("Document compressed for Step Functions workflow (always compress)")
    else:
        # Fallback to direct document dict if no working bucket
        compressed_document = document.to_dict()
        logger.warning(
            "No WORKING_BUCKET configured, sending uncompressed document to workflow"
        )

    # Inject use_bda flag and bda_project_arn from config into document for state machine routing.
    # The unified state machine uses $.document.use_bda to choose BDA vs pipeline branch,
    # and $.document.bda_project_arn for the per-config-version BDA project.
    if config_table_name:
        try:
            # Read the version PINNED above, so the routing flags and the rest of
            # the pipeline are guaranteed to come from the same config version.
            # (Still tolerant of an unset pin: the pin block above is best-effort.)
            config_version = getattr(document, "config_version", None) or "default"
            manager = ConfigurationManager(table_name=config_table_name)

            # Read use_bda from the full config (properly decompresses gzip storage)
            config = manager.get_merged_configuration(config_version)
            use_bda = getattr(config, "use_bda", False) if config else False
            compressed_document["use_bda"] = bool(use_bda)

            # Read per-version BDA project ARN (stored as top-level DynamoDB metadata)
            if use_bda:
                bda_project_arn = manager.get_bda_project_arn(config_version)
                if bda_project_arn:
                    compressed_document["bda_project_arn"] = bda_project_arn
                    logger.info(
                        f"Config version '{config_version}': use_bda=True, bda_project_arn={bda_project_arn}"
                    )
                else:
                    # No BDA project linked — fall back to pipeline to avoid errors
                    logger.warning(
                        f"Config version '{config_version}' has use_bda=True but no BDA project ARN linked. "
                        f"Falling back to pipeline mode. Please sync the config version to a BDA project."
                    )
                    compressed_document["use_bda"] = False
            else:
                logger.info(
                    f"Config version '{config_version}': use_bda=False, using pipeline mode"
                )
        except Exception as e:
            logger.warning(
                f"Could not read config from ConfigurationManager: {e}. Defaulting to pipeline mode.",
                exc_info=True,
            )
            compressed_document["use_bda"] = False
    else:
        logger.warning(
            "CONFIG_TABLE env var not set. Cannot determine use_bda flag. Defaulting to pipeline mode."
        )
        compressed_document["use_bda"] = False

    event = {"document": compressed_document}
    if review_finalize_token:
        event["review"] = {"finalizeToken": review_finalize_token}

    logger.info(
        f"Starting workflow for document (size: {len(json.dumps(event, default=str))} chars)"
    )

    start_args: Dict[str, Any] = {
        "stateMachineArn": state_machine_arn,
        "input": json.dumps(event),
    }
    if execution_name:
        start_args["name"] = execution_name

    # Last, so that an abort written during the preparation above is seen.
    if not claim_start(document.input_key):
        raise DocumentAbortedBeforeStart(document.input_key)

    try:
        execution = sfn.start_execution(**start_args)

        # Set workflow execution ARN and start_time in the document
        document.workflow_execution_arn = execution.get("executionArn", "")
        document.start_time = datetime.now(timezone.utc).isoformat()

        logger.info(f"Workflow started: {execution.get('executionArn', '')}")
        return execution
    except ClientError as e:
        if (
            execution_name
            and e.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists"
        ):
            raise ExecutionAlreadyStarted(
                execution_name, execution_arn_for(execution_name)
            ) from e
        logger.error(f"Error starting workflow: {str(e)}")
        document.workflow_execution_arn = document.workflow_execution_arn or ""
        raise
    except Exception as e:
        logger.error(f"Error starting workflow: {str(e)}")
        # Ensure we have a default workflow_execution_arn to avoid None errors
        document.workflow_execution_arn = document.workflow_execution_arn or ""
        raise


def review_finalize_token_of(record: Dict[str, Any]) -> Optional[str]:
    """The ``ReviewFinalizeToken`` message attribute, when valid.

    A review-queue finalize message names its execution from this token instead
    of the message id, so a re-send by the sweeper collapses into
    ``ExecutionAlreadyExists`` rather than starting a second finalize run. An
    invalid value is ignored with a WARNING (today's naming applies).
    """
    attribute = (record.get("messageAttributes") or {}).get("ReviewFinalizeToken") or {}
    token = attribute.get("stringValue")
    if token is None:
        return None
    if isinstance(token, str) and _FINALIZE_TOKEN_RE.match(token):
        return token
    logger.warning("Ignoring an invalid ReviewFinalizeToken message attribute")
    return None


def process_message(record: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Process a single SQS message

    Args:
        record: The SQS message record

    Returns:
        Tuple of (success, message_id)

    Note: This function handles its own errors and returns success/failure
    """
    message = record["body"]
    message_id = record["messageId"]
    finalize_token = review_finalize_token_of(record)
    receipt_handle = record.get("receiptHandle", "")

    try:
        # Handle both compressed and uncompressed documents
        working_bucket = os.environ.get("WORKING_BUCKET")
        message_data = json.loads(message)
        document = Document.load_document(message_data, working_bucket, logger)
        object_key = document.input_key
        logger.info(f"Processing message {message_id} for object {object_key}")

        # Check if document has been aborted before starting workflow.
        # Must run before CB check so aborted docs can be acked regardless
        # of outage state.
        current_doc = document_service.get_document(object_key)
        if current_doc and current_doc.status == Status.ABORTED:
            logger.info(
                f"Document {object_key} was aborted by user, skipping workflow start"
            )
            # Delete the message immediately rather than relying on the
            # batch-outcome path. If this invocation times out on a later
            # message, Lambda reports nothing to SQS and every message in
            # the batch — including this aborted one — would be
            # redelivered, causing the "check if aborted, skip, return"
            # cycle to repeat until the message eventually hits its
            # maxReceiveCount. Same invariant the workflow-started path
            # below enforces (see #904).
            ack_message(receipt_handle)
            return True, message_id

        # Check circuit breaker before paying the cost of X-Ray setup and
        # counter increment.
        cb_allowed, cb_state = check_circuit_breaker()
        if not cb_allowed:
            logger.warning(
                f"Circuit breaker {cb_state} for {object_key} - message will retry later"
            )
            # Only the OPEN state gets its visibility pushed out: that outage
            # is expected to last RECOVERY_TIMEOUT_SECONDS. A transient
            # state-read failure should come back on the next 60 s delivery.
            if cb_state == CB_STATE_OPEN and receipt_handle:
                extend_visibility_for_outage(receipt_handle)
            return False, message_id

        # X-Ray annotations
        # Round-17 review fix: this was ``{document.id}`` — a Python set
        # literal, not the ID itself. X-Ray annotations accept only
        # scalar values (str/int/bool/float), so the SDK either rejected
        # this or recorded a set-repr string instead of the doc ID.
        xray_recorder.put_annotation("document_id", document.id)
        xray_recorder.put_annotation("processing_stage", "queue_processor")
        current_segment = xray_recorder.current_segment()
        if current_segment:
            document.trace_id = current_segment.trace_id
            logger.info(f"Updated {document.id} trace_id: {document.trace_id}")

        # Try to increment counter
        if not update_counter(increment=True):
            logger.warning(f"Concurrency limit reached for {object_key}")
            # Being at the limit is normal under load — but it is also what a
            # LEAKED counter looks like, and a leaked counter never recovers on
            # its own, so the queue would stop draining permanently. Check (and
            # if confirmed, correct) here, where the cost is paid only when we
            # are actually blocked. Never raises the counter, and requires two
            # samples GRACE apart before touching it.
            try:
                if reconcile_counter() is not None and update_counter(increment=True):
                    logger.info(
                        f"Capacity recovered after reconciliation; proceeding with {object_key}"
                    )
                else:
                    return False, message_id
            except Exception as e:
                # Reconciliation is best-effort; a failure here must not change
                # the outcome for this message.
                logger.warning(f"Counter reconciliation failed: {e}", exc_info=True)
                return False, message_id

        # Everything from here on is compensated by the counter decrement below,
        # so it must contain ONLY work that happens before the execution exists.
        # Once StartExecution succeeds the slot has an owner (the workflow
        # tracker, on the terminal event) and this function must neither
        # decrement it nor report the message as failed.
        workflow_started = False
        try:
            # Start workflow with the document
            try:
                execution = start_workflow(
                    document,
                    execution_name_for(object_key, finalize_token or message_id),
                    review_finalize_token=finalize_token,
                )
            except DocumentAbortedBeforeStart:
                # Aborted after the check at the top of this function read it.
                # No execution exists, so the slot taken above is this
                # function's to hand back, as on the duplicate path below.
                logger.info(
                    f"Document {object_key} was aborted while its start was being "
                    f"prepared; skipping workflow start"
                )
                try:
                    update_counter(increment=False)
                except Exception as counter_error:
                    logger.error(
                        f"Failed to decrement counter: {counter_error}", exc_info=True
                    )
                    _emit_concurrency_event_metric("ConcurrencyCounterUpdateFailed")
                ack_message(receipt_handle)
                return True, message_id
            except ExecutionAlreadyStarted as dup:
                # An earlier delivery of this same message already started the
                # workflow, and the invocation handling it died before SQS
                # learned the outcome. That execution owns its slot; the
                # increment above was for a start that did not happen, so hand
                # it back, then ack the message so it stops being redelivered.
                logger.warning(
                    f"Message {message_id} for {object_key} was redelivered after "
                    f"its workflow already started as {dup.execution_arn}; acking "
                    f"without starting a second execution."
                )
                try:
                    update_counter(increment=False)
                except Exception as counter_error:
                    logger.error(
                        f"Failed to decrement counter: {counter_error}", exc_info=True
                    )
                    # Swallowing this is right — the execution exists and the
                    # message must be acked. What #1358 lacked was not the log
                    # line above (ERROR, with a traceback) but a DATAPOINT: no
                    # metric moved while the counter climbed, so nothing anyone
                    # watches showed the leak.
                    _emit_concurrency_event_metric("ConcurrencyCounterUpdateFailed")
                ack_message(receipt_handle)
                return True, message_id
            workflow_started = True

            # Delete the message NOW rather than at the end of the batch: if this
            # invocation times out on a later message, Lambda reports nothing to
            # SQS and every message in the batch, this one included, would be
            # redelivered (#904).
            ack_message(receipt_handle)

            # Update document status in document service.
            #
            # AFTER the point of no return: the workflow is running. A failure
            # here used to fall into the handler below, which decremented the
            # counter (leaving it one BELOW the true in-flight count, which
            # over-admits work) and returned failure, so SQS redelivered the
            # message and a SECOND workflow was started for the same document.
            # Now it is logged and the message is acked, because the execution
            # that matters already exists.
            #
            # Conditional on the document not being ABORTED. The start claim in
            # ``start_workflow`` closes the window up to the claim; an abort
            # landing after it -- during StartExecution, or before this write --
            # wrote ABORTED for a document it still read as QUEUED, with nothing
            # to stop, and this write used to overwrite it with RUNNING (#1612).
            # Refused here, the execution is stopped and ABORTED is left alone.
            try:
                updated_doc = document_service.update_document(
                    document, unless_status=Status.ABORTED
                )
                logger.info(f"Document updated: {updated_doc}")
            except Exception as post_start_error:
                if (
                    getattr(post_start_error, "error_code", None)
                    == CONDITIONAL_CHECK_FAILED
                ):
                    stop_aborted_start(object_key, document.workflow_execution_arn)
                    return True, message_id
                logger.error(
                    f"Workflow for {object_key} started as "
                    f"{execution.get('executionArn') if isinstance(execution, dict) else execution} "
                    f"but the tracking update failed: {post_start_error}. NOT "
                    f"decrementing the counter and NOT retrying the message — the "
                    f"execution owns the slot and a retry would duplicate it.",
                    exc_info=True,
                )
                # The write did not say whether the document was aborted, so
                # ask: an abort that landed in the window must still stop it.
                if aborted_now(object_key):
                    stop_aborted_start(object_key, document.workflow_execution_arn)

            return True, message_id

        except Exception as e:
            logger.error(f"Error processing {object_key}: {str(e)}", exc_info=True)
            # Release the slot ONLY if no execution was created. If one was, the
            # workflow tracker will decrement on its terminal event and doing it
            # here as well would double-release.
            if workflow_started:
                logger.error(
                    f"Error after the workflow for {object_key} started: {e}. "
                    f"Leaving the counter alone — the execution owns the slot.",
                    exc_info=True,
                )
                return True, message_id
            try:
                update_counter(increment=False)
            except Exception as counter_error:
                logger.error(
                    f"Failed to decrement counter: {counter_error}", exc_info=True
                )
                _emit_concurrency_event_metric("ConcurrencyCounterUpdateFailed")
            return False, message_id

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in message {message_id}: {str(e)}")
        return False, message_id

    except KeyError as e:
        logger.error(f"Missing required field in message {message_id}: {str(e)}")
        return False, message_id

    except Exception as e:
        logger.error(
            f"Unexpected error processing message {message_id}: {str(e)}", exc_info=True
        )
        return False, message_id


@xray_recorder.capture("queue_processor")  # pyright: ignore[reportCallIssue] - aws-xray-sdk types capture() as the wrapped function, not the decorator factory
def handler(event, context):
    logger.info(f"Processing event: {json.dumps(sanitize_event_for_logging(event))}")
    logger.info(f"Processing batch of {len(event['Records'])} messages")

    failed_message_ids = []

    for record in event["Records"]:
        success, message_id = process_message(record)
        if not success:
            failed_message_ids.append(message_id)

    return {
        "batchItemFailures": [
            {"itemIdentifier": message_id} for message_id in failed_message_ids
        ]
    }
