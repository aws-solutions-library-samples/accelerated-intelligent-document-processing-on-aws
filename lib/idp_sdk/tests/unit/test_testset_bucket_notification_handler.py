# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the TestSetBucketNotification custom-resource inline Lambda.

S3 permits only ONE conditional bucket-config operation against a bucket at a
time. A CloudFormation rollback tears down TestSetBucketPolicy,
TestSetBucketAutoDelete and this notification config concurrently, so
PutBucketNotificationConfiguration intermittently returns OperationAborted ("A
conflicting conditional operation is currently in progress").

On 2026-07-27 that happened during a rollback and, because the handler had no
retry and failed hard on Delete, it wedged the whole stack in ROLLBACK_FAILED —
a state needing manual intervention — even though the identical call succeeded
3 minutes later. Two properties are tested here:

  1. Transient conflicts are retried (with backoff) instead of failing.
  2. A Delete never fails the resource, so cleanup can't block a teardown.

The handler is embedded in template.yaml as InlineCode, so the source is
extracted from the template and exercised directly (same approach as
test_webui_oauth_urls_handler.py).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BUCKET = "idp-test-testsetbucket-abc123"
_FN_ARN = "arn:aws:lambda:us-east-1:1:function:TestSetZipExtractor"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


class _ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """Fake S3 client that fails a set number of times before succeeding."""

    def __init__(self, fail_codes=None):
        # fail_codes: list of error codes to raise on successive calls.
        self._fail_codes = list(fail_codes or [])
        self.calls = []

    def put_bucket_notification_configuration(self, Bucket, NotificationConfiguration):  # noqa: N803
        self.calls.append((Bucket, NotificationConfiguration))
        if self._fail_codes:
            raise _ClientError(self._fail_codes.pop(0))


def _load_handler(fake_s3):
    """Extract + exec the inline handler with fakes injected."""
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    template = _plain(cfnlint_decode.load(str(_repo_root() / "template.yaml")))
    code = template["Resources"]["TestSetBucketNotificationFunction"]["Properties"][
        "InlineCode"
    ]
    assert isinstance(code, str) and "def handler" in code

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, *a, **k: fake_s3  # noqa: ARG005

    sent = {}
    fake_cfnresponse = types.ModuleType("cfnresponse")
    fake_cfnresponse.SUCCESS = "SUCCESS"
    fake_cfnresponse.FAILED = "FAILED"

    def _send(event, context, status, data, *a, **k):  # noqa: ARG001
        sent["status"] = status
        sent["reason"] = k.get("reason", "")

    fake_cfnresponse.send = _send

    # The handler imports ClientError from botocore.exceptions; make our fake
    # exception the one it catches.
    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_exceptions.ClientError = _ClientError
    fake_botocore.exceptions = fake_exceptions

    saved = {
        k: sys.modules.get(k)
        for k in ("boto3", "cfnresponse", "botocore", "botocore.exceptions")
    }
    sys.modules["boto3"] = fake_boto3
    sys.modules["cfnresponse"] = fake_cfnresponse
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_exceptions
    try:
        mod = types.ModuleType("testset_notification_handler")
        exec(compile(code, "<TestSetBucketNotificationFunction>", "exec"), mod.__dict__)  # nosec B102 - executes this repo's own shipped handler source under test
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # Never actually sleep through the backoff ladder.
    mod.time = types.SimpleNamespace(sleep=lambda s: None)
    return mod, sent


def _event(request_type):
    return {
        "RequestType": request_type,
        "ResourceProperties": {"BucketName": _BUCKET, "FunctionArn": _FN_ARN},
        "StackId": "arn:aws:cloudformation:us-east-1:1:stack/s/1",
        "RequestId": "req-1",
        "LogicalResourceId": "TestSetBucketNotificationConfiguration",
        "ResponseURL": "https://cfn-response.example/x",
    }


class _Context:
    log_stream_name = "test-stream"


# --------------------------------------------------------------------------- #
# the fix: retry transient S3 bucket-config conflicts
# --------------------------------------------------------------------------- #


def test_operation_aborted_is_retried_until_it_succeeds():
    """The exact 2026-07-27 failure — must now recover instead of failing."""
    s3 = _FakeS3(fail_codes=["OperationAborted", "OperationAborted"])
    mod, sent = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    assert len(s3.calls) == 3  # two conflicts, then success
    assert sent["status"] == "SUCCESS"


def test_create_succeeds_first_try_without_retrying():
    s3 = _FakeS3()
    mod, sent = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    assert len(s3.calls) == 1
    assert sent["status"] == "SUCCESS"


@pytest.mark.parametrize(
    "code",
    [
        "OperationAborted",
        "SlowDown",
        "RequestTimeout",
        "InternalError",
        "ServiceUnavailable",
        "TooManyRequests",
    ],
)
def test_all_transient_codes_are_retried(code):
    s3 = _FakeS3(fail_codes=[code])
    mod, sent = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    assert len(s3.calls) == 2
    assert sent["status"] == "SUCCESS"


def test_non_transient_error_fails_fast_on_create():
    """AccessDenied is deterministic — retrying it would only waste time."""
    s3 = _FakeS3(fail_codes=["AccessDenied"])
    mod, sent = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    assert len(s3.calls) == 1
    assert sent["status"] == "FAILED"
    assert "AccessDenied" in sent["reason"]


def test_retry_ladder_is_bounded():
    """Persistent conflicts must eventually give up, not spin forever."""
    s3 = _FakeS3(fail_codes=["OperationAborted"] * 50)
    mod, sent = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    assert len(s3.calls) == len(mod.RETRY_DELAYS) + 1
    assert sent["status"] == "FAILED"


def test_retry_ladder_fits_in_the_lambda_timeout():
    """The backoff budget must not exceed the function's configured Timeout.

    Otherwise the Lambda is killed mid-ladder and never answers CloudFormation,
    which hangs the stack for an hour instead of failing fast.
    """
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")
    template = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    timeout = int(
        template["Resources"]["TestSetBucketNotificationFunction"]["Properties"][
            "Timeout"
        ]
    )
    mod, _ = _load_handler(_FakeS3())
    assert sum(mod.RETRY_DELAYS) < timeout, (
        f"retry ladder ({sum(mod.RETRY_DELAYS)}s) exceeds the Lambda "
        f"Timeout ({timeout}s)"
    )


# --------------------------------------------------------------------------- #
# the fix: a Delete must never block a stack teardown
# --------------------------------------------------------------------------- #


def test_delete_reports_success_even_when_s3_keeps_failing():
    """This is what turns ROLLBACK_FAILED back into a clean rollback.

    The bucket is DeletionPolicy: Retain, so a leftover notification config on a
    bucket that's going away is harmless — far better than wedging the stack.
    """
    s3 = _FakeS3(fail_codes=["OperationAborted"] * 50)
    mod, sent = _load_handler(s3)

    mod.handler(_event("Delete"), _Context())

    assert sent["status"] == "SUCCESS"
    assert "attempted" in sent["reason"].lower()


def test_delete_still_fails_over_to_success_on_hard_errors():
    s3 = _FakeS3(fail_codes=["AccessDenied"])
    mod, sent = _load_handler(s3)

    mod.handler(_event("Delete"), _Context())

    assert sent["status"] == "SUCCESS"


def test_delete_clears_the_notification_config():
    s3 = _FakeS3()
    mod, sent = _load_handler(s3)

    mod.handler(_event("Delete"), _Context())

    assert s3.calls == [(_BUCKET, {})]
    assert sent["status"] == "SUCCESS"


# --------------------------------------------------------------------------- #
# unchanged behavior: the config it writes
# --------------------------------------------------------------------------- #


def test_create_writes_the_expected_zip_suffix_notification():
    s3 = _FakeS3()
    mod, _ = _load_handler(s3)

    mod.handler(_event("Create"), _Context())

    bucket, config = s3.calls[0]
    assert bucket == _BUCKET
    lambda_configs = config["LambdaFunctionConfigurations"]
    assert lambda_configs[0]["LambdaFunctionArn"] == _FN_ARN
    assert lambda_configs[0]["Events"] == ["s3:ObjectCreated:*"]
    rules = lambda_configs[0]["Filter"]["Key"]["FilterRules"]
    assert rules == [{"Name": "suffix", "Value": ".zip"}]


def test_update_rewrites_the_config():
    s3 = _FakeS3()
    mod, sent = _load_handler(s3)

    mod.handler(_event("Update"), _Context())

    assert len(s3.calls) == 1
    assert "LambdaFunctionConfigurations" in s3.calls[0][1]
    assert sent["status"] == "SUCCESS"


# --------------------------------------------------------------------------- #
# the template-level half of the fix: serialize the bucket-config writes
# --------------------------------------------------------------------------- #


def test_notification_resource_depends_on_the_bucket_policy():
    """Ordering the two conditional writes removes the race at the source."""
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")
    template = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    depends = template["Resources"]["TestSetBucketNotificationConfiguration"][
        "DependsOn"
    ]
    assert "TestSetBucketPolicy" in [str(d) for d in depends]
