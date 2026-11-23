# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Lambda function to get agent chat messages for a specific session.
This function queries the ChatMessagesTable by sessionId and returns messages in chronological order.
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError
from chat_session_owner import claim_chat_session, holds_session_row
from log_sanitizer import sanitize_event_for_logging


class ResourceAccessDenied(PermissionError):
    """A refusal by a per-object control rather than by the caller's group.

    Declared locally because this Lambda's package does not carry ``idp_common``;
    the canonical class is ``idp_common.api_adapter.ResourceAccessDenied`` and the
    dispatcher recognises either by class NAME, since a resolver reached through
    ``lambda:Invoke`` surfaces its exception as the ``errorType`` string and the
    class object never crosses that boundary. (``ResourceNotFound`` and
    ``AuthorizationError`` are already declared this way elsewhere in the tree.)

    Still a ``PermissionError``, so the client sees exactly what it saw before:
    403, ``errorType: "Unauthorized"``, same message. What it adds is
    ``errors[0].authzControl: "resource"``, which is how a consumer tells "refused
    because of their group" from "group was fine, this session is not theirs" —
    two opposite verdicts that arrived in one indistinguishable response and made
    the live RBAC matrix report this check working as a group-check failure
    (issue #1308).
    """


# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class ReportedServerError(RuntimeError):
    """A 500 whose message is written for the caller, so the dispatcher passes it on.

    Declared locally because this resolver does not import ``idp_common``; the
    dispatcher matches the class by name. The canonical class, and the rule for
    when to raise it, is ``idp_common.api_adapter.ReportedServerError``.
    """


# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")

# Get environment variables
CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE")
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE")
# Document-chat sessions live in a separate table (PK=sessionId, ownerSub=<sub>)
# rather than the agent ChatSessionsTable (PK=userId, SK=sessionId). This
# resolver serves BOTH agent and document chat (the UI polls getChatMessages for
# both in the non-streaming / GovCloud path), so ownership is verified against
# whichever table registered the session. Optional: when unset, doc-chat
# ownership simply isn't checked here (agent-chat behavior is unchanged).
CHAT_DOCUMENT_SESSIONS_TABLE = os.environ.get("CHAT_DOCUMENT_SESSIONS_TABLE")
# Who each session id belongs to, keyed by sessionId — the one record every chat
# path writes before it stores a message. See chat_session_owner.py.
CHAT_SESSION_OWNERS_TABLE = os.environ.get("CHAT_SESSION_OWNERS_TABLE")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

# Feature flag: enforce that the calling Cognito user owns the session before
# returning messages. Defaults to "true". An operator can set this to "false"
# via the ENFORCE_CHAT_SESSION_OWNERSHIP environment variable to restore the
# pre-fix behavior during migration (e.g., if legacy sessions predate the
# ChatSessionsTable and need to be accessible to administrators). We log a
# warning when the enforcement is disabled.
ENFORCE_CHAT_SESSION_OWNERSHIP = (
    os.environ.get("ENFORCE_CHAT_SESSION_OWNERSHIP", "true").lower() != "false"
)


def _owns_document_session(session_id: str, caller_sub: str) -> bool:
    """Whether a Chat-with-Document ownership row names ``caller_sub``.

    ``CHAT_DOCUMENT_SESSIONS_TABLE`` (PK=sessionId, ownerSub) is where the
    Chat-with-Document resolver recorded owners before ``ChatSessionOwnersTable``
    existed, so a row there is the earlier evidence for a document-chat session.
    Raises on a DynamoDB error; the caller fails closed.
    """
    if not CHAT_DOCUMENT_SESSIONS_TABLE or not caller_sub:
        return False
    doc_sessions = dynamodb.Table(CHAT_DOCUMENT_SESSIONS_TABLE)
    item = doc_sessions.get_item(Key={"sessionId": session_id}).get("Item")
    return bool(item) and item.get("ownerSub") == caller_sub


def _verify_session_ownership(session_id: str, user_id: str, caller_sub: str) -> bool:
    """Return True if the caller owns the given chat session.

    The owner is the one ``ChatSessionOwnersTable`` records for the session id
    (``chat_session_owner.claim_chat_session``). Holding a ``ChatSessionsTable``
    row for the id is not enough on its own: sending a message under any id used
    to create one, so a row says the caller has used the id, not that nobody else
    has. Such a row — or a Chat-with-Document ownership row — counts only for an
    id nobody owns yet, which is a session written before the owner table
    existed, and the first such read registers the caller as its owner.

    This read never registers an id nobody has used, so asking about one cannot
    reserve it. Fails **closed**: an unconfigured owner table, or any DynamoDB
    error, denies.
    """
    if not (CHAT_SESSION_OWNERS_TABLE and CHAT_SESSIONS_TABLE):
        logger.error(
            "CHAT_SESSION_OWNERS_TABLE or CHAT_SESSIONS_TABLE is not configured; "
            "cannot verify chat session ownership, denying."
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
            holds_earlier_record=lambda: (
                holds_session_row(sessions_table, user_id, session_id)
                or _owns_document_session(session_id, caller_sub)
            ),
        )
    except ClientError as e:
        logger.error(
            f"Error verifying session ownership for user={user_id} "
            f"session={session_id}: {e}"
        )
        return False


