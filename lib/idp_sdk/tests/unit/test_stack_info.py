# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``StackInfo`` — the translation from CloudFormation to the names the SDK uses.

``idp_sdk._core.stack_info`` is the first thing almost every other operation in
this SDK calls: it reads a deployed stack's outputs and resources and returns a
flat dictionary of friendly names (``InputBucket``, ``StateMachineArn``,
``DocumentsTable`` …) that the rest of the code indexes. Nothing downstream
re-checks those values, so a wrong or silently-empty entry surfaces much later
and somewhere else — as an upload to ``""``, or as a workflow stopper that
reports "state machine ARN not found in stack outputs" for a stack that has one.

The tests are therefore about the mapping itself, and about the two different
things "absent" can mean here:

* An **output** that is missing yields ``""``. ``get_resources`` uses
  ``outputs.get(key, "")``, so a renamed output degrades to an empty string that
  every caller will treat as a name. Each such key is asserted to be exactly
  ``""`` rather than ``None`` or missing, because that is what callers see.
* A **resource** that is missing behaves two ways depending on which one it is:
  a missing ``DocumentQueue`` raises out of ``get_resources`` entirely, while a
  missing ``TrackingTable`` degrades to ``""``. Both are pinned; the asymmetry is
  the kind of thing that gets "tidied" in the wrong direction.

Most tests run against ``moto``'s CloudFormation with a real template, so the
physical ids are the ones CloudFormation itself would produce — an SQS queue's
physical id really is its URL, which is the assumption ``_get_queue_url`` rests
on and the one a hand-written stub would simply grant. Two places use a scripted
client instead, and each says why: the ``Stacks: []`` guard, which no real
CloudFormation response produces, and ``get_nested_stack_output``, where moto's
physical id for a nested stack is a bare name while the real service returns an
ARN — so moto cannot exercise the ARN parsing the source performs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core.stack_info import StackInfo, get_stack_resources

STACK_NAME = "IDP-test"
REGION = "us-east-1"
TABLE_NAME = "IDP-test-TrackingTable-1AB2C"
QUEUE_NAME = "idp-test-document-queue"

ALL_OUTPUTS = {
    "S3InputBucketName": "idp-test-inputbucket-1a2b",
    "S3OutputBucketName": "idp-test-outputbucket-1a2b",
    "S3ConfigurationBucketName": "idp-test-configurationbucket-1a2b",
    "S3EvaluationBaselineBucketName": "idp-test-evaluationbaselinebucket-1a2b",
    "S3TestSetBucketName": "idp-test-testsetbucket-1a2b",
    "LambdaLookupFunctionName": "IDP-test-DocumentStatusLookup-abc",
    "StateMachineArn": (
        "arn:aws:states:us-east-1:123456789012:stateMachine:IDP-test-StateMachine"
    ),
}


def template(
    outputs: Optional[Dict[str, str]] = None,
    *,
    with_queue: bool = True,
    with_table: bool = True,
) -> str:
    resources: Dict[str, Any] = {}
    if with_queue:
        resources["DocumentQueue"] = {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": QUEUE_NAME},
        }
    if with_table:
        resources["TrackingTable"] = {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": TABLE_NAME,
                "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
        }
    if not resources:
        # CloudFormation requires at least one resource.
        resources["Placeholder"] = {"Type": "AWS::SQS::Queue", "Properties": {}}

    body: Dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "AWS GenAI IDP Accelerator - test fixture",
        "Resources": resources,
    }
    if outputs:
        body["Outputs"] = {key: {"Value": value} for key, value in outputs.items()}
    return json.dumps(body)


def create_stack(body: str, name: str = STACK_NAME) -> None:
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=name, TemplateBody=body
    )


@pytest.fixture
def aws(aws_credentials):
    with mock_aws():
        yield aws_credentials


def count_calls(stack_info: StackInfo, operation: str) -> List[int]:
    """Count botocore calls for one CloudFormation operation.

    Used by the caching tests. Counting real calls is the only way to tell a
    cache hit from a re-fetch: both return the same dictionary.
    """
    counter = [0]

    def record(**_kwargs):
        counter[0] += 1

    stack_info.cfn.meta.events.register(
        f"before-call.cloudformation.{operation}", record
    )
    return counter


