# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one "is this CI?" rule, and that no suite under ``scripts/tests`` grows its own.

:mod:`ci_environment` exists because the suites here each spelled the rule as
``bool(os.getenv("CI") or ...)``, which reads ``CI=false`` as in CI. The first half of
this file pins the rule; the second half is the closure that keeps it the only one.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import ci_environment
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(ci_environment.__file__).resolve()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ci_environment.CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_nothing_set_is_not_ci(clean_env: pytest.MonkeyPatch) -> None:
    assert ci_environment.in_ci() is False


@pytest.mark.parametrize("name", ci_environment.CI_VARIABLES)
@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", " on "])
def test_each_variable_on_its_own_marks_ci(
    clean_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Each name counts by itself: a runner may set only its own."""
    clean_env.setenv(name, value)
    assert ci_environment.in_ci() is True


@pytest.mark.parametrize("name", ci_environment.CI_VARIABLES)
@pytest.mark.parametrize("value", ["", "0", "false", "False", "NO", " off ", "false\n"])
def test_an_explicit_negative_is_not_ci(
    clean_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """``CI=false`` is a request for a developer's behaviour, not a CI marker."""
    clean_env.setenv(name, value)
    assert ci_environment.in_ci() is False


def test_one_positive_outweighs_a_negative(clean_env: pytest.MonkeyPatch) -> None:
    """``CI=false`` does not cancel a platform's own variable."""
    clean_env.setenv("CI", "false")
    clean_env.setenv("GITLAB_CI", "true")
    assert ci_environment.in_ci() is True


def test_the_environment_is_read_at_call_time(clean_env: pytest.MonkeyPatch) -> None:
    assert ci_environment.in_ci() is False
    clean_env.setenv("CI", "true")
    assert ci_environment.in_ci() is True


def test_an_explicit_mapping_is_used_instead_of_the_process_environment(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("CI", "true")
    assert ci_environment.in_ci({}) is False
    assert ci_environment.in_ci({"GITHUB_ACTIONS": "true"}) is True


# --------------------------------------------------------------------------- #
# Closure: nothing else under scripts/tests reads a CI variable directly.
# --------------------------------------------------------------------------- #


def _suite_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=HELPER.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [HELPER.parent / path for path in listed]


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _direct_reads(tree: ast.AST) -> list[int]:
    """Lines reading a CI variable from the process environment other than via the helper.

    ``os.getenv(<name>)``, ``os.environ.get(<name>)`` and a load of
    ``os.environ[<name>]``, for each name in :data:`ci_environment.CI_VARIABLES`.
    Writes (``monkeypatch.setenv``, ``env["CI"] = ...`` on a copy) are not reads and
    are not matched.
    """
    names = set(ci_environment.CI_VARIABLES)
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and first.value in names):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value)
            ):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_os_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in names
        ):
            lines.append(node.lineno)
    return lines


def test_the_closure_detects_each_spelling_it_claims_to() -> None:
    """Non-vacuity: every shape :func:`_direct_reads` names is actually found."""
    source = (
        "import os\n"
        'a = bool(os.getenv("CI"))\n'
        'b = os.environ.get("GITLAB_CI", "")\n'
        'c = os.environ["GITHUB_ACTIONS"]\n'
        'os.environ["CI"] = "x"\n'
        'd = os.getenv("CI_JOB_ID")\n'
    )
    assert sorted(_direct_reads(ast.parse(source))) == [2, 3, 4]


def test_no_suite_reads_a_ci_variable_except_through_the_helper() -> None:
    files = _suite_files()
    assert any(path.name == "test_ux_recorder.py" for path in files), (
        "the file listing no longer reaches scripts/tests, so this check would pass "
        f"vacuously (listed {len(files)} file(s))"
    )
    found = []
    for path in files:
        if path.resolve() == HELPER or not path.is_file():
            continue
        for line in _direct_reads(ast.parse(path.read_text(), filename=str(path))):
            found.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not found, (
        f"these lines decide whether the run is CI by reading the environment "
        f"themselves: {found}. Use ci_environment.in_ci(), which is the one rule "
        f"here and the one that reads CI=false as not CI."
    )
