# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the table parser tool used in agentic extraction."""

from idp_common.extraction.tools.table_parser import (
    _assess_confidence,
    _find_tables_in_text,
    _is_page_marker,
    _is_separator_row,
    _parse_single_table,
    _split_table_row,
    create_parse_table_tool,
    parse_markdown_tables,
)

# =============================================================================
# Helper function tests
# =============================================================================


class TestSplitTableRow:
    """Tests for _split_table_row."""

    def test_standard_row(self):
        result = _split_table_row("| A | B | C |")
        assert result == ["A", "B", "C"]

    def test_no_outer_pipes(self):
        result = _split_table_row("A | B | C")
        assert result == ["A", "B", "C"]

    def test_extra_whitespace(self):
        result = _split_table_row("|  Date  |  Amount  |  Description  |")
        assert result == ["Date", "Amount", "Description"]

    def test_empty_cells(self):
        result = _split_table_row("| A | | C |")
        assert result == ["A", "", "C"]

    def test_single_cell(self):
        result = _split_table_row("| Only |")
        assert result == ["Only"]


class TestIsSeparatorRow:
    """Tests for _is_separator_row."""

    def test_standard_separator(self):
        assert _is_separator_row("|---|---|---|")

    def test_separator_with_colons(self):
        assert _is_separator_row("|:---|---:|:---:|")

    def test_separator_with_spaces(self):
        assert _is_separator_row("| --- | --- | --- |")

    def test_not_separator_data_row(self):
        assert not _is_separator_row("| A | B | C |")

    def test_not_separator_text(self):
        assert not _is_separator_row("Some random text")

    def test_not_separator_empty(self):
        assert not _is_separator_row("")


# =============================================================================
# Page marker tests
# =============================================================================


class TestIsPageMarker:
    """Tests for _is_page_marker."""

    def test_standard_page_marker(self):
        assert _is_page_marker("--- PAGE 5 ---")

    def test_page_marker_page_1(self):
        assert _is_page_marker("--- PAGE 1 ---")

    def test_page_marker_large_number(self):
        assert _is_page_marker("--- PAGE 123 ---")

    def test_page_marker_with_whitespace(self):
        assert _is_page_marker("  --- PAGE 5 ---  ")

    def test_not_page_marker_partial(self):
        assert not _is_page_marker("--- PAGE 5")

    def test_not_page_marker_empty(self):
        assert not _is_page_marker("")

    def test_not_page_marker_similar_text(self):
        assert not _is_page_marker("--- SECTION 5 ---")


# =============================================================================
# Table finding tests
# =============================================================================


class TestFindTablesInText:
    """Tests for _find_tables_in_text."""

    def test_single_table(self):
        text = """Some header text

| Name | Age | City |
|---|---|---|
| Alice | 30 | NYC |
| Bob | 25 | LA |

Some footer text"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 1
        assert len(tables[0]["data_lines"]) == 2
        assert "Alice" in tables[0]["data_lines"][0]

    def test_multiple_tables(self):
        text = """Table 1:
| A | B |
|---|---|
| 1 | 2 |

Table 2:
| X | Y | Z |
|---|---|---|
| a | b | c |
| d | e | f |"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 2
        assert len(tables[0]["data_lines"]) == 1
        assert len(tables[1]["data_lines"]) == 2

    def test_no_tables(self):
        text = "Just some plain text without any tables."
        tables = _find_tables_in_text(text)
        assert len(tables) == 0

    def test_header_without_data(self):
        """A table with header + separator but no data rows should not be found."""
        text = """| A | B |
|---|---|
"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 0

    def test_table_with_surrounding_text(self):
        text = """Account: 12345
Statement Period: Jan 2024

| Date | Description | Amount | Balance |
|---|---|---|---|
| 01/15 | Deposit | 3500.00 | 5200.00 |
| 01/16 | ATM | -200.00 | 5000.00 |

