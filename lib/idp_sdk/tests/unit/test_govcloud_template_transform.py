# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the GovCloud CloudFormation template transformer.

The GovCloud transform must remove every ``AWS::CloudFront::*`` resource (those
types do not exist in GovCloud and produce ``E3006 Resource type
'AWS::CloudFront::Distribution' does not exist in 'us-gov-west-1'`` errors), and
must leave NO dangling reference to a removed CloudFront resource or the removed
``UseCloudFrontHosting`` condition. It must also force ``WebUIHosting=APIGateway``
while keeping the rest of the UI intact.
"""

from pathlib import Path

import pytest

from idp_sdk._core.template_transform import GovCloudTemplateTransformer

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


def _template_with_cloudfront():
    """A synthetic template exercising the CloudFront Fn::If / resource shapes."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Test",
        "Parameters": {
            "WebUIHosting": {
                "Type": "String",
                "Default": "CloudFront",
                "AllowedValues": ["CloudFront", "APIGateway"],
            },
            "CloudFrontPriceClass": {"Type": "String", "Default": "PriceClass_100"},
            "CloudFrontAllowedGeos": {"Type": "String", "Default": ""},
            "LambdaWebAdapterLayerArn": {"Type": "String", "Default": ""},
        },
        "Conditions": {
            "UseCloudFrontHosting": {
                "Fn::Equals": [{"Ref": "WebUIHosting"}, "CloudFront"]
            },
            "HasLambdaWebAdapterLayerArn": {
                "Fn::Not": [{"Fn::Equals": [{"Ref": "LambdaWebAdapterLayerArn"}, ""]}]
            },
            "UseApiGatewayHosting": {
                "Fn::Equals": [{"Ref": "WebUIHosting"}, "APIGateway"]
            },
            "ShouldEnableGeoRestriction": {
                "Fn::Not": [{"Fn::Equals": [{"Ref": "CloudFrontAllowedGeos"}, ""]}]
            },
        },
        "Resources": {
            # Core resources the transform must keep.
            "InputBucket": {"Type": "AWS::S3::Bucket"},
            "OutputBucket": {"Type": "AWS::S3::Bucket"},
            "WorkingBucket": {"Type": "AWS::S3::Bucket"},
            "TrackingTable": {"Type": "AWS::DynamoDB::Table"},
            "ConfigurationTable": {"Type": "AWS::DynamoDB::Table"},
            "CustomerManagedEncryptionKey": {"Type": "AWS::KMS::Key"},
            "PATTERNSTACK": {"Type": "AWS::CloudFormation::Stack"},
            "WebUIBucket": {"Type": "AWS::S3::Bucket"},
            "WebUIProxyRole": {
                "Type": "AWS::IAM::Role",
                "Condition": "UseApiGatewayHosting",
            },
            # CloudFront resources — must all be removed.
            "CloudFrontOriginAccessControl": {
                "Type": "AWS::CloudFront::OriginAccessControl",
                "Condition": "UseCloudFrontHosting",
            },
            "SecurityHeadersPolicy": {
                "Type": "AWS::CloudFront::ResponseHeadersPolicy",
                "Condition": "UseCloudFrontHosting",
            },
            "CloudFrontDistribution": {
                "Type": "AWS::CloudFront::Distribution",
                "Condition": "UseCloudFrontHosting",
                "Properties": {"Foo": {"Ref": "SecurityHeadersPolicy"}},
            },
            # A kept resource whose CORS origin uses the CloudFront Fn::If shape.
            "SomeBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "CorsConfiguration": {
                        "CorsRules": [
                            {
                                "AllowedOrigins": [
                                    {
                                        "Fn::If": [
                                            "UseCloudFrontHosting",
                                            {
                                                "Fn::Sub": "https://${CloudFrontDistribution.DomainName}"
                                            },
                                            "https://api.example.com",
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                },
            },
            # A Lambda Function URL (not available in GovCloud) + its permission.
            "ChatStreamProcessorUrl": {
                "Type": "AWS::Lambda::Url",
                "Properties": {
                    "AuthType": "AWS_IAM",
                    "TargetFunctionArn": {"Ref": "ChatStreamProcessorFunction"},
                },
            },
            "ChatStreamProcessorUrlPermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "Action": "lambda:InvokeFunctionUrl",
                    # The real permission names the function (template.yaml:7881).
                    # Keep the fixture faithful: without this the "permission
                    # targeting a removed function" pruning branch is never
                    # exercised by any test.
                    "FunctionName": {"Ref": "ChatStreamProcessorFunction"},
                },
            },
            # A permission that does NOT match the InvokeFunctionUrl action, so it
            # can only be removed via the FunctionName reference check.
            "ChatStreamProcessorInvokePermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "Action": "lambda:InvokeFunction",
                    "FunctionName": {"Ref": "ChatStreamProcessorFunction"},
                },
            },
            # A standalone managed policy whose ONLY statement targets the removed
            # function: the whole resource must go (an empty PolicyDocument is
            # invalid), exercising the Properties.PolicyDocument branch.
            "ChatStreamManagedPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["lambda:InvokeFunction"],
                                "Resource": {
                                    "Fn::GetAtt": ["ChatStreamProcessorFunction", "Arn"]
                                },
                            }
                        ],
                    }
                },
            },
            # A surviving resource whose logical id CONTAINS a removed one, plus a
            # policy referencing it via Fn::Sub. Neither may be collateral damage:
            # prefix-matching would prune the statement, and a raw substring scan
            # in validation would fail the publish for a non-reason.
            "ChatStreamProcessorFunctionAlarm": {"Type": "AWS::CloudWatch::Alarm"},
            "AlarmReaderRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "ReadAlarm",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["cloudwatch:DescribeAlarms"],
                                        "Resource": {
                                            "Fn::Sub": "${ChatStreamProcessorFunctionAlarm}"
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            },
            # An unrelated function with an intentional empty Policies list — the
            # transform must not rewrite resources it pruned nothing from.
            "UnrelatedFunction": {
                "Type": "AWS::Serverless::Function",
                "Properties": {"Policies": []},
            },
            # The Function URL's handler: it layers in the AWS Lambda Web Adapter,
            # published only in the commercial partition, so it cannot be created
            # in GovCloud either (403 lambda:GetLayerVersion) and must go too.
            "ChatStreamProcessorFunction": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "Handler": "run.sh",
                    "Layers": [
                        {"Ref": "IDPCommonBaseLayer"},
                        {
                            "Fn::If": [
                                "HasLambdaWebAdapterLayerArn",
                                {"Ref": "LambdaWebAdapterLayerArn"},
                                {
                                    "Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:753240598075:layer:LambdaAdapterLayerX86:25"
                                },
                            ]
                        },
                    ],
                    "LoggingConfig": {
                        "LogGroup": {"Ref": "ChatStreamProcessorLogGroup"}
                    },
                },
            },
            "ChatStreamProcessorLogGroup": {"Type": "AWS::Logs::LogGroup"},
            "IDPCommonBaseLayer": {"Type": "AWS::Serverless::LayerVersion"},
            # A function WITHOUT the LWA layer must survive untouched.
            "AgentChatProcessorFunction": {
                "Type": "AWS::Serverless::Function",
                "Properties": {"Layers": [{"Ref": "IDPCommonBaseLayer"}]},
            },
            # The invoke grant for the removed function. Its ONLY statement
            # targets that function, so pruning must drop the whole inline policy
            # — IAM rejects `Statement: []` with "Syntax errors in policy" (400).
            "CognitoAuthorizedRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "ChatStreamInvoke",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["lambda:InvokeFunction"],
                                        "Resource": {
                                            "Fn::GetAtt": [
                                                "ChatStreamProcessorFunction",
                                                "Arn",
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                        {
                            "PolicyName": "MixedResources",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": ["lambda:InvokeFunction"],
                                        "Resource": [
                                            {
                                                "Fn::GetAtt": [
                                                    "ChatStreamProcessorFunction",
                                                    "Arn",
                                                ]
                                            },
                                            {
                                                "Fn::GetAtt": [
                                                    "AgentChatProcessorFunction",
                                                    "Arn",
                                                ]
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                    ]
                },
            },
            # A resource that references the Function URL (like the UI CodeBuild
            # env var VITE_STREAM_URL) — must be blanked, not left dangling.
            "UICodeBuildProject": {
                "Type": "AWS::CodeBuild::Project",
                "Properties": {
                    "Environment": {
                        "EnvironmentVariables": [
                            {
                                "Name": "VITE_STREAM_URL",
                                "Value": {
                                    "Fn::GetAtt": [
                                        "ChatStreamProcessorUrl",
                                        "FunctionUrl",
                                    ]
                                },
                            }
                        ]
                    }
                },
            },
            # A LoggingBucket policy with the CloudFront-service statement.
            "LoggingBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Sid": "AllowCloudFrontLogs",
                                "Effect": "Allow",
                                "Principal": {
                                    "Service": {
                                        "Fn::Sub": "cloudfront.${AWS::URLSuffix}"
                                    }
                                },
                                "Action": ["s3:PutObject"],
                            },
                            {
                                "Sid": "KeepThis",
                                "Effect": "Allow",
                                "Principal": {"Service": "logging.s3.amazonaws.com"},
                                "Action": ["s3:PutObject"],
                            },
                        ]
                    }
                },
            },
        },
        "Outputs": {
            "ApplicationWebURL": {
                "Value": {
                    "Fn::If": [
                        "UseCloudFrontHosting",
                        {"Fn::Sub": "https://${CloudFrontDistribution.DomainName}/"},
                        {"Fn::Sub": "${APIRESOLVERSTACK.Outputs.HttpApiEndpoint}/"},
                    ]
                }
            },
            # An output that GetAtts the removed Function URL — must be dropped.
            "ChatStreamUrlOutput": {
                "Value": {"Fn::GetAtt": ["ChatStreamProcessorUrl", "FunctionUrl"]}
            },
        },
    }


