# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP service-credential models."""

from typing import List, Optional

from pydantic import BaseModel, Field


class McpCredential(BaseModel):
    """One MCP service credential, as the registry records it. Never the secret."""

    client_id: str
    name: Optional[str] = None
    role: Optional[str] = None
    allowed_config_versions: List[str] = Field(default_factory=list)
    tenant: Optional[str] = None
    write_tools: bool = False
    enabled: bool = False
    status: Optional[str] = Field(
        default=None, description="active, disabled, expired or revoked"
    )
    kind: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    rotated_from: Optional[str] = None
    replaced_by: Optional[str] = None
    secret_arn: Optional[str] = None


class McpCredentialIssued(BaseModel):
    """What create and rotate return: the only results that carry the secret."""

    credential: McpCredential
    client_id: str
    client_secret: str = Field(repr=False)
    token_url: Optional[str] = None
    gateway_url: Optional[str] = None
    secret_arn: Optional[str] = None
    oauth_scopes: List[str] = Field(default_factory=list)
