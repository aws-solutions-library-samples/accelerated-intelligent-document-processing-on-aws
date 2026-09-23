# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Document deletion utilities for IDP.

This module provides robust document deletion functions that handle:
- S3 input/output file deletion
- DynamoDB tracking record deletion
- List entry cleanup with timestamp-aware shard handling
"""

import fnmatch
import logging
from typing import Any, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


# Both selectors below feed a delete, so each is written as a named predicate rather
# than inline. A selector in front of an irreversible operation is worth reading on its
# own, and the two answer different questions: a batch is a CONTAINER of documents,
# while a document's outputs include the document's own key.


def _is_in_batch(object_key: str, batch_id: str) -> bool:
    """Is ``object_key`` a document belonging to batch ``batch_id``?

    A batch id is a leading **path segment**, not a substring. Every site that puts a
    batch id into an S3 key writes ``f"{batch_id}/..."`` — the batch processor's copy,
    upload and metadata paths all do — so the delimiter is what the key layout means by
    "in this batch".

    ⚠️ **A substring test, and even a bare ``startswith``, over-match here, and this
    feeds a delete.** ``batch_id in object_key`` also selected ``batch-10/b.pdf`` and
    ``archive/batch-1x/c.pdf`` for ``batch-1``; ``object_key.startswith(batch_id)``
    still selects ``batch-10/b.pdf``, so matching the old docstring's word "prefix"
    literally would not have fixed the reported case. Single-digit batch ids are the
    common case in manual use, which is where the collision is most likely.

    A caller who genuinely wants substring behaviour has ``get_documents_by_pattern``,
    which takes an explicit ``*batch-1*``; narrowing here removes no capability.
    """
    return object_key.startswith(f"{batch_id.rstrip('/')}/")


def _is_document_output(key: str, object_key: str) -> bool:
    """Is ``key`` an output object belonging to the document at ``object_key``?

    Outputs are written **beneath** ``{input_key}/`` — ``sections/``, ``summary/``,
    ``rule_validation/``, ``runs/`` and so on — which is the layout
    ``idp_common.document_versions`` documents and builds its own prefixes from. The
    document's own key is included as well, so nothing is missed if anything is ever
    written at it directly.

    ⚠️ **An S3 ``Prefix`` is a byte prefix, not a path segment**, which is why this is a
    client-side predicate rather than a narrower ``Prefix=``. Purging ``invoice.pdf``
    with a bare prefix also enumerated ``invoice.pdf.bak/...`` and ``invoice.pdf-v2/...``
    — and this path deletes **all object versions and delete markers** it finds, so a
    prefix collision destroyed a sibling document's entire output history rather than
    just its current objects. The listing still passes ``Prefix=object_key`` so the
    server does the coarse narrowing; this decides what is actually deleted.
    """
    if key == object_key:
        return True
    return key.startswith(f"{object_key.rstrip('/')}/")


def calculate_shard(timestamp: str) -> Tuple[str, str]:
    """
    Calculate shard information from timestamp.

    Args:
        timestamp: ISO timestamp string (e.g., "2025-09-10T12:03:27.256164+00:00")

    Returns:
        tuple: (date_part, shard_str) where shard_str is 2-digit padded

    Raises:
        ValueError: If timestamp format is invalid
    """
    if not timestamp or not isinstance(timestamp, str):
        raise ValueError(
            f"Invalid timestamp: must be a non-empty string, got {type(timestamp)}"
        )

    if "T" not in timestamp:
        raise ValueError(
            f"Invalid timestamp format: missing 'T' separator, got {timestamp}"
        )

    try:
        date_part = timestamp.split("T")[0]  # e.g., 2025-09-10
        time_part = timestamp.split("T")[1]

        if ":" not in time_part:
            raise ValueError(
                f"Invalid time format: missing ':' separator, got {time_part}"
            )

        hour_part = int(time_part.split(":")[0])  # e.g., 12

        # Validate hour range
        if not 0 <= hour_part <= 23:
            raise ValueError(f"Invalid hour: must be 0-23, got {hour_part}")

        # Calculate shard (6 shards per day = 4 hours each)
        hours_in_shard = 24 / 6
        shard = int(hour_part / hours_in_shard)
        shard_str = f"{shard:02d}"  # Format with leading zero

        return date_part, shard_str

    except (ValueError, IndexError) as e:
        if "Invalid" in str(e):
            raise  # Re-raise our custom validation errors
        raise ValueError(f"Invalid timestamp format: {timestamp}, error: {str(e)}")


def _try_exact_list_deletion(
    tracking_table, list_pk: str, list_sk: str, object_key: str
) -> bool:
    """
    Attempt to delete list entry with exact timestamp match.

    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of list entry
        list_sk: Sort key of list entry
        object_key: Document object key for logging

    Returns:
        bool: True if successful, False if not found
    """
    try:
        logger.debug(f"Trying exact deletion - PK={list_pk}, SK={list_sk}")
        result = tracking_table.delete_item(
            Key={"PK": list_pk, "SK": list_sk}, ReturnValues="ALL_OLD"
        )

        if "Attributes" in result:
            logger.info(f"Deleted list entry with exact match: PK={list_pk}")
            return True
        else:
            logger.debug(
                f"No list entry found with exact match: PK={list_pk}, SK={list_sk}"
            )
            return False
    except Exception as e:
        logger.error(f"Error in exact list deletion for {object_key}: {str(e)}")
        return False


def _query_shard_for_object_key(
    tracking_table, list_pk: str, object_key: str
) -> List[Dict[str, Any]]:
    """
    Query a shard for any list entries containing the specified object key.
    Uses DynamoDB filter expressions for efficiency.

    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of the shard
        object_key: Document object key to search for

    Returns:
        List[Dict]: List of matching DynamoDB items
    """
    try:
        logger.debug(f"Querying shard {list_pk} for ObjectKey: {object_key}")

        # Use filter expression to efficiently query only matching items
        response = tracking_table.query(
            KeyConditionExpression=Key("PK").eq(list_pk),
            FilterExpression="ObjectKey = :obj_key OR contains(SK, :obj_id)",
            ExpressionAttributeValues={
                ":obj_key": object_key,
                ":obj_id": f"#id#{object_key}",
            },
        )

        matching_items = []
        if "Items" in response:
            for item in response["Items"]:
                matching_items.append(item)

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = tracking_table.query(
                KeyConditionExpression=Key("PK").eq(list_pk),
                FilterExpression="ObjectKey = :obj_key OR contains(SK, :obj_id)",
                ExpressionAttributeValues={
                    ":obj_key": object_key,
                    ":obj_id": f"#id#{object_key}",
                },
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )

            if "Items" in response:
                matching_items.extend(response["Items"])

        logger.debug(f"Found {len(matching_items)} matching entries in shard {list_pk}")
        return matching_items

    except Exception as e:
        logger.error(f"Error querying shard for object key {object_key}: {str(e)}")
        return []


def _get_adjacent_shards(date_part: str, shard_str: str) -> List[str]:
    """
    Get adjacent shard identifiers for edge case handling.

    Args:
        date_part: Date string (e.g., "2025-09-10")
        shard_str: Shard string (e.g., "03")

    Returns:
        List[str]: List of adjacent shard PKs
    """
    try:
        current_shard = int(shard_str)
        adjacent_shards = []

        # Previous shard
        if current_shard > 0:
            prev_shard = f"{current_shard - 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{prev_shard}")

        # Next shard
        if current_shard < 5:  # Max shard is 05
            next_shard = f"{current_shard + 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{next_shard}")

        return adjacent_shards

    except Exception as e:
        logger.error(f"Error calculating adjacent shards: {str(e)}")
        return []


def delete_list_entries_robust(
    tracking_table, object_key: str, document_metadata: Optional[Dict[str, Any]]
) -> bool:
    """
    Robustly delete list entries for the given object key.
    Uses multiple strategies: exact match, shard query, adjacent shard search.

    Args:
        tracking_table: DynamoDB table resource
        object_key: Document object key
        document_metadata: Optional document metadata containing timestamp info

    Returns:
        bool: True if any entries were deleted
    """
    deleted_any = False

    # Strategy 1: Try exact timestamp match if we have document metadata
    if document_metadata:
        event_time = None
        if "QueuedTime" in document_metadata and document_metadata["QueuedTime"]:
            event_time = document_metadata["QueuedTime"]
        elif (
            "InitialEventTime" in document_metadata
            and document_metadata["InitialEventTime"]
        ):
            event_time = document_metadata["InitialEventTime"]

        if event_time:
            try:
                date_part, shard_str = calculate_shard(event_time)
                list_pk = f"list#{date_part}#s#{shard_str}"
                list_sk = f"ts#{event_time}#id#{object_key}"

                if _try_exact_list_deletion(
                    tracking_table, list_pk, list_sk, object_key
                ):
                    deleted_any = True
                    return deleted_any  # Success, no need for fallback
            except Exception as e:
                logger.error(f"Error in exact timestamp deletion: {str(e)}")

    # Strategy 2: Query calculated shard for any entries with matching ObjectKey
    if document_metadata:
        try:
            event_time = document_metadata.get("QueuedTime") or document_metadata.get(
                "InitialEventTime"
            )
            if event_time:
                date_part, shard_str = calculate_shard(event_time)
                list_pk = f"list#{date_part}#s#{shard_str}"

                matching_entries = _query_shard_for_object_key(
                    tracking_table, list_pk, object_key
                )
                for entry in matching_entries:
                    try:
                        result = tracking_table.delete_item(
                            Key={"PK": entry["PK"], "SK": entry["SK"]},
                            ReturnValues="ALL_OLD",
                        )
                        if "Attributes" in result:
                            deleted_any = True
                    except Exception as e:
                        logger.error(f"Error deleting found list entry: {str(e)}")

                # Strategy 3: Check adjacent shards for edge cases
                if not deleted_any:
                    adjacent_shards = _get_adjacent_shards(date_part, shard_str)

                    for adj_pk in adjacent_shards:
                        matching_entries = _query_shard_for_object_key(
                            tracking_table, adj_pk, object_key
                        )
                        for entry in matching_entries:
                            try:
                                result = tracking_table.delete_item(
                                    Key={"PK": entry["PK"], "SK": entry["SK"]},
                                    ReturnValues="ALL_OLD",
                                )
                                if "Attributes" in result:
                                    deleted_any = True
                            except Exception as e:
                                logger.error(
                                    f"Error deleting adjacent shard entry: {str(e)}"
                                )
        except Exception as e:
            logger.error(f"Error in shard query strategies: {str(e)}")

    return deleted_any


def _delete_run_records(tracking_table, object_key: str) -> int:
    """
    Delete all document version (run) items for a document.

    Run items are keyed PK=doc#<key>, SK=run#<run_id>. Returns the number of
    run items deleted. Idempotent — safe on documents with no runs.
    """
    doc_pk = f"doc#{object_key}"
    deleted = 0
    exclusive_start_key = None
    while True:
        query_kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(doc_pk)
            & Key("SK").begins_with("run#"),
            "ProjectionExpression": "PK, SK",
        }
        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key
        response = tracking_table.query(**query_kwargs)
        for item in response.get("Items", []):
            try:
                tracking_table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                deleted += 1
            except Exception as e:
                logger.error(f"Error deleting run item {item.get('SK')}: {str(e)}")
        exclusive_start_key = response.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break
    if deleted:
        logger.info(f"Deleted {deleted} run records for {object_key}")
    return deleted


def delete_single_document(
    object_key: str,
    tracking_table,
    s3_client,
    input_bucket: str,
    output_bucket: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delete a single document and all its associated data.

    Args:
        object_key: Document object key (S3 path)
        tracking_table: DynamoDB table resource
        s3_client: boto3 S3 client
        input_bucket: Input S3 bucket name
        output_bucket: Output S3 bucket name
        dry_run: If True, only report what would be deleted

    Returns:
        Dict with deletion results:
        - success: bool
        - object_key: str
        - deleted: Dict with counts of deleted items
        - errors: List of error messages
    """
    result = {
        "success": True,
        "object_key": object_key,
        "deleted": {
            "input_file": False,
            "output_files": 0,
            "list_entries": False,
            "document_record": False,
            "run_records": 0,
        },
        "errors": [],
    }

    # Get document metadata first
    doc_pk = f"doc#{object_key}"
    document_metadata = None
    try:
        response = tracking_table.get_item(Key={"PK": doc_pk, "SK": "none"})
        if "Item" in response:
            document_metadata = response["Item"]
            logger.debug(f"Found document metadata for {object_key}")
        else:
            logger.warning(f"Document metadata not found for {object_key}")
    except Exception as e:
        error_msg = f"Error getting document metadata: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    if dry_run:
        logger.info(f"[DRY RUN] Would delete document: {object_key}")
        return result

    # Delete from input bucket
    try:
        logger.debug(f"Deleting from input bucket: {input_bucket}/{object_key}")
        s3_client.delete_object(Bucket=input_bucket, Key=object_key)
        result["deleted"]["input_file"] = True
    except Exception as e:
        error_msg = f"Error deleting from input bucket: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete from output bucket. The output bucket is versioning-enabled, and
    # document version history pins prior runs' output bytes as noncurrent
    # object versions. A versionless delete_object would only add delete markers
    # and leak those pinned bytes forever, so on a full document delete we purge
    # ALL versions (and any delete markers) under the prefix to reclaim storage.
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        deleted_output_count = 0

        for page in paginator.paginate(Bucket=output_bucket, Prefix=object_key):
            entries = page.get("Versions", []) + page.get("DeleteMarkers", [])
            objects = [
                {"Key": e["Key"], "VersionId": e["VersionId"]}
                for e in entries
                if e.get("VersionId") and _is_document_output(e["Key"], object_key)
            ]
            # delete_objects accepts up to 1000 keys per call
            for i in range(0, len(objects), 1000):
                batch = objects[i : i + 1000]
                s3_client.delete_objects(
                    Bucket=output_bucket, Delete={"Objects": batch, "Quiet": True}
                )
                deleted_output_count += len(batch)

        result["deleted"]["output_files"] = deleted_output_count
        logger.debug(f"Deleted {deleted_output_count} output object versions")
    except Exception as e:
        error_msg = f"Error deleting from output bucket: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete list entries
    try:
        deletion_success = delete_list_entries_robust(
            tracking_table, object_key, document_metadata
        )
        result["deleted"]["list_entries"] = deletion_success
    except Exception as e:
        error_msg = f"Error in list entry deletion: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete document version (run) records. The pinned output object versions
    # and run manifests they reference were already purged above (the
    # all-versions prefix delete covers the runs/ prefix too), so only the run
    # tracking items remain.
    try:
        run_count = _delete_run_records(tracking_table, object_key)
        result["deleted"]["run_records"] = run_count
    except Exception as e:
        error_msg = f"Error deleting run records: {str(e)}"
        logger.error(error_msg)
        result["errors"].append(error_msg)

    # Delete document record
    if document_metadata:
        try:
            tracking_table.delete_item(Key={"PK": doc_pk, "SK": "none"})
            result["deleted"]["document_record"] = True
            logger.debug(f"Deleted document record for {object_key}")
        except Exception as e:
            error_msg = f"Error deleting document record: {str(e)}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    result["success"] = len(result["errors"]) == 0
    return result


