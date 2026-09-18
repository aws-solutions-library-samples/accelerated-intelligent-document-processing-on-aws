# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every package directory of every first-party library carries an explicit
`__init__.py`.

This is a **policy** gate, deliberately stricter than setuptools. It is not a
model of what setuptools would or would not ship, and it must not be read as one.

What prompted it: `lib/idp_common_pkg/idp_common/agents/utils/` shipped two
modules and no `__init__.py`, while `agents/factory/agent_factory.py` imports
`..utils.conversation_manager` and `..utils.memory_provider` with no guard. That
looked like a packaging hole, and issue #924 was filed on the theory that the
subpackage was missing from the built wheel and would raise
`ModuleNotFoundError` for anyone on a non-editable install.

**It was not, and it would not.** For a `pyproject.toml`
`[tool.setuptools.packages.find]` table, `namespaces` defaults to **true**:
`setuptools/config/expand.py::find_packages` takes `namespaces=True` and
dispatches to `PEP420PackageFinder`, not `PackageFinder`, so a directory with no
`__init__.py` is discovered anyway. Measured on the pre-fix tree,
`read_configuration("pyproject.toml", expand=True)` returned 82 packages
including `idp_common.agents.utils`; a wheel built from pre-fix source with
`setuptools.build_meta.build_wheel` contained both `conversation_manager.py` and
`memory_provider.py`, and installing it `--no-deps` into a clean virtual
environment resolved the import. No user ever hit the failure this file was
originally written to describe.

The `__init__.py` is still worth having, and this gate is still worth having,
for a different and narrower reason: an implicit PEP 420 namespace package is an
accident waiting to be relied on. An explicit marker states the package boundary
outright, keeps behaviour identical under a `packages` list, under `setup.cfg`
(where `find` really is strict), and under any tool that walks the tree itself,
and removes the need for a reader to know the `namespaces` default in order to
predict what ships.

So this file applies setuptools' STRICT finder (`find_packages`, which does
require `__init__.py`) as the mechanism for checking the policy, and reports a
missing marker as a policy violation rather than as a predicted import failure.
It covers all five first-party distributions, including `lib/idp_cli_pkg`, whose
`packages = ["idp_cli"]` is an explicit list that would omit any subpackage added
under it — a case where the omission IS real, because an explicit list is not
subject to any finder.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from fnmatch import fnmatch
from pathlib import Path

import pytest
from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_ROOT = REPO_ROOT / "lib"

pytestmark = pytest.mark.unit

# Directory names that are never part of a distribution's importable source.
IGNORED_DIR_NAMES = {"__pycache__", "build", "dist", "tests", "test", "examples"}

# Distributions whose built wheel is allowed to omit a source package directory.
# Format: "<dist>/<dotted.package>" -> reason (with an issue reference).
# Empty, and it should stay that way: a module that is in the source tree but not
# in the wheel is an import failure waiting for the first non-editable install.
WHEEL_OMISSIONS_ALLOWED: dict[str, str] = {}


def _pyprojects() -> list[Path]:
    return sorted(LIB_ROOT.glob("*/pyproject.toml"))


def _packages_table_kind(pyproject: Path) -> str:
    """Classify the distribution's packages declaration without skipping.

    Split out from `_discovered_packages` so the anti-vacuity guard can count
    the distributions that will actually be CHECKED. `pytest.skip` is a silent
    pass: a distribution carrying only a `[project]` table plus an unmarked
    subpackage was measured to give "7 passed, 1 skipped" — green, having
    examined nothing. Counting `lib/*/pyproject.toml` files does not notice,
    because the file is there either way; it is the check that goes missing.
    """
    cfg = tomllib.loads(pyproject.read_text())
    packages = cfg.get("tool", {}).get("setuptools", {}).get("packages")
    if isinstance(packages, list):
        return "list"
    if not isinstance(packages, dict):
        return "absent"
    return "find" if "find" in packages else "unrecognised"


