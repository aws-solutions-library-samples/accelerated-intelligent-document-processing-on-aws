# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
API transport adapter for resolver Lambdas.

This module lets a single resolver Lambda serve BOTH:

1. AWS AppSync (legacy) — event shape: ``{"arguments": {...},
   "identity": {"claims": {...}, "username": ...}, "info": {"fieldName": ...}}``
2. API Gateway **HTTP API** (payload format 2.0) with a Cognito **JWT
   authorizer** — claims live at
   ``event["requestContext"]["authorizer"]["jwt"]["claims"]`` and the request
   body carries ``{"arguments": {...}}``.

The migration off AppSync (which is unavailable in GovCloud and not
FedRAMP-compliant) reuses the existing resolver Lambdas unchanged; this adapter
normalizes the incoming event into the AppSync shape the handlers already
expect, then wraps the handler's return value into an HTTP API proxy response.

CRITICAL — ``cognito:groups`` shape
-----------------------------------
AppSync delivers ``cognito:groups`` as a JSON list (or a bare string for a
single group). The HTTP API JWT authorizer instead **flattens** the groups
array into a single space-joined, bracket-wrapped string, e.g.
``"[Admin Author]"`` (or ``"[]"`` when empty, or ``"Admin"`` for one group
depending on serialization). Every resolver's RBAC depends on ``cognito:groups``
being a *list*. :func:`_coerce_groups` restores it. Getting this wrong either
locks every user out or fails open — it is the single most important detail in
the AppSync migration and is covered by unit tests.
"""

import base64
import json
import logging
import os
import re
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class _DecimalEncoder(json.JSONEncoder):
    """Encode DynamoDB ``Decimal`` values as int/float for JSON responses."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _coerce_groups(groups: Any) -> List[str]:
    """Normalize a ``cognito:groups`` claim into a list of group names.

    Handles every shape we have observed across authorizer types:
    - ``None`` / missing                          -> ``[]``
    - list (AppSync)                               -> unchanged (stringified)
    - ``"Admin"`` (single group)                   -> ``["Admin"]``
    - ``"Admin,Author"`` (REST API Cognito authorizer, comma-joined) -> ``["Admin", "Author"]``
    - ``"[Admin Author]"`` (HTTP API JWT authorizer flattened array) -> ``["Admin", "Author"]``
    - ``"[]"`` (empty bracketed)                   -> ``[]``
    - ``'["Admin","Author"]'`` (JSON-encoded list) -> ``["Admin", "Author"]``
    """
    if groups is None:
        return []
    if isinstance(groups, list):
        return [str(g) for g in groups]
    if isinstance(groups, str):
        s = groups.strip()
        if not s:
            return []
        # Bracket-wrapped form from the HTTP API JWT authorizer.
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            # Try JSON first (handles '["Admin","Author"]'), then fall back to
            # the authorizer's space-separated, unquoted form ('[Admin Author]').
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(g) for g in parsed]
            except (ValueError, TypeError):
                pass
            return [g.strip().strip('"') for g in inner.split() if g.strip()]
        # REST API Cognito User Pools authorizer joins groups with commas (and
        # may use newlines/spaces). Split on any of those.
        if any(sep in s for sep in (",", "\n", " ")):
            return [g.strip().strip('"') for g in re.split(r"[,\n ]+", s) if g.strip()]
        # Bare single group name.
        return [s]
    # Unknown type — best effort.
    return [str(groups)]


def _is_appsync_event(event: Dict[str, Any]) -> bool:
    """An AppSync resolver event always carries ``arguments`` and ``identity``."""
    return isinstance(event, dict) and "arguments" in event and "identity" in event


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the HTTP API request body into a dict, handling base64 encoding.

    A non-object JSON body (list/scalar) is wrapped so callers always get a dict.
    """
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:  # noqa: BLE001 - tolerate malformed input
            logger.warning("Failed to base64-decode request body")
            return {}
    if isinstance(body, dict):
        return body  # already parsed (e.g. tests)
    if isinstance(body, list):
        return {"arguments": body}
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, TypeError):
        logger.warning("Request body is not valid JSON")
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {"arguments": parsed}


def _field_from_event(event: Dict[str, Any]) -> str:
    """Resolve the GraphQL field name for an HTTP API event.

    Routes are ``POST /op/{field}``, and the REST API declares
    ``method.request.path.field: true``, so ``pathParameters`` is what this
    resolves from in practice. The raw-path fallback only covers event shapes that
    carry no path parameters (a payload-2.0 event, a hand-built local invoke).

    The field name is deliberately NOT read from the request body: the body is
    caller-controlled, and a second route into operation selection would be a
    second thing every authorization check has to agree about.
    """
    path_params = event.get("pathParameters") or {}
    if path_params.get("field"):
        return path_params["field"]
    rc = event.get("requestContext") or {}
    raw_path = (rc.get("http") or {}).get("path") or event.get("rawPath") or ""
    if raw_path:
        seg = raw_path.rstrip("/").rsplit("/", 1)[-1]
        if seg and seg != "op":
            return seg
    return ""


def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return an AppSync-shaped event regardless of the incoming transport.

    AppSync events pass through unchanged. HTTP API (payload v2.0 + JWT
    authorizer) events are converted to ``{"arguments", "identity", "info"}``
    with ``identity.claims['cognito:groups']`` restored to a list.
    """
    if _is_appsync_event(event):
        return event

    rc = event.get("requestContext") or {}
    authorizer = rc.get("authorizer") or {}
    jwt_claims = (authorizer.get("jwt") or {}).get("claims") or {}

    # Some configurations surface claims directly under authorizer (lambda
    # authorizer) — tolerate that too.
    if not jwt_claims and "claims" in authorizer:
        jwt_claims = authorizer.get("claims") or {}

    groups = _coerce_groups(jwt_claims.get("cognito:groups"))
    username = jwt_claims.get("cognito:username") or jwt_claims.get("sub") or ""
    email = jwt_claims.get("email") or username

    body = _parse_body(event)
    # The thin REST client posts {"arguments": {...}}; tolerate a bare body too.
    arguments = body.get("arguments") if isinstance(body, dict) else None
    if arguments is None:
        arguments = body if isinstance(body, dict) else {}

    field = _field_from_event(event)

    # Rebuild a claims dict with the normalized (list) groups so downstream RBAC
    # reads the same shape it always has under AppSync.
    normalized_claims = dict(jwt_claims)
    normalized_claims["cognito:groups"] = groups

    return {
        "arguments": arguments,
        "identity": {
            "claims": normalized_claims,
            "username": email,  # AppSync uses email as identity.username
            "sub": jwt_claims.get("sub", ""),
            "sourceIp": (rc.get("http") or {}).get("sourceIp"),
        },
        "info": {"fieldName": field},
        # Preserve the original event for handlers that need raw HTTP context.
        "_httpApiEvent": event,
    }


