# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep the documented standing-failure baseline honest and self-consistent.

Three documents tell a reader what a green tree looks like:

* ``docs/testing.md`` — the published test map;
* ``.claude/skills/full-test-battery.md`` — the procedure, and the one place the
  accepted-failure list lives;
* ``.claude/skills/release-validation.md`` — the release checklist row for
  ``make test``.

All three asserted "there is no standing failure set" while
``feature-platform/pii-anonymizer/feature-api/tests/test_handler.py::test_report_list_and_aggregate``
failed on any machine with an assume-role ``AWS_PROFILE`` (#974). Nothing noticed,
because nothing tied the claim to anything checkable — the same shape as the CI
parity gap (``test_ci_gate_parity.py``): a fact repeated in three prose paragraphs
and enforced nowhere.

So this asserts the cheap, non-flaky half of the property. It does **not** run the
battery — a whole-battery-in-a-test would take half an hour, need a correctly
installed tree, and be exactly the kind of gate people learn to ignore. What it does
instead is make the *documentation* internally consistent and falsifiable:

* the accepted-failure table in ``full-test-battery.md`` exists, between markers, so
  it can be found mechanically rather than by prose match;
* the count stated in that section's heading equals the number of rows in it;
* all three documents state the same count, so a real standing failure can no longer
  be recorded in one place and denied in two others;
* every row that is present names a test file that exists and carries a date, so a
  row cannot rot into a permanent waiver for a file nobody has looked at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BATTERY = REPO_ROOT / ".claude" / "skills" / "full-test-battery.md"
TESTING_DOC = REPO_ROOT / "docs" / "testing.md"
RELEASE_VALIDATION = REPO_ROOT / ".claude" / "skills" / "release-validation.md"

BEGIN = "<!-- STANDING-FAILURES-BEGIN -->"
END = "<!-- STANDING-FAILURES-END -->"

# Every document states the baseline in this one form. A number is required rather
# than prose on purpose: "some tests may fail" is unfalsifiable, which is worse than a
# wrong claim, because every future failure is then consistent with the documentation.
COUNT_RE = re.compile(r"\*\*Expected standing failures:\s*(\d+)\.?\*\*")

# Every document that states the baseline. `full-test-battery.md` also carries the
# list, so it is authoritative; the other two must state the same number.
DOCS_STATING_THE_COUNT = (BATTERY, TESTING_DOC, RELEASE_VALIDATION)


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _table_rows() -> list[str]:
    """The data rows of the accepted-failure table, header and rule excluded."""
    text = _read(BATTERY)
    assert BEGIN in text and END in text, (
        f"{BATTERY.relative_to(REPO_ROOT)} must delimit its accepted-failure table "
        f"with {BEGIN} / {END}. Those markers are how this gate finds the table "
        "without pattern-matching prose; do not remove them."
    )
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]
    rows = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("|")]
    assert len(rows) >= 2, (
        "the accepted-failure table must keep its header and separator rows even when "
        "it is empty — an empty table is the statement that there are no accepted "
        "failures, and it has to be visibly a table to say that"
    )
    # rows[0] is the header, rows[1] the |---|---| separator.
    return rows[2:]


def _stated_count(path: Path = BATTERY) -> int:
    match = COUNT_RE.search(_read(path))
    assert match, (
        f"{path.relative_to(REPO_ROOT)} must state the baseline as "
        "'**Expected standing failures: N**' so it can be compared with the table in "
        f"{BATTERY.relative_to(REPO_ROOT)}. A reader who cannot find the number will "
        "assume whichever number suits them, which is how three documents came to "
        "deny a failure that was reproducing every day (#974)."
    )
    return int(match.group(1))


def test_stated_count_matches_the_table() -> None:
    """The number in the heading is the number of rows. Nothing else."""
    rows = _table_rows()
    assert _stated_count() == len(rows), (
        f"{BATTERY.relative_to(REPO_ROOT)} says 'Expected standing failures: "
        f"{_stated_count()}' but its table holds {len(rows)} row(s). Whichever you "
        "changed, change the other: a count that disagrees with the list is how a "
        "standing failure gets silently added or silently forgotten.\n"
        + "\n".join(rows)
    )


def test_all_three_documents_state_the_same_count() -> None:
    """One number, three places. A disagreement is the #974 failure mode exactly."""
    authoritative = _stated_count(BATTERY)
    for path in DOCS_STATING_THE_COUNT:
        assert _stated_count(path) == authoritative, (
            f"{path.relative_to(REPO_ROOT)} states "
            f"{_stated_count(path)} standing failure(s) but "
            f"{BATTERY.relative_to(REPO_ROOT)} states {authoritative}. That is the "
            "exact inconsistency #974 was filed for: the battery named a failure and "
            "the published page denied it. The battery's table is authoritative — "
            "bring this document to it, and if the count is non-zero also name the "
            "failing test and its expected failure mode here rather than hedging."
        )


def test_every_recorded_row_is_specific_and_still_exists() -> None:
    """A row must name a real test file and a date, or it is a blank waiver."""
    rows = _table_rows()
    if not rows:
        pytest.skip("no accepted failures recorded — nothing to validate")
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) >= 5, (
            f"row is missing columns (want suite | test | failure mode | cause | "
            f"date): {row}"
        )
        suite, test, mode, cause, date = cells[:5]
        assert re.search(r"\d{4}-\d{2}-\d{2}", date), (
            f"row has no ISO date, so nobody can tell how stale it is: {row}"
        )
        assert mode and cause, (
            "a row without a failure mode and a verified cause is a blanket waiver: "
            f"{row}"
        )
        # The suite/test cells should point at something that exists. Accept either
        # cell carrying the path, since the useful split differs per suite.
        candidates = re.findall(r"[\w./-]+\.py", f"{suite} {test}")
        assert candidates, f"row names no test file: {row}"
        for candidate in candidates:
            matches = list(REPO_ROOT.glob(f"**/{Path(candidate).name}"))
            assert matches, (
                f"row names {candidate}, which no longer exists anywhere in the tree "
                f"— delete the row: {row}"
            )
