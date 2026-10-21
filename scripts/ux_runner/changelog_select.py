#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which first-time-user stories an MR's CHANGELOG entries select, by integer scores."""

from __future__ import annotations

import argparse
import functools
import re
import sys
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from ux_runner import python_floor  # noqa: E402

python_floor.require(__file__)

from ux_runner import stories, ui_change  # noqa: E402

ENTRY_KINDS: tuple[str, ...] = ("Added", "Changed", "Fixed")
LEAD_RE = re.compile(r"\A(?:⚠️?\s*)?\*\*(?P<lead>.+?)\*\*")
DOC_LINK_RE = re.compile(
    r"\]\((?:\./)?docs/(?P<page>[^)#\s]+\.md)(?P<anchor>#[^)\s]*)?\)"
)
ISSUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/issues/(\d+)"),
    re.compile(r"(?<![\w&/])#(\d+)\b"),
)
SELECT_THRESHOLD = 2
#: A link to a section a story declares selects it on its own; a link to another
#: section of a page it declares is a hint worth one point however many entries
#: carry one, so it needs a second signal, because the pages stories declare are
#: long and several stories declare each.
ANCHOR_POINTS = 3
PAGE_POINTS = 1
ISSUE_POINTS = 2
TERM_POINTS_MAX = 2

_CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
_ATX_RE = re.compile(r"(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*\Z")
_LABEL_RE = re.compile(r"\[(?P<label>[^\]]+)\]")
_BULLETS = ("- ", "* ")
_ISSUE_DIGITS = 9
_EXCERPT_CHARS = 120
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SERIES_RE = re.compile(r"[0-9a-f]{12}")
_UI_BUILD_RE = re.compile(r"build:[0-9a-f]{40}")
_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_UNRELEASED = "unreleased"


@dataclass(frozen=True)
class Entry:
    section: str
    kind: str
    lead: str
    text: str
    docs: tuple[str, ...]
    issues: tuple[int, ...]
    line: int


@dataclass(frozen=True)
class AddedEntry:
    entry: Entry
    modified: bool
    in_released_section: bool


@dataclass(frozen=True)
class StoryScore:
    story: str
    changelog_points: int
    secondary_points: int
    total: int
    reasons: tuple[str, ...]
    entries: tuple[int, ...]


@dataclass(frozen=True)
class ChosenLine:
    """One added entry as the reply shows it.

    ``story`` is the selected story the entry gave CHANGELOG points to. When it
    gave none, ``covered_by`` is the best-ranked story the entry selects on its
    own that the caps left out of the run, else None.
    """

    kind: str
    lead: str
    story: str | None
    covered_by: str | None = None


@dataclass(frozen=True)
class Selection:
    flows: tuple[stories.FlowKey, ...]
    stories: tuple[str, ...]
    scores: tuple[StoryScore, ...]
    added: tuple[AddedEntry, ...]
    chosen: tuple[ChosenLine, ...]
    uncovered: tuple[str, ...]
    dropped_personas: tuple[str, ...]
    warnings: tuple[str, ...]


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _issue_numbers(text: str) -> tuple[int, ...]:
    """Issue numbers by :data:`ISSUE_RES`; a run of digits too long to be one is not."""
    numbers: set[int] = set()
    for pattern in ISSUE_RES:
        for match in pattern.finditer(text):
            digits = match.group(1)
            if digits.isascii() and len(digits) <= _ISSUE_DIGITS:
                numbers.add(int(digits))
    return tuple(sorted(numbers))


def _doc_links(text: str) -> tuple[str, ...]:
    links = []
    for match in DOC_LINK_RE.finditer(text):
        anchor = match.group("anchor") or ""
        links.append(match.group("page") + (anchor if len(anchor) > 1 else ""))
    return _unique(links)


def _entry(section: str, kind: str, line: int, parts: Sequence[str]) -> Entry | None:
    text = " ".join(" ".join(parts).split())
    if not text:
        return None
    lead = LEAD_RE.match(text)
    return Entry(
        section=section,
        kind=kind,
        lead=lead.group("lead") if lead else "",
        text=text,
        docs=_doc_links(text),
        issues=_issue_numbers(text),
        line=line,
    )


