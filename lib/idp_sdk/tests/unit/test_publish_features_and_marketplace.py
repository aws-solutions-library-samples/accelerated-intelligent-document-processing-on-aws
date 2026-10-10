# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``IDPPublisher``'s feature-platform and marketplace steps.

Two curated YAML files in ``config_library/`` decide what a deployed stack offers
in its extension catalog. ``extensions-oss.yaml`` lists in-repo feature
directories, which publish builds (``sam build`` + ``sam package``, a Vite UI
bundle) and uploads under a version-free ``<prefix>/extensions/<id>/`` base;
``extensions-marketplace.yaml`` lists closed-source paid extensions, whose
metadata has to be carried inline because there is no in-repo manifest to read.
``write_catalog_file`` merges the two into the single ``catalog.json`` object the
host's ``listCatalogFeatures`` resolver reads.

Three things shaped these tests.

First, the normalization functions are pure and their wrong answers are
expensive. ``_normalize_marketplace_regions`` converts legacy flat
``sellerBucket``/``sellerBucketRegion``/``templateKey`` fields into the schema-1.1
per-region map, and the docstring records why guessing is forbidden: the old
resolver reused one region's bucket everywhere and handed customers a template
whose baked ``CodeUri`` pointed at another region's objects. So the tests cover
the partial and malformed shapes — a region spec that is not a mapping, one
missing half of its pair, a ``regions`` key that is a list — not just the happy
path. ``_marketplace_license_mode`` decides which authority confirms a paid
subscription, and its default is deliberately the strict one, so both the
defaulting and the refusal to accept an unrecognised value are asserted.

Second, ``_build_and_upload_single_feature`` takes ``load_manifest`` and
``FeaturePublisher`` as parameters, so its caching logic can be driven with fakes
without touching npm or SAM. Its three cache-invalidation rules each exist
because of a specific shipped failure — a bundle whose baked version does not
match the registered one makes the host's FeatureLoader refuse to run it, and a
git-ignored agent-source zip that was cleaned between runs makes install-time
CodeBuild fail with a 404 — so each is tested as an independent reason to
rebuild.

Third, the upload half runs against ``moto`` rather than a mock, because what
matters is the key layout: a wrong ``Content-Type`` or a preset uploaded outside
the ``<version>/`` subfolder is invisible at publish time and fails at install.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core.publish import IDPPublisher

# Two tests below make a file unreadable to reach an OSError branch. Root
# ignores mode bits, so they would silently assert the wrong thing in a CI
# container that runs as root.
_needs_a_non_root_user = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root can read a mode-000 file, so the OSError branch is unreachable",
)

_BUCKET = "idp-publish-artifacts"
_PREFIX = "idp"
_VERSION = "0.6.9"
_FEATURE_ID = "sample-feature"
_FEATURE_VERSION = "1.2.3"
_REGION = "us-east-1"


def _publisher(s3_client=None):
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = f"{_PREFIX}/{_VERSION}"
    pub.region = _REGION
    pub.s3_client = s3_client
    pub.console.quiet = True
    return pub


def _recording_publisher(s3_client=None):
    """A publisher whose warnings and errors are captured instead of printed."""
    pub = _publisher(s3_client)
    pub.warnings = []
    pub.errors = []
    pub.log_warning = lambda msg, thread=None: pub.warnings.append(msg)
    pub.log_error = lambda msg, thread=None: pub.errors.append(msg)
    return pub


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# _bundled_feature_dirs
# ---------------------------------------------------------------------------


def test_bundled_feature_dirs_come_from_the_oss_extensions_file(tmp_path, monkeypatch):
    """The order and the exact paths are the contract, in file order.

    Each path becomes a built, uploaded extension and a catalog entry, so a
    dropped entry means a feature silently missing from every fresh stack's
    catalog.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-oss.yaml",
        'schemaVersion: "1.0"\n'
        "features:\n"
        "  - path: feature-platform/sample-feature\n"
        "  - path: feature-platform/pii-anonymizer\n",
    )

    assert _publisher()._bundled_feature_dirs() == [
        "feature-platform/sample-feature",
        "feature-platform/pii-anonymizer",
    ]


def test_a_missing_oss_extensions_file_falls_back_to_the_default_list(
    tmp_path, monkeypatch
):
    """A trimmed checkout still bundles the reference sample.

    The fallback is returned as a fresh list, so a caller that mutates it cannot
    corrupt the class attribute for the rest of the process.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()

    dirs = pub._bundled_feature_dirs()

    assert dirs == ["feature-platform/sample-feature"]
    dirs.append("mutated")
    assert pub._bundled_feature_dirs() == ["feature-platform/sample-feature"]
    assert IDPPublisher._DEFAULT_BUNDLED_FEATURE_DIRS == [
        "feature-platform/sample-feature"
    ]


def test_an_empty_features_list_is_not_the_same_as_an_absent_file(
    tmp_path, monkeypatch
):
    """A present file with no entries means "bundle nothing", and is honoured.

    This distinction is load-bearing for a build that deliberately ships no OSS
    extensions: falling back to the default here would re-add the sample feature
    to a catalog somebody explicitly emptied.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-oss.yaml",
        'schemaVersion: "1.0"\nfeatures: []\n',
    )

    assert _publisher()._bundled_feature_dirs() == []


def test_malformed_oss_extensions_yaml_is_a_hard_error(tmp_path, monkeypatch):
    """A parse error must stop publish, not quietly bundle nothing.

    Silently producing an empty list is how a broken file would drop every
    bundled feature from the catalog with a green publish.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-oss.yaml",
        "features:\n  - path: a\n   bad-indent: true\n",
    )

    with pytest.raises(SystemExit) as exc:
        _publisher()._bundled_feature_dirs()

    assert exc.value.code == 1


@pytest.mark.parametrize(
    "body",
    [
        'schemaVersion: "1.0"\nfeatures: feature-platform/sample-feature\n',
        'schemaVersion: "1.0"\nfeatures:\n  sample: feature-platform/sample\n',
    ],
    ids=["a-bare-string", "a-mapping"],
)
def test_a_features_key_that_is_not_a_list_is_a_hard_error(body, tmp_path, monkeypatch):
    """``features:`` must be a list; a string or mapping is refused.

    Both shapes are plausible hand-edits, and both would otherwise be iterated
    character-by-character or key-by-key.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config_library" / "extensions-oss.yaml", body)

    with pytest.raises(SystemExit) as exc:
        _publisher()._bundled_feature_dirs()

    assert exc.value.code == 1


@pytest.mark.parametrize(
    "entry", ["  - feature-platform/sample-feature\n", "  - name: sample\n", "  - {}\n"]
)
def test_a_feature_entry_without_a_path_is_a_hard_error(entry, tmp_path, monkeypatch):
    """Every entry must be a mapping carrying ``path``."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-oss.yaml",
        'schemaVersion: "1.0"\nfeatures:\n' + entry,
    )

    with pytest.raises(SystemExit) as exc:
        _publisher()._bundled_feature_dirs()

    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _get_feature_deps
