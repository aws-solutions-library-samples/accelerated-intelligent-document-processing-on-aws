# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Tests for ``ocr.mark_handwriting``.

These use the real ``textractor`` library on synthetic Textract responses, so
they cover the actual markdown conversion (plain lines and table cells), not a
mock of it.

The fixtures follow the response shape in the Textract developer guide, "Lines and
words of text"
(https://docs.aws.amazon.com/textract/latest/dg/how-it-works-lines-words.html):
``TextType`` is on the WORD blocks and not on the LINE that owns them, so
handwriting is read from a LINE's WORD children.
``test_lines_carry_no_texttype_like_real_textract`` pins that fixture shape.
"""

import copy
import inspect
import io
import logging
import re
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.models import IDPConfig, OCRConfig
from idp_common.ocr import service as service_module
from idp_common.ocr.service import (
    HANDWRITING_LEGEND,
    HANDWRITING_MARKER,
    OCR_NOTE_HEADERS,
    SIGNATURE_SUMMARY_HEADER,
    OcrService,
)

pytestmark = pytest.mark.unit

P = "PRINTED"
H = "HANDWRITING"


def _geometry(left, top, width=0.07, height=0.03):
    return {
        "BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": height},
        "Polygon": [
            {"X": left, "Y": top},
            {"X": left + width, "Y": top},
            {"X": left + width, "Y": top + height},
            {"X": left, "Y": top + height},
        ],
    }


def _line_blocks(key, words, top):
    """A LINE block and its WORD blocks.

    ``words`` is a list of (text, TextType) pairs. As in real Textract output,
    the TextType goes on the WORD blocks.
    """
    word_ids = [f"{key}_w{i}" for i in range(len(words))]
    line = {
        "BlockType": "LINE",
        "Id": key,
        "Text": " ".join(text for text, _ in words),
        "Confidence": 91.0,
        "Page": 1,
        "Geometry": _geometry(0.1, top, 0.5),
        "Relationships": [{"Type": "CHILD", "Ids": word_ids}],
    }
    blocks = [line]
    for i, (text, text_type) in enumerate(words):
        blocks.append(
            {
                "BlockType": "WORD",
                "Id": word_ids[i],
                "Text": text,
                "Confidence": 91.0,
                "TextType": text_type,
                "Page": 1,
                "Geometry": _geometry(0.1 + 0.08 * i, top),
            }
        )
    return blocks


def _words(text, text_type):
    return [(w, text_type) for w in text.split()]


def _response(lines):
    """A one-page Textract response from (words, top) lines."""
    blocks = [
        {
            "BlockType": "PAGE",
            "Id": "page",
            "Page": 1,
            "Geometry": _geometry(0, 0, 1, 1),
        }
    ]
    line_ids = []
    for index, (words, top) in enumerate(lines):
        key = f"line{index}"
        line_ids.append(key)
        blocks.extend(_line_blocks(key, words, top))
    blocks[0]["Relationships"] = [{"Type": "CHILD", "Ids": line_ids}]
    return {"DocumentMetadata": {"Pages": 1}, "Blocks": blocks}


MIXED_PAGE = [
    (_words("Monthly Card Statement", P), 0.05),
    (_words("Account holder: Jane Smith", P), 0.12),
    (_words("Please update my name on my account", H), 0.30),
    (_words("Jane Smlth", H), 0.34),
]


def _service(mark_handwriting, backend="textract"):
    config = IDPConfig(
        ocr=OCRConfig(backend=backend, mark_handwriting=mark_handwriting)
    )
    with patch("boto3.client"):
        return OcrService(config=config)


def _text(mark, response):
    return _service(mark)._parse_textract_response(response, 1)["text"]


class TestConfig:
    def test_off_by_default(self):
        assert OCRConfig().mark_handwriting is False
        assert IDPConfig().ocr.mark_handwriting is False
        assert _service(False).mark_handwriting is False

    @pytest.mark.parametrize("stored", ["true", "True", True])
    def test_accepts_stored_string_values(self, stored):
        assert IDPConfig(ocr={"mark_handwriting": stored}).ocr.mark_handwriting is True

    def test_service_reads_the_setting(self):
        assert _service(True).mark_handwriting is True

    def test_the_deprecated_parameters_leave_it_off(self):
        with patch("boto3.client"):
            service = OcrService(backend="textract", enhanced_features=["TABLES"])
        assert service.mark_handwriting is False

    def test_warns_when_backend_cannot_report_handwriting(self, caplog):
        with caplog.at_level(logging.WARNING, logger="idp_common.ocr.service"):
            _service(True, backend="none")
        assert any("mark_handwriting" in r.getMessage() for r in caplog.records)

    def test_no_warning_for_textract(self, caplog):
        with caplog.at_level(logging.WARNING, logger="idp_common.ocr.service"):
            _service(True)
        assert not any("mark_handwriting" in r.getMessage() for r in caplog.records)


class TestNoteHeaders:
    def test_every_note_header_in_the_module_is_listed(self):
        """OCR_NOTE_HEADERS is the inventory classification/page_order.py copies
        to keep appended notes out of the page-number footer zone. A note added
        to this module without listing its header would be read as the page's
        last lines."""
        source = inspect.getsource(service_module)
        literals = set(re.findall(r"--- OCR [^\n\"']*? ---", source))
        literals.discard("--- OCR ... ---")  # the placeholder in a comment
        assert literals == set(OCR_NOTE_HEADERS)


class TestMarkHandwritingBlocks:
    def test_lines_carry_no_texttype_like_real_textract(self):
        response = _response(MIXED_PAGE)
        assert all(
            "TextType" not in b for b in response["Blocks"] if b["BlockType"] == "LINE"
        )
        _, count = OcrService._mark_handwriting_blocks(response)
        assert count == 2  # found from the WORD blocks

    def test_marks_the_last_word_of_each_handwritten_line(self):
        marked, count = OcrService._mark_handwriting_blocks(_response(MIXED_PAGE))
        assert count == 2
        by_id = {b["Id"]: b for b in marked["Blocks"]}
        # 7 words: only the last one carries the marker.
        assert by_id["line2_w6"]["Text"] == f"account {HANDWRITING_MARKER}"
        assert by_id["line2_w5"]["Text"] == "my"
        assert by_id["line2"]["Text"] == (
            f"Please update my name on my account {HANDWRITING_MARKER}"
        )
        assert by_id["line3"]["Text"] == f"Jane Smlth {HANDWRITING_MARKER}"
        assert by_id["line0"]["Text"] == "Monthly Card Statement"
        assert by_id["line1"]["Text"] == "Account holder: Jane Smith"

    def test_mixed_line_marks_only_the_handwritten_run(self):
        response = _response([([("Name:", P), ("Jane", H), ("Smlth", H)], 0.1)])
        marked, count = OcrService._mark_handwriting_blocks(response)
        by_id = {b["Id"]: b for b in marked["Blocks"]}
        assert count == 1
        assert by_id["line0"]["Text"] == f"Name: Jane Smlth {HANDWRITING_MARKER}"
        assert by_id["line0_w0"]["Text"] == "Name:"
        assert by_id["line0_w1"]["Text"] == "Jane"

    def test_two_handwritten_runs_on_one_line_are_marked_separately(self):
        words = [("Smlth", H), ("account", P), ("4821", H)]
        marked, _ = OcrService._mark_handwriting_blocks(_response([(words, 0.1)]))
        line = next(b for b in marked["Blocks"] if b["Id"] == "line0")
        assert line["Text"] == (
            f"Smlth {HANDWRITING_MARKER} account 4821 {HANDWRITING_MARKER}"
        )

    def test_a_word_without_text_does_not_carry_the_marker(self):
        words = [("Jane", H), ("", H)]
        marked, count = OcrService._mark_handwriting_blocks(_response([(words, 0.1)]))
        line = next(b for b in marked["Blocks"] if b["Id"] == "line0")
        assert count == 1
        assert line["Text"] == f"Jane {HANDWRITING_MARKER}"
        empty_word = next(b for b in marked["Blocks"] if b["Id"] == "line0_w1")
        assert empty_word["Text"] == ""

    def test_never_modifies_the_input_response(self):
        response = _response(MIXED_PAGE)
        snapshot = copy.deepcopy(response)
        OcrService._mark_handwriting_blocks(response)
        assert response == snapshot

    def test_returns_the_same_response_when_nothing_to_mark(self):
        response = _response([(_words("All printed", P), 0.1)])
        marked, count = OcrService._mark_handwriting_blocks(response)
        assert count == 0
        assert marked is response

    def test_a_line_without_an_id_is_left_unmarked(self):
        response = _response(
            [(_words("Jane Smlth", H), 0.1), (_words("Smlth", H), 0.2)]
        )
        del response["Blocks"][1]["Id"]  # line0
        marked, count = OcrService._mark_handwriting_blocks(response)
        assert count == 1
        by_id = {b.get("Id"): b for b in marked["Blocks"]}
        assert by_id["line1"]["Text"] == f"Smlth {HANDWRITING_MARKER}"
        assert "Id" not in marked["Blocks"][1]
        assert marked["Blocks"][1]["Text"] == "Jane Smlth"

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"Blocks": None},
            {"Blocks": "x"},
            {"Blocks": [None, 3]},
            {"Blocks": [{"BlockType": "LINE", "Id": "l0", "Relationships": ["x", 3]}]},
        ],
    )
    def test_tolerates_malformed_responses(self, response):
        _, count = OcrService._mark_handwriting_blocks(response)
        assert count == 0


class TestParseTextractResponse:
    def test_markers_appear_in_the_page_text(self):
        text = _text(True, _response(MIXED_PAGE))
        assert f"Jane Smlth {HANDWRITING_MARKER}" in text
        assert f"Please update my name on my account {HANDWRITING_MARKER}" in text
        assert "Account holder: Jane Smith" in text
        for line in text.splitlines():
            if line.startswith(("Monthly Card", "Account holder")):
                assert HANDWRITING_MARKER not in line

    def test_each_run_is_marked_exactly_once_in_the_page_text(self):
        """One marker per run of handwritten words, not one per word, and the
        marking is not applied twice on the way through textractor."""
        text = _text(
            True,
            _response(
                [
                    (_words("Account holder: Jane Smith", P), 0.1),
                    (_words("Jane Eleanor Smlth", H), 0.2),
                    ([("Smlth", H), ("account", P), ("4821", H)], 0.3),
                ]
            ),
        )
        page_lines = text.split(HANDWRITING_LEGEND)[0].splitlines()
        one_run = [line for line in page_lines if "Eleanor" in line]
        two_runs = [line for line in page_lines if "4821" in line]
        assert len(one_run) == 1 and one_run[0].count(HANDWRITING_MARKER) == 1
        assert one_run[0].strip() == f"Jane Eleanor Smlth {HANDWRITING_MARKER}"
        assert len(two_runs) == 1 and two_runs[0].count(HANDWRITING_MARKER) == 2
        assert text.split(HANDWRITING_LEGEND)[0].count(HANDWRITING_MARKER) == 3
        assert text.count(HANDWRITING_LEGEND) == 1

    def test_real_document_shape_is_marked(self):
        # A printed decree with a handwritten note.
        response = _response(
            [
                (_words("DECREE FOR CHANGE OF NAME", P), 0.05),
                (_words("changed from JANE ELEANOR SMITH", P), 0.12),
                (_words("Hi there,", H), 0.60),
                (_words("Jane Eleancr Smith", H), 0.64),
            ]
        )
        text = _text(True, response)
        assert f"Jane Eleancr Smith {HANDWRITING_MARKER}" in text
        assert "changed from JANE ELEANOR SMITH" in text
        assert f"JANE ELEANOR SMITH {HANDWRITING_MARKER}" not in text

    def test_legend_is_added_once_and_explains_the_marker(self):
        text = _text(True, _response(MIXED_PAGE))
        assert text.count(HANDWRITING_LEGEND) == 1
        assert "never copy it into a value" in text

    def test_legend_comes_before_the_signature_note(self):
        response = _response(MIXED_PAGE)
        response["Blocks"].append(
            {
                "BlockType": "SIGNATURE",
                "Id": "sig-1",
                "Confidence": 98.1,
                "Page": 1,
                "Geometry": _geometry(0.7, 0.9, 0.2, 0.05),
            }
        )
        text = _text(True, response)
        legend_at = text.index(HANDWRITING_LEGEND)
        signature_at = text.index(SIGNATURE_SUMMARY_HEADER)
        assert legend_at < signature_at
        assert text[:legend_at].count(SIGNATURE_SUMMARY_HEADER) == 0

    def test_off_gives_the_unmarked_text_and_no_legend(self):
        text = _text(False, _response(MIXED_PAGE))
        assert HANDWRITING_MARKER not in text
        assert HANDWRITING_LEGEND not in text

    def test_on_with_no_handwriting_equals_off(self):
        response = _response([(_words("Only printed text", P), 0.1)])
        assert _text(True, response) == _text(False, response)

    def test_raw_response_and_text_confidence_are_unchanged(self):
        response = _response(MIXED_PAGE)
        before_confidence = OcrService._generate_text_confidence_data(
            _service(False), response
        )
        snapshot = copy.deepcopy(response)
        _service(True)._parse_textract_response(response, 1)
        assert response == snapshot  # what rawText.json is written from
        assert (
            OcrService._generate_text_confidence_data(_service(False), response)
            == before_confidence
        )

    def test_marker_survives_in_a_table_cell(self):
        # A 2x2 table whose bottom-right cell is handwritten.
        cells = {
            (1, 1): ("Name", P),
            (1, 2): ("Amount", P),
            (2, 1): ("Smith", P),
            (2, 2): ("329.00", H),
        }
        blocks = [
            {
                "BlockType": "PAGE",
                "Id": "page",
                "Page": 1,
                "Geometry": _geometry(0, 0, 1, 1),
            }
        ]
        cell_ids, line_ids = [], []
        for (row, col), (value, text_type) in cells.items():
            top = 0.1 + 0.1 * row
            key = f"c{row}{col}"
            line_ids.append(f"{key}_line")
            line = _line_blocks(f"{key}_line", [(value, text_type)], top)
            blocks.extend(line)
            word_ids = line[0]["Relationships"][0]["Ids"]
            cell_ids.append(key)
            blocks.append(
                {
                    "BlockType": "CELL",
                    "Id": key,
                    "RowIndex": row,
                    "ColumnIndex": col,
                    "RowSpan": 1,
                    "ColumnSpan": 1,
                    "Confidence": 90.0,
                    "Page": 1,
                    "Geometry": _geometry(0.1 + 0.3 * col, top, 0.25, 0.08),
                    "Relationships": [{"Type": "CHILD", "Ids": word_ids}],
                    "EntityTypes": ["COLUMN_HEADER"] if row == 1 else [],
                }
            )
        blocks.append(
            {
                "BlockType": "TABLE",
                "Id": "table",
                "Confidence": 90.0,
                "Page": 1,
                "Geometry": _geometry(0.1, 0.15, 0.7, 0.3),
                "Relationships": [{"Type": "CHILD", "Ids": cell_ids}],
            }
        )
        blocks[0]["Relationships"] = [{"Type": "CHILD", "Ids": line_ids + ["table"]}]
        text = _text(True, {"DocumentMetadata": {"Pages": 1}, "Blocks": blocks})
        assert f"329.00 {HANDWRITING_MARKER}" in text
        assert f"Smith {HANDWRITING_MARKER}" not in text
        # On the table row itself, inside the handwritten cell.
        rows = [line for line in text.splitlines() if line.startswith("|")]
        assert any(
            re.fullmatch(
                rf"\|\s*Smith\s*\|\s*329\.00 {re.escape(HANDWRITING_MARKER)}\s*\|", row
            )
            for row in rows
        ), rows

    def test_markers_reach_the_block_based_fallback(self):
        """When textractor cannot parse the response, the page text is built from
        the LINE blocks, whose Text was rebuilt with the markers."""
        with patch(
            "textractor.parsers.response_parser.parse",
            side_effect=ValueError("unparseable"),
        ):
            text = _text(True, _response(MIXED_PAGE))
        assert text.startswith("Monthly Card Statement\nAccount holder: Jane Smith\n")
        assert f"Jane Smlth {HANDWRITING_MARKER}" in text
        assert text.count(HANDWRITING_LEGEND) == 1

    def test_marker_survives_in_a_form_value(self):
        """A FORMS key-value pair with a printed key and a handwritten value."""
        blocks = [
            {
                "BlockType": "PAGE",
                "Id": "page",
                "Page": 1,
                "Geometry": _geometry(0, 0, 1, 1),
            }
        ]
        line = _line_blocks("l0", [("Name:", P), ("Jane", H), ("Smlth", H)], 0.1)
        blocks.extend(line)
        key_word, *value_words = line[0]["Relationships"][0]["Ids"]
        blocks.extend(
            [
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": "key",
                    "EntityTypes": ["KEY"],
                    "Confidence": 90.0,
                    "Page": 1,
                    "Geometry": _geometry(0.1, 0.1, 0.07),
                    "Relationships": [
                        {"Type": "VALUE", "Ids": ["value"]},
                        {"Type": "CHILD", "Ids": [key_word]},
                    ],
                },
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": "value",
                    "EntityTypes": ["VALUE"],
                    "Confidence": 90.0,
                    "Page": 1,
                    "Geometry": _geometry(0.18, 0.1, 0.15),
                    "Relationships": [{"Type": "CHILD", "Ids": value_words}],
                },
            ]
        )
        blocks[0]["Relationships"] = [{"Type": "CHILD", "Ids": ["l0", "key"]}]
        text = _text(True, {"DocumentMetadata": {"Pages": 1}, "Blocks": blocks})
        assert f"Jane Smlth {HANDWRITING_MARKER}" in text
        assert f"Name: {HANDWRITING_MARKER}" not in text
        # On the rendered key-value line, after the value.
        page_lines = [
            line.strip() for line in text.split(HANDWRITING_LEGEND)[0].splitlines()
        ]
        assert f"Name: Jane Smlth {HANDWRITING_MARKER}" in page_lines, page_lines

    def test_marker_survives_in_layout_text(self):
        """LAYOUT is in the default Textract features, so most production pages
        are rendered from layout blocks rather than bare lines."""
        response = _response(
            [
                (_words("Monthly Card Statement", P), 0.05),
                (_words("Please update my name on my account", H), 0.30),
                (_words("Jane Smlth", H), 0.34),
            ]
        )
        blocks = response["Blocks"]
        line_ids = [b["Id"] for b in blocks if b["BlockType"] == "LINE"]
        layout = [
            ("LAYOUT_TITLE", "title", [line_ids[0]], 0.05),
            ("LAYOUT_TEXT", "text", line_ids[1:], 0.30),
        ]
        for block_type, key, children, top in layout:
            blocks.append(
                {
                    "BlockType": block_type,
                    "Id": key,
                    "Confidence": 95.0,
                    "Page": 1,
                    "Geometry": _geometry(0.1, top, 0.6, 0.08),
                    "Relationships": [{"Type": "CHILD", "Ids": children}],
                }
            )
        blocks[0]["Relationships"] = [
            {"Type": "CHILD", "Ids": line_ids + ["title", "text"]}
        ]
        text = _text(True, response)
        page_text = text.split(HANDWRITING_LEGEND)[0]
        assert f"on my account {HANDWRITING_MARKER}" in page_text
        assert f"Jane Smlth {HANDWRITING_MARKER}" in page_text
        assert page_text.count(HANDWRITING_MARKER) == 2
        assert "Monthly Card Statement" in page_text
        assert f"Statement {HANDWRITING_MARKER}" not in page_text


class _S3Writes:
    """Stands in for the ``s3`` module the service writes through."""

    def __init__(self):
        self.files = {}

    def write_content(self, content, bucket, key, content_type=None):
        self.files[key.rsplit("/pages/", 1)[1]] = copy.deepcopy(content)


def _image(fmt):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buffer, format=fmt)
    return buffer.getvalue()


# Both Textract page paths write the same four files: an uploaded image file,
# and a page rendered from a PDF.
_PAGE_PATHS = {
    "image_file": lambda service: service._process_image_file_direct(
        "out", "doc", _image("PNG")
    ),
    "pdf_page": lambda service: service._process_page_with_image(
        0, _image("JPEG"), "out", "doc"
    ),
}


class TestFilesWrittenForAPage:
    """What each Textract page path writes, with the setting on and off: only
    result.json differs."""

    def _write_page(self, mark_handwriting, page_path):
        writes = _S3Writes()
        service = _service(mark_handwriting)
        service.enhanced_features = False
        service.textract_client = MagicMock()
        service.textract_client.detect_document_text.return_value = _response(
            MIXED_PAGE
        )
        with patch.object(service_module, "s3", writes):
            _PAGE_PATHS[page_path](service)
        return writes.files

    @pytest.mark.parametrize("page_path", sorted(_PAGE_PATHS))
    def test_only_the_page_text_carries_the_markers(self, page_path):
        on = self._write_page(True, page_path)
        off = self._write_page(False, page_path)
        assert HANDWRITING_MARKER in on["1/result.json"]["text"]
        assert HANDWRITING_LEGEND in on["1/result.json"]["text"]
        assert HANDWRITING_MARKER not in off["1/result.json"]["text"]
        for name in ("1/rawText.json", "1/textConfidence.json", "1/pageData.json"):
            assert on[name] == off[name], name
            assert HANDWRITING_MARKER not in str(on[name]), name
