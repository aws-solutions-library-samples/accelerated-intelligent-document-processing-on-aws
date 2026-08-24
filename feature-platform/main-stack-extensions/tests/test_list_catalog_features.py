"""Unit tests for the list_catalog_features Lambda.

Discovery is manifest-driven: the resolver reads ONE catalog.json from the
stack's ConfigurationBucket via a single GetObject. It performs NO
ListObjectsV2 (asserted indirectly — no bucket-listing fixtures exist).
"""

from __future__ import annotations

import json

import boto3
from _helpers import make_appsync_event

_CATALOG_KEY = "config_library/catalog.json"


def _preload(monkeypatch, load_lambda, configuration_bucket: str | None = None):
    """Configure env vars + (re-)import the lambda module fresh."""
    if configuration_bucket:
        monkeypatch.setenv("CONFIGURATION_BUCKET", configuration_bucket)
    else:
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("list_catalog_features")


def _put_catalog(bucket: str, features: list[dict]):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(
        Bucket=bucket,
        Key=_CATALOG_KEY,
        Body=json.dumps({"schemaVersion": "1.0", "features": features}).encode("utf-8"),
    )


def test_no_configuration_bucket_returns_empty_list(
    monkeypatch, load_lambda, aws_credentials
):
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=None)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result == []


def test_missing_catalog_returns_empty_list(
    monkeypatch, configuration_bucket, load_lambda
):
    # Bucket exists but no catalog.json object → empty (NoSuchKey).
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result == []


def test_lists_features_sorted_by_display_name(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(
        configuration_bucket,
        [
            {
                "featureId": "zeta",
                "displayName": "Zeta Widget",
                "latestVersion": "1.0.0",
                "source": "oss",
            },
            {
                "featureId": "alpha",
                "displayName": "Alpha Widget",
                "latestVersion": "2.1.0",
                "source": "oss",
                "iconUrl": "https://example.com/a.png",
            },
        ],
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)

    assert [f["featureId"] for f in result] == ["alpha", "zeta"]
    assert result[0] == {
        "featureId": "alpha",
        "displayName": "Alpha Widget",
        "latestVersion": "2.1.0",
        "iconUrl": "https://example.com/a.png",
        "description": None,
        "docsUrl": None,
        "showInNav": True,
        "source": "oss",
        "productCode": None,
        "marketplaceListingUrl": None,
        "artifactBucket": None,
        "artifactPrefix": None,
        # OSS features ship with the host, so they are never region-scoped.
        "availableInRegion": True,
        "availableRegions": [],
    }


def test_description_is_surfaced(monkeypatch, configuration_bucket, load_lambda):
    _put_catalog(
        configuration_bucket,
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Sample: Document Status (feature add-on)",
                "latestVersion": "1.0.4",
                "source": "oss",
                "description": "Adds a Document Status page.",
                "docsUrl": "extensions/sample-document-status",
            }
        ],
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result[0]["description"] == "Adds a Document Status page."
    assert result[0]["docsUrl"] == "extensions/sample-document-status"


