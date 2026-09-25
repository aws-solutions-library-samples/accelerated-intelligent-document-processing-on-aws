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
import time
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
        # Sorted, not ordered: trees are measured concurrently, so which tree's
        # subprocess is spawned first is not a property. `--jobs 1` is where the
        # order is pinned, in TestTreesAreMeasuredConcurrently below.
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

        # Keyed on WHICH tree, not on which call arrived first. Trees are measured
        # concurrently, so "the second invocation" names a different tree from run to run;
        # a fake that failed the second call would make this test assert the message names
        # whichever tree happened to lose the race.
        def run(cmd, cwd=None, **kwargs):
            rec_ok(cmd, cwd=cwd)
            return subprocess.CompletedProcess(cmd, 1 if Path(cwd).name == "bad" else 0)

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
        assert sorted(calls) == ["a", "b", "c"]

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
        by_name = {c[1].name: c[0] for c in rec.calls}
        assert "-n" not in by_name["ser"]
        assert "-n" in by_name["par"]

    def test_the_scripts_tree_is_declared_serial_in_the_real_registry(self):
        """Not a synthetic tree: the one whose baseline was measured serially.

        The `scripts` figures in `coverage_debt.json` come from a serial run. Flipping this
        changes the conditions the recorded numbers were taken under, so it is a decision
        that comes with a measurement rather than a default.
        """
        assert ccd.TREES_BY_NAME["scripts"].serial is True