# ---------------------------------------------------------------------------
# get_resources
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_friendly_name_resolves_from_the_stack(aws):
    """The full mapping, against a stack that has everything.

    The two non-output entries are the interesting ones. ``DocumentQueueUrl``
    comes from the ``DocumentQueue`` resource's *physical id*, which for SQS is
    the queue URL itself; ``DocumentsTable`` comes from ``TrackingTable``'s
    physical id, the real table name. Both are asserted against what
    CloudFormation actually recorded rather than against a literal, so a change
    to which logical id is consulted fails here.
    """
    create_stack(template(ALL_OUTPUTS))
    cfn = boto3.client("cloudformation", region_name=REGION)
    physical = {
        summary["LogicalResourceId"]: summary["PhysicalResourceId"]
        for summary in cfn.list_stack_resources(StackName=STACK_NAME)[
            "StackResourceSummaries"
        ]
    }

    resources = StackInfo(STACK_NAME, REGION).get_resources()

    assert resources == {
        "InputBucket": ALL_OUTPUTS["S3InputBucketName"],
        "OutputBucket": ALL_OUTPUTS["S3OutputBucketName"],
        "ConfigurationBucket": ALL_OUTPUTS["S3ConfigurationBucketName"],
        "EvaluationBaselineBucket": ALL_OUTPUTS["S3EvaluationBaselineBucketName"],
        "TestSetBucket": ALL_OUTPUTS["S3TestSetBucketName"],
        "DocumentQueueUrl": physical["DocumentQueue"],
        "LookupFunctionName": ALL_OUTPUTS["LambdaLookupFunctionName"],
        "StateMachineArn": ALL_OUTPUTS["StateMachineArn"],
        "DocumentsTable": TABLE_NAME,
        "SettingsParameter": f"{STACK_NAME}-Settings",
    }
    assert resources["DocumentQueueUrl"].endswith(QUEUE_NAME)


@pytest.mark.unit
def test_a_missing_output_becomes_an_empty_string_not_a_missing_key(aws):
    """Every key is always present; a missing output degrades to ``""``.

    That is the contract callers rely on — they index the dictionary directly —
    but it also means a renamed output is invisible here and shows up downstream
    as an S3 operation against an empty bucket name. Asserting ``""`` explicitly
    (rather than ``falsy``) pins which of the two behaviours this is.
    """
    create_stack(template({"StateMachineArn": ALL_OUTPUTS["StateMachineArn"]}))

    resources = StackInfo(STACK_NAME, REGION).get_resources()

    assert resources["InputBucket"] == ""
    assert resources["OutputBucket"] == ""
    assert resources["ConfigurationBucket"] == ""
    assert resources["EvaluationBaselineBucket"] == ""
    assert resources["TestSetBucket"] == ""
    assert resources["LookupFunctionName"] == ""
    # The ones that do not come from outputs are unaffected.
    assert resources["StateMachineArn"] == ALL_OUTPUTS["StateMachineArn"]
    assert resources["DocumentsTable"] == TABLE_NAME
    assert resources["SettingsParameter"] == f"{STACK_NAME}-Settings"


@pytest.mark.unit
def test_a_stack_with_no_outputs_at_all_still_resolves_its_resources(aws):
    create_stack(template(None))

    resources = StackInfo(STACK_NAME, REGION).get_resources()

    assert resources["DocumentsTable"] == TABLE_NAME
    assert resources["DocumentQueueUrl"].endswith(QUEUE_NAME)
    assert resources["StateMachineArn"] == ""


@pytest.mark.unit
def test_the_settings_parameter_name_is_derived_and_never_verified(aws):
    """``SettingsParameter`` is a string built from the stack name.

    No API call checks that the parameter exists, so this entry is non-empty even
    for a stack that has no settings parameter at all. Worth pinning because it
    is the one entry in the mapping whose presence says nothing about the
    deployment.
    """
    create_stack(template(ALL_OUTPUTS), name="IDP-other")

    resources = StackInfo("IDP-other", REGION).get_resources()

    assert resources["SettingsParameter"] == "IDP-other-Settings"


