# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert every path in pyrightconfig.json's `include` array exists on disk.

`include` listed `idp_cli/idp_cli`, a path that has not existed since the CLI
moved to `lib/idp_cli_pkg/idp_cli`. basedpyright prints one line about the
missing directory and then contributes *nothing* for that entry, so it exits 0
over whatever remains. The result was that `lib/idp_cli_pkg/idp_cli/cli.py`
(6,791 lines) and the rest of the CLI package were type-checked by nothing at
all, while `make typecheck` looked like it was passing over the whole repo.

A stale include entry is invisible by construction: the gate gets quieter, not
louder, so nothing draws attention to it. These tests make the typo fail here
instead. `exclude` is checked two ways — a stale exclude silently stops
excluding (the less harmful direction, but still a lie about intent), and an
exclude entry that covers an include entry guts the gate from the other side,
because `exclude` wins.

Coverage of first-party source is **derived** from `lib/*/pyproject.toml`, not
listed in this file. An earlier version listed three paths in the test body,
which meant a newly added distribution was covered by nothing and the test
still passed — the same "control that enumerates the known instances" defect
this whole change set is about. Three distributions really were in that blind
spot (`idp_sdk`, `idp_feature_sdk`, `idp_mcp_connector_pkg`: 64 files, 28,958
lines). All are now in `include`; measured cost was 0 new errors and +18
warnings, so covering them was free.

Coverage of the *rest* of the tree is derived the same way, from `git ls-files`:
`test_every_tracked_python_file_is_type_checked` fails if any tracked `.py` file
falls outside `include` or is cancelled by `exclude`. That is the check the
six-entry `include` list could not have: it reached 432 of 1230 tracked files,
and the 798 it missed included every `lib/*/tests` suite and all of `scripts/`,
`nested/`, `feature-platform/`, `benchmarks/` and `samples/`. A carve-out from it
goes in `TYPECHECK_SCOPE_EXCLUSIONS` with a reason, and is asserted to still
shield something.

Deliberately NOT asserted here: `typeCheckingMode` (currently "basic") or the
13 diagnostic rules pinned to "none". Tightening those is a separate decision
with a much larger diff; this file only guarantees that whatever strictness is
configured is actually applied to the files it claims to cover.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest
from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]
PYRIGHT_CONFIG = REPO_ROOT / "pyrightconfig.json"
LIB_ROOT = REPO_ROOT / "lib"

pytestmark = pytest.mark.unit

# Top-level directory names a distribution may contain that are not shipped,
# importable source. Mirrors IGNORED_DIR_NAMES in test_package_discovery.py.
NON_SOURCE_TOP_LEVELS = {"tests", "test", "examples", "build", "dist", "docs"}

# Distributions deliberately NOT under a pyrightconfig `include` entry.
#
# This must stay EMPTY unless there is a reason worth writing down. The whole
# point of deriving the inventory below is that a newly added distribution is
# covered by default and dropping one is a visible decision, not an omission.
# Format: distribution directory name -> reason (and an issue reference).
#
# All five first-party distributions are covered as of this commit. The three
# that were previously outside `include` (idp_sdk, idp_feature_sdk,
# idp_mcp_connector_pkg — 64 files, 28,958 lines) were added after measuring
# that they contribute 0 basedpyright errors, so covering them cost nothing.
COVERAGE_EXEMPTIONS: dict[str, str] = {}


def _config() -> dict:
    return json.loads(PYRIGHT_CONFIG.read_text())


def _include_paths() -> list[str]:
    return list(_config().get("include", []))


def _pyprojects() -> list[Path]:
    """The source of truth for "what first-party distributions exist".

    Same glob `test_package_discovery.py` uses, deliberately: a control that
    enumerates the known instances in its own body stops being a control the
    moment a sixth distribution is added. This one derives the inventory, so a
    new `lib/<something>/pyproject.toml` is in scope automatically.
    """
    return sorted(LIB_ROOT.glob("*/pyproject.toml"))