# ---------------------------------------------------------------------------


def test_feature_deps_name_every_input_to_a_feature_build():
    """These paths are the cache key for "has this feature changed?".

    A path missing from this list is a file whose edit does not invalidate the
    feature's ``.checksum``, so the build is skipped and the previous UI bundle
    is re-published. The whole list is asserted, in order, because the order
    feeds a concatenated digest.
    """
    deps = _publisher()._get_feature_deps(Path("feature-platform/sample-feature"))

    assert deps == [
        "feature-platform/sample-feature/feature.yaml",
        "feature-platform/sample-feature/template.yaml",
        "feature-platform/sample-feature/feature-api",
        "feature-platform/sample-feature/feature-ui/src",
        "feature-platform/sample-feature/feature-ui/package.json",
        "feature-platform/sample-feature/feature-ui/vite.config.ts",
        "feature-platform/sample-feature/feature-ui/tsconfig.json",
        "feature-platform/sample-feature/feature-ui/index.html",
        "feature-platform/sample-feature/ui-deployer",
    ]


# ---------------------------------------------------------------------------
# _sample_config_id / _sample_label
# ---------------------------------------------------------------------------


def test_an_explicit_config_id_is_trusted_without_checking_the_directory(
    tmp_path, monkeypatch
):
    """An override wins outright — including over a preset that does not exist.

    The curated table in ``_SAMPLE_OVERRIDES`` is not validated against
    ``config_library/unified/``, so a typo there publishes a manifest entry whose
    ``configId`` the UI will offer to import and fail to find. Every id in the
    table does currently resolve, so this pins the mechanism rather than a live
    break.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher()

    assert (
        pub._sample_config_id("no-such-preset", "lending_package") == "no-such-preset"
    )


def test_a_config_id_is_resolved_by_folder_name_convention(tmp_path, monkeypatch):
    """With no override, a matching preset directory supplies the association.

    This is what makes a newly added sample automatically pick up its config:
    drop ``samples/foo.pdf`` beside ``config_library/unified/foo/`` and the
    manifest links them.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config_library" / "unified" / "my-preset").mkdir(parents=True)
    pub = _publisher()

    assert pub._sample_config_id(None, "my-preset") == "my-preset"
    assert pub._sample_config_id(None, "absent-preset") is None
    # A file of that name is not a preset directory.
    _write(tmp_path / "config_library" / "unified" / "a-file", "x")
    assert pub._sample_config_id(None, "a-file") is None


def test_a_curated_sample_uses_its_table_entry(tmp_path, monkeypatch):
    """The override table is keyed by the full filename, extension included."""
    monkeypatch.chdir(tmp_path)

    name, desc, config_id = _publisher()._sample_label("lending_package.pdf")

    assert name == "Lending Package"
    assert "mortgage" in desc
    assert config_id == "lending-package-sample"


def test_a_batch_directory_is_keyed_by_its_bare_name(tmp_path, monkeypatch):
    """Batch subdirectories have no extension, so the key is the folder name."""
    monkeypatch.chdir(tmp_path)

    assert _publisher()._sample_label("w2") == (
        "W-2 Forms",
        "Batch of W-2 tax form documents.",
        "fake-w2",
    )


@pytest.mark.parametrize(
    ("key", "expected_name"),
    [
        ("quarterly_earnings_report.pdf", "Quarterly Earnings Report"),
        ("my-scanned-receipt.png", "My Scanned Receipt"),
        ("mixed_case-Name.tiff", "Mixed Case Name"),
        ("  padded_name  .pdf", "Padded Name"),
    ],
)
def test_an_unlisted_sample_gets_a_name_derived_from_its_filename(
    key, expected_name, tmp_path, monkeypatch
):
    """Unlisted samples are still indexed, with a readable derived name.

    Underscores and dashes become spaces and the result is title-cased, which is
    what lets a sample be added to ``samples/`` with no code change. Note
    ``.title()`` lowercases the rest of each word, so ``mixed_case-Name`` comes
    back as ``Mixed Case Name`` — the derived name is not the filename's casing.
    """
    monkeypatch.chdir(tmp_path)

    name, desc, config_id = _publisher()._sample_label(key)

    assert name == expected_name
    assert desc == f"Sample document: {expected_name}."
    assert config_id is None


def test_an_unlisted_sample_still_picks_up_a_matching_preset(tmp_path, monkeypatch):
    """The convention lookup applies to the derived path too, not only overrides."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config_library" / "unified" / "docsplit").mkdir(parents=True)

    assert _publisher()._sample_label("docsplit.pdf")[2] == "docsplit"


# ---------------------------------------------------------------------------
# _normalize_marketplace_regions
# ---------------------------------------------------------------------------


def test_a_schema_1_1_regions_map_is_normalized_and_trimmed(tmp_path, monkeypatch):
    """Whitespace and a leading slash on the key are cleaned, per region.

    The host joins ``sellerBucket`` and ``templateKey`` into an S3 URL, so a
    retained leading ``/`` produces ``s3://bucket//path`` — a different, absent
    object — and a region name with trailing whitespace never matches the
    stack's region.
    """
    monkeypatch.chdir(tmp_path)
    item = {
        "featureId": "paid-feature",
        "regions": {
            "us-east-1": {
                "sellerBucket": "seller-use1",
                "templateKey": "/extensions/paid/template.yaml",
            },
            " us-west-2 ": {
                "sellerBucket": "  seller-usw2  ",
                "templateKey": "  extensions/paid/template.yaml  ",
            },
        },
    }

    assert _publisher()._normalize_marketplace_regions(item) == {
        "us-east-1": {
            "sellerBucket": "seller-use1",
            "templateKey": "extensions/paid/template.yaml",
        },
        "us-west-2": {
            "sellerBucket": "seller-usw2",
            "templateKey": "extensions/paid/template.yaml",
        },
    }


@pytest.mark.parametrize(
    ("bad_spec", "why"),
    [
        ("seller-bucket-only", "not a mapping"),
        ({"sellerBucket": "seller"}, "no templateKey"),
        ({"templateKey": "k.yaml"}, "no sellerBucket"),
        ({"sellerBucket": "  ", "templateKey": "k.yaml"}, "blank sellerBucket"),
        ({"sellerBucket": "seller", "templateKey": "  "}, "blank templateKey"),
    ],
)
def test_one_unusable_region_is_dropped_without_losing_the_others(
    bad_spec, why, tmp_path, monkeypatch
):
    """A half-filled region entry is skipped with a warning naming the region.

    Both halves are required because the host needs a bucket *and* a key to build
    the launch URL. Dropping just that region is the right blast radius: the
    extension stays available everywhere it is properly described, and the host
    reports it unavailable in the broken one.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()
    item = {
        "featureId": "paid-feature",
        "regions": {
            "eu-west-1": bad_spec,
            "us-east-1": {"sellerBucket": "seller-use1", "templateKey": "t.yaml"},
        },
    }

    result = pub._normalize_marketplace_regions(item)

    assert result == {
        "us-east-1": {"sellerBucket": "seller-use1", "templateKey": "t.yaml"}
    }
    assert len(pub.warnings) == 1
    assert "eu-west-1" in pub.warnings[0], why


