# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource Lambda — runs on Create / Update / Delete.

Extends the minimal docs-by-status ui-deployer with the two "vertical pack"
registrations the Claims Review sample demonstrates:

On Create or Update:
  1. Copies s3://<FEATURE_BUCKET>/<FEATURE_ARTIFACT_PREFIX>/<FEATURE_VERSION>/ui-bundle.js
     into s3://<WEBUI_BUCKET>/features/<FEATURE_ID>/v<FEATURE_VERSION>/ui-bundle.js
  2. Directly invokes the host's `registerFeature` resolver Lambda to add a
     row to InstalledFeatures.
  3. Downloads the bundled config preset (config-preset/claims-config.yaml,
     uploaded by the publisher under <FEATURE_ARTIFACT_PREFIX>/<FEATURE_VERSION>),
     INJECTS the postRuleValidation hook into its rule_validation.postHook, and
     invokes the host's `applyFeatureConfigPreset` resolver — creating a
     NON-ACTIVE config version
     `sample-health-insurance-review-v<FEATURE_VERSION>` for an admin to activate.
     (The hook travels inside the preset, not via a separate registerFeatureHooks
     call, so activating that version brings the rules and hook together.)

On Delete:
  1. Deletes the copied UI bundle.
  2. Invokes the host's resolver Lambdas for `unregisterFeature`,
     `unregisterFeatureHooks`, and `removeFeatureConfigPreset` (the host
     preserves the preset version if an admin has it active). Failures are
     logged, never block stack delete.

The AppSync transport was removed; the host resolver Lambdas already parse the
AppSync resolver event shape {info:{fieldName}, arguments, identity}, so each
field is dispatched by invoking the appropriate host function directly:
  registerFeature / unregisterFeature        -> REGISTER_FEATURE_FUNCTION_ARN
  unregisterFeatureHooks                     -> REGISTER_FEATURE_HOOKS_FUNCTION_ARN
  applyFeatureConfigPreset / removeFeatureConfigPreset
                                             -> APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN

The Lambda's execution role carries the tag `idp:feature-id=<FEATURE_ID>`
(set in template.yaml) so the main stack's WebUIBucketPolicy allows writes
under `features/<FEATURE_ID>/*` only.
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
_MAIN_STACK_NAME = os.environ["MAIN_STACK_NAME"]
_WEBUI_BUCKET = os.environ["WEBUI_BUCKET"]
_FEATURE_BUCKET = os.environ["FEATURE_BUCKET"]
# Version-FREE base prefix of this extension's artifacts in FEATURE_BUCKET (the
# host's artifacts bucket), e.g. "<prefix>/extensions/<id>". The versioned
# artifacts live under "<base>/<FEATURE_VERSION>/...".
_FEATURE_ARTIFACT_PREFIX = os.environ["FEATURE_ARTIFACT_PREFIX"].rstrip("/")
# ARNs of the host's resolver Lambdas (the AppSync transport was removed).
_REGISTER_FEATURE_FUNCTION_ARN = os.environ["REGISTER_FEATURE_FUNCTION_ARN"]
_REGISTER_FEATURE_HOOKS_FUNCTION_ARN = os.environ["REGISTER_FEATURE_HOOKS_FUNCTION_ARN"]
_APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN = os.environ[
    "APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN"
]
_FEATURE_API_ENDPOINT = os.environ.get("FEATURE_API_ENDPOINT", "")
# ARN of this feature's postRuleValidation hook Lambda (template.yaml).
_HOOK_FUNCTION_ARN = os.environ.get("HOOK_FUNCTION_ARN", "")
# Key of the bundled config preset, relative to the versioned artifact prefix.
# Matches feature.yaml -> configPreset.path (the publisher uploads it under
# "<base>/<version>/<configPreset.path>").
_CONFIG_PRESET_RELATIVE_KEY = os.environ.get(
    "CONFIG_PRESET_RELATIVE_KEY", "config-preset/claims-config.yaml"
)

