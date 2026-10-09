# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the ECR image verification the DockerBuildRun custom resource runs.

The subject is the wait in ``_verify_ecr_images_available``. It has two jobs that
have to stay apart:

* **Presence** -- every expected tag must exist before the Lambda functions that
  pull it are created. An absent tag keeps polling, which is deliberate and is the
  behaviour issue #1310 arrived through; it is pinned here so the bound added for
  #1336 cannot quietly change it.
* **Scan ordering** -- with ``EnableECRImageScanning=true`` the repository has
  ScanOnPush, and the wait holds while ECR reports a scan ``IN_PROGRESS``. That
  wait used to be unbounded, so a slow scan polled until CloudFormation's
  one-hour custom-resource limit and the stack failed with "CloudFormation did
  not receive a response from your Custom Resource" -- a message naming the
  cfn-response path and not image scanning. It is now bounded by a budget derived
  from the build's own start time, and exhausting it deploys the image with a
  warning that says "ECR IMAGE SCANNING".

The wait reads ``imageScanStatus`` only. Nothing here calls
``describe_image_scan_findings``, so no severity gates the deploy; the tests
assert the log messages say so rather than implying a gate that does not exist.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = Path(__file__).resolve().parent / "index.py"
_MODULE_NAME = "start_codebuild_index_under_test"


class _FakeCfnResource:
    """Stand-in for ``crhelper.CfnResource``.

    ``crhelper`` is a Lambda-layer dependency (``requirements.txt`` pins
    ``crhelper~=2.0.10``) and is not a test dependency of this repository, so the
    module under test cannot be imported without it. The decorators it supplies
    only register handlers; returning the function unchanged is enough for the
    suite to call ``poll_create_or_update`` directly.
    """

    def __init__(self, *_args, **_kwargs):
        self.Data: dict = {}
        self.init_failures: list = []

    def _passthrough(self, func):
        return func

    create = update = delete = _passthrough
    poll_create = poll_update = _passthrough

    def init_failure(self, exception):  # pragma: no cover - never hit in tests
        self.init_failures.append(exception)

    def __call__(self, event, context):  # pragma: no cover - handler not exercised
        raise AssertionError("the suite calls the handlers directly")


@pytest.fixture(scope="module")
def index():
    """Import ``index.py`` with ``crhelper`` stubbed and no real AWS clients."""
    fake_crhelper = SimpleNamespace(CfnResource=_FakeCfnResource)
    env = {"AWS_DEFAULT_REGION": "us-east-1", "LOG_LEVEL": "INFO"}

    with (
        patch.dict(sys.modules, {"crhelper": fake_crhelper}),
        patch.dict(os.environ, env, clear=False),
        patch("boto3.client") as mock_client,
    ):
        mock_client.side_effect = lambda name, **_kw: MagicMock(name=f"client-{name}")

        sys.path.insert(0, str(_INDEX_PATH.parent))
        try:
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[_MODULE_NAME] = module
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(_INDEX_PATH.parent))

    yield module

    sys.modules.pop(_MODULE_NAME, None)


def test_the_module_under_test_is_this_worktree_copy(index):
    """Guard against importing a same-named module from another checkout."""
    assert Path(index.__file__) == _INDEX_PATH


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_ECR_URI = "123456789012.dkr.ecr.us-east-1.amazonaws.com/idp-repo"
_VERSION = "0.0.1-test"
_IMAGES = ["ocr-function"]


def _describe_images_returning(scan_status, found=True):
    """A ``describe_images`` stub reporting one image with ``scan_status``."""

    def _describe_images(repositoryName, imageIds):  # noqa: N803 - boto3 casing
        if not found:
            return {"imageDetails": []}
        detail: dict = {"imageTags": [imageIds[0]["imageTag"]]}
        if scan_status is not None:
            detail["imageScanStatus"] = {"status": scan_status}
        return {"imageDetails": [detail]}

    return _describe_images


def _client_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": f"simulated {code}"}}, "DescribeImages"
    )


def _verify(index, scan_status, started_secs_ago, found=True):
    """Run the verification with a build that started ``started_secs_ago`` ago."""
    started = (
        None
        if started_secs_ago is None
        else datetime.now(timezone.utc) - timedelta(seconds=started_secs_ago)
    )
    index.ECR_CLIENT.describe_images.side_effect = _describe_images_returning(
        scan_status, found=found
    )
    return index._verify_ecr_images_available(_ECR_URI, _VERSION, _IMAGES, started)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# --------------------------------------------------------------------------- #
# the budget arithmetic
# --------------------------------------------------------------------------- #


