# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder
from botocore.exceptions import BotoCoreError, ClientError

from idp_common.config import ConfigurationManager
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status

patch_all()

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
DOCUMENT_QUEUE_URL = os.environ.get("DOCUMENT_QUEUE_URL", "")
RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("RECOVERY_TIMEOUT_SECONDS", "300"))
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


def update_counter(increment: bool = True) -> bool:
    """
    Update the concurrency counter

    Args:
        increment: Whether to increment (True) or decrement (False) the counter

    Returns:
        bool: True if update successful, False if at limit

    Raises:
        ClientError: If DynamoDB operation fails
    """
    logger.info(f"Updating counter: increment={increment}, max={MAX_CONCURRENT}")
    try:
        update_args = {
            "Key": {"counter_id": COUNTER_ID},
            "UpdateExpression": "ADD active_count :inc",
            "ExpressionAttributeValues": {
                ":inc": 1 if increment else -1,
                ":max": MAX_CONCURRENT,
            },
            "ReturnValues": "UPDATED_NEW",
        }

        if increment:
            update_args["ConditionExpression"] = "active_count < :max"

        logger.info(f"Counter update args: {update_args}")
        response = concurrency_table.update_item(**update_args)
        logger.info(f"Counter update response: {response}")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("Concurrency limit reached")
            return False
        logger.error(f"Error updating counter: {e}")
        raise


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
    if active <= 0:
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
    target = max(running, int(prev_running or 0))
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


def check_circuit_breaker() -> tuple[bool, str]:
    """
    Check if the circuit breaker allows new workflows.

    Returns:
        Tuple of (allowed: bool, state: str)
        - allowed: True if workflows should proceed, False if blocked
        - state: Current circuit breaker state (CLOSED, OPEN, HALF_OPEN, DISABLED, ERROR)
    """
    if not CIRCUIT_BREAKER_ENABLED:
        return True, "DISABLED"

    try:
        response = concurrency_table.get_item(
            Key={"counter_id": CIRCUIT_BREAKER_ID},
            ProjectionExpression="#state",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = response.get("Item")

        if not item:
            return True, "CLOSED"

        state = item.get("state", "CLOSED")

        if state == "OPEN":
            logger.warning("Circuit breaker is OPEN - blocking new workflows")
            return False, state
        elif state == "HALF_OPEN":
            logger.info("Circuit breaker is HALF_OPEN - allowing probe traffic")
            return True, state

        return True, state

    except (ClientError, BotoCoreError) as e:
        # BotoCoreError (connect/timeout/endpoint) bypasses ClientError.
        # Matches the round-16 hardening applied to sibling helpers
        # (_count_running_executions, _put_drift_sample) — a transient
        # DynamoDB blip must fail-open (return True) rather than raise.
        logger.error(f"Error checking circuit breaker: {e}")
        return True, "ERROR"


def extend_visibility_for_outage(receipt_handle: str) -> None:
    """Push SQS visibility to RECOVERY_TIMEOUT_SECONDS so OPEN-state retries
    don't burn through the queue's maxReceiveCount during a long Bedrock outage.

    Non-fatal: if the call fails, the message still reappears on the default
    30s visibility timeout and the next invocation handles the retry.
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


def start_workflow(document: Document) -> Dict[str, Any]:
    """
    Start Step Functions workflow

    Args:
        document: The Document object to process

    Returns:
        Dict containing execution details

    Raises:
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

    logger.info(
        f"Starting workflow for document (size: {len(json.dumps(event, default=str))} chars)"
    )

    try:
        execution = sfn.start_execution(
            stateMachineArn=state_machine_arn, input=json.dumps(event)
        )

        # Set workflow execution ARN and start_time in the document
        document.workflow_execution_arn = execution.get("executionArn", "")
        document.start_time = datetime.now(timezone.utc).isoformat()

        logger.info(f"Workflow started: {execution.get('executionArn', '')}")
        return execution
    except Exception as e:
        logger.error(f"Error starting workflow: {str(e)}")
        # Ensure we have a default workflow_execution_arn to avoid None errors
        document.workflow_execution_arn = document.workflow_execution_arn or ""
        raise


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
            return True, message_id  # Return success to remove message from queue

        # Check circuit breaker before paying the cost of X-Ray setup and
        # counter increment.
        cb_allowed, cb_state = check_circuit_breaker()
        if not cb_allowed:
            logger.warning(
                f"Circuit breaker {cb_state} for {object_key} - message will retry later"
            )
            if cb_state == "OPEN" and receipt_handle:
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
            execution = start_workflow(document)
            workflow_started = True

            # Update document status in document service.
            #
            # AFTER the point of no return: the workflow is running. A failure
            # here used to fall into the handler below, which decremented the
            # counter (leaving it one BELOW the true in-flight count, which
            # over-admits work) and returned failure, so SQS redelivered the
            # message and a SECOND workflow was started for the same document.
            # Now it is logged and the message is acked, because the execution
            # that matters already exists.
            try:
                updated_doc = document_service.update_document(document)
                logger.info(f"Document updated: {updated_doc}")
            except Exception as post_start_error:
                logger.error(
                    f"Workflow for {object_key} started as "
                    f"{execution.get('executionArn') if isinstance(execution, dict) else execution} "
                    f"but the tracking update failed: {post_start_error}. NOT "
                    f"decrementing the counter and NOT retrying the message — the "
                    f"execution owns the slot and a retry would duplicate it.",
                    exc_info=True,
                )

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


@xray_recorder.capture("queue_processor")
def handler(event, context):
    logger.info(f"Processing event: {json.dumps(event)}")
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
