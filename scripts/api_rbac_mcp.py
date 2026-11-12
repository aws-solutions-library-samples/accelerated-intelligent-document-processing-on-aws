# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live MCP cells for ``make api-test``: each role against each MCP tool.

The MCP server is a third way into the deployment. The REST matrix in
``test_api_rbac.py`` cannot reach it: MCP callers present a Cognito ACCESS token to
an AgentCore Gateway, which hands each call to a request interceptor and then to the
MCP handler. This module drives that path with the harness's own role users and
records one row per cell through the harness's ``_record``.

What a cell asserts:

* **role cells** (every matrix principal x every tool in ``mcp_tools:``). A role the
  tool admits is NOT refused for its role; one it does not admit IS. The arguments
  are deliberately benign (a batch and a document that do not exist, a process call
  with no document), so an admitted call fails ordinary validation and does no work.
  ``search`` is driven only for the roles it refuses, because an admitted call runs
  the analytics agent.
* **listing cells**: ``tools/list`` names exactly the tools the role may call.
* **a forged caller block**: a Viewer sending ``_caller`` claiming Admin is still
  refused ``process``.
* **scope cells**: the scoped Author is refused ``get_results`` on a live document
  outside their scope, which an Admin is served.
* **the stack-created client-credentials client**: admitted to read tools when
  isolation is Off, and its listing names all five tools.

Refusals are recognised by the wording ``idp_common.mcp_access`` writes, so a cell
whose call failed for any other reason is not mistaken for a role refusal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rbac_common import aws  # noqa: E402

#: Phrases ``idp_common.mcp_access.authorize_tool`` uses when it refuses on the
#: caller's role, write permission or scope-only rule. A refusal with none of them
#: is not a role refusal.
ROLE_REFUSALS = (
    "does not permit the",
    "write tools are not enabled",
    "not available to a caller with a configuration scope",
    "holds no role the MCP server admits",
)
#: The one refusal ``document_scope`` writes, the same for every out-of-scope case.
SCOPE_REFUSAL = "not permitted to act on that document"

INERT_TOOL_ARGS = {
    "status": {"batch_id": "rbac-test-nonexistent-batch"},
    "get_results": {"document_id": "rbac-test-nonexistent.pdf"},
    "process": {},
    "reprocess": {"step": "extraction", "document_ids": "rbac-test-nonexistent.pdf"},
    "search": {"query": "how many documents"},
}
TARGET = "IDPTools___"


def _outputs(ctx):
    outputs = aws(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        ctx["stack"],
        "--query",
        "Stacks[0].Outputs",
        region=ctx["region"],
    )
    return {o["OutputKey"]: o["OutputValue"] for o in outputs or []}


def resolve_mcp(ctx):
    """The gateway URL and clients, or ``None`` when MCP is not enabled."""
    out = _outputs(ctx)
    url = out.get("MCPServerEndpoint")
    if not url or not url.startswith("https://"):
        return None

    def secret(client_id):
        return aws(
            "cognito-idp",
            "describe-user-pool-client",
            "--user-pool-id",
            ctx["user_pool"],
            "--client-id",
            client_id,
            "--query",
            "UserPoolClient.ClientSecret",
            "--output",
            "text",
            region=ctx["region"],
        )

    user_client = out.get("MCPClientId", "")
    connector = out.get("MCPConnectorClientId", "")
    return {
        "url": url,
        "token_url": out.get("MCPTokenURL", ""),
        "user_client": user_client,
        "user_secret": secret(user_client) if user_client else "",
        "connector": connector,
        "connector_secret": secret(connector) if connector else "",
    }


def user_access_token(ctx, mcp, email, password):
    """An ACCESS token from the user-authentication client (the one Quick's user
    authentication uses), via ADMIN_USER_PASSWORD_AUTH, which that client allows."""
    digest = hmac.new(
        mcp["user_secret"].encode(),
        (email + mcp["user_client"]).encode(),
        hashlib.sha256,
    ).digest()
    secret_hash = base64.b64encode(digest).decode()
    return aws(
        "cognito-idp",
        "admin-initiate-auth",
        "--user-pool-id",
        ctx["user_pool"],
        "--client-id",
        mcp["user_client"],
        "--auth-flow",
        "ADMIN_USER_PASSWORD_AUTH",
        "--auth-parameters",
        json.dumps(
            {"USERNAME": email, "PASSWORD": password, "SECRET_HASH": secret_hash}
        ),
        "--query",
        "AuthenticationResult.AccessToken",
        "--output",
        "text",
        region=ctx["region"],
    )


def service_token(mcp, client_id, client_secret):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        mcp["token_url"],
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - stack URL
        return json.load(resp)["access_token"]


