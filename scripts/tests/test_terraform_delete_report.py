# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`idp-cli delete --terraform` lists the Lambda log groups the destroy leaves
behind, judged against the module the publish builds; it also pins this
module's CodeBuild and Glue results.

`terraform destroy` removes only what is in the state. Lambda, CodeBuild and Glue
create log groups of their own when a function, build or crawler first runs, and
those are left behind by a Terraform destroy just as by a CloudFormation delete.
The SDK derives them from the state (`terraform_deploy.service_log_groups`), so
this test translates `template.yaml` offline, builds a `terraform show -json`
state from every resource the module declares, and compares what the SDK reports
with what the CloudFormation templates say: a function whose definition declares
no `LoggingConfig.LogGroup` logs to the group Lambda creates.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSLATOR = REPO_ROOT / "terraform" / "translator"

_RESOURCE = re.compile(r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', re.M | re.S)
_LOG_GROUP_REF = re.compile(r"aws_cloudwatch_log_group\.(\w+)")
_CRAWLER_CONFIG_REF = re.compile(
    r"^\s*crawler_security_configuration = awscc_glue_security_configuration\.(\w+)",
    re.M,
)


@pytest.fixture(scope="module")
def module_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("module") / "m"
    subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "idp_tf.cli",
            "../../template.yaml",
            "--offline",
            "--out",
            str(out),
        ],
        cwd=TRANSLATOR,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return out


def _modules(root: Path) -> list[Path]:
    return [root, *sorted(p for p in (root / "modules").glob("*") if p.is_dir())]


def _cfn_functions_without_a_log_group() -> set[str]:
    """Terraform labels of the Lambda functions whose CloudFormation definition
    declares no ``LoggingConfig.LogGroup``, across the templates the module
    is translated from."""
    if str(TRANSLATOR) not in sys.path:
        sys.path.insert(0, str(TRANSLATOR))
    from idp_tf.expand import expand, load_template, offline_placeholders
    from idp_tf.hcl import snake

    templates = [
        REPO_ROOT / "template.yaml",
        REPO_ROOT / "patterns" / "unified" / "template.yaml",
        REPO_ROOT / "feature-platform" / "main-stack-extensions" / "template.yaml",
        *sorted((REPO_ROOT / "nested").glob("*/template.yaml")),
    ]
    labels = set()
    for path in templates:
        resources = expand(offline_placeholders(load_template(path)))["Resources"]
        for lid, res in resources.items():
            if res.get("Type") != "AWS::Lambda::Function":
                continue
            logging = (res.get("Properties") or {}).get("LoggingConfig") or {}
            if not logging.get("LogGroup"):
                labels.add(snake(lid))
    return labels


def _values(module: str, kind: str, label: str, body: str) -> dict | None:
    """State values for one declared resource, names unique per module and each
    log-group reference resolved to the name given to the referenced group."""
    where = f"{module}.{kind}.{label}"

    def group(attribute: str) -> list[str]:
        line = re.search(rf"^\s*{attribute}\s*=\s*(.*)$", body, re.M)
        if not line:
            return []
        ref = _LOG_GROUP_REF.search(line.group(1))
        assert ref, f"{where}: {line.group(0).strip()} is not a log group reference"
        return [f"/declared/{module}/{ref.group(1)}"]

    if kind == "aws_cloudwatch_log_group":
        return {"name": f"/declared/{module}/{label}"}
    if kind == "aws_lambda_function":
        return {
            "function_name": f"{module}-{label}",
            "logging_config": [{"log_group": g} for g in group("log_group")],
        }
    if kind == "aws_codebuild_project":
        return {
            "name": f"{module}-{label}",
            "logs_config": [
                {"cloudwatch_logs": [{"status": "ENABLED", "group_name": g}]}
                for g in group("group_name")
            ],
        }
    if kind == "awscc_glue_security_configuration":
        encrypted = "cloudwatch_encryption" in body
        return {
            "name": f"{module}-{label}",
            "encryption_configuration": {
                "cloudwatch_encryption": (
                    {"cloudwatch_encryption_mode": "SSE-KMS"} if encrypted else None
                )
            },
        }
    if kind == "awscc_glue_crawler":
        config = _CRAWLER_CONFIG_REF.search(body)
        return {
            "name": f"{module}-{label}",
            "role": f"{module}-role",
            "crawler_security_configuration": (
                f"{module}-{config.group(1)}" if config else None
            ),
        }
    return None


def _synthetic_state(root: Path) -> dict:
    resources = []
    for module in _modules(root):
        text = "\n".join(p.read_text() for p in sorted(module.glob("*.tf")))
        for kind, label, body in _RESOURCE.findall(text):
            values = _values(module.name, kind, label, body)
            if values is not None:
                resources.append(
                    {
                        "address": f"module.{module.name}.{kind}.{label}",
                        "mode": "managed",
                        "type": kind,
                        "values": values,
                    }
                )
    return {"values": {"root_module": {"resources": resources}}}


def test_every_lambda_default_log_group_is_reported(module_dir):
    from idp_sdk._core import terraform_deploy as tf

    state = _synthetic_state(module_dir)
    reported = tf.service_log_groups(state)
    created_for: dict[str, set[str]] = {}
    for entry in reported:
        for address in entry["created_for"]:
            kind, label = address.split(".")[-2:]
            created_for.setdefault(kind, set()).add(label)

    lambdas = {
        r["address"].split(".")[-1]
        for r in state["values"]["root_module"]["resources"]
        if r["type"] == "aws_lambda_function"
    }
    expected = _cfn_functions_without_a_log_group()
    # An empty expectation would make the comparison below vacuous, and a
    # function the translation lost would silently leave it.
    assert expected, "no function without a log group found; the derivation broke"
    assert expected <= lambdas, sorted(expected - lambdas)
    assert created_for.get("aws_lambda_function") == expected
    for entry in reported:
        _, module, kind, label = entry["created_for"][0].split(".")
        if kind == "aws_lambda_function":
            assert entry["physical_id"] == f"/aws/lambda/{module}-{label}"

    # Every CodeBuild project names a declared group, which destroy deletes.
    assert any(
        r["type"] == "aws_codebuild_project"
        for r in state["values"]["root_module"]["resources"]
    )
    assert "aws_codebuild_project" not in created_for

    # The crawler's security configuration encrypts S3 only, so it writes to the
    # group every crawler in the Region shares; its stream is the deployment's.
    crawlers = [e for e in reported if e["physical_id"].startswith("/aws-glue/")]
    assert [(e["physical_id"], e["shared"]) for e in crawlers] == [
        ("/aws-glue/crawlers", True)
    ]
    assert crawlers[0]["log_streams"] == [
        f"{module_dir.name}-document_sections_crawler"
    ]
