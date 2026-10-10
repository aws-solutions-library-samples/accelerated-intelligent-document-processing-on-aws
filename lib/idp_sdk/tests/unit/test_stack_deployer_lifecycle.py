# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``StackDeployer``'s deploy, poll and diagnose paths in ``idp_sdk._core.stack``.

This file covers the CloudFormation-facing half of the deployer: deciding whether
a deploy is a create or an update and what parameter list to send
(``deploy_stack``), answering what state a stack is in (``_stack_exists``,
``_get_stack_status``, ``get_stack_operation_in_progress``), interrupting and
waiting (``cancel_update_stack``, ``wait_for_stable_state``,
``monitor_stack_progress``, ``_wait_for_completion``), and explaining a failure
once one has happened (``_get_stack_outputs``, ``_get_stack_failure_reason``,
``get_deployment_failure_analysis``, ``get_stack_events``).

Two things shaped the tests.

**The update parameter list is the highest-consequence value in the module.** On
an update, a parameter the caller did not mention must be sent as
``UsePreviousValue``, a parameter the *new* template no longer declares must be
dropped entirely (or CloudFormation rejects the whole update), and a parameter
that is fixed at pool-creation time must be refused outright before any API call
(#835 — applying it leaves the stack in ``UPDATE_ROLLBACK_FAILED``). All three are
asserted against the request that actually goes out, or against what the stack
really holds afterwards, rather than against a mock's call record.

**Failure analysis is what an operator reads when a deploy breaks**, and a
nested-stack deploy reports the real cause several stacks down. The recursion,
the cascade/wrapper classification that keeps "Resource creation cancelled" from
being reported as the root cause, and the stale-event filter that stops a
*previous* deploy's error being blamed for this one are all covered here.

``moto`` drives everything it can — real stacks, real parameters, real updates,
real events — and its CloudFormation is genuinely good at this. Where it falls
short (``validate_template`` reports no parameters at all,
``cancel_update_stack`` is unimplemented, and a scripted sequence of differing
statuses cannot be produced at all) the tests use ``botocore.stub.Stubber``, which
still validates each request against the real service model. The polling loops get
a fake clock so the suite stays fast.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import ANY, Stubber
from moto import mock_aws

from idp_sdk._core import stack as stack_mod
from idp_sdk._core.stack import StackDeployer

pytestmark = pytest.mark.unit

REGION = "us-east-1"

# Every parameter is referenced, because moto's validate_template rejects an
# unused one, and the stack produces an Output so the outputs paths are real.
TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Parameters": {
        "AdminEmail": {"Type": "String"},
        "LogLevel": {"Type": "String", "Default": "INFO"},
    },
    "Resources": {
        "Topic": {
            "Type": "AWS::SNS::Topic",
            "Properties": {
                "DisplayName": {"Ref": "AdminEmail"},
                "TopicName": {"Ref": "LogLevel"},
            },
        }
    },
    "Outputs": {
        "AdminAddress": {"Value": {"Ref": "AdminEmail"}},
        "TopicArn": {"Value": {"Ref": "Topic"}},
    },
}
TEMPLATE_JSON = json.dumps(TEMPLATE)


class FakeClock:
    """Stand-in for the ``time`` module inside ``stack.py``.

    ``stack.py`` uses only ``time.time()`` and ``time.sleep()``. Replacing the
    module reference rather than patching the real ``time`` module keeps the
    substitution inside the code under test, and makes each ``sleep`` advance the
    clock by the requested amount so a timeout is reached deterministically
    instead of after ten real minutes.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(stack_mod, "time", fake)
    return fake


@pytest.fixture
def template_file(tmp_path: Path) -> Path:
    path = tmp_path / "template.json"
    path.write_text(TEMPLATE_JSON)
    return path


def _stack_response(
    status: str,
    outputs: list[dict] | None = None,
    parameters: list[dict] | None = None,
) -> dict:
    stack: dict[str, Any] = {
        "StackName": "S1",
        "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        "StackStatus": status,
    }
    if outputs is not None:
        stack["Outputs"] = outputs
    if parameters is not None:
        stack["Parameters"] = parameters
    return {"Stacks": [stack]}


def _event(
    resource: str = "Topic",
    status: str = "CREATE_FAILED",
    reason: str = "boom",
    resource_type: str = "AWS::SNS::Topic",
    physical_id: str = "",
    when: dt.datetime | None = None,
) -> dict:
    return {
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/S1/abc",
        "EventId": f"{resource}-{status}-{reason}",
        "StackName": "S1",
        "LogicalResourceId": resource,
        "PhysicalResourceId": physical_id,
        "ResourceType": resource_type,
        "Timestamp": when or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
    }


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_the_region_is_pinned_onto_the_cloudformation_client(aws_credentials: str):
    """A deployer built for one region must not talk to another — a stack created
    in the wrong region is invisible to every later call."""
    deployer = StackDeployer(region="eu-west-1")
    assert deployer.region == "eu-west-1"
    assert deployer.cfn.meta.region_name == "eu-west-1"


def test_no_region_falls_back_to_the_ambient_one(aws_credentials: str):
    deployer = StackDeployer()
    assert deployer.region is None
    assert deployer.cfn.meta.region_name == aws_credentials


# ---------------------------------------------------------------------------
# deploy_stack — create
# ---------------------------------------------------------------------------


def test_a_template_source_is_required(aws_credentials: str):
    with pytest.raises(ValueError, match="template_path or template_url"):
        StackDeployer(region=REGION).deploy_stack("S1")


def test_a_create_sends_only_the_given_parameters_and_records_the_stack_id(
    aws_credentials: str, template_file: Path
):
    """A create has no previous values, so the parameter list is exactly what the
    caller asked for; anything the template defaults is left to CloudFormation."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        result = deployer.deploy_stack(
            "S1",
            template_path=str(template_file),
            parameters={"AdminEmail": "a@b.c"},
        )

        assert result["operation"] == "CREATE"
        assert result["status"] == "INITIATED"
        assert result["stack_name"] == "S1"
        assert result["stack_id"].startswith(
            "arn:aws:cloudformation:us-east-1:123456789012:stack/S1/"
        )
        assert result["deploy_start_time"].tzinfo is not None

        live = deployer.cfn.describe_stacks(StackName="S1")["Stacks"][0]
        assert {p["ParameterKey"]: p["ParameterValue"] for p in live["Parameters"]} == {
            "AdminEmail": "a@b.c",
            "LogLevel": "INFO",
        }


