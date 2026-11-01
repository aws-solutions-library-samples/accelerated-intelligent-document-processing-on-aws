# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The WSL setup guide installs Python packages through ``make setup-venv``.

``make setup-venv`` installs every package against the export of
``lib/idp_common_pkg/uv.lock`` and checks afterwards that no first-party package came
from an index, which is what makes a local environment hold the versions CI tests. The
WSL guide, and the next steps ``scripts/setup/wsl_setup.sh`` prints, used plain
``pip install`` lines instead, so a WSL environment resolved its versions afresh
(#1747). WSL is Linux, so ``make`` is available and there is no reason to differ from
the Linux guide.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "setup-development-env-WSL.md"
SETUP_SCRIPT = REPO / "scripts" / "setup" / "wsl_setup.sh"

# A package install by pip in any spelling a reader would copy; `uv pip` included.
_PIP_INSTALL = re.compile(r"\b(?:pip3?|python3?\s+-m\s+pip|uv\s+pip)\s+install\b")


def _fenced_lines(markdown: str) -> list[str]:
    lines, inside = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def test_the_guide_installs_nothing_with_pip():
    commands = _fenced_lines(GUIDE.read_text(encoding="utf-8"))
    # Non-vacuity: the fence parse found the guide's commands.
    assert any("aws configure" in line for line in commands)
    offending = [line for line in commands if _PIP_INSTALL.search(line)]
    assert not offending, (
        f"{GUIDE.relative_to(REPO)} installs with pip, not against the lock: "
        f"{offending}. Use `make setup-venv`."
    )
    assert any(line.strip() == "make setup-venv" for line in commands)


def test_the_guide_names_a_setup_script_that_exists():
    commands = _fenced_lines(GUIDE.read_text(encoding="utf-8"))
    scripts = [
        line.strip() for line in commands if re.match(r"\s*\./scripts/\S+\.sh", line)
    ]
    assert scripts, "the guide no longer runs a setup script; update this test"
    for script in scripts:
        assert (REPO / script.split()[0]).is_file(), f"{script} does not exist"


def test_the_setup_scripts_next_steps_install_nothing_with_pip():
    echoed = [
        line
        for line in SETUP_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("echo")
    ]
    assert echoed
    offending = [line for line in echoed if _PIP_INSTALL.search(line)]
    assert not offending, f"{SETUP_SCRIPT.relative_to(REPO)} prints {offending}"
    assert any("make setup-venv" in line for line in echoed)
