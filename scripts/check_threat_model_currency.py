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

**What this gate can and cannot enforce.** It is a reminder, not a proof. It reads
three files (``VERSION``, ``CHANGELOG.md`` and the model's README) and extracts one
value, so the minimum way to clear it is a one-line edit to that row plus a
regenerate — no re-review required. That is deliberate: there is no mechanical test
for "this prose accurately describes the current architecture", and a gate that
pretended otherwise would just be a worse reminder. Bumping the field without
re-reviewing makes the model *wrong* rather than *stale*, which is worse, so the
failure message says so and names where to start.

What keeps the honest path cheaper than the dishonest one is visibility rather than
enforcement. The reviewed version is carried into the generated Threat Composer
export, which is regenerated and diff-checked by the same ``make lint-cicd`` run, so
a bump-without-review produces a commit whose entire diff is one metadata line in
the README and the matching line in the export — and nothing else. A silent bypass
is possible; an invisible one is not, because "claims to have reviewed a release and
changed no threat entry" is exactly the shape a reviewer notices.

The gate also prints an **advisory** list of corpus documents whose own
``Applies to release`` row is more than ``MAX_RELEASES_BEHIND`` behind ``VERSION``.
That list never affects the exit code. The model deliberately does not claim uniform
freshness — a blanket version bump would assert reviews that did not happen — so
per-document staleness is reported to make that policy auditable, not enforced.

Exit codes:
    0 — the threat model is current (at most one release behind)
    1 — it is too far behind, or the recorded version cannot be interpreted, or the
        reviewed release has no ``## [X.Y.Z]`` heading in ``CHANGELOG.md``
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

#: Each corpus document's own currency marker, reported advisory-only.
_APPLIES_RE = re.compile(
    r"^\|\s*\*\*Applies to release\*\*\s*\|\s*(?P<value>[^|]+?)\s*\|", re.MULTILINE
)


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


class ReviewedVersionNotReleased(ValueError):
    """The reviewed version has no ``## [X.Y.Z]`` heading in ``CHANGELOG.md``.

    A distinct condition from "the model is stale", and with a distinct remedy:
    the CHANGELOG is missing a release heading, or the README row names a version
    that never shipped. Raised only when the reviewed version is *older* than the
    newest heading, because a version newer than every heading is the ordinary
    state of an in-progress release cycle rather than an error — see
    ``releases_behind``.
    """


def _release_key(version: str) -> tuple[int, ...]:
    """Sort key for an ``X.Y.Z`` release identity."""
    return tuple(int(part) for part in version.split("."))


def releases_behind(reviewed: str, current: str, releases: list[str]) -> int:
    """How many releases the reviewed version is behind the current one.

    ``current`` is normally the in-development version and so absent from
    ``releases``; it is treated as sitting one place past the newest release.
    A reviewed version equal to ``current`` is zero behind, and negative
    distances are clamped to 0 — a model reviewed against something newer than
    ``VERSION`` is not stale.

    A reviewed version that is *newer than every heading* in ``CHANGELOG.md`` is
    measured, not rejected. Two ordinary release-commit orderings produce exactly
    that state and neither is a problem with the threat model: ``VERSION`` being
    bumped to the next ``.devN`` before the previous release's ``## [X.Y.Z]``
    heading lands, and a skipped release whose heading is never added at all.
    Both are handled by measuring on a timeline that includes both endpoints, so
    the distance stays honest even when the CHANGELOG has a gap.

    A reviewed version *older* than the newest heading but absent from it is a
    different matter — a missing heading or a typo — and raises
    :class:`ReviewedVersionNotReleased`.
    """
    if reviewed == current:
        return 0

    index = {version: position for position, version in enumerate(releases)}

    if reviewed in index:
        current_position = index.get(current, len(releases))
        return max(0, current_position - index[reviewed])

    newest = releases[-1] if releases else None
    if newest is None or _release_key(reviewed) > _release_key(newest):
        timeline = sorted({*releases, reviewed, current}, key=_release_key)
        return max(0, timeline.index(current) - timeline.index(reviewed))

    raise ReviewedVersionNotReleased(
        f"the threat model records {FIELD_LABEL}: {reviewed}, but CHANGELOG.md has "
        f"no '## [{reviewed}]' heading and {reviewed} is older than its newest "
        f"release ({newest}). VERSION is {current}. This is a release-notes gap, "
        f"not a stale threat model, and the remedy is one of two edits: add the "
        f"'## [{reviewed}]' release heading to CHANGELOG.md if that release "
        f"shipped, or correct the version in the README row if it names a release "
        f"that never did."
    )


def stale_documents(
    current: str, releases: list[str], limit: int = MAX_RELEASES_BEHIND
) -> list[tuple[str, str, int]]:
    """Corpus documents whose ``Applies to release`` row is more than ``limit`` behind.

    Advisory only — the caller must not let this affect the exit code. A document
    without the row, or with one this module cannot interpret, is skipped rather
    than reported: this is a visibility aid, not a second gate.
    """
    stale: list[tuple[str, str, int]] = []
    for path in sorted(THREAT_MODEL_README.parent.rglob("*.md")):
        match = _APPLIES_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            continue
        try:
            applies = normalize(match.group("value"))
            behind = releases_behind(applies, current, releases)
        except ValueError:
            continue
        if behind > limit:
            stale.append((str(path.relative_to(REPO_ROOT)), applies, behind))
    return stale


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
    except ReviewedVersionNotReleased as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stale = stale_documents(current, releases)
    if stale:
        print(
            f"note: {len(stale)} threat-model document(s) are more than "
            f"{MAX_RELEASES_BEHIND} release(s) behind VERSION {current} — advisory "
            f"only, not a failure. The corpus does not claim uniform freshness; see "
            f'"What \'reviewed\' covers" in '
            f"{THREAT_MODEL_README.relative_to(REPO_ROOT)}."
        )
        for document, applies, distance in stale:
            print(
                f"        {document} — last verified against {applies}, "
                f"{distance} behind"
            )

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
