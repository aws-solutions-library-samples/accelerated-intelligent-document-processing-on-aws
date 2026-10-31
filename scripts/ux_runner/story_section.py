# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MR reply for first-time-user stories, composed from structured fields alone."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ux_runner import changelog_select, chrome, section, story_verdict

#: Bumped when the reply would read differently for the same run record, so a
#: head already replied to under an older revision is replied to again.
STORY_REPLY_REVISION = 2
MARKER_KINDS: tuple[str, ...] = ("run", "unchanged", "limit")
STORY_MARKER_RE = re.compile(
    r"<!-- ux-story: sha=(?P<sha>[0-9a-f]{40}) rev=(?P<rev>[0-9]{1,4}) "
    r"sel=(?P<sel>[0-9a-f]{12}) kind=(?P<kind>run|unchanged|limit) "
    r"upload=(?P<upload>none|[0-9]{1,12}(?:,[0-9]{1,12})?) -->"
)
STORY_VIDEO_LINE_RE = re.compile(
    r"^!\[UX story\]\(/uploads/[0-9a-f]{16,64}/[A-Za-z0-9._-]{1,128}\)$", re.M
)
STANDALONE_LINE = "_The code review for this head was not found, so the first-time-user stories are posted on their own._"

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHORT_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_SEL_RE = re.compile(r"[0-9a-f]{12}")
_AI_REVIEW_MARKER_RE = re.compile(r"<!--\s*ai-review:")
_MAX_UPLOADS = 2
_MAX_UPLOAD_ID = 10**12
_MAX_VIDEOS = 2
_MAX_CHOSEN_LINES = 20
_MAX_ROWS = 30
_MAX_NOTE_LINES = 5
_MAX_NUDGE_LINES = 10
_LEAD_CHARS = 120
_KIND_CHARS = 40
_TITLE_CHARS = 120
_CELL_CHARS = 160
_NOTE_CHARS = 200
_NAME_CHARS = 120
_CAVEAT_CHARS = 600
_NUDGE_TAIL = "(add one to scripts/ux_stories.toml if a person does this in the web UI)"
LOOK_ONLY_CAVEAT = (
    "looked only: each flow was told to change nothing on the shared stack, and a "
    "best-effort guard refused the saves it recognised"
)


@dataclass(frozen=True)
class StoryMarker:
    note_id: int | None
    sha: str
    rev: int
    sel: str
    kind: str
    uploads: tuple[int, ...]


def _full_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _FULL_SHA_RE.fullmatch(value):
        raise ValueError(f"{name} must be a full 40-character lowercase SHA")
    return value


def _upload_ids(uploads: Sequence[int]) -> str:
    shown = list(uploads)
    if len(shown) > _MAX_UPLOADS:
        raise ValueError(f"a story reply carries at most {_MAX_UPLOADS} uploads")
    for upload in shown:
        if (
            isinstance(upload, bool)
            or not isinstance(upload, int)
            or not 0 <= upload < _MAX_UPLOAD_ID
        ):
            raise ValueError("each upload id must be an integer of at most 12 digits")
    return ",".join(str(upload) for upload in shown) or "none"


def marker_line(
    *, head_sha: str, sel: str, kind: str, uploads: Sequence[int] = ()
) -> str:
    """The reply's first line; STORY_MARKER_RE matches it whole."""
    sha = _full_sha(head_sha, "head_sha")
    if not isinstance(sel, str) or not _SEL_RE.fullmatch(sel):
        raise ValueError("sel must be 12 lowercase hex digits")
    if kind not in MARKER_KINDS:
        raise ValueError(f"kind must be one of {', '.join(MARKER_KINDS)}")
    return (
        f"<!-- ux-story: sha={sha} rev={STORY_REPLY_REVISION} sel={sel} "
        f"kind={kind} upload={_upload_ids(uploads)} -->"
    )


def _code(text: object, limit: int) -> str:
    return section.sanitize(text, limit).replace("`", "'")


