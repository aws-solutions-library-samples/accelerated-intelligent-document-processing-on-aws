# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a failed extraction section records, and what it must not erase (#1049).

``idp_common.extraction.failure`` is the shared body behind the persist-then-
re-raise at both extraction Lambda boundaries. These tests cover it directly; the
boundaries themselves are covered in
``tests/unit/lambdas/test_extraction_function_failure_persistence.py``.

The assertion that matters most here is the *preservation* one. A successful
extraction run REPLACES the section's extraction-stage issues (``_save_results``
does exactly that, so a re-run cannot leave a stale warning behind). Doing the
same on the failure path would delete the ``extraction_rows_below_ocr_estimate``
issue that ``ExtractionOutputIncomplete`` had just persisted — i.e. it would
delete the diagnosis this module exists to surface.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from idp_common.extraction.failure import (
    EXTRACTION_FAILED_CODE,
    persist_section_after_extraction_failure,
    record_section_extraction_failure,
)
from idp_common.extraction.service import ExtractionOutputIncomplete
from idp_common.models import Document, ProcessingIssue, Section, Status
from idp_common.utils.transient_errors import is_transient_error

ROW_SHORTFALL_CODE = "extraction_rows_below_ocr_estimate"


def _document(issues: list[ProcessingIssue] | None = None) -> Document:
    return Document(
        id="doc.pdf",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.EXTRACTING,
        sections=[
            Section(
                section_id="1",
                classification="bank-statement",
                page_ids=["1"],
                processing_issues=issues or [],
            )
        ],
    )


def _row_shortfall_issue() -> ProcessingIssue:
    """The issue #1044 persists before raising, at the severity ``fail`` gives it."""
    return ProcessingIssue(
        stage="extraction",
        severity="error",
        code=ROW_SHORTFALL_CODE,
        message=(
            "Extracted 43 rows for transactions but the section's OCR evidences "
            "about 1200."
        ),
        section_id="1",
        details={"extracted_rows": 43, "ocr_estimated_rows": 1200},
    )


@pytest.mark.unit
def test_the_failure_is_recorded_as_an_error_severity_extraction_issue():
    document = _document()
    issue = record_section_extraction_failure(
        document, "1", ExtractionOutputIncomplete("materially incomplete: 43 of 1200")
    )

    assert issue is not None
    assert issue.code == EXTRACTION_FAILED_CODE
    assert issue.severity == "error"
    assert issue.stage == "extraction"
    assert issue.section_id == "1"
    # The exception's own message is already a full explanation with a remedy, so
    # root_cause carries it verbatim rather than paraphrasing it.
    assert issue.root_cause == (
        "ExtractionOutputIncomplete: materially incomplete: 43 of 1200"
    )
    assert document.sections[0].processing_issues == [issue]


@pytest.mark.unit
def test_a_persisted_row_shortfall_diagnosis_is_not_erased():
    """The #1032 case. Both issues must reach the section record.

    The shortfall issue is what names the rows lost and the remedy; the
    ``extraction_failed`` issue is what says the section FAILED rather than
    completing with a warning, which the shortfall issue alone cannot express —
    it is written at ``warning`` severity under ``row_shortfall_action: warn``
    and at ``error`` under ``fail``.
    """
    shortfall = _row_shortfall_issue()
    document = _document([shortfall])

    record_section_extraction_failure(
        document, "1", ExtractionOutputIncomplete("materially incomplete")
    )

    codes = [pi.code for pi in document.sections[0].processing_issues]
    assert codes == [ROW_SHORTFALL_CODE, EXTRACTION_FAILED_CODE]
    # The same object, not a rebuilt copy: its details are what the UI reads.
    assert document.sections[0].processing_issues[0] is shortfall


@pytest.mark.unit
def test_issues_from_other_stages_survive():
    """The section write replaces the whole map in DynamoDB, so an OCR or
    classification issue dropped here is deleted from the record."""
    ocr_issue = ProcessingIssue(
        stage="ocr", severity="warning", code="ocr_low_confidence", message="blurry"
    )
    document = _document([ocr_issue])

    record_section_extraction_failure(document, "1", ValueError("boom"))

    assert [pi.code for pi in document.sections[0].processing_issues] == [
        "ocr_low_confidence",
        EXTRACTION_FAILED_CODE,
    ]


@pytest.mark.unit
def test_repeated_attempts_do_not_accumulate_one_issue_each():
    """A transient failure is retried by the state machine; if each attempt
    appended, the Sections panel would show a growing pile of identical rows."""
    document = _document()
    record_section_extraction_failure(document, "1", ValueError("first"))
    record_section_extraction_failure(document, "1", ValueError("second"))

    issues = document.sections[0].processing_issues
    assert [pi.code for pi in issues] == [EXTRACTION_FAILED_CODE]
    assert issues[0].root_cause == "ValueError: second"


