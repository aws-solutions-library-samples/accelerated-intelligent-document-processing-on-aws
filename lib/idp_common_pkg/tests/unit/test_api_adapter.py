# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for idp_common.api_adapter.

These tests are the RBAC-correctness gate for the AppSync -> API Gateway HTTP
API migration. The single most important behavior is that the HTTP API JWT
authorizer's flattened ``cognito:groups`` string (e.g. "[Admin Author]") is
restored to a list so resolver RBAC keeps working identically to AppSync.
"""

import json

import pytest

from idp_common.api_adapter import (
    CallerIdentityRefused,
    _coerce_groups,
    api_resolver,
    normalize_event,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# _coerce_groups — the critical claim-shape normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ([], []),
        (["Admin"], ["Admin"]),
        (["Admin", "Author"], ["Admin", "Author"]),
        ("Admin", ["Admin"]),
        # HTTP API JWT authorizer flattened forms:
        ("[Admin Author]", ["Admin", "Author"]),
        ("[Admin]", ["Admin"]),
        ("[]", []),
        ("[Admin Author Viewer]", ["Admin", "Author", "Viewer"]),
        # JSON-encoded list form:
        ('["Admin","Author"]', ["Admin", "Author"]),
        ('["Admin"]', ["Admin"]),
        # REST API Cognito authorizer comma-joined form:
        ("Admin,Author", ["Admin", "Author"]),
        ("Admin, Author, Viewer", ["Admin", "Author", "Viewer"]),
        ("Admin\nAuthor", ["Admin", "Author"]),
        # whitespace / empty string
        ("", []),
        ("   ", []),
    ],
)
def test_coerce_groups(raw, expected):
    assert _coerce_groups(raw) == expected


# --------------------------------------------------------------------------- #
# normalize_event — an identity the event asserts is never authoritative
#
# AppSync was removed in 0.6.0, so a resolver-shaped event no longer arrives from
# a transport that authenticated anybody: the only caller that can present one is
# a principal invoking the function directly, and the identity it puts in the
# payload is its own assertion. These tests pin the three outcomes — pass through
# an explicit "no identity" (the IAM-gated backend marker), prefer the verified
# claims, refuse an assertion that contradicts them or has nothing behind it.
# --------------------------------------------------------------------------- #
def _asserted(field="listDocuments", groups=("Admin",), **identity_extra):
    """A resolver-shaped event whose identity is the caller's own claim."""
    identity = {"claims": {"cognito:groups": list(groups)}}
    identity.update(identity_extra)
    return {
        "arguments": {"limit": 50},
        "identity": identity,
        "info": {"fieldName": field},
    }


def test_backend_invocation_with_no_identity_passes_through_unchanged():
    """The IAM-gated service-to-service path, which must keep working.

    A null ``identity`` is this repository's marker for an invocation gated by IAM
    on the function ARN rather than by Cognito groups — see
    ``idp_common.testset_scope.is_direct_invoke`` and
    ``_enforce_agent_chat_groups`` in ``src/lambda/agent_chat_processor``. It
    asserts no groups, so there is nothing to forge, and every group gate
    downstream reads it as ungrouped.
    """
    event = {
        "arguments": {"limit": 50},
        "identity": None,
        "info": {"fieldName": "listDocuments"},
    }
    out = normalize_event(event)
    assert out is event, "the backend path must be passed through untouched"
    assert out["identity"] is None


def test_an_asserted_identity_with_no_verified_claims_is_refused():
    """The escalation this refusal exists to stop: choosing your own groups."""
    with pytest.raises(CallerIdentityRefused):
        normalize_event(_asserted(groups=("Admin",)))


def test_even_an_asserted_identity_claiming_no_groups_is_refused():
    """Refused, not rewritten to the backend marker.

    Stripping the assertion instead would be worse than honouring it: an identity
    of ``None`` is the trusted-backend marker, so downgrading a caller's failed
    assertion into it would stand the group gates down rather than close them.
    """
    with pytest.raises(CallerIdentityRefused):
        normalize_event(_asserted(groups=()))


