# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for `make coverage`'s table.

This module shipped with **zero** tests, in a change whose whole subject was that
untested code looks fine until someone reads it. It sits in the gate layer, so a wrong
number here is worse than a wrong number in ordinary code: it is the thing people would
consult to decide where the next test goes, and a silently mis-sorted or mis-attributed
row sends that decision the wrong way.

Two properties carry the module's value and both are asserted directly:

**The sort order is the whole point.** The table exists because `pytest --cov`'s own
report is alphabetical, which answers "what is this file's coverage" and not "where
should the next test go". Sorting by uncovered statements descending is what makes a
2,320-statement module at 80% outrank a 195-statement one at 23%. A test that only
checked the rows were present would pass with the sort removed.

**The delta against the baseline must distinguish three states** — unchanged, moved, and
absent-from-the-baseline — because a file reported as `=` when it has actually regressed
is precisely the failure the ratchet beside it exists to catch.

**Statement counts come from the report's own `line` elements**, not from its line-rate,
which would be circular and would round. Showing that needs a report whose two fields
*disagree*: against a self-consistent one, a rate written at full precision recovers the
count exactly, both implementations return the same numbers, and the assertion passes with
the property removed.

Each of those three properties was checked by mutation -- the implementation was broken
one line at a time and this suite had to go red. Three of the first five mutations
survived, each because the test data was accidentally degenerate rather than because the
assertion was wrong: three filenames that happened to be in the correct alphabetical
order, a `<source>` equal to the tree root so a mis-resolved root fell through to the
identical string, and the self-consistent report above. Data that cannot distinguish the
two implementations is the failure mode of a test that reads perfectly well.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "coverage_table", REPO_ROOT / "scripts" / "coverage_table.py"
)
assert _spec and _spec.loader
table = importlib.util.module_from_spec(_spec)
sys.modules["coverage_table"] = table
_spec.loader.exec_module(table)

#: `idp_common`'s tree root -- what its report paths are made relative to. Derived from
#: the same registry the module reads, so a tree that moves moves here too, and a test
#: that hardcoded the path would keep passing against a stale location.
IDP_COMMON = table.TREES_BY_NAME["idp_common"]
PKG_ROOT = table.tree_root(IDP_COMMON)

#: The ratchet module itself. `coverage_table` imports from it, so loading the table has
#: already put it in `sys.modules`; fetching it here rather than re-importing keeps the
#: two scripts' idea of where a report lives comparable to the same single object.
debt = sys.modules["check_coverage_debt"]


def _report(
    tmp_path: Path, files: dict[str, tuple[int, int]], *, total: float = 80.0
) -> Path:
    """A Cobertura report with explicit per-file (statements, uncovered) counts.

    `hits="0"` marks an uncovered line, which is how the module counts rather than
    deriving from the rate.
    """
    source = PKG_ROOT / "idp_common"
    classes = []
    for name, (stmts, missed) in files.items():
        lines = "".join(
            f'<line number="{i + 1}" hits="{0 if i < missed else 1}"/>'
            for i in range(stmts)
        )
        rate = (stmts - missed) / stmts if stmts else 0
        classes.append(
            f'<class filename="{name}" line-rate="{rate}"><lines>{lines}</lines></class>'
        )
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<?xml version="1.0" ?>'
        f'<coverage line-rate="{total / 100}">'
        f"<sources><source>{source}</source></sources>"
        f"<packages><package><classes>{''.join(classes)}</classes></package></packages>"
        f"</coverage>\n",
        encoding="utf-8",
    )
    return path


def _run(
    monkeypatch, capsys, report: Path, *args: str, baseline: dict | None = None
) -> str:
    if baseline is not None:
        path = report.parent / "coverage_debt.json"
        path.write_text(json.dumps(baseline))
        monkeypatch.setattr(table, "BASELINE", path)
    monkeypatch.setattr(
        sys, "argv", ["coverage_table.py", "--report", str(report), *args]
    )
    assert table.main() == 0
    return capsys.readouterr().out


@pytest.mark.unit
class TestStatementCounts:
    def test_counts_come_from_the_line_elements_not_the_rate(self, tmp_path):
        """Deriving the count from the rate would be circular, and would round.

        Asserting that against a *consistent* report proves nothing: a rate written at
        full precision recovers the count exactly, so both implementations agree and the
        test passes either way (it did). This report is deliberately **inconsistent** --
        900 of 1,000 lines carry `hits="0"` while the `line-rate` attribute claims 90%
        covered -- so the two implementations must disagree, and only one of them can
        return 900.
        """
        path = tmp_path / "inconsistent.xml"
        lines = "".join(
            f'<line number="{i + 1}" hits="{0 if i < 900 else 1}"/>'
            for i in range(1000)
        )
        path.write_text(
            f'<?xml version="1.0" ?><coverage line-rate="0.1">'
            f"<sources><source>{PKG_ROOT / 'idp_common'}</source></sources>"
            f'<packages><package><classes><class filename="ocr/service.py" '
            f'line-rate="0.9"><lines>{lines}</lines></class>'
            f"</classes></package></packages></coverage>\n",
            encoding="utf-8",
        )
        assert table._statement_counts(path, PKG_ROOT) == {
            "idp_common/ocr/service.py": (1000, 900)
        }, "the uncovered count must come from the hits, not from the line-rate"

    def test_a_fully_covered_file_reports_zero_uncovered(self, tmp_path):
        report = _report(tmp_path, {"a.py": (5, 0)})
        assert table._statement_counts(report, PKG_ROOT)["idp_common/a.py"] == (5, 0)


