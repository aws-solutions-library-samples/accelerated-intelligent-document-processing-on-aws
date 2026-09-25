# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the per-tree coverage runner, `scripts/coverage_all.py`.

This module builds the reports the ratchet reads. That makes a silent failure here worse
than a silent failure in the ratchet: if a tree's pytest invocation is malformed, the run
produces no report, `check_coverage_debt.py` skips that tree, and the gate reports success
having checked eight trees out of nine. So the properties asserted are the ones that decide
*whether a report is produced at all*, and the ones that decide whether a failed run is
distinguishable from a clean one.

No pytest is actually run: `subprocess.run` is replaced, and what is asserted is the
command line that would have been issued. Running the nine real suites here would take
minutes and would assert nothing about the command construction that is the point.

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
from pathlib import Path

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
    """`Tree.serial` is a correctness property, so the flag cannot override it.

    A suite that drives the code under test as a subprocess has its coverage
    under-collected by xdist workers, and the symptom is a large fall in a file whose own
    suite is green -- measured on `scripts`, two hook modules read 33 and 10 points below
    their true figures. Recording that is worse than having no ratchet on those files: it
    pre-approves a real regression down to the recorded floor. So the declaration wins
    over the command line in the direction that protects the measurement.
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
        """Not a synthetic tree: the one this was measured on.

        `scripts/tests/test_check_commit_text.py` and `test_check_shared_branch.py` run
        the hooks they cover as subprocesses. If this ever flips back to parallel, two
        real 95%-plus baselines get re-recorded 10 and 33 points lower.
        """
        assert ccd.TREES_BY_NAME["scripts"].serial is True


@pytest.mark.unit
class TestTreesAreMeasuredConcurrently:
    """Why the concurrency exists, and why it has to be *across* trees.

    Both CI configurations now run this producer before the ratchet, which is the whole of
    issue #1256: before that, the only report either CI wrote was `idp_common`'s and the
    ratchet named the other eight trees as "not checked" and exited 0. Producing nine
    reports sequentially is a wall clock nobody accepts — `scripts` alone measures 989 s.

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

    def test_the_cpu_count_is_the_affinity_mask_not_the_machine(self):
        """A CI runner is a container on a bigger host, so `os.cpu_count()` overstates it
        — and xdist's own `auto` reads the affinity mask, so anything else hands out
        shares of a machine larger than the one the trees run on."""
        import os as _os

        if not hasattr(_os, "sched_getaffinity"):  # pragma: no cover - non-Linux
            pytest.skip("no sched_getaffinity on this platform")
        assert cov_all.cpu_count() == len(_os.sched_getaffinity(0))

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