def test_tags_are_applied_to_the_created_stack(
    aws_credentials: str, template_file: Path
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        deployer.deploy_stack(
            "S1",
            template_path=str(template_file),
            parameters={"AdminEmail": "a@b.c"},
            tags={"Owner": "platform", "CostCentre": "1234"},
        )
        live = deployer.cfn.describe_stacks(StackName="S1")["Stacks"][0]
        assert {t["Key"]: t["Value"] for t in live["Tags"]} == {
            "Owner": "platform",
            "CostCentre": "1234",
        }


def test_waiting_on_a_create_returns_the_stack_outputs(
    aws_credentials: str, template_file: Path
):
    with mock_aws():
        result = StackDeployer(region=REGION).deploy_stack(
            "S1",
            template_path=str(template_file),
            parameters={"AdminEmail": "a@b.c"},
            wait=True,
        )
    assert result["success"] is True
    assert result["status"] == "CREATE_COMPLETE"
    assert result["outputs"]["AdminAddress"] == "a@b.c"
    assert result["outputs"]["TopicArn"].startswith("arn:aws:sns:")


def test_a_template_url_is_used_without_reading_any_file(aws_credentials: str):
    """The URL path must not touch the filesystem — there is no local copy."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "create_stack",
            {"StackId": "arn:aws:cloudformation:us-east-1:1:stack/S1/x"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3.us-east-1.amazonaws.com/b/t.yaml",
                "Parameters": [],
                "Capabilities": ANY,
                "DisableRollback": False,
            },
        )
        result = deployer.deploy_stack(
            "S1", template_url="https://s3.us-east-1.amazonaws.com/b/t.yaml"
        )
    assert result["stack_id"] == "arn:aws:cloudformation:us-east-1:1:stack/S1/x"


@pytest.mark.parametrize("no_rollback", [True, False])
def test_no_rollback_is_forwarded_as_disable_rollback_on_create(
    aws_credentials: str, no_rollback: bool
):
    """Losing this flag destroys the failed resources an operator needs in order
    to diagnose the failure, because CloudFormation rolls the stack back and
    deletes them. Sending it when it was not asked for is worse: a failed create
    then leaves a half-built stack behind and every retry is a no-op update."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "create_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [],
                "Capabilities": ANY,
                "DisableRollback": no_rollback,
            },
        )
        deployer.deploy_stack(
            "S1", template_url="https://s3/x.yaml", no_rollback=no_rollback
        )
        stub.assert_no_pending_responses()


def test_an_update_never_sends_disable_rollback(
    aws_credentials: str, template_file: Path
):
    """``DisableRollback`` is create-only in this code path. The Stubber's expected
    parameters are exact, so an ``update_stack`` carrying the key fails here."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("CREATE_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stacks",
            _stack_response("CREATE_COMPLETE", parameters=[]),
            {"StackName": "S1"},
        )
        stub.add_response(
            "validate_template",
            {"Parameters": []},
            {"TemplateURL": "https://s3/x.yaml"},
        )
        stub.add_response(
            "update_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [],
                "Capabilities": ANY,
            },
        )
        deployer.deploy_stack("S1", template_url="https://s3/x.yaml", no_rollback=True)
        stub.assert_no_pending_responses()


def test_the_role_arn_is_forwarded_when_given(aws_credentials: str):
    """Deploying under a CloudFormation service role is how a locked-down account
    grants the deploy its permissions; dropping the ARN makes the deploy run as
    the caller and fail on the first resource."""
    deployer = StackDeployer(region=REGION)
    role = "arn:aws:iam::123456789012:role/CfnDeploy"
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "create_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [],
                "Capabilities": ANY,
                "RoleARN": role,
                "DisableRollback": True,
            },
        )
        deployer.deploy_stack(
            "S1",
            template_url="https://s3/x.yaml",
            role_arn=role,
            no_rollback=True,
        )
        stub.assert_no_pending_responses()


def test_the_three_expand_capabilities_are_always_requested(aws_credentials: str):
    """The accelerator's template uses named IAM roles and SAM transforms, so a
    missing capability fails the deploy with InsufficientCapabilities."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "create_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [],
                "Capabilities": [
                    "CAPABILITY_IAM",
                    "CAPABILITY_NAMED_IAM",
                    "CAPABILITY_AUTO_EXPAND",
                ],
                "DisableRollback": False,
            },
        )
        deployer.deploy_stack("S1", template_url="https://s3/x.yaml")
        stub.assert_no_pending_responses()


