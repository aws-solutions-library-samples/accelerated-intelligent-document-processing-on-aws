"""Unit tests for the check_feature_entitlement Lambda.

moto does not implement marketplace-entitlement, so we use botocore.stub.Stubber
to programme the boto3 client inside the module after import. The product code
now comes from the feature's InstalledFeatures row (DynamoDB, via moto) — baked
from the manifest at install — rather than a host env map.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from _helpers import make_appsync_event
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

_CATALOG_KEY = "config_library/catalog.json"


def _seed_row(table_name, feature_id, *, product_code=None):
    item = {"featureId": feature_id}
    if product_code is not None:
        item["productCode"] = product_code
    boto3.resource("dynamodb", region_name="us-east-1").Table(table_name).put_item(
        Item=item
    )


_ENDPOINT_VARS = (
    "AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT",
    "AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE",
    "AWS_ENDPOINT_URL",
)


def _preload(
    monkeypatch,
    load_lambda,
    *,
    table_name="",
    default_customer="CUST-default",
    buyer_account="111122223333",
    source_tag="simulator",
    configuration_bucket="",
    endpoint_override=None,
    endpoint_var="AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT",
    agreement_region=None,
):
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", table_name)
    monkeypatch.setenv("DEFAULT_CUSTOMER_IDENTIFIER", default_customer)
    monkeypatch.setenv("DEFAULT_BUYER_ACCOUNT_ID", buyer_account)
    monkeypatch.setenv("SIMULATOR_SOURCE_TAG", source_tag)
    monkeypatch.setenv("CONFIGURATION_BUCKET", configuration_bucket)
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    # Endpoint overrides decide the REPORTED source, so every test must be
    # explicit about them rather than inheriting the developer's shell.
    for var in _ENDPOINT_VARS:
        monkeypatch.delenv(var, raising=False)
    if endpoint_override is not None:
        monkeypatch.setenv(endpoint_var, endpoint_override)
    if agreement_region is not None:
        monkeypatch.setenv("MARKETPLACE_AGREEMENT_REGION", agreement_region)
    else:
        monkeypatch.delenv("MARKETPLACE_AGREEMENT_REGION", raising=False)
    return load_lambda("check_feature_entitlement")


def _put_catalog(bucket: str, features: list) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket,
        Key=_CATALOG_KEY,
        Body=json.dumps({"schemaVersion": "1.0", "features": features}).encode("utf-8"),
    )


def _stub(
    mod,
    entitlements=None,
    *,
    expected_product="prod123",
    expected_customer="CUST-default",
    expected_account=None,
):
    """Inject a Stubber against the module's boto3 client and seed a response.

    Pass `expected_account` to assert the buyer-account filter
    (CUSTOMER_AWS_ACCOUNT_ID) instead of the CUSTOMER_IDENTIFIER filter.
    """
    client = mod._client()
    stubber = Stubber(client)
    filt = (
        {"CUSTOMER_AWS_ACCOUNT_ID": [expected_account]}
        if expected_account is not None
        else {"CUSTOMER_IDENTIFIER": [expected_customer]}
    )
    stubber.add_response(
        "get_entitlements",
        {"Entitlements": entitlements or []},
        {"ProductCode": expected_product, "Filter": filt},
    )
    stubber.activate()
    return stubber


def test_none_when_no_product_code_marketplace_mode(
    monkeypatch, load_lambda, installed_features_table
):
    """Marketplace mode: a feature whose install row has no productCode → NONE."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result == {
        "featureId": "docs-by-status",
        "state": "NONE",
        "expiresAt": None,
        "customerIdentifier": None,
        "productCode": None,
        "source": "none",
    }


