# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether a document can be run again from a step, checked before anything is queued.

``check`` reads and never writes. Every surface that offers a start step asks the
same function, so the web UI, the SDK, the CLI and the MCP tool refuse the same
requests with the same code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from idp_common.config.feature_flags import policy_rule_validation_enabled
from idp_common.models import Status

from .mode import ProcessingMode
from .models import RunKind
from .steps import (
    ASSESSMENT,
    BDA,
    BEGINNING,
    CLASSIFICATION,
    EVALUATION,
    EXTRACTION,
    INTERNAL_STARTS,
    POLICY_RULE_VALIDATION,
    RESUME,
    SUMMARIZATION,
    StepNotInMode,
    UnknownStep,
    optional_step_ids,
    position,
    resolve_start,
)

logger = logging.getLogger(__name__)

#: The refusal codes.
MALWARE_GATE = "MALWARE_GATE"
RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
NOT_FAILED = "NOT_FAILED"
UNKNOWN_STEP = "UNKNOWN_STEP"
STEP_NOT_IN_MODE = "STEP_NOT_IN_MODE"
OUTPUT_MISSING = "OUTPUT_MISSING"
OTHER_PROFILE_BEGINNING_ONLY = "OTHER_PROFILE_BEGINNING_ONLY"
REVISION_HAS_PREPROCESSING_HOOK = "REVISION_HAS_PREPROCESSING_HOOK"
REVISION_CHANGES_MODE = "REVISION_CHANGES_MODE"

#: Statuses whose documents must never be re-admitted to the pipeline. This is
#: the one declaration of the set.
#:
#: The malware-scanning ingest gate refuses a document *before* any workflow
#: starts, so the file is still in the input bucket. Running a document again does
#: not go back through that gate, so without this check a quarantined document
#: would be pushed into OCR, page rendering and Bedrock.
#:
#: ``MALWARE_SCANNING`` belongs here although it is not a refusal: a document held
#: at the gate has no verdict yet, and a run would process the file unscanned.
MALWARE_SCANNING_STATUS = Status.MALWARE_SCANNING.value
MALWARE_REFUSED_STATUSES = frozenset(
    {
        Status.MALWARE_SCANNING.value,
        Status.QUARANTINED.value,
        Status.MALWARE_SCAN_FAILED.value,
    }
)

#: Statuses of a document that a run is working on.
IN_PROGRESS_STATUSES = frozenset(
    status.value
    for status in (
        Status.QUEUED,
        Status.RUNNING,
        Status.PREPROCESSING,
        Status.OCR,
        Status.CLASSIFYING,
        Status.EXTRACTING,
        Status.ASSESSING,
        Status.POSTPROCESSING,
        Status.SUMMARIZING,
        Status.RULE_VALIDATION_POLICY_CLASSIFICATION,
        Status.RULE_VALIDATION,
        Status.RULE_VALIDATION_ORCHESTRATOR,
        Status.EVALUATING,
    )
)

#: The kinds refused while another run is in progress. A new upload and the runs
#: that follow a review are not: a re-sent message and a finalize run must still
#: go through.
KINDS_REFUSED_IN_PROGRESS = frozenset(
    {
        RunKind.REPROCESS,
        RunKind.SENT_BACK,
        RunKind.RESTART_FAILED,
        RunKind.SAVED_EDITS,
    }
)

#: The kinds whose profile and revision are chosen by the caller. Every other
#: kind keeps the revision the document is pinned to.
KINDS_THAT_CHOOSE_A_CONFIGURATION = frozenset({RunKind.REPROCESS})


@dataclass(frozen=True)
class ReprocessRequest:
    """One document, and how it is asked to run again."""

    object_key: str
    start: str = BEGINNING
    skip: Tuple[str, ...] = ()
    #: The profile and revision to run under; ``None`` keeps the document's.
    version: Optional[str] = None
    revision: Optional[int] = None
    kind: RunKind = RunKind.REPROCESS
    #: The sections to extract again, when the caller knows them (saved edits).
    sections: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EligibilityContext:
    """What ``check`` reads through. Every callable reads; none writes."""

    #: The document as the tracking row has it, or ``None`` when there is no row.
    load_document: Callable[[str], Any]
    #: The processing mode of a profile at a revision (``None`` = the head).
    mode_for: Callable[[Optional[str], Optional[int]], ProcessingMode]
    #: The published revision of a profile, or ``None``.
    published_revision: Callable[[str], Optional[int]] = lambda profile: None
    #: Whether a configuration has an enabled preprocessing hook.
    has_preprocessing_hook: Callable[[Any], bool] = field(
        default=lambda config: preprocessing_hook_enabled(config)
    )


