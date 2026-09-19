# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep ``docs/testing.md`` honest about the test tiers this repo has.

``docs/testing.md`` is the published map of every test layer — what it proves, how
to run it, whether CI runs it. A map is worth having only while it is complete, and
the way it goes stale is silent: someone adds a ``stacktest-*`` variant, or renames a
target, and the page keeps describing the repo as it was. The same class of gap as
the CI parity one (``test_ci_gate_parity.py``): nothing noticed for months because
nothing checked.

So this asserts, against the Makefiles, ``scripts/run_all_tests.py`` and the docs
tree rather than against a copy of the facts:

* every live-tier target (``stacktest-*``, ``transform-deploy-test-*``) is described
  on the page — the families that have grown before;
* the named entry points of every layer are on the page;
* every ``make <target>`` the page mentions still exists;
* every skill and doc the page links to still exists;
* the page is reachable — sidebar plus the docs index;
* every directory on disk holding a ``test_*.py`` is registered in
  ``scripts/run_all_tests.py``, and every suite that registry deliberately
  *excludes* is named on the page — in both directions;
* no document that points a reader at the page describes it as mapping every test
  *method*, because nothing here checks that and the page is not built for it.

**Granularity, and why it is not per test method.** ``CLAUDE.md`` and the page itself
used to claim that *every test method* was mapped, and this file never checked
anything of the sort (issue #986). Measured at the time that claim was corrected, it
was off by three orders of magnitude: 527 Python test modules and 88 Vitest spec
files, against 5 test modules and 2 individual test functions named anywhere on the
page. A function count is deliberately not quoted here: four defensible definitions
of "a test function" give four different numbers spanning about fifty, and ``pytest``
reports more again because parametrisation expands them, so the module count is the
figure that survives being restated. A guard demanding a page entry per test function
would be unmaintainable and would be satisfied by a wall of generated rows nobody
reads, and the page is keyed by ``make`` target by design — it is a map of how to run
things and what each tier proves, so a method added inside a suite that already runs
genuinely needs no row. The claim was therefore narrowed to tiers and entry points,
which is what the first five checks above have always enforced, and the last check
keeps the narrowed wording from reverting in any of the documents that repeat it.

The two registry checks are what keep the narrowed claim from being merely weaker. The
page's layer-1 promise is that ``make test`` runs *everything*, so the honest edge of
that promise is the set of suites it does not run. Both sides are derived:
``run_all_tests.discover_test_roots`` walks the filesystem and ``classify`` compares
it against that script's own two registries, and the excluded set is read from
``run_all_tests.QUARANTINE``. Nothing here restates a path. Re-deriving
``classify`` here is deliberate rather than redundant: that check backs ``make test``,
which runs in **neither** CI (both run ``make test-cicd -C lib/idp_common_pkg`` and
``make test-packages-cicd``), while ``pytest scripts/tests`` does run in both — so
until now a test directory in a brand-new location failed only on a developer's
machine, and only if that developer happened to run ``make test``.

Scope note, so the boundary is not mistaken for coverage: a new test module added
*inside* an already-registered directory is not caught here, by design. Whether every
registered root is actually *run by CI* is a different question, guarded for
``src/lambda`` by ``test_src_lambda_tests_in_ci.py`` and tracked for the rest by
issue #980.
"""

from __future__ import annotations

import importlib.util
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

# Targets the page cites that live in a package Makefile, not the root one. They are
# still checked to exist — in lib/idp_common_pkg/Makefile — rather than waved through.
PACKAGE_TARGETS = {"test-unit", "test-cicd", "test-integration"}

# The page section that discloses the suites `make test` does not run. Located by
# heading so the table can be found without hardcoding a line number; renaming the
# heading fails the two tests below with that as the message, which is the intent —
# the disclosure is part of the page's contract, not incidental formatting.
EXCLUDED_SUITES_HEADING = "### Suites `make test` does not run"

# The wording that made the page claim more than any guard checks. Four documents
# carried it when #986 was filed — ``CLAUDE.md``, ``CONTRIBUTING.md``,
# ``docs/README.md`` and ``docs/threat-model.md`` — and three were found only by
# grepping for the phrase afterwards, which is why the file set below is derived
# rather than listed. A paraphrase will get past this; the literal regression will
# not, and the literal regression is what happened.
OVERCLAIM_RE = re.compile(
    r"every test method|all test methods|each test method|every test function",
    re.IGNORECASE,
)

# ``CHANGELOG.md`` is a historical record: the entry describing the claim that was
# corrected has to quote it. That is the only reason for an exemption here, so it is
# named with the reason rather than matched by a pattern that could grow to cover
# documents making the claim in the present tense.
OVERCLAIM_EXEMPT = {"CHANGELOG.md"}

TARGET_RE = re.compile(r"^([a-zA-Z0-9_.-]+):", re.MULTILINE)
MAKE_CALL_RE = re.compile(r"`?make ([a-z][a-z0-9-]*)")
SKILL_LINK_RE = re.compile(r"blob/develop/\.claude/skills/([a-z0-9-]+\.md)")
SECTION_END_RE = re.compile(r"^#{2,3} ", re.MULTILINE)


def _targets(makefile: Path) -> set[str]:
    return set(TARGET_RE.findall(makefile.read_text(encoding="utf-8")))


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _run_all_tests_module():
    """Import ``scripts/run_all_tests.py`` by path (it is a script, not a package).

    Same loader as ``test_src_lambda_tests_in_ci.py``. Importing it is what makes the
    two registry checks below derive their facts instead of restating them.
    """
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests_for_doc_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _markdown_referring_to_the_page() -> list[tuple[str, str]]:
    """Every Markdown document that points a reader at the page, plus the page itself.

    Derived by walking the tree, so a new document that links the page is covered the
    day it lands. Build output, dependencies and worktree checkouts are pruned with
    ``run_all_tests.PRUNE_DIR_MARKERS``, which already carries that list for the test
    walk — one place to maintain rather than two.
    """
    prune = _run_all_tests_module().PRUNE_DIR_MARKERS
    found: list[tuple[str, str]] = []
    for path in sorted(REPO_ROOT.rglob("*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(marker in "/" + rel for marker in prune) or rel in OVERCLAIM_EXEMPT:
            continue
        # rglob yields broken symlinks, and this tree grows them: the docs-site
        # build populates docs-site/src/content/docs/ with gitignored symlinks
        # into docs/, and renaming or deleting a source page leaves one dangling.
        # Reading it raises FileNotFoundError, which turned this check into a
        # bare traceback that fired only on a developer machine -- never in CI,
        # because CI does not build the site into that directory. The worst
        # possible distribution for a diagnostic with no guidance in it.
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel == DOC.relative_to(REPO_ROOT).as_posix() or "testing.md" in text:
            found.append((rel, text))
    return found


def _documented_exclusions() -> set[str]:
    """The repo-relative paths named in the page's excluded-suites table.

    Only the first column is read. The written reason beside it is prose for a reader
    and is deliberately not diffed against the registry's one-line reason: requiring
    the two to match character-for-character would make the page a second copy of the
    registry rather than an explanation of it.
    """
    text = _doc_text()
    assert EXCLUDED_SUITES_HEADING in text, (
        f"{DOC.relative_to(REPO_ROOT)} must carry a {EXCLUDED_SUITES_HEADING!r} "
        "section naming the suites scripts/run_all_tests.py excludes from `make test`"
    )
    section = text.split(EXCLUDED_SUITES_HEADING, 1)[1]
    end = SECTION_END_RE.search(section)
    if end:
        section = section[: end.start()]
    paths: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        if cell.startswith("`") and cell.endswith("`") and len(cell) > 2:
            paths.add(cell.strip("`"))
    return paths


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
def test_every_test_directory_is_registered_for_discovery() -> None:
    """The page's layer-1 promise: no suite sits outside the map.

    ``classify`` raises ``SystemExit`` naming each directory that holds a
    ``test_*.py`` and appears in neither registry. That is the same check
    ``make test`` performs, re-run here because neither CI runs ``make test`` and
    both run ``pytest scripts/tests``.
    """
    module = _run_all_tests_module()
    try:
        module.classify(module.discover_test_roots())
    except SystemExit as exc:  # pragma: no cover - only on the defect
        pytest.fail(
            "a test directory is registered in neither RUN_ROOTS nor QUARANTINE in "
            f"scripts/run_all_tests.py, so `make test` will not run it and "
            f"{DOC.relative_to(REPO_ROOT)} does not describe it:\n{exc}"
        )


@pytest.mark.unit
def test_suites_excluded_from_make_test_are_named_on_the_page() -> None:
    """A suite that exists and never runs must not be invisible to a reader."""
    module = _run_all_tests_module()
    undocumented = sorted(set(module.QUARANTINE) - _documented_exclusions())
    assert not undocumented, (
        f"{undocumented} are excluded from `make test` by "
        "scripts/run_all_tests.py's QUARANTINE registry but are absent from the "
        f"{EXCLUDED_SUITES_HEADING!r} table in {DOC.relative_to(REPO_ROOT)}. Add a "
        "row with the reason, or run the suite instead."
    )


@pytest.mark.unit
def test_the_page_names_no_exclusion_that_no_longer_exists() -> None:
    """The reverse direction: a row for a suite that now runs misleads a reader."""
    module = _run_all_tests_module()
    stale = sorted(_documented_exclusions() - set(module.QUARANTINE))
    assert not stale, (
        f"{DOC.relative_to(REPO_ROOT)} lists {stale} as excluded from `make test`, "
        "but scripts/run_all_tests.py no longer quarantines them. Remove the rows."
    )


@pytest.mark.unit
def test_no_document_claims_the_page_maps_every_test_method() -> None:
    """The claim this file is named for: it must not come back anywhere.

    The page maps tiers and ``make`` entry points. Saying it maps every test *method*
    tells a contributor that adding tests without touching it will be caught, and
    nothing catches that — which is the whole of #986.
    """
    offenders: list[str] = []
    for rel, text in _markdown_referring_to_the_page():
        for match in OVERCLAIM_RE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}: {match.group(0)!r}")
    assert not offenders, (
        "these documents claim docs/testing.md maps individual test methods, which no "
        "guard enforces and which the page is not designed to do:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\nSay 'every test layer and tier' instead, or add the per-method check "
        "that would make the stronger claim true."
    )


@pytest.mark.unit
def test_the_page_is_reachable() -> None:
    """An unlinked page is one nobody finds, which is the same as not writing it."""
    assert '{ label: "Testing", slug: "testing" }' in SIDEBAR.read_text(
        encoding="utf-8"
    ), "add docs/testing.md to the Evaluation & Testing sidebar group"
    assert "(./testing.md)" in DOCS_INDEX.read_text(encoding="utf-8"), (
        "add docs/testing.md to the docs/README.md index"
    )
