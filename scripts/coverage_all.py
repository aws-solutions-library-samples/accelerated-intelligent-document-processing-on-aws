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
reach more than one tree (issue #1256). Sequentially the total is a wall clock nobody
would accept; concurrently the job costs its slowest tree instead, and which tree that is
moves, so measure rather than reading a figure here.

The concurrency has to be **across** trees because it may not be available inside one.
:attr:`check_coverage_debt.Tree.serial` is where a tree records that it declines
``-n``, with the measurement at the entry. ⚠️ No tree declares it today, and the belief
it was introduced for turned out to be wrong: `scripts` carried it because xdist was
thought to under-collect a suite driving its subject as a subprocess, and what actually
under-collects such a suite is the absence of subprocess instrumentation
(:func:`subprocess_coverage_env`), not the worker count. So read the flag as a per-entry
measurement rather than as a property of a shape — the Lambda handler fan-outs look like
the obvious case for it and are measured faster without it.

That makes the **worker budget** the thing to get right. ``-n auto`` asks xdist for one
worker per CPU, so N concurrent trees each passing it oversubscribe the host by a factor of
N — which on a CI runner turns a wall-clock win into a loss. Each parallel tree is
therefore given an explicit ``-n <k>`` with ``k`` a share of the host, never ``-n auto``;
:func:`worker_share` is that division, and a serial tree is given nothing at all.

Output is captured per tree and printed in one block when that tree finishes, because
interleaved pytest output from several trees is unreadable, and unreadable output on the
producer for a gate is how a failed run gets mistaken for a slow one.

## Two trees are one logical tree over many pytest roots

:attr:`check_coverage_debt.Tree.fanout` marks a tree whose ``cwd`` is a *prefix* rather
than a package: the Lambda handler directories under a ``src/lambda/`` segment. 82 of
them contain a module named ``index.py``, so the collision argument above applies 38
times over rather than twice, and one invocation cannot cover them.
:func:`run_fanout` is that case — one pytest per derived root, all appending into one
coverage data file, rendered once by ``coverage xml`` and vouched for by one merged
JUnit record. Which roots those are is derived per run
(:func:`check_coverage_debt.fanout_roots`), so a handler that gains its first test joins
the measurement without an edit anywhere.

Several of the files these suites cover are executed as **subprocesses** — a gate script
run the way a developer runs it, a git hook run the way git runs it — and the coverage of
a subprocess is collected only if that process starts coverage for itself.
:func:`subprocess_coverage_env` is the whole of that wiring, and every pytest invocation
this script makes goes through it.

⚠️ **The figures are only reproducible if the AWS environment is neutralised**, so every
invocation goes through :func:`hermetic_aws_env` as well. A suite that can resolve real
credentials measures different lines from one that cannot, and because botocore caches the
credentials it first resolved onto a process-global session, *which test ran first in a
worker process* decides the answer — so the figure moved run to run at a fixed worker
count on a developer machine and not at all on a runner, which has nothing to resolve.
That reasoning is written out where :func:`hermetic_aws_env` is imported below, with
the measurement; the function itself lives in :mod:`scripts.hermetic_aws_env`, shared
with :mod:`scripts.run_all_tests`.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# `scripts/` on the path, because this file is loaded BY PATH (by `make`, and by
# `scripts/tests/test_offline_suites_are_hermetic.py`) and so has no package to
# import its siblings through.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from hermetic_aws_env import (  # noqa: E402, F401
    # The two lists are re-exported rather than used here: this file is loaded by
    # path, and `scripts/tests/test_offline_suites_are_hermetic.py` reads them off it
    # to compare against `$(HERMETIC_AWS)` in the makefile.
    HERMETIC_AWS_SET,
    HERMETIC_AWS_UNSET,
    hermetic_aws_env,
)

sys.path.pop(0)