@dataclass(frozen=True)
class Eligibility:
    """The answer for one document."""

    ok: bool
    #: A refusal code, empty when ``ok``.
    code: str = ""
    message: str = ""
    #: The step the run starts at, after aliases and ``resume``.
    start: str = ""
    #: The sections to extract again, when not all of them.
    sections: Tuple[str, ...] = ()
    #: The processing mode the document runs in.
    mode: str = ""
    #: The document as loaded, for the caller that goes on to queue it.
    document: Any = None


def preprocessing_hook_enabled(config: Any) -> bool:
    """True when a configuration runs a preprocessing hook: enabled, with an ARN."""
    preprocessing = (
        config.get("preprocessing")
        if isinstance(config, dict)
        else getattr(config, "preprocessing", None)
    )
    if preprocessing is None:
        return False
    if isinstance(preprocessing, dict):
        return bool(preprocessing.get("enabled")) and bool(preprocessing.get("arn"))
    return bool(getattr(preprocessing, "enabled", False)) and bool(
        getattr(preprocessing, "arn", None)
    )


def _status_of(document: Any) -> str:
    status = getattr(document, "status", None)
    return str(getattr(status, "value", status) or "")


def _has_text_and_image(page: Any) -> bool:
    return bool(page.image_uri) and bool(page.parsed_text_uri or page.raw_text_uri)


def _pages_ready(document: Any, mode: str) -> bool:
    """OCR's output is there: every page has its image and its text.

    In BDA mode one call produces pages and sections together, so the pages
    being there is the whole test.
    """
    pages = list((document.pages or {}).values())
    if mode == BDA:
        return bool(pages)
    return bool(pages) and all(_has_text_and_image(page) for page in pages)


def _classes_ready(document: Any, mode: str) -> bool:
    """Classification's output is there: sections, and a class on every page.

    In BDA mode a page that matched no blueprint has no class and is still
    processed, so only the sections are required.
    """
    if not document.sections:
        return False
    if mode == BDA:
        return True
    pages = list((document.pages or {}).values())
    return bool(pages) and all(page.classification for page in pages)


def _sections_without_result(document: Any) -> List[str]:
    return [
        section.section_id
        for section in document.sections or []
        if not section.extraction_result_uri and not getattr(section, "excluded", False)
    ]


def _has_assessment_issue(document: Any) -> bool:
    return any(
        getattr(issue, "stage", "") == ASSESSMENT
        for section in document.sections or []
        for issue in getattr(section, "processing_issues", None) or []
    )


def resolve_resume(document: Any, config: Any, mode: str) -> Tuple[str, List[str]]:
    """The first step a failed document has no output for, and its sections.

    Output-based, with no dependence on state names. The earliest of:

    1. ``beginning``, when a page lacks its image or text, or there are no pages;
    2. ``classification``, when a page has no class or there are no sections;
    3. ``extraction`` for the sections that have no result;
    4. ``assessment``, when a section recorded an assessment processing issue;
    5. ``policy_rule_validation``, when it is enabled for the configuration and
       the document has no consolidated result;
    6. ``summarization``, when the document has no summary;
    7. ``evaluation``.

    In BDA mode the first three are all ``beginning``, and there is no assessment
    step.
    """
    if not _pages_ready(document, mode):
        return BEGINNING, []
    if not _classes_ready(document, mode):
        return (BEGINNING if mode == BDA else CLASSIFICATION), []
    missing = _sections_without_result(document)
    if missing:
        if mode == BDA:
            return BEGINNING, []
        all_sections = [
            section.section_id
            for section in document.sections
            if not getattr(section, "excluded", False)
        ]
        # Every section missing its result is a plain extraction start.
        return EXTRACTION, ([] if missing == all_sections else missing)
    if mode != BDA and _has_assessment_issue(document):
        return ASSESSMENT, []
    result = getattr(document, "rule_validation_result", None)
    if policy_rule_validation_enabled(config) and not getattr(
        result, "output_uri", None
    ):
        return POLICY_RULE_VALIDATION, []
    if not getattr(document, "summary_report_uri", None):
        return SUMMARIZATION, []
    return EVALUATION, []


