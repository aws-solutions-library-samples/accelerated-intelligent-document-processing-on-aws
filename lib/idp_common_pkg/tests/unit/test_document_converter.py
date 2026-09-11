# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import (  # nosec B405 - constructing XML in-memory for tests, no parsing
    Element,
    SubElement,
)

import pytest

from idp_common.ocr.document_converter import DocumentConverter


@pytest.mark.unit
def test_document_converter_initialization():
    """Test DocumentConverter initialization."""
    converter = DocumentConverter(dpi=150)
    assert converter.dpi == 150
    assert converter.page_width == int(8.5 * 150)
    assert converter.page_height == int(11 * 150)


@pytest.mark.unit
def test_convert_text_to_pages():
    """Test text to pages conversion."""
    converter = DocumentConverter(dpi=72)  # Lower DPI for faster testing

    # Test simple text
    text = "Hello World\nThis is a test"
    pages = converter.convert_text_to_pages(text)

    assert len(pages) >= 1
    assert isinstance(pages[0], tuple)
    assert len(pages[0]) == 2
    assert isinstance(pages[0][0], bytes)  # Image bytes
    assert isinstance(pages[0][1], str)  # Text content


@pytest.mark.unit
def test_convert_csv_to_pages():
    """Test CSV to pages conversion."""
    converter = DocumentConverter(dpi=72)

    csv_content = "Name,Age,City\nJohn,25,NYC\nJane,30,LA"
    pages = converter.convert_csv_to_pages(csv_content)

    assert len(pages) >= 1
    assert isinstance(pages[0][0], bytes)
    assert "Name" in pages[0][1]
    assert "John" in pages[0][1]


@pytest.mark.unit
def test_convert_legacy_xls_to_pages():
    """A legacy .xls workbook is actually read, not silently blanked.

    Without `xlrd` declared in the `[ocr]` extra, `pd.read_excel` raises
    "Missing optional dependency 'xlrd'" for the BIFF format, the handler at the
    bottom of `convert_excel_to_pages` swallows it, and the document completes as a
    single empty page reading "Error reading Excel file" — so a format that is
    advertised in the changelog, accepted by the upload picker, and routed by
    `ocr/service.py` produced no content. An import assertion would not catch this;
    only reading a real .xls file does.

    The fixture is synthetic, generated with `xlwt` (two sheets, six cells).
    """
    converter = DocumentConverter(dpi=72)

    fixture = Path(__file__).parent / "fixtures" / "two_sheets.xls"
    pages = converter.convert_excel_to_pages(fixture.read_bytes())

    all_text = " ".join(text for _, text in pages)
    assert "Error reading Excel file" not in all_text
    # Content from both sheets survives the conversion.
    assert "Widget" in all_text
    assert "GrandTotal" in all_text


@pytest.mark.unit
def test_format_csv_as_table():
    """Test CSV table formatting."""
    converter = DocumentConverter()

    rows = [["Name", "Age"], ["John", "25"], ["Jane", "30"]]
    formatted = converter._format_csv_as_table(rows)

    assert "Name" in formatted
    assert "John" in formatted
    assert "|" in formatted  # Table separator


@pytest.mark.unit
def test_create_empty_page():
    """Test empty page creation."""
    converter = DocumentConverter(dpi=72)

    empty_page = converter._create_empty_page()
    assert isinstance(empty_page, bytes)
    assert len(empty_page) > 0


# ---------------------------------------------------------------------------
# DOCX image extraction tests
# ---------------------------------------------------------------------------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _make_text_paragraph(text: str) -> Element:
    """Build a minimal <w:p> XML element containing a text run."""
    p = Element(f"{{{_W_NS}}}p")
    r = SubElement(p, f"{{{_W_NS}}}r")
    t = SubElement(r, f"{{{_W_NS}}}t")
    t.text = text
    return p


