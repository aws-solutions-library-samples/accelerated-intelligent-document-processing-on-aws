"""Unit tests for the get_feature_launch_url Lambda."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from _helpers import make_appsync_event

_CATALOG_KEY = "config_library/catalog.json"
# OSS extension artifacts live under this VERSION-FREE base in the artifacts
# bucket (which, in these tests, is the same mock bucket used as
# ConfigurationBucket). The template is at <base>/template.yaml; versioned
# artifacts under <base>/<version>/.
_ARTIFACT_PREFIX = "artifacts/genai-idp/extensions/docs-by-status"


def _preload(monkeypatch, mock_stack, load_lambda):
    # The mock S3 bucket doubles as both the ConfigurationBucket (catalog.json)
    # and the artifacts bucket (OSS feature templates) for these unit tests.
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", mock_stack["table_name"])
    monkeypatch.setenv("CONFIGURATION_BUCKET", mock_stack["bucket"])
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("ARTIFACT_REGION", "us-east-1")
    # Console (deploy) region for the launch URL — pin so assertions are stable.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MAIN_STACK_NAME", "idp-main")
    monkeypatch.setenv("ADMIN_GROUP", "Admin")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("get_feature_launch_url")


def _put(bucket: str, key: str, data) -> None:
    body = (
        json.dumps(data).encode("utf-8") if not isinstance(data, (bytes, str)) else data
    )
    if isinstance(body, str):
        body = body.encode("utf-8")
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=bucket, Key=key, Body=body
    )


def _put_catalog(bucket: str, features: list) -> None:
    _put(bucket, _CATALOG_KEY, {"schemaVersion": "1.0", "features": features})


def _oss_entry(feature_id: str, version: str, bucket: str, **extra) -> dict:
    """An OSS catalog entry pointing at the (mock) artifacts bucket."""
    return {
        "featureId": feature_id,
        "displayName": extra.get("displayName", feature_id),
        "source": "oss",
        "latestVersion": version,
        "artifactBucket": bucket,
        "artifactPrefix": _ARTIFACT_PREFIX,
    }


def test_happy_path_new_install(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "1.2.3", bucket)])

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=["Admin"]
    )
    result = mod.handler(event, None)

    assert result["featureId"] == "docs-by-status"
    assert result["version"] == "1.2.3"
    # OSS template URL is a bare S3 URL against the artifacts bucket, at the
    # VERSION-FREE extension base — no presign, no version in the path.
    expected = (
        f"https://{bucket}.s3.us-east-1.amazonaws.com/{_ARTIFACT_PREFIX}/template.yaml"
    )
    assert result["templateUrl"] == expected
    # New install → suggested stackName is derived
    assert result["stackName"] == "idp-main-feature-docs-by-status"

    # Parameters are MainStackName + FeatureBucket only. NEITHER the version NOR
    # the artifact prefix is a CFN parameter — both are baked into the template
    # at publish time (the CFN console drops/blanks params on a template change).
    # See `_parameters_for_feature` docstring.
    params = json.loads(result["parameters"])
    assert params["MainStackName"] == "idp-main"
    assert params["FeatureBucket"] == bucket
    assert "FeatureVersion" not in params
    assert "FeatureKeyPrefix" not in params
    assert "FeatureArtifactPrefix" not in params

    # Launch URL is well-formed and includes all parameters
    parsed = urlparse(result["launchUrl"])
    assert parsed.netloc == "console.aws.amazon.com"
    assert "region=us-east-1" in parsed.query
    # Fragment contains the real CFN quick-create query
    assert "stacks/quickcreate" in parsed.fragment
    frag_query = parse_qs(parsed.fragment.split("?", 1)[1])
    assert frag_query["templateURL"][0] == result["templateUrl"]
    assert frag_query["stackName"][0] == result["stackName"]
    assert frag_query["param_MainStackName"][0] == "idp-main"
    assert "param_FeatureVersion" not in frag_query


def test_update_existing_install_preserves_stack_name(
    monkeypatch, mock_stack, load_lambda
):
    """When InstalledFeatures has a row but the CFN stack doesn't actually
    exist (or DescribeStacks fails), the resolver still returns the recorded
    `stackName` and falls back to a create-form URL. This is the
    InstalledFeatures-row-is-stale recovery path.
    """
    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "2.0.0", bucket)])

    boto3.resource("dynamodb", region_name="us-east-1").Table(table).put_item(
        Item={
            "featureId": "docs-by-status",
            "displayName": "Docs",
            "installedVersion": "1.0.0",
            "stackName": "my-preferred-stackname",
            "stackRegion": "us-east-1",
            "uiBundlePath": "features/docs-by-status/v1.0.0/",
            "installedAt": "2026-01-01T00:00:00Z",
        }
    )

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=["Admin"]
    )
    result = mod.handler(event, None)
    # stackName comes from the DDB row; CFN stack doesn't exist so URL falls
    # back to the create form (admin will see AlreadyExistsException only if
    # they do have a stack with that name in real AWS — we don't here).
    assert result["stackName"] == "my-preferred-stackname"
    assert result["version"] == "2.0.0"
    assert "stacks/quickcreate" in result["launchUrl"]


def test_update_url_when_stack_exists(monkeypatch, mock_stack, load_lambda):
    """When InstalledFeatures has a row AND a CFN stack of that name exists,
    the resolver returns an "update existing stack" URL targeting the
    stack's ARN — not the create-form URL. This is the happy path for
    feature upgrades and the fix for the AlreadyExistsException users hit
    when re-running quickcreate against an installed feature.
    """
    import boto3 as _boto3

    bucket = mock_stack["bucket"]
    table = mock_stack["table_name"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "2.0.0", bucket)])

    # Install the DDB row pointing at a stack name we'll create below.
    stack_name = "idp-main-feature-docs-by-status"
    _boto3.resource("dynamodb", region_name="us-east-1").Table(table).put_item(
        Item={
            "featureId": "docs-by-status",
            "displayName": "Docs",
            "installedVersion": "1.0.0",
            "stackName": stack_name,
            "stackRegion": "us-east-1",
            "uiBundlePath": "features/docs-by-status/v1.0.0/",
            "installedAt": "2026-01-01T00:00:00Z",
        }
    )
    # Create a real (moto-mocked) CFN stack so DescribeStacks returns the ARN.
    cfn = _boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(
        StackName=stack_name,
        TemplateBody='{"AWSTemplateFormatVersion":"2010-09-09","Resources":'
        '{"D":{"Type":"AWS::CloudFormation::WaitConditionHandle"}}}',
    )

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=["Admin"]
    )
    result = mod.handler(event, None)

    # URL should be the update form, targeting the stack ARN.
    parsed = urlparse(result["launchUrl"])
    assert "stacks/update/template" in parsed.fragment
    frag_query = parse_qs(parsed.fragment.split("?", 1)[1])
    # Update form uses stackId (full ARN), not stackName.
    assert "stackId" in frag_query
    assert "stackName" not in frag_query
    assert frag_query["stackId"][0].startswith("arn:aws:cloudformation:us-east-1:")
    assert stack_name in frag_query["stackId"][0]
    # The new version's templateURL is still passed; CFN Console pre-loads it.
    # The version is baked INTO that template (publisher substitutes
    # `<FEATURE_VERSION_TOKEN>` at upload time), so the update applies the
    # new version even though the URL doesn't carry a `param_FeatureVersion`.
    assert frag_query["templateURL"][0] == result["templateUrl"]
    assert "param_FeatureVersion" not in frag_query


def test_explicit_version_overrides_latest(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "2.0.0", bucket)])

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl",
        {"featureId": "docs-by-status", "version": "1.0.0"},
        groups=["Admin"],
    )
    result = mod.handler(event, None)
    # The requested version is reflected in the response, but the template URL
    # is version-free (the version reaches the stack via the baked template, not
    # the URL/params), so it does NOT appear in templateUrl.
    assert result["version"] == "1.0.0"
    assert "1.0.0" not in result["templateUrl"]
    assert result["templateUrl"].endswith(f"{_ARTIFACT_PREFIX}/template.yaml")


def test_non_admin_is_rejected(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "1.0.0", bucket)])

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=["Viewer"]
    )
    with pytest.raises(mod.AuthorizationError):
        mod.handler(event, None)


def test_no_groups_is_rejected(monkeypatch, mock_stack, load_lambda):
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "1.0.0", bucket)])

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=[]
    )
    with pytest.raises(mod.AuthorizationError):
        mod.handler(event, None)


def test_missing_catalog_entry_raises(monkeypatch, mock_stack, load_lambda):
    # No catalog at all → OSS branch can't resolve artifactBucket/version.
    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "unknown-feature"}, groups=["Admin"]
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        mod.handler(event, None)


def test_missing_featureId_raises(monkeypatch, mock_stack, load_lambda):
    mod = _preload(monkeypatch, mock_stack, load_lambda)
    with pytest.raises(ValueError, match="featureId"):
        mod.handler(
            make_appsync_event("getFeatureLaunchUrl", {}, groups=["Admin"]), None
        )


def test_oss_feature_bucket_and_prefix_come_from_catalog(
    monkeypatch, mock_stack, load_lambda
):
    """The catalog entry's artifactBucket/artifactPrefix (stamped by idp-cli
    publish) drive the resolver: artifactBucket -> FeatureBucket param, and
    artifactPrefix -> the version-free template URL. The prefix is NOT a CFN
    param (it's baked into the template at publish time).
    """
    bucket = mock_stack["bucket"]
    _put_catalog(bucket, [_oss_entry("docs-by-status", "1.2.3", bucket)])

    mod = _preload(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "docs-by-status"}, groups=["Admin"]
    )
    result = mod.handler(event, None)

    params = json.loads(result["parameters"])
    assert params["FeatureBucket"] == bucket
    assert "FeatureArtifactPrefix" not in params
    assert "FeatureKeyPrefix" not in params
    # The catalog artifactPrefix drives the (version-free) template URL.
    assert result["templateUrl"].endswith(f"{_ARTIFACT_PREFIX}/template.yaml")


# ---------------------------------------------------------------------------
# Marketplace features: catalog-driven, region-mapped, bare public template URL.
# (`_CATALOG_KEY` / `_put_catalog` are defined once at the top of the module.)
# ---------------------------------------------------------------------------


def _preload_marketplace(monkeypatch, mock_stack, load_lambda):
    # ConfigurationBucket reuses the mock S3 bucket; catalog + seller objects
    # live alongside the OSS feature artifacts in the same moto-mocked S3.
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", mock_stack["table_name"])
    monkeypatch.setenv("CONFIGURATION_BUCKET", mock_stack["bucket"])
    monkeypatch.setenv("CATALOG_KEY", _CATALOG_KEY)
    monkeypatch.setenv("ARTIFACT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MAIN_STACK_NAME", "idp-main")
    monkeypatch.setenv("ADMIN_GROUP", "Admin")
    monkeypatch.setenv("DEFAULT_CUSTOMER_IDENTIFIER", "cust-1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("get_feature_launch_url")


_MP_TEMPLATE_KEY = "artifacts/genai-idp-mp/extensions/my-paid-extension/template.yaml"


def _mp_entry(bucket: str, regions: dict | None = None, **extra) -> dict:
    """A schema-1.1 marketplace catalog entry with an explicit regions map."""
    entry = {
        "featureId": "my-paid-extension",
        "displayName": "My Paid Extension",
        "source": "marketplace",
        "latestVersion": "0.1.4",
        "productCode": "prod-xyz",
        "productId": "prod-abc123",
        "regions": regions
        if regions is not None
        else {
            "us-east-1": {
                "sellerBucket": bucket,
                "templateKey": _MP_TEMPLATE_KEY,
            }
        },
    }
    entry.update(extra)
    return entry


def test_marketplace_region_hit_returns_bare_public_url(
    monkeypatch, mock_stack, load_lambda
):
    """Schema 1.1: the caller's region resolves in `regions` → bare public URL.

    Marketplace artifacts are public-read by necessity (Seller Ops fetches the
    registered template URL, and CloudFormation fetches the code zips from the
    buyer's account), so there is deliberately NO presign here.
    """
    bucket = mock_stack["bucket"]
    _put(bucket, _MP_TEMPLATE_KEY, "AWSTemplateFormatVersion: '2010-09-09'")
    _put_catalog(bucket, [_mp_entry(bucket)])

    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    # Force the entitlement gate open (GetEntitlements isn't moto-backed).

    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    result = mod.handler(event, None)

    assert result["featureId"] == "my-paid-extension"
    assert result["version"] == "0.1.4"
    parsed = urlparse(result["templateUrl"])
    qs = parse_qs(parsed.query)
    # The region's bucket, verbatim from the catalog — never derived.
    assert parsed.netloc == f"{bucket}.s3.us-east-1.amazonaws.com"
    assert parsed.path == f"/{_MP_TEMPLATE_KEY}"
    # NOT presigned: no signature, no expiry. Removing the presign is
    # deliberate; re-adding one would break long-lived Update wizard sessions.
    assert "X-Amz-Signature" not in qs
    assert "Signature" not in qs
    assert "X-Amz-Expires" not in qs
    assert "templateURL=" in result["launchUrl"]
    # The feature stack's ui-deployer reads its UI bundle from the SELLER
    # bucket; FeatureBucket is the only S3 coordinate passed as a param. The
    # version-free base and version are baked into the template at publish time,
    # so they are NOT params.
    params = json.loads(result["parameters"])
    assert params["FeatureBucket"] == bucket
    assert "FeatureArtifactPrefix" not in params
    assert "FeatureKeyPrefix" not in params


def test_marketplace_region_miss_fails_closed(monkeypatch, mock_stack, load_lambda):
    """An unlisted region must fail closed, never derive a bucket name.

    Deriving `<basename>-<region>` would be a security hole: S3 bucket names are
    global, so a derived name in a region we don't publish to could resolve to a
    bucket someone else owns, handing the customer a template we didn't write.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [
            _mp_entry(
                bucket,
                regions={
                    "us-west-2": {
                        "sellerBucket": "aws-ml-blog-us-west-2",
                        "templateKey": _MP_TEMPLATE_KEY,
                    },
                    "eu-central-1": {
                        "sellerBucket": "aws-ml-blog-eu-central-1",
                        "templateKey": _MP_TEMPLATE_KEY,
                    },
                },
            )
        ],
    )
    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    # The stack runs in us-east-1, which the entry does NOT list.
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    with pytest.raises(mod.FeatureNotAvailableInRegionError) as exc:
        mod.handler(event, None)
    message = str(exc.value)
    assert "us-east-1" in message
    # The error names where it IS available so the UI can say something useful.
    assert "us-west-2" in message and "eu-central-1" in message


