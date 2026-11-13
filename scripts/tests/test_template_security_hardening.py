# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Template hardening that the Terraform-side scan reported as CloudFormation gaps.

``make terraform-scan-diff`` scans the generated Terraform with checkov rules that
have no CloudFormation counterpart, so a finding there that is "the template is the
same" is a CloudFormation gap no CloudFormation-side scanner reports. Each rule
below closes one of them and holds it closed:

1. **The API's WAF web ACL logs.** ``ApiWafWebACL`` (an IP allowlist with
   ``DefaultAction: Block``) has an ``AWS::WAFv2::LoggingConfiguration`` under the
   same condition, sending to a log group whose name starts ``aws-waf-logs-``
   (WAF refuses any other CloudWatch Logs destination), with the template's
   retention parameter and its customer-managed key. The destination is the bare
   log-group ARN: ``!GetAtt <group>.Arn`` ends in ``:*``, which WAF rejects.
2. **Every CodeBuild project logs to a group the stack owns.** Without
   ``LogsConfig`` CodeBuild creates ``/aws/codebuild/<project>`` itself, with no
   retention and no key, and the stack never deletes it. Each project in the main
   deployment (``template.yaml`` and every template it deploys as a nested stack,
   derived from the ``TemplateURL`` values rather than listed) names a group
   ``/${AWS::StackName}/codebuild/<LogicalId>`` with retention and the key, and
   its service role may write to that group and may not create log groups at all.
3. **The UI build runs unprivileged.** ``UICodeBuildProject`` runs ``npm`` and
   the AWS CLI only; privileged mode exists to run a Docker daemon. If its
   buildspec ever gains a Docker command, the second assertion says so, rather
   than the build failing at deploy time with privileged mode off.
4. **BDAProcessResultsFunction has no SSM grant.** ``ssm:GetParameter``,
   ``PutParameter`` and ``GetParametersByPath`` on ``*`` came with the removed
   SageMaker A2I flow; neither the handler nor any ``idp_common`` module it
   imports calls SSM, and it has no ``SETTINGS_PARAMETER``.
5. **The web UI bucket expires noncurrent versions.** It is versioned and every UI
   build overwrites its objects, so without a rule the overwritten versions are
   kept forever. Only *noncurrent* versions expire: current objects are the live
   UI.
6. **The MCP content bucket aborts incomplete multipart uploads bucket-wide**, not
   only under ``mcp-temp/``.
"""

from __future__ import annotations

import functools
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CFN_YAML = REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"

pytestmark = pytest.mark.unit


@functools.lru_cache(maxsize=None)
def _cfn_yaml() -> Any:
    spec = importlib.util.spec_from_file_location("_cfn_yaml_hardening", _CFN_YAML)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _long_form(node: Any) -> Any:
    """The loader keeps short-form tags as ``!Ref``/``!Sub`` keys; spell every
    intrinsic in long form (``Ref``, ``Fn::Sub``) so one comparison covers both."""
    if isinstance(node, list):
        return [_long_form(v) for v in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if isinstance(k, str) and k.startswith("!"):
                k = "Ref" if k == "!Ref" else f"Fn::{k[1:]}"
            out[k] = _long_form(v)
        return out
    return node


@functools.lru_cache(maxsize=None)
def _load(rel_path: str) -> dict:
    return _long_form(_cfn_yaml().load_cfn_template(REPO_ROOT / rel_path))


def _resources(rel_path: str) -> dict[str, dict]:
    return _load(rel_path).get("Resources") or {}


def _main_deployment_templates() -> list[str]:
    """``template.yaml`` and every template it deploys as a nested stack, at any
    depth, from the ``TemplateURL`` values (``./<dir>/.aws-sam/packaged.yaml``)."""
    out: list[str] = []
    todo = ["template.yaml"]
    while todo:
        rel = todo.pop()
        if rel in out:
            continue
        out.append(rel)
        base = Path(rel).parent
        for res in _resources(rel).values():
            if res.get("Type") != "AWS::CloudFormation::Stack":
                continue
            url = (res.get("Properties") or {}).get("TemplateURL")
            if not isinstance(url, str) or not url.endswith("/.aws-sam/packaged.yaml"):
                continue
            child = base / url.removeprefix("./").removesuffix(
                "/.aws-sam/packaged.yaml"
            )
            todo.append(str(child / "template.yaml"))
    return sorted(out)


def _is_ref(value: Any, name: str) -> bool:
    return isinstance(value, dict) and value.get("Ref") == name


def _sub_text(value: Any) -> str | None:
    if isinstance(value, dict) and "Fn::Sub" in value:
        sub = value["Fn::Sub"]
        return sub[0] if isinstance(sub, list) else sub
    return None


def _key_expression_ok(rel_path: str, value: Any) -> bool:
    """The KmsKeyId the template's other log groups use."""
    if rel_path == "template.yaml":
        return value == {"Fn::GetAtt": ["CustomerManagedEncryptionKey", "Arn"]} or (
            value == {"Fn::GetAtt": "CustomerManagedEncryptionKey.Arn"}
        )
    return _is_ref(value, "CustomerManagedEncryptionKeyArn")


