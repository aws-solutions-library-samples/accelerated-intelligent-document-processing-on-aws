"""Offline tests for terraform/tools/parity.py (``make terraform-parity``).

Recorded response shapes stand in for CloudFormation, S3, Cloud Control and
Lambda; nothing is called. What is pinned: both trees are read down every
nested stack, names that necessarily differ are normalised away, a real
property difference is reported unless the baseline explains it, a resource on
one side only fails, and a baseline entry that explained nothing is stale; and,
from a full-stack comparison, that a name is replaced only as a whole token,
compound Cloud Control identifiers are built and an unreadable one is listed
rather than fatal, generated queue names, key ARNs, empty VPC configurations and
CloudFormation's own tags are representation rather than differences, and a
per-target policy is compared through its target.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "terraform" / "tools" / "parity.py"
ACCOUNT = "123456789012"
ARN = f"arn:aws:cloudformation:us-west-2:{ACCOUNT}:stack"


@pytest.fixture(scope="module")
def parity():
    spec = importlib.util.spec_from_file_location("_parity", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, StackName):
        return [{"StackResourceSummaries": self.pages[StackName]}]


class Cfn:
    def __init__(self, stacks, described=None):
        self.stacks = stacks
        self.described = described or {}

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return Paginator(self.stacks)

    def describe_stacks(self, StackName):
        return {"Stacks": [self.described[StackName]]}


class S3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[(Bucket, Key)]).encode())}


class CloudControl:
    def __init__(self, props):
        self.props = props

    def get_resource(self, TypeName, Identifier):
        return {
            "ResourceDescription": {
                "Properties": json.dumps(self.props[(TypeName, Identifier)])
            }
        }


class Lambda:
    def get_function(self, FunctionName):
        return {"Code": {"Location": "https://presigned"}}


def summary(lid, rtype, physical):
    return {
        "LogicalResourceId": lid,
        "ResourceType": rtype,
        "PhysicalResourceId": physical,
    }


CFN_NESTED = f"{ARN}/idp-PATTERNSTACK-1AB2C3/uuid"
CFN = Cfn(
    {
        "idp": [
            summary("InputBucket", "AWS::S3::Bucket", "idp-inputbucket-x1y2"),
            summary("PATTERNSTACK", "AWS::CloudFormation::Stack", CFN_NESTED),
            summary("GetDomain", "Custom::GetDomain", "dom"),
        ],
        CFN_NESTED: [
            summary(
                "OcrFunction", "AWS::Lambda::Function", "idp-PATTERNSTACK-1AB2C3-Ocr-Z9"
            ),
        ],
    }
)
DIRECTORY = {
    "root": {
        "resources": [
            summary("InputBucket", "AWS::S3::Bucket", "tfidp-inputbucket-20261107"),
            summary(
                "PATTERNSTACK",
                "AWS::CloudFormation::Stack",
                f"{ARN}/tfidp-PATTERNSTACK/terraform",
            ),
            summary("GetDomain", "Custom::GetDomain", "dom"),
        ],
        "outputs": {},
        "nested": {
            "PATTERNSTACK": {
                "resources": [
                    summary(
                        "OcrFunction",
                        "AWS::Lambda::Function",
                        "tfidp-PATTERNSTACK-OcrFunction-abcd1234",
                    )
                ],
                "nested": {},
            }
        },
    }
}
LABEL = {
    "StackId": f"{ARN}/tfidp/1",
    "Outputs": [
        {
            "OutputKey": "TerraformResourceDirectory",
            "OutputValue": "s3://bridge/terraform/cfn-resource-directory.json",
        }
    ],
}


def props(bucket_logging="idp-logs"):
    return {
        ("AWS::S3::Bucket", "idp-inputbucket-x1y2"): {
            "BucketName": "idp-inputbucket-x1y2",
            "LoggingConfiguration": {"DestinationBucketName": "logs"},
            "Tags": [{"Key": "aws:cloudformation:stack-name", "Value": "idp"}],
        },
        ("AWS::S3::Bucket", "tfidp-inputbucket-20261107"): {
            "BucketName": "tfidp-inputbucket-20261107",
            "LoggingConfiguration": {"DestinationBucketName": bucket_logging},
        },
        ("AWS::Lambda::Function", "idp-PATTERNSTACK-1AB2C3-Ocr-Z9"): {
            "FunctionName": "idp-PATTERNSTACK-1AB2C3-Ocr-Z9",
            "Environment": {"Variables": {"STACK": "idp-PATTERNSTACK-1AB2C3"}},
            "Arn": "arn:1",
        },
        ("AWS::Lambda::Function", "tfidp-PATTERNSTACK-OcrFunction-abcd1234"): {
            "FunctionName": "tfidp-PATTERNSTACK-OcrFunction-abcd1234",
            "Environment": {"Variables": {"STACK": "tfidp-PATTERNSTACK"}},
            "Arn": "arn:2",
            "Tags": [{"Key": "idp:stack-name", "Value": "tfidp-PATTERNSTACK"}],
        },
    }


def run(parity, cc_props, baseline=(), tf_dir=DIRECTORY):
    tf_side = parity.tf_tree(
        Cfn({}, {"tfidp": LABEL}),
        S3({("bridge", "terraform/cfn-resource-directory.json"): tf_dir}),
        "tfidp",
    )
    return parity.compare(
        parity.cfn_tree(CFN, "idp"),
        tf_side,
        CloudControl(cc_props),
        Lambda(),
        list(baseline),
        log=lambda _: None,
    )


def test_both_trees_are_read_down_every_nested_stack(parity):
    resources, names = parity.cfn_tree(CFN, "idp")
    assert {r.identity for r in resources} == {
        "InputBucket",
        "PATTERNSTACK",
        "GetDomain",
        "PATTERNSTACK/OcrFunction",
    }
    assert names == {"": "idp", "PATTERNSTACK": "idp-PATTERNSTACK-1AB2C3"}
    _, tf_names = parity.tf_tree(
        Cfn({}, {"tfidp": LABEL}),
        S3({("bridge", "terraform/cfn-resource-directory.json"): DIRECTORY}),
        "tfidp",
    )
    assert tf_names == {"": "tfidp", "PATTERNSTACK": "tfidp-PATTERNSTACK"}


def test_names_that_must_differ_are_normalised_away(parity):
    """Stack names, nested stack names, physical ids and tool tags differ on
    every pair; none is a finding."""
    report = run(parity, props(bucket_logging="logs"))
    assert report.compared == 2
    assert report.differences == []
    assert report.ok


def test_a_property_difference_is_reported(parity):
    report = run(parity, props(bucket_logging="elsewhere"))
    assert report.differences == [
        (
            "InputBucket",
            ".LoggingConfiguration.DestinationBucketName",
            'cloudformation="logs" terraform="elsewhere"',
        )
    ]
    assert not report.ok


def test_an_explained_difference_passes_and_an_unused_entry_is_stale(parity):
    entry = {
        "type": "AWS::S3::Bucket",
        "path": ".LoggingConfiguration",
        "reason": "r",
    }
    unused = {"type": "AWS::SQS::Queue", "path": ".X", "reason": "r"}
    report = run(parity, props(bucket_logging="elsewhere"), [entry, unused])
    assert report.differences == []
    assert len(report.explained) == 1
    assert report.stale == [("AWS::SQS::Queue", ".X")]
    assert not report.ok


def test_a_resource_on_one_side_only_fails(parity):
    tf_dir = json.loads(json.dumps(DIRECTORY))
    tf_dir["root"]["nested"]["PATTERNSTACK"]["resources"] = []
    p = props(bucket_logging="logs")
    report = run(parity, p, tf_dir=tf_dir)
    assert report.only_cfn == ["PATTERNSTACK/OcrFunction"]
    assert not report.ok


def test_the_committed_baseline_has_a_reason_per_entry():
    baseline = json.loads((SCRIPT.parent / "parity_baseline.json").read_text())
    keys = [(e["type"], e["path"]) for e in baseline["entries"]]
    assert len(keys) == len(set(keys))
    assert all(e["reason"].strip() for e in baseline["entries"])


# ------------------------------------------------- normalising and identifiers
# The tests below build both sides directly from Resource lists, shaped on a
# full-stack 0.6.16 comparison: Cloud Control reads, CloudFormation's schemas
# and templates, and SQS's queue attributes are recorded dicts.

Q = f"https://sqs.us-west-2.amazonaws.com/{ACCOUNT}"
SQS_ARN = f"arn:aws:sqs:us-west-2:{ACCOUNT}"
KEY_ID = "1111aaaa-2222-bbbb-3333-cccc4444dddd"
TF_KEY_ID = "5555eeee-6666-ffff-7777-0000aaaa1111"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class ReadingCloudControl:
    """Cloud Control that records each identifier it was asked for and raises
    the given error code for the identifiers in ``errors``."""

    def __init__(self, props, errors=None):
        self.props, self.errors, self.asked = props, errors or {}, []

    def get_resource(self, TypeName, Identifier):
        self.asked.append((TypeName, Identifier))
        if Identifier in self.errors:
            raise ClientError(self.errors[Identifier])
        if (TypeName, Identifier) not in self.props:
            raise ClientError("ValidationException")
        return {
            "ResourceDescription": {
                "Properties": json.dumps(self.props[(TypeName, Identifier)])
            }
        }


class SchemaCfn:
    """DescribeType and GetTemplate, as the parity tool reads them."""

    def __init__(self, primary, templates):
        self.primary, self.templates = primary, templates

    def describe_type(self, Type, TypeName):
        assert Type == "RESOURCE"
        ids = self.primary.get(TypeName, ["Id"])
        return {
            "Schema": json.dumps(
                {"primaryIdentifier": [f"/properties/{p}" for p in ids]}
            )
        }

    def get_template(self, StackName, TemplateStage):
        return {"TemplateBody": self.templates[StackName]}


class Sqs:
    def __init__(self, policies, fail=False):
        self.policies, self.fail = policies, fail

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        if self.fail:
            raise ClientError("AccessDenied")
        policy = self.policies.get(QueueUrl)
        return {"Attributes": {"Policy": policy} if policy else {}}


def side(name, *resources):
    """One side of a comparison: ``(stack path, logical id, type, physical id)``
    rows under the stack ``name``."""
    module = sys.modules.get("_parity")
    assert module is not None, "the parity fixture registers the tool as _parity"
    resource = module.Resource
    return ([resource(*row) for row in resources], {"": name})


def compare_sides(parity, cfn_side, tf_side, cc, **kw):
    return parity.compare(cfn_side, tf_side, cc, Lambda(), [], log=lambda _: None, **kw)


def test_a_name_is_replaced_only_as_a_whole_token(parity):
    """A group whose physical id is ``Author`` rewrote ``Authorization`` into
    ``<<AuthorGroup>Group>ization`` in a CORS header list: substitution was by
    substring, and a placeholder could itself be rewritten by a later name."""
    subs = parity.Substitutions(
        [("Author", "<AuthorGroup>"), ("AuthorGroup", "<Other>"), ("idp", "<STACK>")]
    )
    assert subs.apply("Authorization") == "Authorization"
    assert subs.apply("Author") == "<AuthorGroup>"
    assert subs.apply("group Author, then AuthorGroup") == (
        "group <AuthorGroup>, then <Other>"
    )
    assert subs.apply("idp-Queue-x") == "<STACK>-Queue-x"
    assert subs.apply("arn:aws:s3:::idp/*") == "arn:aws:s3:::<STACK>/*"
    assert parity.normalise(
        {"AllowedHeaders": ["Authorization", "Content-Type"]}, subs
    ) == {"AllowedHeaders": ["Authorization", "Content-Type"]}


def test_a_compound_identifier_is_built_from_the_template(parity):
    """A user pool group's physical id is its name; Cloud Control reads it by
    ``UserPoolId|GroupName``. Each side's pool id comes from its own resources,
    and the Terraform side is read with the CloudFormation twin's template."""
    template = {
        "Resources": {
            "AdminGroup": {
                "Type": "AWS::Cognito::UserPoolGroup",
                "Properties": {"UserPoolId": {"Ref": "UserPool"}, "GroupName": "Admin"},
            }
        }
    }
    cfn = SchemaCfn(
        {"AWS::Cognito::UserPoolGroup": ["UserPoolId", "GroupName"]},
        {"idp": template},
    )
    cc = ReadingCloudControl(
        {
            ("AWS::Cognito::UserPool", "us-west-2_cfn"): {"UserPoolName": "p"},
            ("AWS::Cognito::UserPool", "us-west-2_tf"): {"UserPoolName": "p"},
            ("AWS::Cognito::UserPoolGroup", "us-west-2_cfn|Admin"): {
                "GroupName": "Admin",
                "UserPoolId": "us-west-2_cfn",
            },
            ("AWS::Cognito::UserPoolGroup", "us-west-2_tf|Admin"): {
                "GroupName": "Admin",
                "UserPoolId": "us-west-2_tf",
            },
        }
    )
    report = compare_sides(
        parity,
        side(
            "idp",
            ("", "UserPool", "AWS::Cognito::UserPool", "us-west-2_cfn"),
            ("", "AdminGroup", "AWS::Cognito::UserPoolGroup", "Admin"),
        ),
        side(
            "tfidp",
            ("", "UserPool", "AWS::Cognito::UserPool", "us-west-2_tf"),
            ("", "AdminGroup", "AWS::Cognito::UserPoolGroup", "Admin"),
        ),
        cc,
        cfn=cfn,
    )
    assert ("AWS::Cognito::UserPoolGroup", "us-west-2_cfn|Admin") in cc.asked
    assert ("AWS::Cognito::UserPoolGroup", "us-west-2_tf|Admin") in cc.asked
    assert report.compared == 2
    assert report.unreadable == []
    assert report.ok