def test_the_wait_budget_plus_its_reserve_is_exactly_the_cloudformation_limit(index):
    """The cap is derived from the hour, not picked as a round number."""
    assert index.CUSTOM_RESOURCE_BUDGET_SECONDS == 3600
    assert (
        index.SCAN_WAIT_BUDGET_SECONDS + index.SCAN_WAIT_RESERVE_SECONDS
        == index.CUSTOM_RESOURCE_BUDGET_SECONDS
    )


def test_the_reserve_covers_two_poll_cycles_and_one_lambda_timeout(index):
    """Enough left to notice the deadline, answer, and have the answer land."""
    assert index.POLL_INTERVAL_SECONDS == 120
    assert index.LAMBDA_TIMEOUT_SECONDS == 60
    assert index.SCAN_WAIT_RESERVE_SECONDS == (
        2 * index.POLL_INTERVAL_SECONDS + index.LAMBDA_TIMEOUT_SECONDS
    )
    assert index.SCAN_WAIT_BUDGET_SECONDS > 0


def test_a_build_that_ran_to_the_codebuild_timeout_leaves_no_scan_wait(index):
    """A slow build shortens the scan wait instead of overrunning the hour.

    ``DockerBuildProject`` carries ``TimeoutInMinutes: 55``, and the budget works
    out at the same 3300s, so a build that ran to its own CodeBuild limit leaves
    exactly none of the hour for scan waiting -- which is the right answer, not a
    coincidence to lean on: the two numbers are independent and the assertion is
    here so that moving either one is visible.
    """
    codebuild_timeout_secs = 55 * 60
    assert index.SCAN_WAIT_BUDGET_SECONDS <= codebuild_timeout_secs
    remaining = index._scan_wait_remaining_seconds(
        datetime.now(timezone.utc) - timedelta(seconds=codebuild_timeout_secs)
    )
    assert remaining is not None and remaining <= 0


def test_a_naive_build_start_time_is_read_as_utc(index):
    """botocore returns aware datetimes; a naive one must not raise."""
    naive_utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    remaining = index._scan_wait_remaining_seconds(naive_utc_now)
    assert remaining is not None
    assert remaining == pytest.approx(index.SCAN_WAIT_BUDGET_SECONDS, abs=30)


def test_an_unknown_build_start_time_has_no_clock(index):
    assert index._scan_wait_remaining_seconds(None) is None


# --------------------------------------------------------------------------- #
# the bound itself
# --------------------------------------------------------------------------- #


def test_a_running_scan_keeps_polling_while_the_budget_remains(index):
    assert _verify(index, "IN_PROGRESS", started_secs_ago=60) is False


def test_the_running_scan_wait_is_bounded(index):
    """Once the budget is spent the image deploys rather than polling the hour out."""
    spent = index.SCAN_WAIT_BUDGET_SECONDS + 60
    assert _verify(index, "IN_PROGRESS", started_secs_ago=spent) is True


def test_the_exhausted_wait_names_image_scanning(index, caplog):
    """The whole cost of #1336 was a terminal message pointing somewhere else."""
    caplog.set_level(logging.DEBUG)
    _verify(index, "IN_PROGRESS", started_secs_ago=index.SCAN_WAIT_BUDGET_SECONDS + 60)

    warned = _warnings(caplog)
    assert warned, "exhausting the wait must warn, not pass silently"
    assert any("ECR IMAGE SCANNING" in m and "exhausted" in m for m in warned)
    assert any("findings are not read" in m.lower() for m in warned)


def test_an_unknown_build_start_time_does_not_wait_unbounded(index, caplog):
    """With no origin for the budget the wait cannot be bounded, so it is not taken."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, "IN_PROGRESS", started_secs_ago=None) is True
    assert any("ECR IMAGE SCANNING" in m for m in _warnings(caplog))


def test_the_remaining_budget_is_reported_while_waiting(index, caplog):
    caplog.set_level(logging.DEBUG)
    _verify(index, "IN_PROGRESS", started_secs_ago=60)
    messages = [r.getMessage() for r in caplog.records]
    assert any("ECR IMAGE SCANNING" in m and "still in progress" in m for m in messages)


# --------------------------------------------------------------------------- #
# what still deploys, and what the log says about it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["COMPLETE", "ACTIVE"])
def test_a_scan_that_finishes_within_the_cap_deploys(index, status, caplog):
    caplog.set_level(logging.DEBUG)
    assert _verify(index, status, started_secs_ago=60) is True
    assert not _warnings(caplog)


def test_a_finished_scan_is_not_described_as_a_clean_result(index, caplog):
    """No severity is read, so the log must not imply the scan passed."""
    caplog.set_level(logging.DEBUG)
    _verify(index, "COMPLETE", started_secs_ago=60)
    messages = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "findings are not read" in messages


@pytest.mark.parametrize("status", ["FAILED", "UNSUPPORTED_IMAGE", "PENDING"])
def test_a_scan_that_did_not_finish_still_deploys_but_warns(index, status, caplog):
    """Unchanged behaviour, honestly logged: these statuses were silent before."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, status, started_secs_ago=60) is True

    warned = _warnings(caplog)
    assert any(status in m for m in warned)
    assert any("does not gate" in m for m in warned)


