# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to handle agent chat message requests.
This function stores messages in DynamoDB and invokes the agent chat processor Lambda
for conversational, multi-turn interactions.
"""

import json
import logging
import os
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

# Get environment variables
CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE")
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")
AGENT_CHAT_PROCESSOR_FUNCTION = os.environ.get("AGENT_CHAT_PROCESSOR_FUNCTION")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

# Agent Chat is available to Admin/Author/Viewer; Reviewer is excluded.
_AGENT_CHAT_GROUPS = ("Admin", "Author", "Viewer")


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The single REST route's Cognito authorizer only *authenticates* — it does
    not enforce the group. So we enforce it server-side here (matching the
    pattern in calculate_capacity_resolver) so a Reviewer calling
    /op/sendAgentChatMessage directly is rejected (closes GAP-03).

    A Cognito invocation always carries a non-None ``identity``; direct Lambda
    invocations (the backend publish path) have no identity and are gated by
    IAM, so only real UI callers are group-checked.
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


# --- inline log sanitizer ---------------------------------------------------
# Minimal inline redactor. Kept here rather than importing from idp_common to
# avoid adding a Lambda Layer dependency to this resolver. If this file grows
# to need idp_common anyway, promote to
# `from idp_common.utils.log_sanitizer import sanitize_event_for_logging`.
_LOG_SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "authorization",
    "apikey",
    "api_key",
    "cookie",
    "credential",
    "claims",
    "identity",
)


def _sanitize_for_log(obj):
    """Deep-copy `obj` redacting values whose keys match the denylist."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in _LOG_SENSITIVE_KEYS):
                out[k] = "***REDACTED***" if v is not None else None
            else:
                out[k] = _sanitize_for_log(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_for_log(v) for v in obj]
    return obj


