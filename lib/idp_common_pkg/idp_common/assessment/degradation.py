# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recording that a section came back with NO confidence scores.

There are two ways that happens, and both must leave the same trace:

* the confidence pass **ran and failed** deterministically — ``#901``'s guard
  keeps the (already paid-for) extraction and degrades the section
  (:func:`degrade_section_to_no_confidence`);
* the confidence pass **never ran** because the section did not carry what it
  needs — no extraction result to assess, no pages to send, an empty
  ``inference_result`` (:func:`skip_section_no_confidence`).

Both end with a document that **completes**, so neither produces a failed
execution, a DLQ message or anything the existing failure alarms can see. The
only trace either can leave is on the section itself, which is why both go
through :func:`record_confidence_unavailable`: one error-severity
``ProcessingIssue`` (rendered in the Sections panel's **Status** column, and
persisted by the Assessment Lambda's ``update_document_section`` call) plus one
``AssessmentConfidenceUnavailable`` count (``#996``'s alarm). After a degrade,
:func:`record_no_confidence_in_result` also writes the issue into the section's
extraction result, where the Processing Report reads it.

Appending to ``document.errors`` is **not** a trace. ``processresults_function``
reads a section document's ``errors`` only inside its ``Status.FAILED`` branch,
so on a completing document the list is never read at all — which is how the
skip paths stayed silent (``#1006``).

The two share one implementation on purpose: an alarm that watches "a section
has no confidence" must not be able to see one cause and miss the other because
the two grew apart.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from idp_common import metrics, s3, utils
from idp_common.decision_model import DecisionModelRefused, DecisionModelUnavailable
from idp_common.models import Document, ProcessingIssue

logger = logging.getLogger(__name__)

#: One unit per section that ends up without confidence scores, whatever the
#: cause. Alarmed on by ``AssessmentConfidenceUnavailableAlarm`` in the parent
#: template, which reads the parent stack's namespace (the pattern's
#: ``METRIC_NAMESPACE`` is the parent stack name).
CONFIDENCE_UNAVAILABLE_METRIC = "AssessmentConfidenceUnavailable"

#: The confidence pass ran and failed deterministically (#901).
CONFIDENCE_FAILED_CODE = "assessment_failed_confidence_unavailable"

#: The confidence pass never ran — the section had nothing assessable (#1006).
#: A distinct code from ``CONFIDENCE_FAILED_CODE`` because the remedies have
#: nothing in common: an operator reading the Sections panel for a skip needs to
#: look at what produced the section, not at the confidence model's context
#: window or batch size.
CONFIDENCE_SKIPPED_CODE = "assessment_skipped_confidence_unavailable"


def record_confidence_unavailable(
    document: Document,
    section_id: str,
    issue: ProcessingIssue,
) -> ProcessingIssue:
    """Attach ``issue`` to ``section_id`` and publish the no-confidence count.

    The issue REPLACES only the section's assessment-stage issues. Extraction's
    own issues on the same section (``extraction_incomplete``,
    ``extraction_validation_failed``, …) must survive, because the section write
    that follows replaces the whole map in DynamoDB — an unconditional
    assignment here deleted them.

    The metric is published AFTER the issue is recorded but before the caller
    persists it, so the count and the section record cannot disagree about
    whether the section has confidence.

    The put is wrapped even though ``put_metric`` already swallows errors around
    its own CloudWatch call: every caller of this function is on a path whose
    purpose is to NOT fail a document whose extraction succeeded, so anything
    raising here would trade a paid-for extraction for a missing telemetry
    point. A lost count is the cheaper failure, and it is logged.
    """
    for section in document.sections or []:
        if section.section_id == section_id:
            section.processing_issues = [
                pi
                for pi in (section.processing_issues or [])
                if getattr(pi, "stage", None) != "assessment"
            ] + [issue]
            break
    else:
        # No section to carry the issue, so the metric below is the only signal
        # this document will ever produce. Say so rather than returning quietly.
        logger.error(
            "Section %s is not present in document %s, so the %s issue could "
            "not be recorded on it; only the %s metric will report this.",
            section_id,
            getattr(document, "id", "<unknown>"),
            issue.code,
            CONFIDENCE_UNAVAILABLE_METRIC,
        )

    try:
        metrics.put_metric(CONFIDENCE_UNAVAILABLE_METRIC, 1)
    except Exception as metric_error:
        logger.warning(
            "Could not publish %s for section %s: %s. The section is still "
            "degraded and its processing issue still recorded.",
            CONFIDENCE_UNAVAILABLE_METRIC,
            section_id,
            metric_error,
        )
    return issue


def _is_content_filter_block(error: BaseException) -> bool:
    from idp_common.utils.bedrock_utils import is_content_filter_block

    return is_content_filter_block(error)


def degrade_section_to_no_confidence(
    document: Document,
    section_id: str,
    error: BaseException,
) -> ProcessingIssue:
    """#901: keep a successful extraction when assessment fails deterministically.

    Assessment is an *enrichment* pass: extraction already ran, already wrote its
    results to S3, and was already paid for (two observed runs discarded $17.34 and
    $7.05 of correct extraction — 1,200/1,200 rows at 1.000 cell accuracy — because
    the confidence pass hit a deterministic
    ``ValidationException: Input is too long for requested model.``). Failing the
    document threw away the expensive, correct part of the work to report the loss
    of the cheap, advisory part.

    So for a DETERMINISTIC failure the document is no longer marked
    ``Status.FAILED``. Instead the confidence gap is recorded as an
    error-severity ``ProcessingIssue`` on the section, which the caller persists via
    ``update_document_section``.

    **Where it is visible.** That write goes to the section's DynamoDB record, so the
    issue appears in the **Status** column of the document's Sections panel (and its
    popover). The Visual Editor's **Processing Report** tab renders
    ``metadata.processing_issues`` and the processing flow from the section's
    extraction ``result.json`` in S3, which extraction wrote before this pass ran;
    the Assessment Lambda calls :func:`record_no_confidence_in_result` next, which
    writes the issue there too and marks the Confidence stage as having no scores,
    so the report does not show a confidence pass that scored nothing. That write is
    best effort: if it fails, the issue is on the section record only.

    ``processresults_function`` fails a document only when a section
    document comes back ``Status.FAILED`` (a section's ``errors`` list is read only
    inside that branch), so leaving the status alone is what makes the document
    succeed-without-confidence.

    This is deliberately NOT applied to transient failures: the Assessment Lambda
    checks throttling and ``is_transient_error`` FIRST and re-raises those so Step
    Functions retries the section as before. Only a failure that would fail
    identically on every retry degrades — a retry cannot fix an input that is too
    long for the model.

    **Why this also emits a metric (#996).** Degrading rather than failing is right
    for one section, but it makes a *systemic* assessment failure — a missing
    Bedrock grant, a confidence config every section's input exceeds, a code bug on
    this path — present as a fleet-wide processing issue that no alarm sees, because
    nothing else on this path publishes to CloudWatch and ``ProcessingIssueCount``
    is a DynamoDB attribute, not a metric. Before this, the same failure would have
    failed documents and lit the existing failure alarms. The
    ``AssessmentConfidenceUnavailable`` count restores a signal at the *volume*
    level, where the distinction lives: one degraded section is an expected outcome,
    dozens in a quarter of an hour is a misconfiguration. Alarmed on in the parent
    template (``AssessmentConfidenceUnavailableAlarm``), which can read this
    namespace because the pattern's ``METRIC_NAMESPACE`` is the *parent* stack name.

    Returns the recorded ``ProcessingIssue``.
    """
    # The LLM remedies at the end do not apply to a decision model: its window is
    # fixed, and list_batch_size does not change what one page holds.
    if isinstance(error, DecisionModelRefused):
        remedy = (
            "The decision model refused a request, or a request was not sent "
            "because it left no room for page text; a refusal is not retried, and "
            "the root cause names the pages. If a request was over the model's "
            "window, lower extraction.confidence.decision_model.max_input_tokens or "
            "set its input to images; if it left no room for text, raise "
            "max_input_tokens."
        )
    elif isinstance(error, DecisionModelUnavailable):
        remedy = (
            "The decision model could not be reached or did not answer, and this "
            "failure was not retried. The root cause names the error: check the "
            "function ARN (its name must start with GENAIIDP-), the pipeline's "
            "permission to invoke it, and the function's own logs."
        )
    elif _is_content_filter_block(error):
        from idp_common.utils.bedrock_utils import content_block_wording

        blocker, fix = content_block_wording(error, "confidence model")
        remedy = (
            f"{blocker[0].upper()}{blocker[1:]} the confidence model's "
            "reply, and a retry would send the same section to the same filter. "
            f"Review the section's pages, or {fix}."
        )
    else:
        remedy = (
            "Deterministic failures are not retried; if the confidence model "
            "rejected the input as too long, use a confidence model with a larger "
            "context window, reduce extraction.confidence.list_batch_size, or "
            "process smaller sections."
        )
    issue = ProcessingIssue(
        stage="assessment",
        severity="error",
        code=CONFIDENCE_FAILED_CODE,
        message=(
            "Confidence assessment failed for this section, so its extracted "
            "values have NO confidence scores and are not covered by "
            "confidence-based review (HITL thresholds). The extracted data itself "
            f"is complete and was kept. {remedy}"
        ),
        root_cause=f"{type(error).__name__}: {error}",
        section_id=section_id,
    )
    return record_confidence_unavailable(document, section_id, issue)


#: How a report line marks each issue severity.
_SEVERITY_ICONS = {"error": "✗", "warning": "⚠", "info": "ℹ"}

#: The Confidence line of the extraction's text report, as
#: ``ExtractionService._generate_processing_report`` renders the flow stage.
_REPORT_CONFIDENCE_LINE = re.compile(r"^  \S Confidence: .*$", re.MULTILINE)

#: What the Confidence stage says once the pass has produced no scores.
_NO_SCORES_DETAIL = "no confidence scores: the confidence pass failed"


def merge_assessment_issues_into_result(
    extraction_data: Dict[str, Any], issues: List[ProcessingIssue]
) -> None:
    """Write the assessment stage's ``issues`` into a section's extraction result.

    MERGE, do not replace. Extraction already wrote its own issues
    (``extraction_incomplete``, ``extraction_validation_failed``, ...) into
    ``metadata.processing_issues``; the assessment stage owns only the
    assessment-stage ones, so those are replaced and the rest kept. The issues are
    also appended to the text ``processing_report``, which extraction wrote before
    the assessment step ran and so otherwise never mentions them.
    """
    metadata = extraction_data.get("metadata") or {}
    inherited = [
        pi
        for pi in (metadata.get("processing_issues") or [])
        if isinstance(pi, dict) and pi.get("stage") != "assessment"
    ]
    merged = inherited + [pi.to_dict() for pi in issues]
    if not merged:
        return
    extraction_data["metadata"] = metadata
    metadata["processing_issues"] = merged
    issue_lines = ["", "Processing Issues (assessment):"]
    for pi in issues:
        issue_lines.append(
            f"  {_SEVERITY_ICONS.get(pi.severity, '•')} [{pi.code}] {pi.message}"
        )
        if pi.root_cause:
            issue_lines.append(f"      Root cause: {pi.root_cause}")
    existing_report = extraction_data.get("processing_report", "")
    extraction_data["processing_report"] = (
        existing_report + "\n" + "\n".join(issue_lines)
    ).strip()


def record_no_confidence_in_result(
    extraction_result_uri: Optional[str], issue: ProcessingIssue
) -> bool:
    """Show a degraded section's issue in its Processing Report. Best effort.

    The Processing Report tab and the text report read the section's extraction
    result in S3. Extraction wrote it before the confidence pass ran, so after a
    degrade it still shows a Confidence stage as if the pass had scored the
    section. This writes ``issue`` into that file the way a successful pass writes
    its own issues, and marks the Confidence stage as having produced no scores.

    A result that already holds ``explainability_info`` is left alone: the failure
    came after the pass wrote its scores, and marking that file "no confidence
    scores" would contradict the scores beside it.

    The text report's Confidence line is rewritten in place rather than the report
    re-rendered. The renderer is ``ExtractionService._generate_processing_report``,
    and importing the extraction package here would bring strands into the
    Assessment Lambda, which does not install it. The line's format is pinned to
    that renderer by a test.

    Returns whether the file was updated. Never raises: the degrade path exists so
    the document completes, so a failure here is logged and the issue stays on the
    section record, where the Sections panel shows it.
    """
    if not extraction_result_uri:
        return False
    try:
        data = s3.get_json_content(extraction_result_uri)
        if not isinstance(data, dict):
            return False
        if data.get("explainability_info"):
            logger.warning(
                "Not marking %s as having no confidence scores: it already holds "
                "the pass's scores, so the %s failure came after they were written. "
                "The issue is on the section record.",
                extraction_result_uri,
                issue.code,
            )
            return False
        merge_assessment_issues_into_result(data, [issue])
        flow = (data.get("metadata") or {}).get("processing_flow") or {}
        for stage in flow.get("stages") or []:
            if isinstance(stage, dict) and stage.get("key") == "confidence":
                stage.update(status="warning", detail=_NO_SCORES_DETAIL, fanout=0)
                stage.pop("model", None)
        report = data.get("processing_report")
        if isinstance(report, str):
            data["processing_report"] = _REPORT_CONFIDENCE_LINE.sub(
                f"  ⚠ Confidence: {_NO_SCORES_DETAIL}", report, count=1
            )
        bucket, key = utils.parse_s3_uri(extraction_result_uri)
        s3.write_content(data, bucket, key, content_type="application/json")
        return True
    except Exception as e:  # noqa: BLE001 - reporting must not fail the degrade
        logger.warning(
            "Could not record the %s issue in the extraction result %s, so the "
            "Processing Report does not show it; it is on the section record. %s",
            issue.code,
            extraction_result_uri,
            e,
        )
        return False


def skip_section_no_confidence(
    document: Document,
    section_id: str,
    reason: str,
    remedy: Optional[str] = None,
) -> ProcessingIssue:
    """#1006: record that the confidence pass never ran for this section.

    A section can reach the Assessment step with nothing to assess — no
    extraction result written, no pages, or an extraction result whose
    ``inference_result`` is empty. None of that is a confidence-model failure,
    and none of it is worth failing a document over: whatever extraction
    produced is already in S3 and paid for, exactly as in ``#901``.

    It is also not "nothing happened". The section ends up with no confidence
    scores and therefore outside confidence-based review, which is the same
    operational outcome as a degrade, so it leaves the same trace: the issue
    below on the section, and the ``AssessmentConfidenceUnavailable`` count.
    Sharing the metric is deliberate — the alarm exists to answer "are sections
    coming back without confidence?", and the answer is yes either way. The
    ``code`` and ``root_cause`` are what separate the causes for whoever opens
    the document, and they point at different remedies: a skip is a question
    about the section, not about the confidence model.

    ``reason`` states what was missing; ``remedy`` optionally states what to
    look at. Both land in ``root_cause``.

    Returns the recorded ``ProcessingIssue``.
    """
    issue = ProcessingIssue(
        stage="assessment",
        severity="error",
        code=CONFIDENCE_SKIPPED_CODE,
        message=(
            "Confidence assessment did not run for this section, so its "
            "extracted values have NO confidence scores and are not covered by "
            "confidence-based review (HITL thresholds). The confidence model was "
            "never called: the section did not carry anything to assess. "
            "Whatever extraction produced for the section is unchanged."
        ),
        root_cause=f"{reason} {remedy}".strip() if remedy else reason,
        section_id=section_id,
    )
    return record_confidence_unavailable(document, section_id, issue)
