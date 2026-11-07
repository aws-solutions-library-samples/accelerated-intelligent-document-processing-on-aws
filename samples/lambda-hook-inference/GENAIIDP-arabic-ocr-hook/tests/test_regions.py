from itertools import permutations

import pytest
from r5ocr.geometry import Box
from r5ocr.regions import build_regions

pytestmark = pytest.mark.unit


def test_same_line_boxes_from_two_detectors_merge():
    rs = build_regions(
        [Box(10, 10, 110, 30, "paddle"), Box(12, 11, 108, 29, "textract")]
    )
    assert len(rs) == 1 and set(rs[0].boxes) == {"paddle", "textract"}


def test_different_lines_stay_separate_and_ordered():
    rs = build_regions([Box(10, 50, 110, 70, "paddle"), Box(10, 10, 110, 30, "paddle")])
    assert [r.extent.y0 for r in rs] == [10, 50] and [r.id for r in rs] == [1, 2]


def test_whole_row_box_is_dropped_when_split_segments_cover_it():
    left = Box(10, 10, 100, 30, "paddle")
    right = Box(300, 10, 390, 30, "paddle")
    row = Box(10, 10, 390, 30, "textract")
    rs = build_regions([left, right, row])
    # row area 7600, segments cover 3600 (47%) -> not redundant;
    # area ratio > 3 and iou < 0.3 -> own region
    assert len(rs) == 3


def test_redundant_container_dropped():
    a = Box(10, 10, 200, 30, "paddle")
    b = Box(205, 10, 400, 30, "paddle")
    row = Box(10, 10, 400, 30, "textract")
    rs = build_regions([a, b, row])
    assert len(rs) == 2 and all("textract" not in r.boxes for r in rs)


def test_tiny_boxes_dropped():
    assert build_regions([Box(0, 0, 1, 1, "paddle")]) == []


def test_grouping_is_input_order_independent():
    a = Box(0, 10, 100, 30, "paddle")
    b = Box(50, 10, 150, 30, "paddle")
    c = Box(100, 10, 200, 30, "paddle")
    for order in permutations([a, b, c]):
        rs = build_regions(list(order))
        assert len(rs) == 1
        e = rs[0].extent
        assert (e.x0, e.y0, e.x1, e.y1) == (0, 10, 200, 30)


def test_reading_order_not_skewed_by_titles():
    title1 = Box(0, 0, 300, 200, "paddle")
    title2 = Box(400, 0, 700, 200, "paddle")
    line1 = Box(0, 210, 100, 230, "paddle")
    line2 = Box(0, 235, 100, 255, "paddle")
    expected = [(b.y0, b.x0) for b in (title1, title2, line1, line2)]
    for order in ([title1, title2, line1, line2], [title1, title2, line2, line1]):
        rs = sorted(build_regions(order), key=lambda r: r.id)
        assert [(r.extent.y0, r.extent.x0) for r in rs] == expected


def test_distinct_lines_stay_separate_under_permutation():
    first = Box(0, 10, 100, 30, "paddle")
    second = Box(0, 50, 100, 70, "paddle")
    tx = Box(2, 11, 98, 29, "textract")
    for order in permutations([first, second, tx]):
        assert len(build_regions(list(order))) == 2


def test_arabic_row_reads_right_to_left():
    # Two Arabic segments of one row: the right-hand one is read first, so the
    # LINE blocks of a split Arabic value join in reading order.
    left = Box(10, 10, 100, 30, "paddle", "السودان")
    right = Box(300, 10, 400, 30, "paddle", "جمهورية")
    for order in permutations([left, right]):
        rs = build_regions(list(order))
        assert [r.members[0].text for r in rs] == ["جمهورية", "السودان"]


def test_latin_row_reads_left_to_right():
    left = Box(10, 10, 100, 30, "textract", "Republic")
    right = Box(300, 10, 400, 30, "textract", "Sudan")
    for order in permutations([left, right]):
        rs = build_regions(list(order))
        assert [r.members[0].text for r in rs] == ["Republic", "Sudan"]


def test_row_direction_is_per_row():
    ar_l = Box(10, 10, 100, 30, "paddle", "السودان")
    ar_r = Box(300, 10, 400, 30, "paddle", "جمهورية")
    en_l = Box(10, 60, 100, 80, "textract", "Republic")
    en_r = Box(300, 60, 400, 80, "textract", "Sudan")
    rs = build_regions([en_r, ar_l, en_l, ar_r])
    assert [r.members[0].text for r in rs] == [
        "جمهورية",
        "السودان",
        "Republic",
        "Sudan",
    ]