Total: $5000.00"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 1
        assert len(tables[0]["data_lines"]) == 2

    def test_table_with_empty_line_artifacts(self):
        """Test that parser handles OCR page break artifacts (empty lines within table)."""
        text = """| Name | Amount |
|---|---|
| Alice | 100 |

| Bob | 200 |
| Carol | 300 |
"""
        tables = _find_tables_in_text(text)
        # Should parse as single table with lookahead recovery
        assert len(tables) == 1, "Expected single table despite empty line"
        assert len(tables[0]["data_lines"]) == 4, "Expected 4 rows (3 data + 1 empty)"
        assert tables[0]["gaps_recovered"] >= 1, "Expected at least one gap recovered"

    def test_table_with_multiple_empty_lines(self):
        """Test parser handles multiple consecutive empty lines."""
        text = """| Col1 | Col2 |
|---|---|
| Row1 | A |


| Row2 | B |
"""
        tables = _find_tables_in_text(text, max_empty_line_gap=3)
        assert len(tables) == 1, "Should recover across 2 empty lines"
        assert tables[0]["gaps_recovered"] >= 1

    def test_table_with_excessive_gaps(self):
        """Test that parser stops at large gaps (not part of table)."""
        text = """| Col1 | Col2 |
|---|---|
| Row1 | A |




| Col3 | Col4 |
|---|---|
| Row2 | B |
"""
        tables = _find_tables_in_text(text, max_empty_line_gap=2)
        # Should parse as 2 separate tables (gap too large)
        assert len(tables) == 2, "Expected 2 tables separated by large gap"

    def test_table_with_missing_pipe_in_row(self):
        """Test recovery from OCR dropping pipe character in a single row."""
        text = """| Name | Amount |
|---|---|
| Alice | 100 |
Bob 200
| Carol | 300 |
"""
        tables = _find_tables_in_text(text)
        # Should recover the corrupted row
        assert len(tables) == 1
        # Note: corrupted row will be included if next row has pipes
        assert tables[0]["gaps_recovered"] >= 1

    def test_page_marker_between_table_rows(self):
        """Page markers between table rows should be skipped, keeping table contiguous."""
        text = """| Name | Amount |
|---|---|
| Alice | 100 |
--- PAGE 2 ---
| Bob | 200 |
| Carol | 300 |
"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 1, "Page marker should not split the table"
        # data_lines should not contain the page marker
        assert all("PAGE" not in line for line in tables[0]["data_lines"] if line)
        # Should have the 3 real data rows
        non_empty = [line for line in tables[0]["data_lines"] if line.strip()]
        assert len(non_empty) == 3

    def test_page_marker_before_table(self):
        """Page marker before a table should not interfere with parsing."""
        text = """--- PAGE 1 ---
| Name | Amount |
|---|---|
| Alice | 100 |
"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 1
        assert len(tables[0]["data_lines"]) == 1
        assert "Alice" in tables[0]["data_lines"][0]

    def test_multiple_page_markers_in_table(self):
        """Multiple page markers within table data should all be skipped."""
        text = """| Col1 | Col2 |
|---|---|
| Row1 | A |
--- PAGE 2 ---
| Row2 | B |
--- PAGE 3 ---
| Row3 | C |
"""
        tables = _find_tables_in_text(text)
        assert len(tables) == 1, "Multiple page markers should not split the table"
        non_empty = [line for line in tables[0]["data_lines"] if line.strip()]
        assert len(non_empty) == 3


# =============================================================================
# Table merge tests
# =============================================================================


