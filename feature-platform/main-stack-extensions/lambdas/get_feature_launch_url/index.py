# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Query.getFeatureLaunchUrl resolver. Admin-only.

Builds a CloudFormation Console URL that deploys (for first install) OR
updates (for already-installed features) a feature stack in the caller's AWS
account. The parameters are pre-filled so the admin only has to click
"Create stack" / "Update stack".

The resolver picks one of two URL forms based on whether the feature is
already installed in this main stack (i.e. has a row in InstalledFeatures
DDB):

  - **Not installed yet** → ``#/stacks/quickcreate?templateURL=…&stackName=…&param_*=…``
    Lands on the CFN Console "Quick create stack" page.

  - **Already installed** → ``#/stacks/update/template?stackId=<arn>&templateURL=…&param_*=…``
    Lands on the "Update stack" wizard step 1 with the new template URL
    pre-loaded. Without this branch the quickcreate URL fails with
    ``AlreadyExistsException`` ("Stack [<name>] already exists") because
    quickcreate is create-only.

If we cannot resolve the existing stack's ARN (e.g. the stack was deleted
out-of-band but the InstalledFeatures row was left behind, or the resolver
Lambda's IAM role lacks ``cloudformation:DescribeStacks``) we fall back to
the create-form URL with a warning logged. The admin will then see the
``AlreadyExistsException`` themselves — same failure mode as before the fix
— but the common case (stack exists & describable) gets the right URL.

Server-side admin check: only callers whose `cognito:groups` claim includes
`Admin` are allowed. UI hiding is a convenience; the real gate is here.

For each feature this reads the catalog entry (from ConfigurationBucket) for the
version + artifact location, builds the template URL, and pre-fills CFN
parameters (including a reference back to this main stack so the feature stack
can look up its exports).

Two feature kinds are supported, distinguished by the catalog manifest's
`source` field (read from ConfigurationBucket, GetObject-only — no listing):

  - **OSS features** (`source="oss"`) — artifacts live in the artifacts bucket
    (the same bucket the main template is published to), under a VERSION-FREE
    base `<artifactPrefix>` = `<prefix>/extensions/<id>`: the template at
    `<artifactPrefix>/template.yaml` and versioned artifacts under
    `<artifactPrefix>/<version>/`. The catalog entry carries `artifactBucket` +
    `artifactPrefix`; the launch URL is a bare S3 HTTPS URL (no presign),
    inheriting the artifacts bucket's access model — exactly like the main-stack
    quick-create link.

  - **Marketplace features** (`source="marketplace"`) — the template lives in a
    seller bucket whose objects are PUBLIC-READ, and the catalog entry maps each
    supported region to an EXPLICIT bucket + version-free template key. This
    resolver looks the caller's region up in that map and FAILS CLOSED with
    `FeatureNotAvailableInRegionError` when it is absent. The launch URL is a
    bare S3 HTTPS URL, exactly like the OSS path.

    Two things about this deserve to be stated plainly, because both are easy to
    "tidy up" back into bugs:

    1. **The bucket is never derived.** We do not concatenate a basename with
       ``AWS::Region``. S3 bucket names are global and guessable, so a derived
       name in a region we don't publish to could resolve to a bucket somebody
       else owns — and we would hand the customer a CloudFormation template we
       did not write. Look up, or fail.

    2. **There is no presign, and that is not a downgrade.** Marketplace
       artifacts *must* be public-read: the registered Quick Launch template URL
       is fetched by AWS Seller Ops during listing review and by CloudFormation
       in an arbitrary buyer account, and the Lambda code zips are fetched from
       the buyer's account at deploy time. A presigned URL never covered either,
       so it protected nothing while adding a failure mode (expiry mid-way
       through the CFN "Update stack" wizard → an opaque 403).

    Consequently this resolver performs **no entitlement check of its own**. There
    is nothing here to protect by re-checking, and `checkFeatureEntitlement` is
    the single host-side authority — it already decides whether the UI offers
    Launch at all. A second gate here denied every genuinely subscribed customer
    (see the comment at the marketplace branch below). The commercial gate is the
    Marketplace subscription plus the extension's own runtime entitlement check.

