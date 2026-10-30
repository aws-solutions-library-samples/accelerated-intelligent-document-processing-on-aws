# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/changelog_select.py, the MR story selector.

Synthetic CHANGELOGs and catalogues pin each rule: entry parsing, the whole-file
diff, scoring, the threshold, ranking, the caps and the reply's lines. Two suites
read the real files instead. Claims verbatim: every ``claims`` lead in
``scripts/ux_stories.toml`` is the bold lead of exactly one entry of
``CHANGELOG.md``, as this parser reads it. The replay: each claim's entry, taken out
of the real ``CHANGELOG.md`` and put back as the MR that added it, selects its own
story. The replay derives each base from the working tree's file rather than from
git history, because the GitLab job that runs this suite clones shallow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import changelog_select, stories  # noqa: E402
from ux_runner.changelog_select import AddedEntry, Entry  # noqa: E402

CHANGELOG = REPO_ROOT / "CHANGELOG.md"
SCRIPT = SCRIPTS / "ux_runner" / "changelog_select.py"
HEADING_RE = re.compile(r"#{1,6}(?:[ \t]|$)")

LICENSE = (
    "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
    "SPDX-License-Identifier: MIT-0\n\n# Changelog\n\n"
)

SAMPLE = (
    LICENSE
    + """\
## [Unreleased]

### Added

- **Prefix mappings route a folder to a profile.** Documents that land under a \
prefix use its profile ([guide](docs/configuration-profiles.md#prefix-mappings)).

### Fixed

- **The viewer keeps the last page** ([#1327](https://github.com/o/r/issues/1327)).

## [0.6.12]

### Added

- **Sonnet 5.5 is selectable.** It is in every picklist.
  A continuation line,
    indented twice.
* **A star bullet.** Its text.

## Templates
   - us-west-2: `https://example.invalid/idp-main_0.6.12.yaml`

### Added

- **An entry under a stray heading.** Its text.
"""
)

PERSONAS = {
    "analyst": {"group": "Author", "background": "You started here this week."},
    "lead": {"group": "Viewer", "background": "You check processing results."},
    "admin": {"group": "Admin", "background": "You look after this deployment."},
}


def _story(
    story_id: str,
    *,
    tier: str = "p0",
    expectations: dict[str, str] | None = None,
    mr_as: Sequence[str] | None = None,
    docs: Sequence[str] = (),
    issues: Sequence[int] = (),
    terms: Sequence[str] = (),
    ui_paths: Sequence[str] = (),
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": story_id,
        "title": "Find the place a thing is shown",
        "intent": "Find out where the thing is shown, and what it says.",
        "kind": "read",
        "documented_steps": 3,
        "as": expectations or {"analyst": "allowed"},
        "tier": tier,
        "since": "0.6.0",
        "origin": "smoke",
        "match": {
            "docs": list(docs),
            "issues": list(issues),
            "terms": list(terms),
            "ui_paths": list(ui_paths),
        },
    }
    if mr_as is not None:
        body["mr_as"] = list(mr_as)
    return body


def _catalogue(*bodies: dict[str, Any], **runs: int) -> stories.Catalogue:
    data: dict[str, Any] = {
        "version": 1,
        "personas": PERSONAS,
        "stories": list(bodies),
    }
    if runs:
        data["runs"] = runs
    return stories.parse_catalogue(data, source="<test catalogue>")


def _entry(text: str, *, section: str = "Unreleased", kind: str = "Added") -> Entry:
    parsed = changelog_select.parse_entries(
        f"## [{section}]\n\n### {kind}\n\n- {text}\n"
    )
    assert len(parsed) == 1
    return parsed[0]


def _added(*texts: str) -> list[AddedEntry]:
    return [
        AddedEntry(entry=_entry(text), modified=False, in_released_section=False)
        for text in texts
    ]


def _real_catalogue() -> stories.Catalogue:
    return stories.load_catalogue()


