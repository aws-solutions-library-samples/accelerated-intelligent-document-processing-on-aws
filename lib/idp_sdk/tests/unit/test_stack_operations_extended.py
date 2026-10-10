# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the rest of ``idp_sdk.operations.stack``.

``StackOperation`` is the SDK's CloudFormation namespace: ``deploy`` builds the
parameter set and hands it to ``idp_sdk._core.stack.StackDeployer``, ``delete``
optionally sweeps retained resources afterwards, ``monitor`` and
``wait_for_stable_state`` are the two polling loops, ``get_failure_analysis``
turns raw failure dictionaries into typed causes, and ``exists``, ``get_status``,
``check_in_progress``, ``cancel_update``, ``get_resources``, ``get_bucket_info``
and ``cleanup_orphaned`` are thin reads. ``tests/unit/test_stack_operations.py``
already covers the happy path of ``deploy`` and ``delete``.

**What shaped these tests.** Three things.

First, ``deploy`` does not pass its own arguments through — it calls
``build_parameters``, which renames and stringifies them into CloudFormation
parameter names. That function is left real here and the assertions are on the
dictionary the deployer received, because the defect this code has already
shipped was a parameter *name* the template does not declare (``EnableHITL``),
which every mock-the-call test passed.

Second, the two polling loops are the only places in the module with control flow
worth testing, and both are driven by ``time``. They run against a fake clock
that advances only when the loop sleeps, so a test can assert the timeout is
honoured and the poll interval is respected without waiting.