def parse_entries(text: str) -> list[Entry]:
    """Every column-0 bullet of a CHANGELOG, with its section, kind, lead and links.

    An entry runs to the next column-0 bullet or ATX heading. An H1 or H2 heading
    ends the kind in force, so an entry is never filed under the previous release's
    ``###``; only an H2 written ``## [label]`` changes the section.
    """
    entries: list[Entry] = []
    section = ""
    kind = ""
    current: tuple[str, str, int, list[str]] | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if number == 1:
            line = line.lstrip("\ufeff")
        heading = _ATX_RE.match(line) if line.startswith("#") else None
        starts_entry = heading is None and line.startswith(_BULLETS)
        if heading is not None or starts_entry:
            if current is not None:
                entry = _entry(*current)
                if entry is not None:
                    entries.append(entry)
            current = None
        if heading is not None:
            level = len(heading.group("hashes"))
            title = (heading.group("text") or "").strip()
            if level <= 2:
                kind = ""
            if level == 2:
                label = _LABEL_RE.match(title)
                if label is not None:
                    section = label.group("label").strip()
            elif level == 3:
                kind = title
        elif starts_entry:
            current = (section, kind, number, [line[2:]])
        elif current is not None:
            current[3].append(line)
    if current is not None:
        entry = _entry(*current)
        if entry is not None:
            entries.append(entry)
    return entries


def _is_unreleased(section: str) -> bool:
    return section.casefold() == _UNRELEASED


def added_entries(base_text: str, head_text: str) -> list[AddedEntry]:
    """Head entries whose whole text no base entry has, in any section.

    The whole file is compared rather than ``[Unreleased]`` alone, so an entry a
    release cut moved under its version heading is not new, and an entry that
    lands under a released heading is still seen.
    """
    base = parse_entries(base_text)
    texts = {entry.text for entry in base}
    leads = {entry.lead for entry in base if entry.lead}
    return [
        AddedEntry(
            entry=entry,
            modified=bool(entry.lead) and entry.lead in leads,
            in_released_section=bool(entry.section)
            and not _is_unreleased(entry.section),
        )
        for entry in parse_entries(head_text)
        if entry.text not in texts
    ]


def _page(doc: str) -> str:
    return doc.split("#", 1)[0]


@functools.lru_cache(maxsize=512)
def _term_re(term: str) -> re.Pattern[str]:
    words = re.escape(" ".join(term.split()))
    return re.compile(r"(?<!\w)" + words + r"(?!\w)", re.IGNORECASE)


def _entry_points(entry: Entry, match: stories.Match) -> tuple[int, bool, list[str]]:
    """An entry's points for one story but its page hint, whether it holds one, why."""
    pages = {_page(doc) for doc in match.docs}
    points = 0
    hinted = False
    reasons: list[str] = []
    for link in entry.docs:
        if link in match.docs:
            points += ANCHOR_POINTS
            reasons.append(f"anchor:{link}")
        elif _page(link) in pages:
            hinted = True
            reasons.append(f"page:{_page(link)}")
    for number in entry.issues:
        if number in match.issues:
            points += ISSUE_POINTS
            reasons.append(f"issue:{number}")
    found_terms = 0
    for term in match.terms:
        if found_terms >= TERM_POINTS_MAX:
            break
        if _term_re(term).search(entry.text):
            found_terms += 1
            reasons.append(f"term:{term}")
    return points + found_terms, hinted, reasons


def score_story(
    story: stories.Story,
    added: Sequence[AddedEntry],
    *,
    edited_sections: Collection[str],
    changed_files: Sequence[str],
) -> StoryScore:
    """One story's points: CHANGELOG points per added entry, then secondary points.

    ``entries`` are the positions in ``added`` of the entries that gave CHANGELOG
    points, a page hint included; the hints of every entry together are worth
    :data:`PAGE_POINTS`. Secondary points (a declared docs section edited, a
    ``ui_paths`` file changed) add to the total and rank, and never select on
    their own.
    """
    changelog_points = 0
    hinted = False
    reasons: list[str] = []
    positions: list[int] = []
    for position, item in enumerate(added):
        points, page, why = _entry_points(item.entry, story.match)
        if points or page:
            changelog_points += points
            hinted = hinted or page
            reasons += why
            positions.append(position)
    if hinted:
        changelog_points += PAGE_POINTS
    secondary = 0
    edited = frozenset(edited_sections)
    docs_edit = next((doc for doc in story.match.docs if doc in edited), None)
    if docs_edit is not None:
        secondary += 1
        reasons.append(f"docs_edit:{docs_edit}")
    ui_glob = next(
        (
            glob
            for glob in story.match.ui_paths
            if any(ui_change.matches(path, (glob,)) for path in changed_files)
        ),
        None,
    )
    if ui_glob is not None:
        secondary += 1
        reasons.append(f"ui_path:{ui_glob}")
    return StoryScore(
        story=story.id,
        changelog_points=changelog_points,
        secondary_points=secondary,
        total=changelog_points + secondary,
        reasons=tuple(reasons),
        entries=tuple(positions),
    )


