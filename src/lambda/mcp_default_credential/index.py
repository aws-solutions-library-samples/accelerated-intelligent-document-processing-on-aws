# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource that records the stack-created MCP client
(``MCPConnectorClient``) in the MCP credential registry as "Default
(stack-created)".

From then on that client is resolved like any other credential, so an Admin can
narrow, disable, rotate or revoke it on the MCP Configuration page. Its row keeps
the access it always had (Author, no scope, write tools on), and is created
disabled where tenant isolation forbids an unscoped caller
(``idp_common.mcp_credentials.register_stack_default``).

A resource and a function of its own, like the interceptor attachment, so a
rollback or a downgrade deletes it with this code: Delete removes the row (only a
row of that kind), and the earlier release's interceptor resolves the client as it
did before.
"""

import logging
import os

import boto3
import cfnresponse

from idp_common import mcp_credentials

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _setting(props, prop: str, env: str, otherwise: str) -> str:
    """The resource property, else the function's environment, else ``otherwise``.

    ``otherwise`` for the isolation mode is ``Strict``, so a value that reached
    neither place registers the client disabled rather than unscoped."""
    return str(props.get(prop) or os.environ.get(env) or otherwise)


def handler(event, context):
    props = event.get("ResourceProperties") or {}
    client_id = str(props.get("ClientId") or "")
    physical_id = f"{client_id}|mcp-default-credential"
    table = boto3.resource("dynamodb").Table(str(props.get("TableName") or ""))
    try:
        if event.get("RequestType") == "Delete":
            try:
                removed = mcp_credentials.remove_stack_default(table, client_id)
                logger.info(f"Removed the stack-created credential row: {removed}")
            except Exception as exc:  # noqa: BLE001 - never block a delete/rollback
                logger.error(f"Could not remove the row: {exc}", exc_info=True)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
            return
        row = mcp_credentials.register_stack_default(
            table,
            client_id,
            secret_arn=str(props.get("SecretArn") or ""),
            environ={
                "TENANT_ISOLATION_MODE": _setting(
                    props, "TenantIsolationMode", "TENANT_ISOLATION_MODE", "Strict"
                ),
                "TENANT_GRANT_SOURCE": _setting(
                    props, "TenantGrantSource", "TENANT_GRANT_SOURCE", "Manual"
                ),
            },
        )
        logger.info(
            f"Stack-created credential {client_id}: enabled={bool(row.get('enabled'))}"
        )
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
    except Exception as exc:  # noqa: BLE001 - reported to CloudFormation
        logger.error(
            f"Could not register the stack-created credential: {exc}", exc_info=True
        )
        cfnresponse.send(
            event, context, cfnresponse.FAILED, {}, physical_id, reason=str(exc)
        )
