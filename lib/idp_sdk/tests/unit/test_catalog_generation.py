# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Feature Platform catalog.json generation in IDPPublisher.

Covers the pure (no-AWS) catalog logic:
- merging OSS bundled-feature entries with the curated extensions-marketplace.yaml
- de-duplication (OSS wins over a same-id marketplace entry)
- malformed marketplace entries are skipped, not fatal
- missing marketplace file → OSS-only catalog
"""

from __future__ import annotations

import json
import os

import pytest

from idp_sdk._core.publish import IDPPublisher


@pytest.fixture
def publisher_in_tmp(tmp_path, monkeypatch):
    """An IDPPublisher rooted in a temp dir with a config_library/ subdir."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("config_library", exist_ok=True)
    pub = IDPPublisher(verbose=False)
    return pub, tmp_path


def _write_marketplace(tmp_path, body: str):
    (tmp_path / "config_library" / "extensions-marketplace.yaml").write_text(
        body, encoding="utf-8"
    )


def _read_catalog(tmp_path):
    return json.loads(
        (tmp_path / "config_library" / "catalog.json").read_text(encoding="utf-8")
    )


def test_oss_only_when_no_marketplace_file(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    oss = [
        {
            "featureId": "docs-by-status",
            "displayName": "Docs By Status",
            "description": "",
            "iconUrl": "",
            "source": "oss",
            "latestVersion": "1.0.2",
        }
    ]
    catalog = pub.write_catalog_file(oss)
    # 1.1 added marketplace `regions` + `productId`; purely additive.
    assert catalog["schemaVersion"] == "1.1"
    assert [f["featureId"] for f in catalog["features"]] == ["docs-by-status"]
    # Written to disk too.
    on_disk = _read_catalog(tmp_path)
    assert on_disk == catalog


def test_merges_marketplace_and_oss_sorted(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
schemaVersion: "1.0"
features:
  - featureId: idp-monitor
    displayName: "IDP Monitor"
    productCode: "prod-xyz"
    marketplaceListingUrl: "https://aws.amazon.com/marketplace/pp/x"
    sellerBucket: "seller-prod"
    sellerBucketRegion: "us-east-1"
    latestVersion: "0.1.4"
    templateKey: "extensions/idp-monitor/template.yaml"
""",
    )
    oss = [
        {
            "featureId": "docs-by-status",
            "displayName": "Zzz Docs",
            "description": "",
            "iconUrl": "",
            "source": "oss",
            "latestVersion": "1.0.2",
        }
    ]
    catalog = pub.write_catalog_file(oss)
    # Sorted by displayName: "IDP Monitor" < "Zzz Docs".
    assert [f["featureId"] for f in catalog["features"]] == [
        "idp-monitor",
        "docs-by-status",
    ]
    mp = catalog["features"][0]
    assert mp["source"] == "marketplace"
    assert mp["productCode"] == "prod-xyz"
    assert mp["sellerBucket"] == "seller-prod"
    # Legacy flat fields are normalized into a one-entry regions map so the host
    # only ever reads one shape.
    assert mp["regions"] == {
        "us-east-1": {
            "sellerBucket": "seller-prod",
            "templateKey": "extensions/idp-monitor/template.yaml",
        }
    }


def test_oss_wins_over_same_id_marketplace_entry(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: dup
    displayName: "Marketplace Dup"
    productCode: "p"
    sellerBucket: "b"
    latestVersion: "9.9.9"
    templateKey: "k"
""",
    )
    oss = [
        {
            "featureId": "dup",
            "displayName": "OSS Dup",
            "description": "",
            "iconUrl": "",
            "source": "oss",
            "latestVersion": "1.0.0",
        }
    ]
    catalog = pub.write_catalog_file(oss)
    assert len(catalog["features"]) == 1
    assert catalog["features"][0]["source"] == "oss"
    assert catalog["features"][0]["latestVersion"] == "1.0.0"


def test_skips_malformed_marketplace_entries(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - displayName: "No featureId"      # dropped
  - "not even a mapping"             # dropped
  - featureId: good
    displayName: "Good"
    productCode: "p"
    sellerBucket: "b"
    latestVersion: "1.0.0"
    templateKey: "k"
""",
    )
    catalog = pub.write_catalog_file([])
    assert [f["featureId"] for f in catalog["features"]] == ["good"]


def test_show_in_nav_passthrough_and_default(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: hidden-mp
    displayName: "Hidden Marketplace"
    productCode: "p"
    sellerBucket: "b"
    latestVersion: "1.0.0"
    templateKey: "k"
    showInNav: false
  - featureId: visible-mp
    displayName: "Visible Marketplace"
    productCode: "p2"
    sellerBucket: "b"
    latestVersion: "1.0.0"
    templateKey: "k2"
""",
    )
    oss = [
        {
            "featureId": "hidden-oss",
            "displayName": "Hidden OSS Sample",
            "description": "",
            "iconUrl": "",
            "source": "oss",
            "latestVersion": "0.1.0",
            "showInNav": False,
        }
    ]
    catalog = pub.write_catalog_file(oss)
    by_id = {f["featureId"]: f for f in catalog["features"]}
    assert by_id["hidden-mp"]["showInNav"] is False
    assert by_id["visible-mp"]["showInNav"] is True  # absent → default True
    assert by_id["hidden-oss"]["showInNav"] is False


# ---------------------------------------------------------------------------
# Schema 1.1: explicit per-region bucket + version-free templateKey.
# ---------------------------------------------------------------------------


def test_regions_map_propagates_to_catalog(publisher_in_tmp):
    """The multi-region shape reaches catalog.json intact, with productId."""
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
schemaVersion: "1.1"
features:
  - featureId: idp-auto-optimizer
    displayName: "Auto Optimizer"
    productCode: "q0k0s3zuuga46hle6fecx547"
    productId: "prod-a5ee62vs2xa72"
    marketplaceListingUrl: "https://aws.amazon.com/marketplace/pp/prodview-44jb64lvdxr3y"
    latestVersion: "0.1.0"
    regions:
      us-west-2:
        sellerBucket: aws-ml-blog-us-west-2
        templateKey: artifacts/genai-idp-mp/extensions/idp-auto-optimizer/template.yaml
      eu-central-1:
        sellerBucket: aws-ml-blog-eu-central-1
        templateKey: artifacts/genai-idp-mp/extensions/idp-auto-optimizer/template.yaml
""",
    )
    catalog = pub.write_catalog_file([])
    entry = catalog["features"][0]
    assert entry["productId"] == "prod-a5ee62vs2xa72"
    assert sorted(entry["regions"]) == ["eu-central-1", "us-west-2"]
    assert entry["regions"]["us-west-2"]["sellerBucket"] == "aws-ml-blog-us-west-2"
    # templateKey stays VERSION-FREE — a version-bearing key goes stale on Update.
    for spec in entry["regions"].values():
        assert spec["templateKey"].endswith("/template.yaml")
        assert entry["latestVersion"] not in spec["templateKey"]


def test_regions_map_wins_over_legacy_flat_fields(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: both-schemas
    displayName: "Both"
    productCode: "p"
    latestVersion: "1.0.0"
    sellerBucket: "legacy-bucket"
    sellerBucketRegion: "us-east-1"
    templateKey: "extensions/both/template.yaml"
    regions:
      us-west-2:
        sellerBucket: new-bucket
        templateKey: extensions/both/template.yaml
""",
    )
    entry = pub.write_catalog_file([])["features"][0]
    assert list(entry["regions"]) == ["us-west-2"]
    # Legacy fields are still EMITTED verbatim for an older host, but they do not
    # contribute to `regions`.
    assert entry["sellerBucket"] == "legacy-bucket"