def _all_cloudfront_types(template):
    return [
        name
        for name, res in template.get("Resources", {}).items()
        if isinstance(res, dict)
        and str(res.get("Type", "")).startswith("AWS::CloudFront::")
    ]


def test_cloudfront_resources_removed():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    assert _all_cloudfront_types(result) == []
    for name in (
        "CloudFrontDistribution",
        "CloudFrontOriginAccessControl",
        "SecurityHeadersPolicy",
    ):
        assert name not in result["Resources"]


def test_use_cloudfront_condition_removed():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    assert "UseCloudFrontHosting" not in result.get("Conditions", {})
    # UseApiGatewayHosting must survive (the UI is still served via API Gateway).
    assert "UseApiGatewayHosting" in result["Conditions"]


def test_webui_hosting_forced_to_apigateway():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    param = result["Parameters"]["WebUIHosting"]
    assert param["AllowedValues"] == ["APIGateway"]
    assert param["Default"] == "APIGateway"


def test_cloudfront_only_parameters_removed():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    assert "CloudFrontPriceClass" not in result["Parameters"]
    assert "CloudFrontAllowedGeos" not in result["Parameters"]


def test_hosting_if_collapsed_to_else_branch():
    """Fn::If[UseCloudFrontHosting] collapses to the API-Gateway (else) value."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    cors = result["Resources"]["SomeBucket"]["Properties"]["CorsConfiguration"]
    origin = cors["CorsRules"][0]["AllowedOrigins"][0]
    assert origin == "https://api.example.com"
    web_url = result["Outputs"]["ApplicationWebURL"]["Value"]
    assert web_url == {"Fn::Sub": "${APIRESOLVERSTACK.Outputs.HttpApiEndpoint}/"}


def test_cloudfront_service_policy_statement_removed():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    stmts = result["Resources"]["LoggingBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    sids = {s.get("Sid") for s in stmts}
    assert "AllowCloudFrontLogs" not in sids
    assert "KeepThis" in sids


def test_lambda_function_url_and_permission_removed():
    """AWS::Lambda::Url (unavailable in GovCloud) + its InvokeFunctionUrl perm go."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    resources = result["Resources"]
    assert "ChatStreamProcessorUrl" not in resources
    assert "ChatStreamProcessorUrlPermission" not in resources
    # No AWS::Lambda::Url type anywhere.
    assert not [
        n
        for n, r in resources.items()
        if isinstance(r, dict) and r.get("Type") == "AWS::Lambda::Url"
    ]


