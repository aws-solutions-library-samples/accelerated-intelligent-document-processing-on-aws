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
from fnmatch import fnmatchcase
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

# The two key spaces a caller's scope row is found on, on the HOST's UsersTable,
# and the claim each is read from. Restated from ``idp_common.config_scope`` — this
# extension ships as its own stack with no ``idp_common`` layer — and held to that
# module by ``scripts/tests/test_scope_lookup_fail_closed.py``.
USERS_TABLE_SCOPE_INDEX = "EmailIndex"
USERS_TABLE_SCOPE_KEY = "email"
USERS_TABLE_SUB_POINTER_PREFIX = "SUB#"
USERS_TABLE_USER_KEY_PREFIX = "USER#"
SCOPE_KEY_CLAIM = "email"
SCOPE_SUB_CLAIM = "sub"

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

    The value is the hash key of a ``UsersTable`` ``EmailIndex`` query, so it has
    to be an email address or it matches nothing.

    ⚠️ **There is deliberately no fallback to another claim.** A
    ``cognito:username`` is not an email address for every caller, so querying an
    email-keyed index with one matches no row — and an empty page is
    indistinguishable from "this user has no restriction". A fallback therefore
    converts an *unresolvable* caller into an *unrestricted* one, with no AWS
    fault required, on the route that reveals the PII re-identification mapping.

    The Cognito ``sub`` is **not** a fallback for this value either. It is a key
    into a different key space on the same table and is read separately by
    :func:`_caller_sub`; putting one where the other belongs is the substitution
    this docstring is about, not a second route.

    This mirrors ``caller_email_from_claims`` in the host's
    ``idp_common.config_scope``, which is the canonical statement of the rule.
    The logic is restated here rather than imported because this extension ships
    as its own stack with no ``idp_common`` layer; the shared static gate in
    ``scripts/tests/test_scope_lookup_fail_closed.py`` covers this file so the
    two cannot drift.
    """
    return str(_caller_claims(event).get(SCOPE_KEY_CLAIM) or "").strip()


def _caller_sub(event: Dict[str, Any]) -> str:
    """The caller's immutable Cognito ``sub``, from the ``sub`` claim and no other.

    Used only to build a ``SUB#<sub>`` pointer key on the host UsersTable, never
    as the hash key of ``EmailIndex``. A request body is not a source for it.
    """
    return str(_caller_claims(event).get(SCOPE_SUB_CLAIM) or "").strip()


def _sub_pointer_key(caller_sub: str) -> Dict[str, str]:
    """The UsersTable key of the pointer item for one Cognito ``sub``."""
    key = f"{USERS_TABLE_SUB_POINTER_PREFIX}{caller_sub}"
    return {"PK": key, "SK": key}


def _user_row_key(user_id: str) -> Dict[str, str]:
    """The UsersTable key of the row holding one user's scope."""
    key = f"{USERS_TABLE_USER_KEY_PREFIX}{user_id}"
    return {"PK": key, "SK": key}


def _caller_groups(event: Dict[str, Any]) -> list:
    raw = _caller_claims(event).get("cognito:groups") or []
    if isinstance(raw, str):
        # Cognito serializes the groups claim as a bracketed string over HTTP API.
        raw = [g for g in raw.strip("[]").replace(",", " ").split() if g]
    return list(raw)


def _caller_allowed_versions(email: str, caller_sub: str = "") -> Optional[list]:
    """The caller's allowedConfigVersions scope from the host UsersTable.

    The row is looked for on the immutable Cognito ``sub`` first, via a
    ``SUB#<sub>`` pointer item carrying the ``userId``, and on the ``email`` claim
    second, via ``EmailIndex``. The two are not a fallback chain over one key: each
    identifier goes only to the key space that indexes it. Preferring the ``sub``
    is what keeps the restriction working for a caller whose address has diverged
    from their row, which on this route would otherwise hand out a
    re-identification key.

    Returns None = unrestricted (no scope set, matching the host's own rule).
    Raises ScopeLookupError on a lookup failure so callers gating a sensitive
    resource (the PII mapping) FAIL CLOSED rather than treating a transient
    DynamoDB error as 'unrestricted'."""
    caller_sub = str(caller_sub or "").strip()
    if not email and not caller_sub:
        raise ScopeLookupError("no caller email or sub in JWT claims")
    if not _USERS_TABLE:
        # No UsersTable wired — cannot evaluate scope; deny for the mapping.
        raise ScopeLookupError("USERS_TABLE_NAME not configured")
    try:
        from boto3.dynamodb.conditions import Key as _Key

        table = _ddb().Table(_USERS_TABLE)
        row = None
        if caller_sub:
            pointer = table.get_item(Key=_sub_pointer_key(caller_sub)).get("Item")
            user_id = str((pointer or {}).get("userId") or "").strip()
            if user_id:
                row = table.get_item(Key=_user_row_key(user_id)).get("Item") or None
        if row is None and email:
            resp = table.query(
                IndexName=USERS_TABLE_SCOPE_INDEX,
                KeyConditionExpression=_Key(USERS_TABLE_SCOPE_KEY).eq(email),
            )
            items = resp.get("Items", [])
            row = items[0] if items else None
    except Exception as exc:  # noqa: BLE001
        # No caller email in the message: it lands in a log group and is re-raised
        # to a route that may surface it. Mirrors the canonical module, which omits
        # it for the same reason.
        logger.warning("User scope lookup failed on the host UsersTable: %s", exc)
        raise ScopeLookupError(str(exc)) from exc
    if row is None and not email:
        # A sub-only caller whose sub no row records. Unlike an empty email page
        # this is not an answer about the caller — their row may simply predate the
        # pointer writer — and there is no second key to try, so it denies.
        raise ScopeLookupError("no caller email, and no row records this caller's sub")
    if row is None:
        return None  # user has no explicit scope row → unrestricted
    return _normalize_scope(row.get("allowedConfigVersions"))


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


