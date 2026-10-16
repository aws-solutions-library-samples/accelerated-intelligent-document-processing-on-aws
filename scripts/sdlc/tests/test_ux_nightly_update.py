# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The scheduled in-place update of the MR UX test's review stack, idp-ux-nightly.

codebuild_deployment.py moves that stack to tonight's template on a thread of its
own beside the primary suite, and only on the scheduled develop run. The stack is
long-lived and shared, and every way this can go wrong is quiet: an update from a
button press lands an unmerged template on it, a delete takes a fixture other runs
rely on, an exit code of 0 reads as an update that never happened, or the update's
outcome leaks into the verdict the GitLab monitor reads off this same summary as
`OVERALL: PASS` or `OVERALL: FAIL`. So those are what is pinned here, against fakes
for CloudFormation and run_command: nothing in this file reaches AWS or a shell.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, WaiterError

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py"
SERVICE_ROLE_TEMPLATE = (
    "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)
TEMPLATE_URL = "https://s3.us-east-1.amazonaws.com/idp-sdlc-artifacts/idp/main.yaml"
ITS_OWN_BOUNDARY = "arn:aws:iam::123456789012:policy/idp-ux-nightly-PermissionsBoundary"
SHARED_BOUNDARY = "arn:aws:iam::123456789012:policy/idp-cicd-shared-PermissionsBoundary"
A_RUNS_BOUNDARY = "arn:aws:iam::123456789012:policy/idp-1013-131553-PermissionsBoundary"
ROLE_ARN = (
    "arn:aws:iam::123456789012:role/idp-ux-nightly-iam-CloudFormationServiceRole-A1"
)
PASS_ROLE_POLICY_ARN = (
    "arn:aws:iam::123456789012:policy/idp-ux-nightly-iam-PassRolePolicy"
)
LONG_AGO = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
FORBIDDEN_HELPERS = (
    "cleanup_stack",
    "cleanup_iam_resources",
    "teardown_primary_stack",
    "retain_failed_stack",
)


@pytest.fixture(autouse=True)
def _in_tmp_cwd(tmp_path, monkeypatch):
    """Run in a scratch directory holding the one repository file the update reads.

    CodeBuild runs the harness from the zip root, and both the run-options file and
    the -iam template are read relative to it. Copying the real template there keeps
    that relative read honest without any test writing into the checkout.
    """
    target = tmp_path / SERVICE_ROLE_TEMPLATE
    target.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / SERVICE_ROLE_TEMPLATE, target)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _gone(stack_name: str) -> ClientError:
    return _client_error(
        "ValidationError",
        f"Stack with id {stack_name} does not exist",
        "DescribeStacks",
    )


class _FakeWaiter:
    def __init__(self, cf: _FakeCloudFormation) -> None:
        self._cf = cf

    def wait(self, **kwargs: object) -> None:
        self._cf.calls.append(("wait", kwargs))
        if self._cf.waiter_error is not None:
            raise self._cf.waiter_error