@pytest.mark.parametrize("identity", ["Admin", ["Admin"], 7])
def test_a_non_dict_asserted_identity_is_refused(identity):
    event = {"arguments": {}, "identity": identity, "info": {"fieldName": "x"}}
    with pytest.raises(CallerIdentityRefused):
        normalize_event(event)


def test_the_refusal_is_an_authorization_denial():
    """A ``PermissionError`` subclass, so every existing mapping makes it a 403."""
    assert issubclass(CallerIdentityRefused, PermissionError)


def test_verified_claims_win_over_an_asserted_identity_that_agrees():
    """Precedence, not refusal, when there is nothing to disagree about."""
    event = _http_event("listDocuments", "[Viewer]", email="user@example.com")
    event["arguments"] = {}
    event["identity"] = {
        "claims": {"cognito:groups": ["Viewer"], "email": "user@example.com"},
        "username": "user@example.com",
    }
    out = normalize_event(event)
    # Rebuilt from the request context, not handed back as supplied.
    assert out is not event
    assert out["identity"]["claims"]["cognito:groups"] == ["Viewer"]


def test_an_asserted_identity_claiming_an_unverified_group_is_refused():
    """The verified claim says Viewer; the payload says Admin. Refused, not downgraded.

    Refusing rather than quietly using the verified (lesser) claims is the same
    rule ``resolve_caller_sub`` applies in ``src/lambda/chat_stream_processor``:
    a supplied identity that contradicts the proven one is never legitimate.
    """
    event = _http_event("listUsers", "Viewer")
    event["arguments"] = {}
    event["identity"] = {"claims": {"cognito:groups": ["Admin"]}}
    with pytest.raises(CallerIdentityRefused) as exc:
        normalize_event(event)
    assert "Admin" in str(exc.value)


def test_an_asserted_identity_naming_another_principal_is_refused():
    """Ownership scope is keyed off identity.username/sub, so impersonation counts."""
    event = _http_event("getMyProfile", "[Viewer]", email="user@example.com")
    event["arguments"] = {}
    event["identity"] = {
        "claims": {"cognito:groups": ["Viewer"]},
        "username": "someone.else@example.com",
    }
    with pytest.raises(CallerIdentityRefused):
        normalize_event(event)


# --------------------------------------------------------------------------- #
# normalize_event — HTTP API conversion
# --------------------------------------------------------------------------- #
def _http_event(field, groups_claim, arguments=None, email="user@example.com"):
    return {
        "version": "2.0",
        "routeKey": f"POST /op/{field}",
        "rawPath": f"/op/{field}",
        "pathParameters": {"field": field},
        "isBase64Encoded": False,
        "body": json.dumps({"arguments": arguments or {}}),
        "requestContext": {
            "http": {"method": "POST", "path": f"/op/{field}", "sourceIp": "1.2.3.4"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "cognito:groups": groups_claim,
                        "cognito:username": "user-sub-123",
                        "email": email,
                        "sub": "user-sub-123",
                    }
                }
            },
        },
    }


def test_http_event_restores_groups_list():
    """The flattened authorizer groups string must become a list for RBAC."""
    event = _http_event("listDocuments", "[Admin Author]", {"limit": 10})
    out = normalize_event(event)
    groups = out["identity"]["claims"]["cognito:groups"]
    assert groups == ["Admin", "Author"]
    assert isinstance(groups, list)


def test_http_event_field_and_arguments():
    event = _http_event("reprocessDocument", "[Author]", {"objectKeys": ["k1"]})
    out = normalize_event(event)
    assert out["info"]["fieldName"] == "reprocessDocument"
    assert out["arguments"] == {"objectKeys": ["k1"]}


