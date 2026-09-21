#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test script for orchestrator with subagents.

This script tests that the conversational orchestrator properly delegates
to subagents and that subagent responses are returned correctly.

Usage:
    python test_orchestrator_with_subagents.py
"""

import argparse
import logging
import sys

import boto3

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def test_orchestrator_with_analytics_agent():
    """Test that orchestrator properly delegates to analytics agent."""
    # logger.info("=" * 60)
    # logger.info("Test: Orchestrator with Analytics Agent")
    # logger.info("=" * 60)

    try:
        from idp_common.agents.analytics import get_analytics_config
        from idp_common.agents.factory import agent_factory

        # Get configuration
        config = get_analytics_config()
        session = boto3.Session()
        session_id = "test-orchestrator-analytics"

        # Get all registered agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        logger.info(f"Registered agents: {agent_ids}")

        # Create conversational orchestrator
        orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids, session_id=session_id, config=config, session=session
        )

        logger.info("Orchestrator created successfully")

        # Test 1: Ask a question that should route to analytics agent
        analytics_question = "How many documents have been processed?"

        logger.info(f"\nSending question: {analytics_question}")
        logger.info("This should route to Analytics Agent...")

        response = orchestrator(analytics_question)

        # Convert AgentResult to string if needed
        response_text = str(response) if not isinstance(response, str) else response

        logger.info(f"\nResponse received: {response_text[:200]}...")

        # Check if response mentions analytics or data
        if any(
            keyword in response_text.lower()
            for keyword in ["data", "query", "database", "analytics", "agent", "error"]
        ):
            logger.info("✅ Response appears to be from Analytics Agent")
            logger.info(f"Full response: {response_text}")
            return True
        else:
            logger.warning(
                "⚠️  Response doesn't clearly indicate Analytics Agent was used"
            )
            logger.info(f"Full response: {response_text}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_orchestrator_with_error_analyzer_agent():
    """Test that orchestrator properly delegates to error analyzer agent."""
    logger.info("\n" + "=" * 60)
    logger.info("Test: Orchestrator with Error Analyzer Agent")
    logger.info("=" * 60)

    try:
        from idp_common.agents.analytics import get_analytics_config
        from idp_common.agents.factory import agent_factory

        # Get configuration
        config = get_analytics_config()
        session = boto3.Session()
        session_id = "test-orchestrator-error-analyzer"

        # Get all registered agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        # Create conversational orchestrator
        orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids, session_id=session_id, config=config, session=session
        )

        logger.info("Orchestrator created successfully")

        # Test 2: Ask a question that should route to error analyzer agent
        error_question = "Are there any recent errors in the system?"

        logger.info(f"\nSending question: {error_question}")
        logger.info("This should route to Error Analyzer Agent...")

        response = orchestrator(error_question)

        # Convert AgentResult to string if needed
        response_text = str(response) if not isinstance(response, str) else response

        logger.info(f"\nResponse received: {response_text[:200]}...")

        # Check if response mentions errors or analysis
        if any(
            keyword in response_text.lower()
            for keyword in ["error", "log", "cloudwatch", "analyze", "agent"]
        ):
            logger.info("✅ Response appears to be from Error Analyzer Agent")
            logger.info(f"Full response: {response_text}")
            return True
        else:
            logger.warning(
                "⚠️  Response doesn't clearly indicate Error Analyzer Agent was used"
            )
            logger.info(f"Full response: {response_text}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_orchestrator_with_code_intelligence_agent():
    """Test that orchestrator properly delegates to code intelligence agent."""
    logger.info("\n" + "=" * 60)
    logger.info("Test: Orchestrator with Code Intelligence Agent")
    logger.info("=" * 60)

    try:
        import time

        from idp_common.agents.analytics import get_analytics_config
        from idp_common.agents.factory import agent_factory

        # Get configuration
        config = get_analytics_config()
        session = boto3.Session()
        session_id = "test-orchestrator-code-intelligence"

        # Get all registered agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        logger.info(f"Registered agents: {agent_ids}")

        # Create conversational orchestrator
        start_time = time.time()
        orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids, session_id=session_id, config=config, session=session
        )
        creation_time = time.time() - start_time

        logger.info(f"Orchestrator created successfully in {creation_time:.2f}s")

        # Test: Ask a question that should route to code intelligence agent
        code_question = (
            "What is this repository about? Can you explain the main components?"
        )

        logger.info(f"\nSending question: {code_question}")
        logger.info("This should route to Code Intelligence Agent...")

        query_start = time.time()
        response = orchestrator(code_question)
        query_time = time.time() - query_start

        # Convert AgentResult to string if needed
        response_text = str(response) if not isinstance(response, str) else response

        logger.info(f"\n⏱️  Response received in {query_time:.2f} seconds")
        logger.info(f"Response length: {len(response_text)} characters")

        # Check if response mentions code/repository concepts
        if any(
            keyword in response_text.lower()
            for keyword in [
                "repository",
                "code",
                "component",
                "agent",
                "file",
                "directory",
            ]
        ):
            logger.info("✅ Response appears to be from Code Intelligence Agent")
            logger.info(f"Full response: {response_text}")
            return True
        else:
            logger.warning(
                "⚠️  Response doesn't clearly indicate Code Intelligence Agent was used"
            )
            logger.info(f"Full response: {response_text}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_orchestrator_routing():
    """Test that orchestrator properly routes to different agents."""
    logger.info("\n" + "=" * 60)
    logger.info("Test: Orchestrator Routing Logic")
    logger.info("=" * 60)

    try:
        from idp_common.agents.analytics import get_analytics_config
        from idp_common.agents.factory import agent_factory

        # Get configuration
        config = get_analytics_config()
        session = boto3.Session()
        session_id = "test-orchestrator-routing"

        # Get all registered agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        logger.info(f"Testing with {len(agent_ids)} agents: {agent_ids}")

        # Create conversational orchestrator
        orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids, session_id=session_id, config=config, session=session
        )

        # Test general question
        general_question = "What agents are available?"

        logger.info(f"\nSending question: {general_question}")
        logger.info("This should be answered by orchestrator directly...")

        response = orchestrator(general_question)

        # Convert AgentResult to string if needed
        response_text = str(response) if not isinstance(response, str) else response

        logger.info(f"\nResponse received: {response_text[:200]}...")

        # Check if response lists agents
        agents_mentioned = sum(
            1
            for agent in all_agents
            if agent["agent_name"].lower() in response_text.lower()
        )

        if agents_mentioned > 0:
            logger.info(f"✅ Orchestrator listed {agents_mentioned} agents in response")
            logger.info(f"Full response: {response_text}")
            return True
        else:
            logger.warning("⚠️  Orchestrator didn't clearly list available agents")
            logger.info(f"Full response: {response_text}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_subagent_tool_invocation():
    """Test that subagents can invoke their tools."""
    logger.info("\n" + "=" * 60)
    logger.info("Test: Subagent Tool Invocation")
    logger.info("=" * 60)

    try:
        from idp_common.agents.analytics import get_analytics_config
        from idp_common.agents.factory import agent_factory

        # Get configuration
        config = get_analytics_config()
        session = boto3.Session()
        session_id = "test-subagent-tools"

        # Get all registered agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        # Create conversational orchestrator
        orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids, session_id=session_id, config=config, session=session
        )

        # Test a question that requires tool use
        tool_question = "Query the database to show me document processing statistics"

        logger.info(f"\nSending question: {tool_question}")
        logger.info("This should trigger Analytics Agent to use database tools...")

        response = orchestrator(tool_question)

        # Convert AgentResult to string if needed
        response_text = str(response) if not isinstance(response, str) else response

        logger.info(f"\nResponse received: {response_text[:200]}...")

        # Check if response indicates tool was used
        if any(
            keyword in response_text.lower()
            for keyword in ["query", "database", "table", "result", "data"]
        ):
            logger.info("✅ Response indicates tools were invoked")
            logger.info(f"Full response: {response_text}")
            return True
        else:
            logger.warning("⚠️  Response doesn't clearly indicate tool invocation")
            logger.info(f"Full response: {response_text}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Test orchestrator with subagents")
    parser.add_argument(
        "--test",
        choices=[
            "analytics",
            "error-analyzer",
            "code-intelligence",
            "routing",
            "tools",
            "all",
        ],
        default="all",
        help="Which test to run",
    )

    args = parser.parse_args()

    results = {}

    if args.test in ["analytics", "all"]:
        results["analytics"] = test_orchestrator_with_analytics_agent()

    if args.test in ["error-analyzer", "all"]:
        results["error_analyzer"] = test_orchestrator_with_error_analyzer_agent()

    if args.test in ["code-intelligence", "all"]:
        results["code_intelligence"] = test_orchestrator_with_code_intelligence_agent()

    if args.test in ["routing", "all"]:
        results["routing"] = test_orchestrator_routing()

    if args.test in ["tools", "all"]:
        results["tools"] = test_subagent_tool_invocation()

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("Test Summary")
    logger.info("=" * 60)

    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        logger.info(f"{test_name}: {status}")

    all_passed = all(results.values())
    logger.info(
        "\n" + ("✅ All tests passed!" if all_passed else "❌ Some tests failed")
    )

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
