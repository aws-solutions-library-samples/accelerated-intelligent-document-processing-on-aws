# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway interceptor that identifies MCP callers.

Configured on the stack's MCP gateway for both interception points, with request
headers passed, so it sees the caller's access token after the gateway's JWT
authorizer has validated it.

On a request:

* ``tools/call`` resolves the caller (``idp_common.mcp_access.resolve_caller``),
  refuses a tool the caller may not use, discards any ``_caller`` argument the
  client sent and adds a signed one bound to this gateway request, which the MCP
  handler verifies. The gateway's own semantic tool search is forwarded unchanged;
  its result is filtered on the way back.
* ``tools/list`` is refused to a caller who cannot be resolved.
* every other method (``initialize``, ``ping``, notifications) passes through.

On a response, ``tools/list`` and the gateway's semantic tool search are reduced to
the tools the caller may call; every other response passes through unchanged.

Any exception propagates. The gateway answers an interceptor failure with an
internal error and does not invoke the target, so failing here refuses the call.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import boto3

from idp_common import mcp_access

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

OUTPUT_VERSION = "1.0"

# Created on first use, so importing this module needs no region.
_clients: Dict[str, Any] = {}
_signing_key: Dict[str, bytes] = {}
# Resolved scopes, per container, for SCOPE_CACHE_TTL_SECONDS. Only successful
# lookups are stored (see config_scope).
_scope_cache: Dict[str, Any] = {}
# Resolved signed-in users, per container, keyed on the token itself (its subject,
# client and issue time), for USER_CACHE_SECONDS. A tools/call is resolved on the
# request and a tools/list on both request and response, and each resolution reads
# the user pool, whose read quota the web UI shares. Only users are cached; the
# window bounds how long a user disabled in Cognito keeps their open token working
# here, on top of the token's own lifetime.
USER_CACHE_SECONDS = 30.0
_user_cache: Dict[tuple, Any] = {}


def _client(name: str):
    if name not in _clients:
        if name == "dynamodb":
            _clients[name] = boto3.resource("dynamodb")
        else:
            _clients[name] = boto3.client(name)
    return _clients[name]


def _key() -> bytes:
    """The caller-signing key, read once per container."""
    secret_id = os.environ.get("CALLER_SIGNING_SECRET_ARN", "")
    if secret_id not in _signing_key:
        _signing_key[secret_id] = mcp_access.load_signing_key(
            _client("secretsmanager"), secret_id
        )
    return _signing_key[secret_id]


def _ids(name: str):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _authorization(request: Dict[str, Any]) -> Optional[str]:
    for name, value in (request.get("headers") or {}).items():
        if str(name).lower() == "authorization":
            return value
    return None


def _resolve(request: Dict[str, Any]) -> mcp_access.Caller:
    claims = mcp_access.token_claims(_authorization(request))
    region = os.environ.get("AWS_REGION", "")
    mcp_access.check_token_origin(
        claims,
        expected_issuer=mcp_access.expected_issuer(
            region,
            os.environ.get("USER_POOL_ID", ""),
            os.environ.get("URL_SUFFIX", "amazonaws.com"),
        ),
    )
    key = (
        str(claims.get("client_id") or ""),
        str(claims.get("sub") or ""),
        str(claims.get("iat") or ""),
    )
    cached = _user_cache.get(key)
    if cached and time.time() - cached[0] < USER_CACHE_SECONDS:
        return cached[1]
    caller = mcp_access.resolve_caller(
        claims,
        user_client_ids=_ids("MCP_USER_CLIENT_IDS"),
        stack_service_client_ids=_ids("MCP_STACK_SERVICE_CLIENT_IDS"),
        cognito=_client("cognito-idp"),
        user_pool_id=os.environ.get("USER_POOL_ID", ""),
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=_client("dynamodb"),
        scope_cache=_scope_cache,
    )
    if caller.kind == "user":
        if len(_user_cache) > 1000:
            _user_cache.clear()
        _user_cache[key] = (time.time(), caller)
    return caller


