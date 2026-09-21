#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Integration test for Code Intelligence Agent with DeepWiki MCP.

Run directly:
    python test_code_intelligence.py
"""

import logging
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from idp_common.agents.code_intelligence.agent import create_code_intelligence_agent

logger = logging.getLogger(__name__)


def test_code_intelligence_agent_deepwiki_connection():
    """
    Test that Code Intelligence Agent can connect to DeepWiki
    and answer questions about the IDP repository.

    This test verifies:
    1. Agent creation succeeds
    2. MCP client is returned
    3. Tools are discovered from DeepWiki
    4. Agent can respond to repository questions
    """
    print("=" * 60)
    print("Code Intelligence Agent - DeepWiki Connection Test")
    print("=" * 60)

    # Set required environment variable
    os.environ["DOCUMENT_ANALYSIS_AGENT_MODEL_ID"] = (
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
    )

    print("\n1. Creating Code Intelligence Agent...")

    try:
        # Create agent
        agent, mcp_client = create_code_intelligence_agent()

        # Verify agent and client were created
        if agent is None:
            print("❌ FAILED: Agent was not created")
            return False

        if mcp_client is None:
            print("❌ FAILED: MCP client was not returned")
            return False

        print("✅ Agent created successfully")

        # Verify agent has tools
        if not hasattr(agent, "tool_names"):
            print("❌ FAILED: Agent doesn't have tool_names attribute")
            return False

        tool_names = list(agent.tool_names) if agent.tool_names else []

        if len(tool_names) == 0:
            print("❌ FAILED: No tools discovered from DeepWiki")
            return False

        print(f"✅ Discovered {len(tool_names)} tools: {tool_names}")

        # Test query about repository
        print("\n2. Testing query: 'What is this repository about?'")

        # Use agent within MCP client context
        with mcp_client:
            response = agent("What is this repository about?")

        # Extract text from AgentResult
        if hasattr(response, "text"):
            response_text = response.text
        elif hasattr(response, "content"):
            response_text = response.content
        else:
            response_text = str(response)

        # Verify response is not empty
        if response_text is None or len(response_text) == 0:
            print("❌ FAILED: Response is empty")
            return False

        print(f"✅ Response received ({len(response_text)} characters)")
        print(f"\nResponse preview:\n{response_text[:400]}...\n")

        # Verify response contains relevant keywords
        response_lower = response_text.lower()
        relevant_keywords = [
            "document",
            "processing",
            "idp",
            "intelligent",
            "aws",
            "repository",
        ]

        found_keywords = [kw for kw in relevant_keywords if kw in response_lower]

        if len(found_keywords) < 2:
            print(
                f"⚠️  WARNING: Response contains only {len(found_keywords)} relevant keywords: {found_keywords}"
            )
            print("Expected at least 2 keywords from:", relevant_keywords)
        else:
            print(f"✅ Response contains relevant keywords: {found_keywords}")

        print("\n" + "=" * 60)
        print("✅ TEST PASSED - Code Intelligence Agent is working!")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"\n❌ TEST FAILED with error: {str(e)}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    # Run test
    success = test_code_intelligence_agent_deepwiki_connection()

    # Exit with appropriate code
    sys.exit(0 if success else 1)
