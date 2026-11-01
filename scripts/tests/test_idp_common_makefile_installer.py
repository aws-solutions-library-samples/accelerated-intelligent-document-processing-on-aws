# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``lib/idp_common_pkg``'s install targets use the installer of ``$(PYTHON)``.

``test-unit-cicd`` (without ``SKIP_INSTALL=1``) and ``dev`` ran a bare ``pip
install``. A venv made by ``uv venv`` has no pip of its own, so in one that is
activated a bare ``pip`` is whichever pip comes next on ``PATH`` — on the machine
this was found on, the pip of a different, shared virtualenv — and the install
lands in another interpreter while pytest runs this one (#1707).

The recipe is read back through ``make --dry-run`` with a stand-in interpreter,
so what is asserted is the command make would run, not the Makefile's spelling.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "lib" / "idp_common_pkg"

pytestmark = pytest.mark.unit

INSTALLING_TARGETS = ("test-unit-cicd", "dev")


def _interpreter(tmp_path: Path, *, has_pip: bool) -> Path:
    """A stand-in for $(PYTHON) that answers only the `-m pip --version` probe."""
    name = "py-with-pip" if has_pip else "py-without-pip"
    path = tmp_path / name
    status = 0 if has_pip else 1
    path.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_line(target: str, python: Path) -> str:
    out = subprocess.run(
        ["make", "--dry-run", "--no-print-directory", "-C", str(PKG), target],
        env={"PATH": "/usr/bin:/bin", "PYTHON": str(python)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = [line for line in out.splitlines() if "install" in line]
    assert lines, f"`make -n {target}` printed no install command:\n{out}"
    return lines[0]


@pytest.mark.parametrize("target", INSTALLING_TARGETS)
def test_an_interpreter_with_pip_installs_through_its_own_pip(tmp_path, target):
    python = _interpreter(tmp_path, has_pip=True)
    assert f"{python} -m pip install -e" in _install_line(target, python)


@pytest.mark.parametrize("target", INSTALLING_TARGETS)
def test_an_interpreter_without_pip_installs_through_uv_aimed_at_it(tmp_path, target):
    python = _interpreter(tmp_path, has_pip=False)
    line = _install_line(target, python)
    assert f"uv pip install --python {python} -e" in line
    assert not re.search(r"(^|[\s;])pip install", line.replace("uv pip install", ""))


def test_no_recipe_in_the_package_makefile_calls_a_bare_pip():
    """A recipe line that starts a command with `pip` names no interpreter at all."""
    bare = [
        line.strip()
        for line in (PKG / "Makefile").read_text(encoding="utf-8").splitlines()
        if line.startswith("\t")
        and re.search(r"(^|[\s;@]|then\s)pip3?\s+install", line.strip())
        and "uv pip" not in line
    ]
    assert not bare, f"bare pip in lib/idp_common_pkg/Makefile recipes: {bare}"
