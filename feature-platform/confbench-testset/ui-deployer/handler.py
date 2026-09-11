# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource Lambda — runs on Create / Update / Delete.

On Create or Update:
  1. Copies s3://<FEATURE_BUCKET>/<FEATURE_ARTIFACT_PREFIX>/<FEATURE_VERSION>/ui-bundle.js
     into s3://<WEBUI_BUCKET>/features/<FEATURE_ID>/v<FEATURE_VERSION>/ui-bundle.js
  2. Invokes the host's `registerFeature` resolver to add an InstalledFeatures row.
  3. Applies the bundled Invoice config preset as a NON-ACTIVE Configuration
     Profile `confbench-testset` for an admin to activate. Each release is a
     REVISION of that profile, not a profile of its own.

On Delete:
  1. Deletes the copied UI bundle.
  2. Invokes `unregisterFeature` and `removeFeatureConfigPreset`. Failures are
     logged, never block stack delete.

Note this deployer does NOT ingest any documents. Installing the extension
creates only machinery; the 32.71 GB dataset moves only when an admin starts an
ingest job from the feature UI. That separation is the whole point — it keeps
install fast and makes the storage cost an explicit, subset-able decision rather
than a side effect of deploying the accelerator.

Config-version naming and Test Studio
-------------------------------------
Test Studio auto-selects a configuration by matching the config version name to
the *test set id* (src/ui/src/components/test-studio/TestRunner.tsx). That
convention serves the main stack's `managed_config/*` entries, whose version
names are bare (`fake-w2`, `realkie-fcc-verified`).

The platform resolver always derives `<featureId>-v<version>`, so this
extension's preset lands as `confbench-testset-v0.1.0`, which no `confbench*`
test set id can equal.

Rather than bend the naming, the host now lets a test set DECLARE its config
version: the ingest planner writes `configVersion` onto each test-set record and
Test Studio preselects it (falling back to the id-name convention, then to the
active version). So the name derived here does not need to match anything — it
just needs to be what the planner records, which is why the same
`<featureId>-v<version>` string is computed in both places (see
`config_version_name()` below and CONFIG_VERSION_NAME in the template). That
string is the feature id alone: the host records each release as a REVISION of
one profile rather than creating `<featureId>-v<version>` per release.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

import boto3
import yaml

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_FEATURE_ID = os.environ["FEATURE_ID"]
_FEATURE_DISPLAY_NAME = os.environ["FEATURE_DISPLAY_NAME"]
_FEATURE_VERSION = os.environ["FEATURE_VERSION"]
_WEBUI_BUCKET = os.environ["WEBUI_BUCKET"]
_FEATURE_BUCKET = os.environ["FEATURE_BUCKET"]
# Version-FREE base prefix of this extension's artifacts in FEATURE_BUCKET.
# Versioned artifacts live under "<base>/<FEATURE_VERSION>/...".
_FEATURE_ARTIFACT_PREFIX = os.environ["FEATURE_ARTIFACT_PREFIX"].rstrip("/")
_REGISTER_FEATURE_FUNCTION_ARN = os.environ["REGISTER_FEATURE_FUNCTION_ARN"]
_APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN = os.environ[
    "APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN"
]
_FEATURE_API_ENDPOINT = os.environ.get("FEATURE_API_ENDPOINT", "")
_CONFIG_PRESET_RELATIVE_KEY = os.environ.get(
    "CONFIG_PRESET_RELATIVE_KEY", "config-preset/confbench-config.yaml"
)

