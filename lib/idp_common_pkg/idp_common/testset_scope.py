# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side enforcement of per-user test-set scope (``allowedTestSets``).

An ``Annotator`` may read and annotate only the test sets named in their
``allowedTestSets``, enforced in every resolver that touches test-set documents.
This is a security boundary, not a UI convention: annotators are often external
contractors onboarded to label a single test set.

A queue deep-link is not a credential — the URL only navigates, and access is
gated by the scoped Cognito session. That holds only if the server checks scope on
every operation, so the assertion helper raises rather than returning a falsy
value a caller could forget to check.

Lives in ``idp_common`` because three separate deploy artifacts enforce the same
rule (the test-set resolver, the HITL review Lambda, and the queue); a scope check
that drifts between them is a privilege-escalation bug.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Roles that are never test-set scoped. Admins and Authors own the test sets and
# need to see all of them; scoping them would break test-set management itself.
UNSCOPED_GROUPS = ("Admin", "Author")

# The role this scope exists for.
ANNOTATOR_GROUP = "Annotator"

# Scope lookups happen on every queue read and every review operation, so they are
# cached briefly per Lambda container. The TTL bounds how long a *revoked*
# annotator keeps access, hence minutes rather than hours.
_SCOPE_CACHE_TTL_SECONDS = 300

# An *empty* scope is cached far more briefly, because the two directions are not
# symmetric: being slow to revoke trades safely against read cost, but being slow
# to grant leaves a newly-assigned annotator denied for the full TTL.
_EMPTY_SCOPE_CACHE_TTL_SECONDS = 10
_scope_cache: Dict[str, Dict[str, Any]] = {}


class TestSetAccessDenied(Exception):
    """Raised when a caller may not touch the requested test set.

    A distinct type so resolvers can map it to a 403 rather than a 500, and so a
    scope failure is never confused with a missing-record error.
    """

    # Stops pytest collecting this as a test class on the strength of its "Test"
    # prefix.
    __test__ = False


def caller_groups(event: Optional[Dict[str, Any]]) -> List[str]:
    """Cognito groups from an AppSync-shaped event, normalized to a list."""
    identity = (event or {}).get("identity") or {}
    groups = (identity.get("claims") or {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return list(groups)


def caller_email(event: Optional[Dict[str, Any]]) -> str:
    """Caller's email, which is how users are keyed in the users table."""
    identity = (event or {}).get("identity") or {}
    claims = identity.get("claims") or {}
    return (
        claims.get("email")
        or identity.get("username")
        or claims.get("cognito:username")
        or ""
    )


def is_direct_invoke(event: Optional[Dict[str, Any]]) -> bool:
    """True for a trusted service-to-service invoke (no Cognito identity).

    A direct Lambda invoke carries no ``identity`` and is gated by IAM on the
    function ARN instead of by Cognito groups.
    """
    return (event or {}).get("identity") is None


def get_allowed_test_sets(email: str, users_table: Any = None) -> Optional[List[str]]:
    """The test sets this user is restricted to, or None if unrestricted.

    Returns None — meaning "no restriction" — when the users table isn't
    configured, the user has no record, or the record carries no
    ``allowedTestSets``. That default is safe here only because
    ``assert_can_access_test_set`` requires an *explicit* scope for Annotators;
    an unscoped Annotator is denied everything rather than granted everything.
    """
    if not email:
        return None

    now = time.time()
    cached = _scope_cache.get(email)
    if cached:
        ttl = (
            _SCOPE_CACHE_TTL_SECONDS
            if cached["scope"]
            else _EMPTY_SCOPE_CACHE_TTL_SECONDS
        )
        if (now - cached["timestamp"]) < ttl:
            return cached["scope"]

    scope: Optional[List[str]] = None
    table = users_table
    if table is None:
        table_name = os.environ.get("USERS_TABLE_NAME", "")
        if table_name:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)

    if table is not None:
        try:
            from boto3.dynamodb.conditions import Key

            response = table.query(
                IndexName="EmailIndex",
                KeyConditionExpression=Key("email").eq(email),
            )
            items = response.get("Items") or []
            if items:
                stored = items[0].get("allowedTestSets")
                if stored:
                    scope = [str(s) for s in stored]
        except Exception as e:  # noqa: BLE001
            # Fails closed: Annotators are denied when scope is None, so a lookup
            # failure removes access rather than granting it.
            logger.warning("Could not look up test-set scope for %s: %s", email, e)

    _scope_cache[email] = {"scope": scope, "timestamp": now}
    return scope


def clear_scope_cache() -> None:
    """Drop cached scopes (used by tests and after a scope change)."""
    _scope_cache.clear()


def assert_can_access_test_set(
    event: Optional[Dict[str, Any]],
    test_set_id: str,
    users_table: Any = None,
) -> None:
    """Raise ``TestSetAccessDenied`` unless the caller may touch this test set.

    The rules, in order:

    1. A direct (IAM-gated) invoke is trusted — no Cognito identity to scope.
    2. Admin and Author are unscoped: they own test sets.
    3. An Annotator must have ``test_set_id`` in their ``allowedTestSets``. An
       Annotator with *no* scope is denied rather than unrestricted, so a
       misconfigured or half-created annotator fails closed.
    4. Any other role (e.g. Reviewer, Viewer) is refused test-set annotation
       access; production HITL review is a different axis and grants nothing here.
    """
    if is_direct_invoke(event):
        return

    groups = caller_groups(event)
    if any(group in groups for group in UNSCOPED_GROUPS):
        return

    email = caller_email(event)

    if ANNOTATOR_GROUP in groups:
        allowed = get_allowed_test_sets(email, users_table)
        if not allowed:
            logger.warning(
                "Forbidden: annotator %s has no allowedTestSets scope", email
            )
            raise TestSetAccessDenied(
                "Unauthorized: this account is not assigned to any test set"
            )
        if test_set_id not in allowed:
            logger.warning(
                "Forbidden: annotator %s is not scoped to test set '%s'",
                email,
                test_set_id,
            )
            raise TestSetAccessDenied(
                f"Unauthorized: not assigned to test set '{test_set_id}'"
            )
        return

    logger.warning(
        "Forbidden: caller %s (groups=%s) attempted test-set annotation access",
        email,
        groups,
    )
    raise TestSetAccessDenied(
        "Unauthorized: test-set annotation requires Admin, Author or a scoped "
        "Annotator account"
    )