def _cell(text: object, limit: int = _CELL_CHARS) -> str:
    return section.sanitize(text, limit).replace("|", "\\|")


def _spoken(identifier: str, separator: str) -> str:
    return identifier.replace(separator, " ")


def _flows(
    results: Sequence[story_verdict.FlowResult],
) -> list[tuple[story_verdict.Confirmation, story_verdict.FlowResult]]:
    """One (confirmation, the attempt it shows) per flow key, in key order."""
    return [
        (confirmation, shown)
        for _, confirmation, shown in story_verdict.flow_confirmations(results)
    ]


def _icon(verdict: str) -> str:
    key = story_verdict.VERDICT_ICON_KEYS.get(verdict, "not_covered")
    return section.OUTCOME_ICONS.get(key, section.OUTCOME_ICONS["not_covered"])


def _steps(result: story_verdict.FlowResult) -> str:
    used, budget = result.actions_used, result.budget
    if any(isinstance(n, bool) or not isinstance(n, int) for n in (used, budget)):
        return "-"
    return f"{used} of {budget}"


def _quoted(text: object) -> str:
    """MR-authored text as a code span, which nothing in it can close or mark up."""
    shown = _code(text, _LEAD_CHARS)
    return f"`{shown}`" if shown else "`-`"


def _named(text: object) -> str:
    return section.sanitize(text, _NAME_CHARS)


def _flow_named(flow: str) -> str:
    story, _, persona = flow.partition("@")
    return f"{_named(story)} as {_named(_spoken(persona, '-'))}"


def _chosen_lines(
    chosen: Sequence[changelog_select.ChosenLine],
    chosen_by: Mapping[str, Any] | None = None,
) -> list[str]:
    """How the run's flows were chosen: from the MR's CHANGELOG, or not by an MR.

    ``chosen_by`` is the record's ``kind`` of choice: ``mr`` (the default, which an
    older record without one is), ``story`` with its ``flows``, ``all`` with its
    ``tier`` and ``shard``, or ``replay``.
    """
    by = chosen_by if isinstance(chosen_by, Mapping) else {}
    kind = by.get("kind", "mr")
    if kind == "story":
        flows = [
            _flow_named(flow) for flow in by.get("flows") or [] if isinstance(flow, str)
        ]
        return [f"Chosen by hand: {', '.join(flows[:_MAX_CHOSEN_LINES]) or 'none'}"]
    if kind == "all":
        tier = _named(by.get("tier") or "p0")
        shard = by.get("shard")
        where = f", shard {_named(shard).replace('/', ' of ')}" if shard else ""
        return [f"The {tier} set of stories{where}, not chosen by an MR"]
    if kind == "replay":
        return ["The flows of an earlier run, replayed"]
    if not chosen:
        return ["Chosen from this MR's CHANGELOG lines: none"]
    lines = ["Chosen from this MR's CHANGELOG lines:"]
    for line in list(chosen)[:_MAX_CHOSEN_LINES]:
        entry_kind = section.sanitize(line.kind, _KIND_CHARS)
        if entry_kind in changelog_select.ENTRY_KINDS:
            prefix = f"{entry_kind}: "
        else:
            prefix = f"{_quoted(entry_kind)}: " if entry_kind else ""
        if line.story:
            target = _named(line.story)
        elif line.covered_by:
            target = f"{_named(line.covered_by)}, not run: more stories qualified"
        else:
            target = "no story"
        lines.append(f"- {prefix}{_quoted(line.lead)} -> {target}")
    if len(chosen) > _MAX_CHOSEN_LINES:
        lines.append(f"- and {len(chosen) - _MAX_CHOSEN_LINES} more line(s)")
    return lines


