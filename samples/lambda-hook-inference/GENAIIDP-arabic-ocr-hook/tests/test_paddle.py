import io
import os

import numpy as np
import pytest
from PIL import Image
from r5ocr.detectors.paddle import detect

pytestmark = pytest.mark.unit

_ARABIC_FONTS = [
    "/System/Library/Fonts/Supplemental/Baghdad.ttc",
    "/System/Library/Fonts/Supplemental/Damascus.ttc",
    "/System/Library/Fonts/Supplemental/Nadeem.ttc",
]


class _Res:
    json = {
        "res": {
            "rec_polys": [[[10, 5], [60, 5], [60, 20], [10, 20]]],
            "rec_texts": ["مرحبا"],
            "rec_scores": [0.9],
        }
    }


class _FakeEngine:
    def predict(self, arr):
        assert isinstance(arr, np.ndarray) and arr.ndim == 3
        return [_Res()]


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (80, 30), "white").save(b, "JPEG")
    return b.getvalue()


def test_detect_converts_polygons_to_boxes():
    boxes = detect(_jpeg(), engine=_FakeEngine())
    assert [(b.x0, b.y0, b.x1, b.y1, b.source, b.text) for b in boxes] == [
        (10, 5, 60, 20, "paddle", "مرحبا")
    ]


@pytest.mark.integration
def test_real_engine_finds_arabic_line():
    pytest.importorskip("paddleocr")
    font_path = next((f for f in _ARABIC_FONTS if os.path.exists(f)), None)
    if font_path is None:
        pytest.skip("no Arabic font available")
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(img).text(
        (20, 30), "مرحبا بالعالم", fill="black", font=ImageFont.truetype(font_path, 48)
    )
    b = io.BytesIO()
    img.save(b, "PNG")
    assert len(detect(b.getvalue())) >= 1