def _make_image_paragraph(embed_id: str = "rId1") -> Element:
    """Build a minimal <w:p> XML element containing an image drawing."""
    p = Element(f"{{{_W_NS}}}p")
    r = SubElement(p, f"{{{_W_NS}}}r")
    drawing = SubElement(r, f"{{{_W_NS}}}drawing")
    inline = SubElement(drawing, "wp:inline")
    graphic = SubElement(inline, f"{{{_A_NS}}}graphic")
    gd = SubElement(graphic, f"{{{_A_NS}}}graphicData")
    pic = SubElement(gd, "pic:pic")
    blipFill = SubElement(pic, "pic:blipFill")
    blip = SubElement(blipFill, f"{{{_A_NS}}}blip")
    blip.set(f"{{{_R_NS}}}embed", embed_id)
    return p


def _make_table_element() -> Element:
    """Build a minimal <w:tbl> XML element."""
    return Element(f"{{{_W_NS}}}tbl")


def _make_mock_doc(body_children, image_cache=None):
    """Create a mock python-docx Document with the given body children."""
    if image_cache is None:
        image_cache = {}

    doc = MagicMock()

    # Set up body element with children
    body = MagicMock()
    body.__iter__ = MagicMock(return_value=iter(body_children))
    doc.element.body = body

    # Set up relationships for image cache
    rels = {}
    for rel_id, img_bytes in image_cache.items():
        rel = MagicMock()
        rel.reltype = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        )
        rel.target_part.blob = img_bytes
        rels[rel_id] = rel
    doc.part.rels = rels

    # Set up paragraphs fallback (for error handling)
    doc.paragraphs = []

    return doc


@pytest.mark.unit
def test_extract_word_formatting_text_only():
    """Test _extract_word_formatting with text-only document."""
    converter = DocumentConverter(dpi=72)
    p_elem = _make_text_paragraph("Hello World")
    doc = _make_mock_doc([p_elem])

    with patch("docx.text.paragraph.Paragraph") as MockParagraph:
        mock_para = MagicMock()
        mock_para.text = "Hello World"
        mock_para.style.name = "Normal"
        mock_para.alignment = None
        mock_run = MagicMock()
        mock_run.text = "Hello World"
        mock_run.bold = False
        mock_run.italic = False
        mock_run.underline = False
        mock_run.font.size = None
        mock_run.font.name = None
        mock_para.runs = [mock_run]
        MockParagraph.return_value = mock_para

        elements, page_geometry = converter._extract_word_formatting(doc)

    # Should have paragraph element, no image elements
    para_elements = [e for e in elements if e["type"] == "paragraph"]
    image_elements = [e for e in elements if e["type"] == "image"]
    assert len(para_elements) == 1
    assert len(image_elements) == 0
    assert para_elements[0]["text"] == "Hello World"
    # page_geometry should have defaults
    assert "usable_height_px" in page_geometry


@pytest.mark.unit
def test_extract_word_formatting_with_images():
    """Test _extract_word_formatting detects images in paragraphs."""
    converter = DocumentConverter(dpi=72)

    # Create body with text paragraph, image paragraph, text paragraph
    p_text1 = _make_text_paragraph("Before image")
    p_image = _make_image_paragraph("rId1")
    p_text2 = _make_text_paragraph("After image")

    fake_image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG header
    doc = _make_mock_doc(
        [p_text1, p_image, p_text2],
        image_cache={"rId1": fake_image_bytes},
    )

    with patch("docx.text.paragraph.Paragraph") as MockParagraph:

        def make_para(elem, parent):
            mock_para = MagicMock()
            # Determine if this paragraph has text
            t_elems = elem.findall(f".//{{{_W_NS}}}t")
            text = "".join(t.text or "" for t in t_elems)
            mock_para.text = text
            mock_para.style.name = "Normal"
            mock_para.alignment = None
            if text.strip():
                mock_run = MagicMock()
                mock_run.text = text
                mock_run.bold = False
                mock_run.italic = False
                mock_run.underline = False
                mock_run.font.size = None
                mock_run.font.name = None
                mock_para.runs = [mock_run]
            else:
                mock_para.runs = []
            return mock_para

        MockParagraph.side_effect = make_para

        elements, _geom = converter._extract_word_formatting(doc)

    types = [e["type"] for e in elements]
    assert "image" in types, "Should detect image elements"
    assert "paragraph" in types, "Should detect paragraph elements"

    # Verify image has bytes
    image_elements = [e for e in elements if e["type"] == "image"]
    assert len(image_elements) == 1
    assert image_elements[0]["image_bytes"] == fake_image_bytes