def _shown_lead(entry: Entry) -> str:
    """The bold lead, or for an entry without one, the start of its text."""
    if entry.lead:
        return entry.lead
    if len(entry.text) <= _EXCERPT_CHARS:
        return entry.text
    return entry.text[: _EXCERPT_CHARS - 1].rstrip() + "…"


def _tier_index(tier: str) -> int:
    return stories.TIERS.index(tier) if tier in stories.TIERS else len(stories.TIERS)


def _candidates(ranked: Sequence[stories.Story]) -> list[stories.FlowKey]:
    """Every ``mr_as`` persona of the ranked stories, in rank order, then the rest."""
    firsts: list[stories.FlowKey] = []
    rests: list[stories.FlowKey] = []
    for story in ranked:
        ordered = stories.mr_personas(story)
        cut = len(story.mr_as)
        firsts += [stories.FlowKey(story=story.id, persona=p) for p in ordered[:cut]]
        rests += [stories.FlowKey(story=story.id, persona=p) for p in ordered[cut:]]
    return firsts + rests


def select(
    catalogue: stories.Catalogue,
    added: Sequence[AddedEntry],
    *,
    edited_sections: Collection[str] = (),
    changed_files: Sequence[str] = (),
    available_groups: Collection[str] = stories.SIGN_IN_GROUPS,
) -> Selection:
    """The stories and flows an MR runs, and the lines its reply shows.

    A persona whose group cannot sign in is reported in ``dropped_personas`` only
    when its flow would otherwise have been taken, since that is the flow the MR
    lost. ``uncovered`` holds the leads of added entries that select no story of
    the catalogue on their own, as :func:`uncovered_unreleased` reads the nightly's,
    so an entry whose story the caps left out is not called uncovered.
    """
    by_id = {story.id: story for story in catalogue.stories}
    scores = [
        score_story(
            story, added, edited_sections=edited_sections, changed_files=changed_files
        )
        for story in catalogue.stories
    ]
    ranked = sorted(
        (score for score in scores if score.total > 0),
        key=lambda score: (
            -score.total,
            _tier_index(by_id[score.story].tier),
            score.story,
        ),
    )
    chosen_scores = [
        score for score in ranked if score.changelog_points >= SELECT_THRESHOLD
    ][: catalogue.runs.mr_stories]
    groups = frozenset(available_groups)
    flows: list[stories.FlowKey] = []
    dropped: list[str] = []
    for key in _candidates([by_id[score.story] for score in chosen_scores]):
        if len(flows) >= catalogue.runs.mr_flows:
            break
        if catalogue.persona(key.persona).group not in groups:
            if key.persona not in dropped:
                dropped.append(key.persona)
            continue
        flows.append(key)
    chosen = []
    ranked_stories = [by_id[score.story] for score in ranked]
    for position, item in enumerate(added):
        story_id = next(
            (score.story for score in chosen_scores if position in score.entries), None
        )
        covered_by = None
        if story_id is None:
            covered_by = next(
                (story.id for story in ranked_stories if _selects(story, item.entry)),
                None,
            )
        chosen.append(
            ChosenLine(
                kind=item.entry.kind,
                lead=_shown_lead(item.entry),
                story=story_id,
                covered_by=covered_by,
            )
        )
    uncovered = _unique(
        [
            _shown_lead(item.entry)
            for item in added
            if not any(_selects(story, item.entry) for story in catalogue.stories)
        ]
    )
    warnings = []
    for item in added:
        lead = _shown_lead(item.entry)
        if item.in_released_section:
            warnings.append(f"in released section [{item.entry.section}]: {lead}")
        if item.modified:
            warnings.append(f"rewords an existing entry: {lead}")
    return Selection(
        flows=tuple(flows),
        stories=tuple(score.story for score in chosen_scores),
        scores=tuple(ranked),
        added=tuple(added),
        chosen=tuple(chosen),
        uncovered=uncovered,
        dropped_personas=tuple(dropped),
        warnings=tuple(warnings),
    )


