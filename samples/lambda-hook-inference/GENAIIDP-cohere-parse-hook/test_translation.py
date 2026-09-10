#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Offline unit tests for the Cohere Parse -> Textract translation in index.py.

These tests do NOT call the Cohere API or AWS — they validate the pure
translation logic (HTML->Markdown table conversion, block flattening, geometry
normalization, and metering). Run with:

    python test_translation.py
or
    pytest test_translation.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402, I001


# ---------------------------------------------------------------------------
# HTML table -> Markdown pipe table
# ---------------------------------------------------------------------------


def test_html_table_with_header():
    html = (
        "<table><tr><th>Date</th><th>Amount</th></tr>"
        "<tr><td>2026-01-02</td><td>$10.00</td></tr>"
        "<tr><td>2026-01-03</td><td>$20.00</td></tr></table>"
    )
    md = index.html_table_to_markdown(html)
    assert md.split("\n") == [
        "| Date | Amount |",
        "|---|---|",
        "| 2026-01-02 | $10.00 |",
        "| 2026-01-03 | $20.00 |",
    ]
    print("test_html_table_with_header: PASS")


def test_html_table_without_header_synthesizes_one():
    """No <th> means no header row — the first data row must not be eaten."""
    html = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
    md = index.html_table_to_markdown(html)
    lines = md.split("\n")
    assert lines[0] == "|  |  |"
    assert lines[1] == "|---|---|"
    # Both data rows survive.
    assert lines[2:] == ["| a | b |", "| c | d |"]
    print("test_html_table_without_header_synthesizes_one: PASS")


def test_html_table_thead_with_td_cells_is_the_header():
    """Cohere marks its header with <thead> containing plain <td> cells.

    Keying on <th> alone would leave the real header as a body row under a
    blank header, and the deterministic table parser would then read every
    column name as an empty string.
    """
    html = (
        "<table><thead><tr><td>DATE</td><td>AMOUNT ($)</td></tr></thead>"
        "<tbody><tr><td>06/09</td><td>150.00</td></tr></tbody></table>"
    )
    md = index.html_table_to_markdown(html)
    assert md.split("\n") == [
        "| DATE | AMOUNT ($) |",
        "|---|---|",
        "| 06/09 | 150.00 |",
    ]
    print("test_html_table_thead_with_td_cells_is_the_header: PASS")


def test_html_table_colspan_and_ragged_rows():
    html = (
        "<table><tr><th>H1</th><th>H2</th><th>H3</th></tr>"
        '<tr><td colspan="2">wide</td><td>x</td></tr>'
        "<tr><td>only</td></tr></table>"
    )
    md = index.html_table_to_markdown(html)
    lines = md.split("\n")
    # colspan=2 keeps columns aligned with the text in the FIRST spanned column;
    # repeating it would show the extraction model one label as several values.
    assert lines[2] == "| wide |  | x |"
    # short row is padded to full width
    assert lines[3] == "| only |  |  |"
    print("test_html_table_colspan_and_ragged_rows: PASS")


def test_table_description_is_never_emitted():
    """Parse's table `description` is model-written prose, not transcription.

    On a live bank statement it restated the account number as 0035258015143
    where the table said 003525801543, so injecting it would hand fabricated
    values to extraction as if they had been read off the page.
    """
    resp = {
        "pages": [
            {
                "type": "blocks",
                "index": 0,
                "blocks": [
                    {
                        "type": "table",
                        "table": {
                            "html": (
                                "<table><thead><tr><td>ACCOUNT</td></tr></thead>"
                                "<tbody><tr><td>003525801543</td></tr></tbody></table>"
                            ),
                            "description": (
                                "The Checking account (0035258015143) has a "
                                "balance of $5,657.47."
                            ),
                        },
                    }
                ],
            }
        ],
        "meta": {"billed_units": {"pages": 1}},
    }
    text, _, _ = index.build_textract_response(resp)
    assert "003525801543" in text
    assert "0035258015143" not in text
    assert "balance of" not in text
    print("test_table_description_is_never_emitted: PASS")


def test_html_table_escapes_pipes_and_collapses_whitespace():
    html = "<table><tr><td>a | b</td><td>multi\n  line<br>text</td></tr></table>"
    md = index.html_table_to_markdown(html)
    assert "a \\| b" in md
    assert "multi line text" in md
    print("test_html_table_escapes_pipes_and_collapses_whitespace: PASS")


