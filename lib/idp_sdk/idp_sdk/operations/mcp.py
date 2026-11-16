# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP service credentials, for scripts and infrastructure as code.

Every rule and every write is in ``idp_common.mcp_credentials``, which the MCP
Configuration page's REST operations call too; this namespace only finds the stack's
resources and calls it, so the CLI, the SDK and the page refuse the same things.

The settings those functions need (the gateway, the token URL, the stack key, the
isolation mode) are read from the credential manager function's own environment,
so a credential made here is configured exactly as one made on the page.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from idp_sdk.exceptions import (
    IDPResourceNotFoundError,
    IDPStackError,
    IDPValidationError,
)
from idp_sdk.models.mcp import McpCredential, McpCredentialIssued

MANAGER_FUNCTION = "McpCredentialManagerFunction"


def _snake(row: Mapping[str, Any]) -> Dict[str, Any]:
    keys = {
        "clientId": "client_id",
        "allowedConfigVersions": "allowed_config_versions",
        "writeTools": "write_tools",
        "createdBy": "created_by",
        "createdAt": "created_at",
        "updatedBy": "updated_by",
        "updatedAt": "updated_at",
        "expiresAt": "expires_at",
        "lastUsedAt": "last_used_at",
        "rotatedFrom": "rotated_from",
        "replacedBy": "replaced_by",
        "secretArn": "secret_arn",
    }
    return {keys.get(k, k): v for k, v in row.items() if v is not None}


def _credential(row: Mapping[str, Any]) -> McpCredential:
    return McpCredential(**_snake(row))


def _issued(result: Mapping[str, Any]) -> McpCredentialIssued:
    return McpCredentialIssued(
        credential=_credential(result["credential"]),
        client_id=result["clientId"],
        client_secret=result["clientSecret"],
        token_url=result.get("tokenUrl"),
        gateway_url=result.get("gatewayUrl"),
        secret_arn=result.get("secretArn"),
        oauth_scopes=list(result.get("oauthScopes") or []),
    )


