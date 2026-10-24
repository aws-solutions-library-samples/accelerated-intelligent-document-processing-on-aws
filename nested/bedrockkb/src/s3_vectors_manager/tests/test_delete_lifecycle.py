# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deleting the S3 Vectors bucket and index, driven through ``lambda_handler``.

S3 Vectors refuses to delete a vector bucket that still holds an index. The Delete
branch used to call ``delete_vector_bucket`` alone, log the refusal as expected and
report SUCCESS, so every stack delete left the bucket and its index behind
(issue #1589). These tests pin the replacement behaviour: the index is deleted
first, a bucket or index that is already gone counts as deleted, and the handler
reports FAILED rather than SUCCESS while the bucket still exists.

The client is a real ``s3vectors`` client under ``botocore.stub.Stubber``, so every
request is validated against the installed service model and the error codes are
ones that model declares. Each stub is consumed exactly once and
``assert_no_pending_responses`` checks that the handler made every call queued for
it, in order; nothing here loops on an open-ended mock.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.session
import handler
import pytest
from botocore.stub import Stubber

REGION = "us-east-1"
BUCKET = "idp-documentkb-abc123-s3-vectors"
INDEX = "bedrock-kb-index"
PHYSICAL_ID = f"s3vectors://{BUCKET}/{INDEX}"
MODEL = botocore.session.get_session().get_service_model("s3vectors")


def _errors(operation: str) -> set[str]:
    return {shape.name for shape in MODEL.operation_model(operation).error_shapes}


@pytest.fixture
def s3vectors(monkeypatch):
    """A stubbed real ``s3vectors`` client that ``handler`` will receive."""
    client = botocore.session.get_session().create_client(
        "s3vectors", region_name=REGION
    )
    # Every s3vectors operation the handler attempts, in order, including one the
    # Stubber rejects. Recorded on provide-client-params, the first event of a call:
    # Stubber hooks before-parameter-build and before-call and raises or answers
    # there, so a recorder on either never sees an unstubbed call (measured).
    calls: list[str] = []
    client.meta.events.register(
        "provide-client-params.s3vectors",
        lambda model, **_kwargs: calls.append(model.name),
    )
    stubber = Stubber(client)
    sts = botocore.session.get_session().create_client("sts", region_name=REGION)
    sts_stubber = Stubber(sts)
    clients = {"s3vectors": client, "sts": sts}
    monkeypatch.setattr(handler.boto3, "client", lambda name, **_kwargs: clients[name])
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    send = MagicMock()
    monkeypatch.setattr(handler.cfnresponse, "send", send)
    with stubber, sts_stubber:
        yield SimpleNamespace(
            stubber=stubber, sts=sts_stubber, send=send, sleeps=sleeps, calls=calls
        )
        stubber.assert_no_pending_responses()
        sts_stubber.assert_no_pending_responses()


def _event(request_type: str, **overrides) -> dict:
    properties = {
        "ResourceType": "S3VectorBucketAndIndex",
        "BucketName": BUCKET,
        "IndexName": INDEX,
        "Region": REGION,
    }
    properties.update(overrides.pop("properties", {}))
    event = {
        "RequestType": request_type,
        "ResourceProperties": properties,
        "PhysicalResourceId": PHYSICAL_ID,
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/x",
        "RequestId": "req-00000000",
        "LogicalResourceId": "S3VectorBucketAndIndex",
        "ResponseURL": "https://example.invalid/response",
    }
    event.update(overrides)
    return event


def _status(send: MagicMock) -> str:
    assert send.call_count == 1, send.call_args_list
    return send.call_args.args[2]


def _reason(send: MagicMock) -> str:
    return send.call_args.kwargs.get("reason", "")


def _delete_index(stubber, **error):
    params = {"vectorBucketName": BUCKET, "indexName": INDEX}
    if error:
        stubber.add_client_error("delete_index", expected_params=params, **error)
    else:
        stubber.add_response("delete_index", {}, params)


def _delete_bucket(stubber, **error):
    params = {"vectorBucketName": BUCKET}
    if error:
        stubber.add_client_error(
            "delete_vector_bucket", expected_params=params, **error
        )
    else:
        stubber.add_response("delete_vector_bucket", {}, params)


NOT_FOUND = {"service_error_code": "NotFoundException", "http_status_code": 404}
INVALID = {"service_error_code": "ValidationException", "http_status_code": 400}
CONFLICT = {"service_error_code": "ConflictException", "http_status_code": 409}
NOT_EMPTY = {"service_error_code": "ConflictException", "http_status_code": 409}
DENIED = {"service_error_code": "AccessDeniedException", "http_status_code": 403}


def _get_bucket(stubber):
    stubber.add_response(
        "get_vector_bucket",
        {
            "vectorBucket": {
                "vectorBucketName": BUCKET,
                "vectorBucketArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}",
                "creationTime": "2026-01-01T00:00:00Z",
            }
        },
        {"vectorBucketName": BUCKET},
    )


def _caller_identity(sts_stubber):
    sts_stubber.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:root",
            "UserId": "x",
        },
        {},
    )


