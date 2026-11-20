# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``CustomerManagedKmsKeyArn``: a supplied key for a new deployment (#2016).

When the parameter is empty (its default) the main template must deploy exactly
what it deployed before the parameter existed, so an existing stack updated
without it replaces and re-encrypts nothing. When it is set, the stack creates no
key and every reference to the deployment's key is the supplied ARN.

Both halves are asserted structurally over ``template.yaml``:

- every reference to ``CustomerManagedEncryptionKey`` is the one
  ``Fn::If [UseSuppliedKmsKey, !GetAtt CustomerManagedKmsKeyCheck.KeyArn, <the old
  reference>]`` form (``scripts/tests/deployment_key.py``), so the empty case
  resolves to the reference the template always had, and a supplied key is only
  used once the check has accepted it;
- collapsing every such ``Fn::If`` to its default branch and removing the check
  (``CustomerManagedKmsKeyCheck``, its function and log group) leaves a template
  that names neither the parameter, its conditions nor the check anywhere — which
  is the statement "the default render is the template without the feature";
- collapsing to the supplied branch leaves nothing naming the stack's key or alias.

These are properties of the source template, not a comparison with an earlier
revision of it; the comparison with the base the feature was written against was
measured once and is recorded in the pull request.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import deployment_key as dk
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = "CustomerManagedKmsKeyCheck"
CHECK_FUNCTION = "CustomerManagedKmsKeyCheckFunction"
CHECK_LOG_GROUP = "CustomerManagedKmsKeyCheckFunctionLogGroup"
ALIAS = "CustomerManagedEncryptionKeyAlias"
CHECK_TRIO = {CHECK, CHECK_FUNCTION, CHECK_LOG_GROUP}


@pytest.fixture(scope="module")
def template() -> dict:
    from idp_sdk._core.cfn_yaml import load_cfn_template

    return load_cfn_template(str(REPO_ROOT / "template.yaml"))


def _names_key(node: Any) -> bool:
    return dk.is_key_arn(node) or dk.is_key_id(node)


def _walk(node: Any, path: str, out: list[tuple[str, Any]]) -> None:
    """Every node naming the stack key (Ref, GetAtt or Sub) outside a
    deployment-key ``Fn::If``, and every other ``UseSuppliedKmsKey`` choice
    outside the check's own policies, with its path; deployment-key forms are
    recorded as ``("ok", path)``."""
    if dk.is_deployment_key_arn(node) or dk.is_deployment_key_id(node):
        out.append(("ok", path))
        return
    if dk.branches(node) is not None and not path.startswith(
        f"Resources/{CHECK_FUNCTION}/"
    ):
        # Any other UseSuppliedKmsKey choice would change what the default
        # branch deploys, or what a supplied key replaces.
        out.append(("bare", path))
        return
    if _names_key(node):
        out.append(("bare", path))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("Fn::Sub", "!Sub"):
                text = v[0] if isinstance(v, list) else v
                if isinstance(text, str) and re.search(
                    r"\$\{" + dk.KEY + r"[.}]", text
                ):
                    out.append(("bare", f"{path}/{k}"))
            _walk(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", out)


def _references(template: dict) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for section in ("Resources", "Outputs"):
        for lid, body in (template.get(section) or {}).items():
            if section == "Resources" and lid == ALIAS:
                # The alias is the one bare reference: it exists only with the key
                # (asserted below), so it may name the key directly.
                continue
            _walk(body, f"{section}/{lid}", out)
    return out


def _collapse(node: Any, branch: int) -> Any:
    """``branch`` 1 is the supplied-key branch, 2 the default."""
    b = dk.branches(node)
    if b is not None:
        return _collapse(b[branch - 1], branch)
    if isinstance(node, dict):
        return {k: _collapse(v, branch) for k, v in node.items()}
    if isinstance(node, list):
        return [_collapse(v, branch) for v in node]
    return node


def test_every_reference_to_the_stack_key_is_the_deployment_key_form(template):
    refs = _references(template)
    bare = [p for kind, p in refs if kind == "bare"]
    ok = [p for kind, p in refs if kind == "ok"]
    assert ok, "no deployment-key reference found; the walk reads nothing"
    assert not bare, (
        "these name CustomerManagedEncryptionKey directly, so with "
        "CustomerManagedKmsKeyArn set they would name a resource that is not "
        f"created. Write `!If [UseSuppliedKmsKey, !GetAtt "
        f"CustomerManagedKmsKeyCheck.KeyArn, <reference>]` instead: {bare}"
    )


# What each property's default branch names: the key's ARN (``!GetAtt .Arn``)
# or its id (``!Ref``). The default branch is what an existing stack deploys, so
# it must stay the form each property had before CustomerManagedKmsKeyArn
# existed; a flip from id to ARN would update every such resource on upgrade.
# IAM resources, log groups and the nested stacks' ``...Arn`` parameters need an
# ARN; the rest have always taken the id. A property not listed here fails, so a
# new kind of reference is decided once, here, rather than copied by accident.
DEFAULT_FORM_BY_PROPERTY = {
    ("AWS::Logs::LogGroup", "/KmsKeyId"): "arn",
    ("AWS::Serverless::Function", "/Policies[]/Statement[]/Resource"): "arn",
    ("AWS::Serverless::Function", "/Policies[]/Statement[]/Resource[]"): "arn",
    ("AWS::Serverless::Function", "/Environment/Variables/KMS_KEY_ID"): "arn",
    # A statement present only under another condition.
    ("AWS::Serverless::Function", "/Policies[]/!If[]/Statement[]/Resource"): "arn",
    (
        "AWS::Glue::SecurityConfiguration",
        "/EncryptionConfiguration/S3Encryptions[]/KmsKeyArn",
    ): "arn",
    ("AWS::IAM::Role", "/Policies[]/PolicyDocument/Statement[]/Resource"): "arn",
    ("AWS::IAM::Role", "/Policies[]/PolicyDocument/Statement[]/Resource[]"): "arn",
    ("AWS::IAM::Policy", "/PolicyDocument/Statement[]/Resource"): "arn",
    ("AWS::IAM::Policy", "/PolicyDocument/Statement[]/Resource[]"): "arn",
    ("AWS::IAM::ManagedPolicy", "/PolicyDocument/Statement[]/Resource"): "arn",
    ("AWS::IAM::ManagedPolicy", "/PolicyDocument/Statement[]/Resource[]"): "arn",
    (
        "AWS::CloudFormation::Stack",
        "/Parameters/CustomerManagedEncryptionKeyArn",
    ): "arn",
    (
        "AWS::CloudFormation::Stack",
        "/Parameters/pCustomerManagedEncryptionKeyArn",
    ): "arn",
    ("AWS::SQS::Queue", "/KmsMasterKeyId"): "id",
    ("AWS::SNS::Topic", "/KmsMasterKeyId"): "id",
    ("AWS::DynamoDB::Table", "/SSESpecification/KMSMasterKeyId"): "id",
    ("AWS::SecretsManager::Secret", "/KmsKeyId"): "id",
    (
        "AWS::S3::Bucket",
        "/BucketEncryption/ServerSideEncryptionConfiguration[]"
        "/ServerSideEncryptionByDefault/KMSMasterKeyID",
    ): "id",
}


def _default_forms(template: dict) -> list[tuple[str, str, str, str]]:
    """``(logical id, type, property path, "arn" | "id")`` per reference."""
    out: list[tuple[str, str, str, str]] = []

    def walk(node: Any, lid: str, rtype: str, path: str) -> None:
        if dk.is_deployment_key_arn(node) or dk.is_deployment_key_id(node):
            form = "arn" if dk.is_deployment_key_arn(node) else "id"
            out.append((lid, rtype, re.sub(r"\[\d+\]", "[]", path), form))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, lid, rtype, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, lid, rtype, f"{path}[{i}]")

    for lid, body in (template.get("Resources") or {}).items():
        walk(body.get("Properties") or {}, lid, body.get("Type", ""), "")
    return out