def test_a_regions_key_of_the_wrong_type_warns_and_falls_back_to_legacy(
    tmp_path, monkeypatch
):
    """A list where a mapping belongs is reported, then the legacy fields are used.

    A YAML author writing ``regions:`` as a sequence of region names is the
    plausible mistake, and falling through rather than raising keeps an entry
    that also carries the legacy fields publishable.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()
    item = {
        "featureId": "paid-feature",
        "regions": ["us-east-1", "us-west-2"],
        "sellerBucket": "seller-legacy",
        "sellerBucketRegion": "us-east-1",
        "templateKey": "extensions/paid/template.yaml",
    }

    result = pub._normalize_marketplace_regions(item)

    assert result == {
        "us-east-1": {
            "sellerBucket": "seller-legacy",
            "templateKey": "extensions/paid/template.yaml",
        }
    }
    assert len(pub.warnings) == 1
    assert "must be a mapping" in pub.warnings[0]


def test_regions_all_unusable_still_falls_back_to_the_legacy_fields(
    tmp_path, monkeypatch
):
    """An empty normalized map is indistinguishable from an absent one, by design.

    A partially-migrated entry — a ``regions`` block that is present but
    describes nothing usable — keeps working from its legacy fields instead of
    publishing an extension nobody can install.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()
    item = {
        "featureId": "paid-feature",
        "regions": {"eu-west-1": {"sellerBucket": "only-a-bucket"}},
        "sellerBucket": "seller-legacy",
        "sellerBucketRegion": "us-west-2",
        "templateKey": "t.yaml",
    }

    assert pub._normalize_marketplace_regions(item) == {
        "us-west-2": {"sellerBucket": "seller-legacy", "templateKey": "t.yaml"}
    }


def test_a_complete_legacy_entry_is_folded_into_a_one_region_map(tmp_path, monkeypatch):
    """Schema 1.0's flat fields describe exactly one region, and only that one.

    The bug schema 1.1 exists to fix was reusing this single bucket in every
    region, so the normalized map must have exactly one key.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()
    item = {
        "featureId": "paid-feature",
        "sellerBucket": "seller-legacy",
        "sellerBucketRegion": " us-east-1 ",
        "templateKey": "/extensions/paid/template.yaml",
    }

    result = pub._normalize_marketplace_regions(item)

    assert result == {
        "us-east-1": {
            "sellerBucket": "seller-legacy",
            "templateKey": "extensions/paid/template.yaml",
        }
    }
    assert pub.warnings == [], "a well-formed legacy entry should not warn"


def test_a_legacy_entry_with_no_region_is_refused_rather_than_guessed(
    tmp_path, monkeypatch
):
    """No ``sellerBucketRegion`` means no placement — and a warning.

    Guessing is the exact defect schema 1.1 exists to fix: the old resolver used
    the entry's bucket in every region and handed customers a template whose
    baked ``CodeUri`` pointed at another region's objects. An empty map makes the
    host say "not available in <region>", which is the honest answer.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()
    item = {
        "featureId": "paid-feature",
        "sellerBucket": "seller-legacy",
        "templateKey": "t.yaml",
    }

    assert pub._normalize_marketplace_regions(item) == {}
    assert len(pub.warnings) == 1
    assert "no usable region mapping" in pub.warnings[0]


def test_an_entry_describing_no_artifacts_at_all_is_silent(tmp_path, monkeypatch):
    """A metadata-only entry is a normal, non-noisy case.

    Distinguished from the case above on purpose: an entry that names a bucket
    but cannot be placed is a mistake worth a warning, while one that names no
    artifacts is simply a catalog listing (discovery metadata plus a marketplace
    URL) and must not warn on every publish.
    """
    monkeypatch.chdir(tmp_path)
    pub = _recording_publisher()

    assert pub._normalize_marketplace_regions({"featureId": "listing-only"}) == {}
    assert pub.warnings == []


# ---------------------------------------------------------------------------
# _marketplace_license_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, ""])
def test_an_absent_license_mode_defaults_to_the_strictest_authority(
    raw, tmp_path, monkeypatch
):
    """The host-side default is ``marketplace-live``, and that asymmetry is deliberate.

    The extension side defaults to ``none`` so a missing value cannot lock a
    paying customer out; the host side defaults to the strictest check so a
    missing value cannot over-claim that a subscription was verified. Flipping
    this to match the extension side would make the host serve paid features
    without checking anything.
    """
    monkeypatch.chdir(tmp_path)
    item = {"featureId": "paid-feature"}
    if raw is not None:
        item["licenseMode"] = raw

    assert _publisher()._marketplace_license_mode(item) == "marketplace-live"


@pytest.mark.parametrize("mode", ["none", "simulated", "marketplace-live"])
def test_every_recognised_license_mode_passes_through(mode, tmp_path, monkeypatch):
    """The three modes mirror ``_LICENSE_MODES`` in ``check_feature_entitlement``.

    They are two independent literals in two packages, so a value accepted here
    and unknown there would publish a catalog the host rejects at runtime.
    """
    monkeypatch.chdir(tmp_path)

    assert (
        _publisher()._marketplace_license_mode(
            {"featureId": "paid-feature", "licenseMode": mode}
        )
        == mode
    )
    assert mode in IDPPublisher._LICENSE_MODES


def test_surrounding_whitespace_in_a_license_mode_is_tolerated(tmp_path, monkeypatch):
    """A hand-edited YAML value with a stray space is stripped, not rejected."""
    monkeypatch.chdir(tmp_path)

    assert (
        _publisher()._marketplace_license_mode(
            {"featureId": "paid-feature", "licenseMode": "  simulated  "}
        )
        == "simulated"
    )


@pytest.mark.parametrize(
    "bad", ["marketplace", "live", "None", "NONE", "simulate", True, 0]
)
def test_an_unrecognised_license_mode_aborts_publish(bad, tmp_path, monkeypatch):
    """A typo must be a hard error, never a silent downgrade.

    This field decides which authority confirms a paid subscription. Falling back
    to the default on an unrecognised value would turn ``licenseMdoe: none`` into
    a strict check (an outage for a working extension) or, worse in the other
    direction, a mis-spelled mode into no check at all. Note the comparison is
    case-sensitive and happens after ``str()``, so ``NONE`` and the boolean
    ``True`` are both refused.
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _publisher()._marketplace_license_mode(
            {"featureId": "paid-feature", "licenseMode": bad}
        )

    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _load_marketplace_features
# ---------------------------------------------------------------------------


def test_a_missing_marketplace_file_yields_an_oss_only_catalog(tmp_path, monkeypatch):
    """An OSS-only build has no marketplace file, and that is not an error."""
    monkeypatch.chdir(tmp_path)

    assert _publisher()._load_marketplace_features() == []


def test_malformed_marketplace_yaml_is_a_hard_error(tmp_path, monkeypatch):
    """A parse error must not publish a catalog that hides every paid extension."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        "features:\n  - featureId: a\n   broken: true\n",
    )

    with pytest.raises(SystemExit) as exc:
        _publisher()._load_marketplace_features()

    assert exc.value.code == 1


