"""`make version` must leave `lib/idp_common_pkg/uv.lock` consistent with the bump.

The lockfile records the project's own version. `make lock-constraints` runs
`uv export --locked`, which refuses a lockfile that disagrees with its
`pyproject.toml`, and `make setup` and every CI job reach that target first. So a
version bump that rewrites `pyproject.toml` and not the lockfile turns the first
pipeline after it red before any test runs (#1818).

The test runs the real target against a scratch copy of exactly the files the
target says it stamps (read from its own "Version updated ... in:" list, so a file
added to the target is copied without editing this test), then asks `uv` itself
whether the lockfile is still current. `uv` is a hard requirement rather than a
skip: `make setup` cannot run without it, and a skip here would read as a pass.

`uv lock --check` stands in for `uv export --locked` deliberately: both refuse a
lockfile whose project version disagrees with `pyproject.toml`, and `--check` does
it without writing an export. `PYTHON` is passed through because the scratch tree
has no `.venv`, so the Makefile would otherwise pick whatever `python3` is on the
path, and its version-validation step reports a missing `packaging` module as an
invalid version string.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
LOCK = Path("lib/idp_common_pkg/uv.lock")
BUMP = "9.8.7"


def _stamped_paths() -> list[Path]:
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\nversion:")
    recipe = text[start : text.index("\n##@ ", start)]
    paths = [Path(p) for p in re.findall(r'^\t@echo "  - (\S+)"', recipe, re.M)]
    assert paths, "could not read the `version` target's stamped-path list"
    return paths


def _scratch_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(MAKEFILE, root / "Makefile")
    shutil.copytree(REPO_ROOT / "make", root / "make")
    for rel in _stamped_paths():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, root / rel)
    return root


def _uv() -> str:
    uv = shutil.which("uv")
    assert uv, "uv is required (make setup needs it); install it with `pip install uv`"
    return uv


@pytest.mark.unit
def test_the_lockfile_is_one_of_the_stamped_paths() -> None:
    assert LOCK in _stamped_paths()


@pytest.mark.unit
def test_make_version_leaves_the_lockfile_current(tmp_path: Path) -> None:
    uv = _uv()
    root = _scratch_tree(tmp_path)
    lock_dir = root / LOCK.parent

    before = subprocess.run(
        [uv, "lock", "--check"], cwd=lock_dir, capture_output=True, text=True
    )
    assert before.returncode == 0, (
        f"the committed lockfile is already stale, so this test proves nothing:\n"
        f"{before.stderr}"
    )

    bump = subprocess.run(
        ["make", "version", f"V={BUMP}", f"PYTHON={sys.executable}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert bump.returncode == 0, bump.stdout + bump.stderr

    pyproject = (lock_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{BUMP}"' in pyproject, "the bump did not reach pyproject.toml"

    after = subprocess.run(
        [uv, "lock", "--check"], cwd=lock_dir, capture_output=True, text=True
    )
    assert after.returncode == 0, (
        f"`make version V={BUMP}` left {LOCK} out of date with its pyproject.toml, "
        f"so `uv export --locked` (make lock-constraints) would refuse it:\n"
        f"{after.stderr}"
    )


@pytest.mark.unit
def test_make_version_changes_only_the_projects_own_lock_entry(
    tmp_path: Path,
) -> None:
    """The bump must not re-resolve: a dependency pin moving here would be a
    dependency change hidden inside a version commit."""
    root = _scratch_tree(tmp_path)
    original = (root / LOCK).read_text(encoding="utf-8").splitlines()

    bump = subprocess.run(
        ["make", "version", f"V={BUMP}", f"PYTHON={sys.executable}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert bump.returncode == 0, bump.stdout + bump.stderr

    updated = (root / LOCK).read_text(encoding="utf-8").splitlines()
    assert len(updated) == len(original)
    changed = [(a, b) for a, b in zip(original, updated, strict=True) if a != b]
    assert len(changed) == 1, changed
    assert changed[0][1] == f'version = "{BUMP}"'
