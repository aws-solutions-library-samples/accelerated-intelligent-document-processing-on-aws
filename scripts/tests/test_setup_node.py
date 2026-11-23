# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Node half of a development environment.

Two pieces: ``scripts/ui_deps_freshness.py``, which the UI gates use to tell a
``node_modules`` that does not match its lockfile from a current one, and
``scripts/setup_node.sh``, which ``make setup``, ``make setup-venv`` (``auto``) and
``make setup-node`` (``required``) run. The script is driven with a PATH holding
stand-in ``node``/``npm`` commands, so no test installs anything.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup_node.sh"


def _load():
    path = REPO_ROOT / "scripts" / "ui_deps_freshness.py"
    spec = importlib.util.spec_from_file_location("_ui_deps_freshness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FRESHNESS = _load()


# --------------------------------------------------------------------------- #
# ui_deps_freshness
# --------------------------------------------------------------------------- #
def _project(tmp_path: Path, wanted: dict, installed: dict | None) -> Path:
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": {"": {}, **wanted}}), encoding="utf-8"
    )
    if installed is not None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / ".package-lock.json").write_text(
            json.dumps({"packages": installed}), encoding="utf-8"
        )
    return tmp_path


def test_a_matching_install_is_current(tmp_path):
    pkgs = {"node_modules/a": {"version": "1.0.0"}}
    assert FRESHNESS.stale_dependencies(_project(tmp_path, pkgs, pkgs)) == []


def test_a_missing_package_is_stale(tmp_path):
    root = _project(tmp_path, {"node_modules/a": {"version": "1.0.0"}}, {})
    assert FRESHNESS.stale_dependencies(root) == ["node_modules/a"]


def test_a_package_at_another_version_is_stale(tmp_path):
    root = _project(
        tmp_path,
        {"node_modules/a": {"version": "2.0.0"}},
        {"node_modules/a": {"version": "1.0.0"}},
    )
    assert FRESHNESS.stale_dependencies(root) == ["node_modules/a"]


def test_an_absent_optional_package_is_not_stale(tmp_path):
    # npm leaves out the optional binaries built for other platforms.
    root = _project(
        tmp_path,
        {"node_modules/@esbuild/win32-x64": {"version": "0.1.0", "optional": True}},
        {},
    )
    assert FRESHNESS.stale_dependencies(root) == []


def test_node_modules_without_the_hidden_lockfile_is_stale(tmp_path):
    root = _project(tmp_path, {"node_modules/a": {"version": "1.0.0"}}, None)
    (root / "node_modules").mkdir()
    assert FRESHNESS.stale_dependencies(root) == ["node_modules/.package-lock.json"]


def test_no_node_modules_or_no_lockfile_cannot_be_judged(tmp_path):
    assert FRESHNESS.stale_dependencies(tmp_path) is None
    root = _project(tmp_path, {"node_modules/a": {"version": "1.0.0"}}, None)
    assert FRESHNESS.stale_dependencies(root) is None


def test_describe_names_a_few_and_counts_the_rest():
    stale = [f"node_modules/p{i}" for i in range(5)]
    line = FRESHNESS.describe("src/ui", stale)
    assert "src/ui/node_modules does not match src/ui/package-lock.json" in line
    assert "p0, p1, p2 and 2 more" in line


def test_the_real_ui_install_is_judged_current_when_it_was_just_installed():
    """Run against src/ui as it is: a fresh `npm ci` must not read as stale.

    This is the check that the optional-package rule is right for the real lockfile,
    which a synthetic tree cannot show. It only applies where node_modules exists.
    """
    ui = REPO_ROOT / "src" / "ui"
    hidden = ui / "node_modules" / ".package-lock.json"
    lock = ui / "package-lock.json"
    if not hidden.is_file() or hidden.stat().st_mtime < lock.stat().st_mtime:
        pytest.skip("src/ui/node_modules is absent or older than its lockfile")
    assert FRESHNESS.stale_dependencies(ui) == []


# --------------------------------------------------------------------------- #
# setup_node.sh
# --------------------------------------------------------------------------- #
def _bin(tmp_path: Path, node_version: str | None, npm_fails: bool = False) -> Path:
    """A directory of stand-in commands; `npm` records each call in calls.log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    if node_version is not None:
        (bindir / "node").write_text(f"#!/bin/sh\necho v{node_version}\n")
        status = 1 if npm_fails else 0
        (bindir / "npm").write_text(
            f'#!/bin/sh\n[ "$1" = --version ] && {{ echo 11.0.0; exit 0; }}\n'
            f'echo "$PWD $*" >> "{log}"\nexit {status}\n'
        )
    (bindir / "basedpyright").write_text("#!/bin/sh\n")
    for tool in bindir.iterdir():
        tool.chmod(0o755)
    return bindir


def _run(tmp_path: Path, mode: str, bindir: Path, **env: str):
    clean = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CI",
            "CODEBUILD_BUILD_ID",
            "SKIP_NODE_SETUP",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
        }
    }
    # The stand-ins first, then only the system directories (sh, sed, sort, flock),
    # so a real node or npm on the developer's PATH is never reached.
    clean["PATH"] = f"{bindir}:/usr/bin:/bin"
    clean.update(env)
    return subprocess.run(
        ["sh", str(SCRIPT), mode],
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.is_file() else []


@pytest.mark.parametrize("variable", ["CI", "CODEBUILD_BUILD_ID", "SKIP_NODE_SETUP"])
def test_auto_mode_installs_nothing_in_ci_or_when_told_not_to(tmp_path, variable):
    result = _run(tmp_path, "auto", _bin(tmp_path, "22.12.0"), **{variable: "1"})
    assert result.returncode == 0
    assert _calls(tmp_path) == []


def test_auto_mode_without_node_warns_and_succeeds(tmp_path):
    result = _run(tmp_path, "auto", _bin(tmp_path, None))
    assert result.returncode == 0
    assert "Node dependencies NOT installed" in result.stdout
    assert "make setup-node" in result.stdout


def test_required_mode_without_node_fails(tmp_path):
    result = _run(tmp_path, "required", _bin(tmp_path, None))
    assert result.returncode == 1
    assert "node is not on PATH" in result.stderr


@pytest.mark.parametrize("mode, code", [("auto", 0), ("required", 1)])
def test_an_old_node_is_refused(tmp_path, mode, code):
    result = _run(tmp_path, mode, _bin(tmp_path, "22.11.9"))
    assert result.returncode == code
    assert "older than the 22.12.0" in result.stdout + result.stderr
    assert _calls(tmp_path) == []


def test_a_new_enough_node_runs_npm_ci_in_both_projects(tmp_path):
    result = _run(tmp_path, "required", _bin(tmp_path, "24.1.0"))
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(tmp_path)
    assert any(c.startswith(f"{REPO_ROOT / 'src' / 'ui'} ci ") for c in calls), calls
    assert any(c.startswith(f"{REPO_ROOT / 'docs-site'} ci ") for c in calls), calls
    # basedpyright is on the stand-in PATH, so it is not installed again.
    assert not any("install -g" in c for c in calls), calls


@pytest.mark.parametrize("mode, code", [("auto", 0), ("required", 1)])
def test_a_failed_install_is_reported(tmp_path, mode, code):
    result = _run(tmp_path, mode, _bin(tmp_path, "22.12.0", npm_fails=True))
    assert result.returncode == code
    assert "install failed for: src/ui docs-site" in result.stdout + result.stderr