def _request_id(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    request_id = custom.get("REQUEST_ID")
    if not request_id:
        raise RuntimeError("the gateway passed no REQUEST_ID in the client context")
    return str(request_id)


def _forward(body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def _respond(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {"transformedGatewayResponse": {"statusCode": status, "body": body}},
    }


def _refuse(body: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    """A refusal the client and its model can read.

    A refused ``tools/call`` is a tool result with ``isError``, which MCP clients
    show to the model; anything else is a JSON-RPC error.
    """
    message = str(exc)
    rpc_id = body.get("id")
    if body.get("method") == "tools/call":
        result = {"isError": True, "content": [{"type": "text", "text": message}]}
        return _respond(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return _respond(
        200,
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": message}},
    )


def _on_request(body: Dict[str, Any], request: Dict[str, Any], context: Any):
    method = body.get("method")
    if method not in ("tools/call", "tools/list"):
        return _forward(body)
    try:
        caller = _resolve(request)
        if method == "tools/list":
            return _forward(body)
        params = dict(body.get("params") or {})
        name = str(params.get("name") or "")
        if name == mcp_access.GATEWAY_SEARCH_TOOL:
            return _forward(body)
        tool = mcp_access.bare_tool_name(name)
        mcp_access.authorize_tool(caller, tool)
    except mcp_access.McpAccessRefused as exc:
        logger.warning("Refused MCP %s: %s", method, exc)
        return _refuse(body, exc)
    arguments = dict(params.get("arguments") or {})
    arguments.pop(mcp_access.CALLER_ARGUMENT, None)
    arguments[mcp_access.CALLER_ARGUMENT] = mcp_access.sign_caller(
        caller, key=_key(), request_id=_request_id(context), now=time.time()
    )
    params["arguments"] = arguments
    forwarded = dict(body)
    forwarded["params"] = params
    logger.info(
        "Forwarding MCP tool %s for %s caller %s (scoped=%s)",
        tool,
        caller.kind,
        caller.id,
        caller.scope is not None,
    )
    return _forward(forwarded)


def _filtered_list_body(caller, response_body: Dict[str, Any]) -> Dict[str, Any]:
    """The listing rebuilt from the fields this filter knows, so a field the gateway
    adds later cannot carry an unfiltered copy of the tools past it."""
    out = dict(response_body)
    result = out.get("result") or {}
    rebuilt: Dict[str, Any] = {
        "tools": mcp_access.filter_tool_list(caller, result.get("tools") or [])
    }
    if result.get("nextCursor"):
        rebuilt["nextCursor"] = result["nextCursor"]
    out["result"] = rebuilt
    return out


def _filtered_search_body(caller, response_body: Dict[str, Any]) -> Dict[str, Any]:
    """The gateway's semantic search returns its tools as JSON text in the result.
    The result is rebuilt from ``content`` and ``isError`` only, so any other field
    (``structuredContent``, for one) cannot carry the unfiltered list past it."""
    out = dict(response_body)
    result = out.get("result") or {}
    content = []
    for item in result.get("content") or []:
        item = dict(item)
        if item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text") or "")
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list):
                parsed["tools"] = mcp_access.filter_tool_list(caller, parsed["tools"])
                item["text"] = json.dumps(parsed)
            else:
                # Not the shape this filter knows, so nothing in it is shown.
                item["text"] = json.dumps({"tools": []})
        content.append(item)
    out["result"] = {"content": content, "isError": bool(result.get("isError"))}
    return out


def _on_response(body, request, response):
    status = response.get("statusCode", 200)
    response_body = response.get("body") or {}
    method = body.get("method")
    name = str((body.get("params") or {}).get("name") or "")
    filters_list = method == "tools/list"
    filters_search = method == "tools/call" and name == mcp_access.GATEWAY_SEARCH_TOOL
    if not (filters_list or filters_search) or "result" not in response_body:
        return _respond(status, response_body)
    try:
        caller = _resolve(request)
    except mcp_access.McpAccessRefused as exc:
        return _refuse(body, exc)
    if filters_list:
        return _respond(status, _filtered_list_body(caller, response_body))
    return _respond(status, _filtered_search_body(caller, response_body))


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    mcp = (event or {}).get("mcp") or {}
    request = mcp.get("gatewayRequest") or {}
    body = request.get("body") or {}
    response = mcp.get("gatewayResponse")
    if response is not None:
        return _on_response(body, request, response)
    return _on_request(body, request, context)
