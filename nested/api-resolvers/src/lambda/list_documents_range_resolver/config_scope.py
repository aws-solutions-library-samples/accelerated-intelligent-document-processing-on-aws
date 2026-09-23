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

The caller is joined to their row on the **immutable Cognito ``sub``** where the
row records one, and on the ``email`` claim otherwise. See
:data:`USERS_TABLE_SUB_POINTER_PREFIX` for the two key spaces and why the sub one
needs no change to the table.
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

# The two key spaces that find a caller's scope row, and why they are not
# interchangeable.
#
# The row itself is ``PK``/``SK`` = ``USER#<userId>``, where ``userId`` is a
# ``uuid4`` minted by ``user_management`` and unrelated to any Cognito identifier.
# Nothing in a token names it, so a verified caller reaches it one of two ways:
#
# * **the immutable Cognito ``sub``**, via a *pointer item* in the same table whose
#   ``PK``/``SK`` is ``SUB#<sub>`` and which carries the ``userId``. This is the
#   durable join: a ``sub`` is assigned once per account and nothing a user or an
#   external IdP can change alters it.
# * **the ``email`` claim**, via :data:`USERS_TABLE_SCOPE_INDEX`. This is the
#   compatibility join, and it is the only one available for a row written before
#   the pointer existed or for a caller whose Cognito account this deployment's
#   user administration has never seen.
#
# Email alone is not a safe join, which is why the sub one exists. An address can
# stop matching the row it should find: a pool created with
# ``ExternalIdPEmailMutable=true`` lets a user change their own, Cognito re-applies
# an external IdP's ``AttributeMapping`` on every federated sign-in, and matching
# is case-sensitive while mail systems are not. Every one of those reads as "no
# row", and "no row" deliberately means *unrestricted* — so a divergence lifts the
# restriction silently. See ``docs/external-idp.md``.
#
# The pointer deliberately reuses the table's **existing** ``PK``/``SK`` key
# schema instead of adding a GSI. Nothing about the ``AWS::DynamoDB::Table``
# resource changes, so no deployment takes a table update for this: the read is a
# ``GetItem`` the IAM grants already allow (every scope consumer holds
# ``dynamodb:GetItem`` on the table as well as ``dynamodb:Query`` on its indexes),
# and there is no index-backfill window during which a fail-closed lookup would
# deny every scoped caller.
#
# ⚠️ A pointer item must NOT carry an ``email`` attribute. A DynamoDB GSI indexes
# only items that have its hash key, so an item without ``email`` is absent from
# :data:`USERS_TABLE_SCOPE_INDEX` entirely — which is what keeps pointers out of
# the email query's result page. Give a pointer an ``email`` and a ``Limit=1``
# email query could return the pointer instead of the row; the pointer carries no
# ``allowedConfigVersions``, so the scope would silently lift. This is asserted
# against a real index in the ``user_management`` writer suite.
USERS_TABLE_SUB_POINTER_PREFIX = "SUB#"
USERS_TABLE_USER_KEY_PREFIX = "USER#"
USERS_TABLE_SUB_ATTRIBUTE = "cognitoSub"

# The claim each lookup key is read from, and the only one for each. See
# caller_email_from_claims and caller_sub_from_claims.
SCOPE_KEY_CLAIM = "email"
SCOPE_SUB_CLAIM = "sub"

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

    This value is the hash key of a :data:`USERS_TABLE_SCOPE_INDEX` query, so it
    has to be an email address or it matches nothing.

    ⚠️ **There is deliberately no fallback to another field.** Every alternative
    identifier a claims set might carry — a ``cognito:username``, a username an
    adapter substituted for a missing email — is not an email address for every
    caller, and querying an email-keyed index with one matches no row. An empty
    page is indistinguishable from "this user has no restriction", so a fallback
    converts an *unresolvable* caller into an *unrestricted* one: silently, with
    no AWS fault required, and precisely for the callers whose claims are least
    like the ones the code was tested against. Returning the empty string instead
    lets :func:`resolve_allowed_config_versions` fall through to the ``sub`` join
    and, failing that, raise — and the request is denied.

    The Cognito ``sub`` is **not** a fallback for this value. It is a key into a
    different key space (see :data:`USERS_TABLE_SUB_POINTER_PREFIX`) and is read
    separately by :func:`caller_sub_from_claims`. Putting one where the other
    belongs is the substitution this docstring is about, not a second route.

    This is the same rule the Chat-with-Document processor states at its own
    ``_caller_email``; the two are deliberately identical because they are the
    same decision.
    """
    if not isinstance(claims, Mapping):
        return ""
    return str(claims.get(SCOPE_KEY_CLAIM) or "").strip()


def caller_sub_from_claims(claims: Optional[Mapping[str, Any]]) -> str:
    """The caller's immutable Cognito ``sub``, from the ``sub`` claim and no other.

    Used only to build a :data:`USERS_TABLE_SUB_POINTER_PREFIX` key, never as the
    hash key of :data:`USERS_TABLE_SCOPE_INDEX` — the two key spaces are disjoint
    and each accepts exactly one identifier.

    ⚠️ **A request body is not a source for this value.** The ``sub`` must come
    from claims a transport verified. A body-supplied ``callerSub`` is chosen by
    the caller being restricted, so keying a scope on it is not a control; that
    is how the original instance of this defect class was written.
    """
    if not isinstance(claims, Mapping):
        return ""
    return str(claims.get(SCOPE_SUB_CLAIM) or "").strip()


def sub_pointer_key(caller_sub: str) -> dict:
    """The UsersTable key of the pointer item for one Cognito ``sub``.

    One function so the writer in ``user_management`` and every reader build the
    same key. A pointer that disagrees with its readers by one character is a
    silent "no row", which means unrestricted.
    """
    key = f"{USERS_TABLE_SUB_POINTER_PREFIX}{caller_sub}"
    return {"PK": key, "SK": key}


def user_row_key(user_id: str) -> dict:
    """The UsersTable key of the row holding one user's scope."""
    key = f"{USERS_TABLE_USER_KEY_PREFIX}{user_id}"
    return {"PK": key, "SK": key}


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


