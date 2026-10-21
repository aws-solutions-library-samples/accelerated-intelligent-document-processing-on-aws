# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Domain exceptions for ``idp_common.review_queues``.

Each carries a stable ``code`` (for example ``ALREADY_CLAIMED``) and renders as
``"<CODE>: message"``. They deliberately do not subclass the builtins: the API
resolver maps them to exactly ``PermissionError``, ``ValueError`` or
``ResourceNotFound``, because the dispatcher maps errors by class name and a
subclass would become a 500.
"""


class ReviewQueueError(Exception):
    """Base class for review-queue errors."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ReviewAccessDenied(ReviewQueueError):
    """The caller may not perform this action (membership, scope, lease, role)."""


class ReviewConflict(ReviewQueueError):
    """The item's state doesn't allow the action (claimed, expired, stale)."""


class ReviewValidationError(ReviewQueueError):
    """An argument is invalid (bad cursor, missing reason, oversized draft)."""


class ReviewItemNotFound(ReviewQueueError):
    """The queue item, or the document behind it, doesn't exist."""
