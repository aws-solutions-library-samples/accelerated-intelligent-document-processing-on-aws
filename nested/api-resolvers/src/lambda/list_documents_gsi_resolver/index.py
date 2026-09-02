# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda resolver for listDocuments and getDocumentCount GraphQL queries.

Uses the TypeDateIndex GSI on TrackingTable for efficient queries:
- listDocuments: Paginated query with date range filtering and RBAC-based filtering
- getDocumentCount: COUNT query for header display

RBAC (Role-Based Access Control):
- Admin/Author/Viewer: See all documents (scoped by allowedConfigVersions if set)
- Reviewer: See only HITL-pending documents + their own completed reviews

Performance: O(matched items) instead of O(total table items)
"""

import json
import logging
import os
import sys
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

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

# Limits
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# GSI name
TYPE_DATE_INDEX = "TypeDateIndex"

# TypeDateIndex hash-key values; must match idp_common.dynamodb.service. Documents
# submitted by Test Studio carry their own ItemType, so they never appear in the
# production Document List. Auto Optimizer submissions are NOT tagged today — they
# would need to stamp submission-source the way the test copier does.
ITEM_TYPE_DOCUMENT = "document"
ITEM_TYPE_TEST_DOCUMENT = "test-document"

# HITL statuses that indicate a completed/skipped review
COMPLETED_HITL_STATUSES = {"skipped", "reviewskipped", "completed", "reviewcompleted"}


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal objects from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def _get_caller_identity(event):
    """Extract caller's Cognito groups, username, and email from AppSync event identity."""
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    # Email can be in claims.email or identity.username (AppSync uses email as username)
    email = claims.get("email", "") or identity.get("username", "") or username

    # Groups may be a string if user is in one group
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
    """Look up user's allowedConfigVersions from UsersTable with caching.
    
    Returns None if unrestricted (no scope set or Admin), or a list of allowed version names.
    """
    users_table_name = os.environ.get("USERS_TABLE_NAME", "")
    if not users_table_name:
        return None
    
    # Check cache
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
            if scope and len(scope) > 0:
                result = list(scope)
            else:
                result = None
        else:
            result = None
    except Exception as e:
        logger.warning(f"Failed to look up user scope for {caller_email}: {e}")
        result = None
    
    # Update cache
    _user_scope_cache[caller_email] = {"scope": result, "timestamp": now}
    return result


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
        return get_document_count(event)
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
    
    # Build GSI query
    query_kwargs = {
        "IndexName": TYPE_DATE_INDEX,
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
    # Reviewer sees: HITL-pending documents (not completed/skipped) that are either
    # unassigned or assigned to them, PLUS completed documents owned by them
    if reviewer_only:
        reviewer_id = caller["username"]
        reviewer_email = caller["email"]
        # Build filter: HITL is active AND (
        #   (not completed AND (no owner OR owner is me))
        #   OR owner is me  (covers in-progress + completed reviews they own)
        # )
        # HITL is considered active when either:
        #   - HITLTriggered = true (explicit boolean flag), OR
        #   - HITLStatus indicates a pending/in-progress review (defense in depth
        #     for items where HITLTriggered was not written to DynamoDB)
        # Owner match checks HITLReviewOwner against both reviewer_id and
        # reviewer_email, because claim_review stores identity.username (often
        # the email) while this resolver reads cognito:username from claims.
        owner_is_me = (
            Attr("HITLReviewOwner").eq(reviewer_id) |
            Attr("HITLReviewOwner").eq(reviewer_email)
        )
        hitl_active = (
            Attr("HITLTriggered").eq(True) |
            Attr("HITLStatus").eq("PendingReview") |
            Attr("HITLStatus").eq("InProgress") |
            Attr("HITLStatus").eq("ReviewInProgress")
        )
        filter_expr = (
            hitl_active & (
                (
                    ~Attr("HITLCompleted").eq(True) &
                    (
                        Attr("HITLReviewOwner").not_exists() |
                        Attr("HITLReviewOwner").eq("") |
                        owner_is_me
                    )
                ) |
                owner_is_me
            )
        )
        query_kwargs["FilterExpression"] = filter_expr
        logger.info(f"Applied reviewer filter for user: {reviewer_id} / {reviewer_email}")


    # Handle pagination token
    if next_token:
        try:
            query_kwargs["ExclusiveStartKey"] = json.loads(next_token)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Invalid nextToken: {next_token}")
    
    logger.info(f"Querying TypeDateIndex: limit={limit}, start={start_dt}, end={end_dt}")
    
    try:
        response = table.query(**query_kwargs)
    except Exception as e:
        logger.error(f"GSI query failed: {e}")
        raise
    
    items = response.get("Items", [])
    last_key = response.get("LastEvaluatedKey")
    
    logger.info(f"Query returned {len(items)} items, has more: {last_key is not None}")
    
    # Get user's config-version scope for filtering
    allowed_versions = None
    if not caller.get("is_admin"):
        caller_email = caller.get("email", "")
        users_table = os.environ.get("USERS_TABLE_NAME", "")
        logger.info(f"Scope check: caller_email={caller_email}, username={caller['username']}, USERS_TABLE_NAME={'set:' + users_table if users_table else 'EMPTY'}")
        if users_table and caller_email:
            allowed_versions = _get_user_allowed_config_versions(caller_email)
            logger.info(f"Config-version scope result for {caller_email}: {allowed_versions or 'unrestricted (no scope set)'}")
        else:
            logger.warning(f"Cannot check scope: users_table={'set' if users_table else 'EMPTY'}, email='{caller_email}'")
    
    # Transform GSI projection items to match the Document GraphQL type
    # Apply config-version scope filtering (post-query filter since ConfigVersion is in GSI projection)
    documents = []
    for item in items:
        # Filter by config version scope if user has restrictions.
        # Fails CLOSED: a document with no ConfigVersion cannot be proven in
        # scope, so a scoped caller does not see it. It used to be admitted,
        # which leaked every document processed before config-version stamping
        # (and any document whose stamp failed) to every scoped user.
        if allowed_versions:
            doc_version = item.get("ConfigVersion") or item.get("ConfigurationVersion")
            if not scope_allows(allowed_versions, doc_version):
                logger.info(
                    f"Scope filter rejected PK={item.get('PK')}: "
                    f"ConfigVersion={doc_version!r} not in {allowed_versions}"
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


def get_document_count(event):
    """
    Get document count using TypeDateIndex GSI with SELECT COUNT.
    
    This is an efficient O(matched items) count that doesn't read item data.
    
    Args (from GraphQL):
        startDateTime: ISO 8601 start time
        endDateTime: ISO 8601 end time
    
    Returns:
        { count: Int }
    """
    args = event.get("arguments", {})
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")
    
    table_name = os.environ["TRACKING_TABLE_NAME"]
    table = dynamodb.Table(table_name)
    
    # Build GSI count query. Must use the same view as listDocuments, or the
    # header count would not match the rows on screen.
    item_type = _item_type_for_view(args.get("view"))
    query_kwargs = {
        "IndexName": TYPE_DATE_INDEX,
        "Select": "COUNT",
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
    
    logger.info(f"Counting documents: start={start_dt}, end={end_dt}")
    
    # Paginate through all count pages (DynamoDB may split count across pages)
    total_count = 0
    while True:
        response = table.query(**query_kwargs)
        total_count += response.get("Count", 0)
        
        last_key = response.get("LastEvaluatedKey")
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        else:
            break
    
    logger.info(f"Total document count: {total_count}")
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
        # NOTE: ProcessingIssueCount is written to the base table item but is NOT
        # part of this GSI's INCLUDE projection (DynamoDB does not allow adding
        # attributes to an existing GSI's projection in-place). It therefore
        # resolves to None on the fast list path and is filtered out below; the
        # per-document view (getDocument, full item) returns the real count. If a
        # future migration recreates the GSI with this attribute, this line will
        # start populating it automatically.
        "ProcessingIssueCount": item.get("ProcessingIssueCount"),
    }

    # Remove None values to keep response clean
    return {k: v for k, v in doc.items() if v is not None}