def test_a_service_assigned_identifier_part_is_the_physical_id(parity):
    """An API Gateway resource is read by ``RestApiId|ResourceId``: the template
    sets the API (here through a nested stack parameter and a GetAtt the parent
    resolves) but not the id the service assigned, which is the physical id."""
    parent = {
        "Resources": {
            "API": {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {"Parameters": {"ApiId": {"Ref": "RestApi"}}},
            }
        }
    }
    child = {
        "Resources": {
            "OpResource": {
                "Type": "AWS::ApiGateway::Resource",
                "Properties": {"RestApiId": {"Ref": "ApiId"}, "PathPart": "op"},
            },
            "Perm": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {"FunctionName": {"Fn::GetAtt": ["Fn", "Arn"]}},
            },
        }
    }
    cfn = SchemaCfn(
        {
            "AWS::ApiGateway::Resource": ["RestApiId", "ResourceId"],
            "AWS::Lambda::Permission": ["FunctionName", "Id"],
        },
        {"idp": parent, "idp-API-1": child},
    )
    props = {}
    for tag in ("c", "t"):
        props[("AWS::ApiGateway::RestApi", f"api{tag}")] = {"Name": "x"}
        props[("AWS::ApiGateway::Resource", f"api{tag}|res{tag}")] = {"PathPart": "op"}
        props[("AWS::Lambda::Function", f"fn{tag}")] = {"Arn": f"arn:fn{tag}"}
        props[("AWS::Lambda::Permission", f"arn:fn{tag}|perm{tag}")] = {"Action": "a"}
    cc = ReadingCloudControl(props)
    cfn_side = (
        [
            parity.Resource("", "RestApi", "AWS::ApiGateway::RestApi", "apic"),
            parity.Resource("", "API", parity.STACK_TYPE, f"{ARN}/idp-API-1/u"),
            parity.Resource("API", "OpResource", "AWS::ApiGateway::Resource", "resc"),
            parity.Resource("API", "Fn", "AWS::Lambda::Function", "fnc"),
            parity.Resource("API", "Perm", "AWS::Lambda::Permission", "permc"),
        ],
        {"": "idp", "API": "idp-API-1"},
    )
    tf_side = (
        [
            parity.Resource("", "RestApi", "AWS::ApiGateway::RestApi", "apit"),
            parity.Resource("", "API", parity.STACK_TYPE, f"{ARN}/tfidp-API/terraform"),
            parity.Resource("API", "OpResource", "AWS::ApiGateway::Resource", "rest"),
            parity.Resource("API", "Fn", "AWS::Lambda::Function", "fnt"),
            parity.Resource("API", "Perm", "AWS::Lambda::Permission", "permt"),
        ],
        {"": "tfidp", "API": "tfidp-API"},
    )
    report = compare_sides(parity, cfn_side, tf_side, cc, cfn=cfn)
    assert ("AWS::ApiGateway::Resource", "apic|resc") in cc.asked
    assert ("AWS::ApiGateway::Resource", "apit|rest") in cc.asked
    assert ("AWS::Lambda::Permission", "arn:fnt|permt") in cc.asked
    assert report.unreadable == []
    assert report.compared == 4


