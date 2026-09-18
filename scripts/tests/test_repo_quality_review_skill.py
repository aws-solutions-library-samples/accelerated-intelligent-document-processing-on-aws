# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the repo-quality-review skill, and for the skill-symlink class.

The skill this file guards is itself about two defect classes: a control that
exists but is never consulted, and a fix applied to the instance rather than the
class. It would be poor form to ship it guarded by a per-skill assertion, which is
the shape it warns about — so the symlink check below enumerates **every**
``.cline/skills`` entry from the directory rather than naming one.

Why the symlink matters: ``.claude/skills/`` is canonical and each
``.cline/skills/*.md`` is a symlink to its counterpart (see the "Two skill systems,
one source of truth" section of ``.claude/skills/documentation.md``). A real file
there diverges silently, and the two assistants then read different instructions.

The content assertions are deliberately thin. They pin only the load-bearing
promises a future edit could remove without anyone noticing: that the skill is
read-only, and that both defect classes are still in it. They do not police prose.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = REPO_ROOT / ".claude" / "skills"
CLINE_SKILLS = REPO_ROOT / ".cline" / "skills"
SKILL = CLAUDE_SKILLS / "repo-quality-review.md"


@pytest.mark.unit
def test_the_skill_exists() -> None:
    assert SKILL.is_file(), f"{SKILL} is missing"


@pytest.mark.unit
def test_the_skill_is_registered_in_claude_md() -> None:
    """An unregistered skill is one nobody finds."""
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".claude/skills/repo-quality-review.md" in claude_md


@pytest.mark.unit
def test_every_cline_skill_is_a_symlink_into_claude_skills() -> None:
    """Enumerated from the directory, not from a list someone must remember."""
    entries = sorted(p for p in CLINE_SKILLS.iterdir() if p.name.endswith(".md"))
    assert entries, f"no skill entries found under {CLINE_SKILLS}"

    copies = [p.name for p in entries if not p.is_symlink()]
    assert not copies, (
        f".cline/skills entries must be symlinks to .claude/skills, not copies: {copies}"
    )

    for entry in entries:
        target = Path(os.path.realpath(entry))
        assert target.is_file(), f"{entry.name} points at a missing file: {target}"
        assert target.parent == CLAUDE_SKILLS.resolve(), (
            f"{entry.name} resolves outside .claude/skills: {target}"
        )


@pytest.mark.unit
def test_the_cline_entry_for_this_skill_resolves_to_the_canonical_file() -> None:
    entry = CLINE_SKILLS / "repo-quality-review.md"
    assert entry.is_symlink(), f"{entry} must be a symlink to the .claude skill"
    assert os.path.realpath(entry).endswith(".claude/skills/repo-quality-review.md")


@pytest.mark.unit
def test_the_skill_keeps_its_read_only_constraint_and_both_defect_classes() -> None:
    """The three promises the skill would be unsafe or useless without."""
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "read-only by construction",  # the hard constraint
        "never consulted at the decision point",  # Class 1
        "the class was not",  # Class 2
        "Known non-defects",  # the withdrawal register
    ):
        assert phrase in text, f"the skill no longer states: {phrase!r}"
