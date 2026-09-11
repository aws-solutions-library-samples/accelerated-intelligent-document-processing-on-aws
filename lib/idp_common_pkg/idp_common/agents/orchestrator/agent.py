# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Orchestrator Agent implementation using Strands framework.

This agent coordinates multiple specialized agents, routing queries to the most
appropriate agent based on the user's request and the capabilities of each agent.
"""

import logging
import os
from typing import Any, AsyncIterator, Dict, List

import boto3
import strands
from strands import tool

from ..common.cost_metrics import with_cost_hook
from ..common.strands_bedrock_model import create_strands_bedrock_model
from .config import get_chat_companion_model_id

logger = logging.getLogger(__name__)


def create_orchestrator_agent(
    config: Dict[str, Any],
    session: Any,
    agent_ids: List[str],
    **kwargs,
) -> strands.Agent:
    """
    Create and configure the orchestrator agent with specialized agents as tools.

    Args:
        config: Configuration dictionary
        session: Boto3 session for AWS operations
        agent_ids: List of agent IDs to include as tools in the orchestrator
        **kwargs: Additional arguments

    Returns:
        strands.Agent: Configured orchestrator agent instance
    """

    # Create tool functions for each specialized agent
    tools = []

    logger.info("Creating orchestrator with sub-agent streaming enabled")

    for agent_id in agent_ids:
        # Create a unique function name based on the agent ID
        func_name = f"{agent_id.replace('-', '_')}_agent"

        def create_tool_function(aid, fname):
            # Async streaming version - always enabled
            async def tool_func(query: str) -> AsyncIterator:
                """Route query to specialized agent with streaming."""
                import asyncio

                try:
                    from ..factory import agent_factory

                    # Create fresh boto3 session for each sub-agent to isolate connection pools
                    # This prevents connection pool conflicts between concurrent sub-agents
                    sub_session = boto3.Session()

                    # Strip `hooks` and `session_id` — subagents don't
                    # inherit the orchestrator's hooks. The motivating case
                    # is the `DynamoDBMemoryHookProvider`: if forwarded, every
                    # subagent Bedrock invocation would write its internal tool
                    # chatter to the SAME session_id row that persists the
                    # top-level user↔assistant turns, contaminating the
                    # conversation history the orchestrator loads next turn.
                    # This filter is intentionally BROAD (drops any hook the
                    # caller passed) rather than narrow (only drop memory hooks)
                    # because (a) the ControlPlaneCostHook that subagents want
                    # lives in their own creator functions, not caller kwargs, and (b)
                    # inspecting hook types to decide would couple the
                    # orchestrator to specific hook classes. If a future caller
                    # legitimately wants a hook on both orchestrator AND
                    # subagents, it must register it in both places explicitly.
                    # Subagent tool results still flow into the orchestrator's
                    # message list as `tool_result` content blocks — that's the
                    # intended memory path across the boundary.
                    sub_kwargs = {
                        k: v
                        for k, v in kwargs.items()
                        if k not in ("session_id", "hooks")
                    }
                    specialized_agent = agent_factory.create_agent(
                        agent_id=aid,
                        config=config,
                        session=sub_session,  # Use fresh session per sub-agent
                        **sub_kwargs,
                    )

                    # Stream sub-agent events with timeout per event
                    timeout_seconds = int(
                        os.environ.get("SUBAGENT_TIMEOUT_SECONDS", "120")
                    )
                    logger.info(
                        f"Starting sub-agent {aid} with {timeout_seconds}s timeout"
                    )

                    with specialized_agent:
                        stream = specialized_agent.stream_async(query)
                        last_event_time = asyncio.get_event_loop().time()

                        try:
                            async for event in stream:
                                # Check if we've exceeded timeout since last event
                                current_time = asyncio.get_event_loop().time()
                                if current_time - last_event_time > timeout_seconds:
                                    error_msg = f"Sub-agent {aid} timed out (no events for {timeout_seconds}s)"
                                    logger.error(error_msg)
                                    yield f"Error: {error_msg}"
                                    break

                                last_event_time = current_time

                                # Log what events we're receiving from sub-agent
                                logger.info(
                                    f"Sub-agent {aid} event: {list(event.keys())}"
                                )

                                # Yield each event for the orchestrator to process
                                yield event

                                # When we get the final result, yield it as the tool return
                                if "result" in event:
                                    result = event["result"]
                                    response_text = str(result)

                                    # Extract JSON from markdown code blocks if present
                                    from ..common.response_utils import (
                                        extract_json_from_markdown,
                                    )

                                    cleaned_response = extract_json_from_markdown(
                                        response_text
                                    )

                                    # Check if this is structured data (table/plot)
                                    try:
                                        import json

                                        parsed = json.loads(cleaned_response)
                                        response_type = parsed.get("responseType")

                                        if response_type in ["table", "plotData"]:
                                            # Yield a special event to signal structured data
                                            yield {
                                                "structured_data_detected": True,
                                                "responseType": response_type,
                                            }
                                            logger.info(
                                                f"Sub-agent {aid} returning {response_type} data"
                                            )
                                    except (
                                        json.JSONDecodeError,
                                        AttributeError,
                                        TypeError,
                                    ):
                                        # Not JSON or doesn't have responseType - continue normally
                                        pass

                                    logger.info(
                                        f"Sub-agent {aid} completed, yielding final result"
                                    )
                                    yield cleaned_response
                                    break
                        except asyncio.TimeoutError:
                            error_msg = f"Sub-agent {aid} timed out after {timeout_seconds} seconds"
                            logger.error(error_msg)
                            yield f"Error: {error_msg}. The agent took too long to respond."

                except Exception as e:
                    logger.error(f"Error in sub-agent {aid}: {e}", exc_info=True)

                    # Check if this is a Bedrock-related error
                    error_str = str(e)
                    is_bedrock_error = any(
                        pattern in error_str
                        for pattern in [
                            "serviceUnavailableException",
                            "ServiceUnavailableException",
                            "ThrottlingException",
                            "throttlingException",
                            "ModelErrorException",
                            "TooManyRequestsException",
                            "EventStreamError",
                            "Bedrock is unable to process",
                        ]
                    )

                    if is_bedrock_error:
                        # Yield a structured error event that agent_chat_processor can detect
                        yield {
                            "bedrock_error": True,
                            "error_message": error_str,
                            "agent_id": aid,
                        }

                    # Also yield the text error for the orchestrator to handle
                    yield f"Error processing query with {aid}: {error_str}"

            # Rename the function to match the desired tool name
            tool_func.__name__ = fname
            tool_func.__qualname__ = fname
            # Store the original agent_id as a custom attribute
            tool_func._original_agent_id = aid
            # Apply tool decorator and return
            return tool(tool_func)

        # Create the tool function for this specific agent
        tool_func = create_tool_function(agent_id, func_name)
        tools.append(tool_func)

    # Build agent descriptions for system prompt
    agent_descriptions = []
    for tool_func in tools:
        # Get the original agent_id from the function's custom attribute
        agent_id = getattr(tool_func, "_original_agent_id", None)
        if not agent_id:
            raise ValueError(
                f"Tool function {tool_func.__name__} missing _original_agent_id attribute"
            )

        from ..factory import agent_factory

        info = agent_factory._registry[agent_id]
        agent_name = info["agent_name"]
        agent_description = info["agent_description"]
        sample_queries = info["sample_queries"]

        sample_queries_text = ""
        if sample_queries:
            sample_queries_text = f"\nExample queries: {', '.join(sample_queries)}"

        agent_descriptions.append(
            f"- {agent_name}: {agent_description}{sample_queries_text}"
        )

    # Create system prompt with agent descriptions
    agent_names_description_sample_queries = "\n".join(agent_descriptions)

    system_prompt = f"""You are IDP Companion, an intelligent AI Assistant that can answer question about the IDP app. 
    Specifically, as the main agent or the orchestrator, you cooridnate and leverage specialized agents to answer user queries.

