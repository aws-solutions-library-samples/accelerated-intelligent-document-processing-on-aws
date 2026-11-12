# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who is calling an MCP tool, and what that caller may do.

The MCP server is an AgentCore Gateway with one Lambda target (the
``agentcore_mcp_handler``). The gateway validates the caller's Cognito access token
and then forwards only the tool arguments to the target, so the target never sees
who the caller is. A request interceptor Lambda sits between the two: it reads the
validated token, resolves it to a principal, refuses a tool the principal may not
call, and adds a ``_caller`` block to the arguments the target receives. The
handler then applies the principal's configuration scope to every document it
reads or returns.

This module holds every decision both halves make, so the interceptor and the
handler cannot disagree about it:

* :data:`MCP_TOOL_POLICY` — which roles may call each tool, whether the tool
  writes, and whether it is limited to callers with no configuration scope.
  ``scripts/api_rbac_expectations.yaml`` carries the same table under
  ``mcp_tools:`` and ``scripts/sdlc/scan_api_rbac.py`` fails when the two differ.
* :func:`authorize_tool` — the one check, applied by both halves.
* :func:`sign_caller` / :func:`verify_caller` — the ``_caller`` block is signed
  with a key only the interceptor and the handler can read, and bound to the
  gateway's request id. Two measured properties of the gateway make this
  necessary rather than belt and braces: it forwards tool arguments the tool
  schema does not declare, verbatim, so a client can send a ``_caller`` of its
  own; and ``UpdateGateway`` replaces the whole gateway configuration, so an
  update that omits the interceptor removes it. With the signature, a missing
  interceptor means every tool call is refused, never that a client-supplied
  block is believed.

Nothing here performs I/O at import time; the AWS lookups take their clients as
arguments so the module stays importable and testable without boto3.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: The Cognito groups this stack creates, in ascending order of privilege. A token
#: can carry other groups as well (Cognito adds one per external identity
#: provider), and those confer nothing here.
APP_ROLES = ("Viewer", "Reviewer", "Author", "Admin")

#: The roles that read documents. Annotator is a stack group too, but it reaches
#: documents only through test-set labelling, whose per-object control has no MCP
#: counterpart, so no MCP tool admits it.
DOCUMENT_ROLES = ("Admin", "Author", "Reviewer", "Viewer")

#: Never configuration-scoped, matching ``idp_common.document_scope``.
UNSCOPED_ROLES = ("Admin",)

#: The gateway prefixes every tool with its target's name and three underscores.
TARGET_NAME = "IDPTools"
TOOL_NAME_DELIMITER = "___"

#: The gateway's built-in semantic tool search. It is a ``tools/call`` whose result
#: lists tools, so the interceptor filters its result exactly as it filters
#: ``tools/list``; calling it is allowed to anyone who may list tools.
GATEWAY_SEARCH_TOOL = "x_amz_bedrock_agentcore_search"

#: The argument the interceptor adds. Any client-supplied value under this name
#: is discarded before the interceptor writes its own.
CALLER_ARGUMENT = "_caller"

CALLER_VERSION = 1

#: How long a signed caller block is accepted after the interceptor issued it. The
#: block is also bound to one gateway request id, so this only bounds how long a
#: block could be honoured at all; it is not what prevents reuse.
CALLER_MAX_AGE_SECONDS = 300

REFUSAL_PREFIX = "Unauthorized"


@dataclass(frozen=True)
class ToolPolicy:
    """Who may call one MCP tool.

    ``roles``: the groups admitted. ``write``: the tool starts processing, so a
    service credential must also have write tools enabled. ``unscoped_only``: the
    tool reads across every Configuration Profile with nothing to filter its
    output on, so it is refused to any caller who has a scope.
    """

    roles: tuple
    write: bool
    unscoped_only: bool
    equivalent_op: str


