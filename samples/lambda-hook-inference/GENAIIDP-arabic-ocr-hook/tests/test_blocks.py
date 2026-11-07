import json

import pytest
from r5ocr.blocks import to_textract_blocks
from r5ocr.geometry import Box
from r5ocr.metrics import emit

pytestmark = pytest.mark.unit


def test_blocks_schema_and_dedup():
    lines = [
        (Box(10, 10, 60, 20, "paddle"), "مرحبا"),
        (Box(10, 10, 60, 20, "paddle"), "بكم"),
        (Box(10, 40, 90, 55, "textract"), "Hello"),
        (Box(5, 5, 5, 9, "paddle"), "bad"),
    ]
    out = to_textract_blocks(lines, 100, 100)
    blocks = out["Blocks"]
    page, rest = blocks[0], blocks[1:]
    assert page["BlockType"] == "PAGE" and [b["Text"] for b in rest] == [
        "مرحبا بكم",
        "Hello",
    ]
    assert all("Confidence" not in b for b in blocks)
    assert page["Relationships"] == [{"Type": "CHILD", "Ids": [b["Id"] for b in rest]}]
    assert rest[0]["Geometry"]["BoundingBox"] == {
        "Left": 0.1,
        "Top": 0.1,
        "Width": 0.5,
        "Height": 0.1,
    }
    assert len(rest[0]["Geometry"]["Polygon"]) == 4


def test_emit_prints_one_emf_line(capsys):
    emit("LinesRouted", 3, Script="ar")
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1
    doc = json.loads(printed[0])
    assert doc["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "IDP/ArabicOCR"
    assert doc["LinesRouted"] == 3 and doc["Script"] == "ar"
