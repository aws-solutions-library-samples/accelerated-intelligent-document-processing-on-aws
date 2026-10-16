# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Argument models for the Inbox API operations.

The resolver validates ``event["arguments"]`` with these models. Arguments arrive
camelCase (``nextToken``, ``mutedTypes``). Unknown arguments are rejected
(``extra="forbid"``), so no operation accepts a user id: the caller's key comes
only from verified identity claims. Responses are the service models
(``MessagePage``, ``MutationResult``, ``Preferences``) dumped with
``model_dump(by_alias=True, mode="json")``.
"""

from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

from idp_common.notifications.keys import NOTIFICATION_ID_RE
from idp_common.notifications.models import (
    DEFAULT_LIST_LIMIT,
    TYPE_ID_PATTERN,
    Severity,
    clamp_list_limit,
)

#: At most this many ids per mutation (D15).
MAX_IDS_PER_MUTATION = 100
#: At most this many type filters on a list call.
MAX_TYPE_FILTERS = 20
#: At most this many muted types in one preferences update.
MAX_MUTED_TYPES = 100
#: Cursor length bound, matching ``keys.decode_cursor``.
MAX_NEXT_TOKEN_LENGTH = 256

TypeId = Annotated[StrictStr, Field(pattern=TYPE_ID_PATTERN)]


class _ArgsModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class ListArgs(_ArgsModel):
    """``listInboxMessages`` arguments."""

    read: StrictBool | None = None
    types: list[TypeId] | None = Field(default=None, max_length=MAX_TYPE_FILTERS)
    severities: list[Severity] | None = None
    limit: int = DEFAULT_LIST_LIMIT
    next_token: StrictStr | None = Field(default=None, max_length=MAX_NEXT_TOKEN_LENGTH)

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp_limit(cls, v: Any) -> int:
        # Clamped, not rejected; only a non-integer is an error.
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("limit must be an integer")
        return clamp_list_limit(v)


class IdsArgs(_ArgsModel):
    """Arguments naming message ids: 1–100, de-duplicated, order kept."""

    ids: list[StrictStr] = Field(min_length=1, max_length=MAX_IDS_PER_MUTATION)

    @field_validator("ids")
    @classmethod
    def _check_ids(cls, v: list[str]) -> list[str]:
        for i in v:
            if not NOTIFICATION_ID_RE.match(i):
                raise ValueError("each id must be a message id")
        return list(dict.fromkeys(v))


class MarkArgs(IdsArgs):
    """``markInboxMessages`` arguments."""

    read: StrictBool


class PrefsArgs(_ArgsModel):
    """``updateInboxPreferences`` arguments."""

    muted_types: list[TypeId] = Field(max_length=MAX_MUTED_TYPES)


def summarize_validation_error(error: ValidationError) -> str:
    """A 400 message naming each bad argument and why, never its value."""
    parts = []
    for err in error.errors(include_input=False, include_url=False):
        loc = ".".join(map(str, err["loc"])) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts) or "invalid arguments"
