# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the per-tree coverage runner, `scripts/coverage_all.py`.

This module builds the reports the ratchet reads. That makes a silent failure here worse
than a silent failure in the ratchet: if a tree's pytest invocation is malformed, the run
produces no report and `check_coverage_debt.py` names that tree as unchecked -- and where
that leaves nothing checked at all it refuses to report a verdict rather than passing
(#1190). So the properties asserted are the ones that decide *whether a report is produced
at all*, the ones that decide whether a failed run is distinguishable from a clean one, and
the one that decides whether a report describes the subprocesses the suite started.

Most of these tests run no pytest: `subprocess.run` is replaced, and what is asserted is
the command line that would have been issued. Running the nine real suites here would take
minutes and would assert nothing about the command construction that is the point. The
exception is the subprocess-instrumentation class at the end, which runs a real pytest over
a purpose-built probe package, because the property there is what a child process inherits
and no command line shows it.

Two behaviours are easy to get wrong and both have a test:

**Exit code 5 is not a failure.** pytest exits 5 when a marker filter collects nothing,
which `-m "not integration"` legitimately does. Treating it as a failure would make the
whole run red for a tree that is simply all-integration; treating a *real* failure as
success would let a partial report be recorded as a measurement.

**A tree whose tests failed is named.** Its report still exists and still parses, and its
numbers will be uniformly and inexplicably low, because the tests that would have covered
those lines never ran. That has been mistaken for a real regression before, so the run says
which trees to distrust rather than printing nine figures that all look equally solid.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import coverage
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "coverage_all", REPO_ROOT / "scripts" / "coverage_all.py"
)
assert _spec and _spec.loader
cov_all = importlib.util.module_from_spec(_spec)
sys.modules["coverage_all"] = cov_all
_spec.loader.exec_module(cov_all)

ccd = cov_all._ccd