def _real_changelog() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def _without(text: str, entry: Entry) -> str:
    """``text`` without ``entry``'s lines, as the CHANGELOG before the MR adding it."""
    lines = text.split("\n")
    start = entry.line - 1
    end = start + 1
    while end < len(lines) and not (
        lines[end].startswith(("- ", "* ")) or HEADING_RE.match(lines[end])
    ):
        end += 1
    return "\n".join(lines[:start] + lines[end:])


@pytest.mark.unit
def test_entries_carry_their_section_kind_lead_and_line() -> None:
    entries = changelog_select.parse_entries(SAMPLE)
    assert [(e.section, e.kind, e.lead, e.line) for e in entries] == [
        ("Unreleased", "Added", "Prefix mappings route a folder to a profile.", 10),
        ("Unreleased", "Fixed", "The viewer keeps the last page", 14),
        ("0.6.12", "Added", "Sonnet 5.5 is selectable.", 20),
        ("0.6.12", "Added", "A star bullet.", 23),
        ("0.6.12", "Added", "An entry under a stray heading.", 30),
    ]


@pytest.mark.unit
def test_continuation_lines_are_joined_and_whitespace_normalised() -> None:
    sonnet = changelog_select.parse_entries(SAMPLE)[2]
    assert sonnet.text == (
        "**Sonnet 5.5 is selectable.** It is in every picklist. "
        "A continuation line, indented twice."
    )


@pytest.mark.unit
def test_a_heading_ends_an_entry_and_indented_lines_outside_one_are_ignored() -> None:
    entries = changelog_select.parse_entries(SAMPLE)
    assert all("##" not in entry.text for entry in entries)
    assert all("us-west-2" not in entry.text for entry in entries)
    assert entries[3].text == "**A star bullet.** Its text."


@pytest.mark.unit
def test_an_h2_resets_the_kind_and_only_a_bracketed_h2_changes_the_section() -> None:
    text = "## [0.6.12]\n\n### Fixed\n\n## Templates\n\n- **No kind here.** x\n"
    (entry,) = changelog_select.parse_entries(text)
    assert (entry.section, entry.kind) == ("0.6.12", "")
    before = changelog_select.parse_entries("- **Before any section.** x\n")
    assert [(e.section, e.kind) for e in before] == [("", "")]


@pytest.mark.unit
def test_an_atx_heading_needs_a_space_and_at_most_six_hashes() -> None:
    text = (
        "## [Unreleased]\n\n### Added\n\n- **One.** first\n#123 is not a heading\n"
        "####### nor is this\n### Fixed ###\n\n- **Two.** second\n"
    )
    one, two = changelog_select.parse_entries(text)
    assert one.text == "**One.** first #123 is not a heading ####### nor is this"
    assert one.issues == (123,)
    assert two.kind == "Fixed"


@pytest.mark.unit
def test_the_lead_follows_an_optional_warning_sign_and_keeps_its_markdown() -> None:
    assert _entry("⚠️ **Breaking.** body").lead == "Breaking."
    assert _entry("⚠ **No selector.** body").lead == "No selector."
    assert _entry("**Documents record *where* it came from.** b").lead == (
        "Documents record *where* it came from."
    )
    assert _entry("**`allowDocumentUpdate` is editable.** b").lead == (
        "`allowDocumentUpdate` is editable."
    )
    assert _entry("Plain text, no bold lead.").lead == ""
    assert _entry("Text then **bold** later.").lead == ""


@pytest.mark.unit
def test_doc_links_are_unique_in_order_and_keep_their_anchor() -> None:
    entry = _entry(
        "**x** [a](docs/web-ui.md#upload-documents) [b](./docs/web-ui.md) "
        "[c](docs/web-ui.md#upload-documents) [d](docs/sub/page.md#) "
        "[e](../docs/other.md) [f](https://example.invalid/docs/other.md) "
        "[g](docs/notes.txt)"
    )
    assert entry.docs == ("web-ui.md#upload-documents", "web-ui.md", "sub/page.md")


