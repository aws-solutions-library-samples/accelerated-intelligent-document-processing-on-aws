# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which signed-in user a chat session id belongs to.

Chat history is stored under the session id alone: ``ChatMessagesTable`` is keyed
``PK=sessionId`` and is shared by Agent Chat and Chat-with-Document. The session id
is chosen by the browser and arrives in the request body, so the only thing that
makes a message read the caller's own is knowing who the id belongs to. The
per-user session list (``ChatSessionsTable``, keyed ``(userId, sessionId)``) cannot
answer that: a row there says the caller has used the id, and sending one message
under any id creates such a row, so it cannot also say nobody else has.

This module records the owner once, in ``ChatSessionOwnersTable`` (keyed
``sessionId``, attribute ``ownerSub``), with a conditional write: the first user
to use an id owns it, and a second user can neither register it nor read it.
The owner is the Cognito ``sub`` from claims a transport verified, which is the
one identifier that is the same on every chat path and never changes.

Two kinds of caller use it:

* a path that **writes** a turn (sending a message) may register an id nobody owns
  yet, provided the id has no stored messages — an id with history and no owner is
  one written before this registry existed, see below;
* a path that **reads or deletes** history (``getChatMessages``,
  ``deleteChatSession``) never registers a fresh id, so asking about an unused id
  cannot reserve it.

Ids written before this registry existed have messages and no owner. They are
registered, on first use, by the caller who holds the older evidence of using
them — the caller's own ``ChatSessionsTable`` row, or the Chat-with-Document
ownership row — and by nobody else. Both kinds of evidence can only have been
written before the registry, because every path that writes them now registers
the owner first.

The owner row's expiry is moved forward on every turn, to the retention period
plus :data:`EXPIRY_MARGIN_SECONDS`, so it outlives the messages written under it:
each message expires a retention period after it was written, and the reply of a
turn is written at most one Lambda invocation (15 minutes) after the turn began.

The same row also records whether the session's conversation holds results from
the agents that read this deployment's own data (Analytics, Error Analyzer):
:data:`DATA_RESULTS_ATTRIBUTE`, written once by :func:`record_data_results` and
read by :func:`holds_data_results`. It lives here because this row is the one
record that is per session, belongs to exactly one user, is read before every
turn on both chat paths, and is not removed when the user deletes the session
from their list — while the conversation memory it describes is kept under the
same user and session. ``idp_common.chat_session_agents`` decides what the flag
changes.

The flag lasts as long as the owner row: its expiry moves forward on every turn,
to the retention period plus the margin below. Conversation memory items carry no
expiry, so a session left idle for longer than that and then resumed under the
same id is registered afresh, without the flag, while its memory is still there.

