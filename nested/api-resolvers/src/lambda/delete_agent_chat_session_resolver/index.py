# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Lambda function to delete an agent chat session and all its messages.
This function deletes the session metadata from ChatSessionsTable and all messages from ChatMessagesTable.
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
CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE")
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")

# Mirrors the switch in get_agent_chat_messages_resolver, so the read and the
# delete side of a session answer the same question the same way. Default on.
ENFORCE_CHAT_SESSION_OWNERSHIP = (
    os.environ.get("ENFORCE_CHAT_SESSION_OWNERSHIP", "true").lower() != "false"
)


class ResourceAccessDenied(PermissionError):
    """The caller's groups admit this operation and the OBJECT was refused.

    The canonical class is ``idp_common.api_adapter.ResourceAccessDenied``; this
    resolver carries no layer, so it declares its own and the dispatcher
    recognises it by class name across the invoke boundary. Same 403, same
    ``errorType: "Unauthorized"``, same message — what it adds is
    ``authzControl: "resource"``, so a refusal of this kind is not read as a
    group-authorization failure.
    """


def _caller_owns_session(session_id: str, user_id: str) -> bool:
    """Whether ``user_id`` owns ``session_id``, per ChatSessionsTable.

    ChatSessionsTable is keyed (userId, sessionId), so a hit for this caller is
    what establishes ownership. Fails **closed** on a DynamoDB error: an
    ownership question this cannot answer must not resolve to "yes" on the path
    that then deletes.

    The unconfigured-table case returns True, matching the read side's stated
    early-deployment behaviour rather than introducing a second convention; that
    residual is why the enforcement switch above exists and is documented as the
    configuration an operator must complete.
    """
    if not CHAT_SESSIONS_TABLE:
        logger.warning(
            "CHAT_SESSIONS_TABLE is not configured; cannot verify chat session "
            "ownership before deleting. Allowing; set CHAT_SESSIONS_TABLE in the "
            "Lambda environment to enable ownership enforcement."
        )
        return True

    try:
        sessions_table = dynamodb.Table(CHAT_SESSIONS_TABLE)
        response = sessions_table.get_item(
            Key={"userId": user_id, "sessionId": session_id}
        )
        return "Item" in response
    except ClientError as e:
        logger.error(
            f"Error verifying session ownership before delete for user={user_id} "
            f"session={session_id}: {e}"
        )
        return False


def handler(event, context):
    """
    Delete an agent chat session and all its messages.
    
    Args:
        event: The event dict from AppSync containing:
            - sessionId: The session ID to delete
        context: The Lambda context
        
    Returns:
        Boolean indicating success
    """
    logger.info(f"Received delete agent chat session event: {json.dumps(sanitize_event_for_logging(event))}")
    
    try:
        # Extract arguments from the event
        arguments = event.get("arguments", {})
        session_id = arguments.get("sessionId")
        
        if not session_id:
            raise ValueError("sessionId parameter is required")
        
        # Get user identity from context for security
        identity = event.get("identity", {})
        user_id = identity.get("username") or identity.get("sub") or "anonymous"
        
        logger.info(f"Deleting agent chat session {session_id} (user: {user_id})")

        # Scope this delete to the session's owner BEFORE anything is removed.
        #
        # ⚠️ The two steps below are keyed differently, and only one of them
        # carries the caller. ChatSessionsTable is (userId, sessionId), so step 1
        # can only ever touch this caller's own row. ChatMessagesTable is keyed
        # (PK=sessionId, SK), which holds no caller term at all — so step 2's
        # query and its batch delete are scoped by the session id alone, and a
        # session id is the only argument this operation takes. Establishing
        # ownership here is therefore the only thing that makes step 2 the
        # caller's own data; there is no key expression that can do it.
        #
        # Step 1 is also not a substitute: a miss there deletes nothing and is
        # logged rather than raised (by design, so a half-deleted session can be
        # cleaned up), so it cannot stop step 2 on its own.
        #
        # This mirrors getChatMessages, which refuses the same way — the read and
        # the delete side of one session now answer the same question with the
        # same control, rather than the read side alone.
        if ENFORCE_CHAT_SESSION_OWNERSHIP:
            if user_id == "anonymous":
                logger.warning(
                    "Rejecting deleteChatSession request with no resolvable user "
                    "identity in event.identity (username/sub both absent)."
                )
                raise PermissionError("Unauthorized: caller identity not available.")
            if not _caller_owns_session(session_id, user_id):
                logger.warning(
                    f"Rejecting deleteChatSession: user={user_id} does not own "
                    f"session={session_id}"
                )
                raise ResourceAccessDenied(
                    "Unauthorized: session not found for this user."
                )
        else:
            logger.warning(
                "ENFORCE_CHAT_SESSION_OWNERSHIP is disabled. Skipping the "
                "session-ownership check before deleting user=%s session=%s.",
                user_id,
                session_id,
            )

        # Initialize both tables
        messages_table = dynamodb.Table(CHAT_MESSAGES_TABLE)
        sessions_table = dynamodb.Table(CHAT_SESSIONS_TABLE)

        # Step 1: Delete session metadata from ChatSessionsTable
        try:
            sessions_table.delete_item(
                Key={
                    "userId": user_id,
                    "sessionId": session_id
                }
            )
            logger.info(f"Deleted session metadata for {session_id}")
        except ClientError as e:
            # Log but don't fail if session metadata doesn't exist
            logger.warn(f"Could not delete session metadata: {str(e)}")
        
        # Step 2: Delete all messages from ChatMessagesTable
        response = messages_table.query(
            KeyConditionExpression="PK = :session_id",
            ExpressionAttributeValues={
                ":session_id": session_id
            },
            ProjectionExpression="PK, SK"  # Only need keys for deletion
        )
        
        items = response.get("Items", [])
        
        if not items:
            logger.info(f"No messages found for session {session_id}")
            return True  # Session doesn't exist, consider it successfully deleted
        
        # Delete all messages in batches
        deleted_count = 0
        
        # DynamoDB batch_writer handles batching automatically
        with messages_table.batch_writer() as batch:
            for item in items:
                batch.delete_item(
                    Key={
                        "PK": item["PK"],
                        "SK": item["SK"]
                    }
                )
                deleted_count += 1
        
        logger.info(f"Successfully deleted session metadata and {deleted_count} messages for session {session_id}")
        return True
        
    except PermissionError:
        # ⚠️ Must come before the catch-all, and before `ClientError`, or the
        # refusal above is laundered into a fault. `http_api_dispatcher` picks a
        # status from two signals and the catch-all destroys both: the class name
        # becomes `Exception`, and re-wrapping moves the "Unauthorized" token off
        # the front of the message where the anchored `startswith` can no longer
        # see it. A cross-user delete attempt would then be reported as a 500 and
        # counted in the deployment's fault rate rather than its denial signal —
        # the refusal working, and nothing able to tell.
        #
        # This is the same defect, in the same shape, that
        # `get_agent_chat_messages_resolver` and `get_file_contents_resolver`
        # already carry tests for. Re-raised unchanged so the subclass survives:
        # `ResourceAccessDenied` is what carries `authzControl: "resource"`, and
        # re-raising the base class would erase the distinction this resolver
        # just gained.
        raise
    except ClientError as e:
        error_msg = f"DynamoDB error: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
    except ValueError as e:
        # Re-raise validation errors so they become GraphQL errors
        logger.error(f"Validation error: {str(e)}")
        raise e
    except Exception as e:
        error_msg = f"Error deleting agent chat session: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
