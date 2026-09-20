# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`getChatMessages`' ownership refusal reaches the caller as a 403, not a 500.

Chat sessions carry the user's own queries and whatever document content the model
quoted back, so the ownership check is the control that keeps one user's history
away from another. It refused correctly — and then the handler's catch-all
rewrapped the refusal as ``Exception(f"Error getting agent chat messages: {e}")``,
which destroyed both signals ``http_api_dispatcher`` chooses a status from: the
exception class name became ``Exception``, and the "Unauthorized" token moved off
the front of the message where the anchored ``str.startswith`` could no longer see
it. A cross-user access attempt was therefore reported as a server fault, and
counted in the deployment's 5xx rate instead of its denial signal.

This is the same defect, in the same shape, as the one in
``get_file_contents_resolver`` — a refusal raised correctly and then laundered into
a fault by a catch-all one frame up.
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
        "get_agent_chat_messages_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["get_agent_chat_messages_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _event(*, username="alice@example.com", session_id="s-1"):
    return {
        "info": {"fieldName": "getChatMessages"},
        "arguments": {"sessionId": session_id},
        "identity": {"username": username, "claims": {"sub": "sub-alice"}},
    }


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    """No session row exists, so ownership cannot be established."""
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(index, "dynamodb", resource)
    monkeypatch.setattr(index, "ENFORCE_CHAT_SESSION_OWNERSHIP", True)
    return table


class TestTheOwnershipRefusalIsAnAuthorizationRefusal:
    def test_a_session_the_caller_does_not_own_is_a_permissionerror(self):
        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event(), None)

        assert type(excinfo.value).__name__ == "PermissionError", (
            "the dispatcher matches the exception CLASS NAME to choose 403"
        )
        assert str(excinfo.value).startswith("Unauthorized"), (
            "and falls back to an anchored message prefix, so the message must "
            "still begin with it"
        )

    def test_no_resolvable_identity_is_a_permissionerror(self):
        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event(username="anonymous"), None)

        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_refusal_does_not_name_the_session_or_the_owner(self):
        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event(session_id="s-secret"), None)

        assert "s-secret" not in str(excinfo.value)

    def test_the_messages_table_is_never_queried_for_a_refused_session(self, _tables):
        with pytest.raises(PermissionError):
            index.handler(_event(), None)

        _tables.query.assert_not_called()