Environment:
    ARTIFACT_REGION            Region for the bare OSS template URL (defaults to AWS_REGION)
    CONFIGURATION_BUCKET        Stack's ConfigurationBucket (holds catalog.json)
    CATALOG_KEY                 Catalog key (default config_library/catalog.json)
    DEFAULT_CUSTOMER_IDENTIFIER Marketplace customer id fallback (no request header)
    MAIN_STACK_NAME            This IDP stack's name (passed to the feature stack as a parameter)
    INSTALLED_FEATURES_TABLE   DynamoDB table name (for looking up existing installs when updating)
    ADMIN_GROUP                Cognito group name for admins (default "Admin")
    LOG_LEVEL                  Logging level (default INFO)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Region used to build the bare OSS template URL against the artifacts bucket
# (the bucket the main template is published to, which is same-region as this
# stack).
_ARTIFACT_REGION = os.environ.get(
    "ARTIFACT_REGION", os.environ.get("AWS_REGION", "us-east-1")
)
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_DEFAULT_CUSTOMER_IDENTIFIER = os.environ.get("DEFAULT_CUSTOMER_IDENTIFIER", "")
_MAIN_STACK_NAME = os.environ.get("MAIN_STACK_NAME", "")
_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
_ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Catalog lives in the stack's own ConfigurationBucket (Lambda's default region).
_config_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")
# CloudFormation client uses the Lambda's default region (where the IDP main
# stack lives — feature stacks live alongside it). DescribeStacks is used to
# resolve an existing stack's full ARN for the update URL form.
_cfn = boto3.client("cloudformation")


def _read_catalog_entry(feature_id: str) -> Optional[Dict[str, Any]]:
    """Return the catalog.json entry for `feature_id`, or None if absent.

    Single GetObject against ConfigurationBucket — never lists.
    """
    if not _CONFIGURATION_BUCKET:
        return None
    try:
        resp = _config_s3.get_object(Bucket=_CONFIGURATION_BUCKET, Key=_CATALOG_KEY)
        catalog = json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        logger.warning("Failed to read catalog: %s", exc)
        return None
    except (BotoCoreError, ValueError) as exc:
        logger.warning("Bad catalog JSON: %s", exc)
        return None
    for entry in catalog.get("features") or []:
        if isinstance(entry, dict) and entry.get("featureId") == feature_id:
            return entry
    return None


def _customer_identifier(event: Dict[str, Any]) -> Optional[str]:
    headers = (event.get("request", {}) or {}).get("headers", {}) or {}
    for key in (
        "x-amzn-marketplace-customer-identifier",
        "X-Amzn-Marketplace-Customer-Identifier",
    ):
        if headers.get(key):
            return headers[key]
    return _DEFAULT_CUSTOMER_IDENTIFIER or None


class FeatureNotAvailableInRegionError(Exception):
    """Raised when a marketplace feature has no artifacts in the caller's region."""


def _resolve_region_artifacts(
    catalog_entry: Dict[str, Any], region: str
) -> Dict[str, str]:
    """Resolve {sellerBucket, templateKey} for `region` from a catalog entry.

    Catalog schema 1.1 carries an explicit ``regions`` map. We look the region
    up and raise ``FeatureNotAvailableInRegionError`` when it is missing. We do
    NOT synthesize a bucket name from a basename plus the region: S3 bucket
    names are global and guessable, so a synthesized name in a region we don't
    publish to could belong to somebody else, and the customer would be sent to
    a CloudFormation template we did not author. Fail closed instead.

    Legacy schema 1.0 entries (flat ``sellerBucket`` + ``sellerBucketRegion`` +
    ``templateKey``) are honored as a deprecated fallback, but ONLY when their
    ``sellerBucketRegion`` matches the caller's region — the old resolver used
    that bucket in every region regardless, which is precisely the wrong-region
    deploy bug schema 1.1 fixes.
    """
    available = _available_regions(catalog_entry)
    regions = catalog_entry.get("regions")
    if isinstance(regions, dict):
        spec = regions.get(region)
        if isinstance(spec, dict):
            bucket = (spec.get("sellerBucket") or "").strip()
            key = (spec.get("templateKey") or "").strip().lstrip("/")
            if bucket and key:
                return {"sellerBucket": bucket, "templateKey": key}

    # Deprecated flat schema — accepted only for its own declared region.
    legacy_bucket = (catalog_entry.get("sellerBucket") or "").strip()
    legacy_region = (catalog_entry.get("sellerBucketRegion") or "").strip()
    legacy_key = (catalog_entry.get("templateKey") or "").strip().lstrip("/")
    if legacy_bucket and legacy_key and legacy_region == region:
        logger.warning(
            "Feature %r uses the deprecated flat sellerBucket/sellerBucketRegion "
            "catalog schema; migrate it to a 'regions' map.",
            catalog_entry.get("featureId"),
        )
        return {"sellerBucket": legacy_bucket, "templateKey": legacy_key}

    where = ", ".join(available) if available else "no regions"
    raise FeatureNotAvailableInRegionError(
        f"{catalog_entry.get('displayName') or catalog_entry.get('featureId')} is "
        f"not available in {region}. Supported regions: {where}."
    )


