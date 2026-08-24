# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB-direct operations for the HTTP API dispatcher.

Under AppSync these fields were served by **VTL DynamoDB resolvers** (no Lambda).
The HTTP API has no VTL, so this module reimplements that exact behavior in
Python against the same tables. It is intentionally a faithful port of the VTL
in ``nested/api-resolvers/template.yaml`` (DiscoveryTableDataSource and
AgentTableDataSource resolvers).

Tables are passed via environment variables set by CloudFormation:
- ``TRACKING_TABLE_NAME``
- ``DISCOVERY_TABLE_NAME``
- ``AGENT_TABLE_NAME``

Agent-job rows are user-scoped with ``PK = "agent#<userId>"`` / ``SK = jobId``
where ``userId`` is the caller's email (AppSync ``identity.username``) falling
back to ``sub`` then ``"anonymous"`` — matching the VTL exactly so a user only
sees their own jobs.
"""

import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_dynamodb = boto3.resource("dynamodb")

_VALID_DISCOVERY_STATUSES = {
    "PENDING",
    "IN_PROGRESS",
    "COMPLETED",
    "FAILED",
    "OPTIMIZATION_IN_PROGRESS",
    "OPTIMIZATION_COMPLETED",
    "OPTIMIZATION_FAILED",
    "QUEUED",
    "PREPARING",
    "EMBEDDING",
    "CLUSTERING",
    "ANALYZING",
}
_DISCOVERY_TERMINAL = {
    "COMPLETED",
    "FAILED",
    "OPTIMIZATION_COMPLETED",
    "OPTIMIZATION_FAILED",
}
_AGENT_TERMINAL = {"COMPLETED", "FAILED"}

# Optional discovery-status update fields -> attribute name (VTL parity).
_DISCOVERY_OPTIONAL_FIELDS = [
    "errorMessage",
    "discoveredClassName",
    "statusMessage",
    "jobType",
    "currentStep",
    "totalDocuments",
    "clustersFound",
    "discoveredClasses",
    "reflectionReport",
]

_SHARDS_IN_DAY = 6
_SHARD_DIVIDER = 24 // _SHARDS_IN_DAY

_HANDLED = {
    # TrackingTable (DynamoDB-direct under AppSync)
    "getDocument",
    "listDocumentsDateHour",
    "listDocumentsDateShard",
    # DiscoveryTable
    "listDiscoveryJobs",
    "updateDiscoveryJobStatus",
    "deleteDiscoveryJob",
    # AgentTable
    "getAgentJobStatus",
    "listAgentJobs",
    "updateAgentJobStatus",
    "deleteAgentJob",
    # Circuit breaker — only handled here as the disabled-state fallback. When
    # the circuit breaker is ENABLED the field is routed to the dedicated
    # CircuitBreakerResolverFunction via FIELD_FUNCTION_MAP and never reaches
    # this module; when DISABLED that resolver Lambda doesn't exist (its ARN is
    # empty), so the dispatcher falls through to here for a graceful response.
    "getCircuitBreakerStatus",
}


def handles(field: str) -> bool:
    return field in _HANDLED


# Per-op RBAC, preserving the AppSync schema's authorization exactly. Under
# AppSync the @aws_cognito_user_pools(cognito_groups) directive gated these
# fields BEFORE the VTL resolver ran; the REST dispatcher's Cognito authorizer
# only authenticates, so the group check must be re-applied here (defense in
# depth, matching the Lambda-backed resolvers).
#   - value = set of allowed groups (caller must be in at least one)
#   - value = None            -> any authenticated user (schema type-default)
#   - value = _IAM_ONLY       -> backend/IAM principals only; NEVER a Cognito
#                                user. These are unreachable from the UI and the
#                                backend workers now write DynamoDB directly
#                                (APPSYNC_API_URL is empty), so reject outright.
_IAM_ONLY = object()
_REQUIRED_GROUPS: Dict[str, Any] = {
    "getDocument": None,
    "listDocumentsDateHour": None,
    "listDocumentsDateShard": None,
    "listDiscoveryJobs": {"Admin", "Author"},
    "deleteDiscoveryJob": {"Admin", "Author"},
    "updateDiscoveryJobStatus": _IAM_ONLY,
    "getAgentJobStatus": {"Admin", "Author", "Viewer"},
    "listAgentJobs": {"Admin", "Author", "Viewer"},
    "updateAgentJobStatus": _IAM_ONLY,
    "deleteAgentJob": None,  # any authed; further scoped to caller's own PK
    "getCircuitBreakerStatus": None,
}


def _caller_groups(event: Dict[str, Any]) -> list:
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return list(groups)


def _enforce_rbac(field: str, event: Dict[str, Any]) -> None:
    """Raise PermissionError if the caller isn't allowed to run `field`.
    Mirrors the AppSync schema directives (dispatcher maps PermissionError->403)."""
    required = _REQUIRED_GROUPS.get(field)
    if required is None:
        return  # any authenticated user
    if required is _IAM_ONLY:
        logger.warning("Rejected IAM-only op %s from Cognito caller", field)
        raise PermissionError(f"Unauthorized: {field} is not callable via the API")
    groups = _caller_groups(event)
    if not (set(required).intersection(groups)):
        logger.warning(
            "Forbidden: caller (groups=%s) attempted %s (requires %s)",
            groups,
            field,
            sorted(required),
        )
        raise PermissionError(
            f"Unauthorized: {field} requires one of {sorted(required)}"
        )


def _now_iso() -> str:
    # ISO-8601 in UTC with millisecond precision + 'Z', matching VTL nowISO8601.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def _to_native(obj: Any) -> Any:
    """Convert DynamoDB Decimals to int/float for JSON-friendly returns."""
    if isinstance(obj, list):
        return [_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def _caller_user_id(event: Dict[str, Any]) -> str:
    """Replicate VTL: username -> sub -> 'anonymous'."""
    identity = event.get("identity") or {}
    return identity.get("username") or identity.get("sub") or "anonymous"


def _tracking_table():
    return _dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])


def _discovery_table():
    return _dynamodb.Table(os.environ["DISCOVERY_TABLE_NAME"])


def _agent_table():
    return _dynamodb.Table(os.environ["AGENT_TABLE_NAME"])


# --------------------------------------------------------------------------- #
# Documents (TrackingTableDataSource — DynamoDB-direct under AppSync)
# --------------------------------------------------------------------------- #
def _get_document(event: Dict[str, Any]) -> Any:
    """GetItem PK="doc#<ObjectKey>", SK="none" — returns the raw item (VTL parity)."""
    object_key = event["arguments"]["ObjectKey"]
    resp = _tracking_table().get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
    return _to_native(resp.get("Item"))


def _list_documents_date_hour(event: Dict[str, Any]) -> Dict[str, Any]:
    args = event.get("arguments", {})
    now = time.gmtime()
    date = args.get("date") or time.strftime("%Y-%m-%d", now)
    hour = args.get("hour")
    if hour is None:
        hour = now.tm_hour
    hour = int(hour)
    if hour < 0 or hour > 23:
        raise ValueError("Invalid hour parameter - value should be between 0 and 23")
    shard_pad = f"{hour // _SHARD_DIVIDER:02d}"
    hour_pad = f"{hour:02d}"
    resp = _tracking_table().query(
        KeyConditionExpression=Key("PK").eq(f"list#{date}#s#{shard_pad}")
        & Key("SK").begins_with(f"ts#{date}T{hour_pad}"),
    )
    return {
        "Documents": _to_native(resp.get("Items", [])),
        "nextToken": resp.get("LastEvaluatedKey")
        and str(resp["LastEvaluatedKey"])
        or None,
    }


def _list_documents_date_shard(event: Dict[str, Any]) -> Dict[str, Any]:
    args = event.get("arguments", {})
    now = time.gmtime()
    date = args.get("date") or time.strftime("%Y-%m-%d", now)
    shard = args.get("shard")
    if shard is None:
        shard = now.tm_hour // _SHARD_DIVIDER
    shard = int(shard)
    if shard >= _SHARDS_IN_DAY or shard < 0:
        raise ValueError(
            f"Invalid shard parameter value - must be positive and less than {_SHARDS_IN_DAY}"
        )
    shard_pad = f"{shard:02d}"
    resp = _tracking_table().query(
        KeyConditionExpression=Key("PK").eq(f"list#{date}#s#{shard_pad}"),
    )
    return {
        "Documents": _to_native(resp.get("Items", [])),
        "nextToken": resp.get("LastEvaluatedKey")
        and str(resp["LastEvaluatedKey"])
        or None,
    }


# --------------------------------------------------------------------------- #
# Discovery jobs (DiscoveryTableDataSource)
# --------------------------------------------------------------------------- #
def _list_discovery_jobs(_event: Dict[str, Any]) -> Dict[str, Any]:
    resp = _discovery_table().scan(Limit=50)
    return {
        "DiscoveryJobs": _to_native(resp.get("Items", [])),
        "nextToken": resp.get("LastEvaluatedKey")
        and str(resp["LastEvaluatedKey"])
        or None,
    }


def _update_discovery_job_status(event: Dict[str, Any]) -> Any:
    args = event.get("arguments", {})
    status = args.get("status")
    if status not in _VALID_DISCOVERY_STATUSES:
        raise ValueError("Invalid status value")

    names = {"#status": "status", "#updatedAt": "updatedAt"}
    values = {":status": status, ":updatedAt": _now_iso()}
    expr = "SET #status = :status, #updatedAt = :updatedAt"

    for fld in _DISCOVERY_OPTIONAL_FIELDS:
        val = args.get(fld)
        if val is not None and val != "":
            names[f"#{fld}"] = fld
            values[f":{fld}"] = val
            expr += f", #{fld} = :{fld}"

    if status in _DISCOVERY_TERMINAL:
        names["#completedAt"] = "completedAt"
        values[":completedAt"] = _now_iso()
        expr += ", #completedAt = :completedAt"

    resp = _discovery_table().update_item(
        Key={"jobId": args["jobId"]},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return _to_native(resp.get("Attributes"))


def _delete_discovery_job(event: Dict[str, Any]) -> bool:
    _discovery_table().delete_item(Key={"jobId": event["arguments"]["jobId"]})
    return True


# --------------------------------------------------------------------------- #
# Agent jobs (AgentTableDataSource) — user-scoped PK = "agent#<userId>"
# --------------------------------------------------------------------------- #
def _agent_pk(user_id: str) -> str:
    return f"agent#{user_id}"


def _shape_agent_job(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jobId": item.get("SK"),
        "status": item.get("status"),
        "query": item.get("query"),
        "agentIds": item.get("agentIds"),
        "createdAt": item.get("createdAt"),
        "completedAt": item.get("completedAt"),
        "result": item.get("result"),
        "error": item.get("error"),
        "agent_messages": item.get("agent_messages"),
    }


def _get_agent_job_status(event: Dict[str, Any]) -> Any:
    user_id = _caller_user_id(event)
    resp = _agent_table().get_item(
        Key={"PK": _agent_pk(user_id), "SK": event["arguments"]["jobId"]}
    )
    item = resp.get("Item")
    if not item:
        return None
    return _to_native(_shape_agent_job(item))


def _list_agent_jobs(event: Dict[str, Any]) -> Dict[str, Any]:
    user_id = _caller_user_id(event)
    args = event.get("arguments", {})
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_agent_pk(user_id)),
        "ScanIndexForward": False,
    }
    if args.get("limit"):
        kwargs["Limit"] = int(args["limit"])
    if args.get("nextToken"):
        kwargs["ExclusiveStartKey"] = args["nextToken"]
    resp = _agent_table().query(**kwargs)
    items = [_shape_agent_job(i) for i in resp.get("Items", [])]
    # listAgentJobs response shape excludes agent_messages in VTL; drop it.
    for it in items:
        it.pop("agent_messages", None)
    return {
        "items": _to_native(items),
        "nextToken": resp.get("LastEvaluatedKey")
        and str(resp["LastEvaluatedKey"])
        or None,
    }


def _update_agent_job_status(event: Dict[str, Any]) -> bool:
    args = event.get("arguments", {})
    user_id = args.get("userId")  # VTL uses the explicit userId arg here
    status = args.get("status")

    names = {"#status": "status"}
    values = {":status": status}
    expr = "SET #status = :status"

    if args.get("result"):
        names["#result"] = "result"
        values[":result"] = args["result"]
        expr += ", #result = :result"
    if args.get("error"):
        names["#error"] = "error"
        values[":error"] = args["error"]
        expr += ", #error = :error"
    if status in _AGENT_TERMINAL:
        names["#completedAt"] = "completedAt"
        values[":completedAt"] = _now_iso()
        expr += ", #completedAt = :completedAt"

    resp = _agent_table().update_item(
        Key={"PK": _agent_pk(user_id), "SK": args["jobId"]},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return bool(resp.get("Attributes"))


def _delete_agent_job(event: Dict[str, Any]) -> bool:
    user_id = _caller_user_id(event)
    _agent_table().delete_item(
        Key={"PK": _agent_pk(user_id), "SK": event["arguments"]["jobId"]}
    )
    return True


# --------------------------------------------------------------------------- #
# Circuit breaker (disabled-state fallback only)
# --------------------------------------------------------------------------- #
def _get_circuit_breaker_status(_event: Dict[str, Any]) -> Dict[str, Any]:
    """Disabled-state CircuitBreakerStatus. Mirrors circuit_breaker_resolver's
    _format_status({}) when CIRCUIT_BREAKER_ENABLED is false. Only invoked when
    the circuit breaker feature is off (see _HANDLED note); the UI's
    CircuitBreakerBadge hides itself on enabled=false."""
    return {
        "enabled": False,
        "state": None,
        "openedAt": None,
        "lastCheckedAt": None,
        "failureCount": 0,
        "recoveryAttempts": 0,
        "lastError": None,
    }


_DISPATCH = {
    "getDocument": _get_document,
    "getCircuitBreakerStatus": _get_circuit_breaker_status,
    "listDocumentsDateHour": _list_documents_date_hour,
    "listDocumentsDateShard": _list_documents_date_shard,
    "listDiscoveryJobs": _list_discovery_jobs,
    "updateDiscoveryJobStatus": _update_discovery_job_status,
    "deleteDiscoveryJob": _delete_discovery_job,
    "getAgentJobStatus": _get_agent_job_status,
    "listAgentJobs": _list_agent_jobs,
    "updateAgentJobStatus": _update_agent_job_status,
    "deleteAgentJob": _delete_agent_job,
}


def dispatch(field: str, event: Dict[str, Any]) -> Any:
    _enforce_rbac(field, event)
    return _DISPATCH[field](event)
