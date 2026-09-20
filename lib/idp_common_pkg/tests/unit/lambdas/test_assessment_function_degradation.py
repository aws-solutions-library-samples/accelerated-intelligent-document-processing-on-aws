# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assessment Lambda: an assessment failure must not discard the extraction (#901).

Assessment is an enrichment pass that runs AFTER extraction has written its
results to S3 and been paid for. When the confidence model rejected an oversized
input with a deterministic ``ValidationException: Input is too long for requested
model.``, the handler marked the whole document ``Status.FAILED``, which
``processresults_function`` turns into a failed document — throwing away correct,
expensive extraction (two observed runs discarded $17.34 and $7.05 of extraction
that had scored 1,200/1,200 rows at 1.000 cell accuracy) in order to report the
loss of the advisory part.

These tests load the REAL Lambda module (``patterns/unified/src/assessment_function``)
and pin the boundary:

1. A DETERMINISTIC failure keeps the extraction: no ``Status.FAILED``, the
   section's ``extraction_result_uri`` intact, and the confidence gap recorded as
   an error-severity ``ProcessingIssue`` (``assessment_failed_confidence_unavailable``)
   that the existing ``update_document_section`` call persists for the UI.
2. A TRANSIENT failure is unchanged: it still raises ``TransientError`` so Step
   Functions retries the section. The retry classification itself is not touched
   by this change — the throttling and ``is_transient_error`` branches are checked
   first and both still re-raise.
3. A THROTTLING failure still re-raises for the state machine's own retry.
4. #1006: a section the confidence pass could not run over at all does not
   complete silently either. That one runs the REAL ``AssessmentService``, because
   the gap it covers was invisible precisely at this level — the service returned
   the document, the handler saw no exception, and the Lambda's response was
   indistinguishable from a fully scored section.
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common import metrics
from idp_common.models import Document, Section, Status
from idp_common.utils.transient_errors import TransientError

_INDEX_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/assessment_function/index.py",
)


def _load_module():
    """Load the Lambda module with X-Ray's import-time side effects stubbed.

    ``patch_all()`` and the ``@xray_recorder.capture`` decorator run at import
    time; neither is under test and both need a tracing context.
    """
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    with patch.dict(
        "sys.modules",
        {
            "aws_xray_sdk": MagicMock(),
            "aws_xray_sdk.core": xray_core,
        },
    ):
        spec = importlib.util.spec_from_file_location("assessment_index", _INDEX_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


assessment_index = _load_module()

_EXTRACTION_URI = "s3://out/doc.pdf/1/result.json"


def _document() -> Document:
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
                extraction_result_uri=_EXTRACTION_URI,
            )
        ],
    )


class _Context:
    function_name = "assessment"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:assessment"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def wired(monkeypatch):
    """The handler with every outbound dependency stubbed except the failure."""
    # Empty working bucket → serialize_document returns the document inline
    # instead of compressing it to S3 (no AWS calls in a unit test).
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(assessment_index, "get_config", lambda **kw: MagicMock())
    doc_service = MagicMock()
    monkeypatch.setattr(
        assessment_index, "create_document_service", lambda *a, **kw: doc_service
    )
    # Extraction results exist but carry no explainability_info, so the handler
    # does NOT take the assessment-skip branch.
    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: {"inference_result": {"transactions": [{"amount": "1.00"}]}},
    )
    return doc_service


def _invoke_with_failure(monkeypatch, error):
    service = MagicMock()
    service.process_document_section.side_effect = error
    monkeypatch.setattr(
        assessment_index,
        "assessment",
        SimpleNamespace(AssessmentService=lambda **kw: service),
    )
    event = {"document": _document().to_dict(), "section_id": "1"}
    return assessment_index.handler(event, _Context())


def test_deterministic_failure_keeps_the_extraction(wired, monkeypatch):
    """An oversized-input ValidationException degrades instead of failing: the
    document is not FAILED, the extraction URI survives, and the missing
    confidence is recorded as a surfaced ProcessingIssue."""
    error = botocore.exceptions.ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Input is too long for requested model.",
            }
        },
        "Converse",
    )
    result = _invoke_with_failure(monkeypatch, error)

    document = Document.from_dict(result["document"])
    assert document.status != Status.FAILED
    # The extraction results are still referenced (and were never rewritten).
    assert document.sections[0].extraction_result_uri == _EXTRACTION_URI

    issues = document.sections[0].processing_issues
    assert [i.code for i in issues] == ["assessment_failed_confidence_unavailable"]
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.stage == "assessment"
    assert "ValidationException" in (issue.root_cause or "")
    assert "no confidence" in issue.message.lower()

    # The section (with its issue) is persisted for the UI by the existing write.
    assert wired.update_document_section.called


def test_deterministic_failure_keeps_extraction_issues_from_extraction_stage(
    wired, monkeypatch
):
    """Degrading replaces only assessment-stage issues; an issue extraction already
    recorded on the section must survive (the section write replaces the whole
    map, so dropping them here would delete them from DynamoDB)."""
    from idp_common.models import ProcessingIssue

    doc = _document()
    doc.sections[0].processing_issues = [
        ProcessingIssue(
            stage="extraction",
            severity="warning",
            code="extraction_incomplete",
            message="fewer rows than minItems",
        )
    ]
    service = MagicMock()
    service.process_document_section.side_effect = ValueError("deterministic boom")
    monkeypatch.setattr(
        assessment_index,
        "assessment",
        SimpleNamespace(AssessmentService=lambda **kw: service),
    )
    result = assessment_index.handler(
        {"document": doc.to_dict(), "section_id": "1"}, _Context()
    )
    document = Document.from_dict(result["document"])
    codes = [i.code for i in document.sections[0].processing_issues]
    assert codes == [
        "extraction_incomplete",
        "assessment_failed_confidence_unavailable",
    ]
    assert document.status != Status.FAILED


