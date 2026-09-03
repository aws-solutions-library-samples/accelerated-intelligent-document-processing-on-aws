# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolver for applyFeatureConfigPreset / removeFeatureConfigPreset.

Completes the manifest's `configPreset` contract: the publisher uploads the
preset file next to the feature's artifacts, the feature stack's ui-deployer
custom resource downloads it at install time and calls
`applyFeatureConfigPreset` (IAM-auth) with the parsed config. This resolver
writes it to the host's ConfigurationTable as a NON-ACTIVE **Configuration
Profile** named after the feature:

    Config#<featureId>
      IsActive: false        # never auto-activated — an admin opts in
      Managed: false         # so an admin can delete it in the UI
      Description: <description>
      LatestRevision: n      # one revision per feature release
      <config payload fields...>

One profile per feature, one revision per feature release
---------------------------------------------------------
This used to write `Config#<featureId>-v<version>` — a new **profile** for every
release of the feature. Twelve releases of one feature meant twelve profiles, and
a profile is not a version: it is an access-control object (an admin must add it
to every scoped user's `allowedConfigVersions`), a document-visibility partition,
a confidence-curve bucket in Test Studio, and a row in the Configuration Profiles
table forever. That is *lineage*, and lineage now has a home — revisions. See
docs/configuration-profiles.md and issue #697.

So the profile name carries no version, and each install/upgrade cuts a revision
of the same profile. An upgrade is then a diff an admin can read, and the
no-op suppression in `ConfigurationManager` means an upgrade that does not change
the preset records nothing at all.

Why the preset is written as a FULL config now
----------------------------------------------
A preset is typically a SPARSE overlay: it sets only the sections the feature
cares about (`classes`, `rule_validation`) and relies on host defaults for the
rest (ocr, classification, extraction). The old code wrote it raw and unflagged,
relying on `get_merged_configuration()` to merge it over `Config#default` on
first read and auto-migrate it to full.

That merge still has to happen — but it now happens HERE, at install time,
against the host's `Config#default`, so that:

  * the revision body is the same shape a later admin edit would produce.
    Recording a sparse body would make the first diff after an admin's edit show
    the entire configuration as "added", because it would be comparing an overlay
    with a full snapshot.
  * the config that gets recorded is the one that will actually run.

If `Config#default` cannot be read, or `idp_common` is unavailable, the function
falls back to the old raw sparse write (unflagged, so read-time merge still
applies) and logs loudly. Failing a feature install because history could not be
recorded would be the wrong trade.

Removal
-------
`removeFeatureConfigPreset` deletes `Config#<featureId>` **and** every legacy
`Config#<featureId>-v*` row, together with their revision history. If a target is
the ACTIVE profile, `Config#default` is activated first: a feature's config
outliving the feature stack means the pipeline runs a configuration whose hooks
point at deleted Lambdas, which is worse than a config change at uninstall time.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_CONFIG_TABLE = os.environ["CONFIGURATION_TABLE"]

# ConfigurationManager reads the table name from its own env var. Exported here
# (rather than passed per-call) so every helper below shares one manager config.
os.environ.setdefault("CONFIGURATION_TABLE_NAME", _CONFIG_TABLE)

_FEATURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")

_DEFAULT_PROFILE = "default"

# Row-level metadata fields owned by the configuration manager. The preset
# payload must not smuggle these in (they would corrupt version bookkeeping).
_CONFIG_METADATA_FIELDS = {
    "Configuration",
    "CreatedAt",
    "UpdatedAt",
    "IsActive",
    "Description",
    "Managed",
    "BdaProjectArn",
    "BdaSyncStatus",
    "BdaLastSyncedAt",
    "LatestRevision",
    "PublishedRevision",
}

_dynamodb = boto3.resource("dynamodb")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _profile_name(feature_id: str) -> str:
    """The Configuration Profile a feature owns: one per feature, not per release."""
    return feature_id


def _legacy_key_prefix(feature_id: str) -> str:
    """Key prefix of the per-release profiles written before #697."""
    return f"Config#{feature_id}-v"


def _parse_config(raw: Any) -> Dict[str, Any]:
    """AWSJSON arrives as a JSON-encoded string; direct Lambda tests may
    pass a dict. Normalize to a dict and strip metadata/underscore keys."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a JSON object; got {type(raw).__name__}")
    return {
        k: v
        for k, v in raw.items()
        if k not in _CONFIG_METADATA_FIELDS and not k.startswith("_")
    }


def _manager() -> Optional[Any]:
    """
    A ConfigurationManager, or None when `idp_common` is not on the path.

    None is the degraded mode: the preset is still applied, just without a
    revision. The layer is asserted statically by
    scripts/sdlc/tests/test_resolver_layer_coverage.py, so None in production
    means the template lost its layer — hence the error-level log.
    """
    try:
        from idp_common.config.configuration_manager import ConfigurationManager

        return ConfigurationManager(table_name=_CONFIG_TABLE)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "idp_common is unavailable (%s); the config preset will be applied "
            "WITHOUT recording a revision. Check that this function still has the "
            "IDPCommon base layer.",
            exc,
        )
        return None


def _merge_over_default(manager: Any, preset: Dict[str, Any]) -> Optional[Any]:
    """
    The preset overlaid on the host's `Config#default`, validated as an IDPConfig.

    Returns None when the default profile cannot be read or the result does not
    validate — the caller then falls back to the raw sparse write, which is what
    this function's absence used to do implicitly on every install.
    """
    try:
        from copy import deepcopy

        from idp_common.config.merge_utils import deep_update
        from idp_common.config.models import IDPConfig

        default_config = manager.get_configuration("Config", version=_DEFAULT_PROFILE)
        if not isinstance(default_config, IDPConfig):
            logger.warning(
                "Config#%s is not readable as a full configuration (%s); writing the "
                "preset as a sparse overlay instead",
                _DEFAULT_PROFILE,
                type(default_config).__name__,
            )
            return None

        merged = deepcopy(default_config.model_dump(mode="python"))
        # deep_update replaces lists wholesale, so a preset's `classes` list
        # overrides the default's rather than appending to it — the intended
        # behavior for a feature that ships its own document classes.
        deep_update(merged, preset)
        return IDPConfig(**merged)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not merge the preset over Config#%s (%s); writing it as a sparse "
            "overlay instead",
            _DEFAULT_PROFILE,
            exc,
        )
        return None


def _write_sparse(
    profile: str, config: Dict[str, Any], description: str, feature_id: str
) -> str:
    """
    The pre-#697 write: a raw, unflagged sparse overlay, no revision.

    Kept as the fallback path. The row is deliberately NOT flagged
    `_config_format: "full"` — an unflagged row is merged over the host defaults
    on first read and auto-migrated, whereas flagging a sparse overlay "full"
    skips that merge and surfaces at runtime as missing required fields (e.g.
    "No system_prompt found in classification configuration").
    """
    table = _dynamodb.Table(_CONFIG_TABLE)
    config_key = f"Config#{profile}"
    existing = (table.get_item(Key={"Configuration": config_key})).get("Item") or {}
    timestamp = _now()
    item: Dict[str, Any] = {
        "Configuration": config_key,
        "_feature_id": feature_id,
        "CreatedAt": existing.get("CreatedAt", timestamp),
        "UpdatedAt": timestamp,
        # Never resurrect IsActive=false onto a row an admin has since activated.
        "IsActive": existing.get("IsActive", False),
        "Description": description,
        "Managed": False,
        **config,
    }
    table.put_item(Item=item)
    return timestamp


def _stamp_feature_owner(profile: str, feature_id: str) -> None:
    """
    Record which feature owns the profile.

    The profile name no longer carries the feature version, and after #697 it is
    just `<featureId>` — indistinguishable from a profile an admin created with
    that name. `_feature_id` is the provenance marker that survives a rename of
    the display convention, and it is what a future orphan-cleanup would key on.
    It lives outside the config body (leading underscore) so it never reaches
    IDPConfig validation.
    """
    try:
        _dynamodb.Table(_CONFIG_TABLE).update_item(
            Key={"Configuration": f"Config#{profile}"},
            UpdateExpression="SET #fid = :fid",
            ExpressionAttributeNames={"#fid": "_feature_id"},
            ExpressionAttributeValues={":fid": feature_id},
            ConditionExpression="attribute_exists(Configuration)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not stamp _feature_id on Config#%s: %s", profile, exc)


def _apply(payload: Dict[str, Any]) -> Dict[str, Any]:
    feature_id = payload.get("featureId") or ""
    version = payload.get("version") or ""
    if not _FEATURE_ID_RE.match(feature_id):
        raise ValueError(f"Invalid featureId {feature_id!r}")
    if not _VERSION_RE.match(version):
        raise ValueError(f"Invalid version {version!r}")
    config = _parse_config(payload.get("config"))
    if not config:
        raise ValueError("config must contain at least one configuration field")
    description = payload.get("description") or (
        f"Config preset installed by feature {feature_id} v{version}"
    )

    profile = _profile_name(feature_id)
    manager = _manager()
    merged = _merge_over_default(manager, config) if manager else None
    revision: Optional[int] = None

    if manager is not None and merged is not None:
        # save_configuration preserves the existing row's IsActive and CreatedAt,
        # and cuts the revision. An install that changes nothing records nothing.
        manager.save_configuration(
            "Config",
            merged,
            version=profile,
            description=description,
            created_by=f"feature:{feature_id}",
            revision_notes=f"Config preset from {feature_id} v{version}",
        )
        try:
            revision = manager.resolve_published_revision(profile)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the revision of Config#%s: %s", profile, exc)
        timestamp = _now()
    else:
        timestamp = _write_sparse(profile, config, description, feature_id)

    _stamp_feature_owner(profile, feature_id)

    logger.info(
        "Applied config preset Config#%s for feature %s v%s (%d top-level fields, "
        "revision=%s)",
        profile,
        feature_id,
        version,
        len(config),
        f"r{revision}" if revision is not None else "not recorded",
    )
    return {
        "featureId": feature_id,
        "configVersionName": profile,
        "appliedAt": timestamp,
        "configRevision": revision,
    }


def _profiles_owned_by(table: Any, feature_id: str) -> List[Tuple[str, bool]]:
    """
    Every profile this feature created: `Config#<featureId>` plus the legacy
    per-release `Config#<featureId>-v*` rows. Returns (profile name, is_active).

    The legacy rows are swept because a stack that installed the feature before
    #697 accumulated one profile per release, and only the version being
    uninstalled would otherwise be removed — which is how a dev stack ends up
    with twelve orphaned profiles from one uninstalled feature.
    """
    found: List[Tuple[str, bool]] = []
    exact_key = f"Config#{feature_id}"
    item = (table.get_item(Key={"Configuration": exact_key})).get("Item") or {}
    if item:
        found.append((feature_id, bool(item.get("IsActive"))))

    legacy_prefix = _legacy_key_prefix(feature_id)
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(Configuration, :p)",
        "ExpressionAttributeValues": {":p": legacy_prefix},
        "ProjectionExpression": "Configuration, IsActive",
    }
    while True:
        resp = table.scan(**scan_kwargs)
        for row in resp.get("Items") or []:
            name = str(row["Configuration"]).split("#", 1)[1]
            found.append((name, bool(row.get("IsActive"))))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return found


def _remove(feature_id: str) -> bool:
    if not _FEATURE_ID_RE.match(feature_id or ""):
        raise ValueError(f"Invalid featureId {feature_id!r}")
    table = _dynamodb.Table(_CONFIG_TABLE)
    manager = _manager()

    targets = _profiles_owned_by(table, feature_id)
    deleted, failed = 0, 0
    for profile, is_active in targets:
        if is_active:
            # A feature's configuration must not outlive the feature stack: its
            # inline hooks point at Lambdas this uninstall is deleting, so leaving
            # it active means every subsequent document runs against dangling hook
            # ARNs. Hand the pipeline back to `default` first.
            logger.warning(
                "Config#%s is ACTIVE; activating Config#%s before deleting it so the "
                "pipeline is not left pointing at this feature's deleted hooks",
                profile,
                _DEFAULT_PROFILE,
            )
            try:
                if manager is not None:
                    manager.activate_version(_DEFAULT_PROFILE)
                else:
                    table.update_item(
                        Key={"Configuration": f"Config#{profile}"},
                        UpdateExpression="SET IsActive = :f",
                        ExpressionAttributeValues={":f": False},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Could not activate Config#%s; leaving Config#%s in place rather "
                    "than deleting the running configuration: %s",
                    _DEFAULT_PROFILE,
                    profile,
                    exc,
                )
                failed += 1
                continue

        try:
            if manager is not None:
                # Goes through the manager so the profile's revision index and its
                # revision bodies in S3 go with it; a raw delete_item would leave
                # them orphaned, and a later profile of the same name would appear
                # to inherit this one's history.
                manager.delete_configuration("Config", version=profile)
            else:
                table.delete_item(Key={"Configuration": f"Config#{profile}"})
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not delete Config#%s: %s", profile, exc)
            failed += 1

    logger.info(
        "removeFeatureConfigPreset(%s): deleted=%d failed=%d (candidates=%d)",
        feature_id,
        deleted,
        failed,
        len(targets),
    )
    return True


def handler(event: Dict[str, Any], _context: Any) -> Any:
    logger.info("applyFeatureConfigPreset event: %s", event)
    field = event.get("info", {}).get("fieldName", "")
    args = event.get("arguments", {}) or {}
    if field == "applyFeatureConfigPreset":
        return _apply(args.get("input", {}) or {})
    if field == "removeFeatureConfigPreset":
        return _remove(args.get("featureId", ""))
    raise ValueError(f"Unknown field: {field!r}")
