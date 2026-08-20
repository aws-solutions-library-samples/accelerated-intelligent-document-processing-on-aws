"""Unit tests for the list_installed_features Lambda.

`latestVersion` (for the "Update available" badge) is read at RUNTIME from each
extension's published `<base>/latest.json`, falling back to catalog.json in
ConfigurationBucket. That decoupling is the point: the catalog is frozen at
host-publish time, so reading it alone meant an extension release was invisible
until the whole accelerator was re-released and the customer took it.

No bucket listing anywhere: one GetObject of the catalog plus at most one
GetObject of latest.json per installed feature, memoized and fail-soft.
"""

from __future__ import annotations

import json

import boto3
import pytest
from _helpers import make_appsync_event

_CATALOG_KEY = "config_library/catalog.json"


def _preload(
    monkeypatch, table_name: str, load_lambda, configuration_bucket: str | None = None
):
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", table_name)
    if configuration_bucket:
        monkeypatch.setenv("CONFIGURATION_BUCKET", configuration_bucket)
    else:
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("list_installed_features")


def _seed(table_name: str, items):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(table_name)
    for item in items:
        table.put_item(Item=item)


def _put_catalog(bucket: str, features: list):
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket,
        Key=_CATALOG_KEY,
        Body=json.dumps({"schemaVersion": "1.0", "features": features}).encode("utf-8"),
    )


def test_empty_returns_empty_list(monkeypatch, installed_features_table, load_lambda):
    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert result == []


def test_returns_features_sorted_by_display_name(
    monkeypatch, installed_features_table, load_lambda
):
    _seed(
        installed_features_table,
        [
            {
                "featureId": "zeta",
                "displayName": "Zeta Feature",
                "installedVersion": "1.0.0",
                "stackName": "idp-feature-zeta",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/zeta/v1.0.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            },
            {
                "featureId": "alpha",
                "displayName": "Alpha Feature",
                "installedVersion": "2.0.0",
                "stackName": "idp-feature-alpha",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/alpha/v2.0.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            },
        ],
    )

    mod = _preload(monkeypatch, installed_features_table, load_lambda)
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert [f["featureId"] for f in result] == ["alpha", "zeta"]
    # No catalog configured → latestVersion None and updateAvailable false.
    assert all(f["latestVersion"] is None for f in result)
    assert all(f["updateAvailable"] is False for f in result)


def test_update_available_when_catalog_version_differs(
    monkeypatch, mock_stack, load_lambda
):
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    _seed(
        table_name,
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Docs",
                "installedVersion": "1.0.0",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/docs-by-status/v1.0.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.1.0"}],
    )

    mod = _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    assert len(result) == 1
    assert result[0]["installedVersion"] == "1.0.0"
    assert result[0]["latestVersion"] == "1.1.0"
    assert result[0]["updateAvailable"] is True