def test_transient_failure_still_raises_for_step_functions(wired, monkeypatch):
    """A read timeout is retryable, so it must still surface as TransientError —
    the degrade path must not swallow failures a retry could fix."""
    error = botocore.exceptions.ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    with pytest.raises(TransientError):
        _invoke_with_failure(monkeypatch, error)


def test_throttling_failure_still_raises_for_step_functions(wired, monkeypatch):
    """Throttling keeps its own name so the state machine's ThrottlingException
    retry (with its longer backoff) still applies."""
    error = Exception("ThrottlingException: too many tokens, please wait")
    with pytest.raises(Exception) as excinfo:
        _invoke_with_failure(monkeypatch, error)
    assert "Throttling" in str(excinfo.value)


def _oversized_input_error():
    return botocore.exceptions.ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Input is too long for requested model.",
            }
        },
        "Converse",
    )


def test_degrading_emits_the_confidence_unavailable_metric(wired, monkeypatch):
    """#996: a degrade must publish AssessmentConfidenceUnavailable.

    Degrading is the right outcome for one section, but it removed the only signal
    a SYSTEMIC confidence failure used to produce (failed documents lighting the
    failure alarms). Without this metric such a failure is visible only as a
    processing issue in the Sections panel, one document at a time, with nothing to
    alarm on — ``ProcessingIssueCount`` is a DynamoDB attribute, not a metric.
    """
    calls = []
    monkeypatch.setattr(
        metrics,
        "put_metric",
        lambda name, value, *a, **kw: calls.append((name, value)),
    )

    _invoke_with_failure(monkeypatch, _oversized_input_error())

    assert calls == [("AssessmentConfidenceUnavailable", 1)]


def test_a_successful_assessment_emits_no_degrade_metric(wired, monkeypatch):
    """The metric must count degrades only, or the alarm's threshold is meaningless:
    a count emitted on every section would breach on throughput alone."""
    calls = []
    monkeypatch.setattr(
        metrics,
        "put_metric",
        lambda name, value, *a, **kw: calls.append((name, value)),
    )
    service = MagicMock()
    service.process_document_section.return_value = _document()
    monkeypatch.setattr(
        assessment_index,
        "assessment",
        SimpleNamespace(AssessmentService=lambda **kw: service),
    )

    assessment_index.handler(
        {"document": _document().to_dict(), "section_id": "1"}, _Context()
    )

    assert not [c for c in calls if c[0] == "AssessmentConfidenceUnavailable"]


def test_a_failed_metric_put_does_not_fail_the_document(wired, monkeypatch):
    """The metric is best-effort. CloudWatch being unreachable must not turn the
    path that exists to AVOID failing a document into one that fails it — the whole
    point of #901's guard is that the extraction is kept."""
    monkeypatch.setattr(
        metrics,
        "put_metric",
        MagicMock(side_effect=RuntimeError("cloudwatch unreachable")),
    )

    result = _invoke_with_failure(monkeypatch, _oversized_input_error())

    document = Document.from_dict(result["document"])
    assert document.status != Status.FAILED
    assert document.sections[0].extraction_result_uri == _EXTRACTION_URI
    assert [i.code for i in document.sections[0].processing_issues] == [
        "assessment_failed_confidence_unavailable"
    ]


def test_a_section_with_nothing_to_assess_does_not_complete_silently(
    wired, monkeypatch
):
    """#1006: the whole invocation, with the REAL AssessmentService.

    This is the level at which the gap was invisible. A section that reaches the
    Assessment step with no extraction result makes the service return early; the
    handler sees no exception, so its degrade branch never runs, and the response
    it hands back to the state machine looks exactly like a section that was
    scored. ``processresults_function`` then lets the document complete, because a
    section document's ``errors`` list is read only when its status is
    ``Status.FAILED``.

    So the assertions below are about what leaves the Lambda: the returned document
    carries the issue, the section write the UI reads was made, and the count the
    alarm reads was published.
    """
    from idp_common.assessment.degradation import (
        CONFIDENCE_SKIPPED_CODE,
        CONFIDENCE_UNAVAILABLE_METRIC,
    )
    from idp_common.config.models import IDPConfig

    puts = []
    monkeypatch.setattr(
        metrics, "put_metric", lambda name, value, *a, **kw: puts.append((name, value))
    )
    # The real service needs a real config; confidence assessment is on by default.
    monkeypatch.setattr(assessment_index, "get_config", lambda **kw: IDPConfig())

    document = _document()
    document.sections[0].extraction_result_uri = None

    result = assessment_index.handler(
        {"document": document.to_dict(), "section_id": "1"}, _Context()
    )

    returned = Document.from_dict(result["document"])
    issue_codes = [i.code for i in (returned.sections[0].processing_issues or [])]
    assert issue_codes == [CONFIDENCE_SKIPPED_CODE]
    assert (CONFIDENCE_UNAVAILABLE_METRIC, 1) in puts

    # Persisted for the Sections panel by the handler's existing section write.
    assert wired.update_document_section.called
    persisted = wired.update_document_section.call_args.kwargs["section"]
    assert [i.code for i in persisted.processing_issues] == [CONFIDENCE_SKIPPED_CODE]
