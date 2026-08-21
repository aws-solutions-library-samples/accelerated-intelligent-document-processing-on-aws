# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Mutation resolver for registerFeature / unregisterFeature.

Called by a feature stack's `RegisterFeature` custom resource during stack create
(`registerFeature`) and stack delete (`unregisterFeature`). Authenticated via
@aws_iam — the feature stack's custom-resource Lambda role must be granted
`appsync:GraphQL` on the main stack's API.

A `registerFeature` call is idempotent: if the featureId already exists, the row
is overwritten with the new values. This lets feature stacks safely replay the CR
(e.g. during Update) and makes upgrading a feature to a newer version a simple
overwrite.

Environment:
    INSTALLED_FEATURES_TABLE   DynamoDB table name
    LOG_LEVEL                  Logging level (default INFO)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")

_dynamodb = boto3.resource("dynamodb")

# Required top-level fields on the `input` payload.
_REQUIRED_FIELDS = (
    "featureId",
    "displayName",
    "installedVersion",
    "stackName",
    "stackId",
    "stackRegion",
    "uiBundlePath",
)

# Optional fields that will be copied through if present.
_OPTIONAL_FIELDS = (
    "featureApiEndpoint",
    "generationQueueArn",
    "iconUrl",
    "installedBy",
    # Marketplace identity, baked from the feature manifest at publish time and
    # persisted here so the host's subscribe/entitlement resolvers can read the
    # product code straight from the install row — no per-host configuration.
    "productCode",
    "marketplaceListingUrl",
    # Which authority the EXTENSION ITSELF enforces its subscription against
    # ("none" | "simulated" | "marketplace-live"), baked from the manifest the
    # same way. This is what makes the host's catalog entry verifiable rather
    # than aspirational: checkFeatureEntitlement prefers this value, so the host
    # checks the authority the extension actually honours, and reports a mismatch
    # when the catalog disagrees. Absent for anything installed before this
    # existed — the resolver falls back to the catalog, then the stack setting.
    "licenseMode",
)

# Mirrors _LICENSE_MODES in check_feature_entitlement. An unrecognised value is
# dropped rather than persisted: the row is the highest-priority input to the
# host's authority resolution, so letting a typo through would silently pick an
# authority nobody chose. Dropping it falls back to the catalog entry.
_LICENSE_MODES = ("none", "simulated", "marketplace-live")


def _validate_input(payload: Dict[str, Any]) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        raise ValueError(f"registerFeature input missing required fields: {missing}")
    feature_id = payload["featureId"]
    # featureId must be a DNS/S3-prefix-safe slug (lowercase letters, digits, hyphens).
    # Also used as a session-tag value so it must avoid whitespace.
    if not feature_id.replace("-", "").isalnum() or feature_id != feature_id.lower():
        raise ValueError(
            f"Invalid featureId {feature_id!r}: must be lowercase alphanumerics and hyphens only"
        )
    if len(feature_id) > 63:
        raise ValueError(f"Invalid featureId {feature_id!r}: max length 63")


def _register(payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_input(payload)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item: Dict[str, Any] = {
        "featureId": payload["featureId"],
        "displayName": payload["displayName"],
        "installedVersion": payload["installedVersion"],
        "stackName": payload["stackName"],
        "stackId": payload["stackId"],
        "stackRegion": payload["stackRegion"],
        "uiBundlePath": payload["uiBundlePath"],
        "installedAt": now,
    }
    for f in _OPTIONAL_FIELDS:
        val = payload.get(f)
        if val:
            item[f] = val

    declared_mode = item.get("licenseMode")
    if declared_mode is not None and declared_mode not in _LICENSE_MODES:
        # Deliberately does NOT name a source file. The host has no idea where the
        # extension got this value: the bundled SDK bakes it from the manifest's
        # `marketplace.licenseMode`, but an extension is equally free to declare it
        # as a CloudFormation Mappings constant, a stack parameter, or anything
        # else its ui-deployer can read — the host only requires it in the payload.
        # An earlier version of this message told the operator to fix "the
        # manifest", which is wrong advice for any extension that declares it
        # elsewhere.
        logger.warning(
            "Feature %s sent licenseMode=%r in its registerFeature payload, which "
            "is not one of %s. Dropping it; the host will fall back to its catalog "
            "entry for this feature. Fix whatever the extension's ui-deployer "
            "reads the value from.",
            item["featureId"],
            declared_mode,
            ", ".join(_LICENSE_MODES),
        )
        item.pop("licenseMode")

    table = _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
    table.put_item(Item=item)
    logger.info(
        "Registered feature %s v%s (stack %s)",
        item["featureId"],
        item["installedVersion"],
        item["stackName"],
    )

    # Return the InstalledFeature shape (no need for latestVersion in the mutation response).
    return {
        "featureId": item["featureId"],
        "displayName": item["displayName"],
        "installedVersion": item["installedVersion"],
        "latestVersion": None,
        "updateAvailable": False,
        "stackName": item["stackName"],
        "stackRegion": item["stackRegion"],
        "stackId": item["stackId"],
        "uiBundlePath": item["uiBundlePath"],
        "featureApiEndpoint": item.get("featureApiEndpoint"),
        "generationQueueArn": item.get("generationQueueArn"),
        "iconUrl": item.get("iconUrl"),
        "installedAt": item["installedAt"],
        "installedBy": item.get("installedBy"),
    }


def _unregister(feature_id: str) -> bool:
    if not feature_id:
        raise ValueError("unregisterFeature requires a featureId")
    table = _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
    # delete_item is a no-op if the item doesn't exist — return True either way
    # so a feature-stack's delete CR can idempotently retry.
    table.delete_item(Key={"featureId": feature_id})
    logger.info("Unregistered feature %s", feature_id)
    return True


def handler(event: Dict[str, Any], context: Any) -> Any:
    """AppSync resolver entry point for both registerFeature and unregisterFeature."""
    logger.info("register/unregister feature event: %s", event)
    if not _INSTALLED_FEATURES_TABLE:
        raise RuntimeError("INSTALLED_FEATURES_TABLE env var is not configured")

    field = event.get("info", {}).get("fieldName", "")
    args = event.get("arguments", {}) or {}

    if field == "registerFeature":
        return _register(args.get("input", {}) or {})
    if field == "unregisterFeature":
        return _unregister(args.get("featureId", ""))

    raise ValueError(f"Unknown field for register_feature handler: {field!r}")
