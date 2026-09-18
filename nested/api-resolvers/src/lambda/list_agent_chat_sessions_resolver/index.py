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
        limit = arguments.get("limit", 20)  # Default limit
        next_token = arguments.get("nextToken")
        surface = arguments.get("surface")
        
        # Get user identity from context
        identity = event.get("identity", {})
        logger.info(f"DEBUG - Full identity context: {json.dumps(identity)}")
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
        error_msg = f"DynamoDB error: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Error listing chat sessions: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