def _normalize_scope(raw: Any) -> Optional[list]:
    """Coerce a raw ``allowedConfigVersions`` attribute into a scope list.

    Returns None for "unrestricted" — absent, empty, or nothing usable left after
    blank entries are dropped, because a stray empty string must not become a rule
    that matches nothing. Mirrors ``normalize_scope`` in
    ``idp_common.config_scope``; see ``_scope_allows`` for why it is restated here.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        logger.warning(
            "Ignoring unusable allowedConfigVersions of type %s", type(raw).__name__
        )
        return None
    entries = [str(entry).strip() for entry in raw if str(entry).strip()]
    return entries or None


def _scope_allows(allowed: Optional[list], profile_name: Optional[str]) -> bool:
    """Whether a scope permits a configuration profile name.

    Entries may be exact names (``lending``) or **glob patterns**
    (``lending-*``, ``uc?-prod``): patterns are first-class in this scope axis
    because deployments predating revision history encode lineage in the profile
    *name*. A plain ``in`` test would deny a pattern-scoped user every row they are
    entitled to — fail-closed, so not a leak, but wrong.

    An empty or unset scope is unrestricted; a set scope **denies an unnamed
    target**, because an object with no profile name cannot be proven in scope.

    ⚠️ This restates ``scope_allows`` from ``idp_common.config_scope``, which is
    canonical. It is not imported because this extension ships as its own stack
    with no ``idp_common`` layer — the same reason ``_caller_email`` restates
    ``caller_email_from_claims``. Keep the two in step; a scope matcher that
    differs between call sites is the same class of bug as a lookup that does.
    """
    entries = _normalize_scope(allowed)
    if not entries:
        return True
    if not profile_name:
        return False
    name = str(profile_name)
    return any(
        entry == name
        or (any(c in entry for c in ("*", "?", "[")) and fnmatchcase(name, entry))
        for entry in entries
    )


def _visible_to(row: Dict[str, Any], is_admin: bool, allowed: Optional[list]) -> bool:
    """Config-version RBAC for a report row: Admins and unrestricted users see
    all; a scoped user sees a row only if the ORIGINAL's config version is in
    their allowedConfigVersions."""
    if is_admin:
        return True
    return _scope_allows(allowed, row.get("originalConfigVersion"))


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
        # lookup failure denies with the same 403 the two /report/{docId} routes
        # below return for the identical condition.
        #
        # Two shapes this must NOT take. Encoding the denial as an empty `allowed`
        # list for `_visible_to` to interpret works only while that function reads
        # `None` — and not any falsy value — as unrestricted, which is one
        # refactor away from inverting it. And answering 200 with an empty row set
        # is worse than either: no rows are served, so it is still fail-closed,
        # but this is an *audit* view, and an empty report is the truthful answer
        # when nothing was redacted. A missing IAM grant, a throttle or a caller
        # with no email claim would all present to a reviewer as "the anonymizer
        # redacted nothing" — the UI renders `rows: []` as a legitimate zero and
        # only raises its error banner on a non-2xx.
        try:
            allowed = _caller_allowed_versions(_caller_email(event), _caller_sub(event))
        except ScopeLookupError:
            logger.warning("Scope lookup failed for the report list — denying")
            return _response(403, {"error": "Access denied: could not verify scope."})
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
        # RBAC first, before the record is read. FAIL CLOSED on a scope-lookup
        # error (this route serves a re-identification key).
        #
        # ⚠️ The ORDER is part of the control, not style. Reading the record first
        # answers 404 for an absent one and 403 for a present one, to a caller whose
        # scope could not be resolved — one bit of the audit table's contents per
        # request, to a caller entitled to none of it. Resolving the scope first
        # means every such caller gets the same 403 whatever the id names.
        try:
            allowed = _caller_allowed_versions(_caller_email(event), _caller_sub(event))
        except ScopeLookupError:
            return _response(403, {"error": "Access denied: could not verify scope."})
        try:
            row = _get_row(doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get report row failed")
            return _response(500, {"error": str(exc)})
        if not row:
            return _response(404, {"error": f"no redaction record for {doc_id!r}"})
        if not row.get("mappingStored"):
            return _response(404, {"error": "no stored mapping for this document"})

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
        # RBAC first, before the record is read: a scoped user may only see a row
        # for a config version they are allowed (fail closed on a lookup error →
        # 403). Reading the record first would distinguish an absent id (404) from a
        # present one (403) for a caller whose scope cannot be resolved — an
        # existence oracle over the audit table, one bit per request. Same ordering
        # rule as the /mapping route above.
        try:
            allowed = _caller_allowed_versions(_caller_email(event), _caller_sub(event))
        except ScopeLookupError:
            return _response(403, {"error": "Access denied: could not verify scope."})
        try:
            row = _get_row(doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get report row failed")
            return _response(500, {"error": str(exc)})
        if not row:
            return _response(404, {"error": f"no redaction record for {doc_id!r}"})
        if not _visible_to(row, is_admin, allowed):
            return _response(403, {"error": "Access denied."})
        return _response(200, row)

    return _response(404, {"error": f"unknown path {path}"})
