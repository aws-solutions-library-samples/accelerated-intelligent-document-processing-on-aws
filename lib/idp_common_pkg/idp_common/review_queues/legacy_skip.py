# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skip All Reviews on a legacy (non-queue) review: the one implementation.

Used by the single-document ``skipAllSectionsReview`` (``complete_section_review``)
and by bulk Skip All Reviews (:mod:`.review_skip`). The tracking table is taken as
a Table-protocol object (``.get_item`` / ``.update_item``), the shape a boto3
``Table`` has; nothing else is called on it. The caller resubmits the document
when it wants downstream processing; this module never does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Tuple

from .exceptions import ReviewHeldByOther
from .statuses import (
    FINISHED_HITL_STATUSES,
    LegacyHitlStatus,
    is_review_finished,
    is_review_raised,
)

logger = logging.getLogger(__name__)

#: ``HITLStatus`` values the web UI reads as completed or skipped when it decides
#: whether to offer Skip All Reviews (``document-action-gates.ts``): lower-cased,
#: all whitespace removed. Looser than :func:`is_review_finished`, which matches
#: exactly after ``strip()``; the server refuses wherever either one reads finished.
UI_SKIP_HIDDEN_STATUSES = frozenset(
    {"completed", "reviewcompleted", "skipped", "reviewskipped"}
)


def tracking_key(object_key: str) -> dict[str, str]:
    return {"PK": f"doc#{object_key}", "SK": "none"}


def not_finished_condition() -> Tuple[str, dict[str, Any]]:
    """``(condition, values)`` refusing a write onto a finished review.

    An absent ``HITLStatus`` makes ``IN`` false, so the condition holds for it.
    """
    values = {f":finished{i}": value for i, value in enumerate(FINISHED_HITL_STATUSES)}
    return f"NOT (HITLStatus IN ({', '.join(values)}))", values


def review_raised_condition() -> Tuple[str, dict[str, Any]]:
    """``(condition, values)`` holding :func:`is_review_raised` at a write.

    ``attribute_type(..., S)`` rather than ``attribute_exists``, so a stored NULL or
    any other non-string ``HITLStatus`` reads as unset here as it does in the rule.
    """
    return (
        "(HITLTriggered = :raisedFlag OR (attribute_type(HITLStatus, :raisedType) "
        "AND HITLStatus <> :raisedUnset AND HITLStatus <> :raisedNone))",
        {
            ":raisedFlag": True,
            ":raisedType": "S",
            ":raisedUnset": "",
            ":raisedNone": "N/A",
        },
    )


def review_finished_error(action: str) -> ValueError:
    return ValueError(
        f"REVIEW_FINISHED: this review is already finished, so there is nothing "
        f"to {action}"
    )


def no_review_pending_error(action: str) -> ValueError:
    return ValueError(
        f"NO_REVIEW_PENDING: this document has no review waiting, so there is "
        f"nothing to {action}"
    )


def read_review_row(table: Any, object_key: str) -> dict[str, Any]:
    """The tracking row, read consistently so a review finished a moment ago shows."""
    return (
        table.get_item(Key=tracking_key(object_key), ConsistentRead=True).get("Item")
        or {}
    )


def skip_offered(row: Mapping[str, Any]):
    """``True`` where the web UI offers Skip All Reviews, else why it does not.

    A review was raised (:func:`is_review_raised`) and it is not completed or
    skipped. Returns ``"finished"`` or ``"not raised"`` otherwise.
    """
    status = row.get("HITLStatus")
    compact = "".join(str(status or "").lower().split())
    if is_review_finished(status) or compact in UI_SKIP_HIDDEN_STATUSES:
        return "finished"
    if is_review_raised(row):
        return True
    return "not raised"


@dataclass(frozen=True)
class OwnerRule:
    """Refuse a review another reviewer holds (bulk, non-Admin callers)."""

    username: str = ""
    email: str = ""

    def holds(self, owner: Any) -> bool:
        owner = str(owner or "")
        return not owner or owner in {self.username, self.email} - {""}


@dataclass
class LegacySkipResult:
    object_key: str
    skipped_sections: list = field(default_factory=list)
    completed_sections: list = field(default_factory=list)


def _is_condition_failure(exc: BaseException) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get(
        "Code"
    ) == "ConditionalCheckFailedException"


