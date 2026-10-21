# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The seams between the first-time-user story modules, checked from both sides.

Each module has a suite of its own, and each of those tests one side of a boundary
against a fake or a copy of the other. This file runs the real modules against
each other, so a change on one side that its own suite accepts but the other side
does not read fails here:

* the journal ``ux_story_mcp`` writes is, line for line and field for field, what
  ``story_verdict.read_journal`` reads, in both modes, at every end reason, and up
  to the writer's own bounds, and the judge reads a served session as its report
  says;
* the docs index answers in the shapes the server serves, within the bounds the
  server's tool schemas advertise;
* every flow of the real catalogue produces a start record the server takes, a
  journal the judge reads, and a flow context the run record round-trips;
* a vocabulary that two modules each hold stays equal in both;
* each module imports only what the module graph gives it.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_story_mcp as story  # noqa: E402
from ux_runner import (  # noqa: E402
    docs_index,
    section,
    stories,
    story_verdict,
)

HARNESS = "f23669def653f7c3a9606841cfc178c5456c1e8f"
EPOCH = 1760961234.5
SEEN_ON_SCREEN = "/a-help-link-the-screen-showed/"

INPUTS = stories.SeriesInputs(
    model="us.anthropic.claude-opus-5-5",
    effort="high",
    prompt_revision=1,
    judgement_revision=story_verdict.JUDGEMENT_REVISION,
    relay_revision=0,
    claude_code="2.1.281",
    chrome_devtools_mcp="1.10.1",
    chrome="141.0.7390.54",
    brief_sha256="a" * 64,
    ci_contract_sha256="b" * 64,
)

PAGES = {
    "configuration-profiles.md": (
        "# Configuration Profiles\n\nProfiles hold configuration.\n\n"
        "## Prefix mappings\n\nA prefix mapping assigns a profile to every document "
        "that lands under a prefix.\n\n## Long reference\n\n"
        + " ".join(f"entry{n}" for n in range(1500))
        + "\n"
    ),
    "web-ui.md": (
        "# Web UI\n\nThe web application.\n\n## Upload documents\n\n"
        "Send a document in.\n"
    ),
}
PREFIX_ID = "configuration-profiles.md#prefix-mappings"
LONG_ADDRESS = "/configuration-profiles/#long-reference"

START_LINE_KEYS = [
    "v",
    "type",
    "seq",
    "t",
    *story.START_KEYS[: story.START_KEYS.index("attempt")],
    "budget",
    "relay",
    *story.START_KEYS[story.START_KEYS.index("attempt") :],
    "epoch",
]
CALL_LINE_KEYS = [
    "v",
    "type",
    "seq",
    "t",
    "tool",
    "class",
    "counted",
    "actions_used",
    "images_sent",
    "ok",
    "refused",
    "label",
    "args",
    "result",
    "terminal",
    "after_budget",
]
END_LINE_KEYS = ["v", "type", "seq", "t", "reason"]


class Clock:
    """A monotonic clock that moves one second each time it is read."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@pytest.fixture(scope="module")
def catalogue() -> stories.Catalogue:
    return stories.load_catalogue()


@pytest.fixture
def index(tmp_path: Path) -> docs_index.Index:
    """A real index, built, saved and loaded back, as the server's ``--index`` is."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    for name, text in PAGES.items():
        (root / "docs" / name).write_text(text, encoding="utf-8")
    built = docs_index.build(
        root,
        source=docs_index.DocsSource(kind="worktree", ref=None, dirty=False),
        pages=sorted(PAGES),
    )
    return docs_index.load(docs_index.save(built, tmp_path / "index.json"))


def synthetic_start(
    *,
    expectation: str = "allowed",
    kind: str = "read",
    group: str = "Author",
    corpus_hash: str = "9e8d7c6b5a41",
) -> dict[str, Any]:
    return {
        "story": "seam-story",
        "story_hash": "0f3a9c2e71b4",
        "series": "5d0c1e9a2b7f",
        "persona": "seam-persona",
        "group": group,
        "expectation": expectation,
        "kind": kind,
        "attempt": 1,
        "docs_ref": "worktree",
        "corpus_hash": corpus_hash,
        "harness": HARNESS,
        "model": INPUTS.model,
        "effort": INPUTS.effort,
        "mode": "unattended",
    }


