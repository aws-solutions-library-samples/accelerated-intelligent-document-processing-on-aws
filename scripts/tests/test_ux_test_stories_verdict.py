# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/story_verdict.py: the journal reader, the truth
table, findings, results.json and the judge CLI."""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
VERDICT_PY = SCRIPTS / "ux_runner" / "story_verdict.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import section, story_verdict  # noqa: E402
from ux_runner.story_verdict import (  # noqa: E402
    Confirmation,
    FlowContext,
    FlowResult,
    Journal,
    JournalError,
    RunRecordError,
    SessionOutcome,
    Verdict,
)

SERIES = "5d0c1e9a2b7f"
STORY_HASH = "0f3a9c2e71b4"
CORPUS_HASH = "9e8d7c6b5a41"
DOCS_REF = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
HARNESS = "f23669def653f7c3a9606841cfc178c5456c1e8f"
MODEL = "us.anthropic.claude-opus-5-5"
EPOCH = 1760961234.5
STORY = "route-folder-to-profile"
TITLE = "Make one input folder use one configuration"
DECLARED = (
    "configuration-profiles.md"
    "#prefix-mappings-assigning-a-profile-by-where-a-document-lands"
)
SENTINEL = "sentinel-7f3e-must-not-be-echoed"
KEY_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
ISSUE_ID_RE = re.compile(r"[A-Z]{4}-[0-9a-f]{8}")


def flow(persona: str = "new-analyst", **changes: Any) -> FlowContext:
    story = changes.pop("story", STORY)
    key = f"{story}@{persona}"
    values: dict[str, Any] = {
        "key": key,
        "story": story,
        "persona": persona,
        "group": "Author",
        "title": TITLE,
        "expectation": "allowed",
        "kind": "commit_point",
        "documented_steps": 5,
        "needs": (),
        "declared_docs": (DECLARED,),
        "series": SERIES,
        "budget": 50,
        "journal": story_verdict.journal_path(key, changes.get("attempt", 1)),
        "audit_state": "clean",
    }
    values.update(changes)
    return FlowContext(**values)


def session(**changes: Any) -> SessionOutcome:
    values: dict[str, Any] = {
        "started_epoch": EPOCH - 5.0,
        "ended_epoch": EPOCH + 100.0,
        "timed_out": False,
        "returncode": 0,
        "stop": "end_turn",
        "is_error": False,
        "turns": 12,
        "duration_s": 105.0,
        "cost_usd": 1.23,
        "cost_usd_pinned": 1.31,
        "counted_usd": 1.31,
        "usage": {"input_tokens": 8123, "output_tokens": 6810},
    }
    values.update(changes)
    return SessionOutcome(**values)


def hung(last_t: float, idle: float) -> SessionOutcome:
    return session(timed_out=True, stop=None, ended_epoch=EPOCH + last_t + idle)


class Lines:
    """A journal written line for line the way ux_story_mcp.JournalWriter writes one."""

    def __init__(
        self,
        *,
        relay: bool = False,
        budget: int = 50,
        persona: str = "new-analyst",
        group: str = "Author",
        expectation: str = "allowed",
        kind: str = "commit_point",
        story: str = STORY,
        attempt: int = 1,
        series: str = SERIES,
        mode: str = "unattended",
    ) -> None:
        self.relay = relay
        self.budget = budget
        self.actions = 0
        self.t = 0.0
        self.records: list[dict[str, Any]] = [
            {
                "v": 1,
                "type": "start",
                "seq": 0,
                "t": 0.0,
                "story": story,
                "story_hash": STORY_HASH,
                "series": series,
                "persona": persona,
                "group": group,
                "expectation": expectation,
                "kind": kind,
                "budget": budget if relay else None,
                "relay": relay,
                "attempt": attempt,
                "docs_ref": DOCS_REF,
                "corpus_hash": CORPUS_HASH,
                "harness": HARNESS,
                "model": MODEL,
                "effort": "high",
                "mode": mode,
                "epoch": EPOCH,
            }
        ]

    def call(
        self,
        tool: str,
        cls: str,
        *,
        ok: bool = True,
        refused: str | None = None,
        label: str | None = None,
        args: dict[str, Any] | None = None,
        ids: tuple[str, ...] = (),
        counted: bool | None = None,
        terminal: str | None = None,
        after_budget: bool | None = None,
    ) -> Lines:
        if counted is None:
            counted = self.relay and cls in ("action", "guard")
        if counted:
            self.actions += 1
        self.t += 1.0
        self.records.append(
            {
                "v": 1,
                "type": "call",
                "seq": len(self.records),
                "t": self.t,
                "tool": tool,
                "class": cls,
                "counted": counted,
                "actions_used": self.actions if self.relay else None,
                "images_sent": 0 if self.relay else None,
                "ok": ok,
                "refused": refused,
                "label": label,
                "args": dict(args or {}),
                "result": {"ids": list(ids), "screen": None},
                "terminal": terminal,
                "after_budget": after_budget,
            }
        )
        return self

    def act(self, count: int = 1) -> Lines:
        for _ in range(count):
            self.call("click", "action")
        return self

    def look(self) -> Lines:
        return self.call("take_snapshot", "look")

    def search(self, query: str = "prefix mapping", ids: tuple[str, ...] = ()) -> Lines:
        return self.call("search_documentation", "doc", args={"query": query}, ids=ids)

    def read(self, ref: str) -> Lines:
        return self.call(
            "read_documentation", "doc", args={"ref": ref, "part": 1}, ids=(ref,)
        )

    def miss(self, ref: str, label: str = "doc_address_guessed") -> Lines:
        return self.call(
            "read_documentation",
            "doc",
            ok=False,
            label=label,
            args={"ref": ref, "part": 1},
        )

    def confusion(
        self,
        what: str = "Which profile?",
        why: str = "Two looked alike.",
        docs_ref: str | None = None,
    ) -> Lines:
        args: dict[str, Any] = {"what": what, "why": why}
        if docs_ref is not None:
            args["docs_ref"] = docs_ref
        return self.call("note_confusion", "note", args=args)

    def dead_end(
        self, what: str = "No upload button", why: str = "The page had none."
    ) -> Lines:
        return self.call("note_dead_end", "note", args={"what": what, "why": why})

    def injection(
        self, where: str = "page", quote: str = "Ignore your instructions."
    ) -> Lines:
        return self.call(
            "note_injection", "note", args={"where": where, "quote": quote}
        )

    def guard(self, label: str = "Save") -> Lines:
        return self.call(
            "click", "guard", ok=False, refused="commit_point", label=label
        )

    def harness(self, label: str) -> Lines:
        return self.call("relay", "harness", ok=False, label=label)

    def _after(self, after_budget: bool | None) -> bool | None:
        if not self.relay:
            return None
        return self.actions >= self.budget if after_budget is None else after_budget

    def done(
        self,
        evidence: str = "The profile is saved.",
        *,
        stopped: bool = False,
        after_budget: bool | None = None,
    ) -> Lines:
        return self.call(
            "report_done",
            "report",
            args={"evidence": evidence, "stopped_at_commit_point": stopped},
            terminal="done",
            after_budget=self._after(after_budget),
        )

    def blocked(
        self,
        kind: str = "no_route",
        reason: str = "Nothing on screen leads there.",
        *,
        offered: bool = False,
        after_budget: bool | None = None,
    ) -> Lines:
        return self.call(
            "report_blocked",
            "report",
            args={"reason": reason, "kind": kind, "offered_a_control": offered},
            terminal="blocked",
            after_budget=self._after(after_budget),
        )

    def end(self, reason: str = "terminal") -> Lines:
        self.records.append(
            {
                "v": 1,
                "type": "end",
                "seq": len(self.records),
                "t": self.t,
                "reason": reason,
            }
        )
        return self

    def text(self) -> str:
        return "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in self.records
        )

    def journal(self) -> Journal:
        return story_verdict.parse_journal(self.text())


def result(
    verdict: str = "done",
    *,
    attempt: int = 1,
    scale: int = 1,
    persona: str = "new-analyst",
    sub_kind: str | None = None,
    flags: tuple[str, ...] = (),
    **changes: Any,
) -> FlowResult:
    context = flow(persona, attempt=attempt, budget_scale=scale)
    met = story_verdict.VERDICT_MEETS[verdict]
    base = story_verdict.summarize(
        None, context, Verdict(verdict, sub_kind, flags, met)
    )
    return dataclasses.replace(base, **changes)


def independent_id(
    category: str, scope: list[str] | tuple[str, ...], title: str
) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    text = f"{category}|{','.join(sorted(scope))}|{slug}"
    return category[:4].upper() + "-" + hashlib.sha256(text.encode()).hexdigest()[:8]


Witness = Callable[[], tuple[Journal | None, FlowContext]]

ROW_VERDICTS: dict[int, Verdict] = {
    0: Verdict("not_run", "persona", (), None),
    1: Verdict("error", "launch", (), None),
    2: Verdict("error", "audit", (), None),
    3: Verdict("error", "is_error", (), None),
    4: Verdict("error", "crash", (), None),
    5: Verdict("error", "journal", (), None),
    6: Verdict("error", "signed_out", (), None),
    7: Verdict("error", "relay", (), None),
    8: Verdict("error", "hang", (), None),
    9: Verdict("not_run", "precondition", (), None),
    10: Verdict("out_of_steps", None, (), None),
    11: Verdict("offered", None, (), False),
    12: Verdict("refused_as_expected", None, (), True),
    13: Verdict("stuck", "no_route", (), False),
    14: Verdict("not_refused", None, (), False),
    15: Verdict("offered", None, ("unproven",), False),
    16: Verdict("done", None, (), True),
    17: Verdict("gave_up", "budget_exhausted", (), None),
    18: Verdict("gave_up", "refusal_limit", (), None),
    19: Verdict("gave_up", "max_turns", (), None),
    20: Verdict("gave_up", "dollar_cap", (), None),
    21: Verdict("gave_up", "wall_clock", (), None),
    22: Verdict("unclear", None, (), None),
}

ROW_WITNESSES: dict[int, Witness] = {
    0: lambda: (None, flow(not_run_reason="persona", journal=None)),
    1: lambda: (None, flow(harness_error="launch", journal=None)),
    2: lambda: (
        Lines().done().end().journal(),
        flow(audit_state="violations", session=session()),
    ),
    3: lambda: (
        Lines().end("eof").journal(),
        flow(session=session(is_error=True, stop="error")),
    ),
    4: lambda: (Lines().end("eof").journal(), flow(session=session(stop=None))),
    5: lambda: (None, flow(session=session())),
    6: lambda: (
        Lines().harness("signed_out").end("eof").journal(),
        flow(session=session()),
    ),
    7: lambda: (
        Lines(relay=True).harness("child_died").end("eof").journal(),
        flow(session=session()),
    ),
    8: lambda: (
        Lines().search().end("signal").journal(),
        flow(session=hung(1.0, story_verdict.HANG_SECONDS)),
    ),
    9: lambda: (
        Lines().blocked("precondition").end().journal(),
        flow(needs=("complete-document",), session=session()),
    ),
    10: lambda: (
        Lines(relay=True, budget=30).act(30).blocked(after_budget=True).end().journal(),
        flow(budget=30, session=session()),
    ),
    11: lambda: (
        Lines(expectation="not_offered")
        .blocked("refused", offered=True)
        .end()
        .journal(),
        flow(expectation="not_offered", session=session()),
    ),
    12: lambda: (
        Lines(expectation="refused", kind="read").blocked("refused").end().journal(),
        flow(expectation="refused", kind="read", session=session()),
    ),
    13: lambda: (Lines().blocked("no_route").end().journal(), flow(session=session())),
    14: lambda: (
        Lines(expectation="refused", kind="read").done().end().journal(),
        flow(expectation="refused", kind="read", session=session()),
    ),
    15: lambda: (
        Lines(expectation="not_offered").done().end().journal(),
        flow(expectation="not_offered", session=session()),
    ),
    16: lambda: (Lines().done().end().journal(), flow(session=session())),
    17: lambda: (
        Lines(relay=True, budget=30).act(30).end("eof").journal(),
        flow(budget=30, session=session()),
    ),
    18: lambda: (
        Lines(relay=True).act(3).end("refusal_limit").journal(),
        flow(session=session()),
    ),
    19: lambda: (
        Lines().search().end("eof").journal(),
        flow(session=session(stop="max_turns", is_error=True)),
    ),
    20: lambda: (
        Lines().end("eof").journal(),
        flow(session=session(stop="budget", is_error=True)),
    ),
    21: lambda: (
        Lines().search().end("signal").journal(),
        flow(session=hung(1.0, 30.0)),
    ),
    22: lambda: (Lines().search().end("eof").journal(), flow(session=session())),
}

#: For every row but the catch-all, one input both it and a later row answer,
#: differently. The later row is the next one wherever the two can coincide.
CONTESTS: tuple[tuple[int, int, Witness], ...] = (
    (
        0,
        1,
        lambda: (
            None,
            flow(not_run_reason="budget", harness_error="launch", journal=None),
        ),
    ),
    (
        1,
        2,
        lambda: (
            Lines().done().end().journal(),
            flow(harness_error="sign_in", audit_state="violations", session=session()),
        ),
    ),
    (
        2,
        3,
        lambda: (
            Lines().end("eof").journal(),
            flow(strays=2, session=session(is_error=True, stop="error")),
        ),
    ),
    (
        3,
        4,
        lambda: (
            Lines().end("eof").journal(),
            flow(session=session(is_error=True, stop=None)),
        ),
    ),
    (4, 5, lambda: (None, flow(session=session(stop=None)))),
    (5, 19, lambda: (None, flow(session=session(stop="max_turns")))),
    (
        6,
        7,
        lambda: (
            Lines(relay=True)
            .harness("signed_out")
            .harness("child_died")
            .end("eof")
            .journal(),
            flow(session=session()),
        ),
    ),
    (
        7,
        8,
        lambda: (
            Lines(relay=True).harness("child_died").end("signal").journal(),
            flow(session=hung(1.0, 500.0)),
        ),
    ),
    (
        8,
        9,
        lambda: (
            Lines().blocked("precondition").end("signal").journal(),
            flow(
                needs=("complete-document",),
                session=hung(1.0, story_verdict.HANG_SECONDS + 80.0),
            ),
        ),
    ),
    (
        9,
        10,
        lambda: (
            Lines(relay=True, budget=30)
            .act(30)
            .blocked("precondition", after_budget=True)
            .end()
            .journal(),
            flow(budget=30, needs=("complete-document",), session=session()),
        ),
    ),
    (
        10,
        11,
        lambda: (
            Lines(relay=True, budget=30, expectation="not_offered")
            .act(30)
            .blocked("refused", offered=True, after_budget=True)
            .end()
            .journal(),
            flow(budget=30, expectation="not_offered", session=session()),
        ),
    ),
    (
        11,
        12,
        lambda: (
            Lines(expectation="not_offered")
            .blocked("refused", offered=True)
            .end()
            .journal(),
            flow(expectation="not_offered", session=session()),
        ),
    ),
    (
        12,
        13,
        lambda: (
            Lines(expectation="refused", kind="read")
            .blocked("refused")
            .end()
            .journal(),
            flow(expectation="refused", kind="read", session=session()),
        ),
    ),
    (
        13,
        19,
        lambda: (
            Lines().blocked("wrong_outcome").end("eof").journal(),
            flow(session=session(stop="max_turns")),
        ),
    ),
    (
        14,
        16,
        lambda: (
            Lines(expectation="refused", kind="read").done().end().journal(),
            flow(expectation="refused", kind="read", session=session()),
        ),
    ),
    (
        15,
        16,
        lambda: (
            Lines(expectation="not_offered").done().end().journal(),
            flow(expectation="not_offered", session=session()),
        ),
    ),
    (
        16,
        17,
        lambda: (
            Lines(relay=True, budget=30)
            .act(30)
            .done(after_budget=True)
            .end()
            .journal(),
            flow(budget=30, session=session()),
        ),
    ),
    (
        17,
        18,
        lambda: (
            Lines(relay=True, budget=30).act(30).end("refusal_limit").journal(),
            flow(budget=30, session=session()),
        ),
    ),
    (
        18,
        19,
        lambda: (
            Lines(relay=True).act(2).end("refusal_limit").journal(),
            flow(session=session(stop="max_turns")),
        ),
    ),
    (
        19,
        21,
        lambda: (
            Lines().search().end("signal").journal(),
            flow(
                session=session(
                    stop="max_turns", timed_out=True, ended_epoch=EPOCH + 11
                )
            ),
        ),
    ),
    (
        20,
        21,
        lambda: (
            Lines().search().end("signal").journal(),
            flow(
                session=session(stop="budget", timed_out=True, ended_epoch=EPOCH + 11)
            ),
        ),
    ),
    (
        21,
        22,
        lambda: (
            Lines().search().end("signal").journal(),
            flow(session=hung(1.0, 10.0)),
        ),
    ),
)

#: Neighbouring rows no single input can satisfy together.
DISJOINT_NEIGHBOURS = frozenset({(5, 6), (13, 14), (14, 15), (19, 20)})


def first_row(journal: Journal | None, context: FlowContext) -> int:
    facts = story_verdict._facts(journal, context)
    return next(
        index for index, row in enumerate(story_verdict._ROWS) if row(facts) is not None
    )


@pytest.mark.unit
def test_the_table_has_the_twenty_three_rows_of_the_freeze():
    assert len(story_verdict._ROWS) == 23
    assert sorted(ROW_VERDICTS) == sorted(ROW_WITNESSES) == list(range(23))


@pytest.mark.unit
@pytest.mark.parametrize("row", sorted(ROW_WITNESSES))
def test_every_row_of_the_truth_table_is_pinned(row):
    journal, context = ROW_WITNESSES[row]()
    assert story_verdict.judge(journal, context) == ROW_VERDICTS[row]
    assert first_row(journal, context) == row


@pytest.mark.unit
@pytest.mark.parametrize(
    "winner, loser", [(winner, loser) for winner, loser, _ in CONTESTS]
)
def test_each_precedence_position_decides_a_contest(winner, loser):
    witness = dict(((w, lo), make) for w, lo, make in CONTESTS)[(winner, loser)]
    journal, context = witness()
    facts = story_verdict._facts(journal, context)
    rows = list(story_verdict._ROWS)
    assert rows[winner](facts) is not None and rows[loser](facts) is not None
    assert all(rows[index](facts) is None for index in range(winner))
    real = story_verdict.judge(journal, context)
    assert real == rows[winner](facts)
    swapped = rows.copy()
    swapped[winner], swapped[loser] = swapped[loser], swapped[winner]
    mutated = story_verdict._decide(swapped, facts)
    assert mutated == rows[loser](facts)
    assert mutated != real


@pytest.mark.unit
def test_the_contests_cover_every_row_and_every_neighbour_that_can_coincide():
    winners = [winner for winner, _, _ in CONTESTS]
    assert sorted(winners) == list(range(22))
    adjacent = {(w, lo) for w, lo, _ in CONTESTS if lo == w + 1}
    assert adjacent | DISJOINT_NEIGHBOURS == {(r, r + 1) for r in range(22)}
    assert not adjacent & DISJOINT_NEIGHBOURS


@pytest.mark.unit
def test_demoting_any_row_below_the_catch_all_changes_its_witness():
    for row in range(22):
        journal, context = ROW_WITNESSES[row]()
        facts = story_verdict._facts(journal, context)
        rows = list(story_verdict._ROWS)
        demoted = rows[:row] + rows[row + 1 :] + [rows[row]]
        assert story_verdict._decide(demoted, facts) != ROW_VERDICTS[row], row


@pytest.mark.unit
@pytest.mark.parametrize("reason", story_verdict.NOT_RUN_REASONS)
def test_row_0_names_each_not_run_reason(reason):
    verdict = story_verdict.judge(None, flow(not_run_reason=reason, journal=None))
    assert verdict == Verdict("not_run", reason, (), None)


@pytest.mark.unit
@pytest.mark.parametrize("cause", story_verdict.HARNESS_ERRORS)
def test_row_1_names_each_harness_error(cause):
    verdict = story_verdict.judge(
        Lines().done().end().journal(), flow(harness_error=cause, session=session())
    )
    assert verdict == Verdict("error", cause, (), None)


@pytest.mark.unit
def test_row_2_is_a_stray_process_as_well_as_an_audit_violation():
    journal = Lines().done().end().journal()
    assert story_verdict.judge(journal, flow(strays=1)).sub_kind == "audit"
    for state in ("clean", "unavailable", "not_run"):
        assert story_verdict.judge(journal, flow(audit_state=state)).verdict == "done"


@pytest.mark.unit
@pytest.mark.parametrize(
    "stop, row", [("max_turns", 19), ("budget", 20), ("end_turn", 3), ("error", 3)]
)
def test_row_3_leaves_max_turns_and_the_dollar_cap_to_gave_up(stop, row):
    journal = Lines().end("eof").journal()
    context = flow(session=session(is_error=True, stop=stop))
    assert first_row(journal, context) == row


@pytest.mark.unit
@pytest.mark.parametrize(
    "idle, cause",
    [
        (story_verdict.HANG_SECONDS, "hang"),
        (story_verdict.HANG_SECONDS - 0.5, "wall_clock"),
    ],
)
def test_the_hang_boundary_is_hang_seconds_of_quiet(idle, cause):
    journal = Lines().search().search().end("signal").journal()
    context = flow(session=hung(2.0, idle))
    assert story_verdict.judge(journal, context).sub_kind == cause


@pytest.mark.unit
def test_quiet_is_measured_from_the_last_call_not_the_start():
    quiet = story_verdict.HANG_SECONDS
    journal = Lines().search().end("signal").journal()
    assert story_verdict.judge(
        journal, flow(session=hung(0.0, quiet + 0.5))
    ).sub_kind == ("wall_clock")
    assert story_verdict.judge(journal, flow(session=hung(1.0, quiet))).sub_kind == (
        "hang"
    )
    empty = Lines().end("signal").journal()
    assert (
        story_verdict.judge(empty, flow(session=hung(0.0, quiet + 1.0))).sub_kind
        == "hang"
    )
    assert story_verdict.judge(empty, flow(session=hung(0.0, 60.0))).sub_kind == (
        "wall_clock"
    )


@pytest.mark.unit
@pytest.mark.parametrize("kind", story_verdict.BLOCKED_KINDS)
def test_row_13_carries_the_blocked_kind_and_a_precondition_without_needs_is_stuck(
    kind,
):
    journal = Lines().blocked(kind).end().journal()
    assert story_verdict.judge(journal, flow()) == Verdict("stuck", kind, (), False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "after_budget, stopped, flags",
    [
        (False, False, ()),
        (True, False, ("done_after_budget",)),
        (False, True, ("stopped_at_commit_point",)),
        (True, True, ("done_after_budget", "stopped_at_commit_point")),
    ],
)
def test_row_16_flags_are_a_sorted_subset(after_budget, stopped, flags):
    lines = Lines(relay=True, budget=30).act(30 if after_budget else 3)
    journal = lines.done(stopped=stopped, after_budget=after_budget).end().journal()
    assert story_verdict.judge(journal, flow(budget=30)) == Verdict(
        "done", None, flags, True
    )


@pytest.mark.unit
def test_without_a_relay_rows_10_16s_budget_flag_and_17_cannot_fire():
    lines = Lines().act(80).blocked().end()
    assert story_verdict.judge(lines.journal(), flow(budget=30)).verdict == "stuck"
    done = Lines().act(80).done(stopped=True).end().journal()
    assert story_verdict.judge(done, flow(budget=30)).flags == (
        "stopped_at_commit_point",
    )
    quiet = Lines().act(80).end("eof").journal()
    assert story_verdict.judge(quiet, flow(budget=30)).verdict == "unclear"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["pair", "watch"])
def test_pair_and_watch_sessions_skip_the_session_rows(mode):
    journal = Lines(mode=mode).search().end("eof").journal()
    assert story_verdict.judge(journal, flow(session=None)).verdict == "unclear"
    assert story_verdict.judge(journal, flow(session=None)).sub_kind is None


@pytest.mark.unit
def test_only_the_first_accepted_report_decides():
    lines = Lines().blocked("no_route")
    lines.call(
        "report_done",
        "report",
        args={"evidence": "Done after all.", "stopped_at_commit_point": False},
        terminal="done",
    )
    assert story_verdict.judge(lines.end().journal(), flow()).verdict == "stuck"


def _sweep_lines(
    terminal: str | None,
    expectation: str,
    kind: str,
    budget_state: bool | None,
    offered: bool,
    stopped: bool,
    blocked_kind: str,
) -> Lines:
    relay = budget_state is not None
    lines = Lines(relay=relay, budget=30, expectation=expectation, kind=kind)
    if relay:
        lines.act(30 if budget_state else 3)
        if kind == "commit_point":
            lines.guard("Save")
    lines.search()
    after = budget_state if relay else None
    if terminal == "done":
        lines.done(stopped=stopped, after_budget=after)
    elif terminal == "blocked":
        lines.blocked(blocked_kind, offered=offered, after_budget=after)
    return lines.end("terminal" if terminal else "eof")


def _table(
    terminal: str | None,
    expectation: str,
    kind: str,
    budget_state: bool | None,
    offered: bool,
    stopped: bool,
    blocked_kind: str,
    needs: tuple[str, ...],
) -> tuple[str, str | None, tuple[str, ...]]:
    """The truth table restated from the freeze, independently of the module."""
    denied = expectation in ("refused", "not_offered")
    if terminal == "blocked":
        if blocked_kind == "precondition" and needs:
            return "not_run", "precondition", ()
        if budget_state is True:
            return "out_of_steps", None, ()
        if expectation == "not_offered" and offered:
            return "offered", None, ()
        if denied:
            return "refused_as_expected", None, ()
        return "stuck", blocked_kind, ()
    if terminal == "done":
        if denied:
            if kind == "read":
                return "not_refused", None, ()
            return "offered", None, ("unproven",)
        flags = [
            name
            for name, on in (
                ("done_after_budget", budget_state is True),
                ("stopped_at_commit_point", stopped),
            )
            if on
        ]
        return "done", None, tuple(sorted(flags))
    if budget_state is True:
        return "gave_up", "budget_exhausted", ()
    return "unclear", None, ()


def _finding_shape(
    verdict: str, sub_kind: str | None, kind: str, relay: bool
) -> tuple[str, str, str] | None:
    if verdict == "not_refused":
        return "flow", "blocker", f"Author was able to: {TITLE}"
    if verdict == "stuck" and sub_kind == "refused":
        return (
            "flow",
            "blocker",
            f"Refused although docs/rbac.md allows Author: {TITLE}",
        )
    if verdict == "stuck":
        return "flow", "blocker", f"Not possible for a first-time Author: {TITLE}"
    if verdict == "offered":
        label = "Save" if relay and kind == "commit_point" else TITLE
        return "clarity", "major", f"Author is offered a control it cannot use: {label}"
    return None


@pytest.mark.unit
def test_the_full_truth_table_with_findings_order_met_and_issue_ids():
    combos = itertools.product(
        (None, "done", "blocked"),
        ("allowed", "refused", "not_offered"),
        ("read", "commit_point"),
        (None, False, True),
        (False, True),
        (False, True),
        story_verdict.BLOCKED_KINDS,
        ((), ("complete-document",)),
    )
    seen: set[str] = set()
    for combo in combos:
        terminal, expectation, kind, budget_state, offered, stopped, blocked, needs = (
            combo
        )
        journal = _sweep_lines(*combo[:7]).journal()
        context = flow(
            expectation=expectation,
            kind=kind,
            budget=30,
            needs=needs,
            session=session(),
        )
        name, sub_kind, flags = _table(*combo)
        met = story_verdict.VERDICT_MEETS[name]
        verdict = story_verdict.judge(journal, context)
        assert verdict == Verdict(name, sub_kind, flags, met), combo
        assert met is (
            True
            if name in story_verdict.GOOD_VERDICTS
            else False
            if name in story_verdict.BAD_VERDICTS
            else None
        ), combo
        assert name in story_verdict.VERDICT_ORDER
        seen.add(name)
        summary = story_verdict.summarize(journal, context, verdict)
        found = story_verdict.findings([summary])
        shape = _finding_shape(name, sub_kind, kind, budget_state is not None)
        if shape is None:
            assert found == [], combo
            continue
        assert len(found) == 1, combo
        finding = found[0]
        assert (finding.category, finding.severity, finding.title) == shape, combo
        assert finding.scope == (context.key,)
        assert finding.confirmation == "unconfirmed"
        assert finding.id == independent_id(shape[0], [context.key], shape[2])
        assert ISSUE_ID_RE.fullmatch(finding.id)
    assert seen == set(story_verdict.VERDICT_ORDER) - {"error"}


@pytest.mark.unit
def test_the_verdict_tables_agree_with_each_other():
    order = story_verdict.VERDICT_ORDER
    assert len(set(order)) == len(order) == 10
    for table in (
        story_verdict.VERDICT_MEETS,
        story_verdict.VERDICT_ICON_KEYS,
        story_verdict.VERDICT_LABELS,
    ):
        assert set(table) == set(order)
    meets = story_verdict.VERDICT_MEETS
    assert story_verdict.BAD_VERDICTS == {v for v in order if meets[v] is False}
    assert story_verdict.GOOD_VERDICTS == {v for v in order if meets[v] is True}
    assert story_verdict.RERUN_VERDICTS == story_verdict.BAD_VERDICTS
    assert story_verdict.BUDGET_RERUN_VERDICTS <= {v for v in order if meets[v] is None}
    assert order[:3] == ("not_refused", "stuck", "offered")
    assert set(story_verdict.VERDICT_ICON_KEYS.values()) <= set(section.OUTCOME_ICONS)
    assert story_verdict.ERROR_CAUSES[: len(story_verdict.HARNESS_ERRORS)] == (
        story_verdict.HARNESS_ERRORS
    )
    assert story_verdict.JUDGEMENT_REVISION == 3


@pytest.mark.unit
def test_no_verdict_label_says_blocked_or_broken():
    labels = list(story_verdict.VERDICT_LABELS.values())
    labels += list(story_verdict._STUCK_SUFFIXES.values())
    for label in labels:
        assert not re.search(r"\b(blocked|broken)\b", label, re.I), label


@pytest.mark.unit
def test_the_catalogue_constants_held_here_equal_the_catalogue_s():
    from ux_runner import stories

    assert story_verdict._EXPECTATIONS == stories.EXPECTATIONS
    assert story_verdict._KINDS == stories.KINDS
    assert story_verdict._SIGN_IN_GROUPS == stories.SIGN_IN_GROUPS
    assert story_verdict._ID_RE.pattern == stories.ID_RE.pattern


@pytest.mark.unit
def test_the_display_bounds_are_the_journal_writer_s():
    import ux_story_mcp

    assert story_verdict._WHAT_CHARS == ux_story_mcp.WHAT_CHARS
    assert story_verdict._WHY_CHARS == ux_story_mcp.WHY_CHARS
    assert story_verdict._REF_CHARS == ux_story_mcp.REF_CHARS
    assert story_verdict._EVIDENCE_CHARS == ux_story_mcp.EVIDENCE_CHARS
    assert story_verdict._REASON_CHARS == ux_story_mcp.REASON_CHARS
    assert story_verdict.MAX_JOURNAL_LINES == ux_story_mcp._JOURNAL_LINES_MAX
    assert story_verdict.MAX_JOURNAL_BYTES == ux_story_mcp._JOURNAL_BYTES_MAX


@pytest.mark.unit
def test_a_journal_the_writer_writes_is_one_the_reader_reads(tmp_path):
    import ux_story_mcp

    start = {
        "story": STORY,
        "story_hash": STORY_HASH,
        "series": SERIES,
        "persona": "platform-admin",
        "group": "Admin",
        "expectation": "allowed",
        "kind": "commit_point",
        "attempt": 1,
        "docs_ref": DOCS_REF,
        "corpus_hash": CORPUS_HASH,
        "harness": HARNESS,
        "model": MODEL,
        "effort": "high",
        "mode": "unattended",
    }
    ticks = iter(float(n) for n in range(100))
    path = tmp_path / "journal.jsonl"
    writer = ux_story_mcp.JournalWriter(
        path,
        start,
        relay=False,
        budget=None,
        clock=lambda: next(ticks),
        wall=lambda: EPOCH,
    )
    writer.call(
        tool="search_documentation",
        cls="doc",
        ok=True,
        args={"query": "prefix mapping"},
        ids=[DECLARED],
    )
    writer.call(
        tool="note_confusion",
        cls="note",
        ok=True,
        args={"what": "Which profile?", "why": "Two looked alike.", "docs_ref": None},
    )
    writer.call(
        tool="read_documentation",
        cls="doc",
        ok=False,
        label="doc_address_guessed",
        args={"ref": "/guessed/", "part": 1},
    )
    writer.call(
        tool="report_done",
        cls="report",
        ok=True,
        args={"evidence": "Saved.", "stopped_at_commit_point": True},
        terminal="done",
    )
    writer.end("terminal")
    journal = story_verdict.read_journal(path)
    assert journal.start.relay is False and journal.start.budget is None
    assert [call.cls for call in journal.calls] == ["doc", "note", "doc", "report"]
    assert journal.end is not None and journal.end.reason == "terminal"
    context = flow("platform-admin", group="Admin", session=session())
    verdict = story_verdict.judge(journal, context)
    assert verdict == Verdict("done", None, ("stopped_at_commit_point",), True)
    summary = story_verdict.summarize(journal, context, verdict)
    assert summary.searches == 1 and summary.declared_docs_found is True
    assert summary.doc_addresses_guessed == 1
    assert [note.what for note in summary.notes.confusion] == ["Which profile?"]


def _valid_relay() -> Lines:
    return Lines(relay=True).act(2).search().guard().confusion().done().end()


def _valid_own() -> Lines:
    return Lines().search().read(DECLARED).dead_end().blocked().end()


def _mutated(lines: Lines, index: int, **changes: Any) -> str:
    record = dict(lines.records[index])
    for key, value in changes.items():
        if value is _DROP:
            record.pop(key, None)
        else:
            record[key] = value
    records = list(lines.records)
    records[index] = record
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)


_DROP = object()

MALFORMED: tuple[tuple[str, Callable[[], str], int], ...] = (
    ("an unknown key", lambda: _mutated(_valid_own(), 1, zzz_extra=SENTINEL), 2),
    ("a key of another shape", lambda: _mutated(_valid_own(), 1, **{SENTINEL: 1}), 2),
    ("a missing key", lambda: _mutated(_valid_own(), 1, ok=_DROP), 2),
    ("a wrong type", lambda: _mutated(_valid_own(), 1, ok=SENTINEL), 2),
    (
        "a class outside the set",
        lambda: _mutated(_valid_own(), 1, **{"class": SENTINEL}),
        2,
    ),
    (
        "a refusal outside the set",
        lambda: _mutated(_valid_own(), 1, ok=False, refused=SENTINEL),
        2,
    ),
    ("a seq that skips", lambda: _mutated(_valid_own(), 2, seq=7), 3),
    ("a t that decreases", lambda: _mutated(_valid_own(), 2, t=0.5), 3),
    ("a second start line", lambda: _mutated(_valid_own(), 1, type="start"), 2),
    (
        "a first line that is no start",
        lambda: _mutated(_valid_own(), 0, type="call"),
        1,
    ),
    ("a version other than 1", lambda: _mutated(_valid_own(), 1, v=2), 2),
    ("a version spelled true", lambda: _mutated(_valid_own(), 1, v=True), 2),
    ("a type outside the set", lambda: _mutated(_valid_own(), 1, type=SENTINEL), 2),
    (
        "a story that is no id",
        lambda: _mutated(_valid_own(), 0, story=SENTINEL + " X"),
        1,
    ),
    ("a hash of another shape", lambda: _mutated(_valid_own(), 0, series=SENTINEL), 1),
    ("a group outside the set", lambda: _mutated(_valid_own(), 0, group=SENTINEL), 1),
    ("a mode outside the set", lambda: _mutated(_valid_own(), 0, mode=SENTINEL), 1),
    ("an empty model", lambda: _mutated(_valid_own(), 0, model=""), 1),
    ("an effort over its bound", lambda: _mutated(_valid_own(), 0, effort=SENTINEL), 1),
    ("an attempt of 3", lambda: _mutated(_valid_own(), 0, attempt=3), 1),
    ("a start t other than 0", lambda: _mutated(_valid_own(), 0, t=1.5), 1),
    ("a budget without a relay", lambda: _mutated(_valid_own(), 0, budget=50), 1),
    ("no budget with a relay", lambda: _mutated(_valid_relay(), 0, budget=None), 1),
    ("a count without a relay", lambda: _mutated(_valid_own(), 1, actions_used=1), 2),
    (
        "no count with a relay",
        lambda: _mutated(_valid_relay(), 1, actions_used=None),
        2,
    ),
    ("counted without a relay", lambda: _mutated(_valid_own(), 1, counted=True), 2),
    ("ok on a refused call", lambda: _mutated(_valid_own(), 1, refused="limit"), 2),
    ("a terminal on a search", lambda: _mutated(_valid_own(), 1, terminal="done"), 2),
    ("a terminal on a refused report", lambda: _mutated(_valid_own(), 4, ok=False), 5),
    (
        "a terminal of the other tool",
        lambda: _mutated(_valid_own(), 4, terminal="done"),
        5,
    ),
    (
        "a terminal without its arguments",
        lambda: _mutated(_valid_own(), 4, args={"reason": "x"}),
        5,
    ),
    (
        "after_budget without a relay",
        lambda: _mutated(_valid_own(), 4, after_budget=True),
        5,
    ),
    (
        "no after_budget on a relayed report",
        lambda: _mutated(_valid_relay(), 6, after_budget=None),
        7,
    ),
    (
        "after_budget on a call",
        lambda: _mutated(_valid_relay(), 1, after_budget=False),
        2,
    ),
    ("a label on an action", lambda: _mutated(_valid_relay(), 1, label=SENTINEL), 2),
    (
        "a harness label outside the set",
        lambda: _mutated(
            _valid_own(), 1, **{"class": "harness", "ok": False, "label": SENTINEL}
        ),
        2,
    ),
    (
        "a doc label outside the set",
        lambda: _mutated(_valid_own(), 2, ok=False, label=SENTINEL),
        3,
    ),
    (
        "a doc line naming another tool",
        lambda: _mutated(_valid_own(), 1, tool=SENTINEL),
        2,
    ),
    (
        "nine arguments",
        lambda: _mutated(_valid_own(), 1, args={f"a{n}": n for n in range(9)}),
        2,
    ),
    (
        "an argument that is a list",
        lambda: _mutated(_valid_own(), 1, args={"query": [SENTINEL]}),
        2,
    ),
    (
        "an argument that is a float",
        lambda: _mutated(_valid_own(), 1, args={"query": 1.5}),
        2,
    ),
    (
        "twenty-one ids",
        lambda: _mutated(
            _valid_own(), 1, result={"ids": ["a.md"] * 21, "screen": None}
        ),
        2,
    ),
    (
        "a result with another key",
        lambda: _mutated(_valid_own(), 1, result={"ids": [], "screen": None, "x": 1}),
        2,
    ),
    (
        "an id that is no text",
        lambda: _mutated(_valid_own(), 1, result={"ids": [1], "screen": None}),
        2,
    ),
    (
        "an end reason outside the set",
        lambda: _mutated(_valid_own(), 5, reason=SENTINEL),
        6,
    ),
    (
        "a line after the end",
        lambda: _valid_own().text() + _valid_own().text().split("\n")[1] + "\n",
        7,
    ),
    ("a line that is no JSON", lambda: Lines().text() + "{" + SENTINEL + "\n", 2),
    (
        "a line that is a JSON list",
        lambda: Lines().text() + '["' + SENTINEL + '"]\n',
        2,
    ),
    ("a key twice", lambda: Lines().text().replace('"v":1,', '"v":1,"v":1,', 1), 1),
    ("a NaN", lambda: Lines().text().replace('"t":0.0', '"t":NaN', 1), 1),
    (
        "an infinite number",
        lambda: Lines().search().text().replace('"t":1.0', '"t":1e999'),
        2,
    ),
    ("an empty line", lambda: Lines().text() + "\n" + Lines().text(), 2),
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text, line",
    [(make, line) for _, make, line in MALFORMED],
    ids=[m[0] for m in MALFORMED],
)
def test_a_malformed_journal_names_the_line_never_the_text(text, line):
    with pytest.raises(JournalError) as raised:
        story_verdict.parse_journal(text())
    message = str(raised.value)
    assert f"journal line {line}" in message
    assert SENTINEL not in message


@pytest.mark.unit
def test_the_valid_journals_the_malformed_ones_start_from_parse():
    relay = _valid_relay().journal()
    assert relay.start.relay is True and relay.start.budget == 50
    assert [call.actions_used for call in relay.calls] == [1, 2, 2, 3, 3, 3]
    own = _valid_own().journal()
    assert own.calls[0].ids == () and own.calls[1].ids == (DECLARED,)
    assert own.calls[3].terminal == "blocked" and own.calls[3].after_budget is None
    assert own.end is not None and own.end.seq == 5


@pytest.mark.unit
def test_an_unknown_key_of_a_plain_shape_is_named_and_any_other_is_not():
    with pytest.raises(JournalError, match="the unknown key 'zzz_extra'"):
        story_verdict.parse_journal(_mutated(_valid_own(), 1, zzz_extra=1))
    with pytest.raises(JournalError, match="has an unknown key") as raised:
        story_verdict.parse_journal(_mutated(_valid_own(), 1, **{"Odd-Key": 1}))
    assert "Odd-Key" not in str(raised.value)


@pytest.mark.unit
def test_a_torn_final_segment_is_ignored():
    whole = Lines().search().done().text()
    torn = whole + '{"v":1,"type":"call","seq":3,"t":3.0,"tool":"sea'
    journal = story_verdict.parse_journal(torn)
    assert len(journal.calls) == 2 and journal.end is None
    with pytest.raises(JournalError, match="no start line"):
        story_verdict.parse_journal(whole.split("\n")[0])
    with pytest.raises(JournalError, match="no start line"):
        story_verdict.parse_journal("")


@pytest.mark.unit
def test_the_line_and_byte_bounds_refuse_before_parsing():
    with pytest.raises(JournalError, match="5000 lines"):
        story_verdict.parse_journal("x\n" * (story_verdict.MAX_JOURNAL_LINES + 1))
    with pytest.raises(JournalError, match="bytes"):
        story_verdict.parse_journal("x" * (story_verdict.MAX_JOURNAL_BYTES + 1))
    with pytest.raises(JournalError, match="bytes"):
        story_verdict.parse_journal("é" * (story_verdict.MAX_JOURNAL_BYTES // 2 + 1))


@pytest.mark.unit
def test_read_journal_reads_a_regular_file_and_drops_a_torn_multibyte_tail(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_bytes(Lines().done().text().encode() + '{"label":"é'.encode()[:-1])
    journal = story_verdict.read_journal(path)
    assert journal.calls[0].terminal == "done"


@pytest.mark.unit
def test_read_journal_refuses_a_symlink_a_directory_and_text_that_is_not_utf8(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(Lines().text())
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(JournalError, match="symbolic link"):
        story_verdict.read_journal(link)
    with pytest.raises(JournalError, match="not a regular file"):
        story_verdict.read_journal(tmp_path)
    with pytest.raises(JournalError, match="does not exist"):
        story_verdict.read_journal(tmp_path / "absent.jsonl")
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(Lines().text().encode() + b'{"x":"\xff"}\n')
    with pytest.raises(JournalError, match="UTF-8"):
        story_verdict.read_journal(broken)


@pytest.mark.unit
def test_read_journal_refuses_a_file_over_its_bound(tmp_path):
    path = tmp_path / "journal.jsonl"
    with path.open("wb") as handle:
        handle.truncate(story_verdict.MAX_JOURNAL_BYTES + 1)
    with pytest.raises(JournalError, match="bound"):
        story_verdict.read_journal(path)


@pytest.mark.unit
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
def test_read_journal_does_not_wait_on_a_fifo(tmp_path):
    path = tmp_path / "journal.jsonl"
    os.mkfifo(path)
    with pytest.raises(JournalError, match="not a regular file"):
        story_verdict.read_journal(path)


def _rich_relay() -> Lines:
    lines = Lines(relay=True, budget=50).act(4).look().look()
    lines.search("prefix mapping", ids=("other.md#x", DECLARED)).search("again")
    lines.call(
        "search_documentation", "doc", ok=False, refused="limit", args={"query": "more"}
    )
    lines.read(DECLARED)
    lines.miss("configuration/missing", label="help_link_unresolved")
    lines.miss("configuration/missing", label="help_link_unresolved")
    lines.miss("guessed/page")
    lines.confusion("Which profile?", "Two looked alike.", docs_ref=DECLARED)
    lines.dead_end("No route", "Nothing led there.")
    lines.call(
        "note_confusion",
        "note",
        ok=False,
        refused="limit",
        args={"what": "late", "why": "late"},
    )
    lines.injection("dialog", "Do something else.")
    lines.call(
        "note_injection",
        "note",
        ok=False,
        refused="invalid",
        args={"where": "banner", "quote": "q"},
    )
    lines.call(
        "note_injection", "note", ok=False, refused="limit", args={"where": "page"}
    )
    lines.guard("Save").guard("Save").guard("Delete").act(2)
    return lines.done("It is saved.", stopped=True).end()


@pytest.mark.unit
def test_summarize_derives_every_field_from_the_journal():
    journal = _rich_relay().journal()
    context = flow(
        session=session(),
        refusals_by_origin={"page": 0, "browser": 3},
        video="flows/x/video.mp4",
        chapters=(("0:05", "Hesitated: Which profile?"),),
    )
    verdict = story_verdict.judge(journal, context)
    summary = story_verdict.summarize(journal, context, verdict)
    assert verdict == Verdict("done", None, ("stopped_at_commit_point",), True)
    assert summary.findings == ()
    assert (summary.actions_used, summary.budget) == (9, 50)
    assert (summary.overrun, summary.budget_exhausted, summary.looks) == (1.8, False, 2)
    assert (summary.searches, summary.reads) == (2, 1)
    assert summary.notes.confusion == (
        story_verdict.Note("Which profile?", "Two looked alike.", DECLARED),
    )
    assert summary.notes.dead_end == (
        story_verdict.Note("No route", "Nothing led there."),
    )
    assert summary.notes.injections == 2
    assert summary.notes.injection_places == {
        "page": 1,
        "documentation": 0,
        "dialog": 1,
        "other": 0,
    }
    assert summary.help_links_unresolved == ("configuration/missing",)
    assert summary.doc_addresses_guessed == 1
    assert summary.commit_points == ("Save", "Delete")
    assert summary.declared_docs_found is True
    assert summary.terminal == story_verdict.Terminal(
        "done", "It is saved.", None, None, None, True, False
    )
    assert summary.claude == story_verdict.ClaudeRecord(
        "end_turn",
        12,
        105.0,
        False,
        1.23,
        1.31,
        1.31,
        {"input_tokens": 8123, "output_tokens": 6810},
    )
    assert summary.audit == "clean"
    assert summary.refusals_by_origin == {"page": 0, "browser": 3}
    assert summary.video == "flows/x/video.mp4"
    assert summary.chapters == (("0:05", "Hesitated: Which profile?"),)


@pytest.mark.unit
def test_summarize_without_a_relay_leaves_effort_unknown():
    journal = Lines().act(3).search(ids=("other.md",)).blocked().end().journal()
    context = flow(session=session())
    summary = story_verdict.summarize(
        journal, context, story_verdict.judge(journal, context)
    )
    assert summary.actions_used is None and summary.budget is None
    assert summary.overrun is None and summary.budget_exhausted is None
    assert summary.looks is None
    assert summary.declared_docs_found is False
    assert summary.terminal == story_verdict.Terminal(
        "blocked", None, "Nothing on screen leads there.", "no_route", False, None, None
    )


@pytest.mark.unit
def test_summarize_of_a_flow_with_no_journal_is_empty_but_whole():
    context = flow(not_run_reason="time", journal=None)
    summary = story_verdict.summarize(None, context, story_verdict.judge(None, context))
    assert (summary.verdict, summary.sub_kind) == ("not_run", "time")
    assert (summary.searches, summary.reads, summary.doc_addresses_guessed) == (0, 0, 0)
    assert summary.terminal is None and summary.claude is None
    assert summary.notes.injection_places == dict.fromkeys(
        story_verdict.INJECTION_PLACES, 0
    )


@pytest.mark.unit
def test_a_timeout_with_no_result_is_recorded_as_a_timeout_stop():
    journal = Lines().search().end("signal").journal()
    context = flow(session=hung(1.0, 10.0))
    summary = story_verdict.summarize(
        journal, context, story_verdict.judge(journal, context)
    )
    assert summary.claude is not None and summary.claude.stop == "timeout"


@pytest.mark.unit
def test_summarize_sanitises_model_and_page_text_to_the_writer_s_bounds():
    lines = Lines(relay=True)
    lines.confusion("ping @all about #12", "w" * 500, docs_ref="r" * 400)
    lines.miss("/quick-action " + "a" * 400, label="help_link_unresolved")
    lines.guard("Save @here " + "b" * 200)
    lines.blocked("no_route", "z" * 700)
    journal = lines.end().journal()
    context = flow(chapters=tuple((f"0:{n:02d}", f"@all {n}") for n in range(45)))
    summary = story_verdict.summarize(
        journal, context, story_verdict.judge(journal, context)
    )
    note = summary.notes.confusion[0]
    assert note.what == "ping @\u200ball about #\u200b12"
    assert len(note.why) == 400 and note.why.endswith(section.ELLIPSIS)
    assert note.docs_ref is not None and len(note.docs_ref) == 300
    assert summary.help_links_unresolved[0].startswith("\\/quick-action")
    assert len(summary.help_links_unresolved[0]) == 300
    assert summary.commit_points[0].startswith("Save @\u200bhere")
    assert len(summary.commit_points[0]) == 120
    assert summary.terminal is not None and summary.terminal.reason is not None
    assert len(summary.terminal.reason) == 600
    assert len(summary.chapters) == section.MAX_CHAPTERS
    assert summary.chapters[0] == ("0:00", "@\u200ball 0")


@pytest.mark.unit
def test_the_lists_keep_unique_entries_up_to_twenty():
    lines = Lines(relay=True)
    for n in range(30):
        lines.miss(f"page-{n % 25}", label="help_link_unresolved")
        lines.guard(f"Control {n % 25}")
    journal = lines.end("eof").journal()
    summary = story_verdict.summarize(
        journal, flow(), story_verdict.judge(journal, flow())
    )
    assert summary.help_links_unresolved == tuple(f"page-{n}" for n in range(20))
    assert summary.commit_points == tuple(f"Control {n}" for n in range(20))


@pytest.mark.unit
def test_confirm_one_attempt():
    good = story_verdict.confirm([result("done")])
    assert good == Confirmation("done", "single", 1, 1, 1, False)
    assert good.label == ""
    bad = story_verdict.confirm([result("stuck", sub_kind="no_route")])
    assert bad == Confirmation("stuck", "unconfirmed", 1, 1, 1, False)
    assert bad.label == "unconfirmed (1 of 1)"


@pytest.mark.unit
def test_confirm_an_extended_rerun_takes_the_second_attempt():
    first = result("out_of_steps")
    better = result("done", attempt=2, scale=2)
    assert story_verdict.confirm([better, first]) == Confirmation(
        "done", "single", 1, 1, 2, True
    )
    worse = result("stuck", attempt=2, scale=2, sub_kind="no_route")
    confirmation = story_verdict.confirm([first, worse])
    assert confirmation == Confirmation("stuck", "unconfirmed", 1, 1, 2, True)


@pytest.mark.unit
def test_confirm_a_rerun_at_the_same_budget():
    first = result("offered", flags=("unproven",))
    agreed = story_verdict.confirm([first, result("offered", attempt=2)])
    assert agreed == Confirmation("offered", "confirmed", 2, 2, 1, False)
    assert agreed.label == "2 of 2"
    mixed = story_verdict.confirm([result("done", attempt=2), first])
    assert mixed == Confirmation("offered", "mixed", 1, 2, 1, False)
    assert mixed.label == "mixed (1 of 2)"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "sub_kind"), [("error", "launch"), ("not_run", "time")]
)
def test_a_rerun_that_measured_nothing_leaves_the_first_attempt_alone(
    verdict: str, sub_kind: str
) -> None:
    first = result("stuck", sub_kind="no_route")
    nothing = result(verdict, attempt=2, sub_kind=sub_kind)
    shown = story_verdict.confirm([first, nothing])
    assert shown == Confirmation("stuck", "unconfirmed", 1, 1, 1, False)
    extended = story_verdict.confirm(
        [result("gave_up"), result(verdict, attempt=2, scale=2, sub_kind=sub_kind)]
    )
    assert (extended.verdict, extended.state, extended.attempt) == (
        "gave_up",
        "single",
        1,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "attempts",
    [
        [],
        [result(), result(attempt=2), result(attempt=2)],
        [result(), result(persona="team-lead", attempt=2)],
        [result(), result()],
    ],
    ids=["none", "three", "two keys", "same number"],
)
def test_confirm_refuses_attempts_that_are_not_one_flow_s(attempts):
    with pytest.raises(ValueError):
        story_verdict.confirm(attempts)


@pytest.mark.unit
def test_every_flow_finding_of_the_table():
    done = story_verdict.Terminal(
        "done", "I opened the editor.", None, None, None, False, None
    )
    refused = story_verdict.Terminal(
        "blocked", None, "The save was refused.", "refused", False, None, None
    )
    lost = story_verdict.Terminal(
        "blocked", None, "No route.", "wrong_outcome", False, None, None
    )
    cases = [
        (
            result("not_refused", terminal=done),
            ("flow", "blocker", f"Author was able to: {TITLE}"),
            "docs/rbac.md says Author cannot do this, and the flow reported it done. "
            "Evidence: I opened the editor.",
        ),
        (
            result("stuck", sub_kind="refused", terminal=refused),
            (
                "flow",
                "blocker",
                f"Refused although docs/rbac.md allows Author: {TITLE}",
            ),
            "The application refused a Author the docs say may do this. "
            "The save was refused.",
        ),
        (
            result("stuck", sub_kind="wrong_outcome", terminal=lost),
            ("flow", "blocker", f"Not possible for a first-time Author: {TITLE}"),
            "A first-time Author following the documentation could not complete this "
            "(wrong outcome). No route.",
        ),
        (
            result("offered", flags=("unproven",), commit_points=("Save", "Delete")),
            ("clarity", "major", "Author is offered a control it cannot use: Delete"),
            "The API refusal was not tested: nothing was pressed.",
        ),
        (
            result("offered"),
            ("clarity", "major", f"Author is offered a control it cannot use: {TITLE}"),
            "The control was offered and then disabled, refused or failed.",
        ),
    ]
    for flow_result, (category, severity, title), body in cases:
        (finding,) = story_verdict.findings([flow_result])
        assert (finding.category, finding.severity, finding.title) == (
            category,
            severity,
            title,
        )
        assert finding.body == body
        assert finding.scope == (flow_result.key,)
        assert finding.confirmation == "unconfirmed"
        assert finding.id == independent_id(category, [flow_result.key], title)


@pytest.mark.unit
def test_no_finding_for_anything_but_a_bad_verdict():
    for verdict in story_verdict.VERDICT_ORDER:
        if verdict in story_verdict.BAD_VERDICTS:
            continue
        found = story_verdict.findings([result(verdict, doc_addresses_guessed=3)])
        assert found == [], verdict


@pytest.mark.unit
def test_a_docs_finding_scopes_every_key_with_the_address():
    first = result("done", help_links_unresolved=("help/missing", "help/other"))
    second = result(
        "done", persona="team-lead", help_links_unresolved=("help/missing",)
    )
    found = story_verdict.findings([second, first])
    assert [f.title for f in found] == [
        "A help link on screen leads to no documentation page: help/missing",
        "A help link on screen leads to no documentation page: help/other",
    ]
    assert found[0].scope == (first.key, second.key)
    assert found[1].scope == (first.key,)
    assert {f.category for f in found} == {"docs"}
    assert {f.severity for f in found} == {"major"}
    assert {f.confirmation for f in found} == {None}
    assert {f.body for f in found} == {
        "read_documentation found no page at an address the screen showed."
    }
    assert found[0].id == independent_id("docs", found[0].scope, found[0].title)


@pytest.mark.unit
def test_findings_follow_the_confirmation():
    first = result(
        "not_refused",
        terminal=story_verdict.Terminal("done", "first", None, None, None, False, None),
    )
    second = result(
        "not_refused",
        attempt=2,
        terminal=story_verdict.Terminal(
            "done", "second", None, None, None, False, None
        ),
    )
    (confirmed,) = story_verdict.findings([first, second])
    assert confirmed.confirmation == "confirmed" and confirmed.body.endswith("first")
    (mixed,) = story_verdict.findings([first, result("done", attempt=2)])
    assert mixed.confirmation == "mixed"
    extended = result(
        "stuck",
        attempt=2,
        scale=2,
        sub_kind="no_route",
        terminal=story_verdict.Terminal(
            "blocked", None, "late reason", "no_route", False, None, None
        ),
    )
    (late,) = story_verdict.findings([result("out_of_steps"), extended])
    assert late.confirmation == "unconfirmed" and late.body.endswith("late reason")
    assert (
        story_verdict.findings(
            [
                result("gave_up", sub_kind="max_turns"),
                result("done", attempt=2, scale=2),
            ]
        )
        == []
    )


@pytest.mark.unit
def test_findings_sort_blockers_first_then_category_then_title():
    found = story_verdict.findings(
        [
            result("offered", persona="a-one"),
            result("done", persona="b-two", help_links_unresolved=("x",)),
            result("stuck", persona="c-three", sub_kind="no_route"),
            result("not_refused", persona="d-four"),
        ]
    )
    assert [(f.severity, f.category) for f in found] == [
        ("blocker", "flow"),
        ("blocker", "flow"),
        ("major", "clarity"),
        ("major", "docs"),
    ]
    assert found[0].title < found[1].title


@pytest.mark.unit
def test_untrusted_parts_and_whole_titles_are_sanitised_and_bounded():
    terminal = story_verdict.Terminal(
        "done", "@all see #12 " + "e" * 400, None, None, None, False, None
    )
    (finding,) = story_verdict.findings([result("not_refused", terminal=terminal)])
    evidence = finding.body.split(" Evidence: ", 1)[1]
    assert evidence.startswith("@\u200ball see #\u200b12") and len(evidence) == 300
    (offered,) = story_verdict.findings(
        [result("offered", commit_points=("@here " + "c" * 300,))]
    )
    assert len(offered.title) == 200 and "@\u200bhere" in offered.title


@pytest.mark.unit
def test_attach_findings_marks_every_attempt_of_a_scoped_key():
    first = result("stuck", sub_kind="no_route")
    second = result("stuck", attempt=2, sub_kind="no_route")
    other = result("done", persona="team-lead")
    found = story_verdict.findings([first, second, other])
    attached = story_verdict.attach_findings([first, second, other], found)
    assert [r.findings for r in attached] == [(found[0].id,), (found[0].id,), ()]


@pytest.mark.unit
def test_issue_id_is_the_archive_formula():
    title = "Author is offered a control it cannot use: Save"
    key = "route-folder-to-profile@new-analyst"
    assert story_verdict.issue_id("clarity", [key], title) == "CLAR-720d0dba"
    assert story_verdict.issue_id("clarity", ["b@c", "a@b"], title) == (
        story_verdict.issue_id("clarity", ["a@b", "b@c"], title)
    )
    assert story_verdict.issue_id("docs", [key], title).startswith("DOCS-")
    assert story_verdict.issue_id("flow", [key], title).startswith("FLOW-")
    for scope, text in [
        (["a@b"], "Ünïcode — title!"),
        (["x@y", "a@b"], "  spaced  out  "),
    ]:
        assert story_verdict.issue_id("flow", scope, text) == independent_id(
            "flow", scope, text
        )


@pytest.mark.unit
def test_slug():
    assert story_verdict.slug("Author is offered: Save!") == "author-is-offered-save"
    assert story_verdict.slug("--Ünïcode__  ok--") == "n-code-ok"


@pytest.mark.unit
@pytest.mark.parametrize(
    "flow_result, confirmation, label",
    [
        (result("done"), None, "DONE"),
        (
            result("done", flags=("stopped_at_commit_point",)),
            None,
            "DONE, stopped before saving",
        ),
        (
            result("done", flags=("done_after_budget",)),
            None,
            "DONE, after the step budget",
        ),
        (
            result("done", flags=("done_after_budget", "stopped_at_commit_point")),
            None,
            "DONE, stopped before saving, after the step budget",
        ),
        (
            result("stuck", sub_kind="no_route"),
            None,
            "STUCK: no route (unconfirmed (1 of 1))",
        ),
        (
            result("stuck", sub_kind="wrong_outcome"),
            None,
            "STUCK: wrong outcome (unconfirmed (1 of 1))",
        ),
        (
            result("stuck", sub_kind="refused"),
            None,
            "STUCK: refused although allowed (unconfirmed (1 of 1))",
        ),
        (
            result("stuck", sub_kind="precondition"),
            None,
            "STUCK: precondition missing (unconfirmed (1 of 1))",
        ),
        (
            result("gave_up", sub_kind="budget_exhausted"),
            None,
            "GAVE UP: budget exhausted",
        ),
        (
            result("not_run", sub_kind="sign_in_refused"),
            None,
            "NOT RUN: sign in refused",
        ),
        (result("error", sub_kind="is_error"), None, "HARNESS ERROR: is error"),
        (result("refused_as_expected"), None, "REFUSED, as the docs say"),
        (result("unclear"), None, "UNCLEAR"),
        (
            result("offered", flags=("unproven",)),
            Confirmation("offered", "confirmed", 2, 2, 1, False),
            "OFFERED a control the docs say this role lacks (2 of 2)",
        ),
        (
            result("not_refused"),
            Confirmation("not_refused", "mixed", 1, 2, 1, False),
            "NOT REFUSED: did what the docs say this role cannot (mixed (1 of 2))",
        ),
    ],
)
def test_verdict_label(flow_result, confirmation, label):
    shown = confirmation or story_verdict.confirm([flow_result])
    assert story_verdict.verdict_label(flow_result, shown) == label


@pytest.mark.unit
def test_verdict_label_adds_no_suffix_from_an_attempt_of_another_verdict():
    shown = Confirmation("stuck", "mixed", 1, 2, 1, False)
    assert story_verdict.verdict_label(
        result("done", flags=("stopped_at_commit_point",)), shown
    ) == ("STUCK (mixed (1 of 2))")


FLOW_RESULT_KEYS = (
    "key story persona group title expectation kind series attempt budget_scale "
    "verdict sub_kind flags met_expectation findings documented_steps actions_used "
    "budget overrun budget_exhausted looks searches reads notes help_links_unresolved "
    "doc_addresses_guessed commit_points declared_docs_found terminal claude audit "
    "refusals_by_origin video chapters"
).split()


def _rich_result() -> FlowResult:
    journal = _rich_relay().journal()
    context = flow(
        session=session(),
        refusals_by_origin={"page": 0, "browser": 3, "selftest": 1},
        chapters=(("0:05", "Hesitated: Which profile?"),),
    )
    summary = story_verdict.summarize(
        journal, context, story_verdict.judge(journal, context)
    )
    return dataclasses.replace(summary, findings=("CLAR-3e1f0a9b",))


@pytest.mark.unit
def test_flow_result_json_keeps_the_frozen_key_order_and_round_trips():
    original = _rich_result()
    data = original.to_json()
    assert list(data) == FLOW_RESULT_KEYS
    assert list(data["notes"]) == ["confusion", "dead_end", "injection"]
    assert data["notes"]["injection"] == {
        "count": 2,
        "where": {"page": 1, "documentation": 0, "dialog": 1, "other": 0},
    }
    assert list(data["terminal"]) == [
        "type",
        "evidence",
        "reason",
        "kind",
        "offered_a_control",
        "stopped_at_commit_point",
        "after_budget",
    ]
    assert list(data["claude"]) == [
        "stop",
        "turns",
        "duration_s",
        "is_error",
        "cost_usd",
        "cost_usd_pinned",
        "counted_usd",
        "usage",
    ]
    assert data["chapters"] == [["0:05", "Hesitated: Which profile?"]]
    assert FlowResult.from_json(json.loads(json.dumps(data))) == original
    bare = result("not_run", sub_kind="since")
    assert FlowResult.from_json(json.loads(json.dumps(bare.to_json()))) == bare


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"zzz_extra": 1},
        {"verdict": SENTINEL},
        {"verdict": "stuck", "sub_kind": None, "met_expectation": False},
        {"verdict": "done", "sub_kind": "no_route"},
        {"met_expectation": False},
        {"flags": [SENTINEL]},
        {"findings": [SENTINEL]},
        {"key": "other@persona"},
        {"attempt": 3},
        {"series": SENTINEL},
        {"notes": {"confusion": [], "dead_end": []}},
        {"terminal": {"type": SENTINEL}},
        {"claude": {"stop": "end_turn"}},
        {"audit": SENTINEL},
        {"refusals_by_origin": {SENTINEL: -1}},
        {"chapters": [["only one"]]},
        {"overrun": float("nan")},
        {"searches": True},
    ],
)
def test_flow_result_from_json_refuses_any_other_shape(change):
    data = _rich_result().to_json()
    data.update(change)
    with pytest.raises(RunRecordError) as raised:
        FlowResult.from_json(data)
    assert SENTINEL not in str(raised.value)


@pytest.mark.unit
def test_flow_result_from_json_refuses_a_missing_key():
    data = _rich_result().to_json()
    del data["audit"]
    with pytest.raises(RunRecordError, match="'audit'"):
        FlowResult.from_json(data)


@pytest.mark.unit
def test_flow_context_json_has_the_field_order_and_round_trips():
    context = flow(
        attempt=2,
        budget_scale=2,
        session=session(usage=None),
        refusals_by_origin={"browser": 2},
        chapters=(("1:02", "Reported done"),),
        video="flows/x/attempt-2/video.mp4",
        strays=0,
    )
    data = context.to_json()
    assert list(data) == [f.name for f in dataclasses.fields(FlowContext)]
    assert data["journal"] == f"flows/{context.key}/attempt-2/journal.jsonl"
    assert list(data["session"]) == [f.name for f in dataclasses.fields(SessionOutcome)]
    assert FlowContext.from_json(json.loads(json.dumps(data))) == context
    never = flow(not_run_reason="persona", journal=None)
    assert FlowContext.from_json(never.to_json()) == never


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"journal": "flows/../../etc/journal.jsonl"},
        {"journal": "/abs/journal.jsonl"},
        {
            "journal": "flows/route-folder-to-profile@new-analyst/attempt-2/journal.jsonl"
        },
        {"key": "a@b"},
        {"group": "Annotator"},
        {"not_run_reason": SENTINEL},
        {"harness_error": "hang"},
        {"audit_state": SENTINEL},
        {"session": {"stop": "end_turn"}},
        {"needs": "complete-document"},
        {"budget": -1},
        {"budget_scale": 0},
    ],
)
def test_flow_context_from_json_refuses_any_other_shape(change):
    data = flow().to_json()
    data.update(change)
    with pytest.raises(RunRecordError) as raised:
        FlowContext.from_json(data)
    assert SENTINEL not in str(raised.value)


@pytest.mark.unit
def test_finding_json_round_trips_and_refuses_other_shapes():
    (finding,) = story_verdict.findings([result("offered", flags=("unproven",))])
    data = finding.to_json()
    assert list(data) == [
        "id",
        "category",
        "severity",
        "title",
        "body",
        "scope",
        "confirmation",
    ]
    assert story_verdict.Finding.from_json(json.loads(json.dumps(data))) == finding
    for change in (
        {"id": "clar-1234"},
        {"scope": []},
        {"severity": "minor"},
        {"category": "x"},
    ):
        with pytest.raises(RunRecordError):
            story_verdict.Finding.from_json({**data, **change})


@pytest.mark.unit
def test_results_document_sorts_counts_and_summarises_by_confirmation():
    first = result("stuck", persona="new-analyst", sub_kind="no_route")
    second = result("done", persona="new-analyst", attempt=2)
    third = result("offered", persona="a-lead", flags=("unproven",))
    fourth = result("done", persona="team-lead", help_links_unresolved=("x",))
    found = story_verdict.findings([first, second, third, fourth])
    attached = story_verdict.attach_findings([second, fourth, first, third], found)
    document = story_verdict.results_document(attached, found)
    assert list(document) == [
        "schema",
        "version",
        "judgement_revision",
        "flows",
        "findings",
        "summary",
    ]
    assert document["schema"] == "ux-test-stories/results"
    assert (document["version"], document["judgement_revision"]) == (
        1,
        story_verdict.JUDGEMENT_REVISION,
    )
    assert [(f["key"], f["attempt"]) for f in document["flows"]] == [
        ("route-folder-to-profile@a-lead", 1),
        ("route-folder-to-profile@new-analyst", 1),
        ("route-folder-to-profile@new-analyst", 2),
        ("route-folder-to-profile@team-lead", 1),
    ]
    assert [f["category"] for f in document["findings"]] == ["flow", "clarity", "docs"]
    assert document["summary"] == {
        "flows": 3,
        "attempts": 4,
        "verdicts": {"stuck": 1, "offered": 1, "done": 1},
        "findings": {"flow/blocker": 1, "clarity/major": 1, "docs/major": 1},
        "counted_usd": 0.0,
        "pinned_usd": 0.0,
        "sessions_without_usage": 0,
    }
    assert list(document["summary"]["verdicts"]) == ["stuck", "offered", "done"]
    empty = story_verdict.results_document([], [])
    assert empty["summary"] == {
        "flows": 0,
        "attempts": 0,
        "verdicts": {},
        "findings": {},
        "counted_usd": 0.0,
        "pinned_usd": 0.0,
        "sessions_without_usage": 0,
    }


def _claude(counted: float, pinned: float | None) -> story_verdict.ClaudeRecord:
    return story_verdict.ClaudeRecord(
        stop="end_turn",
        turns=3,
        duration_s=60.0,
        is_error=False,
        cost_usd=counted,
        cost_usd_pinned=pinned,
        counted_usd=counted,
        usage=None if pinned is None else {"output_tokens": 10},
    )


@pytest.mark.unit
def test_the_summary_totals_every_sessions_cost_and_says_what_it_cannot_price() -> None:
    """Each attempt's session counts once, a re-run beside the first; an attempt that
    never started one costs nothing; and a session that reported no usage counts at
    what the budget counted it at, with no pinned figure, which the summary counts
    so a reader knows the pinned total is a floor."""
    results = [
        result("done", claude=_claude(1.881593, 1.770643)),
        result("stuck", persona="team-lead", claude=_claude(0.563115, 0.563115)),
        result(
            "stuck",
            persona="team-lead",
            attempt=2,
            scale=2,
            claude=_claude(0.474681, 0.474681),
        ),
        result("error", persona="a-lead", sub_kind="hang", claude=_claude(4.0, None)),
        result("not_run", persona="b-lead", sub_kind="budget", claude=None),
    ]
    found = story_verdict.findings(results)
    document = story_verdict.results_document(
        story_verdict.attach_findings(results, found), found
    )
    summary = document["summary"]
    assert (
        summary["counted_usd"],
        summary["pinned_usd"],
        summary["sessions_without_usage"],
    ) == (6.919389, 2.808439, 1)
    assert story_verdict.cost_text(summary) == (
        "$6.92 counted, $2.81 at the pinned rates (1 session reported no usage)"
    )
    two = {**summary, "sessions_without_usage": 2}
    assert story_verdict.cost_text(two).endswith("(2 sessions reported no usage)")
    priced = story_verdict.cost_totals(results[:3])
    assert story_verdict.cost_text(priced) == (
        "$2.92 counted, $2.81 at the pinned rates"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("watched", "totals"),
    [
        ({"counted_usd": 3.25, "cost_usd_pinned": 0.5}, (4.25, 1.5, 0)),
        ({"counted_usd": None, "cost_usd_pinned": None}, (1.0, 1.0, 1)),
        ({"counted_usd": 3.25}, (4.25, 1.0, 1)),
        ({"counted_usd": -2.0, "cost_usd_pinned": -1.0}, (1.0, 1.0, 1)),
        ({"counted_usd": True, "cost_usd_pinned": "0.5"}, (1.0, 1.0, 1)),
        ({"counted_usd": math.inf, "cost_usd_pinned": math.nan}, (1.0, 1.0, 1)),
        (None, (1.0, 1.0, 0)),
        ([3.25, 0.5], (1.0, 1.0, 0)),
    ],
)
def test_a_watched_session_counts_as_one_session_of_the_run(
    watched: object, totals: tuple[float, float, int]
) -> None:
    """A watched run's session is the record's ``interactive``, not a flow result's,
    so its cost counts beside the results' own; a cost it does not hold as a number
    of dollars counts as nothing counted and no usage reported, and a record with
    no watched session adds none."""
    results = [result("done", claude=_claude(1.0, 1.0))]
    found = story_verdict.cost_totals(results, watched)
    assert (
        found["counted_usd"],
        found["pinned_usd"],
        found["sessions_without_usage"],
    ) == totals
    document = story_verdict.results_document(results, [], watched)
    assert {key: document["summary"][key] for key in found} == found


def run_document(
    contexts: list[FlowContext], *, revision: int = story_verdict.JUDGEMENT_REVISION
) -> dict[str, Any]:
    return {
        "schema": "ux-test-stories/run",
        "version": 1,
        "mode": "local",
        "shard": None,
        "harness_sha": HARNESS,
        "harness_dirty": False,
        "catalogue_sha256": "ab" * 32,
        "prompt_revision": 1,
        "judgement_revision": revision,
        "relay_revision": 0,
        "model": MODEL,
        "effort": "high",
        "region": "us-east-1",
        "versions": {"claude_code": "2.1.281", "chrome_devtools_mcp": "1.10.1"},
        "stack": {
            "name": "idp-ux-nightly",
            "version": "0.6.12.dev1",
            "build": "2026-10-19",
        },
        "stack_state": None,
        "stack_changed_mid_run": False,
        "ui_source": {"kind": "deployed"},
        "docs": {
            "ref": DOCS_REF,
            "corpus_hash": CORPUS_HASH,
            "source": "git",
            "dirty": None,
        },
        "limits": {"step_budget_multiple": 10, "step_budget_floor": 30},
        "flows": [context.to_json() for context in contexts],
        "credential_scope": {"claude": "scoped", "render": None},
        "spend_usd": 2.62,
    }


def write_run(
    root: Path,
    flows: list[tuple[FlowContext, str | None]],
    *,
    revision: int = story_verdict.JUDGEMENT_REVISION,
) -> Path:
    run = root / "ux-test-stories"
    run.mkdir()
    for context, text in flows:
        if text is not None and context.journal is not None:
            path = run / context.journal
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    record = run_document([context for context, _ in flows], revision=revision)
    (run / "story-run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return run


@pytest.mark.unit
def test_run_record_reads_the_flows():
    contexts = [flow(), flow("team-lead", attempt=1), flow(attempt=2, budget_scale=2)]
    assert story_verdict.run_record(run_document(contexts)) == contexts


@pytest.mark.unit
@pytest.mark.parametrize(
    "change, words",
    [
        ({"schema": "ux-test-stories/results"}, "'schema'"),
        ({"version": 2}, "'version'"),
        ({"judgement_revision": "1"}, "'judgement_revision'"),
        ({"flows": {}}, "'flows'"),
        ({"flows": [{"key": SENTINEL}]}, "flows[0]"),
        ({"flows": [flow().to_json(), flow().to_json()]}, "appears twice"),
        ({"flows": [flow(attempt=2).to_json()]}, "no first attempt"),
    ],
)
def test_run_record_refuses_a_bad_shape(change, words):
    data = {**run_document([flow()]), **change}
    with pytest.raises(RunRecordError) as raised:
        story_verdict.run_record(data)
    assert words in str(raised.value)
    assert SENTINEL not in str(raised.value)


@pytest.mark.unit
def test_judge_run_judges_each_flow_from_its_journal(tmp_path):
    good = flow("platform-admin", group="Admin", session=session())
    bad = flow(expectation="not_offered", session=session())
    never = flow("team-lead", not_run_reason="persona", journal=None)
    run = write_run(
        tmp_path,
        [
            (good, Lines(persona="platform-admin", group="Admin").done().end().text()),
            (bad, Lines(expectation="not_offered").done().end().text()),
            (never, None),
        ],
    )
    results, found = story_verdict.judge_run(run)
    assert [(r.key, r.verdict) for r in results] == [
        ("route-folder-to-profile@new-analyst", "offered"),
        ("route-folder-to-profile@platform-admin", "done"),
        ("route-folder-to-profile@team-lead", "not_run"),
    ]
    assert [f.category for f in found] == ["clarity"]
    assert results[0].findings == (found[0].id,)


@pytest.mark.unit
def test_an_unusable_journal_is_that_flow_s_journal_error(tmp_path):
    missing = flow("a-one", session=session())
    mismatched = flow("b-two", session=session())
    linked = flow("c-three", session=session())
    torn = flow("d-four", session=session())
    run = write_run(
        tmp_path,
        [
            (missing, None),
            (mismatched, Lines(persona="someone-else").done().end().text()),
            (linked, None),
            (torn, Lines(persona="d-four").done().text() + '{"v":1,"type":"end"'),
        ],
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "journal.jsonl").write_text(
        Lines(persona="c-three").done().end().text()
    )
    (run / "flows" / linked.key).symlink_to(elsewhere, target_is_directory=True)
    results, _ = story_verdict.judge_run(run)
    shown = {r.persona: (r.verdict, r.sub_kind) for r in results}
    assert shown == {
        "a-one": ("error", "journal"),
        "b-two": ("error", "journal"),
        "c-three": ("error", "journal"),
        "d-four": ("done", None),
    }


def _golden_flows() -> list[tuple[FlowContext, str]]:
    read = flow(
        story="find-where-to-start",
        title="Find where to start",
        kind="read",
        documented_steps=3,
        declared_docs=("quick-start.md#web-ui",),
        budget=30,
        session=session(),
    )
    offered = flow(expectation="not_offered", session=session())
    admin = flow("platform-admin", group="Admin", session=session())
    return [
        (
            read,
            Lines(story="find-where-to-start", kind="read")
            .search("where to start", ids=("quick-start.md#web-ui",))
            .read("quick-start.md#web-ui")
            .dead_end()
            .injection()
            .done("The upload page opened.")
            .end()
            .text(),
        ),
        (
            offered,
            Lines(relay=True, expectation="not_offered")
            .act(8)
            .guard("Save")
            .look()
            .done("The Save button was offered.", stopped=True)
            .end()
            .text(),
        ),
        (
            admin,
            Lines(relay=True, persona="platform-admin", group="Admin")
            .act(13)
            .confusion()
            .look()
            .act(1)
            .done(stopped=True)
            .end()
            .text(),
        ),
    ]


GOLDEN_TABLE = """\
ux-test-stories summary v1
run\t{run}
results\t{run}/results.json
flow\tattempt\tverdict\tstate\tmet\tsteps\tbudget\thesitations\tdead_ends\tinjections\tfindings
find-where-to-start@new-analyst\t1\tdone\tsingle\tyes\t-\t-\t0\t1\t1\t-
route-folder-to-profile@new-analyst\t1\toffered\tunconfirmed\tno\t9\t50\t0\t0\t0\tCLAR-720d0dba
route-folder-to-profile@platform-admin\t1\tdone\tsingle\tyes\t14\t50\t1\t0\t0\t-
verdicts\toffered=1,done=2
findings\tclarity/major=1
journals\t{run}/flows
"""


class FakeScrub:
    """A TreeScrub that records what it keeps and redacts one known value."""

    def __init__(self) -> None:
        self.kept: list[str] = []

    def keep(self, *values: str) -> None:
        self.kept.extend(values)

    def tree(self, value: object, *, fields: bool = False) -> object:
        if isinstance(value, str):
            return value.replace("hunter2-persona-secret", "[redacted]")
        if isinstance(value, dict):
            return {key: self.tree(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        return value


@pytest.fixture
def scrub(monkeypatch: pytest.MonkeyPatch) -> FakeScrub:
    fake = FakeScrub()
    monkeypatch.setattr(story_verdict, "_default_scrub", lambda: fake)
    return fake


def _table_tokens_are_closed(text: str) -> None:
    lines = text.splitlines()
    assert lines[0] == "ux-test-stories summary v1"
    header = lines[3].split("\t")
    assert len(header) == 11
    for line in lines[4:-3]:
        cells = line.split("\t")
        assert len(cells) == 11
        key, attempt, verdict, state, met, steps, budget, *counts, ids = cells
        assert KEY_RE.fullmatch(key)
        assert attempt in ("1", "2")
        assert verdict in story_verdict.VERDICT_ORDER
        assert state in story_verdict.CONFIRMATION_STATES
        assert met in ("yes", "no", "-")
        assert all(re.fullmatch(r"[0-9]+|-", cell) for cell in (steps, budget))
        assert all(re.fullmatch(r"[0-9]+", cell) for cell in counts)
        assert ids == "-" or all(ISSUE_ID_RE.fullmatch(i) for i in ids.split(","))
    for line in lines[-3:-1]:
        name, value = line.split("\t")
        assert name in ("verdicts", "findings")
        assert value == "none" or all(
            re.fullmatch(r"[a-z_/]+=[0-9]+", part) for part in value.split(",")
        )


@pytest.mark.unit
def test_judge_summary_prints_the_frozen_table(tmp_path, scrub, capsys):
    run = write_run(tmp_path, _golden_flows())
    assert story_verdict.main(["judge", str(run), "--summary"]) == 0
    out = capsys.readouterr().out
    assert out == GOLDEN_TABLE.format(run=run.resolve())
    _table_tokens_are_closed(out)


@pytest.mark.unit
def test_the_table_repeats_the_flow_s_state_on_every_attempt_row(
    tmp_path, scrub, capsys
):
    first = flow(expectation="not_offered", session=session())
    second = flow(expectation="not_offered", attempt=2, session=session())
    text = Lines(expectation="not_offered").done().end()
    again = Lines(expectation="not_offered", attempt=2).done().end()
    run = write_run(tmp_path, [(first, text.text()), (second, again.text())])
    assert story_verdict.main(["judge", str(run), "--summary"]) == 0
    out = capsys.readouterr().out
    rows = [line.split("\t") for line in out.splitlines()[4:6]]
    assert [(r[1], r[2], r[3]) for r in rows] == [
        ("1", "offered", "confirmed"),
        ("2", "offered", "confirmed"),
    ]
    assert rows[0][10] == rows[1][10] != "-"
    assert "verdicts\toffered=1\n" in out
    _table_tokens_are_closed(out)


@pytest.mark.unit
def test_an_empty_run_reads_none(tmp_path, scrub, capsys):
    run = write_run(tmp_path, [])
    assert story_verdict.main(["judge", str(run), "--summary"]) == 0
    out = capsys.readouterr().out
    assert "verdicts\tnone\nfindings\tnone\n" in out
    _table_tokens_are_closed(out)


@pytest.mark.unit
def test_summary_table_refuses_a_path_or_token_outside_its_shapes(tmp_path):
    results, found = [result("done")], []
    with pytest.raises(ValueError):
        story_verdict.summary_table(tmp_path / "a\tb", results, found)
    with pytest.raises(ValueError):
        story_verdict.summary_table(
            tmp_path, [result("done", findings=("not an id",))], found
        )
    with pytest.raises(ValueError):
        story_verdict.summary_table(
            tmp_path, [dataclasses.replace(result(), key="x y")], found
        )


@pytest.mark.unit
def test_judge_writes_the_scrubbed_results_and_keeps_the_series(
    tmp_path, scrub, capsys
):
    flows = _golden_flows()
    secret = flow("team-lead", series="0123456789ab", session=session())
    flows.append(
        (
            secret,
            Lines(persona="team-lead", series="0123456789ab")
            .confusion("hunter2-persona-secret")
            .done()
            .end()
            .text(),
        )
    )
    run = write_run(tmp_path, flows)
    assert story_verdict.main(["judge", str(run)]) == 0
    assert capsys.readouterr().out == ""
    written = (run / "results.json").read_text(encoding="utf-8")
    results, found = story_verdict.judge_run(run)
    assert written == story_verdict.dump_json(
        scrub.tree(story_verdict.results_document(results, found))
    )
    assert "hunter2-persona-secret" not in written and "[redacted]" in written
    assert sorted(scrub.kept) == ["0123456789ab", SERIES]
    assert (
        written.endswith("}\n")
        and json.loads(written)["schema"] == "ux-test-stories/results"
    )


@pytest.mark.unit
def test_check_reproduces_the_bytes_and_names_the_first_difference(
    tmp_path, scrub, capsys
):
    run = write_run(tmp_path, _golden_flows())
    assert story_verdict.main(["judge", str(run)]) == 0
    before = (run / "results.json").read_bytes()
    assert story_verdict.main(["judge", str(run), "--check"]) == 0
    assert capsys.readouterr().out == ""
    assert (run / "results.json").read_bytes() == before
    data = json.loads(before)
    data["flows"][1]["verdict"] = "done"
    (run / "results.json").write_text(story_verdict.dump_json(data))
    assert story_verdict.main(["judge", str(run), "--check"]) == 1
    assert capsys.readouterr().out == "different\tresults.json\t$.flows[1].verdict\n"
    data = json.loads(before)
    data["summary"]["findings"] = {}
    (run / "results.json").write_text(story_verdict.dump_json(data))
    assert story_verdict.main(["judge", str(run), "--check"]) == 1
    assert capsys.readouterr().out == (
        'different\tresults.json\t$.summary.findings["clarity/major"]\n'
    )
    (run / "results.json").write_bytes(before.replace(b"\n", b"\r\n"))
    assert story_verdict.main(["judge", str(run), "--check"]) == 1
    assert capsys.readouterr().out == "different\tresults.json\t$\n"
    (run / "results.json").unlink()
    assert story_verdict.main(["judge", str(run), "--check", "--summary"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("different\tresults.json\t$\nux-test-stories summary v1\n")
    assert not (run / "results.json").exists()


@pytest.mark.unit
def test_check_reports_a_judgement_revision_instead_of_a_difference(
    tmp_path, scrub, capsys
):
    current = story_verdict.JUDGEMENT_REVISION
    other = current + 1
    line = f"judgement_revision\trun={other}\tjudge={current}"
    run = write_run(tmp_path, _golden_flows(), revision=other)
    assert story_verdict.main(["judge", str(run), "--check"]) == 3
    assert capsys.readouterr().out == line + "\n"
    assert not (run / "results.json").exists()
    assert story_verdict.main(["judge", str(run), "--summary"]) == 3
    out = capsys.readouterr().out.splitlines()
    assert out == [line, story_verdict.other_revision_line(other)]
    assert not (run / "results.json").exists()
    assert story_verdict.main(["judge", str(run), "--rejudge"]) == 0
    assert capsys.readouterr().out.startswith(line + "\nWARNING: re-judged under")
    assert (
        json.loads((run / "results.json").read_text())["judgement_revision"] == current
    )
    assert story_verdict.main(["judge", str(run), "--check", "--rejudge"]) == 1


@pytest.mark.unit
def test_an_unreadable_story_run_is_a_failure(tmp_path, scrub, capsys):
    assert story_verdict.main(["judge", str(tmp_path)]) == 1
    assert capsys.readouterr().out == (
        "FAILED: story-run.json cannot be opened: it does not exist\n"
    )
    (tmp_path / "story-run.json").write_text("{" + SENTINEL)
    assert story_verdict.main(["judge", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert out == "FAILED: story-run.json is not valid JSON\n"
    (tmp_path / "story-run.json").write_text(
        json.dumps(run_document([]) | {"flows": [{"key": SENTINEL}]})
    )
    assert story_verdict.main(["judge", str(tmp_path), "--check"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAILED: story-run.json flows[0]: ") and SENTINEL not in out


@pytest.mark.unit
def test_an_unreadable_journal_is_never_a_cli_failure(tmp_path, scrub, capsys):
    context = flow(session=session())
    run = write_run(tmp_path, [(context, '{"v":1,"type":"start"}\n')])
    assert story_verdict.main(["judge", str(run), "--summary"]) == 0
    captured = capsys.readouterr()
    assert "\terror\tsingle\t-\t" in captured.out
    assert captured.err.startswith(
        f"unreadable journal\t{context.key}\t1\tjournal line 1"
    )


@pytest.mark.unit
def test_results_json_is_never_written_through_a_link(tmp_path, scrub, capsys):
    run = write_run(tmp_path, _golden_flows())
    target = tmp_path / "target.txt"
    target.write_text("untouched")
    (run / "results.json").symlink_to(target)
    assert story_verdict.main(["judge", str(run)]) == 1
    assert capsys.readouterr().out == (
        "FAILED: results.json could not be written: "
        "it is a symbolic link, which is never followed\n"
    )
    assert target.read_text() == "untouched"


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [[], ["judge"], ["judge", "a", "b"], ["judge", "a", "--fix"], ["review", "a"]],
)
def test_usage_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        story_verdict.main(argv)
    assert raised.value.code == 2


@pytest.mark.unit
def test_importing_the_judge_loads_only_the_standard_library_section_and_the_floor():
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SCRIPTS)!r})
        before = set(sys.modules)
        from ux_runner import story_verdict
        added = set(sys.modules) - before
        print("\\n".join(sorted(
            name for name in added
            if name.split(".")[0] not in sys.stdlib_module_names
        )))
        """
    )
    found = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert found.stdout.split() == [
        "ux_runner",
        "ux_runner.python_floor",
        "ux_runner.section",
        "ux_runner.story_verdict",
        "ux_runner.ui_change",
    ]


