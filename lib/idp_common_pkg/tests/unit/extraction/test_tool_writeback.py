# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the tool-result write-back optimization (perf/agentic-tool-writeback).

The agentic extraction table tools (parse_table, map_table_to_schema) persist
their full parsed/mapped rows in agent STATE and are read from there by the
downstream mapping/finalize tools. They should NOT echo the dense rows back as
the tool-result ``content`` that Strands re-sends to the model on every
subsequent turn — that re-transmission is what ballooned agentic input tokens.

These tests assert the invariant that makes the optimization work:
  1. The tool return has BOTH ``status`` and ``content`` so Strands treats it as
     a pre-formatted ToolResult (skipping the ``str(result)`` inline fallback in
     strands.tools.decorator._wrap_tool_result).
  2. For large tables, the model-visible ``content`` does NOT contain the full
     row set (only head/tail samples + counts), so context growth is bounded.
  3. The full rows remain available in agent state and in the return's
     top-level keys for the pipeline / existing callers.
"""

from __future__ import annotations

import json

import pytest

# The factories' tools are decorated with the REAL strands ``@tool`` and called
# directly: ``DecoratedFunctionTool.__call__`` forwards to the wrapped function.
# Do not replace the decorator here (``strands.tool``, or the name a production
# module bound from it): a replacement made at import outlives this module and
# turns every Strands tool built later in the same process into a plain
# function, which an Agent then cannot register (#1756). The strands-binding
# check in tests/conftest.py fails a module that does it.
DecoratedFunctionTool = pytest.importorskip(
    "strands.tools.decorator",
    reason="strands-agents not installed (a conftest stub does not count)",
).DecoratedFunctionTool

from idp_common.extraction.tools.table_parser import (  # noqa: E402
    INLINE_ROW_CAP,
    create_map_table_to_schema_tool,
    create_parse_table_tool,
)


class _State:
    def __init__(self):
        self._d: dict = {}

    def get(self, k=None):
        return self._d.get(k) if k else self._d

    def set(self, k, v):
        self._d[k] = v


class _Agent:
    def __init__(self):
        self.state = _State()


def _big_markdown_table(n: int) -> str:
    header = "| Date | Description | Amount | Balance |"
    sep = "|---|---|---|---|"
    rows = [
        f"| 2025-01-{(i % 28) + 1:02d} | TXN NUMBER {i:05d} DESCRIPTION | "
        f"${i}.00 | ${1000 + i}.00 |"
        for i in range(n)
    ]
    return "\n".join([header, sep] + rows)


def _content_text_len(result: dict) -> int:
    """Serialized size of the model-visible tool-result content (JSON block)."""
    content = result["content"]
    return len(json.dumps(content))


class TestParseTableWriteback:
    def test_returns_status_and_content(self):
        """Return must have both keys so Strands does not inline str(result)."""
        agent = _Agent()
        tool = create_parse_table_tool()
        result = tool(table_text=_big_markdown_table(10), agent=agent)
        assert "status" in result and "content" in result
        assert result["status"] in ("success", "error")
        # content is a list of ToolResultContent blocks
        assert isinstance(result["content"], list)
        assert result["content"] and "json" in result["content"][0]

    def test_full_rows_kept_in_state_not_in_content(self):
        """Large table: full rows persist in state; content only samples them."""
        n = INLINE_ROW_CAP + 300
        agent = _Agent()
        tool = create_parse_table_tool()
        result = tool(table_text=_big_markdown_table(n), agent=agent)

        # Full rows still available in state for map_table_to_schema.
        stored = agent.state.get("last_parse_table_result")
        assert stored["row_count"] == n
        assert len(stored["rows"]) == n

        # Full rows still on the return's top-level key (pipeline / callers).
        assert len(result["rows"]) == n

        # But the model-visible content does NOT carry all rows.
        content_json = result["content"][0]["json"]
        assert "rows" not in content_json  # capped-out
        assert content_json["row_count"] == n
        assert len(content_json["sample_first_rows"]) <= 5
        assert len(content_json["sample_last_rows"]) <= 5

    def test_content_much_smaller_than_full_serialization(self):
        """The model-visible content is a small fraction of the full result."""
        n = INLINE_ROW_CAP + 500
        agent = _Agent()
        tool = create_parse_table_tool()
        result = tool(table_text=_big_markdown_table(n), agent=agent)

        full_len = len(json.dumps(result["rows"]))
        content_len = _content_text_len(result)
        # Bounded sample content should be <10% of the full row serialization.
        assert content_len < full_len * 0.10, (
            f"content={content_len} not << full_rows={full_len}"
        )

    def test_small_table_still_inlines_rows(self):
        """Small tables keep echoing rows (fallback manual-extraction path)."""
        n = 5
        agent = _Agent()
        tool = create_parse_table_tool()
        result = tool(table_text=_big_markdown_table(n), agent=agent)
        content_json = result["content"][0]["json"]
        assert content_json["row_count"] == n
        assert "rows" in content_json
        assert len(content_json["rows"]) == n

    def test_no_tables_preserves_parser_status_in_state(self):
        """The Bedrock-facing status is folded to success/error, but the parser's
        semantic status ("no_tables_found") must NOT be lost from the object
        stored in agent state — it is preserved under ``parser_status``.
        (Regression: the fold used to mutate the same object held in state.)"""
        agent = _Agent()
        tool = create_parse_table_tool()
        # Text with no Markdown table -> parser status "no_tables_found".
        result = tool(table_text="Just some prose, no table here.", agent=agent)

        # Return is Bedrock-ToolResult-valid.
        assert result["status"] in ("success", "error")
        # Semantic status preserved on the return...
        assert result["parser_status"] == "no_tables_found"
        # ...and in the object stored in agent state (not clobbered to success).
        stored = agent.state.get("last_parse_table_result")
        if stored is not None:  # state only set when a parse was attempted
            assert stored.get("parser_status") == "no_tables_found"


class TestMapTableWriteback:
    def _prime_parse(self, agent, n):
        parse_tool = create_parse_table_tool()
        parse_tool(table_text=_big_markdown_table(n), agent=agent)

    def test_returns_status_and_content(self):
        agent = _Agent()
        self._prime_parse(agent, 10)
        map_tool = create_map_table_to_schema_tool()
        result = map_tool(
            column_mapping={
                "Date": "date",
                "Description": "description",
                "Amount": "amount",
                "Balance": "balance",
            },
            agent=agent,
        )
        assert "status" in result and "content" in result
        assert isinstance(result["content"], list)

    def test_large_map_keeps_rows_in_state_only(self):
        n = INLINE_ROW_CAP + 400
        agent = _Agent()
        self._prime_parse(agent, n)
        map_tool = create_map_table_to_schema_tool()
        result = map_tool(
            column_mapping={
                "Date": "date",
                "Description": "description",
                "Amount": "amount",
                "Balance": "balance",
            },
            agent=agent,
        )
        # Full mapped rows persist in state for finalize_table_extraction.
        stored = agent.state.get("mapped_table_rows")
        assert stored["row_count"] == n
        # Return top-level still has all rows (pipeline).
        assert len(result["mapped_rows"]) == n
        # Model-visible content is bounded.
        content_json = result["content"][0]["json"]
        assert "mapped_rows" not in content_json
        assert content_json["row_count"] == n
        content_len = _content_text_len(result)
        full_len = len(json.dumps(result["mapped_rows"]))
        assert content_len < full_len * 0.10


class TestLazyImagesConfig:
    """The lazy_images knob (perf: don't pre-load page images when the pre-flight
    table parser already covers the doc; the table path is text-driven and
    view_image fetches on demand). Defaults on; overridable for image-dependent
    corpora. The service gating (service.py) is exercised end-to-end by the
    live/local A/B; here we lock the config contract."""

    def test_lazy_images_defaults_true(self):
        from idp_common.config.models import TableParsingConfig

        assert TableParsingConfig().lazy_images is True

    def test_lazy_images_overridable(self):
        from idp_common.config.models import TableParsingConfig

        assert TableParsingConfig(lazy_images=False).lazy_images is False

    def test_lazy_images_present_after_merge(self):
        from idp_common.config.merge_utils import merge_config_with_defaults
        from idp_common.config.models import IDPConfig

        merged = merge_config_with_defaults({}, validate=True)
        cfg = IDPConfig(**merged)
        assert cfg.extraction.agentic.table_parsing.lazy_images is True


# =============================================================================
# The factory returns a real Strands tool
# =============================================================================


class TestTableToolsAreStrandsTools:
    """The ToolResult shape above only matters if Strands sees a decorated tool."""

    def test_both_factories_return_strands_tools(self):
        for tool_obj, name in (
            (create_parse_table_tool(), "parse_table"),
            (create_map_table_to_schema_tool(), "map_table_to_schema"),
        ):
            assert isinstance(tool_obj, DecoratedFunctionTool), tool_obj
            assert tool_obj.tool_name == name
