"""Tests for scripts/srt/scanner_health.py.

`srt assess` catches a scanner crash, logs it, and carries on with an empty
result — so a scanner that never ran looks identical to a clean scan in the
findings table. These tests cover the on-disk signal used to tell the two apart.
"""

import json
import sys
import time
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRT_DIR))

from scanner_health import (  # noqa: E402
    WHOLE_REPO_SCANNERS,
    failed_checkov_scans,
    missing_whole_repo_scanners,
)

pytestmark = pytest.mark.unit


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _scan_dir(srt, name, scanned_path, with_checkov_summary=True):
    """Create a per-template scan dir the way `srt assess` does."""
    d = srt / name
    _write(d / "security-matrix.json", [{"path": scanned_path, "check_id": "DDB-002"}])
    if with_checkov_summary:
        _write(d / "checkov-summary.json", [])
    return d


@pytest.fixture
def srt(tmp_path):
    """An .srt dir with every scanner having completed successfully."""
    root = tmp_path / ".srt"
    for filename in WHOLE_REPO_SCANNERS.values():
        _write(root / filename, [])
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


class TestWholeRepoScanners:
    def test_all_present_reports_nothing(self, srt):
        assert missing_whole_repo_scanners(srt) == []

    def test_missing_summary_is_reported(self, srt):
        (srt / "semgrep-summary.json").unlink()
        assert missing_whole_repo_scanners(srt) == ["semgrep"]

    def test_empty_list_summary_still_counts_as_success(self, srt):
        # SRT writes [] for a clean scan, so presence — not content — is the
        # signal. A zero-finding scanner must not be reported as crashed.
        _write(srt / "bandit-summary.json", [])
        assert missing_whole_repo_scanners(srt) == []

    def test_stale_summary_counts_as_missing(self, srt):
        """A previous run's output must not mask this run's crash."""
        stale = srt / "syft-summary.json"
        old = time.time() - 3600
        import os

        os.utime(stale, (old, old))
        assert missing_whole_repo_scanners(srt, since=time.time() - 60) == ["syft"]

    def test_absent_srt_dir_reports_everything(self, tmp_path):
        assert missing_whole_repo_scanners(tmp_path / "nope") == sorted(
            WHOLE_REPO_SCANNERS
        )


class TestCheckovScans:
    def test_completed_scans_report_nothing(self, srt):
        _scan_dir(srt, "template", "template.yaml")
        _scan_dir(srt, "nested-api-resolvers-template", "nested/api-resolvers/template.yaml")
        assert failed_checkov_scans(srt) == []

    def test_missing_summary_is_reported_with_resolved_path(self, srt):
        _scan_dir(srt, "template", "template.yaml")
        _scan_dir(
            srt,
            ".aws-sam-idp-main",
            ".aws-sam/idp-main.yaml",
            with_checkov_summary=False,
        )
        assert failed_checkov_scans(srt) == [
            (".aws-sam-idp-main", ".aws-sam/idp-main.yaml")
        ]

    def test_path_resolution_survives_unreadable_matrix(self, srt):
        """The dir name is not reversibly parseable, so path may be None."""
        d = srt / "some-template"
        d.mkdir(parents=True)
        (d / "security-matrix.json").write_text("{not json", encoding="utf-8")
        assert failed_checkov_scans(srt) == [("some-template", None)]

    def test_non_scan_dirs_ignored(self, srt):
        # logs/ and .venv/ have no security-matrix.json and must not be mistaken
        # for a crashed template scan.
        (srt / ".venv" / "bin").mkdir(parents=True)
        assert failed_checkov_scans(srt) == []

    def test_stale_summary_counts_as_missing(self, srt):
        import os

        d = _scan_dir(srt, "template", "template.yaml")
        old = time.time() - 3600
        os.utime(d / "checkov-summary.json", (old, old))
        assert failed_checkov_scans(srt, since=time.time() - 60) == [
            ("template", "template.yaml")
        ]

    def test_absent_srt_dir_is_not_an_error(self, tmp_path):
        assert failed_checkov_scans(tmp_path / "nope") == []
