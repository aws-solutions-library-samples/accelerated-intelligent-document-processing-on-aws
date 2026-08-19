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


def test_unknown_product_is_404_without_disclosing_the_catalog(mod):
    resp = mod.handler(
        _event(account_id="111111111111", body={"productId": "prod-nope"}), None
    )
    assert resp["statusCode"] == 404
    # Must not leak which products exist.
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
# Seller-side query shape. NOTE: unverified against a live seller account — the
# filter-name fallback exists precisely because we could not test it.
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
    # First attempt uses the name proven to work on the buyer side.
    assert names.get("ResourceIdentifier") == ["prod-paid"]


def test_falls_back_to_alternate_resource_filter_name(mod, monkeypatch):
    """The docs' Proposer list says `ResourceId`; the working buyer-side call uses
    `ResourceIdentifier`. FilterName is free-form and validated server-side only,
    so we try both rather than guessing — and this test pins the fallback."""
    from botocore.exceptions import ClientError

    attempted = []

    class _PickyAgreement:
        def search_agreements(self, **kwargs):
            names = [f["name"] for f in kwargs["filters"]]
            attempted.append(names)
            if "ResourceIdentifier" in names:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": "Provided combination of filters is not supported",
                        }
                    },
                    "SearchAgreements",
                )
            return {"agreementViewSummaries": [{"agreementId": "agmt-fallback"}]}

    monkeypatch.setattr(mod, "_agreement", lambda: _PickyAgreement())
    entitled, detail = mod._has_active_agreement("prod-paid", "111111111111")

    assert entitled is True
    assert "agmt-fallback" in detail
    assert len(attempted) == 2
    assert "ResourceIdentifier" in attempted[0]
    assert "ResourceId" in attempted[1]


def test_both_filter_names_failing_reports_not_entitled(mod, monkeypatch):
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
