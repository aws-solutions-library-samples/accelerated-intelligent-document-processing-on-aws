# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Clear what a run will produce again, before it is queued.

A run that starts at a step runs that step and every later one. Their earlier
outputs must be gone first: each processing Lambda skips its work when it finds
its own output already there, which is right for a retry and wrong for a run that
was asked to do the step again. So the sender clears the outputs of the start
step and of every later step, on the document it queues and in S3, and the
Lambdas find nothing to skip on.

Skipping a step does not clear its output: the run does not produce it again.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence, Tuple

from .steps import (
    ASSESSMENT,
    BEGINNING,
    CLASSIFICATION,
    EVALUATION,
    EXTRACTION,
    POLICY_RULE_VALIDATION,
    START_ALIASES,
    SUMMARIZATION,
    position,
)

logger = logging.getLogger(__name__)

#: The tracking-row attributes that hold each stage's output. A whole-document
#: write only sets attributes, so a cleared field must be removed from the row by
#: name, or the row keeps describing the previous run.
ROW_SECTIONS = ("Sections",)
ROW_POLICY_RULE_VALIDATION = ("RuleValidationResult", "RuleValidationResultUri")
ROW_SUMMARY = ("SummaryReportUri",)
ROW_EVALUATION = ("EvaluationReportUri", "EvaluationStatus")

#: The output prefix of policy rule validation, under the document's key.
POLICY_RULE_VALIDATION_PREFIX = "rule_validation/"


def _s3_client(s3: Any) -> Any:
    if s3 is not None:
        return s3
    from idp_common.s3 import get_s3_client

    return get_s3_client()


def _delete_result(uri: Optional[str], s3: Any) -> None:
    """Delete one section result. A missing object is not an error."""
    if not uri or not uri.startswith("s3://"):
        return
    from idp_common.utils import parse_s3_uri

    try:
        bucket, key = parse_s3_uri(uri)
        _s3_client(s3).delete_object(Bucket=bucket, Key=key)
    except Exception as error:  # noqa: BLE001 — the run overwrites what is left
        logger.warning("Could not delete the section result %s: %s", uri, error)


def _targets(document: Any, sections: Sequence[str]) -> List[Any]:
    """The sections a section-level clearing applies to: the named ones, or all."""
    wanted = set(sections)
    return [
        section
        for section in document.sections or []
        if not wanted or section.section_id in wanted
    ]


def _clear_alerts(section: Any) -> None:
    section.confidence_threshold_alerts = []
    section.confidence_alert_total = None
    section.missing_required_fields = []


def recount(document: Any) -> None:
    """Set the document's alert and missing-field counts from its sections."""
    document.confidence_alert_count = sum(
        max(
            len(section.confidence_threshold_alerts or []),
            section.confidence_alert_total or 0,
        )
        for section in document.sections or []
    )
    document.missing_required_field_count = sum(
        len(section.missing_required_fields or [])
        for section in document.sections or []
    )


def clear_policy_rule_validation(
    document: Any, *, s3: Any = None, output_bucket: Optional[str] = None
) -> None:
    """Clear a document's policy rule validation result, and its files in S3.

    Removes every object under ``<input key>/rule_validation/`` in the output
    bucket and resets ``rule_validation_result``. A failed delete is logged and
    not raised: the stage reads the section outputs of its own run, so a file
    left behind is not consolidated.
    """
    document.rule_validation_result = None
    bucket = output_bucket or getattr(document, "output_bucket", None)
    input_key = getattr(document, "input_key", None)
    if not bucket or not input_key:
        return
    from idp_common.document_versions import delete_current_output_objects

    try:
        deleted = delete_current_output_objects(
            _s3_client(s3),
            bucket,
            input_key,
            subprefixes=(POLICY_RULE_VALIDATION_PREFIX,),
        )
        logger.info(
            "Cleared %d policy rule validation files for %s", deleted, input_key
        )
    except Exception as error:  # noqa: BLE001 — see the docstring
        logger.warning(
            "Failed to clear policy rule validation data for %s: %s", input_key, error
        )


def clear_after_review(document: Any) -> None:
    """Clear the fields a run after a review produces again.

    Fields only, on the document that is queued: the policy rule validation
    result, the summary and the evaluation. The run starts after extraction, so
    pages, sections and their results are kept. Nothing is deleted in S3: the
    stages write over their own outputs.
    """
    document.rule_validation_result = None
    _clear_summary(document)
    _clear_evaluation(document)


def _clear_summary(document: Any) -> None:
    document.summary_report_uri = None
    document.summarization_result = None


def _clear_evaluation(document: Any) -> None:
    document.evaluation_report_uri = None
    document.evaluation_results_uri = None
    document.evaluation_status = None
    document.evaluation_result = None


def clear_for_start(
    document: Any,
    start: str,
    *,
    sections: Sequence[str] = (),
    s3: Any = None,
    output_bucket: Optional[str] = None,
    keep_objects: bool = False,
) -> Tuple[str, ...]:
    """Clear the outputs of ``start`` and of every later step.

    | Start | Cleared |
    |---|---|
    | ``beginning`` | pages and sections, on the document only; the caller purges the output prefix |
    | ``classification`` | page classes, their confidence, reasons, candidates and boundaries; sections and their results in S3 |
    | ``extraction`` | per target section: the result (and its object in S3), attributes, alerts, the alert total, missing fields |
    | ``assessment`` | per target section: alerts, the alert total, missing fields |
    | ``policy_rule_validation`` | the result, and its files in S3 |
    | ``summarization`` | the summary |
    | ``evaluation`` | the evaluation |

    Each row also clears every row below it. ``sections`` names the target
    sections of an extraction or assessment start; empty means every section.

    With ``keep_objects`` nothing is deleted in S3: the section results and the
    policy rule validation files stay where they are, and the fields are cleared
    exactly as without it.

    Returns the tracking-row attributes that hold what was cleared, for the
    caller to remove from the row.

    Raises:
        UnknownStep: ``start`` is not a step or an alias of one.
    """
    start = START_ALIASES.get(start, start)
    at = position(start)
    removed: List[str] = []

    if at == position(BEGINNING):
        document.pages = {}
        document.sections = []
        document.num_pages = 0
    elif at == position(CLASSIFICATION):
        if not keep_objects:
            for section in document.sections or []:
                _delete_result(section.extraction_result_uri, s3)
        for page in (document.pages or {}).values():
            page.classification = None
            page.confidence = None
            page.classification_reason = None
            page.classification_candidates = None
            page.document_boundary = None
        document.sections = []
        removed.extend(ROW_SECTIONS)
    elif at == position(EXTRACTION):
        for section in _targets(document, sections):
            if not keep_objects:
                _delete_result(section.extraction_result_uri, s3)
            section.extraction_result_uri = None
            section.attributes = None
            _clear_alerts(section)
    elif at == position(ASSESSMENT):
        for section in _targets(document, sections):
            _clear_alerts(section)

    if at <= position(ASSESSMENT):
        recount(document)
    if at <= position(POLICY_RULE_VALIDATION):
        if at == position(BEGINNING) or keep_objects:
            # The caller purges the whole output prefix, or the files are kept.
            document.rule_validation_result = None
        else:
            clear_policy_rule_validation(document, s3=s3, output_bucket=output_bucket)
        removed.extend(ROW_POLICY_RULE_VALIDATION)
    if at <= position(SUMMARIZATION):
        _clear_summary(document)
        removed.extend(ROW_SUMMARY)
    if at <= position(EVALUATION):
        _clear_evaluation(document)
        removed.extend(ROW_EVALUATION)
    return tuple(removed)
