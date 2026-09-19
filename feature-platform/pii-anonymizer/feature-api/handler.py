# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PII Anonymization feature API — backs the Redaction Report tab.

Route                              | Returns
---------------------------------- | -------------------------------------------
GET /config                        | Small bootstrap blob for the UI
GET /report                        | List of redaction audit rows (metadata only)
GET /report?window=7d              | Same, filtered to rows created in the window
GET /report/{docId}                | A single audit row

The audit rows are metadata ONLY — pii_count, mode, source/redacted keys,
companion version, timestamps. NO PII is ever stored or returned. The table is
OWNED by this feature and populated by the preprocessing hook (hook/handler.py).

The HTTP API Gateway (template.yaml) is fronted by a Cognito JWT authorizer
pointing at the main stack's User Pool, so we only handle application logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_AUDIT_TABLE = os.environ.get("AUDIT_TABLE_NAME", "")
_MAPPING_TABLE = os.environ.get("MAPPING_TABLE_NAME", "")
_HOOK_FUNCTION_ARN = os.environ.get("HOOK_FUNCTION_ARN", "")
_USERS_TABLE = os.environ.get("USERS_TABLE_NAME", "")
_WINDOW_RE = re.compile(r"^(\d+)([hdw])$")

# Built on first use, NOT at import. `boto3.resource(...)` at module scope
# resolves credentials and constructs a client while the module is still being
# imported, which is the wrong moment twice over. In Lambda it moves credential
# resolution into cold-start init, where a failure surfaces as an import error
# rather than as a handled invocation error. In tests it binds before any mock
# the test installs — pytest sets fixtures up *before* entering a
# `@mock_aws`-decorated test function — which made this suite depend on both test
# order and the developer's ambient AWS profile (see tests/test_handler.py::mod).
# Behaviour in Lambda is unchanged: the container is reused across invocations,
# so the resource is still created at most once per container, just on the first
# invocation instead of at init.
_dynamodb: Any = None


def _ddb() -> Any:
    """The DynamoDB resource for this container, created on first use."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


class ScopeLookupError(Exception):
    """UsersTable scope lookup failed — callers must fail CLOSED (deny)."""


def _caller_claims(event: Dict[str, Any]) -> Dict[str, Any]:
    """JWT claims the HTTP API's Cognito authorizer attached to the request."""
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    ) or {}


def _caller_email(event: Dict[str, Any]) -> str:
    """The caller's email, from the ``email`` claim and nothing else.

    The value is the key of a ``UsersTable`` ``EmailIndex`` query, and email is
    the only identifier that joins a Cognito principal to a row there — the row's
    key is a ``uuid4`` unrelated to the Cognito ``sub``, and no ``sub`` attribute
    is stored on the table at all.

    ⚠️ **There is deliberately no fallback to another claim.** A ``sub`` or a
    ``cognito:username`` is not an email address for every caller, so querying an
    email-keyed index with one matches no row — and an empty page is
    indistinguishable from "this user has no restriction". A fallback therefore
    converts an *unresolvable* caller into an *unrestricted* one, with no AWS
    fault required, on the route that reveals the PII re-identification mapping.
    Returning the empty string instead makes ``_caller_allowed_versions`` raise,
    and the request is denied.

    This mirrors ``caller_email_from_claims`` in the host's
    ``idp_common.config_scope``, which is the canonical statement of the rule.
    The logic is restated here rather than imported because this extension ships
    as its own stack with no ``idp_common`` layer; the shared static gate in
    ``scripts/tests/test_scope_lookup_fail_closed.py`` covers this file so the
    two cannot drift.
    """
    return str(_caller_claims(event).get("email") or "").strip()


def _caller_groups(event: Dict[str, Any]) -> list:
    raw = _caller_claims(event).get("cognito:groups") or []
    if isinstance(raw, str):
        # Cognito serializes the groups claim as a bracketed string over HTTP API.
        raw = [g for g in raw.strip("[]").replace(",", " ").split() if g]
    return list(raw)


