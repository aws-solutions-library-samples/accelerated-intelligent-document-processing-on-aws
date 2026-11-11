# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``scripts/terraform_scan_diff.py`` (``make terraform-scan-diff``).

checkov is not run: the classification and the verdict are exercised on
recorded report shapes. What is pinned is what a green gate cannot show: that a
Terraform finding the CloudFormation side passes fails it, that a suppression on
the CloudFormation side counts as the same finding, that a check checkov numbers
differently per framework is still compared, that a stale baseline entry and a
moved pinned count fail, that the Terraform-only ratchet fails on a new check,
a higher count and a lower one and passes at the pin, and that both baselines'
checkov version is the one the Makefile pins.
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
#: Terraform check -> CloudFormation check, as ``counterparts`` derives it.
COUNTERPART = {
    "CKV_AWS_18": "CKV_AWS_18",
    "CKV_AWS_116": "CKV_AWS_116",
    "CKV_AWS_290": "CKV_AWS_111",
}


def classify(gate, cfn_reports, tf_reports, conditional=()):
    status = gate.cfn_status(cfn_reports, [], SOURCES)
    return gate.classify(tf_reports, status, MANIFEST, set(conditional), COUNTERPART)


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
    (only,) = classes["terraform-only check"]
    assert only["resourceType"] == "aws_s3_bucket"


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
    tf_only = json.loads(
        (REPO_ROOT / "scripts" / "terraform_scan_tf_only_baseline.json").read_text()
    )
    assert tf_only["checkovVersion"] == pinned.group(1)
    assert tf_only["checks"]
    for check, e in tf_only["checks"].items():
        assert re.fullmatch(r"CKV2?_AWS_\d+", check), check
        assert e["reason"].strip(), check
        assert e["counts"], check
        for rtype, n in e["counts"].items():
            assert rtype.startswith("aws_") and isinstance(n, int) and n > 0, (
                check,
                rtype,
                n,
            )


def test_the_bridge_gaps_fixed_in_the_translator_are_not_baselined():
    """The bridge's log group takes the stack key and its bucket logs to the
    logging bucket, so neither check may be explained away again."""
    baseline = json.loads(
        (REPO_ROOT / "scripts" / "terraform_scan_baseline.json").read_text()
    )
    keys = {(e["check"], e["resource"]) for e in baseline["entries"]}
    assert ("CKV_AWS_158", "aws_cloudwatch_log_group.cfn_bridge") not in keys
    assert ("CKV_AWS_18", "aws_s3_bucket.cfn_bridge") not in keys


# ------------------------------------------------------- counterpart checks
LIST = """\
|      | Id              | Type     | Entity          | Policy                | IaC       | Link |
|------|-----------------|----------|-----------------|-----------------------|-----------|------|
|    0 | CKV_AWS_111     | resource | AWS::IAM::Role  | Ensure write is ok.   | CFN       | x    |
|    1 | CKV_AWS_111     | resource | AWS::IAM::Policy| Ensure write is ok.   | CFN       | x    |
|    2 | CKV_AWS_18      | resource | AWS::S3::Bucket | Ensure logging        | CFN       | x    |
|    3 | CKV_AZURE_1     | resource | azurerm_thing   | Ensure write is ok.   | CFN       | x    |
"""


def test_the_check_list_is_parsed_per_id(gate):
    assert gate.parse_check_list(LIST) == {
        "CKV_AWS_111": {"Ensure write is ok."},
        "CKV_AWS_18": {"Ensure logging"},
    }


def test_a_check_is_matched_by_id_else_by_identical_policy_text(gate):
    cfn_checks = {
        "CKV_AWS_18": {"Ensure logging"},
        "CKV_AWS_111": {"Ensure IAM policies does not allow write access"},
        "CKV_AWS_7": {"Twin"},
        "CKV_AWS_8": {"twin."},
    }
    tf_checks = {
        "CKV_AWS_18": {"Something else entirely"},
        "CKV_AWS_290": {"ensure IAM policies  does not allow write access."},
        "CKV_AWS_300": {"Twin"},
        "CKV_AWS_272": {"No twin"},
    }
    assert gate.counterparts(cfn_checks, tf_checks) == {
        "CKV_AWS_18": "CKV_AWS_18",
        "CKV_AWS_290": "CKV_AWS_111",
    }