def test_each_default_branch_keeps_the_form_its_property_always_had(template):
    forms = _default_forms(template)
    assert forms, "no deployment-key reference found; the walk reads nothing"
    wrong = [f for f in forms if DEFAULT_FORM_BY_PROPERTY.get((f[1], f[2])) != f[3]]
    assert not wrong, (
        "these default branches name the key in a form their property has not "
        "used (or a property this test has not decided); an existing stack would "
        f"see the change on upgrade: {wrong}"
    )


def test_the_documented_key_policy_grants_every_service_the_stacks_own_does(
    template,
):
    """docs/customer-managed-kms-key.md gives the policy a supplied key needs;
    a service principal the stack grants its own key and the page omits is a
    deployment that fails (the S3 Vectors index is the measured case)."""
    page = (REPO_ROOT / "docs" / "customer-managed-kms-key.md").read_text()
    doc = json.loads(re.search(r"```json\n(.*?)```", page, re.S).group(1))
    documented = {
        s["Principal"]["Service"].removesuffix(".amazonaws.com")
        for s in doc["Statement"]
        if "Service" in s.get("Principal", {})
    }
    granted: set[str] = set()

    def services(node: Any) -> None:
        if isinstance(node, dict):
            svc = node.get("Service")
            text = (
                svc.get("!Sub") or svc.get("Fn::Sub") if isinstance(svc, dict) else None
            )
            if isinstance(text, str):
                granted.add(text.removesuffix(".${AWS::URLSuffix}"))
            for v in node.values():
                services(v)
        elif isinstance(node, list):
            for v in node:
                services(v)

    services(template["Resources"][dk.KEY]["Properties"]["KeyPolicy"])
    assert granted, (
        "no service principal found in the key policy; the walk reads nothing"
    )
    assert documented == granted


def test_the_branches_are_complementary(template):
    assert dk.complementary(template) == []
    alias = template["Resources"][ALIAS]
    assert alias.get("Condition") == dk.CREATE_OWN
    assert alias["Properties"]["TargetKeyId"] in ({"!Ref": dk.KEY}, {"Ref": dk.KEY})


