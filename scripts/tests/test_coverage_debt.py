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

Every behavioural test drives the real functions against a synthetic report written to
`tmp_path`, rather than asserting how the code is written.
"""

from __future__ import annotations

import importlib.util
import json
import sys
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


def _write_tree_report(tree, rates: dict[str, float], total: float = 80.0) -> Path:
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
    return report


@pytest.mark.unit
class TestCheckOrchestration:
    """`check()` across trees. Every tree is checked, and an unchecked one is named.

    The per-tree comparison is covered above; what is asserted here is the part that
    decides the exit code and what the operator is told. A gate that passes while
    measuring nothing is the specific failure this whole ratchet exists to prevent, so
    "a tree with no report" must not read as "a tree that is fine".
    """

    def test_a_tree_with_no_report_is_skipped_and_named_not_silently_passed(
        self, monkeypatch, tmp_path
    ):
        tree = _fake_tree(tmp_path)
        _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        code, problems, skipped = ccd.check()
        assert (code, problems, skipped) == (0, [], [tree.name]), (
            "an unmeasured tree must be reported as unchecked; returning it as a pass "
            "is how a gate reads green while checking nothing"
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
