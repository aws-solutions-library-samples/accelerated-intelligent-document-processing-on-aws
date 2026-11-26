# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MCP service credentials: the REST operations behind the MCP Configuration page,
and the daily expiry sweep.

Every operation is Admin-only and is refused here before anything is read, as the
dispatcher's group floor refuses it first. The rules for a credential's role and
scope, and every write, are in ``idp_common.mcp_credentials``, which ``idp-cli mcp
credentials`` calls too.

A scoped Admin (one whose Users-table row carries ``allowedConfigVersions``) can
grant a credential only profiles inside their own scope, and cannot create an
unscoped one, so the page is never a way to widen one's own reach. A scoped Admin
lists only the credentials inside their scope.

The function is also the service token of ``McpCredentialsCleanup``, whose Delete
revokes every credential: on a stack delete, a downgrade to a release without the
registry, or MCP being switched off. Those credentials were made at run time, so
nothing else removes them.

A gateway update can take minutes and an API request must answer in well under
thirty seconds, so the API path does not wait for one. ``revokeMcpCredential``
writes the revoked row (refused from then on) and leaves the gateway entry, app
client and secret to a finishing task; ``createMcpCredential`` and
``rotateMcpCredential`` wait for the gateway only until
:data:`API_GATEWAY_STEP_SECONDS` and otherwise leave the addition to one. A
finishing task is this function invoking itself asynchronously with an internal
event (:func:`_is_internal_task`); the daily sweep finishes any it does not.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import boto3
import cfnresponse
from botocore.config import Config

from idp_common import mcp_credentials as mc
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    is_pattern,
    normalize_scope,
    resolve_allowed_config_versions,
    scope_allows,
)
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ADMIN_OPERATIONS = (
    "listMcpCredentials",
    "createMcpCredential",
    "updateMcpCredential",
    "rotateMcpCredential",
    "revokeMcpCredential",
    "setMcpCredentialTenant",
)

#: Timeouts for every AWS call an API request makes. The botocore defaults (a
#: 60-second read, several attempts) would let one hung call outlast the whole
#: request, since the deadline below is only checked between calls. With these,
#: one call costs at most API_CALL_WORST_SECONDS.
API_CONNECT_TIMEOUT_SECONDS = 1
API_READ_TIMEOUT_SECONDS = 3
API_MAX_ATTEMPTS = 2
API_CALL_WORST_SECONDS = (
    API_CONNECT_TIMEOUT_SECONDS + API_READ_TIMEOUT_SECONDS
) * API_MAX_ATTEMPTS
API_CLIENT_CONFIG = Config(
    connect_timeout=API_CONNECT_TIMEOUT_SECONDS,
    read_timeout=API_READ_TIMEOUT_SECONDS,
    retries={"total_max_attempts": API_MAX_ATTEMPTS, "mode": "standard"},
)

#: Seconds after the handler starts by which the gateway step of a create or
#: rotate reached through the API gives up waiting and is finished out of band.
#: The dispatcher stops waiting for this function 20 seconds after invoking it
#: (``_RESOLVER_READ_TIMEOUT_SECONDS`` in the dispatcher, itself inside API
#: Gateway's 29-second integration limit), and that clock includes this
#: function's cold start, which happens before the handler starts. Eight, plus
#: one call already in flight at the deadline (API_CALL_WORST_SECONDS, 8), leaves
#: about four for the cold start, the marker write and the self-invoke, each of
#: which is itself bounded by API_CLIENT_CONFIG.
API_GATEWAY_STEP_SECONDS = 8

#: Seconds a finishing task keeps in hand before the function's own timeout.
TASK_RESERVE_SECONDS = 15

#: The key of the internal event that runs a finishing task.
TASK_EVENT_KEY = "mcpCredentialTask"

_clients: Dict[Any, Any] = {}


def _client(name: str, api: bool = False):
    """A cached client. ``api=True`` is the API path's, with
    :data:`API_CLIENT_CONFIG`; the sweep, the finishing tasks and the custom
    resource run asynchronously with minutes to spare and keep the defaults, so
    a slow gateway read there is waited for rather than abandoned."""
    key = (name, api)
    if key not in _clients:
        kwargs = {"config": API_CLIENT_CONFIG} if api else {}
        _clients[key] = (
            boto3.resource("dynamodb", **kwargs)
            if name == "dynamodb"
            else boto3.client(name, **kwargs)
        )
    return _clients[key]