def test_function_url_reference_blanked_not_dangling():
    """VITE_STREAM_URL (GetAtt <url>.FunctionUrl) is blanked, not left dangling."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    env = result["Resources"]["UICodeBuildProject"]["Properties"]["Environment"][
        "EnvironmentVariables"
    ]
    stream = next(e for e in env if e["Name"] == "VITE_STREAM_URL")
    assert stream["Value"] == ""
    # The output that GetAtt'd the URL is dropped entirely.
    assert "ChatStreamUrlOutput" not in result.get("Outputs", {})


# ---------------------------------------------------------------------------
# LWA-dependent function removal (issue #677).
#
# Removing only the AWS::Lambda::Url left ChatStreamProcessorFunction behind.
# It layers in the AWS Lambda Web Adapter, published solely by commercial-
# partition account 753240598075; because AWS account IDs do not span
# partitions, `arn:${AWS::Partition}:` substitution can never make that ARN
# resolve in GovCloud, and the deploy died with a 403 on
# lambda:GetLayerVersion that reads like a missing resource-based policy.
# ---------------------------------------------------------------------------


def test_lwa_dependent_function_removed():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    resources = result["Resources"]
    assert "ChatStreamProcessorFunction" not in resources
    # Its log group goes too (nothing else referenced it).
    assert "ChatStreamProcessorLogGroup" not in resources
    # A function that does NOT use the LWA layer is untouched.
    assert "AgentChatProcessorFunction" in resources
    assert "IDPCommonBaseLayer" in resources


def test_no_lwa_layer_reference_survives():
    """No trace of the commercial-only LWA layer may remain."""
    import yaml

    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    blob = yaml.dump(result, default_flow_style=False)
    for marker in ("753240598075", "LambdaAdapterLayer", "LambdaWebAdapterLayerArn"):
        assert marker not in blob, f"LWA marker survived: {marker}"
    # The now-dead override parameter and its condition are dropped.
    assert "LambdaWebAdapterLayerArn" not in result["Parameters"]
    assert "HasLambdaWebAdapterLayerArn" not in result.get("Conditions", {})


def test_emptied_inline_iam_policy_is_dropped_not_left_empty():
    """The implementation trap from #677: IAM rejects `Statement: []` with a 400.

    ChatStreamInvoke's only statement targets the removed function, so the whole
    inline policy must disappear — pruning the statement alone would produce an
    empty statement list and roll the stack back somewhere else entirely.
    """
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    policies = result["Resources"]["CognitoAuthorizedRole"]["Properties"]["Policies"]
    names = [p["PolicyName"] for p in policies]
    assert "ChatStreamInvoke" not in names
    for policy in policies:
        assert policy["PolicyDocument"]["Statement"], (
            f"{policy['PolicyName']} left with an empty Statement list"
        )


def test_partially_referencing_statement_keeps_surviving_resources():
    """A Resource LIST loses only the removed entries; the statement survives."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    policies = result["Resources"]["CognitoAuthorizedRole"]["Properties"]["Policies"]
    mixed = next(p for p in policies if p["PolicyName"] == "MixedResources")
    resources = mixed["PolicyDocument"]["Statement"][0]["Resource"]
    assert resources == [{"Fn::GetAtt": ["AgentChatProcessorFunction", "Arn"]}]