Vendored verbatim into the resolver Lambdas that ship without the ``idp_common``
layer (``nested/api-resolvers/src/lambda/*/chat_session_owner.py``), so it imports
nothing beyond ``botocore``. ``test_chat_session_owner_vendored.py`` keeps the
copies identical.
"""

from typing import Any, Callable, Optional

from botocore.exceptions import ClientError

#: Attribute holding the owner's Cognito ``sub`` on a ``ChatSessionOwnersTable`` row.
OWNER_ATTRIBUTE = "ownerSub"
#: The table's TTL attribute (epoch seconds), as on the other chat tables.
EXPIRES_ATTRIBUTE = "ExpiresAfter"
#: Added to every expiry this module writes. Longer than any one turn can run, so
#: a reply written at the end of a turn never outlives its session's owner row.
EXPIRY_MARGIN_SECONDS = 3600
#: Epoch seconds at which the session's conversation first held a result from an
#: agent that reads this deployment's data. Absent until then; never removed.
DATA_RESULTS_ATTRIBUTE = "dataResultsAt"


def recorded_owner(owners_table: Any, session_id: str) -> Optional[str]:
    """The ``sub`` that owns ``session_id``, or ``None`` when nobody does.

    A row whose owner attribute is missing or empty returns ``""``, which matches
    no caller: a row that exists without saying whose it is must not read as free.
    A strongly consistent read, because the answer decides a conditional write.
    """
    item = owners_table.get_item(
        Key={"sessionId": session_id}, ConsistentRead=True
    ).get("Item")
    if item is None:
        return None
    return str(item.get(OWNER_ATTRIBUTE) or "")


def _is_conditional_failure(error: ClientError) -> bool:
    return (
        error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def _extend(owners_table: Any, session_id: str, owner_sub: str, expires_after: int):
    """Move the owner row's expiry forward. False if the row is no longer theirs."""
    try:
        owners_table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET #exp = :exp",
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={
                "#exp": EXPIRES_ATTRIBUTE,
                "#owner": OWNER_ATTRIBUTE,
            },
            ExpressionAttributeValues={
                ":exp": expires_after + EXPIRY_MARGIN_SECONDS,
                ":owner": owner_sub,
            },
        )
    except ClientError as error:
        if _is_conditional_failure(error):
            return False
        raise
    return True


def _register(
    owners_table: Any, session_id: str, owner_sub: str, expires_after: int
) -> bool:
    """Record ``owner_sub`` as the owner if nobody is. True if they own it after."""
    try:
        owners_table.put_item(
            Item={
                "sessionId": session_id,
                OWNER_ATTRIBUTE: owner_sub,
                EXPIRES_ATTRIBUTE: expires_after + EXPIRY_MARGIN_SECONDS,
            },
            ConditionExpression="attribute_not_exists(sessionId)",
        )
    except ClientError as error:
        if not _is_conditional_failure(error):
            raise
        # Somebody registered it between our read and our write; it is ours only
        # if that somebody was us.
        return recorded_owner(owners_table, session_id) == owner_sub
    return True


def claim_chat_session(
    owners_table: Any,
    session_id: str,
    owner_sub: str,
    *,
    expires_after: int,
    allow_new: bool,
    has_stored_messages: Callable[[], bool],
    holds_earlier_record: Callable[[], bool],
) -> bool:
    """Whether ``owner_sub`` owns ``session_id``, registering it where allowed.

    Args:
        owners_table: the ``ChatSessionOwnersTable`` resource.
        session_id: the id from the request.
        owner_sub: the caller's Cognito ``sub``, from verified claims. Empty
            never owns anything.
        expires_after: epoch seconds the session's messages are kept until (a
            retention period from now); the owner row is kept
            :data:`EXPIRY_MARGIN_SECONDS` longer, and moved forward on every
            successful call.
        allow_new: True on a path that writes a turn. An id nobody owns and
            that has no stored messages is then registered to the caller.
            False on a read or delete path, which never reserves an unused id.
        has_stored_messages: whether any message is stored under the id. Called
            only when nobody owns it.
        holds_earlier_record: whether the caller holds a session record written
            before the owner registry existed. Called only when nobody owns it.

    Returns:
        True when the caller owns the id after this call, False otherwise. A
        DynamoDB error other than a lost conditional write is raised, so a
        question that could not be answered is never read as "yes".
    """
    if not session_id or not owner_sub:
        return False
    owner = recorded_owner(owners_table, session_id)
    if owner is not None:
        if owner != owner_sub:
            return False
        return _extend(owners_table, session_id, owner_sub, expires_after)
    if holds_earlier_record():
        return _register(owners_table, session_id, owner_sub, expires_after)
    if allow_new and not has_stored_messages():
        return _register(owners_table, session_id, owner_sub, expires_after)
    return False


def has_stored_messages(messages_table: Any, session_id: str) -> bool:
    """Whether ``ChatMessagesTable`` holds any message under ``session_id``."""
    response = messages_table.query(
        KeyConditionExpression="PK = :sid",
        ExpressionAttributeValues={":sid": session_id},
        ProjectionExpression="PK",
        Limit=1,
        ConsistentRead=True,
    )
    return bool(response.get("Items"))


def holds_session_row(sessions_table: Any, user_id: str, session_id: str) -> bool:
    """Whether ``ChatSessionsTable`` has the caller's ``(userId, sessionId)`` row."""
    if not user_id:
        return False
    response = sessions_table.get_item(
        Key={"userId": user_id, "sessionId": session_id}, ConsistentRead=True
    )
    return "Item" in response


def holds_data_results(owners_table: Any, session_id: str) -> bool:
    """Whether ``session_id``'s conversation already holds data-agent results.

    A strongly consistent read of the owner row. ``False`` for an id with no
    row. A DynamoDB error is raised: the caller decides what an unanswered
    question means, and for this one the answer it acts on is "yes".
    """
    if not session_id:
        return False
    item = owners_table.get_item(
        Key={"sessionId": session_id}, ConsistentRead=True
    ).get("Item")
    return bool(item and item.get(DATA_RESULTS_ATTRIBUTE))


def record_data_results(
    owners_table: Any, session_id: str, owner_sub: str, recorded_at: int
) -> bool:
    """Record that ``session_id``'s conversation now holds data-agent results.

    Written only on the owner's own row (``#owner = :owner``), and only once: a
    later call keeps the first time. Returns False when the row is missing or
    belongs to someone else, so nothing was recorded. A DynamoDB error other than
    that condition is raised, so a caller can refuse to hand on a result it could
    not record.
    """
    if not session_id or not owner_sub:
        return False
    try:
        owners_table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET #flag = if_not_exists(#flag, :at)",
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={
                "#flag": DATA_RESULTS_ATTRIBUTE,
                "#owner": OWNER_ATTRIBUTE,
            },
            ExpressionAttributeValues={":at": int(recorded_at), ":owner": owner_sub},
        )
    except ClientError as error:
        if _is_conditional_failure(error):
            return False
        raise
    return True