def _output_missing(document: Any, start: str, mode: str) -> str:
    """Why ``start`` cannot begin on this document, or empty when it can."""
    if document is None:
        return "the document has not been processed yet"
    at = position(start)
    if at >= position(CLASSIFICATION) and not _pages_ready(document, mode):
        return "the document has no page text and images; start at the beginning"
    if at >= position(EXTRACTION) and not _classes_ready(document, mode):
        return "the document's pages have no classes or it has no sections"
    if at >= position(ASSESSMENT) and _sections_without_result(document):
        return "a section has no extraction result"
    return ""


def _refused(code: str, message: str, **fields: Any) -> Eligibility:
    return Eligibility(ok=False, code=code, message=message, **fields)


def check(request: ReprocessRequest, context: EligibilityContext) -> Eligibility:
    """Whether the request can be queued, and the step it would start at.

    Checks, in this order: the malware gate; a run in progress; that ``resume``
    is asked of a failed document; the start and skip values; the step in the
    document's mode; the outputs the start needs; and the rules for running
    under another profile or revision.
    """
    key = request.object_key
    try:
        document = context.load_document(key)
    except Exception as error:  # noqa: BLE001 — fails closed, see below
        # The only backstop for a known-infected or unscannable file: a run does
        # not go back through the ingest gate, so a read that fails must refuse.
        logger.error(
            "Could not read %s's status before running it again (%s); refusing "
            "until the status can be read",
            key,
            error,
        )
        return _refused(
            MALWARE_GATE,
            f"Could not verify {key}'s status before reprocessing. This is "
            f"temporary — retry the request.",
        )

    status = _status_of(document) if document is not None else ""
    if status == MALWARE_SCANNING_STATUS:
        return _refused(
            MALWARE_GATE,
            f"Document {key} is still being scanned for malware. Reprocess is "
            f"available once the scan verdict arrives — usually within a few "
            f"minutes. Reprocessing now would process the file before anything "
            f"has decided whether it is safe.",
            document=document,
        )
    if status in MALWARE_REFUSED_STATUSES:
        return _refused(
            MALWARE_GATE,
            f"Document {key} was refused at the malware-scanning gate (status "
            f"{status}) and cannot be reprocessed. Delete it, or — if it is a "
            f"legitimate document GuardDuty could not read, such as a "
            f"password-protected PDF — re-upload an unprotected copy so it is "
            f"scanned again.",
            document=document,
        )
    if request.kind in KINDS_REFUSED_IN_PROGRESS and status in IN_PROGRESS_STATUSES:
        return _refused(
            RUN_IN_PROGRESS,
            f"Document {key} is being processed (status {status}). Wait for the "
            f"run to finish, or abort it, then try again.",
            document=document,
        )

    unknown_skips = sorted(set(request.skip) - set(optional_step_ids()))
    if unknown_skips:
        return _refused(
            UNKNOWN_STEP,
            f"{', '.join(unknown_skips)} cannot be skipped; the steps that can be "
            f"skipped are {', '.join(optional_step_ids())}.",
            document=document,
        )

    profile = getattr(document, "config_version", None) if document else None
    pinned_revision = getattr(document, "config_revision", None) if document else None
    document_mode = context.mode_for(profile, pinned_revision)
    mode = document_mode.mode

    sections: Tuple[str, ...] = tuple(request.sections)
    start = request.start
    if start == RESUME:
        if status != Status.FAILED.value:
            return _refused(
                NOT_FAILED,
                f"Document {key} did not fail (status {status or 'unknown'}), so "
                f"there is no failed run to restart.",
                document=document,
                mode=mode,
            )
        start, resumed = resolve_resume(document, document_mode.config, mode)
        sections = tuple(resumed)
    try:
        start = resolve_start(start, mode)
    except StepNotInMode as error:
        return _refused(STEP_NOT_IN_MODE, f"{error}.", document=document, mode=mode)
    except UnknownStep as error:
        return _refused(UNKNOWN_STEP, f"{error}.", document=document, mode=mode)
    if start in INTERNAL_STARTS:
        return _refused(
            UNKNOWN_STEP,
            f"'{start}' is not a step a run can be asked to start at.",
            document=document,
            mode=mode,
        )

    if start != BEGINNING:
        missing = _output_missing(document, start, mode)
        if missing:
            return _refused(
                OUTPUT_MISSING,
                f"Document {key} cannot start at {start}: {missing}.",
                document=document,
                mode=mode,
            )

    if request.kind in KINDS_THAT_CHOOSE_A_CONFIGURATION and start != BEGINNING:
        refusal = _configuration_refusal(
            request, context, profile, pinned_revision, mode, start
        )
        if refusal is not None:
            code, message = refusal
            return _refused(code, message, document=document, mode=mode)

    if start not in (EXTRACTION, ASSESSMENT):
        sections = ()
    return Eligibility(
        ok=True, start=start, sections=sections, mode=mode, document=document
    )