class TestMergeAdjacentTables:
    """Tests for _merge_adjacent_tables."""

    def test_merge_identical_columns(self):
        """Test merging tables with identical column structure."""
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| Name | Age |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alice | 30 |", "| Bob | 25 |"],
                "start_line_idx": 0,
                "end_line_idx": 3,
                "gaps_recovered": 0,
            },
            {
                "header_line": "| Name | Age |",
                "separator_line": "|---|---|",
                "data_lines": ["| Carol | 28 |"],
                "start_line_idx": 5,
                "end_line_idx": 7,
                "gaps_recovered": 1,
            },
        ]

        merged = _merge_adjacent_tables(tables, proximity_threshold=10)
        assert len(merged) == 1, "Should merge tables with identical columns"
        assert len(merged[0]["data_lines"]) == 3, "Should have all 3 rows"
        assert merged[0]["gaps_recovered"] == 1, "Should accumulate gaps_recovered"
        assert merged[0]["merged_tables"] == 1, "Should track merge count"

    def test_no_merge_different_columns(self):
        """Test that tables with different columns are not merged."""
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| Name | Age |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alice | 30 |"],
                "start_line_idx": 0,
                "end_line_idx": 2,
                "gaps_recovered": 0,
            },
            {
                "header_line": "| City | Country |",
                "separator_line": "|---|---|",
                "data_lines": ["| NYC | USA |"],
                "start_line_idx": 4,
                "end_line_idx": 6,
                "gaps_recovered": 0,
            },
        ]

        merged = _merge_adjacent_tables(tables)
        assert len(merged) == 2, "Should not merge tables with different columns"

    def test_no_merge_distant_tables(self):
        """Test that distant tables are not merged even with same columns."""
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| Name | Age |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alice | 30 |"],
                "start_line_idx": 0,
                "end_line_idx": 2,
                "gaps_recovered": 0,
            },
            {
                "header_line": "| Name | Age |",
                "separator_line": "|---|---|",
                "data_lines": ["| Bob | 25 |"],
                "start_line_idx": 100,
                "end_line_idx": 102,
                "gaps_recovered": 0,
            },
        ]

        merged = _merge_adjacent_tables(tables, proximity_threshold=10)
        assert len(merged) == 2, "Should not merge distant tables"

    def test_merge_blank_continuation_header(self):
        """Continuation pages that lose a header label still merge.

        Regression test for the Nuveen large-table timeout: a multi-page table
        keeps its header on page 1 (``Fund Name``) but the header cell is blank
        on continuation pages. The strict-equality merge left the rows split
        across multiple tables; placeholder-tolerant merging must collapse them
        into one and keep the named column so every row is keyed by ``Fund Name``.
        """
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| Fund Name | Ticker |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alpha Fund | AAA |"],
                "start_line_idx": 0,
                "end_line_idx": 2,
                "gaps_recovered": 0,
            },
            {
                # Continuation page: first header cell blank
                "header_line": "|  | Ticker |",
                "separator_line": "|---|---|",
                "data_lines": ["| Beta Fund | BBB |", "| Gamma Fund | CCC |"],
                "start_line_idx": 4,
                "end_line_idx": 6,
                "gaps_recovered": 0,
            },
        ]

        merged = _merge_adjacent_tables(tables, proximity_threshold=10)
        assert len(merged) == 1, "Blank continuation header should still merge"
        assert len(merged[0]["data_lines"]) == 3, "Should have all 3 rows"
        assert merged[0]["merged_tables"] == 1, "Should track merge count"
        # The retained header must keep the real column name, not the blank cell,
        # so downstream column derivation keys every row under 'Fund Name'.
        assert _split_table_row(merged[0]["header_line"]) == ["Fund Name", "Ticker"]

    def test_merge_unnamed_placeholder_header(self):
        """A ``_unnamed_*`` placeholder header cell does not block merging."""
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| _unnamed_0 | Ticker |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alpha Fund | AAA |"],
                "start_line_idx": 0,
                "end_line_idx": 2,
                "gaps_recovered": 0,
            },
            {
                "header_line": "| Fund Name | Ticker |",
                "separator_line": "|---|---|",
                "data_lines": ["| Beta Fund | BBB |"],
                "start_line_idx": 4,
                "end_line_idx": 6,
                "gaps_recovered": 0,
            },
        ]

        merged = _merge_adjacent_tables(tables, proximity_threshold=10)
        assert len(merged) == 1, "Placeholder header should still merge"
        # The named cell from the second fragment fills the placeholder.
        assert _split_table_row(merged[0]["header_line"]) == ["Fund Name", "Ticker"]

    def test_no_merge_genuinely_different_named_columns(self):
        """Two named-but-different columns must NOT merge (no false positives)."""
        from idp_common.extraction.tools.table_parser import _merge_adjacent_tables

        tables = [
            {
                "header_line": "| Fund Name | Ticker |",
                "separator_line": "|---|---|",
                "data_lines": ["| Alpha Fund | AAA |"],
                "start_line_idx": 0,
                "end_line_idx": 2,
                "gaps_recovered": 0,
            },
            {
                "header_line": "| City | Country |",
                "separator_line": "|---|---|",
                "data_lines": ["| NYC | USA |"],
                "start_line_idx": 4,
                "end_line_idx": 6,
                "gaps_recovered": 0,
            },
        ]

        merged = _merge_adjacent_tables(tables, proximity_threshold=10)
        assert len(merged) == 2, "Distinct named columns must not merge"


