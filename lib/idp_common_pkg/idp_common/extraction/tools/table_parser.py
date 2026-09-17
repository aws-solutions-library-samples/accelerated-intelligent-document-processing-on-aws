# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Table parser tool for agentic extraction.

Provides a deterministic Markdown table parser that the extraction agent can use
to efficiently extract structured tabular data without LLM inference. The agent
decides when to use this tool based on document quality and table structure.

This tool handles the mechanical parsing while the agent handles semantic reasoning:
- Column-to-schema field mapping
- Value correction for low-confidence cells
- Fallback to LLM extraction when table quality is poor
"""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)

# Maximum number of parsed data rows to inline in a tool result's ``content``
# (the part Strands re-sends to the model on EVERY subsequent turn). The full
# row set is always preserved in agent state for the mapping/finalize pipeline;
# only the model-visible echo is capped. Tables at or below this size still
# echo all rows so the manual/fallback extraction workflow keeps working; large
# tables echo only head+tail samples + stats, so the dense rows never re-enter
# the growing conversation. This is the fix for the agentic input-token blow-up
# (parsed rows were being re-transmitted N times across the multi-turn loop).
INLINE_ROW_CAP = 50
# How many head/tail sample rows to show the model when a table exceeds the cap.
SAMPLE_ROWS = 5

# Value transforms the mapping tool understands. The date ones exist because
# ``finalize_table_extraction`` validates against a Pydantic model generated
# from the class schema, and ``format: date`` there is a real ``datetime.date``
# — so a parsed ``05/09/2024`` can never validate, no matter how correct it is.
# Without a deterministic conversion the agent's only recourse is to re-emit
# every row itself, which is exactly the output-token cost the deterministic
# table pipeline exists to avoid.
DATE_TRANSFORMS = {
    # transform name -> date_order passed to the shared date parser
    "date_to_iso": "auto",
    "date_to_iso_mdy": "MDY",
    "date_to_iso_dmy": "DMY",
}
# Cap on how many distinct per-value transform refusals are reported back.
MAX_TRANSFORM_WARNINGS = 3
_SUPPORTED_TRANSFORMS = frozenset(
    {"strip_currency", "strip_whitespace", "lowercase", "uppercase"}
    | set(DATE_TRANSFORMS)
)


def _to_iso_date(value: str, date_order: str) -> tuple[str, str]:
    """Convert one date string to ISO ``YYYY-MM-DD``. ``(value, reason)``.

    Returns the value unchanged with a reason when the conversion is refused —
    a wrongly "fixed" date is data corruption nothing downstream can catch, so
    an ambiguous value is left exactly as parsed. Reuses the same parser (and
    therefore the same refusal policy) as ``extraction.coercion``.
    """
    from idp_common.extraction.coercion import _parse_date_string

    iso, note = _parse_date_string(value, date_order)
    if iso is not None:
        return iso, ""
    # note == "" means "already ISO and valid: nothing to do"
    return value, note


def _compact_content(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap a compact summary dict as a Strands ToolResultContent list.

    Returning a dict with BOTH ``status`` and ``content`` makes Strands treat it
    as an already-formatted ToolResult (see strands.tools.decorator
    ._wrap_tool_result) instead of falling back to ``str(result)`` — which would
    inline the ENTIRE dict (all parsed rows) into agent.messages and re-send it
    every turn. Bedrock's request formatter (BedrockModel
    ._format_request_message_content) transmits only this ``content`` (plus
    toolUseId/status), silently dropping any extra top-level keys — so the dense
    ``rows``/``mapped_rows`` we keep alongside for state/tests are never sent.
    """
    return [{"json": summary}]


def _build_row_summary(
    rows: list[Any],
    row_count: int,
    rows_key: str,
    next_tool: str,
) -> dict[str, Any]:
    """Build the model-visible row portion of a tool-result summary.

    Small tables (<= ``INLINE_ROW_CAP``) inline every row under ``rows_key`` so
    the manual/fallback extraction workflow still sees the data. Large tables
    inline only head/tail samples + counts + a note pointing the agent at the
    next tool (which reads the full rows from agent state) — so the dense rows
    never re-enter the growing conversation. Shared by parse_table and
    map_table_to_schema to avoid drift between the two blocks.
    """
    if row_count <= INLINE_ROW_CAP:
        return {rows_key: rows}
    return {
        "sample_first_rows": rows[:SAMPLE_ROWS],
        "sample_last_rows": rows[-SAMPLE_ROWS:],
        "rows_omitted_from_echo": row_count - 2 * SAMPLE_ROWS,
        "note": (
            f"All {row_count} rows are stored in agent state. "
            f"Call {next_tool} next (it reads them from state) — "
            "do NOT re-type the rows."
        ),
    }


def _split_table_row(line: str) -> list[str]:
    """
    Split a Markdown table row into cells, handling edge pipes.

    Args:
        line: A single Markdown table row (e.g., "| A | B | C |")

    Returns:
        List of cell values with whitespace stripped
    """
    # Strip leading/trailing whitespace and pipes
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    return [cell.strip() for cell in stripped.split("|")]


def _is_page_marker(line: str) -> bool:
    """Check if a line is a page boundary marker (e.g., '--- PAGE 5 ---')."""
    stripped = line.strip()
    return stripped.startswith("--- PAGE ") and stripped.endswith("---")


def _is_separator_row(line: str) -> bool:
    """
    Check if a line is a Markdown table separator row (e.g., |---|---|---|).

    Args:
        line: A line of text to check

    Returns:
        True if the line is a separator row
    """
    # Remove pipes and whitespace, check if remaining is only dashes, colons, spaces
    cleaned = line.replace("|", "").replace("-", "").replace(":", "").strip()
    return len(cleaned) == 0 and "-" in line


