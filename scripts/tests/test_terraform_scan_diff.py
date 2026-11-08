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
    venv = Path(".cache/idp-terraform-check/checkov-9.9.9")
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "checkov").write_text("")
    (venv / ".installed").write_text("9.9.9")
    exe = gate.checkov_binary("9.9.9", Path(".cache/idp-terraform-check"))
    assert exe.is_absolute()
    assert exe == (tmp_path / venv / "bin" / "checkov")
    elsewhere = tmp_path / "module"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert exe.is_file()
