import pytest
from r5ocr.geometry import Box
from r5ocr.regions import Region
from r5ocr.routing import choose_box

pytestmark = pytest.mark.unit


def _region(**boxes):
    return Region(1, {k: v for k, v in boxes.items()}, Box(0, 0, 10, 10, "region"))


def test_arabic_prefers_paddle_then_textract():
    p, t = Box(0, 0, 1, 1, "paddle"), Box(0, 0, 2, 2, "textract")
    assert choose_box(_region(paddle=p, textract=t), "ar") is p
    assert choose_box(_region(textract=t), "ar") is t


def test_english_prefers_textract_then_paddle():
    p, t = Box(0, 0, 1, 1, "paddle"), Box(0, 0, 2, 2, "textract")
    assert choose_box(_region(paddle=p, textract=t), "en") is t
    assert choose_box(_region(paddle=p), "en") is p


def test_none_script_returns_none():
    assert choose_box(_region(paddle=Box(0, 0, 1, 1, "paddle")), "none") is None
