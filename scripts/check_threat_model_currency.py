#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail the build when the threat model has not been re-reviewed recently enough.

``security/threat-modeling/`` is a real STRIDE model — 90-odd threats, a scored
risk register, a per-feature document per surface. It is also, by construction,
a description of an architecture at a point in time, and this architecture moves
every release. When it drifts it does not merely go quiet: it makes confident
present-tense claims about controls that no longer exist. Before this gate it had
reached six releases behind, still describing the AWS AppSync API layer that the
REST migration removed, which is the one thing a threat model exists to get right.

So the model records the release it was last reviewed against, in a
machine-readable row of its README:

    | **Last reviewed against version** | 0.6.9 |

and this script compares that against ``VERSION``, counting distance in
*releases* rather than in version arithmetic. The repo's release cadence
increments the third component (0.6.7 -> 0.6.8 -> 0.6.9), and each of those
carries features, so "one minor version" in the ordinary sense would never
trip; ``CHANGELOG.md``'s ordered list of shipped releases is the authoritative
sequence and is what the distance is measured on.

**The threshold is one release.** Zero would red-line ``develop`` the moment
``VERSION`` is bumped to the next ``.devN``, before there is anything to review —
the bump is the first commit of a cycle, not the last. Two or more is how it got
to six: each individual slip looks reasonable. One release means the re-review is
due during the cycle that follows the release it describes, and the branch only
goes red if a whole release ships without it.

This is deliberately a gate that can only be cleared by doing the work. Bumping
the field without re-reviewing makes the model *wrong* rather than *stale*, which
is worse, so the failure message says so and names where to start.

Exit codes:
    0 — the threat model is current (at most one release behind)
    1 — it is too far behind, or the recorded version cannot be interpreted
    2 — a required file is missing or unparseable
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
THREAT_MODEL_README = REPO_ROOT / "security" / "threat-modeling" / "README.md"

#: The machine-readable field. Kept as one exact row so a reader editing the
#: table cannot half-rename it: the parse fails loudly instead of silently
#: reading a stale value from somewhere else.
FIELD_LABEL = "Last reviewed against version"

#: Releases of slack. See the module docstring for why this is 1 and not 0 or 2.
MAX_RELEASES_BEHIND = 1

_FIELD_RE = re.compile(
    r"^\|\s*\*\*" + re.escape(FIELD_LABEL) + r"\*\*\s*\|\s*(?P<value>[^|]+?)\s*\|\s*$",
    re.MULTILINE,
)
_RELEASE_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def normalize(version: str) -> str:
    """Reduce a version string to its ``X.Y.Z`` release identity.

    ``v0.6.9``, ``0.6.9.dev3`` and ``0.6.9`` are all the same release for this
    purpose: a pre-release of 0.6.9 is reviewed against 0.6.9's architecture.
    """
    stripped = version.strip().lstrip("vV").strip()
    match = re.match(r"^(\d+\.\d+\.\d+)", stripped)
    if not match:
        raise ValueError(f"not an X.Y.Z version: {version!r}")
    return match.group(1)


def released_versions(changelog_text: str) -> list[str]:
    """Shipped releases in order, oldest first.

    ``CHANGELOG.md`` lists them newest first under ``## [X.Y.Z]`` headings, and
    ``## [Unreleased]`` is skipped by the pattern because it is not a version.
    """
    return list(reversed(_RELEASE_HEADING_RE.findall(changelog_text)))


def releases_behind(reviewed: str, current: str, releases: list[str]) -> int:
    """How many releases the reviewed version is behind the current one.

    ``current`` is normally the in-development version and so absent from
    ``releases``; it is treated as sitting one place past the newest release.
    A reviewed version equal to ``current``, or newer than every release, is
    zero behind. Negative distances are clamped to 0 — a model reviewed against
    something newer than ``VERSION`` is not stale.
    """
    if reviewed == current:
        return 0

    index = {version: position for position, version in enumerate(releases)}
    current_position = index.get(current, len(releases))

    if reviewed not in index:
        raise ValueError(
            f"{reviewed!r} is neither a release in CHANGELOG.md nor the current "
            f"VERSION ({current})"
        )

    return max(0, current_position - index[reviewed])


def read_reviewed_version(readme_text: str) -> str:
    match = _FIELD_RE.search(readme_text)
    if not match:
        raise ValueError(
            f"no '| **{FIELD_LABEL}** | <version> |' row in "
            f"{THREAT_MODEL_README.relative_to(REPO_ROOT)}. This gate reads that "
            f"row as the model's currency marker; restore it rather than removing "
            f"the check."
        )
    return normalize(match.group("value"))


def _failure_message(reviewed: str, current: str, behind: int, skipped: list[str]) -> str:
    readme = THREAT_MODEL_README.relative_to(REPO_ROOT)
    return f"""\
Threat model currency gate FAILED

  {readme} records
      {FIELD_LABEL}: {reviewed}
  and VERSION is {current} — {behind} releases later.

  Releases shipped since the last review: {", ".join(skipped) or "(none)"}
  The gate allows at most {MAX_RELEASES_BEHIND}.

  Do NOT clear this by editing the version field alone. A model that claims to
  describe {current} while describing {reviewed}'s architecture is worse than one
  that admits it is stale: readers act on the controls it names. Instead:

    1. Re-review the model against the code. Start with
         security/threat-modeling/architecture/system-overview.md
       sections 4 (trust boundaries) and 5 (authorization model), then the
       per-feature document for any surface the releases above touched.
       Those releases' CHANGELOG.md entries are the shortest list of what changed.

    2. Record the outcome: correct the threat entries, update each document's
       "Applies to release" row, and add a Version History entry to {readme}.

    3. Set "{FIELD_LABEL}" to {current}, then regenerate the export:
         python3 security/threat-modeling/scripts/build_threat_model.py

    4. Re-run: make check-threat-model-currency
"""


def main() -> int:
    for path in (VERSION_FILE, CHANGELOG, THREAT_MODEL_README):
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 2

    try:
        current = normalize(VERSION_FILE.read_text(encoding="utf-8"))
        reviewed = read_reviewed_version(THREAT_MODEL_README.read_text(encoding="utf-8"))
        releases = released_versions(CHANGELOG.read_text(encoding="utf-8"))
        behind = releases_behind(reviewed, current, releases)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if behind > MAX_RELEASES_BEHIND:
        position = releases.index(reviewed) if reviewed in releases else len(releases)
        skipped = [*releases[position + 1 :], current]
        print(_failure_message(reviewed, current, behind, skipped), file=sys.stderr)
        return 1

    print(
        f"threat model is current: reviewed against {reviewed}, VERSION is "
        f"{current} ({behind} release(s) behind, limit {MAX_RELEASES_BEHIND})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