def _row_by_sub(table: Any, caller_sub: str) -> Optional[Mapping[str, Any]]:
    """The caller's row reached through the ``SUB#<sub>`` pointer, or None.

    Two ``GetItem`` calls rather than one: the pointer holds only the ``userId``,
    never a copy of the scope. A copy would be a second source of truth for an
    authorization decision, and ``update_user`` writes the scope to one place.

    A pointer that names a row which no longer exists returns None and logs — a
    user deleted without their pointer being cleaned up. The caller then tries the
    email join, exactly as it would for a caller with no pointer at all.

    ⚠️ **This leg cannot return an unrestricted row, and that is deliberate.** The
    writer's invariant is that a pointer exists only for a row carrying
    ``allowedConfigVersions`` (see ``_record_cognito_sub`` in ``user_management``),
    because this leg is read *first* and therefore decides the answer — a pointer at
    an unrestricted row would pin "unrestricted" ahead of whatever the email join
    would have found. A pointer that resolves an unscoped row means the invariant has
    been broken somewhere, so it is treated as stale rather than believed: the caller
    falls through to the email join, which can only tighten. The alternative —
    trusting it — is the one shape that would let this leg *widen* a scope.
    """
    pointer = table.get_item(Key=sub_pointer_key(caller_sub)).get("Item")
    if not pointer:
        return None
    user_id = str(pointer.get("userId") or "").strip()
    if not user_id:
        logger.warning(
            "A UsersTable %s pointer carries no userId; falling back to the %r join",
            USERS_TABLE_SUB_POINTER_PREFIX,
            SCOPE_KEY_CLAIM,
        )
        return None
    row = table.get_item(Key=user_row_key(user_id)).get("Item")
    if not row:
        logger.warning(
            "A UsersTable %s pointer names a row that does not exist; falling back "
            "to the %r join",
            USERS_TABLE_SUB_POINTER_PREFIX,
            SCOPE_KEY_CLAIM,
        )
        return None
    if not normalize_scope(row.get("allowedConfigVersions")):
        logger.warning(
            "A UsersTable %s pointer names a row carrying no allowedConfigVersions, "
            "which the writer's invariant forbids. Treating it as stale and falling "
            "back to the %r join; the pointer needs removing.",
            USERS_TABLE_SUB_POINTER_PREFIX,
            SCOPE_KEY_CLAIM,
        )
        return None
    return row


def _row_by_email(
    table: Any, caller_email: str, key_factory: Any
) -> Optional[Mapping[str, Any]]:
    """The caller's row reached through :data:`USERS_TABLE_SCOPE_INDEX`, or None."""
    response = table.query(
        IndexName=USERS_TABLE_SCOPE_INDEX,
        KeyConditionExpression=key_factory(USERS_TABLE_SCOPE_KEY).eq(caller_email),
        Limit=1,
    )
    items = response.get("Items") or []
    return items[0] if items else None