def _available_regions(catalog_entry: Dict[str, Any]) -> List[str]:
    """Sorted list of regions a marketplace feature publishes artifacts to.

    Reads the schema 1.1 ``regions`` map, falling back to the deprecated flat
    ``sellerBucketRegion``. Shared with `list_catalog_features`' availability
    flags so the UI and this resolver can never disagree about where a feature
    can be installed.
    """
    regions = catalog_entry.get("regions")
    if isinstance(regions, dict):
        found = sorted(
            str(r)
            for r, spec in regions.items()
            if isinstance(spec, dict)
            and (spec.get("sellerBucket") or "").strip()
            and (spec.get("templateKey") or "").strip()
        )
        if found:
            return found
    legacy_region = (catalog_entry.get("sellerBucketRegion") or "").strip()
    if legacy_region and (catalog_entry.get("sellerBucket") or "").strip():
        return [legacy_region]
    return []


class AuthorizationError(Exception):
    """Raised when a non-admin caller requests getFeatureLaunchUrl."""


def _assert_admin(event: Dict[str, Any]) -> None:
    groups = event.get("identity", {}).get("claims", {}).get("cognito:groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    if _ADMIN_GROUP not in groups:
        raise AuthorizationError(
            f"getFeatureLaunchUrl requires membership in group {_ADMIN_GROUP!r}"
        )


# OSS feature version + artifact location now come from the catalog entry
# (stamped by `idp-cli publish`); the host no longer reads latest.json /
# manifest.json from a stack-owned feature bucket.


def _existing_stack_name(feature_id: str) -> Optional[str]:
    if not _INSTALLED_FEATURES_TABLE:
        return None
    try:
        row = (
            _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
            .get_item(Key={"featureId": feature_id})
            .get("Item")
        )
        return row.get("stackName") if row else None
    except ClientError as exc:
        logger.warning("Could not look up existing install for %s: %s", feature_id, exc)
        return None


def _describe_stack_arn(stack_name: str) -> Optional[str]:
    """Resolve a stack name to its full ARN via cloudformation:DescribeStacks.

    Returns ``None`` if:
    - the stack does not exist (e.g. the InstalledFeatures row is stale and
      the stack was deleted out-of-band);
    - the stack is in a state where update isn't sensible
      (DELETE_COMPLETE, REVIEW_IN_PROGRESS) — caller will fall back to the
      create URL form;
    - the resolver Lambda's IAM role lacks
      ``cloudformation:DescribeStacks`` (logged at WARNING; caller falls
      back gracefully).

    Using the ARN (not the name) in the update URL is preferred because it
    survives stack rename and disambiguates if multiple stacks happen to
    share the same name across accounts (extremely unlikely, but cheap to
    do correctly).
    """
    try:
        resp = _cfn.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        # Stack-doesn't-exist comes back as a ValidationError, not a 404.
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "")
        if code == "ValidationError" and "does not exist" in message:
            logger.info(
                "Stack %r does not exist — InstalledFeatures row is stale; "
                "URL will fall back to the create form",
                stack_name,
            )
            return None
        # Permissions / throttling / other transient — log and degrade
        # gracefully to the create URL, which surfaces the
        # AlreadyExistsException to the admin (same UX as before this fix).
        logger.warning(
            "describe_stacks(%s) failed (%s: %s); falling back to create URL",
            stack_name,
            code,
            message,
        )
        return None

    stacks = resp.get("Stacks") or []
    if not stacks:
        return None
    stack = stacks[0]
    status = stack.get("StackStatus", "")
    # Stacks in these states cannot be updated. The update URL would land
    # the admin on an error page; the create URL gives them a clearer
    # AlreadyExistsException (or, if the stack is gone, succeeds).
    if status in {
        "DELETE_COMPLETE",
        "DELETE_IN_PROGRESS",
        "REVIEW_IN_PROGRESS",
        "CREATE_IN_PROGRESS",
        "ROLLBACK_IN_PROGRESS",
    }:
        logger.info(
            "Stack %r is in non-updatable state %s; falling back to create URL",
            stack_name,
            status,
        )
        return None
    return stack.get("StackId")