# Available Specialized Agents
{agent_names_description_sample_queries}

# Your Workflow
1. **Analyze** the user's query to understand what information is needed
2. **Select** the most appropriate specialized agent(s) based on capabilities and sample queries
3. **Call** the agent tool(s) to gather information - you may call multiple agents if needed
4. **Evaluate** if the response fully answers the user's question
5. **Stop** if you have sufficient information - do NOT make redundant calls
6. **Process** the responses and format your final answer appropriately

# Agent Response Format
Specialized agents return JSON responses in this structure:
```json
{{
  "responseType": "text" | "table" | "plotData",
  "content": "...",        // For text responses
  "tableData": {{...}},    // For table responses  
  "plotData": {{...}}      // For plot responses
}}
```

# How to Format Your Final Response

## Text Responses (responseType: "text")
When an agent returns text:
- Extract and understand the `content` field
- **Evaluate if the response fully answers the user's question**
- If the response is complete and satisfactory, **STOP and respond immediately**
- Only call additional agents if the first response is incomplete or missing critical information
- Once you have sufficient information, respond in **natural conversational text** (NOT JSON)
- If a single agent fully answers the question, you can pass through its answer directly
- For multi-agent responses, synthesize the information into a coherent answer
- **NEVER call the same agent twice with the same or similar query**