def test_a_minimal_marketplace_entry_is_filled_out_with_every_field(
    tmp_path, monkeypatch
):
    """The host reads exactly one shape, so every key is emitted, always.

    ``regions`` in particular is present-but-empty rather than absent, and the
    deprecated flat fields are emitted as empty strings, so a host older than the
    catalog fails loudly with "catalog entry is incomplete" instead of reusing
    one region's bucket everywhere. The full dict is asserted because a dropped
    key is a ``KeyError`` in a resolver, not a publish-time failure.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        'schemaVersion: "1.1"\nfeatures:\n  - featureId: paid-feature\n',
    )

    assert _publisher()._load_marketplace_features() == [
        {
            "featureId": "paid-feature",
            # No displayName given, so the id stands in — the catalog is sorted
            # by displayName, which would raise on None.
            "displayName": "paid-feature",
            "description": "",
            "iconUrl": "",
            "docsUrl": "",
            "showInNav": True,
            "source": "marketplace",
            "latestVersion": "",
            "productCode": "",
            "productId": "",
            "marketplaceListingUrl": "",
            "licenseMode": "marketplace-live",
            "regions": {},
            "sellerBucket": "",
            "sellerBucketRegion": "",
            "templateKey": "",
        }
    ]


def test_a_fully_specified_marketplace_entry_keeps_its_legacy_fields_verbatim(
    tmp_path, monkeypatch
):
    """Both schemas are emitted at once, on purpose.

    ``regions`` is what a current host reads; the flat fields are what an older
    one reads. An entry that supplies both must publish both unchanged, including
    the un-normalized leading slash in the legacy ``templateKey`` — the legacy
    host does its own joining.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        """
schemaVersion: "1.1"
features:
  - featureId: paid-feature
    displayName: Paid Feature
    description: A paid thing.
    iconUrl: https://example.invalid/icon.svg
    docsUrl: https://example.invalid/docs
    showInNav: false
    latestVersion: 2.1.0
    productCode: prod-code-abc
    productId: prod-id-xyz
    marketplaceListingUrl: https://aws.amazon.com/marketplace/pp/EXAMPLE
    licenseMode: simulated
    sellerBucket: seller-legacy
    sellerBucketRegion: us-east-1
    templateKey: /extensions/paid/template.yaml
    regions:
      us-east-1:
        sellerBucket: seller-use1
        templateKey: extensions/paid/template.yaml
""",
    )

    (entry,) = _publisher()._load_marketplace_features()

    assert entry["showInNav"] is False
    assert entry["licenseMode"] == "simulated"
    assert entry["productCode"] == "prod-code-abc"
    assert entry["productId"] == "prod-id-xyz"
    assert entry["regions"] == {
        "us-east-1": {
            "sellerBucket": "seller-use1",
            "templateKey": "extensions/paid/template.yaml",
        }
    }
    # Legacy fields pass through untouched — note the leading slash survives.
    assert entry["sellerBucket"] == "seller-legacy"
    assert entry["sellerBucketRegion"] == "us-east-1"
    assert entry["templateKey"] == "/extensions/paid/template.yaml"


def test_a_quoted_false_show_in_nav_is_read_as_visible(tmp_path, monkeypatch):
    """Sharp edge pinned: ``showInNav: "false"`` means True.

    The field is coerced with ``bool(item.get("showInNav", True))``, and every
    non-empty string is truthy. Unquoted YAML ``false`` parses to the boolean and
    works (asserted above); a quoted ``"false"`` parses to a string and silently
    turns nav visibility back on, putting an extension meant to be
    Browse-catalog-only into the sidebar of every stack.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        'features:\n  - featureId: paid-feature\n    showInNav: "false"\n',
    )

    assert _publisher()._load_marketplace_features()[0]["showInNav"] is True


@pytest.mark.parametrize(
    "entry", ["  - displayName: No Id\n", "  - paid-feature\n", "  - featureId: ''\n"]
)
def test_an_entry_with_no_feature_id_is_skipped_with_a_warning(
    entry, tmp_path, monkeypatch
):
    """An entry with no id cannot be de-duped or installed, so it is dropped.

    Skipping rather than aborting is the right call here — one malformed entry
    should not withhold every other paid extension from the catalog — but it does
    mean the warning is the only signal, which is why it is asserted.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        "features:\n" + entry + "  - featureId: good-feature\n    displayName: Good\n",
    )
    pub = _recording_publisher()

    entries = pub._load_marketplace_features()

    assert [e["featureId"] for e in entries] == ["good-feature"]
    assert len(pub.warnings) == 1
    assert "malformed marketplace extension entry" in pub.warnings[0]


@pytest.mark.parametrize(
    ("body", "shape"),
    [
        ("features:\n  paid-feature:\n    displayName: Paid\n", "a mapping"),
        ("features: paid-feature\n", "a bare string"),
    ],
)
def test_a_features_key_of_the_wrong_type_silently_yields_no_paid_extensions(
    body, shape, tmp_path, monkeypatch
):
    """DEFECT pinned: the marketplace loader tolerates what the OSS loader refuses.

    ``_bundled_feature_dirs`` checks ``isinstance(entries, list)`` and exits 1 on
    anything else. ``_load_marketplace_features`` does not: it iterates whatever
    ``features`` holds, so a mapping yields its keys and a string yields its
    characters, each of which fails the ``isinstance(item, dict)`` test and is
    warned about and skipped. The result is an empty marketplace list, a
    successful publish, and a catalog with no paid extensions in it — precisely
    the "broken catalog would silently hide paid extensions" outcome the
    function's own docstring says it prevents.

    The warnings are the only trace, and for the string case there is one per
    character. Pinned rather than fixed: the fix is the same ``isinstance`` guard
    the OSS loader already has.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config_library" / "extensions-marketplace.yaml", body)
    pub = _recording_publisher()

    assert pub._load_marketplace_features() == [], shape
    assert pub.warnings, "not even a warning was emitted"


def test_marketplace_entries_reach_the_written_catalog(tmp_path, monkeypatch):
    """End to end: the loader's output is what ``catalog.json`` carries.

    ``write_catalog_file`` sorts by lower-cased ``displayName`` and lets an OSS
    entry win a ``featureId`` collision. The sort is what makes the file's bytes
    stable across publishes, which matters because the file is hashed into the
    config-library sync.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-marketplace.yaml",
        """
features:
  - featureId: zeta-paid
    displayName: Zeta Paid
    licenseMode: none
  - featureId: alpha-paid
    displayName: alpha Paid
""",
    )
    pub = _publisher()

    catalog = pub.write_catalog_file(
        [
            {
                "featureId": "mid-oss",
                "displayName": "Mid OSS",
                "description": "",
                "iconUrl": "",
                "source": "oss",
                "latestVersion": "1.0.0",
            }
        ]
    )

    assert [f["featureId"] for f in catalog["features"]] == [
        "alpha-paid",
        "mid-oss",
        "zeta-paid",
    ]
    on_disk = json.loads(
        (tmp_path / "config_library" / "catalog.json").read_text(encoding="utf-8")
    )
    assert on_disk == catalog
    by_id = {f["featureId"]: f for f in catalog["features"]}
    assert by_id["alpha-paid"]["licenseMode"] == "marketplace-live"
    assert by_id["zeta-paid"]["licenseMode"] == "none"