# =============================================================================
# Single table parsing tests
# =============================================================================


class TestParseSingleTable:
    """Tests for _parse_single_table."""

    def test_basic_parsing(self):
        columns, rows, warnings = _parse_single_table(
            header_line="| Date | Amount | Description |",
            data_lines=[
                "| 01/15 | 3500.00 | Direct Deposit |",
                "| 01/16 | -200.00 | ATM Withdrawal |",
            ],
        )
        assert columns == ["Date", "Amount", "Description"]
        assert len(rows) == 2
        assert rows[0]["Date"] == "01/15"
        assert rows[0]["Amount"] == "3500.00"
        assert rows[1]["Description"] == "ATM Withdrawal"
        assert len(warnings) == 0

    def test_fewer_cells_than_columns(self):
        columns, rows, warnings = _parse_single_table(
            header_line="| A | B | C |",
            data_lines=["| 1 | 2 |"],
        )
        assert len(rows) == 1
        assert rows[0]["C"] == ""  # Padded
        assert len(warnings) > 0

    def test_more_cells_than_columns(self):
        columns, rows, warnings = _parse_single_table(
            header_line="| A | B |",
            data_lines=["| 1 | 2 | 3 |"],
        )
        assert len(rows) == 1
        assert len(rows[0]) == 2  # Truncated to column count
        assert len(warnings) > 0


# =============================================================================
# Confidence assessment tests
# =============================================================================


class TestAssessConfidence:
    """Tests for _assess_confidence."""

    def test_no_confidence_data(self):
        result = _assess_confidence(
            rows=[{"A": "hello"}],
            confidence_data=None,
        )
        assert result["confidence_available"] is False
        assert result["avg_confidence"] is None

    def test_high_confidence(self):
        confidence_data = """| Text | Confidence |
|:---|---:|
| hello | 99.5 |
| world | 98.2 |"""
        result = _assess_confidence(
            rows=[{"A": "hello", "B": "world"}],
            confidence_data=confidence_data,
        )
        assert result["confidence_available"] is True
        assert result["avg_confidence"] is not None
        assert result["avg_confidence"] > 95.0
        assert len(result["low_confidence_cells"]) == 0

    def test_low_confidence_detection(self):
        confidence_data = """| Text | Confidence |
|:---|---:|
| hello | 99.5 |
| wrold | 82.3 |"""
        result = _assess_confidence(
            rows=[{"A": "hello", "B": "wrold"}],
            confidence_data=confidence_data,
        )
        assert result["confidence_available"] is True
        assert len(result["low_confidence_cells"]) == 1
        assert result["low_confidence_cells"][0]["confidence"] == 82.3

    def test_empty_confidence_data(self):
        result = _assess_confidence(
            rows=[{"A": "hello"}],
            confidence_data="",
        )
        assert result["confidence_available"] is False


# =============================================================================
# Full parse_markdown_tables tests
# =============================================================================


