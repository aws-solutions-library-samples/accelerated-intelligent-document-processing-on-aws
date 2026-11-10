# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The RBAC scanner's MCP tool checks (M1-M5) fire on the defect each names.

The live tree only ever exercises the passing side of a check, which passes just as
happily when the check has been deleted. So each test here builds a small
synthetic tree that satisfies every check, applies one defect, and asserts the
specific finding appears — and the baseline test asserts the unmodified tree
produces none, so a finding below cannot come from the fixture being broken.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parents[1]


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "scan_api_rbac_mcp", _SDLC_DIR / "scan_api_rbac.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_api_rbac_mcp"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()

pytestmark = pytest.mark.unit

APP_GROUPS = {"Admin", "Author", "Reviewer", "Viewer", "Annotator"}

POLICY = """
APP_ROLES = ("Viewer", "Reviewer", "Author", "Admin")
DOCUMENT_ROLES = ("Admin", "Author", "Reviewer", "Viewer")
MCP_TOOL_POLICY = {
    "search": ToolPolicy(roles=("Admin", "Author", "Viewer"), write=False,
                         unscoped_only=True, equivalent_op="chat"),
    "get_results": ToolPolicy(roles=DOCUMENT_ROLES, write=False,
                              unscoped_only=False, equivalent_op="getDocument"),
    "process": ToolPolicy(roles=("Admin", "Author"), write=True,
                          unscoped_only=False, equivalent_op="upload"),
}
"""

REGISTRY = """
TOOLS = {
    "search": ("search", "SearchTool"),
    "get_results": ("batch_results", "GetResultsTool"),
    "process": ("batch_run", "BatchRunTool"),
}
"""

SCHEMA = """
IDP_TOOLS_SCHEMA = [
    {"name": "search", "inputSchema": {}},
    {"name": "get_results", "inputSchema": {}},
    {"name": "process", "inputSchema": {}},
]
"""

HANDLER = "def lambda_handler(e, c):\n    c2 = verify_caller(e)\n    authorize_tool(c2, 't')\n"
INTERCEPTOR = (
    "def handler(e, c):\n"
    "    x = mcp_access.resolve_caller(e)\n"
    "    mcp_access.authorize_tool(x, 't')\n"
    "    mcp_access.filter_tool_list(x, [])\n"
    "    mcp_access.sign_caller(x)\n"
)
RESULTS_TOOL = (
    "def run(c):\n"
    "    mcp_access.require_document_keys_in_scope(c, [])\n"
    "    mcp_access.document_keys_in_scope(c, [])\n"
)
PROCESS_TOOL = "def run(c):\n    mcp_access.require_profile_in_scope(c, 'p')\n"

TEMPLATE = """Resources:
  AgentCoreGateway:
    Type: Custom::AgentCoreGateway
    Properties:
      ClientId: !Ref ExternalAppClient
      ConnectorClientId: !Ref MCPConnectorClient
  McpIdentityInterceptorFunction:
    Type: AWS::Serverless::Function
    Properties:
      Environment:
        Variables:
          MCP_USER_CLIENT_IDS: !Ref ExternalAppClient
          MCP_STACK_SERVICE_CLIENT_IDS: !Ref MCPConnectorClient
  Other:
    Type: AWS::SNS::Topic
"""


def spec():
    return {
        "mcp_clients": {"ExternalAppClient": "user", "MCPConnectorClient": "service"},
        "mcp_tools": {
            "search": {
                "groups": ["Admin", "Author", "Viewer"],
                "write": False,
                "unscoped_only": True,
                "equivalent_op": "chat",
                "enforced_in": "tools/search.py",
            },
            "get_results": {
                "groups": ["Admin", "Author", "Reviewer", "Viewer"],
                "write": False,
                "unscoped_only": False,
                "scope_checked": True,
                "scope_filtered": True,
                "equivalent_op": "getDocument",
                "enforced_in": "tools/results.py",
            },
            "process": {
                "groups": ["Admin", "Author"],
                "write": True,
                "unscoped_only": False,
                "scope_checked": True,
                "equivalent_op": "upload",
                "enforced_in": "tools/process.py",
            },
        },
    }


