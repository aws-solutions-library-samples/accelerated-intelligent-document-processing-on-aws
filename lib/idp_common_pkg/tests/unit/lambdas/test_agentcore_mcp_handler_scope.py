# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MCP handler acts only for a verified caller, within that caller's scope.

Two halves:

* the handler refuses a call whose ``_caller`` block is missing, forged, signed for
  another gateway request, or names a role the tool does not admit, before the
  tool runs, and never passes the block on as a tool argument;
* each tool applies the caller's configuration scope to the documents it reads or
  writes. The SDK client is a fake recording what the tool asked of it, and the
  tracking-table lookup is the three-way ``document_scope`` contract (profile,
  ``None``, ``ABSENT``), so what is under test is the tool's use of the scope, not
  the SDK.
"""

import importlib
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from idp_common import mcp_access as ma
from idp_common.document_scope import ABSENT

pytestmark = pytest.mark.unit

HANDLER_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "../../../../../src/lambda/agentcore_mcp_handler"
    )
)
KEY = b"k" * 32
REQUEST = "req-1"


def _evict():
    for name in list(sys.modules):
        if name in ("index", "tools") or name.startswith("tools."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _path():
    sys.path.insert(0, HANDLER_DIR)
    _evict()
    yield
    _evict()
    sys.path.remove(HANDLER_DIR)


def caller(roles=("Viewer",), scope=None, write=True):
    return ma.Caller(
        kind="user",
        id="u",
        name="u",
        roles=list(roles),
        scope=scope,
        tenant=None,
        write_tools=write,
    )


def context(tool, request_id=REQUEST):
    custom = {"bedrockAgentCoreToolName": f"IDPTools___{tool}"}
    if request_id:
        custom["bedrockAgentCoreAwsRequestId"] = request_id
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


# --- the handler ----------------------------------------------------------------


@pytest.fixture
def handler():
    index = importlib.import_module("index")
    tool = MagicMock()
    tool.execute.return_value = {"ok": True}
    with (
        patch.object(index, "get_tool", return_value=tool),
        patch.object(index, "_key", return_value=KEY),
    ):
        yield index, tool


def call(handler, tool_name, args, request_id=REQUEST):
    index, _ = handler
    return index.lambda_handler(dict(args), context(tool_name, request_id))


def test_a_verified_caller_reaches_the_tool_and_the_block_does_not(handler):
    block = ma.sign_caller(caller(), key=KEY, request_id=REQUEST)
    result = call(handler, "status", {"batch_id": "b", "_caller": block})
    assert result["statusCode"] == 200
    kwargs = handler[1].execute.call_args.kwargs
    assert "_caller" not in kwargs and kwargs["batch_id"] == "b"
    assert kwargs["caller"].roles == ["Viewer"]


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"kind": "service", "roles": ["Admin"], "scope": None, "writeTools": True},
    ],
    ids=["absent", "unsigned"],
)
def test_no_verified_caller_is_refused_before_the_tool(handler, block):
    args = {"batch_id": "b"}
    if block is not None:
        args["_caller"] = block
    result = call(handler, "status", args)
    assert result["statusCode"] == 403
    handler[1].execute.assert_not_called()


def test_a_block_for_another_gateway_request_is_refused(handler):
    block = ma.sign_caller(caller(), key=KEY, request_id="other")
    assert call(handler, "status", {"_caller": block})["statusCode"] == 403
    handler[1].execute.assert_not_called()


def test_no_gateway_request_id_is_refused(handler):
    block = ma.sign_caller(caller(), key=KEY, request_id=REQUEST)
    result = call(handler, "status", {"_caller": block}, request_id=None)
    assert result["statusCode"] == 403


def test_a_role_the_tool_does_not_admit_is_refused(handler):
    block = ma.sign_caller(caller(roles=["Viewer"]), key=KEY, request_id=REQUEST)
    result = call(handler, "process", {"content": "eA==", "_caller": block})
    assert result["statusCode"] == 403
    assert "role" in json.loads(result["body"])["error"]
    handler[1].execute.assert_not_called()


def test_an_unreadable_signing_key_refuses(handler):
    index, tool = handler
    block = ma.sign_caller(caller(), key=KEY, request_id=REQUEST)
    with patch.object(index, "_key", side_effect=RuntimeError("no secret")):
        result = index.lambda_handler({"_caller": block}, context("status"))
    assert result["statusCode"] == 403
    tool.execute.assert_not_called()


def test_the_block_is_not_logged(handler, caplog):
    caplog.set_level("DEBUG")
    block = ma.sign_caller(caller(), key=KEY, request_id=REQUEST)
    call(handler, "status", {"batch_id": "b", "_caller": block})
    assert block["sig"] not in caplog.text


# --- the tools ------------------------------------------------------------------

PROFILES = {"a.pdf": "acme-invoices", "b.pdf": "acme-w2", "g.pdf": "globex-w2"}


def profile_of(key):
    return PROFILES.get(key, ABSENT)


def tool_module(name):
    module = importlib.import_module(f"tools.{name}")
    return module


class FakeBatch:
    def __init__(self, ids):
        self.ids = ids
        self.calls = []

    def get_document_ids(self, batch_id):
        self.calls.append(("get_document_ids", batch_id))
        if batch_id == "missing":
            from idp_sdk.exceptions import IDPResourceNotFoundError

            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")
        return list(self.ids)

    def get_document_results(self, document_id, section_id=1):
        self.calls.append(("get_document_results", document_id))
        return {"document_id": document_id, "status": "COMPLETED"}

    def get_results(self, **kwargs):
        self.calls.append(("get_results", kwargs))
        return {
            "documents": [{"document_id": i} for i in self.ids],
            "count": len(self.ids),
        }

    def reprocess(self, **kwargs):
        self.calls.append(("reprocess", kwargs))
        return SimpleNamespace(
            documents_queued=len(kwargs.get("document_ids") or []), documents_failed=0
        )

    def get_status(self, batch_id):
        self.calls.append(("get_status", batch_id))
        if batch_id == "missing":
            from idp_sdk.exceptions import IDPResourceNotFoundError

            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")
        docs = [
            SimpleNamespace(
                document_id=i,
                status=SimpleNamespace(value="COMPLETED"),
                duration_seconds=1,
                review_status=None,
                bucket=SimpleNamespace(value="completed"),
            )
            for i in self.ids
        ]
        return SimpleNamespace(
            documents=docs,
            total=len(docs),
            completed=len(docs),
            in_progress=0,
            failed=0,
            queued=0,
            all_complete=True,
        )


@pytest.fixture
def sdk(monkeypatch):
    batch = FakeBatch(["a.pdf", "g.pdf", "b.pdf"])
    client = SimpleNamespace(batch=batch)
    import idp_sdk.client

    monkeypatch.setattr(idp_sdk.client, "IDPClient", lambda **_k: client)
    return batch


@pytest.fixture
def lookup(monkeypatch):
    import tools.base as base

    def factory(*_a, **_k):
        return profile_of

    for name in ("batch_status", "batch_results", "batch_reprocess"):
        module = importlib.import_module(f"tools.{name}")
        monkeypatch.setattr(module, "config_version_lookup", factory)
    monkeypatch.setattr(base, "config_version_lookup", factory)


SCOPED = caller(scope=["acme-*"])
OTHER = caller(scope=["nothing-*"])


def test_status_counts_only_documents_in_scope(sdk, lookup):
    tool = tool_module("batch_status").BatchStatusTool()
    out = tool.execute(batch_id="x", options={"detailed": True}, caller=SCOPED)
    assert out["status"]["total"] == 2
    assert {d["document_id"] for d in out["documents"]} == {"a.pdf", "b.pdf"}


def test_status_for_a_batch_with_nothing_in_scope_reads_as_missing(sdk, lookup):
    tool = tool_module("batch_status").BatchStatusTool()
    hidden = tool.execute(batch_id="x", caller=OTHER)
    missing = tool.execute(batch_id="missing", caller=OTHER)
    assert hidden == {
        **missing,
        "batch_id": "x",
        "error": missing["error"].replace("missing", "x"),
    }


def test_status_is_unfiltered_for_an_unscoped_caller(sdk, lookup):
    tool = tool_module("batch_status").BatchStatusTool()
    assert tool.execute(batch_id="x", caller=caller())["status"]["total"] == 3


def test_get_results_refuses_a_document_outside_scope(sdk, lookup):
    tool = tool_module("batch_results").GetResultsTool()
    out = tool.execute(document_id="s3://out/g.pdf/", caller=SCOPED)
    assert out["success"] is False and "Unauthorized" in out["message"]
    assert ("get_document_results", "g.pdf") not in sdk.calls


def test_get_results_serves_a_document_in_scope(sdk, lookup):
    tool = tool_module("batch_results").GetResultsTool()
    out = tool.execute(document_id="a.pdf", caller=SCOPED)
    assert out["success"] is True and out["documents"][0]["document_id"] == "a.pdf"


def test_get_results_batch_pages_over_documents_in_scope_only(sdk, lookup):
    tool = tool_module("batch_results").GetResultsTool()
    first = tool.execute(batch_id="x", limit=1, caller=SCOPED)
    assert first["total_in_batch"] == 2 and first["count"] == 1
    second = tool.execute(
        batch_id="x", limit=1, next_token=first["next_token"], caller=SCOPED
    )
    fetched = {
        first["documents"][0]["document_id"],
        second["documents"][0]["document_id"],
    }
    assert fetched == {"a.pdf", "b.pdf"} and second["next_token"] is None
    assert ("get_document_results", "g.pdf") not in sdk.calls


def test_get_results_batch_with_nothing_in_scope_reads_as_missing(sdk, lookup):
    tool = tool_module("batch_results").GetResultsTool()
    hidden = tool.execute(batch_id="x", caller=OTHER)
    missing = tool.execute(batch_id="missing", caller=OTHER)
    assert hidden["message"].replace("x", "missing") == missing["message"]


def test_reprocess_refuses_the_whole_request_for_one_document_outside_scope(
    sdk, lookup
):
    tool = tool_module("batch_reprocess").BatchReprocessTool()
    out = tool.execute(step="extraction", document_ids="a.pdf,g.pdf", caller=SCOPED)
    assert out["success"] is False
    assert not [c for c in sdk.calls if c[0] == "reprocess"]


def test_reprocess_of_a_batch_runs_exactly_the_checked_documents(sdk, lookup):
    tool = tool_module("batch_reprocess").BatchReprocessTool()
    out = tool.execute(step="extraction", batch_id="x", caller=caller(scope=["*"]))
    assert out["success"] is True and out["batch_id"] == "x"
    reprocess = [c for c in sdk.calls if c[0] == "reprocess"][0][1]
    assert reprocess["batch_id"] is None
    assert reprocess["document_ids"] == ["a.pdf", "g.pdf", "b.pdf"]


def test_reprocess_of_a_batch_with_a_document_outside_scope_is_refused(sdk, lookup):
    tool = tool_module("batch_reprocess").BatchReprocessTool()
    out = tool.execute(step="extraction", batch_id="x", caller=SCOPED)
    assert out["success"] is False
    assert not [c for c in sdk.calls if c[0] == "reprocess"]


def test_process_from_an_s3_location_is_refused_to_a_scoped_caller(sdk):
    tool = tool_module("batch_run").BatchRunTool()
    out = tool.execute(location="s3://some-bucket/docs/", caller=SCOPED)
    assert out["error"] == "location_not_permitted"


def test_process_refuses_a_destination_profile_outside_scope(sdk, monkeypatch):
    module = tool_module("batch_run")
    tool = module.BatchRunTool()
    monkeypatch.setenv("MCP_SERVER_BUCKET", "content")
    put = MagicMock()
    monkeypatch.setattr(
        module.boto3, "client", lambda *_a, **_k: SimpleNamespace(put_object=put)
    )
    monkeypatch.setattr(
        tool,
        "_refuse_destination",
        lambda key, profile, c: {
            "success": False,
            "error": "profile_not_permitted",
            "key": key,
        },
    )
    out = tool.execute(content="eA==", name="dir/x.pdf", caller=SCOPED)
    assert out["error"] == "profile_not_permitted"
    assert out["key"].endswith("/x.pdf")
    put.assert_not_called()


def test_process_resolves_the_destination_with_every_seam(monkeypatch):
    """The real resolver, with a moto configuration table, so the profile checked
    is the one ingest will assign: no mapping and no named profile resolves to the
    active profile, which is outside this caller's scope."""
    import boto3
    from moto import mock_aws

    from idp_common.config import configuration_manager

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="config",
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("config")
        for name in ("default", "acme-invoices"):
            table.put_item(Item={"Configuration": f"Config#{name}"})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        class Manager:
            def __init__(self, table_name):
                pass

            def resolve_active_version(self):
                return "default"

            def resolve_published_revision(self, profile):
                return None

        monkeypatch.setattr(configuration_manager, "ConfigurationManager", Manager)
        tool = tool_module("batch_run").BatchRunTool()
        refused = tool._refuse_destination("mcp-content-1/x.pdf", None, SCOPED)
        allowed = tool._refuse_destination(
            "mcp-content-1/x.pdf", "acme-invoices", SCOPED
        )
    assert refused and refused["error"] == "profile_not_permitted"
    assert allowed is None