class TestParseMarkdownTables:
    """Tests for parse_markdown_tables."""

    def test_success_simple_table(self):
        text = """| Name | Age |
|---|---|
| Alice | 30 |
| Bob | 25 |"""
        result = parse_markdown_tables(text)
        assert result["status"] == "success"
        assert result["row_count"] == 2
        assert result["columns"] == ["Name", "Age"]
        assert result["rows"][0]["Name"] == "Alice"
        assert result["rows"][1]["Age"] == "25"

    def test_no_tables_found(self):
        result = parse_markdown_tables("No tables here")
        assert result["status"] == "no_tables_found"
        assert result["row_count"] == 0

    def test_expected_columns_match(self):
        text = """| Date | Amount | Description |
|---|---|---|
| 01/15 | 100 | Payment |"""
        result = parse_markdown_tables(
            text, expected_columns=["Date", "Amount", "Description"]
        )
        assert result["status"] == "success"
        assert result["quality"]["column_match"]["match_ratio"] == 1.0

    def test_expected_columns_partial_match(self):
        text = """| Date | Amt |
|---|---|
| 01/15 | 100 |"""
        result = parse_markdown_tables(
            text, expected_columns=["Date", "Amount", "Description"]
        )
        assert result["status"] == "success"
        column_match = result["quality"]["column_match"]
        assert "amount" in column_match["missing_expected"]
        assert "description" in column_match["missing_expected"]
        assert column_match["match_ratio"] < 1.0

    def test_multiple_tables(self):
        text = """| A | B |
|---|---|
| 1 | 2 |

| X | Y | Z |
|---|---|---|
| a | b | c |"""
        result = parse_markdown_tables(text)
        assert result["status"] == "success"
        assert result["table_count"] == 2
        assert len(result["tables"]) == 2
        # First table exposed at top level
        assert result["columns"] == ["A", "B"]
        # Second table accessible via tables list
        assert result["tables"][1]["columns"] == ["X", "Y", "Z"]

    def test_with_confidence_data(self):
        text = """| Name | Score |
|---|---|
| Alice | 95 |"""
        confidence = """| Text | Confidence |
|:---|---:|
| Alice | 99.8 |
| 95 | 97.5 |"""
        result = parse_markdown_tables(text, confidence_data=confidence)
        assert result["status"] == "success"
        assert result["quality"]["confidence_available"] is True
        assert result["quality"]["avg_confidence"] is not None

    def test_quality_metrics(self):
        text = """| A | B | C |
|---|---|---|
| 1 | 2 | 3 |
| 4 | 5 | 6 |"""
        result = parse_markdown_tables(text)
        quality = result["quality"]
        assert quality["parse_success_rate"] == 1.0
        assert quality["consistent_column_count"] is True
        assert quality["empty_cell_count"] == 0
        assert quality["total_cells"] == 6

    def test_bank_statement_table(self):
        """Test with realistic bank statement OCR output."""
        text = """Account Number: 12345678
Statement Period: January 1, 2024 - January 31, 2024

| Date | Description | Debit | Credit | Balance |
|---|---|---|---|---|
| 01/01/2024 | Opening Balance | | | 5,000.00 |
| 01/05/2024 | Direct Deposit - ACME Corp | | 3,500.00 | 8,500.00 |
| 01/10/2024 | Electric Bill Payment | 150.00 | | 8,350.00 |
| 01/15/2024 | ATM Withdrawal | 200.00 | | 8,150.00 |
| 01/20/2024 | Online Transfer | 1,500.00 | | 6,650.00 |
| 01/25/2024 | Grocery Store | 85.50 | | 6,564.50 |
| 01/31/2024 | Closing Balance | | | 6,564.50 |

Closing Balance: $6,564.50"""
        result = parse_markdown_tables(text)
        assert result["status"] == "success"
        assert result["row_count"] == 7
        assert result["columns"] == [
            "Date",
            "Description",
            "Debit",
            "Credit",
            "Balance",
        ]
        assert result["rows"][0]["Date"] == "01/01/2024"
        assert result["rows"][1]["Credit"] == "3,500.00"
        assert result["rows"][4]["Debit"] == "1,500.00"


