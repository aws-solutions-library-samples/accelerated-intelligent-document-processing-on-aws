# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Lambda function to delete an agent chat session and all its messages.
This function deletes the session metadata from ChatSessionsTable and all messages from ChatMessagesTable.
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError
from chat_session_owner import claim_chat_session, holds_session_row
from log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")

# Get environment variables
CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE")
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")
# Who each session id belongs to, keyed by sessionId. See chat_session_owner.py.
CHAT_SESSION_OWNERS_TABLE = os.environ.get("CHAT_SESSION_OWNERS_TABLE")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

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


def _caller_owns_session(session_id: str, user_id: str, caller_sub: str) -> bool:
    """Whether the caller owns ``session_id``, per ChatSessionOwnersTable.

    The same question getChatMessages asks, answered by the same function
    (``chat_session_owner.claim_chat_session``), so the read and the delete side
    of one session cannot disagree. The caller's own ChatSessionsTable row counts
    only for an id nobody owns yet — a session written before the owner table
    existed — because sending a message under any id used to create one. Never
    registers an unused id. Fails **closed** on an unconfigured table or a
    DynamoDB error: an ownership question this cannot answer must not resolve to
    "yes" on the path that then deletes.
    """
    if not (CHAT_SESSION_OWNERS_TABLE and CHAT_SESSIONS_TABLE):
        logger.error(
            "CHAT_SESSION_OWNERS_TABLE or CHAT_SESSIONS_TABLE is not configured; "
            "cannot verify chat session ownership before deleting, denying."
        )
        return False

    sessions_table = dynamodb.Table(CHAT_SESSIONS_TABLE)
    try:
        return claim_chat_session(
            dynamodb.Table(CHAT_SESSION_OWNERS_TABLE),
            session_id,
            caller_sub,
            expires_after=int(time.time()) + DATA_RETENTION_DAYS * 24 * 60 * 60,
            allow_new=False,
            has_stored_messages=lambda: True,  # not consulted when allow_new=False
            holds_earlier_record=lambda: holds_session_row(
                sessions_table, user_id, session_id
            ),
        )
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
        # What a session's owner is recorded as (ChatSessionOwnersTable).
        caller_sub = identity.get("sub") or (identity.get("claims") or {}).get(
            "sub"
        ) or ""
        
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
            if user_id == "anonymous" or not caller_sub:
                logger.warning(
                    "Rejecting deleteChatSession request with no resolvable user "
                    "identity in event.identity (username/sub both absent)."
                )
                raise PermissionError("Unauthorized: caller identity not available.")
            if not _caller_owns_session(session_id, user_id, caller_sub):
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
