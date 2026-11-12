# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP service credentials: client-credentials apps an Admin creates for the MCP server.

A service credential lets a non-human caller (a shared AWS Quick workflow, a nightly
integration, the MCP Connector) reach the MCP server with a role, a configuration
scope and, under tenant isolation, a tenant of its own, instead of the stack-wide
client every such caller used to share.

Each credential is its own Cognito app client, allowed the ``client_credentials``
flow only, with a one-hour access token. Its secret is written to Secrets Manager
(``<stack>/mcp/credentials/<clientId>``, encrypted with the stack's key) and shown
to the Admin once; nothing here returns it again. A row in the credentials table
records everything the gateway interceptor needs to resolve a token from it, and
the interceptor reads that row on every call, so a credential that is disabled,
revoked or expired is refused on the next request even while one of its tokens is
still valid. The client id is also added to (and on revocation removed from) the
gateway's JWT authorizer, through :mod:`idp_common.mcp_gateway`.

The rules a credential's role and scope must satisfy are the ones a user's must:

* the role is one of the stack's four roles; Admin only where an unscoped caller is
  unrestricted (``TenantIsolationMode=Off`` and ``TenantGrantSource=Manual``),
  because Admin is cross-tenant by design;
* where an unscoped caller is denied, every other credential needs a scope;
* a scope must sit inside one tenant, or inside no tenant
  (``idp_common.config.tenants.scope_rejection_reason``, which ``createUser`` uses);
* write tools only for roles that may write (Admin and Author).

Both the credential manager Lambda (the REST operations behind the MCP
Configuration page) and the SDK (``idp-cli mcp credentials``) call these functions,
so the two refuse exactly the same things.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from idp_common import mcp_gateway
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    normalize_scope,
    unscoped_is_denied,
)

logger = logging.getLogger(__name__)

ROLES = ("Viewer", "Reviewer", "Author", "Admin")
WRITE_ROLES = ("Admin", "Author")
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 3650
MAX_ROTATION_OVERLAP_DAYS = 7
EXPIRY_WARNING_DAYS = 14
ACCESS_TOKEN_MINUTES = 60
LAST_USED_RESOLUTION = timedelta(hours=1)
SECRET_RECOVERY_DAYS = 7
NAME_MAX = 64
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}$")

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"


#: Seconds ``revoke_all`` keeps in hand for the custom resource to answer.
CLEANUP_RESERVE_SECONDS = 30


class CredentialError(ValueError):
    """A request that must be refused, with a message the caller can act on."""