# =============================================================================
# Tool creation tests
# =============================================================================


class TestCreateParseTableTool:
    """Tests for create_parse_table_tool."""

    def test_tool_creation_no_confidence(self):
        tool_fn = create_parse_table_tool()
        assert callable(tool_fn)

    def test_tool_creation_with_confidence(self):
        tool_fn = create_parse_table_tool(
            confidence_data_by_page={
                "1": "| Text | Confidence |\n|---|---|\n| hello | 99.0 |"
            }
        )
        assert callable(tool_fn)


# =============================================================================
# Config model tests
# =============================================================================


class TestTableParsingConfig:
    """Tests for TableParsingConfig in config models."""

    def test_default_values(self):
        from idp_common.config.models import TableParsingConfig

        config = TableParsingConfig()
        assert config.enabled is False
        assert config.min_confidence_threshold == 95.0
        assert config.min_parse_success_rate == 0.90
        assert config.use_confidence_data is True

    def test_custom_values(self):
        from idp_common.config.models import TableParsingConfig

        config = TableParsingConfig(
            enabled=True,
            min_confidence_threshold=90.0,
            min_parse_success_rate=0.85,
            use_confidence_data=False,
        )
        assert config.enabled is True
        assert config.min_confidence_threshold == 90.0
        assert config.min_parse_success_rate == 0.85
        assert config.use_confidence_data is False

    def test_string_values_parsed(self):
        from idp_common.config.models import TableParsingConfig

        config = TableParsingConfig(
            min_confidence_threshold="92.5",
            min_parse_success_rate="0.88",
        )
        assert config.min_confidence_threshold == 92.5
        assert config.min_parse_success_rate == 0.88

    def test_nested_in_agentic_config(self):
        from idp_common.config.models import AgenticConfig

        config = AgenticConfig(
            enabled=True,
            table_parsing={"enabled": True, "min_confidence_threshold": 90.0},
        )
        assert config.table_parsing.enabled is True
        assert config.table_parsing.min_confidence_threshold == 90.0

    def test_nested_in_idp_config(self):
        from idp_common.config.models import IDPConfig

        config = IDPConfig(
            extraction={
                "agentic": {
                    "enabled": True,
                    "table_parsing": {
                        "enabled": True,
                        "min_confidence_threshold": 92.0,
                    },
                }
            }
        )
        assert config.extraction.agentic.table_parsing.enabled is True
        assert config.extraction.agentic.table_parsing.min_confidence_threshold == 92.0

    def test_default_in_idp_config(self):
        """Table parsing should be disabled by default."""
        from idp_common.config.models import IDPConfig

        config = IDPConfig()
        assert config.extraction.agentic.table_parsing.enabled is False


class TestAssessConfidenceFixedBreak:
    """Tests for the fixed _assess_confidence that no longer has a premature break."""

    def test_finds_lowest_confidence_across_multiple_matches(self):
        """After the break fix, should find the true lowest confidence."""
        # Confidence data where "value" is a substring of multiple entries
        # with different confidence scores
        confidence_data = (
            "| Text | Confidence |\n"
            "|:---|---:|\n"
            "| total value 100 | 99.5 |\n"
            "| value 200 | 85.0 |\n"
            "| other value 300 | 92.0 |\n"
        )
        rows = [{"col": "value"}]
        result = _assess_confidence(rows, confidence_data)

        assert result["confidence_available"] is True
        # Should find 85.0 (lowest), not 99.5 (first match)
        assert len(result["low_confidence_cells"]) == 1
        assert result["low_confidence_cells"][0]["confidence"] == 85.0

    def test_exact_match_preferred_over_substring(self):
        """Exact match should still work and be preferred."""
        confidence_data = (
            "| Text | Confidence |\n"
            "|:---|---:|\n"
            "| hello | 97.0 |\n"
            "| hello world | 80.0 |\n"
        )
        rows = [{"col": "hello"}]
        result = _assess_confidence(rows, confidence_data)

        assert result["confidence_available"] is True
        # Exact match "hello" at 97.0, no low-confidence cells
        assert result["avg_confidence"] == 97.0
        assert len(result["low_confidence_cells"]) == 0


