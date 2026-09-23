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

## What it is not

Not a blocking gate. It reports precisely — file, before, after, delta, remedy — and the
decision about whether that blocks a merge belongs to whoever is merging. Neither
`develop` nor `main` is branch-protected in this repository, which is
[deliberate](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933),
so a red result here informs rather than refuses.

## Where the report comes from

This script re-measures nothing: it reads the coverage XML that the normal
whole-package run (`make test-cicd -C lib/idp_common_pkg`) writes to
`lib/idp_common_pkg/test-reports/coverage.xml`.

⚠️ **Check that run succeeded before believing a drop reported here.** A partially
failed pytest still writes a report, and a report from a run where some xdist workers
errored shows large, uniform-looking falls across unrelated files — the tests that would
have covered them never executed. Two symptoms to recognise, because both have been
mistaken for a real regression: `Different tests were collected between gw1 and gwN`
(usually because a test file was edited while the run was in flight), and a file whose
own suite you know to be green reported far below its baseline. Re-run before recording
anything.

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


def check() -> tuple[int, list[str], list[str]]:
    """Check every tree with a report. Returns (exit code, problems, skipped names)."""
    baseline = load_baseline()
    trees = baseline.get("trees", {})
    problems: list[str] = []
    skipped: list[str] = []
    for tree in TREES:
        recorded = trees.get(tree.name, {}).get("files", {})
        tree_problems, overall = check_tree(tree, recorded)
        if overall is None:
            skipped.append(tree.name)
            continue
        if not recorded:
            problems.append(
                f"[{tree.name}] a report exists but this tree has no recorded baseline, "
                f"so nothing about it is ratcheted. Re-record with --write."
            )
        problems.extend(tree_problems)
    return (1 if problems else 0), problems, skipped


def write_baseline() -> None:
    trees: dict[str, dict] = {}
    for tree in TREES:
        report = _resolve_report(tree)
        if report is None:
            print(f"  … {tree.name}: no report, leaving its baseline untouched")
            existing = load_baseline().get("trees", {}).get(tree.name)
            if existing:
                trees[tree.name] = existing
            continue
        measured = read_report(report, tree_root(tree))
        tracked = set(tracked_source_files(tree))
        files = {
            path: rate
            for path, rate in sorted(measured.items())
            if path in tracked and not any(path.startswith(k) for k in NOT_MEASURED)
        }
        trees[tree.name] = {"total": report_total(report), "files": files}
        print(f"  ✓ {tree.name}: {len(files)} file(s) at {report_total(report):.2f}%")

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
    args = parser.parse_args()

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
        write_baseline()
        return 0

    code, problems, skipped = check()
    if code:
        print(f"❌ coverage ratchet: {len(problems)} problem(s)\n")
        for problem in problems:
            print(f"  - {problem}\n")
        print(
            "This gate reports; it does not decide. See "
            "scripts/check_coverage_debt.py's module docstring for what each means."
        )
        return 1

    baseline = load_baseline()
    checked = [t.name for t in TREES if t.name not in skipped]
    files = sum(
        len(baseline.get("trees", {}).get(n, {}).get("files", {})) for n in checked
    )
    print(
        f"✅ coverage ratchet: {files} file(s) across {len(checked)} tree(s) at or above "
        f"their recorded coverage"
    )
    if skipped:
        # Named rather than silent: a skipped tree is an unchecked tree, and a gate that
        # passes while checking nothing is the failure this whole effort is about.
        print(
            f"   not checked (no report from this run): {', '.join(sorted(skipped))} — "
            f"run `make coverage-all` to produce them"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