class _FakeCloudFormation:
    """DescribeStacks answered from two stack records, and every call recorded.

    An attribute it does not define is recorded before it raises, because the code
    under test catches every exception as a thread failure: a delete, or any other
    API nobody expected, has to surface through `unexpected` rather than through an
    error the update would swallow.
    """

    def __init__(
        self,
        *,
        status: str = "UPDATE_COMPLETE",
        boundary: str = ITS_OWN_BOUNDARY,
        created_boundary: str | None = None,
        role_arn: str = ROLE_ARN,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.unexpected: list[str] = []
        self.gone: set[str] = set()
        self.describe_errors: dict[str, Exception] = {}
        self.update_error: Exception | None = None
        self.waiter_error: Exception | None = None
        iam_outputs = [
            {"OutputKey": "PassRolePolicyArn", "OutputValue": PASS_ROLE_POLICY_ARN},
            {"OutputKey": "RequiredStackNamePrefix", "OutputValue": "idp"},
        ]
        if role_arn:
            iam_outputs.insert(
                1, {"OutputKey": "ServiceRoleArn", "OutputValue": role_arn}
            )
        self.stacks = {
            "idp-ux-nightly": {
                "StackName": "idp-ux-nightly",
                "StackStatus": status,
                "LastUpdatedTime": LONG_AGO,
                "Parameters": [
                    {
                        "ParameterKey": "AdminEmail",
                        "ParameterValue": "a@example.invalid",
                    },
                    {
                        "ParameterKey": "PermissionsBoundaryArn",
                        "ParameterValue": boundary,
                    },
                ],
            },
            "idp-ux-nightly-iam": {
                "StackName": "idp-ux-nightly-iam",
                "StackStatus": "UPDATE_COMPLETE",
                "Parameters": [
                    {
                        "ParameterKey": "CreatedRolePermissionsBoundaryArn",
                        "ParameterValue": (
                            boundary if created_boundary is None else created_boundary
                        ),
                    },
                    {"ParameterKey": "ManagedStackNamePrefix", "ParameterValue": "idp"},
                ],
                "Outputs": iam_outputs,
            },
        }

    def describe_stacks(self, StackName: str) -> dict:
        self.calls.append(("describe_stacks", StackName))
        if StackName in self.describe_errors:
            raise self.describe_errors[StackName]
        if StackName in self.gone:
            raise _gone(StackName)
        return {"Stacks": [dict(self.stacks[StackName])]}

    def update_stack(self, **kwargs: object) -> dict:
        self.calls.append(("update_stack", kwargs))
        if self.update_error is not None:
            raise self.update_error
        return {"StackId": "stack-id"}

    def get_waiter(self, name: str) -> _FakeWaiter:
        self.calls.append(("get_waiter", name))
        return _FakeWaiter(self)

    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        self.unexpected.append(name)
        raise AssertionError(f"unexpected CloudFormation call: {name}")

    def made(self, api: str) -> list[Any]:
        return [args for name, args in self.calls if name == api]


@dataclass
class _Command:
    cmd: str
    check: bool
    timeout: object
    never_abort: bool


class _FakeDeploy:
    """Stands in for run_command, and plays CloudFormation's side of the deploy."""

    def __init__(
        self,
        cbd,
        cf: _FakeCloudFormation,
        *,
        returncode: int = 0,
        final_status: str | None = "UPDATE_COMPLETE",
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._cbd = cbd
        self._cf = cf
        self._returncode = returncode
        self._final_status = final_status
        self._stdout = stdout
        self._stderr = stderr
        self.commands: list[_Command] = []

    def __call__(self, cmd: str, check: bool = True, timeout: object = None):
        self.commands.append(
            _Command(
                cmd,
                check,
                timeout,
                getattr(self._cbd._thread_local, "never_abort", False),
            )
        )
        if self._final_status is not None:
            main = self._cf.stacks["idp-ux-nightly"]
            main["StackStatus"] = self._final_status
            main["LastUpdatedTime"] = datetime.now(timezone.utc) + timedelta(seconds=1)
        return subprocess.CompletedProcess(
            cmd, self._returncode, self._stdout, self._stderr
        )


@dataclass
class _Installed:
    cf: _FakeCloudFormation
    deploy: _FakeDeploy
    clients: list[tuple[str, dict, bool]]
    forbidden: list[str]


def _install(cbd, monkeypatch, cf=None, deploy=None) -> _Installed:
    cf = cf or _FakeCloudFormation()
    deploy = deploy or _FakeDeploy(cbd, cf)
    clients: list[tuple[str, dict, bool]] = []
    forbidden: list[str] = []

    def fake_client(service, *args, **kwargs):
        clients.append(
            (service, kwargs, getattr(cbd._thread_local, "never_abort", False))
        )
        if service != "cloudformation":
            raise AssertionError(f"unexpected boto3 client {service!r}")
        return cf

    def refuse(name):
        def called(*args, **kwargs):
            forbidden.append(name)
            raise AssertionError(f"{name} called from the nightly update")

        return called

    monkeypatch.setattr(cbd.boto3, "client", fake_client)
    monkeypatch.setattr(cbd, "run_command", deploy)
    for name in FORBIDDEN_HELPERS:
        monkeypatch.setattr(cbd, name, refuse(name))
    return _Installed(cf, deploy, clients, forbidden)


def _assert_destroyed_nothing(installed: _Installed) -> None:
    assert installed.cf.unexpected == []
    assert installed.forbidden == []
    assert not [c for c in installed.deploy.commands if "delete" in c.cmd.lower()]


# ---------------------------------------------------------------------------
# The stack it updates is one the Step 0 sweeps leave standing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_review_stack_is_one_the_step0_sweeps_leave_standing(cbd):
    members = getattr(cbd, "SWEEP_EXEMPT_STACK_NAMES", None)
    assert members is not None and cbd.UX_NIGHTLY_STACK_NAME in members, (
        f"{cbd.UX_NIGHTLY_STACK_NAME!r} is not in SWEEP_EXEMPT_STACK_NAMES "
        f"({'not defined' if members is None else sorted(members)}). That constant "
        "arrives with feature/ux-review-stack-sweep-exemption: rebase this branch "
        "onto a develop that carries it. Until then the scheduled run's Step 0 "
        "sweeps would delete the review stack and its -iam stack after 3 h and its "
        "buckets after 6 h."
    )


@pytest.mark.unit
def test_both_ends_of_the_run_options_file_name_it_the_same(cbd, itd):
    assert cbd.CI_RUN_OPTIONS_FILE == itd.CI_RUN_OPTIONS_FILE == "ci_run_options.json"


# ---------------------------------------------------------------------------
# Only the scheduled develop run asks for the update
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_missing_options_file_reads_as_no_options(cbd):
    assert cbd.read_ci_run_options() == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'["schedule", "develop"]', b'"schedule"', b"\xff\xfe\x00"],
    ids=["invalid-json", "empty-list", "list", "string", "not-utf8"],
)
def test_an_unreadable_or_non_object_options_file_reads_as_no_options(cbd, content):
    Path(cbd.CI_RUN_OPTIONS_FILE).write_bytes(content)
    assert cbd.read_ci_run_options() == {}


@pytest.mark.unit
def test_an_options_object_is_read_back_whole(cbd):
    Path("elsewhere.json").write_text(
        json.dumps({"version": 1, "pipeline_source": "schedule", "ref": "develop"}),
        encoding="utf-8",
    )
    assert cbd.read_ci_run_options("elsewhere.json") == {
        "version": 1,
        "pipeline_source": "schedule",
        "ref": "develop",
    }


@pytest.mark.unit
def test_what_the_monitor_writes_on_the_schedule_is_what_codebuild_acts_on(cbd, itd):
    itd.write_ci_run_options(
        environ={
            "CI_PIPELINE_SOURCE": "schedule",
            "CI_COMMIT_REF_NAME": "develop",
            "CI_PIPELINE_ID": "4242",
        }
    )
    assert cbd.ux_nightly_update_requested(cbd.read_ci_run_options(), environ={})


@pytest.mark.unit
def test_only_the_scheduled_develop_run_requests_the_update(cbd):
    options = {"pipeline_source": "schedule", "ref": "develop"}
    assert cbd.ux_nightly_update_requested(options, environ={}) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "options",
    [
        {"pipeline_source": "merge_request_event", "ref": "develop"},
        {"pipeline_source": "push", "ref": "develop"},
        {"pipeline_source": "web", "ref": "develop"},
        {"pipeline_source": "trigger", "ref": "develop"},
        {"pipeline_source": "schedule", "ref": "main"},
        {"pipeline_source": "schedule", "ref": "feature/rotate"},
        {"pipeline_source": "schedule"},
        {"ref": "develop"},
        {},
        {"pipeline_source": "Schedule", "ref": "develop"},
    ],
    ids=[
        "mr-button",
        "develop-push-button",
        "web",
        "trigger",
        "schedule-on-main",
        "schedule-on-a-branch",
        "no-ref",
        "no-source",
        "nothing",
        "wrong-case",
    ],
)
def test_every_other_run_leaves_the_review_stack_alone(cbd, options):
    assert cbd.ux_nightly_update_requested(options, environ={}) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["false", "0", "no", "off", " FALSE ", "Off", "", " "]
)
def test_the_codebuild_switch_turns_the_update_off(cbd, value):
    """An empty value is off, as IDP_KEEP_FAILED_STACK reads one, so an entry an
    operator declares with a blank value turns the update off rather than on."""
    options = {"pipeline_source": "schedule", "ref": "develop"}
    environ = {cbd.UX_NIGHTLY_DISABLE_ENV: value}
    assert cbd.ux_nightly_update_requested(options, environ=environ) is False


