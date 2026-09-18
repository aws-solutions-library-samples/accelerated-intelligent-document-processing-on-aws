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

import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest
from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_ROOT = REPO_ROOT / "lib"

pytestmark = pytest.mark.unit

# Directory names that are never part of a distribution's importable source.
IGNORED_DIR_NAMES = {"__pycache__", "build", "dist", "tests", "test", "examples"}


def _pyprojects() -> list[Path]:
    return sorted(LIB_ROOT.glob("*/pyproject.toml"))


def _discovered_packages(pyproject: Path) -> tuple[set[str], list[str]]:
    """Return (packages setuptools would install, exclude patterns in effect)."""
    cfg = tomllib.loads(pyproject.read_text())
    setuptools_cfg = cfg.get("tool", {}).get("setuptools", {})
    packages = setuptools_cfg.get("packages")
    pkg_dir = pyproject.parent

    # Explicit list form: `packages = ["idp_cli"]`.
    if isinstance(packages, list):
        return set(packages), []

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
    return found, list(exclude)


def _source_dirs_with_python(pkg_dir: Path, top_levels: set[str]) -> list[Path]:
    """Every directory under the distribution's top-level packages holding .py."""
    out: list[Path] = []
    for top in sorted(top_levels):
        top_dir = pkg_dir / top
        if not top_dir.is_dir():
            continue
        for candidate in [
            top_dir,
            *sorted(p for p in top_dir.rglob("*") if p.is_dir()),
        ]:
            rel_parts = candidate.relative_to(pkg_dir).parts
            if any(part in IGNORED_DIR_NAMES for part in rel_parts):
                continue
            if any(part.endswith(".egg-info") for part in rel_parts):
                continue
            if any(p.suffix == ".py" for p in candidate.iterdir() if p.is_file()):
                out.append(candidate)
    return out


def test_there_are_first_party_distributions_to_check() -> None:
    """Guard against this whole file passing vacuously if lib/ is restructured."""
    assert len(_pyprojects()) >= 5, (
        f"expected at least 5 first-party distributions under {LIB_ROOT}, "
        f"found {[str(p) for p in _pyprojects()]}"
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=lambda p: p.parent.name)
def test_every_source_dir_is_a_discovered_package(pyproject: Path) -> None:
    discovered, excludes = _discovered_packages(pyproject)
    pkg_dir = pyproject.parent
    top_levels = {name.split(".")[0] for name in discovered}
    assert top_levels, f"{pyproject} discovered no packages at all"

    missing: list[str] = []
    for src_dir in _source_dirs_with_python(pkg_dir, top_levels):
        dotted = ".".join(src_dir.relative_to(pkg_dir).parts)
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
    discovered, _ = _discovered_packages(pyproject)
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
