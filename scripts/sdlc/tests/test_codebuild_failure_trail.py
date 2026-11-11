"""Tests for the deploy-failure evidence chain and the fixes around it.

Covers the three things that made the 2026-07-27 CI failure hard to diagnose:
  1. A CodeBuild custom-resource failure reports only "CodeBuild failed with
     status: FAILED" to CloudFormation, so the summary must follow that trail
     into the failing build's own log stream
     (get_codebuild_failure_details).
  2. The recovery command must be derived from the stack's REAL status —
     `continue-update-rollback` is invalid from a CREATE rollback
     (_recovery_command).
  3. The S3 bucket-config OperationAborted conflict that wedged the rollback is
     a known transient race and must be retryable
     (_is_transient_deploy_race).
"""

from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeCfn:
    def __init__(self, resources=None, status=None, raise_on_describe_stacks=False):
        self._resources = resources or {}
        self._status = status
        self._raise = raise_on_describe_stacks

    def get_paginator(self, name):
        raise RuntimeError("no paginator in this botocore version")

    def describe_stack_resources(self, StackName):  # noqa: N803
        return {"StackResources": self._resources.get(StackName, [])}

    def describe_stacks(self, StackName):  # noqa: N803
        if self._raise:
            raise RuntimeError(f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": self._status}]}


class _FakeCodeBuild:
    def __init__(self, builds_by_project):
        self._builds = builds_by_project

    def list_builds_for_project(self, projectName, sortOrder=None):  # noqa: N803
        return {"ids": [b["id"] for b in self._builds.get(projectName, [])]}

    def batch_get_builds(self, ids):
        flat = [b for builds in self._builds.values() for b in builds]
        return {"builds": [b for b in flat if b["id"] in ids]}


class _FakeLogs:
    def __init__(self, events_by_stream=None, raise_error=None):
        self._events = events_by_stream or {}
        self._raise = raise_error

    def get_log_events(self, logGroupName, logStreamName, startFromHead=None):  # noqa: N803
        if self._raise:
            raise RuntimeError(self._raise)
        msgs = self._events.get((logGroupName, logStreamName), [])
        return {"events": [{"message": m} for m in msgs]}


def _patch_clients(cbd, monkeypatch, cfn=None, codebuild=None, logs=None):
    def _factory(service, *a, **kw):
        if service == "cloudformation":
            return cfn or _FakeCfn()
        if service == "codebuild":
            return codebuild or _FakeCodeBuild({})
        if service == "logs":
            return logs or _FakeLogs()
        raise AssertionError(f"unexpected client: {service}")

    monkeypatch.setattr(cbd.boto3, "client", _factory)


# The real event shape emitted by get_cloudformation_logs for the failure that
# started the 2026-07-27 incident.
def _codebuild_cr_event():
    return {
        "stack_name": "idp-0727-211933-MULTIDOCDISCOVERYSTACK-1MQRU7V6EWVP3",
        "resource_type": "Custom::DockerBuild",
        "logical_id": "DockerBuildRun",
        "status": "CREATE_FAILED",
        "reason": (
            "Received response status [FAILED] from custom resource. Message "
            "returned: CodeBuild failed with status: FAILED (RequestId: "
            "4d503034-3a38-406a-8cc4-af5bb4cfa38e)"
        ),
    }


def _failed_build(build_id="proj:abc123"):
    return {
        "id": build_id,
        "buildStatus": "FAILED",
        "phases": [
            {"phaseType": "PRE_BUILD", "phaseStatus": "SUCCEEDED"},
            {
                "phaseType": "BUILD",
                "phaseStatus": "FAILED",
                "contexts": [
                    {
                        "message": (
                            "Error while executing command: docker build -t "
                            "$ECR_REPO_URI:$IMAGE_TAG .. Reason: exit status 1"
                        )
                    }
                ],
            },
        ],
        "logs": {
            "groupName": "/aws/codebuild/DockerBuildProject-9qS7dcXXFFIe",
            "streamName": "f86e3cbc-09b4-47fa-9d3d-aced84824872",
            "deepLink": "https://console.aws.amazon.com/cloudwatch/home#logEvent:x",
        },
    }


# --------------------------------------------------------------------------- #
# get_codebuild_failure_details — follow the trail past the CFN dead end
# --------------------------------------------------------------------------- #


def test_codebuild_detail_surfaces_the_real_pip_error(cbd, monkeypatch):
    """The whole point: turn "CodeBuild failed" into the actual root cause."""
    nested = "idp-0727-211933-MULTIDOCDISCOVERYSTACK-1MQRU7V6EWVP3"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "DockerBuildProject",
                    "PhysicalResourceId": "DockerBuildProject-9qS7dcXXFFIe",
                }
            ]
        }
    )
    codebuild = _FakeCodeBuild({"DockerBuildProject-9qS7dcXXFFIe": [_failed_build()]})
    logs = _FakeLogs(
        {
            (
                "/aws/codebuild/DockerBuildProject-9qS7dcXXFFIe",
                "f86e3cbc-09b4-47fa-9d3d-aced84824872",
            ): [
                "Downloading numpy-1.26.4-cp312-cp312-manylinux.whl (18.0 MB)",
                "ERROR: Exception:",
                "BrokenPipeError: [Errno 32] Broken pipe",
                "ERROR: failed to build: failed to solve: process "
                '"/bin/sh -c pip install ..." did not complete successfully',
            ]
        }
    )
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild, logs=logs)

    details = cbd.get_codebuild_failure_details(
        "idp-0727-211933", [_codebuild_cr_event()]
    )

    assert len(details) == 1
    d = details[0]
    assert d["project_name"] == "DockerBuildProject-9qS7dcXXFFIe"
    assert d["build_status"] == "FAILED"
    assert d["failed_phase"] == "BUILD"
    assert "docker build" in d["phase_error"]
    # The evidence that was missing from the original summary.
    assert "BrokenPipeError" in d["log_tail"]
    assert d["log_url"].startswith("https://")


