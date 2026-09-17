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
instead. `exclude` is checked the same way — a stale exclude silently stops
excluding, which is the less harmful direction but still a lie about intent.

Deliberately NOT asserted here: `typeCheckingMode` (currently "basic") or the
13 diagnostic rules pinned to "none". Tightening those is a separate decision
with a much larger diff; this file only guarantees that whatever strictness is
configured is actually applied to the files it claims to cover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYRIGHT_CONFIG = REPO_ROOT / "pyrightconfig.json"

pytestmark = pytest.mark.unit


def _config() -> dict:
    return json.loads(PYRIGHT_CONFIG.read_text())


def _include_paths() -> list[str]:
    return list(_config().get("include", []))


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


def test_first_party_python_packages_are_covered() -> None:
    """Pin the specific regression: the two shipped Python libraries and the
    parent-stack Lambda sources must each fall under some `include` entry.

    Without this, correcting the CLI path today does not stop the next move
    from dropping it again — the entry would simply be deleted rather than
    left dangling, and every other test in this file would still pass.
    """
    must_be_covered = [
        "lib/idp_common_pkg/idp_common",
        "lib/idp_cli_pkg/idp_cli",
        "src/lambda",
    ]
    includes = _include_paths()
    for rel in must_be_covered:
        covered = any(
            rel == inc or rel.startswith(inc.rstrip("/") + "/") for inc in includes
        )
        assert covered, (
            f"{rel!r} is not covered by any pyrightconfig.json `include` entry "
            f"(current entries: {includes}). It is a shipped first-party source "
            "tree and must be type-checked."
        )
