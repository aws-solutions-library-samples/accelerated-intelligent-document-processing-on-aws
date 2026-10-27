# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the caller is told when an operation fails unexpectedly.

The dispatcher maps a resolver's exception to a status by its class name, which is
all that crosses ``lambda:Invoke`` along with the message. Four classes are a
resolver's way of saying something to the caller on purpose, and their messages are
the response: ``PermissionError`` (and ``ResourceAccessDenied``) for 403,
``ValueError`` for 400 — which is how ``NOT_CLAIMED``, ``QUEUE_MANAGED``,
``REVIEW_FINISHED`` and "already claimed by" reach the review screens —
``ResourceNotFound`` for 404, and ``ReportedServerError`` for a 500 whose text the
resolver composed for the caller (``deleteDocument``'s per-document report).

Anything else is unexpected, and its text was not written for a reader: an AWS
authorization failure names the role and the resource ARNs. That reaches the caller
as a generic message carrying a request id, with ``errorType`` still
``InternalError`` (the web UI's ``isAuthorizationError`` keys on the type), and the
full text is logged under that id. The request id is the dispatcher's Lambda request
id, which prefixes every line that invocation logs.

The resolver's failure is reproduced as the Lambda service reports it: an invoke
response with ``FunctionError`` set and the ``errorType`` / ``errorMessage``
payload the Python runtime writes for an unhandled exception.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"
ROLE_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Stack-ResolverRole-Ab12/Stack-fn"
TABLE_ARN = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/Stack-TrackingTable-Xy"
ACCESS_DENIED = (
    "An error occurred (AccessDeniedException) when calling the GetItem operation: "
    f"User: {ROLE_ARN} is not authorized to perform: dynamodb:GetItem on resource: "
    f"{TABLE_ARN} because no identity-based policy allows the dynamodb:GetItem action"
)
REQUEST_ID = "4f7e1c2a-req-dispatcher"
CONTEXT = SimpleNamespace(aws_request_id=REQUEST_ID)
FUNCTION_ARN = "arn:aws:lambda:us-east-1:000000000000:function:resolver"


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_DISPATCHER_DIR = (
    _find_repo_root() / "nested" / "api-resolvers" / "src" / "lambda"
) / "http_api_dispatcher"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def idx(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: object())
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    return _load_module("index", _DISPATCHER_DIR / "index.py")


class _FailingResolver:
    """The invoke response for a resolver that raised ``error_type(message)``."""

    def __init__(self, error_type: str, message: str):
        self._payload = {"errorType": error_type, "errorMessage": message}

    def invoke(self, **_kwargs):
        return {
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(json.dumps(self._payload).encode("utf-8")),
            "ResponseMetadata": {"RequestId": "resolver-invocation-77"},
        }


class _DeniedInvoke:
    """``lambda:InvokeFunction`` itself refused: a non-retryable ClientError."""

    def invoke(self, **_kwargs):
        raise ClientError(
            {
                "Error": {"Code": "AccessDeniedException", "Message": ACCESS_DENIED},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "Invoke",
        )


def _event(field="claimReview", request_id="gateway-req-9"):
    return {
        "requestContext": {
            "requestId": request_id,
            "http": {"method": "POST"},
            "authorizer": {
                "jwt": {"claims": {"cognito:groups": ["Admin"], "email": "a@b.c"}}
            },
        },
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": {"objectKey": "doc.pdf"}}),
        "headers": {},
    }


def _call(
    idx, monkeypatch, client, *, field="claimReview", context=CONTEXT, event=None
):
    monkeypatch.setattr(idx, "_lambda", client)
    idx.FIELD_FUNCTION_MAP[idx.FIELD_ALIASES.get(field, field)] = FUNCTION_ARN
    response = idx.handler(event or _event(field), context)
    return response["statusCode"], json.loads(response["body"])


def _assert_generic(status, body, request_id=REQUEST_ID):
    assert status == 500
    (error,) = body["errors"]
    assert error["errorType"] == "InternalError", "the web UI keys on the type"
    assert error["message"] == (
        f"An unexpected server error occurred. Request ID: {request_id}"
    )
    assert error["requestId"] == request_id
    rendered = json.dumps(body)
    for internal in (ACCOUNT, "arn:aws:", "AccessDenied", "Stack-", "dynamodb:"):
        assert internal not in rendered, f"{internal!r} reached the caller: {rendered}"


# --------------------------------------------------------------------------
# Unexpected: a generic message and a request id, the detail in the log
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_type",
    ["ClientError", "AccessDeniedException", "Exception", "RuntimeError", "", "Boom"],
)
def test_an_unexpected_resolver_error_reaches_the_caller_without_its_text(
    idx, monkeypatch, caplog, error_type
):
    with caplog.at_level(logging.ERROR):
        status, body = _call(
            idx, monkeypatch, _FailingResolver(error_type, ACCESS_DENIED)
        )
    _assert_generic(status, body)
    # The operator still has everything: the text, under the id the caller holds,
    # and the resolver's own invocation id to find its log lines.
    assert ACCESS_DENIED in caplog.text
    assert REQUEST_ID in caplog.text
    assert "resolver-invocation-77" in caplog.text


def test_a_failure_of_the_invoke_itself_reaches_the_caller_without_its_text(
    idx, monkeypatch, caplog
):
    """The dispatcher's own AWS call failing: its message names the dispatcher's
    role and the resolver's function ARN."""
    with caplog.at_level(logging.ERROR):
        status, body = _call(idx, monkeypatch, _DeniedInvoke())
    _assert_generic(status, body)
    assert ROLE_ARN in caplog.text and REQUEST_ID in caplog.text


def test_a_failure_in_a_field_served_without_a_resolver_is_generic_too(
    idx, monkeypatch, caplog
):
    """A field ``ddb_direct`` serves fails inside the dispatcher, not a resolver."""
    field = next(iter(sorted(idx.ddb_direct._HANDLED)))
    idx.FIELD_FUNCTION_MAP.pop(idx.FIELD_ALIASES.get(field, field), None)

    def fail(*_args, **_kwargs):
        raise RuntimeError(ACCESS_DENIED)

    monkeypatch.setattr(idx.ddb_direct, "dispatch", fail)
    monkeypatch.setattr(idx.authz, "enforce", lambda *_a, **_k: None)
    monkeypatch.setattr(idx, "validate_arguments", lambda *_a, **_k: None)
    with caplog.at_level(logging.ERROR):
        response = idx.handler(_event(field), CONTEXT)
    _assert_generic(response["statusCode"], json.loads(response["body"]))
    assert ACCESS_DENIED in caplog.text


def test_without_a_lambda_context_the_gateway_request_id_is_the_reference(
    idx, monkeypatch, caplog
):
    with caplog.at_level(logging.ERROR):
        status, body = _call(
            idx,
            monkeypatch,
            _FailingResolver("Exception", ACCESS_DENIED),
            context=None,
            event=_event(request_id="gateway-req-9"),
        )
    _assert_generic(status, body, request_id="gateway-req-9")
    assert "gateway-req-9" in caplog.text


def test_with_no_id_anywhere_a_fresh_reference_is_logged_and_returned(
    idx, monkeypatch, caplog
):
    event = _event()
    del event["requestContext"]["requestId"]
    with caplog.at_level(logging.ERROR):
        status, body = _call(
            idx,
            monkeypatch,
            _FailingResolver("Exception", ACCESS_DENIED),
            context=None,
            event=event,
        )
    request_id = body["errors"][0]["requestId"]
    assert re.fullmatch(r"[0-9a-f-]{36}", request_id), request_id
    _assert_generic(status, body, request_id=request_id)
    assert request_id in caplog.text


# --------------------------------------------------------------------------
# Deliberate: the message a resolver raised for the caller arrives unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "message", "status", "wire_type"),
    [
        (
            "ValueError",
            "NOT_CLAIMED: nobody has claimed this review, so there is nothing "
            "to release",
            400,
            "BadRequest",
        ),
        (
            "ValueError",
            "QUEUE_MANAGED: this document is in a review queue; work it from "
            "Review Queues",
            400,
            "BadRequest",
        ),
        (
            "ValueError",
            "REVIEW_FINISHED: this review is already finished, so there is nothing "
            "to start",
            400,
            "BadRequest",
        ),
        ("ValueError", "Document is already claimed by bob", 400, "BadRequest"),
        ("KeyError", "objectKey", 400, "BadRequest"),
        (
            "PermissionError",
            "Only the review owner or an admin can release this review",
            403,
            "Unauthorized",
        ),
        (
            "ResourceAccessDenied",
            "Unauthorized: this chat session belongs to another user",
            403,
            "Unauthorized",
        ),
        ("ResourceNotFound", "Test set ts-1 not found", 404, "ResourceNotFound"),
        (
            "ReportedServerError",
            "1 of 1 document(s) could not be completely deleted (0 completed) — "
            "a.pdf: AccessDenied on s3:ListBucketVersions",
            500,
            "InternalError",
        ),
    ],
)
def test_a_deliberate_message_reaches_the_caller_unchanged(
    idx, monkeypatch, error_type, message, status, wire_type
):
    got_status, body = _call(idx, monkeypatch, _FailingResolver(error_type, message))
    (error,) = body["errors"]
    assert got_status == status
    assert error["errorType"] == wire_type
    assert error["message"] == message


def test_a_reported_server_error_carries_the_request_id_as_well(idx, monkeypatch):
    _, body = _call(
        idx, monkeypatch, _FailingResolver("ReportedServerError", "a.pdf: failed")
    )
    assert body["errors"][0]["requestId"] == REQUEST_ID


# --------------------------------------------------------------------------
# The same rule in the dual-transport decorator
# --------------------------------------------------------------------------


def _http_event():
    return {
        "requestContext": {
            "requestId": "gateway-req-3",
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"cognito:groups": ["Admin"]}}},
        },
        "pathParameters": {"field": "x"},
        "body": json.dumps({"arguments": {}}),
        "headers": {},
    }


def test_the_decorator_answers_an_unexpected_error_generically(caplog):
    from idp_common.api_adapter import api_resolver

    @api_resolver
    def handler(event, context):
        raise RuntimeError(ACCESS_DENIED)

    with caplog.at_level(logging.ERROR):
        result = handler(_http_event(), CONTEXT)
    _assert_generic(result["statusCode"], json.loads(result["body"]))
    assert ACCESS_DENIED in caplog.text and REQUEST_ID in caplog.text


def test_the_decorator_passes_a_reported_server_error_through():
    from idp_common.api_adapter import ReportedServerError, api_resolver

    @api_resolver
    def handler(event, context):
        raise ReportedServerError("a.pdf: could not be completely deleted")

    result = handler(_http_event(), CONTEXT)
    (error,) = json.loads(result["body"])["errors"]
    assert result["statusCode"] == 500
    assert error["errorType"] == "InternalError"
    assert error["message"] == "a.pdf: could not be completely deleted"


def test_a_reported_server_error_is_still_caught_as_a_runtime_error():
    """``deleteDocument`` raised ``RuntimeError`` before it raised this class; a
    caller catching that keeps catching it."""
    from idp_common.api_adapter import ReportedServerError

    assert issubclass(ReportedServerError, RuntimeError)