#: The authorization table for every tool the gateway target serves. Each row
#: mirrors the REST operation the tool corresponds to, so a caller gets the same
#: answer through MCP as through the web UI:
#:
#: * ``search`` runs the analytics agent, as Agent Chat does
#:   (``sendAgentChatMessage``: Admin, Author, Viewer). Its tables span every
#:   document and cannot be filtered per row, so it is also unscoped-only — which
#:   under strict tenant isolation makes it Admin-only, as Agent Chat is.
#: * ``status`` and ``get_results`` read documents, as ``getDocument`` does, and
#:   filter what they return by the caller's scope.
#: * ``process`` uploads a document (``uploadDocument``: Admin, Author).
#: * ``reprocess`` re-runs documents (``reprocessDocument``: Admin, Author).
MCP_TOOL_POLICY: Dict[str, ToolPolicy] = {
    "search": ToolPolicy(
        roles=("Admin", "Author", "Viewer"),
        write=False,
        unscoped_only=True,
        equivalent_op="sendAgentChatMessage",
    ),
    "status": ToolPolicy(
        roles=DOCUMENT_ROLES,
        write=False,
        unscoped_only=False,
        equivalent_op="getDocument",
    ),
    "get_results": ToolPolicy(
        roles=DOCUMENT_ROLES,
        write=False,
        unscoped_only=False,
        equivalent_op="getDocument",
    ),
    "process": ToolPolicy(
        roles=("Admin", "Author"),
        write=True,
        unscoped_only=False,
        equivalent_op="uploadDocument",
    ),
    "reprocess": ToolPolicy(
        roles=("Admin", "Author"),
        write=True,
        unscoped_only=False,
        equivalent_op="reprocessDocument",
    ),
}


class McpAccessRefused(PermissionError):
    """A tool call or a caller block that must not be served."""


def refusal(message: str) -> McpAccessRefused:
    return McpAccessRefused(f"{REFUSAL_PREFIX}: {message}")


def bare_tool_name(gateway_tool_name: str) -> str:
    """``IDPTools___get_results`` -> ``get_results``; other names are returned as
    given, so an unknown name stays unknown rather than being made to look like a
    known one."""
    name = str(gateway_tool_name or "")
    if TOOL_NAME_DELIMITER in name:
        prefix, rest = name.split(TOOL_NAME_DELIMITER, 1)
        if prefix == TARGET_NAME:
            return rest
    return name


def app_roles(groups: Iterable[Any]) -> List[str]:
    """The groups in ``groups`` that this stack defines, in ``APP_ROLES`` order."""
    held = {str(g) for g in groups or ()}
    return [role for role in APP_ROLES if role in held]


