# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Classification must report a page it could not classify (#1006's defect class).

``ClassificationService`` produced no ``ProcessingIssue`` of any kind. A page whose
classification failed after retries became ``unclassified`` with the reason in
``DocumentClassification.metadata["error"]``; a page whose predicted class was out
of vocabulary after every retry was coerced to ``invalidClassFallback`` with the
reason in ``metadata["validation_error"]``. Neither reached a user:

* that ``metadata`` dict is attached to the ``Page`` with ``setattr``, and ``Page``
  has no ``metadata`` field — so it is absent from ``Document.to_dict`` and never
  survives the Step Functions hop or reaches DynamoDB;
* the matching ``document.errors`` line is read only by
  ``processresults_function``'s ``Status.FAILED`` branch, which a document that
  completes never enters.

So the document finished green with a page whose class was a fallback nobody chose,
and the section built from it took extraction's empty-schema route and (after
#1006's carve-out) was silent there too. The issue is now recorded on the section
that contains the page, which is where ``ProcessingIssues`` is exposed in the API
schema and rendered in the UI.

Severity is what keeps this usable rather than ignored: a classification that
failed gets an error, while a class coerced to the configured fallback — where a
page the model cannot place ends up, so the commonest of the three — gets a
warning, because a class *was* assigned and an error indicator on most documents is
one nobody reads.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from idp_common.classification.models import DocumentClassification, PageClassification
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page, ProcessingIssue, Status

_CONFIG = {
    "classes": [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "invoice",
            "x-aws-idp-document-type": "invoice",
            "type": "object",
            "description": "An invoice",
            "properties": {"total": {"type": "string"}},
        },
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "receipt",
            "x-aws-idp-document-type": "receipt",
            "type": "object",
            "description": "A receipt",
            "properties": {"total": {"type": "string"}},
        },
    ],
    "classification": {
        "model": "us.amazon.nova-lite-v1:0",
        "system_prompt": "classify",
        "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
        "classificationMethod": "multimodalPageLevelClassification",
    },
}


@pytest.fixture
def service() -> ClassificationService:
    with patch("boto3.Session"):
        return ClassificationService(region="us-west-2", config=_CONFIG)


def _document(page_count: int = 2) -> Document:
    doc = Document(id="doc.pdf", input_key="doc.pdf", status=Status.CLASSIFYING)
    for i in range(1, page_count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i), image_uri=f"s3://bucket/doc.pdf/pages/{i}/image.jpg"
        )
    return doc


def _page(page_id: str, doc_type: str, **metadata) -> PageClassification:
    return PageClassification(
        page_id=page_id,
        classification=DocumentClassification(doc_type=doc_type, metadata=metadata),
    )


def _issues(document: Document, code: str) -> list[ProcessingIssue]:
    return [
        issue
        for section in document.sections
        for issue in (section.processing_issues or [])
        if issue.code == code
    ]