@pytest.mark.unit
def test_a_missing_document_queue_makes_the_whole_resolution_fail(aws):
    """``_get_queue_url`` raises, and ``get_resources`` does not catch it.

    So a stack that is not an IDP stack fails loudly at the first thing that asks
    for its resources, rather than returning a mapping full of empty strings.
    That is the desirable behaviour, and it is asymmetric with the tracking table
    below, which is why both are asserted.
    """
    create_stack(template(ALL_OUTPUTS, with_queue=False))

    with pytest.raises(ValueError, match="DocumentQueue not found in stack resources"):
        StackInfo(STACK_NAME, REGION).get_resources()


@pytest.mark.unit
def test_a_missing_tracking_table_degrades_to_an_empty_string(aws):
    """``_get_table_name`` swallows everything and returns ``""``.

    The consequence is concrete: ``WorkflowStopper`` reports "DocumentsTable not
    found in stack resources" and skips aborting queued documents, while the rest
    of the command runs. That is a recoverable outcome, unlike the queue case, so
    the difference is deliberate — but it means a renamed table logical id is a
    silent partial failure.
    """
    create_stack(template(ALL_OUTPUTS, with_table=False))

    resources = StackInfo(STACK_NAME, REGION).get_resources()

    assert resources["DocumentsTable"] == ""
    assert resources["DocumentQueueUrl"].endswith(QUEUE_NAME)


@pytest.mark.unit
def test_a_stack_that_does_not_exist_raises(aws):
    """A typo in the stack name must fail immediately.

    ``_get_stack_outputs`` logs and re-raises, so the CLI surfaces
    CloudFormation's own ``ValidationError`` rather than an empty mapping.
    """
    with pytest.raises(Exception) as caught:
        StackInfo("IDP-never-deployed", REGION).get_resources()

    assert "does not exist" in str(caught.value)


@pytest.mark.unit
def test_resources_are_resolved_once_and_then_cached(aws):
    """The second call must make no CloudFormation request.

    Every operation in the SDK builds a ``StackInfo`` and asks for resources, so
    the cache is what keeps a batch run from issuing a ``DescribeStacks`` per
    document. Identity is asserted too: callers receive the same dictionary
    object, so a caller that mutated it would affect every later reader.
    """
    create_stack(template(ALL_OUTPUTS))
    stack_info = StackInfo(STACK_NAME, REGION)
    describes = count_calls(stack_info, "DescribeStacks")
    lists = count_calls(stack_info, "ListStackResources")

    first = stack_info.get_resources()
    after_first = (describes[0], lists[0])
    second = stack_info.get_resources()

    assert first is second
    assert (describes[0], lists[0]) == after_first
    assert describes[0] == 1


@pytest.mark.unit
def test_a_stack_with_no_outputs_is_re_described_every_time(aws):
    """Defect (benign): the outputs cache is guarded by truthiness, not presence.

    ``_get_stack_outputs`` returns early only ``if self._outputs_cache``, and an
    empty dict is falsy, so a stack that publishes no outputs is re-described on
    every call. The observable consequence is extra CloudFormation calls — and
    CloudFormation's ``DescribeStacks`` is throttled at a low rate, so a batch
    operation over such a stack can be throttled where an identical stack with
    one output would not be.

    ``get_resources`` masks this in normal use because its own cache is always
    non-empty; the re-fetch is reachable through ``_get_stack_outputs``, which is
    what this test calls. Pinned rather than fixed.
    """
    create_stack(template(None))
    stack_info = StackInfo(STACK_NAME, REGION)
    describes = count_calls(stack_info, "DescribeStacks")

    assert stack_info._get_stack_outputs() == {}
    assert stack_info._get_stack_outputs() == {}

    assert describes[0] == 2


@pytest.mark.unit
def test_outputs_are_cached_when_there_is_at_least_one(aws):
    create_stack(template({"StateMachineArn": ALL_OUTPUTS["StateMachineArn"]}))
    stack_info = StackInfo(STACK_NAME, REGION)
    describes = count_calls(stack_info, "DescribeStacks")

    stack_info._get_stack_outputs()
    stack_info._get_stack_outputs()

    assert describes[0] == 1