def _statements(role: dict) -> list[dict]:
    out: list[dict] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, dict):
            if "Fn::If" in x and len(x) == 1:
                for branch in x["Fn::If"][1:]:
                    walk(branch)
            elif "Effect" in x:
                out.append(x)
            elif "Statement" in x:
                walk(x["Statement"])

    for policy in (role.get("Properties") or {}).get("Policies") or []:
        if isinstance(policy, dict):
            walk(policy.get("PolicyDocument"))
    return out


def _actions(stmt: dict) -> list[str]:
    a = stmt.get("Action")
    return [a] if isinstance(a, str) else list(a or [])


# --------------------------------------------------------------------- 1. WAF
API = "nested/api-resolvers/template.yaml"


def _waf_logging() -> tuple[dict, dict]:
    res = _resources(API)
    configs = [
        r for r in res.values() if r.get("Type") == "AWS::WAFv2::LoggingConfiguration"
    ]
    assert len(configs) == 1, (
        f"expected one AWS::WAFv2::LoggingConfiguration in {API}, found {len(configs)}"
    )
    cfg = configs[0]
    dests = cfg["Properties"]["LogDestinationConfigs"]
    assert len(dests) == 1, dests
    text = _sub_text(dests[0])
    assert text is not None, f"destination must be a !Sub of the bare ARN: {dests[0]}"
    m = re.fullmatch(
        r"arn:\$\{AWS::Partition\}:logs:\$\{AWS::Region\}:\$\{AWS::AccountId\}"
        r":log-group:\$\{(\w+)\}",
        text,
    )
    assert m, f"destination is not the bare log-group ARN of a group: {text}"
    group = res[m.group(1)]
    assert group["Type"] == "AWS::Logs::LogGroup"
    return cfg, group


def test_waf_web_acl_has_a_logging_configuration_under_its_condition() -> None:
    cfg, _ = _waf_logging()
    acl = _resources(API)["ApiWafWebACL"]
    assert cfg.get("Condition") == acl.get("Condition") == "IsWafEnabled"
    assert cfg["Properties"]["ResourceArn"] in (
        {"Fn::GetAtt": ["ApiWafWebACL", "Arn"]},
        {"Fn::GetAtt": "ApiWafWebACL.Arn"},
    )


def test_waf_log_group_is_named_retained_and_encrypted() -> None:
    _, group = _waf_logging()
    props = group["Properties"]
    assert group.get("Condition") == "IsWafEnabled"
    name = _sub_text(props.get("LogGroupName"))
    assert name is not None and name.startswith("aws-waf-logs-"), (
        f"WAF accepts only a log group named aws-waf-logs-*: {props.get('LogGroupName')}"
    )
    assert "${StackName}" in name or "${AWS::StackName}" in name
    assert _is_ref(props.get("RetentionInDays"), "LogRetentionDays")
    assert _key_expression_ok(API, props.get("KmsKeyId"))