@pytest.mark.unit
def test_issue_numbers_come_from_both_forms_sorted_and_unique() -> None:
    entry = _entry(
        "**x** [#1411](https://github.com/o/r/issues/1411) and #808, #12, "
        "/issues/7 again #12"
    )
    assert entry.issues == (7, 12, 808, 1411)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "an entity &#35; is not an issue",
        "a fragment /#35 is not one",
        "a word#35 is not one",
        "#35abc is not one",
        "#" + "9" * 5000 + " is too long to be one",
        "#٣٥ is not ASCII",
    ],
)
def test_text_that_only_looks_like_an_issue_is_not_one(text: str) -> None:
    assert _entry(f"**x** {text}").issues == ()


@pytest.mark.unit
def test_crlf_and_a_byte_order_mark_parse_like_plain_text() -> None:
    plain = changelog_select.parse_entries(SAMPLE)
    assert (
        changelog_select.parse_entries("\ufeff" + SAMPLE.replace("\n", "\r\n")) == plain
    )


@pytest.mark.unit
def test_an_empty_bullet_is_not_an_entry() -> None:
    assert changelog_select.parse_entries("## [Unreleased]\n\n- \n- \n") == []


@pytest.mark.unit
def test_a_new_entry_is_added_and_the_rest_are_not() -> None:
    head = SAMPLE.replace(
        "### Fixed\n", "### Fixed\n\n- **A new fix.** Fixed it ([#5](x/issues/5)).\n", 1
    )
    added = changelog_select.added_entries(SAMPLE, head)
    assert [(a.entry.lead, a.modified, a.in_released_section) for a in added] == [
        ("A new fix.", False, False)
    ]
    assert added[0].entry.issues == (5,)


@pytest.mark.unit
def test_an_entry_a_release_cut_moves_is_not_added() -> None:
    cut = SAMPLE.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [0.6.13]\n", 1)
    assert [e.section for e in changelog_select.parse_entries(cut)][:2] == [
        "0.6.13",
        "0.6.13",
    ]
    assert changelog_select.added_entries(SAMPLE, cut) == []


@pytest.mark.unit
def test_a_reworded_entry_is_added_and_marked_modified() -> None:
    head = SAMPLE.replace("It is in every picklist.", "It is in every list.")
    (added,) = changelog_select.added_entries(SAMPLE, head)
    assert added.entry.lead == "Sonnet 5.5 is selectable."
    assert added.modified is True
    assert added.in_released_section is True


@pytest.mark.unit
def test_an_entry_under_a_released_heading_is_flagged_wherever_it_lands() -> None:
    head = SAMPLE.replace(
        "* **A star bullet.** Its text.\n",
        "* **A star bullet.** Its text.\n- **Landed under the release.** x\n",
    )
    (added,) = changelog_select.added_entries(SAMPLE, head)
    assert (added.entry.section, added.in_released_section) == ("0.6.12", True)
    assert added.modified is False


@pytest.mark.unit
def test_lead_less_entries_are_never_modified_and_no_section_is_not_released() -> None:
    base = "- an entry with no lead\n"
    head = "- another entry with no lead\n"
    (added,) = changelog_select.added_entries(base, head)
    assert (added.modified, added.in_released_section) == (False, False)
    unreleased = "## [unreleased]\n\n- **Lower case.** x\n"
    (lower,) = changelog_select.added_entries("", unreleased)
    assert lower.in_released_section is False


@pytest.mark.unit
def test_an_exact_doc_anchor_scores_three_and_links_elsewhere_on_its_pages_one() -> (
    None
):
    story = _catalogue(
        _story("route", docs=["configuration-profiles.md#prefix-mappings", "web-ui.md"])
    ).stories[0]
    added = _added(
        "**a** [x](docs/configuration-profiles.md#prefix-mappings)",
        "**b** [x](docs/configuration-profiles.md#other-anchor)",
        "**c** [x](docs/web-ui.md)",
        "**d** [x](docs/web-ui.md#upload-documents)",
        "**e** [x](docs/unrelated.md)",
    )
    score = changelog_select.score_story(
        story, added, edited_sections=(), changed_files=()
    )
    assert score.changelog_points == 3 + 3 + changelog_select.PAGE_POINTS == 7
    assert score.reasons == (
        "anchor:configuration-profiles.md#prefix-mappings",
        "page:configuration-profiles.md",
        "anchor:web-ui.md",
        "page:web-ui.md",
    )
    assert score.entries == (0, 1, 2, 3)