def test_an_oversized_template_is_staged_to_s3_and_referenced_by_url(
    aws_credentials: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """CloudFormation refuses an inline body over 51,200 bytes, so a large local
    template must be uploaded and passed as a URL. Both the switch and the
    uploaded bytes are checked, because a deploy that sends a stale or absent
    object fails with an unhelpful S3 error.
    """
    # moto's policy evaluator cannot model aws:SecureTransport, so the
    # EnforceSSLOnly policy the staging bucket gets would 403 the upload itself.
    monkeypatch.setattr(
        "idp_sdk._core.stack.apply_enforce_ssl_only",
        lambda *_a, **_k: True,
    )
    padding = "# " + "x" * 60000 + "\n"
    big = tmp_path / "big.json"
    big.write_text(padding + TEMPLATE_JSON)
    assert len(big.read_text().encode("utf-8")) > 51200

    import boto3

    with mock_aws():
        deployer = StackDeployer(region=REGION)
        with Stubber(deployer.cfn) as stub:
            stub.add_client_error(
                "describe_stacks",
                service_error_code="ValidationError",
                service_message="Stack with id S1 does not exist",
            )
            stub.add_response(
                "create_stack",
                {"StackId": "id"},
                {
                    "StackName": "S1",
                    "TemplateURL": ANY,
                    "Parameters": [],
                    "Capabilities": ANY,
                    "DisableRollback": False,
                },
            )
            deployer.deploy_stack("S1", template_path=str(big))

        s3 = boto3.client("s3", region_name=REGION)
        bucket = s3.list_buckets()["Buckets"][0]["Name"]
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]]
        assert len(keys) == 1 and keys[0].startswith("idp-cli/templates/S1_")
        stored = s3.get_object(Bucket=bucket, Key=keys[0])["Body"].read().decode()
    assert stored == big.read_text()


def test_a_create_collision_is_reported_with_the_remedy(aws_credentials: str):
    """``AlreadyExistsException`` only happens when the existence check raced, and
    the message has to tell the operator what to do instead."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_client_error(
            "create_stack", service_error_code="AlreadyExistsException"
        )
        with pytest.raises(ValueError, match="already exists. Use --update"):
            deployer.deploy_stack("S1", template_url="https://s3/x.yaml")


def test_any_other_create_failure_propagates(aws_credentials: str):
    """Swallowing this would report a successful deploy that never started."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_client_error("create_stack", service_error_code="AccessDenied")
        with pytest.raises(Exception, match="AccessDenied"):
            deployer.deploy_stack("S1", template_url="https://s3/x.yaml")


def test_a_creation_fixed_parameter_the_template_lacks_is_dropped_on_create(
    aws_credentials: str,
):
    """#835's create half: a caller that injects the safe default for new stacks
    must not fail a create against a template that predates the parameter.

    The Stubber's expected parameters are the assertion — the request that goes
    out must carry only ``AdminEmail``.
    """
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "AdminEmail"}]},
            {"TemplateURL": "https://s3/x.yaml"},
        )
        stub.add_response(
            "create_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [
                    {"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}
                ],
                "Capabilities": ANY,
                "DisableRollback": False,
            },
        )
        deployer.deploy_stack(
            "S1",
            template_url="https://s3/x.yaml",
            parameters={"AdminEmail": "a@b.c", "ExternalIdPEmailMutable": "true"},
        )
        stub.assert_no_pending_responses()


def test_the_template_is_not_validated_when_no_creation_fixed_parameter_is_given(
    aws_credentials: str,
):
    """``validate_template`` is an extra API call on every deploy; it is only worth
    making when a creation-fixed parameter is actually in play. The Stubber has no
    response queued for it, so a stray call fails this test."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        stub.add_response(
            "create_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/x.yaml",
                "Parameters": [
                    {"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}
                ],
                "Capabilities": ANY,
                "DisableRollback": False,
            },
        )
        deployer.deploy_stack(
            "S1", template_url="https://s3/x.yaml", parameters={"AdminEmail": "a@b.c"}
        )
        stub.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# deploy_stack — update
# ---------------------------------------------------------------------------


def test_an_update_preserves_parameters_the_caller_did_not_mention(
    aws_credentials: str, template_file: Path
):
    """End to end against moto: change one parameter, and the other must keep the
    value the stack was created with.

    Sending an explicit value for every parameter instead would overwrite an
    operator's out-of-band change; sending none would revert them to the template
    defaults. This is the single most consequential behaviour in ``deploy_stack``.
    """
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        deployer.deploy_stack(
            "S1", template_path=str(template_file), parameters={"AdminEmail": "a@b.c"}
        )
        result = deployer.deploy_stack(
            "S1", template_path=str(template_file), parameters={"LogLevel": "DEBUG"}
        )
        assert result["operation"] == "UPDATE"

        live = deployer.cfn.describe_stacks(StackName="S1")["Stacks"][0]
        assert {p["ParameterKey"]: p["ParameterValue"] for p in live["Parameters"]} == {
            "AdminEmail": "a@b.c",
            "LogLevel": "DEBUG",
        }


def test_an_update_omits_the_tags_key_so_existing_tags_survive(
    aws_credentials: str, template_file: Path
):
    """``update_stack`` replaces the whole tag set, and there is no per-tag
    UsePreviousValue — so passing no tags must mean *omit the key*, not "send an
    empty list", which would strip every tag from the stack and from every
    taggable resource under it."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        deployer.deploy_stack(
            "S1",
            template_path=str(template_file),
            parameters={"AdminEmail": "a@b.c"},
            tags={"Owner": "platform"},
        )
        deployer.deploy_stack(
            "S1", template_path=str(template_file), parameters={"LogLevel": "DEBUG"}
        )
        live = deployer.cfn.describe_stacks(StackName="S1")["Stacks"][0]
        assert {t["Key"]: t["Value"] for t in live["Tags"]} == {"Owner": "platform"}