@dataclass
class Caller:
    """A resolved MCP principal.

    ``kind`` is ``"user"`` (a person signed in through the user-authentication
    client) or ``"service"`` (a client-credentials app). ``scope`` is ``None`` for
    unrestricted, otherwise the ``allowedConfigVersions`` list, which under strict
    isolation may be the deny-all sentinel from ``idp_common.config_scope``.
    """

    kind: str
    id: str
    name: str
    roles: List[str]
    scope: Optional[List[str]]
    tenant: Optional[str]
    write_tools: bool

    def to_payload(self) -> Dict[str, Any]:
        return {
            "v": CALLER_VERSION,
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "roles": list(self.roles),
            "scope": None if self.scope is None else list(self.scope),
            "tenant": self.tenant,
            "writeTools": bool(self.write_tools),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Caller":
        scope = payload.get("scope")
        return cls(
            kind=str(payload.get("kind") or ""),
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            roles=app_roles(payload.get("roles") or ()),
            scope=None if scope is None else [str(s) for s in scope],
            tenant=payload.get("tenant"),
            write_tools=bool(payload.get("writeTools")),
        )


def authorize_tool(caller: Caller, tool: str) -> ToolPolicy:
    """Return the tool's policy, or raise :class:`McpAccessRefused`.

    The order of the checks is the order a reader would ask them in: does the tool
    exist, does the caller hold a role it admits, may this caller write, and may a
    scoped caller use it at all.
    """
    policy = MCP_TOOL_POLICY.get(tool)
    if policy is None:
        raise refusal(f"the tool {tool!r} is not available.")
    if not any(role in policy.roles for role in caller.roles):
        raise refusal(f"your role does not permit the {tool!r} tool.")
    if policy.write and not caller.write_tools:
        raise refusal(f"write tools are not enabled for this caller ({tool!r}).")
    if policy.unscoped_only and caller.scope is not None:
        raise refusal(
            f"the {tool!r} tool reads across every configuration profile, so it is "
            f"not available to a caller with a configuration scope."
        )
    return policy


def allowed_tool_names(caller: Caller) -> List[str]:
    """Every tool :func:`authorize_tool` admits for ``caller``."""
    allowed = []
    for name in MCP_TOOL_POLICY:
        try:
            authorize_tool(caller, name)
        except McpAccessRefused:
            continue
        allowed.append(name)
    return allowed


def filter_tool_list(caller: Caller, tools: Sequence[Mapping[str, Any]]) -> list:
    """``tools`` (a ``tools/list`` result) reduced to what ``caller`` may call.

    The gateway's own search tool is kept, because its result is filtered too. Any
    other name this module does not know is dropped: a tool nobody has written a
    policy for is not a tool anyone may call.
    """
    allowed = set(allowed_tool_names(caller))
    kept = []
    for tool in tools or ():
        name = str((tool or {}).get("name") or "")
        if name == GATEWAY_SEARCH_TOOL or bare_tool_name(name) in allowed:
            kept.append(tool)
    return kept


# --- the signed caller block ------------------------------------------------


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def sign_caller(
    caller: Caller, *, key: bytes, request_id: str, now: Optional[float] = None
) -> Dict[str, Any]:
    """The ``_caller`` block for one gateway request.

    ``request_id`` is the interceptor's ``REQUEST_ID`` (from its client context),
    which the gateway hands the target as ``bedrockAgentCoreAwsRequestId``; binding
    the block to it means a block is honoured for one request only.
    """
    if not key:
        raise ValueError("a signing key is required")
    if not request_id:
        raise ValueError("a gateway request id is required")
    payload = caller.to_payload()
    payload["requestId"] = str(request_id)
    payload["iat"] = int(time.time() if now is None else now)
    payload["sig"] = _signature(key, payload)
    return payload


def verify_caller(
    block: Any,
    *,
    key: bytes,
    request_id: str,
    now: Optional[float] = None,
    max_age: int = CALLER_MAX_AGE_SECONDS,
) -> Caller:
    """The :class:`Caller` a signed block describes, or a refusal.

    Every failure is a refusal with the same message, so the response does not say
    which property of a forged block was wrong.
    """
    generic = refusal("the caller identity for this request could not be verified.")
    if not key or not request_id or not isinstance(block, Mapping):
        raise generic
    signature = block.get("sig")
    payload = {k: v for k, v in block.items() if k != "sig"}
    if not isinstance(signature, str):
        raise generic
    if not hmac.compare_digest(signature, _signature(key, payload)):
        logger.warning("Denied an MCP tool call: the caller block signature differs")
        raise generic
    if payload.get("v") != CALLER_VERSION:
        raise generic
    if str(payload.get("requestId")) != str(request_id):
        logger.warning(
            "Denied an MCP tool call: the caller block was issued for a different "
            "gateway request"
        )
        raise generic
    issued = payload.get("iat")
    current = time.time() if now is None else now
    if not isinstance(issued, int) or not (0 <= current - issued <= max_age):
        logger.warning("Denied an MCP tool call: the caller block is outside its age")
        raise generic
    return Caller.from_payload(payload)


def load_signing_key(secrets_client: Any, secret_id: str) -> bytes:
    """The signing key held in Secrets Manager under ``secret_id``.

    The secret is a stack-generated string; its bytes are the key. An empty or
    unreadable secret raises, and both callers turn that into a refusal.
    """
    if not secret_id:
        raise RuntimeError("the caller-signing secret is not configured")
    value = secrets_client.get_secret_value(SecretId=secret_id).get("SecretString")
    if not value:
        raise RuntimeError("the caller-signing secret is empty")
    return value.encode("utf-8")


# --- reading the validated access token --------------------------------------


def token_claims(authorization_header: Optional[str]) -> Dict[str, Any]:
    """The claims of the bearer token in ``authorization_header``.

    The signature is NOT checked here and does not need to be: the interceptor only
    runs after the gateway's JWT authorizer has validated the token against this
    stack's user pool and its allowed clients (a token from any other client is
    refused with 403 before an interceptor is invoked). The caller still checks the
    issuer and ``token_use`` against what it expects, so a token that reached this
    point some other way is not believed.
    """
    if not authorization_header:
        raise refusal("the request carries no access token.")
    parts = str(authorization_header).strip().split()
    token = parts[-1] if parts else ""
    segments = token.split(".")
    if len(segments) != 3:
        raise refusal("the access token is not a JWT.")
    body = segments[1] + "=" * (-len(segments[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise refusal("the access token could not be read.") from exc
    if not isinstance(claims, dict):
        raise refusal("the access token could not be read.")
    return claims


def check_token_origin(claims: Mapping[str, Any], *, expected_issuer: str) -> None:
    """Refuse a token not issued by this stack's pool as an access token."""
    if not expected_issuer or claims.get("iss") != expected_issuer:
        raise refusal("the access token was not issued by this deployment.")
    if claims.get("token_use") != "access":
        raise refusal("an access token is required.")
    if not claims.get("client_id"):
        raise refusal("the access token names no client.")


def expected_issuer(region: str, user_pool_id: str, url_suffix: str = "amazonaws.com"):
    """The ``iss`` claim of tokens from ``user_pool_id``."""
    return f"https://cognito-idp.{region}.{url_suffix}/{user_pool_id}"


# --- resolving a principal ----------------------------------------------------


def _user_attributes(response: Mapping[str, Any]) -> Dict[str, str]:
    return {
        str(a.get("Name")): str(a.get("Value"))
        for a in response.get("UserAttributes") or ()
        if a.get("Name")
    }


def resolve_user_caller(
    claims: Mapping[str, Any],
    *,
    cognito: Any,
    user_pool_id: str,
    users_table_name: str,
    dynamodb: Any,
    scope_cache: Optional[dict] = None,
) -> Caller:
    """The principal behind a user's access token.

    Groups come from the validated token, which is where the REST dispatcher reads
    them too. The configuration scope comes from the same lookup every other
    consumer uses (``resolve_allowed_config_versions``), keyed on the account's
    ``sub`` and its email address.

    An access token carries no ``email`` claim (measured against a Cognito pool), so
    the address is read from the user pool with ``AdminGetUser``. That read also
    refuses a disabled account at once, rather than when its token expires, and
    refuses a token whose ``sub`` does not match the account its username names.
    """
    from idp_common.config_scope import (
        ScopeLookupError,
        resolve_allowed_config_versions,
    )

    username = str(claims.get("username") or "")
    token_sub = str(claims.get("sub") or "")
    if not username or not token_sub:
        raise refusal("the access token does not identify a user.")
    try:
        user = cognito.admin_get_user(UserPoolId=user_pool_id, Username=username)
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal
        logger.warning("Denied an MCP request: the user could not be read (%s)", exc)
        raise refusal("the signed-in user could not be found.") from exc
    attributes = _user_attributes(user)
    if attributes.get("sub") != token_sub:
        raise refusal("the access token does not match the signed-in user.")
    if user.get("Enabled") is False:
        raise refusal("the signed-in user is disabled.")
    roles = app_roles(claims.get("cognito:groups") or ())
    if not roles:
        raise refusal("the signed-in user holds no role the MCP server admits.")
    if any(role in UNSCOPED_ROLES for role in roles):
        scope = None
    else:
        try:
            scope = resolve_allowed_config_versions(
                attributes.get("email", "").strip(),
                caller_sub=token_sub,
                users_table_name=users_table_name,
                dynamodb=dynamodb,
                cache=scope_cache,
            )
        except ScopeLookupError as exc:
            logger.warning(
                "Denied an MCP request: the user's configuration scope could not be "
                "evaluated (%s)",
                exc,
            )
            raise refusal("your configuration scope could not be determined.") from exc
    return Caller(
        kind="user",
        id=token_sub,
        name=username,
        roles=roles,
        scope=scope,
        tenant=None,
        write_tools=True,
    )


def resolve_caller(
    claims: Mapping[str, Any],
    *,
    user_client_ids: Sequence[str],
    cognito: Any,
    user_pool_id: str,
    users_table_name: str,
    dynamodb: Any,
    scope_cache: Optional[dict] = None,
    environ: Optional[Mapping[str, str]] = None,
    credential_row: Optional[Any] = None,
) -> Caller:
    """Route a validated token to the resolver for its client, or refuse it.

    ``credential_row(client_id)`` returns the MCP credential registry row for a
    client, or ``None``; a client-credentials token from any client is resolved
    from it (``idp_common.mcp_credentials``), read on every
    call so a revoked credential is refused on the next request.

    A token is a user token only when it comes from a user-authentication client
    AND names a user; a service token only when it names none and the credential
    registry has a row for its client. The stack-created client-credentials client
    is in the registry too ("Default (stack-created)"). A client with no rule here is
    refused, so a client added to the gateway's allowed list without one can
    authenticate and do nothing.
    """
    client_id = str(claims.get("client_id") or "")
    has_user = bool(claims.get("username"))
    if client_id in set(user_client_ids) and has_user:
        return resolve_user_caller(
            claims,
            cognito=cognito,
            user_pool_id=user_pool_id,
            users_table_name=users_table_name,
            dynamodb=dynamodb,
            scope_cache=scope_cache,
        )
    if credential_row is not None and not has_user and client_id:
        from idp_common.mcp_credentials import caller_from_row

        return caller_from_row(credential_row(client_id), environ=environ)
    raise refusal("this client is not permitted to use the MCP server.")


# --- configuration scope over documents ----------------------------------------


def document_keys_in_scope(
    caller: Caller, keys: Iterable[str], *, config_version_of: Any
) -> List[str]:
    """The keys in ``keys`` whose document ``caller`` may see, in order.

    ``config_version_of(key)`` returns the profile on the document's tracking row,
    ``None`` when the row records none, or ``document_scope.ABSENT`` when there is
    no row — the contract of ``document_scope.tracked_config_version``. For a
    scoped caller a document with no profile or no row is dropped, as the
    document-list resolvers drop it. An unscoped caller is not looked up at all.
    """
    from idp_common.config_scope import scope_allows
    from idp_common.document_scope import is_absent

    keys = list(keys)
    if caller.scope is None:
        return keys
    kept = []
    for key in keys:
        profile = config_version_of(key)
        if is_absent(profile):
            continue
        if scope_allows(caller.scope, profile):
            kept.append(key)
    return kept


def require_document_keys_in_scope(
    caller: Caller, keys: Iterable[str], *, config_version_of: Any
) -> None:
    """Refuse unless every key names a document ``caller`` may act on.

    One out-of-scope key refuses the whole request, so a request cannot be used to
    learn which keys are in scope one at a time. The refusal is
    ``document_scope``'s, which reads the same for a key outside the scope, a key
    with no profile and a key no document is tracked under.
    """
    from idp_common.document_scope import assert_document_in_scope

    if caller.scope is None:
        return
    for key in keys:
        assert_document_in_scope(key, caller.scope, config_version_of=config_version_of)


def require_profile_in_scope(caller: Caller, profile: Optional[str]) -> None:
    """Refuse unless ``caller`` may put a document into ``profile``.

    For a write whose destination profile has been resolved (``process``). A
    missing profile is refused for a scoped caller, as ``scope_allows`` refuses it.
    """
    from idp_common.document_scope import assert_profile_in_scope

    assert_profile_in_scope(caller.scope, profile, what="document upload")