@pytest.mark.unit
def test_page_hints_never_select_however_many_entries_carry_them() -> None:
    catalogue = _catalogue(_story("hinted", docs=["test-studio.md#versioning"]))
    added = _added(
        *(f"**{n}.** See [x](docs/test-studio.md#other-{n})." for n in range(4))
    )
    (score,) = changelog_select.select(catalogue, added).scores
    assert (score.changelog_points, score.entries) == (1, (0, 1, 2, 3))
    assert changelog_select.select(catalogue, added).stories == ()
    with_term = _added("**Versioning.** See [x](docs/test-studio.md#other).")
    catalogue = _catalogue(
        _story("hinted", docs=["test-studio.md#versioning"], terms=["versioning"])
    )
    assert changelog_select.select(catalogue, with_term).stories == ("hinted",)


@pytest.mark.unit
def test_each_shared_issue_scores_two() -> None:
    story = _catalogue(_story("history", issues=[573, 1327])).stories[0]
    (score_entry,) = _added(
        "**x** #573 and [#1327](https://github.com/o/r/issues/1327)"
    )
    score = changelog_select.score_story(
        story, [score_entry], edited_sections=(), changed_files=()
    )
    assert (score.changelog_points, score.reasons) == (4, ("issue:573", "issue:1327"))


@pytest.mark.unit
def test_terms_match_whole_words_case_insensitively_at_most_twice_an_entry() -> None:
    story = _catalogue(
        _story("highlight", terms=["bounding box", "page image", "highlight", "box"])
    ).stories[0]
    added = _added(
        "**Every Bounding Box shows.** The page image and its highlight.",
        "**Boxes and boxing do not count.** Nor does bounding boxes.",
        "**One more box.** x",
    )
    score = changelog_select.score_story(
        story, added, edited_sections=(), changed_files=()
    )
    assert score.changelog_points == 2 + 0 + 1
    assert score.reasons == ("term:bounding box", "term:page image", "term:box")
    assert score.entries == (0, 2)


@pytest.mark.unit
def test_a_term_with_extra_spaces_still_matches_normalised_text() -> None:
    story = _catalogue(_story("spaced", terms=["Test  Studio"])).stories[0]
    score = changelog_select.score_story(
        story, _added("**x** in Test Studio"), edited_sections=(), changed_files=()
    )
    assert score.changelog_points == 1


@pytest.mark.unit
def test_secondary_points_add_to_the_total_but_never_select_alone() -> None:
    catalogue = _catalogue(
        _story(
            "edited",
            docs=["web-ui.md#upload-documents", "web-ui.md#document-list"],
            ui_paths=["src/ui/src/components/upload/**"],
        )
    )
    story = catalogue.stories[0]
    score = changelog_select.score_story(
        story,
        _added("**Unrelated.** x"),
        edited_sections={"web-ui.md#document-list", "other.md"},
        changed_files=["README.md", "src/ui/src/components/upload/panel.tsx"],
    )
    assert (score.changelog_points, score.secondary_points, score.total) == (0, 2, 2)
    assert score.reasons == (
        "docs_edit:web-ui.md#document-list",
        "ui_path:src/ui/src/components/upload/**",
    )
    selection = changelog_select.select(
        catalogue,
        _added("**Unrelated.** x"),
        edited_sections={"web-ui.md#document-list"},
        changed_files=["src/ui/src/components/upload/panel.tsx"],
    )
    assert selection.stories == ()
    assert selection.flows == ()
    assert [s.story for s in selection.scores] == ["edited"]


