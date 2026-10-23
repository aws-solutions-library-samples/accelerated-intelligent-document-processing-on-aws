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
  ScanOnPush, and the wait holds while ECR reports a scan as queued or running.
  That wait used to be unbounded, so a slow scan polled until CloudFormation's
  one-hour custom-resource limit and the stack failed with "CloudFormation did
  not receive a response from your Custom Resource" -- a message naming the
  cfn-response path and not image scanning. It is now bounded by a budget derived
  from the build's own start time, and exhausting it deploys the image with a
  warning that says "ECR IMAGE SCANNING".

**Both scanning modes are covered, because they report different statuses.**
Basic scan-on-push goes ``IN_PROGRESS -> COMPLETE``; enhanced (Inspector)
scanning, a registry-level setting that overrides the repository's ScanOnPush,
goes ``PENDING -> ACTIVE``. A wait that knows only ``IN_PROGRESS`` orders nothing
in an enhanced-scanning account, so every waiting test is parametrised over both.

The wait reads ``imageScanStatus`` only. Nothing here calls
``describe_image_scan_findings``, so no severity gates the deploy; the tests
assert the log messages say so rather than implying a gate that does not exist.

One shape recurs in the terminal-error tests: crhelper truncates an over-long
failure Reason by keeping the **tail**, so the identifying phrase has to be at the
end of the message. ``_crhelper_truncate`` replicates that rule and
``_REALISTIC_ACCESS_DENIED`` is long enough to trigger it.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
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


def _client_error(code, message=None):
    return ClientError(
        {"Error": {"Code": code, "Message": message or f"simulated {code}"}},
        "DescribeImages",
    )


# The message AWS actually returns for a denied ECR call, with the ARNs that make
# it long. `str(ClientError)` wraps this in "An error occurred (Code) when calling
# the DescribeImages operation: ..." so the whole reason runs well past crhelper's
# 256-character limit, which is the point.
_REALISTIC_ACCESS_DENIED = (
    "User: arn:aws:sts::123456789012:assumed-role/"
    "idp-stack-CodeBuildExecutionRole-ABCDEFGHIJKL/idp-stack-CodeBuildTrigger-"
    "MNOPQRSTUVWX is not authorized to perform: ecr:DescribeImages on resource: "
    "arn:aws:ecr:us-east-1:123456789012:repository/idp-stack-ecrrepository-"
    "abcdefghijkl because no identity-based policy allows the "
    "ecr:DescribeImages action"
)