def _discovered_packages(pyproject: Path) -> tuple[set[str], list[str], list[str]]:
    """Return (packages setuptools would install, exclude patterns, where roots).

    `where` is returned because it is needed to walk the tree correctly. Earlier
    it was honoured for discovery and then dropped, which made the whole check
    vacuous for a src-layout distribution — see `_source_dirs_with_python`.
    """
    cfg = tomllib.loads(pyproject.read_text())
    setuptools_cfg = cfg.get("tool", {}).get("setuptools", {})
    packages = setuptools_cfg.get("packages")
    pkg_dir = pyproject.parent

    # Explicit list form: `packages = ["idp_cli"]`. `package-dir` can relocate
    # it, so honour that rather than assuming the flat layout.
    if isinstance(packages, list):
        pkg_dirs = setuptools_cfg.get("package-dir", {})
        roots = sorted({str(Path(v).parent) if v else "." for v in pkg_dirs.values()})
        return set(packages), [], roots or ["."]

    if not isinstance(packages, dict):
        pytest.skip(f"{pyproject} declares no [tool.setuptools.packages] table")

    # `find_packages` is the STRICT finder: it requires __init__.py. That is the
    # point — it is the mechanism for checking the explicit-marker policy, NOT a
    # model of what setuptools will ship. setuptools' own pyproject path passes
    # `namespaces=True` and so uses `find_namespace_packages` (PEP 420), which
    # would discover an unmarked directory and make this gate vacuous.
    #
    # There is deliberately no `find-namespace` branch. `find-namespace` is the
    # setup.cfg spelling (`[options.packages.find-namespace]`) and can never
    # appear in a pyproject.toml table: setuptools' pyproject validator allows
    # exactly one key, `find`, with `additional keys: False`, and rejects
    # `find-namespace` with a ValueError. A branch for it would be unreachable.
    if "find" in packages:
        spec = packages["find"]
    else:
        pytest.skip(f"{pyproject} uses an unrecognised packages table")

    where = spec.get("where", ["."])
    include = spec.get("include", ["*"])
    exclude = spec.get("exclude", [])

    found: set[str] = set()
    for root in where:
        found.update(
            find_packages(where=str(pkg_dir / root), include=include, exclude=exclude)
        )
    return found, list(exclude), list(where)


def _source_dirs_with_python(
    pkg_dir: Path, top_levels: set[str], where: list[str]
) -> list[tuple[Path, str]]:
    """Every directory holding .py under the distribution's top-level packages.

    Returns (directory, dotted package name) pairs. The dotted name is relative
    to the `where` root, not to the distribution root, which is what setuptools
    means by a package name and what the wheel archive path mirrors.

    `where` is a parameter because omitting it made this walk silently examine
    nothing for a src-layout distribution: discovery ran against `pkg_dir/src`
    while the walk looked for `pkg_dir/<top>`, `is_dir()` was False, the loop
    `continue`d, and the caller's `assert top_levels` still passed because
    packages *had* been discovered. A `lib/idp_srclayout_pkg/` with
    `where = ["src"]` and an unmarked `src/idp_srclayout/unmarked/mod.py` was
    measured at "8 passed". No distribution here is src-layout today, so this is
    a forward guard rather than a live defect.
    """
    out: list[tuple[Path, str]] = []
    for root in where:
        base = (pkg_dir / root).resolve()
        if not base.is_dir():
            continue
        for top in sorted(top_levels):
            top_dir = base / top
            if not top_dir.is_dir():
                continue
            for candidate in [
                top_dir,
                *sorted(p for p in top_dir.rglob("*") if p.is_dir()),
            ]:
                rel_parts = candidate.relative_to(base).parts
                if any(part in IGNORED_DIR_NAMES for part in rel_parts):
                    continue
                if any(part.endswith(".egg-info") for part in rel_parts):
                    continue
                if any(p.suffix == ".py" for p in candidate.iterdir() if p.is_file()):
                    out.append((candidate, ".".join(rel_parts)))
    return out