@pytest.mark.unit
def test_a_story_is_selected_from_two_changelog_points() -> None:
    catalogue = _catalogue(
        _story("one-term", terms=["prefix mapping"]),
        _story("two-terms", terms=["prefix mapping", "profile"]),
    )
    selection = changelog_select.select(
        catalogue, _added("**A prefix mapping picks a profile.** x")
    )
    assert selection.stories == ("two-terms",)
    assert [(s.story, s.changelog_points) for s in selection.scores] == [
        ("two-terms", 2),
        ("one-term", 1),
    ]


@pytest.mark.unit
def test_stories_rank_by_total_then_tier_then_id_and_the_first_two_are_taken() -> None:
    catalogue = _catalogue(
        _story("zeta", tier="p0", issues=[1]),
        _story("alpha", tier="p1", issues=[1]),
        _story("beta", tier="p0", issues=[1]),
        _story("best", tier="p1", issues=[1], terms=["profile"]),
    )
    selection = changelog_select.select(catalogue, _added("**A profile.** #1"))
    assert [s.story for s in selection.scores] == ["best", "beta", "zeta", "alpha"]
    assert selection.stories == ("best", "beta")
    capped = _catalogue(
        _story("zeta", issues=[1]), _story("beta", issues=[1]), mr_stories=1
    )
    assert changelog_select.select(capped, _added("**x** #1")).stories == ("beta",)


@pytest.mark.unit
def test_mr_as_personas_of_every_story_come_first_then_the_rest() -> None:
    catalogue = _catalogue(
        _story(
            "first",
            issues=[1],
            terms=["profile"],
            expectations={
                "admin": "allowed",
                "analyst": "not_offered",
                "lead": "allowed",
            },
            mr_as=["admin"],
        ),
        _story(
            "second",
            issues=[1],
            expectations={"analyst": "allowed", "lead": "allowed"},
        ),
        mr_flows=4,
    )
    selection = changelog_select.select(catalogue, _added("**profile** #1"))
    assert [str(key) for key in selection.flows] == [
        "first@admin",
        "second@analyst",
        "first@analyst",
        "first@lead",
    ]


@pytest.mark.unit
def test_the_flow_cap_takes_the_first_candidates() -> None:
    catalogue = _catalogue(
        _story(
            "only",
            issues=[1],
            expectations={"admin": "allowed", "analyst": "allowed", "lead": "allowed"},
        ),
        mr_flows=2,
    )
    selection = changelog_select.select(catalogue, _added("**x** #1"))
    assert [str(key) for key in selection.flows] == ["only@admin", "only@analyst"]


@pytest.mark.unit
def test_a_persona_that_cannot_sign_in_is_dropped_and_its_slot_reused() -> None:
    catalogue = _catalogue(
        _story(
            "only",
            issues=[1],
            expectations={"lead": "allowed", "analyst": "allowed", "admin": "allowed"},
        ),
        mr_flows=2,
    )
    selection = changelog_select.select(
        catalogue, _added("**x** #1"), available_groups=("Author", "Admin")
    )
    assert [str(key) for key in selection.flows] == ["only@analyst", "only@admin"]
    assert selection.dropped_personas == ("lead",)
    past_the_cap = changelog_select.select(
        catalogue, _added("**x** #1"), available_groups=("Viewer", "Author")
    )
    assert [str(key) for key in past_the_cap.flows] == ["only@lead", "only@analyst"]
    assert past_the_cap.dropped_personas == ()


@pytest.mark.unit
def test_each_added_entry_gets_one_chosen_line_naming_its_best_selected_story() -> None:
    catalogue = _catalogue(
        _story("strong", issues=[1, 2]),
        _story("weak", issues=[1]),
        _story("other", issues=[3]),
        mr_stories=2,
    )
    added = _added(
        "**Both.** #1 #2", "**Neither.** x", "**Other.** #3", "lead-less text"
    )
    selection = changelog_select.select(catalogue, added)
    assert selection.stories == ("strong", "other")
    assert [(c.kind, c.lead, c.story) for c in selection.chosen] == [
        ("Added", "Both.", "strong"),
        ("Added", "Neither.", None),
        ("Added", "Other.", "other"),
        ("Added", "lead-less text", None),
    ]
    assert selection.uncovered == ("Neither.", "lead-less text")
    assert selection.added == tuple(added)