def _find_tables_in_text(
    text: str, max_empty_line_gap: int = 3
) -> list[dict[str, Any]]:
    """
    Find all Markdown tables in OCR text, returning their positions and content.

    A table is identified as:
    1. A header row containing pipes
    2. A separator row (dashes with pipes)
    3. One or more data rows containing pipes

    This function is robust to OCR artifacts like empty lines and missing pipes
    within table content by using lookahead to detect table continuation.

    Args:
        text: Full OCR text that may contain one or more Markdown tables
        max_empty_line_gap: Maximum consecutive empty lines to allow within a table
            before treating as table boundary (default: 3)

    Returns:
        List of dicts, each with:
        - 'header_line': str - the header row
        - 'separator_line': str - the separator row
        - 'data_lines': list[str] - the data rows
        - 'start_line_idx': int - line index where table starts
        - 'end_line_idx': int - line index where table ends
        - 'gaps_recovered': int - number of gaps that were recovered via lookahead
    """
    lines = text.split("\n")
    tables: list[dict[str, Any]] = []
    i = 0

    while i < len(lines) - 1:  # Need at least header + separator
        line = lines[i].strip()

        # Skip page markers
        if _is_page_marker(line):
            i += 1
            continue

        # Look for potential header row (must contain pipe)
        if "|" not in line:
            i += 1
            continue

        # Check if next line is a separator
        if i + 1 >= len(lines):
            i += 1
            continue

        next_line = lines[i + 1].strip()
        if not _is_separator_row(next_line):
            i += 1
            continue

        # Found header + separator. Collect data rows with lookahead tolerance
        header_line = line
        separator_line = next_line
        data_lines: list[str] = []
        j = i + 2
        gaps_recovered = 0

        while j < len(lines):
            data_line = lines[j].strip()

            # Stop at another separator row (nested table boundary)
            if _is_separator_row(data_line):
                break

            # Treat page markers as empty lines (don't break table continuity)
            if _is_page_marker(data_line):
                j += 1
                continue

            # Handle empty lines with lookahead
            if not data_line:
                # Look ahead to see if table continues after gap
                lookahead_found = False
                lookahead_distance = 0
                for k in range(j + 1, min(j + max_empty_line_gap + 1, len(lines))):
                    lookahead_line = lines[k].strip()
                    if lookahead_line and "|" in lookahead_line:
                        # Don't jump over another separator (that's a new table)
                        if not _is_separator_row(lookahead_line):
                            # Check if this is a new table header (has separator after it)
                            if k + 1 < len(lines):
                                next_after_lookahead = lines[k + 1].strip()
                                if _is_separator_row(next_after_lookahead):
                                    # This is a new table, not a continuation
                                    break
                            # Valid table row continuation
                            lookahead_found = True
                            lookahead_distance = k - j
                            break

                if lookahead_found:
                    # Table continues after gap - preserve gap as empty rows
                    for _ in range(lookahead_distance):
                        data_lines.append("")
                    j += lookahead_distance
                    gaps_recovered += 1
                    continue
                else:
                    # Real end of table
                    break

            # Handle lines without pipes - use lookahead
            if "|" not in data_line:
                # Look ahead to see if this is just one corrupted line
                lookahead_found = False
                if j + 1 < len(lines):
                    next_lookahead = lines[j + 1].strip()
                    if next_lookahead and "|" in next_lookahead:
                        # Likely a corrupted row, keep it and continue
                        data_lines.append(data_line)
                        j += 1
                        gaps_recovered += 1
                        continue

                # No table continuation found
                break

            # Valid table row
            data_lines.append(data_line)
            j += 1

        if data_lines:
            tables.append(
                {
                    "header_line": header_line,
                    "separator_line": separator_line,
                    "data_lines": data_lines,
                    "start_line_idx": i,
                    "end_line_idx": j - 1,
                    "gaps_recovered": gaps_recovered,
                }
            )

        # Skip past this table
        i = j

    return tables


def _is_placeholder_header_cell(cell: str) -> bool:
    """Return True for a header cell that carries no real column name.

    OCR of a table that continues across a page break frequently drops the
    repeated header text on continuation pages, so the first column (or any
    column) can come back blank — which later becomes an ``_unnamed_N``
    placeholder in :func:`_parse_single_table`. Such cells must not block the
    merge of otherwise-identical table fragments.
    """
    stripped = cell.strip()
    return not stripped or stripped.startswith("_unnamed_")


def _headers_mergeable(prev_cols: list[str], curr_cols: list[str]) -> bool:
    """Whether two header rows describe the same columns for merge purposes.

    Equal-length headers are considered mergeable when every position either
    matches exactly (case/whitespace-insensitive) or one side is a placeholder
    (blank / ``_unnamed_*``). This recovers the common page-break artifact where
    a continuation page loses only the (usually first) header label while all
    other columns line up. A pure exact match still qualifies.
    """
    if len(prev_cols) != len(curr_cols):
        return False
    for prev_cell, curr_cell in zip(prev_cols, curr_cols):
        if prev_cell.strip().lower() == curr_cell.strip().lower():
            continue
        if _is_placeholder_header_cell(prev_cell) or _is_placeholder_header_cell(
            curr_cell
        ):
            continue
        return False
    return True


def _upgrade_header_line(prev_header: str, curr_header: str) -> str:
    """Fill placeholder cells in ``prev_header`` from ``curr_header``.

    When merging fragments, keep every real column name: if the retained
    (previous) header has a blank/``_unnamed_*`` cell but the incoming fragment
    names that column, adopt the named value. This ensures the merged table's
    columns (derived from ``header_line`` downstream) key EVERY row consistently
    — otherwise continuation rows would be keyed under ``_unnamed_0`` and lose,
    e.g., the fund name on most rows.
    """
    prev_cols = _split_table_row(prev_header)
    curr_cols = _split_table_row(curr_header)
    if len(prev_cols) != len(curr_cols):
        return prev_header
    if not any(_is_placeholder_header_cell(c) for c in prev_cols):
        return prev_header
    upgraded = [
        curr
        if _is_placeholder_header_cell(prev) and not _is_placeholder_header_cell(curr)
        else prev
        for prev, curr in zip(prev_cols, curr_cols)
    ]
    return "| " + " | ".join(upgraded) + " |"


