# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review queues resolver: queue lists, claims, drafts, dispositions, supervision.

A thin dispatcher over ``idp_common.review_queues.ReviewQueueService``. The
caller comes only from verified identity claims (``_caller_context``); no
operation accepts a user argument. Errors leave as exactly ``PermissionError``
(403), ``ValueError`` (400) or ``ResourceNotFound`` (404), because the dispatcher
maps them by class name.
"""

import logging
import os
from typing import Any, Callable

import boto3
from pydantic import ValidationError

from idp_common.api_adapter import ResourceNotFound
from idp_common.config_scope import (
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
)
from idp_common.notifications import recipient_key_from_claims
from idp_common.review_queues import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
    ReviewValidationError,
)
from idp_common.review_queues.annotators import annotator_emails
from idp_common.review_queues.api_models import (
    ClaimNextArgs,
    CompleteArgs,
    EscalateArgs,
    ItemCycleArgs,
    ItemKeyArgs,
    ListMyWorkArgs,
    ListQueueItemsArgs,
    MoveArgs,
    MyQueuesArgs,
    NoArgs,
    ReassignArgs,
    RejectArgs,
    SaveDraftArgs,
    clamp_limit,
    summarize_validation_error,
)
from idp_common.review_queues.models import CallerContext, QueueRef
from idp_common.review_queues.queue_defs import (
    DEFAULT_QUEUE_ID,
    ESCALATION_QUEUE_ID,
    QueueDefinition,
)
from idp_common.review_queues.service import create_review_queue_service

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module scope so warm invocations reuse them; the service is None when
# HITL_TABLE_NAME is unset (a wiring fault for this function).
_SERVICE = create_review_queue_service()
_DYNAMODB = boto3.resource("dynamodb")
_SCOPE_CACHE: dict = {}

# Annotators work the items an Admin assigned to them; Admins manage the queues
# and assign the work. Nobody claims an item themselves.
_WORKERS = frozenset({"Admin", "Annotator"})
_ADMIN = frozenset({"Admin"})

#: Mirrors scripts/api_rbac_expectations.yaml.
_FIELD_GROUPS: dict[str, frozenset] = {
    "listMyReviewWork": _WORKERS,
    "getReviewItem": _WORKERS,
    "saveReviewDraft": _WORKERS,
    "completeReviewItem": _WORKERS,
    "rejectReviewItem": _WORKERS,
    "getMyReviewQueues": _ADMIN,
    "listReviewQueueItems": _ADMIN,
    "claimReviewItem": _ADMIN,
    "claimNextReviewItem": _ADMIN,
    "renewReviewLease": _ADMIN,
    "releaseReviewItem": _ADMIN,
    "escalateReviewItem": _ADMIN,
    "getReviewQueuesOverview": _ADMIN,
    "forceReleaseReviewItem": _ADMIN,
    "reassignReviewItem": _ADMIN,
    "moveReviewItem": _ADMIN,
    "listReviewQueues": _ADMIN,
    "listAnnotators": _ADMIN,
    "createReviewQueue": _ADMIN,
    "updateReviewQueue": _ADMIN,
    "deleteReviewQueue": _ADMIN,
}


def _groups(event: dict) -> list:
    claims = (event.get("identity") or {}).get("claims") or {}
    groups = claims.get("cognito:groups") or []
    return [groups] if isinstance(groups, str) else list(groups)


def _caller_context(event: dict) -> CallerContext:
    """The caller, from verified claims only, with their config-version scope.

    The scope (``allowed_config_versions``) fails closed: a lookup that can't be
    evaluated refuses the request.
    """
    claims = (event.get("identity") or {}).get("claims") or {}
    groups = frozenset(_groups(event))
    try:
        reviewer_key = recipient_key_from_claims(claims)
    except Exception:
        logger.warning("Rejecting review-queue request code=ROLE: no caller identity")
        raise PermissionError("ROLE: no caller identity") from None
    email = caller_email_from_claims(claims).lower()
    is_admin = "Admin" in groups
    allowed_config_versions = None
    if not is_admin:
        try:
            allowed_config_versions = resolve_allowed_config_versions(
                email,
                users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
                dynamodb=_DYNAMODB,
                caller_sub=caller_sub_from_claims(claims),
                cache=_SCOPE_CACHE,
            )
        except Exception:
            logger.warning(
                "Rejecting review-queue request code=OUT_OF_SCOPE: scope lookup failed"
            )
            raise PermissionError("OUT_OF_SCOPE: scope lookup failed") from None
    return CallerContext(
        reviewer_key=reviewer_key,
        email=email,
        groups=groups,
        is_admin=is_admin,
        allowed_versions=allowed_config_versions,
    )


def _ref(profile, name):
    """A queue named by the call; queues are system-wide, the profile is ignored."""
    return None if not name else QueueRef(queue=name)


def _dump(model) -> Any:
    return model.model_dump(by_alias=True, mode="json")


def _my_queues(c, args):
    a = MyQueuesArgs.model_validate(args)
    return _dump(_SERVICE.get_my_queues(c, item_limit=clamp_limit(a.item_limit)))


def _list_items(c, args):
    a = ListQueueItemsArgs.model_validate(args)
    page = _SERVICE.list_queue_items(
        c,
        _ref(a.queue_profile, a.queue_name),
        claimable=a.claimable,
        limit=clamp_limit(a.limit),
        cursor=a.next_token,
    )
    return _dump(page)


def _my_work(c, args):
    a = ListMyWorkArgs.model_validate(args)
    return _dump(
        _SERVICE.list_my_work(c, limit=clamp_limit(a.limit), cursor=a.next_token)
    )


def _get_item(c, args):
    a = ItemKeyArgs.model_validate(args)
    detail = _SERVICE.get_item_detail(c, a.object_key)
    # An Annotator opens only the items assigned to them.
    if not c.is_admin and not detail.item.is_mine:
        logger.warning("Rejecting review-queue request code=NOT_ASSIGNED")
        raise PermissionError("NOT_ASSIGNED: this item isn't assigned to you")
    return _dump(detail)


def _claim(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.claim(c, a.object_key, a.cycle_id))


def _claim_next(c, args):
    a = ClaimNextArgs.model_validate(args)
    return _dump(_SERVICE.claim_next(c, _ref(a.queue_profile, a.queue_name)))


def _renew(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.renew(c, a.object_key, a.cycle_id))


def _release(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.release(c, a.object_key, a.cycle_id))


def _save_draft(c, args):
    a = SaveDraftArgs.model_validate(args)
    return _dump(
        _SERVICE.save_draft(
            c, a.object_key, a.cycle_id, a.section_id, a.data, a.base_revision
        )
    )


def _complete(c, args):
    a = CompleteArgs.model_validate(args)
    return _dump(_SERVICE.complete(c, a.object_key, a.cycle_id, a.checklist, a.note))


def _reject(c, args):
    a = RejectArgs.model_validate(args)
    return _dump(_SERVICE.reject(c, a.object_key, a.cycle_id, a.reason, a.note))


def _escalate(c, args):
    a = EscalateArgs.model_validate(args)
    return _dump(_SERVICE.escalate(c, a.object_key, a.cycle_id, a.note))


def _overview(c, args):
    NoArgs.model_validate(args)
    return _dump(_SERVICE.overview(c))


def _force_release(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.force_release(c, a.object_key, a.cycle_id))


def _reassign(c, args):
    a = ReassignArgs.model_validate(args)
    return _dump(_SERVICE.reassign(c, a.object_key, a.cycle_id, a.assignee_email))


def _move(c, args):
    a = MoveArgs.model_validate(args)
    return _dump(_SERVICE.move(c, a.object_key, a.cycle_id, a.target_queue))


# ------------------------------------------------- system-wide queue definitions


def _queue_view(d: QueueDefinition) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description or None,
        "priority": d.priority,
        "checklist": list(d.checklist),
        "rejectReasons": list(d.reject_reasons),
        "timeLimitMinutes": d.time_limit_minutes,
        "eligibleAnnotators": list(d.eligible_annotators),
        "isDefaultQueue": d.id == DEFAULT_QUEUE_ID,
        "isEscalationQueue": d.id == ESCALATION_QUEUE_ID,
    }


def _queue_input(raw: Any) -> QueueDefinition:
    data = raw if isinstance(raw, dict) else {}
    fields = {
        "id": data.get("id"),
        "name": data.get("name") or "",
        "description": data.get("description") or "",
        "priority": data.get("priority", 50),
        "checklist": data.get("checklist") or [],
        "time_limit_minutes": data.get("timeLimitMinutes", 60),
        "eligible_annotators": data.get("eligibleAnnotators") or [],
    }
    if data.get("rejectReasons"):
        fields["reject_reasons"] = data["rejectReasons"]
    return QueueDefinition.model_validate(fields)


def _list_annotators(c, args):
    """The members of the Cognito Annotator group: who HITL work can go to."""
    NoArgs.model_validate(args)
    pool = os.environ.get("USER_POOL_ID", "")
    if not pool:
        raise ValueError("BAD_CONFIGURATION: USER_POOL_ID is not set")
    return annotator_emails(pool)


def _list_queues(c, args):
    NoArgs.model_validate(args)
    return [_queue_view(d) for d in _SERVICE.directory.queue_defs.list()]


def _create_queue(c, args):
    d = _queue_input(args.get("input"))
    return _queue_view(_SERVICE.directory.queue_defs.put(d, create=True))


def _update_queue(c, args):
    d = _queue_input(args.get("input"))
    return _queue_view(_SERVICE.directory.queue_defs.put(d, create=False))


def _delete_queue(c, args):
    queue_id = str(args.get("id") or "").strip().lower()
    if not queue_id:
        raise ValueError("BAD_ARGUMENT: id is required")
    if _SERVICE.store.count_queue(QueueRef(queue=queue_id), open_only=False) > 0:
        raise ReviewValidationError(
            "QUEUE_NOT_EMPTY", "move or complete this queue's items before deleting it"
        )
    _SERVICE.directory.queue_defs.delete(queue_id)
    return True


_FIELDS: dict[str, Callable[[CallerContext, dict], Any]] = {
    "getMyReviewQueues": _my_queues,
    "listReviewQueueItems": _list_items,
    "listMyReviewWork": _my_work,
    "getReviewItem": _get_item,
    "claimReviewItem": _claim,
    "claimNextReviewItem": _claim_next,
    "renewReviewLease": _renew,
    "releaseReviewItem": _release,
    "saveReviewDraft": _save_draft,
    "completeReviewItem": _complete,
    "rejectReviewItem": _reject,
    "escalateReviewItem": _escalate,
    "getReviewQueuesOverview": _overview,
    "forceReleaseReviewItem": _force_release,
    "reassignReviewItem": _reassign,
    "moveReviewItem": _move,
    "listReviewQueues": _list_queues,
    "listAnnotators": _list_annotators,
    "createReviewQueue": _create_queue,
    "updateReviewQueue": _update_queue,
    "deleteReviewQueue": _delete_queue,
}


def handler(event, context):
    field = (event.get("info") or {}).get("fieldName", "")
    args = event.get("arguments") or {}
    # Field and argument keys only: never identity, claims, notes or draft data.
    logger.info("Review queues resolver field=%s argKeys=%s", field, sorted(args))
    fn = _FIELDS.get(field)
    if fn is None:
        raise ValueError("BAD_ARGUMENT: unsupported field")
    if not _FIELD_GROUPS[field].intersection(_groups(event)):
        logger.warning("Rejecting review-queue request field=%s code=ROLE", field)
        raise PermissionError(
            "ROLE: this operation requires one of "
            + ", ".join(sorted(_FIELD_GROUPS[field]))
        )
    if _SERVICE is None:
        raise RuntimeError("Review queues are not configured")
    caller = _caller_context(event)
    try:
        return fn(caller, args)
    except ValidationError as e:
        raise ValueError("BAD_ARGUMENT: " + summarize_validation_error(e)) from None
    except ReviewAccessDenied as e:
        logger.warning("Rejecting review-queue request field=%s code=%s", field, e.code)
        raise PermissionError(str(e)) from None
    except (ReviewConflict, ReviewValidationError) as e:
        logger.info("Refused review-queue request field=%s code=%s", field, e.code)
        raise ValueError(str(e)) from None
    except ReviewItemNotFound as e:
        raise ResourceNotFound(str(e)) from None
