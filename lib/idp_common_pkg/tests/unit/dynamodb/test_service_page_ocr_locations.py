# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A page's OCR confidence and raw OCR locations survive the tracking row (#2258).

Reprocess and Send back for reprocessing rebuild the document from the tracking
row. The row stored only ``TextUri`` (the parsed text), so a rebuilt page had no
``text_confidence_uri`` and its ``raw_text_uri`` pointed at the parsed text: the
confidence assessment then ran with no OCR confidence and scored every populated
field 1.0. These go through real DynamoDB (moto) and the service's own writer and
reader, and through ``ReprocessService`` for the path the defect was seen on.
"""

import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Page, Section, Status
from idp_common.run_plan import reprocess as reprocess_module
from idp_common.run_plan.reprocess import QUEUED, ReprocessService

pytestmark = pytest.mark.unit

TABLE = "tracking-ocr-locations-test-table"
REGION = "us-east-1"
OUT = "test-out"
KEY = "acme/statement.pdf"


def _uri(page_id, filename, key=KEY):
    return f"s3://{OUT}/{key}/pages/{page_id}/{filename}"


@pytest.fixture
def service(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
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
        yield DocumentDynamoDBService(
            dynamodb_client=DynamoDBClient(table_name=TABLE, region=REGION)
        )


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _raw_pages(key=KEY):
    item = _table().get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]
    return {str(p["Id"]): p for p in item["Pages"]}


def _ocr_page(page_id):
    """A page as the OCR step produces it: all four outputs, side by side."""
    return Page(
        page_id=page_id,
        image_uri=_uri(page_id, "image.jpg"),
        raw_text_uri=_uri(page_id, "rawText.json"),
        parsed_text_uri=_uri(page_id, "result.json"),
        text_confidence_uri=_uri(page_id, "textConfidence.json"),
        ocr_page_data_uri=_uri(page_id, "pageData.json"),
        classification="Invoice",
    )


def _document(pages, key=KEY):
    document = Document(
        id=key,
        input_key=key,
        input_bucket="test-in",
        output_bucket=OUT,
        status=Status.COMPLETED,
        queued_time="2026-11-01T00:00:00+00:00",
        initial_event_time="2026-11-01T00:00:00+00:00",
        config_version="default",
    )
    for page in pages:
        document.pages[page.page_id] = page
    document.num_pages = len(pages)
    document.sections.append(
        Section(
            section_id="1",
            classification="Invoice",
            page_ids=[p.page_id for p in pages],
            extraction_result_uri=f"s3://{OUT}/{key}/sections/1/result.json",
        )
    )
    return document


def _store(service, document):
    service.create_document(document)
    service.update_document(document)


def _put_legacy_row(pages, key=KEY):
    """A row as code before #2258 wrote it: TextUri, no confidence, no raw."""
    _table().put_item(
        Item={
            "PK": f"doc#{key}",
            "SK": "none",
            "ObjectKey": key,
            "ObjectStatus": "COMPLETED",
            "Pages": pages,
        }
    )


class TestTheWriterAndReaderRoundTrip:
    def test_the_confidence_and_raw_locations_are_stored(self, service):
        _store(service, _document([_ocr_page("1")]))

        page = _raw_pages()["1"]

        assert page["TextUri"] == _uri("1", "result.json")
        assert page["TextConfidenceUri"] == _uri("1", "textConfidence.json")
        assert page["RawTextUri"] == _uri("1", "rawText.json")

    def test_a_rebuilt_page_has_the_locations_the_ocr_step_gave_it(self, service):
        original = _ocr_page("1")
        _store(service, _document([original]))

        rebuilt = service.get_document(KEY).pages["1"]

        assert rebuilt.text_confidence_uri == original.text_confidence_uri
        assert rebuilt.raw_text_uri == original.raw_text_uri
        assert rebuilt.parsed_text_uri == original.parsed_text_uri
        assert rebuilt.ocr_page_data_uri == original.ocr_page_data_uri

    def test_a_page_without_them_adds_no_attributes(self, service):
        # Only parsed text, as BDA's older pages and minimal fixtures have.
        _store(
            service,
            _document([Page(page_id="1", parsed_text_uri=_uri("1", "other.json"))]),
        )

        page = _raw_pages()["1"]

        assert "TextConfidenceUri" not in page
        assert "RawTextUri" not in page

    def test_a_raw_location_equal_to_the_text_location_is_not_repeated(self, service):
        same = _uri("1", "result.json")
        _store(
            service,
            _document([Page(page_id="1", raw_text_uri=same, parsed_text_uri=same)]),
        )

        assert "RawTextUri" not in _raw_pages()["1"]

    def test_a_run_record_snapshots_them_too(self, service):
        document = _document([_ocr_page("1")])
        service.create_document_run(
            document, run_id="r1", manifest_uri="s3://m", file_count=1
        )

        (page,) = service.get_document_run(KEY, "r1")["Pages"]

        assert page["TextConfidenceUri"] == _uri("1", "textConfidence.json")
        assert page["RawTextUri"] == _uri("1", "rawText.json")