def _http_response(status: int, payload: Any) -> Dict[str, Any]:
    """Build an HTTP API (proxy) response with JSON body, CORS + security headers.

    CORS `Access-Control-Allow-Origin` defaults to `*`: the UI's fetch sends NO
    credentials/cookies (only a Bearer JWT in the Authorization header — see
    src/ui/src/api/rest-client.ts), so a wildcard is valid and cannot be combined
    with credentials, and it avoids a CloudFront->API-stack CloudFormation
    dependency cycle (CloudFront's origin IS this API). ZAP flags the `*` as
    "Cross-Domain Misconfiguration"; it is safe here for that reason.

    CORS_ALLOW_ORIGIN is a CODE-LEVEL HOOK, not a wired stack parameter — the
    dispatcher template does not set this env var, so a stock deployment always
    gets `*`. It exists for forks / manual overrides that pin the dispatcher
    Lambda's env. NOTE: even when set, this only changes the /op POST responses;
    the OPTIONS preflight + gateway-response CORS headers (CloudFormation-static)
    still emit `*`. A `*` preflight still admits a locked-down origin, so this is
    functionally fine, but it is NOT a full API-wide origin lockdown.

    Security headers (X-Content-Type-Options, Strict-Transport-Security,
    X-Frame-Options, Referrer-Policy) mirror the CloudFront ResponseHeadersPolicy
    that fronts the SPA — added here too so a client hitting the execute-api
    endpoint directly still gets them (ZAP scans that endpoint, not CloudFront).
    """
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("CORS_ALLOW_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            # Security headers (defense-in-depth for direct execute-api access;
            # CloudFront already sets these for the browser-facing SPA origin).
            "X-Content-Type-Options": "nosniff",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
        "body": json.dumps(payload, cls=_DecimalEncoder),
    }


def api_resolver(fn: Callable[[Dict[str, Any], Any], Any]) -> Callable:
    """Decorator that makes an AppSync resolver handler dual-transport.

    - Normalizes the event (AppSync passthrough / HTTP API conversion).
    - For HTTP API invocations, wraps the return value into an HTTP proxy
      response and maps exceptions to status codes:
        * ``PermissionError``           -> 403
        * ``ValueError`` / ``KeyError`` -> 400
        * anything else                 -> 500
      The error body matches the GraphQL shape the UI already parses:
      ``{"errors": [{"message": ..., "errorType": ...}]}``.
    - For AppSync invocations, returns the handler result unchanged and lets
      exceptions propagate (AppSync maps them to GraphQL errors).
    """

    @wraps(fn)
    def wrapper(event: Dict[str, Any], context: Any = None) -> Any:
        is_http = not _is_appsync_event(event)
        normalized = normalize_event(event)
        if not is_http:
            return fn(normalized, context)

        try:
            result = fn(normalized, context)
        except PermissionError as e:
            logger.warning("Authorization denied: %s", e)
            return _http_response(
                403, {"errors": [{"message": str(e), "errorType": "Unauthorized"}]}
            )
        except (ValueError, KeyError) as e:
            logger.warning("Bad request: %s", e)
            return _http_response(
                400, {"errors": [{"message": str(e), "errorType": "BadRequest"}]}
            )
        except Exception as e:  # noqa: BLE001 - surface as 500 to the client
            logger.error("Resolver error: %s", e, exc_info=True)
            return _http_response(
                500,
                {"errors": [{"message": str(e), "errorType": "InternalError"}]},
            )
        return _http_response(200, result)

    return wrapper
