# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/story_section.py, the MR reply for story runs.

Three things are pinned. The reply is exactly its layout, against golden text.
Every MR-authored or model-derived string in it is sanitised, so a CHANGELOG lead
or a note can mention nobody, link nothing, open no HTML comment and break no table
cell. And the story marker cannot be confused with PR 1's markers in either
direction: ``section.UX_MARKER_RE`` and ``ai_mr_review.MARKER_RE``, the latter read
from that file's source rather than imported, match nothing this module writes, and
``STORY_MARKER_RE`` matches nothing PR 1 writes. A story marker counts only as a
note's first character.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
AI_MR_REVIEW = SCRIPTS / "sdlc" / "ai_mr_review.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import chrome, section, story_section, story_verdict  # noqa: E402
from ux_runner.changelog_select import ChosenLine  # noqa: E402

HEAD = "0123456789abcdef" * 2 + "01234567"
OTHER_HEAD = "fedcba9876543210" * 2 + "fedcba98"
SEL = "9e8d7c6b5a41"
VIDEO = "![UX story](/uploads/0123456789abcdef0123456789abcdef/ux-story-mr812-01234567-1.mp4)"
SECOND_VIDEO = "![UX story](/uploads/fedcba9876543210fedcba9876543210/ux-story-mr812-01234567-2.mp4)"
UX_TEST_VIDEO = (
    "![UX test](/uploads/0123456789abcdef0123456789abcdef/ux-test-mr812-01234567.mp4)"
)
MODEL = "us.anthropic.claude-opus-5-5"
STACK = "idp-ux-nightly"
STACK_FACTS = section.StackFacts(
    name=STACK, region="us-east-1", version="0.6.12", build="2026-10-16 21:07:20"
)
UNREAD_FACTS = section.StackFacts(name=STACK, region="us-east-1")
TITLE = "Make one input folder use one configuration"
ROUTE_LEAD = (
    "Configuration Profiles can be assigned by S3 prefix, so the destination "
    "decides the configuration."
)
AIOHTTP_LEAD = "`aiohttp` is updated to 3.14.5"
AIOHTTP_SHOWN = "'aiohttp' is updated to 3.14.5"
HELP_LINK = "/configuration-profiles/#prefix-mappings"
HELP_LINK_SHOWN = "\\" + HELP_LINK

GOLDEN = f"""\
<!-- ux-story: sha={HEAD} rev={story_section.STORY_REPLY_REVISION} sel={SEL} kind=run upload=456 -->
### First-time-user stories for `01234567`

Chosen from this MR's CHANGELOG lines:
- Added: `{ROUTE_LEAD}` -> route-folder-to-profile
- Fixed: `{AIOHTTP_SHOWN}` -> no story

| Story | As | Expected | Verdict | Steps | Hesitations |
|---|---|---|---|---|---|
| {TITLE} | new analyst | not offered | ❌ OFFERED a control the docs say this role lacks (2 of 2) | 9 of 50 | 0 |
| {TITLE} | platform admin | allowed | ✅ DONE, stopped before saving | 14 of 50 | 1 |

**Where a first-time user hesitated**
- {TITLE} as platform admin: The prefix field gave no example

**Docs vs screen**
- A help link on screen leads to no documentation page: {HELP_LINK_SHOWN}
- {TITLE} as new analyst: The preview showed a mapping that could not be saved

Injection attempts: none

- Not covered by any story: `{AIOHTTP_SHOWN}` (add one to scripts/ux_stories.toml \
if a person does this in the web UI)

**Caveats:** {story_section.LOOK_ONLY_CAVEAT}; backend: \
`{STACK}` v0.6.12, build 2026-10-16; the UI is the review stack's own deployment, \
not this MR's; docs from this MR's head (`01234567`); one machine's attempt per \
story, following the documentation literally; {section.UI_FACTS_CAVEAT}

{VIDEO}

🤖 Automated first-time-user stories of `01234567` by `{MODEL}` in a confined \
headless browser against `{STACK}`, following `.claude/skills/ux-test-stories-ci.md`. \
**Advisory only** — it approves nothing and gates nothing.
"""