def _crhelper_truncate(reason: str) -> str:
    """crhelper's own truncation rule, from ``CfnResource._send``.

    ``resource_helper.py``::

        if len(str(str(self.Reason))) > 256:
            self.Reason = "ERROR: (truncated) " + str(self.Reason)[len(str(self.Reason)) - 240:]

    It keeps the **tail**, so an identifier at the front of a long reason is
    exactly what CloudFormation never sees. Replicated here rather than imported
    because ``crhelper`` is a Lambda-layer dependency and is stubbed in this
    suite; ``test_the_truncation_rule_matches_crhelper`` pins the shape against
    the quoted source above.
    """
    if len(reason) > 256:
        return "ERROR: (truncated) " + reason[len(reason) - 240 :]
    return reason


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


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


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
    exactly none of the hour for scan waiting -- which is the right answer rather
    than a coincidence to lean on.

    This assertion pins only the inequality, `budget <= 3300`. The budget itself
    is pinned exactly by the two tests above it, taken together: the reserve's
    composition plus `budget + reserve == 3600`.
    """
    codebuild_timeout_secs = 55 * 60
    assert index.SCAN_WAIT_BUDGET_SECONDS <= codebuild_timeout_secs
    remaining = index._scan_wait_remaining_seconds(
        datetime.now(timezone.utc) - timedelta(seconds=codebuild_timeout_secs)
    )
    assert remaining is not None and remaining <= 0


@pytest.fixture
def non_utc_timezone(monkeypatch):
    """Put the process in a non-UTC zone for the duration of a test.

    Without this, nothing can distinguish ``replace(tzinfo=utc)`` from
    ``astimezone(utc)`` on a naive datetime: both are identities when the local
    zone *is* UTC, which is what this host and the CI runners use, so a test
    written here passes against either and pins neither.
    """
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_naive_build_start_time_is_read_as_utc(index, non_utc_timezone):
    """botocore returns aware datetimes; a naive one must be read as UTC, not local.

    ``startTime`` is always aware in practice, so the coercion exists for a
    defensive case -- but reading it as *local* time would silently shift the
    deadline by the UTC offset, which under ``America/New_York`` hands the wait
    four or five extra hours it does not have.
    """
    naive_utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    remaining = index._scan_wait_remaining_seconds(naive_utc_now)
    assert remaining is not None
    assert remaining == pytest.approx(index.SCAN_WAIT_BUDGET_SECONDS, abs=30)


def test_the_non_utc_fixture_actually_changes_the_local_zone(non_utc_timezone):
    """Guard the guard: if TZ is not applied, the test above pins nothing again."""
    offset = datetime.now().astimezone().utcoffset()
    assert offset is not None and offset != timedelta(0)


def test_an_unknown_build_start_time_has_no_clock(index):
    assert index._scan_wait_remaining_seconds(None) is None


# --------------------------------------------------------------------------- #
# the bound itself
# --------------------------------------------------------------------------- #


def test_both_scanning_modes_queued_statuses_are_recognised(index):
    """Basic scanning reports IN_PROGRESS; enhanced (Inspector) reports PENDING.

    Enhanced scanning is a registry-level setting that overrides the repository's
    own ``ScanOnPush`` -- which is all ``EnableECRImageScanning`` sets -- and its
    sequence is PENDING -> ACTIVE, so ``IN_PROGRESS`` never appears. A wait that
    knows only ``IN_PROGRESS`` orders nothing in an enhanced-scanning account
    while the docstring claims it does.
    """
    assert set(index.SCAN_RUNNING_STATUSES) == {"IN_PROGRESS", "PENDING"}
    assert set(index.SCAN_FINISHED_STATUSES) == {"COMPLETE", "ACTIVE"}
    assert not set(index.SCAN_RUNNING_STATUSES) & set(index.SCAN_FINISHED_STATUSES)


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PENDING"])
def test_a_running_scan_keeps_polling_while_the_budget_remains(index, status):
    assert _verify(index, status, started_secs_ago=60) is False


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PENDING"])
def test_the_running_scan_wait_is_bounded(index, status):
    """Once the budget is spent the image deploys rather than polling the hour out."""
    spent = index.SCAN_WAIT_BUDGET_SECONDS + 60
    assert _verify(index, status, started_secs_ago=spent) is True


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PENDING"])
def test_the_exhausted_wait_names_image_scanning(index, status, caplog):
    """The whole cost of #1336 was a terminal message pointing somewhere else."""
    caplog.set_level(logging.DEBUG)
    _verify(index, status, started_secs_ago=index.SCAN_WAIT_BUDGET_SECONDS + 60)

    warned = _warnings(caplog)
    assert warned, "exhausting the wait must warn, not pass silently"
    assert any("ECR IMAGE SCANNING" in m and "exhausted" in m for m in warned)
    assert any(status in m for m in warned), "the holding status must be named"
    assert any("findings are not read" in m.lower() for m in warned)


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PENDING"])
def test_an_unknown_build_start_time_does_not_wait_unbounded(index, status, caplog):
    """With no origin for the budget the wait cannot be bounded, so it is not taken."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, status, started_secs_ago=None) is True
    assert any("ECR IMAGE SCANNING" in m for m in _warnings(caplog))


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PENDING"])
def test_the_remaining_budget_is_reported_while_waiting(index, status, caplog):
    caplog.set_level(logging.DEBUG)
    _verify(index, status, started_secs_ago=60)
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


@pytest.mark.parametrize(
    "status", ["FAILED", "UNSUPPORTED_IMAGE", "SCAN_ELIGIBILITY_EXPIRED"]
)
def test_a_scan_that_did_not_finish_still_deploys_but_warns(index, status, caplog):
    """Unchanged behaviour, honestly logged: these statuses were silent before."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, status, started_secs_ago=60) is True

    warned = _warnings(caplog)
    assert any(status in m for m in warned)
    assert any("does not gate" in m for m in warned)
    assert any("ECR IMAGE SCANNING" in m for m in warned)


