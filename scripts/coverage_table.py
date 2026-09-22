#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Print `idp_common` test coverage as a readable table, worst-covered first.

`pytest --cov`'s own terminal report is alphabetical and several hundred lines long, which
answers "what is the coverage of this file" but not "where should the next test go". This
sorts by **uncovered statements descending**, because that is the quantity that actually
moves the overall figure: a 23%-covered 195-statement module is worth less attention than a
80%-covered 2,320-statement one, and an alphabetical list makes them look equivalent.

It also shows each file's delta against `scripts/coverage_debt.json`, so a regression is
visible in the same view rather than needing a separate run of the ratchet.

Reads a Cobertura XML report; it does not run the tests. `make coverage` runs them first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Reuse the report parser rather than writing a second one: its `<source>`-relative
# normalisation is the detail that silently broke the ratchet's first version, and two
# copies of that logic would be two chances to get it wrong.
from check_coverage_debt import (  # noqa: E402
    BASELINE,
    DEFAULT_REPORT,
    read_report,
    report_total,
)


def _baseline() -> dict[str, float]:
    if not BASELINE.is_file():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8")).get("files", {})


def _statement_counts(report: Path) -> dict[str, tuple[int, int]]:
    """(statements, uncovered) per file, counted from the report's own line elements.

    Cobertura carries a line-rate but not a statement count, so the lines are counted
    directly. Deriving the count from the rate would be circular and would round.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(report)
    roots = [Path(s.text) for s in tree.iter("source") if s.text]
    package_root = (REPO_ROOT / "lib" / "idp_common_pkg").resolve()
    counts: dict[str, tuple[int, int]] = {}
    for cls in tree.iter("class"):
        filename = cls.get("filename")
        if filename is None:
            continue
        key = filename
        for root in roots:
            try:
                key = str((root / filename).resolve().relative_to(package_root))
                break
            except ValueError:
                continue
        lines = list(cls.iter("line"))
        total = len(lines)
        missed = sum(1 for line in lines if line.get("hits") == "0")
        counts[key] = (total, missed)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="how many files to list, worst-covered first (0 = all)",
    )
    parser.add_argument(
        "--min-statements",
        type=int,
        default=0,
        help="hide files smaller than this, which dominate the list at 0%% and rarely matter",
    )
    args = parser.parse_args()

    if not args.report.is_file():
        print(
            f"No coverage report at {args.report}.\n"
            f"Run `make coverage`, which measures first and then prints this table.",
            file=sys.stderr,
        )
        return 1

    rates = read_report(args.report)
    counts = _statement_counts(args.report)
    base = _baseline()

    rows = [
        (path, counts.get(path, (0, 0))[0], counts.get(path, (0, 0))[1], rate)
        for path, rate in rates.items()
        if counts.get(path, (0, 0))[0] >= args.min_statements
    ]
    rows.sort(key=lambda r: (-r[2], r[0]))
    shown = rows if args.top == 0 else rows[: args.top]

    total_stmts = sum(r[1] for r in rows)
    total_missed = sum(r[2] for r in rows)
    overall = report_total(args.report)

    print()
    print(
        f"idp_common test coverage — {overall:.2f}% overall "
        f"({total_stmts:,} statements, {total_missed:,} uncovered)"
    )
    if base:
        recorded = json.loads(BASELINE.read_text(encoding="utf-8")).get("total", 0.0)
        drift = overall - recorded
        sign = "+" if drift >= 0 else ""
        print(
            f"recorded baseline {recorded:.2f}%  ({sign}{drift:.2f} points)  "
            f"— `make check-coverage-debt` is the gate"
        )
    print()
    print(f"{'MODULE':<58} {'STMTS':>7} {'MISS':>6} {'COVER':>7} {'vs BASE':>9}")
    print("-" * 91)
    for path, stmts, missed, rate in shown:
        was = base.get(path)
        if was is None:
            delta = "new"
        elif abs(rate - was) < 0.005:
            delta = "="
        else:
            delta = f"{rate - was:+.2f}"
        print(f"{path:<58} {stmts:>7,} {missed:>6,} {rate:>6.0f}% {delta:>9}")
    print("-" * 91)
    print(f"{'TOTAL':<58} {total_stmts:>7,} {total_missed:>6,} {overall:>6.0f}%")
    if args.top and len(rows) > args.top:
        print(
            f"\n{len(rows) - args.top} more file(s) not shown — `--top 0` for all, "
            f"sorted by uncovered statements descending."
        )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
