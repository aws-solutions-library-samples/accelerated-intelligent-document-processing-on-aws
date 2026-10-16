#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ratchet test coverage across every measurable tree: per file, and in aggregate.

`idp_common` coverage was 67% when the effort that produced this began. A single number
is easy to raise once and easy to lose quietly, which is why this gate has two halves
that fail on different things.

**It covers every tree in :data:`TREES`, not one.** Measuring them was the point: nobody
knew `scripts/` was already at 81% across 33,000 statements, or that
`feature-platform/main-stack-extensions` was at 95%, or that `idp_sdk` (36%) and
`idp_cli` (27%) were the only genuinely low ones. An unmeasured tree is not a tree at 0%
— it is a tree nobody can make a decision about, and this repository had six of them.

Each tree is a separate pytest invocation, because several packages ship their own
`tests/conftest.py` and pytest imports them all as the module `tests.conftest`; running
two roots in one process is a collision. `scripts/coverage_all.py` produces one report
per tree and this script reads them.

**The aggregate floor** (`--cov-fail-under`, in `lib/idp_common_pkg/Makefile`) catches a
general slide. It is the blunt half and it is not enough on its own.

**The per-file baseline** (`scripts/coverage_debt.json`, checked here) catches what the
floor cannot see: the total can sit perfectly still while one module falls from 99% to
60%, because another module grew at the same time. The aggregate hides that completely;
a per-file record does not. The reverse is also true — a per-file baseline alone lets the
total sink as new, untested files arrive — so both halves exist and neither is redundant.

## What makes this fail

* A recorded file's coverage **drops** below its baseline, by more than
  :data:`TOLERANCE_PCT`. Named, with the before and after.
* A recorded file has **vanished**. Either it was deleted and the entry is stale, or it
  moved and the entry now shields nothing.
* A tracked `idp_common` source file is in **neither** the baseline nor
  :data:`NOT_MEASURED` — universe closure, so a new module cannot arrive uncovered and
  unnoticed.

It deliberately does **not** fail when coverage *rises*. A ratchet that demanded the
baseline be rewritten on every improvement would be re-recorded reflexively, and a file
whose real coverage had fallen would be re-recorded along with it. `--write` is explicit.

## What it quotes beside a finding, which is not a finding of its own

