#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Produce one coverage report per measurable tree, for the ratchet to read.

One invocation per tree, not one over the repository. Several packages ship their own
``tests/conftest.py`` and pytest imports them all as the module ``tests.conftest``, so a
single run across two roots is an import collision —
:mod:`scripts.run_all_tests` shells out per root for the same reason.

Each tree's report lands at ``<tree>/test-reports/coverage-<name>.xml``, which is where
``scripts/check_coverage_debt.py`` looks for it.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "check_coverage_debt", REPO_ROOT / "scripts" / "check_coverage_debt.py"
)
assert _spec and _spec.loader
_ccd = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage_debt"] = _ccd
_spec.loader.exec_module(_ccd)


def run(tree, python: str, parallel: bool) -> tuple[str, int, float | None]:
    report = _ccd.report_path(tree)
    report.parent.mkdir(parents=True, exist_ok=True)
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if parallel:
        cmd += ["-n", "auto"]
    cmd += [
        f"--cov={tree.cov}",
        f"--cov-report=xml:{report}",
        "--cov-report=",
        *tree.args,
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT / tree.cwd)
    total = None
    if report.is_file():
        try:
            total = _ccd.report_total(report)
        except Exception:  # pragma: no cover - malformed report
            total = None
    # 5 == no tests collected for the marker, which is not a failure of this script.
    return tree.name, (0 if result.returncode in (0, 5) else result.returncode), total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", action="append", help="measure just this tree (repeatable)"
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

    failures, results = [], []
    for tree in trees:
        print(
            f"\n=== coverage: {tree.name} ({tree.cwd}, --cov={tree.cov}) ===",
            flush=True,
        )
        # A tree that declares `serial` is never run in parallel, whatever the flag
        # says: for those, `-n auto` does not just cost time, it reports coverage that is
        # wrong in a way nothing downstream can detect. `--serial` can force the rest
        # serial too, but it cannot force a serial tree parallel.
        parallel = not args.serial and not tree.serial
        name, code, total = run(tree, python, parallel=parallel)
        results.append((name, total))
        if code:
            failures.append(name)

    print("\n" + "=" * 62)
    for name, total in results:
        print(
            f"  {name:<26} {total:>6.2f}%"
            if total is not None
            else f"  {name:<26}  (no report)"
        )
    if failures:
        print(
            f"\n⚠️  tests failed in: {', '.join(failures)} — the reports above may be partial"
        )
    print("\nNow run: python3 scripts/check_coverage_debt.py")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
