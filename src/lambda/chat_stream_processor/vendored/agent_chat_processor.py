# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to process agent chat messages with streaming support.

This function creates a conversational orchestrator with all registered agents
and streams responses in real-time via AppSync subscriptions.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime

import boto3
import botocore.exceptions
from idp_common.agents.analytics import get_analytics_config
from idp_common.agents.common.config import configure_logging
from idp_common.agents.factory import agent_factory

# Import Bedrock error handling
try:
    from idp_common.agents.common.bedrock_error_messages import (
        BedrockErrorMessageHandler,
    )
    _BEDROCK_ERROR_HANDLING_AVAILABLE = True
except ImportError:
    _BEDROCK_ERROR_HANDLING_AVAILABLE = False
    BedrockErrorMessageHandler = None

# Configure logging for both application and Strands framework
configure_logging()

# Get logger for this module
logger = logging.getLogger(__name__)

# Sub-agent streaming is always enabled

_CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE")
_CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")
_DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))


def _persist_chat_turn(
    session_id, user_id, surface, prompt, assistant_text, persist_user_message=True
):
    """Persist a chat turn to the history tables.

    ``persist_user_message=False`` is used when the invoker (the agent_chat
    resolver on the non-streaming path) has ALREADY stored the user message and
    session metadata — then only the assistant reply is written here, and the
    session messageCount is bumped by 1 instead of 2.
    """
    if not session_id or not user_id:
        logger.warning(
            "No caller identity for session %s; skipping chat history write "
            "(assistant reply will NOT be visible to the polling UI path)",
            session_id,
        )
        return
    if not _CHAT_MESSAGES_TABLE or not _CHAT_SESSIONS_TABLE:
        logger.warning("Chat persistence tables not configured; skipping history write")
        return
    try:
        dynamodb = boto3.resource("dynamodb")
        messages_table = dynamodb.Table(_CHAT_MESSAGES_TABLE)
        sessions_table = dynamodb.Table(_CHAT_SESSIONS_TABLE)
        expires_after = int(time.time()) + (_DATA_RETENTION_DAYS * 24 * 60 * 60)
        user_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if persist_user_message:
            messages_table.put_item(
                Item={
                    "PK": session_id,
                    "SK": user_ts,
                    "role": "user",
                    "content": prompt,
                    "timestamp": user_ts,
                    "isProcessing": False,
                    "ExpiresAfter": expires_after,
                }
            )
        if assistant_text:
            assistant_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            messages_table.put_item(
                Item={
                    "PK": session_id,
                    "SK": assistant_ts,
                    "role": "assistant",
                    "content": assistant_text,
                    "timestamp": assistant_ts,
                    "isProcessing": False,
                    "ExpiresAfter": expires_after,
                }
            )
        last_message = prompt[:100] + "..." if len(prompt) > 100 else prompt
        # Count only the messages THIS call stored.
        stored_count = (1 if persist_user_message else 0) + (1 if assistant_text else 0)
        existing = sessions_table.get_item(
            Key={"userId": user_id, "sessionId": session_id}
        ).get("Item")
        if existing:
            if stored_count:
                sessions_table.update_item(
                    Key={"userId": user_id, "sessionId": session_id},
                    UpdateExpression=(
                        "SET updatedAt = :ts, messageCount = messageCount + :inc, "
                        "lastMessage = :last, surface = :surface"
                    ),
                    ExpressionAttributeValues={
                        ":ts": user_ts,
                        ":inc": stored_count,
                        ":last": last_message,
                        ":surface": surface,
                    },
                )
        else:
            title = prompt.strip()
            if len(title) > 50:
                title = title[:47] + "..."
            sessions_table.put_item(
                Item={
                    "userId": user_id,
                    "sessionId": session_id,
                    "title": title,
                    "createdAt": user_ts,
                    "updatedAt": user_ts,
                    # The user message exists in the session even when the
                    # resolver stored it (persist_user_message=False), so the
                    # count includes it either way on first write.
                    "messageCount": 2 if assistant_text else 1,
                    "lastMessage": last_message,
                    "surface": surface,
                    "ExpiresAfter": expires_after,
                }
            )
    except Exception as e:
        logger.error(f"Failed to persist chat turn for session {session_id}: {e}")


