# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side matching of per-user configuration-profile scope
(``allowedConfigVersions``).

A non-admin user may optionally be restricted to a set of Configuration Profiles.
That scope decides two things: which profiles they can read or edit, and which
*documents* they can see (a document is stamped with the profile it was processed
under). Both are security boundaries.

Lives in ``idp_common`` because the same rule is enforced in half a dozen
separate deploy artifacts — the configuration resolver, both document-list
resolvers, the reprocess resolver, the BDA sync resolver, and the document chat
processor. A scope check that drifts between them is a privilege-escalation bug,
which is exactly what happened before this module existed: the document-list
resolvers admitted any document whose ``ConfigVersion`` was absent, while the
configuration resolver denied an absent version.

Two rules, both deliberate:

- **An empty or unset scope means unrestricted.** This is the default for most
  users and must stay that way; scoping is opt-in per user.
- **A set scope fails closed.** A document or profile with no name to match
  against is denied, not admitted. An unnamed object cannot be proven in scope,
  and "cannot prove" must not mean "allow" on a security boundary.

Entries may be exact names (``lending``) or glob patterns (``lending-*``,
``uc?-prod``). Patterns exist because deployments predating revision history
encode lineage in the *name* (``usecaseA_v1``, ``usecaseA_v2``, …), so scoping a
user to a use case otherwise means re-granting on every iteration. Only an admin
can set a scope entry and only an admin can create a profile, so a pattern
cannot be used to widen one's own access.

Matching is only half of the rule. The other half is *whose* scope is being
matched, and that is :func:`resolve_allowed_config_versions` — the fail-closed
UsersTable lookup every consumer must use rather than writing its own. It lives
here for the same reason ``scope_allows`` does: a lookup that drifts between call
sites is the same privilege-escalation bug as a matcher that drifts, and it had
already drifted into eight near-copies, each of which read an unresolvable caller
as an *unrestricted* one.
"""

from __future__ import annotations

import logging
import time
from fnmatch import fnmatchcase
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Characters that make a scope entry a glob rather than a literal name.
_GLOB_CHARS = ("*", "?", "[")

# The UsersTable GSI the scope lookup reads, and the attribute it is keyed on.
# Named here so every consumer resolves the same index from one symbol: an index
# name is just a string, a wrong one raises only at runtime, and unit suites stub
# the DynamoDB layer — which is how a query against an index no template declared
# survived in the chat processor.
USERS_TABLE_SCOPE_INDEX = "EmailIndex"
USERS_TABLE_SCOPE_KEY = "email"

# The claim the lookup key is read from, and the ONLY one. See _caller_email.
SCOPE_KEY_CLAIM = "email"

# How long a resolved scope may be reused within one Lambda container. Bursty UI
# polling otherwise costs one Query per request. Only *successful* lookups are
# cached; a failure is never remembered as an answer.
SCOPE_CACHE_TTL_SECONDS = 60.0


class ScopeLookupError(Exception):
    """The caller's config-version scope could not be evaluated — deny.

    "Cannot evaluate" is not "unrestricted", and the difference is the whole
    point of this exception existing. A consumer that catches this MUST refuse
    the request: raise ``PermissionError`` (the REST dispatcher turns it into a
    403), or return the in-band ``{"success": false, "error": {"type":
    "Unauthorized"}}`` shape the configuration and sync resolvers use. Returning
    ``None`` instead silently disables RBAC for every scoped caller whenever the
    stack wiring, the IAM grant or the index name drifts — which is AUTH.T07.
    """


def caller_email_from_claims(claims: Optional[Mapping[str, Any]]) -> str:
    """The caller's email address, from the ``email`` claim and nothing else.

    Email is the only identifier that joins a Cognito principal to a UsersTable
    row. The row's key is ``PK``/``SK`` = ``USER#<userId>`` where ``userId`` is a
    ``uuid4`` minted by ``user_management`` and unrelated to the Cognito ``sub``;
    the Cognito account's username *is* the email; and no ``sub`` attribute is
    stored on the table at all. So there is no key a ``GetItem`` could be built
    from, and :data:`USERS_TABLE_SCOPE_INDEX` is the join.

    ⚠️ **There is deliberately no fallback to another field.** Every alternative
    identifier a claims set might carry — a ``sub``, a ``cognito:username``, a
    username an adapter substituted for a missing email — is not an email address
    for every caller, and querying an email-keyed index with one matches no row.
    An empty page is indistinguishable from "this user has no restriction", so a
    fallback converts an *unresolvable* caller into an *unrestricted* one:
    silently, with no AWS fault required, and precisely for the callers whose
    claims are least like the ones the code was tested against. Returning the
    empty string instead makes :func:`resolve_allowed_config_versions` raise, and
    the request is denied.

    This is the same rule the Chat-with-Document processor states at its own
    ``_caller_email``; the two are deliberately identical because they are the
    same decision.
    """
    if not isinstance(claims, Mapping):
        return ""
    return str(claims.get(SCOPE_KEY_CLAIM) or "").strip()


def normalize_scope(raw: Any) -> Optional[List[str]]:
    """
    Coerce a raw ``allowedConfigVersions`` attribute into a scope list.

    Returns None for "unrestricted" (absent, empty, or not a usable sequence) so
    callers can use a single ``if scope:`` test, and drops blank entries — a
    stray empty string must not become a rule that matches nothing.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        logger.warning(
            f"Ignoring unusable allowedConfigVersions of type {type(raw).__name__}"
        )
        return None
    entries = [str(entry).strip() for entry in raw if str(entry).strip()]
    return entries or None


