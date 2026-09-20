# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What HTTP status a refusal *inside a resolver* reaches the caller with.

The dispatcher runs each resolver in a separate Lambda, so only two things survive
the invoke: the exception's class NAME (`errorType`) and its message. It chooses a
status from those — `PermissionError`/`AuthorizationError`, or a message beginning
"Unauthorized"/"Forbidden", becomes 403; `ValueError`/`KeyError` becomes 400;
anything else becomes 500 `InternalError`.

That makes the mapping fragile in a specific way worth pinning: a resolver that
catches its own exceptions and re-raises them wrapped destroys BOTH signals — the
class name becomes `Exception` and the "Unauthorized" token moves off the front of
the message. `get_file_contents_resolver` did exactly that, so the bucket
allow-list refusal that is the only thing preventing the resolver being a generic
S3-read gadget answered the denied caller with **500 InternalError**. Two costs:
an operator debugging a legitimate 403 chased a phantom server fault, and the
deployment's monitored 5xx rate counted deliberate policy denials — masking a real
spike in faults, and letting anyone probing bucket names inflate the fault signal
instead of the denial one.

`test_the_wrapped_refusal_shape_is_still_a_500` is the negative control. It is not
testing a bug; it pins the dispatcher rule that makes the wrapping fatal, so a
future resolver that reintroduces the wrap has a test saying why that is not
allowed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_REPO = _find_repo_root()
_DISPATCHER_DIR = (
    _REPO / "nested" / "api-resolvers" / "src" / "lambda" / ("http_api_dispatcher")
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def idx(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: object())
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    return _load_module("index", _DISPATCHER_DIR / "index.py")


class _RefusingLambda:
    """A resolver Lambda that returns a handled-error payload."""

    def __init__(self, error_type: str, message: str):
        self._payload = {"errorType": error_type, "errorMessage": message}

    def invoke(self, **kwargs):
        return {
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(json.dumps(self._payload).encode("utf-8")),
        }


ARN = "arn:aws:lambda:us-east-1:123456789012:function:x"


def _event(field):
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"cognito:groups": ["Admin"]}}},
        },
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": {"s3Uri": "s3://b/k"}}),
        "headers": {},
    }


def _respond(idx, monkeypatch, field, error_type, message):
    monkeypatch.setattr(idx, "_lambda", _RefusingLambda(error_type, message))
    target = idx.FIELD_ALIASES.get(field, field)
    idx.FIELD_FUNCTION_MAP[target] = ARN
    resp = idx.handler(_event(field))
    return resp["statusCode"], json.loads(resp["body"])["errors"][0]


class TestTheBucketAllowListRefusalIsA403:
    @pytest.mark.parametrize("field", ["getFileContents", "getFilePresignedUrl"])
    def test_a_permissionerror_becomes_403_unauthorized(self, idx, monkeypatch, field):
        """Both fields are served by the one resolver, so both must map."""
        status, error = _respond(
            idx,
            monkeypatch,
            field,
            "PermissionError",
            "Unauthorized: requested bucket is not accessible from this deployment.",
        )

        assert status == 403
        assert error["errorType"] == "Unauthorized", (
            "the UI's isAuthorizationError keys on this marker; without it the "
            "viewers show 'Please try again', which for a refusal cannot work"
        )

    def test_the_wrapped_refusal_shape_is_still_a_500(self, idx, monkeypatch):
        """Negative control: this is the shape the resolver must not produce.

        `errorType: "Exception"` misses the class-name arm, and the wrapper's
        "Error fetching file: " prefix moves "Unauthorized" off the front of the
        message so the prefix arm misses too — `str.startswith` is anchored.
        """
        status, error = _respond(
            idx,
            monkeypatch,
            "getFileContents",
            "Exception",
            "Error fetching file: Unauthorized: requested bucket is not accessible "
            "from this deployment.",
        )

        assert status == 500
        assert error["errorType"] == "InternalError"


class TestValidationRefusalsAre400:
    @pytest.mark.parametrize(
        "message",
        [
            "Invalid S3 URI: expected s3://<bucket>/<key>",
            "Invalid S3 URI: key is required",
            "File not found",
            "Date range too large: 3650 days requested, maximum is 365 days.",
        ],
    )
    def test_a_valueerror_becomes_400_badrequest(self, idx, monkeypatch, message):
        status, error = _respond(
            idx, monkeypatch, "getFileContents", "ValueError", message
        )

        assert status == 400
        assert error["errorType"] == "BadRequest"
        assert error["message"] == message, (
            "the message is the only thing telling the caller what to change"
        )


class TestAGenuineFaultIsStillA500:
    def test_a_bare_exception_is_an_internal_error(self, idx, monkeypatch):
        """Unchanged, and deliberately so: a failure of this deployment's own IAM,
        bucket policy or KMS grant is a server fault and belongs in the 5xx rate."""
        status, error = _respond(
            idx,
            monkeypatch,
            "getFileContents",
            "Exception",
            "This deployment could not read the requested file. "
            "Contact an administrator.",
        )

        assert status == 500
        assert error["errorType"] == "InternalError"

    def test_the_fault_message_carries_no_raw_s3_detail(self, idx, monkeypatch):
        """S3's own text distinguished NoSuchBucket from AccessDenied from
        Forbidden, which made the 500 an existence oracle over the allow-listed
        buckets, and reads to the UI as the caller's authorization problem."""
        _, error = _respond(
            idx,
            monkeypatch,
            "getFileContents",
            "Exception",
            "This deployment could not read the requested file. "
            "Contact an administrator.",
        )

        assert "Access Denied" not in error["message"]
        assert "NoSuchBucket" not in error["message"]
