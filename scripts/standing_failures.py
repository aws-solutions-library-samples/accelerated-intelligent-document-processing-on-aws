# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The declared standing-failure baseline, and the comparison against a real run.

``.claude/skills/full-test-battery.md`` carries one table of accepted test
failures and one stated count. Both used to be prose that nothing measured: the
only check on them was that the count equalled the number of rows, so the
document could assert a clean baseline while the tree was red and its own gate
would agree (#1095).

This module is the half that makes the claim falsifiable. It parses the table
into node ids and compares that set against the failures a run actually
observed, in **both** directions:

* a failure nobody declared — the tree is red and the baseline does not say so;
* a declared failure that did not occur — the row has outlived its cause and is
  now a standing waiver over a test that passes.

``scripts/run_all_tests.py`` calls it with the results of the run it just
performed, so there is no stored artifact between the measurement and the
comparison and therefore no staleness rule to get wrong. The JSON and JUnit
files that run writes under ``test-reports/`` are outputs for a reader to
inspect; nothing reads them back to decide a verdict. That is deliberate — a
gate that reads a results directory passes when the directory is empty or stale
unless something else proves the directory is fresh, which is the same defect
one level up.

``scripts/tests/test_standing_failure_baseline.py`` uses the same parser to
assert the table stays machine-comparable, so a row written as prose fails there
rather than being silently skipped by the comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BATTERY = REPO_ROOT / ".claude" / "skills" / "full-test-battery.md"

BEGIN = "<!-- STANDING-FAILURES-BEGIN -->"
END = "<!-- STANDING-FAILURES-END -->"

# Every document states the baseline in this one form. A number is required
# rather than prose on purpose: "some tests may fail" is unfalsifiable, which is
# worse than a wrong claim, because every future failure is then consistent with
# the documentation.
COUNT_RE = re.compile(r"\*\*Expected standing failures:\s*(\d+)\.?\*\*")

# A pytest node id: a path ending .py, then one or more ::-separated names. The
# table's Test cell must hold one, because that is what a run reports and so the
# only form the two sides can be compared in. Backticks around it are optional.
NODE_ID_RE = re.compile(r"(?P<path>[\w./-]+\.py)(?P<names>(?:::[^\s|`]+)+)")


class BaselineError(Exception):
    """The table cannot be read, so no verdict about it is possible."""


@dataclass(frozen=True)
class DeclaredFailure:
    """One row of the accepted-failure table."""

    node_id: str
    suite: str
    mode: str
    cause: str
    date: str
    row: str

    @property
    def path(self) -> str:
        return self.node_id.split("::", 1)[0]


@dataclass(frozen=True)
class Verdict:
    """The two-directional comparison of observed failures against declared ones."""

    observed: tuple[str, ...]
    declared: tuple[str, ...]
    unexpected: tuple[str, ...]
    resolved: tuple[str, ...]

    @property
    def agrees(self) -> bool:
        return not self.unexpected and not self.resolved


def table_rows(text: str | None = None) -> list[str]:
    """The data rows of the accepted-failure table, header and rule excluded."""
    text = battery_text() if text is None else text
    if BEGIN not in text or END not in text:
        raise BaselineError(
            f"{BATTERY.relative_to(REPO_ROOT)} must delimit its accepted-failure "
            f"table with {BEGIN} / {END}. Those markers are how the table is found "
            "without pattern-matching prose; do not remove them."
        )
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]
    rows = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(rows) < 2:
        raise BaselineError(
            "the accepted-failure table must keep its header and separator rows "
            "even when it is empty — an empty table is the statement that there "
            "are no accepted failures, and it has to be visibly a table to say so"
        )
    # rows[0] is the header, rows[1] the |---|---| separator.
    return rows[2:]


def battery_text() -> str:
    if not BATTERY.is_file():
        raise BaselineError(f"{BATTERY.relative_to(REPO_ROOT)} is missing")
    return BATTERY.read_text(encoding="utf-8")


def stated_count(path: Path = BATTERY) -> int:
    """The number the given document states, as a number."""
    if not path.is_file():
        raise BaselineError(f"{path} is missing")
    match = COUNT_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise BaselineError(
            f"{path.relative_to(REPO_ROOT)} must state the baseline as "
            "'**Expected standing failures: N**'. A reader who cannot find the "
            "number will assume whichever number suits them, which is how three "
            "documents came to deny a failure that reproduced every day (#974)."
        )
    return int(match.group(1))


def parse_node_id(cell: str) -> str | None:
    """Return the pytest node id a table cell holds, or None if it holds none."""
    match = NODE_ID_RE.search(cell)
    return None if match is None else match.group(0)


def declared_failures(text: str | None = None) -> list[DeclaredFailure]:
    """Parse the accepted-failure table. Raises if a row is not comparable.

    A row that does not yield a node id is an error rather than a skipped row:
    silently ignoring it would let a waiver sit in the table while the failure it
    describes still counts as unexpected, and the reader would have no way to
    tell which rows are load-bearing.
    """
    parsed: list[DeclaredFailure] = []
    for row in table_rows(text):
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 5:
            raise BaselineError(
                "row is missing columns (want suite | test | failure mode | cause "
                f"| date): {row}"
            )
        suite, test, mode, cause, date = cells[:5]
        node_id = parse_node_id(test) or parse_node_id(f"{suite} {test}")
        if node_id is None:
            raise BaselineError(
                "row names no pytest node id, so no run can be compared against "
                f"it — write it as `path/to/test_x.py::test_name`: {row}"
            )
        parsed.append(
            DeclaredFailure(
                node_id=node_id,
                suite=suite,
                mode=mode,
                cause=cause,
                date=date,
                row=row,
            )
        )
    return parsed


def compare(
    observed: set[str] | frozenset[str], declared: set[str] | frozenset[str]
) -> Verdict:
    """Compare an observed failing set with the declared one, both directions."""
    return Verdict(
        observed=tuple(sorted(observed)),
        declared=tuple(sorted(declared)),
        unexpected=tuple(sorted(observed - set(declared))),
        resolved=tuple(sorted(set(declared) - set(observed))),
    )


def describe(verdict: Verdict) -> str:
    """The message a runner prints. States what to do about each direction."""
    lines: list[str] = []
    if verdict.unexpected:
        lines.append(
            f"❌ {len(verdict.unexpected)} failure(s) the baseline does not declare:"
        )
        lines += [f"     {node}" for node in verdict.unexpected]
        lines.append(
            "   Fix them. They are regressions until proved otherwise — see "
            ".claude/skills/full-test-battery.md for the three checks that prove "
            "it (install, order, pristine develop). If one is genuinely accepted, "
            "add a row to the accepted-failure table there naming its node id, "
            "failure mode, verified cause and date, and bump the stated count."
        )
    if verdict.resolved:
        lines.append(
            f"❌ {len(verdict.resolved)} declared standing failure(s) did not fail "
            "in this run:"
        )
        lines += [f"     {node}" for node in verdict.resolved]
        lines.append(
            "   Delete the row from the accepted-failure table in "
            ".claude/skills/full-test-battery.md and lower the stated count. A row "
            "over a passing test is a waiver that will wave through the next real "
            "failure in that file."
        )
    if not lines:
        if verdict.declared:
            lines.append(
                f"✅ the {len(verdict.declared)} declared standing failure(s) are "
                "exactly the failures observed."
            )
        else:
            lines.append("✅ no failures, and the baseline declares none.")
    return "\n".join(lines)