def test_nested_table_falls_back_to_html():
    """Markdown cannot represent a nested table, so keep the original HTML."""
    html = "<table><tr><td><table><tr><td>inner</td></tr></table></td></tr></table>"
    assert index.html_table_to_markdown(html) == html
    print("test_nested_table_falls_back_to_html: PASS")


def test_non_table_html_is_preserved():
    html = "<p>no tables here</p>"
    assert index.html_table_to_markdown(html) == html
    print("test_non_table_html_is_preserved: PASS")


# ---------------------------------------------------------------------------
# Full response translation ("blocks" output format)
# ---------------------------------------------------------------------------

BLOCKS_RESPONSE = {
    "id": "req-1",
    "pages": [
        {
            "type": "blocks",
            "index": 0,
            "blocks": [
                {"type": "text", "text": {"content": "# Invoice\nAccount: 12345"}},
                {
                    "type": "table",
                    "table": {
                        "type": "html",
                        "html": (
                            "<table><tr><th>Item</th><th>Cost</th></tr>"
                            "<tr><td>Widget</td><td>$9.00</td></tr></table>"
                        ),
                        "title": "Line items",
                        "bounding_box": {
                            "top_left_x": 100,
                            "top_left_y": 400,
                            "bottom_right_x": 900,
                            "bottom_right_y": 700,
                        },
                        "bounding_box_normalized": {
                            "top_left_x": 0.1,
                            "top_left_y": 0.2,
                            "bottom_right_x": 0.9,
                            "bottom_right_y": 0.5,
                        },
                    },
                },
                {
                    "type": "image",
                    "image": {
                        "id": "img-1",
                        "description": "company logo",
                        "category": "logo",
                        "bounding_box_normalized": {
                            "top_left_x": 0.0,
                            "top_left_y": 0.0,
                            "bottom_right_x": 0.2,
                            "bottom_right_y": 0.1,
                        },
                    },
                },
            ],
        }
    ],
    "meta": {"api_version": {"version": "2"}, "billed_units": {"pages": 1}},
}


def test_blocks_response_translation():
    text, textract, pages = index.build_textract_response(BLOCKS_RESPONSE)
    assert pages == 1

    # Markdown output: text, then the converted pipe table, then the figure.
    assert "# Invoice" in text
    assert "| Item | Cost |" in text
    assert "| Widget | $9.00 |" in text
    assert "<table>" not in text  # HTML was converted
    assert "![logo: company logo](img-1)" in text

    blocks = textract["Blocks"]
    assert blocks[0]["BlockType"] == "PAGE"
    line_blocks = [b for b in blocks if b["BlockType"] == "LINE"]
    assert [b["Text"] for b in line_blocks] == [
        "# Invoice",
        "Account: 12345",
        "**Line items**",
        "| Item | Cost |",
        "|---|---|",
        "| Widget | $9.00 |",
        "![logo: company logo](img-1)",
    ]

    # Cohere Parse returns NO confidence scores — no block may claim one.
    assert all("Confidence" not in b for b in blocks)

    # Text lines have no geometry (Parse gives boxes for tables/figures only).
    assert "Geometry" not in line_blocks[0]

    # Every line derived from the table shares the table's normalized box, which
    # the IDP OCR service flags as paragraph-level geometry.
    table_lines = line_blocks[2:6]
    boxes = {tuple(sorted(b["Geometry"]["BoundingBox"].items())) for b in table_lines}
    assert len(boxes) == 1, "table lines should share one bounding box"
    bbox = table_lines[0]["Geometry"]["BoundingBox"]
    assert abs(bbox["Left"] - 0.1) < 1e-9
    assert abs(bbox["Top"] - 0.2) < 1e-9
    assert abs(bbox["Width"] - 0.8) < 1e-9
    assert abs(bbox["Height"] - 0.3) < 1e-9

    figure_line = line_blocks[-1]
    assert figure_line["Geometry"]["BoundingBox"]["Width"] == 0.2

    assert textract["ModelId"] == index.COHERE_PARSE_MODEL
    print("test_blocks_response_translation: PASS")


def test_convert_html_tables_disabled_keeps_html():
    original = index.CONVERT_HTML_TABLES
    index.CONVERT_HTML_TABLES = False
    try:
        text, _, _ = index.build_textract_response(BLOCKS_RESPONSE)
        assert "<table>" in text
        assert "| Item | Cost |" not in text
    finally:
        index.CONVERT_HTML_TABLES = original
    print("test_convert_html_tables_disabled_keeps_html: PASS")