def test_codebuild_detail_skipped_when_no_codebuild_failure(cbd, monkeypatch):
    """An ordinary failure must not trigger CodeBuild lookups (or extra cost)."""
    _patch_clients(cbd, monkeypatch)
    events = [
        {
            "resource_type": "AWS::Logs::LogGroup",
            "status": "CREATE_FAILED",
            "reason": "The specified log group does not exist",
        }
    ]
    assert cbd.get_codebuild_failure_details("stack", events) == []
    assert cbd.get_codebuild_failure_details("stack", []) == []
    assert cbd.get_codebuild_failure_details("stack", None) == []


def test_codebuild_detail_prefers_the_failed_build_over_a_later_success(
    cbd, monkeypatch
):
    """With the new in-Lambda retry, the newest build may be a passing retry.

    The build we must report on is the one that FAILED.
    """
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "DockerBuildProject",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )
    succeeded = {"id": "proj:new", "buildStatus": "SUCCEEDED", "phases": [], "logs": {}}
    codebuild = _FakeCodeBuild({"proj": [succeeded, _failed_build("proj:old")]})
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild)

    ev = dict(_codebuild_cr_event(), stack_name=nested)
    details = cbd.get_codebuild_failure_details("parent", [ev])

    assert [d["build_id"] for d in details] == ["proj:old"]


def _in_progress_build(build_id="proj:retry"):
    """A build that has started but not finished — no failure phase yet."""
    return {
        "id": build_id,
        "buildStatus": "IN_PROGRESS",
        "phases": [
            {"phaseType": "PRE_BUILD", "phaseStatus": "SUCCEEDED"},
            {"phaseType": "BUILD"},  # still running: no phaseStatus
        ],
        "logs": {
            "groupName": "/aws/codebuild/proj",
            "streamName": "retry-stream",
            "deepLink": "https://console.aws.amazon.com/cloudwatch/home#retry",
        },
    }


