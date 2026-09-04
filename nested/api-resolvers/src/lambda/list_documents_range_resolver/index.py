# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda resolver for listDocumentsByDateRange GraphQL query.

Performs server-side iteration through date/shard partitions and
batch-fetches document details, returning paginated results.

This avoids the client-side fan-out pattern used for short time periods,
making it suitable for custom date ranges of any length.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

# Vendored verbatim from idp_common/config_scope.py: this function has no
# idp_common layer (it is on the hottest UI query and is kept dependency-free).
# test_config_scope_vendored.py fails if the copies drift.
# The sys.path insert makes the sibling import work both in Lambda (where the
# handler directory is already on the path) and when another suite loads this
# module by file path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_scope import scope_allows  # noqa: E402

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")

# User scope cache (TTL-based, per Lambda container)
_user_scope_cache = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds

# Must match DOCUMENT_LIST_SHARDS_PER_DAY in the frontend
SHARDS_PER_DAY = 6
HOURS_PER_SHARD = 24 // SHARDS_PER_DAY  # 4 hours

# Limits
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
BATCH_GET_LIMIT = 100  # DynamoDB BatchGetItem limit


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal objects from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_range(start_date: str, end_date: str):
    """Yield each date string (YYYY-MM-DD) from start_date to end_date inclusive."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    current = start
    while current <= end:
        yield current.strftime("%Y-%m-%d")
        current += timedelta(days=1)


def _shard_pks_for_range(start_dt: datetime, end_dt: datetime):
    """
    Return an ordered list of (date_str, shard_index) tuples covering the
    time window [start_dt, end_dt].

    Each shard covers a 4-hour window.  We generate PKs from the earliest
    shard that contains start_dt through the latest shard that contains end_dt.
    """
    pairs = []
    current = start_dt.replace(minute=0, second=0, microsecond=0)
    # Align to shard boundary
    current = current.replace(hour=(current.hour // HOURS_PER_SHARD) * HOURS_PER_SHARD)

    while current <= end_dt:
        date_str = current.strftime("%Y-%m-%d")
        shard = current.hour // HOURS_PER_SHARD
        pairs.append((date_str, shard))
        current += timedelta(hours=HOURS_PER_SHARD)

    return pairs


def _query_shard(table, date_str: str, shard: int, start_iso: str, end_iso: str):
    """
    Query a single shard partition and return list entries whose SK timestamp
    falls within [start_iso, end_iso].
    """
    shard_pad = f"{shard:02d}"
    pk = f"list#{date_str}#s#{shard_pad}"

    # SK format: ts#{ISO_TIMESTAMP}#id#{OBJECT_KEY}
    # We can use begins_with on the date portion for a coarse filter,
    # then apply a fine-grained filter for exact timestamp boundaries.
    items = []
    query_kwargs = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "Select": "ALL_ATTRIBUTES",
    }

    while True:
        response = table.query(**query_kwargs)
        for item in response.get("Items", []):
            # Extract timestamp from SK: ts#2026-02-07T14:22:00.000Z#id#doc.pdf
            sk = item.get("SK", "")
            if sk.startswith("ts#"):
                parts = sk.split("#id#", 1)
                ts_part = parts[0][3:]  # strip "ts#"
                # Only include items within the requested range
                if start_iso <= ts_part <= end_iso:
                    items.append(item)
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return items


def _batch_get_documents(table_name, object_keys):
    """
    Fetch full document records using BatchGetItem for efficiency.
    Returns a dict of ObjectKey -> document item.
    """
    if not object_keys:
        return {}

    ddb_client = boto3.client("dynamodb")
    documents = {}

    # Process in chunks of BATCH_GET_LIMIT
    for i in range(0, len(object_keys), BATCH_GET_LIMIT):
        chunk = object_keys[i : i + BATCH_GET_LIMIT]
        keys = [
            {"PK": {"S": f"doc#{key}"}, "SK": {"S": "none"}}
            for key in chunk
        ]

        request_items = {table_name: {"Keys": keys}}

        while request_items:
            response = ddb_client.batch_get_item(RequestItems=request_items)

            for item in response.get("Responses", {}).get(table_name, []):
                # Convert DynamoDB JSON to regular dict
                doc = _unmarshall_item(item)
                if doc and doc.get("ObjectKey"):
                    documents[doc["ObjectKey"]] = doc

            # Handle unprocessed keys (throttling)
            request_items = response.get("UnprocessedKeys", {})

    return documents


def _unmarshall_item(ddb_item):
    """Convert a DynamoDB JSON item to a regular Python dict."""
    deserializer = boto3.dynamodb.types.TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in ddb_item.items()}


def _serialize_next_token(shard_index: int, item_offset: int) -> str:
    """Encode pagination state as a JSON string."""
    return json.dumps({"si": shard_index, "io": item_offset})


def _deserialize_next_token(token: str):
    """Decode pagination state."""
    if not token:
        return 0, 0
    try:
        data = json.loads(token)
        return data.get("si", 0), data.get("io", 0)
    except (json.JSONDecodeError, AttributeError):
        return 0, 0


# ---------------------------------------------------------------------------
# RBAC helpers (mirrors list_documents_gsi_resolver pattern)
# ---------------------------------------------------------------------------

def _get_caller_identity(event):
    """Extract caller's Cognito groups, username, and email from AppSync event identity."""
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    email = claims.get("email", "") or identity.get("username", "") or username

    if isinstance(groups, str):
        groups = [groups]

    return {
        "groups": groups,
        "username": username,
        "email": email,
        "is_admin": "Admin" in groups,
        "is_author": "Author" in groups,
        "is_reviewer": "Reviewer" in groups,
        "is_viewer": "Viewer" in groups,
    }


