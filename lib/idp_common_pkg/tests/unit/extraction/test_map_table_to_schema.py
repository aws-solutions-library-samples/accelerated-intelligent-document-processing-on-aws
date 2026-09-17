# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the map_table_to_schema tool used in agentic extraction."""

from __future__ import annotations

import strands  # noqa: E402 — mocked by conftest; make @tool a passthrough

strands.tool = lambda fn: fn

from idp_common.extraction.tools.table_parser import (  # noqa: E402
    create_map_table_to_schema_tool,
)

# ---------------------------------------------------------------------------
# Lightweight mock for Strands Agent (only needs .state with get/set)
# ---------------------------------------------------------------------------


class _SimpleState:
    def __init__(self):
        self._data: dict = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value


class MockAgent:
    def __init__(self):
        self.state = _SimpleState()


def _make_agent_with_parse_result(tables=None, columns=None, rows=None):
    """Create a MockAgent pre-loaded with last_parse_table_result."""
    agent = MockAgent()
    if tables is not None:
        agent.state.set("last_parse_table_result", {"tables": tables})
    elif columns is not None or rows is not None:
        agent.state.set(
            "last_parse_table_result",
            {"columns": columns or [], "rows": rows or []},
        )
    return agent


# ---------------------------------------------------------------------------
# Shared parse result fixtures
# ---------------------------------------------------------------------------

_SIMPLE_TABLE = {
    "columns": ["Name", "Amount", "Description"],
    "rows": [
        {"Name": "Alice", "Amount": "$1,234.56", "Description": "Direct Deposit"},
        {"Name": "Bob", "Amount": "$200.00", "Description": "ATM Withdrawal"},
    ],
}


# =============================================================================
# Basic mapping tests
# =============================================================================


class TestMapTableToSchemaBasic:
    """Tests for basic column mapping functionality."""

    def test_standard_column_mapping(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=_SIMPLE_TABLE["columns"], rows=_SIMPLE_TABLE["rows"]
        )
        result = tool_fn(
            column_mapping={"Name": "name", "Amount": "amount"},
            agent=agent,
        )
        assert result["status"] == "success"
        assert result["row_count"] == 2
        assert result["mapped_rows"][0]["name"] == "Alice"
        assert result["mapped_rows"][1]["amount"] == "$200.00"

    def test_case_insensitive_mapping(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Name", "Age"], rows=[{"Name": "Alice", "Age": "30"}]
        )
        result = tool_fn(column_mapping={"name": "full_name"}, agent=agent)
        assert result["status"] == "success"
        assert result["mapped_rows"][0]["full_name"] == "Alice"

    def test_whitespace_tolerant_mapping(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Name", "Age"], rows=[{"Name": "Alice", "Age": "30"}]
        )
        result = tool_fn(column_mapping={"  Name  ": "full_name"}, agent=agent)
        assert result["status"] == "success"
        assert result["mapped_rows"][0]["full_name"] == "Alice"

    def test_fuzzy_substring_matching(self):
        """Mapping 'Desc' matches column 'Description' via substring."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Description", "Amount"],
            rows=[{"Description": "Payment", "Amount": "100"}],
        )
        result = tool_fn(column_mapping={"Desc": "desc"}, agent=agent)
        assert result["status"] == "success"
        assert result["mapped_rows"][0]["desc"] == "Payment"

    def test_unmapped_columns_reported(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Name", "Age", "City"],
            rows=[{"Name": "Alice", "Age": "30", "City": "NYC"}],
        )
        result = tool_fn(column_mapping={"Name": "name"}, agent=agent)
        assert result["status"] == "success"
        assert "Age" in result["unmapped_columns"]
        assert "City" in result["unmapped_columns"]

    def test_unmatched_mapping_column_warning(self):
        """Mapping a column that doesn't exist in the table triggers a warning."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Name"], rows=[{"Name": "Alice"}]
        )
        result = tool_fn(
            column_mapping={"Name": "name", "NonExistent": "nope"}, agent=agent
        )
        assert result["status"] == "success"
        assert any("NonExistent" in w for w in result["warnings"])

    def test_sample_rows_included(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["A"],
            rows=[{"A": "first"}, {"A": "middle"}, {"A": "last"}],
        )
        result = tool_fn(column_mapping={"A": "val"}, agent=agent)
        assert result["sample_first_row"]["val"] == "first"
        assert result["sample_last_row"]["val"] == "last"


# =============================================================================
# Error cases
# =============================================================================