def _rest_event(field, groups_claim, arguments=None, email="user@example.com"):
    """API Gateway REST API (v1) proxy event with a Cognito User Pools authorizer.

    Claims live at requestContext.authorizer.claims (flat, not .jwt.claims) and
    cognito:groups is a comma-joined string.
    """
    return {
        "resource": "/op/{field}",
        "path": f"/op/{field}",
        "httpMethod": "POST",
        "pathParameters": {"field": field},
        "isBase64Encoded": False,
        "body": json.dumps({"arguments": arguments or {}}),
        "requestContext": {
            "resourcePath": "/op/{field}",
            "httpMethod": "POST",
            "identity": {"sourceIp": "1.2.3.4"},
            "authorizer": {
                "claims": {
                    "cognito:groups": groups_claim,
                    "cognito:username": "user-sub-123",
                    "email": email,
                    "sub": "user-sub-123",
                }
            },
        },
    }


def test_rest_event_claims_and_groups():
    """REST API authorizer claims (flat) + comma-joined groups normalize correctly."""
    event = _rest_event("listDocuments", "Admin,Author", {"limit": 5})
    out = normalize_event(event)
    assert out["info"]["fieldName"] == "listDocuments"
    assert out["arguments"] == {"limit": 5}
    assert out["identity"]["claims"]["cognito:groups"] == ["Admin", "Author"]
    assert out["identity"]["username"] == "user@example.com"


def test_rest_event_single_group():
    event = _rest_event("getPricing", "Viewer")
    out = normalize_event(event)
    assert out["identity"]["claims"]["cognito:groups"] == ["Viewer"]


def test_rest_event_rbac_parity():
    """An Admin-only handler behaves the same on REST API events."""

    @api_resolver
    def handler(event, context):
        if "Admin" not in event["identity"]["claims"]["cognito:groups"]:
            raise PermissionError("Admin only")
        return {"ok": True}

    assert handler(_rest_event("x", "Admin,Author"), None)["statusCode"] == 200
    assert handler(_rest_event("x", "Viewer"), None)["statusCode"] == 403


def test_http_event_identity_username_is_email():
    event = _http_event("getMyProfile", "[Viewer]", email="reviewer@corp.com")
    out = normalize_event(event)
    # Resolvers read identity.username as the email (AppSync convention).
    assert out["identity"]["username"] == "reviewer@corp.com"
    assert out["identity"]["claims"]["email"] == "reviewer@corp.com"


def test_http_event_empty_groups():
    event = _http_event("listDocuments", "[]")
    out = normalize_event(event)
    assert out["identity"]["claims"]["cognito:groups"] == []


def test_http_event_field_from_raw_path_when_no_path_params():
    event = _http_event("getPricing", "[Admin]")
    del event["pathParameters"]
    out = normalize_event(event)
    assert out["info"]["fieldName"] == "getPricing"


def test_http_event_base64_body():
    import base64 as b64

    raw = json.dumps({"arguments": {"x": 1}})
    event = _http_event("getPricing", "[Admin]")
    event["body"] = b64.b64encode(raw.encode()).decode()
    event["isBase64Encoded"] = True
    out = normalize_event(event)
    assert out["arguments"] == {"x": 1}


def test_http_event_bare_body_without_arguments_key():
    """Tolerate a body that is the arguments dict directly."""
    event = _http_event("x", "[Admin]")
    event["body"] = json.dumps({"objectKey": "abc"})
    out = normalize_event(event)
    assert out["arguments"] == {"objectKey": "abc"}


# --------------------------------------------------------------------------- #
# api_resolver decorator
# --------------------------------------------------------------------------- #
def test_decorator_direct_invocation_returns_raw():
    """The direct (resolver-shaped) path returns the handler result unwrapped.

    Exercised with the only resolver-shaped event that is still accepted: the
    IAM-gated backend invocation, whose identity is null.
    """

    @api_resolver
    def handler(event, context):
        return {"ok": True, "identity": event["identity"]}

    event = {"arguments": {}, "identity": None, "info": {"fieldName": "x"}}
    result = handler(event, None)
    # Direct path: raw return, no statusCode wrapping.
    assert result == {"ok": True, "identity": None}