def handler(event, context):
    """
    Handle agent chat message requests from AppSync.

    This function stores user messages in DynamoDB and invokes the agent chat processor
    for conversational, multi-turn interactions. All registered agents are automatically
    available to the orchestrator.

    Args:
        event: The event dict from AppSync containing:
            - prompt: The user's message
            - sessionId: The conversation session ID
            - method: The message method (default: "chat")
        context: The Lambda context

    Returns:
        AgentChatMessage with role, content, timestamp, isProcessing, and sessionId
    """
    logger.info(f"Received agent chat event: {json.dumps(_sanitize_for_log(event))}")

    # Defense-in-depth RBAC: Reviewer is excluded from Agent Chat. Raise so the
    # dispatcher maps it to 403/Unauthorized (not an opaque 500 or a 200 error
    # dict). Backend publish-path invocations have no identity and skip this.
    if event.get("identity") is not None and not _caller_in_groups(
        event, _AGENT_CHAT_GROUPS
    ):
        raise PermissionError(
            "Unauthorized: Agent Chat requires Admin, Author or Viewer group"
        )

    try:
        # Extract arguments from the event
        arguments = event.get("arguments", {})
        prompt = arguments.get("prompt")
        session_id = arguments.get("sessionId")
        method = arguments.get("method", "chat")
        # Default off: the Code Intelligence Agent sends repository context to a
        # third-party MCP service, so callers must opt in explicitly. Require a
        # literal boolean True — the dispatcher's schema rejects non-booleans on
        # the HTTP path, but direct (IAM) invocations are not validated, and
        # bool("false") is True.
        enable_code_intelligence = arguments.get("enableCodeIntelligence") is True
        tool_metadata = arguments.get("toolMetadata")

        # Validate required parameters
        if not prompt:
            error_msg = "prompt parameter is required"
            logger.error(error_msg)
            raise Exception(error_msg)

        if not session_id:
            error_msg = "sessionId parameter is required"
            logger.error(error_msg)
            raise Exception(error_msg)

        # Validate prompt length
        if len(prompt) > 100000:
            error_msg = "Prompt exceeds maximum length of 100000 characters"
            logger.error(f"{error_msg}. Prompt length: {len(prompt)}")
            raise Exception(error_msg)

        # Calculate expiration time (TTL)
        current_time = int(time.time())
        expires_after = current_time + (DATA_RETENTION_DAYS * 24 * 60 * 60)

        # Create timestamp in ISO-8601 format with Z suffix for UTC
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Determine message role and processing status
        assistant_methods = [
            "assistant_response",
            "assistant_final_response",
            "assistant_processing",
            "assistant_thinking",
            "assistant_stream",
            "assistant_tool_use",
            "assistant_tool_result",
            "assistant_error",
            # Sub-agent event types
            "subagent_start",
            "subagent_stream",
            "subagent_end",
            "subagent_error",
            "assistant_final_response",
            # Structured data event types
            "structured_data_start",
            # Tool section event types
            "tool_execution_start",
            "tool_execution_complete",
            "tool_result_start",
            "tool_result_complete",
        ]
        is_assistant_response = method in assistant_methods
        role = "assistant" if is_assistant_response else "user"
        is_processing_complete = method in [
            "assistant_final_response",
            "assistant_error",
            "tool_execution_complete",
            "tool_result_complete",
        ]

        # Only store certain message types in DynamoDB
        # Store user messages and final assistant responses only
        # All intermediate messages (tool executions, tool results, sub-agent messages) are NOT stored
        is_final_response = (
            not is_assistant_response  # Store all user messages
            or method
            in [
                "assistant_final_response",  # Store only the final generated response
                "assistant_error",  # Store final errors for debugging
            ]
        )

        if is_final_response:
            # Store message in DynamoDB with session-based keys
            table = dynamodb.Table(CHAT_MESSAGES_TABLE)

            message = {
                "PK": session_id,  # Session-based partition key
                "SK": timestamp,  # Timestamp as sort key for chronological ordering
                "role": role,
                "content": prompt,
                "timestamp": timestamp,
                "isProcessing": not is_processing_complete,
                "ExpiresAfter": expires_after,
            }

            table.put_item(Item=message)
            logger.info(
                f"Stored message in DynamoDB for session {session_id}: {method}"
            )

            # Manage session metadata for user messages
            if not is_assistant_response:  # This is a user message
                # Get user identity for session metadata
                identity = event.get("identity", {})
                user_id = identity.get("username") or identity.get("sub") or "anonymous"

                manage_session_metadata(
                    user_id, session_id, prompt, timestamp, expires_after
                )
        else:
            logger.info(f"Skipped storing streaming message in DynamoDB: {method}")

        # Invoke the agent chat processor for user messages
        # The processor will handle the orchestrator creation and streaming response
        if not is_assistant_response and AGENT_CHAT_PROCESSOR_FUNCTION:
            # Thread the caller identity through to the processor. Without it,
            # the processor's _persist_chat_turn silently skips writing the
            # final assistant message, and the non-streaming (polling) UI path
            # never sees a reply. Use the same username-first resolution the
            # session-ownership check in get_agent_chat_messages uses, so the
            # processor updates the SAME ChatSessionsTable record this resolver
            # created above.
            identity = event.get("identity", {})
            caller_sub = identity.get("username") or identity.get("sub") or ""
            lambda_client.invoke(
                FunctionName=AGENT_CHAT_PROCESSOR_FUNCTION,
                InvocationType="Event",
                Payload=json.dumps(
                    {
                        "sessionId": session_id,
                        "prompt": prompt,
                        "method": method,
                        "timestamp": timestamp,
                        "enableCodeIntelligence": enable_code_intelligence,
                        "callerSub": caller_sub,
                        # This resolver already stored the user message and session
                        # metadata above — tell the processor to persist only the
                        # assistant reply, or every turn would be double-written.
                        "persistUserMessage": False,
                    }
                ),
            )
            logger.info(
                f"Invoked agent chat processor for session: {session_id} (Code Intelligence: {enable_code_intelligence})"
            )

        # Map method to messageType for frontend
        message_type = (
            method
            if method
            in [
                "tool_execution_start",
                "tool_execution_complete",
                "tool_result_start",
                "tool_result_complete",
                "subagent_stream",
                "assistant_final_response",
                "structured_data_start",
            ]
            else None
        )

        # Return AgentChatMessage format
        response = {
            "role": str(role),
            "content": str(prompt),
            "timestamp": str(timestamp),
            "isProcessing": not is_processing_complete,
            "sessionId": str(session_id),
        }

        # Add messageType if it's a tool-related message
        if message_type:
            response["messageType"] = message_type

        # Add toolMetadata if provided
        if tool_metadata:
            response["toolMetadata"] = tool_metadata

        return response

    except ClientError as e:
        error_msg = f"DynamoDB error: {str(e)}"
        logger.error(error_msg)
        # Return error as assistant message
        return {
            "role": "assistant",
            "content": f"Error: {error_msg}",
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "isProcessing": False,
            "sessionId": str(session_id) if session_id else "",
            "messageType": None,
            "toolMetadata": None,
        }