# Track Lambda cold/warm starts for debugging
_lambda_invocation_count = 0

# Global client cache for warm Lambda containers
# Reusing clients significantly reduces latency in warm starts
_client_cache = {}

def get_cached_boto3_session():
    """
    Get or create a cached boto3 session for warm Lambda containers.

    Returns:
        Cached boto3.Session instance
    """
    global _client_cache

    if 'session' not in _client_cache:
        _client_cache['session'] = boto3.Session()
        logger.info("Created new boto3 session (will be cached for warm starts)")
    else:
        logger.info("Reusing cached boto3 session from warm container")

    return _client_cache['session']


# --- Emission sink -------------------------------------------------------
#
# Streaming events flow through ``publish_stream_update`` to the active sink.
# This processor always runs behind the Lambda Function URL streaming endpoint
# (``chat_stream_processor``), which injects a sink via ``set_sink`` that writes
# SSE ``data: {json}`` chunks to the HTTP response stream. If no sink is
# installed, events are dropped with a warning — there is no AppSync fallback.
#
# A sink is a callable: sink(session_id, content, method, is_processing,
# tool_metadata) -> None (it may be sync; it is invoked from async code).
_active_sink = None


def set_sink(sink) -> None:
    """Install the emission sink (used by the streaming endpoint).

    Pass ``None`` to clear the installed sink.
    """
    global _active_sink
    _active_sink = sink


def clean_content_for_display(content):
    """
    Remove thinking tags from content for display.
    
    Agents may use <thinking>...</thinking> tags for internal reasoning.
    This function removes those tags so only the final response is shown to users.
    
    Args:
        content: The raw content from the agent
        
    Returns:
        Cleaned content without thinking tags
    """
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
    return cleaned.strip()


async def publish_stream_update(
    appsync_client, session_id, content, method, message_id, is_processing=True, tool_metadata=None
):
    """
    Publish a streaming update to the active emission sink.

    This processor always runs behind the Lambda Function URL streaming
    endpoint, which installs a sink via ``set_sink``. The ``appsync_client``
    parameter is retained for call-site compatibility but is unused — there is
    no AppSync fallback. If no sink is installed, the event is dropped with a
    warning.

    Args:
        appsync_client: Unused (retained for call-site compatibility).
        session_id: The conversation session ID
        content: The content to send
        method: The message method (e.g., "assistant_stream", "assistant_final_response")
        message_id: Unique ID for this message
        is_processing: Whether the agent is still processing
        tool_metadata: Optional tool metadata for tool-related messages

    Returns:
        None
    """
    try:
        # Clean and truncate content if needed
        cleaned_content = str(content).replace('\r', ' ')
        if len(cleaned_content) > 10000:
            cleaned_content = cleaned_content[:10000] + "..."

        if _active_sink is None:
            logger.warning(
                "No emission sink installed; dropping agent chat event "
                "(method=%s)",
                method,
            )
            return None

        # Route the event to the streaming sink. The sink mirrors the same
        # payload shape the UI consumes.
        _active_sink(
            session_id=str(session_id),
            content=cleaned_content,
            method=method,
            is_processing=bool(is_processing),
            tool_metadata=tool_metadata,
        )
        logger.info(f"Published message via stream sink: {method}")
        return None

    except Exception as e:
        logger.error(f"Error publishing stream update: {e}")
        return None


