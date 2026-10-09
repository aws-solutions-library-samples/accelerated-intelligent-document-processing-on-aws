"""Unit tests for the subscribe_feature Lambda.

The Lambda returns a Marketplace (or simulator) URL the UI should redirect the
admin to. The product code + marketplace listing URL come from the feature's
InstalledFeatures row (baked from the manifest at install), falling back to
catalog.json — which is the path that matters, since Subscribe runs BEFORE the
install row exists. These tests seed the row via the `installed_features_table`
fixture and the catalog via `_put_catalog`.
"""

from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from _helpers import make_appsync_event
from botocore.exceptions import ClientError


def _seed_row(table_name, feature_id, *, product_code=None, listing_url=None):
    """Put an InstalledFeatures row carrying the marketplace identity."""
    item = {"featureId": feature_id}
    if product_code is not None:
        item["productCode"] = product_code
    if listing_url is not None:
        item["marketplaceListingUrl"] = listing_url
    boto3.resource("dynamodb", region_name="us-east-1").Table(table_name).put_item(
        Item=item
    )


def _preload(
    monkeypatch,
    load_lambda,
    *,
    table_name="",
    simulator_endpoint="http://sim.example.com",
    offer_map="{}",
    default_customer="CUST-default",
    default_buyer_account="111122223333",
    source_tag="simulator",
    configuration_bucket="",
):
    monkeypatch.setenv("SIMULATOR_ADMIN_ENDPOINT", simulator_endpoint)
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", table_name)
    monkeypatch.setenv("FEATURE_OFFER_ID_MAP", offer_map)
    monkeypatch.setenv("DEFAULT_CUSTOMER_IDENTIFIER", default_customer)
    monkeypatch.setenv("DEFAULT_BUYER_ACCOUNT_ID", default_buyer_account)
    monkeypatch.setenv("ADMIN_GROUP", "Admin")
    monkeypatch.setenv("SIMULATOR_SOURCE_TAG", source_tag)
    monkeypatch.setenv("CONFIGURATION_BUCKET", configuration_bucket)
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("subscribe_feature")


_CATALOG_KEY = "config_library/catalog.json"


def _put_catalog(bucket: str, features: list) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket,
        Key=_CATALOG_KEY,
        Body=json.dumps({"schemaVersion": "1.1", "features": features}).encode("utf-8"),
    )


def test_happy_path_simulator_mode(monkeypatch, load_lambda, installed_features_table):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature",
            {
                "featureId": "docs-by-status",
                "returnUrl": "http://app/features/docs-by-status",
            },
            groups=["Admin"],
        ),
        None,
    )

    # Entitlement state stays NONE — the admin still has to accept terms.
    assert result["featureId"] == "docs-by-status"
    assert result["state"] == "NONE"
    assert result["expiresAt"] is None
    assert result["customerIdentifier"] == "CUST-default"
    assert result["productCode"] == "prod123"
    assert result["source"] == "simulated"
    # Constructed marketplaceUrl
    assert result["marketplaceUrl"].startswith(
        "http://sim.example.com/marketplace/pp/prod123"
    )
    parsed = urlparse(result["marketplaceUrl"])
    q = parse_qs(parsed.query)
    assert q["featureId"] == ["docs-by-status"]
    assert q["buyerAccountId"] == ["111122223333"]
    assert q["returnUrl"] == ["http://app/features/docs-by-status"]


def test_simulator_mode_synthesizes_product_code(
    monkeypatch, load_lambda, installed_features_table
):
    """In simulator mode, a row without a productCode falls back to prod-<id>-sim."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    assert result["productCode"] == "prod-docs-by-status-sim"
    assert "prod-docs-by-status-sim" in result["marketplaceUrl"]


def test_marketplace_mode_requires_product_code(
    monkeypatch, load_lambda, installed_features_table
):
    """In marketplace mode, a feature whose install row has no productCode raises."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
        simulator_endpoint="",
    )
    with pytest.raises(mod.SubscribeError, match="productCode"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
            ),
            None,
        )


