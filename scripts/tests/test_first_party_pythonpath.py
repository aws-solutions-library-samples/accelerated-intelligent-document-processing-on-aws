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

⚠️ **"Every" is a claim about a universe, and it has to be derived or it is a claim
about the invocations somebody remembered.** It was the latter: ``make
test-integration`` was a bare ``$(PYTHON) -m pytest``, correct about needing real AWS
credentials and dropping the pin along with the stripping, and nothing here looked
at it, because the pin was asserted of the *wrapper* and the wrapper was assumed of
the recipes (#1365). Three things now establish the "every":

* a **text closure** over the recipe lines of both makefiles — every line that
  starts a Python interpreter must name ``$(PYTEST_HERMETIC)`` or
  ``$(PYTEST_FIRST_PARTY)``, the two wrappers that apply the pin;
* an **expansion-derived sweep** of ``lib/idp_common_pkg/Makefile``, which
  dry-runs every target it defines and keeps the commands that start pytest, so a
  target added there is covered with no dependence on how its line is spelled;
* a **measured probe per command** from that sweep: the command is read back from
  ``make --dry-run``, and the interpreter under it must resolve every first-party
  package inside this checkout and must not discard a ``PYTHONPATH`` its caller
  set. A hand-rolled ``env PYTHONPATH=lib/idp_common_pkg`` fails there exactly as
  no pin at all does.

Which of the two wrappers a given recipe *should* use is
``scripts/tests/test_offline_suites_are_hermetic.py``'s question: one of them also
strips the AWS environment, and exactly one target must not have that.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
LIBRARY_MAKEFILE_DIR = REPO_ROOT / "lib" / "idp_common_pkg"
LIBRARY_MAKEFILE = LIBRARY_MAKEFILE_DIR / "Makefile"
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
import make_recipes  # noqa: E402

sys.path.pop(0)

pytestmark = pytest.mark.unit

#: Ceiling for the measured probes below, which import five packages under a wrapper.
_PROBE_TIMEOUT = 120


@contextlib.contextmanager
def _probe_makefile(body: str):
    """A throwaway makefile holding `body`, yielded as an absolute path.

    ⚠️ **A second `-f` rather than `--eval`, and the reason is portability.**
    `--eval` arrived in GNU Make **3.82**, and macOS ships **3.81** — the last
    GPLv2 release — so every probe in this module used to fail there with
    `unrecognized option` and a usage dump. Six tests red on every Mac with a
    stock toolchain, green in CI on Linux, for a reason nothing to do with this
    repository.

    `make -f Makefile -f <probe>` is accepted by 3.81 and does the same job:
    once any `-f` is given make stops reading the default makefile, so the real
    one is named explicitly and the probe target is appended to it. The
    expansion measured is therefore still the one a *recipe* gets, which is the
    whole point of this helper, and it is measured through the same `make` the
    recipes themselves run under rather than through a newer one installed
    alongside it.

    The path must be absolute because `-C` changes directory first, and a
    relative `-f` would then resolve against the wrong place.

    ⚠️ **One thing differs from `--eval`: the probe appears in
    `MAKEFILE_LIST`.** Every consumer of that variable in these makefiles is
    immediate (`FIRST_PARTY_CHECKOUT :=` in `make/hermetic_aws.mk`, and the
    library Makefile's `include $(dir $(lastword $(MAKEFILE_LIST)))…`), so all
    of them are evaluated before the probe is read and the measured values are
    unaffected -- checked against `--eval` for every variable probed here, in
    both call-site directories, including the command-line override, and all
    identical. A *lazily* expanded consumer would see the probe and diverge, so
    do not extend this helper to one without re-measuring.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mk", prefix="idp-probe-", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _make_variable(name: str, *, directory: Path) -> str:
    """One variable's value, expanded by ``make`` reading the REAL Makefile.

    The probe target is appended as a second makefile, so this measures the same
    expansion a recipe gets — including the two different relative include paths
    the root and library Makefiles use, which is the part a text parse cannot
    check. See `_probe_makefile` for why not ``--eval``.
    """
    # Single-quoted deliberately: the value ends in a shell parameter expansion
    # that keeps the caller's own pin, and a double-quoted printf would have
    # make's own shell resolve it here — against this process's environment,
    # which is not the one a recipe runs in.
    with _probe_makefile(f"__probe__:\n\t@printf '%s' '$({name})'") as probe:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "make",
                # `make -C` narrates on stdout, ahead of the value, and that
                # would be read as part of it.
                #
                # ⚠️ Removing this does not fail on a stock macOS run, and the
                # reason is the make *version*: `-C` implies
                # `--print-directory` in GNU Make 4.x but not in 3.81. So the
                # mutation that proves this flag is needed has to be run under a
                # 4.x `make` — measured there, dropping it fails five tests.
                # `MAKELEVEL`, which is inherited because every pytest run here
                # is a child of a make recipe, only changes the banner from
                # `make:` to `make[1]:`; it is not what makes the flag
                # necessary, and 3.81 does not narrate even with it set.
                #
                # Load-bearing wherever `make` is 4.x: both CIs, a Linux dev
                # box, and any Mac whose PATH puts Homebrew's make first.
                "--no-print-directory",
                "-C",
                str(directory),
                "-f",
                "Makefile",
                "-f",
                str(probe),
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
    """Run ``python -c code`` under the wrapper ``make`` expands, through ``sh``.

    A shell is needed because the wrapper's ``PYTHONPATH`` value ends in a parameter
    expansion that keeps the caller's own pin (``$${PYTHONPATH:+:$$PYTHONPATH}``);
    running the argv directly would pass that text through uninterpreted and measure
    something no recipe ever does. ``sh -c`` is named explicitly rather than reached
    through ``shell=True``, which is both the spelling a `make` recipe itself uses and
    the one the security scan does not rate as a high-severity finding.

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
    result = subprocess.run(  # noqa: S603 - fixed argv; the text comes from our makefile
        ["sh", "-c", command],
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
def _importable_first_party_packages() -> list[str]:
    """Every importable package under the first-party roots, from the filesystem."""
    packages = sorted(
        entry.name
        for root in first_party_roots(REPO_ROOT)
        for entry in root.iterdir()
        if (entry / "__init__.py").is_file()
    )
    assert packages, "no importable package found under the first-party roots"
    return packages


def _provenance_probe(packages: list[str]) -> str:
    return (
        "import importlib\n"
        f"for name in {packages!r}:\n"
        "    print(name, importlib.import_module(name).__file__)\n"
    )


def _assert_every_origin_is_in_this_checkout(output: str, *, how: str) -> None:
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines, f"the provenance probe printed nothing {how}"
    for line in lines:
        name, _, origin = line.partition(" ")
        assert Path(origin).resolve().is_relative_to(REPO_ROOT), (
            f"{how}, {name} resolved to {origin}, outside {REPO_ROOT}"
        )


def test_every_first_party_package_resolves_in_this_checkout_under_the_wrapper() -> (
    None
):
    """The assertion that matters, and the only one a correct-but-inert pin fails.

    Each root's package is imported under the wrapper the recipes use and asked for its
    ``__file__``. On the machine this was written on, without the pin, four of the five
    answered from a different project and one from another worktree of this repository.
    """
    packages = _importable_first_party_packages()
    _assert_every_origin_is_in_this_checkout(
        _run_under_wrapper(_provenance_probe(packages)),
        how="under the pinned wrapper",
    )


#: What makes a recipe line an interpreter invocation. Deliberately not the word
#: "pytest": a wrapped line says ``$(PYTEST_HERMETIC)`` and never says it, and an
#: unwrapped one could avoid it too — ``$(PYTHON) -m $(PYTEST_MOD)`` runs pytest and
#: contains neither — so all three shapes are matched. ``rm -rf .pytest_cache`` is
#: not one, which is why the bare word needs a leading boundary.
_INTERPRETER_LINE = re.compile(
    r"\$\(PYTEST_HERMETIC\)|\$\(PYTEST_FIRST_PARTY\)|\$\(PYTHON\)\s+-m\s"
    r"|(?:^|\s)pytest\s"
)

#: The two wrappers, either of which applies the pin. One also strips the AWS
#: environment; which of the two a recipe should use is
#: ``test_offline_suites_are_hermetic.py``'s question, not this module's.
_PINNING_WRAPPERS = ("$(PYTEST_HERMETIC)", "$(PYTEST_FIRST_PARTY)")


def _recipe_interpreter_lines(makefile: Path) -> list[str]:
    """Runnable recipe lines in ``makefile`` that start a Python interpreter."""
    found: list[str] = []
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if not line.startswith("\t"):
            continue
        body = line.lstrip("\t").lstrip("@").strip()
        if body.startswith("#") or body.startswith("echo"):
            continue
        if _INTERPRETER_LINE.search(body):
            found.append(body)
    return found


@pytest.mark.parametrize("makefile", [MAKEFILE, LIBRARY_MAKEFILE_DIR / "Makefile"])
def test_every_pytest_recipe_line_goes_through_a_pinning_wrapper(
    makefile: Path,
) -> None:
    """The universe, so fixing one target is not mistaken for fixing the shape.

    ``make test-integration`` was a bare ``$(PYTHON) -m pytest``, and the reason it
    could be is that nothing asked the question of *every* recipe: the pin was
    asserted for the wrapper and the wrapper was assumed. A **new** unpinned target
    in either makefile is caught here, and the two probes below then establish that
    the wrapper a line names really does pin.

    This half is a text closure over the recipe lines, and that is a deliberate
    split rather than an oversight. For ``lib/idp_common_pkg/Makefile`` the
    universe is also derived by *expansion* — see :func:`_library_pytest_commands`,
    which dry-runs every target — and that is the stronger form, with no dependence
    on how a line is spelled. The root ``Makefile`` is text-level because
    dry-running its whole target list would mean dry-running the publish and deploy
    targets, which is neither cheap nor obviously safe. What the text form cannot
    see is an invocation whose line names neither wrapper, nor ``$(PYTHON) -m``,
    nor ``pytest``; no such spelling exists in either file today.
    """
    lines = _recipe_interpreter_lines(makefile)
    assert lines, (
        f"no interpreter recipe line found in {makefile.relative_to(REPO_ROOT)}, so "
        "this closure is asserting nothing. Either the suites moved or "
        "_INTERPRETER_LINE no longer matches how they are written."
    )
    unwrapped = [
        line for line in lines if not any(w in line for w in _PINNING_WRAPPERS)
    ]
    assert not unwrapped, (
        f"these {makefile.relative_to(REPO_ROOT)} recipe lines run pytest without "
        f"going through {' or '.join(_PINNING_WRAPPERS)}:\n  "
        + "\n  ".join(unwrapped)
        + "\n\nBoth apply an absolute PYTHONPATH naming this checkout's lib/* "
        "roots; without one the suite reports on whichever tree the editable "
        "install last pointed at, and reports green while doing it (#1094). Use "
        "$(PYTEST_HERMETIC) for an offline suite and $(PYTEST_FIRST_PARTY) for one "
        "that needs real AWS credentials (#1365)."
    )


@functools.lru_cache(maxsize=1)
def _library_pytest_commands() -> dict[str, str]:
    """``command → the targets that run it``, for every pytest recipe in the library.

    **Derived from the expansion, not from the text**, which is what makes this a
    universe rather than a list. ``make --dry-run`` is run for every target the
    makefile defines and the ones whose commands start pytest are kept, so a target
    added later is covered without being named here, and one that reaches pytest
    through a variable with no ``pytest`` in its name is covered too.

    A target that cannot be dry-run is a loud failure rather than a silent skip:
    the gap a sweep like this can have is a member it quietly could not read.
    """
    by_command: dict[str, list[str]] = {}
    for target in make_recipes.targets(LIBRARY_MAKEFILE):
        lines = make_recipes.dry_run_lines(target, LIBRARY_MAKEFILE_DIR)
        for command in make_recipes.pytest_lines(lines):
            by_command.setdefault(command, []).append(target)
    return {command: ", ".join(names) for command, names in by_command.items()}


def _pytest_command_cases() -> list[tuple[str, str]]:
    return sorted(_library_pytest_commands().items(), key=lambda pair: pair[1])


def test_the_library_makefile_has_pytest_recipes_to_check() -> None:
    """A floor: an empty sweep would make the two probes below collect nothing.

    An empty parametrization is reported as *no tests*, not as a failure, so the
    whole guarantee below would go quiet without anything turning red.
    """
    commands = _library_pytest_commands()
    assert commands, (
        f"no target in {LIBRARY_MAKEFILE.relative_to(REPO_ROOT)} expands to a "
        "pytest command, so the pin is being asserted over nothing. Either the "
        "suites moved or `make --dry-run` is no longer readable here."
    )


@pytest.mark.parametrize(
    ("command", "targets"),
    _pytest_command_cases(),
    ids=[t for _, t in _pytest_command_cases()],
)
def test_every_pytest_recipe_in_the_library_pins_this_checkout(
    command: str, targets: str
) -> None:
    """Every pytest recipe in ``lib/idp_common_pkg``, not the two that were wrong.

    #1365 named one target, ``test-integration``, which had been written as a bare
    ``$(PYTHON) -m pytest``: right about needing real credentials, and dropping the
    ``PYTHONPATH`` pin along with the stripping, so the suite that talks to real
    infrastructure was the one free to measure whichever checkout
    ``site-packages`` pointed at. Measured before the fix, from
    ``lib/idp_common_pkg`` with no ambient ``PYTHONPATH``: ``idp_sdk`` and
    ``idp_cli`` resolved out of another checkout of this repository.

    Fixing the instance is not fixing the shape. A **new** unpinned target added
    to the same file — ``test-integration-live: $(PYTHON) -m pytest -m
    "integration" --live`` — was caught by nothing, so the universe here is derived
    from the makefile's own targets rather than named.

    What is asserted is the **capability**, not the spelling: the command is read
    back from ``make --dry-run``, so a recipe that hand-rolled its own ``env
    PYTHONPATH=lib/...`` — relative, or naming only one root — fails here exactly
    as a recipe with no pin at all does, while a rename of ``$(PYTEST_HERMETIC)``
    or ``$(PYTEST_FIRST_PARTY)`` does not fail here for the wrong reason.

    ⚠️ The ambient ``PYTHONPATH`` is stripped from the probe's environment. Anyone
    running this suite the documented way has the five roots exported already, and
    the child would inherit a correct answer from its caller with the pin deleted
    from the makefile entirely.
    """
    packages = _importable_first_party_packages()
    result = make_recipes.run_probe(
        command,
        _provenance_probe(packages),
        cwd=LIBRARY_MAKEFILE_DIR,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert result.returncode == 0, (
        f"the provenance probe failed to run under the command `make {targets}` "
        f"runs (`{command}`):\n{result.stderr}"
    )
    _assert_every_origin_is_in_this_checkout(
        result.stdout, how=f"under the command `make {targets}` runs (`{command}`)"
    )


@pytest.mark.parametrize(
    ("command", "targets"),
    _pytest_command_cases(),
    ids=[t for _, t in _pytest_command_cases()],
)
def test_every_pytest_recipe_keeps_a_pin_the_caller_set(
    command: str, targets: str
) -> None:
    """Ours first, theirs after — for each wrapper, not for one of them.

    ``test_the_wrapper_keeps_a_pin_the_caller_set_as_well`` asserts this of
    ``$(PYTEST_HERMETIC)``. Measured: dropping the ``$${PYTHONPATH:+…}`` suffix
    from ``PYTEST_FIRST_PARTY`` **alone** makes ``make test-integration`` discard a
    developer's own ``PYTHONPATH`` — contradicting the comment two lines above it —
    with every other test in this module green, because the shared property was
    only ever probed through the other wrapper.
    """
    sentinel = str(REPO_ROOT / "a-path-the-caller-set")
    result = make_recipes.run_probe(
        command,
        "import os; print(os.environ.get('PYTHONPATH', '<unset>'))",
        cwd=LIBRARY_MAKEFILE_DIR,
        env=dict(os.environ, PYTHONPATH=sentinel),
    )
    assert result.returncode == 0, result.stderr
    entries = result.stdout.strip().split(os.pathsep)
    roots = [str(p) for p in first_party_roots(REPO_ROOT)]
    assert entries[: len(roots)] == roots, (
        f"the command `make {targets}` runs does not put this checkout's roots "
        f"first: {entries}"
    )
    assert entries[-1] == sentinel, (
        f"the command `make {targets}` runs discards a PYTHONPATH the caller set "
        f"({sentinel}): {entries}. The pin is meant to win, not to clobber."
    )


def test_the_wrapper_keeps_a_pin_the_caller_set_as_well() -> None:
    """Ours first, theirs after: the checkout under test wins without clobbering."""
    # Any absolute path will do: it is carried through the environment and compared,
    # never opened. Derived from the checkout rather than written as a /tmp literal,
    # which both linters read as a temp-file hazard that is not one here.
    sentinel = str(REPO_ROOT / "a-path-the-caller-set")
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
    with _probe_makefile("__probe__:\n\t@printf '%s' '$(PYTEST_HERMETIC)'") as probe:
        wrapper = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "make",
                "--no-print-directory",
                "-C",
                str(REPO_ROOT),
                "FIRST_PARTY_PYTHONPATH=",
                "-f",
                "Makefile",
                "-f",
                str(probe),
                "__probe__",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    assert wrapper.returncode == 0, wrapper.stderr
    assert "PYTHONPATH=" not in wrapper.stdout, wrapper.stdout


def test_the_escape_works_from_the_environment_too_not_only_the_command_line() -> None:
    """The same escape, set as an environment variable rather than a make word.

    ⚠️ **These two forms are not equivalent, and only one of them depends on the
    `?=`.** A command-line assignment overrides a makefile variable however it
    was defined, so the test above passes whether `hermetic_aws.mk` says `?=` or
    `:=`. An *environment* assignment is overridden by `:=` and honoured by
    `?=`. So switching that one operator silently breaks the documented escape
    for anyone who exports the variable, and the command-line form cannot see
    it: measured, `?=` → `:=` leaves all other tests in this module green.

    Neither CLAUDE.md nor `hermetic_aws.mk` restricts the escape to one form, so
    both are covered here.
    """
    with _probe_makefile("__probe__:\n\t@printf '%s' '$(PYTEST_HERMETIC)'") as probe:
        wrapper = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "make",
                "--no-print-directory",
                "-C",
                str(REPO_ROOT),
                "-f",
                "Makefile",
                "-f",
                str(probe),
                "__probe__",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, FIRST_PARTY_PYTHONPATH=""),
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


def test_makefile_is_the_file_a_recipe_would_actually_read() -> None:
    """`-f Makefile` is only faithful while `Makefile` wins make's own search.

    The probes name the real makefile explicitly, because once any `-f` is given
    make stops reading the default one. That is faithful only so long as
    `Makefile` *is* what a bare `make` in these directories would read -- and
    **`GNUmakefile` outranks it**, as does a lowercase `makefile` on a
    case-sensitive filesystem. Verified on both 3.81 and 4.4.1: with a
    `GNUmakefile` present, bare `make` reads it and `-f Makefile` does not.

    So adding one would silently point every probe here at a different file than
    production reads, which is the exact class of divergence this module exists
    to detect. None exists today; this keeps that a measured fact.
    """
    # Read the directory rather than probing paths: `Path.exists()` is
    # case-insensitive on macOS and `resolve()` does not canonicalise the case
    # either, so `directory / "makefile"` reports True for the real `Makefile`.
    # A listing gives the names as the filesystem actually holds them.
    outranking = [
        directory / name
        for directory in (REPO_ROOT, LIBRARY_MAKEFILE_DIR)
        for name in sorted(os.listdir(directory))
        if name in ("GNUmakefile", "makefile")
    ]
    assert not outranking, (
        f"these outrank Makefile in make's search order, so `-f Makefile` in this "
        f"module would probe a different file than a bare `make` recipe reads: "
        f"{[str(p) for p in outranking]}"
    )