def test_a_differently_numbered_counterpart_is_compared(gate):
    passed = classify(
        gate,
        [cfn("passed", "AWS::S3::Bucket.InputBucket", "CKV_AWS_111")],
        [tf("aws_s3_bucket.input_bucket", "CKV_AWS_290")],
    )
    (f,) = passed["softer"]
    assert f["cfnCheck"] == "CKV_AWS_111"
    skipped = classify(
        gate,
        [cfn("skipped", "AWS::S3::Bucket.InputBucket", "CKV_AWS_111")],
        [tf("aws_s3_bucket.input_bucket", "CKV_AWS_290")],
    )
    assert [f["check"] for f in skipped["equal"]] == ["CKV_AWS_290"]
    assert "terraform-only check" not in skipped


# ------------------------------------------------- Terraform-only ratchet
def tf_only(gate, *findings):
    """Synthetic checkov report -> the Terraform-only findings ``run`` ratchets."""
    reports = [tf(address, check) for address, check in findings]
    return classify(gate, [], reports).get("terraform-only check", [])


TWO_BUCKETS = (
    ("aws_s3_bucket.input_bucket", "CKV_AWS_144"),
    ("module.patternstack.aws_s3_bucket.cfn_bridge", "CKV_AWS_144"),
)


def pinned(counts, reason="replication is the deployer's choice"):
    return {
        "checkovVersion": "1",
        "checks": {"CKV_AWS_144": {"reason": reason, "counts": counts}},
    }


def test_terraform_only_findings_at_their_pin_pass(gate):
    findings = tf_only(gate, *TWO_BUCKETS)
    assert {f["resourceType"] for f in findings} == {"aws_s3_bucket"}
    assert gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}), "1") == []


def test_a_new_terraform_only_check_fails(gate):
    findings = tf_only(
        gate, *TWO_BUCKETS, ("aws_lambda_function.optional", "CKV_AWS_50")
    )
    (problem,) = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}), "1")
    assert problem.startswith("new Terraform-only check CKV_AWS_50")
    assert "{'aws_lambda_function': 1}" in problem


def test_a_higher_count_fails(gate):
    findings = tf_only(gate, *TWO_BUCKETS)
    (problem,) = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 1}), "1")
    assert "fails on 2 aws_s3_bucket, pinned at 1" in problem
    assert "a new finding" in problem


def test_a_new_resource_type_for_a_pinned_check_fails(gate):
    findings = tf_only(
        gate, *TWO_BUCKETS, ("aws_lambda_function.optional", "CKV_AWS_144")
    )
    (problem,) = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}), "1")
    assert "now fails on 1 aws_lambda_function" in problem


def test_a_lower_count_fails_as_stale_and_names_the_new_pin(gate):
    findings = tf_only(gate, TWO_BUCKETS[0])
    (problem,) = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}), "1")
    assert problem.startswith("stale: Terraform-only CKV_AWS_144")
    assert "Lower the pin to 1" in problem


def test_a_pinned_pair_that_no_longer_occurs_is_stale(gate):
    findings = tf_only(gate, *TWO_BUCKETS)
    base = pinned({"aws_s3_bucket": 2, "aws_s3_bucket_policy": 3})
    (problem,) = gate.tf_only_verdict(findings, base, "1")
    assert "fails on 0 aws_s3_bucket_policy, pinned at 3" in problem
    assert "(remove the pair)" in problem


def test_an_entry_without_a_reason_or_from_another_checkov_fails(gate):
    findings = tf_only(gate, *TWO_BUCKETS)
    no_reason = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}, " "), "1")
    assert no_reason == [
        "terraform_scan_tf_only_baseline.json: CKV_AWS_144 has no reason"
    ]
    other = gate.tf_only_verdict(findings, pinned({"aws_s3_bucket": 2}), "2")
    assert len(other) == 1 and "re-measure" in other[0]


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