def is_pattern(entry: str) -> bool:
    """True when a scope entry is a glob rather than a literal profile name."""
    return any(char in entry for char in _GLOB_CHARS)


def scope_allows(scope: Optional[Sequence[str]], profile_name: Optional[str]) -> bool:
    """
    Whether a scope permits a Configuration Profile (or a document stamped with
    one).

    Args:
        scope: The caller's ``allowedConfigVersions``, or None/empty for
            unrestricted.
        profile_name: The profile name to test. An empty or missing name is
            **denied** whenever a scope is set — see the module docstring.
    """
    entries = normalize_scope(scope)
    if not entries:
        return True
    if not profile_name:
        return False
    name = str(profile_name)
    for entry in entries:
        if entry == name:
            return True
        if is_pattern(entry) and fnmatchcase(name, entry):
            return True
    return False


def filter_profiles(scope: Optional[Sequence[str]], names: Iterable[str]) -> List[str]:
    """Reduce an iterable of profile names to those the scope permits."""
    entries = normalize_scope(scope)
    if not entries:
        return list(names)
    return [name for name in names if scope_allows(entries, name)]


def resolve_allowed_config_versions(
    caller_email: str,
    *,
    users_table_name: str,
    dynamodb: Any,
    cache: Optional[MutableMapping[str, Any]] = None,
    cache_ttl: float = SCOPE_CACHE_TTL_SECONDS,
) -> Optional[List[str]]:
    """Look up one caller's ``allowedConfigVersions``, failing CLOSED.

    Args:
        caller_email: The caller's email, as produced by
            :func:`caller_email_from_claims`. An empty value denies; nothing is
            substituted for it and **no query is issued**.
        users_table_name: ``USERS_TABLE_NAME``. Empty means the scope cannot be
            evaluated, which denies — the parent template wires this
            unconditionally for every consumer, so an empty value is a wiring
            regression, not a deployment without RBAC.
        dynamodb: The caller's ``boto3.resource("dynamodb")``. Passed in rather
            than built here so this module needs no boto3 at import time, which
            is what lets the two document-list resolvers vendor it verbatim
            without an ``idp_common`` layer.
        cache: Optional per-container ``{email: {"scope": ..., "timestamp": ...}}``
            map. Only successful lookups are stored.
        cache_ttl: Seconds a cached scope stays usable.

    Returns:
        ``None`` when the caller is **unrestricted** — either no UsersTable row
        matches them, or the row sets no ``allowedConfigVersions``. This is the
        default for most users and is deliberate: scoping is opt-in per user, and
        denying on an empty page would lock every ordinary user out of the UI. It
        is the one "absence means allow" branch in this module, and it is an
        *answer* from the table, not a failure to get one.

        Otherwise the list of profile names (or glob patterns) the caller may see,
        to be passed to :func:`scope_allows`.

    Raises:
        ScopeLookupError: whenever the scope cannot be *evaluated* — no table
            wired, no caller email, or a failed DynamoDB query. Every consumer
            must turn this into a refusal; see the exception's docstring.
    """
    if not users_table_name:
        raise ScopeLookupError("USERS_TABLE_NAME is not configured")
    if not caller_email:
        raise ScopeLookupError(
            f"no {SCOPE_KEY_CLAIM!r} claim on the verified caller identity"
        )

    now = time.time()
    if cache is not None:
        cached = cache.get(caller_email)
        if cached and (now - cached["timestamp"]) < cache_ttl:
            return cached["scope"]

    # Imported here, not at module scope, so the matcher above stays importable
    # with no boto3 present and the vendored copies of this file add no
    # dependency to a bundle that does not already carry one.
    from boto3.dynamodb.conditions import Key

    try:
        table = dynamodb.Table(users_table_name)
        response = table.query(
            IndexName=USERS_TABLE_SCOPE_INDEX,
            KeyConditionExpression=Key(USERS_TABLE_SCOPE_KEY).eq(caller_email),
            Limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberately no caller email and no table name in the message: this
        # lands in a log group, and the message is re-raised to a consumer that
        # may surface it. The index name is a constant, so it is safe and is the
        # one detail that distinguishes a missing IAM grant from a bad index.
        logger.error(
            "Config-version scope lookup failed on %s, denying the request: %s",
            USERS_TABLE_SCOPE_INDEX,
            exc,
        )
        raise ScopeLookupError(
            f"UsersTable {USERS_TABLE_SCOPE_INDEX} query failed: {exc}"
        ) from exc

    items = response.get("Items") or []
    scope = normalize_scope(items[0].get("allowedConfigVersions")) if items else None
    if cache is not None:
        cache[caller_email] = {"scope": scope, "timestamp": now}
    return scope
