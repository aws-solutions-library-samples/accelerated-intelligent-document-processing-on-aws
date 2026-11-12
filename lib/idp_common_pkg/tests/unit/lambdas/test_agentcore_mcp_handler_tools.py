# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the AgentCore MCP handler tools.

Covers the two defects reported by external MCP consumers:
  1. get_results was batch-only — it now also accepts document_id, so a
     consumer holding only a document reference (e.g. from a post-processing
     hook event) can retrieve results without a batch id.
  2. A missing agents dependency (strands) in the Lambda layer used to fail
     the handler module import (Runtime.ImportModuleError), taking down ALL
     tools. Imports are now lazy/guarded: batch tools keep working and the
     search tool returns a structured, diagnosable error.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest

from idp_common.mcp_access import Caller

#: An unscoped Admin: these tests are about the tools' own behaviour, which the
#: caller's scope does not change for an unscoped caller.
ADMIN = Caller("user", "u", "u", ["Admin"], None, None, True)

HANDLER_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../src/lambda/agentcore_mcp_handler",
    )
)

# Modules this handler owns; evicted around each test so imports re-execute
# against this test's sys.path and sys.modules state.
_HANDLER_MODULES = ("index", "tools")


def _evict_handler_modules():
    for name in list(sys.modules):
        if name in _HANDLER_MODULES or name.startswith("tools."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _path_setup():
    sys.path.insert(0, HANDLER_DIR)
    _evict_handler_modules()
    yield
    _evict_handler_modules()
    sys.path.remove(HANDLER_DIR)


def _block_strands(monkeypatch):
    """Make `import strands` raise ImportError and force agents re-import."""
    # None in sys.modules makes any import of the module raise ImportError.
    monkeypatch.setitem(sys.modules, "strands", None)
    for name in list(sys.modules):
        if name.startswith("idp_common.agents"):
            monkeypatch.delitem(sys.modules, name)


@pytest.mark.unit
def test_registry_tools_load_without_strands(monkeypatch):
    """Batch tools must be constructible when the agents stack is broken."""
    _block_strands(monkeypatch)
    tools = importlib.import_module("tools")

    for tool_name in ["get_results", "status", "process", "reprocess"]:
        tool = tools.get_tool(tool_name)
        assert tool is not None, f"{tool_name} should load without strands"


@pytest.mark.unit
def test_handler_module_imports_without_strands(monkeypatch):
    """index.py must import (no Runtime.ImportModuleError) without strands."""
    _block_strands(monkeypatch)
    index = importlib.import_module("index")

    # Handler is functional: routes a tool-less event to a 400, not a crash.
    result = index.lambda_handler({}, object())
    assert result["statusCode"] == 400


@pytest.mark.unit
def test_search_returns_structured_error_without_strands(monkeypatch):
    _block_strands(monkeypatch)
    tools = importlib.import_module("tools")

    result = tools.get_tool("search").execute(query="how many documents?", caller=ADMIN)

    assert result["success"] is False
    assert "dependencies are not installed" in result["error"]


@pytest.mark.unit
def test_get_results_requires_batch_or_document_id():
    tools = importlib.import_module("tools")

    result = tools.get_tool("get_results").execute(caller=ADMIN)

    assert result["success"] is False
    assert result["error"] == "missing_identifier"


def _mock_idp_sdk(monkeypatch):
    """Install a fake idp_sdk.client.IDPClient and return its batch mock."""
    batch = MagicMock()
    client_cls = MagicMock(return_value=MagicMock(batch=batch))
    client_module = MagicMock(IDPClient=client_cls)
    monkeypatch.setitem(sys.modules, "idp_sdk", MagicMock(client=client_module))
    monkeypatch.setitem(sys.modules, "idp_sdk.client", client_module)
    return batch


@pytest.mark.unit
def test_get_results_by_document_id(monkeypatch):
    tools = importlib.import_module("tools")
    batch = _mock_idp_sdk(monkeypatch)
    batch.get_document_results.return_value = {
        "document_id": "Borrowing_Notice_#3.pdf",
        "document_class": "Borrowing Notice",
        "fields": {"amount": "1000.00"},
        "confidence": {"amount": 0.99},
        "page_count": 1,
        "status": "COMPLETED",
    }

    result = tools.get_tool("get_results").execute(
        caller=ADMIN, document_id="s3://output-bucket/Borrowing_Notice_#3.pdf/"
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["documents"][0]["document_id"] == "Borrowing_Notice_#3.pdf"
    batch.get_document_results.assert_called_once_with(
        document_id="s3://output-bucket/Borrowing_Notice_#3.pdf/",
        section_id=1,
    )
    batch.get_results.assert_not_called()


@pytest.mark.unit
def test_get_results_by_batch_id_still_works(monkeypatch):
    tools = importlib.import_module("tools")
    batch = _mock_idp_sdk(monkeypatch)
    batch.get_results.return_value = {
        "batch_id": "mcp-batch-1",
        "section_id": 1,
        "count": 2,
        "total_in_batch": 2,
        "documents": [{"document_id": "a.pdf"}, {"document_id": "b.pdf"}],
    }

    result = tools.get_tool("get_results").execute(batch_id="mcp-batch-1", caller=ADMIN)

    assert result["success"] is True
    assert result["count"] == 2
    batch.get_results.assert_called_once()
    batch.get_document_results.assert_not_called()
