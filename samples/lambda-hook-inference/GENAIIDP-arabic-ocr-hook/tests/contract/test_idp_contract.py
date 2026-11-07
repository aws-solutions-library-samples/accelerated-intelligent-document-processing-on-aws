"""Contract tests: feed our Textract-format blocks into the REAL idp_common
OCR/grounding code (never mocked) to prove the UI gets line geometry and that
extracted field values (Arabic and English) ground to our boxes.

Skips cleanly (via ``pytest.importorskip``) in the hook's own venv, which has
no idp_common dependencies. Run for real with the repo venv, e.g.:

    PYTHONPATH=$PWD/lib/idp_common_pkg:$PWD/samples/lambda-hook-inference/GENAIIDP-arabic-ocr-hook \\
        /path/to/.venv-repo/bin/python -m pytest samples/lambda-hook-inference/GENAIIDP-arabic-ocr-hook/tests/contract -v -o addopts=""
"""

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
sys.path.insert(0, os.path.join(REPO, "lib/idp_common_pkg"))
idp_service = pytest.importorskip("idp_common.ocr.service")
from r5ocr.blocks import to_textract_blocks  # noqa: E402
from r5ocr.geometry import Box  # noqa: E402

from idp_common.assessment.ocr_grounding import match_value_to_geometry  # noqa: E402

pytestmark = pytest.mark.unit


def _build_page_data(lines: list[tuple[Box, str]], parsed_text: str) -> dict:
    """Run our hook's blocks through the REAL OcrService._build_page_data."""
    blocks = to_textract_blocks(lines, 1000, 1000)
    svc = idp_service.OcrService.__new__(idp_service.OcrService)
    return svc._build_page_data(blocks, parsed_text, "bedrock")


def _page_data():
    return _build_page_data(
        [
            (Box(100, 50, 400, 80, "paddle"), "جمهورية السودان"),
            (Box(100, 120, 300, 140, "textract"), "SD0000001"),
        ],
        "جمهورية السودان\nSD0000001",
    )


def test_page_data_has_line_geometry():
    pd = _page_data()
    assert pd["provider"] == "bedrock-lambdahook" and pd["geometryAvailable"] is True
    assert [ln["geometrySource"] for ln in pd["lines"]] == ["line", "line"]
    assert pd["lines"][0]["geometry"]["boundingBox"] == {
        "left": 0.1,
        "top": 0.05,
        "width": 0.3,
        "height": 0.03,
    }


@pytest.mark.parametrize("value,top", [("جمهورية السودان", 0.05), ("SD0000001", 0.12)])
def test_grounding_matches_arabic_and_english(value, top):
    geometry, source, _ = match_value_to_geometry(value, {1: _page_data()})
    assert source.startswith("ocr")
    assert abs(geometry["boundingBox"]["top"] - top) < 1e-6


def test_bilingual_row_grounds_each_language_to_its_own_segment():
    """A single bilingual row split into two LINEs (Arabic + English segments)
    must ground each language's value to its OWN segment's box, not the
    other's — proving the UI highlights the right half of the row."""
    arabic_box = Box(500, 50, 900, 80, "paddle")
    english_box = Box(100, 50, 400, 80, "paddle")
    pd = _build_page_data(
        [
            (arabic_box, "نوع الرخصة : عامة"),
            (english_box, "License Type : General"),
        ],
        "License Type : General\nنوع الرخصة : عامة",
    )
    page_data_by_page = {1: pd}

    geometry, source, _ = match_value_to_geometry("عامة", page_data_by_page)
    assert source.startswith("ocr")
    bb = geometry["boundingBox"]
    assert bb["left"] == pytest.approx(0.5) and bb["width"] == pytest.approx(0.4)

    geometry, source, _ = match_value_to_geometry("General", page_data_by_page)
    assert source.startswith("ocr")
    bb = geometry["boundingBox"]
    assert bb["left"] == pytest.approx(0.1) and bb["width"] == pytest.approx(0.3)


def test_grounding_returns_none_for_value_not_on_page():
    """A value that appears nowhere on the page must ground to None, never a
    false box."""
    assert (
        match_value_to_geometry(
            "this-value-does-not-appear-anywhere-999", {1: _page_data()}
        )
        is None
    )


def test_merged_boxes_never_produce_paragraph_geometry_source():
    """to_textract_blocks merges identical boxes into a single LINE block
    before _build_page_data ever sees them, so no two emitted LINE blocks can
    share a box — every line's geometrySource must be "line", never
    "paragraph" (which idp_common reserves for backends, like Mistral, that
    only have paragraph-level geometry shared across several lines)."""
    pd = _build_page_data(
        [
            (Box(100, 50, 400, 80, "paddle"), "خط أول"),
            (Box(100, 120, 300, 140, "textract"), "خط ثاني"),
            (Box(100, 200, 500, 230, "paddle"), "Line three"),
        ],
        "خط أول\nخط ثاني\nLine three",
    )
    assert len(pd["lines"]) == 3
    assert all(line["geometrySource"] == "line" for line in pd["lines"])