@pytest.mark.unit
def test_an_entry_whose_story_the_caps_left_out_is_covered_and_says_by_what() -> None:
    catalogue = _catalogue(
        _story("first", issues=[1], terms=["profile"]),
        _story("second", issues=[1], terms=["profile"]),
        _story("third", issues=[2]),
        mr_stories=2,
    )
    selection = changelog_select.select(
        catalogue, _added("**A profile.** #1", "**Third.** #2", "**None.** #9")
    )
    assert selection.stories == ("first", "second")
    assert [(c.lead, c.story, c.covered_by) for c in selection.chosen] == [
        ("A profile.", "first", None),
        ("Third.", None, "third"),
        ("None.", None, None),
    ]
    assert selection.uncovered == ("None.",)


@pytest.mark.unit
def test_the_mr_s_uncovered_leads_are_the_nightly_s_for_the_same_entries() -> None:
    catalogue = _real_catalogue()
    entries = (
        "**Pipeline hooks can now be told to update documents.** See "
        "[hooks](docs/feature-platform.md#pipeline-hooks).",
        "**Pick a model in the UI.** See "
        "[config](docs/configuration.md#pattern-configuration-via-web-ui).",
        "**Highlights explain themselves.** See "
        "[viewers](docs/extraction-and-confidence.md#what-the-viewers-show).",
        "**Something nobody does in the UI.** See [x](docs/unrelated.md).",
    )
    selection = changelog_select.select(catalogue, _added(*entries))
    assert len(selection.stories) == catalogue.runs.mr_stories
    unreleased = "## [Unreleased]\n\n### Added\n\n" + "".join(
        f"- {entry}\n" for entry in entries
    )
    assert list(selection.uncovered) == changelog_select.uncovered_unreleased(
        catalogue, unreleased
    )
    assert selection.uncovered == ("Something nobody does in the UI.",)
    left_out = [line for line in selection.chosen if line.story is None]
    assert [line.covered_by for line in left_out[:1]] == ["change-hook-update-setting"]


@pytest.mark.unit
def test_a_link_to_another_section_of_a_declared_page_selects_nothing_alone() -> None:
    catalogue = _real_catalogue()
    selection = changelog_select.select(
        catalogue,
        _added("**An Annotator inbox.** See [inbox](docs/web-ui.md#inbox)."),
    )
    assert selection.stories == () and selection.flows == ()
    declaring = [
        story.id
        for story in catalogue.stories
        if any(doc.startswith("web-ui.md") for doc in story.match.docs)
    ]
    assert len(declaring) > catalogue.runs.mr_stories
    hinted = {score.story for score in selection.scores}
    assert set(declaring) <= hinted
    assert all(score.changelog_points == 1 for score in selection.scores)


@pytest.mark.unit
def test_warnings_name_a_released_section_and_a_rewording() -> None:
    catalogue = _catalogue(_story("only", issues=[1]))
    entry = _entry("**Old lead.** #1", section="0.6.12")
    added = [AddedEntry(entry=entry, modified=True, in_released_section=True)]
    selection = changelog_select.select(catalogue, added)
    assert selection.warnings == (
        "in released section [0.6.12]: Old lead.",
        "rewords an existing entry: Old lead.",
    )


@pytest.mark.unit
def test_a_long_lead_less_entry_is_shown_by_an_excerpt() -> None:
    text = "word " * 60
    selection = changelog_select.select(
        _catalogue(_story("x", issues=[9])), _added(text)
    )
    (lead,) = selection.uncovered
    assert len(lead) == 120
    assert lead.endswith("…")
    assert lead.startswith("word word")