# --------------------------------------------------------------- 2. CodeBuild
def _codebuild_projects() -> list[tuple[str, str, dict]]:
    return [
        (rel, lid, res)
        for rel in _main_deployment_templates()
        for lid, res in _resources(rel).items()
        if res.get("Type") == "AWS::CodeBuild::Project"
    ]


def test_the_codebuild_universe_is_not_empty() -> None:
    found = {(rel, lid) for rel, lid, _ in _codebuild_projects()}
    assert ("template.yaml", "UICodeBuildProject") in found
    assert ("patterns/unified/template.yaml", "DockerBuildProject") in found
    assert ("nested/multi-doc-discovery/template.yaml", "DockerBuildProject") in found


@pytest.mark.parametrize(
    "rel,lid",
    [(rel, lid) for rel, lid, _ in _codebuild_projects()],
    ids=lambda v: v,
)
def test_codebuild_project_logs_to_its_own_log_group(rel: str, lid: str) -> None:
    project = _resources(rel)[lid]
    logs = (project["Properties"].get("LogsConfig") or {}).get("CloudWatchLogs")
    assert logs, f"{rel} {lid} has no LogsConfig.CloudWatchLogs"
    assert logs.get("Status") == "ENABLED"
    group_ref = logs.get("GroupName")
    assert isinstance(group_ref, dict) and set(group_ref) == {"Ref"}, group_ref
    group = _resources(rel)[group_ref["Ref"]]
    assert group["Type"] == "AWS::Logs::LogGroup"
    assert group.get("Condition") == project.get("Condition")
    props = group["Properties"]
    assert _sub_text(props.get("LogGroupName")) == (
        f"/${{AWS::StackName}}/codebuild/{lid}"
    )
    assert _is_ref(props.get("RetentionInDays"), "LogRetentionDays")
    assert _key_expression_ok(rel, props.get("KmsKeyId"))


@pytest.mark.parametrize(
    "rel,lid",
    [(rel, lid) for rel, lid, _ in _codebuild_projects()],
    ids=lambda v: v,
)
def test_codebuild_role_writes_its_group_and_creates_none(rel: str, lid: str) -> None:
    res = _resources(rel)
    project = res[lid]
    role_ref = project["Properties"]["ServiceRole"]
    role_lid = (
        role_ref["Ref"]
        if "Ref" in role_ref
        else (
            role_ref["Fn::GetAtt"][0]
            if isinstance(role_ref["Fn::GetAtt"], list)
            else role_ref["Fn::GetAtt"].split(".")[0]
        )
    )
    group_lid = project["Properties"]["LogsConfig"]["CloudWatchLogs"]["GroupName"][
        "Ref"
    ]
    stmts = _statements(res[role_lid])
    logs = [s for s in stmts if any(a.startswith("logs:") for a in _actions(s))]
    assert logs, f"{role_lid} cannot write logs at all"
    for s in logs:
        assert s.get("Effect") == "Allow"
        assert "logs:CreateLogGroup" not in _actions(s), (
            f"{role_lid} may create log groups: with LogsConfig the group exists "
            f"before the build, and CreateLogGroup would let a build recreate a "
            f"deleted one with no retention and no key"
        )
        assert set(_actions(s)) <= {"logs:CreateLogStream", "logs:PutLogEvents"}
        resources = (
            s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        )
        assert resources and all(
            r
            in ({"Fn::GetAtt": [group_lid, "Arn"]}, {"Fn::GetAtt": f"{group_lid}.Arn"})
            for r in resources
        ), f"{role_lid} logs statement is not scoped to {group_lid}: {resources}"
    assert not any("aws/codebuild" in str(s.get("Resource")) for s in stmts)


# ------------------------------------------------------- 3. UI privileged mode
DOCKER = re.compile(r"\b(docker|dockerd|buildx|podman)\b|--use-container")


