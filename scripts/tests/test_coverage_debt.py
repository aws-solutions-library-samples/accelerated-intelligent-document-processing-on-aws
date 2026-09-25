# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the coverage ratchet.

The ratchet spans nine trees and has two halves — an aggregate floor in `lib/idp_common_pkg/Makefile` and a
per-file baseline checked by `scripts/check_coverage_debt.py` — and both are asserted
here, because the aggregate is the half people remember and the per-file half is the one
that catches the regression the aggregate hides.

**The first version of the checker silently recorded nothing**, and that is why several
of these tests exist in the shape they do. Cobertura's `filename` attribute is relative
to the report's own `<source>` root, so for `--cov=idp_common` it reads `ocr/service.py`
while the baseline and `git ls-files` both speak `idp_common/ocr/service.py`. Comparing
the two forms matched zero files; the baseline recorded zero entries; and the
universe-closure check passed *because* its `path in measured` test was false for
everything. The only symptom was a cheerful "recorded 0 file(s)". So:

* :func:`test_the_recorded_baseline_is_not_empty` is a non-vacuity guard on the real
  baseline file, not on a fixture.
* :func:`test_report_filenames_are_resolved_against_their_source_root` pins the
  normalisation directly, with a report in the shape coverage.py actually emits.

**A gate that measured nothing must not report success**, which this one did: with no
report it printed a ✅ and exited 0 — measured on the commit before the change, on a tree
with no report, as `✅ coverage ratchet: 0 file(s) across 0 tree(s) at or above their
recorded coverage`, exit 0. In both CI configurations it runs as a separate step from the
run that writes its input, so that line was one reordering away from being the whole
coverage story of a green pipeline. The sharper half of the same property is a report from
a run that did **not** finish: an xdist collection mismatch still writes a well-formed
`coverage.xml`, the ratchet named large falls that had not happened, and the remedy it
printed (`--write`) would have recorded them permanently. Issue #1190. So:

* :class:`TestNothingMeasured` pins the exit code and the absence of a ✅ for every way
  the gate can end up with nothing to compare, including `--require-tree`.
* :class:`TestRunTrust` pins the trust decision against a **real** errored run: one test
  reproduces an xdist collection mismatch and drives the gate over what it leaves behind,
  and a second reads the record captured from such a run, so the parsing is pinned even
  where xdist is unavailable.
* :class:`TestRunRecordWiring` derives the `--junitxml` argument both producers must pass
  from :func:`check_coverage_debt.run_record_path`, because dropping it from either would
  otherwise degrade the trust check silently rather than loudly.

Every behavioural test drives the real functions against a synthetic report written to
`tmp_path`, rather than asserting how the code is written.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "check_coverage_debt", REPO_ROOT / "scripts" / "check_coverage_debt.py"
)
assert _spec and _spec.loader
ccd = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage_debt"] = ccd
_spec.loader.exec_module(ccd)


def _report(
    tmp_path: Path, rates: dict[str, float], *, source: Path, total: float = 80.0
) -> Path:
    """A Cobertura report shaped the way coverage.py emits one.

    `filename` attributes are `<source>`-relative and carry no package prefix, which is
    the detail the checker has to normalise. Two separate bugs have been caused by
    getting that wrong -- once by comparing the two forms directly, and once by
    shadowing the tree-root parameter with the `<source>` loop variable, which silently
    stripped the prefix again. Both produced "recorded 0 file(s)" and a cheerful tick.
    """
    classes = "\n".join(
        f'<class filename="{name}" line-rate="{rate / 100}"/>'
        for name, rate in rates.items()
    )
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<?xml version="1.0" ?>'
        f'<coverage line-rate="{total / 100}">'
        f"<sources><source>{source}</source></sources>"
        f"<packages><package><classes>{classes}</classes></package></packages>"
        f"</coverage>\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.unit
class TestReportParsing:
    def test_keys_are_made_relative_to_the_TREE_ROOT_not_the_source_dir(self, tmp_path):
        """The bug that produced "recorded 0 file(s)" twice, by two different routes.

        `<source>` is the package directory; the baseline and `git ls-files` both speak
        tree-root-relative paths. The second time, the tree-root parameter was shadowed
        by the `for source in roots` loop variable, so `relative_to` used the source dir
        and every key lost its `idp_common/` prefix -- 227 measured, 227 tracked, 0
        overlap.
        """
        tree = ccd.TREES_BY_NAME["idp_common"]
        report = _report(
            tmp_path,
            {"ocr/service.py": 99.0},
            source=ccd.tree_root(tree) / "idp_common",
        )
        assert ccd.read_report(report, ccd.tree_root(tree)) == {
            "idp_common/ocr/service.py": 99.0
        }

    def test_the_overall_rate_is_read_from_the_root_element(self, tmp_path):
        report = _report(tmp_path, {}, source=REPO_ROOT, total=77.86)
        assert ccd.report_total(report) == 77.86


@pytest.mark.unit
class TestRegressionDetection:
    """`check_tree` against a synthetic report, per tree."""

    TREE = "idp_common"

    def _check(self, tmp_path, recorded, measured, monkeypatch):
        tree = ccd.TREES_BY_NAME[self.TREE]
        report = _report(tmp_path, measured, source=ccd.tree_root(tree) / "idp_common")
        monkeypatch.setattr(ccd, "_resolve_report", lambda t: report)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: list(recorded))
        return ccd.check_tree(tree, recorded)[0]

    def test_an_unchanged_file_passes(self, tmp_path, monkeypatch):
        assert (
            self._check(
                tmp_path,
                {"idp_common/ocr/service.py": 99.0},
                {"ocr/service.py": 99.0},
                monkeypatch,
            )
            == []
        )

    def test_a_drop_beyond_the_tolerance_fails_and_names_both_numbers(
        self, tmp_path, monkeypatch
    ):
        problems = self._check(
            tmp_path,
            {"idp_common/ocr/service.py": 99.0},
            {"ocr/service.py": 60.0},
            monkeypatch,
        )
        assert problems and "99.00" in problems[0] and "60.00" in problems[0]
        assert "[idp_common]" in problems[0], (
            "the tree is not named, so the file is ambiguous"
        )

    def test_a_rise_does_not_fail(self, tmp_path, monkeypatch):
        """A ratchet demanding a re-record on every improvement gets re-recorded
        reflexively, and a file whose coverage genuinely fell goes with it."""
        assert (
            self._check(
                tmp_path,
                {"idp_common/ocr/service.py": 60.0},
                {"ocr/service.py": 99.0},
                monkeypatch,
            )
            == []
        )

    @pytest.mark.parametrize("delta", [0.5, 1.0])
    def test_a_drop_within_the_tolerance_is_allowed(self, tmp_path, monkeypatch, delta):
        assert (
            self._check(
                tmp_path,
                {"idp_common/ocr/service.py": 90.0},
                {"ocr/service.py": 90.0 - delta},
                monkeypatch,
            )
            == []
        )

    def test_a_drop_just_past_the_tolerance_fails(self, tmp_path, monkeypatch):
        assert self._check(
            tmp_path,
            {"idp_common/ocr/service.py": 90.0},
            {"ocr/service.py": 88.5},
            monkeypatch,
        )

    def test_a_recorded_file_missing_from_the_report_fails(self, tmp_path, monkeypatch):
        problems = self._check(
            tmp_path,
            {"idp_common/gone.py": 50.0},
            {"ocr/service.py": 99.0},
            monkeypatch,
        )
        assert problems and "gone.py" in problems[0]


@pytest.mark.unit
class TestUniverseClosure:
    def test_a_measured_file_absent_from_the_baseline_fails(
        self, tmp_path, monkeypatch
    ):
        tree = ccd.TREES_BY_NAME["idp_common"]
        report = _report(
            tmp_path, {"brand_new.py": 10.0}, source=ccd.tree_root(tree) / "idp_common"
        )
        monkeypatch.setattr(ccd, "_resolve_report", lambda t: report)
        monkeypatch.setattr(
            ccd, "tracked_source_files", lambda t: ["idp_common/brand_new.py"]
        )
        problems, _ = ccd.check_tree(tree, {})
        assert problems and "brand_new.py" in problems[0]

    def test_every_not_measured_entry_carries_a_reason(self):
        for path, reason in ccd.NOT_MEASURED.items():
            assert reason.strip(), f"NOT_MEASURED[{path}] has no reason"


@pytest.mark.unit
class TestTreeRegistry:
    def test_the_registry_is_not_empty(self):
        """Non-vacuity on the registry itself.

        Every other test in this class iterates TREES, so emptying it makes all of them
        pass — measured: replacing the registry with `()` left 28/28 green. A derived
        gate whose universe can be silently emptied is the failure this suite exists to
        prevent, one level up from the files it checks.
        """
        assert len(ccd.TREES) >= 9, (
            f"only {len(ccd.TREES)} tree(s) registered; nine were measured when this "
            f"gate was written, so a smaller number means trees were dropped rather "
            f"than that the repository shrank"
        )

    @pytest.mark.parametrize("name", ["idp_common", "scripts", "idp_sdk", "idp_cli"])
    def test_the_trees_that_matter_are_registered_by_name(self, name):
        """Named individually, so dropping any one of them fails.

        A count alone can be satisfied by swapping a large tree for a small one.
        `scripts` is here because it is 33,000 statements that nobody had measured, and
        `idp_sdk`/`idp_cli` because they are the two whose recorded figures went stale for
        needs to hold.
        """
        assert name in ccd.TREES_BY_NAME

    def test_every_tree_directory_exists(self):
        for tree in ccd.TREES:
            assert (REPO_ROOT / tree.cwd).is_dir(), (
                f"{tree.name}: {tree.cwd} is missing"
            )

    def test_tree_names_are_unique(self):
        names = [t.name for t in ccd.TREES]
        assert len(names) == len(set(names))

    def test_every_tree_discovers_source_files(self):
        """A tree whose source discovery finds nothing ratchets nothing.

        This is the assertion that would have caught the prefix bug at registry level
        rather than after a --write printed nine zeros.
        """
        for tree in ccd.TREES:
            assert ccd.tracked_source_files(tree), (
                f"{tree.name}: tracked_source_files() found no source under "
                f"{tree.cwd}/{tree.cov}; its --cov target is probably wrong"
            )


@pytest.mark.unit
class TestTheRealBaseline:
    """Assertions about the committed baseline, not a fixture."""

    def _baseline(self):
        return json.loads((REPO_ROOT / "scripts" / "coverage_debt.json").read_text())

    def test_every_tree_is_recorded(self):
        trees = self._baseline().get("trees", {})
        missing = [t.name for t in ccd.TREES if t.name not in trees]
        assert not missing, f"these trees have no recorded baseline: {missing}"

    def test_no_tree_recorded_zero_files(self):
        """The non-vacuity ratchet, and it has earned its keep twice.

        Both path-matching bugs presented as a tree recording **zero** files while
        reporting success. A fixture-only suite cannot see that, because each fixture
        supplies its own entries.
        """
        empty = [
            name
            for name, data in self._baseline().get("trees", {}).items()
            if not data.get("files")
        ]
        assert not empty, (
            f"these trees recorded zero files, which means their paths did not match "
            f"rather than that they have no source: {empty}"
        )

    def test_the_recorded_totals_are_plausible(self):
        for name, data in self._baseline().get("trees", {}).items():
            assert 0 < data.get("total", 0) <= 100, f"{name}: total {data.get('total')}"

    def test_every_recorded_path_is_still_tracked(self):
        for tree in ccd.TREES:
            recorded = set(
                self._baseline().get("trees", {}).get(tree.name, {}).get("files", {})
            )
            missing = sorted(recorded - set(ccd.tracked_source_files(tree)))
            assert not missing, (
                f"{tree.name}: baseline records {len(missing)} path(s) git no longer "
                f"tracks: {missing[:4]}"
            )