OPS = {
    "chat": {"groups": ["Admin", "Author", "Viewer"]},
    "getDocument": {"groups": "ANY_GROUP"},
    "upload": {"groups": ["Admin", "Author"]},
}


def build(tmp_path: Path, **overrides) -> Path:
    files = {
        scanner.MCP_ACCESS_MODULE: POLICY,
        scanner.MCP_TOOL_REGISTRY: REGISTRY,
        scanner.MCP_GATEWAY_MANAGER: SCHEMA,
        scanner.MCP_HANDLER: HANDLER,
        scanner.MCP_INTERCEPTOR: INTERCEPTOR,
        "tools/search.py": "def run():\n    pass\n",
        "tools/results.py": RESULTS_TOOL,
        "tools/process.py": PROCESS_TOOL,
        "template.yaml": TEMPLATE,
    }
    files.update(overrides)
    for rel, text in files.items():
        if text is None:
            continue
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def run(tmp_path, spec_value=None, ops=None, **overrides):
    repo = build(tmp_path, **overrides)
    return scanner.mcp_checks(repo, spec_value or spec(), ops or OPS, APP_GROUPS)


def codes(findings):
    return sorted({(f.check, f.op) for f in findings})


def test_the_fixture_tree_is_clean(tmp_path):
    assert run(tmp_path) == []


def test_m1_a_tool_missing_from_the_gateway_schema(tmp_path):
    schema = SCHEMA.replace('    {"name": "process", "inputSchema": {}},\n', "")
    got = run(tmp_path, **{scanner.MCP_GATEWAY_MANAGER: schema})
    assert ("M1", "process") in codes(got)


def test_m1_a_registered_tool_with_no_policy(tmp_path):
    registry = REGISTRY.replace("}\n", '    "delete": ("d", "D"),\n}\n')
    got = run(tmp_path, **{scanner.MCP_TOOL_REGISTRY: registry})
    assert ("M1", "delete") in codes(got)


def test_m1_an_expectations_entry_with_no_tool(tmp_path):
    s = spec()
    s["mcp_tools"]["status"] = dict(s["mcp_tools"]["search"])
    assert ("M1", "status") in codes(run(tmp_path, spec_value=s))


def test_m1_a_policy_entry_the_reader_cannot_parse_is_a_failure(tmp_path):
    policy = POLICY.replace("write=True,", "write=WRITE_FLAG,")
    got = run(tmp_path, **{scanner.MCP_ACCESS_MODULE: policy})
    assert any(f.check == "M1" and "could not read" in f.message for f in got)


def test_m1_no_mcp_tools_section(tmp_path):
    got = run(tmp_path, spec_value={"mcp_clients": spec()["mcp_clients"]})
    assert [f.check for f in got] == ["M1"]


def test_m2_policy_roles_differ_from_the_expectations(tmp_path):
    policy = POLICY.replace(
        'roles=("Admin", "Author"), write=True',
        'roles=("Admin", "Author", "Viewer"), write=True',
    )
    got = run(tmp_path, **{scanner.MCP_ACCESS_MODULE: policy})
    assert ("M2", "process") in codes(got)


@pytest.mark.parametrize("flag", ["write", "unscoped_only"])
def test_m2_a_flag_differs(tmp_path, flag):
    s = spec()
    s["mcp_tools"]["search"][flag] = not s["mcp_tools"]["search"][flag]
    assert ("M2", "search") in codes(run(tmp_path, spec_value=s))


def test_m3_mcp_admits_a_role_the_rest_operation_refuses(tmp_path):
    ops = dict(OPS, upload={"groups": ["Admin"]})
    assert ("M3", "process") in codes(run(tmp_path, ops=ops))


def test_m3_an_equivalent_operation_that_does_not_exist(tmp_path):
    ops = {k: v for k, v in OPS.items() if k != "chat"}
    assert ("M3", "search") in codes(run(tmp_path, ops=ops))