def _s3_https_url(bucket: str, region: str, key: str) -> str:
    """Bare virtual-hosted-style S3 HTTPS URL for a template object.

    Used by BOTH the OSS and marketplace paths — marketplace artifacts are
    public-read (see the module docstring), so they need no presign and the two
    paths produce the same kind of URL.
    """
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key.lstrip('/')}"


def _oss_template_https_url(bucket: str, key_prefix: str) -> str:
    """Bare virtual-hosted-style S3 URL for an OSS feature template.

    OSS extension artifacts are published to the same artifacts bucket as the
    main template, under a VERSION-FREE base `<prefix>/extensions/<id>`, with
    the template at `<base>/template.yaml` (newest publish overwrites it — like
    idp-main.yaml). `key_prefix` is that version-free base; the Launch Stack URL
    inherits the main template's own access model (public for the public
    release, private for a self-publish — no presign; cf. marketplace, whose
    private seller bucket always requires one).
    """
    return _s3_https_url(bucket, _ARTIFACT_REGION, f"{key_prefix}/template.yaml")


def _build_create_url(
    region: str,
    template_url: str,
    stack_name: str,
    parameters: Dict[str, str],
) -> str:
    """Build a CloudFormation Console quick-create URL for first install.

    Ref: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stack-params-url.html
    """
    parts = [
        f"templateURL={quote(template_url, safe='')}",
        f"stackName={quote(stack_name, safe='')}",
    ]
    for key, val in sorted(parameters.items()):
        parts.append(f"param_{quote(key, safe='')}={quote(str(val), safe='')}")
    query = "&".join(parts)
    return f"https://console.aws.amazon.com/cloudformation/home?region={region}#/stacks/quickcreate?{query}"


def _build_update_url(
    region: str,
    template_url: str,
    stack_arn: str,
    parameters: Dict[str, str],
) -> str:
    """Build a CloudFormation Console "update existing stack" URL.

    Lands on the update wizard (Step 1: Specify template) with the new
    template URL pre-loaded; admin clicks Next through param review and
    confirms. Targets the stack by full ARN so name drift doesn't matter.

    The ``param_*`` query params are honored on this path too — the update
    wizard uses them as parameter overrides, just like quickcreate. CFN
    parameters that aren't overridden retain their existing values.
    """
    parts = [
        f"stackId={quote(stack_arn, safe='')}",
        f"templateURL={quote(template_url, safe='')}",
    ]
    for key, val in sorted(parameters.items()):
        parts.append(f"param_{quote(key, safe='')}={quote(str(val), safe='')}")
    query = "&".join(parts)
    return (
        f"https://console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/update/template?{query}"
    )


# Backward-compat alias kept for any external callers / tests that imported
# `_build_launch_url` directly. Prefer the explicit `_build_create_url` /
# `_build_update_url` going forward.
_build_launch_url = _build_create_url


