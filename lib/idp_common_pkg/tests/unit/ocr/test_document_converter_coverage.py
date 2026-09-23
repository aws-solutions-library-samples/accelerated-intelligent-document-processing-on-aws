# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`document_converter` turns a non-PDF upload into the page images and page text
the rest of the OCR path consumes, and its failures are silent by construction.

Every public entry point here (`convert_text_to_pages`, `convert_csv_to_pages`,
`convert_excel_to_pages`, `convert_word_to_pages`) is wrapped in a bare
`except Exception` that answers failure with one white page and a short message.
So does nearly every private helper. Nothing in this module raises for the caller
to see, apart from the deliberate `UnsupportedLegacyFormatError` covered in
`test_legacy_doc_fails_loudly.py`. The consequence is that a dropped row, a
truncated table, a page rendered off the edge of its own canvas and a document
split into the wrong number of pages all look identical from outside: the pipeline
succeeds, classification and extraction run, and the answer is quietly wrong.

That shaped the tests in three ways.

**Conservation is asserted, not sampled.** Where the module distributes content
across pages — text lines, CSV rows, markdown lines, Word elements — the test
reconstructs the whole output and compares it against the whole input, rather than
checking that a probe string appears somewhere. A page-count check alone passes
with the body of the loop deleted, and an "is `Widget` in the text" check passes
with every other row dropped. Several tests here use a deliberately multi-page
input for the same reason: a single-page fixture is satisfied by a pagination loop
that only ever emits its first iteration.

**Images are inspected, not counted.** A returned `bytes` object proves nothing:
`_create_empty_page` returns valid JPEG bytes too, and it is what every failure
handler returns. So the rendering tests open the JPEG with Pillow and measure it —
canvas size, whether any ink was laid down at all, where the ink's bounding box
starts horizontally (alignment), how far down it extends (underlines, and clipping
past the bottom margin), how many dark pixels there are (emboldening), and the
fill colour of a table's header band. Those are the properties a model reading the
page image actually depends on, and they are the ones a mocked `ImageDraw` cannot
speak to.

**Fixtures are real files wherever a real file is cheap.** The Word tests build
genuine `.docx` packages with `python-docx` — including real embedded PNGs made
with Pillow, real explicit page breaks, real tables and a real `<w:sectPr>` — and
the Excel tests build genuine `.xlsx` workbooks with `pandas`/`openpyxl`. This is
not only fidelity: it is what distinguishes a passing test from a true one. The
header-flag test below documents a case where the previously committed
`MagicMock`-based test reports the opposite of what a real `.docx` table does,
because `MagicMock`s held in a plain list compare equal by identity and
`python-docx` `_Row` objects do not.

Tests whose name or docstring says "pins current behaviour" are characterization
tests over defects found while writing this module. They assert what the code does
today so that a change to it is visible; they are not statements that the
behaviour is correct. Each names the consequence.
"""

from __future__ import annotations

import io
import os
from typing import Any, Callable
from unittest.mock import patch

import pandas as pd
import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont

from idp_common.ocr import document_converter as dc_module
from idp_common.ocr.document_converter import DocumentConverter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures and helpers, kept local to this module on purpose
# ---------------------------------------------------------------------------

# 72 dpi keeps the rendered canvases small (612x792) so the image inspection
# below stays fast. Tests that care about the production default say so.
SMALL_DPI = 72


def _converter(dpi: int = SMALL_DPI) -> DocumentConverter:
    return DocumentConverter(dpi=dpi)


def _open(img_bytes: bytes) -> Image.Image:
    """Open rendered page bytes, failing the test if they are not a real image."""
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    return img


# Anything below this grey counts as ink; JPEG ringing around glyph edges keeps
# "white" a few counts short of 255, so a strict == 255 test would report ink
# everywhere.
_INK_THRESHOLD = 200
_INK_LUT = [255 if value < _INK_THRESHOLD else 0 for value in range(256)]


def _ink_mask(img: Image.Image) -> Image.Image:
    return img.convert("L").point(_INK_LUT)


def _ink_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of everything darker than near-white, or None if blank."""
    return _ink_mask(img).getbbox()


def _ink_pixels(img: Image.Image) -> int:
    """Count of pixels darker than near-white.

    The mask holds only 0 and 255, so every non-zero histogram bucket is ink.
    """
    return sum(_ink_mask(img).histogram()[1:])


def _para(
    text: str,
    *,
    is_heading: bool = False,
    heading_level: int = 0,
    alignment: str = "left",
    runs: list[dict[str, Any]] | None = None,
    space_before: int = 3,
    space_after: int = 3,
) -> dict[str, Any]:
    """A paragraph element dict of the shape `_extract_word_formatting` emits."""
    if runs is None:
        runs = [
            {
                "text": text,
                "bold": False,
                "italic": False,
                "underline": False,
                "font_size": None,
                "font_name": None,
            }
        ]
    return {
        "type": "paragraph",
        "text": text,
        "style": "Heading 1" if is_heading else "Normal",
        "is_heading": is_heading,
        "heading_level": heading_level,
        "alignment": alignment,
        "runs": runs,
        "space_before": space_before,
        "space_after": space_after,
    }


