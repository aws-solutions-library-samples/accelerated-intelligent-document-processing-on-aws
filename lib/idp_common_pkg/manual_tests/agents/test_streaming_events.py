#!/usr/bin/env python3
"""
Test script to observe streaming events from orchestrator and sub-agents.

This script creates an orchestrator with sub-agents and streams a query,
printing out every event to understand the event structure.

Usage:
    python manual_tests/agents/test_streaming_events.py
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import boto3

from idp_common.agents.analytics import get_analytics_config
from idp_common.agents.factory import agent_factory

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def print_event(event, indent=0):
    """Pretty print an event with indentation."""
    prefix = "  " * indent

    if isinstance(event, dict):
        for key, value in event.items():
            if isinstance(value, (dict, list)):
                print(f"{prefix}📦 {key}:")
                print_event(value, indent + 1)
            elif isinstance(value, str) and len(value) > 100:
                print(f"{prefix}📝 {key}: {value[:100]}...")
            else:
                print(f"{prefix}📝 {key}: {value}")
    elif isinstance(event, list):
        for i, item in enumerate(event):
            print(f"{prefix}[{i}]:")
            print_event(item, indent + 1)
    else:
        print(f"{prefix}{event}")


async def test_orchestrator_streaming():
    """Test orchestrator streaming and observe all events."""

    print("\n" + "=" * 80)
    print("🚀 TESTING ORCHESTRATOR STREAMING EVENTS")
    print("=" * 80 + "\n")

    # Create boto3 session
    session = boto3.Session()

    # Get configuration
    config = get_analytics_config()

    # Get available agents
    all_agents = agent_factory.list_available_agents()
    agent_ids = [agent["agent_id"] for agent in all_agents]

    print(f"📋 Available agents: {agent_ids}\n")

    # Create orchestrator with all agents
    print("🔧 Creating orchestrator...")
    orchestrator = agent_factory.create_conversational_orchestrator(
        agent_ids=agent_ids,
        session_id="test-session-123",
        config=config,
        session=session,
    )
    print("✅ Orchestrator created\n")

    # Test query that will trigger a sub-agent
    test_query = "What tables are available in the analytics database?"

    print(f"💬 Query: {test_query}\n")
    print("=" * 80)
    print("📡 STREAMING EVENTS:")
    print("=" * 80 + "\n")

    event_count = 0

    try:
        async for event in orchestrator.stream_async(test_query):
            event_count += 1

            print(f"\n{'─' * 80}")
            print(f"EVENT #{event_count}")
            print(f"{'─' * 80}")

            # Print event keys first
            if isinstance(event, dict):
                print(f"🔑 Keys: {list(event.keys())}")
                print()

            # Identify event type
            if "data" in event:
                print("📤 TYPE: Orchestrator text streaming")
                print(f"   Content: {event['data'][:100]}...")

            elif "current_tool_use" in event:
                print("🔧 TYPE: Orchestrator calling sub-agent (tool)")
                tool_use = event["current_tool_use"]
                print(f"   Tool: {tool_use.get('name')}")
                print(f"   Tool Use ID: {tool_use.get('toolUseId')}")
                if "input" in tool_use:
                    print(
                        f"   Input keys: {list(tool_use['input'].keys()) if isinstance(tool_use['input'], dict) else type(tool_use['input'])}"
                    )

            elif "tool_stream_event" in event:
                print("🛠️  TYPE: Sub-agent streaming event")
                tool_stream = event["tool_stream_event"]
                tool_data = tool_stream.get("data")

                print(f"   Data type: {type(tool_data)}")

                if isinstance(tool_data, str):
                    print(f"   String content: {tool_data[:100]}...")

                elif isinstance(tool_data, dict):
                    print(f"   Dict keys: {list(tool_data.keys())}")

                    # Check for nested events
                    if "data" in tool_data:
                        print(f"   ↳ Sub-agent text: {str(tool_data['data'])[:100]}...")

                    elif "current_tool_use" in tool_data:
                        print("   ↳ Sub-agent calling its own tool!")
                        nested_tool = tool_data["current_tool_use"]
                        print(f"      Tool: {nested_tool.get('name')}")
                        print(f"      Tool Use ID: {nested_tool.get('toolUseId')}")

                    elif "tool_stream_event" in tool_data:
                        print("   ↳ Nested tool streaming!")
                        nested_stream = tool_data["tool_stream_event"]
                        nested_data = nested_stream.get("data")
                        print(f"      Nested data type: {type(nested_data)}")
                        if isinstance(nested_data, str):
                            print(f"      Nested content: {nested_data[:100]}...")

                    elif "message" in tool_data:
                        print("   ↳ Sub-agent message event")
                        msg = tool_data["message"]
                        print(f"      Role: {msg.get('role')}")
                        if msg.get("role") == "user":
                            content = msg.get("content", [])
                            for item in content:
                                if isinstance(item, dict) and "toolResult" in item:
                                    print("      ↳ Tool result detected!")

                    elif "structured_data_detected" in tool_data:
                        print(f"   ↳ Structured data: {tool_data.get('responseType')}")

                    else:
                        print("   ↳ Other dict event")

            elif "message" in event:
                print("💬 TYPE: Message event (tool result)")
                msg = event["message"]
                print(f"   Role: {msg.get('role')}")

            elif "result" in event:
                print("🏁 TYPE: Final result")
                result = str(event["result"])
                print(f"   Result: {result[:200]}...")

            else:
                print("❓ TYPE: Unknown event")

            # Print full event structure (truncated)
            print("\n📋 Full event structure:")
            print_event(event, indent=1)

    except Exception as e:
        logger.error(f"Error during streaming: {e}", exc_info=True)

    finally:
        # Cleanup
        if hasattr(orchestrator, "__exit__"):
            orchestrator.__exit__(None, None, None)

    print(f"\n{'=' * 80}")
    print(f"✅ STREAMING COMPLETE - Total events: {event_count}")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    asyncio.run(test_orchestrator_streaming())
