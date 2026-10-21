# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Host Inbox notifications: per-user messages posted by platform Lambdas.

Producers call :func:`notify`; the API resolver uses :class:`NotificationService`
through :func:`create_notification_service`. See ``README.md`` in this package.

Importing this package is cheap: models, keys, links and the catalogue need only
Pydantic. The service (which pulls in boto3 through ``idp_common.dynamodb``) is
loaded lazily on first access to ``NotificationService``,
``create_notification_service`` or ``notify``.
"""

import importlib
from typing import TYPE_CHECKING, Any

from idp_common.notifications.catalogue import (
    PLATFORM_TYPES,
    get_type,
    list_types,
    register_type,
)
from idp_common.notifications.exceptions import (
    InvalidCursorError,
    NotificationError,
    NotificationNotFoundError,
    NotificationValidationError,
    UnknownNotificationTypeError,
)
from idp_common.notifications.keys import (
    normalize_recipient_key,
    recipient_key_from_claims,
    recipient_key_from_cognito_user,
)
from idp_common.notifications.links import (
    document_detail_link,
    encode_uri_component,
    validate_link,
)
from idp_common.notifications.models import (
    InboxMessage,
    MessagePage,
    MutationResult,
    NotificationRequest,
    NotificationSettings,
    NotificationType,
    PostResult,
    Preferences,
)

if TYPE_CHECKING:
    from idp_common.notifications.service import (
        NotificationService as NotificationService,
    )
    from idp_common.notifications.service import (
        create_notification_service as create_notification_service,
    )
    from idp_common.notifications.service import notify as notify

_LAZY_SERVICE_NAMES = frozenset(
    {"NotificationService", "create_notification_service", "notify"}
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_SERVICE_NAMES:
        service = importlib.import_module("idp_common.notifications.service")
        return getattr(service, name)
    raise AttributeError(f"module 'idp_common.notifications' has no attribute '{name}'")


__all__ = [
    "NotificationService",
    "create_notification_service",
    "notify",
    "NotificationRequest",
    "NotificationType",
    "InboxMessage",
    "MessagePage",
    "MutationResult",
    "Preferences",
    "PostResult",
    "NotificationSettings",
    "register_type",
    "list_types",
    "get_type",
    "PLATFORM_TYPES",
    "recipient_key_from_claims",
    "recipient_key_from_cognito_user",
    "normalize_recipient_key",
    "document_detail_link",
    "encode_uri_component",
    "validate_link",
    "NotificationError",
    "NotificationValidationError",
    "UnknownNotificationTypeError",
    "InvalidCursorError",
    "NotificationNotFoundError",
]