def _table(
    flows: Sequence[tuple[story_verdict.Confirmation, story_verdict.FlowResult]],
) -> list[str]:
    if not flows:
        return ["_No flow results._"]
    lines = [
        "| Story | As | Expected | Verdict | Steps | Hesitations |",
        "|---|---|---|---|---|---|",
    ]
    for confirmation, result in list(flows)[:_MAX_ROWS]:
        label = story_verdict.verdict_label(result, confirmation)
        cells = (
            _cell(result.title, _TITLE_CHARS),
            _cell(_spoken(result.persona, "-")),
            _cell(_spoken(result.expectation, "_")),
            f"{_icon(confirmation.verdict)} {_cell(label)}",
            _steps(result),
            str(len(result.notes.confusion)),
        )
        lines.append("| " + " | ".join(cells) + " |")
    if len(flows) > _MAX_ROWS:
        lines += ["", f"_and {len(flows) - _MAX_ROWS} more flow(s) in the artifacts_"]
    return lines


def _note_line(result: story_verdict.FlowResult, note: story_verdict.Note) -> str:
    title = section.sanitize(result.title, _TITLE_CHARS)
    persona = section.sanitize(_spoken(result.persona, "-"), _NAME_CHARS)
    return f"- {title} as {persona}: {section.sanitize(note.what, _NOTE_CHARS)}"


def _block(heading: str, lines: Sequence[str]) -> list[str]:
    if not lines:
        return [f"**{heading}** — none"]
    return [f"**{heading}**", *list(lines)[:_MAX_NOTE_LINES]]


def _nudge_lines(uncovered: Sequence[str]) -> list[str]:
    leads = [_quoted(text) for text in uncovered if _code(text, _LEAD_CHARS)]
    lines = [
        f"- Not covered by any story: {lead} {_NUDGE_TAIL}"
        for lead in leads[:_MAX_NUDGE_LINES]
    ]
    if len(leads) > _MAX_NUDGE_LINES:
        more = len(leads) - _MAX_NUDGE_LINES
        lines.append(
            f"- Not covered by any story: {more} more (listed in the job's artifacts)"
        )
    return lines


def _videos(videos: Sequence[str]) -> list[str]:
    shown = list(videos)
    if len(shown) > _MAX_VIDEOS:
        raise ValueError(f"a story reply carries at most {_MAX_VIDEOS} videos")
    for video in shown:
        if not isinstance(video, str) or not STORY_VIDEO_LINE_RE.fullmatch(video):
            raise ValueError("each video must be one ![UX story](/uploads/...) line")
    return shown


def compose_reply(
    *,
    head_sha: str,
    sel: str,
    uploads: Sequence[int],
    results: Sequence[story_verdict.FlowResult],
    found: Sequence[story_verdict.Finding],
    chosen: Sequence[changelog_select.ChosenLine],
    uncovered: Sequence[str],
    caveats: Sequence[str],
    videos: Sequence[str],
    model: str,
    stack: str,
    chosen_by: Mapping[str, Any] | None = None,
) -> str:
    """The run reply: the marker line, then sections built from structured fields.

    Every MR-authored string, the CHANGELOG leads and kinds, is sanitised and
    shown as a code span, which no quote or arrow in it can close; every
    model-derived field is sanitised again, idempotently, and a table cell
    escapes ``|``.
    """
    sha8 = _full_sha(head_sha, "head_sha")[:8]
    shown_videos = _videos(videos)
    flows = _flows(results)
    lines = [
        marker_line(head_sha=head_sha, sel=sel, kind="run", uploads=uploads),
        f"### First-time-user stories for `{sha8}`",
        "",
        *_chosen_lines(chosen, chosen_by),
        "",
        *_table(flows),
        "",
    ]
    hesitations = [
        _note_line(result, note)
        for _, result in flows
        for note in result.notes.confusion
    ]
    lines += _block("Where a first-time user hesitated", hesitations)
    docs_titles = [
        f"- {section.sanitize(finding.title, _NOTE_CHARS)}"
        for finding in found
        if finding.category == "docs"
    ]
    dead_ends = [
        _note_line(result, note)
        for _, result in flows
        for note in result.notes.dead_end
    ]
    lines += ["", *_block("Docs vs screen", docs_titles + dead_ends)]
    injections = sum(result.notes.injections for result in results)
    lines += [
        "",
        "Injection attempts: "
        + (f"{injections} (quotes in the job's artifacts)" if injections else "none"),
    ]
    nudges = _nudge_lines(uncovered)
    if nudges:
        lines += ["", *nudges]
    notes = [
        section.sanitize(caveat, _CAVEAT_CHARS)
        for caveat in caveats
        if isinstance(caveat, str)
    ]
    notes = [note for note in notes if note]
    if notes:
        lines += ["", "**Caveats:** " + "; ".join(notes)]
    if shown_videos:
        lines += ["", *shown_videos]
    lines += [
        "",
        f"🤖 Automated first-time-user stories of `{sha8}` by "
        f"`{_code(model, _NAME_CHARS)}` in a confined headless browser against "
        f"`{_code(stack, _NAME_CHARS)}`, following "
        "`.claude/skills/ux-test-stories-ci.md`. **Advisory only** — it approves "
        "nothing and gates nothing.",
    ]
    return "\n".join(lines) + "\n"


