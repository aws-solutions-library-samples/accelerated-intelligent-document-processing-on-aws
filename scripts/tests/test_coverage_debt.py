# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the coverage ratchet.

The ratchet spans every tree in `check_coverage_debt.TREES` and has two halves — an
aggregate floor in `lib/idp_common_pkg/Makefile` and a
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

import ast
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import gate_premises
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


#: The per-file coverage #1221 achieved on two Lambda handler directories, by path.
#:
#: Keyed by **repo-relative path**, not by tree name, and that is the whole reason this
#: pin is worth having rather than being a second copy of the baseline. What has to be
#: held is the coverage of these four files. *Which* `Tree` records them is an
#: implementation detail of the registry, and #1227's decided remedy — a derived fan-out
#: over the handler directories — will change it. A pin written against today's two
#: registry entries would have to be rewritten by that change; one written against the
#: paths outlives it, because it asks only that *some* registered tree ratchet the file.
#:
#: Why pin at all when the ratchet already compares these files: the ratchet's floor
#: lives in `scripts/coverage_debt.json`, and `--write` re-records every measured tree
#: from whatever the current run reports. So the baseline alone cannot tell a deliberate
#: lowering from an accidental one — following the gate's own printed remedy is enough to
#: move ten floors at once, which is what #1421 is about. A figure pinned here cannot
#: move without an edit that says it is moving.
#:
#: Measured by `scripts/coverage_all.py` on 2026-10-14 with no tests skipped: 118
#: collected for `complete_section_review` and 383 for `calculate_capacity`, identical at
#: `-n 16` and serial. The directory figures #1221 reports as 99% are the whole-report
#: aggregates (99.66% and 98.69%), which include each directory's own test modules; these
#: are the source files alone, which is what the baseline records.
HANDLER_COVERAGE_FLOORS_FROM_1221: dict[str, float] = {
    "src/lambda/complete_section_review/index.py": 100.0,
    "src/lambda/calculate_capacity/index.py": 96.9,
    "src/lambda/calculate_capacity/log_sanitizer.py": 100.0,
    "src/lambda/calculate_capacity/validation.py": 100.0,
}

#: The directories those floors cover — derived from the paths, never listed twice.
#:
#: Not named `PINNED_…`. That prefix is a fragment in the name vocabulary the collector
#: under `scripts/tests/` uses to find constants that switch a gate off for something,
#: so the registry meta-test asks for an authored judgement about any constant spelled
#: that way. This one switches nothing off — it is the universe the two assertions below
#: are enforced over — and the documented remedy when a name matches by accident is to
#: rename, so the name is what changed.
HANDLER_DIRECTORIES_WITH_FLOORS: tuple[str, ...] = tuple(
    sorted({str(Path(p).parent) for p in HANDLER_COVERAGE_FLOORS_FROM_1221})
)


