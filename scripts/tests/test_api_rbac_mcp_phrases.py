# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live MCP cells recognise refusals by the words ``idp_common.mcp_access`` uses.

``scripts/api_rbac_mcp.py`` reads a refusal by its wording, so a reworded refusal
would turn every "refused" cell into "NOT refused" (or, worse, an admitted call
failing for another reason into a pass). Each phrase is produced here by the real
functions, so a rewording fails this offline test rather than a live run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_rbac_mcp  # noqa: E402

from idp_common import mcp_access as ma  # noqa: E402

pytestmark = pytest.mark.unit


def caller(roles, scope=None, write=True):
    return ma.Caller("user", "u", "u", list(roles), scope, None, write)


def refusal(fn):
    try:
        fn()
    except PermissionError as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


@pytest.mark.parametrize(
    "make",
    [
        lambda: ma.authorize_tool(caller(["Viewer"]), "process"),
        lambda: ma.authorize_tool(caller(["Author"], write=False), "process"),
        lambda: ma.authorize_tool(caller(["Author"], scope=["x"]), "search"),
    ],
    ids=["role", "write", "scope-only"],
)
def test_every_role_refusal_is_recognised(make):
    assert api_rbac_mcp.role_refused(refusal(make))


def test_the_no_role_refusal_is_recognised():
    text = refusal(
        lambda: ma.resolve_user_caller(
            {"username": "u", "sub": "s", "cognito:groups": ["Okta"]},
            cognito=type(
                "C",
                (),
                {
                    "admin_get_user": lambda self, **k: {
                        "UserAttributes": [{"Name": "sub", "Value": "s"}]
                    }
                },
            )(),
            user_pool_id="p",
            users_table_name="t",
            dynamodb=None,
        )
    )
    assert api_rbac_mcp.role_refused(text)


def test_the_scope_refusal_is_recognised():
    from idp_common.document_scope import ABSENT

    text = refusal(
        lambda: ma.require_document_keys_in_scope(
            caller(["Viewer"], scope=["x"]), ["k"], config_version_of=lambda _k: ABSENT
        )
    )
    assert api_rbac_mcp.SCOPE_REFUSAL in text


def test_an_ordinary_failure_is_not_a_role_refusal():
    assert not api_rbac_mcp.role_refused("Failed to retrieve results: Batch not found")


def test_a_served_result_is_read_through_the_nested_body():
    """Measured shape: the Lambda response is text, and its body is JSON text."""
    text = json.dumps({"statusCode": 200, "body": json.dumps({"success": True})})
    assert api_rbac_mcp.tool_body(text) == {"success": True}
    assert api_rbac_mcp.tool_body("not json") == {}


@pytest.mark.parametrize(
    "body",
    [
        '{"clientId": "c1", "clientSecret": "s"}',
        '{"data": {"createMcpCredential": {"clientId": "c1", "clientSecret": "s"}}}',
    ],
    ids=["rest", "graphql-shaped"],
)
def test_the_credential_cells_read_the_operation_result(body):
    """``/op/<field>`` returns the resolver's result itself; reading only a
    ``data`` wrapper found no client id, so the cell reported a failed create and
    never revoked the credential it had made."""
    got = api_rbac_mcp.operation_result(body, "createMcpCredential")
    assert got["clientId"] == "c1"


@pytest.mark.parametrize("body", ["", "not json", "[1, 2]"])
def test_an_unreadable_operation_result_is_empty(body):
    assert api_rbac_mcp.operation_result(body, "createMcpCredential") == {}