def compose_standalone(reply: str) -> str:
    """``reply`` as a note of its own: STANDALONE_LINE and a blank line after the marker.

    The marker stays at position 0, the only place a story marker counts.
    """
    opening = STORY_MARKER_RE.match(reply) if isinstance(reply, str) else None
    if opening is None or not reply.startswith("\n", opening.end()):
        raise ValueError("a story reply must start with its marker line")
    rest = reply[opening.end() + 1 :].lstrip("\n")
    body = reply[: opening.end()] + "\n" + STANDALONE_LINE + "\n\n" + rest
    if _AI_REVIEW_MARKER_RE.search(body):
        raise ValueError("a standalone story note must not carry an ai-review marker")
    return body


def compose_unchanged(*, head_sha: str, sel: str, since_sha: str) -> str:
    """The one-line reply for a head whose stories, UI and docs are all unchanged."""
    since8 = _full_sha(since_sha, "since_sha")[:8]
    marker = marker_line(head_sha=head_sha, sel=sel, kind="unchanged")
    return (
        f"{marker}\nFirst-time-user stories: unchanged since `{since8}` (same "
        f"stories, UI and docs), so nothing was run for `{head_sha[:8]}`.\n"
    )


def compose_limit(*, head_sha: str, sel: str, iid: int, runs: int) -> str:
    """The one-time reply once an MR has used its ``runs`` story runs."""
    if isinstance(iid, bool) or not isinstance(iid, int) or iid < 1:
        raise ValueError("iid must be a positive integer")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        raise ValueError("runs must be a non-negative integer")
    marker = marker_line(head_sha=head_sha, sel=sel, kind="limit")
    return (
        f"{marker}\nFirst-time-user stories: this MR has used its {runs} runs; run "
        f"`make ai-ux-test-stories-local MR={iid}` to run them locally.\n"
    )