def _merge_adjacent_tables(
    tables: list[dict[str, Any]], proximity_threshold: int = 10
) -> list[dict[str, Any]]:
    """
    Merge consecutive tables that have the same column structure.

    This recovers from table splits caused by OCR artifacts like page breaks,
    where a single logical table gets parsed as multiple separate tables. Header
    rows are compared with placeholder tolerance (see :func:`_headers_mergeable`)
    so a continuation page that lost a header label (blank / ``_unnamed_*`` cell)
    still merges; the retained header is upgraded to keep every real column name.

    Args:
        tables: List of parsed table dicts from _find_tables_in_text
        proximity_threshold: Maximum line gap between tables to consider for merging

    Returns:
        List of merged tables (fewer items if tables were merged)
    """
    if len(tables) <= 1:
        return tables

    merged: list[dict[str, Any]] = [tables[0]]

    for table in tables[1:]:
        prev = merged[-1]

        # Compare column structure
        prev_cols = _split_table_row(prev["header_line"])
        curr_cols = _split_table_row(table["header_line"])

        # Calculate proximity (line gap between tables)
        line_gap = table["start_line_idx"] - prev["end_line_idx"]

        # Merge if columns match (placeholder-tolerant) and tables are close
        if _headers_mergeable(prev_cols, curr_cols) and line_gap <= proximity_threshold:
            # Preserve every real column name when a fragment fills a blank header
            prev["header_line"] = _upgrade_header_line(
                prev["header_line"], table["header_line"]
            )
            # Merge data rows into previous table
            prev["data_lines"].extend(table["data_lines"])
            prev["end_line_idx"] = table["end_line_idx"]

            # Accumulate gaps_recovered
            prev["gaps_recovered"] = prev.get("gaps_recovered", 0) + table.get(
                "gaps_recovered", 0
            )

            # Track number of additional tables merged (not including the original)
            if "merged_tables" not in prev:
                prev["merged_tables"] = 0
            prev["merged_tables"] += 1

            logger.debug(
                f"Merged adjacent table with {len(table['data_lines'])} rows "
                f"(line gap: {line_gap}, total rows now: {len(prev['data_lines'])})"
            )
        else:
            # Different structure or too far apart - keep as separate table
            merged.append(table)

    return merged