def _is_reviewer_only(caller):
    """Check if caller is a reviewer-only user (no Admin/Author/Viewer groups)."""
    return caller["is_reviewer"] and not caller["is_admin"] and not caller["is_author"] and not caller["is_viewer"]


def _get_user_allowed_config_versions(caller_email):
    """Look up user's allowedConfigVersions from UsersTable with caching."""
    users_table_name = os.environ.get("USERS_TABLE_NAME", "")
    if not users_table_name:
        return None

    now = time.time()
    cached = _user_scope_cache.get(caller_email)
    if cached and (now - cached["timestamp"]) < _USER_SCOPE_CACHE_TTL:
        return cached["scope"]

    try:
        users_table = dynamodb.Table(users_table_name)
        response = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key("email").eq(caller_email),
        )
        items = response.get("Items", [])
        if items:
            scope = items[0].get("allowedConfigVersions")
            result = list(scope) if scope and len(scope) > 0 else None
        else:
            result = None
    except Exception as e:
        logger.warning(f"Failed to look up user scope for {caller_email}: {e}")
        result = None

    _user_scope_cache[caller_email] = {"scope": result, "timestamp": now}
    return result


def _should_include_document(doc, caller, reviewer_only, allowed_versions):
    """Apply RBAC filtering to a single document.

    Returns True if the document should be included in results.
    """
    # Config-version scope filter (applies to non-admin users)
    if allowed_versions:
        # Fails CLOSED — see the same filter in list_documents_gsi_resolver: an
        # unstamped document cannot be proven in scope, so it is not shown.
        doc_version = doc.get("ConfigVersion") or doc.get("ConfigurationVersion")
        if not scope_allows(allowed_versions, doc_version):
            return False

    # Reviewer-only filter: only HITL-pending or owned documents
    if reviewer_only:
        hitl_triggered = doc.get("HITLTriggered", False)
        hitl_status = doc.get("HITLStatus", "")
        hitl_completed = doc.get("HITLCompleted", False)
        hitl_owner = doc.get("HITLReviewOwner", "")
        reviewer_id = caller["username"]
        reviewer_email = caller["email"]

        hitl_active = (
            hitl_triggered is True
            or hitl_status in ("PendingReview", "InProgress", "ReviewInProgress")
        )
        owner_is_me = hitl_owner in (reviewer_id, reviewer_email)

        if not hitl_active:
            return False
        if hitl_completed and not owner_is_me:
            return False
        if not hitl_completed and hitl_owner and not owner_is_me:
            return False

    return True


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """AppSync Lambda resolver handler."""
    logger.info("listDocumentsByDateRange invoked")
    logger.debug(f"Event: {json.dumps(event, default=str)}")

    args = event.get("arguments", {})
    start_date_time = args.get("startDateTime")
    end_date_time = args.get("endDateTime")
    limit = min(args.get("limit") or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    next_token = args.get("nextToken")

    if not start_date_time or not end_date_time:
        raise ValueError("startDateTime and endDateTime are required")

    # Parse ISO timestamps
    # Handle both formats: 2026-02-07T00:00:00.000Z and 2026-02-07T00:00:00Z
    start_dt = datetime.fromisoformat(start_date_time.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_date_time.replace("Z", "+00:00"))

    if start_dt > end_dt:
        raise ValueError("startDateTime must be before endDateTime")

    table_name = os.environ["TRACKING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    # Generate all shard partition keys for the range
    shard_pairs = _shard_pks_for_range(start_dt, end_dt)
    logger.info(
        f"Date range {start_date_time} → {end_date_time}: "
        f"{len(shard_pairs)} shards to query"
    )

    # Resume from pagination token
    start_shard_idx, start_item_offset = _deserialize_next_token(next_token)

    # Collect list entries across shards until we have enough for a page
    collected_entries = []
    current_shard_idx = start_shard_idx
    current_item_offset = start_item_offset if current_shard_idx == start_shard_idx else 0

    # ISO strings for filtering
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    result_next_token = None

    while current_shard_idx < len(shard_pairs) and len(collected_entries) < limit:
        date_str, shard = shard_pairs[current_shard_idx]
        logger.debug(f"Querying shard: {date_str}#s#{shard:02d}")

        shard_items = _query_shard(table, date_str, shard, start_iso, end_iso)

        # Apply offset if resuming within a shard
        if current_item_offset > 0:
            shard_items = shard_items[current_item_offset:]
            current_item_offset = 0

        remaining_capacity = limit - len(collected_entries)
        if len(shard_items) > remaining_capacity:
            # Take only what we need and set next_token
            collected_entries.extend(shard_items[:remaining_capacity])
            result_next_token = _serialize_next_token(
                current_shard_idx,
                (start_item_offset if current_shard_idx == start_shard_idx else 0)
                + remaining_capacity,
            )
            break
        else:
            collected_entries.extend(shard_items)
            current_shard_idx += 1

    # If we ran out of shards, no next token
    if current_shard_idx >= len(shard_pairs):
        result_next_token = None

    logger.info(f"Collected {len(collected_entries)} list entries")

    # RBAC: Get caller identity and scope
    caller = _get_caller_identity(event)
    reviewer_only = _is_reviewer_only(caller)
    allowed_versions = None
    if not caller["is_admin"]:
        allowed_versions = _get_user_allowed_config_versions(caller["email"])

    logger.info(f"Caller groups: {caller['groups']}, reviewer_only: {reviewer_only}")

    # Extract ObjectKeys from list entries
    object_keys = [entry.get("ObjectKey") for entry in collected_entries if entry.get("ObjectKey")]

    # Batch-fetch full document records
    documents_map = _batch_get_documents(table_name, object_keys)
    logger.info(f"Fetched {len(documents_map)} document details")

    # Build response, preserving list entry PK/SK for compatibility
    # Apply RBAC filtering (reviewer-only + config-version scope)
    documents = []
    for entry in collected_entries:
        obj_key = entry.get("ObjectKey")
        if obj_key and obj_key in documents_map:
            doc = documents_map[obj_key]
            # RBAC filter
            if not _should_include_document(doc, caller, reviewer_only, allowed_versions):
                continue
            # Add list entry PK/SK for potential deletion/reprocessing
            doc["ListPK"] = entry.get("PK")
            doc["ListSK"] = entry.get("SK")
            documents.append(doc)

    # Sort by InitialEventTime descending (most recent first)
    documents.sort(
        key=lambda d: d.get("InitialEventTime", ""),
        reverse=True,
    )

    response = {
        "Documents": documents,
        "nextToken": result_next_token,
    }

    logger.info(
        f"Returning {len(documents)} documents (after RBAC filtering), "
        f"hasNextPage={result_next_token is not None}"
    )

    return response
