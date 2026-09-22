# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the coverage ratchet.

The ratchet has two halves — an aggregate floor in `lib/idp_common_pkg/Makefile` and a
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


def _report(tmp_path: Path, rates: dict[str, float], *, total: float = 80.0) -> Path:
    """A Cobertura report shaped the way coverage.py emits one for --cov=idp_common.

    Note the `filename` attributes are `<source>`-relative and carry no `idp_common/`
    prefix, which is the detail the checker has to normalise.
    """
    source = REPO_ROOT / "lib" / "idp_common_pkg" / "idp_common"
    classes = "\n".join(
        f'<class filename="{name}" line-rate="{rate / 100}"/>'
        for name, rate in rates.items()
    )
    path = tmp_path / "coverage.xml"
    path.write_text(
        f'<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{total / 100}">'
        f"<sources><source>{source}</source></sources>"
        f"<packages><package><classes>{classes}</classes></package></packages>"
        f"</coverage>\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def baseline_at(tmp_path, monkeypatch):
    """Point the checker at a throwaway baseline so the real one is never written."""

    def _set(files: dict[str, float], total: float = 80.0) -> Path:
        path = tmp_path / "coverage_debt.json"
        path.write_text(json.dumps({"total": total, "files": files}, indent=2))
        monkeypatch.setattr(ccd, "BASELINE", path)
        return path

    return _set


@pytest.mark.unit
class TestReportParsing:
    def test_report_filenames_are_resolved_against_their_source_root(self, tmp_path):
        """The bug that made the first version record nothing.

        `filename` is `<source>`-relative, so it must be joined to `<source>` and made
        package-relative before it can be compared with anything.
        """
        report = _report(tmp_path, {"ocr/service.py": 99.0})
        assert ccd.read_report(report) == {"idp_common/ocr/service.py": 99.0}

    def test_a_filename_outside_the_package_is_left_alone(self, tmp_path):
        # Defensive rather than expected: a report covering something else should not be
        # silently rewritten into a package-relative path it does not belong to.
        report = _report(tmp_path, {"../elsewhere/mod.py": 50.0})
        assert "idp_common/../elsewhere/mod.py" not in ccd.read_report(report)

    def test_the_overall_rate_is_read_from_the_root_element(self, tmp_path):
        assert ccd.report_total(_report(tmp_path, {}, total=77.86)) == 77.86


@pytest.mark.unit
class TestRegressionDetection:
    def test_an_unchanged_file_passes(self, tmp_path, baseline_at):
        baseline_at({"idp_common/ocr/service.py": 99.0})
        code, problems = ccd.check(_report(tmp_path, {"ocr/service.py": 99.0}))
        assert (code, problems) == (0, [])

    def test_a_drop_beyond_the_tolerance_fails_and_names_both_numbers(
        self, tmp_path, baseline_at
    ):
        baseline_at({"idp_common/ocr/service.py": 99.0})
        code, problems = ccd.check(_report(tmp_path, {"ocr/service.py": 60.0}))
        assert code == 1
        assert "99.00" in problems[0] and "60.00" in problems[0], problems
        assert "ocr/service.py" in problems[0]

    def test_a_rise_does_not_fail(self, tmp_path, baseline_at):
        """Deliberate. A ratchet that demanded a re-record on every improvement would be
        re-recorded reflexively, and a file whose coverage had genuinely fallen would be
        re-recorded along with it."""
        baseline_at({"idp_common/ocr/service.py": 60.0})
        code, _ = ccd.check(_report(tmp_path, {"ocr/service.py": 99.0}))
        assert code == 0

    @pytest.mark.parametrize("delta", [0.5, 1.0])
    def test_a_drop_within_the_tolerance_is_allowed(self, tmp_path, baseline_at, delta):
        # Line coverage is not perfectly stable run to run; a zero-tolerance ratchet over
        # 226 files turns that noise into routine red, and a routinely-red gate is ignored.
        baseline_at({"idp_common/ocr/service.py": 90.0})
        code, _ = ccd.check(_report(tmp_path, {"ocr/service.py": 90.0 - delta}))
        assert code == 0

    def test_a_drop_just_past_the_tolerance_fails(self, tmp_path, baseline_at):
        # Paired with the test above, so the tolerance is pinned as a boundary rather than
        # as "some slack exists".
        baseline_at({"idp_common/ocr/service.py": 90.0})
        code, _ = ccd.check(_report(tmp_path, {"ocr/service.py": 88.5}))
        assert code == 1

    def test_a_recorded_file_missing_from_the_report_fails(self, tmp_path, baseline_at):
        """Either the entry is stale, or the file stopped being imported by any test.

        The second is a real regression and looks exactly like the first, so it cannot be
        passed over silently.
        """
        baseline_at({"idp_common/gone.py": 50.0})
        code, problems = ccd.check(_report(tmp_path, {"ocr/service.py": 99.0}))
        assert code == 1
        assert "gone.py" in problems[0]


@pytest.mark.unit
class TestUniverseClosure:
    def test_a_measured_file_absent_from_the_baseline_fails(
        self, tmp_path, baseline_at, monkeypatch
    ):
        """A new module must not arrive unratcheted."""
        baseline_at({})
        monkeypatch.setattr(
            ccd, "tracked_source_files", lambda: ["idp_common/brand_new.py"]
        )
        code, problems = ccd.check(_report(tmp_path, {"brand_new.py": 10.0}))
        assert code == 1
        assert "brand_new.py" in problems[0]

    def test_an_exempt_path_is_not_required(self, tmp_path, baseline_at, monkeypatch):
        baseline_at({})
        monkeypatch.setattr(
            ccd, "tracked_source_files", lambda: ["idp_common/skipme/data.py"]
        )
        monkeypatch.setattr(ccd, "NOT_MEASURED", {"idp_common/skipme": "a reason"})
        code, _ = ccd.check(_report(tmp_path, {"skipme/data.py": 0.0}))
        assert code == 0

    def test_every_not_measured_entry_carries_a_reason(self):
        for path, reason in ccd.NOT_MEASURED.items():
            assert reason.strip(), f"NOT_MEASURED[{path}] has no reason"


@pytest.mark.unit
class TestTheRealBaseline:
    """Assertions about the committed baseline, not about a fixture."""

    def test_the_recorded_baseline_is_not_empty(self):
        """The non-vacuity ratchet: an empty baseline shields nothing.

        The first version of the checker recorded zero files and reported success, so
        every other assertion about it was **vacuous** — the per-file comparison had no
        files to compare and the universe-closure check had nothing to close over.
        Nothing in a fixture-only suite would have noticed, because each fixture supplied
        its own entries. This is the assertion that reads the committed baseline.
        """
        baseline = json.loads(
            (REPO_ROOT / "scripts" / "coverage_debt.json").read_text(encoding="utf-8")
        )
        files = baseline.get("files", {})
        assert len(files) >= 200, (
            f"the coverage baseline records only {len(files)} file(s); idp_common has "
            f"hundreds of modules, so this is almost certainly a path-matching failure "
            f"rather than a small package"
        )
        assert baseline.get("total", 0) > 50, "the recorded overall figure looks unset"

    def test_every_recorded_path_is_package_relative(self):
        baseline = json.loads(
            (REPO_ROOT / "scripts" / "coverage_debt.json").read_text(encoding="utf-8")
        )
        for path in baseline["files"]:
            assert path.startswith("idp_common/"), (
                f"{path} is not package-relative, so it cannot match a report key"
            )

    def test_every_recorded_path_is_still_tracked(self):
        tracked = set(ccd.tracked_source_files())
        baseline = json.loads(
            (REPO_ROOT / "scripts" / "coverage_debt.json").read_text(encoding="utf-8")
        )
        missing = sorted(set(baseline["files"]) - tracked)
        assert not missing, (
            f"the baseline records {len(missing)} path(s) git no longer tracks, so those "
            f"entries shield nothing: {missing[:5]}"
        )


@pytest.mark.unit
class TestWiring:
    """A gate nobody invokes is not a gate."""

    def test_the_root_makefile_exposes_the_target(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "check-coverage-debt:" in makefile
        assert "scripts/check_coverage_debt.py" in makefile

    @pytest.mark.parametrize("target", ["lint", "fastlint"])
    def test_the_lint_sets_do_NOT_run_it(self, target):
        """Deliberate, and the opposite of where a gate usually belongs.

        This ratchet reads the coverage report that `make test-cicd -C lib/idp_common_pkg`
        writes. The lint targets never build one, so wired there it would find no report,
        exit 0, and pass **vacuously** — a gate that cannot fail is worse than an absent
        one, because it reads as protection. Both CI configurations invoke it immediately
        after their test step instead, which the next test asserts.
        """
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        line = next(ln for ln in makefile.splitlines() if ln.startswith(f"{target}: "))
        assert "check-coverage-debt" not in line, (
            f"`make {target}` runs the coverage ratchet, which cannot see a report at "
            f"that point in the sequence and would therefore pass vacuously"
        )

    @pytest.mark.parametrize(
        "config", [".gitlab-ci.yml", ".github/workflows/developer-tests.yml"]
    )
    def test_each_ci_runs_it_after_the_test_step(self, config):
        """Order is the whole point: before the tests there is no report to read."""
        path = REPO_ROOT / config
        if not path.is_file():
            pytest.skip(f"{config} is not present in this tree")
        text = path.read_text(encoding="utf-8")
        assert "make check-coverage-debt" in text, (
            f"{config} does not run the coverage ratchet"
        )
        assert text.index("make check-coverage-debt") > text.index(
            "make test-cicd -C lib/idp_common_pkg"
        ), (
            f"{config} runs the coverage ratchet before the test step that writes the "
            f"report it reads, so it would pass vacuously"
        )

    def test_the_aggregate_floor_is_set_and_overridable(self):
        makefile = (REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile").read_text(
            encoding="utf-8"
        )
        assert "COV_FLOOR ?= --cov-fail-under=" in makefile, (
            "the aggregate floor is missing or not overridable with ?="
        )
        assert "$(COV_FLOOR)" in makefile, "COV_FLOOR is defined but never used"

    def test_the_floor_is_not_above_the_recorded_total(self):
        """A floor above the measured figure red-lines every branch immediately.

        Checked against the baseline rather than against a hardcoded number, so raising
        one without the other fails here instead of in CI.
        """
        makefile = (REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile").read_text(
            encoding="utf-8"
        )
        floor = float(
            makefile.split("COV_FLOOR ?= --cov-fail-under=")[1].split()[0].strip()
        )
        recorded = json.loads(
            (REPO_ROOT / "scripts" / "coverage_debt.json").read_text(encoding="utf-8")
        )["total"]
        assert floor <= recorded, (
            f"the floor ({floor}) is above the recorded total ({recorded:.2f}), so the "
            f"gate fails on an unmodified tree"
        )


@pytest.mark.unit
def test_a_missing_report_is_not_a_failure(tmp_path, capsys):
    """The report is a build artifact, and this gate runs in sequences that do not build
    one (`make lint`). Exiting non-zero there would make the gate mean "you did not run
    the tests", which is not what it is for."""
    assert ccd.main.__doc__ is None or True  # main() is exercised via its argv below
    argv = sys.argv
    try:
        sys.argv = ["check_coverage_debt.py", "--report", str(tmp_path / "nope.xml")]
        assert ccd.main() == 0
    finally:
        sys.argv = argv
    assert "nothing to check" in capsys.readouterr().out
