# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The notification type catalogue.

Types are data, declared in :data:`PLATFORM_TYPES`, so the reader Lambda sees the
same catalogue as every producer without importing producer modules.
"""

import threading

from idp_common.notifications.exceptions import UnknownNotificationTypeError
from idp_common.notifications.models import NotificationType

# Production types are declared here as data; producers append. Never call
# register_type() from production code. Every type can be muted.
PLATFORM_TYPES: tuple[NotificationType, ...] = (
    # HITL review queues (idp_common.review_queues.notify is the only producer).
    NotificationType(
        id="review.queue_items_waiting",
        display_name="Review queue items waiting",
        description="A document is waiting in a review queue you work.",
        default_severity="info",
    ),
    NotificationType(
        id="review.item_reassigned",
        display_name="Review item assigned to you",
        description="An Admin assigned a review queue item or Test Studio document to you.",
        default_severity="info",
    ),
    NotificationType(
        id="review.claim_released",
        display_name="Your review claim was released",
        description="A review queue item you had claimed was released, moved or withdrawn.",
        default_severity="warning",
    ),
    NotificationType(
        id="review.claim_expiring",
        display_name="Review claim expiring",
        description="Your claim on a review queue item is about to expire.",
        default_severity="warning",
    ),
    NotificationType(
        id="review.claim_expired",
        display_name="Review claim expired",
        description="Your claim on a review queue item expired; your draft is kept.",
        default_severity="warning",
    ),
    # Admin assignment of queue items and Test Studio documents to Annotators
    # (idp_common.review_queues.testset_docs). Assignments do not expire.
    NotificationType(
        id="review.task_reminder",
        display_name="Task reminder",
        description="A document assigned to you is still open after its time limit.",
        default_severity="info",
    ),
)

_lock = threading.Lock()
_registry: dict[str, NotificationType] = {t.id: t for t in PLATFORM_TYPES}


def register_type(notification_type: NotificationType) -> None:
    """Register a type (tests and the live-check tool only).

    Idempotent for an identical definition; a conflicting redefinition raises
    ``ValueError``.
    """
    with _lock:
        existing = _registry.get(notification_type.id)
        if existing is not None and existing != notification_type:
            raise ValueError(
                f"notification type {notification_type.id!r} is already "
                "registered with a different definition"
            )
        _registry[notification_type.id] = notification_type


def unregister_type(type_id: str) -> None:
    """Remove a registered type (tests only)."""
    with _lock:
        _registry.pop(type_id, None)


def get_type(type_id: str) -> NotificationType | None:
    return _registry.get(type_id)


def require_type(type_id: str) -> NotificationType:
    found = _registry.get(type_id)
    if found is None:
        raise UnknownNotificationTypeError(f"Unknown notification type: {type_id}")
    return found


def list_types() -> list[NotificationType]:
    """Every registered type, sorted by display name, then id."""
    with _lock:
        types = list(_registry.values())
    return sorted(types, key=lambda t: (t.display_name, t.id))
