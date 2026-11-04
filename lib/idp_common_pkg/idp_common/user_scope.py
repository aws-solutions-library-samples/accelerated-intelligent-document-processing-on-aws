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
    table: Any, *, email: str, cognito_sub: str = ""
) -> Optional[Dict[str, Any]]:
    """A user's row: through the ``SUB#`` pointer first, then ``EmailIndex``.

    The same order :func:`idp_common.config_scope.resolve_allowed_config_versions`
    reads in, but returning the row whether or not it is scoped, because a writer
    has to find an unscoped row to scope it. Read failures propagate.
    """
    from boto3.dynamodb.conditions import Key

    if cognito_sub:
        pointer = table.get_item(Key=sub_pointer_key(cognito_sub)).get("Item")
        user_id = str((pointer or {}).get("userId") or "").strip()
        if user_id:
            row = table.get_item(Key=user_row_key(user_id)).get("Item")
            if row:
                return row
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
        if items:
            return items[0]
    return None


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
    row = find_user_row(users_table, email=email, cognito_sub=cognito_sub)
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
        users_table.update_item(
            Key=user_row_key(user_id),
            UpdateExpression="SET " + ", ".join(set_parts) + remove,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
        )
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
