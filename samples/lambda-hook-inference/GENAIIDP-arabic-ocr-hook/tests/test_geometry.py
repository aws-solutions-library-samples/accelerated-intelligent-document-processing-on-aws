import pytest
from r5ocr.geometry import (
    Box,
    intersection,
    iou,
    normalise,
    overlap_over_smaller,
    union,
    vertical_overlap_ratio,
)

pytestmark = pytest.mark.unit


def test_area_and_intersection():
    a = Box(0, 0, 10, 10, "paddle")
    b = Box(5, 5, 15, 15, "textract")
    assert a.area() == 100 and intersection(a, b) == 25
    assert round(iou(a, b), 4) == round(25 / 175, 4)


def test_overlap_over_smaller_contained():
    big = Box(0, 0, 100, 20, "textract")
    small = Box(10, 2, 30, 18, "paddle")
    assert overlap_over_smaller(big, small) == 1.0


def test_vertical_overlap_ratio_same_line_and_different_lines():
    a = Box(0, 0, 50, 20, "p")
    b = Box(60, 2, 90, 22, "p")
    c = Box(0, 40, 50, 60, "p")
    assert vertical_overlap_ratio(a, b) == 18 / 20
    assert vertical_overlap_ratio(a, c) == 0.0


def test_union():
    u = union([Box(0, 0, 10, 10, "p"), Box(20, 5, 30, 12, "p")], "p")
    assert (u.x0, u.y0, u.x1, u.y1, u.source) == (0, 0, 30, 12, "p")


def test_normalise_clamps_and_rejects_degenerate():
    assert normalise(Box(-5, 10, 110, 60, "p"), 100, 200) == {
        "Left": 0.0,
        "Top": 0.05,
        "Width": 1.0,
        "Height": 0.25,
    }
    assert normalise(Box(10, 10, 10, 50, "p"), 100, 200) is None