@pytest.mark.unit
def test_the_convenience_function_returns_the_same_mapping(aws):
    """``get_stack_resources`` is what most callers actually import."""
    create_stack(template(ALL_OUTPUTS))

    assert get_stack_resources(STACK_NAME, REGION) == (
        StackInfo(STACK_NAME, REGION).get_resources()
    )


@pytest.mark.unit
def test_the_clients_are_built_in_the_requested_region(aws):
    """Both clients must honour the region, including the SSM one.

    ``get_settings`` reads a parameter that only exists in the deployment's
    region, so an SSM client built in the session default would answer
    ``ParameterNotFound`` for a perfectly good stack.
    """
    stack_info = StackInfo(STACK_NAME, "eu-west-1")

    assert stack_info.cfn.meta.region_name == "eu-west-1"
    assert stack_info.ssm.meta.region_name == "eu-west-1"


# ---------------------------------------------------------------------------
# get_settings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_settings_are_read_and_parsed_from_the_stacks_ssm_parameter(aws):
    """The parameter name is ``<stack>-Settings`` and its value is JSON."""
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=f"{STACK_NAME}-Settings",
        Value=json.dumps({"IDPPattern": "unified", "MaxConcurrent": 100}),
        Type="String",
    )

    assert StackInfo(STACK_NAME, REGION).get_settings() == {
        "IDPPattern": "unified",
        "MaxConcurrent": 100,
    }


@pytest.mark.unit
def test_a_missing_settings_parameter_yields_an_empty_dict(aws):
    """Settings are optional, so their absence must not raise.

    Callers merge this into their own defaults, and an empty mapping is the right
    neutral value. A raise here would make every operation depend on a parameter
    that older deployments do not have.
    """
    assert StackInfo(STACK_NAME, REGION).get_settings() == {}


@pytest.mark.unit
def test_an_unparseable_settings_parameter_yields_an_empty_dict(aws):
    """A corrupt parameter degrades to no settings rather than a traceback.

    The failure is logged at warning level only, so the operator sees defaults
    being used without being told the parameter was unreadable. Pinned because
    the alternative reading — that empty means "no settings were set" — is wrong.
    """
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=f"{STACK_NAME}-Settings", Value="{not valid json", Type="String"
    )

    assert StackInfo(STACK_NAME, REGION).get_settings() == {}


# ---------------------------------------------------------------------------
# validate_stack
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_freshly_created_stack_validates(aws):
    create_stack(template(ALL_OUTPUTS))

    assert StackInfo(STACK_NAME, REGION).validate_stack() is True


@pytest.mark.unit
def test_a_stack_that_does_not_exist_does_not_validate(aws):
    """The failure is swallowed and reported as ``False``.

    This is the method a caller uses *before* doing anything, so it must answer
    rather than raise — the raising behaviour belongs to ``get_resources``.
    """
    assert StackInfo("IDP-never-deployed", REGION).validate_stack() is False


