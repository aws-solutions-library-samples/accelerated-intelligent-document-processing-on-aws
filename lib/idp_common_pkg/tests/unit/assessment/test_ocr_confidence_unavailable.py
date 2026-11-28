# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A confidence pass sent without OCR confidence says so on the section (#2258).

The shipped confidence prompt carries ``{OCR_TEXT_CONFIDENCE}`` and tells the model
to account for OCR confidence when it is provided. When a page's
``textConfidence.json`` cannot be read and cannot be regenerated from the raw OCR
output, ``_get_text_confidence_data`` returns an empty string and the prompt goes
out with an empty block. That was the state of every page of a reprocessed
document before the tracking row stored the confidence location, and the section
came back with every populated field at 1.0 and nothing to show why. These tests
pin the warning that now records it, and the cases where it must stay silent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from idp_common.assessment.service import AssessmentService
from idp_common.models import Document, Page, ProcessingIssue, Section, Status

pytestmark = pytest.mark.unit

_EXTRACTION_URI = "s3://out/doc.pdf/sections/1/result.json"
_CODE = AssessmentService.OCR_CONFIDENCE_UNAVAILABLE_CODE

_BATCHED = {
    "assessment": {"invoice_number": {"confidence": 1.0}},
    "alerts": [],
    "metering": {},
    "parsing_succeeded": True,
    "duration_seconds": 0.1,
    "split_stats": None,
}


def _config(task_prompt: str) -> dict:
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string", "description": "Number"}
                },
            }
        ],
        "assessment": {
            "enabled": True,
            "model": "us.amazon.nova-lite-v1:0",
            "task_prompt": task_prompt,
        },
    }


def _uri(page_id: str, filename: str) -> str:
    return f"s3://out/doc.pdf/pages/{page_id}/{filename}"


def _document(page_ids=("1", "2")) -> Document:
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.ASSESSING,
    )
    for page_id in page_ids:
        doc.pages[page_id] = Page(
            page_id=page_id,
            image_uri=_uri(page_id, "image.jpg"),
            raw_text_uri=_uri(page_id, "rawText.json"),
            parsed_text_uri=_uri(page_id, "result.json"),
            text_confidence_uri=_uri(page_id, "textConfidence.json"),
        )
    doc.sections.append(
        Section(
            section_id="1",
            classification="invoice",
            page_ids=list(page_ids),
            extraction_result_uri=_EXTRACTION_URI,
        )
    )
    return doc


def _json_reader(readable_confidence_pages):
    """``s3.get_json_content`` over the extraction result and the OCR outputs.

    Text confidence (and raw OCR) is readable only for the named pages, so for the
    others both the file and the regeneration fallback fail.
    """

    def read(uri):
        if uri == _EXTRACTION_URI:
            return {
                "document_class": {"type": "invoice"},
                "inference_result": {"invoice_number": "INV-1"},
                "metadata": {"parsing_succeeded": True},
            }
        for page_id in readable_confidence_pages:
            if uri == _uri(page_id, "textConfidence.json"):
                return {"text": f"| INV-1 | 99.{page_id} |"}
        raise FileNotFoundError(uri)

    return read


def _issues(document: Document) -> list[ProcessingIssue]:
    return [
        issue
        for issue in (document.sections[0].processing_issues or [])
        if issue.code == _CODE
    ]


def _run(service, document, readable, mock_get_json, mock_batched):
    mock_get_json.side_effect = _json_reader(readable)
    mock_batched.return_value = dict(_BATCHED)
    return service.process_document_section(document, "1")


_WITH_OCR = "{DOCUMENT_CLASS} {EXTRACTION_RESULTS} {OCR_TEXT_CONFIDENCE}"


@patch("idp_common.assessment.batching.assess_results_batched")
@patch("idp_common.s3.write_content")
@patch("idp_common.image.prepare_image", return_value=b"image")
@patch("idp_common.s3.get_text_content", return_value="INVOICE INV-1")
@patch("idp_common.s3.get_json_content")
@patch("idp_common.metrics.put_metric")
class TestTheWarning:
    def test_a_page_without_ocr_confidence_is_reported(
        self, _metric, mock_get_json, _text, _image, _write, mock_batched
    ):
        service = AssessmentService(region="us-west-2", config=_config(_WITH_OCR))

        result = _run(service, _document(), ["1"], mock_get_json, mock_batched)

        (issue,) = _issues(result)
        assert issue.stage == "assessment"
        assert issue.severity == "warning"
        assert issue.section_id == "1"
        assert issue.details == {"page_ids": ["2"]}
        assert "1 of this section's 2 pages" in issue.message
        # The pass still ran, with the confidence the readable page supplied.
        sent = mock_batched.call_args.kwargs["ocr_text_confidence"]
        assert "99.1" in sent

    def test_every_page_supplying_it_records_nothing(
        self, _metric, mock_get_json, _text, _image, _write, mock_batched
    ):
        service = AssessmentService(region="us-west-2", config=_config(_WITH_OCR))

        result = _run(service, _document(), ["1", "2"], mock_get_json, mock_batched)

        assert _issues(result) == []

    def test_a_prompt_that_never_asks_for_it_records_nothing(
        self, _metric, mock_get_json, _text, _image, _write, mock_batched
    ):
        service = AssessmentService(
            region="us-west-2",
            config=_config("{DOCUMENT_CLASS} {EXTRACTION_RESULTS} {DOCUMENT_TEXT}"),
        )

        result = _run(service, _document(), [], mock_get_json, mock_batched)

        assert _issues(result) == []

    def test_a_page_with_no_ocr_output_at_all_records_nothing(
        self, _metric, mock_get_json, _text, _image, _write, mock_batched
    ):
        # Nothing was expected for a page the OCR step gave no output locations.
        service = AssessmentService(region="us-west-2", config=_config(_WITH_OCR))
        document = _document(("1",))
        document.pages["1"].raw_text_uri = None
        document.pages["1"].text_confidence_uri = None

        result = _run(service, document, [], mock_get_json, mock_batched)

        assert _issues(result) == []

    def test_the_warning_keeps_the_extraction_stages_issues(
        self, _metric, mock_get_json, _text, _image, _write, mock_batched
    ):
        service = AssessmentService(region="us-west-2", config=_config(_WITH_OCR))
        document = _document()
        extraction_issue = ProcessingIssue(
            stage="extraction",
            severity="warning",
            code="extraction_sparse",
            message="sparse",
            section_id="1",
        )
        document.sections[0].processing_issues = [extraction_issue]

        result = _run(service, document, ["1"], mock_get_json, mock_batched)

        codes = [i.code for i in result.sections[0].processing_issues]
        assert "extraction_sparse" in codes
        assert _CODE in codes