@pytest.mark.unit
@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_any_other_switch_value_leaves_the_schedule_deciding(cbd, value):
    options = {"pipeline_source": "schedule", "ref": "develop"}
    environ = {cbd.UX_NIGHTLY_DISABLE_ENV: value}
    assert cbd.ux_nightly_update_requested(options, environ=environ) is True


@pytest.mark.unit
def test_the_switch_is_read_from_the_process_environment_by_default(cbd, monkeypatch):
    options = {"pipeline_source": "schedule", "ref": "develop"}
    monkeypatch.setenv("IDP_UX_NIGHTLY_UPDATE", "false")
    assert cbd.ux_nightly_update_requested(options) is False
    monkeypatch.delenv("IDP_UX_NIGHTLY_UPDATE")
    assert cbd.ux_nightly_update_requested(options) is True


# ---------------------------------------------------------------------------
# The update itself: prechecks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_absent_stack_is_skipped_as_not_deployed_and_never_deployed_to(
    cbd, monkeypatch
):
    installed = _install(cbd, monkeypatch)
    installed.cf.gone.add("idp-ux-nightly")

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["reason"], outcome["phase"]) == (
        "skipped",
        "not_deployed",
        "precheck",
    )
    assert installed.deploy.commands == []
    assert installed.cf.made("update_stack") == []


@pytest.mark.unit
def test_a_stack_that_cannot_be_read_is_a_failure_and_not_an_absence(cbd, monkeypatch):
    installed = _install(cbd, monkeypatch)
    installed.cf.describe_errors["idp-ux-nightly"] = _client_error(
        "Throttling", "Rate exceeded", "DescribeStacks"
    )

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"]) == ("failed", "precheck")
    assert installed.deploy.commands == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [
        "UPDATE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "CREATE_IN_PROGRESS",
    ],
)
def test_a_stack_with_an_operation_in_progress_is_skipped_as_busy(
    cbd, monkeypatch, status
):
    """`idp-cli deploy` would only monitor it and exit 0 with nothing applied."""
    installed = _install(cbd, monkeypatch, _FakeCloudFormation(status=status))

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["reason"], outcome["status"]) == (
        "skipped",
        "busy",
        status,
    )
    assert installed.deploy.commands == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", ["UPDATE_ROLLBACK_FAILED", "ROLLBACK_COMPLETE", "DELETE_FAILED"]
)
def test_a_stack_no_update_can_start_from_is_a_precheck_failure(
    cbd, monkeypatch, status
):
    installed = _install(cbd, monkeypatch, _FakeCloudFormation(status=status))

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"], outcome["status"]) == (
        "failed",
        "precheck",
        status,
    )
    assert installed.cf.made("update_stack") == []
    assert installed.deploy.commands == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("boundary", "created_boundary"),
    [
        (A_RUNS_BOUNDARY, A_RUNS_BOUNDARY),
        (ITS_OWN_BOUNDARY, SHARED_BOUNDARY),
        (SHARED_BOUNDARY, ITS_OWN_BOUNDARY),
        ("", ITS_OWN_BOUNDARY),
        ("", ""),
        (
            "arn:aws:iam::123456789012:policy/ci/idp-ux-nightly-PermissionsBoundary",
            "arn:aws:iam::123456789012:policy/ci/idp-ux-nightly-PermissionsBoundary",
        ),
    ],
    ids=[
        "a-runs-boundary",
        "its-own-but-the-role-forces-the-shared-one",
        "shared-but-the-role-forces-its-own",
        "no-boundary-on-the-stack",
        "no-boundary-anywhere",
        "its-name-under-a-path",
    ],
)
def test_a_boundary_that_is_neither_its_own_nor_the_shared_one_is_refused(
    cbd, monkeypatch, boundary, created_boundary
):
    """A run's boundary and -iam role are deleted at that run's teardown."""
    installed = _install(
        cbd,
        monkeypatch,
        _FakeCloudFormation(boundary=boundary, created_boundary=created_boundary),
    )

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["reason"], outcome["phase"]) == (
        "skipped",
        "boundary_not_its_own",
        "precheck",
    )
    assert installed.cf.made("update_stack") == []
    assert installed.deploy.commands == []


@pytest.mark.unit
def test_a_stack_without_its_own_iam_pre_stack_is_refused(cbd, monkeypatch):
    installed = _install(cbd, monkeypatch)
    installed.cf.gone.add("idp-ux-nightly-iam")

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["reason"]) == ("skipped", "boundary_not_its_own")
    assert installed.deploy.commands == []


@pytest.mark.unit
@pytest.mark.parametrize("boundary", [ITS_OWN_BOUNDARY, SHARED_BOUNDARY])
def test_its_own_boundary_or_the_shared_one_is_updated(cbd, monkeypatch, boundary):
    installed = _install(cbd, monkeypatch, _FakeCloudFormation(boundary=boundary))

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert outcome["state"] == "updated"
    assert len(installed.deploy.commands) == 1


# ---------------------------------------------------------------------------
# The -iam pre-stack refresh
# ---------------------------------------------------------------------------