def test_leading_slash_stripped_from_template_key(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: slashy
    displayName: "Slashy"
    productCode: "p"
    latestVersion: "1.0.0"
    regions:
      us-east-1:
        sellerBucket: b
        templateKey: /extensions/slashy/template.yaml
""",
    )
    entry = pub.write_catalog_file([])["features"][0]
    assert entry["regions"]["us-east-1"]["templateKey"] == (
        "extensions/slashy/template.yaml"
    )


def test_region_entry_missing_bucket_or_key_is_skipped(publisher_in_tmp):
    """A half-specified region is dropped, not published as a broken mapping."""
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: partial
    displayName: "Partial"
    productCode: "p"
    latestVersion: "1.0.0"
    regions:
      us-east-1:
        sellerBucket: good-bucket
        templateKey: extensions/partial/template.yaml
      us-west-2:
        sellerBucket: ""
        templateKey: extensions/partial/template.yaml
      eu-west-1:
        sellerBucket: b
""",
    )
    entry = pub.write_catalog_file([])["features"][0]
    assert list(entry["regions"]) == ["us-east-1"]


def test_legacy_entry_without_region_yields_no_mapping(publisher_in_tmp):
    """No sellerBucketRegion → we refuse to guess which region the bucket is in.

    Guessing is the bug schema 1.1 exists to fix: the old resolver reused one
    bucket in every region, producing a template whose baked CodeUri pointed at
    another region's objects.
    """
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: no-region
    displayName: "No Region"
    productCode: "p"
    latestVersion: "1.0.0"
    sellerBucket: "some-bucket"
    templateKey: "extensions/no-region/template.yaml"