@functools.lru_cache(maxsize=None)
def _wheel_entries(pyproject: Path) -> tuple[str, ...]:
    """Build the distribution's wheel offline and return its member names.

    Built from a COPY in a temp directory, never in place. Building in the source
    tree leaves a `build/` directory behind, and a stale one silently changes the
    answer — it is why `idp_common_pkg`'s pyproject carries seven `*build*`
    exclude patterns in the first place. Copying also keeps this test from
    colliding with the other suites `make test-packages-cicd` runs in the same
    tree. Measured at ~1s per distribution, ~3s for all five.

    No network and no build isolation: `setuptools.build_meta` is called directly
    in a subprocess, so nothing is downloaded. setuptools is declared in
    idp_common_pkg's `test` extra for exactly this kind of direct use.
    """
    pkg_dir = pyproject.parent
    # Copy source and tests, but not build outputs or caches, so the wheel is
    # faithful to what `pip wheel` would produce from a clean checkout.
    ignore = shutil.ignore_patterns(
        "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", ".venv"
    )
    tmp = Path(tempfile.mkdtemp(prefix="wheel-contents-"))
    try:
        source = tmp / pkg_dir.name
        shutil.copytree(pkg_dir, source, ignore=ignore, symlinks=True)
        out = tmp / "wheel"
        out.mkdir()
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from setuptools import build_meta; "
                "build_meta.build_wheel(sys.argv[1])",
                str(out),
            ],
            cwd=source,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(source)},
        )
        if proc.returncode != 0:
            pytest.fail(
                f"could not build a wheel for {pyproject.relative_to(REPO_ROOT)} "
                f"(rc={proc.returncode}). This test cannot check wheel contents "
                f"without one, so the failure is reported rather than skipped.\n"
                f"stderr tail:\n{proc.stderr[-2000:]}"
            )
        wheels = sorted(out.glob("*.whl"))
        assert wheels, f"build produced no .whl for {pyproject}"
        with zipfile.ZipFile(wheels[0]) as zf:
            return tuple(zf.namelist())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_there_are_first_party_distributions_to_check() -> None:
    """Guard against this whole file passing vacuously if lib/ is restructured."""
    assert len(_pyprojects()) >= 5, (
        f"expected at least 5 first-party distributions under {LIB_ROOT}, "
        f"found {[str(p) for p in _pyprojects()]}"
    )


def test_every_distribution_is_actually_checked_not_skipped() -> None:
    """Count the distributions that get CHECKED, not the pyproject files present.

    `_discovered_packages` calls `pytest.skip` for a distribution whose packages
    declaration it cannot read. A skip is a silent pass, and the guard above
    cannot see it: it counts `lib/*/pyproject.toml` files, and the file is still
    there — it is the check that went missing. So a distribution with only a
    `[project]` table plus an unmarked subpackage reported "7 passed, 1 skipped".

    Deliberately not order-dependent (no shared "was checked" set populated by
    the parametrised tests, which would be wrong under xdist or with `-k`): this
    re-derives from the same files which distributions are resolvable.

    0 skips measured today; this is a forward guard.
    """
    unresolvable = {
        p.parent.name: _packages_table_kind(p)
        for p in _pyprojects()
        if _packages_table_kind(p) not in {"list", "find"}
    }
    assert not unresolvable, (
        f"these first-party distributions declare no packages table this file can "
        f"read, so every check here SKIPS them and reports green: {unresolvable}.\n"
        "'absent' means no [tool.setuptools.packages] at all; 'unrecognised' means "
        "a table with no `find` key. Either give the distribution an explicit "
        "packages declaration, or teach _discovered_packages to handle the new "
        "form — do not leave it skipped."
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=lambda p: p.parent.name)
def test_every_source_dir_is_a_discovered_package(pyproject: Path) -> None:
    discovered, excludes, where = _discovered_packages(pyproject)
    pkg_dir = pyproject.parent
    top_levels = {name.split(".")[0] for name in discovered}
    assert top_levels, f"{pyproject} discovered no packages at all"

    source_dirs = _source_dirs_with_python(pkg_dir, top_levels, where)
    assert source_dirs, (
        f"{pyproject.relative_to(REPO_ROOT)}: packages were discovered but the "
        f"walk found no directories to check (where={where}). `assert top_levels` "
        "above does NOT catch this — discovery succeeded; it is the walk that came "
        "back empty, which is how a src-layout distribution passed while examining "
        "nothing. Fix _source_dirs_with_python rather than trusting this result."
    )

    missing: list[str] = []
    for _src_dir, dotted in source_dirs:
        if dotted in discovered:
            continue
        # Respect a deliberate exclusion rather than reporting it as a defect.
        if any(fnmatch(dotted, pattern) for pattern in excludes):
            continue
        missing.append(dotted)

    assert not missing, (
        f"{pyproject.relative_to(REPO_ROOT)}: these directories contain .py files "
        f"but carry no __init__.py: {sorted(missing)}. Add one to each.\n\n"
        "This is a repository POLICY — every package directory declares itself "
        "explicitly — not a prediction that the modules will be missing from the "
        "wheel. With a pyproject.toml `packages.find` table setuptools defaults "
        "to `namespaces=True` and would discover them anyway, as an implicit PEP "
        "420 namespace package. The policy exists because relying on that is "
        "fragile: the same tree behaves differently under an explicit `packages` "
        "list, under setup.cfg's strict `find`, and under any tool that walks the "
        "directories itself, and a reader has to know the `namespaces` default to "
        "predict what ships. An `__init__.py` makes the package boundary true "
        "everywhere.\n\n"
        "If a directory genuinely should NOT be a package, add it to "
        "`exclude` in the pyproject table (respected here) or to "
        "IGNORED_DIR_NAMES in this file."
    )