def test_codebuild_detail_skips_an_in_progress_build_for_the_real_failure(
    cbd, monkeypatch
):
    """An IN_PROGRESS build is not "the failure" — keep looking.

    Regression guard. The selector originally matched any build that was
    `!= "SUCCEEDED"`, which includes IN_PROGRESS. Because the DockerBuildRun
    custom resource now retries once, the newest build is frequently still
    running when the summary captures evidence — so the selector would stop at
    the retry, yielding an empty phase_error and a partial log tail, and never
    reach the build that actually failed. Worse, the prompt labels that section
    authoritative, actively steering the model toward empty evidence.
    """
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "DockerBuildProject",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )
    # Newest first, as list_builds_for_project(sortOrder=DESCENDING) returns.
    codebuild = _FakeCodeBuild(
        {"proj": [_in_progress_build("proj:retry"), _failed_build("proj:real")]}
    )
    logs = _FakeLogs(
        {
            (
                "/aws/codebuild/DockerBuildProject-9qS7dcXXFFIe",
                "f86e3cbc-09b4-47fa-9d3d-aced84824872",
            ): ["BrokenPipeError: [Errno 32] Broken pipe"]
        }
    )
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild, logs=logs)

    ev = dict(_codebuild_cr_event(), stack_name=nested)
    details = cbd.get_codebuild_failure_details("parent", [ev])

    assert [d["build_id"] for d in details] == ["proj:real"], (
        "reported on the in-progress retry instead of the build that failed"
    )
    # And the report is actually useful, not empty.
    assert details[0]["failed_phase"] == "BUILD"
    assert "docker build" in details[0]["phase_error"]
    assert "BrokenPipeError" in details[0]["log_tail"]


def test_codebuild_detail_returns_nothing_when_only_in_progress(cbd, monkeypatch):
    """No terminal failure yet → report nothing rather than empty noise."""
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "P",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )
    codebuild = _FakeCodeBuild({"proj": [_in_progress_build()]})
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild)

    ev = dict(_codebuild_cr_event(), stack_name=nested)
    assert cbd.get_codebuild_failure_details("parent", [ev]) == []


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "TIMED_OUT", "STOPPED"])
def test_every_terminal_failure_status_is_reported(cbd, monkeypatch, status):
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "P",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )
    build = dict(_failed_build("proj:x"), buildStatus=status)
    codebuild = _FakeCodeBuild({"proj": [build]})
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild)

    ev = dict(_codebuild_cr_event(), stack_name=nested)
    details = cbd.get_codebuild_failure_details("parent", [ev])
    assert [d["build_status"] for d in details] == [status]


def test_codebuild_detail_degrades_gracefully_on_api_error(cbd, monkeypatch):
    """Evidence collection must never raise into the summary path."""
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "P",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )

    class _Boom:
        def list_builds_for_project(self, **kw):
            raise RuntimeError("AccessDenied")

    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=_Boom())
    ev = dict(_codebuild_cr_event(), stack_name=nested)
    details = cbd.get_codebuild_failure_details("parent", [ev])
    assert len(details) == 1
    assert "AccessDenied" in details[0]["error"]


def test_codebuild_detail_handles_unreadable_log_stream(cbd, monkeypatch):
    nested = "nested-stack"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "P",
                    "PhysicalResourceId": "proj",
                }
            ]
        }
    )
    codebuild = _FakeCodeBuild({"proj": [_failed_build()]})
    logs = _FakeLogs(raise_error="ResourceNotFoundException")
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild, logs=logs)

    ev = dict(_codebuild_cr_event(), stack_name=nested)
    details = cbd.get_codebuild_failure_details("parent", [ev])
    assert "could not read" in details[0]["log_tail"]
    # The deep link is still handed over so a human can go look.
    assert details[0]["log_url"].startswith("https://")


