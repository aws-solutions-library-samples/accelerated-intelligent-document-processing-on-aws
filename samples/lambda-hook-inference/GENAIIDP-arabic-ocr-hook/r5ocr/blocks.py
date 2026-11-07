"""Textract-format blocks consumed unchanged by idp_common _build_page_data."""

from __future__ import annotations

from .geometry import Box, normalise


def _polygon(bb: dict) -> list:
    left, t, w, h = bb["Left"], bb["Top"], bb["Width"], bb["Height"]
    return [
        {"X": left, "Y": t},
        {"X": left + w, "Y": t},
        {"X": left + w, "Y": t + h},
        {"X": left, "Y": t + h},
    ]


def to_textract_blocks(lines: list[tuple[Box, str]], width: int, height: int) -> dict:
    merged: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    for box, text in lines:
        bb = normalise(box, width, height)
        if bb is None or not text.strip():
            continue
        key = (bb["Left"], bb["Top"], bb["Width"], bb["Height"])
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(text.strip())
    line_blocks = []
    for n, key in enumerate(order, 1):
        bb = dict(zip(("Left", "Top", "Width", "Height"), key))
        line_blocks.append(
            {
                "BlockType": "LINE",
                "Id": f"line-{n}",
                "Text": " ".join(merged[key]),
                "Geometry": {"BoundingBox": bb, "Polygon": _polygon(bb)},
            }
        )
    full = {"Left": 0.0, "Top": 0.0, "Width": 1.0, "Height": 1.0}
    page = {
        "BlockType": "PAGE",
        "Id": "page-1",
        "Geometry": {"BoundingBox": full, "Polygon": _polygon(full)},
        "Relationships": [{"Type": "CHILD", "Ids": [b["Id"] for b in line_blocks]}],
    }
    return {"Blocks": [page] + line_blocks}
