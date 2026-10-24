# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to abort test runs and their associated document processing.

For every test run named in the request, this function:
1. Retrieves the test run metadata from DynamoDB and invokes the abort workflow
   Lambda for all of the run's documents
2. Waits, once for all of the runs together, for those documents to settle
3. Queues the run's metrics calculation and updates its status to ABORTED
"""

import json
import logging
import os
import random
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')
sqs_client = boto3.client('sqs')

# BatchGetItem answers a throttled or oversized read with UnprocessedKeys, which
# the caller must resubmit. The same bound and backoff as idp_common's
# DynamoDBClient.batch_get_items (this function has no idp_common layer, so it
# cannot call it): up to this many calls per chunk, with exponential backoff and
# full jitter between them. Keys still held back after that are reported as
# unread, not as missing.
BATCH_GET_MAX_ATTEMPTS = 6


def _backoff_sleep(attempt, deadline=None):
    """Exponential backoff with full jitter, capped at one second.

    Never sleeps past ``deadline`` (an absolute ``time.time()`` value), so a
    retry cannot carry the handler beyond its wait budget.
    """
    delay = random.uniform(0, min(1.0, 0.05 * 2**attempt))  # nosec B311
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.time()))
    if delay > 0:
        time.sleep(delay)


# The REST API reaches this resolver through http_api_dispatcher, which stops
# reading the invoke after its _RESOLVER_READ_TIMEOUT_SECONDS (20 s) and answers
# 504 -- while this function runs on and completes the abort, so the caller is
# told an abort that succeeded has failed (#1606). The settle wait below was
# sized for AppSync (25 s plus a 2 s pause) and outlived that bound whenever the
# documents did not settle quickly.
#
# So every wait in one invocation -- the document settle poll, the pause after
# queueing metrics, and BatchGetItem backoff -- shares a single deadline this
# many seconds after the handler starts. It is one deadline for the whole
# request rather than one per run, so a request naming several runs aborts them
# in phases: every run's documents are told to stop first, then one settle wait
# covers all of them, then every run is marked ABORTED. Each run therefore gets
# the whole settle wait, where aborting them one after another would either
# multiply the bound by their count or, under one deadline, leave the first run's
# wait to use it all and mark the later runs ABORTED a moment after dispatch. The
# rest of the dispatcher's 20 s is left for what this clock does not see: cold
# start before the handler runs, the API calls made around the waits (at most one
# DynamoDB call can be in flight when the deadline passes), and the response's
# trip back through the dispatcher. The API calls themselves are not bounded by
# it, so a request naming very many runs can still outlast the dispatcher.
#
# Shortening the wait does not change what the abort does; it changes how long
# the documents get to settle before the run is marked ABORTED and its metrics
# are queued, which matters because both are taken once. A run whose documents
# have not settled by the deadline is handled exactly as one that outlasted the
# previous 25 s: the abort proceeds and a warning is logged.
ABORT_WAIT_BUDGET_SECONDS = 12

# Pause after queueing the runs' metrics calculations, before marking them
# ABORTED; taken once per request, not once per run. The settle wait ends this
# long before the deadline so that the pause fits inside it.
METRICS_START_PAUSE_SECONDS = 2

# Seconds between document status polls during the settle wait.
SETTLE_POLL_INTERVAL_SECONDS = 2


# Test run statuses that can be aborted (before processing completes)
ABORTABLE_STATUSES = {'QUEUED', 'RUNNING'}

# Evaluation outcomes that will not change again, for a document whose own
# ObjectStatus is already COMPLETED. Anything outside this set is read as
# "evaluation still in progress", so a terminal status missing from it makes the
# abort wait below burn its whole budget before giving up on a document that was
# finished all along. Must therefore name every terminal value ANY writer of
# that attribute can set, which is a list this one has twice fallen behind:
# TIMED_OUT was missing until a run that could not be aborted exposed it, and
# DISABLED — evaluation switched off for the run — until #1330. Beyond the
# pipeline's own statuses it includes the outcomes of promoting a document to an
# evaluation baseline, which overwrite the attribute (copy_to_baseline_resolver,
# and idp_sdk's evaluation processor): BASELINE_AVAILABLE and BASELINE_ERROR are
# settled, and so is BASELINE_COPYING — see the run-status resolver's note on why
# none of the three can tell a run anything, and why waiting on the last of them
# is unbounded rather than brief. A set with a name is what lets the run-status
# resolver's own classification be compared against this one, by
# test_results_resolver.py::test_both_readers_of_an_evaluation_status_agree,
# rather than the two drifting apart again in a literal here.
TERMINAL_EVALUATION_STATUSES = {
    'COMPLETED',
    'FAILED',
    'NO_BASELINE',
    'TIMED_OUT',
    'DISABLED',
    'BASELINE_AVAILABLE',
    'BASELINE_ERROR',
    'BASELINE_COPYING',
}


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def lambda_handler(event, context):
    """
    Abort test runs by stopping document processing and updating status.

    Args:
        event: GraphQL event with testRunIds argument
        context: Lambda context

    Returns:
        Dict with abort results including counts and errors
    """
    try:
        # Defense-in-depth: abortTestRuns is an Admin+Author operation.
        # Allow direct Lambda invocations (no 'identity' field or identity=None) for CI/automation.
        # AppSync invocations always have 'identity' with non-None value, so RBAC is still enforced for UI users.
        # Security: Direct invocation path is gated by IAM (lambda:InvokeFunction permission on this ARN),
        # not Cognito groups. CI/automation uses IAM credentials; UI users go through AppSync + Cognito.
        # Raise (not return a 200 dict) so the dispatcher maps it to 403/Unauthorized.
        is_appsync_invoke = event.get('identity') is not None
        if is_appsync_invoke and not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: abortTestRuns requires Admin or Author group"
            )

        test_run_ids = event['arguments']['testRunIds']

        # Validate non-empty input
        if not test_run_ids or len(test_run_ids) == 0:
            return {
                "success": False,
                "message": "No test runs provided to abort",
                "abortedCount": 0,
                "failedCount": 0,
                "errors": ["Empty testRunIds list"]
            }

        logger.info(f"Aborting test runs: {test_run_ids}")

        tracking_table_name = os.environ.get('TRACKING_TABLE_NAME')
        abort_workflow_function_name = os.environ.get('ABORT_WORKFLOW_FUNCTION_NAME')

        if not tracking_table_name or not abort_workflow_function_name:
            raise ValueError("Required environment variables not set")

        tracking_table = dynamodb.Table(tracking_table_name)  # type: ignore[attr-defined]

        # One wait budget for the whole request; see ABORT_WAIT_BUDGET_SECONDS.
        deadline = time.time() + ABORT_WAIT_BUDGET_SECONDS

        aborted_count = 0
        failed_count = 0
        errors = []

        def record_failure(test_run_id, error_msg):
            nonlocal failed_count
            failed_count += 1
            errors.append(f"{test_run_id}: {error_msg}")
            logger.error(f"Failed to abort test run {test_run_id}: {error_msg}")

        # The runs are aborted in three phases rather than one after another, so
        # that every run gets the same settle wait: run by run, the first run's
        # wait used the whole budget and every later run was marked ABORTED a
        # moment after its documents were told to stop (see
        # ABORT_WAIT_BUDGET_SECONDS for why that ordering matters).
        #
        # Phase 1: tell every abortable run's documents to stop.
        dispatched = {}  # test run id -> its document object keys, request order
        for test_run_id in test_run_ids:
            if test_run_id in dispatched:
                record_failure(test_run_id, "Test run named more than once in the request")
                continue
            try:
                object_keys, error_msg = _dispatch_abort(
                    test_run_id, tracking_table, abort_workflow_function_name
                )
            except Exception as e:
                logger.error(
                    f"Exception aborting test run {test_run_id}: {str(e)}",
                    exc_info=True
                )
                object_keys, error_msg = None, str(e)
            if error_msg is not None:
                record_failure(test_run_id, error_msg)
            else:
                dispatched[test_run_id] = object_keys

        if dispatched:
            # Phase 2: one settle wait over every dispatched run's documents,
            # leaving room inside the deadline for the pause after queueing
            # metrics. An error here is handled as a wait that ran out: the
            # documents have been told to stop, so the abort proceeds.
            all_keys = [key for keys in dispatched.values() for key in keys]
            run_label = ", ".join(dispatched)
            logger.info(f"Waiting for documents to reach terminal state for test run(s) {run_label}")
            try:
                _wait_for_documents_terminal_state(
                    tracking_table,
                    run_label,
                    all_keys,
                    deadline=deadline - METRICS_START_PAUSE_SECONDS,
                )
            except Exception as e:
                logger.warning(
                    f"Settle wait failed for test run(s) {run_label}: {e}. "
                    "Proceeding with the abort anyway."
                )

            # Phase 3: queue each run's metrics calculation, pause once so the
            # metrics Lambda has started reading, then mark each run ABORTED.
            queued = [
                test_run_id for test_run_id in dispatched
                if _queue_metrics_calculation(test_run_id)
            ]
            if queued:
                # Within the deadline, so it shortens (to nothing) once the
                # settle wait and the queueing calls have used the budget.
                pause = min(
                    METRICS_START_PAUSE_SECONDS, max(0.0, deadline - time.time())
                )
                time.sleep(pause)
                logger.info(f"Waited {pause:.1f} seconds for metrics calculation to start")

            for test_run_id in dispatched:
                error_msg = _mark_test_run_aborted(test_run_id, tracking_table)
                if error_msg is None:
                    aborted_count += 1
                    logger.info(f"Successfully aborted test run {test_run_id}")
                else:
                    record_failure(test_run_id, error_msg)

        # Prepare response
        success = aborted_count > 0
        message = f"Aborted {aborted_count} test run(s)"
        if failed_count > 0:
            message += f", {failed_count} failed"

        logger.info(f"Abort test runs complete: {message}")

        return {
            "success": success,
            "message": message,
            "abortedCount": aborted_count,
            "failedCount": failed_count,
            "errors": errors if errors else None
        }

    except PermissionError:
        # RBAC denial must reach the dispatcher as 403; do not swallow into 200.
        raise
    except Exception as e:
        logger.error(f"Error in abort test runs handler: {str(e)}", exc_info=True)
        # Extract test_run_ids if available from event
        test_run_ids = event.get('arguments', {}).get('testRunIds', []) if 'event' in locals() else []
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "abortedCount": 0,
            "failedCount": len(test_run_ids),
            "errors": [str(e)]
        }


def _dispatch_abort(test_run_id, tracking_table, abort_workflow_function_name):
    """
    Phase 1 for one run: read it, check it can be aborted, and tell its
    documents to stop.

    Args:
        test_run_id: Test run identifier
        tracking_table: DynamoDB table resource
        abort_workflow_function_name: Name of the abort workflow Lambda function

    Returns:
        ``(object_keys, None)`` when the run's documents were told to stop, or
        ``(None, error_message)`` when the run cannot be aborted.
    """
    try:
        # Get test run metadata
        response = tracking_table.get_item(
            Key={'PK': f"testrun#{test_run_id}", 'SK': "metadata"}
        )
    except ClientError as e:
        logger.error(f"DynamoDB error for test run {test_run_id}: {str(e)}")
        return None, f"Database error: {str(e)}"

    if 'Item' not in response:
        return None, "Test run not found"

    item = response['Item']
    current_status = item.get('Status', '').upper()

    # Check if test run can be aborted
    if current_status not in ABORTABLE_STATUSES:
        return None, f"Cannot abort test run with status {current_status}"

    # Extract document object keys from Files list
    object_keys = []
    if 'Files' in item and item['Files']:
        for file_name in item['Files']:
            object_key = f"{test_run_id}/{file_name}"
            object_keys.append(object_key)

    # Abort all documents in the test run (async invocation in batches)
    # Note: abort_workflow_resolver handles completed/failed gracefully
    if object_keys:
        logger.info(
            f"Aborting {len(object_keys)} documents for test run {test_run_id}"
        )

        # Process in batches of 100 to avoid Lambda timeout
        # All batches run in parallel (InvocationType='Event')
        batch_size = 100
        batch_count = 0
        for i in range(0, len(object_keys), batch_size):
            batch = object_keys[i:i + batch_size]
            try:
                lambda_client.invoke(
                    FunctionName=abort_workflow_function_name,
                    InvocationType='Event',  # Async - runs in parallel
                    Payload=json.dumps({
                        'arguments': {'objectKeys': batch}
                    })
                )
                batch_count += 1
                logger.info(f"Invoked abort workflow batch {batch_count} with {len(batch)} documents")
            except Exception as e:
                logger.error(f"Failed to invoke abort workflow batch {batch_count}: {str(e)}")
                # Continue with next batch

        logger.info(f"Invoked {batch_count} abort workflow batches for total of {len(object_keys)} documents")

    return object_keys, None


def _queue_metrics_calculation(test_run_id):
    """Queue the metrics calculation for any completed documents of one run.

    Returns True when a message was sent, False when no queue is configured or
    the send failed (logged as a warning; the abort proceeds).
    """
    try:
        queue_url = os.environ.get("TEST_RESULT_CACHE_UPDATE_QUEUE_URL")
        if not queue_url:
            return False
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"testRunId": test_run_id}),
        )
        logger.info(
            f"Queued cache update for aborted test run: {test_run_id}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Failed to queue cache update for {test_run_id}: {e}"
        )
        return False


def _mark_test_run_aborted(test_run_id, tracking_table):
    """Set one run's status to ABORTED, unless it has meanwhile left QUEUED/RUNNING.

    Returns None on success, or an error message.
    """
    # Use conditional update to prevent overwriting if test run already completed/evaluating
    try:
        # Format timestamp for AppSync AWSDateTime scalar (ISO 8601 with Z suffix)
        completed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        tracking_table.update_item(
            Key={'PK': f"testrun#{test_run_id}", 'SK': "metadata"},
            UpdateExpression="SET #status = :status, CompletedAt = :completed_at",
            ConditionExpression="#status IN (:queued, :running)",
            ExpressionAttributeNames={'#status': 'Status'},
            ExpressionAttributeValues={
                ':status': 'ABORTED',
                ':completed_at': completed_at,
                ':queued': 'QUEUED',
                ':running': 'RUNNING'
            }
        )
        logger.info(f"Updated test run {test_run_id} status to ABORTED")
        return None
    except Exception as e:
        logger.error(f"Failed to update test run status: {str(e)}")
        return f"Failed to update status: {str(e)}"


def _batch_get_document_items(tracking_table, object_keys, deadline=None):
    """
    Batch get document items from DynamoDB.

    Args:
        tracking_table: DynamoDB table resource
        object_keys: List of document object keys (S3 paths)
        deadline: Optional absolute ``time.time()`` value. Once it has passed,
            no further call is made: held-back keys are not resubmitted and
            chunks not yet read are reported unread.

    Returns:
        ``(items_by_key, unread_keys)``: a dict mapping object_key to item dict
        for every record read, and the set of object keys whose record could
        not be read — still unprocessed after ``BATCH_GET_MAX_ATTEMPTS`` calls,
        or in a batch the service refused. A key in neither was read and does
        not exist.
    """
    if not object_keys:
        return {}, set()

    table_name = tracking_table.table_name
    items_by_key = {}
    unread_keys = set()

    # DynamoDB batch_get_item supports up to 100 keys per batch
    batch_size = 100
    for i in range(0, len(object_keys), batch_size):
        batch = object_keys[i:i + batch_size]
        if deadline is not None and time.time() >= deadline:
            unread_keys.update(batch)
            continue
        keys = [{'PK': f"doc#{key}", 'SK': 'none'} for key in batch]
        request_items = {table_name: {'Keys': keys}}
        attempt = 0

        try:
            while request_items:
                response = dynamodb.batch_get_item(RequestItems=request_items)

                # Map responses back to object keys
                for item in response.get('Responses', {}).get(table_name, []):
                    # Extract object_key from PK (format: "doc#object_key")
                    pk = item.get('PK', '')
                    if pk.startswith('doc#'):
                        obj_key = pk[4:]  # Remove "doc#" prefix
                        items_by_key[obj_key] = item

                # Keys held back (throttling, or the 16 MB response cap) are
                # resubmitted with backoff, a bounded number of times.
                request_items = response.get('UnprocessedKeys') or {}
                if not request_items:
                    break
                attempt += 1
                out_of_time = deadline is not None and time.time() >= deadline
                if attempt >= BATCH_GET_MAX_ATTEMPTS or out_of_time:
                    held = request_items.get(table_name, {}).get('Keys', [])
                    unread_keys.update(
                        k['PK'][4:] for k in held
                        if str(k.get('PK', '')).startswith('doc#')
                    )
                    logger.warning(
                        f"Batch get left {len(held)} keys unprocessed after "
                        f"{attempt} attempts; their status is unknown"
                    )
                    break
                _backoff_sleep(attempt, deadline)

        except Exception as e:
            logger.error(f"Batch get failed for batch starting at index {i}: {str(e)}")
            unread_keys.update(k for k in batch if k not in items_by_key)

    return items_by_key, unread_keys


def _wait_for_documents_terminal_state(
    tracking_table, test_run_id, object_keys, max_wait_time=None, deadline=None
):
    """
    Wait for all documents in the test run to reach a terminal state.

    Terminal states for documents:
    - COMPLETED with a terminal EvaluationStatus (see
      TERMINAL_EVALUATION_STATUSES — evaluation finished, failed, timed out,
      had no baseline, or was disabled for the run)
    - ABORTED (stopped by abort workflow)
    - FAILED (processing failed)

    Args:
        tracking_table: DynamoDB table resource
        test_run_id: Test run identifier
        object_keys: List of document object keys (S3 paths)
        max_wait_time: Maximum time to wait in seconds, from now.
        deadline: Absolute ``time.time()`` value by which the wait ends. With
            both given, the earlier wins; with neither, the wait is bounded by
            ABORT_WAIT_BUDGET_SECONDS. No poll starts, and no sleep runs, past it.
    """
    poll_interval = SETTLE_POLL_INTERVAL_SECONDS
    start_time = time.time()
    end_time = start_time + (
        ABORT_WAIT_BUDGET_SECONDS if max_wait_time is None else max_wait_time
    )
    if deadline is not None:
        end_time = min(end_time, deadline)

    # Terminal statuses: documents that won't change further
    terminal_statuses = {'COMPLETED', 'ABORTED', 'FAILED'}

    # Initialize counters
    pending_count = 0
    terminal_count = 0
    unknown_count = 0

    while time.time() < end_time:
        pending_count = 0
        terminal_count = 0
        unknown_count = 0

        # Use batch_get_item for efficiency (100 keys per batch vs 100 sequential GETs)
        items_by_key, unread_keys = _batch_get_document_items(
            tracking_table, object_keys, deadline=end_time
        )

        for object_key in object_keys:
            try:
                item = items_by_key.get(object_key)
                if not item:
                    if object_key in unread_keys:
                        # The read was throttled or refused, so nothing is known
                        # about this document. It is not terminal, so keep
                        # waiting; the next poll may read it.
                        logger.warning(
                            f"Status of document {object_key} is unknown: its "
                            "tracking record could not be read this poll"
                        )
                        unknown_count += 1
                    else:
                        logger.warning(f"Document {object_key} not found in tracking table")
                        pending_count += 1
                    continue

                doc_status = item.get('ObjectStatus', '').upper()
                # .strip() as well as .upper(), matching the run-status
                # reader: a padded value must not be terminal for one of them
                # and in progress for the other, and the test that compares the
                # two sets cannot see a difference in how they are read.
                eval_status = item.get('EvaluationStatus', '').strip().upper()

                # Check if document reached terminal state
                # Terminal = processing done AND (evaluation done OR no evaluation needed)
                if doc_status == 'COMPLETED':
                    # Document processing finished, check if evaluation is also done
                    if eval_status in TERMINAL_EVALUATION_STATUSES:
                        terminal_count += 1
                    else:
                        # Still evaluating (or evaluation not started yet)
                        pending_count += 1
                elif doc_status in terminal_statuses:
                    # ABORTED or FAILED - no evaluation happens, already terminal
                    terminal_count += 1
                else:
                    # Still processing (QUEUED, RUNNING, OCR, EXTRACTING, etc.)
                    pending_count += 1

            except Exception as e:
                logger.warning(f"Failed to check status for {object_key}: {str(e)}")
                pending_count += 1

        logger.info(
            f"Document status check: {terminal_count} terminal, {pending_count} pending, "
            f"{unknown_count} unknown"
        )

        # All documents reached terminal state
        if pending_count == 0 and unknown_count == 0:
            logger.info(
                f"All documents reached terminal state for test run {test_run_id} "
                f"({terminal_count} terminal)"
            )
            return

        # Wait before next poll, but not past the end of the wait
        remaining = end_time - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    # Timeout reached
    logger.warning(
        f"Timeout waiting for documents to reach terminal state for test run {test_run_id}. "
        f"Proceeding with metrics calculation anyway. "
        f"Final status: {terminal_count} terminal, {pending_count} pending, "
        f"{unknown_count} unknown (tracking record could not be read)"
    )
