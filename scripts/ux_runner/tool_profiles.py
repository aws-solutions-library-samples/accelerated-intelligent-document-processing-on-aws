# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The tool sandbox the UX session's ``claude`` runs inside.

The session drives a real browser signed in to a shared review stack, on pages
rendered by the MR's own code, so the page is attacker-influenced input to the
model in the same way a diff is to ``scripts/sdlc/ai_mr_review.py``. What the model
can do about anything a page talks it into is decided here, as two explicit lists:

* :data:`EXPERT_ALLOWED_TOOLS` grants the chrome-devtools-mcp tools that
  navigate, give input, snapshot and screenshot, which is what a person
  testing the UI does, plus the four tools of ``scripts/ux_recorder_mcp.py``
  that mark, note, pause and resume the recording;
* :data:`EXPERT_DISALLOWED_TOOLS` denies, by name, the Claude Code built-ins
  ``scripts/sdlc/ai_mr_review.py`` denies and seven more that read, search, keep
  a task list, load a skill, delegate or ask, and every chrome-devtools-mcp tool
  that runs page JavaScript, uploads a file, lists, opens, closes or switches
  tabs, changes the viewport or device, profiles or audits the page, inspects an
  element's CSS rules, or reads console or network traffic.

Three things make the lists mean something, and none of them lives in this file
alone: ``--permission-mode manual`` under ``-p`` refuses anything outside the
granted set, because there is nobody to ask; ``--tools ''`` removes the built-in
tool set before either list is read; and the deny list holds even if a later edit
widens the grant. See ``agent.claude_argv``. So the built-in half of the deny list
is belt and braces, not a list of every built-in the pinned CLI has: releases add
built-ins it does not name (worktree, scheduling and messaging tools among them),
and ``--tools ''`` is what removes those, as it removes the rest.

A tool name says what a tool is for, not every parameter it takes, so the lists
alone do not keep script out of the page: under its default flags the granted
``navigate_page`` takes an ``initScript`` to run on the next document and follows
``javascript:`` and ``data:`` URLs. ``agent.DEVTOOLS_FLAGS`` starts the server with
``--javascriptEvaluation=false``, which removes that parameter, refuses those URLs
and stops serving ``evaluate_script`` at all.

Closed over the pinned server
-----------------------------
:data:`DEVTOOLS_TOOLS_AT_PIN` is the tool universe of the pinned
chrome-devtools-mcp, and the tests require every one of its tools to be either
granted or denied by name. A version bump that adds a tool therefore fails until
somebody decides which list it belongs in, instead of the new tool being refused
or granted by whatever the default happens to be.

