# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every pytest invocation is pinned to the checkout it was started from.

``import idp_common`` follows the editable-install pointer in the interpreter's
``site-packages``, not the checkout a suite lives in, so an unpinned run reports on
whichever tree last ran an install — and reports green while doing it, because the
package it imported is a real revision of this one (#1094). ``PYTHONPATH`` is what
makes the run describe the tree it was started from, and the point of the change this
file covers is that the pin is the **default** rather than something a developer has to
remember.

Three places compute it, because none of them can call the others: ``make`` (for every
pytest invocation in both Makefiles), ``scripts/first_party_paths.py`` (for
``scripts/run_all_tests.py``, which spawns one pytest per root) and
``scripts/tests/first_party_provenance.py`` (which ``conftest.py`` loads by path and
which therefore may not import from the tree). The rule they each implement is
``lib/*/pyproject.toml``. Asserting they agree — and that they agree with what
``FIRST_PARTY_EDITABLES`` installs — is what keeps three expressions of one rule from
drifting into three rules.

The central assertion is **measured, not parsed**: the wrapper `make` actually expands
is run, and the interpreter under it is asked where each first-party package came from.
A text assertion about the Makefile would keep passing if the pin were correct and
inert, which is the failure mode this repository keeps rediscovering.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
LIBRARY_MAKEFILE_DIR = REPO_ROOT / "lib" / "idp_common_pkg"
RUNNER = REPO_ROOT / "scripts" / "run_all_tests.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from first_party_paths import (  # noqa: E402
    checkout_pythonpath,
    first_party_roots,
    pinned_environment,
)

sys.path.pop(0)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import first_party_provenance as fpp  # noqa: E402

sys.path.pop(0)

pytestmark = pytest.mark.unit

#: Ceiling for the measured probes below, which import five packages under a wrapper.
_PROBE_TIMEOUT = 120


def _make_variable(name: str, *, directory: Path) -> str:
    """One variable's value, expanded by ``make`` reading the REAL Makefile.

    ``--eval`` adds a target to the makefile ``make`` would have read anyway, so this
    measures the same expansion a recipe gets — including the two different relative
    include paths the root and library Makefiles use, which is the part a text parse
    cannot check.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "make",
            "--no-print-directory",  # `make -C` narrates on stdout, ahead of the value
            "-C",
            str(directory),
            "--eval",
            # Single-quoted deliberately: the value ends in a shell parameter
            # expansion that keeps the caller's own pin, and a double-quoted printf
            # would have make's own shell resolve it here — against this process's
            # environment, which is not the one a recipe runs in.
            f"__probe__:\n\t@printf '%s' '$({name})'",
            "__probe__",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"could not read $({name}) from the makefile in {directory}: {result.stderr}"
    )
    return result.stdout.strip()


def _editable_paths() -> list[Path]:
    """The package directories ``FIRST_PARTY_EDITABLES`` installs.

    Parsed as text on purpose: that variable is the authority on what is installed,
    and reading it here is what ties the pin to the install rather than to a second
    list that happens to agree today.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    block = text.split("FIRST_PARTY_EDITABLES", 1)[1].split("\n\n", 1)[0]
    return sorted(
        REPO_ROOT / spec for spec in re.findall(r'-e\s+"?([^"\[\s\\]+)', block)
    )


def _run_under_wrapper(code: str, env: dict[str, str] | None = None) -> str:
    """Run ``python -c code`` under the wrapper ``make`` expands, through a shell.

    A shell is needed because the wrapper's ``PYTHONPATH`` value ends in a parameter
    expansion that keeps the caller's own pin (``$${PYTHONPATH:+:$$PYTHONPATH}``);
    running the argv directly would pass that text through uninterpreted and measure
    something no recipe ever does.

    ⚠️ ``PYTHONPATH`` is stripped from the inherited environment unless the caller
    supplies one. Without that, this probe measures **the environment the test runner
    was started in**: anyone running this suite the documented way has the five roots
    exported already, so the child inherits a correct answer and the probe passes with
    the pin deleted from the makefile entirely. Measured: that mutation is invisible
    with the ambient variable left in place.
    """
    if env is None:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    wrapper = _make_variable("PYTEST_HERMETIC", directory=REPO_ROOT)
    assert wrapper.endswith("-m pytest"), wrapper
    command = f"{wrapper[: -len('-m pytest')]} -c {_quote(code)}"
    result = subprocess.run(  # noqa: S602 - the command is built from our own makefile
        command,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=env,
        timeout=_PROBE_TIMEOUT,
    )
    assert result.returncode == 0, f"{command}\n{result.stderr}"
    return result.stdout.strip()


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# one rule, three expressions of it
# --------------------------------------------------------------------------- #
def test_the_rule_finds_the_packages_this_checkout_has() -> None:
    """A floor: an empty answer would make every assertion below vacuously true."""
    roots = first_party_roots(REPO_ROOT)
    assert roots, "no lib/*/pyproject.toml found, so nothing would be pinned"
    assert all(root.is_dir() for root in roots)


def test_make_python_and_the_provenance_guard_agree() -> None:
    from_make = _make_variable("FIRST_PARTY_PYTHONPATH", directory=REPO_ROOT)
    from_python = checkout_pythonpath(REPO_ROOT)
    from_guard = os.pathsep.join(str(p) for p in fpp.first_party_roots(REPO_ROOT))
    assert from_make == from_python == from_guard, (
        "the three implementations of 'which roots get pinned' disagree:\n"
        f"  make/hermetic_aws.mk      : {from_make}\n"
        f"  scripts/first_party_paths : {from_python}\n"
        f"  first_party_provenance    : {from_guard}"
    )


def test_the_pin_matches_what_first_party_editables_installs() -> None:
    """The pin and the install are the same set, or one of them is wrong.

    A package added under ``lib/`` and left out of ``FIRST_PARTY_EDITABLES`` is not
    installed, and one installed from elsewhere is not pinned. Either way the next
    person measures a tree they did not choose.
    """
    assert first_party_roots(REPO_ROOT) == _editable_paths()


def test_the_pin_is_absolute() -> None:
    """A relative entry is dropped by any subprocess started in another directory."""
    value = _make_variable("FIRST_PARTY_PYTHONPATH", directory=REPO_ROOT)
    assert value
    assert all(Path(entry).is_absolute() for entry in value.split(os.pathsep))


def test_both_makefiles_pin_the_same_checkout() -> None:
    """CI runs the library's own targets with ``make -C``, from a different directory.

    The value is resolved from the shared makefile's own path rather than from the
    working directory, so the two include sites have to produce the same answer.
    """
    assert _make_variable("FIRST_PARTY_PYTHONPATH", directory=LIBRARY_MAKEFILE_DIR) == (
        _make_variable("FIRST_PARTY_PYTHONPATH", directory=REPO_ROOT)
    )


# --------------------------------------------------------------------------- #
# measured: what the wrapper actually does to an interpreter
# --------------------------------------------------------------------------- #
def test_every_first_party_package_resolves_in_this_checkout_under_the_wrapper() -> (
    None
):
    """The assertion that matters, and the only one a correct-but-inert pin fails.

    Each root's package is imported under the wrapper the recipes use and asked for its
    ``__file__``. On the machine this was written on, without the pin, four of the five
    answered from a different project and one from another worktree of this repository.
    """
    packages = sorted(
        entry.name
        for root in first_party_roots(REPO_ROOT)
        for entry in root.iterdir()
        if (entry / "__init__.py").is_file()
    )
    assert packages, "no importable package found under the first-party roots"
    code = (
        "import importlib\n"
        f"for name in {packages!r}:\n"
        "    print(name, importlib.import_module(name).__file__)\n"
    )
    for line in _run_under_wrapper(code).splitlines():
        name, _, origin = line.partition(" ")
        assert Path(origin).resolve().is_relative_to(REPO_ROOT), (
            f"under the pinned wrapper, {name} still resolved to {origin}, outside "
            f"{REPO_ROOT}"
        )


def test_the_wrapper_keeps_a_pin_the_caller_set_as_well() -> None:
    """Ours first, theirs after: the checkout under test wins without clobbering."""
    sentinel = "/tmp/a-path-the-caller-cares-about"  # noqa: S108 - a string, not a file
    env = dict(os.environ, PYTHONPATH=sentinel)
    value = _run_under_wrapper("import os; print(os.environ['PYTHONPATH'])", env=env)
    entries = value.split(os.pathsep)
    assert entries[-1] == sentinel
    assert entries[: len(first_party_roots(REPO_ROOT))] == [
        str(p) for p in first_party_roots(REPO_ROOT)
    ]


def test_suppressing_the_pin_leaves_pythonpath_unset_rather_than_empty() -> None:
    """``FIRST_PARTY_PYTHONPATH=`` is the deliberate-installed-copy escape.

    It has to leave the variable absent, not empty: an empty ``PYTHONPATH`` entry is
    the working directory, so the escape would quietly add a path of its own.
    """
    wrapper = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "make",
            "--no-print-directory",
            "-C",
            str(REPO_ROOT),
            "FIRST_PARTY_PYTHONPATH=",
            "--eval",
            "__probe__:\n\t@printf '%s' '$(PYTEST_HERMETIC)'",
            "__probe__",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrapper.returncode == 0, wrapper.stderr
    assert "PYTHONPATH=" not in wrapper.stdout, wrapper.stdout


# --------------------------------------------------------------------------- #
# the auto-discovering runner, which spawns its own pytest per root
# --------------------------------------------------------------------------- #
def test_pinned_environment_prepends_without_dropping_anything() -> None:
    env = pinned_environment(REPO_ROOT, {"PYTHONPATH": "/keep/me", "OTHER": "kept"})
    assert env["OTHER"] == "kept"
    assert env["PYTHONPATH"].endswith(f"{os.pathsep}/keep/me")
    assert env["PYTHONPATH"].startswith(str(first_party_roots(REPO_ROOT)[0]))


def test_pinned_environment_does_not_mutate_the_caller_s_environment() -> None:
    before = dict(os.environ)
    pinned_environment(REPO_ROOT)
    assert dict(os.environ) == before


def test_the_runner_passes_the_pinned_environment_to_every_child() -> None:
    """An ``ast`` assertion, because the behavioural form is the whole gate.

    ``run_all_tests.py`` spawns one pytest per registered root — some forty of them,
    several minutes — and it takes no argument that would narrow the set, so measuring
    this by running it would mean running the entire test gate inside one test. What is
    measured instead is the call site: the ``subprocess.run`` that launches a root must
    pass ``env=`` a value derived from ``pinned_environment``, since an omitted ``env``
    inherits the ambient one and is exactly the defect.
    """
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    pinned_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "pinned_environment"
    }
    assert pinned_names, (
        "scripts/run_all_tests.py never calls pinned_environment, so the pytest "
        "subprocesses it starts inherit whatever the editable-install pointer says"
    )

    def _is_pytest_launch(node: ast.AST) -> bool:
        """A ``subprocess.run`` whose argv literally names ``pytest``.

        Narrow on purpose: the module also shells out to ``git``, and requiring an
        ``env=`` on that call would be asserting something this test has no business
        asserting.
        """
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            return False
        return any(
            isinstance(element, ast.Constant) and element.value == "pytest"
            for element in node.args[0].elts
        )

    launches = [node for node in ast.walk(tree) if _is_pytest_launch(node)]
    assert launches, "no subprocess.run([... 'pytest' ...]) found to check"
    for call in launches:
        env_kwargs = [kw for kw in call.keywords if kw.arg == "env"]
        assert env_kwargs, (
            f"the subprocess.run at line {call.lineno} passes no env=, so that child "
            "inherits the ambient PYTHONPATH"
        )
        assert any(
            isinstance(kw.value, ast.Name) and kw.value.id in pinned_names
            for kw in env_kwargs
        ), (
            f"the subprocess.run at line {call.lineno} passes an env= that does not "
            f"come from pinned_environment (expected one of {sorted(pinned_names)})"
        )
