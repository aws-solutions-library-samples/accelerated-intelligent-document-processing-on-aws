# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The guard on direct writes of a document's section results.

A document in a review queue is edited only through its queue item, so a direct
upload of one of its section results (``uploadDocument``) is refused with the
``QUEUE_MANAGED`` code the UI shows. Any other document, key or bucket passes.
"""

import logging

import boto3
import pytest
from moto import mock_aws

from idp_common.review_queues.section_writes import (
    QUEUE_MANAGED_MESSAGE,
    assert_section_result_writable,
    section_result_document,
)

OUTPUT = "output-bucket"
KEY = "in/a b.pdf/sections/1/result.json"


@pytest.fixture
def tracking():
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="tracking",
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


def _row(table, status, document="in/a b.pdf"):
    table.put_item(Item={"PK": f"doc#{document}", "SK": "none", "HITLStatus": status})


def _check(table, key=KEY, bucket=OUTPUT):
    assert_section_result_writable(table, bucket=bucket, key=key, output_bucket=OUTPUT)


@pytest.mark.unit
class TestSectionResultWrites:
    def test_an_in_review_document_is_refused_with_a_warning(self, tracking, caplog):
        _row(tracking, "in_review")
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ValueError) as excinfo:
                _check(tracking)
        assert str(excinfo.value) == QUEUE_MANAGED_MESSAGE
        assert str(excinfo.value).startswith("QUEUE_MANAGED:")
        assert any(
            "Rejecting" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("status", ["complete", "rejected", "REVIEW_IN_PROGRESS"])
    def test_a_finished_or_legacy_review_passes(self, tracking, status):
        _row(tracking, status)
        _check(tracking)

    def test_a_document_with_no_row_passes(self, tracking):
        _check(tracking)

    def test_a_page_key_and_another_bucket_pass_without_a_read(self, tracking):
        _row(tracking, "in_review")
        _check(tracking, key="in/a b.pdf/pages/1/result.json")
        _check(tracking, bucket="baseline-bucket")

    def test_a_key_with_spaces_and_slashes_names_its_document(self):
        assert section_result_document(KEY) == "in/a b.pdf"
        assert (
            section_result_document("x/y/z.pdf/sections/12/result.json") == "x/y/z.pdf"
        )
        assert section_result_document("in/a.pdf/sections/1/other.json") is None
        assert section_result_document("sections/1/result.json") is None