def test_synthesized_product_code_simulator_mode(
    monkeypatch, load_lambda, installed_features_table
):
    """Simulator mode: a row without a productCode uses synthesized prod-<id>-sim
    and calls GetEntitlements against it."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="simulator",
    )
    _stub(
        mod,
        entitlements=[],
        expected_product="prod-docs-by-status-sim",
        expected_customer="CUST-default",
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["productCode"] == "prod-docs-by-status-sim"
    assert result["customerIdentifier"] == "CUST-default"
    assert result["source"] == "simulated"


def test_marketplace_mode_no_customer_identifier_filters_by_buyer_account(
    monkeypatch, load_lambda, installed_features_table
):
    """Marketplace mode: with no CustomerIdentifier, GetEntitlements falls back to
    the buyer AWS account (CUSTOMER_AWS_ACCOUNT_ID) — the same deterministic key
    subscribe uses. The fallback is keyed on DEFAULT_BUYER_ACCOUNT_ID, NOT on
    SOURCE_TAG, because the main stack only ever emits "auto" or "marketplace"
    (never "simulator"), and an endpoint-configured stack points at the simulator
    while tagged "marketplace". An account with no subscription → empty → NONE."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="",
        buyer_account="111122223333",
        source_tag="marketplace",
    )
    _stub(
        mod,
        entitlements=[],
        expected_product="prod123",
        expected_account="111122223333",
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["customerIdentifier"] is None
    assert result["productCode"] == "prod123"
    assert result["source"] == "simulated"


def test_missing_customer_identifier_filters_by_buyer_account_simulator_mode(
    monkeypatch, load_lambda, installed_features_table
):
    """Simulator mode: with no CustomerIdentifier, GetEntitlements is filtered by
    the buyer AWS account (the deterministic key shared with subscribe) — NOT a
    synthesized customer id. The resolved customer id is echoed from the matched
    entitlement."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="",
        buyer_account="111122223333",
        source_tag="simulator",
    )
    future = datetime.now(timezone.utc) + timedelta(days=30)
    _stub(
        mod,
        entitlements=[
            {
                "ProductCode": "prod123",
                "CustomerIdentifier": "cust-62c036d80d5c",
                "ExpirationDate": future,
            }
        ],
        expected_product="prod123",
        expected_account="111122223333",
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "ACTIVE"
    # Customer id is echoed from the matched entitlement (looked up by account).
    assert result["customerIdentifier"] == "cust-62c036d80d5c"
    assert result["productCode"] == "prod123"
    assert result["source"] == "simulated"


def test_active_when_active_entitlement(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    stubber = _stub(
        mod,
        entitlements=[
            {
                "ProductCode": "prod123",
                "Dimension": "USERS",
                "CustomerIdentifier": "CUST-default",
                "ExpirationDate": future,
            }
        ],
    )
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement", {"featureId": "docs-by-status"}
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "ACTIVE"
    assert result["expiresAt"]
    assert result["expiresAt"].endswith("Z")
    assert result["customerIdentifier"] == "CUST-default"
    assert result["productCode"] == "prod123"


def test_expired_when_only_expired_entitlement(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    past = datetime.now(timezone.utc) - timedelta(days=30)
    stubber = _stub(mod, entitlements=[{"ExpirationDate": past}])
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement", {"featureId": "docs-by-status"}
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "EXPIRED"
    assert result["expiresAt"]


def test_active_beats_expired_when_both_present(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    past = datetime.now(timezone.utc) - timedelta(days=30)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    stubber = _stub(
        mod,
        entitlements=[
            {"ExpirationDate": past, "Dimension": "A"},
            {"ExpirationDate": future, "Dimension": "B"},
        ],
    )
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement", {"featureId": "docs-by-status"}
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "ACTIVE"


def test_active_when_no_expiration(monkeypatch, load_lambda, installed_features_table):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    stubber = _stub(mod, entitlements=[{"Dimension": "X"}])
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement", {"featureId": "docs-by-status"}
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "ACTIVE"
    assert result["expiresAt"] is None


def test_none_when_empty_entitlements(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    stubber = _stub(mod, entitlements=[])
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement", {"featureId": "docs-by-status"}
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "NONE"


def test_header_customer_identifier_takes_precedence(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="CUST-default",
    )
    future = datetime.now(timezone.utc) + timedelta(days=30)
    stubber = _stub(
        mod,
        entitlements=[{"ExpirationDate": future}],
        expected_customer="CUST-from-header",
    )
    try:
        result = mod.handler(
            make_appsync_event(
                "checkFeatureEntitlement",
                {"featureId": "docs-by-status"},
                headers={"x-amzn-marketplace-customer-identifier": "CUST-from-header"},
            ),
            None,
        )
    finally:
        stubber.deactivate()
    assert result["state"] == "ACTIVE"
    assert result["customerIdentifier"] == "CUST-from-header"


def test_missing_featureId_raises(monkeypatch, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    with pytest.raises(ValueError, match="featureId"):
        mod.handler(make_appsync_event("checkFeatureEntitlement", {}), None)


def test_auto_mode_returns_active_without_marketplace_call(monkeypatch, load_lambda):
    """Auto-subscribe mode (no simulator, no Marketplace endpoint) short-circuits
    to ACTIVE for every featureId. The boto3 marketplace-entitlement client must
    never be instantiated — that's the contract that lets the stack run with no
    Marketplace credentials."""
    mod = _preload(monkeypatch, load_lambda, source_tag="auto")
    # Sanity: no client created yet at module load.
    assert mod._entitlement_client is None
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result == {
        "featureId": "docs-by-status",
        "state": "ACTIVE",
        "expiresAt": None,
        "customerIdentifier": None,
        "productCode": None,
        "source": "auto",
    }
    # Contract: no boto3 client is constructed in auto mode.
    assert mod._entitlement_client is None


def test_no_table_falls_back_to_none_marketplace_mode(monkeypatch, load_lambda):
    # No InstalledFeatures table configured → no productCode → marketplace NONE.
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name="",
        source_tag="marketplace",
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["source"] == "none"


def test_oss_feature_short_circuits_to_active_marketplace_mode(
    monkeypatch, mock_stack, load_lambda
):
    """OSS catalog features have no Marketplace contract — even with a simulator/
    Marketplace endpoint configured (source_tag=marketplace), they short-circuit
    to ACTIVE so the UI shows the Install prompt, not 'Subscription required'.
    No entitlement client is constructed for the OSS path."""
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.0.0"}],
    )
    # No productCode on the install row — marketplace mode would otherwise be NONE.
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result == {
        "featureId": "docs-by-status",
        "state": "ACTIVE",
        "expiresAt": None,
        "customerIdentifier": None,
        "productCode": None,
        "source": "oss",
    }
    # Contract: the OSS path never touches the marketplace-entitlement client.
    assert mod._entitlement_client is None


def test_marketplace_feature_still_gated_when_catalog_present(
    monkeypatch, mock_stack, load_lambda
):
    """A catalog entry with source=marketplace does NOT short-circuit — the
    entitlement check still runs (here: no productCode on the row → NONE)."""
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [{"featureId": "idp-monitor", "source": "marketplace", "latestVersion": "1.0"}],
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "idp-monitor"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["source"] == "none"


# ---------------------------------------------------------------------------
# Catalog fallback for productCode — makes the NOT-YET-INSTALLED path work.
# ---------------------------------------------------------------------------


def _mp_catalog_entry(**over) -> dict:
    entry = {
        "featureId": "idp-auto-optimizer",
        "displayName": "Auto Optimizer",
        "source": "marketplace",
        "latestVersion": "0.1.0",
        "productCode": "q0k0s3zuuga46hle6fecx547",
        "productId": "prod-a5ee62vs2xa72",
        "marketplaceListingUrl": "https://aws.amazon.com/marketplace/pp/prodview-x",
    }
    entry.update(over)
    return entry


def test_product_code_falls_back_to_catalog_when_not_installed(
    monkeypatch, mock_stack, load_lambda
):
    """Before install there is no InstalledFeatures row — the catalog must serve.

    Previously this returned state=NONE / source="none" even for a subscribed
    customer, so the UI said "no entitlement" with no way forward.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],  # table exists, but NO row seeded
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    # No ExpirationDate → an entitlement with no expiry, i.e. ACTIVE.
    _stub(
        mod,
        entitlements=[{"CustomerIdentifier": "CUST-default"}],
        expected_product="q0k0s3zuuga46hle6fecx547",
    )
    result = mod.handler(
        make_appsync_event(
            "checkFeatureEntitlement", {"featureId": "idp-auto-optimizer"}
        ),
        None,
    )
    assert result["productCode"] == "q0k0s3zuuga46hle6fecx547"
    assert result["state"] == "ACTIVE"