class _Recorder:
    """Stands in for `subprocess.run`, recording every command and cwd.

    Appends under a lock, because `coverage_all.main` measures trees on a thread pool
    and `list.append` from several threads is not what this is here to exercise. ⚠️ The
    recorded **order** is the pool's scheduling order and is not a property to assert —
    three assertions in this module did assert it and passed or failed run to run. Where
    an order genuinely matters, drive the sequential path with `--jobs 1`.
    """

    def __init__(self, returncode: int = 0, write_report: bool = True) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.returncode = returncode
        self.write_report = write_report
        self._lock = threading.Lock()

    def cmd_for(self, cwd_name: str) -> list[str]:
        """The one command recorded for the tree whose cwd is named ``cwd_name``.

        ⚠️ Use this rather than `calls[0]`/`calls[1]`. Positional indexing into a list
        several threads appended to reads whichever tree happened to be scheduled first,
        so an assertion about *that* tree's command line is really an assertion about the
        pool — which is how a per-tree claim ("the serial tree gets no `-n`") passed and
        failed run to run while the thing it claimed was never checked.
        """
        matching = [cmd for cmd, cwd in self.calls if cwd and cwd.name == cwd_name]
        assert len(matching) == 1, (
            f"expected exactly one invocation from {cwd_name!r}, got {len(matching)}; "
            f"recorded: {[c.name for _, c in self.calls if c]}"
        )
        return matching[0]

    def __call__(self, cmd, cwd=None, **kwargs):
        with self._lock:
            self.calls.append((list(cmd), Path(cwd) if cwd else None))
        if self.write_report:
            for arg in cmd:
                if arg.startswith("--cov-report=xml:"):
                    out = Path(arg.split(":", 1)[1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(
                        '<?xml version="1.0" ?><coverage line-rate="0.5"/>\n',
                        encoding="utf-8",
                    )
        return subprocess.CompletedProcess(cmd, self.returncode)


def _fake_lib_package(root: Path, name: str) -> Path:
    """A `lib/<name>/pyproject.toml` under `root`, which is what the pin is derived from.

    `first_party_pythonpath` globs `lib/*/pyproject.toml` relative to
    `coverage_all.REPO_ROOT`, and `_install` repoints that at `tmp_path` — so a fixture
    without this produces an empty pin, and an assertion about the pin then passes over
    a run that carries none.
    """
    package = root / "lib" / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return package


def _fake_tree(tmp_path: Path, name: str):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return ccd.Tree(name, name, "pkg")


def _install(monkeypatch, tmp_path, trees, recorder):
    monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cov_all, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ccd, "TREES", tuple(trees))
    monkeypatch.setattr(ccd, "TREES_BY_NAME", {t.name: t for t in trees})
    monkeypatch.setattr(cov_all.subprocess, "run", recorder)


def _cov_report_arg(cmd: list[str]) -> str:
    return next(a for a in cmd if a.startswith("--cov-report=xml:"))


@pytest.mark.unit
class TestTheCommandItBuilds:
    def test_each_tree_gets_one_invocation_from_its_own_directory(
        self, monkeypatch, tmp_path, capsys
    ):
        """One invocation per tree, not one over the repository.

        Several of these packages ship their own `tests/conftest.py`, and pytest imports
        them all as the module `tests.conftest`; a single run across two roots is an
        import collision. So a change that consolidated these into one call would not be
        an optimisation, and the cwd is what keeps them separate.
        """
        trees = [_fake_tree(tmp_path, "alpha"), _fake_tree(tmp_path, "beta")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        # Compared as a SET, deliberately. Trees are measured concurrently, so the order
        # the calls are recorded in is the order the pool happened to schedule them —
        # asserting a sequence here passed and failed run to run. What the test is about
        # survives: one invocation per tree, each from that tree's own directory, and no
        # tree consolidated into another's call.
        assert sorted(c[1].name for c in rec.calls) == ["alpha", "beta"]

    def test_the_report_lands_where_the_ratchet_looks_for_it(
        self, monkeypatch, tmp_path, capsys
    ):
        """The one coupling that matters between the two scripts.

        Both ask `check_coverage_debt.report_path`, so they cannot disagree. Asserting
        the resolved path rather than the string shape is what makes this test fail if
        either side starts computing it independently.
        """
        tree = _fake_tree(tmp_path, "alpha")
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [tree], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        written = Path(_cov_report_arg(rec.calls[0][0]).split(":", 1)[1])
        assert written == ccd.report_path(tree)
        assert written.is_file()

    def test_each_trees_extra_pytest_args_are_passed_through(
        self, monkeypatch, tmp_path, capsys
    ):
        """A tree's marker filter or explicit test path is part of what it measures.

        Dropping `-m "not integration"` would run suites that need AWS credentials, and
        dropping an explicit test path would collect the whole repository from that cwd.
        """
        tree = ccd.Tree("alpha", "alpha", "pkg", ("-m", "not integration", "tests"))
        (tmp_path / "alpha").mkdir(parents=True, exist_ok=True)
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [tree], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        cmd = rec.calls[0][0]
        assert cmd[-3:] == ["-m", "not integration", "tests"]
        assert "--cov=pkg" in cmd

    def test_parallel_by_default_and_serial_when_asked(
        self, monkeypatch, tmp_path, capsys
    ):
        """`--serial` exists for a specific bug, so it has to actually change the flags.

        A submodule-scoped `--cov` under `-n auto` produced spurious failures (#1159);
        the escape hatch is only useful if `-n auto` really is absent when it is given.
        """
        tree = _fake_tree(tmp_path, "alpha")
        for argv, expect in ([], True), (["--serial"], False):
            rec = _Recorder()
            _install(monkeypatch, tmp_path, [tree], rec)
            monkeypatch.setattr(sys, "argv", ["coverage_all.py", *argv])
            assert cov_all.main() == 0
            capsys.readouterr()
            assert ("-n" in rec.calls[0][0]) is expect, argv

    def test_the_interpreter_comes_from_PYTHON_when_set(
        self, monkeypatch, tmp_path, capsys
    ):
        """The same override the Makefile uses, so a venv's interpreter is measurable.

        Without it these runs would silently use whichever python happens to be first on
        PATH, which in this repository is a shared install that several checkouts write
        editable pointers into.
        """
        tree = _fake_tree(tmp_path, "alpha")
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [tree], rec)
        monkeypatch.setenv("PYTHON", "/opt/weird/python3")
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert rec.calls[0][0][0] == "/opt/weird/python3"


@pytest.mark.unit
class TestFailuresAreDistinguishableFromCleanRuns:
    def test_exit_code_5_no_tests_collected_is_not_a_failure(
        self, monkeypatch, tmp_path, capsys
    ):
        """pytest exits 5 when a marker filter collects nothing, which is legitimate.

        Reading it as a failure makes the whole run red for a tree that is simply
        all-integration, and a routinely-red gate gets ignored.
        """
        tree = _fake_tree(tmp_path, "alpha")
        _install(monkeypatch, tmp_path, [tree], _Recorder(returncode=5))
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        assert "tests failed" not in capsys.readouterr().out

    def test_a_real_test_failure_exits_one_and_names_the_tree(
        self, monkeypatch, tmp_path, capsys
    ):
        """The report still exists and still parses; its numbers are just wrong.

        A run where some tests never executed shows large, uniform-looking falls across
        unrelated files, which has been mistaken for a genuine regression. Naming the
        tree is what lets the reader distrust the right figures instead of all of them.
        """
        good, bad = _fake_tree(tmp_path, "good"), _fake_tree(tmp_path, "bad")
        rec_ok = _Recorder()

        def run(cmd, cwd=None, **kwargs):
            rec_ok(cmd, cwd=cwd)
            # Keyed on the TREE, not on a call counter. "whichever ran first succeeded"
            # is a race once trees are measured concurrently, and it made this test pass
            # or fail run to run — while the property it is about, that the failing tree
            # is the one named, is exactly what such a race would silently invert.
            return subprocess.CompletedProcess(cmd, 1 if Path(cwd).name == "bad" else 0)

        _install(monkeypatch, tmp_path, [good, bad], run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 1
        out = capsys.readouterr().out
        assert "tests failed in: bad" in out, out
        assert "good" not in out.split("tests failed in:")[1], out
        assert "may be partial" in out, out

    def test_a_failing_tree_does_not_stop_the_later_trees_from_being_measured(
        self, monkeypatch, tmp_path, capsys
    ):
        """Otherwise one broken suite would leave every subsequent tree unmeasured, and
        the ratchet would skip them all while reporting the trees it did check as fine."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        calls: list[str] = []
        lock = threading.Lock()

        def run(cmd, cwd=None, **kwargs):
            with lock:
                calls.append(Path(cwd).name)
            return subprocess.CompletedProcess(cmd, 1 if Path(cwd).name == "a" else 0)

        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 1
        out = capsys.readouterr().out
        # A SET, because the trees are measured concurrently and the completion order is
        # the pool's to choose. The property is that `a` failing cost nothing: all three
        # were still invoked.
        assert sorted(calls) == ["a", "b", "c"]
        # And the one that failed is named, which is the half concurrency could plausibly
        # break — a result attributed to whichever thread finished next would report the
        # wrong tree while still exiting 1, so the exit code alone does not show it.
        assert "tests failed in: a" in out, out
        assert "failed in: b" not in out and "failed in: c" not in out, out

    def test_a_tree_that_produced_no_report_is_shown_as_such_not_as_zero_percent(
        self, monkeypatch, tmp_path, capsys
    ):
        """0% and "no report" are different findings and must not be conflated.

        0% means measured and untested; no report means not measured, and the second is
        the one that makes the ratchet skip the tree silently.
        """
        tree = _fake_tree(tmp_path, "alpha")
        _install(monkeypatch, tmp_path, [tree], _Recorder(write_report=False))
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        cov_all.main()
        out = capsys.readouterr().out
        assert "(no report)" in out, out
        assert "0.00%" not in out, out

    def test_a_malformed_report_is_reported_as_absent_rather_than_crashing(
        self, monkeypatch, tmp_path, capsys
    ):
        """A truncated report is what a killed run leaves behind, and the summary has to
        survive it -- an unhandled parse error here would lose the other trees' figures
        that had already been measured."""
        tree = _fake_tree(tmp_path, "alpha")

        def run(cmd, cwd=None, **kwargs):
            out = Path(_cov_report_arg(list(cmd)).split(":", 1)[1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("<coverage line-rate=", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0)

        _install(monkeypatch, tmp_path, [tree], run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        assert "(no report)" in capsys.readouterr().out


@pytest.mark.unit
class TestTreeSelection:
    def test_only_measures_just_the_named_trees(self, monkeypatch, tmp_path, capsys):
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--only", "b"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert [c[1].name for c in rec.calls] == ["b"]

    def test_only_is_repeatable(self, monkeypatch, tmp_path, capsys):
        """Two `--only` flags select both trees and nothing else.

        Driven with `--jobs 1` so the caller's own order — `c` before `a`, which is not
        registry order — is observable at all. Under concurrency the recorded order is
        the pool's, so the same assertion on the default path passed and failed run to
        run; the selection is the property, and the order is asserted here where it is
        real rather than inferred from a race.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(
            sys,
            "argv",
            ["coverage_all.py", "--jobs", "1", "--only", "c", "--only", "a"],
        )
        assert cov_all.main() == 0
        capsys.readouterr()
        assert [c[1].name for c in rec.calls] == ["c", "a"]

    def test_only_selects_exactly_the_named_trees_when_run_concurrently(
        self, monkeypatch, tmp_path, capsys
    ):
        """The same selection on the default (concurrent) path, as a set.

        Separate from the test above because the two assert different things: that one
        is about the order `--only` preserves, this one about the membership it produces
        — and only the second is a property of the path CI actually takes.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(
            sys, "argv", ["coverage_all.py", "--only", "c", "--only", "a"]
        )
        assert cov_all.main() == 0
        capsys.readouterr()
        assert sorted(c[1].name for c in rec.calls) == ["a", "c"]

    def test_an_unknown_tree_name_exits_2_and_lists_the_known_ones(
        self, monkeypatch, tmp_path, capsys
    ):
        """A typo must not measure nothing and exit 0.

        That is the shape of the failure this whole area is about: `--only idp-common`
        (a hyphen) running no tests, writing no report, and the ratchet then skipping
        the tree it was asked to check.
        """
        trees = [_fake_tree(tmp_path, "alpha")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--only", "alfa"])
        assert cov_all.main() == 2
        out = capsys.readouterr().out
        assert "alfa" in out and "alpha" in out, out
        assert rec.calls == [], "nothing should have been run"

    def test_the_summary_points_at_the_ratchet_as_the_next_step(
        self, monkeypatch, tmp_path, capsys
    ):
        """Producing reports is not checking them, and the two are separate commands."""
        tree = _fake_tree(tmp_path, "alpha")
        _install(monkeypatch, tmp_path, [tree], _Recorder())
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        assert "check_coverage_debt" in capsys.readouterr().out


@pytest.mark.unit
class TestASerialTreeIsNeverRunInParallel:
    """A tree's declaration wins over the command line, in the safe direction.

    `--serial` can force a tree that is normally parallel to run serially; nothing can
    force a tree that declares `serial` to run in parallel. That asymmetry is the point: a
    tree declares it because its recorded baseline was measured that way, and a flag on one
    invocation should not silently change the conditions a comparison is made under.

    What the declaration is *not* protecting is the coverage of code a suite runs as a
    subprocess. That is collected by the environment `coverage_all.subprocess_coverage_env`
    supplies, and it is unaffected by the worker count -- see the class at the end of this
    module, and `Tree.serial`.
    """

    def test_a_serial_tree_gets_no_n_auto_even_without_the_flag(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = ccd.Tree("alpha", "alpha", "pkg", (), serial=True)
        (tmp_path / "alpha").mkdir(parents=True, exist_ok=True)
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [tree], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert "-n" not in rec.calls[0][0], rec.calls[0][0]

    def test_a_parallel_tree_still_gets_n_auto_by_default(
        self, monkeypatch, tmp_path, capsys
    ):
        """The other direction, so the flag is not simply disabled for everything."""
        tree = ccd.Tree("alpha", "alpha", "pkg", (), serial=False)
        (tmp_path / "alpha").mkdir(parents=True, exist_ok=True)
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [tree], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert "-n" in rec.calls[0][0]

    def test_serial_declarations_and_the_flag_are_independent_per_tree(
        self, monkeypatch, tmp_path, capsys
    ):
        """A mixed run: one declared serial, one not, with no flag given."""
        a = ccd.Tree("ser", "ser", "pkg", (), serial=True)
        b = ccd.Tree("par", "par", "pkg", (), serial=False)
        for t in (a, b):
            (tmp_path / t.cwd).mkdir(parents=True, exist_ok=True)
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [a, b], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        # Looked up by tree, not by position: the two are measured concurrently, so
        # `calls[0]` is whichever the pool scheduled first.
        assert "-n" not in rec.cmd_for("ser")
        assert "-n" in rec.cmd_for("par")

    def test_no_registry_tree_declares_serial_without_recording_why(self):
        """`serial` costs wall clock, so an entry that declares it has to say why.

        This replaces an assertion that `scripts` **is** serial. That assertion was
        the pin on a decision, and its docstring set the price of changing it: "a
        decision that comes with a measurement rather than a default". The
        measurement was taken on 2026-10-14 and is recorded at the entry --
        `6093 passed` either way, `86.22%` either way, identical per file, and
        1291s serial against 585s at the `-n 4` that `coverage-all-cicd` actually
        gives this tree. So `scripts` is no longer serial, and the pin now guards
        the general property instead of that one tree's value.

        Why the general form. Serial was costing 29 of the 34 minutes of the
        `unit_tests` job -- 84% of it, as the only serial tree of nine -- and the
        reason in the entry ("not meaningfully faster on this tree anyway") had
        quietly stopped being true as the suite grew to 6093 tests. A pin on the
        literal value cannot catch a stale justification; requiring the entry to
        carry its measurement at least puts the claim where the next reader will
        check it.

        ⚠️ Asserted as "the set is empty", not as a loop over serial trees with
        the check inside. The loop form is what this test was first written as,
        and with no serial tree left its body never executed: it passed
        vacuously AND left its own line uncovered, which the per-file coverage
        diff caught. A guard that cannot run is the thing this repository keeps
        finding, so the assertion is on a value that is always computed.

        `Tree.serial` stays as a capability -- the synthetic trees earlier in
        this class cover both directions of it -- and if a tree ever needs it
        again, the failure here is where the measurement gets asked for.
        """
        serial_trees = [tree.name for tree in ccd.TREES if tree.serial]
        assert serial_trees == [], (
            f"{serial_trees} declare serial=True. Serial is the expensive option: "
            "it cost 29 of the 34 minutes of the `unit_tests` job when `scripts` "
            "was the only one of nine. Record at the entry what the serial run "
            "buys -- the measurement `Tree.serial` describes -- and then update "
            "this test deliberately."
        )

    def test_the_scripts_tree_runs_in_parallel_in_the_real_registry(self):
        """The flip itself, so a silent revert shows up as a failure.

        Reverting this costs ~12 minutes of every `unit_tests` run, which is the
        kind of regression that is invisible in a diff and obvious only in a
        pipeline nobody is timing.
        """
        assert ccd.TREES_BY_NAME["scripts"].serial is False


def _probe_tree(tmp_path: Path, name: str = "probe"):
    """A one-file package whose only execution is two subprocesses, each with its own cwd.

    The statements in `first_child` and `second_child` are reachable in exactly one way
    each: the test runs `mod.py` twice as a child process, from directories that are
    neither the package's nor pytest's. Nothing imports the module, so an in-process
    measurement of this tree reports 0.00% for it however many times the test passes --
    which is what makes it a probe for subprocess collection rather than for coverage in
    general.

    Two things about its shape, each of which a simpler probe cannot see.

    **Two children rather than one, because one child cannot tell whether the children are
    writing over each other.** Without `parallel` in the configuration
    ``COVERAGE_PROCESS_START`` names, every child writes to the single path
    ``COVERAGE_FILE`` gives, so the last one wins and the earlier one's lines are simply
    gone -- and with a single child the report still reads 100%, which is the measurement
    that makes this shape necessary.

    **Each child also imports two modules the report must not contain**, because a child with
    no source bound measures everything it imports and the parent's `combine()` then merges
    all of it. That is invisible to any assertion about `pkg/mod.py`'s own rate: it shows up
    as files in the report that are not in what was asked for, so the test asserts the
    report's **file set** as well as that rate. The two sit at different distances on
    purpose, because a bound can be wrong by being absent or by being too wide:

    * ``outside/far.py`` is outside the probe tree altogether, so it is excluded by any bound
      at all and catches the bound being missing.
    * ``sibling.py`` is inside the tree root but outside the ``pkg`` subdirectory this tree
      measures, so only the right bound excludes it. Widening the source from ``<root>/pkg``
      to ``<root>`` is a one-token simplification that on the real `scripts` tree would point
      every child at the whole repository, and with `far.py` alone the suite stayed green
      through it.
    """
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside" / "far.py").write_text("REACHED = True\n", encoding="utf-8")
    (root / "sibling.py").write_text("ALSO_REACHED = True\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(
        "import sys\n"
        "\n"
        "\n"
        "def first_child():\n"
        "    a = 1\n"
        "    b = a + 1\n"
        "    return b\n"
        "\n"
        "\n"
        "def second_child():\n"
        "    c = 3\n"
        "    d = c + 1\n"
        "    return d\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    sys.path.insert(0, sys.argv[2])\n"
        "    sys.path.insert(0, sys.argv[3])\n"
        "    import far\n"
        "    import sibling\n"
        "\n"
        '    which = first_child if sys.argv[1] == "first" else second_child\n'
        "    reached = far.REACHED and sibling.ALSO_REACHED\n"
        "    sys.exit(0 if which() and reached else 1)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_probe.py").write_text(
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        'MOD = Path(__file__).resolve().parents[1] / "pkg" / "mod.py"\n'
        'OUTSIDE = Path(__file__).resolve().parents[2] / "outside"\n'
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "\n"
        "\n"
        "def _run(which, cwd):\n"
        "    return subprocess.run(\n"
        "        [sys.executable, str(MOD), which, str(OUTSIDE), str(ROOT)],\n"
        "        cwd=cwd,\n"
        "        check=False,\n"
        "    ).returncode\n"
        "\n"
        "\n"
        "def test_the_first_child_runs(tmp_path):\n"
        '    assert _run("first", tmp_path) == 0\n'
        "\n"
        "\n"
        "def test_the_second_child_runs(tmp_path):\n"
        '    assert _run("second", tmp_path) == 0\n',
        encoding="utf-8",
    )
    return ccd.Tree(name, name, "pkg", ("tests",))


def _probe_rate(report: Path) -> float:
    """`pkg/mod.py`'s line rate out of a report this run produced, or 0.0 if absent."""
    rates = ccd.read_report(report, root=report.parent.parent)
    return rates.get("pkg/mod.py", 0.0)


def _probe_files(report: Path) -> set[str]:
    """Every file the report describes, named the way the report names it.

    Read from the XML rather than through `read_report`, because the question here is
    whether the report reaches **outside** the tree and `read_report`'s keys are
    tree-relative -- it would have to fail to relativise the very files this is looking
    for.
    """
    return {
        cls.get("filename", "")
        for cls in ET.parse(report).iter("class")  # pyright: ignore[reportUnknownMemberType]
    }


@pytest.mark.unit
class TestASubprocessOfAMeasuredSuiteCannotRunUninstrumented:
    """The capability, measured end to end: a child process's coverage is collected.

    Not "the environment mentions `COVERAGE_PROCESS_START`". A check written that way
    passes on any spelling that sets the variable, including ones that collect nothing --
    pointing it at a configuration file without `parallel`, or leaving the data file
    relative so each child writes into a directory that is then deleted. So this runs a
    real pytest over a real probe package through `run`, the production function, and
    reads the resulting report. The statements it looks at execute only in a child process
    started with a working directory of its own, so the number is 0.00% unless collection
    genuinely works.

    Both directions are asserted for each of the three variables, because only the pair is
    evidence: with the wiring the probe reads 100.00% over the tree's own files alone;
    without ``COVERAGE_PROCESS_START`` or ``COVERAGE_FILE`` it reads 0.00%; and without
    ``COVERAGE_SUBPROCESS_SOURCE`` the rate is unaffected while the report grows past the
    tree. The negative halves are what would fail if an assertion had been written against
    something the mechanism does not need.

    These run a nested pytest, which the rest of this module deliberately does not: what
    is asserted here is a property of the environment a child process inherits, and no
    command line can show it.
    """

    def test_the_probe_is_fully_covered_through_the_production_wiring(
        self, monkeypatch, tmp_path
    ):
        tree = _probe_tree(tmp_path)
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_all, "REPO_ROOT", tmp_path)
        # `capture` left at its default, so a probe that fails shows its pytest output
        # here rather than only in a return value this assertion discards.
        name, code, total, _ = cov_all.run(tree, sys.executable, parallel=False)
        assert (name, code) == ("probe", 0)
        assert total is not None
        assert _probe_rate(ccd.report_path(tree)) == 100.0
        # And the report describes this tree and nothing else: the children imported
        # `outside/far.py`, which is measurable and must not be measured.
        files = _probe_files(ccd.report_path(tree))
        assert not any("far.py" in f for f in files), files
        assert not any("sibling.py" in f for f in files), files

    @pytest.mark.parametrize(
        "withheld", ["COVERAGE_PROCESS_START", "COVERAGE_FILE", "both"]
    )
    def test_withholding_either_variable_loses_the_subprocess_entirely(
        self, monkeypatch, tmp_path, withheld
    ):
        """Each variable on its own is necessary, so neither is decoration.

        `COVERAGE_PROCESS_START` decides whether the child measures anything;
        `COVERAGE_FILE` decides whether what it measured is in a place the parent's
        `combine()` will find. Withholding either gives the same 0.00%, which is why a
        test that only set one of them would have looked like it worked.
        """
        drop = (
            ["COVERAGE_PROCESS_START", "COVERAGE_FILE"]
            if withheld == "both"
            else [withheld]
        )

        complete = cov_all.subprocess_coverage_env

        def crippled(data_dir, source_dir, env=None):
            out = complete(data_dir, source_dir, env)
            for key in drop:
                out.pop(key, None)
            return out

        tree = _probe_tree(tmp_path)
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_all, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_all, "subprocess_coverage_env", crippled)
        name, code, _, _ = cov_all.run(tree, sys.executable, parallel=False)
        assert (name, code) == ("probe", 0)
        assert _probe_rate(ccd.report_path(tree)) == 0.0

    def test_withholding_the_source_bound_grows_the_report_past_the_tree(
        self, monkeypatch, tmp_path
    ):
        """The third variable, and it fails in a way no rate can show.

        ``COVERAGE_SUBPROCESS_SOURCE`` does not decide whether a child measures -- it
        decides *what*. Withheld, each child measures every file it imports, `combine()`
        merges all of it, and the report stops describing one tree: `pkg/mod.py` still
        reads 100.00% while `outside/far.py` joins the report and the whole-tree total
        moves. A ratchet cannot use such a report and `--write` would record every one of
        those files, so the assertion is about the report's **file set**.
        """
        complete = cov_all.subprocess_coverage_env

        def crippled(data_dir, source_dir, env=None):
            out = complete(data_dir, source_dir, env)
            out.pop("COVERAGE_SUBPROCESS_SOURCE", None)
            return out

        tree = _probe_tree(tmp_path)
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_all, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_all, "subprocess_coverage_env", crippled)
        name, code, _, _ = cov_all.run(tree, sys.executable, parallel=False)
        assert (name, code) == ("probe", 0)
        files = _probe_files(ccd.report_path(tree))
        assert any("far.py" in f for f in files), files
        assert any("sibling.py" in f for f in files), files

    def test_the_configuration_the_variable_names_makes_children_write_their_own_data(
        self,
    ):
        """`parallel` is what keeps a child from overwriting the parent's data file.

        `coverage.process_startup` builds its Coverage object from this file and nothing
        else, so without `parallel` every child writes to the single path `COVERAGE_FILE`
        names -- the one the parent is writing too. Asserted against `coverage`'s own
        parser rather than against the file's text, so a rename of the option or a move to
        another section is caught.
        """
        assert cov_all.SUBPROCESS_RC.is_file()
        config = coverage.Coverage(config_file=str(cov_all.SUBPROCESS_RC)).config
        assert config.parallel is True


