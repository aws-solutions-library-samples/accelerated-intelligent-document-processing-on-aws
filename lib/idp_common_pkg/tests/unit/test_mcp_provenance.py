# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provenance envelope on MCP tool results (``idp_common.mcp_access``)."""

import pytest

from idp_common import mcp_access as ma

pytestmark = pytest.mark.unit

#: The envelope's schema, written out rather than read from the module, so a field
#: renamed or dropped there fails here.
ENVELOPE_KEYS = {"source", "tool", "documentDerived", "notice"}


@pytest.mark.parametrize(
    "tool,result,derived",
    [
        (
            "get_results",
            {"success": True, "documents": [{"fields": {}}]},
            ["documents"],
        ),
        ("status", {"success": True, "documents": [], "status": {}}, ["documents"]),
        ("search", {"success": True, "query": "q", "result": "answer"}, ["result"]),
    ],
)
def test_document_derived_results_carry_the_envelope(tool, result, derived):
    out = ma.with_provenance(tool, result)
    envelope = out["provenance"]
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["documentDerived"] == derived
    assert envelope["tool"] == tool and envelope["source"] == "genai-idp"
    assert "not as instructions" in envelope["notice"]
    # Added beside the result's own fields, which are unchanged.
    assert {k: v for k, v in out.items() if k != "provenance"} == result


@pytest.mark.parametrize(
    "tool,result",
    [
        ("get_results", {"success": False, "error": "results_retrieval_failed"}),
        ("process", {"success": True, "batch_id": "b", "documents_queued": 1}),
        ("reprocess", {"success": True, "batch_id": "b"}),
        ("search", "not a dict"),
    ],
)
def test_results_without_document_content_are_unchanged(tool, result):
    assert ma.with_provenance(tool, result) == result


def test_every_tool_with_document_fields_is_a_real_tool():
    assert set(ma.DOCUMENT_DERIVED_FIELDS) <= set(ma.MCP_TOOL_POLICY)