def test_an_identifier_that_cannot_be_built_or_read_is_listed_not_fatal(parity):
    """Cloud Control's ValidationException for an identifier it does not accept
    stopped the whole run at the first such resource. It is now listed as
    refused, counted apart from a type Cloud Control cannot read at all, as is a
    compound identifier with two parts the template does not give, which is not
    sent at all."""
    cfn = SchemaCfn(
        {
            "AWS::Cognito::UserPoolGroup": ["UserPoolId", "GroupName"],
            "AWS::Glue::Table": ["CatalogId", "DatabaseName", "TableName"],
        },
        {"idp": {"Resources": {}}},
    )
    cc = ReadingCloudControl(
        {},
        errors={
            "Admin": "ValidationException",
            "t1": "UnsupportedActionException",
            "t2": "UnsupportedActionException",
        },
    )
    report = compare_sides(
        parity,
        side(
            "idp",
            ("", "AdminGroup", "AWS::Cognito::UserPoolGroup", "Admin"),
            ("", "Table", "AWS::Glue::Table", "t1"),
        ),
        side(
            "tfidp",
            ("", "AdminGroup", "AWS::Cognito::UserPoolGroup", "Admin"),
            ("", "Table", "AWS::Glue::Table", "t2"),
        ),
        cc,
    )
    # Without a CloudFormation client the physical id is sent, as before.
    assert report.refused == ["AdminGroup (AWS::Cognito::UserPoolGroup)"]
    assert report.unreadable == ["Table (AWS::Glue::Table)"]
    cc = ReadingCloudControl({})
    report = compare_sides(
        parity,
        side("idp", ("", "Table", "AWS::Glue::Table", "t1")),
        side("tfidp", ("", "Table", "AWS::Glue::Table", "t2")),
        cc,
        cfn=cfn,
    )
    assert report.refused == ["Table (AWS::Glue::Table)"]
    assert cc.asked == []


