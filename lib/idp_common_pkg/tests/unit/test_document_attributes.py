# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Caller-supplied document attributes (#1851, increment 1).

The S3 intake path is exercised end to end against moto's S3 and DynamoDB: an
object is uploaded with real user metadata (and, for the sidecar route, a real
second object), ``Document.from_s3_event`` reads it with a real ``head_object``,
and the document is written to and read back from a real tracking table. moto
lower-cases user-metadata names the way S3 does, which is the behaviour the
inline route's name mapping depends on.
"""

import json
import logging
import math

import boto3
import pytest
from moto import mock_aws

from idp_common import document_attributes as da
from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status

REGION = "us-east-1"
BUCKET = "attr-input-bucket"
OTHER_BUCKET = "attr-other-bucket"
TABLE = "attr-tracking-table"


def _s3():
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    client.create_bucket(Bucket=OTHER_BUCKET)
    return client


def _event(key, bucket=BUCKET):
    return {
        "detail": {"bucket": {"name": bucket}, "object": {"key": key}},
        "time": "2026-11-20T00:00:00Z",
    }


def _service():
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return DocumentDynamoDBService(
        dynamodb_client=DynamoDBClient(table_name=TABLE, region=REGION)
    )


def _raw_item(key):
    return (
        boto3.resource("dynamodb", region_name=REGION)
        .Table(TABLE)
        .get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]
    )


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["a", "account_id", "case-ref", "0abc", "a" * 63, "x_1-2"],
)
def test_keys_matching_the_rule_are_accepted(key):
    assert da.key_problem(key) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["", "A", "Account", "_lead", "-lead", "a" * 64, "a b", "a.b", "a/b", "é", 5],
)
def test_keys_outside_the_rule_are_refused(key):
    assert da.key_problem(key) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "ACME-0042",
        "x" * da.MAX_STRING_BYTES,
        0,
        -3,
        2.5,
        True,
        False,
        da.MAX_SAFE_INTEGER,
        ["a", 1, True],
        ["x"] * da.MAX_LIST_ITEMS,
        [],
    ],
)
def test_values_within_the_rules_are_accepted(value):
    assert da.value_problem(value) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        None,
        {"nested": "object"},
        [["nested"]],
        [{"a": 1}],
        "x" * (da.MAX_STRING_BYTES + 1),
        "é" * (da.MAX_STRING_BYTES // 2 + 1),  # bound is bytes, not characters
        ["x"] * (da.MAX_LIST_ITEMS + 1),
        ["x" * (da.MAX_STRING_BYTES + 1)],
        math.inf,
        math.nan,
        da.MAX_SAFE_INTEGER + 1,
        -(da.MAX_SAFE_INTEGER + 1),
    ],
)
def test_values_outside_the_rules_are_refused(value):
    assert da.value_problem(value) is not None


@pytest.mark.unit
def test_count_bound_keeps_the_first_keys_in_sorted_order():
    candidate = {f"k{i:03d}": "v" for i in range(da.MAX_ATTRIBUTES + 5)}
    intake = da.sanitize_attributes(candidate)
    assert len(intake.attributes) == da.MAX_ATTRIBUTES
    assert sorted(intake.attributes) == sorted(candidate)[: da.MAX_ATTRIBUTES]
    assert [k for k, _ in intake.dropped] == sorted(candidate)[da.MAX_ATTRIBUTES :]


@pytest.mark.unit
def test_total_size_bound_is_enforced():
    # 40 strings of 1000 bytes each is ~41 KB, over the 32 KB total.
    candidate = {f"k{i:02d}": "x" * 1000 for i in range(40)}
    intake = da.sanitize_attributes(candidate)
    assert intake.dropped
    assert da.serialized_size(intake.attributes) <= da.MAX_TOTAL_BYTES
    assert all("larger than" in why for _, why in intake.dropped)
    # Adding any dropped key back would break the bound, so nothing was dropped
    # that fitted.
    first_dropped = intake.dropped[0][0]
    trial = dict(intake.attributes, **{first_dropped: candidate[first_dropped]})
    assert da.serialized_size(trial) > da.MAX_TOTAL_BYTES


@pytest.mark.unit
def test_validate_raises_naming_every_problem():
    with pytest.raises(da.AttributeValidationError) as exc:
        da.validate_attributes({"Bad": "x", "ok": "y", "nested": {"a": 1}})
    message = str(exc.value)
    assert "'Bad'" in message and "'nested'" in message and "'ok'" not in message


@pytest.mark.unit
@pytest.mark.parametrize("bad", [None, [], "a=b", 3])
def test_validate_refuses_a_non_object(bad):
    with pytest.raises(da.AttributeValidationError):
        da.validate_attributes(bad)


@pytest.mark.unit
def test_an_invalid_key_is_quoted_bounded():
    intake = da.sanitize_attributes({"A" * 5000: "v"})
    ((key, _),) = intake.dropped
    assert len(key) <= 70


# --------------------------------------------------------------------------- #
# The Document model
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_a_document_without_attributes_serialises_without_the_key():
    data = Document(id="k", input_key="k").to_dict()
    assert "attributes" not in data
    assert Document.from_dict(data).attributes == {}


@pytest.mark.unit
def test_attributes_round_trip_through_to_dict_and_from_dict():
    attrs = {"account_id": "ACME-0042", "amount": 12.5, "flags": ["a", True]}
    doc = Document(id="k", input_key="k", attributes=dict(attrs))
    restored = Document.from_dict(json.loads(doc.to_json()))
    assert restored.attributes == attrs


# --------------------------------------------------------------------------- #
# S3 intake, end to end
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@mock_aws
def test_inline_metadata_is_read_with_hyphens_as_underscores():
    s3 = _s3()
    s3.put_object(
        Bucket=BUCKET,
        Key="in/doc.pdf",
        Body=b"%PDF",
        Metadata={"idp-attr-account-id": "ACME-0042", "config-version": "v1"},
    )
    doc = Document.from_s3_event(_event("in/doc.pdf"), "out")
    assert doc.attributes == {"account_id": "ACME-0042"}
    assert doc.config_version == "v1"
    assert doc.processing_issues == []


@pytest.mark.unit
@mock_aws
def test_an_upload_without_attributes_produces_the_same_document_as_before():
    s3 = _s3()
    s3.put_object(Bucket=BUCKET, Key="in/plain.pdf", Body=b"%PDF")
    doc = Document.from_s3_event(_event("in/plain.pdf"), "out")
    assert doc.attributes == {}
    assert doc.processing_issues == []
    assert "attributes" not in doc.to_dict()


@pytest.mark.unit
@mock_aws
def test_the_sidecar_route_reads_a_typed_set():
    s3 = _s3()
    attrs = {"case-ref": "C-1", "amount": 42, "tags": ["x", "y"], "urgent": True}
    s3.put_object(
        Bucket=BUCKET, Key="in/doc.pdf" + da.SIDECAR_SUFFIX, Body=json.dumps(attrs)
    )
    s3.put_object(
        Bucket=BUCKET,
        Key="in/doc.pdf",
        Body=b"%PDF",
        Metadata={
            da.SIDECAR_URI_METADATA_KEY: f"s3://{BUCKET}/in/doc.pdf"
            + da.SIDECAR_SUFFIX,
            "idp-attr-urgent": "inline-wins",
        },
    )
    doc = Document.from_s3_event(_event("in/doc.pdf"), "out")
    assert doc.attributes == {**attrs, "urgent": "inline-wins"}
    assert doc.processing_issues == []


@pytest.mark.unit
@mock_aws
@pytest.mark.parametrize(
    "uri_template, body, reason_fragment",
    [
        (
            "s3://" + OTHER_BUCKET + "/in/doc.pdf" + da.SIDECAR_SUFFIX,
            b"{}",
            "input bucket",
        ),
        ("s3://" + BUCKET + "/in/doc.json", b"{}", "does not end in"),
        ("https://example.invalid/x" + da.SIDECAR_SUFFIX, None, "not an s3://"),
        (
            "s3://" + BUCKET + "/in/missing" + da.SIDECAR_SUFFIX,
            None,
            "could not be read",
        ),
        (
            "s3://" + BUCKET + "/in/doc.pdf" + da.SIDECAR_SUFFIX,
            b"[1, 2]",
            "not a JSON object",
        ),
        (
            "s3://" + BUCKET + "/in/doc.pdf" + da.SIDECAR_SUFFIX,
            b"{not json",
            "not valid",
        ),
        (
            "s3://" + BUCKET + "/in/doc.pdf" + da.SIDECAR_SUFFIX,
            json.dumps({"k": "x" * da.MAX_TOTAL_BYTES}).encode(),
            "larger than",
        ),
    ],
)
def test_a_rejected_sidecar_is_dropped_with_a_warning(
    uri_template, body, reason_fragment
):
    s3 = _s3()
    if body is not None:
        bucket, _, key = uri_template[len("s3://") :].partition("/")
        s3.put_object(Bucket=bucket, Key=key, Body=body)
    s3.put_object(
        Bucket=BUCKET,
        Key="in/doc.pdf",
        Body=b"%PDF",
        Metadata={da.SIDECAR_URI_METADATA_KEY: uri_template, "idp-attr-ok": "kept"},
    )
    doc = Document.from_s3_event(_event("in/doc.pdf"), "out")
    assert doc.attributes == {"ok": "kept"}
    (issue,) = doc.processing_issues
    assert issue.code == da.ISSUE_ATTRIBUTES_DROPPED
    assert issue.severity == "warning" and issue.stage == "ingest"
    assert issue.details["dropped_count"] == 1
    assert reason_fragment in issue.details["dropped"][0]["reason"]


@pytest.mark.unit
@mock_aws
def test_invalid_inline_attributes_are_dropped_and_the_document_still_builds():
    s3 = _s3()
    s3.put_object(
        Bucket=BUCKET,
        Key="in/doc.pdf",
        Body=b"%PDF",
        Metadata={
            "idp-attr-good": "yes",
            "idp-attr-": "empty name",
            "idp-attr-" + "a" * 70: "too long a name",
            "idp-attr-big": "x" * (da.MAX_STRING_BYTES + 1),
        },
    )
    doc = Document.from_s3_event(_event("in/doc.pdf"), "out")
    assert doc.attributes == {"good": "yes"}
    (issue,) = doc.processing_issues
    assert issue.details["dropped_count"] == 3
    # Keys and reasons only: no value reaches the issue.
    rendered = json.dumps(issue.to_dict())
    assert "too long a name" not in rendered and "x" * 100 not in rendered


@pytest.mark.unit
@mock_aws
def test_attribute_values_are_not_logged(caplog):
    s3 = _s3()
    s3.put_object(
        Bucket=BUCKET,
        Key="in/doc.pdf",
        Body=b"%PDF",
        Metadata={
            "idp-attr-account-id": "SECRET-ACCOUNT-9",
            "idp-attr-Bad": "SECRET-2",
        },
    )
    with caplog.at_level(logging.DEBUG, logger="idp_common"):
        Document.from_s3_event(_event("in/doc.pdf"), "out")
    # Our own log lines only: botocore's DEBUG wire log echoes response headers,
    # and Lambda runs it at INFO, where it is silent.
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("idp_common")
    )
    assert "SECRET-ACCOUNT-9" not in ours
    assert "SECRET-2" not in ours
    # The name is still visible, so a missing attribute is debuggable.
    assert "idp-attr-account-id" in ours


@pytest.mark.unit
@mock_aws
def test_read_attributes_false_skips_them():
    s3 = _s3()
    s3.put_object(
        Bucket=BUCKET, Key="in/doc.pdf", Body=b"%PDF", Metadata={"idp-attr-a": "1"}
    )
    doc = Document.from_s3_event(_event("in/doc.pdf"), "out", read_attributes=False)
    assert doc.attributes == {}


@pytest.mark.unit
def test_sidecar_keys_are_recognised():
    assert da.is_sidecar_key("a/b.pdf" + da.SIDECAR_SUFFIX)
    assert not da.is_sidecar_key("a/b.pdf")
    assert not da.is_sidecar_key("a/b.json")


# --------------------------------------------------------------------------- #
# encode_for_upload -> S3 -> intake reproduces the set exactly
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@mock_aws
@pytest.mark.parametrize(
    "attrs, expect_sidecar",
    [
        ({"account_id": "ACME-0042", "region": "eu"}, False),
        ({"case-ref": "C-1"}, True),  # a hyphenated key cannot go inline
        ({"amount": 3}, True),  # non-strings cannot go inline
        ({"name": "Zoë"}, True),  # non-ASCII cannot go inline
        ({f"k{i:02d}": "x" * 100 for i in range(30)}, True),  # over S3's 2 KB
    ],
)
def test_encoded_upload_round_trips_through_intake(attrs, expect_sidecar):
    s3 = _s3()
    key = "in/doc.pdf"
    metadata, body = da.encode_for_upload(attrs, {"config-version": "v1"})
    assert (body is not None) is expect_sidecar
    if body is not None:
        s3.put_object(Bucket=BUCKET, Key=key + da.SIDECAR_SUFFIX, Body=body)
        metadata[da.SIDECAR_URI_METADATA_KEY] = (
            f"s3://{BUCKET}/{key}{da.SIDECAR_SUFFIX}"
        )
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"%PDF", Metadata=metadata)
    doc = Document.from_s3_event(_event(key), "out")
    assert doc.attributes == attrs
    assert doc.config_version == "v1"
    assert doc.processing_issues == []


# --------------------------------------------------------------------------- #
# Tracking table
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@mock_aws
def test_attributes_are_written_on_create_and_read_back():
    service = _service()
    attrs = {
        "account_id": "ACME-0042",
        "amount": 12.5,
        "count": 3,
        "flags": [True, "x"],
    }
    service.create_document(
        Document(
            id="a/x.pdf",
            input_key="a/x.pdf",
            status=Status.QUEUED,
            queued_time="2026-11-20T00:00:00+00:00",
            attributes=dict(attrs),
        )
    )
    assert "DocumentAttributes" in _raw_item("a/x.pdf")
    assert service.get_document("a/x.pdf").attributes == attrs


@pytest.mark.unit
@mock_aws
def test_a_document_without_attributes_writes_no_attribute():
    service = _service()
    service.create_document(
        Document(
            id="a/y.pdf",
            input_key="a/y.pdf",
            status=Status.QUEUED,
            queued_time="2026-11-20T00:00:00+00:00",
        )
    )
    assert "DocumentAttributes" not in _raw_item("a/y.pdf")
    assert service.get_document("a/y.pdf").attributes == {}


@pytest.mark.unit
@mock_aws
def test_a_whole_document_update_keeps_the_stored_attributes():
    service = _service()
    doc = Document(
        id="a/z.pdf",
        input_key="a/z.pdf",
        status=Status.QUEUED,
        queued_time="2026-11-20T00:00:00+00:00",
        attributes={"account_id": "A"},
    )
    service.create_document(doc)
    # An update from a document object that carries no attributes (a payload
    # rebuilt by a later step) must not clear them.
    service.update_document(
        Document(id="a/z.pdf", input_key="a/z.pdf", status=Status.COMPLETED)
    )
    assert service.get_document("a/z.pdf").attributes == {"account_id": "A"}


@pytest.mark.unit
def test_the_drop_issue_carries_the_modules_code_and_bounds_what_it_names():
    from idp_common.models import attributes_dropped_issue

    dropped = [(f"k{i}", "reason") for i in range(da.MAX_DROPPED_NAMED + 5)]
    issue = attributes_dropped_issue(dropped)
    assert issue.code == da.ISSUE_ATTRIBUTES_DROPPED
    assert issue.details["dropped_count"] == da.MAX_DROPPED_NAMED + 5
    assert len(issue.details["dropped"]) == da.MAX_DROPPED_NAMED


@pytest.mark.unit
def test_document_attributes_does_not_import_models():
    """One direction only: models uses this module, never the reverse, so the
    two do not form an import cycle."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(da))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(name.startswith("idp_common") for name in imported), imported
