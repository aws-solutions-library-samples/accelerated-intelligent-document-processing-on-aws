# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a chat's title and session list show of a message the Quick Start panel sent.

The panel wraps its instructions for the agent in ``<panel-context>`` blocks. The
agent reads them; the user, the session title and the session list must not, and the
UI, the chat processors and the agent chat resolver each strip them.
"""

import importlib.util
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.common.chat_text import PANEL_CONTEXT_PATTERN, visible_chat_text

pytestmark = pytest.mark.unit

HIDDEN = "<panel-context>[3 document(s) attached — do not ask me to upload again.]</panel-context>"
PROCESSOR_COPIES = (
    "src/lambda/agent_chat_processor/index.py",
    "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py",
)


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "lambda" / "agent_chat_processor").is_dir():
            return parent
    raise RuntimeError("repo root not found")


@pytest.mark.parametrize(
    ("prompt", "visible"),
    [
        (f"Pay stubs from our vendor\n\n{HIDDEN}", "Pay stubs from our vendor"),
        (HIDDEN, ""),
        ("Before\n<panel-context>a\nb</panel-context> after", "Before\n after"),
        ("Just a question", "Just a question"),
        ("", ""),
    ],
)
def test_visible_chat_text(prompt, visible):
    assert visible_chat_text(prompt) == visible


def test_the_resolver_and_the_ui_strip_the_same_blocks():
    """Each keeps its own copy of the rule, so the copies are held to this one."""
    root = _repo_root()
    resolver = (
        root / "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py"
    ).read_text(encoding="utf-8")
    ui = (root / "src/ui/src/components/agent-chat/panelContext.ts").read_text(
        encoding="utf-8"
    )
    assert (
        f'_PANEL_CONTEXT_PATTERN = re.compile(r"{PANEL_CONTEXT_PATTERN.pattern}", re.DOTALL)'
        in resolver
    )
    assert PANEL_CONTEXT_PATTERN.flags & re.DOTALL
    assert (
        "const PANEL_CONTEXT_BLOCKS = /<panel-context>[\\s\\S]*?<\\/panel-context>/g;"
        in ui
    )
    assert "const OPEN_TAG = '<panel-context>';" in ui
    assert "const CLOSE_TAG = '</panel-context>';" in ui


def _load_processor(relative: str):
    stubs = {}
    for name, attr in (
        ("idp_common.agents.analytics", "get_analytics_config"),
        ("idp_common.agents.factory", "agent_factory"),
        (
            "idp_common.agents.common.bedrock_error_messages",
            "BedrockErrorMessageHandler",
        ),
    ):
        module = stubs.setdefault(name, types.ModuleType(name))
        setattr(module, attr, MagicMock())
    path = _repo_root() / relative
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            f"panel_context_{path.stem}", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _persist(module, monkeypatch, prompt, existing):
    sessions = MagicMock()
    sessions.get_item.return_value = {"Item": existing} if existing else {}
    resource = MagicMock()
    resource.Table.side_effect = lambda name: (
        sessions if name == "sessions" else MagicMock()
    )
    monkeypatch.setattr(module, "_CHAT_MESSAGES_TABLE", "messages")
    monkeypatch.setattr(module, "_CHAT_SESSIONS_TABLE", "sessions")
    monkeypatch.setattr(module.boto3, "resource", lambda *_a, **_k: resource)
    module._persist_chat_turn("s1", "u1", "quick_start", prompt, "reply")
    return sessions


@pytest.mark.parametrize("relative", PROCESSOR_COPIES)
def test_a_new_session_is_titled_by_what_the_user_sees(relative, monkeypatch):
    module = _load_processor(relative)
    sessions = _persist(module, monkeypatch, f"Attached 3 documents\n\n{HIDDEN}", None)
    item = sessions.put_item.call_args.kwargs["Item"]
    assert item["title"] == "Attached 3 documents"
    assert item["lastMessage"] == "Attached 3 documents"


@pytest.mark.parametrize("relative", PROCESSOR_COPIES)
def test_a_message_the_user_sees_none_of_keeps_the_previous_preview(
    relative, monkeypatch
):
    module = _load_processor(relative)
    sessions = _persist(
        module, monkeypatch, HIDDEN, {"lastMessage": "Pay stubs from our vendor"}
    )
    values = sessions.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":last"] == "Pay stubs from our vendor"


@pytest.fixture
def resolver(monkeypatch):
    directory = _repo_root() / "nested/api-resolvers/src/lambda/agent_chat_resolver"
    monkeypatch.syspath_prepend(str(directory))
    for name in ("chat_session_owner", "log_sanitizer"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "panel_context_agent_chat_resolver", directory / "index.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_resolver_titles_a_new_session_by_what_the_user_sees(resolver):
    sessions = MagicMock()
    resolver.create_session_metadata(
        sessions, "u1", "s1", f"Attached 3 documents\n\n{HIDDEN}", "t", 0
    )
    item = sessions.put_item.call_args.kwargs["Item"]
    assert item["title"] == "Attached 3 documents"
    assert item["lastMessage"] == "Attached 3 documents"


def test_the_resolver_keeps_the_preview_for_a_message_the_user_sees_none_of(resolver):
    sessions = MagicMock()
    resolver.update_session_metadata(sessions, "u1", "s1", HIDDEN, "t")
    call = sessions.update_item.call_args.kwargs
    assert "lastMessage" not in call["UpdateExpression"]
    assert ":last_message" not in call["ExpressionAttributeValues"]