# --------------------------------------------------------------------------- #
# _recovery_command — the command the old summary got wrong
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status,expected_verb",
    [
        # The incident case: a CREATE that rolled back. continue-update-rollback
        # is INVALID here; the old summary recommended it anyway.
        ("ROLLBACK_FAILED", "delete-stack"),
        ("ROLLBACK_COMPLETE", "delete-stack"),
        ("CREATE_FAILED", "delete-stack"),
        # The ONLY status where continue-update-rollback applies.
        ("UPDATE_ROLLBACK_FAILED", "continue-update-rollback"),
    ],
)
def test_recovery_command_matches_stack_state(cbd, monkeypatch, status, expected_verb):
    _patch_clients(cbd, monkeypatch, cfn=_FakeCfn(status=status))
    cmd, reported = cbd._recovery_command("my-stack")
    assert expected_verb in cmd
    assert "my-stack" in cmd
    assert reported == status


def test_recovery_command_never_suggests_continue_update_for_create_rollback(
    cbd, monkeypatch
):
    """Regression guard for the specific wrong advice in the 07-27 summary."""
    _patch_clients(cbd, monkeypatch, cfn=_FakeCfn(status="ROLLBACK_FAILED"))
    cmd, _ = cbd._recovery_command("idp-0727-211933")
    assert "continue-update-rollback" not in cmd


def test_recovery_command_handles_already_deleted_stack(cbd, monkeypatch):
    _patch_clients(cbd, monkeypatch, cfn=_FakeCfn(raise_on_describe_stacks=True))
    cmd, status = cbd._recovery_command("gone")
    assert cmd == ""
    assert status == ""


def test_recovery_command_gives_nothing_for_healthy_stack(cbd, monkeypatch):
    _patch_clients(cbd, monkeypatch, cfn=_FakeCfn(status="CREATE_COMPLETE"))
    cmd, status = cbd._recovery_command("fine")
    assert cmd == ""
    assert status == "CREATE_COMPLETE"


# --------------------------------------------------------------------------- #
# _is_transient_deploy_race — the S3 bucket-config conflict
# --------------------------------------------------------------------------- #


def _operation_aborted_event(status="DELETE_FAILED"):
    return {
        "resource_type": "Custom::S3BucketNotification",
        "logical_id": "TestSetBucketNotificationConfiguration",
        "status": status,
        "reason": (
            "Received response status [FAILED] from custom resource. Message "
            "returned: Error configuring bucket notification: An error occurred "
            "(OperationAborted) when calling the "
            "PutBucketNotificationConfiguration operation: A conflicting "
            "conditional operation is currently in progress against this "
            "resource. Please try again."
        ),
    }


def test_operation_aborted_delete_failure_is_retryable(cbd):
    """This is what wedged the stack in ROLLBACK_FAILED — a DELETE_FAILED.

    The pre-existing detector only looked at CREATE_FAILED, so it did not fire.
    """
    result = {"failure_type": "deploy", "cf_events": [_operation_aborted_event()]}
    assert cbd._is_transient_deploy_race(result) is True


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"])
def test_operation_aborted_retryable_in_every_relevant_phase(cbd, status):
    result = {"failure_type": "deploy", "cf_events": [_operation_aborted_event(status)]}
    assert cbd._is_transient_deploy_race(result) is True


def test_other_bucket_notification_errors_are_not_retryable(cbd):
    """A real permission/config error fails identically on retry — must surface."""
    result = {
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "Custom::S3BucketNotification",
                "status": "CREATE_FAILED",
                "reason": (
                    "Error configuring bucket notification: An error occurred "
                    "(AccessDenied) when calling the "
                    "PutBucketNotificationConfiguration operation"
                ),
            }
        ],
    }
    assert cbd._is_transient_deploy_race(result) is False


def test_status_scoping_is_per_entry_not_global(cbd):
    """Widening to DELETE_FAILED must not loosen the CREATE-only races.

    A LogGroup "does not exist" seen as a DELETE_FAILED is not the known create
    race, so it must not be retried.
    """
    result = {
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "AWS::Logs::LogGroup",
                "status": "DELETE_FAILED",
                "reason": "The specified log group does not exist",
            }
        ],
    }
    assert cbd._is_transient_deploy_race(result) is False


def test_operation_aborted_not_retried_for_validation_failure(cbd):
    result = {"failure_type": "validation", "cf_events": [_operation_aborted_event()]}
    assert cbd._is_transient_deploy_race(result) is False


