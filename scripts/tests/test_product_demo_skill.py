# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The product-demo skill and its storyboard file.

The skill is a procedure an agent follows against a live stack, so nothing else
exercises it. These pin the parts a run would otherwise discover the hard way:
that the user is asked before anything is recorded, that the recorder commands it
gives are ones the recorder accepts, that the paths it cites exist, that the storyboard file parses into what the skill reads, and
that the skill is findable.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / ".claude" / "skills" / "product-demo" / "SKILL.md"
STORYBOARDS = REPO_ROOT / "scripts" / "demo_storyboards.yaml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ux_recorder  # noqa: E402
from rbac_common import template_group_names  # noqa: E402

#: Every Cognito group the stack creates, read from template.yaml.
VALID_PERSONAS = set(template_group_names())
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


def _recorder_commands() -> list[list[str]]:
    """Every ``ux_recorder.py <subcommand> ...`` the skill gives, as words."""
    joined = re.sub(r"\\\n\s*", " ", _skill())
    return [
        shlex.split(match, comments=True)
        for match in re.findall(r"ux_recorder\.py ([a-z][^`\n]*)", joined)
    ]


def _recorder_subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = ux_recorder.build_parser()
    (sub,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return dict(sub.choices)


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
        """Some recorder command the skill gives starts it with ``--kind demo``."""
        starts = [argv for argv in _recorder_commands() if argv[0] == "start"]
        assert any(
            argv[argv.index("--kind") + 1] == "demo"
            for argv in starts
            if "--kind" in argv
        ), starts

    def test_asks_the_user_with_the_question_tool_before_recording(self):
        """The user picks a storyboard through ``AskUserQuestion`` before the
        recorder is started: the tool is named ahead of the first recorder command."""
        text = _skill()
        assert "AskUserQuestion" in text
        assert text.index("AskUserQuestion") < text.index("ux_recorder.py start")

    def test_every_recorder_command_is_one_the_recorder_accepts(self):
        """Each ``ux_recorder.py`` subcommand and option the skill cites exists, and
        each ``--kind`` it passes is one ``start`` offers."""
        subcommands = _recorder_subcommands()
        commands = _recorder_commands()
        assert commands, "the skill cites no ux_recorder.py command"
        for argv in commands:
            assert argv[0] in subcommands, argv
            parser = subcommands[argv[0]]
            options = {o for a in parser._actions for o in a.option_strings}
            for word in argv[1:]:
                if word.startswith("--"):
                    assert word in options, (argv[0], word)
            if "--kind" in argv:
                (kind,) = [a for a in parser._actions if "--kind" in a.option_strings]
                assert argv[argv.index("--kind") + 1] in (kind.choices or ()), argv

    def test_every_path_the_skill_cites_exists(self):
        cited = set(re.findall(r"(?:\.claude/skills|scripts|docs)/[\w./-]+", _skill()))
        assert cited, "the skill cites no repository path"
        missing = sorted(
            path for path in cited if not (REPO_ROOT / path.rstrip(".")).exists()
        )
        assert not missing, missing

    def test_is_registered_in_claude_md(self):
        claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert ".claude/skills/product-demo/SKILL.md" in claude_md

    def test_scripts_readme_documents_demo_mode(self):
        readme = (REPO_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
        assert "--kind demo" in readme