def test_m4_a_scope_filtered_tool_that_does_not_filter(tmp_path):
    results = "def run(c):\n    mcp_access.require_document_keys_in_scope(c, [])\n"
    got = run(tmp_path, **{"tools/results.py": results})
    assert ("M4", "get_results") in codes(got)


def test_m4_a_mention_in_a_comment_does_not_count(tmp_path):
    process = "# mcp_access.require_profile_in_scope(c, 'p')\ndef run(c):\n    pass\n"
    got = run(tmp_path, **{"tools/process.py": process})
    assert ("M4", "process") in codes(got)


def test_m4_a_mention_in_a_string_does_not_count(tmp_path):
    process = "def run(c):\n    log('require_profile_in_scope was skipped')\n"
    got = run(tmp_path, **{"tools/process.py": process})
    assert ("M4", "process") in codes(got)


@pytest.mark.parametrize("name", scanner.MCP_HANDLER_CALLS)
def test_m4_the_handler_skips_a_shared_check(tmp_path, name):
    handler = HANDLER.replace(f"{name}(", "something_else(")
    got = run(tmp_path, **{scanner.MCP_HANDLER: handler})
    assert any(f.check == "M4" and name in f.message for f in got)


@pytest.mark.parametrize("name", scanner.MCP_INTERCEPTOR_CALLS)
def test_m4_the_interceptor_skips_a_shared_check(tmp_path, name):
    interceptor = INTERCEPTOR.replace(f".{name}(", ".something_else(")
    got = run(tmp_path, **{scanner.MCP_INTERCEPTOR: interceptor})
    assert any(f.check == "M4" and name in f.message for f in got)


def test_m4_a_missing_interceptor_is_a_failure(tmp_path):
    got = run(tmp_path, **{scanner.MCP_INTERCEPTOR: None})
    assert any(f.check == "M4" and "cannot be read" in f.message for f in got)


def test_m5_a_client_admitted_without_a_resolution_rule(tmp_path):
    template = TEMPLATE.replace(
        "      ConnectorClientId: !Ref MCPConnectorClient\n",
        "      ConnectorClientId: !Ref MCPConnectorClient\n"
        "      ExtraClientIds:\n        - !Ref SomeOtherClient\n",
    )
    s = spec()
    original = scanner.MCP_GATEWAY_CLIENT_PROPERTIES
    try:
        scanner.MCP_GATEWAY_CLIENT_PROPERTIES = original + ("ExtraClientIds",)
        got = run(tmp_path, spec_value=s, **{"template.yaml": template})
    finally:
        scanner.MCP_GATEWAY_CLIENT_PROPERTIES = original
    assert any(f.check == "M5" and "SomeOtherClient" in f.message for f in got)


def test_m5_the_interceptor_is_not_told_about_a_client(tmp_path):
    template = TEMPLATE.replace(
        "          MCP_STACK_SERVICE_CLIENT_IDS: !Ref MCPConnectorClient\n", ""
    )
    got = run(tmp_path, **{"template.yaml": template})
    assert any(
        f.check == "M5" and "MCP_STACK_SERVICE_CLIENT_IDS" in f.message for f in got
    )


def test_m5_a_client_declared_with_the_wrong_kind(tmp_path):
    s = spec()
    s["mcp_clients"]["MCPConnectorClient"] = "user"
    got = run(tmp_path, spec_value=s)
    assert any(f.check == "M5" for f in got)


def test_m5_an_unknown_kind(tmp_path):
    s = spec()
    s["mcp_clients"]["MCPConnectorClient"] = "robot"
    got = run(tmp_path, spec_value=s)
    assert any(f.check == "M5" and "robot" in f.message for f in got)


def test_the_live_policy_table_is_readable():
    """The reader understands the real module, so M1's 'could not read' cannot be
    the steady state of the live tree."""
    text = (scanner.REPO / scanner.MCP_ACCESS_MODULE).read_text()
    table = scanner.mcp_tool_policy_from_source(text)
    assert table and set(table) == {
        "search",
        "status",
        "get_results",
        "process",
        "reprocess",
    }