def test_no_update_when_catalog_is_BEHIND_installed(
    monkeypatch, mock_stack, load_lambda
):
    """A catalog OLDER than the installed version is not an update.

    This is the routine case, not an edge case: `idp-feature-cli deploy
    --from-code` (the documented dev loop) installs a newer extension
    immediately, while catalog.json only refreshes on a host stack
    create/update. The previous `latest != installed` check reported
    "Update available: v0.1.0" to an admin already running v0.1.1 — an
    invitation to downgrade.
    """
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    _seed(
        table_name,
        [
            {
                "featureId": "confbench-testset",
                "displayName": "Test Set - ConfBench",
                "installedVersion": "0.1.1",
                "stackName": "s",
                "stackRegion": "us-west-2",
                "uiBundlePath": "features/confbench-testset/v0.1.1/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(
        bucket,
        [{"featureId": "confbench-testset", "source": "oss", "latestVersion": "0.1.0"}],
    )

    mod = _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    assert result[0]["installedVersion"] == "0.1.1"
    assert result[0]["latestVersion"] == "0.1.0"
    assert result[0]["updateAvailable"] is False


def test_version_comparison_is_numeric_not_lexicographic(
    monkeypatch, mock_stack, load_lambda
):
    """0.1.10 > 0.1.9 numerically, even though it sorts earlier as a string."""
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    _seed(
        table_name,
        [
            {
                "featureId": "f",
                "displayName": "F",
                "installedVersion": "0.1.9",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/f/v0.1.9/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(bucket, [{"featureId": "f", "latestVersion": "0.1.10"}])

    mod = _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)
    assert (
        mod.handler(make_appsync_event("listInstalledFeatures"), None)[0][
            "updateAvailable"
        ]
        is True
    )


def test_prerelease_ordering(monkeypatch, mock_stack, load_lambda):
    """SemVer 11: a prerelease has LOWER precedence than its release, so
    1.0.0 is an update over 1.0.0-rc1 but 1.0.0-rc1 is not over 1.0.0."""
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    for installed, latest, expected in (
        ("1.0.0-rc1", "1.0.0", True),
        ("1.0.0", "1.0.0-rc1", False),
    ):
        _seed(
            table_name,
            [
                {
                    "featureId": "p",
                    "displayName": "P",
                    "installedVersion": installed,
                    "stackName": "s",
                    "stackRegion": "us-east-1",
                    "uiBundlePath": "features/p/",
                    "installedAt": "2026-01-01T00:00:00Z",
                }
            ],
        )
        _put_catalog(bucket, [{"featureId": "p", "latestVersion": latest}])
        mod = _preload(
            monkeypatch, table_name, load_lambda, configuration_bucket=bucket
        )
        got = mod.handler(make_appsync_event("listInstalledFeatures"), None)[0][
            "updateAvailable"
        ]
        assert got is expected, f"installed={installed} latest={latest}"


def test_dotted_prerelease_identifiers_compare_numerically(
    monkeypatch, mock_stack, load_lambda
):
    """SemVer 11.4: a NUMERIC prerelease identifier compares numerically, so
    rc.10 > rc.2 — the opposite of what string comparison gives.

    Undotted `rc10` vs `rc2` is deliberately NOT an update: those are single
    alphanumeric identifiers, which 11.4 compares in ASCII order, making
    `rc10` genuinely lower than `rc2`. Only dot-separated numeric parts get
    numeric treatment.
    """
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    for installed, latest, expected in (
        ("1.0.0-rc.2", "1.0.0-rc.10", True),  # numeric identifier
        ("1.0.0-rc.10", "1.0.0-rc.2", False),
        ("1.0.0-alpha", "1.0.0-beta", True),  # alphanumeric, ASCII order
        ("1.0.0-alpha.1", "1.0.0-alpha.beta", True),  # numeric < alphanumeric
        ("1.0.0", "1.0.0+build", False),  # 10: build metadata is not precedence
    ):
        _seed(
            table_name,
            [
                {
                    "featureId": "pre",
                    "displayName": "Pre",
                    "installedVersion": installed,
                    "stackName": "s",
                    "stackRegion": "us-east-1",
                    "uiBundlePath": "features/pre/",
                    "installedAt": "2026-01-01T00:00:00Z",
                }
            ],
        )
        _put_catalog(bucket, [{"featureId": "pre", "latestVersion": latest}])
        mod = _preload(
            monkeypatch, table_name, load_lambda, configuration_bucket=bucket
        )
        got = mod.handler(make_appsync_event("listInstalledFeatures"), None)[0][
            "updateAvailable"
        ]
        assert got is expected, f"installed={installed} latest={latest}"


def test_unparseable_version_falls_back_to_inequality(
    monkeypatch, mock_stack, load_lambda
):
    """Non-SemVer strings keep the old behavior rather than silently hiding the
    badge — better to over-report than to strand a real update."""
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    _seed(
        table_name,
        [
            {
                "featureId": "odd",
                "displayName": "Odd",
                "installedVersion": "latest",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/odd/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(bucket, [{"featureId": "odd", "latestVersion": "2026-08-01"}])

    mod = _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)
    assert (
        mod.handler(make_appsync_event("listInstalledFeatures"), None)[0][
            "updateAvailable"
        ]
        is True
    )


def test_update_not_available_when_versions_match(monkeypatch, mock_stack, load_lambda):
    table_name = mock_stack["table_name"]
    bucket = mock_stack["bucket"]

    _seed(
        table_name,
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Docs",
                "installedVersion": "1.1.0",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/docs-by-status/v1.1.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(
        bucket,
        [{"featureId": "docs-by-status", "source": "oss", "latestVersion": "1.1.0"}],
    )

    mod = _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert result[0]["updateAvailable"] is False


def test_feature_absent_from_catalog_does_not_crash(
    monkeypatch, mock_stack, load_lambda
):
    # Installed feature not in the catalog (e.g. unadvertised / catalog absent
    # for it) → latestVersion None, still listed.
    _seed(
        mock_stack["table_name"],
        [
            {
                "featureId": "ghost",
                "displayName": "Ghost",
                "installedVersion": "0.1.0",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/ghost/v0.1.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(mock_stack["bucket"], [])  # empty catalog

    mod = _preload(
        monkeypatch,
        mock_stack["table_name"],
        load_lambda,
        configuration_bucket=mock_stack["bucket"],
    )
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert result[0]["latestVersion"] is None
    assert result[0]["updateAvailable"] is False


def test_malformed_catalog_does_not_crash(monkeypatch, mock_stack, load_lambda):
    _seed(
        mock_stack["table_name"],
        [
            {
                "featureId": "quirk",
                "displayName": "Quirk",
                "installedVersion": "0.1.0",
                "stackName": "s",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/quirk/v0.1.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=mock_stack["bucket"], Key=_CATALOG_KEY, Body=b"this is not JSON"
    )

    mod = _preload(
        monkeypatch,
        mock_stack["table_name"],
        load_lambda,
        configuration_bucket=mock_stack["bucket"],
    )
    result = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert result[0]["latestVersion"] is None


def test_missing_env_var_raises(monkeypatch, load_lambda, aws_credentials):
    monkeypatch.delenv("INSTALLED_FEATURES_TABLE", raising=False)
    mod = load_lambda("list_installed_features")
    with pytest.raises(RuntimeError, match="INSTALLED_FEATURES_TABLE"):
        mod.handler(make_appsync_event("listInstalledFeatures"), None)


# ---------------------------------------------------------------------------
# Runtime latest.json lookup — the mechanism that decouples an extension's
# release cadence from the host's. Must be fast, cached, and fail soft.
# ---------------------------------------------------------------------------

_MP_BASE = "artifacts/genai-idp-mp/extensions/idp-auto-optimizer"
_OSS_BASE = "artifacts/genai-idp/extensions/docs-by-status"


def _put_latest_json(bucket: str, base: str, version: str, **extra):
    """Write the shape idp_feature_sdk's publisher._update_latest_json writes."""
    payload = {
        "featureId": extra.pop("featureId", "idp-auto-optimizer"),
        "version": version,
        "displayName": "Auto Optimizer",
        "bundleSha256": "0" * 64,
        "publishedAt": "2026-08-14T17:17:40.955Z",
    }
    payload.update(extra)
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket,
        Key=f"{base}/latest.json",
        Body=json.dumps(payload).encode("utf-8"),
    )


def _mp_catalog_entry(bucket: str, catalog_version: str = "0.1.0") -> dict:
    return {
        "featureId": "idp-auto-optimizer",
        "displayName": "Auto Optimizer",
        "source": "marketplace",
        "latestVersion": catalog_version,
        "productCode": "q0k0s3zuuga46hle6fecx547",
        "regions": {
            "us-east-1": {
                "sellerBucket": bucket,
                "templateKey": f"{_MP_BASE}/template.yaml",
            }
        },
    }


def _mp_installed_row(installed_version: str = "0.1.0") -> dict:
    return {
        "featureId": "idp-auto-optimizer",
        "displayName": "Auto Optimizer",
        "installedVersion": installed_version,
        "stackName": "idp-main-feature-idp-auto-optimizer",
        "stackRegion": "us-east-1",
        "uiBundlePath": f"features/idp-auto-optimizer/v{installed_version}/",
        "installedAt": "2026-08-14T00:00:00Z",
    }


def _preload_runtime(monkeypatch, table_name, load_lambda, bucket):
    monkeypatch.setenv("HOST_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return _preload(monkeypatch, table_name, load_lambda, configuration_bucket=bucket)


def test_runtime_latest_json_overrides_stale_catalog(
    monkeypatch, mock_stack, load_lambda
):
    """The headline case: a NEW extension version with an unchanged host.

    The catalog still says 0.1.0 (that's what shipped with the accelerator), but
    the seller published 0.2.0. The customer must see the update WITHOUT taking
    a new accelerator release.
    """
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    assert feature["latestVersion"] == "0.2.0"
    assert feature["updateAvailable"] is True


def test_runtime_latest_json_older_than_installed_shows_no_update(
    monkeypatch, mock_stack, load_lambda
):
    """latest.json BEHIND the install must not invite a downgrade."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.3.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    assert feature["latestVersion"] == "0.2.0"
    assert feature["updateAvailable"] is False


def test_missing_latest_json_falls_back_to_catalog(
    monkeypatch, mock_stack, load_lambda
):
    """No latest.json object → catalog value, no error. Fail soft."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.9.9")])
    # deliberately no _put_latest_json

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    assert feature["latestVersion"] == "0.9.9"
    assert feature["updateAvailable"] is True


def test_malformed_latest_json_falls_back_to_catalog(
    monkeypatch, mock_stack, load_lambda
):
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.9.9")])
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket, Key=f"{_MP_BASE}/latest.json", Body=b"not json at all"
    )

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.9.9"


def test_latest_json_without_version_field_falls_back(
    monkeypatch, mock_stack, load_lambda
):
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.9.9")])
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket,
        Key=f"{_MP_BASE}/latest.json",
        Body=json.dumps({"featureId": "idp-auto-optimizer"}).encode("utf-8"),
    )

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.9.9"


def test_s3_failure_never_raises(monkeypatch, mock_stack, load_lambda):
    """A hard S3 error (blocked egress, DNS, throttle) must not break the query."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.9.9")])

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)

    def _boom(*_args, **_kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(mod, "_public_s3", _boom)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.9.9"


def test_latest_json_result_is_cached(monkeypatch, mock_stack, load_lambda):
    """Called on every page load, so a warm container must not re-fetch."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)

    calls = {"n": 0}
    real_client = mod._public_s3

    def _counting(region):
        calls["n"] += 1
        return real_client(region)

    monkeypatch.setattr(mod, "_public_s3", _counting)

    for _ in range(3):
        (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
        assert feature["latestVersion"] == "0.2.0"
    assert calls["n"] == 1, "latest.json should be fetched once, then served from cache"


def test_negative_result_is_cached(monkeypatch, mock_stack, load_lambda):
    """A MISSING latest.json must also be cached, or every page load pays for it."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.9.9")])

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    calls = {"n": 0}
    real_client = mod._public_s3

    def _counting(region):
        calls["n"] += 1
        return real_client(region)

    monkeypatch.setattr(mod, "_public_s3", _counting)
    for _ in range(3):
        mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert calls["n"] == 1


def test_expired_cache_entry_is_refetched(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.2.0"

    # Expire the cache, then publish a newer version.
    mod._latest_json_cache.clear()
    _put_latest_json(bucket, _MP_BASE, "0.3.0")
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.3.0"


def test_oss_feature_latest_json_is_read_too(monkeypatch, mock_stack, load_lambda):
    """OSS extensions get the same decoupling — that's most of the value here."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(
        table,
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Docs By Status",
                "installedVersion": "1.0.0",
                "stackName": "idp-main-feature-docs-by-status",
                "stackRegion": "us-east-1",
                "uiBundlePath": "features/docs-by-status/v1.0.0/",
                "installedAt": "2026-01-01T00:00:00Z",
            }
        ],
    )
    _put_catalog(
        bucket,
        [
            {
                "featureId": "docs-by-status",
                "displayName": "Docs By Status",
                "source": "oss",
                "latestVersion": "1.0.0",
                "artifactBucket": bucket,
                "artifactPrefix": _OSS_BASE,
            }
        ],
    )
    _put_latest_json(bucket, _OSS_BASE, "1.2.0", featureId="docs-by-status")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "1.2.0"
    assert feature["updateAvailable"] is True


def test_unlisted_region_does_not_guess_a_bucket(monkeypatch, mock_stack, load_lambda):
    """No latest.json lookup at all when the catalog doesn't publish this region.

    Same invariant as getFeatureLaunchUrl: never synthesize a bucket name.
    """
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    entry = _mp_catalog_entry(bucket, catalog_version="0.9.9")
    entry["regions"] = {
        "eu-central-1": {
            "sellerBucket": "aws-ml-blog-eu-central-1",
            "templateKey": f"{_MP_BASE}/template.yaml",
        }
    }
    _put_catalog(bucket, [entry])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    calls = {"n": 0}
    monkeypatch.setattr(
        mod, "_public_s3", lambda region: calls.__setitem__("n", calls["n"] + 1)
    )
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert calls["n"] == 0
    assert feature["latestVersion"] == "0.9.9"


def test_lookup_can_be_disabled(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    monkeypatch.setenv("LATEST_JSON_LOOKUP", "false")
    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] == "0.1.0"


def test_installed_feature_absent_from_catalog_has_no_badge(
    monkeypatch, mock_stack, load_lambda
):
    """A feature installed --from-code isn't in the catalog; no location, no badge."""
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [])

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)
    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)
    assert feature["latestVersion"] is None
    assert feature["updateAvailable"] is False


def test_unsigned_read_is_attempted_before_signed(monkeypatch, mock_stack, load_lambda):
    """Public (anonymous) read is the primary path; signed is only a fallback.

    Ordering matters: an anonymous GET needs no IAM grant on the host role and no
    bucket-policy grant from the publisher, so it's what makes this lookup safe
    to enable on an existing deployment. The signed retry exists only for a
    private self-published artifacts bucket.
    """
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _seed(table, [_mp_installed_row("0.1.0")])
    _put_catalog(bucket, [_mp_catalog_entry(bucket, catalog_version="0.1.0")])
    _put_latest_json(bucket, _MP_BASE, "0.2.0")

    mod = _preload_runtime(monkeypatch, table, load_lambda, bucket)

    order: list[str] = []
    real_public = mod._public_s3
    real_read = mod._read_latest_json_once

    def _tracking_public(region):
        client = real_public(region)
        order.append("unsigned")
        return client

    def _tracking_read(client, bucket_, key_):
        if client is mod._s3:
            order.append("signed")
        return real_read(client, bucket_, key_)

    monkeypatch.setattr(mod, "_public_s3", _tracking_public)
    monkeypatch.setattr(mod, "_read_latest_json_once", _tracking_read)

    (feature,) = mod.handler(make_appsync_event("listInstalledFeatures"), None)

    # moto refuses the anonymous request, so both attempts happen here — but the
    # unsigned one must come FIRST.
    assert order[0] == "unsigned"
    assert "signed" in order
    assert feature["latestVersion"] == "0.2.0"