@pytest.mark.unit
class TestTreesAreMeasuredConcurrently:
    """Why the concurrency exists, and why it has to be *across* trees.

    Both CI configurations now run this producer before the ratchet, which is the whole of
    issue #1256: before that, the only report either CI wrote was `idp_common`'s and the
    ratchet named the other eight trees as "not checked" and exited 0. Producing nine
    reports sequentially is a wall clock nobody accepts — `scripts` alone takes about 16
    minutes.

    It cannot be made faster from the inside, either: `scripts` declares `Tree.serial`
    because xdist under-collects a suite that drives its subject as a subprocess, so
    `-n auto` there is a correctness bug rather than a speed-up. The only remaining axis is
    to measure the other trees while it runs, which is what these tests assert — by
    **observing overlap**, not by reading the flag back.
    """

    @staticmethod
    def _observing_recorder(hold: float = 0.05):
        """A fake `subprocess.run` that records how many calls were ever in flight."""
        import threading

        state = {"live": 0, "peak": 0, "order": []}
        lock = threading.Lock()

        def run(cmd, cwd=None, **kwargs):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
                state["order"].append(Path(cwd).name)
            time.sleep(hold)
            with lock:
                state["live"] -= 1
            return subprocess.CompletedProcess(cmd, 0)

        return run, state

    def test_several_trees_really_are_in_flight_at_once(
        self, monkeypatch, tmp_path, capsys
    ):
        """Measured by overlap, because a flag that is parsed is not work that overlapped.

        The assertion that would be vacuous here is `"--jobs" in sys.argv` or a check that
        a ThreadPoolExecutor was constructed: both pass over a `main()` that then runs the
        trees one after another. So the fake subprocess counts how many calls were live
        simultaneously, and the peak has to exceed one.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        run, state = self._observing_recorder()
        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "4"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert state["peak"] > 1, (
            f"four trees with --jobs 4 never overlapped (peak in flight "
            f"{state['peak']}), so the run is sequential whatever the flag says"
        )
        assert sorted(state["order"]) == ["a", "b", "c", "d"]

    def test_jobs_one_is_sequential_and_keeps_registry_order(
        self, monkeypatch, tmp_path, capsys
    ):
        """The other direction, so the previous test is not passing on a fixed pool size.

        `--jobs 1` is also the only mode in which call order is a property: it is what a
        developer reaches for when reading interleaved output is the problem.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        run, state = self._observing_recorder(hold=0.01)
        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "1"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert state["peak"] == 1, state
        assert state["order"] == ["a", "b", "c", "d"]

    def test_the_default_is_concurrent(self, monkeypatch, tmp_path, capsys):
        """With no flag at all. CI passes `--jobs`, but a default of 1 would mean the
        local target and the CI target measure the same trees at wildly different cost,
        and the local one is where the number is usually first read."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        run, state = self._observing_recorder()
        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert state["peak"] > 1, state

    def test_no_tree_is_ever_given_n_auto(self, monkeypatch, tmp_path, capsys):
        """The oversubscription bug concurrency introduces, asserted against directly.

        `-n auto` asks xdist for one worker per CPU **per process**, so four concurrent
        trees each carrying it request four times the host. On a CI runner that is slower
        than the sequential run it replaced, which would make this whole change a
        regression that still looks like parallelism in the log.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "4"])
        assert cov_all.main() == 0
        capsys.readouterr()
        for cmd, _ in rec.calls:
            assert "auto" not in cmd, cmd
            n = cmd[cmd.index("-n") + 1]
            assert n.isdigit() and int(n) >= 2, cmd

    def test_the_worker_budget_divides_the_host_between_concurrent_trees(self):
        """`worker_share` is the division, and both ends of it matter.

        One tree gets the whole host; four trees get a quarter each; and a tree never
        drops below 2 workers, because a one-worker xdist run pays the startup of
        parallelism for none of the benefit.
        """
        assert cov_all.worker_share(1, cpus=16) == 16
        assert cov_all.worker_share(4, cpus=16) == 4
        assert cov_all.worker_share(8, cpus=16) == 2
        assert cov_all.worker_share(32, cpus=4) == 2, "floor of 2 workers"
        assert cov_all.worker_share(4, cpus=1) == 2

    def test_the_cpu_count_is_the_affinity_mask_not_the_machine(self, monkeypatch):
        """A CI runner is a container on a bigger host, so `os.cpu_count()` overstates it
        — and xdist's own `auto` reads the affinity mask, so anything else hands out shares
        of a machine larger than the one the trees run on.

        The two have to be made to **disagree** for this to assert anything. Comparing
        `cpu_count()` against the live affinity mask passes on any machine where the mask
        is the whole host, which is every machine this is likely to be run on: replacing
        the function's body with `os.cpu_count()` — deleting its entire subject — left that
        comparison green. So the container case is constructed rather than hoped for.
        """
        monkeypatch.setattr(cov_all.os, "sched_getaffinity", lambda _pid: {0, 1, 2})
        monkeypatch.setattr(cov_all.os, "cpu_count", lambda: 64)
        assert cov_all.cpu_count() == 3, (
            "cpu_count() read the machine rather than this process's affinity mask, so on "
            "a CI runner it hands out shares of a host larger than the one in use"
        )

    def test_the_default_job_count_is_bounded_by_the_host(
        self, monkeypatch, tmp_path, capsys
    ):
        """The bound `DEFAULT_JOBS` documents, asserted as arithmetic rather than prose.

        `worker_share` floors at 2, so N concurrent trees ask for at least 2N xdist workers.
        On a 4-CPU runner the default of 4 jobs would request 8 — the oversubscription the
        budget exists to prevent. The bound was documented and not implemented.
        """
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c", "d", "e", "f")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(cov_all, "cpu_count", lambda: 4)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py"])
        assert cov_all.main() == 0
        out = capsys.readouterr().out
        assert "2 at a time" in out, out

    def test_every_real_tree_gets_a_distinct_coverage_data_file(self):
        """Over the REAL registry, not synthetic trees in separate temp directories.

        The data file is derived from each tree's own working directory, so distinctness is
        a property of `TREES` rather than of the derivation — and a synthetic fixture that
        puts each tree in its own `tmp_path` subdirectory makes it true by construction and
        can never catch a second tree added at an existing `cwd`. Measured: adding a tenth
        Tree at `lib/idp_sdk` collides, two concurrent trees interleave into one data file,
        and both reports come out well-formed and wrong.
        """
        assert len(ccd.TREES) >= 9, (
            "registry looks truncated; this check would be vacuous"
        )
        by_cwd: dict[str, list[str]] = {}
        for tree in ccd.TREES:
            by_cwd.setdefault(str(ccd.tree_root(tree)), []).append(tree.name)
        collisions = {cwd: names for cwd, names in by_cwd.items() if len(names) > 1}
        assert not collisions, (
            f"these trees share a working directory, so they share one COVERAGE_FILE and "
            f"would interleave into one data set when measured concurrently — each "
            f"reporting the other's lines, both reports well-formed: {collisions}. Give "
            f"the colliding tree its own data file rather than relying on the cwd."
        )

    def test_each_trees_output_is_printed_as_one_block_under_its_own_heading(
        self, monkeypatch, tmp_path, capsys
    ):
        """Interleaved pytest output from four trees is unreadable, and this is the
        producer for a gate: a reader who cannot find a failure summary reads a failed
        run as a slow one."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]

        def run(cmd, cwd=None, **kwargs):
            name = Path(cwd).name
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"line1-{name}\nline2-{name}\n", stderr=""
            )

        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "3"])
        assert cov_all.main() == 0
        out = capsys.readouterr().out
        for name in ("a", "b", "c"):
            block = out.index(f"coverage: {name} ")
            assert out.index(f"line1-{name}", block) < out.index(
                f"line2-{name}", block
            ), out
            # Nothing from another tree between this tree's heading and its last line.
            span = out[block : out.index(f"line2-{name}", block)]
            for other in set("abc") - {name}:
                assert f"line1-{other}" not in span, span

    def test_each_tree_gets_its_own_coverage_data_file(
        self, monkeypatch, tmp_path, capsys
    ):
        """Two concurrent trees sharing one `.coverage` would interleave into one data set
        and each report the other's lines, with both reports well-formed. The default
        filename is per-cwd, which is *usually* distinct — and "usually" is not a property
        a measurement can rest on."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        seen: list[str] = []

        def run(cmd, cwd=None, env=None, **kwargs):
            assert env is not None, "no env passed, so COVERAGE_FILE is unset"
            seen.append(env["COVERAGE_FILE"])
            return subprocess.CompletedProcess(cmd, 0)

        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "3"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert len(set(seen)) == 3, seen


