# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Query.listInstalledFeatures resolver.

Returns every installed feature in this IDP stack together with its
latest-available version, so the UI can show "Update available" badges.

`latestVersion` is resolved at RUNTIME from each extension's published
``<base>/latest.json``, with the deployed catalog's ``latestVersion`` as a
fallback. That ordering is the point of this resolver:

  - The catalog is baked at HOST-publish time. Reading `latestVersion` from it
    meant a new extension release stayed invisible to customers until the whole
    accelerator was re-released AND the customer took that release — so shipping
    an extension patch required shipping the platform. For a paid Marketplace
    extension on its own release cadence that is untenable.
  - `latest.json` is written by the extension publisher on every release
    (`idp_feature_sdk.publisher._update_latest_json`) and carries
    ``{featureId, version, displayName, bundleSha256, publishedAt}``. Reading it
    live decouples extension releases from host releases entirely — and it
    benefits OSS extensions for exactly the same reason.

Three properties this lookup must hold, because it runs on every page load:

  1. **Fail soft.** Any failure — unreachable object, bad JSON, blocked egress,
     no catalog location — falls back to the catalog value and ultimately to no
     badge. It must NEVER turn into an error, because that would break the
     Extensions list for a cosmetic feature.
  2. **Cached.** Results are memoized per (bucket, key) in the module scope for
     LATEST_JSON_TTL_SECONDS, so a warm container serves page loads without S3
     traffic. Failures are cached too (for a shorter negative TTL) so a missing
     object doesn't cost a round trip every single time.
  3. **Unsigned first, signed second.** The normal path is an ANONYMOUS GET:
     extension artifacts are public-read by design (a Marketplace template URL is
     fetched by AWS Seller Ops and by CloudFormation in an arbitrary buyer
     account), and an unsigned read needs no IAM grant on the host's role and no
     bucket-policy grant from the publisher — so turning this on cannot regress
     an existing deployment. If public read is REFUSED we retry signed, which
     covers an OSS extension self-published to the operator's own private bucket;
     that only succeeds where the operator deliberately granted this role
     s3:GetObject, so it adds a capability rather than a requirement.

No bucket LISTING anywhere: one GetObject of the catalog, plus at most one
GetObject of latest.json per installed feature.

Called by any signed-in user (Viewer and up). Does NOT check entitlement — that is a
separate resolver (`checkFeatureEntitlement`). The UI combines the two.

Environment:
    INSTALLED_FEATURES_TABLE   DynamoDB table name
    CONFIGURATION_BUCKET        Stack's ConfigurationBucket (holds catalog.json)
    CATALOG_KEY                 Catalog key (default config_library/catalog.json)
    HOST_REGION                 Region used to resolve marketplace `regions` (defaults to AWS_REGION)
    LATEST_JSON_LOOKUP          "false" disables the runtime lookup (catalog only)
    LATEST_JSON_TTL_SECONDS     Success cache TTL (default 300)
    LATEST_JSON_NEGATIVE_TTL_SECONDS  Failure cache TTL (default 60)
    LOG_LEVEL                  Logging level (default INFO)
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_HOST_REGION = os.environ.get("HOST_REGION") or os.environ.get("AWS_REGION", "")
_LATEST_JSON_LOOKUP = os.environ.get("LATEST_JSON_LOOKUP", "true").lower() not in (
    "false",
    "0",
    "no",
)
_LATEST_JSON_TTL = int(os.environ.get("LATEST_JSON_TTL_SECONDS", "300"))
_LATEST_JSON_NEGATIVE_TTL = int(
    os.environ.get("LATEST_JSON_NEGATIVE_TTL_SECONDS", "60")
)
# Cap the fan-out so a stack with many installed extensions can't open an
# unbounded number of sockets from one invocation.
_LATEST_JSON_MAX_WORKERS = 8

_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")

# Unsigned S3 client for public extension artifacts. Short timeouts and a single
# retry: this is a cosmetic badge on a page-load path, so a blocked network must
# degrade in ~2s, not stall the invocation. Built lazily so import stays cheap
# and unit tests can patch it.
_public_s3_clients: Dict[str, Any] = {}
_PUBLIC_S3_CONFIG = Config(
    signature_version=UNSIGNED,
    connect_timeout=2,
    read_timeout=3,
    retries={"max_attempts": 2, "mode": "standard"},
)