class TestMapTableToSchemaErrors:
    """Tests for error handling."""

    def test_no_agent_returns_error(self):
        tool_fn = create_map_table_to_schema_tool()
        result = tool_fn(column_mapping={"A": "a"}, agent=None)
        assert result["status"] == "error"

    def test_no_parse_data_in_state(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = MockAgent()  # no last_parse_table_result
        result = tool_fn(column_mapping={"A": "a"}, agent=agent)
        assert result["status"] == "error"

    def test_no_rows_returns_error(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(columns=["A"], rows=[])
        result = tool_fn(column_mapping={"A": "a"}, agent=agent)
        assert result["status"] == "error"


# =============================================================================
# Value transforms
# =============================================================================


class TestMapTableTransforms:
    """Tests for value_transforms parameter."""

    def test_strip_currency(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Amount"], rows=[{"Amount": "$1,234.56"}]
        )
        result = tool_fn(
            column_mapping={"Amount": "amount"},
            value_transforms={"amount": "strip_currency"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["amount"] == "1234.56"

    def test_strip_currency_no_dollar_sign(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Amount"], rows=[{"Amount": "1,234.56"}]
        )
        result = tool_fn(
            column_mapping={"Amount": "amount"},
            value_transforms={"amount": "strip_currency"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["amount"] == "1234.56"

    def test_strip_whitespace(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Code"], rows=[{"Code": "AB  CD  EF"}]
        )
        result = tool_fn(
            column_mapping={"Code": "code"},
            value_transforms={"code": "strip_whitespace"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["code"] == "ABCDEF"

    def test_lowercase(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Status"], rows=[{"Status": "ACTIVE"}]
        )
        result = tool_fn(
            column_mapping={"Status": "status"},
            value_transforms={"status": "lowercase"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["status"] == "active"

    def test_uppercase(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Type"], rows=[{"Type": "credit"}]
        )
        result = tool_fn(
            column_mapping={"Type": "type"},
            value_transforms={"type": "uppercase"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["type"] == "CREDIT"

    def test_unknown_transform_passthrough(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(columns=["Val"], rows=[{"Val": "hello"}])
        result = tool_fn(
            column_mapping={"Val": "val"},
            value_transforms={"val": "unknown_transform"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["val"] == "hello"


# =============================================================================
# Static fields
# =============================================================================


class TestMapTableStaticFields:
    """Tests for static_fields parameter."""

    def test_static_fields_added_to_every_row(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Item"],
            rows=[{"Item": "A"}, {"Item": "B"}, {"Item": "C"}],
        )
        result = tool_fn(
            column_mapping={"Item": "item"},
            static_fields={"account": "XYZ-123"},
            agent=agent,
        )
        assert all(r["account"] == "XYZ-123" for r in result["mapped_rows"])

    def test_static_fields_none_is_noop(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(columns=["A"], rows=[{"A": "val"}])
        result = tool_fn(column_mapping={"A": "a"}, static_fields=None, agent=agent)
        assert result["status"] == "success"
        assert "account" not in result["mapped_rows"][0]


# =============================================================================
# Merged-row splitting
# =============================================================================


class TestMapTableMergedRowSplitting:
    """Tests for automatic merged-row detection and splitting."""

    def test_no_split_single_value_columns(self):
        """Normal rows with single values should pass through unchanged."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Price", "Qty"],
            rows=[{"Price": "$10.00", "Qty": "5"}],
        )
        result = tool_fn(column_mapping={"Price": "price", "Qty": "qty"}, agent=agent)
        assert result["row_count"] == 1

    def test_split_dual_dollar_values(self):
        """Rows with two dollar amounts in 2+ columns should split."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Price", "Value", "Name"],
            rows=[
                {
                    "Price": "$57.90 $55.11",
                    "Value": "$100.00 $200.00",
                    "Name": "FOO BAR",
                }
            ],
        )
        result = tool_fn(
            column_mapping={"Price": "price", "Value": "value", "Name": "name"},
            agent=agent,
        )
        assert result["row_count"] == 2
        assert result["mapped_rows"][0]["price"] == "$57.90"
        assert result["mapped_rows"][1]["price"] == "$55.11"

    def test_split_dual_plain_numbers(self):
        """Rows with two plain numbers in 2+ columns should split."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Qty", "Total", "Desc"],
            rows=[{"Qty": "457 889", "Total": "100 200", "Desc": "ACME CORP BETA INC"}],
        )
        result = tool_fn(
            column_mapping={"Qty": "qty", "Total": "total", "Desc": "desc"},
            agent=agent,
        )
        assert result["row_count"] == 2
        assert result["mapped_rows"][0]["qty"] == "457"
        assert result["mapped_rows"][1]["qty"] == "889"

    def test_split_text_fields_at_midpoint(self):
        """Text fields with 4+ words should split at middle."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Price", "Value", "Name"],
            rows=[
                {
                    "Price": "$10.00 $20.00",
                    "Value": "$30.00 $40.00",
                    "Name": "GKE CORPORATION DVE CORPORATION",
                }
            ],
        )
        result = tool_fn(
            column_mapping={"Price": "price", "Value": "value", "Name": "name"},
            agent=agent,
        )
        assert result["row_count"] == 2
        assert result["mapped_rows"][0]["name"] == "GKE CORPORATION"
        assert result["mapped_rows"][1]["name"] == "DVE CORPORATION"

    def test_no_split_insufficient_signals(self):
        """Only 1 column with dual values (< 2 signals) should not split."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["Price", "Name"],
            rows=[{"Price": "$57.90 $55.11", "Name": "Single Item"}],
        )
        result = tool_fn(column_mapping={"Price": "price", "Name": "name"}, agent=agent)
        # Only 1 column has merge signal, needs >= 2 to split
        assert result["row_count"] == 1


# =============================================================================
# State accumulation
# =============================================================================


class TestMapTableStateAccumulation:
    """Tests for agent state management."""

    def test_multiple_tables_combined(self):
        """When parse result has multiple tables, all rows are combined."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            tables=[
                {"columns": ["A", "B"], "rows": [{"A": "1", "B": "2"}]},
                {"columns": ["A", "B"], "rows": [{"A": "3", "B": "4"}]},
            ]
        )
        result = tool_fn(column_mapping={"A": "a", "B": "b"}, agent=agent)
        assert result["row_count"] == 2

    def test_accumulates_across_calls(self):
        """Calling map_table_to_schema twice accumulates rows in state."""
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(columns=["X"], rows=[{"X": "first"}])
        tool_fn(column_mapping={"X": "x"}, agent=agent)

        # Simulate a second parse_table result
        agent.state.set(
            "last_parse_table_result",
            {"columns": ["X"], "rows": [{"X": "second"}]},
        )
        tool_fn(column_mapping={"X": "x"}, agent=agent)

        mapped = agent.state.get("mapped_table_rows")
        assert mapped["row_count"] == 2
        assert mapped["mapped_rows"][0]["x"] == "first"
        assert mapped["mapped_rows"][1]["x"] == "second"

    def test_stats_updated_in_state(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = _make_agent_with_parse_result(
            columns=["A"], rows=[{"A": "val1"}, {"A": "val2"}]
        )
        tool_fn(column_mapping={"A": "a"}, agent=agent)
        stats = agent.state.get("table_parsing_stats")
        assert stats["rows_mapped"] == 2
        assert stats["mapping_used"] is True


# =============================================================================
# Date transforms
# =============================================================================


class TestMapTableDateTransforms:
    """Tests for the date_to_iso* value transforms.

    These exist because ``finalize_table_extraction`` validates against a model
    generated from the class schema, where ``format: date`` is a real
    ``datetime.date``. A correctly parsed ``05/09/2024`` cannot validate, and
    the agent's only other recourse is to re-emit every row by hand.
    """

    def _rows(self, dates):
        return _make_agent_with_parse_result(
            columns=["Date"], rows=[{"Date": d} for d in dates]
        )

    def test_mdy_transform_converts_ambiguous_dates(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["05/09/2024", "12/16/2024"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_mdy"},
            agent=agent,
        )
        assert [r["Date"] for r in result["mapped_rows"]] == [
            "2024-05-09",
            "2024-12-16",
        ]

    def test_dmy_transform_reads_the_other_order(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["05/09/2024"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_dmy"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["Date"] == "2024-09-05"

    def test_auto_converts_unambiguous_and_refuses_ambiguous(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["15/03/2024", "01/02/2024"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso"},
            agent=agent,
        )
        # Unambiguous (15 cannot be a month) converts; ambiguous is left alone.
        assert result["mapped_rows"][0]["Date"] == "2024-03-15"
        assert result["mapped_rows"][1]["Date"] == "01/02/2024"
        assert any("refused" in w.lower() for w in result["warnings"])

    def test_explicit_order_never_overrides_an_unambiguous_value(self):
        """A 15 is not a month however the caller reads the column."""
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["15/03/2024"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_mdy"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["Date"] == "2024-03-15"

    def test_iso_values_pass_through_unchanged(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["2024-03-15"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_mdy"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["Date"] == "2024-03-15"
        assert not [w for w in result["warnings"] if "refused" in w.lower()]

    def test_unparseable_value_is_left_alone_and_reported(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["not a date"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_mdy"},
            agent=agent,
        )
        assert result["mapped_rows"][0]["Date"] == "not a date"
        assert any("refused" in w.lower() for w in result["warnings"])

    def test_refusal_warnings_are_capped_per_field_not_per_row(self):
        """A 1,200-row table must not emit 1,200 warnings."""
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["01/02/2024"] * 50)
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso"},
            agent=agent,
        )
        assert len([w for w in result["warnings"] if "refused" in w.lower()]) == 1

    def test_unknown_transform_is_reported_once(self):
        tool_fn = create_map_table_to_schema_tool()
        agent = self._rows(["05/09/2024", "06/09/2024"])
        result = tool_fn(
            column_mapping={"Date": "Date"},
            value_transforms={"Date": "date_to_iso_ymd"},  # not a real transform
            agent=agent,
        )
        assert result["mapped_rows"][0]["Date"] == "05/09/2024"
        unknown = [w for w in result["warnings"] if "Unknown value transform" in w]
        assert len(unknown) == 1
        assert "date_to_iso_mdy" in unknown[0]
