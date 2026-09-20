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
from config_scope import (  # noqa: E402
    ScopeLookupError,
    caller_email_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)

# Same story for the log redactor: byte-identical copy of
# idp_common/utils/log_sanitizer.py, kept in step by
# scripts/sync_resolver_log_sanitizer.sh and asserted by
# scripts/tests/test_resolver_log_sanitizer.py.
from log_sanitizer import sanitize_event_for_logging  # noqa: E402

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

# Work budget: the most sequential DynamoDB Queries one invocation may issue.
#
# The shard fan-out is driven entirely by a caller-supplied value. The range is
# decomposed into one partition per HOURS_PER_SHARD window and `_query_shard` is
# called for each, in a `while` loop, one after another. The loop exits early once
# `limit` entries are collected, so a DENSE range is cheap — but a sparse one (a
# window holding fewer than `limit` documents, which includes every window before
# the deployment existed) walks every shard. Unbounded, that is SHARDS_PER_DAY
# queries per requested day with no ceiling at all.
#
# The budget to fit is the one the caller actually experiences, which is neither
# this function's `Timeout` (120s) nor API Gateway's integration limit (29s): the
# dispatcher invokes resolvers with a 20s read timeout
# (`_RESOLVER_READ_TIMEOUT_SECONDS` in http_api_dispatcher/index.py) and returns a
# labelled 504 when it expires. Past 20s the work is still billed and still
# consuming read capacity with nobody left to receive the answer.
#
# Measured cost of one empty-shard Query issued sequentially from in-region
# compute against a real tracking table: 3.74ms mean, 4.23ms p90 (n=200). Half the
# 20s window at the p90 cost is ~2360 queries; the other half covers cold start,
# the config-scope lookup, the BatchGetItem round trips for the page, and
# serialization.
MAX_SHARD_QUERIES_PER_REQUEST = 2360

# The caller-facing rule, in whole days, because that is what the request states
# and what an error message can usefully name. It is a SEPARATE number from the
# work budget above on purpose: `test_range_cap.py` recomputes the worst-case
# query count for this many days FROM SHARDS_PER_DAY and fails if it no longer
# fits MAX_SHARD_QUERIES_PER_REQUEST. So raising either this cap or the shard
# fan-out has to be justified against the measurement rather than silently
# multiplying the work a single request can buy.
MAX_RANGE_DAYS = 365

# The range cap bounds the WORST case; this bounds the ACTUAL one. A cap derived
# from a measured per-query cost is only as good as the measurement, and a
# throttled table, a hot partition or shards that are merely non-empty all cost
# more than the 4.23ms an empty one did. Mirrors the enforced pair already in
# list_documents_gsi_resolver (`_COUNT_MAX_PAGES` + `_COUNT_TIME_RESERVE_MS`):
# stop, return what was collected, and hand back a nextToken so the caller
# resumes exactly where this invocation stopped. The pagination token already
# encodes the shard index, so resuming is free.
#
# 15s, measured against the dispatcher's 20s window rather than this function's
# own Timeout: stopping after the dispatcher has stopped listening would answer
# nobody.
_SHARD_LOOP_BUDGET_SECONDS = 15.0
# Leave enough of the invocation to BatchGetItem the page and serialize it.
_SHARD_LOOP_TIME_RESERVE_MS = 5_000


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