def test_another_cloud_control_error_still_stops_the_run(parity):
    cc = ReadingCloudControl({}, errors={"b1": "ThrottlingException"})
    with pytest.raises(ClientError):
        compare_sides(
            parity,
            side("idp", ("", "B", "AWS::S3::Bucket", "b1")),
            side("tfidp", ("", "B", "AWS::S3::Bucket", "b2")),
            cc,
        )


def queue_pair(cfn_name, tf_name):
    return f"{Q}/{cfn_name}", f"{Q}/{tf_name}"


def test_generated_queue_names_and_the_arns_holding_them_are_normalised(parity):
    """Terraform names a queue with a generated string, CloudFormation with the
    stack name, logical id and a suffix. The queue's physical id is its URL, so
    the name inside it is what the queue's name, its ARN in an event source
    mapping, a dead-letter target, a redrive policy and an alarm dimension
    carry."""
    cfn_dlq, tf_dlq = queue_pair(
        "idp-DiscoveryDLQ-FZ25SCRDdF24", "PawaKXiSXj9pxDW15aTSgjmsH-pPpyg6XFGi2O"
    )
    cfn_q, tf_q = queue_pair(
        "idp-DiscoveryQueue-4gMvuJYkaz4F", "koOY85AklHsondRXMph7fSr2P-W7KjpyfNhMAQ"
    )

    def queue_props(name, dlq_name):
        return {
            "QueueName": name,
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": f"{SQS_ARN}:{dlq_name}", "maxReceiveCount": 3}
            ),
        }

    props = {}
    for dlq, q, fn in ((cfn_dlq, cfn_q, "fnc"), (tf_dlq, tf_q, "fnt")):
        dlq_name, q_name = dlq.rsplit("/", 1)[1], q.rsplit("/", 1)[1]
        props[("AWS::SQS::Queue", dlq)] = {"QueueName": dlq_name}
        props[("AWS::SQS::Queue", q)] = queue_props(q_name, dlq_name)
        props[("AWS::Lambda::EventSourceMapping", f"esm-{fn}")] = {
            "EventSourceArn": f"{SQS_ARN}:{q_name}"
        }
        props[("AWS::Lambda::Function", fn)] = {
            "DeadLetterConfig": {"TargetArn": f"{SQS_ARN}:{dlq_name}"}
        }
        props[("AWS::CloudWatch::Alarm", f"alarm-{fn}")] = {
            "Dimensions": [{"Name": "QueueName", "Value": dlq_name}]
        }
    rows = (
        ("DLQ", "AWS::SQS::Queue"),
        ("Queue", "AWS::SQS::Queue"),
        ("Mapping", "AWS::Lambda::EventSourceMapping"),
        ("Fn", "AWS::Lambda::Function"),
        ("Alarm", "AWS::CloudWatch::Alarm"),
    )
    cfn_ids = (cfn_dlq, cfn_q, "esm-fnc", "fnc", "alarm-fnc")
    tf_ids = (tf_dlq, tf_q, "esm-fnt", "fnt", "alarm-fnt")
    report = compare_sides(
        parity,
        side("idp", *[("", lid, t, i) for (lid, t), i in zip(rows, cfn_ids)]),
        side("tfidp", *[("", lid, t, i) for (lid, t), i in zip(rows, tf_ids)]),
        ReadingCloudControl(props),
    )
    assert report.compared == 5
    assert report.differences == []


