# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The WSL setup guide installs Python packages only through ``make setup-venv``.

``make setup-venv`` installs every package against the export of
``lib/idp_common_pkg/uv.lock`` and checks afterwards that no first-party package came
from an index, which is what makes a local environment hold the versions CI tests. The
WSL guide, and the next steps ``scripts/setup/wsl_setup.sh`` prints, used plain
``pip install`` lines instead, so a WSL environment resolved its versions afresh
(#1747). WSL is Linux, so ``make`` is available and there is no reason to differ from
the Linux guide.

The rule is stated about the installer rather than about spellings of an install
command: no command the guide gives a reader to copy, and no line of the setup script,
invokes ``pip`` in any form (``pip``, ``pip3.12``, ``python -m pip``, ``uv pip``), with
or without options between it and ``install``. A word is a run of letters, digits and
``.+-``, so the apt package ``python3-pip`` is one word and is not ``pip``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "setup-development-env-WSL.md"
SETUP_SCRIPT = REPO / "scripts" / "setup" / "wsl_setup.sh"

_WORD = re.compile(r"[\w.+-]+")
_PIP = re.compile(r"pip[\d.]*")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _invokes_pip(text: str) -> bool:
    return any(_PIP.fullmatch(word) for word in _WORD.findall(text))


def _guide_code(markdown: str, inline: bool = True) -> list[str]:
    """Every fenced line and, with ``inline``, every inline code span: what a
    reader copies."""
    code, fence = [], None
    for line in markdown.splitlines():
        opener = _FENCE.match(line)
        if opener and fence in (None, opener.group(1)):
            fence = None if fence else opener.group(1)
            continue
        if fence:
            code.append(line)
        elif inline:
            code.extend(_INLINE_CODE.findall(line))
    return code


def test_the_guide_gives_no_pip_command():
    code = _guide_code(GUIDE.read_text(encoding="utf-8"))
    # Non-vacuity: the parse found the guide's commands.
    assert any("aws configure" in line for line in code)
    offending = [line for line in code if _invokes_pip(line)]
    assert not offending, (
        f"{GUIDE.relative_to(REPO)} installs with pip, not against the lock: "
        f"{offending}. Use `make setup-venv`."
    )
    # A step that runs it, not a mention of it in prose.
    fenced = _guide_code(GUIDE.read_text(encoding="utf-8"), inline=False)
    assert any(line.strip() == "make setup-venv" for line in fenced)


def test_the_guide_names_a_setup_script_that_exists():
    code = _guide_code(GUIDE.read_text(encoding="utf-8"))
    scripts = [
        line.strip() for line in code if re.match(r"\s*\./scripts/\S+\.sh", line)
    ]
    assert scripts, "the guide no longer runs a setup script; update this test"
    for script in scripts:
        assert (REPO / script.split()[0]).is_file(), f"{script} does not exist"


def test_the_setup_script_never_invokes_pip():
    lines = [
        line
        for line in SETUP_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    # Non-vacuity: the apt line naming python3-pip is read and is not pip.
    assert any("python3-pip" in line for line in lines)
    offending = [line for line in lines if _invokes_pip(line)]
    assert not offending, f"{SETUP_SCRIPT.relative_to(REPO)} invokes pip: {offending}"
    # The printed next step itself, not a mention of the target in another step.
    assert any(re.search(r':\s*make setup-venv"\s*$', line) for line in lines)
