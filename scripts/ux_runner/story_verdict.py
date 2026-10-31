#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The verdict of a first-time-user story flow, judged from its journal alone."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from typing import TypeGuard

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from ux_runner import python_floor  # noqa: E402

python_floor.require(__file__)

from ux_runner import section  # noqa: E402

#: Bumped when a verdict, a finding or results.json would change for the same journals.
JUDGEMENT_REVISION = 3
RESULTS_SCHEMA = "ux-test-stories/results"
RUN_SCHEMA = "ux-test-stories/run"
RESULTS_VERSION = 1
RUN_VERSION = 1
#: A timed-out session whose last call is at least this old was hung, not acting.
#: A relayed browser call may run two minutes and is journalled when it returns,
#: and a model turn takes time of its own, so this is well past both.
HANG_SECONDS = 240.0
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_LINES = 5000
#: Every verdict, worst first.
VERDICT_ORDER: tuple[str, ...] = (
    "not_refused",
    "stuck",
    "offered",
    "out_of_steps",
    "error",
    "gave_up",
    "unclear",
    "not_run",
    "refused_as_expected",
    "done",
)
#: The verdicts that miss the expectation the docs set.
BAD_VERDICTS: frozenset[str] = frozenset({"not_refused", "stuck", "offered"})
#: The verdicts that meet it.
GOOD_VERDICTS: frozenset[str] = frozenset({"done", "refused_as_expected"})
#: Re-run once at the same budget, in MR and nightly runs.
RERUN_VERDICTS: frozenset[str] = frozenset({"not_refused", "stuck", "offered"})
#: The verdicts of an attempt that measured nothing about the product.
_UNMEASURED_VERDICTS: frozenset[str] = frozenset({"error", "not_run"})
#: Re-run once at twice the budget, at night only.
BUDGET_RERUN_VERDICTS: frozenset[str] = frozenset({"out_of_steps", "gave_up"})
#: Whether each verdict meets the expectation; None leaves it unknown.
VERDICT_MEETS: dict[str, bool | None] = {
    "not_refused": False,
    "stuck": False,
    "offered": False,
    "out_of_steps": None,
    "error": None,
    "gave_up": None,
    "unclear": None,
    "not_run": None,
    "refused_as_expected": True,
    "done": True,
}
#: Each verdict's key into ``section.OUTCOME_ICONS``.
VERDICT_ICON_KEYS: dict[str, str] = {
    "done": "works",
    "refused_as_expected": "works",
    "stuck": "broken",
    "not_refused": "broken",
    "offered": "broken",
    "out_of_steps": "not_covered",
    "gave_up": "not_covered",
    "unclear": "not_covered",
    "not_run": "not_covered",
    "error": "blocked",
}
#: What a reader sees for each verdict, before its suffix and confirmation.
VERDICT_LABELS: dict[str, str] = {
    "done": "DONE",
    "refused_as_expected": "REFUSED, as the docs say",
    "stuck": "STUCK",
    "not_refused": "NOT REFUSED: did what the docs say this role cannot",
    "offered": "OFFERED a control the docs say this role lacks",
    "out_of_steps": "OUT OF STEPS",
    "gave_up": "GAVE UP",
    "unclear": "UNCLEAR",
    "not_run": "NOT RUN",
    "error": "HARNESS ERROR",
}
#: Why a flow was not run; the sub-kind of not_run.
NOT_RUN_REASONS: tuple[str, ...] = (
    "persona",
    "budget",
    "time",
    "sign_in_refused",
    "since",
    "precondition",
)
#: The orchestrator's own failures, set on the flow context before judging.
HARNESS_ERRORS: tuple[str, ...] = (
    "launch",
    "selftest",
    "sign_in",
    "viewport",
    "recorder",
    "session",
)
#: The sub-kinds of error.
ERROR_CAUSES: tuple[str, ...] = HARNESS_ERRORS + (
    "audit",
    "is_error",
    "crash",
    "journal",
    "signed_out",
    "relay",
    "hang",
)
#: The sub-kinds of gave_up.
GAVE_UP_CAUSES: tuple[str, ...] = (
    "budget_exhausted",
    "refusal_limit",
    "max_turns",
    "dollar_cap",
    "wall_clock",
)
VERDICT_FLAGS: tuple[str, ...] = (
    "done_after_budget",
    "stopped_at_commit_point",
    "unproven",
)
#: Finding categories, in the order results.json sorts them.
FINDING_CATEGORIES: tuple[str, ...] = ("flow", "clarity", "docs")
CONFIRMATION_STATES: tuple[str, ...] = ("single", "unconfirmed", "confirmed", "mixed")
#: Journal v1's call classes; equal to ux_story_mcp's, which writes them.
JOURNAL_CLASSES: tuple[str, ...] = (
    "action",
    "look",
    "doc",
    "note",
    "report",
    "guard",
    "budget",
    "harness",
)
#: Journal v1's refusal reasons; equal to ux_story_mcp's.
REFUSALS: tuple[str, ...] = (
    "budget",
    "withheld",
    "commit_point",
    "session_end",
    "image_cap",
    "size",
    "limit",
    "terminal_done",
    "invalid",
)
#: Journal v1's end reasons; equal to ux_story_mcp's.
END_REASONS: tuple[str, ...] = ("terminal", "eof", "signal", "refusal_limit")
SESSION_MODES: tuple[str, ...] = ("unattended", "watch", "pair")
INJECTION_PLACES: tuple[str, ...] = ("page", "documentation", "dialog", "other")
BLOCKED_KINDS: tuple[str, ...] = (
    "no_route",
    "wrong_outcome",
    "refused",
    "precondition",
)
#: The labels of a documentation read that found no page.
DOC_MISS_LABELS: tuple[str, ...] = ("help_link_unresolved", "doc_address_guessed")
#: The labels of the harness's own journal lines.
HARNESS_LABELS: tuple[str, ...] = ("signed_out", "child_died")

RUN_FILE = "story-run.json"
RESULTS_FILE = "results.json"
FLOWS_DIR = "flows"

#: stories.ID_RE, the shape of a story and a persona id.
_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_KEY_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_HASH_RE = re.compile(r"[0-9a-f]{12}")
_DOCS_REF_RE = re.compile(r"[0-9a-f]{40}|worktree(?:\+dirty)?")
_HARNESS_RE = re.compile(r"[0-9a-f]{40}(?:\+dirty)?|unknown")
_ISSUE_ID_RE = re.compile(r"[A-Z]{4}-[0-9a-f]{8}")
_ORIGIN_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
#: An unknown key is named in an error only in this shape, so no error quotes text.
_NAMEABLE_KEY_RE = re.compile(r"[a-z_]{1,32}")
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PATH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: stories.EXPECTATIONS, KINDS and SIGN_IN_GROUPS, held here so the judge
#: imports nothing of the catalogue's.
_EXPECTATIONS: tuple[str, ...] = ("allowed", "refused", "not_offered")
_KINDS: tuple[str, ...] = ("read", "commit_point")
_SIGN_IN_GROUPS: tuple[str, ...] = ("Admin", "Author", "Reviewer", "Viewer")
_SEVERITIES: tuple[str, ...] = ("blocker", "major")
_STOPS: tuple[str, ...] = ("end_turn", "max_turns", "budget", "error", "timeout")
_AUDIT_STATES: tuple[str, ...] = ("clean", "violations", "unavailable", "not_run")
_USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_SEARCH_TOOL = "search_documentation"
_READ_TOOL = "read_documentation"
_CLASS_TOOLS: dict[str, tuple[str, ...]] = {
    "doc": (_SEARCH_TOOL, _READ_TOOL),
    "note": ("note_confusion", "note_dead_end", "note_injection"),
    "report": ("report_done", "report_blocked"),
}
_REPORT_TOOLS: dict[str, str] = {"done": "report_done", "blocked": "report_blocked"}
_STUCK_SUFFIXES: dict[str, str] = {
    "no_route": ": no route",
    "wrong_outcome": ": wrong outcome",
    "refused": ": refused although allowed",
    "precondition": ": precondition missing",
}
_START_FIELDS: tuple[str, ...] = (
    "v",
    "type",
    "seq",
    "t",
    "story",
    "story_hash",
    "series",
    "persona",
    "group",
    "expectation",
    "kind",
    "budget",
    "relay",
    "attempt",
    "docs_ref",
    "corpus_hash",
    "harness",
    "model",
    "effort",
    "mode",
    "epoch",
)
_CALL_FIELDS: tuple[str, ...] = (
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
)
_END_FIELDS: tuple[str, ...] = ("v", "type", "seq", "t", "reason")
_RESULT_FIELDS: tuple[str, ...] = ("ids", "screen")
#: The journal writer's bounds (ux_story_mcp), applied again to what is shown.
_WHAT_CHARS = 200
_WHY_CHARS = 400
_REF_CHARS = 300
_EVIDENCE_CHARS = 400
_REASON_CHARS = 600
_LABEL_CHARS = 120
_MODEL_CHARS = 120
_EFFORT_CHARS = 20
_ARGS_MAX = 8
_IDS_MAX = 20
_NOTES_PER_FLOW = 60
#: help_links_unresolved and commit_points keep at most this many entries.
_LIST_MAX = 20
#: The bounds of §8.4: an untrusted part of a finding, and a whole title.
_FINDING_PART_CHARS = 300
_FINDING_TITLE_CHARS = 200
_RECORD_MAX_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


class JournalError(Exception):
    """A journal that cannot be read as v1; the message names the line, never its text."""


class RunRecordError(Exception):
    """story-run.json or results.json is not the v1 shape."""


