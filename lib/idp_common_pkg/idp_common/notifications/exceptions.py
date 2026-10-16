# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Domain exceptions for ``idp_common.notifications``.

Validation errors are ``ValueError`` subclasses and not-found is a
``LookupError`` subclass, so generic callers can still catch them by the
builtin class. The API resolver re-raises them as exactly ``ValueError`` /
``ResourceNotFound``, because the dispatcher maps errors by class name.
"""


class NotificationError(Exception):
    """Base class for notification errors."""


class NotificationValidationError(NotificationError, ValueError):
    """An input (request, key, link, type id, ...) is invalid."""


class UnknownNotificationTypeError(NotificationValidationError):
    """The notification type id is not in the type catalogue."""


class InvalidCursorError(NotificationValidationError):
    """A ``nextToken`` cursor is malformed."""


class NotificationNotFoundError(NotificationError, LookupError):
    """None of the requested messages exists for the caller."""