@pytest.mark.unit
class TestWiring:
    """A gate nobody invokes is not a gate."""

    def _makefile(self):
        return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "target",
        ["check-coverage-debt", "coverage", "coverage-all", "coverage-summary"],
    )
    def test_the_target_exists(self, target):
        assert f"\n{target}:" in self._makefile()

    @pytest.mark.parametrize("target", ["lint", "fastlint"])
    def test_the_lint_sets_do_NOT_run_the_ratchet(self, target):
        """Deliberate. It reads reports the lint targets never build, so wired there it
        would find nothing, exit 0, and pass vacuously -- worse than absent, because it
        reads as protection."""
        line = next(
            ln for ln in self._makefile().splitlines() if ln.startswith(f"{target}: ")
        )
        assert "check-coverage-debt" not in line

    @pytest.mark.parametrize(
        "config", [".gitlab-ci.yml", ".github/workflows/developer-tests.yml"]
    )
    def test_each_ci_runs_it_after_the_test_step(self, config):
        """Order is the whole point: before the tests there is no report to read."""
        path = REPO_ROOT / config
        if not path.is_file():
            pytest.skip(f"{config} is not present")
        text = path.read_text(encoding="utf-8")
        assert "make check-coverage-debt" in text
        assert text.index("make check-coverage-debt") > text.index(
            "make test-cicd -C lib/idp_common_pkg"
        )

    def test_the_aggregate_floor_is_set_and_overridable(self):
        makefile = (REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile").read_text()
        assert "COV_FLOOR ?= --cov-fail-under=" in makefile
        assert "$(COV_FLOOR)" in makefile

    def test_the_floor_is_not_above_idp_commons_recorded_total(self):
        """A floor above the measured figure red-lines an unmodified tree."""
        makefile = (REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile").read_text()
        floor = float(makefile.split("COV_FLOOR ?= --cov-fail-under=")[1].split()[0])
        recorded = json.loads(
            (REPO_ROOT / "scripts" / "coverage_debt.json").read_text()
        )["trees"]["idp_common"]["total"]
        assert floor <= recorded, f"floor {floor} exceeds recorded {recorded:.2f}"


def _fake_tree(tmp_path: Path, name: str = "faketree"):
    """A tree rooted in `tmp_path`, so `--write` cannot touch the real baseline.

    Registered by monkeypatching TREES, not by editing the real registry: these tests
    exercise the orchestration, and doing that against the nine real trees would make
    them depend on whichever reports happen to be lying around from the last run.
    """
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    return ccd.Tree(name, str(root), "pkg")


def _install(monkeypatch, tmp_path, trees, baseline: dict | None = None) -> Path:
    """Point the module at a throwaway registry and baseline file."""
    monkeypatch.setattr(ccd, "TREES", tuple(trees))
    monkeypatch.setattr(ccd, "TREES_BY_NAME", {t.name: t for t in trees})
    monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
    path = tmp_path / "coverage_debt.json"
    if baseline is not None:
        path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(ccd, "BASELINE", path)
    return path


def _write_run_record(
    report: Path, *, errors: int = 0, failures: int = 0, tests: int = 7
) -> Path:
    """The JUnit XML a pytest run writes beside its coverage report.

    Every producer in the tree writes one, so a fixture that omits it is not a report
    shaped like a real one. Written with the same `<testsuite>` attributes pytest emits,
    which :data:`ERRORED_RUN_RECORD` — captured from a real errored run — is the check on.
    """
    record = ccd.run_record_path(report)
    record.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" skipped="0" '
        f'tests="{tests}" time="1.5"/></testsuites>\n',
        encoding="utf-8",
    )
    return record


def _write_tree_report(
    tree, rates: dict[str, float], total: float = 80.0, *, run_record: bool = True
) -> Path:
    report = ccd.report_path(tree)
    report.parent.mkdir(parents=True, exist_ok=True)
    classes = "\n".join(
        f'<class filename="{n}" line-rate="{r / 100}"/>' for n, r in rates.items()
    )
    report.write_text(
        f'<?xml version="1.0" ?><coverage line-rate="{total / 100}">'
        f"<sources><source>{ccd.tree_root(tree) / tree.cov}</source></sources>"
        f"<packages><package><classes>{classes}</classes></package></packages>"
        f"</coverage>\n",
        encoding="utf-8",
    )
    if run_record:
        _write_run_record(report)
    return report


@pytest.mark.unit
class TestCheckOrchestration:
    """`check()` across trees. Every tree is checked, and an unchecked one is named.

    The per-tree comparison is covered above; what is asserted here is the part that
    decides the exit code and what the operator is told. A gate that passes while
    measuring nothing is the specific failure this whole ratchet exists to prevent, so
    "a tree with no report" must not read as "a tree that is fine".
    """

    def test_a_tree_with_no_report_is_named_as_unchecked_beside_one_that_was_measured(
        self, monkeypatch, tmp_path
    ):
        """An unmeasured tree is named, and does not make the measured one's pass wider.

        The one-tree version of this used to assert ``(0, [], [name])`` — a tree with no
        report, no problems, exit 0 — which is the vacuous pass #1190 is about. Nothing
        was measured there, so that case now refuses; see
        :class:`TestNothingMeasured`. What stays true, and is asserted here, is that a
        report missing for *one* tree is reported rather than silently folded into the
        verdict for the others.
        """
        measured, absent = (
            _fake_tree(tmp_path, "measured"),
            _fake_tree(tmp_path, "gone"),
        )
        _install(
            monkeypatch,
            tmp_path,
            [measured, absent],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        code, problems, unchecked, checked = ccd.check()
        assert (code, problems) == (0, [])
        assert [(u.tree, bool(u.reason)) for u in unchecked] == [("gone", True)], (
            "an unmeasured tree must be reported as unchecked, with a reason; returning "
            "it as a pass is how a gate reads green while checking nothing"
        )

    def test_a_report_with_no_recorded_baseline_is_a_problem_not_a_pass(
        self, monkeypatch, tmp_path
    ):
        """A measured-but-unrecorded tree is unratcheted, which is the state the ratchet
        exists to rule out -- so it fails rather than being accepted silently."""
        tree = _fake_tree(tmp_path)
        _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        code, problems, unchecked, checked = ccd.check()
        assert code == 1 and unchecked == [] and checked == [tree.name]
        assert any("no recorded baseline" in p for p in problems), problems

    def test_problems_from_several_trees_are_all_reported_and_each_is_labelled(
        self, monkeypatch, tmp_path
    ):
        """Not just the first. A loop that returned early would hide the second tree's
        regression behind the first one's, and the label is what tells them apart."""
        a, b = _fake_tree(tmp_path, "alpha"), _fake_tree(tmp_path, "beta")
        _install(
            monkeypatch,
            tmp_path,
            [a, b],
            {
                "trees": {
                    "alpha": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                    "beta": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                }
            },
        )
        _write_tree_report(a, {"mod.py": 50.0})
        _write_tree_report(b, {"mod.py": 40.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        code, problems, _, _ = ccd.check()
        assert code == 1
        assert any(p.startswith("[alpha]") for p in problems), problems
        assert any(p.startswith("[beta]") for p in problems), problems

    def test_one_healthy_tree_does_not_mask_another_trees_regression(
        self, monkeypatch, tmp_path
    ):
        a, b = _fake_tree(tmp_path, "good"), _fake_tree(tmp_path, "bad")
        _install(
            monkeypatch,
            tmp_path,
            [a, b],
            {
                "trees": {
                    "good": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                    "bad": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                }
            },
        )
        _write_tree_report(a, {"mod.py": 95.0})
        _write_tree_report(b, {"mod.py": 20.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        code, problems, _, _ = ccd.check()
        assert code == 1
        assert all("[good]" not in p for p in problems), problems


@pytest.mark.unit
class TestWriteBaseline:
    """`--write`. The claim worth testing is what it does to trees it did NOT measure."""

    def test_a_tree_with_no_report_keeps_its_recorded_baseline(
        self, monkeypatch, tmp_path
    ):
        """The load-bearing property of `--write`.

        `make coverage-all --only one_tree` followed by `--write` must not erase the
        other eight trees' baselines. If it did, the routine act of re-recording one
        tree would silently un-ratchet everything else, and the next run would report
        no problems because there would be nothing left to compare against.
        """
        measured, untouched = (
            _fake_tree(tmp_path, "measured"),
            _fake_tree(tmp_path, "untouched"),
        )
        path = _install(
            monkeypatch,
            tmp_path,
            [measured, untouched],
            {
                "trees": {
                    "untouched": {"total": 77.0, "files": {"pkg/old.py": 77.0}},
                }
            },
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        ccd.write_baseline()
        written = json.loads(path.read_text())["trees"]
        assert written["untouched"] == {"total": 77.0, "files": {"pkg/old.py": 77.0}}, (
            "a partial run must not discard another tree's baseline"
        )
        assert written["measured"]["files"] == {"pkg/mod.py": 90.0}

    def test_an_untracked_file_in_the_report_is_not_recorded(
        self, monkeypatch, tmp_path
    ):
        """Build output and stray files appear in a report but are not source.

        Recording them would pin coverage for paths git does not track, and the
        staleness check would then fail the moment they were cleaned up.
        """
        tree = _fake_tree(tmp_path)
        path = _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0, "generated.py": 12.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        ccd.write_baseline()
        assert set(json.loads(path.read_text())["trees"][tree.name]["files"]) == {
            "pkg/mod.py"
        }

    def test_the_written_file_records_the_tolerance_and_how_to_regenerate_it(
        self, monkeypatch, tmp_path
    ):
        """It is generated, so it has to say so in itself.

        A hand-edited baseline is indistinguishable from a recorded one once committed,
        and the tolerance is the number a reader needs to interpret any entry.
        """
        tree = _fake_tree(tmp_path)
        path = _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        ccd.write_baseline()
        data = json.loads(path.read_text())
        assert data["tolerancePct"] == ccd.TOLERANCE_PCT
        assert "do not hand-edit" in " ".join(data["$comment"]).lower()
        assert "--write" in data["generator"]


@pytest.mark.unit
class TestTheCommandLine:
    """Exit codes and what is printed. This is the surface CI and a human both read."""

    def test_a_clean_check_exits_zero_and_says_how_much_it_checked(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = _fake_tree(tmp_path)
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 0
        out = capsys.readouterr().out
        assert "1 file(s) across 1 tree(s)" in out, out

    def test_a_clean_check_still_names_the_trees_it_could_not_check(
        self, monkeypatch, tmp_path, capsys
    ):
        """Exit 0 with a tree unmeasured is the dangerous case: the run looks like
        success. It has to say which trees its success does not cover."""
        measured, absent = (
            _fake_tree(tmp_path, "measured"),
            _fake_tree(tmp_path, "absent"),
        )
        _install(
            monkeypatch,
            tmp_path,
            [measured, absent],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 0
        out = capsys.readouterr().out
        assert "not checked" in out and "absent" in out, out

    def test_a_regression_exits_one_and_prints_every_problem(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = _fake_tree(tmp_path)
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {
                "trees": {
                    tree.name: {
                        "total": 90.0,
                        "files": {"pkg/a.py": 90.0, "pkg/b.py": 90.0},
                    }
                }
            },
        )
        _write_tree_report(tree, {"a.py": 30.0, "b.py": 20.0})
        monkeypatch.setattr(
            ccd, "tracked_source_files", lambda t: ["pkg/a.py", "pkg/b.py"]
        )
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 1
        out = capsys.readouterr().out
        assert "pkg/a.py" in out and "pkg/b.py" in out, out

    def test_the_failure_output_says_the_gate_reports_rather_than_decides(
        self, monkeypatch, tmp_path, capsys
    ):
        """Neither branch here is protected, so a red result informs a human who then
        decides. Output that read as a refusal would misstate what it can do."""
        tree = _fake_tree(tmp_path)
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 90.0, "files": {"pkg/a.py": 90.0}}}},
        )
        _write_tree_report(tree, {"a.py": 10.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/a.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 1
        assert "does not decide" in capsys.readouterr().out

    def test_summary_prints_each_recorded_tree_and_exits_zero(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = _fake_tree(tmp_path)
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 83.14, "files": {"pkg/a.py": 90.0}}}},
        )
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--summary"])
        assert ccd.main() == 0
        out = capsys.readouterr().out
        assert tree.name in out and "83.14" in out, out

    def test_summary_with_no_baseline_says_so_rather_than_printing_an_empty_table(
        self, monkeypatch, tmp_path, capsys
    ):
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path)])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--summary"])
        assert ccd.main() == 0
        assert "no coverage baseline" in capsys.readouterr().out

    def test_write_records_and_exits_zero(self, monkeypatch, tmp_path, capsys):
        tree = _fake_tree(tmp_path)
        path = _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write"])
        assert ccd.main() == 0
        assert json.loads(path.read_text())["trees"][tree.name]["files"]
        assert "recorded 1 file(s)" in capsys.readouterr().out


#: A run record captured from a **real** xdist collection mismatch, verbatim apart from
#: the `hostname` attribute, which is replaced because a machine name belongs to the
#: machine that ran it and not to this repository. Everything else is as pytest wrote it,
#: the `timestamp` included.
#:
#: Reproduced deterministically rather than imagined: a test module whose collected set
#: depends on a counter file it increments on import, so each worker collects a different
#: list. pytest wrote a well-formed `coverage.xml` beside this, reporting 50% for a file
#: whose tests had not run -- which is the whole danger. What this pins is the two
#: attributes the gate reads (`errors`, and `failures` being 0 -- an errored run is not a
#: failed one) and that the recognisable text lands in the record rather than only on
#: stdout, where it is gone by the time a separate CI step runs the ratchet.
#: :func:`TestRunTrust.test_a_real_xdist_collection_mismatch_still_looks_like_this`
#: re-reproduces the failure and checks this capture still describes reality.
ERRORED_RUN_RECORD = '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="3" failures="0" skipped="0" tests="3" time="0.500" timestamp="2026-09-24T12:17:46.218448+00:00" hostname="HOST"><testcase classname="" name="gw1" time="0.000"><error message="collection failure">Different tests were collected between gw0 and gw1. The difference is:\n--- gw0\n\n+++ gw1\n\n@@ -1 +1,2 @@\n\n tests/test_collect.py::test_always\n+tests/test_collect.py::test_generated_0\nTo see why this happens see \'Known limitations\' in documentation for pytest-xdist</error></testcase><testcase classname="" name="gw2" time="0.000"><error message="collection failure">Different tests were collected between gw0 and gw2. The difference is:\n--- gw0\n\n+++ gw2\n\n@@ -1 +1,3 @@\n\n tests/test_collect.py::test_always\n+tests/test_collect.py::test_generated_0\n+tests/test_collect.py::test_generated_1\nTo see why this happens see \'Known limitations\' in documentation for pytest-xdist</error></testcase><testcase classname="" name="gw3" time="0.000"><error message="collection failure">Different tests were collected between gw0 and gw3. The difference is:\n--- gw0\n\n+++ gw3\n\n@@ -1 +1,4 @@\n\n tests/test_collect.py::test_always\n+tests/test_collect.py::test_generated_0\n+tests/test_collect.py::test_generated_1\n+tests/test_collect.py::test_generated_2\nTo see why this happens see \'Known limitations\' in documentation for pytest-xdist</error></testcase></testsuite></testsuites>'


@pytest.mark.unit
class TestReportResolution:
    """Which file each tree's figure is read from."""

    def test_the_per_tree_report_is_preferred(self, monkeypatch, tmp_path):
        tree = _fake_tree(tmp_path)
        _install(monkeypatch, tmp_path, [tree])
        report = _write_tree_report(tree, {"mod.py": 90.0})
        assert ccd._resolve_report(tree) == report

    def test_a_tree_with_no_report_resolves_to_none(self, monkeypatch, tmp_path):
        tree = _fake_tree(tmp_path)
        _install(monkeypatch, tmp_path, [tree])
        assert ccd._resolve_report(tree) is None

    def test_the_missing_report_reason_names_every_path_that_would_be_accepted(
        self, monkeypatch, tmp_path
    ):
        """Or the remedy misdirects.

        `idp_common` is accepted from two paths, and the remedy printed under this reason
        (`make test-cicd -C lib/idp_common_pkg`) produces the **legacy** one. Naming only
        the other sends a reader who followed the remedy back to a file that is still
        absent while the gate now passes.
        """
        legacy = tmp_path / "legacy-coverage.xml"
        monkeypatch.setattr(ccd, "LEGACY_IDP_COMMON_REPORT", legacy)
        idp = ccd.Tree("idp_common", str(tmp_path / "idpc"), "pkg")
        (tmp_path / "idpc" / "pkg").mkdir(parents=True, exist_ok=True)
        _install(monkeypatch, tmp_path, [idp], {"trees": {}})
        reason = ccd.check().unchecked[0].reason
        assert str(legacy) in reason and str(ccd.report_path(idp)) in reason, reason

    def test_the_legacy_coverage_xml_fallback_applies_only_to_idp_common(
        self, monkeypatch, tmp_path
    ):
        """`make test-cicd -C lib/idp_common_pkg` writes `coverage.xml`, so that one
        name is accepted for that one tree -- otherwise the existing CI step would
        stop feeding this gate. Extending the fallback to every tree would make a
        stale `coverage.xml` in any package silently stand in for a real measurement.
        """
        legacy = tmp_path / "legacy.xml"
        legacy.write_text('<?xml version="1.0" ?><coverage line-rate="0.5"/>\n')
        monkeypatch.setattr(ccd, "LEGACY_IDP_COMMON_REPORT", legacy)
        other = _fake_tree(tmp_path, "not_idp_common")
        idp = ccd.Tree("idp_common", str(tmp_path / "idpc"), "pkg")
        (tmp_path / "idpc" / "pkg").mkdir(parents=True, exist_ok=True)
        _install(monkeypatch, tmp_path, [idp, other])
        assert ccd._resolve_report(idp) == legacy
        assert ccd._resolve_report(other) is None


def _run_with_nondeterministic_collection(
    tmp_path: Path,
) -> subprocess.CompletedProcess:
    """Reproduce an xdist collection mismatch, and let it write its own reports.

    Deterministically, without a race: the test module's collected set depends on a
    counter file it increments every time it is imported, so each worker collects a
    different list and xdist errors. That is the same failure as a test file edited while
    a run is in flight, which is how it happened here, and reproducing it is the point —
    a hand-written record encoding a belief about what such a run leaves behind would be
    asserting the belief, and what the run leaves behind is the entire question.
    """
    (tmp_path / "pkgsrc").mkdir()
    (tmp_path / "pkgsrc" / "__init__.py").write_text(
        textwrap.dedent("""
            def covered():
                return 1

            def never_run():
                return 2
            """),
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_collect.py").write_text(
        textwrap.dedent('''
            """Collected set depends on a counter this module increments on import."""
            import pathlib

            import pkgsrc

            _counter = pathlib.Path(__file__).with_name("counter")
            _n = int(_counter.read_text()) if _counter.exists() else 0
            _counter.write_text(str(_n + 1))


            def test_always():
                assert pkgsrc.covered() == 1


            for _i in range(_n):
                exec(f"def test_generated_{_i}():\\n    assert pkgsrc.never_run() == 2\\n")
            '''),
        encoding="utf-8",
    )
    reports = tmp_path / "test-reports"
    reports.mkdir()
    report = reports / "coverage.xml"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))
    }
    env["PYTHONPATH"] = str(tmp_path)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            "4",
            "--cov=pkgsrc",
            f"--cov-report=xml:{report}",
            "--cov-report=",
            f"--junitxml={ccd.run_record_path(report)}",
            "tests/",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


