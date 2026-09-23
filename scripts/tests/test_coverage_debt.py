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