def delete_documents(
    object_keys: List[str],
    tracking_table,
    s3_client,
    input_bucket: str,
    output_bucket: str,
    dry_run: bool = False,
    continue_on_error: bool = True,
) -> Dict[str, Any]:
    """
    Delete multiple documents and all their associated data.

    Args:
        object_keys: List of document object keys (S3 paths)
        tracking_table: DynamoDB table resource
        s3_client: boto3 S3 client
        input_bucket: Input S3 bucket name
        output_bucket: Output S3 bucket name
        dry_run: If True, only report what would be deleted
        continue_on_error: If True, continue deleting other documents on error

    Returns:
        Dict with deletion results:
        - success: bool (True if all deleted successfully)
        - deleted_count: int
        - failed_count: int
        - results: List[Dict] with per-document results
    """
    results = []
    deleted_count = 0
    failed_count = 0

    for object_key in object_keys:
        try:
            result = delete_single_document(
                object_key=object_key,
                tracking_table=tracking_table,
                s3_client=s3_client,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                dry_run=dry_run,
            )
            results.append(result)

            if result["success"]:
                deleted_count += 1
            else:
                failed_count += 1
                if not continue_on_error:
                    break

        except Exception as e:
            logger.error(f"Error deleting document {object_key}: {str(e)}")
            results.append(
                {"success": False, "object_key": object_key, "errors": [str(e)]}
            )
            failed_count += 1
            if not continue_on_error:
                break

    return {
        "success": failed_count == 0,
        "deleted_count": deleted_count,
        "failed_count": failed_count,
        "total_count": len(object_keys),
        "results": results,
        "dry_run": dry_run,
    }