def _align_to_shard(dt: datetime) -> datetime:
    """Floor ``dt`` to the start of the shard window that contains it."""
    return dt.replace(
        hour=(dt.hour // HOURS_PER_SHARD) * HOURS_PER_SHARD,
        minute=0,
        second=0,
        microsecond=0,
    )


def _projected_shard_queries(start_dt: datetime, end_dt: datetime) -> int:
    """How many shard Queries ``_shard_pks_for_range`` would issue, in O(1).

    Counted arithmetically rather than by generating the list, because the list is
    the thing being bounded: a range spanning centuries materializes tens of
    millions of tuples and exhausts the function's 512 MB before any limit check
    could look at them. ``test_range_cap.py`` asserts this agrees exactly with
    ``len(_shard_pks_for_range(...))``.
    """
    span = (end_dt - _align_to_shard(start_dt)).total_seconds()
    return int(span // (HOURS_PER_SHARD * 3600)) + 1


def _shard_pks_for_range(start_dt: datetime, end_dt: datetime):
    """
    Return an ordered list of (date_str, shard_index) tuples covering the
    time window [start_dt, end_dt].

    Each shard covers a 4-hour window.  We generate PKs from the earliest
    shard that contains start_dt through the latest shard that contains end_dt.
    """
    pairs = []
    # One implementation of the alignment rule, shared with the O(1) projection
    # in _projected_shard_queries so the bound cannot drift from what is iterated.
    current = _align_to_shard(start_dt)

    while current <= end_dt:
        date_str = current.strftime("%Y-%m-%d")
        shard = current.hour // HOURS_PER_SHARD
        pairs.append((date_str, shard))
        current += timedelta(hours=HOURS_PER_SHARD)

    return pairs


def _remaining_invocation_ms(context):
    """Milliseconds left in this invocation, or ``None`` when unknowable.

    A direct invoke or a test may pass no context, and the AppSync/Lambda context
    is duck-typed, so neither the attribute nor an integer answer can be assumed.
    """
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(remaining):
        return None
    try:
        return int(remaining())
    except (TypeError, ValueError, AttributeError):
        return None


def _shard_budget_exhausted(started_at: float, queries: int, context):
    """Whether the shard walk must stop now, and why. See the constants above."""
    if queries >= MAX_SHARD_QUERIES_PER_REQUEST:
        return f"query budget of {MAX_SHARD_QUERIES_PER_REQUEST} reached"
    elapsed = time.monotonic() - started_at
    if elapsed >= _SHARD_LOOP_BUDGET_SECONDS:
        return f"{elapsed:.1f}s spent, over the {_SHARD_LOOP_BUDGET_SECONDS}s budget"
    left = _remaining_invocation_ms(context)
    if left is not None and left < _SHARD_LOOP_TIME_RESERVE_MS:
        return f"only {left}ms of the invocation left"
    return None


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
    """Extract caller's Cognito groups, username, and email from the event identity.

    ``email`` is the config-version scope lookup key, so it comes from the
    ``email`` claim alone — see ``caller_email_from_claims`` in the vendored
    ``config_scope``. ``username`` keeps its fallback chain because it is not a
    scope key: it matches ``HITLReviewOwner`` for the reviewer-only view.
    """
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    email = caller_email_from_claims(claims)

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
    """The caller's allowedConfigVersions, or None if unrestricted.

    Thin wrapper over the shared fail-closed lookup in ``config_scope`` so every
    consumer of this rule resolves it identically. Raises ``ScopeLookupError``
    when the scope cannot be evaluated; the caller must deny.
    """
    return resolve_allowed_config_versions(
        caller_email,
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=dynamodb,
        cache=_user_scope_cache,
        cache_ttl=_USER_SCOPE_CACHE_TTL,
    )


def _caller_scope_or_deny(caller):
    """The caller's config-version scope for one request, or a denial.

    Admins are unrestricted and never looked up. For everyone else this fails
    CLOSED: a scope that cannot be *evaluated* (no UsersTable wired, no email
    claim on the verified identity, a failed DynamoDB query) is not a caller
    without restrictions, and reading it as one hands every document in the
    deployment to a caller entitled to a subset (AUTH.T07).
    """
    if caller["is_admin"]:
        return None
    try:
        return _get_user_allowed_config_versions(caller["email"])
    except ScopeLookupError as e:
        logger.error(
            "Denying listDocumentsByDateRange: config-version scope "
            "unresolved: %s",
            e,
        )
        raise PermissionError(
            "Unauthorized: your configuration scope could not be verified"
        ) from e


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
        # Only non-empty identifiers can match an owner. `reviewer_email` comes
        # from the `email` claim alone and may legitimately be absent, and an
        # unowned document carries `HITLReviewOwner == ""` — so comparing the two
        # empties would read "nobody owns it" as "I own it".
        owner_is_me = bool(hitl_owner) and hitl_owner in (
            v for v in (reviewer_id, reviewer_email) if v
        )

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
    logger.debug(
        "Event: %s", json.dumps(sanitize_event_for_logging(event), default=str)
    )

    args = event.get("arguments", {})
    start_date_time = args.get("startDateTime")
    end_date_time = args.get("endDateTime")
    # Clamped, not defaulted: MAX_PAGE_SIZE is a ceiling the caller cannot raise.
    # The lower clamp matters too — a negative `limit` makes the collection loop's
    # `len(collected) < limit` false on entry, so the request returns an empty page
    # and a null nextToken, which is indistinguishable from "the range is empty".
    limit = max(1, min(args.get("limit") or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    next_token = args.get("nextToken")

    if not start_date_time or not end_date_time:
        raise ValueError("startDateTime and endDateTime are required")

    # Parse ISO timestamps
    # Handle both formats: 2026-02-07T00:00:00.000Z and 2026-02-07T00:00:00Z
    # A malformed value raises ValueError, which the dispatcher reports as 400.
    start_dt = datetime.fromisoformat(start_date_time.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_date_time.replace("Z", "+00:00"))

    if start_dt > end_dt:
        raise ValueError("startDateTime must be before endDateTime")

    # Bound the caller-supplied range BEFORE any partition is generated or read.
    #
    # Refusing in band, naming the maximum, is the point. Accepting the request
    # meant the dispatcher's 20s read timeout fired and the browser showed
    # "Request failed (504)" with nothing to say the range was the problem, while
    # the resolver kept querying — up to its own 120s Timeout — and kept spending
    # read capacity on an answer nobody was left to receive. Any authenticated
    # caller could do that, repeatedly, for the cost of one HTTP request.
    #
    # The check is arithmetic, not a count of the generated list, because the list
    # is part of what is being bounded: a range spanning centuries materializes
    # tens of millions of tuples in `_shard_pks_for_range` and exhausts the
    # function's 512 MB before any limit could look at them.
    #
    # `>` on a timedelta, matching DateRangeModal.tsx exactly, so the client bound
    # and this one refuse the same set of ranges rather than leaving a sliver the
    # picker offers and the server rejects.
    span = end_dt - start_dt
    if span > timedelta(days=MAX_RANGE_DAYS):
        span_days = span.total_seconds() / 86400
        logger.warning(
            "Refusing listDocumentsByDateRange: %.1f-day range would issue %d "
            "sequential shard queries, over the %d-query budget (max %d days)",
            span_days,
            _projected_shard_queries(start_dt, end_dt),
            MAX_SHARD_QUERIES_PER_REQUEST,
            MAX_RANGE_DAYS,
        )
        raise ValueError(
            f"Date range too large: {span_days:.0f} days requested, "
            f"maximum is {MAX_RANGE_DAYS} days. "
            "Narrow the range, or page through it in shorter windows."
        )

    table_name = os.environ["TRACKING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    # RBAC: resolve the caller and their config-version scope BEFORE reading any
    # shard. A caller whose scope cannot be evaluated is denied, and there is no
    # point iterating partitions for a request that will be refused.
    caller = _get_caller_identity(event)
    reviewer_only = _is_reviewer_only(caller)
    allowed_versions = _caller_scope_or_deny(caller)
    logger.info(f"Caller groups: {caller['groups']}, reviewer_only: {reviewer_only}")

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
    budget_stop = None
    shard_queries = 0
    loop_started_at = time.monotonic()

    while current_shard_idx < len(shard_pairs) and len(collected_entries) < limit:
        # Checked BEFORE the query, so the shard named by current_shard_idx is
        # genuinely unread when the resume token is built below.
        budget_stop = _shard_budget_exhausted(loop_started_at, shard_queries, context)
        if budget_stop:
            break

        date_str, shard = shard_pairs[current_shard_idx]
        logger.debug(f"Querying shard: {date_str}#s#{shard:02d}")

        shard_items = _query_shard(table, date_str, shard, start_iso, end_iso)
        shard_queries += 1

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

    # The rule: a nextToken is owed whenever shards remain unread. Stating it that
    # way rather than per-exit-path matters, because two of the paths out of the loop
    # do not set one themselves. The budget stop is one. The other is a page that
    # fills EXACTLY: the `else` branch runs, the index advances, and the `while`
    # condition goes false without reaching the mid-shard `break` — so a page that
    # happens to come out exactly `limit` long would leave the rest of the range
    # unreachable.
    #
    # `current_item_offset` is the right resume offset in every path: it is zeroed as
    # soon as it has been applied, and the budget stop is checked before the query,
    # so an unconsumed offset is still pending against the shard being named.
    if result_next_token is None and current_shard_idx < len(shard_pairs):
        result_next_token = _serialize_next_token(current_shard_idx, current_item_offset)

    if budget_stop:
        logger.warning(
            "listDocumentsByDateRange stopped early after %d of %d shard queries "
            "(%s); returning a nextToken so the caller can resume",
            shard_queries,
            len(shard_pairs),
            budget_stop,
        )

    logger.info(f"Collected {len(collected_entries)} list entries")

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
