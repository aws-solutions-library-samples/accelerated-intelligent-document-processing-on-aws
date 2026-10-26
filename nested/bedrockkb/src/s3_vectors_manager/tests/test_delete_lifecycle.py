# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deleting the S3 Vectors bucket and index, driven through ``lambda_handler``.

S3 Vectors refuses to delete a vector bucket that still holds an index. The Delete
branch used to call ``delete_vector_bucket`` alone, log the refusal as expected and
report SUCCESS, so every stack delete left the bucket and its index behind
(issue #1589). These tests pin the replacement behaviour: every index in the
bucket is listed and deleted first, a bucket or index that is already gone counts
as deleted, and the handler reports FAILED rather than SUCCESS while the bucket
still exists.

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


def _list_indexes(stubber, *names, then=None, after=None, bucket=BUCKET, **error):
    """One ListIndexes page. ``after`` is the nextToken the request must carry;
    ``then`` is the one the page answers with (None ends the listing)."""
    params = {"vectorBucketName": bucket, "maxResults": handler.INDEX_LIST_PAGE_SIZE}
    if after:
        params["nextToken"] = after
    if error:
        stubber.add_client_error("list_indexes", expected_params=params, **error)
        return
    page = {
        "indexes": [
            {
                "vectorBucketName": bucket,
                "indexName": name,
                "indexArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{bucket}/index/{name}",
                "creationTime": "2026-01-01T00:00:00Z",
            }
            for name in names
        ]
    }
    if then:
        page["nextToken"] = then
    stubber.add_response("list_indexes", page, params)


def _delete_index(stubber, name=INDEX, bucket=BUCKET, **error):
    params = {"vectorBucketName": bucket, "indexName": name}
    if error:
        stubber.add_client_error("delete_index", expected_params=params, **error)
    else:
        stubber.add_response("delete_index", {}, params)


def _delete_bucket(stubber, bucket=BUCKET, **error):
    params = {"vectorBucketName": bucket}
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


def test_list_page_size_is_the_service_maximum():
    """maxResults is validated by the Stubber against the model's range, and the
    handler asks for the largest page the service allows."""
    shape = MODEL.operation_model("ListIndexes").input_shape.members["maxResults"]
    assert handler.INDEX_LIST_PAGE_SIZE == shape.metadata["max"]


def test_delete_removes_the_index_before_the_bucket(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.sleeps == []


def test_delete_succeeds_when_the_index_is_already_gone(s3vectors):
    _list_indexes(s3vectors.stubber)
    _delete_index(s3vectors.stubber, **NOT_FOUND)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_succeeds_when_bucket_and_index_are_already_gone(s3vectors):
    """A rollback of a failed Create, or a retried Delete, must not get stuck."""
    _list_indexes(s3vectors.stubber, **NOT_FOUND)
    _delete_index(s3vectors.stubber, **NOT_FOUND)
    _delete_bucket(s3vectors.stubber, **NOT_FOUND)

    handler.lambda_handler(
        _event("Delete", PhysicalResourceId=f"s3vectors-failed://{BUCKET}/{INDEX}"),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_waits_for_the_bucket_to_become_empty(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **NOT_EMPTY)
    _delete_bucket(s3vectors.stubber, **NOT_EMPTY)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.sleeps == [2, 4]


def test_delete_reports_failed_while_the_bucket_is_still_not_empty(s3vectors):
    """The defect itself: a bucket that cannot be deleted is not a successful delete."""
    _list_indexes(s3vectors.stubber, INDEX)
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
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **DENIED)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert s3vectors.sleeps == []


def test_delete_reports_failed_when_the_index_cannot_be_deleted(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, **DENIED)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    # The failure must be the denied DeleteIndex itself, not a later call the stub
    # was never given (which would also read as FAILED).
    assert "AccessDeniedException" in _reason(s3vectors.send)
    assert "DeleteIndex" in _reason(s3vectors.send)


def _rebuild_old_properties() -> dict:
    """Previous properties that differ from ``_event``'s in an index setting.

    The handler recreates the index on Update only when its dimension, distance
    metric or metadata configuration changed, so these tests change one of them.
    """
    return dict(_event("Delete")["ResourceProperties"], DistanceMetric="euclidean")


def test_update_with_unchanged_names_recreates_a_missing_index(s3vectors):
    """An index rebuild recreates a missing index rather than failing the update."""
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

    event = _event("Update", OldResourceProperties=_rebuild_old_properties())
    handler.lambda_handler(event, SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"


def test_delete_treats_a_name_the_service_rejects_as_nothing_to_delete(s3vectors):
    """A rollback after a Create the service refused must not end DELETE_FAILED.

    ``sanitize_bucket_name`` can still produce a name containing ``--``. If the
    service rejects it, nothing was created under it, so there is nothing to delete.
    """
    _list_indexes(s3vectors.stubber, **INVALID)
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
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber)
    for _ in range(handler.BUCKET_DELETE_ATTEMPTS):
        _delete_bucket(s3vectors.stubber, **INVALID)
        _get_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "ValidationException" in _reason(s3vectors.send)
    assert len(s3vectors.sleeps) == handler.BUCKET_DELETE_ATTEMPTS - 1


def test_validation_error_then_successful_delete(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber, **INVALID)
    _get_bucket(s3vectors.stubber)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == [
        "ListIndexes",
        "DeleteIndex",
        "DeleteVectorBucket",
        "GetVectorBucket",
        "DeleteVectorBucket",
    ]


def test_an_unanswerable_existence_check_is_not_read_as_gone(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX)
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

    event = _event("Update", OldResourceProperties=_rebuild_old_properties())
    handler.lambda_handler(event, SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "CreateIndex" not in s3vectors.calls


def test_delete_removes_every_index_in_the_bucket_across_pages(s3vectors):
    """The decision this file pins: a bucket holding indexes the resource did not
    name (one left by an index rename, one created outside the stack) is still
    deleted. The listing follows nextToken, and the named index, listed or not,
    is deleted exactly once."""
    _list_indexes(s3vectors.stubber, "old-index", INDEX, then="page-2")
    _list_indexes(s3vectors.stubber, "made-elsewhere", after="page-2")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "old-index")
    _delete_index(s3vectors.stubber, "made-elsewhere", **NOT_FOUND)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == [
        "ListIndexes",
        "ListIndexes",
        "DeleteIndex",
        "DeleteIndex",
        "DeleteIndex",
        "DeleteVectorBucket",
    ]


def test_delete_fails_when_another_index_cannot_be_deleted(s3vectors):
    _list_indexes(s3vectors.stubber, INDEX, "other")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "other", **DENIED)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "AccessDeniedException" in _reason(s3vectors.send)
    assert "DeleteVectorBucket" not in s3vectors.calls


def test_a_failed_listing_still_deletes_the_named_index_and_the_bucket(s3vectors):
    """ListIndexes refused (for example by a permissions boundary): the handler
    deletes the index it knows and lets the bucket delete decide. A bucket that
    held only that index is deleted, so the stack is not left DELETE_FAILED."""
    _list_indexes(s3vectors.stubber, **DENIED)
    _delete_index(s3vectors.stubber, INDEX)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == ["ListIndexes", "DeleteIndex", "DeleteVectorBucket"]


def test_a_failed_listing_cannot_report_success_while_the_bucket_is_not_empty(
    s3vectors,
):
    _list_indexes(s3vectors.stubber, **DENIED)
    _delete_index(s3vectors.stubber, INDEX)
    for _ in range(handler.BUCKET_DELETE_ATTEMPTS):
        _delete_bucket(s3vectors.stubber, **NOT_EMPTY)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert "list-indexes" in _reason(s3vectors.send)


def test_a_repeated_token_ends_the_listing(s3vectors, caplog):
    """A service that keeps answering the same nextToken must not loop forever."""
    _list_indexes(s3vectors.stubber, INDEX, then="same")
    _list_indexes(s3vectors.stubber, "other", then="same", after="same")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "other")
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls.count("ListIndexes") == 2
    assert "repeated a nextToken" in caplog.text


