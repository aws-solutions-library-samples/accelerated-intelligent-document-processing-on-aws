# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource Lambda — runs on Create / Update / Delete.

Copies the UMD UI bundle into the host WebUIBucket and registers the feature
with the host, passing the FeatureApi endpoint + the generation queue ARN so the
host's Quick Start tools and the Test Studio "Generate Synthetic Data" button
can discover and call this extension.

The AppSync transport was removed from the host; the host's registerFeature /
unregisterFeature resolver Lambda already parses the AppSync resolver event
shape {info:{fieldName}, arguments, identity}, so we invoke it directly (same
pattern as sample-health-insurance-review). No config preset and no pipeline
hooks — the generator is invoked on-demand via the FeatureApi / generation
queue, not as a per-document pipeline mutation.

The execution role carries the tag `idp:feature-id=<FEATURE_ID>` so the main
stack's WebUIBucketPolicy allows writes under `features/<FEATURE_ID>/*` only.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_FEATURE_ID = os.environ["FEATURE_ID"]
_FEATURE_DISPLAY_NAME = os.environ["FEATURE_DISPLAY_NAME"]
_FEATURE_VERSION = os.environ["FEATURE_VERSION"]
_MAIN_STACK_NAME = os.environ["MAIN_STACK_NAME"]
_WEBUI_BUCKET = os.environ["WEBUI_BUCKET"]
_FEATURE_BUCKET = os.environ["FEATURE_BUCKET"]
_FEATURE_ARTIFACT_PREFIX = os.environ["FEATURE_ARTIFACT_PREFIX"].rstrip("/")
# ARN of the host's registerFeature resolver Lambda (AppSync transport removed).
_REGISTER_FEATURE_FUNCTION_ARN = os.environ["REGISTER_FEATURE_FUNCTION_ARN"]
_FEATURE_API_ENDPOINT = os.environ.get("FEATURE_API_ENDPOINT", "")
_GENERATION_QUEUE_ARN = os.environ.get("GENERATION_QUEUE_ARN", "")

# Fail fast if the publisher's token substitution didn't happen (the env vars
# still carry the <..._TOKEN> placeholders). Same guard as the samples.
for _var, _val in (
    ("FEATURE_VERSION", _FEATURE_VERSION),
    ("FEATURE_ARTIFACT_PREFIX", _FEATURE_ARTIFACT_PREFIX),
):
    if not _val or "TOKEN" in _val:
        raise RuntimeError(
            f"{_var} env var is unsubstituted/empty ({_val!r}). The feature "
            f"template still carries a <..._TOKEN> placeholder — re-run "
            f"`idp-feature-cli publish` and redeploy."
        )

_s3 = boto3.client("s3")


def _artifact_prefix() -> str:
    """Versioned source artifact prefix in FEATURE_BUCKET."""
    return f"{_FEATURE_ARTIFACT_PREFIX}/{_FEATURE_VERSION}"


def _bundle_ui(request_type: str) -> str:
    """Copy/delete the UMD bundle; return the uiBundlePath registered with the host."""
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
            # bounds staleness to 5 minutes at negligible re-fetch cost,
            # matching the CloudFront distribution's own MaxTTL.
            CacheControl="public,max-age=300",
        )
    elif request_type == "Delete":
        logger.info("Deleting s3://%s/%s", _WEBUI_BUCKET, dst_key)
        try:
            _s3.delete_object(Bucket=_WEBUI_BUCKET, Key=dst_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UI bundle delete failed (ignored): %s", exc)

    return f"features/{_FEATURE_ID}/v{_FEATURE_VERSION}/"


# Feature registration — direct Lambda invoke of the host's registerFeature
# resolver. The AppSync transport was removed; the resolver Lambda parses the
# AppSync resolver event shape {info:{fieldName}, arguments, identity}, so we
# hand it that event directly (same pattern as sample-health-insurance-review).
_lambda = boto3.client("lambda")


def _invoke_resolver(field_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronously invoke the host registerFeature resolver for one field."""
    payload = {
        "info": {"fieldName": field_name},
        "arguments": arguments,
        "identity": {"username": "feature-install", "groups": ["Admin"]},
    }
    resp = _lambda.invoke(
        FunctionName=_REGISTER_FEATURE_FUNCTION_ARN,
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
                "generationQueueArn": _GENERATION_QUEUE_ARN or None,
                "installedBy": caller.get("Arn", "unknown"),
            }
        },
    )


def _unregister() -> None:
    _invoke_resolver("unregisterFeature", {"featureId": _FEATURE_ID})


def _send_response(
    event: Dict[str, Any],
    status: str,
    reason: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
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
        elif request_type == "Delete":
            try:
                _unregister()
            except Exception as exc:  # noqa: BLE001
                logger.warning("unregisterFeature failed (ignored): %s", exc)
        _send_response(event, "SUCCESS", "OK")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Custom resource failed")
        _send_response(event, "FAILED", str(exc))