def _notes(
    confusion: Sequence[str] = (), dead_end: Sequence[str] = (), injections: int = 0
) -> story_verdict.Notes:
    return story_verdict.Notes(
        confusion=tuple(story_verdict.Note(what=w, why="why") for w in confusion),
        dead_end=tuple(story_verdict.Note(what=w, why="why") for w in dead_end),
        injections=injections,
        injection_places={place: 0 for place in story_verdict.INJECTION_PLACES},
    )


def _result(
    key: str = "route-folder-to-profile@platform-admin",
    *,
    attempt: int = 1,
    verdict: str = "done",
    sub_kind: str | None = None,
    flags: Sequence[str] = (),
    group: str = "Admin",
    title: str = TITLE,
    expectation: str = "allowed",
    actions_used: int | None = None,
    budget: int | None = None,
    notes: story_verdict.Notes | None = None,
    commit_points: Sequence[str] = (),
    help_links: Sequence[str] = (),
) -> story_verdict.FlowResult:
    story, persona = key.split("@")
    return story_verdict.FlowResult(
        key=key,
        story=story,
        persona=persona,
        group=group,
        title=title,
        expectation=expectation,
        kind="commit_point",
        series="0123456789ab",
        attempt=attempt,
        budget_scale=1,
        verdict=verdict,
        sub_kind=sub_kind,
        flags=tuple(flags),
        met_expectation=story_verdict.VERDICT_MEETS[verdict],
        findings=(),
        documented_steps=5,
        actions_used=actions_used,
        budget=budget,
        overrun=None,
        budget_exhausted=None,
        looks=None,
        searches=0,
        reads=0,
        notes=notes or _notes(),
        help_links_unresolved=tuple(help_links),
        doc_addresses_guessed=0,
        commit_points=tuple(commit_points),
        declared_docs_found=False,
        terminal=None,
        claude=None,
        audit="clean",
        refusals_by_origin={},
        video=None,
        chapters=(),
    )


def _golden_results() -> list[story_verdict.FlowResult]:
    analyst = "route-folder-to-profile@new-analyst"
    offered = dict(
        verdict="offered",
        flags=("unproven",),
        group="Author",
        expectation="not_offered",
        actions_used=9,
        budget=50,
        commit_points=("Save",),
    )
    return [
        _result(
            flags=("stopped_at_commit_point",),
            actions_used=14,
            budget=50,
            notes=_notes(confusion=["The prefix field gave no example"]),
        ),
        _result(
            analyst,
            attempt=1,
            notes=_notes(
                dead_end=["The preview showed a mapping that could not be saved"]
            ),
            help_links=[HELP_LINK],
            **offered,
        ),
        _result(analyst, attempt=2, **offered),
    ]


def _reply(**overrides: Any) -> str:
    results = overrides.pop("results", None) or _golden_results()
    values: dict[str, Any] = {
        "head_sha": HEAD,
        "sel": SEL,
        "uploads": [456],
        "results": results,
        "found": story_verdict.findings(results),
        "chosen": [
            ChosenLine(kind="Added", lead=ROUTE_LEAD, story="route-folder-to-profile"),
            ChosenLine(kind="Fixed", lead=AIOHTTP_LEAD, story=None),
        ],
        "uncovered": [AIOHTTP_LEAD],
        "caveats": story_section.standard_caveats(
            ui_source="deployed",
            docs_sha=HEAD,
            backend_changed=False,
            stack=STACK_FACTS,
        ),
        "videos": [VIDEO],
        "model": MODEL,
        "stack": STACK,
    }
    values.update(overrides)
    return story_section.compose_reply(**values)


def _ai_review_marker_re() -> re.Pattern[str]:
    """ai_mr_review.MARKER_RE, compiled from that file's source, never imported."""
    tree = ast.parse(AI_MR_REVIEW.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MARKER_RE"
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Call)
            return re.compile(ast.literal_eval(node.value.args[0]))
    raise AssertionError("scripts/sdlc/ai_mr_review.py no longer defines MARKER_RE")


