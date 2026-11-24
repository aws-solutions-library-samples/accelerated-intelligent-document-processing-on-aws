# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Apply a completed review to the document: the first step of the run after it.

A reviewer who completes a review has confirmed the values of every section, the
ones they corrected and the ones they left. ``apply_review_changes`` writes the
corrections onto the section results, records the review in each section's
result, and clears the alerts and missing-field entries the review answered.

The record is what makes the document read as reviewed afterwards
(``routing_inputs``): the routing rules after policy rule validation then see a
reviewed document, and the built-in rules do not hold it again.

A run that extracted some sections again (the item's ``restart_step`` is
extraction) reaches this step after the extraction. The result of such a section
is new, so the reviewer has not confirmed it: its corrections are set on the new
result field by field (``merge_corrections``), its record says it was extracted
again, and it keeps the alerts the corrections did not answer. The document then
does not read as reviewed, and the routing rules count that section again.

Running it twice gives the same objects, so the step can be retried.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from botocore.exceptions import ClientError
from pydantic import BaseModel

from idp_common.field_paths import FieldPathError, set_path
from idp_common.field_values import _is_under
from idp_common.run_plan.clearing import recount
from idp_common.run_plan.steps import EXTRACTION

from .draft_diff import leaf_values
from .exceptions import ReviewConflict
from .finalize import apply_corrections
from .models import REVIEW_KEY, DraftRecord, ReviewItemRecord
from .routing import recognize_finalize

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CHANGED_PATHS",
    "MAX_CHANGED_PATH_BYTES",
    "REVIEW_KEY",
    "ApplyReviewResult",
    "apply_review_changes",
    "extracted_review_record",
    "merge_corrections",
    "review_record",
]

#: The record's format version.
REVIEW_RECORD_VERSION = 1
#: A record names at most this many changed paths, each at most this long.
MAX_CHANGED_PATHS = 200
MAX_CHANGED_PATH_BYTES = 256

_MAX_WORKERS = 8
_COMPLETE = "complete"
_INFERENCE_RESULT = "inference_result"
#: The S3 error codes of an object that is not there.
_MISSING_OBJECT_CODES = ("NoSuchKey", "404", "NotFound")


class ApplyReviewResult(BaseModel):
    """What applying a review did."""

    #: Sections whose corrections were written onto their result.
    applied_sections: int
    #: Sections whose result now records that the reviewer confirmed them.
    confirmed_sections: int
    #: Sections the run extracted again, whose result now records that.
    extracted_sections: int
    cycle_id: str


def _bounded(paths: Sequence[str]) -> Tuple[List[str], bool]:
    """The paths a record keeps, and whether any was left out."""
    kept = [p for p in paths if len(p.encode("utf-8")) <= MAX_CHANGED_PATH_BYTES]
    if len(kept) > MAX_CHANGED_PATHS:
        kept = kept[:MAX_CHANGED_PATHS]
    return kept, len(kept) != len(paths)


def review_record(
    item: ReviewItemRecord, draft: Optional[DraftRecord] = None
) -> Dict[str, Any]:
    """The review record of one section's result.

    ``confirmed`` is true: completing a review confirms every section whose
    result the reviewer saw. ``changed_paths`` are the fields the reviewer
    changed in this section, from its draft; a section the reviewer left as it
    was has none. Every value comes from the item and the draft, never from the
    clock, so writing the record again gives the same bytes.
    """
    paths: List[str] = list(draft.changed_fields) if draft is not None else []
    kept, cut = _bounded(paths)
    return {
        "v": REVIEW_RECORD_VERSION,
        "cycle_id": item.cycle_id,
        "reviewed_by": item.disposed_by or "",
        "reviewed_at": item.disposed_at or "",
        "confirmed": True,
        "changed_paths": kept,
        "changed_paths_truncated": bool(
            cut or (draft is not None and draft.changed_fields_truncated)
        ),
    }