def _service_role_parameters() -> set[str]:
    class _Loader(yaml.SafeLoader):
        pass

    def _untagged(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    _Loader.add_multi_constructor("!", _untagged)
    with (REPO_ROOT / SERVICE_ROLE_TEMPLATE).open(encoding="utf-8") as handle:
        loader = _Loader(handle)
        try:
            return set(loader.get_single_data()["Parameters"])
        finally:
            loader.dispose()


@pytest.mark.unit
def test_the_iam_refresh_sends_tonights_template_and_keeps_every_parameter(
    cbd, monkeypatch
):
    """create_iam_resources is create-only, so this is how the role gains grants.

    Every parameter the template declares goes back as UsePreviousValue. One left
    out would be reset to its default by the refresh, which for the role's own
    boundary means silently removing it.
    """
    installed = _install(cbd, monkeypatch)

    cbd.update_ux_nightly_stack(TEMPLATE_URL)

    (update,) = installed.cf.made("update_stack")
    assert update["StackName"] == "idp-ux-nightly-iam"
    assert update["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert update["TemplateBody"] == (REPO_ROOT / SERVICE_ROLE_TEMPLATE).read_text(
        encoding="utf-8"
    )
    assert update["Parameters"] == [
        {"ParameterKey": "CreatedRolePermissionsBoundaryArn", "UsePreviousValue": True},
        {"ParameterKey": "ServiceRolePermissionsBoundaryArn", "UsePreviousValue": True},
        {"ParameterKey": "ManagedStackNamePrefix", "UsePreviousValue": True},
    ]
    assert {p["ParameterKey"] for p in update["Parameters"]} == (
        _service_role_parameters()
    )
    assert installed.cf.made("get_waiter") == ["stack_update_complete"]
    assert installed.cf.made("wait") == [
        {
            "StackName": "idp-ux-nightly-iam",
            "WaiterConfig": {"MaxAttempts": 60, "Delay": 15},
        }
    ]


@pytest.mark.unit
def test_an_iam_template_with_nothing_to_change_is_fine(cbd, monkeypatch):
    installed = _install(cbd, monkeypatch)
    installed.cf.update_error = _client_error(
        "ValidationError", "No updates are to be performed.", "UpdateStack"
    )

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert outcome["state"] == "updated"
    assert installed.cf.made("get_waiter") == []
    assert len(installed.deploy.commands) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("update_error", "waiter_error"),
    [
        (
            _client_error(
                "ValidationError",
                "Stack:idp-ux-nightly-iam is in UPDATE_ROLLBACK_FAILED state",
                "UpdateStack",
            ),
            None,
        ),
        (_client_error("AccessDenied", "not authorized", "UpdateStack"), None),
        (
            None,
            WaiterError(
                name="StackUpdateComplete",
                reason="Waiter encountered a terminal failure state",
                last_response={},
            ),
        ),
    ],
    ids=["update-refused", "update-denied", "update-rolled-back"],
)
def test_a_failed_iam_refresh_stops_before_the_deploy(
    cbd, monkeypatch, update_error, waiter_error
):
    installed = _install(cbd, monkeypatch)
    installed.cf.update_error = update_error
    installed.cf.waiter_error = waiter_error

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"]) == ("failed", "iam")
    assert installed.deploy.commands == []


@pytest.mark.unit
def test_an_iam_pre_stack_without_a_service_role_stops_before_the_deploy(
    cbd, monkeypatch
):
    installed = _install(cbd, monkeypatch, _FakeCloudFormation(role_arn=""))

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"]) == ("failed", "iam")
    assert installed.deploy.commands == []


# ---------------------------------------------------------------------------
# The deploy, and reading its result back off the stack
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_deploy_command_is_exactly_the_update_and_nothing_more(cbd, monkeypatch):
    """No --custom-config: a local file would land in a bucket the sweep reaps.

    No --admin-email or --parameters either: the update path needs neither, and
    every parameter keeps its previous value.
    """
    installed = _install(cbd, monkeypatch)

    cbd.update_ux_nightly_stack(TEMPLATE_URL)

    (command,) = installed.deploy.commands
    assert command.cmd == (
        f"idp-cli deploy --stack-name idp-ux-nightly --template-url {TEMPLATE_URL} "
        f"--role-arn {ROLE_ARN} --wait"
    )
    assert command.check is False
    assert command.timeout == cbd.UX_NIGHTLY_DEPLOY_TIMEOUT_SECONDS
    for flag in ("--custom-config", "--admin-email", "--parameters", "--headless"):
        assert flag not in command.cmd


@pytest.mark.unit
def test_an_update_complete_after_the_start_is_updated(cbd, monkeypatch):
    _install(cbd, monkeypatch)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["status"], outcome["phase"]) == (
        "updated",
        "UPDATE_COMPLETE",
        "",
    )


@pytest.mark.unit
def test_a_rolled_back_update_is_a_stack_failure(cbd, monkeypatch):
    cf = _FakeCloudFormation()
    deploy = _FakeDeploy(cbd, cf, returncode=1, final_status="UPDATE_ROLLBACK_COMPLETE")
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"], outcome["status"]) == (
        "failed",
        "stack",
        "UPDATE_ROLLBACK_COMPLETE",
    )


@pytest.mark.unit
def test_an_exit_of_zero_that_applied_nothing_is_up_to_date_not_updated(
    cbd, monkeypatch
):
    cf = _FakeCloudFormation()
    deploy = _FakeDeploy(cbd, cf, returncode=0, final_status=None)
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert outcome["state"] == "up_to_date"


@pytest.mark.unit
def test_a_stack_with_no_update_time_and_an_exit_of_zero_is_up_to_date(
    cbd, monkeypatch
):
    cf = _FakeCloudFormation(status="CREATE_COMPLETE")
    del cf.stacks["idp-ux-nightly"]["LastUpdatedTime"]
    deploy = _FakeDeploy(cbd, cf, returncode=0, final_status=None)
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["status"]) == ("up_to_date", "CREATE_COMPLETE")


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "✗ Error: An error occurred (ValidationError) when calling the UpdateStack "
        "operation: No updates are to be performed.",
        "✗ Error: An error occurred (ValidationError) when calling the UpdateStack\n"
        "operation: No updates are\nto be performed.",
    ],
    ids=["one-line", "wrapped-by-the-console"],
)
def test_a_template_identical_to_last_nights_is_up_to_date(cbd, monkeypatch, stdout):
    """CloudFormation refuses an update that changes nothing, so idp-cli exits 1."""
    cf = _FakeCloudFormation()
    deploy = _FakeDeploy(cbd, cf, returncode=1, final_status=None, stdout=stdout)
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert outcome["state"] == "up_to_date"