Third, ``monitor``'s DELETE branch catches ``deployer.cfn.exceptions.ClientError``.
That attribute is a real exception class on a real client and a ``Mock``
attribute on a mock, and ``except <a Mock>`` raises ``TypeError`` — so the fake
deployer here carries botocore's real class, and ``get_resources`` is tested
against a real (moto) CloudFormation stack rather than a stubbed resource dict.
"""

import json
import sys
import time

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPStackError
from idp_sdk.models import (
    BucketInfo,
    CancelUpdateResult,
    FailureAnalysis,
    OrphanedResourceCleanupResult,
    StackMonitorResult,
    StackOperationInProgress,
    StackResources,
    StackStableStateResult,
)

pytestmark = pytest.mark.unit

STACK_NAME = "idp-stack-test"
REGION = "us-east-1"


# ---------------------------------------------------------------------------
# A recording StackDeployer
# ---------------------------------------------------------------------------


class FakeCfn:
    """The ``cfn`` client attribute ``monitor`` reaches through.

    ``exceptions.ClientError`` must be a real exception class: ``monitor``'s
    DELETE branch names it in an ``except`` clause, and a ``Mock`` there makes
    the first poll raise ``TypeError`` instead of catching anything.
    """

    class exceptions:  # noqa: N801 - mirrors botocore's client attribute
        ClientError = ClientError

    def __init__(self, responses):
        self._responses = list(responses)
        self.describe_calls = []

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 casing
        self.describe_calls.append(StackName)
        nxt = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _client_error(message):
    return ClientError(
        {"Error": {"Code": "ValidationError", "Message": message}}, "DescribeStacks"
    )


class FakeDeployer:
    """Recorder for every ``StackDeployer`` method these operations call."""

    def __init__(self, region=None, **kwargs):
        self.region = region
        self.calls = {}
        self.cfn = FakeCfn([{"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}])
        self.deploy_result = {}
        self.delete_result = {}
        self.cleanup_result = {}
        self.exists_result = True
        self.status_result = None
        self.status_results = None
        self.in_progress_result = None
        self.cancel_result = {}
        self.failure_analysis = {}
        self.bucket_info = []
        self.outputs = {}
        self.failure_reason = None

    # -- deploy / delete ---------------------------------------------------
    def deploy_stack(self, **kwargs):
        self.calls["deploy_stack"] = kwargs
        return self.deploy_result

    def delete_stack(self, **kwargs):
        self.calls["delete_stack"] = kwargs
        return self.delete_result

    def cleanup_retained_resources(self, identifier):
        self.calls["cleanup_retained_resources"] = identifier
        return self.cleanup_result

    # -- reads -------------------------------------------------------------
    def _stack_exists(self, name):
        self.calls["_stack_exists"] = name
        return self.exists_result

    def _get_stack_status(self, name):
        self.calls.setdefault("_get_stack_status", []).append(name)
        if self.status_results is not None:
            if isinstance(self.status_results[0], Exception):
                raise self.status_results.pop(0)
            return (
                self.status_results.pop(0)
                if len(self.status_results) > 1
                else self.status_results[0]
            )
        return self.status_result

    def get_stack_operation_in_progress(self, name):
        self.calls["get_stack_operation_in_progress"] = name
        return self.in_progress_result

    def cancel_update_stack(self, name):
        self.calls["cancel_update_stack"] = name
        return self.cancel_result

    def get_deployment_failure_analysis(self, name, deploy_start_time=None):
        self.calls["get_deployment_failure_analysis"] = {
            "name": name,
            "deploy_start_time": deploy_start_time,
        }
        return self.failure_analysis

    def get_bucket_info(self, name):
        self.calls["get_bucket_info"] = name
        return self.bucket_info

    # -- monitor helpers ---------------------------------------------------
    def _get_stack_outputs(self, stack):
        self.calls["_get_stack_outputs"] = stack
        return self.outputs

    def _get_stack_failure_reason(self, name):
        self.calls["_get_stack_failure_reason"] = name
        return self.failure_reason


@pytest.fixture
def deployer(monkeypatch):
    """Install ``FakeDeployer`` and hand the test a configured instance.

    The operations construct the deployer themselves, so the fixture pre-builds
    one and has the class hand that same object back — otherwise a test could not
    set up a return value before the call.
    """
    instance = FakeDeployer(region=REGION)

    def factory(region=None, **kwargs):
        instance.region = region
        return instance

    monkeypatch.setattr("idp_sdk._core.stack.StackDeployer", factory)
    return instance


@pytest.fixture
def fake_clock(monkeypatch):
    """A clock that only moves when the code under test sleeps.

    Both polling loops compute elapsed time from ``time.time()`` and wait with
    ``time.sleep()``. Advancing the clock from inside ``sleep`` makes the loops
    deterministic: a test can say "the third poll is the one that succeeds" or
    "no poll ever succeeds" and get an answer immediately.
    """

    class Clock:
        def __init__(self):
            self.now = 1_000.0
            self.sleeps = []

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if len(self.sleeps) > 500:  # pragma: no cover - runaway loop guard
                raise AssertionError("polling loop did not terminate")

    clock = Clock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


def _client():
    return IDPClient(stack_name=STACK_NAME, region=REGION)


# ---------------------------------------------------------------------------
# deploy()
# ---------------------------------------------------------------------------


class TestDeploy:
    def test_enable_hitl_is_refused_with_the_remedy_before_any_work(
        self, deployer, aws_credentials
    ):
        """HITL stopped being a CloudFormation parameter in v0.4.11. Sending it
        anyway made CloudFormation reject the whole operation after the caller had
        waited for a build and upload, so the refusal has to happen here, and the
        message has to say where HITL now lives.

        Note this raises ``ValueError`` rather than the SDK's own error type: the
        check sits above the ``try`` that converts everything else into
        ``IDPStackError``.
        """
        with pytest.raises(ValueError, match="no longer a CloudFormation parameter"):
            _client().stack.deploy(enable_hitl=True)

        assert deployer.calls == {}

    def test_enable_hitl_false_is_accepted_and_sends_no_parameter(
        self, deployer, aws_credentials
    ):
        """``False``/``None`` is the documented accepted value, and it must not
        reappear in the parameter set under any spelling."""
        deployer.deploy_result = {"success": True, "operation": "UPDATE"}

        _client().stack.deploy(enable_hitl=False, template_url="https://x/t.yaml")

        params = deployer.calls["deploy_stack"]["parameters"]
        assert not any("HITL" in key for key in params)

    def test_the_arguments_are_translated_into_cloudformation_parameter_names(
        self, deployer, aws_credentials
    ):
        """``build_parameters`` is left real here. The SDK's own argument names are
        not the template's parameter names, and a wrong name is accepted by every
        mock and rejected by CloudFormation."""
        deployer.deploy_result = {"success": True}

        _client().stack.deploy(
            template_url="https://example/t.yaml",
            admin_email="admin@example.com",
            max_concurrent=25,
            log_level="DEBUG",
            custom_config="s3://cfg-bucket/config.yaml",
        )

        assert deployer.calls["deploy_stack"]["parameters"] == {
            "AdminEmail": "admin@example.com",
            # CloudFormation parameter values are strings, always.
            "MaxConcurrentWorkflows": "25",
            "LogLevel": "DEBUG",
            "CustomConfigPath": "s3://cfg-bucket/config.yaml",
        }

    def test_unspecified_arguments_are_omitted_rather_than_sent_as_empty(
        self, deployer, aws_credentials
    ):
        """On an update, an absent parameter means "keep the current value". An
        empty string would overwrite it."""
        deployer.deploy_result = {"success": True}

        _client().stack.deploy(template_url="https://example/t.yaml")

        assert deployer.calls["deploy_stack"]["parameters"] == {}

    def test_additional_parameters_are_merged_and_win_over_the_named_ones(
        self, deployer, aws_credentials
    ):
        """``parameters`` is the escape hatch for anything the signature does not
        name, so it is applied last."""
        deployer.deploy_result = {"success": True}

        _client().stack.deploy(
            template_url="https://example/t.yaml",
            log_level="INFO",
            parameters={"LogLevel": "ERROR", "WebUIHosting": "APIGateway"},
        )

        params = deployer.calls["deploy_stack"]["parameters"]
        assert params["LogLevel"] == "ERROR"
        assert params["WebUIHosting"] == "APIGateway"

    def test_a_template_url_deploy_sends_no_template_path(
        self, deployer, aws_credentials
    ):
        """The two branches call ``deploy_stack`` with different keyword sets, and
        passing both would leave the deployer choosing."""
        deployer.deploy_result = {"success": True}

        _client().stack.deploy(template_url="https://example/t.yaml", wait=False)

        kwargs = deployer.calls["deploy_stack"]
        assert kwargs["template_url"] == "https://example/t.yaml"
        assert "template_path" not in kwargs
        assert kwargs["wait"] is False

    def test_an_explicit_template_path_is_used_and_beats_from_code(
        self, deployer, aws_credentials, tmp_path, monkeypatch
    ):
        """``template_path`` takes precedence, and the build must not run at all —
        a build that runs anyway costs minutes and can overwrite the artifact the
        caller asked to deploy."""
        ran = []

        def refuse_to_build(*args, **kwargs):
            ran.append(args)
            raise AssertionError("the build must not run when template_path is given")

        monkeypatch.setattr("subprocess.run", refuse_to_build)
        deployer.deploy_result = {"success": True}
        template = tmp_path / "prebuilt.yaml"
        template.write_text("Resources: {}\n")

        _client().stack.deploy(template_path=str(template), from_code=str(tmp_path))

        assert deployer.calls["deploy_stack"]["template_path"] == str(template)
        assert "template_url" not in deployer.calls["deploy_stack"]
        assert ran == []

    def test_tags_and_role_arn_are_forwarded_verbatim(self, deployer, aws_credentials):
        deployer.deploy_result = {"success": True}
        role = "arn:aws:iam::123456789012:role/CfnServiceRole"

        _client().stack.deploy(
            template_url="https://example/t.yaml",
            role_arn=role,
            tags={"CostCentre": "1234"},
            no_rollback=True,
        )

        kwargs = deployer.calls["deploy_stack"]
        assert kwargs["role_arn"] == role
        assert kwargs["tags"] == {"CostCentre": "1234"}
        assert kwargs["no_rollback"] is True

    def test_the_result_fields_come_from_the_deployer(self, deployer, aws_credentials):
        deployer.deploy_result = {
            "success": True,
            "operation": "UPDATE",
            "status": "UPDATE_COMPLETE",
            "stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/x/abc",
            "outputs": {"WebUIURL": "https://d123.cloudfront.net"},
            "deploy_start_time": None,
        }

        result = _client().stack.deploy(template_url="https://example/t.yaml")

        assert result.success is True
        assert result.operation == "UPDATE"
        assert result.status == "UPDATE_COMPLETE"
        assert result.stack_name == STACK_NAME
        assert result.outputs == {"WebUIURL": "https://d123.cloudfront.net"}
        assert result.error is None

    def test_a_deployer_result_missing_everything_is_reported_as_unknown_failure(
        self, deployer, aws_credentials
    ):
        """The defaults must not be optimistic: an empty result is a failure with
        an unknown status, not a success."""
        deployer.deploy_result = {}

        result = _client().stack.deploy(template_url="https://example/t.yaml")

        assert result.success is False
        assert result.operation == "UNKNOWN"
        assert result.status == "UNKNOWN"

    def test_a_deployer_exception_becomes_an_idp_stack_error(
        self, deployer, aws_credentials, monkeypatch
    ):
        def explode(**kwargs):
            raise RuntimeError("AlreadyExistsException")

        monkeypatch.setattr(deployer, "deploy_stack", explode)

        with pytest.raises(IDPStackError, match="AlreadyExistsException"):
            _client().stack.deploy(template_url="https://example/t.yaml")


class TestDeployFromCode:
    @pytest.fixture
    def project(self, tmp_path):
        (tmp_path / "publish.py").write_text("# publisher\n")
        (tmp_path / ".aws-sam").mkdir()
        (tmp_path / ".aws-sam" / "idp-main.yaml").write_text("Resources: {}\n")
        return tmp_path

    def _record_subprocess(self, monkeypatch, returncode=0, stderr=""):
        seen = {}

        class Completed:
            def __init__(self):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        def fake_run(cmd, cwd=None, capture_output=False, text=False):
            seen["cmd"] = cmd
            seen["cwd"] = cwd
            return Completed()

        monkeypatch.setattr("subprocess.run", fake_run)
        return seen

    def test_the_publisher_is_invoked_in_the_project_with_a_derived_bucket(
        self, deployer, aws_credentials, project, monkeypatch
    ):
        """The build shells out to the project's own ``publish.py`` with three
        positional arguments, run from the project root. The bucket basename
        embeds the account id, so it has to come from STS."""
        seen = self._record_subprocess(monkeypatch)
        deployer.deploy_result = {"success": True}

        with mock_aws():
            _client().stack.deploy(from_code=str(project))

        assert seen["cwd"] == str(project)
        assert seen["cmd"] == [
            sys.executable,
            str(project / "publish.py"),
            "idp-accelerator-artifacts-123456789012",
            "idp-sdk",
            REGION,
        ]
        assert deployer.calls["deploy_stack"]["template_path"] == str(
            project / ".aws-sam" / "idp-main.yaml"
        )

    def test_a_client_without_a_region_falls_back_to_us_west_2(
        self, deployer, aws_credentials, project, monkeypatch
    ):
        """The publisher requires a region positionally, so there has to be a
        literal default; this pins which one, because the artifacts land in a
        region-suffixed bucket and a surprise region is a surprise bucket."""
        seen = self._record_subprocess(monkeypatch)
        deployer.deploy_result = {"success": True}

        with mock_aws():
            IDPClient(stack_name=STACK_NAME).stack.deploy(from_code=str(project))

        assert seen["cmd"][-1] == "us-west-2"

    def test_a_project_without_publish_py_is_refused(
        self, deployer, aws_credentials, tmp_path, monkeypatch
    ):
        """The guard fires before STS is called. Note the configuration error is
        re-wrapped as ``IDPStackError`` by the enclosing handler, so a caller
        cannot distinguish "wrong directory" from "deployment failed" by type —
        only by the message, which does still name the directory."""
        called = []
        monkeypatch.setattr("subprocess.run", lambda *a, **k: called.append(a))

        with pytest.raises(IDPStackError, match="publish.py not found"):
            _client().stack.deploy(from_code=str(tmp_path))

        assert called == []

    def test_a_failing_build_reports_the_publisher_stderr(
        self, deployer, aws_credentials, project, monkeypatch
    ):
        self._record_subprocess(
            monkeypatch, returncode=1, stderr="docker: command not found"
        )

        with mock_aws():
            with pytest.raises(IDPStackError, match="docker: command not found"):
                _client().stack.deploy(from_code=str(project))

        assert "deploy_stack" not in deployer.calls


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    def test_force_delete_all_forces_a_wait_and_sweeps_by_stack_id(
        self, deployer, aws_credentials
    ):
        """Retained resources can only be identified once CloudFormation has
        finished, so ``force_delete_all`` overrides ``wait=False``. The sweep is
        keyed on the stack *id* because the name no longer resolves after a
        successful delete."""
        stack_id = "arn:aws:cloudformation:us-east-1:123456789012:stack/x/abc"
        deployer.delete_result = {
            "success": True,
            "status": "DELETE_COMPLETE",
            "stack_id": stack_id,
        }
        deployer.cleanup_result = {"log_groups": {"deleted": ["a"], "errors": []}}

        result = _client().stack.delete(wait=False, force_delete_all=True)

        assert deployer.calls["delete_stack"]["wait"] is True
        assert deployer.calls["cleanup_retained_resources"] == stack_id
        assert result.cleanup_result == deployer.cleanup_result

    def test_the_sweep_falls_back_to_the_stack_name(self, deployer, aws_credentials):
        """A delete that was only initiated has no stack id yet, and the sweep
        still has to be given something that identifies the stack."""
        deployer.delete_result = {"status": "DELETE_IN_PROGRESS"}
        deployer.cleanup_result = {}

        _client().stack.delete(force_delete_all=True)

        assert deployer.calls["cleanup_retained_resources"] == STACK_NAME

    def test_without_force_delete_all_nothing_is_swept(self, deployer, aws_credentials):
        deployer.delete_result = {"success": True, "status": "DELETE_COMPLETE"}

        result = _client().stack.delete()

        assert "cleanup_retained_resources" not in deployer.calls
        assert result.cleanup_result is None

    def test_wait_false_is_passed_through_when_not_forced(
        self, deployer, aws_credentials
    ):
        deployer.delete_result = {"status": "INITIATED"}

        _client().stack.delete(wait=False, empty_buckets=True)

        assert deployer.calls["delete_stack"] == {
            "stack_name": STACK_NAME,
            "empty_buckets": True,
            "wait": False,
        }

    def test_an_initiated_delete_counts_as_a_successful_initiation(
        self, deployer, aws_credentials
    ):
        """The no-wait path sets no ``success`` key. Defaulting to ``False`` would
        report every fire-and-forget delete as having failed to start."""
        deployer.delete_result = {"status": "INITIATED"}

        result = _client().stack.delete(wait=False)

        assert result.success is True
        assert result.status == "INITIATED"

    def test_any_other_status_without_a_success_key_is_not_a_success(
        self, deployer, aws_credentials
    ):
        deployer.delete_result = {
            "status": "DELETE_FAILED",
            "error": "bucket not empty",
        }

        result = _client().stack.delete()

        assert result.success is False
        assert result.error == "bucket not empty"

    def test_an_explicit_success_false_is_not_overridden_by_the_status(
        self, deployer, aws_credentials
    ):
        deployer.delete_result = {"success": False, "status": "INITIATED"}

        assert _client().stack.delete().success is False

    def test_a_deletion_exception_becomes_an_idp_stack_error(
        self, deployer, aws_credentials, monkeypatch
    ):
        monkeypatch.setattr(
            deployer,
            "delete_stack",
            lambda **k: (_ for _ in ()).throw(RuntimeError("TerminationProtection")),
        )

        with pytest.raises(IDPStackError, match="TerminationProtection"):
            _client().stack.delete()


# ---------------------------------------------------------------------------
# get_resources()
# ---------------------------------------------------------------------------

RESOURCE_STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "DocumentQueue": {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": "idp-stack-test-queue"},
        },
        "TrackingTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": "idp-stack-test-tracking",
                "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
    },
    "Outputs": {
        "S3InputBucketName": {"Value": "idp-stack-test-input"},
        "S3OutputBucketName": {"Value": "idp-stack-test-output"},
        "S3ConfigurationBucketName": {"Value": "idp-stack-test-config"},
        "StateMachineArn": {
            "Value": "arn:aws:states:us-east-1:123456789012:stateMachine:idp"
        },
    },
}


class TestGetResources:
    """Run against a real (moto) stack: the aliases are the whole behaviour.

    ``StackResources`` declares CloudFormation-cased aliases (``InputBucket``) for
    snake_cased fields (``input_bucket``), and pydantic ignores keyword arguments
    it does not recognise. A stubbed resource dict would not show whether the
    alias mapping matches the keys ``StackInfo`` actually produces.
    """

    @pytest.fixture
    def live_stack(self, aws_credentials):
        with mock_aws():
            boto3.client("cloudformation", region_name=REGION).create_stack(
                StackName=STACK_NAME,
                TemplateBody=json.dumps(RESOURCE_STACK_TEMPLATE),
            )
            yield _client()

    def test_every_discovered_resource_lands_on_its_aliased_field(self, live_stack):
        resources = live_stack.stack.get_resources()

        assert isinstance(resources, StackResources)
        assert resources.input_bucket == "idp-stack-test-input"
        assert resources.output_bucket == "idp-stack-test-output"
        assert resources.configuration_bucket == "idp-stack-test-config"
        assert resources.documents_table == "idp-stack-test-tracking"
        assert resources.state_machine_arn is not None
        assert resources.state_machine_arn.endswith(":stateMachine:idp")
        # The queue URL is the SQS resource's physical id, not an output.
        assert resources.document_queue_url is not None
        assert resources.document_queue_url.endswith("/idp-stack-test-queue")

    def test_an_output_the_stack_does_not_declare_arrives_as_an_empty_string(
        self, live_stack
    ):
        """``StackInfo`` reports a missing output as ``""`` rather than omitting
        it, so the optional fields are empty strings and not ``None``. Worth
        knowing before writing ``if resources.test_set_bucket is None``: the
        falsy check is the one that works."""
        resources = live_stack.stack.get_resources()

        assert resources.test_set_bucket == ""
        assert resources.evaluation_baseline_bucket == ""

    def test_a_stack_in_a_bad_state_is_refused(self, aws_credentials):
        """``ROLLBACK_IN_PROGRESS`` and friends are not usable states. The guard
        lives in the client, and the message must name the stack."""
        with mock_aws():
            with pytest.raises(IDPStackError, match="not in a valid state"):
                IDPClient(
                    stack_name="never-created", region=REGION
                ).stack.get_resources()


# ---------------------------------------------------------------------------
# exists() / get_status() / check_in_progress() / cancel_update()
# ---------------------------------------------------------------------------


class TestSimpleReads:
    @pytest.mark.parametrize("exists", [True, False])
    def test_exists_reports_the_deployers_answer_for_the_named_stack(
        self, deployer, aws_credentials, exists
    ):
        deployer.exists_result = exists

        assert _client().stack.exists() is exists
        assert deployer.calls["_stack_exists"] == STACK_NAME

    def test_exists_honours_a_stack_name_override(self, deployer, aws_credentials):
        deployer.exists_result = False

        IDPClient(region=REGION).stack.exists(stack_name="other")

        assert deployer.calls["_stack_exists"] == "other"

    def test_get_status_returns_the_raw_cloudformation_status(
        self, deployer, aws_credentials
    ):
        deployer.status_result = "UPDATE_ROLLBACK_IN_PROGRESS"

        assert _client().stack.get_status() == "UPDATE_ROLLBACK_IN_PROGRESS"

    def test_get_status_returns_none_for_a_stack_that_does_not_exist(
        self, deployer, aws_credentials
    ):
        """``None`` and a status string are different answers; collapsing the
        former to ``""`` would make "no stack" look like a state."""
        deployer.status_result = None

        assert _client().stack.get_status() is None

    def test_check_in_progress_returns_none_when_the_stack_is_idle(
        self, deployer, aws_credentials
    ):
        deployer.in_progress_result = None

        assert _client().stack.check_in_progress() is None
        assert deployer.calls["get_stack_operation_in_progress"] == STACK_NAME

    def test_check_in_progress_types_the_operation_and_status(
        self, deployer, aws_credentials
    ):
        deployer.in_progress_result = {
            "operation": "UPDATE",
            "status": "UPDATE_IN_PROGRESS",
        }

        result = _client().stack.check_in_progress()

        assert isinstance(result, StackOperationInProgress)
        assert result.operation == "UPDATE"
        assert result.status == "UPDATE_IN_PROGRESS"

    def test_cancel_update_maps_success_and_message(self, deployer, aws_credentials):
        deployer.cancel_result = {
            "success": True,
            "message": "Cancellation initiated",
        }

        result = _client().stack.cancel_update()

        assert isinstance(result, CancelUpdateResult)
        assert result.success is True
        assert result.message == "Cancellation initiated"
        assert result.error is None
        assert deployer.calls["cancel_update_stack"] == STACK_NAME

    def test_cancel_update_defaults_to_failure_and_carries_the_error(
        self, deployer, aws_credentials
    ):
        deployer.cancel_result = {"error": "Stack is not in UPDATE_IN_PROGRESS"}

        result = _client().stack.cancel_update()

        assert result.success is False
        assert result.error == "Stack is not in UPDATE_IN_PROGRESS"


# ---------------------------------------------------------------------------
# monitor()
# ---------------------------------------------------------------------------


class TestMonitor:
    def test_a_successful_update_reports_the_outputs(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn([{"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}])
        deployer.outputs = {"WebUIURL": "https://d123.cloudfront.net"}

        result = _client().stack.monitor(operation="UPDATE")

        assert isinstance(result, StackMonitorResult)
        assert result.success is True
        assert result.status == "UPDATE_COMPLETE"
        assert result.outputs == {"WebUIURL": "https://d123.cloudfront.net"}
        assert result.error is None
        assert fake_clock.sleeps == []  # terminal on the first poll

    def test_a_failed_update_reports_the_failure_reason_and_no_outputs(
        self, deployer, aws_credentials, fake_clock
    ):
        """Outputs from a rolled-back stack describe the *previous* deployment, so
        returning them on failure would be actively misleading."""
        deployer.cfn = FakeCfn(
            [{"Stacks": [{"StackStatus": "UPDATE_ROLLBACK_COMPLETE"}]}]
        )
        deployer.outputs = {"WebUIURL": "https://stale.cloudfront.net"}
        deployer.failure_reason = "UpdateDefaultConfig: int(None)"

        result = _client().stack.monitor(operation="UPDATE")

        assert result.success is False
        assert result.status == "UPDATE_ROLLBACK_COMPLETE"
        assert result.outputs == {}
        assert result.error == "UpdateDefaultConfig: int(None)"
        assert "_get_stack_outputs" not in deployer.calls

    def test_it_polls_at_the_requested_interval_until_a_terminal_state(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn(
            [
                {"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]},
                {"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]},
                {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]},
            ]
        )

        result = _client().stack.monitor(operation="UPDATE", poll_interval_seconds=7)

        assert result.success is True
        assert fake_clock.sleeps == [7, 7]
        assert len(deployer.cfn.describe_calls) == 3

    def test_a_create_that_rolls_back_is_a_failure(
        self, deployer, aws_credentials, fake_clock
    ):
        """``ROLLBACK_COMPLETE`` is terminal for CREATE and is not success — the
        stack exists but has no resources."""
        deployer.cfn = FakeCfn([{"Stacks": [{"StackStatus": "ROLLBACK_COMPLETE"}]}])
        deployer.failure_reason = "IAM role quota exceeded"

        result = _client().stack.monitor(operation="CREATE")

        assert result.success is False
        assert result.error == "IAM role quota exceeded"

    def test_a_delete_that_makes_the_stack_vanish_is_a_success(
        self, deployer, aws_credentials, fake_clock
    ):
        """Once DELETE_COMPLETE, ``describe_stacks`` by name raises rather than
        answering — so the exception *is* the success signal, and it is matched by
        the message rather than by the error code."""
        deployer.cfn = FakeCfn([_client_error("Stack with id x does not exist")])

        result = _client().stack.monitor(operation="DELETE")

        assert result.success is True
        assert result.status == "DELETE_COMPLETE"
        assert result.outputs == {}

    def test_a_delete_reporting_no_stacks_is_also_a_success(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn([{"Stacks": []}])

        result = _client().stack.monitor(operation="DELETE")

        assert result.success is True
        assert result.status == "DELETE_COMPLETE"

    def test_a_successful_delete_does_not_try_to_read_outputs(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn([{"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}])

        result = _client().stack.monitor(operation="DELETE")

        assert result.success is True
        assert result.outputs == {}
        assert "_get_stack_outputs" not in deployer.calls

    def test_an_unrelated_client_error_during_a_delete_is_not_swallowed(
        self, deployer, aws_credentials, fake_clock
    ):
        """Only "does not exist" means finished. A throttle or an access denial
        must not be reported as a completed deletion."""
        deployer.cfn = FakeCfn([_client_error("Rate exceeded")])

        with pytest.raises(IDPStackError, match="Rate exceeded"):
            _client().stack.monitor(operation="DELETE")

    def test_an_update_on_a_missing_stack_is_an_error_not_a_completion(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn([{"Stacks": []}])

        with pytest.raises(IDPStackError, match="not found"):
            _client().stack.monitor(operation="UPDATE")

    def test_the_timeout_is_honoured_and_reported_as_its_own_status(
        self, deployer, aws_credentials, fake_clock
    ):
        """TIMEOUT is not a CloudFormation status; the caller has to be able to
        tell "still running when I gave up" from any real stack state."""
        deployer.cfn = FakeCfn([{"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]}])

        result = _client().stack.monitor(
            operation="UPDATE", poll_interval_seconds=10, timeout_seconds=30
        )

        assert result.success is False
        assert result.status == "TIMEOUT"
        assert result.error is not None and "30s" in result.error
        assert fake_clock.sleeps == [10, 10, 10, 10]

    def test_an_unrecognised_operation_never_terminates_and_times_out(
        self, deployer, aws_credentials, fake_clock
    ):
        """DEFECT (operations/stack.py:386-387). ``complete_statuses.get(operation,
        [])`` means an operation name outside {CREATE, UPDATE, DELETE} has an
        *empty* terminal-status set, so the loop polls a finished stack until the
        timeout — by default an hour of CloudFormation calls followed by a
        misleading ``TIMEOUT``. A caller who writes ``operation="update"`` in
        lower case gets exactly that. Pinned as-is; an unknown operation ought to
        be refused up front.
        """
        deployer.cfn = FakeCfn([{"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}])

        result = _client().stack.monitor(
            operation="update", poll_interval_seconds=60, timeout_seconds=120
        )

        assert result.status == "TIMEOUT"
        assert len(deployer.cfn.describe_calls) == 3

    def test_an_unexpected_exception_is_wrapped_naming_the_stack(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.cfn = FakeCfn([ValueError("malformed response")])

        with pytest.raises(IDPStackError, match=f"monitoring stack {STACK_NAME}"):
            _client().stack.monitor(operation="UPDATE")


# ---------------------------------------------------------------------------
# wait_for_stable_state()
# ---------------------------------------------------------------------------


class TestWaitForStableState:
    def test_an_already_stable_stack_returns_at_once(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.status_results = ["UPDATE_COMPLETE"]

        result = _client().stack.wait_for_stable_state()

        assert isinstance(result, StackStableStateResult)
        assert result.success is True
        assert result.status == "UPDATE_COMPLETE"
        assert result.message is not None and "UPDATE_COMPLETE" in result.message
        assert fake_clock.sleeps == []

    def test_a_transitional_stack_is_polled_until_it_settles(
        self, deployer, aws_credentials, fake_clock
    ):
        """This is the method's whole purpose: a stack mid-rollback becomes
        operable again, and the caller wants the resulting state — which here is a
        *failed* rollback, still reported as ``success=True`` because the question
        asked was "is it stable", not "did it work"."""
        deployer.status_results = [
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_FAILED",
        ]

        result = _client().stack.wait_for_stable_state(poll_interval_seconds=5)

        assert result.success is True
        assert result.status == "UPDATE_ROLLBACK_FAILED"
        assert fake_clock.sleeps == [5, 5]

    def test_a_vanished_stack_counts_as_stable(
        self, deployer, aws_credentials, fake_clock
    ):
        """``None`` from the status read means the stack is gone, which is a
        stable state — waiting for it to become one of the named statuses would
        hang until the timeout."""
        deployer.status_results = [None]

        result = _client().stack.wait_for_stable_state()

        assert result.success is True
        assert result.status == "DELETE_COMPLETE"
        assert result.message == "Stack no longer exists"

    def test_a_does_not_exist_exception_also_counts_as_stable(
        self, deployer, aws_credentials, fake_clock
    ):
        """The underlying read may raise instead of returning ``None`` depending
        on which API answered, so both shapes have to mean the same thing."""
        deployer.status_results = [_client_error("Stack with id x does not exist")]

        result = _client().stack.wait_for_stable_state()

        assert result.success is True
        assert result.status == "DELETE_COMPLETE"

    def test_any_other_exception_is_wrapped(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.status_results = [_client_error("Rate exceeded")]

        with pytest.raises(IDPStackError, match=f"polling stack {STACK_NAME}"):
            _client().stack.wait_for_stable_state()

    def test_the_timeout_reports_timeout_rather_than_raising(
        self, deployer, aws_credentials, fake_clock
    ):
        deployer.status_results = ["UPDATE_IN_PROGRESS"]

        result = _client().stack.wait_for_stable_state(
            timeout_seconds=20, poll_interval_seconds=10
        )

        assert result.success is False
        assert result.status == "TIMEOUT"
        assert result.message is not None and "20s" in result.message


# ---------------------------------------------------------------------------
# get_failure_analysis()
# ---------------------------------------------------------------------------


class TestGetFailureAnalysis:
    RAW = {
        "stack_name": "idp-stack-test",
        "root_causes": [
            {
                "resource": "OCRFunction",
                "resource_type": "AWS::Lambda::Function",
                "reason": "Resource handler returned message: image not found",
                "status": "CREATE_FAILED",
                "physical_id": "",
                "stack": "idp-stack-test-PATTERNSTACK-ABC",
                "stack_path": "PATTERNSTACK",
                "is_cascade": False,
            }
        ],
        "all_failures": [
            {
                "resource": "OCRFunction",
                "resource_type": "AWS::Lambda::Function",
                "reason": "Resource handler returned message: image not found",
                "status": "CREATE_FAILED",
                "stack": "idp-stack-test-PATTERNSTACK-ABC",
                "is_cascade": False,
            },
            {
                "resource": "PATTERNSTACK",
                "resource_type": "AWS::CloudFormation::Stack",
                "reason": "Embedded stack failed",
                "status": "CREATE_FAILED",
                "stack": "idp-stack-test",
                "is_cascade": True,
            },
        ],
    }

    def test_causes_are_typed_and_the_cascade_is_distinguished(
        self, deployer, aws_credentials
    ):
        """The root-cause/cascade split is the point of this call: a nested stack
        reporting "Embedded stack failed" is noise, and the Lambda underneath it
        is the actionable error."""
        deployer.failure_analysis = self.RAW

        analysis = _client().stack.get_failure_analysis()

        assert isinstance(analysis, FailureAnalysis)
        assert analysis.stack_name == STACK_NAME
        assert len(analysis.root_causes) == 1
        cause = analysis.root_causes[0]
        assert cause.resource == "OCRFunction"
        assert cause.resource_type == "AWS::Lambda::Function"
        assert "image not found" in cause.reason
        assert cause.status == "CREATE_FAILED"
        assert cause.stack_path == "PATTERNSTACK"
        assert cause.is_cascade is False
        assert analysis.cascade_count == 1

    def test_missing_keys_fall_back_to_the_documented_defaults(
        self, deployer, aws_credentials
    ):
        """A failure event that names no resource must still produce a usable
        object: these models have no optional fields for ``resource`` or
        ``reason``, so an absent key would be a ValidationError rather than a
        report. In particular the containing stack defaults to the stack asked
        about, which is right for a failure in the top-level stack."""
        deployer.failure_analysis = {"all_failures": [{}]}

        analysis = _client().stack.get_failure_analysis()

        assert analysis.stack_name == STACK_NAME
        cause = analysis.all_failures[0]
        assert cause.resource == "Unknown"
        assert cause.reason == "Unknown"
        assert cause.resource_type == ""
        assert cause.stack == STACK_NAME
        assert cause.is_cascade is False
        assert analysis.cascade_count == 0

    def test_the_deploy_start_time_filter_is_forwarded(self, deployer, aws_credentials):
        """Without it the analysis reports the *previous* deployment's errors,
        which is the most confusing possible answer."""
        from datetime import datetime, timezone

        started = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        deployer.failure_analysis = {}

        _client().stack.get_failure_analysis(deploy_start_time=started)

        assert deployer.calls["get_deployment_failure_analysis"] == {
            "name": STACK_NAME,
            "deploy_start_time": started,
        }


# ---------------------------------------------------------------------------
# cleanup_orphaned()
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup(monkeypatch):
    calls = {}

    class FakeCleanup:
        def __init__(self, region=None, profile=None):
            calls["init"] = {"region": region, "profile": profile}
            self.results = {}

        def run_cleanup(self, dry_run=False, auto_approve=False, regions=None):
            calls["run_cleanup"] = {
                "dry_run": dry_run,
                "auto_approve": auto_approve,
                "regions": regions,
            }
            return self.results

    # The operation builds the sweep itself, so the per-resource-type results a
    # test wants returned have to be settable after the fixture runs: the test
    # assigns `holder["results"]` and the factory reads it at construction time.
    holder = {"results": {}, "calls": calls}

    def factory(region=None, profile=None):
        instance = FakeCleanup(region=region, profile=profile)
        instance.results = holder["results"]
        return instance

    monkeypatch.setattr(
        "idp_sdk._core.cleanup_orphaned.OrphanedResourceCleanup", factory
    )
    return holder


class TestCleanupOrphaned:
    def test_the_flags_reach_the_cleanup_and_a_clean_sweep_reports_no_problems(
        self, cleanup, aws_credentials
    ):
        cleanup["results"] = {
            "log_groups": {"deleted": ["/aws/lambda/x"], "errors": []},
            "buckets": {"deleted": [], "errors": []},
        }

        result = IDPClient(region=REGION).stack.cleanup_orphaned(
            dry_run=True, auto_approve=True, regions=["us-east-1", "eu-central-1"]
        )

        assert isinstance(result, OrphanedResourceCleanupResult)
        assert result.has_errors is False
        assert result.has_disabled is False
        assert result.results == cleanup["results"]
        assert cleanup["calls"]["run_cleanup"] == {
            "dry_run": True,
            "auto_approve": True,
            "regions": ["us-east-1", "eu-central-1"],
        }

    def test_an_error_in_any_resource_type_sets_the_flag(
        self, cleanup, aws_credentials
    ):
        """The flags are ``any()`` over every resource type, so a failure in one
        sweep is not hidden by five clean ones."""
        cleanup["results"] = {
            "log_groups": {"deleted": [], "errors": []},
            "iam_policies": {"deleted": [], "errors": ["AccessDenied on policy X"]},
        }

        result = IDPClient(region=REGION).stack.cleanup_orphaned()

        assert result.has_errors is True
        assert result.has_disabled is False

    def test_a_disabled_distribution_is_flagged_separately(
        self, cleanup, aws_credentials
    ):
        """A CloudFront distribution can only be disabled now and deleted ~15
        minutes later, so "disabled" is a distinct outcome from "error" — it means
        run the sweep again, not investigate."""
        cleanup["results"] = {
            "cloudfront": {"deleted": [], "disabled": ["E123ABC"], "errors": []}
        }

        result = IDPClient(region=REGION).stack.cleanup_orphaned()

        assert result.has_disabled is True
        assert result.has_errors is False

    def test_a_client_without_a_region_falls_back_to_us_west_2(
        self, cleanup, aws_credentials
    ):
        """The sweep needs a region to construct its clients even though it then
        scans several, so there is a literal default. Pinned because an
        accidental ``None`` here becomes a ``NoRegionError`` deep inside the
        sweep."""
        IDPClient().stack.cleanup_orphaned()

        assert cleanup["calls"]["init"]["region"] == "us-west-2"

    def test_the_profile_is_forwarded(self, cleanup, aws_credentials):
        IDPClient(region=REGION).stack.cleanup_orphaned(profile="idp-admin")

        assert cleanup["calls"]["init"]["profile"] == "idp-admin"


# ---------------------------------------------------------------------------
# get_bucket_info()
# ---------------------------------------------------------------------------


class TestGetBucketInfo:
    def test_each_bucket_is_typed_field_by_field(self, deployer, aws_credentials):
        deployer.bucket_info = [
            {
                "logical_id": "InputBucket",
                "bucket_name": "idp-stack-test-input",
                "object_count": 1420,
                "total_size": 13_107_200,
                "size_display": "12.5 MB",
            }
        ]

        infos = _client().stack.get_bucket_info()

        assert [isinstance(i, BucketInfo) for i in infos] == [True]
        assert infos[0].logical_id == "InputBucket"
        assert infos[0].bucket_name == "idp-stack-test-input"
        assert infos[0].object_count == 1420
        assert infos[0].total_size == 13_107_200
        assert infos[0].size_display == "12.5 MB"
        assert deployer.calls["get_bucket_info"] == STACK_NAME

    def test_an_empty_bucket_reports_zero_rather_than_unknown(
        self, deployer, aws_credentials
    ):
        """0 objects and "size unknown" are different answers, and the caller uses
        this to decide whether a bucket must be emptied before deletion."""
        deployer.bucket_info = [
            {"logical_id": "WorkingBucket", "bucket_name": "idp-work"}
        ]

        info = _client().stack.get_bucket_info()[0]

        assert info.object_count == 0
        assert info.total_size == 0
        assert info.size_display == "Unknown"

    def test_a_stack_with_no_buckets_yields_an_empty_list(
        self, deployer, aws_credentials
    ):
        deployer.bucket_info = []

        assert _client().stack.get_bucket_info() == []
