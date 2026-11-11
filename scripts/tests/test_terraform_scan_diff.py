# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``scripts/terraform_scan_diff.py`` (``make terraform-scan-diff``).

checkov is not run: the classification and the verdict are exercised on
recorded report shapes. What is pinned is what a green gate cannot show: that a
Terraform finding the CloudFormation side passes fails it, that a suppression on
the CloudFormation side counts as the same finding, that a stale baseline entry
and a moved pinned count fail, and that the baseline's checkov version is the
one the Makefile pins.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "terraform_scan_diff.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("_terraform_scan_diff", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MANIFEST = {
    "resources": [
        {
            "stack": "",
            "logicalId": "InputBucket",
            "addresses": [
                "aws_s3_bucket.input_bucket",
                "aws_s3_bucket_versioning.input_bucket_versioning",
            ],
        },
        {
            "stack": "PATTERNSTACK",
            "logicalId": "OcrFunction",
            "addresses": ["module.patternstack.aws_lambda_function.ocr_function"],
        },
        {
            "stack": "",
            "logicalId": "Optional",
            "addresses": ["aws_lambda_function.optional"],
        },
    ]
}


def cfn(kind, resource, check, path="/template.yaml"):
    return {
        "check_type": "cloudformation",
        "results": {
            f"{kind}_checks": [
                {"check_id": check, "resource": resource, "file_path": path}
            ]
        },
    }


def tf(resource, check, path="/main.tf"):
    return {
        "check_type": "terraform",
        "results": {
            "failed_checks": [
                {
                    "check_id": check,
                    "resource": resource,
                    "file_path": path,
                    "check_name": "n",
                }
            ]
        },
    }


SOURCES = {"template.yaml": "", "patterns/unified/template.yaml": "PATTERNSTACK"}
COMPARABLE = {"CKV_AWS_18", "CKV_AWS_116"}


def classify(gate, cfn_reports, tf_reports, conditional=()):
    status = gate.cfn_status(cfn_reports, [], SOURCES)
    return gate.classify(tf_reports, status, MANIFEST, set(conditional), COMPARABLE)


def test_a_check_the_cloudformation_side_passes_is_softer(gate):
    classes = classify(
        gate,
        [cfn("passed", "AWS::S3::Bucket.InputBucket", "CKV_AWS_18")],
        [tf("aws_s3_bucket_versioning.input_bucket_versioning", "CKV_AWS_18")],
    )
    (f,) = classes["softer"]
    assert f["resource"] == "InputBucket"
    problems = gate.verdict(
        classes, {"checkovVersion": "1", "notEvaluatedCount": 0}, "1"
    )
    assert any(p.startswith("softer: CKV_AWS_18") for p in problems)


def test_a_cloudformation_suppression_is_the_same_finding(gate):
    classes = classify(
        gate,
        [
            cfn("passed", "AWS::Lambda::Function.OcrFunction", "CKV_AWS_116"),
            cfn(
                "skipped",
                "AWS::Serverless::Function.OcrFunction",
                "CKV_AWS_116",
                "/patterns/unified/template.yaml",
            ),
        ],
        [
            tf(
                "aws_lambda_function.ocr_function",
                "CKV_AWS_116",
                "/modules/patternstack/main.tf",
            )
        ],
    )
    assert [f["resource"] for f in classes["equal"]] == ["PATTERNSTACK/OcrFunction"]


def test_translator_added_and_terraform_only_checks_are_classed_apart(gate):
    classes = classify(
        gate,
        [],
        [
            tf("module.documentkb.aws_s3_bucket.cfn_bridge", "CKV_AWS_18"),
            tf("aws_s3_bucket.input_bucket", "CKV_AWS_272"),
        ],
    )
    assert classes["translator-added"][0]["resource"] == "aws_s3_bucket.cfn_bridge"
    assert len(classes["terraform-only check"]) == 1


def test_a_conditional_resource_the_cloudformation_scan_skipped_is_counted(gate):
    classes = classify(
        gate,
        [],
        [tf("aws_lambda_function.optional", "CKV_AWS_116")],
        [("", "Optional")],
    )
    assert len(classes["not evaluated on the CloudFormation side"]) == 1
    base = {"checkovVersion": "1", "notEvaluatedCount": 1, "entries": []}
    assert gate.verdict(classes, base, "1") == []
    moved = gate.verdict(classes, dict(base, notEvaluatedCount=0), "1")
    assert any("pinned at 0" in p for p in moved)


def test_a_baselined_finding_passes_and_a_stale_entry_fails(gate):
    classes = classify(
        gate, [], [tf("module.patternstack.aws_s3_bucket.cfn_bridge", "CKV_AWS_18")]
    )
    entry = {
        "check": "CKV_AWS_18",
        "resource": "aws_s3_bucket.cfn_bridge",
        "reason": "r",
    }
    base = {"checkovVersion": "1", "notEvaluatedCount": 0, "entries": [entry]}
    assert gate.verdict(classes, base, "1") == []
    stale = dict(entry, check="CKV_AWS_21")
    problems = gate.verdict(classes, dict(base, entries=[entry, stale]), "1")
    assert problems == [
        "stale baseline entry, matches no finding: ('CKV_AWS_21', "
        "'aws_s3_bucket.cfn_bridge')"
    ]