def test_no_scan_status_is_not_warned_about(index, caplog):
    """EnableECRImageScanning defaults to false at the root, so this is the norm."""
    caplog.set_level(logging.DEBUG)
    assert _verify(index, None, started_secs_ago=60) is True
    assert not _warnings(caplog)


def test_an_absent_scan_status_is_not_claimed_to_mean_scanning_is_off(index, caplog):
    """F6: with ScanOnPush ON, a tag read just after the push can have no record.

    So the absence does not establish that scanning is disabled, and a line
    asserting it does states the opposite of the truth in exactly the window the
    wait exists for. The behaviour is deliberately unchanged -- waiting on an
    absent record would spend the whole budget on every deploy in the default
    configuration, where no record ever arrives -- but the wording has to say
    which it is.
    """
    caplog.set_level(logging.DEBUG)
    _verify(index, None, started_secs_ago=60)

    messages = [r.getMessage() for r in caplog.records]
    absent = [m for m in messages if "no scan status" in m]
    assert absent, f"no line reported the absent scan status: {messages}"

    line = absent[0]
    assert "ECR IMAGE SCANNING" in line
    assert "yet" in line, "the absence must be reported as provisional"
    assert "(image scanning is not enabled for this repository)" not in line
    assert "indistinguishable" in line


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


def test_the_truncation_rule_matches_crhelper(index):
    """Pin the shape of the rule this file's helper replicates.

    From ``crhelper.resource_helper.CfnResource._send``: over 256 characters, the
    Reason becomes ``"ERROR: (truncated) " + reason[len(reason) - 240:]``. Keeping
    the **tail** is the whole reason the identifier goes last.
    """
    short = "x" * 256
    assert _crhelper_truncate(short) == short

    long_reason = "HEAD" + "y" * 300 + "TAIL"
    truncated = _crhelper_truncate(long_reason)
    assert truncated.startswith("ERROR: (truncated) ")
    assert truncated.endswith("TAIL")
    assert "HEAD" not in truncated
    assert len(truncated) == len("ERROR: (truncated) ") + 240


def test_the_identifier_survives_crhelper_truncating_a_realistic_reason(index):
    """F1: a denied ECR call is long enough that a leading identifier is cut off.

    This is the case the docstring names first (permissions), and losing the
    identifier here reproduces exactly the defect this change exists to fix: a
    terminal message that does not say what failed.
    """
    index.ECR_CLIENT.describe_images.side_effect = _client_error(
        "AccessDeniedException", _REALISTIC_ACCESS_DENIED
    )

    with pytest.raises(index.EcrImageVerificationError) as excinfo:
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )

    reason = str(excinfo.value)
    assert len(reason) > 256, (
        "this case must actually exceed crhelper's limit, or the test is vacuous "
        f"-- measured {len(reason)} characters"
    )

    shown = _crhelper_truncate(reason)
    assert "ECR image verification" in shown, (
        f"the identifier was truncated away; CloudFormation would show only {shown!r}"
    )
    assert f"ocr-function-{_VERSION}" in shown


def test_a_named_error_is_logged_once_and_not_relabelled_unexpected(index, caplog):
    """F3: the EcrImageVerificationError passthrough in the broad handler.

    Without it the error falls to the catch-all, which logs it a second time as
    "unexpected fatal error" -- a wrong description of an error named correctly a
    moment earlier -- and re-wraps the message, pushing the identifier further
    from the tail crhelper keeps.
    """
    caplog.set_level(logging.DEBUG)
    index.ECR_CLIENT.describe_images.side_effect = _client_error(
        "AccessDeniedException", _REALISTIC_ACCESS_DENIED
    )

    with pytest.raises(index.EcrImageVerificationError) as excinfo:
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )

    errors = _errors(caplog)
    assert len(errors) == 1, f"the error was logged {len(errors)} times: {errors}"
    assert "ECR IMAGE VERIFICATION" in errors[0]
    assert "unexpected" not in errors[0]
    assert "unexpected" not in str(excinfo.value)


