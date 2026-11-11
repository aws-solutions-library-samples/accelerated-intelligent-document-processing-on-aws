# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The AgentCore Gateway request/response interceptor that identifies MCP callers.

The events are the shape a live gateway sent an interceptor (us-west-2, recorded
when this function was written):

* ``{"interceptorInputVersion": "1.0", "mcp": {"rawGatewayRequest": {"body": str},
  "gatewayRequest": {"path": "/mcp", "httpMethod": "POST", "headers": {...},
  "body": {...}, "context": None}, "gatewayResponse": None | {...}}}``;
* the ``Authorization`` header is present because the interceptor is configured
  with ``passRequestHeaders``;
* the interceptor's client context carries ``REQUEST_ID``, which the gateway passes
  to the target as ``bedrockAgentCoreAwsRequestId``.

Cognito, the Users table and the signing secret are moto, so the caller resolution
runs end to end rather than against stubs that would decide the answer.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from idp_common import mcp_access as ma
from idp_common.config_scope import TENANT_GRANT_SOURCE_ENV, TENANT_ISOLATION_ENV

pytestmark = pytest.mark.unit

REGION = "us-east-1"
INTERCEPTOR = (
    Path(__file__).resolve().parents[5] / "src/lambda/mcp_identity_interceptor/index.py"
)
REQUEST_ID = "184bd53c-b0bc-48a5-8d59-bcf4b4ada42c"
USER_CLIENT = "6g97km074t91v627s3ugutgiae"
SERVICE_CLIENT = "50hmqfukml41lhoj31r0dchceh"


def _jwt(claims):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256', 'kid': 'k'})}.{seg(claims)}.c2lnbmF0dXJl"


def _context(request_id=REQUEST_ID):
    custom = {
        "GATEWAY_ARN": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/g"
    }
    if request_id:
        custom["REQUEST_ID"] = request_id
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def _event(body, token=None, response=None, header="Authorization"):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers[header] = f"Bearer {token}"
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": json.dumps(body)},
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": headers,
                "body": body,
                "context": None,
            },
            "gatewayResponse": response,
        },
    }