def _png_bytes(width: int = 120, height: int = 60, colour: str = "black") -> bytes:
    """A genuine PNG, so `python-docx` can read its dimensions when embedding."""
    img = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(img).rectangle([2, 2, width - 3, height - 3], fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _docx_bytes(build: Callable[[Any], None]) -> bytes:
    """Build a real .docx package in memory and return its bytes."""
    document = Document()
    build(document)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _named_fonts_unavailable() -> Callable[..., Any]:
    """A `truetype` stand-in that fails for a named or path-addressed face but
    still lets `ImageFont.load_default()` work.

    `load_default()` calls `truetype()` on an in-memory stream, so patching
    `truetype` with a blanket `side_effect` breaks the very fallback under test
    and the OSError escapes to the outer handler instead.
    """
    real_truetype = ImageFont.truetype

    def truetype(font: Any = None, size: int = 10, *args: Any, **kwargs: Any) -> Any:
        if isinstance(font, (str, bytes, os.PathLike)):
            raise OSError("cannot open resource")
        return real_truetype(font, size, *args, **kwargs)

    return truetype


def _xlsx_bytes(sheets: list[tuple[str, pd.DataFrame]]) -> bytes:
    """Build a real .xlsx workbook in memory and return its bytes."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


class TestPlainTextPagination:
    """`convert_text_to_pages` is the fallback every other converter degrades to,
    so losing content here loses it for every format."""

    def test_every_line_of_a_multi_page_document_survives_in_order(self):
        """The whole point of the pagination loop. A 200-line document at 72 dpi
        does not fit on one page, so this fails if the loop emits only its first
        chunk, drops a chunk, or reorders them — none of which a single-page
        fixture can detect."""
        converter = _converter()
        lines = [f"LINE{i:04d}" for i in range(200)]
        pages = converter.convert_text_to_pages("\n".join(lines))

        assert len(pages) > 1, "a 200-line document must not fit on one page"
        recovered = "\n".join(text for _, text in pages).split("\n")
        assert recovered == lines

    def test_page_count_follows_the_declared_line_budget(self):
        """Pins the arithmetic that decides how much goes on a page. At 72 dpi the
        text area is 792 - 2*36 = 720 px and a line is 16 px, so 45 lines fit."""
        converter = _converter()
        lines_per_page = (converter.page_height - 2 * converter.margin) // 16
        assert lines_per_page == 45

        pages = converter.convert_text_to_pages("\n".join("x" for _ in range(90)))
        assert len(pages) == 2
        assert len(pages[0][1].split("\n")) == 45
        assert len(pages[1][1].split("\n")) == 45

        # One line past the boundary must open a third page, not be discarded.
        pages = converter.convert_text_to_pages("\n".join("x" for _ in range(91)))
        assert len(pages) == 3
        assert pages[2][1] == "x"

    def test_a_long_line_is_wrapped_without_losing_characters(self):
        """Wrapping slices the line; an off-by-one in either slice silently eats or
        duplicates characters in the middle of a record."""
        converter = _converter()
        chars_per_line = (converter.page_width - 2 * converter.margin) // 7
        assert chars_per_line == 77

        body = "".join(str(i % 10) for i in range(500))
        pages = converter.convert_text_to_pages(body)

        recovered = "".join(text for _, text in pages).replace("\n", "")
        assert recovered == body
        # And it really was wrapped rather than left as one over-wide line.
        first_line = pages[0][1].split("\n")[0]
        assert len(first_line) == chars_per_line

    def test_blank_lines_are_preserved_rather_than_collapsed(self):
        """Blank lines are record separators in a lot of plain-text input; the
        `if not line.strip()` branch exists to keep them."""
        pages = _converter().convert_text_to_pages("first\n\n\nsecond")
        assert pages[0][1] == "first\n\n\nsecond"

    def test_empty_input_still_yields_exactly_one_page(self):
        """A document with zero pages breaks every downstream consumer, so the
        `pages if pages else ...` guard must hold for empty input."""
        pages = _converter().convert_text_to_pages("")
        assert len(pages) == 1
        _open(pages[0][0])

    def test_the_rendered_page_is_a_real_jpeg_of_canvas_size_with_ink_on_it(self):
        """A blank page is the module's signature failure and it is invisible from
        the returned text, which is computed independently of the drawing."""
        converter = _converter()
        pages = converter.convert_text_to_pages("VISIBLE TEXT")

        img = _open(pages[0][0])
        assert img.format == "JPEG"
        assert img.size == (converter.page_width, converter.page_height)
        assert _ink_bbox(img) is not None, "page rendered blank"

    def test_a_render_failure_keeps_the_full_text(self):
        """When drawing fails the text must still reach OCR unabridged — the
        handler returns `content`, not the truncated page text."""
        converter = _converter()
        content = "\n".join(f"LINE{i}" for i in range(100))

        with patch.object(dc_module.Image, "new", side_effect=OSError("no canvas")):
            pages = converter.convert_text_to_pages(content)

        assert len(pages) == 1
        assert pages[0][1] == content
        # The image is the hardcoded last-resort literal here; see
        # TestEmptyPage.test_the_last_resort_bytes_do_not_decode.
        assert pages[0][0]

    def test_a_missing_named_font_does_not_stop_conversion(self):
        """`ImageFont.truetype("DejaVuSansMono.ttf")` resolves by name on some
        hosts and not others; the OSError branch must keep rendering."""
        converter = _converter()
        with patch.object(dc_module.ImageFont, "truetype", _named_fonts_unavailable()):
            pages = converter.convert_text_to_pages("STILL RENDERS")
        assert pages[0][1] == "STILL RENDERS"
        assert _ink_bbox(_open(pages[0][0])) is not None


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestCsvConversion:
    def test_every_row_of_a_well_formed_csv_appears_exactly_once_in_order(self):
        converter = _converter()
        content = "id,label\n" + "\n".join(f"{i},L{i:03d}" for i in range(8))

        pages = converter.convert_csv_to_pages(content)
        text = "\n".join(t for _, t in pages)

        positions = [text.index(f"L{i:03d}") for i in range(8)]
        assert positions == sorted(positions), "rows reordered"
        for i in range(8):
            assert text.count(f"L{i:03d}") == 1, f"row L{i:03d} duplicated or lost"

    def test_a_csv_too_long_for_one_page_keeps_every_row_and_repeats_the_header(self):
        """Two separate failure modes in one fixture. A row lost at a page seam is
        invisible downstream, and a continuation page that does not repeat the
        markdown header stops being a parseable table — the extraction path reads
        this text as markdown, so the second page's rows would lose their column
        names."""
        converter = _converter()
        content = "id,label,amount\n" + "\n".join(
            f"{i},L{i:03d},{i * 7}" for i in range(60)
        )

        pages = converter.convert_csv_to_pages(content)
        assert len(pages) > 1, "60 rows must not fit on one 72 dpi page"

        text = "\n".join(t for _, t in pages)
        for i in range(60):
            assert text.count(f"L{i:03d}") == 1, f"row L{i:03d} duplicated or lost"

        header_line = pages[0][1].split("\n")[0]
        separator_line = pages[0][1].split("\n")[1]
        assert "label" in header_line
        assert "---" in separator_line
        for page_index, (_, page_text) in enumerate(pages[1:], start=1):
            lines = page_text.split("\n")
            assert lines[0] == header_line, f"page {page_index} lost the table header"
            assert lines[1] == separator_line
        # Exactly once per page: the first page already contains its own header, so
        # prepending there would duplicate it and make the markdown malformed.
        for page_index, (_, page_text) in enumerate(pages):
            assert page_text.count(header_line) == 1, (
                f"page {page_index} repeats the header line"
            )

    def test_the_basic_parser_takes_over_when_pandas_cannot_tokenize(self):
        """A row with more fields than the header makes `pd.read_csv` raise
        `ParserError`; the fallback must still produce a table rather than the
        empty page the outer handler would give."""
        converter = _converter()
        pages = converter.convert_csv_to_pages("a,b\n1,2\nKEEPME,4,5,6\n")

        text = "\n".join(t for _, t in pages)
        assert "KEEPME" in text
        assert "| a | b |" in text

    def test_a_ragged_row_keeps_its_extra_fields(self):
        """A row wider than the header keeps every field (#1158).

        A row with more fields than the header is exactly what pushes `pd.read_csv`
        into `ParserError` and therefore into this `csv.reader` fallback — so the path
        reached *because* the file is ragged was the one discarding the ragged data.
        `col_widths` was sized from the header alone and the cell loop was guarded by
        `col_idx < len(col_widths)`, so every field past the header's width was
        dropped, with nothing logged at row scope. The rendered markdown was
        well-formed, so a CSV whose last column appears on only some rows reached
        extraction with that column silently missing."""
        converter = _converter()
        pages = converter.convert_csv_to_pages("a,b\n1,2\n3,4,DROPPED\n")

        text = "\n".join(t for _, t in pages)
        assert "DROPPED" in text

    def test_every_row_is_rendered_at_the_tables_full_width(self):
        """The pipe count must be the same on every line.

        A markdown table with a ragged pipe count is parsed inconsistently by
        downstream readers, so a short row is padded rather than left narrow — which
        also means the header and the widened data rows line up.
        """
        converter = _converter()
        table = converter._format_csv_as_table([["a", "b"], ["1"], ["3", "4", "EXTRA"]])

        pipe_counts = {line.count("|") for line in table.split("\n")}
        assert len(pipe_counts) == 1, (
            f"rows rendered at differing widths: {sorted(pipe_counts)}"
        )
        assert "EXTRA" in table

    def test_a_header_only_csv_returns_a_page_with_no_text_at_all(self):
        """Pins current behaviour, and it is a content-loss defect.

        `df.empty` is true for a frame with columns and zero rows, so a CSV that
        is only a header line returns one blank page and an empty string — the
        column names, which are the entire content of the file, are discarded."""
        pages = _converter().convert_csv_to_pages("alpha,beta,gamma\n")

        assert len(pages) == 1
        assert pages[0][1] == ""
        assert "alpha" not in pages[0][1]

    def test_a_completely_empty_csv_yields_one_page(self):
        """`pd.read_csv` raises `EmptyDataError`, then `csv.reader` gives no rows."""
        pages = _converter().convert_csv_to_pages("")
        assert len(pages) == 1
        assert pages[0][1] == ""

    def test_an_unreadable_csv_still_returns_its_text(self):
        """The outer handler's job: on total failure the original content must
        survive as page text rather than being replaced by an error string."""
        converter = _converter()
        with patch.object(
            converter, "_convert_markdown_to_pages", side_effect=RuntimeError("boom")
        ):
            pages = converter.convert_csv_to_pages("a,b\n1,2\n")

        assert len(pages) == 1
        assert pages[0][1] == "a,b\n1,2\n"


class TestCsvFormatting:
    def test_format_csv_as_table_emits_a_separator_only_when_there_are_data_rows(self):
        converter = _converter()

        one_row = converter._format_csv_as_table([["a", "b"]])
        assert one_row == "| a | b |"

        two_rows = converter._format_csv_as_table([["a", "b"], ["1", "2"]])
        assert two_rows.split("\n") == ["| a | b |", "| --- | --- |", "| 1 | 2 |"]

    def test_format_csv_as_table_sizes_the_separator_from_the_widest_cell(self):
        """The separator's dash count comes from `col_widths`, which is measured
        over every row, not just the header — a narrow header with wide data must
        still produce a markdown-valid separator."""
        converter = _converter()
        out = converter._format_csv_as_table([["a"], ["0123456789"]])
        assert out.split("\n")[1] == "| " + "-" * 10 + " |"

    def test_format_csv_as_table_never_truncates_cell_text(self):
        """An earlier shape of this helper capped column width; a capped cell
        silently shortens an account number or a description."""
        long_value = "X" * 300
        out = _converter()._format_csv_as_table([["h"], [long_value]])
        assert long_value in out

    def test_format_csv_as_table_handles_no_rows(self):
        assert _converter()._format_csv_as_table([]) == ""

    def test_pandas_formatting_pins_number_and_date_presentation(self):
        """These are the values a model reads off the page. An integer column
        rendered without separators, or a date rendered as a full timestamp, both
        change what extraction sees."""
        converter = _converter()
        frame = pd.DataFrame(
            {
                "Count": [1234567, 42],
                "Amount": [2.5, 3.0],
                "When": pd.to_datetime(["2024-03-15", "2024-12-01"]),
            }
        )

        markdown = converter._format_csv_with_pandas(frame, "irrelevant")

        cells = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in markdown.split("\n")
        ]
        assert cells[0] == ["Count", "Amount", "When"]
        # Trailing zeros are stripped on the CSV path, unlike the Excel path,
        # which keeps two decimals — see TestExcelTableData below. A float that
        # happens to be integral loses its decimals, not its value.
        assert cells[2] == ["1,234,567", "2.5", "2024-03-15"]
        assert cells[3] == ["42", "3", "2024-12-01"]
        assert "00:00:00" not in markdown

    def test_pandas_formatting_falls_back_to_the_raw_csv_on_error(self):
        """The fallback re-parses `original_content`, so the rows must come back
        even though the DataFrame it was handed is unusable."""
        converter = _converter()
        with patch.object(
            pd.api.types, "is_numeric_dtype", side_effect=TypeError("dtype gone")
        ):
            out = converter._format_csv_with_pandas(
                pd.DataFrame({"a": [1]}), "a,b\n1,RECOVERED\n"
            )

        assert "RECOVERED" in out


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


class TestExcelConversion:
    def test_every_sheet_of_a_real_workbook_is_converted_in_order(self):
        """The characteristic Office failure is a multi-sheet workbook reduced to
        its first sheet. This reads a genuine .xlsx, so the whole
        `ExcelFile.sheet_names` loop has to run for it to pass."""
        workbook = _xlsx_bytes(
            [
                ("First", pd.DataFrame({"Item": ["Widget"], "Qty": [3]})),
                ("Second", pd.DataFrame({"Region": ["West"], "Total": [1500]})),
                ("Third", pd.DataFrame({"Note": ["Final"]})),
            ]
        )

        pages = _converter().convert_excel_to_pages(workbook)
        text = "\n".join(t for _, t in pages)

        assert "Error reading Excel file" not in text
        for value in ("Widget", "West", "1,500", "Final"):
            assert value in text, f"{value} lost in conversion"
        assert text.index("Widget") < text.index("West") < text.index("Final")
        # Sheet names label the sections when there is more than one sheet.
        assert "## First" in text
        assert "## Second" in text
        assert "## Third" in text

    def test_a_single_sheet_workbook_is_not_given_a_sheet_heading(self):
        """The `> 1` guard: one sheet is the whole document, so a "## Sheet1"
        heading would be noise the model has to ignore."""
        workbook = _xlsx_bytes([("Only", pd.DataFrame({"Item": ["Widget"]}))])
        text = "\n".join(t for _, t in _converter().convert_excel_to_pages(workbook))

        assert "Widget" in text
        assert "## Only" not in text
        assert not text.lstrip().startswith("#")

    def test_an_empty_sheet_is_skipped_without_disturbing_the_others(self):
        """An empty sheet reaching the renderer would emit a bare heading with no
        table under it; skipping it must not cost the sheets around it."""
        workbook = _xlsx_bytes(
            [
                ("Data", pd.DataFrame({"Item": ["Widget"], "Qty": [3]})),
                ("Blank", pd.DataFrame()),
                ("More", pd.DataFrame({"Item": ["Gadget"]})),
            ]
        )

        text = "\n".join(t for _, t in _converter().convert_excel_to_pages(workbook))

        assert "Blank" not in text
        assert "Widget" in text
        assert "Gadget" in text
        assert "## Data" in text and "## More" in text

    def test_an_empty_workbook_yields_one_page(self):
        workbook = _xlsx_bytes([("Blank", pd.DataFrame())])
        pages = _converter().convert_excel_to_pages(workbook)
        assert len(pages) == 1
        _open(pages[0][0])

    def test_unreadable_bytes_degrade_to_a_single_labelled_page(self):
        """The degradation shape matters: one page whose text names the failure,
        so an operator reading the extraction output can tell what happened."""
        pages = _converter().convert_excel_to_pages(b"not-a-workbook")
        assert len(pages) == 1
        assert pages[0][1] == "Error reading Excel file"
        _open(pages[0][0])

    def test_a_workbook_too_long_for_one_page_keeps_every_row(self):
        """Excel goes through the same markdown pager as CSV; a row lost at a seam
        would be invisible."""
        frame = pd.DataFrame({"Label": [f"L{i:03d}" for i in range(60)]})
        workbook = _xlsx_bytes([("Long", frame)])

        pages = _converter().convert_excel_to_pages(workbook)
        assert len(pages) > 1
        text = "\n".join(t for _, t in pages)
        for i in range(60):
            assert text.count(f"L{i:03d}") == 1


class TestExcelTableData:
    def test_row_count_and_header_row_are_exact(self):
        """A header row plus one row per record. Off by one here means either a
        data row is read as a header or a record is dropped."""
        frame = pd.DataFrame({"A": [1, 2, 3], "B": ["x", "y", "z"]})
        table = _converter()._extract_excel_table_data(frame)

        assert len(table) == len(frame) + 1
        assert [cell["text"] for cell in table[0]] == ["A", "B"]
        assert all(cell["is_header"] and cell["bold"] for cell in table[0])
        assert [row[1]["text"] for row in table[1:]] == ["x", "y", "z"]

    def test_cell_text_and_alignment_are_derived_from_the_column_type(self):
        """Alignment and the formatted text are what reaches the page image, so
        each branch is pinned by value rather than by being executed."""
        frame = pd.DataFrame(
            {
                "Count": [1234567, 42],
                "Amount": [2.5, 3.0],
                "Missing": [None, None],
                "When": pd.to_datetime(["2024-03-15", "2024-12-01"]),
                "Money": ["$99.99", "free"],
            }
        )

        table = _converter()._extract_excel_table_data(frame)
        first = {cell_name: table[1][i] for i, cell_name in enumerate(frame.columns)}

        assert (first["Count"]["text"], first["Count"]["alignment"]) == (
            "1,234,567",
            "right",
        )
        assert first["Count"]["data_type"] == "numeric"
        assert first["Amount"]["text"] == "2.50"
        # An integral float is shown without decimals but keeps its magnitude.
        assert table[2][1]["text"] == "3"
        assert (first["Missing"]["text"], first["Missing"]["alignment"]) == ("", "left")
        assert (first["When"]["text"], first["When"]["alignment"]) == (
            "2024-03-15",
            "center",
        )
        assert (first["Money"]["text"], first["Money"]["data_type"]) == (
            "$99.99",
            "currency",
        )
        # Plain text in the same column is not promoted to currency.
        assert table[2][4]["data_type"] == "text"

    def test_a_date_that_cannot_be_formatted_keeps_its_string_form(self):
        """`is_datetime64_any_dtype` says the column is a date, so the code calls
        `value.strftime`. When the two disagree — the reason this handler exists —
        the cell must still carry the value rather than being blanked."""
        frame = pd.DataFrame({"When": ["not-a-timestamp"]})

        with patch.object(pd.api.types, "is_datetime64_any_dtype", return_value=True):
            table = _converter()._extract_excel_table_data(frame)

        assert table[1][0]["text"] == "not-a-timestamp"
        assert table[1][0]["data_type"] == "date"

    def test_a_frame_with_no_rows_yields_no_table_data(self):
        assert _converter()._extract_excel_table_data(pd.DataFrame()) == []

    def test_the_fallback_keeps_every_value_but_only_the_first_column_name(self):
        """Pins current behaviour of the `except` path, reached when pandas dtype
        introspection fails (the reason this handler exists — it is version
        fragile). Data survives; the `break` after the first column means every
        header but the first is lost, so the surviving columns arrive unlabelled."""
        frame = pd.DataFrame({"First": [1, 2], "Second": ["keep", "also"]})

        with patch.object(
            pd.api.types, "is_numeric_dtype", side_effect=TypeError("dtype gone")
        ):
            table = _converter()._extract_excel_table_data(frame)

        values = [cell["text"] for row in table[1:] for cell in row]
        assert "keep" in values and "also" in values
        assert [cell["text"] for cell in table[0]] == ["First"]
        assert "Second" not in [cell["text"] for cell in table[0]]

    def test_the_fallback_returns_nothing_rather_than_raising(self):
        """Both handlers fail when the object is not a frame at all; the caller
        treats `[]` as "no table" and carries on."""
        assert _converter()._extract_excel_table_data(object()) == []


class TestExcelMarkdown:
    def _table_element(self, rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
        return {"type": "excel_table", "data": rows, "sheet_name": "S"}

    def test_numeric_cells_round_trip_back_through_the_dataframe(self):
        """The markdown generator parses the formatted text back into numbers so
        `to_markdown` can right-align them. A comma left in place would make the
        column a string column and lose the alignment."""
        rows = [
            [
                {"text": "Qty", "is_header": True, "data_type": "text"},
                {"text": "Price", "is_header": True, "data_type": "text"},
            ],
            [
                {"text": "1,200", "is_header": False, "data_type": "numeric"},
                {"text": "3.75", "is_header": False, "data_type": "numeric"},
            ],
        ]

        markdown = _converter()._generate_enhanced_excel_markdown(
            [self._table_element(rows)]
        )

        assert "1,200" in markdown
        assert "3.75" in markdown
        assert "Qty" in markdown and "Price" in markdown

    def test_an_unparseable_numeric_cell_keeps_its_original_text(self):
        rows = [
            [{"text": "Qty", "is_header": True, "data_type": "text"}],
            [{"text": "n/a", "is_header": False, "data_type": "numeric"}],
        ]
        markdown = _converter()._generate_enhanced_excel_markdown(
            [self._table_element(rows)]
        )
        assert "n/a" in markdown

    def test_a_ragged_table_falls_back_to_pipe_rows_without_losing_cells(self):
        """`pd.DataFrame(data, columns=headers)` raises when a row is wider than
        the header. The inner handler must emit the rows verbatim — this is the
        one place in the module where ragged input keeps all of its cells."""
        rows = [
            [{"text": "A", "is_header": True, "data_type": "text"}],
            [
                {"text": "one", "is_header": False, "data_type": "text"},
                {"text": "EXTRA", "is_header": False, "data_type": "text"},
            ],
        ]

        markdown = _converter()._generate_enhanced_excel_markdown(
            [self._table_element(rows)]
        )

        assert "EXTRA" in markdown
        assert "| one | EXTRA |" in markdown

    def test_an_empty_table_element_is_skipped(self):
        markdown = _converter()._generate_enhanced_excel_markdown(
            [self._table_element([])]
        )
        assert markdown == ""

    def test_the_outer_fallback_keeps_sheet_names_and_cells(self):
        """Reached when the whole generator fails; the recovery has to carry both
        sheets' content and both headings, not just the text."""
        elements = [
            {"type": "sheet_header", "sheet_name": "Alpha"},
            self._table_element(
                [[{"text": "A", "is_header": True}], [{"text": "one"}]]
            ),
            {"type": "sheet_header", "sheet_name": "Beta"},
            self._table_element(
                [[{"text": "B", "is_header": True}], [{"text": "two"}]]
            ),
        ]

        with patch.object(pd, "DataFrame", side_effect=RuntimeError("boom")):
            markdown = _converter()._generate_enhanced_excel_markdown(elements)

        assert "## Alpha" in markdown and "## Beta" in markdown
        assert "| one |" in markdown and "| two |" in markdown

    def test_render_formatted_excel_content_recovers_all_cells_on_failure(self):
        """If markdown generation dies the content must still be paged, with the
        sheet headings intact."""
        converter = _converter()
        elements = [
            {"type": "sheet_header", "sheet_name": "Alpha"},
            {
                "type": "excel_table",
                "data": [[{"text": "H"}], [{"text": "RECOVERED"}]],
            },
        ]

        with patch.object(
            converter,
            "_generate_enhanced_excel_markdown",
            side_effect=RuntimeError("boom"),
        ):
            pages = converter._render_formatted_excel_content(elements)

        text = "\n".join(t for _, t in pages)
        assert "RECOVERED" in text
        assert "=== Sheet: Alpha ===" in text


# ---------------------------------------------------------------------------
# Word, against real .docx packages
# ---------------------------------------------------------------------------


class TestWordEndToEnd:
    def test_explicit_page_breaks_produce_exactly_that_many_pages_in_order(self):
        """The headline Word invariant. Three paragraphs separated by two real
        page breaks must come back as three pages carrying the right text in the
        right order — a converter that returns one page, or four, or the same page
        three times, all look like success to the caller."""

        def build(document: Any) -> None:
            document.add_paragraph("ALPHA first page")
            document.add_page_break()
            document.add_paragraph("BETA second page")
            document.add_page_break()
            document.add_paragraph("GAMMA third page")

        pages = _converter().convert_word_to_pages(_docx_bytes(build))

        assert len(pages) == 3
        assert [t.strip() for t in (text for _, text in pages)] == [
            "ALPHA first page",
            "BETA second page",
            "GAMMA third page",
        ]

    def test_each_page_of_a_multi_page_document_is_a_distinct_non_blank_image(self):
        """Page text and page image are produced by separate code, so identical or
        blank images would not show up in the text assertions above. Distinctness
        is the check that catches the whole document being rendered onto page one's
        canvas and copied."""

        def build(document: Any) -> None:
            document.add_paragraph("ALPHA")
            document.add_page_break()
            document.add_paragraph("BETA")

        converter = _converter()
        pages = converter.convert_word_to_pages(_docx_bytes(build))

        assert len(pages) == 2
        for image_bytes, _ in pages:
            img = _open(image_bytes)
            assert img.size == (converter.page_width, converter.page_height)
            assert _ink_bbox(img) is not None, "page rendered blank"
        assert pages[0][0] != pages[1][0], "both pages rendered identically"

    def test_a_table_keeps_every_cell_and_the_order_of_its_rows(self):
        """Tables are the content most often lost here, and a table flattened into
        the wrong row order reads as a different set of records."""

        def build(document: Any) -> None:
            table = document.add_table(rows=3, cols=2)
            values = [
                ("Item", "Amount"),
                ("Widget", "10.50"),
                ("Gadget", "22.75"),
            ]
            for row_index, (left, right) in enumerate(values):
                table.cell(row_index, 0).text = left
                table.cell(row_index, 1).text = right

        pages = _converter().convert_word_to_pages(_docx_bytes(build))
        text = "\n".join(t for _, t in pages)

        assert "Item | Amount" in text
        assert "Widget | 10.50" in text
        assert "Gadget | 22.75" in text
        assert text.index("Widget") < text.index("Gadget")

    def test_paragraphs_and_tables_keep_their_document_order(self):
        """The body is iterated as a single sequence precisely so a table between
        two paragraphs does not get hoisted to the end."""

        def build(document: Any) -> None:
            document.add_paragraph("BEFORE the table")
            table = document.add_table(rows=1, cols=1)
            table.cell(0, 0).text = "INSIDE the table"
            document.add_paragraph("AFTER the table")

        text = "\n".join(
            t for _, t in _converter().convert_word_to_pages(_docx_bytes(build))
        )

        assert text.index("BEFORE") < text.index("INSIDE") < text.index("AFTER")

    def test_an_embedded_image_is_handed_to_the_callback_and_its_text_kept(self):
        """The OCR callback is how an embedded image's content enters the page
        text at all. Asserting only that the callback fired would pass with the
        wrong bytes handed over, or the returned text thrown away."""
        png = _png_bytes()
        seen: list[bytes] = []

        def callback(image_bytes: bytes) -> str:
            seen.append(image_bytes)
            return "TEXT FROM THE PICTURE"

        def build(document: Any) -> None:
            document.add_paragraph("BEFORE the picture")
            document.add_picture(io.BytesIO(png), width=Inches(2))
            document.add_paragraph("AFTER the picture")

        pages = _converter().convert_word_to_pages(
            _docx_bytes(build), ocr_image_callback=callback
        )
        text = "\n".join(t for _, t in pages)

        assert seen == [png], "the callback did not receive the embedded image"
        assert "TEXT FROM THE PICTURE" in text
        assert text.index("BEFORE") < text.index("TEXT FROM THE PICTURE")
        assert text.index("TEXT FROM THE PICTURE") < text.index("AFTER")

    def test_without_a_callback_an_embedded_image_becomes_a_visible_placeholder(self):
        """Silence would be worse: a reader of the page text has to be able to see
        that something was there."""
        png = _png_bytes()

        def build(document: Any) -> None:
            document.add_picture(io.BytesIO(png), width=Inches(2))

        text = "\n".join(
            t for _, t in _converter().convert_word_to_pages(_docx_bytes(build))
        )
        assert "[Image]" in text

    def test_a_callback_that_fails_marks_the_image_rather_than_dropping_it(self):
        png = _png_bytes()

        def build(document: Any) -> None:
            document.add_picture(io.BytesIO(png), width=Inches(2))

        text = "\n".join(
            t
            for _, t in _converter().convert_word_to_pages(
                _docx_bytes(build),
                ocr_image_callback=lambda _b: (_ for _ in ()).throw(
                    RuntimeError("ocr down")
                ),
            )
        )
        assert "[Image - OCR failed]" in text

    def test_a_document_of_only_empty_paragraphs_still_yields_a_page(self):
        def build(document: Any) -> None:
            for _ in range(3):
                document.add_paragraph("")

        pages = _converter().convert_word_to_pages(_docx_bytes(build))
        assert len(pages) >= 1
        _open(pages[0][0])


class TestWordPageGeometry:
    def test_sectpr_twips_are_converted_to_pixels(self):
        """Page geometry sets the pagination budget, so an error here changes how
        many pages a Word document becomes. A4 landscape with half-inch margins:
        (16838 - 1440) / 1440 in tall and (11906 - 1440) / 1440 in wide."""
        body = parse_xml(
            f"<w:body {nsdecls('w')}>"
            '<w:sectPr><w:pgSz w:w="16838" w:h="11906"/>'
            '<w:pgMar w:top="720" w:bottom="720" w:left="720" w:right="720"/>'
            "</w:sectPr></w:body>"
        )

        geometry = _converter(150)._extract_page_geometry(body, qn("w:sectPr"), qn)

        assert geometry["usable_height_px"] == int((11906 - 1440) / 1440 * 150)
        assert geometry["usable_width_px"] == int((16838 - 1440) / 1440 * 150)
        # Landscape really is wider than it is tall.
        assert geometry["usable_width_px"] > geometry["usable_height_px"]

    def test_a_body_without_sectpr_falls_back_to_us_letter(self):
        body = parse_xml(f"<w:body {nsdecls('w')}/>")
        geometry = _converter(150)._extract_page_geometry(body, qn("w:sectPr"), qn)
        assert geometry == {"usable_height_px": 1350, "usable_width_px": 975}

    def test_a_sectpr_missing_its_attributes_falls_back_to_us_letter(self):
        """`pgSz`/`pgMar` present but empty is common in documents written by
        tools other than Word; the `.get(name, default)` defaults must be Letter."""
        body = parse_xml(
            f"<w:body {nsdecls('w')}><w:sectPr><w:pgSz/><w:pgMar/></w:sectPr></w:body>"
        )
        geometry = _converter(150)._extract_page_geometry(body, qn("w:sectPr"), qn)
        assert geometry == {"usable_height_px": 1350, "usable_width_px": 975}

    def test_the_layout_budget_scales_with_the_converters_dpi(self):
        """The budget must track the canvas it will be drawn on (#1156).

        `_extract_page_geometry` converted twips at a hardcoded 150 dpi while the
        canvas `_render_word_page` draws on is sized from `self.dpi`. Every real
        `.docx` has a `<w:sectPr>`, so this path — not the dpi-aware
        `_default_page_geometry` — is what paginates Word documents, and `OcrService`
        builds the converter at 300 dpi by default. So at the production default the
        budget filled 45% of the canvas and a document became roughly 2.2x the pages
        it has, each separately uploaded, OCR'd, classified and billed; below 150 it
        inverted and the overflow was drawn off the bottom edge.
        """
        body = parse_xml(
            f"<w:body {nsdecls('w')}>"
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440"/>'
            "</w:sectPr></w:body>"
        )

        budgets = {
            dpi: _converter(dpi)._extract_page_geometry(body, qn("w:sectPr"), qn)
            for dpi in (72, 150, 300)
        }
        # 9 usable inches (11 less two 1-inch margins) at each dpi.
        assert budgets[72]["usable_height_px"] == 648
        assert budgets[150]["usable_height_px"] == 1350
        assert budgets[300]["usable_height_px"] == 2700

    def test_the_budget_stays_within_the_canvas_at_every_dpi(self):
        """The property that actually matters, expressed as a ratio rather than as
        three numbers: the budget must never exceed the drawable canvas, or the
        overflow is drawn off the edge, and it must not be a small fraction of it, or
        pages break early and multiply.

        The ratio is about 0.90 rather than 1.00, and that is deliberate: the budget
        uses the **document's own** margins from `<w:pgMar>` (1 inch here) while the
        canvas uses the converter's 0.5 inch margin. Keeping the document's margins is
        what makes the page count match how the document paginates in Word, and the
        spare canvas is whitespace rather than lost content.
        """
        body = parse_xml(
            f"<w:body {nsdecls('w')}>"
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440"/>'
            "</w:sectPr></w:body>"
        )

        for dpi in (72, 150, 300, 600):
            converter = _converter(dpi)
            budget = converter._extract_page_geometry(body, qn("w:sectPr"), qn)
            canvas_h = converter.page_height - 2 * converter.margin
            ratio = budget["usable_height_px"] / canvas_h
            assert 0.85 <= ratio <= 1.0, (
                f"at {dpi} dpi the budget is {ratio:.2f} of the canvas; above 1.0 the "
                "page is drawn off its bottom edge and well below 1.0 it breaks early "
                "and multiplies the page count"
            )

    def test_a_low_dpi_page_is_no_longer_drawn_past_its_bottom_margin(self):
        """The rendered consequence, asserted on the IMAGE rather than the text.

        At 72 dpi the budget used to exceed the canvas, so a page carried more lines
        than it could render and the excess was clipped out of the page image while
        remaining in the page text. A text assertion cannot fail for that, which is
        why this measures the ink bounding box.
        """
        converter = _converter(72)
        lines = [f"LINE{i:03d} body text for this paragraph" for i in range(80)]

        def build(document: Any) -> None:
            for line in lines:
                document.add_paragraph(line)

        pages = converter.convert_word_to_pages(_docx_bytes(build))

        # Text is still complete -- that was never the defect.
        text = "\n".join(t for _, t in pages)
        for line in lines:
            assert line in text, f"{line!r} lost from the page text"

        bbox = _ink_bbox(_open(pages[0][0]))
        assert bbox is not None
        assert bbox[3] < converter.page_height - converter.margin, (
            f"ink reaches y={bbox[3]} on a page whose bottom margin starts at "
            f"{converter.page_height - converter.margin}, so content is being drawn "
            "off the rendered page"
        )


class TestWordFormattingExtraction:
    def test_a_page_break_before_paragraph_is_detected(self):
        """`w:pageBreakBefore` is the other way Word starts a page and it lives in
        `pPr` rather than in a run, so it needs its own branch."""

        def build(document: Any) -> None:
            document.add_paragraph("FIRST")
            second = document.add_paragraph("SECOND")
            second.paragraph_format.page_break_before = True

        pages = _converter().convert_word_to_pages(_docx_bytes(build))

        assert len(pages) == 2
        assert "FIRST" in pages[0][1] and "SECOND" not in pages[0][1]
        assert "SECOND" in pages[1][1]

    def test_a_last_rendered_page_break_is_detected(self):
        """Word records where it last laid out a page break in
        `w:lastRenderedPageBreak`; for a document with no explicit breaks this is
        the only page information available."""
        converter = _converter()
        document = Document()
        document.add_paragraph("FIRST")
        broken = parse_xml(
            f"<w:p {nsdecls('w')}><w:r><w:lastRenderedPageBreak/>"
            "<w:t>SECOND</w:t></w:r></w:p>"
        )
        document.element.body.insert(
            list(document.element.body).index(
                document.paragraphs[-1]._p  # pyright: ignore[reportPrivateUsage]
            )
            + 1,
            broken,
        )

        elements, _geometry = converter._extract_word_formatting(document)
        types = [element["type"] for element in elements]
        assert "page_break" in types
        assert types.index("page_break") == 1, (
            "the break must sit between the two paragraphs"
        )

    def test_a_break_that_is_not_a_page_break_does_not_split_the_page(self):
        """`w:br` with no `w:type` is a line break. Treating it as a page break
        would explode a normal document into one page per line."""
        converter = _converter()
        document = Document()
        document.element.body.insert(
            0,
            parse_xml(
                f"<w:p {nsdecls('w')}><w:r><w:t>A</w:t><w:br/><w:t>B</w:t></w:r></w:p>"
            ),
        )

        elements, _geometry = converter._extract_word_formatting(document)
        assert [e["type"] for e in elements].count("page_break") == 0

    def test_an_empty_paragraph_becomes_spacing_not_nothing(self):
        """Blank paragraphs carry the document's vertical rhythm, which is part of
        what decides where pages break."""
        converter = _converter()
        document = Document()
        document.add_paragraph("A")
        document.add_paragraph("")
        document.add_paragraph("B")

        elements, _geometry = converter._extract_word_formatting(document)
        assert [e["type"] for e in elements] == ["paragraph", "spacing", "paragraph"]
        assert elements[1]["height"] == 12

    def test_an_image_part_that_cannot_be_read_does_not_cost_the_whole_document(self):
        """The image cache is built up front; a relationship whose part will not
        load must degrade to that one picture going missing rather than to the
        structured walk failing and the document collapsing to flat text."""
        converter = _converter()

        def build(document: Any) -> None:
            document.add_paragraph("SURROUNDING TEXT")
            document.add_picture(io.BytesIO(_png_bytes()), width=Inches(2))

        raw = _docx_bytes(build)
        from docx.parts.image import ImagePart

        with patch.object(
            ImagePart,
            "blob",
            property(lambda _self: (_ for _ in ()).throw(OSError("part unreadable"))),
        ):
            pages = converter.convert_word_to_pages(raw)

        text = "\n".join(t for _, t in pages)
        assert "SURROUNDING TEXT" in text
        assert "Error reading Word document" not in text
        assert "[Image]" not in text

    def test_the_fallback_keeps_the_documents_text_when_iteration_fails(self):
        """If the structured walk dies, the paragraphs' text must still come
        through — this handler is the difference between a degraded document and
        an empty one."""
        converter = _converter()
        document = Document()
        document.add_paragraph("KEEP THIS ONE")
        document.add_paragraph("AND THIS ONE")

        class _Exploding:
            def __iter__(self) -> Any:
                raise RuntimeError("body unreadable")

            def find(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        with patch.object(type(document.element), "body", _Exploding()):
            elements, geometry = converter._extract_word_formatting(document)

        assert len(elements) == 1
        assert elements[0]["text"] == "KEEP THIS ONE\nAND THIS ONE"
        # Geometry was read before the walk failed, so the caller still gets a
        # usable budget rather than a zero-height one.
        assert geometry["usable_height_px"] > 0
        assert geometry["usable_width_px"] > 0


class TestParagraphElement:
    def test_a_heading_records_its_level(self):
        document = Document()
        paragraph = document.add_heading("Section title", level=3)

        element = DocumentConverter._build_paragraph_element(
            paragraph, WD_ALIGN_PARAGRAPH
        )

        assert element["is_heading"] is True
        assert element["heading_level"] == 3
        # Headings get more space than body text, which is what makes them read
        # as headings once the font hierarchy is gone.
        assert element["space_before"] == 6 and element["space_after"] == 6

    def test_a_heading_style_without_a_number_defaults_to_level_one(self):
        """Templates carry styles like "Heading" or "Heading Alpha" whose last
        word is not a digit; `int()` on it raises and the handler must give the
        paragraph a usable level rather than letting the whole element be lost."""
        document = Document()
        document.styles.add_style("Heading Alpha", WD_STYLE_TYPE.PARAGRAPH)
        paragraph = document.add_paragraph("Title")
        paragraph.style = document.styles["Heading Alpha"]

        element = DocumentConverter._build_paragraph_element(
            paragraph, WD_ALIGN_PARAGRAPH
        )
        assert element["is_heading"] is True
        assert element["heading_level"] == 1

    @pytest.mark.parametrize(
        ("docx_alignment", "expected"),
        [
            (WD_ALIGN_PARAGRAPH.CENTER, "center"),
            (WD_ALIGN_PARAGRAPH.RIGHT, "right"),
            (WD_ALIGN_PARAGRAPH.JUSTIFY, "justify"),
            (WD_ALIGN_PARAGRAPH.LEFT, "left"),
            (None, "left"),
        ],
    )
    def test_alignment_is_mapped_from_the_docx_enum(
        self, docx_alignment: Any, expected: str
    ):
        """Alignment survives into the rendered image, so a mis-mapping moves the
        text. `LEFT` is included because it is falsy-adjacent in the enum and the
        code tests `if paragraph.alignment:` before comparing."""
        document = Document()
        paragraph = document.add_paragraph("Body")
        paragraph.alignment = docx_alignment

        element = DocumentConverter._build_paragraph_element(
            paragraph, WD_ALIGN_PARAGRAPH
        )
        assert element["alignment"] == expected

    def test_run_level_formatting_is_captured_per_run(self):
        document = Document()
        paragraph = document.add_paragraph()
        plain = paragraph.add_run("plain ")
        strong = paragraph.add_run("strong")
        strong.bold = True
        strong.underline = True
        strong.font.size = Inches(0.25)  # 18 pt
        strong.font.name = "Courier New"

        element = DocumentConverter._build_paragraph_element(
            paragraph, WD_ALIGN_PARAGRAPH
        )

        assert [run["text"] for run in element["runs"]] == [plain.text, "strong"]
        assert element["runs"][0]["bold"] is False
        assert element["runs"][1]["bold"] is True
        assert element["runs"][1]["underline"] is True
        assert element["runs"][1]["font_size"] == 18.0
        assert element["runs"][1]["font_name"] == "Courier New"
        assert element["text"] == "plain strong"

    def test_a_paragraph_whose_runs_are_all_whitespace_still_carries_its_text(self):
        """`formatted_runs` only keeps runs with non-blank text; if that leaves
        nothing, a synthetic run has to carry `paragraph.text` or the renderer
        draws an empty paragraph."""
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("   ")
        paragraph.add_run("\t")

        element = DocumentConverter._build_paragraph_element(
            paragraph, WD_ALIGN_PARAGRAPH
        )

        assert len(element["runs"]) == 1
        assert element["runs"][0]["text"] == "   \t"


class TestTableElement:
    def test_every_cell_of_a_real_table_is_captured_in_shape(self):
        document = Document()
        table = document.add_table(rows=2, cols=3)
        for row_index in range(2):
            for column_index in range(3):
                table.cell(
                    row_index, column_index
                ).text = f"r{row_index}c{column_index}"

        element = DocumentConverter._build_table_element(table)

        assert element is not None
        assert [[cell["text"] for cell in row] for row in element["data"]] == [
            ["r0c0", "r0c1", "r0c2"],
            ["r1c0", "r1c1", "r1c2"],
        ]

    def test_the_first_row_of_a_real_docx_table_is_marked_as_a_header(self):
        """The header row keeps its emphasis in the rendered page image (#1157).

        This asserted the opposite until the comparison was fixed:
        `is_header = table.rows[0] == row` relied on `==`, but `table.rows[idx]`
        builds a fresh `_Row` on each access and `python-docx` gives `_Row` no
        `__eq__`, so it was identity against a different object and false for every
        row. Every table in every `.docx` lost its header bold and grey background in
        the page image, leaving a vision model no cue for which row names the columns,
        while the page *text* was unaffected — so nothing downstream could notice."""
        document = Document()
        table = document.add_table(rows=2, cols=1)
        table.cell(0, 0).text = "Header"
        table.cell(1, 0).text = "Data"

        element = DocumentConverter._build_table_element(table)
        assert element is not None
        assert [cell["is_header"] for row in element["data"] for cell in row] == [
            True,
            False,
        ]

    def test_a_fresh_row_object_per_access_is_still_the_underlying_hazard(self):
        """The property that made the old comparison wrong, pinned on its own.

        Kept because the fix reads an index and an XML element rather than comparing
        `_Row` objects, and the reason for that is only visible here: if a future
        python-docx gave `_Row` a value `__eq__`, a reader would have no way to tell
        from the code why the indirection was there. Measured on python-docx 1.2.0.
        """
        document = Document()
        table = document.add_table(rows=2, cols=1)

        assert table.rows[0] is not table.rows[0]
        assert table.rows[0] != table.rows[0]
        # The XML element behind it IS stable, which is what the fix relies on.
        assert table.rows[0]._tr is table.rows[0]._tr

    def test_the_formats_own_header_flag_is_preferred_over_the_first_row(self):
        """`<w:trPr><w:tblHeader/>` is what Word writes for "repeat as header row".

        It can mark more than one row, which a first-row rule cannot express, so where
        the author set it that is the signal used. Two-row headers are ordinary in
        financial tables — a spanning title above the column names.
        """
        from docx.oxml.ns import qn

        document = Document()
        table = document.add_table(rows=3, cols=1)
        for idx in range(3):
            table.cell(idx, 0).text = f"row-{idx}"

        # Mark rows 0 and 1 as repeating header rows.
        for idx in (0, 1):
            tr_pr = table.rows[idx]._tr.get_or_add_trPr()
            tr_pr.append(tr_pr.makeelement(qn("w:tblHeader"), {}))

        element = DocumentConverter._build_table_element(table)
        assert element is not None
        assert [row[0]["is_header"] for row in element["data"]] == [True, True, False]

    def test_a_table_whose_only_marked_header_is_not_the_first_row(self):
        """The discriminating case for preferring the flag: a marked row that a
        first-row rule would miss, and a first row it would wrongly emphasise."""
        from docx.oxml.ns import qn

        document = Document()
        table = document.add_table(rows=3, cols=1)
        tr_pr = table.rows[1]._tr.get_or_add_trPr()
        tr_pr.append(tr_pr.makeelement(qn("w:tblHeader"), {}))

        element = DocumentConverter._build_table_element(table)
        assert element is not None
        assert [row[0]["is_header"] for row in element["data"]] == [False, True, False]

    def test_a_table_with_no_rows_is_reported_as_absent(self):
        """Returning an element with empty data would put a zero-row table into
        the layout and consume a page slot for nothing."""

        class _NoRows:
            rows: list[Any] = []

        assert DocumentConverter._build_table_element(_NoRows()) is None


class TestEmbeddedImageExtraction:
    @staticmethod
    def _drawing_paragraph(embed_id: str, cy: str | None, extra_blip: bool = False):
        extent = f'<wp:extent cx="914400" cy="{cy}"/>' if cy is not None else ""
        second = '<a:blip r:embed="rId9"/>' if extra_blip else ""
        return parse_xml(
            "<w:p "
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:r><w:drawing><wp:inline>{extent}"
            f'<a:blip r:embed="{embed_id}"/>{second}'
            "</wp:inline></w:drawing></w:r></w:p>"
        )

    @pytest.mark.parametrize(("dpi", "expected"), [(72, 72), (150, 150), (300, 300)])
    def test_the_display_height_is_converted_from_emus_at_the_given_dpi(
        self, dpi: int, expected: int
    ):
        """`wp:extent`'s `cy` is in EMUs (914400 per inch) and drives where pages
        break, so a wrong conversion moves page boundaries. One inch tall must
        come back as exactly `dpi` pixels."""
        elements: list[dict[str, Any]] = []
        DocumentConverter._extract_images_from_paragraph(
            self._drawing_paragraph("rId1", cy="914400"),
            {"rId1": b"png-bytes"},
            elements,
            qn("w:drawing"),
            qn("a:blip"),
            qn("r:embed"),
            dpi,
        )

        assert len(elements) == 1
        assert elements[0]["display_height_px"] == expected
        assert elements[0]["image_bytes"] == b"png-bytes"

    def test_a_missing_extent_leaves_the_height_unknown(self):
        """Zero means "unknown", and `_estimate_element_height` then applies its
        200 px fallback rather than treating the image as taking no space."""
        elements: list[dict[str, Any]] = []
        DocumentConverter._extract_images_from_paragraph(
            self._drawing_paragraph("rId1", cy=None),
            {"rId1": b"png"},
            elements,
            qn("w:drawing"),
            qn("a:blip"),
            qn("r:embed"),
            150,
        )

        assert elements[0]["display_height_px"] == 0
        assert DocumentConverter._estimate_element_height(elements[0]) == 200

    def test_a_non_numeric_extent_does_not_abort_the_extraction(self):
        elements: list[dict[str, Any]] = []
        DocumentConverter._extract_images_from_paragraph(
            self._drawing_paragraph("rId1", cy="not-a-number"),
            {"rId1": b"png"},
            elements,
            qn("w:drawing"),
            qn("a:blip"),
            qn("r:embed"),
            150,
        )
        assert len(elements) == 1
        assert elements[0]["display_height_px"] == 0

    def test_several_images_in_one_paragraph_are_all_kept_in_order(self):
        """A paragraph holding two pictures is ordinary, and keeping only the
        first would lose one silently."""
        elements: list[dict[str, Any]] = []
        DocumentConverter._extract_images_from_paragraph(
            self._drawing_paragraph("rId1", cy="914400", extra_blip=True),
            {"rId1": b"first", "rId9": b"second"},
            elements,
            qn("w:drawing"),
            qn("a:blip"),
            qn("r:embed"),
            150,
        )

        assert [element["image_bytes"] for element in elements] == [b"first", b"second"]

    def test_an_image_whose_relationship_is_missing_is_dropped(self):
        """Pins current behaviour. An unresolvable `r:embed` produces no element
        and no warning at image scope, so a picture whose relationship part failed
        to load disappears from the document without a trace."""
        elements: list[dict[str, Any]] = []
        DocumentConverter._extract_images_from_paragraph(
            self._drawing_paragraph("rIdMissing", cy="914400"),
            {"rId1": b"png"},
            elements,
            qn("w:drawing"),
            qn("a:blip"),
            qn("r:embed"),
            150,
        )
        assert elements == []


class TestResolveImageElements:
    def test_multi_line_ocr_text_becomes_one_paragraph_per_line(self):
        """Layout and page breaks are computed per element, so collapsing OCR'd
        text into a single element would mis-size the image's contribution."""
        resolved = _converter()._resolve_image_elements(
            [{"type": "image", "image_bytes": b"img"}],
            lambda _b: "first line\n\nthird line",
        )

        assert [element["type"] for element in resolved] == [
            "paragraph",
            "spacing",
            "paragraph",
        ]
        assert resolved[0]["text"] == "first line"
        assert resolved[2]["text"] == "third line"
        assert resolved[0]["runs"][0]["text"] == "first line"

    def test_blank_ocr_output_becomes_a_visible_placeholder(self):
        """An OCR pass that finds nothing must not leave the image invisible: the
        reader has to know a picture was there."""
        resolved = _converter()._resolve_image_elements(
            [{"type": "image", "image_bytes": b"img"}], lambda _b: "   \n\t "
        )
        assert any(element.get("text") == "[Image]" for element in resolved)

    def test_an_image_element_with_no_bytes_skips_the_callback(self):
        calls: list[bytes] = []
        resolved = _converter()._resolve_image_elements(
            [{"type": "image", "image_bytes": b""}],
            lambda b: calls.append(b) or "should not be used",  # pyright: ignore[reportUnknownLambdaType]
        )
        assert calls == []
        assert resolved[0]["text"] == "[Image]"

    def test_non_image_elements_pass_through_untouched_and_in_order(self):
        first = _para("A")
        spacing = {"type": "spacing", "height": 9}
        last = _para("B")

        resolved = _converter()._resolve_image_elements([first, spacing, last], None)

        assert resolved == [first, spacing, last]
        assert resolved[0] is first


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------


class TestWordPageLayout:
    def test_no_element_is_lost_or_reordered_by_pagination(self):
        """The invariant that makes every other layout assertion meaningful: the
        pages, concatenated, are exactly the input with the break markers removed.
        A page-count assertion alone passes while content is being dropped."""
        converter = _converter()
        geometry = {"usable_height_px": 200, "usable_width_px": 900}
        elements: list[dict[str, Any]] = []
        for i in range(30):
            elements.append(_para(f"P{i:02d}"))
            if i % 7 == 3:
                elements.append({"type": "page_break"})

        pages = converter._calculate_word_page_layout(elements, geometry)

        flattened = [element for page in pages for element in page]
        assert flattened == [e for e in elements if e["type"] != "page_break"]
        assert len(pages) > 1

    def test_content_overflows_onto_a_new_page_at_the_budget(self):
        """Exact arithmetic so the boundary is pinned: three 22 px paragraphs fit
        in 66 px, four do not."""
        converter = _converter()
        elements = [_para(f"P{i}") for i in range(4)]
        assert DocumentConverter._estimate_element_height(elements[0]) == 22

        assert (
            len(
                converter._calculate_word_page_layout(
                    elements[:3], {"usable_height_px": 66, "usable_width_px": 900}
                )
            )
            == 1
        )
        assert (
            len(
                converter._calculate_word_page_layout(
                    elements, {"usable_height_px": 66, "usable_width_px": 900}
                )
            )
            == 2
        )

    def test_a_leading_page_break_does_not_open_an_empty_page(self):
        """A blank leading page would be OCR'd, classified and billed."""
        converter = _converter()
        pages = converter._calculate_word_page_layout(
            [{"type": "page_break"}, _para("ONLY")],
            {"usable_height_px": 1350, "usable_width_px": 975},
        )
        assert len(pages) == 1
        assert pages[0][0]["text"] == "ONLY"

    def test_consecutive_and_trailing_page_breaks_do_not_open_empty_pages(self):
        converter = _converter()
        pages = converter._calculate_word_page_layout(
            [
                _para("A"),
                {"type": "page_break"},
                {"type": "page_break"},
                _para("B"),
                {"type": "page_break"},
            ],
            {"usable_height_px": 1350, "usable_width_px": 975},
        )
        assert len(pages) == 2
        assert [page[0]["text"] for page in pages] == ["A", "B"]

    def test_an_element_taller_than_a_whole_page_is_kept(self):
        """The overflow test is guarded by `and current_page`, so an oversized
        first element must land somewhere rather than being discarded."""
        converter = _converter()
        huge = {"type": "image", "image_bytes": b"x", "display_height_px": 5000}
        pages = converter._calculate_word_page_layout(
            [huge], {"usable_height_px": 200, "usable_width_px": 900}
        )
        assert pages == [[huge]]

    def test_no_elements_yield_one_empty_page_not_zero_pages(self):
        assert _converter()._calculate_word_page_layout(
            [],
            {
                "usable_height_px": 200,
                "usable_width_px": 900,
            },
        ) == [[]]

    def test_omitting_the_geometry_uses_the_dpi_aware_default(self):
        """The `page_geometry is None` path must scale with the canvas, otherwise
        a converter at a different dpi paginates against the wrong budget."""
        small = _converter(72)
        large = _converter(300)
        elements = [_para(f"P{i}") for i in range(60)]

        assert len(small._calculate_word_page_layout(elements)) > len(
            large._calculate_word_page_layout(elements)
        )


class TestElementHeightEstimates:
    """These numbers are the entire input to pagination, so each is pinned."""

    def test_spacing_uses_its_declared_height_and_defaults_to_twelve(self):
        assert (
            DocumentConverter._estimate_element_height(
                {"type": "spacing", "height": 40}
            )
            == 40
        )
        assert DocumentConverter._estimate_element_height({"type": "spacing"}) == 12

    def test_a_paragraph_grows_with_its_wrapped_line_count(self):
        """80 characters per line; the multiplier is what stops a long paragraph
        being treated as one line and overrunning the page."""
        short = DocumentConverter._estimate_element_height(_para("x"))
        assert short == 16 + 3 + 3

        three_lines = DocumentConverter._estimate_element_height(_para("x" * 160))
        assert three_lines == 16 * 3 + 3 + 3

    def test_a_heading_is_taller_than_body_text(self):
        heading = DocumentConverter._estimate_element_height(
            _para(
                "Title", is_heading=True, heading_level=1, space_before=6, space_after=6
            )
        )
        assert heading == 24 + 6 + 6
        assert heading > DocumentConverter._estimate_element_height(_para("Title"))

    def test_a_table_is_measured_by_its_row_count(self):
        element = {
            "type": "table",
            "data": [[{"text": "a"}] for _ in range(4)],
            "space_before": 12,
            "space_after": 12,
        }
        assert DocumentConverter._estimate_element_height(element) == 4 * 25 + 24

    def test_an_unknown_element_type_is_given_a_nonzero_height(self):
        """Zero would let an unbounded number of unknown elements pile onto one
        page."""
        assert DocumentConverter._estimate_element_height({"type": "footnote"}) == 20


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderWordPage:
    def test_the_page_text_is_paragraphs_then_table_rows_in_document_order(self):
        converter = _converter()
        elements = [
            _para("Intro line"),
            {
                "type": "table",
                "data": [
                    [{"text": "Item"}, {"text": "Qty"}],
                    [{"text": "Widget"}, {"text": "3"}],
                ],
                "space_before": 12,
                "space_after": 12,
            },
            _para("Closing line"),
        ]

        _image_bytes, text = converter._render_word_page(
            elements, converter._load_fonts()
        )

        assert text.split("\n") == [
            "Intro line",
            "Item | Qty",
            "Widget | 3",
            "Closing line",
        ]

    def test_the_rendered_page_is_the_right_size_and_not_blank(self):
        converter = _converter()
        image_bytes, _text = converter._render_word_page(
            [_para("Visible content")], converter._load_fonts()
        )

        img = _open(image_bytes)
        assert img.format == "JPEG"
        assert img.size == (converter.page_width, converter.page_height)
        assert _ink_bbox(img) is not None, "page rendered blank"

    def test_spacing_elements_push_content_down_the_page(self):
        """Spacing is the only thing preserving a document's vertical structure in
        the image; if it were ignored, everything would render flush to the top."""
        converter = _converter()
        fonts = converter._load_fonts()

        top_bytes, _ = converter._render_word_page([_para("Text")], fonts)
        pushed_bytes, _ = converter._render_word_page(
            [{"type": "spacing", "height": 200}, _para("Text")], fonts
        )

        top_bbox = _ink_bbox(_open(top_bytes))
        pushed_bbox = _ink_bbox(_open(pushed_bytes))
        assert top_bbox is not None and pushed_bbox is not None
        assert pushed_bbox[1] - top_bbox[1] == 200

    def test_a_heading_is_drawn_with_its_heading_font(self):
        """`font_key = f"heading{min(level, 6)}"` is built by string interpolation,
        so a level past 6 would look up a key that does not exist and lose the
        paragraph to the page's error handler. The clamp is what prevents that."""
        converter = _converter(150)
        fonts = converter._load_fonts()

        for level in (1, 6, 9):
            image_bytes, text = converter._render_word_page(
                [
                    _para(
                        f"Heading level {level}",
                        is_heading=True,
                        heading_level=level,
                        space_before=6,
                        space_after=6,
                    )
                ],
                fonts,
            )
            assert text == f"Heading level {level}"
            assert _ink_bbox(_open(image_bytes)) is not None

    def test_a_render_failure_still_returns_every_elements_text(self):
        """A malformed element must not cost the page its text. The fallback reads
        `elem.get("text")`, so the content reaches OCR even with no image."""
        converter = _converter()
        broken = {"type": "paragraph", "text": "IMPORTANT CONTENT"}  # no is_heading

        image_bytes, text = converter._render_word_page(
            [broken, _para("ALSO THIS")], converter._load_fonts()
        )

        assert "IMPORTANT CONTENT" in text
        assert "ALSO THIS" in text
        _open(image_bytes)


class TestRenderParagraph:
    def _blank(
        self, converter: DocumentConverter
    ) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGB", (converter.page_width, converter.page_height), "white")
        return img, ImageDraw.Draw(img)

    def test_alignment_moves_the_ink_across_the_page(self):
        """Alignment is computed from measured text width; if the measurement or
        the arithmetic were wrong the text would be drawn in the wrong place, or
        off the page, and nothing else would notice."""
        converter = _converter(150)
        fonts = converter._load_fonts()
        width = converter.page_width - 2 * converter.margin
        starts: dict[str, int] = {}

        for alignment in ("left", "center", "right"):
            img, draw = self._blank(converter)
            converter._render_formatted_paragraph(
                draw,
                _para("Short", alignment=alignment),
                converter.margin,
                converter.margin,
                width,
                fonts["normal"],
            )
            bbox = _ink_bbox(img)
            assert bbox is not None, f"{alignment} paragraph rendered nothing"
            starts[alignment] = bbox[0]

        assert starts["left"] < starts["center"] < starts["right"]
        # Right-aligned text still lands inside the text area.
        assert starts["right"] < converter.margin + width

    def test_bold_runs_lay_down_more_ink_than_plain_runs(self):
        """Bold is simulated by over-drawing at four offsets; if the offsets were
        dropped, bold would be indistinguishable from plain in the image."""
        converter = _converter(150)
        fonts = converter._load_fonts()

        counts: dict[bool, int] = {}
        for bold in (False, True):
            img, draw = self._blank(converter)
            element = _para(
                "Emphasis",
                runs=[
                    {
                        "text": "Emphasis",
                        "bold": bold,
                        "italic": False,
                        "underline": False,
                        "font_size": None,
                        "font_name": None,
                    }
                ],
            )
            converter._render_formatted_paragraph(
                draw, element, converter.margin, converter.margin, 900, fonts["normal"]
            )
            counts[bold] = _ink_pixels(img)

        assert counts[True] > counts[False]

    def test_an_underlined_run_draws_a_rule_below_the_text(self):
        converter = _converter(150)
        fonts = converter._load_fonts()

        bottoms: dict[bool, int] = {}
        for underline in (False, True):
            img, draw = self._blank(converter)
            element = _para(
                "Underlined",
                runs=[
                    {
                        "text": "Underlined",
                        "bold": False,
                        "italic": False,
                        "underline": underline,
                        "font_size": None,
                        "font_name": None,
                    }
                ],
            )
            converter._render_formatted_paragraph(
                draw, element, converter.margin, converter.margin, 900, fonts["normal"]
            )
            bbox = _ink_bbox(img)
            assert bbox is not None
            bottoms[underline] = bbox[3]

        assert bottoms[True] > bottoms[False]

    def test_consecutive_runs_are_laid_out_side_by_side(self):
        """Runs share a line; if the x cursor did not advance they would be drawn
        on top of each other and the paragraph would be unreadable."""
        converter = _converter(150)
        fonts = converter._load_fonts()

        def render(run_texts: list[str]) -> int:
            img, draw = self._blank(converter)
            element = _para(
                "".join(run_texts),
                runs=[
                    {
                        "text": text,
                        "bold": False,
                        "italic": False,
                        "underline": False,
                        "font_size": None,
                        "font_name": None,
                    }
                    for text in run_texts
                ],
            )
            converter._render_formatted_paragraph(
                draw, element, converter.margin, converter.margin, 2000, fonts["normal"]
            )
            bbox = _ink_bbox(img)
            assert bbox is not None
            return bbox[2]

        assert render(["AAAA", "BBBB"]) > render(["AAAA"])

    def test_an_empty_run_is_skipped_without_consuming_width(self):
        converter = _converter(150)
        fonts = converter._load_fonts()

        def right_edge(run_texts: list[str]) -> int:
            img, draw = self._blank(converter)
            element = _para(
                "AAAA",
                runs=[
                    {
                        "text": text,
                        "bold": False,
                        "italic": False,
                        "underline": False,
                        "font_size": None,
                        "font_name": None,
                    }
                    for text in run_texts
                ],
            )
            converter._render_formatted_paragraph(
                draw, element, converter.margin, converter.margin, 2000, fonts["normal"]
            )
            bbox = _ink_bbox(img)
            assert bbox is not None
            return bbox[2]

        assert right_edge(["", "AAAA"]) == right_edge(["AAAA"])

    def test_runs_that_overrun_the_width_continue_on_a_second_line(self):
        """Without the wrap, a paragraph of many runs is drawn in one row off the
        right edge of the page and everything past the margin is lost from the
        image. The reported height has to grow with the extra rows so the next
        element is not drawn on top of it."""
        converter = _converter(150)
        fonts = converter._load_fonts()
        runs = [
            {
                "text": "WORD ",
                "bold": False,
                "italic": False,
                "underline": False,
                "font_size": None,
                "font_name": None,
            }
            for _ in range(40)
        ]
        img, draw = self._blank(converter)

        height = converter._render_formatted_paragraph(
            draw,
            _para("WORD " * 40, runs=runs),
            converter.margin,
            converter.margin,
            120,
            fonts["normal"],
        )

        assert height > 16, "a wrapped paragraph must report more than one line"
        bbox = _ink_bbox(img)
        assert bbox is not None
        assert bbox[3] > converter.margin + 16, "nothing was drawn on a second row"

    def test_a_malformed_run_falls_back_to_drawing_the_whole_paragraph(self):
        """The fallback is the difference between a page missing one paragraph and
        a page missing that paragraph silently."""
        converter = _converter(150)
        fonts = converter._load_fonts()
        img, draw = self._blank(converter)
        element = _para("RECOVERED TEXT")
        element["runs"] = [{"bold": False, "underline": False}]  # no "text" key

        height = converter._render_formatted_paragraph(
            draw, element, converter.margin, converter.margin, 900, fonts["normal"]
        )

        assert height == 20
        assert _ink_bbox(img) is not None, "fallback drew nothing"


class TestRenderTable:
    def _blank(
        self, converter: DocumentConverter
    ) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGB", (converter.page_width, converter.page_height), "white")
        return img, ImageDraw.Draw(img)

    def _rows(self, n_rows: int, n_cols: int) -> list[list[dict[str, Any]]]:
        return [
            [
                {
                    "text": f"r{r}c{c}",
                    "is_header": r == 0,
                    "bold": r == 0,
                    "alignment": "center" if r == 0 else "left",
                }
                for c in range(n_cols)
            ]
            for r in range(n_rows)
        ]

    def test_the_returned_height_is_the_row_count_times_the_row_height(self):
        """This value advances `y_pos` in `_render_word_page`, so an error here
        makes the next element overlap the table or float away from it."""
        converter = _converter(150)
        _img, draw = self._blank(converter)

        height = converter._render_formatted_table(
            draw,
            self._rows(4, 2),
            converter.margin,
            converter.margin,
            900,
            converter._load_fonts(),
        )
        assert height == 4 * 25

    def test_every_column_receives_ink(self):
        """If the x cursor did not advance per cell, all three columns would be
        drawn over column one and the table would read as a single column."""
        converter = _converter(150)
        img, draw = self._blank(converter)
        width = 900
        converter._render_formatted_table(
            draw,
            self._rows(2, 3),
            converter.margin,
            converter.margin,
            width,
            converter._load_fonts(),
        )

        column_width = width // 3
        for column in range(3):
            band = img.crop(
                (
                    converter.margin + column * column_width + 2,
                    converter.margin,
                    converter.margin + (column + 1) * column_width - 2,
                    converter.margin + 2 * 25,
                )
            )
            assert _ink_bbox(band) is not None, f"column {column} rendered empty"

    def test_a_header_row_gets_a_shaded_band(self):
        """The grey band is the only visual cue telling a model which row names
        the columns."""
        converter = _converter(150)
        img, draw = self._blank(converter)
        converter._render_formatted_table(
            draw,
            self._rows(2, 2),
            converter.margin,
            converter.margin,
            900,
            converter._load_fonts(),
        )

        header_pixel = img.getpixel((converter.margin + 400, converter.margin + 20))
        body_pixel = img.getpixel((converter.margin + 400, converter.margin + 45))
        assert header_pixel == (240, 240, 240)
        assert body_pixel == (255, 255, 255)

    def test_cell_alignment_moves_the_text_within_its_column(self):
        """Numeric Excel cells are right-aligned, which is how a column of figures
        reads as a column. The offset is computed from the measured text width, so
        an error here pushes the text out of its own cell."""
        converter = _converter(150)
        width = 900
        column_width = width // 2
        starts: dict[str, int] = {}

        for alignment in ("left", "center", "right"):
            img, draw = self._blank(converter)
            rows: list[list[dict[str, Any]]] = [
                [
                    {
                        "text": "9",
                        "is_header": False,
                        "bold": False,
                        "alignment": alignment,
                    },
                    {
                        "text": "",
                        "is_header": False,
                        "bold": False,
                        "alignment": "left",
                    },
                ]
            ]
            converter._render_formatted_table(
                draw,
                rows,
                converter.margin,
                converter.margin,
                width,
                converter._load_fonts(),
            )
            # Crop inside the first column, clear of its borders, so only the
            # cell's text is measured.
            band = img.crop(
                (
                    converter.margin + 2,
                    converter.margin + 2,
                    converter.margin + column_width - 2,
                    converter.margin + 23,
                )
            )
            bbox = _ink_bbox(band)
            assert bbox is not None, f"{alignment} cell text not drawn"
            starts[alignment] = bbox[0]

        assert starts["left"] < starts["center"] < starts["right"]

    def test_an_empty_table_occupies_no_height(self):
        converter = _converter(150)
        _img, draw = self._blank(converter)
        assert (
            converter._render_formatted_table(
                draw,
                [],
                converter.margin,
                converter.margin,
                900,
                converter._load_fonts(),
            )
            == 0
        )

    def test_a_malformed_cell_falls_back_to_plain_rows(self):
        """A cell missing its `text` key must not blank the whole table: the
        fallback re-draws every row with `.get`."""
        converter = _converter(150)
        img, draw = self._blank(converter)
        rows: list[list[dict[str, Any]]] = [
            [{"text": "Header"}],
            [{"is_header": False}],  # no "text"
        ]

        height = converter._render_formatted_table(
            draw,
            rows,
            converter.margin,
            converter.margin,
            900,
            converter._load_fonts(),
        )

        assert height == 2 * 20
        assert _ink_bbox(img) is not None, "fallback drew nothing"


class TestRenderFormattedWordContent:
    """`_render_formatted_word_content` has no caller in the tree — `convert_word_to_pages`
    paginates before resolving images instead, so that the page count comes from
    the DOCX metadata. It is covered here because it remains a public-shaped
    helper on the class and would be the obvious thing to reach for."""

    def test_it_paginates_and_renders_every_page(self):
        converter = _converter()
        elements = [_para("ONE"), {"type": "page_break"}, _para("TWO")]

        pages = converter._render_formatted_word_content(elements)

        assert [text for _, text in pages] == ["ONE", "TWO"]
        for image_bytes, _ in pages:
            assert _ink_bbox(_open(image_bytes)) is not None

    def test_it_falls_back_to_plain_text_keeping_every_paragraph(self):
        converter = _converter()
        elements = [_para("KEEP ONE"), _para("KEEP TWO")]

        with patch.object(
            converter, "_calculate_word_page_layout", side_effect=RuntimeError("boom")
        ):
            pages = converter._render_formatted_word_content(elements)

        text = "\n".join(t for _, t in pages)
        assert "KEEP ONE" in text and "KEEP TWO" in text

    def test_no_elements_still_yield_a_page(self):
        pages = _converter()._render_formatted_word_content([])
        assert len(pages) == 1
        _open(pages[0][0])


# ---------------------------------------------------------------------------
# Markdown pager
# ---------------------------------------------------------------------------


class TestMarkdownPager:
    def test_page_text_is_the_original_markdown_not_the_rendered_form(self):
        """Documented contract, and load-bearing: the extraction path reads this
        text as markdown. Stripping `#` or `**` for the image must not strip them
        from the text."""
        markdown = "# Title\n\n- bullet item\n\nsome **bold** words\nplain line"
        pages = _converter()._convert_markdown_to_pages(markdown)

        assert len(pages) == 1
        assert pages[0][1] == markdown

    def test_every_line_of_a_long_document_is_paged_exactly_once(self):
        """The pager advances by `len(page_original_lines)`, so an error there
        either loses a block of lines or repeats one."""
        converter = _converter()
        lines = [f"LINE{i:03d}" for i in range(140)]
        pages = converter._convert_markdown_to_pages("\n".join(lines))

        assert len(pages) > 1
        text = "\n".join(t for _, t in pages)
        for line in lines:
            assert text.count(line) == 1, f"{line} duplicated or lost"
        assert text.split("\n") == lines

    def test_line_budget_matches_the_declared_line_height(self):
        """18 px per line at 72 dpi over a 720 px text area is 40 lines."""
        converter = _converter()
        lines_per_page = (converter.page_height - 2 * converter.margin) // 18
        assert lines_per_page == 40

        pages = converter._convert_markdown_to_pages("\n".join("x" for _ in range(41)))
        assert len(pages) == 2
        assert pages[1][1] == "x"

    def test_headings_bullets_and_bold_all_reach_the_image(self):
        """Each formatting branch chooses a font and an x offset; a branch that
        drew nothing would leave that line missing from the image while still
        present in the text."""
        converter = _converter()
        for line in ("# Heading", "- bullet", "* bullet", "**bold**", "plain"):
            pages = converter._convert_markdown_to_pages(line)
            assert _ink_bbox(_open(pages[0][0])) is not None, f"{line!r} drew nothing"

    def test_a_bullet_is_indented_relative_to_plain_text(self):
        """The bullet branch sets `x_pos = margin + 20`; the `if not
        line.startswith(...)` guard afterwards must not reset it."""
        converter = _converter()
        plain = _ink_bbox(_open(converter._convert_markdown_to_pages("item")[0][0]))
        bullet = _ink_bbox(_open(converter._convert_markdown_to_pages("- item")[0][0]))
        assert plain is not None and bullet is not None
        assert bullet[0] > plain[0]

    def test_empty_markdown_yields_one_page(self):
        pages = _converter()._convert_markdown_to_pages("")
        assert len(pages) == 1
        assert pages[0][1] == ""

    def test_an_over_wide_line_is_wrapped_rather_than_clipped(self):
        """A line wider than the text area must be wrapped onto extra rendered
        rows; without that, the tail of a long markdown cell is drawn off the
        right edge and lost from the image."""
        converter = _converter()
        long_line = " ".join(f"word{i:02d}" for i in range(60))
        pages = converter._convert_markdown_to_pages(long_line)

        bbox = _ink_bbox(_open(pages[0][0]))
        assert bbox is not None
        assert bbox[2] <= converter.page_width - converter.margin + 2
        assert bbox[3] > converter.margin + 18, "expected more than one rendered row"

    def test_a_failure_degrades_to_the_plain_text_converter(self):
        converter = _converter()
        with patch.object(
            converter, "_analyze_table_structure", side_effect=RuntimeError("boom")
        ):
            pages = converter._convert_markdown_to_pages("RECOVER ME")
        assert "RECOVER ME" in pages[0][1]

    def test_a_missing_monospace_font_does_not_stop_paging(self):
        """The three named faces this pager asks for are absent on hosts outside
        the Debian font layout, which is the ordinary case, so the OSError branch
        is the one that runs in production."""
        converter = _converter()
        with patch.object(dc_module.ImageFont, "truetype", _named_fonts_unavailable()):
            pages = converter._convert_markdown_to_pages("# Title\nbody line")

        assert pages[0][1] == "# Title\nbody line"
        assert _ink_bbox(_open(pages[0][0])) is not None

    def test_a_continuation_page_fits_its_repeated_header_within_the_budget(self):
        """A continuation page must not be handed more lines than it can render.

        `_ensure_table_headers` prepended two lines to a continuation page without
        reducing that page's line budget, so a **full** interior page carried
        `lines_per_page + 2` lines onto a canvas that renders `lines_per_page`, and the
        `break  # Page is full` guard dropped the overflow from the IMAGE while it
        stayed in the page text. `ocr/service.py` builds its OCR blocks from the page
        text, so text-based extraction was unaffected and nothing reported the
        discrepancy; a vision-capable classification or extraction call shown that page
        did not see its last rows (#1158).

        ⚠️ **This needs a table long enough to FILL an interior page.** A 100-row CSV
        does not reproduce it — measured, its page 2 holds 21 of 83 lines, so the two
        extra fit and before and after are identical. 300 rows makes the interior pages
        full, where the old code handed them 85 lines against a canvas rendering 83.
        """
        converter = _converter()
        content = "id,label\n" + "\n".join(f"{i},L{i:03d}" for i in range(300))

        pages = converter.convert_csv_to_pages(content)
        assert len(pages) >= 3, "need an interior page for this to say anything"

        # Conservation first: no row may be lost or duplicated at a seam.
        all_text = "\n".join(t for _, t in pages)
        for i in range(300):
            assert all_text.count(f"L{i:03d}") == 1, f"row L{i:03d} duplicated or lost"

        lines_per_page = (converter.page_height - 2 * converter.margin) // 18
        for page_number, (_, page_text) in enumerate(pages, start=1):
            handed = len(page_text.split("\n"))
            assert handed <= lines_per_page, (
                f"page {page_number} was handed {handed} lines onto a canvas that "
                f"renders {lines_per_page}; the excess is dropped from the image while "
                "remaining in the page text, which no text assertion can detect"
            )

        # And the interior page really does repeat the header, which is the feature
        # whose budget this is: a fix that simply stopped prepending would satisfy the
        # assertion above and lose the header instead. Matched on the column names
        # rather than a literal prefix, because the pandas path pads cells to width.
        first_line = pages[1][1].split("\n")[0]
        assert first_line.startswith("|") and "id" in first_line, first_line
        assert "label" in first_line, first_line

    def test_a_wrapped_line_at_the_foot_of_a_page_stops_at_the_margin(self):
        """The inner break: a line that wraps into more rows than are left must
        stop at the bottom margin rather than being drawn off the canvas, where it
        would appear as a band of cut-off glyphs along the page edge."""
        converter = _converter()
        lines_per_page = (converter.page_height - 2 * converter.margin) // 18
        long_line = " ".join(f"w{i:02d}" for i in range(200))
        content = "\n".join(["filler"] * (lines_per_page - 1) + [long_line])

        pages = converter._convert_markdown_to_pages(content)

        bbox = _ink_bbox(_open(pages[0][0]))
        assert bbox is not None
        assert bbox[3] <= converter.page_height - converter.margin
        # The whole line is still in the page text.
        assert long_line in pages[0][1]


class TestTableStructureAnalysis:
    def test_two_tables_are_found_with_their_ranges(self):
        lines = [
            "intro",
            "| a | b |",
            "| --- | --- |",
            "| 1 | 2 |",
            "",
            "| c | d |",
            "| --- | --- |",
            "| 3 | 4 |",
            "| 5 | 6 |",
        ]

        info = _converter()._analyze_table_structure(lines)

        assert info["table_ranges"] == [(1, 3), (5, 8)]
        assert [header_idx for header_idx, _h, _s in info["headers"]] == [1, 5]
        assert info["headers"][0][1] == "| a | b |"
        assert info["headers"][0][2] == "| --- | --- |"

    def test_a_pipe_row_without_a_separator_is_not_a_table(self):
        """Otherwise any prose line containing a pipe would be treated as a table
        header and duplicated onto later pages."""
        info = _converter()._analyze_table_structure(
            ["| not | a | table |", "still prose"]
        )
        assert info["table_ranges"] == []

    def test_a_separator_without_pipes_is_not_a_table(self):
        info = _converter()._analyze_table_structure(
            ["| a | b |", "-----", "| 1 | 2 |"]
        )
        assert info["table_ranges"] == []

    def test_a_header_at_the_very_last_line_is_not_a_table(self):
        info = _converter()._analyze_table_structure(["| a | b |"])
        assert info["table_ranges"] == []

    def test_no_tables_in_plain_text(self):
        info = _converter()._analyze_table_structure(["one", "two", "three"])
        assert info == {"headers": [], "table_ranges": []}


class TestEnsureTableHeaders:
    def _info(self) -> dict[str, Any]:
        return {
            "headers": [(0, "| a | b |", "| --- | --- |")],
            "table_ranges": [(0, 50)],
        }

    def test_a_page_starting_mid_table_is_given_the_header_back(self):
        """Without this, the continuation page is a body of rows with no column
        names — unparseable as markdown and ambiguous to a model."""
        page = ["| 41 | 42 |", "| 43 | 44 |"]
        result = _converter()._ensure_table_headers(page, self._info(), 41)

        assert result == ["| a | b |", "| --- | --- |"] + page

    def test_a_page_starting_at_the_table_header_is_left_alone(self):
        """It already contains the header; prepending would duplicate it."""
        page = ["| a | b |", "| --- | --- |", "| 1 | 2 |"]
        assert _converter()._ensure_table_headers(page, self._info(), 0) == page

    def test_a_page_starting_after_the_table_is_left_alone(self):
        page = ["prose after the table"]
        assert _converter()._ensure_table_headers(page, self._info(), 90) == page

    def test_an_empty_page_or_a_document_with_no_tables_is_left_alone(self):
        converter = _converter()
        assert converter._ensure_table_headers([], self._info(), 41) == []
        assert converter._ensure_table_headers(
            ["x"], {"headers": [], "table_ranges": []}, 41
        ) == ["x"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestTextWrapping:
    def test_no_word_is_lost_or_reordered_by_wrapping(self):
        converter = _converter()
        img = Image.new("RGB", (100, 100), "white")
        draw = ImageDraw.Draw(img)
        text = " ".join(f"word{i:02d}" for i in range(40))

        wrapped = converter._wrap_text_to_width(
            text, ImageFont.load_default(), 120, draw
        )

        assert len(wrapped) > 1, "expected the text to be wrapped"
        assert " ".join(wrapped).split() == text.split()

    def test_a_single_word_wider_than_the_limit_is_still_emitted(self):
        """The `or not current_line` clause. Without it an over-long token would
        be dropped rather than overflowing."""
        converter = _converter()
        draw = ImageDraw.Draw(Image.new("RGB", (100, 100), "white"))
        word = "X" * 200

        assert converter._wrap_text_to_width(
            word, ImageFont.load_default(), 10, draw
        ) == [word]

    def test_whitespace_only_text_is_returned_unchanged(self):
        """Returning `[]` here would delete a blank markdown line and close up the
        vertical structure of the page."""
        converter = _converter()
        draw = ImageDraw.Draw(Image.new("RGB", (100, 100), "white"))
        assert converter._wrap_text_to_width(
            "   ", ImageFont.load_default(), 500, draw
        ) == ["   "]

    def test_text_that_fits_is_left_on_one_line(self):
        converter = _converter()
        draw = ImageDraw.Draw(Image.new("RGB", (100, 100), "white"))
        assert converter._wrap_text_to_width(
            "short enough", ImageFont.load_default(), 5000, draw
        ) == ["short enough"]


class TestTextWidth:
    def test_width_grows_with_the_text(self):
        converter = _converter()
        draw = ImageDraw.Draw(Image.new("RGB", (400, 100), "white"))
        font = ImageFont.load_default()

        one = converter._get_text_width(draw, "M", font)
        many = converter._get_text_width(draw, "MMMMMMMM", font)
        assert 0 < one < many

    def test_it_falls_back_to_textsize_when_textbbox_is_absent(self):
        """The ladder exists for older Pillow releases; the middle rung has to
        return the width component, not the whole tuple."""

        class _OldDraw:
            def textsize(self, _text: str, font: Any = None) -> tuple[int, int]:
                return (123, 9)

        assert _converter()._get_text_width(_OldDraw(), "anything", None) == 123

    def test_it_estimates_from_length_when_neither_method_exists(self):
        """The last rung keeps alignment and wrapping working rather than raising
        out of the whole render."""
        assert _converter()._get_text_width(object(), "abcde", None) == 40


class TestFontLoading:
    def test_every_font_key_the_renderer_indexes_is_present(self):
        """`_render_word_page` does `fonts["normal"]` unguarded and builds
        `heading1`..`heading6` keys by name, so a missing key raises out of the
        page render and costs the page its image."""
        fonts = _converter()._load_fonts()
        assert set(fonts) == {
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "heading6",
            "normal",
            "small",
        }
        for font in fonts.values():
            assert font.getbbox("Mg") is not None

    def test_the_keys_survive_a_host_with_none_of_the_expected_font_paths(self):
        """This is the live situation on hosts whose fonts are not in the Debian
        layout the path list assumes, so the `load_default` branch is the one that
        actually runs."""
        with patch.object(dc_module.os.path, "exists", return_value=False):
            fonts = _converter()._load_fonts()
        assert len(fonts) == 8
        assert all(font is not None for font in fonts.values())

    def test_a_usable_system_font_gives_headings_a_real_size_hierarchy(self):
        """The point of loading a TrueType face: headings must render larger than
        body text. With no face available every size collapses to the same bitmap
        default and the hierarchy is lost."""
        real_truetype = ImageFont.truetype

        def fake_truetype(_path: Any, size: int) -> Any:
            return real_truetype("DejaVuSansMono.ttf", size)

        with (
            patch.object(dc_module.os.path, "exists", return_value=True),
            patch.object(dc_module.ImageFont, "truetype", fake_truetype),
        ):
            fonts = _converter()._load_fonts()

        sizes = {name: font.size for name, font in fonts.items()}  # pyright: ignore[reportAttributeAccessIssue]
        assert sizes == {
            "heading1": 24,
            "heading2": 20,
            "heading3": 18,
            "heading4": 16,
            "heading5": 14,
            "heading6": 13,
            "normal": 12,
            "small": 10,
        }

    def test_a_font_path_that_exists_but_will_not_load_does_not_break_loading(self):
        """A present-but-unreadable face is the awkward case: the path check
        succeeds so the code commits to it, and only `truetype` fails."""
        with (
            patch.object(dc_module.os.path, "exists", return_value=True),
            patch.object(dc_module.ImageFont, "truetype", _named_fonts_unavailable()),
        ):
            fonts = _converter()._load_fonts()
        assert len(fonts) == 8
        assert all(font.getbbox("Mg") is not None for font in fonts.values())

    def test_an_unreadable_font_directory_does_not_break_loading(self):
        with patch.object(
            dc_module.os.path, "exists", side_effect=OSError("permission denied")
        ):
            fonts = _converter()._load_fonts()
        assert len(fonts) == 8


class TestEmptyPage:
    def test_it_is_a_valid_white_jpeg_of_the_canvas_size(self):
        """Every failure handler in the module returns this, and it is uploaded to
        S3 and handed to Bedrock, so it has to be a real image at the page size."""
        converter = _converter()
        img = _open(converter._create_empty_page())

        assert img.format == "JPEG"
        assert img.size == (converter.page_width, converter.page_height)
        assert _ink_bbox(img) is None

    def test_it_scales_with_the_converters_dpi(self):
        assert _open(_converter(300)._create_empty_page()).size == (2550, 3300)

    def test_the_last_resort_bytes_decode(self):
        """The literal that ships when image creation is impossible must DECODE.

        `load()` is the assertion, not `open()` (#1158). The previous literal parsed
        as a 1x1 JPEG and then raised `OSError: broken data stream` on decode, because
        its `SOF0` marker declared one component while the component specifications
        that followed and the `SOS` marker described three, and its scan was a single
        byte. A header-only validity check therefore passed and the file was uploaded
        to S3 as an apparently valid page image, failing later in whatever first
        decoded it — Textract, a Bedrock image block, or the UI's page preview — with
        an error pointing at the consumer rather than at the producer."""
        converter = _converter()
        with patch.object(dc_module.Image, "new", side_effect=OSError("no canvas")):
            img_bytes = converter._create_empty_page()

        assert img_bytes.startswith(b"\xff\xd8") and img_bytes.endswith(b"\xff\xd9")
        img = Image.open(io.BytesIO(img_bytes))
        assert (img.format, img.size) == ("JPEG", (1, 1))
        img.load()  # the whole point: this used to raise

    def test_the_last_resort_constant_itself_decodes(self):
        """Asserted on the constant as well as through the fallback path, so a future
        edit to the literal is caught even if nothing exercises `_create_empty_page`'s
        deepest rung. It is produced by Pillow rather than hand-assembled for the same
        reason."""
        img = Image.open(io.BytesIO(dc_module._MINIMAL_WHITE_JPEG))
        img.load()
        assert (img.format, img.size) == ("JPEG", (1, 1))

    def test_a_zero_byte_save_falls_through_to_a_minimal_jpeg(self):
        """The middle rung of the ladder: the full-size save produced nothing, so
        a 1x1 image is built instead. It still has to be decodable."""
        converter = _converter()
        real_new = dc_module.Image.new

        class _SilentImage:
            def save(self, _buffer: Any, **_kwargs: Any) -> None:
                return None

        calls: list[int] = []

        def new(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return _SilentImage()
            return real_new(*args, **kwargs)

        with patch.object(dc_module.Image, "new", new):
            img_bytes = converter._create_empty_page()

        img = _open(img_bytes)
        assert img.size == (1, 1)
        assert len(calls) == 2, "the minimal-image rung was not reached"


class TestConverterGeometry:
    @pytest.mark.parametrize(
        ("dpi", "expected"),
        [
            # dpi, (width px, height px, margin px) for US Letter with 0.5" margins
            (72, (612, 792, 36)),
            (150, (1275, 1650, 75)),
            (300, (2550, 3300, 150)),
            (600, (5100, 6600, 300)),
        ],
    )
    def test_the_canvas_is_us_letter_at_the_requested_dpi(
        self, dpi: int, expected: tuple[int, int, int]
    ):
        """Page dimensions feed every layout calculation and the image handed to
        Textract, whose ability to read small print degrades with resolution. The
        expected pixel counts are written out rather than recomputed from the same
        formula the code uses, so a change to the paper size is visible."""
        converter = DocumentConverter(dpi=dpi)
        assert (converter.page_width, converter.page_height, converter.margin) == (
            expected
        )
        rendered = _open(converter._create_empty_page())
        assert rendered.size == (expected[0], expected[1])

    def test_the_default_dpi_is_150(self):
        """`OcrService` overrides this, but a direct caller gets the default and
        the sectPr geometry math is hardcoded to match it."""
        assert DocumentConverter().dpi == 150


def test_the_module_does_not_depend_on_import_order_of_optional_readers():
    """`pandas` and `python-docx` are imported inside the functions that need
    them, which is what lets a Lambda built with only the `[ocr]` extra import
    this module at all. A top-level import would break those packages at import
    time rather than at use."""
    source = os.path.join(os.path.dirname(dc_module.__file__), "document_converter.py")
    with open(source, encoding="utf-8") as handle:
        header = handle.read().split("class UnsupportedLegacyFormatError")[0]

    assert "import pandas" not in header
    assert "from docx" not in header
    assert "import docx" not in header