def standard_caveats(
    *,
    ui_source: str,
    docs_sha: str | None,
    backend_changed: bool,
    stack: section.StackFacts,
    refusals: Iterable[object] = (),
    background: Callable[[str], bool] = chrome.not_the_page,
    mr: bool = True,
) -> list[str]:
    """What a reader weighs every story verdict against, in reply order.

    ``ui_source`` is ``deployed``, or ``build`` with or without ``:<tree>``.
    ``stack`` is the review stack as the signed-in UI reported it: the backend is
    named by its version and build (:func:`section.backend_caveat`), never by an
    assumption about what it was last deployed from, and when the UI showed
    either, the reply says they are as the UI reports them, since on an MR's
    build the MR's own code renders them. ``refusals`` are the egress proxy's
    records; they add :func:`section.refusal_caveat`'s line when something the
    page asked for was refused. ``background`` says which records are Chrome's
    own or the proxy self-test's, which every run has; it is the MR UX test's
    own test, :func:`chrome.not_the_page`, unless a caller passes another, so
    the count is the page's alone. ``mr`` is False for a run no merge request
    chose, whose UI is named without one.
    """
    if ui_source == "deployed":
        ui = (
            "the UI is the review stack's own deployment, not this MR's"
            if mr
            else "the UI is the stack's own deployment"
        )
    elif ui_source == "build" or ui_source.startswith("build:"):
        ui = "the UI is this MR's build" if mr else "the UI is a local build"
    else:
        raise ValueError("ui_source must be 'deployed' or 'build[:<tree>]'")
    backend = section.backend_caveat(stack)
    if backend_changed:
        backend += (
            "; this MR's own backend changes are not deployed there, so a bad "
            "result may need them"
        )
    if docs_sha is None:
        docs = "docs from the working tree"
    else:
        docs = f"docs from this MR's head (`{_full_sha(docs_sha, 'docs_sha')[:8]}`)"
    caveats = [
        LOOK_ONLY_CAVEAT,
        backend,
        ui,
        docs,
        "one machine's attempt per story, following the documentation literally",
    ]
    refused = section.refusal_caveat(refusals, background=background)
    if refused is not None:
        caveats.append(refused)
    if stack.shown_version or stack.shown_build:
        caveats.append(section.UI_FACTS_CAVEAT)
    return caveats


def story_markers(notes_mine: Sequence[Mapping[str, Any]]) -> list[StoryMarker]:
    """The story marker of each note, in the order given, read at position 0 only.

    ``notes_mine`` are the token's own notes, newest first (``gitlab_api.my_notes``).
    A marker anywhere but a note's first character is text the note quotes.
    """
    markers = []
    for note in notes_mine:
        body = note.get("body") if isinstance(note, Mapping) else None
        found = STORY_MARKER_RE.match(body) if isinstance(body, str) else None
        if found is None:
            continue
        note_id = note.get("id")
        upload = found.group("upload")
        markers.append(
            StoryMarker(
                note_id=(
                    note_id
                    if isinstance(note_id, int) and not isinstance(note_id, bool)
                    else None
                ),
                sha=found.group("sha"),
                rev=int(found.group("rev")),
                sel=found.group("sel"),
                kind=found.group("kind"),
                uploads=(
                    ()
                    if upload == "none"
                    else tuple(int(part) for part in upload.split(","))
                ),
            )
        )
    return markers


def has_story_reply(
    notes_mine: Sequence[Mapping[str, Any]],
    head_sha: str,
    *,
    rev: int = STORY_REPLY_REVISION,
) -> bool:
    """Whether a reply of any kind was posted for exactly ``head_sha`` at ``rev``."""
    return any(
        marker.sha == head_sha and marker.rev == rev
        for marker in story_markers(notes_mine)
    )


def runs_used(notes_mine: Sequence[Mapping[str, Any]]) -> int:
    """How many run replies this MR holds, of any head and revision."""
    return sum(1 for marker in story_markers(notes_mine) if marker.kind == "run")


def latest_run(notes_mine: Sequence[Mapping[str, Any]]) -> StoryMarker | None:
    """The newest run reply's marker; ``notes_mine`` arrive newest first."""
    return next(
        (marker for marker in story_markers(notes_mine) if marker.kind == "run"), None
    )


def supersede_story_videos(note_body: str, new_sha8: str) -> str | None:
    """``note_body`` with its story video lines superseded, or None if none changed.

    Only a body that starts with a story marker is touched, so no note but this
    job's own is ever rewritten.
    """
    if not isinstance(new_sha8, str) or not _SHORT_SHA_RE.fullmatch(new_sha8):
        raise ValueError("new_sha8 must be an abbreviated head SHA")
    if not isinstance(note_body, str) or STORY_MARKER_RE.match(note_body) is None:
        return None
    replaced, count = STORY_VIDEO_LINE_RE.subn(
        f"_Video superseded by `{new_sha8}`._", note_body
    )
    return replaced if count else None
