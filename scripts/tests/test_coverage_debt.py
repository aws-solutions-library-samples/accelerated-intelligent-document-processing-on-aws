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
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

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
        `idp_sdk`/`idp_cli` because at 36% and 27% they are the two the ratchet most
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
        code, problems, unchecked = ccd.check()
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
        code, problems, skipped = ccd.check()
        assert code == 1 and skipped == []
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
        code, problems, _ = ccd.check()
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
        code, problems, _ = ccd.check()
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


#: A run record captured from a **real** xdist collection mismatch, verbatim apart from the
#: `hostname` and `timestamp` attributes, which are replaced because a machine name and a
#: wall clock belong to the machine that ran it and not to this repository.
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
class TestCIStatesThePrecondition:
    """Both CI configurations must name the tree their test step measures.

    The gate runs as a separate step from the run that writes its input in both, and
    nothing asserted that the input existed. Asserted per config file rather than once,
    because the failure mode this repository keeps hitting is a gate present in one CI and
    absent from the other.
    """

    @pytest.mark.parametrize(
        "config", [".gitlab-ci.yml", ".github/workflows/developer-tests.yml"]
    )
    def test_the_ratchet_invocation_requires_the_tree_that_step_measures(self, config):
        text = (REPO_ROOT / config).read_text(encoding="utf-8")
        line = next(
            (
                ln
                for ln in text.splitlines()
                if "make check-coverage-debt" in ln and not ln.lstrip().startswith("#")
            ),
            None,
        )
        assert line, f"{config} no longer invokes the coverage ratchet at all"
        assert "--require-tree=idp_common" in line, (
            f"{config} invokes the ratchet without stating which tree must have been "
            f"measured: {line.strip()!r}. `make test-cicd -C lib/idp_common_pkg` above it "
            f"measures idp_common, and with that unstated the step passes on a run where "
            f"the report never appeared."
        )

    def test_the_makefile_target_passes_the_argument_through(self):
        """Both CI lines set `CHECK_COVERAGE_DEBT_ARGS`, so the recipe has to forward it.
        A recipe that ignored it would leave two green CI steps asserting nothing."""
        recipe = next(
            block
            for block in (REPO_ROOT / "Makefile").read_text().split("\ncheck-")
            if block.startswith("coverage-debt:")
        )
        assert "$(CHECK_COVERAGE_DEBT_ARGS)" in recipe.split("\n\n")[0], recipe[:400]