def _call(name, arguments=None, rid=1):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(TENANT_GRANT_SOURCE_ENV, raising=False)
    with mock_aws():
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        subs = {}
        for name, email in (
            ("scoped", "scoped@example.com"),
            ("open", "o@example.com"),
        ):
            user = cognito.admin_create_user(
                UserPoolId=pool,
                Username=name,
                UserAttributes=[{"Name": "email", "Value": email}],
            )["User"]
            subs[name] = next(
                a["Value"] for a in user["Attributes"] if a["Name"] == "sub"
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName="users",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(
            Item={
                "PK": "USER#1",
                "SK": "USER#1",
                "email": "scoped@example.com",
                "allowedConfigVersions": ["acme-*"],
            }
        )
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="stack/mcp/caller-signing-key", SecretString="s" * 64
        )["ARN"]
        env = {
            "AWS_REGION": REGION,
            "USER_POOL_ID": pool,
            "USERS_TABLE_NAME": "users",
            "MCP_USER_CLIENT_IDS": USER_CLIENT,
            "MCP_STACK_SERVICE_CLIENT_IDS": SERVICE_CLIENT,
            "CALLER_SIGNING_SECRET_ARN": secret,
            "URL_SUFFIX": "amazonaws.com",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        spec = importlib.util.spec_from_file_location(
            "mcp_interceptor_index", INTERCEPTOR
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["mcp_interceptor_index"] = module
        spec.loader.exec_module(module)
        issuer = ma.expected_issuer(REGION, pool)
        yield SimpleNamespace(
            mod=module, pool=pool, subs=subs, issuer=issuer, key=b"s" * 64
        )
        sys.modules.pop("mcp_interceptor_index", None)


def user_token(world, name, *groups, client=USER_CLIENT):
    return _jwt(
        {
            "iss": world.issuer,
            "token_use": "access",
            "client_id": client,
            "sub": world.subs[name],
            "username": name,
            "cognito:groups": list(groups),
            "scope": "openid email profile",
        }
    )


def service_token(world, client=SERVICE_CLIENT):
    return _jwt(
        {
            "iss": world.issuer,
            "token_use": "access",
            "client_id": client,
            "sub": client,
            "scope": "idp-mcp-connector/access",
        }
    )


def forwarded_args(result):
    assert result["interceptorOutputVersion"] == "1.0"
    assert "transformedGatewayResponse" not in result["mcp"], result
    return result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]


def refusal_text(result):
    response = result["mcp"]["transformedGatewayResponse"]
    assert "transformedGatewayRequest" not in result["mcp"]
    body = response["body"]
    if "error" in body:
        return body["error"]["message"]
    assert body["result"]["isError"] is True
    return body["result"]["content"][0]["text"]


# --- tools/call -----------------------------------------------------------------


def test_an_allowed_call_is_forwarded_with_a_signed_caller(world):
    event = _event(
        _call("IDPTools___get_results", {"document_id": "a.pdf"}),
        user_token(world, "scoped", "Viewer"),
    )
    args = forwarded_args(world.mod.handler(event, _context()))
    assert args["document_id"] == "a.pdf"
    caller = ma.verify_caller(args["_caller"], key=world.key, request_id=REQUEST_ID)
    assert (caller.kind, caller.roles, caller.scope) == ("user", ["Viewer"], ["acme-*"])


def test_a_client_supplied_caller_is_replaced(world):
    forged = {"kind": "service", "roles": ["Admin"], "scope": None, "writeTools": True}
    event = _event(
        _call("IDPTools___status", {"batch_id": "b", "_caller": forged}),
        user_token(world, "scoped", "Viewer"),
    )
    args = forwarded_args(world.mod.handler(event, _context()))
    caller = ma.verify_caller(args["_caller"], key=world.key, request_id=REQUEST_ID)
    assert caller.roles == ["Viewer"] and caller.scope == ["acme-*"]


def test_a_tool_the_role_does_not_admit_is_refused_before_the_target(world):
    event = _event(
        _call("IDPTools___process", {"content": "eA==", "name": "x.pdf"}),
        user_token(world, "open", "Viewer"),
    )
    assert "role" in refusal_text(world.mod.handler(event, _context()))


def test_search_is_refused_to_a_scoped_user(world):
    event = _event(
        _call("IDPTools___search", {"query": "q"}),
        user_token(world, "scoped", "Author"),
    )
    assert "configuration scope" in refusal_text(world.mod.handler(event, _context()))


def test_an_unknown_tool_is_refused(world):
    event = _event(_call("IDPTools___nope"), user_token(world, "open", "Admin"))
    assert "not available" in refusal_text(world.mod.handler(event, _context()))


def test_the_refusal_keeps_the_json_rpc_id(world):
    event = _event(
        _call("IDPTools___process", rid=42), user_token(world, "open", "Viewer")
    )
    body = world.mod.handler(event, _context())["mcp"]["transformedGatewayResponse"][
        "body"
    ]
    assert body["id"] == 42 and body["jsonrpc"] == "2.0"


def test_the_stack_service_client_is_an_author_when_isolation_is_off(world):
    event = _event(
        _call("IDPTools___process", {"content": "eA==", "name": "x.pdf"}),
        service_token(world),
    )
    caller = ma.verify_caller(
        forwarded_args(world.mod.handler(event, _context()))["_caller"],
        key=world.key,
        request_id=REQUEST_ID,
    )
    assert (caller.kind, caller.roles, caller.write_tools) == (
        "service",
        ["Author"],
        True,
    )


def test_the_stack_service_client_is_refused_under_strict(world, monkeypatch):
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    event = _event(_call("IDPTools___status", {"batch_id": "b"}), service_token(world))
    assert "tenant" in refusal_text(world.mod.handler(event, _context()))


def test_a_client_with_no_rule_is_refused(world):
    event = _event(_call("IDPTools___status"), service_token(world, client="other"))
    assert "not permitted" in refusal_text(world.mod.handler(event, _context()))


def test_a_token_from_another_pool_is_refused(world):
    claims = json.loads(
        base64.urlsafe_b64decode(
            user_token(world, "open", "Admin").split(".")[1] + "=="
        )
    )
    claims["iss"] = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other"
    event = _event(_call("IDPTools___status"), _jwt(claims))
    assert "not issued by this deployment" in refusal_text(
        world.mod.handler(event, _context())
    )


def test_no_authorization_header_refuses(world):
    event = _event(_call("IDPTools___status"))
    assert "no access token" in refusal_text(world.mod.handler(event, _context()))


def test_the_header_name_is_matched_case_insensitively(world):
    event = _event(
        _call("IDPTools___status", {"batch_id": "b"}),
        user_token(world, "open", "Viewer"),
        header="authorization",
    )
    assert "_caller" in forwarded_args(world.mod.handler(event, _context()))


def test_no_gateway_request_id_fails_closed(world):
    event = _event(_call("IDPTools___status"), user_token(world, "open", "Viewer"))
    with pytest.raises(Exception):
        world.mod.handler(event, _context(request_id=None))


def test_the_gateway_search_tool_is_forwarded_without_a_caller(world):
    event = _event(
        _call(ma.GATEWAY_SEARCH_TOOL, {"query": "documents"}),
        user_token(world, "open", "Viewer"),
    )
    args = forwarded_args(world.mod.handler(event, _context()))
    assert args == {"query": "documents"}


# --- other methods --------------------------------------------------------------


@pytest.mark.parametrize("method", ["initialize", "ping", "notifications/initialized"])
def test_protocol_methods_pass_through_unchanged(world, method):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    result = world.mod.handler(
        _event(body, user_token(world, "open", "Viewer")), _context()
    )
    assert result["mcp"]["transformedGatewayRequest"]["body"] == body


def test_tools_list_is_refused_to_a_caller_who_cannot_be_resolved(world):
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    event = _event(body, service_token(world, client="other"))
    assert "not permitted" in refusal_text(world.mod.handler(event, _context()))


# --- responses ------------------------------------------------------------------

ALL_TOOLS = [
    {"name": ma.GATEWAY_SEARCH_TOOL},
    {"name": "IDPTools___search"},
    {"name": "IDPTools___status"},
    {"name": "IDPTools___get_results"},
    {"name": "IDPTools___process"},
    {"name": "IDPTools___reprocess"},
]


def _list_response(tools):
    return {
        "statusCode": 200,
        "headers": {},
        "body": {"jsonrpc": "2.0", "id": 3, "result": {"tools": tools}},
        "isStreamingResponse": False,
    }


def test_tools_list_is_filtered_to_the_callers_tools(world):
    request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    event = _event(
        request,
        user_token(world, "open", "Reviewer"),
        response=_list_response(ALL_TOOLS),
    )
    out = world.mod.handler(event, _context())["mcp"]["transformedGatewayResponse"]
    names = {t["name"] for t in out["body"]["result"]["tools"]}
    assert names == {
        ma.GATEWAY_SEARCH_TOOL,
        "IDPTools___status",
        "IDPTools___get_results",
    }
    assert out["statusCode"] == 200


def test_the_gateway_search_result_is_filtered_too(world):
    """Measured: the gateway's semantic search returns the unfiltered tool list as
    JSON text inside the call result, so filtering tools/list alone is not enough."""
    request = _call(ma.GATEWAY_SEARCH_TOOL, {"query": "process"})
    text = json.dumps({"tools": ALL_TOOLS})
    response = {
        "statusCode": 200,
        "headers": {},
        "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"isError": False, "content": [{"type": "text", "text": text}]},
        },
        "isStreamingResponse": False,
    }
    event = _event(request, user_token(world, "open", "Viewer"), response=response)
    out = world.mod.handler(event, _context())["mcp"]["transformedGatewayResponse"]
    names = {
        t["name"]
        for t in json.loads(out["body"]["result"]["content"][0]["text"])["tools"]
    }
    assert names == {
        ma.GATEWAY_SEARCH_TOOL,
        "IDPTools___search",
        "IDPTools___status",
        "IDPTools___get_results",
    }


