# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource that attaches the MCP caller interceptor to the
stack's AgentCore Gateway, and detaches it on Delete.

A resource of its own, with a function of its own, rather than a step inside the
gateway custom resource, because of what a rollback runs. CloudFormation rolls a
failed update back by running the PREVIOUS template's handlers, and the previous
gateway manager does not know about the interceptor, so it could not take it off
again; the interceptor function would then be deleted while the gateway still
named it, and every MCP request would fail. As its own resource, a rollback or a
downgrade to a release without it deletes this resource, which runs this code's
Delete, before the interceptor function is removed.

Create and Update attach the interceptor and fail the stack operation if they
cannot. Delete detaches it and always reports success, so a detach that fails
cannot block a stack deletion or a rollback; the gateway is either about to be
deleted or is running a handler that does not need it.

A gateway that the gateway custom resource could not create (it reports an id of
``N/A`` in a region where AgentCore is unavailable) has nothing to attach to.
"""

import logging
import os

import boto3
import cfnresponse

from idp_common import mcp_gateway

PHYSICAL_SUFFIX = "|interceptor"

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _control(props):
    return boto3.client(
        "bedrock-agentcore-control",
        region_name=props.get("Region") or os.environ.get("AWS_REGION"),
    )


def handler(event, context):
    props = event.get("ResourceProperties") or {}
    gateway_id = str(props.get("GatewayId") or "")
    interceptor_arn = str(props.get("InterceptorArn") or "")
    physical_id = f"{gateway_id}{PHYSICAL_SUFFIX}"
    request = event.get("RequestType")
    try:
        if request == "Delete":
            # The gateway this resource was attached to is the one its physical id
            # names, which is what CloudFormation sends a Delete for. The
            # properties can name another gateway (a replacement), and detaching
            # there would remove the interceptor from the live one.
            sent = str(event.get("PhysicalResourceId") or physical_id)
            target = (
                sent[: -len(PHYSICAL_SUFFIX)] if sent.endswith(PHYSICAL_SUFFIX) else ""
            )
            try:
                if target and not target.startswith("N/A"):
                    detached = mcp_gateway.detach_interceptor(
                        _control(props), target, interceptor_arn
                    )
                    logger.info(f"Detach from {target}: changed={detached}")
            except Exception as exc:  # noqa: BLE001 - never block a delete/rollback
                logger.error(f"Could not detach the interceptor: {exc}", exc_info=True)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, sent)
            return
        if not gateway_id or gateway_id.startswith("N/A"):
            logger.warning("No gateway exists in this region; nothing to attach to")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
            return
        # ensure_interceptor sets the gateway's whole interceptor list to this one,
        # so an Update naming a replaced function needs no separate detach. An
        # Update naming another gateway changes the physical id, and
        # CloudFormation then sends the old one a Delete, which detaches there.
        changed = mcp_gateway.ensure_interceptor(
            _control(props), gateway_id, interceptor_arn
        )
        logger.info(f"Interceptor on {gateway_id}: changed={changed}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
    except Exception as exc:  # noqa: BLE001 - reported to CloudFormation
        logger.error(f"Could not attach the interceptor: {exc}", exc_info=True)
        cfnresponse.send(
            event, context, cfnresponse.FAILED, {}, physical_id, reason=str(exc)
        )
