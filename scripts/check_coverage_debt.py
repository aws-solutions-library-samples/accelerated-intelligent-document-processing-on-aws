#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ratchet `idp_common`'s test coverage: per file, and in aggregate.

Coverage was 67% when the effort that produced this began and is measured per release
since. A single number is easy to raise once and easy to lose quietly, which is why this
gate has two halves that fail on different things.

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

## Why coverage is measured serially

A submodule-scoped `--cov` under `pytest -n auto` produces spurious failures on this
package, because `idp_common/__init__.py` caches lazily-imported submodules privately and
a patch can land on a different module object than the one under test (issue #1159). This
script reads a coverage XML report produced by the normal whole-package run, so it does
not re-measure and is unaffected — but anyone regenerating the baseline by hand should
know which flag combinations are trustworthy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "lib" / "idp_common_pkg"
BASELINE = REPO_ROOT / "scripts" / "coverage_debt.json"
DEFAULT_REPORT = PACKAGE_ROOT / "test-reports" / "coverage.xml"

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


def tracked_source_files() -> list[str]:
    """Every tracked `.py` under `idp_common`, as package-relative paths."""
    out = subprocess.run(
        ["git", "ls-files", "lib/idp_common_pkg/idp_common/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    prefix = "lib/idp_common_pkg/"
    return sorted(p[len(prefix) :] for p in out if p.startswith(prefix))


def read_report(report: Path) -> dict[str, float]:
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
        for root in roots:
            candidate = (root / filename).resolve()
            try:
                key = str(candidate.relative_to(PACKAGE_ROOT.resolve()))
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


def check(report: Path) -> tuple[int, list[str]]:
    """Compare a report against the baseline. Returns (exit code, problem lines)."""
    baseline = load_baseline()
    recorded: dict[str, float] = baseline.get("files", {})
    measured = read_report(report)
    problems: list[str] = []

    for path, was in sorted(recorded.items()):
        now = measured.get(path)
        if now is None:
            problems.append(
                f"{path} is recorded at {was:.2f}% but the report does not mention it. "
                f"If it moved or was deleted, re-record with --write; if it stopped "
                f"being imported by any test, that is the regression."
            )
            continue
        if now < was - TOLERANCE_PCT:
            problems.append(
                f"{path} fell from {was:.2f}% to {now:.2f}% "
                f"(-{was - now:.2f} points, tolerance {TOLERANCE_PCT}). Add tests for "
                f"the lines it lost, or re-record deliberately with --write."
            )

    # Universe closure: a new module must be measured, exempt, or explicitly recorded.
    unaccounted = [
        path
        for path in tracked_source_files()
        if path not in recorded
        and path in measured
        and not any(path.startswith(k) for k in NOT_MEASURED)
    ]
    if unaccounted:
        problems.append(
            f"{len(unaccounted)} source file(s) are measured but not in the baseline, so "
            f"their coverage is unratcheted: "
            + ", ".join(unaccounted[:8])
            + ("..." if len(unaccounted) > 8 else "")
            + ". Re-record with --write."
        )

    return (1 if problems else 0), problems


def write_baseline(report: Path) -> None:
    measured = read_report(report)
    tracked = set(tracked_source_files())
    files = {
        path: rate
        for path, rate in sorted(measured.items())
        if path in tracked and not any(path.startswith(k) for k in NOT_MEASURED)
    }
    BASELINE.write_text(
        json.dumps(
            {
                "$comment": [
                    "Per-file line coverage baseline for idp_common. Generated -- do not",
                    "hand-edit. Regenerate with:",
                    "  make test-cicd -C lib/idp_common_pkg SKIP_INSTALL=1",
                    "  python3 scripts/check_coverage_debt.py --write",
                    "",
                    "This is the half of the coverage ratchet that the aggregate",
                    "--cov-fail-under cannot see: the total can hold steady while one",
                    "module regresses, because another grew at the same time.",
                    "",
                    "Coverage may rise freely; only a drop beyond the tolerance fails.",
                ],
                "generator": "python3 scripts/check_coverage_debt.py --write",
                "tolerancePct": TOLERANCE_PCT,
                "total": report_total(report),
                "files": files,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"✅ recorded {len(files)} file(s) at {report_total(report):.2f}% overall "
        f"into {BASELINE.relative_to(REPO_ROOT)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Cobertura XML coverage report (default: the one make test-cicd writes)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Re-record the baseline from the report instead of checking against it",
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print the recorded total and file count"
    )
    args = parser.parse_args()

    if args.summary:
        baseline = load_baseline()
        print(
            f"coverage baseline: {len(baseline.get('files', {}))} file(s), "
            f"{baseline.get('total', 0):.2f}% overall"
        )
        return 0

    if not args.report.is_file():
        # Absence is not a failure: the report is a build artifact, and this gate runs in
        # sequences that may not have produced one. Saying so beats a misleading pass.
        print(
            f"ℹ️  no coverage report at {args.report} — nothing to check. Produce one "
            f"with: make test-cicd -C lib/idp_common_pkg SKIP_INSTALL=1"
        )
        return 0

    if args.write:
        write_baseline(args.report)
        return 0

    code, problems = check(args.report)
    if code:
        print(f"❌ coverage ratchet: {len(problems)} problem(s)\n")
        for problem in problems:
            print(f"  - {problem}\n")
        print(
            "This gate reports; it does not decide. See scripts/check_coverage_debt.py's "
            "module docstring for what each failure means."
        )
        return 1

    baseline = load_baseline()
    print(
        f"✅ coverage ratchet: {len(baseline.get('files', {}))} file(s) at or above "
        f"their recorded coverage; overall {report_total(args.report):.2f}% "
        f"(recorded {baseline.get('total', 0):.2f}%)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