def test_the_default_branch_is_the_template_without_the_feature(template):
    """Collapse to the empty-parameter branch, remove the check, and nothing of
    the feature is left: the default render is the template without it."""
    t = copy.deepcopy(template)
    resources = _collapse(t["Resources"], 2)
    outputs = _collapse(t.get("Outputs") or {}, 2)
    for lid in CHECK_TRIO:
        assert lid in resources, f"{lid} is gone; revisit this test"
        del resources[lid]
    key = resources[dk.KEY]
    assert key.pop("Condition") == dk.CREATE_OWN
    assert resources[ALIAS].pop("Condition") == dk.CREATE_OWN
    deps = key["DependsOn"]
    assert CHECK in deps
    key["DependsOn"] = [d for d in deps if d != CHECK]
    text = json.dumps({"Resources": resources, "Outputs": outputs}, default=str)
    for name in (dk.PARAMETER, dk.USE_SUPPLIED, dk.CREATE_OWN, CHECK):
        assert name not in text, (
            f"{name} survives in the default branch, so an existing stack updated "
            f"without CustomerManagedKmsKeyArn would see a changed resource"
        )


def test_the_supplied_branch_never_names_the_stack_key(template):
    resources = _collapse(copy.deepcopy(template["Resources"]), 1)
    outputs = _collapse(copy.deepcopy(template.get("Outputs") or {}), 1)
    live = {
        lid: body
        for lid, body in resources.items()
        if body.get("Condition") != dk.CREATE_OWN
    }
    assert dk.KEY not in live and ALIAS not in live
    out: list[tuple[str, Any]] = []
    for lid, body in {**live, **outputs}.items():
        _walk(body, lid, out)
    assert not out, f"named with the supplied key in force: {out}"


def test_the_check_runs_first_and_is_unconditional(template):
    resources = template["Resources"]
    check = resources[CHECK]
    assert "Condition" not in check, (
        "the check must exist in every stack: clearing the parameter on a stack "
        "created with one is the case it refuses, and a conditional check would "
        "already be gone then"
    )
    assert check["Properties"]["KeyArn"] in (
        {"!Ref": dk.PARAMETER},
        {"Ref": dk.PARAMETER},
    )
    assert CHECK in resources[dk.KEY]["DependsOn"]
    # The check's log group cannot use the key the check orders.
    assert "KmsKeyId" not in resources[CHECK_LOG_GROUP]["Properties"]


def test_the_check_is_granted_only_what_it_calls(template):
    policies = template["Resources"][CHECK_FUNCTION]["Properties"]["Policies"]
    allows: list[tuple[Any, Any, bool]] = []
    for p in policies:
        inner = p.get("!If") or p.get("Fn::If")
        if inner is not None:
            assert inner[0] == dk.USE_SUPPLIED and inner[2] in (
                {"!Ref": "AWS::NoValue"},
                {"Ref": "AWS::NoValue"},
            )
            statements, guarded = inner[1]["Statement"], True
        else:
            statements, guarded = p["Statement"], False
        for s in statements:
            if s["Effect"] == "Allow":
                allows.append((s["Action"], s["Resource"], guarded))
    assert sorted(map(str, allows)) == sorted(
        map(
            str,
            [
                ("cloudformation:DescribeStacks", {"!Ref": "AWS::StackId"}, False),
                ("kms:DescribeKey", {"!Ref": dk.PARAMETER}, True),
            ],
        )
    )


@pytest.mark.parametrize(
    "value,accepted",
    [
        ("", True),
        (
            "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            True,
        ),
        (
            "arn:aws-us-gov:kms:us-gov-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            True,
        ),
        (
            "arn:aws-cn:kms:cn-north-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            True,
        ),
        (
            "arn:aws:kms:eu-west-1:111122223333:key/mrk-1234abcd12ab34cd56ef1234567890ab",
            True,
        ),
        ("1234abcd-12ab-34cd-56ef-1234567890ab", False),
        (
            "arn:foo:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            False,
        ),
        ("arn:aws:kms:us-east-1:111122223333:alias/my-key", False),
        # An alias name shaped like a key id: only the `key/` prefix rejects it.
        (
            "arn:aws:kms:us-east-1:111122223333:alias/1234abcd-12ab-34cd-56ef-1234567890ab",
            False,
        ),
        (
            "arn:aws:s3:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            False,
        ),
        (
            "arn:aws:kms:us-east-1:11112222333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            False,
        ),
        (
            "arn:aws:kms:us-east-1:111122223333:key/1234ABCD-12ab-34cd-56ef-1234567890ab",
            False,
        ),
        (
            "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab ",
            False,
        ),
    ],
)
def test_the_allowed_pattern_is_a_partition_agnostic_key_arn(template, value, accepted):
    pattern = template["Parameters"][dk.PARAMETER]["AllowedPattern"]
    # CloudFormation anchors AllowedPattern on the whole value.
    assert bool(re.fullmatch(pattern, value)) is accepted