def catalogue_start(
    catalogue: stories.Catalogue,
    key: stories.FlowKey,
    *,
    corpus_hash: str,
    attempt: int = 1,
) -> dict[str, Any]:
    """The start record of one catalogue flow, built only from the loader's types."""
    found = catalogue.story(key.story)
    story_hash = stories.story_hash(catalogue, found)
    return {
        "story": found.id,
        "story_hash": story_hash,
        "series": stories.series_hash(story_hash, key.persona, INPUTS),
        "persona": key.persona,
        "group": catalogue.persona(key.persona).group,
        "expectation": found.expectation_for(key.persona),
        "kind": found.kind,
        "attempt": attempt,
        "docs_ref": "worktree",
        "corpus_hash": corpus_hash,
        "harness": HARNESS,
        "model": INPUTS.model,
        "effort": INPUTS.effort,
        "mode": "unattended",
    }


def catalogue_context(
    catalogue: stories.Catalogue,
    key: stories.FlowKey,
    start: Mapping[str, Any],
    **changes: Any,
) -> story_verdict.FlowContext:
    """The judge's context for one catalogue flow, built only from the loader's types."""
    found = catalogue.story(key.story)
    values: dict[str, Any] = {
        "key": str(key),
        "story": found.id,
        "persona": key.persona,
        "group": start["group"],
        "title": found.title,
        "expectation": start["expectation"],
        "kind": found.kind,
        "documented_steps": found.documented_steps,
        "needs": found.needs,
        "declared_docs": found.match.docs,
        "series": start["series"],
        "budget": stories.budget(catalogue.limits, found),
        "attempt": start["attempt"],
        "journal": story_verdict.journal_path(str(key), start["attempt"]),
        "audit_state": "clean",
        "session": session_outcome(),
    }
    values.update(changes)
    return story_verdict.FlowContext(**values)


