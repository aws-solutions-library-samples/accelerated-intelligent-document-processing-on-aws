# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Writing a user's configuration scope into the UsersTable.

Readers of the scope live in :mod:`idp_common.config_scope`. This module is the
writer side, and it holds the two invariants every reader depends on:

* a user's scope lives on exactly one row, ``USER#<userId>``;
* a ``SUB#<sub>`` pointer exists **only** for a row that carries a scope.

``src/lambda/user_management`` (an Admin setting a scope by hand), the sign-in
trigger (a scope granted by the identity provider's claim) and the SDK (a scope
pushed by an entitlement system) all write through here, so the invariants are
stated once.

Who may set a scope is the stack's ``TenantGrantSource``
(:func:`tenant_grant_source`): ``Manual`` (an Admin, in User Management),
``IdpClaim`` (the identity provider's claim at each fresh sign-in) or ``Api`` (an
entitlement system calling ``setUserTenant`` or ``idp-cli tenancy
set-user-tenant``). Under the last two a user's scope is exactly one tenant's
profile pattern or :data:`NO_TENANT_GRANTED`, never empty, because an empty scope
means *unrestricted* outside strict isolation.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from idp_common.config_scope import (  # noqa: F401 - grant-source names re-exported
    GRANT_SOURCE_API,
    GRANT_SOURCE_IDP_CLAIM,
    GRANT_SOURCE_MANUAL,
    SCOPE_KEY_CLAIM,
    SCOPE_SUB_CLAIM,
    TENANT_GRANT_SOURCE_ENV,
    USERS_TABLE_SCOPE_INDEX,
    USERS_TABLE_SCOPE_KEY,
    USERS_TABLE_SUB_ATTRIBUTE,
    normalize_scope,
    sub_pointer_key,
    user_row_key,
)

logger = logging.getLogger(__name__)

#: The scope written for a user the identity provider or an entitlement push grants
#: no tenant (or more than one). Profile names are validated to letters, digits,
#: ``-``, ``_`` and ``.``, and this is not a glob, so no profile and no document
#: matches it: every scope consumer reads it as "no access" with no code of its own.
NO_TENANT_GRANTED = "!no-tenant-granted"

GRANT_SOURCES = (GRANT_SOURCE_MANUAL, GRANT_SOURCE_IDP_CLAIM, GRANT_SOURCE_API)

#: What ``scopeSource`` records on a row, per grant source.
SCOPE_SOURCE_IDP = "idp"
SCOPE_SOURCE_API = "api"


def tenant_grant_source(environ: Optional[Mapping[str, str]] = None) -> str:
    """The stack's ``TenantGrantSource`` parameter, read per call.

    An unset or unrecognised value is ``Manual``, which is today's behaviour; the
    wiring gate (``scripts/tests/test_tenant_isolation_wiring.py``) is what fails on
    a function deployed without the variable.
    """
    env = os.environ if environ is None else environ
    value = env.get(TENANT_GRANT_SOURCE_ENV, GRANT_SOURCE_MANUAL)
    return value if value in GRANT_SOURCES else GRANT_SOURCE_MANUAL


