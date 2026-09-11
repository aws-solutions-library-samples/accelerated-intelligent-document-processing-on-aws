"""Tests for scripts/srt/ci_paths.py and the committed SRT suppression baseline.

The baseline test is the durable guard: it fails if a finding on a gitignored or
deleted path gets committed into scripts/srt/issues.json again. That pollution
is not harmless — SRT keys suppressions on (path, resourceType, resourceName,
check_id), so an artifact-path entry can never cover the real source template,
and one left at "resolved" re-detects as "reopened", which DOES gate CI.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

from ci_paths import (  # noqa: E402
    _normalize,
    is_gating_status,
    is_in_ci_checkout,
    partition_by_ci_visibility,
    tracked_files,
)

pytestmark = pytest.mark.unit


class TestNormalize:
    def test_plain_relative_path_unchanged(self):
        assert _normalize("nested/api-resolvers/template.yaml") == (
            "nested/api-resolvers/template.yaml"
        )

    def test_strips_dot_slash_prefix(self):
        assert _normalize("./template.yaml") == "template.yaml"

    def test_converts_backslashes(self):
        assert _normalize("nested\\bedrockkb\\template.yaml") == (
            "nested/bedrockkb/template.yaml"
        )

    def test_empty_and_none(self):
        assert _normalize("") is None
        assert _normalize(None) is None


class TestIsInCiCheckout:
    TRACKED = {"template.yaml", "nested/api-resolvers/template.yaml"}

    def test_tracked_path_is_visible(self):
        assert is_in_ci_checkout("template.yaml", self.TRACKED) is True

    def test_build_artifact_is_not_visible(self):
        assert is_in_ci_checkout(".aws-sam/idp-main.yaml", self.TRACKED) is False
        assert (
            is_in_ci_checkout(
                "nested/api-resolvers/.aws-sam/packaged.yaml", self.TRACKED
            )
            is False
        )

    def test_fails_closed_when_tracked_set_unknown(self):
        # If we could not ask git, everything must still gate — never silently
        # drop a finding we failed to classify.
        assert is_in_ci_checkout(".aws-sam/idp-main.yaml", None) is True

    def test_pathless_finding_gates(self):
        assert is_in_ci_checkout(None, self.TRACKED) is True
        assert is_in_ci_checkout("", self.TRACKED) is True


class TestTrackedFiles:
    def test_lists_this_repo(self):
        tracked = tracked_files(PROJECT_ROOT)
        assert tracked is not None
        assert "template.yaml" in tracked
        # Build output is gitignored, so it must never appear.
        assert not any(".aws-sam/" in p for p in tracked)

    def test_returns_none_outside_a_git_repo(self, capsys):
        with tempfile.TemporaryDirectory() as tmp:
            # Isolate from any enclosing repo (e.g. /tmp inside a checkout).
            subprocess.run(
                ["git", "init", "-q", tmp], check=True, capture_output=True
            )
            Path(tmp, ".git").rename(Path(tmp, "not-git"))
            assert tracked_files(tmp) is None
        assert "Warning" in capsys.readouterr().out


class TestGatingStatus:
    def test_open_and_reopened_gate(self):
        assert is_gating_status("Open") is True
        # Regression guard for the 0.6.5 escape: SRT flips a re-detected
        # 'resolved' finding to 'reopened', which must still fail the build.
        # 'resolved' is not a sticky disposition; only 'suppressed' is.
        assert is_gating_status("reopened") is True

    def test_dispositioned_statuses_do_not_gate(self):
        assert is_gating_status("suppressed") is False
        assert is_gating_status("resolved") is False

    def test_missing_status_does_not_gate(self):
        assert is_gating_status(None) is False
        assert is_gating_status("") is False


class TestPartition:
    def test_splits_on_trackedness(self):
        issues = [
            {"check_id": "DDB-002", "path": "template.yaml"},
            {"check_id": "DDB-002", "path": ".aws-sam/idp-main.yaml"},
        ]
        ci_visible, local_only = partition_by_ci_visibility(issues, PROJECT_ROOT)
        assert [i["path"] for i in ci_visible] == ["template.yaml"]
        assert [i["path"] for i in local_only] == [".aws-sam/idp-main.yaml"]


class TestCommittedBaseline:
    """scripts/srt/issues.json must only carry findings CI can actually see."""

    @staticmethod
    def _baseline():
        with open(SRT_DIR / "issues.json", encoding="utf-8") as f:
            return json.load(f)

    def test_no_entries_on_untracked_paths(self):
        _, local_only = partition_by_ci_visibility(self._baseline(), PROJECT_ROOT)
        offenders = sorted(
            {
                f"{i.get('path')} ({i.get('check_id')} {i.get('resourceName')})"
                for i in local_only
            }
        )
        assert not offenders, (
            "scripts/srt/issues.json has entries on gitignored or deleted paths. "
            "These can never match a real finding in CI (the suppression key "
            "includes the path) and a stale 'resolved' one re-detects as "
            "'reopened', which gates the build. Remove them; run 'make srt-clean' "
            "before scanning locally.\n  " + "\n  ".join(offenders)
        )

    def test_suppressed_entries_carry_a_reason(self):
        offenders = [
            f"{i.get('check_id')} {i.get('resourceName')} in {i.get('path')}"
            for i in self._baseline()
            if (i.get("status") or "").lower() == "suppressed"
            and not (i.get("suppressionReason") or "").strip()
        ]
        assert not offenders, (
            "Every suppressed finding needs a suppressionReason so reviewers can "
            "audit the accepted risk:\n  " + "\n  ".join(offenders)
        )


class TestAwsProfileBootstrap:
    """`srt config` needs a profile to exist, so setup.py has to be able to make one.

    The scan is static analysis and needs no credentials, but `srt config` — the
    command that installs the five scanners — enumerates profiles and aborts with
    "✗ No AWS profiles found!" when there are none. setup.py used to create one only
    via `aws configure`, i.e. only when the image happened to ship awscli. It does on
    the GitLab runner and does not in python:3.13-bookworm, so the GitHub job failed
    at setup with every scanner missing. These tests pin the CLI-less path, and that
    it can never clobber a developer's real config.
    """

    @staticmethod
    def _setup_module():
        import importlib.util

        path = PROJECT_ROOT / "scripts" / "srt" / "setup.py"
        spec = importlib.util.spec_from_file_location("srt_setup", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_writes_a_default_profile_when_none_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("default", "us-east-1")
        config = tmp_path / ".aws" / "config"
        assert config.exists()
        # `default` is spelled [default]; anything else is [profile name].
        assert "[default]" in config.read_text()
        assert "region = us-east-1" in config.read_text()

    def test_named_profile_uses_the_profile_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("srtci", "eu-west-1")
        assert "[profile srtci]" in (tmp_path / ".aws" / "config").read_text()

    def test_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = self._setup_module()
        mod.write_aws_profile("default", "us-east-1")
        mod.write_aws_profile("default", "us-west-2")
        text = (tmp_path / ".aws" / "config").read_text()
        assert text.count("[default]") == 1
        # The second call must not rewrite the region either.
        assert "us-east-1" in text and "us-west-2" not in text

    def test_never_clobbers_an_existing_config(self, tmp_path, monkeypatch):
        """A developer's real credentials/settings must survive untouched."""
        monkeypatch.setenv("HOME", str(tmp_path))
        aws = tmp_path / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile mine]\nregion = ap-southeast-2\n")
        self._setup_module().write_aws_profile("default", "us-east-1")
        text = (aws / "config").read_text()
        assert "[profile mine]" in text and "ap-southeast-2" in text
        assert "[default]" in text

    def test_file_is_not_world_readable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("default", "us-east-1")
        mode = (tmp_path / ".aws" / "config").stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