# Fail fast (with a clear CloudWatch message) when the publisher's token
# substitution didn't happen. Without this we'd build a CopyObject source of
# `.../v<FEATURE_VERSION_TOKEN>/ui-bundle.js`, which surfaces as an opaque
# NoSuchKey in the RegisterFeatureResource failure event.
for _var, _val in (
    ("FEATURE_VERSION", _FEATURE_VERSION),
    ("FEATURE_ARTIFACT_PREFIX", _FEATURE_ARTIFACT_PREFIX),
):
    if not _val or "TOKEN" in _val:
        raise RuntimeError(
            f"{_var} env var is unsubstituted/empty ({_val!r}). The feature "
            f"template still carries a <..._TOKEN> placeholder (or it was wired "
            f"to an empty CFN parameter). Both FEATURE_VERSION and "
            f"FEATURE_ARTIFACT_PREFIX are baked into template.yaml at publish "
            f"time — re-run `idp-feature-cli publish` and redeploy."
        )

_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")


def _artifact_prefix() -> str:
    """Versioned source artifact prefix in FEATURE_BUCKET."""
    return f"{_FEATURE_ARTIFACT_PREFIX}/{_FEATURE_VERSION}"


# ---------------------------------------------------------------------------
# UI bundle copy
# ---------------------------------------------------------------------------
def _bundle_ui(request_type: str) -> str:
    """Copy (or remove) the UMD bundle; returns the registered uiBundlePath."""
    src_key = f"{_artifact_prefix()}/ui-bundle.js"
    dst_key = f"features/{_FEATURE_ID}/v{_FEATURE_VERSION}/ui-bundle.js"

    if request_type in ("Create", "Update"):
        logger.info(
            "Copying s3://%s/%s -> s3://%s/%s",
            _FEATURE_BUCKET,
            src_key,
            _WEBUI_BUCKET,
            dst_key,
        )
        _s3.copy_object(
            CopySource={"Bucket": _FEATURE_BUCKET, "Key": src_key},
            Bucket=_WEBUI_BUCKET,
            Key=dst_key,
            MetadataDirective="REPLACE",
            ContentType="application/javascript",
            # Short TTL, NOT immutable: the key is version-addressed so version
            # bumps bust caches, but same-version republishes do happen
            # (hotfixes), and a long immutable header would pin a stale bundle
            # in browsers. 5 minutes matches the host distribution's MaxTTL.
            CacheControl="public,max-age=300",
        )
    elif request_type == "Delete":
        logger.info("Deleting s3://%s/%s", _WEBUI_BUCKET, dst_key)
        try:
            _s3.delete_object(Bucket=_WEBUI_BUCKET, Key=dst_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UI bundle delete failed (ignored): %s", exc)

    return f"features/{_FEATURE_ID}/v{_FEATURE_VERSION}/"


# ---------------------------------------------------------------------------
# Host resolver invocation
# ---------------------------------------------------------------------------
def _invoke_resolver(
    function_arn: str, field_name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Synchronously invoke a host resolver Lambda for a single GraphQL field.

    The AppSync transport was removed, but the host's resolver Lambdas still
    parse the AppSync resolver event shape, so we hand them that shape directly.
    """
    payload = {
        "info": {"fieldName": field_name},
        "arguments": arguments,
        "identity": {"username": "feature-install", "groups": ["Admin"]},
    }
    resp = _lambda.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = resp["Payload"].read().decode("utf-8")
    if resp.get("FunctionError"):
        raise RuntimeError(f"{field_name} resolver failed: {body}")
    return json.loads(body) if body else {}


def _register(ui_bundle_path: str, stack_id: str) -> None:
    caller = boto3.client("sts").get_caller_identity()
    region = os.environ.get("AWS_REGION", "us-east-1")
    _invoke_resolver(
        _REGISTER_FEATURE_FUNCTION_ARN,
        "registerFeature",
        {
            "input": {
                "featureId": _FEATURE_ID,
                "displayName": _FEATURE_DISPLAY_NAME,
                "installedVersion": _FEATURE_VERSION,
                "stackName": os.environ.get("AWS_STACK_NAME", stack_id.split("/")[-2])
                if "/" in stack_id
                else stack_id,
                "stackId": stack_id,
                "stackRegion": region,
                "uiBundlePath": ui_bundle_path,
                "featureApiEndpoint": _FEATURE_API_ENDPOINT or None,
                "installedBy": caller.get("Arn", "unknown"),
            }
        },
    )


def _unregister() -> None:
    _invoke_resolver(
        _REGISTER_FEATURE_FUNCTION_ARN, "unregisterFeature", {"featureId": _FEATURE_ID}
    )


# ---------------------------------------------------------------------------
# Config preset
# ---------------------------------------------------------------------------
def _load_preset() -> Dict[str, Any]:
    preset_key = f"{_artifact_prefix()}/{_CONFIG_PRESET_RELATIVE_KEY}"
    logger.info("Fetching config preset s3://%s/%s", _FEATURE_BUCKET, preset_key)
    resp = _s3.get_object(Bucket=_FEATURE_BUCKET, Key=preset_key)
    preset = yaml.safe_load(resp["Body"].read().decode("utf-8"))
    if not isinstance(preset, dict):
        raise RuntimeError(f"Config preset at {preset_key} did not parse to a mapping")
    return preset


def config_version_name() -> str:
    """The Configuration Profile the host's resolver will create.

    Mirrors `_profile_name()` in the platform's apply_feature_config_preset
    resolver: ONE profile per feature, named for the feature. It used to be
    `<featureId>-v<version>` — a new profile per release — which made every
    upgrade mint another access-control object. The feature's releases are now
    revisions of this one profile.
    """
    return _FEATURE_ID


def _apply_config_preset() -> None:
    """Apply the bundled Invoice preset as a non-active config version."""
    preset = _load_preset()
    result = _invoke_resolver(
        _APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN,
        "applyFeatureConfigPreset",
        {
            "input": {
                "featureId": _FEATURE_ID,
                "version": _FEATURE_VERSION,
                "config": json.dumps(preset),
                "description": (
                    f"ConfBench Invoice extraction schema — same schema as "
                    f"RealKIE-FCC-Verified so clean-vs-degraded accuracy is "
                    f"directly comparable (installed by {_FEATURE_DISPLAY_NAME} "
                    f"v{_FEATURE_VERSION})"
                ),
            }
        },
    )
    logger.info(
        "Applied config preset as version %s: %s", config_version_name(), result
    )


def _remove_config_preset() -> None:
    _invoke_resolver(
        _APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN,
        "removeFeatureConfigPreset",
        {"featureId": _FEATURE_ID},
    )


# ---------------------------------------------------------------------------
# CloudFormation custom-resource protocol
# ---------------------------------------------------------------------------
def _send_response(
    event: Dict[str, Any],
    status: str,
    reason: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """PUT the response to the pre-signed URL CloudFormation provided."""
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason,
            "PhysicalResourceId": event.get("PhysicalResourceId")
            or f"{_FEATURE_ID}-{event['LogicalResourceId']}",
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data or {},
        }
    ).encode("utf-8")
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    # ResponseURL is the pre-signed S3 callback CloudFormation supplies in the
    # custom-resource event; this is the required CFN response protocol and the
    # same pattern every other feature's ui-deployer uses.
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # noqa: S310
        resp.read()


def lambda_handler(event: Dict[str, Any], _context: Any) -> None:
    logger.info("CFN custom resource: %s", event.get("RequestType"))
    try:
        request_type = event["RequestType"]
        bundle_path = _bundle_ui(request_type)
        if request_type in ("Create", "Update"):
            _register(bundle_path, event["StackId"])
            _apply_config_preset()
        elif request_type == "Delete":
            # Never block stack delete on teardown failures — log and move on.
            for label, fn in (
                ("unregisterFeature", _unregister),
                ("removeFeatureConfigPreset", _remove_config_preset),
            ):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s failed (ignored): %s", label, exc)
        _send_response(event, "SUCCESS", "OK")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Custom resource failed")
        _send_response(event, "FAILED", str(exc))
