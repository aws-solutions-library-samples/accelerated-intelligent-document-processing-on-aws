# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Circuit Breaker AppSync resolver.

Dispatches on event["info"]["fieldName"]:
- getCircuitBreakerStatus: reads ConcurrencyTable directly, returns status
  with enabled=false if the feature is disabled at deploy time.
- pauseCircuitBreaker / resumeCircuitBreaker / probeCircuitBreaker: admin-only
  (Cognito group "Admin"). Invokes circuit_breaker_manager Lambda synchronously
  with the corresponding action. Transition logic (ConditionExpressions, SNS
  publish, metrics, AppSync fan-out) lives in the manager — this resolver is
  just the GraphQL entrypoint.
- publishCircuitBreakerStatus: IAM-only pass-through used by the manager
  Lambda to trigger the onCircuitBreakerStatusChange subscription.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_dynamodb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")

CONCURRENCY_TABLE = os.environ["CONCURRENCY_TABLE"]
CIRCUIT_BREAKER_MANAGER_ARN = os.environ.get("CIRCUIT_BREAKER_MANAGER_ARN", "")
CIRCUIT_BREAKER_ENABLED = (
    os.environ.get("CIRCUIT_BREAKER_ENABLED", "false").lower() == "true"
)

_CIRCUIT_BREAKER_ID = "circuit_breaker"


def _get_caller(event: dict) -> dict:
    """Extract caller identity from AppSync event."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):
        groups = [groups]
    email = (
        claims.get("email")
        or identity.get("username")
        or claims.get("cognito:username")
        or claims.get("sub")
        or "unknown"
    )
    return {
        "email": email,
        "groups": groups,
        "is_admin": "Admin" in groups,
        "is_iam": "userArn" in identity and "accountId" in identity and not claims,
    }


def _read_state() -> dict:
    """Read circuit breaker state from ConcurrencyTable."""
    table = _dynamodb.Table(CONCURRENCY_TABLE)
    response = table.get_item(Key={"counter_id": _CIRCUIT_BREAKER_ID})
    return response.get("Item") or {}


def _format_status(item: dict) -> dict:
    """Shape the raw DDB item into the GraphQL CircuitBreakerStatus type."""
    if not CIRCUIT_BREAKER_ENABLED:
        return {
            "enabled": False,
            "state": None,
            "openedAt": None,
            "lastCheckedAt": None,
            "failureCount": 0,
            "recoveryAttempts": 0,
            "lastError": None,
        }
    return {
        "enabled": True,
        "state": item.get("state", "CLOSED"),
        "openedAt": item.get("opened_at"),
        "lastCheckedAt": item.get("last_checked_at"),
        "failureCount": int(item.get("failure_count", 0) or 0),
        "recoveryAttempts": int(item.get("recovery_attempts", 0) or 0),
        "lastError": item.get("last_error"),
    }


def _invoke_manager(action: str, caller: dict, reason: str) -> dict:
    """Invoke circuit_breaker_manager synchronously and return its state body."""
    if not CIRCUIT_BREAKER_MANAGER_ARN:
        raise RuntimeError("Circuit breaker manager not configured")
    payload = {"action": action, "reason": reason, "user": caller["email"]}
    response = _lambda.invoke(
        FunctionName=CIRCUIT_BREAKER_MANAGER_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    raw = response["Payload"].read()
    parsed = json.loads(raw) if raw else {}
    body = parsed.get("body") if isinstance(parsed, dict) else None
    if isinstance(body, dict):
        return _format_status(body)
    # Fallback: re-read from DDB if manager returned a string/legacy body.
    return _format_status(_read_state())


def handler(event, context):
    """AppSync resolver entrypoint."""
    info = event.get("info") or {}
    field = info.get("fieldName", "")
    logger.info(f"Circuit breaker resolver: field={field}")

    if field == "getCircuitBreakerStatus":
        if not CIRCUIT_BREAKER_ENABLED:
            return _format_status({})
        return _format_status(_read_state())

    if field == "publishCircuitBreakerStatus":
        # IAM-only: invoked by circuit_breaker_manager. AppSync subscriptions
        # fan out automatically once this mutation returns successfully.
        caller = _get_caller(event)
        if not caller["is_iam"]:
            raise Exception("Unauthorized: publishCircuitBreakerStatus is IAM-only")
        status_arg = (event.get("arguments") or {}).get("status")
        if isinstance(status_arg, str):
            try:
                return json.loads(status_arg)
            except json.JSONDecodeError:
                pass
        return _format_status(_read_state())

    # Admin mutations
    if field in ("pauseCircuitBreaker", "resumeCircuitBreaker", "probeCircuitBreaker"):
        caller = _get_caller(event)
        if not caller["is_admin"]:
            raise Exception("Unauthorized: Admin role required")
        reason = ((event.get("arguments") or {}).get("reason") or "").strip()
        if not reason:
            raise ValueError("A reason is required")
        action_map = {
            "pauseCircuitBreaker": "manual_open",
            "resumeCircuitBreaker": "manual_close",
            "probeCircuitBreaker": "manual_probe",
        }
        return _invoke_manager(action_map[field], caller, reason)

    raise Exception(f"Unknown field: {field}")
