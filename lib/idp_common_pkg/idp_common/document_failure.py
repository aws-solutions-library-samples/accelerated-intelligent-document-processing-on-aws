# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Making a *failed document* record its own diagnosis before it raises (#1064).

The section-level form of this was #1049 / PR #1059: every extraction failure
raises, and both extraction Lambda entry points wrote the section only *after*
the service returned, so a failing section reached the tracking table carrying
whatever classification had left there. This module is the **document-level**
analogue, live in three handlers on the unified pattern, each of which has a
diagnosis in hand and raises without writing it:

* ``rule-validation-function`` — the per-section rule validation Lambda. Its
  service records the reason in ``document.errors`` and sets ``Status.FAILED``;
  the handler checks the status and raises. It owns **no** section write at all
  (it computes ``section_index`` for one and never uses it), so nothing is
  persisted.
* ``processresults_function`` — collects a per-section verdict for every failed
  section, then raises about twenty-nine lines after its last write.
* ``rule-validation-orchestration-function`` — the only one of the three that
  *tried*. Its recorder referenced ``Status.ERROR``, which is not a member of
  :class:`~idp_common.models.Status`, and passed an ``error_message=`` keyword
  ``update_document_status`` does not accept, so it raised ``AttributeError``
  into a surrounding ``except`` that logged and swallowed it. It had never
  recorded anything, and the only evidence was a "Failed to update document
  status" line that reads like a transient DynamoDB problem.

None of the three states has a ``Catch`` in ``workflow.asl.json``, so nothing
downstream rescues them, and ``workflow_tracker`` writes only a bare
``Document`` of status plus completion time for a FAILED execution — which is
what makes the pre-raise write the only opportunity there is.

**The diagnosis is attached to a SECTION, never to the document.** That is the
one design decision here worth stating, because the obvious alternative is to
persist ``document.errors`` — the free text these stages actually write — and it
is the wrong answer twice over. ``errors`` is not persisted by
``_document_to_update_expressions`` and never has been; it is also the scattered
signal ``ProcessingIssue`` was introduced to *replace*, and it is read today only
by ``processresults_function``'s own ``Status.FAILED`` branch. The alternative of
a new document-level ``ProcessingIssues`` attribute was already considered and
declined where classification faces the same choice (see
``ClassificationService._record_unclassified_page_issues``): ``ProcessingIssues``
is a **Section** field in the API schema, so a document-level issue would bump
``ProcessingIssueCount`` — which the document list does read — and then have no
text to show behind the badge. Giving it text means a new DynamoDB attribute, a
resolver shaping it, a schema type and a UI surface, four layers for a channel
that already has a working one.

So each site names the section (or sections) the failure belongs to, and the
diagnosis travels as an error-severity :class:`~idp_common.models.ProcessingIssue`
down the path that is already persisted and already rendered: the Sections panel's
status column on the document's own page, which reads the section's
``ProcessingIssues`` directly.