def test_agents_utils_specifically_is_packaged() -> None:
    """Pin the directory from issue #924 by name.

    The generic test above is the real guard, but it derives its expectations
    from the tree. If `agents/utils` were deleted rather than marked, that test
    would pass; this one says out loud which subpackage must carry a marker.
    """
    pyproject = LIB_ROOT / "idp_common_pkg" / "pyproject.toml"
    discovered, _, _ = _discovered_packages(pyproject)
    assert "idp_common.agents.utils" in discovered, (
        "idp_common.agents.utils has no __init__.py, so the strict finder does "
        "not see it. `agent_factory.create_conversational_agent()` imports "
        "..utils.conversation_manager and ..utils.memory_provider unguarded, "
        "which is why this directory in particular is named here.\n\n"
        "To be accurate about the consequence: an unmarked directory here does "
        "NOT break a wheel install. setuptools' pyproject `packages.find` "
        "defaults to `namespaces=True`, and a wheel built from the unmarked tree "
        "was measured to contain both modules and to import cleanly from a "
        "non-editable install. The requirement is the explicit-marker policy "
        "described in this module's docstring, not an averted ModuleNotFoundError."
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=lambda p: p.parent.name)
def test_every_source_package_is_in_the_built_wheel(pyproject: Path) -> None:
    """Assert against the actual wheel, not against setuptools' configuration.

    Why this exists alongside the policy test above. That test honours the
    pyproject `exclude` array, deliberately and defensibly — it is a policy test
    about explicit markers, not a prediction about wheel contents. But the
    consequence is that `exclude` is an unguarded route to the very outcome this
    file was written about: appending `"idp_common.bedrock*"` and
    `"idp_common.ocr*"` to `idp_common_pkg`'s existing `exclude` array drops both
    subpackages from the wheel (discovered package count 82 -> 80) and the policy
    test still reports green, because it treats an exclusion as a deliberate
    decision rather than a defect.

    Nothing in the repository asserted anything about wheel contents. This does:
    it builds the wheel and requires every source package directory to be in it.
    It does NOT honour `exclude`, on purpose — a module present in the tree and
    absent from the wheel is an import failure on the first non-editable install
    however it came about. A genuine exclusion goes in WHEEL_OMISSIONS_ALLOWED
    with a reason, which is a visible decision.

    This is also the only check here that would have settled issue #924 directly
    rather than by reasoning about setuptools' `namespaces` default.
    """
    discovered, _, where = _discovered_packages(pyproject)
    pkg_dir = pyproject.parent
    top_levels = {name.split(".")[0] for name in discovered}
    assert top_levels, f"{pyproject} discovered no packages at all"

    source_dirs = _source_dirs_with_python(pkg_dir, top_levels, where)
    assert source_dirs, (
        f"{pyproject.relative_to(REPO_ROOT)}: found no source directories to "
        "check against the wheel (where={where}). Either the layout changed or the "
        "walk is stale — an empty list would make this test pass while examining "
        "nothing."
    )

    entries = _wheel_entries(pyproject)
    assert entries, f"{pyproject} built an empty wheel"

    missing: list[str] = []
    for _src_dir, dotted in source_dirs:
        if f"{pyproject.parent.name}/{dotted}" in WHEEL_OMISSIONS_ALLOWED:
            continue
        # A wheel lays packages out by dotted name, with the `where` root
        # stripped — which is exactly what `dotted` already is.
        prefix = dotted.replace(".", "/") + "/"
        if not any(
            name.startswith(prefix) and name.endswith(".py") for name in entries
        ):
            missing.append(dotted)

    assert not missing, (
        f"{pyproject.relative_to(REPO_ROOT)}: these package directories hold .py "
        f"files in the source tree but contribute nothing to the built wheel: "
        f"{sorted(missing)}.\n\n"
        "Anyone installing this distribution non-editably (every Lambda build "
        "does) gets ModuleNotFoundError for these. The usual cause is an "
        "over-broad pattern in the pyproject `exclude` array — note that the "
        "policy test above deliberately honours `exclude`, so it will NOT catch "
        "this.\n\n"
        "If the omission is intended, add '<dist>/<dotted.package>' to "
        "WHEEL_OMISSIONS_ALLOWED in this file with a reason."
    )