class TestParseSuccessRateRowBased:
    """Tests for the improved parse_success_rate that uses row-level error counting."""

    def test_perfect_table_has_rate_1(self):
        """A table with no column mismatch warnings should have rate 1.0."""
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |"
        result = parse_markdown_tables(text)
        assert result["quality"]["parse_success_rate"] == 1.0
        assert result["quality"]["consistent_column_count"] is True

    def test_rate_accounts_for_row_errors_only(self):
        """Non-cell warnings (like expected column mismatches) should NOT
        lower parse_success_rate."""
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        # Pass expected_columns that don't match — this creates a warning
        # but NOT a row-level cell error
        result = parse_markdown_tables(text, expected_columns=["X", "Y"])
        # Should still be 1.0 because no ROW had column count issues
        assert result["quality"]["parse_success_rate"] == 1.0

    def test_row_with_fewer_cells_lowers_rate(self):
        """A row with fewer cells than columns creates a 'cells' warning
        and should lower parse_success_rate."""
        text = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n| only_one |"
        result = parse_markdown_tables(text)
        # 2 rows, 1 with error → rate = 0.5
        assert result["quality"]["parse_success_rate"] == 0.5
        assert result["quality"]["consistent_column_count"] is False


# =============================================================================
# Labels below a blank header row
# =============================================================================

# The shape Textract produced for samples/Nuveen.pdf: on every page a blank
# header row, the labels in the next row, and on the first page the first
# column's label on a row of its own.
BLANK = "|  |  |  |\n|--|--|--|\n"
LABELS = "|  | Ticker | NAV |\n"


def _pages(*bodies: str) -> str:
    """One blank-header fragment per body, separated so they do not merge."""
    return "\n\n--- PAGE BREAK ---\n\n".join(BLANK + body for body in bodies)


def _all_rows(result: dict) -> list[dict]:
    return [row for table in result["tables"] for row in table["rows"]]