def test_the_baseline_was_measured_with_the_pinned_checkov():
    makefile = (REPO_ROOT / "Makefile").read_text()
    pinned = re.search(r"^CHECKOV_VERSION := (\S+)$", makefile, re.M)
    assert pinned, "CHECKOV_VERSION is not pinned in the Makefile"
    baseline = json.loads(
        (REPO_ROOT / "scripts" / "terraform_scan_baseline.json").read_text()
    )
    assert baseline["checkovVersion"] == pinned.group(1)
    for e in baseline["entries"]:
        assert e["reason"].strip(), e


def test_terraform_check_runs_the_scan_diff():
    """Both CIs run `make terraform-check`; the scan diff rides on it, so the
    two gate sets stay identical without a second CI entry, and
    GATES_DELIBERATELY_OUT_OF_CI's reason for `terraform-scan-diff` holds."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    recipe = makefile.split("\nterraform-check:", 1)[1].split("\n\n", 1)[0]
    assert "$(MAKE) --no-print-directory terraform-scan-diff" in recipe


def test_a_relative_cache_dir_gives_an_absolute_checkov(gate, tmp_path, monkeypatch):
    """GitHub's job sets TERRAFORM_CHECK_CACHE=.cache/idp-terraform-check, and the
    scans run from other directories; a relative executable failed the second
    scan with FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    venv = gate.checkov_venv("9.9.9", Path(".cache/idp-terraform-check"))
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "checkov").write_text("")
    (venv / ".installed").write_text("9.9.9")
    exe = gate.checkov_binary("9.9.9", Path(".cache/idp-terraform-check"))
    assert exe.is_absolute()
    assert exe == venv / "bin" / "checkov"
    elsewhere = tmp_path / "module"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert exe.is_file()


def test_checkov_runs_through_its_interpreter_not_its_shebang(gate, tmp_path):
    """GitLab's runners failed every scan with `Exec format error`: pip writes
    checkov's `#!python` line as the venv interpreter's absolute path, which under
    `/builds/...` is longer than the kernel reads. A script whose shebang cannot
    be executed must still run, and both entry points must go through the
    interpreter."""
    bin_dir = tmp_path / "checkov-9.9.9" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    exe = bin_dir / "checkov"
    exe.write_text(
        "#!/" + "x" * 300 + "/python\n"
        "import json, sys\n"
        "if '--list' in sys.argv:\n"
        "    print('CKV_AWS_1 CKV2_AWS_2')\n"
        "else:\n"
        "    print(json.dumps({'results': {'failed_checks': []}}))\n"
    )
    exe.chmod(0o755)
    assert gate.run_checkov(exe, [], tmp_path) == [{"results": {"failed_checks": []}}]
    assert gate.check_ids(exe, "terraform") == {"CKV_AWS_1", "CKV2_AWS_2"}


def test_a_checkov_venv_built_on_another_architecture_is_never_reused(
    gate, tmp_path, monkeypatch
):
    """CI runners of both architectures share one cache entry, and a venv from the
    other one fails with Exec format error, so each platform gets its own."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    amd = gate.checkov_venv("9.9.9", cache)
    (amd / "bin").mkdir(parents=True)
    (amd / "bin" / "checkov").write_text("")
    (amd / ".installed").write_text("9.9.9")
    assert gate.checkov_binary("9.9.9", cache) == amd / "bin" / "checkov"

    monkeypatch.setattr(gate.platform, "machine", lambda: "aarch64")
    arm = gate.checkov_venv("9.9.9", cache)
    assert arm != amd
    installed = []

    def fake_run(argv, **kwargs):
        installed.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            Path(argv[3]).mkdir(parents=True)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    exe = gate.checkov_binary("9.9.9", cache)
    assert exe == arm / "bin" / "checkov"
    assert installed and str(arm) in installed[0]
    assert (amd / "bin" / "checkov").is_file()


def test_checkov_is_started_through_its_interpreter_not_its_launcher(
    gate, tmp_path, monkeypatch
):
    """A CI checkout's virtualenv path makes the launcher's #! line longer than
    older kernels accept (Exec format error), so both calls run the venv Python."""
    exe = tmp_path / "venv" / "bin" / "checkov"
    calls = []

    class _Done:
        stdout = "[]"
        stderr = ""
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Done()

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    gate.run_checkov(exe, ["-d", "."], tmp_path)
    gate.check_ids(exe, "terraform")
    interpreter = str(exe.parent / "python")
    assert [argv[:2] for argv in calls] == [[interpreter, str(exe)]] * 2