def test_installed_row_still_wins_over_catalog(monkeypatch, mock_stack, load_lambda):
    """The install row is baked from the manifest, so it stays authoritative."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _put_catalog(bucket, [_mp_catalog_entry(productCode="from-catalog")])
    _seed_row(table, "idp-auto-optimizer", product_code="from-install-row")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=table,
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    _stub(mod, entitlements=[], expected_product="from-install-row")
    result = mod.handler(
        make_appsync_event(
            "checkFeatureEntitlement", {"featureId": "idp-auto-optimizer"}
        ),
        None,
    )
    assert result["productCode"] == "from-install-row"


# ---------------------------------------------------------------------------
# marketplace-live: buyer-side AWS Marketplace Agreement API (SearchAgreements).
#
# GetEntitlements cannot serve as the gate — it is seller-side, and a usage-based
# SaaS listing has no entitlement records at all, so from a buyer account it
# returns HTTP 200 with an EMPTY list rather than an error. A fail-closed gate
# built on that silently denies every real customer. Hence SearchAgreements, and
# hence the three-way ACTIVE / NONE / UNKNOWN distinction below.
# ---------------------------------------------------------------------------


def _stub_agreements(
    mod, summaries=None, *, error=None, expected_product="prod-a5ee62vs2xa72"
):
    client = mod._agreement_client()
    stubber = Stubber(client)
    expected_params = {
        "catalog": "AWSMarketplace",
        "filters": [
            {"name": "PartyType", "values": ["Acceptor"]},
            {"name": "AgreementType", "values": ["PurchaseAgreement"]},
            {"name": "ResourceIdentifier", "values": [expected_product]},
            {"name": "Status", "values": ["ACTIVE"]},
        ],
    }
    if error:
        stubber.add_client_error(
            "search_agreements",
            service_error_code=error,
            expected_params=expected_params,
        )
    else:
        stubber.add_response(
            "search_agreements",
            {"agreementViewSummaries": summaries or []},
            expected_params,
        )
    stubber.activate()
    return stubber


def _live_event():
    return make_appsync_event(
        "checkFeatureEntitlement", {"featureId": "idp-auto-optimizer"}
    )


def test_live_active_agreement_is_active(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    end = datetime.now(timezone.utc) + timedelta(days=30)
    _stub_agreements(
        mod,
        [{"agreementId": "agmt-1", "status": "ACTIVE", "endTime": end}],
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "marketplace-live"
    assert result["expiresAt"].startswith(end.isoformat()[:10])


def test_live_open_ended_agreement_has_no_expiry(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _stub_agreements(mod, [{"agreementId": "agmt-1", "status": "ACTIVE"}])
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["expiresAt"] is None


def test_live_empty_result_is_an_authoritative_none(
    monkeypatch, mock_stack, load_lambda
):
    """A SUCCESSFUL empty response really means "not subscribed in this account".

    Unlike GetEntitlements, SearchAgreements is scoped to the caller, so this is
    a real negative and the UI should show Subscribe.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _stub_agreements(mod, [])
    result = mod.handler(_live_event(), None)
    assert result["state"] == "NONE"
    assert result["source"] == "marketplace-live"


