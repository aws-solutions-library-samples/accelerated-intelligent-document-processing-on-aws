# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HITL review queues: routing, queue items, claims, dispositions and finalize.

Importing the package is light: it exposes the status vocabulary
(:mod:`.statuses`) and the domain exceptions (:mod:`.exceptions`) eagerly and
loads every other submodule on first attribute access. See ``README.md``.
"""

import importlib

from .exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
    ReviewQueueError,
    ReviewValidationError,
)
from .statuses import (
    LEGACY_DONE,
    LEGACY_PENDING,
    LIVE_ITEM_STATES,
    TERMINAL_ITEM_STATES,
    TRACKING_REVIEW_ATTRIBUTES,
    HitlView,
    LegacyHitlStatus,
    QueueItemState,
    ReviewStatus,
    normalize_hitl_status,
    review_status_for_state,
)

#: Submodules loaded on first access as ``idp_common.review_queues.<name>``.
_LAZY = (
    "annotators",
    "keys",
    "models",
    "api_models",
    "store",
    "directory",
    "reasons",
    "routing",
    "service",
    "status_object",
    "finalize",
    "notify",
    "tracker",
    "sweeper",
    "queue_defs",
    "testset_docs",
    "section_writes",
    "testset_labels",
)


def __getattr__(name):
    if name in _LAZY:
        try:
            return importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                raise AttributeError(
                    f"module {__name__!r} has no attribute {name!r}"
                ) from None
            raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "HitlView",
    "LEGACY_DONE",
    "LEGACY_PENDING",
    "LIVE_ITEM_STATES",
    "LegacyHitlStatus",
    "QueueItemState",
    "ReviewAccessDenied",
    "ReviewConflict",
    "ReviewItemNotFound",
    "ReviewQueueError",
    "ReviewStatus",
    "ReviewValidationError",
    "TERMINAL_ITEM_STATES",
    "TRACKING_REVIEW_ATTRIBUTES",
    "normalize_hitl_status",
    "review_status_for_state",
]
