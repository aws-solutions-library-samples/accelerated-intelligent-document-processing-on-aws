# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource Lambda — runs on Create / Update / Delete.

On Create or Update:
  1. Copies s3://<FEATURE_BUCKET>/<FEATURE_ARTIFACT_PREFIX>/<FEATURE_VERSION>/ui-bundle.js
     into s3://<WEBUI_BUCKET>/features/<FEATURE_ID>/v<FEATURE_VERSION>/ui-bundle.js.
  2. Invokes the host's `registerFeature` resolver Lambda to add a row to
     InstalledFeatures.
  3. Downloads the bundled configuration preset, fills the decision model's
     endpoint (the function's `live` alias ARN, DECIDER_ENDPOINT) into its
     section-classification settings, and invokes the host's
     `applyFeatureConfigPreset` resolver, which writes the NON-ACTIVE
     configuration profile `<FEATURE_ID>`. Installing never changes the active
     configuration: an admin reviews the profile and activates it.

On Delete:
  Deletes the copied UI bundle, then invokes `unregisterFeature` and
  `removeFeatureConfigPreset`. Failures are logged and never block the delete.

The host's resolver Lambdas parse the resolver event shape
{info:{fieldName}, arguments, identity}, so each field is dispatched by invoking
the function that owns it:
  registerFeature / unregisterFeature        -> REGISTER_FEATURE_FUNCTION_ARN
  applyFeatureConfigPreset / removeFeatureConfigPreset
                                             -> APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN
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
# Version-free base prefix of this extension's artifacts in FEATURE_BUCKET; the
# versioned artifacts live under "<base>/<FEATURE_VERSION>/...".
_FEATURE_ARTIFACT_PREFIX = os.environ["FEATURE_ARTIFACT_PREFIX"].rstrip("/")
_REGISTER_FEATURE_FUNCTION_ARN = os.environ["REGISTER_FEATURE_FUNCTION_ARN"]
_APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN = os.environ[
    "APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN"
]
# The decision model function's `live` alias ARN (template.yaml).
_DECIDER_ENDPOINT = os.environ.get("DECIDER_ENDPOINT", "")
# Matches feature.yaml -> configPreset.path; the publisher uploads the preset
# under "<base>/<version>/<configPreset.path>".
_CONFIG_PRESET_RELATIVE_KEY = os.environ.get(
    "CONFIG_PRESET_RELATIVE_KEY", "config-preset/section-classification.yaml"
)

#: Where the endpoint goes in the preset.
ENDPOINT_PATH = (
    "classification",
    "split_then_classify",
    "section_classification",
    "decision_model",
    "endpoint",
)

# Fail fast when the publisher's token substitution did not happen: the copy
# below would otherwise fail later with an opaque NoSuchKey.
for _var, _val in (
    ("FEATURE_VERSION", _FEATURE_VERSION),
    ("FEATURE_ARTIFACT_PREFIX", _FEATURE_ARTIFACT_PREFIX),
):
    if not _val or "TOKEN" in _val:
        raise RuntimeError(
            f"{_var} env var is unsubstituted/empty ({_val!r}). Both FEATURE_VERSION "
            f"and FEATURE_ARTIFACT_PREFIX are baked into template.yaml at publish "
            f"time; re-run `idp-feature-cli publish` and redeploy."
        )

_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")


def _artifact_prefix() -> str:
    """Versioned artifact prefix in FEATURE_BUCKET: ``<base>/<FEATURE_VERSION>``."""
    return f"{_FEATURE_ARTIFACT_PREFIX}/{_FEATURE_VERSION}"


def _bundle_ui(request_type: str) -> str:
    """Copy or delete the UI bundle; return the uiBundlePath registered with the host."""
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
            # Short TTL rather than immutable: a same-version republish happens,
            # and matches the CloudFront distribution's own MaxTTL.
            CacheControl="public,max-age=300",
        )
    elif request_type == "Delete":
        logger.info("Deleting s3://%s/%s", _WEBUI_BUCKET, dst_key)
        try:
            _s3.delete_object(Bucket=_WEBUI_BUCKET, Key=dst_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UI bundle delete failed (ignored): %s", exc)
    return f"features/{_FEATURE_ID}/v{_FEATURE_VERSION}/"


def _invoke_resolver(
    function_arn: str, field_name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Synchronously invoke a host resolver Lambda for one field.

    Raises on a Lambda FunctionError (an exception the resolver raised).
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
                "stackName": stack_id.split("/")[-2] if "/" in stack_id else stack_id,
                "stackId": stack_id,
                "stackRegion": region,
                "uiBundlePath": ui_bundle_path,
                "featureApiEndpoint": None,
                "installedBy": caller.get("Arn", "unknown"),
            }
        },
    )


def _unregister() -> None:
    _invoke_resolver(
        _REGISTER_FEATURE_FUNCTION_ARN, "unregisterFeature", {"featureId": _FEATURE_ID}
    )


def fill_endpoint(preset: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
    """Set the decision model endpoint in ``preset``, creating the path as needed.

    Everything else in the preset is kept. Raises when ``endpoint`` is empty: a
    preset that selects the decision model with no endpoint is refused by the
    configuration model, so installing one would fail later and further from
    the cause.
    """
    if not endpoint:
        raise RuntimeError(
            "DECIDER_ENDPOINT is empty: the preset selects the decision model and "
            "needs the function's ARN"
        )
    node: Dict[str, Any] = preset
    for key in ENDPOINT_PATH[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[ENDPOINT_PATH[-1]] = endpoint
    return preset


def _apply_config_preset() -> None:
    """Download the bundled preset, fill in the endpoint, and hand it to the host."""
    preset_key = f"{_artifact_prefix()}/{_CONFIG_PRESET_RELATIVE_KEY}"
    logger.info("Fetching config preset s3://%s/%s", _FEATURE_BUCKET, preset_key)
    resp = _s3.get_object(Bucket=_FEATURE_BUCKET, Key=preset_key)
    preset = yaml.safe_load(resp["Body"].read().decode("utf-8"))
    if not isinstance(preset, dict):
        raise RuntimeError(f"Config preset at {preset_key} did not parse to a mapping")
    fill_endpoint(preset, _DECIDER_ENDPOINT)
    logger.info("Filled decision model endpoint %s into the preset", _DECIDER_ENDPOINT)
    result = _invoke_resolver(
        _APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN,
        "applyFeatureConfigPreset",
        {
            "input": {
                "featureId": _FEATURE_ID,
                "version": _FEATURE_VERSION,
                "config": json.dumps(preset),
                "description": (
                    f"{_FEATURE_DISPLAY_NAME}: section classification by the "
                    f"decision model (installed by feature v{_FEATURE_VERSION})"
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
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310  # nosec B310 - CloudFormation-supplied custom-resource callback URL (https), not user input
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
