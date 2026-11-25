# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A content-filter block fails the section it happened in, not the document (#2038).

Bedrock answers a model reply its output filter withholds with a
``ValidationException`` carrying ``CONTENT_FILTER_BLOCK_MESSAGE``. The retry
decorators retry it; once they give up it reached
``ExtractionService.process_document_section``, which appended it to
``document.errors`` and re-raised, so the extraction Lambda failed, the
ProcessSections Map (which has no Catch) failed, and every other section's
completed extraction was discarded with the document.

These tests pin the replacement behaviour end to end without AWS:

1. the matcher recognises the block raw and wrapped, and nothing else;
2. the service records an error-severity ``extraction_failed`` issue on that
   section, writes an empty result, and RETURNS instead of raising;
3. the collate step (``processresults_function``) given that section beside a
   normal one completes rather than failing the document, and persists the issue;
4. the confidence pass reads the stub the service actually wrote and names the
   block rather than "extraction returned no fields".

The error code is derived from the botocore service model for ``Converse``; the
message is the constant the retry decorators match on.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import botocore.exceptions
import botocore.session
import pytest

from idp_common.assessment.degradation import CONFIDENCE_SKIPPED_CODE
from idp_common.assessment.service import AssessmentService
from idp_common.config.models import IDPConfig
from idp_common.extraction.failure import EXTRACTION_FAILED_CODE
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, ProcessingIssue, Section, Status
from idp_common.utils.bedrock_utils import (
    CONTENT_FILTER_BLOCK_MESSAGE,
    is_content_filter_block,
)
from tests.unit._handler_loading import load_with_xray_stubbed


def _converse_error_codes() -> list[str]:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return [shape.name for shape in model.operation_model("Converse").error_shapes]


def _client_error(code: str, message: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse"
    )


def _block() -> botocore.exceptions.ClientError:
    codes = _converse_error_codes()
    assert "ValidationException" in codes, codes
    return _client_error("ValidationException", CONTENT_FILTER_BLOCK_MESSAGE)


_INVOICE_CLASS = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "invoice",
    "x-aws-idp-document-type": "invoice",
    "type": "object",
    "description": "An invoice",
    "properties": {
        "invoice_number": {"type": "string", "description": "The invoice number"}
    },
}


def _document() -> Document:
    """Two sections, so a test about WHICH section was failed can fail."""
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="input-bucket",
        output_bucket="output-bucket",
        status=Status.EXTRACTING,
        num_pages=2,
    )
    for n in ("1", "2"):
        doc.pages[n] = Page(
            page_id=n,
            image_uri=f"s3://input-bucket/doc.pdf/pages/{n}/image.jpg",
            parsed_text_uri=f"s3://input-bucket/doc.pdf/pages/{n}/parsed.txt",
        )
        doc.sections.append(
            Section(section_id=n, classification="invoice", page_ids=[n])
        )
    return doc


def _extract(error: BaseException, *, stale_issue: bool = False):
    """Run the REAL ``process_document_section`` for section 2 with the model call
    raising ``error``. Returns (document, the dict written to S3 or None)."""
    service = ExtractionService(config=IDPConfig(classes=[_INVOICE_CLASS]))
    document = _document()
    target = document.sections[1]
    other = ProcessingIssue(
        stage="classification",
        severity="warning",
        code="classification_page_assigned_to_neighbour",
        message="kept",
        section_id="2",
    )
    target.processing_issues = [other]
    if stale_issue:
        target.processing_issues.append(
            ProcessingIssue(
                stage="extraction",
                severity="info",
                code="extraction_sparse",
                message="from an earlier run",
                section_id="2",
            )
        )
    with (
        patch.object(
            ExtractionService,
            "_prepare_section_context",
            return_value=([{"text": "page text"}], "system prompt"),
        ),
        patch.object(ExtractionService, "_invoke_extraction_model", side_effect=error),
        patch("idp_common.s3.write_content") as write,
    ):
        result = service.process_document_section(document=document, section_id="2")
    written = write.call_args.args[0] if write.call_args else None
    return result, written


@pytest.mark.unit
class TestMatcher:
    def test_raw_block(self):
        assert is_content_filter_block(_block())

    def test_block_raised_inside_a_converse_stream(self):
        """botocore names an in-stream error by its event-stream MEMBER, so the
        code is ``validationException``; Strands streams by default."""
        model = botocore.session.get_session().get_service_model("bedrock-runtime")
        stream = model.operation_model("ConverseStream").output_shape.members["stream"]
        member = next(
            name
            for name, shape in stream.members.items()
            if shape.name == "ValidationException"
        )
        assert member != "ValidationException"
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": member, "Message": CONTENT_FILTER_BLOCK_MESSAGE}},
            "ConverseStream",
        )
        try:
            raise RuntimeError("agent run failed") from error
        except RuntimeError as wrapped:
            assert is_content_filter_block(wrapped)

    def test_block_wrapped_the_way_strands_wraps_it(self):
        from strands.types.exceptions import EventLoopException

        try:
            try:
                raise _block()
            except botocore.exceptions.ClientError as inner:
                raise EventLoopException(inner) from inner
        except EventLoopException as wrapped:
            assert is_content_filter_block(wrapped)

    def test_a_wrapper_whose_text_omits_the_message_is_looked_through(self):
        """Strands' wrapper happens to repeat the inner text; a wrapper that
        does not must still be judged by the block it was raised from."""
        try:
            try:
                raise _block()
            except botocore.exceptions.ClientError as inner:
                raise RuntimeError("agent run failed") from inner
        except RuntimeError as wrapped:
            assert CONTENT_FILTER_BLOCK_MESSAGE not in str(wrapped)
            assert is_content_filter_block(wrapped)

    def test_a_context_link_is_not_followed(self):
        """Only ``raise ... from`` counts, as everywhere else in bedrock_utils."""
        try:
            try:
                raise _block()
            except botocore.exceptions.ClientError:
                raise RuntimeError("unrelated failure in a handler")  # noqa: B904
        except RuntimeError as later:
            assert later.__context__ is not None
            assert not is_content_filter_block(later)

    def test_another_validation_exception_is_not_a_block(self):
        assert not is_content_filter_block(
            _client_error(
                "ValidationException", "Input is too long for requested model"
            )
        )

    @pytest.mark.parametrize(
        "code",
        [c for c in _converse_error_codes() if c != "ValidationException"],
    )
    def test_a_definite_other_code_is_not_a_block_whatever_it_says(self, code):
        assert not is_content_filter_block(
            _client_error(code, CONTENT_FILTER_BLOCK_MESSAGE)
        )

    def test_the_other_codes_were_derived(self):
        """An empty derivation would collect the test above as a skip."""
        assert len([c for c in _converse_error_codes() if c != "ValidationException"])