def _parameters_for_feature(
    feature_id: str,
    version: str,
    manifest: Optional[Dict[str, Any]],
    feature_bucket: str,
) -> Dict[str, str]:
    """Compute the set of pre-filled CFN parameters.

    Every feature template is required to accept at least:
      - MainStackName (the IDP stack name; used by the feature to look up Exports)
      - FeatureBucket — the bucket the feature stack's ui-deployer reads the UMD
        bundle from to copy into the main stack's WebUIBucket. For OSS this is
        the artifacts bucket; for marketplace, the seller bucket. Not
        version-bearing (stable across versions), so it's safe as a parameter.

    NEITHER the version NOR the version-free artifact prefix
    (`<prefix>/extensions/<id>`) is a CFN parameter. Both are baked into the
    published template at upload time by the publisher (which substitutes the
    `<FEATURE_VERSION_TOKEN>` and `<FEATURE_ARTIFACT_PREFIX_TOKEN>`
    placeholders). Why? CloudFormation Console's "Update stack" wizard
    PRESERVES existing parameter values and inconsistently honors `param_*` URL
    overrides — so on a template change that renames/adds a param, the new param
    arrives EMPTY (observed: an empty FeatureArtifactPrefix produced a
    `s3://bucket//<version>/...` bad key and the update rolled back). Baking
    both into the template means there is no param for the console to drop, and
    a stack Update always carries the correct, current values.

    The publisher may advertise additional defaults in `manifest.json ->
    defaultParameters` — which override these if needed.
    """
    params: Dict[str, str] = {
        "MainStackName": _MAIN_STACK_NAME,
        "FeatureBucket": feature_bucket,
    }
    if manifest:
        defaults: Dict[str, Any] = manifest.get("defaultParameters") or {}
        for k, v in defaults.items():
            if isinstance(v, (str, int, float, bool)):
                params[str(k)] = str(v)
    # Defensive: drop params the template no longer declares (baked instead),
    # so passing them via the URL can't produce "Parameters: [X] do not exist
    # in the template" in the CFN console.
    params.pop("FeatureVersion", None)
    params.pop("FeatureArtifactPrefix", None)
    return params


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("getFeatureLaunchUrl event: %s", event)
    if not _MAIN_STACK_NAME:
        raise RuntimeError("MAIN_STACK_NAME env var is not configured")

    _assert_admin(event)

    args = event.get("arguments", {}) or {}
    feature_id = args.get("featureId")
    if not feature_id or not isinstance(feature_id, str):
        raise ValueError("featureId is required")

    # Discover whether this is an OSS or marketplace feature from the catalog.
    # Absent entry → treat as OSS (back-compat with the FeatureBucket layout).
    catalog_entry = _read_catalog_entry(feature_id) or {}
    source = catalog_entry.get("source") or "oss"

    if source == "marketplace":
        # Resolve the artifacts for THIS region from the catalog's explicit
        # `regions` map — never derived. Raises FeatureNotAvailableInRegionError
        # when the feature isn't published here, which the UI renders as
        # "not available in <region>" rather than a broken Launch button.
        stack_region = os.environ.get("AWS_REGION", _ARTIFACT_REGION)
        artifacts = _resolve_region_artifacts(catalog_entry, stack_region)
        seller_bucket = artifacts["sellerBucket"]
        template_key = artifacts["templateKey"]

        product_code = catalog_entry.get("productCode") or ""
        if not (product_code and seller_bucket and template_key):
            raise RuntimeError(
                f"Marketplace feature {feature_id!r} catalog entry is incomplete "
                f"(need productCode and a regions entry with sellerBucket + "
                f"templateKey)"
            )
        # `version` is informational on this path. The template URL is
        # version-free and the version is baked into the published template, so
        # an empty/stale catalog `latestVersion` must NOT block a launch — that
        # coupling is exactly what the runtime latest.json lookup removes.
        version = args.get("version") or catalog_entry.get("latestVersion") or ""

        # NO entitlement gate here — deliberately, and this is a removal.
        #
        # There used to be an "advisory" one, and it denied EVERY genuinely
        # subscribed customer on the production path. Two independent reasons,
        # either sufficient:
        #
        #   1. It required a CustomerIdentifier from a request header or
        #      DEFAULT_CUSTOMER_IDENTIFIER, neither of which exists on a
        #      real-Marketplace stack — so it raised before making any API call.
        #   2. It asked seller-side `GetEntitlements`, which from a buyer account
        #      returns HTTP 200 with an empty list forever for a usage-based SaaS
        #      listing. That is the finding this platform's live path was rebuilt
        #      around; a fail-closed gate on it denies every real customer while
        #      looking healthy against the simulator.
        #
        # `checkFeatureEntitlement` is the single host-side authority. It resolves
        # the feature's own `licenseMode` and, for `marketplace-live`, asks the
        # buyer-side Agreement API against real AWS — and the UI only offers
        # Launch/Update when it answered ACTIVE. A second gate here, implemented
        # differently, could only ever agree with it by coincidence; when it
        # disagreed the customer saw "Subscription active" on the page and
        # "no entitlement" on the button, which is precisely the contradiction
        # this platform has been unpicking.
        #
        # Nothing is protected by re-checking: the template and code zips are
        # public-read by necessity (see the module docstring), so this was never a
        # confidentiality boundary. The commercial gate is the Marketplace
        # subscription plus the extension's own runtime entitlement check, in the
        # seller's account, which is the only place it can be enforced.
        manifest = None
        # Bare public S3 URL — no presign. A presigned URL could never have
        # covered the objects CloudFormation fetches from the buyer's account
        # anyway, and its expiry broke long-running Update wizard sessions.
        template_url = _s3_https_url(seller_bucket, stack_region, template_key)
        # Same convention as OSS: `templateKey` is the VERSION-FREE
        # `<seller-base>/template.yaml`; its directory is the version-free
        # extension base the feature stack self-locates versioned artifacts
        # under (`<base>/<version>/...`).
        param_feature_bucket = seller_bucket
        param_feature_artifact_prefix = template_key.rsplit("/", 1)[0]
    else:
        # OSS: artifacts live in the artifacts bucket under the version-free
        # extension base `<prefix>/extensions/<id>`, template at
        # `<base>/template.yaml`. Bare template URL (no presign) — same access
        # model as the main-stack quick-create link.
        artifact_bucket = catalog_entry.get("artifactBucket") or ""
        artifact_prefix = catalog_entry.get("artifactPrefix") or ""
        version = args.get("version") or catalog_entry.get("latestVersion") or ""
        if not (artifact_bucket and artifact_prefix and version):
            raise RuntimeError(
                f"OSS feature {feature_id!r} catalog entry is incomplete "
                f"(need artifactBucket, artifactPrefix, latestVersion). Re-publish "
                f"with a current idp-cli."
            )
        manifest = None
        param_feature_bucket = artifact_bucket
        # artifactPrefix is the VERSION-FREE extension base
        # (`<prefix>/extensions/<id>`). The template lives at `<base>/template.yaml`
        # (version-free); the feature stack self-locates its versioned artifacts
        # under `<base>/<version>/...` from its baked FEATURE_VERSION. We pass the
        # version-free base as FeatureArtifactPrefix — nothing version-bearing is
        # stored as a CFN parameter that could go stale on Update.
        param_feature_artifact_prefix = artifact_prefix
        template_url = _oss_template_https_url(
            artifact_bucket, param_feature_artifact_prefix
        )

    # If the feature is already installed, look up its stackName from the
    # InstalledFeatures DDB row written by the RegisterFeature CR at install
    # time; otherwise suggest a sensible new name.
    existing_name = _existing_stack_name(feature_id)
    stack_name = existing_name or f"{_MAIN_STACK_NAME}-feature-{feature_id}"

    # NB: param_feature_artifact_prefix is used only to build the (version-free)
    # template URL above — it is NOT passed as a CFN parameter. The prefix is
    # baked into the template at publish time (see _parameters_for_feature).
    params = _parameters_for_feature(
        feature_id,
        version,
        manifest,
        feature_bucket=param_feature_bucket,
    )

    # Resolve the existing stack's full ARN. If we can — and the stack is in
    # an updatable state — use the update-form URL so the admin lands on
    # CFN Console's "Update stack" flow instead of getting
    # AlreadyExistsException from the create-form URL. If anything goes
    # wrong (stack gone / IAM denied / unhelpful state) we fall back to the
    # create form, preserving pre-fix behaviour.
    stack_arn: Optional[str] = None
    if existing_name:
        stack_arn = _describe_stack_arn(existing_name)

    # The CFN console region for the quick-create/update link is the stack's own
    # region (feature stacks deploy alongside the main stack).
    console_region = os.environ.get("AWS_REGION", _ARTIFACT_REGION)
    if stack_arn:
        launch_url = _build_update_url(console_region, template_url, stack_arn, params)
        is_update = True
    else:
        launch_url = _build_create_url(console_region, template_url, stack_name, params)
        is_update = False

    logger.info(
        "getFeatureLaunchUrl: featureId=%s version=%s isUpdate=%s",
        feature_id,
        version,
        is_update,
    )

    return {
        "featureId": feature_id,
        "version": version,
        "launchUrl": launch_url,
        "templateUrl": template_url,
        "stackName": stack_name,
        "parameters": json.dumps(params),  # AWSJSON
    }
