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
    ItemCycleArgs,
    ItemKeyArgs,
    ListMyWorkArgs,
    ListQueueItemsArgs,
    MoveArgs,
    MyQueuesArgs,
    NoArgs,
    ReassignArgs,
    RejectArgs,
    ReleaseArgs,
    RemoveArgs,
    SaveDraftArgs,
    clamp_limit,
    summarize_validation_error,
)
from idp_common.review_queues.models import CallerContext, QueueRef
from idp_common.review_queues.queue_defs import DEFAULT_QUEUE_ID, QueueDefinition
from idp_common.review_queues.service import create_review_queue_service

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module scope so warm invocations reuse them; the service is None when
# HITL_TABLE_NAME is unset (a wiring fault for this function).
_SERVICE = create_review_queue_service()
_DYNAMODB = boto3.resource("dynamodb")
_SCOPE_CACHE: dict = {}

# Annotators work the queues that list them and Reviewers every Production queue: they
# claim items (or an Admin assigns them) and work their own. Admins manage the queues
# and assign the work.
_WORKERS = frozenset({"Admin", "Annotator", "Reviewer"})
_ADMIN = frozenset({"Admin"})

#: Mirrors scripts/api_rbac_expectations.yaml.
_FIELD_GROUPS: dict[str, frozenset] = {
    "listMyReviewWork": _WORKERS,
    "getReviewItem": _WORKERS,
    "saveReviewDraft": _WORKERS,
    "completeReviewItem": _WORKERS,
    "rejectReviewItem": _WORKERS,
    # Home › Queues: an Annotator lists the queues that list them, a Reviewer every
    # Production queue; either opens their items, claims one (assigning it to
    # themselves) and releases it (Skip). The service limits each to the queues the
    # caller works (`QueueDirectory.is_member`).
    "getMyReviewQueues": _WORKERS,
    "listReviewQueueItems": _WORKERS,
    "claimReviewItem": _WORKERS,
    "claimNextReviewItem": _WORKERS,
    "releaseReviewItem": _WORKERS,
    "renewReviewLease": _ADMIN,
    "removeReviewItem": _ADMIN,
    "getReviewQueuesOverview": _ADMIN,
    "forceReleaseReviewItem": _ADMIN,
    "reassignReviewItem": _ADMIN,
    "takeOverReviewItem": _ADMIN,
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
    # The service admits the item's holder and the Annotators its queue lists; for
    # anyone but the holder it is read-only (drafts and dispositions need the claim).
    detail = _SERVICE.get_item_detail(c, a.object_key)
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
    a = ReleaseArgs.model_validate(args)
    return _dump(_SERVICE.release(c, a.object_key, a.cycle_id, a.reason, a.note))


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


def _remove(c, args):
    a = RemoveArgs.model_validate(args)
    return _dump(_SERVICE.remove(c, a.object_key, a.cycle_id, a.note))


def _overview(c, args):
    NoArgs.model_validate(args)
    return _dump(_SERVICE.overview(c))


def _force_release(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.force_release(c, a.object_key, a.cycle_id))


def _reassign(c, args):
    a = ReassignArgs.model_validate(args)
    return _dump(_SERVICE.reassign(c, a.object_key, a.cycle_id, a.assignee_email))


def _take_over(c, args):
    a = ItemCycleArgs.model_validate(args)
    return _dump(_SERVICE.take_over(c, a.object_key, a.cycle_id))


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
        "skipReasons": list(d.skip_reasons),
        "timeLimitMinutes": d.time_limit_minutes,
        "eligibleAnnotators": list(d.eligible_annotators),
        "isDefaultQueue": d.id == DEFAULT_QUEUE_ID,
        "enabled": d.enabled,
        "version": d.version,
    }


def _queue_input(raw: Any, existing: QueueDefinition | None = None) -> QueueDefinition:
    """The definition an input describes.

    ``enabled`` left out keeps the ``existing`` value (an update) or takes the
    default (a new queue: enabled). A ``confidenceThreshold`` sent by an older
    client is ignored: queues have no threshold of their own.
    """
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
    # Left out, an update keeps the stored list and a new queue gets the defaults.
    if data.get("skipReasons"):
        fields["skip_reasons"] = data["skipReasons"]
    elif existing is not None:
        fields["skip_reasons"] = list(existing.skip_reasons)
    for key, name in (("enabled", "enabled"),):
        if data.get(key) is not None:
            fields[name] = data[key]
        elif existing is not None:
            fields[name] = getattr(existing, name)
    return QueueDefinition.model_validate(fields)


def _list_annotators(c, args):
    """The members of the Cognito Annotator group: who HITL work can be assigned to.
    Reviewers are never assigned work; they claim it."""
    NoArgs.model_validate(args)
    pool = os.environ.get("USER_POOL_ID", "")
    if not pool:
        raise ValueError("BAD_CONFIGURATION: USER_POOL_ID is not set")
    return annotator_emails(pool)


def _list_queues(c, args):
    NoArgs.model_validate(args)
    # A fresh, strongly consistent read past this instance's cache: the list is what
    # an Admin edits a queue from, and the version an update is checked against comes
    # from it.
    return [_queue_view(d) for d in _SERVICE.directory.queue_defs.list(fresh=True)]


def _create_queue(c, args):
    d = _queue_input(args.get("input"))
    return _queue_view(_SERVICE.directory.queue_defs.put(d, create=True))


def _expected_version(raw: Any) -> int | None:
    """The ``version`` an update was built from, or None if the caller sent none."""
    version = raw.get("version") if isinstance(raw, dict) else None
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("BAD_ARGUMENT: version must be a non-negative integer")
    return version


def _update_queue(c, args):
    """Replace a queue's definition.

    The input's ``version`` is the one the caller read; if the queue has been written
    since, the update is refused with ``QUEUE_CHANGED`` rather than undoing that write.
    A caller that sends no version (an older client) is checked against the version
    of the consistent read made here: its fields replace whatever that read found,
    and it is refused only if another write lands between that read and this one,
    which protects the fields this merges from the stored definition (``enabled``,
    ``skipReasons``).
    """
    raw = args.get("input")
    queue_id = str((raw or {}).get("id") or "") if isinstance(raw, dict) else ""
    expected = _expected_version(raw)
    defs = _SERVICE.directory.queue_defs
    defs.invalidate()
    # A consistent read, so the version taken for a caller that sent none is current;
    # get() as well, which seeds the Default queue if it was never listed.
    existing = (defs.read_current(queue_id) or defs.get(queue_id)) if queue_id else None
    d = _queue_input(raw, existing)
    if expected is None and existing is not None:
        expected = existing.version
    return _queue_view(defs.put(d, create=False, expected_version=expected))


def _delete_queue(c, args):
    """Delete a queue; its items move to the Default queue first."""
    queue_id = str(args.get("id") or "").strip().lower()
    if not queue_id:
        raise ValueError("BAD_ARGUMENT: id is required")
    _SERVICE.delete_queue(c, queue_id)
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
    "removeReviewItem": _remove,
    "getReviewQueuesOverview": _overview,
    "forceReleaseReviewItem": _force_release,
    "reassignReviewItem": _reassign,
    "takeOverReviewItem": _take_over,
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