def test_an_unexpected_error_also_names_image_verification(index, caplog):
    caplog.set_level(logging.DEBUG)
    index.ECR_CLIENT.describe_images.side_effect = RuntimeError("socket closed")

    with pytest.raises(index.EcrImageVerificationError) as excinfo:
        index._verify_ecr_images_available(
            _ECR_URI, _VERSION, _IMAGES, datetime.now(timezone.utc)
        )

    assert "ECR image verification" in str(excinfo.value)

    errors = _errors(caplog)
    assert len(errors) == 1
    assert "ECR IMAGE VERIFICATION" in errors[0]


def test_no_expected_images_fails_instead_of_waiting_forever(index):
    """F7: there is no default image set, because a wrong one is a permanent poll.

    The list that used to be here named three ``hitl-*`` images no buildspec in
    this repository builds, and omitted every ``bda-*``, ``rule-validation-*`` and
    ``mlflow-logger-*`` image that one does.
    """
    index.ECR_CLIENT.describe_images.side_effect = AssertionError(
        "ECR must not be called with no image set to verify"
    )

    for empty in (None, []):
        with pytest.raises(index.EcrImageVerificationError) as excinfo:
            index._verify_ecr_images_available(
                _ECR_URI, _VERSION, empty, datetime.now(timezone.utc)
            )
        reason = str(excinfo.value)
        assert "ExpectedImages" in reason
        assert "ECR image verification" in reason
        assert _crhelper_truncate(reason) == reason, "short enough not to truncate"


def test_no_default_image_set_survives_in_the_module(index):
    """The deleted fallback list must not come back: no `hitl` tags anywhere."""
    source = _INDEX_PATH.read_text(encoding="utf-8")
    assert "hitl-wait-function" not in source
    assert "hitl-status-update-function" not in source
    assert "hitl-process-function" not in source


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


# --------------------------------------------------------------------------- #
# #1345: what the poller may write to CloudWatch
#
# The poller used to log the whole `batch_get_builds` reply at INFO, once every
# two minutes for the length of the build. It now logs BUILD_LOG_FIELDS of the
# one build it read.
#
# These tests are written against the **capability** rather than against the
# three items the issue happened to name, because the leak is in the shape of the
# argument and not in the message text: any field CodeBuild returns, including
# ones the service has not added yet, reaches the log if the whole reply is
# logged. So the member list comes from botocore's own model of the operation and
# every member outside the allowlist is probed, which is also what keeps the test
# honest if the allowlist grows -- adding a field to BUILD_LOG_FIELDS removes it
# from the probe set and that is visible in the diff.
#
# The group's log group is the auto-created one with no retention policy set
# (this is a custom-resource-only Lambda), so anything written here persists for
# the life of the account.
#
# The claim is bounded to the reply, and the bound is worth stating: this file
# also logs botocore error text in full on its failure paths -- the poller's broad
# handler and the fatal-ECR-error branch -- and a realistic AccessDeniedException
# carries an account id and an assumed-role ARN. That is deliberate and depended
# on elsewhere in this suite: crhelper carries the exception text to
# CloudFormation as the custom resource's failure Reason, which is the only place
# an operator learns why a stack operation failed. So "the poller may log only
# BUILD_LOG_FIELDS" is a statement about the success path and about the reply, not
# about everything this function can write.
# --------------------------------------------------------------------------- #


def _build_shape_members() -> dict:
    """Every member of botocore's ``Build`` shape, keyed by name.

    botocore's service model is the authority for what `batch_get_builds` can
    return; a hand-written list of fields would encode this suite's beliefs about
    CodeBuild rather than measuring them, and would not grow with the service.
    """
    import botocore.session

    operation = (
        botocore.session.get_session()
        .get_service_model("codebuild")
        .operation_model("BatchGetBuilds")
    )
    output_shape = operation.output_shape
    assert output_shape is not None
    return dict(output_shape.members["builds"].member.members)


def _value_carrying(shape, sentinel: str):
    """A value of ``shape``'s broad type with ``sentinel`` somewhere inside it.

    Scalars that are not strings get the sentinel string anyway: nothing on the
    IN_PROGRESS poll path coerces a build field other than ``buildStatus``, so a
    string in a timestamp or integer slot is never parsed, and the alternative --
    a real timestamp -- could not carry a sentinel to look for.
    """
    if shape.type_name == "list":
        return [sentinel]
    if shape.type_name in ("structure", "map"):
        return {"leaked": sentinel}
    return sentinel