def test_marketplace_legacy_flat_schema_still_works(
    monkeypatch, mock_stack, load_lambda
):
    """Deprecated schema 1.0 (flat sellerBucket + sellerBucketRegion) is honored.

    Accepted only for its OWN declared region — the old resolver used that one
    bucket in every region, which is the wrong-region deploy bug 1.1 fixes.
    """
    bucket = mock_stack["bucket"]
    _put(bucket, _MP_TEMPLATE_KEY, "AWSTemplateFormatVersion: '2010-09-09'")
    entry = _mp_entry(bucket, regions={})
    entry.update(
        {
            "sellerBucket": bucket,
            "sellerBucketRegion": "us-east-1",
            "templateKey": _MP_TEMPLATE_KEY,
        }
    )
    _put_catalog(bucket, [entry])

    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    result = mod.handler(event, None)
    assert urlparse(result["templateUrl"]).netloc == (
        f"{bucket}.s3.us-east-1.amazonaws.com"
    )


def test_marketplace_legacy_flat_schema_other_region_fails_closed(
    monkeypatch, mock_stack, load_lambda
):
    """A legacy entry declared for another region must NOT be reused here."""
    bucket = mock_stack["bucket"]
    entry = _mp_entry(bucket, regions={})
    entry.update(
        {
            "sellerBucket": "seller-bucket-eu-central-1",
            "sellerBucketRegion": "eu-central-1",
            "templateKey": _MP_TEMPLATE_KEY,
        }
    )
    _put_catalog(bucket, [entry])

    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    with pytest.raises(mod.FeatureNotAvailableInRegionError):
        mod.handler(event, None)