# --------------------------------------------------------------------------- #
# _capture_cf_events — CodeBuild detail must be snapshotted before teardown
# --------------------------------------------------------------------------- #


def test_capture_cf_events_snapshots_codebuild_detail(cbd, monkeypatch):
    monkeypatch.setattr(cbd, "get_cloudformation_logs", lambda n: [_codebuild_cr_event()])
    monkeypatch.setattr(
        cbd,
        "get_codebuild_failure_details",
        lambda stack, events: [{"build_id": "proj:abc", "log_tail": "BrokenPipeError"}],
    )
    result = {}
    cbd._capture_cf_events(result, "idp-test")
    assert result["codebuild_failures"][0]["log_tail"] == "BrokenPipeError"


def test_capture_cf_events_survives_codebuild_lookup_failure(cbd, monkeypatch):
    """A failure collecting extra evidence must not lose the CF events."""

    def _boom(stack, events):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(cbd, "get_cloudformation_logs", lambda n: [_codebuild_cr_event()])
    monkeypatch.setattr(cbd, "get_codebuild_failure_details", _boom)
    result = {}
    cbd._capture_cf_events(result, "idp-test")
    assert result["cf_events"]
    assert "codebuild_failures" not in result


def test_capture_cf_events_skips_codebuild_when_no_events(cbd, monkeypatch):
    monkeypatch.setattr(cbd, "get_cloudformation_logs", lambda n: [])
    called = []
    monkeypatch.setattr(
        cbd,
        "get_codebuild_failure_details",
        lambda *a: called.append(a) or [],
    )
    result = {}
    cbd._capture_cf_events(result, "idp-test")
    assert called == []
    assert "error" in result["cf_events"][0]


# The marker set has to match what the handlers actually emit. The shared
# build-run handler (src/lambda/start_codebuild) serves both DockerBuildRun
# resources since #1824; a marker keyed only on the wording of the handler it
# replaced would leave this trail matching nothing.
_SHARED_HANDLER = (
    Path(__file__).resolve().parents[3] / "src" / "lambda" / "start_codebuild" / "index.py"
)


def test_the_shared_build_handler_still_emits_a_recognised_failure_reason(cbd):
    source = _SHARED_HANDLER.read_text(encoding="utf-8")
    emitted = "build did not complete - status: ["
    assert emitted in source, (
        "the shared build-run handler's failure wording changed; update "
        "_CODEBUILD_CR_FAILURE_MARKERS to match it"
    )
    reason = "build did not complete - status: [FAILED]"
    assert any(m in reason.lower() for m in cbd._CODEBUILD_CR_FAILURE_MARKERS)


@pytest.mark.parametrize(
    "reason",
    [
        "Received response status [FAILED] from custom resource. Message returned: "
        "build did not complete - status: [FAILED] (RequestId: 1)",
        "Received response status [FAILED] from custom resource. Message returned: "
        "Build DockerBuildProject-x:y did not complete within the Lambda budget.",
        "Received response status [FAILED] from custom resource. Message returned: "
        "CodeBuild failed with status: FAULT",
    ],
)
def test_every_handler_wording_triggers_the_trail(cbd, monkeypatch, reason):
    nested = "idp-0727-211933-MULTIDOCDISCOVERYSTACK-1MQRU7V6EWVP3"
    cfn = _FakeCfn(
        resources={
            nested: [
                {
                    "ResourceType": "AWS::CodeBuild::Project",
                    "LogicalResourceId": "DockerBuildProject",
                    "PhysicalResourceId": "DockerBuildProject-9qS7dcXXFFIe",
                }
            ]
        }
    )
    codebuild = _FakeCodeBuild({"DockerBuildProject-9qS7dcXXFFIe": [_failed_build()]})
    _patch_clients(cbd, monkeypatch, cfn=cfn, codebuild=codebuild)
    event = dict(_codebuild_cr_event(), reason=reason)

    details = cbd.get_codebuild_failure_details("idp-0727-211933", [event])

    assert [d["project_name"] for d in details] == ["DockerBuildProject-9qS7dcXXFFIe"]
