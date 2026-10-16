# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the command a ``make`` target would run, and run a probe through it.

Two test modules assert things about what ``lib/idp_common_pkg``'s pytest recipes
hand the interpreter — one about the AWS environment they remove
(``test_offline_suites_are_hermetic.py``), one about the ``PYTHONPATH`` they pin
(``test_first_party_pythonpath.py``). Both questions are about the *expanded*
command rather than about the text of the recipe, and that distinction is the
reason this helper exists rather than another substring search.

A recipe line is a spelling. ``$(PYTEST_FIRST_PARTY) -m "integration"`` and a
hand-written ``env PYTHONPATH=lib/idp_common_pkg python3 -m pytest`` read
identically to a check that looks for the wrapper's name, and only one of them
pins the checkout. ``make --dry-run`` answers the question the assertion is
actually about: what will run. Then :func:`probe_command` keeps everything up to
the ``-m pytest`` and substitutes an interpreter one-liner for the suite, so the
*environment* that command establishes can be measured without running the tests
it would run.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

#: The spelling both Makefiles use to start pytest. :func:`probe_command` splits
#: the command here, so a recipe that invoked the ``pytest`` console script
#: instead would reach a loud assertion rather than a silently skipped probe.
PYTEST_MARKER = "-m pytest"


def dry_run_lines(
    target: str, directory: Path, *, allow_failure: bool = False
) -> list[str]:
    """Every command ``make <target>`` would run, as ``make`` itself expands it.

    ``--dry-run`` prints the recipe without executing it, which matters here: the
    targets this is used on run test suites, one of them against real AWS.

    ``allow_failure`` returns an empty list instead of asserting, for the sweep
    that dry-runs every target in a makefile to find the ones that run pytest —
    where a target that cannot be dry-run is not a finding.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["make", "--no-print-directory", "--dry-run", target],
        cwd=str(directory),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if allow_failure:
            return []
        raise AssertionError(
            f"`make --dry-run {target}` failed in {directory}: {result.stderr}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def pytest_lines(lines: list[str]) -> list[str]:
    """The subset of ``lines`` that start pytest."""
    return [line for line in lines if PYTEST_MARKER in line]


def targets(makefile: Path) -> list[str]:
    """Every ordinary target name a makefile defines, in file order.

    Names beginning with ``.`` (``.PHONY`` and friends) are directives rather than
    targets, and a line with no tab-indented body is a dependency declaration; both
    are harmless to dry-run and are dropped here so the sweep reads cleanly.
    """
    found: list[str] = []
    for line in makefile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*):(?!=)", line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def recipe_text(makefile: Path, target: str) -> str:
    """The tab-indented body of ``target``, as written — not expanded."""
    lines = makefile.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if re.match(rf"^{re.escape(target)}:(?!=)", line):
            collecting = True
            continue
        if collecting:
            if line.startswith("\t"):
                body.append(line)
            elif line.strip():
                break
    return "\n".join(body)


def dry_run_command(target: str, directory: Path) -> str:
    """The one pytest command ``make <target>`` would run.

    Asserts there is exactly one, so a recipe that grew a second pytest
    invocation is a deliberate edit here rather than a silently unprobed half.
    """
    found = pytest_lines(dry_run_lines(target, directory))
    assert len(found) == 1, (
        f"`make --dry-run {target}` in {directory} contains {len(found)} commands "
        f"with {PYTEST_MARKER!r}, expected exactly one: {found}"
    )
    return found[0]


def probe_command(command: str, code: str) -> str:
    """``command`` with its pytest invocation replaced by ``python -c code``.

    Everything before ``-m pytest`` is the part under test — the ``env``
    manipulation, the variable assignments and the interpreter — and it is kept
    verbatim, including shell parameter expansions, which is why callers run the
    result through ``sh -c`` rather than as an argv.
    """
    assert PYTEST_MARKER in command, (
        f"the recipe's command does not contain {PYTEST_MARKER!r}, so this probe "
        f"cannot tell which part of it is the environment and which is the suite:\n"
        f"  {command}\n"
        "If the recipe now starts pytest some other way, update PYTEST_MARKER "
        "deliberately — do not delete the assertion, which is what this failing "
        "loudly instead of skipping is for."
    )
    prefix = command[: command.index(PYTEST_MARKER)]
    return f"{prefix} -c {shlex.quote(code)}"


def run_probe(
    command: str, code: str, *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run :func:`probe_command` under ``sh``, returning the completed process."""
    return subprocess.run(  # noqa: S603 - argv is fixed; the text comes from our makefile
        ["sh", "-c", probe_command(command, code)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env=env,
        timeout=120,
    )
