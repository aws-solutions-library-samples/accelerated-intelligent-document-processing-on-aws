# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A section whose extraction failed holds its document for human review (#2038).

A content-filter block now fails only its own section: extraction records an
error-severity ``extraction_failed`` issue, writes an empty result and returns, so
the document completes. Such a section has no fields and no confidence scores, so
before this change it raised no review reason and the document completed
unreviewed. These tests drive the real chain: the section comes from the REAL
``ExtractionService.process_document_section`` raising a block whose shape is
derived from the botocore model (the harness in
``tests/unit/extraction/test_content_filter_fails_one_section.py``), and the
collate step is the deployed ``processresults_function`` handler, in both review
modes, with HITL on and off.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.failure import EXTRACTION_FAILED_CODE
from idp_common.models import ProcessingIssue, Status
from idp_common.review_queues import reasons as reasons_module
from idp_common.review_queues import routing
from idp_common.review_queues.models import ReviewReason, StatusReason
from idp_common.review_queues.reasons import build_reasons
from idp_common.utils.bedrock_utils import CONTENT_FILTER_BLOCK_MESSAGE
from tests.unit._handler_loading import load_with_xray_stubbed
from tests.unit.extraction.test_content_filter_fails_one_section import (
    _block,
    _Context,
    _document,
    _extract,
)
from tests.unit.review_queues.test_reasons_and_routing import _Store

_PROCESSRESULTS = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/processresults_function/index.py",
)
_ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-2038"


def _config(*, hitl: bool) -> IDPConfig:
    config = IDPConfig()
    config.hitl.enabled = hitl
    return config


def _blocked_and_healthy():
    """Section 2 as the real extraction service leaves it after a block, and a
    healthy section 1 with no issues and no alerts."""
    blocked, _ = _extract(_block())
    healthy = _document()
    healthy.sections = [healthy.sections[0]]
    return blocked, healthy


def _run_processresults(monkeypatch, *, hitl: bool, store=None):
    processresults = load_with_xray_stubbed(
        "processresults_index_2038_review", _PROCESSRESULTS
    )
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(processresults, "get_config", lambda **kw: _config(hitl=hitl))
    monkeypatch.setattr(processresults, "create_metadata_file", lambda *a, **kw: None)
    monkeypatch.setattr(processresults, "_REVIEW_STORE", store)
    doc_service = MagicMock()
    doc_service.get_document.return_value = None
    writes = []
    doc_service.update_document.side_effect = lambda d: (
        writes.append(
            {
                "hitl_status": d.hitl_status,
                "pending": d.hitl_sections_pending,
            }
        ),
        d,
    )[1]
    monkeypatch.setattr(
        processresults, "create_document_service", lambda *a, **kw: doc_service
    )
    blocked, healthy = _blocked_and_healthy()
    classified = _document()
    classified.status = Status.CLASSIFYING
    response = processresults.handler(
        {
            "ClassificationResult": {"document": classified.to_dict()},
            "ExtractionResults": [
                {"section_id": "1", "document": healthy.to_dict()},
                {"section_id": "2", "document": blocked.to_dict()},
            ],
            "execution_arn": _ARN,
        },
        _Context(),
    )
    return response, writes


@pytest.mark.unit
class TestLegacyHitl:
    """No review store (or the Default queue off): the legacy HITL marking."""

    def test_the_failed_section_is_marked_for_review(self, monkeypatch):
        response, writes = _run_processresults(monkeypatch, hitl=True)
        assert response["hitl_triggered"] is True
        assert writes[-1]["hitl_status"] == "PendingReview"
        # Only the failed section, not the healthy one.
        assert writes[-1]["pending"] == ["2"]

    def test_hitl_disabled_completes_without_review(self, monkeypatch):
        response, writes = _run_processresults(monkeypatch, hitl=False)
        assert response["hitl_triggered"] is False
        assert writes[-1]["hitl_status"] != "PendingReview"
        assert not writes[-1]["pending"]


@pytest.mark.unit
class TestReviewQueues:
    """A review store with the Default queue enabled: queue routing."""

    def test_the_document_is_held_with_a_reason_naming_the_section(self, monkeypatch):
        store = _Store()
        response, _ = _run_processresults(monkeypatch, hitl=True, store=store)
        assert response.get("review_hold") is True
        [(record, _, _)] = store.holding
        assert record.sections == ["2"]
        [reason] = record.reasons
        assert reason.kind == "extraction_failed"
        assert reason.section_id == "2"
        assert reason.field is None
        # The label is the issue the reviewer also sees in the Sections panel,
        # which names the content filter.
        assert reason.label and "content filter" in reason.label
        assert reason.score == 0.0
        # Not the legacy marking: queue mode decides instead.
        assert response["hitl_triggered"] is False

    def test_hitl_disabled_is_not_queued(self, monkeypatch):
        store = _Store()
        response, _ = _run_processresults(monkeypatch, hitl=False, store=store)
        assert "review_hold" not in response
        assert store.holding == []
        assert response["hitl_triggered"] is False


@pytest.mark.unit
class TestWhichIssuesCount:
    def _section(self, *issues):
        blocked, _ = _blocked_and_healthy()
        section = blocked.sections[1]
        section.processing_issues = list(issues)
        return blocked, section

    def _issue(self, code, severity):
        return ProcessingIssue(
            stage="extraction",
            severity=severity,
            code=code,
            message="m",
            section_id="2",
        )

    @pytest.mark.parametrize(
        "code,severity",
        [
            (EXTRACTION_FAILED_CODE, "warning"),
            ("extraction_rows_below_ocr_estimate", "error"),
            ("extraction_incomplete", "error"),
        ],
    )
    def test_only_an_error_severity_extraction_failed_counts(self, code, severity):
        document, section = self._section(self._issue(code, severity))
        section.confidence_threshold_alerts = []
        config = _config(hitl=True)
        assert build_reasons(document, config, mode="pipeline") == []
        assert routing.legacy_section_triggers(section, hitl_enabled=True) is False

    def test_it_counts_in_bda_mode_and_from_a_serialized_issue(self):
        document, section = self._section(
            self._issue(EXTRACTION_FAILED_CODE, "error").to_dict()
        )
        [reason] = build_reasons(document, _config(hitl=True), mode="bda")
        assert (reason.kind, reason.section_id) == ("extraction_failed", "2")

    def test_the_blocked_section_carries_the_block_in_its_issue(self):
        blocked, _ = _blocked_and_healthy()
        issue = reasons_module.failed_extraction_issue(blocked.sections[1])
        assert issue is not None
        assert CONTENT_FILTER_BLOCK_MESSAGE in issue.root_cause


@pytest.mark.unit
def test_the_code_matches_the_one_extraction_writes():
    """``reasons`` spells the code to stay importable without the extraction extra."""
    assert reasons_module.EXTRACTION_FAILED_ISSUE_CODE == EXTRACTION_FAILED_CODE


@pytest.mark.unit
def test_the_kind_is_accepted_by_both_published_models():
    for model in (ReviewReason, StatusReason):
        reason = model(
            kind="extraction_failed", section_id="2", score=0.0, threshold=0.8
        )
        assert reason.kind == "extraction_failed"
