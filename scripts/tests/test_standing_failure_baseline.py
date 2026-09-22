# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep the documented standing-failure baseline honest, and tied to a real run.

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

The claim is enforced in two halves, because they have different costs.

**The cheap half is here.** It does not run the battery — a
whole-battery-in-a-test would take half an hour, need a correctly installed tree,
and be exactly the kind of gate people learn to ignore. Instead it makes the
declaration internally consistent, falsifiable, and *machine-comparable*:

* the accepted-failure table exists, between markers, so it can be found
  mechanically rather than by prose match;
* the count stated in that section's heading equals the number of rows in it;
* all three documents state the same count, so a real standing failure can no
  longer be recorded in one place and denied in two others;
* every row names a pytest **node id** that exists in the tree, and a date — so a
  row cannot rot into a permanent waiver, and cannot be written as prose that the
  comparison below would silently skip.

**The half that needs real results is in the runner**, because that is the
process that has them: ``scripts/run_all_tests.py`` compares the failures it
observed against this table in both directions and fails the run on either
asymmetry (#1095). The tests at the bottom of this module cover that comparison
and the JUnit parsing it keys on, so the runner's verdict is measured here rather
than asserted in a comment.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BATTERY = REPO_ROOT / ".claude" / "skills" / "full-test-battery.md"
TESTING_DOC = REPO_ROOT / "docs" / "testing.md"
RELEASE_VALIDATION = REPO_ROOT / ".claude" / "skills" / "release-validation.md"

# Every document that states the baseline. `full-test-battery.md` also carries the
# list, so it is authoritative; the other two must state the same number.
DOCS_STATING_THE_COUNT = (BATTERY, TESTING_DOC, RELEASE_VALIDATION)


def _load(name: str, relative: str) -> types.ModuleType:
    """Import a ``scripts/`` module by path, without putting it on sys.path."""
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {relative}"
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a @dataclass in the module resolves its own
    # module out of sys.modules while the class body is being processed, and
    # raises AttributeError if it is not there yet.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load("_standing_failures_under_test", "scripts/standing_failures.py")
runner = _load("_run_all_tests_under_test", "scripts/run_all_tests.py")


def test_stated_count_matches_the_table() -> None:
    """The number in the heading is the number of rows. Nothing else."""
    rows = baseline.table_rows()
    stated = baseline.stated_count()
    assert stated == len(rows), (
        f"{BATTERY.relative_to(REPO_ROOT)} says 'Expected standing failures: "
        f"{stated}' but its table holds {len(rows)} row(s). Whichever you "
        "changed, change the other: a count that disagrees with the list is how a "
        "standing failure gets silently added or silently forgotten.\n"
        + "\n".join(rows)
    )


def test_all_three_documents_state_the_same_count() -> None:
    """One number, three places. A disagreement is the #974 failure mode exactly."""
    authoritative = baseline.stated_count(BATTERY)
    for path in DOCS_STATING_THE_COUNT:
        assert baseline.stated_count(path) == authoritative, (
            f"{path.relative_to(REPO_ROOT)} states "
            f"{baseline.stated_count(path)} standing failure(s) but "
            f"{BATTERY.relative_to(REPO_ROOT)} states {authoritative}. That is the "
            "exact inconsistency #974 was filed for: the battery named a failure and "
            "the published page denied it. The battery's table is authoritative — "
            "bring this document to it, and if the count is non-zero also name the "
            "failing test and its expected failure mode here rather than hedging."
        )


def test_every_recorded_row_is_specific_and_still_exists() -> None:
    """A row must name a real node id and a date, or it is a blank waiver."""
    declared = baseline.declared_failures()
    if not declared:
        pytest.skip("no accepted failures recorded — nothing to validate")
    for row in declared:
        assert re.search(r"\d{4}-\d{2}-\d{2}", row.date), (
            f"row has no ISO date, so nobody can tell how stale it is: {row.row}"
        )
        assert row.mode and row.cause, (
            "a row without a failure mode and a verified cause is a blanket waiver: "
            f"{row.row}"
        )
        assert (REPO_ROOT / row.path).is_file(), (
            f"row names {row.path}, which is not a file in the tree. The node id "
            "must be repo-relative, because that is the form the runner compares "
            f"against: {row.row}"
        )


def test_a_row_written_as_prose_is_rejected() -> None:
    """The comparison keys on node ids, so an unparseable row must not pass.

    Without this, a row reading "the pii-anonymizer report test" would sit in the
    table, satisfy every other check here, and be invisible to the runner — so the
    failure it describes would still count as undeclared and the row would waive
    nothing while looking like it waived something.
    """
    prose = (
        "<!-- STANDING-FAILURES-BEGIN -->\n"
        "| Suite | Test | Expected failure mode | Verified cause | Date |\n"
        "|---|---|---|---|---|\n"
        "| feature api | the report aggregate test | flaky | moto state | 2026-01-01 |\n"
        "<!-- STANDING-FAILURES-END -->\n"
    )
    with pytest.raises(baseline.BaselineError, match="node id"):
        baseline.declared_failures(prose)


def test_the_comparison_reports_both_directions() -> None:
    """Undeclared failure and declared-but-passing are both asymmetries."""
    agree = baseline.compare({"a.py::t"}, {"a.py::t"})
    assert agree.agrees and not agree.unexpected and not agree.resolved

    undeclared = baseline.compare({"a.py::t", "b.py::t"}, {"a.py::t"})
    assert undeclared.unexpected == ("b.py::t",)
    assert not undeclared.resolved
    assert not undeclared.agrees
    assert "b.py::t" in baseline.describe(undeclared)

    passing = baseline.compare(set(), {"a.py::t"})
    assert passing.resolved == ("a.py::t",)
    assert not passing.unexpected
    assert not passing.agrees
    assert "Delete the row" in baseline.describe(passing)


def test_junit_parsing_recovers_a_repo_relative_node_id(tmp_path: Path) -> None:
    """A run's failures must come back as ids comparable with the table's.

    pytest writes each ``file`` attribute relative to the rootdir it computed,
    which for this repo's packages is usually the package directory rather than
    the repo root. An id keyed on the rootdir-relative path would never match a
    repo-relative row, so the resolution is measured here against a file that
    really exists in a package with its own ``pytest.ini``.
    """
    root = "lib/idp_common_pkg/tests"
    target = "lib/idp_common_pkg/tests/unit/test_utils.py"
    assert (REPO_ROOT / target).is_file(), f"fixture path moved: {target}"

    xml = tmp_path / "suite.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="3">'
        '<testcase classname="tests.unit.test_utils" name="test_passes"'
        ' file="tests/unit/test_utils.py" line="1" time="0.0" />'
        '<testcase classname="tests.unit.test_utils" name="test_fails"'
        ' file="tests/unit/test_utils.py" line="2" time="0.0">'
        '<failure message="boom">boom</failure></testcase>'
        '<testcase classname="tests.unit.test_utils.TestThing" name="test_method"'
        ' file="tests/unit/test_utils.py" line="3" time="0.0">'
        '<error message="collection">collection</error></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    found = runner.failing_node_ids(xml, root)
    assert found == [
        f"{target}::test_fails",
        f"{target}::TestThing::test_method",
    ]


def test_an_unreadable_junit_file_is_not_an_absence_of_failures(
    tmp_path: Path,
) -> None:
    """ "No findings" and "could not read the findings" must not look alike."""
    assert runner.failing_node_ids(tmp_path / "missing.xml", "scripts/tests") == []
    truncated = tmp_path / "truncated.xml"
    truncated.write_text("<testsuites><testsuite", encoding="utf-8")
    assert runner.failing_node_ids(truncated, "scripts/tests") == []


def _fake_run(returncode: int):
    def run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode)

    return run