def _importable_source_dirs(pyproject: Path) -> list[str]:
    """Repo-relative paths of the importable package dirs a distribution ships.

    Resolved from the distribution's own setuptools configuration rather than
    assumed from the directory name — they differ in practice
    (`lib/idp_mcp_connector_pkg` ships `idp_mcp_connector`, `lib/idp_cli_pkg`
    ships `idp_cli`).
    """
    cfg = tomllib.loads(pyproject.read_text())
    setuptools_cfg = cfg.get("tool", {}).get("setuptools", {})
    packages = setuptools_cfg.get("packages")
    pkg_dir = pyproject.parent

    tops: set[tuple[str, str]] = set()  # (where, top-level package name)
    if isinstance(packages, list):
        # Explicit list form: `packages = ["idp_cli"]`.
        tops = {(".", name.split(".")[0]) for name in packages}
    elif isinstance(packages, dict) and "find" in packages:
        spec = packages["find"]
        for root in spec.get("where", ["."]):
            found = find_packages(
                where=str(pkg_dir / root),
                include=spec.get("include", ["*"]),
                exclude=spec.get("exclude", []),
            )
            tops |= {(root, name.split(".")[0]) for name in found}

    out: list[str] = []
    for where, top in sorted(tops):
        if top in NON_SOURCE_TOP_LEVELS:
            continue
        target = (pkg_dir / where / top).resolve()
        if not target.is_dir():
            continue
        out.append(str(target.relative_to(REPO_ROOT)))
    return out


def test_pyright_config_exists() -> None:
    assert PYRIGHT_CONFIG.is_file(), f"missing {PYRIGHT_CONFIG}"


def test_include_is_non_empty() -> None:
    """An empty `include` would make basedpyright fall back to scanning the
    whole tree, which is not what this config intends."""
    assert _include_paths(), "pyrightconfig.json declares no `include` paths"


@pytest.mark.parametrize("rel", _include_paths())
def test_every_include_path_exists(rel: str) -> None:
    """A non-existent include entry silently removes files from the gate."""
    target = REPO_ROOT / rel
    assert target.exists(), (
        f"pyrightconfig.json `include` lists {rel!r}, which does not exist. "
        "basedpyright contributes nothing for a missing include entry, so this "
        "typo would silently drop those files from `make typecheck`. Update the "
        "path (or remove the entry if the code is gone)."
    )


@pytest.mark.parametrize("rel", _include_paths())
def test_every_include_path_contains_python(rel: str) -> None:
    """Existing but Python-free is the same failure wearing a disguise: the
    entry contributes no files, so it is not actually covering anything."""
    target = REPO_ROOT / rel
    if not target.exists():
        pytest.skip("covered by test_every_include_path_exists")
    if target.is_file():
        assert target.suffix == ".py", f"include entry {rel!r} is not a .py file"
        return
    assert next(target.rglob("*.py"), None) is not None, (
        f"pyrightconfig.json `include` lists {rel!r}, which exists but holds no "
        "*.py files, so it adds nothing to the typecheck set."
    )


def test_exclude_paths_that_look_concrete_exist() -> None:
    """Check the `exclude` entries that name a real directory.

    Glob-style entries (`**/build`, `patterns/*/src`) are skipped: they are
    patterns, and a pattern matching nothing today is legitimate.
    """
    stale = [
        rel
        for rel in _config().get("exclude", [])
        if not any(ch in rel for ch in "*?[")
        if not (REPO_ROOT / rel).exists()
    ]
    assert not stale, (
        f"pyrightconfig.json `exclude` names path(s) that do not exist: {stale}. "
        "A stale exclude no longer excludes anything — either the path moved (fix "
        "it) or the code is gone (drop the entry)."
    )


def _is_covered(rel: str, includes: list[str]) -> bool:
    return any(rel == inc or rel.startswith(inc.rstrip("/") + "/") for inc in includes)


def test_there_are_first_party_distributions_to_check() -> None:
    """Anti-vacuity guard for the derived inventory below.

    If `lib/` is restructured and the glob stops matching, the parametrised
    test would silently collect nothing and the file would still report green.
    """
    assert len(_pyprojects()) >= 5, (
        f"expected at least 5 first-party distributions under {LIB_ROOT}, found "
        f"{[str(p) for p in _pyprojects()]}. The coverage check below derives its "
        "inventory from this glob, so an empty result makes it vacuous."
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=lambda p: p.parent.name)
def test_first_party_distribution_is_typechecked(pyproject: Path) -> None:
    """Every first-party distribution's importable source falls under `include`.

    Derived from `lib/*/pyproject.toml`, NOT from a list written here. The
    earlier version of this test named three paths in its own body, so a newly
    added distribution was covered by nothing and the test still passed —
    exactly the failure mode this file exists to prevent, one level up. Three
    distributions (idp_sdk, idp_feature_sdk, idp_mcp_connector_pkg) were in that
    blind spot when it was found.

    To drop a distribution from the gate, add it to COVERAGE_EXEMPTIONS with a
    reason. That is a visible decision; an omission is not.
    """
    dist = pyproject.parent.name
    includes = _include_paths()
    source_dirs = _importable_source_dirs(pyproject)

    assert source_dirs, (
        f"{pyproject.relative_to(REPO_ROOT)}: could not resolve any importable "
        "package directory from its setuptools configuration. Either the layout "
        "changed or the derivation in _importable_source_dirs() is stale — fix "
        "that before trusting this test, because an empty result would otherwise "
        "pass silently."
    )

    uncovered = [rel for rel in source_dirs if not _is_covered(rel, includes)]

    if dist in COVERAGE_EXEMPTIONS:
        assert uncovered, (
            f"{dist!r} is listed in COVERAGE_EXEMPTIONS but is now fully covered "
            f"by pyrightconfig.json `include`. Delete the exemption — a stale one "
            "hides the next real gap."
        )
        pytest.skip(f"{dist}: exempt — {COVERAGE_EXEMPTIONS[dist]}")

    assert not uncovered, (
        f"{dist!r} ships first-party Python that no pyrightconfig.json `include` "
        f"entry covers: {uncovered} (current entries: {includes}).\n\n"
        "Add the path to `include` so it is type-checked. Measure the cost first "
        "with `basedpyright --venvpath <repo> <path>`; the three distributions "
        "added in this commit contributed 0 errors, so it is often free.\n\n"
        "If it genuinely must stay out of the gate, add an entry to "
        "COVERAGE_EXEMPTIONS in this file with a one-line reason and an issue "
        "reference, so the gap is a recorded decision rather than an omission."
    )


