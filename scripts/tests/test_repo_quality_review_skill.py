# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the repo-quality-review skill, and for the skill-registration class.

The skill this file guards is itself about two defect classes: a control that
exists but is never consulted, and a fix applied to the instance rather than the
class. It would be poor form to ship it guarded by per-skill assertions, which is
the shape it warns about — so the structural check below enumerates from the
**directory** rather than naming one file: every ``.claude/skills/<name>/SKILL.md``
must have a row in the ``CLAUDE.md`` skill table. Writing it required first fixing
the gap it exposed (``sync-pii-anonymizer`` had no row), which is the intended
order: close the class, do not narrow the assertion to dodge it. The format of the
skill files themselves is guarded in ``test_skill_format.py``.

The content assertion is derived from the skill itself: every measurement it refers
to by letter must have a section defining it, which is checked outside code fences
because the skill embeds a report template whose headings repeat the real ones.
Nothing here pins a heading's or a section's wording.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = REPO_ROOT / ".claude" / "skills"
SKILL = CLAUDE_SKILLS / "repo-quality-review" / "SKILL.md"


def _fence_stripped(text: str) -> str:
    """``text`` with fenced code blocks removed.

    The skill embeds a suggested-report template whose headings duplicate the real
    ones, so a heading assertion has to ignore fenced content or it proves nothing.
    """
    return re.sub(r"^```.*?^```", "", text, flags=re.DOTALL | re.MULTILINE)


@pytest.mark.unit
def test_the_skill_exists() -> None:
    assert SKILL.is_file(), f"{SKILL} is missing"


@pytest.mark.unit
def test_the_skill_is_registered_in_claude_md() -> None:
    """An unregistered skill is one nobody finds."""
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".claude/skills/repo-quality-review/SKILL.md" in claude_md


@pytest.mark.unit
def test_every_claude_skill_is_registered_in_claude_md() -> None:
    """The class, not just this instance: enumerated from the directory.

    The skill table in ``CLAUDE.md`` is the only index of ``.claude/skills/``, so a
    file with no row is one no assistant is told to consult. Guarding only the skill
    this PR adds would be a Class 2 defect — the instance fixed, the class left open —
    inside the very file that teaches Class 2. This closes it.
    """
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    unregistered = sorted(
        p.parent.name
        for p in CLAUDE_SKILLS.glob("*/SKILL.md")
        if f".claude/skills/{p.parent.name}/SKILL.md" not in claude_md
    )
    assert any(CLAUDE_SKILLS.glob("*/SKILL.md")), (
        f"no skills found under {CLAUDE_SKILLS}"
    )
    assert not unregistered, (
        "every .claude/skills/*/SKILL.md needs a row in the CLAUDE.md skill table; "
        f"missing: {unregistered}"
    )


@pytest.mark.unit
def test_every_measurement_the_skill_refers_to_is_defined() -> None:
    """A measurement referred to by letter has a section defining it.

    Derived from the skill's own references rather than from a list here, so a new
    measurement needs no edit to this file and a deleted one still fails while any
    dimension or worked example refers to it.
    """
    body = _fence_stripped(SKILL.read_text(encoding="utf-8"))
    referred = set(re.findall(r"\bmeasurement ([A-Z]2?)\b", body))
    assert referred, "the skill refers to no measurement by letter"
    missing = sorted(
        label
        for label in referred
        if not re.search(rf"^### {label}\.|^\*\*{label} ", body, flags=re.MULTILINE)
    )
    assert not missing, (
        "the skill refers to these measurements by letter but no longer defines "
        f"them (expected a '### <label>.' or '**<label> ' heading): {missing}"
    )