@pytest.mark.unit
def test_a_failed_deploy_that_changed_nothing_is_a_stack_failure(cbd, monkeypatch):
    cf = _FakeCloudFormation()
    deploy = _FakeDeploy(
        cbd, cf, returncode=1, final_status=None, stderr="✗ Error: AccessDenied"
    )
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"], outcome["status"]) == (
        "failed",
        "stack",
        "UPDATE_COMPLETE",
    )


@pytest.mark.unit
def test_an_update_complete_status_left_by_an_earlier_night_is_not_updated(
    cbd, monkeypatch
):
    """The status alone says nothing about tonight: the timestamp has to agree."""
    cf = _FakeCloudFormation()
    deploy = _FakeDeploy(cbd, cf, returncode=2, final_status=None)
    _install(cbd, monkeypatch, cf, deploy)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert outcome["state"] != "updated"
    assert (outcome["state"], outcome["phase"]) == ("failed", "stack")


@pytest.mark.unit
def test_an_unexpected_exception_is_a_thread_failure_and_never_raises(cbd, monkeypatch):
    def broken_client(*args, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(cbd.boto3, "client", broken_client)

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert (outcome["state"], outcome["phase"]) == ("failed", "thread")
    assert isinstance(outcome["minutes"], float)


@pytest.mark.unit
def test_the_update_opts_its_own_thread_out_of_the_fail_fast_sweep(cbd, monkeypatch):
    """Set on the worker before anything runs, and only on that worker."""
    installed = _install(cbd, monkeypatch)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        outcome = executor.submit(cbd.update_ux_nightly_stack, TEMPLATE_URL).result(
            timeout=60
        )
    finally:
        executor.shutdown(wait=True)

    assert outcome["state"] == "updated"
    assert [flag for _service, _kwargs, flag in installed.clients] == [True]
    assert [command.never_abort for command in installed.deploy.commands] == [True]
    assert getattr(cbd._thread_local, "never_abort", False) is False


@pytest.mark.unit
def test_its_clients_ride_out_a_throttle_like_every_other_harness_client(
    cbd, monkeypatch
):
    installed = _install(cbd, monkeypatch)

    cbd.update_ux_nightly_stack(TEMPLATE_URL)

    assert installed.clients
    for service, kwargs, _flag in installed.clients:
        assert service == "cloudformation"
        assert kwargs.get("config") is cbd._THROTTLE_RETRY_CONFIG


def _scenario_absent(cbd, cf):
    cf.gone.add("idp-ux-nightly")
    return None


def _scenario_busy(cbd, cf):
    cf.stacks["idp-ux-nightly"]["StackStatus"] = "UPDATE_IN_PROGRESS"
    return None


def _scenario_a_runs_boundary(cbd, cf):
    cf.stacks["idp-ux-nightly"]["Parameters"][1]["ParameterValue"] = A_RUNS_BOUNDARY
    return None


def _scenario_iam_refused(cbd, cf):
    cf.update_error = _client_error("AccessDenied", "denied", "UpdateStack")
    return None


def _scenario_iam_rolled_back(cbd, cf):
    cf.waiter_error = WaiterError(name="x", reason="terminal", last_response={})
    return None


def _scenario_updated(cbd, cf):
    return None


def _scenario_rolled_back(cbd, cf):
    return _FakeDeploy(cbd, cf, returncode=1, final_status="UPDATE_ROLLBACK_COMPLETE")


def _scenario_up_to_date(cbd, cf):
    return _FakeDeploy(cbd, cf, returncode=0, final_status=None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "scenario",
    [
        _scenario_absent,
        _scenario_busy,
        _scenario_a_runs_boundary,
        _scenario_iam_refused,
        _scenario_iam_rolled_back,
        _scenario_updated,
        _scenario_rolled_back,
        _scenario_up_to_date,
    ],
    ids=lambda scenario: scenario.__name__.removeprefix("_scenario_"),
)
def test_no_path_through_the_update_deletes_anything(cbd, monkeypatch, scenario):
    """The stack has to outlive every run, so not one path may take anything away.

    Each path also returns the documented outcome shape, so the summary row always
    has something to render.
    """
    cf = _FakeCloudFormation()
    installed = _install(cbd, monkeypatch, cf, scenario(cbd, cf))

    outcome = cbd.update_ux_nightly_stack(TEMPLATE_URL)

    _assert_destroyed_nothing(installed)
    assert set(outcome) == {"state", "reason", "phase", "status", "minutes"}
    assert outcome["state"] in {"updated", "up_to_date", "skipped", "failed"}
    assert isinstance(outcome["minutes"], float)


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


@pytest.mark.unit
def test_the_update_source_names_no_teardown_path_and_no_delete_api():
    """The runtime checks above cover the paths a test drives; this covers the rest.

    The docstring is left out of the string scan: it says why CloudFront is never
    touched, which is the opposite of touching it.
    """
    update = _function("update_ux_nightly_stack")
    body = update.body[1:] if ast.get_docstring(update) else update.body
    called = set().union(*(_called_names(stmt) for stmt in body))

    assert "run_command" in called and "update_stack" in called
    assert called & set(FORBIDDEN_HELPERS) == set()
    assert [name for name in called if "delete" in name.lower()] == []
    assert [name for name in called if "disable" in name.lower()] == []
    strings = [
        node.value
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert strings
    assert not [text for text in strings if "cloudfront" in text.lower()]


@pytest.mark.unit
def test_the_update_marks_its_thread_before_doing_anything_else():
    update = _function("update_ux_nightly_stack")
    body = update.body[1:] if ast.get_docstring(update) else update.body
    first = body[0]

    assert isinstance(first, ast.Assign)
    assert ast.unparse(first) == "_thread_local.never_abort = True"


@pytest.mark.unit
def test_the_update_refreshes_the_same_template_create_iam_resources_deploys():
    """Two literals for one path drift apart; this keeps them one path."""

    def path_literals(name):
        return {
            node.value
            for node in ast.walk(_function(name))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(".yaml")
        }

    assert path_literals("update_ux_nightly_stack") == {SERVICE_ROLE_TEMPLATE}
    assert path_literals("create_iam_resources") == {SERVICE_ROLE_TEMPLATE}


@pytest.mark.unit
def test_the_update_is_no_suite_step_probe_or_startup_sweep(cbd):
    update = cbd.update_ux_nightly_stack

    assert not update.__name__.startswith("cleanup_stale_")
    for step in (*cbd.PARALLEL_TEST_STEPS, *cbd.SEQUENTIAL_TEST_STEPS):
        assert update not in step
    assert all(probe.validate_fn is not update for probe in cbd.PROBE_VARIANTS)
    assert update.__name__ not in {sweep.func for sweep in cbd.STARTUP_SWEEPS}


# ---------------------------------------------------------------------------
# Joining the update, bounded from main()'s start
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    def __init__(self) -> None:
        self.shutdowns: list[bool] = []

    def shutdown(self, wait: bool = True, **kwargs: object) -> None:
        self.shutdowns.append(wait)


class _StillRunning:
    """A future that never finishes, recording the timeout each wait was given."""

    def __init__(self, cbd) -> None:
        self._cbd = cbd
        self.timeouts: list[float] = []

    def result(self, timeout: float | None = None) -> dict:
        self.timeouts.append(timeout)
        raise self._cbd.FuturesTimeoutError()


@pytest.mark.unit
def test_the_wait_is_whatever_is_left_of_the_deadline_since_main_started(cbd):
    future, executor = _StillRunning(cbd), _RecordingExecutor()

    outcome = cbd.join_ux_nightly_update(
        future, executor, started=100.0, clock=lambda: 700.0, deadline=5400.0
    )

    assert future.timeouts == [4800.0]
    assert outcome == {"state": "running", "minutes": 10.0}
    assert executor.shutdowns == [False]


@pytest.mark.unit
def test_a_join_reached_after_the_deadline_does_not_wait_at_all(cbd):
    future, executor = _StillRunning(cbd), _RecordingExecutor()

    outcome = cbd.join_ux_nightly_update(
        future, executor, started=0.0, clock=lambda: 7200.0
    )

    assert future.timeouts == [0.0]
    assert outcome == {"state": "running", "minutes": 120.0}
    assert executor.shutdowns == [False]


@pytest.mark.unit
def test_the_default_deadline_is_the_ninety_minute_join(cbd):
    future, executor = _StillRunning(cbd), _RecordingExecutor()

    cbd.join_ux_nightly_update(future, executor, started=0.0, clock=lambda: 0.0)

    assert future.timeouts == [90 * 60]
    assert cbd.UX_NIGHTLY_JOIN_DEADLINE_SECONDS == 90 * 60
    assert cbd.UX_NIGHTLY_DEPLOY_TIMEOUT_SECONDS < cbd.UX_NIGHTLY_JOIN_DEADLINE_SECONDS


@pytest.mark.unit
def test_the_join_deadline_leaves_the_headroom_main_does_not_measure(cbd, itd):
    """The deadline counts from main()'s start; the two bounds do not.

    CodeBuild's project timeout counts from the build's start and the GitLab
    monitor's handoff from the monitor's own, and the pipeline's source stage, the
    build's provisioning, its install phase and whatever main() does after the join
    all fall between those starts and the deadline. Nothing measures them, so the
    headroom they share is pinned at what it is today: twenty minutes below the
    handoff and thirty below the timeout. Raising the deadline, or lowering either
    bound, then fails here until somebody has measured the allowance.
    """
    template = (
        REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"
    ).read_text(encoding="utf-8")
    (timeout_minutes,) = re.findall(
        r"^\s*TimeoutInMinutes:\s*(\d+)\s*$", template, re.MULTILINE
    )
    deadline = cbd.UX_NIGHTLY_JOIN_DEADLINE_SECONDS
    assert itd.MONITOR_HANDOFF_SECONDS - deadline >= 20 * 60
    assert int(timeout_minutes) * 60 - deadline >= 30 * 60


@pytest.mark.unit
def test_a_finished_update_hands_back_its_outcome(cbd):
    future: Future = Future()
    finished = {
        "state": "updated",
        "reason": "",
        "phase": "",
        "status": "UPDATE_COMPLETE",
        "minutes": 31.5,
    }
    future.set_result(finished)
    executor = _RecordingExecutor()

    outcome = cbd.join_ux_nightly_update(
        future, executor, started=0.0, clock=lambda: 60.0
    )

    assert outcome == finished
    assert executor.shutdowns == [False]


@pytest.mark.unit
@pytest.mark.parametrize(
    "settle",
    [
        lambda future: future.set_exception(RuntimeError("thread died")),
        lambda future: future.set_result("updated"),
        lambda future: future.set_result(None),
    ],
    ids=["raised", "not-a-dict", "nothing"],
)
def test_a_thread_that_died_or_returned_junk_is_a_thread_failure(cbd, settle):
    future: Future = Future()
    settle(future)
    executor = _RecordingExecutor()

    outcome = cbd.join_ux_nightly_update(
        future, executor, started=0.0, clock=lambda: 1.0
    )

    assert outcome == {"state": "failed", "phase": "thread"}
    assert executor.shutdowns == [False]


@pytest.mark.unit
def test_a_real_update_thread_past_the_deadline_is_left_running(cbd):
    """The real executor and future, so the timeout class caught is the real one."""
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(release.wait, 60)
        outcome = cbd.join_ux_nightly_update(
            future, executor, started=0.0, clock=lambda: 5400.0
        )
        assert outcome == {"state": "running", "minutes": 90.0}
        assert not future.done()
    finally:
        release.set()
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# The summary row: fixed vocabulary, never the verdict
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            {"state": "updated", "status": "UPDATE_COMPLETE", "minutes": 34.2},
            (
                "passed",
                "Updated in place to tonight's template — UPDATE_COMPLETE in 34.2 min",
            ),
        ),
        (
            {"state": "up_to_date", "status": "UPDATE_COMPLETE", "minutes": 2.0},
            ("passed", "Already on tonight's template — nothing to update"),
        ),
        (
            {"state": "not_requested"},
            ("skipped", "Not updated — not a scheduled develop run"),
        ),
        (
            {"state": "skipped", "reason": "not_deployed", "phase": "precheck"},
            ("skipped", "Not updated — the stack is not deployed in this account"),
        ),
        (
            {"state": "skipped", "reason": "busy", "status": "UPDATE_IN_PROGRESS"},
            (
                "skipped",
                "Not updated — another operation was in progress (UPDATE_IN_PROGRESS)",
            ),
        ),
        (
            {"state": "skipped", "reason": "boundary_not_its_own"},
            (
                "warning",
                "Not updated — its permissions boundary is neither its own nor the shared one",
            ),
        ),
        (
            {
                "state": "failed",
                "phase": "stack",
                "status": "UPDATE_ROLLBACK_COMPLETE",
                "minutes": 12.0,
            },
            (
                "warning",
                "Update did not complete — UPDATE_ROLLBACK_COMPLETE after 12.0 min (stack)",
            ),
        ),
        (
            {"state": "failed", "phase": "thread"},
            ("warning", "Update did not complete — UNKNOWN after 0.0 min (thread)"),
        ),
        (
            {"state": "running", "minutes": 88.0},
            (
                "warning",
                "Still updating when the run ended — 88.0 min so far; CloudFormation carries on",
            ),
        ),
        (
            {"state": "not_started"},
            ("cancelled", "Not started (publish failed)"),
        ),
        (None, ("cancelled", "Not reported")),
        ({}, ("cancelled", "Not reported")),
        ({"state": "renamed_later"}, ("cancelled", "Not reported")),
        ({"state": "skipped", "reason": "a_new_reason"}, ("cancelled", "Not reported")),
        ({"state": "skipped"}, ("cancelled", "Not reported")),
    ],
    ids=[
        "updated",
        "up_to_date",
        "not_requested",
        "not_deployed",
        "busy",
        "boundary",
        "failed",
        "failed-in-the-thread",
        "running",
        "not_started",
        "none",
        "empty",
        "unknown-state",
        "unknown-skip-reason",
        "skip-without-reason",
    ],
)
def test_every_outcome_renders_its_fixed_row(cbd, outcome, expected):
    assert cbd.ux_nightly_row(outcome) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "junk",
    [
        "OVERALL: FAIL",
        "OVERALL",
        "OVERALL_PASS",
        "UPDATE_COMPLETE\n💥 OVERALL: FAIL",
        "💥 OVERALL: FAIL",
        "update_complete",
        "A" * 65,
        "",
        42,
        None,
        ["UPDATE_COMPLETE"],
        {"OVERALL": "FAIL"},
    ],
)
@pytest.mark.parametrize("state", ["updated", "failed", "busy"])
def test_no_status_the_update_reports_can_put_overall_in_the_row(cbd, state, junk):
    outcome = (
        {"state": "skipped", "reason": "busy", "status": junk}
        if state == "busy"
        else {"state": state, "status": junk, "phase": junk, "minutes": junk}
    )

    status, text = cbd.ux_nightly_row(outcome)

    assert "OVERALL" not in text
    assert "UNKNOWN" in text
    assert status in {"passed", "skipped", "warning"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcome",
    [
        {"state": ["updated"]},
        {"state": "failed", "phase": ["stack"], "status": {"a": 1}},
        {"state": "skipped", "reason": ["busy"]},
        {"state": "running", "minutes": float("nan")},
        {"state": "running", "minutes": float("inf")},
        {"state": "running", "minutes": True},
        {"state": "running", "minutes": "88"},
        "updated",
        ["updated"],
    ],
)
def test_a_malformed_outcome_never_breaks_the_summary(cbd, outcome):
    status, text = cbd.ux_nightly_row(outcome)

    assert status in {"passed", "skipped", "warning", "cancelled"}
    assert "OVERALL" not in text
    assert "nan" not in text and "inf" not in text