def test_error_codes_come_from_the_service_model():
    """The codes the handler branches on are ones the service can actually send.

    The previous not-found lists named ``NoSuchBucket``, ``IndexNotFound`` and
    ``NoSuchIndex``, none of which s3vectors defines, so a missing index raised.
    """
    assert handler.NOT_FOUND_CODES, "empty code set would make every check vacuous"
    assert handler.BUCKET_NOT_EMPTY_CODES
    assert handler.NOT_FOUND_CODES <= _errors("DeleteIndex")
    assert handler.NOT_FOUND_CODES <= _errors("DeleteVectorBucket")
    assert handler.BUCKET_NOT_EMPTY_CODES <= _errors("DeleteVectorBucket")
    assert handler.ABSENT_ON_DELETE_CODES <= _errors("DeleteIndex")
    assert handler.ABSENT_ON_DELETE_CODES <= _errors("DeleteVectorBucket")


def test_delete_removes_the_index_before_the_bucket(s3vectors):
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.sleeps == []


def test_delete_succeeds_when_the_index_is_already_gone(s3vectors):
    _delete_index(s3vectors.stubber, **NOT_FOUND)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_succeeds_when_bucket_and_index_are_already_gone(s3vectors):
    """A rollback of a failed Create, or a retried Delete, must not get stuck."""
    _delete_index(s3vectors.stubber, **NOT_FOUND)
    _delete_bucket(s3vectors.stubber, **NOT_FOUND)

    handler.lambda_handler(
        _event("Delete", PhysicalResourceId=f"s3vectors-failed://{BUCKET}/{INDEX}"),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_waits_for_the_bucket_to_become_empty(s3vectors):
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **NOT_EMPTY)
    _delete_bucket(s3vectors.stubber, **NOT_EMPTY)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.sleeps == [2, 4]


def test_delete_reports_failed_while_the_bucket_is_still_not_empty(s3vectors):
    """The defect itself: a bucket that cannot be deleted is not a successful delete."""
    _delete_index(s3vectors.stubber)
    for _ in range(handler.BUCKET_DELETE_ATTEMPTS):
        _delete_bucket(s3vectors.stubber, **NOT_EMPTY)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert BUCKET in _reason(s3vectors.send)
    assert "list-indexes" in _reason(s3vectors.send)
    assert "ConflictException" in _reason(s3vectors.send), "the service's error is kept"
    assert s3vectors.sleeps == [2, 4, 8, 16, 30, 30, 30]
    # The function's timeout is 600 s; the budget must leave most of it for the
    # calls themselves, not merely fit under it.
    assert sum(s3vectors.sleeps) <= 180, s3vectors.sleeps
    assert s3vectors.send.call_args.kwargs["physicalResourceId"] == PHYSICAL_ID


def test_delete_does_not_retry_an_error_that_waiting_cannot_fix(s3vectors):
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **DENIED)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert s3vectors.sleeps == []


def test_delete_reports_failed_when_the_index_cannot_be_deleted(s3vectors):
    _delete_index(s3vectors.stubber, **DENIED)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    # The failure must be the denied DeleteIndex itself, not a later call the stub
    # was never given (which would also read as FAILED).
    assert "AccessDeniedException" in _reason(s3vectors.send)
    assert "DeleteIndex" in _reason(s3vectors.send)


