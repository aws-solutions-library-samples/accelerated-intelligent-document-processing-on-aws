#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Print one measured tree's test coverage as a readable table, worst-covered first.

`pytest --cov`'s own terminal report is alphabetical and several hundred lines long, which
answers "what is the coverage of this file" but not "where should the next test go". This
sorts by **uncovered statements descending**, because that is the quantity that actually
moves the overall figure: a 23%-covered 195-statement module is worth less attention than a
80%-covered 2,320-statement one, and an alphabetical list makes them look equivalent.

It also shows each file's delta against `scripts/coverage_debt.json`, so a regression is
visible in the same view rather than needing a separate run of the ratchet.

Reads a Cobertura XML report; it does not run the tests. `make coverage` runs `idp_common`'s
suite first, and `make coverage-all` runs every tree. Which tree is tabulated comes from
`--tree`, whose choices are the registry in `check_coverage_debt.py` rather than a second
list — there are nine measured trees, and a table that could only ever show one of them
would report the other eight as having no coverage at all.
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
# copies of that logic would be two chances to get it wrong. The tree registry is imported
# for the same reason -- a local list of trees here would be a second answer to "what is
# measured", and it would be the one that goes stale.
from check_coverage_debt import (  # noqa: E402
    BASELINE,
    TREES,
    TREES_BY_NAME,
    _resolve_report,
    read_report,
    report_total,
    tree_root,
)


def _baseline(tree_name: str) -> tuple[dict[str, float], float]:
    """One tree's recorded per-file rates, and its recorded overall figure."""
    if not BASELINE.is_file():
        return {}, 0.0
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    entry = data.get("trees", {}).get(tree_name, {})
    return entry.get("files", {}), entry.get("total", 0.0)


def _statement_counts(report: Path, base: Path) -> dict[str, tuple[int, int]]:
    """(statements, uncovered) per file, counted from the report's own line elements.

    Cobertura carries a line-rate but not a statement count, so the lines are counted
    directly. Deriving the count from the rate would be circular and would round.

    ``base`` is the tree's root, which is what makes the keys comparable with the
    baseline's. It is a parameter rather than a constant because each tree has a different
    one; hardcoding a single package root here attributed every other tree's files to
    absolute paths that matched nothing in the baseline.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(report)
    roots = [Path(s.text) for s in tree.iter("source") if s.text]
    counts: dict[str, tuple[int, int]] = {}
    for cls in tree.iter("class"):
        filename = cls.get("filename")
        if filename is None:
            continue
        key = filename
        for source in roots:
            try:
                key = str((source / filename).resolve().relative_to(base))
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
    parser.add_argument(
        "--tree",
        default="idp_common",
        choices=[t.name for t in TREES],
        help="which measured tree to tabulate (default: idp_common)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="a specific Cobertura report; by default the one this tree's last run wrote",
    )
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
        help="hide files below this many statements, which crowd the list at 0%%",
    )
    args = parser.parse_args()

    tree = TREES_BY_NAME[args.tree]
    report = args.report or _resolve_report(tree)
    if report is None or not report.is_file():
        # An error, unlike in the ratchet, which runs in sequences that may legitimately
        # not have built a report. This is a command someone typed to see a table:
        # printing nothing and exiting 0 would read as "no coverage" rather than "not
        # measured yet".
        print(
            f"No coverage report for {tree.name}"
            + (f" at {report}" if report else "")
            + ".\nRun `make coverage-all` (every tree) or "
            f"`python3 scripts/coverage_all.py --only {tree.name}`.",
            file=sys.stderr,
        )
        return 1

    base = tree_root(tree)
    rates = read_report(report, base)
    counts = _statement_counts(report, base)
    recorded, recorded_total = _baseline(tree.name)

    rows = [
        (path, counts.get(path, (0, 0))[0], counts.get(path, (0, 0))[1], rate)
        for path, rate in rates.items()
        if counts.get(path, (0, 0))[0] >= args.min_statements
    ]
    rows.sort(key=lambda r: (-r[2], r[0]))
    shown = rows if args.top == 0 else rows[: args.top]

    total_stmts = sum(r[1] for r in rows)
    total_missed = sum(r[2] for r in rows)
    overall = report_total(report)

    print()
    print(
        f"{tree.name} test coverage — {overall:.2f}% overall "
        f"({total_stmts:,} statements, {total_missed:,} uncovered)"
    )
    if recorded:
        drift = overall - recorded_total
        print(
            f"recorded baseline {recorded_total:.2f}%  ({drift:+.2f} points)  "
            f"— `make check-coverage-debt` is the gate"
        )
    print()
    print(f"{'MODULE':<58} {'STMTS':>7} {'MISS':>6} {'COVER':>7} {'vs BASE':>9}")
    print("-" * 91)
    for path, stmts, missed, rate in shown:
        was = recorded.get(path)
        if was is None:
            # Distinct from "=": an unrecorded file is unratcheted, which is a different
            # situation from one that has not moved.
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
