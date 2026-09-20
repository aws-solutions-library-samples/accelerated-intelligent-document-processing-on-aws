# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A section that comes back without confidence scores must say so (#1006).

``AssessmentService.process_document_section`` has several paths that end without
any confidence for the section. Each of them used to append a line to
``document.errors`` and return, which is not a signal anyone can act on:
``processresults_function`` reads a section document's ``errors`` only inside its
``Status.FAILED`` branch, so on a document that completes the list is never read.
Nothing raised either, so the Assessment Lambda's ``except`` branch never ran, no
``ProcessingIssue`` reached the section's DynamoDB record and the
``AssessmentConfidenceUnavailable`` metric behind
``AssessmentConfidenceUnavailableAlarm`` (#996) was never published — the document
completed looking exactly like one that had been fully scored.

These tests pin the contract per path:

* **nothing to assess** — no ``extraction_result_uri``, no ``page_ids``, or an
  empty ``inference_result`` that extraction did **not** flag as deliberate —
  records an error-severity ``assessment_skipped_confidence_unavailable`` issue on
  the section and publishes the metric, then returns;
* **nothing to record on** — no document, no sections, or a ``section_id`` the
  document does not contain — raises, because there is no section to carry an
  issue and a caller asking for a section that is not there has a bug;
* the three deliberate silent paths — confidence disabled by configuration, an
  excluded section class, and a class with no attributes to extract (whose empty
  result extraction flags ``skipped_due_to_empty_attributes``) — stay silent, or
  the alarm would fire on healthy throughput.

Two later additions, both the same shape:

* **a section none of whose pages the document contains** was the one case where
  silence was actively misleading rather than merely missing. The page loop
  appended a ``document.errors`` line per absent page and carried on, so the
  confidence pass ran with ``document_text == ""`` and ``page_images == []`` and
  the section came back WITH a confidence number for every field, derived from
  nothing. It is now skipped like the others, and when only SOME pages are absent
  the pass still runs but records a warning;
* **a section whose class is absent from the configuration** is no longer covered
  by the attribute-less carve-out. Extraction distinguishes the two at the
  producer (``metadata.empty_schema_reason``), so a class renamed or deleted while
  documents were in flight is reported, while a blank page's ``unclassified``
  section stays silent here and is reported by the classification stage instead.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from idp_common.assessment.degradation import (
    CONFIDENCE_SKIPPED_CODE,
    CONFIDENCE_UNAVAILABLE_METRIC,
)
from idp_common.assessment.service import AssessmentService
from idp_common.empty_schema import (
    EMPTY_SCHEMA_CLASS_NOT_CONFIGURED,
    EMPTY_SCHEMA_REASON_KEY,
    EMPTY_SCHEMA_UNCLASSIFIED,
)
from idp_common.models import Document, Page, ProcessingIssue, Section, Status

_EXTRACTION_URI = "s3://output-bucket/doc.pdf/sections/1/result.json"


def _config(enabled: bool = True) -> dict:
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "description": "An invoice",
                "properties": {
                    "invoice_number": {
                        "type": "string",
                        "description": "The invoice number",
                    }
                },
            }
        ],
        "assessment": {
            "enabled": enabled,
            "model": "us.amazon.nova-lite-v1:0",
            "task_prompt": "{DOCUMENT_CLASS} {EXTRACTION_RESULTS} {DOCUMENT_TEXT}",
        },
    }


@pytest.fixture
def service() -> AssessmentService:
    return AssessmentService(region="us-west-2", config=_config())


def _document() -> Document:
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="input-bucket",
        output_bucket="output-bucket",
        status=Status.ASSESSING,
    )
    doc.pages["1"] = Page(
        page_id="1",
        image_uri="s3://input-bucket/doc.pdf/pages/1/image.jpg",
        parsed_text_uri="s3://input-bucket/doc.pdf/pages/1/parsed.txt",
    )
    doc.sections.append(
        Section(
            section_id="1",
            classification="invoice",
            page_ids=["1"],
            extraction_result_uri=_EXTRACTION_URI,
        )
    )
    return doc


def _skip_issues(document: Document) -> list[ProcessingIssue]:
    return [
        issue
        for issue in (document.sections[0].processing_issues or [])
        if issue.code == CONFIDENCE_SKIPPED_CODE
    ]


def _assert_signalled(document: Document, mock_put_metric, *, root_cause: str) -> None:
    """The section carries the issue AND the metric was published."""
    issues = _skip_issues(document)
    assert len(issues) == 1, document.sections[0].processing_issues
    issue = issues[0]
    assert issue.stage == "assessment"
    assert issue.severity == "error"
    assert issue.section_id == "1"
    assert "no confidence scores" in issue.message.lower()
    assert root_cause in issue.root_cause

    assert (CONFIDENCE_UNAVAILABLE_METRIC, 1) in [
        call.args[:2] for call in mock_put_metric.call_args_list
    ]