@pytest.mark.unit
class TestRunTrust:
    """Whether a report describes a finished run. The half that has misled people twice.

    A `coverage.xml` from a run whose xdist workers errored is well-formed, parses
    cleanly, and reports large falls across files whose tests never executed. Nothing in
    it says so, and the gate used to compare it and then offer `--write` — which
    regenerates the whole baseline, so recording those falls once makes them
    indistinguishable from deliberate ones. Issue #1190.
    """

    def test_a_real_xdist_collection_mismatch_still_looks_like_this(self, tmp_path):
        """Reproduce the failure, then read what it left behind.

        Four claims, all measured here rather than assumed: the errored run **does** write
        a coverage report; it writes a run record too; that record carries a non-zero
        `errors` count with `failures` at zero; and the gate reads the pair as
        untrustworthy. If a future pytest or xdist release stops recording the mismatch in
        the record, this fails — which is the only way to notice that the signal the gate
        depends on has moved.
        """
        result = _run_with_nondeterministic_collection(tmp_path)
        assert "Different tests were collected" in (result.stdout + result.stderr), (
            "the reproduction did not produce a collection mismatch, so this test is "
            f"measuring something else:\n{result.stdout[-3000:]}"
        )
        report = tmp_path / "test-reports" / "coverage.xml"
        record = ccd.run_record_path(report)
        assert report.is_file(), (
            "an errored run wrote no coverage report at all, which would mean the gate's "
            "problem is not what #1190 describes"
        )
        assert record.is_file()
        root = ET.fromstring(record.read_text(encoding="utf-8"))
        suites = list(root.iter("testsuite"))
        assert sum(int(s.get("errors") or 0) for s in suites) > 0, record.read_text()
        assert sum(int(s.get("failures") or 0) for s in suites) == 0, (
            "the mismatch is recorded as a failure rather than an error, so the two "
            "attributes do not mean what the gate's message says they do"
        )
        trust = ccd.run_trust(report)
        assert trust.state == "errored", trust
        assert "error(s)" in trust.detail

    def test_the_captured_record_from_such_a_run_is_read_as_errored(self, tmp_path):
        """The same decision, without needing xdist installed to reach it.

        :data:`ERRORED_RUN_RECORD` is that run's record, captured. This is the test that
        holds the parsing — the counts, and that a non-zero count refuses — if the
        reproduction above is ever unable to run.
        """
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.5"/>\n')
        ccd.run_record_path(report).write_text(ERRORED_RUN_RECORD, encoding="utf-8")
        trust = ccd.run_trust(report)
        assert trust.state == "errored", trust
        assert "3 error(s)" in trust.detail, trust.detail

    def test_a_clean_record_is_trusted(self, tmp_path):
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.9"/>\n')
        _write_run_record(report, tests=812)
        assert ccd.run_trust(report).state == "clean"

    def test_a_run_with_test_failures_is_refused_too(self, tmp_path):
        """A failing run is an incomplete measurement as well: a test that failed part
        way through covered part of what it would have covered, and the lines after the
        assertion never ran. In both CIs the test step's failure stops the job before this
        gate runs, so the strictness costs nothing there and protects a local run."""
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.9"/>\n')
        _write_run_record(report, failures=2)
        trust = ccd.run_trust(report)
        assert trust.state == "errored" and "2 failure(s)" in trust.detail

    def test_a_report_with_no_record_beside_it_is_not_trusted(self, tmp_path):
        """Absent evidence is not evidence of a clean run.

        This is the fail-closed direction, and it is what stops the check being turned off
        by deleting a file: a report with nothing vouching for it is unverified, and an
        unverified tree is not checked.
        """
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.9"/>\n')
        trust = ccd.run_trust(report)
        assert trust.state == "unverified"
        assert "test-results.xml" in trust.detail

    def test_a_record_written_long_before_the_report_vouches_for_nothing(
        self, tmp_path
    ):
        """Pairing, not timing. A hand-run `pytest --cov` with no `--junitxml` leaves a
        fresh report beside an older record, and reading that record as this run's would
        be a stale clean bill of health for a measurement it never saw."""
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.9"/>\n')
        record = _write_run_record(report)
        stale = time.time() - (ccd.RUN_RECORD_PAIRING_SECONDS + 120)
        os.utime(record, (stale, stale))
        trust = ccd.run_trust(report)
        assert trust.state == "unverified", trust
        assert "different run" in trust.detail

    def test_an_error_element_under_a_zero_error_count_is_still_an_error(
        self, tmp_path
    ):
        """The attribute is the summary; the elements are the record.

        Reading only `errors="0"` would be trusting one spelling of "this run was clean".
        pytest writes both, and the larger of the two is what the gate uses, so a record
        carrying an `<error>` while claiming none is refused.
        """
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.5"/>\n')
        ccd.run_record_path(report).write_text(
            '<?xml version="1.0" ?><testsuites><testsuite name="pytest" errors="0" '
            'failures="0" tests="4"><testcase name="gw1"><error message="collection '
            'failure">Different tests were collected between gw0 and gw1</error>'
            "</testcase></testsuite></testsuites>\n",
            encoding="utf-8",
        )
        trust = ccd.run_trust(report)
        assert trust.state == "errored", trust

    def test_a_failure_element_under_a_zero_failure_count_is_still_a_failure(
        self, tmp_path
    ):
        """The same reading for `<failure>` as for `<error>`, and it needs its own test:
        removing the failure floor while leaving the error one left every other test in
        this file green."""
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.5"/>\n')
        ccd.run_record_path(report).write_text(
            '<?xml version="1.0" ?><testsuites><testsuite name="pytest" errors="0" '
            'failures="0" tests="4"><testcase name="test_x"><failure message="assert">'
            "AssertionError</failure></testcase></testsuite></testsuites>\n",
            encoding="utf-8",
        )
        assert ccd.run_trust(report).state == "errored"

    def test_the_pairing_window_cannot_be_widened_into_uselessness(self):
        """A bound on the constant, because the test above derives its skew from it.

        Measured: widening :data:`check_coverage_debt.RUN_RECORD_PAIRING_SECONDS` to 10**9
        left every other test in this file green, the staleness one included — it offsets
        the record by `the window + 120`, so the window growing grows the test with it.
        That is the shape of a check that cannot fail. An hour is already far more than the
        seconds pytest takes to write the second file, and a window of days or years would
        let a record from a previous job vouch for this one's report.
        """
        assert 60 <= ccd.RUN_RECORD_PAIRING_SECONDS <= 3600, (
            f"the pairing window is {ccd.RUN_RECORD_PAIRING_SECONDS}s. Above an hour it "
            f"admits a record from an earlier run as this run's clean bill of health; "
            f"below a minute a slow report write would be refused as a stale pair."
        )

    def test_a_record_that_does_not_parse_is_not_trusted(self, tmp_path):
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.9"/>\n')
        ccd.run_record_path(report).write_text(
            "<testsuites>truncated", encoding="utf-8"
        )
        assert ccd.run_trust(report).state == "unverified"

    def test_a_record_of_zero_tests_is_not_trusted(self, tmp_path):
        """No errors, no failures, and nothing ran: the report describes an empty run, and
        every recorded file would read as a total loss."""
        report = tmp_path / "coverage.xml"
        report.write_text('<?xml version="1.0" ?><coverage line-rate="0.0"/>\n')
        _write_run_record(report, tests=0)
        trust = ccd.run_trust(report)
        assert trust.state == "unverified" and "0 tests" in trust.detail

    @pytest.mark.parametrize(
        ("report_name", "expected"),
        [
            ("coverage.xml", "test-results.xml"),
            ("coverage-scripts.xml", "coverage-scripts-results.xml"),
        ],
    )
    def test_the_record_path_follows_the_report_that_produced_it(
        self, tmp_path, report_name, expected
    ):
        """Two producers, two report names, one rule for where the record sits. Spelled
        out here because `scripts/coverage_all.py` and `lib/idp_common_pkg/Makefile` both
        derive their `--junitxml` from this function rather than repeating a path."""
        assert ccd.run_record_path(tmp_path / report_name).name == expected