class TreeScrub(Protocol):
    def keep(self, *values: str) -> None: ...
    def tree(self, value: object, *, fields: bool = False) -> object: ...


@dataclass(frozen=True)
class JournalStart:
    story: str
    story_hash: str
    series: str
    persona: str
    group: str
    expectation: str
    kind: str
    budget: int | None
    relay: bool
    attempt: int
    docs_ref: str
    corpus_hash: str
    harness: str
    model: str
    effort: str
    mode: str
    epoch: float


@dataclass(frozen=True)
class JournalCall:
    seq: int
    t: float
    tool: str
    cls: str
    counted: bool
    actions_used: int | None
    images_sent: int | None
    ok: bool
    refused: str | None
    label: str | None
    args: Mapping[str, Any]
    ids: tuple[str, ...]
    screen: str | None
    terminal: str | None
    after_budget: bool | None


@dataclass(frozen=True)
class JournalEnd:
    seq: int
    t: float
    reason: str


@dataclass(frozen=True)
class Journal:
    start: JournalStart
    calls: tuple[JournalCall, ...]
    end: JournalEnd | None


@dataclass(frozen=True)
class SessionOutcome:
    started_epoch: float
    ended_epoch: float
    timed_out: bool
    returncode: int
    stop: str | None
    is_error: bool
    turns: int | None
    duration_s: float | None
    cost_usd: float | None
    cost_usd_pinned: float | None
    counted_usd: float
    usage: Mapping[str, int] | None

    def to_json(self) -> dict[str, Any]:
        """The ``session`` object of a story-run.json flow."""
        return {
            "started_epoch": self.started_epoch,
            "ended_epoch": self.ended_epoch,
            "timed_out": self.timed_out,
            "returncode": self.returncode,
            "stop": self.stop,
            "is_error": self.is_error,
            "turns": self.turns,
            "duration_s": self.duration_s,
            "cost_usd": self.cost_usd,
            "cost_usd_pinned": self.cost_usd_pinned,
            "counted_usd": self.counted_usd,
            "usage": None if self.usage is None else dict(self.usage),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SessionOutcome:
        """Rebuild a session outcome, refusing any shape :meth:`to_json` would not write."""
        record = _Fields(data, "a flow's session", _SESSION_FIELDS, RunRecordError)
        return cls(
            started_epoch=record.number("started_epoch"),
            ended_epoch=record.number("ended_epoch"),
            timed_out=record.flag("timed_out"),
            returncode=record.whole("returncode", least=None),
            stop=record.maybe_choice("stop", _STOPS),
            is_error=record.flag("is_error"),
            turns=record.maybe_whole("turns"),
            duration_s=record.maybe_number("duration_s"),
            cost_usd=record.maybe_number("cost_usd"),
            cost_usd_pinned=record.maybe_number("cost_usd_pinned"),
            counted_usd=record.number("counted_usd"),
            usage=record.usage("usage"),
        )


_SESSION_FIELDS: tuple[str, ...] = (
    "started_epoch",
    "ended_epoch",
    "timed_out",
    "returncode",
    "stop",
    "is_error",
    "turns",
    "duration_s",
    "cost_usd",
    "cost_usd_pinned",
    "counted_usd",
    "usage",
)


@dataclass(frozen=True)
class FlowContext:
    key: str
    story: str
    persona: str
    group: str
    title: str
    expectation: str
    kind: str
    documented_steps: int
    needs: tuple[str, ...]
    declared_docs: tuple[str, ...]
    series: str
    budget: int
    attempt: int = 1
    budget_scale: int = 1
    journal: str | None = None
    not_run_reason: str | None = None
    harness_error: str | None = None
    audit_state: str = "not_run"
    strays: int = 0
    session: SessionOutcome | None = None
    refusals_by_origin: Mapping[str, int] = field(default_factory=dict)
    video: str | None = None
    chapters: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        """The story-run.json flow: the field names of this class, in order."""
        return {
            "key": self.key,
            "story": self.story,
            "persona": self.persona,
            "group": self.group,
            "title": self.title,
            "expectation": self.expectation,
            "kind": self.kind,
            "documented_steps": self.documented_steps,
            "needs": list(self.needs),
            "declared_docs": list(self.declared_docs),
            "series": self.series,
            "budget": self.budget,
            "attempt": self.attempt,
            "budget_scale": self.budget_scale,
            "journal": self.journal,
            "not_run_reason": self.not_run_reason,
            "harness_error": self.harness_error,
            "audit_state": self.audit_state,
            "strays": self.strays,
            "session": None if self.session is None else self.session.to_json(),
            "refusals_by_origin": dict(self.refusals_by_origin),
            "video": self.video,
            "chapters": [list(pair) for pair in self.chapters],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FlowContext:
        """Rebuild a flow context, refusing any shape :meth:`to_json` would not write."""
        names = tuple(f.name for f in dataclasses.fields(cls))
        record = _Fields(data, "a flow context", names, RunRecordError)
        story, persona, key = _flow_identity(record)
        attempt = record.choice_of("attempt", (1, 2))
        journal = record.maybe_text("journal")
        if journal is not None and journal != journal_path(key, attempt):
            raise record.fail("journal", f"{journal_path(key, attempt)!r} or null")
        session = record.data["session"]
        return cls(
            key=key,
            story=story,
            persona=persona,
            group=record.choice("group", _SIGN_IN_GROUPS),
            title=record.text("title"),
            expectation=record.choice("expectation", _EXPECTATIONS),
            kind=record.choice("kind", _KINDS),
            documented_steps=record.whole("documented_steps"),
            needs=record.texts("needs"),
            declared_docs=record.texts("declared_docs"),
            series=record.shaped("series", _HASH_RE, "12 lowercase hex digits"),
            budget=record.whole("budget"),
            attempt=attempt,
            budget_scale=record.whole("budget_scale", least=1),
            journal=journal,
            not_run_reason=record.maybe_choice("not_run_reason", NOT_RUN_REASONS),
            harness_error=record.maybe_choice("harness_error", HARNESS_ERRORS),
            audit_state=record.choice("audit_state", _AUDIT_STATES),
            strays=record.whole("strays"),
            session=None if session is None else SessionOutcome.from_json(session),
            refusals_by_origin=record.counts("refusals_by_origin"),
            video=record.maybe_text("video"),
            chapters=record.pairs("chapters"),
        )


@dataclass(frozen=True)
class Verdict:
    verdict: str
    sub_kind: str | None
    flags: tuple[str, ...]
    met_expectation: bool | None


@dataclass(frozen=True)
class Note:
    what: str
    why: str
    docs_ref: str | None = None


@dataclass(frozen=True)
class Notes:
    confusion: tuple[Note, ...]
    dead_end: tuple[Note, ...]
    injections: int
    injection_places: Mapping[str, int]


@dataclass(frozen=True)
class Terminal:
    type: str
    evidence: str | None
    reason: str | None
    kind: str | None
    offered_a_control: bool | None
    stopped_at_commit_point: bool | None
    after_budget: bool | None


@dataclass(frozen=True)
class ClaudeRecord:
    stop: str | None
    turns: int | None
    duration_s: float | None
    is_error: bool
    cost_usd: float | None
    cost_usd_pinned: float | None
    counted_usd: float
    usage: Mapping[str, int] | None


@dataclass(frozen=True)
class FlowResult:
    key: str
    story: str
    persona: str
    group: str
    title: str
    expectation: str
    kind: str
    series: str
    attempt: int
    budget_scale: int
    verdict: str
    sub_kind: str | None
    flags: tuple[str, ...]
    met_expectation: bool | None
    findings: tuple[str, ...]
    documented_steps: int
    actions_used: int | None
    budget: int | None
    overrun: float | None
    budget_exhausted: bool | None
    looks: int | None
    searches: int
    reads: int
    notes: Notes
    help_links_unresolved: tuple[str, ...]
    doc_addresses_guessed: int
    commit_points: tuple[str, ...]
    declared_docs_found: bool
    terminal: Terminal | None
    claude: ClaudeRecord | None
    audit: str
    refusals_by_origin: Mapping[str, int]
    video: str | None
    chapters: tuple[tuple[str, str], ...]

    def to_json(self) -> dict[str, Any]:
        """One results.json flow, keys in the frozen order of §5.3."""
        terminal = self.terminal
        claude = self.claude
        return {
            "key": self.key,
            "story": self.story,
            "persona": self.persona,
            "group": self.group,
            "title": self.title,
            "expectation": self.expectation,
            "kind": self.kind,
            "series": self.series,
            "attempt": self.attempt,
            "budget_scale": self.budget_scale,
            "verdict": self.verdict,
            "sub_kind": self.sub_kind,
            "flags": list(self.flags),
            "met_expectation": self.met_expectation,
            "findings": list(self.findings),
            "documented_steps": self.documented_steps,
            "actions_used": self.actions_used,
            "budget": self.budget,
            "overrun": self.overrun,
            "budget_exhausted": self.budget_exhausted,
            "looks": self.looks,
            "searches": self.searches,
            "reads": self.reads,
            "notes": {
                "confusion": [_note_json(note) for note in self.notes.confusion],
                "dead_end": [_note_json(note) for note in self.notes.dead_end],
                "injection": {
                    "count": self.notes.injections,
                    "where": {
                        place: self.notes.injection_places.get(place, 0)
                        for place in INJECTION_PLACES
                    },
                },
            },
            "help_links_unresolved": list(self.help_links_unresolved),
            "doc_addresses_guessed": self.doc_addresses_guessed,
            "commit_points": list(self.commit_points),
            "declared_docs_found": self.declared_docs_found,
            "terminal": None
            if terminal is None
            else {
                "type": terminal.type,
                "evidence": terminal.evidence,
                "reason": terminal.reason,
                "kind": terminal.kind,
                "offered_a_control": terminal.offered_a_control,
                "stopped_at_commit_point": terminal.stopped_at_commit_point,
                "after_budget": terminal.after_budget,
            },
            "claude": None
            if claude is None
            else {
                "stop": claude.stop,
                "turns": claude.turns,
                "duration_s": claude.duration_s,
                "is_error": claude.is_error,
                "cost_usd": claude.cost_usd,
                "cost_usd_pinned": claude.cost_usd_pinned,
                "counted_usd": claude.counted_usd,
                "usage": None if claude.usage is None else dict(claude.usage),
            },
            "audit": self.audit,
            "refusals_by_origin": dict(self.refusals_by_origin),
            "video": self.video,
            "chapters": [list(pair) for pair in self.chapters],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FlowResult:
        """Rebuild a flow result, refusing any shape :meth:`to_json` would not write."""
        names = tuple(f.name for f in dataclasses.fields(cls))
        record = _Fields(data, "a flow result", names, RunRecordError)
        story, persona, key = _flow_identity(record)
        verdict = record.choice("verdict", VERDICT_ORDER)
        causes = _SUB_KINDS.get(verdict)
        sub_kind = record.maybe_choice("sub_kind", causes or ())
        if (causes is None) != (sub_kind is None):
            raise record.fail("sub_kind", "set exactly when the verdict has causes")
        flags = record.texts("flags")
        if any(flag not in VERDICT_FLAGS for flag in flags):
            raise record.fail("flags", "a list of verdict flags")
        met = record.maybe_flag("met_expectation")
        if met is not VERDICT_MEETS[verdict]:
            raise record.fail(
                "met_expectation", f"{VERDICT_MEETS[verdict]} for {verdict}"
            )
        ids = record.texts("findings")
        if any(not _ISSUE_ID_RE.fullmatch(ident) for ident in ids):
            raise record.fail("findings", "a list of finding ids")
        return cls(
            key=key,
            story=story,
            persona=persona,
            group=record.choice("group", _SIGN_IN_GROUPS),
            title=record.text("title"),
            expectation=record.choice("expectation", _EXPECTATIONS),
            kind=record.choice("kind", _KINDS),
            series=record.shaped("series", _HASH_RE, "12 lowercase hex digits"),
            attempt=record.choice_of("attempt", (1, 2)),
            budget_scale=record.whole("budget_scale", least=1),
            verdict=verdict,
            sub_kind=sub_kind,
            flags=flags,
            met_expectation=met,
            findings=ids,
            documented_steps=record.whole("documented_steps"),
            actions_used=record.maybe_whole("actions_used"),
            budget=record.maybe_whole("budget"),
            overrun=record.maybe_number("overrun"),
            budget_exhausted=record.maybe_flag("budget_exhausted"),
            looks=record.maybe_whole("looks"),
            searches=record.whole("searches"),
            reads=record.whole("reads"),
            notes=_notes_from_json(record.data["notes"]),
            help_links_unresolved=record.texts("help_links_unresolved"),
            doc_addresses_guessed=record.whole("doc_addresses_guessed"),
            commit_points=record.texts("commit_points"),
            declared_docs_found=record.flag("declared_docs_found"),
            terminal=_terminal_from_json(record.data["terminal"]),
            claude=_claude_from_json(record.data["claude"]),
            audit=record.choice("audit", _AUDIT_STATES),
            refusals_by_origin=record.counts("refusals_by_origin"),
            video=record.maybe_text("video"),
            chapters=record.pairs("chapters"),
        )


@dataclass(frozen=True)
class Confirmation:
    verdict: str
    state: str
    agree: int
    of: int
    attempt: int
    extended_budget: bool

    @property
    def label(self) -> str:
        """ "" when single, else ``unconfirmed (1 of 1)``, ``2 of 2`` or ``mixed (1 of 2)``."""
        if self.state == "single":
            return ""
        if self.state == "confirmed":
            return f"{self.agree} of {self.of}"
        return f"{self.state} ({self.agree} of {self.of})"


@dataclass(frozen=True)
class Finding:
    id: str
    category: str
    severity: str
    title: str
    body: str
    scope: tuple[str, ...]
    confirmation: str | None

    def to_json(self) -> dict[str, Any]:
        """One results.json finding."""
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "scope": list(self.scope),
            "confirmation": self.confirmation,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Finding:
        """Rebuild a finding, refusing any shape :meth:`to_json` would not write."""
        names = tuple(f.name for f in dataclasses.fields(cls))
        record = _Fields(data, "a finding", names, RunRecordError)
        scope = record.texts("scope")
        if not scope or any(not _KEY_RE.fullmatch(key) for key in scope):
            raise record.fail("scope", "a non-empty list of flow keys")
        return cls(
            id=record.shaped("id", _ISSUE_ID_RE, "a finding id"),
            category=record.choice("category", FINDING_CATEGORIES),
            severity=record.choice("severity", _SEVERITIES),
            title=record.text("title"),
            body=record.text("body"),
            scope=scope,
            confirmation=record.maybe_choice("confirmation", CONFIRMATION_STATES),
        )


_SUB_KINDS: dict[str, tuple[str, ...]] = {
    "not_run": NOT_RUN_REASONS,
    "error": ERROR_CAUSES,
    "gave_up": GAVE_UP_CAUSES,
    "stuck": BLOCKED_KINDS,
}


class _Fields:
    """Typed reads of one JSON object whose keys are closed; errors name keys only."""

    def __init__(
        self,
        data: object,
        where: str,
        keys: Sequence[str],
        error: Callable[[str], Exception],
    ) -> None:
        self.where = where
        self.error = error
        if not isinstance(data, Mapping):
            raise error(f"{where} is not a JSON object")
        for key in data:
            if key not in keys:
                raise error(f"{where} has {_unknown_key(key)}")
        for key in keys:
            if key not in data:
                raise error(f"{where} has no {key!r}")
        self.data: Mapping[str, Any] = data

    def fail(self, key: str, wanted: str) -> Exception:
        return self.error(f"{self.where}: {key!r} must be {wanted}")

    def text(self, key: str) -> str:
        value = self.data[key]
        if not isinstance(value, str):
            raise self.fail(key, "text")
        return value

    def maybe_text(self, key: str) -> str | None:
        value = self.data[key]
        if value is not None and not isinstance(value, str):
            raise self.fail(key, "text or null")
        return value

    def bounded(self, key: str, limit: int) -> str:
        value = self.data[key]
        if not isinstance(value, str) or not value or len(value) > limit:
            raise self.fail(key, f"text of 1 to {limit} characters")
        return value

    def whole(self, key: str, *, least: int | None = 0) -> int:
        value = self.data[key]
        if not _is_int(value) or (least is not None and value < least):
            floor = "" if least is None else f" of at least {least}"
            raise self.fail(key, f"a whole number{floor}")
        return value

    def maybe_whole(self, key: str, *, least: int = 0) -> int | None:
        value = self.data[key]
        if value is None:
            return None
        if not _is_int(value) or value < least:
            raise self.fail(key, f"a whole number of at least {least}, or null")
        return value

    def choice_of(self, key: str, options: Sequence[int]) -> int:
        value = self.data[key]
        if not _is_int(value) or value not in options:
            raise self.fail(key, " or ".join(str(option) for option in options))
        return value

    def number(self, key: str, *, least: float | None = None) -> float:
        value = self.data[key]
        if not _is_number(value) or (least is not None and value < least):
            raise self.fail(key, "a finite number")
        return value

    def maybe_number(self, key: str) -> float | None:
        value = self.data[key]
        if value is not None and not _is_number(value):
            raise self.fail(key, "a finite number or null")
        return value

    def flag(self, key: str) -> bool:
        value = self.data[key]
        if not isinstance(value, bool):
            raise self.fail(key, "true or false")
        return value

    def maybe_flag(self, key: str) -> bool | None:
        value = self.data[key]
        if value is not None and not isinstance(value, bool):
            raise self.fail(key, "true, false or null")
        return value

    def choice(self, key: str, options: Sequence[str]) -> str:
        value = self.data[key]
        if not isinstance(value, str) or value not in options:
            raise self.fail(key, "one of " + ", ".join(options))
        return value

    def maybe_choice(self, key: str, options: Sequence[str]) -> str | None:
        value = self.data[key]
        if value is None:
            return None
        if not isinstance(value, str) or value not in options:
            raise self.fail(key, "null or one of " + ", ".join(options))
        return value

    def shaped(self, key: str, pattern: re.Pattern[str], wanted: str) -> str:
        value = self.data[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise self.fail(key, wanted)
        return value

    def texts(self, key: str) -> tuple[str, ...]:
        value = self.data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.fail(key, "a list of text")
        return tuple(value)

    def counts(self, key: str) -> dict[str, int]:
        value = self.data[key]
        if not isinstance(value, Mapping) or not all(
            isinstance(name, str)
            and _ORIGIN_RE.fullmatch(name)
            and _is_int(count)
            and count >= 0
            for name, count in value.items()
        ):
            raise self.fail(key, "an object of counts")
        return dict(value)

    def pairs(self, key: str) -> tuple[tuple[str, str], ...]:
        value = self.data[key]
        if not isinstance(value, list) or not all(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            for pair in value
        ):
            raise self.fail(key, "a list of [time, label] pairs")
        return tuple((pair[0], pair[1]) for pair in value)

    def usage(self, key: str) -> dict[str, int] | None:
        value = self.data[key]
        if value is None:
            return None
        if not isinstance(value, Mapping) or not all(
            name in _USAGE_KEYS and _is_int(count) and count >= 0
            for name, count in value.items()
        ):
            raise self.fail(key, "null or an object of token counts")
        return dict(value)


def _unknown_key(key: object) -> str:
    if isinstance(key, str) and _NAMEABLE_KEY_RE.fullmatch(key):
        return f"the unknown key {key!r}"
    return "an unknown key"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _flow_identity(record: _Fields) -> tuple[str, str, str]:
    story = record.shaped("story", _ID_RE, "a story id")
    persona = record.shaped("persona", _ID_RE, "a persona id")
    key = record.text("key")
    if key != f"{story}@{persona}":
        raise record.fail("key", "story@persona")
    return story, persona, key


def _note_json(note: Note) -> dict[str, Any]:
    return {"what": note.what, "why": note.why, "docs_ref": note.docs_ref}


def _notes_from_json(data: object) -> Notes:
    record = _Fields(
        data,
        "a flow result's notes",
        ("confusion", "dead_end", "injection"),
        RunRecordError,
    )
    lists: dict[str, tuple[Note, ...]] = {}
    for name in ("confusion", "dead_end"):
        items = record.data[name]
        if not isinstance(items, list):
            raise record.fail(name, "a list of notes")
        notes: list[Note] = []
        for item in items:
            note = _Fields(
                item, f"a {name} note", ("what", "why", "docs_ref"), RunRecordError
            )
            notes.append(
                Note(
                    what=note.text("what"),
                    why=note.text("why"),
                    docs_ref=note.maybe_text("docs_ref"),
                )
            )
        lists[name] = tuple(notes)
    injection = _Fields(
        record.data["injection"],
        "a flow result's injections",
        ("count", "where"),
        RunRecordError,
    )
    where = _Fields(
        injection.data["where"],
        "a flow result's injection places",
        INJECTION_PLACES,
        RunRecordError,
    )
    return Notes(
        confusion=lists["confusion"],
        dead_end=lists["dead_end"],
        injections=injection.whole("count"),
        injection_places={place: where.whole(place) for place in INJECTION_PLACES},
    )


def _terminal_from_json(data: object) -> Terminal | None:
    if data is None:
        return None
    names = tuple(f.name for f in dataclasses.fields(Terminal))
    record = _Fields(data, "a flow result's terminal", names, RunRecordError)
    return Terminal(
        type=record.choice("type", tuple(_REPORT_TOOLS)),
        evidence=record.maybe_text("evidence"),
        reason=record.maybe_text("reason"),
        kind=record.maybe_choice("kind", BLOCKED_KINDS),
        offered_a_control=record.maybe_flag("offered_a_control"),
        stopped_at_commit_point=record.maybe_flag("stopped_at_commit_point"),
        after_budget=record.maybe_flag("after_budget"),
    )


def _claude_from_json(data: object) -> ClaudeRecord | None:
    if data is None:
        return None
    names = tuple(f.name for f in dataclasses.fields(ClaudeRecord))
    record = _Fields(data, "a flow result's claude record", names, RunRecordError)
    return ClaudeRecord(
        stop=record.maybe_choice("stop", _STOPS),
        turns=record.maybe_whole("turns"),
        duration_s=record.maybe_number("duration_s"),
        is_error=record.flag("is_error"),
        cost_usd=record.maybe_number("cost_usd"),
        cost_usd_pinned=record.maybe_number("cost_usd_pinned"),
        counted_usd=record.number("counted_usd"),
        usage=record.usage("usage"),
    )


def journal_path(key: str, attempt: int) -> str:
    """Where a flow attempt's journal lives, relative to the run directory."""
    if attempt == 1:
        return f"{FLOWS_DIR}/{key}/journal.jsonl"
    return f"{FLOWS_DIR}/{key}/attempt-{attempt}/journal.jsonl"


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("a key appears twice in one object")
        found[key] = value
    return found


def _no_constant(name: str) -> Any:
    raise ValueError("JSON holds no NaN or Infinity")


def _loads(text: str) -> Any:
    """``json.loads`` refusing a repeated key, NaN and Infinity."""
    return json.loads(text, object_pairs_hook=_json_object, parse_constant=_no_constant)


def _line_fields(
    number: int, record: Mapping[str, Any], keys: Sequence[str]
) -> _Fields:
    return _Fields(record, f"journal line {number}", keys, JournalError)


def _start_line(line: _Fields) -> JournalStart:
    if line.data["t"] != 0:
        raise line.fail("t", "0 on the start line")
    relay = line.flag("relay")
    budget = line.data["budget"]
    if relay and not (_is_int(budget) and budget >= 0):
        raise line.fail("budget", "a whole number when relay is true")
    if not relay and budget is not None:
        raise line.fail("budget", "null when relay is false")
    return JournalStart(
        story=line.shaped("story", _ID_RE, "a story id"),
        story_hash=line.shaped("story_hash", _HASH_RE, "12 lowercase hex digits"),
        series=line.shaped("series", _HASH_RE, "12 lowercase hex digits"),
        persona=line.shaped("persona", _ID_RE, "a persona id"),
        group=line.choice("group", _SIGN_IN_GROUPS),
        expectation=line.choice("expectation", _EXPECTATIONS),
        kind=line.choice("kind", _KINDS),
        budget=budget,
        relay=relay,
        attempt=line.choice_of("attempt", (1, 2)),
        docs_ref=line.shaped(
            "docs_ref", _DOCS_REF_RE, "a commit, worktree or worktree+dirty"
        ),
        corpus_hash=line.shaped("corpus_hash", _HASH_RE, "12 lowercase hex digits"),
        harness=line.shaped(
            "harness", _HARNESS_RE, "a commit, optionally +dirty, or unknown"
        ),
        model=line.bounded("model", _MODEL_CHARS),
        effort=line.bounded("effort", _EFFORT_CHARS),
        mode=line.choice("mode", SESSION_MODES),
        epoch=line.number("epoch", least=0),
    )


def _label(line: _Fields, cls: str) -> str | None:
    label = line.maybe_text("label")
    if label is None or cls == "guard":
        return label
    closed = {
        "doc": DOC_MISS_LABELS,
        "harness": HARNESS_LABELS,
        "budget": ("out_of_steps",),
    }
    if label not in closed.get(cls, ()):
        raise line.fail("label", f"null or a {cls} label")
    return label


def _args(line: _Fields) -> dict[str, Any]:
    args = line.data["args"]
    if (
        not isinstance(args, Mapping)
        or len(args) > _ARGS_MAX
        or not all(
            isinstance(value, (str, bool)) or value is None or _is_int(value)
            for value in args.values()
        )
    ):
        raise line.fail("args", f"an object of at most {_ARGS_MAX} plain values")
    return dict(args)


def _result(number: int, line: _Fields) -> tuple[tuple[str, ...], str | None]:
    result = _Fields(
        line.data["result"],
        f"journal line {number} result",
        _RESULT_FIELDS,
        JournalError,
    )
    ids = result.texts("ids")
    if len(ids) > _IDS_MAX:
        raise result.fail("ids", f"at most {_IDS_MAX} section ids")
    return ids, result.maybe_text("screen")


def _terminal_args(line: _Fields, terminal: str, args: Mapping[str, Any]) -> None:
    if terminal == "done":
        sound = isinstance(args.get("evidence"), str) and isinstance(
            args.get("stopped_at_commit_point"), bool
        )
    else:
        sound = (
            isinstance(args.get("reason"), str)
            and args.get("kind") in BLOCKED_KINDS
            and isinstance(args.get("offered_a_control"), bool)
        )
    if not sound:
        raise line.fail("args", f"the arguments of an accepted {terminal} report")


def _call_line(number: int, line: _Fields, start: JournalStart) -> JournalCall:
    cls = line.choice("class", JOURNAL_CLASSES)
    tool = line.text("tool")
    if cls in _CLASS_TOOLS and tool not in _CLASS_TOOLS[cls]:
        raise line.fail("tool", f"a {cls} tool on a {cls} line")
    counted = line.flag("counted")
    if start.relay:
        actions_used = line.whole("actions_used")
        images_sent = line.whole("images_sent")
    else:
        for key in ("actions_used", "images_sent"):
            if line.data[key] is not None:
                raise line.fail(key, "null when relay is false")
        if counted:
            raise line.fail("counted", "false when relay is false")
        actions_used = images_sent = None
    ok = line.flag("ok")
    refused = line.maybe_choice("refused", REFUSALS)
    if refused is not None and ok:
        raise line.fail("ok", "false on a refused call")
    label = _label(line, cls)
    args = _args(line)
    ids, screen = _result(number, line)
    terminal = line.maybe_choice("terminal", tuple(_REPORT_TOOLS))
    after_budget = line.maybe_flag("after_budget")
    if terminal is not None:
        if cls != "report" or not ok or tool != _REPORT_TOOLS[terminal]:
            raise line.fail("terminal", "set only on an accepted report of that kind")
        _terminal_args(line, terminal, args)
    if (terminal is not None and start.relay) != (after_budget is not None):
        raise line.fail(
            "after_budget", "a boolean on a relayed terminal report, else null"
        )
    return JournalCall(
        seq=line.data["seq"],
        t=line.data["t"],
        tool=tool,
        cls=cls,
        counted=counted,
        actions_used=actions_used,
        images_sent=images_sent,
        ok=ok,
        refused=refused,
        label=label,
        args=args,
        ids=ids,
        screen=screen,
        terminal=terminal,
        after_budget=after_budget,
    )


def parse_journal(text: str) -> Journal:
    """§5.2 reader rules; JournalError naming the line number and key, never its content."""
    if len(text) > MAX_JOURNAL_BYTES or len(text.encode("utf-8")) > MAX_JOURNAL_BYTES:
        raise JournalError(
            f"the journal is over its bound of {MAX_JOURNAL_BYTES} bytes"
        )
    lines = text.split("\n")[:-1]
    if len(lines) > MAX_JOURNAL_LINES:
        raise JournalError(
            f"the journal is over its bound of {MAX_JOURNAL_LINES} lines"
        )
    if not lines:
        raise JournalError("the journal has no start line")
    start: JournalStart | None = None
    calls: list[JournalCall] = []
    end: JournalEnd | None = None
    previous = 0.0
    for index, raw in enumerate(lines):
        number = index + 1
        if end is not None:
            raise JournalError(f"journal line {number} follows the end line")
        try:
            record = _loads(raw)
        except (ValueError, RecursionError):
            raise JournalError(f"journal line {number} is not valid JSON") from None
        if not isinstance(record, dict):
            raise JournalError(f"journal line {number} is not a JSON object")
        kind = record.get("type")
        if kind not in ("start", "call", "end"):
            raise JournalError(
                f"journal line {number}: 'type' must be start, call or end"
            )
        if index == 0 and kind != "start":
            raise JournalError("journal line 1: the first line must be the start line")
        if index > 0 and kind == "start":
            raise JournalError(f"journal line {number}: a second start line")
        keys = {"start": _START_FIELDS, "call": _CALL_FIELDS, "end": _END_FIELDS}[kind]
        line = _line_fields(number, record, keys)
        if not _is_int(record["v"]) or record["v"] != 1:
            raise line.fail("v", "1")
        if not _is_int(record["seq"]) or record["seq"] != index:
            raise line.fail("seq", f"{index}, one more than the line before")
        moment = record["t"]
        if not _is_number(moment) or moment < previous:
            raise line.fail("t", "a number no smaller than the line before's")
        previous = moment
        if start is None:
            start = _start_line(line)
        elif kind == "call":
            calls.append(_call_line(number, line, start))
        else:
            end = JournalEnd(
                seq=index, t=moment, reason=line.choice("reason", END_REASONS)
            )
    if start is None:
        raise JournalError("the journal has no start line")
    return Journal(start=start, calls=tuple(calls), end=end)


_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


def _why_not(error: OSError) -> str:
    if error.errno in (errno.ELOOP, errno.EMLINK):
        return "it is a symbolic link, which is never followed"
    if error.errno == errno.ENOENT:
        return "it does not exist"
    return error.strerror or type(error).__name__


def _read_bounded(
    path: Path, limit: int, what: str, error: Callable[[str], Exception]
) -> bytes:
    """At most ``limit`` bytes of the regular file at ``path``, never through a link."""
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as failure:
        raise error(f"{what} cannot be opened: {_why_not(failure)}") from None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            raise error(f"{what} is not a regular file")
        if found.st_size > limit:
            raise error(f"{what} is over its bound of {limit} bytes")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(_READ_CHUNK, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise error(f"{what} is over its bound of {limit} bytes")
        return bytes(data)
    except OSError as failure:
        raise error(f"{what} cannot be read: {_why_not(failure)}") from None
    finally:
        os.close(descriptor)


def read_journal(path: Path) -> Journal:
    """Opened O_NOFOLLOW, a regular file of at most MAX_JOURNAL_BYTES, then parse_journal."""
    data = _read_bounded(path, MAX_JOURNAL_BYTES, "the journal", JournalError)
    whole = data[: data.rfind(b"\n") + 1]
    try:
        text = whole.decode("utf-8")
    except UnicodeDecodeError:
        raise JournalError("the journal is not UTF-8 text") from None
    return parse_journal(text)


@dataclass(frozen=True)
class _Facts:
    """What the truth table's conditions read, computed once per flow."""

    journal: Journal | None
    context: FlowContext
    session: SessionOutcome | None
    terminal: JournalCall | None
    harness_labels: frozenset[str]
    idle: float | None
    most_actions: int | None

    @property
    def args(self) -> Mapping[str, Any]:
        return {} if self.terminal is None else self.terminal.args

    @property
    def blocked(self) -> bool:
        return self.terminal is not None and self.terminal.terminal == "blocked"

    @property
    def done(self) -> bool:
        return self.terminal is not None and self.terminal.terminal == "done"

    @property
    def denied(self) -> bool:
        return self.context.expectation in ("refused", "not_offered")


def _facts(journal: Journal | None, context: FlowContext) -> _Facts:
    session = context.session
    calls = () if journal is None else journal.calls
    terminal = next((call for call in calls if call.terminal is not None), None)
    labels = frozenset(
        call.label for call in calls if call.cls == "harness" and call.label is not None
    )
    idle = None
    if session is not None and journal is not None:
        last = calls[-1].t if calls else 0.0
        idle = session.ended_epoch - (journal.start.epoch + last)
    most = None
    if journal is not None and journal.start.relay:
        most = max(
            (c.actions_used for c in calls if c.actions_used is not None), default=0
        )
    return _Facts(journal, context, session, terminal, labels, idle, most)


def _verdict(
    name: str, sub_kind: str | None = None, flags: tuple[str, ...] = ()
) -> Verdict:
    return Verdict(name, sub_kind, flags, VERDICT_MEETS[name])


def _row_not_run(f: _Facts) -> Verdict | None:
    reason = f.context.not_run_reason
    return None if reason is None else _verdict("not_run", reason)


def _row_harness_error(f: _Facts) -> Verdict | None:
    cause = f.context.harness_error
    return None if cause is None else _verdict("error", cause)


def _row_audit(f: _Facts) -> Verdict | None:
    dirty = f.context.audit_state == "violations" or f.context.strays > 0
    return _verdict("error", "audit") if dirty else None


def _row_is_error(f: _Facts) -> Verdict | None:
    s = f.session
    hit = s is not None and s.is_error and s.stop not in ("max_turns", "budget")
    return _verdict("error", "is_error") if hit else None


def _row_crash(f: _Facts) -> Verdict | None:
    s = f.session
    hit = s is not None and not s.timed_out and s.stop is None
    return _verdict("error", "crash") if hit else None


def _row_no_journal(f: _Facts) -> Verdict | None:
    return _verdict("error", "journal") if f.journal is None else None


def _row_signed_out(f: _Facts) -> Verdict | None:
    return _verdict("error", "signed_out") if "signed_out" in f.harness_labels else None


def _row_child_died(f: _Facts) -> Verdict | None:
    return _verdict("error", "relay") if "child_died" in f.harness_labels else None


def _row_hang(f: _Facts) -> Verdict | None:
    s = f.session
    hit = (
        s is not None and s.timed_out and f.idle is not None and f.idle >= HANG_SECONDS
    )
    return _verdict("error", "hang") if hit else None


def _row_precondition(f: _Facts) -> Verdict | None:
    hit = f.blocked and f.args.get("kind") == "precondition" and bool(f.context.needs)
    return _verdict("not_run", "precondition") if hit else None


def _row_out_of_steps(f: _Facts) -> Verdict | None:
    hit = f.blocked and f.terminal is not None and f.terminal.after_budget is True
    return _verdict("out_of_steps") if hit else None


def _row_offered_on_block(f: _Facts) -> Verdict | None:
    hit = (
        f.blocked
        and f.context.expectation == "not_offered"
        and f.args.get("offered_a_control") is True
    )
    return _verdict("offered") if hit else None


def _row_refused_as_expected(f: _Facts) -> Verdict | None:
    return _verdict("refused_as_expected") if f.blocked and f.denied else None


def _row_stuck(f: _Facts) -> Verdict | None:
    if not f.blocked:
        return None
    kind = f.args.get("kind")
    return _verdict("stuck", kind if isinstance(kind, str) else None)


def _row_not_refused(f: _Facts) -> Verdict | None:
    hit = f.done and f.denied and f.context.kind == "read"
    return _verdict("not_refused") if hit else None


def _row_offered_on_done(f: _Facts) -> Verdict | None:
    hit = f.done and f.denied and f.context.kind == "commit_point"
    return _verdict("offered", flags=("unproven",)) if hit else None


def _row_done(f: _Facts) -> Verdict | None:
    if not f.done or f.terminal is None:
        return None
    flags = []
    if f.terminal.after_budget is True:
        flags.append("done_after_budget")
    if f.args.get("stopped_at_commit_point") is True:
        flags.append("stopped_at_commit_point")
    return _verdict("done", flags=tuple(sorted(flags)))


def _row_budget_exhausted(f: _Facts) -> Verdict | None:
    journal = f.journal
    hit = (
        journal is not None
        and journal.start.relay
        and journal.start.budget is not None
        and f.most_actions is not None
        and f.most_actions >= journal.start.budget
    )
    return _verdict("gave_up", "budget_exhausted") if hit else None


def _row_refusal_limit(f: _Facts) -> Verdict | None:
    end = None if f.journal is None else f.journal.end
    hit = end is not None and end.reason == "refusal_limit"
    return _verdict("gave_up", "refusal_limit") if hit else None


def _row_max_turns(f: _Facts) -> Verdict | None:
    hit = f.session is not None and f.session.stop == "max_turns"
    return _verdict("gave_up", "max_turns") if hit else None


def _row_dollar_cap(f: _Facts) -> Verdict | None:
    hit = f.session is not None and f.session.stop == "budget"
    return _verdict("gave_up", "dollar_cap") if hit else None


def _row_wall_clock(f: _Facts) -> Verdict | None:
    hit = f.session is not None and f.session.timed_out
    return _verdict("gave_up", "wall_clock") if hit else None


def _row_unclear(f: _Facts) -> Verdict | None:
    return _verdict("unclear")


#: The truth table of §8.1, row 0 first; the first row that answers decides.
_ROWS: tuple[Callable[[_Facts], Verdict | None], ...] = (
    _row_not_run,
    _row_harness_error,
    _row_audit,
    _row_is_error,
    _row_crash,
    _row_no_journal,
    _row_signed_out,
    _row_child_died,
    _row_hang,
    _row_precondition,
    _row_out_of_steps,
    _row_offered_on_block,
    _row_refused_as_expected,
    _row_stuck,
    _row_not_refused,
    _row_offered_on_done,
    _row_done,
    _row_budget_exhausted,
    _row_refusal_limit,
    _row_max_turns,
    _row_dollar_cap,
    _row_wall_clock,
    _row_unclear,
)


def _decide(
    rows: Sequence[Callable[[_Facts], Verdict | None]], facts: _Facts
) -> Verdict:
    for row in rows:
        found = row(facts)
        if found is not None:
            return found
    return _verdict("unclear")


def judge(journal: Journal | None, context: FlowContext) -> Verdict:
    """The precedence in §8. ``journal`` is None when it is missing or raised JournalError."""
    return _decide(_ROWS, _facts(journal, context))


def _shown(value: object, limit: int) -> str:
    return section.sanitize(value, limit)


def _unique(values: Sequence[str], limit: int) -> tuple[str, ...]:
    kept: list[str] = []
    for value in values:
        if value and value not in kept:
            kept.append(value)
    return tuple(kept[:limit])


def _terminal_of(call: JournalCall | None) -> Terminal | None:
    if call is None:
        return None
    args = call.args
    if call.terminal == "done":
        stopped = args.get("stopped_at_commit_point")
        return Terminal(
            type="done",
            evidence=_shown(args.get("evidence"), _EVIDENCE_CHARS),
            reason=None,
            kind=None,
            offered_a_control=None,
            stopped_at_commit_point=stopped if isinstance(stopped, bool) else None,
            after_budget=call.after_budget,
        )
    kind = args.get("kind")
    offered = args.get("offered_a_control")
    return Terminal(
        type="blocked",
        evidence=None,
        reason=_shown(args.get("reason"), _REASON_CHARS),
        kind=kind if kind in BLOCKED_KINDS else None,
        offered_a_control=offered if isinstance(offered, bool) else None,
        stopped_at_commit_point=None,
        after_budget=call.after_budget,
    )


def _claude_of(session: SessionOutcome | None) -> ClaudeRecord | None:
    if session is None:
        return None
    stop = session.stop
    if stop is None and session.timed_out:
        stop = "timeout"
    return ClaudeRecord(
        stop=stop,
        turns=session.turns,
        duration_s=session.duration_s,
        is_error=session.is_error,
        cost_usd=session.cost_usd,
        cost_usd_pinned=session.cost_usd_pinned,
        counted_usd=session.counted_usd,
        usage=None if session.usage is None else dict(session.usage),
    )


def _notes_of(calls: Sequence[JournalCall]) -> Notes:
    confusion: list[Note] = []
    dead_end: list[Note] = []
    places = dict.fromkeys(INJECTION_PLACES, 0)
    injections = 0
    for call in calls:
        if call.cls != "note":
            continue
        args = call.args
        if call.tool == "note_injection":
            if call.refused == "invalid":
                continue
            injections += 1
            where = args.get("where")
            places[
                where if isinstance(where, str) and where in places else "other"
            ] += 1
        elif call.ok and call.tool == "note_confusion":
            confusion.append(
                Note(
                    what=_shown(args.get("what"), _WHAT_CHARS),
                    why=_shown(args.get("why"), _WHY_CHARS),
                    docs_ref=_shown(args.get("docs_ref"), _REF_CHARS) or None,
                )
            )
        elif call.ok and call.tool == "note_dead_end":
            dead_end.append(
                Note(
                    what=_shown(args.get("what"), _WHAT_CHARS),
                    why=_shown(args.get("why"), _WHY_CHARS),
                )
            )
    return Notes(
        confusion=tuple(confusion[:_NOTES_PER_FLOW]),
        dead_end=tuple(dead_end[:_NOTES_PER_FLOW]),
        injections=injections,
        injection_places=places,
    )


def summarize(
    journal: Journal | None, context: FlowContext, verdict: Verdict
) -> FlowResult:
    """One FlowResult (§5.3), findings left empty; model text sanitised to the bounds of §6."""
    calls = () if journal is None else journal.calls
    relay = journal is not None and journal.start.relay
    budget = journal.start.budget if journal is not None and relay else None
    actions_used = None
    if relay:
        actions_used = max(
            (call.actions_used for call in calls if call.actions_used is not None),
            default=0,
        )
    overrun = None
    if actions_used is not None and context.documented_steps > 0:
        overrun = round(actions_used / context.documented_steps, 1)
    exhausted = None
    if actions_used is not None and budget is not None:
        exhausted = actions_used >= budget
    docs = [call for call in calls if call.cls == "doc"]
    searches = [call for call in docs if call.tool == _SEARCH_TOOL]
    reads = [call for call in docs if call.tool == _READ_TOOL]
    declared = set(context.declared_docs)
    return FlowResult(
        key=context.key,
        story=context.story,
        persona=context.persona,
        group=context.group,
        title=_shown(context.title, _FINDING_TITLE_CHARS),
        expectation=context.expectation,
        kind=context.kind,
        series=context.series,
        attempt=context.attempt,
        budget_scale=context.budget_scale,
        verdict=verdict.verdict,
        sub_kind=verdict.sub_kind,
        flags=verdict.flags,
        met_expectation=verdict.met_expectation,
        findings=(),
        documented_steps=context.documented_steps,
        actions_used=actions_used,
        budget=budget,
        overrun=overrun,
        budget_exhausted=exhausted,
        looks=sum(1 for call in calls if call.cls == "look") if relay else None,
        searches=sum(1 for call in searches if call.ok),
        reads=sum(1 for call in reads if call.ok),
        notes=_notes_of(calls),
        help_links_unresolved=_unique(
            [
                _shown(call.args.get("ref"), _REF_CHARS)
                for call in reads
                if call.label == "help_link_unresolved"
            ],
            _LIST_MAX,
        ),
        doc_addresses_guessed=sum(
            1 for call in reads if call.label == "doc_address_guessed"
        ),
        commit_points=_unique(
            [
                _shown(call.label, _LABEL_CHARS)
                for call in calls
                if call.cls == "guard" and call.refused == "commit_point"
            ],
            _LIST_MAX,
        ),
        declared_docs_found=any(
            ident in declared for call in searches for ident in call.ids
        ),
        terminal=_terminal_of(
            next((call for call in calls if call.terminal is not None), None)
        ),
        claude=_claude_of(context.session),
        audit=context.audit_state,
        refusals_by_origin=dict(context.refusals_by_origin),
        video=context.video,
        chapters=tuple(
            (
                _shown(at, section.CHAPTER_TIME_CHARS),
                _shown(label, section.CHAPTER_CHARS),
            )
            for at, label in context.chapters[: section.MAX_CHAPTERS]
        ),
    )


def confirm(attempts: Sequence[FlowResult]) -> Confirmation:
    """§8.3, over one flow key's attempts.

    A re-run that measured nothing, a harness error or a re-run that never
    started, neither confirms nor disputes the first attempt, which then stands
    alone.
    """
    ordered = sorted(attempts, key=lambda result: result.attempt)
    if not ordered or len(ordered) > 2:
        raise ValueError("a flow has one attempt or two")
    if len({result.key for result in ordered}) != 1:
        raise ValueError("every attempt must be of the same flow key")
    if len({result.attempt for result in ordered}) != len(ordered):
        raise ValueError("a flow's attempts must be numbered apart")
    first = ordered[0]
    second = ordered[1] if len(ordered) == 2 else None
    if second is None or second.verdict in _UNMEASURED_VERDICTS:
        state = "unconfirmed" if first.verdict in BAD_VERDICTS else "single"
        return Confirmation(first.verdict, state, 1, 1, first.attempt, False)
    if second.budget_scale > first.budget_scale:
        state = "unconfirmed" if second.verdict in BAD_VERDICTS else "single"
        return Confirmation(second.verdict, state, 1, 1, second.attempt, True)
    if second.verdict == first.verdict:
        return Confirmation(first.verdict, "confirmed", 2, 2, first.attempt, False)
    return Confirmation(first.verdict, "mixed", 1, 2, first.attempt, False)


def flow_confirmations(
    results: Sequence[FlowResult],
) -> list[tuple[str, Confirmation, FlowResult]]:
    """Per flow key, in key order: its confirmation and the attempt it shows."""
    rows: list[tuple[str, Confirmation, FlowResult]] = []
    for key, attempts in _by_key(results).items():
        confirmation = confirm(attempts)
        shown = next(r for r in attempts if r.attempt == confirmation.attempt)
        rows.append((key, confirmation, shown))
    return rows


def slug(title: str) -> str:
    """The issue-id slug: casefolded, runs of anything but a-z and 0-9 as one hyphen."""
    return _SLUG_RE.sub("-", title.casefold()).strip("-")


def issue_id(category: str, scope: Sequence[str], title: str) -> str:
    """category[:4].upper() + "-" + sha256(f"{category}|{','.join(sorted(scope))}|{slug(title)}")[:8]."""
    text = f"{category}|{','.join(sorted(scope))}|{slug(title)}"
    return (
        category[:4].upper()
        + "-"
        + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    )


def _part(value: object) -> str:
    return section.sanitize(value, _FINDING_PART_CHARS)


def _finding(
    category: str,
    severity: str,
    title: str,
    body: str,
    scope: Sequence[str],
    confirmation: str | None,
) -> Finding:
    shown = section.sanitize(title, _FINDING_TITLE_CHARS)
    keys = tuple(sorted(scope))
    return Finding(
        id=issue_id(category, keys, shown),
        category=category,
        severity=severity,
        title=shown,
        body=body,
        scope=keys,
        confirmation=confirmation,
    )


def _flow_finding(result: FlowResult, confirmation: Confirmation) -> Finding:
    group = result.group
    title = result.title
    terminal = result.terminal
    if confirmation.verdict == "not_refused":
        body = (
            f"docs/rbac.md says {group} cannot do this, and the flow reported it done."
        )
        evidence = _part(None if terminal is None else terminal.evidence)
        return _finding(
            "flow",
            "blocker",
            f"{group} was able to: {title}",
            body + (f" Evidence: {evidence}" if evidence else ""),
            (result.key,),
            confirmation.state,
        )
    if confirmation.verdict == "stuck":
        reason = _part(None if terminal is None else terminal.reason)
        if result.sub_kind == "refused":
            heading = f"Refused although docs/rbac.md allows {group}: {title}"
            body = f"The application refused a {group} the docs say may do this."
        else:
            cause = (result.sub_kind or "").replace("_", " ")
            heading = f"Not possible for a first-time {group}: {title}"
            body = (
                f"A first-time {group} following the documentation could not "
                f"complete this ({cause})."
            )
        return _finding(
            "flow",
            "blocker",
            heading,
            body + (f" {reason}" if reason else ""),
            (result.key,),
            confirmation.state,
        )
    label = _part(result.commit_points[-1]) if result.commit_points else ""
    body = (
        "The API refusal was not tested: nothing was pressed."
        if "unproven" in result.flags
        else "The control was offered and then disabled, refused or failed."
    )
    return _finding(
        "clarity",
        "major",
        f"{group} is offered a control it cannot use: {label or title}",
        body,
        (result.key,),
        confirmation.state,
    )


def _finding_order(found: Finding) -> tuple[int, int, str, tuple[str, ...], str]:
    return (
        _SEVERITIES.index(found.severity),
        FINDING_CATEGORIES.index(found.category),
        found.title,
        found.scope,
        found.id,
    )


def _by_key(results: Sequence[FlowResult]) -> dict[str, list[FlowResult]]:
    grouped: dict[str, list[FlowResult]] = {}
    for result in sorted(results, key=lambda r: (r.key, r.attempt)):
        grouped.setdefault(result.key, []).append(result)
    return grouped


def findings(results: Sequence[FlowResult]) -> list[Finding]:
    """§8.4, over every attempt of every flow."""
    found: list[Finding] = []
    addresses: dict[str, set[str]] = {}
    for _, confirmation, shown in flow_confirmations(results):
        if confirmation.verdict in BAD_VERDICTS:
            found.append(_flow_finding(shown, confirmation))
    for key, attempts in _by_key(results).items():
        for attempt in attempts:
            for address in attempt.help_links_unresolved:
                shown = _part(address)
                if shown:
                    addresses.setdefault(shown, set()).add(key)
    for address, keys in addresses.items():
        found.append(
            _finding(
                "docs",
                "major",
                f"A help link on screen leads to no documentation page: {address}",
                "read_documentation found no page at an address the screen showed.",
                tuple(keys),
                None,
            )
        )
    return sorted(found, key=_finding_order)


def attach_findings(
    results: Sequence[FlowResult], found: Sequence[Finding]
) -> list[FlowResult]:
    """Each result with the ids of the findings whose scope holds its key."""
    return [
        dataclasses.replace(
            result,
            findings=tuple(dict.fromkeys(f.id for f in found if result.key in f.scope)),
        )
        for result in results
    ]


def verdict_label(result: FlowResult, confirmation: Confirmation) -> str:
    """Display label (§8.5)."""
    verdict = confirmation.verdict
    label = VERDICT_LABELS[verdict]
    if result.verdict == verdict:
        if verdict == "done":
            if "stopped_at_commit_point" in result.flags:
                label += ", stopped before saving"
            if "done_after_budget" in result.flags:
                label += ", after the step budget"
        elif verdict == "stuck" and result.sub_kind in _STUCK_SUFFIXES:
            label += _STUCK_SUFFIXES[result.sub_kind]
        elif verdict in ("gave_up", "not_run", "error") and result.sub_kind:
            label += ": " + result.sub_kind.replace("_", " ")
    if confirmation.label:
        label += f" ({confirmation.label})"
    return label


def _counts_line(counts: Mapping[str, int]) -> str:
    return ",".join(f"{name}={count}" for name, count in counts.items()) or "none"


def _verdict_counts(results: Sequence[FlowResult]) -> dict[str, int]:
    tally = dict.fromkeys(VERDICT_ORDER, 0)
    for attempts in _by_key(results).values():
        tally[confirm(attempts).verdict] += 1
    return {name: count for name, count in tally.items() if count}


def _finding_counts(found: Sequence[Finding]) -> dict[str, int]:
    tally = {
        f"{category}/{severity}": 0
        for severity in _SEVERITIES
        for category in FINDING_CATEGORIES
    }
    for item in found:
        tally[f"{item.category}/{item.severity}"] += 1
    return {name: count for name, count in tally.items() if count}


def cost_totals(
    results: Sequence[FlowResult], watched: object = None
) -> dict[str, Any]:
    """What every attempt's session cost, over all of ``results`` and ``watched``.

    ``counted_usd`` is the total the job's budget counted, each session's
    ``counted_usd``, which is the run's ``spend_usd``. ``pinned_usd`` is the total
    at the pinned rates of every session that reported its usage, and
    ``sessions_without_usage`` how many did not, so ``pinned_usd`` is a floor
    whenever that is above 0. An attempt that never started a session costs
    nothing. A watched run's one session is not a flow result's: it is
    ``watched``, the run record's ``interactive``, and counts as one session with
    its ``counted_usd`` and ``cost_usd_pinned``. A ``counted_usd`` it does not hold
    as a number of dollars counts as nothing, and such a ``cost_usd_pinned`` as no
    usage reported.
    """
    sessions = [
        (result.claude.counted_usd, result.claude.cost_usd_pinned)
        for result in results
        if result.claude is not None
    ]
    if isinstance(watched, Mapping):
        sessions.append(
            (
                _usd(watched.get("counted_usd")) or 0.0,
                _usd(watched.get("cost_usd_pinned")),
            )
        )
    counted = pinned = 0.0
    without = 0
    for counted_usd, pinned_usd in sessions:
        counted = round(counted + counted_usd, 6)
        if pinned_usd is None:
            without += 1
        else:
            pinned = round(pinned + pinned_usd, 6)
    return {
        "counted_usd": counted,
        "pinned_usd": pinned,
        "sessions_without_usage": without,
    }


def _usd(value: object) -> float | None:
    return float(value) if _is_number(value) and value >= 0 else None


def cost_text(totals: Mapping[str, Any]) -> str:
    """One clause for a status line, from :func:`cost_totals`' mapping."""
    text = (
        f"${float(totals['counted_usd']):.2f} counted, "
        f"${float(totals['pinned_usd']):.2f} at the pinned rates"
    )
    without = int(totals["sessions_without_usage"])
    if without:
        text += f" ({without} session{'' if without == 1 else 's'} reported no usage)"
    return text


def results_document(
    results: Sequence[FlowResult], found: Sequence[Finding], watched: object = None
) -> dict[str, Any]:
    """The results.json v1 mapping, before redaction, its summary costing
    ``watched`` too (:func:`cost_totals`)."""
    ordered = sorted(results, key=lambda r: (r.key, r.attempt))
    return {
        "schema": RESULTS_SCHEMA,
        "version": RESULTS_VERSION,
        "judgement_revision": JUDGEMENT_REVISION,
        "flows": [result.to_json() for result in ordered],
        "findings": [item.to_json() for item in sorted(found, key=_finding_order)],
        "summary": {
            "flows": len({result.key for result in ordered}),
            "attempts": len(ordered),
            "verdicts": _verdict_counts(ordered),
            "findings": _finding_counts(found),
            **cost_totals(ordered, watched),
        },
    }


def _run_header(data: object) -> tuple[Mapping[str, Any], int]:
    if not isinstance(data, Mapping):
        raise RunRecordError(f"{RUN_FILE} is not a JSON object")
    if data.get("schema") != RUN_SCHEMA:
        raise RunRecordError(f"{RUN_FILE}: 'schema' must be {RUN_SCHEMA}")
    version = data.get("version")
    if not _is_int(version) or version != RUN_VERSION:
        raise RunRecordError(f"{RUN_FILE}: 'version' must be {RUN_VERSION}")
    revision = data.get("judgement_revision")
    if not _is_int(revision) or revision < 1:
        raise RunRecordError(f"{RUN_FILE}: 'judgement_revision' must be a whole number")
    return data, revision


def run_record(data: Mapping[str, Any]) -> list[FlowContext]:
    """story-run.json v1 into its flows; RunRecordError on a bad shape."""
    record, _ = _run_header(data)
    flows = record.get("flows")
    if not isinstance(flows, list):
        raise RunRecordError(f"{RUN_FILE}: 'flows' must be a list")
    contexts: list[FlowContext] = []
    for index, item in enumerate(flows):
        try:
            contexts.append(FlowContext.from_json(item))
        except RunRecordError as error:
            raise RunRecordError(f"{RUN_FILE} flows[{index}]: {error}") from None
    seen: dict[str, set[int]] = {}
    for context in contexts:
        numbers = seen.setdefault(context.key, set())
        if context.attempt in numbers:
            raise RunRecordError(
                f"{RUN_FILE}: {context.key} attempt {context.attempt} appears twice"
            )
        numbers.add(context.attempt)
    for key, numbers in seen.items():
        if 1 not in numbers:
            raise RunRecordError(f"{RUN_FILE}: {key} has a re-run but no first attempt")
    return contexts


def _load_json(path: Path, what: str, error: Callable[[str], Exception]) -> Any:
    data = _read_bounded(path, _RECORD_MAX_BYTES, what, error)
    try:
        return _loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise error(f"{what} is not valid JSON") from None


def _load_run(run_dir: Path) -> tuple[Mapping[str, Any], int]:
    return _run_header(_load_json(run_dir / RUN_FILE, RUN_FILE, RunRecordError))


def _beneath(run_dir: Path, relative: str) -> Path:
    """``run_dir / relative``, refusing a link or a non-directory on the way."""
    current = run_dir
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            found = os.lstat(current)
        except OSError as failure:
            raise JournalError(
                f"the journal's directory {part!r} cannot be used: {_why_not(failure)}"
            ) from None
        if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode):
            raise JournalError(
                f"the journal's directory {part!r} is not a plain directory"
            )
    return run_dir / relative


def _matches_flow(journal: Journal, context: FlowContext) -> bool:
    start = journal.start
    return (
        start.story,
        start.persona,
        start.group,
        start.expectation,
        start.kind,
        start.series,
        start.attempt,
    ) == (
        context.story,
        context.persona,
        context.group,
        context.expectation,
        context.kind,
        context.series,
        context.attempt,
    )


def _flow_journal(
    run_dir: Path,
    context: FlowContext,
    unreadable: Callable[[FlowContext, str], None] | None,
) -> Journal | None:
    if context.journal is None:
        return None
    try:
        journal = read_journal(_beneath(run_dir, context.journal))
        if not _matches_flow(journal, context):
            raise JournalError("the journal's start line names another flow")
    except JournalError as error:
        if unreadable is not None:
            unreadable(context, str(error))
        return None
    return journal


def _judge_contexts(
    run_dir: Path,
    contexts: Sequence[FlowContext],
    unreadable: Callable[[FlowContext, str], None] | None,
) -> tuple[list[FlowResult], list[Finding]]:
    results: list[FlowResult] = []
    for context in contexts:
        journal = _flow_journal(run_dir, context, unreadable)
        results.append(summarize(journal, context, judge(journal, context)))
    results.sort(key=lambda r: (r.key, r.attempt))
    found = findings(results)
    return attach_findings(results, found), found


def judge_run(run_dir: Path) -> tuple[list[FlowResult], list[Finding]]:
    """Read run_dir/story-run.json and every journal it names, judge, summarize, find, attach."""
    record, _ = _load_run(run_dir)
    return _judge_contexts(run_dir, run_record(record), None)


def summary_table(
    run_dir: Path, results: Sequence[FlowResult], found: Sequence[Finding]
) -> str:
    """The §5.10 table, with a trailing newline."""
    root = run_dir.resolve()
    if _PATH_CONTROL_RE.search(str(root)):
        raise ValueError("the run directory's path holds a control character")
    ordered = sorted(results, key=lambda r: (r.key, r.attempt))
    states = {
        key: confirm(attempts).state for key, attempts in _by_key(ordered).items()
    }
    lines = [
        "ux-test-stories summary v1",
        f"run\t{root}",
        f"results\t{root / RESULTS_FILE}",
        "flow\tattempt\tverdict\tstate\tmet\tsteps\tbudget\thesitations\tdead_ends"
        "\tinjections\tfindings",
    ]
    for result in ordered:
        if not _KEY_RE.fullmatch(result.key) or result.verdict not in VERDICT_ORDER:
            raise ValueError("a flow result carries a key or verdict outside its set")
        if any(not _ISSUE_ID_RE.fullmatch(ident) for ident in result.findings):
            raise ValueError("a flow result carries a finding id outside its shape")
        met = {True: "yes", False: "no", None: "-"}[result.met_expectation]
        cells = [
            result.key,
            str(int(result.attempt)),
            result.verdict,
            states[result.key],
            met,
            "-" if result.actions_used is None else str(int(result.actions_used)),
            "-" if result.budget is None else str(int(result.budget)),
            str(len(result.notes.confusion)),
            str(len(result.notes.dead_end)),
            str(int(result.notes.injections)),
            ",".join(result.findings) or "-",
        ]
        lines.append("\t".join(cells))
    lines += [
        f"verdicts\t{_counts_line(_verdict_counts(ordered))}",
        f"findings\t{_counts_line(_finding_counts(found))}",
        f"journals\t{root / FLOWS_DIR}",
    ]
    return "\n".join(lines) + "\n"


def dump_json(value: object) -> str:
    """json.dumps(value, indent=2, ensure_ascii=False) + "\\n"."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _child(path: str, key: str) -> str:
    if _PLAIN_KEY_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"


def _first_difference(ours: object, theirs: object, path: str) -> str | None:
    """The first JSON path where ``theirs`` departs from ``ours``, named from ours only."""
    if isinstance(ours, dict):
        if not isinstance(theirs, dict):
            return path
        their_keys = list(theirs)
        for index, key in enumerate(ours):
            if index >= len(their_keys) or their_keys[index] != key:
                return _child(path, key)
            found = _first_difference(ours[key], theirs[key], _child(path, key))
            if found is not None:
                return found
        return path if len(their_keys) != len(ours) else None
    if isinstance(ours, list):
        if not isinstance(theirs, list):
            return path
        for index, item in enumerate(ours):
            if index >= len(theirs):
                return f"{path}[{index}]"
            found = _first_difference(item, theirs[index], f"{path}[{index}]")
            if found is not None:
                return found
        return f"{path}[{len(ours)}]" if len(theirs) > len(ours) else None
    if type(ours) is not type(theirs) or ours != theirs:
        return path
    return None


def _difference(text: str, target: Path) -> str | None:
    """None when ``target`` holds exactly ``text``, else the first differing JSON path."""
    try:
        existing = _read_bounded(
            target, _RECORD_MAX_BYTES, RESULTS_FILE, RunRecordError
        )
    except RunRecordError:
        return "$"
    if existing == text.encode("utf-8"):
        return None
    try:
        theirs = _loads(existing.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "$"
    return _first_difference(json.loads(text), theirs, "$") or "$"


def _write_text(path: Path, text: str) -> None:
    descriptor = os.open(path, _WRITE_FLAGS, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(text.encode("utf-8"))


def _default_scrub() -> TreeScrub:
    """The job's redactor, imported only here so judging needs nothing of commit 3's."""
    from ux_runner import artifacts

    return artifacts.Scrub(artifacts.redactor_for(os.environ))


def _say_unreadable(context: FlowContext, reason: str) -> None:
    print(
        f"unreadable journal\t{context.key}\t{context.attempt}\t{reason}",
        file=sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story_verdict.py",
        description="Judge a first-time-user story run from its journals.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    judging = commands.add_parser(
        "judge", help="judge RUN_DIR and write RUN_DIR/results.json"
    )
    judging.add_argument("run_dir", metavar="RUN_DIR")
    judging.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 when results.json would change",
    )
    judging.add_argument(
        "--summary", action="store_true", help="print the structured summary table"
    )
    judging.add_argument(
        "--rejudge",
        action="store_true",
        help="judge a run judged under another JUDGEMENT_REVISION again under "
        "this one, rewriting results.json",
    )
    return parser


def other_revision_line(revision: object) -> str:
    """What a judge says, writing nothing, of a run judged under another revision."""
    return (
        f"FAILED: this run was judged under JUDGEMENT_REVISION {revision} and this "
        f"checkout judges under {JUDGEMENT_REVISION}, so nothing was judged or "
        "written; judge it with the checkout of its own revision, or pass --rejudge "
        "to judge it under this one and rewrite its results"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check and args.rejudge:
        print("FAILED: --check writes nothing, so --rejudge has nothing to rewrite")
        return 1
    run_dir = Path(args.run_dir)
    try:
        record, revision = _load_run(run_dir)
        if revision != JUDGEMENT_REVISION:
            print(f"judgement_revision\trun={revision}\tjudge={JUDGEMENT_REVISION}")
            if not args.rejudge:
                if not args.check:
                    print(other_revision_line(revision))
                return 3
            print(
                f"WARNING: re-judged under JUDGEMENT_REVISION {JUDGEMENT_REVISION}; "
                f"the run was judged under {revision}"
            )
        results, found = _judge_contexts(run_dir, run_record(record), _say_unreadable)
    except RunRecordError as error:
        print(f"FAILED: {error}")
        return 1
    scrub = _default_scrub()
    scrub.keep(*sorted({result.series for result in results}))
    text = dump_json(
        scrub.tree(results_document(results, found, record.get("interactive")))
    )
    status = 0
    target = run_dir / RESULTS_FILE
    if args.check:
        difference = _difference(text, target)
        if difference is not None:
            print(f"different\t{RESULTS_FILE}\t{difference}")
            status = 1
    else:
        try:
            _write_text(target, text)
        except OSError as error:
            print(f"FAILED: {RESULTS_FILE} could not be written: {_why_not(error)}")
            return 1
    if args.summary:
        sys.stdout.write(summary_table(run_dir, results, found))
    return status


if __name__ == "__main__":
    sys.exit(main())