def test_permission_and_managed_policy_targeting_removed_function_are_removed():
    """Both reference-pruning branches that DELETE a resource must fire.

    `ChatStreamProcessorInvokePermission` can only be caught by the FunctionName
    check (its Action isn't InvokeFunctionUrl), and `ChatStreamManagedPolicy` is a
    standalone PolicyDocument whose only statement targets the removed function —
    an empty PolicyDocument is invalid, so the resource itself must go.
    """
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    resources = result["Resources"]
    assert "ChatStreamProcessorInvokePermission" not in resources
    assert "ChatStreamManagedPolicy" not in resources


def test_resource_whose_name_contains_a_removed_one_is_untouched():
    """Guards two prefix-matching hazards at once.

    `ChatStreamProcessorFunctionAlarm` survives `ChatStreamProcessorFunction`
    being removed, and the Fn::Sub statement referencing the alarm must NOT be
    pruned — nor may validation fail the publish over the shared prefix.
    """
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    resources = result["Resources"]
    assert "ChatStreamProcessorFunctionAlarm" in resources
    policies = resources["AlarmReaderRole"]["Properties"]["Policies"]
    stmts = policies[0]["PolicyDocument"]["Statement"]
    assert len(stmts) == 1, "the alarm's Fn::Sub statement was wrongly pruned"
    assert stmts[0]["Resource"] == {"Fn::Sub": "${ChatStreamProcessorFunctionAlarm}"}
    # And the shared prefix must not be reported as a dangling reference.
    assert t.validate_no_cloudfront(result) is True


