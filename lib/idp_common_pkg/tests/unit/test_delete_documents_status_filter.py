# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``status_filter`` against tracking records written by the real writer (#2189).

The bulk-delete selectors filtered on an attribute named ``Status``, which no
tracking record carries: ``DocumentDynamoDBService`` stores a document's status
as ``ObjectStatus``. Every earlier test of the filter seeded its own records with
``Status`` and so agreed with the defect. Here the records come from
``create_document``, ``update_document`` and ``create_document_run`` — the
pipeline's own writers — and the scan runs against a moto table, so the filter
expression is evaluated by moto's DynamoDB emulation rather than asserted on.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.delete_documents import (
    get_documents_by_batch,
    get_documents_by_pattern,
)
from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status

REGION = "us-east-1"
TABLE = "status-filter-tracking"

#: Each document's key and the status the writer leaves it in.
DOCUMENT_STATUSES = (
    ("batch-1/done.pdf", Status.COMPLETED),
    ("batch-1/broken.pdf", Status.FAILED),
    ("batch-1/waiting.pdf", Status.QUEUED),
    ("batch-2/other-broken.pdf", Status.FAILED),
    # Statuses where one name is a prefix of others, so a prefix match would
    # widen the delete.
    ("batch-3/validating.pdf", Status.RULE_VALIDATION),
    ("batch-3/orchestrating.pdf", Status.RULE_VALIDATION_ORCHESTRATOR),
    ("batch-3/policy.pdf", Status.RULE_VALIDATION_POLICY_CLASSIFICATION),
)


@pytest.fixture
def tracking_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        table = resource.create_table(
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
        writer = DocumentDynamoDBService(
            dynamodb_client=DynamoDBClient(table_name=TABLE, region=REGION)
        )
        for key, status in DOCUMENT_STATUSES:
            document = Document(
                id=key,
                input_key=key,
                status=Status.QUEUED,
                queued_time="2026-11-20T10:00:00Z",
                initial_event_time="2026-11-20T10:00:00Z",
            )
            writer.create_document(document)
            if status is not Status.QUEUED:
                document.status = status
                writer.update_document(document)
        # A completed run of the FAILED document: run records carry no status, so
        # they must neither select the document for COMPLETED nor duplicate it.
        broken = Document(
            id="batch-1/broken.pdf",
            input_key="batch-1/broken.pdf",
            status=Status.COMPLETED,
            queued_time="2026-11-20T09:00:00Z",
        )
        writer.create_document_run(broken, "20261120T090000Z-r1", "s3://m/x")
        yield table


def _stored_statuses(table):
    """What the writer stored, read back, so the fixture is checked, not assumed."""
    items = table.scan()["Items"]
    return {
        item["PK"]: item.get("ObjectStatus")
        for item in items
        if item["PK"].startswith("doc#") and item["SK"] == "none"
    }


@pytest.mark.unit
def test_the_writer_stores_the_status_as_object_status(tracking_table):
    stored = _stored_statuses(tracking_table)
    assert stored == {f"doc#{key}": status.value for key, status in DOCUMENT_STATUSES}
    assert not any("Status" in item for item in tracking_table.scan()["Items"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("FAILED", ["batch-1/broken.pdf"]),
        ("COMPLETED", ["batch-1/done.pdf"]),
        ("QUEUED", ["batch-1/waiting.pdf"]),
    ],
)
def test_batch_selection_honours_the_status_filter(tracking_table, status, expected):
    assert (
        get_documents_by_batch(tracking_table, "batch-1", status_filter=status)
        == expected
    )


@pytest.mark.unit
def test_pattern_selection_honours_the_status_filter(tracking_table):
    selected = get_documents_by_pattern(tracking_table, "*", status_filter="FAILED")
    assert sorted(selected) == ["batch-1/broken.pdf", "batch-2/other-broken.pdf"]


@pytest.mark.unit
def test_a_status_no_document_has_selects_nothing(tracking_table):
    assert get_documents_by_pattern(tracking_table, "*", status_filter="RUNNING") == []


@pytest.mark.unit
def test_no_status_filter_selects_every_document_once(tracking_table):
    selected = get_documents_by_pattern(tracking_table, "*")
    assert sorted(selected) == sorted(key for key, _ in DOCUMENT_STATUSES)


@pytest.mark.unit
def test_the_status_filter_is_an_exact_match(tracking_table):
    """A status that prefixes others must not select their documents too."""
    assert get_documents_by_batch(
        tracking_table, "batch-3", status_filter="RULE_VALIDATION"
    ) == ["batch-3/validating.pdf"]