@pytest.mark.unit
class TestAPageThatCouldNotBeClassifiedIsReported:
    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_classification_failure_is_an_error_on_the_section(
        self, mock_classify_page, service
    ):
        """Retries exhausted / a non-retryable backend error. The page has no class,
        so nothing could be extracted from it."""
        mock_classify_page.side_effect = [
            _page("1", "invoice"),
            _page(
                "2",
                "unclassified",
                error="Max retries exceeded for SageMaker classification",
                unclassified_reason="failed",
            ),
        ]

        result = service.classify_document(_document())

        issues = _issues(result, "classification_failed")
        assert len(issues) == 1
        issue = issues[0]
        assert issue.stage == "classification"
        assert issue.severity == "error"
        assert "2" in issue.details["page_ids"]
        assert "Max retries exceeded" in issue.root_cause
        # The section it is attached to is the one holding the page.
        section = next(s for s in result.sections if s.section_id == issue.section_id)
        assert "2" in section.page_ids

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_a_page_with_no_content_is_a_warning(self, mock_classify_page, service):
        """Neither usable OCR text nor a loadable page image, which may mean an
        empty page or missing page artifacts. Warning rather than error because the
        message cannot tell those apart — and because an error indicator that
        appears on most documents is one nobody reads, the same argument that keeps
        the confidence alarm usable."""
        mock_classify_page.side_effect = [
            _page("1", "invoice"),
            _page(
                "2",
                "unclassified",
                error="No content available for classification",
                unclassified_reason="no_content",
            ),
        ]

        result = service.classify_document(_document())

        issues = _issues(result, "classification_page_no_content")
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert not _issues(result, "classification_failed")

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_class_coerced_to_the_fallback_is_a_warning(
        self, mock_classify_page, service
    ):
        """The stored class is not the model's answer, and extraction ran against
        the fallback's schema. Worth saying; not a failure."""
        mock_classify_page.side_effect = [
            _page(
                "1",
                "unclassified",
                validation_error=(
                    "Model returned invalid class 'bank_statement' after 3 "
                    "attempt(s); assigned fallback 'unclassified'."
                ),
            ),
        ]

        result = service.classify_document(_document(page_count=1))

        issues = _issues(result, "classification_invalid_class_fallback")
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "bank_statement" in issues[0].root_cause

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_one_issue_per_section_not_per_page(self, mock_classify_page, service):
        """A 50-page section of unclassifiable pages must not produce 50 identical
        rows."""
        mock_classify_page.side_effect = [
            _page(
                str(i),
                "unclassified",
                error="No content available for classification",
                unclassified_reason="no_content",
            )
            for i in range(1, 4)
        ]

        result = service.classify_document(_document(page_count=3))

        issues = _issues(result, "classification_page_no_content")
        assert len(issues) == 1
        assert sorted(issues[0].details["page_ids"]) == ["1", "2", "3"]

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_a_healthy_document_records_nothing(self, mock_classify_page, service):
        """Every page classified, so there is nothing to report — the check must not
        badge ordinary documents."""
        mock_classify_page.side_effect = [
            _page("1", "invoice"),
            _page("2", "invoice"),
        ]

        result = service.classify_document(_document())

        assert not any(section.processing_issues for section in result.sections)

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_two_different_causes_on_one_section_both_survive(
        self, mock_classify_page, service
    ):
        """An unclassifiable page and a failed page in the same section are two
        different facts, so neither write may evict the other."""
        mock_classify_page.side_effect = [
            _page(
                "1",
                "unclassified",
                error="No content available for classification",
                unclassified_reason="no_content",
            ),
            _page(
                "2",
                "unclassified",
                error="ValidationException: model not available",
                unclassified_reason="failed",
            ),
        ]

        result = service.classify_document(_document())

        codes = {
            issue.code
            for section in result.sections
            for issue in (section.processing_issues or [])
        }
        assert codes == {"classification_page_no_content", "classification_failed"}


@pytest.mark.unit
class TestTheRootCauseIsBoundedAndCorrectlyAttributed:
    """Two pages failing the same way for different reasons, and a huge detail.

    The `root_cause` is the operator's only pointer, and it names the affected page
    ids — so attributing one page's reason to another is worse than saying nothing.
    Its text also comes from model output or a service error and rides to DynamoDB
    inside the section map, which has a 400 KB item ceiling that
    `serialize_processing_issues` does not guard.
    """

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_distinct_reasons_are_not_attributed_to_each_other(
        self, mock_classify_page, service
    ):
        mock_classify_page.side_effect = [
            _page(
                "1",
                "unclassified",
                error="AccessDeniedException: no model grant",
                unclassified_reason="failed",
            ),
            _page(
                "2",
                "unclassified",
                error="ThrottlingException: slow down",
                unclassified_reason="failed",
            ),
        ]

        result = service.classify_document(_document())

        issues = _issues(result, "classification_failed")
        assert len(issues) == 1
        root_cause = issues[0].root_cause
        assert "AccessDeniedException" in root_cause
        assert "ThrottlingException" in root_cause

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_one_reason_shared_by_both_pages_is_not_repeated(
        self, mock_classify_page, service
    ):
        """Distinct reasons, so the same message twice collapses to once."""
        mock_classify_page.side_effect = [
            _page(
                str(i),
                "unclassified",
                error="ThrottlingException: slow down",
                unclassified_reason="failed",
            )
            for i in (1, 2)
        ]

        result = service.classify_document(_document())

        root_cause = _issues(result, "classification_failed")[0].root_cause
        assert root_cause.count("ThrottlingException") == 1

    @patch("idp_common.classification.service.ClassificationService.classify_page")
    def test_a_huge_model_output_is_truncated(self, mock_classify_page, service):
        """A `validation_error` embeds the rejected model output, which on a parse
        failure is a whole line of raw generation."""
        mock_classify_page.side_effect = [
            _page("1", "unclassified", validation_error="x" * 20000),
            _page("2", "invoice"),
        ]

        result = service.classify_document(_document())

        root_cause = _issues(result, "classification_invalid_class_fallback")[
            0
        ].root_cause
        assert len(root_cause) < 1000
