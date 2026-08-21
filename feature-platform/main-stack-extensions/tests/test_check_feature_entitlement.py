"""Unit tests for the check_feature_entitlement Lambda.

moto does not implement marketplace-entitlement, so we use botocore.stub.Stubber
to programme the boto3 client inside the module after import. The product code
now comes from the feature's InstalledFeatures row (DynamoDB, via moto) — baked
from the manifest at install — rather than a host env map.

The authority is PER EXTENSION (`licenseMode`), so most fixtures below take one:
`_mp_catalog_entry()` declares `marketplace-live` (what publish.py bakes for a
listed product) and tests that want the simulator dev loop pass
`licenseMode="simulated"`. Clients are cached per (service, endpoint), and the
Stubber is attached to the client the handler will actually reuse — which is also
what makes the mixed-mode leak test meaningful.
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

# A stand-in for the marketplace-simulator. `.invalid` is reserved by RFC 2606, so
# a stub that leaks into a real request fails loudly instead of reaching anything.
SIMULATOR_ENDPOINT = "https://simulator.example.invalid"


def _seed_row(table_name, feature_id, *, product_code=None, license_mode=None):
    item = {"featureId": feature_id}
    if product_code is not None:
        item["productCode"] = product_code
    # What the EXTENSION declares it enforces, propagated through registerFeature.
    if license_mode is not None:
        item["licenseMode"] = license_mode
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
    simulator_endpoint=SIMULATOR_ENDPOINT,
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
    # WHERE the simulator lives — stack-scoped. Separate from WHICH authority an
    # extension uses, which is the whole point of the per-extension licenseMode.
    monkeypatch.setenv("MARKETPLACE_SIMULATOR_ENDPOINT", simulator_endpoint or "")
    if agreement_region is not None:
        monkeypatch.setenv("MARKETPLACE_AGREEMENT_REGION", agreement_region)
    else:
        monkeypatch.delenv("MARKETPLACE_AGREEMENT_REGION", raising=False)
    return load_lambda("check_feature_entitlement")


def _authority(mod, mode):
    """The authority a feature with this licenseMode resolves to.

    Tests use it to reach the exact client the handler will reuse — clients are
    cached per (service, endpoint), so stubbing the authority's client is
    stubbing the handler's client.
    """
    return mod._authority_for_mode(mode, "test")


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
    client = mod._entitlement_client(_authority(mod, "simulated"))
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


def test_no_product_code_synthesises_one_for_a_simulator_authority(
    monkeypatch, load_lambda, installed_features_table
):
    """A simulator authority with no productCode anywhere synthesises one.

    Reaching the seller-side path at all means the authority is simulator-backed,
    and `prod-<id>-sim` is the key the simulator's own subscribe flow uses. This
    used to be gated on the stack-wide tag being literally "simulator", which the
    main stack never emitted, so the branch was dead and every such check returned
    NONE/source="none". The authority now says so directly.
    """
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
    )
    _stub(mod, entitlements=[], expected_product="prod-docs-by-status-sim")
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["productCode"] == "prod-docs-by-status-sim"
    assert result["source"] == "simulated"
    assert result["licenseMode"] == "simulated"


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
    assert mod._clients == {}
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "ACTIVE"
    assert result["source"] == "auto"
    assert result["licenseMode"] == "none"
    assert result["productCode"] is None
    # Contract: no marketplace client is constructed when checks are off.
    assert mod._clients == {}


def test_no_table_still_answers_from_the_resolved_authority(monkeypatch, load_lambda):
    """No InstalledFeatures table and no catalog → the legacy stack setting.

    The answer still comes from a named authority rather than the old
    source="none", which said "no productCode registered" and so conflated a
    missing identity with a missing subscription.
    """
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name="",
        source_tag="marketplace",
    )
    _stub(mod, entitlements=[], expected_product="prod-docs-by-status-sim")
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "docs-by-status"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["source"] == "simulated"


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
    assert result["state"] == "ACTIVE"
    assert result["source"] == "oss"
    assert result["licenseMode"] == "none"
    # Contract: the OSS path never touches a marketplace client.
    assert mod._clients == {}


def test_marketplace_feature_still_gated_when_catalog_present(
    monkeypatch, mock_stack, load_lambda
):
    """A catalog entry with source=marketplace does NOT short-circuit — the
    entitlement check still runs against the resolved authority."""
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [
            {
                "featureId": "idp-monitor",
                "source": "marketplace",
                "latestVersion": "1.0",
                "licenseMode": "simulated",
            }
        ],
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace",
        configuration_bucket=bucket,
    )
    _stub(mod, entitlements=[], expected_product="prod-idp-monitor-sim")
    result = mod.handler(
        make_appsync_event("checkFeatureEntitlement", {"featureId": "idp-monitor"}),
        None,
    )
    assert result["state"] == "NONE"
    assert result["source"] == "simulated"


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
        # What publish.py bakes for a listed product. Tests that want the
        # simulator dev loop override it with licenseMode="simulated".
        "licenseMode": "marketplace-live",
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
    _put_catalog(
        bucket, [_mp_catalog_entry(productCode="from-catalog", licenseMode="simulated")]
    )
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
    client = mod._agreement_client(_authority(mod, "marketplace-live"))
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
    assert result["state"] == "ACTIVE"
    assert result["source"] == "auto"
    assert result["licenseMode"] == "none"


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
    _put_catalog(bucket, [_mp_catalog_entry(licenseMode="simulated")])
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
    _put_catalog(bucket, [_mp_catalog_entry(licenseMode="simulated")])
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
# Reported source is derived from the AUTHORITY, per extension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["simulator", "marketplace"])
def test_legacy_seller_side_modes_both_report_simulated(
    monkeypatch, mock_stack, load_lambda, mode
):
    """The two legacy seller-side stack modes are one reported source.

    They are the same code path — GetEntitlements, which returns
    200-with-an-empty-list from a buyer account and so cannot verify anything
    against real AWS. Reporting the deployment mode verbatim leaked a distinction
    no consumer can act on, and made `marketplace` (the weakest source) read as
    more authoritative than `marketplace-live`. Reached here through the migration
    chain: the catalog entry declares no licenseMode, so step 3 applies.
    """
    bucket = mock_stack["bucket"]
    entry = _mp_catalog_entry()
    del entry["licenseMode"]  # a catalog published before licenseMode existed
    _put_catalog(bucket, [entry])
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
    assert result["licenseMode"] == "simulated"


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


def _queue_agreements(mod, calls, mode="marketplace-live"):
    """Queue an ordered list of SearchAgreements outcomes on ONE authority's client.

    Each entry is ``(product, party_type, summaries_or_error_code)``. Stubber
    asserts the exact request for every call, so the queue pins BOTH the number
    of calls and the filter set each one used — which is the point: the production
    query must not change, and must not be relaxed by a sibling extension
    resolving against the simulator.

    `mode` selects WHICH authority's client is stubbed. Stubbing the live client
    while the handler calls the simulator (or vice versa) shows up as an
    unconsumed stub plus a failing real call, so the pairing is load-bearing.
    """
    stubber = Stubber(mod._agreement_client(_authority(mod, mode)))
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
# THE load-bearing invariant, re-anchored PER REQUEST.
#
# `marketplace-live` is the only source `isVerifiedEntitlement()` accepts and the
# only one extension authors are told to trust (`entitlementVerified`). It used to
# be copied from the SubscriptionMode parameter, so a stack whose Marketplace
# endpoints pointed at a simulator reported simulator answers as a verified live
# check. It is now derived from the endpoint THIS CALL used, so it survives a
# stack that resolves different extensions against different authorities.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["none", "simulated", "marketplace-live"])
def test_only_a_real_aws_call_can_report_marketplace_live(
    monkeypatch, load_lambda, mode
):
    """Across every license mode, `marketplace-live` implies endpoint=None."""
    mod = _preload(monkeypatch, load_lambda, source_tag="marketplace-live")
    authority = _authority(mod, mode)
    source = mod._reported_source(authority)
    if source == "marketplace-live":
        assert authority.endpoint is None, (
            f"mode {mode!r} reported the verified source while aimed at "
            f"{authority.endpoint!r}"
        )
    else:
        assert source in ("simulated", "auto")


def test_live_authority_ignores_the_stacks_simulator_endpoint(monkeypatch, load_lambda):
    """A configured simulator must not capture an extension that declares live.

    This is the whole point of the change: the stack says WHERE the simulator is,
    the extension says WHETHER it uses it. Previously the endpoint was read from
    per-function env vars, so a configured simulator captured every extension.
    """
    mod = _preload(
        monkeypatch,
        load_lambda,
        source_tag="marketplace-live",
        simulator_endpoint=SIMULATOR_ENDPOINT,
    )
    live = _authority(mod, "marketplace-live")
    assert live.endpoint is None
    assert mod._reported_source(live) == "marketplace-live"
    # ...while a sibling extension in simulated mode still reaches the simulator.
    assert _authority(mod, "simulated").endpoint == SIMULATOR_ENDPOINT


@pytest.mark.parametrize("var", _ENDPOINT_VARS)
def test_legacy_endpoint_env_vars_are_popped_not_obeyed(monkeypatch, load_lambda, var):
    """The env-var mechanism is gone — it is what forced stack-wide behaviour.

    Environment variables are per-function, so while boto3 read the endpoint from
    the environment a single resolver could only ever be aimed at one authority.
    They are now popped at import so "no explicit endpoint" genuinely means real
    AWS, and survive only as a fallback for the simulator's location.
    """
    import os

    mod = _preload(
        monkeypatch,
        load_lambda,
        source_tag="marketplace-live",
        endpoint_override=SIMULATOR_ENDPOINT,
        endpoint_var=var,
        simulator_endpoint="",  # force the legacy value to be the only source
    )
    assert os.environ.get(var) is None, f"{var} was left in the environment"
    assert mod._LEGACY_ENDPOINT_OVERRIDE == SIMULATOR_ENDPOINT
    # Used only for WHERE the simulator is...
    assert _authority(mod, "simulated").endpoint == SIMULATOR_ENDPOINT
    # ...never to capture a live extension.
    assert _authority(mod, "marketplace-live").endpoint is None


def test_empty_endpoint_env_var_is_not_an_override(monkeypatch, load_lambda):
    """The template sets these vars to '' when no simulator is configured.

    Presence must not count; only a non-empty value does. Reading presence would
    make every production stack look simulator-backed.
    """
    mod = _preload(
        monkeypatch,
        load_lambda,
        source_tag="marketplace-live",
        endpoint_override="",
        simulator_endpoint="",
    )
    assert mod._LEGACY_ENDPOINT_OVERRIDE == ""
    assert mod._SIMULATOR_ENDPOINT == ""


# ---------------------------------------------------------------------------
# MIXED-MODE STACK — the deliverable this whole change exists for.
#
# One stack, one warm Lambda process, two extensions: a listed one confirmed
# against real AWS Marketplace and an in-development one checked against the
# simulator. Each must report its own source, and neither may leak the other's
# authority — which is a real risk now that clients are cached.
# ---------------------------------------------------------------------------


def _mixed_mod(monkeypatch, mock_stack, load_lambda):
    _put_catalog(
        mock_stack["bucket"],
        [
            # Listed and published → real AWS Marketplace.
            _mp_catalog_entry(),
            # Still in development → the simulator.
            _mp_catalog_entry(
                featureId="idp-dev-extension",
                displayName="Dev Extension",
                productCode="dev-product-code",
                productId="prod-dev-extension",
                licenseMode="simulated",
            ),
            # No Marketplace contract at all.
            {"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.0.0"},
        ],
    )
    return _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=mock_stack["bucket"],
        simulator_endpoint=SIMULATOR_ENDPOINT,
    )


def _event(feature_id):
    return make_appsync_event("checkFeatureEntitlement", {"featureId": feature_id})


def test_mixed_mode_stack_resolves_each_extension_against_its_own_authority(
    monkeypatch, mock_stack, load_lambda
):
    mod = _mixed_mod(monkeypatch, mock_stack, load_lambda)

    # The listed extension: buyer-side SearchAgreements against real AWS. Stubbing
    # the LIVE authority's client is what proves the call went there — a stub on
    # the simulator client would go unconsumed and the real call would fail.
    live_stub = _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, [{"agreementId": "a", "status": "ACTIVE"}])]
    )
    live = mod.handler(_event("idp-auto-optimizer"), None)
    live_stub.assert_no_pending_responses()

    # The in-development extension: seller-side GetEntitlements against the
    # simulator, in the SAME warm process.
    _stub(
        mod,
        entitlements=[{"CustomerIdentifier": "CUST-default"}],
        expected_product="dev-product-code",
    )
    dev = mod.handler(_event("idp-dev-extension"), None)

    oss = mod.handler(_event("docs-by-status"), None)

    assert live["state"] == "ACTIVE"
    assert live["source"] == "marketplace-live", (
        "the listed extension lost its authority"
    )
    assert live["licenseMode"] == "marketplace-live"

    assert dev["state"] == "ACTIVE"
    assert dev["source"] == "simulated", "the dev extension was answered by real AWS"
    assert dev["licenseMode"] == "simulated"

    assert oss["source"] == "oss"

    # No leakage in either direction, and both authorities really were used.
    endpoints = {endpoint for (_svc, endpoint, _region) in mod._clients}
    assert endpoints == {None, SIMULATOR_ENDPOINT}


def test_mixed_mode_simulated_sibling_cannot_relax_the_live_query(
    monkeypatch, mock_stack, load_lambda
):
    """The simulator accommodations must not follow a live extension.

    Resolving the simulated extension first sets up the state that used to be
    process-wide. The live query must still be the canonical four-filter form —
    `_queue_agreements` asserts the exact request, so a relaxed one fails here.
    """
    mod = _mixed_mod(monkeypatch, mock_stack, load_lambda)
    _stub(mod, entitlements=[], expected_product="dev-product-code")
    assert mod.handler(_event("idp-dev-extension"), None)["source"] == "simulated"

    stub = _queue_agreements(mod, [("prod-a5ee62vs2xa72", True, [])])
    live = mod.handler(_event("idp-auto-optimizer"), None)
    stub.assert_no_pending_responses()
    assert live["state"] == "NONE"
    assert live["source"] == "marketplace-live"


# ---------------------------------------------------------------------------
# Resolution chain: row → catalog → legacy stack setting → hard default.
# ---------------------------------------------------------------------------


def test_install_row_license_mode_wins_over_the_catalog(
    monkeypatch, mock_stack, load_lambda
):
    """Step 1. The extension's own declaration is what it actually enforces.

    This is the reported bug, in the shape that matters: the catalog says
    `simulated` but the installed extension honours real Marketplace. Aligning the
    host with the extension is the only way the two gates can agree.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry(licenseMode="simulated")])
    _seed_row(
        mock_stack["table_name"],
        "idp-auto-optimizer",
        product_code="q0k0s3zuuga46hle6fecx547",
        license_mode="marketplace-live",
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    stub = _queue_agreements(mod, [("prod-a5ee62vs2xa72", True, [])])
    result = mod.handler(_live_event(), None)
    stub.assert_no_pending_responses()

    assert result["licenseMode"] == "marketplace-live"
    assert result["source"] == "marketplace-live"
    assert result["licenseModeMismatch"] is True
    assert result["declaredLicenseMode"] == "marketplace-live"
    assert result["catalogLicenseMode"] == "simulated"


def test_agreeing_declarations_are_not_a_mismatch(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    _seed_row(
        mock_stack["table_name"],
        "idp-auto-optimizer",
        product_code="q0k0s3zuuga46hle6fecx547",
        license_mode="marketplace-live",
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    _queue_agreements(mod, [("prod-a5ee62vs2xa72", True, [])])
    result = mod.handler(_live_event(), None)
    assert result["licenseModeMismatch"] is False


def test_catalog_license_mode_used_when_the_row_is_silent(
    monkeypatch, mock_stack, load_lambda
):
    """Step 2 — and the not-yet-installed path, where there is no row at all."""
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    _queue_agreements(mod, [("prod-a5ee62vs2xa72", True, [])])
    result = mod.handler(_live_event(), None)
    assert result["licenseMode"] == "marketplace-live"
    assert result["declaredLicenseMode"] is None
    assert result["licenseModeMismatch"] is False


@pytest.mark.parametrize(
    "legacy_mode,expected_mode",
    [("marketplace", "simulated"), ("simulator", "simulated"), ("auto", "none")],
)
def test_legacy_stack_setting_is_step_three(
    monkeypatch, mock_stack, load_lambda, legacy_mode, expected_mode
):
    """Step 3 keeps a stack working whose catalog predates `licenseMode`."""
    bucket = mock_stack["bucket"]
    entry = _mp_catalog_entry()
    del entry["licenseMode"]
    _put_catalog(bucket, [entry])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag=legacy_mode,
        configuration_bucket=bucket,
    )
    authority, declared, from_catalog = mod._resolve_authority(
        "idp-auto-optimizer", {}, entry
    )
    assert authority.mode == expected_mode
    assert declared is None and from_catalog is None


def test_legacy_live_plus_endpoint_override_keeps_its_old_behaviour(
    monkeypatch, mock_stack, load_lambda
):
    """The one migration case that must be byte-identical to the last release.

    `SubscriptionMode=marketplace-live` + a simulator endpoint + a catalog with no
    `licenseMode` called the buyer-side Agreement API against the simulator and
    reported `simulated`. Mapping it to the seller-side API instead would change
    which API a live stack calls during an upgrade.
    """
    bucket = mock_stack["bucket"]
    entry = _mp_catalog_entry()
    del entry["licenseMode"]
    _put_catalog(bucket, [entry])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
        endpoint_override=SIMULATOR_ENDPOINT,
        simulator_endpoint="",
    )
    authority, _declared, _catalog = mod._resolve_authority(
        "idp-auto-optimizer", {}, entry
    )
    assert authority.mode == "simulated"
    assert authority.api == mod._API_AGREEMENT
    assert authority.endpoint == SIMULATOR_ENDPOINT
    assert mod._reported_source(authority) == "simulated"


def test_hard_default_is_live_for_a_marketplace_entry(monkeypatch, load_lambda):
    """Step 4. An unrecognised stack setting must not under-check a paid product.

    Deliberately the OPPOSITE of the extension-side default (`none`): the host
    must not over-claim verification, the extension must not lock a paying
    customer out.
    """
    mod = _preload(monkeypatch, load_lambda, source_tag="nonsense-value")
    marketplace, _d, _c = mod._resolve_authority("f", {}, {"source": "marketplace"})
    assert marketplace.mode == "marketplace-live"
    unknown, _d, _c = mod._resolve_authority("f", {}, {})
    assert unknown.mode == "none"


def test_kill_switch_outranks_every_declaration(monkeypatch, mock_stack, load_lambda):
    """ "Check nothing on this stack" must not be overridable per extension."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="auto",
        configuration_bucket=bucket,
    )
    result = mod.handler(_live_event(), None)
    assert result["licenseMode"] == "none"
    assert result["source"] == "auto"
    assert mod._clients == {}, "the kill switch still built a marketplace client"


def test_unrecognised_declared_mode_is_ignored_not_obeyed(
    monkeypatch, mock_stack, load_lambda
):
    """A typo on the install row must not silently pick an authority."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    authority, declared, _catalog = mod._resolve_authority(
        "idp-auto-optimizer",
        {"licenseMode": "marketplace_live"},  # underscore typo
        _mp_catalog_entry(),
    )
    assert declared is None
    assert authority.mode == "marketplace-live"  # fell through to the catalog