def _canonical_short_hash(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


SERIES_A = "0123456789ab"
SERIES_B = "ba9876543210"
TREE = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"


@pytest.mark.unit
def test_the_selection_hash_is_the_short_hash_of_the_sorted_flows_ui_and_docs() -> None:
    flows = [
        (stories.FlowKey("route", "new-analyst"), SERIES_B),
        (stories.FlowKey("route", "admin"), SERIES_A),
    ]
    value = changelog_select.selection_hash(
        flows, ui_source=f"build:{TREE}", docs_tree=TREE
    )
    assert value == _canonical_short_hash(
        {
            "flows": [["route@admin", SERIES_A], ["route@new-analyst", SERIES_B]],
            "ui_source": f"build:{TREE}",
            "docs_tree": TREE,
        }
    )
    assert value == changelog_select.selection_hash(
        list(reversed(flows)), ui_source=f"build:{TREE}", docs_tree=TREE
    )


@pytest.mark.unit
def test_the_selection_hash_changes_with_the_ui_the_docs_and_a_series() -> None:
    flows = [(stories.FlowKey("route", "admin"), SERIES_A)]
    base = changelog_select.selection_hash(flows, ui_source="deployed", docs_tree=TREE)
    others = {
        changelog_select.selection_hash(
            flows, ui_source=f"build:{TREE}", docs_tree=TREE
        ),
        changelog_select.selection_hash(
            flows, ui_source="deployed", docs_tree="worktree"
        ),
        changelog_select.selection_hash(
            [(stories.FlowKey("route", "admin"), SERIES_B)],
            ui_source="deployed",
            docs_tree=TREE,
        ),
        changelog_select.selection_hash([], ui_source="deployed", docs_tree=TREE),
    }
    assert base not in others
    assert len(others) == 4


@pytest.mark.unit
@pytest.mark.parametrize(
    ("flows", "ui_source", "docs_tree"),
    [
        ([], "build", TREE),
        ([], "build:" + TREE[:12], TREE),
        ([], "deployed", TREE.upper()),
        ([], "deployed", "head"),
        ([(stories.FlowKey("route", "admin"), "XYZ")], "deployed", TREE),
        ([("route@admin", SERIES_A)], "deployed", TREE),
    ],
)
def test_the_selection_hash_refuses_inputs_of_the_wrong_shape(
    flows: Any, ui_source: str, docs_tree: str
) -> None:
    with pytest.raises(ValueError):
        changelog_select.selection_hash(flows, ui_source=ui_source, docs_tree=docs_tree)


@pytest.mark.unit
def test_uncovered_unreleased_lists_the_unreleased_leads_no_story_selects() -> None:
    catalogue = _catalogue(
        _story("route", docs=["configuration-profiles.md#prefix-mappings"]),
        _story("viewer", terms=["viewer"]),
    )
    assert changelog_select.uncovered_unreleased(catalogue, SAMPLE) == [
        "The viewer keeps the last page"
    ]


@pytest.mark.unit
def test_the_nudge_command_prints_one_lead_a_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    toml = tmp_path / "stories.toml"
    toml.write_text(
        'version = 1\n\n[personas.analyst]\ngroup = "Author"\n'
        'background = "You started here this week."\n\n[[stories]]\nid = "route"\n'
        'title = "Find the place a thing is shown"\n'
        'intent = "Find out where the thing is shown."\nkind = "read"\n'
        'documented_steps = 3\nas = { analyst = "allowed" }\ntier = "p0"\n'
        'since = "0.6.0"\norigin = "smoke"\n\n[stories.match]\n'
        'docs = ["configuration-profiles.md#prefix-mappings"]\n',
        encoding="utf-8",
    )
    code = changelog_select.main(
        ["nudge", "--changelog", str(changelog), "--catalogue", str(toml)]
    )
    assert code == 0
    assert capsys.readouterr().out == "The viewer keeps the last page\n"


@pytest.mark.unit
def test_the_nudge_command_fails_with_one_line_on_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = changelog_select.main(["nudge", "--changelog", str(tmp_path / "absent.md")])
    assert code == 1
    out = capsys.readouterr().out
    assert out.startswith("FAILED: ") and out.count("\n") == 1


@pytest.mark.unit
def test_the_nudge_command_refuses_a_changelog_past_the_git_read_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(changelog_select.ui_change, "MAX_OUTPUT_BYTES", 64)
    assert changelog_select.main(["nudge", "--changelog", str(changelog)]) == 1
    assert "larger than 64 bytes" in capsys.readouterr().out


@pytest.mark.unit
def test_the_nudge_command_runs_by_path_isolated_on_the_real_files() -> None:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(SCRIPT), "nudge"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    printed = done.stdout.splitlines()
    assert printed == changelog_select.uncovered_unreleased(
        _real_catalogue(), _real_changelog()
    )


@pytest.mark.unit
def test_every_entry_of_the_real_changelog_starts_at_a_bullet_line() -> None:
    text = _real_changelog()
    lines = text.split("\n")
    entries = changelog_select.parse_entries(text)
    assert entries, "CHANGELOG.md parsed to no entries at all"
    for entry in entries:
        assert lines[entry.line - 1].startswith(("- ", "* ")), entry.line
        assert entry.text


def _toml_line(lead: str) -> str:
    """The line of scripts/ux_stories.toml holding ``lead``, for a failure message."""
    probe = lead[:40]
    lines = stories.CATALOGUE_PATH.read_text(encoding="utf-8").splitlines()
    found = next((n for n, line in enumerate(lines, 1) if probe in line), None)
    return (
        f"scripts/ux_stories.toml line {found}" if found else "scripts/ux_stories.toml"
    )


@pytest.mark.unit
def test_every_claim_lead_is_the_lead_of_exactly_one_changelog_entry() -> None:
    """A claim quotes its CHANGELOG lead verbatim; the section holding it is not checked,
    so the release cut that relabels ``[Unreleased]`` cannot fail this."""
    entries = changelog_select.parse_entries(_real_changelog())
    problems = []
    for story in _real_catalogue().stories:
        for claim in story.claims:
            lead = " ".join(claim.lead.split())
            count = sum(1 for entry in entries if entry.lead == lead)
            if count != 1:
                problems.append(
                    f"story {story.id!r} ({_toml_line(claim.lead)}): the claim lead "
                    f"{lead!r} is the bold lead of {count} CHANGELOG.md entries, not "
                    "one. Quote the entry's lead exactly there, or reword the claim."
                )
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_each_claims_entry_selects_its_own_story_when_replayed() -> None:
    """The MR that added each claim's entry, replayed, selects that claim's story."""
    text = _real_changelog()
    catalogue = _real_catalogue()
    entries = changelog_select.parse_entries(text)
    claims = [
        (story.id, claim.lead) for story in catalogue.stories for claim in story.claims
    ]
    assert claims, "scripts/ux_stories.toml declares no claims to replay"
    problems = []
    for story_id, lead in claims:
        lead = " ".join(lead.split())
        matching = [entry for entry in entries if entry.lead == lead]
        if len(matching) != 1:
            problems.append(
                f"story {story_id!r}: {len(matching)} entries carry its lead"
            )
            continue
        added = changelog_select.added_entries(_without(text, matching[0]), text)
        if [item.entry for item in added] != matching:
            problems.append(
                f"story {story_id!r}: the replay added {len(added)} entries"
            )
            continue
        selection = changelog_select.select(catalogue, added)
        if story_id not in selection.stories or not any(
            line.story == story_id for line in selection.chosen
        ):
            scores = [(s.story, s.changelog_points, s.total) for s in selection.scores]
            problems.append(
                f"story {story_id!r}: its CHANGELOG.md entry, replayed, selects "
                f"{selection.stories} with scores {scores}; adjust that story's "
                "[stories.match] in scripts/ux_stories.toml"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_secondary_points_rank_stories_that_tie_on_changelog_points() -> None:
    catalogue = _catalogue(
        _story("alpha", issues=[1]),
        _story("beta", issues=[1]),
        _story("gamma", issues=[1], ui_paths=["src/ui/src/components/upload/**"]),
    )
    selection = changelog_select.select(
        catalogue,
        _added("**x** #1"),
        changed_files=["src/ui/src/components/upload/panel.tsx"],
    )
    assert [s.story for s in selection.scores] == ["gamma", "alpha", "beta"]
    assert selection.stories == ("gamma", "alpha")
