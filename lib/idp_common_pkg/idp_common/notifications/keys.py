# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recipient keys, item keys, notification ids and list cursors.

The recipient key is the one identifier both readers (the API, from verified
claims) and producers (from a user name or a Cognito user) derive, so it must be
computed here and nowhere else.
"""

import base64
import binascii
import hashlib
import json
import re
import secrets
import time
from typing import Any, Mapping

from idp_common.notifications.exceptions import (
    InvalidCursorError,
    NotificationValidationError,
)

NOTIFICATION_ID_RE = re.compile(r"^[0-9]{13}[0-9a-f]{12}$")

PARTITION_PREFIX = "USER#"
MESSAGE_PREFIX = "N#"
DEDUPE_PREFIX = "D#"
IDEMPOTENCY_PREFIX = "I#"
PREFS_SK = "PREFS"

MAX_CURSOR_LENGTH = 256
_CURSOR_VERSION = 1


def normalize_recipient_key(value: Any) -> str:
    """Strip and lowercase a recipient identifier. Empty input is invalid.

    Lowercasing matches the case-insensitive user pool, so an Admin-typed
    ``Alice@Example.com`` reaches the user who signs in as ``alice@example.com``.
    """
    key = str(value if value is not None else "").strip().lower()
    if not key:
        raise NotificationValidationError("recipient key must not be empty")
    return key


def recipient_key_from_claims(claims: Mapping[str, Any]) -> str:
    """Recipient key from verified ID-token claims.

    Precedence is ``email``, then ``cognito:username``, then ``sub`` — the same
    identifier ``idp_common.api_adapter.normalize_event`` exposes as
    ``identity.username`` — normalized with :func:`normalize_recipient_key`.
    Pass only claims from a verified ID token, never caller-supplied values.
    """
    return normalize_recipient_key(
        claims.get("email") or claims.get("cognito:username") or claims.get("sub")
    )


def recipient_key_from_cognito_user(user: Mapping[str, Any]) -> str:
    """Recipient key from a Cognito ``ListUsersInGroup`` / ``ListUsers`` entry.

    Uses the ``email`` attribute, then ``Username``, then the ``sub``
    attribute, so it matches :func:`recipient_key_from_claims` for the same user.
    """
    attributes = {a.get("Name"): a.get("Value") for a in (user.get("Attributes") or [])}
    return normalize_recipient_key(
        attributes.get("email") or user.get("Username") or attributes.get("sub")
    )


def partition_key(rk: str) -> str:
    """Table partition key for a recipient."""
    return PARTITION_PREFIX + normalize_recipient_key(rk)


def new_notification_id(now_ms: int | None = None, rand_hex: str | None = None) -> str:
    """Time-sortable id: 13-digit epoch milliseconds + 12 lowercase hex chars."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if rand_hex is None:
        rand_hex = secrets.token_hex(6)
    return f"{now_ms:013d}{rand_hex}"


def message_sk(notification_id: str) -> str:
    return MESSAGE_PREFIX + notification_id


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def dedupe_sk(dedupe_key: str) -> str:
    return DEDUPE_PREFIX + _digest(dedupe_key)


def idempotency_sk(idempotency_key: str) -> str:
    return IDEMPOTENCY_PREFIX + _digest(idempotency_key)


def recipient_hash(rk: str) -> str:
    """Short, non-reversible recipient reference for logs."""
    return hashlib.sha256(rk.encode("utf-8")).hexdigest()[:12]


def encode_cursor(notification_id: str) -> str:
    """Opaque ``nextToken`` naming the last returned id (base64url, no padding)."""
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "a": notification_id}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> str:
    """Return the id a cursor names, or raise :class:`InvalidCursorError`."""
    if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_LENGTH:
        raise InvalidCursorError("Invalid nextToken")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except (binascii.Error, ValueError, UnicodeError):
        raise InvalidCursorError("Invalid nextToken") from None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("Invalid nextToken")
    after = payload.get("a")
    if not isinstance(after, str) or not NOTIFICATION_ID_RE.match(after):
        raise InvalidCursorError("Invalid nextToken")
    return after