@pytest.mark.unit
class TestTheWorkerBudgetUnderConcurrency:
    """Concurrent trees share one host, so no tree may ask for all of it.

    ``-n auto`` is per-process: four trees each passing it request four times the
    machine, and the oversubscription costs more than the concurrency wins. The budget
    is therefore a division, and the thing to assert is that ``auto`` never reaches a
    command line once more than one tree is in flight — a regression to it would look
    like a speed-up on a 16-core developer box and a slow-down on a 4-CPU runner, which
    is the direction nobody measures.
    """

    def test_no_concurrent_invocation_asks_for_auto(
        self, monkeypatch, tmp_path, capsys
    ):
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert rec.calls, "nothing ran"
        for cmd, cwd in rec.calls:
            assert "auto" not in cmd, (cwd, cmd)
            # And it is a real count, so a regression to `-n` with a missing value
            # cannot pass by `auto` merely being absent.
            n = cmd[cmd.index("-n") + 1]
            assert n.isdigit() and int(n) >= 2, (cwd, cmd)

    def test_the_share_is_at_least_two_and_divides_the_host(self):
        """Floored at 2: one worker is slower than the serial run it replaced while
        still paying xdist's startup, so the floor is not cosmetic."""
        assert cov_all.worker_share(4, cpus=16) == 4
        assert cov_all.worker_share(3, cpus=16) == 5
        assert cov_all.worker_share(1, cpus=16) == 16
        # Small hosts: the floor, not a zero or a one.
        assert cov_all.worker_share(4, cpus=4) == 2
        assert cov_all.worker_share(9, cpus=2) == 2
        assert cov_all.worker_share(0, cpus=8) == 8  # a zero divisor must not raise

    def test_jobs_is_bounded_by_the_host_not_only_by_the_flag(
        self, monkeypatch, tmp_path, capsys
    ):
        """⚠️ The bound has to be computed, and prose was the previous answer.

        `worker_share` floors at 2, so `--jobs 4` asks for 8 workers whatever the
        machine has — and GitHub's runner is a 4-CPU container. The ceiling is therefore
        `cpus // 2`, enforced here against a pretend 4-CPU host so the assertion does
        not depend on whatever the test machine has.
        """
        monkeypatch.setattr(cov_all, "cpu_count", lambda: 4)
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d", "e")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "5"])
        assert cov_all.main() == 0
        out = capsys.readouterr().out
        assert "2 at a time" in out, out
        assert "2 xdist worker(s) each on 4 CPU(s)" in out, out

    def test_jobs_is_bounded_by_the_number_of_trees(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(cov_all, "cpu_count", lambda: 64)
        trees = [_fake_tree(tmp_path, "only")]
        _install(monkeypatch, tmp_path, trees, _Recorder())
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "8"])
        assert cov_all.main() == 0
        assert "1 at a time" in capsys.readouterr().out

    def test_jobs_below_one_is_refused_rather_than_clamped(
        self, monkeypatch, tmp_path, capsys
    ):
        """`--jobs 0` is a caller error, and clamping it would run the whole thing with
        a plausible-looking log instead of saying the flag was wrong."""
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path, "a")], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "0"])
        assert cov_all.main() == 2
        assert rec.calls == [], "nothing should have been run"

    def test_a_serial_tree_gets_no_n_even_in_a_concurrent_run(
        self, monkeypatch, tmp_path, capsys
    ):
        """The asymmetry survives concurrency, which is the whole reason for it.

        `scripts` declares serial because xdist under-collects a suite driving its
        subject as a subprocess. Concurrency exists to hide that tree's 16 minutes
        behind the others — so a budget that handed it workers would be the change that
        defeats its own purpose, silently, by reporting falls in files whose suites are
        green.
        """
        ser = ccd.Tree("ser", "ser", "pkg", (), serial=True)
        par = ccd.Tree("par", "par", "pkg", (), serial=False)
        for t in (ser, par):
            (tmp_path / t.cwd).mkdir(parents=True, exist_ok=True)
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [ser, par], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "2"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert "-n" not in rec.cmd_for("ser")
        assert "-n" in rec.cmd_for("par")

    def test_the_concurrent_path_prints_one_block_per_tree(
        self, monkeypatch, tmp_path, capsys
    ):
        """Interleaved pytest output from several trees is unreadable, and this is the
        producer for a gate: a reader who cannot find the failure summary reads a failed
        run as a slow one. So each tree's output is captured and printed under a lock
        behind its own heading."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        _install(monkeypatch, tmp_path, trees, _Recorder())
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        out = capsys.readouterr().out
        headings = [
            line for line in out.splitlines() if line.startswith("=== coverage:")
        ]
        assert len(headings) == 4, out
        assert sorted(h.split()[2] for h in headings) == ["a", "b", "c", "d"], headings
        # The wall clock is reported, because the only reason this concurrency exists is
        # time and an unreported figure is one nobody re-measures.
        assert "wall clock:" in out, out


@pytest.mark.unit
class TestSkipRefusesRatherThanNarrowingSilently:
    """`--skip` removes a tree from the producer; getting it wrong must be loud.

    The variable that uses it (`COVERAGE_CICD_SKIP`) is registered in
    gate_exemptions.json precisely because it narrows what a gate's input covers, and
    the registered reason rests on the skip naming exactly the tree an earlier CI step
    already measured. A typo there would be an unmeasured tree, which is #1256 again.
    """

    def test_a_skipped_tree_is_not_measured_and_the_others_are(
        self, monkeypatch, tmp_path, capsys
    ):
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--skip", "b"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert sorted(c[1].name for c in rec.calls) == ["a", "c"]

    def test_an_unknown_name_to_skip_exits_2_and_measures_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        """A typo must not quietly skip nothing either. Both directions are wrong: the
        caller's wall-clock expectation and the gate's report coverage stop matching,
        and only one of the two is visible in a job log."""
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path, "alpha")], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--skip", "alfa"])
        assert cov_all.main() == 2
        out = capsys.readouterr().out
        assert "alfa" in out and "alpha" in out, out
        assert rec.calls == [], "nothing should have been run"

    def test_skipping_every_tree_is_refused(self, monkeypatch, tmp_path, capsys):
        """Otherwise the producer exits 0 having written no report at all, and the
        ratchet behind it then refuses with nothing to point at."""
        rec = _Recorder()
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path, "alpha")], rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--skip", "alpha"])
        assert cov_all.main() == 2
        assert "nothing would be reported" in capsys.readouterr().out
        assert rec.calls == []


@pytest.mark.unit
class TestTheRunIsPinnedToThisCheckout:
    """A figure measured from another checkout is worse than no figure (#1094).

    `import idp_common` follows the editable-install pointer in the interpreter's
    site-packages, not the checkout a suite lives in, and this script spawns
    `python -m pytest` itself rather than through `$(PYTEST_HERMETIC)` — so the pin that
    wrapper would have supplied has to be supplied here. What makes it worse than
    elsewhere is the destination: these figures are a ratchet's input and `--write`
    records them permanently.
    """

    def test_every_invocation_carries_this_checkouts_package_roots(
        self, monkeypatch, tmp_path, capsys
    ):
        captured: dict[str, str] = {}
        tree = _fake_tree(tmp_path, "alpha")

        def run(cmd, cwd=None, env=None, **kwargs):
            captured.update(env or {})
            return subprocess.CompletedProcess(cmd, 0)

        _install(monkeypatch, tmp_path, [tree], run)
        # `_install` repoints REPO_ROOT at tmp_path, so the checkout this run is pinned
        # to is tmp_path. Give it a `lib/` package, or the pin is legitimately empty and
        # the assertion would pass over a run carrying no pin at all.
        _fake_lib_package(tmp_path, "idp_fake_pkg")
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        cov_all.main()
        capsys.readouterr()
        pinned = cov_all.first_party_pythonpath()
        assert pinned, "no lib/*/pyproject.toml found, so the pin would be vacuous"
        assert captured.get("PYTHONPATH", "").startswith(pinned), captured.get(
            "PYTHONPATH"
        )

    def test_the_roots_are_absolute_and_inside_this_checkout(self):
        """Absolute because a child that changes directory loses a relative entry, and
        inside this checkout because an entry naming a sibling worktree resolves
        perfectly while saying nothing about this tree."""
        roots = cov_all.first_party_pythonpath().split(":")
        assert roots and all(roots)
        for root in roots:
            assert Path(root).is_absolute(), root
            assert Path(root).is_relative_to(cov_all.REPO_ROOT), root
            assert (Path(root) / "pyproject.toml").is_file(), root

    def test_the_set_is_derived_from_the_tree_not_listed(self, monkeypatch, tmp_path):
        """A package added under `lib/` is covered without an edit here, which is the
        same rule `FIRST_PARTY_PYTHONPATH` in make/hermetic_aws.mk follows."""
        (tmp_path / "lib" / "a_new_package").mkdir(parents=True)
        (tmp_path / "lib" / "a_new_package" / "pyproject.toml").write_text("[project]")
        monkeypatch.setattr(cov_all, "REPO_ROOT", tmp_path)
        assert cov_all.first_party_pythonpath() == str(
            tmp_path / "lib" / "a_new_package"
        )

    def test_a_callers_own_pythonpath_is_kept_behind_the_pin(
        self, monkeypatch, tmp_path, capsys
    ):
        """Prepended rather than replacing: the pin must win, and an existing entry a
        suite legitimately needs must not be dropped to achieve that."""
        captured: dict[str, str] = {}
        tree = _fake_tree(tmp_path, "alpha")

        def run(cmd, cwd=None, env=None, **kwargs):
            captured.update(env or {})
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
        _install(monkeypatch, tmp_path, [tree], run)
        _fake_lib_package(tmp_path, "idp_fake_pkg")
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        cov_all.main()
        capsys.readouterr()
        path = captured.get("PYTHONPATH", "")
        assert path.endswith(":/somewhere/else"), path
        assert path.startswith(cov_all.first_party_pythonpath()), path