class CredentialNotFound(LookupError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def secret_name(stack_name: str, client_id: str) -> str:
    return f"{stack_name}/mcp/credentials/{client_id}"


@dataclass
class CredentialServices:
    """Everything the operations touch, injected so the SDK and the Lambda share
    one implementation and tests can supply moto clients."""

    cognito: Any
    secrets: Any
    table: Any
    control: Any
    gateway_id: str
    user_pool_id: str
    stack_name: str
    kms_key_id: str
    oauth_scopes: Sequence[str]
    token_url: str = ""
    gateway_url: str = ""
    tenants: Callable[[], List[Mapping[str, Any]]] = field(default=lambda: [])
    environ: Optional[Mapping[str, str]] = None
    notify: Optional[Callable[..., Any]] = None


# --- validation -----------------------------------------------------------------


def validate_access(
    role: str,
    scope: Any,
    write_tools: bool,
    *,
    tenants: Sequence[Mapping[str, Any]],
    environ: Optional[Mapping[str, str]] = None,
    require_scope: bool = True,
) -> Optional[List[str]]:
    """The normalised scope, or :class:`CredentialError` naming what is wrong.

    ``require_scope=False`` is for ``TenantGrantSource=Api``, where a credential is
    created with no scope and receives one only from ``setMcpCredentialTenant``;
    until then it resolves to the scope no profile matches."""
    from idp_common.config.tenants import scope_rejection_reason

    if role not in ROLES:
        raise CredentialError(f"Role must be one of {', '.join(ROLES)}.")
    entries = normalize_scope(scope)
    if role == "Admin":
        if unscoped_is_denied(environ):
            raise CredentialError(
                "An Admin credential is cross-tenant, so it is available only when "
                "TenantIsolationMode is Off and TenantGrantSource is Manual."
            )
        if entries:
            raise CredentialError(
                "An Admin credential has no configuration scope, as an Admin user "
                "has none."
            )
        return None
    if write_tools and role not in WRITE_ROLES:
        raise CredentialError("Write tools are available only to Admin and Author.")
    if not entries and require_scope and unscoped_is_denied(environ):
        raise CredentialError(
            "This deployment runs with tenant isolation, so a credential needs a "
            "configuration scope inside one tenant."
        )
    if entries and STRICT_UNSCOPED_DENY in entries:
        raise CredentialError("That scope entry is reserved.")
    reason = scope_rejection_reason(entries, tenants) if entries else None
    if reason:
        raise CredentialError(reason)
    return entries


def _expiry(now: datetime, expires_in_days: Optional[int]) -> Optional[str]:
    if expires_in_days is None:
        expires_in_days = DEFAULT_EXPIRY_DAYS
    if expires_in_days == 0:
        return None
    if not isinstance(expires_in_days, int) or not (
        1 <= expires_in_days <= MAX_EXPIRY_DAYS
    ):
        raise CredentialError(
            f"Expiry must be 0 (never) or between 1 and {MAX_EXPIRY_DAYS} days."
        )
    return _iso(now + timedelta(days=expires_in_days))


def _validate_name(name: str) -> str:
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        raise CredentialError(
            f"Name must be 1 to {NAME_MAX} characters: letters, digits, spaces and "
            f". _ ( ) -"
        )
    return name


# --- the row --------------------------------------------------------------------

#: The fields a listing returns. Never the secret, which is not on the row.
PUBLIC_FIELDS = (
    "clientId",
    "name",
    "role",
    "allowedConfigVersions",
    "tenant",
    "writeTools",
    "enabled",
    "status",
    "createdBy",
    "createdAt",
    "updatedBy",
    "updatedAt",
    "expiresAt",
    "lastUsedAt",
    "rotatedFrom",
    "replacedBy",
    "secretArn",
    "kind",
)


def public(row: Mapping[str, Any]) -> Dict[str, Any]:
    out = {k: row.get(k) for k in PUBLIC_FIELDS if k in row}
    out["writeTools"] = bool(row.get("writeTools"))
    out["enabled"] = bool(row.get("enabled"))
    out["status"] = status_of(row)
    if "allowedConfigVersions" in out:
        out["allowedConfigVersions"] = list(out["allowedConfigVersions"] or [])
    return out


def status_of(row: Mapping[str, Any], now: Optional[datetime] = None) -> str:
    if row.get("status") == STATUS_REVOKED:
        return STATUS_REVOKED
    expires = _parse(row.get("expiresAt"))
    if expires and expires <= (now or _now()):
        return STATUS_EXPIRED
    return STATUS_ACTIVE if row.get("enabled") else STATUS_DISABLED


def get_row(table: Any, client_id: str) -> Optional[Dict[str, Any]]:
    if not client_id:
        return None
    return table.get_item(Key={"clientId": client_id}, ConsistentRead=True).get("Item")


def _all_rows(table: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items") or [])
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key
    return rows


def list_credentials(services: CredentialServices) -> List[Dict[str, Any]]:
    return sorted(
        (public(r) for r in _all_rows(services.table)),
        key=lambda r: str(r.get("createdAt") or ""),
    )


def _audit(action: str, actor: str, row: Mapping[str, Any], **extra: Any) -> None:
    """One structured line per change: the acting principal, the credential's id
    and name and the new access. Never the secret."""
    entry = {
        "audit": "mcp-credential",
        "action": action,
        "actor": actor,
        "clientId": row.get("clientId"),
        "name": row.get("name"),
        "role": row.get("role"),
        "allowedConfigVersions": list(row.get("allowedConfigVersions") or []),
        "writeTools": bool(row.get("writeTools")),
        "enabled": bool(row.get("enabled")),
    }
    entry.update(extra)
    logger.info(json.dumps(entry, default=str))


# --- operations -------------------------------------------------------------------


def _new_client(services: CredentialServices, name: str) -> Dict[str, Any]:
    client = services.cognito.create_user_pool_client(
        UserPoolId=services.user_pool_id,
        ClientName=f"mcp-{re.sub(r'[^A-Za-z0-9-]+', '-', name)[:100]}",
        GenerateSecret=True,
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=list(services.oauth_scopes),
        SupportedIdentityProviders=["COGNITO"],
        AccessTokenValidity=ACCESS_TOKEN_MINUTES,
        TokenValidityUnits={"AccessToken": "minutes"},
        # No user sign-in flow and no attribute a user could write through this
        # client: left unset, Cognito allows SRP and custom-auth sign-in and lets
        # a user's token write every mutable attribute. email is listed because
        # the pool requires it, and a required attribute must be writable.
        ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
        ReadAttributes=["email"],
        WriteAttributes=["email"],
        EnableTokenRevocation=True,
        PreventUserExistenceErrors="ENABLED",
    )["UserPoolClient"]
    return client


def _store_secret(services: CredentialServices, name: str, client: Mapping[str, Any]):
    return services.secrets.create_secret(
        Name=secret_name(services.stack_name, client["ClientId"]),
        Description=f"MCP service credential {name}",
        KmsKeyId=services.kms_key_id,
        SecretString=json.dumps(
            {
                "clientId": client["ClientId"],
                "clientSecret": client["ClientSecret"],
                "tokenUrl": services.token_url,
            }
        ),
        Tags=[
            {"Key": "idp:stack-name", "Value": services.stack_name},
            {"Key": "idp:mcp-credential", "Value": name},
        ],
    )["ARN"]


def _discard(services: CredentialServices, client_id: str) -> None:
    """Best-effort removal of a half-created credential's client and secret."""
    for call in (
        lambda: services.cognito.delete_user_pool_client(
            UserPoolId=services.user_pool_id, ClientId=client_id
        ),
        lambda: services.secrets.delete_secret(
            SecretId=secret_name(services.stack_name, client_id),
            ForceDeleteWithoutRecovery=True,
        ),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - cleanup after a failure
            logger.warning("Cleanup after a failed credential create: %s", exc)


def _issue(
    services: CredentialServices,
    *,
    name: str,
    role: str,
    scope: Optional[List[str]],
    tenant: Optional[str],
    write_tools: bool,
    expires_at: Optional[str],
    actor: str,
    now: datetime,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    client = _new_client(services, name)
    client_id = client["ClientId"]
    try:
        secret_arn = _store_secret(services, name, client)
        row: Dict[str, Any] = {
            "clientId": client_id,
            "name": name,
            "role": role,
            "writeTools": bool(write_tools),
            "enabled": True,
            "status": STATUS_ACTIVE,
            "kind": "admin",
            "createdBy": actor,
            "createdAt": _iso(now),
            "secretArn": secret_arn,
        }
        if scope:
            row["allowedConfigVersions"] = list(scope)
        if tenant:
            row["tenant"] = tenant
        if expires_at:
            row["expiresAt"] = expires_at
        row.update(extra or {})
        services.table.put_item(
            Item=row, ConditionExpression="attribute_not_exists(clientId)"
        )
        mcp_gateway.update_allowed_clients(
            services.control, services.gateway_id, add=[client_id]
        )
    except Exception:
        try:
            services.table.delete_item(Key={"clientId": client_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cleanup of a failed credential row: %s", exc)
        _discard(services, client_id)
        raise
    return {
        "credential": public(row),
        "clientId": client_id,
        "clientSecret": client["ClientSecret"],
        "tokenUrl": services.token_url,
        "gatewayUrl": services.gateway_url,
        "secretArn": secret_arn,
        "oauthScopes": list(services.oauth_scopes),
    }


def create_credential(
    services: CredentialServices,
    *,
    name: str,
    role: str,
    allowed_config_versions: Any = None,
    write_tools: bool = False,
    expires_in_days: Optional[int] = None,
    actor: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create a credential; the only response that ever carries its secret."""
    now = now or _now()
    name = _validate_name(name)
    api = _source_is_api(services.environ)
    if api and normalize_scope(allowed_config_versions):
        raise CredentialError(
            "Tenants are granted by the entitlement system here; create the "
            "credential without a scope and use setMcpCredentialTenant."
        )
    scope = validate_access(
        role,
        allowed_config_versions,
        write_tools,
        tenants=services.tenants(),
        environ=services.environ,
        require_scope=not api,
    )
    result = _issue(
        services,
        name=name,
        role=role,
        scope=scope,
        tenant=None,
        write_tools=write_tools,
        expires_at=_expiry(now, expires_in_days),
        actor=actor,
        now=now,
    )
    _audit("create", actor, result["credential"])
    return result


#: Every change but revocation itself is conditional on the row not being revoked
#: at the moment of the write, so a change that read the row before a concurrent
#: revoke cannot write it back to active.
_NOT_REVOKED = "attribute_exists(clientId) AND #rv <> :rv"


def _guarded(
    services: CredentialServices, client_id: str, write: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        return services.table.update_item(**write)["Attributes"]
    except services.table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise CredentialError(
            f"Credential {client_id} was revoked, or removed, while it was being "
            "changed."
        ) from exc


def _require(services: CredentialServices, client_id: str) -> Dict[str, Any]:
    row = get_row(services.table, client_id)
    if not row:
        raise CredentialNotFound(f"No MCP credential {client_id!r}.")
    return row


def update_credential(
    services: CredentialServices,
    client_id: str,
    *,
    role: Optional[str] = None,
    allowed_config_versions: Any = None,
    write_tools: Optional[bool] = None,
    enabled: Optional[bool] = None,
    clear_scope: bool = False,
    actor: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Change a credential's role, scope, write tools or enabled flag. Never its
    secret. A revoked credential cannot be changed."""
    now = now or _now()
    row = _require(services, client_id)
    if row.get("status") == STATUS_REVOKED:
        raise CredentialError("A revoked credential cannot be changed.")
    new_role = role if role is not None else row.get("role")
    if clear_scope:
        scope_in: Any = None
    elif allowed_config_versions is not None:
        scope_in = allowed_config_versions
    else:
        scope_in = row.get("allowedConfigVersions")
    new_write = bool(row.get("writeTools")) if write_tools is None else write_tools
    api = _source_is_api(services.environ)
    if api and (clear_scope or allowed_config_versions is not None):
        raise CredentialError(
            "Tenants are granted by the entitlement system here; use "
            "setMcpCredentialTenant."
        )
    scope = validate_access(
        str(new_role),
        scope_in,
        new_write,
        tenants=services.tenants(),
        environ=services.environ,
        require_scope=not api,
    )
    names = {"#u": "updatedAt", "#b": "updatedBy", "#r": "role", "#w": "writeTools"}
    values: Dict[str, Any] = {
        ":u": _iso(now),
        ":b": actor,
        ":r": new_role,
        ":w": new_write,
    }
    sets = ["#u = :u", "#b = :b", "#r = :r", "#w = :w"]
    removes: List[str] = []
    names["#s"] = "allowedConfigVersions"
    if scope:
        sets.append("#s = :s")
        values[":s"] = list(scope)
    else:
        removes.append("#s")
    if enabled is not None:
        names["#e"] = "enabled"
        names["#st"] = "status"
        sets += ["#e = :e", "#st = :st"]
        values[":e"] = bool(enabled)
        values[":st"] = STATUS_ACTIVE if enabled else STATUS_DISABLED
    expression = "SET " + ", ".join(sets)
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    write = dict(
        Key={"clientId": client_id},
        UpdateExpression=expression,
        ExpressionAttributeNames={**names, "#rv": "status"},
        ExpressionAttributeValues={**values, ":rv": STATUS_REVOKED},
        ConditionExpression=_NOT_REVOKED,
        ReturnValues="ALL_NEW",
    )
    updated = _guarded(services, client_id, write)
    _audit("update", actor, updated)
    return public(updated)


def _source_is_api(environ: Optional[Mapping[str, str]]) -> bool:
    import os

    from idp_common.config_scope import GRANT_SOURCE_API, TENANT_GRANT_SOURCE_ENV

    env = os.environ if environ is None else environ
    return env.get(TENANT_GRANT_SOURCE_ENV) == GRANT_SOURCE_API


def set_credential_tenant(
    services: CredentialServices,
    client_id: str,
    tenant_id: Optional[str],
    *,
    actor: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Grant a credential one tenant, or none, as ``setUserTenant`` does for a user:
    the scope becomes exactly the tenant's profile pattern, or the marker no profile
    matches. Only where ``TenantGrantSource`` is ``Api``."""
    from idp_common.user_scope import scope_for_resolution

    now = now or _now()
    if not _source_is_api(services.environ):
        raise CredentialError(
            "setMcpCredentialTenant is available only when TenantGrantSource is Api."
        )
    row = _require(services, client_id)
    if row.get("role") == "Admin":
        raise CredentialError("An Admin credential is cross-tenant by design.")
    if row.get("status") == STATUS_REVOKED:
        raise CredentialError("A revoked credential cannot be changed.")
    tenant = None
    if tenant_id:
        tenant = next(
            (t for t in services.tenants() if t.get("tenantId") == tenant_id), None
        )
        if tenant is None:
            raise CredentialError(f"Tenant {tenant_id!r} is not declared.")
    scope = scope_for_resolution({"tenant": tenant})
    updated = _guarded(
        services,
        client_id,
        dict(
            Key={"clientId": client_id},
            UpdateExpression=(
                "SET allowedConfigVersions = :s, tenant = :t, updatedAt = :u, "
                "updatedBy = :b"
            ),
            ExpressionAttributeNames={"#rv": "status"},
            ExpressionAttributeValues={
                ":s": scope,
                ":t": tenant_id or "",
                ":u": _iso(now),
                ":b": actor,
                ":rv": STATUS_REVOKED,
            },
            ConditionExpression=_NOT_REVOKED,
            ReturnValues="ALL_NEW",
        ),
    )
    _audit("set-tenant", actor, updated, tenant=tenant_id)
    return public(updated)


def rotate_credential(
    services: CredentialServices,
    client_id: str,
    *,
    overlap_days: int = MAX_ROTATION_OVERLAP_DAYS,
    actor: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Issue a replacement with the same role, scope and write tools, and shorten
    the old credential's life to ``overlap_days`` (at most seven) so callers can
    move across. The replacement's secret is shown once, as on create."""
    now = now or _now()
    if not isinstance(overlap_days, int) or not (
        0 <= overlap_days <= MAX_ROTATION_OVERLAP_DAYS
    ):
        raise CredentialError(
            f"The overlap must be between 0 and {MAX_ROTATION_OVERLAP_DAYS} days."
        )
    old = _require(services, client_id)
    if status_of(old, now) != STATUS_ACTIVE:
        raise CredentialError("Only an active credential can be rotated.")
    scope = validate_access(
        str(old.get("role")),
        old.get("allowedConfigVersions"),
        bool(old.get("writeTools")),
        tenants=services.tenants(),
        environ=services.environ,
        require_scope=not _source_is_api(services.environ),
    )
    old_expiry = _parse(old.get("expiresAt"))
    old_created = _parse(old.get("createdAt"))
    lifetime = (
        (old_expiry - old_created).days
        if old_expiry and old_created
        else DEFAULT_EXPIRY_DAYS
    )
    result = _issue(
        services,
        name=str(old.get("name")),
        role=str(old.get("role")),
        scope=scope,
        tenant=old.get("tenant") or None,
        write_tools=bool(old.get("writeTools")),
        expires_at=_iso(now + timedelta(days=max(1, lifetime))) if old_expiry else None,
        actor=actor,
        now=now,
        extra={"rotatedFrom": client_id},
    )
    cutover = now + timedelta(days=overlap_days)
    if old_expiry and old_expiry < cutover:
        cutover = old_expiry
    try:
        _guarded(
            services,
            client_id,
            dict(
                Key={"clientId": client_id},
                UpdateExpression="SET expiresAt = :x, replacedBy = :n, "
                "updatedAt = :u, updatedBy = :b",
                ExpressionAttributeNames={"#rv": "status"},
                ExpressionAttributeValues={
                    ":x": _iso(cutover),
                    ":n": result["clientId"],
                    ":u": _iso(now),
                    ":b": actor,
                    ":rv": STATUS_REVOKED,
                },
                ConditionExpression=_NOT_REVOKED,
                ReturnValues="ALL_NEW",
            ),
        )
    except Exception:
        # The replacement's secret has not been shown to anyone, so a replacement
        # whose old credential cannot be shortened is withdrawn rather than left
        # working beside it.
        revoke_credential(
            services, result["clientId"], actor=actor, now=now, reason="rotate-failed"
        )
        raise
    _audit("rotate", actor, result["credential"], replaces=client_id)
    if overlap_days == 0:
        revoke_credential(services, client_id, actor=actor, now=now)
    return result


def revoke_credential(
    services: CredentialServices,
    client_id: str,
    *,
    actor: str,
    now: Optional[datetime] = None,
    reason: str = STATUS_REVOKED,
) -> Dict[str, Any]:
    """Refuse the credential at once, then remove its client, its secret (with a
    seven-day recovery window) and its gateway entry. The row stays, marked
    revoked, so the listing still shows who created and revoked it.

    The row is changed first: from that write on, the interceptor refuses every
    token from the client, so the later steps can fail without leaving a
    credential that still works."""
    now = now or _now()
    row = _require(services, client_id)
    updated = services.table.update_item(
        Key={"clientId": client_id},
        UpdateExpression="SET enabled = :f, #st = :r, revokedAt = :u, revokedBy = :b,"
        " updatedAt = :u, updatedBy = :b",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":f": False,
            ":r": STATUS_REVOKED,
            ":u": _iso(now),
            ":b": actor,
        },
        ReturnValues="ALL_NEW",
    )["Attributes"]
    errors = []
    try:
        mcp_gateway.update_allowed_clients(
            services.control, services.gateway_id, remove=[client_id]
        )
    except Exception as exc:  # noqa: BLE001 - the row already refuses the client
        errors.append(f"gateway: {exc}")
    if row.get("kind") != "stack-default":
        try:
            services.cognito.delete_user_pool_client(
                UserPoolId=services.user_pool_id, ClientId=client_id
            )
        except services.cognito.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"client: {exc}")
        try:
            services.secrets.delete_secret(
                SecretId=secret_name(services.stack_name, client_id),
                RecoveryWindowInDays=SECRET_RECOVERY_DAYS,
            )
        except services.secrets.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"secret: {exc}")
    _audit("revoke", actor, updated, reason=reason, cleanup_errors=errors)
    return public(updated)


def sweep(services: CredentialServices, *, now: Optional[datetime] = None) -> Dict:
    """The daily pass: revoke credentials past their expiry, and warn the creator
    of any expiring within ``EXPIRY_WARNING_DAYS`` once."""
    now = now or _now()
    expired, warned = [], []
    rows = _all_rows(services.table)
    for row in rows:
        if row.get("status") == STATUS_REVOKED:
            continue
        expires = _parse(row.get("expiresAt"))
        if not expires:
            continue
        if expires <= now:
            revoke_credential(
                services,
                row["clientId"],
                actor="expiry-sweep",
                now=now,
                reason=STATUS_EXPIRED,
            )
            expired.append(row["clientId"])
        elif (
            expires - now <= timedelta(days=EXPIRY_WARNING_DAYS)
            and not row.get("expiryWarnedAt")
            and not row.get("replacedBy")
        ):
            if services.notify and row.get("createdBy"):
                services.notify(
                    type_id="mcp.credential_expiring",
                    title=f"MCP credential {row.get('name')} expires soon",
                    body=(
                        f"The MCP credential {row.get('name')} ({row['clientId']}) "
                        f"expires on {row['expiresAt']}. Rotate it on the MCP "
                        f"Configuration page to keep its callers working."
                    ),
                    source="mcp-credentials",
                    users=[str(row["createdBy"])],
                    dedupe_key=f"mcp-credential-expiry:{row['clientId']}",
                )
            services.table.update_item(
                Key={"clientId": row["clientId"]},
                UpdateExpression="SET expiryWarnedAt = :u",
                ExpressionAttributeValues={":u": _iso(now)},
            )
            warned.append(row["clientId"])
    return {
        "expired": expired,
        "warned": warned,
        "reconciled": _reconcile_gateway(services, rows, expired),
    }


def _reconcile_gateway(
    services: CredentialServices, rows: Sequence[Mapping[str, Any]], expired: List[str]
) -> Dict[str, Any]:
    """Put the gateway's client list back in line with the registry: a write by a
    concurrent stale reader can drop a live credential's client or restore a
    revoked one. Disabled credentials stay listed, so enabling one again works at
    once; the interceptor refuses them meanwhile.

    The rows are the ones the sweep read when it started, so a credential created
    or revoked while it runs can be listed wrongly until the next day's sweep. A
    wrongly listed revoked credential is still refused by the interceptor and its
    app client is already gone; a wrongly unlisted new one is refused at the
    gateway until then. Only registry clients are ever added or removed."""
    revoked = [
        str(r["clientId"])
        for r in rows
        if r.get("status") == STATUS_REVOKED or r["clientId"] in expired
    ]
    live = [str(r["clientId"]) for r in rows if str(r["clientId"]) not in revoked]
    try:
        mcp_gateway.update_allowed_clients(
            services.control, services.gateway_id, add=live, remove=revoked
        )
    except Exception as exc:  # noqa: BLE001 - reported, retried tomorrow
        logger.warning("MCP credential sweep could not reconcile the gateway: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def revoke_all(
    services: CredentialServices,
    *,
    actor: str,
    now: Optional[datetime] = None,
    seconds_left: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Revoke every credential the registry holds, for the stack's own removal of
    the registry: a stack delete, a downgrade to a release without it, or MCP
    being switched off. The app clients, secrets and gateway entries were made at
    run time, so CloudFormation does not remove them, and a release without the
    registry has nothing that would refuse them.

    The steps run in order of what they stop: one gateway update takes every
    client off the gateway, so none of their tokens is accepted there; then each
    row is marked revoked; then each app client and secret is deleted. The last
    step stops while ``seconds_left()`` is under :data:`CLEANUP_RESERVE_SECONDS`,
    and what it did not reach is returned as ``unfinished``, so a Delete always
    has time to answer. A stack-owned client (``kind`` ``stack-default``) keeps
    its gateway entry and its app client: the release being returned to uses it.

    Best effort: failures are reported, never raised."""
    now = now or _now()
    rows = [r for r in _all_rows(services.table) if r.get("status") != STATUS_REVOKED]
    errors: List[str] = []
    owned = [r for r in rows if r.get("kind") != "stack-default"]
    owned_ids = [str(r["clientId"]) for r in owned]
    if owned_ids:
        try:
            mcp_gateway.update_allowed_clients(
                services.control, services.gateway_id, remove=owned_ids
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gateway: {exc}")
    for row in rows:
        try:
            services.table.update_item(
                Key={"clientId": row["clientId"]},
                UpdateExpression="SET enabled = :f, #st = :r, revokedAt = :u, "
                "revokedBy = :b, updatedAt = :u, updatedBy = :b",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":f": False,
                    ":r": STATUS_REVOKED,
                    ":u": _iso(now),
                    ":b": actor,
                },
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"row {row['clientId']}: {exc}")
    unfinished: List[str] = []
    for row in owned:
        cid = str(row["clientId"])
        if seconds_left is not None and seconds_left() < CLEANUP_RESERVE_SECONDS:
            unfinished.append(cid)
            continue
        for what, call in (
            (
                "client",
                lambda: services.cognito.delete_user_pool_client(
                    UserPoolId=services.user_pool_id, ClientId=cid
                ),
            ),
            (
                "secret",
                lambda: services.secrets.delete_secret(
                    SecretId=secret_name(services.stack_name, cid),
                    RecoveryWindowInDays=SECRET_RECOVERY_DAYS,
                ),
            ),
        ):
            try:
                call()
            except Exception as exc:  # noqa: BLE001
                if "ResourceNotFound" not in type(exc).__name__ + str(exc):
                    errors.append(f"{what} {cid}: {exc}")
        _audit("revoke", actor, {**row, "enabled": False}, reason="registry-removed")
    return {
        "revoked": [str(r["clientId"]) for r in rows],
        "errors": errors,
        "unfinished": unfinished,
    }


# --- resolving a token from a credential --------------------------------------------


def caller_from_row(
    row: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    environ: Optional[Mapping[str, str]] = None,
):
    """The MCP principal a credential row describes, or a refusal.

    Unknown, revoked, disabled and expired are each refused. The scope is the row's,
    judged against the current mode: a credential with no scope where an unscoped
    caller is denied resolves to the scope no profile matches, and an Admin
    credential there is refused, so a mode switched on after a credential was
    created narrows it at once rather than at its next edit."""
    from idp_common import mcp_access

    now = now or _now()
    if not row:
        raise mcp_access.refusal("this client is not permitted to use the MCP server.")
    status = status_of(row, now)
    if status != STATUS_ACTIVE:
        raise mcp_access.refusal(f"this MCP credential is {status}.")
    role = str(row.get("role") or "")
    if role not in ROLES:
        raise mcp_access.refusal("this MCP credential has no valid role.")
    scope = normalize_scope(row.get("allowedConfigVersions"))
    if unscoped_is_denied(environ):
        if role == "Admin":
            raise mcp_access.refusal(
                "an Admin MCP credential is not available under tenant isolation."
            )
        if scope is None:
            scope = [STRICT_UNSCOPED_DENY]
    if role == "Admin":
        scope = None
    return mcp_access.Caller(
        kind="service",
        id=str(row.get("clientId") or ""),
        name=str(row.get("name") or ""),
        roles=[role],
        scope=scope,
        tenant=row.get("tenant") or None,
        write_tools=bool(row.get("writeTools")) and role in WRITE_ROLES,
    )


def touch_last_used(table: Any, client_id: str, now: Optional[datetime] = None):
    """Record use at most once per ``LAST_USED_RESOLUTION``. Never raises."""
    now = now or _now()
    try:
        table.update_item(
            Key={"clientId": client_id},
            UpdateExpression="SET lastUsedAt = :n",
            ConditionExpression=(
                "attribute_exists(clientId) AND "
                "(attribute_not_exists(lastUsedAt) OR lastUsedAt < :cut)"
            ),
            ExpressionAttributeValues={
                ":n": _iso(now),
                ":cut": _iso(now - LAST_USED_RESOLUTION),
            },
        )
    except Exception:  # noqa: BLE001 - the condition failing is the common case
        pass
