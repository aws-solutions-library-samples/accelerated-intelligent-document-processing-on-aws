"""Offline tests for terraform/tools/parity.py (``make terraform-parity``).

Recorded response shapes stand in for CloudFormation, S3, Cloud Control and
Lambda; nothing is called. What is pinned: both trees are read down every
nested stack, names that necessarily differ are normalised away, a real
property difference is reported unless the baseline explains it, a resource on
one side only fails, and a baseline entry that explained nothing is stale.
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