def _scan_all_document_keys(
    tracking_table, status_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Scan all document items from the tracking table.

    Args:
        tracking_table: DynamoDB table resource
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of DynamoDB items with PK starting with 'doc#'
    """
    from boto3.dynamodb.conditions import Attr

    items: List[Dict[str, Any]] = []
    filter_expr = Attr("PK").begins_with("doc#")
    if status_filter:
        filter_expr = filter_expr & Attr("Status").eq(status_filter)

    scan_kwargs: Dict[str, Any] = {"FilterExpression": filter_expr}

    while True:
        response = tracking_table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return items


def get_documents_by_batch(
    tracking_table, batch_id: str, status_filter: Optional[str] = None
) -> List[str]:
    """
    Get all document object keys for a batch.

    Args:
        tracking_table: DynamoDB table resource
        batch_id: Batch ID. Matched as a leading path segment, so ``batch-1``
            selects ``batch-1/a.pdf`` and does **not** select ``batch-10/b.pdf``.
            For substring or wildcard selection use
            :func:`get_documents_by_pattern`.
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of object keys
    """
    object_keys = []

    try:
        items = _scan_all_document_keys(tracking_table, status_filter)
        for item in items:
            object_key = item.get("ObjectKey", "")
            if _is_in_batch(object_key, batch_id):
                object_keys.append(object_key)
    except Exception as e:
        logger.error(f"Error getting documents for batch {batch_id}: {str(e)}")

    return object_keys


def get_documents_by_pattern(
    tracking_table, pattern: str, status_filter: Optional[str] = None
) -> List[str]:
    """
    Get all document object keys matching a wildcard pattern.

    Uses fnmatch-style patterns (*, ?, [seq], [!seq]).

    Examples:
        - ``"batch-123/*"`` — all docs in batch-123
        - ``"*/invoice*.pdf"`` — any invoice PDF in any batch
        - ``"*2025*"`` — any doc with 2025 in the key

    Args:
        tracking_table: DynamoDB table resource
        pattern: Wildcard pattern to match against object keys
        status_filter: Optional status filter ('COMPLETED', 'FAILED', 'PROCESSING', etc.)

    Returns:
        List of matching object keys
    """
    object_keys = []

    try:
        items = _scan_all_document_keys(tracking_table, status_filter)
        for item in items:
            object_key = item.get("ObjectKey", "")
            if fnmatch.fnmatch(object_key, pattern):
                object_keys.append(object_key)
    except Exception as e:
        logger.error(f"Error getting documents for pattern {pattern}: {str(e)}")

    return object_keys
