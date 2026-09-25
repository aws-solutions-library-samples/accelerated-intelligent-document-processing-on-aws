#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Produce one coverage report per measurable tree, for the ratchet to read.

One invocation per tree, not one over the repository. Several packages ship their own
``tests/conftest.py`` and pytest imports them all as the module ``tests.conftest``, so a
single run across two roots is an import collision —
:mod:`scripts.run_all_tests` shells out per root for the same reason.

Each tree's report lands at ``<tree>/test-reports/coverage-<name>.xml``, which is where
``scripts/check_coverage_debt.py`` looks for it, with the run's JUnit XML beside it at
``coverage-<name>-results.xml``. The ratchet compares a report only when that record says
the run finished: a run whose xdist workers errored writes a well-formed coverage report
whose numbers are an artefact, and nothing inside the report itself says so.

## Trees run concurrently, and the concurrency is across them rather than inside them

Both CI configurations now run this before the ratchet, which is what lets the ratchet
reach more than one tree (issue #1256). Sequentially that is a wall clock nobody would
accept: `scripts` alone measures 989 s and it is one of nine.

The concurrency has to be **across** trees because it cannot be inside all of them.
:attr:`check_coverage_debt.Tree.serial` forbids ``-n auto`` for a tree whose suite drives
the code under test as a subprocess — xdist under-collects it, and the symptom is a
confident-looking fall in a file whose own suite is green. So `scripts` is stuck being one
process, and the only way to hide its 989 s is to measure the other eight while it runs.

That makes the **worker budget** the thing to get right. ``-n auto`` asks xdist for one
worker per CPU, so N concurrent trees each passing it oversubscribe the host by a factor of
N — which on a CI runner turns a wall-clock win into a loss. Each parallel tree is
therefore given an explicit ``-n <k>`` with ``k`` a share of the host, never ``-n auto``;
:func:`worker_share` is that division and a serial tree is given nothing at all.

Output is captured per tree and printed in one block when that tree finishes, because
interleaved pytest output from several trees is unreadable, and unreadable output on the
producer for a gate is how a failed run gets mistaken for a slow one.
Several of the files these suites cover are executed as **subprocesses** — a gate script
run the way a developer runs it, a git hook run the way git runs it — and the coverage of
a subprocess is collected only if that process starts coverage for itself.
:func:`subprocess_coverage_env` is the whole of that wiring, and every pytest invocation
this script makes goes through it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How many trees to measure at once when ``--jobs`` is not given.
#:
#: Bounded by the number of trees there are, and by the host: the point of the budget is
#: that concurrent trees share one machine, so more jobs than CPUs is a slowdown dressed
#: up as parallelism.
DEFAULT_JOBS = 4

_spec = importlib.util.spec_from_file_location(
    "check_coverage_debt", REPO_ROOT / "scripts" / "check_coverage_debt.py"
)
assert _spec and _spec.loader
_ccd = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage_debt"] = _ccd
_spec.loader.exec_module(_ccd)


def cpu_count() -> int:
    """CPUs this process may actually use, which on a CI runner is not ``os.cpu_count()``.

    ``sched_getaffinity`` is what xdist's own ``auto`` reads, so deriving the budget from
    anything else would hand out shares of a machine larger than the one the trees run on.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # pragma: no cover - non-Linux
        return max(1, os.cpu_count() or 1)


def worker_share(jobs: int, cpus: int | None = None) -> int:
    """xdist workers to give ONE tree when ``jobs`` trees are measured at once.

    Never ``auto``. ``-n auto`` is per-process, so four concurrent trees each asking for it
    request four times the host, and the oversubscription costs more than the concurrency
    wins. At least 2, because a tree reduced to a single worker is slower than the serial
    run it replaced while still paying xdist's startup.
    """
    cpus = cpu_count() if cpus is None else cpus
    return max(2, cpus // max(1, jobs))


#: The configuration a subprocess started by a measured suite runs coverage under.
#:
#: Named so that nothing discovers it implicitly: `coverage` and `pytest-cov` search for
#: `.coveragerc`, `setup.cfg`, `tox.ini` and `pyproject.toml`, and this repository has
#: coverage settings in none of them — every option in force is one a command line spells
#: out. Keeping that true means this file is reached only through the environment variable
#: below.
SUBPROCESS_RC = REPO_ROOT / "scripts" / "coverage_subprocess.rc"


def subprocess_coverage_env(
    data_dir: Path, source_dir: Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a pytest invocation needs for its subprocesses to be measured.

    A suite that runs the code it covers as a **subprocess** — `scripts/hooks/` run the
    way git runs them, a gate script run the way a developer runs it — measures nothing
    about that code unless the subprocess starts coverage itself. What starts it is the
    `.pth` file `coverage` installs into `site-packages`: it runs
    `coverage.process_startup()` in every Python process, and that call returns
    immediately unless ``COVERAGE_PROCESS_START`` names a configuration file. Nothing
    else supplies it. `pytest-cov` used to, through a bootstrap of its own
    (``COV_CORE_SOURCE``), and no longer ships one — so the collection is this
    repository's to arrange, at the point where it asks for coverage.

    Three variables are load-bearing and they fail differently:

    * ``COVERAGE_PROCESS_START`` decides *whether* the subprocess measures anything.
    * ``COVERAGE_FILE`` decides whether what it measured is *found*. The data file is
      resolved against the writing process's own working directory, so a subprocess
      started with ``cwd=tmp_path`` writes its data into a directory that is deleted
      before anything combines it. Pointing both the parent and its children at one
      absolute path is what makes the location independent of where a test chose to run.
    * ``COVERAGE_SUBPROCESS_SOURCE`` decides *what* it measures, and the tree's own source
      directory is the answer. `process_startup` builds its Coverage from the rc file
      alone, so a child given no source measures every file it imports, the parent's
      `combine()` merges all of it, and the report grows from one tree to whatever the
      children touched. Measured over the **whole** `scripts` tree (4,906 tests): 1,189
      files and a whole-tree total of 31.52%, against 51 files and 82% with the bound. One
      test file does not show it -- the children of most of these suites import nothing
      outside the tree -- which is why the probe in
      `scripts/tests/test_coverage_all.py` makes its children import a module that is
      outside it on purpose. A ratchet cannot use such a report, and `--write` would record
      every one of those files. Like the data file it has to be **absolute**, which `run`
      guarantees by deriving it from `REPO_ROOT` rather than from `tree.cwd`; the
      `resolve()` below only normalises what it is given.

    Measured on a probe whose only execution is a subprocess with a `cwd` of its own:
    0.00% with neither of the first two variables, 0.00% with ``COVERAGE_PROCESS_START``
    alone, 100.00% with both. `scripts/tests/test_coverage_all.py` runs that probe against
    this function rather than asserting that the variables are spelled a particular way.
    """
    out = dict(os.environ if env is None else env)
    out["COVERAGE_PROCESS_START"] = str(SUBPROCESS_RC)
    out["COVERAGE_FILE"] = str(Path(data_dir).resolve() / ".coverage")
    out["COVERAGE_SUBPROCESS_SOURCE"] = str(Path(source_dir).resolve())
    return out


def run(
    tree, python: str, parallel: bool, workers: int | None = None
) -> tuple[str, int, float | None, str]:
    report = _ccd.report_path(tree)
    report.parent.mkdir(parents=True, exist_ok=True)
    # The JUnit XML is what lets the ratchet tell a finished run from one whose workers
    # errored: a coverage report carries no record of its own session, so without this
    # the ratchet has to treat every report here as unverifiable. Its path is derived
    # from the report's, never spelled out twice -- `check_coverage_debt.run_record_path`
    # is the single answer to where it goes.
    record = _ccd.run_record_path(report)
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if parallel:
        # An explicit count, not `auto`: see `worker_share`. `auto` under concurrency asks
        # for the whole host once per tree.
        cmd += ["-n", str(workers if workers is not None else worker_share(1))]
    cmd += [
        f"--cov={tree.cov}",
        f"--cov-report=xml:{report}",
        "--cov-report=",
        f"--junitxml={record}",
        *tree.args,
    ]
    cwd = REPO_ROOT / tree.cwd
    # What the children measure has to be the same tree the parent measures, spelled
    # absolutely: `--cov=<tree.cov>` is resolved by pytest-cov against this cwd, and a
    # child does not have this cwd.
    source = cwd if tree.cov == "." else cwd / tree.cov
    # `subprocess_coverage_env` also names COVERAGE_FILE, absolutely, under this tree's own
    # cwd -- which is what keeps concurrent trees from interleaving into one data file, as
    # well as what lets a child's data be found at all. Every Tree has a distinct cwd, so
    # the per-tree property comes for free from the instrument rather than from a second
    # mechanism here.
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=subprocess_coverage_env(cwd, source),
        capture_output=True,
        text=True,
    )
    total = None
    if report.is_file():
        try:
            total = _ccd.report_total(report)
        except Exception:  # pragma: no cover - malformed report
            total = None
    output = (result.stdout or "") + (result.stderr or "")
    # 5 == no tests collected for the marker, which is not a failure of this script.
    return (
        tree.name,
        (0 if result.returncode in (0, 5) else result.returncode),
        total,
        output,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", action="append", help="measure just this tree (repeatable)"
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help=(
            "do NOT measure this tree (repeatable) — for a caller whose earlier step "
            "already wrote that tree's report"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"measure N trees at once (default {DEFAULT_JOBS}; 1 is fully sequential). "
            f"Each parallel tree gets a share of the host's CPUs, never -n auto"
        ),
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="do not pass -n auto for ANY tree (trees that declare serial are always serial)",
    )
    args = parser.parse_args()

    python = os.environ.get("PYTHON") or sys.executable
    trees = _ccd.TREES
    if args.only:
        missing = [n for n in args.only if n not in _ccd.TREES_BY_NAME]
        if missing:
            print(f"unknown tree(s): {missing}. Known: {[t.name for t in trees]}")
            return 2
        trees = tuple(_ccd.TREES_BY_NAME[n] for n in args.only)
    if args.skip:
        # A misspelled --skip must not silently measure a tree the caller believes was
        # skipped, nor silently skip nothing: either way the caller's wall-clock
        # expectation and the gate's report coverage stop matching, which is the shape of
        # defect this whole area is about.
        unknown = [n for n in args.skip if n not in _ccd.TREES_BY_NAME]
        if unknown:
            print(
                f"unknown tree(s) to --skip: {unknown}. "
                f"Known: {[t.name for t in _ccd.TREES]}"
            )
            return 2
        trees = tuple(t for t in trees if t.name not in set(args.skip))
        if not trees:
            print("--skip left no tree to measure, so nothing would be reported")
            return 2

    jobs = DEFAULT_JOBS if args.jobs is None else args.jobs
    if jobs < 1:
        print(f"--jobs must be at least 1, got {jobs}")
        return 2
    jobs = min(jobs, len(trees))
    workers = worker_share(jobs)

    print(
        f"measuring {len(trees)} tree(s), {jobs} at a time, "
        f"{workers} xdist worker(s) each on {cpu_count()} CPU(s)",
        flush=True,
    )

    lock = threading.Lock()
    failures: list[str] = []
    totals: dict[str, float | None] = {}
    started = time.monotonic()

    def measure(tree) -> None:
        # A tree that declares `serial` is never run in parallel, whatever the flag
        # says: for those, `-n auto` does not just cost time, it reports coverage that is
        # wrong in a way nothing downstream can detect. `--serial` can force the rest
        # serial too, but it cannot force a serial tree parallel.
        parallel = not args.serial and not tree.serial
        began = time.monotonic()
        name, code, total, output = run(
            tree, python, parallel=parallel, workers=workers
        )
        with lock:
            # Printed as one block per tree. Several trees' pytest output interleaved line
            # by line is unreadable, and this is the producer for a gate: a reader who
            # cannot find the failure summary reads a failed run as a slow one.
            print(
                f"\n=== coverage: {tree.name} ({tree.cwd}, --cov={tree.cov}) — "
                f"{time.monotonic() - began:.0f}s ===",
                flush=True,
            )
            print(output.rstrip(), flush=True)
            totals[name] = total
            if code:
                failures.append(name)

    # Submitted in registry order, which is largest-first, so the longest tree (`scripts`,
    # serial and ~989 s) starts in the first wave rather than being picked up last when
    # there is nothing left to overlap it with.
    if jobs == 1:
        for tree in trees:
            measure(tree)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(measure, trees))

    print("\n" + "=" * 62)
    for tree in trees:
        total = totals.get(tree.name)
        print(
            f"  {tree.name:<26} {total:>6.2f}%"
            if total is not None
            else f"  {tree.name:<26}  (no report)"
        )
    print(f"\n  wall clock: {time.monotonic() - started:.0f}s for {len(trees)} tree(s)")
    if failures:
        print(
            f"\n⚠️  tests failed in: {', '.join(sorted(failures))} — the reports above "
            f"may be partial"
        )
    print("\nNow run: python3 scripts/check_coverage_debt.py")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
