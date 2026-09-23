# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No commit reaching this branch suppresses CI.

A commit message carrying one of the directives in
``scripts/hooks/check_commit_text.py``'s ``CI_SUPPRESSION_DIRECTIVES`` suppresses
**every** gate on **both** CI platforms. Neither configuration opts into that and
neither can switch it off in YAML: GitHub Actions skips a ``push``- or
``pull_request``-triggered run when the head commit's message contains one, and GitLab
skips pipeline creation. Since no check on this repository is a *required* status check
(issue #933), the result is not a pull request that merges with a red gate — it is one
that merges with nothing having run and nothing for a reviewer to look at. That has
happened here: a commit in such a cluster introduced four Bandit findings that broke the
security gate for every branch cut from ``develop`` afterwards (#1072).

**What this catches, and what it cannot.** It runs wherever ``pytest scripts/tests``
runs, which is ``make test-packages-cicd`` on both platforms. So a marked commit in the
*middle* of a pull request's branch is caught on that pull request, because only the
**head** commit's message decides whether the run happens at all. A marked commit that
is the head commit takes this gate with it, and is caught on the next pull request whose
checks do run — which is late, but it is the difference between somebody finding out and
nobody finding out. The client-side half is the ``PreToolUse`` hook, which refuses the
``git commit`` before it exists; that covers commands run through the assistant's Bash
tool and nothing else. Neither half can stop a merge, for the reason #933 records.

**Scope starts at :data:`ENFORCED_FROM`, and the bound is measured rather than
asserted.** Seventeen commits before that point carry a directive; they are published on
``develop``, so their messages cannot be rewritten without a force-push that would
invalidate every branch derived from them. Rather than list seventeen SHAs with
seventeen near-identical sentences, the range simply starts after them — and
:func:`test_the_history_outside_the_scanned_range_still_holds_exactly_the_known_markers`
pins that count, so moving ``ENFORCED_FROM`` forward to make a new marker "historical"
fails here instead of passing quietly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts" / "hooks"))
from check_commit_text import (  # noqa: E402
    CI_SUPPRESSION,
    CI_SUPPRESSION_DIRECTIVES,
)

sys.path.pop(0)

pytestmark = pytest.mark.unit

#: The commit this gate is enforced from: ``develop`` when it was written. Commits at or
#: before it are outside the range, which is what makes the seventeen unrewritable ones
#: a bounded, counted fact rather than a list of carve-outs.
ENFORCED_FROM = "19b4205129f36547d3dcc85a0bc2a10c6097bf32"

#: How many commits before :data:`ENFORCED_FROM` carry a directive. Measured, not
#: chosen (2026-09-23, 9679 commits reachable). Pinned in both directions: a smaller
#: number means history was rewritten, a larger one means the start point moved forward
#: over a commit that should have been caught.
MARKERS_BEFORE_ENFORCED_FROM = 17

#: The fallback range used when :data:`ENFORCED_FROM` is not in the clone, which is the
#: normal state of a shallow CI checkout once ``develop`` has moved on. GitHub's
#: ``developer_tests`` job clones with ``fetch-depth: 0`` and never needs this; GitLab's
#: ``code_checks`` runs at the project's shallow depth and eventually will.
#:
#: Scanning what the clone has is the only honest option there — failing would red-line
#: a branch for a condition nobody can fix from this tree, and skipping would make the
#: gate quietly decorative, which is the defect class this repository keeps finding. The
#: window is smaller than the distance from HEAD to the newest pre-enforcement marker
#: (68 commits when this was written, and it only grows), so the fallback cannot reach
#: one of those and report it as a new finding;
#: :func:`test_the_fallback_window_cannot_reach_a_pre_enforcement_marker` measures that
#: rather than trusting it.
FALLBACK_WINDOW = 50

# A commit message contains newlines and blank lines, so the records and their fields
# are separated by control characters a message cannot hold. Git is asked for them by
# ESCAPE (`%x00`, `%x1e`) rather than by the bytes themselves: a literal NUL in an argv
# entry is rejected outright by `subprocess`.
_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x00"
_LOG_FORMAT = "--format=%H%x00%B%x1e"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def has_commit(rev: str) -> bool:
    """Whether ``rev`` names a commit object this checkout actually has.

    A shallow clone answers no for anything past its boundary, which is the case the
    fallback range exists for.
    """
    return _git("cat-file", "-e", f"{rev}^{{commit}}").returncode == 0


def is_shallow() -> bool:
    """Whether this checkout is a shallow clone, i.e. its history is truncated.

    Distinct from :func:`has_commit`, and the distinction is what a counting assertion
    needs. Presence of a commit does **not** imply presence of its ancestors: a clone
    deep enough to contain :data:`ENFORCED_FROM` can still be missing most of what
    precedes it, so a count taken there is an undercount rather than a refutation.

    Measured: at `--depth 60` this repository contains ENFORCED_FROM and reports 1,057
    of 9,679 commits reachable, yielding 14 marked commits instead of 17. GitLab's
    `code_checks` runs at the project's own depth and saw 10. Both look exactly like
    "history was rewritten" to an assertion that only checked the commit exists.
    """
    return _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"


def marked_commits(*revisions: str) -> list[tuple[str, str, str]]:
    """``(sha, directive, subject)`` for every commit in ``revisions`` carrying one."""
    result = _git("log", _LOG_FORMAT, *revisions)
    if result.returncode != 0:
        pytest.fail(f"git log {' '.join(revisions)} failed: {result.stderr}")
    found: list[tuple[str, str, str]] = []
    for record in result.stdout.split(_RECORD_SEPARATOR):
        entry = record.strip()
        if not entry:
            continue
        sha, _, message = entry.partition(_FIELD_SEPARATOR)
        match = CI_SUPPRESSION.search(message)
        if match:
            subject = next(iter(message.strip().splitlines()), "")
            found.append((sha, match.group(0), subject[:72]))
    return found


def scan_range() -> tuple[list[str], str]:
    """The revision arguments to scan, and a description of why they are those."""
    if has_commit(ENFORCED_FROM):
        return [f"{ENFORCED_FROM}..HEAD"], f"commits after {ENFORCED_FROM[:12]}"
    return (
        ["-n", str(FALLBACK_WINDOW), "HEAD"],
        f"the {FALLBACK_WINDOW} most recent commits ({ENFORCED_FROM[:12]} is not in "
        "this clone, which is what a shallow CI checkout looks like)",
    )


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_no_commit_in_scope_suppresses_ci() -> None:
    revisions, described = scan_range()
    offenders = marked_commits(*revisions)
    assert not offenders, (
        "these commits carry a directive that suppresses every gate on both CI "
        f"platforms, so the pipelines that should have judged them never ran "
        f"(scanned {described}):\n"
        + "\n".join(
            f"  {sha[:12]} {directive}  {subject}"
            for sha, directive, subject in offenders
        )
        + "\n\nIf the commit is not pushed yet, reword it: `git rebase -i` or, for the "
        "most recent one, `git commit --amend`. If it is already on a shared branch, "
        "its message cannot be rewritten — move ENFORCED_FROM in this file past it and "
        "raise MARKERS_BEFORE_ENFORCED_FROM to match, which records the decision where "
        "the next reader will see it.\n"
        "Directives refused: " + ", ".join(CI_SUPPRESSION_DIRECTIVES)
    )


# --------------------------------------------------------------------------- #
# the ratchets on this gate's own scope
# --------------------------------------------------------------------------- #
def test_the_scanned_range_resolves_and_an_empty_one_is_explained() -> None:
    """The range must be one git understands, and an empty one must have a reason.

    Emptiness is not automatically a defect: a branch sitting exactly on the
    enforcement point has nothing after it to judge, which is the state this file was
    written in. It IS a defect if the range is empty while commits exist after that
    point, because then the gate above passes by looking at nothing. Those two are told
    apart by asking git whether HEAD is behind the start point, rather than by assuming
    either one.

    The detector's own non-vacuity is established separately, by the count test below:
    pointed at history it finds seventeen directives, so a pattern that had stopped
    matching would fail there rather than read as a clean range here.
    """
    revisions, described = scan_range()
    result = _git("rev-list", "--count", *revisions)
    assert result.returncode == 0, (
        f"git does not understand the range this gate scans ({described}): "
        f"{result.stderr}"
    )
    if int(result.stdout.strip()) == 0:
        behind = _git("merge-base", "--is-ancestor", "HEAD", ENFORCED_FROM)
        assert behind.returncode == 0, (
            f"the range this gate scans ({described}) is empty, yet HEAD is not at or "
            f"behind {ENFORCED_FROM[:12]} — so commits exist that this gate is not "
            "looking at, and it is passing by scanning nothing."
        )


def test_the_history_outside_the_scanned_range_still_holds_exactly_the_known_markers() -> (
    None
):
    """Count-pinning on the scope bound, in both directions.

    Moving ``ENFORCED_FROM`` forward is how this gate would be defeated: everything the
    start point passes over stops being looked at, and nothing would say so. Pinning
    the number of directives behind the start point means such a move fails here.

    Needs history before the start point, so it is skipped rather than weakened on a
    shallow clone — the count is measured where the history exists (GitHub's
    ``developer_tests`` clones in full).

    The skip is keyed on :func:`is_shallow`, **not** on whether the start point is
    present. Those come apart: a clone deep enough to contain ``ENFORCED_FROM`` can
    still be missing most of its ancestors, and the resulting undercount is
    indistinguishable from rewritten history. That is how this gate failed GitLab's
    ``code_checks`` while passing GitHub's — 10 markers found against 17 recorded.
    """
    if not has_commit(ENFORCED_FROM):
        pytest.skip(
            f"{ENFORCED_FROM[:12]} is not in this clone; nothing behind it to count"
        )
    if is_shallow():
        # Not a weakening: a shallow clone cannot count what it does not have, and the
        # number it does produce is an undercount that reads as "history was rewritten".
        # GitHub's developer_tests clones with fetch-depth: 0 and enforces this.
        pytest.skip(
            "this is a shallow clone, so the history behind "
            f"{ENFORCED_FROM[:12]} is truncated and any count of it would be an "
            "undercount; the pin is enforced on a full clone"
        )
    behind = marked_commits(ENFORCED_FROM)
    assert len(behind) == MARKERS_BEFORE_ENFORCED_FROM, (
        f"{len(behind)} commits at or before {ENFORCED_FROM[:12]} carry a "
        f"CI-suppressing directive, not the {MARKERS_BEFORE_ENFORCED_FROM} recorded. "
        "More means the start point was moved forward over a commit this gate should "
        "have reported — put it back, or re-measure deliberately and say why. Fewer "
        "means history was rewritten, and the pin needs re-measuring."
    )


def test_the_fallback_window_cannot_reach_a_pre_enforcement_marker() -> None:
    """The shallow-clone fallback must not report a published commit as a new finding.

    The window is a count of commits from HEAD, so this asks how far away the newest
    directive behind the start point is. Measured rather than assumed, because the
    answer changes with every merge — in the safe direction, but a gate that depends on
    that should say so out loud.
    """
    if not has_commit(ENFORCED_FROM):
        pytest.skip(f"{ENFORCED_FROM[:12]} is not in this clone")
    behind = marked_commits(ENFORCED_FROM)
    if not behind:
        pytest.skip(
            "no directives behind the start point, so the window cannot reach one"
        )
    newest = behind[0][0]
    result = _git("rev-list", "--count", f"{newest}..HEAD")
    assert result.returncode == 0, result.stderr
    distance = int(result.stdout.strip())
    assert distance > FALLBACK_WINDOW, (
        f"the newest pre-enforcement directive ({newest[:12]}) is {distance} commits "
        f"behind HEAD, inside the {FALLBACK_WINDOW}-commit fallback window. On a "
        "shallow clone this gate would report a published commit nobody can rewrite. "
        "Shrink FALLBACK_WINDOW."
    )


def test_the_directive_list_and_the_pattern_agree() -> None:
    """The refused spellings are the ones the platforms document.

    This is the universe-closure ratchet on ``CI_SUPPRESSION_DIRECTIVES``, which is
    registered in ``scripts/tests/gate_exemptions.json`` as an enforced universe rather
    than an exemption: the hazard in that constant is a member **missing** from it. The
    list is prose for a reader and the pattern is what runs, so the two can drift — a
    directive added to one and not the other leaves the failure message naming something
    the gate does not catch, or the gate catching something no message explains. Both
    directions are covered, this one with the inverse below.
    """
    assert CI_SUPPRESSION_DIRECTIVES, "nothing would be refused"
    for directive in CI_SUPPRESSION_DIRECTIVES:
        assert CI_SUPPRESSION.search(f"fix: something {directive}"), (
            f"{directive} is named to the reader but the pattern does not match it"
        )


@pytest.mark.parametrize(
    "message",
    [
        "fix: skip the ci step in the docs example",
        "docs: explain [skip] and [ci] as separate tokens",
        "test: assert no_ci_marker() returns None",
        "chore: bump ci-skip-detector to 2.0",
    ],
)
def test_text_that_only_looks_like_a_directive_is_not_matched(message: str) -> None:
    """Paired with the case above: a pattern that matched these would be unusable."""
    assert CI_SUPPRESSION.search(message) is None