def skip_legacy_review(
    table: Any,
    get_document: Callable[[str], Any],
    object_key: str,
    *,
    username: str = "",
    user_email: str = "",
    reason: Optional[str] = None,
    note: Optional[str] = None,
    require_offered: bool,
    require_raised: bool = False,
    owner_rule: Optional[OwnerRule] = None,
    source: str = "ui",
    job_id: Optional[str] = None,
    resubmitted: bool = True,
) -> LegacySkipResult:
    """Skip every pending section and mark the review skipped.

    ``require_offered`` refuses a review the web UI would not offer Skip All
    Reviews for (finished, or never raised), and conditions the write on the
    review not being finished. ``require_raised`` additionally holds "raised" at
    the write (bulk). ``owner_rule`` refuses a review another reviewer holds.
    ``reason``, ``note``, ``source``, ``job_id`` and ``resubmitted`` are kept with
    the skip in ``HITLReviewHistory``.
    """
    document = get_document(object_key)
    if not document:
        raise ValueError(f"Document {object_key} not found")

    completed = set(document.hitl_sections_completed or [])

    # Skipped sections live on the row (not in the document model). Read
    # consistently, so a review finished a moment ago is seen by the check below.
    row = read_review_row(table, object_key)
    existing_skipped = set(row.get("HITLSectionsSkipped", []) or [])

    if require_offered:
        offered = skip_offered(row)
        if offered is not True:
            logger.warning(f"Rejecting skip-all on {object_key}: {offered}")
            if offered == "finished":
                raise review_finished_error("skip")
            raise no_review_pending_error("skip")
    if owner_rule is not None and not owner_rule.holds(row.get("HITLReviewOwner")):
        logger.warning("Rejecting skip-all of a review another reviewer holds")
        raise ReviewHeldByOther()

    all_section_ids = {
        section.section_id for section in document.sections if section.section_id
    }
    # Sections to skip = all sections that are not already completed.
    sections_to_skip = all_section_ids - completed - existing_skipped
    all_skipped = list(sections_to_skip | existing_skipped)

    review_record = {
        "sectionId": "ALL_SKIPPED",
        "reviewedBy": username or "unknown",
        "reviewedByEmail": user_email or "",
        "reviewedAt": datetime.now(timezone.utc).isoformat(),
        "action": "skip_all",
        "skippedSections": list(sections_to_skip),
        **({"reason": reason} if reason else {}),
        **({"note": note} if note else {}),
        "source": source,
        **({"jobId": job_id} if job_id else {}),
        "resubmitted": bool(resubmitted),
    }
    # Appended by DynamoDB: a reviewer finishing a section while someone skips
    # the rest would otherwise have their entry overwritten by whichever of the
    # two wrote last.
    #
    # `HITLSectionsSkipped` in the same statement is still a whole-list write, and
    # it is **not** safe under the same overlap. `all_skipped` unions
    # `existing_skipped` in, so nothing another skip-all recorded is dropped, and
    # two skip-alls that read the same `completed` compute the same set. What
    # diverges is `completed` itself: a reviewer finishing a section while this
    # runs means one caller computed `all_section_ids - completed` from a
    # `completed` that did not yet name that section, so it lands in
    # `HITLSectionsSkipped` as well as in `HITLSectionsCompleted`. The input that
    # diverges arrives from the document model, written by
    # `DocumentDynamoDBService.update_document` (one unconditional whole-document
    # write shared by the pipeline and several resolvers), so guarding this one
    # caller would look like a fix and not be one. Reported in #1111.
    update: dict[str, Any] = {
        "Key": tracking_key(object_key),
        "UpdateExpression": (
            "SET HITLStatus = :status, HITLSectionsPending = :pending, "
            "HITLSectionsSkipped = :skipped, "
            "HITLReviewHistory = list_append("
            "if_not_exists(HITLReviewHistory, :empty_history), :new_history), "
            "HITLCompleted = :hitlCompleted, HITLReviewedBy = :reviewedBy, "
            "HITLReviewedByEmail = :reviewedByEmail REMOVE HITLPendingReview"
        ),
        "ExpressionAttributeValues": {
            ":status": LegacyHitlStatus.REVIEW_SKIPPED.value,
            ":pending": [],
            ":skipped": all_skipped,
            ":empty_history": [],
            ":new_history": [review_record],
            ":hitlCompleted": True,
            ":reviewedBy": username or "unknown",
            ":reviewedByEmail": user_email or "",
        },
    }
    conditions = []
    if require_offered:
        not_finished, finished_values = not_finished_condition()
        conditions.append(not_finished)
        update["ExpressionAttributeValues"].update(finished_values)
    if require_raised:
        raised, raised_values = review_raised_condition()
        conditions.append(raised)
        update["ExpressionAttributeValues"].update(raised_values)
    if conditions:
        update["ConditionExpression"] = " AND ".join(conditions)
    try:
        table.update_item(**update)
    except Exception as e:
        if not _is_condition_failure(e):
            raise
        logger.warning(
            f"Rejecting skip-all of a review finished during it {object_key}"
        )
        raise review_finished_error("skip") from None

    logger.info(
        f"All sections skipped for document {object_key}. Skipped: {all_skipped}, "
        f"Completed: {list(completed)}"
    )
    return LegacySkipResult(
        object_key=object_key,
        skipped_sections=all_skipped,
        completed_sections=list(completed),
    )