@pytest.mark.unit
class TestTheSectionFailsAndTheServiceReturns:
    def test_the_block_is_recorded_on_the_section_and_not_raised(self):
        document, written = _extract(_block())

        section = document.sections[1]
        failed = [
            i for i in section.processing_issues if i.code == EXTRACTION_FAILED_CODE
        ]
        assert len(failed) == 1
        assert failed[0].severity == "error"
        assert failed[0].stage == "extraction"
        assert failed[0].section_id == "2"
        assert "content filter" in failed[0].message
        assert CONTENT_FILTER_BLOCK_MESSAGE in failed[0].root_cause
        # Another stage's issue survives.
        assert "classification_page_assigned_to_neighbour" in [
            i.code for i in section.processing_issues
        ]
        # Nothing for the collate step to fail the document on.
        assert document.errors == []
        assert document.status != Status.FAILED
        # The empty result was written and the section points at it.
        assert written is not None
        assert written["inference_result"] == {}
        assert written["metadata"]["content_filtered"] is True
        assert section.extraction_result_uri == (
            "s3://output-bucket/doc.pdf/sections/2/result.json"
        )
        assert [i["code"] for i in written["metadata"]["processing_issues"]] == [
            i.code for i in section.processing_issues
        ]
        # The other section was not touched.
        assert not document.sections[0].processing_issues
        assert document.sections[0].extraction_result_uri is None

    def test_a_stale_extraction_issue_from_an_earlier_run_is_replaced(self):
        document, _ = _extract(_block(), stale_issue=True)
        codes = [i.code for i in document.sections[1].processing_issues]
        assert "extraction_sparse" not in codes
        assert EXTRACTION_FAILED_CODE in codes

    def test_any_other_validation_exception_still_raises(self):
        """The control: only the content-filter block changes outcome."""
        error = _client_error("ValidationException", "Malformed input request")
        with pytest.raises(botocore.exceptions.ClientError):
            _extract(error)


_PROCESSRESULTS = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/processresults_function/index.py",
)


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.mark.unit
def test_the_document_completes_with_the_failed_section_flagged(monkeypatch):
    """The collate step is what failed the document; it must now complete it."""
    processresults = load_with_xray_stubbed(
        "processresults_index_2038", _PROCESSRESULTS
    )
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(processresults, "get_config", lambda **kw: IDPConfig())
    monkeypatch.setattr(processresults, "create_metadata_file", lambda *a, **kw: None)
    doc_service = MagicMock()
    doc_service.get_document.return_value = None
    writes = []
    doc_service.update_document.side_effect = lambda d: (
        writes.append(
            {s.section_id: [i.code for i in s.processing_issues] for s in d.sections}
        ),
        d,
    )[1]
    monkeypatch.setattr(
        processresults, "create_document_service", lambda *a, **kw: doc_service
    )

    blocked, _ = _extract(_block())
    healthy = _document()
    healthy.sections = [healthy.sections[0]]
    classified = _document()
    classified.status = Status.CLASSIFYING

    response = processresults.handler(
        {
            "ClassificationResult": {"document": classified.to_dict()},
            "ExtractionResults": [
                {"section_id": "1", "document": healthy.to_dict()},
                {"section_id": "2", "document": blocked.to_dict()},
            ],
            "execution_arn": "arn:aws:states:us-east-1:1:execution:sm:exec",
        },
        _Context(),
    )

    assert "document" in response
    assert EXTRACTION_FAILED_CODE in writes[-1]["2"]
    assert writes[-1]["1"] == []


@pytest.mark.unit
@patch("idp_common.metrics.put_metric")
def test_the_confidence_pass_names_the_block(mock_put_metric):
    """Reads the stub the extraction service wrote, not a hand-written one."""
    _, written = _extract(_block())
    service = AssessmentService(
        region="us-west-2",
        config={
            "classes": [_INVOICE_CLASS],
            "assessment": {
                "enabled": True,
                "model": "us.amazon.nova-lite-v1:0",
                "task_prompt": "{DOCUMENT_CLASS} {EXTRACTION_RESULTS} {DOCUMENT_TEXT}",
            },
        },
    )
    document = _document()
    document.status = Status.ASSESSING
    document.sections[
        1
    ].extraction_result_uri = "s3://output-bucket/doc.pdf/sections/2/result.json"
    with patch("idp_common.s3.get_json_content", return_value=written):
        result = service.process_document_section(document, "2")

    skipped = [
        i
        for i in result.sections[1].processing_issues or []
        if i.code == CONFIDENCE_SKIPPED_CODE
    ]
    assert len(skipped) == 1
    assert "content filter" in skipped[0].root_cause