_SENSITIVE_BUILD_MEMBERS = sorted(
    name
    for name in _build_shape_members()
    # BUILD_LOG_FIELDS is read from the module in the tests themselves; this
    # collection-time set is deliberately derived from the names in the source so
    # that the parametrisation cannot be narrowed by a change to the module
    # without the diff showing it.
    if name not in ("id", "buildStatus", "currentPhase", "startTime")
)


def _poller_log_text(caplog) -> str:
    """Every log record the poller emitted, as one string."""
    return "\n".join(r.getMessage() for r in caplog.records)


def test_the_build_shape_has_members_to_probe():
    """An empty derived list would collect as nothing and pin nothing.

    A `parametrize` over an empty sequence is reported as a *skip*, not a
    failure, so the guarantee below would disappear silently if botocore's model
    moved or the lookup broke.
    """
    assert len(_SENSITIVE_BUILD_MEMBERS) >= 25, _SENSITIVE_BUILD_MEMBERS
    # The three the issue named, so the probe set demonstrably covers them.
    for named in ("arn", "environment", "serviceRole"):
        assert named in _SENSITIVE_BUILD_MEMBERS


def test_the_log_allowlist_names_real_build_fields(index):
    """A typo in BUILD_LOG_FIELDS does not leak -- it silently logs nothing.

    `_build_log_summary` omits absent keys, so `buildstatus` for `buildStatus`
    would leave the poll line empty and take the diagnostic away rather than
    failing anywhere.
    """
    assert index.BUILD_LOG_FIELDS, "an empty allowlist logs nothing at all"
    members = _build_shape_members()
    for field in index.BUILD_LOG_FIELDS:
        assert field in members, f"{field} is not a member of the Build shape"
        # Scalars only. `_build_log_summary` copies the value whole and has no
        # depth limit, so allowlisting one of the sixteen structure or list
        # members -- `logs`, `environment`, `vpcConfig`, ... -- would log that
        # whole subtree, and every one of them has named fields worth not
        # logging (`logs.cloudWatchLogsArn`, `vpcConfig.vpcId`). The probe below
        # cannot see this: it plants its sentinel at a key production never
        # reads, so a member moved into the allowlist leaves its probe green.
        assert members[field].type_name not in ("structure", "list", "map"), (
            f"{field} is a {members[field].type_name}; logging it logs a subtree"
        )


def test_the_probe_set_and_the_allowlist_together_are_the_whole_build_shape(index):
    """Universe closure, which is what makes the probe below trustworthy.

    The probe set is derived from the field names spelled in this file rather
    than from the module, so that narrowing the probe takes a visible edit here.
    The cost of that is two lists which could drift apart: a member in neither
    would be probed by nothing and logged by nothing, and a member in both would
    be allowlisted while its probe asserts it is not logged. This closes it.
    """
    members = set(_build_shape_members())
    allowlisted = set(index.BUILD_LOG_FIELDS)
    probed = set(_SENSITIVE_BUILD_MEMBERS)

    assert not (allowlisted & probed), allowlisted & probed
    assert allowlisted | probed == members, members - (allowlisted | probed)


@pytest.mark.parametrize("member", _SENSITIVE_BUILD_MEMBERS)
def test_no_build_field_outside_the_allowlist_reaches_the_log(index, caplog, member):
    """Every other member of the reply, one at a time, must not be logged."""
    caplog.set_level(logging.DEBUG)
    sentinel = f"SENTINEL-{member}-0ff1ce"
    build = {
        "id": "proj:11111111-2222-3333-4444-555555555555",
        "buildStatus": "IN_PROGRESS",
        member: _value_carrying(_build_shape_members()[member], sentinel),
    }
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {"builds": [build]}

    # The in-progress poll is the one that runs repeatedly, and it reaches the
    # logging without touching ECR at all.
    assert index.poll_create_or_update(_poll_event(), None) is None

    logged = _poller_log_text(caplog)
    assert logged, "the poll logged nothing, so this test would pass vacuously"
    assert sentinel not in logged, f"{member} reached the log: {logged!r}"


