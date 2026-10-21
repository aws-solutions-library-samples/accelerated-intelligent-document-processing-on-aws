# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recipient expansion: explicit users plus Cognito group members, bounded."""

import functools
import logging
from typing import Any, Callable, Iterable, Iterator, Sequence

import boto3

from idp_common.notifications.exceptions import NotificationValidationError
from idp_common.notifications.keys import (
    normalize_recipient_key,
    recipient_key_from_cognito_user,
)

logger = logging.getLogger(__name__)

#: Safety limit on recipients per post (fan-out bound). Not a tuning knob.
MAX_RECIPIENTS = 1000

_PAGE_LIMIT = 60  # ListUsersInGroup maximum


@functools.lru_cache(maxsize=8)
def _cognito_client(region: str | None = None) -> Any:
    # Raw boto3 because idp_common has no cognito-idp wrapper (S.3). Created on
    # first group fan-out, never at import time.
    return boto3.client("cognito-idp", region_name=region)


def list_group_members(
    user_pool_id: str, group: str, *, client: Any = None
) -> Iterator[dict]:
    """Yield every user in a Cognito group, following ``NextToken``."""
    cognito = client or _cognito_client()
    kwargs: dict[str, Any] = {
        "UserPoolId": user_pool_id,
        "GroupName": group,
        "Limit": _PAGE_LIMIT,
    }
    while True:
        response = cognito.list_users_in_group(**kwargs)
        yield from response.get("Users", [])
        token = response.get("NextToken")
        if not token:
            return
        kwargs["NextToken"] = token


def expand_recipients(
    users: Sequence[str],
    groups: Sequence[str],
    *,
    user_pool_id: str | None,
    lister: Callable[[str, str], Iterable[dict]] | None = None,
    max_recipients: int = MAX_RECIPIENTS,
) -> tuple[list[str], bool]:
    """Recipient keys for a post, and whether the list was truncated.

    Explicit users come first, then group members (disabled users skipped),
    de-duplicated case-insensitively in order, stopping at ``max_recipients``.
    """
    if groups and not user_pool_id:
        raise NotificationValidationError("group recipients need USER_POOL_ID")
    list_members = lister or list_group_members

    seen: set[str] = set()
    ordered: list[str] = []
    truncated = False

    def add(key: str) -> bool:
        nonlocal truncated
        if key in seen:
            return True
        if len(ordered) >= max_recipients:
            truncated = True
            return False
        seen.add(key)
        ordered.append(key)
        return True

    for user in users:
        if not add(normalize_recipient_key(user)):
            break
    if not truncated:
        for group in groups:
            for member in list_members(user_pool_id or "", group):
                if member.get("Enabled") is False:
                    continue
                if not add(recipient_key_from_cognito_user(member)):
                    break
            if truncated:
                break
    if truncated:
        logger.warning(
            "event=inbox_fanout_truncated groups=%d cap=%d",
            len(groups),
            max_recipients,
        )
    return ordered, truncated