def extracted_review_record(
    item: ReviewItemRecord,
    applied: Sequence[str] = (),
    unapplied: Sequence[str] = (),
) -> Dict[str, Any]:
    """The review record of a section the run after the review extracted again.

    ``confirmed`` is false: the result is new, and the reviewer has not seen it.
    ``changed_paths`` are the corrections that were set on the new result, and
    ``unapplied_paths`` the ones that could not be. Both lists are bounded as
    ``review_record`` bounds its paths, and ``changed_paths_truncated`` is true
    when either lost a path. Every value comes from the item and the merge,
    never from the clock.
    """
    changed, changed_cut = _bounded(applied)
    left, left_cut = _bounded(unapplied)
    return {
        "v": REVIEW_RECORD_VERSION,
        "cycle_id": item.cycle_id,
        "reviewed_by": item.disposed_by or "",
        "reviewed_at": item.disposed_at or "",
        "confirmed": False,
        "extracted_again": True,
        "changed_paths": changed,
        "changed_paths_truncated": changed_cut or left_cut,
        "unapplied_paths": left,
    }


def _inference(result: Any) -> Any:
    return result.get(_INFERENCE_RESULT, {}) if isinstance(result, dict) else {}


def _set(target: Any, path: str, value: Any) -> bool:
    try:
        return set_path(target, path, value)
    except FieldPathError:
        # A key the path grammar cannot spell (an empty one, say).
        return False


def merge_corrections(base: Any, draft: Any, new: Any) -> Tuple[List[str], List[str]]:
    """Set a reviewer's corrections on a section result that was extracted again.

    ``base`` is the result the reviewer started from, ``draft`` their corrected
    copy of it and ``new`` the result of the new extraction, which is changed in
    place. A correction is a leaf path under ``inference_result`` whose value in
    the draft differs from the base. Each one the draft holds is set on ``new``
    where the path still resolves (``field_paths.set_path``). The rest are not
    applied: a path that no longer resolves (a list row the new extraction does
    not have), and a value the reviewer removed, which is not removed from a new
    extraction.

    Returns the applied paths and the unapplied paths, each sorted. The same
    three inputs give the same result, and so does a ``new`` the corrections
    were already set on.
    """
    before = leaf_values(_inference(base))
    after = leaf_values(_inference(draft))
    target = new.get(_INFERENCE_RESULT) if isinstance(new, dict) else None
    applied: List[str] = []
    unapplied: List[str] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        if path in after and _set(target, path, after[path]):
            applied.append(path)
        else:
            unapplied.append(path)
    return applied, unapplied


def _base_uri(output_bucket: str, doc_key: str, cycle_id: str, section_id: str) -> str:
    """Where completing the review kept the result a reviewer started from."""
    # Imported here: only a run that extracted again needs the service module.
    from .service import BASE_KEY_FORMAT

    key = BASE_KEY_FORMAT.format(doc=doc_key, cycle=cycle_id, section=section_id)
    return f"s3://{output_bucket}/{key}"


def _read_base(s3: Any, uri: str) -> Any:
    """The result a reviewer started from; ``{}`` when no copy of it was kept."""
    try:
        return s3.get_json_content(uri)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
            return {}
        raise


def _answered(entry: Any, corrected: Set[str]) -> bool:
    """True when an alert or missing-field entry is on a corrected path.

    On it, under it, or over it: a correction of ``Lines[0]`` answers an alert on
    ``Lines[0].Amount``, and a correction of ``Address.City`` one on ``Address``.
    """
    if not isinstance(entry, Mapping):
        return False
    name = entry.get("attribute_name") or entry.get("attributeName")
    if not isinstance(name, str):
        return False
    return _is_under(name, corrected) or any(
        _is_under(path, {name}) for path in corrected
    )


def _processed(document: Any) -> List[Any]:
    return [
        section
        for section in list(getattr(document, "sections", None) or [])
        if not getattr(section, "excluded", False)
    ]


def _extracted(item: ReviewItemRecord, sections: Sequence[Any]) -> Set[str]:
    """The ids of the sections the run extracted again; empty when it did not."""
    if item.restart_step != EXTRACTION:
        return set()
    return {str(section_id) for section_id in item.restart_sections} or {
        str(section.section_id) for section in sections
    }


