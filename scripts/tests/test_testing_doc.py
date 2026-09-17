# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep ``docs/testing.md`` honest about the test methods this repo has.

``docs/testing.md`` is the published map of every test layer — what it proves, how
to run it, whether CI runs it. A map is worth having only while it is complete, and
the way it goes stale is silent: someone adds a ``stacktest-*`` variant, or renames a
target, and the page keeps describing the repo as it was. The same class of gap as
the CI parity one (``test_ci_gate_parity.py``): nothing noticed for months because
nothing checked.

So this asserts, against the Makefiles and the docs tree rather than against a copy
of the facts:

* every live-tier target (``stacktest-*``, ``transform-deploy-test-*``) is described
  on the page — the families that have grown before;
* the named entry points of every layer are on the page;
* every ``make <target>`` the page mentions still exists;
* every skill and doc the page links to still exists;
* the page is reachable — sidebar plus the docs index.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "testing.md"
MAKEFILE = REPO_ROOT / "Makefile"
COMMON_MAKEFILE = REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile"
SIDEBAR = REPO_ROOT / "docs-site" / "astro.config.mjs"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"

# Live-tier target families. A new member of either must be documented: these are
# the tiers no CI runs, so the page is the only place a reader can learn they exist.
LIVE_TIER_PREFIXES = ("stacktest-", "transform-deploy-test-")

# Helpers and aliases: nothing to describe that the aliased target does not already
# cover. Keep the reason with the name — an unexplained exclusion is how a real gate
# gets waved through later.
LIVE_TIER_EXEMPT = {
    "stacktest-list": "discovery helper: prints the other stacktest targets",
    "transform-deploy-test-list": "discovery helper: prints the transform targets",
    "stacktest-rbac": "alias of api-test, which the page documents",
    "stacktest-benchmark": "alias of benchmark-release, which the page documents",
}

# One entry point per layer on the page. Renaming one of these without touching the
# page is exactly the drift this test exists to catch.
LAYER_ENTRY_POINTS = [
    "make test",
    "make test-list",
    "make lint-cicd",
    "make typecheck-pr",
    "make ui-test",
    "make srt-scan",
    "make dep-audit",
    "make api-test-static",
    "make api-test",
    "make live-auth-checks",
    "make ux-test",
    "make benchmark-release",
    "make security-results",
]

# Targets the page cites that live in a package Makefile, not the root one.
PACKAGE_TARGETS = {"test-unit", "test-cicd"}

TARGET_RE = re.compile(r"^([a-zA-Z0-9_.-]+):", re.MULTILINE)
MAKE_CALL_RE = re.compile(r"`?make ([a-z][a-z0-9-]*)")
SKILL_LINK_RE = re.compile(r"blob/develop/\.claude/skills/([a-z0-9-]+\.md)")


def _targets(makefile: Path) -> set[str]:
    return set(TARGET_RE.findall(makefile.read_text(encoding="utf-8")))


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_page_exists_with_frontmatter_and_licence() -> None:
    text = _doc_text()
    assert text.startswith('---\ntitle: "Testing"\n---'), (
        "docs/*.md needs YAML frontmatter with a title; the docs site keys off it"
    )
    assert "SPDX-License-Identifier: MIT-0" in text.split("# Testing")[0]


@pytest.mark.unit
def test_every_live_tier_target_is_documented() -> None:
    """A deploy variant nobody documented is a tier nobody knows how to run."""
    undocumented = sorted(
        target
        for target in _targets(MAKEFILE)
        if target.startswith(LIVE_TIER_PREFIXES)
        and target not in LIVE_TIER_EXEMPT
        and f"make {target}" not in _doc_text()
    )
    assert not undocumented, (
        f"{undocumented} run in no CI and are absent from {DOC.relative_to(REPO_ROOT)}. "
        "Add a row to the live-stack tier table, or exempt it in LIVE_TIER_EXEMPT "
        "with the reason."
    )


@pytest.mark.unit
def test_exemptions_still_exist_as_targets() -> None:
    """A stale exemption silently un-guards a target that was renamed into it."""
    targets = _targets(MAKEFILE)
    assert not sorted(set(LIVE_TIER_EXEMPT) - targets), (
        f"LIVE_TIER_EXEMPT names targets the Makefile no longer has: "
        f"{sorted(set(LIVE_TIER_EXEMPT) - targets)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("entry_point", LAYER_ENTRY_POINTS)
def test_layer_entry_point_is_on_the_page(entry_point: str) -> None:
    assert entry_point in _doc_text(), (
        f"{entry_point!r} is a layer's entry point and must appear in "
        f"{DOC.relative_to(REPO_ROOT)}"
    )


@pytest.mark.unit
def test_every_make_target_the_page_cites_exists() -> None:
    """Catches a rename that updated the Makefile and not the docs."""
    root = _targets(MAKEFILE)
    package = _targets(COMMON_MAKEFILE)
    missing = sorted(
        {
            name
            for name in MAKE_CALL_RE.findall(_doc_text())
            if name not in root and not (name in PACKAGE_TARGETS and name in package)
        }
    )
    assert not missing, (
        f"{DOC.relative_to(REPO_ROOT)} cites unknown make targets: {missing}"
    )


@pytest.mark.unit
def test_linked_skills_and_docs_resolve() -> None:
    text = _doc_text()
    missing_skills = sorted(
        name
        for name in set(SKILL_LINK_RE.findall(text))
        if not (REPO_ROOT / ".claude" / "skills" / name).exists()
    )
    assert not missing_skills, f"linked skills do not exist: {missing_skills}"

    missing_docs = sorted(
        target
        for target in set(re.findall(r"\]\((\./[^)#]+)", text))
        if not (DOC.parent / target).exists()
    )
    assert not missing_docs, f"relative doc links do not resolve: {missing_docs}"


@pytest.mark.unit
def test_the_page_is_reachable() -> None:
    """An unlinked page is one nobody finds, which is the same as not writing it."""
    assert '{ label: "Testing", slug: "testing" }' in SIDEBAR.read_text(
        encoding="utf-8"
    ), "add docs/testing.md to the Evaluation & Testing sidebar group"
    assert "(./testing.md)" in DOCS_INDEX.read_text(encoding="utf-8"), (
        "add docs/testing.md to the docs/README.md index"
    )
