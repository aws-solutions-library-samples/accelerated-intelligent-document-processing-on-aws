# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the headless CloudFormation template transformer.

Regression coverage for the class of bug that produced
"Template error: instance of Fn::GetAtt references undefined resource
GraphQLApi" at deploy time: a resource/output that survives the transform
while still referencing a resource the transform removed (AppSync, Cognito,
WebUI, Discovery, Feature Platform, ...).
"""

import re
from pathlib import Path

import pytest

from idp_sdk._core.template_transform import HeadlessTemplateTransformer

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    """Walk up until we find the repo root (contains the main template.yaml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


def _minimal_template():
    """A template carrying the resources/outputs that broke headless deploys."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Test",
        "Parameters": {
            "EnableMCP": {"Type": "String", "Default": "true"},
            "EnableFeaturePlatform": {"Type": "String", "Default": "true"},
            "AppSyncVisibility": {"Type": "String", "Default": "PUBLIC"},
        },
        "Conditions": {
            "IsFeaturePlatformEnabled": {
                "Fn::Equals": [{"Ref": "EnableFeaturePlatform"}, "true"]
            },
            "UsePrivateAppSync": {
                "Fn::Equals": [{"Ref": "AppSyncVisibility"}, "PRIVATE"]
            },
        },
        "Resources": {
            # Core resources the validator requires to survive.
            "InputBucket": {"Type": "AWS::S3::Bucket"},
            "OutputBucket": {"Type": "AWS::S3::Bucket"},
            "WorkingBucket": {"Type": "AWS::S3::Bucket"},
            "TrackingTable": {"Type": "AWS::DynamoDB::Table"},
            "ConfigurationTable": {"Type": "AWS::DynamoDB::Table"},
            "CustomerManagedEncryptionKey": {"Type": "AWS::KMS::Key"},
            "PATTERNSTACK": {"Type": "AWS::CloudFormation::Stack"},
            # Resources the transform removes.
            "GraphQLApi": {"Type": "AWS::AppSync::GraphQLApi"},
            "UserPool": {"Type": "AWS::Cognito::UserPool"},
            "WebUIBucket": {"Type": "AWS::S3::Bucket"},
            "DiscoveryBucket": {"Type": "AWS::S3::Bucket"},
            # The Feature Platform nested stack — the regression source.
            "FeaturePlatformStack": {
                "Type": "AWS::CloudFormation::Stack",
                "Condition": "IsFeaturePlatformEnabled",
                "DependsOn": ["APIRESOLVERSTACK"],
                "Properties": {
                    "Parameters": {
                        "GraphQLApiId": {"Fn::GetAtt": ["GraphQLApi", "ApiId"]},
                        "GraphQLApiArn": {"Fn::GetAtt": ["GraphQLApi", "Arn"]},
                        "UserPoolId": {"Ref": "UserPool"},
                        "WebUIBucketName": {"Ref": "WebUIBucket"},
                        "DiscoveryBucketName": {"Ref": "DiscoveryBucket"},
                    }
                },
            },
        },
        "Outputs": {
            "AppSyncEndpointForDNS": {
                "Condition": "UsePrivateAppSync",
                "Value": {
                    "Fn::Select": [
                        2,
                        {
                            "Fn::Split": [
                                "/",
                                {"Fn::GetAtt": ["GraphQLApi", "GraphQLUrl"]},
                            ]
                        },
                    ]
                },
            },
            "TrackingTableName": {"Value": {"Ref": "TrackingTable"}},
        },
    }


def _dangling_refs(template, removed):
    """Return (kind, name, path) tuples referencing a removed resource."""
    findings = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "Ref" and isinstance(v, str) and v.split(".")[0] in removed:
                    findings.append(("Ref", v, path))
                elif k == "Fn::GetAtt":
                    name = v[0] if isinstance(v, list) else str(v).split(".")[0]
                    if name in removed:
                        findings.append(("Fn::GetAtt", name, path))
                elif k == "Fn::Sub":
                    s = v[0] if isinstance(v, list) else v
                    if isinstance(s, str):
                        for m in re.findall(r"\$\{([^}]+)\}", s):
                            if m.split(".")[0] in removed:
                                findings.append(("Fn::Sub", m, path))
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, x in enumerate(node):
                walk(x, f"{path}[{i}]")

    walk(template.get("Resources", {}), "Resources")
    walk(template.get("Outputs", {}), "Outputs")
    return findings


def test_feature_platform_stack_removed():
    """FeaturePlatformStack must be stripped — it refs removed AppSync/Cognito."""
    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(_minimal_template())
    assert "FeaturePlatformStack" in t.all_resources_to_remove
    assert "FeaturePlatformStack" not in result["Resources"]


def test_appsync_dns_output_removed():
    """AppSyncEndpointForDNS refs GraphQLApi.GraphQLUrl and must be removed."""
    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(_minimal_template())
    assert "AppSyncEndpointForDNS" not in result.get("Outputs", {})


def test_enable_feature_platform_forced_false():
    """EnableFeaturePlatform default flips to 'false' — the nested stack is gone.

    Leaving the default at 'true' would ask CloudFormation to create a nested
    stack the transform has stripped out.
    """
    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(_minimal_template())
    assert result["Parameters"]["EnableFeaturePlatform"]["Default"] == "false"
    # The TrackingTableName Output is unconditional (and carries no Export), so
    # it survives either way and stays readable from describe-stacks.
    assert "TrackingTableName" in result["Outputs"]


def test_no_dangling_references_to_removed_resources():
    """The whole point: nothing left references a removed resource."""
    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(_minimal_template())
    findings = _dangling_refs(result, t.all_resources_to_remove)
    assert findings == [], f"Dangling references remain: {findings}"


def test_real_template_transforms_without_dangling_refs():
    """Run the transform against the ACTUAL committed template.yaml and assert no
    resource/output/Sub left behind references a removed resource.

    The synthetic _minimal_template only carries a handful of resources, so a
    resource that survives the transform while referencing a removed one (e.g.
    ChatStreamProcessorFunction -> UsersTable) slips past it. This exercises the
    same class of bug — "Fn::GetAtt references undefined resource ..." — against
    every resource that actually ships.
    """
    # cfnlint ships a CloudFormation-aware YAML decoder that understands the
    # shorthand !Ref/!GetAtt/!Sub tags in the source template (plain
    # yaml.safe_load cannot). Skip cleanly if it isn't installed.
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    template_path = _repo_root() / "template.yaml"

    # cfn_yaml.load returns the template as dict-subclass "node" objects (it
    # understands the shorthand !Ref/!GetAtt/!Sub tags plain yaml.safe_load
    # chokes on). The transform round-trips through yaml.safe_dump/load
    # internally, which can't serialize those node types — so coerce the whole
    # tree to plain dict/list/str/scalars first.
    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    template = _plain(cfnlint_decode.load(str(template_path)))
    assert isinstance(template, dict) and "Resources" in template

    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(template)

    findings = _dangling_refs(result, t.all_resources_to_remove)
    assert findings == [], (
        "Headless transform left dangling references to removed resources "
        f"(add them to a strip set in template_transform.py): {findings}"
    )


# publish.py substitutes these <TOKEN> placeholders in the committed template at
# publish time (artifact bucket, prefix, zip names, version, ...). The raw
# placeholders are harmless for structural assertions but not for cfn-lint:
# cfn-lint 1.51 started validating AWS::Lambda::LayerVersion Content.S3Bucket
# against the S3 bucket-name pattern, so "<ARTIFACT_BUCKET_TOKEN>" reported
# E1161/E3031 — a finding about the placeholder, not about the template we
# actually publish. Substituting a lint-valid stand-in (lowercase + dashes: legal
# as both a bucket name and an S3 key fragment) keeps the Error gate below honest
# instead of making it exempt whole rules.
_PUBLISH_TOKEN_RE = re.compile(r"<([A-Z0-9_]+)>")


def _substitute_publish_tokens(text: str) -> str:
    """Replace every publish-time <TOKEN> with a lint-valid stand-in value."""
    return _PUBLISH_TOKEN_RE.sub(lambda m: m.group(1).lower().replace("_", "-"), text)


def _load_real_template_plain():
    """Load the committed template.yaml as plain dict/list/str (no cfn nodes).

    cfn-lint ships a CloudFormation-aware YAML decoder that understands the
    shorthand !Ref/!GetAtt/!Sub tags plain yaml.safe_load chokes on. The
    transform round-trips through yaml.safe_dump/load internally, which can't
    serialize those node types — so coerce the whole tree to plain
    dict/list/str/scalars first, resolving publish-time tokens on the way.
    """
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return _substitute_publish_tokens(str(node))
        return node

    loaded = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    # cfn_yaml.load may return the template or a (template, matches) tuple.
    template = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)
    assert isinstance(template, dict) and "Resources" in template
    return template


def test_real_template_headless_ui_surface_fully_removed():
    """Transform the ACTUAL template.yaml; assert the whole UI surface is gone.

    The dangling-ref tests only prove nothing *references* a removed resource —
    they'd still pass if the transform silently stopped removing, say, the
    Cognito UserPool (a resource with no inbound refs would just survive). This
    asserts the transform's *intent*: headless = no UI, so the representative
    UI/auth/edge resources and their families must actually be absent, while the
    document-processing core survives.
    """
    result = HeadlessTemplateTransformer().apply_transforms(_load_real_template_plain())
    resources = result["Resources"]

    # Representative resources from each family the headless transform removes.
    must_be_absent = {
        "CloudFront": "CloudFrontDistribution",
        "Web UI bucket": "WebUIBucket",
        "UI CodeBuild": "UICodeBuildProject",
        "UI REST API stack": "APIRESOLVERSTACK",
        "Cognito user pool": "UserPool",
        "WAF web ACL": "WAFWebACL",
        "Agent table": "AgentTable",
        "Feature platform stack": "FeaturePlatformStack",
    }
    still_present = {
        family: name for family, name in must_be_absent.items() if name in resources
    }
    assert not still_present, (
        "Headless transform left UI-surface resources in place "
        f"(they should be stripped): {still_present}"
    )

    # No AWS::CloudFront::* / WAFv2 types anywhere either — these are
    # unambiguously edge/UI-only, so a renamed logical id (which the name-based
    # strip set above would miss) still gets caught here.
    #
    # NOTE: Cognito is deliberately NOT banned by type. The interactive Web-UI
    # user pool (`UserPool`) is stripped (asserted by name above), but the
    # Jobs API keeps its OWN Cognito pool for machine-to-machine OAuth
    # (`ApiUserPool` / `ApiAppClient` / ..., gated on EnableJobsApi=true). Those
    # AWS::Cognito::* resources surviving is correct, not a UI leak.
    surviving_ui_types = sorted(
        str(r.get("Type"))
        for r in resources.values()
        if isinstance(r, dict)
        and str(r.get("Type", "")).startswith(("AWS::CloudFront::", "AWS::WAFv2::"))
    )
    assert surviving_ui_types == [], (
        f"Headless transform left edge/UI-only resource types: {surviving_ui_types}"
    )

    # The document-processing core must survive.
    for core in ("InputBucket", "OutputBucket", "TrackingTable", "PATTERNSTACK"):
        assert core in resources, f"Headless transform dropped core resource {core}"


def test_real_template_headless_passes_govcloud_region_cfn_lint():
    """Transform template.yaml to headless and run REAL cfn-lint for GovCloud.

    Mirror of the GovCloud transform's region-lint probe (see
    ``test_govcloud_template_transform.py::test_real_template_passes_govcloud_
    region_cfn_lint`` and scripts/sdlc/docs/CI_TEST_COVERAGE.md). The headless
    template is the API-only variant used for GovCloud batch deployments, so it
    must also be free of GovCloud-unsupported resource types (CloudFront,
    Lambda::Url, ...). This is strictly stronger than the dangling-ref tests:
    cfn-lint ``--region us-gov-west-1`` flags E3006 for *any* unsupported type,
    including a newly introduced one the transformer doesn't yet know to strip.

    No AWS credentials needed (cfn-lint's region check is offline). Skips
    cleanly if cfn-lint isn't installed.
    """
    import json
    import shutil
    import subprocess  # nosec B404 - fixed args, no user input
    import tempfile

    import yaml

    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")

    result = HeadlessTemplateTransformer().apply_transforms(_load_real_template_plain())

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(result, fh)
        out_path = fh.name

    proc = subprocess.run(  # nosec B603 - fixed executable + args
        ["cfn-lint", out_path, "--region", "us-gov-west-1", "--format", "json"],
        capture_output=True,
        text=True,
    )
    try:
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        findings = []
    # Only E3006 ("Resource type ... does not exist in <region>") is the
    # GovCloud-support signal we gate on. Other cfn-lint findings (W-codes,
    # unrelated E-codes from the round-tripped/plain-dumped template) are out of
    # scope for THIS probe and would make it flaky.
    e3006 = [f for f in findings if f.get("Rule", {}).get("Id") == "E3006"]
    assert e3006 == [], (
        "GovCloud-unsupported resource type(s) survived the headless transform "
        "(cfn-lint E3006). Add them to a strip set in template_transform.py: "
        + "; ".join(
            f"{f.get('Location', {}).get('Path')}: {f.get('Message')}" for f in e3006
        )
    )


def test_headless_transform_leaves_no_unresolved_parameter_reference():
    """No surviving Ref/Fn::Sub may point at a parameter the transform removed.

    Regression guard for the bug that broke EVERY ``--headless`` deploy from
    2026-07-16: the ``SuppressAdminInvite`` condition tests the ``AdminEmail``
    parameter against the CI sentinel, ``AdminEmail`` is removed for headless, and
    the condition was not. A Condition referencing a deleted parameter is not dead
    weight — CloudFormation rejects the whole template up front:

        Template format error: Unresolved dependencies [AdminEmail].
        Cannot reference resources in the Conditions block of the template

    so the stack failed at validate time, before creating a single resource
    (matching the "fails earlier on a parameter mismatch" report in issue #676).

    Structural, so it needs neither cfn-lint nor credentials, and it covers
    Conditions/Rules/Outputs — not just Resources.
    """
    base = _load_real_template_plain()
    before = set(base.get("Parameters", {}))
    result = HeadlessTemplateTransformer().apply_transforms(base)
    removed = before - set(result.get("Parameters", {}))
    assert removed, "the headless transform should remove some parameters"

    def _refs(node):
        """Every parameter/resource name referenced via Ref or Fn::Sub."""
        found = set()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Ref" and isinstance(value, str):
                    found.add(value)
                elif key == "Fn::Sub":
                    text = value[0] if isinstance(value, list) else value
                    if isinstance(text, str):
                        found.update(re.findall(r"\$\{([A-Za-z0-9:]+)[.}]", text))
                found |= _refs(value)
        elif isinstance(node, list):
            for item in node:
                found |= _refs(item)
        return found

    dangling = sorted(removed & _refs(result))
    assert not dangling, (
        "headless template still references removed parameter(s): "
        f"{', '.join(dangling)}. Add the referencing condition/resource to a "
        "removal set in template_transform.py — CloudFormation rejects the whole "
        "template, it does not ignore the dead reference."
    )


def test_real_template_headless_has_no_cfn_lint_errors():
    """The headless template must have ZERO cfn-lint Error-level findings.

    The sibling GovCloud probe deliberately gates only on E3006 and discards other
    E-codes as out of scope — which is exactly how an ``E1020 'AdminEmail' is not
    one of [...]`` (a removed parameter still referenced by a surviving condition)
    reached users. This gate closes that hole for the whole Error class.

    Warnings are NOT gated: the headless template legitimately carries unused
    conditions and unreachable Fn::If branches after the transform, and gating
    those would be flaky. Offline — no credentials. Skips if cfn-lint is absent.
    """
    import json
    import shutil
    import subprocess  # nosec B404 - fixed args, no user input
    import tempfile

    import yaml

    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")

    result = HeadlessTemplateTransformer().apply_transforms(_load_real_template_plain())

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(result, fh)
        out_path = fh.name

    proc = subprocess.run(  # nosec B603 - fixed executable + args
        ["cfn-lint", out_path, "--format", "json"],
        capture_output=True,
        text=True,
    )
    try:
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        findings = []
    errors = [f for f in findings if f.get("Level") == "Error"]
    assert errors == [], (
        "the headless template has cfn-lint Error-level finding(s); "
        "CloudFormation will reject it: "
        + "; ".join(
            f"{f.get('Rule', {}).get('Id')} at {f.get('Location', {}).get('Path')}: "
            f"{f.get('Message')}"
            for f in errors
        )
    )


def test_real_template_headless_drops_external_idp_email_mutable_and_its_condition():
    """#835: the parameter and the condition that reads it must BOTH go, or the
    headless template carries a dangling Ref that CloudFormation rejects (E1020).
    Behavioural counterpart of the source-text check in
    scripts/sdlc/tests/test_userpool_email_mutability.py."""
    base = _load_real_template_plain()
    assert "ExternalIdPEmailMutable" in base["Parameters"], (
        "premise: develop declares it"
    )
    assert "ExternalIdPEmailIsMutable" in base["Conditions"]
    result = HeadlessTemplateTransformer().apply_transforms(base)
    assert "ExternalIdPEmailMutable" not in result.get("Parameters", {})
    assert "ExternalIdPEmailIsMutable" not in result.get("Conditions", {})
    interface = result.get("Metadata", {}).get("AWS::CloudFormation::Interface", {})
    for group in interface.get("ParameterGroups", []):
        assert "ExternalIdPEmailMutable" not in group.get("Parameters", [])
    assert "ExternalIdPEmailMutable" not in interface.get("ParameterLabels", {})


# ---------------------------------------------------------------------------
# #981: the transform must REPORT the IAM policy statements it deletes.
#
# Dropping a statement cannot fail at deploy time — a policy with fewer
# statements is still a valid policy — so an over-deletion only shows up later
# as a runtime access-denied in the deployed partition. These tests assert both
# halves: that exactly the CloudFront statement goes, AND that the removal is
# reported with the resource it came from. The reporting half is what is new;
# the filtering was already correct, so a test that only checked the filtering
# would pass with the reporting deleted again.
# ---------------------------------------------------------------------------

_KEEP_SID = "AllowPipelineReads"
_CLOUDFRONT_SID = "AllowCloudFrontLogs"


def _template_with_mixed_bucket_policy():
    """Minimal template whose surviving bucket policy mixes both statements."""
    template = _minimal_template()
    template["Resources"]["LoggingBucketPolicy"] = {
        "Type": "AWS::S3::BucketPolicy",
        "Properties": {
            "Bucket": {"Ref": "OutputBucket"},
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": _CLOUDFRONT_SID,
                        "Effect": "Allow",
                        "Principal": {
                            "Service": {"Fn::Sub": "cloudfront.${AWS::URLSuffix}"}
                        },
                        "Action": "s3:PutObject",
                        "Resource": {"Fn::Sub": "${OutputBucket.Arn}/*"},
                    },
                    {
                        "Sid": _KEEP_SID,
                        "Effect": "Allow",
                        "Principal": {
                            "Service": {"Fn::Sub": "lambda.${AWS::URLSuffix}"}
                        },
                        "Action": "s3:GetObject",
                        "Resource": {"Fn::Sub": "${OutputBucket.Arn}/*"},
                    },
                ],
            },
        },
    }
    return template


def _statement_sids(template, resource_name="LoggingBucketPolicy"):
    policy = template["Resources"][resource_name]["Properties"]["PolicyDocument"]
    return [s.get("Sid") for s in policy["Statement"]]


def test_cloudfront_statement_removed_and_reported(caplog):
    """Exactly the CloudFront statement goes, and the removal is reported."""
    t = HeadlessTemplateTransformer()
    with caplog.at_level("INFO", logger="idp_sdk._core.template_transform"):
        result = t.apply_transforms(_template_with_mixed_bucket_policy())

    # Filtering: the CloudFront statement goes, the other one stays untouched.
    assert _statement_sids(result) == [_KEEP_SID]

    # Reporting, machine-readable: one record naming the resource and the count.
    matching = [
        r
        for r in t.policy_statement_removals
        if r.resource_identifier == "LoggingBucketPolicy"
    ]
    assert len(matching) == 1, (
        f"expected one removal record for LoggingBucketPolicy, "
        f"got {t.policy_statement_removals}"
    )
    assert matching[0].removed == 1
    assert matching[0].narrowed == 0
    assert "CloudFront" in matching[0].reason

    # Reporting, operator-visible: the resource name and count reach the INFO log
    # the transform's callers configure, both per-removal and in the summary.
    messages = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("LoggingBucketPolicy" in m and "removed 1" in m for m in messages), (
        f"no per-removal INFO line naming the resource: {messages}"
    )
    summary = [m for m in messages if m.startswith("Removed 1 policy statement(s)")]
    assert summary, f"no removal summary line: {messages}"
    assert "LoggingBucketPolicy" in summary[0]


def test_policy_statement_removal_summary_reports_zero_when_nothing_removed(caplog):
    """A template with no CloudFront statement still gets an explicit '0' line.

    Silence and "nothing to remove" must not look the same to an operator
    auditing what the transform did to a role.
    """
    t = HeadlessTemplateTransformer()
    template = _template_with_mixed_bucket_policy()
    statements = template["Resources"]["LoggingBucketPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    statements[:] = [s for s in statements if s["Sid"] != _CLOUDFRONT_SID]

    with caplog.at_level("INFO", logger="idp_sdk._core.template_transform"):
        result = t.apply_transforms(template)

    assert _statement_sids(result) == [_KEEP_SID]
    assert t.policy_statement_removals == []
    assert "Removed 0 policy statements" in [r.getMessage() for r in caplog.records]


def test_cloudfront_principal_narrowing_is_reported():
    """A statement that only loses CloudFront from a Service LIST is reported too.

    It survives, so no count of removed statements would mention it, but the
    role's effective permissions changed all the same.
    """
    template = _template_with_mixed_bucket_policy()
    policy = template["Resources"]["LoggingBucketPolicy"]["Properties"][
        "PolicyDocument"
    ]
    policy["Statement"] = [
        {
            "Sid": _KEEP_SID,
            "Effect": "Allow",
            "Principal": {
                "Service": [
                    {"Fn::Sub": "cloudfront.${AWS::URLSuffix}"},
                    {"Fn::Sub": "lambda.${AWS::URLSuffix}"},
                ]
            },
            "Action": "s3:GetObject",
            "Resource": {"Fn::Sub": "${OutputBucket.Arn}/*"},
        }
    ]

    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(template)

    kept = result["Resources"]["LoggingBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert len(kept) == 1
    assert kept[0]["Principal"]["Service"] == [{"Fn::Sub": "lambda.${AWS::URLSuffix}"}]
    records = [
        r
        for r in t.policy_statement_removals
        if r.resource_identifier == "LoggingBucketPolicy"
    ]
    assert len(records) == 1
    assert (records[0].removed, records[0].narrowed) == (0, 1)


def test_role_inline_policy_removal_is_reported_with_the_policy_name():
    """For a role, the identifier must pin down WHICH inline policy changed."""
    template = _template_with_mixed_bucket_policy()
    template["Resources"]["LoggingRole"] = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "Policies": [
                {
                    "PolicyName": "CloudFrontLogDelivery",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": _CLOUDFRONT_SID,
                                "Effect": "Allow",
                                "Principal": {"Service": "cloudfront.amazonaws.com"},
                                "Action": "s3:PutObject",
                                "Resource": "*",
                            },
                            {
                                "Sid": _KEEP_SID,
                                "Effect": "Allow",
                                "Action": "s3:GetObject",
                                "Resource": "*",
                            },
                        ],
                    },
                }
            ]
        },
    }

    t = HeadlessTemplateTransformer()
    result = t.apply_transforms(template)

    kept = result["Resources"]["LoggingRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    assert [s["Sid"] for s in kept] == [_KEEP_SID]
    assert any(
        r.resource_identifier == "LoggingRole.CloudFrontLogDelivery" and r.removed == 1
        for r in t.policy_statement_removals
    ), t.policy_statement_removals


def test_real_template_policy_statement_removals_are_all_attributed():
    """Against the real template: every removal names a resource and a reason.

    An unattributed record would be exactly the gap #981 describes — a statement
    gone from a deployed role with nothing saying which role.
    """
    t = HeadlessTemplateTransformer()
    t.apply_transforms(_load_real_template_plain())
    assert t.policy_statement_removals, (
        "premise: the real template has statements the headless transform drops "
        "(a CloudFront log-delivery grant and an AppSync/MCP permission). An "
        "empty record here means the reporting stopped working, not that the "
        "transform stopped deleting."
    )
    for record in t.policy_statement_removals:
        assert record.resource_identifier.strip(), record
        assert record.reason.strip(), record
        assert record.removed + record.narrowed > 0, record