@pytest.mark.unit
class TestNothingMeasured:
    """Exit codes for every way the gate can end up with nothing to compare.

    Before this, all of them printed `✅ coverage ratchet: 0 file(s) across 0 tree(s) at
    or above their recorded coverage` and exited 0 — measured on the parent commit. In a
    job log that line is indistinguishable from a clean ratchet over the whole tree.
    """

    def _no_reports(self, monkeypatch, tmp_path, trees=None):
        trees = trees or [_fake_tree(tmp_path)]
        _install(monkeypatch, tmp_path, trees, {"trees": {}})
        return trees

    def test_no_report_at_all_refuses_where_it_used_to_pass(
        self, monkeypatch, tmp_path, capsys
    ):
        self._no_reports(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        code = ccd.main()
        out = capsys.readouterr().out
        assert code == 2, f"exit {code}; the pre-change behaviour was 0 for this input"
        assert "✅" not in out, (
            f"a run that measured nothing printed a success marker:\n{out}"
        )
        assert "measured nothing" in out, out

    def test_a_report_nothing_vouches_for_is_not_a_measurement(
        self, monkeypatch, tmp_path, capsys
    ):
        """The case a reader is most likely to misread: a `coverage.xml` is right there.

        So the refusal has to name the reason that particular report was not used, not
        merely say nothing was measured.
        """
        tree = _fake_tree(tmp_path)
        _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0}, run_record=False)
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "✅" not in out and "not checked" in out, out
        assert "no run record beside it" in out, out

    def test_an_errored_report_is_refused_without_naming_a_single_fabricated_loss(
        self, monkeypatch, tmp_path, capsys
    ):
        """The losses in such a report did not happen, so printing them is the harm.

        The pre-change gate printed one problem line per recorded file — 227 of them on
        this repository's real baseline, measured — each ending in the suggestion to
        re-record with `--write`.
        """
        tree = _fake_tree(tmp_path)
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        report = _write_tree_report(tree, {"mod.py": 12.0}, run_record=False)
        ccd.run_record_path(report).write_text(ERRORED_RUN_RECORD, encoding="utf-8")
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "refusing to compare" in out, out
        assert "fell from" not in out, (
            f"a refused report's per-file falls were printed anyway:\n{out}"
        )
        assert "✅" not in out

    def test_a_refusal_beside_a_real_finding_does_not_claim_nothing_was_measured(
        self, monkeypatch, tmp_path, capsys
    ):
        """One tree refused, another measured and genuinely regressed.

        A refusal is per tree, so "this run measured nothing" is a claim only the set of
        **checked** trees can settle. Printed over a real 50-point fall it is false in the
        worst direction: a reader dismisses the regression as another artefact of the
        unfinished run. So the heading names the trees that were measured, and says a
        finding about one of them is real.
        """
        good, bad = _fake_tree(tmp_path, "good"), _fake_tree(tmp_path, "bad")
        _install(
            monkeypatch,
            tmp_path,
            [good, bad],
            {
                "trees": {
                    "good": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                    "bad": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                }
            },
        )
        _write_tree_report(good, {"mod.py": 40.0})
        bad_report = _write_tree_report(bad, {"mod.py": 40.0}, run_record=False)
        ccd.run_record_path(bad_report).write_text(ERRORED_RUN_RECORD, encoding="utf-8")
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "fell from 90.00% to 40.00%" in out, out
        assert "measured nothing" not in out, (
            f"the heading says nothing was measured while reporting a real regression in "
            f"a tree that WAS measured:\n{out}"
        )
        assert "good" in out.split("\n")[0], (
            f"the heading does not name the measured tree, so a reader cannot tell the "
            f"real finding from the refused one:\n{out}"
        )

    def test_one_measured_tree_out_of_several_is_still_a_pass(
        self, monkeypatch, tmp_path, capsys
    ):
        """The property that keeps this usable, asserted deliberately.

        Both CIs measure `idp_common` alone, and so does the ordinary local command, so
        demanding every tree would red-line every branch for a condition no developer
        causes. Partial measurement passes; what changed is that measuring *nothing*
        does not.
        """
        measured, absent = (
            _fake_tree(tmp_path, "measured"),
            _fake_tree(tmp_path, "away"),
        )
        _install(
            monkeypatch,
            tmp_path,
            [measured, absent],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 0
        out = capsys.readouterr().out
        assert "✅" in out and "away" in out and "not checked" in out, out

    def test_require_tree_fails_when_that_tree_was_not_checked(
        self, monkeypatch, tmp_path, capsys
    ):
        """The precondition both CI steps now state. Another tree being measured must not
        satisfy it — that is exactly the substitution a changed report path makes."""
        measured, wanted = (
            _fake_tree(tmp_path, "measured"),
            _fake_tree(tmp_path, "wanted"),
        )
        _install(
            monkeypatch,
            tmp_path,
            [measured, wanted],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-tree=wanted"]
        )
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "--require-tree=wanted" in out and "✅" not in out, out

    def test_require_tree_is_satisfied_by_that_tree_being_checked(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = _fake_tree(tmp_path, "measured")
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-tree=measured"]
        )
        assert ccd.main() == 0
        assert "✅" in capsys.readouterr().out

    @pytest.mark.parametrize("mode", ["--summary", "--write"])
    def test_require_tree_is_refused_in_a_mode_that_checks_nothing(
        self, monkeypatch, tmp_path, capsys, mode
    ):
        """Refused rather than ignored.

        Neither `--summary` nor `--write` compares anything, so a caller passing
        `--require-tree` alongside one of them has stated a precondition nothing will
        evaluate — an assertion silently doing nothing, which is the shape of defect the
        flag exists to prevent.
        """
        tree = _fake_tree(tmp_path, "measured")
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", mode, "--require-tree=measured"]
        )
        assert ccd.main() == 2
        assert "--require-tree" in capsys.readouterr().out

    def test_require_tree_with_an_unknown_name_is_an_error_not_a_satisfied_check(
        self, monkeypatch, tmp_path, capsys
    ):
        """A typo must not read as a met precondition.

        `--require-tree=idp_commmon` against a list of nine trees is the shape that turns
        an assertion into decoration, and both CI configurations pass this flag from a
        string in a YAML file where nothing else would catch it.
        """
        tree = _fake_tree(tmp_path, "measured")
        _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-tree=measuredd"]
        )
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "do not exist" in out and "measuredd" in out, out


