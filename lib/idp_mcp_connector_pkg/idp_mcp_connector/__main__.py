# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Entry point for the IDP MCP Connector.

Usage:
    python -m idp_mcp_connector

Required environment variables:
    IDP_MCP_ENDPOINT       - AgentCore Gateway URL (MCPServerEndpoint from CloudFormation)
    IDP_MCP_TOKEN_URL      - Cognito OAuth token endpoint (MCPTokenURL from CloudFormation)
    IDP_MCP_CLIENT_ID      - Cognito app client ID (MCPConnectorClientId from CloudFormation)
    IDP_MCP_CLIENT_SECRET  - Cognito app client secret (a service credential's, shown once on the
                             MCP Configuration page, or the one at MCPConnectorClientSecretArn)
"""

import asyncio
import logging
import os
import sys

from .auth import CognitoAuth
from .connector import run_connector

# Configure logging to stderr so it doesn't interfere with MCP stdio communication
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)

# Required environment variables and their CloudFormation output counterparts
REQUIRED_ENV_VARS = {
    "IDP_MCP_ENDPOINT": "MCPServerEndpoint",
    "IDP_MCP_TOKEN_URL": "MCPTokenURL",  # nosec B105 - CFN output name, not a secret
    "IDP_MCP_CLIENT_ID": "a service credential, or MCPConnectorClientId",
    "IDP_MCP_CLIENT_SECRET": "a service credential, or the secret at MCPConnectorClientSecretArn",  # nosec B105 - where to find it, not a secret
}


def _check_env_vars() -> dict[str, str]:
    """
    Read and validate required environment variables.

    Returns:
        Dictionary of environment variable values.

    Exits with code 1 if any required variable is missing.
    """
    missing = []
    values: dict[str, str] = {}

    for var, cf_output in REQUIRED_ENV_VARS.items():
        value = os.environ.get(var)
        if not value:
            missing.append(f"  {var}  ({cf_output})")
        else:
            values[var] = value

    if missing:
        print(
            "IDP MCP Connector: Missing required environment variables:\n"
            + "\n".join(missing)
            + "\n\n"
            "Set these from a service credential on the IDP MCP Configuration page,\n"
            "or from your IDP CloudFormation stack outputs.\n"
            "See: genaiic-idp-accelerator/docs/mcp-connector.md for setup instructions.",
            file=sys.stderr,
        )
        sys.exit(1)

    return values


def _handle_version_flag() -> None:
    """Print version and exit if --version flag is present."""
    if "--version" in sys.argv:
        from . import __version__

        print(f"idp_mcp_connector {__version__}")
        sys.exit(0)


def _handle_help_flag() -> None:
    """Print help and exit if --help flag is present."""
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)


async def _main() -> None:
    """Async entry point: validate config, initialize auth, run connector."""
    logger.info("IDP MCP Connector starting...")

    env = _check_env_vars()

    auth = CognitoAuth(
        token_url=env["IDP_MCP_TOKEN_URL"],
        client_id=env["IDP_MCP_CLIENT_ID"],
        client_secret=env["IDP_MCP_CLIENT_SECRET"],
    )

    await run_connector(
        endpoint=env["IDP_MCP_ENDPOINT"],
        auth=auth,
    )


def main() -> None:
    """Synchronous entry point (used by the idp-mcp-connector CLI script)."""
    _handle_version_flag()
    _handle_help_flag()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("IDP MCP Connector stopped.")
    except Exception as e:
        logger.error(f"IDP MCP Connector failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