class ScriptedCfn:
    """A CloudFormation client returning a fixed ``describe_stacks`` response.

    Two of the source's branches cannot be reached through a real service. A
    response with an empty ``Stacks`` list is one: CloudFormation raises
    ``ValidationError`` instead of returning one, so the guard exists for a
    response shape only a stub can produce — and leaving it untested would mean
    nobody notices if it starts returning the wrong thing.
    """

    def __init__(self, response: Dict[str, Any]):
        self._response = response
        self.calls: List[str] = []

    def describe_stacks(self, StackName: str):
        self.calls.append(StackName)
        return self._response


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,expected",
    [
        ("CREATE_COMPLETE", True),
        ("UPDATE_COMPLETE", True),
        ("UPDATE_ROLLBACK_COMPLETE", True),
        ("CREATE_IN_PROGRESS", False),
        ("UPDATE_IN_PROGRESS", False),
        ("ROLLBACK_COMPLETE", False),
        ("UPDATE_ROLLBACK_FAILED", False),
        ("DELETE_IN_PROGRESS", False),
        ("DELETE_COMPLETE", False),
        ("CREATE_FAILED", False),
        ("REVIEW_IN_PROGRESS", False),
    ],
)
def test_only_the_three_settled_statuses_validate(aws, status, expected):
    """``ROLLBACK_COMPLETE`` is the case that matters.

    A stack in ``ROLLBACK_COMPLETE`` exists and can be described, but its
    resources were rolled back — so operating on it would target half-created
    infrastructure. It is deliberately not in the accepted list, and a change
    that added it (it reads like a completed state) would let the CLI run against
    a failed deployment.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = ScriptedCfn({"Stacks": [{"StackStatus": status}]})

    assert stack_info.validate_stack() is expected


@pytest.mark.unit
def test_an_empty_stack_list_does_not_validate(aws):
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = ScriptedCfn({"Stacks": []})

    assert stack_info.validate_stack() is False


@pytest.mark.unit
def test_an_empty_stack_list_raises_from_the_output_reader(aws):
    """The same response shape is an error, not a ``False``, when reading outputs.

    ``_get_stack_outputs`` raises ``ValueError("Stack not found: …")``. Only a
    scripted client can produce this, for the reason given on ``ScriptedCfn``.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = ScriptedCfn({"Stacks": []})

    with pytest.raises(ValueError, match=f"Stack not found: {STACK_NAME}"):
        stack_info._get_stack_outputs()


# ---------------------------------------------------------------------------
# _get_table_name and _get_queue_url error handling
# ---------------------------------------------------------------------------


class BrokenPaginator:
    """A client whose ``list_stack_resources`` paginator fails."""

    def get_paginator(self, operation_name: str):
        raise RuntimeError(f"AccessDenied on {operation_name}")

    def describe_stacks(self, StackName: str):
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": []}]}


@pytest.mark.unit
def test_a_table_lookup_that_fails_returns_an_empty_string(aws):
    """``_get_table_name`` catches everything, including an access denial.

    An IAM role without ``cloudformation:ListStackResources`` therefore produces
    a stack whose ``DocumentsTable`` is ``""`` — indistinguishable from a stack
    that has no tracking table. Pinned because the permission failure is the
    likelier cause in practice and nothing reports it above warning level.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = BrokenPaginator()

    assert stack_info._get_table_name("TrackingTable") == ""


@pytest.mark.unit
def test_a_queue_lookup_that_fails_raises(aws):
    """``_get_queue_url`` re-raises, so the same permission gap is fatal here.

    The two helpers read the same API through the same client and disagree about
    what a failure means. That is the asymmetry this pair of tests exists to make
    visible.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = BrokenPaginator()

    with pytest.raises(RuntimeError, match="AccessDenied on list_stack_resources"):
        stack_info._get_queue_url()


# ---------------------------------------------------------------------------
# get_nested_stack_output
# ---------------------------------------------------------------------------


class NestedStackCfn:
    """A CloudFormation client shaped like the real service's nested stacks.

    ``get_nested_stack_output`` takes the nested stack's ``PhysicalResourceId``
    and reads ``split("/")[1]`` from it, which is correct for the real service
    (``arn:aws:cloudformation:<region>:<account>:stack/<name>/<id>``) and wrong
    for ``moto``, which returns a bare generated name with no slashes. So moto
    cannot exercise this method at all — the ARN parsing is the behaviour under
    test, and a fake that returns a name makes the code raise ``IndexError``.
    """

    def __init__(self, resources: List[Dict[str, str]], outputs: Dict[str, Any]):
        self._resources = resources
        self._outputs = outputs
        self.described: List[str] = []

    def describe_stack_resources(self, StackName: str):
        return {"StackResources": self._resources}

    def describe_stacks(self, StackName: str):
        self.described.append(StackName)
        if StackName not in self._outputs:
            raise ValueError(f"Stack not found: {StackName}")
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": key, "OutputValue": value}
                        for key, value in self._outputs[StackName].items()
                    ]
                }
            ]
        }


