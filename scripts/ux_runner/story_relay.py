# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The relay between a story flow's model and the pinned chrome-devtools-mcp.

In relay mode the ``ux-story`` server (``scripts/ux_story_mcp.py``) is the only MCP
server Claude Code sees. It starts chrome-devtools-mcp itself, exactly as
``agent.mcp_servers`` starts it for the MR UX test, speaks MCP to it as a client
that reports no ``roots`` capability, and serves only the twelve
``tool_profiles.STORY_BROWSER_TOOLS`` the pinned release lists, with the arguments
that write files, take a full-page capture or pick a lossless format removed from
their schemas and from every call.

Between the model and the browser it holds the flow's guard rails: an action
budget, a viewport screenshot after each action attached to that action's result
up to an image cap, a cap on looks and on refused calls, snapshots cut to a fixed
number of elements, a best-effort commit guard, and signed-out detection. Every
call it handles becomes one journal line through :class:`Ledger`, which also puts
the action and image counts on the server's own lines.

The commit guard judges a click by the control's name, and a key that presses or
submits (Enter, or Space on a control) by the element that has focus, whether
``press_key`` presses it or ``type_text`` types it: the pinned server types a line
break as Enter and a space as Space. On a page whose editor may save its own edits,
which the review workspace's does and marks with Discard Changes while it is
editable (:data:`AUTOSAVE_CONTROLS`), it also refuses every call that changes a
value there:
the editing tools, a key other than Tab or Escape, and a click on a checkbox, option
or another control a click sets. It judges all of these against the page as it is
when the call arrives: after any browser call but a screenshot whose answer held no
snapshot, the relay reads the page again for itself before it lets such a call
through, so a control relabelled in place (a wizard's "Next" that becomes "Create")
is judged by its new name. That read is not shown to the
model and is not journalled.

A link in the side navigation that leads to another page of the same app is followed
rather than judged by its name, because clicking it opens that page and the side
navigation sends nothing when one of its links is clicked: its "Upload Document(s)"
opens the upload page, whose own Upload button is refused. That needs the link's own
line to give it an address after its name, with no other quoted in its text, on the
page's own origin and other than the page's own address, and the link to sit in the
landmark the web UI names the side navigation (:data:`SIDE_NAVIGATION_NAMES`),
outside any dialog. ``scripts/tests/test_ux_story_relay.py`` checks against
``src/ui/src`` that only that navigation carries the name and that nothing in it
sends a mutation or handles a click on a link beyond following it. A link anywhere
else, breadcrumbs and other navigation landmarks included, and one with no address,
with ``#``, with another place on the same page or with the page itself, is judged
by its name as any control is, since a page can attach any handler to it. A link to
a sign-out route is refused as ending the session, whatever it is called.

chrome-devtools-mcp writes names and values unescaped, so a value holding a newline
arrives as continuation lines, and a line of page text shaped like an element
cannot be told from a real one. The guard therefore judges every element that
carries the uid clicked, and treats every element after an open modal dialog as
inside it, so a forged line cannot take away a refusal that an element's own line
earns, or one that an open modal before it earns. It can take away one that rests
on where an element sits, because an element's parent is the nearest earlier line
one level shallower: a forged line re-parents every real element after it that is
deeper than it, up to the next real element at its depth or shallower. So it can
take a control out of a dialog that is not modal, out of a form, or out of the
control or the checkbox it sits in, and an unnamed button away from the text box
beside it, and it can put a later link to a route of the app into a forged side
navigation, where it is followed. A name is written unescaped as well, so a link
with no address whose name ends in a quote and an address reads as having that
address, and in the side navigation is followed too. Following a link to a route
commits nothing unless the page also sends something when it is clicked, which
the guard cannot see. A forged sign-in form can still end a flow as signed out,
and a forged address can make a help link look seen.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import agent, processes, tool_profiles

#: Bumped when the relay would serve, refuse or journal a call differently for the
#: same page, which starts a new series for every flow.
RELAY_REVISION = 6
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "ux-story"
CLIENT_VERSION = "1"
CHILD_NAME = "chrome-devtools-mcp"
STRIPPED_ARGUMENTS: tuple[str, ...] = ("filePath", "fullPage", "format", "quality")
SCREENSHOT_FORMAT = "jpeg"
SCREENSHOT_QUALITY = 70
IMAGE_CAP = 18
LOOKS_PER_ACTION = 2
REFUSAL_LIMIT = 20
SNAPSHOT_NODES = 220
SNAPSHOT_CHARS = 30_000
TEXT_CHARS = 40_000
SCREEN_BYTES = 400 * 1024
SCREENS_OVER_BUDGET = 40
REQUEST_BYTES = 1024 * 1024
RESULT_BYTES = 8 * 1024 * 1024
CALL_SECONDS = 120.0
START_SECONDS = 30.0
STOP_SECONDS = 5.0
TIMEOUT_MS_MAX = 60_000
SNAPSHOT_HEADING = "## Latest page snapshot"
INJECTION_TOOL = "note_injection"
COMMIT_VERBS: tuple[str, ...] = (
    "save",
    "delete",
    "remove",
    "upload",
    "submit",
    "publish",
    "process",
    "reprocess",
    "re-extract",
    "re-run",
    "rerun",
    "restore",
    "reset",
    "abort",
    "apply",
    "confirm",
    "re-confirm",
    "approve",
    "accept",
    "reject",
    "mark",
    "label",
    "claim",
    "release",
    "complete",
    "take over",
    "next item",
    "unassign",
    "clear read",
    "skip",
    "send",
    "pause",
    "resume",
    "probe",
    "enable",
    "disable",
    "activate",
    "deactivate",
    "change",
    "auto-detect",
    "sync",
    "run",
    "start",
    "choose file",
    "choose files",
    "browse",
    "generate",
    "regenerate",
    "import",
    "replace",
    "wipe",
    "install",
    "uninstall",
    "subscribe",
    "unsubscribe",
    "deploy",
    "use as",
    "cancel subscription",
)
#: Refused only inside a dialog. Outside one, a control named so opens a form or a
#: confirmation ("Create queue", "Assign to…", "Move…"), or rearranges something on
#: the page until it is saved ("Move up").
DIALOG_VERBS: tuple[str, ...] = ("create", "add", "new", "assign", "reassign", "move")
#: A control so named marks a page whose editor may save an edit by itself. The review
#: workspace's editor saves a draft a moment after the last edit, and the first save
#: of an unassigned item assigns it to the Admin who made it. It shows Discard Changes
#: whenever it is editable, as every annotation frame and the configuration editor do,
#: so on a page that does not save by itself this refuses only an edit a story could
#: not save anyway; Save draft is what an older deployment's editor shows.
AUTOSAVE_CONTROLS: tuple[str, ...] = ("save draft", "discard changes")
#: The browser tools that set a value wherever they are pointed.
EDITING_TOOLS: tuple[str, ...] = ("fill", "fill_form", "type_text", "drag")
#: Roles of the controls a click sets a value on.
EDITING_ROLES: tuple[str, ...] = (
    "checkbox",
    "switch",
    "radio",
    "option",
    "menuitemcheckbox",
    "menuitemradio",
    "slider",
    "spinbutton",
)
#: The keys that move the focus or close what is open, and set no value.
FOCUS_KEYS: tuple[str, ...] = ("tab", "escape")
#: What separates a control's status from its action in one name, as in
#: "Claimed by you — release"; each part after one is judged as a name of its own.
NAME_SEPARATOR = "\u2014"
SESSION_END_VERBS: tuple[str, ...] = (
    "sign out",
    "sign-out",
    "signout",
    "log out",
    "log-out",
    "logout",
)
CONTROL_ROLES: tuple[str, ...] = (
    "button",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
)
DIALOG_ROLES: tuple[str, ...] = ("dialog", "alertdialog")
#: Roles of the landmark a link must sit in to be followed rather than judged.
NAVIGATION_ROLES: tuple[str, ...] = ("navigation",)
#: The names, as :func:`plain` compares them, of the one navigation landmark whose
#: links are followed: the side navigation, which every page of the web UI names so
#: (``appLayoutLabels.navigation``). Breadcrumbs, step lists and any other
#: navigation are judged by name.
SIDE_NAVIGATION_NAMES: tuple[str, ...] = ("side navigation",)
#: The role of a page's root element, whose own line gives the page's address.
ROOT_ROLE = "RootWebArea"
#: The schemes of an address a followed link may lead to.
ROUTE_SCHEMES: tuple[str, ...] = ("http", "https")
TEXT_ENTRY_ROLES: tuple[str, ...] = ("textbox", "searchbox", "combobox", "spinbutton")
#: Text entries Enter submits wherever they are; the others only in a form or dialog.
SUBMITTING_ENTRY_ROLES: tuple[str, ...] = ("textbox",)
FORM_ROLES: tuple[str, ...] = ("form",)
#: The keys that press a focused control, as the pinned server names them, casefolded.
PRESSING_KEYS: tuple[str, ...] = ("enter", "numpadenter", "\r", "\n", " ", "space")
#: Of those, the ones that also submit the text entry that has focus.
SUBMITTING_KEYS: tuple[str, ...] = ("enter", "numpadenter", "\r", "\n")
CONFIRMING_DIALOGS: tuple[str, ...] = ("confirm", "prompt")
SIGN_IN_BUTTON_RE = re.compile(
    r"(?:sign in|sign-in|signin|log in|log-in|login)(?: with .+)?"
)

OUT_OF_STEPS = (
    "Out of steps: stop acting; call report_done or report_blocked, then write your "
    "short report."
)
OUT_OF_STEPS_MARK = "Out of steps"
COMMIT_POINT_TEXT = (
    "This would change something on a shared system. If pressing it would complete "
    "the task, call report_done with stopped_at_commit_point=true; otherwise continue "
    "another way."
)
SESSION_END_TEXT = (
    "This would end the session. Do not sign out; continue another way, or report "
    "what you found."
)
STALE_UID_TEXT = (
    "That element is not in your latest snapshot. Take a snapshot and use a uid "
    "from it."
)
NO_DIALOG_TEXT = (
    "No browser dialog has been shown to you. Take a snapshot to see whether one "
    "is open before you answer it."
)
PAGE_CHANGED_TEXT = (
    "The page has changed since your latest snapshot and that element is no longer "
    "on it. Take a snapshot and use a uid from it."
)
PAGE_UNREAD_TEXT = (
    "The page could not be read just now, so this was not done. Take a snapshot "
    "and try again."
)
REPORTED_TEXT = (
    "You have already reported, so this flow takes no more actions. Write your "
    "short summary and stop."
)
NO_LOOKS_TEXT = "No more looks in this flow; act, or report what you found."
NO_SCREENSHOTS_TEXT = "No more screenshots in this flow; use take_snapshot."
SIGNED_OUT_TEXT = (
    "The application has signed you out, which ends this flow. Stop acting and "
    "write your short summary."
)
BROWSER_GONE_TEXT = (
    "The browser has stopped answering, which ends this flow. Stop acting and write "
    "your short summary."
)
SESSION_OVER_TEXT = (
    "This flow's browser session has ended. Write your short summary and stop."
)
REFUSAL_LIMIT_TEXT = (
    "This flow has ended: too many calls were refused. Write your short summary "
    "and stop."
)
TIMED_OUT_TEXT = "The browser did not answer in time."
CANCELLED_TEXT = "The call was cancelled."
TOO_LARGE_TEXT = "The browser's answer was too large to relay."
SCREENSHOT_UNUSABLE_TEXT = "The screenshot could not be shown."
CUT_TEXT = (
    "(Snapshot cut: {shown} of its {total} elements are listed. The rest of the page "
    "is not in this snapshot; page through, filter or close what is open to list "
    "other elements.)"
)
TEXT_CUT_TEXT = "(Answer cut at {limit} characters.)"

_NODE_RE = re.compile(r"(?P<indent>(?:  )*)uid=(?P<uid>[^\s\"]+)(?: (?P<rest>.*))?")
_ROLE_RE = re.compile(r"(?P<role>[^\s\"]+)")
_NAME_RE = re.compile(
    r" ?\"(?P<name>.*?)\"(?=$| [A-Za-z][\w-]*(?:=\"|\s|$)| \[selected)", re.S
)
_URL_ATTR_RE = re.compile(r"\burl=\"(?P<url>[^\"]+)\"")
_URL_TEXT_RE = re.compile(r"https?://[^\s\"'<>()]+")
_OPEN_DIALOG_RE = re.compile(
    r"^# Open dialog\n(?P<kind>[a-z]+): (?P<message>.*?)\.?\nCall ", re.M | re.S
)
_BLOCKED_DIALOG_RE = re.compile(
    r"A dialog is open \((?P<kind>[a-z]+): (?P<message>.*?)\)\.", re.S
)
_MODAL_RE = re.compile(r"(?:^| )modal(?= |$)")
_FLAG_RE = re.compile(r"[a-z]+")
_DEFAULT_VALUE_RE = re.compile(r" \(default value: \".*\"\)$", re.S)
_SECTION_END_RE = re.compile(r"^(?:#{1,2} |Error: )")
_JPEG_MAGIC = b"\xff\xd8\xff"
_JOURNAL_INT_BOUND = 2**31
_POLL_SECONDS = 0.1
_READ_BYTES = 64 * 1024
_METHOD_NOT_FOUND = -32601


class RelayError(Exception):
    """chrome-devtools-mcp could not be started, or answered out of protocol."""


class Journal(Protocol):
    @property
    def seq(self) -> int: ...

    def call(
        self,
        *,
        tool: str,
        cls: str,
        ok: bool,
        refused: str | None = None,
        label: str | None = None,
        args: Mapping[str, Any] | None = None,
        ids: Sequence[str] = (),
        screen: str | None = None,
        counted: bool = False,
        actions_used: int | None = None,
        images_sent: int | None = None,
        terminal: str | None = None,
        after_budget: bool | None = None,
    ) -> int: ...

    def end(self, reason: str) -> None: ...


class Ledger:
    """The flow's journal in relay mode: counts on every line, refusals counted.

    Every line in a relay journal carries the actions counted so far and the images
    sent so far, the server's own lines included, so the server writes through this
    rather than the journal itself. A counted line adds itself to the action count
    before it is written, an accepted terminal report gets ``after_budget`` from the
    count at that moment, and every refused line adds one to :attr:`refusals`,
    which ends the flow at :data:`REFUSAL_LIMIT`. The one refused line that does not
    is an injection note past the ones recorded: the model is told it was taken, so
    a page full of attempts cannot end the flow through the notes about them.
    """

    def __init__(self, journal: Journal, *, budget: int) -> None:
        self.journal = journal
        self.budget = budget
        self.actions_used = 0
        self.images_sent = 0
        self.looks = 0
        self.refusals = 0

    @property
    def seq(self) -> int:
        """The seq of the last line written."""
        return self.journal.seq

    @property
    def spent(self) -> bool:
        """Whether the flow has had as many refused calls as it may."""
        return self.refusals >= REFUSAL_LIMIT

    def count_image(self) -> None:
        """One more image reaches the model with the next line written."""
        self.images_sent += 1

    def call(
        self,
        *,
        tool: str,
        cls: str,
        ok: bool,
        refused: str | None = None,
        label: str | None = None,
        args: Mapping[str, Any] | None = None,
        ids: Sequence[str] = (),
        screen: str | None = None,
        counted: bool = False,
        actions_used: int | None = None,
        images_sent: int | None = None,
        terminal: str | None = None,
        after_budget: bool | None = None,
    ) -> int:
        """Write one line with the relay's counts; any counts passed in are ignored."""
        if counted:
            self.actions_used += 1
        if refused is not None and not (tool == INJECTION_TOOL and refused == "limit"):
            self.refusals += 1
        if terminal is not None:
            after_budget = self.actions_used >= self.budget
        return self.journal.call(
            tool=tool,
            cls=cls,
            ok=ok,
            refused=refused,
            label=label,
            args=args,
            ids=ids,
            screen=screen,
            counted=counted,
            actions_used=self.actions_used,
            images_sent=self.images_sent,
            terminal=terminal,
            after_budget=after_budget,
        )

    def end(self, reason: str) -> None:
        """End the journal."""
        self.journal.end(reason)


@dataclass(frozen=True)
class Node:
    """One element of a snapshot: its uid, role, name and place in the tree.

    ``parent`` is the index of the nearest enclosing element in the snapshot's
    ``nodes``, or None at the root. ``text`` is the element's own line, with any
    continuation lines a value holding a newline brought with it. ``flags`` are the
    bare words after the name on the element's own line, such as ``focused`` and
    ``multiline``; a word inside a quoted value can land there too, which only ever
    adds a flag. ``url`` is the address the element's own line gives after its
    name, as a link's ``url="..."`` does, when its text quotes no other: an
    element whose name or value quotes one has none, whether or not it has one of
    its own.
    """

    uid: str
    role: str
    name: str
    depth: int
    parent: int | None
    text: str
    flags: frozenset[str] = frozenset()
    url: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """A relayed snapshot: every element parsed, and the ones the model was shown.

    A uid can repeat in one snapshot, for text boxes chrome-devtools-mcp cannot
    tell apart, so elements are held in page order and a uid resolves to the last
    element carrying it, as the server's own map resolves it.
    """

    nodes: tuple[Node, ...]
    shown: frozenset[int]
    text: str
    addresses: frozenset[str]

    @property
    def total(self) -> int:
        """How many elements the page's snapshot held before it was cut."""
        return len(self.nodes)

    def find(self, uid: str) -> int | None:
        """The index of the element ``uid`` names, or None."""
        found = self.indices(uid)
        return found[-1] if found else None

    def indices(self, uid: str) -> list[int]:
        """The index of every element carrying ``uid``, in page order."""
        return [index for index, node in enumerate(self.nodes) if node.uid == uid]

    def chain(self, index: int) -> list[Node]:
        """The element and its ancestors, nearest first."""
        return [self.nodes[current] for current in self._chain_indices(index)]

    def _chain_indices(self, index: int) -> list[int]:
        found: list[int] = []
        current: int | None = index
        while current is not None and current not in found:
            found.append(current)
            current = self.nodes[current].parent
        return found

    def in_dialog(self, index: int) -> bool:
        """Whether the element is a dialog, sits inside one, or follows an open modal."""
        if any(node.role in DIALOG_ROLES for node in self.chain(index)):
            return True
        return any(
            node.role in DIALOG_ROLES and _MODAL_RE.search(node.text.split("\n")[0])
            for node in self.nodes[:index]
        )

    def _in_form(self, index: int) -> bool:
        return any(node.role in FORM_ROLES for node in self.chain(index))

    @property
    def page_url(self) -> str | None:
        """The page's own address, from its root element's line, or None."""
        if self.nodes and self.nodes[0].role == ROOT_ROLE:
            return self.nodes[0].url
        return None

    def followed_link(self, index: int) -> int | None:
        """The index of the link a click on the element follows within the app, or None.

        A click lands on the element, or, when the element is no control itself,
        such as a link's own text, on the nearest control holding it. That control
        is followed when it is a ``link`` with an address of its own (:class:`Node`'s
        ``url``) that leads to another page of the page's own app
        (:func:`leads_to_a_route`), and the nearest :data:`NAVIGATION_ROLES`
        landmark holding it is the side navigation (:data:`SIDE_NAVIGATION_NAMES`),
        outside any dialog. Any other control is judged by its name: a link in
        breadcrumbs or another navigation, and one with no address, with ``#``,
        with another place on the same page or with the page itself among them,
        since a page can attach any handler to one of those.
        """
        for position in self._chain_indices(index):
            if self.nodes[position].role in CONTROL_ROLES:
                return position if self._navigates(position) else None
        return None

    def _navigates(self, index: int) -> bool:
        node = self.nodes[index]
        page = self.page_url
        if node.role != "link" or node.url is None or page is None:
            return False
        if not leads_to_a_route(node.url, page) or self.in_dialog(index):
            return False
        landmark = next(
            (
                other
                for other in self.chain(index)[1:]
                if other.role in NAVIGATION_ROLES
            ),
            None,
        )
        return landmark is not None and plain(landmark.name) in SIDE_NAVIGATION_NAMES

    def commit_control(self, index: int) -> tuple[str, str] | None:
        """``(refusal, name)`` when clicking the element would press a commit control.

        The element itself counts whatever its role, so a click on a control's own
        text is judged by that text, and so does every ancestor whose role is in
        :data:`CONTROL_ROLES`. A name that begins with a sign-out verb is a
        ``session_end``; one that begins with a commit verb, or with one of
        :data:`DIALOG_VERBS` inside a dialog, is a ``commit_point``, and so is a name
        whose part after :data:`NAME_SEPARATOR` does. A control with no name at all
        beside a text entry, such as a chat box's send icon, is a ``commit_point``
        too. A link to a route of the app's own whose path names a sign-out is a
        ``session_end`` whatever its name.

        A click that follows a link (:meth:`followed_link`) opens another page, and
        the side navigation sends nothing when one of its links is clicked, which
        ``scripts/tests/test_ux_story_relay.py`` checks against the web UI's
        source, so that link, and the element the click landed on inside it, are
        judged by the sign-out verbs alone: the side navigation's "Upload
        Document(s)" opens the upload page, whose own Upload button is refused. A
        control holding that link is judged as usual.
        """
        chain = self._chain_indices(index)
        followed = self.followed_link(index)
        opened = chain[: chain.index(followed) + 1] if followed is not None else []
        for position in chain:
            node = self.nodes[position]
            if position != index and node.role not in CONTROL_ROLES:
                continue
            if self._signs_out(node):
                return "session_end", node.name or "a sign-out link"
            if not plain(node.name):
                if (
                    position not in opened
                    and node.role in CONTROL_ROLES
                    and self._beside_text_entry(position)
                ):
                    return "commit_point", f"an unnamed {node.role}"
                continue
            found = self._judged_name(
                node, self.in_dialog(position), opens=position in opened
            )
            if found is not None:
                return found, node.name
        return None

    def _signs_out(self, node: Node) -> bool:
        page = self.page_url
        if node.role != "link" or node.url is None or page is None:
            return False
        route = route_of(node.url, page)
        return route is not None and names_a_sign_out(route)

    def _judged_name(self, node: Node, inside: bool, *, opens: bool) -> str | None:
        parts = [node.name, *node.name.split(NAME_SEPARATOR)[1:]]
        for part in (plain(part) for part in parts):
            if not part:
                continue
            if begins_with(part, SESSION_END_VERBS):
                return "session_end"
            if opens:
                continue
            if begins_with(part, COMMIT_VERBS):
                return "commit_point"
            if inside and begins_with(part, DIALOG_VERBS):
                return "commit_point"
        return None

    def _beside_text_entry(self, index: int) -> bool:
        parent = self.nodes[index].parent
        return any(
            node.role in TEXT_ENTRY_ROLES and self._is_within(other, parent)
            for other, node in enumerate(self.nodes)
            if other != index
        )

    def _is_within(self, index: int, ancestor: int | None) -> bool:
        return ancestor is None or ancestor in self._chain_indices(index)[1:]

    def control_of(self, uid: str) -> tuple[str, str] | None:
        """The first refusal any element carrying ``uid`` earns, in page order."""
        for index in self.indices(uid):
            found = self.commit_control(index)
            if found is not None:
                return found
        return None

    def pressed(self, key: str) -> tuple[str, str] | None:
        """``(refusal, name)`` when pressing ``key`` would press or submit something.

        ``key`` is the key the pinned server presses, casefolded. It is judged by
        every element marked ``focused``: one that is a commit control as a click
        on it would be, and, for a key in :data:`SUBMITTING_KEYS`, a text entry
        that such a key submits, which is a ``textbox`` anywhere and any other text
        entry inside a form or a dialog. Space in a text entry types a space, so
        there it presses nothing.
        """
        if key not in PRESSING_KEYS:
            return None
        for index, node in enumerate(self.nodes):
            if "focused" not in node.flags:
                continue
            if key not in SUBMITTING_KEYS and node.role in TEXT_ENTRY_ROLES:
                continue
            found = self.commit_control(index)
            if found is not None:
                return found
            if key in SUBMITTING_KEYS and self._submits(index):
                field = plain(node.name)
                return "commit_point", f"Enter in {node.name}" if field else "Enter"
        return None

    @property
    def autosave_control(self) -> str | None:
        """The name of a control marking an editor that saves its own edits, or None.

        Such a page saves a value as soon as it changes, so changing one there is a
        commit point whatever control the change is made with. A line of page text
        shaped like that control only adds refusals. Markers are tried in the order
        :data:`AUTOSAVE_CONTROLS` lists them, so a page showing Save draft names that
        rather than Discard Changes.
        """
        for marker in AUTOSAVE_CONTROLS:
            for node in self.nodes:
                if node.role in CONTROL_ROLES and begins_with(
                    plain(node.name), (marker,)
                ):
                    return node.name
        return None

    def sets_a_value(self, uid: str) -> tuple[str, str] | None:
        """``(commit_point, name)`` when clicking ``uid`` would set a value an editor saves.

        That is a click on a page with an :attr:`autosave_control` on an element
        carrying ``uid`` that is, or sits inside, a control of a role in
        :data:`EDITING_ROLES`; ``name`` is the autosave control's.
        """
        marker = self.autosave_control
        if marker is None:
            return None
        for index in self.indices(uid):
            if any(node.role in EDITING_ROLES for node in self.chain(index)):
                return "commit_point", marker
        return None

    def _submits(self, index: int) -> bool:
        role = self.nodes[index].role
        if role in SUBMITTING_ENTRY_ROLES:
            return True
        return role in TEXT_ENTRY_ROLES and (
            self.in_dialog(index) or self._in_form(index)
        )

    @property
    def signed_out(self) -> bool:
        """Whether the page is the sign-in form: a password field beside a sign-in button.

        Beside means inside the same form, or neither inside a form. A password
        field alone, such as a change-password form's, is not the sign-in form, and
        neither is a sign-in link or a button that only begins with the words.
        """
        fields = [
            index
            for index, node in enumerate(self.nodes)
            if node.role == "textbox" and plain(node.name).rstrip(" *:") == "password"
        ]
        buttons = [
            index
            for index, node in enumerate(self.nodes)
            if node.role == "button" and SIGN_IN_BUTTON_RE.fullmatch(plain(node.name))
        ]
        return any(
            self._form_of(one) == self._form_of(other)
            for one in fields
            for other in buttons
        )

    def _form_of(self, index: int) -> int | None:
        for position, node in enumerate(self.chain(index)):
            if position and node.role == "form":
                return self.nodes.index(node)
        return None


@dataclass(frozen=True)
class Reply:
    """What the relay answers one call with, and a recorder mark to forward, if any."""

    content: tuple[Mapping[str, Any], ...]
    is_error: bool
    mark: Mapping[str, str] | None = None

    def result(self) -> dict[str, Any]:
        """The MCP tool result."""
        return {
            "content": [dict(item) for item in self.content],
            "isError": self.is_error,
        }


@dataclass
class Outcome:
    """How one request to chrome-devtools-mcp ended.

    ``kind`` is ``result`` or ``error`` (a JSON-RPC answer, in ``message``), ``gone``
    (the child exited or closed its output), ``size`` (an answer over
    :data:`RESULT_BYTES`), ``timeout``, or ``stopped`` (cancelled by the client or
    by a stop signal).
    """

    kind: str
    message: Mapping[str, Any] | None = None


@dataclass
class _Waiter:
    request_id: int
    done: threading.Event = field(default_factory=threading.Event)
    outcome: Outcome | None = None


def plain(name: str) -> str:
    """A control name as the guard compares it: casefolded, one-spaced, leading marks gone."""
    text = " ".join(name.split()).casefold()
    start = 0
    while start < len(text) and not text[start].isalnum():
        start += 1
    return text[start:]


def begins_with(name: str, verbs: Sequence[str]) -> bool:
    """Whether ``name`` begins with one of ``verbs`` as a whole word or phrase."""
    for verb in verbs:
        if name == verb:
            return True
        if name.startswith(verb) and not name[len(verb)].isalnum():
            return True
    return False


def route_of(url: str, page: str) -> str | None:
    """The path of the app's route ``url`` leads to from ``page``, or None.

    Both must be http(s) addresses of one origin, the same scheme, host and port,
    and ``url`` must lead somewhere ``page`` is not. A fragment that begins with
    ``/`` is a hash router's route, and its path is the fragment's up to any query,
    unless ``url`` is ``page`` itself. Otherwise ``url`` must name another document
    than ``page``, by its path or its query, and its path is the route's. ``#``, a
    fragment that names a place on the page, and the page's own address are no
    route: following one stays on the page, and runs whatever the page attached to
    the link.
    """
    try:
        link = urllib.parse.urlsplit(url)
        here = urllib.parse.urlsplit(page)
        origins = (
            (link.scheme, link.hostname, link.port),
            (here.scheme, here.hostname, here.port),
        )
    except ValueError:
        return None
    if link.scheme not in ROUTE_SCHEMES or link.hostname is None:
        return None
    if origins[0] != origins[1]:
        return None
    same_document = (link.path or "/", link.query) == (here.path or "/", here.query)
    if link.fragment.startswith("/"):
        if same_document and link.fragment == here.fragment:
            return None
        return link.fragment.split("?", 1)[0]
    return None if same_document else (link.path or "/")


def leads_to_a_route(url: str, page: str) -> bool:
    """Whether ``url`` leads from ``page`` to a route of the same app (:func:`route_of`)."""
    return route_of(url, page) is not None


def names_a_sign_out(route: str) -> bool:
    """Whether a segment of ``route``'s path is one of :data:`SESSION_END_VERBS`."""
    return any(
        urllib.parse.unquote(segment).casefold() in SESSION_END_VERBS
        for segment in route.split("/")
    )


def devtools_argv(
    devtools: Path, *, home: Path, fs_root: Path, search_path: str
) -> list[str]:
    """chrome-devtools-mcp's command line, as ``agent.mcp_servers`` starts it."""
    return [
        "/usr/bin/env",
        "-i",
        f"PATH={search_path}",
        f"HOME={home}",
        "LANG=C.UTF-8",
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1",
        str(devtools),
        *agent.DEVTOOLS_FLAGS,
        f"--filesystemRoot={fs_root}",
    ]


def relayed_tools(listed: object) -> dict[str, dict[str, Any]]:
    """The tools the relay serves: STORY_BROWSER_TOOLS the child lists, stripped.

    A tool keeps its name, description, annotations and input schema. The schema
    loses every property in :data:`STRIPPED_ARGUMENTS`, takes no other properties,
    and bounds a ``timeout`` to :data:`TIMEOUT_MS_MAX` milliseconds. The order is
    ``STORY_BROWSER_TOOLS``'s.
    """
    tools = listed.get("tools") if isinstance(listed, Mapping) else None
    served: dict[str, Mapping[str, Any]] = {}
    for tool in tools if isinstance(tools, list) else ():
        if isinstance(tool, Mapping) and isinstance(tool.get("name"), str):
            served.setdefault(tool["name"], tool)
    found: dict[str, dict[str, Any]] = {}
    for name in tool_profiles.STORY_BROWSER_TOOLS:
        tool = served.get(name)
        if tool is None or not isinstance(tool.get("inputSchema"), Mapping):
            continue
        found[name] = _stripped(tool)
    return found


def _stripped(tool: Mapping[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(json.dumps(tool["inputSchema"]))
    properties = schema.get("properties")
    kept = {
        key: _bounded(key, spec)
        for key, spec in (properties.items() if isinstance(properties, dict) else ())
        if key not in STRIPPED_ARGUMENTS and isinstance(spec, dict)
    }
    schema["properties"] = kept
    if isinstance(schema.get("required"), list):
        schema["required"] = [key for key in schema["required"] if key in kept]
    schema["additionalProperties"] = False
    definition: dict[str, Any] = {
        "name": tool["name"],
        "description": str(tool.get("description") or ""),
        "inputSchema": schema,
    }
    annotations = tool.get("annotations")
    if isinstance(annotations, Mapping):
        definition["annotations"] = json.loads(json.dumps(annotations))
    return definition


def _bounded(key: str, spec: dict[str, Any]) -> dict[str, Any]:
    if key == "timeout" and spec.get("type") == "integer":
        return {**spec, "minimum": 0, "maximum": TIMEOUT_MS_MAX}
    return spec


def _type_problem(key: str, spec: Mapping[str, Any], value: object) -> str | None:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return f"{key} must be a string"
        choices = spec.get("enum")
        if isinstance(choices, list) and value not in choices:
            return f"{key} must be one of: {', '.join(str(c) for c in choices)}"
        return None
    if kind == "boolean":
        return None if isinstance(value, bool) else f"{key} must be true or false"
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{key} must be a whole number"
        return None
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{key} must be a number"
        return None
    if kind == "array":
        return _array_problem(key, spec, value)
    if kind == "object" and not isinstance(value, dict):
        return f"{key} must be an object"
    return None


def _array_problem(key: str, spec: Mapping[str, Any], value: object) -> str | None:
    if not isinstance(value, list):
        return f"{key} must be a list"
    least = spec.get("minItems")
    if isinstance(least, int) and len(value) < least:
        return f"{key} needs at least {least} item(s)"
    items = spec.get("items")
    if not isinstance(items, Mapping):
        return None
    for item in value:
        if items.get("type") == "string" and not isinstance(item, str):
            return f"each item of {key} must be a string"
        if items.get("type") == "object":
            if not isinstance(item, dict):
                return f"each item of {key} must be an object"
            fields = items.get("properties") or {}
            for name in items.get("required") or ():
                if not isinstance(item.get(name), str) and (
                    fields.get(name, {}).get("type") == "string"
                ):
                    return f"each item of {key} needs a string {name}"
    return None


def read_arguments(
    schema: Mapping[str, Any], arguments: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """The arguments to forward, the arguments to journal, and the first problem.

    Only the schema's own properties are read: anything else the model sent is
    dropped, never forwarded and never journalled. A ``timeout`` is clamped to the
    bound the schema states. Lists and objects are journalled as compact JSON text.
    """
    properties = schema.get("properties")
    specs: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
    forward: dict[str, Any] = {}
    problem: str | None = None
    for key, spec in specs.items():
        if key not in arguments or not isinstance(spec, Mapping):
            continue
        value = arguments[key]
        wrong = _type_problem(key, spec, value)
        if wrong:
            problem = problem or wrong
            continue
        if key == "timeout" and isinstance(value, int):
            value = min(max(value, 0), TIMEOUT_MS_MAX)
        forward[key] = value
    for key in schema.get("required") or ():
        if key not in forward and problem is None:
            problem = f"{key} is required"
    journal = {key: _journal_value(value) for key, value in forward.items()}
    return forward, journal, problem


def _journal_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and -_JOURNAL_INT_BOUND < value < _JOURNAL_INT_BOUND:
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def parse_nodes(lines: Sequence[str]) -> list[Node]:
    """The elements of a snapshot section's lines, one per ``uid=`` line.

    A line that does not start an element continues the one before it, which is
    how a value holding a newline arrives. The parent of an element is the nearest
    earlier element one level shallower. An element's ``url`` is read once its
    continuation lines are in, so an address a continuation line quotes takes its
    address away, as one its name quotes does.
    """
    nodes: list[Node] = []
    attributes: list[str] = []
    last_at_depth: dict[int, int] = {}
    for line in lines:
        match = _NODE_RE.fullmatch(line)
        if match is None:
            if nodes:
                nodes[-1] = dataclasses.replace(
                    nodes[-1], text=nodes[-1].text + "\n" + line
                )
            continue
        depth = len(match.group("indent")) // 2
        role, name, flags, after = _role_name_and_flags(match.group("rest") or "")
        parent = last_at_depth.get(depth - 1) if depth else None
        for deeper in [level for level in last_at_depth if level >= depth]:
            del last_at_depth[deeper]
        last_at_depth[depth] = len(nodes)
        nodes.append(Node(match.group("uid"), role, name, depth, parent, line, flags))
        attributes.append(after)
    return [
        dataclasses.replace(node, url=_only_url(node.text, after))
        for node, after in zip(nodes, attributes, strict=True)
    ]


def _only_url(text: str, after: str) -> str | None:
    own = _URL_ATTR_RE.findall(after)
    if len(own) != 1 or len(_URL_ATTR_RE.findall(text)) != 1:
        return None
    return own[0]


def _role_name_and_flags(rest: str) -> tuple[str, str, frozenset[str], str]:
    role = ""
    position = 0
    found = _ROLE_RE.match(rest)
    if found is not None:
        role = found.group("role")
        position = found.end()
    named = _NAME_RE.match(rest, position)
    if named is not None:
        position = named.end()
    after = rest[position:]
    flags = frozenset(word for word in after.split() if _FLAG_RE.fullmatch(word))
    return role, named.group("name") if named is not None else "", flags, after


def split_section(text: str) -> tuple[str, list[str], str] | None:
    """``(before, snapshot lines, after)`` of a text holding a snapshot, else None."""
    lines = text.split("\n")
    try:
        start = lines.index(SNAPSHOT_HEADING)
    except ValueError:
        return None
    end = start + 1
    while end < len(lines) and not _SECTION_END_RE.match(lines[end]):
        end += 1
    body = lines[start + 1 : end]
    while body and not body[-1].strip():
        body.pop()
    before = "\n".join(lines[:start])
    after = "\n".join(lines[end:])
    return before, body, after


def cut_snapshot(
    lines: Sequence[str],
    *,
    nodes: int = SNAPSHOT_NODES,
    chars: int = SNAPSHOT_CHARS,
) -> Snapshot:
    """The snapshot as the model is shown it: at most ``nodes`` elements and ``chars``.

    It is cut on element boundaries. When it has to be cut, the elements of an
    open modal dialog are kept first, because a modal is drawn at the end of the
    page and is what the page is waiting on, and then the page's other elements
    from the top, each run stopping at the first element that does not fit. What is
    shown stays in page order, followed by a note saying how many elements were
    left out.
    """
    parsed = tuple(parse_nodes(lines))
    whole = sum(len(node.text) + 1 for node in parsed)
    chosen: set[int]
    if len(parsed) <= nodes and whole <= chars:
        chosen = set(range(len(parsed)))
    else:
        modal = _modal_members(parsed)
        chosen = set()
        used = 0
        for group in (
            [i for i in range(len(parsed)) if i in modal],
            [i for i in range(len(parsed)) if i not in modal],
        ):
            for index in group:
                cost = len(parsed[index].text) + 1
                if len(chosen) >= nodes or used + cost > chars:
                    break
                chosen.add(index)
                used += cost
    shown = [node.text for index, node in enumerate(parsed) if index in chosen]
    if len(chosen) < len(parsed):
        shown.append(CUT_TEXT.format(shown=len(chosen), total=len(parsed)))
    return Snapshot(
        nodes=parsed,
        shown=frozenset(chosen),
        text="\n".join([SNAPSHOT_HEADING, *shown]),
        addresses=frozenset(
            _addresses(
                node.text for index, node in enumerate(parsed) if index in chosen
            )
        ),
    )


def _modal_members(parsed: Sequence[Node]) -> set[int]:
    roots = {
        index
        for index, node in enumerate(parsed)
        if node.role in DIALOG_ROLES and _MODAL_RE.search(node.text)
    }
    members: set[int] = set()
    for index, node in enumerate(parsed):
        current: int | None = index
        steps = 0
        while current is not None and steps <= len(parsed):
            if current in roots:
                members.add(index)
                break
            current = parsed[current].parent
            steps += 1
    return members


def _addresses(texts: Any) -> set[str]:
    found: set[str] = set()
    for text in texts:
        urls = [m.group("url") for m in _URL_ATTR_RE.finditer(text)]
        urls += [m.group(0).rstrip(".,;:") for m in _URL_TEXT_RE.finditer(text)]
        for url in urls:
            found.add(url)
            try:
                parts = urllib.parse.urlsplit(url)
            except ValueError:
                continue
            if parts.path:
                tail = parts.path
                if parts.query:
                    tail += "?" + parts.query
                if parts.fragment:
                    tail += "#" + parts.fragment
                found.add(tail)
                found.add(tail.lstrip("/"))
    found.discard("")
    return found


def open_dialog(texts: Sequence[str]) -> tuple[str, str] | None:
    """``(type, message)`` of the browser dialog the child's answer says is open."""
    for text in texts:
        match = _OPEN_DIALOG_RE.search(text) or _BLOCKED_DIALOG_RE.search(text)
        if match is not None:
            message = _DEFAULT_VALUE_RE.sub("", match.group("message"))
            return match.group("kind"), message
    return None


def jpeg_bytes(image: Mapping[str, Any]) -> bytes | None:
    """The decoded JPEG of an image item, when it is one and within SCREEN_BYTES."""
    if image.get("mimeType") != "image/jpeg" or not isinstance(image.get("data"), str):
        return None
    if len(image["data"]) > (SCREEN_BYTES * 4) // 3 + 4:
        return None
    try:
        data = base64.b64decode(image["data"], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > SCREEN_BYTES or not data.startswith(_JPEG_MAGIC):
        return None
    return data


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


class Child:
    """chrome-devtools-mcp as an MCP client sees it: one request at a time.

    Its output is read on a thread of its own, which answers the child's ``ping``,
    refuses every other request it makes (``roots/list``, ``sampling/*``,
    ``elicitation/*``) with method-not-found, drops its notifications and hands the
    answer to the one request in flight. A line longer than ``limit`` bytes fails
    that request as ``size``; the end of its output fails it as ``gone``.
    """

    def __init__(
        self, process: processes.ManagedProcess, *, limit: int | None = None
    ) -> None:
        popen = process.popen
        if popen.stdin is None or popen.stdout is None:
            raise RelayError("chrome-devtools-mcp was started without pipes")
        self.process = process
        self._stdin = popen.stdin
        self._stdout = popen.stdout
        self._limit = RESULT_BYTES if limit is None else limit
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._waiting: _Waiter | None = None
        self.alive = True
        self.closing = False
        self._reader = threading.Thread(
            target=self._read, name="devtools-reader", daemon=True
        )
        self._reader.start()

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
        stop: Callable[[], bool] = lambda: False,
    ) -> Outcome:
        """Send one request and wait for its answer, ``timeout`` seconds at most."""
        with self._lock:
            if not self.alive:
                return Outcome("gone")
            waiter = _Waiter(self._next_id)
            self._next_id += 1
            self._waiting = waiter
        try:
            message = {
                "jsonrpc": "2.0",
                "id": waiter.request_id,
                "method": method,
                "params": dict(params),
            }
            if not self._send(message):
                return Outcome("gone")
            deadline = time.monotonic() + timeout
            while not waiter.done.wait(_POLL_SECONDS):
                if stop():
                    self._cancel(waiter.request_id, "cancelled by the client")
                    return Outcome("stopped")
                if time.monotonic() >= deadline:
                    self._cancel(waiter.request_id, "timed out")
                    return Outcome("timeout")
            return waiter.outcome or Outcome("gone")
        finally:
            with self._lock:
                if self._waiting is waiter:
                    self._waiting = None

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> bool:
        """Send one notification; False when the child can no longer be written to."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        return self._send(message)

    def close(self) -> None:
        """Close the child's input, which ends it, and kill its group if it lingers."""
        self.closing = True
        with self._write_lock:
            try:
                self._stdin.close()
            except OSError:
                pass
        try:
            self.process.popen.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        self.process.terminate(grace=STOP_SECONDS)
        self._reader.join(timeout=STOP_SECONDS)
        if not self._reader.is_alive():
            try:
                self._stdout.close()
            except OSError:
                pass

    def _cancel(self, request_id: int, reason: str) -> None:
        self.notify(
            "notifications/cancelled", {"requestId": request_id, "reason": reason}
        )

    def _send(self, message: Mapping[str, Any]) -> bool:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self._stdin.closed:
                return False
            view = memoryview(data)
            try:
                descriptor = self._stdin.fileno()
                while view:
                    view = view[os.write(descriptor, view) :]
            except (OSError, ValueError):
                return False
        return True

    def _read(self) -> None:
        buffer = bytearray()
        discarding = False
        while True:
            try:
                chunk = os.read(self._stdout.fileno(), _READ_BYTES)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > self._limit:
                        buffer.clear()
                        if not discarding:
                            discarding = True
                            self._deliver_failure("size")
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if discarding or len(line) > self._limit:
                    if not discarding:
                        self._deliver_failure("size")
                    discarding = False
                    continue
                self._handle(line)
        with self._lock:
            self.alive = False
            waiter = self._waiting
        if waiter is not None and waiter.outcome is None:
            waiter.outcome = Outcome("gone")
            waiter.done.set()

    def _deliver_failure(self, kind: str) -> None:
        with self._lock:
            waiter = self._waiting
        if waiter is not None and waiter.outcome is None:
            waiter.outcome = Outcome(kind)
            waiter.done.set()

    def _handle(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict):
            return
        if "method" in message:
            if "id" in message:
                self._answer(message)
            return
        if "id" not in message or not ("result" in message or "error" in message):
            return
        with self._lock:
            waiter = self._waiting
        if waiter is None or message.get("id") != waiter.request_id:
            return
        if isinstance(message["id"], bool) or waiter.outcome is not None:
            return
        waiter.outcome = Outcome("result" if "result" in message else "error", message)
        waiter.done.set()

    def _answer(self, request: Mapping[str, Any]) -> None:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
        if request.get("method") == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": _METHOD_NOT_FOUND, "message": "Method not found"}
        self._send(reply)


def start_child(
    devtools: Path,
    *,
    home: Path,
    fs_root: Path,
    search_path: str,
    starter: Callable[..., processes.ManagedProcess] = processes.start,
) -> tuple[Child, dict[str, Any]]:
    """Start chrome-devtools-mcp, initialize it as a client with no roots, list its tools.

    The child starts under ``env -i`` with nothing inherited, in ``fs_root``, as
    the leader of its own process group, and its errors go to this process's own
    standard error, where Claude Code keeps an MCP server's log. RelayError when it
    does not start, or does not answer ``initialize`` and ``tools/list`` within
    :data:`START_SECONDS`.
    """
    argv = devtools_argv(devtools, home=home, fs_root=fs_root, search_path=search_path)
    try:
        process = starter(
            argv,
            name=CHILD_NAME,
            env={},
            cwd=fs_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except OSError as error:
        raise RelayError(
            f"{CHILD_NAME} could not be started: {error.strerror or type(error).__name__}"
        ) from None
    child = Child(process)
    try:
        hello = child.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout=START_SECONDS,
        )
        if hello.kind != "result" or not isinstance(
            (hello.message or {}).get("result"), Mapping
        ):
            raise RelayError(f"{CHILD_NAME} did not answer initialize ({hello.kind})")
        child.notify("notifications/initialized")
        listed = child.request("tools/list", {}, timeout=START_SECONDS)
        result = (listed.message or {}).get("result")
        if listed.kind != "result" or not isinstance(result, Mapping):
            raise RelayError(f"{CHILD_NAME} did not answer tools/list ({listed.kind})")
    except BaseException:
        child.close()
        raise
    return child, dict(result)


class Relay:
    """The browser half of the ``ux-story`` server: twelve tools behind guard rails.

    ``child`` is None when chrome-devtools-mcp never started, in which case the
    relay serves nothing and the flow's journal already holds a ``child_died`` line.
    Calls arrive one at a time from the server's main thread; :meth:`begin` and
    :meth:`cancel` are the only methods another thread calls.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        budget: int,
        screens: Path,
        child: Child | None,
        tools: Mapping[str, Mapping[str, Any]] | None = None,
        call_seconds: float = CALL_SECONDS,
    ) -> None:
        self.ledger = ledger
        self.budget = budget
        self.screens = screens
        self.child = child
        self.call_seconds = call_seconds
        self._tools: dict[str, Mapping[str, Any]] = dict(tools or {})
        self.snapshot: Snapshot | None = None
        self.addresses: set[str] = set()
        self.dialog: tuple[str, str] | None = None
        self.over: str | None = None if child is not None else "child_died"
        self.reported = False
        self._fresher: Snapshot | None = None
        self._page_stale = True
        self._died_logged = child is None
        self._marked_out_of_steps = False
        self._screens_saved = 0
        self._cancelled = threading.Event()
        self._stop: Callable[[], bool] = lambda: False

    def stop_when(self, stop: Callable[[], bool]) -> None:
        """Give up waiting on the browser as soon as ``stop`` is true, as for a signal."""
        self._stop = stop

    def tools(self) -> list[dict[str, Any]]:
        """The tool definitions the relay serves, in STORY_BROWSER_TOOLS order."""
        return [json.loads(json.dumps(tool)) for tool in self._tools.values()]

    def serves(self, name: str) -> bool:
        """Whether ``name`` is one of the browser tools the relay serves."""
        return name in self._tools

    def seen_on_screen(self, ref: str) -> bool:
        """Whether ``ref`` is an address a snapshot the model was shown held."""
        return ref.strip() in self.addresses

    def begin(self) -> None:
        """A new request from the client is about to be handled."""
        self._cancelled.clear()

    def cancel(self) -> None:
        """The client cancelled the request being handled; stop waiting on the browser."""
        self._cancelled.set()

    def refuse_invalid(self, name: str) -> None:
        """Journal a call to ``name`` whose arguments were not an object."""
        self.ledger.call(tool=name, cls=_class_of(name), ok=False, refused="invalid")

    def settle(self) -> None:
        """Before the journal ends: record a child that died while nothing was asked of it."""
        child = self.child
        if child is not None and not child.alive and not child.closing:
            self._died(CHILD_NAME)

    def close(self) -> None:
        """Stop chrome-devtools-mcp."""
        if self.child is not None:
            self.child.close()

    def end_actions(self) -> None:
        """The flow has reported: refuse every action from now on, and keep the looks."""
        self.reported = True

    def call(self, name: str, arguments: Mapping[str, Any]) -> Reply:
        """Run one relayed tool through the guard rails, journalling it once."""
        cls = _class_of(name)
        schema = self._tools[name]["inputSchema"]
        forward, args, problem = read_arguments(schema, arguments)
        if problem:
            return self._refuse(name, cls, "invalid", problem, args=args)
        if self.over is not None:
            return self._refuse(name, cls, "session_end", SESSION_OVER_TEXT, args=args)
        if self.reported and cls == "action":
            return self._refuse(
                name, "guard", "terminal_done", REPORTED_TEXT, args=args
            )
        guarded = self._guard(name, forward)
        if guarded is not None:
            refused, label, text = guarded
            return self._refuse(name, "guard", refused, text, args=args, label=label)
        if cls == "action" and self.ledger.actions_used >= self.budget:
            return self._out_of_steps(name, args)
        if name == "take_screenshot" and self.ledger.images_sent >= IMAGE_CAP:
            return self._refuse(name, cls, "image_cap", NO_SCREENSHOTS_TEXT, args=args)
        if cls == "look" and self.ledger.looks >= LOOKS_PER_ACTION * self.budget:
            return self._refuse(name, cls, "limit", NO_LOOKS_TEXT, args=args)
        if name == "take_screenshot":
            forward = {
                **forward,
                "format": SCREENSHOT_FORMAT,
                "quality": SCREENSHOT_QUALITY,
            }
        if cls == "look":
            self.ledger.looks += 1
        outcome = self._ask(name, forward)
        if outcome.kind not in ("result", "error"):
            return self._failed(name, cls, args, outcome)
        return self._answered(name, cls, args, outcome)

    def _guard(
        self, name: str, forward: Mapping[str, Any]
    ) -> tuple[str, str | None, str] | None:
        if name == "click":
            return self._guard_click(forward.get("uid"))
        if name == "handle_dialog":
            if forward.get("action") != "accept":
                return None
            if self.dialog is None:
                return "invalid", None, NO_DIALOG_TEXT
            kind, message = self.dialog
            if kind in CONFIRMING_DIALOGS:
                return "commit_point", message or kind, COMMIT_POINT_TEXT
            return None
        keys = _keys_of(name, forward)
        if name in EDITING_TOOLS or any(key not in FOCUS_KEYS for key in keys):
            edited = self._guard_edit()
            if edited is not None:
                return edited
        return self._guard_keys(keys)

    def _guard_click(self, uid: object) -> tuple[str, str | None, str] | None:
        snapshot = self.snapshot
        if snapshot is None or not isinstance(uid, str):
            return "invalid", None, STALE_UID_TEXT
        if not any(index in snapshot.shown for index in snapshot.indices(uid)):
            return "invalid", None, STALE_UID_TEXT
        found = snapshot.control_of(uid)
        if found is None:
            page = self._current_page()
            if page is None:
                return "invalid", None, self._unread_text()
            if page is not snapshot:
                if not page.indices(uid):
                    return "invalid", None, PAGE_CHANGED_TEXT
                found = page.control_of(uid)
            if found is None:
                found = page.sets_a_value(uid)
        return None if found is None else _refusal(found)

    def _guard_edit(self) -> tuple[str, str | None, str] | None:
        page = self._current_page()
        if page is None:
            return "invalid", None, self._unread_text()
        marker = page.autosave_control
        return None if marker is None else ("commit_point", marker, COMMIT_POINT_TEXT)

    def _guard_keys(self, keys: Sequence[str]) -> tuple[str, str | None, str] | None:
        pressing = [key for key in keys if key in PRESSING_KEYS]
        if not pressing:
            return None
        page = self._current_page()
        if page is None:
            return "invalid", None, self._unread_text()
        for key in pressing:
            found = page.pressed(key)
            if found is not None:
                return _refusal(found)
        return None

    def _unread_text(self) -> str:
        return BROWSER_GONE_TEXT if self.over == "child_died" else PAGE_UNREAD_TEXT

    def _current_page(self) -> Snapshot | None:
        """The page as it is now: the latest snapshot, read again if anything ran since.

        The read is the relay's own and is neither shown nor journalled. None when
        the page cannot be read; a child found gone is journalled as having died.
        """
        if not self._page_stale:
            return self._fresher or self.snapshot
        outcome = self._ask("take_snapshot", {})
        if outcome.kind == "gone":
            self._died("take_snapshot")
            return None
        if outcome.kind != "result":
            return None
        texts, _, is_error = _content_of(outcome.message or {})
        if is_error:
            return None
        self.dialog = open_dialog(texts)
        found = next(
            (parts for parts in map(split_section, texts) if parts is not None), None
        )
        if found is None:
            return None
        self._fresher = cut_snapshot(found[1])
        self._page_stale = False
        return self._fresher

    def _out_of_steps(self, name: str, args: Mapping[str, Any]) -> Reply:
        first = not self._marked_out_of_steps
        self._marked_out_of_steps = True
        self.ledger.call(
            tool=name,
            cls="budget" if first else "action",
            ok=False,
            refused="budget",
            label="out_of_steps" if first else None,
            args=args,
        )
        mark = {"label": OUT_OF_STEPS_MARK, "say": OUT_OF_STEPS} if first else None
        return Reply((_text(OUT_OF_STEPS),), True, mark)

    def _refuse(
        self,
        name: str,
        cls: str,
        refused: str,
        text: str,
        *,
        args: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> Reply:
        self.ledger.call(
            tool=name, cls=cls, ok=False, refused=refused, label=label, args=args
        )
        return Reply((_text(text),), True)

    def _ask(self, name: str, arguments: Mapping[str, Any]) -> Outcome:
        child = self.child
        if child is None:
            return Outcome("gone")
        return child.request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout=self.call_seconds,
            stop=lambda: self._cancelled.is_set() or self._stop(),
        )

    def _failed(
        self, name: str, cls: str, args: Mapping[str, Any], outcome: Outcome
    ) -> Reply:
        self._page_stale = True
        counted = cls == "action"
        refused = "size" if outcome.kind == "size" else None
        self.ledger.call(
            tool=name, cls=cls, ok=False, refused=refused, counted=counted, args=args
        )
        if outcome.kind == "gone":
            self._died(name)
            return Reply((_text(BROWSER_GONE_TEXT),), True)
        text = {
            "size": TOO_LARGE_TEXT,
            "timeout": TIMED_OUT_TEXT,
        }.get(outcome.kind, CANCELLED_TEXT)
        return Reply((_text(text),), True)

    def _answered(
        self, name: str, cls: str, args: Mapping[str, Any], outcome: Outcome
    ) -> Reply:
        message = outcome.message or {}
        texts, images, is_error = _content_of(message)
        self.dialog = open_dialog(texts)
        texts, snapshot = self._relay_snapshots(texts)
        if snapshot is None and name != "take_screenshot":
            self._page_stale = True
        counted = cls == "action"
        if snapshot is not None and snapshot.signed_out:
            self.ledger.call(
                tool=name, cls=cls, ok=not is_error, counted=counted, args=args
            )
            self.ledger.call(tool=name, cls="harness", ok=False, label="signed_out")
            self.over = "signed_out"
            return Reply((_text(SIGNED_OUT_TEXT),), True)
        screen: str | None = None
        shown: list[dict[str, str]] = []
        ok = not is_error
        if name == "take_screenshot" and images:
            data = jpeg_bytes(images[0])
            if data is None:
                texts.append(SCREENSHOT_UNUSABLE_TEXT)
                ok = False
            else:
                screen = self._save(data)
                self.ledger.count_image()
                shown.append(_image(images[0]))
        elif counted and not is_error:
            screen, attached = self._after_action()
            if attached is not None:
                shown.append(attached)
        self.ledger.call(
            tool=name, cls=cls, ok=ok, counted=counted, args=args, screen=screen
        )
        if self.over == "child_died":
            self._died(name)
            return Reply((_text(BROWSER_GONE_TEXT),), True)
        content = [_text(text) for text in _bounded_texts(texts)] + shown
        return Reply(tuple(content), is_error or not ok)

    def _relay_snapshots(self, texts: list[str]) -> tuple[list[str], Snapshot | None]:
        latest: Snapshot | None = None
        out: list[str] = []
        for text in texts:
            parts = split_section(text)
            if parts is None:
                out.append(text)
                continue
            before, lines, after = parts
            latest = cut_snapshot(lines)
            out.append("\n".join(part for part in (before, latest.text, after) if part))
        if latest is not None:
            self.snapshot = latest
            self.addresses |= latest.addresses
            self._fresher = None
            self._page_stale = False
        return out, latest

    def _after_action(self) -> tuple[str | None, dict[str, str] | None]:
        outcome = self._ask(
            "take_screenshot",
            {"format": SCREENSHOT_FORMAT, "quality": SCREENSHOT_QUALITY},
        )
        if outcome.kind == "gone":
            self.over = "child_died"
            return None, None
        if outcome.kind != "result":
            return None, None
        texts, images, is_error = _content_of(outcome.message or {})
        self.dialog = open_dialog(texts)
        if is_error or not images:
            return None, None
        data = jpeg_bytes(images[0])
        if data is None:
            return None, None
        screen = self._save(data)
        if self.ledger.images_sent >= IMAGE_CAP:
            return screen, None
        self.ledger.count_image()
        return screen, _image(images[0])

    def _save(self, data: bytes) -> str | None:
        if self._screens_saved >= self.budget + SCREENS_OVER_BUDGET:
            return None
        name = f"{self._screens_saved + 1:03d}.jpg"
        try:
            descriptor = os.open(
                self.screens / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            return None
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
        except OSError:
            return None
        finally:
            os.close(descriptor)
        self._screens_saved += 1
        return f"screens/{name}"

    def _died(self, tool: str) -> None:
        self.over = "child_died"
        if self._died_logged:
            return
        self._died_logged = True
        self.ledger.call(tool=tool, cls="harness", ok=False, label="child_died")


def _class_of(name: str) -> str:
    return "look" if name in tool_profiles.STORY_LOOK_TOOLS else "action"


def _refusal(found: tuple[str, str]) -> tuple[str, str | None, str]:
    refused, label = found
    text = SESSION_END_TEXT if refused == "session_end" else COMMIT_POINT_TEXT
    return refused, label, text


def pressed_key(key: str) -> str:
    """The key a combination presses, casefolded, as the pinned server parses it.

    The server splits on ``+`` and presses the last key while it holds the others,
    so ``"Control+Enter"`` presses Enter and ``"Control++"`` presses ``+``.
    """
    tokens: list[str] = []
    current = ""
    for character in key:
        if character == "+" and current:
            tokens.append(current)
            current = ""
        else:
            current += character
    if current:
        tokens.append(current)
    return tokens[-1].casefold() if tokens else ""


def _keys_of(name: str, forward: Mapping[str, Any]) -> list[str]:
    if name == "press_key":
        key = forward.get("key")
        return [pressed_key(key)] if isinstance(key, str) else []
    if name == "type_text":
        return typed_keys(forward)
    return []


def typed_keys(arguments: Mapping[str, Any]) -> list[str]:
    """The keys a ``type_text`` call presses that could press or submit something.

    The pinned server types each character that is a key on its keyboard by
    pressing that key, so a line break presses Enter and a space presses Space;
    ``submitKey`` is pressed after the text.
    """
    keys: list[str] = []
    text = arguments.get("text")
    if isinstance(text, str) and ("\n" in text or "\r" in text):
        keys.append("enter")
    if isinstance(text, str) and " " in text:
        keys.append(" ")
    submit = arguments.get("submitKey")
    if isinstance(submit, str):
        keys.append(pressed_key(submit))
    return keys


def _image(image: Mapping[str, Any]) -> dict[str, str]:
    return {"type": "image", "mimeType": "image/jpeg", "data": str(image["data"])}


def _content_of(
    message: Mapping[str, Any],
) -> tuple[list[str], list[Mapping[str, Any]], bool]:
    """The text items, the image items and whether the child's answer is an error."""
    if "error" in message:
        error = message.get("error")
        detail = error.get("message") if isinstance(error, Mapping) else None
        shown = " ".join(str(detail or "no detail").split())[:300]
        return [f"The browser tool refused the call: {shown}"], [], True
    result = message.get("result")
    if not isinstance(result, Mapping):
        return [], [], True
    content = result.get("content")
    texts: list[str] = []
    images: list[Mapping[str, Any]] = []
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
        elif item.get("type") == "image":
            images.append(item)
    return texts, images, result.get("isError") is True


def _bounded_texts(texts: Sequence[str]) -> list[str]:
    left = TEXT_CHARS
    out: list[str] = []
    for text in texts:
        if left <= 0:
            break
        if len(text) > left:
            out.append(text[:left] + "\n" + TEXT_CUT_TEXT.format(limit=TEXT_CHARS))
            left = 0
            continue
        out.append(text)
        left -= len(text)
    return out


def start(
    devtools: Path,
    *,
    ledger: Ledger,
    budget: int,
    out: Path,
    environ: Mapping[str, str],
    starter: Callable[..., processes.ManagedProcess] = processes.start,
    say: Callable[[str], None] = lambda message: None,
) -> Relay:
    """The relay for one flow, with chrome-devtools-mcp started and listed.

    ``out`` is the server's own directory: screenshots go to ``out/screens`` and
    the child's filesystem root is the empty ``out/devtools-root``, both created
    here, private. The child's ``PATH`` and ``HOME`` are this server's own, which
    ``agent.story_server`` sets. A child that cannot be started or listed is a
    ``child_died`` line, never an exception: the relay then serves no tool, and the
    flow is judged a harness error.
    """
    screens = out / "screens"
    root = out / "devtools-root"
    try:
        screens.mkdir(mode=0o700)
        root.mkdir(mode=0o700)
        child, listed = start_child(
            devtools,
            home=Path(environ.get("HOME") or root),
            fs_root=root,
            search_path=environ.get("PATH") or agent.DEFAULT_SEARCH_PATH,
            starter=starter,
        )
    except (OSError, RelayError) as error:
        say(f"the browser relay could not start: {error}")
        relay = Relay(ledger, budget=budget, screens=screens, child=None)
        ledger.call(tool=CHILD_NAME, cls="harness", ok=False, label="child_died")
        return relay
    return Relay(
        ledger, budget=budget, screens=screens, child=child, tools=relayed_tools(listed)
    )