def test_a_parameter_the_new_template_dropped_is_not_sent(aws_credentials: str):
    """Sending a parameter the new template no longer declares makes
    CloudFormation reject the entire update, so an upgrade across a release that
    removed a parameter would be impossible."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            _stack_response("CREATE_COMPLETE"),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "CREATE_COMPLETE",
                parameters=[
                    {"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"},
                    {"ParameterKey": "EnableHITL", "ParameterValue": "true"},
                ],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "AdminEmail"}]},
            {"TemplateURL": "https://s3/new.yaml"},
        )
        stub.add_response(
            "update_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/new.yaml",
                "Parameters": [
                    {"ParameterKey": "AdminEmail", "UsePreviousValue": True}
                ],
                "Capabilities": ANY,
            },
        )
        deployer.deploy_stack("S1", template_url="https://s3/new.yaml")
        stub.assert_no_pending_responses()


def test_a_brand_new_parameter_is_sent_with_its_value(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("CREATE_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "CREATE_COMPLETE",
                parameters=[{"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "AdminEmail"}, {"ParameterKey": "New"}]},
            {"TemplateURL": "https://s3/new.yaml"},
        )
        stub.add_response(
            "update_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/new.yaml",
                "Parameters": [
                    {"ParameterKey": "AdminEmail", "UsePreviousValue": True},
                    {"ParameterKey": "New", "ParameterValue": "42"},
                ],
                "Capabilities": ANY,
            },
        )
        deployer.deploy_stack(
            "S1", template_url="https://s3/new.yaml", parameters={"New": "42"}
        )
        stub.assert_no_pending_responses()


def test_an_unknown_template_parameter_set_preserves_everything(aws_credentials: str):
    """When ``validate_template`` fails, the empty set must mean "keep them all"
    rather than "the template declares nothing" — otherwise a transient
    validation failure would strip every parameter from the update."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("CREATE_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "CREATE_COMPLETE",
                parameters=[
                    {"ParameterKey": "A", "ParameterValue": "1"},
                    {"ParameterKey": "B", "ParameterValue": "2"},
                ],
            ),
            {"StackName": "S1"},
        )
        stub.add_client_error("validate_template", service_error_code="Throttling")
        stub.add_response(
            "update_stack",
            {"StackId": "id"},
            {
                "StackName": "S1",
                "TemplateURL": "https://s3/new.yaml",
                "Parameters": [
                    {"ParameterKey": "A", "UsePreviousValue": True},
                    {"ParameterKey": "B", "UsePreviousValue": True},
                ],
                "Capabilities": ANY,
            },
        )
        deployer.deploy_stack("S1", template_url="https://s3/new.yaml")
        stub.assert_no_pending_responses()


def test_a_changed_creation_fixed_parameter_is_refused_before_any_api_call(
    aws_credentials: str,
):
    """#835's update half. The refusal must land before ``update_stack``, because
    the update itself is what leaves the stack in ``UPDATE_ROLLBACK_FAILED``.

    The Stubber has no ``update_stack`` response queued, so reaching it would
    fail this test rather than pass it quietly.
    """
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("CREATE_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "CREATE_COMPLETE",
                parameters=[
                    {
                        "ParameterKey": "ExternalIdPEmailMutable",
                        "ParameterValue": "false",
                    }
                ],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "ExternalIdPEmailMutable"}]},
            {"TemplateURL": "https://s3/new.yaml"},
        )
        with pytest.raises(ValueError, match="cannot be changed on an existing stack"):
            deployer.deploy_stack(
                "S1",
                template_url="https://s3/new.yaml",
                parameters={"ExternalIdPEmailMutable": "true"},
            )


# ---------------------------------------------------------------------------
# _stack_exists / _get_stack_status / get_stack_operation_in_progress
# ---------------------------------------------------------------------------