@pytest.mark.unit
class TestSkippingATreeAnEarlierStepMeasured:
    """`--skip` is how CI avoids re-running the one suite it has already run.

    It is the risky half of the wall-clock work: a skip is indistinguishable from a
    missing report at this level, and a missing report is the #1256 defect itself. What
    makes it safe is not anything here — it is that the ratchet step is invoked with
    `--require-all-trees`, so a tree skipped here and produced by nobody is red BY NAME.
    These tests cover the part that belongs to this script: a skip that does what it says,
    and a misspelling that cannot pass for one.
    """

    def test_a_skipped_tree_is_not_measured_and_the_rest_are(
        self, monkeypatch, tmp_path, capsys
    ):
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b", "c")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--skip", "b"])
        assert cov_all.main() == 0
        out = capsys.readouterr().out
        assert sorted(c[1].name for c in rec.calls) == ["a", "c"]
        # And it is not silently reported as a tree with no coverage.
        assert "  b " not in out, out

    def test_an_unknown_skip_name_exits_2_and_measures_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        """A typo must not measure the tree the caller believes it excluded, nor exclude
        nothing while reporting success — either way the caller's cost expectation and
        the set of reports produced stop matching."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--skip", "bee"])
        assert cov_all.main() == 2
        out = capsys.readouterr().out
        assert "bee" in out and "'b'" in out, out
        assert rec.calls == [], "nothing should have been run"

    def test_skipping_every_tree_exits_2_rather_than_reporting_success(
        self, monkeypatch, tmp_path, capsys
    ):
        """Otherwise the producer for a gate can be told to produce nothing and say it
        worked, which is the state #1190 and #1256 are both about."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(
            sys, "argv", ["coverage_all.py", "--skip", "a", "--skip", "b"]
        )
        assert cov_all.main() == 2
        capsys.readouterr()
        assert rec.calls == []

    def test_a_non_positive_jobs_count_exits_2(self, monkeypatch, tmp_path, capsys):
        """`--jobs 0` must not mean "measure nothing" and must not mean "unbounded"."""
        trees = [_fake_tree(tmp_path, "a")]
        rec = _Recorder()
        _install(monkeypatch, tmp_path, trees, rec)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "0"])
        assert cov_all.main() == 2
        capsys.readouterr()
        assert rec.calls == []

    def test_the_cicd_makefile_target_skips_exactly_the_tree_CI_already_measured(self):
        """The Makefile's `COVERAGE_CICD_SKIP` names one tree, and it has to be the one
        an earlier CI step really produces a report for. Skipping any other tree would
        leave it unmeasured, which `--require-all-trees` then makes red — loud, but the
        point is to not ship it."""
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        line = next(
            ln for ln in makefile.splitlines() if ln.startswith("COVERAGE_CICD_SKIP ?=")
        )
        skipped = [w for w in line.split()[2:] if w != "--skip"]
        assert skipped == ["idp_common"], line
        # The RECIPE, not just the variable. Deleting the recipe body leaves a target
        # that exists, runs nothing, and produces no report for any tree — which is
        # #1256 restored, with `make coverage-all-cicd` still green in both CI logs.
        # Measured: that deletion left this test passing until this assertion existed.
        recipe = makefile.split("\ncoverage-all-cicd:")[1].split("\n\n")[0]
        assert "scripts/coverage_all.py" in recipe, recipe
        assert "--jobs $(COVERAGE_CICD_JOBS)" in recipe, recipe
        assert "$(COVERAGE_CICD_SKIP)" in recipe, recipe
        # And that tree's report is the one the ratchet accepts from the existing step.
        assert ccd.LEGACY_IDP_COMMON_REPORT.name == "coverage.xml"
        assert ccd.TREES_BY_NAME["idp_common"].cwd == "lib/idp_common_pkg"


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
        name, code, total, _out = cov_all.run(tree, sys.executable, parallel=False)
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
        name, code, _total, _out = cov_all.run(tree, sys.executable, parallel=False)
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
        name, code, _total, _out = cov_all.run(tree, sys.executable, parallel=False)
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
class TestTheMeasuredSuitesRunHermetically:
    """This script spawns pytest itself, so it has to supply what `$(PYTEST_HERMETIC)` would.

    `coverage-all-cicd` is a **gated** recipe — both CI configurations run it — and every
    other gated recipe reaches pytest through the wrapper in `make/hermetic_aws.mk`, whose
    stated job is that "a suite here that reaches a real AWS endpoint should fail loudly
    rather than quietly transact against whichever account the developer happens to be
    signed in to". A direct `python -m pytest` with `os.environ` passed through defeats
    that for all nine trees at once, and it would do so silently, because the symptom is a
    suite passing.

    The variable list is parsed from that makefile rather than restated, so these tests
    assert the parse rather than a hard-coded set of names.
    """

    def test_every_variable_the_makefile_unsets_is_unset_here(self):
        """Derived, and asserted against the derivation's own source.

        A test naming today's twelve variables would pass over an implementation that named
        today's twelve variables, and the failure that matters is a THIRTEENTH being added
        to the makefile and not reaching this path.
        """
        text = (REPO_ROOT / "make" / "hermetic_aws.mk").read_text(encoding="utf-8")
        body = text.split("HERMETIC_AWS :=", 1)[1]
        lines = []
        for line in body.splitlines():
            lines.append(line)
            if not line.rstrip().endswith("\\"):
                break
        tokens = " ".join(lines).replace("\\", " ").split()
        expected = {
            tokens[i + 1]
            for i, tok in enumerate(tokens)
            if tok == "-u" and i + 1 < len(tokens)
        }
        assert len(expected) >= 8, (
            f"only parsed {len(expected)} names out of HERMETIC_AWS, which would make this "
            f"check nearly vacuous: {expected}"
        )
        seeded = {name: "leaked" for name in expected}
        seeded["UNRELATED_VARIABLE"] = "kept"
        got = cov_all.hermetic_env(seeded)
        leaked = sorted(name for name in expected if name in got)
        assert not leaked, (
            f"these AWS variables reach the measured suites: {leaked}. A gated recipe must "
            f"not hand a test subprocess the machine's real credentials."
        )
        assert got["UNRELATED_VARIABLE"] == "kept", (
            "it stripped more than the AWS names"
        )

    def test_the_credential_files_are_redirected_not_merely_unset(self):
        """Unsetting the variables is not enough: botocore falls back to `~/.aws/config`
        and `~/.aws/credentials` by default, which is how a "clean" environment still finds
        a profile. The makefile redirects both, and so must this."""
        got = cov_all.hermetic_env({})
        assert got["AWS_CONFIG_FILE"] == "/dev/null", got.get("AWS_CONFIG_FILE")
        assert got["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null", got
        assert got["AWS_EC2_METADATA_DISABLED"] == "true", got

    def test_a_renamed_makefile_variable_fails_here_rather_than_silently_disabling_it(
        self, monkeypatch, tmp_path
    ):
        """The one way this whole mechanism could become a no-op: the parse finding nothing.

        A wrapper that does nothing would strip nothing, and every assertion above would
        still pass on an environment that happened to carry no AWS variables — the "correct
        authority, empty result" shape. So the production path refuses a wrapper that
        changes neither a removal nor an assignment, which is checkable without knowing
        anything about `env`'s option grammar.
        """
        fake = tmp_path / "hermetic_aws.mk"
        fake.write_text("HERMETIC_AWS := env\n", encoding="utf-8")
        monkeypatch.setattr(cov_all, "HERMETIC_MK", fake)
        # The expansion is memoised per path, so a fresh path is enough; clearing it anyway
        # keeps this independent of whether an earlier test in the file warmed the cache.
        cov_all._hermetic_expansion.cache_clear()
        with pytest.raises(AssertionError, match="changed nothing"):
            cov_all.hermetic_env({})
        cov_all._hermetic_expansion.cache_clear()

    def test_every_invocation_carries_the_hermetic_environment_and_the_pythonpath_pin(
        self, monkeypatch, tmp_path, capsys
    ):
        """End to end through `main`, not just the helper, because the helper existing is
        not the helper being called."""
        trees = [_fake_tree(tmp_path, n) for n in ("a", "b")]
        seen: list[dict] = []

        def run(cmd, cwd=None, env=None, **kwargs):
            assert env is not None
            seen.append(env)
            return subprocess.CompletedProcess(cmd, 0)

        # A first-party package root in the fake checkout, so the pin has something to
        # derive. Its absence is what makes the pin empty, which is correct behaviour for a
        # tree with no `lib/` and would otherwise make this assertion untestable.
        (tmp_path / "lib" / "fake_pkg").mkdir(parents=True)
        (tmp_path / "lib" / "fake_pkg" / "pyproject.toml").write_text(
            "", encoding="utf-8"
        )
        monkeypatch.setenv("AWS_PROFILE", "a-real-profile")
        _install(monkeypatch, tmp_path, trees, run)
        monkeypatch.setattr(sys, "argv", ["coverage_all.py", "--jobs", "2"])
        assert cov_all.main() == 0
        capsys.readouterr()
        assert len(seen) == 2
        for env in seen:
            assert "AWS_PROFILE" not in env, "a real profile reached a measured suite"
            assert env["AWS_CONFIG_FILE"] == "/dev/null"
            # PREFIX, not equality. `run()` prepends the pin to any existing PYTHONPATH,
            # which is what makes this checkout win over an inherited one — and every
            # make-driven invocation here already exports the five real roots, so an
            # equality assertion is red under `make test-packages-cicd` (a shared gate in
            # both CIs) and red again when `coverage-all-cicd` measures the `scripts` tree.
            # A permanently-red test carries no more signal than a permanently-green one:
            # it made the two mutations this test is the sole detector for
            # (`hermetic_env()` not called, and the pin dropped) indistinguishable from
            # the baseline failure set.
            assert env["PYTHONPATH"].split(":")[0] == str(
                tmp_path / "lib" / "fake_pkg"
            ), env["PYTHONPATH"]

    def test_the_pythonpath_pin_names_every_first_party_root_absolutely(self):
        """Derived from `lib/*/pyproject.toml`, the same rule the makefile uses, so a new
        package under `lib/` is covered without being listed. The packages import each
        other, so pinning one and not the rest is refused by the provenance guard."""
        roots = cov_all.first_party_pythonpath().split(":")
        expected = sorted(
            str(p.parent) for p in (REPO_ROOT / "lib").glob("*/pyproject.toml")
        )
        assert expected, "no first-party roots found; this check would be vacuous"
        assert roots == expected, (roots, expected)
        assert all(r.startswith("/") for r in roots), (
            "a relative entry does not survive into a subprocess that changes directory"
        )