@pytest.mark.unit
class TestSortOrder:
    def test_rows_are_ordered_by_UNCOVERED_statements_descending(
        self, tmp_path, monkeypatch, capsys
    ):
        """The module's reason to exist. A big module at 80% outranks a small one at 23%.

        Removing the sort leaves every row present, so a presence-only assertion would
        pass; the order is what makes the table actionable.
        """
        out = _run(
            monkeypatch,
            capsys,
            _report(
                tmp_path,
                {
                    # Names chosen so alphabetical order is the REVERSE of the
                    # order this table must produce. With the same names in
                    # coincidentally-correct alphabetical order, deleting the sort
                    # leaves the assertion passing -- which it did, until this data
                    # was fixed.
                    "a_tiny.py": (10, 1),  # 90%, 1 uncovered  -> last
                    "m_small_but_awful.py": (100, 77),  # 23%, 77 uncovered -> middle
                    "z_big_and_decent.py": (2320, 456),  # 80%, 456 uncovered -> first
                },
            ),
            "--top",
            "0",
        )
        rows = [
            ln.split()[0] for ln in out.splitlines() if ln.startswith("idp_common/")
        ]
        assert rows == [
            "idp_common/z_big_and_decent.py",
            "idp_common/m_small_but_awful.py",
            "idp_common/a_tiny.py",
        ], out

    def test_top_limits_the_rows_and_says_how_many_are_hidden(
        self, tmp_path, monkeypatch, capsys
    ):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {f"m{i}.py": (100, 100 - i) for i in range(5)}),
            "--top",
            "2",
        )
        assert len([ln for ln in out.splitlines() if ln.startswith("idp_common/")]) == 2
        assert "3 more file(s) not shown" in out

    def test_top_zero_shows_everything_and_hides_the_footer(
        self, tmp_path, monkeypatch, capsys
    ):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {f"m{i}.py": (100, 100 - i) for i in range(5)}),
            "--top",
            "0",
        )
        assert len([ln for ln in out.splitlines() if ln.startswith("idp_common/")]) == 5
        assert "more file(s) not shown" not in out

    def test_min_statements_hides_the_small_files_that_dominate_at_zero_percent(
        self, tmp_path, monkeypatch, capsys
    ):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"big.py": (500, 100), "stub.py": (3, 3)}),
            "--top",
            "0",
            "--min-statements",
            "10",
        )
        assert "idp_common/big.py" in out
        assert "idp_common/stub.py" not in out


@pytest.mark.unit
class TestBaselineDelta:
    """Three states that must not be conflated: unchanged, moved, unrecorded."""

    def _cell(self, out: str, filename: str) -> str:
        row = next(ln for ln in out.splitlines() if filename in ln)
        return row.split()[-1]

    def test_an_unchanged_file_shows_equals(self, tmp_path, monkeypatch, capsys):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"a.py": (100, 20)}),
            "--top",
            "0",
            baseline={
                "trees": {
                    "idp_common": {"total": 80.0, "files": {"idp_common/a.py": 80.0}}
                }
            },
        )
        assert self._cell(out, "idp_common/a.py") == "="

    def test_a_regression_shows_a_signed_delta_not_equals(
        self, tmp_path, monkeypatch, capsys
    ):
        """A regressed file reported as `=` is exactly what the ratchet beside this
        table exists to catch, so the table must not hide it."""
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"a.py": (100, 40)}),
            "--top",
            "0",
            baseline={
                "trees": {
                    "idp_common": {"total": 80.0, "files": {"idp_common/a.py": 80.0}}
                }
            },
        )
        assert self._cell(out, "idp_common/a.py") == "-20.00"

    def test_an_improvement_shows_a_positive_delta(self, tmp_path, monkeypatch, capsys):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"a.py": (100, 10)}),
            "--top",
            "0",
            baseline={
                "trees": {
                    "idp_common": {"total": 80.0, "files": {"idp_common/a.py": 80.0}}
                }
            },
        )
        assert self._cell(out, "idp_common/a.py") == "+10.00"

    def test_a_file_absent_from_the_baseline_is_marked_new(
        self, tmp_path, monkeypatch, capsys
    ):
        """Distinct from `=`. An unrecorded file is unratcheted, which is a different
        thing from one that has not moved."""
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"fresh.py": (50, 25)}),
            "--top",
            "0",
            baseline={"trees": {"idp_common": {"total": 80.0, "files": {}}}},
        )
        assert self._cell(out, "idp_common/fresh.py") == "new"


