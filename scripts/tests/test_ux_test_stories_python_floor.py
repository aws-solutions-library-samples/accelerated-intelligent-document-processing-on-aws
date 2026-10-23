# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/python_floor.py and the host scripts that call it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import python_floor  # noqa: E402

GUARDED = (
    "scripts/ux_runner/stories.py",
    "scripts/ux_runner/changelog_select.py",
    "scripts/ux_runner/story_verdict.py",
    "scripts/ux_runner/story_report.py",
    "scripts/sdlc/ai_ux_test_stories.py",
)

#: Run in a child process, which reports itself as 3.9 and lacks what 3.9 lacks
#: (``tomllib`` and ``typing.TypeGuard``) before it runs the script with ``--help``.
AS_PYTHON_3_9 = """
import runpy, sys, typing
sys.version_info = (3, 9, 6, "final", 0)
sys.modules["tomllib"] = None
del typing.TypeGuard
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.unit
def test_this_python_meets_the_floor() -> None:
    python_floor.require("stories.py")


@pytest.mark.unit
def test_an_older_python_ends_with_one_failed_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(python_floor, "MINIMUM", (99, 0))
    with pytest.raises(SystemExit) as raised:
        python_floor.require("/anywhere/stories.py")
    message = raised.value.code
    assert isinstance(message, str)
    assert message.startswith(
        "FAILED: stories.py needs Python 99.0 or newer, and this is "
    )
    assert "\n" not in message
    assert "PYTHON=python3.12" in message
    assert "py on Windows" in message


@pytest.mark.unit
@pytest.mark.parametrize("script", GUARDED)
def test_each_host_script_refuses_before_an_import_an_older_python_lacks(
    script: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-c", AS_PYTHON_3_9, str(REPO_ROOT / script)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith(
        f"FAILED: {Path(script).name} needs Python 3.11 or newer, and this is "
    )


@pytest.mark.unit
def test_docs_index_still_runs_where_the_floor_would_refuse() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            AS_PYTHON_3_9,
            str(SCRIPTS / "ux_runner" / "docs_index.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: docs_index.py")
