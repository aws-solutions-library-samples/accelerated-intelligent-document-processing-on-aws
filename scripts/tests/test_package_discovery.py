# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert every source directory of every first-party library is packaged.

`lib/idp_common_pkg/idp_common/agents/utils/` shipped two modules and no
`__init__.py`. `lib/idp_common_pkg/pyproject.toml` discovers packages with
setuptools `[tool.setuptools.packages.find]`, which — unlike `find-namespace` —
skips any directory lacking `__init__.py`. So `idp_common.agents.utils` was
absent from the built wheel, while `agents/factory/agent_factory.py` imported
`..utils.conversation_manager` and `..utils.memory_provider` with no guard.

That combination cannot fail locally: an editable install resolves imports
against the source tree, where the directory plainly exists. It fails only in a
Lambda built from the wheel, only on the conversational-agent path, and only at
the moment a user reaches that path — the worst possible place to discover it.

This test reproduces setuptools' own discovery from the declared configuration
and asserts that every directory holding a `.py` file resolves to a discovered
package. It is deliberately not a wheel build: no network, no build isolation,
no minutes of CI time, and the answer is identical because it applies the same
finder to the same config. It also covers all five first-party distributions
rather than only the one that was broken, including `lib/idp_cli_pkg`, whose
`packages = ["idp_cli"]` is an explicit list that would omit any subpackage
added under it.

Adding a new module directory is now a gate failure until it has an
`__init__.py` (or the config switches to `find-namespace`), instead of a
deployment failure later.
"""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest
from setuptools import find_namespace_packages, find_packages

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

    # `find` requires __init__.py; `find-namespace` does not.
    if "find-namespace" in packages:
        finder, spec = find_namespace_packages, packages["find-namespace"]
    elif "find" in packages:
        finder, spec = find_packages, packages["find"]
    else:
        pytest.skip(f"{pyproject} uses an unrecognised packages table")

    where = spec.get("where", ["."])
    include = spec.get("include", ["*"])
    exclude = spec.get("exclude", [])

    found: set[str] = set()
    for root in where:
        found.update(
            finder(where=str(pkg_dir / root), include=include, exclude=exclude)
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
        f"{pyproject.relative_to(REPO_ROOT)} would not package these directories, "
        f"even though each contains .py files: {sorted(missing)}. setuptools "
        "`packages.find` skips any directory without an __init__.py, so these "
        "modules are absent from the built wheel and raise ModuleNotFoundError on "
        "a non-editable install. Add an __init__.py (preferred — it makes the "
        "package boundary explicit), or switch the config to `find-namespace`."
    )


def test_agents_utils_specifically_is_packaged() -> None:
    """Pin the exact regression from issue #924 by name.

    The generic test above is the real guard, but it derives its expectations
    from the tree. If `agents/utils` were deleted rather than fixed, that test
    would pass; this one says out loud which subpackage must ship.
    """
    pyproject = LIB_ROOT / "idp_common_pkg" / "pyproject.toml"
    discovered, _ = _discovered_packages(pyproject)
    assert "idp_common.agents.utils" in discovered, (
        "idp_common.agents.utils is not in the discovered package set. "
        "agent_factory.create_conversational_agent() imports "
        "..utils.conversation_manager and ..utils.memory_provider unguarded, so "
        "omitting it breaks the agent chat path on any wheel-based install."
    )