def resolve_allowed_config_versions(
    caller_email: str,
    *,
    users_table_name: str,
    dynamodb: Any,
    caller_sub: str = "",
    cache: Optional[MutableMapping[str, Any]] = None,
    cache_ttl: float = SCOPE_CACHE_TTL_SECONDS,
) -> Optional[List[str]]:
    """Look up one caller's ``allowedConfigVersions``, failing CLOSED.

    The caller's row is looked for on the **immutable Cognito ``sub``** first and
    on the ``email`` claim second. The two are not a fallback chain over one key:
    each identifier is put only to the key space that indexes it (see
    :data:`USERS_TABLE_SUB_POINTER_PREFIX`), and neither is ever substituted for
    the other. The order is what makes the restriction survive an address that has
    diverged from the row it should match, for every row that records a ``sub``.

    Args:
        caller_email: The caller's email, as produced by
            :func:`caller_email_from_claims`. Nothing is substituted for an empty
            value and no email query is issued for one.
        users_table_name: ``USERS_TABLE_NAME``. Empty means the scope cannot be
            evaluated, which denies — the parent template wires this
            unconditionally for every consumer, so an empty value is a wiring
            regression, not a deployment without RBAC.
        dynamodb: The caller's ``boto3.resource("dynamodb")``. Passed in rather
            than built here so this module needs no boto3 at import time, which
            is what lets the two document-list resolvers vendor it verbatim
            without an ``idp_common`` layer.
        caller_sub: The caller's Cognito ``sub``, as produced by
            :func:`caller_sub_from_claims`. Optional, and empty on a transport
            that verifies no ``sub``; the email join then carries the lookup on
            its own, which is what every deployment did before pointers existed.
        cache: Optional per-container ``{key: {"scope": ..., "timestamp": ...}}``
            map, keyed on both identifiers. Only successful lookups are stored.
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
            wired, neither identifier present on the verified identity, a failed
            DynamoDB read, or a caller who presents only a ``sub`` that no row
            records (there is then no second key to try, so the absence of a row
            is not an answer about this caller). Every consumer must turn this
            into a refusal; see the exception's docstring.
    """
    if not users_table_name:
        raise ScopeLookupError("USERS_TABLE_NAME is not configured")
    caller_sub = str(caller_sub or "").strip()
    if not caller_email and not caller_sub:
        raise ScopeLookupError(
            f"neither a {SCOPE_KEY_CLAIM!r} nor a {SCOPE_SUB_CLAIM!r} claim on the "
            "verified caller identity"
        )

    now = time.time()
    # Both identifiers are in the cache key. Keying on one alone would let a
    # caller resolved by sub serve a caller resolved by email, or the reverse.
    cache_key = f"{caller_sub}\x00{caller_email}"
    if cache is not None:
        cached = cache.get(cache_key)
        if cached and (now - cached["timestamp"]) < cache_ttl:
            return cached["scope"]

    # Imported here, not at module scope, so the matcher above stays importable
    # with no boto3 present and the vendored copies of this file add no
    # dependency to a bundle that does not already carry one.
    from boto3.dynamodb.conditions import Key

    try:
        table = dynamodb.Table(users_table_name)
        row = _row_by_sub(table, caller_sub) if caller_sub else None
        if row is None and caller_email:
            row = _row_by_email(table, caller_email, Key)
    except Exception as exc:  # noqa: BLE001
        # Deliberately no caller email, no sub and no table name in the message:
        # this lands in a log group, and the message is re-raised to a consumer
        # that may surface it. The index name is a constant, so it is safe and is
        # the one detail that distinguishes a missing IAM grant from a bad index.
        logger.error(
            "Config-version scope lookup failed (%s / %s), denying the request: %s",
            USERS_TABLE_SUB_POINTER_PREFIX,
            USERS_TABLE_SCOPE_INDEX,
            exc,
        )
        raise ScopeLookupError(
            f"UsersTable scope read failed ({USERS_TABLE_SCOPE_INDEX}): {exc}"
        ) from exc

    if row is None and not caller_email:
        # A sub-only caller whose sub no row records. Unlike an empty email page,
        # this is not an answer about the caller: their row may well exist and
        # simply predate the pointer writer, and there is no second key to try.
        # Reading it as "unrestricted" would be the fail-open this module exists
        # to prevent, so it denies.
        raise ScopeLookupError(
            f"no {SCOPE_KEY_CLAIM!r} claim, and no UsersTable row records this "
            f"caller's Cognito {SCOPE_SUB_CLAIM!r}"
        )

    if row is not None and caller_sub:
        recorded_sub = str(row.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
        if recorded_sub and recorded_sub != caller_sub:
            # Checked on **whichever** leg matched, not only the email one. On the
            # email leg this is the interesting case below. On the pointer leg it
            # means the pointer and the row it names disagree about whose row it is,
            # which the writer never produces — and since that leg is read first,
            # leaving it unchecked would be the quieter of the two.
            #
            # The address resolved to a row belonging to a *different* Cognito
            # account — an address reassigned, or a Cognito account recreated
            # under the same one. The row is still used: it is the row for this
            # address, and a scope is a restriction, so applying it is the
            # conservative direction. But an operator needs to see it, because it
            # means one of the two accounts has a stale row.
            logger.warning(
                "A UsersTable row found by %r records a different Cognito %r than "
                "the caller presented. Its scope is being applied; the row's %s "
                "needs reconciling.",
                SCOPE_KEY_CLAIM,
                SCOPE_SUB_CLAIM,
                USERS_TABLE_SUB_ATTRIBUTE,
            )

    scope = normalize_scope(row.get("allowedConfigVersions")) if row else None
    if cache is not None:
        cache[cache_key] = {"scope": scope, "timestamp": now}
    return scope
