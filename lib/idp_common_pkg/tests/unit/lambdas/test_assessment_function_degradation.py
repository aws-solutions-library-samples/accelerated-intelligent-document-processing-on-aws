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

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from idp_common import metrics
from idp_common.models import Document, Section, Status
from idp_common.utils.transient_errors import TransientError
from tests.unit._handler_loading import load_with_xray_stubbed

_INDEX_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/assessment_function/index.py",
)


def _load_module():
    """Load the Lambda module with X-Ray's import-time side effects stubbed.

    ``patch_all()`` and the ``@xray_recorder.capture`` decorator run at import
    time; neither is under test and both need a tracing context.
    """
    return load_with_xray_stubbed("assessment_index", _INDEX_PATH)


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
    # A degrade writes its issue into that result for the Processing Report.
    doc_service.result_writes = []
    monkeypatch.setattr(
        assessment_index.s3,
        "write_content",
        lambda content, bucket, key, **kw: doc_service.result_writes.append(
            (content, bucket, key)
        ),
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
    # The extraction results are still referenced, and their values untouched.
    assert document.sections[0].extraction_result_uri == _EXTRACTION_URI
    ((written, _bucket, _key),) = wired.result_writes
    assert written["inference_result"] == {"transactions": [{"amount": "1.00"}]}

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


_CONFIDENCE_DETAIL = "decision model GENAIIDP-decider · text, one request per page"


def _extraction_result_with_report():
    """A result.json as extraction writes it: the Confidence stage looks done."""
    return {
        "inference_result": {"Total": "100.00"},
        "metadata": {
            "processing_issues": [
                {"stage": "extraction", "code": "extraction_incomplete"}
            ],
            "processing_flow": {
                "stages": [
                    {"key": "extract", "label": "Extract", "status": "ok"},
                    {
                        "key": "confidence",
                        "label": "Confidence",
                        "detail": _CONFIDENCE_DETAIL,
                        "status": "ok",
                        "fanout": 0,
                        "model": "GENAIIDP-decider",
                    },
                ]
            },
        },
        "processing_report": (
            "=== EXTRACTION PROCESSING REPORT ===\n\nProcessing Flow:\n"
            "  ✓ Extract: single LLM pass\n"
            f"  ✓ Confidence: {_CONFIDENCE_DETAIL}\n"
        ),
    }


def test_a_decision_model_refusal_shows_in_the_processing_report(wired, monkeypatch):
    """A refused page degrades the section, and the Processing Report (which
    extraction wrote before the pass) stops showing a Confidence stage that
    scored it: the issue is written into the result and the stage marked."""
    from idp_common.decision_model import DecisionModelRefused

    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: _extraction_result_with_report(),
    )
    error = DecisionModelRefused(
        "The decision model refused 1 of 2 confidence requests, for page 2, so "
        "the section has no decision-model confidence. First refusal: "
        "GENAIIDP-decider refused the request: ValueError: 5000 tokens > 4096"
    )
    result = _invoke_with_failure(monkeypatch, error)

    (issue,) = Document.from_dict(result["document"]).sections[0].processing_issues
    assert issue.code == "assessment_failed_confidence_unavailable"
    assert "decision_model.max_input_tokens or set its input" in issue.message
    assert "list_batch_size" not in issue.message
    assert "page 2" in (issue.root_cause or "")

    ((written, bucket, key),) = wired.result_writes
    assert (bucket, key) == ("out", "doc.pdf/1/result.json")
    assert written["inference_result"] == {"Total": "100.00"}
    codes = [pi["code"] for pi in written["metadata"]["processing_issues"]]
    assert codes == [
        "extraction_incomplete",
        "assessment_failed_confidence_unavailable",
    ]
    stage = written["metadata"]["processing_flow"]["stages"][1]
    assert stage["status"] == "warning"
    assert "no confidence scores" in stage["detail"]
    assert "model" not in stage
    report = written["processing_report"]
    assert f"✓ Confidence: {_CONFIDENCE_DETAIL}" not in report
    assert "⚠ Confidence: no confidence scores" in report
    assert "[assessment_failed_confidence_unavailable]" in report
    assert "✓ Extract: single LLM pass" in report


def test_a_report_that_cannot_be_written_still_degrades(wired, monkeypatch):
    """The report write is best effort: the degrade path exists so the document
    completes, so an S3 failure there leaves the issue on the section only."""

    def _fail(*args, **kwargs):
        raise RuntimeError("S3 is down")

    monkeypatch.setattr(assessment_index.s3, "write_content", _fail)
    result = _invoke_with_failure(monkeypatch, ValueError("deterministic boom"))
    document = Document.from_dict(result["document"])
    assert document.status != Status.FAILED
    assert [i.code for i in document.sections[0].processing_issues] == [
        "assessment_failed_confidence_unavailable"
    ]
    assert wired.update_document_section.called


def test_a_section_with_no_readable_result_writes_nothing(monkeypatch):
    """No result URI, or a result that is not a JSON object: nothing is written
    over it, and the caller is told so."""
    from idp_common import s3
    from idp_common.assessment.degradation import record_no_confidence_in_result
    from idp_common.models import ProcessingIssue

    writes = []
    monkeypatch.setattr(s3, "write_content", lambda *a, **kw: writes.append(a))
    issue = ProcessingIssue(stage="assessment", severity="error", code="c", message="m")

    monkeypatch.setattr(
        s3, "get_json_content", lambda uri: pytest.fail("read without a URI")
    )
    assert record_no_confidence_in_result(None, issue) is False

    monkeypatch.setattr(s3, "get_json_content", lambda uri: ["not", "an", "object"])
    assert record_no_confidence_in_result(_EXTRACTION_URI, issue) is False
    assert writes == []


