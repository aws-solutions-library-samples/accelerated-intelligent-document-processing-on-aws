# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structured ProcessingIssue spine (1.4) + completeness gate (1.3).

Covers:
- ``ProcessingIssue`` round-trips through ``to_dict``/``from_dict`` at the
  dataclass, ``Section``, and ``Document`` levels, and the document rollup
  count/flag.
- ``build_assessment_issues`` maps split_stats → the correct severity/code.
- ``audit_explainability`` detects null-confidence rows, out-of-range
  confidence, and missing geometry per mode.
"""

from __future__ import annotations

from idp_common.assessment.batching import (
    audit_explainability,
    build_assessment_issues,
)
from idp_common.models import Document, ProcessingIssue, Section, Status


# --------------------------------------------------------------------------- #
# ProcessingIssue round-trip + rollup
# --------------------------------------------------------------------------- #
def test_processing_issue_round_trip():
    issue = ProcessingIssue(
        stage="assessment",
        severity="error",
        code="assessment_incomplete",
        message="5 rows unscored",
        root_cause="Nova Lite output cap 10000 exceeded",
        section_id="sec-1",
        details={"unrecoverable_rows": 5},
    )
    back = ProcessingIssue.from_dict(issue.to_dict())
    assert back == issue


def test_processing_issue_to_dict_omits_empty_optionals():
    issue = ProcessingIssue(
        stage="ocr", severity="info", code="x", message="m"
    )  # no root_cause/section_id/details
    d = issue.to_dict()
    assert "root_cause" not in d and "section_id" not in d and "details" not in d


def test_document_section_issue_round_trip_and_rollup():
    s = Section(
        section_id="sec-1",
        classification="bank-statement",
        processing_issues=[
            ProcessingIssue(
                stage="assessment",
                severity="error",
                code="assessment_incomplete",
                message="m",
                details={"unrecoverable_rows": 3},
            )
        ],
    )
    d = Document(id="doc1", status=Status.COMPLETED, sections=[s])
    d.processing_issues = [
        ProcessingIssue(stage="ocr", severity="warning", code="ocr_low", message="x")
    ]

    # Rollup properties
    assert d.processing_issue_count == 2  # 1 section + 1 doc-level
    assert d.has_processing_issues is True

    # Round-trip
    restored = Document.from_dict(d.to_dict())
    assert len(restored.sections[0].processing_issues) == 1
    assert restored.sections[0].processing_issues[0].details["unrecoverable_rows"] == 3
    assert len(restored.processing_issues) == 1
    assert restored.processing_issue_count == 2
    # Top-level count is emitted for GSI projection.
    assert d.to_dict()["processing_issue_count"] == 2


def test_clean_document_emits_no_issue_keys():
    s = Section(section_id="s2", classification="x")
    d = Document(id="d2", status=Status.COMPLETED, sections=[s])
    doc_dict = d.to_dict()
    assert "processing_issues" not in doc_dict["sections"][0]
    assert "processing_issue_count" not in doc_dict
    assert d.has_processing_issues is False


# --------------------------------------------------------------------------- #
# build_assessment_issues — severity ladder
# --------------------------------------------------------------------------- #
def _stats(**over):
    base = {
        "truncated_calls": 0,
        "splits": 0,
        "min_batch_size_used": None,
        "rows_recovered_by_retry": 0,
        "unrecoverable_rows": 0,
        "derived_batch_size": None,
        "configured_batch_size": 25,
        "escalation_model": None,
        "rows_recovered_by_escalation": 0,
        "escalation_rounds": 0,
        "deadline_reached": False,
    }
    base.update(over)
    return base


def test_build_issues_clean_run_none():
    assert build_assessment_issues(_stats()) == []
    assert build_assessment_issues(None) == []


def test_build_issues_unrecoverable_is_error():
    issues = build_assessment_issues(
        _stats(truncated_calls=4, splits=4, unrecoverable_rows=34),
        section_id="sec-1",
        confidence_model="us.amazon.nova-lite-v1:0",
        geometry_mode="llm_grounded",
    )
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].code == "assessment_incomplete"
    assert "34" in issues[0].message
    assert "nova-lite" in issues[0].root_cause


def test_build_issues_recovered_with_retries_is_info():
    issues = build_assessment_issues(
        _stats(
            truncated_calls=1,
            splits=1,
            rows_recovered_by_retry=9,
            derived_batch_size=8,
        ),
        section_id="sec-1",
        confidence_model="us.amazon.nova-lite-v1:0",
        geometry_mode="llm_grounded",
    )
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert issues[0].code == "assessment_recovered_with_retries"


def test_build_issues_escalation_recovered_is_info_with_chain():
    issues = build_assessment_issues(
        _stats(
            truncated_calls=2,
            escalation_rounds=1,
            escalation_model="us.anthropic.claude-sonnet-4-20250514-v1:0",
            rows_recovered_by_escalation=12,
        ),
        section_id="sec-1",
    )
    assert issues[0].severity == "info"
    assert "sonnet-4" in issues[0].root_cause


def test_build_issues_deadline_is_warning():
    issues = build_assessment_issues(
        _stats(truncated_calls=1, deadline_reached=True),
        section_id="sec-1",
    )
    assert issues[0].severity == "warning"
    assert issues[0].code == "assessment_deadline_reached"


def test_build_issues_truncated_no_recovery_is_not_false_recovered():
    """Regression: a call that truncated but recovered 0 rows and left 0 rows
    unscored (e.g. the model truncated on an empty/near-empty list because
    extraction under-produced) must NOT be reported as 'recovered_with_retries'.
    It is an honest 'assessment_truncated' WARNING instead."""
    issues = build_assessment_issues(
        _stats(
            truncated_calls=1,
            rows_recovered_by_retry=0,
            rows_recovered_by_escalation=0,
            unrecoverable_rows=0,
        ),
        section_id="sec-2",
        confidence_model="us.amazon.nova-lite-v1:0",
        geometry_mode="ocr_only",
    )
    assert len(issues) == 1
    assert issues[0].code == "assessment_truncated"
    assert issues[0].severity == "warning"
    # Must NOT claim rows were scored/recovered.
    assert "recovered" not in issues[0].message.lower()
    assert "all rows were confidence-scored" not in issues[0].message.lower()


# --------------------------------------------------------------------------- #
# audit_explainability — completeness gate
# --------------------------------------------------------------------------- #
def test_audit_detects_null_confidence_rows():
    data = {"txns": [{"amt": "1"}, {"amt": "2"}, {"amt": "3"}]}
    assessment = {
        "txns": [
            {"amt": {"confidence": 0.9}},
            {"amt": {"confidence": None}},  # unscored
            {"amt": {"confidence": 0.8}},
        ]
    }
    gaps, issues = audit_explainability(assessment, data, geometry_mode="ocr_only")
    assert gaps == {"txns": [1]}


def test_audit_flags_out_of_range_confidence():
    data = {"total": "100"}
    assessment = {"total": {"confidence": 1.5}}  # invalid
    gaps, issues = audit_explainability(assessment, data, geometry_mode="off")
    assert any(i.code == "assessment_confidence_out_of_range" for i in issues)


def test_audit_flags_missing_geometry_when_mode_on():
    data = {"txns": [{"amt": "1"}]}
    # scored but no bbox/geometry, and mode wants geometry
    assessment = {"txns": [{"amt": {"confidence": 0.9}}]}
    gaps, issues = audit_explainability(assessment, data, geometry_mode="ocr_only")
    assert any(i.code == "assessment_geometry_incomplete" for i in issues)


def test_audit_geometry_ok_when_present():
    data = {"txns": [{"amt": "1"}]}
    assessment = {
        "txns": [
            {
                "amt": {
                    "confidence": 0.9,
                    "geometry": [{"page": 1, "bbox": [0, 0, 1, 1]}],
                }
            }
        ]
    }
    gaps, issues = audit_explainability(assessment, data, geometry_mode="ocr_only")
    assert not gaps
    assert not any(i.code == "assessment_geometry_incomplete" for i in issues)


def test_audit_no_geometry_check_when_mode_off():
    data = {"txns": [{"amt": "1"}]}
    assessment = {"txns": [{"amt": {"confidence": 0.9}}]}
    gaps, issues = audit_explainability(assessment, data, geometry_mode="off")
    assert not issues  # geometry not required, confidence present & in range


def test_audit_flags_trailing_rows_when_assessment_shorter_than_data():
    # The model omitted trailing rows: assessment has 2, data has 5. The
    # completeness gate MUST report the missing indices 2,3,4 (not silently stop
    # at min(len)). This is the exact under-reporting the gate exists to catch.
    data = {"txns": [{"amt": str(i)} for i in range(5)]}
    assessment = {
        "txns": [
            {"amt": {"confidence": 0.9}},
            {"amt": {"confidence": 0.9}},
        ]
    }
    gaps, _issues = audit_explainability(assessment, data, geometry_mode="ocr_only")
    assert gaps == {"txns": [2, 3, 4]}


# --------------------------------------------------------------------------- #
# The standalone assessment step must not DELETE extraction's issues
#
# Found by a live e2e of PR #694. The assessment Lambda writes the section with
# `SET Sections[i] = :section`, a whole-map replace, and the service assigned
# `doc_section.processing_issues = processing_issues` unconditionally — only its
# own, assessment-stage issues. So with `confidence.mode: separate` (the
# RECOMMENDED mode, and the only one where this step runs) every issue extraction
# had raised was deleted from DynamoDB: no section status icon, nothing in the
# document list's Processing Issues column.
#
# Observed on IDP1: the same document under `integrated` (no standalone
# assessment step) kept `extraction_validation_failed`; under `separate` the
# section's ProcessingIssues came back absent, while the section's own
# result.json still listed it. That mismatch is the signature.
# --------------------------------------------------------------------------- #


def _apply_stage_merge(existing, new_assessment_issues):
    """The rule the service applies: replace assessment-stage, keep the rest.

    Kept as a small helper so the intent is asserted directly; the service's
    inline comprehension is exercised through it in the integration test below.
    """
    return [
        pi for pi in (existing or []) if getattr(pi, "stage", None) != "assessment"
    ] + list(new_assessment_issues)


def _issue(stage, code):
    return ProcessingIssue(
        stage=stage, severity="warning", code=code, message=code, section_id="1"
    )


def test_extraction_issues_survive_an_assessment_that_found_nothing():
    existing = [
        _issue("extraction", "extraction_validation_failed"),
        _issue("extraction", "extraction_incomplete"),
    ]
    merged = _apply_stage_merge(existing, [])
    assert [i.code for i in merged] == [
        "extraction_validation_failed",
        "extraction_incomplete",
    ]


def test_assessment_issues_are_appended_not_interleaved_away():
    existing = [_issue("extraction", "extraction_validation_failed")]
    merged = _apply_stage_merge(existing, [_issue("assessment", "assessment_x")])
    assert [i.code for i in merged] == [
        "extraction_validation_failed",
        "assessment_x",
    ]


def test_a_rerun_replaces_its_own_stale_assessment_issues():
    """Idempotence: two assessment passes must not accumulate duplicates."""
    existing = [
        _issue("extraction", "extraction_incomplete"),
        _issue("assessment", "assessment_geometry_incomplete"),
    ]
    merged = _apply_stage_merge(existing, [_issue("assessment", "assessment_x")])
    assert [i.code for i in merged] == ["extraction_incomplete", "assessment_x"]


def test_no_issues_anywhere_stays_empty():
    assert _apply_stage_merge([], []) == []
    assert _apply_stage_merge(None, []) == []


def test_the_service_uses_this_rule():
    """Pin the rule to the source, so an inline rewrite cannot drift from it."""
    import inspect

    from idp_common.assessment import service as svc

    source = inspect.getsource(svc)
    assert 'if getattr(pi, "stage", None) != "assessment"' in source, (
        "the assessment service no longer preserves other stages' processing "
        "issues; a whole-section DynamoDB write will delete them again"
    )
    assert "doc_section.processing_issues = processing_issues" not in source, (
        "the unconditional replace is back"
    )