def _caller_allowed_versions(email: str) -> Optional[list]:
    """The caller's allowedConfigVersions scope from the host UsersTable.

    Returns None = unrestricted (no scope set, matching the host's own rule).
    Raises ScopeLookupError on a lookup failure so callers gating a sensitive
    resource (the PII mapping) FAIL CLOSED rather than treating a transient
    DynamoDB error as 'unrestricted'."""
    if not email:
        raise ScopeLookupError("no caller email in JWT claims")
    if not _USERS_TABLE:
        # No UsersTable wired — cannot evaluate scope; deny for the mapping.
        raise ScopeLookupError("USERS_TABLE_NAME not configured")
    try:
        from boto3.dynamodb.conditions import Key as _Key

        table = _ddb().Table(_USERS_TABLE)
        resp = table.query(
            IndexName="EmailIndex", KeyConditionExpression=_Key("email").eq(email)
        )
        items = resp.get("Items", [])
        if items:
            scope = items[0].get("allowedConfigVersions")
            return list(scope) if scope else None
        return None  # user has no explicit scope row → unrestricted
    except Exception as exc:  # noqa: BLE001
        logger.warning("User scope lookup failed for %s: %s", email, exc)
        raise ScopeLookupError(str(exc)) from exc


def _parse_window(raw: Optional[str]) -> Optional[timedelta]:
    if not raw:
        return None
    m = _WINDOW_RE.match(raw)
    if not m:
        raise ValueError(f"Unsupported window {raw!r}; examples: 24h, 7d, 4w")
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[
        unit
    ]


def _response(status: int, body: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": body if isinstance(body, str) else json.dumps(body, default=str),
    }