def test_stack_existence_is_answered_from_cloudformation(
    aws_credentials: str, template_file: Path
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        assert deployer._stack_exists("S1") is False
        deployer.cfn.create_stack(
            StackName="S1",
            TemplateBody=TEMPLATE_JSON,
            Parameters=[{"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}],
        )
        assert deployer._stack_exists("S1") is True


def test_an_access_error_is_not_mistaken_for_a_missing_stack(aws_credentials: str):
    """Reading AccessDenied as "does not exist" would turn an update into a
    create attempt against a stack that is really there."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("describe_stacks", service_error_code="AccessDenied")
        with pytest.raises(Exception, match="AccessDenied"):
            deployer._stack_exists("S1")


def test_stack_status_of_a_missing_stack_is_none(aws_credentials: str):
    with mock_aws():
        assert StackDeployer(region=REGION)._get_stack_status("nope") is None


def test_stack_status_is_read_from_the_live_stack(
    aws_credentials: str, template_file: Path
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        deployer.cfn.create_stack(
            StackName="S1",
            TemplateBody=TEMPLATE_JSON,
            Parameters=[{"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}],
        )
        assert deployer._get_stack_status("S1") == "CREATE_COMPLETE"


def test_an_empty_stack_list_reads_as_no_status(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        assert deployer._get_stack_status("S1") is None


def test_a_status_error_other_than_absence_propagates(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("describe_stacks", service_error_code="Throttling")
        with pytest.raises(Exception, match="Throttling"):
            deployer._get_stack_status("S1")


@pytest.mark.parametrize(
    ("status", "operation"),
    [
        ("CREATE_IN_PROGRESS", "CREATE"),
        ("ROLLBACK_IN_PROGRESS", "CREATE"),
        ("UPDATE_IN_PROGRESS", "UPDATE"),
        ("UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE"),
        ("UPDATE_ROLLBACK_IN_PROGRESS", "UPDATE"),
        ("UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE"),
        ("DELETE_IN_PROGRESS", "DELETE"),
    ],
)
def test_each_in_progress_status_maps_to_its_operation(
    aws_credentials: str, status: str, operation: str
):
    """The caller uses the operation to pick which completion statuses to wait
    for, so a wrong mapping waits for a status that will never arrive. The two
    rollback states are the ones worth naming: a rollback during *create* is a
    CREATE operation, and a rollback during *update* is an UPDATE."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response(status), {"StackName": "S1"}
        )
        assert deployer.get_stack_operation_in_progress("S1") == {
            "operation": operation,
            "status": status,
        }


@pytest.mark.parametrize(
    "status",
    [
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
    ],
)
def test_a_settled_status_is_not_an_operation_in_progress(
    aws_credentials: str, status: str
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response(status), {"StackName": "S1"}
        )
        assert deployer.get_stack_operation_in_progress("S1") is None


def test_a_missing_stack_has_no_operation_in_progress(aws_credentials: str):
    with mock_aws():
        assert (
            StackDeployer(region=REGION).get_stack_operation_in_progress("nope") is None
        )


# ---------------------------------------------------------------------------
# cancel_update_stack
# ---------------------------------------------------------------------------


def test_a_cancellation_is_reported_as_initiated_not_complete(aws_credentials: str):
    """Cancelling only *starts* a rollback, so the message must not claim the
    stack is back to normal — the caller has to wait for
    UPDATE_ROLLBACK_COMPLETE afterwards."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("cancel_update_stack", {}, {"StackName": "S1"})
        result = deployer.cancel_update_stack("S1")
    assert result["success"] is True
    assert "cancellation initiated" in result["message"]
    assert "S1" in result["message"]


def test_a_failed_cancellation_is_returned_rather_than_raised(aws_credentials: str):
    """Cancelling a stack that is not updating is the normal race, and the caller
    recovers from it — so this reports rather than aborts."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "cancel_update_stack",
            service_error_code="ValidationError",
            service_message="CancelUpdateStack cannot be called from current stack status",
        )
        result = deployer.cancel_update_stack("S1")
    assert result["success"] is False
    assert "current stack status" in result["error"]


# ---------------------------------------------------------------------------
# wait_for_stable_state
# ---------------------------------------------------------------------------


def test_waiting_polls_until_the_stack_settles(aws_credentials: str, clock: FakeClock):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        for status in ("UPDATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE"):
            stub.add_response(
                "describe_stacks", _stack_response(status), {"StackName": "S1"}
            )
        result = deployer.wait_for_stable_state("S1")
    assert result == {
        "success": True,
        "status": "UPDATE_COMPLETE",
        "message": "Stack reached stable state: UPDATE_COMPLETE",
    }
    assert clock.slept == [10, 10]


@pytest.mark.parametrize(
    "status",
    [
        "CREATE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_FAILED",
        "DELETE_FAILED",
    ],
)
def test_a_failed_but_settled_state_counts_as_stable(
    aws_credentials: str, clock: FakeClock, status: str
):
    """ "Stable" means "no operation running", not "healthy". A caller blocked here
    on a stack in UPDATE_ROLLBACK_FAILED would wait forever, and that state needs
    manual intervention, so it must be reported at once."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response(status), {"StackName": "S1"}
        )
        result = deployer.wait_for_stable_state("S1")
    assert result["success"] is True
    assert result["status"] == status


def test_waiting_times_out_with_the_budget_in_the_message(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        for _ in range(4):
            stub.add_response(
                "describe_stacks",
                _stack_response("UPDATE_IN_PROGRESS"),
                {"StackName": "S1"},
            )
        result = deployer.wait_for_stable_state("S1", timeout_seconds=30)
    assert result == {
        "success": False,
        "error": "Timeout after 30s",
        "status": "TIMEOUT",
    }


def test_a_stack_that_disappears_while_waiting_counts_as_deleted(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            _stack_response("DELETE_IN_PROGRESS"),
            {"StackName": "S1"},
        )
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        result = deployer.wait_for_stable_state("S1")
    assert result["success"] is True
    assert result["status"] == "DELETE_COMPLETE"


def test_an_absence_reported_as_a_non_client_error_still_counts_as_deleted(
    aws_credentials: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    """The outer handler catches an absence signalled as something other than a
    ClientError — an endpoint or retry wrapper, for instance. `_get_stack_status`
    already translates the ClientError form, so this branch is only reachable
    through a different exception type, and it is patched in directly."""
    deployer = StackDeployer(region=REGION)
    monkeypatch.setattr(
        deployer,
        "_get_stack_status",
        lambda _name: (_ for _ in ()).throw(RuntimeError("Stack does not exist here")),
    )
    result = deployer.wait_for_stable_state("S1")
    assert result["status"] == "DELETE_COMPLETE"


def test_an_unrelated_polling_error_aborts_the_wait(
    aws_credentials: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    deployer = StackDeployer(region=REGION)
    monkeypatch.setattr(
        deployer,
        "_get_stack_status",
        lambda _name: (_ for _ in ()).throw(RuntimeError("Throttling")),
    )
    with pytest.raises(RuntimeError, match="Throttling"):
        deployer.wait_for_stable_state("S1")


# ---------------------------------------------------------------------------
# monitor_stack_progress
# ---------------------------------------------------------------------------


def test_monitoring_a_create_returns_the_outputs_on_success(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            _stack_response("CREATE_IN_PROGRESS"),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(status="CREATE_IN_PROGRESS")]},
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "CREATE_COMPLETE",
                outputs=[{"OutputKey": "TopicArn", "OutputValue": "arn:aws:sns:::t"}],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(status="CREATE_COMPLETE")]},
            {"StackName": "S1"},
        )
        result = deployer.monitor_stack_progress("S1", "CREATE")
    assert result == {
        "stack_name": "S1",
        "operation": "CREATE",
        "status": "CREATE_COMPLETE",
        "success": True,
        "outputs": {"TopicArn": "arn:aws:sns:::t"},
    }
    assert clock.slept == [10]


def test_monitoring_a_failed_create_carries_the_root_cause(
    aws_credentials: str, clock: FakeClock
):
    """A rollback is a failure even though it "completed", and the caller's only
    diagnostic is the error string assembled from the stack events."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("ROLLBACK_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(reason="Invalid email address")]},
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(reason="Invalid email address")]},
            {"StackName": "S1"},
        )
        result = deployer.monitor_stack_progress("S1", "CREATE")
    assert result["success"] is False
    assert result["status"] == "ROLLBACK_COMPLETE"
    assert result["outputs"] == {}
    assert result["error"] == "Topic: Invalid email address"