def test_decorator_refuses_a_direct_invocation_that_asserts_an_identity():
    """The refusal reaches the invoker instead of the handler running as Admin."""
    calls = []

    @api_resolver
    def handler(event, context):
        calls.append(event)
        return {"ok": True}

    with pytest.raises(CallerIdentityRefused):
        handler(_asserted(field="x", groups=("Admin",)), None)
    assert calls == [], "the handler must not run on a refused identity"


def test_decorator_http_wraps_success():
    @api_resolver
    def handler(event, context):
        return {"value": event["arguments"].get("n", 0) * 2}

    event = _http_event("calc", "[Admin]", {"n": 21})
    result = handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"value": 42}
    assert result["headers"]["Content-Type"] == "application/json"


def test_http_response_sets_security_headers():
    # ZAP flagged the execute-api endpoint for missing security headers; the
    # dispatcher's own responses must now carry them (defense-in-depth for
    # direct-API access; CloudFront already sets them for the SPA origin).
    from idp_common.api_adapter import _http_response

    h = _http_response(200, {"ok": True})["headers"]
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "max-age=31536000" in h["Strict-Transport-Security"]
    assert "includeSubDomains" in h["Strict-Transport-Security"]
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_http_response_cors_origin_defaults_star_and_is_overridable(monkeypatch):
    from idp_common.api_adapter import _http_response

    monkeypatch.delenv("CORS_ALLOW_ORIGIN", raising=False)
    assert _http_response(200, {})["headers"]["Access-Control-Allow-Origin"] == "*"
    # A deployment can lock the origin down without touching code (avoids the
    # CloudFront->API CFN dependency cycle that forces the wildcard default).
    monkeypatch.setenv("CORS_ALLOW_ORIGIN", "https://d123.cloudfront.net")
    assert (
        _http_response(200, {})["headers"]["Access-Control-Allow-Origin"]
        == "https://d123.cloudfront.net"
    )


def test_decorator_http_permission_error_403():
    @api_resolver
    def handler(event, context):
        raise PermissionError("not allowed")

    result = handler(_http_event("x", "[Viewer]"), None)
    assert result["statusCode"] == 403
    body = json.loads(result["body"])
    assert body["errors"][0]["errorType"] == "Unauthorized"


def test_decorator_http_value_error_400():
    @api_resolver
    def handler(event, context):
        raise ValueError("bad input")

    result = handler(_http_event("x", "[Admin]"), None)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["errors"][0]["errorType"] == "BadRequest"


def test_decorator_http_unexpected_error_500():
    @api_resolver
    def handler(event, context):
        raise RuntimeError("boom")

    result = handler(_http_event("x", "[Admin]"), None)
    assert result["statusCode"] == 500
    assert json.loads(result["body"])["errors"][0]["errorType"] == "InternalError"


def test_decorator_http_decimal_serialization():
    from decimal import Decimal

    @api_resolver
    def handler(event, context):
        return {"cost": Decimal("1.23"), "count": Decimal("5")}

    result = handler(_http_event("x", "[Admin]"), None)
    body = json.loads(result["body"])
    assert body == {"cost": 1.23, "count": 5}


def test_decorator_rbac_depends_on_where_the_groups_came_from():
    """An Admin-only handler admits the verified Admin and nobody who claims to be one.

    The three arms are the whole point of this module: the groups decide the
    outcome only when the transport established them. Asserting the same group list
    in the payload buys nothing, and the group list the authorizer flattens is
    restored so a real Admin is not locked out.
    """

    @api_resolver
    def handler(event, context):
        groups = event["identity"]["claims"]["cognito:groups"]
        if "Admin" not in groups:
            raise PermissionError("Admin only")
        return {"ok": True}

    # Verified Admin (flattened groups claim) -> allowed.
    assert handler(_http_event("x", "[Admin Author]"), None)["statusCode"] == 200

    # Verified non-Admin -> 403.
    assert handler(_http_event("x", "[Viewer]"), None)["statusCode"] == 403

    # Self-asserted Admin on a direct invocation -> refused before the handler runs.
    with pytest.raises(CallerIdentityRefused):
        handler(_asserted(field="x", groups=("Admin",)), None)
