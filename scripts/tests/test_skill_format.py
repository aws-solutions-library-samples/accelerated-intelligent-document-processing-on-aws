# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every skill is ``.claude/skills/<name>/SKILL.md`` in the open Agent Skills layout.

Claude Code and Cline both register a skill only from a directory holding a
``SKILL.md`` whose front matter carries a ``name`` equal to the directory name and a
``description``. A flat ``.claude/skills/<name>.md`` is read by neither as a skill,
so it is reached only if some prose happens to point at it. ``.agents/skills`` is a
symlink to the same directory for the assistants that look there, so there is one
copy of each skill. The rules are in the "Skill format" section of
``.claude/skills/documentation/SKILL.md``.

Enumerated from the directory, so a new skill is covered without an edit here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / ".claude" / "skills"
AGENTS_LINK = REPO_ROOT / ".agents" / "skills"

#: Cline's documented limit, and the tighter of the two assistants'.
MAX_DESCRIPTION = 1024
#: Skill names as both assistants accept them: lowercase words joined by hyphens.
NAME_SHAPE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRONT_MATTER_KEYS = {"name", "description", "disable-model-invocation"}

SKILL_DIRS = sorted(p for p in SKILLS.iterdir() if p.is_dir())


def _front_matter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match, f"{path.relative_to(REPO_ROOT)} does not open with --- front matter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), (
        f"{path.relative_to(REPO_ROOT)}: front matter is not a mapping"
    )
    return data


@pytest.mark.unit
def test_the_skills_directory_holds_only_skill_directories() -> None:
    assert SKILL_DIRS, f"no skill directories under {SKILLS}"
    stray = sorted(p.name for p in SKILLS.iterdir() if not p.is_dir())
    assert not stray, (
        "a file directly under .claude/skills/ is registered by no assistant; move "
        f"each to .claude/skills/<name>/SKILL.md: {stray}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_each_skill_has_valid_front_matter(skill: Path) -> None:
    path = skill / "SKILL.md"
    assert path.is_file() and not path.is_symlink(), f"{skill.name}/ has no SKILL.md"
    data = _front_matter(path)

    assert data.get("name") == skill.name, (
        f"{skill.name}/SKILL.md: name must equal the directory name, got {data.get('name')!r}"
    )
    assert NAME_SHAPE.fullmatch(skill.name), (
        f"{skill.name}: not a lowercase hyphenated name"
    )

    description = data.get("description")
    assert isinstance(description, str) and description.strip(), (
        f"{skill.name}/SKILL.md: description is missing"
    )
    assert len(description) <= MAX_DESCRIPTION, (
        f"{skill.name}/SKILL.md: description is {len(description)} characters, "
        f"over {MAX_DESCRIPTION}"
    )

    unknown = sorted(set(data) - FRONT_MATTER_KEYS)
    assert not unknown, f"{skill.name}/SKILL.md: unexpected front-matter keys {unknown}"
    if "disable-model-invocation" in data:
        assert data["disable-model-invocation"] is True, (
            f"{skill.name}/SKILL.md: disable-model-invocation must be true or absent"
        )


@pytest.mark.unit
def test_agents_skills_is_a_symlink_to_the_claude_skills() -> None:
    assert AGENTS_LINK.is_symlink(), f"{AGENTS_LINK} must be a symlink, not a copy"
    assert AGENTS_LINK.readlink().as_posix() == "../.claude/skills"
    assert AGENTS_LINK.resolve() == SKILLS.resolve()


@pytest.mark.unit
def test_there_is_no_per_assistant_copy_of_the_skills() -> None:
    """A second tree is how two assistants come to read different instructions."""
    assert not (REPO_ROOT / ".cline" / "skills").exists(), (
        ".cline/skills/ is gone: Cline reads .claude/skills/ directly"
    )