def test_unrelated_resource_policies_are_not_rewritten():
    """The transform must not mutate resources it pruned nothing from."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    props = result["Resources"]["UnrelatedFunction"]["Properties"]
    assert "Policies" in props and props["Policies"] == []


def test_validation_flags_a_dangling_reference_to_a_removed_function():
    """Guard the guard: a surviving reference must fail validation, not pass."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    # Re-introduce a dangling reference of the kind the transform must prune.
    result["Outputs"]["Oops"] = {
        "Value": {"Fn::GetAtt": ["ChatStreamProcessorFunction", "Arn"]}
    }
    assert t.validate_no_cloudfront(result) is False


def test_no_dangling_cloudfront_references():
    """Nothing may reference a removed CloudFront resource or the removed condition."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    assert t.validate_no_cloudfront(result) is True


def test_description_marked_govcloud():
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_cloudfront())
    assert "GovCloud" in result["Description"]


def test_real_template_has_no_cloudfront_after_transform():
    """Transform the ACTUAL committed template.yaml; assert zero CloudFront left.

    Uses cfn-lint's CloudFormation-aware YAML decoder to parse the shorthand
    !Ref/!GetAtt/!If tags in the source template.
    """
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    template = _plain(cfnlint_decode.load(str(_repo_root() / "template.yaml")))
    assert isinstance(template, dict) and "Resources" in template

    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(template)

    # No CloudFront resource types remain.
    assert _all_cloudfront_types(result) == []
    # No AWS::Lambda::Url remains (also raises E3006 in GovCloud).
    assert not [
        n
        for n, r in result["Resources"].items()
        if isinstance(r, dict) and r.get("Type") == "AWS::Lambda::Url"
    ]
    # No GovCloud-unsupported resource types + no dangling CloudFront refs.
    assert t.validate_no_cloudfront(result) is True
    # Hosting forced to APIGateway; the API-Gateway hosting wiring survives.
    assert result["Parameters"]["WebUIHosting"]["AllowedValues"] == ["APIGateway"]
    assert "UseApiGatewayHosting" in result["Conditions"]
    assert "WebUIProxyRole" in result["Resources"]


def test_real_template_removes_the_lwa_chat_stream_family():
    """Transform the ACTUAL template.yaml; the LWA-dependent family must be gone.

    Regression guard for issue #677: stripping only ``ChatStreamProcessorUrl``
    left ``ChatStreamProcessorFunction`` (and its commercial-only LWA layer)
    behind and every ``--govcloud`` deploy rolled back on a 403
    lambda:GetLayerVersion.
    """
    import yaml

    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    loaded = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    template = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)

    assert isinstance(template, dict) and "Resources" in template
    # Sanity: the base template really does carry the LWA-dependent function, so
    # this test fails loudly if it is renamed rather than silently passing.
    assert "ChatStreamProcessorFunction" in template["Resources"]

    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(template)
    resources = result["Resources"]
    blob = yaml.dump(result, default_flow_style=False)

    for name in (
        "ChatStreamProcessorFunction",
        "ChatStreamProcessorLogGroup",
        "ChatStreamProcessorUrl",
        "ChatStreamProcessorUrlPermission",
    ):
        assert name not in resources, f"{name} survived the GovCloud transform"
    # No dangling reference anywhere (the CognitoAuthorizedRole invoke grant).
    assert "ChatStreamProcessor" not in blob
    # No reference to the commercial-only LWA layer or its override parameter.
    for marker in ("753240598075", "LambdaAdapterLayer", "LambdaWebAdapterLayerArn"):
        assert marker not in blob, f"LWA marker survived: {marker}"
    # No inline IAM policy left with an empty statement list (IAM 400).
    assert t.validate_no_cloudfront(result) is True

    # Chat still works in GovCloud: the polling transport's backing functions and
    # the UI REST API that fronts them are retained — the streaming function is
    # not in that path.
    for kept in (
        "AgentChatProcessorFunction",
        "ChatWithDocumentProcessorFunction",
        "APIRESOLVERSTACK",
    ):
        assert kept in resources, f"{kept} must survive (polling chat path)"


def test_real_template_passes_govcloud_region_cfn_lint():
    """Transform the ACTUAL template.yaml and run REAL cfn-lint for a GovCloud region.

    This is the offline "GovCloud transform + region-aware cfn-lint" fast-gate
    probe (see scripts/sdlc/docs/CI_TEST_COVERAGE.md). It is strictly stronger
    than ``validate_no_cloudfront`` / ``test_real_template_has_no_cloudfront_
    after_transform``: those only check the transformer's own hardcoded
    resource lists, whereas cfn-lint ``--region us-gov-west-1`` flags E3006 for
    *any* GovCloud-unsupported resource type — so a NEWLY introduced one (a
    future ``AWS::CloudFront::*``, ``AWS::Lambda::Url``, etc.) fails here even
    though the transformer doesn't know to strip it.

    No AWS credentials needed (cfn-lint's region check is offline). Skips
    cleanly if cfn-lint or its decoder isn't installed.
    """
    import json
    import shutil
    import subprocess  # nosec B404 - fixed args, no user input
    import tempfile

    import yaml

    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")
    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    loaded = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    # cfn_yaml.load may return the template or a (template, matches) tuple.
    template = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)

    result = GovCloudTemplateTransformer().apply_transforms(template)

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
    # unrelated E-codes from the round-tripped/plain-dumped template) are not in
    # scope for THIS probe and would make it flaky.
    e3006 = [f for f in findings if f.get("Rule", {}).get("Id") == "E3006"]
    assert e3006 == [], (
        "GovCloud-unsupported resource type(s) survived the transform "
        "(cfn-lint E3006). Add them to a strip set in template_transform.py: "
        + "; ".join(
            f"{f.get('Location', {}).get('Path')}: {f.get('Message')}" for f in e3006
        )
    )


def test_govcloud_transform_with_headless_jobs_api_passes_region_cfn_lint():
    """GovCloud transform + EnableJobsApi=true is the intended GovCloud combo.

    The GovCloud transform keeps the full UI but makes it CloudFront-free; the
    real GovCloud deployment also sets the ``EnableJobsApi=true`` CFN parameter
    to stand up the Jobs REST API (a Private API Gateway + /jobs Lambdas — see
    docs/govcloud-batch-api.md). Those Jobs-API resources are gated on
    ``DeployApiGateway`` (= EnableJobsApi), so the base region-lint test (which
    lints with the parameter at its 'false' default) never exercises them.

    Flip ``EnableJobsApi`` to default 'true' BEFORE the transform so cfn-lint
    evaluates the Jobs-API resources too, then assert the transformed template is
    still free of GovCloud-unsupported types (E3006). Catches a GovCloud-illegal
    resource type introduced specifically on the Jobs-API path.

    Offline (cfn-lint region check needs no credentials). Skips if cfn-lint is
    absent.
    """
    import json
    import shutil
    import subprocess  # nosec B404 - fixed args, no user input
    import tempfile

    import yaml

    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")
    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    loaded = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    template = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)

    # Turn the Jobs API ON so its DeployApiGateway-gated resources are linted.
    enable_jobs_api = template.get("Parameters", {}).get("EnableJobsApi")
    assert enable_jobs_api is not None, (
        "EnableJobsApi parameter missing from template.yaml — the Jobs-API "
        "gate this test relies on has moved or been renamed."
    )
    enable_jobs_api["Default"] = "true"

    result = GovCloudTemplateTransformer().apply_transforms(template)

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
    e3006 = [f for f in findings if f.get("Rule", {}).get("Id") == "E3006"]
    assert e3006 == [], (
        "GovCloud-unsupported resource type(s) survived the transform on the "
        "EnableJobsApi=true (Jobs API) path (cfn-lint E3006): "
        + "; ".join(
            f"{f.get('Location', {}).get('Path')}: {f.get('Message')}" for f in e3006
        )
    )


# ---------------------------------------------------------------------------
# GovCloud-safe default overrides (vector store / KB model / config preset).
#
# These three transforms only change parameter Defaults (and, for the preset,
# a Mappings entry + AllowedValues) so a GovCloud deploy that passes none of
# these parameters still lands on GovCloud-valid values instead of the
# commercial defaults, which fail at deploy/runtime in GovCloud. See the
# method docstrings in template_transform.py for the ValidationExceptions each
# one prevents.
# ---------------------------------------------------------------------------


def _template_with_govcloud_defaults():
    """Minimal template carrying the params/mappings the defaults transform edits.

    Uses the SAME commercial defaults as the real template.yaml so the tests
    assert the transform actually *changes* them (not just that it tolerates a
    value already set to the GovCloud one).
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Test",
        "Mappings": {
            "ConfigurationMap": {
                "lending-package-sample": {"ConfigPath": "lending-package-sample"},
            },
        },
        "Parameters": {
            "KnowledgeBaseVectorStore": {
                "Type": "String",
                "Default": "S3_VECTORS",
                "AllowedValues": ["S3_VECTORS", "OPENSEARCH_SERVERLESS"],
            },
            "KnowledgeBaseModelId": {
                "Type": "String",
                "Default": "us.amazon.nova-pro-v1:0",
                "AllowedValues": [
                    "us.amazon.nova-pro-v1:0",
                    "amazon.nova-pro-v1:0",
                    "amazon.nova-lite-v1:0",
                    "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0",
                ],
            },
            "ConfigurationPreset": {
                "Type": "String",
                "Default": "lending-package-sample",
                "AllowedValues": ["lending-package-sample", "rvl-cdip"],
            },
        },
        "Resources": {"InputBucket": {"Type": "AWS::S3::Bucket"}},
    }