#: How many trees to measure at once when ``--jobs`` is not given.
#:
#: Bounded in :func:`main` by the number of trees there are **and** by the host:
#: concurrent trees share one machine, and because :func:`worker_share` floors at 2
#: workers, N jobs ask for at least 2N workers. More than half the CPUs' worth of jobs is
#: therefore oversubscription, which is a slowdown dressed up as parallelism — so the
#: value here is a ceiling a small host lowers, not a number every host obeys.
DEFAULT_JOBS = 4

_spec = importlib.util.spec_from_file_location(
    "check_coverage_debt", REPO_ROOT / "scripts" / "check_coverage_debt.py"
)
assert _spec and _spec.loader
_ccd = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage_debt"] = _ccd
_spec.loader.exec_module(_ccd)


#: The configuration a subprocess started by a measured suite runs coverage under.
#:
#: Named so that nothing discovers it implicitly: `coverage` and `pytest-cov` search for
#: `.coveragerc`, `setup.cfg`, `tox.ini` and `pyproject.toml`, and this repository has
#: coverage settings in none of them — every option in force is one a command line spells
#: out. Keeping that true means this file is reached only through the environment variable
#: below.
SUBPROCESS_RC = REPO_ROOT / "scripts" / "coverage_subprocess.rc"

# `HERMETIC_AWS_UNSET`, `HERMETIC_AWS_SET` and `hermetic_aws_env` are imported above
# from `scripts/hermetic_aws_env.py`, which is the one Python expression of the
# `$(HERMETIC_AWS)` wrapper and carries the reasoning for it. They were defined here
# first, in #1377, and then needed again by `scripts/run_all_tests.py` — the other
# pytest caller written in Python rather than as a `make` recipe, and the one `make
# test` invokes. A second copy of a list of credential sources drifts, and the
# direction that matters is the quiet one, so there is one copy and both callers read
# it. Re-exported as module attributes on purpose: this file is loaded by path, and
# the gate in `scripts/tests/test_offline_suites_are_hermetic.py` reads them off it.
#
# Why it matters *here* specifically: the figures this script produces are a
# ratchet's input and `--write` records them permanently, and with the environment
# readable they were **nondeterministic**. `boto3.DEFAULT_SESSION` is created by the
# first bare `boto3.client(...)` in a process and caches the credentials it resolved,
# so whether a `try: boto3...get_caller_identity() / except:` branch is covered
# depends on which test ran *first in its process* — and xdist's `--dist load`
# partition varies with the worker count and run to run. Measured on
# `idp_feature_sdk` (461 tests, 0 errors, 0 skips in every cell): `pack.py` at 99.44%
# or 100.00% from one run to the next at a fixed `-n 4`, and under this
# neutralisation 100.00% at every worker count tried. Issue #1377.


