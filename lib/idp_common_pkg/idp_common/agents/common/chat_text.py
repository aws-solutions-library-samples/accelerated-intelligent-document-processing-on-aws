# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The part of a chat message its sender sees, as opposed to the part only the agent reads.

The Quick Start panel adds instructions for the agent to the messages it sends:
that attached documents are being analysed, or what Discovery found and how to
present it. They are wrapped in ``<panel-context>...</panel-context>`` so the agent
reads them while the user, the session title and the session list do not. The web
UI strips the same blocks before showing a user message
(``src/ui/src/components/agent-chat/panelContext.ts``), and the agent chat resolver
keeps its own copy of the pattern, because it carries no ``idp_common``; a test holds
the three to one pattern.
"""

from __future__ import annotations

import re

PANEL_CONTEXT_PATTERN = re.compile(r"<panel-context>.*?</panel-context>", re.DOTALL)


def visible_chat_text(prompt: str) -> str:
    """``prompt`` without its ``<panel-context>`` blocks, stripped of outer whitespace."""
    return PANEL_CONTEXT_PATTERN.sub("", prompt or "").strip()
