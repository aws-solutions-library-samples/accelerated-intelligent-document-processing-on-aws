# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Making a *failed* extraction section visible where the UI looks (#1049).

Every extraction failure raises — ``ExtractionInputTooLarge``,
``ExtractionImageRejected``, ``ModelInvalidToolUseSequence``,
``ExtractionOutputIncomplete`` — and both extraction Lambda entry points persist
the section to DynamoDB only *after* the call returns. So the write never
happened on a failure and the section's record still said whatever classification
left there: no issue, no status icon, nothing in the Sections panel. The
explanation existed, but only in the Step Functions cause and CloudWatch.

``ExtractionOutputIncomplete`` (#1032) is what made that worth fixing rather than
noting. It is the one failure in the family that deliberately persists a
*diagnosis* before raising — under ``extraction.row_shortfall_action: fail`` the
partial rows and an error-severity ``extraction_rows_below_ocr_estimate`` issue
are written to the section's ``result.json`` — and the DynamoDB row is exactly
where a reader would go looking for it.

Two functions, because the two halves answer to different owners:

* :func:`record_section_extraction_failure` decides *what* to record. Pure — it
  mutates the passed document and performs no I/O.
* :func:`persist_section_after_extraction_failure` performs the write, using the
  document service the calling Lambda already built.

Both live here rather than in either handler because there are **two** Lambda
entry points for extraction — ``index.handler`` (in-process) and
``sfn_runtime_handler.handler`` in ``merge`` mode (the Distributed Map shard
merge) — and the failure that motivated this reaches a reader through whichever
one ran. The decision to persist-then-re-raise is still taken at each Lambda
boundary, which is what owns the write; only the body is shared, so the two modes
cannot drift apart on it (the same reasoning that put the raise itself in
``_save_results``).

**Transient failures are excluded.** A read timeout or a throttle is going to be
retried by the state machine, so marking the section would show a red section for
the length of the ladder and then clear it. See
:func:`persist_section_after_extraction_failure`.

**No metric here.** Unlike the assessment degradation path
(``idp_common.assessment.degradation``), which exists precisely because the
document *completes* and therefore trips no alarm, everything routed through this
module re-raises: the execution fails, and the existing failure alarms and DLQ
already count it. A second signal would double-count the same event.
"""

import logging
from typing import Any, Optional

from idp_common.models import Document, ProcessingIssue, Section
from idp_common.utils.transient_errors import is_transient_error

logger = logging.getLogger(__name__)

#: Stage that owns the issues this module writes. Matches the value
#: ``ExtractionService._save_results`` uses, so a successful re-run replaces the
#: issue below instead of accumulating alongside it.
EXTRACTION_STAGE = "extraction"

#: The section's extraction step raised. Distinct from the *detection* codes
#: extraction writes on a section that still succeeded
#: (``extraction_incomplete``, ``extraction_rows_below_ocr_estimate``, …): those
#: flag a result, this one says there is no successful result to flag. The
#: distinction is load-bearing for ``extraction_rows_below_ocr_estimate``, which
#: is written at ``warning`` severity under ``row_shortfall_action: warn`` (the
#: document completes) and at ``error`` under ``fail`` (the section is failed by
#: ``ExtractionOutputIncomplete``) — read on its own it cannot tell a reader
#: which of the two happened, and this code is what does.
EXTRACTION_FAILED_CODE = "extraction_failed"

_FAILED_MESSAGE = (
    "Extraction did not complete for this section. Any partial result already "
    "written to the section's output is kept and still readable, but the section "
    "itself failed, so its extracted data should not be treated as complete. "
    "The details name what failed and the remedy."
)


def _find_section(document: Document, section_id: str) -> Optional[Section]:
    for section in document.sections or []:
        if section.section_id == section_id:
            return section
    return None


def record_section_extraction_failure(
    document: Document,
    section_id: str,
    error: BaseException,
) -> Optional[ProcessingIssue]:
    """Attach an error-severity ``extraction_failed`` issue to ``section_id``.

    Returns the recorded issue, or ``None`` when ``document`` carries no section
    with that id (in which case there is nothing to persist and the caller should
    not write).

    **What it keeps.** Issues already on the section are preserved, including
    extraction's own. That is deliberate and it is the whole reason the
    ``ExtractionOutputIncomplete`` case works: by the time it raises, the section
    already carries the error-severity ``extraction_rows_below_ocr_estimate``
    issue naming the rows extracted, the OCR estimate and the remedy, and
    replacing extraction-stage issues here — which is what a *successful* run does
    — would delete the diagnosis this exists to surface. The only issue replaced
    is a previous ``extraction_failed``, so a retried section does not accumulate
    one per attempt.

    A stale detection issue from an earlier run survives alongside the new
    failure. That is accurate rather than sloppy: the section's
    ``extraction_result_uri`` still points at the earlier run's output, so the
    earlier run's findings about it are still the findings about what is there.

    ``root_cause`` carries ``type(error).__name__`` and the message, which for
    every exception in this family is already a full explanation with a remedy —
    ``_explain_input_overflow``, ``_explain_image_rejection`` and
    ``_fail_on_row_shortfall`` each build one. So the user-facing ``message``
    stays generic and the specifics are not paraphrased in a second place that
    could drift from the first.
    """
    section = _find_section(document, section_id)
    if section is None:
        logger.error(
            "Section %s is not present in document %s, so the %s issue could not "
            "be recorded on it; the failure is reported only by the exception.",
            section_id,
            getattr(document, "id", "<unknown>"),
            EXTRACTION_FAILED_CODE,
        )
        return None

    issue = ProcessingIssue(
        stage=EXTRACTION_STAGE,
        severity="error",
        code=EXTRACTION_FAILED_CODE,
        message=_FAILED_MESSAGE,
        root_cause=f"{type(error).__name__}: {error}",
        section_id=section_id,
    )
    section.processing_issues = [
        pi
        for pi in (section.processing_issues or [])
        if getattr(pi, "code", None) != EXTRACTION_FAILED_CODE
    ] + [issue]
    return issue


def persist_section_after_extraction_failure(
    document_service: Any,
    document: Document,
    section_id: str,
    section_index: int,
    error: BaseException,
) -> Optional[ProcessingIssue]:
    """Record the failure on the section and write the section to DynamoDB.

    Call this from the ``except`` block that is about to re-raise, and re-raise
    unchanged afterwards. ``document`` is the object that was handed to
    ``process_document_section`` / ``merge_section_shards``: both mutate it in
    place and return the same object, so on a raise it is still the caller's
    handle on everything the service recorded before giving up.

    **A TRANSIENT error records nothing.** ``is_transient_error`` is the same
    predicate the handler's own wrapper uses to decide whether to re-raise under
    the name ``ExtractionStep`` / ``ExtractionMergeStep`` retries, so it is exactly
    "a retry is coming". Marking the section in the meantime would show it as
    failed for as long as that ladder runs — eight attempts at 2.5x backoff from a
    10-second interval is most of three hours — and then clear itself, which is a
    false alarm rather than a diagnosis. The sibling assessment path declines the
    same case for the same reason. The residual is that a ladder which exhausts
    every attempt leaves the section unmarked; the execution still fails and the
    failure alarms still see it.

    **Nothing in here may raise.** The original exception is what the user needs —
    it is the one that names the rows lost, or the input that was too large, and
    it is what the Step Functions cause reports. A DynamoDB write that fails while
    trying to make that exception more visible must not replace it with itself, so
    every failure here is logged and swallowed. Returns ``None`` if nothing was
    recorded or the write did not happen.
    """
    if is_transient_error(error):
        logger.info(
            "Not marking section %s as failed: %s is transient, so the step will "
            "be retried and a marked section would clear itself.",
            section_id,
            type(error).__name__,
        )
        return None
    issue = record_section_extraction_failure(document, section_id, error)
    if issue is None:
        return None
    try:
        document_service.update_document_section(
            document_id=document.input_key,
            section_index=section_index,
            section=_find_section(document, section_id),
        )
        logger.info(
            "Persisted failed section %s (index %d) with issue %s for document %s",
            section_id,
            section_index,
            issue.code,
            document.input_key,
        )
    except Exception as persist_error:
        logger.error(
            "Could not persist failed section %s for document %s: %s. The failure "
            "is still reported by the exception that is about to be re-raised.",
            section_id,
            getattr(document, "input_key", "<unknown>"),
            persist_error,
            exc_info=True,
        )
        return None
    return issue