def test_live_api_error_degrades_to_advisory_active(
    monkeypatch, mock_stack, load_lambda
):
    """An ERRORED call is indistinguishable from "not subscribed" — so allow.

    Failing closed on a missing IAM grant or an unsupported partition would lock
    a paying customer out of an extension they bought. The extension's own
    runtime entitlement check remains the authoritative gate, so a permissive
    host gate costs nothing.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _stub_agreements(mod, error="AccessDeniedException")
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "advisory"


def test_live_missing_product_id_is_advisory_not_denial(
    monkeypatch, mock_stack, load_lambda
):
    """No productId in the catalog → we cannot check, so don't pretend we did."""
    bucket = mock_stack["bucket"]
    entry = _mp_catalog_entry()
    del entry["productId"]
    _put_catalog(bucket, [entry])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "advisory"


def test_live_mode_still_short_circuits_oss(monkeypatch, mock_stack, load_lambda):
    """The OSS path must not be affected by any of this."""
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.0.0"}],
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "ACTIVE"
    assert result["source"] == "oss"


def test_auto_mode_unchanged_by_live_support(monkeypatch, mock_stack, load_lambda):
    """`auto` must remain a zero-API-call short circuit."""
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
    )
    result = mod.handler(_live_event(), None)
    assert result == {
        "featureId": "idp-auto-optimizer",
        "state": "ACTIVE",
        "expiresAt": None,
        "customerIdentifier": None,
        "productCode": None,
        "source": "auto",
    }


# ---------------------------------------------------------------------------
# Unverified-grant telemetry. `auto` and `advisory` both hand out access to a
# PAID extension without confirming a subscription, and both are invisible in
# the product — the page looks exactly like a real subscription. The metric is
# the operator-side signal that it is happening.
#
# NB this is CUSTOMER-side observability (it lands in the customer's own
# CloudWatch), not seller-side revenue protection. It exists so an admin can
# see that their stack isn't verifying subscriptions — typically a missing
# aws-marketplace:SearchAgreements permission.
# ---------------------------------------------------------------------------


def _capture_metrics(monkeypatch, mod) -> list:
    emitted: list = []
    monkeypatch.setattr(
        mod,
        "_emit_unverified_grant_metric",
        lambda feature_id, source: emitted.append((feature_id, source)),
    )
    return emitted


