# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Lambda function to list chat sessions for the current user.
This function queries the ChatSessionsTable to get session metadata efficiently.
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")

# Get environment variables
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")

# Page size: default, and the ceiling a caller cannot raise.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


def _clamped_limit(requested) -> int:
    """The requested page size, bounded to [1, MAX_PAGE_SIZE].

    A non-integer is treated as "not asked for" rather than raising, matching the
    tolerant shape in test_results_resolver: the spec has already established the
    argument is an Int when it is present at all.
    """
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(value, MAX_PAGE_SIZE))


def handler(event, context):
    """
    List chat sessions for the current user from the ChatSessionsTable.
    
    Args:
        event: The event dict from AppSync containing:
            - limit: Optional limit for pagination
            - nextToken: Optional pagination token
        context: The Lambda context
        
    Returns:
        ChatSessionConnection with items and nextToken
    """
    logger.info(f"Received list chat sessions event: {json.dumps(sanitize_event_for_logging(event))}")
    logger.info(f"DEBUG - CHAT_SESSIONS_TABLE env var: {CHAT_SESSIONS_TABLE}")
    
    try:
        # Extract arguments from the event
        arguments = event.get("arguments", {})
        # Clamped, not defaulted. `arguments.get("limit", 20)` was a default the
        # caller could raise without bound — the central validation spec checks
        # only that it is an Int — so the page size was whatever was asked for.
        # Same shape as the enforced clamps in list_documents_gsi_resolver and
        # test_set_resolver; the lower bound is there because DynamoDB rejects a
        # non-positive Limit with a ValidationException, which surfaces as a 500.
        limit = _clamped_limit(arguments.get("limit"))
        next_token = arguments.get("nextToken")
        surface = arguments.get("surface")
        
        # Get user identity from context
        identity = event.get("identity", {})
        # NOT the whole identity object: `sanitize_event_for_logging` two lines up
        # redacts `identity` and `claims` precisely because they carry the caller's
        # token claims, and dumping it here put back what that call took out.
        logger.debug("Resolving caller from identity keys: %s", sorted(identity))
        user_id = identity.get("username") or identity.get("sub") or "anonymous"
        
        logger.info(f"Listing chat sessions for user: {user_id}")
        
        # Check if table name is configured
        if not CHAT_SESSIONS_TABLE:
            logger.error("CHAT_SESSIONS_TABLE environment variable not set")
            return {
                "items": [],
                "nextToken": None
            }
        
        # Query the ChatSessionsTable for this user's sessions
        table = dynamodb.Table(CHAT_SESSIONS_TABLE)
        
        # Build query parameters
        query_params = {
            "KeyConditionExpression": "userId = :user_id",
            "ExpressionAttributeValues": {
                ":user_id": user_id
            },
            "ScanIndexForward": False,  # Sort by sessionId descending (most recent first)
            "Limit": limit
        }

        # Scope to a surface (Companion "chat" vs "quick_start") when requested.
        # Legacy rows written before the surface attribute existed are treated as
        # "chat" so they remain visible in the Companion history.
        if surface == "chat":
            query_params["FilterExpression"] = "attribute_not_exists(surface) OR surface = :surface"
            query_params["ExpressionAttributeValues"][":surface"] = surface
        elif surface:
            query_params["FilterExpression"] = "surface = :surface"
            query_params["ExpressionAttributeValues"][":surface"] = surface

        if next_token:
            try:
                query_params["ExclusiveStartKey"] = json.loads(next_token)
            except (json.JSONDecodeError, ValueError):
                logger.warn(f"Invalid next_token format: {next_token}")
                # Continue without pagination
        
        # Query the sessions table
        response = table.query(**query_params)
        items = response.get("Items", [])
        
        # Convert DynamoDB items to ChatSession format
        sessions = []
        for item in items:
            session = {
                "sessionId": item.get("sessionId", ""),
                "title": item.get("title", "Untitled Chat"),
                "createdAt": item.get("createdAt", ""),
                "updatedAt": item.get("updatedAt", ""),
                "messageCount": item.get("messageCount", 0),
                "lastMessage": item.get("lastMessage", "")
            }
            sessions.append(session)
        
        # Prepare next token for pagination
        response_next_token = None
        if response.get("LastEvaluatedKey"):
            response_next_token = json.dumps(response["LastEvaluatedKey"])
        
        result = {
            "items": sessions,
            "nextToken": response_next_token
        }
        
        logger.info(f"Returning {len(sessions)} sessions for user {user_id}")
        return result
        
    except ClientError as e:
        # Logged in full, returned generically. The class name is `Exception`, so the
        # dispatcher relays this message verbatim into the 500 body — and a botocore
        # authorization message names the assumed-role ARN and the table ARN. Same
        # disclosure the S3 path in get_file_contents_resolver closes, and the same
        # remedy: detail to the operator, not to the caller.
        logger.error("DynamoDB error: %s", e, exc_info=True)
        raise Exception(
            "This deployment could not read the chat data. Contact an administrator."
        ) from e
    except Exception as e:
        error_msg = f"Error listing chat sessions: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