# ---------------------------------------------------------------------------
# build_and_upload_sample_features
# ---------------------------------------------------------------------------


def test_no_bundled_feature_directories_returns_empty_results(tmp_path, monkeypatch):
    """A trimmed checkout publishes with an empty feature bucket, not a failure.

    The three-tuple shape matters: the caller unpacks it straight into
    ``build_main_template``'s ``sample_features_hash`` / ``sample_features_list``.
    """
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config_library" / "extensions-oss.yaml",
        'schemaVersion: "1.0"\nfeatures:\n  - path: feature-platform/absent\n',
    )

    assert _publisher().build_and_upload_sample_features() == ("", [], [])


def test_a_missing_feature_sdk_is_reported_as_an_actionable_error(
    tmp_path, monkeypatch
):
    """The lazy import's failure must name the install command.

    ``idp_feature_sdk`` lives in ``lib/idp_feature_sdk/`` and some CI
    environments strip feature trees, so this path is reachable in practice; a
    bare ``ModuleNotFoundError`` from inside a publish sequence would not say
    what to do about it.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "feature-platform" / "sample-feature").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "idp_feature_sdk.manifest", None)

    with pytest.raises(RuntimeError) as exc:
        _publisher().build_and_upload_sample_features()

    assert "pip install -e lib/idp_feature_sdk/" in str(exc.value)


def _fake_single_feature(pub, results):
    """Stub ``_build_and_upload_single_feature``, recording which dirs it saw."""
    seen = []

    def fake(feature_dir, load_manifest, FeaturePublisher):
        seen.append(feature_dir)
        return results[feature_dir.name]

    pub._build_and_upload_single_feature = fake
    return seen


def _two_feature_tree(root):
    for name, version in (("feat-a", "1.0.0"), ("feat-b", "2.0.0")):
        d = root / "feature-platform" / name
        _write(d / "feature.yaml", f"featureId: {name}\nversion: {version}\n")
        _write(d / "template.yaml", "Resources: {}\n")
        _write(d / "feature-ui" / "src" / "index.tsx", "export const x = 1;\n")
        _write(d / "feature-ui" / "package.json", '{"name":"ui"}')
    _write(
        root / "config_library" / "extensions-oss.yaml",
        'schemaVersion: "1.0"\n'
        "features:\n"
        "  - path: feature-platform/feat-a\n"
        "  - path: feature-platform/feat-b\n",
    )


def test_every_bundled_feature_is_built_and_its_results_merged(tmp_path, monkeypatch):
    """File lists concatenate, catalog entries collect, and a None entry is dropped.

    ``_build_and_upload_single_feature`` may legitimately return no catalog entry,
    and appending that None would put a null into ``catalog.json``'s feature list
    and break the resolver's sort.
    """
    monkeypatch.chdir(tmp_path)
    _two_feature_tree(tmp_path)
    pub = _publisher()
    seen = _fake_single_feature(
        pub,
        {
            "feat-a": (["template.yaml", "latest.json"], {"featureId": "feat-a"}),
            "feat-b": (["template.yaml"], None),
        },
    )

    checksum, file_list, catalog_entries = pub.build_and_upload_sample_features()

    assert [d.name for d in seen] == ["feat-a", "feat-b"]
    assert file_list == ["template.yaml", "latest.json", "template.yaml"]
    assert catalog_entries == [{"featureId": "feat-a"}]
    assert len(checksum) == 16
    assert all(c in "0123456789abcdef" for c in checksum)


def test_the_bundled_feature_hash_moves_when_a_feature_source_changes(
    tmp_path, monkeypatch
):
    """The hash is the main template's re-run trigger for ``PublishSampleFeature``.

    CloudFormation only re-invokes that custom resource when one of its
    properties changes, so a hash that does not move for an edited feature
    template leaves the previously-copied artifacts in the feature bucket. A
    fresh publisher is used for each measurement because the checksum helpers
    memoize per instance.
    """
    monkeypatch.chdir(tmp_path)
    _two_feature_tree(tmp_path)
    results = {
        "feat-a": ([], None),
        "feat-b": ([], None),
    }

    def measure():
        pub = _publisher()
        _fake_single_feature(pub, results)
        return pub.build_and_upload_sample_features()[0]

    before = measure()
    assert measure() == before, "the hash churns with no source change"

    (
        tmp_path / "feature-platform" / "feat-b" / "feature-ui" / "src" / "index.tsx"
    ).write_text("export const x = 2;\n", encoding="utf-8")

    assert measure() != before


# ---------------------------------------------------------------------------
# _build_and_upload_single_feature
# ---------------------------------------------------------------------------


def _manifest(version=_FEATURE_VERSION, **overrides):
    """A duck-typed feature manifest matching what the publisher reads."""
    fields = {
        "featureId": _FEATURE_ID,
        "version": version,
        "displayName": "Sample Feature",
        "description": "The reference contract sample.",
        "iconUrl": "https://example.invalid/icon.svg",
        "docsUrl": "https://example.invalid/docs",
        "showInNav": False,
        "capabilities": ["custom-api"],
        "defaultParameters": {"LogLevel": "INFO"},
        "marketplace": types.SimpleNamespace(productCode=None, listingUrl=None),
        "configPreset": None,
        "agentSource": None,
        "template": types.SimpleNamespace(path="template.yaml"),
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _feature_tree(root, bundle_text=f'{{"version":"{_FEATURE_VERSION}"}}'):
    """A built feature directory: source template plus a Vite ``dist`` bundle."""
    d = root / "feature-platform" / _FEATURE_ID
    _write(
        d / "feature.yaml", f"featureId: {_FEATURE_ID}\nversion: {_FEATURE_VERSION}\n"
    )
    _write(d / "template.yaml", "Description: the SOURCE template\n")
    _write(d / "feature-ui" / "src" / "index.tsx", "export const x = 1;\n")
    _write(d / "feature-ui" / "dist" / "ui-bundle.js", bundle_text)
    return d


class _FakeFeaturePublisher:
    """Stands in for ``idp_feature_sdk.publisher.FeaturePublisher``.

    Records construction and calls, and writes the UI bundle on ``build`` the way
    a real Vite build does, so the publisher's post-build version check has
    something real to read.
    """

    instances = []

    def __init__(self, feature_dir, console=None):
        self.feature_dir = Path(feature_dir)
        self.calls = []
        self.build_raises = None
        self.bundle_text = f'{{"version":"{_FEATURE_VERSION}"}}'
        type(self).instances.append(self)

    def validate(self):
        self.calls.append("validate")
        return _manifest()

    def build(self, manifest):
        self.calls.append("build")
        if self.build_raises is not None:
            raise self.build_raises
        bundle = self.feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text(self.bundle_text, encoding="utf-8")


@pytest.fixture
def feature_publisher_factory():
    """A fresh ``FeaturePublisher`` class per test, with no shared instance list."""

    class Factory(_FakeFeaturePublisher):
        instances = []

    yield Factory
    Factory.instances = []


def _packaging_recorder(pub, packaged_body="Description: the PACKAGED template\n"):
    """Stub ``build_and_package_template``, writing the artifact it must produce."""
    calls = []

    def fake(directory, force_rebuild=False):
        calls.append({"directory": directory, "force_rebuild": force_rebuild})
        if packaged_body is not None:
            out = Path(directory) / ".aws-sam" / "packaged.yaml"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(packaged_body, encoding="utf-8")

    pub.build_and_package_template = fake
    return calls


def _base(feature_id=_FEATURE_ID):
    return f"{_PREFIX}/extensions/{feature_id}"


@mock_aws
def test_a_fresh_feature_is_validated_built_packaged_and_uploaded(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """The first publish of a feature runs the whole chain and records the cache.

    ``force_rebuild=True`` on ``build_and_package_template`` is deliberate — the
    feature's ``sam package`` must rewrite its local ``CodeUri`` values to
    ``s3://`` on every publish — and the ``.checksum`` written afterwards is what
    makes the next publish cheap. The packaged template, not the source one, is
    what reaches S3: the source still carries unrewritten ``CodeUri`` paths that
    CloudFormation cannot resolve.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    pub = _publisher(s3)
    packaging = _packaging_recorder(pub)

    file_list, catalog_entry = pub._build_and_upload_single_feature(
        feature_dir, lambda d: _manifest(), feature_publisher_factory
    )

    built = feature_publisher_factory.instances
    assert len(built) == 1
    assert built[0].calls == ["validate", "build"]
    assert packaging == [{"directory": str(feature_dir), "force_rebuild": True}]
    assert (feature_dir / ".checksum").read_text(encoding="utf-8")

    template = s3.get_object(Bucket=_BUCKET, Key=f"{_base()}/template.yaml")
    assert template["Body"].read().decode() == "Description: the PACKAGED template\n"
    assert file_list == [
        f"{_FEATURE_VERSION}/manifest.json",
        f"{_FEATURE_VERSION}/ui-bundle.js",
        "latest.json",
        "template.yaml",
    ]
    assert catalog_entry == {
        "featureId": _FEATURE_ID,
        "displayName": "Sample Feature",
        "description": "The reference contract sample.",
        "iconUrl": "https://example.invalid/icon.svg",
        "docsUrl": "https://example.invalid/docs",
        "showInNav": False,
        "source": "oss",
        "licenseMode": "none",
        "latestVersion": _FEATURE_VERSION,
        "artifactBucket": _BUCKET,
        "artifactPrefix": _base(),
    }
    # The catalog's artifactPrefix is version-free: the feature template
    # self-locates its versioned artifacts from its baked FEATURE_VERSION.
    assert _VERSION not in catalog_entry["artifactPrefix"]