@pytest.mark.unit
def test_a_missing_section_records_nothing_and_writes_nothing():
    """There is no section to carry the issue, so there is nothing to persist —
    and writing an arbitrary index would corrupt a different section's record."""
    document = _document()
    service = MagicMock()

    assert record_section_extraction_failure(document, "99", ValueError("boom")) is None
    assert (
        persist_section_after_extraction_failure(
            document_service=service,
            document=document,
            section_id="99",
            section_index=0,
            error=ValueError("boom"),
        )
        is None
    )
    assert not service.update_document_section.called


@pytest.mark.unit
def test_the_write_targets_the_section_by_index_and_carries_the_issue():
    document = _document([_row_shortfall_issue()])
    service = MagicMock()

    persist_section_after_extraction_failure(
        document_service=service,
        document=document,
        section_id="1",
        section_index=3,
        error=ExtractionOutputIncomplete("materially incomplete"),
    )

    kwargs = service.update_document_section.call_args.kwargs
    assert kwargs["document_id"] == "doc.pdf"
    assert kwargs["section_index"] == 3
    assert [pi.code for pi in kwargs["section"].processing_issues] == [
        ROW_SHORTFALL_CODE,
        EXTRACTION_FAILED_CODE,
    ]


@pytest.mark.unit
def test_a_transient_error_records_nothing():
    """A retry is coming, so marking the section would show it failed for the length
    of the ladder and then clear itself. ``is_transient_error`` is the same predicate
    the handler uses to decide whether to re-raise under the name the state machine
    retries, so this is exactly "a retry is coming"."""
    document = _document()
    service = MagicMock()

    error = botocore.exceptions.ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    assert is_transient_error(error), "fixture precondition"

    assert (
        persist_section_after_extraction_failure(
            document_service=service,
            document=document,
            section_id="1",
            section_index=0,
            error=error,
        )
        is None
    )
    assert not service.update_document_section.called
    assert document.sections[0].processing_issues == []


@pytest.mark.unit
def test_a_deterministic_error_is_recorded_even_when_it_shares_vocabulary():
    """The guard must key on the classifier, not on the words in the message. The
    row-shortfall and input-overflow exceptions are deterministic by construction —
    neither class name is in any Retry list."""
    for error in (
        ExtractionOutputIncomplete("materially incomplete"),
        ValueError("connection to the schema was reset"),
    ):
        assert not is_transient_error(error), f"fixture precondition for {error!r}"
        document = _document()
        service = MagicMock()
        assert persist_section_after_extraction_failure(
            document_service=service,
            document=document,
            section_id="1",
            section_index=0,
            error=error,
        )
        assert service.update_document_section.called


@pytest.mark.unit
def test_an_exception_echoing_document_content_is_bounded_but_keeps_its_remedy():
    """The bound itself lives on the model and is covered in
    ``tests/unit/test_processing_issue_bounds.py``. What matters here is that this
    path's ``root_cause`` survives it *usefully*, because an oversized exception on
    this path is the concrete case the bound was added for: the section write would
    otherwise exceed DynamoDB's 400 KB item ceiling, and this path swallows that
    write error deliberately, so the section would be left unmarked.
    """
    document = _document()
    remedy = "Set extraction.row_shortfall_action to 'warn' to accept a partial list."
    issue = record_section_extraction_failure(
        document,
        "1",
        ExtractionOutputIncomplete(f"row data: {'y' * 500_000} {remedy}"),
    )

    assert issue is not None
    assert len(issue.root_cause.encode("utf-8")) <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES
    # Head: the exception class still identifies the failure.
    assert issue.root_cause.startswith("ExtractionOutputIncomplete: row data:")
    # Tail: the remedy is the sentence a reader acts on, and it is still there. A
    # bound that clipped the tail would drop it while still looking like it worked.
    assert issue.root_cause.endswith(remedy)


@pytest.mark.unit
def test_a_root_cause_within_the_bound_is_left_exactly_as_it_is():
    """The ordinary case must be untouched: every diagnostic sentence in the tree is
    far shorter than the bound, and an elision marker on one would be noise."""
    cause = "ExtractionOutputIncomplete: materially incomplete: 43 of 1200 rows."
    issue = ProcessingIssue(
        stage="extraction",
        severity="error",
        code=EXTRACTION_FAILED_CODE,
        message="m",
        root_cause=cause,
    )
    assert issue.root_cause == cause


@pytest.mark.unit
def test_a_failing_write_is_swallowed_so_it_cannot_mask_the_original_error():
    """This runs inside an ``except`` block that is about to re-raise. Raising
    here would replace the exception naming the actual failure with a DynamoDB
    error about the attempt to report it."""
    document = _document()
    service = MagicMock()
    service.update_document_section.side_effect = RuntimeError("dynamodb unreachable")

    assert (
        persist_section_after_extraction_failure(
            document_service=service,
            document=document,
            section_id="1",
            section_index=0,
            error=ExtractionOutputIncomplete("materially incomplete"),
        )
        is None
    )