def test_vector_store_default_forced_to_opensearch():
    """S3 Vectors is unsupported for Bedrock KBs in GovCloud → default to OSS."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_govcloud_defaults())
    param = result["Parameters"]["KnowledgeBaseVectorStore"]
    assert param["Default"] == "OPENSEARCH_SERVERLESS"
    # Both values are KEPT (unlike WebUIHosting, which is forced to one) so a
    # caller can still opt into S3_VECTORS if/when GovCloud supports it.
    assert set(param["AllowedValues"]) == {"S3_VECTORS", "OPENSEARCH_SERVERLESS"}


def test_kb_model_default_forced_to_govcloud_verified():
    """Commercial us. inference-profile default is invalid in GovCloud."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_govcloud_defaults())
    param = result["Parameters"]["KnowledgeBaseModelId"]
    assert param["Default"] == GovCloudTemplateTransformer.GOVCLOUD_KB_MODEL_DEFAULT
    assert param["Default"] == "amazon.nova-pro-v1:0"
    # AllowedValues untouched (the GovCloud-safe entries are already present).
    assert "amazon.nova-pro-v1:0" in param["AllowedValues"]


def test_configuration_preset_default_forced_to_govcloud_sample():
    """Preset default flips to the GovCloud sample, added to map + AllowedValues."""
    t = GovCloudTemplateTransformer()
    result = t.apply_transforms(_template_with_govcloud_defaults())
    preset = result["Parameters"]["ConfigurationPreset"]
    assert preset["Default"] == "lending-package-sample-govcloud"
    # Selectable in the dropdown, listed first.
    assert preset["AllowedValues"][0] == "lending-package-sample-govcloud"
    # The commercial presets are still selectable (not removed).
    assert "lending-package-sample" in preset["AllowedValues"]
    # And the ConfigurationMap gained the GovCloud sample's ConfigPath entry.
    cfg_map = result["Mappings"]["ConfigurationMap"]
    assert cfg_map["lending-package-sample-govcloud"] == {
        "ConfigPath": "lending-package-sample-govcloud"
    }


def test_govcloud_default_overrides_are_idempotent():
    """Re-running the transform on already-GovCloud values is a no-op (no dup)."""
    t = GovCloudTemplateTransformer()
    once = t.apply_transforms(_template_with_govcloud_defaults())
    twice = t.apply_transforms(once)
    preset = twice["Parameters"]["ConfigurationPreset"]
    # 'lending-package-sample-govcloud' inserted exactly once, not twice.
    assert preset["AllowedValues"].count("lending-package-sample-govcloud") == 1
    assert twice["Parameters"]["KnowledgeBaseVectorStore"]["Default"] == (
        "OPENSEARCH_SERVERLESS"
    )
    assert twice["Parameters"]["KnowledgeBaseModelId"]["Default"] == (
        "amazon.nova-pro-v1:0"
    )