@pytest.mark.unit
class TestWriteRefusesUntrustworthyInput:
    """`--write` is the permanent half, so its refusals are stricter than the check's."""

    def test_write_records_nothing_at_all_from_an_errored_report(
        self, monkeypatch, tmp_path, capsys
    ):
        """Not "skips that tree": records nothing.

        `--write` regenerates the whole file, and the operator's next act is to commit it.
        Recording the other eight trees while silently dropping the bad one would leave
        the baseline half-refreshed and read as a success.
        """
        good, bad = _fake_tree(tmp_path, "good"), _fake_tree(tmp_path, "bad")
        path = _install(
            monkeypatch,
            tmp_path,
            [good, bad],
            {"trees": {"good": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(good, {"mod.py": 95.0})
        bad_report = _write_tree_report(bad, {"mod.py": 11.0}, run_record=False)
        ccd.run_record_path(bad_report).write_text(ERRORED_RUN_RECORD, encoding="utf-8")
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        before = path.read_text()
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write"])
        assert ccd.main() == 2
        assert path.read_text() == before, "the baseline was rewritten anyway"
        out = capsys.readouterr().out
        assert "recorded nothing" in out and "bad" in out, out

    def test_write_leaves_an_unverified_trees_baseline_untouched(
        self, monkeypatch, tmp_path, capsys
    ):
        """A report nothing vouches for is treated as no report: its recorded entries
        survive, rather than being replaced by figures from a run that may not have
        finished."""
        verified, unverified = (
            _fake_tree(tmp_path, "verified"),
            _fake_tree(tmp_path, "unverified"),
        )
        path = _install(
            monkeypatch,
            tmp_path,
            [verified, unverified],
            {"trees": {"unverified": {"total": 77.0, "files": {"pkg/old.py": 77.0}}}},
        )
        _write_tree_report(verified, {"mod.py": 90.0})
        _write_tree_report(unverified, {"mod.py": 5.0}, run_record=False)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write"])
        assert ccd.main() == 0
        written = json.loads(path.read_text())["trees"]
        assert written["unverified"] == {"total": 77.0, "files": {"pkg/old.py": 77.0}}
        assert written["verified"]["files"] == {"pkg/mod.py": 90.0}

    def test_write_with_nothing_recordable_refuses_rather_than_rewriting_the_file(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = _fake_tree(tmp_path)
        path = _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        before = path.read_text()
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write"])
        assert ccd.main() == 2
        assert path.read_text() == before
        assert "recorded nothing" in capsys.readouterr().out


@pytest.mark.unit
class TestRunRecordWiring:
    """Every producer of a report must also produce the record that vouches for it.

    This is the pin against the trust check going quietly inert. Dropping `--junitxml`
    from a producer does not make the gate lenient — every tree becomes unverified, and a
    run with nothing verifiable refuses — but it does make the gate useless, and it would
    be a one-word edit in a file nobody reads alongside this one. Both expectations are
    **derived** from `run_record_path`, so renaming either half fails here.
    """

    def test_the_idp_common_suite_writes_the_record_beside_its_report(self):
        makefile = (REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile").read_text()
        report = ccd.LEGACY_IDP_COMMON_REPORT
        expected = f"--junitxml=test-reports/{ccd.run_record_path(report).name}"
        assert f"--cov-report=xml:test-reports/{report.name}" in makefile, (
            "the idp_common suite no longer writes the report this gate reads; the "
            "gate's resolver and this Makefile have to agree"
        )
        assert expected in makefile, (
            f"lib/idp_common_pkg/Makefile does not pass {expected}, so the report it "
            f"writes has nothing vouching for it and the coverage ratchet can no longer "
            f"tell a finished run from one whose workers errored"
        )

    def test_the_per_tree_runner_writes_the_record_beside_each_report(
        self, monkeypatch
    ):
        """Read from the command `coverage_all.run` actually builds, not from its source.

        A grep for `--junitxml` would pass on a line that computed the wrong path.
        """
        spec = importlib.util.spec_from_file_location(
            "coverage_all_probe", REPO_ROOT / "scripts" / "coverage_all.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tree = ccd.TREES_BY_NAME["idp_common"]
        issued: list[list[str]] = []

        def fake_run(cmd, cwd=None, **kwargs):
            issued.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        module.run(tree, sys.executable, parallel=False)
        expected = f"--junitxml={ccd.run_record_path(ccd.report_path(tree))}"
        assert expected in issued[0], (
            f"coverage_all.py does not pass {expected}; every tree it measures would be "
            f"unverifiable, and `make coverage-all` followed by the ratchet would check "
            f"nothing:\n{issued[0]}"
        )


@pytest.mark.unit
class TestTheCIPreconditionNeedsNoCIChange:
    """What protects the two CI steps is the gate's own refusal, not a flag they pass.

    The ratchet runs as a separate step from the run that writes its input in both
    configurations, and nothing asserted that the input existed. The assertion now lives
    in the script and is unconditional, which is why neither CI needs an argument: a step
    whose report never appeared checks no tree, and a run that checked no tree refuses.
    Both halves are pinned here — the invocation order, and the refusal reaching an
    invocation that passes nothing — because a change to either would restore the gap
    while leaving the other looking intact.
    """

    @pytest.mark.parametrize(
        "config", [".gitlab-ci.yml", ".github/workflows/developer-tests.yml"]
    )
    def test_each_config_runs_the_ratchet_after_its_producer_in_the_SAME_job(
        self, config
    ):
        """Same job, and in order — not merely further down the file.

        File position is the cheap reading and it is not the guarantee: two GitLab jobs in
        one stage run in **parallel**, so moving the ratchet into a job of its own would
        leave a position-based assertion green with the ordering gone. So the YAML is
        parsed and the two commands are located in one job's own command list.
        """
        # Imported at module scope, not behind `importorskip`: PyYAML is a hard
        # dependency of `idp_common` and a dozen suites here import it outright, so a
        # skip would only be a way for this assertion to stop running.
        config_data = yaml.safe_load((REPO_ROOT / config).read_text(encoding="utf-8"))
        producer, ratchet = (
            "make test-cicd -C lib/idp_common_pkg",
            ("make check-coverage-debt"),
        )

        def command_lists(node):
            """Every ordered list of shell commands this config declares, as strings."""
            if isinstance(node, dict):
                steps = node.get("steps")
                if isinstance(steps, list):  # a GitHub job
                    yield [str(s.get("run", "")) for s in steps if isinstance(s, dict)]
                script = node.get("script")
                if isinstance(script, list):  # a GitLab job
                    yield [str(line) for line in script]
                for value in node.values():
                    yield from command_lists(value)

        holding_both = [
            commands
            for commands in command_lists(config_data)
            if any(producer in c for c in commands)
            and any(ratchet in c for c in commands)
        ]
        assert holding_both, (
            f"{config} has no single job running both `{producer}` and `{ratchet}`. "
            f"Either the ratchet is gone, or it now runs in a job of its own — where "
            f"nothing orders it after the run that writes the report it reads."
        )
        for commands in holding_both:
            first = next(i for i, c in enumerate(commands) if producer in c)
            after = next(i for i, c in enumerate(commands) if ratchet in c)
            assert after > first, (
                f"{config} runs the ratchet at position {after}, before its producer at "
                f"{first}, so it reads whatever report the workspace already had."
            )

    def test_an_invocation_with_no_arguments_still_refuses_a_missing_report(
        self, monkeypatch, tmp_path, capsys
    ):
        """The CI invocation, argument for argument: `check_coverage_debt.py` and nothing
        else. If the refusal ever needs a flag to fire, both CI steps go back to passing
        on a run that measured nothing, and no edit to either config file would show it."""
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path)], {"trees": {}})
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 2
        assert "✅" not in capsys.readouterr().out

    def test_the_makefile_target_can_carry_the_named_precondition(self):
        """`--require-tree` has to be reachable through the target both CIs call, or it is
        a flag with no route to the callers that would use it."""
        recipe = next(
            block
            for block in (REPO_ROOT / "Makefile").read_text().split("\ncheck-")
            if block.startswith("coverage-debt:")
        )
        assert "$(CHECK_COVERAGE_DEBT_ARGS)" in recipe.split("\n\n")[0], recipe[:400]


def _make_would_run(target: str) -> list[str]:
    """The commands `make <target>` would actually execute, from `make -n`.

    Reading the recipe as text cannot answer this. `make` strips a leading `@`, expands
    variables, and — the case that matters — treats everything after a `#` as a comment, so
    a recipe whose flag has been moved into a trailing comment still *contains* that flag
    while not passing it. Measured: moving `--require-all-trees` into a trailing comment on
    the `check-coverage-debt-cicd` recipe left every text-based assertion here green while
    the gate silently went back to naming unmeasured trees and exiting 0 — which is issue
    #1256 restored with no red mark anywhere. So the question is put to `make` itself.
    """
    out = subprocess.run(
        ["make", "-n", "--no-print-directory", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"make -n {target} failed: {out.stderr[-400:]}"
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    assert lines, f"make -n {target} would run nothing at all"
    # Split each line the way the SHELL will, discarding `#` comments. `make` does not
    # treat `#` inside a recipe as a comment -- it hands the whole line to the shell, which
    # does -- so a flag moved after a `#` survives in `make -n` output while never reaching
    # the program. Measured: that is the one spelling `make -n` alone still accepted.
    # Tokens are rejoined per line so callers can go on matching substrings.
    argv_lines = []
    for line in lines:
        try:
            argv_lines.append(" ".join(shlex.split(line, comments=True)))
        except ValueError:  # pragma: no cover - unbalanced quoting in a recipe
            argv_lines.append(line)
    return [ln for ln in argv_lines if ln]


def _invoked_commands(config: Path) -> set[str]:
    """Every shell command a CI config actually runs, with comments removed.

    GitHub spells a step's command `run: <cmd>`; GitLab spells it `- <cmd>` inside a
    `script:` list. Both allow a trailing `# ...` comment on the same line, and both allow
    a whole line to be commented out — so the text of a disabled invocation survives in the
    file, and a substring search cannot tell it from a live one. That is the difference this
    helper exists to make, because the gate being checked here is one whose entire defect
    was reading as present while doing nothing.
    """
    commands: set[str] = set()
    for raw in config.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("run:"):
            line = line[len("run:") :]
        elif line.startswith("- "):
            line = line[2:]
        else:
            continue
        # Strip a trailing same-line comment. No CI command here carries a literal `#`.
        commands.add(line.split("#")[0].strip())
    return commands


@pytest.mark.unit
class TestRequireAllTreesIsDerivedFromTheRegistry:
    """What both CI steps now pass, and why it is one flag rather than nine arguments.

    Before it existed, CI produced one report of nine and this gate **named** the other
    eight as "not checked (no report from this run)" and exited 0. Eight trees'
    baselines — including the two files that are this repository's own commit-text and
    shared-branch guards — were therefore ratcheted by nothing while a green tick said
    otherwise. Issue #1256.

    The property that matters is that the required set is **derived from** :data:`TREES`,
    not written out by the caller. A Makefile or CI config spelling the nine names would be
    a second copy of the registry, and the copy that rots is the one deciding what the gate
    may skip: a tenth tree added to `TREES` and forgotten in that list would be unratcheted
    while the gate reported that every tree was required.
    """

    def test_it_fails_naming_every_tree_that_produced_no_report(
        self, monkeypatch, tmp_path, capsys
    ):
        measured = _fake_tree(tmp_path, "measured")
        absent_a = _fake_tree(tmp_path, "absent_a")
        absent_b = _fake_tree(tmp_path, "absent_b")
        _install(
            monkeypatch,
            tmp_path,
            [measured, absent_a, absent_b],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-all-trees"]
        )
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert "✅" not in out, out
        for name in ("absent_a", "absent_b"):
            assert f"--require-tree={name}" in out, out

    def test_without_the_flag_the_same_state_passes_which_is_the_defect(
        self, monkeypatch, tmp_path, capsys
    ):
        """The control. Two of three trees unmeasured and the gate exits 0 and prints ✅ —
        the state both CIs were in. Asserting the flag's effect without asserting that the
        unflagged run behaves differently would leave the flag untested against a gate
        that already failed on its own."""
        measured = _fake_tree(tmp_path, "measured")
        absent_a = _fake_tree(tmp_path, "absent_a")
        absent_b = _fake_tree(tmp_path, "absent_b")
        _install(
            monkeypatch,
            tmp_path,
            [measured, absent_a, absent_b],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 0
        assert "✅" in capsys.readouterr().out

    def test_it_is_satisfied_when_every_tree_was_checked(
        self, monkeypatch, tmp_path, capsys
    ):
        """The other direction, so the flag is not simply "always fail"."""
        a, b = _fake_tree(tmp_path, "a"), _fake_tree(tmp_path, "b")
        _install(
            monkeypatch,
            tmp_path,
            [a, b],
            {
                "trees": {
                    "a": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                    "b": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                }
            },
        )
        for tree in (a, b):
            _write_tree_report(tree, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-all-trees"]
        )
        assert ccd.main() == 0
        assert "✅" in capsys.readouterr().out

    def test_a_tree_added_to_the_registry_is_required_without_editing_anything(
        self, monkeypatch, tmp_path, capsys
    ):
        """The derivation, asserted as a derivation.

        A test that only checked today's nine names would pass over an implementation that
        hardcoded today's nine names, which is the failure mode this flag exists to avoid.
        So the registry is given a tree that did not exist when the flag was written, and
        the flag has to require it.
        """
        measured = _fake_tree(tmp_path, "measured")
        newcomer = _fake_tree(tmp_path, "a_tree_invented_by_this_test")
        _install(
            monkeypatch,
            tmp_path,
            [measured, newcomer],
            {"trees": {"measured": {"total": 90.0, "files": {"pkg/mod.py": 90.0}}}},
        )
        _write_tree_report(measured, {"mod.py": 90.0})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-all-trees"]
        )
        assert ccd.main() == 2
        assert "a_tree_invented_by_this_test" in capsys.readouterr().out

    def test_it_is_refused_in_a_mode_that_checks_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        """Same reason `--require-tree` is: `--summary` and `--write` compare nothing, so
        a caller passing either alongside this has asserted a precondition nothing will
        evaluate, and an ignored assertion is the defect the flag exists for."""
        tree = _fake_tree(tmp_path, "measured")
        _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        for mode in ("--summary", "--write"):
            monkeypatch.setattr(
                sys, "argv", ["check_coverage_debt.py", mode, "--require-all-trees"]
            )
            assert ccd.main() == 2, mode
            assert "--require-tree" in capsys.readouterr().out, mode

    def test_both_cis_reach_the_target_that_carries_it(self):
        """A flag no caller passes protects nothing. `check-coverage-debt-cicd` is the
        target both CI configurations invoke, and it has to be the one carrying the flag —
        the plain target deliberately does not, because a developer measuring one tree
        locally would be red for eight they never intended to measure."""
        # Asked of `make`, not of the recipe text: see `_make_would_run`.
        cicd = _make_would_run("check-coverage-debt-cicd")
        assert any("--require-all-trees" in ln for ln in cicd), cicd
        plain = _make_would_run("check-coverage-debt")
        assert not any("--require-all-trees" in ln for ln in plain), plain
        # And the producer really invokes the producer, with a budget.
        producer = _make_would_run("coverage-all-cicd")
        assert any("scripts/coverage_all.py" in ln for ln in producer), producer
        assert any("--jobs" in ln for ln in producer), producer
        assert any("--skip" in ln for ln in producer), producer
        # Matched as an INVOKED command, not as text anywhere in the file. A plain
        # substring search over the config passes for `run: true  # make
        # coverage-all-cicd` — a step commented out in place, which is how a CI gate
        # gets switched off in a diff that looks like a comment. Measured: that exact
        # mutation left this assertion green in its substring form.
        for config in (
            REPO_ROOT / ".gitlab-ci.yml",
            REPO_ROOT / ".github/workflows/developer-tests.yml",
        ):
            invoked = _invoked_commands(config)
            for target in ("make coverage-all-cicd", "make check-coverage-debt-cicd"):
                assert any(target == cmd for cmd in invoked), (
                    f"{config.name} does not INVOKE `{target}`. It may still mention "
                    f"it in a comment or behind a disabled step, which is not the same "
                    f"thing. Commands found: {sorted(invoked)[:12]}"
                )


#: `on:` of the workflow that holds the coverage gates, pinned.
#:
#: Narrowing this leaves both steps present, unconditional and correctly ordered in a job
#: that never runs on a pull request to the integration branch -- an escape no assertion
#: about a step can reach. Measured: `branches: ["release/**"]` left every other check here
#: green. Pinned rather than shape-checked, because `branches`, `branches-ignore`, `paths`
#: and `types` can each narrow it and enumerating the ways is the denylist mistake again.
GITHUB_WORKFLOW_TRIGGER = {"pull_request": {"branches": ["**"]}}

#: The GitHub gate job, **everything except its steps**, pinned as one mapping.
#:
#: Pinned wholesale rather than key by key, because naming the keys that matter is the
#: enumerate-today's-cases mistake a third time and the space is not enumerable. Five ways
#: past a key-by-key pin were measured, each leaving every step present and correct in a job
#: that produces no red mark: `needs:` on a job that is itself skipped, an empty `strategy`
#: matrix, a `runs-on` label no runner has, and — one scope from the keys that were pinned —
#: `defaults: run: shell:` naming a valid custom shell that swallows every step's status.
#: `steps` is excluded because it legitimately churns and is asserted in detail elsewhere.
GITHUB_GATE_JOB = {
    "name": "Lint, Type Check, and Test",
    "runs-on": "ubuntu-latest",
    "timeout-minutes": 120,
    "permissions": {"contents": "read", "issues": "read", "checks": "write"},
    "container": {"image": "python:3.13-bookworm"},
}

#: GitLab's top-level `workflow:`, pinned. It is the counterpart of GitHub's `on:`.
#:
#: `workflow: rules: - when: never`, or a never-true rule here, disables **every job in the
#: pipeline** in three lines — measured, with every other check green. Pinning the job's own
#: `rules` cannot see it, because it is a scope above.
GITLAB_PIPELINE_WORKFLOW = {
    "rules": [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
        {"if": "$CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS", "when": "never"},
        {
            "if": '$CI_COMMIT_BRANCH == "develop"',
            "auto_cancel": {"on_new_commit": "none"},
        },
        {"if": "$CI_COMMIT_BRANCH"},
    ]
}

#: `rules:` of the GitLab job that holds them, pinned, for that reason and one more.
#:
#: A `rules:` list cannot be forbidden the way a step's `if:` can, and a never-true
#: condition is non-empty and carries no `when`, so neither a presence check nor a
#: `when: never` check sees it.
GITLAB_GATE_JOB_RULES = [
    {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
    {"if": "$CI_COMMIT_BRANCH"},
]


@pytest.mark.unit
class TestTheCIStepsAreLiveAndNotMerelyPresent:
    """Present is not running, and this gate's whole subject is a check that read as
    protection while providing none.

    A step can be in a CI config and do nothing, and the diff that arranges it reads like
    housekeeping every time. All of these were measured to leave a text-based suite — and a
    first attempt at a structural one — completely green:

    * ``continue-on-error: true`` on the **step**: it runs, prints red, job stays green.
    * ``continue-on-error: true`` on the **job**: one line, and every gate in that job is
      neutered at once, and most of the shared gate set are steps in ``developer_tests``.
      The count is deliberately not written here: it moves whenever a gate is added, and
      what matters is that one line reaches all of them.
    * ``if: false``, ``if: ${{false}}``, ``if: ${{ !always() }}``,
      ``if: ${{ github.event_name == 'never_happens' }}``: the step never runs. A denylist
      of literal spellings loses to whitespace, and then to semantics.
    * GitLab ``allow_failure: true``, ``when: never``, or ``rules: [{when: never}]`` on the
      job.
    * the producer ordered **after** the ratchet, or moved into a **different job** — where
      a step list flattened across jobs is satisfied by mere file order.
    * a ``-`` prefix on the make recipe line (``-@python3 …``), which tells `make` to ignore
      the command's exit status. Measured end to end: ``make check-coverage-debt-cicd``
      exits **0** while printing ``🚫 coverage ratchet: no verdict``. ``make -n`` prints the
      command intact, so asking `make` what it would *run* is structurally blind to it the
      same way a text search was blind to ``#``.

    The rule these all violate is one rule — **a gate's red mark must be able to reach the
    thing that decides the merge** — so the assertions are about that rather than about
    spellings. Two consequences worth stating: the two steps must be **unconditional**
    (any ``if:`` at all is a finding here, because deciding whether an expression can ever
    be true is evaluation rather than reading, and these two steps have no reason to carry
    one), and both must live in the **same** job, which is what makes the ordering question
    meaningful.
    """

    #: Keys that make a step's or a job's failure not count, in either platform's spelling.
    FAILURE_SWALLOWING = ("continue-on-error", "allow_failure")

    GATE_COMMANDS = ("make coverage-all-cicd", "make check-coverage-debt-cicd")

    @staticmethod
    def _github_jobs() -> dict:
        doc = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/developer-tests.yml").read_text(
                encoding="utf-8"
            )
        )
        jobs = doc.get("jobs", {})
        assert jobs, "no jobs parsed out of developer-tests.yml"
        return jobs

    def _github_job_running(self, command: str) -> tuple[str, dict, dict]:
        """The (job name, job, step) whose ``run`` IS this command. Exactly one."""
        found = [
            (name, job, step)
            for name, job in self._github_jobs().items()
            for step in (job.get("steps") or [])
            if str(step.get("run", "")).strip() == command
        ]
        assert len(found) == 1, (
            f"expected exactly one GitHub step whose `run` is `{command}`, found "
            f"{len(found)}. A step that merely MENTIONS the command, in a comment or "
            f"inside a longer shell line, does not count."
        )
        return found[0]

    @pytest.mark.parametrize("command", GATE_COMMANDS)
    def test_no_github_step_or_its_job_swallows_the_failure(self, command):
        name, job, step = self._github_job_running(command)
        for scope, obj in (("step", step), (f"job `{name}`", job)):
            for key in self.FAILURE_SWALLOWING:
                value = obj.get(key)
                # Truthiness, not `is True`: YAML `'true'` is a string and passes an
                # identity test while GitHub still honours it.
                swallowed = value is not None and str(value).strip().lower() in (
                    "true",
                    "yes",
                    "on",
                    "1",
                )
                assert not swallowed, (
                    f"`{command}`: {scope} sets {key}={value!r}, so a red result does not "
                    f"reach whatever decides the merge. At job scope this disables every "
                    f"gate in the job, not just this one."
                )

    @pytest.mark.parametrize("command", GATE_COMMANDS)
    def test_neither_github_step_is_conditional_at_all(self, command):
        """Any `if:` is a finding, rather than a denylist of spellings that are false.

        `if: false`, `if: ${{false}}`, `if: ${{ !always() }}` and
        `if: ${{ github.event_name == 'never_happens' }}` are all permanently false and only
        the first two look it. Deciding whether an arbitrary expression can ever hold is
        evaluation, not reading, and nothing here attempts it — so the property asserted is
        the one that needs no evaluation: these two steps are unconditional.
        """
        _, job, step = self._github_job_running(command)
        assert "if" not in step, (
            f"`{command}` is behind `if: {step.get('if')!r}`. Whether that can ever be true "
            f"is not something this test can decide, which is why any condition on this "
            f"step is a finding: a gate behind a false condition is indistinguishable in a "
            f"job log from one that passed."
        )
        assert "if" not in job, f"its job is conditional: if={job.get('if')!r}"

    def test_both_github_steps_are_in_the_same_job_and_in_the_right_order(self):
        """Ordering only means something within a job, and a flattened step list hides that.

        Moved into a job of its own the producer has no checkout and no dependency on this
        job, so it cannot write the reports this job's ratchet reads — while a step list
        gathered across all jobs still shows it "before" by file order.
        """
        producer_job, job, _ = self._github_job_running("make coverage-all-cicd")
        ratchet_job, _, _ = self._github_job_running("make check-coverage-debt-cicd")
        assert producer_job == ratchet_job, (
            f"the producer is in job `{producer_job}` and the ratchet in `{ratchet_job}`, so "
            f"the ratchet cannot read the reports the producer writes — a separate job is a "
            f"separate filesystem here."
        )
        runs = [str(s.get("run", "")).strip() for s in job["steps"]]
        assert runs.index("make coverage-all-cicd") < runs.index(
            "make check-coverage-debt-cicd"
        ), runs

    def test_gitlab_runs_both_in_one_job_that_can_actually_fail(self):
        doc = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
        named = [
            (name, job)
            for name, job in doc.items()
            if isinstance(job, dict)
            and any(
                "make check-coverage-debt-cicd" in str(line)
                for line in (job.get("script") or [])
            )
        ]
        assert len(named) == 1, (
            f"expected one GitLab job to run the ratchet, got {named!r}"
        )
        name, job = named[0]
        for key in self.FAILURE_SWALLOWING:
            value = job.get(key)
            assert value is None or str(value).strip().lower() not in (
                "true",
                "yes",
                "on",
                "1",
            ), f"job `{name}` sets {key}={value!r}, so its red mark blocks nothing"
        # `when` is an ALLOWLIST, not a denylist. `never` is the obvious way to stop a job
        # running and `manual` is the quiet one: the job then exists, reads as skipped, and
        # waits for a button nobody presses on a merge request.
        when = job.get("when")
        assert when in (None, "on_success"), (
            f"job `{name}` sets when={when!r}. Only `on_success` (or nothing) lets this "
            f"gate block anything: `never` skips it, `manual` waits for a button press."
        )
        # `rules:` cannot be forbidden the way a step's `if:` can, and whether a rule can
        # ever hold is evaluation rather than reading -- a never-true condition
        # (`$CI_COMMIT_BRANCH == "a-branch-that-never-exists"`) is non-empty, carries no
        # `when`, and never runs, so neither a presence check nor a `when: never` check
        # sees it. The list is PINNED instead: any edit to it is a finding somebody reads,
        # which is the only reading-based answer that works here.
        assert job.get("rules") == GITLAB_GATE_JOB_RULES, (
            f"job `{name}`'s `rules:` no longer match what was recorded. That is not "
            f"necessarily wrong, but it decides whether this gate runs at all, and a "
            f"never-true condition is indistinguishable from a live one without "
            f"evaluating it. Confirm the job still runs on merge requests and on branch "
            f"pushes, then update GITLAB_GATE_JOB_RULES.\n"
            f"  recorded: {GITLAB_GATE_JOB_RULES}\n  now:      {job.get('rules')}"
        )
        script = [str(line).strip() for line in job["script"]]
        assert script.index("make coverage-all-cicd") < script.index(
            "make check-coverage-debt-cicd"
        ), script
        for line in script:
            if any(cmd.split()[-1] in line for cmd in self.GATE_COMMANDS):
                assert "|| true" not in line and not line.rstrip().endswith("|| :"), (
                    line
                )

    def test_the_github_workflow_still_triggers_on_every_pull_request(self):
        """The escape that is nowhere near either step: narrow the workflow's own trigger.

        `on: pull_request: branches: ["release/**"]` leaves both steps present,
        unconditional and correctly ordered inside a job that never runs on a pull request
        to the integration branch. Nothing about a step can see that, so the trigger is
        pinned and changing it is a finding somebody reads.
        """
        raw = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/developer-tests.yml").read_text(
                encoding="utf-8"
            )
        )
        # PyYAML resolves the bare key `on` to the boolean True, so a lookup by the string
        # silently finds nothing -- which would make this assertion compare None to the
        # recorded value and fail loudly rather than pass vacuously, but only by luck.
        trigger = raw.get("on", raw.get(True))
        assert trigger == GITHUB_WORKFLOW_TRIGGER, (
            "the workflow holding the coverage gates no longer triggers on what was "
            "recorded, so both steps can be present and correct in a job that never "
            f"runs.\n  recorded: {GITHUB_WORKFLOW_TRIGGER}\n  now:      {trigger}\n"
            "If deliberate, confirm the gates still run on a pull request targeting the "
            "integration branch, then update GITHUB_WORKFLOW_TRIGGER."
        )

    #: A deterministic way to make each target's command fail, needing no coverage report.
    #:
    #: An unknown tree name: both scripts reject one and exit 2, whatever reports happen to
    #: be lying around. That matters because the alternative -- run the target and hope the
    #: ratchet refuses -- depends on the local tree having no reports, which is true in CI
    #: and false on a developer's machine after `make coverage-all`.
    #: The gated targets, and the script each one must be running for the stub to bite.
    GATED_TARGET_SCRIPTS = {
        "check-coverage-debt-cicd": "scripts/check_coverage_debt.py",
        "coverage-all-cicd": "scripts/coverage_all.py",
    }

    FORCED_FAILURE = {
        "check-coverage-debt-cicd": (
            "CHECK_COVERAGE_DEBT_ARGS=--require-tree=a_tree_that_does_not_exist"
        ),
        "coverage-all-cicd": ("COVERAGE_CICD_SKIP=--skip a_tree_that_does_not_exist"),
    }

    @pytest.mark.parametrize("target", sorted(FORCED_FAILURE))
    def test_a_failing_command_makes_the_target_fail(self, target):
        """RUN the target and require a non-zero exit, rather than reading its recipe.

        Reading cannot close this. Every textual rule invites the next spelling, and three
        were measured, each making `make check-coverage-debt-cicd` **exit 0** while printing
        `🚫 coverage ratchet: no verdict`:

        * ``-@python3 …`` — the `-` prefix tells make to ignore the command's status.
        * ``@-python3 …`` — the same thing with two characters transposed. make strips any
          leading run of ``@ - +`` in any order, so a rule that checks for one order misses
          the other.
        * ``.IGNORE: check-coverage-debt-cicd`` — outside the recipe altogether, so a rule
          about recipe lines cannot see it at all. ``MAKEFLAGS += -i`` is the same shape, and
          so is ``|| true`` appended to the line.

        Running the target answers all of them and anything else of the kind, because the
        property asserted is the one that matters — a failing command makes the target fail —
        rather than any of the ways of breaking it. It costs one sub-second `make` call: the
        command is given an unknown tree name, which both scripts refuse without measuring
        anything.
        """
        override = self.FORCED_FAILURE[target]
        name, _, value = override.partition("=")
        out = subprocess.run(
            ["make", "--no-print-directory", target, f"{name}={value}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert out.returncode != 0, (
            f"`make {target}` exited 0 although its command failed. Its output was:\n"
            f"{(out.stdout + out.stderr)[-800:]}\n"
            f"A recipe line prefixed with `-` or `@-`, suffixed with `|| true`, or named in "
            f"`.IGNORE:`/`MAKEFLAGS += -i` makes the target succeed however the command "
            f"fails — so this gate would report nothing while both CIs stayed green."
        )

    @pytest.mark.parametrize("target", sorted(GATED_TARGET_SCRIPTS))
    def test_a_failing_command_fails_the_target_on_CIs_own_invocation(
        self, target, tmp_path
    ):
        """Make the command fail without changing a single thing make sees.

        This is the assertion that covers **both** targets, and the reason it can is that it
        moves the forced failure out of make's arguments entirely: a stub `python3` earlier
        on `PATH` exits 2, so every make variable is exactly what CI passes and there is
        nothing on the make side to branch on.

        That property is what the two weaker forms lack. A forced-failure *argument* is
        observable and a recipe can key off it — measured on both recipes, in opposite
        polarities: ``|| [ -z "$(CHECK_COVERAGE_DEBT_ARGS)" ]`` on the ratchet and
        ``|| [ "$(COVERAGE_CICD_SKIP)" = "--skip idp_common" ]`` on the producer each failed
        for the probe and **succeeded for CI**, with every other check green. Comparing exit
        statuses fixes that for the ratchet but cannot reach the producer, whose real command
        measures eight trees and takes minutes.

        Under the stub both targets refuse in about 0.01 s, so this is the cheap form as well
        as the strong one, and it catches every swallow: ``-``/``@-`` prefixes, ``|| true``,
        ``.IGNORE:``, ``MAKEFLAGS += -i``, ``.SHELLFLAGS``, a trailing pipe, and both
        conditional clauses above. The residual, stated rather than hidden: a recipe could
        branch on ``$PATH``. That is less plausible than branching on a variable the Makefile
        defines itself, and the exit-status comparison below carries the same residual.
        """
        stub = tmp_path / "bin"
        stub.mkdir()
        fake = stub / "python3"
        fake.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        fake.chmod(0o755)
        out = subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
        )
        assert out.returncode != 0, (
            f"`make {target}` exited 0 although the interpreter running its command exited "
            f"2, so the target does not carry its command's status and this gate reports "
            f"and blocks nothing. A `-`/`@-` prefix, a trailing `|| true`, `.IGNORE:`, "
            f"`MAKEFLAGS += -i`, `.SHELLFLAGS`, or a clause branching on one of the "
            f"recipe's own variables are all ways to lose it.\n"
            f"make said:\n{(out.stdout + out.stderr)[-500:]}"
        )

    def test_the_ratchet_target_exits_exactly_as_its_command_does(self):
        """RUN the target and its command with **no override**, and require equal statuses.

        The forced-failure test above supplies an argument CI does not, and anything
        observable can be branched on: appending ``|| [ -z "$(CHECK_COVERAGE_DEBT_ARGS)" ]``
        to this recipe was measured to fail for the probe and **succeed for CI**, with every
        other check here green. This assertion is taken through the same command line CI
        uses, so there is nothing about the probe left to branch on, and it subsumes every
        swallow: `-`/`@-` prefixes, `|| true`, `.IGNORE:`, `MAKEFLAGS += -i`,
        `.SHELLFLAGS`, a trailing pipe.

        Only this target, and deliberately: `coverage-all-cicd`'s command measures eight
        trees and takes minutes, so running it here is not affordable. The mechanisms that
        are **global** to the makefile (`.IGNORE`, `MAKEFLAGS`, `.SHELLFLAGS`) are caught
        for both targets by this one test, since they are properties of the file rather
        than of a recipe. What is left uncovered for the producer is a swallow written into
        **its own** recipe line while the ratchet's stays clean — that one is caught by the
        forced-failure test above, which is why both exist.
        """
        command = ["python3", "scripts/check_coverage_debt.py", "--require-all-trees"]
        direct = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
        through_make = subprocess.run(
            ["make", "--no-print-directory", "check-coverage-debt-cicd"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert through_make.returncode == direct.returncode, (
            f"`make check-coverage-debt-cicd` exited {through_make.returncode} while "
            f"`{' '.join(command)}` exited {direct.returncode}. The target must carry its "
            f"command's status or this gate blocks nothing.\n"
            f"make said:\n{(through_make.stdout + through_make.stderr)[-500:]}"
        )

    def test_the_gate_job_is_structurally_what_was_recorded(self):
        """Everything about the job except its steps, compared as one mapping.

        Five escapes live in these keys and none is about a step: a dependency on a job that
        is itself skipped, an empty matrix, a runner label nothing matches, and a `defaults:
        run: shell:` naming a valid custom shell that returns 0 however the step fared. Each
        left every other assertion here green. Adding those four names to a list would have
        been the enumerate-today's-cases move for a third time, and the next unpinned key
        would be the next finding — so the whole mapping is pinned and any structural change
        is one diff somebody reads.
        """
        job = dict(self._github_jobs()["developer_tests"])
        job.pop("steps", None)
        assert job == GITHUB_GATE_JOB, (
            "the job holding the coverage gates is structurally different from what was "
            "recorded. Keys here decide whether it runs and whether a red step reaches the "
            "merge decision at all.\n"
            f"  recorded: {GITHUB_GATE_JOB}\n  now:      {job}\n"
            "If the change is deliberate, confirm the job still runs AND still goes red on "
            "a pull request, then update GITHUB_GATE_JOB."
        )

    def test_the_gitlab_pipeline_itself_still_runs(self):
        """The scope above every job, and GitLab's counterpart of GitHub's `on:`.

        `workflow: rules: - when: never` — or a rule that is merely never true — disables
        every job in the pipeline, so every gate in this repository stops running, in three
        lines that look like ordinary pipeline hygiene. Pinning a job's own `rules` cannot
        see it.
        """
        doc = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
        assert doc.get("workflow") == GITLAB_PIPELINE_WORKFLOW, (
            "`.gitlab-ci.yml`'s top-level `workflow:` differs from what was recorded. This "
            "governs whether the pipeline runs at all, so a change here can silence every "
            "gate in the repository.\n"
            f"  recorded: {GITLAB_PIPELINE_WORKFLOW}\n  now:      {doc.get('workflow')}\n"
            "If deliberate, confirm a merge request still starts a pipeline, then update "
            "GITLAB_PIPELINE_WORKFLOW."
        )

    def test_the_forced_failure_really_is_the_command_failing(self):
        """The control for the test above, which would otherwise pass on a target that
        always fails, or on a `make` that could not find the target at all."""
        for target in self.FORCED_FAILURE:
            out = subprocess.run(
                ["make", "-n", "--no-print-directory", target],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            assert out.returncode == 0, (
                f"`make -n {target}` fails, so the test above would pass for the wrong "
                f"reason: {out.stderr[-300:]}"
            )
            # And the override has to REACH the command. Dropping
            # `$(CHECK_COVERAGE_DEBT_ARGS)` from the recipe leaves the forced-failure test
            # passing on any machine holding a report with findings, because the target
            # then exits non-zero for its own reasons -- a pass for the wrong reason, which
            # was measured.
            override = self.FORCED_FAILURE[target]
            name, _, value = override.partition("=")
            expanded = subprocess.run(
                ["make", "-n", "--no-print-directory", target, f"{name}={value}"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            assert expanded.returncode == 0, expanded.stderr[-300:]
            assert "a_tree_that_does_not_exist" in expanded.stdout, (
                f"`make {target} {override}` does not put that argument on the command "
                f"line, so the forced-failure test is not forcing anything and would pass "
                f"off whatever the target happened to do. Expanded: "
                f"{expanded.stdout.strip()[:400]!r}"
            )


#: The exact tracked source files each tree's baseline does not record, as measured.
#:
#: The **set**, not the count. A count cancels: adding one unrecorded module while recording
#: one of the existing ones leaves the number unchanged, and that was measured on `scripts` —
#: this repository's own gate layer — with a new unratcheted module and every check green. A
#: set makes the new path a named finding instead of an arithmetic coincidence, and it is no
#: harder to satisfy.
#:
#: Not required to be empty, because empty is not reachable and a check that cannot pass gets
#: deleted. Every path here has the same cause — **no suite imports it**, so no coverage
#: report mentions it, so there is no figure to record; `write_baseline` records what a report
#: contains intersected with what git tracks, which is why these are absent rather than at 0.
#: Three groups, and the reason is per group rather than one sentence over all three:
#:
#: * `scripts` — standalone operator scripts (`model_finetuning/`, `examples/`,
#:   `security/live_checks/`, the PyPI name-placeholder packages) that no test imports.
#: * `main_stack_extensions` — seven copies of `log_sanitizer.py`, one vendored into each
#:   Lambda directory.
#: * `pii_anonymizer_hook` — the vendored `vendor/pii_anonymizer/` tree, whose scope decision
#:   is already recorded against `ruff.toml`'s `extend-exclude`.
#:
#: Six trees are **empty**, and that is the half that does the work: a new module in any of
#: them is a finding here the moment it is committed, with nothing measured.
UNRECORDED_TRACKED_FILES: dict[str, list[str]] = {
    "idp_common": [],
    "scripts": [
        "scripts/examples/dynamodb_service_example.py",
        "scripts/model_finetuning/create_finetuning_job.py",
        "scripts/model_finetuning/create_provisioned_throughput.py",
        "scripts/model_finetuning/inference_example.py",
        "scripts/model_finetuning/prepare_nova_finetuning_data.py",
        "scripts/pypi-placeholders/idp-accelerator-cli/idp_accelerator_cli/__init__.py",
        "scripts/pypi-placeholders/idp-feature-sdk/idp_feature_sdk/__init__.py",
        "scripts/pypi-placeholders/idp-mcp-connector/idp_mcp_connector/__init__.py",
        "scripts/sdlc/generate_api_validation_spec.py",
        "scripts/sdlc/validate_buildspec.py",
        "scripts/security/live_checks/cognito_groups.py",
        "scripts/security/live_checks/oidc_provider/deploy.py",
        "scripts/security/live_checks/verify_execution_scope.py",
        "scripts/security/live_checks/verify_federated_signin.py",
        "scripts/security/live_checks/verify_idp_group_mapping.py",
        "scripts/srt/fix.py",
        "scripts/srt/prune_stale_scans.py",
    ],
    "idp_sdk": [],
    "main_stack_extensions": [
        "lambdas/get_feature_launch_url/log_sanitizer.py",
        "lambdas/list_catalog_features/log_sanitizer.py",
        "lambdas/list_installed_features/log_sanitizer.py",
        "lambdas/register_feature/log_sanitizer.py",
        "lambdas/register_feature_hooks/log_sanitizer.py",
        "lambdas/subscribe_feature/log_sanitizer.py",
        "lambdas/unsubscribe_feature/log_sanitizer.py",
    ],
    "idp_cli": [],
    "idp_feature_sdk": [],
    "seller_entitlement": [],
    "pii_anonymizer_hook": [
        "vendor/pii_anonymizer/__init__.py",
        "vendor/pii_anonymizer/core/__init__.py",
        "vendor/pii_anonymizer/core/pii_detector.py",
        "vendor/pii_anonymizer/core/prompts.py",
        "vendor/pii_anonymizer/core/synthetic_pii_generator.py",
        "vendor/pii_anonymizer/core/text_replacer.py",
        "vendor/pii_anonymizer/core/value_categorizer.py",
        "vendor/pii_anonymizer/helpers/__init__.py",
        "vendor/pii_anonymizer/helpers/config_loader.py",
        "vendor/pii_anonymizer/helpers/font_config.py",
        "vendor/pii_anonymizer/helpers/model_config_helper.py",
        "vendor/pii_anonymizer/helpers/model_router.py",
        "vendor/pii_anonymizer/helpers/page_type_checker.py",
        "vendor/pii_anonymizer/helpers/pdf_processor.py",
        "vendor/pii_anonymizer/helpers/text_chunker.py",
        "vendor/pii_anonymizer/helpers/textract_helper.py",
        "vendor/pii_anonymizer/helpers/threaded_detector.py",
        "vendor/pii_anonymizer/helpers/token_tracker.py",
        "vendor/pii_anonymizer/processors/__init__.py",
        "vendor/pii_anonymizer/processors/image_processor.py",
        "vendor/pii_anonymizer/processors/pdf_image_processor.py",
        "vendor/pii_anonymizer/processors/pdf_text_processor.py",
        "vendor/pii_anonymizer/processors/tabular_processor.py",
        "vendor/pii_anonymizer/processors/txt_processor.py",
        "vendor/pii_anonymizer/processors/word_processor.py",
        "vendor/pii_anonymizer/redaction/__init__.py",
        "vendor/pii_anonymizer/redaction/pdf_redactor.py",
        "vendor/pii_anonymizer/validation/__init__.py",
        "vendor/pii_anonymizer/validation/document_validator.py",
        "vendor/pii_anonymizer/validation/model_schemas.py",
        "vendor/pii_anonymizer/validation/pdf_validator.py",
    ],
    "pii_anonymizer_api": [],
}


@pytest.mark.unit
class TestTheBaselineAccountsForEveryTrackedSourceFile:
    """The closure in the direction the gate itself cannot check offline.

    `check_tree` reports a file that is **measured and unrecorded**, which is the right
    runtime check and needs a coverage report to fire. Before both CIs produced one per tree,
    seven trees never had a report in CI, so for those the check never ran anywhere: two
    `parameters.py` files reached the integration branch measured-and-unratcheted, and were
    found only by measuring nine trees by hand. Wiring CI to measure them is the instance
    fix. This is the class fix, and it needs no measurement at all — it compares
    `git ls-files` against the committed baseline, so it fires at commit time on the pull
    request that adds the module.

    It is a **count** rather than a closure because the closure is not satisfiable today;
    see :data:`UNRECORDED_TRACKED_FILES` for the residual and why each part of it exists.
    """

    def test_no_tree_gains_an_unrecorded_tracked_source_file(self):
        baseline = json.loads(ccd.BASELINE.read_text(encoding="utf-8"))["trees"]
        assert len(ccd.TREES) >= 9, (
            "registry looks truncated; this would be near-vacuous"
        )
        appeared, resolved = {}, {}
        for tree in ccd.TREES:
            tracked = set(ccd.tracked_source_files(tree))
            assert tracked, (
                f"no tracked source files found for {tree.name}, so this check would pass "
                f"vacuously for it — an empty derived set is a skip, not a failure"
            )
            recorded = set(baseline.get(tree.name, {}).get("files", {}))
            gap = {
                path
                for path in tracked - recorded
                if not any(path.startswith(k) for k in ccd.NOT_MEASURED)
            }
            expected = UNRECORDED_TRACKED_FILES.get(tree.name)
            assert expected is not None, (
                f"tree `{tree.name}` is in TREES and not in UNRECORDED_TRACKED_FILES, so "
                f"nothing records which of its files are outside the baseline. Add it with "
                f"what it measures — an empty list if its baseline is complete, which is "
                f"the answer for six of the nine."
            )
            if new := sorted(gap - set(expected)):
                appeared[tree.name] = new
            if gone := sorted(set(expected) - gap):
                resolved[tree.name] = gone
        assert not appeared, (
            "these tracked source files are outside their tree's baseline and were not "
            "before, so their coverage is ratcheted by nothing — and no coverage run was "
            "needed to see it:\n"
            + "\n".join(
                f"  {name}: {', '.join(paths)}" for name, paths in appeared.items()
            )
            + "\nRecord them: measure that tree and run `check_coverage_debt.py --write`. If "
            "a file genuinely cannot be measured, add it to NOT_MEASURED with the reason, "
            "which is the only sanctioned way for a file to be out of scope. Do not add it "
            "here to make this pass — this list is the residual, not a suppression list."
        )
        assert not resolved, (
            "these files are now recorded, which is progress that has to be banked or the "
            "list pre-approves losing it again:\n"
            + "\n".join(
                f"  {name}: {', '.join(paths)}" for name, paths in resolved.items()
            )
            + "\nRemove them from UNRECORDED_TRACKED_FILES."
        )


#: `check_coverage_debt.NOT_MEASURED`'s members, pinned.
#:
#: Its own reason says the list is "deliberately short, and meant to stay short", and until
#: now nothing evaluated that. It became load-bearing for a second gate when
#: :data:`UNRECORDED_TRACKED_FILES` started deriving its universe as tracked minus recorded
#: minus `NOT_MEASURED`: from that point a one-line entry here makes a brand-new tracked
#: source file invisible to the closure check as well as to the runtime one. Measured: a new
#: file plus a new entry passed both suites with 1328 tests green.
#:
#: Non-vacuity could not see that, because the pre-existing member satisfies it forever
#: whatever is added beside it. The set is pinned instead, which is the registry's own idiom.
NOT_MEASURED_MEMBERS = {"idp_common/agents/analytics/assets"}


@pytest.mark.unit
def test_the_not_measured_list_has_not_grown():
    """A new prefix here is the documented way past the closure check, so it is a finding.

    Not a prohibition — the list exists for a real case and may legitimately gain a member.
    What it must not do is gain one silently, because a prefix added here takes effect on two
    gates at once the moment it is written.
    """
    assert NOT_MEASURED_MEMBERS, "the pin is empty, so this check would pass vacuously"
    actual = set(ccd.NOT_MEASURED)
    added = sorted(actual - NOT_MEASURED_MEMBERS)
    removed = sorted(NOT_MEASURED_MEMBERS - actual)
    assert not added, (
        "these prefixes were added to NOT_MEASURED: "
        + ", ".join(added)
        + ". That exempts everything under them from BOTH the coverage ratchet's "
        "universe-closure check and the offline closure in this file, so a brand-new "
        "untested module under one of them is ratcheted by nothing. The honest way to have "
        "a module at 0% is to RECORD it at 0%. If the exemption is right, say why in "
        "scripts/tests/gate_exemptions.json and add it to NOT_MEASURED_MEMBERS."
    )
    assert not removed, (
        "these prefixes are gone from NOT_MEASURED: "
        + ", ".join(removed)
        + ". Good, if their files are now recorded — remove them from "
        "NOT_MEASURED_MEMBERS so the pin cannot pre-approve re-adding them."
    )
