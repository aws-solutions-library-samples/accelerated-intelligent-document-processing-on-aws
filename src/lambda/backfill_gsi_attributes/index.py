# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GSI Attribute Backfill Lambda - Worker for Step Functions Distributed Map.

Backfills ItemType and HITLPendingReview attributes on existing TrackingTable items
to populate the TypeDateIndex and HITLPendingReviewIndex GSIs.

Architecture:
- Invoked by Step Functions Distributed Map (one invocation per scan segment)
- Uses parallel scan with segment/totalSegments for efficient distribution
- Returns continuation token if approaching Lambda timeout (14 min safety margin)
- Step Functions handles retries, continuation, and result aggregation

Item Type Mapping (from PK prefix):
- doc#     → ItemType = "document"
- testrun# → ItemType = "testrun"  
- testset# → ItemType = "testset"
- list#    → skip (shard index entries, not needed in GSI)
- agent#   → skip (agent job entries)
"""

import json
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# PK prefix to ItemType mapping
PK_PREFIX_TO_ITEM_TYPE = {
    "doc#": "document",
    "testrun#": "testrun",
    "testset#": "testset",
}

# HITL statuses that indicate review is complete (not pending)
HITL_COMPLETED_STATUSES = {
    "completed",
    "reviewcompleted",
    "skipped",
    "reviewskipped",
}

# Safety margin before Lambda timeout (60 seconds)
TIMEOUT_SAFETY_MARGIN_MS = 60_000

# Batch size for conditional updates
UPDATE_BATCH_SIZE = 25


def lambda_handler(event, context):
    """
    Process one segment of the TrackingTable parallel scan.
    
    Input event:
    {
        "tableName": "stack-TrackingTable-xxx",
        "segment": 0,
        "totalSegments": 10,
        "exclusiveStartKey": null | {...},  // for continuation
        "dryRun": false
    }
    
    Output:
    {
        "segment": 0,
        "processed": 1500,
        "updated": 1200,
        "skipped": 300,
        "errors": 0,
        "continuation": null | {"exclusiveStartKey": {...}},
        "complete": true | false
    }
    """
    table_name = event["tableName"]
    segment = event["segment"]
    total_segments = event["totalSegments"]
    exclusive_start_key = event.get("exclusiveStartKey")
    dry_run = event.get("dryRun", False)

    logger.info(
        f"Starting backfill: segment={segment}/{total_segments}, "
        f"table={table_name}, continuation={'yes' if exclusive_start_key else 'no'}, "
        f"dryRun={dry_run}"
    )

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    stats = {
        "segment": segment,
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "continuation": None,
        "complete": False,
    }

    scan_kwargs = {
        "Segment": segment,
        "TotalSegments": total_segments,
        # Fetch attributes needed for ItemType, HITL status, and ConfidenceAlertCount backfill
        "ProjectionExpression": "PK, SK, #it, HITLTriggered, HITLCompleted, HITLStatus, HITLPendingReview, InitialEventTime, ConfidenceAlertCount, Sections, SubmissionSource, TestSetId",
        "ExpressionAttributeNames": {"#it": "ItemType"},
    }

    if exclusive_start_key:
        scan_kwargs["ExclusiveStartKey"] = exclusive_start_key

    run_exists = _test_run_verifier(table)

    while True:
        # Check if we're approaching Lambda timeout
        remaining_ms = context.get_remaining_time_in_millis()
        if remaining_ms < TIMEOUT_SAFETY_MARGIN_MS:
            logger.warning(
                f"Approaching timeout ({remaining_ms}ms remaining). "
                f"Returning continuation for segment {segment}. "
                f"Stats: {stats}"
            )
            # Return continuation token for Step Functions to resume
            stats["continuation"] = scan_kwargs.get("ExclusiveStartKey")
            return stats

        # Perform one page of the parallel scan
        try:
            response = table.scan(**scan_kwargs)
        except ClientError as e:
            logger.error(f"Scan error on segment {segment}: {e}")
            stats["errors"] += 1
            raise

        items = response.get("Items", [])
        logger.info(
            f"Segment {segment}: scanned {len(items)} items "
            f"(total processed so far: {stats['processed']})"
        )

        # Process each item
        for item in items:
            stats["processed"] += 1
            pk = item.get("PK", "")
            
            # Determine what updates are needed
            updates = _determine_updates(item, pk, run_exists)
            
            if not updates:
                stats["skipped"] += 1
                continue

            if dry_run:
                stats["updated"] += 1
                continue

            # Apply the update
            try:
                _apply_update(table, item["PK"], item["SK"], updates)
                stats["updated"] += 1
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # Item was already updated (race condition with live traffic) - skip
                    stats["skipped"] += 1
                else:
                    logger.error(f"Update error for PK={pk}: {e}")
                    stats["errors"] += 1

        # Check for more pages
        last_key = response.get("LastEvaluatedKey")
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key
        else:
            # Segment complete
            stats["complete"] = True
            logger.info(f"Segment {segment} complete. Stats: {stats}")
            return stats

    return stats


_TEST_RUN_KEY_RE = re.compile(r"^doc#([^/]+-\d{8}-\d{6})/")


def _looks_like_test_run_document(pk, item, run_exists=None):
    """True when this document item was submitted by a test run.

    SubmissionSource/TestSetId are definitive when present. For documents stored
    before those were recorded, the only signal is key shape
    ("<testSetId>-<YYYYMMDD>-<HHMMSS>/", written by the test file copier) — and a
    real upload can share it, since nothing stops a customer from organising a
    bucket into timestamped folders. Retyping one hides it from the Document List
    entirely, which is worse than leaving a legacy test document visible there, so
    the shape alone is not enough: ``run_exists`` must confirm the prefix names an
    actual test run. Without a verifier the document is left as it is.
    """
    if item.get("SubmissionSource") or item.get("TestSetId"):
        return True
    match = _TEST_RUN_KEY_RE.match(pk)
    if not match or run_exists is None:
        return False
    return bool(run_exists(match.group(1)))


def _test_run_verifier(table):
    """Confirm a run id has a test-run record, memoised across the segment.

    One run covers many documents, so this is a handful of reads per scan rather
    than one per item.
    """
    seen = {}

    def exists(run_id):
        if run_id not in seen:
            try:
                seen[run_id] = "Item" in table.get_item(
                    Key={"PK": f"testrun#{run_id}", "SK": "metadata"},
                    ProjectionExpression="PK",
                )
            except ClientError as e:
                logger.warning(f"Could not verify test run '{run_id}': {e}")
                seen[run_id] = False
        return seen[run_id]

    return exists


def _determine_updates(item, pk, run_exists=None):
    """
    Determine which attributes need to be set on this item.
    Returns a dict of {attribute_name: value} or empty dict if no updates needed.
    """
    updates = {}
    
    # 1. Check if ItemType needs to be set
    if "ItemType" not in item:
        for prefix, item_type in PK_PREFIX_TO_ITEM_TYPE.items():
            if pk.startswith(prefix):
                updates["ItemType"] = item_type
                break
        # If PK doesn't match any known prefix (e.g., list#, agent#), skip ItemType

    # 1b. Retype test-run submissions: without this they carry ItemType="document"
    # and show up in the Document List alongside real uploads.
    if item.get("ItemType") == "document" or updates.get("ItemType") == "document":
        if _looks_like_test_run_document(pk, item, run_exists):
            updates["ItemType"] = "test-document"

    # 2. Check if HITLPendingReview needs to be set (only for documents)
    if pk.startswith("doc#") and "HITLPendingReview" not in item:
        hitl_triggered = item.get("HITLTriggered")
        hitl_completed = item.get("HITLCompleted")
        hitl_status = item.get("HITLStatus", "")

        # Set HITLPendingReview if:
        # - HITL was triggered (True or "true")
        # - HITL is not completed
        # - Status is not in a completed state
        is_triggered = hitl_triggered in (True, "true", "True")
        is_completed = hitl_completed in (True, "true", "True")
        status_normalized = hitl_status.lower().replace(" ", "") if hitl_status else ""
        is_status_complete = status_normalized in HITL_COMPLETED_STATUSES

        if is_triggered and not is_completed and not is_status_complete:
            updates["HITLPendingReview"] = "true"

    # 3. For testrun# items, ensure InitialEventTime is set from CreatedAt
    # (testrun items use CreatedAt, but the GSI sorts by InitialEventTime)
    if pk.startswith("testrun#") and "InitialEventTime" not in item:
        # We need to fetch CreatedAt from the full item
        updates["_needs_created_at_copy"] = True

    # 4. Compute ConfidenceAlertCount from Sections for documents that don't have it,
    # or that have it incorrectly set to 0 while sections have alerts (repair from
    # serialization bug where downstream steps overwrote the correct value with 0)
    if pk.startswith("doc#"):
        existing_count = item.get("ConfidenceAlertCount")
        sections = item.get("Sections", []) or []
        computed_count = sum(
            len(section.get("ConfidenceThresholdAlerts", []) or [])
            for section in sections
        )
        if existing_count is None or (existing_count == 0 and computed_count > 0):
            updates["ConfidenceAlertCount"] = computed_count

    return updates


def _apply_update(table, pk, sk, updates):
    """
    Apply attribute updates to a single item with conditional check.
    Uses a condition to ensure idempotency.
    """
    # Handle the special case where we need CreatedAt → InitialEventTime copy
    needs_created_at = updates.pop("_needs_created_at_copy", False)
    
    if needs_created_at:
        # Fetch the full item to get CreatedAt
        try:
            response = table.get_item(
                Key={"PK": pk, "SK": sk},
                ProjectionExpression="CreatedAt"
            )
            created_at = response.get("Item", {}).get("CreatedAt")
            if created_at:
                updates["InitialEventTime"] = created_at
        except ClientError:
            pass  # Skip this field if we can't read it

    if not updates:
        return

    # Build the update expression
    set_parts = []
    expr_names = {}
    expr_values = {}

    for i, (attr_name, attr_value) in enumerate(updates.items()):
        placeholder_name = f"#attr{i}"
        placeholder_value = f":val{i}"
        set_parts.append(f"{placeholder_name} = {placeholder_value}")
        expr_names[placeholder_name] = attr_name
        expr_values[placeholder_value] = attr_value

    update_expression = "SET " + ", ".join(set_parts)  # nosemgrep: python.aws-lambda.security.tainted-sql-string.tainted-sql-string - DynamoDB UpdateExpression, not SQL; values parameterised via ExpressionAttributeValues

    # Build condition: at least one of the attributes doesn't exist yet OR
    # ConfidenceAlertCount is 0 (repair case for serialization bug).
    # This ensures idempotency while allowing repair of incorrect 0 values.
    condition_parts = []
    for i, attr_name in enumerate(updates.keys()):
        if attr_name == "ConfidenceAlertCount":
            # Allow overwrite when attribute doesn't exist OR is incorrectly 0
            condition_parts.append(
                f"(attribute_not_exists(#attr{i}) OR #attr{i} = :zero)"
            )
            expr_values[":zero"] = 0
        elif attr_name == "ItemType" and attr_value == "test-document":
            # Retyping deliberately overwrites an existing "document" value, so
            # attribute_not_exists alone would reject it. Still idempotent: the
            # condition fails once the value is already "test-document".
            condition_parts.append(
                f"(attribute_not_exists(#attr{i}) OR #attr{i} = :legacy_doc_type)"
            )
            expr_values[":legacy_doc_type"] = "document"
        else:
            condition_parts.append(f"attribute_not_exists(#attr{i})")
    condition_expression = " OR ".join(condition_parts)

    table.update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression=update_expression,
        ConditionExpression=condition_expression,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _send_cfn_response(event, context, status, data=None, reason=""):
    """Send response to CloudFormation custom resource."""
    import urllib3
    
    response_url = event.get("ResponseURL", "")
    if not response_url:
        logger.warning("No ResponseURL in event - skipping CFN response")
        return
    
    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": event.get("LogicalResourceId", context.log_stream_name),
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data or {},
    }
    
    http = urllib3.PoolManager()
    try:
        http.request(
            "PUT",
            response_url,
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": ""},
        )
        logger.info(f"CFN response sent: {status}")
    except Exception as e:
        logger.error(f"Failed to send CFN response: {e}")


def handler(event, context):
    """CloudFormation Custom Resource handler for triggering backfill."""
    logger.info(f"Custom Resource event: {json.dumps(event)}")

    request_type = event.get("RequestType", "")
    
    if request_type == "Delete":
        _send_cfn_response(event, context, "SUCCESS", reason="Delete - no action needed")
        return

    # On Create/Update, check if backfill is needed and start the state machine
    try:
        table_name = event["ResourceProperties"]["TrackingTableName"]
        state_machine_arn = event["ResourceProperties"]["BackfillStateMachineArn"]
        total_segments = int(event["ResourceProperties"].get("TotalSegments", "10"))
        
        # Quick check: sample one item to see if ItemType already exists
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)
        
        # Scan for one doc# item to check which attributes need backfilling.
        # filtered-scan-ok: a deliberate one-item SAMPLE, not a search for a
        # specific row — any doc# item answers "has the backfill already run?",
        # and the state machine re-checks per segment. Paginating here would just
        # cost a full table scan to learn the same thing.
        response = table.scan(
            FilterExpression="begins_with(PK, :prefix)",  # nosemgrep: python.aws-lambda.security.tainted-sql-string.tainted-sql-string - DynamoDB FilterExpression with hardcoded value, not SQL
            ExpressionAttributeValues={":prefix": "doc#"},
            Limit=1,
            ProjectionExpression="PK, ItemType, ConfidenceAlertCount",
        )
        
        items = response.get("Items", [])
        if not items:
            logger.info("No items found in table - new stack, no backfill needed")
            _send_cfn_response(
                event, context, "SUCCESS",
                {"BackfillStatus": "SKIPPED", "Reason": "Empty table"},
                reason="No items to backfill"
            )
            return

        # Check if the sampled item already has all required attributes
        sample_item = items[0]
        has_item_type = "ItemType" in sample_item
        has_confidence_count = "ConfidenceAlertCount" in sample_item

        # A one-item sample can only answer "is an attribute missing?", never "is
        # an attribute's VALUE now wrong?". The retype pass is the second kind: a
        # legacy test-run document already carries ItemType="document", so it looks
        # complete here while being exactly the row that needs rewriting — and the
        # stacks holding those rows are precisely the ones that ran an earlier
        # backfill and would be skipped. So a changed BackfillVersion starts the
        # run unconditionally; the sample only short-circuits when this resource's
        # properties are unchanged (a no-op stack update re-invoking Create).
        version = str(event["ResourceProperties"].get("BackfillVersion", ""))
        old_version = str(
            (event.get("OldResourceProperties") or {}).get("BackfillVersion", "")
        )
        version_changed = version != old_version
        if has_item_type and has_confidence_count and not version_changed:
            logger.info(
                "Sample item already has ItemType and ConfidenceAlertCount and "
                f"BackfillVersion is unchanged ({version}) - nothing to do"
            )
            _send_cfn_response(
                event, context, "SUCCESS",
                {"BackfillStatus": "ALREADY_DONE", "Reason": "All GSI attributes present"},
                reason="Backfill already complete"
            )
            return
        
        logger.info(
            f"Backfill needed: ItemType={'present' if has_item_type else 'MISSING'}, "
            f"ConfidenceAlertCount={'present' if has_confidence_count else 'MISSING'}, "
            f"BackfillVersion={old_version or '(none)'} -> {version}"
        )

        # Start the backfill state machine
        sfn_client = boto3.client("stepfunctions")
        execution_input = {
            "tableName": table_name,
            "totalSegments": total_segments,
        }
        
        execution_name = f"backfill-{int(time.time())}"
        sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(execution_input),
        )
        
        logger.info(f"Started backfill state machine: {execution_name}")
        _send_cfn_response(
            event, context, "SUCCESS",
            {
                "BackfillStatus": "STARTED",
                "ExecutionName": execution_name,
                "TotalSegments": str(total_segments),
            },
            reason=f"Backfill started: {execution_name}"
        )

    except Exception as e:
        logger.error(f"Error in custom resource handler: {e}")
        # Don't fail the stack update - backfill can be retried manually
        _send_cfn_response(
            event, context, "SUCCESS",
            {"BackfillStatus": "ERROR", "Error": str(e)},
            reason=f"Backfill error (non-blocking): {str(e)}"
        )
