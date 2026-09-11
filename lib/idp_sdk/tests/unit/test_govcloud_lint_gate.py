# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the region-aware cfn-lint gate in the GovCloud publish path.

`transform_template_govcloud` runs `cfn-lint <file> --region <govcloud-region>`
after transforming, so that any GovCloud-unsupported resource type (E3006) that
survives the transform fails the publish/deploy loudly rather than only at
deploy time. These tests mock the subprocess so they need neither cfn-lint nor
AWS credentials.
"""

import subprocess
import types

import pytest

import idp_sdk.operations.publish as pub
from idp_sdk.operations.publish import (
    DEFAULT_GOVCLOUD_LINT_REGION,
    PublishOperation,
    _cfn_lint_region_errors,
)

pytestmark = pytest.mark.unit


def _fake_run(stdout="", returncode=0):
    def _run(cmd, capture_output=True, text=True):  # noqa: ARG001
        _run.last_cmd = cmd
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    _run.last_cmd = None
    return _run


def test_default_lint_region_is_govcloud():
    assert DEFAULT_GOVCLOUD_LINT_REGION.startswith("us-gov-")


def test_cfn_lint_arg_order_is_file_then_region(monkeypatch):
    """Regression guard: the region flag MUST follow the file path.

    `cfn-lint --region <r> <file>` makes cfn-lint consume the filename as a
    region and silently lint nothing — the original false-pass bug.
    """
    fake = _fake_run(stdout="", returncode=0)
    monkeypatch.setattr(pub.shutil, "which", lambda _: "/usr/bin/cfn-lint")
    monkeypatch.setattr(pub.subprocess, "run", fake)

    _cfn_lint_region_errors("/tmp/t.yaml", "us-gov-west-1")  # nosec B108 - test fixture path, cfn-lint is mocked (no file I/O)

    cmd = fake.last_cmd
    assert cmd[0] == "cfn-lint"
    # File comes before --region.
    assert cmd.index("/tmp/t.yaml") < cmd.index("--region")  # nosec B108 - test fixture path, cfn-lint is mocked (no file I/O)
    assert cmd[cmd.index("--region") + 1] == "us-gov-west-1"


def test_cfn_lint_parses_only_e_codes(monkeypatch):
    out = (
        "E3006 Resource type 'AWS::Lambda::Url' does not exist in 'us-gov-west-1'\n"
        "W1028 something unreachable\n"
        "E1001 another error\n"
    )
    monkeypatch.setattr(pub.shutil, "which", lambda _: "/usr/bin/cfn-lint")
    monkeypatch.setattr(pub.subprocess, "run", _fake_run(stdout=out, returncode=4))

    ran, errors, _ = _cfn_lint_region_errors("/tmp/t.yaml", "us-gov-west-1")  # nosec B108 - test fixture path, cfn-lint is mocked (no file I/O)
    assert ran is True
    assert len(errors) == 2  # the two E-codes, not the W-code
    assert all(e.startswith("E") for e in errors)


def test_cfn_lint_skips_gracefully_when_not_installed(monkeypatch):
    monkeypatch.setattr(pub.shutil, "which", lambda _: None)
    ran, errors, note = _cfn_lint_region_errors("/tmp/t.yaml", "us-gov-west-1")  # nosec B108 - test fixture path, cfn-lint is mocked (no file I/O)
    assert ran is False
    assert errors == []
    assert "not installed" in note


def _patch_transformer(monkeypatch, *, transform_ok=True):
    """Make GovCloudTemplateTransformer.transform a no-op returning transform_ok."""
    import idp_sdk._core.template_transform as tt

    def _fake_transform(self, src, out):  # noqa: ARG001
        # Pretend it wrote a file (the gate mocks cfn-lint, so contents unused).
        return transform_ok

    monkeypatch.setattr(tt.GovCloudTemplateTransformer, "transform", _fake_transform)


def test_transform_fails_when_lint_finds_errors(monkeypatch, tmp_path):
    src = tmp_path / "idp-main.yaml"
    src.write_text("Resources: {}\n")
    _patch_transformer(monkeypatch)
    monkeypatch.setattr(pub.shutil, "which", lambda _: "/usr/bin/cfn-lint")
    monkeypatch.setattr(
        pub.subprocess,
        "run",
        _fake_run(
            stdout="E3006 Resource type 'AWS::Lambda::Url' does not exist in 'us-gov-west-1'\n",
            returncode=4,
        ),
    )

    op = PublishOperation(types.SimpleNamespace(_region="us-gov-west-1"))
    result = op.transform_template_govcloud(
        str(src), str(tmp_path / "out.yaml"), lint_region="us-gov-west-1"
    )
    assert result.success is False
    assert result.error is not None
    assert "cfn-lint errors" in result.error
    assert "AWS::Lambda::Url" in result.error


def test_transform_succeeds_when_lint_clean(monkeypatch, tmp_path):
    src = tmp_path / "idp-main.yaml"
    src.write_text("Resources: {}\n")
    _patch_transformer(monkeypatch)
    monkeypatch.setattr(pub.shutil, "which", lambda _: "/usr/bin/cfn-lint")
    monkeypatch.setattr(pub.subprocess, "run", _fake_run(stdout="", returncode=0))

    op = PublishOperation(types.SimpleNamespace(_region="us-gov-west-1"))
    result = op.transform_template_govcloud(
        str(src), str(tmp_path / "out.yaml"), lint_region="us-gov-west-1"
    )
    assert result.success is True
    assert result.error is None


def test_transform_succeeds_when_cfn_lint_missing(monkeypatch, tmp_path):
    """A missing cfn-lint must not block publish (graceful skip)."""
    src = tmp_path / "idp-main.yaml"
    src.write_text("Resources: {}\n")
    _patch_transformer(monkeypatch)
    monkeypatch.setattr(pub.shutil, "which", lambda _: None)

    op = PublishOperation(types.SimpleNamespace(_region="us-gov-west-1"))
    result = op.transform_template_govcloud(
        str(src), str(tmp_path / "out.yaml"), lint_region="us-gov-west-1"
    )
    assert result.success is True


def test_lint_gate_disabled_when_region_none(monkeypatch, tmp_path):
    """lint_region=None skips the gate entirely (cfn-lint never invoked)."""
    src = tmp_path / "idp-main.yaml"
    src.write_text("Resources: {}\n")
    _patch_transformer(monkeypatch)

    called = {"run": False}

    def _boom(*a, **k):  # noqa: ARG001
        called["run"] = True
        raise AssertionError("cfn-lint should not run when lint_region is None")

    monkeypatch.setattr(pub.subprocess, "run", _boom)
    monkeypatch.setattr(pub.shutil, "which", lambda _: "/usr/bin/cfn-lint")

    op = PublishOperation(types.SimpleNamespace(_region="us-gov-west-1"))
    result = op.transform_template_govcloud(
        str(src), str(tmp_path / "out.yaml"), lint_region=None
    )
    assert result.success is True
    assert called["run"] is False