def test_a_key_arn_and_its_key_id_are_the_same_key(parity):
    def bucket(key):
        return {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"KMSMasterKeyID": key}}
                ]
            }
        }

    props = {
        ("AWS::KMS::Key", KEY_ID): {"KeyId": KEY_ID},
        ("AWS::KMS::Key", TF_KEY_ID): {"KeyId": TF_KEY_ID},
        ("AWS::S3::Bucket", "bc"): bucket(KEY_ID),
        ("AWS::S3::Bucket", "bt"): bucket(
            f"arn:aws:kms:us-west-2:{ACCOUNT}:key/{TF_KEY_ID}"
        ),
    }
    report = compare_sides(
        parity,
        side(
            "idp",
            ("", "Key", "AWS::KMS::Key", KEY_ID),
            ("", "B", "AWS::S3::Bucket", "bc"),
        ),
        side(
            "tfidp",
            ("", "Key", "AWS::KMS::Key", TF_KEY_ID),
            ("", "B", "AWS::S3::Bucket", "bt"),
        ),
        ReadingCloudControl(props),
    )
    assert report.differences == []


def test_an_empty_vpc_config_is_no_vpc_but_a_real_one_still_differs(parity):
    empty = {"Ipv6AllowedForDualStack": False, "SecurityGroupIds": [], "SubnetIds": []}
    assert parity.normalise({"VpcConfig": empty, "Runtime": "x"}, []) == {
        "Runtime": "x"
    }
    real = {"SecurityGroupIds": ["sg-1"], "SubnetIds": ["subnet-1"]}
    found = parity.diff(
        parity.normalise({"VpcConfig": real}, []), parity.normalise({}, [])
    )
    assert [p for p, _ in found] == [".VpcConfig"]


