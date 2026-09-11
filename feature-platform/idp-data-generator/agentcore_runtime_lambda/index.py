# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom resource Lambda: manage the AgentCore Runtime hosting the generator.

AgentCore Runtime has no native CloudFormation resource, so this control-plane
custom resource creates / updates / deletes it via the bedrock-agentcore-control
API (clone of the shape used by agentcore_gateway_manager). It runs after the
container image has been built and pushed to ECR.

Create  -> CreateAgentRuntime (HTTP protocol, PUBLIC network), poll to READY.
Update  -> UpdateAgentRuntime with the same container URI so the :latest image
           digest is re-resolved when a new image was pushed (BuildHash change).
Delete  -> DeleteAgentRuntime.

The runtime ARN is returned as the ``AgentRuntimeArn`` output and the agent
runtime id is the PhysicalResourceId.
"""

import json
import logging
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_READY = "READY"
_TERMINAL_FAIL = ("CREATE_FAILED", "UPDATE_FAILED")
_POLL_INTERVAL = 15
_MAX_WAIT = 780  # 13 minutes — stay within the Lambda 15-min ceiling


def _control_client():
    return boto3.client("bedrock-agentcore-control")


def _find_runtime_id(client, name):
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return rt.get("agentRuntimeId")
    return None


def _wait_ready(client, runtime_id):
    elapsed = 0
    while elapsed < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        rt = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt.get("status")
        logger.info("Runtime %s status: %s (%ds)", runtime_id, status, elapsed)
        if status == _READY:
            return rt
        if status in _TERMINAL_FAIL:
            raise RuntimeError(f"AgentCore Runtime entered {status}")
    raise TimeoutError(f"Runtime {runtime_id} not READY within {_MAX_WAIT}s")


def _create_or_update(props):
    client = _control_client()
    name = props["AgentRuntimeName"]
    container_uri = props["ContainerUri"]
    role_arn = props["ExecutionRoleArn"]
    env = props.get("EnvironmentVariables", {}) or {}

    kwargs = dict(
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": container_uri}
        },
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables={k: str(v) for k, v in env.items()},
    )

    runtime_id = _find_runtime_id(client, name)
    if runtime_id:
        logger.info("Updating existing runtime %s (%s)", name, runtime_id)
        client.update_agent_runtime(agentRuntimeId=runtime_id, **kwargs)
    else:
        logger.info("Creating runtime %s", name)
        resp = _create_with_slr_retry(client, name, kwargs)
        runtime_id = resp["agentRuntimeId"]

    rt = _wait_ready(client, runtime_id)
    return runtime_id, rt["agentRuntimeArn"]


def _create_with_slr_retry(client, name, kwargs):
    """Create the runtime, tolerating service-linked-role propagation lag.

    The bedrock-agentcore SLR is created on first runtime creation and is
    eventually consistent; CreateAgentRuntime can transiently fail with a
    "Failed creating service linked role" message until it propagates.
    """
    last_err = None
    for attempt in range(6):
        try:
            return client.create_agent_runtime(agentRuntimeName=name, **kwargs)
        except ClientError as e:
            msg = str(e)
            if (
                "service linked role" in msg.lower()
                or "service-linked role" in msg.lower()
            ):
                last_err = e
                logger.info("SLR not ready (attempt %d), retrying in 10s", attempt + 1)
                time.sleep(10)
                continue
            raise
    raise last_err


_SENTINEL_IDS = {"pending", "synthesis-agentcore-runtime"}


def _delete(props, runtime_id):
    client = _control_client()
    name = props.get("AgentRuntimeName")
    target = runtime_id if runtime_id and runtime_id not in _SENTINEL_IDS else None
    if not target and name:
        target = _find_runtime_id(client, name)
    if not target:
        logger.info("No runtime to delete for %s", name)
        return
    try:
        client.delete_agent_runtime(agentRuntimeId=target)
        logger.info("Deleted runtime %s", target)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return
        raise


def handler(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId", "synthesis-agentcore-runtime")

    try:
        if request_type == "Delete":
            _delete(props, physical_id)
            _send(event, context, "SUCCESS", physical_id)
            return

        runtime_id, runtime_arn = _create_or_update(props)
        _send(
            event,
            context,
            "SUCCESS",
            runtime_id,
            data={"AgentRuntimeArn": runtime_arn, "AgentRuntimeId": runtime_id},
        )
    except Exception as e:
        logger.exception("AgentCore Runtime custom resource failed")
        _send(event, context, "FAILED", physical_id, reason=str(e))


def _send(event, context, status, physical_resource_id, data=None, reason=""):
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
            "PhysicalResourceId": physical_resource_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data or {},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="PUT",
    )
    urllib.request.urlopen(req)  # noqa: S310  # nosec B310 - CloudFormation-supplied custom-resource callback URL (https), not user input
