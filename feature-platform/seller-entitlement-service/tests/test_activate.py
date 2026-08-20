# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the seller-side activation Lambda.

The security-critical property is that the buyer account is taken ONLY from the
API-Gateway-verified `requestContext.identity`, never from the request body — a
body field would let any caller claim to be a subscribed account and defeat the
entire service. Several tests below exist purely to pin that down.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

_LAMBDA = Path(__file__).resolve().parents[1] / "lambdas" / "activate" / "index.py"


@pytest.fixture
def mod(monkeypatch):
    """Load the lambda fresh with a known environment."""
    monkeypatch.setenv(
        "PRODUCT_REGISTRY_JSON",
        json.dumps(
            {
                "prod-paid": {"productCode": "code-paid", "allowFreeTier": False},
                "prod-freemium": {"productCode": "code-free", "allowFreeTier": True},
            }
        ),
    )
    monkeypatch.setenv("SIGNING_KEY_ARN", "arn:aws:kms:us-east-1:1:key/abc")
    monkeypatch.setenv("TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("ALLOWED_ACCOUNTS", "")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    spec = importlib.util.spec_from_file_location("seller_activate", _LAMBDA)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Never touch real KMS.
    monkeypatch.setattr(module, "_kms", lambda: _FakeKms(), raising=True)
    return module


class _FakeKms:
    def sign(self, **kwargs):
        assert kwargs["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
        assert kwargs["MessageType"] == "RAW"
        return {"Signature": b"fake-signature"}


def _event(*, account_id=None, body=None, user_arn=None):
    identity = {}
    if account_id is not None:
        identity["accountId"] = account_id
    if user_arn is not None:
        identity["userArn"] = user_arn
    return {
        "requestContext": {"identity": identity},
        "body": json.dumps(body if body is not None else {"productId": "prod-paid"}),
    }


def _claims(response):
    return json.loads(base64.b64decode(json.loads(response["body"])["token"]))


# ---------------------------------------------------------------------------
# Caller identity — the security boundary.
# ---------------------------------------------------------------------------


def test_buyer_account_comes_from_verified_identity_not_body(mod, monkeypatch):
    """A body-supplied account MUST be ignored.

    If this ever regresses, anyone could POST {"buyerAccountId": "<subscribed
    account>"} and mint a valid token for a product they never bought.
    """
    seen = {}
    monkeypatch.setattr(
        mod,
        "_has_active_agreement",
        lambda product_id, buyer: (seen.update(buyer=buyer), (True, "ok"))[1],
    )
    resp = mod.handler(
        _event(
            account_id="111111111111",
            body={"productId": "prod-paid", "buyerAccountId": "999999999999"},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    assert seen["buyer"] == "111111111111", "must use the VERIFIED account"
    assert _claims(resp)["buyerAccountId"] == "111111111111"


def test_missing_verified_identity_refuses_to_mint(mod):
    """No verified caller → 401, never a token.

    Only reachable if the API method is misconfigured (authorization NONE). Fail
    closed: minting for an unidentified caller would be a free-for-all.
    """
    resp = mod.handler(_event(account_id=None), None)
    assert resp["statusCode"] == 401
    assert "token" not in resp["body"]


def test_falls_back_to_parsing_verified_caller_arn(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "ok"))
    resp = mod.handler(
        _event(account_id=None, user_arn="arn:aws:sts::222222222222:assumed-role/r/s"),
        None,
    )
    assert resp["statusCode"] == 200
    assert _claims(resp)["buyerAccountId"] == "222222222222"


# ---------------------------------------------------------------------------
# Entitlement decision.
# ---------------------------------------------------------------------------


def test_active_agreement_issues_token(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-1"))
    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["freeTier"] is False
    assert body["signingAlgorithm"] == "RSASSA_PSS_SHA_256"
    claims = _claims(resp)
    assert claims["productId"] == "prod-paid"
    assert claims["exp"] > claims["iat"]


def test_no_agreement_on_paid_only_product_is_refused(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (False, "none"))
    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "not_entitled"


def test_no_agreement_on_freemium_product_gets_free_tier_token(mod, monkeypatch):
    """A listing with a free dimension serves unsubscribed accounts in reduced
    mode rather than refusing — otherwise the free tier is unusable."""
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (False, "none"))
    resp = mod.handler(
        _event(account_id="111111111111", body={"productId": "prod-freemium"}), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["freeTier"] is True
    assert _claims(resp)["freeTier"] is True


def test_subscribed_freemium_account_is_not_marked_free_tier(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-1"))
    resp = mod.handler(
        _event(account_id="111111111111", body={"productId": "prod-freemium"}), None
    )
    assert json.loads(resp["body"])["freeTier"] is False


def test_agreement_lookup_failure_fails_CLOSED(mod, monkeypatch):
    """Opposite of the host's advisory-allow, on purpose.

    An error here means the SELLER's infrastructure is broken; issuing tokens on
    our own failure would make the gate meaningless. The buyer-side grace period
    on the last-known-good token is what protects a paying customer.
    """
    monkeypatch.setattr(
        mod, "_has_active_agreement", lambda p, b: (False, "agreement lookup failed: x")
    )
    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# Product registry + input handling.
# ---------------------------------------------------------------------------


def test_unknown_product_does_not_disclose_the_catalog(mod):
    """Superseded 404: an unknown product now answers exactly like "not entitled",
    so existence is not observable. See
    test_unknown_product_is_indistinguishable_from_not_entitled."""
    resp = mod.handler(
        _event(account_id="111111111111", body={"productId": "prod-nope"}), None
    )
    assert resp["statusCode"] == 403
    assert "prod-paid" not in resp["body"]


def test_missing_product_id_is_400(mod):
    resp = mod.handler(_event(account_id="111111111111", body={}), None)
    assert resp["statusCode"] == 400


def test_non_json_body_is_400(mod):
    event = _event(account_id="111111111111")
    event["body"] = "not json"
    assert mod.handler(event, None)["statusCode"] == 400


def test_allow_listed_account_bypasses_the_check(mod, monkeypatch):
    """Documented escape hatch for the seller's own test deployments."""
    monkeypatch.setattr(
        mod,
        "_has_active_agreement",
        lambda p, b: pytest.fail(
            "must not call Marketplace for an allow-listed account"
        ),
    )
    monkeypatch.setattr(mod, "_ALLOWED_ACCOUNTS", {"333333333333"})
    resp = mod.handler(_event(account_id="333333333333"), None)
    assert resp["statusCode"] == 200


def test_missing_signing_key_is_500_not_an_unsigned_token(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "ok"))
    monkeypatch.setattr(mod, "_SIGNING_KEY_ARN", "")
    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 500
    assert "token" not in json.loads(resp["body"])


# ---------------------------------------------------------------------------
# Seller-side query shape. VERIFIED against a live seller account with a positive
# control (subscribed buyer -> ACTIVE agreement) and a negative one (unsubscribed
# buyer -> empty list, not an error).
# ---------------------------------------------------------------------------


def test_search_agreements_uses_proposer_side_filters(mod, monkeypatch):
    calls = []

    class _FakeAgreement:
        def search_agreements(self, **kwargs):
            calls.append(kwargs)
            return {"agreementViewSummaries": [{"agreementId": "agmt-9"}]}

    monkeypatch.setattr(mod, "_agreement", lambda: _FakeAgreement())
    entitled, detail = mod._has_active_agreement("prod-paid", "111111111111")

    assert entitled is True
    assert "agmt-9" in detail
    (call,) = calls
    assert call["catalog"] == "AWSMarketplace"
    names = {f["name"]: f["values"] for f in call["filters"]}
    # Proposer, not Acceptor: this runs in the SELLER account.
    assert names["PartyType"] == ["Proposer"]
    assert names["AgreementType"] == ["PurchaseAgreement"]
    assert names["AcceptorAccountId"] == ["111111111111"]
    assert names["Status"] == ["ACTIVE"]
    # `ResourceIdentifier` is the only accepted name: the docs' Proposer-side
    # prose says `ResourceId`, which the live service rejects with
    # `ValidationException: Provided filter name is invalid`.
    assert names.get("ResourceIdentifier") == ["prod-paid"]
    assert "ResourceId" not in names


def test_lookup_error_reports_not_entitled(mod, monkeypatch):
    from botocore.exceptions import ClientError

    class _BrokenAgreement:
        def search_agreements(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "SearchAgreements",
            )

    monkeypatch.setattr(mod, "_agreement", lambda: _BrokenAgreement())
    entitled, detail = mod._has_active_agreement("prod-paid", "111111111111")
    assert entitled is False
    assert "agreement lookup failed" in detail


def test_empty_result_is_not_entitled(mod, monkeypatch):
    class _EmptyAgreement:
        def search_agreements(self, **kwargs):
            return {"agreementViewSummaries": []}

    monkeypatch.setattr(mod, "_agreement", lambda: _EmptyAgreement())
    entitled, detail = mod._has_active_agreement("prod-paid", "111111111111")
    assert entitled is False
    assert "no active agreement" in detail


# ---------------------------------------------------------------------------
# Activation roster. The durable answer to "who has connected?" — the logs age
# out with LogRetentionInDays and are printf-formatted, so they are the forensic
# backstop, not the record.
# ---------------------------------------------------------------------------


class _Table:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def update_item(self, **kwargs):
        if self._error:
            raise self._error
        self.calls.append(kwargs)
        return {}


def _with_roster(mod, monkeypatch, error=None):
    table = _Table(error=error)
    monkeypatch.setattr(mod, "_ACTIVATIONS_TABLE", "activations")
    monkeypatch.setattr(mod, "_activations", lambda: table)
    return table


def test_granted_activation_is_recorded(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-7"))
    table = _with_roster(mod, monkeypatch)

    resp = mod.handler(
        _event(
            account_id="111111111111",
            user_arn="arn:aws:sts::111111111111:assumed-role/r/s",
        ),
        None,
    )
    assert resp["statusCode"] == 200
    (call,) = table.calls
    assert call["Key"] == {
        "buyerAccountId": "111111111111",
        "productId": "prod-paid",
    }
    vals = call["ExpressionAttributeValues"]
    assert vals[":outcome"] == "granted"
    assert vals[":granted"] == 1
    assert vals[":one"] == 1
    # Counters must ADD and first-seen must not be clobbered by concurrent calls.
    assert "ADD attemptCount :one, grantedCount :granted" in call["UpdateExpression"]
    assert "if_not_exists(firstAttemptAt, :now)" in call["UpdateExpression"]


def test_refused_activation_is_also_recorded(mod, monkeypatch):
    """A refusal is the more interesting record — it's an unentitled account
    trying to use a paid product."""
    monkeypatch.setattr(
        mod, "_has_active_agreement", lambda p, b: (False, "no active agreement")
    )
    table = _with_roster(mod, monkeypatch)

    resp = mod.handler(_event(account_id="222222222222"), None)
    assert resp["statusCode"] == 403
    (call,) = table.calls
    vals = call["ExpressionAttributeValues"]
    assert vals[":outcome"] == "refused"
    assert vals[":granted"] == 0
    assert vals[":one"] == 1
    assert "no active agreement" in vals[":detail"]


def test_roster_write_failure_does_not_break_activation(mod, monkeypatch):
    """Bookkeeping must never be the reason an entitled customer is refused."""
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-7"))
    _with_roster(mod, monkeypatch, error=RuntimeError("table on fire"))

    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 200, "must still issue the token"


def test_recording_is_skipped_when_no_table_configured(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "ok"))
    monkeypatch.setattr(mod, "_ACTIVATIONS_TABLE", "")
    monkeypatch.setattr(
        mod,
        "_activations",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve a table")),
    )
    assert mod.handler(_event(account_id="111111111111"), None)["statusCode"] == 200


def test_detail_and_arn_are_truncated(mod, monkeypatch):
    """Unbounded strings from an API error must not blow the DDB item limit."""
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "x" * 5000))
    table = _with_roster(mod, monkeypatch)
    mod.handler(
        _event(account_id="111111111111", user_arn="arn:aws:sts::1:" + "y" * 5000),
        None,
    )
    vals = table.calls[0]["ExpressionAttributeValues"]
    assert len(vals[":detail"]) <= 512
    assert len(vals[":arn"]) <= 512


def test_activation_metric_is_valid_emf(mod, monkeypatch, caplog):
    import logging as _logging

    with caplog.at_level(_logging.INFO):
        mod._emit_activation_metric("prod-paid", "refused")

    records = []
    for message in caplog.messages:
        try:
            parsed = json.loads(message)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "_aws" in parsed:
            records.append(parsed)

    assert len(records) == 1
    payload = records[0]
    assert isinstance(payload["_aws"]["Timestamp"], int)
    (directive,) = payload["_aws"]["CloudWatchMetrics"]
    assert directive["Metrics"] == [{"Name": "ActivationAttempt", "Unit": "Count"}]
    for dimension_set in directive["Dimensions"]:
        for dim in dimension_set:
            assert dim in payload, f"dimension {dim} missing from EMF payload"
    assert payload["Outcome"] == "refused"
    assert payload["ProductId"] == "prod-paid"


def test_metric_emission_never_raises(mod, monkeypatch):
    monkeypatch.setattr(
        mod.logger, "info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    mod._emit_activation_metric("prod-paid", "granted")


# ---------------------------------------------------------------------------
# Security hardening. The endpoint admits ANY authenticated AWS principal, so
# every one of these inputs is reachable by an attacker who merely has an AWS
# account — the bar is "has credentials", not "is a customer".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,label",
    [
        ("[1,2]", "JSON array"),
        ('"hello"', "JSON string"),
        ("null", "JSON null"),
        ("0", "JSON number"),
        ("true", "JSON bool"),
    ],
)
def test_non_object_body_is_400_not_a_crash(mod, body, label):
    """Regression: `json.loads` returns list/str/int/None for these, and calling
    .get() on them raised an unhandled AttributeError — so a ONE-BYTE body ("0")
    from any AWS account produced a 502 plus a stack trace, burying real signals."""
    event = _event(account_id="111111111111")
    event["body"] = body
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 400, label
    assert "must be a JSON object" in resp["body"]


def test_oversized_body_is_rejected_before_parsing(mod):
    """API Gateway accepts up to 10 MB; the only legitimate field here is a
    product id, so anything large is abuse and must not reach json.loads."""
    event = _event(account_id="111111111111")
    event["body"] = json.dumps({"productId": "prod-paid", "pad": "x" * 20000})
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 413


def test_unknown_product_is_indistinguishable_from_not_entitled(mod, monkeypatch):
    """No product-existence oracle.

    Any AWS account can probe this endpoint, and a listing in "Limited" state has
    a productId that is NOT public — so a distinct 404 would confirm an unreleased
    product exists. Status AND body must match the not-entitled response exactly.
    """
    monkeypatch.setattr(
        mod, "_has_active_agreement", lambda p, b: (False, "no active agreement")
    )
    unknown = mod.handler(
        _event(account_id="111111111111", body={"productId": "prod-does-not-exist"}),
        None,
    )
    refused = mod.handler(_event(account_id="111111111111"), None)

    assert unknown["statusCode"] == refused["statusCode"] == 403
    assert unknown["body"] == refused["body"]
    # And it must not name the products that DO exist.
    assert "prod-paid" not in unknown["body"]
    assert "prod-freemium" not in unknown["body"]


def test_refusal_never_leaks_internal_error_detail_to_the_caller(mod, monkeypatch):
    """The roster records why; the caller is told only "not entitled".

    An AccessDenied or ValidationException string would disclose seller-side
    configuration to an arbitrary AWS account.
    """
    monkeypatch.setattr(
        mod,
        "_has_active_agreement",
        lambda p, b: (
            False,
            "agreement lookup failed: AccessDeniedException arn:aws:...",
        ),
    )
    resp = mod.handler(_event(account_id="111111111111"), None)
    assert resp["statusCode"] == 403
    assert "AccessDenied" not in resp["body"]
    assert "arn:aws" not in resp["body"]


def test_token_carries_a_key_id_so_rotation_is_possible(mod, monkeypatch):
    """KMS cannot auto-rotate a SIGN_VERIFY key. Rotation means running two keys
    and trusting both during the overlap, which a verifier cannot do without a
    `kid` — and `kid` cannot be added later without breaking deployed verifiers."""
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-1"))
    resp = mod.handler(_event(account_id="111111111111"), None)
    claims = _claims(resp)
    assert claims["kid"] == mod._SIGNING_KEY_ARN


def test_response_never_contains_key_material(mod, monkeypatch):
    monkeypatch.setattr(mod, "_has_active_agreement", lambda p, b: (True, "agmt-1"))
    resp = mod.handler(_event(account_id="111111111111"), None)
    body = json.loads(resp["body"])
    assert set(body) == {
        "token",
        "signature",
        "signingAlgorithm",
        "expiresAt",
        "freeTier",
    }


def test_allow_listed_account_gets_FULL_capability_not_free_tier(mod, monkeypatch):
    """The allow-list exists to test the PAID path.

    Marking it free-tier would silently give reduced capability and look like the
    bypass was broken. Documented consequence: every entry gets the full paid
    product for free, which is why it must be empty in production.
    """
    monkeypatch.setattr(
        mod,
        "_has_active_agreement",
        lambda p, b: pytest.fail(
            "must not call Marketplace for an allow-listed account"
        ),
    )
    monkeypatch.setattr(mod, "_ALLOWED_ACCOUNTS", {"333333333333"})
    resp = mod.handler(_event(account_id="333333333333"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["freeTier"] is False
    assert _claims(resp)["freeTier"] is False


def test_no_logger_call_receives_token_material():
    """No logger call may be passed token, signature, or claims material.

    Checks the ARGUMENTS of every `logger.*` call by AST, which is a stronger
    guard than a scanner heuristic over message text: it would catch someone
    adding the signed payload to an existing log line whose wording looks
    innocuous. (Semgrep's `logger-credential-disclosure` flagged this file once
    for having the word "token" in a message string — a false positive, since the
    arguments were all identifiers and counters.)
    """
    import ast
    import re

    source = _LAMBDA.read_text(encoding="utf-8")
    # WORD-BOUNDARY match, not substring. `_` is a word character in Python's
    # `re`, so `\btoken\b` correctly does NOT match `_TOKEN_TTL_SECONDS` — a
    # duration constant, which a substring match flagged as a leak. Being precise
    # beats maintaining an allowlist of safe-but-similar names.
    sensitive = re.compile(r"\b(token|signature|payload|claims)\b", re.IGNORECASE)
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            # args[0] is the format string; the rest are the interpolated values.
            for arg in node.args[1:]:
                rendered = ast.unparse(arg)
                if sensitive.search(rendered):
                    offenders.append(f"line {node.lineno}: {rendered}")
    assert not offenders, "logger call(s) may leak token material: " + "; ".join(
        offenders
    )