def test_cloudformations_own_tags_are_dropped_in_every_shape(parity):
    """``UserPoolTags`` and an SSM parameter's ``Tags`` are maps, an identity
    pool's ``IdentityPoolTags`` a Key/Value list; all three carried
    ``aws:cloudformation:*`` tags the Terraform side does not."""
    cfn_tags = {
        "aws:cloudformation:logical-id": "UserPool",
        "aws:cloudformation:stack-name": "idp",
    }
    assert parity.normalise({"UserPoolTags": cfn_tags}, []) == {}
    assert parity.normalise({"Tags": {**cfn_tags, "team": "a"}}, []) == {
        "Tags": {"team": "a"}
    }
    listed = [{"Key": k, "Value": v} for k, v in cfn_tags.items()]
    assert parity.normalise({"IdentityPoolTags": listed}, []) == parity.normalise(
        {"IdentityPoolTags": []}, []
    )


def test_a_custom_resources_id_is_a_placeholder_though_it_is_not_compared(parity):
    """A knowledge base made by a custom resource is not compared, but its id is
    in the data source and the parameter that hold it."""
    props = {
        ("AWS::SSM::Parameter", "pc"): {"Value": "BBUMWOC77H"},
        ("AWS::SSM::Parameter", "pt"): {"Value": "YHRDPGUSYZ"},
    }
    report = compare_sides(
        parity,
        side(
            "idp",
            ("", "KnowledgeBase", "Custom::KnowledgeBase", "BBUMWOC77H"),
            ("", "Param", "AWS::SSM::Parameter", "pc"),
        ),
        side(
            "tfidp",
            ("", "KnowledgeBase", "Custom::KnowledgeBase", "YHRDPGUSYZ"),
            ("", "Param", "AWS::SSM::Parameter", "pt"),
        ),
        ReadingCloudControl(props),
    )
    assert report.differences == []
    assert report.compared == 1