⚠️ **The document list's issue badge is only updated by the whole-document writer.**
``ProcessingIssueCount`` is written by ``update_document``, which
:func:`persist_failed_document` uses, and **not** by ``update_document_section``,
which :func:`persist_failed_section` uses — that writer emits a single
``SET Sections[i] = :section`` and touches no counter. So a failure recorded through
the section path is visible on the document's page and may leave the list's badge
reading its previous (often zero) value. Neither list resolver recovers it: the
range resolver returns the stored value, the GSI resolver cannot return the counter
at all (it is absent from that index's INCLUDE projection) and returns no
``Sections`` either, and the UI prefers any non-null stored value over deriving one.

That is deliberate rather than an oversight, and the reason is that the correct
count is not knowable from that writer. ``update_document_section`` does not read the
item, so it cannot compute a document-wide total; the per-section handler has
narrowed ``document.sections`` to the single section it owns before it persists, so a
locally derived count would be 1 and would **clobber a larger correct count** a
sibling wrote; and it runs inside a ``Map`` at ``MaxConcurrency: 10``, so any total
derived from its input snapshot is racy by construction. This is the same reason that
writer already declines to write document-wide confidence-alert totals. Fixing the
badge on this path needs an atomic counter increment, which is not idempotent across
an eight-attempt retry ladder — a separate change, and one that has to cover
``extraction_failed`` as well, which reaches the badge the same way.

Four rules are bundled into :func:`persist_failed_section` and
:func:`persist_failed_document` rather than left to each call site, because they
are the parts that get quietly dropped:

1. **The original exception propagates unchanged.** Every failure inside the
   persist is logged and swallowed. A DynamoDB write that fails while trying to
   make an exception more visible must not surface in its place — the Step
   Functions cause would then report an unavailable table instead of the actual
   failure.
2. **Issues already on the section are preserved.** Only an issue with the same
   ``code`` is replaced, so a retried document does not collect one per attempt
   and a diagnosis another stage wrote is not deleted.
3. **A transient failure records nothing.** See :func:`failure_is_transient`.
4. **The free-size text goes in ``root_cause``**, where
   ``ProcessingIssue.__post_init__`` bounds it. ``message`` is a fixed template at
   every site here; it is also written to DynamoDB and is *not* bounded, so
   composing it from an exception or from ``document.errors`` would reopen the
   400 KB item-ceiling failure that bound exists for.

**No new metric.** Everything routed through this module re-raises, so the
execution fails and the existing failure alarms and DLQ already count it — the
same reasoning as ``idp_common.extraction.failure``, and the opposite of
``idp_common.assessment.degradation``, which exists precisely because its
document *completes*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from idp_common.models import Document, ProcessingIssue, Section
from idp_common.utils.transient_errors import is_transient_error

logger = logging.getLogger(__name__)

#: Stage names. ``rule_validation`` is the rule-validation feature's two Lambdas;
#: ``postprocessing`` is the collate step, which is the document's status at the
#: point ``processresults_function`` gives up.
RULE_VALIDATION_STAGE = "rule_validation"
POSTPROCESSING_STAGE = "postprocessing"

#: This section's rule validation raised, so it has no compliance verdict.
RULE_VALIDATION_FAILED_CODE = "rule_validation_failed"

#: Per-section rule validation produced a result but the orchestrator's
#: consolidation — the step that turns those into the single compliance decision —
#: failed. A distinct code from the one above because it says something different
#: to a reader: the section *was* validated, and the answer was lost afterwards.
#: The two cannot coexist on one section, since a section-level failure fails the
#: execution before the orchestrator runs.
RULE_VALIDATION_NOT_CONSOLIDATED_CODE = "rule_validation_not_consolidated"

#: The collate step found this section's processing had failed.
SECTION_PROCESSING_FAILED_CODE = "section_processing_failed"

#: Every code in this module that means "the stage RAISED", as opposed to flagging
#: a result the pipeline still accepted. The UI renders these as **Failed** rather
#: than **Incomplete** (``src/ui/src/components/common/processing-issues-utils.ts``),
#: and ``scripts/tests/test_failure_code_ui_parity.py`` fails if a code listed here
#: or in ``idp_common.extraction.failure`` is missing from that set — the set is a
#: hand-written literal in another language, so nothing else would notice.
FAILURE_CODES = frozenset(
    {
        RULE_VALIDATION_FAILED_CODE,
        RULE_VALIDATION_NOT_CONSOLIDATED_CODE,
        SECTION_PROCESSING_FAILED_CODE,
    }
)

RULE_VALIDATION_FAILED_MESSAGE = (
    "Rule validation did not complete for this section, so it has no compliance "
    "verdict. The details name what failed."
)

RULE_VALIDATION_NOT_CONSOLIDATED_MESSAGE = (
    "This section was validated, but consolidating the document's rule-validation "
    "results into a single compliance decision failed, so no verdict was recorded "
    "for it. The details name what failed."
)

SECTION_PROCESSING_FAILED_MESSAGE = (
    "Processing failed for this section, so its extracted data should not be "
    "treated as complete. The details name what failed."
)


@dataclass
class SectionDiagnosis:
    """What to record, for one section.

    ``root_cause`` carries the variable-length text — an exception, or the
    ``document.errors`` entries a service accumulated — and is bounded by
    ``ProcessingIssue.__post_init__``. ``message`` is the fixed user-facing
    sentence and must stay a template: it is unbounded.
    """

    section_id: str
    stage: str
    code: str
    message: str
    root_cause: str = ""
    severity: str = "error"
    details: Dict[str, Any] = field(default_factory=dict)


def failure_is_transient(error: BaseException) -> bool:
    """True when ``error`` is a failure a retry could succeed at.

    Recording for one of those shows the section failed for as long as the retry
    ladder runs and then clears it, which is a false alarm rather than a diagnosis.
    The transient tier on all three of these task states is eight attempts at 2.5x
    backoff from a ten-second interval — most of three hours.

    **"Transient" and "will be retried" are the same test at these three sites,
    and that is what makes this suppression sound.** Step Functions matches a Lambda
    failure by ``errorType``, which is the exception's **class name**;
    :func:`is_transient_error` judges by error *code* and ``__cause__`` chain. Those
    are different questions, and a ``Retry.ErrorEquals`` list cannot ask the second
    one — so the classification is answered in one place, here, and put on the wire
    under one class name: ``raise_if_transient`` re-raises any transient cause as
    ``TransientError``, and every task state whose handler can do that lists exactly
    that name. Extraction and assessment have worked this way since #787; the three
    rule-validation states (``PolicyClassificationStep``, ``RuleValidationStep``,
    ``RuleValidationOrchestration``) joined them in #1101, which is what closed the
    gap this docstring used to describe.

    The class names that would agree *anyway* are the ones botocore derives from a
    modeled error code — a Bedrock ``ThrottlingException`` arrives as a class of that
    very name, and the task states list it. The ones that would not are the majority:
    a bare ``ClientError`` carrying the code ``ThrottlingException`` (the shape
    botocore produces when the code is not in the service's error map), a
    ``ReadTimeoutError``, a ``ConnectTimeoutError``, ``ModelTimeoutException``,
    ``ModelNotReadyException``, ``InternalServerException``, a Strands wrapper around
    any of them. Each of those is transient to this predicate, and each now reaches
    the state machine as ``TransientError``, so for these a record withheld here is
    matched by a retry that is actually coming.

    ⚠️ **A throttle under one of AWS's OTHER spellings is invisible to this predicate,
    and then nothing happens at all.** ``TRANSIENT_ERROR_NAMES`` carries
    ``throttlingexception`` and not the legacy ``Throttling``, nor
    ``RequestThrottled``, ``RequestThrottledException``, ``ThrottledException`` or
    ``LimitExceededException``. A throttle arriving under one of those is judged
    deterministic, so it is neither suppressed here **nor** retried by the state
    machine: the document fails on the first attempt, though at least it fails with a
    recorded diagnosis rather than silently. Bedrock does not use those spellings —
    the observed case is a CloudFormation throttle, code ``Throttling``, in a bare
    ``ClientError`` — but several services this pipeline calls do. Tracked in
    `#1132 <https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1132>`_;
    it is a vocabulary gap in the shared predicate rather than anything specific to
    these three sites.

    ⚠️ **One residual, shared with ``idp_common.extraction.failure``: a ladder that
    exhausts every attempt leaves the sections unmarked.** A Lambda cannot see which
    attempt it is on — Step Functions does not pass the retry count into the payload
    unless the state is wired to send ``$$.State.RetryCount``, and no state here is —
    so the final attempt runs exactly the code the first one did and suppresses the
    record for the same reason. The execution still fails, and the failure alarms and
    DLQ still count it; what is missing is the per-section diagnosis. Closing that
    means either injecting the attempt number or catching the exhausted ladder in the
    state machine, both of which are state-machine changes.
    """
    return is_transient_error(error)


def summarize_errors(errors: Sequence[str], fallback: str = "") -> str:
    """Render a service's accumulated ``document.errors`` as one ``root_cause``.

    The count goes **first** because the bound elides the *middle*: on a document
    that accumulated more than about forty of these, the head and tail survive and
    the reader still learns how many there were. Returns ``fallback`` when there is
    nothing to summarise, so a site that has only its exception still records it
    rather than an empty string.
    """
    entries = [str(e) for e in (errors or []) if str(e)]
    if not entries:
        return fallback
    noun = "error" if len(entries) == 1 else "errors"
    return f"{len(entries)} {noun}: " + "; ".join(entries)


def _find_section(document: Document, section_id: str) -> Optional[Section]:
    for section in document.sections or []:
        if section.section_id == section_id:
            return section
    return None


def record_section_failure(
    document: Document,
    diagnosis: SectionDiagnosis,
) -> Optional[ProcessingIssue]:
    """Attach ``diagnosis`` to its section as an issue. Pure — no I/O.

    Returns the recorded issue, or ``None`` when ``document`` carries no section
    with that id, in which case there is nothing to persist and the caller must
    not write: a whole-map section write addressed at a missing section would
    corrupt a sibling's record.

    Issues already on the section survive, including ones from this stage. Only a
    previous issue with the **same code** is replaced, so a retried document does
    not accumulate one per attempt while a different stage's diagnosis — the thing
    a reader most needs next to this one — is kept.
    """
    section = _find_section(document, diagnosis.section_id)
    if section is None:
        logger.error(
            "Section %s is not present in document %s, so the %s issue could not be "
            "recorded on it; the failure is reported only by the exception.",
            diagnosis.section_id,
            getattr(document, "id", "<unknown>"),
            diagnosis.code,
        )
        return None

    issue = ProcessingIssue(
        stage=diagnosis.stage,
        severity=diagnosis.severity,
        code=diagnosis.code,
        message=diagnosis.message,
        root_cause=diagnosis.root_cause,
        section_id=diagnosis.section_id,
        details=dict(diagnosis.details),
    )
    section.processing_issues = [
        pi
        for pi in (section.processing_issues or [])
        if getattr(pi, "code", None) != diagnosis.code
    ] + [issue]
    return issue


def _record_all(
    document: Document,
    diagnoses: Sequence[SectionDiagnosis],
) -> List[ProcessingIssue]:
    recorded = []
    for diagnosis in diagnoses:
        issue = record_section_failure(document, diagnosis)
        if issue is not None:
            recorded.append(issue)
    return recorded


def persist_failed_section(
    document_service: Any,
    document: Document,
    error: BaseException,
    diagnosis: SectionDiagnosis,
    section_index: int,
) -> List[ProcessingIssue]:
    """Record one section's failure and write **that section** atomically.

    For a handler running inside a ``Map``: ``update_document_section`` is a
    ``SET Sections[i] = :section`` on one slot, so ten concurrent iterations do not
    read-modify-write over each other the way ``update_document`` would.
    ``section_index`` must be the section's position in the **full** document,
    captured before the handler narrowed ``document.sections`` to its own section.

    Call from the ``except`` block that is about to re-raise, and re-raise
    unchanged. Never raises; returns the issues actually written, which is empty
    when the failure was transient, the section was absent, or the write failed.
    """
    if failure_is_transient(error):
        logger.info(
            "Not marking section %s as failed: %s is transient, so the step will be "
            "retried and a marked section would clear itself.",
            diagnosis.section_id,
            type(error).__name__,
        )
        return []
    recorded = _record_all(document, [diagnosis])
    if not recorded:
        return []
    try:
        document_service.update_document_section(
            document_id=document.input_key,
            section_index=section_index,
            section=_find_section(document, diagnosis.section_id),
        )
    except Exception as persist_error:
        logger.error(
            "Could not persist failed section %s for document %s: %s. The failure is "
            "still reported by the exception that is about to be re-raised.",
            diagnosis.section_id,
            getattr(document, "input_key", "<unknown>"),
            persist_error,
            exc_info=True,
        )
        return []
    logger.info(
        "Persisted failed section %s (index %d) with issue %s for document %s",
        diagnosis.section_id,
        section_index,
        recorded[0].code,
        document.input_key,
    )
    return recorded


def persist_failed_document(
    document_service: Any,
    document: Document,
    error: BaseException,
    diagnoses: Sequence[SectionDiagnosis],
) -> List[ProcessingIssue]:
    """Record several sections' failures and write the **whole document** once.

    For a handler that already owns ``update_document`` and holds every section:
    one write carries all of the issues, and it is the same write that would have
    happened on the success path. ``ProcessingIssueCount`` is written by that path
    only when the document object carries the sections the count is derived from,
    which is satisfied here by construction — a document with no sections produces
    no diagnoses and this returns before writing.

    Call from the ``except`` block that is about to re-raise, and re-raise
    unchanged. Never raises; returns the issues actually written.
    """
    if failure_is_transient(error):
        logger.info(
            "Not marking document %s as failed: %s is transient, so the step will be "
            "retried and marked sections would clear themselves.",
            getattr(document, "input_key", "<unknown>"),
            type(error).__name__,
        )
        return []
    recorded = _record_all(document, diagnoses)
    if not recorded:
        return []
    try:
        document_service.update_document(document)
    except Exception as persist_error:
        logger.error(
            "Could not persist the failed document %s: %s. The failure is still "
            "reported by the exception that is about to be re-raised.",
            getattr(document, "input_key", "<unknown>"),
            persist_error,
            exc_info=True,
        )
        return []
    logger.info(
        "Persisted failed document %s with %d issue(s) across its sections",
        document.input_key,
        len(recorded),
    )
    return recorded
