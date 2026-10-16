# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for BDA-as-OCR conversion (idp_common.bda.bda_ocr).
"""

# ruff: noqa: E402, I001

import json

import pytest

from unittest.mock import MagicMock

from idp_common.bda.bda_ocr import (
    OCR_PROJECT_NAME_SUFFIX,
    _map_point,
    _normalize_corners,
    bda_standard_output_to_textract_blocks,
    build_ocr_project_override_config,
    build_ocr_project_standard_output_config,
    build_profile_arn,
    delete_ocr_project_by_name,
    extract_markdown,
    find_or_create_ocr_project,
    sanitize_ocr_project_name,
)


def _sample_standard_output():
    """A minimal single-page BDA standard-output payload with two lines."""
    return {
        "metadata": {"semantic_modality": "DOCUMENT", "number_of_pages": 1},
        "document": {"representation": {"markdown": "# Title\n\n| a | b |"}},
        "pages": [
            {
                "page_index": 0,
                "representation": {
                    "markdown": "# Title\n\n| a | b |",
                    "text": "Title a b",
                },
            }
        ],
        "text_lines": [
            {
                "id": "line-1",
                "text": "Title",
                "confidence": 0.008,  # BDA's broken line confidence
                "page_index": 0,
                "locations": [
                    {
                        "page_index": 0,
                        "bounding_box": {
                            "left": 0.1,
                            "top": 0.2,
                            "width": 0.3,
                            "height": 0.05,
                        },
                    }
                ],
            },
            {
                "id": "line-2",
                "text": "a b",
                "confidence": 0.01,
                "page_index": 0,
                "locations": [
                    {
                        "page_index": 0,
                        "bounding_box": {
                            "left": 0.1,
                            "top": 0.3,
                            "width": 0.4,
                            "height": 0.05,
                        },
                    }
                ],
            },
        ],
        "text_words": [
            {
                "id": "w1",
                "text": "Title",
                "confidence": 0.99,
                "line_id": "line-1",
                "page_index": 0,
                "locations": [
                    {
                        "bounding_box": {
                            "left": 0.1,
                            "top": 0.2,
                            "width": 0.15,
                            "height": 0.05,
                        }
                    }
                ],
            },
            {
                "id": "w2",
                "text": "a",
                "confidence": 0.90,
                "line_id": "line-2",
                "page_index": 0,
                "locations": [
                    {
                        "bounding_box": {
                            "left": 0.1,
                            "top": 0.3,
                            "width": 0.1,
                            "height": 0.05,
                        }
                    }
                ],
            },
            {
                "id": "w3",
                "text": "b",
                "confidence": 0.80,
                "line_id": "line-2",
                "page_index": 0,
                "locations": [
                    {
                        "bounding_box": {
                            "left": 0.25,
                            "top": 0.3,
                            "width": 0.1,
                            "height": 0.05,
                        }
                    }
                ],
            },
        ],
    }


@pytest.mark.unit
def test_block_structure_and_counts():
    out = bda_standard_output_to_textract_blocks(_sample_standard_output())
    assert out["DocumentMetadata"]["Pages"] == 1
    types = {}
    for b in out["Blocks"]:
        types[b["BlockType"]] = types.get(b["BlockType"], 0) + 1
    assert types == {"PAGE": 1, "LINE": 2, "WORD": 3}


@pytest.mark.unit
def test_line_confidence_is_word_average_not_broken_bda_value():
    out = bda_standard_output_to_textract_blocks(_sample_standard_output())
    lines = {b["Id"]: b for b in out["Blocks"] if b["BlockType"] == "LINE"}
    # line-1 has one word at 0.99 -> 99.0 (NOT the 0.008 broken source value)
    assert lines["line-1"]["Confidence"] == pytest.approx(99.0)
    # line-2 = mean(0.90, 0.80) = 0.85 -> 85.0
    assert lines["line-2"]["Confidence"] == pytest.approx(85.0)


@pytest.mark.unit
def test_word_confidence_scaled_to_0_100():
    out = bda_standard_output_to_textract_blocks(_sample_standard_output())
    words = {b["Id"]: b for b in out["Blocks"] if b["BlockType"] == "WORD"}
    assert words["w1"]["Confidence"] == pytest.approx(99.0)
    assert words["w2"]["Confidence"] == pytest.approx(90.0)


@pytest.mark.unit
def test_line_to_word_child_relationships():
    out = bda_standard_output_to_textract_blocks(_sample_standard_output())
    lines = {b["Id"]: b for b in out["Blocks"] if b["BlockType"] == "LINE"}
    assert lines["line-1"]["Relationships"][0]["Ids"] == ["w1"]
    assert lines["line-2"]["Relationships"][0]["Ids"] == ["w2", "w3"]


@pytest.mark.unit
def test_empty_text_table_cell_line_synthesizes_text_from_words():
    """BDA leaves ``text`` empty on table-cell lines; the content lives only in
    ``text_words``. The converter must synthesize the LINE text from its child
    words so the line survives the pageData builder's empty-text filter (which
    would otherwise strip all table-cell text/confidence/geometry for BDA OCR).
    """
    payload = _sample_standard_output()
    # A table-cell line: no line-level text, but two child words carry the value.
    payload["text_lines"].append(
        {
            "id": "line-cell",
            "text": "",  # BDA emits empty text for table cells
            "confidence": 0.99,
            "page_index": 0,
            "locations": [
                {
                    "page_index": 0,
                    "bounding_box": {
                        "left": 0.5,
                        "top": 0.5,
                        "width": 0.2,
                        "height": 0.05,
                    },
                }
            ],
        }
    )
    payload["text_words"].extend(
        [
            {
                "id": "wc1",
                "text": "1159.3",
                "confidence": 0.95,
                "line_id": "line-cell",
                "page_index": 0,
                "locations": [
                    {
                        "bounding_box": {
                            "left": 0.5,
                            "top": 0.5,
                            "width": 0.1,
                            "height": 0.05,
                        }
                    }
                ],
            },
            {
                "id": "wc2",
                "text": "grams",
                "confidence": 0.85,
                "line_id": "line-cell",
                "page_index": 0,
                "locations": [
                    {
                        "bounding_box": {
                            "left": 0.6,
                            "top": 0.5,
                            "width": 0.1,
                            "height": 0.05,
                        }
                    }
                ],
            },
        ]
    )

    out = bda_standard_output_to_textract_blocks(payload)
    cell = next(b for b in out["Blocks"] if b.get("Id") == "line-cell")
    # Text is joined from the child words rather than left empty.
    assert cell["Text"] == "1159.3 grams"
    # Confidence is still the word-confidence mean, not the broken BDA value.
    assert cell["Confidence"] == pytest.approx(90.0)
    # Word children (with their own text/confidence/geometry) are preserved.
    assert cell["Relationships"][0]["Ids"] == ["wc1", "wc2"]


@pytest.mark.unit
def test_empty_text_line_without_words_stays_empty():
    """A truly empty line (no words) has no text to synthesize and stays empty."""
    payload = _sample_standard_output()
    payload["text_lines"].append(
        {
            "id": "line-blank",
            "text": "",
            "confidence": 0.5,
            "page_index": 0,
            "locations": [
                {
                    "page_index": 0,
                    "bounding_box": {
                        "left": 0.5,
                        "top": 0.7,
                        "width": 0.2,
                        "height": 0.05,
                    },
                }
            ],
        }
    )
    out = bda_standard_output_to_textract_blocks(payload)
    blank = next(b for b in out["Blocks"] if b.get("Id") == "line-blank")
    assert blank["Text"] == ""
    assert "Relationships" not in blank


@pytest.mark.unit
def test_geometry_is_normalized_boundingbox_and_polygon():
    out = bda_standard_output_to_textract_blocks(_sample_standard_output())
    line1 = next(b for b in out["Blocks"] if b.get("Id") == "line-1")
    bb = line1["Geometry"]["BoundingBox"]
    # BoundingBox is derived from the polygon envelope; compare with tolerance
    # since (left + width) - left can introduce tiny float error.
    assert bb["Left"] == pytest.approx(0.1)
    assert bb["Top"] == pytest.approx(0.2)
    assert bb["Width"] == pytest.approx(0.3)
    assert bb["Height"] == pytest.approx(0.05)
    poly = line1["Geometry"]["Polygon"]
    assert poly[0] == {"X": pytest.approx(0.1), "Y": pytest.approx(0.2)}
    assert poly[2] == {"X": pytest.approx(0.4), "Y": pytest.approx(0.25)}


@pytest.mark.unit
def test_normalize_corners_treats_identity_as_no_mapping():
    """Unit-square corners (a clean, un-rectified page) => no mapping needed."""
    assert _normalize_corners([[0, 0], [1, 0], [1, 1], [0, 1]]) is None
    # Near-identity within tolerance is also treated as identity.
    assert (
        _normalize_corners(
            [[1e-8, -3e-6], [1.0000074, -4e-8], [0.9999999, 1.0000025], [-4e-7, 1.0]]
        )
        is None
    )


@pytest.mark.unit
def test_normalize_corners_rescales_by_scale_factor():
    """corners are normalized to the rectified crop; scale lifts them to page."""
    # A crop half the page's width and a third its height => scale (2, 3).
    corners = [[0.05, 0.02], [0.45, 0.0], [0.45, 0.30], [0.05, 0.32]]
    out = _normalize_corners(corners, scale=(2.0, 3.0))
    assert out == [
        (pytest.approx(0.10), pytest.approx(0.06)),
        (pytest.approx(0.90), pytest.approx(0.00)),
        (pytest.approx(0.90), pytest.approx(0.90)),
        (pytest.approx(0.10), pytest.approx(0.96)),
    ]


@pytest.mark.unit
def test_normalize_corners_identity_after_scale_is_no_mapping():
    """If scaling makes the quad the unit square, treat as identity (no map)."""
    # rectified corners at half scale that, times (2, 2), become the unit square.
    assert (
        _normalize_corners(
            [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]], scale=(2.0, 2.0)
        )
        is None
    )


@pytest.mark.unit
def test_normalize_corners_rejects_malformed():
    assert _normalize_corners(None) is None
    assert _normalize_corners([[0, 0], [1, 0], [1, 1]]) is None  # only 3 corners
    assert _normalize_corners([[0, 0], [1, 0], [1, 1], ["x", 1]]) is None
    # Accepts {x, y} dict form as well as [x, y] pairs.
    assert _normalize_corners(
        [
            {"x": 0.1, "y": 0.0},
            {"x": 0.9, "y": 0.1},
            {"x": 0.8, "y": 1.0},
            {"x": 0.0, "y": 0.9},
        ]
    ) == [(0.1, 0.0), (0.9, 0.1), (0.8, 1.0), (0.0, 0.9)]


@pytest.mark.unit
def test_map_point_bilinear_over_corners():
    """Bilinear map: (0,0)->TL, (1,0)->TR, (1,1)->BR, (0,1)->BL, (.5,.5)->center."""
    corners = [(0.1, 0.0), (0.9, 0.1), (0.8, 1.0), (0.0, 0.9)]
    assert _map_point(0.0, 0.0, corners) == pytest.approx((0.1, 0.0))
    assert _map_point(1.0, 0.0, corners) == pytest.approx((0.9, 0.1))
    assert _map_point(1.0, 1.0, corners) == pytest.approx((0.8, 1.0))
    assert _map_point(0.0, 1.0, corners) == pytest.approx((0.0, 0.9))
    cx = (0.1 + 0.9 + 0.8 + 0.0) / 4
    cy = (0.0 + 0.1 + 1.0 + 0.9) / 4
    assert _map_point(0.5, 0.5, corners) == pytest.approx((cx, cy))


@pytest.mark.unit
def test_geometry_mapped_back_to_original_space_when_rectified():
    """A rectified (skewed) page maps boxes back into original-image space.

    BDA returns boxes normalized against its internally-rectified image; the
    stored page image and UI overlay use original-image space. With non-identity
    corners the converter must re-project, producing a quadrilateral polygon and
    an axis-aligned envelope that lands on the tilted text (not the deskewed
    grid). Regression test for BDA-OCR bounding boxes being visibly offset on
    skewed scans while Textract's were fine.
    """
    payload = _sample_standard_output()
    # Simulate a page rotated ~clockwise: rectified corners fall on a tilted quad
    # in the original image (TL, TR, BR, BL).
    corners = [[0.1, 0.0], [0.9, 0.1], [0.8, 1.0], [0.0, 0.9]]
    payload["pages"][0]["asset_metadata"] = {"corners": corners}

    out = bda_standard_output_to_textract_blocks(payload)
    line1 = next(b for b in out["Blocks"] if b.get("Id") == "line-1")
    geom = line1["Geometry"]

    # Raw BDA box for line-1 is left=0.1, top=0.2, width=0.3, height=0.05. Mapped
    # through the tilted quad it is NO LONGER the raw rectangle.
    bb = geom["BoundingBox"]
    assert bb != {"Left": 0.1, "Top": 0.2, "Width": 0.3, "Height": 0.05}

    # Every mapped polygon vertex must equal the bilinear map of the raw corner.
    raw_corners = [(0.1, 0.2), (0.4, 0.2), (0.4, 0.25), (0.1, 0.25)]
    corner_pts = [(c[0], c[1]) for c in corners]
    for poly_pt, (u, v) in zip(geom["Polygon"], raw_corners):
        exp_x, exp_y = _map_point(u, v, corner_pts)
        assert poly_pt["X"] == pytest.approx(exp_x)
        assert poly_pt["Y"] == pytest.approx(exp_y)

    # BoundingBox is the axis-aligned envelope of that polygon.
    xs = [p["X"] for p in geom["Polygon"]]
    ys = [p["Y"] for p in geom["Polygon"]]
    assert bb["Left"] == pytest.approx(min(xs))
    assert bb["Top"] == pytest.approx(min(ys))
    assert bb["Width"] == pytest.approx(max(xs) - min(xs))
    assert bb["Height"] == pytest.approx(max(ys) - min(ys))


@pytest.mark.unit
def test_subregion_crop_rescales_corners_with_original_image_size():
    """BDA rectifying to a page sub-region: corners normalized to the crop.

    Regression test for the driver's-license case: BDA cropped/rectified to the
    card (a fraction of the page) and reported ``corners`` normalized against
    that rectified crop, so without the original-image size the boxes were
    squeezed into a small band near the top. Passing ``original_image_size``
    (with the rectified pixel dims in asset_metadata) rescales corners into
    original-image space so geometry lands on the real text.
    """
    payload = _sample_standard_output()
    # Original page 1000x1000; BDA rectified to a 500x300 crop => scale (2, 3.33).
    # The crop's corners (in rectified 0-1) sit in the page's upper-left region.
    payload["pages"][0]["asset_metadata"] = {
        "rectified_image_width_pixels": 500,
        "rectified_image_height_pixels": 300,
        "corners": [[0.10, 0.05], [0.40, 0.05], [0.40, 0.25], [0.10, 0.25]],
    }

    # Without the size, corners are used as-is (band near the top-left).
    out_nosize = bda_standard_output_to_textract_blocks(payload)
    line_nosize = next(b for b in out_nosize["Blocks"] if b.get("Id") == "line-1")
    # With the size, corners are rescaled by (1000/500, 1000/300) = (2, 3.33).
    out_sized = bda_standard_output_to_textract_blocks(
        payload, original_image_size=(1000, 1000)
    )
    line_sized = next(b for b in out_sized["Blocks"] if b.get("Id") == "line-1")

    scaled_corners = [
        (0.10 * 2, 0.05 * (1000 / 300)),
        (0.40 * 2, 0.05 * (1000 / 300)),
        (0.40 * 2, 0.25 * (1000 / 300)),
        (0.10 * 2, 0.25 * (1000 / 300)),
    ]
    raw_corners = [(0.1, 0.2), (0.4, 0.2), (0.4, 0.25), (0.1, 0.25)]
    for poly_pt, (u, v) in zip(line_sized["Geometry"]["Polygon"], raw_corners):
        exp_x, exp_y = _map_point(u, v, scaled_corners)
        assert poly_pt["X"] == pytest.approx(exp_x)
        assert poly_pt["Y"] == pytest.approx(exp_y)

    # The sized mapping must reach further down the page than the unsized one
    # (the whole point of the fix): larger max-Y for the same source box.
    y_nosize = max(p["Y"] for p in line_nosize["Geometry"]["Polygon"])
    y_sized = max(p["Y"] for p in line_sized["Geometry"]["Polygon"])
    assert y_sized > y_nosize


@pytest.mark.unit
def test_identity_corners_leave_geometry_unchanged():
    """A clean page (identity corners) must pass geometry through untouched."""
    payload = _sample_standard_output()
    payload["pages"][0]["asset_metadata"] = {
        "corners": [[0, 0], [1, 0], [1, 1], [0, 1]]
    }
    out = bda_standard_output_to_textract_blocks(payload)
    line1 = next(b for b in out["Blocks"] if b.get("Id") == "line-1")
    bb = line1["Geometry"]["BoundingBox"]
    assert bb["Left"] == pytest.approx(0.1)
    assert bb["Top"] == pytest.approx(0.2)
    assert bb["Width"] == pytest.approx(0.3)
    assert bb["Height"] == pytest.approx(0.05)


@pytest.mark.unit
def test_accepts_json_string_input():
    out = bda_standard_output_to_textract_blocks(json.dumps(_sample_standard_output()))
    assert any(b["BlockType"] == "LINE" for b in out["Blocks"])


@pytest.mark.unit
def test_page_index_filter_excludes_other_pages():
    payload = _sample_standard_output()
    payload["text_lines"].append(
        {"id": "p1-line", "text": "other page", "confidence": 0.5, "page_index": 1}
    )
    payload["text_words"].append(
        {
            "id": "p1w",
            "text": "other",
            "confidence": 0.5,
            "line_id": "p1-line",
            "page_index": 1,
        }
    )
    out = bda_standard_output_to_textract_blocks(payload, page_index=0)
    texts = [b.get("Text") for b in out["Blocks"] if b["BlockType"] == "LINE"]
    assert "other page" not in texts


@pytest.mark.unit
def test_line_with_no_words_falls_back_to_own_confidence():
    payload = {
        "pages": [{"page_index": 0, "representation": {"markdown": "x"}}],
        "text_lines": [
            {
                "id": "l",
                "text": "x",
                "confidence": 0.42,
                "page_index": 0,
                "locations": [],
            }
        ],
        "text_words": [],
    }
    out = bda_standard_output_to_textract_blocks(payload)
    line = next(b for b in out["Blocks"] if b["BlockType"] == "LINE")
    assert line["Confidence"] == pytest.approx(42.0)
    assert "Relationships" not in line
    assert line["Geometry"] is None


@pytest.mark.unit
def test_extract_markdown_prefers_page_markdown():
    assert extract_markdown(_sample_standard_output()) == "# Title\n\n| a | b |"


@pytest.mark.unit
def test_extract_markdown_by_page_index_falls_back_to_text():
    payload = {
        "pages": [{"page_index": 0, "representation": {"text": "plain only"}}],
    }
    assert extract_markdown(payload, page_index=0) == "plain only"


@pytest.mark.unit
def test_extract_markdown_document_level_fallback():
    payload = {"document": {"representation": {"markdown": "doc md"}}}
    assert extract_markdown(payload) == "doc md"


@pytest.mark.unit
def test_ocr_project_config_is_sync_compatible():
    cfg = build_ocr_project_standard_output_config()["document"]
    # SYNC projects allow exactly one text format
    assert cfg["outputFormat"]["textFormat"]["types"] == ["MARKDOWN"]
    # No LLM generative fields (cost/latency) for pure OCR
    assert cfg["generativeField"]["state"] == "DISABLED"
    assert cfg["extraction"]["boundingBox"]["state"] == "ENABLED"


@pytest.mark.unit
def test_override_config_routes_images_to_document():
    routing = build_ocr_project_override_config()["modalityRouting"]
    assert routing["jpeg"] == "DOCUMENT"
    assert routing["png"] == "DOCUMENT"


def _mock_control_client(existing_projects, project_detail):
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.return_value = {"projects": existing_projects}
    client.get_data_automation_project.return_value = {"project": project_detail}
    return client


_PROJECT_NAME = "mystack_OCR_StdOutput"


@pytest.mark.unit
def test_find_or_create_reuses_project_and_repairs_missing_routing():
    """A pre-existing project without modality routing must be repaired in place."""
    arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/abc"
    client = _mock_control_client(
        existing_projects=[{"projectName": _PROJECT_NAME, "projectArn": arn}],
        project_detail={"status": "COMPLETED", "overrideConfiguration": {}},
    )
    result = find_or_create_ocr_project(_PROJECT_NAME, bda_control_client=client)
    assert result == arn
    client.create_data_automation_project.assert_not_called()
    # Missing routing -> project updated with the override.
    client.update_data_automation_project.assert_called_once()
    kwargs = client.update_data_automation_project.call_args.kwargs
    assert kwargs["overrideConfiguration"]["modalityRouting"]["jpeg"] == "DOCUMENT"


@pytest.mark.unit
def test_find_or_create_reuses_project_without_update_when_routing_present():
    arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/abc"
    client = _mock_control_client(
        existing_projects=[{"projectName": _PROJECT_NAME, "projectArn": arn}],
        project_detail={
            "status": "COMPLETED",
            "overrideConfiguration": {
                "modalityRouting": {"jpeg": "DOCUMENT", "png": "DOCUMENT"}
            },
        },
    )
    result = find_or_create_ocr_project(_PROJECT_NAME, bda_control_client=client)
    assert result == arn
    client.update_data_automation_project.assert_not_called()


@pytest.mark.unit
def test_find_or_create_creates_when_absent_and_waits_for_completed():
    """No existing project -> create with routing override, wait for COMPLETED."""
    new_arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/new"
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.return_value = {"projects": []}
    client.create_data_automation_project.return_value = {"projectArn": new_arn}
    client.get_data_automation_project.return_value = {
        "project": {"status": "COMPLETED"}
    }
    result = find_or_create_ocr_project(_PROJECT_NAME, bda_control_client=client)
    assert result == new_arn
    kwargs = client.create_data_automation_project.call_args.kwargs
    assert kwargs["projectName"] == _PROJECT_NAME
    assert kwargs["projectType"] == "SYNC"
    assert kwargs["overrideConfiguration"]["modalityRouting"]["png"] == "DOCUMENT"


@pytest.mark.unit
def test_find_or_create_handles_conflict_by_refetching():
    """A concurrent create (ConflictException) is resolved by re-fetching by name."""
    arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/abc"

    class _Conflict(Exception):
        pass

    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.exceptions.ConflictException = _Conflict
    client.create_data_automation_project.side_effect = _Conflict()
    # First list (find) is empty; second list (re-fetch) returns the project.
    client.list_data_automation_projects.side_effect = [
        {"projects": []},
        {"projects": [{"projectName": _PROJECT_NAME, "projectArn": arn}]},
    ]
    client.get_data_automation_project.return_value = {
        "project": {
            "status": "COMPLETED",
            "overrideConfiguration": {
                "modalityRouting": {"jpeg": "DOCUMENT", "png": "DOCUMENT"}
            },
        },
    }
    result = find_or_create_ocr_project(_PROJECT_NAME, bda_control_client=client)
    assert result == arn


@pytest.mark.unit
def test_delete_ocr_project_by_name_deletes_matched_arn():
    arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/abc"
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.return_value = {
        "projects": [{"projectName": _PROJECT_NAME, "projectArn": arn}]
    }
    result = delete_ocr_project_by_name(_PROJECT_NAME, bda_control_client=client)
    assert result == arn
    client.delete_data_automation_project.assert_called_once_with(projectArn=arn)


@pytest.mark.unit
def test_delete_ocr_project_by_name_noop_when_absent():
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.return_value = {"projects": []}
    result = delete_ocr_project_by_name(_PROJECT_NAME, bda_control_client=client)
    assert result is None
    client.delete_data_automation_project.assert_not_called()


@pytest.mark.unit
def test_delete_ocr_project_by_name_swallows_delete_error():
    """A delete failure must not raise (stack deletion must not be blocked)."""
    arn = "arn:aws:bedrock:us-west-2:111122223333:data-automation-project/abc"
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.return_value = {
        "projects": [{"projectName": _PROJECT_NAME, "projectArn": arn}]
    }
    client.delete_data_automation_project.side_effect = Exception("boom")
    result = delete_ocr_project_by_name(_PROJECT_NAME, bda_control_client=client)
    assert result is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "stack_name,expected",
    [
        ("mystack", "mystack_OCR_StdOutput"),
        ("My-Stack-123", "My-Stack-123_OCR_StdOutput"),
        ("weird name.with/chars", "weird-name-with-chars_OCR_StdOutput"),
    ],
)
def test_sanitize_ocr_project_name(stack_name, expected):
    assert sanitize_ocr_project_name(stack_name) == expected


@pytest.mark.unit
def test_sanitize_ocr_project_name_truncates_but_keeps_suffix():
    name = sanitize_ocr_project_name("a" * 200)
    assert len(name) <= 128
    assert name.endswith(OCR_PROJECT_NAME_SUFFIX)


@pytest.mark.unit
@pytest.mark.parametrize(
    "region,expected_geo",
    [
        ("us-west-2", "us"),
        ("eu-central-1", "eu"),
        ("ap-southeast-2", "apac"),  # NOT "ap" — the real profile is apac.*
        ("ca-central-1", "na"),  # AWS names the Canada (Central) profile na.*, not ca.*
        ("sa-east-1", "sa"),
    ],
)
def test_build_profile_arn_geo_prefix(region, expected_geo):
    arn = build_profile_arn(region, "111122223333")
    assert arn.endswith(f":data-automation-profile/{expected_geo}.data-automation-v1")
    assert arn.startswith(f"arn:aws:bedrock:{region}:111122223333:")


@pytest.mark.unit
def test_build_profile_arn_honors_partition():
    arn = build_profile_arn("us-gov-west-1", "111122223333", partition="aws-us-gov")
    assert arn.startswith("arn:aws-us-gov:bedrock:us-gov-west-1:")


@pytest.mark.unit
def test_bda_metering_key_maps_to_pricing_entry():
    """The metering key must resolve to bda/documents-standard in pricing.yaml."""
    from idp_common.ocr.service import OcrService

    svc = OcrService.__new__(OcrService)
    svc.bda_project_arn = "arn:aws:bedrock:us-west-2:1:data-automation-project/x"
    svc._bda_profile_arn = "arn:aws:bedrock:us-west-2:1:data-automation-profile/y"
    svc.bda_runtime_client = MagicMock()
    svc.bda_runtime_client.invoke_data_automation.return_value = {
        "outputSegments": [{"standardOutput": json.dumps(_sample_standard_output())}]
    }

    blocks, tc, text, metering = svc._run_bda_ocr("s3://b/pages/1/image.jpg")

    # Cost calculator strips the OCR/ context -> service_api "bda/documents-standard".
    assert "OCR/bda/documents-standard" in metering
    assert metering["OCR/bda/documents-standard"]["pages"] == 1
    # Response contract: standardOutput is a JSON string inside outputSegments[0].
    svc.bda_runtime_client.invoke_data_automation.assert_called_once()
    call = svc.bda_runtime_client.invoke_data_automation.call_args.kwargs
    assert call["inputConfiguration"] == {"s3Uri": "s3://b/pages/1/image.jpg"}
    assert "| Title | 99.0 |" in tc["text"]
    assert text == "# Title\n\n| a | b |"


@pytest.mark.unit
def test_ensure_bda_arns_raises_without_project_arn():
    """The OCR hot path never creates a project; it errors clearly if none set."""
    import threading

    from idp_common.ocr.service import OcrService

    svc = OcrService.__new__(OcrService)
    svc.bda_project_arn = None
    svc._bda_profile_arn = None
    svc.region = "us-west-2"
    svc._bda_arn_lock = threading.Lock()

    with pytest.raises(ValueError, match="no project ARN"):
        svc._ensure_bda_arns()


@pytest.mark.unit
def test_roundtrip_through_ocr_service_helpers():
    """The converted blocks must work with OcrService's existing helpers."""
    from idp_common.ocr.service import OcrService

    blocks = bda_standard_output_to_textract_blocks(_sample_standard_output())
    svc = OcrService.__new__(OcrService)  # bypass __init__/AWS clients

    tc = OcrService._generate_text_confidence_data(svc, blocks)
    assert "| Title | 99.0 |" in tc["text"]

    md = extract_markdown(_sample_standard_output())
    pd = OcrService._build_page_data(svc, blocks, md, "bda")
    assert pd["provider"] == "bda"
    assert pd["confidenceAvailable"] is True
    assert pd["geometryAvailable"] is True
    assert pd["wordsAvailable"] is True
    assert len(pd["lines"]) == 2