def test_markdown_output_format():
    """The 'markdown' output format returns one content string per page."""
    resp = {
        "pages": [
            {
                "type": "markdown",
                "index": 0,
                "markdown": {
                    "content": "# Title\n\nBody text",
                    "images": [
                        {
                            "id": "img-1",
                            "description": "chart",
                            "category": "other",
                            "bounding_box_normalized": {
                                "top_left_x": 0.1,
                                "top_left_y": 0.1,
                                "bottom_right_x": 0.5,
                                "bottom_right_y": 0.4,
                            },
                        }
                    ],
                },
            }
        ],
        "meta": {"billed_units": {"pages": 1}},
    }
    text, textract, pages = index.build_textract_response(resp)
    assert text == "# Title\n\nBody text"
    assert pages == 1
    lines = [b["Text"] for b in textract["Blocks"] if b["BlockType"] == "LINE"]
    assert lines == ["# Title", "Body text"]
    # Images are embedded in the content, so there is nothing to attach a box to.
    assert all(
        "Geometry" not in b for b in textract["Blocks"] if b["BlockType"] == "LINE"
    )
    print("test_markdown_output_format: PASS")


def test_flat_block_shapes_are_tolerated():
    """The docs are loose about nesting, so un-nested payloads must work too."""
    resp = {
        "pages": [
            {
                "type": "blocks",
                "index": 0,
                "blocks": [
                    {"type": "text", "content": "flat content"},
                    {
                        "type": "image",
                        "id": "img-2",
                        "description": "signature of taxpayer",
                        "category": "signature",
                    },
                ],
            }
        ]
    }
    text, textract, pages = index.build_textract_response(resp)
    assert "flat content" in text
    assert "![signature: signature of taxpayer](img-2)" in text
    # No billed_units -> fall back to the page count
    assert pages == 1
    print("test_flat_block_shapes_are_tolerated: PASS")


def test_missing_and_degenerate_geometry_is_dropped():
    resp = {
        "pages": [
            {
                "type": "blocks",
                "index": 0,
                "blocks": [
                    {
                        "type": "table",
                        "table": {
                            "html": "<table><tr><td>x</td></tr></table>",
                            # zero-area box -> not usable geometry
                            "bounding_box_normalized": {
                                "top_left_x": 0.5,
                                "top_left_y": 0.5,
                                "bottom_right_x": 0.5,
                                "bottom_right_y": 0.5,
                            },
                        },
                    },
                    {
                        "type": "table",
                        "table": {
                            "html": "<table><tr><td>y</td></tr></table>",
                            # pixel-only box: no page dimensions, so unusable
                            "bounding_box": {
                                "top_left_x": 10,
                                "top_left_y": 10,
                                "bottom_right_x": 90,
                                "bottom_right_y": 90,
                            },
                        },
                    },
                ],
            }
        ],
        "meta": {"billed_units": {"pages": 1}},
    }
    _, textract, _ = index.build_textract_response(resp)
    lines = [b for b in textract["Blocks"] if b["BlockType"] == "LINE"]
    assert lines, "expected the table content to survive"
    assert all("Geometry" not in b for b in lines)
    print("test_missing_and_degenerate_geometry_is_dropped: PASS")


def test_empty_pages():
    text, textract, pages = index.build_textract_response({"pages": []})
    assert text == ""
    assert textract["Blocks"] == []
    assert pages == 0
    print("test_empty_pages: PASS")


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


class _FakeHTTPError:
    def __init__(self, retry_after):
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


def test_retry_after_parsing():
    assert index._retry_after_seconds(_FakeHTTPError("5")) == 5.0
    assert index._retry_after_seconds(_FakeHTTPError(None)) is None
    assert index._retry_after_seconds(_FakeHTTPError("not-a-number")) is None
    # Absurd values are ignored so a bad header cannot stall the Lambda.
    assert index._retry_after_seconds(_FakeHTTPError("3600")) is None
    print("test_retry_after_parsing: PASS")


if __name__ == "__main__":
    test_html_table_with_header()
    test_html_table_without_header_synthesizes_one()
    test_html_table_thead_with_td_cells_is_the_header()
    test_html_table_colspan_and_ragged_rows()
    test_table_description_is_never_emitted()
    test_html_table_escapes_pipes_and_collapses_whitespace()
    test_nested_table_falls_back_to_html()
    test_non_table_html_is_preserved()
    test_blocks_response_translation()
    test_convert_html_tables_disabled_keeps_html()
    test_markdown_output_format()
    test_flat_block_shapes_are_tolerated()
    test_missing_and_degenerate_geometry_is_dropped()
    test_empty_pages()
    test_retry_after_parsing()
    print("\nAll translation tests passed.")