@pytest.mark.unit
def test_resolve_image_elements_with_callback():
    """Test _resolve_image_elements calls OCR callback for images."""
    converter = DocumentConverter(dpi=72)

    elements = [
        {
            "type": "paragraph",
            "text": "Before",
            "style": "Normal",
            "is_heading": False,
            "heading_level": 0,
            "alignment": "left",
            "runs": [
                {
                    "text": "Before",
                    "bold": False,
                    "italic": False,
                    "underline": False,
                    "font_size": None,
                    "font_name": None,
                }
            ],
            "space_before": 3,
            "space_after": 3,
        },
        {"type": "image", "image_bytes": b"fake-image-data"},
        {
            "type": "paragraph",
            "text": "After",
            "style": "Normal",
            "is_heading": False,
            "heading_level": 0,
            "alignment": "left",
            "runs": [
                {
                    "text": "After",
                    "bold": False,
                    "italic": False,
                    "underline": False,
                    "font_size": None,
                    "font_name": None,
                }
            ],
            "space_before": 3,
            "space_after": 3,
        },
    ]

    callback = MagicMock(return_value="OCR extracted text from image")
    resolved = converter._resolve_image_elements(elements, callback)

    # Callback should have been called with the image bytes
    callback.assert_called_once_with(b"fake-image-data")

    # No image elements should remain
    image_elements = [e for e in resolved if e["type"] == "image"]
    assert len(image_elements) == 0

    # OCR text should appear as a paragraph
    all_text = " ".join(e.get("text", "") for e in resolved if e.get("text"))
    assert "OCR extracted text from image" in all_text


@pytest.mark.unit
def test_resolve_image_elements_without_callback():
    """Test _resolve_image_elements uses placeholder when no callback."""
    converter = DocumentConverter(dpi=72)

    elements = [
        {"type": "image", "image_bytes": b"fake-image-data"},
    ]

    resolved = converter._resolve_image_elements(elements, ocr_image_callback=None)

    # Should have placeholder text
    para_elements = [e for e in resolved if e["type"] == "paragraph"]
    assert len(para_elements) >= 1
    assert any("[Image]" in e["text"] for e in para_elements)


@pytest.mark.unit
def test_resolve_image_elements_callback_failure():
    """Test _resolve_image_elements handles callback failure gracefully."""
    converter = DocumentConverter(dpi=72)

    elements = [
        {"type": "image", "image_bytes": b"fake-image-data"},
    ]

    callback = MagicMock(side_effect=Exception("OCR service unavailable"))
    resolved = converter._resolve_image_elements(elements, callback)

    # Should have fallback text
    para_elements = [e for e in resolved if e["type"] == "paragraph"]
    assert len(para_elements) >= 1
    assert any("OCR failed" in e["text"] for e in para_elements)


@pytest.mark.unit
def test_convert_word_to_pages_with_ocr_callback():
    """Test convert_word_to_pages passes OCR callback through."""
    converter = DocumentConverter(dpi=72)

    callback = MagicMock(return_value="Text from image")
    page_geometry = converter._default_page_geometry()

    with (
        patch.object(converter, "_extract_word_formatting") as mock_extract,
        patch.object(converter, "_resolve_image_elements") as mock_resolve,
        patch.object(converter, "_render_word_page") as mock_render,
        patch("docx.Document"),
    ):
        mock_extract.return_value = (
            [{"type": "image", "image_bytes": b"img", "display_height_px": 100}],
            page_geometry,
        )
        mock_resolve.return_value = [
            {
                "type": "paragraph",
                "text": "Text from image",
                "style": "Normal",
                "is_heading": False,
                "heading_level": 0,
                "alignment": "left",
                "runs": [
                    {
                        "text": "Text from image",
                        "bold": False,
                        "italic": False,
                        "underline": False,
                        "font_size": None,
                        "font_name": None,
                    }
                ],
                "space_before": 2,
                "space_after": 2,
            }
        ]
        mock_render.return_value = (b"page-image", "Text from image")

        result = converter.convert_word_to_pages(
            b"fake-docx", ocr_image_callback=callback
        )

    mock_resolve.assert_called_once()
    # Verify callback was passed to _resolve_image_elements
    args = mock_resolve.call_args
    assert args[0][1] is callback
    assert result == [(b"page-image", "Text from image")]