def selection_hash(
    flows: Sequence[tuple[stories.FlowKey, str]], *, ui_source: str, docs_tree: str
) -> str:
    """The short hash of the flows and their series, the UI under test and the docs.

    ``ui_source`` is ``build:<src_ui tree>`` or ``deployed``; ``docs_tree`` is the
    40-hex tree id of the docs or ``worktree``. A later head with the same hash
    would run the same stories against the same UI and docs.
    """
    if ui_source != "deployed" and not _UI_BUILD_RE.fullmatch(ui_source):
        raise ValueError("ui_source must be 'deployed' or 'build:<40-hex tree id>'")
    if docs_tree != "worktree" and not _SHA_RE.fullmatch(docs_tree):
        raise ValueError("docs_tree must be 'worktree' or a 40-hex tree id")
    pairs = []
    for key, series in flows:
        if not isinstance(key, stories.FlowKey):
            raise ValueError("each flow must be a stories.FlowKey")
        if not isinstance(series, str) or not _SERIES_RE.fullmatch(series):
            raise ValueError(f"the series of {key} must be 12 lowercase hex digits")
        pairs.append([str(key), series])
    return stories.short_hash(
        {"flows": sorted(pairs), "ui_source": ui_source, "docs_tree": docs_tree}
    )


def _selects(story: stories.Story, entry: Entry) -> bool:
    """Whether ``entry`` on its own gives ``story`` the CHANGELOG points that select it."""
    alone = (AddedEntry(entry=entry, modified=False, in_released_section=False),)
    score = score_story(story, alone, edited_sections=(), changed_files=())
    return score.changelog_points >= SELECT_THRESHOLD


def uncovered_unreleased(
    catalogue: stories.Catalogue, changelog_text: str
) -> list[str]:
    """The ``[Unreleased]`` leads no story scores SELECT_THRESHOLD CHANGELOG points on."""
    leads: list[str] = []
    for entry in parse_entries(changelog_text):
        if not _is_unreleased(entry.section):
            continue
        if any(_selects(story, entry) for story in catalogue.stories):
            continue
        lead = _shown_lead(entry)
        if lead not in leads:
            leads.append(lead)
    return leads


def _printable(text: str) -> str:
    return " ".join(_CONTROL_RE.sub(" ", text).split())


def _read_text(path: Path) -> str:
    """``path`` as UTF-8, refused past the bound CI reads a CHANGELOG from git to."""
    with path.open("rb") as handle:
        data = handle.read(ui_change.MAX_OUTPUT_BYTES + 1)
    if len(data) > ui_change.MAX_OUTPUT_BYTES:
        raise ValueError(f"{path} is larger than {ui_change.MAX_OUTPUT_BYTES} bytes")
    return data.decode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="changelog_select.py",
        description="Selection of first-time-user stories from CHANGELOG entries.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    nudge = commands.add_parser(
        "nudge",
        help="print each [Unreleased] lead that no story in the catalogue covers",
    )
    nudge.add_argument(
        "--changelog", type=Path, default=_CHANGELOG_PATH, help="CHANGELOG.md to read"
    )
    nudge.add_argument(
        "--catalogue",
        type=Path,
        default=stories.CATALOGUE_PATH,
        help="the story catalogue to score against",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """``nudge``: one uncovered ``[Unreleased]`` lead per line; exit 0, or 1 on failure."""
    args = _parser().parse_args(argv)
    try:
        catalogue = stories.load_catalogue(args.catalogue)
        text = _read_text(args.changelog)
    except (stories.CatalogueError, OSError, ValueError) as error:
        print(f"FAILED: {error}")
        return 1
    for lead in uncovered_unreleased(catalogue, text):
        print(_printable(lead))
    return 0


if __name__ == "__main__":
    sys.exit(main())