def _to_plain(value: Any) -> Any:
    """Convert DynamoDB Decimals to int/float for JSON serialization."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _list_report(since: Optional[datetime]) -> List[Dict[str, Any]]:
    if not _AUDIT_TABLE:
        raise RuntimeError("AUDIT_TABLE_NAME env var is not set")
    table = _ddb().Table(_AUDIT_TABLE)
    since_iso = since.isoformat().replace("+00:00", "Z") if since is not None else None

    items: List[Dict[str, Any]] = []
    # ByCreatedAt GSI: hash=gsiPk("ALL"), range=createdAt — a single partition
    # ordered by time, so we can range-filter and return newest-first cheaply.
    key_cond = Key("gsiPk").eq("ALL")
    if since_iso:
        key_cond = key_cond & Key("createdAt").gte(since_iso)
    kwargs: Dict[str, Any] = {
        "IndexName": "ByCreatedAt",
        "KeyConditionExpression": key_cond,
        "ScanIndexForward": False,  # newest first
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [_to_plain(i) for i in items]


def _get_row(doc_id: str) -> Optional[Dict[str, Any]]:
    table = _ddb().Table(_AUDIT_TABLE)
    item = table.get_item(Key={"documentId": doc_id}).get("Item")
    return _to_plain(item) if item else None


def _read_mapping(doc_id: str) -> Optional[Dict[str, Any]]:
    """Read the stored mapping (contains real PII) from the FEATURE-OWNED
    mapping DynamoDB table — never a host-proxyable bucket."""
    if not _MAPPING_TABLE:
        return None
    try:
        item = (
            _ddb()
            .Table(_MAPPING_TABLE)
            .get_item(Key={"documentId": doc_id})
            .get("Item")
        )
        return _to_plain(item) if item else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read mapping for %s: %s", doc_id, exc)
        return None


def _visible_to(row: Dict[str, Any], is_admin: bool, allowed: Optional[list]) -> bool:
    """Config-version RBAC for a report row: Admins and unrestricted users see
    all; a scoped user sees a row only if the ORIGINAL's config version is in
    their allowedConfigVersions."""
    if is_admin or allowed is None:
        return True
    return (row.get("originalConfigVersion") or "") in allowed


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}
    logger.info(
        "pii-anonymizer API %s %s",
        event.get("requestContext", {}).get("http", {}).get("method"),
        path,
    )

    if path.rstrip("/") == "/config":
        return _response(
            200,
            {
                "feature": "pii-anonymizer",
                "hookFunctionArn": _HOOK_FUNCTION_ARN or None,
            },
        )

    is_admin = "Admin" in _caller_groups(event)

    if path.rstrip("/") == "/report":
        try:
            window = _parse_window(qs.get("window"))
        except ValueError as exc:
            return _response(400, {"error": str(exc)})
        since = datetime.now(timezone.utc) - window if window else None
        # RBAC: scope the list to config versions the caller may see. A scope
        # lookup failure denies — the caller is shown an empty report rather than
        # every row. The denial is *returned here*, not encoded as an empty
        # `allowed` list for `_visible_to` to interpret: an empty list means
        # "nothing matches" only as long as that function keeps reading `None`
        # (and not any falsy value) as unrestricted, which is one refactor away
        # from turning this denial into the opposite.
        try:
            allowed = _caller_allowed_versions(_caller_email(event))
        except ScopeLookupError:
            logger.warning("Scope lookup failed for report list — returning empty")
            return _response(
                200,
                {
                    "rows": [],
                    "total": 0,
                    "totalPiiRedacted": 0,
                    "window": qs.get("window") or "all",
                    "asOf": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        try:
            rows = [r for r in _list_report(since) if _visible_to(r, is_admin, allowed)]
        except Exception as exc:  # noqa: BLE001
            logger.exception("list report failed")
            return _response(500, {"error": str(exc)})
        total_pii = sum(int(r.get("piiCount") or 0) for r in rows)
        return _response(
            200,
            {
                "rows": rows,
                "total": len(rows),
                "totalPiiRedacted": total_pii,
                "window": qs.get("window") or "all",
                "asOf": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )

    # GET /report/{docId}/mapping — RBAC-GATED. Returns the original->synthetic
    # PII mapping (a re-identification key) ONLY to a caller whose
    # allowedConfigVersions include the ORIGINAL document's config version.
    mm = re.match(r"^/report/(.+)/mapping$", path)
    if mm:
        doc_id = unquote(mm.group(1))
        try:
            row = _get_row(doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get report row failed")
            return _response(500, {"error": str(exc)})
        if not row:
            return _response(404, {"error": f"no redaction record for {doc_id!r}"})
        if not row.get("mappingStored"):
            return _response(404, {"error": "no stored mapping for this document"})

        # RBAC: caller must be allowed the ORIGINAL's config version. FAIL CLOSED
        # on a scope-lookup error (this is a re-identification key).
        try:
            allowed = _caller_allowed_versions(_caller_email(event))
        except ScopeLookupError:
            return _response(403, {"error": "Access denied: could not verify scope."})
        if not _visible_to(row, is_admin, allowed):
            return _response(
                403,
                {
                    "error": "Access denied: you do not have access to the "
                    "config version that processed the original document."
                },
            )

        mapping_doc = _read_mapping(doc_id)
        if mapping_doc is None:
            return _response(404, {"error": "stored mapping not found"})
        return _response(200, mapping_doc)

    m = re.match(r"^/report/(.+)$", path)
    if m:
        doc_id = unquote(m.group(1))
        try:
            row = _get_row(doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get report row failed")
            return _response(500, {"error": str(exc)})
        if not row:
            return _response(404, {"error": f"no redaction record for {doc_id!r}"})
        # RBAC: a scoped user may only see a row for a config version they're
        # allowed (fail closed on lookup error → 403).
        try:
            allowed = _caller_allowed_versions(_caller_email(event))
        except ScopeLookupError:
            return _response(403, {"error": "Access denied: could not verify scope."})
        if not _visible_to(row, is_admin, allowed):
            return _response(403, {"error": "Access denied."})
        return _response(200, row)

    return _response(404, {"error": f"unknown path {path}"})