@pytest.mark.unit
def test_build_table_element():
    """Test _build_table_element preserves table formatting."""
    mock_table = MagicMock()

    # Create mock rows and cells
    header_row = MagicMock()
    data_row = MagicMock()
    mock_table.rows = [header_row, data_row]

    header_cell1 = MagicMock()
    header_cell1.text = "Name"
    header_cell2 = MagicMock()
    header_cell2.text = "Value"
    header_row.cells = [header_cell1, header_cell2]

    data_cell1 = MagicMock()
    data_cell1.text = "Field1"
    data_cell2 = MagicMock()
    data_cell2.text = "Data1"
    data_row.cells = [data_cell1, data_cell2]

    result = DocumentConverter._build_table_element(mock_table)

    assert result is not None
    assert result["type"] == "table"
    assert len(result["data"]) == 2  # header + data row
    assert result["data"][0][0]["text"] == "Name"
    assert result["data"][0][0]["is_header"] is True
    assert result["data"][0][0]["bold"] is True
    assert result["data"][1][0]["text"] == "Field1"
    assert result["data"][1][0]["is_header"] is False


# ---------------------------------------------------------------------------
# Page layout calculation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calculate_word_page_layout_with_page_breaks():
    """Test _calculate_word_page_layout respects explicit page_break elements."""
    converter = DocumentConverter(dpi=150)
    geometry = {"usable_height_px": 1350, "usable_width_px": 975}

    elements = [
        _simple_para("Page 1 text"),
        {"type": "page_break"},
        _simple_para("Page 2 text"),
    ]

    pages = converter._calculate_word_page_layout(elements, geometry)
    assert len(pages) == 2
    assert pages[0][0]["text"] == "Page 1 text"
    assert pages[1][0]["text"] == "Page 2 text"


@pytest.mark.unit
def test_calculate_word_page_layout_image_display_height():
    """Test that image display_height_px drives page overflow correctly."""
    converter = DocumentConverter(dpi=150)
    # Usable height = 500px for easy arithmetic
    geometry = {"usable_height_px": 500, "usable_width_px": 975}

    elements = [
        {"type": "image", "image_bytes": b"img1", "display_height_px": 300},
        {"type": "image", "image_bytes": b"img2", "display_height_px": 300},
    ]

    pages = converter._calculate_word_page_layout(elements, geometry)
    # 300 + 300 = 600 > 500, so second image overflows to page 2
    assert len(pages) == 2


@pytest.mark.unit
def test_calculate_word_page_layout_images_fit_one_page():
    """Two small images fitting on one page stay on one page."""
    converter = DocumentConverter(dpi=150)
    geometry = {"usable_height_px": 800, "usable_width_px": 975}

    elements = [
        {"type": "image", "image_bytes": b"img1", "display_height_px": 300},
        {"type": "image", "image_bytes": b"img2", "display_height_px": 300},
    ]

    pages = converter._calculate_word_page_layout(elements, geometry)
    assert len(pages) == 1


@pytest.mark.unit
def test_estimate_element_height_image_uses_display_height():
    """_estimate_element_height should use display_height_px for images."""
    elem = {"type": "image", "image_bytes": b"x", "display_height_px": 650}
    assert DocumentConverter._estimate_element_height(elem) == 650


@pytest.mark.unit
def test_estimate_element_height_image_fallback():
    """_estimate_element_height should use sensible fallback when no display height."""
    elem = {"type": "image", "image_bytes": b"x", "display_height_px": 0}
    assert DocumentConverter._estimate_element_height(elem) == 200


def _simple_para(text: str) -> dict:
    """Helper to build a minimal paragraph element dict for layout tests."""
    return {
        "type": "paragraph",
        "text": text,
        "style": "Normal",
        "is_heading": False,
        "heading_level": 0,
        "alignment": "left",
        "runs": [
            {
                "text": text,
                "bold": False,
                "italic": False,
                "underline": False,
                "font_size": None,
                "font_name": None,
            }
        ],
        "space_before": 3,
        "space_after": 3,
    }
