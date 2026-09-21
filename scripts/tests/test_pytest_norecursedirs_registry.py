# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards on `norecursedirs` in the tracked `pytest.ini` files.

`norecursedirs` is a path exemption: it decides which directories pytest never
walks, so a directory named there is one no invocation can collect. Three
properties have to hold for that to be a safe thing to write, and none of them
were checked.

**The restated default has to stay complete.** Setting `norecursedirs` REPLACES
pytest's default rather than extending it, so an author adding one directory must
restate `build`, `node_modules`, `dist`, `.*` and the rest. Dropping one would
start walking build output and a vendored `.venv`, which does not fail loudly --
it produces a slower run that collects somebody else's tests.

**It has to shield something.** A `norecursedirs` entry for a directory holding no
`test_*.py` is dead, and a dead entry silently pre-exempts whatever next occupies
that path. This is the non-vacuity ratchet the exemption registry asks for.

**It has to agree with the test-root registry.** `scripts/run_all_tests.py` is the
thing that decides which suites run, and its guard hard-errors on a directory
holding `test_*.py` that is in neither of its two registries. A `norecursedirs`
entry that is not also registered there would be an exclusion recorded in one
place and invisible in the other -- exactly the shape this repository has been
bitten by, where a control exists and nothing consults it.

Registered in `scripts/tests/gate_exemptions.json` as
`lib/idp_common_pkg/pytest.ini::norecursedirs`.
"""

from __future__ import annotations

import configparser
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pytest_default_norecursedirs() -> tuple[str, ...]:
    """pytest's own default for `norecursedirs`, which setting it REPLACES.

    Read out of a real pytest parser rather than restated here. Restating it would
    fail silently in the one direction this guard exists for: a pytest release that
    adds a directory to the default would leave the tracked `pytest.ini` quietly
    narrower with this test still green. Reading it means a pytest release that
    moves this attribute fails loudly instead.

    `_parser._inidict` is private, and so is `get_config`; there is no public read
    of an ini-option *default*, so the choice is only which private thing to touch.
    The entry is a 3-tuple of (help, type, default) — asserted as a list rather than
    indexed blindly, so a shape change fails as a shape change rather than being
    converted into a plausible wrong value.
    """
    from _pytest.config import get_config

    config = get_config([])
    spec = config._parser._inidict["norecursedirs"]
    default = spec[2]
    assert isinstance(default, list) and default, (
        "pytest's norecursedirs ini-option no longer exposes a non-empty list "
        f"default at _inidict[...][2]; got {default!r}. The shape of "
        "_pytest.config.Parser._inidict has changed — read it wherever it lives now."
    )
    return tuple(default)


def _tracked_pytest_inis() -> list[Path]:
    # git's default pathspec `*` crosses `/`, so `*/pytest.ini` reaches every
    # package-level file at any depth; no deeper pattern is needed.
    out = subprocess.run(
        ["git", "ls-files", "pytest.ini", "*/pytest.ini"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel for rel in out]


def _run_all_tests_module():
    """Load scripts/run_all_tests.py under a private alias.

    Same idiom as test_testing_doc.py and its siblings: the module is a script, not
    an importable package member, and loading it under its own top-level name would
    put a second copy of it in sys.modules alongside theirs.
    """
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests_for_pytest_ini", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norecursedirs(path: Path) -> tuple[str, ...]:
    # interpolation=None: ConfigParser's default BasicInterpolation would raise on a
    # literal '%' in a future value rather than reading it as written.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    if not parser.has_option("pytest", "norecursedirs"):
        return ()
    return tuple(parser.get("pytest", "norecursedirs").split())


def _files_with_norecursedirs() -> list[Path]:
    return [p for p in _tracked_pytest_inis() if _norecursedirs(p)]


def test_at_least_one_tracked_pytest_ini_is_discoverable_with_this_surface() -> None:
    """The discovery pathspec has to reach the files that use the surface.

    `scripts/tests/exemption_discovery.py` passes its source names to `git
    ls-files` as pathspecs. A bare `pytest.ini` has no wildcard, so it matched
    only the repo-root file -- which carries no `norecursedirs` -- and the one
    tracked file that does carry one was invisible to the exemption registry.
    """
    assert _tracked_pytest_inis(), "no tracked pytest.ini found at all"
    assert _files_with_norecursedirs(), (
        "no tracked pytest.ini sets norecursedirs, so this guard is vacuous. "
        "If the setting was removed, delete this module and its registry entry."
    )


@pytest.mark.parametrize(
    "path", _files_with_norecursedirs(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_the_restated_pytest_default_is_complete(path: Path) -> None:
    """Every directory pytest excludes by default must still be excluded."""
    configured = set(_norecursedirs(path))
    missing = sorted(set(_pytest_default_norecursedirs()) - configured)
    assert not missing, (
        f"{path.relative_to(REPO_ROOT)} sets norecursedirs and so REPLACES pytest's "
        f"default, but does not restate {missing}. Those directories are now walked. "
        "Add them back alongside whatever this file meant to exclude."
    )


def _extra_entries(path: Path) -> tuple[str, ...]:
    default = set(_pytest_default_norecursedirs())
    return tuple(e for e in _norecursedirs(path) if e not in default)


@pytest.mark.parametrize(
    "path", _files_with_norecursedirs(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_each_added_exclusion_shields_at_least_one_test_file(path: Path) -> None:
    """Non-vacuity: an exclusion that hides nothing is dead and pre-exempting.

    A `norecursedirs` entry naming a directory with no `test_*.py` under it
    excludes nothing today and silently excludes whatever is added there next.
    """
    for entry in _extra_entries(path):
        target = path.parent / entry
        assert target.is_dir(), (
            f"{path.relative_to(REPO_ROOT)} excludes '{entry}', which is not a "
            "directory. The exclusion shields nothing and is stale."
        )
        shielded = list(target.rglob("test_*.py"))
        assert shielded, (
            f"{path.relative_to(REPO_ROOT)} excludes '{entry}', which holds no "
            "test_*.py. The exclusion is vacuous: it shields nothing now and "
            "pre-exempts whatever is put there next. Delete it."
        )


@pytest.mark.parametrize(
    "path", _files_with_norecursedirs(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_each_added_exclusion_is_also_registered_as_a_non_running_suite(
    path: Path,
) -> None:
    """The two exclusions must agree.

    Excluding a directory from collection and registering it as a suite that
    `make test` does not run are separate decisions recorded in separate files. If
    only the first is made, the directory holds `test_*.py` that nothing runs and
    nothing says so -- which is the state `scripts/run_all_tests.py`'s guard and
    `docs/testing.md`'s table exist to prevent.
    """
    registry = _run_all_tests_module()
    registered = {r.rstrip("/") for r in (*registry.RUN_ROOTS, *registry.QUARANTINE)}
    for entry in _extra_entries(path):
        target = path.parent / entry
        if not target.is_dir():
            continue
        for test_file in target.rglob("test_*.py"):
            rel_dir = test_file.parent.relative_to(REPO_ROOT).as_posix()
            assert rel_dir in registered, (
                f"{path.relative_to(REPO_ROOT)} excludes '{entry}' from pytest "
                f"collection, but '{rel_dir}' is in neither RUN_ROOTS nor "
                "QUARANTINE in scripts/run_all_tests.py. Register it there (and in "
                "docs/testing.md) so the exclusion is visible to the test-root "
                "guard as well as to pytest."
            )
