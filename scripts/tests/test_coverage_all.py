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
    """Stands in for `subprocess.run`, recording every command and cwd."""

    def __init__(self, returncode: int = 0, write_report: bool = True) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.returncode = returncode
        self.write_report = write_report

    def __call__(self, cmd, cwd=None, **kwargs):
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
        assert [c[1].name for c in rec.calls] == ["alpha", "beta"]

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
        calls = {"n": 0}
        rec_ok = _Recorder()

        def run(cmd, cwd=None, **kwargs):
            calls["n"] += 1
            rec_ok(cmd, cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0 if calls["n"] == 1 else 1)

        _install(monkeypatch, tmp_path, [good, bad], run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 1
        out = capsys.readouterr().out
        assert "tests failed in: bad" in out, out
        assert "may be partial" in out, out

    def test_a_failing_tree_does_not_stop_the_later_trees_from_being_measured(
        self, monkeypatch, tmp_path, capsys
    ):
        """Otherwise one broken suite would leave every subsequent tree unmeasured, and
        the ratchet would skip them all while reporting the trees it did check as fine."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        calls: list[str] = []

        def run(cmd, cwd=None, **kwargs):
            calls.append(Path(cwd).name)
            return subprocess.CompletedProcess(cmd, 1 if Path(cwd).name == "a" else 0)

        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 1
        capsys.readouterr()
        assert calls == ["a", "b", "c"]

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
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(
            sys, "argv", ["coverage_all.py", "--only", "c", "--only", "a"]
        )
        assert cov_all.main() == 0
        capsys.readouterr()
        assert [c[1].name for c in rec.calls] == ["c", "a"]

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
        assert "-n" not in rec.calls[0][0]
        assert "-n" in rec.calls[1][0]

    def test_the_scripts_tree_is_declared_serial_in_the_real_registry(self):
        """Not a synthetic tree: the one whose baseline was measured serially.

        The `scripts` figures in `coverage_debt.json` come from a serial run. Flipping this
        changes the conditions the recorded numbers were taken under, so it is a decision
        that comes with a measurement rather than a default.
        """
        assert ccd.TREES_BY_NAME["scripts"].serial is True


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

    **Each child also imports a module from outside the measured tree** (``outside/far.py``,
    reached by putting its directory on `sys.path`), because a child with no source bound
    measures everything it imports and the parent's `combine()` then merges all of it. That
    is invisible to any assertion about `pkg/mod.py`'s own rate: it shows up as files in the
    report that are not in the tree, so the test asserts the report's **file set** as well as
    that rate.
    """
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside" / "far.py").write_text("REACHED = True\n", encoding="utf-8")
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
        "    import far\n"
        "\n"
        '    which = first_child if sys.argv[1] == "first" else second_child\n'
        "    sys.exit(0 if which() and far.REACHED else 1)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_probe.py").write_text(
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        'MOD = Path(__file__).resolve().parents[1] / "pkg" / "mod.py"\n'
        'OUTSIDE = Path(__file__).resolve().parents[2] / "outside"\n'
        "\n"
        "\n"
        "def _run(which, cwd):\n"
        "    return subprocess.run(\n"
        "        [sys.executable, str(MOD), which, str(OUTSIDE)], cwd=cwd, check=False\n"
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
        name, code, total = cov_all.run(tree, sys.executable, parallel=False)
        assert (name, code) == ("probe", 0)
        assert total is not None
        assert _probe_rate(ccd.report_path(tree)) == 100.0
        # And the report describes this tree and nothing else: the children imported
        # `outside/far.py`, which is measurable and must not be measured.
        files = _probe_files(ccd.report_path(tree))
        assert not any("far.py" in f for f in files), files

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
        name, code, _ = cov_all.run(tree, sys.executable, parallel=False)
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
        name, code, _ = cov_all.run(tree, sys.executable, parallel=False)
        assert (name, code) == ("probe", 0)
        files = _probe_files(ccd.report_path(tree))
        assert any("far.py" in f for f in files), files

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
