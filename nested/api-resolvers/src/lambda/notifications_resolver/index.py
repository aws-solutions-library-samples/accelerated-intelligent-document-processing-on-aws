# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Inbox resolver: serves the caller's own Inbox messages and preferences.

A thin dispatcher over ``idp_common.notifications.NotificationService``. Every
operation is self-scoped: the partition key is built from the verified ID-token
claims (``recipient_key_from_claims``) and no operation accepts a user argument.

Errors leave as exactly ``ValueError`` (400), ``ResourceNotFound`` (404) or
``PermissionError`` (403), because the dispatcher maps them by class name.
"""

import logging
import os
from typing import Any, Callable

from pydantic import ValidationError

from idp_common.api_adapter import ResourceNotFound
from idp_common.notifications import (
    create_notification_service,
    recipient_key_from_claims,
)
from idp_common.notifications.api_models import (
    IdsArgs,
    ListArgs,
    MarkArgs,
    PrefsArgs,
    summarize_validation_error,
)
from idp_common.notifications.exceptions import (
    NotificationNotFoundError,
    NotificationValidationError,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module scope so warm invocations reuse it; None when NOTIFICATIONS_TABLE_NAME
# is unset (a wiring fault for this function).
_SERVICE = create_notification_service()


def _caller_key(event: dict) -> str:
    """The caller's recipient key, from verified identity claims only."""
    claims = (event.get("identity") or {}).get("claims") or {}
    if not claims.get("cognito:groups"):
        logger.warning("Rejecting Inbox request: caller holds no group")
        raise PermissionError("Unauthorized: caller holds no group")
    try:
        return recipient_key_from_claims(claims)
    except NotificationValidationError:
        logger.warning("Rejecting Inbox request: no caller identity in claims")
        raise PermissionError("Unauthorized: no caller identity") from None


def _no_args(args: dict) -> None:
    if args:
        raise ValueError(
            "This operation takes no arguments: " + ", ".join(sorted(args))
        )


def _list(caller_key: str, args: dict) -> Any:
    a = ListArgs.model_validate(args)
    page = _SERVICE.list_messages(
        caller_key,
        read=a.read,
        types=a.types,
        severities=a.severities,
        limit=a.limit,
        next_token=a.next_token,
    )
    return page.model_dump(by_alias=True, mode="json")


def _count(caller_key: str, args: dict) -> Any:
    _no_args(args)
    return _SERVICE.unread_count(caller_key)


def _get_prefs(caller_key: str, args: dict) -> Any:
    _no_args(args)
    return _SERVICE.get_preferences(caller_key).model_dump(by_alias=True, mode="json")


def _mark(caller_key: str, args: dict) -> Any:
    a = MarkArgs.model_validate(args)
    result = _SERVICE.mark(caller_key, a.ids, read=a.read)
    return result.model_dump(by_alias=True, mode="json")


def _mark_all(caller_key: str, args: dict) -> Any:
    _no_args(args)
    return _SERVICE.mark_all_read(caller_key).model_dump(by_alias=True, mode="json")


def _delete(caller_key: str, args: dict) -> Any:
    a = IdsArgs.model_validate(args)
    return _SERVICE.delete(caller_key, a.ids).model_dump(by_alias=True, mode="json")


def _clear_read(caller_key: str, args: dict) -> Any:
    _no_args(args)
    return _SERVICE.clear_read(caller_key).model_dump(by_alias=True, mode="json")


def _update_prefs(caller_key: str, args: dict) -> Any:
    a = PrefsArgs.model_validate(args)
    prefs = _SERVICE.update_preferences(caller_key, a.muted_types)
    return prefs.model_dump(by_alias=True, mode="json")


_FIELDS: dict[str, Callable[[str, dict], Any]] = {
    "listInboxMessages": _list,
    "getInboxUnreadCount": _count,
    "getInboxPreferences": _get_prefs,
    "markInboxMessages": _mark,
    "markAllInboxMessagesRead": _mark_all,
    "deleteInboxMessages": _delete,
    "clearReadInboxMessages": _clear_read,
    "updateInboxPreferences": _update_prefs,
}


def handler(event, context):
    field = (event.get("info") or {}).get("fieldName", "")
    args = event.get("arguments") or {}
    # Field and argument keys only: never identity, claims, titles or bodies.
    logger.info("Inbox resolver field=%s argKeys=%s", field, sorted(args))
    fn = _FIELDS.get(field)
    if fn is None:
        raise ValueError(f"Unsupported field: {field}")
    if _SERVICE is None:
        raise RuntimeError("Inbox store is not configured")
    caller_key = _caller_key(event)
    try:
        return fn(caller_key, args)
    except ValidationError as e:
        raise ValueError(summarize_validation_error(e)) from None
    except NotificationValidationError as e:
        raise ValueError(str(e)) from None
    except NotificationNotFoundError as e:
        raise ResourceNotFound(str(e)) from None
