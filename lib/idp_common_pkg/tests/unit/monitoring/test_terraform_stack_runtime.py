# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run-time stack lookups inside a Terraform deployment.

A function the generated module marks as a stack-tree reader carries
IDP_TF_RESOURCE_DIRECTORY. With it set, the hook answers ListStackResources for
the deployment and its nested stacks from that directory, without calling
DescribeStacks; without it, nothing is installed and CloudFormation is used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import boto3
import pytest

from idp_common.utils import terraform_stack as ts

DIRECTORY = {
    "root": {
        "resources": [
            {
                "LogicalResourceId": "PATTERNSTACK",
                "ResourceType": "AWS::CloudFormation::Stack",
                "PhysicalResourceId": "arn:aws:cloudformation:us-west-2:111122223333:stack/stk-PATTERNSTACK/terraform",
            }
        ],
        "outputs": {},
        "nested": {
            "PATTERNSTACK": {
                "resources": [
                    {
                        "LogicalResourceId": "DocumentProcessingWorkflow",
                        "ResourceType": "AWS::StepFunctions::StateMachine",
                        "PhysicalResourceId": "arn:aws:states:us-west-2:111122223333:stateMachine:stk-DocumentProcessingWorkflow",
                    }
                ],
                "outputs": {},
                "nested": {},
            }
        },
    }
}


class _FakeS3:
    def get_object(self, Bucket, Key):  # noqa: N803
        assert (Bucket, Key) == ("bridge", "terraform/cfn-resource-directory.json")
        return {"Body": SimpleNamespace(read=lambda: json.dumps(DIRECTORY).encode())}


class _NoDescribe:
    def describe_stacks(self, **_):
        raise AssertionError("inside the deployment the directory comes from the env")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    monkeypatch.setenv("STACK_NAME", "stk")
    monkeypatch.setattr(ts, "_raw_clients", lambda region: (_NoDescribe(), _FakeS3()))
    ts.reset_cache()
    yield monkeypatch
    ts.reset_cache()


@pytest.mark.unit
def test_reader_function_resolves_the_stack_tree_from_the_directory(env):
    env.setenv(ts.DIRECTORY_ENV, "s3://bridge/terraform/cfn-resource-directory.json")
    ts.install_if_terraform()
    cfn = boto3.client("cloudformation", region_name="us-west-2")
    root = cfn.list_stack_resources(StackName="stk")["StackResourceSummaries"]
    nested_arn = root[0]["PhysicalResourceId"]
    nested = cfn.list_stack_resources(StackName=nested_arn)["StackResourceSummaries"]
    assert nested[0]["LogicalResourceId"] == "DocumentProcessingWorkflow"


@pytest.mark.unit
def test_nothing_is_installed_without_the_directory_env(env, monkeypatch):
    calls = []
    monkeypatch.setattr(ts, "install", lambda: calls.append(1))
    env.delenv(ts.DIRECTORY_ENV, raising=False)
    ts.install_if_terraform()
    assert calls == []