def _summary(cbd, *, primary_ok=True, **kwargs):
    stack = "idp-1015-000000"
    primary = {
        "stack_name": stack,
        "success": primary_ok,
        "step_results": {
            "Step 3: Default config": {"status": "passed" if primary_ok else "failed"}
        },
        "teardown": {"findings": []},
    }
    sweeps = {sweep.func: [] for sweep in cbd.STARTUP_SWEEPS}
    return cbd.build_consolidated_summary(stack, primary, [], True, sweeps, **kwargs)


@pytest.mark.unit
@pytest.mark.parametrize(
    "update",
    [
        {
            "state": "failed",
            "phase": "stack",
            "status": "UPDATE_ROLLBACK_COMPLETE",
            "minutes": 12.0,
        },
        {"state": "failed", "phase": "thread"},
        {"state": "running", "minutes": 88.0},
        {"state": "skipped", "reason": "boundary_not_its_own"},
        {"state": "failed", "status": "OVERALL: FAIL", "phase": "OVERALL: FAIL"},
    ],
    ids=["rolled-back", "thread", "running", "boundary", "hostile-status"],
)
def test_an_update_that_went_wrong_leaves_a_passing_run_passing(cbd, update):
    text = _summary(cbd, ux_nightly_update=update)
    lines = text.splitlines()

    assert lines[-2] == "🎉 OVERALL: PASS"
    assert text.count("OVERALL:") == 1
    assert "  ⚠️ " in text.split("🌙", 1)[1]