""",
    )
    entry = pub.write_catalog_file([])["features"][0]
    assert entry["regions"] == {}
    # Still published, with legacy fields intact — the host reports it as
    # unavailable rather than silently launching the wrong region's template.
    assert entry["sellerBucket"] == "some-bucket"


def test_malformed_regions_value_is_ignored(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
features:
  - featureId: bad-regions
    displayName: "Bad Regions"
    productCode: "p"
    latestVersion: "1.0.0"
    regions: "us-east-1"
""",
    )
    entry = pub.write_catalog_file([])["features"][0]
    assert entry["regions"] == {}


def test_empty_marketplace_list(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(tmp_path, 'schemaVersion: "1.0"\nfeatures: []\n')
    catalog = pub.write_catalog_file([])
    assert catalog["features"] == []


# ---------------------------------------------------------------------------
# extensions-oss.yaml parsing (which OSS feature dirs to bundle).
# ---------------------------------------------------------------------------


def _write_oss(tmp_path, body: str):
    (tmp_path / "config_library" / "extensions-oss.yaml").write_text(
        body, encoding="utf-8"
    )


def test_oss_features_file_parsed(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_oss(
        tmp_path,
        """
schemaVersion: "1.0"
features:
  - path: feature-platform/sample-feature
  - path: feature-platform/another
""",
    )
    assert pub._bundled_feature_dirs() == [
        "feature-platform/sample-feature",
        "feature-platform/another",
    ]


def test_oss_features_missing_file_uses_default(publisher_in_tmp):
    pub, _ = publisher_in_tmp
    assert pub._bundled_feature_dirs() == pub._DEFAULT_BUNDLED_FEATURE_DIRS


def test_oss_features_entry_without_path_is_fatal(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    _write_oss(tmp_path, "features:\n  - notpath: x\n")
    with pytest.raises(SystemExit):
        pub._bundled_feature_dirs()


# ---------------------------------------------------------------------------
# licenseMode — which authority the HOST checks, declared per extension.
#
# Defaulted HERE, at publish time, rather than in the resolver. The resolver's
# fallback chain is install-row → catalog → legacy stack setting, so a runtime
# default would shadow the legacy step and change behaviour for a stack running an
# older catalog. Baking it means a current catalog always carries an explicit
# value and the legacy step is reached only by a catalog that predates the field.
# ---------------------------------------------------------------------------


def test_license_mode_defaults_to_marketplace_live_when_absent(publisher_in_tmp):
    """The host's default is the STRICT one.

    Deliberately the opposite of the extension-side default (`none`). An
    unrecognised or missing value must not over-claim verification for something
    listed in the marketplace catalog, so the host degrades to the strictest
    authority; the extension degrades to serving so it cannot lock a paying
    customer out.
    """
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
schemaVersion: "1.1"
features:
  - featureId: paid-thing
    displayName: "Paid Thing"
    productCode: "code"
    productId: "prod-thing"
""",
    )
    catalog = pub.write_catalog_file([])
    (entry,) = catalog["features"]
    assert entry["licenseMode"] == "marketplace-live"


@pytest.mark.parametrize("mode", ["none", "simulated", "marketplace-live"])
def test_license_mode_passthrough(publisher_in_tmp, mode):
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        f"""
schemaVersion: "1.1"
features:
  - featureId: paid-thing
    displayName: "Paid Thing"
    licenseMode: {mode}
""",
    )
    catalog = pub.write_catalog_file([])
    (entry,) = catalog["features"]
    assert entry["licenseMode"] == mode


def test_unrecognised_license_mode_is_fatal(publisher_in_tmp):
    """A typo decides which authority confirms a paid subscription.

    Getting that wrong quietly is the failure the field exists to prevent, so this
    fails the publish rather than silently downgrading.
    """
    pub, tmp_path = publisher_in_tmp
    _write_marketplace(
        tmp_path,
        """
schemaVersion: "1.1"
features:
  - featureId: paid-thing
    displayName: "Paid Thing"
    licenseMode: marketplace_live
""",
    )
    with pytest.raises(SystemExit):
        pub.write_catalog_file([])


def test_oss_entries_carry_license_mode_none(publisher_in_tmp):
    """extensions-oss.yaml needs no field, but the CATALOG states it explicitly.

    One shape for every entry the host reads, rather than "absent means none here
    and something else there".
    """
    pub, _tmp_path = publisher_in_tmp
    catalog = pub.write_catalog_file(
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Docs By Status",
                "description": "",
                "iconUrl": "",
                "source": "oss",
                "licenseMode": "none",
                "latestVersion": "1.0.2",
            }
        ]
    )
    (entry,) = catalog["features"]
    assert entry["licenseMode"] == "none"
