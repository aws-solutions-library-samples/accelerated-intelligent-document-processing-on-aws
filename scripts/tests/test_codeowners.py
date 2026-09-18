# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline gate on ``.github/CODEOWNERS`` and the governance documents.

Nothing in ``scripts/``, the ``Makefile`` or ``.github/workflows/`` referenced
``CODEOWNERS``, ``GOVERNANCE.md``, ``ROADMAP.md`` or ``SECURITY.md`` before this
file existed, so two things could rot silently:

1. **A CODEOWNERS pattern that matches nothing.** This repository has already
   deleted ``patterns/pattern-1/`` through ``patterns/pattern-3/``; a rule naming
   a path that no longer exists keeps sitting in the file, matches nothing, and
   routes nothing, with no warning from GitHub (its own validator checks *owners*,
   not whether a *pattern* still resolves).
2. **A broken relative link in the governance documents.** The docs-site build
   cannot cover these: ``docs-site/setup.sh`` symlinks content only from ``docs/``
   and ``images/``, so the root-level governance files are not in the Starlight
   content collection at all and ``make docs-build`` never reads them.

Both were verified by hand when the files were added, and a one-off manual
verification is exactly what this file exists to replace.

A third check lived here until ``MAINTAINERS.md`` was removed: it asserted that
the handles in CODEOWNERS and that page named the same people. Deleting the prose
roster removed the divergence it policed — CODEOWNERS is now the only record of
who reviews what, so there is no second copy to disagree with it.

**Everything is derived, not enumerated.** The rules, their count, the handles and
the document set are all read out of the files, so a rule, a handle or a new
governance page added later is covered without touching this test.

Deliberately *not* covered here: whether each named owner holds **write access**,
without which GitHub silently ignores their CODEOWNERS entry. That needs an
authenticated token, and GitHub's own
``GET /repos/{owner}/{repo}/codeowners/errors`` is the authoritative answer, so it
belongs in a maintainer-run check rather than in a gate that would red-line pull
requests from forks for a condition no pull request can fix.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = Path(".github/CODEOWNERS")

# Root-level Markdown files reached by the closure below but deliberately left
# out of the link check, each with the reason. Both are asserted to still exist
# and to still be reachable, so an exclusion cannot go stale unnoticed.
LINK_CHECK_EXCLUDED = {
    "CHANGELOG.md": (
        "append-only historical record: released entries link to docs that were "
        "later renamed or removed, and repointing a frozen release note at a "
        "different file would misrepresent what shipped. Three such links are "
        "broken today, identically on develop."
    ),
    "CONTRIBUTING.md": (
        "predates the governance file set and is being rewritten separately; its "
        "two root-absolute /.github/ISSUE_TEMPLATE/ links are a pre-existing "
        "defect that belongs with that rewrite, not here."
    ),
}

# Owner forms this gate understands. A team (@org/team) or a bare email address is
# valid CODEOWNERS syntax but would need different handling, so encountering one is
# a failure rather than a silent skip.
_HANDLE = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
OWNER_TOKEN = re.compile(rf"^@{_HANDLE}$")