@mock_aws
def test_a_second_publish_of_unchanged_source_uses_the_cached_bundle(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """An unchanged feature skips the npm build entirely.

    The cache key is computed from the same ``_get_feature_deps`` list, so the
    second run must not construct a ``FeaturePublisher`` at all — and it must
    still upload, because a cached build is not a cached upload.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    load_manifest = lambda d: _manifest()  # noqa: E731

    first = _publisher(s3)
    _packaging_recorder(first)
    first._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )
    assert len(feature_publisher_factory.instances) == 1

    second = _publisher(s3)
    _packaging_recorder(second)
    file_list, _ = second._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )

    assert len(feature_publisher_factory.instances) == 1, "the feature was rebuilt"
    assert f"{_FEATURE_VERSION}/ui-bundle.js" in file_list


@mock_aws
def test_an_edited_feature_source_invalidates_the_cache(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """A changed dependency file forces the rebuild the checksum exists for."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    load_manifest = lambda d: _manifest()  # noqa: E731

    first = _publisher(s3)
    _packaging_recorder(first)
    first._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )

    (feature_dir / "feature-ui" / "src" / "index.tsx").write_text(
        "export const x = 2;\n", encoding="utf-8"
    )
    second = _publisher(s3)
    _packaging_recorder(second)
    second._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )

    assert len(feature_publisher_factory.instances) == 2


