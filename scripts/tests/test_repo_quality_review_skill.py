# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the repo-quality-review skill, and for the skill-symlink class.

The skill this file guards is itself about two defect classes: a control that
exists but is never consulted, and a fix applied to the instance rather than the
class. It would be poor form to ship it guarded by per-skill assertions, which is
the shape it warns about — so both structural checks below enumerate from the
**directory** rather than naming one file: every ``.cline/skills`` entry must be a
symlink, and every ``.claude/skills/*.md`` must have a row in the ``CLAUDE.md`` skill
table. Writing the second one required first fixing the gap it exposed
(``sync-pii-anonymizer.md`` had no row), which is the intended order: close the class,
do not narrow the assertion to dodge it.

Why the symlink matters: ``.claude/skills/`` is canonical and each
``.cline/skills/*.md`` is a symlink to its counterpart (see the "Two skill systems,
one source of truth" section of ``.claude/skills/documentation.md``). A real file
there diverges silently, and the two assistants then read different instructions.

The content assertions police **structure, not prose**. They pin the load-bearing
promises a future edit could remove without anyone noticing — the read-only
constraint, both defect classes, the three mandatory sections, all ten dimensions and
every measurement label the skill claims to define — and say nothing about wording.
The distinction matters because the first draft of this file pinned only four bare
phrases, and the ``Baseline measurements`` section — over half the skill, 401 of its
830 lines when this was written — could be deleted green.

Two traps worth knowing if you extend these:

* Pin **heading** forms (``## Known non-defects``), not bare phrases. "Known
  non-defects" also appears as a row in the Inputs table, so the bare string survived
  deleting the entire register.
* Check headings against the text with fenced code blocks **stripped**. The skill
  contains a suggested-report template inside a ```` ```markdown ```` fence which
  repeats ``## Baseline measurements``, so an unstripped substring test for that
  heading passes even with the real section gone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = REPO_ROOT / ".claude" / "skills"
CLINE_SKILLS = REPO_ROOT / ".cline" / "skills"
SKILL = CLAUDE_SKILLS / "repo-quality-review.md"

# The measurements the skill promises to define. Numbered sub-measurements (``D2``)
# are headed in bold rather than with ``###``; both forms are accepted.
MEASUREMENT_LABELS = (
    "A", "B", "C", "D", "D2", "E", "F", "F2", "G", "G2", "H", "I", "I2",
)

# Sections the skill is unusable without. Compared against the fence-stripped text.
MANDATORY_SECTIONS = (
    "## Baseline measurements",
    "## Output contract",
    "## Known non-defects",
)

# ``.claude`` skills with no ``.cline`` symlink, and why. ``documentation.md``'s
# "When adding a new skill" rule says to create one, so an absence needs a reason
# recorded where a future reader can check it — that is what this table is for.
#
# Do NOT add a symlink merely to shorten this list. Whether a skill should be
# visible to Cline is a judgement about that assistant's capabilities, and creating
# one to satisfy a test would invert the decision.
CLINE_EXEMPT = {
    "full-test-battery.md": "live tier: drives the full battery against a real stack",
    "run-benchmarks.md": "live tier: runs the benchmark matrix against Bedrock",
    "run-stack-tests.md": "live tier: deploy-variant stack tests against a live stack",
    "test-upgrade.md": "live tier: deploys and upgrades real CloudFormation stacks",
    "transform-deploy-test.md": "live tier: deploys a transformed template",
}


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
    assert ".claude/skills/repo-quality-review.md" in claude_md


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
        p.name
        for p in CLAUDE_SKILLS.glob("*.md")
        if f".claude/skills/{p.name}" not in claude_md
    )
    assert not unregistered, (
        "every .claude/skills/*.md needs a row in the CLAUDE.md skill table; "
        f"missing: {unregistered}"
    )


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
def test_every_claude_skill_has_a_cline_entry() -> None:
    """The converse of the symlink test, which the first draft of this file omitted.

    ``documentation.md``'s "When adding a new skill" step says to add a ``.cline``
    symlink, so a skill without one is invisible to Cline. Closing only the other
    direction let ``sync-pii-anonymizer.md`` be forced into the ``CLAUDE.md`` table by
    the test above while still being unreadable by Cline — two sides of the
    visibility triangle guarded, the third open.

    Absences are allowed but must be *stated*: every one needs a row in
    ``CLINE_EXEMPT`` giving the reason.
    """
    linked = {
        Path(os.path.realpath(entry)).name
        for entry in CLINE_SKILLS.iterdir()
        if entry.name.endswith(".md")
    }
    missing = sorted(p.name for p in CLAUDE_SKILLS.glob("*.md") if p.name not in linked)

    undocumented = [name for name in missing if name not in CLINE_EXEMPT]
    assert not undocumented, (
        "these .claude skills have no .cline symlink and no recorded reason; either "
        "add the symlink (see documentation.md 'Two skill systems, one source of "
        f"truth') or add a CLINE_EXEMPT row saying why not: {undocumented}"
    )

    stale = sorted(set(CLINE_EXEMPT) - set(missing))
    assert not stale, (
        f"CLINE_EXEMPT names skills that now have a .cline symlink; drop them: {stale}"
    )


@pytest.mark.unit
def test_the_skill_keeps_its_read_only_constraint_and_both_defect_classes() -> None:
    """The three promises the skill would be unsafe or useless without.

    Each phrase is pinned in its **heading** form. ``Known non-defects`` on its own
    also occurs as a row in the Inputs table, so the bare string stayed satisfied
    after the entire register was deleted.
    """
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "## Hard constraint — read-only by construction",  # the hard constraint
        "## Class 1 — a control that exists as an artifact but is never consulted",
        "## Class 2 — the instance was fixed and the class was not",
        "## Known non-defects",  # the withdrawal register
    ):
        assert phrase in text, f"the skill no longer states: {phrase!r}"


@pytest.mark.unit
def test_the_skill_defines_every_measurement_it_claims() -> None:
    """The baseline measurements are the skill's substance, so pin the set of them.

    Structural, not editorial: this says nothing about what a measurement contains,
    only that a label the skill's dimension table and worked examples refer to by
    letter still has a section defining it. Without it the whole ``Baseline
    measurements`` section — 13 measurements, over half the file — could be deleted
    and every other test here would still pass.
    """
    text = SKILL.read_text(encoding="utf-8")
    missing = [
        label
        for label in MEASUREMENT_LABELS
        if not re.search(rf"^### {label}\.|^\*\*{label} ", text, flags=re.MULTILINE)
    ]
    assert not missing, (
        "the skill refers to these measurements by letter but no longer defines "
        f"them (expected a '### <label>.' or '**<label> ' heading): {missing}"
    )


@pytest.mark.unit
def test_the_skill_keeps_its_mandatory_sections_and_all_ten_dimensions() -> None:
    """Sections checked outside code fences; dimensions counted, not read.

    The ten dimensions are described as "each row is mandatory", so the count is the
    promise. Counting rows rather than matching their text keeps this structural — a
    dimension may be renamed or rewritten freely, but not dropped.
    """
    body = _fence_stripped(SKILL.read_text(encoding="utf-8"))

    absent = [heading for heading in MANDATORY_SECTIONS if heading not in body]
    assert not absent, f"the skill is missing mandatory section(s): {absent}"

    rows = re.findall(r"^\| \d+ \| \*\*", body, flags=re.MULTILINE)
    assert len(rows) == 10, (
        "the dimension table must keep all ten mandatory dimensions; found "
        f"{len(rows)}. If a dimension was genuinely retired, update this count and "
        "say why in the skill."
    )