def test_monitoring_a_delete_treats_a_vanished_stack_as_success(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        result = deployer.monitor_stack_progress("S1", "DELETE")
    assert result == {
        "stack_name": "S1",
        "operation": "DELETE",
        "status": "DELETE_COMPLETE",
        "success": True,
    }


def test_monitoring_a_delete_does_not_read_access_denied_as_deleted(
    aws_credentials: str, clock: FakeClock
):
    """The DELETE branch swallows "does not exist" on purpose; swallowing anything
    else would report a stack as deleted while it is still standing, and the
    caller would go on to clean up resources that are still in use."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("describe_stacks", service_error_code="AccessDenied")
        with pytest.raises(Exception, match="AccessDenied"):
            deployer.monitor_stack_progress("S1", "DELETE")


def test_monitoring_a_delete_treats_an_empty_stack_list_as_success(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        result = deployer.monitor_stack_progress("S1", "DELETE")
    assert result["status"] == "DELETE_COMPLETE"
    assert result["success"] is True


def test_a_completed_delete_reports_no_outputs(aws_credentials: str, clock: FakeClock):
    """A deleted stack has no outputs to read, and asking for them would fail."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "DELETE_COMPLETE",
                outputs=[{"OutputKey": "Stale", "OutputValue": "x"}],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(status="DELETE_COMPLETE")]},
            {"StackName": "S1"},
        )
        result = deployer.monitor_stack_progress("S1", "DELETE")
    assert result["success"] is True
    assert result["outputs"] == {}


def test_monitoring_a_non_delete_operation_on_a_missing_stack_raises(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        with pytest.raises(ValueError, match="Stack S1 not found"):
            deployer.monitor_stack_progress("S1", "UPDATE")


def test_an_unrecognised_operation_polls_forever(
    aws_credentials: str, clock: FakeClock
):
    """DEFECT, pinned as-is: ``monitor_stack_progress`` never terminates for an
    operation it does not know.

    ``complete_statuses.get(operation, [])`` at ``_core/stack.py:539`` yields an
    empty target list, so no status can ever match and the loop polls every ten
    seconds indefinitely. The stack below is already at ``CREATE_COMPLETE`` — a
    settled state — and is still not accepted. Note the asymmetry with
    ``_wait_for_completion``, which indexes the same dict with ``[operation]`` and
    so raises ``KeyError`` instead; a typo'd operation therefore hangs one caller
    and crashes the other.

    The test proves non-termination by making the third sleep abort, which is the
    only way to assert "does not return" without hanging the suite.
    """

    class Abort(Exception):
        pass

    polls = 0

    def sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls >= 3:
            raise Abort()

    clock.sleep = sleep  # type: ignore[method-assign]

    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        for _ in range(3):
            stub.add_response(
                "describe_stacks",
                _stack_response("CREATE_COMPLETE"),
                {"StackName": "S1"},
            )
            stub.add_response(
                "describe_stack_events",
                {"StackEvents": [_event()]},
                {"StackName": "S1"},
            )
        with pytest.raises(Abort):
            deployer.monitor_stack_progress("S1", "RESTORE")
    assert polls == 3


# ---------------------------------------------------------------------------
# _wait_for_completion
# ---------------------------------------------------------------------------


def test_waiting_for_an_update_returns_outputs_and_polls(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            _stack_response("UPDATE_IN_PROGRESS"),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events", {"StackEvents": [_event()]}, {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stacks",
            _stack_response(
                "UPDATE_COMPLETE",
                outputs=[{"OutputKey": "A", "OutputValue": "1"}],
            ),
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events", {"StackEvents": [_event()]}, {"StackName": "S1"}
        )
        result = deployer._wait_for_completion("S1", "UPDATE")
    assert result["success"] is True
    assert result["outputs"] == {"A": "1"}
    assert clock.slept == [10]


def test_waiting_for_completion_on_a_missing_stack_raises(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        with pytest.raises(ValueError, match="Stack S1 not found"):
            deployer._wait_for_completion("S1", "CREATE")


def test_an_unrecognised_operation_raises_here(aws_credentials: str, clock: FakeClock):
    """The other half of the asymmetry described in
    ``test_an_unrecognised_operation_polls_forever``: this function indexes the
    status table directly, so it fails fast."""
    with pytest.raises(KeyError):
        StackDeployer(region=REGION)._wait_for_completion("S1", "RESTORE")


def test_the_deploy_start_time_is_passed_into_the_failure_analysis(
    aws_credentials: str, clock: FakeClock
):
    """A stack that failed once already carries old FAILED events. Reporting one
    of those as the cause of *this* deploy sends the operator after a problem they
    already fixed, so the cut-off has to reach the analysis."""
    started = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    stale = _event(
        resource="OldResource",
        reason="an error from last week",
        when=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
    )
    fresh = _event(
        resource="Topic",
        reason="this deploy's real error",
        when=dt.datetime(2026, 6, 1, 0, 5, tzinfo=dt.timezone.utc),
    )
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks", _stack_response("ROLLBACK_COMPLETE"), {"StackName": "S1"}
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [stale, fresh]},
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [stale, fresh]},
            {"StackName": "S1"},
        )
        result = deployer._wait_for_completion(
            "S1", "CREATE", deploy_start_time=started
        )
    assert result["error"] == "Topic: this deploy's real error"


