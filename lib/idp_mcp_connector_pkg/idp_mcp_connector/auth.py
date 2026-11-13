# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Cognito OAuth token manager for the IDP MCP Connector.

Handles client_credentials grant authentication and automatic token refresh.
"""

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class CognitoAuth:
    """
    Manages OAuth 2.0 token lifecycle for Cognito client_credentials grant.

    Tokens are cached in memory and automatically refreshed 60 seconds before
    expiry to ensure uninterrupted operation.
    """

    # Refresh token this many seconds before actual expiry
    EXPIRY_BUFFER_SECONDS = 60

    def __init__(self, token_url: str, client_id: str, client_secret: str):
        """
        Initialize the Cognito authenticator.

        Args:
            token_url: Cognito OAuth token endpoint (MCPTokenURL from CloudFormation outputs)
            client_id: Cognito app client ID (MCPConnectorClientId from CloudFormation outputs)
            client_secret: Cognito app client secret (a service credential's, or the one at MCPConnectorClientSecretArn)
        """
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret

        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0  # Unix timestamp when token expires

    async def get_token(self) -> str:
        """
        Return a valid access token, authenticating or refreshing as needed.

        Returns:
            A valid Bearer token string.

        Raises:
            httpx.HTTPStatusError: If Cognito returns a non-2xx response.
            ValueError: If the token response is missing expected fields.
        """
        if not self._is_token_valid():
            await self._authenticate()
        return self._access_token  # type: ignore[return-value]

    def _is_token_valid(self) -> bool:
        """Check whether the cached token is still valid (with expiry buffer)."""
        if self._access_token is None:
            return False
        return time.time() < (self._token_expiry - self.EXPIRY_BUFFER_SECONDS)

    async def _authenticate(self) -> None:
        """
        Obtain a new access token from Cognito using client_credentials grant.

        Updates internal token cache on success.
        """
        logger.info("Authenticating with Cognito...")

        # Cognito client_credentials grant requires HTTP Basic Auth
        # (client_id:client_secret in Authorization header, not in request body)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "scope": "idp-mcp-connector/access",
                },
                auth=(self._client_id, self._client_secret),
            )
            response.raise_for_status()

        token_data = response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(
                f"Cognito response missing 'access_token'. Response keys: {list(token_data.keys())}"
            )

        expires_in = int(token_data.get("expires_in", 3600))
        self._access_token = access_token
        self._token_expiry = time.time() + expires_in

        logger.info(
            f"Authentication successful. Token valid for {expires_in}s "
            f"(expires at {time.strftime('%H:%M:%S', time.localtime(self._token_expiry))})"
        )