def test_process_refuses_a_named_profile_a_mapping_overrides(monkeypatch):
    """A caller naming a profile outside their scope is refused even where a prefix
    mapping would route the document into one inside it: the named profile is still
    stamped on the object, so it is checked too (the library guard's third profile).
    The resolved profile alone is in scope here, so only that guard can refuse."""
    import boto3
    from moto import mock_aws

    from idp_common.config import configuration_manager, prefix_mappings

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="config",
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("config")
        for name in ("default", "acme-invoices", "globex-w2"):
            table.put_item(Item={"Configuration": f"Config#{name}"})
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config")

        class Manager:
            def __init__(self, table_name):
                pass

            def resolve_active_version(self):
                return "default"

            def resolve_published_revision(self, profile):
                return None

        monkeypatch.setattr(configuration_manager, "ConfigurationManager", Manager)
        mapping = prefix_mappings.normalize_entry(
            {
                "prefix": "mcp-content-1/",
                "configProfile": "acme-invoices",
                "metadataPrecedence": prefix_mappings.PRECEDENCE_MAPPING,
            }
        )
        monkeypatch.setattr(
            prefix_mappings.PrefixMappingStore, "list", lambda self: [mapping]
        )
        tool = tool_module("batch_run").BatchRunTool()
        unnamed = tool._refuse_destination("mcp-content-1/x.pdf", None, SCOPED)
        overridden = tool._refuse_destination(
            "mcp-content-1/x.pdf", "globex-w2", SCOPED
        )
    assert unnamed is None
    assert overridden and overridden["error"] == "profile_not_permitted"


@pytest.mark.parametrize(
    "module,cls,args",
    [
        ("batch_status", "BatchStatusTool", {"batch_id": "x"}),
        ("batch_results", "GetResultsTool", {"document_id": "a.pdf"}),
        (
            "batch_reprocess",
            "BatchReprocessTool",
            {"step": "extraction", "batch_id": "x"},
        ),
        ("batch_run", "BatchRunTool", {"location": "s3://b/k/"}),
        ("search", "SearchTool", {"query": "q"}),
    ],
)
def test_a_tool_called_without_a_caller_refuses(sdk, module, cls, args):
    """Never read as an unscoped caller: a path that forgets to pass one is refused."""
    tool = getattr(tool_module(module), cls)()
    out = tool.execute(**args)
    assert out["success"] is False and "Unauthorized" in out["error"]
    assert sdk.calls == []