class TestLabelsBelowABlankHeader:
    """Labels under a blank header become the columns when the document repeats them."""

    def test_labels_repeated_across_pages_become_the_columns(self):
        result = parse_markdown_tables(
            _pages(
                LABELS + "| Fund Name |  |  |\n| Alpha Fund | AAAX | $10.16 |\n",
                LABELS + "| Beta Fund | BBBX | $9.74 |\n",
            )
        )
        assert result["table_count"] == 1
        assert result["columns"] == ["Fund Name", "Ticker", "NAV"]
        assert result["row_count"] == 2
        assert [r["Ticker"] for r in result["rows"]] == ["AAAX", "BBBX"]
        assert (
            "Header row was blank; took the column names from the next 3 row(s)"
            in result["warnings"]
        )

    def test_a_lone_candidate_is_left_as_data(self):
        """A short all-text data row passes every lexical guard; one page cannot tell."""
        result = parse_markdown_tables(
            BLANK + "| Alpha Fund | AAAX | Growth |\n| Beta Fund | BBBX | $9.74 |\n"
        )
        assert result["columns"] == ["_unnamed_0", "_unnamed_1", "_unnamed_2"]
        assert result["row_count"] == 2

    def test_a_one_page_table_with_the_artifact_keeps_its_rows(self):
        result = parse_markdown_tables(
            BLANK + LABELS + "| Fund Name |  |  |\n| Alpha Fund | AAAX | $10.16 |\n"
        )
        assert result["columns"] == ["_unnamed_0", "_unnamed_1", "_unnamed_2"]
        assert result["row_count"] == 3

    def test_a_blank_header_over_data_on_every_page_is_left_as_it_was(self):
        result = parse_markdown_tables(
            _pages("| Alpha Fund | AAAX | $1.00 |\n", "| Beta Fund | BBBX | $2.00 |\n")
        )
        assert all(t["columns"][0] == "_unnamed_0" for t in result["tables"])
        assert len(_all_rows(result)) == 2

    def test_a_named_header_is_never_replaced(self):
        result = parse_markdown_tables(
            "| Name | Ticker | NAV |\n|--|--|--|\n| Label | Other | Text |\n"
            "| Alpha Fund | AAAX | $1.00 |\n"
        )
        assert result["columns"] == ["Name", "Ticker", "NAV"]
        assert result["row_count"] == 2

    def test_a_label_row_that_overlaps_the_header_stays_data(self):
        result = parse_markdown_tables(
            _pages(
                LABELS + "| Total | Amount |  |\n| Alpha | AAAX | $1.00 |\n",
                LABELS + "| Beta | BBBX | $2.00 |\n",
            )
        )
        rows = _all_rows(result)
        assert [r["Ticker"] for r in rows] == ["Amount", "AAAX", "BBBX"]

    def test_a_first_row_with_a_dash_placeholder_is_data(self):
        body = "| Alpha Fund | AAAX | - |\n| Beta Fund | BBBX | $9.74 |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert len(_all_rows(result)) == 4

    def test_a_first_row_blank_in_a_value_column_is_data(self):
        body = "| Alpha Fund | AAAX |  |\n| Beta Fund | BBBX | $9.74 |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert len(_all_rows(result)) == 4

    def test_a_sparse_category_row_is_not_taken_for_the_header(self):
        body = "| EQUITIES |  |  |\n|  | AAAX |  |\n| Alpha Fund | BBBX | $9.74 |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert len(_all_rows(result)) == 6

    def test_duplicate_labels_are_not_promoted(self):
        body = "|  | Market | Market |\n| Alpha Fund | $1.00 | $2.00 |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert len(_all_rows(result)) == 4

    def test_an_all_text_table_is_never_re_headed(self):
        body = "| Alpha Fund | AAAX | Growth |\n| Beta Fund | BBBX | Value |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert len(_all_rows(result)) == 4

    def test_a_label_row_short_of_cells_is_padded_to_the_header(self):
        body = "| Name | NAV |\n| Alpha Fund | $1.00 | Growth |\n"
        result = parse_markdown_tables(_pages(body, body))
        assert result["tables"][0]["columns"] == ["Name", "NAV", "_unnamed_2"]
        assert len(_all_rows(result)) == 2

    def test_pages_whose_labels_differ_only_in_case_and_punctuation_share_a_header(
        self,
    ):
        result = parse_markdown_tables(
            _pages(
                "|  | Ticker | NAV |\n| Alpha Fund | AAAX | $1.00 |\n",
                "|  | ticker | NAV. |\n| Beta Fund | BBBX | $2.00 |\n",
            )
        )
        assert {tuple(t["columns"]) for t in result["tables"]} == {
            ("_unnamed_0", "Ticker", "NAV")
        }
        assert [r["Ticker"] for r in _all_rows(result)] == ["AAAX", "BBBX"]

    def test_one_over_wide_row_does_not_turn_the_recovery_off(self):
        result = parse_markdown_tables(
            _pages(
                LABELS + "| Alpha Fund | AAAX | $1.00 | $9.99 |\n",
                LABELS + "| Beta Fund | BBBX | $2.00 |\n",
            )
        )
        assert result["columns"][1:] == ["Ticker", "NAV"]
        assert [r["Ticker"] for r in _all_rows(result)] == ["AAAX", "BBBX"]

    def test_a_category_row_under_the_labels_on_a_later_page_stays_a_row(self):
        result = parse_markdown_tables(
            _pages(
                LABELS + "| Fund Name |  |  |\n| Alpha Fund | AAAX | $1.00 |\n",
                LABELS + "| EQUITIES |  |  |\n| Beta Fund | BBBX | $2.00 |\n",
            )
        )
        assert result["columns"] == ["Fund Name", "Ticker", "NAV"]
        names = [r["Fund Name"] for r in _all_rows(result)]
        assert names == ["Alpha Fund", "EQUITIES", "Beta Fund"]
