# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The CloudFormation hook that lets --stack-name address a Terraform deployment."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from idp_sdk._core import terraform_stack as ts

REGION = "us-west-2"
ROOT = "mystack"
NESTED_ARN = (
    "arn:aws:cloudformation:us-west-2:111122223333:stack/mystack-PATTERNSTACK/terraform"
)

DIRECTORY = {
    "_stack_id": "arn:aws:cloudformation:us-west-2:111122223333:stack/mystack/abc",
    "_stack_name": ROOT,
    "resources": [
        {
            "LogicalResourceId": "TrackingTable",
            "ResourceType": "AWS::DynamoDB::Table",
            "PhysicalResourceId": "mystack-tracking",
        },
        {
            "LogicalResourceId": "PATTERNSTACK",
            "ResourceType": "AWS::CloudFormation::Stack",
            "PhysicalResourceId": NESTED_ARN,
        },
    ],
    "outputs": {"S3InputBucketName": "in-bucket"},
    "nested": {
        "PATTERNSTACK": {
            "resources": [
                {
                    "LogicalResourceId": "DocumentProcessingWorkflow",
                    "ResourceType": "AWS::StepFunctions::StateMachine",
                    "PhysicalResourceId": "arn:aws:states:us-west-2:111122223333:stateMachine:x",
                }
            ],
            "outputs": {
                "StateMachineArn": "arn:aws:states:us-west-2:111122223333:stateMachine:x"
            },
            "nested": {},
        }
    },
}


@pytest.fixture
def hooked(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    ts.install()
    ts.reset_cache()
    with ts._lock:
        ts._roots[(REGION, ROOT)] = DIRECTORY
        ts._register_nested(REGION, DIRECTORY)
    client = boto3.client("cloudformation", region_name=REGION)
    yield client
    ts.reset_cache()


@pytest.mark.unit
def test_list_stack_resources_is_served_from_the_directory(hooked):
    pages = hooked.get_paginator("list_stack_resources").paginate(StackName=ROOT)
    got = {
        r["LogicalResourceId"]: r["PhysicalResourceId"]
        for p in pages
        for r in p["StackResourceSummaries"]
    }
    assert got == {"TrackingTable": "mystack-tracking", "PATTERNSTACK": NESTED_ARN}


@pytest.mark.unit
def test_describe_stack_resource_by_logical_id(hooked):
    detail = hooked.describe_stack_resource(
        StackName=ROOT, LogicalResourceId="TrackingTable"
    )
    assert detail["StackResourceDetail"]["PhysicalResourceId"] == "mystack-tracking"


@pytest.mark.unit
def test_nested_stack_outputs_and_resources_by_synthetic_name_and_arn(hooked):
    name = NESTED_ARN.split("/")[1]
    stack = hooked.describe_stacks(StackName=name)["Stacks"][0]
    assert {o["OutputKey"] for o in stack["Outputs"]} == {"StateMachineArn"}
    res = hooked.list_stack_resources(StackName=NESTED_ARN)["StackResourceSummaries"]
    assert res[0]["LogicalResourceId"] == "DocumentProcessingWorkflow"


@pytest.mark.unit
def test_a_cloudformation_stack_goes_to_cloudformation(hooked):
    with ts._lock:
        ts._roots[(REGION, "cfnstack")] = None
    stub = Stubber(hooked)
    stub.add_response(
        "list_stack_resources",
        {"StackResourceSummaries": []},
        {"StackName": "cfnstack"},
    )
    with stub:
        assert (
            hooked.list_stack_resources(StackName="cfnstack")["StackResourceSummaries"]
            == []
        )
    stub.assert_no_pending_responses()


@pytest.mark.unit
def test_the_label_stack_itself_is_described_by_cloudformation(hooked):
    stub = Stubber(hooked)
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": ROOT,
                    "CreationTime": "2026-01-01T00:00:00Z",
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        },
        {"StackName": ROOT},
    )
    with stub:
        assert hooked.describe_stacks(StackName=ROOT)["Stacks"][0]["StackName"] == ROOT


@pytest.mark.unit
def test_terraform_description_marker():
    assert ts.is_terraform_stack(
        "AWS GenAI IDP Accelerator (SO9027) (v0.6.13) (Terraform)"
    )
    assert not ts.is_terraform_stack("AWS GenAI IDP Accelerator (SO9027) (v0.6.13)")
    assert not ts.is_terraform_stack(None)


@pytest.mark.unit
@pytest.mark.parametrize("action", ["deploy", "delete"])
def test_deploy_and_delete_refuse_a_terraform_labelled_stack(monkeypatch, action):
    from idp_sdk._core.stack import StackDeployer

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    deployer = StackDeployer(region=REGION)
    stub = Stubber(deployer.cfn)
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": ROOT,
                    "CreationTime": "2026-01-01T00:00:00Z",
                    "StackStatus": "CREATE_COMPLETE",
                    "Description": "AWS GenAI IDP Accelerator (v0.6.13) (Terraform)",
                }
            ]
        },
        {"StackName": ROOT},
    )
    with stub, pytest.raises(ValueError, match="Terraform deployment"):
        if action == "deploy":
            deployer.deploy_stack(
                stack_name=ROOT, template_url="https://example/t.yaml"
            )
        else:
            deployer.delete_stack(stack_name=ROOT)


@pytest.mark.unit
def test_terraform_marker_followed_by_the_read_only_notice():
    assert ts.is_terraform_stack(
        "AWS GenAI IDP Accelerator (SO9027) (v0.6.13) (Terraform) - READ-ONLY label "
        "for a Terraform deployment. Updated only by Terraform; do not update this "
        "stack in CloudFormation."
    )