def _tracked_sources_under(directory: str) -> list[str]:
    """Tracked, non-test `.py` under ``directory``, repo-relative.

    Filtered through the gate's own :func:`check_coverage_debt._looks_like_test` rather
    than a second rule, because a closure assertion whose universe is derived differently
    from the gate's reports a mismatch that is an artefact of the two rules disagreeing.
    """
    out = subprocess.run(
        ["git", "ls-files", f"{directory}/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(p for p in out if not ccd._looks_like_test(p))


@pytest.mark.unit
class TestTheHandlerDirectoriesWithCoverageToLose:
    """#1221's coverage work on two Lambda handler directories, held by path.

    #1221 reports taking `src/lambda/complete_section_review` from 47% to 99% and
    `src/lambda/calculate_capacity` from 31% to 99%, and for a while nothing held either
    figure: no handler directory was in the coverage registry, so a later change could
    take both back down with every gate green (#1227). Both are registered now, and this
    class is the half that survives the registry being reshaped.

    It asserts a property of the **committed baseline**, so it needs no coverage report
    and no measurement — which is what makes it host-insensitive where the ratchet is
    not. A machine that skips tests measures a lower figure and the ratchet says so; this
    pin instead fails only when the recorded floor itself moves down.
    """

    def _recorded_by_path(self) -> dict[str, float]:
        """Every file the committed baseline records, re-keyed repo-relative.

        Baseline keys are relative to their own tree's root, so `index.py` means two
        different files in two different trees. Resolving each through
        :func:`check_coverage_debt.tree_root` is what makes a path-keyed assertion
        possible at all, and it is also what will keep working when those two entries are
        replaced by one fan-out tree.
        """
        # Read through the gate's own constant rather than a second copy of the path, so
        # the register this asserts about is by construction the one the gate checks.
        baseline = json.loads(ccd.BASELINE.read_text(encoding="utf-8"))
        recorded: dict[str, float] = {}
        for tree in ccd.TREES:
            root = ccd.tree_root(tree)
            files = baseline.get("trees", {}).get(tree.name, {}).get("files", {})
            for key, rate in files.items():
                resolved = (root / key).resolve()
                try:
                    recorded[str(resolved.relative_to(REPO_ROOT))] = rate
                except ValueError:  # pragma: no cover - a tree rooted outside the repo
                    continue
        return recorded

    def test_the_pin_set_is_not_empty(self):
        """Non-vacuity, and it is not theoretical.

        Every other assertion here is parametrised over
        ``HANDLER_COVERAGE_FLOORS_FROM_1221``, and pytest reports a parametrisation over
        an empty collection as a skip rather than a failure — so emptying the dict would
        turn this whole class green while protecting nothing.
        """
        assert len(HANDLER_COVERAGE_FLOORS_FROM_1221) >= 4, (
            f"only {len(HANDLER_COVERAGE_FLOORS_FROM_1221)} file(s) pinned; four were "
            f"pinned when #1221's work was protected, so a smaller number means pins "
            f"were dropped rather than that the directories shrank"
        )
        assert len(HANDLER_DIRECTORIES_WITH_FLOORS) == 2, (
            HANDLER_DIRECTORIES_WITH_FLOORS
        )

    @pytest.mark.parametrize("path", sorted(HANDLER_COVERAGE_FLOORS_FROM_1221))
    def test_the_file_is_ratcheted_at_or_above_its_pinned_floor(self, path):
        floor = HANDLER_COVERAGE_FLOORS_FROM_1221[path]
        recorded = self._recorded_by_path()
        assert recorded, (
            "the committed baseline resolved to no files at all, so this assertion "
            "would otherwise be comparing against an empty map"
        )
        assert path in recorded, (
            f"{path} is not recorded by any tree in check_coverage_debt.TREES, so "
            f"nothing holds its coverage. #1221 took it to {floor:.2f}% and #1227 is "
            f"about exactly this gap: measure its directory with `python3 "
            f"scripts/coverage_all.py --only <tree>` and record it with "
            f"`python3 scripts/check_coverage_debt.py --write-missing`, which adds only "
            f"absent entries and cannot move an existing floor."
        )
        assert recorded[path] >= floor, (
            f"{path} is recorded at {recorded[path]:.2f}% but #1221 took it to "
            f"{floor:.2f}%. A recorded floor only moves down when someone runs a write "
            f"flag, so this is a lowering rather than a measurement: either the work was "
            f"undone, or `--write` re-recorded the tree from a run that executed fewer "
            f"tests. Re-measure before changing the number here."
        )

    @pytest.mark.parametrize("path", sorted(HANDLER_COVERAGE_FLOORS_FROM_1221))
    def test_the_pinned_path_is_still_tracked(self, path):
        """Staleness: a renamed file must fail here rather than leave the pin inert.

        A pin naming a path git no longer tracks shields nothing and pre-approves
        whatever next occupies the name, which is the failure mode the exemption registry
        documents for suppression entries. The ratchet catches the same rename from the
        other side — a recorded path the report no longer mentions — but only while a
        report exists, and this assertion needs none.
        """
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert tracked.returncode == 0, (
            f"{path} is pinned at {HANDLER_COVERAGE_FLOORS_FROM_1221[path]:.2f}% but git "
            f"does not track it. If it moved, move the pin; if it was deleted, delete "
            f"the pin — leaving it names a file that does not exist."
        )

    @pytest.mark.parametrize("directory", HANDLER_DIRECTORIES_WITH_FLOORS)
    def test_every_source_file_in_the_directory_is_pinned(self, directory):
        """Universe closure over the two directories, which is what makes the pin hold.

        A pin over four named files protects those four. A fifth module added beside them
        would be ratcheted by the baseline at whatever it first measures and pinned by
        nothing — so adding a source file here has to fail until its floor is written
        down, the same way a new tree fails `test_every_tree_is_recorded`.
        """
        found = _tracked_sources_under(directory)
        assert found, (
            f"git tracks no non-test .py under {directory}, so this assertion examined "
            f"nothing. Either the directory moved or the filter is wrong."
        )
        pinned = sorted(
            p
            for p in HANDLER_COVERAGE_FLOORS_FROM_1221
            if p.startswith(f"{directory}/")
        )
        assert found == pinned, (
            f"{directory} holds {len(found)} source file(s) and "
            f"{len(pinned)} are pinned. Unpinned: "
            f"{sorted(set(found) - set(pinned))}; pinned but absent: "
            f"{sorted(set(pinned) - set(found))}. Measure the directory and add each "
            f"file's floor to HANDLER_COVERAGE_FLOORS_FROM_1221."
        )


#: The Lambda handler directories the coverage baseline deliberately does not record.
#:
#: **Membership is derived, which is the requirement #1227 settles on**: a handler
#: directory holding tracked code and no test file. Nothing is listed, so the first test
#: written for one of these takes it out of this set and into the fan-out's measured
#: roots, and a handler added with no test joins this set — in both directions without
#: an edit here. The alternative, registering handlers one at a time, is a list a new
#: handler joins only when somebody notices, which is the gap the issue is about.
#:
#: **What it turns off, for one member.** The coverage ratchet's per-file baseline. A
#: file recorded at some percentage is held there; a file absent from the baseline is
#: ratcheted at nothing, and every source file in one of these directories is absent.
#:
#: **The premise is computed, not asserted.** ``gate_premises.collects_zero_tests`` is
#: run against every member by
#: :func:`TestTheHandlerDirectoryUniverse.test_the_exempt_directory_really_collects_no_test`
#: — a directory nothing collects has no suite to measure its coverage with, which is a
#: property of that one directory rather than of the set. Derivation and premise are
#: deliberately **different instruments**: the derivation asks git whether a test file is
#: present, the predicate asks pytest whether anything collects. A directory holding a
#: file named ``*_test.py`` would be untested to the first and collected by the second —
#: pytest's default ``python_files`` matches that spelling and
#: :func:`check_coverage_debt._looks_like_test` does not — and the disagreement then
#: fails here loudly rather than quietly exempting a directory that has a suite.
#:
#: Two ratchets, both asked for when option B was chosen. ``EXEMPT_HANDLER_DIRECTORY_COUNT``
#: pins the size, so a 46th arrival fails rather than being absorbed into an unmeasured
#: set; and :func:`TestTheHandlerDirectoryUniverse.test_every_handler_directory_is_accounted_for`
#: closes the universe, so a directory in neither this set nor the baseline fails.
HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE: frozenset[str] = frozenset(
    directory.path
    for directory in ccd.handler_directories().values()
    if not directory.tests
)

#: How many handler directories had no test when option B was implemented.
#:
#: Compared for **equality**, not as an upper bound, and the direction that matters is
#: the one the maintainer asked for: a bound of ``<=`` lets the number fall as tests are
#: written and then silently re-absorb that many new unmeasured handlers. Equality makes
#: both directions a one-line edit a reviewer sees — downwards when a suite is written
#: (which is the whole point of the number falling), upwards only with an explanation.
#:
#: 45, derived on 2026-10-15 from 84 handler directories holding tracked code: 38 with a
#: suite the fan-out measures, 45 with none, and one (``src/lambda/ocr_benchmark_deployer``)
#: that `scripts/run_all_tests.py` quarantines. The attribution rule that produces 84 is
#: written out in :func:`check_coverage_debt.handler_directories`; a different rule gives
#: a different number, which is why #1227 carries three.
EXEMPT_HANDLER_DIRECTORY_COUNT = 45

#: Source files inside a MEASURED handler directory that have no coverage floor,
#: because coverage.py's file discovery cannot reach them. Keyed by path, with the reason.
#:
#: **Keyed by path rather than counted**, which is the difference between an exemption and
#: a budget. A count pin passes when the audited file is replaced by a different one:
#: measured, by deleting this entry's file and planting an unreachable module under
#: another root, which a cardinality pin accepted while a different source file shipped
#: unmeasured. The registry's rule is one entry, one member's worth of reason, and this is
#: the shape that follows it.
#:
#: The premise is computed per member by
#: `gate_premises.not_reachable_by_a_package_walk`, bound to the fan-out root the file
#: sits under: coverage.py walks the measured source and recurses into a subdirectory only
#: if it is an importable package, so a module below a non-package directory reaches the
#: data only when something imports it and can be given no floor at all. The gate supplies
#: the other half the predicate deliberately does not claim -- that nothing imports this
#: one -- by deriving membership from the files that have no recorded floor.
COVERAGE_UNREACHABLE_FILES_EXEMPT: dict[str, str] = {
    "src/lambda/chat_stream_processor/vendored/chat_with_document_processor.py": (
        "A vendored copy whose original is `src/lambda/chat_with_document_processor/"
        "index.py`, held in step with it by `src/lambda/chat_stream_processor/tests/"
        "test_vendored_in_sync.py`, and imported by no test of its own. It sits in "
        "`vendored/`, which is not a package, so coverage.py's walk over "
        "`--cov=chat_stream_processor` never recurses to it -- measured both ways: "
        "dropping an `__init__.py` into that directory puts the file in the report at "
        "0.00% and moves the directory's figure from 58% to 43%. The remedy is therefore "
        "in the tree rather than here: make `vendored/` a package, or have a test import "
        "the module."
    ),
}


def _handler_directories_with_recorded_coverage() -> frozenset[str]:
    """Handler directories with at least one source file in the committed baseline.

    The **enforced** half of the universe, and it is read from the baseline rather than
    from :func:`check_coverage_debt.fanout_roots` on purpose. Deriving both halves from
    one partition would make the closure assertion below tautological: it would compare
    a set against itself and pass whatever the baseline contained. What has to be true is
    that a directory with a suite actually has a recorded floor — which is the thing
    #1227 is about, since #1221 took two directories to 99% with nothing holding either.
    """
    baseline = json.loads(ccd.BASELINE.read_text(encoding="utf-8"))
    directories = set(ccd.handler_directories())
    found: set[str] = set()
    for tree in ccd.TREES:
        root = ccd.tree_root(tree)
        for key in baseline.get("trees", {}).get(tree.name, {}).get("files", {}):
            try:
                rel = (root / key).resolve().relative_to(REPO_ROOT)
            except ValueError:  # pragma: no cover - a tree rooted outside the repo
                continue
            parent = str(rel.parent)
            if parent in directories:
                found.add(parent)
    return frozenset(found)


@pytest.mark.unit
class TestTheHandlerDirectoryUniverse:
    """#1227's fan-out: every handler directory is measured, exempt, or fails here.

    The defect this closes is not the untested directories — it is that the **tested**
    ones were ratcheted by nothing. No handler directory was in
    :data:`check_coverage_debt.TREES` at all, so #1221's work on
    ``complete_section_review`` and ``calculate_capacity`` could be undone with every
    gate green. Two individual entries were a holding action; this class is what makes
    the arrangement survive a handler being added.
    """

    def test_the_exempt_set_is_computed_and_not_a_list_of_names(self):
        """ "Derived, not listed" is the first requirement, and nothing else here tests it.

        Replacing the comprehension below with a ``frozenset`` of today's 45 path
        literals passes every other assertion in this class: the count still matches, the
        premise still holds for each, and the universe is still closed. It would simply
        stop tracking the tree — which is the whole defect, and the state the repository's
        other exemption lists were in.

        So this reads the **declaration**, by parsing it. A substring search for
        ``handler_directories`` would be satisfied by the docstring above it, which is
        the same failure the registry meta-test's own predicate check ran into.

        ⚠️ **What it covers is the declaration, which is narrower than "derived".** A
        review defeated it in two lines by keeping the real derivation and intersecting
        it with a sibling module-level tuple of today's 45 paths: no string literal is in
        the declaration, ``handler_directories`` is still called, and the set is frozen.
        That mutation is caught — but by
        :func:`test_every_handler_directory_is_accounted_for`, which failed naming the
        planted directory, not by this. So read this as refusing the *obvious* way to
        re-list the set, with the closure assertion as the thing that actually holds the
        property. Chasing every expression that could narrow a comprehension would mean
        re-implementing taint analysis in a test.
        """
        module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        declarations = [
            node
            for node in module.body
            if isinstance(node, ast.AnnAssign | ast.Assign)
            for target in (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
            if isinstance(target, ast.Name)
            and target.id == "HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE"
        ]
        assert len(declarations) == 1, (
            f"{len(declarations)} module-level declarations of the exempt set; the "
            f"registry keys it by name, so exactly one has to be the authority"
        )
        value = declarations[0].value
        literals = [
            n.value
            for n in ast.walk(value)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        assert not literals, (
            f"the exempt set names {literals} as string literal(s), so its membership is "
            f"written down rather than derived from the tree. A listed exemption passes "
            f"every other check here on the day it is written and stops tracking the "
            f"repository immediately afterwards; deriving it is the requirement #1227 "
            f"settles on."
        )
        derivation = {
            n.func.attr
            if isinstance(n.func, ast.Attribute)
            else getattr(n.func, "id", "")
            for n in ast.walk(value)
            if isinstance(n, ast.Call)
        }
        assert "handler_directories" in derivation, (
            f"the exempt set is built without calling check_coverage_debt."
            f"handler_directories(); it calls {sorted(derivation)}. That function is the "
            f"single authority for what a handler directory is -- shared with the "
            f"fan-out and with #1442 -- and a second derivation beside it is a second "
            f"attribution rule, which is how two universes come to disagree."
        )

    def test_the_exempt_set_is_not_empty(self):
        """Non-vacuity, and it is not theoretical.

        The per-member premise check below is parametrised over this set, and pytest
        reports a parametrisation over an empty collection as a **skip** rather than a
        failure — so a derivation that silently found nothing would turn the premise
        check green while computing nothing.
        """
        assert HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE, (
            "the derivation found no untested handler directory at all, which means the "
            "attribution rule stopped matching rather than that every handler gained a "
            "suite — 45 had none when this was written. Check "
            "check_coverage_debt.handler_directories()."
        )

    def test_the_handler_universe_is_not_empty(self):
        """The same guard one level up, on the universe every assertion here derives from.

        Emptying ``handler_directories()`` would make the closure assertion compare two
        empty sets and pass.
        """
        assert len(ccd.handler_directories()) >= 80, (
            f"only {len(ccd.handler_directories())} handler directory(ies) discovered; "
            f"84 held tracked code when this was written, so a much smaller number means "
            f"the derivation broke rather than that the repository shrank"
        )

    def test_the_exempt_count_is_pinned(self):
        """Count pinning: a 46th untested handler fails rather than being absorbed.

        This is the ratchet the maintainer asked for by name when choosing option B over
        baselining everything at 0.00. Without it, a derived exemption grows silently —
        the derivation is what makes membership honest, and a pinned count is what makes
        growth a decision.
        """
        found = sorted(HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE)
        # The remedy differs by DIRECTION, so only the applicable one is printed. Both
        # were printed at first, and the two read as contradictory advice on the case
        # that actually occurs: a message telling the reader to lower the pin to 46 when
        # 46 is what it just refused.
        if len(found) > EXEMPT_HANDLER_DIRECTORY_COUNT:
            remedy = (
                f"{len(found) - EXEMPT_HANDLER_DIRECTORY_COUNT} handler directory(ies) "
                f"arrived with no suite. Write one — the directory then joins the "
                f"fan-out's measured roots automatically and leaves this set — or raise "
                f"the pin deliberately, naming the directory and why it ships untested. "
                f"The names are below; the new one is the one your change added, which "
                f"is why they are not listed anywhere to diff against: membership is "
                f"derived, and a committed list of 45 names is the thing this replaced."
            )
        else:
            remedy = (
                f"a suite was written for "
                f"{EXEMPT_HANDLER_DIRECTORY_COUNT - len(found)} directory(ies), which is "
                f"the point of this number falling. Lower the pin to {len(found)} in the "
                f"same change, and record the directory's coverage: `python3 "
                f"scripts/coverage_all.py --only <tree>` then `python3 "
                f"scripts/check_coverage_debt.py --write-missing`."
            )
        assert len(found) == EXEMPT_HANDLER_DIRECTORY_COUNT, (
            f"{len(found)} handler directory(ies) hold tracked code and no test file, "
            f"where {EXEMPT_HANDLER_DIRECTORY_COUNT} were audited when the coverage "
            f"fan-out landed. {remedy}\n  Current set: {found}"
        )

    @pytest.mark.parametrize(
        "directory", sorted(HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE)
    )
    def test_the_exempt_directory_really_collects_no_test(self, directory):
        """The premise, computed per member rather than stated once for the set.

        ``collects_zero_tests`` is the authority here because the exemption's whole
        claim is that there is no suite to measure the directory's coverage with. The
        identical sentence has already been wrong once in this repository — a directory
        excluded from every gate as collecting zero tests collects six, all passing — and
        this set has 45 members, which is exactly the size at which one justification
        read in aggregate passes while a member fails.
        """
        holds, explanation = gate_premises.collects_zero_tests(directory)
        assert holds, (
            f"{directory} is exempt from the coverage baseline on the premise that it "
            f"has no suite, and {explanation}. If it collects tests, it belongs in the "
            f"fan-out's measured roots: that happens automatically once git tracks a "
            f"file check_coverage_debt._looks_like_test() recognises, so the usual cause "
            f"is a test file named `*_test.py`, which pytest collects and that helper "
            f"does not call a test. If collection ERRORED, the premise to record is the "
            f"unavailable dependency, which is what scripts/run_all_tests.py's "
            f"QUARANTINE already records for the one directory in that state."
        )

    def test_every_handler_directory_is_accounted_for(self):
        """Universe closure: in the baseline, in the exemption, or quarantined — or fail.

        The assertion that makes the exemption trustworthy at all, and the one that would
        have caught #1227's own defect: a handler directory with a suite and no recorded
        floor is in **neither** set and fails here by name. A new handler is therefore
        unable to join an unmeasured set quietly, whichever of the three states it
        arrives in.

        The third bucket is consumed rather than authored. ``src/lambda/ocr_benchmark_deployer``
        holds a suite whose collection errors because ``huggingface_hub`` is not a test
        dependency, and `scripts/run_all_tests.py` already records that per directory
        with the reason. Writing a second list here would be a second place for the same
        judgement to go stale, so this reads that one; if the dependency ever becomes a
        test dependency the entry goes away and the directory has to be measured like any
        other.
        """
        universe = set(ccd.handler_directories())
        enforced = _handler_directories_with_recorded_coverage()
        exempt = HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE
        quarantined = universe & ccd.quarantined_test_roots()

        unaccounted = sorted(universe - enforced - exempt - quarantined)
        assert not unaccounted, (
            f"{len(unaccounted)} handler directory(ies) are in neither the coverage "
            f"baseline nor the derived exemption nor the test-root quarantine, so "
            f"nothing holds their coverage and nothing says why: {unaccounted}.\n"
            f"If the directory has a suite, measure its tree "
            f"(`python3 scripts/coverage_all.py --only "
            f"{', --only '.join(sorted(t.name for t in ccd.TREES if t.fanout))}`) and "
            f"record it with `python3 scripts/check_coverage_debt.py --write-missing`. "
            f"If its suite cannot run here, say so in scripts/run_all_tests.py's "
            f"QUARANTINE with the reason, per directory."
        )

        both = sorted(enforced & exempt)
        assert not both, (
            f"{both} are recorded in the coverage baseline AND derived as having no "
            f"test. The two cannot both be true: a baselined file was measured, so "
            f"something collected. The usual cause is a stale baseline entry for a "
            f"directory whose suite was deleted — re-record with --write, which is the "
            f"only mode that drops an entry."
        )

    def test_every_source_file_in_a_measured_root_has_a_floor_or_is_unreachable(self):
        """Closure over **files**, not only over directories — which is the stronger half.

        The directory closure above is satisfied by *one* recorded file per root, and
        `check_tree`'s own unaccounted check only reports a file the report **mentions**,
        which an unimported file never is. So a tracked source file inside a measured
        root could sit with no floor and nothing saying so, which is the thing the
        per-file baseline exists to prevent on the only tree it had not reached.

        One file is in that state, and it is **named rather than counted**:
        :data:`COVERAGE_UNREACHABLE_FILES_EXEMPT` is keyed by path and compared here as
        an exact set, so a *different* unreachable file arriving fails even if the
        audited one is removed in the same change — which a cardinality pin does not,
        measured. The premise is computed per member by
        :func:`gate_premises.not_reachable_by_a_package_walk`, and the entry is
        registered in `scripts/tests/gate_exemptions.json`.
        """
        baseline = json.loads(ccd.BASELINE.read_text(encoding="utf-8"))
        recorded: set[str] = set()
        for tree in ccd.TREES:
            root = ccd.tree_root(tree)
            for key in baseline.get("trees", {}).get(tree.name, {}).get("files", {}):
                try:
                    recorded.add(str((root / key).resolve().relative_to(REPO_ROOT)))
                except ValueError:  # pragma: no cover - a tree rooted outside the repo
                    continue

        universe = ccd.handler_directories()
        roots = [
            root for tree in ccd.TREES if tree.fanout for root in ccd.fanout_roots(tree)
        ]
        assert roots, "fanout_roots() derived no root, so this examines nothing"

        unfloored = sorted(
            rel
            for root in roots
            for rel in universe[root].sources
            if rel not in recorded
        )
        exempt = sorted(COVERAGE_UNREACHABLE_FILES_EXEMPT)
        assert unfloored == exempt, (
            f"the source files in a MEASURED handler directory with no recorded floor "
            f"are {unfloored}, where {exempt} is what was audited.\n"
            f"  Unaccounted ({sorted(set(unfloored) - set(exempt))}): measure the tree "
            f"and run `python3 scripts/check_coverage_debt.py --write-missing`. If "
            f"coverage cannot reach the file at all, add it to "
            f"COVERAGE_UNREACHABLE_FILES_EXEMPT with its reason and register the "
            f"entry.\n"
            f"  Stale ({sorted(set(exempt) - set(unfloored))}): the file now has a floor "
            f"or is gone, so the entry shields nothing and pre-exempts whatever next "
            f"occupies the path -- delete it."
        )

    @pytest.mark.parametrize("path", sorted(COVERAGE_UNREACHABLE_FILES_EXEMPT))
    def test_an_unreachable_exempt_file_really_is_unreachable(self, path):
        """The premise, computed per member against the root it is measured under.

        Bound to that root rather than asked about the file's parent alone, because
        coverage's walk *does* reach a file sitting directly in the measured source
        whether or not that directory is a package -- only recursion needs one. A version
        of this asking about the parent alone answers "unreachable" for every handler
        module in the repository, since no handler directory is a package.
        """
        roots = [
            root
            for tree in ccd.TREES
            if tree.fanout
            for root in ccd.fanout_roots(tree)
            if path.startswith(f"{root}/")
        ]
        assert len(roots) == 1, (
            f"{path} is exempt from the coverage baseline as unreachable, but it is "
            f"under {len(roots)} measured fan-out root(s) ({roots}). An exemption for a "
            f"file no fan-out measures is an exemption from nothing."
        )
        holds, explanation = gate_premises.not_reachable_by_a_package_walk(
            path, roots[0]
        )
        assert holds, (
            f"{path} is exempt on the premise that coverage's file discovery cannot "
            f"reach it from {roots[0]}, and {explanation}. If a package walk does reach "
            f"it, the file is measurable and belongs in the baseline: measure the tree "
            f"and run `python3 scripts/check_coverage_debt.py --write-missing`."
        )
        assert COVERAGE_UNREACHABLE_FILES_EXEMPT[path].strip(), (
            f"{path} carries no reason. A path with no reason is a path nobody decided "
            f"about."
        )

    def test_every_fanout_root_has_a_recorded_floor(self):
        """The enforced half, stated as its own failure rather than folded into closure.

        Closure above fails if a root is in no bucket; this fails with the *measured*
        reason, which is the one a reader needs: the fan-out ran this directory and the
        baseline records none of its files, so the ratchet compares nothing for it.
        """
        roots = {
            root for tree in ccd.TREES if tree.fanout for root in ccd.fanout_roots(tree)
        }
        assert roots, "fanout_roots() derived no root for any fan-out tree"
        unrecorded = sorted(roots - _handler_directories_with_recorded_coverage())
        assert not unrecorded, (
            f"{len(unrecorded)} fan-out root(s) have a suite and no file in the coverage "
            f"baseline, so their coverage is unratcheted — which is precisely #1227: "
            f"{unrecorded}. Measure the tree and run "
            f"`python3 scripts/check_coverage_debt.py --write-missing`."
        )

    def test_every_handler_prefix_is_covered_by_a_fanout_tree(self):
        """A third ``src/lambda`` prefix must fail rather than become invisible.

        The prefixes are derived from the tree, and the registry names two of them. A
        nested stack introducing a third would otherwise put every handler under it
        outside the fan-out, outside the exemption's own derivation — which is scoped to
        a prefix — and outside every figure, with nothing objecting.
        """
        prefixes = set(ccd.handler_prefixes())
        assert prefixes, "no handler prefix discovered at all"
        covered = {t.cwd.rstrip("/") for t in ccd.TREES if t.fanout}
        missing = sorted(prefixes - covered)
        assert not missing, (
            f"these handler prefixes are covered by no fan-out tree, so every handler "
            f"directory under them is measured by nothing: {missing}. Add a "
            f"`Tree(<name>, <prefix>, '.', fanout=True)` to check_coverage_debt.TREES "
            f"and record its baseline."
        )
        stray = sorted(covered - prefixes)
        assert not stray, (
            f"these fan-out trees name a prefix that holds no handler directory: "
            f"{stray}. A fan-out over nothing measures nothing while reading as a "
            f"registered tree."
        )

    def test_the_fanout_roots_are_exactly_the_tested_unquarantined_directories(self):
        """The derivation, checked against the universe it is taken from.

        Not a restatement of :func:`check_coverage_debt.fanout_roots`: it compares the
        roots the runner will actually invoke against the three-way split of the
        universe, so a derivation narrowed in either direction — a prefix filter that
        stops matching, a quarantine read that returns everything — fails here rather
        than silently measuring fewer directories than the closure believes.
        """
        universe = ccd.handler_directories()
        quarantined = set(universe) & ccd.quarantined_test_roots()
        expected = sorted(
            path
            for path, directory in universe.items()
            if directory.tests and path not in quarantined
        )
        roots = sorted(
            root for tree in ccd.TREES if tree.fanout for root in ccd.fanout_roots(tree)
        )
        assert roots == expected, (
            f"the fan-out will invoke {len(roots)} root(s) where {len(expected)} handler "
            f"directories hold a runnable suite. Not invoked: "
            f"{sorted(set(expected) - set(roots))}; invoked but not expected: "
            f"{sorted(set(roots) - set(expected))}."
        )

    def test_a_planted_handler_directory_reaches_the_universe_once_it_is_staged(
        self, tmp_path, monkeypatch
    ):
        """The declared bound on the derivation, measured in both states.

        A gate whose verdict changes at ``git add`` time is a real cost and the kind this
        repository has been bitten by, so it is pinned rather than left to be discovered:
        a planted handler is invisible while untracked and in the universe once staged.
        The window is one ``git add`` wide, and the reason it is not closed with
        ``--others`` is that :func:`check_coverage_debt.tracked_source_files` — where the
        baseline's universe comes from — cannot see an untracked file either, and
        ``write_baseline`` cannot record one. A derivation that saw them would admit a
        fan-out root nothing could ever record.

        Driven against a throwaway checkout, so the real tree is untouched.
        """
        handler = tmp_path / "src" / "lambda" / "planted"
        handler.mkdir(parents=True)
        (handler / "index.py").write_text("X = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
        )
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)

        assert "src/lambda/planted" not in ccd.handler_directories(), (
            "an untracked handler is in the universe, so the derivation has started "
            "reading the working tree rather than the index — which also means a "
            "fan-out root can appear that write_baseline is unable to record"
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True
        )
        assert "src/lambda/planted" in ccd.handler_directories(), (
            "a STAGED handler is still invisible, so a new untested handler would reach "
            "a branch without the count pin ever firing — the window would then be a "
            "whole commit wide rather than one `git add`"
        )

    def test_a_source_file_directly_under_the_prefix_is_not_a_directory(
        self, tmp_path, monkeypatch
    ):
        """The attribution rule that decides the count, which the real tree cannot show.

        Disabling this guard today changes no number, because the only Python directly
        under either prefix is a `conftest.py` and the source filter drops it anyway. Put
        a *non-test* module there and the two rules diverge: `src/lambda/shared.py`
        becomes a handler "directory" named after a file, it has no suite, so it joins the
        derived exemption and breaks the count pin — for a file, not a handler. That is
        the artefact behind the figure the issue carries which is two higher than this
        derivation's, and it is the kind of thing a downstream filter masks until the day
        somebody adds a shared module.
        """
        prefix = tmp_path / "src" / "lambda"
        (prefix / "handler").mkdir(parents=True)
        (prefix / "handler" / "index.py").write_text("X = 1\n", encoding="utf-8")
        (prefix / "shared.py").write_text("HELPER = True\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True
        )
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
        found = ccd.handler_directories()
        assert sorted(found) == ["src/lambda/handler"], (
            f"a module sitting directly under the prefix was attributed to a handler "
            f"directory: {sorted(found)}. A path with nothing after the directory "
            f"component names a FILE, and counting it inflates the universe, the exempt "
            f"set and therefore the pinned count."
        )
        assert found["src/lambda/handler"].sources == (
            "src/lambda/handler/index.py",
        ), found["src/lambda/handler"]

    def test_an_unreadable_quarantine_registry_raises_rather_than_returning_nothing(
        self, tmp_path, monkeypatch
    ):
        """Fail closed, because the quiet direction here is the dangerous one.

        An empty answer does not merely lose a judgement: the fan-out would then invoke
        the one root whose collection errors, that error lands in the merged run record,
        and `run_trust` refuses the **whole tree**. So one unreadable registry would take
        a tree of 21 directories' coverage out of the gate, reported as a refusal nobody
        would connect to this function.
        """
        registry = tmp_path / "run_all_tests.py"
        registry.write_text("RUN_ROOTS = ['a']\n", encoding="utf-8")
        monkeypatch.setattr(ccd, "RUN_ROOT_REGISTRY", registry)
        with pytest.raises(AssertionError, match="QUARANTINE"):
            ccd.quarantined_test_roots()

        registry.write_text("QUARANTINE = {}\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="QUARANTINE"):
            ccd.quarantined_test_roots()

        registry.write_text("QUARANTINE = ['a']\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="QUARANTINE"):
            ccd.quarantined_test_roots()

    def test_a_directory_holding_only_tests_is_not_a_handler_directory(
        self, tmp_path, monkeypatch
    ):
        """The source filter, which nothing in the real tree exercises.

        Every directory under a `src/lambda/` prefix today holds at least one source
        file, so dropping this filter changes no number here and survives every other
        assertion in this class. It matters anyway and in the dangerous direction: such a
        directory holds no source, so no file of it can ever be recorded, so it would sit
        in the universe permanently unaccounted for — a gate red for a condition nobody
        could clear, which is how a gate gets switched off.
        """
        bare = tmp_path / "src" / "lambda" / "tests_only"
        bare.mkdir(parents=True)
        (bare / "test_something.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        real = tmp_path / "src" / "lambda" / "with_code"
        real.mkdir(parents=True)
        (real / "index.py").write_text("X = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True
        )
        monkeypatch.setattr(ccd, "REPO_ROOT", tmp_path)
        found = ccd.handler_directories()
        assert "src/lambda/with_code" in found, found
        assert "src/lambda/tests_only" not in found, (
            "a directory holding only test files is counted as a handler directory. It "
            "has no source file, so the baseline can never record one, so it would be "
            "unaccounted for in the closure ratchet forever."
        )

    def test_no_exempt_directory_is_also_a_fanout_root(self):
        """The two sets the fan-out and the exemption are built from cannot overlap."""
        roots = {
            root for tree in ccd.TREES if tree.fanout for root in ccd.fanout_roots(tree)
        }
        overlap = sorted(roots & HANDLER_DIRECTORIES_EXEMPT_FROM_THE_COVERAGE_BASELINE)
        assert not overlap, (
            f"{overlap} are both measured by the fan-out and exempt from the baseline"
        )


def _fake_tree(tmp_path: Path, name: str = "faketree"):
    """A tree rooted in `tmp_path`, so `--write` cannot touch the real baseline.

    Registered by monkeypatching TREES, not by editing the real registry: these tests
    exercise the orchestration, and doing that against the real registry would make
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
    report: Path,
    *,
    errors: int = 0,
    failures: int = 0,
    tests: int = 7,
    skipped: int = 0,
) -> Path:
    """The JUnit XML a pytest run writes beside its coverage report.

    Every producer in the tree writes one, so a fixture that omits it is not a report
    shaped like a real one. Written with the same `<testsuite>` attributes pytest emits,
    which :data:`ERRORED_RUN_RECORD` — captured from a real errored run — is the check on.
    """
    record = ccd.run_record_path(report)
    record.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{tests}" time="1.5"/></testsuites>\n',
        encoding="utf-8",
    )
    return record


def _write_tree_report(
    tree,
    rates: dict[str, float],
    total: float = 80.0,
    *,
    run_record: bool = True,
    tests: int = 7,
    skipped: int = 0,
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
        _write_run_record(report, tests=tests, skipped=skipped)
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
        other trees' baselines. If it did, the routine act of re-recording one
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

        `--require-tree=idp_commmon` against the registry's list is the shape that turns
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
        Recording every other tree while silently dropping the bad one would leave
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


@pytest.mark.unit
class TestMeasurementConditions:
    """A figure is a property of the test set that produced it, so that set is recorded.

    `idp_feature_sdk` was recorded at 99.94% over 461 tests on a machine with the AWS SAM
    CLI installed. The runner has none, so an autouse fixture in that tree's
    `tests/conftest.py` skips 24 tests and the same commit measures 97.15% — and the gate
    reported three files falling by up to 7.80 points when not one had lost a line. The
    floor was simply unreachable where the gate lives, and nothing in the baseline said
    under what conditions it had been taken. Issue #1377.

    What is asserted here is both halves of the remedy: the conditions are **recorded**,
    and they are **quoted beside a finding without ever becoming one**. The second half
    is the one worth guarding — a count difference that failed on its own would red-line
    a branch for a condition nobody caused, and coverage is explicitly allowed to rise.
    """

    def test_the_skip_count_is_read_off_the_run_record(self, tmp_path):
        tree = _fake_tree(tmp_path)
        report = _write_tree_report(tree, {"mod.py": 90.0}, tests=461, skipped=24)
        trust = ccd.run_trust(report)
        assert (trust.collected, trust.skipped) == (461, 24)

    def test_a_skipped_test_does_not_make_a_report_untrustworthy(self, tmp_path):
        """A skip is a test declining to run, not a run that did not finish.

        Only `errors` and `failures` mean the numbers are artefacts. Refusing a report
        for carrying skips would refuse `scripts` (49), `idp_common` (15+) and
        `idp_feature_sdk` on every runner — i.e. almost everything.
        """
        tree = _fake_tree(tmp_path)
        report = _write_tree_report(tree, {"mod.py": 90.0}, tests=461, skipped=24)
        assert ccd.run_trust(report).state == "clean"
        assert "24 skipped" in ccd.run_trust(report).detail

    def test_write_records_the_conditions_each_figure_was_measured_under(
        self, monkeypatch, tmp_path
    ):
        tree = _fake_tree(tmp_path, "measured")
        _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 90.0}, tests=461, skipped=24)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        assert ccd.write_baseline() == 0
        written = json.loads(ccd.BASELINE.read_text())["trees"]["measured"]
        assert written["conditions"] == {"collected": 461, "skipped": 24}, (
            "a baseline whose conditions are unrecorded is indistinguishable from one "
            "the gate's own environment can reach, which is how #1377 arose"
        )

    def test_a_fall_is_reported_with_the_skip_drift_that_may_explain_it(
        self, monkeypatch, tmp_path
    ):
        """The diagnosis that cost a day: did it lose lines, or did its tests not run?"""
        tree = _fake_tree(tmp_path)
        _write_tree_report(tree, {"mod.py": 92.20}, tests=437, skipped=24)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        problems, _ = ccd.check_tree(
            tree, {"pkg/mod.py": 100.0}, {"collected": 461, "skipped": 0}
        )
        assert any("fell from 100.00% to 92.20%" in p for p in problems)
        diagnosis = [p for p in problems if "declined to run here" in p]
        assert diagnosis, f"no conditions diagnosis beside the fall: {problems}"
        assert "skipped 24 test(s)" in diagnosis[0]
        assert "that skipped 0" in diagnosis[0]
        assert "capability gate" in diagnosis[0]

    def test_a_recorded_file_the_report_omits_gets_the_same_diagnosis(
        self, monkeypatch, tmp_path
    ):
        """A skipped test can stop a module being imported at all, not just lose lines."""
        tree = _fake_tree(tmp_path)
        _write_tree_report(tree, {"other.py": 90.0}, tests=437, skipped=24)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/other.py"])
        problems, _ = ccd.check_tree(
            tree, {"pkg/gone.py": 100.0}, {"collected": 461, "skipped": 0}
        )
        assert any("does not mention it" in p for p in problems)
        assert any("declined to run here" in p for p in problems)

    def test_drifted_skips_alone_are_NOT_a_finding(self, monkeypatch, tmp_path):
        """The guard on the whole feature: no new way for this gate to go red.

        Coverage is allowed to rise and a run with fewer skips measures more, so a count
        difference on its own says nothing is wrong. A gate that failed on it would be
        red on every machine whose tool set differs from the recording one — which is
        every developer machine — and a gate that is routinely red for reasons nobody
        caused gets switched off.
        """
        tree = _fake_tree(tmp_path)
        _write_tree_report(tree, {"mod.py": 100.0}, tests=437, skipped=240)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        problems, _ = ccd.check_tree(
            tree, {"pkg/mod.py": 100.0}, {"collected": 461, "skipped": 0}
        )
        assert problems == [], (
            "a skip-count difference with no fall must stay silent; it is context for a "
            f"finding, not a finding: {problems}"
        )

    def test_a_handful_of_drifted_skips_is_not_worth_a_sentence(
        self, monkeypatch, tmp_path
    ):
        """Below the threshold the line is noise beside the finding, so it is omitted."""
        tree = _fake_tree(tmp_path)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        _write_tree_report(
            tree,
            {"mod.py": 80.0},
            tests=461,
            skipped=ccd.SKIP_DRIFT_WORTH_MENTIONING - 1,
        )
        problems, _ = ccd.check_tree(
            tree, {"pkg/mod.py": 100.0}, {"collected": 461, "skipped": 0}
        )
        assert any("fell from" in p for p in problems)
        assert not any("declined to run here" in p for p in problems)

    def test_a_baseline_with_no_conditions_says_so_beside_a_fall(
        self, monkeypatch, tmp_path
    ):
        """Every tree recorded before this existed is in that state, so it is named.

        Silence would be the wrong answer: the reader cannot tell "the conditions match"
        from "nobody recorded them", and those are opposite verdicts on whether the fall
        is real.
        """
        tree = _fake_tree(tmp_path)
        _write_tree_report(tree, {"mod.py": 80.0}, tests=437, skipped=24)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        problems, _ = ccd.check_tree(tree, {"pkg/mod.py": 100.0}, None)
        assert any("records no measurement conditions" in p for p in problems)

    def test_every_recorded_tree_that_has_conditions_has_them_well_formed(self):
        """Guards the real baseline rather than a synthetic one.

        Trees recorded before this feature carry none, which is allowed; a tree that
        carries the key must carry both counts, as integers, with `skipped` no greater
        than `tests`.
        """
        trees = json.loads(ccd.BASELINE.read_text())["trees"]
        with_conditions = {
            name: data["conditions"]
            for name, data in trees.items()
            if "conditions" in data
        }
        assert with_conditions, (
            "no tree records its measurement conditions. At least idp_feature_sdk "
            "should: its baseline is the one that was unreachable in CI (#1377)."
        )
        for name, conditions in with_conditions.items():
            assert set(conditions) == {"collected", "skipped"}, f"{name}: {conditions}"
            assert isinstance(conditions["collected"], int), name
            assert isinstance(conditions["skipped"], int), name
            assert 0 <= conditions["skipped"] <= conditions["collected"], (
                f"{name}: skipped {conditions['skipped']} of {conditions['collected']}"
            )


@pytest.mark.unit
class TestRequireAllTreesIsDerivedFromTheRegistry:
    """`--require-all-trees` must turn an unmeasured tree into a REFUSAL (#1256).

    ⚠️ **Asserting a non-zero exit does not prove this, and that is the whole
    difficulty.** `check()` computes ``2 if refusals else (1 if problems else 0)``:
    `unchecked` appears in **neither** term, so a tree with no report contributes
    nothing at all to the exit code. A case built from one tree therefore exits 2 with
    the flag *and* without it — with it because the required tree was not checked,
    without it because *no* tree was checked, which is a different refusal reached by a
    different line. A proof reading only the status passes on that, and would keep
    passing with the flag deleted. #1190 was exactly that shape of vacuity in this
    gate, so the method here is the one that found it.

    Every case runs **twice over the same fixture** — once through
    `--require-all-trees`, once bare — and is classified by the refusal **wording**,
    which only the per-name loop appending to `refusals` can emit. The bare column is
    the control: it must exit 0 with that wording absent, which is what makes
    "Unchecked" and "refused" distinguishable rather than inferred.

    Each fixture therefore carries a **healthy** tree alongside the unmeasured one.
    Without it the bare run refuses on "no tree was checked" and the two columns
    collapse into the same answer again.
    """

    #: The sentence only the `for name in required:` loop in `check()` produces. Nothing
    #: on the `unchecked` path can print it, which is what makes it a classifier.
    REFUSAL = "was passed, but this tree was not checked"

    def _both_ways(self, monkeypatch, tmp_path, capsys, build):
        """Run `main()` over one fixture with the flag and without. Returns both.

        `build` is called once per run against a freshly installed registry, so the two
        runs see identical inputs — a fixture built once and compared twice would let a
        first run's side effect (a report written, a baseline rewritten) explain the
        difference instead of the flag.
        """
        results = {}
        for label, argv in (
            ("flag", ["check_coverage_debt.py", "--require-all-trees"]),
            ("bare", ["check_coverage_debt.py"]),
        ):
            root = tmp_path / label
            root.mkdir()
            build(root)
            monkeypatch.setattr(sys, "argv", argv)
            code = ccd.main()
            results[label] = (code, capsys.readouterr().out)
        return results

    def _healthy_plus(self, root, monkeypatch, extra, baseline_extra=None):
        """A tree that WILL compare clean, plus `extra` trees that will not."""
        healthy = _fake_tree(root, "healthy")
        trees = [healthy, *extra]
        baseline = {
            "trees": {
                "healthy": {"total": 90.0, "files": {"pkg/mod.py": 90.0}},
                **(baseline_extra or {}),
            }
        }
        _install(monkeypatch, root, trees, baseline)
        monkeypatch.setattr(
            ccd,
            "tracked_source_files",
            lambda t: ["pkg/mod.py"] if t.name == "healthy" else [],
        )
        _write_tree_report(healthy, {"mod.py": 90.0})
        return trees

    def test_a_tree_whose_report_is_MISSING_is_refused_by_name_and_not_bare(
        self, monkeypatch, tmp_path, capsys
    ):
        """Case 1. The ordinary CI failure: a producer step that did not run.

        The control column is the assertion that matters — bare exits **0** over the
        same inputs, naming `absent` as unchecked and passing, which is precisely the
        state that left eight of nine trees unratcheted.
        """

        def build(root):
            self._healthy_plus(
                root,
                monkeypatch,
                [_fake_tree(root, "absent")],
                {"absent": {"total": 70.0, "files": {"pkg/mod.py": 70.0}}},
            )
            # Deliberately no report for `absent`.

        got = self._both_ways(monkeypatch, tmp_path, capsys, build)

        flag_code, flag_out = got["flag"]
        bare_code, bare_out = got["bare"]
        assert flag_code == 2, f"flag run exited {flag_code}:\n{flag_out}"
        assert self.REFUSAL in flag_out and "[absent]" in flag_out, flag_out
        assert "no coverage report at" in flag_out, flag_out
        # The control. Classified by wording, not by status: bare must reach a PASS over
        # the identical fixture, so the difference is attributable to the flag alone.
        assert bare_code == 0, (
            f"the control run exited {bare_code}, so this fixture fails for a reason "
            f"other than the flag and the case proves nothing:\n{bare_out}"
        )
        assert self.REFUSAL not in bare_out, bare_out
        assert "not checked" in bare_out and "✅" in bare_out, bare_out

    def test_a_tree_whose_report_is_EMPTY_is_refused_by_name_and_not_bare(
        self, monkeypatch, tmp_path, capsys
    ):
        """Case 2, and the one a reader is likeliest to misjudge: the file is there.

        A zero-byte `coverage.xml` is what a producer leaves behind when it is killed
        mid-write, and it is worse than an absent one because the remedy a reader
        reaches for is "the report exists, so the gate must have compared it". The
        report resolves, nothing parses, and the tree still reaches no verdict — so the
        flag has to refuse it by name exactly as it refuses an absent one.
        """

        def build(root):
            trees = self._healthy_plus(
                root,
                monkeypatch,
                [_fake_tree(root, "empty")],
                {"empty": {"total": 70.0, "files": {"pkg/mod.py": 70.0}}},
            )
            report = ccd.report_path(trees[1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("", encoding="utf-8")
            # A run record beside it, so the refusal cannot be the trust check firing on
            # a missing record instead of on the empty report itself.
            _write_run_record(report)

        got = self._both_ways(monkeypatch, tmp_path, capsys, build)

        flag_code, flag_out = got["flag"]
        bare_code, bare_out = got["bare"]
        assert flag_code == 2, f"flag run exited {flag_code}:\n{flag_out}"
        assert self.REFUSAL in flag_out and "[empty]" in flag_out, flag_out
        assert bare_code == 0, (
            f"the control run exited {bare_code}, so this fixture fails for a reason "
            f"other than the flag and the case proves nothing:\n{bare_out}"
        )
        assert self.REFUSAL not in bare_out, bare_out
        assert "✅" in bare_out, bare_out

    def test_a_tree_ADDED_TO_THE_REGISTRY_is_required_without_any_caller_edit(
        self, monkeypatch, tmp_path, capsys
    ):
        """Case 3, and the reason the derivation is inside the script.

        The tree is **invented by this test** and named nothing like the real ones,
        so the requirement cannot be satisfied by a list of today's names in a Makefile,
        a CI config or this module. If the derivation stopped reading `TREES`, a tree
        added to the registry and produced by nobody would be unratcheted while the gate
        reported that every tree was required — which is the defect one layer up from
        the one #1256 is about.
        """

        def build(root):
            self._healthy_plus(
                root,
                monkeypatch,
                [_fake_tree(root, "a_tree_invented_by_this_test")],
                # No baseline entry either: a brand-new tree has none, which is the real
                # arrival shape. Its report is also absent.
            )

        got = self._both_ways(monkeypatch, tmp_path, capsys, build)

        flag_code, flag_out = got["flag"]
        bare_code, bare_out = got["bare"]
        assert flag_code == 2, f"flag run exited {flag_code}:\n{flag_out}"
        assert self.REFUSAL in flag_out, flag_out
        assert "[a_tree_invented_by_this_test]" in flag_out, flag_out
        assert bare_code == 0, (
            f"the control run exited {bare_code}, so this fixture fails for a reason "
            f"other than the flag and the case proves nothing:\n{bare_out}"
        )
        assert self.REFUSAL not in bare_out, bare_out

    def test_the_required_set_is_every_registry_tree_and_nothing_else(
        self, monkeypatch, tmp_path, capsys
    ):
        """The derivation's extent, over the REAL registry rather than a fixture.

        Equality in both directions. A derivation that required a superset would refuse
        on a name no producer could ever satisfy; one that required a subset is the
        #1256 gap with a flag on top. Read off the refusals, which name one tree each,
        so this is the gate's own answer rather than a re-derivation of it.
        """
        trees = [_fake_tree(tmp_path, t.name) for t in ccd.TREES]
        _install(monkeypatch, tmp_path, trees, {"trees": {}})
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: [])
        # ⚠️ `LEGACY_IDP_COMMON_REPORT` is an absolute path fixed at import, so
        # `_install`'s REPO_ROOT patch does not move it and `idp_common` resolves to
        # whatever report the real checkout happens to hold. Without this the test
        # passes on a clean tree and fails right after `make test-cicd` writes that
        # file — an order dependence, not a finding.
        monkeypatch.setattr(
            ccd, "LEGACY_IDP_COMMON_REPORT", tmp_path / "no-legacy-report.xml"
        )
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--require-all-trees"]
        )
        assert ccd.main() == 2
        out = capsys.readouterr().out
        refused = {
            line.split("]")[0].lstrip(" -[")
            for line in out.splitlines()
            if self.REFUSAL in line
        }
        assert refused == {t.name for t in ccd.TREES}, (
            f"the flag required {sorted(refused)} but the registry holds "
            f"{sorted(t.name for t in ccd.TREES)}"
        )

    def test_it_is_refused_alongside_write_or_summary_naming_the_flag_given(
        self, monkeypatch, tmp_path, capsys
    ):
        """Neither mode compares anything, so the precondition would go unevaluated.

        The message has to name `--require-all-trees`, because that is the flag on the
        command line: `--require-tree` is only what it expands into internally, and a
        refusal citing a flag the caller did not pass sends them looking for it.
        """
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path)], {"trees": {}})
        for other in ("--write", "--summary"):
            monkeypatch.setattr(
                sys, "argv", ["check_coverage_debt.py", "--require-all-trees", other]
            )
            assert ccd.main() == 2
            out = capsys.readouterr().out
            assert "--require-all-trees" in out, out
            assert "--require-tree asserts" not in out, out

    def test_the_cicd_ratchet_target_is_the_only_one_passing_the_flag(self):
        """Both CIs must reach the flag through a target, and the local one must not.

        `check-coverage-debt` with no arguments is correct for a developer who measured
        one tree, and is the exact invocation that passed over eight unmeasured ones in
        CI. The two spellings therefore have to stay distinct targets.
        """
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        recipes = {}
        for block in makefile.split("\n"):
            if block.startswith(("check-coverage-debt:", "check-coverage-debt-cicd:")):
                recipes[block.split(":")[0]] = True
        assert set(recipes) == {"check-coverage-debt", "check-coverage-debt-cicd"}, (
            f"expected both targets, found {sorted(recipes)}"
        )
        cicd = makefile.split("\ncheck-coverage-debt-cicd:")[1].split("\n\n")[0]
        local = makefile.split("\ncheck-coverage-debt:")[1].split("\n\n")[0]
        assert "--require-all-trees" in cicd, cicd
        assert "--require-all-trees" not in local, local

    def test_the_cicd_producer_skips_exactly_the_tree_CI_already_measured(self):
        """Pins `COVERAGE_CICD_SKIP`'s membership, and that the recipe still runs.

        Two separate things, because the registry entry for that variable rests on both
        and neither follows from the other: a second name added to it would narrow the
        producer silently, and a recipe emptied of its command would leave the variable
        looking correct while nothing was produced at all.
        """
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assigned = next(
            line.split("?=", 1)[1].strip()
            for line in makefile.splitlines()
            if line.startswith("COVERAGE_CICD_SKIP")
        )
        assert assigned == "--skip idp_common", (
            f"COVERAGE_CICD_SKIP is {assigned!r}. Every name in it is a tree the "
            f"producer does not measure; only `idp_common` has an earlier CI step "
            f"writing its report."
        )
        recipe = makefile.split("\ncoverage-all-cicd:")[1].split("\n\n")[0]
        assert "scripts/coverage_all.py" in recipe, recipe
        assert "$(COVERAGE_CICD_SKIP)" in recipe, recipe
        assert "--jobs" in recipe, recipe

    @pytest.mark.parametrize(
        "config", [".gitlab-ci.yml", ".github/workflows/developer-tests.yml"]
    )
    def test_each_config_runs_the_producer_then_the_cicd_ratchet_in_ONE_job(
        self, config
    ):
        """Same job and in order, parsed rather than grepped for position.

        Two GitLab jobs in one stage run in **parallel**, so file position proves no
        ordering; and the reports pass between these steps through the workspace, so
        splitting them across jobs would need artifacts. Both facts make "one job, in
        this order" the property to assert.
        """
        data = yaml.safe_load((REPO_ROOT / config).read_text(encoding="utf-8"))
        wanted = [
            "make test-cicd -C lib/idp_common_pkg",
            "make coverage-all-cicd",
            "make check-coverage-debt-cicd",
        ]

        def command_lists(node):
            if isinstance(node, dict):
                steps = node.get("steps")
                if isinstance(steps, list):  # a GitHub job
                    yield [str(s.get("run", "")) for s in steps if isinstance(s, dict)]
                script = node.get("script")
                if isinstance(script, list):  # a GitLab job
                    yield [str(line) for line in script]
                for value in node.values():
                    yield from command_lists(value)

        holding_all = [
            commands
            for commands in command_lists(data)
            if all(any(w in c for c in commands) for w in wanted)
        ]
        assert holding_all, (
            f"{config} has no single job running all of {wanted}. Either a step is "
            f"gone, or they are now in separate jobs — where nothing orders the "
            f"ratchet after the runs that write the reports it reads, and the reports "
            f"would have to be passed between them as artifacts."
        )
        for commands in holding_all:
            positions = [
                next(i for i, c in enumerate(commands) if w in c) for w in wanted
            ]
            assert positions == sorted(positions), (
                f"{config} runs them at {positions} for {wanted}; the ratchet must "
                f"follow both producers or it reads whatever the workspace had."
            )


@pytest.mark.unit
class TestWriteMissingCannotMoveAnExistingFloor:
    """The bounded remedy, for the one finding that needs exactly one new entry.

    `check_tree` reports a tracked file the report measures and the baseline does not
    record, and the remedy it prints is `--write` — which re-records the file set of
    every measured tree wholesale. On a gate whose figures are host-sensitive that is
    the wrong direction: measured on the `scripts` tree while closing #1256, one file
    needed recording and `--write` would have raised ten existing floors with it, six
    past the tolerance and the largest by 18.75 points, pinning CI to a figure this
    machine produced.

    So the property to hold is narrow and absolute: an existing entry is byte-identical
    afterwards, whatever the report says about it.
    """

    def _tree_with(self, monkeypatch, tmp_path, recorded, measured, tracked):
        tree = _fake_tree(tmp_path)
        path = _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 50.0, "files": dict(recorded)}}},
        )
        _write_tree_report(tree, measured, total=99.0)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: list(tracked))
        return tree, path

    def test_a_file_the_baseline_lacks_is_added_at_its_measured_figure(
        self, monkeypatch, tmp_path, capsys
    ):
        tree, path = self._tree_with(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0, "new.py": 97.71},
            tracked=["pkg/old.py", "pkg/new.py"],
        )
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        out = capsys.readouterr().out
        files = json.loads(path.read_text())["trees"][tree.name]["files"]
        assert files["pkg/new.py"] == 97.71, files
        assert "pkg/new.py at 97.71%" in out, out

    def test_an_existing_entry_is_untouched_even_though_the_report_measures_higher(
        self, monkeypatch, tmp_path, capsys
    ):
        """The direction that matters. `old.py` is recorded at 40 and measures 95 here;
        `--write` would pin 95, which is only reachable on a machine like this one."""
        tree, path = self._tree_with(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0, "new.py": 97.71},
            tracked=["pkg/old.py", "pkg/new.py"],
        )
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        capsys.readouterr()
        recorded = json.loads(path.read_text())["trees"][tree.name]
        assert recorded["files"]["pkg/old.py"] == 40.0, recorded
        # And the tree's own total and conditions are not re-derived either: nothing
        # about what was already recorded is this mode's business.
        assert recorded["total"] == 50.0, recorded

    def test_it_does_not_lower_an_existing_entry_either(
        self, monkeypatch, tmp_path, capsys
    ):
        """A fall is a FINDING, never something a write mode absorbs. If this mode
        lowered a floor it would be a way to make the ratchet's own report go away."""
        tree, path = self._tree_with(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 90.0},
            measured={"old.py": 10.0},
            tracked=["pkg/old.py"],
        )
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        capsys.readouterr()
        files = json.loads(path.read_text())["trees"][tree.name]["files"]
        assert files["pkg/old.py"] == 90.0, files
        # The fall is still reported by the ordinary check, which is where it belongs.
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py"])
        assert ccd.main() == 1

    def test_nothing_missing_is_a_no_op_on_the_recorded_figures_and_idempotent(
        self, monkeypatch, tmp_path, capsys
    ):
        """The common case once the debt is paid, asserted in the two forms that hold.

        Byte-identity against the *input* is not one of them and must not be asserted:
        every write regenerates the file's `$comment` envelope and indentation, so a
        hand-written or older fixture differs in bytes while recording the same figures.
        What is true is that the recorded figures are unchanged, and that a second run
        over the gate's own output is byte-for-byte identical — which is the property a
        reviewer relies on, since it is what keeps a no-op from producing a diff.
        """
        tree, path = self._tree_with(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0},
            tracked=["pkg/old.py"],
        )
        before = json.loads(path.read_text())["trees"]
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        assert "nothing missing" in capsys.readouterr().out
        assert json.loads(path.read_text())["trees"] == before
        first = path.read_bytes()
        assert ccd.main() == 0
        capsys.readouterr()
        assert path.read_bytes() == first, "a second run produced a diff"

    def test_it_refuses_an_errored_report_exactly_as_write_does(
        self, monkeypatch, tmp_path, capsys
    ):
        """The narrower mode must not be the loophole in the trust check. A run whose
        workers errored measures files it never executed, so a file "missing from the
        baseline" in such a report may simply be one whose tests did not run."""
        tree, path = self._tree_with(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0, "new.py": 97.0},
            tracked=["pkg/old.py", "pkg/new.py"],
        )
        report = ccd.report_path(tree)
        ccd.run_record_path(report).write_text(ERRORED_RUN_RECORD, encoding="utf-8")
        os.utime(ccd.run_record_path(report), (report.stat().st_mtime,) * 2)
        before = path.read_bytes()
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 2
        out = capsys.readouterr().out
        assert path.read_bytes() == before, "it wrote despite refusing"
        # And the refusal is the EXPLICIT one, naming the unfinished run. Two branches
        # independently stop a bad report being recorded, so dropping the first still
        # writes nothing and still exits 2 — it just reports the tree as "leaving its
        # baseline untouched", which reads as an absent report rather than a refused
        # one. The remedy differs (produce a report / re-run the suite), so the wording
        # is the part worth pinning here; the safety is doubly held either way.
        assert "recorded nothing" in out and "did not finish" in out, out
        assert "leaving its baseline untouched" not in out, out

    def test_passing_both_write_modes_is_refused(self, monkeypatch, tmp_path, capsys):
        """One re-records every file of every measured tree, the other cannot change a
        single existing figure. There is no reading of both that narrows either."""
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path)], {"trees": {}})
        monkeypatch.setattr(
            sys, "argv", ["check_coverage_debt.py", "--write", "--write-missing"]
        )
        assert ccd.main() == 2
        assert "Pick one" in capsys.readouterr().out

    def test_it_is_refused_alongside_a_required_tree(
        self, monkeypatch, tmp_path, capsys
    ):
        """Same reason `--write` is: neither mode compares anything, so a precondition
        passed with it is one nothing will evaluate."""
        _install(monkeypatch, tmp_path, [_fake_tree(tmp_path)], {"trees": {}})
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_coverage_debt.py", "--write-missing", "--require-all-trees"],
        )
        assert ccd.main() == 2
        assert "--require-all-trees" in capsys.readouterr().out

    def test_a_tree_with_no_baseline_at_all_is_recorded_in_full(
        self, monkeypatch, tmp_path, capsys
    ):
        """There is nothing to preserve, so the ordinary full record is both correct and
        the only thing available — and recording nothing would leave the tree with no
        baseline, which `check` reports as unratcheted."""
        tree = _fake_tree(tmp_path)
        path = _install(monkeypatch, tmp_path, [tree], {"trees": {}})
        _write_tree_report(tree, {"mod.py": 88.0}, total=88.0)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        capsys.readouterr()
        recorded = json.loads(path.read_text())["trees"][tree.name]
        assert recorded["files"] == {"pkg/mod.py": 88.0}
        assert recorded["total"] == 88.0