class TestARowWrittenBeforeTheFix:
    def test_the_locations_are_recovered_from_the_ocr_page_layout(self, service):
        _put_legacy_row(
            [
                {
                    "Id": 1,
                    "Class": "Invoice",
                    "ImageUri": _uri("1", "image.jpg"),
                    "TextUri": _uri("1", "result.json"),
                    "OcrPageDataUri": _uri("1", "pageData.json"),
                }
            ]
        )

        page = service.get_document(KEY).pages["1"]

        assert page.text_confidence_uri == _uri("1", "textConfidence.json")
        assert page.raw_text_uri == _uri("1", "rawText.json")
        assert page.parsed_text_uri == _uri("1", "result.json")

    def test_a_location_in_another_layout_is_not_guessed_at(self, service):
        # BDA mode stores parsedResult.json; a different page id in the path is
        # not this page's output either.
        bda = _uri("1", "parsedResult.json")
        other_page = _uri("7", "result.json")
        _put_legacy_row(
            [
                {"Id": 1, "TextUri": bda},
                {"Id": 2, "TextUri": other_page},
            ]
        )

        pages = service.get_document(KEY).pages

        assert pages["1"].text_confidence_uri is None
        assert pages["1"].raw_text_uri == bda
        assert pages["2"].text_confidence_uri is None
        assert pages["2"].raw_text_uri == other_page

    def test_a_stored_location_wins_over_the_derived_one(self, service):
        _put_legacy_row(
            [
                {
                    "Id": 1,
                    "TextUri": _uri("1", "result.json"),
                    "TextConfidenceUri": "s3://elsewhere/conf.json",
                    "RawTextUri": "s3://elsewhere/raw.json",
                }
            ]
        )

        page = service.get_document(KEY).pages["1"]

        assert page.text_confidence_uri == "s3://elsewhere/conf.json"
        assert page.raw_text_uri == "s3://elsewhere/raw.json"


class _Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, **message):
        self.sent.append(message)
        return {"MessageId": "m-1"}


class TestReprocessFromAssessment:
    """The path the defect was seen on: Reprocess, Start at Assessment."""

    def _reprocess(self, service, monkeypatch):
        monkeypatch.setattr(
            reprocess_module, "delete_current_output_objects", lambda *a, **k: 0
        )
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 1, "Metadata": {}}
        queue = _Queue()
        # No working bucket, so the message body carries the document itself.
        (outcome,) = ReprocessService(
            document_service=service,
            sqs_client=queue,
            queue_url="https://sqs.us-east-1.amazonaws.com/1/q",
            s3=s3,
            input_bucket="test-in",
            output_bucket=OUT,
        ).reprocess([KEY], start="assessment")
        assert outcome.status == QUEUED, outcome
        return json.loads(queue.sent[0]["MessageBody"])["pages"]

    def test_the_queued_document_keeps_each_pages_ocr_confidence(
        self, service, monkeypatch
    ):
        _store(service, _document([_ocr_page("1"), _ocr_page("2")]))

        pages = self._reprocess(service, monkeypatch)

        for page_id in ("1", "2"):
            assert pages[page_id]["text_confidence_uri"] == _uri(
                page_id, "textConfidence.json"
            )
            assert pages[page_id]["raw_text_uri"] == _uri(page_id, "rawText.json")

    def test_a_document_processed_before_the_fix_reprocesses_with_it_too(
        self, service, monkeypatch
    ):
        _store(service, _document([_ocr_page("1")]))
        # Strip what the fixed writer added, leaving the row older code wrote.
        item = _table().get_item(Key={"PK": f"doc#{KEY}", "SK": "none"})["Item"]
        for page in item["Pages"]:
            page.pop("TextConfidenceUri")
            page.pop("RawTextUri")
        _table().put_item(Item=item)

        pages = self._reprocess(service, monkeypatch)

        assert pages["1"]["text_confidence_uri"] == _uri("1", "textConfidence.json")
        assert pages["1"]["raw_text_uri"] == _uri("1", "rawText.json")