def _tenants() -> List[Dict[str, Any]]:
    from idp_common.config.tenants import TenantStore

    table_name = os.environ.get("CONFIGURATION_TABLE_NAME", "")
    if not table_name:
        raise RuntimeError(
            "CONFIGURATION_TABLE_NAME is not set, so tenant boundaries cannot be "
            "checked"
        )
    return TenantStore(_client("dynamodb", api=True).Table(table_name)).list()


def _notify(**kwargs):
    from idp_common.notifications import notify

    return notify(**kwargs)


def services(defer=None, api: bool = False) -> mc.CredentialServices:
    return mc.CredentialServices(
        cognito=_client("cognito-idp", api),
        secrets=_client("secretsmanager", api),
        table=_client("dynamodb", api).Table(os.environ["MCP_CREDENTIALS_TABLE"]),
        control=_client("bedrock-agentcore-control", api),
        gateway_id=os.environ["GATEWAY_ID"],
        user_pool_id=os.environ["USER_POOL_ID"],
        stack_name=os.environ["STACK_NAME"],
        kms_key_id=os.environ["KMS_KEY_ID"],
        oauth_scopes=[s for s in os.environ.get("OAUTH_SCOPES", "").split(",") if s],
        token_url=os.environ.get("TOKEN_URL", ""),
        gateway_url=os.environ.get("GATEWAY_URL", ""),
        tenants=_tenants,
        notify=_notify,
        defer=defer,
    )


def _function_name(context: Any) -> str:
    return getattr(context, "function_name", None) or os.environ.get(
        "AWS_LAMBDA_FUNCTION_NAME", ""
    )


def _self_invoker(context: Any, api: bool = False):
    """``CredentialServices.defer`` for this function: an asynchronous invoke of
    itself with the internal task event. It raises on failure, and
    ``idp_common.mcp_credentials`` logs that and leaves the row's marker for the
    sweep, so a request whose finishing task could not be scheduled still
    succeeds."""
    name = _function_name(context)

    def defer(task: str, client_id: str) -> None:
        if not name:
            raise RuntimeError("this function's own name is not known")
        _client("lambda", api).invoke(
            FunctionName=name,
            InvocationType="Event",
            Payload=json.dumps(
                {TASK_EVENT_KEY: {"task": task, "clientId": client_id}}
            ).encode("utf-8"),
        )

    return defer


def _is_internal_task(event: Any) -> bool:
    """A finishing task this function scheduled for itself.

    The REST dispatcher cannot produce one. Every event it sends is built by
    ``idp_common.api_adapter.normalize_event`` from fixed keys (``arguments``,
    ``identity``, ``info`` and ``_httpApiEvent``), with the request body only ever
    nested under ``arguments``; the one event it passes through unchanged is
    recognised by carrying ``arguments`` and ``identity``. An event with either of
    those keys is therefore never a task. Anything else would have to be a direct
    ``lambda:InvokeFunction`` on this function, and a principal holding that can
    already send any operation here with claims of its choosing. And a task
    carries no authority of its own: it
    names a client id, and both tasks only move a credential towards what its row
    already records (cleanup only of a revoked row, admission only of a live one),
    which the daily sweep does too."""
    return (
        isinstance(event, dict)
        and isinstance(event.get(TASK_EVENT_KEY), dict)
        and "arguments" not in event
        and "identity" not in event
        and "info" not in event
    )