def handler(event, context):
    """
    Get agent chat messages for a specific session.

    Args:
        event: The event dict from AppSync containing:
            - sessionId: The session ID to retrieve messages for
        context: The Lambda context

    Returns:
        List of AgentChatMessage objects
    """
    # Log a redacted copy — event.identity.claims contains Cognito user info.
    logger.info(
        f"Received get agent chat messages event: "
        f"{json.dumps(sanitize_event_for_logging(event))}"
    )

    try:
        # Extract arguments from the event
        arguments = event.get("arguments", {})
        session_id = arguments.get("sessionId")

        if not session_id:
            raise ValueError("sessionId parameter is required")

        # Get user identity from context for security
        identity = event.get("identity", {})
        user_id = identity.get("username") or identity.get("sub") or "anonymous"
        # The Cognito sub specifically: it is what a session's owner is recorded
        # as. The session list (ChatSessionsTable) keys on username||sub, hence
        # both are tracked.
        caller_sub = (
            identity.get("sub") or (identity.get("claims") or {}).get("sub") or ""
        )

        logger.info(
            f"Getting agent chat messages for session {session_id} (user: {user_id})"
        )

        # Authorization: enforce that the calling user owns the requested
        # session. Chat sessions can contain sensitive PII (user queries,
        # document content quoted by the LLM), so same-account Cognito
        # authentication alone is not sufficient. The owner is the one
        # ChatSessionOwnersTable records for the session id.
        if ENFORCE_CHAT_SESSION_OWNERSHIP:
            if user_id == "anonymous" or not caller_sub:
                logger.warning(
                    "Rejecting getChatMessages request with no resolvable user "
                    "identity in event.identity (username/sub both absent)."
                )
                raise PermissionError("Unauthorized: caller identity not available.")
            if not _verify_session_ownership(session_id, user_id, caller_sub):
                logger.warning(
                    f"Rejecting getChatMessages: user={user_id} does not own "
                    f"session={session_id}"
                )
                # Per-object: the caller holds a group this operation admits and
                # this SESSION is not theirs. The refusal above — no resolvable
                # caller identity at all — is not about the object, so it keeps the
                # unattributed class and the strict reading that goes with it.
                raise ResourceAccessDenied(
                    "Unauthorized: session not found for this user."
                )
        else:
            logger.warning(
                "ENFORCE_CHAT_SESSION_OWNERSHIP is disabled. Skipping "
                "session-ownership check for user=%s session=%s.",
                user_id,
                session_id,
            )

        # Query the ChatMessagesTable for this specific session
        table = dynamodb.Table(CHAT_MESSAGES_TABLE)

        # Query by PK (sessionId) to get all messages for this session
        response = table.query(
            KeyConditionExpression="PK = :session_id",
            ExpressionAttributeValues={":session_id": session_id},
            ScanIndexForward=True,  # Sort by SK (timestamp) in ascending order
        )

        items = response.get("Items", [])

        # Convert DynamoDB items to AgentChatMessage format
        messages = []
        for item in items:
            message = {
                "role": item.get("role", ""),
                "content": item.get("content", ""),
                "timestamp": item.get("timestamp", ""),
                "isProcessing": item.get("isProcessing", False),
                "sessionId": item.get("PK", ""),  # PK is the sessionId
            }
            messages.append(message)

        logger.info(f"Returning {len(messages)} messages for session {session_id}")
        return messages

    except ClientError as e:
        # Logged in full, returned as this message, which is written for the caller:
        # a botocore authorization message names the assumed-role ARN and the table
        # ARN, so the detail goes to the operator. `ReportedServerError` is the class
        # the dispatcher passes a 500 message through for.
        logger.error("DynamoDB error: %s", e, exc_info=True)
        raise ReportedServerError(
            "This deployment could not read the chat data. Contact an administrator."
        ) from e
    except (PermissionError, ValueError):
        # Re-raised unchanged. The catch-all below wrapped every exception as
        # `Exception(f"Error getting agent chat messages: {e}")`, which destroyed
        # both signals http_api_dispatcher chooses a status from: the class name
        # became `Exception`, and the "Unauthorized" token moved off the front of
        # the message, where `str.startswith` could no longer see it. So a
        # cross-user chat-history access attempt — the thing the ownership check
        # exists to refuse — was reported as a 500 server fault, and landed in the
        # 5xx rate rather than the denial signal. Both refusals are already logged
        # at WARNING at their raise site; re-logging them here at ERROR is what made
        # a deliberate denial indistinguishable from a crash.
        raise
    except Exception as e:
        error_msg = f"Error getting agent chat messages: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise Exception(error_msg) from e
