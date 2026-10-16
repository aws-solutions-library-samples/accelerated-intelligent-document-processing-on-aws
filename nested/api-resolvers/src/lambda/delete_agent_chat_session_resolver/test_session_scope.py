# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`deleteChatSession` deletes the caller's own session and nothing else.

The two tables this resolver writes are keyed differently, and only one of them
carries the caller. ChatSessionsTable is ``(userId, sessionId)``, so the metadata
delete can only ever reach this caller's row. ChatMessagesTable is
``(PK=sessionId, SK)``, which holds no caller term — so the message query and the
batch delete that follows it are scoped by the session id alone, and a session id
is the only argument the operation takes.

That makes the ownership check the thing that scopes the destructive half. There
is no key expression that can do it, and the metadata delete cannot stand in: a
miss there removes nothing and is logged rather than raised, by design, so that a
half-deleted session can still be cleaned up.

The assertions below are therefore about the *order and the effect*: no query and
no delete against the messages table before ownership is established, and the
refusal carried as a resource-level denial so it is not read as a group-
authorization failure. ``getChatMessages`` refuses the same way, so the read and
the delete side of one session now answer the same question with the same control.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CHAT_MESSAGES_TABLE", "IDP-ChatMessagesTable")
os.environ.setdefault("CHAT_SESSIONS_TABLE", "IDP-ChatSessionsTable")

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "delete_agent_chat_session_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["delete_agent_chat_session_index"] = module
    spec.loader.exec_module(module)
    return module


def _wire(module, *, session_owned_by_caller):
    """Point the module's DynamoDB resource at two recording table doubles.

    ``session_owned_by_caller`` decides what ChatSessionsTable answers for the
    ownership probe, which is the single input this behaviour turns on.
    """
    sessions = MagicMock(name="ChatSessionsTable")
    messages = MagicMock(name="ChatMessagesTable")

    sessions.get_item.return_value = (
        {"Item": {"userId": "owner", "sessionId": "s-1"}}
        if session_owned_by_caller
        else {}
    )
    messages.query.return_value = {
        "Items": [{"PK": "s-1", "SK": "m#1"}, {"PK": "s-1", "SK": "m#2"}]
    }

    def table(name):
        return messages if name == os.environ["CHAT_MESSAGES_TABLE"] else sessions

    module.dynamodb = MagicMock()
    module.dynamodb.Table.side_effect = table
    return sessions, messages


def _event(user, session_id="s-1"):
    return {
        "arguments": {"sessionId": session_id},
        "identity": {"username": user, "sub": f"sub-{user}"},
    }


def test_a_session_the_caller_does_not_own_is_refused_and_nothing_is_deleted():
    module = _load_index()
    sessions, messages = _wire(module, session_owned_by_caller=False)

    with pytest.raises(PermissionError) as refused:
        module.handler(_event("someone-else"), None)

    # Resource-level, not the group floor: the caller's groups admit this
    # operation and the OBJECT was refused. The dispatcher reads the class name.
    assert type(refused.value).__name__ == "ResourceAccessDenied"
    assert str(refused.value).startswith("Unauthorized:")

    # The ownership probe was aimed at the caller's own partition...
    sessions.get_item.assert_called_once_with(
        Key={"userId": "someone-else", "sessionId": "s-1"}
    )
    # ...and the destructive half never ran. Both halves matter: a query that
    # happens before the refusal would already have read another user's keys.
    messages.query.assert_not_called()
    messages.batch_writer.assert_not_called()
    sessions.delete_item.assert_not_called()


def test_the_owner_deletes_their_own_session_and_its_messages():
    module = _load_index()
    sessions, messages = _wire(module, session_owned_by_caller=True)

    assert module.handler(_event("owner"), None) is True

    sessions.get_item.assert_called_once_with(
        Key={"userId": "owner", "sessionId": "s-1"}
    )
    sessions.delete_item.assert_called_once_with(
        Key={"userId": "owner", "sessionId": "s-1"}
    )
    messages.query.assert_called_once()
    messages.batch_writer.assert_called_once()


def test_a_caller_with_no_resolvable_identity_is_refused_before_any_read():
    """``anonymous`` is the fallback when neither username nor sub is present.

    Kept distinct from the case above: this refusal is not about the object, so
    it stays a plain ``PermissionError`` and keeps the unattributed class — and
    the strict reading the harness gives an unlabelled refusal.
    """
    module = _load_index()
    sessions, messages = _wire(module, session_owned_by_caller=True)

    with pytest.raises(PermissionError) as refused:
        module.handler({"arguments": {"sessionId": "s-1"}, "identity": {}}, None)

    assert type(refused.value).__name__ == "PermissionError"
    sessions.get_item.assert_not_called()
    messages.query.assert_not_called()


def test_the_ownership_probe_fails_closed_when_the_lookup_errors():
    """A DynamoDB error during the probe must not resolve to "yes".

    This is the direction that matters on a path that then deletes: an ownership
    question the resolver cannot answer has to deny.
    """
    from botocore.exceptions import ClientError

    module = _load_index()
    sessions, messages = _wire(module, session_owned_by_caller=True)
    sessions.get_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
    )

    with pytest.raises(PermissionError):
        module.handler(_event("owner"), None)

    messages.query.assert_not_called()
    messages.batch_writer.assert_not_called()