def _run_task(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    task = event[TASK_EVENT_KEY].get("task")
    client_id = event[TASK_EVENT_KEY].get("clientId")
    if task not in mc.TASKS or not isinstance(client_id, str) or not client_id:
        logger.warning(f"Rejecting a malformed MCP credential task: {task!r}")
        return {"complete": False, "skipped": "malformed task"}
    deadline = None
    if context is not None:
        remaining = context.get_remaining_time_in_millis() / 1000
        deadline = time.monotonic() + max(0.0, remaining - TASK_RESERVE_SECONDS)
    result = mc.run_task(
        services(defer=_self_invoker(context)), task, client_id, deadline=deadline
    )
    if result.get("complete") or result.get("skipped"):
        logger.info(f"MCP credential task {task}: {json.dumps(result)}")
    else:
        logger.warning(
            f"MCP credential task {task} unfinished; the daily sweep retries it: "
            f"{json.dumps(result)}"
        )
    return result


def _claims(event: Dict[str, Any]) -> Dict[str, Any]:
    identity = event.get("identity") or {}
    return identity.get("claims") or {}


def _actor(claims: Dict[str, Any]) -> str:
    """Who to record as having made a change. A label for the audit line and the
    row's ``createdBy``/``updatedBy``, never a scope key."""
    email = caller_email_from_claims(claims)
    if email:
        return email
    return caller_sub_from_claims(claims) or ""


def _is_admin(claims: Dict[str, Any]) -> bool:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return "Admin" in groups


def admin_scope(claims: Dict[str, Any]) -> Optional[List[str]]:
    """The calling Admin's own ``allowedConfigVersions``, or ``None``.

    An Admin with no row, or an unscoped one under tenant isolation (the deny-all
    sentinel), is unrestricted, as Admins are everywhere else. A lookup that fails
    refuses the request."""
    try:
        scope = resolve_allowed_config_versions(
            caller_email_from_claims(claims),
            caller_sub=caller_sub_from_claims(claims),
            users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
            dynamodb=_client("dynamodb", api=True),
        )
    except ScopeLookupError as exc:
        raise PermissionError(
            "Unauthorized: your configuration scope could not be determined."
        ) from exc
    if scope == [STRICT_UNSCOPED_DENY]:
        return None
    return scope


def require_within(own: Optional[List[str]], requested: Any) -> None:
    """Refuse a credential scope that reaches beyond the Admin's own.

    A literal profile must be one the Admin's scope allows; a pattern must be one of
    the Admin's own entries, because whether one pattern contains another is not
    decided by matching. An unscoped credential needs an unscoped Admin."""
    if own is None:
        return
    entries = normalize_scope(requested)
    if not entries:
        raise PermissionError(
            "Unauthorized: an Admin with a configuration scope can create only "
            "credentials scoped inside it."
        )
    for entry in entries:
        allowed = entry in own if is_pattern(entry) else scope_allows(own, entry)
        if not allowed:
            raise PermissionError(
                "Unauthorized: that scope reaches beyond your own configuration scope."
            )


def _inside(own: List[str], row: Dict[str, Any]) -> bool:
    try:
        require_within(own, row.get("allowedConfigVersions"))
    except PermissionError:
        return False
    return True


CLEANUP_PHYSICAL_ID = "mcp-credentials-cleanup"


def _is_custom_resource(event: Any) -> bool:
    """A CloudFormation request. The REST dispatcher builds every API event from
    fixed keys, so an API caller cannot produce these."""
    return (
        isinstance(event, dict)
        and bool(event.get("RequestType"))
        and bool(event.get("ResponseURL"))
        and bool(event.get("StackId"))
    )


def _custom_resource(event: Dict[str, Any], context: Any) -> None:
    """``McpCredentialsCleanup``. Create and Update do nothing; Delete revokes every
    credential and always reports success, so a stack delete or downgrade is never
    held up by cleanup that can be finished by hand."""
    if event["RequestType"] == "Delete":
        try:
            result = mc.revoke_all(
                services(),
                actor="cloudformation",
                seconds_left=(
                    (lambda: context.get_remaining_time_in_millis() / 1000)
                    if context is not None
                    else None
                ),
            )
            logger.info(f"MCP credentials revoked on registry removal: {result}")
        except Exception as exc:  # noqa: BLE001 - a Delete must not fail
            logger.warning(f"MCP credential cleanup incomplete: {exc}")
    cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, CLEANUP_PHYSICAL_ID)


