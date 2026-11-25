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
  the SDK. The reprocess tool does not go through the SDK: it invokes the stack's
  reprocess resolver for the caller, and the Lambda client is a fake recording
  each invoke.
"""

import importlib
import io
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
            awaiting_review=0,
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
    # By module name rather than `import tools.base`: `tools` is the handler's own
    # directory, importable only through the `_path` fixture's `sys.path` insert, so
    # a static import names a module the type gate cannot resolve.
    base = importlib.import_module("tools.base")

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


# --- the reprocess tool ----------------------------------------------------------

REPROCESS_ARN = "arn:aws:lambda:us-east-1:111122223333:function:idp-ReprocessResolver"
AUTHOR = caller(roles=("Author",))
SCOPED_AUTHOR = caller(roles=("Author",), scope=["acme-*"])


class FakeResolver:
    """The reprocess resolver behind ``Invoke``: records each event, queues each
    document, or answers as scripted."""

    def __init__(self):
        self.events = []
        self.function_names = []
        self.errors = {}
        self.refuse = {}

    def invoke(self, FunctionName, InvocationType, Payload):
        assert InvocationType == "RequestResponse"
        self.function_names.append(FunctionName)
        event = json.loads(Payload)
        self.events.append(event)
        error = self.errors.get(len(self.events))
        if error is not None:
            body = {"errorType": error[0], "errorMessage": error[1]}
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(body).encode()),
            }
        results = []
        for key in event["arguments"]["objectKeys"]:
            if key in self.refuse:
                results.append(
                    {
                        "objectKey": key,
                        "status": "REFUSED",
                        "startStep": None,
                        "reason": self.refuse[key],
                        "message": f"{key} cannot be processed again",
                    }
                )
            else:
                results.append(
                    {
                        "objectKey": key,
                        "status": "QUEUED",
                        "startStep": "extraction",
                        "reason": None,
                        "message": None,
                    }
                )
        queued = sum(1 for result in results if result["status"] == "QUEUED")
        body = {"queued": queued, "results": results}
        return {"Payload": io.BytesIO(json.dumps(body).encode())}

    @property
    def keys(self):
        return [
            key for event in self.events for key in event["arguments"]["objectKeys"]
        ]


@pytest.fixture
def resolver(monkeypatch):
    module = tool_module("batch_reprocess")
    fake = FakeResolver()
    monkeypatch.setattr(module, "_lambda", lambda: fake)
    monkeypatch.setenv("REPROCESS_FUNCTION_ARN", REPROCESS_ARN)
    return fake


def reprocess_tool():
    return tool_module("batch_reprocess").BatchReprocessTool()


def test_reprocess_invokes_the_resolver_for_the_caller_with_no_identity(
    sdk, lookup, resolver
):
    out = reprocess_tool().execute(
        step="extraction", document_ids="a.pdf, b.pdf", caller=SCOPED_AUTHOR
    )

    assert out["success"] is True
    assert resolver.function_names == [REPROCESS_ARN]
    [event] = resolver.events
    assert "identity" not in event
    assert event["info"] == {"fieldName": "reprocessDocument"}
    assert event["arguments"] == {
        "objectKeys": ["a.pdf", "b.pdf"],
        "startStep": "extraction",
    }
    assert event["actingCaller"] == SCOPED_AUTHOR.to_payload()
    assert event["actingCaller"]["roles"] == ["Author"]
    assert event["actingCaller"]["scope"] == ["acme-*"]


def test_reprocess_returns_each_documents_result(sdk, lookup, resolver):
    resolver.refuse = {"b.pdf": "MALWARE_GATE"}

    out = reprocess_tool().execute(
        step="extraction", document_ids="a.pdf,b.pdf", caller=AUTHOR
    )

    assert out["success"] is True
    assert (out["documents_queued"], out["documents_failed"]) == (1, 1)
    assert out["summary"] == {"queued": 1, "failed": 1}
    assert out["results"] == [
        {
            "document_id": "a.pdf",
            "status": "QUEUED",
            "start_step": "extraction",
            "reason": None,
            "message": None,
        },
        {
            "document_id": "b.pdf",
            "status": "REFUSED",
            "start_step": None,
            "reason": "MALWARE_GATE",
            "message": "b.pdf cannot be processed again",
        },
    ]
    assert "1 were not queued" in out["message"]


@pytest.mark.parametrize(
    "skip", [["summarization", "evaluation"], "summarization, evaluation"]
)
def test_reprocess_passes_the_steps_to_skip(sdk, lookup, resolver, skip):
    reprocess_tool().execute(
        step="resume", document_ids="a.pdf", skip=skip, caller=AUTHOR
    )

    assert resolver.events[0]["arguments"] == {
        "objectKeys": ["a.pdf"],
        "startStep": "resume",
        "skipSteps": ["summarization", "evaluation"],
    }


@pytest.mark.parametrize(
    "who", [caller(), caller(roles=("Reviewer",)), caller(roles=())]
)
def test_reprocess_refuses_a_caller_with_no_reprocess_role_before_any_invoke(
    sdk, lookup, resolver, who
):
    out = reprocess_tool().execute(step="extraction", document_ids="a.pdf", caller=who)

    assert out["success"] is False and "does not permit" in out["error"]
    assert resolver.events == [] and sdk.calls == []


def test_reprocess_refuses_a_caller_whose_write_tools_are_off(sdk, lookup, resolver):
    out = reprocess_tool().execute(
        step="extraction",
        document_ids="a.pdf",
        caller=caller(roles=("Author",), write=False),
    )

    assert out["success"] is False and "write tools" in out["error"]
    assert resolver.events == []


@pytest.mark.parametrize("step", ["nonsense", "", "apply_review_changes", "OCR"])
def test_reprocess_refuses_an_unknown_step_before_any_invoke(
    sdk, lookup, resolver, step
):
    out = reprocess_tool().execute(step=step, document_ids="a.pdf", caller=AUTHOR)

    assert out["success"] is False and "not a step" in out["error"]
    assert resolver.events == [] and sdk.calls == []


@pytest.mark.parametrize("skip", [["extraction"], ["nonsense"], "beginning"])
def test_reprocess_refuses_a_step_that_cannot_be_skipped(sdk, lookup, resolver, skip):
    out = reprocess_tool().execute(
        step="extraction", document_ids="a.pdf", skip=skip, caller=AUTHOR
    )

    assert out["success"] is False and "cannot be skipped" in out["error"]
    assert resolver.events == []


def test_reprocess_refuses_the_whole_request_for_one_document_outside_scope(
    sdk, lookup, resolver
):
    out = reprocess_tool().execute(
        step="extraction", document_ids="a.pdf,g.pdf", caller=SCOPED_AUTHOR
    )
    assert out["success"] is False and "Unauthorized" in out["error"]
    assert resolver.events == []


def test_reprocess_of_a_batch_runs_exactly_the_checked_documents(sdk, lookup, resolver):
    out = reprocess_tool().execute(
        step="extraction", batch_id="x", caller=caller(roles=("Author",), scope=["*"])
    )
    assert out["success"] is True and out["batch_id"] == "x"
    assert resolver.keys == ["a.pdf", "g.pdf", "b.pdf"]
    assert ("get_document_ids", "x") in sdk.calls


def test_reprocess_of_a_batch_with_a_document_outside_scope_is_refused(
    sdk, lookup, resolver
):
    out = reprocess_tool().execute(
        step="extraction", batch_id="x", caller=SCOPED_AUTHOR
    )
    assert out["success"] is False
    assert resolver.events == []


def test_reprocess_of_a_batch_for_an_unscoped_caller_expands_the_batch(
    sdk, lookup, resolver
):
    """The resolver takes documents, so the batch is expanded for every caller."""
    out = reprocess_tool().execute(step="extraction", batch_id="x", caller=AUTHOR)

    assert out["documents_queued"] == 3
    assert resolver.keys == ["a.pdf", "g.pdf", "b.pdf"]


@pytest.mark.parametrize(
    "arguments",
    [{}, {"document_ids": "a.pdf", "batch_id": "x"}, {"document_ids": " , "}],
)
def test_reprocess_needs_documents_or_a_batch_and_not_both(
    sdk, lookup, resolver, arguments
):
    out = reprocess_tool().execute(step="extraction", caller=AUTHOR, **arguments)

    assert out["success"] is False
    assert resolver.events == []


@pytest.mark.parametrize(
    "error", [("PermissionError", "Access denied: out of scope"), ("KeyError", "'x'")]
)
def test_reprocess_reports_a_request_the_resolver_refuses_or_fails_on(
    sdk, lookup, resolver, error
):
    resolver.errors = {1: error}

    out = reprocess_tool().execute(
        step="extraction", document_ids="a.pdf", caller=AUTHOR
    )

    assert out == {"success": False, "error": error[1], "step": "extraction"}


def test_reprocess_sends_many_documents_in_bounded_requests_each_for_the_caller(
    sdk, lookup, resolver, monkeypatch
):
    from idp_common.run_plan.resolver_requests import DOCUMENTS_PER_REQUEST

    keys = [f"d{n}.pdf" for n in range(DOCUMENTS_PER_REQUEST + 2)]

    out = reprocess_tool().execute(
        step="extraction", document_ids=",".join(keys), caller=AUTHOR
    )

    assert out["documents_queued"] == len(keys)
    assert [len(e["arguments"]["objectKeys"]) for e in resolver.events] == [
        DOCUMENTS_PER_REQUEST,
        2,
    ]
    assert all(e["actingCaller"] == AUTHOR.to_payload() for e in resolver.events)
    assert all("identity" not in e for e in resolver.events)


def test_reprocess_reads_no_sdk_reprocess():
    """The tool holds no copy of the reprocess and reaches none through the SDK."""
    source = open(os.path.join(HANDLER_DIR, "tools", "batch_reprocess.py")).read()

    assert "batch.reprocess" not in source and "batch.rerun" not in source
    assert "REPROCESS_FUNCTION_ARN" in source


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
            def __init__(self, table_name, region=None):
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
            def __init__(self, table_name, region=None):
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


def test_a_document_derived_result_leaves_the_handler_with_its_envelope(handler):
    index, tool = handler
    tool.execute.return_value = {"success": True, "documents": [{"fields": {}}]}
    block = ma.sign_caller(caller(), key=KEY, request_id=REQUEST)
    out = index.lambda_handler(
        {"document_id": "a.pdf", "_caller": block}, context("get_results")
    )
    body = json.loads(out["body"])
    assert body["provenance"]["documentDerived"] == ["documents"]
    assert body["documents"] == [{"fields": {}}]


def test_scoped_status_counts_documents_awaiting_review_as_settled(
    sdk, lookup, monkeypatch
):
    """A held document settles a scoped caller's batch, as it does an unscoped one.

    The scoped branch restates the SDK's `all_complete` over the documents in
    scope, so it has to know the awaiting-review bucket too; reading only
    completed + failed left an agent polling a batch whose one in-scope document
    sat in review, with no bound (#2022).
    """

    def get_status(batch_id):
        def one(document_id, status, bucket):
            return SimpleNamespace(
                document_id=document_id,
                status=SimpleNamespace(value=status),
                duration_seconds=1,
                review_status=None,
                bucket=SimpleNamespace(value=bucket),
            )

        docs = [
            one("a.pdf", "COMPLETED", "completed"),
            one("b.pdf", "HITL_IN_PROGRESS", "awaiting_review"),
            one("g.pdf", "RUNNING", "running"),
        ]
        return SimpleNamespace(
            documents=docs,
            total=3,
            completed=1,
            in_progress=1,
            failed=0,
            queued=0,
            awaiting_review=1,
            all_complete=False,
        )

    monkeypatch.setattr(sdk, "get_status", get_status)
    tool = tool_module("batch_status").BatchStatusTool()

    out = tool.execute(batch_id="x", caller=SCOPED)

    # g.pdf, the running one, is out of scope; the in-scope pair has settled.
    assert out["status"]["total"] == 2
    assert out["status"]["awaiting_review"] == 1
    assert out["all_complete"] is True
    # A person is the blocker, and the response says so in words.
    assert "1 document(s) awaiting human review" in out["message"]


def test_status_without_documents_in_review_carries_no_review_message(sdk, lookup):
    tool = tool_module("batch_status").BatchStatusTool()
    out = tool.execute(batch_id="x", caller=caller())
    assert out["status"]["awaiting_review"] == 0
    assert "message" not in out
