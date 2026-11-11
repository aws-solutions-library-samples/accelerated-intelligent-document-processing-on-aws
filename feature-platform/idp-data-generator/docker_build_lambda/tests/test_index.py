# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The data generator's image-build custom resource waits by polling (#1824).

The handler this replaces waited inside one Lambda invocation for at most 840
seconds, so a build that CodeBuild was slow to start failed the stack operation,
and the rollback's rebuild failed the same way. It now starts the build on
Create/Update and lets crhelper re-invoke the poller until the build settles,
which bounds the wait by CloudFormation's hour rather than Lambda's 15 minutes.

``crhelper`` is a Lambda dependency (``requirements.txt``), not a test
dependency, so it is stubbed: its decorators only register handlers, and the
suite calls them directly.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_INDEX_PATH = Path(__file__).resolve().parents[1] / "index.py"
_MODULE_NAME = "idp_data_generator_docker_build_index_under_test"


class _FakeCfnResource:
    def __init__(self, *_args, **_kwargs):
        self.Data: dict = {}

    def _passthrough(self, func):
        return func

    create = update = delete = _passthrough
    poll_create = poll_update = _passthrough

    def init_failure(self, exception):  # pragma: no cover - never hit in tests
        raise exception

    def __call__(self, event, context):  # pragma: no cover - handler not exercised
        raise AssertionError("the suite calls the handlers directly")


@pytest.fixture(scope="module")
def index():
    fake_crhelper = SimpleNamespace(CfnResource=_FakeCfnResource)
    with (
        patch.dict(sys.modules, {"crhelper": fake_crhelper}),
        patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1"}, clear=False),
        patch("boto3.client", side_effect=lambda name, **_kw: MagicMock(name=name)),
    ):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_MODULE_NAME, None)


@pytest.fixture
def codebuild(index, monkeypatch):
    client = MagicMock(name="codebuild")
    client.start_build.return_value = {"build": {"id": "proj:build-1"}}
    monkeypatch.setattr(index, "CODEBUILD", client)
    monkeypatch.setattr(index.HELPER, "Data", {})
    return client


def _poll_event(physical_id=None):
    event = {"CrHelperData": {"build_id": "proj:build-1"}}
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def _status(codebuild, status):
    codebuild.batch_get_builds.return_value = {
        "builds": [{"id": "proj:build-1", "buildStatus": status}]
    }


def test_the_module_under_test_is_this_worktree_copy(index):
    assert Path(index.__file__) == _INDEX_PATH


def test_create_and_update_start_the_build_and_record_its_id(index, codebuild):
    event = {"ResourceProperties": {"ProjectName": "DockerBuildProject-EXAMPLE"}}

    index.start_build(event, None)

    codebuild.start_build.assert_called_once_with(
        projectName="DockerBuildProject-EXAMPLE"
    )
    assert index.HELPER.Data["build_id"] == "proj:build-1"


def test_a_build_still_queued_is_polled_again_rather_than_failed(index, codebuild):
    """The defect: a build CodeBuild has not finished is not a failure."""
    _status(codebuild, "IN_PROGRESS")

    assert index.poll_build(_poll_event(), None) is None


def test_a_succeeded_build_completes_with_a_stable_physical_id(index, codebuild):
    _status(codebuild, "SUCCEEDED")

    assert index.poll_build(_poll_event(), None) == index.DEFAULT_PHYSICAL_ID
    assert index.poll_build(_poll_event("existing-id"), None) == "existing-id"


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "TIMED_OUT", "STOPPED"])
def test_a_build_that_ends_otherwise_fails_naming_the_status(index, codebuild, status):
    _status(codebuild, status)

    with pytest.raises(RuntimeError) as raised:
        index.poll_build(_poll_event(), None)

    # The deploy-failure trail (scripts/sdlc/codebuild_deployment.py) follows a
    # reason carrying this wording into the build's log.
    assert f"CodeBuild failed with status: {status}" in str(raised.value)


def test_a_missing_build_fails(index, codebuild):
    codebuild.batch_get_builds.return_value = {"builds": []}

    with pytest.raises(RuntimeError, match="could not find build"):
        index.poll_build(_poll_event(), None)


def test_the_handler_never_sleeps_inside_an_invocation():
    """Waiting inside the invocation is the defect, so no sleep may come back.

    An ast check rather than a behavioural one: the polling functions return
    immediately whatever the build status, so no input distinguishes a handler
    that also sleeps somewhere else in the module.
    """
    tree = ast.parse(_INDEX_PATH.read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "sleep" not in calls