The universe was read from chrome-devtools-mcp 1.10.1 itself: ``tools/list`` over
stdio with networking denied answers exactly these 30 names under the server's
default flags, and 23 under ``agent.DEVTOOLS_FLAGS``, which switch the emulation,
performance and memory categories and page JavaScript off. Under those flags no
schema holds ``initScript``, and the only tools that take a ``pageId`` are
``close_page`` and ``select_page``, both denied; under the defaults every granted
browser tool requires one. The universe is the default-flag set on purpose: a flag
that is renamed or ignored by a later release puts those tools back, and they are
denied here by name either way.
"""

from __future__ import annotations

from dataclasses import dataclass

CHROME_DEVTOOLS_SERVER = "chrome-devtools"
RECORDER_SERVER = "ux-recorder"


def mcp_tool(server: str, tool: str) -> str:
    """The tool id Claude Code gives ``tool`` on MCP server ``server``."""
    return f"mcp__{server}__{tool}"


#: Every tool chrome-devtools-mcp 1.10.1 lists under its default flags, by bare
#: name, sorted. Read from the pinned release's own tools/list over stdio.
DEVTOOLS_TOOLS_AT_PIN: tuple[str, ...] = (
    "click",
    "close_page",
    "drag",
    "emulate",
    "evaluate_script",
    "fill",
    "fill_form",
    "get_console_message",
    "get_css_styles",
    "get_network_request",
    "handle_dialog",
    "hover",
    "lighthouse_audit",
    "list_console_messages",
    "list_network_requests",
    "list_pages",
    "navigate_page",
    "new_page",
    "performance_analyze_insight",
    "performance_start_trace",
    "performance_stop_trace",
    "press_key",
    "resize_page",
    "select_page",
    "take_heapsnapshot",
    "take_screenshot",
    "take_snapshot",
    "type_text",
    "upload_file",
    "wait_for",
)

#: The tools scripts/ux_recorder_mcp.py serves, by bare name.
RECORDER_TOOLS: tuple[str, ...] = ("mark", "note", "pause", "resume")

#: What the UX session may do: twelve chrome-devtools tools that navigate, give
#: input, snapshot and screenshot, then the four recorder tools. Nothing that runs
#: script in the page, reads console or network traffic, or touches a second tab.
EXPERT_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__chrome-devtools__navigate_page",
    "mcp__chrome-devtools__wait_for",
    "mcp__chrome-devtools__click",
    "mcp__chrome-devtools__hover",
    "mcp__chrome-devtools__fill",
    "mcp__chrome-devtools__fill_form",
    "mcp__chrome-devtools__type_text",
    "mcp__chrome-devtools__press_key",
    "mcp__chrome-devtools__drag",
    "mcp__chrome-devtools__handle_dialog",
    "mcp__chrome-devtools__take_snapshot",
    "mcp__chrome-devtools__take_screenshot",
    "mcp__ux-recorder__mark",
    "mcp__ux-recorder__note",
    "mcp__ux-recorder__pause",
    "mcp__ux-recorder__resume",
)

#: Named explicitly rather than left to the allowlist: the grant is a claim about
#: what was thought of, and this is a claim about what must not happen however the
#: session is talked into asking for it. First the built-ins ai_mr_review's
#: DISALLOWED_TOOLS denies and seven more that read, search, keep a task list,
#: load a skill, delegate or ask: belt and braces, not the CLI's whole built-in
#: set, which ``--tools ''`` removes outright. Then the chrome-devtools tools that
#: run page JavaScript, upload a file, list, open, close or switch tabs, change
#: the viewport or device (which would misplace the recorder's click markers),
#: profile or audit the page, inspect an element's CSS rules, or read console and
#: network traffic.
EXPERT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "TodoWrite",
    "AskUserQuestion",
    "Skill",
    "mcp__chrome-devtools__evaluate_script",
    "mcp__chrome-devtools__lighthouse_audit",
    "mcp__chrome-devtools__upload_file",
    "mcp__chrome-devtools__new_page",
    "mcp__chrome-devtools__close_page",
    "mcp__chrome-devtools__select_page",
    "mcp__chrome-devtools__list_pages",
    "mcp__chrome-devtools__emulate",
    "mcp__chrome-devtools__resize_page",
    "mcp__chrome-devtools__performance_start_trace",
    "mcp__chrome-devtools__performance_stop_trace",
    "mcp__chrome-devtools__performance_analyze_insight",
    "mcp__chrome-devtools__take_heapsnapshot",
    "mcp__chrome-devtools__get_css_styles",
    "mcp__chrome-devtools__list_console_messages",
    "mcp__chrome-devtools__get_console_message",
    "mcp__chrome-devtools__list_network_requests",
    "mcp__chrome-devtools__get_network_request",
)


def is_granted(tool: str) -> bool:
    """Whether the UX session may call ``tool``: granted and not denied."""
    return tool in EXPERT_ALLOWED_TOOLS and tool not in EXPERT_DISALLOWED_TOOLS


@dataclass(frozen=True)
class ToolProfile:
    """One session's two tool lists, as ``agent.claude_argv`` passes them."""

    name: str
    allowed: tuple[str, ...]
    disallowed: tuple[str, ...]


#: The MR UX test's profile: the two EXPERT lists above, as they are.
EXPERT = ToolProfile(
    name="expert", allowed=EXPERT_ALLOWED_TOOLS, disallowed=EXPERT_DISALLOWED_TOOLS
)