def _commands_named(message: str) -> set[str]:
    """Every write flag a message offers, read so the two can be told apart.

    `--write-missing` *contains* `--write`, so a plain substring test for the wide flag
    is satisfied by the narrow one and an assertion built on it passes against the
    message it was written to reject. The longer flag is matched first and consumed, so
    "names one and not the other" becomes a question with an answer — which is the only
    form in which this property can be asserted at all.
    """
    named: set[str] = set()
    rest = message
    for flag in ("--write-missing", "--write"):
        if flag in rest:
            named.add(flag)
            rest = rest.replace(flag, "")
    return named


@pytest.mark.unit
class TestTheRemedyNamesTheCommandThatFixesThatFinding:
    """A printed remedy is run, so each finding must name the command *it* needs.

    One remedy served every finding, and it was `--write`. For the two findings that need
    nothing but new entries that command is wider than the finding: `--write` re-records
    every measured tree wholesale, so following it to record one new module also accepts
    the running machine's figure for every other file in that tree. Counted twice on the
    `scripts` tree — once while closing #1256 and again in #1421 — one file needed
    recording and `--write` would have raised ten existing floors with it, two of them on
    files whose host-versus-CI discrepancy is deliberately pinned and must stay visible.
    The gate goes green either way and the diff looks like bookkeeping, which is what
    makes a printed command the strongest possible nudge toward the wrong action.

    What is asserted here is the distinction in **both** directions, because asserting
    only that `--write-missing` appears somewhere passes on a message naming both flags
    and leaves the reader exactly as stuck. Every case is driven through the real
    function against a report that produces that one finding, and the two cases that
    genuinely need the wider command are pinned as keeping it — together with the
    measurement that makes `--write` the right answer there, so a later edit cannot
    "fix" them by symmetry.
    """

    def _tree(self, monkeypatch, tmp_path, recorded, measured, tracked, **report):
        tree = _fake_tree(tmp_path)
        path = _install(
            monkeypatch,
            tmp_path,
            [tree],
            {"trees": {tree.name: {"total": 50.0, "files": dict(recorded)}}}
            if recorded is not None
            else {"trees": {}},
        )
        _write_tree_report(tree, measured, total=99.0, **report)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: list(tracked))
        return tree, path

    def _only(self, problems: list[str], needle: str) -> str:
        matching = [p for p in problems if needle in p]
        assert len(matching) == 1, f"expected one {needle!r} finding, got {problems}"
        return matching[0]

    def test_an_unratcheted_file_is_told_to_use_write_missing_and_not_bare_write(
        self, monkeypatch, tmp_path
    ):
        """The finding is one absent entry, and `--write-missing` adds exactly that.

        The fixture is deliberately lopsided: the recorded file is measured *higher* than
        its floor, so `--write` would move it and `--write-missing` provably cannot. A
        fixture where the recorded figure matched would be fixed identically by either
        command and would assert nothing.
        """
        tree, _ = self._tree(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0, "new.py": 97.71},
            tracked=["pkg/old.py", "pkg/new.py"],
        )
        problems, _ = ccd.check_tree(tree, {"pkg/old.py": 40.0})
        remedy = self._only(problems, "unratcheted")
        assert _commands_named(remedy) == {"--write-missing"}, remedy
        assert "cannot raise or lower an existing floor" in remedy, remedy

    def test_running_the_remedy_it_prints_records_the_entry_and_moves_no_floor(
        self, monkeypatch, tmp_path, capsys
    ):
        """The flag is parsed out of the printed message and then run.

        This is the assertion the two incidents would have failed: following the gate's
        own instruction has to fix the finding it was printed for and nothing else. With
        the old message the flag read out of it is `--write`, and the recorded floor
        below moves from 40.00 to 95.00 — so the test is about the remedy's *effect*
        rather than about its wording.
        """
        tree, path = self._tree(
            monkeypatch,
            tmp_path,
            recorded={"pkg/old.py": 40.0},
            measured={"old.py": 95.0, "new.py": 97.71},
            tracked=["pkg/old.py", "pkg/new.py"],
        )
        problems, _ = ccd.check_tree(tree, {"pkg/old.py": 40.0})
        named = _commands_named(self._only(problems, "unratcheted"))
        assert len(named) == 1, f"the remedy must name one command, not {named}"
        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", *named])
        assert ccd.main() == 0
        capsys.readouterr()
        files = json.loads(path.read_text())["trees"][tree.name]["files"]
        assert files["pkg/old.py"] == 40.0, (
            f"the remedy printed for one absent entry moved an existing floor: {files}"
        )
        assert files["pkg/new.py"] == 97.71, files
        assert ccd.check().code == 0, "the remedy it printed did not clear the finding"

    def test_a_tree_with_no_baseline_is_told_to_use_write_missing_too(
        self, monkeypatch, tmp_path, capsys
    ):
        """Nothing in this tree is ratcheted, and `--write-missing` records it in full.

        The narrow flag is the right answer for a reason that is a property of
        `write_baseline`, not of the flag's name: a tree with no entry has nothing to
        preserve, so `missing_only` falls through to the ordinary full record — total,
        conditions and every file. What it does *not* do is re-record the other measured
        trees, which is the whole difference, so a second tree is registered here with a
        floor its report exceeds and asserted unmoved.
        """
        unratcheted, other = _fake_tree(tmp_path, "fresh"), _fake_tree(tmp_path, "aged")
        path = _install(
            monkeypatch,
            tmp_path,
            [unratcheted, other],
            {"trees": {"aged": {"total": 50.0, "files": {"pkg/mod.py": 40.0}}}},
        )
        _write_tree_report(unratcheted, {"mod.py": 88.0}, total=88.0)
        _write_tree_report(other, {"mod.py": 95.0}, total=95.0)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])

        remedy = self._only(ccd.check().problems, "no recorded baseline")
        assert _commands_named(remedy) == {"--write-missing"}, remedy

        monkeypatch.setattr(sys, "argv", ["check_coverage_debt.py", "--write-missing"])
        assert ccd.main() == 0
        capsys.readouterr()
        written = json.loads(path.read_text())["trees"]
        assert written["fresh"]["files"] == {"pkg/mod.py": 88.0}
        assert written["fresh"]["conditions"]["collected"] == 7
        assert written["aged"]["files"] == {"pkg/mod.py": 40.0}, (
            "recording the unratcheted tree moved another tree's floor"
        )

    def test_a_baseline_with_no_conditions_keeps_write_because_only_write_adds_them(
        self, monkeypatch, tmp_path
    ):
        """The one finding `--write-missing` genuinely cannot fix, so it still says
        `--write` — and says that the command is the wider one.

        This finding is only reachable for a tree that *has* recorded files (it is quoted
        beside a fall), and `missing_only` preserves such an entry verbatim. A conditions
        record is written only as part of recording a whole tree, so naming the narrow
        flag here would print a remedy that runs, reports success, and leaves the finding
        exactly where it was. The behavioural half of that claim is measured in the next
        test rather than asserted in prose.
        """
        tree = _fake_tree(tmp_path)
        _write_tree_report(tree, {"mod.py": 80.0}, tests=437, skipped=24)
        monkeypatch.setattr(ccd, "tracked_source_files", lambda t: ["pkg/mod.py"])
        problems, _ = ccd.check_tree(tree, {"pkg/mod.py": 100.0}, None)
        remedy = self._only(problems, "records no measurement conditions")
        assert "--write" in _commands_named(remedy), remedy
        assert "--write-missing cannot" in remedy, (
            "the reader is told to use the wider command without being told why the "
            f"narrow one is not offered: {remedy}"
        )
        assert "every measured tree" in remedy, remedy

    @pytest.mark.parametrize("missing_only", [True, False])
    def test_only_the_wide_command_captures_conditions_on_an_existing_entry(
        self, monkeypatch, tmp_path, capsys, missing_only
    ):
        """Why the remedy above is `--write`, measured on both modes of one function.

        A tree recorded before `conditions` existed has files and no conditions, which is
        exactly the state the finding reports. `--write-missing` leaves it in that state;
        `--write` captures them. Parametrised over the same fixture so the two answers are
        a property of the mode rather than of two differently built trees.
        """
        tree, path = self._tree(
            monkeypatch,
            tmp_path,
            recorded={"pkg/mod.py": 40.0},
            measured={"mod.py": 90.0},
            tracked=["pkg/mod.py"],
            tests=100,
            skipped=4,
        )
        assert ccd.write_baseline(missing_only=missing_only) == 0
        capsys.readouterr()
        entry = json.loads(path.read_text())["trees"][tree.name]
        assert ("conditions" in entry) is (not missing_only), entry
        if missing_only:
            assert entry["files"] == {"pkg/mod.py": 40.0}, (
                "the narrow mode moved a floor while failing to add conditions"
            )

    @pytest.mark.parametrize(
        ("recorded", "measured", "needle"),
        [
            ({"pkg/mod.py": 90.0}, {"mod.py": 60.0}, "fell from"),
            ({"pkg/gone.py": 50.0}, {"mod.py": 90.0}, "does not mention it"),
        ],
    )
    def test_the_findings_that_need_the_wider_command_still_name_it_and_say_so(
        self, monkeypatch, tmp_path, recorded, measured, needle
    ):
        """Lowering a floor and dropping a stale entry are both outside `missing_only`.

        `--write-missing` preserves every existing entry, so it can neither write a
        smaller figure over one nor delete one whose file has gone: offering it for these
        two findings would be the same defect pointing the other way. What changed for
        them is that the message now says the command re-records every measured tree, so
        a reader knows what they are accepting before they run it.
        """
        tree, _ = self._tree(
            monkeypatch,
            tmp_path,
            recorded=recorded,
            measured=measured,
            tracked=list(recorded),
        )
        problems, _ = ccd.check_tree(tree, recorded, {"collected": 7, "skipped": 0})
        remedy = self._only(problems, needle)
        assert _commands_named(remedy) == {"--write"}, remedy
        assert "wholesale" in remedy, (
            f"the wider command is offered without saying what it moves: {remedy}"
        )