def test_auto_mode_emits_metric_for_paid_feature(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
        configuration_bucket=bucket,
    )
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)

    assert result["source"] == "auto"
    assert result["state"] == "ACTIVE"
    assert emitted == [("idp-auto-optimizer", "auto")]


def test_auto_mode_does_not_emit_for_oss_feature(monkeypatch, mock_stack, load_lambda):
    """OSS extensions have no subscription to verify — warning would be noise.

    Also pins the ordering invariant: an OSS extension reports `oss` even in `auto`
    mode. Being open-source is a property of the extension, so the deployment mode
    must not be able to relabel it — otherwise `oss` is not a dependable signal for
    "this is not a paid extension".
    """
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.0.0"}],
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
        configuration_bucket=bucket,
    )
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["source"] == "oss"
    assert emitted == []


def test_advisory_emits_metric(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _stub_agreements(mod, error="AccessDeniedException")
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)

    assert result["source"] == "advisory"
    assert emitted == [("idp-auto-optimizer", "advisory")]


def test_verified_active_emits_no_metric(monkeypatch, mock_stack, load_lambda):
    """A genuinely confirmed subscription is not an unverified grant."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _stub_agreements(mod, [{"agreementId": "agmt-1", "status": "ACTIVE"}])
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)

    assert result["state"] == "ACTIVE"
    assert result["source"] == "marketplace-live"
    assert emitted == []


def test_metric_payload_is_valid_emf(monkeypatch, mock_stack, load_lambda, caplog):
    """EMF needs `_aws.Timestamp` + CloudWatchMetrics, and the dimension values
    must be present as top-level members. A record missing any of these is
    ingested as a plain log line and silently produces NO metric — the worst
    outcome for a signal whose whole purpose is to be noticed."""
    import logging as _logging

    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
    )
    with caplog.at_level(_logging.INFO):
        mod._emit_unverified_grant_metric("idp-auto-optimizer", "advisory")

    emf_records = []
    for rec in caplog.messages:
        try:
            parsed = json.loads(rec)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "_aws" in parsed:
            emf_records.append(parsed)

    assert len(emf_records) == 1, "expected exactly one EMF record"
    payload = emf_records[0]
    aws_meta = payload["_aws"]
    assert isinstance(aws_meta["Timestamp"], int) and aws_meta["Timestamp"] > 0
    (metric_directive,) = aws_meta["CloudWatchMetrics"]
    assert metric_directive["Namespace"] == "GENAIDP"
    assert metric_directive["Metrics"] == [
        {"Name": "UnverifiedEntitlementGrant", "Unit": "Count"}
    ]
    # Every declared dimension must exist as a top-level member.
    for dimension_set in metric_directive["Dimensions"]:
        for dim in dimension_set:
            assert dim in payload, f"dimension {dim} missing from EMF payload"
    assert payload["UnverifiedEntitlementGrant"] == 1
    assert payload["EntitlementSource"] == "advisory"


def test_metric_emission_never_raises(monkeypatch, mock_stack, load_lambda):
    """Telemetry must not be able to break the query it instruments."""
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
    )

    def _boom(*_a, **_k):
        raise RuntimeError("logging backend exploded")

    monkeypatch.setattr(mod.logger, "info", _boom)
    # Must swallow, not propagate.
    mod._emit_unverified_grant_metric("f", "auto")


def test_simulator_backed_active_emits_metric(monkeypatch, mock_stack, load_lambda):
    """A simulator/endpoint-override ACTIVE is not a real subscription check.

    boto3 was pointed at whatever AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE
    names, so a production host aimed at a simulator would otherwise render a
    clean "subscription active" with nothing recorded anywhere.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    _stub(
        mod,
        entitlements=[{"CustomerIdentifier": "CUST-default"}],
        expected_product="q0k0s3zuuga46hle6fecx547",
    )
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)

    assert result["state"] == "ACTIVE"
    # The metric records the REPORTED source, so dashboards agree with what the
    # UI and extensions see. `marketplace` mode reports `simulated`.
    assert emitted == [("idp-auto-optimizer", "simulated")]