@pytest.mark.unit
def test_an_updated_stack_does_not_rescue_a_failing_run(cbd):
    update = {"state": "updated", "status": "UPDATE_COMPLETE", "minutes": 30.0}

    text = _summary(cbd, primary_ok=False, ux_nightly_update=update)

    assert text.splitlines()[-2] == "💥 OVERALL: FAIL"
    assert text.count("OVERALL:") == 1


@pytest.mark.unit
def test_no_outcome_renders_no_section_and_leaves_the_table_as_it_was(cbd):
    without = _summary(cbd)

    assert "🌙" not in without
    assert _summary(cbd, ux_nightly_update=None) == without


@pytest.mark.unit
def test_the_section_follows_the_probes_and_precedes_the_verdict(cbd):
    update = {"state": "updated", "status": "UPDATE_COMPLETE", "minutes": 34.2}

    lines = _summary(cbd, ux_nightly_update=update).splitlines()

    heading = lines.index(
        "🌙 UX review stack (idp-ux-nightly, updated in place — never part of the "
        "verdict)"
    )
    assert lines[heading + 1] == (
        "  ✅ Updated in place to tonight's template — UPDATE_COMPLETE in 34.2 min"
    )
    assert lines.index("🔬 Deployment-variant probes") < heading
    assert heading < lines.index("🎉 OVERALL: PASS")
    assert lines[heading - 1] == ""


