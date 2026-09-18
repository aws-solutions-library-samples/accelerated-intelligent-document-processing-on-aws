# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
API transport adapter for resolver Lambdas.

This module normalizes two event shapes into the one the resolver handlers expect:

1. The legacy AWS AppSync resolver shape — ``{"arguments": {...},
   "identity": {"claims": {...}, "username": ...}, "info": {"fieldName": ...}}``.
   AppSync was **removed** in release 0.6.0, so nothing produces this shape from a
   transport that authenticated the caller; what is left of it is covered in the
   second CRITICAL note below.
2. API Gateway with a Cognito authorizer — the deployed API is a REST API, whose
   Lambda proxy integration sends payload format 1.0 and puts claims at
   ``event["requestContext"]["authorizer"]["claims"]``; the HTTP API / JWT
   authorizer form (``...["authorizer"]["jwt"]["claims"]``) is read too. The
   request body carries ``{"arguments": {...}}``.

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

CRITICAL — an ``identity`` in the event is never authoritative
-------------------------------------------------------------
AppSync itself is gone (removed in 0.6.0), so nothing legitimate delivers a
resolver-shaped event over a transport that has already authenticated the user.
The only caller that can present one is a principal holding
``lambda:InvokeFunction`` directly on the function, and the ``identity`` it puts
in that payload is its own assertion — no signed token backs it. Passing it
through unchanged let such a caller state its own ``cognito:groups`` and have the
group check made against its own claim.

:func:`normalize_event` therefore applies the same precedence rule the chat
streaming endpoint applies to a body-supplied ``callerSub``
(``resolve_caller_sub`` in ``src/lambda/chat_stream_processor/sse.py``): **the
principal the transport verified wins, and an asserted one that contradicts it is
refused rather than silently preferred.** Where there is no verified principal to
compare against, an asserted identity cannot be honoured at all and is refused
with :class:`CallerIdentityRefused`.

The one shape that still passes through is an event whose ``identity`` is
explicitly ``None``. Across this repository that is the established marker for a
service-to-service invocation gated by IAM on the function ARN rather than by
Cognito groups (``idp_common.testset_scope.is_direct_invoke``,
``_enforce_agent_chat_groups`` in ``src/lambda/agent_chat_processor``): it asserts
no groups, so there is nothing to forge, and the group gates downstream treat it
as ungrouped. Refusing it would break those backend paths, and rewriting an
asserted identity to ``None`` instead of refusing it would be worse than either —
it would promote a caller's failed assertion into the trusted-backend marker.
"""

import base64
import json
import logging
import os
import re
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CallerIdentityRefused(PermissionError):
    """A caller-supplied ``identity`` was refused instead of being honoured.

    Raised when an event carries its own ``identity`` object and either
    contradicts the principal the transport verified, or arrives with no verified
    principal at all — in which case there is nothing that could establish the
    claim, so the authorization decision is refused rather than made from it.

    A subclass of :class:`PermissionError` so every existing mapping treats it as
    an authorization denial: the dispatcher and :func:`api_resolver` both turn a
    ``PermissionError`` into HTTP 403 with ``errorType: "Unauthorized"``.

    The sibling of ``CallerIdentityConflict`` in
    ``src/lambda/chat_stream_processor/sse.py``, which applies the same rule to
    that transport's body-supplied ``callerSub``. The two are deliberately not one
    class: ``sse.py`` lives inside a Lambda's ``CodeUri`` and is not importable
    from this library, and the identifiers it reconciles (SigV4 principals) are
    not the ones reconciled here (Cognito claim sets).
    """


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
    """An AppSync resolver event always carries ``arguments`` and ``identity``.

    A true answer says only that the event has the *shape* AppSync used, not that
    anything authenticated the caller — AppSync was removed in 0.6.0, so nothing
    but a direct ``lambda:InvokeFunction`` produces this shape now. See
    :func:`normalize_event` for what is done with the ``identity`` it carries.
    """
    return isinstance(event, dict) and "arguments" in event and "identity" in event


def _verified_claims(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The claim set the transport verified for this request, or ``None``.

    This is the ONLY trustworthy source of ``cognito:groups`` on the API path: API
    Gateway's Cognito authorizer validates the token and writes the claims into
    the request context, which a caller cannot reach from the request body.

    Both authorizer shapes are read, because both occur: a REST API's Lambda proxy
    integration (payload format 1.0, which is what this solution deploys) puts
    them at ``requestContext.authorizer.claims``, while an HTTP API JWT authorizer
    (payload format 2.0) puts them at ``requestContext.authorizer.jwt.claims``.
    ``None`` means the event carries no authorizer context at all — i.e. it did not
    arrive through the gateway.
    """
    rc = event.get("requestContext") if isinstance(event, dict) else None
    if not isinstance(rc, dict):
        return None
    authorizer = rc.get("authorizer")
    if not isinstance(authorizer, dict):
        return None
    jwt_claims = (authorizer.get("jwt") or {}).get("claims")
    if isinstance(jwt_claims, dict) and jwt_claims:
        return jwt_claims
    claims = authorizer.get("claims")
    if isinstance(claims, dict) and claims:
        return claims
    return None