Example:
- Agent returns: `{{"responseType": "text", "content": "The repository contains 5 Python files..."}}`
- You respond: "Based on the analysis, this repository contains 5 Python files..."

## Structured Data Responses (responseType: "table" or "plotData")
When an agent returns structured data:
- **Return ONLY the raw JSON exactly as received**
- Do NOT add any text, explanations, or formatting
- Do NOT modify the JSON structure
- The UI will handle rendering

Example:
- Agent returns: `{{"responseType": "table", "tableData": {{...}}}}`
- You respond: `{{"responseType": "table", "tableData": {{...}}}}`

# Agent Selection Guidelines
- Match the query to the agent whose description and sample queries are most relevant
- Prefer more specialized agents over general ones when applicable
- You can call multiple agents sequentially to gather all needed information
- Pass clear, specific queries to each agent
- **IMPORTANT**: If a user query requires understanding the codebase or code structure, check if the Code Intelligence Agent is available in your tools. If it is NOT available, politely inform the user that you cannot answer code-related questions because the Code Intelligence Agent has been disabled for this session.

# CRITICAL: Handling Unavailable Agents
- **Code Intelligence Agent**: If the user asks about code, codebase structure, implementation details, or repository information, check if the Code Intelligence Agent is in your available tools
- If the Code Intelligence Agent is NOT available and the query requires code understanding, respond with:
  "I'm unable to answer questions about the codebase because the Code Intelligence Agent is currently disabled. You can enable it using the checkbox below the chat box to ask codebase-related questions."
- Do NOT attempt to answer code-related questions without the Code Intelligence Agent
- Do NOT make up information about the codebase

# CRITICAL: When to Stop Calling Tools
- **Stop immediately** if a single agent call fully answers the user's question
- **Do NOT** make redundant calls to the same agent with similar queries
- **Do NOT** call additional agents "just to be thorough" if you already have sufficient information
- Only call multiple agents if the first response is incomplete or you need different types of information
- Trust the agent responses - they are designed to be comprehensive

# Scope (you are the "Agent Companion Chat")
- You handle general Q&A about the user's documents, analytics, errors, and the codebase. Do NOT describe schema authoring, config-version creation, or synthetic-data generation as your own capabilities.
- If the user wants to SET UP or CONFIGURE the system - create/bootstrap a configuration, author a document schema from a description or example - tell them briefly that this is handled by the separate "Quick Start" assistant, which they can open with the "Quick Start" button at the top of the left navigation. Do not attempt to author schemas or create config versions yourself.

# Key Rules
- For text responses: Be conversational and helpful - return natural language, NOT JSON
- For table/plot responses: Return ONLY the JSON with zero additional text
- Synthesize information from multiple agents when needed
- Keep responses clear and user-friendly
- If a subagent or several subagents result in error after 2 times of retry, reply gracefully by mentioning the error that has occurred and STOP retrying the agents.

"""

    # Get model ID using modern configuration system (reads user-changed values from DynamoDB)
    try:
        model_id = get_chat_companion_model_id()
    except Exception as e:
        logger.warning(f"Failed to get chat companion model ID, using default: {e}")
        model_id = config.get(
            "default_model_id", "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
        )

    # Create the orchestrator agent
    model = create_strands_bedrock_model(
        model_id=model_id,
        boto_session=session,
    )

    # Append control-plane cost hook to caller-supplied hooks so the
    # orchestrator's own routing/decision Bedrock calls land in
    # control_plane_hourly under component="chat-orchestrator".
    orchestrator = strands.Agent(
        system_prompt=system_prompt,
        model=model,
        tools=tools,
        hooks=with_cost_hook(kwargs.get("hooks"), "chat-orchestrator", model_id),
        callback_handler=None,
    )

    return orchestrator