def test_ui_build_runs_unprivileged() -> None:
    env = _resources("template.yaml")["UICodeBuildProject"]["Properties"]["Environment"]
    assert env.get("PrivilegedMode") is False


def test_ui_buildspec_needs_no_docker_daemon() -> None:
    spec = _resources("template.yaml")["UICodeBuildProject"]["Properties"]["Source"][
        "BuildSpec"
    ]
    assert isinstance(spec, str) and "phases:" in spec, "BuildSpec is no longer inline"
    hits = DOCKER.findall(spec)
    assert not hits, (
        f"the UI buildspec now runs {hits}: that needs PrivilegedMode true, which "
        f"test_ui_build_runs_unprivileged forbids. Decide which one changes."
    )


def test_the_docker_pattern_matches_what_it_guards() -> None:
    for line in ("docker build -t x .", "sam build --use-container", "docker buildx b"):
        assert DOCKER.search(line), line
    for line in ("npm run build", "aws s3 cp --recursive build s3://b/"):
        assert not DOCKER.search(line), line


# ------------------------------------------------------ 4. BDA results SSM
UNIFIED = "patterns/unified/template.yaml"


def test_bda_process_results_function_has_no_ssm_grant() -> None:
    fn = _resources(UNIFIED)["BDAProcessResultsFunction"]
    text = repr(fn["Properties"].get("Policies"))
    assert "ssm:" not in text, "BDAProcessResultsFunction grants an SSM action"


def test_bda_process_results_handler_makes_no_ssm_call() -> None:
    src = (
        REPO_ROOT / "patterns/unified/src/bda_processresults_function/index.py"
    ).read_text()
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"""client\(\s*["']ssm["']|get_settings?\(""", code), (
        "the BDA results handler now reads SSM: restore a grant scoped to the "
        "parameter it reads"
    )
    env = _resources(UNIFIED)["BDAProcessResultsFunction"]["Properties"]["Environment"][
        "Variables"
    ]
    assert "SETTINGS_PARAMETER" not in env


# ------------------------------------------------------------- 5/6. buckets
def _rules(lid: str) -> list[dict]:
    props = _resources("template.yaml")[lid]["Properties"]
    return (props.get("LifecycleConfiguration") or {}).get("Rules") or []


def _bucket_wide(rule: dict) -> bool:
    return (
        "Prefix" not in rule
        and "TagFilters" not in rule
        and "ObjectSizeGreaterThan" not in rule
        and "ObjectSizeLessThan" not in rule
    )


def test_web_ui_bucket_expires_only_noncurrent_versions() -> None:
    props = _resources("template.yaml")["WebUIBucket"]["Properties"]
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    rules = [r for r in _rules("WebUIBucket") if r.get("Status") == "Enabled"]
    noncurrent = [
        r for r in rules if _bucket_wide(r) and "NoncurrentVersionExpiration" in r
    ]
    assert noncurrent, "WebUIBucket keeps overwritten UI builds forever"
    assert noncurrent[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30
    for r in rules:
        assert "ExpirationInDays" not in r and "ExpirationDate" not in r, (
            "a current-object expiry on WebUIBucket deletes the live UI"
        )


@pytest.mark.parametrize("lid", ["WebUIBucket", "MCPContentBucket"])
def test_bucket_aborts_incomplete_uploads_bucket_wide(lid: str) -> None:
    rules = [
        r
        for r in _rules(lid)
        if r.get("Status") == "Enabled"
        and _bucket_wide(r)
        and "AbortIncompleteMultipartUpload" in r
    ]
    assert rules, f"{lid} has no bucket-wide AbortIncompleteMultipartUpload rule"
    assert rules[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 1


def test_mcp_temp_files_still_expire() -> None:
    temp = [r for r in _rules("MCPContentBucket") if r.get("Prefix") == "mcp-temp/"]
    assert temp and temp[0]["ExpirationInDays"] == 7