def _parse_single_table(
    header_line: str,
    data_lines: list[str],
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """
    Parse a single Markdown table into columns and rows.

    Handles common OCR artifacts:
    - Extra whitespace
    - Missing/extra pipe characters
    - Inconsistent column counts
    - Empty cells

    Args:
        header_line: The header row text
        data_lines: List of data row texts

    Returns:
        Tuple of (columns, rows, warnings)
        - columns: list of column header strings
        - rows: list of dicts mapping column names to cell values
        - warnings: list of warning messages about parsing issues
    """
    warnings: list[str] = []

    # Parse header columns
    columns = _split_table_row(header_line)

    # Filter out empty column names (can happen with malformed tables)
    if any(not col for col in columns):
        original_count = len(columns)
        columns = [col if col else f"_unnamed_{idx}" for idx, col in enumerate(columns)]
        warnings.append(
            f"Header had {original_count - len([c for c in columns if not c.startswith('_unnamed_')])} "
            f"empty column name(s), assigned placeholder names"
        )

    num_columns = len(columns)

    # Parse data rows
    rows: list[dict[str, str]] = []
    for row_idx, line in enumerate(data_lines):
        cells = _split_table_row(line)

        # Handle column count mismatch
        if len(cells) < num_columns:
            cells.extend([""] * (num_columns - len(cells)))
            warnings.append(
                f"Row {row_idx}: fewer cells ({len(cells) - (num_columns - len(cells))}) "
                f"than columns ({num_columns}), padded with empty values"
            )
        elif len(cells) > num_columns:
            warnings.append(
                f"Row {row_idx}: more cells ({len(cells)}) than columns ({num_columns}), "
                f"extra cells truncated"
            )
            cells = cells[:num_columns]

        row_dict = dict(zip(columns, cells))
        rows.append(row_dict)

    return columns, rows, warnings


def _assess_confidence(
    rows: list[dict[str, str]],
    confidence_data: str | None,
) -> dict[str, Any]:
    """
    Cross-reference parsed table content with OCR confidence data.

    Reads the LINE-level confidence from textConfidence.json format and
    maps it to table content to identify low-confidence regions.

    Args:
        rows: Parsed table rows
        confidence_data: OCR confidence data in Markdown table format
            (as produced by Textract: "| Text | Confidence |\\n...")

    Returns:
        Dict with confidence metrics:
        - avg_confidence: float or None
        - low_confidence_cells: list of dicts with text, confidence, threshold info
        - confidence_available: bool
    """
    if not confidence_data:
        return {
            "avg_confidence": None,
            "low_confidence_cells": [],
            "confidence_available": False,
        }

    # Parse confidence data (format: "| Text | Confidence |\n|:---|---:|\n| some text | 99.5 |")
    conf_map: dict[str, float] = {}
    for line in confidence_data.split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        # Skip header and separator rows
        if "Confidence" in line or _is_separator_row(line):
            continue

        parts = _split_table_row(line)
        if len(parts) >= 2:
            text = parts[0].strip()
            try:
                confidence = float(parts[-1].strip())
                if text:
                    conf_map[text] = confidence
            except ValueError:
                continue

    if not conf_map:
        return {
            "avg_confidence": None,
            "low_confidence_cells": [],
            "confidence_available": False,
        }

    # Match cell values against confidence data
    matched_scores: list[float] = []
    low_confidence_cells: list[dict[str, Any]] = []

    for row_idx, row in enumerate(rows):
        for col_name, cell_value in row.items():
            if not cell_value:
                continue

            # Try exact match first, then substring match
            best_confidence = None
            matched_text = None

            if cell_value in conf_map:
                best_confidence = conf_map[cell_value]
                matched_text = cell_value
            else:
                # Try to find the cell value as a substring of a confidence line.
                # Iterate all entries to find the worst-case (lowest) confidence.
                for conf_text, conf_score in conf_map.items():
                    if cell_value in conf_text or conf_text in cell_value:
                        if best_confidence is None or conf_score < best_confidence:
                            best_confidence = conf_score
                            matched_text = conf_text

            if best_confidence is not None:
                matched_scores.append(best_confidence)
                # Textract confidence is 0-100 scale
                if best_confidence < 95.0:
                    low_confidence_cells.append(
                        {
                            "row": row_idx,
                            "column": col_name,
                            "cell_value": cell_value,
                            "confidence": best_confidence,
                            "matched_text": matched_text,
                        }
                    )

    avg_confidence = (
        sum(matched_scores) / len(matched_scores) if matched_scores else None
    )

    return {
        "avg_confidence": round(avg_confidence, 2) if avg_confidence else None,
        "low_confidence_cells": low_confidence_cells,
        "confidence_available": True,
        "cells_matched": len(matched_scores),
        "total_cells": sum(len(row) for row in rows),
    }


def parse_markdown_tables(
    text: str,
    expected_columns: list[str] | None = None,
    confidence_data: str | None = None,
    max_empty_line_gap: int = 3,
    auto_merge_adjacent_tables: bool = True,
) -> dict[str, Any]:
    """
    Parse all Markdown tables found in OCR text into structured data.

    This is the core parsing function used by the parse_table tool.

    Args:
        text: OCR text containing one or more Markdown tables
        expected_columns: Optional list of expected column names for validation
        confidence_data: Optional OCR confidence data for quality assessment
        max_empty_line_gap: Maximum consecutive empty lines to allow within a table (default: 3)
        auto_merge_adjacent_tables: Whether to merge adjacent tables with identical columns (default: True)

    Returns:
        Dict with:
        - status: "success" or "no_tables_found" or "error"
        - tables: list of parsed table results (if multiple tables found)
        - columns: list of column headers (first/only table)
        - rows: list of row dicts (first/only table)
        - row_count: int
        - quality: quality metrics dict
        - warnings: list of warning strings
    """
    try:
        # Find all tables in the text (with lookahead tolerance for OCR artifacts)
        raw_tables = _find_tables_in_text(text, max_empty_line_gap=max_empty_line_gap)

        # Merge adjacent tables with identical columns (recovers from table splits)
        if auto_merge_adjacent_tables and len(raw_tables) > 1:
            original_count = len(raw_tables)
            raw_tables = _merge_adjacent_tables(raw_tables)
            if len(raw_tables) < original_count:
                logger.info(
                    f"Merged {original_count - len(raw_tables)} adjacent table(s) "
                    f"with identical columns ({original_count} -> {len(raw_tables)} tables)"
                )

        if not raw_tables:
            return {
                "status": "no_tables_found",
                "tables": [],
                "columns": [],
                "rows": [],
                "row_count": 0,
                "quality": {
                    "parse_success_rate": 0.0,
                    "consistent_column_count": False,
                },
                "warnings": ["No Markdown tables found in the provided text"],
            }

        all_tables: list[dict[str, Any]] = []
        all_warnings: list[str] = []

        for table_idx, raw_table in enumerate(raw_tables):
            columns, rows, warnings = _parse_single_table(
                header_line=raw_table["header_line"],
                data_lines=raw_table["data_lines"],
            )

            # Prefix warnings with table index if multiple tables
            if len(raw_tables) > 1:
                warnings = [f"Table {table_idx}: {w}" for w in warnings]

            # Check expected columns if provided
            column_match = None
            if expected_columns:
                # Case-insensitive comparison
                actual_lower = {c.lower().strip() for c in columns}
                expected_lower = {c.lower().strip() for c in expected_columns}
                matched = actual_lower & expected_lower
                missing = expected_lower - actual_lower
                extra = actual_lower - expected_lower

                column_match = {
                    "matched": sorted(matched),
                    "missing_expected": sorted(missing),
                    "extra_actual": sorted(extra),
                    "match_ratio": len(matched) / len(expected_lower)
                    if expected_lower
                    else 0.0,
                }

                if missing:
                    warnings.append(f"Expected columns not found: {sorted(missing)}")

            # Calculate quality metrics
            empty_cells = sum(1 for row in rows for v in row.values() if not v.strip())
            total_cells = len(rows) * len(columns) if columns else 0

            # Count rows with actual parsing errors (column count mismatches)
            row_error_warnings = [w for w in warnings if "cells" in w.lower()]
            rows_with_errors = len(row_error_warnings)

            # Assess confidence
            confidence_metrics = _assess_confidence(rows, confidence_data)

            quality = {
                "parse_success_rate": 1.0 - (rows_with_errors / max(len(rows), 1)),
                "consistent_column_count": rows_with_errors == 0,
                "empty_cell_count": empty_cells,
                "total_cells": total_cells,
                "empty_cell_ratio": empty_cells / total_cells
                if total_cells > 0
                else 0.0,
                **confidence_metrics,
            }

            if column_match:
                quality["column_match"] = column_match

            table_result = {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "quality": quality,
                "warnings": warnings,
                "source_line_range": {
                    "start": raw_table["start_line_idx"],
                    "end": raw_table["end_line_idx"],
                },
                "gaps_recovered": raw_table.get("gaps_recovered", 0),
                "merged_tables": raw_table.get("merged_tables", 0),
            }

            all_tables.append(table_result)
            all_warnings.extend(warnings)

        # For convenience, expose first table's data at top level
        first_table = all_tables[0]

        return {
            "status": "success",
            "tables": all_tables,
            "table_count": len(all_tables),
            # First table data at top level for convenience
            "columns": first_table["columns"],
            "rows": first_table["rows"],
            "row_count": first_table["row_count"],
            "quality": first_table["quality"],
            "warnings": all_warnings,
        }

    except Exception as e:
        logger.error(f"Error parsing tables: {e}", exc_info=True)
        return {
            "status": "error",
            "tables": [],
            "columns": [],
            "rows": [],
            "row_count": 0,
            "quality": {"parse_success_rate": 0.0},
            "warnings": [f"Parse error: {str(e)}"],
        }


def create_parse_table_tool(
    confidence_data_by_page: dict[str, str] | None = None,
    max_empty_line_gap: int = 3,
    auto_merge_adjacent_tables: bool = True,
):
    """
    Create a parse_table tool with optional pre-loaded confidence data.

    The tool also records usage statistics in the agent's state under the key
    ``table_parsing_stats`` so that the extraction service can include them in
    the result metadata for observability.

    Args:
        confidence_data_by_page: Optional dict mapping page IDs to confidence data strings.
            When provided, the tool can cross-reference parsed values with OCR confidence scores.
            Typically available when OCR backend is Textract; None for Bedrock OCR.
        max_empty_line_gap: Maximum consecutive empty lines to allow within a table (default: 3)
        auto_merge_adjacent_tables: Whether to merge adjacent tables with identical columns (default: True)

    Returns:
        A Strands @tool function that the extraction agent can use
    """
    # Merge all page confidence data into a single string for lookup
    merged_confidence: str | None = None
    if confidence_data_by_page:
        merged_confidence = "\n".join(confidence_data_by_page.values())

    @tool
    def parse_table(
        table_text: str,
        expected_columns: list[str] | None = None,
        table_index: int | None = None,
        agent: Any = None,
    ) -> dict[str, Any]:
        """
        Parse a Markdown table from OCR text into structured rows and columns.

        Use this tool when you identify a well-formatted table in the document text.
        It parses the table deterministically (no LLM inference needed) and returns
        structured rows as a list of dictionaries, plus quality metrics.

        After receiving results, YOU must:
        1. Review the quality metrics (parse_success_rate, avg_confidence if available)
        2. Map the parsed column names to your extraction schema fields
        3. Apply any required formatting (dates, numbers, etc.)
        4. Use extraction_tool or apply_json_patches to store the mapped data
        5. If quality is poor (parse_success_rate < 0.90), consider using LLM
           extraction for those specific rows/cells instead

        Args:
            table_text: The text containing Markdown table(s) to parse.
                Can be the full document text or a specific table section.
                The tool will automatically detect table boundaries.
            expected_columns: Optional list of column names you expect in the table.
                Used for validation - the tool reports matched/missing/extra columns.
            table_index: Optional index to select a specific table when multiple
                tables are found (0-based). If None, returns the first table at
                top level with all tables in 'tables' list.

        Returns:
            Dict with parsed data and quality metrics:
            - status: "success" or "error" (Bedrock ToolResult-valid). The
              richer parser status ("success"/"no_tables_found"/"error") is
              preserved under ``parser_status`` — read that, not ``status``,
              for the semantic outcome.
            - parser_status: "success", "no_tables_found", or "error"
            - columns: list of column header strings
            - rows: list of dicts mapping column names to cell values
            - row_count: number of data rows
            - table_count: number of tables found
            - quality: dict with parse_success_rate, avg_confidence (if available), etc.
            - warnings: list of any parsing issues encountered
            - tables: list of all parsed tables (when multiple found)
        """
        result = parse_markdown_tables(
            text=table_text,
            expected_columns=expected_columns,
            confidence_data=merged_confidence,
            max_empty_line_gap=max_empty_line_gap,
            auto_merge_adjacent_tables=auto_merge_adjacent_tables,
        )

        # Add intelligent warnings for potential data completeness issues
        if result.get("status") == "success":
            warnings = result.get("warnings", [])
            quality = result.get("quality", {})

            # Warn if multiple tables with identical columns detected (potential split)
            if result.get("table_count", 0) > 1:
                tables_list = result.get("tables", [])
                if tables_list:
                    # Check if all tables have identical columns
                    column_sets = [tuple(t["columns"]) for t in tables_list]
                    unique_column_sets = set(column_sets)

                    if len(unique_column_sets) == 1:
                        # All tables have identical columns - likely fragments
                        total_rows = sum(t["row_count"] for t in tables_list)
                        warnings.append(
                            f"⚠️ NOTICE: Found {result['table_count']} separate tables with "
                            f"IDENTICAL columns ({total_rows} total rows). These are likely "
                            f"fragments of a single table split by OCR artifacts. "
                            f"Make sure to extract from ALL tables to avoid data loss."
                        )
                    elif len(unique_column_sets) < len(tables_list):
                        # Some tables have matching columns
                        warnings.append(
                            f"NOTICE: Found {result['table_count']} tables with "
                            f"{len(unique_column_sets)} unique column structures. "
                            f"Some tables may be fragments - verify completeness."
                        )

            # Warn if parse success rate is low (potential data loss)
            parse_rate = quality.get("parse_success_rate", 1.0)
            if parse_rate < 0.95:
                warnings.append(
                    f"⚠️ NOTICE: Parse success rate ({parse_rate:.1%}) suggests "
                    f"potential data quality issues. Verify row count and content completeness."
                )

            # Inform about gap recovery (successful artifact handling)
            tables_list = result.get("tables", [])
            total_gaps = sum(t.get("gaps_recovered", 0) for t in tables_list)
            if total_gaps > 0:
                warnings.append(
                    f"ℹ️ INFO: Successfully recovered {total_gaps} gap(s) in table data "
                    f"(empty lines/missing pipes). Data should be complete."
                )

            # Update warnings in result
            result["warnings"] = warnings

        # If a specific table was requested, promote it to top level
        if (
            table_index is not None
            and result.get("tables")
            and 0 <= table_index < len(result["tables"])
        ):
            selected = result["tables"][table_index]
            result["columns"] = selected["columns"]
            result["rows"] = selected["rows"]
            result["row_count"] = selected["row_count"]
            result["quality"] = selected["quality"]

        logger.info(
            "parse_table tool invoked",
            extra={
                "status": result.get("status"),
                "table_count": result.get("table_count", 0),
                "row_count": result.get("row_count", 0),
                "quality": result.get("quality", {}),
                "warnings": result.get("warnings", []),
            },
        )

        # Store parsed result in agent state for map_table_to_schema tool
        if agent is not None:
            agent.state.set("last_parse_table_result", result)

        # Record usage stats in agent state for observability
        if agent is not None:
            quality = result.get("quality", {})
            stats = {
                "tables_parsed": result.get("table_count", 0),
                "rows_parsed": result.get("row_count", 0),
                "parse_success_rate": quality.get("parse_success_rate", 0.0),
                "avg_confidence": quality.get("avg_confidence"),
                "confidence_available": quality.get("confidence_available", False),
            }
            # Accumulate stats across multiple invocations
            existing_stats = agent.state.get("table_parsing_stats")
            if existing_stats:
                stats["tables_parsed"] += existing_stats.get("tables_parsed", 0)
                stats["rows_parsed"] += existing_stats.get("rows_parsed", 0)
                stats["invocation_count"] = (
                    existing_stats.get("invocation_count", 0) + 1
                )
            else:
                stats["invocation_count"] = 1
            agent.state.set("table_parsing_stats", stats)

        # Build a COMPACT, model-visible summary and attach it as the tool
        # result ``content``. The full parsed rows are already persisted in
        # agent state (``last_parse_table_result``) for map_table_to_schema, so
        # the model does not need them echoed back — and echoing them is exactly
        # what caused input tokens to balloon (the dense rows were re-sent every
        # subsequent turn). For small tables we still inline all rows so manual /
        # fallback extraction (parse_table + extraction_tool) keeps working; for
        # large tables we inline only head+tail samples + stats.
        row_count = result.get("row_count", 0)
        rows = result.get("rows", []) or []
        visible_rows = _build_row_summary(
            rows, row_count, rows_key="rows", next_tool="map_table_to_schema"
        )
        summary: dict[str, Any] = {
            "status": result.get("status"),
            "table_count": result.get("table_count", 0),
            "columns": result.get("columns", []),
            "row_count": row_count,
            "quality": result.get("quality", {}),
            "warnings": result.get("warnings", []),
            **visible_rows,
        }

        # Return dict carries BOTH the compact ``content`` (what Strands re-sends
        # to the model every turn) AND the full top-level keys (rows/tables/...)
        # used by state persistence and the pipeline. Bedrock's request formatter
        # transmits only ``content`` + toolUseId + status, dropping the extra
        # keys — so the dense rows never re-enter the conversation.
        #
        # Strands only treats the return as a pre-formatted ToolResult (skipping
        # the str(result) inline path) when the dict has both ``status`` and
        # ``content``, and Bedrock only accepts a ToolResult status of
        # "success"/"error". The parser's richer statuses ("no_tables_found")
        # must be folded to "success"/"error" for the RETURN.
        #
        # But ``result`` is the SAME object already stored in agent state
        # (``last_parse_table_result``, above), so we must NOT clobber its
        # semantic status in place. Preserve the parser status under
        # ``parser_status`` first, then set the Bedrock-facing ``status``. State
        # (and the documented contract) thus retains the original meaning.
        parser_status = result.get("status")
        result.setdefault("parser_status", parser_status)
        result["content"] = _compact_content(summary)
        result["status"] = "error" if parser_status == "error" else "success"
        return result

    return parse_table


def create_map_table_to_schema_tool():
    """
    Create a map_table_to_schema tool that mechanically transforms
    pre-parsed table rows into schema-compliant JSON using a column mapping.

    This eliminates the need for the LLM to generate large JSON payloads
    row-by-row.  The LLM's job is reduced to providing a small mapping
    dict, and this tool does the bulk transformation instantly.

    Returns:
        A Strands @tool function
    """

    @tool
    def map_table_to_schema(
        column_mapping: dict[str, str],
        static_fields: dict[str, str] | None = None,
        value_transforms: dict[str, str] | None = None,
        agent: Any = None,
    ) -> dict[str, Any]:
        """
        Transform pre-parsed table rows into schema-compliant objects using
        a column-to-field mapping.  This is MUCH faster than generating JSON
        row-by-row with the LLM.

        IMPORTANT: This tool requires that parse_table was called first.
        It reads the parsed rows from your agent state (stored automatically
        by parse_table).

        Workflow:
        1. Call parse_table on the document text (parses all tables instantly)
        2. Review the columns returned by parse_table
        3. Call THIS tool with a column_mapping that maps table columns to
           schema fields, plus any static_fields for values not in the table
        4. This tool returns ALL rows transformed — ready for extraction_tool

        Args:
            column_mapping: Maps table column names to schema field names.
                Example: {"Symbol": "symbol_cusip", "Description": "security_description",
                          "Shares": "quantity", "Price": "price", "Market Value": "value"}
                Column name matching is case-insensitive and whitespace-tolerant.
            static_fields: Optional dict of field names and constant values to add
                to every row.  Example: {"account_number": "XXXX-XXXX",
                "cost_basis": "N/A", "total_pages_in_doc": "25"}
            value_transforms: Optional dict of field names and simple transform rules.
                Supported transforms:
                - "strip_currency": removes $, commas  (e.g. "$1,234.56" -> "1234.56")
                - "strip_whitespace": removes all internal whitespace
                - "lowercase" / "uppercase": case conversion
                - "date_to_iso": normalize an unambiguous date to YYYY-MM-DD
                  (e.g. "15/03/2024", "2024/03/15", "March 15, 2024"). An
                  all-numeric date whose day/month order is genuinely ambiguous
                  ("01/02/2024") is REFUSED and reported, never guessed.
                - "date_to_iso_mdy" / "date_to_iso_dmy": same, but state which
                  reading the source documents use so ambiguous values convert
                  too. Use these whenever the schema declares `format: date`
                  and the page shows some other order — the schema's date type
                  accepts ISO only, so unconverted rows fail finalize and you
                  would otherwise have to re-emit every row by hand.
                Example: {"Amount": "strip_currency", "Date": "date_to_iso_mdy"}

        Returns:
            Dict with:
            - status: "success" or "error"
            - mapped_rows: list of dicts with schema field names as keys
            - row_count: number of rows transformed
            - unmapped_columns: table columns that were NOT in column_mapping
            - sample_row: first mapped row (for quick LLM review)
            - warnings: any issues encountered
        """
        warnings: list[str] = []

        # Get parsed table data from agent state
        if agent is None:
            return {
                "status": "error",
                "mapped_rows": [],
                "row_count": 0,
                "warnings": ["Agent state not available"],
            }

        parsed_data = agent.state.get("last_parse_table_result")
        if not parsed_data:
            return {
                "status": "error",
                "mapped_rows": [],
                "row_count": 0,
                "warnings": [
                    "No parsed table data found. Call parse_table first, "
                    "then call this tool."
                ],
            }

        # Get all rows from all tables (they may have been merged already)
        all_rows: list[dict[str, str]] = []
        all_columns: list[str] = []
        tables = parsed_data.get("tables", [])
        if tables:
            for table in tables:
                if not all_columns:
                    all_columns = table.get("columns", [])
                all_rows.extend(table.get("rows", []))
        else:
            all_columns = parsed_data.get("columns", [])
            all_rows = parsed_data.get("rows", [])

        if not all_rows:
            return {
                "status": "error",
                "mapped_rows": [],
                "row_count": 0,
                "warnings": ["No rows found in parsed table data"],
            }

        # Build case-insensitive column mapping lookup
        col_map: dict[str, str] = {}
        col_name_normalized = {c.strip().lower(): c for c in all_columns}

        for table_col, schema_field in column_mapping.items():
            normalized = table_col.strip().lower()
            if normalized in col_name_normalized:
                col_map[col_name_normalized[normalized]] = schema_field
            else:
                # Try fuzzy: check if table_col is a substring of any column
                matched = False
                for orig_col in all_columns:
                    if normalized in orig_col.lower() or orig_col.lower() in normalized:
                        col_map[orig_col] = schema_field
                        matched = True
                        break
                if not matched:
                    warnings.append(
                        f"Column '{table_col}' not found in table columns: "
                        f"{all_columns}"
                    )

        # Identify unmapped columns
        unmapped = [c for c in all_columns if c not in col_map]
        if unmapped:
            warnings.append(f"Unmapped columns (not in column_mapping): {unmapped}")

        # Define value transform functions
        transform_refusals: dict[str, str] = {}
        warned_unknown: set[str] = set()

        def _apply_transform(value: str, transform: str, field: str = "") -> str:
            if not value or not transform:
                return value
            if transform == "strip_currency":
                return value.replace("$", "").replace(",", "").strip()
            elif transform == "strip_whitespace":
                return "".join(value.split())
            elif transform == "lowercase":
                return value.lower()
            elif transform == "uppercase":
                return value.upper()
            elif transform in DATE_TRANSFORMS:
                converted, reason = _to_iso_date(value, DATE_TRANSFORMS[transform])
                if reason and field not in transform_refusals:
                    transform_refusals[field] = f"{value!r}: {reason}"
                return converted
            if transform not in warned_unknown:
                warned_unknown.add(transform)
                warnings.append(
                    f"Unknown value transform {transform!r} ignored. Supported: "
                    + ", ".join(sorted(_SUPPORTED_TRANSFORMS))
                )
            return value

        transforms = value_transforms or {}
        statics = static_fields or {}

        def _detect_and_split_merged_row(
            row: dict[str, str],
        ) -> list[dict[str, str]]:
            """Detect rows where two entries are merged on one line.

            Common OCR artifact at page boundaries where the last row of one
            page and first row of the next get concatenated.  Detection:
            look for columns with two dollar amounts, two quantities, etc.
            """
            import re

            # Find columns with multiple dollar values (e.g. "$57.9 $55.11")
            multi_dollar = re.compile(r"\$[\d,.]+\s+\$[\d,.]+")
            # Find columns with multiple plain numbers (e.g. "457 889")
            multi_number = re.compile(r"^[\d,.]+\s+[\d,.]+$")

            # Check mapped fields for signs of merging
            merge_signals = 0
            for val in row.values():
                val_str = str(val).strip()
                if multi_dollar.search(val_str) or multi_number.match(val_str):
                    merge_signals += 1

            # Need at least 2 columns showing merge pattern
            if merge_signals < 2:
                return [row]

            # Attempt to split: take the first and second value from each field
            row1: dict[str, str] = {}
            row2: dict[str, str] = {}
            split_success = True

            for field, val in row.items():
                val_str = str(val).strip()

                if multi_dollar.search(val_str):
                    # Split dollar values: "$57.9 $55.11" → "$57.9", "$55.11"
                    parts = re.findall(r"\$[\d,.]+", val_str)
                    if len(parts) >= 2:
                        row1[field] = parts[0]
                        row2[field] = parts[1]
                        continue

                if multi_number.match(val_str):
                    # Split plain numbers: "457 889" → "457", "889"
                    parts = val_str.split()
                    if len(parts) >= 2:
                        row1[field] = parts[0]
                        row2[field] = parts[1]
                        continue

                # For text fields, try splitting by multiple words
                # e.g. "GKE CORPORATION DVE CORPORATION" → split at middle
                words = val_str.split()
                if len(words) >= 4:
                    mid = len(words) // 2
                    row1[field] = " ".join(words[:mid])
                    row2[field] = " ".join(words[mid:])
                elif len(words) == 2:
                    # Could be "GKE DVE" (two symbols)
                    row1[field] = words[0]
                    row2[field] = words[1]
                else:
                    # Can't split this field — use same value for both
                    row1[field] = val_str
                    row2[field] = val_str
                    split_success = False

            if not split_success:
                # Couldn't split all fields cleanly
                return [row]

            return [row1, row2]

        # Transform all rows (with automatic merged-row splitting)
        mapped_rows: list[dict[str, str]] = []
        merged_splits = 0
        for row in all_rows:
            mapped_row: dict[str, str] = {}

            # Map table columns to schema fields
            for table_col, schema_field in col_map.items():
                val = row.get(table_col, "")
                if schema_field in transforms:
                    val = _apply_transform(val, transforms[schema_field], schema_field)
                mapped_row[schema_field] = val

            # Add static fields
            for field_name, field_value in statics.items():
                mapped_row[field_name] = field_value

            # Detect and split merged rows
            split_rows = _detect_and_split_merged_row(mapped_row)
            if len(split_rows) > 1:
                merged_splits += 1
                warnings.append(f"Auto-split merged row into {len(split_rows)} entries")

            # Filter out rows where all mapped column values are empty.
            # This removes phantom rows from non-matching tables (e.g. an
            # account_summary table whose columns don't match the transaction
            # column_mapping — every mapped value comes back as "").
            mapped_col_fields = set(col_map.values())
            split_rows = [
                r
                for r in split_rows
                if any(str(r.get(f, "")).strip() for f in mapped_col_fields)
            ]

            mapped_rows.extend(split_rows)

        # Report refused value transforms. A refused date is left exactly as
        # parsed (never guessed), so the agent must know which column still
        # carries a non-schema format rather than discovering it at finalize.
        for field, detail in list(transform_refusals.items())[:MAX_TRANSFORM_WARNINGS]:
            warnings.append(
                f"Date transform refused for {field!r} ({detail}); value left "
                f"unchanged. Pass date_to_iso_mdy or date_to_iso_dmy to state "
                f"the reading this document uses."
            )
        if len(transform_refusals) > MAX_TRANSFORM_WARNINGS:
            warnings.append(
                f"...and {len(transform_refusals) - MAX_TRANSFORM_WARNINGS} more "
                f"field(s) with refused date transforms."
            )

        # Accumulate mapped rows in agent state for finalize_table_extraction
        # (supports multiple calls for chunked processing)
        existing_mapped = agent.state.get("mapped_table_rows")
        if existing_mapped and existing_mapped.get("mapped_rows"):
            all_mapped = existing_mapped["mapped_rows"] + mapped_rows
        else:
            all_mapped = mapped_rows
        agent.state.set(
            "mapped_table_rows",
            {
                "mapped_rows": all_mapped,
                "row_count": len(all_mapped),
                "columns_mapped": list(col_map.values()),
            },
        )

        # Update agent state stats
        existing_stats = agent.state.get("table_parsing_stats") or {}
        existing_stats["rows_mapped"] = len(mapped_rows)
        existing_stats["mapping_used"] = True
        agent.state.set("table_parsing_stats", existing_stats)

        logger.info(
            "map_table_to_schema completed",
            extra={
                "row_count": len(mapped_rows),
                "columns_mapped": len(col_map),
                "unmapped_columns": unmapped,
                "static_fields": list(statics.keys()),
            },
        )

        result = {
            "status": "success",
            "mapped_rows": mapped_rows,
            "row_count": len(mapped_rows),
            "columns_mapped": list(col_map.values()),
            "unmapped_columns": unmapped,
            "warnings": warnings,
        }

        # Include a sample row for the LLM to review
        if mapped_rows:
            result["sample_first_row"] = mapped_rows[0]
            result["sample_last_row"] = mapped_rows[-1]

        # Compact, model-visible content. The full ``mapped_rows`` are persisted
        # in agent state (``mapped_table_rows``) and read directly by
        # finalize_table_extraction, so the model never needs them echoed — and
        # echoing them is exactly the re-transmission that ballooned input
        # tokens. Small results still inline every row (cheap, and preserves the
        # fallback where the model reviews then patches); large results echo only
        # samples + counts. Bedrock transmits only this ``content`` (the extra
        # top-level keys are dropped from the request), so the dense rows stay
        # out of the growing conversation.
        row_count = len(mapped_rows)
        visible_rows = _build_row_summary(
            mapped_rows,
            row_count,
            rows_key="mapped_rows",
            next_tool="finalize_table_extraction",
        )
        summary: dict[str, Any] = {
            "status": "success",
            "row_count": row_count,
            "columns_mapped": list(col_map.values()),
            "unmapped_columns": unmapped,
            "warnings": warnings,
            **visible_rows,
        }
        result["content"] = _compact_content(summary)
        return result

    return map_table_to_schema