# {(bucket, key): (expires_at_monotonic, version_or_None)}
_latest_json_cache: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}


def _public_s3(region: str):
    """Cached unsigned S3 client for `region` (clients are thread-safe to use)."""
    client = _public_s3_clients.get(region)
    if client is None:
        client = boto3.client(
            "s3", region_name=region or None, config=_PUBLIC_S3_CONFIG
        )
        _public_s3_clients[region] = client
    return client


def _read_catalog() -> Dict[str, Dict[str, Any]]:
    """Return {featureId: catalog entry} from catalog.json. Empty on any failure.

    Single GetObject against ConfigurationBucket — never lists. A missing catalog
    just means no update badges (the UI still shows installed features).
    """
    if not _CONFIGURATION_BUCKET:
        return {}
    try:
        resp = _s3.get_object(Bucket=_CONFIGURATION_BUCKET, Key=_CATALOG_KEY)
        catalog = json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "404", "NotFound"):
            logger.warning("Failed to read catalog: %s", exc)
        return {}
    except (BotoCoreError, ValueError) as exc:
        logger.warning("Bad catalog JSON: %s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for entry in catalog.get("features") or []:
        if isinstance(entry, dict):
            fid = entry.get("featureId")
            if isinstance(fid, str) and fid:
                out[fid] = entry
    return out


def _catalog_latest_versions(
    entries: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Return {featureId: latestVersion} from the catalog — the FALLBACK value.

    Kept as a named helper (and still callable with no argument) because it is
    the documented fallback path and is asserted directly by tests.
    """
    if entries is None:
        entries = _read_catalog()
    out: Dict[str, str] = {}
    for fid, entry in entries.items():
        ver = entry.get("latestVersion")
        if isinstance(ver, str) and ver:
            out[fid] = ver
    return out


def _latest_json_location(entry: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """Resolve (bucket, key, region) of an extension's latest.json, or None.

    Both feature kinds publish `latest.json` next to the VERSION-FREE template,
    at `<extension base>/latest.json`:

      - marketplace → the caller's region in the schema-1.1 `regions` map, whose
        `templateKey` is `<base>/template.yaml`. Resolved by lookup only; a
        region we don't publish to yields None rather than a guessed bucket.
      - oss → `artifactBucket` + `artifactPrefix` (already the version-free base).

    Deprecated flat marketplace fields are honored for their own declared region.
    """
    source = entry.get("source") or "oss"
    if source == "marketplace":
        regions = entry.get("regions")
        if isinstance(regions, dict) and _HOST_REGION:
            spec = regions.get(_HOST_REGION)
            if isinstance(spec, dict):
                bucket = (spec.get("sellerBucket") or "").strip()
                key = (spec.get("templateKey") or "").strip().lstrip("/")
                if bucket and "/" in key:
                    return bucket, f"{key.rsplit('/', 1)[0]}/latest.json", _HOST_REGION
        legacy_bucket = (entry.get("sellerBucket") or "").strip()
        legacy_region = (entry.get("sellerBucketRegion") or "").strip()
        legacy_key = (entry.get("templateKey") or "").strip().lstrip("/")
        if legacy_bucket and "/" in legacy_key and legacy_region == _HOST_REGION:
            return (
                legacy_bucket,
                f"{legacy_key.rsplit('/', 1)[0]}/latest.json",
                legacy_region,
            )
        return None

    bucket = (entry.get("artifactBucket") or "").strip()
    prefix = (entry.get("artifactPrefix") or "").strip().strip("/")
    if bucket and prefix:
        return bucket, f"{prefix}/latest.json", _HOST_REGION
    return None


_DENIED_CODES = ("AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch")
_ABSENT_CODES = ("NoSuchKey", "404", "NotFound", "NoSuchBucket")


def _read_latest_json_once(client, bucket: str, key: str) -> Tuple[Optional[str], str]:
    """One GetObject attempt. Returns (version_or_None, outcome).

    `outcome` is "ok" | "denied" | "absent" | "error" so the caller can decide
    whether a second attempt with different credentials is worth making.
    """
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        payload = json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _DENIED_CODES:
            return None, "denied"
        if code in _ABSENT_CODES:
            return None, "absent"
        logger.warning("latest.json read failed for s3://%s/%s: %s", bucket, key, exc)
        return None, "error"
    except (BotoCoreError, ValueError, KeyError) as exc:
        logger.warning("latest.json unusable at s3://%s/%s: %s", bucket, key, exc)
        return None, "error"

    candidate = payload.get("version") if isinstance(payload, dict) else None
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip(), "ok"
    logger.warning("latest.json at s3://%s/%s has no usable 'version'", bucket, key)
    return None, "error"


def _fetch_latest_version(bucket: str, key: str, region: str) -> Optional[str]:
    """Read `version` from an extension's latest.json. None on ANY failure.

    Tries UNSIGNED first, then falls back to a signed read:

      - Unsigned is the normal path. Published extension artifacts are
        public-read, and an anonymous GET needs no IAM grant on the host's role
        and no bucket-policy grant from the publisher — so turning this lookup on
        cannot regress an existing deployment.
      - The signed retry covers a PRIVATE artifacts bucket (an OSS extension
        self-published to the operator's own bucket). It only succeeds if the
        operator has deliberately granted this role s3:GetObject, so it adds a
        capability rather than a requirement.

    The retry costs a second round trip only in the already-degraded case, and
    the outcome is negative-cached either way.

    Memoized per (bucket, key) — successes for LATEST_JSON_TTL_SECONDS, failures
    for the shorter negative TTL, so a missing object doesn't cost a round trip
    on every page load.
    """
    cache_key = (bucket, key)
    now = time.monotonic()
    cached = _latest_json_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    version, outcome = _read_latest_json_once(_public_s3(region), bucket, key)
    if outcome == "denied":
        # Public read refused — maybe a private bucket this role can reach.
        version, outcome = _read_latest_json_once(_s3, bucket, key)
    if outcome in ("denied", "absent"):
        logger.info(
            "No readable latest.json at s3://%s/%s (%s); using catalog value",
            bucket,
            key,
            outcome,
        )

    ttl = _LATEST_JSON_TTL if version else _LATEST_JSON_NEGATIVE_TTL
    _latest_json_cache[cache_key] = (now + ttl, version)
    return version


def _runtime_latest_versions(
    feature_ids: List[str], catalog_entries: Dict[str, Dict[str, Any]]
) -> Dict[str, str]:
    """{featureId: version} read live from latest.json, for the given features.

    Only the features actually installed are looked up, concurrently, so wall
    time is one S3 round trip rather than N. Features with no resolvable
    location, or whose fetch fails, are simply absent from the result and fall
    back to the catalog value.
    """
    if not _LATEST_JSON_LOOKUP:
        return {}
    targets = []
    for fid in feature_ids:
        entry = catalog_entries.get(fid)
        if not entry:
            continue
        location = _latest_json_location(entry)
        if location:
            targets.append((fid, location))
    if not targets:
        return {}

    out: Dict[str, str] = {}
    workers = min(_LATEST_JSON_MAX_WORKERS, len(targets))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(
                lambda t: (t[0], _fetch_latest_version(*t[1])),
                targets,
            )
            for fid, version in results:
                if version:
                    out[fid] = version
    except Exception as exc:  # noqa: BLE001 — cosmetic badge, never fail the query
        logger.warning("Runtime latest.json lookup failed wholesale: %s", exc)
        return {}
    return out


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def _prerelease_key(prerelease: str) -> List[Tuple[int, int, str]]:
    """Comparable key for a prerelease string, per SemVer §11.4.

    Identifiers are dot-separated and compared left to right. Numeric ones
    compare NUMERICALLY (so rc.10 > rc.2, which a plain string compare gets
    backwards) and always rank lower than alphanumeric ones.

    Each identifier becomes (is_alphanumeric, numeric_value, text) so tuple
    comparison reproduces those rules without branching at the call site.
    """
    key: List[Tuple[int, int, str]] = []
    for ident in prerelease.split("."):
        if ident.isdigit():
            key.append((0, int(ident), ""))
        else:
            key.append((1, 0, ident))
    return key


def _parse_version(
    value: str,
) -> Optional[Tuple[int, int, int, int, List[Tuple[int, int, str]]]]:
    """Parse a SemVer string into a comparable tuple, or None if unparseable.

    The 4th element is 0 for a prerelease and 1 for a release, so a prerelease
    sorts BEFORE its release (SemVer §11.3). The 5th orders prereleases among
    themselves (§11.4). Build metadata is ignored — §10 says it takes no part in
    precedence, so 1.0.0+a and 1.0.0+b are equal and neither is an "update".
    """
    m = _SEMVER_RE.match(value.strip())
    if not m:
        return None
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    prerelease = m.group(4) or ""
    return (
        major,
        minor,
        patch,
        0 if prerelease else 1,
        _prerelease_key(prerelease) if prerelease else [],
    )


def _update_available(installed: str, latest: Optional[str]) -> bool:
    """True only when `latest` is strictly NEWER than `installed`.

    Previously this was `latest != installed`, which reported "Update available"
    whenever the two merely DIFFERED — including when the catalog was BEHIND the
    installed version. That happens routinely: an extension installed with
    `idp-feature-cli deploy --from-code` (the documented dev loop) publishes its
    own artifacts immediately, while catalog.json only refreshes on a host stack
    create/update. The UI then told an admin running v0.1.1 that v0.1.0 was
    "available" — an invitation to downgrade.

    Unparseable versions fall back to inequality, preserving the old behavior
    for non-SemVer version strings rather than silently suppressing the badge.
    """
    if not latest:
        return False
    lv, iv = _parse_version(latest), _parse_version(installed)
    if lv is None or iv is None:
        logger.warning(
            "Non-SemVer version compare (installed=%r latest=%r); "
            "falling back to inequality",
            installed,
            latest,
        )
        return latest != installed
    return lv > iv


def _row_to_feature(
    row: Dict[str, Any], latest_by_id: Dict[str, str]
) -> Dict[str, Any]:
    """Map a DDB row to the GraphQL `InstalledFeature` shape."""
    feature_id = row["featureId"]
    installed_version = row.get("installedVersion", "0.0.0")
    latest_version = latest_by_id.get(feature_id)
    return {
        "featureId": feature_id,
        "displayName": row.get("displayName", feature_id),
        "installedVersion": installed_version,
        "latestVersion": latest_version,
        "updateAvailable": _update_available(installed_version, latest_version),
        "stackName": row.get("stackName", ""),
        "stackRegion": row.get("stackRegion", ""),
        "stackId": row.get("stackId"),
        "uiBundlePath": row.get("uiBundlePath", ""),
        "featureApiEndpoint": row.get("featureApiEndpoint"),
        "generationQueueArn": row.get("generationQueueArn"),
        "iconUrl": row.get("iconUrl"),
        "installedAt": row.get("installedAt", ""),
        "installedBy": row.get("installedBy"),
    }


def handler(event: Dict[str, Any], context: Any) -> List[Dict[str, Any]]:
    """AppSync resolver entry point."""
    logger.info("listInstalledFeatures event: %s", event)
    if not _INSTALLED_FEATURES_TABLE:
        raise RuntimeError("INSTALLED_FEATURES_TABLE env var is not configured")

    table = _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
    paginator_kwargs: Dict[str, Any] = {}
    items: List[Dict[str, Any]] = []
    while True:
        resp = table.scan(**paginator_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        paginator_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Catalog first (the fallback), then overlay whatever the extensions'
    # published latest.json says right now. Runtime wins because the catalog is
    # frozen at host-publish time; when the runtime read fails for a feature the
    # catalog value simply stays in place.
    catalog_entries = _read_catalog()
    latest_by_id = _catalog_latest_versions(catalog_entries)
    installed_ids = [
        row["featureId"] for row in items if isinstance(row.get("featureId"), str)
    ]
    latest_by_id.update(_runtime_latest_versions(installed_ids, catalog_entries))

    features = [_row_to_feature(row, latest_by_id) for row in items]
    # Stable order by displayName for the UI
    features.sort(key=lambda f: f["displayName"].lower())
    return features