def manage_session_metadata(user_id, session_id, prompt, timestamp, expires_after):
    """
    Create or update session metadata in the ChatSessionsTable.

    Args:
        user_id: The user ID for the session
        session_id: The session ID
        prompt: The user's message content
        timestamp: The message timestamp
        expires_after: TTL for the session
    """
    try:
        if not CHAT_SESSIONS_TABLE:
            logger.warn(
                "CHAT_SESSIONS_TABLE not configured, skipping session metadata management"
            )
            return

        sessions_table = dynamodb.Table(CHAT_SESSIONS_TABLE)

        # Check if session already exists
        try:
            response = sessions_table.get_item(
                Key={"userId": user_id, "sessionId": session_id}
            )

            if response.get("Item"):
                # Session exists, update it
                update_session_metadata(
                    sessions_table, user_id, session_id, prompt, timestamp
                )
            else:
                # New session, create it
                create_session_metadata(
                    sessions_table,
                    user_id,
                    session_id,
                    prompt,
                    timestamp,
                    expires_after,
                )

        except ClientError as e:
            logger.error(f"Error managing session metadata: {str(e)}")
            # Don't fail the main operation if session metadata fails

    except Exception as e:
        logger.error(f"Unexpected error managing session metadata: {str(e)}")
        # Don't fail the main operation if session metadata fails


def create_session_metadata(
    sessions_table, user_id, session_id, prompt, timestamp, expires_after
):
    """Create a new session metadata record."""
    try:
        # Generate session title from the first message
        title = prompt.strip()
        if len(title) > 50:
            title = title[:47] + "..."

        # Create last message preview
        last_message = prompt[:100] + "..." if len(prompt) > 100 else prompt

        session_record = {
            "userId": user_id,
            "sessionId": session_id,
            "title": title,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "messageCount": 1,  # First user message
            "lastMessage": last_message,
            "ExpiresAfter": expires_after,
        }

        sessions_table.put_item(Item=session_record)
        logger.info(f"Created session metadata for {session_id}")

    except ClientError as e:
        logger.error(f"Error creating session metadata: {str(e)}")


def update_session_metadata(sessions_table, user_id, session_id, prompt, timestamp):
    """Update existing session metadata record."""
    try:
        # Create last message preview
        last_message = prompt[:100] + "..." if len(prompt) > 100 else prompt

        # Update the session record
        sessions_table.update_item(
            Key={"userId": user_id, "sessionId": session_id},
            UpdateExpression="SET updatedAt = :timestamp, messageCount = messageCount + :inc, lastMessage = :last_message",
            ExpressionAttributeValues={
                ":timestamp": timestamp,
                ":inc": 1,
                ":last_message": last_message,
            },
        )

        logger.info(f"Updated session metadata for {session_id}")

    except ClientError as e:
        logger.error(f"Error updating session metadata: {str(e)}")