MD_LINK = re.compile(r"\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//)", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
GLOB_UNSUPPORTED = "?[]!\\"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _tracked_paths() -> frozenset[str]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(p for p in out.split("\0") if p)


def _read(rel: Path | str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _strip_code(text: str) -> str:
    """Drop fenced blocks and inline code spans.

    Both governance files quote shell commands containing ``@`` and ``[...]``, so
    scanning them for @mentions or links without this invents handles.
    """
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        kept.append("" if inside else re.sub(r"`[^`]*`", "", line))
    return "\n".join(kept)


def _codeowners_rules() -> list[tuple[int, str, list[str]]]:
    """Every rule as ``(line number, pattern, owners)``, read from the file."""
    rules: list[tuple[int, str, list[str]]] = []
    for lineno, raw in enumerate(_read(CODEOWNERS).splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append((lineno, pattern, owners))
    return rules


def _matcher(pattern: str) -> re.Pattern[str]:
    """Translate a CODEOWNERS pattern to a regex over repo-relative paths.

    Unsupported glob syntax fails loudly rather than quietly matching nothing,
    which would turn this gate into a rubber stamp.
    """
    bad = sorted({ch for ch in GLOB_UNSUPPORTED if ch in pattern})
    if bad:
        pytest.fail(
            f"{CODEOWNERS}: pattern {pattern!r} uses glob syntax {bad} that this "
            "gate does not implement. Extend _matcher() rather than leaving the "
            "pattern unchecked."
        )
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    dir_only = body.endswith("/")
    body = body.rstrip("/")

    core, i = "", 0
    while i < len(body):
        if body.startswith("**", i):
            core, i = core + ".*", i + 2
        elif body[i] == "*":
            core, i = core + "[^/]*", i + 1
        else:
            core, i = core + re.escape(body[i]), i + 1

    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if dir_only else "(?:/.*)?"
    return re.compile(f"^{prefix}{core}{suffix}$")


def _slug(heading: str) -> str:
    """GitHub's heading-anchor slug: strip markup, lowercase, spaces to hyphens.

    Each space becomes its own hyphen, so "A & B" -> "a--b". Collapsing runs of
    whitespace here silently reports working anchors as broken.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").replace("*", "").replace("_", "")
    text = re.sub(r"<[^>]+>", "", text).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return text.replace(" ", "-").replace("\t", "-")


def _anchors(rel: Path) -> set[str]:
    return {
        _slug(m.group(2))
        for line in _strip_code(_read(rel)).splitlines()
        if (m := HEADING.match(line))
    }


def _relative_links(rel: Path) -> list[str]:
    found = []
    for raw in MD_LINK.findall(_strip_code(_read(rel))):
        target = raw.strip("<>").strip()
        if target and not EXTERNAL.match(target):
            found.append(target)
    return found


def _root_markdown_mentions(text: str) -> set[str]:
    """Root-level ``*.md`` filenames named in prose (CODEOWNERS has no links)."""
    return {
        name
        for name in re.findall(r"\b([A-Z][A-Za-z0-9_.-]*\.md)\b", text)
        if (REPO_ROOT / name).is_file()
    }


def _governance_docs() -> list[Path]:
    """Derive the document set from CODEOWNERS outward.

    Seed: the root-level Markdown files ``.github/CODEOWNERS`` names in its own
    header. Then follow relative links between root-level Markdown files to
    closure. A new root-level governance page linked from one of these is picked
    up automatically; nothing about the set is hardcoded here.
    """
    seen = {CODEOWNERS}
    queue = [Path(n) for n in sorted(_root_markdown_mentions(_read(CODEOWNERS)))]
    seen.update(queue)
    while queue:
        current = queue.pop()
        for target in _relative_links(current):
            path_part = target.partition("#")[0]
            if not path_part:
                continue
            dest = Path(_normalise(current, path_part) or "")
            if (
                dest.suffix == ".md"
                and dest.parent == Path(".")
                and dest not in seen
                and (REPO_ROOT / dest).is_file()
            ):
                seen.add(dest)
                queue.append(dest)
    return sorted(seen - {Path(n) for n in LINK_CHECK_EXCLUDED})


def _normalise(source: Path, path_part: str) -> str | None:
    """Resolve a link target to a repo-relative path, or None if it escapes."""
    base = REPO_ROOT if path_part.startswith("/") else (REPO_ROOT / source).parent
    resolved = (base / path_part.lstrip("/")).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 1. every pattern still owns something
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_every_codeowners_pattern_matches_a_tracked_path() -> None:
    """A rule that owns nothing routes nothing, and GitHub will not tell you."""
    tracked = _tracked_paths()
    rules = _codeowners_rules()
    assert len(rules) >= 2, (
        f"parsed only {len(rules)} rules from {CODEOWNERS}; the parser is broken, "
        "not the file"
    )

    dead = [
        f"line {lineno}: {pattern}"
        for lineno, pattern, _ in rules
        if not any(_matcher(pattern).match(path) for path in tracked)
    ]
    assert not dead, (
        f"{CODEOWNERS} patterns match no tracked file — they route nothing:\n  "
        + "\n  ".join(dead)
        + "\nA directory rename or deletion leaves the rule behind silently. "
        "Update or remove the rule."
    )


@pytest.mark.unit
def test_every_codeowners_rule_names_a_routable_owner() -> None:
    """An owner GitHub cannot resolve routes nothing, exactly like a dead pattern."""
    handles: set[str] = set()
    for lineno, pattern, owners in _codeowners_rules():
        assert owners, f"{CODEOWNERS} line {lineno}: {pattern!r} names no owner"
        for owner in owners:
            assert OWNER_TOKEN.match(owner), (
                f"{CODEOWNERS} line {lineno}: owner {owner!r} is a team or email "
                "rather than a user handle. Valid CODEOWNERS, but this gate does "
                "not handle that form — extend the gate."
            )
            handles.add(owner[1:].casefold())
    assert handles, "extracted no handles from CODEOWNERS; parser is broken"


# --------------------------------------------------------------------------- #
# 2. every relative link in the governance documents resolves
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_governance_document_set_is_derived_not_empty() -> None:
    """A broken derivation would make the link test below pass vacuously.

    The floor is a vacuity guard, not an inventory: it catches a closure that
    returns nothing or only its own seed. Removing a governance page legitimately
    lowers the count, so lower the floor with it rather than reading a drop as a
    parser failure.
    """
    docs = _governance_docs()
    assert len(docs) >= 4, (
        f"derived only {len(docs)} governance documents ({docs}); the closure in "
        "_governance_docs() is broken, so the link check covers almost nothing"
    )
    assert CODEOWNERS in docs and Path("GOVERNANCE.md") in docs


@pytest.mark.unit
@pytest.mark.parametrize("excluded", sorted(LINK_CHECK_EXCLUDED))
def test_link_check_exclusions_are_not_stale(excluded: str) -> None:
    """An exclusion for a file no longer in scope hides nothing and misleads."""
    assert (REPO_ROOT / excluded).is_file(), (
        f"{excluded} is excluded from the link check but no longer exists; drop "
        "the entry from LINK_CHECK_EXCLUDED"
    )
    seed = {CODEOWNERS, *(Path(n) for n in _root_markdown_mentions(_read(CODEOWNERS)))}
    reachable = {
        _normalise(doc, link.partition("#")[0])
        for doc in set(_governance_docs()) | seed
        for link in _relative_links(doc)
        if link.partition("#")[0]
    }
    assert excluded in reachable, (
        f"{excluded} is excluded from the link check but nothing in the "
        "governance set links to it any more; drop the entry from "
        "LINK_CHECK_EXCLUDED"
    )


@pytest.mark.unit
def test_every_relative_link_in_the_governance_docs_resolves() -> None:
    """In-repo links, in files no docs build reads. External URLs are not fetched.

    Both the path and, for a Markdown target, the ``#anchor`` are checked: a link
    to a real file with a heading that was since retitled is just as broken.
    """
    broken: list[str] = []
    checked = 0
    for doc in _governance_docs():
        for target in _relative_links(doc):
            checked += 1
            path_part, _, anchor = target.partition("#")
            if not path_part:
                dest = doc.as_posix()
            else:
                normalised = _normalise(doc, path_part)
                if normalised is None:
                    broken.append(f"{doc}: {target} -> escapes the repository")
                    continue
                dest = normalised
                if not (REPO_ROOT / dest).exists():
                    broken.append(f"{doc}: {target} -> no such path")
                    continue
            if anchor and dest.endswith(".md"):
                if _slug(anchor) not in _anchors(Path(dest)):
                    broken.append(f"{doc}: {target} -> no such heading in {dest}")

    assert checked >= 20, (
        f"only {checked} relative links found across {_governance_docs()}; the "
        "link extractor is broken (40 at the time of writing; most links in these "
        "files are external URLs, which are deliberately not fetched)"
    )
    assert not broken, "broken relative links:\n  " + "\n  ".join(broken)
