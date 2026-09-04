# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
External MCP Agent implementation using Strands framework.
"""

import logging
from typing import Any, Dict, Optional

import boto3
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

from ..common.cost_metrics import with_cost_hook
from ..common.oauth_auth import get_cognito_bearer_token
from ..common.strands_bedrock_model import create_strands_bedrock_model

logger = logging.getLogger(__name__)


def _create_mcp_client_with_transport(
    mcp_url: str, transport: str = "http", bearer_token: Optional[str] = None
) -> MCPClient:
    """
    Create MCP client with specified transport.

    Args:
        mcp_url: MCP server URL
        transport: Transport type ("http" or "sse"), defaults to "http"
        bearer_token: Optional bearer token for authentication

    Returns:
        MCPClient configured with specified transport

    Raises:
        ValueError: If transport type is unsupported
    """
    if transport == "sse":
        logger.info(f"Creating SSE MCP client for {mcp_url}")
        if bearer_token:
            headers = {"authorization": f"Bearer {bearer_token}"}
            logger.info("Using SSE transport with authentication")
            return MCPClient(lambda: sse_client(mcp_url, headers))
        else:
            logger.info("Using SSE transport without authentication")
            return MCPClient(lambda: sse_client(mcp_url))
    elif transport == "http":
        logger.info(f"Creating HTTP MCP client for {mcp_url}")
        if not bearer_token:
            raise ValueError("HTTP transport requires authentication (bearer_token)")
        headers = {
            "authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
        logger.info("Using HTTP transport with authentication")
        return MCPClient(lambda: streamablehttp_client(mcp_url, headers))
    else:
        raise ValueError(
            f"Unsupported transport type: {transport}. Supported types: 'http', 'sse'"
        )


def _validate_mcp_config(mcp_server_config: Dict[str, Any]) -> None:
    """
    Validate MCP server configuration based on transport type.

    Args:
        mcp_server_config: Configuration dictionary

    Raises:
        ValueError: If required fields are missing for transport type
    """
    # mcp_url is always required
    if "mcp_url" not in mcp_server_config:
        raise ValueError("mcp_url is required in MCP server configuration")

    # Get transport type (default to "http" for backward compatibility)
    transport = mcp_server_config.get("transport", "http")

    # Validate transport type
    if transport not in ["http", "sse"]:
        raise ValueError(
            f"Invalid transport type: {transport}. Supported types: 'http', 'sse'"
        )

    # For HTTP transport, Cognito fields are required
    if transport == "http":
        required_cognito_fields = [
            "cognito_user_pool_id",
            "cognito_client_id",
            "cognito_username",
            "cognito_password",
        ]
        missing_fields = [
            field for field in required_cognito_fields if field not in mcp_server_config
        ]
        if missing_fields:
            raise ValueError(
                f"HTTP transport requires Cognito authentication fields: {missing_fields}"
            )

    # For SSE transport, Cognito fields are optional
    logger.info(
        f"Configuration validated for transport type: {transport}"
        + (
            " (with authentication)"
            if transport == "sse" and "cognito_user_pool_id" in mcp_server_config
            else ""
        )
    )


def _get_bearer_token_if_needed(
    mcp_server_config: Dict[str, Any], session: boto3.Session
) -> Optional[str]:
    """
    Get bearer token if Cognito credentials are provided.

    Args:
        mcp_server_config: Configuration dictionary
        session: Boto3 session

    Returns:
        Bearer token string or None if no auth configured

    Raises:
        Exception: If authentication fails
    """
    # Check if all Cognito fields are present
    cognito_fields = [
        "cognito_user_pool_id",
        "cognito_client_id",
        "cognito_username",
        "cognito_password",
    ]
    has_cognito_config = all(field in mcp_server_config for field in cognito_fields)

    if has_cognito_config:
        try:
            logger.info("Cognito credentials provided, authenticating...")
            bearer_token = get_cognito_bearer_token(
                user_pool_id=mcp_server_config["cognito_user_pool_id"],
                client_id=mcp_server_config["cognito_client_id"],
                username=mcp_server_config["cognito_username"],
                password=mcp_server_config["cognito_password"],
                session=session,
            )
            logger.info("Successfully obtained bearer token for MCP authentication")
            return bearer_token
        except Exception as e:
            error_msg = f"Failed to get bearer token: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
    else:
        logger.info(
            "No Cognito credentials provided, proceeding without authentication"
        )
        return None


def create_external_mcp_agent(
    config: Dict[str, Any] = None,
    session: boto3.Session = None,
    model_id: str = None,
    mcp_server_config: Dict[str, Any] = None,
    **kwargs,
) -> Agent:
    """
    Create External MCP Agent that connects to external MCP servers.

    Args:
        config: Configuration dictionary (ignored - MCP agent uses its own config)
        session: Boto3 session for AWS operations. If None, creates default session
        model_id: Model ID to use (required)
        mcp_server_config: Individual MCP server configuration dict (required)
        **kwargs: Additional arguments

    Returns:
        Agent: Configured Strands agent instance with MCP tools

    Raises:
        Exception: If authentication or MCP connection fails
    """
    if mcp_server_config is None:
        raise Exception("mcp_server_config is required")

    # Get session if not provided
    if session is None:
        session = boto3.Session()

    # Get transport type (default to "http" for backward compatibility)
    transport = mcp_server_config.get("transport", "http")
    mcp_url = mcp_server_config["mcp_url"]

    logger.info(f"Creating External MCP Agent with {transport} transport for {mcp_url}")

    # Validate configuration based on transport type
    try:
        _validate_mcp_config(mcp_server_config)
    except ValueError as e:
        error_msg = f"Invalid MCP configuration: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)

    # Get bearer token if Cognito credentials are provided
    bearer_token = _get_bearer_token_if_needed(mcp_server_config, session)

    try:
        # Create MCP client with appropriate transport
        mcp_client = _create_mcp_client_with_transport(mcp_url, transport, bearer_token)

        # Discover tools and create dynamic description within MCP context
        with mcp_client:
            tools = mcp_client.list_tools_sync()

            # Get actual tool names from the MCPAgentTool objects
            tool_names = []
            for tool in tools:
                if hasattr(tool, "tool_name"):
                    tool_names.append(tool.tool_name)
                elif hasattr(tool, "mcp_tool") and hasattr(tool.mcp_tool, "name"):
                    tool_names.append(tool.mcp_tool.name)
                else:
                    raise Exception(
                        f"Unable to extract tool name from MCP tool: {tool}"
                    )

            # Create dynamic description based on available tools
            if tool_names:
                tool_list = ", ".join(tool_names)
                dynamic_description = f"Agent which has access to an external MCP server. The tools available are: {tool_list}."
            else:
                dynamic_description = "Agent which has access to an external MCP server, but no tools were discovered at creation time."

            logger.info(f"Discovered {len(tools)} MCP tools: {tool_names}")

            # Get model ID from parameter or environment variable
            if model_id is None:
                error_msg = "model_id parameter is required"
                logger.error(error_msg)
                raise Exception(error_msg)

            # Create Bedrock model
            bedrock_model = create_strands_bedrock_model(
                model_id=model_id, boto_session=session
            )

            # Create system prompt
            system_prompt = f"""
            You are an AI agent that has access to external tools via MCP (Model Context Protocol).
            
            {dynamic_description}
            
            Use the available tools to help answer user questions. When using tools, provide clear explanations of what you're doing and what the results mean.
            
            If a tool fails or returns an error, explain the issue to the user and suggest alternatives if possible.
            """

            # Create Strands agent with MCP tools
            strands_agent = Agent(
                tools=tools,
                system_prompt=system_prompt,
                model=bedrock_model,
                hooks=with_cost_hook(kwargs.get("hooks"), "external-mcp", model_id),
            )

        # Return the Strands agent - the MCP client will be managed by IDPAgent
        logger.info(
            f"External MCP Agent created successfully using {transport} transport"
        )
        return strands_agent, mcp_client

    except Exception as e:
        error_msg = f"Failed to connect to MCP server at {mcp_url} using {transport} transport: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