def _policy_sides():
    cfn_q, tf_q = queue_pair("idp-DocumentQueue-abc12345", "tfgeneratedname12345")
    props = {
        ("AWS::SQS::Queue", cfn_q): {"QueueName": "idp-DocumentQueue-abc12345"},
        ("AWS::SQS::Queue", tf_q): {"QueueName": "tfgeneratedname12345"},
        ("AWS::IAM::Role", "rc"): {"Policies": [{"PolicyName": "p"}]},
        ("AWS::IAM::Role", "rt"): {"Policies": [{"PolicyName": "p"}]},
    }
    cfn_side = side(
        "idp",
        ("", "DocumentQueue", "AWS::SQS::Queue", cfn_q),
        ("", "DocumentQueuePolicy", "AWS::SQS::QueuePolicy", "idp-DocumentQueuePolicy"),
        ("", "Role", "AWS::IAM::Role", "rc"),
        ("", "RolePolicy", "AWS::IAM::Policy", "idp-RolePolicy-1"),
    )
    tf_side = side(
        "tfidp",
        ("", "DocumentQueue", "AWS::SQS::Queue", tf_q),
        ("", "Role", "AWS::IAM::Role", "rt"),
    )

    def policy(q):
        statement = {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": q}
        return json.dumps({"Statement": [statement]})

    return cfn_side, tf_side, props, cfn_q, tf_q, policy


def test_a_per_target_policy_is_compared_through_its_target(parity):
    """Terraform writes an SQS queue policy as one resource per queue and an
    IAM policy as one per role, with no directory entry for either, so both
    were reported only in CloudFormation. They are compared through the queue's
    policy and the role's inline policies instead."""
    cfn_side, tf_side, props, cfn_q, tf_q, policy = _policy_sides()
    cfn_arn = f"{SQS_ARN}:idp-DocumentQueue-abc12345"
    tf_arn = f"{SQS_ARN}:tfgeneratedname12345"
    sqs = Sqs({cfn_q: policy(cfn_arn), tf_q: policy(tf_arn)})
    report = compare_sides(
        parity, cfn_side, tf_side, ReadingCloudControl(props), sqs=sqs
    )
    assert report.only_cfn == []
    assert [t.split(" ")[0] for t in report.through_target] == [
        "DocumentQueuePolicy",
        "RolePolicy",
    ]
    assert report.differences == []
    assert report.ok

    # A policy Terraform did not apply is a difference on the queue.
    sqs = Sqs({cfn_q: policy(cfn_arn)})
    report = compare_sides(
        parity, cfn_side, tf_side, ReadingCloudControl(props), sqs=sqs
    )
    assert [(who, prop) for who, prop, _ in report.differences] == [
        ("DocumentQueue", ".Policy")
    ]


def test_a_queue_policy_nothing_compared_is_still_one_sided(parity):
    cfn_side, tf_side, props, *_ = _policy_sides()
    report = compare_sides(parity, cfn_side, tf_side, ReadingCloudControl(props))
    assert report.only_cfn == ["DocumentQueuePolicy"]
    # An SQS client but no queue pair: no queue policy was read.
    no_queue = (
        [r for r in cfn_side[0] if r.cfn_type != "AWS::SQS::Queue"],
        cfn_side[1],
    )
    report = compare_sides(
        parity, no_queue, tf_side, ReadingCloudControl(props), sqs=Sqs({})
    )
    assert "DocumentQueuePolicy" in report.only_cfn
    report = compare_sides(
        parity, cfn_side, tf_side, ReadingCloudControl(props), sqs=Sqs({}, fail=True)
    )
    assert report.only_cfn == ["DocumentQueuePolicy"]
    assert report.unreadable == ["DocumentQueue (AWS::SQS::Queue Policy)"]


def test_a_list_difference_names_the_elements_that_differ(parity):
    found = parity.diff(
        {"Policies": [{"PolicyName": "a"}, {"PolicyName": "b"}]},
        {"Policies": [{"PolicyName": "a"}, {"PolicyName": "c"}]},
    )
    assert found == [
        (
            ".Policies",
            'only in cloudformation=[{"PolicyName": "b"}] '
            'only in terraform=[{"PolicyName": "c"}]',
        )
    ]


def test_an_iam_policy_with_no_compared_role_is_still_one_sided(parity):
    report = compare_sides(
        parity,
        side("idp", ("", "RolePolicy", "AWS::IAM::Policy", "idp-RolePolicy-1")),
        side("tfidp"),
        ReadingCloudControl({}),
    )
    assert report.only_cfn == ["RolePolicy"]
    assert report.through_target == []
