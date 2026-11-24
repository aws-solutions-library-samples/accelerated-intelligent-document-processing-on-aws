# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the run that follows a change to a document starts, decided once.

A reviewer who completes or sends back a review, and a user who saves edits
outside a review, are not asked for a start step: :func:`decide_restart` derives
it from the document as it now is. Review completion and Process Changes both
call it, so the two agree.

This module imports pydantic, the standard library, the step list and the plan
model only. In particular it does not import ``idp_common.review_queues``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from pydantic import BaseModel

from .models import RunKind
from .steps import (
    APPLY_REVIEW_CHANGES,
    BDA,
    CLASSIFICATION,
    EXTRACTION,
    POLICY_RULE_VALIDATION,
    resolve_start,
)

__all__ = [
    "CAUSE_ADDED",
    "CAUSE_NO_RESULT",
    "CAUSE_PAGES_CHANGED",
    "MAX_LAYOUT_SECTIONS",
    "SEND_BACK_START",
    "RestartDecision",
    "decide_restart",
    "layout_of",
]

#: Where a sent-back document starts: the page text is kept and the split into
#: sections is done again. In BDA mode that is the beginning.
SEND_BACK_START = CLASSIFICATION

#: Why a section is extracted again. ``added``: it did not exist when the
#: document was placed for review. ``pages_changed``: its pages are not the ones
#: it had then. ``no_result``: it has no extraction result.
CAUSE_ADDED = "added"
CAUSE_PAGES_CHANGED = "pages_changed"
CAUSE_NO_RESULT = "no_result"

#: The most sections a layout records. A document with more records none.
MAX_LAYOUT_SECTIONS = 500


def _page_order(page_id: Any) -> Tuple[int, Any]:
    """Numbered pages first, by number; any other id after them, by its text."""
    text = str(page_id)
    try:
        return (0, int(text))
    except ValueError:
        return (1, text)


def _pages_digest(page_ids: Optional[Iterable[Any]]) -> str:
    """A short digest of a section's pages, the same in whatever order they come."""
    ordered = sorted(page_ids or (), key=_page_order)
    joined = ",".join(str(page_id) for page_id in ordered)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _excluded(section: Any) -> bool:
    return bool(getattr(section, "excluded", False))


def layout_of(document: Any) -> Dict[str, str]:
    """Section id to a digest of the section's pages, for every section.

    Excluded sections are included: a section is told apart by its pages, not by
    whether it is processed. A document with more than ``MAX_LAYOUT_SECTIONS``
    sections, or no document, gives an empty layout, which compares with
    nothing.
    """
    if document is None:
        return {}
    sections = list(document.sections or ())
    if len(sections) > MAX_LAYOUT_SECTIONS:
        return {}
    return {section.section_id: _pages_digest(section.page_ids) for section in sections}


class RestartDecision(BaseModel, frozen=True):
    """Where a run starts, and the sections it extracts again."""

    #: A step id, or ``apply_review_changes``.
    start: str
    #: The sections to extract again, for an extraction start. Empty means every
    #: section.
    sections: Tuple[str, ...] = ()
    #: Why each named section is extracted again, by section id.
    causes: Dict[str, str] = {}


def _cause(section: Any, baseline: Mapping[str, str]) -> Optional[str]:
    """Why a section is extracted again after a review; ``None`` when it is not."""
    if baseline:
        if section.section_id not in baseline:
            return CAUSE_ADDED
        if baseline[section.section_id] != _pages_digest(section.page_ids):
            return CAUSE_PAGES_CHANGED
    if not section.extraction_result_uri:
        return CAUSE_NO_RESULT
    return None


def _after_a_completed_review(
    document: Any, mode: str, baseline: Mapping[str, str]
) -> RestartDecision:
    if document is None or mode == BDA:
        return RestartDecision(start=APPLY_REVIEW_CHANGES)
    causes: Dict[str, str] = {}
    for section in document.sections or ():
        if _excluded(section):
            continue
        cause = _cause(section, baseline)
        if cause is not None:
            causes[section.section_id] = cause
    if not causes:
        return RestartDecision(start=APPLY_REVIEW_CHANGES)
    return RestartDecision(start=EXTRACTION, sections=tuple(causes), causes=causes)


def _after_saved_edits(document: Any, class_reset: bool) -> RestartDecision:
    if class_reset:
        return RestartDecision(start=CLASSIFICATION)
    sections = list(document.sections or ())
    without_result = [
        section.section_id
        for section in sections
        if not section.extraction_result_uri and not _excluded(section)
    ]
    if not without_result:
        return RestartDecision(start=POLICY_RULE_VALIDATION)
    # Every section needing it is a plain extraction start.
    if len(without_result) < len(sections):
        return RestartDecision(
            start=EXTRACTION,
            sections=tuple(without_result),
            causes=dict.fromkeys(without_result, CAUSE_NO_RESULT),
        )
    return RestartDecision(start=EXTRACTION)


def decide_restart(
    document: Any,
    *,
    kind: RunKind,
    mode: str,
    baseline_layout: Optional[Mapping[str, str]] = None,
    class_reset: bool = False,
) -> RestartDecision:
    """Where the run of ``kind`` starts for ``document``, in a processing mode.

    | Kind | The document | Start |
    |---|---|---|
    | ``sent_back`` | any | ``classification``; the beginning in BDA mode |
    | ``review_completed`` | none, or BDA mode | ``apply_review_changes`` |
    | ``review_completed`` | a processed section was added, has other pages or has no result | ``extraction``, for those sections |
    | ``review_completed`` | otherwise | ``apply_review_changes`` |
    | ``saved_edits`` | a page's class was reset (``class_reset``) | ``classification`` |
    | ``saved_edits`` | a processed section has no result | ``extraction``, for those sections |
    | ``saved_edits`` | otherwise | ``policy_rule_validation`` |

    A processed section is one that is not excluded. ``baseline_layout`` is the
    layout (:func:`layout_of`) recorded when the document was placed for review:
    a section is added or has other pages relative to it. With no baseline, or
    an empty one, only a missing result sends a section to extraction. After
    saved edits, sections are named only when some section keeps its result.

    Raises:
        ValueError: ``kind`` is not one of the three, or saved edits are decided
            with no document.
    """
    if kind == RunKind.SENT_BACK:
        return RestartDecision(start=resolve_start(SEND_BACK_START, mode))
    if kind == RunKind.REVIEW_COMPLETED:
        return _after_a_completed_review(document, mode, baseline_layout or {})
    if kind == RunKind.SAVED_EDITS:
        if document is None:
            raise ValueError("the restart after saved edits is decided from a document")
        return _after_saved_edits(document, class_reset)
    raise ValueError(
        f"where a {getattr(kind, 'value', kind)} run starts is not decided from "
        "the document"
    )