@pytest.mark.unit
class TestHeaderAndTotals:
    def test_the_totals_row_sums_the_rows(self, tmp_path, monkeypatch, capsys):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"a.py": (100, 30), "b.py": (200, 70)}, total=66.67),
            "--top",
            "0",
        )
        total_row = next(ln for ln in out.splitlines() if ln.startswith("TOTAL"))
        assert "300" in total_row and "100" in total_row

    def test_the_overall_figure_comes_from_the_report_not_the_rows(
        self, tmp_path, monkeypatch, capsys
    ):
        # The report's own line-rate is authoritative; recomputing it from the shown rows
        # would be wrong whenever --top or --min-statements filtered any of them out.
        out = _run(
            monkeypatch, capsys, _report(tmp_path, {"a.py": (10, 5)}, total=77.86)
        )
        assert "77.86% overall" in out

    def test_the_baseline_line_names_the_gate_rather_than_implying_this_is_one(
        self, tmp_path, monkeypatch, capsys
    ):
        out = _run(
            monkeypatch,
            capsys,
            _report(tmp_path, {"a.py": (10, 2)}),
            baseline={
                "trees": {
                    "idp_common": {"total": 80.0, "files": {"idp_common/a.py": 80.0}}
                }
            },
        )
        assert "check-coverage-debt" in out, (
            "the table must point at the gate; it is a report and asserts nothing itself"
        )


@pytest.mark.unit
def test_a_missing_report_is_an_error_here_unlike_in_the_ratchet(
    tmp_path, monkeypatch, capsys
):
    """Opposite choice from `check_coverage_debt.py`, deliberately.

    The ratchet runs in sequences that may not have built a report, so absence is not a
    failure there. This is a command someone typed to see a table: producing nothing and
    exiting 0 would look like an empty repository rather than a missing measurement.
    """
    monkeypatch.setattr(
        sys, "argv", ["coverage_table.py", "--report", str(tmp_path / "nope.xml")]
    )
    assert table.main() == 1
    assert "No coverage report" in capsys.readouterr().err


@pytest.mark.unit
class TestTheTreeRegistryIsTheOnlyListOfTrees:
    """The table reads `check_coverage_debt.TREES`; it must not keep a second list.

    This is not hypothetical. The table was written when there was one measured tree and
    it imported a module-level `DEFAULT_REPORT` constant naming that tree's report. When
    the ratchet was generalised to nine trees the constant went away, and `make coverage`
    and `make coverage-table` both died on an `ImportError` -- in an open pull request,
    with every other check green, because nothing in the suite ever imported this module.
    """

    def test_an_unregistered_tree_name_is_rejected(self, monkeypatch):
        """`--tree` takes its choices from the registry, so a typo fails loudly."""
        monkeypatch.setattr(
            sys, "argv", ["coverage_table.py", "--tree", "no_such_tree"]
        )
        with pytest.raises(SystemExit) as excinfo:
            table.main()
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("name", [t.name for t in table.TREES])
    def test_every_registered_tree_can_be_tabulated(
        self, name, tmp_path, monkeypatch, capsys
    ):
        """Each tree, not a representative one.

        A `--tree` whose report root is resolved wrongly produces a table of absolute
        paths or of empty strings rather than an error, so checking one tree and
        inferring the rest is how a per-tree mistake survives.
        """
        tree = table.TREES_BY_NAME[name]
        # A `<source>` one level BELOW the tree root, which is the shape a real
        # `--cov=<package>` run produces. That nesting is what makes this test decisive:
        # with the source equal to the root, a wrong base falls through to the raw
        # `filename` -- the same string a correct base produces -- so the assertion holds
        # either way and the mutation survives (it did).
        source = table.tree_root(tree) / "nested_pkg"
        report = tmp_path / "coverage.xml"
        report.write_text(
            f'<?xml version="1.0" ?><coverage line-rate="0.5">'
            f"<sources><source>{source}</source></sources>"
            f'<packages><package><classes><class filename="mod.py" '
            f'line-rate="0.5"><lines><line number="1" hits="1"/>'
            f'<line number="2" hits="0"/></lines></class>'
            f"</classes></package></packages></coverage>\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "coverage_table.py",
                "--tree",
                name,
                "--report",
                str(report),
                "--top",
                "0",
            ],
        )
        assert table.main() == 0
        out = capsys.readouterr().out
        assert f"{name} test coverage" in out
        # Tree-root-relative, so the row names the file the way the baseline does. An
        # absolute path, or a bare "mod.py", means this tree's root was resolved wrongly.
        assert "nested_pkg/mod.py" in out, out
        assert str(table.tree_root(tree)) not in out, out

    def test_the_default_report_is_the_path_this_trees_run_writes(self, tmp_path):
        """No `--report` must resolve to the file `scripts/coverage_all.py` produces.

        The two scripts agree only because both ask the registry. A default spelled out
        here would point at a path nothing writes, and the symptom would be a "no
        coverage report" message that looks like the tests simply had not been run.
        """
        for tree in table.TREES:
            expected = (
                table.tree_root(tree) / "test-reports" / f"coverage-{tree.name}.xml"
            )
            assert debt.report_path(tree) == expected