def _ux_test_section() -> str:
    report = section.parse_report(
        '```json\n{"flows": [{"id": "x", "title": "t", "outcome": "works", '
        '"summary": "s"}], "visible_change_found": true}\n```'
    )
    return section.compose_section(
        head_sha=HEAD,
        persona="Admin",
        stack=section.StackFacts(name=STACK, region="us-east-1"),
        report=report,
        video=UX_TEST_VIDEO,
        video_note=None,
        chapters=[],
        caveats=[],
        model=MODEL,
        upload_id=456,
    )


def _all_story_bodies() -> list[str]:
    reply = _reply()
    return [
        reply,
        story_section.compose_standalone(reply),
        story_section.compose_unchanged(head_sha=HEAD, sel=SEL, since_sha=OTHER_HEAD),
        story_section.compose_limit(head_sha=HEAD, sel=SEL, iid=812, runs=4),
        story_section.marker_line(head_sha=HEAD, sel=SEL, kind="run", uploads=(1, 2)),
    ]


@pytest.mark.unit
def test_the_reply_is_exactly_the_layout() -> None:
    assert _reply() == GOLDEN


@pytest.mark.unit
@pytest.mark.parametrize("uploads", [(), (7,), (123456789012 - 1, 5)])
def test_the_marker_line_round_trips_head_selection_kind_and_uploads(
    uploads: tuple[int, ...],
) -> None:
    for kind in story_section.MARKER_KINDS:
        line = story_section.marker_line(
            head_sha=HEAD, sel=SEL, kind=kind, uploads=uploads
        )
        found = story_section.STORY_MARKER_RE.fullmatch(line)
        assert found is not None
        assert found.group("sha") == HEAD
        assert found.group("rev") == str(story_section.STORY_REPLY_REVISION)
        assert found.group("sel") == SEL
        assert found.group("kind") == kind
        assert found.group("upload") == (
            ",".join(map(str, uploads)) if uploads else "none"
        )
        (marker,) = story_section.story_markers([{"id": 9, "body": line}])
        assert marker == story_section.StoryMarker(
            note_id=9,
            sha=HEAD,
            rev=story_section.STORY_REPLY_REVISION,
            sel=SEL,
            kind=kind,
            uploads=uploads,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {"head_sha": HEAD[:8]},
        {"head_sha": HEAD.upper()},
        {"sel": SEL[:8]},
        {"sel": SEL.upper()},
        {"kind": "posted"},
        {"uploads": (1, 2, 3)},
        {"uploads": (True,)},
        {"uploads": (-1,)},
        {"uploads": (10**12,)},
    ],
)
def test_the_marker_line_refuses_values_the_marker_cannot_carry(
    overrides: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "head_sha": HEAD,
        "sel": SEL,
        "kind": "run",
        "uploads": (),
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        story_section.marker_line(**values)


@pytest.mark.unit
def test_no_pr1_marker_regex_matches_anything_this_module_writes() -> None:
    ai_review = _ai_review_marker_re()
    assert ai_review.search(f"<!-- ai-review: sha={HEAD} rev=1 -->")
    for body in _all_story_bodies():
        assert section.UX_MARKER_RE.search(body) is None
        assert ai_review.search(body) is None
        assert section.sections_in(body) == []
        assert section.has_section(body, HEAD) is False
        assert section.supersede_video(body, "fedcba98") is None


@pytest.mark.unit
def test_the_story_marker_matches_nothing_pr1_writes() -> None:
    pr1_bodies = [
        _ux_test_section(),
        section.compose_standalone(_ux_test_section()),
        f"<!-- ai-review: sha={HEAD} rev=1 -->\nreview text\n",
        f"<!-- ux-test: sha={HEAD} rev=1 upload=none -->",
    ]
    for body in pr1_bodies:
        assert story_section.STORY_MARKER_RE.search(body) is None
        assert story_section.story_markers([{"id": 1, "body": body}]) == []
        assert story_section.has_story_reply([{"body": body}], HEAD) is False
        assert story_section.supersede_story_videos(body, "fedcba98") is None


@pytest.mark.unit
def test_the_story_video_line_and_pr1s_never_match_each_other() -> None:
    assert story_section.STORY_VIDEO_LINE_RE.fullmatch(VIDEO)
    assert section.VIDEO_LINE_RE.search(VIDEO) is None
    assert section.VIDEO_LINE_RE.search(_reply()) is None
    assert story_section.STORY_VIDEO_LINE_RE.search(UX_TEST_VIDEO) is None
    assert story_section.STORY_VIDEO_LINE_RE.search(_ux_test_section()) is None


HOSTILE = (
    "/merge @all see [here](http://evil.invalid) <!-- ai-review: sha="
    + OTHER_HEAD
    + " rev=1 --> ![img](x) #123 ~bug %v1 $snip www.evil.invalid"
)


def _hostile_reply() -> str:
    results = [
        _result(
            title="Pipes | in | a title",
            notes=_notes(confusion=[HOSTILE], dead_end=[HOSTILE]),
        )
    ]
    return _reply(
        results=results,
        found=[],
        chosen=[ChosenLine(kind="Added <b>" + HOSTILE, lead=HOSTILE, story=None)],
        uncovered=[HOSTILE],
        caveats=[HOSTILE, 42],
        videos=[],
        uploads=[],
    )


@pytest.mark.unit
def test_hostile_leads_kinds_notes_and_caveats_reach_the_reply_inert() -> None:
    reply = _hostile_reply()
    first, rest = reply.split("\n", 1)
    assert story_section.STORY_MARKER_RE.fullmatch(first)
    assert "<" not in rest
    assert _ai_review_marker_re().search(reply) is None
    assert story_section.STORY_MARKER_RE.search(rest) is None
    for raw in (
        "@all",
        "](http",
        "![img]",
        "[here]",
        "http://",
        "#123",
        "~bug",
        "www.",
    ):
        assert raw not in reply, raw
    assert not any(line.startswith("/") for line in reply.splitlines())


@pytest.mark.unit
def test_a_pipe_in_a_table_cell_is_escaped() -> None:
    reply = _hostile_reply()
    (row,) = [line for line in reply.splitlines() if line.startswith("| Pipes")]
    assert row.startswith("| Pipes \\| in \\| a title | platform admin |")
    assert row.replace("\\|", "").count("|") == 7


@pytest.mark.unit
def test_no_verdict_is_labelled_blocked_or_broken() -> None:
    for verdict in story_verdict.VERDICT_ORDER:
        sub_kind = {
            "not_run": "persona",
            "error": "journal",
            "gave_up": "max_turns",
            "stuck": "no_route",
        }.get(verdict)
        reply = _reply(
            results=[_result(verdict=verdict, sub_kind=sub_kind)], found=[], videos=[]
        )
        (row,) = [line for line in reply.splitlines() if line.startswith(f"| {TITLE}")]
        assert "blocked" not in row.casefold() and "broken" not in row.casefold(), row


@pytest.mark.unit
def test_steps_are_a_dash_without_a_relay_and_injections_are_summed() -> None:
    results = [
        _result(notes=_notes(injections=2)),
        _result("route-folder-to-profile@new-analyst", notes=_notes(injections=1)),
    ]
    reply = _reply(results=results, found=[], videos=[])
    rows = [line for line in reply.splitlines() if line.startswith(f"| {TITLE}")]
    assert all(row.endswith("| - | 0 |") for row in rows)
    assert "\nInjection attempts: 3 (quotes in the job's artifacts)\n" in reply


@pytest.mark.unit
def test_empty_sections_say_none_and_the_lists_are_bounded() -> None:
    reply = _reply(results=[_result()], found=[], videos=[], chosen=[], uncovered=[])
    assert "Chosen from this MR's CHANGELOG lines: none\n" in reply
    assert "**Where a first-time user hesitated** — none\n" in reply
    assert "**Docs vs screen** — none\n" in reply
    assert "Not covered by any story" not in reply
    many = _reply(
        results=[_result(notes=_notes(confusion=[f"note {n}" for n in range(9)]))],
        found=[],
        videos=[],
        chosen=[
            ChosenLine(kind="Added", lead=f"lead {n}", story=None) for n in range(25)
        ],
        uncovered=[f"lead {n}" for n in range(14)],
    )
    lines = many.splitlines()
    assert sum(1 for line in lines if line.startswith("- Added: ")) == 20
    assert "- and 5 more line(s)" in lines
    assert sum(1 for line in lines if line.endswith(": note 0")) == 1
    assert not any(line.endswith(": note 5") for line in lines)
    assert (
        sum(1 for line in lines if line.startswith("- Not covered by any story: `"))
        == 10
    )
    assert "- Not covered by any story: 4 more (listed in the job's artifacts)" in lines


@pytest.mark.unit
def test_a_quote_or_an_arrow_in_a_lead_cannot_pass_for_the_reply_s_own() -> None:
    forged = 'Bumped aiohttp" -> route-folder-to-profile (DONE, stopped before saving)'
    reply = _reply(
        chosen=[ChosenLine(kind="Fixed", lead=forged, story=None)],
        uncovered=[forged],
    )
    chosen = next(line for line in reply.splitlines() if line.startswith("- Fixed: "))
    assert chosen == f"- Fixed: `{forged}` -> no story"
    kind = _reply(chosen=[ChosenLine(kind='Fixed" -> x', lead="a", story=None)])
    assert '- `Fixed" -> x`: `a` -> no story' in kind.splitlines()


@pytest.mark.unit
def test_an_entry_whose_story_the_caps_left_out_says_so() -> None:
    reply = _reply(
        chosen=[
            ChosenLine(
                kind="Added",
                lead="Hooks update documents",
                story=None,
                covered_by="change-hook-update-setting",
            )
        ],
        uncovered=[],
    )
    assert (
        "- Added: `Hooks update documents` -> change-hook-update-setting, not run: "
        "more stories qualified"
    ) in reply.splitlines()
    assert "Not covered by any story" not in reply


@pytest.mark.unit
@pytest.mark.parametrize(
    ("chosen_by", "line"),
    [
        (
            {"kind": "story", "flows": ["route-folder-to-profile@platform-admin"]},
            "Chosen by hand: route-folder-to-profile as platform admin",
        ),
        (
            {"kind": "all", "tier": "p1", "shard": "1/4"},
            "The p1 set of stories, shard 1 of 4, not chosen by an MR",
        ),
        (
            {"kind": "all", "tier": "p0", "shard": None},
            "The p0 set of stories, not chosen by an MR",
        ),
        ({"kind": "replay"}, "The flows of an earlier run, replayed"),
        ({"kind": "mr"}, "Chosen from this MR's CHANGELOG lines: none"),
        (None, "Chosen from this MR's CHANGELOG lines: none"),
    ],
)
def test_a_run_no_mr_chose_says_how_its_flows_were_chosen(
    chosen_by: dict[str, Any] | None, line: str
) -> None:
    reply = _reply(chosen=[], uncovered=[], chosen_by=chosen_by)
    assert reply.splitlines()[3] == line


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {"head_sha": "0123abcd"},
        {"sel": "nothex"},
        {"uploads": [1, 2, 3]},
        {"videos": [VIDEO, SECOND_VIDEO, VIDEO]},
        {"videos": [UX_TEST_VIDEO]},
        {"videos": [VIDEO + "\nmore"]},
        {"videos": ["![UX story](javascript:alert(1))"]},
    ],
)
def test_compose_reply_refuses_a_bad_head_selection_upload_or_video(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _reply(**overrides)


@pytest.mark.unit
def test_two_videos_and_two_uploads_are_carried() -> None:
    reply = _reply(uploads=[456, 789], videos=[VIDEO, SECOND_VIDEO])
    assert reply.startswith(
        f"<!-- ux-story: sha={HEAD} rev={story_section.STORY_REPLY_REVISION} sel={SEL} kind=run upload=456,789 -->\n"
    )
    assert f"\n{VIDEO}\n{SECOND_VIDEO}\n" in reply


@pytest.mark.unit
def test_the_standalone_note_keeps_the_marker_first_and_the_line_after_it() -> None:
    reply = _reply()
    body = story_section.compose_standalone(reply)
    marker, line, blank, rest = body.split("\n", 3)
    assert marker == reply.split("\n", 1)[0]
    assert line == story_section.STANDALONE_LINE
    assert blank == ""
    assert rest == reply.split("\n", 1)[1]
    assert story_section.has_story_reply([{"body": body}], HEAD) is True


@pytest.mark.unit
def test_the_standalone_note_refuses_an_ai_review_marker_or_a_missing_marker() -> None:
    marker = story_section.marker_line(head_sha=HEAD, sel=SEL, kind="run")
    with pytest.raises(ValueError):
        story_section.compose_standalone(
            f"{marker}\n<!-- ai-review: sha={HEAD} rev=1 -->\n"
        )
    with pytest.raises(ValueError):
        story_section.compose_standalone("### no marker\n")
    with pytest.raises(ValueError):
        story_section.compose_standalone(" " + _reply())


@pytest.mark.unit
def test_the_unchanged_and_limit_replies_are_one_line_under_their_marker() -> None:
    unchanged = story_section.compose_unchanged(
        head_sha=HEAD, sel=SEL, since_sha=OTHER_HEAD
    )
    assert unchanged == (
        f"<!-- ux-story: sha={HEAD} rev={story_section.STORY_REPLY_REVISION} sel={SEL} kind=unchanged upload=none -->\n"
        "First-time-user stories: unchanged since `fedcba98` (same stories, UI and "
        "docs), so nothing was run for `01234567`.\n"
    )
    limit = story_section.compose_limit(head_sha=HEAD, sel=SEL, iid=812, runs=4)
    assert limit == (
        f"<!-- ux-story: sha={HEAD} rev={story_section.STORY_REPLY_REVISION} sel={SEL} kind=limit upload=none -->\n"
        "First-time-user stories: this MR has used its 4 runs; run "
        "`make ai-ux-test-stories-local MR=812` to run them locally.\n"
    )
    with pytest.raises(ValueError):
        story_section.compose_unchanged(head_sha=HEAD, sel=SEL, since_sha="fedcba98")
    for iid, runs in ((0, 4), (True, 4), (812, -1), (812, False)):
        with pytest.raises(ValueError):
            story_section.compose_limit(head_sha=HEAD, sel=SEL, iid=iid, runs=runs)


def _notes_mine() -> list[dict[str, Any]]:
    run = story_section.marker_line(head_sha=HEAD, sel=SEL, kind="run", uploads=(5,))
    older = story_section.marker_line(head_sha=OTHER_HEAD, sel=SEL, kind="run")
    return [
        {
            "id": 40,
            "body": story_section.compose_limit(head_sha=HEAD, sel=SEL, iid=1, runs=4),
        },
        {
            "id": 30,
            "body": story_section.compose_unchanged(
                head_sha=HEAD, sel=SEL, since_sha=OTHER_HEAD
            ),
        },
        {"id": 20, "body": run + "\nbody\n"},
        {"id": 15, "body": "quoted further down: " + older},
        {"id": 12, "body": " " + older},
        {"id": 10, "body": older + "\nbody\n"},
        {"id": 5, "body": None},
        {"body": 7},
    ]


@pytest.mark.unit
def test_markers_count_only_at_position_zero_in_the_order_given() -> None:
    markers = story_section.story_markers(_notes_mine())
    assert [(m.note_id, m.kind, m.sha) for m in markers] == [
        (40, "limit", HEAD),
        (30, "unchanged", HEAD),
        (20, "run", HEAD),
        (10, "run", OTHER_HEAD),
    ]
    assert markers[2].uploads == (5,)
    assert (
        story_section.story_markers([{"id": True, "body": _reply()}])[0].note_id is None
    )


@pytest.mark.unit
def test_runs_used_counts_run_replies_and_latest_run_is_the_newest() -> None:
    notes = _notes_mine()
    assert story_section.runs_used(notes) == 2
    latest = story_section.latest_run(notes)
    assert latest is not None and (latest.note_id, latest.sha) == (20, HEAD)
    assert story_section.latest_run(notes[3:]) is not None
    assert story_section.latest_run(notes[6:]) is None
    assert story_section.runs_used([]) == 0


@pytest.mark.unit
def test_has_story_reply_keys_on_the_full_head_and_revision_of_any_kind() -> None:
    limit_only = [
        {"body": story_section.compose_limit(head_sha=HEAD, sel=SEL, iid=1, runs=4)}
    ]
    assert story_section.has_story_reply(limit_only, HEAD) is True
    assert story_section.has_story_reply(limit_only, OTHER_HEAD) is False
    other = story_section.STORY_REPLY_REVISION + 1
    assert story_section.has_story_reply(limit_only, HEAD, rev=other) is False
    quoted = [{"body": "see " + _reply()}]
    assert story_section.has_story_reply(quoted, HEAD) is False


@pytest.mark.unit
def test_supersede_replaces_only_the_story_video_lines_of_a_story_note() -> None:
    reply = _reply(uploads=[1, 2], videos=[VIDEO, SECOND_VIDEO])
    marked = reply.replace("\n🤖", f"\n{UX_TEST_VIDEO}\n\n🤖")
    replaced = story_section.supersede_story_videos(marked, "fedcba98")
    assert replaced is not None
    assert replaced == marked.replace(
        VIDEO, "_Video superseded by `fedcba98`._"
    ).replace(SECOND_VIDEO, "_Video superseded by `fedcba98`._")
    assert UX_TEST_VIDEO in replaced
    assert story_section.supersede_story_videos(replaced, "fedcba98") is None
    assert story_section.supersede_story_videos("intro\n" + reply, "fedcba98") is None
    assert story_section.supersede_story_videos(_reply(videos=[]), "fedcba98") is None
    with pytest.raises(ValueError):
        story_section.supersede_story_videos(reply, "not-a-sha")


@pytest.mark.unit
def test_the_standard_caveats_follow_the_ui_source_backend_and_docs() -> None:
    build = story_section.standard_caveats(
        ui_source="build:" + HEAD,
        docs_sha=None,
        backend_changed=True,
        stack=STACK_FACTS,
    )
    assert build == [
        story_section.LOOK_ONLY_CAVEAT,
        "backend: `idp-ux-nightly` v0.6.12, build 2026-10-16; this MR's own backend "
        "changes are not deployed there, so a bad result may need them",
        "the UI is this MR's build",
        "docs from the working tree",
        "one machine's attempt per story, following the documentation literally",
        section.UI_FACTS_CAVEAT,
    ]
    assert story_section.standard_caveats(
        ui_source="build", docs_sha=HEAD, backend_changed=False, stack=STACK_FACTS
    )[1:4] == [
        "backend: `idp-ux-nightly` v0.6.12, build 2026-10-16",
        "the UI is this MR's build",
        "docs from this MR's head (`01234567`)",
    ]
    outside = {"docs_sha": None, "backend_changed": False, "stack": STACK_FACTS}
    assert (
        story_section.standard_caveats(ui_source="deployed", mr=False, **outside)[2]
        == "the UI is the stack's own deployment"
    )
    assert (
        story_section.standard_caveats(ui_source="build", mr=False, **outside)[2]
        == "the UI is a local build"
    )
    with pytest.raises(ValueError):
        story_section.standard_caveats(
            ui_source="local", docs_sha=None, backend_changed=False, stack=STACK_FACTS
        )
    with pytest.raises(ValueError):
        story_section.standard_caveats(
            ui_source="deployed",
            docs_sha="abc",
            backend_changed=False,
            stack=STACK_FACTS,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("facts", "backend", "labelled"),
    [
        (STACK_FACTS, "backend: `idp-ux-nightly` v0.6.12, build 2026-10-16", True),
        (
            section.StackFacts(name=STACK, region="us-east-1", version="0.6.12"),
            "backend: `idp-ux-nightly` v0.6.12; its build could not be read from the "
            "UI",
            True,
        ),
        (
            section.StackFacts(
                name=STACK, region="us-east-1", build="2026-10-16 21:07:20"
            ),
            "backend: `idp-ux-nightly`, build 2026-10-16; its version could not be "
            "read from the UI",
            True,
        ),
        (
            UNREAD_FACTS,
            "backend: `idp-ux-nightly`; its version and build could not be read from "
            "the UI",
            False,
        ),
    ],
    ids=["read", "version-only", "build-only", "unread"],
)
def test_the_backend_is_the_one_the_stack_reported_never_develops_by_assumption(
    facts: section.StackFacts, backend: str, labelled: bool
) -> None:
    """The review stack runs whatever it was last deployed with, so a story reply
    names the backend in the MR UX test note's own words, by the version and build
    the signed-in sidebar showed, says when it showed neither, and labels the facts
    as the UI's whenever it showed either one. No caveat says the stack runs
    develop's backend or UI."""
    for ui_source in ("deployed", "build:" + HEAD):
        for changed in (False, True):
            caveats = story_section.standard_caveats(
                ui_source=ui_source,
                docs_sha=None,
                backend_changed=changed,
                stack=facts,
            )
            assert caveats[1] == section.backend_caveat(facts) + (
                "; this MR's own backend changes are not deployed there, so a bad "
                "result may need them"
                if changed
                else ""
            )
            assert caveats[1].startswith(backend)
            assert not [caveat for caveat in caveats if "develop" in caveat]
            assert (section.UI_FACTS_CAVEAT in caveats) is labelled


@pytest.mark.unit
def test_refusals_add_pr1s_refusal_caveat_and_background_ones_do_not() -> None:
    refusals = [
        {"target": "https://cdn.invalid/x.js", "reason": "host_not_allowed"},
        {"target": "https://update.invalid/", "reason": "host_not_allowed"},
    ]
    caveats = story_section.standard_caveats(
        ui_source="deployed",
        docs_sha=None,
        backend_changed=False,
        stack=UNREAD_FACTS,
        refusals=refusals,
        background=lambda target: "update." in target,
    )
    assert caveats[-1] == section.refusal_caveat(refusals[:1])
    assert "1 of the page's requests" in caveats[-1]
    quiet = story_section.standard_caveats(
        ui_source="deployed",
        docs_sha=None,
        backend_changed=False,
        stack=UNREAD_FACTS,
        refusals=refusals[1:],
        background=lambda target: True,
    )
    assert len(quiet) == 5


@pytest.mark.unit
def test_the_refusal_caveat_counts_the_page_s_refusals_alone_by_chrome_s_origins() -> (
    None
):
    """Unless told otherwise, the reply classifies refusals with the MR UX test's
    own test, ``chrome.not_the_page``: the self-test and the hosts Chrome still
    calls by itself stay out of it, and a request of the page's to a host Chrome
    no longer calls is counted as the page's."""
    background = inspect.signature(story_section.standard_caveats).parameters[
        "background"
    ]
    assert background.default is chrome.not_the_page
    selftest = {
        "target": f"http://{chrome.SELFTEST_HOST}/",
        "reason": "plain_http_not_allowed",
    }
    chromes_own = [
        {"target": f"{host}:443", "reason": "host_not_allowed"}
        for host in sorted(chrome.BROWSER_BACKGROUND_HOSTS)
    ]
    pages = [{"target": "www.google.com:443", "reason": "host_not_allowed"}] * 2
    quiet = story_section.standard_caveats(
        ui_source="deployed",
        docs_sha=None,
        backend_changed=False,
        stack=UNREAD_FACTS,
        refusals=[selftest, *chromes_own],
    )
    assert not [caveat for caveat in quiet if "egress proxy refused" in caveat]
    caveats = story_section.standard_caveats(
        ui_source="deployed",
        docs_sha=None,
        backend_changed=False,
        stack=UNREAD_FACTS,
        refusals=[selftest, *chromes_own, *pages],
    )
    assert caveats[-1].startswith(
        "the egress proxy refused 2 of the page's requests (`host_not_allowed` ×2)"
    )
    assert "google" not in caveats[-1]


@pytest.mark.unit
def test_a_reply_without_results_says_so_and_rows_past_the_bound_are_counted() -> None:
    empty = story_section.compose_reply(
        head_sha=HEAD,
        sel=SEL,
        uploads=[],
        results=[],
        found=[],
        chosen=[],
        uncovered=[],
        caveats=[],
        videos=[],
        model=MODEL,
        stack=STACK,
    )
    assert "\n_No flow results._\n" in empty
    assert "| Story |" not in empty
    many = [_result(f"story-{n:02d}@platform-admin") for n in range(33)]
    lines = _reply(results=many, found=[], videos=[]).splitlines()
    rows = [line for line in lines if line.startswith(f"| {TITLE}")]
    assert len(rows) == 30
    note = lines.index("_and 3 more flow(s) in the artifacts_")
    assert lines[note - 1] == "" and lines[note - 2] == rows[-1]