def test_the_poll_still_reports_the_build_and_its_status(index, caplog):
    """The fix must not be "log nothing": a silent poll cannot be diagnosed.

    This is the other half of the probe above. Taken alone, that one is satisfied
    by deleting the log line.
    """
    caplog.set_level(logging.DEBUG)
    build_id = "proj:11111111-2222-3333-4444-555555555555"
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [
            {
                "id": build_id,
                "buildStatus": "IN_PROGRESS",
                "currentPhase": "BUILD",
                "arn": "arn:aws:codebuild:us-east-1:123456789012:build/" + build_id,
            }
        ]
    }

    assert index.poll_create_or_update(_poll_event(), None) is None

    logged = _poller_log_text(caplog)
    assert build_id in logged
    assert "IN_PROGRESS" in logged
    assert "BUILD" in logged


def test_a_build_environment_variable_is_not_logged_on_the_success_path(index, caplog):
    """The standing leak channel: anything a future trigger puts in the build env.

    `batch_get_builds` echoes `environmentVariables`, including any
    `environmentVariablesOverride` the trigger passed, so this is not a bounded
    disclosure of the ARNs -- it is whatever the build is given from now on. The
    SUCCEEDED branch is the one that reads those variables, so it is the branch
    that has to be checked separately from the probe above.
    """
    caplog.set_level(logging.DEBUG)
    # Named `planted_value` rather than `secret`: this file sits inside a Lambda
    # `CodeUri`, so `sam build` copies it into the deployed artifact, and bandit's
    # B105 matches an identifier's NAME against a password wordlist. A variable
    # called `secret` here is a HIGH finding on the security gate regardless of
    # what it holds.
    planted_value = "SENTINEL-env-var-value-0ff1ce"
    build = _succeeded_build(datetime.now(timezone.utc) - timedelta(seconds=60))
    build["environment"]["environmentVariables"].append(
        {"name": "SOME_FUTURE_SETTING", "value": planted_value}
    )
    build["arn"] = "arn:aws:codebuild:us-east-1:123456789012:build/x"
    build["serviceRole"] = (
        "arn:aws:iam::123456789012:role/idp-stack-CodeBuildExecutionRole"
    )
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {"builds": [build]}
    index.ECR_CLIENT.describe_images.side_effect = _describe_images_returning(
        "COMPLETE"
    )

    assert index.poll_create_or_update(_poll_event(), None) is True

    logged = _poller_log_text(caplog)
    assert logged
    assert planted_value not in logged
    # The account id travels in both ARNs and in the ECR registry host. The
    # repository name and the image tag are logged deliberately (#1342) and
    # neither carries one; `ecr_uri.split("/")[-1]` is what drops the host.
    assert "123456789012" not in logged
    assert "environmentVariables" not in logged


def test_the_not_found_branch_names_the_build_instead_of_dumping_the_reply(index):
    """Removing the dump must not remove the diagnostic from this branch.

    The whole reply was the only thing this path reported, and `buildsNotFound`
    echoes back the id the call passed in, so naming the id carries everything
    that line did.

    Both halves are asserted, because the message is raised and so reaches both
    this Lambda's log and CloudFormation's failure Reason. Asserting only that the
    id is present leaves the obvious regression green: a message that interpolates
    the reply contains the id as well, since `buildsNotFound` is where the id
    came from. So a value the reply carries and the message must not is planted
    too.
    """
    build_id = "proj:11111111-2222-3333-4444-555555555555"
    planted_value = "SENTINEL-not-found-reply-0ff1ce"
    index.CODEBUILD_CLIENT.batch_get_builds.return_value = {
        "builds": [],
        "buildsNotFound": [build_id],
        # Not a `Build`, so the probe above never reaches it: `batch_get_builds`
        # returns this alongside `builds` and it is part of what the dump wrote.
        "ResponseMetadata": {"RequestId": planted_value},
    }

    with pytest.raises(RuntimeError) as excinfo:
        index.poll_create_or_update(_poll_event(), None)

    message = str(excinfo.value)
    assert build_id in message
    assert planted_value not in message, f"the reply reached the message: {message!r}"