def apply_review_changes(
    document: Any,
    *,
    store: Any,
    s3: Any = None,
    output_bucket: str,
    execution_arn: str,
    now: Optional[datetime] = None,
) -> ApplyReviewResult:
    """Apply the completed review this run follows.

    1. The document's item must be waiting for this execution as the finalize
       run of a completed review.
    2. The reviewer's corrections are written onto the section results.
    3. Every processed section's result gets the review record
       (``review_record``) under ``review``. The rest of the result is kept.
    4. On the document, every processed section's alerts, alert total and
       missing required fields are cleared, and the document's two counts are
       set from its sections.

    A section the run extracted again (the item's ``restart_step`` is
    extraction; its ``restart_sections``, or every processed section when that
    list is empty) is handled differently, because its result is new:

    * its draft is not copied over the result. The corrections are the fields
      whose value in the draft differs from the result the reviewer started
      from, and each is set on the new result (``merge_corrections``);
    * its record is ``extracted_review_record``: not confirmed;
    * it keeps its alerts and missing required fields, except those on a path a
      correction was set on. Its alert total is cleared, since the list is the
      count that is left.

    A processed section is one that is not excluded. An excluded section is not
    touched.

    Raises:
        ReviewConflict: ``NOT_A_COMPLETED_REVIEW`` when this execution is not
            the finalize run of a completed review of the document.
    """
    from idp_common.utils import parse_s3_uri

    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    found = recognize_finalize(
        document,
        store,
        execution_arn,
        apply=False,
        s3=s3,
        output_bucket=output_bucket,
        now=now,
    )
    if found is None or found[0].disposition != _COMPLETE:
        raise ReviewConflict(
            "NOT_A_COMPLETED_REVIEW",
            "this run does not follow a completed review of the document",
        )
    item = found[0]
    doc_key = document.input_key
    drafts = store.list_drafts(doc_key, item.cycle_id)
    sections = _processed(document)
    extracted = _extracted(item, sections)
    copied = apply_corrections(
        document,
        [draft for draft in drafts if draft.section_id not in extracted],
        s3,
        output_bucket,
    )
    draft_of = {draft.section_id: draft for draft in drafts}
    targets = [s for s in sections if getattr(s, "extraction_result_uri", None)]
    # Resolved here, not in the workers: the base copy of each drafted section
    # the run extracted again.
    base_uri_of = {
        section_id: _base_uri(output_bucket, doc_key, item.cycle_id, section_id)
        for section_id in extracted
        if section_id in draft_of
    }

    def record(section: Any) -> Optional[List[str]]:
        """Write one section's record. For an extracted section, its corrected paths."""
        section_id = str(section.section_id)
        uri = section.extraction_result_uri
        result = s3.get_json_content(uri)
        if not isinstance(result, dict):
            raise ValueError(
                f"the result of section {section.section_id} is not a JSON object"
            )
        corrected: Optional[List[str]] = None
        if section_id not in extracted:
            result[REVIEW_KEY] = review_record(item, draft_of.get(section_id))
        else:
            corrected, unapplied = [], []
            if section_id in base_uri_of:
                draft_key = draft_of[section_id].s3_key
                corrected, unapplied = merge_corrections(
                    _read_base(s3, base_uri_of[section_id]),
                    s3.get_json_content(f"s3://{output_bucket}/{draft_key}"),
                    result,
                )
            result[REVIEW_KEY] = extracted_review_record(item, corrected, unapplied)
        bucket, key = parse_s3_uri(uri)
        s3.write_content(result, bucket, key, "application/json")
        return corrected

    corrected_of: Dict[str, List[str]] = {}
    if targets:
        workers = min(_MAX_WORKERS, len(targets))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="apply-review"
        ) as pool:
            for section, corrected in zip(targets, pool.map(record, targets)):
                if corrected is not None:
                    corrected_of[str(section.section_id)] = corrected

    for section in sections:
        section_id = str(section.section_id)
        if section_id in extracted:
            answered = set(corrected_of.get(section_id, ()))
            section.confidence_threshold_alerts = [
                alert
                for alert in section.confidence_threshold_alerts or []
                if not _answered(alert, answered)
            ]
            section.missing_required_fields = [
                missing
                for missing in section.missing_required_fields or []
                if not _answered(missing, answered)
            ]
        else:
            section.confidence_threshold_alerts = []
            section.missing_required_fields = []
        section.confidence_alert_total = None
    recount(document)

    result = ApplyReviewResult(
        applied_sections=copied + sum(1 for paths in corrected_of.values() if paths),
        confirmed_sections=len(targets) - len(corrected_of),
        extracted_sections=len(corrected_of),
        cycle_id=item.cycle_id,
    )
    logger.info(
        "event=review_applied corrected=%d confirmed=%d extracted=%d",
        result.applied_sections,
        result.confirmed_sections,
        result.extracted_sections,
    )
    return result
