#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ratchet test coverage across every measurable tree: per file, and in aggregate.

`idp_common` coverage was 67% when the effort that produced this began. A single number
is easy to raise once and easy to lose quietly, which is why this gate has two halves
that fail on different things.

**It covers nine trees, not one.** Measuring them was the point: nobody knew `scripts/`
was already at 81% across 33,000 statements, or that
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
        # Measured serially, which is what its recorded figures are. The parallel run
        # is not meaningfully faster on this tree anyway -- one root cannot use every
        # worker -- and the coverage of the hooks its suites run as subprocesses is
        # unaffected by the worker count either way. See `Tree.serial`.
        serial=True,
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
    """
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
    return RunTrust("clean", f"{record.name} records {tests} test(s), 0 errors")


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


def check_tree(
    tree: Tree, recorded: dict[str, float]
) -> tuple[list[str], float | None]:
    """Compare one tree against its recorded baseline. Returns (problems, overall)."""
    report = _resolve_report(tree)
    if report is None:
        return [], None
    measured = read_report(report, tree_root(tree))
    problems: list[str] = []

    for path, was in sorted(recorded.items()):
        now = measured.get(path)
        if now is None:
            problems.append(
                f"[{tree.name}] {path} is recorded at {was:.2f}% but the report does not "
                f"mention it. If it moved or was deleted, re-record with --write; if it "
                f"stopped being imported by any test, that is the regression."
            )
            continue
        if now < was - TOLERANCE_PCT:
            problems.append(
                f"[{tree.name}] {path} fell from {was:.2f}% to {now:.2f}% "
                f"(-{was - now:.2f} points, tolerance {TOLERANCE_PCT}). Add tests for the "
                f"lines it lost, or re-record deliberately with --write."
            )

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
            + ". Re-record with --write."
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
        tree_problems, overall = check_tree(tree, recorded)
        if overall is None:  # pragma: no cover - the report resolved a moment ago
            unchecked.append(Unchecked(tree.name, "its report disappeared mid-run"))
            continue
        checked.append(tree.name)
        if not recorded:
            problems.append(
                f"[{tree.name}] a report exists but this tree has no recorded baseline, "
                f"so nothing about it is ratcheted. Re-record with --write."
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


def write_baseline() -> int:
    """Re-record baselines from the reports that a finished run produced.

    Refuses outright — writing nothing — if any report came from a run that did not
    finish, or if no report is recordable at all. This is the dangerous half of the gate:
    it regenerates the whole file rather than appending, so a fall that a partial run
    invented is indistinguishable from a deliberate one once recorded, and the gate's own
    failure message offers this command as one of two remedies. Skipping the bad tree and
    recording the rest would still leave the operator believing the baseline was
    refreshed; the remedy for an unfinished run is to run it again.
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
        trees[tree.name] = {"total": report_total(report), "files": files}
        print(f"  ✓ {tree.name}: {len(files)} file(s) at {report_total(report):.2f}%")

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
        # config listing the nine names would be a second copy of the registry, and the
        # copy that goes stale is the one that decides what this gate is allowed to skip --
        # so a tree added to TREES and forgotten there would be unratcheted while the gate
        # reported that every tree was required. Issue #1256.
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
    if args.require_tree and (args.summary or args.write):
        # Refused rather than ignored. Neither of those modes compares anything, so a
        # caller passing both has asserted a precondition that nothing will evaluate --
        # and an ignored assertion is the exact shape of the defect this flag exists for.
        print(
            "🚫 --require-tree asserts that a tree was CHECKED, which neither --summary "
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

    if args.write:
        return write_baseline()

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