def test_the_listing_stops_at_the_page_cap(s3vectors, monkeypatch, caplog):
    monkeypatch.setattr(handler, "INDEX_LIST_MAX_PAGES", 2)
    _list_indexes(s3vectors.stubber, INDEX, then="t1")
    _list_indexes(s3vectors.stubber, "second-page", then="t2", after="t1")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "second-page")
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls.count("ListIndexes") == 2
    assert "more than 2 pages" in caplog.text


def test_update_that_renames_the_index_keeps_the_old_index_and_the_bucket(
    s3vectors,
):
    """An index rename creates the new index in the existing bucket and deletes
    nothing. The knowledge base keeps pointing at the old index, and a rollback
    of the stack update returns to it, so deleting it here would lose its
    vectors. The stack Delete deletes it with every other index in the bucket
    (``test_delete_removes_every_index_in_the_bucket_across_pages``)."""
    old_index = "old-index"
    s3vectors.stubber.add_client_error("create_vector_bucket", **CONFLICT)
    _get_bucket(s3vectors.stubber)
    _caller_identity(s3vectors.sts)
    s3vectors.stubber.add_response(
        "create_index",
        {"indexArn": "arn:aws:s3vectors:x"},
        {
            "vectorBucketName": BUCKET,
            "indexName": INDEX,
            "dataType": "float32",
            "dimension": 1024,
            "distanceMetric": "cosine",
            "metadataConfiguration": handler.DEFAULT_METADATA_CONFIGURATION,
        },
    )

    old = dict(_event("Delete")["ResourceProperties"], IndexName=old_index)
    handler.lambda_handler(
        _event("Update", OldResourceProperties=old),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == ["CreateVectorBucket", "GetVectorBucket", "CreateIndex"]


def test_update_that_renames_the_bucket_deletes_the_old_bucket_and_all_its_indexes(
    s3vectors,
):
    old_bucket = "old-bucket-s3-vectors"
    _list_indexes(s3vectors.stubber, INDEX, "extra", bucket=old_bucket)
    _delete_index(s3vectors.stubber, INDEX, bucket=old_bucket)
    _delete_index(s3vectors.stubber, "extra", bucket=old_bucket)
    _delete_bucket(s3vectors.stubber, bucket=old_bucket)
    s3vectors.stubber.add_response(
        "create_vector_bucket",
        {"vectorBucketArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}"},
        {"vectorBucketName": BUCKET},
    )
    s3vectors.stubber.add_response("create_index", {"indexArn": "arn:aws:s3vectors:x"})
    _caller_identity(s3vectors.sts)

    old = dict(_event("Delete")["ResourceProperties"], BucketName=old_bucket)
    handler.lambda_handler(
        _event("Update", OldResourceProperties=old),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == [
        "ListIndexes",
        "DeleteIndex",
        "DeleteIndex",
        "DeleteVectorBucket",
        "CreateVectorBucket",
        "CreateIndex",
    ]


def test_a_transport_error_while_listing_falls_back_to_the_named_index(
    s3vectors, monkeypatch
):
    """``Stubber`` cannot raise a transport error, so the listing itself is
    replaced. A connection failure says nothing about what the bucket holds;
    the bucket delete decides."""
    from botocore.exceptions import EndpointConnectionError

    def unreachable(*_args, **_kwargs):
        raise EndpointConnectionError(endpoint_url="https://s3vectors.invalid")

    monkeypatch.setattr(handler, "list_vector_index_names", unreachable)
    _delete_index(s3vectors.stubber, INDEX)
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "SUCCESS"
    assert s3vectors.calls == ["DeleteIndex", "DeleteVectorBucket"]


def test_a_defect_in_the_listing_is_not_swallowed(s3vectors, monkeypatch):
    def broken(*_args, **_kwargs):
        raise KeyError("indexName")

    monkeypatch.setattr(handler, "list_vector_index_names", broken)

    handler.lambda_handler(_event("Delete"), SimpleNamespace(log_stream_name="ls"))

    assert _status(s3vectors.send) == "FAILED"
    assert s3vectors.calls == []


def test_update_that_renames_the_bucket_is_not_blocked_by_old_cleanup(s3vectors):
    """Cleanup of the previous bucket is best effort during Update."""
    old_bucket = "old-bucket-s3-vectors"
    _list_indexes(s3vectors.stubber, INDEX, bucket=old_bucket)
    _delete_index(s3vectors.stubber, INDEX, bucket=old_bucket, **DENIED)
    s3vectors.stubber.add_response(
        "create_vector_bucket",
        {"vectorBucketArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}"},
        {"vectorBucketName": BUCKET},
    )
    s3vectors.stubber.add_response("create_index", {"indexArn": "arn:aws:s3vectors:x"})
    _caller_identity(s3vectors.sts)

    old = dict(_event("Delete")["ResourceProperties"], BucketName=old_bucket)
    handler.lambda_handler(
        _event("Update", OldResourceProperties=old),
        SimpleNamespace(log_stream_name="ls"),
    )

    assert _status(s3vectors.send) == "SUCCESS"
    assert "DeleteVectorBucket" not in s3vectors.calls


class _Clock:
    """A Lambda context whose remaining time is read from a list, one value
    per call, holding the last value once the list is used up."""

    log_stream_name = "ls"

    def __init__(self, *seconds_left):
        self._left = list(seconds_left)

    def get_remaining_time_in_millis(self):
        value = self._left.pop(0) if len(self._left) > 1 else self._left[0]
        return int(value * 1000)


def test_index_deletes_stop_before_the_function_timeout(s3vectors):
    """A bucket with many indexes reports FAILED, naming how far it got, rather
    than running past the timeout and leaving CloudFormation waiting."""
    margin = handler.INDEX_DELETE_MARGIN_SECONDS
    _list_indexes(s3vectors.stubber, INDEX, "second", "third")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "second")

    handler.lambda_handler(
        _event("Delete"), _Clock(margin + 100, margin + 1, margin - 1)
    )

    assert _status(s3vectors.send) == "FAILED"
    assert "after 2 of 3" in _reason(s3vectors.send)
    assert "DeleteVectorBucket" not in s3vectors.calls


def test_index_deletes_run_while_the_margin_remains(s3vectors):
    margin = handler.INDEX_DELETE_MARGIN_SECONDS
    _list_indexes(s3vectors.stubber, INDEX, "second")
    _delete_index(s3vectors.stubber, INDEX)
    _delete_index(s3vectors.stubber, "second")
    _delete_bucket(s3vectors.stubber)

    handler.lambda_handler(_event("Delete"), _Clock(margin, margin))

    assert _status(s3vectors.send) == "SUCCESS"


def test_the_margin_covers_the_bucket_delete_and_the_response():
    """The margin left for the bucket delete is at least its own backoff, with
    room for the calls and the CloudFormation response."""
    backoff = sum(
        min(
            handler.BUCKET_DELETE_BACKOFF_SECONDS * 2**i,
            handler.BUCKET_DELETE_MAX_BACKOFF_SECONDS,
        )
        for i in range(handler.BUCKET_DELETE_ATTEMPTS - 1)
    )
    assert backoff == 120
    assert handler.INDEX_DELETE_MARGIN_SECONDS >= backoff + 60
