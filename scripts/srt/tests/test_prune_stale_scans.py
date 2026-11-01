"""Tests for scripts/srt/prune_stale_scans.py, the second half of `make srt-clean`.

`make srt-clean` deletes every `.aws-sam` build tree and then prunes the `.srt/`
scan dirs whose template has gone. A scan dir it keeps but cannot refresh is
reported by `failed_checkov_scans` as a crashed checkov run, and with no
resolvable path that report is CI-visible, so the next scan under `CI=1` ends
INCOMPLETE. These tests pin which scan dirs are pruned and which are kept.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

from prune_stale_scans import scan_dir_name, stale_scan_dirs  # noqa: E402
from scanner_health import failed_checkov_scans  # noqa: E402

pytestmark = pytest.mark.unit


def _scan_dir(srt, name, records):
    """A per-template scan dir as `srt assess` leaves it: a matrix, no fresh summary."""
    d = srt / name
    d.mkdir(parents=True)
    (d / "security-matrix.json").write_text(json.dumps(records), encoding="utf-8")
    return d


def _touch(root, rel):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("AWSTemplateFormatVersion: '2010-09-09'\n", encoding="utf-8")


def _names(stale):
    return sorted(d.name for d, _ in stale)


class TestEmptyMatrixBuildArtifacts:
    """SRT writes `[]` for a clean template, so the path cannot be read back."""

    def test_deleted_artifact_with_empty_matrix_is_pruned(self, tmp_path):
        srt = tmp_path / ".srt"
        _scan_dir(srt, "nested-x-.aws-sam-packaged", [])
        _scan_dir(srt, ".aws-sam-idp-main", [])

        stale = stale_scan_dirs(srt, tmp_path)

        assert _names(stale) == [".aws-sam-idp-main", "nested-x-.aws-sam-packaged"]
        assert all(path is None for _, path in stale)

    def test_artifact_still_present_is_kept(self, tmp_path):
        srt = tmp_path / ".srt"
        _scan_dir(srt, "nested-x-.aws-sam-packaged", [])
        _scan_dir(srt, ".aws-sam-idp-main", [])
        _touch(tmp_path, "nested/x/.aws-sam/packaged.yaml")

        assert _names(stale_scan_dirs(srt, tmp_path)) == [".aws-sam-idp-main"]

    def test_a_same_named_template_outside_a_build_tree_does_not_keep_it(
        self, tmp_path
    ):
        # Only a template under `.aws-sam` can be what an `.aws-sam` name was given
        # to, so a source template elsewhere must not count as the live artifact.
        srt = tmp_path / ".srt"
        _scan_dir(srt, "nested-x-.aws-sam-packaged", [])
        _touch(tmp_path, "nested/x/packaged.yaml")

        assert _names(stale_scan_dirs(srt, tmp_path)) == ["nested-x-.aws-sam-packaged"]

    def test_unresolvable_source_template_dir_is_still_left_alone(self, tmp_path):
        # Fail closed for anything that is not named after a build artifact.
        srt = tmp_path / ".srt"
        _scan_dir(srt, "samples-gone-template", [])
        _scan_dir(srt, "nested-aws-sam-lookalike-template", [])

        assert stale_scan_dirs(srt, tmp_path) == []

    def test_the_incomplete_report_is_gone_after_pruning(self, tmp_path):
        # The end-to-end property: before the prune, the empty-matrix artifact dir
        # is reported as a checkov failure with no path, which the scan treats as
        # CI-visible; after it, nothing is reported.
        srt = tmp_path / ".srt"
        _scan_dir(srt, "nested-x-.aws-sam-packaged", [])
        assert failed_checkov_scans(srt) == [("nested-x-.aws-sam-packaged", None)]

        for scan_dir, _ in stale_scan_dirs(srt, tmp_path):
            shutil.rmtree(scan_dir)

        assert failed_checkov_scans(srt) == []


class TestResolvedPaths:
    def test_missing_path_is_pruned_and_present_path_kept(self, tmp_path):
        srt = tmp_path / ".srt"
        _scan_dir(srt, "nested-gone-template", [{"path": "nested/gone/template.yaml"}])
        _scan_dir(srt, "nested-here-template", [{"path": "nested/here/template.yaml"}])
        _touch(tmp_path, "nested/here/template.yaml")

        stale = stale_scan_dirs(srt, tmp_path)

        assert [(d.name, p) for d, p in stale] == [
            ("nested-gone-template", "nested/gone/template.yaml")
        ]

    def test_a_resolved_build_artifact_is_judged_by_its_path_once(self, tmp_path):
        # A scan dir that has findings records its path, so the name rule must not
        # also run for it: reported once, with the path it recorded.
        srt = tmp_path / ".srt"
        _scan_dir(srt, ".aws-sam-idp-main", [{"path": ".aws-sam/idp-main.yaml"}])

        stale = stale_scan_dirs(srt, tmp_path)

        assert [(d.name, p) for d, p in stale] == [
            (".aws-sam-idp-main", ".aws-sam/idp-main.yaml")
        ]


def test_scan_dir_name_matches_the_names_srt_recorded():
    """The forward mapping, checked against what SRT itself wrote down.

    `scripts/srt/issues.json` carries both the scan dir (`stack`) and the scanned
    `path` for each finding, so it is the committed record of SRT's naming rule.
    """
    issues = json.loads((SRT_DIR / "issues.json").read_text(encoding="utf-8"))
    pairs = {
        (entry["stack"], entry["path"])
        for entry in issues
        if entry.get("stack") and entry.get("path")
    }
    assert pairs, "issues.json no longer records any (stack, path) pair to check"
    for stack, path in pairs:
        assert scan_dir_name(path) == stack, (stack, path)
