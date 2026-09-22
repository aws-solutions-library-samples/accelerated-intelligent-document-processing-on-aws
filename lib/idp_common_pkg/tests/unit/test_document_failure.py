# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.document_failure`` — the shared rules, tested directly (#1064).

The three handlers are pinned in
``tests/unit/lambdas/test_document_failure_persistence.py``. This file covers the
four rules the module exists to hold in one place, plus the invariant the whole
approach rests on: that the rule-validation service hands the caller back the same
``Document`` object it recorded onto, so the handler's diagnosis is not a copy of
a stale one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from idp_common.document_failure import (
    FAILURE_CODES,
    POSTPROCESSING_STAGE,
    RULE_VALIDATION_FAILED_CODE,
    RULE_VALIDATION_NOT_CONSOLIDATED_CODE,
    RULE_VALIDATION_STAGE,
    SECTION_PROCESSING_FAILED_CODE,
    SectionDiagnosis,
    failure_is_transient,
    persist_failed_document,
    persist_failed_section,
    record_section_failure,
    summarize_errors,
)
from idp_common.models import Document, ProcessingIssue, Section, Status
from idp_common.utils.transient_errors import TransientError


def _document() -> Document:
    return Document(
        id="internal",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.RULE_VALIDATION,
        sections=[
            Section(section_id="1", classification="w2", page_ids=["1"]),
            Section(section_id="2", classification="bank-statement", page_ids=["2"]),
        ],
    )


def _diagnosis(section_id="2", code=RULE_VALIDATION_FAILED_CODE, root_cause="because"):
    return SectionDiagnosis(
        section_id=section_id,
        stage=RULE_VALIDATION_STAGE,
        code=code,
        message="Rule validation did not complete for this section.",
        root_cause=root_cause,
    )


def _section(document: Document, section_id: str) -> Section:
    return next(s for s in document.sections if s.section_id == section_id)


# --- record_section_failure: what it keeps -----------------------------------


@pytest.mark.unit
def test_records_an_error_severity_issue_on_the_named_section():
    document = _document()
    issue = record_section_failure(document, _diagnosis())
    assert issue is not None
    assert _section(document, "2").processing_issues == [issue]
    assert issue.severity == "error"
    assert issue.section_id == "2"
    # The sibling is untouched.
    assert _section(document, "1").processing_issues == []


@pytest.mark.unit
def test_a_repeated_attempt_replaces_rather_than_accumulates():
    document = _document()
    record_section_failure(document, _diagnosis(root_cause="first"))
    record_section_failure(document, _diagnosis(root_cause="second"))
    issues = _section(document, "2").processing_issues
    assert [i.root_cause for i in issues] == ["second"]


@pytest.mark.unit
def test_issues_from_other_stages_and_other_codes_survive():
    document = _document()
    earlier = ProcessingIssue(
        stage="assessment",
        severity="warning",
        code="assessment_incomplete",
        message="Partial confidence.",
        section_id="2",
    )
    _section(document, "2").processing_issues = [earlier]
    record_section_failure(document, _diagnosis())
    codes = [i.code for i in _section(document, "2").processing_issues]
    assert codes == ["assessment_incomplete", RULE_VALIDATION_FAILED_CODE]


@pytest.mark.unit
def test_two_different_failure_codes_coexist():
    """They say different things, so one must not silently evict the other."""
    document = _document()
    record_section_failure(document, _diagnosis(code=RULE_VALIDATION_FAILED_CODE))
    record_section_failure(
        document, _diagnosis(code=RULE_VALIDATION_NOT_CONSOLIDATED_CODE)
    )
    codes = [i.code for i in _section(document, "2").processing_issues]
    assert codes == [RULE_VALIDATION_FAILED_CODE, RULE_VALIDATION_NOT_CONSOLIDATED_CODE]


@pytest.mark.unit
def test_a_missing_section_records_nothing_rather_than_a_siblings_record():
    document = _document()
    assert record_section_failure(document, _diagnosis(section_id="99")) is None
    assert all(s.processing_issues == [] for s in document.sections)


@pytest.mark.unit
def test_a_sectionless_document_records_nothing():
    document = _document()
    document.sections = []
    assert record_section_failure(document, _diagnosis()) is None


# --- the bound ---------------------------------------------------------------


@pytest.mark.unit
def test_root_cause_is_bounded_by_the_class_not_by_this_module():
    """The cap belongs on ``ProcessingIssue`` so it binds at every call site."""
    document = _document()
    issue = record_section_failure(document, _diagnosis(root_cause="y" * 200_000))
    assert issue is not None
    assert len(issue.root_cause.encode("utf-8")) <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES


@pytest.mark.unit
def test_every_message_template_is_a_fixed_string():
    """``message`` is written to DynamoDB and is NOT bounded.

    Composing it from an exception or from ``document.errors`` would reopen the
    400 KB item-ceiling failure the ``root_cause`` bound exists for, so the
    templates must carry no interpolation.
    """
    from idp_common import document_failure

    for name in dir(document_failure):
        if name.endswith("_MESSAGE"):
            template = getattr(document_failure, name)
            assert isinstance(template, str)
            assert "{" not in template and "%s" not in template, name
            assert len(template.encode("utf-8")) < 512, name


# --- summarize_errors -------------------------------------------------------


@pytest.mark.unit
def test_summarize_errors_leads_with_the_count():
    """The bound elides the MIDDLE, so a count at the front survives it."""
    assert summarize_errors(["a"]) == "1 error: a"
    assert summarize_errors(["a", "b"]) == "2 errors: a; b"