@pytest.mark.parametrize(
    ("observed", "declared", "expected_exit"),
    [
        # A failure nobody declared: red, as it has always been.
        (["x.py::test_a"], [], 1),
        # Exactly the declared set failed: the baseline is doing its job, green.
        (["x.py::test_a"], ["x.py::test_a"], 0),
        # A declared row over a test that now passes: red, delist it. This is the
        # direction that did not exist before #1095 — the table could keep a row
        # for a fixed test indefinitely and nothing would say so.
        ([], ["x.py::test_a"], 1),
    ],
)
def test_the_runner_decides_the_verdict_from_what_it_observed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    observed: list[str],
    declared: list[str],
    expected_exit: int,
) -> None:
    """The gate's exit code follows the comparison, not the per-root exit codes."""
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", _fake_run(1 if observed else 0))
    monkeypatch.setattr(runner, "failing_node_ids", lambda _xml, _root: list(observed))
    monkeypatch.setattr(
        runner,
        "declared_failures",
        lambda: [
            baseline.DeclaredFailure(
                node_id=node,
                suite="s",
                mode="m",
                cause="c",
                date="2026-01-01",
                row="|s|" + node + "|m|c|2026-01-01|",
            )
            for node in declared
        ],
    )

    assert runner.run_gate(["scripts/tests"], integration=False) == expected_exit


def test_a_root_that_fails_with_no_named_test_cannot_be_declared_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A collection error or crash names no node id, so no row can cover it.

    Keying the baseline on node ids means a root that dies before collecting
    anything produces an empty observed set. Treating that as "no failures" would
    turn the most serious kind of red — the suite did not run — into a pass.
    """
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", _fake_run(2))
    monkeypatch.setattr(runner, "failing_node_ids", lambda _xml, _root: [])
    monkeypatch.setattr(runner, "declared_failures", lambda: [])

    assert runner.run_gate(["scripts/tests"], integration=False) == 1