def test_marketplace_mode_uses_install_row_listing_url(
    monkeypatch, load_lambda, installed_features_table
):
    """In marketplace mode, returns the listing URL from the install row."""
    _seed_row(
        installed_features_table,
        "docs-by-status",
        product_code="prod123",
        listing_url="https://aws.amazon.com/marketplace/pp/prodview-abc",
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
        simulator_endpoint="",
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    assert (
        result["marketplaceUrl"] == "https://aws.amazon.com/marketplace/pp/prodview-abc"
    )
    assert result["source"] == "simulated"


def test_simulator_endpoint_wins_over_install_listing_url(
    monkeypatch, load_lambda, installed_features_table
):
    """Regression: when a simulator endpoint is configured it is authoritative —
    redirect to the simulator product page, NOT the feature's (possibly
    placeholder) real-Marketplace listing URL from the install row."""
    _seed_row(
        installed_features_table,
        "docs-by-status",
        product_code="prod123",
        listing_url="https://aws.amazon.com/marketplace/pp/REPLACE-docs-by-status",
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
        simulator_endpoint="http://sim.example.com",
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    assert result["marketplaceUrl"].startswith(
        "http://sim.example.com/marketplace/pp/prod123"
    )
    assert "REPLACE-docs-by-status" not in result["marketplaceUrl"]


def test_marketplace_mode_requires_listing_url(
    monkeypatch, load_lambda, installed_features_table
):
    """No simulator endpoint AND no listing URL on the install row → raises
    (true-production feature published without marketplace.listingUrl)."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
        simulator_endpoint="",  # production: no simulator
    )
    with pytest.raises(mod.SubscribeError, match="listing URL"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
            ),
            None,
        )


def test_offer_id_is_threaded_through(
    monkeypatch, load_lambda, installed_features_table
):
    """When FEATURE_OFFER_ID_MAP has an entry, offerId appears in the URL."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        offer_map='{"docs-by-status":"offer-abc123"}',
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    q = parse_qs(urlparse(result["marketplaceUrl"]).query)
    assert q["offerId"] == ["offer-abc123"]


def test_rejects_non_admin(monkeypatch, load_lambda, installed_features_table):
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    with pytest.raises(Exception, match="Admin"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Viewer"]
            ),
            None,
        )


def test_missing_feature_id(monkeypatch, load_lambda, installed_features_table):
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    with pytest.raises(ValueError, match="featureId"):
        mod.handler(
            make_appsync_event("subscribeFeature", {}, groups=["Admin"]),
            None,
        )


def test_no_endpoint_and_no_listing_url_raises(
    monkeypatch, load_lambda, installed_features_table
):
    """No simulator endpoint and no listing URL on the row → SubscribeError
    (can't build a redirect URL either way)."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        simulator_endpoint="",
    )
    with pytest.raises(mod.SubscribeError, match="listing URL"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
            ),
            None,
        )


def test_header_customer_identifier_takes_precedence(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature",
            {"featureId": "docs-by-status"},
            groups=["Admin"],
            headers={"x-amzn-marketplace-customer-identifier": "CUST-override"},
        ),
        None,
    )
    assert result["customerIdentifier"] == "CUST-override"


def test_default_customer_identifier_for_simulator_when_missing(
    monkeypatch, load_lambda, installed_features_table
):
    """Simulator mode with no default customer → falls back to cust-idp-default."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="",
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    assert result["customerIdentifier"] == "cust-idp-default"


def test_return_url_defaults_when_not_supplied(
    monkeypatch, load_lambda, installed_features_table
):
    """If the caller didn't supply returnUrl, a default /features/<id> is used."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "docs-by-status"}, groups=["Admin"]
        ),
        None,
    )
    q = parse_qs(urlparse(result["marketplaceUrl"]).query)
    assert q["returnUrl"] == ["/features/docs-by-status"]


# ---------------------------------------------------------------------------
# Catalog fallback — the not-yet-installed path, which is the ONLY path that
# matters for Subscribe. The InstalledFeatures row doesn't exist until after
# install, so before this fallback existed, real-Marketplace mode raised
# SubscribeError and the admin got an error instead of the listing page.
# ---------------------------------------------------------------------------

_MP_LISTING = "https://aws.amazon.com/marketplace/pp/prodview-44jb64lvdxr3y"


def _auto_optimizer_entry(**over) -> dict:
    entry = {
        "featureId": "idp-auto-optimizer",
        "displayName": "Auto Optimizer",
        "source": "marketplace",
        "productCode": "q0k0s3zuuga46hle6fecx547",
        "productId": "prod-a5ee62vs2xa72",
        "marketplaceListingUrl": _MP_LISTING,
        "latestVersion": "0.1.0",
    }
    entry.update(over)
    return entry


def test_not_installed_marketplace_feature_uses_catalog_listing_url(
    monkeypatch, mock_stack, load_lambda
):
    """The headline fix: subscribe works BEFORE the feature is installed."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_auto_optimizer_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],  # table exists, NO row seeded
        simulator_endpoint="",  # real-Marketplace mode
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "idp-auto-optimizer"}, groups=["Admin"]
        ),
        None,
    )
    assert result["marketplaceUrl"] == _MP_LISTING
    assert result["productCode"] == "q0k0s3zuuga46hle6fecx547"


def test_install_row_listing_url_wins_over_catalog(
    monkeypatch, mock_stack, load_lambda
):
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _put_catalog(bucket, [_auto_optimizer_entry()])
    _seed_row(
        table,
        "idp-auto-optimizer",
        product_code="from-row",
        listing_url="https://aws.amazon.com/marketplace/pp/from-row",
    )
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=table,
        simulator_endpoint="",
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "idp-auto-optimizer"}, groups=["Admin"]
        ),
        None,
    )
    assert result["marketplaceUrl"].endswith("/from-row")
    assert result["productCode"] == "from-row"


