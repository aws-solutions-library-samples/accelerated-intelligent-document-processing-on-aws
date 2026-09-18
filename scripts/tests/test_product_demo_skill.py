# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The product-demo skill and its storyboard file.

The skill is a procedure an agent follows against a live stack, so nothing else
exercises it. These pin the parts a run would otherwise discover the hard way:
that the recorder is started in demo mode, that the user is asked before anything
is recorded, that the storyboard file parses into what the skill reads, and that
the skill is findable from both assistants.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".claude" / "skills" / "product-demo.md"
CLINE_SKILL = REPO_ROOT / ".cline" / "skills" / "product-demo.md"
STORYBOARDS = REPO_ROOT / "scripts" / "demo_storyboards.yaml"
VALID_PERSONAS = {"Admin", "Author", "Reviewer", "Annotator", "Viewer"}
REQUIRED_FIELDS = {
    "id",
    "title",
    "source",
    "audience",
    "hook",
    "persona",
    "duration_target",
    "fixtures",
    "chapters",
    "closing",
    "takeaways",
    "not_shown",
    "docs",
    "category",
    "last_recorded",
}


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _storyboards() -> list[dict]:
    with STORYBOARDS.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["storyboards"]


@pytest.mark.unit
class TestStoryboardsFile:
    def test_parses_and_every_entry_has_what_the_skill_reads(self):
        boards = _storyboards()
        assert boards, "the file should hold at least the seed storyboard"
        for board in boards:
            missing = REQUIRED_FIELDS - set(board)
            assert not missing, (
                f"storyboard {board.get('id')!r} lacks {sorted(missing)}"
            )

    def test_ids_are_unique_slugs(self):
        ids = [b["id"] for b in _storyboards()]
        assert len(ids) == len(set(ids))
        for slug in ids:
            assert slug == slug.lower() and " " not in slug, slug

    def test_personas_are_groups_the_session_helper_can_create(self):
        for board in _storyboards():
            assert board["persona"] in VALID_PERSONAS, board["id"]

    def test_chapters_carry_narration_and_steps(self):
        for board in _storyboards():
            assert 3 <= len(board["chapters"]) <= 10, (
                f"{board['id']}: a demo is five to eight chapters, not a checklist"
            )
            for chapter in board["chapters"]:
                assert (
                    chapter.get("label") and chapter.get("say") and chapter.get("do")
                ), f"{board['id']}: chapter {chapter.get('label')!r} is incomplete"

    def test_takeaways_fit_the_end_card(self):
        """render puts the first five takeaways on the end card; more are lost."""
        for board in _storyboards():
            assert 1 <= len(board["takeaways"]) <= 5, board["id"]

    def test_narration_never_names_ids_or_clicks(self):
        for board in _storyboards():
            spoken = " ".join(
                [board["hook"], board["closing"]]
                + [c["say"] for c in board["chapters"]]
            ).lower()
            for banned in ("click", "uid ", "stack", "@"):
                assert banned not in spoken, f"{board['id']}: narration says {banned!r}"

    def test_docs_targets_exist(self):
        for board in _storyboards():
            page = board["docs"].split("#")[0]
            assert (REPO_ROOT / page).exists(), f"{board['id']}: {page} is missing"

    def test_categories_are_headings_of_the_demo_videos_page(self):
        page = (REPO_ROOT / "docs" / "demo-videos.md").read_text(encoding="utf-8")
        for board in _storyboards():
            assert f"## {board['category']}" in page, (
                f"{board['id']}: {board['category']!r} is not a docs/demo-videos.md heading"
            )


@pytest.mark.unit
class TestTheSkill:
    def test_starts_the_recorder_in_demo_mode(self):
        text = _skill()
        for needle in (
            "ux_recorder.py start --kind demo",
            "--title",
            "--subtitle",
            "ux_recorder.py mark",
            "ux_recorder.py stop",
            "ux_recorder.py render",
            "demo.md",
            "demo.mp4",
            "scratch/ux-recordings",
        ):
            assert needle in text, needle

    def test_proposes_three_and_asks_before_recording(self):
        text = _skill()
        assert "three" in text.lower()
        assert "AskUserQuestion" in text
        assert "rehears" in text.lower(), "a demo must be rehearsed before recording"

    def test_reuses_the_sibling_skills_rather_than_restating_them(self):
        text = _skill()
        assert ".claude/skills/ux-test.md" in text
        assert ".claude/skills/pr-review.md" in text
        assert "scripts/demo_storyboards.yaml" in text

    def test_states_the_privacy_rule_and_the_no_commit_rule(self):
        lowered = _skill().lower()
        assert "never commit" in lowered
        assert "ask once" in lowered or "say it once" in lowered
        assert "not deployed" in lowered, "blocked-not-deployed must be a named outcome"

    def test_hands_over_a_docs_entry_draft(self):
        text = _skill()
        assert "docs-entry.md" in text and "docs/demo-videos.md" in text
        assert "user-attachments" in text

    def test_is_registered_in_claude_md(self):
        claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert ".claude/skills/product-demo.md" in claude_md

    def test_cline_copy_is_a_symlink(self):
        assert CLINE_SKILL.is_symlink(), f"{CLINE_SKILL} must be a symlink"
        assert os.path.realpath(CLINE_SKILL).endswith(".claude/skills/product-demo.md")

    def test_scripts_readme_documents_demo_mode(self):
        readme = (REPO_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
        assert "--kind demo" in readme