def test_no_scan_status_is_not_warned_about(index, caplog):
    """EnableECRImageScanning defaults to false at the root, so this is the norm."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, None, started_secs_ago=60) is True
    assert not _warnings(caplog)


# --------------------------------------------------------------------------- #
# #1310: the missing-image behaviour must be unchanged
# --------------------------------------------------------------------------- #


def test_an_absent_image_keeps_polling(index):
    assert _verify(index, None, started_secs_ago=60, found=False) is False


def test_an_absent_image_keeps_polling_even_once_the_scan_budget_is_spent(index):
    """The bound added here is on the scan wait only, not on presence."""
    spent = index.SCAN_WAIT_BUDGET_SECONDS + 600
    assert _verify(index, None, started_secs_ago=spent, found=False) is False


def test_image_not_found_exception_keeps_polling(index):
    index.ECR_CLIENT.describe_images.side_effect = _client_error(
        "ImageNotFoundException"
    )
    assert (
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )
        is False
    )


# --------------------------------------------------------------------------- #
# giving up must name this path
# --------------------------------------------------------------------------- #


def test_a_fatal_ecr_error_fails_with_a_reason_naming_image_verification(index):
    """crhelper carries the exception text to CloudFormation as the failure reason."""
    index.ECR_CLIENT.describe_images.side_effect = _client_error(
        "AccessDeniedException"
    )

    with pytest.raises(index.EcrImageVerificationError) as excinfo:
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )

    reason = str(excinfo.value)
    assert "ECR image verification" in reason
    assert "AccessDeniedException" in reason
    assert f"ocr-function-{_VERSION}" in reason


def test_an_unexpected_error_also_names_image_verification(index):
    index.ECR_CLIENT.describe_images.side_effect = RuntimeError("socket closed")

    with pytest.raises(index.EcrImageVerificationError) as excinfo:
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )

    assert "ECR image verification" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# the plumbing: the poller has to hand the origin over, or the bound is dead code
# --------------------------------------------------------------------------- #


def _poll_event():
    return {
        "ResourceType": "Custom::CodeBuildRun",
        "CrHelperData": {"build_id": "proj:11111111-2222-3333-4444-555555555555"},
        "ResourceProperties": {"ExpectedImages": _IMAGES},
    }


def _succeeded_build(start_time):
    build = {
        "id": "proj:11111111-2222-3333-4444-555555555555",
        "buildStatus": "SUCCEEDED",
        "environment": {
            "environmentVariables": [
                {"name": "ECR_URI", "value": _ECR_URI},
                {"name": "IMAGE_VERSION", "value": _VERSION},
            ]
        },
    }
    if start_time is not None:
        build["startTime"] = start_time
    return build


def test_the_poller_hands_the_build_start_time_to_the_verification(index, monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(seconds=300)
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [_succeeded_build(started)]
    }

    seen = {}

    def _spy(ecr_uri, image_version, expected_images=None, build_start_time=None):
        seen["build_start_time"] = build_start_time
        return True

    monkeypatch.setattr(index, "_verify_ecr_images_available", _spy)

    assert index.poll_create_or_update(_poll_event(), None) is True
    assert seen["build_start_time"] == started


def test_the_poller_tolerates_a_build_with_no_start_time(index, monkeypatch):
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [_succeeded_build(None)]
    }

    seen = {}

    def _spy(ecr_uri, image_version, expected_images=None, build_start_time=None):
        seen["build_start_time"] = build_start_time
        return True

    monkeypatch.setattr(index, "_verify_ecr_images_available", _spy)

    assert index.poll_create_or_update(_poll_event(), None) is True
    assert seen["build_start_time"] is None


def test_a_running_scan_makes_the_poller_poll_again(index):
    """End to end through the poller: IN_PROGRESS inside the budget returns None."""
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [_succeeded_build(datetime.now(timezone.utc) - timedelta(seconds=60))]
    }
    index.ECR_CLIENT.describe_images.side_effect = _describe_images_returning(
        "IN_PROGRESS"
    )

    assert index.poll_create_or_update(_poll_event(), None) is None


def test_an_exhausted_scan_wait_lets_the_poller_succeed(index):
    """The same end-to-end path, past the budget, must not keep returning None."""
    spent = index.SCAN_WAIT_BUDGET_SECONDS + 60
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [
            _succeeded_build(datetime.now(timezone.utc) - timedelta(seconds=spent))
        ]
    }
    index.ECR_CLIENT.describe_images.side_effect = _describe_images_returning(
        "IN_PROGRESS"
    )

    assert index.poll_create_or_update(_poll_event(), None) is True
