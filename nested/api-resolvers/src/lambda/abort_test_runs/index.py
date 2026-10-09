# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to abort test runs and their associated document processing.

This function handles aborting test runs by:
1. Retrieving test run metadata from DynamoDB
2. Invoking the abort workflow Lambda for all documents in the test run
3. Updating the test run status to ABORTED
"""

import json
import logging
import os
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

        aborted_count = 0
        failed_count = 0
        errors = []

        # Process each test run
        for test_run_id in test_run_ids:
            try:
                result = abort_single_test_run(
                    test_run_id,
                    tracking_table,
                    abort_workflow_function_name
                )

                if result['success']:
                    aborted_count += 1
                    logger.info(f"Successfully aborted test run {test_run_id}")
                else:
                    failed_count += 1
                    error_msg = result.get('error', 'Unknown error')
                    errors.append(f"{test_run_id}: {error_msg}")
                    logger.error(f"Failed to abort test run {test_run_id}: {error_msg}")

            except Exception as e:
                failed_count += 1
                error_msg = str(e)
                errors.append(f"{test_run_id}: {error_msg}")
                logger.error(
                    f"Exception aborting test run {test_run_id}: {error_msg}",
                    exc_info=True
                )

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


def abort_single_test_run(test_run_id, tracking_table, abort_workflow_function_name):
    """
    Abort a single test run and its documents.

    Args:
        test_run_id: Test run identifier
        tracking_table: DynamoDB table resource
        abort_workflow_function_name: Name of the abort workflow Lambda function

    Returns:
        Dict with 'success' boolean and optional 'error' message
    """
    try:
        # Get test run metadata
        response = tracking_table.get_item(
            Key={'PK': f"testrun#{test_run_id}", 'SK': "metadata"}
        )

        if 'Item' not in response:
            return {"success": False, "error": "Test run not found"}

        item = response['Item']
        current_status = item.get('Status', '').upper()

        # Check if test run can be aborted
        if current_status not in ABORTABLE_STATUSES:
            return {
                "success": False,
                "error": f"Cannot abort test run with status {current_status}"
            }

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

        # Wait for documents to reach terminal state (max 25 seconds to stay within AppSync timeout)
        logger.info(f"Waiting for documents to reach terminal state for test run {test_run_id}")
        _wait_for_documents_terminal_state(tracking_table, test_run_id, object_keys, max_wait_time=25)

        # Queue metrics calculation for any completed documents
        try:
            queue_url = os.environ.get("TEST_RESULT_CACHE_UPDATE_QUEUE_URL")
            if queue_url:
                sqs_client.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps({"testRunId": test_run_id}),
                )
                logger.info(
                    f"Queued cache update for aborted test run: {test_run_id}"
                )

                # Wait briefly to give the metrics calculation Lambda time to start processing
                # before we mark the test run as ABORTED
                time.sleep(2)
                logger.info("Waited 2 seconds for metrics calculation to start")
        except Exception as e:
            logger.warning(
                f"Failed to queue cache update for {test_run_id}: {e}"
            )

        # Update test run status to ABORTED after documents have settled and metrics queued
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
        except Exception as e:
            logger.error(f"Failed to update test run status: {str(e)}")
            return {"success": False, "error": f"Failed to update status: {str(e)}"}

        return {"success": True}

    except ClientError as e:
        logger.error(f"DynamoDB error for test run {test_run_id}: {str(e)}")
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(
            f"Unexpected error for test run {test_run_id}: {str(e)}",
            exc_info=True
        )
        return {"success": False, "error": str(e)}


def _batch_get_document_items(tracking_table, object_keys):
    """
    Batch get document items from DynamoDB.

    Args:
        tracking_table: DynamoDB table resource
        object_keys: List of document object keys (S3 paths)

    Returns:
        Dict mapping object_key to item dict
    """
    if not object_keys:
        return {}

    table_name = tracking_table.table_name
    items_by_key = {}

    # DynamoDB batch_get_item supports up to 100 keys per batch
    batch_size = 100
    for i in range(0, len(object_keys), batch_size):
        batch = object_keys[i:i + batch_size]
        keys = [{'PK': f"doc#{key}", 'SK': 'none'} for key in batch]

        try:
            response = dynamodb.batch_get_item(
                RequestItems={
                    table_name: {
                        'Keys': keys
                    }
                }
            )

            # Map responses back to object keys
            for item in response.get('Responses', {}).get(table_name, []):
                # Extract object_key from PK (format: "doc#object_key")
                pk = item.get('PK', '')
                if pk.startswith('doc#'):
                    obj_key = pk[4:]  # Remove "doc#" prefix
                    items_by_key[obj_key] = item

            # Handle unprocessed keys (throttling, etc.)
            unprocessed = response.get('UnprocessedKeys', {}).get(table_name, {})
            if unprocessed:
                logger.warning(f"Batch get had {len(unprocessed.get('Keys', []))} unprocessed keys")

        except Exception as e:
            logger.error(f"Batch get failed for batch starting at index {i}: {str(e)}")

    return items_by_key


def _wait_for_documents_terminal_state(tracking_table, test_run_id, object_keys, max_wait_time=25):
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
        max_wait_time: Maximum time to wait in seconds (default 25 for AppSync timeout)
    """
    poll_interval = 2  # Check every 2 seconds
    start_time = time.time()

    # Terminal statuses: documents that won't change further
    terminal_statuses = {'COMPLETED', 'ABORTED', 'FAILED'}

    # Initialize counters
    pending_count = 0
    terminal_count = 0

    while time.time() - start_time < max_wait_time:
        pending_count = 0
        terminal_count = 0

        # Use batch_get_item for efficiency (100 keys per batch vs 100 sequential GETs)
        items_by_key = _batch_get_document_items(tracking_table, object_keys)

        for object_key in object_keys:
            try:
                item = items_by_key.get(object_key)
                if not item:
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
            f"Document status check: {terminal_count} terminal, {pending_count} pending"
        )

        # All documents reached terminal state
        if pending_count == 0:
            logger.info(
                f"All documents reached terminal state for test run {test_run_id} "
                f"({terminal_count} terminal)"
            )
            return

        # Wait before next poll
        time.sleep(poll_interval)

    # Timeout reached
    logger.warning(
        f"Timeout waiting for documents to reach terminal state for test run {test_run_id}. "
        f"Proceeding with metrics calculation anyway. "
        f"Final status: {terminal_count} terminal, {pending_count} pending"
    )