async def stream_agent_response(appsync_client, orchestrator, prompt, session_id):
    """
    Stream agent responses in real-time.
    
    This function processes the user's prompt through the orchestrator and
    streams the response chunks as they're generated, providing a real-time
    conversational experience.
    
    Args:
        orchestrator: The conversational orchestrator agent
        prompt: The user's message
        session_id: The conversation session ID
        
    Returns:
        The final displayed text (without thinking tags)
    """
    try:
        full_text_buffer = ""
        displayed_text = ""
        message_id = str(uuid.uuid4())
        current_subagent = None
        current_tool_use_id = None
        sent_subagent_starts = set()  # Track which tool use IDs we've already sent starts for
        sent_tool_use_ids = set()  # Track which nested tool use IDs we've already announced
        tool_input_buffers = {}  # Track input text sent for each tool use ID
        skip_content = False  # Flag to skip content containing JSON markers
        
        logger.info(f"Starting to stream response for session {session_id}")
        
        # Stream the agent's response asynchronously
        async for event in orchestrator.stream_async(prompt):
            
            # Handle force_stop events (Strands internal error handling)
            # This happens when a sub-agent encounters an error like serviceUnavailableException
            if "force_stop" in event or "force_stop_reason" in event:
                force_stop_reason = event.get("force_stop_reason", "Unknown error")
                logger.error(f"Force stop event received: {force_stop_reason}")
                
                # Check if this is a Bedrock-related error
                error_str = str(force_stop_reason)
                is_bedrock_error = any(err in error_str for err in [
                    "serviceUnavailableException",
                    "ServiceUnavailableException", 
                    "ThrottlingException",
                    "throttlingException",
                    "ModelErrorException",
                    "EventStreamError",
                    "Bedrock is unable to process"
                ])
                
                if is_bedrock_error and _BEDROCK_ERROR_HANDLING_AVAILABLE and BedrockErrorMessageHandler:
                    # Create a mock exception to pass to the error handler
                    try:
                        error_info = BedrockErrorMessageHandler.format_error_for_frontend(
                            Exception(error_str)
                        )
                        error_content = json.dumps({
                            "type": "bedrock_error",
                            "errorInfo": error_info
                        })
                    except Exception as format_error:
                        logger.warning(f"Failed to format force_stop error: {format_error}")
                        error_content = json.dumps({
                            "type": "bedrock_error",
                            "errorInfo": {
                                "errorType": "service_unavailable",
                                "message": "The AI service is temporarily unavailable. Please try again in a moment.",
                                "technicalDetails": error_str,
                                "retryRecommended": True,
                                "retryDelaySeconds": 30,
                                "actionRecommendations": [
                                    "Wait a moment and try your request again",
                                    "The service may be experiencing high demand"
                                ],
                                "isTransient": True,
                                "retryAttempts": 0
                            }
                        })
                else:
                    error_content = json.dumps({
                        "type": "bedrock_error",
                        "errorInfo": {
                            "errorType": "agent_error",
                            "message": "An error occurred while processing your request.",
                            "technicalDetails": error_str,
                            "retryRecommended": True,
                            "retryDelaySeconds": 10,
                            "actionRecommendations": [
                                "Try your request again",
                                "If the problem persists, try a simpler query"
                            ],
                            "isTransient": True,
                            "retryAttempts": 0
                        }
                    })
                
                # Publish the error to the frontend
                try:
                    await publish_stream_update(
                        appsync_client,
                        session_id,
                        error_content,
                        "assistant_error",
                        message_id,
                        False
                    )
                    logger.info("Published force_stop error to frontend")
                except Exception as publish_error:
                    logger.error(f"Failed to publish force_stop error: {publish_error}")
                
                # Continue processing - the orchestrator may recover or provide a graceful response
                continue
            
            if "data" in event:
                # Handle streaming chunk
                chunk_text = event["data"]
                full_text_buffer += chunk_text
                
                # Clean the content (remove thinking tags)
                clean_text = clean_content_for_display(full_text_buffer)
            
                # Only send new text that hasn't been displayed yet
                if len(clean_text) > len(displayed_text):
                    new_text = clean_text[len(displayed_text):]
                    displayed_text = clean_text
                    
                    # Publish the chunk to AppSync
                    await publish_stream_update(
                        appsync_client,
                        session_id, 
                        new_text, 
                        "assistant_stream", 
                        message_id, 
                        True
                    )
            
            # Handle sub-agent start (tool use detected)
            elif "current_tool_use" in event:
                tool_use = event["current_tool_use"]
                tool_name = tool_use.get("name")
                
                if tool_name:
                    current_subagent = tool_name
                    current_tool_use_id = tool_use.get("toolUseId")
                    skip_content = False  # Reset skip flag for new agent message
                    
                    # Check if we've already sent a start message for this tool use ID
                    if current_tool_use_id not in sent_subagent_starts:
                        # Mark this tool use ID as having a start message sent
                        sent_subagent_starts.add(current_tool_use_id)
                        
                        # Extract display name
                        agent_display_name = tool_name.replace("_agent", "").replace("_", " ").title()

                        # Publish sub-agent start event
                        try:
                            await publish_stream_update(
                                appsync_client,
                                session_id,
                                json.dumps({
                                    "type": "subagent_start",
                                    "agent_name": agent_display_name,
                                    "tool_name": tool_name,
                                    "tool_use_id": current_tool_use_id
                                }),
                                "subagent_start",
                                message_id,
                                True
                            )
                            logger.info(f"Sub-agent started: {agent_display_name} ({tool_name})")
                        except Exception as e:
                            logger.error(f"Failed to publish subagent_start event: {e}")
                            # Continue processing even if publish fails
                    else:
                        logger.debug(f"Skipping duplicate subagent_start for tool use ID: {current_tool_use_id}")
            
            # Handle sub-agent streaming output
            elif "tool_stream_event" in event:
                tool_stream = event["tool_stream_event"]
                tool_data = tool_stream.get("data")
                
                # Skip low-level Bedrock events
                if isinstance(tool_data, dict) and any(key in tool_data for key in ["init_event_loop", "start_event_loop", "start", "event"]):
                    logger.debug(f"Skipping low-level event: {list(tool_data.keys())}")
                    continue
                
                # Log what we received for debugging
                logger.debug(f"Received tool_stream_event: data type={type(tool_data)}, data={str(tool_data)[:200]}")
                
                # Check if this is a Bedrock error event from sub-agent
                if isinstance(tool_data, dict) and tool_data.get("bedrock_error"):
                    error_message = tool_data.get("error_message", "Unknown error")
                    agent_id = tool_data.get("agent_id", "unknown")
                    logger.error(f"Bedrock error from sub-agent {agent_id}: {error_message}")
                    
                    # Format and publish the error to frontend
                    if _BEDROCK_ERROR_HANDLING_AVAILABLE and BedrockErrorMessageHandler:
                        try:
                            error_info = BedrockErrorMessageHandler.format_error_for_frontend(
                                Exception(error_message)
                            )
                            error_content = json.dumps({
                                "type": "bedrock_error",
                                "errorInfo": error_info
                            })
                        except Exception as format_error:
                            logger.warning(f"Failed to format sub-agent Bedrock error: {format_error}")
                            error_content = json.dumps({
                                "type": "bedrock_error",
                                "errorInfo": {
                                    "errorType": "service_unavailable",
                                    "message": "The AI service is temporarily unavailable. Please try again in a moment.",
                                    "technicalDetails": error_message,
                                    "actionRecommendations": [
                                        "Wait a moment and try your request again",
                                        "The service may be experiencing high demand"
                                    ],
                                    "retryAttempts": 0
                                }
                            })
                    else:
                        error_content = json.dumps({
                            "type": "bedrock_error",
                            "errorInfo": {
                                "errorType": "service_unavailable",
                                "message": "The AI service is temporarily unavailable. Please try again in a moment.",
                                "technicalDetails": error_message,
                                "actionRecommendations": [
                                    "Wait a moment and try your request again",
                                    "The service may be experiencing high demand"
                                ],
                                "retryAttempts": 0
                            }
                        })
                    
                    # Publish the error to the frontend
                    try:
                        await publish_stream_update(
                            appsync_client,
                            session_id,
                            error_content,
                            "assistant_error",
                            message_id,
                            False
                        )
                        logger.info("Published sub-agent Bedrock error to frontend")
                    except Exception as publish_error:
                        logger.error(f"Failed to publish sub-agent Bedrock error: {publish_error}")
                    
                    # Continue processing - the orchestrator will handle the error gracefully
                    continue
                
                # Check if this is a structured data detection event (special case - not streamed to FE)
                elif isinstance(tool_data, dict) and "structured_data_detected" in tool_data:
                    response_type = tool_data.get("responseType")
                    logger.info(f"Detected structured data response from tool stream: {response_type}")
                    
                    # Notify FE that structured data is coming
                    try:
                        await publish_stream_update(
                            appsync_client,
                            session_id,
                            json.dumps({
                                "type": "structured_data_start",
                                "responseType": response_type
                            }),
                            "structured_data_start",
                            message_id,
                            True
                        )
                        logger.info(f"Notified FE of {response_type} response")
                    except Exception as e:
                        logger.error(f"Failed to publish structured_data_start: {e}")

                
                # Handle string data (direct text streaming from sub-agent)
                elif isinstance(tool_data, str):
                    logger.debug(f"Sub-agent text stream (string): {tool_data[:100]}")
                    try:
                        await publish_stream_update(
                            appsync_client,
                            session_id,
                            tool_data,
                            "subagent_stream",
                            message_id,
                            True
                        )
                    except Exception as e:
                        logger.error(f"Failed to publish subagent_stream text: {e}")
                
                # Handle dict events from sub-agent
                elif isinstance(tool_data, dict):
                    content = None
                    
                    # Text streaming wrapped in dict
                    if "data" in tool_data:
                        content = tool_data["data"]
                        logger.debug(f"Sub-agent text stream (dict): {str(content)[:100]}")
                    
                    # Sub-agent calling its own tool
                    elif "current_tool_use" in tool_data:
                        tool_use = tool_data["current_tool_use"]
                        tool_name = tool_use.get("name", "unknown")
                        tool_use_id = tool_use.get("toolUseId")
                        tool_input_raw = tool_use.get("input", "")
                        
                        # Convert input to string for buffering
                        current_input = str(tool_input_raw) if tool_input_raw else ""
                        
                        # Check if this is the first time seeing this tool use
                        if tool_use_id and tool_use_id not in sent_tool_use_ids:
                            # First time - send tool execution start message (no streaming)
                            sent_tool_use_ids.add(tool_use_id)
                            tool_input_buffers[tool_use_id] = ""  # Initialize buffer
                            logger.info(f"Sub-agent {current_subagent} calling tool: {tool_name} (ID: {tool_use_id})")
                            
                            # Send tool execution start message for modal display
                            try:
                                await publish_stream_update(
                                    appsync_client,
                                    session_id,
                                    "tool_execution_start",
                                    "tool_execution_start",
                                    message_id,
                                    True,
                                    tool_metadata={
                                        "toolName": tool_name,
                                        "toolUseId": tool_use_id
                                    }
                                )
                                logger.info(f"Sent tool_execution_start for {tool_name} (ID: {tool_use_id})")
                                content = None  # Don't stream the header
                            except Exception as e:
                                logger.error(f"Failed to publish tool_execution_start: {e}")
                                # Fallback to original streaming behavior
                                content = f"\n\n**Using tool: {tool_name}**\n\n"
                        else:
                            # Subsequent updates - buffer execution details instead of streaming
                            previous_input = tool_input_buffers.get(tool_use_id, "")
                            
                            if len(current_input) > len(previous_input):
                                # New characters added - buffer instead of streaming
                                new_chars = current_input[len(previous_input):]
                                tool_input_buffers[tool_use_id] = current_input
                                logger.debug(f"Tool input delta buffered: {new_chars[:50]}")
                                content = None  # Don't stream execution details
                            else:
                                # No new characters
                                content = None
                    
                    # Sub-agent tool result
                    elif "message" in tool_data:
                        msg = tool_data["message"]
                        msg_role = msg.get("role")
                        
                        # Check if this is a tool result message
                        if msg_role == "user":
                            msg_content = msg.get("content", [])
                            for content_item in msg_content:
                                if isinstance(content_item, dict) and "toolResult" in content_item:
                                    tool_result = content_item["toolResult"]
                                    tool_use_id = tool_result.get("toolUseId")
                                    result_content = tool_result.get("content", [])
                                    
                                    # Try to extract meaningful result text
                                    result_text = ""
                                    for result_item in result_content:
                                        if isinstance(result_item, dict) and "text" in result_item:
                                            result_text = result_item["text"]
                                            break
                                    
                                    # Send tool execution complete with buffered execution details
                                    if tool_use_id and tool_use_id in tool_input_buffers:
                                        try:
                                            execution_content = tool_input_buffers[tool_use_id]
                                            await publish_stream_update(
                                                appsync_client,
                                                session_id,
                                                execution_content,
                                                "tool_execution_complete",
                                                message_id,
                                                True,
                                                tool_metadata={
                                                    "toolUseId": tool_use_id
                                                }
                                            )
                                            logger.info(f"Sent tool_execution_complete for {tool_use_id}")
                                        except Exception as e:
                                            logger.error(f"Failed to publish tool_execution_complete: {e}")
                                    
                                    # Send tool result start
                                    try:
                                        await publish_stream_update(
                                            appsync_client,
                                            session_id,
                                            "",
                                            "tool_result_start",
                                            message_id,
                                            True,
                                            tool_metadata={
                                                "toolUseId": tool_use_id
                                            }
                                        )
                                        logger.info(f"Sent tool_result_start for {tool_use_id}")
                                    except Exception as e:
                                        logger.error(f"Failed to publish tool_result_start: {e}")
                                    
                                    # Send tool result complete with result content
                                    try:
                                        result_content_to_send = result_text if result_text else "Tool completed"
                                        await publish_stream_update(
                                            appsync_client,
                                            session_id,
                                            result_content_to_send,
                                            "tool_result_complete",
                                            message_id,
                                            True,
                                            tool_metadata={
                                                "toolUseId": tool_use_id
                                            }
                                        )
                                        logger.info(f"Sent tool_result_complete for {tool_use_id}")
                                    except Exception as e:
                                        logger.error(f"Failed to publish tool_result_complete: {e}")
                                    
                                    content = None  # Don't stream result content since we sent it via tool lifecycle
                                    break
                        
                        if not content:
                            logger.debug(f"Sub-agent message event (skipping): role={msg_role}")
                    
                    else:
                        # Other dict events - log but don't send
                        logger.debug(f"Sub-agent other event (skipping): {list(tool_data.keys())}")
                    
                    # Only publish if we have content to send
                    if content:
                        # Check if content contains JSON marker and skip if so
                        logger.info(f"content {content}")
                        
                        if "{" in content:
                            skip_content = True
                            logger.info(f"skip_content {skip_content}")
                        elif not skip_content:
                            logger.info(f"not skippedcontent {content}")
                            try:
                                await publish_stream_update(
                                    appsync_client,
                                    session_id,
                                    content,
                                    "subagent_stream",
                                    message_id,
                                    True
                                )
                            except Exception as e:
                                logger.error(f"Failed to publish subagent_stream event: {e}")
                
                else:
                    logger.debug(f"tool_stream_event data doesn't match expected format: {tool_data}")
            
            # Handle sub-agent completion (tool result message)
            elif "message" in event:
                msg = event["message"]
                
                # Tool results come as user messages with toolResult content
                if msg.get("role") == "user":
                    for content in msg.get("content", []):
                        if "toolResult" in content:
                            tool_result = content["toolResult"]
                            
                            # Check if this is the current sub-agent completing
                            if tool_result.get("toolUseId") == current_tool_use_id:
                                # Publish sub-agent end event
                                try:
                                    await publish_stream_update(
                                        appsync_client,
                                        session_id,
                                        json.dumps({
                                            "type": "subagent_end",
                                            "agent_name": current_subagent,
                                            "tool_use_id": current_tool_use_id
                                        }),
                                        "subagent_end",
                                        message_id,
                                        True
                                    )
                                    logger.info(f"Sub-agent completed: {current_subagent}")
                                except Exception as e:
                                    logger.error(f"Failed to publish subagent_end event: {e}")
                                
                                # Reset current sub-agent
                                current_subagent = None
                                current_tool_use_id = None
            
            elif "result" in event:
                # Handle final response
                
                if len(displayed_text) < len(full_text_buffer):
                    displayed_text = clean_content_for_display(full_text_buffer)
                
                # Publish the final response
                await publish_stream_update(
                    appsync_client,
                    session_id, 
                    displayed_text, 
                    "assistant_final_response", 
                    message_id, 
                    False
                )
                
                logger.info(f"Completed streaming for session {session_id}")
                break
        
        return displayed_text
        
    except (botocore.exceptions.ClientError, botocore.exceptions.EventStreamError) as e:
        # Handle Bedrock-specific errors (raised after boto3 Config retries are exhausted)
        logger.error(f"Bedrock error in stream_agent_response: {e}")
        
        # Get user-friendly error information using BedrockErrorMessageHandler
        if _BEDROCK_ERROR_HANDLING_AVAILABLE and BedrockErrorMessageHandler:
            try:
                error_info = BedrockErrorMessageHandler.format_error_for_frontend(e)
                error_content = json.dumps({
                    "type": "bedrock_error",
                    "errorInfo": error_info
                })
            except Exception as format_error:
                logger.warning(f"Failed to format Bedrock error: {format_error}")
                error_content = json.dumps({
                    "type": "bedrock_error",
                    "errorInfo": {
                        "errorType": "service_error",
                        "message": "The AI service encountered an error. Please try again.",
                        "technicalDetails": str(e),
                        "retryRecommended": True,
                        "retryDelaySeconds": 30,
                        "actionRecommendations": [
                            "Wait a moment and try your request again",
                            "Check if the issue persists after a few minutes"
                        ],
                        "isTransient": True,
                        "retryAttempts": 0
                    }
                })
        else:
            error_content = f"Service temporarily unavailable: {str(e)}"
        
        # Publish structured error message
        try:
            await publish_stream_update(
                appsync_client,
                session_id, 
                error_content, 
                "assistant_error", 
                message_id, 
                False
            )
        except Exception as publish_error:
            logger.error(f"Failed to publish Bedrock error message: {publish_error}")
        raise
        
    except Exception as e:
        logger.error(f"Error in stream_agent_response: {e}")
        
        # Check if this is a Bedrock-related error we can handle
        if _BEDROCK_ERROR_HANDLING_AVAILABLE and BedrockErrorMessageHandler:
            try:
                # Try to format as Bedrock error (handles various error types)
                error_info = BedrockErrorMessageHandler.format_error_for_frontend(e)
                error_content = json.dumps({
                    "type": "bedrock_error", 
                    "errorInfo": error_info
                })
            except Exception:
                # Fall back to generic error
                error_content = f"Error: {str(e)}"
        else:
            error_content = f"Error: {str(e)}"
        
        # Publish error message
        try:
            await publish_stream_update(
                appsync_client,
                session_id, 
                error_content, 
                "assistant_error", 
                message_id, 
                False
            )
        except Exception as publish_error:
            logger.error(f"Failed to publish error message: {publish_error}")
        raise