def test_non_first_party_include_entries_still_exist() -> None:
    """`src/lambda` is not a distribution, so the derived test above cannot see
    it. It is the parent stack's Lambda source and must stay covered."""
    assert _is_covered("src/lambda", _include_paths()), (
        "`src/lambda` (parent-stack Lambda handlers) is no longer covered by any "
        "pyrightconfig.json `include` entry. It holds the two "
        "`reportOperatorIssue` errors this gate was repaired to catch."
    )


#: Tracked Python that `include` is allowed not to cover. Each entry is a
#: pyrightconfig `exclude` pattern, and the reason has to be a property of the
#: files it matches rather than of the directory it happens to name.
#:
#: `notebooks/**/*.ipynb` is here because a notebook's cells share one namespace
#: that basedpyright reads in document order, and several of these notebooks are
#: written to be run after a prior step's notebook has populated the kernel. The
#: gap is real and measured rather than assumed: covering them adds 10 errors, 6
#: of them `reportUndefinedVariable` (`s3_client` in five `notebooks/misc/e2e-*`
#: notebooks and `Image` in one), which are worth triaging on their own rather
#: than as part of a config change. `scripts/lint_debt.json` records the same
#: carve-out for ruff, with its own premise.
TYPECHECK_SCOPE_EXCLUSIONS: dict[str, str] = {
    "notebooks/**/*.ipynb": (
        "notebook cells share a namespace across files by design; the 10 errors "
        "this shields are notebook-authoring issues, not application types"
    ),
}


def _tracked_python() -> list[str]:
    """Every tracked `.py` file, from git rather than a filesystem walk.

    `rglob` from the repo root would also walk `scratch/` and
    `.claude/worktrees/`, which routinely hold whole copies of this repository —
    the failure mode `test_repo_walk_guards_prune_local_work.py` exists for.
    """
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def _excluded_by(rel: str) -> str | None:
    for pattern in _config().get("exclude", []):
        if pattern.startswith("**/"):
            if pattern[3:] in Path(rel).parts or fnmatch(rel, pattern):
                return pattern
        elif fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/"):
            return pattern
    return None


def test_there_is_tracked_python_to_check() -> None:
    """Anti-vacuity guard: an empty file list would make the check below pass."""
    assert len(_tracked_python()) > 500, (
        "git ls-files '*.py' returned "
        f"{len(_tracked_python())} paths, which is too few to be this repository. "
        "The coverage check below would be vacuous."
    )