def _principal_names(claims: Dict[str, Any]) -> List[str]:
    """Every identifier a claim set offers for the principal it describes.

    Resolvers key per-object scope off ``identity.username`` and ``identity.sub``
    interchangeably (the AppSync convention was email in ``username``), so a
    comparison has to accept any of them as naming the same principal.
    """
    return [
        str(v)
        for v in (
            claims.get("sub"),
            claims.get("email"),
            claims.get("cognito:username"),
            claims.get("username"),
        )
        if v
    ]


def _refuse_if_contradicts_verified(
    asserted: Any, verified_claims: Dict[str, Any]
) -> None:
    """Refuse an asserted identity that disagrees with the verified principal.

    The verified claims win either way — :func:`normalize_event` rebuilds the
    identity from them and never reads the asserted object. This raises so that a
    disagreement is a refusal rather than a silent discard: an asserted identity
    that names a different principal, or claims a group the token does not carry,
    is never a legitimate request, and answering it with the verified caller's own
    (lesser) permissions would hide the attempt.
    """
    if not isinstance(asserted, dict):
        raise CallerIdentityRefused(
            "Unauthorized: the event asserts an identity of type "
            f"{type(asserted).__name__}, which cannot be reconciled with the "
            "identity the transport verified"
        )

    asserted_claims = asserted.get("claims")
    if not isinstance(asserted_claims, dict):
        asserted_claims = {}

    extra_groups = set(_coerce_groups(asserted_claims.get("cognito:groups"))) - set(
        _coerce_groups(verified_claims.get("cognito:groups"))
    )
    if extra_groups:
        raise CallerIdentityRefused(
            "Unauthorized: the event asserts group membership the verified token "
            f"does not carry ({sorted(extra_groups)})"
        )

    verified_names = set(_principal_names(verified_claims))
    asserted_names = set(_principal_names(asserted_claims)) | set(
        _principal_names(asserted)
    )
    conflicting = asserted_names - verified_names
    if conflicting:
        raise CallerIdentityRefused(
            "Unauthorized: the event asserts a caller identity that does not "
            "match the identity the transport verified"
        )


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
    """Return an AppSync-shaped event whose ``identity`` is one the transport proved.

    Gateway events (a REST proxy event with a Cognito authorizer, or an HTTP API
    payload-v2.0 event with a JWT authorizer) are converted to ``{"arguments",
    "identity", "info"}`` with ``identity`` built from the **verified** claims and
    ``identity.claims['cognito:groups']`` restored to a list.

    An event that carries its own ``identity`` is handled by the rule in this
    module's docstring, and only these three things can happen to it:

    * ``identity`` is explicitly ``None`` and no verified claims accompany it —
      the IAM-gated service-to-service invocation. Passed through unchanged; it
      asserts no groups, and every group gate downstream reads that as ungrouped.
    * Verified claims are present — they win, the identity is rebuilt from them,
      and the asserted object is refused if it contradicts them
      (:func:`_refuse_if_contradicts_verified`).
    * Neither — an asserted identity with nothing that could establish it. Refused
      with :class:`CallerIdentityRefused`, which the dispatcher and
      :func:`api_resolver` render as 403.

    :raises CallerIdentityRefused: for the two refusal cases above. It subclasses
        ``PermissionError``, so callers that already map authorization denials need
        no new branch.
    """
    verified = _verified_claims(event)

    if _is_appsync_event(event):
        # The key is present by definition of the shape, so an explicit null is
        # distinguishable from an absent key here — and the two mean different
        # things: null is the IAM-gated service-to-service marker.
        asserted = event["identity"]
        if verified is None:
            if asserted is None:
                return event
            logger.warning(
                "Refusing an event that asserts its own identity with no verified "
                "claims to support it (a direct invocation cannot choose its own "
                "Cognito groups)"
            )
            raise CallerIdentityRefused(
                "Unauthorized: this invocation asserts a caller identity that no "
                "transport verified"
            )
        _refuse_if_contradicts_verified(asserted, verified)
        # Fall through: the identity below is rebuilt from the verified claims, so
        # the asserted object is never read even when it agreed.

    rc = event.get("requestContext") or {}
    jwt_claims = verified or {}

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
    - For a resolver-shaped (direct) invocation, returns the handler result
      unchanged and lets exceptions propagate — including the
      :class:`CallerIdentityRefused` that :func:`normalize_event` raises for an
      invocation asserting its own identity, which surfaces to the invoker as a
      function error rather than as a 200.
    """

    @wraps(fn)
    def wrapper(event: Dict[str, Any], context: Any = None) -> Any:
        is_http = not _is_appsync_event(event)
        if not is_http:
            return fn(normalize_event(event), context)

        try:
            result = fn(normalize_event(event), context)
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