def test_a_result_that_already_has_scores_is_not_marked_unscored(wired, monkeypatch):
    """A failure raised after the pass wrote its scores into result.json must not
    then write "no confidence scores" beside them."""
    scored = _extraction_result_with_report()
    scored["explainability_info"] = [{"Total": {"confidence": 0.93}}]
    # The handler's own first read sees the result before the pass ran; every
    # read after it sees what the pass wrote.
    reads = iter([_extraction_result_with_report()])
    monkeypatch.setattr(
        assessment_index.s3, "get_json_content", lambda uri: next(reads, scored)
    )
    _invoke_with_failure(monkeypatch, ValueError("failed after the write"))

    assert next(reads, None) is None  # the handler did read before the pass
    assert wired.result_writes == []
    stage = scored["metadata"]["processing_flow"]["stages"][1]
    assert stage["status"] == "ok" and stage["model"] == "GENAIIDP-decider"


def test_an_unavailable_decision_model_gets_its_own_remedy():
    """Throttling past the retries, a timeout or access denied is not a request
    over the window, so the window remedy would send the operator the wrong way."""
    from idp_common.assessment.degradation import degrade_section_to_no_confidence
    from idp_common.decision_model import DecisionModelUnavailable

    issue = degrade_section_to_no_confidence(
        _document(),
        "1",
        DecisionModelUnavailable("GENAIIDP-decider: AccessDeniedException: denied"),
    )
    assert "max_input_tokens" not in issue.message
    assert "could not be reached or did not answer" in issue.message
    assert "AccessDeniedException" in (issue.root_cause or "")


def test_the_report_line_rewritten_after_a_degrade_is_the_one_extraction_renders():
    """The degrade path rewrites the Confidence line of a report it did not
    render. This pins that line's format to the real renderer, so a change there
    fails here instead of silently leaving the old line in place."""
    from idp_common import s3
    from idp_common.assessment.degradation import record_no_confidence_in_result
    from idp_common.extraction.service import ExtractionService
    from idp_common.models import ProcessingIssue

    metadata = _extraction_result_with_report()["metadata"]
    report = ExtractionService(
        region="us-west-2", config={}
    )._generate_processing_report({**metadata, "parsing_succeeded": True})
    assert f"✓ Confidence: {_CONFIDENCE_DETAIL}" in report

    written = []
    result = {"metadata": metadata, "processing_report": report}
    issue = ProcessingIssue(stage="assessment", severity="error", code="c", message="m")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(s3, "get_json_content", lambda uri: result)
        mp.setattr(
            s3, "write_content", lambda content, *a, **kw: written.append(content)
        )
        assert record_no_confidence_in_result(_EXTRACTION_URI, issue) is True
    (rewritten,) = written
    assert f"✓ Confidence: {_CONFIDENCE_DETAIL}" not in rewritten["processing_report"]
    assert "⚠ Confidence: no confidence scores" in rewritten["processing_report"]


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


# ---------------------------------------------------------------------------
# A run that starts at assessment assesses the section again
# ---------------------------------------------------------------------------

_ABSENT = object()


def _invoke_already_assessed(wired, monkeypatch, execution_input=_ABSENT):
    """The handler over a section whose result already carries confidence data.

    Returns the response and the assessment service, so a test can tell the skip
    (the service is never built into a call) from an assessment.
    """
    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: {
            "inference_result": {"transactions": [{"amount": "1.00"}]},
            "explainability_info": [
                {"transactions": [{"amount": {"confidence": 0.9}}]}
            ],
        },
    )
    service = MagicMock()
    service.process_document_section.return_value = _document()
    monkeypatch.setattr(
        assessment_index,
        "assessment",
        SimpleNamespace(AssessmentService=lambda **kw: service),
    )
    event = {"document": _document().to_dict(), "section_id": "1"}
    if execution_input is not _ABSENT:
        event["execution_input"] = execution_input
    return assessment_index.handler(event, _Context()), service


def test_a_run_that_starts_at_assessment_assesses_an_assessed_section_again(
    wired, monkeypatch
):
    result, service = _invoke_already_assessed(
        wired,
        monkeypatch,
        {"runPlan": {"v": 1, "kind": "reprocess", "startStep": "assessment"}},
    )

    assert service.process_document_section.call_count == 1
    assert service.process_document_section.call_args.args[1] == "1"
    assert result["section_id"] == "1"
    # The status write of an assessment that runs, which the skip does not make.
    assert wired.update_document_status.called


@pytest.mark.parametrize(
    "execution_input",
    [
        _ABSENT,
        None,
        {},
        {"document": {}},
        {"runPlan": None},
        {"runPlan": {"v": 1, "kind": "reprocess", "startStep": "beginning"}},
        {"runPlan": {"v": 1, "kind": "saved_edits", "startStep": "extraction"}},
    ],
)
def test_any_other_run_takes_the_already_assessed_skip(
    wired, monkeypatch, execution_input
):
    """A run from the start, a run that starts elsewhere, and a run that began
    under the definition before the state machine passed its input."""
    result, service = _invoke_already_assessed(wired, monkeypatch, execution_input)

    assert not service.process_document_section.called
    assert not wired.update_document_status.called
    assert result["section_id"] == "1"
    document = Document.from_dict(result["document"])
    assert document.sections[0].extraction_result_uri == _EXTRACTION_URI
