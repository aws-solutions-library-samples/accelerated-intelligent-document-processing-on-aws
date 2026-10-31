# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who the Annotators are: the members of the Cognito ``Annotator`` group.

The group is the source of truth because it is what authorizes every HITL API call
(``cognito:groups`` in the caller's token), and it is what User Management and the
external IdP mapping (``ExternalIdPAnnotatorGroupName``) write. The UsersTable
``persona`` holds one role per user, so an Admin who is also an Annotator does not
read as one there; it is used only for the Annotator's ``allowedTestSets`` scope.

No ``idp_common`` wrapper exists for Cognito, so this calls boto3 directly.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .models import UserInfo

ANNOTATOR_GROUP = "Annotator"


def _client(state: dict[str, Any], client_factory: Optional[Callable[[], Any]]):
    if "c" not in state:
        if client_factory:
            state["c"] = client_factory()
        else:
            import boto3

            state["c"] = boto3.client("cognito-idp")
    return state["c"]


def cognito_user_lookup(
    user_pool_id: str, client_factory: Optional[Callable[[], Any]] = None
) -> Callable[[str], Optional[UserInfo]]:
    """A ``user_lookup``: one user's Cognito groups, by email."""
    state: dict[str, Any] = {}

    def lookup(email: str) -> Optional[UserInfo]:
        client = _client(state, client_factory)
        safe = email.replace("\\", "").replace('"', "")
        users = client.list_users(
            UserPoolId=user_pool_id, Filter=f'email = "{safe}"', Limit=2
        ).get("Users", [])
        if not users:
            return UserInfo(email=email, exists=False)
        groups = client.admin_list_groups_for_user(
            UserPoolId=user_pool_id, Username=users[0]["Username"]
        ).get("Groups", [])
        return UserInfo(
            email=email, exists=True, groups=frozenset(g["GroupName"] for g in groups)
        )

    return lookup


def is_annotator(lookup: Callable[[str], Optional[UserInfo]], email: str) -> bool:
    info = lookup(email)
    return bool(info and info.exists and ANNOTATOR_GROUP in info.groups)


def annotator_emails(
    user_pool_id: str, client_factory: Optional[Callable[[], Any]] = None
) -> list[str]:
    """Every enabled member of the Annotator group, by email, sorted."""
    client = _client({}, client_factory)
    emails: set[str] = set()
    kwargs: dict[str, Any] = {"UserPoolId": user_pool_id, "GroupName": ANNOTATOR_GROUP}
    while True:
        page = client.list_users_in_group(**kwargs)
        for user in page.get("Users", []):
            if user.get("Enabled") is False:
                continue
            for attr in user.get("Attributes", []):
                if attr.get("Name") == "email" and attr.get("Value"):
                    emails.add(str(attr["Value"]).lower())
        token = page.get("NextToken")
        if not token:
            return sorted(emails)
        kwargs["NextToken"] = token


#: Persona precedence, highest first: the same order User Management derives a
#: user's persona from their groups in (``_determine_persona_from_groups``).
_PERSONA_ORDER = ("Admin", "Author", "Reviewer", "Annotator", "Viewer")

#: Marks a UsersTable record created from an existing Cognito account, so the
#: user-sync stream doesn't try to create that account again.
COGNITO_SOURCE = "cognito"


def persona_for_groups(groups: frozenset[str] | set[str]) -> str:
    return next((p for p in _PERSONA_ORDER if p in groups), "Viewer")


def cognito_account(
    user_pool_id: str, email: str, client_factory: Optional[Callable[[], Any]] = None
) -> Optional[dict[str, Any]]:
    """``{username, sub, groups, created_at}`` of the Cognito account for ``email``."""
    client = _client({}, client_factory)
    safe = email.replace("\\", "").replace('"', "")
    users = client.list_users(
        UserPoolId=user_pool_id, Filter=f'email = "{safe}"', Limit=2
    ).get("Users", [])
    if not users:
        return None
    user = users[0]
    groups = client.admin_list_groups_for_user(
        UserPoolId=user_pool_id, Username=user["Username"]
    ).get("Groups", [])
    sub = next(
        (a.get("Value") for a in user.get("Attributes", []) if a.get("Name") == "sub"),
        "",
    )
    created = user.get("UserCreateDate")
    return {
        "username": user["Username"],
        "sub": sub or "",
        "groups": frozenset(g["GroupName"] for g in groups),
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        if hasattr(created, "strftime")
        else None,
    }