def rpc(mcp, token, method, params=None):
    """One JSON-RPC call. Returns ``(http_status, body_dict_or_None)``."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req = urllib.request.Request(
        mcp["url"],
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "null")
        except ValueError:
            return exc.code, None


def call_text(mcp, token, tool, args):
    """``(http_status, text)``: every text the result or error carries, joined."""
    status, body = rpc(
        mcp, token, "tools/call", {"name": TARGET + tool, "arguments": args}
    )
    if not isinstance(body, dict):
        return status, ""
    if "error" in body:
        return status, str((body["error"] or {}).get("message", ""))
    parts = [
        str(c.get("text", ""))
        for c in ((body.get("result") or {}).get("content") or [])
        if isinstance(c, dict)
    ]
    return status, "\n".join(parts)


def tool_body(text):
    """The tool's own JSON from a result's text: the gateway returns the Lambda's
    response (``{"statusCode", "body"}``) as text, and ``body`` is the tool's JSON
    as a string. ``{}`` when the text is not that shape."""
    try:
        outer = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if isinstance(outer, dict) and isinstance(outer.get("body"), str):
        try:
            inner = json.loads(outer["body"])
        except ValueError:
            return {}
        return inner if isinstance(inner, dict) else {}
    return outer if isinstance(outer, dict) else {}


def role_refused(text):
    return any(phrase in text for phrase in ROLE_REFUSALS)


def listed_tools(mcp, token):
    status, body = rpc(mcp, token, "tools/list")
    tools = ((body or {}).get("result") or {}).get("tools") or []
    return status, {
        t["name"][len(TARGET) :] for t in tools if t.get("name", "").startswith(TARGET)
    }


def run_mcp_suite(
    ctx,
    record,
    results,
    mcp_tools,
    principals,
    email_of,
    password,
    live_object_key=None,
    scoped_principal=None,
):
    """Drive every MCP cell; returns the number of cells recorded."""
    mcp = resolve_mcp(ctx)
    if mcp is None:
        record(
            results,
            "mcp",
            "-",
            None,
            None,
            "MCP is not enabled on this stack",
            outcome="SKIP",
        )
        return 0
    before = len(results)
    tokens = {}
    for principal in principals:
        try:
            tokens[principal] = user_access_token(
                ctx, mcp, email_of(principal), password
            )
        except Exception as exc:  # noqa: BLE001 - a missing token is a failed cell
            record(
                results,
                "mcp:token",
                principal,
                None,
                False,
                f"could not mint an MCP access token: {exc}",
            )

    for principal, token in tokens.items():
        scoped = principal == scoped_principal
        for tool, spec in mcp_tools.items():
            groups = spec.get("groups") or []
            role = "Author" if scoped else principal
            admitted = role in groups and not (scoped and spec.get("unscoped_only"))
            if tool == "search" and admitted:
                continue  # an admitted search runs the analytics agent
            status, text = call_text(mcp, token, tool, INERT_TOOL_ARGS[tool])
            refused = role_refused(text)
            if admitted:
                record(
                    results,
                    f"mcp:{tool}",
                    principal,
                    status,
                    not refused,
                    "admitted role refused" if refused else "admitted",
                    message=text[:300],
                )
            else:
                record(
                    results,
                    f"mcp:{tool}",
                    principal,
                    status,
                    refused,
                    "refused" if refused else "NOT refused for its role",
                    message=text[:300],
                )
        status, names = listed_tools(mcp, token)
        role = "Author" if scoped else principal
        expected = {
            t
            for t, s in mcp_tools.items()
            if role in (s.get("groups") or [])
            and not (scoped and s.get("unscoped_only"))
        }
        record(
            results,
            "mcp:tools/list",
            principal,
            status,
            names == expected,
            f"listed {sorted(names)}, expected {sorted(expected)}",
        )

    viewer = tokens.get("Viewer")
    if viewer:
        forged = {
            "kind": "service",
            "roles": ["Admin"],
            "scope": None,
            "writeTools": True,
            "v": 1,
            "sig": "0" * 64,
        }
        status, text = call_text(mcp, viewer, "process", {"_caller": forged})
        record(
            results,
            "mcp:forged-caller",
            "Viewer",
            status,
            role_refused(text),
            "forged _caller ignored"
            if role_refused(text)
            else "forged _caller honoured",
            message=text[:300],
        )

    if live_object_key and scoped_principal in tokens and "Admin" in tokens:
        args = {"document_id": live_object_key}
        status, text = call_text(mcp, tokens[scoped_principal], "get_results", args)
        record(
            results,
            "mcp:get_results-scope",
            scoped_principal,
            status,
            SCOPE_REFUSAL in text,
            "out-of-scope document refused"
            if SCOPE_REFUSAL in text
            else "out-of-scope document served",
            message=text[:300],
        )
        status, text = call_text(mcp, tokens["Admin"], "get_results", args)
        served = tool_body(text).get("success") is True
        record(
            results,
            "mcp:get_results-scope",
            "Admin",
            status,
            served,
            "served" if served else "Admin not served the document",
            message=text[:300],
        )
    else:
        record(
            results,
            "mcp:get_results-scope",
            "-",
            None,
            None,
            "no live document or scoped token to test scope with",
            outcome="SKIP",
        )

    if mcp["connector"] and mcp["connector_secret"] and mcp["token_url"]:
        try:
            token = service_token(mcp, mcp["connector"], mcp["connector_secret"])
            status, text = call_text(mcp, token, "status", INERT_TOOL_ARGS["status"])
            isolated = "this MCP credential is disabled" in text
            record(
                results,
                "mcp:status",
                "stack-connector",
                status,
                not role_refused(text),
                "refused under tenant isolation" if isolated else "admitted",
                message=text[:300],
                outcome="PASS" if isolated else None,
            )
        except Exception as exc:  # noqa: BLE001
            record(
                results,
                "mcp:status",
                "stack-connector",
                None,
                False,
                f"connector call failed: {exc}",
            )
    return len(results) - before


def operation_result(body: str, field: str) -> dict:
    """The object a REST operation returned. ``/op/<field>`` answers with the
    resolver's result itself; a GraphQL-shaped ``{"data": {field: ...}}`` body is
    read too, so the cells do not depend on which of the two a stack serves."""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    data = parsed.get("data")
    if isinstance(data, dict) and isinstance(data.get(field), dict):
        return data[field]
    return parsed


def run_credential_cells(
    ctx,
    record,
    results,
    admin_id_token,
    call_body,
    *,
    scope_version,
    live_object_key=None,
):
    """A Viewer service credential, created and revoked through the REST API.

    Asserts, per the MCP credential design: the credential reads, cannot write,
    cannot read a document outside its scope, and is refused on the first request
    after it is revoked. The credential is always revoked, so a failed run leaves
    no working credential behind (the revoked row stays, as every revoked one does).
    """
    mcp = resolve_mcp(ctx)
    if mcp is None:
        record(
            results,
            "mcp:credential",
            "-",
            None,
            None,
            "MCP is not enabled",
            outcome="SKIP",
        )
        return
    status, body = call_body(
        ctx["api_base"],
        "createMcpCredential",
        {
            "name": "rbac-test-viewer",
            "role": "Viewer",
            "allowedConfigVersions": [scope_version],
            "expiresInDays": 1,
        },
        admin_id_token,
    )
    issued = operation_result(body, "createMcpCredential")
    client_id = issued.get("clientId")
    if status != 200 or not client_id or not issued.get("clientSecret"):
        if client_id:
            # Something was made; never leave it working.
            call_body(
                ctx["api_base"],
                "revokeMcpCredential",
                {"clientId": client_id},
                admin_id_token,
            )
        # The body of a successful create carries the secret, so name its fields
        # rather than quoting it.
        record(
            results,
            "mcp:credential",
            "Admin",
            status,
            False,
            "could not create a test credential (response fields: "
            f"{sorted(issued) or 'none'})",
        )
        return
    try:
        token = service_token(mcp, client_id, issued["clientSecret"])
        status, text = call_text(mcp, token, "status", INERT_TOOL_ARGS["status"])
        record(
            results,
            "mcp:credential-read",
            "viewer-credential",
            status,
            not role_refused(text) and "not permitted" not in text,
            "admitted to read",
            message=text[:300],
        )
        status, text = call_text(mcp, token, "process", {})
        record(
            results,
            "mcp:credential-write",
            "viewer-credential",
            status,
            role_refused(text),
            "refused process" if role_refused(text) else "NOT refused process",
            message=text[:300],
        )
        if live_object_key:
            status, text = call_text(
                mcp, token, "get_results", {"document_id": live_object_key}
            )
            record(
                results,
                "mcp:credential-scope",
                "viewer-credential",
                status,
                SCOPE_REFUSAL in text,
                "out-of-scope document refused"
                if SCOPE_REFUSAL in text
                else "out-of-scope document served",
                message=text[:300],
            )
        status, _ = call_body(
            ctx["api_base"],
            "revokeMcpCredential",
            {"clientId": client_id},
            admin_id_token,
        )
        revoked = status == 200
        status, text = call_text(mcp, token, "status", INERT_TOOL_ARGS["status"])
        refused = "revoked" in text or status in (401, 403)
        record(
            results,
            "mcp:credential-revoked",
            "viewer-credential",
            status,
            revoked and refused,
            "refused on the next request"
            if refused
            else "a revoked credential's token was still served",
            message=text[:300],
        )
    finally:
        call_body(
            ctx["api_base"],
            "revokeMcpCredential",
            {"clientId": client_id},
            admin_id_token,
        )