def test_marketplace_empty_catalog_latest_version_still_launches(
    monkeypatch, mock_stack, load_lambda
):
    """An empty catalog `latestVersion` must not block a launch.

    The template URL is version-free and the version is baked into the published
    template, so requiring a catalog version here would re-create exactly the
    host-release coupling the runtime latest.json lookup removes.
    """
    bucket = mock_stack["bucket"]
    _put(bucket, _MP_TEMPLATE_KEY, "AWSTemplateFormatVersion: '2010-09-09'")
    _put_catalog(bucket, [_mp_entry(bucket, latestVersion="")])

    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    result = mod.handler(event, None)
    assert result["version"] == ""
    assert result["templateUrl"].endswith(_MP_TEMPLATE_KEY)


def test_launch_url_does_not_gate_on_entitlement(monkeypatch, mock_stack, load_lambda):
    """This resolver must NOT re-check entitlement. It used to, and the check
    denied every genuinely subscribed customer.

    It required a CustomerIdentifier that a real-Marketplace stack does not have
    (no request header, and DEFAULT_CUSTOMER_IDENTIFIER is empty there), so it
    raised before making any API call; and it asked seller-side GetEntitlements,
    which returns 200-with-an-empty-list from a buyer account for a usage-based
    SaaS listing. The customer saw "Subscription active" on the page and "no
    entitlement" on the Launch button.

    `checkFeatureEntitlement` is the single host-side authority and already decides
    whether Launch is offered. Every marketplace test in this file used to
    monkeypatch the gate to True, which is exactly why none of them caught it —
    so this test deliberately supplies NO entitlement state of any kind.
    """
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [
            {
                "featureId": "my-paid-extension",
                "displayName": "My Paid Extension",
                "source": "marketplace",
                "latestVersion": "0.1.4",
                "productCode": "prod-xyz",
                "licenseMode": "marketplace-live",
                "sellerBucket": bucket,
                "sellerBucketRegion": "us-east-1",
                "templateKey": "features/my-paid-extension/v0.1.4/template.yaml",
            }
        ],
    )
    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)

    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    result = mod.handler(event, None)
    assert result["templateUrl"].endswith(
        "features/my-paid-extension/v0.1.4/template.yaml"
    )
    # And the retired gate's machinery is gone, not merely bypassed.
    assert not hasattr(mod, "_has_active_entitlement")
    assert not hasattr(mod, "NotEntitledError")


def test_marketplace_entry_with_no_region_mapping_at_all(
    monkeypatch, mock_stack, load_lambda
):
    """No `regions` and no legacy fields → unavailable everywhere, not a crash."""
    bucket = mock_stack["bucket"]
    _put_catalog(
        bucket,
        [
            {
                "featureId": "my-paid-extension",
                "source": "marketplace",
                # no regions, no legacy sellerBucket/sellerBucketRegion
            }
        ],
    )
    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    with pytest.raises(mod.FeatureNotAvailableInRegionError, match="no regions"):
        mod.handler(event, None)


def test_marketplace_incomplete_catalog_entry_raises(
    monkeypatch, mock_stack, load_lambda
):
    """Region resolves but productCode is missing → explicit 'incomplete' error."""
    bucket = mock_stack["bucket"]
    entry = _mp_entry(bucket)
    del entry["productCode"]
    _put_catalog(bucket, [entry])

    mod = _preload_marketplace(monkeypatch, mock_stack, load_lambda)
    event = make_appsync_event(
        "getFeatureLaunchUrl", {"featureId": "my-paid-extension"}, groups=["Admin"]
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        mod.handler(event, None)