@mock_aws
def test_a_cached_bundle_without_the_manifest_version_is_rebuilt(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """A checksum hit is not enough — the bundle must carry the version too.

    Vite bakes ``feature.yaml``'s version into the bundle, and the host's
    FeatureLoader refuses to run a bundle whose self-reported version differs
    from the registered one ("bundle version X does not match registered Y"),
    silently serving old code. A ``dist/`` that predates a version bump is
    therefore treated as a cache MISS rather than trusted.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    load_manifest = lambda d: _manifest()  # noqa: E731

    first = _publisher(s3)
    _packaging_recorder(first)
    first._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )

    # Same sources (so the checksum still matches) but a stale bundle.
    (feature_dir / "feature-ui" / "dist" / "ui-bundle.js").write_text(
        '{"version":"1.0.0"}', encoding="utf-8"
    )
    second = _recording_publisher(s3)
    _packaging_recorder(second)
    second._build_and_upload_single_feature(
        feature_dir, load_manifest, feature_publisher_factory
    )

    assert len(feature_publisher_factory.instances) == 2
    assert any("does not carry version" in w for w in second.warnings)


@mock_aws
def test_a_cached_feature_with_a_missing_agent_zip_is_rebuilt(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """A git-ignored agent-source zip that was cleaned away is a cache miss.

    The zip is produced by the package step, is git-ignored, and gets cleaned
    between runs. A cache hit skips ``publisher.build()``, so without this check
    the upload step would find no zip — and install-time CodeBuild would read
    its ``Source.Location`` and fail with a 404.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    agent_source = types.SimpleNamespace(artifactPath="agent-source.zip")
    load_manifest = lambda d: _manifest(agentSource=agent_source)  # noqa: E731

    class ZipBuildingPublisher(feature_publisher_factory):
        instances = []

        def validate(self):
            self.calls.append("validate")
            return _manifest(agentSource=agent_source)

        def build(self, manifest):
            super().build(manifest)
            (self.feature_dir / "agent-source.zip").write_bytes(b"PK\x03\x04payload")

    first = _publisher(s3)
    _packaging_recorder(first)
    first._build_and_upload_single_feature(
        feature_dir, load_manifest, ZipBuildingPublisher
    )
    assert len(ZipBuildingPublisher.instances) == 1

    # Simulate the clean: the zip is gone but every source file is unchanged.
    (feature_dir / "agent-source.zip").unlink()
    second = _recording_publisher(s3)
    _packaging_recorder(second)
    second._build_and_upload_single_feature(
        feature_dir, load_manifest, ZipBuildingPublisher
    )

    assert len(ZipBuildingPublisher.instances) == 2
    assert any("agent-source.zip missing" in w for w in second.warnings)


@mock_aws
def test_a_feature_build_failure_aborts_publish(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """Any exception out of validate/build stops publish before uploading.

    Uploading after a failed build would put a half-built bundle at the
    version-free ``template.yaml`` key, which is the key every existing stack's
    Update reads.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)

    class FailingPublisher(feature_publisher_factory):
        instances = []

        def build(self, manifest):
            raise RuntimeError("vite: 2 errors")

    pub = _recording_publisher(s3)
    _packaging_recorder(pub)

    with pytest.raises(SystemExit) as exc:
        pub._build_and_upload_single_feature(
            feature_dir, lambda d: _manifest(), FailingPublisher
        )

    assert exc.value.code == 1
    assert any("vite: 2 errors" in e for e in pub.errors)
    assert "Contents" not in s3.list_objects_v2(Bucket=_BUCKET)


@mock_aws
def test_a_build_that_produces_a_wrong_version_bundle_is_refused(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """The final guard fires even when the rebuild itself succeeded.

    Uploading a bundle whose baked version differs from the manifest's puts
    wrong-version code at the manifest-version S3 key, and the host then refuses
    to load it at runtime — a failure that looks like a UI bug, not a publish
    bug. So the publisher fails loudly instead.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)

    class WrongVersionPublisher(feature_publisher_factory):
        instances = []

        def build(self, manifest):
            self.bundle_text = '{"version":"9.9.9"}'
            super().build(manifest)

    pub = _recording_publisher(s3)
    _packaging_recorder(pub)

    with pytest.raises(SystemExit) as exc:
        pub._build_and_upload_single_feature(
            feature_dir, lambda d: _manifest(), WrongVersionPublisher
        )

    assert exc.value.code == 1
    assert any("does not contain" in e for e in pub.errors)
    assert "Contents" not in s3.list_objects_v2(Bucket=_BUCKET)


@_needs_a_non_root_user
@mock_aws
def test_an_unreadable_checksum_file_is_treated_as_a_cache_miss(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """A ``.checksum`` that exists but cannot be read must rebuild, not crash.

    Driven with mode 000, which is what a file left behind by a root-owned
    container build looks like to a developer's user. The safe direction is a
    cache MISS: guessing "unchanged" from an unreadable cache would publish
    whatever ``dist/`` happens to hold.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    checksum = feature_dir / ".checksum"
    checksum.write_text("whatever", encoding="utf-8")
    checksum.chmod(0o000)
    pub = _publisher(s3)
    _packaging_recorder(pub)

    try:
        pub._build_and_upload_single_feature(
            feature_dir, lambda d: _manifest(), feature_publisher_factory
        )
    finally:
        checksum.chmod(0o644)

    assert len(feature_publisher_factory.instances) == 1


@mock_aws
def test_a_build_that_produces_no_bundle_at_all_is_refused(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """An absent bundle fails the same guard a wrong-version one does.

    Distinct from the wrong-version case because it takes a different branch: the
    version check reads the file, so it has to answer False rather than raise
    when there is no file. A ``npm run build`` that exits 0 without emitting
    ``dist/ui-bundle.js`` — a misconfigured Vite output path — lands here.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)

    class NoBundlePublisher(feature_publisher_factory):
        instances = []

        def build(self, manifest):
            self.calls.append("build")
            bundle = self.feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
            if bundle.is_file():
                bundle.unlink()

    pub = _recording_publisher(s3)
    _packaging_recorder(pub)

    with pytest.raises(SystemExit) as exc:
        pub._build_and_upload_single_feature(
            feature_dir, lambda d: _manifest(), NoBundlePublisher
        )

    assert exc.value.code == 1
    assert any("does not contain" in e for e in pub.errors)


@_needs_a_non_root_user
@mock_aws
def test_an_unreadable_bundle_is_refused_rather_than_published(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """If the version cannot be read from the bundle, publish stops.

    The version check has to answer a question about the file's contents, so an
    unreadable file is treated as a failed check rather than a passed one.
    Publishing a bundle whose version could not be confirmed is exactly what the
    guard exists to prevent.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"

    class UnreadableBundlePublisher(feature_publisher_factory):
        instances = []

        def build(self, manifest):
            super().build(manifest)
            bundle.chmod(0o000)

    pub = _recording_publisher(s3)
    _packaging_recorder(pub)

    try:
        with pytest.raises(SystemExit) as exc:
            pub._build_and_upload_single_feature(
                feature_dir, lambda d: _manifest(), UnreadableBundlePublisher
            )
    finally:
        bundle.chmod(0o644)

    assert exc.value.code == 1
    assert any("does not contain" in e for e in pub.errors)


@mock_aws
def test_a_missing_packaged_template_aborts_publish(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """``sam package`` must leave ``.aws-sam/packaged.yaml`` behind.

    Falling back to the source template here would publish one whose ``CodeUri``
    still points at local directories, and CloudFormation would reject it at
    install.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    pub = _recording_publisher(s3)
    _packaging_recorder(pub, packaged_body=None)

    with pytest.raises(SystemExit) as exc:
        pub._build_and_upload_single_feature(
            feature_dir, lambda d: _manifest(), feature_publisher_factory
        )

    assert exc.value.code == 1
    assert any("packaged template" in e for e in pub.errors)


@mock_aws
def test_an_unwritable_checksum_file_warns_but_does_not_stop_the_publish(
    tmp_path, monkeypatch, aws_credentials, feature_publisher_factory
):
    """The build cache is an optimisation, so failing to record it is not fatal.

    Driven by making ``.checksum`` a directory, so ``write_text`` raises
    ``IsADirectoryError``. The publish must still complete — the next one simply
    rebuilds.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    (feature_dir / ".checksum").mkdir()
    pub = _recording_publisher(s3)
    _packaging_recorder(pub)

    file_list, catalog_entry = pub._build_and_upload_single_feature(
        feature_dir, lambda d: _manifest(), feature_publisher_factory
    )

    assert "template.yaml" in file_list
    assert catalog_entry["featureId"] == _FEATURE_ID
    assert any("Could not write" in w for w in pub.warnings)


# ---------------------------------------------------------------------------
# _upload_sample_feature_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset_path", "content_type"),
    [
        ("config/preset.yaml", "application/x-yaml"),
        ("config/preset.YML", "application/x-yaml"),
        ("config/preset.json", "application/json"),
        ("config/preset", "application/json"),
    ],
)
@mock_aws
def test_a_config_preset_is_uploaded_under_the_version_at_its_declared_path(
    preset_path, content_type, tmp_path, monkeypatch, aws_credentials
):
    """The preset's relative path must be preserved under ``<version>/``.

    The feature stack's ui-deployer downloads
    ``<FeatureArtifactPrefix>/<version>/<configPreset.path>`` at install to call
    ``applyFeatureConfigPreset``, so the nesting has to survive the upload
    verbatim — flattening ``config/preset.yaml`` to ``preset.yaml`` gives the
    installer a 404. The Content-Type is chosen from the suffix, and anything
    that is not ``.yaml``/``.yml`` is declared JSON.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    _write(feature_dir / preset_path, "classes: []\n")
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
    pub = _publisher(s3)

    uploaded = pub._upload_sample_feature_artifacts(
        feature_dir,
        _manifest(configPreset=types.SimpleNamespace(path=preset_path)),
        bundle,
    )

    key = f"{_base()}/{_FEATURE_VERSION}/{preset_path}"
    obj = s3.get_object(Bucket=_BUCKET, Key=key)
    assert obj["ContentType"] == content_type
    assert obj["Body"].read() == b"classes: []\n"
    assert f"{_FEATURE_VERSION}/{preset_path}" in uploaded
    assert uploaded == sorted(uploaded), "the returned file list must be sorted"


@mock_aws
def test_a_declared_config_preset_with_no_file_aborts_publish(
    tmp_path, monkeypatch, aws_credentials
):
    """A manifest promising a preset that is not there must fail at publish.

    Otherwise the feature installs and then its ui-deployer fails mid-stack
    trying to fetch the preset, which surfaces as a CloudFormation rollback with
    no useful message.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
    pub = _recording_publisher(s3)

    with pytest.raises(SystemExit) as exc:
        pub._upload_sample_feature_artifacts(
            feature_dir,
            _manifest(configPreset=types.SimpleNamespace(path="config/absent.yaml")),
            bundle,
        )

    assert exc.value.code == 1
    assert any("configPreset.path" in e for e in pub.errors)


@mock_aws
def test_a_declared_agent_source_with_no_zip_aborts_publish(
    tmp_path, monkeypatch, aws_credentials
):
    """A missing agent-source zip is a publish-time failure, not an install one.

    The feature stack's CodeBuild project reads
    ``<prefix>/extensions/<id>/<version>/<artifactPath>`` as its
    ``Source.Location``; publishing without it produced a NoSuchKey at install
    time, which is the regression this guard exists for.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
    pub = _recording_publisher(s3)

    with pytest.raises(SystemExit) as exc:
        pub._upload_sample_feature_artifacts(
            feature_dir,
            _manifest(
                agentSource=types.SimpleNamespace(artifactPath="agent-source.zip")
            ),
            bundle,
        )

    assert exc.value.code == 1
    assert any("agentSource.artifactPath" in e for e in pub.errors)


@mock_aws
def test_the_manifest_and_latest_objects_carry_the_metadata_the_host_reads(
    tmp_path, monkeypatch, aws_credentials
):
    """``manifest.json`` is versioned; ``latest.json`` is not, and both are JSON.

    ``latest.json`` sits at the version-free base so it always names the newest
    publish — the same arrangement as ``idp-main-latest.json`` for the main
    template. Its ``publishedAt`` must be a UTC instant with a ``Z`` suffix
    rather than ``+00:00``, because that is the form the UI parses.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"

    _publisher(s3)._upload_sample_feature_artifacts(feature_dir, _manifest(), bundle)

    versioned = s3.get_object(
        Bucket=_BUCKET, Key=f"{_base()}/{_FEATURE_VERSION}/manifest.json"
    )
    assert versioned["ContentType"] == "application/json"
    assert json.loads(versioned["Body"].read()) == {
        "featureId": _FEATURE_ID,
        "displayName": "Sample Feature",
        "version": _FEATURE_VERSION,
        "description": "The reference contract sample.",
        "iconUrl": "https://example.invalid/icon.svg",
        "capabilities": ["custom-api"],
        "defaultParameters": {"LogLevel": "INFO"},
        "marketplace": {"productCode": None, "listingUrl": None},
    }

    latest = json.loads(
        s3.get_object(Bucket=_BUCKET, Key=f"{_base()}/latest.json")["Body"].read()
    )
    assert latest["featureId"] == _FEATURE_ID
    assert latest["version"] == _FEATURE_VERSION
    assert latest["publishedAt"].endswith("Z")
    assert "+00:00" not in latest["publishedAt"]


@mock_aws
def test_the_ui_bundle_is_declared_as_javascript(
    tmp_path, monkeypatch, aws_credentials
):
    """The bundle is fetched by the browser, so its Content-Type matters.

    An object served as ``binary/octet-stream`` (the S3 default) is refused by a
    strict-MIME browser when loaded as a module, which is a blank feature panel
    with a console error and no server-side trace.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"

    _publisher(s3)._upload_sample_feature_artifacts(feature_dir, _manifest(), bundle)

    obj = s3.get_object(
        Bucket=_BUCKET, Key=f"{_base()}/{_FEATURE_VERSION}/ui-bundle.js"
    )
    assert obj["ContentType"] == "application/javascript"
    assert obj["Body"].read() == bundle.read_bytes()


@mock_aws
def test_the_source_template_is_used_when_no_packaged_one_is_given(
    tmp_path, monkeypatch, aws_credentials
):
    """Without a packaged template the manifest's declared path is read.

    Token baking has to happen in both cases, so the fallback path resolves
    ``manifest.template.path`` relative to the feature directory. A digest
    comparison proves the bytes came from that file and were then substituted,
    not copied verbatim.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    feature_dir = _feature_tree(tmp_path)
    (feature_dir / "template.yaml").write_text(
        "Version: '<FEATURE_VERSION_TOKEN>'\nPrefix: '<FEATURE_ARTIFACT_PREFIX_TOKEN>'\n",
        encoding="utf-8",
    )
    bundle = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"

    _publisher(s3)._upload_sample_feature_artifacts(feature_dir, _manifest(), bundle)

    text = (
        s3.get_object(Bucket=_BUCKET, Key=f"{_base()}/template.yaml")["Body"]
        .read()
        .decode()
    )
    assert text == f"Version: '{_FEATURE_VERSION}'\nPrefix: '{_base()}'\n"
    assert (
        hashlib.sha256(text.encode()).hexdigest()
        != hashlib.sha256((feature_dir / "template.yaml").read_bytes()).hexdigest()
    )
