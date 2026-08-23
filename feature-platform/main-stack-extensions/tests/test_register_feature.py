"""Unit tests for the register_feature Lambda."""

from __future__ import annotations

import boto3
import pytest
from _helpers import make_appsync_event


def _preload(monkeypatch, table_name: str, load_lambda):
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", table_name)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("register_feature")


VALID_INPUT = {
    "featureId": "docs-by-status",
    "displayName": "Sample: Document Status (feature add-on)",
    "installedVersion": "1.0.4",
    "stackName": "idp-feature-docs-by-status",
    "stackId": "arn:aws:cloudformation:us-east-1:111:stack/idp-feature-docs-by-status/abc",
    "stackRegion": "us-east-1",
    "uiBundlePath": "features/docs-by-status/v1.0.4/",
    "featureApiEndpoint": "https://abc.execute-api.us-east-1.amazonaws.com",
    "installedBy": "admin@example.com",
    "productCode": "prod-docs-by-status",
    "marketplaceListingUrl": "https://aws.amazon.com/marketplace/pp/prodview-abc",
}


def test_register_feature_happy_path(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)

    event = make_appsync_event("registerFeature", {"input": VALID_INPUT})
    result = mod.handler(event, None)

    assert result["featureId"] == "docs-by-status"
    assert result["installedVersion"] == "1.0.4"
    assert result["updateAvailable"] is False
    assert result["installedAt"]

    # Row persisted
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    row = ddb.Table(installed_features_table).get_item(
        Key={"featureId": "docs-by-status"}
    )["Item"]
    assert row["displayName"] == "Sample: Document Status (feature add-on)"
    assert row["featureApiEndpoint"] == VALID_INPUT["featureApiEndpoint"]
    # Marketplace identity is persisted so subscribe/check can read it.
    assert row["productCode"] == "prod-docs-by-status"
    assert row["marketplaceListingUrl"] == VALID_INPUT["marketplaceListingUrl"]


def test_register_feature_omits_absent_marketplace_fields(
    monkeypatch, installed_features_table, load_lambda
):
    """A feature with no marketplace identity (empty tokens omitted by the
    ui-deployer) persists no productCode / marketplaceListingUrl attributes."""
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    minimal = {
        k: v
        for k, v in VALID_INPUT.items()
        if k not in ("productCode", "marketplaceListingUrl")
    }
    mod.handler(make_appsync_event("registerFeature", {"input": minimal}), None)
    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(installed_features_table)
        .get_item(Key={"featureId": "docs-by-status"})["Item"]
    )
    assert "productCode" not in row
    assert "marketplaceListingUrl" not in row


def test_register_feature_is_idempotent_overwrite(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)

    mod.handler(make_appsync_event("registerFeature", {"input": VALID_INPUT}), None)
    updated = {
        **VALID_INPUT,
        "installedVersion": "1.0.5",
        "displayName": "Docs By Status (renamed)",
    }
    mod.handler(make_appsync_event("registerFeature", {"input": updated}), None)

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    row = ddb.Table(installed_features_table).get_item(
        Key={"featureId": "docs-by-status"}
    )["Item"]
    assert row["installedVersion"] == "1.0.5"
    assert row["displayName"] == "Docs By Status (renamed)"


def test_register_feature_rejects_missing_required_fields(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    bad = {k: v for k, v in VALID_INPUT.items() if k != "stackId"}

    with pytest.raises(ValueError, match="missing required fields"):
        mod.handler(make_appsync_event("registerFeature", {"input": bad}), None)


def test_register_feature_rejects_invalid_featureId(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)

    for bad_id in ["Docs_By_Status", "docs.by.status", "Docs-By-Status", "x" * 64]:
        bad = {**VALID_INPUT, "featureId": bad_id}
        with pytest.raises(ValueError, match="Invalid featureId"):
            mod.handler(make_appsync_event("registerFeature", {"input": bad}), None)


def test_unregister_feature_removes_row(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    mod.handler(make_appsync_event("registerFeature", {"input": VALID_INPUT}), None)

    result = mod.handler(
        make_appsync_event("unregisterFeature", {"featureId": "docs-by-status"}), None
    )
    assert result is True

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    assert "Item" not in ddb.Table(installed_features_table).get_item(
        Key={"featureId": "docs-by-status"}
    )


def test_unregister_feature_is_idempotent_when_missing(
    monkeypatch, installed_features_table, load_lambda
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)

    # Deleting a non-existent featureId should still succeed (retry-safety).
    result = mod.handler(
        make_appsync_event("unregisterFeature", {"featureId": "nonexistent"}), None
    )
    assert result is True


def test_unknown_field_raises(monkeypatch, installed_features_table, load_lambda):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)

    with pytest.raises(ValueError, match="Unknown field"):
        mod.handler(make_appsync_event("frobnicate", {}), None)


# ---------------------------------------------------------------------------
# licenseMode — what the EXTENSION declares it enforces against.
#
# This is what makes the host's catalog entry verifiable rather than aspirational:
# checkFeatureEntitlement prefers this value, so the host checks the authority the
# extension actually honours, and can report a mismatch when its own catalog
# disagrees. Without propagation there is no way to detect that recurring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["none", "simulated", "marketplace-live"])
def test_license_mode_is_persisted(
    monkeypatch, installed_features_table, load_lambda, mode
):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    payload = dict(VALID_INPUT, licenseMode=mode)
    mod.handler(make_appsync_event("registerFeature", {"input": payload}), None)

    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(installed_features_table)
        .get_item(Key={"featureId": "docs-by-status"})["Item"]
    )
    assert row["licenseMode"] == mode


def test_unrecognised_license_mode_is_dropped_not_persisted(
    monkeypatch, installed_features_table, load_lambda
):
    """A typo must not silently pick an authority.

    The install row is the HIGHEST-priority input to the host's authority
    resolution, so persisting garbage would win over both the catalog and the
    stack default. Dropping it falls back to the catalog, which is the safe
    direction.
    """
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    payload = dict(VALID_INPUT, licenseMode="marketplace_live")  # underscore typo
    mod.handler(make_appsync_event("registerFeature", {"input": payload}), None)

    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(installed_features_table)
        .get_item(Key={"featureId": "docs-by-status"})["Item"]
    )
    assert "licenseMode" not in row


def test_absent_license_mode_persists_nothing(
    monkeypatch, installed_features_table, load_lambda
):
    """An extension published before the field existed installs unchanged.

    Absent (not "none") so the host can tell "this extension said nothing" from
    "this extension said check nothing" and fall through to the catalog.
    """
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    mod.handler(make_appsync_event("registerFeature", {"input": VALID_INPUT}), None)

    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(installed_features_table)
        .get_item(Key={"featureId": "docs-by-status"})["Item"]
    )
    assert "licenseMode" not in row