def is_scoped(row: Optional[Mapping[str, Any]]) -> bool:
    """Whether a user row carries a config-version restriction."""
    return bool(normalize_scope((row or {}).get("allowedConfigVersions")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_cognito_sub(
    table: Any,
    user_id: str,
    caller_sub: str,
    *,
    scoped: bool,
    delete_stale_pointer: bool = True,
) -> bool:
    """Record a user's Cognito ``sub`` on their row, and maintain its pointer.

    ⚠️ **A pointer exists only for a row that carries a restriction**, which is what
    ``scoped`` decides. That is the invariant every reader depends on, and it is not
    an optimisation:

    * A row with no ``allowedConfigVersions`` resolves to "unrestricted" through
      either key, so a pointer for it changes no authorization answer.
    * A pointer is read **before** the email join, so it decides the answer. Pointing
      one at an unrestricted row therefore *pins* "unrestricted" ahead of any row the
      email join would have found — and the Cognito sync in ``user_management`` can
      create such a row: it writes a fresh unscoped row whenever a Cognito address
      matches none it knows, which is what happens to a user whose address diverged
      from their (scoped) row. Before this rule, that duplicate got the pointer, and
      correcting the address no longer restored the scope. With it, resolving through
      a pointer can only ever *tighten*.

    ⚠️ The pointer item carries **no** ``email`` attribute, and must not gain one.
    A DynamoDB GSI indexes only items that have its hash key, so an item without
    ``email`` is absent from ``EmailIndex`` altogether — which is what keeps
    pointers out of an email query's result page. Give a pointer an ``email`` and a
    ``Limit=1`` email query could return the pointer instead of the row; the
    pointer holds no ``allowedConfigVersions``, so the scope would silently lift.

    The ``sub`` itself is recorded on the row unconditionally. It is not a lookup key
    on its own — it is how the next sync recognises this row as this Cognito
    account's whatever the address now says — so recording it is always safe.

    Failures are logged, not raised. The pointer is an *additional* route to a row
    that ``EmailIndex`` still finds, so a user whose pointer could not be written
    keeps working exactly as they did before pointers existed. Raising would fail
    the create or the list that triggered it, which is the worse trade.
    """
    if not caller_sub or not user_id:
        return False
    now = _now()
    try:
        # `attribute_exists(PK)` because `update_item` UPSERTS. The user_id can come
        # from a scan that is seconds old, so a concurrent deleteUser is enough to
        # make this mint a partial row — no `email`, no `userId` — which then raises
        # KeyError in both the sync's scan loop and `list_users`, and those are the
        # only route into User Management *and* the only thing that runs this
        # back-fill. One such item would brick the page for every Admin.
        table.update_item(
            Key=user_row_key(user_id),
            UpdateExpression="SET #sub = :sub, updatedAt = :now",
            ExpressionAttributeNames={"#sub": USERS_TABLE_SUB_ATTRIBUTE},
            ExpressionAttributeValues={":sub": caller_sub, ":now": now},
            ConditionExpression="attribute_exists(PK)",
        )
        if scoped:
            pointer = sub_pointer_key(caller_sub)
            pointer["userId"] = user_id
            pointer[USERS_TABLE_SUB_ATTRIBUTE] = caller_sub
            pointer["updatedAt"] = now
            table.put_item(Item=pointer)
        elif delete_stale_pointer:
            # Unrestricted now, so no pointer may survive: a scope removed through
            # updateUser must not leave one behind pointing at the row it used to
            # restrict. Callers that know none can exist yet pass
            # ``delete_stale_pointer=False`` rather than paying for the call.
            table.delete_item(Key=sub_pointer_key(caller_sub))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not record the Cognito %s for user %s; their scope stays "
            "resolvable by %s alone: %s",
            SCOPE_SUB_CLAIM,
            user_id,
            SCOPE_KEY_CLAIM,
            e,
        )
        return False
    return True


def find_user_row(
    table: Any, *, email: str, cognito_sub: str = "", unique: bool = False
) -> Optional[Dict[str, Any]]:
    """A user's row: through the ``SUB#`` pointer first, then ``EmailIndex``.

    The same order :func:`idp_common.config_scope.resolve_allowed_config_versions`
    reads in, but returning the row whether or not it is scoped, because a writer
    has to find an unscoped row to scope it. Read failures propagate.

    ⚠️ **With a ``cognito_sub``, a row recording a different sub is never returned**,
    by either leg. Cognito does not make ``email`` unique, so two live accounts can
    share an address, and each has to keep its own row: a row that records another
    account's sub holds that account's tenant. An account with no row of its own is
    given a new one by the grant. A row recording no sub yet (one an Admin created
    by address) is still returned: it is claimed by the first account to sign in
    with it, as before. Without a ``cognito_sub`` (an API push knows only the
    email) every row with the address is a candidate and ``unique`` decides.
    """
    from boto3.dynamodb.conditions import Key

    def owned_by_another(row: Mapping[str, Any]) -> bool:
        recorded = str(row.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
        return bool(cognito_sub and recorded and recorded != cognito_sub)

    if cognito_sub:
        pointer = table.get_item(Key=sub_pointer_key(cognito_sub)).get("Item")
        user_id = str((pointer or {}).get("userId") or "").strip()
        if user_id:
            row = table.get_item(Key=user_row_key(user_id)).get("Item")
            if row and not owned_by_another(row):
                return row
            if row:
                # This account's pointer names a row that records another
                # account's sub, so the row is not this account's. The grant
                # writes the pointer again, naming the row it does use.
                logger.warning(
                    "A SUB# pointer names a user row recording a different Cognito "
                    "%s; not treating that row as this account's",
                    SCOPE_SUB_CLAIM,
                )
    if email:
        items = (
            table.query(
                IndexName=USERS_TABLE_SCOPE_INDEX,
                KeyConditionExpression=Key(USERS_TABLE_SCOPE_KEY).eq(email),
            ).get("Items")
            or []
        )
        # Pointer items carry no email (see record_cognito_sub), so every item here
        # is a user row.
        if cognito_sub:
            mine = [i for i in items if not owned_by_another(i)]
            if len(mine) < len(items):
                logger.info(
                    "Skipping %d user row(s) with this address that record another "
                    "Cognito %s",
                    len(items) - len(mine),
                    SCOPE_SUB_CLAIM,
                )
            # Prefer the row that already records this sub over an unclaimed one.
            items = sorted(
                mine,
                key=lambda i: (
                    str(i.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip() != cognito_sub
                ),
            )
        if unique and len(items) > 1:
            raise ValueError(
                f"More than one user record has the email {email}, one for each "
                "Cognito account with that address, so a grant addressed by email "
                "alone cannot tell which account it is for. Try again once only "
                "one record for the address remains."
            )
        if items:
            return items[0]
    return None


def clear_granted_scope(
    users_table: Any, *, email: str, cognito_sub: str = "", source: str
) -> Optional[str]:
    """Remove a configuration scope that ``source`` itself wrote, for a user who is
    now an Admin.

    Admins are never granted a tenant, but a user granted one before being promoted
    keeps that row, and an Admin's row scope is still honoured by some paths (an
    upload by a scoped Admin stays inside the scope). Under an external grant source
    nothing else may edit the scope, so the source that wrote it has to remove it.
    Only a scope this source wrote is touched: one an Admin set by hand before the
    grant source changed is left alone.

    Returns the user id when a scope was cleared, else ``None``. Raises on a read or
    write failure, like :func:`apply_tenant_grant`.
    """
    row = find_user_row(
        users_table, email=email, cognito_sub=cognito_sub, unique=not cognito_sub
    )
    if row is None or row.get("scopeSource") != source:
        return None
    user_id = str(row["userId"])
    users_table.update_item(
        Key=user_row_key(user_id),
        UpdateExpression=(
            "SET updatedAt = :now REMOVE allowedConfigVersions, scopeTenant, "
            "scopeSource"
        ),
        ExpressionAttributeValues={":now": _now()},
        ConditionExpression="attribute_exists(PK)",
    )
    # Unscoped now, so its pointer must go: a pointer only ever exists for a row
    # carrying a restriction (see record_cognito_sub).
    sub = cognito_sub or str(row.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
    if sub and not record_cognito_sub(users_table, user_id, sub, scoped=False):
        raise RuntimeError(
            f"The tenant scope of Admin user {user_id} was removed, but its Cognito "
            "sub pointer could not be; the removal is not reported as applied."
        )
    logger.info(
        "Removed the %s-granted scope of user %s, now an Admin", source, user_id
    )
    return user_id


def scope_for_resolution(resolution: Mapping[str, Any]) -> list:
    """The scope a grant resolution writes: one tenant's pattern, or the deny marker."""
    tenant = resolution.get("tenant")
    if tenant and tenant.get("profilePattern"):
        return [str(tenant["profilePattern"])]
    return [NO_TENANT_GRANTED]


def apply_tenant_grant(
    users_table: Any,
    *,
    email: str,
    cognito_sub: str = "",
    resolution: Mapping[str, Any],
    source: str,
    persona_for_new_row: str = "Viewer",
    create_missing: bool = True,
) -> Dict[str, Any]:
    """Write the scope a grant resolution gives one user. Returns what happened.

    ``resolution`` is :func:`idp_common.config.tenants.resolve_grants`' answer.
    ``source`` is :data:`SCOPE_SOURCE_IDP` or :data:`SCOPE_SOURCE_API`.

    * An **Admin** row is never changed: Admins are cross-tenant by design, and a
      scope written onto one would only narrow the web uploads it applies to.
    * Every other user ends with exactly one tenant's pattern or
      :data:`NO_TENANT_GRANTED` -- never an empty scope, which would mean
      *unrestricted* outside strict isolation.
    * A user with no row gets one when ``create_missing`` is set, marked
      ``source: "cognito"`` so the UsersTable stream (``src/lambda/user_sync``) does
      not create a second Cognito account and email it an invitation.

    Raises on a read or write failure; the caller decides whether that fails a
    sign-in (it does) or an API call (it does).

    Returns ``{"action": "admin-unchanged" | "updated" | "created" | "missing",
    "userId", "allowedConfigVersions", "tenant"}``.
    """
    # Without a sub (an API push knows only the email) the row is found by address
    # alone. If two rows share it, the other may hold the SUB# pointer that
    # resolution reads first, so a grant written to this one could report success
    # and change nothing. Refuse instead of guessing.
    row = find_user_row(
        users_table, email=email, cognito_sub=cognito_sub, unique=not cognito_sub
    )
    scope = scope_for_resolution(resolution)
    tenant = resolution.get("tenant") or {}
    tenant_id = tenant.get("tenantId") or None
    now = _now()

    if row is not None and row.get("persona") == "Admin":
        return {
            "action": "admin-unchanged",
            "userId": row.get("userId"),
            "allowedConfigVersions": normalize_scope(row.get("allowedConfigVersions")),
            "tenant": None,
        }

    if row is None and persona_for_new_row == "Admin":
        # The same rule for a user with no row yet: no row is invented for an
        # Admin, because the one it would hold is a scope Admins never carry.
        return {
            "action": "admin-unchanged",
            "userId": None,
            "allowedConfigVersions": None,
            "tenant": None,
        }

    if row is None:
        if not create_missing:
            return {
                "action": "missing",
                "userId": None,
                "allowedConfigVersions": None,
                "tenant": None,
            }
        if not str(email or "").strip():
            # The row is found by its email through EmailIndex, and DynamoDB refuses
            # an empty string as an index key, so a row cannot be created without
            # one. A provider that sends no email claim (Entra, unless the email
            # optional claim is configured) must be fixed at the provider.
            raise ValueError(
                "The identity provider sent no email claim, so this user's tenant "
                "cannot be recorded. Configure the provider to send email (in Entra "
                "ID, the 'email' optional claim)."
            )
        user_id = str(uuid.uuid4())
        item = {
            **user_row_key(user_id),
            "userId": user_id,
            "email": email,
            "persona": persona_for_new_row,
            "status": "active",
            # Read by src/lambda/user_sync: the account already exists in Cognito.
            "source": "cognito",
            "allowedConfigVersions": scope,
            "scopeSource": source,
            "createdAt": now,
            "updatedAt": now,
        }
        if tenant_id:
            item["scopeTenant"] = tenant_id
        if cognito_sub:
            item[USERS_TABLE_SUB_ATTRIBUTE] = cognito_sub
        users_table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        action = "created"
    else:
        user_id = str(row["userId"])
        set_parts = [
            "allowedConfigVersions = :acv",
            "scopeSource = :src",
            "updatedAt = :now",
        ]
        values: Dict[str, Any] = {":acv": scope, ":src": source, ":now": now}
        remove = ""
        if tenant_id:
            set_parts.append("scopeTenant = :tid")
            values[":tid"] = tenant_id
        else:
            remove = " REMOVE scopeTenant"
        condition = "attribute_exists(PK)"
        names: Dict[str, str] = {}
        if cognito_sub:
            # The grant and the binding to this account are one write: the row is
            # bound to ``cognito_sub`` in the same UpdateItem, on condition that it
            # records no sub or this one. A row therefore never carries this grant
            # without recording the account it belongs to, and a row another
            # account bound after it was read is left as it is.
            set_parts.append("#sub = :sub")
            values[":sub"] = cognito_sub
            names["#sub"] = USERS_TABLE_SUB_ATTRIBUTE
            condition += " AND (attribute_not_exists(#sub) OR #sub = :sub)"
        update: Dict[str, Any] = {
            "Key": user_row_key(user_id),
            "UpdateExpression": "SET " + ", ".join(set_parts) + remove,
            "ExpressionAttributeValues": values,
            "ConditionExpression": condition,
        }
        if names:
            update["ExpressionAttributeNames"] = names
        try:
            users_table.update_item(**update)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if cognito_sub and code == "ConditionalCheckFailedException":
                raise RuntimeError(
                    f"The record of user {user_id} was removed, or bound to another "
                    "Cognito account, after it was read, so the tenant grant was not "
                    "written. Signing in again resolves the record afresh."
                ) from exc
            raise
        action = "updated"

    # Always scoped now, so the pointer must exist: it is what keeps the restriction
    # attached to the account if its address later diverges from this row's. An API
    # push knows only the email, so the sub the row already records is used.
    sub = cognito_sub or str((row or {}).get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
    # Unlike the back-fill in user_management, a failure here is raised. The pointer
    # is read before the email join, so one left on another row for the same address
    # (a duplicate row, or one a concurrent first sign-in created) would decide the
    # user's scope while this grant reported success.
    if sub and not record_cognito_sub(users_table, user_id, sub, scoped=True):
        raise RuntimeError(
            f"The tenant grant for user {user_id} was written, but its Cognito sub "
            "pointer could not be; the grant is not reported as applied."
        )

    logger.info(
        "Tenant grant (%s) for user %s: %s -> %s",
        source,
        user_id,
        action,
        tenant_id or "no tenant",
    )
    return {
        "action": action,
        "userId": user_id,
        "allowedConfigVersions": scope,
        "tenant": tenant_id,
    }
