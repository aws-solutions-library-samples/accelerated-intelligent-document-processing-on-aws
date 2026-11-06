"""Run a CloudFormation custom-resource handler from Terraform.

Terraform invokes this function through ``aws_lambda_invocation`` with
``lifecycle_scope = "CRUD"``. It builds the event CloudFormation would have
sent, gives the handler a pre-signed S3 URL as ``ResponseURL``, invokes the
handler asynchronously, waits for the response object, and returns it. A
``FAILED`` response raises, which fails the Terraform apply with the handler's
reason.

The physical resource id is kept in S3 between calls, because Terraform passes
the previous *input* on update and delete but not the previous *result*, and a
handler needs its physical id for both.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUCKET = os.environ["BRIDGE_BUCKET"]
POLL_SECONDS = 5
# Leave time to return before this function's own timeout.
SAFETY_SECONDS = 20

REGION = os.environ["AWS_REGION"]
_SUFFIX = "amazonaws.com.cn" if REGION.startswith("cn-") else "amazonaws.com"
# Sign against the regional endpoint. A URL for the global endpoint is answered
# with a 307 redirect for a new bucket outside us-east-1, and handlers PUT
# their response with urllib, which does not follow a redirect for PUT.
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.{_SUFFIX}",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
lam = boto3.client("lambda")


def _stringify(value: Any) -> Any:
    """CloudFormation passes every scalar property to a handler as a string."""
    if isinstance(value, dict):
        return {k: _stringify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify(v) for v in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return value
    return str(value)


def _state_key(stack: str, logical_id: str) -> str:
    return f"state/{stack}/{logical_id}.json"


# A role and its policy are created in the same apply as this function, and
# IAM is eventually consistent, so a first call can be refused for a while.
IAM_PROPAGATION_SECONDS = 60
IAM_RETRY_SECONDS = 30


def _get(key: str) -> bytes | None:
    deadline = time.monotonic() + IAM_PROPAGATION_SECONDS
    while True:
        try:
            return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            if code == "AccessDenied" and time.monotonic() < deadline:
                logger.info("access denied reading %s; waiting for IAM", key)
                time.sleep(POLL_SECONDS)
                continue
            raise


def _read_state(stack: str, logical_id: str) -> dict[str, Any] | None:
    body = _get(_state_key(stack, logical_id))
    return json.loads(body) if body is not None else None


def _write_state(stack: str, logical_id: str, state: dict[str, Any]) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=_state_key(stack, logical_id),
        Body=json.dumps(state).encode(),
        ServerSideEncryption="AES256",
    )


def _call(event: dict[str, Any], token: str, context: Any) -> dict[str, Any]:
    key = f"responses/{event['RequestId']}.json"
    event["ResponseURL"] = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=3600,
    )
    logger.info("%s %s (%s)", event["RequestType"], event["LogicalResourceId"], token)
    lam.invoke(FunctionName=token, InvocationType="Event", Payload=json.dumps(event))
    while context.get_remaining_time_in_millis() / 1000 > SAFETY_SECONDS:
        body = _get(key)
        if body is None:
            time.sleep(POLL_SECONDS)
            continue
        s3.delete_object(Bucket=BUCKET, Key=key)
        return json.loads(body)
    raise TimeoutError(
        f"{event['LogicalResourceId']}: handler {token} did not respond before "
        "the bridge timed out; check that function's logs"
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    tf = event.get("tf", {})
    action = tf.get("action", "create")
    request_type = {"create": "Create", "update": "Update", "delete": "Delete"}[action]
    stack = event["StackName"]
    logical_id = event["LogicalResourceId"]
    token = event["ServiceToken"]
    state = _read_state(stack, logical_id) or {}

    region = REGION
    account = context.invoked_function_arn.split(":")[4]
    partition = context.invoked_function_arn.split(":")[1]
    cfn_event: dict[str, Any] = {
        "RequestType": request_type,
        "RequestId": str(uuid.uuid4()),
        "StackId": f"arn:{partition}:cloudformation:{region}:{account}:stack/"
        f"{stack}/terraform",
        "LogicalResourceId": logical_id,
        "ResourceType": event["ResourceType"],
        "ResourceProperties": _stringify(event["ResourceProperties"]),
    }
    if request_type != "Create":
        if "PhysicalResourceId" in state:
            cfn_event["PhysicalResourceId"] = state["PhysicalResourceId"]
        prev = tf.get("prev_input") or {}
        if request_type == "Update" and prev:
            cfn_event["OldResourceProperties"] = _stringify(
                prev.get("ResourceProperties", {})
            )
        if request_type == "Delete" and not state:
            # The record of this resource is gone (the bridge bucket was emptied
            # by hand). Still give the handler its Delete, since most handlers
            # clean up by their properties rather than by physical id, but do
            # not let a failure here block terraform destroy.
            cfn_event["PhysicalResourceId"] = f"{stack}-{logical_id}"
            try:
                response = _call(cfn_event, token, context)
                logger.info(
                    "%s: unrecorded Delete -> %s", logical_id, response.get("Status")
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s: unrecorded Delete failed; continuing", logical_id)
            return {"PhysicalResourceId": "", "Data": {}}

    response = _call(cfn_event, token, context)
    if (
        response.get("Status") != "SUCCESS"
        and "AccessDenied" in str(response.get("Reason", ""))
        and context.get_remaining_time_in_millis() / 1000 > IAM_RETRY_SECONDS * 3
    ):
        # A handler's role and its policy can be created or changed in the same
        # apply as the call, and IAM is eventually consistent. Retry once.
        logger.info("%s: AccessDenied, retrying once after IAM settles", logical_id)
        time.sleep(IAM_RETRY_SECONDS)
        cfn_event["RequestId"] = str(uuid.uuid4())
        response = _call(cfn_event, token, context)
    if response.get("Status") != "SUCCESS":
        raise RuntimeError(
            f"{logical_id} {request_type} failed: {response.get('Reason', '')}"
        )
    physical = response.get("PhysicalResourceId") or cfn_event.get(
        "PhysicalResourceId", context.log_stream_name
    )

    old_physical = state.get("PhysicalResourceId")
    if request_type == "Update" and old_physical and physical != old_physical:
        # CloudFormation deletes the old physical resource after a replacement.
        cleanup = dict(cfn_event, RequestType="Delete", RequestId=str(uuid.uuid4()))
        cleanup["PhysicalResourceId"] = old_physical
        cleanup["ResourceProperties"] = cfn_event.get("OldResourceProperties", {})
        cleanup.pop("OldResourceProperties", None)
        _call(cleanup, token, context)

    data = response.get("Data") or {}
    if request_type == "Delete":
        s3.delete_object(Bucket=BUCKET, Key=_state_key(stack, logical_id))
    else:
        _write_state(stack, logical_id, {"PhysicalResourceId": physical})
    return {"PhysicalResourceId": physical, "Data": data}
