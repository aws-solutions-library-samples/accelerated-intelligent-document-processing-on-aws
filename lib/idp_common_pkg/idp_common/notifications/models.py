# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pydantic contracts for ``idp_common.notifications``.

Response models (``InboxMessage``, ``MessagePage``, ``MutationResult``,
``Preferences``) carry camelCase aliases for the API:
``model_dump(by_alias=True, mode="json")``.
"""

import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from idp_common.notifications.keys import normalize_recipient_key
from idp_common.notifications.links import validate_link

Severity = Literal["info", "success", "warning", "error"]

TYPE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{2,63}$"
SOURCE_PATTERN = r"^[a-z0-9][a-z0-9_.:/-]{0,99}$"
GROUP_PATTERN = r"^[\w+=,.@-]{1,128}$"
_METADATA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_GROUP_RE = re.compile(GROUP_PATTERN)

MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 4000
MAX_METADATA_KEYS = 20
MAX_METADATA_BYTES = 2048
MAX_USERS = 100
MAX_GROUPS = 10

#: ``list_messages`` page size: the default and the clamp bounds (D15).
DEFAULT_LIST_LIMIT = 20
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 100

MetadataValue = str | int | float | bool | None


def clamp_list_limit(limit: int | None) -> int:
    """The page size to use: ``None`` → the default, otherwise clamped to 1–100.

    The one place the clamp lives; ``ListArgs`` and ``NotificationService``
    both call it. Out-of-range values are clamped, not rejected.
    """
    if limit is None:
        return DEFAULT_LIST_LIMIT
    return max(MIN_LIST_LIMIT, min(MAX_LIST_LIMIT, int(limit)))


def metadata_json(metadata: dict[str, MetadataValue]) -> str:
    """Compact JSON form stored as ``MetadataJson`` and size-checked."""
    return json.dumps(metadata, separators=(",", ":"))


class NotificationType(BaseModel):
    """A catalogue entry: what a message is about, and whether a user can mute it."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=TYPE_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    default_severity: Severity = "info"


class NotificationRequest(BaseModel):
    """What a producer asks to deliver."""

    type_id: str
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    body: str = Field(default="", max_length=MAX_BODY_LENGTH)
    severity: Severity | None = None
    link: str | None = None
    source: str = Field(pattern=SOURCE_PATTERN)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    metadata: dict[str, MetadataValue] | None = None
    users: list[str] = Field(default_factory=list, max_length=MAX_USERS)
    groups: list[str] = Field(default_factory=list, max_length=MAX_GROUPS)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if any(unicodedata.category(ch) == "Cc" for ch in v):
                raise ValueError("title must not contain control characters")
        return v

    @field_validator("body")
    @classmethod
    def _check_body(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("body must not contain NUL characters")
        return v

    @field_validator("link")
    @classmethod
    def _check_link(cls, v: str | None) -> str | None:
        return None if v is None else validate_link(v)

    @field_validator("metadata")
    @classmethod
    def _check_metadata(
        cls, v: dict[str, MetadataValue] | None
    ) -> dict[str, MetadataValue] | None:
        if v is None:
            return None
        if len(v) > MAX_METADATA_KEYS:
            raise ValueError(f"metadata allows at most {MAX_METADATA_KEYS} keys")
        for key in v:
            if not _METADATA_KEY_RE.match(key):
                raise ValueError(f"invalid metadata key {key!r}")
        if len(metadata_json(v).encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata exceeds {MAX_METADATA_BYTES} bytes as JSON")
        return v

    @field_validator("users")
    @classmethod
    def _normalize_users(cls, v: list[str]) -> list[str]:
        return [normalize_recipient_key(u) for u in v]

    @field_validator("groups")
    @classmethod
    def _check_groups(cls, v: list[str]) -> list[str]:
        for group in v:
            if not _GROUP_RE.match(group):
                raise ValueError(f"invalid group name {group!r}")
        return v

    @model_validator(mode="after")
    def _require_recipients(self) -> "NotificationRequest":
        if not self.users and not self.groups:
            raise ValueError("at least one of users or groups is required")
        return self


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class InboxMessage(_ApiModel):
    """One message as the recipient sees it (no dedupe key, no recipient)."""

    id: str
    type: str
    title: str
    body: str
    severity: Severity
    link: str | None = None
    source: str
    created_at: str
    read: bool
    read_at: str | None = None
    metadata: str | None = None


class MessagePage(_ApiModel):
    items: list[InboxMessage]
    next_token: str | None = None
    unread_count: int


class MutationResult(_ApiModel):
    updated: int
    not_found_ids: list[str] = Field(default_factory=list)
    unread_count: int


class TypeWithMute(_ApiModel):
    id: str
    display_name: str
    description: str
    default_severity: Severity
    muted: bool


class Preferences(_ApiModel):
    types: list[TypeWithMute]
    muted_types: list[str]
    updated_at: str | None = None


class Delivery(BaseModel):
    recipient_hash: str
    notification_id: str | None = None
    outcome: Literal["delivered", "muted", "duplicate", "failed"]


class PostResult(BaseModel):
    deliveries: list[Delivery]
    recipients_considered: int
    truncated: bool

    def _count(self, outcome: str) -> int:
        return sum(1 for d in self.deliveries if d.outcome == outcome)

    @property
    def delivered(self) -> int:
        return self._count("delivered")

    @property
    def muted(self) -> int:
        return self._count("muted")

    @property
    def duplicates(self) -> int:
        return self._count("duplicate")

    @property
    def failed(self) -> int:
        return self._count("failed")


class NotificationSettings(BaseModel):
    """Producer-side settings read from ``hitl.notifications``."""

    retention_days: int = 30
    max_per_user: int = 50


class TrimReport(BaseModel):
    """What one cap trim removed from a recipient's partition."""

    expired_deleted: int = 0
    read_evicted: int = 0
    unread_evicted: int = 0