def test_a_feature_in_neither_the_catalog_nor_the_install_rows_is_not_found(
    monkeypatch, mock_stack, load_lambda
):
    """Nothing knows this feature id -> ResourceNotFound, which maps to 404.

    It used to share the SubscribeError below, which answered 500 and told the
    admin to set `productCode` on a catalog entry that does not exist.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        simulator_endpoint="",
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    with pytest.raises(mod.ResourceNotFound, match="idp-auto-optimizer"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature",
                {"featureId": "idp-auto-optimizer"},
                groups=["Admin"],
            ),
            None,
        )


def test_the_not_found_path_reads_each_source_once(
    monkeypatch, mock_stack, load_lambda
):
    """Each source is read once and the result reused, not re-read to decide 404.

    `_feature_is_absent` used to re-read both sources, costing a second
    GetObject and a second GetItem on the error path — and, more than the cost,
    allowing the two reads to disagree: a feature installed between them is
    absent to one and present to the other, so the status depended on timing.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        simulator_endpoint="",
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    catalog_reads, installed_reads = [], []
    real_catalog, real_installed = mod._catalog_entry, mod._installed_row

    def counting_catalog(feature_id):
        catalog_reads.append(feature_id)
        return real_catalog(feature_id)

    def counting_installed(feature_id):
        installed_reads.append(feature_id)
        return real_installed(feature_id)

    monkeypatch.setattr(mod, "_catalog_entry", counting_catalog)
    monkeypatch.setattr(mod, "_installed_row", counting_installed)

    with pytest.raises(mod.ResourceNotFound):
        mod.handler(
            make_appsync_event(
                "subscribeFeature",
                {"featureId": "idp-auto-optimizer"},
                groups=["Admin"],
            ),
            None,
        )

    assert catalog_reads == ["idp-auto-optimizer"], (
        f"catalog read {len(catalog_reads)} times on the not-found path"
    )
    assert installed_reads == ["idp-auto-optimizer"], (
        f"install row read {len(installed_reads)} times on the not-found path"
    )


def test_a_known_feature_with_an_incomplete_entry_still_raises_subscribe_error(
    monkeypatch, mock_stack, load_lambda
):
    """The catalog lists the feature but carries no productCode -> still 500.

    The companion of the test above, and the reason the two are not one branch:
    this deployment IS misconfigured and the remedy named in the message is the
    right one, so it must not be softened to a 404.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [{"featureId": "idp-auto-optimizer", "source": "marketplace"}])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        simulator_endpoint="",
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    with pytest.raises(mod.SubscribeError, match="extensions-marketplace.yaml"):
        mod.handler(
            make_appsync_event(
                "subscribeFeature",
                {"featureId": "idp-auto-optimizer"},
                groups=["Admin"],
            ),
            None,
        )


def test_an_unreadable_catalog_is_not_reported_as_a_missing_feature(
    monkeypatch, mock_stack, load_lambda
):
    """An unreadable catalog keeps its 500 rather than becoming a 404.

    "No such feature" for a bucket-policy or S3 fault sends the admin to the
    catalog when the fault is the read, and a 404 is also invisible to the 5xx
    error-rate alarm. _catalog_entry reports absence and unreadability
    separately so this stays distinguishable.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        simulator_endpoint="",
        source_tag="marketplace-live",
        configuration_bucket=bucket,
    )
    # AccessDenied specifically: a bucket policy or KMS refusal is the realistic
    # way this read fails, and it is the one _catalog_entry must not read as
    # "the catalog does not list this feature". (A NoSuchKey/404 is handled by the
    # same branch; an exception type the helper does not catch propagates and
    # still reaches the caller as a 500, which is the safe direction.)
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
    )
    with patch.object(mod, "_config_s3") as fake_s3:
        fake_s3.return_value.get_object.side_effect = denied
        with pytest.raises(mod.SubscribeError, match="extensions-marketplace.yaml"):
            mod.handler(
                make_appsync_event(
                    "subscribeFeature",
                    {"featureId": "idp-auto-optimizer"},
                    groups=["Admin"],
                ),
                None,
            )


def test_simulator_endpoint_still_wins_over_catalog_listing(
    monkeypatch, mock_stack, load_lambda
):
    """Dev/CI behaviour is untouched: a configured simulator is authoritative."""
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_auto_optimizer_entry()])
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=mock_stack["table_name"],
        simulator_endpoint="http://sim.example.com",
        source_tag="simulator",
        configuration_bucket=bucket,
    )
    result = mod.handler(
        make_appsync_event(
            "subscribeFeature", {"featureId": "idp-auto-optimizer"}, groups=["Admin"]
        ),
        None,
    )
    assert result["marketplaceUrl"].startswith("http://sim.example.com/marketplace/pp/")
    assert "q0k0s3zuuga46hle6fecx547" in result["marketplaceUrl"]