def _configuration_refusal(
    request: ReprocessRequest,
    context: EligibilityContext,
    profile: Optional[str],
    pinned_revision: Optional[int],
    mode: str,
    start: str,
) -> Optional[Tuple[str, str]]:
    """Why a later start cannot run under the requested configuration, if so.

    A different profile always starts at the beginning. Another revision of the
    same profile may start later unless it has an enabled preprocessing hook
    (which a later start would skip) or changes the processing mode.
    """
    if request.version and request.version != profile:
        current = f"'{profile}'" if profile else "the one the document was run under"
        return (
            OTHER_PROFILE_BEGINNING_ONLY,
            f"Running under the profile '{request.version}' instead of "
            f"{current} starts at the beginning; a later start step keeps the "
            f"document's own profile.",
        )
    # No revision named means the profile's published revision, which is what
    # the run is pinned to when it starts.
    revision = request.revision
    if revision is None and profile:
        revision = context.published_revision(profile)
    if revision is None or revision == pinned_revision:
        return None
    requested = context.mode_for(profile, revision)
    if requested.config is not None and context.has_preprocessing_hook(
        requested.config
    ):
        return (
            REVISION_HAS_PREPROCESSING_HOOK,
            f"Revision r{revision} of '{profile}' has a preprocessing hook, which "
            f"a run starting at {start} would skip. Start at the beginning.",
        )
    if requested.mode != mode:
        return (
            REVISION_CHANGES_MODE,
            f"Revision r{revision} of '{profile}' processes in "
            f"{'BDA' if requested.mode == BDA else 'Pipeline'} mode, and the "
            f"document was processed in {'BDA' if mode == BDA else 'Pipeline'} "
            f"mode. Start at the beginning.",
        )
    return None


def reviewed_for(document: Any, start: str) -> bool:
    """Whether a run from ``start`` leaves a completed review standing.

    True when the document's last review was completed and the run does not
    extract or assess again: it starts at policy rule validation or later.
    """
    if start not in (POLICY_RULE_VALIDATION, SUMMARIZATION, EVALUATION):
        return False
    from idp_common.review_queues.statuses import normalize_hitl_status

    return normalize_hitl_status(getattr(document, "hitl_status", None)).kind == (
        "complete"
    )


def step_options(
    document: Any,
    mode: ProcessingMode,
    context: EligibilityContext,
    request: ReprocessRequest,
    steps: Sequence[Any],
) -> List[dict]:
    """For each step of the mode: whether a run can start there, and why not."""
    options = []
    for run_step in steps:
        answer = check(
            ReprocessRequest(
                object_key=request.object_key,
                start=run_step.id,
                version=request.version,
                revision=request.revision,
                kind=request.kind,
            ),
            context,
        )
        options.append(
            {
                "id": run_step.id,
                "available": answer.ok,
                "skippable": not run_step.required,
                "reason": answer.code or None,
            }
        )
    return options