@pytest.mark.unit
def test_run_by_path_it_answers_help_and_usage():
    shown = subprocess.run(
        [sys.executable, "-I", "-B", str(VERDICT_PY), "judge", "--help"],
        capture_output=True,
        text=True,
    )
    assert shown.returncode == 0
    assert "--check" in shown.stdout and "--summary" in shown.stdout
    usage = subprocess.run(
        [sys.executable, "-I", "-B", str(VERDICT_PY), "judge"],
        capture_output=True,
        text=True,
    )
    assert usage.returncode == 2


@pytest.mark.unit
@pytest.mark.skipif(
    not (SCRIPTS / "ux_runner" / "artifacts.py").is_file(),
    reason="the judge CLI redacts through ux_runner.artifacts, which arrives in commit 3",
)
def test_run_by_path_it_judges_through_the_real_redactor(tmp_path):
    run = write_run(tmp_path, _golden_flows())
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    judged = subprocess.run(
        [sys.executable, "-I", "-B", str(VERDICT_PY), "judge", str(run), "--summary"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert judged.returncode == 0, judged.stdout + judged.stderr
    assert judged.stdout == GOLDEN_TABLE.format(run=run.resolve())
    checked = subprocess.run(
        [sys.executable, "-I", "-B", str(VERDICT_PY), "judge", str(run), "--check"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert (checked.returncode, checked.stdout) == (0, "")


@pytest.mark.unit
def test_a_session_that_ends_on_its_own_after_a_quiet_spell_is_not_hung() -> None:
    journal = Lines().done().end().journal()
    quiet = session(ended_epoch=EPOCH + 1.0 + story_verdict.HANG_SECONDS + 60.0)
    assert story_verdict.judge(journal, flow(session=quiet)).verdict == "done"
    timed_out = dataclasses.replace(quiet, timed_out=True, stop=None)
    assert story_verdict.judge(journal, flow(session=timed_out)) == Verdict(
        "error", "hang", (), None
    )


def _summarized(lines: Lines, context: FlowContext) -> FlowResult:
    journal = lines.journal()
    return story_verdict.summarize(
        journal, context, story_verdict.judge(journal, context)
    )


@pytest.mark.unit
def test_the_budget_is_exhausted_from_its_last_step_on() -> None:
    context = flow(budget=30, session=session())
    for actions, exhausted in ((29, False), (30, True)):
        lines = Lines(relay=True, budget=30).act(actions).blocked().end()
        assert _summarized(lines, context).budget_exhausted is exhausted, actions


@pytest.mark.unit
def test_commit_points_name_only_the_saves_the_guard_refused() -> None:
    lines = (
        Lines(relay=True)
        .guard("Save")
        .call("click", "guard", ok=False, refused="session_end", label="Sign out")
        .blocked()
        .end()
    )
    assert _summarized(lines, flow(session=session())).commit_points == ("Save",)


@pytest.mark.unit
def test_a_declared_page_is_found_only_when_a_search_returned_it() -> None:
    context = flow(session=session())
    searched = Lines().search(ids=(DECLARED,)).done().end()
    assert _summarized(searched, context).declared_docs_found is True
    read = Lines().read(DECLARED).done().end()
    assert _summarized(read, context).declared_docs_found is False


@pytest.mark.unit
def test_a_label_takes_its_detail_only_from_an_attempt_of_the_verdict_it_shows() -> (
    None
):
    shown = result("gave_up", sub_kind="budget_exhausted")
    unmeasured = result("error", attempt=2, sub_kind="launch")
    confirmation = story_verdict.confirm([shown, unmeasured])
    assert confirmation.verdict == "gave_up"
    assert story_verdict.verdict_label(shown, confirmation) == (
        "GAVE UP: budget exhausted"
    )
    assert story_verdict.verdict_label(unmeasured, confirmation) == "GAVE UP"


@pytest.mark.unit
def test_the_verdict_counts_follow_an_extended_rerun() -> None:
    first = result("gave_up", sub_kind="budget_exhausted")
    second = result("done", attempt=2, scale=2)
    document = story_verdict.results_document([first, second], [])
    assert document["summary"]["verdicts"] == {"done": 1}