# ---------------------------------------------------------------------------
# _get_stack_outputs
# ---------------------------------------------------------------------------


def test_outputs_become_a_flat_mapping(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    assert deployer._get_stack_outputs(
        {
            "Outputs": [
                {"OutputKey": "A", "OutputValue": "1"},
                {"OutputKey": "B", "OutputValue": "2"},
            ]
        }
    ) == {"A": "1", "B": "2"}


def test_a_stack_with_no_outputs_yields_an_empty_mapping(aws_credentials: str):
    assert StackDeployer(region=REGION)._get_stack_outputs({}) == {}


def test_a_missing_output_value_becomes_an_empty_string(aws_credentials: str):
    """An output whose value CloudFormation has not resolved yet must read as
    empty rather than raise, because this runs while a stack is still settling."""
    assert StackDeployer(region=REGION)._get_stack_outputs(
        {"Outputs": [{"OutputKey": "A"}, {"OutputValue": "orphan"}]}
    ) == {"A": "", "": "orphan"}


# ---------------------------------------------------------------------------
# get_deployment_failure_analysis / _get_stack_failure_reason
# ---------------------------------------------------------------------------


def _analysis_stub(deployer: StackDeployer, pages: dict[str, list[dict]]) -> Stubber:
    """Queue one ``describe_stack_events`` page per stack name in call order."""
    stub = Stubber(deployer.cfn)
    for name, events in pages.items():
        stub.add_response(
            "describe_stack_events", {"StackEvents": events}, {"StackName": name}
        )
    return stub


def test_only_failed_events_are_collected(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(resource="Ok", status="CREATE_COMPLETE", reason=""),
                _event(resource="Bad", status="CREATE_FAILED", reason="no permission"),
            ]
        },
    ):
        analysis = deployer.get_deployment_failure_analysis("S1")
    assert [f["resource"] for f in analysis["all_failures"]] == ["Bad"]
    assert [f["resource"] for f in analysis["root_causes"]] == ["Bad"]
    assert analysis["stack_name"] == "S1"


def test_a_cancelled_resource_is_not_reported_as_the_root_cause(aws_credentials: str):
    """A single failure cancels every sibling in flight, so a large stack reports
    dozens of "Resource creation cancelled" events alongside the one real error.
    Ranking a cancellation first sends the operator to an innocent resource."""
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(resource="Sibling", reason="Resource creation cancelled"),
                _event(resource="Sibling2", reason="Resource update cancelled"),
                _event(resource="Sibling3", reason="resource creation Cancelled"),
                _event(resource="RealCause", reason="Bucket name already taken"),
            ]
        },
    ):
        analysis = deployer.get_deployment_failure_analysis("S1")
    assert [f["resource"] for f in analysis["root_causes"]] == ["RealCause"]
    assert len(analysis["all_failures"]) == 4
    assert sum(f["is_cascade"] for f in analysis["all_failures"]) == 3


def test_a_nested_stack_failure_is_followed_down_and_labelled_with_its_path(
    aws_credentials: str,
):
    """The parent only ever says "Embedded stack ... was not successfully created",
    which names no resource an operator can act on. The real cause is in the
    nested stack's own events, and the path is how they find it."""
    nested_arn = (
        "arn:aws:cloudformation:us-east-1:123456789012:stack/S1-PATTERNSTACK-A/x"
    )
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(
                    resource="PATTERNSTACK",
                    resource_type="AWS::CloudFormation::Stack",
                    reason="Embedded stack was not successfully created",
                    physical_id=nested_arn,
                )
            ],
            nested_arn: [
                _event(resource="OCRFunction", reason="Layer version does not exist")
            ],
        },
    ):
        analysis = deployer.get_deployment_failure_analysis("S1")

    root = analysis["root_causes"]
    assert [f["resource"] for f in root] == ["OCRFunction"]
    assert root[0]["stack_path"] == "PATTERNSTACK"
    assert root[0]["stack"] == "S1-PATTERNSTACK-A"
    wrapper = [f for f in analysis["all_failures"] if f["is_nested_wrapper"]]
    assert len(wrapper) == 1


def test_a_two_level_nesting_composes_the_path(aws_credentials: str):
    outer = "arn:aws:cloudformation:us-east-1:1:stack/S1-OUTER/x"
    inner = "arn:aws:cloudformation:us-east-1:1:stack/S1-OUTER-INNER/y"
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(
                    resource="OUTER",
                    resource_type="AWS::CloudFormation::Stack",
                    reason="Embedded stack was not successfully updated",
                    physical_id=outer,
                )
            ],
            outer: [
                _event(
                    resource="INNER",
                    resource_type="AWS::CloudFormation::Stack",
                    reason="Embedded stack was not successfully updated",
                    physical_id=inner,
                )
            ],
            inner: [_event(resource="Table", reason="Cannot update GSI projection")],
        },
    ):
        analysis = deployer.get_deployment_failure_analysis("S1")
    assert [f["stack_path"] for f in analysis["root_causes"]] == ["OUTER → INNER"]