# Fail fast (with a clear message in CloudWatch) when the publisher's
# `<FEATURE_VERSION_TOKEN>` substitution didn't happen and the env var still
# carries the placeholder. Without this check, we'd build a CopyObject source
# path of `features/<id>/v<FEATURE_VERSION_TOKEN>/ui-bundle.js`, which doesn't
# exist, and surface as an opaque `NoSuchKey` in the
# RegisterFeatureResource UPDATE_FAILED event.
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
            f"time — re-run `idp-feature-cli publish` (third-party) or "
            f"`python publish.py` (bundled) and redeploy. See "
            f"lib/idp_sdk/idp_sdk/_core/publish.py:_upload_sample_feature_artifacts."
        )

_s3 = boto3.client("s3")


def _artifact_prefix() -> str:
    """Versioned source artifact prefix in FEATURE_BUCKET.

    FEATURE_ARTIFACT_PREFIX is the VERSION-FREE base (`<prefix>/extensions/<id>`)
    passed as a CloudFormation parameter. The versioned artifacts the deployer
    reads (ui-bundle.js, config preset) live under `<base>/<FEATURE_VERSION>/`.
    FEATURE_VERSION is baked into the template at publish time (a literal, not a
    parameter), so it can never go stale on a stack Update — which is the whole
    point of this layout: there is no version-bearing CFN parameter to drift.
    """
    return f"{_FEATURE_ARTIFACT_PREFIX}/{_FEATURE_VERSION}"


# ---------------------------------------------------------------------------
# UI bundle copy
# ---------------------------------------------------------------------------
def _bundle_ui(request_type: str) -> str:
    """Return the uiBundlePath registered with the host (used for RegisterFeature)."""
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
            # Short TTL, NOT immutable: the destination key is version-addressed
            # (v<FEATURE_VERSION>/), so version bumps already bust caches — but
            # same-version republishes DO happen (hotfixes re-uploading an
            # existing version), and a year-long immutable header would pin the
            # stale bundle in browsers until the version changes. max-age=300
            # bounds staleness to 5 minutes at negligible re-fetch cost
            # (~15 KB), matching the CloudFront distribution's own MaxTTL.
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
# Feature registration — direct Lambda invoke of the host's resolver Lambdas.
# The AppSync transport was removed; each resolver Lambda already parses the
# AppSync resolver event shape, so we hand it the same event
# {info:{fieldName}, arguments, identity} directly. Each field is routed to the
# host function that owns it.
# ---------------------------------------------------------------------------
_lambda = boto3.client("lambda")


