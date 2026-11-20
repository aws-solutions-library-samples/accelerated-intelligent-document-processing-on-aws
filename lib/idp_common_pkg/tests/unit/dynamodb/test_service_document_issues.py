# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document-level processing issues reach the tracking row and come back (#597).

Ingest is the one stage with no section to hang a ``ProcessingIssue`` on: the row
is created before classification has made any. ``queue_sender`` records a warning
there when the prefix-mapping read fails and the document is routed with no mapping
consulted, and that warning is only useful if it is on the row the UI reads -- so
these go through real DynamoDB (moto), create, read and update alike.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, ProcessingIssue, Section, Status

TABLE = "tracking-issues-test-table"


def _service():
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
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
        dynamodb_client=DynamoDBClient(table_name=TABLE, region="us-east-1")
    )


def _raw(key):
    return (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TABLE)
        .get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]
    )


def _ingest_issue():
    return ProcessingIssue(
        stage="ingest",
        severity="warning",
        code="config_prefix_mapping_lookup_failed",
        message="The configuration prefix mappings could not be read.",
        root_cause="ClientError: ProvisionedThroughputExceededException",
    )


def _document(key="a/x.pdf", issues=None):
    return Document(
        id=key,
        input_key=key,
        status=Status.QUEUED,
        queued_time="2026-11-19T00:00:00+00:00",
        initial_event_time="2026-11-19T00:00:00+00:00",
        processing_issues=list(issues or []),
    )


@pytest.mark.unit
@mock_aws
def test_an_ingest_issue_is_written_on_create_with_its_count():
    service = _service()
    service.create_document(_document(issues=[_ingest_issue()]))
    item = _raw("a/x.pdf")
    assert item["ProcessingIssues"] == [
        {
            "stage": "ingest",
            "severity": "warning",
            "code": "config_prefix_mapping_lookup_failed",
            "message": "The configuration prefix mappings could not be read.",
            "rootCause": "ClientError: ProvisionedThroughputExceededException",
        }
    ]
    # The list badge reads the count, so it has text behind it from the start.
    assert item["ProcessingIssueCount"] == 1


@pytest.mark.unit
@mock_aws
def test_a_document_with_no_issue_gains_no_attribute():
    service = _service()
    service.create_document(_document())
    item = _raw("a/x.pdf")
    assert "ProcessingIssues" not in item
    assert "ProcessingIssueCount" not in item


@pytest.mark.unit
@mock_aws
def test_the_issue_is_read_back_and_survives_a_whole_document_update():
    """A load -> write round trip must keep the issue AND keep counting it."""
    service = _service()
    service.create_document(_document(issues=[_ingest_issue()]))

    loaded = service.get_document("a/x.pdf")
    assert [i.code for i in loaded.processing_issues] == [
        "config_prefix_mapping_lookup_failed"
    ]
    loaded.sections = [Section(section_id="1", classification="w2", page_ids=["1"])]
    service.update_document(loaded)

    item = _raw("a/x.pdf")
    assert [i["code"] for i in item["ProcessingIssues"]] == [
        "config_prefix_mapping_lookup_failed"
    ]
    assert item["ProcessingIssueCount"] == 1


@pytest.mark.unit
@mock_aws
def test_a_whole_document_update_writes_issues_the_row_lacks():
    """The update path writes the document-level list too, under the same gate as
    the count, so the text behind the badge comes from the same object."""
    service = _service()
    service.create_document(_document())
    carrying = _document(issues=[_ingest_issue()])
    carrying.sections = [Section(section_id="1", classification="w2", page_ids=["1"])]
    service.update_document(carrying)
    item = _raw("a/x.pdf")
    assert [i["code"] for i in item["ProcessingIssues"]] == [
        "config_prefix_mapping_lookup_failed"
    ]
    assert item["ProcessingIssueCount"] == 1
