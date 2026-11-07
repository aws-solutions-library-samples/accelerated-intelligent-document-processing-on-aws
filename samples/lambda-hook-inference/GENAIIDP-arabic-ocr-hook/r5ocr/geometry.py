"""Axis-aligned box helpers in pixel space of the image the hook received."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    source: str
    text: str = ""

    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)


def intersection(a: Box, b: Box) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return w * h if w > 0 and h > 0 else 0.0


def iou(a: Box, b: Box) -> float:
    inter = intersection(a, b)
    denom = a.area() + b.area() - inter
    return inter / denom if denom > 0 else 0.0


def overlap_over_smaller(a: Box, b: Box) -> float:
    smaller = min(a.area(), b.area())
    return intersection(a, b) / smaller if smaller > 0 else 0.0


def vertical_overlap_ratio(a: Box, b: Box) -> float:
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    shorter = min(a.height(), b.height())
    return overlap / shorter if overlap > 0 and shorter > 0 else 0.0


def union(boxes: Sequence[Box], source: str) -> Box:
    if not boxes:
        raise ValueError("union() needs at least one box")
    return Box(
        min(b.x0 for b in boxes),
        min(b.y0 for b in boxes),
        max(b.x1 for b in boxes),
        max(b.y1 for b in boxes),
        source,
    )


def normalise(box: Box, width: int, height: int) -> Optional[Dict[str, float]]:
    if width <= 0 or height <= 0:
        return None
    x0 = min(max(box.x0 / width, 0.0), 1.0)
    x1 = min(max(box.x1 / width, 0.0), 1.0)
    y0 = min(max(box.y0 / height, 0.0), 1.0)
    y1 = min(max(box.y1 / height, 0.0), 1.0)
    if x1 <= x0 or y1 <= y0:
        return None
    return {
        "Left": round(x0, 6),
        "Top": round(y0, 6),
        "Width": round(x1 - x0, 6),
        "Height": round(y1 - y0, 6),
    }