@pytest.mark.unit
def test_summarize_errors_falls_back_when_there_is_nothing_to_summarise():
    assert summarize_errors([], fallback="ValueError: x") == "ValueError: x"
    assert summarize_errors(None, fallback="ValueError: x") == "ValueError: x"
    # Empty strings are not a diagnosis.
    assert summarize_errors(["", ""], fallback="fb") == "fb"


# --- the transient carve-out ------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "error,expected",
    [
        (TransientError(botocore.exceptions.ReadTimeoutError(endpoint_url="x")), True),
        (
            botocore.exceptions.ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "m"}}, "Converse"
            ),
            True,
        ),
        (RuntimeError("schema mismatch"), False),
        # The synthesised exceptions the two status-checking handlers raise.
        (Exception("Rule validation failed for document d, section 2"), False),
        (Exception("Processing failed for 1 out of 2 sections: ..."), False),
    ],
)
def test_failure_is_transient_matches_what_the_state_machine_retries(error, expected):
    assert failure_is_transient(error) is expected


@pytest.mark.unit
def test_transient_failure_writes_nothing_on_either_path():
    transient = TransientError(botocore.exceptions.ReadTimeoutError(endpoint_url="x"))
    service = MagicMock()

    document = _document()
    assert persist_failed_section(service, document, transient, _diagnosis(), 1) == []
    assert persist_failed_document(service, document, transient, [_diagnosis()]) == []
    service.update_document_section.assert_not_called()
    service.update_document.assert_not_called()
    # Nothing was recorded on the object either.
    assert all(s.processing_issues == [] for s in document.sections)


# --- the writes -------------------------------------------------------------


@pytest.mark.unit
def test_persist_failed_section_writes_one_section_by_input_key_and_index():
    service = MagicMock()
    document = _document()
    recorded = persist_failed_section(
        service, document, RuntimeError("boom"), _diagnosis(), 1
    )
    assert [i.code for i in recorded] == [RULE_VALIDATION_FAILED_CODE]
    kwargs = service.update_document_section.call_args.kwargs
    assert kwargs["document_id"] == "doc.pdf"
    assert kwargs["section_index"] == 1
    assert kwargs["section"] is _section(document, "2")


@pytest.mark.unit
def test_persist_failed_document_writes_once_for_all_diagnoses():
    service = MagicMock()
    document = _document()
    recorded = persist_failed_document(
        service,
        document,
        RuntimeError("boom"),
        [
            _diagnosis(section_id="1", code=SECTION_PROCESSING_FAILED_CODE),
            _diagnosis(section_id="2", code=SECTION_PROCESSING_FAILED_CODE),
        ],
    )
    assert len(recorded) == 2
    service.update_document.assert_called_once_with(document)
    assert document.processing_issue_count == 2


@pytest.mark.unit
def test_no_write_happens_when_no_diagnosis_could_be_recorded():
    """A write with nothing to say would replace a section map with a stale copy."""
    service = MagicMock()
    document = _document()
    assert (
        persist_failed_section(
            service, document, RuntimeError("b"), _diagnosis(section_id="99"), 0
        )
        == []
    )
    assert persist_failed_document(service, document, RuntimeError("b"), []) == []
    service.update_document_section.assert_not_called()
    service.update_document.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    "writer,invoke",
    [
        (
            "update_document_section",
            lambda service, document, error: persist_failed_section(
                service, document, error, _diagnosis(), 1
            ),
        ),
        (
            "update_document",
            lambda service, document, error: persist_failed_document(
                service, document, error, [_diagnosis()]
            ),
        ),
    ],
)
def test_a_failing_write_is_swallowed_and_never_raises(writer, invoke):
    """It must not surface in place of the exception it was making visible."""
    service = MagicMock()
    getattr(service, writer).side_effect = RuntimeError("table unavailable")
    document = _document()
    # Does not raise, and reports that nothing was persisted.
    assert invoke(service, document, RuntimeError("the real failure")) == []
    getattr(service, writer).assert_called_once()


@pytest.mark.unit
def test_failure_codes_are_the_ones_that_mean_the_stage_raised():
    assert FAILURE_CODES == {
        RULE_VALIDATION_FAILED_CODE,
        RULE_VALIDATION_NOT_CONSOLIDATED_CODE,
        SECTION_PROCESSING_FAILED_CODE,
    }
    # Stage names follow the existing vocabulary, which the codes are prefixed by.
    assert RULE_VALIDATION_FAILED_CODE.startswith(RULE_VALIDATION_STAGE)
    assert POSTPROCESSING_STAGE == "postprocessing"


# --- the invariant the approach rests on ------------------------------------


@pytest.mark.unit
def test_rule_validation_service_returns_the_same_document_object():
    """The handler's handle on the document must BE the one the service recorded to.

    ``persist_failed_section`` reads the diagnosis off the caller's object, so a
    service that rebuilt and returned a new ``Document`` would make this fix
    persist stale data with every handler test still green — the handler tests
    only *simulate* in-place mutation with a side effect. This asserts it against
    the real code path that sets ``Status.FAILED``.
    """
    from idp_common.rule_validation.service import RuleValidationService

    document = _document()
    returned = RuleValidationService._update_document_status(
        None, document, success=False, error_message="Z3 solver timed out"
    )
    assert returned is document
    assert returned.status == Status.FAILED
    assert "Z3 solver timed out" in document.errors