def test_update_with_unchanged_names_recreates_a_missing_index(s3vectors):
    """Update recreates the index, and a missing one must not fail the update."""
    s3vectors.stubber.add_response(
        "get_vector_bucket",
        {
            "vectorBucket": {
                "vectorBucketName": BUCKET,
                "vectorBucketArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}",
                "creationTime": "2026-01-01T00:00:00Z",
            }
        },
        {"vectorBucketName": BUCKET},
    )
    s3vectors.sts.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:root",
            "UserId": "x",
        },
        {},
    )
    _delete_index(s3vectors.stubber, **NOT_FOUND)
    s3vectors.stubber.add_response("create_index", {"indexArn": "arn:aws:s3vectors:x"})

    event = _event(
        "Update", OldResourceProperties=_event("Delete")["ResourceProperties"]
    )
    handler.lambda_handler(event, SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"


def test_update_that_renames_the_index_is_not_blocked_by_old_cleanup(s3vectors):
    """Cleanup of the previous index is best effort during Update.

    The role's DeleteIndex grant names the current index, so deleting the old one
    is denied. Failing the update over that would block a rename outright.
    """
    old_index = "old-index"
    s3vectors.stubber.add_client_error(
        "delete_index",
        expected_params={"vectorBucketName": BUCKET, "indexName": old_index},
        **DENIED,
    )
    # The bucket name did not change, so it still exists: CreateVectorBucket is
    # refused and the handler reads the existing bucket instead.
    s3vectors.stubber.add_client_error("create_vector_bucket", **CONFLICT)
    _get_bucket(s3vectors.stubber)
    _caller_identity(s3vectors.sts)
    s3vectors.stubber.add_response("create_index", {"indexArn": "arn:aws:s3vectors:x"})

    old = dict(_event("Delete")["ResourceProperties"], IndexName=old_index)
    handler.lambda_handler(
        _event("Update", OldResourceProperties=old),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_treats_a_name_the_service_rejects_as_nothing_to_delete(s3vectors):
    """A rollback after a Create the service refused must not end DELETE_FAILED.

    ``sanitize_bucket_name`` can still produce a name containing ``--``. If the
    service rejects it, nothing was created under it, so there is nothing to delete.
    """
    _delete_index(s3vectors.stubber, **INVALID)
    _delete_bucket(s3vectors.stubber, **INVALID)
    s3vectors.stubber.add_client_error(
        "get_vector_bucket", expected_params={"vectorBucketName": BUCKET}, **INVALID
    )

    handler.lambda_handler(
        _event("Delete", PhysicalResourceId=f"s3vectors-failed://{BUCKET}/{INDEX}"),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"


def test_create_records_bucket_and_index_in_the_physical_id(s3vectors):
    s3vectors.stubber.add_response("create_vector_bucket", {"vectorBucketArn": "arn:x"})
    s3vectors.stubber.add_response("create_index", {"indexArn": "arn:aws:s3vectors:x"})
    _caller_identity(s3vectors.sts)

    event = _event("Create")
    del event["PhysicalResourceId"]
    handler.lambda_handler(event, SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.send.call_args.kwargs["physicalResourceId"] == PHYSICAL_ID


def test_validation_error_on_an_existing_bucket_is_not_read_as_gone(s3vectors):
    """The defect again by another route: SUCCESS while the bucket exists.

    The service model does not say which code a non-empty bucket gets. If it is
    ValidationException, reading that as "absent" would report SUCCESS with the
    bucket still there, so the handler asks GetVectorBucket and keeps trying.
    """
    _delete_index(s3vectors.stubber)
    for _ in range(handler.BUCKET_DELETE_ATTEMPTS):
        _delete_bucket(s3vectors.stubber, **INVALID)
        _get_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "ValidationException" in _reason(s3vectors.send)
    assert len(s3vectors.sleeps) == handler.BUCKET_DELETE_ATTEMPTS - 1


def test_validation_error_then_successful_delete(s3vectors):
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **INVALID)
    _get_bucket(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == [
        "DeleteIndex",
        "DeleteVectorBucket",
        "GetVectorBucket",
        "DeleteVectorBucket",
    ]


def test_an_unanswerable_existence_check_is_not_read_as_gone(s3vectors):
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **INVALID)
    s3vectors.stubber.add_client_error(
        "get_vector_bucket", expected_params={"vectorBucketName": BUCKET}, **DENIED
    )

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "GetVectorBucket" in _reason(s3vectors.send)


def test_update_rebuild_does_not_skip_an_index_it_could_not_delete(s3vectors):
    """Only NotFound means "absent" when Update rebuilds the index.

    Reading ValidationException as absent there would let CreateIndex answer
    ConflictException ("already exists") and report the rebuild as done.
    """
    _get_bucket(s3vectors.stubber)
    _caller_identity(s3vectors.sts)
    _delete_index(s3vectors.stubber, **INVALID)

    event = _event(
        "Update", OldResourceProperties=_event("Delete")["ResourceProperties"]
    )
    handler.lambda_handler(event, SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "CreateIndex" not in s3vectors.calls