A figure is a property of the **test set** that produced it, so every tree records the
conditions it was measured under — how many tests were collected, and how many of them
were skipped. When a file
is reported as fallen or missing, the skip counts are quoted beside it if this run skipped
materially more than the recording run did, because that is the difference between a file
that lost lines and a file whose tests did not run here. A capability gate removes tests
silently on a machine lacking the capability: `idp_feature_sdk` reported three files
falling by up to 7.80 points on a runner with no AWS SAM CLI while all three were intact,
its `tests/conftest.py` having skipped 24 tests that the recording machine ran (#1377).

A count difference **never fails on its own**. Coverage is allowed to rise, a run with
fewer skips measures more, and a gate that red-lined a branch for a condition nobody
caused would be switched off. The line adds information to a finding that already exists.

## What makes it refuse, which is not the same as failing

A verdict needs a measurement, so two inputs produce **no verdict at all** (exit 2)
rather than a pass:

* **No usable report for any tree.** A gate that measured nothing must not report
  success, and with one exit code serving both it did: on a tree with no report this
  printed an informational line and exited 0, indistinguishable in a job log from a
  clean ratchet. In both CI configurations it runs as a separate step from the run that
  writes its input, so a reordering, a changed report path or a tolerated test failure
  put a green tick on a run that measured no coverage. The refusal is unconditional and
  needs no flag, which is what makes it cover every caller — both CI steps included —
  rather than the one place somebody remembered to assert it. `--require-tree NAME` goes
  further for a caller that knows which tree it just measured: it fails **by name** if
  that tree was not checked, which is stronger than "something was checked" once more
  than one tree can have a report. Issue #1190.

  ⚠️ **"Something was checked" is what both CI configurations settled for, and it is
  how eight of the nine trees stayed unratcheted.** The only report either CI produced
  was `lib/idp_common_pkg`'s, so this gate compared one tree, named the other eight as
  unchecked, and exited 0 — a job log in which a complete run and a one-ninth run look
  the same. `--require-all-trees` is the CI spelling: it requires **every** tree in
  :data:`TREES`, and the list is derived from the registry here rather than enumerated
  by a caller, so a tree added to `TREES` is required without any Makefile or CI config
  being edited. Issue #1256.
* **A report whose run did not finish cleanly.** A partially failed pytest still writes
  a `coverage.xml`, and a run where some xdist workers errored writes one showing large,
  uniform-looking falls across unrelated files — the tests that would have covered them
  never executed. Comparing such a report names losses that did not happen, and the
  remedy this gate prints (`--write`) would then record them permanently, which is the
  worse half: `--write` regenerates the whole baseline, so a fabricated drop is
  indistinguishable from a deliberate one afterwards. So a report is compared only when
  the **run record** beside it — the JUnit XML the same pytest invocation writes — says
  the run finished with no errors and no failures. `--write` refuses outright on such a
  report rather than skipping it, because the remedy is to re-run.

A tree whose run record is **absent, unpaired or unreadable** is treated as unmeasured
rather than as trustworthy: it is named as unchecked, and if that leaves nothing checked
the run has measured nothing and refuses on the first rule above. Fail-closed is the
point — dropping `--junitxml` from a producer cannot quietly turn the trust check off,
it turns the whole gate red.

**What this does not answer is *when*.** The pairing check compares the report and its
record against each other, never against the current invocation, so a report and record
left behind **together** by an earlier run are accepted and a full verdict is printed
about a stale measurement. Neither CI can reach that state — each job starts from a fresh
checkout and neither restores `test-reports/` from a cache or an artifact — but a local
tree keeps whatever the last run wrote. A blanket age limit is the wrong fix: after
`make coverage-all`, one tree failing its suite makes `--write` refuse everything, and the
correct recovery is to re-run that one tree and record again, which depends on the other
eight reports still being accepted an hour later. Freshness therefore has to be something
a caller asks for, not a deadline the gate imposes.

Partial measurement is still a pass: one tree measured and eight unmeasured exits 0 and
names the eight, which is the ordinary local case (`make test-cicd -C lib/idp_common_pkg`
measures `idp_common` alone) and the CI case as well.

## What it is not

Not a blocking gate. It reports precisely — file, before, after, delta, remedy — and the
decision about whether that blocks a merge belongs to whoever is merging. Neither
`develop` nor `main` is branch-protected in this repository, which is
[deliberate](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933),
so a red result here informs rather than refuses.

## Where the report comes from

This script re-measures nothing: it reads the coverage XML that the normal
whole-package run (`make test-cicd -C lib/idp_common_pkg`) writes to
`lib/idp_common_pkg/test-reports/coverage.xml`, and the per-tree reports
`scripts/coverage_all.py` writes. Both producers also write the JUnit run record beside
the report, which is what makes the second refusal above possible.

⚠️ **The two producers do not measure subprocesses alike.** Coverage of code a suite runs
as a **child process** is collected only where `COVERAGE_PROCESS_START` and an absolute
`COVERAGE_FILE` are set, and `scripts/coverage_all.py` is the only producer that sets them
(`coverage_all.subprocess_coverage_env`). So for `idp_common` — the one tree with two
possible reports — a figure from the legacy path can be lower than the same tree's figure
from `coverage_all.py`, for a file whose tests drive it as a child process. Which way that
matters depends on which producer the record came from, and the record does not say: a
baseline taken from the legacy path is only ever undershot by it, while one taken from
`coverage_all.py` and then compared against a legacy report would name falls nobody caused.
So re-recording `idp_common` from `coverage_all.py` goes together with giving the legacy
recipe the same environment.

A submodule-scoped `--cov` under `pytest -n auto` used to produce spurious failures on
this package as well, because `idp_common/__init__.py` cached lazily-imported submodules
in a package-private dict and a patch could land on a different module object than the
one under test (issue #1159). That is fixed — the loader defers to `sys.modules` — and
`tests/unit/test_lazy_submodule_loading.py` holds the property, so every flag
combination now agrees.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "scripts" / "coverage_debt.json"


class Tree(NamedTuple):
    """One measurable tree: where its suite runs, what it covers, where the report lands.

    Each entry is a separate pytest invocation because these packages ship their own
    ``tests/conftest.py`` and pytest imports them all as the module ``tests.conftest`` —
    running two roots in one process is a collision, which is why
    ``scripts/run_all_tests.py`` shells out per root too. So there is one report per
    tree rather than one for the repository.
    """

    #: Registry key, and the name used in output and in the baseline.
    name: str
    #: Directory the suite is invoked from, relative to the repo root.
    cwd: str
    #: What to measure: an importable package name, or a path for a flat Lambda tree.
    cov: str
    #: Extra pytest arguments this tree needs (a marker filter, an explicit test path).
    args: tuple[str, ...] = ()
    #: Measure this tree WITHOUT `-n auto`.
    #:
    #: **What under-collects the coverage of a suite that drives the code under test as a
    #: subprocess is the absence of subprocess instrumentation, not the worker count.**
    #: Measured on `scripts/hooks/check_commit_text.py`, one test file, `--cov=scripts`,
    #: 80 passed in every cell: 65.15% serial and 65.15% under `-n 4` without
    #: ``COVERAGE_PROCESS_START``; 98.48% serial and 98.48% under `-n 4` with it. xdist
    #: moves that figure by nothing at all and the instrument moves it by 33 points, so
    #: the flag is not what protects those files -- `scripts/coverage_all.py` is, through
    #: :func:`coverage_all.subprocess_coverage_env`.
    #:
    #: ⚠️ **Read that as a statement about subprocess instrumentation, not as a general
    #: claim that the worker count cannot move a figure.** It can, for a file whose
    #: branches depend on **process-global** state, because the worker count decides how
    #: the tests are partitioned into processes and `--dist load` makes the partition vary
    #: run to run. The live instance was credential resolution: `boto3.DEFAULT_SESSION`
    #: caches the credentials the first bare `boto3.client(...)` in a process resolved, so
    #: a later `monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")` is ignored and a
    #: `try: ...get_caller_identity() / except:` branch is covered or not according to
    #: which test ran first in that worker. Measured on `idp_feature_sdk`, 461 tests and 0
    #: skips in every cell: `pack.py` came out 99.44% or 100.00% from one run to the next
    #: at a fixed `-n 4`. What removes it is neutralising the AWS environment rather than
    #: pinning the worker count -- `coverage_all.hermetic_aws_env`, under which the same
    #: file measured 100.00% at every worker count tried. Issue #1377.
    #:
    #: ⚠️ **A capability difference is a third thing again, and `serial` does not help
    #: with it either.** An absent tool changes the *test set*, not the schedule: with no
    #: AWS SAM CLI on `PATH` the autouse fixture in `lib/idp_feature_sdk/tests/conftest.py`
    #: skips every test that would shell out to `sam`, and that tree measures `437 passed,
    #: 24 skipped` for 97.15% instead of `461 passed` for 99.94% -- reproduced exactly, to
    #: the hundredth on each of the three files that move, by removing `sam` from `PATH`
    #: on a machine that has it. So a baseline recorded where a tool is installed is
    #: **unreachable** where it is not, and the conditions a figure was measured under are
    #: part of the figure.
    #:
    #: What this flag is for, then, is cost and caution: it keeps a tree on the
    #: measurement conditions its recorded baseline was taken under. `scripts` stays
    #: serial because its recorded figures come from a serial run and whether the
    #: whole-tree total differs under `-n auto` has not been measured since subprocess
    #: collection was restored. Flipping it is a decision for whoever measures both.
    serial: bool = False


#: Every tree with a coverage figure. Ordered largest-first so the slow ones start early
#: when they are run in sequence.
#:
#: Measured 2026-09-23, before this registry existed — which is the point of it. Nobody
#: knew `scripts/` was already at 81% across 33,000 statements, nor that
#: `feature-platform/main-stack-extensions` was at 96%; and the two genuinely low trees
#: (`idp_sdk` 36%, `idp_cli` 27%) were not visible as the outliers they are. An
#: unmeasured tree is not a tree at 0%, it is a tree nobody can make a decision about.
TREES: tuple[Tree, ...] = (
    Tree("idp_common", "lib/idp_common_pkg", "idp_common", ("-m", "not integration")),
    Tree(
        "scripts",
        ".",
        "scripts",
        (
            "scripts/tests",
            "scripts/sdlc/tests",
            "scripts/security/tests",
            "scripts/srt/tests",
        ),
        # MEASURED PARALLEL-SAFE, 2026-10-14. See `Tree.serial` for what the flag
        # does and does not protect; this entry records the measurement that the
        # note there asked for before anyone flipped it.
        #
        # Both runs `--cov=scripts`, 6094 passed and 42 skipped in each, with
        # subprocess instrumentation and the hermetic AWS environment live in both
        # (those are what protect the figures; the worker count is not).
        #   serial   1291s (21:31), 0.84 cores, 86.22% (37986/44058)
        #   -n 4      462s (07:41), 4.1  cores, 86.22% (37989/44061)
        # Compared PER FILE, which is the granularity the ratchet actually reads:
        # all 214 files identical to the hundredth, whole-tree delta +0.0009
        # points -- 3 statements, in `coverage_all.py`'s own worker-budget branch,
        # which a parallel run executes and a serial one does not.
        #
        # ⚠️ The `-n 4` is measured at `-n 4`, not inferred from a wider run. An
        # earlier pass of this measurement compared a 14-worker run instead,
        # because `--only <one tree>` clamps `--jobs` to 1 and then hands that one
        # tree every CPU -- so the obvious invocation measures a configuration
        # nothing runs. Three trees at `--jobs 3` on a 14-CPU machine is what
        # reproduces CI's share, since `worker_share` divides and floors.
        #
        # `-n 4` is the worker count this tree actually gets in CI, not an
        # arbitrary one: `coverage-all-cicd` passes `--jobs 4` and
        # `coverage_all.worker_share(4)` divides a 16-vCPU runner four ways. A
        # measurement at `-n auto` would have been a measurement of a
        # configuration nothing runs.
        #
        # The claim this replaces -- "the parallel run is not meaningfully faster
        # on this tree anyway, one root cannot use every worker" -- was true when
        # it was written and is not true at 6093 tests. It was costing 29 minutes
        # of a 34-minute `unit_tests` job, 84% of the job, as the only serial tree
        # of nine.
    ),
    Tree("idp_sdk", "lib/idp_sdk", "idp_sdk", ("-m", "not integration")),
    Tree("main_stack_extensions", "feature-platform/main-stack-extensions", "."),
    Tree("idp_cli", "lib/idp_cli_pkg", "idp_cli"),
    Tree("idp_feature_sdk", "lib/idp_feature_sdk", "idp_feature_sdk"),
    Tree(
        "seller_entitlement",
        "feature-platform/seller-entitlement-service",
        ".",
        ("tests",),
    ),
    Tree(
        "pii_anonymizer_hook", "feature-platform/pii-anonymizer/hook", ".", ("tests",)
    ),
    Tree(
        "pii_anonymizer_api",
        "feature-platform/pii-anonymizer/feature-api",
        ".",
        ("tests",),
    ),
    # Two Lambda handler directories, registered individually rather than as the start
    # of a fan-out.
    #
    # #1221 reports taking `src/lambda/complete_section_review` from 47% to 99% and
    # `src/lambda/calculate_capacity` from 31% to 99%, and until these entries existed
    # nothing held either figure: no handler directory was in this registry, so a later
    # change could take both back down with every gate green. That is the failure the
    # per-file baseline exists to prevent, on the tree it did not reach.
    #
    # ⚠️ **This is not a fan-out and must not be grown into one by addition.** Counting
    # tracked, non-test `.py` attributed to the directory under a `src/lambda/` segment:
    # 84 handler directories hold code, of which 82 hold a module named `index.py`.
    # Registering them one `Tree` at a time would be a hand-written list that a newly
    # added handler joins only when somebody notices, which is the shape #1227 is about.
    # That issue's decided remedy is a **derived** fan-out — one logical tree over many
    # pytest roots, membership computed from the handler directories, and a
    # universe-closure ratchet with a pinned count so a directory added later fails
    # rather than being absorbed. When that lands it subsumes these two: remove both
    # entries and their baseline records drop out on the next write, because
    # `write_baseline` carries over only the trees this registry names.
    #
    # Each is its own `Tree` rather than one covering `src/lambda`, for the reason every
    # entry above is separate: both directories contain a module named `index.py`, as do
    # 82 of the 84, so two in a single pytest process collide at collection.
    Tree("complete_section_review", "src/lambda/complete_section_review", "."),
    Tree("calculate_capacity", "src/lambda/calculate_capacity", "."),
)

TREES_BY_NAME = {tree.name: tree for tree in TREES}


def tree_root(tree: Tree) -> Path:
    """The directory report paths are made relative to."""
    return (REPO_ROOT / tree.cwd).resolve()


def report_path(tree: Tree) -> Path:
    return tree_root(tree) / "test-reports" / f"coverage-{tree.name}.xml"


#: Where `make test-cicd -C lib/idp_common_pkg` already writes idp_common's report. Kept
#: as a fallback so the existing CI step needs no change to keep feeding this gate.
LEGACY_IDP_COMMON_REPORT = (
    REPO_ROOT / "lib" / "idp_common_pkg" / "test-reports" / "coverage.xml"
)

#: Percentage points a file may lose before this fails.
#:
#: Not zero. Line coverage of a file is not perfectly stable run to run: a branch taken
#: only when an optional dependency is importable, a timing-dependent retry, a test that
#: skips on one machine. A zero-tolerance ratchet on ~200 files converts that noise into
#: routine red, and a gate that is routinely red for reasons nobody caused gets ignored,
#: which costs more than the point it protects. One point is wide enough to absorb a
#: single flipped branch in a small file and narrow enough that losing a function fails.
TOLERANCE_PCT = 1.0

#: Source files deliberately absent from the baseline, with the reason.
#:
#: Membership of the universe is derived from `git ls-files`, so this is the only way a
#: file can be out of scope, and each entry has to say why. Kept deliberately short: the
#: honest way to have a file at 0% is to record it at 0% and let the ratchet hold it
#: there, not to exempt it.
NOT_MEASURED: dict[str, str] = {
    "idp_common/agents/analytics/assets": (
        "Data assets (SQL, prompts) that happen to sit under a package directory, not "
        "importable modules; coverage.py reports nothing for them."
    ),
}


def tracked_source_files(tree: Tree) -> list[str]:
    """Every tracked `.py` under this tree's measured source, tree-root-relative."""
    if tree.cov == ".":
        pathspec = f"{tree.cwd}/*.py" if tree.cwd != "." else "*.py"
    else:
        base = tree.cwd.rstrip("/")
        pathspec = f"{base}/{tree.cov}/*.py" if base != "." else f"{tree.cov}/*.py"
    out = subprocess.run(
        ["git", "ls-files", pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    root = tree_root(tree)
    files = []
    for rel in out:
        try:
            files.append(str((REPO_ROOT / rel).resolve().relative_to(root)))
        except ValueError:  # pragma: no cover - outside the tree
            continue
    # A tree measured by path ("." ) sweeps its own tests too; coverage does not report
    # them as source, so excluding them here keeps the two universes comparable.
    return sorted(f for f in files if not _looks_like_test(f))


def _looks_like_test(rel: str) -> bool:
    parts = Path(rel).parts
    return (
        Path(rel).name.startswith("test_")
        or Path(rel).name == "conftest.py"
        or "tests" in parts
    )


def read_report(report: Path, root: Path | None = None) -> dict[str, float]:
    """Per-file line-rate percentages out of a Cobertura XML coverage report.

    Keys are made **package-relative** (`idp_common/ocr/service.py`), which is what the
    baseline and `git ls-files` both speak. Cobertura's `filename` attribute is relative
    to the report's own `<source>` root instead — for `--cov=idp_common` that root is the
    package directory, so `filename` is `ocr/service.py` with no `idp_common/` prefix.
    Comparing the two forms directly matches nothing, silently: every file looks absent,
    the baseline records zero entries, and the universe-closure check passes because its
    `path in measured` test is false for everything. The first run of this script did
    exactly that, and the only symptom was a cheerful "recorded 0 file(s)".
    """
    tree = ET.parse(report)
    roots = [Path(s.text) for s in tree.iter("source") if s.text]
    rates: dict[str, float] = {}
    for cls in tree.iter("class"):
        filename = cls.get("filename")
        rate = cls.get("line-rate")
        if filename is None or rate is None:
            continue
        key = filename
        base = (root or REPO_ROOT).resolve()
        for source in roots:
            candidate = (source / filename).resolve()
            try:
                key = str(candidate.relative_to(base))
                break
            except ValueError:
                continue
        rates[key] = round(float(rate) * 100, 2)
    return rates


def report_total(report: Path) -> float:
    root = ET.parse(report).getroot()
    rate = root.get("line-rate")
    return round(float(rate or 0) * 100, 2)


def load_baseline() -> dict:
    if not BASELINE.is_file():
        return {"files": {}, "total": 0.0}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _resolve_report(tree: Tree) -> Path | None:
    """This tree's report, or ``None`` if no run has produced one."""
    candidate = report_path(tree)
    if candidate.is_file():
        return candidate
    if tree.name == "idp_common" and LEGACY_IDP_COMMON_REPORT.is_file():
        # `make test-cicd -C lib/idp_common_pkg` writes coverage.xml, not
        # coverage-idp_common.xml. Accepting both means the existing CI step keeps
        # feeding this gate without being rewired.
        return LEGACY_IDP_COMMON_REPORT
    return None


#: The run record's filename, for the one report name that does not follow the pattern.
#:
#: `lib/idp_common_pkg`'s suite writes `coverage.xml` and `test-results.xml`;
#: `scripts/coverage_all.py` writes `coverage-<tree>.xml` and `coverage-<tree>-results.xml`.
#: Both halves of each pair come from one pytest invocation, which is what lets the second
#: vouch for the first. `scripts/tests/test_coverage_debt.py` derives the expected
#: `--junitxml` argument for both producers from :func:`run_record_path`, so a rename on
#: either side fails there rather than degrading this gate to "nothing verifiable".
RUN_RECORD_NAMES = {"coverage.xml": "test-results.xml"}

#: How far apart a coverage report and its run record may be written and still be read as
#: one run.
#:
#: pytest writes both at session finish, seconds apart — the gap is the time to serialise
#: a few hundred files. The window is wide because its job is *pairing*, not timing: it
#: rules out a stale record vouching for a fresh report, which is what a hand-run
#: `pytest --cov` with no `--junitxml` beside an older complete run would otherwise
#: produce. Narrow enough to be defeated by a slow serialisation and it would refuse in
#: CI for no reason, which is the failure mode that gets a gate switched off.
RUN_RECORD_PAIRING_SECONDS = 600


class RunTrust(NamedTuple):
    """Whether a coverage report describes a finished run, and how that was decided."""

    #: ``"clean"``, ``"errored"`` or ``"unverified"``.
    state: str
    #: One line naming the evidence, printed as-is.
    detail: str
    #: How many tests the run **collected** — those it ran plus those it skipped.
    #:
    #: This is pytest's `<testsuite tests="...">`, which is the total rather than the
    #: number that executed. `437 passed, 24 skipped` is recorded as 461 and 24, so the
    #: two together say how much of the suite ran; naming it `collected` is what keeps a
    #: reader of the baseline from reading 461 as "461 tests ran".
    collected: int = 0
    #: How many it **skipped** — the measurement condition that matters most here.
    #:
    #: A figure is a property of the test set that produced it, and the cheapest
    #: observable summary of that set is how much of it did not run. A capability gate
    #: (`shutil.which("sam")` is the live one; a missing optional dependency and an
    #: unreachable service are the same shape) silently removes tests on a machine
    #: lacking the capability, so the same suite at the same commit measures a different
    #: tree. `idp_feature_sdk` is `461 passed, 0 skipped` for 99.94% where SAM is
    #: installed and `437 passed, 24 skipped` for 97.15% where it is not.
    #:
    #: Recording the **count** rather than a list of tools is deliberate. Which tool
    #: gates which test is not derivable, so enumerating them would be authored guesswork
    #: in a generated file; the number of tests that did not run is measured, needs no
    #: maintenance, and covers every capability gate including the ones nobody has met
    #: yet. Issue #1377.
    skipped: int = 0


def run_record_path(report: Path) -> Path:
    """The JUnit XML the run that wrote ``report`` writes beside it."""
    return report.with_name(
        RUN_RECORD_NAMES.get(report.name, f"{report.stem}-results.xml")
    )


def run_trust(report: Path) -> RunTrust:
    """Read the run record beside ``report`` and say whether the report can be compared.

    The coverage XML itself cannot answer this. It carries no record of the session that
    produced it: a run whose workers errored during collection writes a perfectly
    well-formed report whose numbers are an artefact, and nothing inside the file says
    so. The JUnit XML written by the same invocation does say so, in the ``errors`` and
    ``failures`` attributes of its ``<testsuite>`` element — measured against a real
    xdist collection mismatch, which records ``errors="3"`` with the
    ``Different tests were collected between gw0 and gwN`` text in each ``<error>``.

    The alternatives were rejected for availability rather than quality: pytest's exit
    status and the xdist message on its stdout are both gone by the time this runs, since
    both CI configurations invoke this gate as a **separate step** from the run — which is
    the gap issue #1190 is about — and the coverage report's own internal consistency
    cannot tell a genuinely low figure from a fabricated one.

    ⚠️ It also answers the cruder question of whether the **report** can be read at all,
    and that belongs here rather than at the two call sites because both of them already
    handle ``unverified`` correctly and neither handled an exception: `check` put the
    traceback in front of a reader instead of a named refusal, and `--write` died partway
    through regenerating the baseline. A zero-byte `coverage.xml` is the live shape — what
    a producer leaves behind when it is killed mid-write, or when the disk filled — and it
    is the more misleading one than an absent file, because a reader who sees the report
    sitting there concludes the gate compared it. Treated as unmeasured, exactly like a
    record that cannot be read, so `--require-all-trees` names the tree and the run
    reaches no verdict for it.
    """
    try:
        ET.parse(report)
    except (ET.ParseError, OSError) as exc:
        return RunTrust(
            "unverified",
            f"{report.name} cannot be read as a coverage report ({exc}), so it measures "
            f"nothing — a producer killed mid-write leaves exactly this",
        )
    record = run_record_path(report)
    if not record.is_file():
        return RunTrust(
            "unverified",
            f"no run record beside it ({record.name} is absent), so whether the run "
            f"that wrote {report.name} finished cannot be established",
        )
    skew = abs(record.stat().st_mtime - report.stat().st_mtime)
    if skew > RUN_RECORD_PAIRING_SECONDS:
        return RunTrust(
            "unverified",
            f"{record.name} was written {skew / 60:.0f} min away from {report.name}, so "
            f"it records a different run and vouches for nothing",
        )
    try:
        root = ET.parse(record).getroot()
    except (ET.ParseError, OSError) as exc:
        # Every failure to read the record lands on "unverified", never on "clean", so
        # widening this clause can only make the gate more cautious. A record it cannot
        # read is a record that vouches for nothing.
        return RunTrust("unverified", f"{record.name} cannot be read ({exc})")
    suites = list(root.iter("testsuite"))
    if not suites:
        return RunTrust(
            "unverified",
            f"{record.name} has no <testsuite> element, so it records no run",
        )
    try:
        errors = sum(int(s.get("errors") or 0) for s in suites)
        failures = sum(int(s.get("failures") or 0) for s in suites)
        tests = sum(int(s.get("tests") or 0) for s in suites)
        # `skipped` is read the same way as the three above and, like them, is taken as
        # the larger of the attribute and the elements, so a producer that writes only
        # one spelling is still read. Unlike them it is not grounds for refusing a
        # report: a skip is a test declining to run, not a run that did not finish.
        skipped = max(
            sum(int(s.get("skipped") or 0) for s in suites),
            len(list(root.iter("skipped"))),
        )
    except ValueError as exc:
        return RunTrust(
            "unverified", f"{record.name} has an unreadable test count ({exc})"
        )
    # The counts a `<testsuite>` attribute claims are the summary; the `<error>` and
    # `<failure>` elements are the record itself. Taking the larger of the two reads a
    # recorded error whichever way the producer wrote it, rather than trusting one
    # spelling of it -- pytest writes both, and a record carrying an `<error>` under
    # `errors="0"` would otherwise read as a clean run.
    errors = max(errors, len(list(root.iter("error"))))
    failures = max(failures, len(list(root.iter("failure"))))
    if errors or failures:
        return RunTrust(
            "errored",
            f"the run that wrote it recorded {errors} error(s) and {failures} "
            f"failure(s), so tests that would have covered these files never ran — "
            f"the falls it reports are artefacts of the run, not regressions. An xdist "
            f"collection mismatch (`Different tests were collected between gw0 and "
            f"gwN`) is the usual cause; re-run before recording anything",
        )
    if not tests:
        return RunTrust(
            "unverified",
            f"{record.name} records 0 tests, so nothing executed and the report "
            f"describes no measurement",
        )
    return RunTrust(
        "clean",
        f"{record.name} records {tests} test(s) collected, {skipped} skipped, 0 errors",
        tests,
        skipped,
    )


class Unchecked(NamedTuple):
    """A tree this run could not compare, and why. Never a pass.

    Not a carve-out, and the distinction matters because the list looks like one: the
    scope decision about what is measured lives in :data:`TREES`, which is the enforced
    universe, and membership here is a property of one invocation rather than of a tree.
    What makes it safe is that every member is **printed with its reason** and under no
    success marker, and that a run where every tree lands here has measured nothing and
    refuses — so a run that checked one tree cannot be read as one that checked nine.
    """

    tree: str
    reason: str


#: How many more tests this run may skip than the recording run before it is mentioned.
#:
#: Not zero: a handful of skips move between runs for reasons nobody chose (a slow optional
#: dependency, a platform guard) and naming them beside every finding would train a reader
#: to scroll past the line. A capability gate taking a whole feature out is the case worth
#: a sentence, and the live one is 24.
SKIP_DRIFT_WORTH_MENTIONING = 5


def _conditions_diagnosis(
    tree: Tree, recorded_conditions: dict | None, trust: RunTrust
) -> str | None:
    """One line on the measurement conditions, when they explain a finding.

    Returned only when a file has already been reported as fallen or missing, so this
    **adds information to an existing finding and never creates one**. That restraint is
    the point: a count difference on its own is not a regression — coverage is allowed to
    rise, and a run with *fewer* skips measures more — so failing on it would red-line
    branches for a condition nobody caused, which is how a gate gets switched off.

    What it answers is the question that cost a day: whether the file really lost lines,
    or whether the tests that covered them did not run here. `idp_feature_sdk` reports
    three files falling by 1.27, 5.21 and 7.80 points on a runner with no AWS SAM CLI,
    and all three are intact — 24 tests were skipped by the autouse fixture in its
    `tests/conftest.py`, and the baseline was recorded where `sam` was installed.
    """
    if not recorded_conditions:
        return (
            f"[{tree.name}] its baseline records no measurement conditions, so whether "
            f"the run above executed the same tests cannot be checked. This report "
            f"collected {trust.collected} test(s) and skipped {trust.skipped} of them. "
            f"Only --write can capture them, and it is the wider command: a conditions "
            f"record is written as part of recording a whole tree, so --write replaces "
            f"every floor in every measured tree with this run's figure. "
            f"--write-missing cannot do it — it preserves the recorded entry as it "
            f"stands, and the conditions are part of that entry. So capturing them "
            f"means accepting this machine's measurement for this tree; do it from a "
            f"run you would be willing to make the baseline."
        )
    was_skipped = recorded_conditions.get("skipped")
    was_collected = recorded_conditions.get("collected")
    if was_skipped is None or was_collected is None:
        return None
    drift = trust.skipped - was_skipped
    if drift < SKIP_DRIFT_WORTH_MENTIONING:
        return None
    return (
        f"[{tree.name}] this report skipped {trust.skipped} test(s) where its baseline "
        f"was recorded over a run that skipped {was_skipped} ({was_collected} collected "
        f"then, {trust.collected} now). {drift} more test(s) declined to run here, so check "
        f"whether the file(s) above lost lines or whether the tests covering them were "
        f"skipped — a capability gate (an absent CLI, a missing optional dependency) "
        f"removes tests silently on a machine that lacks the capability, and a baseline "
        f"recorded on a more capable machine is a floor this environment cannot reach."
    )


def check_tree(
    tree: Tree,
    recorded: dict[str, float],
    recorded_conditions: dict | None = None,
) -> tuple[list[str], float | None]:
    """Compare one tree against its recorded baseline. Returns (problems, overall)."""
    report = _resolve_report(tree)
    if report is None:
        return [], None
    measured = read_report(report, tree_root(tree))
    problems: list[str] = []
    looks_worse = False

    for path, was in sorted(recorded.items()):
        now = measured.get(path)
        if now is None:
            looks_worse = True
            problems.append(
                f"[{tree.name}] {path} is recorded at {was:.2f}% but the report does not "
                f"mention it. If it moved or was deleted, re-record with --write — only "
                f"that drops a stale entry, and it re-records every measured tree's "
                f"floors wholesale while doing so; if it stopped being imported by any "
                f"test, that is the regression."
            )
            continue
        if now < was - TOLERANCE_PCT:
            looks_worse = True
            problems.append(
                f"[{tree.name}] {path} fell from {was:.2f}% to {now:.2f}% "
                f"(-{was - now:.2f} points, tolerance {TOLERANCE_PCT}). Add tests for the "
                f"lines it lost, or re-record deliberately with --write — only that "
                f"lowers a recorded floor, and it re-records every measured tree's "
                f"floors wholesale while doing so."
            )

    if looks_worse:
        diagnosis = _conditions_diagnosis(tree, recorded_conditions, run_trust(report))
        if diagnosis:
            problems.append(diagnosis)

    unaccounted = [
        path
        for path in tracked_source_files(tree)
        if path not in recorded
        and path in measured
        and not any(path.startswith(k) for k in NOT_MEASURED)
    ]
    if unaccounted:
        problems.append(
            f"[{tree.name}] {len(unaccounted)} source file(s) are measured but not in the "
            f"baseline, so their coverage is unratcheted: "
            + ", ".join(unaccounted[:6])
            + ("..." if len(unaccounted) > 6 else "")
            + ". Record them with --write-missing, which adds only the entries the "
            "baseline lacks and by construction cannot raise or lower an existing "
            "floor. Do not reach for the wider re-record here: it would accept this "
            "machine's figure for every other file in every measured tree, which on a "
            "host-sensitive gate is how an unexamined floor becomes permanent."
        )
    return problems, report_total(report)


class CheckResult(NamedTuple):
    """One run's outcome. ``checked`` is why the caller can describe it accurately.

    A refusal is per tree, so "this run measured nothing" is a claim about the whole run
    that only :attr:`checked` can settle: one tree can be compared cleanly and produce a
    genuine regression while another tree's report is refused. Reporting that combination
    as "nothing was measured" tells a reader to dismiss a real finding as an artefact,
    which is the same defect — a heading a log reader cannot tell apart from a different
    outcome — that this gate exists to remove.
    """

    #: 2 when the run reached no verdict, 1 when the ratchet has findings, else 0.
    code: int
    #: Every line to print: findings first, then refusals.
    problems: list[str]
    #: Trees this run could not compare, each with its reason.
    unchecked: list[Unchecked]
    #: Trees actually compared against their baseline. A finding naming one of these is
    #: a real regression whatever else the run refused.
    checked: list[str]


def check(required: tuple[str, ...] = ()) -> CheckResult:
    """Check every tree with a trustworthy report.

    The exit code is **2** when the run produced no verdict — nothing was measured, a
    report came from a run that did not finish, or a tree named in ``required`` was not
    checked — **1** when the ratchet has findings, and 0 otherwise. A refusal outranks a
    finding: the findings from the trees that *were* measured are still printed, but the
    run as a whole did not establish what it set out to.
    """
    baseline = load_baseline()
    trees = baseline.get("trees", {})
    problems: list[str] = []
    refusals: list[str] = []
    unchecked: list[Unchecked] = []
    checked: list[str] = []
    for tree in TREES:
        report = _resolve_report(tree)
        if report is None:
            # No claim about *when*: this gate compares mtimes of the report and its run
            # record against each other, and nothing against the current invocation, so a
            # report and record left behind together by an earlier run are accepted. Say
            # only what was looked for -- and say it for EVERY path that would have been
            # accepted, or the reader follows the remedy, produces the other one, and
            # finds the file this line named still absent.
            looked_in = [str(report_path(tree))]
            if tree.name == "idp_common":
                looked_in.append(str(LEGACY_IDP_COMMON_REPORT))
            unchecked.append(
                Unchecked(tree.name, f"no coverage report at {' or '.join(looked_in)}")
            )
            continue
        trust = run_trust(report)
        if trust.state == "errored":
            refusals.append(
                f"[{tree.name}] refusing to compare {report.name}: {trust.detail}."
            )
            continue
        if trust.state != "clean":
            unchecked.append(Unchecked(tree.name, trust.detail))
            continue
        recorded = trees.get(tree.name, {}).get("files", {})
        tree_problems, overall = check_tree(
            tree, recorded, trees.get(tree.name, {}).get("conditions")
        )
        if overall is None:  # pragma: no cover - the report resolved a moment ago
            unchecked.append(Unchecked(tree.name, "its report disappeared mid-run"))
            continue
        checked.append(tree.name)
        if not recorded:
            problems.append(
                f"[{tree.name}] a report exists but this tree has no recorded baseline, "
                f"so nothing about it is ratcheted. Record it with --write-missing: a "
                f"tree with no entry has nothing to preserve, so it is recorded in full "
                f"— total, conditions and every file — while no other tree's recorded "
                f"floors move."
            )
        problems.extend(tree_problems)

    for name in required:
        if name in checked:
            continue
        reason = next(
            (u.reason for u in unchecked if u.tree == name),
            "its report was refused, see above",
        )
        refusals.append(
            f"[{name}] --require-tree={name} was passed, but this tree was not "
            f"checked: {reason}. The step that produces its report either did not run, "
            f"did not finish, or wrote it somewhere this gate does not look."
        )

    if not checked:
        refusals.append(
            "no tree was checked, so this run measured nothing and has no verdict to "
            "report. Produce a report first: `make test-cicd -C lib/idp_common_pkg "
            "SKIP_INSTALL=1` for idp_common, or `make coverage-all` for every tree."
        )
    code = 2 if refusals else (1 if problems else 0)
    return CheckResult(code, problems + refusals, unchecked, checked)


def write_baseline(missing_only: bool = False) -> int:
    """Re-record baselines from the reports that a finished run produced.

    Refuses outright — writing nothing — if any report came from a run that did not
    finish, or if no report is recordable at all. This is the dangerous half of the gate:
    it regenerates the whole file rather than appending, so a fall that a partial run
    invented is indistinguishable from a deliberate one once recorded, and the gate's own
    failure message offers this command as one of two remedies. Skipping the bad tree and
    recording the rest would still leave the operator believing the baseline was
    refreshed; the remedy for an unfinished run is to run it again.

    ⚠️ **``missing_only`` exists because a single remedy for every finding is wider than
    most of them.** Of the problems `check_tree` raises, two — a tracked file the report
    measures and the baseline does not record, and a whole tree with no baseline entry —
    are fixed by adding entries, and need nothing existing to move. `--write` re-records
    the file set of **every** measured tree wholesale, so with it as the only remedy the
    narrowest available response to one new module is to accept this machine's figure for
    every other file in its tree. That is the wrong direction on a gate whose figures are
    host-sensitive: a baseline recorded on a more capable machine is a floor the gate's
    own environment cannot reach, which is what `conditions` was added for. Measured on
    the `scripts` tree while closing #1256: one file needed recording, and `--write`
    would have raised ten existing floors with it, six past the tolerance and the largest
    by 18.75 points. #1421 reports the same count taken independently on the same tree —
    ten floors again, two of them on files whose host-versus-CI discrepancy is pinned on
    purpose — after the printed remedy was followed a second time and caught by hand.
    That is why those two findings now name this mode, and the ones that genuinely need
    the wider command say that it is wider.

    **Which findings keep `--write`, and why, since the distinction is the whole point.**
    Lowering a recorded floor and dropping a stale entry are both things ``missing_only``
    cannot do by construction, so a file that fell and a recorded path the report no
    longer mentions keep `--write`. So does a baseline with no `conditions` record: a
    tree whose entry already exists is preserved verbatim here, and the conditions are
    part of that entry, so the only way to capture them is to re-record the tree. Those
    messages say that the command is the wider one rather than leaving the reader to
    discover it, because following a printed remedy without knowing what it moves is the
    defect, not the flag.

    So ``missing_only`` adds only entries the baseline **lacks**, and by construction
    cannot raise or lower an existing floor — asserted below rather than left to the
    reader, because "it only adds" is exactly the kind of claim that is true of the code
    as written and false after one edit.
    """
    trees: dict[str, dict] = {}
    measured_trees: list[str] = []
    refusals: list[str] = []
    recorded_now = load_baseline().get("trees", {})
    for tree in TREES:
        report = _resolve_report(tree)
        trust = None if report is None else run_trust(report)
        if trust is not None and trust.state == "errored":
            refusals.append(f"  ✗ {tree.name}: {trust.detail}")
            continue
        if report is None or trust is None or trust.state != "clean":
            why = "no report" if report is None else trust.detail
            print(f"  … {tree.name}: {why}, leaving its baseline untouched")
            existing = recorded_now.get(tree.name)
            if existing:
                trees[tree.name] = existing
            continue
        measured_trees.append(tree.name)
        measured = read_report(report, tree_root(tree))
        tracked = set(tracked_source_files(tree))
        files = {
            path: rate
            for path, rate in sorted(measured.items())
            if path in tracked and not any(path.startswith(k) for k in NOT_MEASURED)
        }
        if missing_only:
            existing = recorded_now.get(tree.name)
            if existing:
                kept = dict(existing.get("files", {}))
                added = {p: r for p, r in files.items() if p not in kept}
                # The whole claim of this mode, checked rather than asserted in prose.
                assert all(kept[p] == existing["files"][p] for p in kept), tree.name
                merged = {**kept, **added}
                trees[tree.name] = {**existing, "files": dict(sorted(merged.items()))}
                if added:
                    print(
                        f"  + {tree.name}: {len(added)} new entr(y/ies) "
                        + ", ".join(
                            f"{p} at {r:.2f}%" for p, r in sorted(added.items())
                        )
                    )
                else:
                    print(f"  = {tree.name}: nothing missing, left exactly as recorded")
                continue
            # A tree with no baseline at all has nothing to preserve, so the ordinary
            # full record is both correct and the only thing available.
        trees[tree.name] = {
            "total": report_total(report),
            # The conditions this figure was measured under, so a later comparison can
            # tell a file that lost lines from one whose tests did not run here. See
            # `RunTrust.skipped` for why the count is recorded and a tool list is not.
            "conditions": {"collected": trust.collected, "skipped": trust.skipped},
            "files": files,
        }
        print(
            f"  ✓ {tree.name}: {len(files)} file(s) at {report_total(report):.2f}% "
            f"({trust.collected} test(s) collected, {trust.skipped} skipped)"
        )

    if refusals:
        print(
            "\n🚫 recorded nothing. These reports came from runs that did not finish:"
        )
        for refusal in refusals:
            print(refusal)
        print(
            "\nThe numbers in them are artefacts of the run, and --write would make them"
            "\npermanent: it regenerates the whole baseline, so a fabricated fall is"
            "\nindistinguishable from a deliberate one afterwards. Re-run the suite and"
            "\nrecord from a clean report."
        )
        return 2
    if not measured_trees:
        print(
            "\n🚫 recorded nothing: no tree has a report from a finished run, so there "
            "is nothing to record.\n   Produce one with `make coverage-all`, or "
            "`make test-cicd -C lib/idp_common_pkg SKIP_INSTALL=1` for idp_common alone."
        )
        return 2

    BASELINE.write_text(
        json.dumps(
            {
                "$comment": [
                    "Per-file line coverage baselines, one entry per measurable tree.",
                    "Generated -- do not hand-edit. Regenerate with:",
                    "  make coverage-all           # produces one report per tree",
                    "  python3 scripts/check_coverage_debt.py --write",
                    "",
                    "This is the half of the coverage ratchet an aggregate floor cannot",
                    "see: a total can hold steady while one module regresses because",
                    "another grew. Coverage may rise freely; only a drop beyond the",
                    "tolerance fails.",
                    "",
                    "A tree with no report is left untouched rather than erased, so a",
                    "partial run cannot silently discard another tree's baseline.",
                    "",
                    "Each tree's `conditions` are the measurement conditions its figures",
                    "were taken under: how many tests were COLLECTED, and how many of",
                    "them SKIPPED. A figure is a property of the test set that produced",
                    "it, and a capability gate (an absent CLI, a missing optional",
                    "dependency) removes tests silently on a machine without it -- so a",
                    "baseline recorded on a more capable machine is a floor the gate's",
                    "own environment cannot reach. idp_feature_sdk was exactly that:",
                    "recorded at 99.94% where the AWS SAM CLI was installed, and",
                    "unreachable in CI, which skips 24 tests and measures 97.15%. A count",
                    "difference is never itself a failure -- it is quoted beside a fall,",
                    "so a reader can tell a file that lost lines from one whose tests",
                    "did not run.",
                ],
                "generator": "python3 scripts/check_coverage_debt.py --write",
                "tolerancePct": TOLERANCE_PCT,
                "trees": trees,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    total_files = sum(len(t.get("files", {})) for t in trees.values())
    print(
        f"✅ recorded {total_files} file(s) across {len(trees)} tree(s) into "
        f"{BASELINE.relative_to(REPO_ROOT)}"
    )
    return 0


def _print_unchecked(unchecked: list[Unchecked]) -> None:
    """Name every tree this run could not compare, with the reason, under no ✅.

    Printed on both the passing and the refusing path. A tree whose report is present but
    whose run cannot be vouched for is the case a reader is most likely to misread — a
    `coverage.xml` is sitting right there — so "nothing was measured" has to come with
    the reason that particular report was not used.
    """
    if not unchecked:
        return
    print(
        f"⏭️  not checked ({len(unchecked)} of {len(TREES)} tree(s)) — nothing below is "
        f"covered by any verdict above:"
    )
    for skip in sorted(unchecked):
        print(f"     {skip.tree}: {skip.reason}")
    print("   run `make coverage-all` to measure them")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Re-record baselines from whatever reports exist, instead of checking",
    )
    parser.add_argument(
        "--write-missing",
        action="store_true",
        help=(
            "Like --write, but ONLY adds entries the baseline lacks: it cannot raise or "
            "lower an existing floor. The bounded remedy for the two findings that need "
            "nothing but new entries — a measured file the baseline does not record, "
            "and a tree with no baseline at all — where --write would re-record every "
            "measured tree wholesale"
        ),
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print each tree's recorded figure"
    )
    parser.add_argument(
        "--require-tree",
        action="append",
        metavar="NAME",
        default=[],
        help=(
            "Fail unless this tree was actually checked (repeatable). For a caller that "
            "knows which tree it measured: the failure then names the tree, rather than "
            "saying only that nothing was checked."
        ),
    )
    parser.add_argument(
        "--require-all-trees",
        action="store_true",
        help=(
            "Fail unless EVERY tree in the registry was checked. What both CI "
            "configurations pass, now that they produce a report per tree"
        ),
    )
    args = parser.parse_args()

    if args.require_all_trees:
        # Derived from the registry, never enumerated by the caller. A Makefile or CI
        # config listing the nine names would be a second copy of TREES, and the copy
        # that goes stale is the one deciding what this gate is allowed to skip -- so a
        # tree added to the registry and forgotten there would be unratcheted while the
        # gate reported that every tree was required. Issue #1256.
        args.require_tree = [
            *args.require_tree,
            *(t.name for t in TREES if t.name not in set(args.require_tree)),
        ]

    unknown = [n for n in args.require_tree if n not in TREES_BY_NAME]
    if unknown:
        # A misspelled tree name must not read as a satisfied precondition: the whole
        # point of the flag is to name something, so naming nothing is an error.
        print(
            f"🚫 --require-tree names {len(unknown)} tree(s) that do not exist: "
            f"{', '.join(unknown)}. Known trees: {', '.join(t.name for t in TREES)}"
        )
        return 2
    if args.require_tree and (args.summary or args.write or args.write_missing):
        # Refused rather than ignored. Neither of those modes compares anything, so a
        # caller passing both has asserted a precondition that nothing will evaluate --
        # and an ignored assertion is the exact shape of the defect this flag exists for.
        # Named by the spelling the caller actually used: `--require-all-trees` expands
        # into `require_tree` above, so a message naming only the latter would cite a
        # flag that is not on the command line.
        flag = "--require-all-trees" if args.require_all_trees else "--require-tree"
        print(
            f"🚫 {flag} asserts that a tree was CHECKED, which neither --summary "
            "nor --write does. Drop one of them."
        )
        return 2

    if args.summary:
        baseline = load_baseline()
        trees = baseline.get("trees", {})
        if not trees:
            print("no coverage baseline recorded yet")
            return 0
        print(f"{'TREE':<26} {'FILES':>7} {'RECORDED':>9}")
        for name, data in sorted(trees.items(), key=lambda kv: -kv[1].get("total", 0)):
            print(
                f"{name:<26} {len(data.get('files', {})):>7} "
                f"{data.get('total', 0):>8.2f}%"
            )
        return 0

    if args.write and args.write_missing:
        # Refused rather than resolved in either direction: one re-records every file of
        # every measured tree and the other cannot change a single existing figure, so
        # there is no reading of both that is a narrowing of one of them.
        print(
            "🚫 --write re-records every measured tree wholesale and --write-missing "
            "adds only what is absent. Pick one."
        )
        return 2
    if args.write or args.write_missing:
        return write_baseline(missing_only=args.write_missing)

    result = check(tuple(args.require_tree))
    code, problems, unchecked = result.code, result.problems, result.unchecked
    if code:
        if code == 2 and not result.checked:
            # Deliberately not the ❌ heading: this run reached no verdict, which is a
            # different thing from finding a regression, and a reader scanning a job log
            # has to be able to tell them apart. Issue #1190.
            print(
                f"🚫 coverage ratchet: no verdict — this run measured nothing. "
                f"{len(problems)} problem(s):\n"
            )
        elif code == 2:
            # Some trees WERE measured, so "nothing was measured" would be false here,
            # and falsely reassuring in the worst direction: a reader would take the real
            # regression printed below for another artefact of the unfinished run.
            print(
                f"🚫 coverage ratchet: no verdict for the whole tree set — "
                f"{len(problems)} problem(s). "
                f"{len(result.checked)} tree(s) WERE measured "
                f"({', '.join(sorted(result.checked))}), so a finding naming one of them "
                f"below is a real regression rather than an artefact:\n"
            )
        else:
            print(f"❌ coverage ratchet: {len(problems)} problem(s)\n")
        for problem in problems:
            print(f"  - {problem}\n")
        _print_unchecked(unchecked)
        print(
            "This gate reports; it does not decide. See "
            "scripts/check_coverage_debt.py's module docstring for what each means."
        )
        return code

    baseline = load_baseline()
    files = sum(
        len(baseline.get("trees", {}).get(n, {}).get("files", {}))
        for n in result.checked
    )
    print(
        f"✅ coverage ratchet: {files} file(s) across {len(result.checked)} tree(s) at or "
        f"above their recorded coverage"
    )
    _print_unchecked(unchecked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