def session_outcome(**changes: Any) -> story_verdict.SessionOutcome:
    values: dict[str, Any] = {
        "started_epoch": EPOCH - 5.0,
        "ended_epoch": EPOCH + 60.0,
        "timed_out": False,
        "returncode": 0,
        "stop": "end_turn",
        "is_error": False,
        "turns": 9,
        "duration_s": 65.0,
        "cost_usd": 0.5,
        "cost_usd_pinned": 0.6,
        "counted_usd": 0.6,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    values.update(changes)
    return story_verdict.SessionOutcome(**values)


def writer(
    path: Path,
    start: Mapping[str, Any],
    *,
    relay: bool = False,
    budget: int | None = None,
) -> story.JournalWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    return story.JournalWriter(
        path, start, relay=relay, budget=budget, clock=Clock(), wall=lambda: EPOCH
    )


def adapter(
    journal: story.JournalWriter,
    index: docs_index.Index,
    *,
    seen_on_screen: Callable[[str], bool] | None = None,
) -> story.StoryAdapter:
    """The server as ``main`` builds it, with no recorder running."""
    link = story.ux_recorder_mcp.RecorderLink(
        journal.path.parent / "no-recorder",
        send=lambda socket, payload: {"ok": True},
        read_current=lambda out_dir: None,
    )
    return story.StoryAdapter(
        link,
        docs=story.IndexLookup(index),
        journal=journal,
        seen_on_screen=seen_on_screen,
    )


def tools_call(
    server: story.StoryAdapter, name: Any, arguments: Any = None, *, omit: bool = False
) -> dict[str, Any]:
    """One ``tools/call`` through the JSON-RPC envelope, as a client sends it."""
    params: dict[str, Any] = {}
    if not omit:
        params["name"] = name
    params["arguments"] = {} if arguments is None else arguments
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    assert reply is not None
    return reply


def reply_text(reply: Mapping[str, Any]) -> dict[str, Any]:
    (content,) = reply["result"]["content"]
    return json.loads(content["text"])


def raw_lines(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def call_fields(call: story_verdict.JournalCall) -> tuple[Any, ...]:
    return (
        call.seq,
        call.t,
        call.tool,
        call.cls,
        call.counted,
        call.actions_used,
        call.images_sent,
        call.ok,
        call.refused,
        call.label,
        dict(call.args),
        call.ids,
        call.screen,
        call.terminal,
        call.after_budget,
    )


def raw_call_fields(raw: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        raw["seq"],
        raw["t"],
        raw["tool"],
        raw["class"],
        raw["counted"],
        raw["actions_used"],
        raw["images_sent"],
        raw["ok"],
        raw["refused"],
        raw["label"],
        raw["args"],
        tuple(raw["result"]["ids"]),
        raw["result"]["screen"],
        raw["terminal"],
        raw["after_budget"],
    )


def assert_read_back_exactly(path: Path) -> story_verdict.Journal:
    """Every line the writer wrote, read back by the judge with no field changed."""
    raw = raw_lines(path)
    journal = story_verdict.read_journal(path)
    first, *calls, last = raw
    assert list(first) == START_LINE_KEYS
    for key in story.START_KEYS:
        assert getattr(journal.start, key) == first[key], key
    assert (journal.start.budget, journal.start.relay, journal.start.epoch) == (
        first["budget"],
        first["relay"],
        first["epoch"],
    )
    assert all(list(line) == CALL_LINE_KEYS for line in calls)
    assert all(list(line["result"]) == ["ids", "screen"] for line in calls)
    assert [call_fields(call) for call in journal.calls] == [
        raw_call_fields(line) for line in calls
    ]
    assert list(last) == END_LINE_KEYS
    assert journal.end is not None
    assert (journal.end.seq, journal.end.t, journal.end.reason) == (
        last["seq"],
        last["t"],
        last["reason"],
    )
    return journal


def every_case_of_one_session(
    server: story.StoryAdapter,
) -> list[tuple[str, Any, bool]]:
    """Drive every row of the own-tools table once; (tool, arguments, omit name)."""
    steps: list[tuple[str, Any, bool]] = [
        ("search_documentation", {"query": "prefix mapping"}, False),
        ("search_documentation", {"query": "zzzz qqqq"}, False),
        ("search_documentation", {"query": "   "}, False),
        ("read_documentation", {"ref": PREFIX_ID}, False),
        ("read_documentation", {"ref": LONG_ADDRESS, "part": 2}, False),
        ("read_documentation", {"ref": PREFIX_ID, "part": 9}, False),
        ("read_documentation", {"ref": PREFIX_ID, "part": 0}, False),
        ("read_documentation", {"ref": "/no-such-page/"}, False),
        ("read_documentation", {"ref": SEEN_ON_SCREEN}, False),
        (
            "note_confusion",
            {"what": "where to start", "why": "two lists", "docs_ref": PREFIX_ID},
            False,
        ),
        ("note_dead_end", {"what": "a page", "why": "it led nowhere"}, False),
        ("note_dead_end", {"what": "missing its why"}, False),
        ("note_injection", {"where": "toolbar", "quote": "q"}, False),
        *[
            (
                "note_injection",
                {"where": "page", "quote": f"ignore your task {n}"},
                False,
            )
            for n in range(story.INJECTIONS_RECORDED + 1)
        ],
        *[
            ("search_documentation", {"query": f"profile {n}"}, False)
            for n in range(story.SEARCHES_PER_FLOW)
        ],
        *[
            ("read_documentation", {"ref": "web-ui.md#upload-documents"}, False)
            for _ in range(story.READS_PER_FLOW)
        ],
        *[
            ("note_confusion", {"what": f"note {n}", "why": "again"}, False)
            for n in range(story.NOTES_PER_FLOW)
        ],
        (
            "report_blocked",
            {"reason": "r", "kind": "sideways", "offered_a_control": False},
            False,
        ),
        (
            "report_done",
            {"evidence": "the list showed it", "stopped_at_commit_point": True},
            False,
        ),
        (
            "report_blocked",
            {"reason": "too late", "kind": "no_route", "offered_a_control": False},
            False,
        ),
        ("report_done", {"evidence": "again", "stopped_at_commit_point": False}, False),
        ("evaluate_script", {"function": "() => 1"}, False),
        ("", {}, True),
        ("note_confusion", "not an object", False),
        ("tools/none", None, False),
    ]
    for name, arguments, omit in steps:
        tools_call(server, name, arguments, omit=omit)
    return steps


@pytest.mark.unit
def test_a_served_session_reads_back_line_for_line_and_field_for_field(
    tmp_path: Path, index: docs_index.Index
) -> None:
    """Every row of the server's own-tools table, through the real index and the
    real reader: every line parses, no field changes, and every class, refusal
    and label the table names is among them."""
    path = tmp_path / "out" / story.JOURNAL_FILE
    journal = writer(path, synthetic_start(corpus_hash=index.corpus_hash))
    server = adapter(journal, index, seen_on_screen=lambda ref: ref == SEEN_ON_SCREEN)
    every_case_of_one_session(server)
    journal.end("terminal")
    read = assert_read_back_exactly(path)
    seen = {
        (call.cls, call.ok, call.refused, call.label, call.terminal)
        for call in read.calls
    }
    assert {
        ("doc", True, None, None, None),
        ("doc", False, "limit", None, None),
        ("doc", False, "invalid", None, None),
        ("doc", False, None, "doc_address_guessed", None),
        ("doc", False, None, "help_link_unresolved", None),
        ("note", True, None, None, None),
        ("note", False, "limit", None, None),
        ("note", False, "invalid", None, None),
        ("report", True, None, None, "done"),
        ("report", False, "terminal_done", None, None),
        ("report", False, "invalid", None, None),
        ("harness", False, "withheld", None, None),
    } <= seen
    assert {call.tool for call in read.calls if call.cls == "harness"} == {
        "evaluate_script",
        "",
        "tools/none",
    }
    assert [call.terminal for call in read.calls if call.terminal] == ["done"]


@pytest.mark.unit
def test_the_judge_summarises_a_served_session_from_what_the_server_counted(
    tmp_path: Path, index: docs_index.Index
) -> None:
    """What the server counted and what the judge derives from its journal agree,
    and a section id the index returned is the form a story declares its docs in.
    The judge keeps an address the screen showed as display text, already
    sanitised, so a leading ``/`` reads back escaped."""
    path = tmp_path / "out" / story.JOURNAL_FILE
    start = synthetic_start(corpus_hash=index.corpus_hash)
    journal = writer(path, start)
    server = adapter(journal, index, seen_on_screen=lambda ref: ref == SEEN_ON_SCREEN)
    every_case_of_one_session(server)
    journal.end("terminal")
    read = story_verdict.read_journal(path)
    context = story_verdict.FlowContext(
        key="seam-story@seam-persona",
        story="seam-story",
        persona="seam-persona",
        group="Author",
        title="A seam",
        expectation="allowed",
        kind="read",
        documented_steps=3,
        needs=(),
        declared_docs=(PREFIX_ID,),
        series=start["series"],
        budget=30,
        journal=story_verdict.journal_path("seam-story@seam-persona", 1),
        audit_state="clean",
        session=session_outcome(),
    )
    verdict = story_verdict.judge(read, context)
    assert (verdict.verdict, verdict.flags, verdict.met_expectation) == (
        "done",
        ("stopped_at_commit_point",),
        True,
    )
    result = story_verdict.summarize(read, context, verdict)
    assert result.searches == server.searches
    assert result.reads == sum(
        1 for c in read.calls if c.tool == "read_documentation" and c.ok
    )
    assert len(result.notes.confusion) == sum(
        1 for c in read.calls if c.tool == "note_confusion" and c.ok
    )
    assert len(result.notes.dead_end) == 1
    assert result.notes.injections == sum(
        1 for c in read.calls if c.tool == "note_injection" and c.refused != "invalid"
    )
    assert result.help_links_unresolved == (
        section.sanitize(SEEN_ON_SCREEN, story.REF_CHARS),
    )
    assert result.doc_addresses_guessed == 1
    assert result.declared_docs_found is True
    assert result.terminal is not None and result.terminal.type == "done"
    assert (result.actions_used, result.budget, result.looks) == (None, None, None)
    unfound = dataclasses.replace(context, declared_docs=("web-ui.md#nowhere",))
    assert story_verdict.summarize(read, unfound, verdict).declared_docs_found is False


REPORTS: list[
    tuple[str, str, tuple[str, ...], str | None, dict[str, Any], tuple[Any, ...]]
] = [
    (
        "allowed",
        "commit_point",
        (),
        "report_done",
        {"evidence": "e", "stopped_at_commit_point": True},
        ("done", None, ("stopped_at_commit_point",), True),
    ),
    (
        "allowed",
        "read",
        (),
        "report_done",
        {"evidence": "e", "stopped_at_commit_point": False},
        ("done", None, (), True),
    ),
    (
        "allowed",
        "read",
        (),
        "report_blocked",
        {"reason": "r", "kind": "no_route", "offered_a_control": False},
        ("stuck", "no_route", (), False),
    ),
    (
        "allowed",
        "commit_point",
        (),
        "report_blocked",
        {"reason": "r", "kind": "refused", "offered_a_control": True},
        ("stuck", "refused", (), False),
    ),
    (
        "allowed",
        "read",
        (),
        "report_blocked",
        {"reason": "r", "kind": "precondition", "offered_a_control": False},
        ("stuck", "precondition", (), False),
    ),
    (
        "allowed",
        "read",
        ("complete-document",),
        "report_blocked",
        {"reason": "r", "kind": "precondition", "offered_a_control": False},
        ("not_run", "precondition", (), None),
    ),
    (
        "not_offered",
        "commit_point",
        (),
        "report_blocked",
        {"reason": "r", "kind": "refused", "offered_a_control": True},
        ("offered", None, (), False),
    ),
    (
        "not_offered",
        "commit_point",
        (),
        "report_blocked",
        {"reason": "r", "kind": "no_route", "offered_a_control": False},
        ("refused_as_expected", None, (), True),
    ),
    (
        "not_offered",
        "commit_point",
        (),
        "report_done",
        {"evidence": "e", "stopped_at_commit_point": True},
        ("offered", None, ("unproven",), False),
    ),
    (
        "refused",
        "read",
        (),
        "report_done",
        {"evidence": "e", "stopped_at_commit_point": False},
        ("not_refused", None, (), False),
    ),
    (
        "refused",
        "read",
        (),
        "report_blocked",
        {"reason": "r", "kind": "refused", "offered_a_control": False},
        ("refused_as_expected", None, (), True),
    ),
    ("allowed", "read", (), None, {}, ("unclear", None, (), None)),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "expectation, kind, needs, report, arguments, expected",
    REPORTS,
    ids=[f"{row[0]}-{row[1]}-{row[3]}-{i}" for i, row in enumerate(REPORTS)],
)
def test_the_judge_reads_a_served_report_as_the_report_says(
    tmp_path: Path,
    index: docs_index.Index,
    expectation: str,
    kind: str,
    needs: tuple[str, ...],
    report: str | None,
    arguments: dict[str, Any],
    expected: tuple[Any, ...],
) -> None:
    path = tmp_path / "out" / story.JOURNAL_FILE
    start = synthetic_start(expectation=expectation, kind=kind)
    journal = writer(path, start)
    server = adapter(journal, index)
    tools_call(server, "search_documentation", {"query": "prefix mapping"})
    if report is not None:
        reply = tools_call(server, report, arguments)
        assert reply["result"]["isError"] is False
    journal.end("terminal" if report else "eof")
    context = story_verdict.FlowContext(
        key="seam-story@seam-persona",
        story="seam-story",
        persona="seam-persona",
        group="Author",
        title="A seam",
        expectation=expectation,
        kind=kind,
        documented_steps=3,
        needs=needs,
        declared_docs=(),
        series=start["series"],
        budget=30,
        journal=story_verdict.journal_path("seam-story@seam-persona", 1),
        audit_state="clean",
        session=session_outcome(),
    )
    verdict = story_verdict.judge(story_verdict.read_journal(path), context)
    assert (
        verdict.verdict,
        verdict.sub_kind,
        verdict.flags,
        verdict.met_expectation,
    ) == expected


@pytest.mark.unit
@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize("reason", story.END_REASONS)
def test_every_end_reason_the_writer_takes_is_one_the_reader_takes(
    tmp_path: Path, relay: bool, reason: str
) -> None:
    path = tmp_path / story.JOURNAL_FILE
    journal = writer(path, synthetic_start(), relay=relay, budget=40 if relay else None)
    journal.end(reason)
    journal.end("eof")
    read = assert_read_back_exactly(path)
    assert read.end is not None and read.end.reason == reason
    assert (read.start.relay, read.start.budget) == (relay, 40 if relay else None)
    assert story_verdict.END_REASONS == story.END_REASONS


@pytest.mark.unit
def test_a_journal_the_writer_fills_by_lines_is_still_one_the_reader_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / story.JOURNAL_FILE
    journal = writer(path, synthetic_start())
    written = 0
    with pytest.raises(story.JournalClosed):
        while True:
            journal.call(
                tool="search_documentation",
                cls="doc",
                ok=True,
                args={"query": "q"},
                ids=[PREFIX_ID],
            )
            written += 1
    journal.end("eof")
    read = story_verdict.read_journal(path)
    assert len(read.calls) == written
    assert len(read.calls) + 2 == story_verdict.MAX_JOURNAL_LINES
    assert read.end is not None and read.end.reason == "eof"


@pytest.mark.unit
def test_a_journal_the_writer_fills_by_bytes_is_still_one_the_reader_reads(
    tmp_path: Path,
) -> None:
    """Wide characters, so the byte bound is reached long before the line bound."""
    path = tmp_path / story.JOURNAL_FILE
    journal = writer(path, synthetic_start())
    arguments = {f"field_{n}": "€" * 600 for n in range(8)}
    with pytest.raises(story.JournalClosed):
        while True:
            journal.call(tool="note_confusion", cls="note", ok=True, args=arguments)
    journal.end("terminal")
    size = path.stat().st_size
    assert story_verdict.MAX_JOURNAL_BYTES - 64 * 1024 < size
    assert size <= story_verdict.MAX_JOURNAL_BYTES
    read = story_verdict.read_journal(path)
    assert read.end is not None and read.end.reason == "terminal"
    assert all(dict(call.args) == arguments for call in read.calls)


RELAY_CALLS: list[dict[str, Any]] = [
    {
        "tool": "take_snapshot",
        "cls": "look",
        "ok": True,
        "actions_used": 0,
        "images_sent": 0,
    },
    {
        "tool": "click",
        "cls": "action",
        "ok": True,
        "counted": True,
        "actions_used": 1,
        "images_sent": 1,
        "screen": "screens/001.jpg",
    },
    {
        "tool": "click",
        "cls": "guard",
        "ok": False,
        "refused": "commit_point",
        "label": "Save the mapping",
        "actions_used": 1,
        "images_sent": 1,
    },
    {
        "tool": "search_documentation",
        "cls": "doc",
        "ok": True,
        "args": {"query": "prefix"},
        "ids": [PREFIX_ID],
        "actions_used": 1,
        "images_sent": 1,
    },
    {
        "tool": "read_documentation",
        "cls": "doc",
        "ok": False,
        "label": "help_link_unresolved",
        "args": {"ref": SEEN_ON_SCREEN, "part": 1},
        "actions_used": 1,
        "images_sent": 1,
    },
    {
        "tool": "evaluate_script",
        "cls": "harness",
        "ok": False,
        "refused": "withheld",
        "actions_used": 1,
        "images_sent": 1,
    },
    {
        "tool": "fill",
        "cls": "budget",
        "ok": False,
        "refused": "budget",
        "label": "out_of_steps",
        "actions_used": 40,
        "images_sent": 2,
    },
    {
        "tool": "report_done",
        "cls": "report",
        "ok": True,
        "args": {"evidence": "e", "stopped_at_commit_point": False},
        "terminal": "done",
        "after_budget": True,
        "actions_used": 40,
        "images_sent": 2,
    },
]


@pytest.mark.unit
def test_relay_lines_read_back_field_for_field(tmp_path: Path) -> None:
    """The lines a relay will write through the same writer: counts, a screen, a
    guard, the budget marker and an after-budget terminal."""
    path = tmp_path / story.JOURNAL_FILE
    journal = writer(path, synthetic_start(kind="commit_point"), relay=True, budget=40)
    for call in RELAY_CALLS:
        journal.call(**call)
    journal.end("refusal_limit")
    read = assert_read_back_exactly(path)
    assert [call.cls for call in read.calls] == [c["cls"] for c in RELAY_CALLS]
    assert read.calls[1].screen == "screens/001.jpg"
    assert read.calls[-1].after_budget is True


@pytest.mark.unit
def test_the_index_answers_in_the_shapes_and_bounds_the_server_serves(
    tmp_path: Path, index: docs_index.Index
) -> None:
    lookup = story.IndexLookup(index)
    hits = lookup.search("prefix mapping", limit=story._SEARCH_RESULTS)
    assert hits and all(
        list(hit) == ["id", "title", "address", "excerpt"] for hit in hits
    )
    assert all(docs_index.resolve(index, hit["id"]) == hit["id"] for hit in hits)
    assert all(docs_index.resolve(index, hit["address"]) == hit["id"] for hit in hits)
    found = lookup.read(LONG_ADDRESS, part=1)
    assert found is not None
    assert list(found) == ["id", "title", "address", "text", "part", "parts", "related"]
    assert found["parts"] >= 2 and found["part"] == 1
    assert len(found["text"]) <= docs_index.READ_PART_CHARS
    assert all(list(item) == ["id", "title", "address"] for item in found["related"])
    assert lookup.read("/no-such-page/", part=1) is None
    with pytest.raises(story.PartError) as raised:
        lookup.read(LONG_ADDRESS, part=found["parts"] + 1)
    assert raised.value.parts == found["parts"]

    journal = writer(tmp_path / "out" / story.JOURNAL_FILE, synthetic_start())
    server = adapter(journal, index)
    searched = reply_text(
        tools_call(server, "search_documentation", {"query": "prefix"})
    )
    assert list(searched) == ["results", "searches_left"]
    assert searched["results"] == lookup.search("prefix", limit=story._SEARCH_RESULTS)
    read = reply_text(tools_call(server, "read_documentation", {"ref": LONG_ADDRESS}))
    assert list(read) == [*found, "reads_left"]
    assert {key: read[key] for key in found} == found
    missed = reply_text(tools_call(server, "read_documentation", {"ref": "/nowhere/"}))
    assert missed == {"ok": False, "error": docs_index.NO_PAGE}


@pytest.mark.unit
def test_the_server_advertises_the_index_s_own_bounds() -> None:
    schemas = {tool["name"]: tool["inputSchema"]["properties"] for tool in story.TOOLS}
    assert story.NO_PAGE_TEXT == docs_index.NO_PAGE
    assert story.QUERY_CHARS == docs_index.QUERY_CHARS
    assert story.REF_CHARS == docs_index.REF_CHARS
    assert (
        schemas["search_documentation"]["query"]["maxLength"] == docs_index.QUERY_CHARS
    )
    assert schemas["read_documentation"]["ref"]["maxLength"] == docs_index.REF_CHARS
    assert schemas["read_documentation"]["part"]["maximum"] == docs_index.MAX_PARTS
    assert story._SEARCH_RESULTS == docs_index.SEARCH_LIMIT
    assert story._RELATED_RESULTS == docs_index.RELATED_LIMIT


@pytest.mark.unit
def test_every_catalogue_flow_starts_a_journal_the_judge_reads_and_a_run_record(
    tmp_path: Path, catalogue: stories.Catalogue
) -> None:
    """For every flow of both tiers: the start record the loader's types give is one
    the server's ``--start`` takes, its journal start line is the judge's, and the
    judge's flow context for it survives story-run.json."""
    keys = stories.flow_keys(catalogue, tiers=stories.TIERS)
    assert len(keys) == len({(s.id, p) for s in catalogue.stories for p in s.personas})
    for number, key in enumerate(keys):
        start = catalogue_start(catalogue, key, corpus_hash="9e8d7c6b5a41")
        record = tmp_path / f"start-{number}.json"
        record.write_text(json.dumps(start), encoding="utf-8")
        taken = story.start_record(record)
        assert taken == start
        path = tmp_path / str(key) / story.JOURNAL_FILE
        writer(path, taken).end("eof")
        read = story_verdict.read_journal(path)
        assert {name: getattr(read.start, name) for name in story.START_KEYS} == start
        context = catalogue_context(catalogue, key, start)
        assert story_verdict.FlowContext.from_json(context.to_json()) == context
        assert stories.FlowKey.parse(context.key) == key
        verdict = story_verdict.judge(read, context)
        assert verdict.verdict == "unclear"
    rerun = catalogue_context(
        catalogue,
        keys[0],
        catalogue_start(catalogue, keys[0], corpus_hash="9e8d7c6b5a41", attempt=2),
    )
    assert rerun.journal == f"flows/{keys[0]}/attempt-2/journal.jsonl"
    assert story_verdict.FlowContext.from_json(rerun.to_json()) == rerun


@pytest.mark.unit
def test_the_vocabularies_held_twice_are_equal() -> None:
    """The server and the judge each keep the catalogue's value sets and the
    journal's shapes, so writer and reader stay apart; they must not drift."""
    assert (
        story._SIGN_IN_GROUPS == story_verdict._SIGN_IN_GROUPS == stories.SIGN_IN_GROUPS
    )
    assert story._EXPECTATIONS == story_verdict._EXPECTATIONS == stories.EXPECTATIONS
    assert story._KINDS == story_verdict._KINDS == stories.KINDS
    assert story._ID_RE.pattern == story_verdict._ID_RE.pattern == stories.ID_RE.pattern
    assert story._DOC_MISS_LABELS == story_verdict.DOC_MISS_LABELS
    assert story._HARNESS_LABELS == story_verdict.HARNESS_LABELS
    for name in (
        "JOURNAL_CLASSES",
        "REFUSALS",
        "END_REASONS",
        "SESSION_MODES",
        "INJECTION_PLACES",
        "BLOCKED_KINDS",
    ):
        assert getattr(story, name) == getattr(story_verdict, name), name
    samples = {
        "_HASH_RE": ["0f3a9c2e71b4", "0F3A9C2E71B4", "0f3a9c2e71b", "0f3a9c2e71b45"],
        "_DOCS_REF_RE": [
            HARNESS,
            "worktree",
            "worktree+dirty",
            "worktree+",
            "WORKTREE",
            HARNESS[:-1],
            HARNESS + "+dirty",
        ],
        "_HARNESS_RE": [
            HARNESS,
            HARNESS + "+dirty",
            "unknown",
            "unknown+dirty",
            HARNESS.upper(),
            "worktree",
        ],
    }
    for name, values in samples.items():
        writes = [bool(getattr(story, name).fullmatch(v)) for v in values]
        reads = [bool(getattr(story_verdict, name).fullmatch(v)) for v in values]
        assert writes == reads, name


@pytest.mark.unit
def test_a_kept_hash_or_sha_survives_the_job_s_redactor_and_still_reads(
    tmp_path: Path,
) -> None:
    """The redactor rewrites any 12-digit run, which an all-digit hash and about one
    commit in sixty hold. The start line names five such values; the judge refuses
    the line once any is rewritten, and reads it whole when all are kept."""
    from ux_runner import artifacts, redact

    hashes = {
        "story_hash": "123456789012",
        "series": "210987654321",
        "corpus_hash": "555555555555",
    }
    shas = {
        "docs_ref": "abcdef" + "123456789012" + "ab" * 11,
        "harness": "fedcba" + "210987654321" + "cd" * 11,
    }
    start = {**synthetic_start(), **hashes, **shas}
    path = tmp_path / story.JOURNAL_FILE
    writer(path, start).end("eof")
    text = path.read_text(encoding="utf-8")
    plain = artifacts.Scrub(artifacts.redactor_for({}))
    assert isinstance(plain.redactor, redact.Redactor)
    with pytest.raises(story_verdict.JournalError):
        story_verdict.parse_journal(plain(text))
    kept = artifacts.Scrub(artifacts.redactor_for({}))
    kept.keep(*hashes.values(), *shas.values())
    read = story_verdict.parse_journal(kept(text))
    assert {name: getattr(read.start, name) for name in [*hashes, *shas]} == {
        **hashes,
        **shas,
    }


GRAPH: dict[str, set[str]] = {
    "ux_runner.python_floor": set(),
    "ux_runner.stories": {"ux_runner.python_floor"},
    "ux_runner.docs_index": {"ux_runner.ui_change", "check_markdown_links"},
    "ux_runner.story_verdict": {
        "ux_runner.python_floor",
        "ux_runner.section",
        "ux_runner.ui_change",
    },
    "ux_runner.changelog_select": {
        "ux_runner.python_floor",
        "ux_runner.stories",
        "ux_runner.ui_change",
    },
    "ux_story_mcp": {"ux_recorder_mcp"},
}
FIRST_PARTY_PREFIXES = ("ux_", "check_", "codebuild", "ai_mr_review", "ai_ux_test")
NEVER_IMPORTED = ("boto3", "botocore", "yaml", "PIL")


@pytest.mark.unit
@pytest.mark.parametrize("module", sorted(GRAPH))
def test_each_story_module_imports_only_what_the_graph_gives_it(module: str) -> None:
    """Imported alone on a bare ``python3 -I``: the first-party modules it pulls in
    are exactly its edges, and nothing outside the standard library is loaded."""
    probe = (
        "import importlib, json, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "before = set(sys.modules)\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded = set(json.loads(done.stdout))
    first_party = {
        name
        for name in loaded
        if name.startswith(FIRST_PARTY_PREFIXES) and name not in {module, "ux_runner"}
    }
    assert first_party == GRAPH[module]
    assert not {name.split(".")[0] for name in loaded} & set(NEVER_IMPORTED)