class McpOperation:
    """MCP service credentials (``client.mcp``)."""

    def __init__(self, client):
        self._client = client

    def _boto(self, service: str):
        import boto3

        return boto3.client(service, region_name=self._client._region)

    def _services(self, stack_name: Optional[str]):
        import boto3

        from idp_common.mcp_credentials import CredentialServices

        name = self._client._require_stack(stack_name)
        function = None
        paginator = self._boto("cloudformation").get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=name):
            for r in page.get("StackResourceSummaries", []):
                if r.get("LogicalResourceId") == MANAGER_FUNCTION:
                    function = r.get("PhysicalResourceId")
        if not function:
            raise IDPResourceNotFoundError(
                f"Stack '{name}' has no MCP credential manager; is EnableMCP true?"
            )
        env: Dict[str, str] = (
            self._boto("lambda")
            .get_function_configuration(FunctionName=function)
            .get("Environment", {})
            .get("Variables", {})
        )
        missing = [
            k
            for k in (
                "MCP_CREDENTIALS_TABLE",
                "GATEWAY_ID",
                "USER_POOL_ID",
                "KMS_KEY_ID",
            )
            if not env.get(k)
        ]
        if missing:
            raise IDPStackError(
                f"The MCP credential manager of '{name}' is missing {missing}"
            )
        dynamodb = boto3.resource("dynamodb", region_name=self._client._region)

        def tenants():
            from idp_common.config.tenants import TenantStore

            table = env.get("CONFIGURATION_TABLE_NAME")
            return TenantStore(dynamodb.Table(table)).list() if table else []

        return CredentialServices(
            cognito=self._boto("cognito-idp"),
            secrets=self._boto("secretsmanager"),
            table=dynamodb.Table(env["MCP_CREDENTIALS_TABLE"]),
            control=self._boto("bedrock-agentcore-control"),
            gateway_id=env["GATEWAY_ID"],
            user_pool_id=env["USER_POOL_ID"],
            stack_name=env.get("STACK_NAME", name),
            kms_key_id=env["KMS_KEY_ID"],
            oauth_scopes=[s for s in env.get("OAUTH_SCOPES", "").split(",") if s],
            token_url=env.get("TOKEN_URL", ""),
            gateway_url=env.get("GATEWAY_URL", ""),
            tenants=tenants,
            # The stack's mode, not this machine's environment.
            environ={
                "TENANT_ISOLATION_MODE": env.get("TENANT_ISOLATION_MODE", "Off"),
                "TENANT_GRANT_SOURCE": env.get("TENANT_GRANT_SOURCE", "Manual"),
            },
        )

    def _actor(self) -> str:
        return str(self._boto("sts").get_caller_identity().get("Arn") or "iam")

    def _call(self, fn, *args, **kwargs):
        from idp_common.mcp_credentials import CredentialError, CredentialNotFound

        try:
            return fn(*args, **kwargs)
        except CredentialNotFound as exc:
            raise IDPResourceNotFoundError(str(exc)) from exc
        except CredentialError as exc:
            raise IDPValidationError(str(exc)) from exc

    def list_credentials(
        self, *, stack_name: Optional[str] = None
    ) -> List[McpCredential]:
        from idp_common import mcp_credentials as mc

        return [_credential(r) for r in mc.list_credentials(self._services(stack_name))]

    def create_credential(
        self,
        name: str,
        role: str,
        *,
        allowed_config_versions: Optional[List[str]] = None,
        write_tools: bool = False,
        expires_in_days: Optional[int] = None,
        stack_name: Optional[str] = None,
    ) -> McpCredentialIssued:
        """Create a credential. The result is the only time its secret is shown;
        it is also stored in Secrets Manager under ``secret_arn``."""
        from idp_common import mcp_credentials as mc

        return _issued(
            self._call(
                mc.create_credential,
                self._services(stack_name),
                name=name,
                role=role,
                allowed_config_versions=allowed_config_versions,
                write_tools=write_tools,
                expires_in_days=expires_in_days,
                actor=self._actor(),
            )
        )

    def update_credential(
        self,
        client_id: str,
        *,
        role: Optional[str] = None,
        allowed_config_versions: Optional[List[str]] = None,
        clear_scope: bool = False,
        write_tools: Optional[bool] = None,
        enabled: Optional[bool] = None,
        stack_name: Optional[str] = None,
    ) -> McpCredential:
        from idp_common import mcp_credentials as mc

        return _credential(
            self._call(
                mc.update_credential,
                self._services(stack_name),
                client_id,
                role=role,
                allowed_config_versions=allowed_config_versions,
                clear_scope=clear_scope,
                write_tools=write_tools,
                enabled=enabled,
                actor=self._actor(),
            )
        )

    def rotate_credential(
        self,
        client_id: str,
        *,
        overlap_days: int = 7,
        stack_name: Optional[str] = None,
    ) -> McpCredentialIssued:
        from idp_common import mcp_credentials as mc

        return _issued(
            self._call(
                mc.rotate_credential,
                self._services(stack_name),
                client_id,
                overlap_days=overlap_days,
                actor=self._actor(),
            )
        )

    def revoke_credential(
        self, client_id: str, *, stack_name: Optional[str] = None
    ) -> McpCredential:
        from idp_common import mcp_credentials as mc

        return _credential(
            self._call(
                mc.revoke_credential,
                self._services(stack_name),
                client_id,
                actor=self._actor(),
            )
        )

    def set_credential_tenant(
        self,
        client_id: str,
        tenant_id: Optional[str],
        *,
        stack_name: Optional[str] = None,
    ) -> McpCredential:
        """Grant a credential one tenant, or none. Only when the stack's
        ``TenantGrantSource`` is ``Api``."""
        from idp_common import mcp_credentials as mc

        return _credential(
            self._call(
                mc.set_credential_tenant,
                self._services(stack_name),
                client_id,
                tenant_id,
                actor=self._actor(),
            )
        )