def _int(value: Any, name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise mc.CredentialError(f"{name} must be a whole number.") from exc


def _scope_arg(arguments: Dict[str, Any]) -> Any:
    value = arguments.get("allowedConfigVersions")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    return value


def _dispatch(
    field: str,
    arguments: Dict[str, Any],
    claims: Dict[str, Any],
    context: Any = None,
    started: Optional[float] = None,
):
    svc = services(defer=_self_invoker(context, api=True), api=True)
    actor = _actor(claims)
    gateway_deadline = (
        time.monotonic() if started is None else started
    ) + API_GATEWAY_STEP_SECONDS
    if field == "listMcpCredentials":
        rows = mc.list_credentials(svc)
        own = admin_scope(claims)
        return rows if own is None else [r for r in rows if _inside(own, r)]
    if field == "createMcpCredential":
        scope = _scope_arg(arguments)
        if arguments.get("role") != "Admin":
            require_within(admin_scope(claims), scope)
        elif admin_scope(claims) is not None:
            raise PermissionError(
                "Unauthorized: an Admin with a configuration scope cannot create an "
                "Admin credential."
            )
        return mc.create_credential(
            svc,
            name=arguments.get("name", ""),
            role=arguments.get("role", ""),
            allowed_config_versions=scope,
            write_tools=bool(arguments.get("writeTools")),
            expires_in_days=_int(arguments.get("expiresInDays"), "expiresInDays"),
            actor=actor,
            gateway_deadline=gateway_deadline,
        )
    if field == "updateMcpCredential":
        scope = _scope_arg(arguments)
        own = admin_scope(claims)
        if own is not None:
            row = mc.get_row(svc.table, arguments.get("clientId", "")) or {}
            require_within(own, row.get("allowedConfigVersions"))
            if scope is not None or arguments.get("clearScope"):
                require_within(own, scope)
        return mc.update_credential(
            svc,
            arguments.get("clientId", ""),
            role=arguments.get("role"),
            allowed_config_versions=scope,
            write_tools=arguments.get("writeTools"),
            enabled=arguments.get("enabled"),
            clear_scope=bool(arguments.get("clearScope")),
            actor=actor,
        )
    if field == "rotateMcpCredential":
        own = admin_scope(claims)
        if own is not None:
            row = mc.get_row(svc.table, arguments.get("clientId", "")) or {}
            require_within(own, row.get("allowedConfigVersions"))
        overlap = _int(arguments.get("overlapDays"), "overlapDays")
        return mc.rotate_credential(
            svc,
            arguments.get("clientId", ""),
            overlap_days=mc.MAX_ROTATION_OVERLAP_DAYS if overlap is None else overlap,
            actor=actor,
            gateway_deadline=gateway_deadline,
            defer_cleanup=True,
        )
    if field == "revokeMcpCredential":
        own = admin_scope(claims)
        if own is not None:
            row = mc.get_row(svc.table, arguments.get("clientId", "")) or {}
            require_within(own, row.get("allowedConfigVersions"))
        return mc.revoke_credential(
            svc, arguments.get("clientId", ""), actor=actor, defer_cleanup=True
        )
    if field == "setMcpCredentialTenant":
        tenant_id = arguments.get("tenantId") or None
        own = admin_scope(claims)
        if own is not None:
            row = mc.get_row(svc.table, arguments.get("clientId", "")) or {}
            require_within(own, row.get("allowedConfigVersions"))
            # Granting a tenant sets the scope to its profile pattern, so that
            # pattern must be inside the Admin's own scope. An unknown tenant is
            # refused by set_credential_tenant; removing a tenant only narrows.
            pattern = next(
                (
                    t.get("profilePattern")
                    for t in svc.tenants()
                    if t.get("tenantId") == tenant_id
                ),
                None,
            )
            if tenant_id and pattern:
                require_within(own, [pattern])
        return mc.set_credential_tenant(
            svc,
            arguments.get("clientId", ""),
            tenant_id,
            actor=actor,
        )
    raise ValueError(f"Unknown operation: {field}")


def handler(event: Dict[str, Any], context: Any):
    started = time.monotonic()
    if _is_custom_resource(event):
        return _custom_resource(event, context)
    if _is_internal_task(event):
        return _run_task(event, context)
    if isinstance(event, dict) and event.get("source") == "aws.events":
        result = mc.sweep(
            services(defer=_self_invoker(context)),
            seconds_left=(
                (lambda: context.get_remaining_time_in_millis() / 1000)
                if context is not None
                else None
            ),
        )
        logger.info(f"MCP credential sweep: {json.dumps(result)}")
        return result
    logger.info(f"Received event: {sanitize_event_for_logging(event)}")
    field = (event.get("info") or {}).get("fieldName", "")
    arguments = event.get("arguments") or {}
    claims = _claims(event)
    if field not in ADMIN_OPERATIONS:
        raise ValueError(f"Unknown operation: {field}")
    if not _is_admin(claims):
        logger.warning(f"Denied {field}: the caller is not in the Admin group")
        raise PermissionError("Unauthorized: Admin group membership required")
    try:
        return _dispatch(field, arguments, claims, context, started)
    except mc.CredentialNotFound as exc:
        raise ValueError(str(exc)) from exc
    except mc.CredentialError as exc:
        raise ValueError(str(exc)) from exc