def _invoke_resolver(
    function_arn: str, field_name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Synchronously invoke a host resolver Lambda for a single GraphQL field.

    Builds the AppSync resolver event shape the resolver already understands.
    Raises on a Lambda FunctionError (handler-raised exception).
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


def _unregister_hooks() -> None:
    """Best-effort cleanup of any hook a prior build registered into the active
    config version via registerFeatureHooks. Current builds bake the hook into
    the preset instead, so this is only for backward compatibility on delete."""
    _invoke_resolver(
        _REGISTER_FEATURE_HOOKS_FUNCTION_ARN,
        "unregisterFeatureHooks",
        {"featureId": _FEATURE_ID},
    )


def _inject_post_rule_validation_hook(preset: Dict[str, Any]) -> None:
    """Add this feature's postRuleValidation hook INTO the preset's config,
    under `rule_validation.postHook`, so the hook travels WITH the preset
    version.

    This is the crux of the fix: the host's pipeline-hooks dispatcher reads
    hooks from the *active* config version. If we instead registered the hook
    via registerFeatureHooks (which targets whatever version is active at
    install — typically `default`), then the moment an admin activates THIS
    preset version the hook would be orphaned in the old version and never
    fire. By baking the hook into the preset payload, activating the preset
    brings the rules and the hook together atomically.

    The hook ARN isn't known until the feature stack deploys, so it can't live
    in the committed claims-config.yaml — it's injected here at install time
    from the HOOK_FUNCTION_ARN env var (set via !GetAtt in template.yaml).

    Merges (does not clobber) any existing rule_validation block / postHook
    list, and is idempotent on stack Update: an existing entry for this
    featureId is replaced rather than duplicated.
    """
    if not _HOOK_FUNCTION_ARN:
        logger.warning(
            "HOOK_FUNCTION_ARN not set — preset will carry no postRuleValidation "
            "hook; the Claims Dashboard will not populate."
        )
        return
    rv = preset.get("rule_validation")
    if not isinstance(rv, dict):
        rv = {}
        preset["rule_validation"] = rv
    existing = rv.get("postHook")
    if not isinstance(existing, list):
        existing = []
    # Drop any prior entry for this feature (idempotent re-apply on Update).
    kept = [
        h
        for h in existing
        if not (isinstance(h, dict) and h.get("featureId") == _FEATURE_ID)
    ]
    kept.append(
        {
            "featureId": _FEATURE_ID,
            "arn": _HOOK_FUNCTION_ARN,
            "order": 100,
            "onError": "continue",
            "enabled": True,
        }
    )
    rv["postHook"] = kept
    logger.info(
        "Injected postRuleValidation hook %s into preset rule_validation.postHook",
        _HOOK_FUNCTION_ARN,
    )


def _apply_config_preset() -> None:
    """Download the bundled preset, inject this feature's pipeline hook, and
    hand it to the host.

    The publisher uploads the preset at the same relative path it has in the
    feature project (feature.yaml -> configPreset.path), under this version's
    key prefix. The host writes it as a NON-ACTIVE config version — an admin
    activates it from the Configuration UI; installing never silently changes
    the active configuration.

    Before sending, we inject the postRuleValidation hook into the preset's
    own `rule_validation.postHook` (see _inject_post_rule_validation_hook) so
    the hook is part of the very version the admin activates — no separate
    registerFeatureHooks call, no orphaned hook.
    """
    # Read from the versioned prefix (<base>/<FEATURE_VERSION>/...); the version
    # comes from the baked FEATURE_VERSION, not a stale-able CFN parameter.
    preset_key = f"{_artifact_prefix()}/{_CONFIG_PRESET_RELATIVE_KEY}"
    logger.info("Fetching config preset s3://%s/%s", _FEATURE_BUCKET, preset_key)
    resp = _s3.get_object(Bucket=_FEATURE_BUCKET, Key=preset_key)
    preset = yaml.safe_load(resp["Body"].read().decode("utf-8"))
    if not isinstance(preset, dict):
        raise RuntimeError(f"Config preset at {preset_key} did not parse to a mapping")
    _inject_post_rule_validation_hook(preset)
    result = _invoke_resolver(
        _APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN,
        "applyFeatureConfigPreset",
        {
            "input": {
                "featureId": _FEATURE_ID,
                "version": _FEATURE_VERSION,
                "config": json.dumps(preset),
                "description": (
                    f"{_FEATURE_DISPLAY_NAME} preset — health insurance prior-auth "
                    f"rule validation + claim-status hook "
                    f"(installed by feature v{_FEATURE_VERSION})"
                ),
            }
        },
    )
    logger.info("applyFeatureConfigPreset: %s", result)


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
    """POST the response to the pre-signed URL CloudFormation provided."""
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
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310  # nosec B310 - CloudFormation-supplied custom-resource callback URL (https), not user input
        resp.read()


def lambda_handler(event: Dict[str, Any], _context: Any) -> None:
    logger.info("CFN custom resource: %s", event.get("RequestType"))
    try:
        request_type = event["RequestType"]
        bundle_path = _bundle_ui(request_type)
        if request_type in ("Create", "Update"):
            _register(bundle_path, event["StackId"])
            # NOTE: we deliberately do NOT call registerFeatureHooks here. That
            # mutation writes the hook into whatever config version is ACTIVE at
            # install time (typically `default`); when the admin then activates
            # this feature's preset version, the hook would be orphaned in the
            # old version and never fire. Instead, _apply_config_preset injects
            # the hook directly into the preset's rule_validation.postHook, so
            # the hook travels with the version that gets activated.
            _apply_config_preset()
        elif request_type == "Delete":
            # Never block stack delete on teardown failures — log and move on.
            for label, fn in (
                ("unregisterFeature", _unregister),
                ("unregisterFeatureHooks", _unregister_hooks),
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