def test_every_tracked_python_file_is_type_checked() -> None:
    """`include` must cover the whole tree, derived from git rather than listed.

    `include` named six paths and reached 432 of 1230 tracked `.py` files. The
    other 798 — every `lib/*/tests` suite, all 137 files under `scripts/`, the
    78 resolver Lambdas under `nested/`, `feature-platform/`, `benchmarks/`,
    `samples/` — were type-checked by nothing except `make typecheck-pr`, and
    only for the files a given pull request happened to touch. Two
    `NameError`-class defects reached `develop` through that gap.

    This asserts the property rather than the list: a new top-level tree holding
    Python fails here instead of being silently uncovered, which is the failure
    the six-entry list could not detect. Broadening `include` cost 3 errors,
    all genuine (a `-> (str, str)` annotation, an `int` subscripted as a string,
    and `"literal" in __doc__` where `__doc__` is `str | None`).
    """
    includes = _include_paths()
    uncovered: list[str] = []
    for rel in _tracked_python():
        if not _is_covered(rel, includes):
            uncovered.append(f"{rel} (no include entry)")
            continue
        pattern = _excluded_by(rel)
        if pattern and pattern not in TYPECHECK_SCOPE_EXCLUSIONS:
            uncovered.append(f"{rel} (excluded by {pattern!r})")

    assert not uncovered, (
        f"{len(uncovered)} tracked .py file(s) are outside `make typecheck`:\n  "
        + "\n  ".join(uncovered[:20])
        + "\n\nAdd the top-level path to pyrightconfig.json `include` (measure the "
        "cost first: broadening it to the whole tree cost 3 errors), or, if the "
        "files genuinely must stay out, add the `exclude` pattern to "
        "TYPECHECK_SCOPE_EXCLUSIONS in this file with a reason that is a property "
        "of those files."
    )


@pytest.mark.parametrize("pattern", sorted(TYPECHECK_SCOPE_EXCLUSIONS))
def test_scope_exclusion_still_shields_something(pattern: str) -> None:
    """A carve-out that matches nothing is a stale claim, not a decision."""
    excludes = _config().get("exclude", [])
    assert pattern in excludes, (
        f"TYPECHECK_SCOPE_EXCLUSIONS names {pattern!r}, which is no longer a "
        "pyrightconfig.json `exclude` pattern. Drop the entry — a stale "
        "exemption hides the next real gap."
    )
    root = REPO_ROOT / pattern.split("/", 1)[0]
    suffix = pattern.rsplit("*", 1)[-1]
    assert next(root.rglob(f"*{suffix}"), None) is not None, (
        f"{pattern!r} matches no file under {root.name}/, so it shields nothing."
    )


def test_venv_is_not_pinned_in_the_config() -> None:
    """`venvPath`/`venv` made the gate's exit code mean nothing.

    They pointed at `./.venv`, so in any checkout without one — a `git worktree`,
    a fresh clone, a CI job that installed basedpyright from npm and nothing else
    — basedpyright printed one line about the missing directory and exited **3**
    regardless of findings. `make typecheck` therefore failed identically whether
    the tree had type errors or not.

    Dropping them is diagnostic-neutral here: with and without a `.venv` present
    the run reports the same 0 errors and 92 warnings (one `tqdm` stub-resolution
    warning trades places with one `reportReturnType` in the same file), because
    `reportMissingImports` and the `reportUnknown*` rules are already "none".
    """
    config = _config()
    present = sorted(key for key in ("venvPath", "venv") if key in config)
    assert not present, (
        f"pyrightconfig.json sets {present}, which makes basedpyright exit 3 in "
        "any checkout without that virtualenv — the same exit code for a config "
        "problem as for nothing at all. If a pinned environment is genuinely "
        "needed, pass --venvpath from the Makefile recipe so a missing one is a "
        "clear message rather than an opaque exit code."
    )


def test_no_exclude_entry_shadows_an_include_entry() -> None:
    """`exclude` beats `include`, so the gate can be gutted through either array.

    Adding `lib/idp_cli_pkg` to `exclude` while leaving the corrected `include`
    entry in place was measured to drop `filesAnalyzed` from 335 to 330 with
    every other test in this file still passing — the same silent narrowing as
    the stale include path, reintroduced from the other direction.

    This is a pure-config check: it does not prove a non-zero file count per
    entry (that would need a basedpyright invocation, which is far slower than
    the rest of this file). It catches an exclude entry that covers an include
    entry outright, which is the shape the mistake actually takes.
    """
    includes = _include_paths()
    excludes = list(_config().get("exclude", []))

    shadowed: list[str] = []
    for inc in includes:
        inc_norm = inc.rstrip("/")
        for exc in excludes:
            exc_norm = exc.rstrip("/")
            # An exclude entry shadows an include entry if it names the entry
            # itself or any ancestor directory of it.
            candidates = [inc_norm, *(str(p) for p in Path(inc_norm).parents)]
            if any(
                c == exc_norm or fnmatch(c, exc_norm) for c in candidates if c != "."
            ):
                shadowed.append(f"exclude {exc!r} shadows include {inc!r}")

    assert not shadowed, (
        "pyrightconfig.json `exclude` cancels out an `include` entry, so those "
        "files are silently dropped from the gate:\n  " + "\n  ".join(shadowed) + "\n"
        "`exclude` wins over `include` in pyright. Narrow the exclude pattern, or "
        "drop the include entry if it is genuinely not meant to be checked."
    )