def test_the_recursion_is_bounded(aws_credentials: str):
    """A nested wrapper whose physical id points back at itself would recurse
    forever without the depth cap; the cap is what keeps a malformed event from
    hanging the CLI instead of reporting a failure."""
    self_ref = "arn:aws:cloudformation:us-east-1:1:stack/Loop/x"
    wrapper = _event(
        resource="Loop",
        resource_type="AWS::CloudFormation::Stack",
        reason="Embedded stack was not successfully created",
        physical_id=self_ref,
    )
    deployer = StackDeployer(region=REGION)
    stub = Stubber(deployer.cfn)
    # Depth 0 uses the name; depths 1..5 use the ARN; depth 6 returns early.
    stub.add_response(
        "describe_stack_events", {"StackEvents": [wrapper]}, {"StackName": "Loop"}
    )
    for _ in range(5):
        stub.add_response(
            "describe_stack_events", {"StackEvents": [wrapper]}, {"StackName": self_ref}
        )
    with stub:
        analysis = deployer.get_deployment_failure_analysis("Loop")
        stub.assert_no_pending_responses()
    assert analysis["root_causes"] == []


def test_an_events_api_failure_becomes_a_reportable_failure(aws_credentials: str):
    """Returning an empty analysis would print "Unknown failure reason" and lose
    the fact that the events could not be read at all."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error(
            "describe_stack_events", service_error_code="AccessDenied"
        )
        analysis = deployer.get_deployment_failure_analysis("S1")
    assert len(analysis["root_causes"]) == 1
    assert "Could not retrieve stack events" in analysis["root_causes"][0]["reason"]
    assert "AccessDenied" in analysis["root_causes"][0]["reason"]


def test_stale_events_are_excluded_by_the_deploy_start_time(aws_credentials: str):
    started = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(
                    resource="Old",
                    when=dt.datetime(2026, 5, 31, 23, 59, tzinfo=dt.timezone.utc),
                ),
                _event(resource="New", when=started),
            ]
        },
    ):
        analysis = deployer.get_deployment_failure_analysis(
            "S1", deploy_start_time=started
        )
    assert [f["resource"] for f in analysis["all_failures"]] == ["New"]


def test_the_failure_reason_prefixes_the_nested_path(aws_credentials: str):
    nested = "arn:aws:cloudformation:us-east-1:1:stack/S1-PATTERNSTACK/x"
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {
            "S1": [
                _event(
                    resource="PATTERNSTACK",
                    resource_type="AWS::CloudFormation::Stack",
                    reason="Embedded stack was not successfully created",
                    physical_id=nested,
                )
            ],
            nested: [_event(resource="OCRFunction", reason="Layer missing")],
        },
    ):
        assert (
            deployer._get_stack_failure_reason("S1")
            == "PATTERNSTACK → OCRFunction: Layer missing"
        )


def test_the_failure_reason_falls_back_to_a_cascade_when_nothing_else_failed(
    aws_credentials: str,
):
    """If every failure is a cascade there is no root cause, but reporting
    "Unknown failure reason" would throw away the only information there is."""
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer,
        {"S1": [_event(resource="Sibling", reason="Resource creation cancelled")]},
    ):
        assert (
            deployer._get_stack_failure_reason("S1")
            == "Sibling: Resource creation cancelled"
        )


def test_the_failure_reason_is_explicit_when_nothing_failed(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(
        deployer, {"S1": [_event(status="CREATE_COMPLETE", reason="")]}
    ):
        assert deployer._get_stack_failure_reason("S1") == "Unknown failure reason"


def test_a_failure_with_no_reason_still_names_the_resource(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with _analysis_stub(deployer, {"S1": [_event(resource="Topic", reason="")]}):
        assert deployer._get_stack_failure_reason("S1") == "Topic: "


# ---------------------------------------------------------------------------
# get_stack_events
# ---------------------------------------------------------------------------


def test_events_are_projected_onto_four_fields(
    aws_credentials: str, template_file: Path
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        deployer.cfn.create_stack(
            StackName="S1",
            TemplateBody=TEMPLATE_JSON,
            Parameters=[{"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}],
        )
        events = deployer.get_stack_events("S1")
    assert events
    assert set(events[0]) == {"timestamp", "resource", "status", "reason"}
    # The projection must keep the status, because the polling loop logs it.
    assert {e["status"] for e in events} <= {
        "CREATE_IN_PROGRESS",
        "CREATE_COMPLETE",
    }
    assert any(e["resource"] == "S1" for e in events)


def test_the_event_limit_is_honoured(aws_credentials: str):
    """The caller polls this every ten seconds purely to log the newest event, so
    the limit is what stops it pulling a large stack's whole history each time."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stack_events",
            {"StackEvents": [_event(resource=f"R{i}") for i in range(10)]},
            {"StackName": "S1"},
        )
        events = deployer.get_stack_events("S1", limit=3)
    assert [e["resource"] for e in events] == ["R0", "R1", "R2"]


def test_an_events_failure_yields_an_empty_list(aws_credentials: str):
    """This is called from inside the polling loops purely for progress logging,
    so a failure here must not abort a deploy that is otherwise succeeding."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("describe_stack_events", service_error_code="Throttling")
        assert deployer.get_stack_events("S1") == []