def test_simulator_backed_none_emits_nothing(monkeypatch, mock_stack, load_lambda):
    """Only a GRANT is an unverified grant; a refusal needs no warning."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    _stub(mod, entitlements=[], expected_product="q0k0s3zuuga46hle6fecx547")
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)

    assert result["state"] == "NONE"
    assert emitted == []


# ---------------------------------------------------------------------------
# Reported source is normalized, and is independent of deployment mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["simulator", "marketplace"])
def test_seller_side_modes_both_report_simulated(
    monkeypatch, mock_stack, load_lambda, mode
):
    """`simulator` and `marketplace` are one reported source, `simulated`.

    They are the same code path — the seller-side GetEntitlements API, which
    returns 200-with-an-empty-list from a buyer account and so cannot verify
    anything against real AWS. Reporting the deployment mode verbatim leaked a
    distinction no consumer can act on, and made `marketplace` (the weakest
    source) read as more authoritative than `marketplace-live`.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag=mode,
        configuration_bucket=bucket,
    )
    _stub(
        mod,
        entitlements=[{"CustomerIdentifier": "CUST-default"}],
        expected_product="q0k0s3zuuga46hle6fecx547",
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "simulated", (
        f"mode {mode!r} must report 'simulated', not the mode name"
    )


@pytest.mark.parametrize(
    "mode", ["auto", "simulator", "marketplace", "marketplace-live"]
)
def test_oss_reports_oss_in_every_mode(monkeypatch, mock_stack, load_lambda, mode):
    """Being open-source is a property of the extension, not the deployment.

    `auto` mode used to be evaluated first and relabelled OSS extensions as
    `auto`, so an extension could not rely on `oss` meaning "not a paid
    extension". No Marketplace call is made in any mode, so no stub is needed —
    if one were attempted the test would error rather than pass.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.0.0"}],
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag=mode,
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "ACTIVE"
    assert result["source"] == "oss", f"mode {mode!r} relabelled an OSS extension"


SIMULATOR_ENDPOINT = "https://simulator.example.invalid"


def _filters(product, *, party_type=True):
    """The expected SearchAgreements filter list, mirroring the resolver."""
    filters = []
    if party_type:
        filters.append({"name": "PartyType", "values": ["Acceptor"]})
    filters.extend(
        [
            {"name": "AgreementType", "values": ["PurchaseAgreement"]},
            {"name": "ResourceIdentifier", "values": [product]},
            {"name": "Status", "values": ["ACTIVE"]},
        ]
    )
    return filters


def _queue_agreements(mod, calls):
    """Queue an ordered list of SearchAgreements outcomes on the module's client.

    Each entry is ``(product, party_type, summaries_or_error_code)``. Stubber
    asserts the exact request for every call, so the queue pins BOTH the number
    of calls and the filter set each one used — which is the point: the
    production query must not change.
    """
    stubber = Stubber(mod._agreement_client())
    for product, party_type, outcome in calls:
        expected = {
            "catalog": "AWSMarketplace",
            "filters": _filters(product, party_type=party_type),
        }
        if isinstance(outcome, str):
            stubber.add_client_error(
                "search_agreements",
                service_error_code=outcome,
                expected_params=expected,
            )
        else:
            stubber.add_response(
                "search_agreements",
                {"agreementViewSummaries": outcome},
                expected,
            )
    stubber.activate()
    return stubber


def _live_mod(monkeypatch, mock_stack, load_lambda, **kw):
    _put_catalog(mock_stack["bucket"], [_mp_catalog_entry()])
    return _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=mock_stack["bucket"],
        **kw,
    )


# ---------------------------------------------------------------------------
# THE load-bearing invariant: an endpoint-overridden deployment cannot produce
# a VERIFIED entitlement.
#
# `marketplace-live` is the only source `isVerifiedEntitlement()` accepts and the
# only one extension authors are told to trust (`entitlementVerified`). It used
# to be copied straight from the SubscriptionMode parameter, so a stack whose
# Marketplace endpoints were aimed at a simulator reported simulator answers as a
# verified live Marketplace check — an extension following the documented advice
# was silently fooled by a fake Marketplace. The source is now DERIVED from the
# endpoint, which is what makes the claim unforgeable by configuration.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("var", _ENDPOINT_VARS)
def test_endpoint_override_never_reports_marketplace_live(
    monkeypatch, load_lambda, var
):
    """Each of the three override vars downgrades the reported source.

    Covers `AWS_ENDPOINT_URL` too — the global botocore override redirects the
    Agreement API just as effectively as the service-specific one, so reading
    only the service-specific vars would leave a hole.
    """
    mod = _preload(
        monkeypatch,
        load_lambda,
        source_tag="marketplace-live",
        endpoint_override=SIMULATOR_ENDPOINT,
        endpoint_var=var,
    )
    assert mod._ENDPOINT_OVERRIDE == SIMULATOR_ENDPOINT
    assert mod._REPORTED_SOURCE == "simulated", (
        f"{var} pointed boto3 at a simulator but the host still reported "
        f"{mod._REPORTED_SOURCE!r}"
    )


@pytest.mark.parametrize(
    "tag,override,expected",
    [
        ("marketplace-live", "", "marketplace-live"),
        ("marketplace-live", SIMULATOR_ENDPOINT, "simulated"),
        ("marketplace", "", "simulated"),
        ("marketplace", SIMULATOR_ENDPOINT, "simulated"),
        ("simulator", SIMULATOR_ENDPOINT, "simulated"),
        ("auto", "", "auto"),
        ("auto", SIMULATOR_ENDPOINT, "auto"),
    ],
)
def test_reported_source_matrix(monkeypatch, load_lambda, tag, override, expected):
    """The full (mode x endpoint) matrix, in one place.

    `auto` keeps its own source even with an endpoint set: it makes no API call
    at all, so "simulated" would be a lie in the other direction — and `auto` is
    already an unverified source.
    """
    mod = _preload(monkeypatch, load_lambda, source_tag="marketplace-live")
    assert mod._reported_source(tag, override) == expected


def test_empty_endpoint_env_var_is_not_an_override(monkeypatch, load_lambda):
    """The template ALWAYS sets these vars — to '' when no simulator is used.

    So presence must not count; only a non-empty value does. Reading presence
    would report every production stack as `simulated` and make the verified
    source unreachable.
    """
    mod = _preload(
        monkeypatch,
        load_lambda,
        source_tag="marketplace-live",
        endpoint_override="",  # exactly what CloudFormation emits by default
    )
    assert mod._ENDPOINT_OVERRIDE == ""
    assert mod._REPORTED_SOURCE == "marketplace-live"


def test_simulator_backed_active_agreement_reports_simulated(
    monkeypatch, mock_stack, load_lambda
):
    """End-to-end: mode=marketplace-live + simulator endpoint + an ACTIVE
    agreement → state ACTIVE but source `simulated`, so
    `isVerifiedEntitlement()` is false and the UI raises the unverified banner."""
    mod = _live_mod(
        monkeypatch, mock_stack, load_lambda, endpoint_override=SIMULATOR_ENDPOINT
    )
    _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, [{"agreementId": "a", "status": "ACTIVE"}])]
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "simulated"
    assert result["source"] != "marketplace-live"


def test_simulator_backed_none_reports_simulated(monkeypatch, mock_stack, load_lambda):
    """The NONE branch reported the live tag from its own literal — fix both."""
    mod = _live_mod(
        monkeypatch, mock_stack, load_lambda, endpoint_override=SIMULATOR_ENDPOINT
    )
    # Neither identifier matches — the honest simulator "not subscribed" case.
    _queue_agreements(
        mod,
        [("prod-a5ee62vs2xa72", True, []), ("q0k0s3zuuga46hle6fecx547", True, [])],
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "NONE"
    assert result["source"] == "simulated"


def test_simulator_backed_grant_emits_unverified_metric(
    monkeypatch, mock_stack, load_lambda
):
    """A simulator-backed ACTIVE on the live path is an unverified grant, and the
    operator-side metric must see it — it previously looked like a clean verified
    subscription and recorded nothing."""
    mod = _live_mod(
        monkeypatch, mock_stack, load_lambda, endpoint_override=SIMULATOR_ENDPOINT
    )
    _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, [{"agreementId": "a", "status": "ACTIVE"}])]
    )
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert emitted == [("idp-auto-optimizer", "simulated")]


def test_real_aws_active_still_verified_and_silent(
    monkeypatch, mock_stack, load_lambda
):
    """The production path is unchanged: no override → `marketplace-live`, no
    metric, one call, canonical four-filter query."""
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, [{"agreementId": "a", "status": "ACTIVE"}])]
    )
    emitted = _capture_metrics(monkeypatch, mod)
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "marketplace-live"
    assert emitted == []


# ---------------------------------------------------------------------------
# Simulator compatibility for the buyer-side query.
#
# Root cause of the live incident: the simulator implements a SUBSET of the API
# and rejects `PartyType` outright (`ValidationException: unknown filter name:
# PartyType`), so every check on a simulator-backed stack degraded to `advisory`.
# It also records agreements under the product CODE, not the product ENTITY id,
# because its buyer console is keyed on productCode.
#
# Both accommodations are gated on an endpoint override being in effect, because
# real AWS accepts PartyType and REJECTS the reduced filter set
# (`ValidationException: Provided combination of filters is not supported`) —
# verified against a live account. So the production query can't be weakened.
# ---------------------------------------------------------------------------


def test_simulator_partytype_rejection_retries_without_it(
    monkeypatch, mock_stack, load_lambda
):
    mod = _live_mod(
        monkeypatch, mock_stack, load_lambda, endpoint_override=SIMULATOR_ENDPOINT
    )
    _queue_agreements(
        mod,
        [
            ("prod-a5ee62vs2xa72", True, "ValidationException"),
            (
                "prod-a5ee62vs2xa72",
                False,
                [{"agreementId": "a", "status": "ACTIVE"}],
            ),
        ],
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    # Still not a verified source — the retry doesn't launder the answer.
    assert result["source"] == "simulated"


def test_simulator_falls_back_to_product_code(monkeypatch, mock_stack, load_lambda):
    """productId matches nothing on the simulator; the productCode does."""
    mod = _live_mod(
        monkeypatch, mock_stack, load_lambda, endpoint_override=SIMULATOR_ENDPOINT
    )
    _queue_agreements(
        mod,
        [
            ("prod-a5ee62vs2xa72", True, []),
            (
                "q0k0s3zuuga46hle6fecx547",
                True,
                [{"agreementId": "a", "status": "ACTIVE"}],
            ),
        ],
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "simulated"


def test_real_aws_does_not_retry_on_validation_error(
    monkeypatch, mock_stack, load_lambda
):
    """No override → no relaxed retry. A single canonical call, then advisory.

    Retrying against real AWS would drop `PartyType` from the one filter
    combination AWS accepts, and the reduced query is rejected there anyway —
    so a retry could only ever hide the real error.
    """
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    stubber = _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, "ValidationException")]
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "advisory"
    # Nothing left unconsumed → exactly one call was made.
    stubber.assert_no_pending_responses()


def test_real_aws_does_not_fall_back_to_product_code(
    monkeypatch, mock_stack, load_lambda
):
    """On real AWS, `ResourceIdentifier` is the product ENTITY id, full stop. An
    authoritative empty result must stay NONE rather than being re-queried under
    an id the API doesn't index."""
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    stubber = _queue_agreements(mod, [("prod-a5ee62vs2xa72", True, [])])
    result = mod.handler(_live_event(), None)
    assert result["state"] == "NONE"
    assert result["source"] == "marketplace-live"
    stubber.assert_no_pending_responses()


def test_access_denied_and_unreachable_get_different_diagnostics(
    monkeypatch, mock_stack, load_lambda
):
    """ "Missing permission" and "wrong Region" have different fixes.

    Collapsing them sent an operator who had merely set
    MARKETPLACE_AGREEMENT_REGION to their own Region hunting for an IAM grant
    they already had.
    """
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "SearchAgreements",
    )
    assert "ACCESS DENIED" in mod._diagnose_agreement_failure(denied)
    unreachable = EndpointConnectionError(endpoint_url="https://x.invalid")
    assert "UNREACHABLE" in mod._diagnose_agreement_failure(unreachable)