def test_marketplace_feature_carries_subscribe_metadata(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(
        configuration_bucket,
        [
            {
                "featureId": "my-paid-extension",
                "displayName": "My Paid Extension",
                "latestVersion": "0.1.4",
                "source": "marketplace",
                "productCode": "abc123",
                "marketplaceListingUrl": "https://aws.amazon.com/marketplace/pp/x",
            }
        ],
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result[0]["source"] == "marketplace"
    assert result[0]["productCode"] == "abc123"
    assert result[0]["marketplaceListingUrl"].endswith("/x")


def test_falls_back_to_feature_id_and_defaults_source_oss(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(configuration_bucket, [{"featureId": "widgetz"}])
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert len(result) == 1
    assert result[0]["displayName"] == "widgetz"
    assert result[0]["source"] == "oss"
    assert result[0]["latestVersion"] == ""


def test_show_in_nav_false_is_surfaced_and_absent_defaults_true(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(
        configuration_bucket,
        [
            {
                "featureId": "hidden-sample",
                "displayName": "Hidden Sample",
                "latestVersion": "0.1.0",
                "showInNav": False,
            },
            {
                "featureId": "visible-feature",
                "displayName": "Visible Feature",
                "latestVersion": "1.0.0",
                # showInNav absent → True
            },
        ],
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    by_id = {f["featureId"]: f for f in result}
    assert by_id["hidden-sample"]["showInNav"] is False
    assert by_id["visible-feature"]["showInNav"] is True


def test_malformed_catalog_does_not_crash(
    monkeypatch, configuration_bucket, load_lambda
):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(
        Bucket=configuration_bucket, Key=_CATALOG_KEY, Body=b"this is not JSON"
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result == []


def test_skips_entries_without_feature_id(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(
        configuration_bucket,
        [
            {"displayName": "No Id"},  # dropped
            {"featureId": "ok", "displayName": "OK", "latestVersion": "1.0.0"},
        ],
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert [f["featureId"] for f in result] == ["ok"]


def test_missing_features_list_returns_empty(
    monkeypatch, configuration_bucket, load_lambda
):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(
        Bucket=configuration_bucket,
        Key=_CATALOG_KEY,
        Body=json.dumps({"schemaVersion": "1.0"}).encode("utf-8"),
    )
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    result = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert result == []


# ---------------------------------------------------------------------------
# Region availability (catalog schema 1.1). Marketplace extensions are
# region-scoped because `sam package` bakes a region-specific s3:// CodeUri into
# the published template; the UI needs to know before it offers a Subscribe CTA.
# ---------------------------------------------------------------------------

_MP_KEY = "artifacts/genai-idp-mp/extensions/idp-auto-optimizer/template.yaml"


def _mp_entry(regions: dict) -> dict:
    return {
        "featureId": "idp-auto-optimizer",
        "displayName": "Auto Optimizer",
        "latestVersion": "0.1.0",
        "source": "marketplace",
        "productCode": "q0k0s3zuuga46hle6fecx547",
        "marketplaceListingUrl": "https://aws.amazon.com/marketplace/pp/prodview-x",
        "regions": regions,
    }


def _three_regions() -> dict:
    return {
        r: {"sellerBucket": f"aws-ml-blog-{r}", "templateKey": _MP_KEY}
        for r in ("us-west-2", "us-east-1", "eu-central-1")
    }


def test_marketplace_available_in_host_region(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(configuration_bucket, [_mp_entry(_three_regions())])
    monkeypatch.setenv("HOST_REGION", "eu-central-1")
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is True
    assert feature["availableRegions"] == ["eu-central-1", "us-east-1", "us-west-2"]


def test_marketplace_unavailable_in_host_region(
    monkeypatch, configuration_bucket, load_lambda
):
    """The whole point: eu-west-2 isn't published, so the UI must not offer it."""
    _put_catalog(configuration_bucket, [_mp_entry(_three_regions())])
    monkeypatch.setenv("HOST_REGION", "eu-west-2")
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is False
    # Still reports where it IS available so the admin isn't left guessing.
    assert "us-east-1" in feature["availableRegions"]


def test_marketplace_legacy_flat_schema_region(
    monkeypatch, configuration_bucket, load_lambda
):
    """Deprecated flat sellerBucketRegion is treated as a single-region map."""
    entry = _mp_entry({})
    entry.update(
        {
            "sellerBucket": "legacy-seller-bucket",
            "sellerBucketRegion": "us-east-1",
            "templateKey": _MP_KEY,
        }
    )
    _put_catalog(configuration_bucket, [entry])
    monkeypatch.setenv("HOST_REGION", "us-east-1")
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is True
    assert feature["availableRegions"] == ["us-east-1"]


def test_marketplace_no_region_mapping_is_unavailable(
    monkeypatch, configuration_bucket, load_lambda
):
    _put_catalog(configuration_bucket, [_mp_entry({})])
    monkeypatch.setenv("HOST_REGION", "us-east-1")
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is False
    assert feature["availableRegions"] == []


def test_unknown_host_region_does_not_claim_unavailability(
    monkeypatch, configuration_bucket, load_lambda
):
    """With no resolvable region we must not assert an unavailability we can't
    substantiate — let getFeatureLaunchUrl be the one to fail closed."""
    _put_catalog(configuration_bucket, [_mp_entry(_three_regions())])
    monkeypatch.setenv("HOST_REGION", "")
    monkeypatch.delenv("AWS_REGION", raising=False)
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is True


def test_half_specified_region_entry_ignored(
    monkeypatch, configuration_bucket, load_lambda
):
    """A region with a bucket but no templateKey isn't installable there."""
    _put_catalog(
        configuration_bucket,
        [
            _mp_entry(
                {
                    "us-east-1": {"sellerBucket": "b"},  # no templateKey
                    "us-west-2": {"sellerBucket": "b2", "templateKey": _MP_KEY},
                }
            )
        ],
    )
    monkeypatch.setenv("HOST_REGION", "us-east-1")
    mod = _preload(monkeypatch, load_lambda, configuration_bucket=configuration_bucket)
    (feature,) = mod.handler(make_appsync_event("listCatalogFeatures"), None)
    assert feature["availableInRegion"] is False
    assert feature["availableRegions"] == ["us-west-2"]