def first_party_pythonpath() -> str:
    """This checkout's first-party package roots, absolute, colon-joined.

    Same rule and same reason as ``FIRST_PARTY_PYTHONPATH`` in `make/hermetic_aws.mk`:
    ``import idp_common`` follows the editable-install pointer in the interpreter's
    site-packages, not the checkout a suite lives in, so an unpinned run can measure a
    **different tree** and read green while doing it (#1094). That is worse here than in
    an ordinary suite, because the figures this script produces are a ratchet's input and
    ``--write`` records them permanently.

    Derived from ``lib/*/pyproject.toml`` rather than listed, so a package added under
    ``lib/`` is covered without an edit here. The paths are absolute because a child that
    changes directory loses a relative one.
    """
    roots = sorted(
        str(pyproject.parent)
        for pyproject in (REPO_ROOT / "lib").glob("*/pyproject.toml")
    )
    return ":".join(roots)


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

    Never ``auto``. ``-n auto`` is per-process, so four concurrent trees each asking for
    it request four times the host, and the oversubscription costs more than the
    concurrency wins. At least 2, because a tree reduced to a single worker is slower
    than the serial run it replaced while still paying xdist's startup.
    """
    cpus = cpu_count() if cpus is None else cpus
    return max(2, cpus // max(1, jobs))


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


#: Prefix of the per-invocation directory holding a fan-out's shared coverage data and
#: its per-root run records.
#:
#: Under the tree's own gitignored ``test-reports/``, which keeps concurrent *trees* out
#: of each other's data for free (every ``Tree`` has a distinct ``cwd``), and created
#: fresh per invocation by ``tempfile.mkdtemp`` so that two runs -- or a run and a test
#: that drives this producer with ``subprocess.run`` mocked -- cannot delete each other's.
#: Removed when the invocation ends; the per-root diagnostics a reader needs are the
#: captured output printed under the tree's heading, with the root, its seconds and its
#: exit status.
FANOUT_WORK_PREFIX = "fanout-"


def _merge_run_records(records: list[Path], destination: Path) -> int:
    """Combine per-root JUnit XMLs into the one record the ratchet reads.

    ``check_coverage_debt.run_trust`` reads a single file beside the report and sums
    ``errors``, ``failures`` and ``tests`` over every ``<testsuite>`` in it, so a
    ``<testsuites>`` wrapper holding one child per root is read exactly as a single
    invocation's record would be — and a root whose suite errored therefore refuses the
    whole tree rather than being averaged away, which is the direction that must not be
    lost in the merge.

    Returns how many root records were readable.

    ⚠️ **Writes nothing when NONE of them is readable**, which is not tidiness. An
    ``<testsuites/>`` with no children records no run, so writing it would replace a
    *good* record left by an earlier real run with one that vouches for nothing — and
    the case where that happens is not hypothetical: `test_coverage_all.py` and
    `test_offline_suites_are_hermetic.py` both drive this function's caller over the
    **real** registry with `subprocess.run` mocked, so no root writes a record and the
    destination is a real path beside a real report. Leaving the old record in place
    instead is fail-closed rather than merely harmless: `run_trust` compares the two
    mtimes, so a stale record beside a fresh report reads as unverified.
    """
    merged = ET.Element("testsuites")
    read = 0
    for record in records:
        try:
            root = ET.parse(record).getroot()
        except (ET.ParseError, OSError):
            continue
        read += 1
        if root.tag == "testsuite":
            merged.append(root)
            continue
        for suite in root.findall("testsuite"):
            merged.append(suite)
    if not read:
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(destination, encoding="utf-8", xml_declaration=True)
    return read


def run_fanout(
    tree, python: str, parallel: bool, workers: int | None = None, capture: bool = False
) -> tuple[str, int, float | None, str]:
    """Own the per-invocation work directory, and remove it however the run ends.

    The ``try``/``finally`` is the whole of this wrapper and it is not tidiness: the
    directory is gitignored, so a leak is invisible to ``git status``, and this producer
    deliberately no longer clears anything on entry — so nothing would ever reap one.
    Measured: a ``kill -9`` mid-run left 108 KB behind, and an exception out of
    :func:`subprocess.run` or :func:`_merge_run_records` leaks the same way.
    """
    report = _ccd.report_path(tree)
    report.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ A FRESH directory per invocation, and never a fixed one cleared on entry. A
    # fixed `test-reports/.fanout` cleared with `shutil.rmtree` was deleting a LIVE run's
    # shared coverage data, because two suites here drive this function over the REAL
    # registry with `subprocess.run` mocked -- and `shutil.rmtree` is not
    # `subprocess.run`, so the guard that protects the run *record* from those tests did
    # not protect the data beside it. Measured in the shape CI runs, which is not a local
    # -only shape: the `scripts` tree passes `scripts/tests` to pytest, so
    # `coverage-all-cicd` runs those very suites concurrently with both fan-out trees in
    # one workspace. The suite PASSED while a live measurement lost 3 of 17 root records
    # and rendered 70.05% against a recorded 69.23%, after which the ratchet named four
    # files its report no longer mentioned and offered the wholesale re-record as the
    # remedy. Two runs in one checkout had the same effect on each other.
    work = Path(
        tempfile.mkdtemp(prefix=f"{FANOUT_WORK_PREFIX}{tree.name}-", dir=report.parent)
    )
    try:
        return _run_fanout(tree, python, parallel, workers, capture, report, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_fanout(
    tree,
    python: str,
    parallel: bool,
    workers: int | None,
    capture: bool,
    report: Path,
    work: Path,
) -> tuple[str, int, float | None, str]:
    """One logical tree over many pytest roots, rendered as one report.

    Each root is its own pytest process, which is the point rather than a cost: 82 of the
    handler directories contain a module named ``index.py``, so two of them collected
    together are an import collision. The coverage of all of them is appended into one
    data file (``--cov-append`` plus one absolute ``COVERAGE_FILE``) and rendered once at
    the end by ``coverage xml``.

    **Why the XML is rendered by ``coverage`` rather than by the last pytest run.**
    ``--cov-report=xml:`` on every invocation means each one overwrites the last, and the
    file that survives is whichever root finished last — a report describing one
    directory under a tree name claiming all of them. Rendering once, from the combined
    data file, is also what makes the report's ``<source>`` the prefix rather than the
    last root, which is the form ``check_coverage_debt.read_report`` resolves against
    ``tree_root``.

    **Each root bounds ``--cov`` to its own directory**, and that is a deliberate scope
    decision rather than a detail. Pointing it at the whole prefix would put every file
    the suites happen to import into one tree's report; bounding it means a handler
    directory nothing collects contributes nothing, so the report — and therefore the
    baseline — describes exactly the directories that have a suite. #1227's decision was
    to baseline those and register the rest as a derived exemption rather than record
    them at ``0.00``. The figure usually quoted for what that avoids is several thousand
    entries; measured through the gate's own derivation it is **80** source files across
    45 directories, and the decision rests on readability and on a 0% floor being unable
    to regress rather than on the count.

    The roots run **sequentially** within the tree. The concurrency in this script is
    across trees (see the module docstring), and a fan-out tree's roots appending into one
    data file cannot also be concurrent without a per-root data file and a combine step;
    measured, the two fan-out trees are not the critical path, so that complexity buys
    nothing today.
    """
    roots = _ccd.fanout_roots(tree)
    record = _ccd.run_record_path(report)
    cwd = REPO_ROOT / tree.cwd
    if not roots:
        # Refused rather than reported as a tree at 0%: a fan-out whose derivation finds
        # no root has measured nothing, and writing an empty report would hand the
        # ratchet a tree it then compares against a populated baseline, naming every
        # recorded file as vanished.
        #
        # ⚠️ And the earlier run's report and record are removed, for the same reason the
        # render failure below removes the report -- more sharply, because here BOTH files
        # are stale, so their mtimes agree with each other and the pairing window cannot
        # see it at all. Measured: with a pair left by a successful run, triggering this
        # refusal left both byte-identical and `run_trust` returned `clean`, reporting
        # the earlier run's test count. The non-zero exit aborts the CI job, which is the
        # same mitigation the render branch has and which was not considered sufficient
        # there: what neither covers is a developer who runs the producer, does not look
        # at its exit status, and then runs the gate.
        report.unlink(missing_ok=True)
        _ccd.run_record_path(report).unlink(missing_ok=True)
        refusal = (
            f"🚫 {tree.name}: fanout_roots() derived no pytest root under {tree.cwd}, so "
            f"this tree would be measured over nothing. Either the prefix moved or the "
            f"derivation is wrong; a report written here would make the ratchet report "
            f"every recorded file as having vanished. Any earlier report and record were "
            f"removed, so the tree is unmeasured rather than described by a stale pair."
        )
        if not capture:
            print(refusal, flush=True)
        return (tree.name, 2, None, refusal)

    # `subprocess_coverage_env` names COVERAGE_FILE absolutely under this work directory,
    # which is both what lets a child process's data be found and what keeps concurrent
    # trees out of each other's data. The directory is new per invocation (above), so
    # there is no stale data file to append to and nothing another run owns to delete.
    #
    # ⚠️ `COVERAGE_SUBPROCESS_SOURCE` is the whole PREFIX, not the current root, and the
    # two bounds are deliberately different. A child process is started by a root's own
    # tests and the parent cannot know which root it belongs to, so bounding children to
    # the prefix is the narrowest bound available; `--cov` per root is narrower and is
    # what keeps an untested handler directory out of the report. So a subprocess could
    # in principle pull a sibling handler in, and that is loud rather than quiet: the
    # ratchet reports it as "measured but not in the baseline". It does not happen today
    # -- the rendered report holds exactly the recorded files and no others.
    env = hermetic_aws_env(subprocess_coverage_env(work, cwd))
    pinned = first_party_pythonpath()
    if pinned:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{pinned}:{existing}" if existing else pinned

    outputs: list[str] = []
    records: list[Path] = []
    worst = 0
    for root in roots:
        leaf = str(Path(root).relative_to(tree.cwd))
        root_record = work / f"{leaf.replace('/', '_')}-results.xml"
        records.append(root_record)
        cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if parallel:
            cmd += ["-n", str(workers if workers is not None else worker_share(1))]
        cmd += [
            f"--cov={leaf}",
            "--cov-append",
            "--cov-report=",
            f"--junitxml={root_record}",
            *tree.args,
            leaf,
        ]
        began = time.monotonic()
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=capture,
            text=True if capture else None,
        )
        # 5 == nothing collected. For a derived root that is a contradiction rather than
        # a tolerable outcome -- the derivation put it here because it holds tests -- so
        # it is reported, but it is not failed on: a capability gate can empty a suite on
        # one machine, and the run record is where that shows up.
        if result.returncode not in (0, 5):
            worst = result.returncode
        if capture:
            outputs.append(
                f"--- {root} ({time.monotonic() - began:.0f}s, "
                f"rc={result.returncode}) ---\n"
                + ((result.stdout or "") + (result.stderr or "")).rstrip()
            )

    rendered = subprocess.run(
        [python, "-m", "coverage", "xml", "-o", str(report)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    # Notes are kept apart from the captured pytest output because they must reach a
    # reader on BOTH paths: with `--jobs 1` nothing captures, and a `coverage xml` that
    # failed would otherwise be a silently absent report under a tree name.
    notes: list[str] = []
    if rendered.returncode != 0:
        worst = worst or rendered.returncode
        # ⚠️ The stale report is REMOVED, and the record is left unwritten below. A
        # failed render leaves whatever an earlier run wrote, and writing a fresh record
        # beside it produces the one pairing `run_trust` cannot detect: it rules out a
        # stale record vouching for a fresh report, and this is the mirror image.
        # ⚠️ That comparison is `abs(mtime difference)` against a 600-second window, so
        # it is SYMMETRIC: what it rules out is the two being far apart, not a direction.
        # Measured by setting the mtimes both ways -- `clean` for a 138-second-old report
        # beside a fresh record AND for the reverse, `unverified` for both at 1200s. So
        # the property is that either pairing inside the window reads as one run.
        #
        # In both CIs this producer and the ratchet are consecutive steps of ONE job in
        # ONE checkout -- GitLab's own comment says the three could not be split into
        # separate jobs without passing the reports between them as artifacts -- so a
        # non-zero exit here does abort before the ratchet reads anything. What it does
        # not cover is a developer who runs the producer, does not look at its exit
        # status, and then runs the gate. Removing the report leaves the tree unmeasured,
        # which `--require-all-trees` refuses by name.
        report.unlink(missing_ok=True)
        notes.append(
            f"⚠️  {tree.name}: coverage xml failed, so this tree has NO report — any "
            f"earlier one was removed rather than left to be read as this run's\n"
            f"{rendered.stdout}{rendered.stderr}"
        )
    else:
        read = _merge_run_records(records, record)
        if read != len(records):
            # Not a warning: a root whose record is unreadable makes the merged record
            # describe a FRACTION of the run while reading as a finished one. The counts
            # are the only trace, and `conditions.collected` is recorded by the baseline
            # and never compared -- only `skipped` drift is -- so a lost root is invisible
            # to the ratchet when its coverage data survived. The producer is the only
            # place that knows, so it fails here.
            worst = worst or 1
            notes.append(
                f"⚠️  {tree.name}: {len(records) - read} of {len(records)} root(s) wrote "
                f"no readable run record, so the merged record vouches for a fraction of "
                f"the run while reading as a finished one"
            )
    if not capture:
        for note in notes:
            print(note, flush=True)
    outputs.extend(notes)

    total = None
    if report.is_file():
        try:
            total = _ccd.report_total(report)
        except Exception:  # pragma: no cover - malformed report
            total = None
    return (tree.name, worst, total, "\n".join(outputs))


def run(
    tree, python: str, parallel: bool, workers: int | None = None, capture: bool = False
) -> tuple[str, int, float | None, str]:
    if tree.fanout:
        return run_fanout(
            tree, python, parallel=parallel, workers=workers, capture=capture
        )
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
        # An explicit count, not `auto`: see `worker_share`. Under concurrency `auto`
        # asks for the whole host once per tree, and the oversubscription costs more
        # than the concurrency wins.
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
    # `subprocess_coverage_env` also names COVERAGE_FILE, absolutely, under this tree's
    # own cwd -- which is what keeps concurrent trees from interleaving into one data
    # file as well as what lets a child's data be found at all. Every Tree has a distinct
    # cwd, so the per-tree property comes from the instrument rather than from a second
    # mechanism here.
    env = hermetic_aws_env(subprocess_coverage_env(cwd, source))
    # The other thing `$(PYTEST_HERMETIC)` would have supplied, and the reason this
    # caller needs it spelled out: without the pin a run can measure another checkout
    # entirely (#1094). Prepended, so a caller's own PYTHONPATH still resolves behind it.
    pinned = first_party_pythonpath()
    if pinned:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{pinned}:{existing}" if existing else pinned
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=capture, text=True if capture else None
    )
    total = None
    if report.is_file():
        try:
            total = _ccd.report_total(report)
        except Exception:  # pragma: no cover - malformed report
            total = None
    output = (result.stdout or "") + (result.stderr or "") if capture else ""
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
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "give each parallel tree exactly N xdist workers, instead of the share "
            "--jobs would divide this host into. The way to reproduce a CI figure for "
            "ONE tree: --only clamps --jobs to 1, and the share for one job is the whole "
            "machine, so the obvious invocation measures a configuration nothing runs"
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
    # Bounded by the trees there are AND by the host, which is what DEFAULT_JOBS claims.
    # The host half has to be computed rather than documented: `worker_share` floors at 2
    # workers, so `--jobs 4` asks for 8 xdist workers whatever the machine has, and
    # GitHub's runner is a 4-CPU container. Oversubscription there is a slowdown dressed
    # up as parallelism -- the precise thing the budget exists to prevent.
    jobs = max(1, min(jobs, len(trees), cpu_count() // 2))
    # ⚠️ The derived share is right for a whole run and WRONG for a comparison, which is
    # why it can be overridden. `--only <one tree>` clamps `jobs` to 1 above, and
    # `worker_share(1)` is the whole machine -- so timing one tree that way measures a
    # worker count no CI run ever uses, and the figure is not comparable to the one it is
    # usually being compared against. Reproducing CI's share previously meant narrowing
    # what `cpu_count()` sees with `taskset`, which is a step outside the tool and easy
    # to leave out of the write-up.
    if args.workers is not None and args.workers < 1:
        # Refused rather than clamped, like `--jobs 0`. The floor in `worker_share` is 2
        # and `--workers 1` is nonetheless accepted: that floor governs what this script
        # DERIVES, where one worker is slower than the serial run it replaces, and this
        # flag exists for a caller reproducing a specific configuration -- including a
        # bad one, deliberately.
        print(f"--workers must be at least 1, got {args.workers}")
        return 2
    if args.workers is not None and args.serial:
        # Refused rather than ignored. `--serial` passes no `-n` at all, so the banner
        # below would announce a worker count nothing uses -- which is precisely the
        # "two numbers that look comparable and are not" failure `--workers` was added
        # to prevent, in the flag's own output.
        print(
            "🚫 --serial passes no -n at all, so --workers would name a share nothing "
            "uses. Drop one of them."
        )
        return 2
    workers = worker_share(jobs) if args.workers is None else args.workers

    # ⚠️ The worker clause is suppressed under `--serial`, which passes no `-n` at all.
    # Printing a share nothing uses is verbatim the misreport the `--workers` refusal
    # above cites as its own justification, and the banner was doing it for the flag that
    # causes it.
    budget = (
        "no -n at all (--serial)"
        if args.serial
        else f"{workers} xdist worker(s) each"
        + ("" if args.workers is None else " (--workers overrides the derived share)")
    )
    print(
        f"measuring {len(trees)} tree(s), {jobs} at a time, {budget} "
        f"on {cpu_count()} CPU(s)",
        flush=True,
    )

    lock = threading.Lock()
    failures: list[str] = []
    totals: dict[str, float | None] = {}
    started = time.monotonic()

    def describe(tree) -> str:
        """One line naming what this tree measures, fan-out root count included.

        A fan-out tree's `--cov` is `.` and its `cwd` is a prefix, so without the root
        count the heading for 38 invocations is indistinguishable from the heading for
        one — which is the difference between a derivation that found its roots and one
        that found none.
        """
        if not tree.fanout:
            return f"{tree.name} ({tree.cwd}, --cov={tree.cov})"
        return (
            f"{tree.name} ({tree.cwd}, fan-out over "
            f"{len(_ccd.fanout_roots(tree))} root(s))"
        )

    def measure(tree) -> None:
        # A tree that declares `serial` is never run in parallel, whatever the flag
        # says: for those, `-n auto` does not just cost time, it reports coverage that is
        # wrong in a way nothing downstream can detect. `--serial` can force the rest
        # serial too, but it cannot force a serial tree parallel.
        parallel = not args.serial and not tree.serial
        began = time.monotonic()
        if jobs == 1:
            # Sequential: stream the output, which is what a developer watching one tree
            # at a time wants, and print the heading first so a long tree is identifiable
            # while it runs rather than only once it finishes.
            print(f"\n=== coverage: {describe(tree)} ===", flush=True)
        name, code, total, output = run(
            tree, python, parallel=parallel, workers=workers, capture=jobs > 1
        )
        with lock:
            if jobs > 1:
                # Printed as one block per tree, under the lock. Several trees' pytest
                # output interleaved line by line is unreadable, and this is the producer
                # for a gate: a reader who cannot find the failure summary reads a failed
                # run as a slow one.
                print(
                    f"\n=== coverage: {describe(tree)} — "
                    f"{time.monotonic() - began:.0f}s ===",
                    flush=True,
                )
                print(output.rstrip(), flush=True)
            totals[name] = total
            if code:
                failures.append(name)

    # Submitted in registry order, which is largest-first, so the longest tree starts
    # in the first wave rather than being picked up last when there is nothing left to
    # overlap it with. Measured on the two trees this mattered for: scheduled last they
    # extended the run, scheduled early they finished inside the slowest tree.
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