def test_simulated_mode_without_a_simulator_is_advisory_not_a_verdict(
    monkeypatch, mock_stack, load_lambda
):
    """`licenseMode: simulated` with no simulator on the stack is a misconfig.

    Calling real AWS GetEntitlements instead would return an empty list and read
    as "not subscribed", which is a wrong answer rather than no answer.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_mp_catalog_entry(licenseMode="simulated")])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
        simulator_endpoint="",
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "advisory"
    assert result["licenseMode"] == "simulated"


# ---------------------------------------------------------------------------
# Simulator compatibility for the buyer-side query.
#
# Root cause of the original incident: the simulator implements a SUBSET of the
# API and rejects `PartyType` outright, and records agreements under the product
# CODE rather than the entity id. Both accommodations are gated on THIS CALL'S
# authority being simulator-backed, because real AWS accepts `PartyType` and
# REJECTS the reduced filter set (verified against a live account).
# ---------------------------------------------------------------------------


def _sim_agreement_mod(monkeypatch, mock_stack, load_lambda):
    """A stack on the legacy simulated-Agreement path (migration shape)."""
    bucket = mock_stack["bucket"]
    entry = _mp_catalog_entry()
    del entry["licenseMode"]
    _put_catalog(bucket, [entry])
    return _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        source_tag="marketplace-live",
        configuration_bucket=bucket,
        endpoint_override=SIMULATOR_ENDPOINT,
        simulator_endpoint="",
    )


def _queue_sim_agreements(mod, calls):
    return _queue_agreements(mod, calls, mode="simulated")


def test_simulator_partytype_rejection_retries_without_it(
    monkeypatch, mock_stack, load_lambda
):
    mod = _sim_agreement_mod(monkeypatch, mock_stack, load_lambda)
    _queue_sim_agreements(
        mod,
        [
            ("prod-a5ee62vs2xa72", True, "ValidationException"),
            ("prod-a5ee62vs2xa72", False, [{"agreementId": "a", "status": "ACTIVE"}]),
        ],
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    # The retry doesn't launder the answer.
    assert result["source"] == "simulated"


def test_simulator_falls_back_to_product_code(monkeypatch, mock_stack, load_lambda):
    """productId matches nothing on the simulator; the productCode does."""
    mod = _sim_agreement_mod(monkeypatch, mock_stack, load_lambda)
    _queue_sim_agreements(
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
    """No relaxed retry against real AWS. One canonical call, then advisory.

    Retrying would drop `PartyType` from the one combination AWS accepts, and the
    reduced query is rejected there anyway — so a retry could only hide the error.
    """
    mod = _live_mod(monkeypatch, mock_stack, load_lambda)
    stubber = _queue_agreements(
        mod, [("prod-a5ee62vs2xa72", True, "ValidationException")]
    )
    result = mod.handler(_live_event(), None)
    assert result["state"] == "ACTIVE"
    assert result["source"] == "advisory"
    stubber.assert_no_pending_responses()


def test_real_aws_does_not_fall_back_to_product_code(
    monkeypatch, mock_stack, load_lambda
):
    """On real AWS `ResourceIdentifier` is the product ENTITY id, full stop."""
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
