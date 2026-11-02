# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda resolver for listDocuments and getDocumentCount GraphQL queries.

Uses two GSIs on TrackingTable that share one key schema (ItemType, InitialEventTime):
- listDocuments: Paginated query with date range filtering and RBAC-based filtering,
  on TypeDateIndexV2, falling back to TypeDateIndex while V2 is backfilling — see
  `_query_list_index`
- getDocumentCount: the header figure beside that list, carrying the same filters,
  on TypeDateIndex. The two indexes hold the same rows (both are sparse on the same
  key attributes), so the count and the list agree whichever index each reads.

RBAC (Role-Based Access Control):
- Admin/Author/Viewer: See all documents (scoped by allowedConfigVersions if set)
- Reviewer: See only documents a review is owed on that are unclaimed (pending, or
  released back to the queue) or claimed by them, plus finished reviews they own on
  rows carrying the legacy HITLTriggered flag — see `_reviewer_filter_expression`

Both operations apply both filters, from the same two helpers
(`_reviewer_filter_expression`, `_item_in_config_scope`), so the count cannot
report documents the list does not show. A caller whose scope cannot be resolved
is denied rather than treated as unrestricted — see `_caller_scope_or_deny`.

Performance: O(matched items) instead of O(total table items)
"""

import json
import logging
import os
import sys
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

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
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)

# Vendored verbatim from idp_common/review_queues/statuses.py, for the same
# reason; test_review_modules_vendored.py fails if the copies drift.
from review_statuses import (  # noqa: E402
    LegacyHitlStatus,
    ReviewStatus,
    normalize_hitl_status,
)

#: Legacy HITL statuses that make a document part of a Reviewer's working set.
#: ``Review Pending`` is what ``releaseReview`` writes: a released review goes back
#: to the queue for another Reviewer to claim. Matched exactly, in both document-list
#: resolvers, so the two lists agree row for row.
_REVIEWER_ACTIVE_STATUSES = (
    LegacyHitlStatus.PENDING_REVIEW.value,
    LegacyHitlStatus.REVIEW_PENDING.value,
    LegacyHitlStatus.IN_PROGRESS.value,
    LegacyHitlStatus.REVIEW_IN_PROGRESS.value,
)


def _review_finished(hitl_status):
    """Whether a stored HITLStatus says the document's review has finished.

    A legacy review completed or skipped, or a queue review that ended complete or
    rejected. Neither queue outcome is accompanied by ``HITLCompleted``: no
    review-queue writer sets it. ``COMPLETED`` is the one finished spelling a real
    writer stored that the exact-match normaliser does not know (the earlier
    SageMaker A2I flow). The web UI's ``isHitlReviewFinished`` is the same rule.
    """
    return (
        normalize_hitl_status(hitl_status).kind in ("complete", "rejected", "skipped")
        or hitl_status == "COMPLETED"
    )


#: Every stored HITLStatus value that reads as a finished review, derived from the
#: declared vocabularies through ``_review_finished`` so a FilterExpression can test
#: it. Exact values: the normaliser also accepts surrounding whitespace, which no
#: writer stores.
_FINISHED_HITL_STATUSES = tuple(
    sorted(
        value
        for value in (
            *(s.value for s in ReviewStatus),
            *(s.value for s in LegacyHitlStatus),
            "COMPLETED",
        )
        if _review_finished(value)
    )
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")

# User scope cache (TTL-based, per Lambda container)
_user_scope_cache = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds

# Stands in for "this caller owns nothing" in the reviewer filter. HITLReviewOwner
# holds a Cognito username or an email address, so no row can carry this value.
_UNMATCHABLE_OWNER = "\x00-no-such-reviewer-\x00"

# Bounds on the SCOPED getDocumentCount tally. A scoped caller's count is computed
# from index rows rather than taken from DynamoDB's `Count`, because the
# config-version filter cannot be expressed as a FilterExpression. That reads the
# same index either way, but it transfers and deserialises item data where COUNT
# transferred a number — so on a very large date range a scoped caller can exhaust
# this function's 30s timeout (and API Gateway's 29s ceiling) where an unscoped
# caller would not. Rather than let the request die with a 502, the tally stops at
# whichever bound is reached first and says so.
_COUNT_MAX_PAGES = 200
# Leave enough of the invocation for the response to be written and returned.
_COUNT_TIME_RESERVE_MS = 5_000

# Limits
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# GSI names. Both are keyed (ItemType, InitialEventTime). TypeDateIndex is read by
# getDocumentCount and by other resolvers; its projection cannot be widened in
# place, so TypeDateIndexV2 projects the document attributes this list reads plus
# the per-document counts it shows (ConfigRevision, MissingRequiredFieldCount,
# ProcessingIssueCount). See the TrackingTable definition in template.yaml.
TYPE_DATE_INDEX = "TypeDateIndex"
LIST_INDEX = "TypeDateIndexV2"

# The two ValidationException messages DynamoDB returns when LIST_INDEX cannot be
# read, both recorded from live queries during an upgrade. "does not have the
# specified index" is returned before the index exists and through the first,
# resource-allocation phase of creating it (CREATING, Backfilling false), and
# after it has been deleted by hand to recover a rolled-back upgrade; "backfilling"
# is returned while it is filled. Matched on these phrases rather than on the
# error code alone, so an invalid request is never silently retried against
# another index.
_LIST_INDEX_UNAVAILABLE_PHRASES = (
    "backfilling global secondary index",
    "does not have the specified index",
)

# TypeDateIndex hash-key values; must match idp_common.dynamodb.service. Documents
# submitted by Test Studio carry their own ItemType, so they never appear in the
# production Document List. Auto Optimizer submissions are NOT tagged today — they
# would need to stamp submission-source the way the test copier does.
ITEM_TYPE_DOCUMENT = "document"
ITEM_TYPE_TEST_DOCUMENT = "test-document"


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal objects from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


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
    # The immutable Cognito sub, the config-version scope lookup's PREFERRED key.
    # It is not a fallback for the email: the two go to disjoint key spaces on the
    # UsersTable. See caller_sub_from_claims.
    caller_sub = caller_sub_from_claims(claims)

    # Groups may be a string if user is in one group
    if isinstance(groups, str):
        groups = [groups]

    return {
        "groups": groups,
        "username": username,
        "email": email,
        "sub": caller_sub,
        "is_admin": "Admin" in groups,
        "is_author": "Author" in groups,
        "is_reviewer": "Reviewer" in groups,
        "is_viewer": "Viewer" in groups,
    }


def _is_reviewer_only(caller):
    """Check if caller is a reviewer-only user (no Admin/Author/Viewer groups)."""
    return caller["is_reviewer"] and not caller["is_admin"] and not caller["is_author"] and not caller["is_viewer"]


def _get_user_allowed_config_versions(caller_email, caller_sub=""):
    """The caller's allowedConfigVersions, or None if unrestricted.

    Thin wrapper over the shared fail-closed lookup in ``config_scope`` so every
    consumer of this rule resolves it identically. Raises ``ScopeLookupError``
    when the scope cannot be evaluated; the caller must deny.
    """
    return resolve_allowed_config_versions(
        caller_email,
        caller_sub=caller_sub,
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=dynamodb,
        cache=_user_scope_cache,
        cache_ttl=_USER_SCOPE_CACHE_TTL,
    )


def _caller_scope_or_deny(caller, operation):
    """The caller's config-version scope for one request, or a denial.

    Admins are unrestricted and never looked up. For everyone else this fails
    CLOSED: a scope that cannot be *evaluated* (no UsersTable wired, no email
    claim on the verified identity, a failed DynamoDB query) is not a caller
    without restrictions, and reading it as one hands every document in the
    deployment to a caller entitled to a subset (AUTH.T07).
    """
    if caller.get("is_admin"):
        return None
    try:
        allowed_versions = _get_user_allowed_config_versions(
            caller.get("email", ""), caller.get("sub", "")
        )
    except ScopeLookupError as e:
        logger.error(f"Denying {operation}: config-version scope unresolved: {e}")
        raise PermissionError(
            "Unauthorized: your configuration scope could not be verified"
        ) from e
    logger.info(
        f"Config-version scope: {allowed_versions or 'unrestricted (no scope set)'}"
    )
    return allowed_versions


def _reviewer_filter_expression(caller):
    """The server-side filter that reduces the index to a reviewer's own work.

    A Reviewer sees HITL-pending documents (not finished) that are either
    unassigned or assigned to them, PLUS finished documents they own:

        HITL is active AND (
            (not finished AND (no owner OR owner is me))
            OR owner is me      # in-progress + finished reviews they own
        )

    Finished is `HITLCompleted` OR a `HITLStatus` that `_review_finished` reads as
    finished. The flag alone is not enough: a review queue records its outcome
    (`complete` or `rejected`) only in `HITLStatus`, so a finished queue review on a
    row still carrying the legacy `HITLTriggered` flag read as owed to every
    Reviewer.

    HITL counts as active when `HITLTriggered` is true OR `HITLStatus` is one of
    `_REVIEWER_ACTIVE_STATUSES` (pending, released back to the queue, or in
    progress) — current writers no longer set the boolean. A finished status is not
    active, so on a row without `HITLTriggered` the "finished documents they own"
    disjunct is reached only through `HITLCompleted` beside a status still active.

    The owner match tests `HITLReviewOwner` against the caller's username *and*
    their email, because claim_review stores `identity.username` (often the
    email) while this resolver reads `cognito:username` from the claims. The
    email disjunct is added only when there is an email to match: it comes from
    the `email` claim alone now, so it can legitimately be empty, and an
    equality test against "" would match rows whose owner attribute is an empty
    string rather than rows owned by this caller. If the identity yields neither
    identifier, "owned by me" matches nothing — such a caller cannot own a
    review, and they still see the unassigned pending queue.

    Shared by listDocuments and getDocumentCount so the header count cannot
    disagree with the rows on screen.
    """
    owner_values = [v for v in (caller["username"], caller["email"]) if v] or [
        _UNMATCHABLE_OWNER
    ]
    owner_is_me = Attr("HITLReviewOwner").eq(owner_values[0])
    for value in owner_values[1:]:
        owner_is_me = owner_is_me | Attr("HITLReviewOwner").eq(value)
    # Built from the whole tuple rather than by index, so a status added to it
    # reaches this filter as it reaches the date-range resolver's membership test.
    hitl_active = Attr("HITLTriggered").eq(True) | Attr("HITLStatus").is_in(
        list(_REVIEWER_ACTIVE_STATUSES)
    )
    review_finished = Attr("HITLCompleted").eq(True) | Attr("HITLStatus").is_in(
        list(_FINISHED_HITL_STATUSES)
    )
    return hitl_active & (
        (
            ~review_finished &
            (
                Attr("HITLReviewOwner").not_exists() |
                Attr("HITLReviewOwner").eq("") |
                owner_is_me
            )
        ) |
        owner_is_me
    )


def _list_filter(caller, reviewer_only):
    """The server-side filter for a restricted caller, or None (unfiltered)."""
    if reviewer_only:
        return _reviewer_filter_expression(caller)
    return None


def _item_in_config_scope(item, allowed_versions):
    """Whether a scoped caller may see this index row.

    Fails CLOSED: a document with no ConfigVersion cannot be proven in scope, so
    a scoped caller does not see it. It used to be admitted, which leaked every
    document processed before config-version stamping (and any document whose
    stamp failed) to every scoped user.

    Shared by listDocuments and getDocumentCount so the header count cannot
    disagree with the rows on screen.
    """
    if not allowed_versions:
        return True
    doc_version = item.get("ConfigVersion") or item.get("ConfigurationVersion")
    return scope_allows(allowed_versions, doc_version)


def handler(event, context):
    """
    AppSync Lambda resolver handler.
    
    Routes to listDocuments or getDocumentCount based on the field name.
    """
    field_name = event.get("info", {}).get("fieldName", "")
    logger.info(f"Resolver invoked for field: {field_name}")
    
    if field_name == "listDocuments":
        return list_documents(event)
    elif field_name == "getDocumentCount":
        return get_document_count(event, context)
    else:
        raise ValueError(f"Unknown field: {field_name}")


def _item_type_for_view(view):
    """Which TypeDateIndex hash key to query for the requested view.

    PRODUCTION (the default) lists ordinary uploads; TEST lists documents submitted
    by Test Studio. The views are mutually exclusive by design;
    there is no combined view.

    Selecting on the index key rather than filtering a projected attribute keeps
    pagination exact: DynamoDB applies FilterExpression after Limit, so a filtered
    page of 50 can return a single row plus a nextToken, with no indication that
    the rest were dropped.
    """
    return ITEM_TYPE_TEST_DOCUMENT if str(view or "").upper() == "TEST" else ITEM_TYPE_DOCUMENT


def list_documents(event):
    """
    List documents using TypeDateIndex GSI with server-side pagination and RBAC filtering.
    
    Args (from GraphQL):
        startDateTime: ISO 8601 start time
        endDateTime: ISO 8601 end time  
        limit: Page size (default 50, max 200)
        nextToken: Pagination token from previous response
    
    Returns:
        {
            Documents: [Document],
            nextToken: String | null,
            totalCount: Int | null
        }
    """
    args = event.get("arguments", {})
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")
    limit = min(int(args.get("limit") or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
    next_token = args.get("nextToken")
    
    table_name = os.environ["TRACKING_TABLE_NAME"]
    table = dynamodb.Table(table_name)
    
    # Get caller identity for RBAC
    caller = _get_caller_identity(event)
    reviewer_only = _is_reviewer_only(caller)

    logger.info(f"Caller groups: {caller['groups']}, reviewer_only: {reviewer_only}, username: {caller['username']}")

    # Resolve the config-version scope BEFORE querying: a caller whose scope
    # cannot be evaluated is denied, and there is no point reading the table for
    # a request that will be refused.
    allowed_versions = _caller_scope_or_deny(caller, "listDocuments")

    # Build GSI query. The index is chosen by `_query_list_index`.
    query_kwargs = {
        "Limit": limit,
        "ScanIndexForward": False,  # Newest first
    }
    
    # Key condition: the ItemType selects the view (production vs test-submitted).
    item_type = _item_type_for_view(args.get("view"))
    if start_dt and end_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").between(start_dt, end_dt)
        )
    elif start_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").gte(start_dt)
        )
    elif end_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").lte(end_dt)
        )
    else:
        query_kwargs["KeyConditionExpression"] = Key("ItemType").eq(item_type)
    
    # RBAC: Server-side filtering for Reviewer-only users
    list_filter = _list_filter(caller, reviewer_only)
    if list_filter is not None:
        query_kwargs["FilterExpression"] = list_filter
        logger.info("Applied reviewer filter for the caller")


    # Handle pagination token
    if next_token:
        try:
            query_kwargs["ExclusiveStartKey"] = json.loads(next_token)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Invalid nextToken: {next_token}")
    
    logger.info(f"Querying {LIST_INDEX}: limit={limit}, start={start_dt}, end={end_dt}")
    
    try:
        response = _query_list_index(table, query_kwargs)
    except Exception as e:
        logger.error(f"GSI query failed: {e}")
        raise
    
    items = response.get("Items", [])
    last_key = response.get("LastEvaluatedKey")
    
    logger.info(f"Query returned {len(items)} items, has more: {last_key is not None}")

    # Transform GSI projection items to match the Document GraphQL type
    # Apply config-version scope filtering (post-query filter since ConfigVersion is in GSI projection)
    documents = []
    for item in items:
        if not _item_in_config_scope(item, allowed_versions):
            logger.info(
                f"Scope filter rejected PK={item.get('PK')}: ConfigVersion="
                f"{item.get('ConfigVersion') or item.get('ConfigurationVersion')!r} "
                f"not in {allowed_versions}"
            )
            continue  # Skip documents outside user's scope
        doc = _gsi_item_to_document(item)
        documents.append(doc)
    
    logger.info(f"After scope filtering: {len(documents)} documents from {len(items)} items")
    
    result = {
        "Documents": documents,
        "nextToken": json.dumps(last_key, cls=DecimalEncoder) if last_key else None,
    }
    
    return result


def _list_index_unavailable(error):
    """Whether a query failed only because LIST_INDEX cannot be read yet (or any more)."""
    details = error.response.get("Error", {}) if isinstance(error, ClientError) else {}
    if details.get("Code") != "ValidationException":
        return False
    message = str(details.get("Message", "")).lower()
    return LIST_INDEX.lower() in message and any(
        phrase in message for phrase in _LIST_INDEX_UNAVAILABLE_PHRASES
    )


def _query_list_index(table, query_kwargs):
    """Query LIST_INDEX, or TypeDateIndex while LIST_INDEX is unavailable.

    A GSI added to an existing table cannot be queried until DynamoDB has finished
    backfilling it, and CloudFormation reports the table updated before that: on a
    live upgrade of a stack holding a handful of documents the table resource was
    complete about eight minutes before the index became readable, and the window
    grows with the table. DescribeTable can also report the index ACTIVE a little
    before a query on it succeeds, so the query's own error is what decides. Until
    then the list is served from TypeDateIndex, which has the
    same keys and so the same rows, the same order and an interchangeable
    `nextToken` (a GSI's LastEvaluatedKey is the table key plus the index key,
    identical for both). What the fallback loses is only the attributes V2 adds:
    those rows carry no counts, and the UI shows a dash for each.
    """
    try:
        return table.query(IndexName=LIST_INDEX, **query_kwargs)
    except ClientError as e:
        if not _list_index_unavailable(e):
            raise
        logger.warning(
            "%s is not readable (%s); serving listDocuments from %s, whose rows "
            "carry no per-document counts.",
            LIST_INDEX,
            e.response.get("Error", {}).get("Message"),
            TYPE_DATE_INDEX,
        )
        return table.query(IndexName=TYPE_DATE_INDEX, **query_kwargs)


def _remaining_invocation_ms(context):
    """Milliseconds left in this invocation, or None if the runtime does not say.

    Read defensively: `context` is whatever the caller passed — the real Lambda
    context in production, `None` or a double in tests — so neither the attribute
    nor an integer answer can be assumed.
    """
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(remaining):
        return None
    try:
        return int(remaining())
    except (TypeError, ValueError, AttributeError):
        return None


def _count_budget_exhausted(pages, context):
    """Whether the scoped tally must stop, and why. See `_COUNT_MAX_PAGES`."""
    if pages >= _COUNT_MAX_PAGES:
        return f"page cap of {_COUNT_MAX_PAGES} reached"
    left = _remaining_invocation_ms(context)
    if left is not None and left < _COUNT_TIME_RESERVE_MS:
        return f"only {left}ms of the invocation left"
    return None


def get_document_count(event, context=None):
    """
    Get document count using the TypeDateIndex GSI.

    Counts exactly the documents `listDocuments` would show the same caller, and
    is the header figure beside that list. It therefore carries the same two RBAC
    filters, resolved from the same helpers:

      * the reviewer-only filter, as a server-side `FilterExpression` — a `COUNT`
        query's `Count` is the post-filter number, so this costs nothing extra;
      * the config-version scope, which cannot be expressed as a
        `FilterExpression` (entries may be glob patterns and an absent
        `ConfigVersion` must fail closed), so a **scoped** caller's count is
        tallied from the index rows instead of taken from `Count`. `ConfigVersion`
        is in the GSI's INCLUDE projection, so this reads the same index, not the
        base table.

    An unrestricted caller — the common case, and every Admin — keeps the
    `Select: COUNT` path, which never reads item data.

    Both filters exist because a count is data too: without them a caller scoped
    to one configuration profile, or a Reviewer who may see only their own HITL
    work, is told how many documents exist outside it, and the header disagrees
    with the rows underneath.

    The scoped tally is **bounded** — see `_COUNT_MAX_PAGES`. If it stops early the
    response carries `approximate: true` and the reason is logged at WARNING.

    ⚠️ Nothing surfaces that flag to a user. It is not in the `DocumentCount` type in
    `schema.graphql`, so the UI's generated client drops it, and no alarm watches the
    log line — so a scoped caller on a very large date range sees a low number with no
    indication it was truncated. Declaring the field means regenerating the UI's typed
    client and rendering it (a "10,000+" affordance), which is the finished version of
    this and is deliberately not done here. Until then, the WARNING in this function's
    log group is the only signal, and reading a scoped count as exact requires checking
    for it.

    Args (from GraphQL):
        startDateTime: ISO 8601 start time
        endDateTime: ISO 8601 end time

    Returns:
        { count: Int }, plus `approximate: True` when the tally was bounded
    """
    args = event.get("arguments", {})
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")

    table_name = os.environ["TRACKING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    caller = _get_caller_identity(event)
    reviewer_only = _is_reviewer_only(caller)
    allowed_versions = _caller_scope_or_deny(caller, "getDocumentCount")

    # Build GSI count query. Must use the same view as listDocuments, or the
    # header count would not match the rows on screen.
    item_type = _item_type_for_view(args.get("view"))
    query_kwargs = {
        "IndexName": TYPE_DATE_INDEX,
        # A scoped caller needs the projected ConfigVersion of each row to decide
        # whether it counts, so only an unscoped caller can use COUNT.
        "Select": "ALL_PROJECTED_ATTRIBUTES"
        if allowed_versions
        else "COUNT",
    }

    if start_dt and end_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").between(start_dt, end_dt)
        )
    elif start_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").gte(start_dt)
        )
    elif end_dt:
        query_kwargs["KeyConditionExpression"] = (
            Key("ItemType").eq(item_type) &
            Key("InitialEventTime").lte(end_dt)
        )
    else:
        query_kwargs["KeyConditionExpression"] = Key("ItemType").eq(item_type)

    list_filter = _list_filter(caller, reviewer_only)
    if list_filter is not None:
        query_kwargs["FilterExpression"] = list_filter

    logger.info(
        f"Counting documents: start={start_dt}, end={end_dt}, "
        f"reviewer_only={reviewer_only}, scoped={bool(allowed_versions)}"
    )

    # Paginate through all count pages (DynamoDB may split count across pages)
    total_count = 0
    pages = 0
    bounded_by = None
    while True:
        response = table.query(**query_kwargs)
        pages += 1
        if allowed_versions:
            # Same row decisions as listDocuments, so the count equals the rows.
            total_count += sum(
                1
                for item in response.get("Items") or []
                if _item_in_config_scope(item, allowed_versions)
            )
        else:
            total_count += response.get("Count", 0)

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        # Only the scoped path carries the per-page item payload that can run this
        # invocation out of time; the COUNT path paginates to completion as before.
        if allowed_versions:
            bounded_by = _count_budget_exhausted(pages, context)
            if bounded_by:
                break
        query_kwargs["ExclusiveStartKey"] = last_key

    if bounded_by:
        logger.warning(
            "getDocumentCount stopped early after %d page(s) (%s); returning an "
            "approximate count of %d for a scoped caller. Narrow the date range.",
            pages,
            bounded_by,
            total_count,
        )
        return {"count": total_count, "approximate": True}

    logger.info(f"Total document count: {total_count} ({pages} page(s))")
    return {"count": total_count}


def _gsi_item_to_document(item):
    """
    Transform a GSI projection item to match the Document GraphQL type.
    
    The GSI INCLUDE projection has a subset of Document fields.
    We map them to the expected GraphQL field names.
    """
    # Extract PK to get ObjectKey if not directly available
    pk = item.get("PK", "")
    object_key = item.get("ObjectKey") or (pk.replace("doc#", "", 1) if pk.startswith("doc#") else pk)
    
    # Build confidence alert count from ConfidenceAlertCount attribute
    confidence_alert_count = item.get("ConfidenceAlertCount")
    
    doc = {
        "PK": item.get("PK"),
        "SK": item.get("SK", "none"),
        "ObjectKey": object_key,
        "ObjectStatus": item.get("ObjectStatus"),
        "InitialEventTime": item.get("InitialEventTime"),
        "CompletionTime": item.get("CompletionTime"),
        "ConfigVersion": item.get("ConfigVersion") or item.get("ConfigurationVersion"),
        # int() because DynamoDB numbers come back as Decimal, which the JSON
        # response encoder rejects.
        "ConfigRevision": int(item["ConfigRevision"])
        if item.get("ConfigRevision") is not None
        else None,
        "EvaluationStatus": item.get("EvaluationStatus"),
        "HITLStatus": item.get("HITLStatus"),
        "HITLTriggered": item.get("HITLTriggered"),
        "HITLCompleted": item.get("HITLCompleted"),
        "HITLReviewOwner": item.get("HITLReviewOwner"),
        "HITLReviewedBy": item.get("HITLReviewedBy"),
        "PageCount": item.get("NumPages"),
        "ConfidenceAlertCount": confidence_alert_count,
        # ProcessingIssueCount, MissingRequiredFieldCount and ConfigRevision are
        # projected by TypeDateIndexV2 but not by TypeDateIndex. On a row served
        # by the fallback (see `_query_list_index`), or one written before the
        # count existed, they are None and dropped below; the UI then shows a
        # dash rather than a 0 the row cannot support.
        "ProcessingIssueCount": item.get("ProcessingIssueCount"),
        "MissingRequiredFieldCount": item.get("MissingRequiredFieldCount"),
    }

    # Remove None values to keep response clean
    return {k: v for k, v in doc.items() if v is not None}