def test_a_tool_result_passes_through_unchanged(world):
    request = _call("IDPTools___status", {"batch_id": "b"})
    response = {
        "statusCode": 200,
        "headers": {},
        "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "{}"}]},
        },
        "isStreamingResponse": False,
    }
    event = _event(request, user_token(world, "open", "Viewer"), response=response)
    out = world.mod.handler(event, _context())["mcp"]["transformedGatewayResponse"]
    assert out["body"] == response["body"] and out["statusCode"] == 200


def test_a_refused_list_response_returns_no_tools(world):
    request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    event = _event(
        request,
        service_token(world, client="other"),
        response=_list_response(ALL_TOOLS),
    )
    out = world.mod.handler(event, _context())["mcp"]["transformedGatewayResponse"]
    assert "tools" not in json.dumps(out["body"].get("result", {}))


def test_no_token_or_secret_value_is_logged(world, caplog):
    """Every record this function and idp_common emit, at DEBUG. botocore's own
    wire log is excluded: it echoes API responses at DEBUG, which the function's
    LOG_LEVEL never enables for it."""
    caplog.set_level("DEBUG")
    token = user_token(world, "open", "Viewer")
    event = _event(_call("IDPTools___status", {"batch_id": "b"}), token)
    world.mod.handler(event, _context())
    ours = "\n".join(
        r.getMessage()
        for r in caplog.records
        if not r.name.startswith(("botocore", "boto3", "urllib3"))
    )
    assert ours, "nothing was logged, so this test would pass vacuously"
    assert token.split(".")[1] not in ours
    assert "s" * 64 not in ours