@pytest.mark.unit
class TestNothingToAssessIsRecorded:
    """Paths where the confidence pass cannot run, so the section is skipped."""

    @patch("idp_common.metrics.put_metric")
    def test_section_without_extraction_result_uri(self, mock_put_metric, service):
        """No extraction result was written, so there is nothing to score — but the
        section still ends up with no confidence, which is what must be reported."""
        document = _document()
        document.sections[0].extraction_result_uri = None

        result = service.process_document_section(document, "1")

        _assert_signalled(result, mock_put_metric, root_cause="No extraction result")

    @patch("idp_common.metrics.put_metric")
    def test_section_without_page_ids(self, mock_put_metric, service):
        """A section with no pages has no text or image to assess against. The
        extraction it already paid for is kept, so this degrades rather than
        failing the document — but it is recorded."""
        document = _document()
        document.sections[0].page_ids = []

        result = service.process_document_section(document, "1")

        _assert_signalled(result, mock_put_metric, root_cause="no page IDs")

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_empty_inference_result(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """The one path that returned in complete silence — not even a line in
        ``document.errors``.

        This is the **genuine** empty result: the class has a schema, extraction
        ran, and the model returned no fields anyway. The section's values have no
        confidence, so it is reported. Contrast
        ``TestDeliberateSilence.test_extraction_produced_no_fields_by_design``.
        """
        mock_get_json_content.return_value = {
            "document_class": {"type": "invoice"},
            "inference_result": {},
            "metadata": {"parsing_succeeded": True},
        }

        result = service.process_document_section(_document(), "1")

        _assert_signalled(result, mock_put_metric, root_cause="empty")

    @patch("idp_common.metrics.put_metric")
    def test_skip_preserves_issues_from_the_extraction_stage(
        self, mock_put_metric, service
    ):
        """The section write replaces the whole issue map in DynamoDB, so an issue
        extraction recorded on this section must survive being skipped."""
        document = _document()
        document.sections[0].extraction_result_uri = None
        document.sections[0].processing_issues = [
            ProcessingIssue(
                stage="extraction",
                severity="warning",
                code="extraction_incomplete",
                message="fewer rows than minItems",
            )
        ]

        result = service.process_document_section(document, "1")

        assert [i.code for i in result.sections[0].processing_issues] == [
            "extraction_incomplete",
            CONFIDENCE_SKIPPED_CODE,
        ]

    @patch("idp_common.metrics.put_metric", side_effect=RuntimeError("no cloudwatch"))
    def test_unreachable_cloudwatch_does_not_break_the_skip(
        self, mock_put_metric, service
    ):
        """The metric is best-effort: a CloudWatch failure must not turn a skipped
        section into a failed document."""
        document = _document()
        document.sections[0].extraction_result_uri = None

        result = service.process_document_section(document, "1")

        assert len(_skip_issues(result)) == 1
        assert result.status != Status.FAILED

    @patch("idp_common.metrics.put_metric")
    def test_assess_document_reports_the_skip_too(self, mock_put_metric, service):
        """The whole-document entry point must not have its own silent filter.

        ``assess_document`` used to skip a section with no extraction result
        itself, with only a log warning — so the same section was reported when
        assessed through ``process_document_section`` and not when assessed
        through here.
        """
        document = _document()
        document.sections[0].extraction_result_uri = None

        result = service.assess_document(document)

        _assert_signalled(result, mock_put_metric, root_cause="No extraction result")


@pytest.mark.unit
class TestNothingToRecordOnRaises:
    """Paths with no section to carry an issue: raise instead of returning."""

    @patch("idp_common.metrics.put_metric")
    def test_no_document(self, mock_put_metric, service):
        with pytest.raises(ValueError, match="No document provided"):
            service.process_document_section(None, "1")

    @patch("idp_common.metrics.put_metric")
    def test_document_with_no_sections(self, mock_put_metric, service):
        document = _document()
        document.sections = []

        with pytest.raises(ValueError, match="no sections to process"):
            service.process_document_section(document, "1")

    @patch("idp_common.metrics.put_metric")
    def test_section_id_not_in_document(self, mock_put_metric, service):
        with pytest.raises(ValueError, match="Section 999 not found"):
            service.process_document_section(_document(), "999")


@pytest.mark.unit
class TestDeliberateSilence:
    """Three paths must NOT report a missing confidence score.

    Each is an expected outcome for a section no extraction was attempted on,
    rather than a section that should have been scored and was not. All three
    occur on **healthy** documents, so reporting any of them would put a red
    indicator in the Sections panel on documents their owner considers normal and
    — because every report also publishes the metric — would breach the alarm's
    volume threshold on ordinary throughput, making it useless for its purpose.
    """

    @patch("idp_common.metrics.put_metric")
    def test_confidence_disabled_by_configuration(self, mock_put_metric):
        service = AssessmentService(region="us-west-2", config=_config(enabled=False))

        result = service.process_document_section(_document(), "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.metrics.put_metric")
    def test_excluded_section_class(self, mock_put_metric, service):
        """An excluded class (e.g. static instruction pages) never had extraction
        run, so there is no confidence to be missing."""
        document = _document()
        document.sections[0].excluded = True
        document.sections[0].exclusion_reason = "excluded_class"

        result = service.process_document_section(document, "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_extraction_produced_no_fields_by_design(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """A class with no attributes is an ordinary outcome, not a gap.

        ``ExtractionService`` skips the LLM for a class whose effective schema is
        empty and writes a stub flagged ``skipped_due_to_empty_attributes``. That
        is reached routinely, not only by a hand-authored attribute-less class:
        classification labels a blank page, a page whose classification errored
        after retries, and every page of a deployment with no document types
        configured as ``"unclassified"``, and no class of that name exists in
        config. A single cover sheet in an otherwise normal document lands here, so
        reporting it would mean an error-severity issue and a metric point per
        cover sheet — twelve such documents in fifteen minutes would page the
        on-call for a healthy fleet at the default threshold of ten.
        """
        mock_get_json_content.return_value = {
            "document_class": {"type": "unclassified"},
            "inference_result": {},
            "metadata": {
                "parsing_succeeded": True,
                "skipped_due_to_empty_attributes": True,
            },
        }

        result = service.process_document_section(_document(), "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_excluded_class_stub_read_from_s3(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """The excluded-class stub is recognised even when the section object has
        lost its ``excluded`` flag — a document reassessed under a configuration
        that no longer marks the class excluded reads the old stub."""
        mock_get_json_content.return_value = {
            "status": "skipped_excluded_class",
            "excluded": True,
            "exclusion_reason": "instruction_pages",
        }

        result = service.process_document_section(_document(), "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_unclassified_section_stays_silent_here(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """Classification determined no class, so the classification stage reports
        it. Reporting it from here as well would double the alarm's volume for the
        commonest benign case in the pipeline — a blank page — and point its
        ``root_cause`` at an Extraction step where nothing is wrong."""
        mock_get_json_content.return_value = {
            "document_class": {"type": "unclassified"},
            "inference_result": {},
            "metadata": {
                "parsing_succeeded": True,
                "skipped_due_to_empty_attributes": True,
                EMPTY_SCHEMA_REASON_KEY: EMPTY_SCHEMA_UNCLASSIFIED,
            },
        }

        result = service.process_document_section(_document(), "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_a_stub_written_before_the_reason_existed_stays_silent(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """Back-compatibility, and it is the safe default in both directions: a
        result file written by an earlier version carries the flag and no reason,
        and must keep reading back as the attribute-less case rather than becoming
        a fleet of new alarm points the moment old documents are reassessed."""
        mock_get_json_content.return_value = {
            "document_class": {"type": "cover_sheet"},
            "inference_result": {},
            "metadata": {
                "parsing_succeeded": True,
                "skipped_due_to_empty_attributes": True,
            },
        }

        result = service.process_document_section(_document(), "1")

        assert not _skip_issues(result)
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]


_BATCHED = {
    "assessment": {"invoice_number": {"confidence": 0.9, "confidence_reason": "clear"}},
    "alerts": [],
    "metering": {},
    "parsing_succeeded": True,
    "duration_seconds": 0.1,
    "split_stats": None,
}


def _extraction_data() -> dict:
    return {
        "document_class": {"type": "invoice"},
        "inference_result": {"invoice_number": "INV-1"},
        "metadata": {"parsing_succeeded": True},
    }


def _page_issues(document: Document) -> list[ProcessingIssue]:
    return [
        issue
        for issue in (document.sections[0].processing_issues or [])
        if issue.code == "assessment_pages_missing"
    ]


@pytest.mark.unit
class TestPagesMissingFromTheDocument:
    """A section whose ``page_ids`` name pages the document does not contain.

    The page loop logged each absence into ``document.errors`` and continued, so
    with EVERY page absent the confidence pass ran against no text and no image and
    still returned a score per field. That is worse than no confidence: a
    fabricated number is indistinguishable in the UI, in HITL routing and in the
    reporting lake from one the model derived from the page.

    This is malformed input, not an ordinary outcome — the reprocess resolver
    validates page ids against ``document.pages`` before saving a regrouping — so
    reporting it cannot reach the alarm's threshold on throughput.
    """

    @patch("idp_common.assessment.batching.assess_results_batched")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_every_page_missing_skips_the_pass_instead_of_scoring_nothing(
        self, mock_put_metric, mock_get_json, mock_write, mock_batched, service
    ):
        mock_get_json.return_value = _extraction_data()
        mock_batched.return_value = dict(_BATCHED)
        document = _document()
        document.sections[0].page_ids = ["7", "8"]

        result = service.process_document_section(document, "1")

        _assert_signalled(result, mock_put_metric, root_cause="None of the section")
        # The point of the fix: no confidence was computed, and nothing was written
        # back over the extraction result.
        assert not mock_batched.called
        assert not mock_write.called

    @patch("idp_common.assessment.batching.assess_results_batched")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.image.prepare_image")
    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_some_pages_missing_still_assesses_and_warns(
        self,
        mock_put_metric,
        mock_get_json,
        mock_get_text,
        mock_prepare_image,
        mock_write,
        mock_batched,
        service,
    ):
        """Partial evidence is not no evidence, so the pass runs and the section
        does get confidence scores. The values that live on the absent pages were
        scored without them, which is a warning — and publishes NO metric, because
        the question the metric answers (is this section coming back without
        confidence?) is answered no."""
        mock_get_json.return_value = _extraction_data()
        mock_get_text.return_value = "INVOICE 1"
        mock_prepare_image.return_value = b"image-bytes"
        mock_batched.return_value = dict(_BATCHED)
        document = _document()
        document.sections[0].page_ids = ["1", "9"]

        result = service.process_document_section(document, "1")

        assert mock_batched.called
        assert not _skip_issues(result)
        warnings = _page_issues(result)
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert warnings[0].details["missing_page_ids"] == ["9"]
        assert CONFIDENCE_UNAVAILABLE_METRIC not in [
            call.args[0] for call in mock_put_metric.call_args_list
        ]

    @patch("idp_common.assessment.batching.assess_results_batched")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.image.prepare_image")
    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_all_pages_present_records_no_warning(
        self,
        mock_put_metric,
        mock_get_json,
        mock_get_text,
        mock_prepare_image,
        mock_write,
        mock_batched,
        service,
    ):
        """The noise guard: an ordinary section must not be badged."""
        mock_get_json.return_value = _extraction_data()
        mock_get_text.return_value = "INVOICE 1"
        mock_prepare_image.return_value = b"image-bytes"
        mock_batched.return_value = dict(_BATCHED)

        result = service.process_document_section(_document(), "1")

        assert not _page_issues(result)
        assert not _skip_issues(result)


@pytest.mark.unit
class TestClassAbsentFromConfigurationIsReported:
    """The carve-out's blind spot, fixed at the producer.

    ``_get_class_schema`` returns ``{}`` both for a class configured with no
    attributes and for a label the configuration does not contain, and extraction
    routed both to one stub flagged ``skipped_due_to_empty_attributes``. Keying the
    carve-out on that one flag therefore also silenced a class **renamed or deleted
    while documents were in flight**, and an old document reprocessed under a
    configuration that no longer defines its class: no fields, no confidence, no
    signal, document green.

    The stub now says which, so only the deliberate cases stay silent.
    """

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_named_class_not_in_configuration(
        self, mock_put_metric, mock_get_json_content, service
    ):
        mock_get_json_content.return_value = {
            "document_class": {"type": "bank_statement"},
            "inference_result": {},
            "metadata": {
                "parsing_succeeded": True,
                "skipped_due_to_empty_attributes": True,
                EMPTY_SCHEMA_REASON_KEY: EMPTY_SCHEMA_CLASS_NOT_CONFIGURED,
            },
        }
        document = _document()
        document.sections[0].classification = "bank_statement"

        result = service.process_document_section(document, "1")

        _assert_signalled(
            result, mock_put_metric, root_cause="not in the configuration"
        )

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_the_remedy_names_configuration_not_the_confidence_model(
        self, mock_put_metric, mock_get_json_content, service
    ):
        """A skip's ``root_cause`` is the operator's only pointer, and the generic
        empty-result text ("extraction returned no fields") would send them to
        inspect a model call that never happened."""
        mock_get_json_content.return_value = {
            "document_class": {"type": "bank_statement"},
            "inference_result": {},
            "metadata": {
                "skipped_due_to_empty_attributes": True,
                EMPTY_SCHEMA_REASON_KEY: EMPTY_SCHEMA_CLASS_NOT_CONFIGURED,
            },
        }
        document = _document()
        document.sections[0].classification = "bank_statement"

        result = service.process_document_section(document, "1")

        root_cause = _skip_issues(result)[0].root_cause
        assert "reclassify" in root_cause
        assert "extraction_class_not_configured" in root_cause