# ---------------------------------------------------------------------------
# main(): wiring, read from source because main() runs a whole night
# ---------------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }


def _calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _names_in(node: ast.AST) -> set[str]:
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


@pytest.mark.unit
def test_main_only_ever_submits_the_update_to_its_own_single_worker_executor():
    main = _function("main")

    assert _calls_to(main, "update_ux_nightly_stack") == []
    mentions = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Name) and node.id == "update_ux_nightly_stack"
    ]
    submits = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "update_ux_nightly_stack"
    ]
    assert len(submits) == 1 and len(mentions) == 1
    (submit,) = submits
    assert isinstance(submit.func, ast.Attribute)
    assert isinstance(submit.func.value, ast.Name)
    executor = submit.func.value.id
    constructions = [
        node.value
        for node in ast.walk(main)
        if isinstance(node, ast.Assign)
        and [ast.unparse(t) for t in node.targets] == [executor]
        and isinstance(node.value, ast.Call)
    ]
    assert [ast.unparse(call) for call in constructions] == [
        "ThreadPoolExecutor(max_workers=1)"
    ]


@pytest.mark.unit
def test_no_branch_on_the_update_assigns_the_verdict():
    main = _function("main")
    guards = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and any(name.startswith("ux_nightly_") for name in _names_in(node.test))
    ]

    assert len(guards) >= 2, "the launch and the join are both guarded"
    for guard in guards:
        for stmt in ast.walk(guard):
            targets = (
                stmt.targets
                if isinstance(stmt, ast.Assign)
                else [stmt.target]
                if isinstance(stmt, (ast.AugAssign, ast.AnnAssign))
                else []
            )
            assigned = {ast.unparse(target) for target in targets}
            assert not assigned & {"stack_success", "failure_reason"}, (
                f"main() line {stmt.lineno}: a branch on the UX update assigns "
                f"{sorted(assigned)}"
            )


@pytest.mark.unit
def test_the_outcome_reaches_only_the_summary_row_and_a_log_line():
    """Nothing carries it into stack_success, failure_reason or a notification."""
    main = _function("main")
    parents = _parents(main)
    reads = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Name)
        and node.id == "ux_nightly_outcome"
        and isinstance(node.ctx, ast.Load)
    ]

    assert reads
    for read in reads:
        parent = parents[read]
        if (
            isinstance(parent, ast.keyword)
            and parent.arg == "ux_nightly_update"
            and isinstance(parents[parent], ast.Call)
            and ast.unparse(parents[parent].func) == "build_consolidated_summary"
        ):
            continue
        node = read
        while node in parents and not (
            isinstance(node, ast.Call) and ast.unparse(node.func) == "print"
        ):
            node = parents[node]
        assert isinstance(node, ast.Call) and ast.unparse(node.func) == "print", (
            f"main() line {read.lineno} reads ux_nightly_outcome outside the "
            "summary row and the log line"
        )


@pytest.mark.unit
def test_exactly_one_summary_carries_the_update_and_it_is_the_final_table():
    main = _function("main")
    parents = _parents(main)
    calls = _calls_to(main, "build_consolidated_summary")
    carrying = [
        call
        for call in calls
        if any(kw.arg == "ux_nightly_update" for kw in call.keywords)
    ]

    assert len(calls) >= 3, "the progress, interim and final tables"
    assert len(carrying) == 1
    final = parents[carrying[0]]
    assert isinstance(final, ast.Assign)
    assert [ast.unparse(t) for t in final.targets] == ["consolidated"]


@pytest.mark.unit
def test_every_summary_call_still_passes_the_sweep_findings():
    main = _function("main")
    calls = _calls_to(main, "build_consolidated_summary")

    assert calls
    for call in calls:
        passed = _names_in(
            ast.Tuple(elts=[*call.args, *(kw.value for kw in call.keywords)])
        )
        assert "startup_sweep_findings" in passed, (
            f"main() line {call.lineno} builds a summary without the sweep findings"
        )


@pytest.mark.unit
def test_main_reads_the_options_once_and_bounds_the_join_from_its_own_start():
    main = _function("main")

    assert len(_calls_to(main, "read_ci_run_options")) == 1
    (join,) = _calls_to(main, "join_ux_nightly_update")
    assert {kw.arg: ast.unparse(kw.value) for kw in join.keywords} == {
        "started": "main_started"
    }
    starts = [
        node
        for node in main.body
        if isinstance(node, ast.Assign)
        and [ast.unparse(t) for t in node.targets] == ["main_started"]
    ]
    assert [ast.unparse(node.value) for node in starts] == ["time.monotonic()"]
