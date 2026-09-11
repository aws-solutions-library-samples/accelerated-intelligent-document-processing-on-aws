# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Code Intelligence Agent implementation using DeepWiki MCP.
"""

import logging
from typing import Any, Dict, Tuple

import boto3
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

from ..common.cost_metrics import with_cost_hook
from ..common.strands_bedrock_model import create_strands_bedrock_model

logger = logging.getLogger(__name__)

# Hardcoded configuration for DeepWiki MCP
# Note: DeepWiki deprecated SSE transport in favor of Streamable HTTP
# See: https://docs.devin.ai/work-with-devin/deepwiki-mcp
DEEPWIKI_MCP_URL = "https://mcp.deepwiki.com/mcp"
REPO_NAME = (
    "aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws"
)


def create_code_intelligence_agent(
    config: Dict[str, Any] = None,
    session: boto3.Session = None,
    **kwargs,
) -> Tuple[Agent, MCPClient]:
    """
    Create Code Intelligence Agent with DeepWiki MCP integration.

    This agent provides code repository intelligence for the IDP codebase using
    the DeepWiki MCP server. The configuration is hardcoded and requires no
    external setup.

    Hardcoded configuration:
    - MCP URL: https://mcp.deepwiki.com/mcp
    - Transport: Streamable HTTP (no authentication)
    - Repository: aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws

    Args:
        config: Configuration dictionary (ignored - agent uses hardcoded config)
        session: Boto3 session for AWS operations. If None, creates default session
        **kwargs: Additional arguments

    Returns:
        Tuple of (Strands Agent, MCP Client)

    Raises:
        Exception: If MCP connection or agent creation fails
    """
    # Get session if not provided
    if session is None:
        session = boto3.Session()

    logger.info("Creating Code Intelligence Agent with DeepWiki MCP")

    try:
        # Create Streamable HTTP MCP client (no authentication needed for DeepWiki)
        logger.info(f"Connecting to DeepWiki MCP at {DEEPWIKI_MCP_URL}")
        mcp_client = MCPClient(lambda: streamablehttp_client(DEEPWIKI_MCP_URL))

        # Discover tools and create agent within MCP context
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
                    logger.warning(f"Unable to extract tool name from MCP tool: {tool}")

            logger.info("Connected to DeepWiki via Streamable HTTP transport")
            logger.info(f"Discovered {len(tools)} tools from DeepWiki: {tool_names}")

            # Get model ID using configuration system (reads user-changed values from DynamoDB)
            try:
                from ..orchestrator.config import get_chat_companion_model_id

                model_id = get_chat_companion_model_id()
            except Exception as e:
                logger.warning(
                    f"Failed to get code intelligence model ID, using default: {e}"
                )
                model_id = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"

            # Create Bedrock model
            bedrock_model = create_strands_bedrock_model(
                model_id=model_id, boto_session=session
            )

            # Create system prompt with repository context and security guardrails
            system_prompt = f"""
You are the Code Intelligence Agent that helps users understand the IDP (Intelligent Document Processing) codebase.

You have access to tools that analyze the repository: {REPO_NAME}

CRITICAL INSTRUCTIONS:
1. You can use "ask_question" and "read_wiki_structure" tools
2. NEVER use "read_wiki_contents" - it causes memory issues and timeouts
3. When calling ANY tool, you MUST always include this parameter:
   {{"repoName": "{REPO_NAME}"}}

Tool Usage Guidelines:
- Use "ask_question" for answering questions about the codebase (PREFERRED)
- Use "read_wiki_structure" to understand directory/file organization if needed
- NEVER use "read_wiki_contents" - it loads too much data and causes failures

Example usage:
{{
    "question": "What is this repository about?",
    "repoName": "{REPO_NAME}"
}}

🔒 SECURITY GUARDRAILS - CRITICAL:
These tools connect to an external public MCP server. You MUST protect sensitive information.

ALLOWED queries (generic codebase questions):
✅ "What is this repository about?"
✅ "Explain the agent architecture"
✅ "How does the document processing workflow work?"
✅ "What are the main components?"
✅ "What AWS services are used?"
✅ "How is the Lambda function structured?"
✅ "What is the deployment process?"

FORBIDDEN queries (contain or could reveal sensitive data):
❌ Questions about specific customer data, documents, or files
❌ Questions about API keys, credentials, secrets, or passwords
❌ Questions about specific AWS account IDs, resource ARNs, or identifiers
❌ Questions about production data, logs, or error messages
❌ Questions about user information, PII, or customer-specific configurations
❌ Questions that include actual data values, file contents, or log entries
❌ Questions about specific deployment environments (prod/staging URLs, IPs)

BEFORE calling any tool, you MUST:
1. Check if the user's query contains or requests sensitive information
2. If it does, REFUSE to call the tool and respond with:
   "I cannot send that query to the external code intelligence service as it may contain sensitive information. I can only answer generic questions about the codebase structure and architecture. Please rephrase your question to be about general code patterns rather than specific data or configurations."
3. If the query is generic and safe, reformulate it to be ONLY about code structure/architecture
4. NEVER include actual data values, credentials, or identifiers in your tool calls

Examples of safe reformulation:
- User: "Why did document ABC123 fail?" 
  → REFUSE (contains specific document ID)
- User: "What's in the config file at /etc/app/config.json?"
  → REFUSE (could contain sensitive configs)
- User: "How does error handling work in the document processor?"
  → SAFE: Call tool with "How does error handling work in the document processing code?"

Your role is to:
- Answer questions about code structure and architecture (using tools when safe)
- Explain implementation details and design decisions (generic patterns only)
- Help developers understand the codebase (without exposing sensitive data)
- Provide insights into how different components work together
- Reference actual code and file locations when possible
- PROTECT sensitive information by refusing unsafe queries

Always be specific and reference the actual repository content in your responses.
Remember: NEVER use read_wiki_contents! NEVER send sensitive data to external services!
"""

            # Create Strands agent with MCP tools
            strands_agent = Agent(
                tools=tools,
                system_prompt=system_prompt,
                model=bedrock_model,
                hooks=with_cost_hook(
                    kwargs.get("hooks"), "code-intelligence", model_id
                ),
            )

        logger.info("Code Intelligence Agent created successfully")
        return strands_agent, mcp_client

    except Exception as e:
        error_msg = f"Failed to create Code Intelligence Agent: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
