# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GovCloud-safety guards for the NESTED unified pattern template.

The `--govcloud` transform (and its cfn-lint gate) only ever ran against the
MAIN template — `patterns/unified/template.yaml` is uploaded verbatim as a
nested stack and was never checked. Issue #676 is exactly what that blind spot
allowed through: `BDAOCRProject` was created unconditionally, GovCloud refused
the project shape with

    ValidationException: Sync project does not support video/audio/document
    modality in Standard Output Configuration

and `PATTERNSTACK` failed, rolling the whole root stack back — regardless of
deploy mode and regardless of the configured `ocr.backend`.

These tests need no AWS credentials: the partition gate is a structural
assertion on the committed template, and cfn-lint's region check is offline.
"""

from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.unit

PATTERN_TEMPLATE = Path("patterns/unified/template.yaml")
BDA_OCR_RESOURCES = (
    "BDAOCRProject",
    "BDAOCRProjectFunction",
    "BDAOCRProjectFunctionLogGroup",
)
GATE_CONDITION = "ShouldCreateBDAOCRProject"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


def _plain(node):
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(x) for x in node]
    if isinstance(node, str):
        return str(node)
    return node


def _load_pattern_template() -> Dict[str, Any]:
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")
    loaded = cfnlint_decode.load(str(_repo_root() / PATTERN_TEMPLATE))
    template = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)
    assert isinstance(template, dict) and "Resources" in template
    return template


def test_bda_ocr_project_is_gated_on_the_commercial_partition():
    """The gate must be partition-driven so no GovCloud deploy can reach it."""
    template = _load_pattern_template()
    condition = template.get("Conditions", {}).get(GATE_CONDITION)
    assert condition is not None, (
        f"{GATE_CONDITION} is missing — without it BDAOCRProject is created "
        "unconditionally and every GovCloud deployment rolls back (issue #676)."
    )
    # Fn::Equals[AWS::Partition, "aws"]: true only in the commercial partition.
    equals = condition.get("Fn::Equals")
    assert equals is not None, f"{GATE_CONDITION} must be an Fn::Equals"
    assert {"Ref": "AWS::Partition"} in equals
    assert "aws" in equals


def test_every_bda_ocr_resource_carries_the_gate():
    template = _load_pattern_template()
    resources = template["Resources"]
    for name in BDA_OCR_RESOURCES:
        assert name in resources, f"{name} missing from the pattern template"
        assert resources[name].get("Condition") == GATE_CONDITION, (
            f"{name} is not gated on {GATE_CONDITION}; it would be created in "
            "GovCloud and fail the nested stack."
        )


def test_bda_project_arn_env_var_is_conditional():
    """BDA_OCR_PROJECT_ARN must not GetAtt the gated resource unconditionally.

    A bare `!GetAtt BDAOCRProject.ProjectArn` on a conditional resource is an
    unresolvable reference when the condition is false, so the env var has to sit
    behind the same condition and fall back to "" — which the OCR service already
    handles (the `bda` backend then errors clearly instead of the stack failing).
    """
    template = _load_pattern_template()
    env = template["Resources"]["OCRFunction"]["Properties"]["Environment"]["Variables"]
    arn = env["BDA_OCR_PROJECT_ARN"]
    assert isinstance(arn, dict) and "Fn::If" in arn, (
        "BDA_OCR_PROJECT_ARN must be an Fn::If on the gate condition, not a bare "
        "GetAtt on a conditional resource."
    )
    branches = arn["Fn::If"]
    assert branches[0] == GATE_CONDITION
    assert branches[1] == {"Fn::GetAtt": ["BDAOCRProject", "ProjectArn"]}
    assert branches[2] == ""


def test_no_other_reference_to_the_gated_resources():
    """Any ungated reference to a conditional resource is a deploy-time error.

    Scans every Resource (including OCRFunction, with only its one asserted
    Fn::If env var removed) plus Outputs — an Output referencing a resource whose
    condition is false is just as fatal as a Resource reference, and excluding
    OCRFunction wholesale would hide a SECOND ungated GetAtt added next to the
    conditional one.
    """
    import copy

    import yaml

    template = _load_pattern_template()
    resources = copy.deepcopy(template["Resources"])

    # Drop the one legitimate, condition-guarded reference so everything else in
    # OCRFunction is still scanned.
    ocr_env = resources["OCRFunction"]["Properties"]["Environment"]["Variables"]
    removed = ocr_env.pop("BDA_OCR_PROJECT_ARN", None)
    assert removed is not None, (
        "OCRFunction no longer has a BDA_OCR_PROJECT_ARN env var — this guard "
        "assumed it does; re-check what replaced it."
    )

    scanned = {
        name: res for name, res in resources.items() if name not in BDA_OCR_RESOURCES
    }
    blob = yaml.dump(
        {"Resources": scanned, "Outputs": template.get("Outputs", {})},
        default_flow_style=False,
    )
    for name in BDA_OCR_RESOURCES:
        assert name not in blob, (
            f"{name} is referenced from an ungated resource or an Output; that "
            "reference breaks when the partition gate is false."
        )


def test_pattern_template_passes_govcloud_region_cfn_lint():
    """Run REAL cfn-lint against the nested pattern template for a GovCloud region.

    The existing GovCloud lint gate only covers the transformed MAIN template, so
    a GovCloud-unsupported resource type introduced in the pattern stack (E3006)
    was previously caught by nothing before deploy. Offline — no credentials.
    Skips cleanly if cfn-lint is not installed.
    """
    import json
    import shutil
    import subprocess  # nosec B404 - fixed args, no user input

    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")

    path = str(_repo_root() / PATTERN_TEMPLATE)
    proc = subprocess.run(  # nosec B603 - fixed executable + args
        ["cfn-lint", path, "--region", "us-gov-west-1", "--format", "json"],
        capture_output=True,
        text=True,
    )
    try:
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        findings = []
    e3006 = [f for f in findings if f.get("Rule", {}).get("Id") == "E3006"]
    assert e3006 == [], (
        "GovCloud-unsupported resource type(s) in the unified pattern template "
        "(cfn-lint E3006): "
        + "; ".join(
            f"{f.get('Location', {}).get('Path')}: {f.get('Message')}" for f in e3006
        )
    )