def nested_resource(logical_id: str, stack_name: str) -> Dict[str, str]:
    return {
        "LogicalResourceId": logical_id,
        "ResourceType": "AWS::CloudFormation::Stack",
        "PhysicalResourceId": (
            "arn:aws:cloudformation:us-east-1:123456789012:stack/"
            f"{stack_name}/1234abcd-5678-90ef-ghij-klmnopqrstuv"
        ),
    }


@pytest.mark.unit
def test_a_nested_stack_output_is_found_by_case_insensitive_pattern(aws):
    """The pattern is matched case-insensitively against the logical id.

    Callers pass ``"apiresolver"`` and the template declares
    ``APIRESOLVERSTACK``; a case-sensitive match would find nothing and raise, so
    the fold is what makes the documented call work at all. The nested stack's
    *name* is parsed out of its ARN and used for the second lookup, which is the
    part moto cannot model.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = NestedStackCfn(
        [
            {
                "LogicalResourceId": "DocumentQueue",
                "ResourceType": "AWS::SQS::Queue",
                "PhysicalResourceId": "https://sqs.us-east-1.amazonaws.com/1/q",
            },
            nested_resource("APIRESOLVERSTACK", "IDP-test-APIRESOLVERSTACK-XYZ"),
        ],
        {
            "IDP-test-APIRESOLVERSTACK-XYZ": {
                "ApiUrl": "https://api.example.com/prod",
                "ApiId": "abc123",
            }
        },
    )

    assert (
        stack_info.get_nested_stack_output("apiresolver", "ApiUrl")
        == "https://api.example.com/prod"
    )
    assert stack_info.cfn.described == ["IDP-test-APIRESOLVERSTACK-XYZ"]


@pytest.mark.unit
def test_a_non_stack_resource_is_never_matched_by_the_pattern(aws):
    """The resource type is checked as well as the name.

    A Lambda function called ``ApiResolverFunction`` matches the pattern
    textually. Treating it as a nested stack would send its function name to
    ``describe_stacks``, and the resulting error would name the wrong thing.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = NestedStackCfn(
        [
            {
                "LogicalResourceId": "ApiResolverFunction",
                "ResourceType": "AWS::Lambda::Function",
                "PhysicalResourceId": "IDP-test-ApiResolverFunction-abc",
            }
        ],
        {},
    )

    with pytest.raises(
        ValueError, match="Nested stack matching 'apiresolver' not found"
    ):
        stack_info.get_nested_stack_output("apiresolver", "ApiUrl")


@pytest.mark.unit
def test_an_output_the_nested_stack_does_not_publish_raises(aws):
    """A missing output raises rather than returning ``None``.

    The annotation says ``Optional[str]``, which reads as though a missing output
    comes back as ``None``; it does not — the only ``None`` would come from an
    output whose value is genuinely absent. Callers must handle the exception.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = NestedStackCfn(
        [nested_resource("APIRESOLVERSTACK", "IDP-test-APIRESOLVERSTACK-XYZ")],
        {"IDP-test-APIRESOLVERSTACK-XYZ": {"ApiId": "abc123"}},
    )

    with pytest.raises(ValueError, match="Output 'ApiUrl' not found in nested stack"):
        stack_info.get_nested_stack_output("apiresolver", "ApiUrl")


@pytest.mark.unit
def test_the_first_matching_nested_stack_wins(aws):
    """Matching stops at the first resource that matches, in listing order.

    Two nested stacks whose logical ids both contain the pattern are possible in
    a template that has a ``PATTERNSTACK`` and a ``PATTERNSTACKV2``; the caller
    gets whichever CloudFormation lists first, which is not something the caller
    can control. Pinned so the ambiguity is on record.
    """
    stack_info = StackInfo(STACK_NAME, REGION)
    stack_info.cfn = NestedStackCfn(
        [
            nested_resource("PATTERNSTACK", "IDP-test-PATTERNSTACK-AAA"),
            nested_resource("PATTERNSTACKV2", "IDP-test-PATTERNSTACKV2-BBB"),
        ],
        {
            "IDP-test-PATTERNSTACK-AAA": {"Which": "first"},
            "IDP-test-PATTERNSTACKV2-BBB": {"Which": "second"},
        },
    )

    assert stack_info.get_nested_stack_output("patternstack", "Which") == "first"