def test_bad_agreement_region_warns_at_cold_start(
    monkeypatch, mock_stack, load_lambda, caplog
):
    """us-west-2 has no Agreement API endpoint — verified against the SDK's own
    endpoint data. The parameter is operator-settable, so say so loudly instead
    of leaving a permanent `advisory` that blames IAM."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        _live_mod(monkeypatch, mock_stack, load_lambda, agreement_region="us-west-2")
    assert any(
        "MARKETPLACE_AGREEMENT_REGION" in m and "us-west-2" in m
        for m in caplog.messages
    ), caplog.messages


def test_unknown_catalog_entry_is_not_treated_as_oss(
    monkeypatch, mock_stack, load_lambda
):
    """A catalog entry that is absent or unreadable must NOT short-circuit as OSS.

    `is_marketplace_feature` is falsy both for a confirmed OSS extension and for
    an unknown one, so keying the short-circuit off it would grant access to a
    paid extension whose catalog entry merely failed to load — skipping the
    entitlement check entirely. Unknown must fall through to the check.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [])  # feature is NOT in the catalog
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event(
            "checkFeatureEntitlement", {"featureId": "idp-auto-optimizer"}
        ),
        None,
    )
    assert result["source"] != "oss", (
        "an unknown catalog entry was treated as OSS — that grants a paid "
        "extension access without any entitlement check"
    )