def handler(event, context):
    """
    Process agent chat messages with streaming.
    
    This handler:
    1. Gets ALL registered agents automatically
    2. Creates a conversational orchestrator with memory
    3. Streams the response in real-time via AppSync
    
    Args:
        event: The event dict containing:
            - sessionId: The conversation session ID
            - prompt: The user's message
            - method: The message method (default: "chat")
            - timestamp: The message timestamp
        context: The Lambda context
        
    Returns:
        Success/error status
    """
    global _lambda_invocation_count
    _lambda_invocation_count += 1
    is_cold_start = _lambda_invocation_count == 1
    
    logger.info(f"Lambda invocation #{_lambda_invocation_count} ({'COLD START' if is_cold_start else 'WARM'})")
    logger.info(f"Received agent chat processor event: {json.dumps(event)}")
    
    try:
        # Use cached boto3 session for warm Lambda containers (significant performance improvement)
        session = get_cached_boto3_session()
        
        # Extract parameters from event
        prompt = event.get("prompt", "")
        session_id = event.get("sessionId")
        # Default off: the Code Intelligence Agent sends repository context
        # to a third-party MCP service, so callers must opt in explicitly.
        # `is True` so a non-boolean in the invoke payload cannot opt in.
        enable_code_intelligence = event.get("enableCodeIntelligence") is True
        method = event.get("method", "chat")
        caller_sub = event.get("callerSub", "")
        # False when invoked by the agent_chat resolver (non-streaming path),
        # which already stored the user message + session metadata itself.
        persist_user_message = event.get("persistUserMessage", True)

        # Validate required parameters
        if not prompt or not session_id:
            error_msg = "prompt and sessionId are required"
            logger.error(error_msg)
            raise Exception(error_msg)
        
        # Get analytics configuration (used by agents)
        config = get_analytics_config()
        logger.info("Configuration loaded successfully")
        
        # Get ALL registered agents automatically
        # No user selection needed - orchestrator has access to all agents
        all_agents = agent_factory.list_available_agents()
        agent_ids = [agent["agent_id"] for agent in all_agents]

        QUICK_START_AGENT_ID = "Quick-Start-Agent"
        use_direct_quick_start = (
            method == "quick_start" and QUICK_START_AGENT_ID in agent_ids
        )

        if method == "quick_start" and not use_direct_quick_start:
            logger.warning(
                "Quick Start mode requested but Quick-Start-Agent not registered; "
                "falling back to full agent list"
            )
        elif method != "quick_start":
            if QUICK_START_AGENT_ID in agent_ids:
                agent_ids.remove(QUICK_START_AGENT_ID)
            # Filter out Code Intelligence Agent if not enabled by user
            CODE_INTELLIGENCE_AGENT_ID = "Code-Intelligence-Agent"
            if not enable_code_intelligence and CODE_INTELLIGENCE_AGENT_ID in agent_ids:
                agent_ids.remove(CODE_INTELLIGENCE_AGENT_ID)
                logger.info("Code Intelligence Agent disabled by user, excluding from orchestrator")

        if use_direct_quick_start:
            logger.info("Quick Start mode: running Quick-Start-Agent directly with memory")
            orchestrator = agent_factory.create_conversational_agent(
                agent_id=QUICK_START_AGENT_ID,
                session_id=session_id,
                config=config,
                session=session,
            )
            logger.info(f"Conversational Quick Start agent created for session {session_id}")
        else:
            logger.info(f"Creating orchestrator with {len(agent_ids)} agents: {agent_ids}")
            # Create conversational orchestrator with memory and streaming support
            orchestrator = agent_factory.create_conversational_orchestrator(
                agent_ids=agent_ids,
                session_id=session_id,
                config=config,
                session=session
            )
            logger.info(f"Conversational orchestrator created for session {session_id}")
        
        # Set up async event loop for streaming
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Events are delivered through the streaming sink installed via
            # set_sink (Function URL transport); no transport client is needed.
            if _active_sink is None:
                logger.warning(
                    "No emission sink installed; agent chat events will be dropped"
                )

            # Stream the agent response
            final_text = loop.run_until_complete(
                stream_agent_response(None, orchestrator, prompt, session_id)
            )
            logger.info(f"Streaming completed successfully for session {session_id}")
            surface = "quick_start" if method == "quick_start" else "chat"
            _persist_chat_turn(
                session_id,
                caller_sub,
                surface,
                prompt,
                final_text,
                persist_user_message=persist_user_message,
            )
        finally:
            # Clean up orchestrator resources (MCP clients, etc.)
            try:
                if hasattr(orchestrator, '__exit__'):
                    orchestrator.__exit__(None, None, None)
                elif hasattr(orchestrator, 'close'):
                    orchestrator.close()
                logger.info("Orchestrator resources cleaned up")
            except Exception as e:
                logger.warning(f"Error cleaning up orchestrator: {e}")
            
            # Cancel all pending tasks before closing the loop
            # This is CRITICAL to prevent task leakage between warm Lambda invocations
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    logger.info(f"Cancelling {len(pending)} pending async tasks")
                    for task in pending:
                        task.cancel()
                    
                    # Give tasks time to handle cancellation gracefully
                    # Use gather with return_exceptions to catch CancelledError
                    try:
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=5.0  # 5 second timeout for cleanup
                            )
                        )
                        logger.info("All pending tasks cancelled gracefully")
                    except asyncio.TimeoutError:
                        logger.warning("Some tasks did not cancel within timeout, forcing cleanup")
            except Exception as e:
                logger.warning(f"Error cancelling pending tasks: {e}")
            
            # Shutdown async generators to prevent "was destroyed but it is pending" errors
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                logger.debug("Async generators shut down")
            except Exception as e:
                logger.warning(f"Error shutting down async generators: {e}")
            
            # Now close the loop
            loop.close()
            logger.info("Event loop closed and all resources cleaned up")
        
        return {
            "statusCode": 200,
            "body": "Streaming completed successfully"
        }
        
    except Exception as e:
        logger.error(f"Error in agent chat processor: {str(e)}")
        
        # Try to publish error message to frontend
        try:
            session_id = event.get("sessionId")
            if session_id:
                # Publish the error through the streaming sink (if installed).
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        publish_stream_update(
                            None,
                            session_id,
                            f"Error: {str(e)}", 
                            "assistant_error", 
                            str(uuid.uuid4()), 
                            False
                        )
                    )
                finally:
                    loop.close()
        except Exception as publish_error:
            logger.error(f"Error publishing error message: {publish_error}")
        
        return {
            "statusCode": 500,
            "body": str(e)
        }
