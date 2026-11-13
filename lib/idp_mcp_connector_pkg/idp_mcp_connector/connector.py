# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Transparent MCP proxy server for the IDP MCP Connector.

Discovers tools dynamically from the remote IDP MCP Server (AgentCore Gateway)
and forwards all tool calls with automatic Cognito authentication.
"""

import json
import logging
from typing import Any, Optional

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .auth import CognitoAuth

logger = logging.getLogger(__name__)


def _decoded(value: Any) -> Any:
    """``value`` parsed when it is a JSON string, else as given."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def provenance_of(result: Any) -> Optional[dict]:
    """The ``provenance`` envelope the IDP MCP server put on a tool result, or None.

    The gateway returns the tool's Lambda response as text inside a JSON-RPC result:
    ``result.content[].text`` holds ``{"statusCode", "body"}`` and ``body`` holds the
    tool's own JSON. Every layer is parsed in turn, and anything else is ignored, so
    an unexpected shape yields no notice rather than an error.
    """
    if not isinstance(result, dict):
        return None
    inner = result.get("result")
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return None
    for item in content:
        payload = _decoded(item.get("text") if isinstance(item, dict) else None)
        if isinstance(payload, dict) and "body" in payload:
            payload = _decoded(payload["body"])
        if isinstance(payload, dict) and isinstance(payload.get("provenance"), dict):
            return payload["provenance"]
    return None


def notice_text(provenance: dict) -> Optional[str]:
    """The note to show ahead of a result, or ``None`` when the envelope is not the
    shape the server sends (a list of field names and a notice)."""
    fields = provenance.get("documentDerived")
    notice = provenance.get("notice")
    if not (isinstance(fields, list) and fields and isinstance(notice, str) and notice):
        return None
    if not all(isinstance(f, str) for f in fields):
        return None
    return (
        f"Note from the IDP MCP server about the result below ({', '.join(fields)}): "
        f"{notice}"
    )


class IDPMCPConnector:
    """
    Transparent MCP proxy that bridges stdio MCP clients (Cline, Kiro) to the
    remote IDP MCP Server on Amazon Bedrock AgentCore Gateway.

    Tools are discovered dynamically from the remote server at startup and
    re-registered as local MCP tools. All calls are forwarded transparently
    with automatic token management.
    """

    def __init__(self, endpoint: str, auth: CognitoAuth):
        """
        Args:
            endpoint: The AgentCore Gateway MCP endpoint URL (MCPServerEndpoint)
            auth: Initialized CognitoAuth instance for token management
        """
        self._endpoint = endpoint.rstrip("/")
        self._auth = auth
        self._discovered_tools: list[types.Tool] = []

    async def _get_headers(self) -> dict[str, str]:
        """Build authenticated request headers."""
        token = await self._auth.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def discover_tools(self) -> list[types.Tool]:
        """
        Fetch available tools from the remote MCP Server.

        Calls the standard MCP tools/list endpoint and returns the tool
        definitions exactly as provided by the remote server.

        Returns:
            List of Tool objects discovered from the remote server.

        Raises:
            httpx.HTTPStatusError: If the remote server returns a non-2xx response.
        """
        logger.info(f"Discovering tools from IDP MCP Server at {self._endpoint}...")
        headers = await self._get_headers()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            response.raise_for_status()

        data = response.json()

        # Handle JSON-RPC 2.0 envelope: {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}
        # Also handle flat formats for forward-compatibility
        tools_data = []
        if isinstance(data, dict):
            if "result" in data:
                result = data["result"]
                tools_data = result.get("tools", []) if isinstance(result, dict) else []
            elif "tools" in data:
                tools_data = data["tools"]
        elif isinstance(data, list):
            tools_data = data

        tools = []
        for t in tools_data:
            tool = types.Tool(
                name=t["name"],
                description=t.get("description", ""),
                inputSchema=t.get(
                    "inputSchema",
                    t.get("input_schema", {"type": "object", "properties": {}}),
                ),
            )
            tools.append(tool)
            logger.info(f"  Discovered tool: {tool.name}")

        self._discovered_tools = tools
        logger.info(
            f"Discovered {len(tools)} tools: {', '.join(t.name for t in tools)}"
        )
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        """
        Forward a tool call to the remote MCP Server.

        Args:
            name: The tool name to call
            arguments: The tool arguments as a dictionary

        Returns:
            List of TextContent with the tool's response.
        """
        logger.info(f"Forwarding tool call: {name}({json.dumps(arguments)[:200]})")
        headers = await self._get_headers()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self._endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        result = response.json()
        logger.info(f"Tool call '{name}' completed successfully")

        # Normalize result to a readable string
        if isinstance(result, dict):
            result_text = json.dumps(result, indent=2, default=str)
        else:
            result_text = str(result)

        content = [types.TextContent(type="text", text=result_text)]
        provenance = provenance_of(result)
        note = notice_text(provenance) if provenance else None
        if note:
            # Ahead of the result, so the client's model reads it first.
            content.insert(0, types.TextContent(type="text", text=note))
        return content

    def create_mcp_server(self) -> Server:
        """
        Build and return a configured MCP Server instance.

        All discovered tools are registered with the server. Tool calls are
        forwarded transparently to the remote IDP MCP Server.

        Must be called after discover_tools().

        Returns:
            A fully configured MCP Server ready to run on stdio.
        """
        server = Server("idp-mcp-server")

        # Capture connector reference for use in closures
        connector = self

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return connector._discovered_tools

        @server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[types.TextContent]:
            return await connector.call_tool(name, arguments or {})

        return server


async def run_connector(endpoint: str, auth: CognitoAuth) -> None:
    """
    Initialize and run the IDP MCP Connector.

    This is the main entry point that:
    1. Authenticates with Cognito
    2. Discovers tools from the remote MCP Server
    3. Starts the local MCP server on stdio

    Args:
        endpoint: AgentCore Gateway URL (MCPServerEndpoint)
        auth: Initialized CognitoAuth instance
    """
    connector = IDPMCPConnector(endpoint=endpoint, auth=auth)

    # Discover tools from the remote server (this also validates auth + connectivity)
    await connector.discover_tools()

    if not connector._discovered_tools:
        logger.warning(
            "No tools were discovered from the remote MCP Server. "
            "Check that MCP is enabled in your IDP stack (EnableMCP: true) "
            "and that IDP_MCP_ENDPOINT is correct."
        )

    # Build the local MCP server with all discovered tools
    server = connector.create_mcp_server()

    logger.info("IDP MCP Connector ready. Listening on stdio.")

    # Run the MCP server on stdio (blocking until client disconnects)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
