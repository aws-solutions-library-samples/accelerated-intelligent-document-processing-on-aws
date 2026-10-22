# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for OCR geometry grounding (assessment/ocr_grounding.py).

Covers value->line matching (exact / substring / multi-line span / paragraph-shared /
fuzzy / no-match), repeated-value spatial disambiguation, 0-1 coordinate correctness,
graceful fallback when geometry is absent, and the full tree-walking enrichment.
"""

from textwrap import dedent
from unittest.mock import patch

import pytest

from idp_common.assessment import ocr_grounding as g
from idp_common.assessment.service import AssessmentService
from idp_common.models import Document, Page, Section, Status

pytestmark = pytest.mark.unit


def _line(text, left, top, width=0.1, height=0.02, conf=99.0, source="line"):
    return {
        "text": text,
        "confidence": conf,
        "geometrySource": source,
        "geometry": {
            "boundingBox": {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        },
    }


def _page(lines, geometry_available=True):
    return {
        "schemaVersion": 1,
        "geometryAvailable": geometry_available,
        "confidenceAvailable": True,
        "lines": lines,
    }


class TestMatchValueToGeometry:
    def test_exact_match_returns_ocr_box(self):
        pd = {1: _page([_line("Account: 12345", 0.1, 0.05)])}
        geom, source, conf = g.match_value_to_geometry("Account: 12345", pd)
        assert source == "ocr"
        assert geom["page"] == 1
        assert geom["boundingBox"]["top"] == 0.05
        # OCR confidence normalized 0-100 -> 0-1.
        assert conf == pytest.approx(0.99)

    def test_substring_match(self):
        # Value is a substring of the OCR line.
        pd = {1: _page([_line("Total Amount Due: $1,234.00", 0.1, 0.2)])}
        geom, source, _ = g.match_value_to_geometry("$1,234.00", pd)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == 0.2

    def test_no_match_returns_none(self):
        pd = {1: _page([_line("Something else", 0.1, 0.05)])}
        assert g.match_value_to_geometry("Nonexistent value", pd) is None

    def test_geometry_unavailable_returns_none(self):
        pd = {1: _page([_line("Account: 12345", 0.1, 0.05)], geometry_available=False)}
        assert g.match_value_to_geometry("Account: 12345", pd) is None

    def test_empty_page_data_returns_none(self):
        assert g.match_value_to_geometry("anything", {}) is None

    def test_multiline_span_unions_boxes(self):
        lines = [
            _line("123 Main Street", 0.1, 0.10, width=0.2, height=0.02),
            _line("Suite 400", 0.1, 0.13, width=0.15, height=0.02),
        ]
        pd = {1: _page(lines)}
        geom, source, _ = g.match_value_to_geometry("123 Main Street Suite 400", pd)
        assert source == "ocr"
        bb = geom["boundingBox"]
        # Union: top of first line, bottom of second.
        assert bb["top"] == pytest.approx(0.10)
        assert bb["height"] == pytest.approx(0.05)  # 0.13 + 0.02 - 0.10
        assert bb["left"] == pytest.approx(0.1)

    def test_paragraph_shared_geometry_tagged(self):
        # Mistral-style: lines share a paragraph box, flagged geometrySource=paragraph.
        line = _line("Paragraph text here", 0.1, 0.3, source="paragraph")
        pd = {1: _page([line])}
        _, source, _ = g.match_value_to_geometry("Paragraph text here", pd)
        assert source == "ocr-paragraph"

    def test_fuzzy_below_threshold_no_match(self):
        # "John Public" tokens {john, public} vs {john, q, public, jr} = 2/4 = 0.5,
        # below the 0.6 threshold -> no match. (Also not a substring: "john public"
        # is not contained in "john q public jr".)
        pd = {1: _page([_line("John Q Public Jr", 0.1, 0.4)])}
        assert g.match_value_to_geometry("John Public", pd) is None

    def test_fuzzy_above_threshold(self):
        pd = {1: _page([_line("Acme Corporation Inc", 0.1, 0.4)])}
        # tokens {acme, corporation, inc} both sides if value close
        geom, source, _ = g.match_value_to_geometry("Acme Corporation Inc Ltd", pd)
        # 3/4 = 0.75 >= 0.6
        assert source == "ocr"


class TestRepeatedValueDisambiguation:
    """The same value on multiple rows must not all collapse onto the first line."""

    def _three_rows(self):
        return {
            1: _page(
                [
                    _line("50.00", 0.8, 0.10),
                    _line("50.00", 0.8, 0.30),
                    _line("50.00", 0.8, 0.50),
                ]
            )
        }

    def test_disambiguates_by_llm_box_proximity(self):
        pd = self._three_rows()
        # LLM placed this field's box near the middle row (top ~0.30).
        llm_geom = {
            "boundingBox": {"left": 0.78, "top": 0.31, "width": 0.12, "height": 0.02},
            "page": 1,
        }
        geom, source, _ = g.match_value_to_geometry("50.00", pd, llm_geom)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == pytest.approx(0.30)

    def test_disambiguates_to_bottom_row(self):
        pd = self._three_rows()
        llm_geom = {
            "boundingBox": {"left": 0.78, "top": 0.49, "width": 0.12, "height": 0.02},
            "page": 1,
        }
        geom, _, _ = g.match_value_to_geometry("50.00", pd, llm_geom)
        assert geom["boundingBox"]["top"] == pytest.approx(0.50)

    def test_ambiguous_without_reference_grounds_first_occurrence(self):
        # ocr_only scalar whose value repeats with NO LLM reference and NO
        # occurrence_index: ground to the FIRST occurrence in reading order
        # (a scalar's value is the same wherever printed) rather than dropping
        # geometry.
        pd = self._three_rows()
        geom, source, _ = g.match_value_to_geometry("50.00", pd, None)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == pytest.approx(0.10)

    def test_llm_ref_off_page_still_resolves_by_proximity(self):
        # Legacy mode: an LLM ref on a different page than all candidates falls
        # back to nearest-by-center across the full pool (unchanged behavior).
        pd = self._three_rows()
        off_page_ref = {
            "boundingBox": {"left": 0.8, "top": 0.48, "width": 0.1, "height": 0.02},
            "page": 99,
        }
        geom, source, _ = g.match_value_to_geometry("50.00", pd, off_page_ref)
        assert source == "ocr"
        # nearest center to top~0.49 is the 0.50 row.
        assert geom["boundingBox"]["top"] == pytest.approx(0.50)

    def test_unique_value_grounds_without_reference(self):
        pd = self._three_rows()
        pd[1]["lines"].append(_line("Unique Vendor Co", 0.1, 0.05))
        geom, source, _ = g.match_value_to_geometry("Unique Vendor Co", pd, None)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == pytest.approx(0.05)


class TestRowOrderDisambiguation:
    """ocr_only mode: repeated values disambiguated by row index (occurrence_index)
    in reading order — no LLM box needed."""

    def _three_rows(self):
        return {
            1: _page(
                [
                    _line("50.00", 0.8, 0.10),
                    _line("50.00", 0.8, 0.30),
                    _line("50.00", 0.8, 0.50),
                ]
            )
        }

    def test_occurrence_index_picks_nth_row(self):
        pd = self._three_rows()
        for idx, expected_top in [(0, 0.10), (1, 0.30), (2, 0.50)]:
            geom, source, _ = g.match_value_to_geometry(
                "50.00", pd, None, occurrence_index=idx
            )
            assert source == "ocr"
            assert geom["boundingBox"]["top"] == pytest.approx(expected_top), idx

    def test_occurrence_index_out_of_range_clamps_to_last(self):
        pd = self._three_rows()
        geom, _, _ = g.match_value_to_geometry("50.00", pd, None, occurrence_index=99)
        assert geom["boundingBox"]["top"] == pytest.approx(0.50)

    def test_reading_order_sorts_across_pages_then_top(self):
        # Out-of-order line insertion still resolves by (page, top).
        pd = {
            1: _page([_line("X", 0.5, 0.40), _line("X", 0.5, 0.10)]),
            2: _page([_line("X", 0.5, 0.05)]),
        }
        # index 0 -> page1 top0.10, index1 -> page1 top0.40, index2 -> page2 top0.05
        g0 = g.match_value_to_geometry("X", pd, None, occurrence_index=0)[0]
        g1 = g.match_value_to_geometry("X", pd, None, occurrence_index=1)[0]
        g2 = g.match_value_to_geometry("X", pd, None, occurrence_index=2)[0]
        assert (g0["page"], g0["boundingBox"]["top"]) == (1, pytest.approx(0.10))
        assert (g1["page"], g1["boundingBox"]["top"]) == (1, pytest.approx(0.40))
        assert (g2["page"], g2["boundingBox"]["top"]) == (2, pytest.approx(0.05))


class TestOcrOnlyGroundingMode:
    """ground_assessment_geometry(geometry_mode='ocr_only') ignores LLM boxes,
    derives geometry from OCR, and uses row order for repeated list values."""

    def test_ocr_only_replaces_llm_box_and_drops_when_unmatched(self):
        page_data = {1: _page([_line("ACME Corp", 0.1, 0.07)])}
        assessment = {
            "vendor": {
                "confidence": 0.9,
                # a hallucinated LLM box that ocr_only must ignore/replace
                "geometry": [
                    {
                        "boundingBox": {"left": 0, "top": 0, "width": 1, "height": 1},
                        "page": 1,
                    }
                ],
            },
            "missing_field": {
                "confidence": 0.5,
                "geometry": [
                    {
                        "boundingBox": {
                            "left": 0.2,
                            "top": 0.2,
                            "width": 0.1,
                            "height": 0.02,
                        },
                        "page": 1,
                    }
                ],
            },
        }
        extraction = {"vendor": "ACME Corp", "missing_field": "not in document"}
        out = g.ground_assessment_geometry(
            assessment, extraction, page_data, "ocr_only"
        )
        # matched field -> real OCR box
        assert out["vendor"]["geometry_source"] == "ocr"
        assert out["vendor"]["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.07)
        # unmatched field -> LLM box DROPPED (no hallucinated coords in ocr_only)
        assert "geometry" not in out["missing_field"]
        assert "geometry_source" not in out["missing_field"]

    def test_ocr_only_list_uses_row_order(self):
        page_data = {
            1: _page(
                [
                    _line("9.99", 0.8, 0.10),
                    _line("9.99", 0.8, 0.30),
                    _line("9.99", 0.8, 0.50),
                ]
            )
        }
        assessment = {
            "txns": [
                {"amount": {"confidence": 0.9}},
                {"amount": {"confidence": 0.9}},
                {"amount": {"confidence": 0.9}},
            ]
        }
        extraction = {
            "txns": [{"amount": "9.99"}, {"amount": "9.99"}, {"amount": "9.99"}]
        }
        out = g.ground_assessment_geometry(
            assessment, extraction, page_data, "ocr_only"
        )
        tops = [
            row["amount"]["geometry"][0]["boundingBox"]["top"] for row in out["txns"]
        ]
        assert tops == [pytest.approx(0.10), pytest.approx(0.30), pytest.approx(0.50)]


class TestExactIndexFastPath:
    """The index-first fast path (perf: ~30× on large tables) must be behaviorally
    identical to the full per-page scan."""

    def test_exact_index_hit_returns_line_box(self):
        pd = {1: _page([_line("AAPL", 0.1, 0.2, width=0.05, height=0.02)])}
        geom, source, _ = g.match_value_to_geometry("AAPL", pd)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == pytest.approx(0.2)
        assert geom["page"] == 1

    def test_exact_hit_skips_fuzzy_noise_on_other_lines(self):
        # A near-miss fuzzy line must NOT displace the exact hit (fast path returns
        # the exact tier and never runs the fuzzy sweep).
        pd = {
            1: _page(
                [
                    _line("AAPL", 0.1, 0.2),
                    _line("AAPLE", 0.1, 0.5),  # fuzzy near-miss, lower on page
                ]
            )
        }
        geom, source, _ = g.match_value_to_geometry("AAPL", pd)
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == pytest.approx(0.2)  # the exact line

    def test_index_first_matches_full_scan_across_pages(self):
        # A value present on page 2 only: fast path (index) and the full scan must
        # yield the same box. (Differential guard for the cross-page fast path.)
        pd = {
            1: _page([_line("Something else", 0.1, 0.1)]),
            2: _page([_line("TARGET VALUE", 0.3, 0.4, width=0.2, height=0.02)]),
        }
        geom, source, _ = g.match_value_to_geometry("TARGET VALUE", pd)
        assert source == "ocr"
        assert geom["page"] == 2
        assert geom["boundingBox"]["left"] == pytest.approx(0.3)

    def test_page_indexes_cached_once(self):
        page = _page([_line("x", 0.1, 0.1)])
        pd = {1: page}
        g.match_value_to_geometry("x", pd)
        # Private caches were populated on the page_data dict.
        assert "__norm_lines_cache__" in page
        assert "__exact_index_cache__" in page
        assert page["__exact_index_cache__"].get("x")  # normalized key indexed


class TestPageResolution:
    def test_multi_page_resolves_to_preferred_page(self):
        pd = {
            1: _page([_line("Balance 100", 0.5, 0.2)]),
            2: _page([_line("Balance 100", 0.5, 0.7)]),
        }
        # LLM box on page 2.
        llm_geom = {
            "boundingBox": {"left": 0.5, "top": 0.7, "width": 0.1, "height": 0.02},
            "page": 2,
        }
        geom, _, _ = g.match_value_to_geometry("Balance 100", pd, llm_geom)
        assert geom["page"] == 2

    def test_falls_back_to_other_page_when_unique(self):
        pd = {
            1: _page([_line("Only on page one", 0.1, 0.2)]),
            2: _page([_line("Something else", 0.1, 0.2)]),
        }
        geom, _, _ = g.match_value_to_geometry("Only on page one", pd, None)
        assert geom["page"] == 1


class TestCoordinateCorrectness:
    def test_grounded_box_is_0_1_not_rescaled(self):
        # pageData geometry is already 0-1; must NOT be divided by 1000.
        pd = {1: _page([_line("X", 0.123, 0.456, width=0.05, height=0.01)])}
        geom, _, _ = g.match_value_to_geometry("X", pd)
        bb = geom["boundingBox"]
        assert bb["left"] == 0.123
        assert bb["top"] == 0.456
        assert bb["width"] == 0.05


class TestGroundAssessmentGeometry:
    def test_replaces_llm_box_and_tags_source(self):
        assessment = {
            "AccountNumber": {
                "confidence": 0.9,
                "confidence_reason": "clear",
                "confidence_threshold": 0.8,
                "geometry": [
                    {
                        "boundingBox": {
                            "left": 0.4,
                            "top": 0.04,
                            "width": 0.2,
                            "height": 0.03,
                        },
                        "page": 1,
                    }
                ],
            }
        }
        extraction = {"AccountNumber": "12345"}
        pd = {1: _page([_line("12345", 0.41, 0.05)])}
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        field = out["AccountNumber"]
        assert field["geometry_source"] == "ocr"
        assert field["geometry"][0]["boundingBox"]["top"] == 0.05
        # LLM confidence untouched.
        assert field["confidence"] == 0.9
        assert field["confidence_reason"] == "clear"
        assert field["ocr_confidence"] == pytest.approx(0.99)

    def test_no_match_keeps_llm_box_tags_llm(self):
        llm_box = {
            "boundingBox": {"left": 0.4, "top": 0.04, "width": 0.2, "height": 0.03},
            "page": 1,
        }
        assessment = {
            "Field": {"confidence": 0.9, "geometry": [dict(llm_box)]},
        }
        extraction = {"Field": "value not in ocr"}
        pd = {1: _page([_line("totally different", 0.1, 0.5)])}
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        field = out["Field"]
        assert field["geometry_source"] == "llm"
        # Box unchanged.
        assert field["geometry"][0]["boundingBox"]["top"] == 0.04

    def test_empty_page_data_is_noop_except_llm_tag(self):
        assessment = {
            "Field": {
                "confidence": 0.9,
                "geometry": [
                    {
                        "boundingBox": {
                            "left": 0.4,
                            "top": 0.04,
                            "width": 0.2,
                            "height": 0.03,
                        },
                        "page": 1,
                    }
                ],
            }
        }
        out = g.ground_assessment_geometry(assessment, {"Field": "x"}, {})
        # No page data -> box unchanged; geometry_source tagged llm.
        assert out["Field"]["geometry"][0]["boundingBox"]["top"] == 0.04

    def test_nested_group_attributes(self):
        assessment = {
            "CompanyAddress": {
                "State": {"confidence": 0.9},
                "ZipCode": {"confidence": 0.9},
            }
        }
        extraction = {"CompanyAddress": {"State": "CA", "ZipCode": "90210"}}
        pd = {
            1: _page(
                [
                    _line("CA", 0.2, 0.10),
                    _line("90210", 0.3, 0.10),
                ]
            )
        }
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        assert out["CompanyAddress"]["State"]["geometry_source"] == "ocr"
        assert out["CompanyAddress"]["ZipCode"]["geometry"][0]["boundingBox"][
            "left"
        ] == pytest.approx(0.3)

    def test_list_items_grounded_per_row(self):
        assessment = {
            "Transactions": [
                {
                    "Amount": {
                        "confidence": 0.9,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "left": 0.8,
                                    "top": 0.11,
                                    "width": 0.1,
                                    "height": 0.02,
                                },
                                "page": 1,
                            }
                        ],
                    }
                },
                {
                    "Amount": {
                        "confidence": 0.9,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "left": 0.8,
                                    "top": 0.31,
                                    "width": 0.1,
                                    "height": 0.02,
                                },
                                "page": 1,
                            }
                        ],
                    }
                },
            ]
        }
        extraction = {
            "Transactions": [{"Amount": "50.00"}, {"Amount": "50.00"}],
        }
        pd = {
            1: _page(
                [
                    _line("50.00", 0.8, 0.10),
                    _line("50.00", 0.8, 0.30),
                ]
            )
        }
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        # Each row grounds to its own line via proximity, not both to the first.
        assert out["Transactions"][0]["Amount"]["geometry"][0]["boundingBox"][
            "top"
        ] == pytest.approx(0.10)
        assert out["Transactions"][1]["Amount"]["geometry"][0]["boundingBox"][
            "top"
        ] == pytest.approx(0.30)

    def test_decomposed_string_value_grounds_by_subkey(self):
        # The assessment LLM split a plain-string field ("Insurance Company") into
        # sub-keyed assessments whose KEYS are fragments of the extracted value. The
        # extraction value is a flat string, so each sub-key name is the text to ground.
        assessment = {
            "Insurance Company": {
                "Fake Insurance Co": {
                    "confidence": 0.99,
                    "geometry": [
                        {
                            "boundingBox": {
                                "left": 0.6,
                                "top": 0.089,
                                "width": 0.1,
                                "height": 0.014,
                            },
                            "page": 1,
                        }
                    ],
                    "geometry_source": "llm",
                },
                "650 Davis Street": {"confidence": 1.0},
                "confidence_threshold": 0.8,
            }
        }
        extraction = {"Insurance Company": "Fake Insurance Co 650 Davis Street"}
        pd = {
            6: _page(
                [
                    _line("Fake Insurance Co", 0.6, 0.50),
                    _line("650 Davis Street", 0.6, 0.55),
                ]
            )
        }
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        ic = out["Insurance Company"]
        assert ic["Fake Insurance Co"]["geometry_source"] == "ocr"
        assert ic["Fake Insurance Co"]["geometry"][0]["page"] == 6
        assert ic["Fake Insurance Co"]["geometry"][0]["boundingBox"][
            "top"
        ] == pytest.approx(0.50)
        # A sub-key with no prior geometry still grounds (adds a box).
        assert ic["650 Davis Street"]["geometry_source"] == "ocr"
        assert ic["650 Davis Street"]["geometry"][0]["boundingBox"][
            "top"
        ] == pytest.approx(0.55)

    def test_true_group_with_missing_extraction_does_not_match_label(self):
        # A genuine group whose extraction value is absent must NOT ground its child
        # keys against OCR text (the key is a label, not a value fragment).
        assessment = {
            "CompanyAddress": {
                "State": {"confidence": 0.9},
            }
        }
        extraction = {}  # no extraction value for the group
        pd = {1: _page([_line("State", 0.2, 0.10)])}
        out = g.ground_assessment_geometry(assessment, extraction, pd)
        # No grounding: child had no box and no value -> not tagged ocr.
        assert out["CompanyAddress"]["State"].get("geometry_source") != "ocr"

    def test_grounding_never_raises(self):
        # Malformed assessment shouldn't blow up; returns input.
        assessment = {"Field": {"confidence": 0.9, "geometry": "not a list"}}
        out = g.ground_assessment_geometry(assessment, {"Field": "x"}, {})
        assert out is assessment

    def test_skip_grounded_leaves_already_grounded_untouched(self):
        # A leaf already grounded per-shard (has geometry_source) must NOT be
        # re-grounded by a post-merge skip_grounded pass — its box is preserved
        # even though the merge-step OCR would match a DIFFERENT line.
        assessment = {
            "Amount": {
                "confidence": 0.9,
                "geometry_source": "ocr",
                "geometry": [
                    {
                        "boundingBox": {
                            "left": 0.1,
                            "top": 0.10,
                            "width": 0.1,
                            "height": 0.02,
                        },
                        "page": 1,
                    }
                ],
            }
        }
        extraction = {"Amount": "50.00"}
        # OCR would ground "50.00" to top=0.90; skip_grounded must keep top=0.10.
        pd = {1: _page([_line("50.00", 0.1, 0.90)])}
        out = g.ground_assessment_geometry(
            assessment, extraction, pd, "ocr_only", skip_grounded=True
        )
        assert out["Amount"]["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.10)

    def test_skip_grounded_still_grounds_ungrounded_leaves(self):
        # A residual leaf with NO geometry_source (e.g. a reconcile-padded
        # placeholder row) IS grounded by the skip_grounded pass — so the merge
        # step still fills in rows the shards left ungrounded.
        assessment = {
            "GroundedRow": {
                "confidence": 0.9,
                "geometry_source": "ocr",
                "geometry": [],
            },
            "PaddedRow": {"confidence": None},  # no geometry_source
        }
        extraction = {"GroundedRow": "AAA", "PaddedRow": "BBB"}
        pd = {1: _page([_line("BBB", 0.2, 0.40)])}
        out = g.ground_assessment_geometry(
            assessment, extraction, pd, "ocr_only", skip_grounded=True
        )
        # The padded leaf got a real OCR box...
        assert out["PaddedRow"].get("geometry_source") == "ocr"
        assert out["PaddedRow"]["geometry"][0]["boundingBox"]["top"] == pytest.approx(
            0.40
        )
        # ...and the already-grounded leaf was left as-is (empty geometry preserved).
        assert out["GroundedRow"]["geometry"] == []

    def test_skip_grounded_false_regrounds_everything(self):
        # Default (skip_grounded=False) re-grounds even already-grounded leaves —
        # the single-agent (non-sharded) path relies on this.
        assessment = {
            "Amount": {
                "confidence": 0.9,
                "geometry_source": "ocr",
                "geometry": [
                    {
                        "boundingBox": {
                            "left": 0.1,
                            "top": 0.10,
                            "width": 0.1,
                            "height": 0.02,
                        },
                        "page": 1,
                    }
                ],
            }
        }
        extraction = {"Amount": "50.00"}
        pd = {1: _page([_line("50.00", 0.1, 0.90)])}
        out = g.ground_assessment_geometry(assessment, extraction, pd, "ocr_only")
        # Re-grounded to the OCR line's actual position.
        assert out["Amount"]["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.90)


class TestLoadPageOcrData:
    def test_skips_pages_without_uri(self):
        class FakePage:
            ocr_page_data_uri = None

        pages = {"1": FakePage()}
        assert g.load_page_ocr_data(pages, ["1"]) == {}

    def test_keys_by_section_relative_page_number(self):
        # A section that starts at DOC page 2 must key its first page as SECTION
        # page 1 (not 2), so the UI (sectionPageIds[page-1]) navigates correctly.
        class FakePage:
            def __init__(self, pid):
                self.page_id = pid
                self.ocr_page_data_uri = f"s3://bucket/pages/{pid}/pageData.json"

        pages = {str(i): FakePage(str(i)) for i in range(2, 6)}  # doc pages 2-5
        with patch.object(
            g.s3,
            "get_json_content",
            return_value={"lines": [], "geometryAvailable": False},
        ):
            result = g.load_page_ocr_data(pages, ["2", "3", "4", "5"])
        # Section-relative: doc pages 2,3,4,5 -> section pages 1,2,3,4.
        assert sorted(result.keys()) == [1, 2, 3, 4]

    def test_page_offset_for_shard_slice(self):
        # A shard covering section pages 6-8 (slice of the section) must still
        # number relative to the WHOLE section via page_offset.
        class FakePage:
            def __init__(self, pid):
                self.ocr_page_data_uri = f"s3://bucket/pages/{pid}/pageData.json"

        pages = {str(i): FakePage(str(i)) for i in range(2, 20)}
        shard_ids = ["7", "8", "9"]  # doc pages; section-relative these are 6,7,8
        with patch.object(
            g.s3,
            "get_json_content",
            return_value={"lines": [], "geometryAvailable": False},
        ):
            result = g.load_page_ocr_data(pages, shard_ids, page_offset=5)
        assert sorted(result.keys()) == [6, 7, 8]

    def test_missing_page_does_not_shift_numbering(self):
        # If a middle page has no OCR data, the surviving pages keep their
        # section-relative numbers (derived from position, not load order).
        class FakePage:
            def __init__(self, uri):
                self.ocr_page_data_uri = uri

        pages = {
            "2": FakePage("s3://b/2/pageData.json"),
            "3": FakePage(None),  # no OCR data
            "4": FakePage("s3://b/4/pageData.json"),
        }
        with patch.object(
            g.s3,
            "get_json_content",
            return_value={"lines": [], "geometryAvailable": False},
        ):
            result = g.load_page_ocr_data(pages, ["2", "3", "4"])
        # page 3 dropped, but 2->1 and 4->3 keep their section-relative positions.
        assert sorted(result.keys()) == [1, 3]

    def test_unreadable_page_is_skipped(self):
        class FakePage:
            ocr_page_data_uri = "s3://bucket/pages/1/pageData.json"

        pages = {"1": FakePage()}
        with patch.object(g.s3, "get_json_content", side_effect=Exception("boom")):
            result = g.load_page_ocr_data(pages, ["1"])
        assert result == {}


class TestSectionRelativePageNumbers:
    """geometry.page must be SECTION-RELATIVE (1 = section's first page) so the UI
    (sectionPageIds[page-1]) navigates to the correct page — regardless of where the
    section starts in the document."""

    def test_grounded_page_is_section_relative(self):
        # page_data keyed section-relative (as load_page_ocr_data now produces):
        # section page 1 = doc page 2, section page 2 = doc page 3, etc.
        pd = {
            1: _page([_line("Alpha", 0.1, 0.10)]),
            2: _page([_line("Bravo", 0.1, 0.10)]),
            3: _page([_line("Charlie", 0.1, 0.10)]),
        }
        assessment = {
            "f1": {"confidence": 0.9},
            "f2": {"confidence": 0.9},
            "f3": {"confidence": 0.9},
        }
        extraction = {"f1": "Alpha", "f2": "Bravo", "f3": "Charlie"}
        out = g.ground_assessment_geometry(assessment, extraction, pd, "ocr_only")
        # A value on the section's 3rd page grounds to page 3 (NOT a doc-absolute
        # number), and the section's 1st-page value to page 1.
        assert out["f1"]["geometry"][0]["page"] == 1
        assert out["f2"]["geometry"][0]["page"] == 2
        assert out["f3"]["geometry"][0]["page"] == 3

    def test_load_then_ground_end_to_end_offset_section(self):
        # Simulate a section starting at doc page 2: load_page_ocr_data keys it
        # section-relative, and grounding stamps those section-relative pages.
        class FakePage:
            def __init__(self, uri):
                self.ocr_page_data_uri = uri

        # Two doc pages (2,3) whose pageData we mock via get_json_content per-uri.
        page_payloads = {
            "s3://b/2/pageData.json": _page([_line("FirstPageVal", 0.2, 0.3)]),
            "s3://b/3/pageData.json": _page([_line("SecondPageVal", 0.2, 0.3)]),
        }
        pages = {
            "2": FakePage("s3://b/2/pageData.json"),
            "3": FakePage("s3://b/3/pageData.json"),
        }
        with patch.object(
            g.s3, "get_json_content", side_effect=lambda uri: page_payloads[uri]
        ):
            pd = g.load_page_ocr_data(pages, ["2", "3"])
        assert sorted(pd.keys()) == [1, 2]
        assessment = {"a": {"confidence": 0.9}, "b": {"confidence": 0.9}}
        extraction = {"a": "FirstPageVal", "b": "SecondPageVal"}
        out = g.ground_assessment_geometry(assessment, extraction, pd, "ocr_only")
        assert out["a"]["geometry"][0]["page"] == 1  # section's first page
        assert out["b"]["geometry"][0]["page"] == 2  # section's second page


class TestServiceIntegration:
    """End-to-end wiring through AssessmentService.process_document_section."""

    def _config(self, ground: bool, geometry_mode: str = "llm_with_ocr_grounding"):
        return {
            "classes": [
                {
                    "x-aws-idp-document-type": "invoice",
                    "type": "object",
                    "properties": {
                        "invoice_number": {
                            "type": "string",
                            "description": "Invoice number",
                        }
                    },
                }
            ],
            "assessment": {
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "default_confidence_threshold": 0.8,
                "ground_geometry_in_ocr": ground,
                "geometry_mode": geometry_mode,
                "system_prompt": "assess",
                "task_prompt": dedent(
                    """
                    {DOCUMENT_CLASS} {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
                    {EXTRACTION_RESULTS} {DOCUMENT_TEXT} {OCR_TEXT_CONFIDENCE}
                    """
                ),
            },
        }

    def _document(self, with_page_data: bool):
        doc = Document(
            id="test-doc",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.ASSESSING,
        )
        doc.pages["1"] = Page(
            page_id="1",
            image_uri="s3://in/d.pdf/pages/1/image.jpg",
            parsed_text_uri="s3://in/d.pdf/pages/1/parsed.txt",
            ocr_page_data_uri=(
                "s3://out/d.pdf/pages/1/pageData.json" if with_page_data else None
            ),
        )
        section = Section(section_id="1", classification="invoice", page_ids=["1"])
        section.extraction_result_uri = "s3://out/d.pdf/sections/1/result.json"
        doc.sections.append(section)
        return doc

    def _run(
        self,
        ground,
        with_page_data,
        llm_bbox_top=0.05,
        geometry_mode="llm_with_ocr_grounding",
    ):
        service = AssessmentService(
            region="us-west-2", config=self._config(ground, geometry_mode)
        )
        extraction = {
            "document_class": {"type": "invoice"},
            "inference_result": {"invoice_number": "INV-123"},
            "metadata": {},
        }
        page_data = _page([_line("INV-123", 0.4, 0.40)])
        # LLM returns a box far from the real OCR line (top 0.40 vs 0.05).
        import json as _json

        top1000 = int(llm_bbox_top * 1000)
        llm_text = _json.dumps(
            {
                "invoice_number": {
                    "confidence": 0.98,
                    "confidence_reason": "clear",
                    "bbox": [400, top1000, 500, top1000 + 20],
                    "page": 1,
                }
            }
        )
        llm_response = {
            "response": {"output": {"message": {"content": [{"text": llm_text}]}}},
            "metering": {"tokens": 1},
        }

        json_side = [extraction]
        if ground and with_page_data:
            json_side.append(page_data)

        written = {}

        def _capture(content, *a, **k):
            written.update(content)

        with (
            patch("idp_common.s3.get_json_content", side_effect=json_side),
            patch("idp_common.s3.get_text_content", return_value="INV-123"),
            patch("idp_common.image.prepare_image", return_value=b"img"),
            patch("idp_common.bedrock.invoke_model", return_value=llm_response),
            patch("idp_common.s3.write_content", side_effect=_capture),
            patch("idp_common.utils.parse_s3_uri", return_value=("out", "k")),
            patch("idp_common.metrics.put_metric"),
        ):
            service.process_document_section(self._document(with_page_data), "1")
        return written["explainability_info"][0]["invoice_number"]

    def test_grounding_on_replaces_box_with_ocr(self):
        field = self._run(ground=True, with_page_data=True)
        assert field["geometry_source"] == "ocr"
        # Box came from the OCR line (top 0.40), not the LLM estimate (top 0.05).
        assert field["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.40)
        assert field["ocr_confidence"] == pytest.approx(0.99)

    def test_grounding_off_keeps_llm_box(self):
        field = self._run(ground=False, with_page_data=True)
        # LLM box preserved (top 0.05), no grounding keys.
        assert field["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.05)
        assert "geometry_source" not in field

    def test_grounding_on_but_no_page_data_keeps_llm_box(self):
        # Flag on, but page has no ocr_page_data_uri -> load returns {} -> LLM box kept.
        field = self._run(ground=True, with_page_data=False)
        assert field["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.05)
        assert "geometry_source" not in field

    def test_ocr_only_grounds_from_ocr_ignoring_llm_box(self):
        # Default mode: the model's bbox is ignored; geometry comes from OCR.
        field = self._run(ground=True, with_page_data=True, geometry_mode="ocr_only")
        assert field["geometry_source"] == "ocr"
        assert field["geometry"][0]["boundingBox"]["top"] == pytest.approx(0.40)

    def test_ocr_only_no_page_data_yields_no_geometry(self):
        # ocr_only + no OCR data -> no box at all (the LLM box is NOT kept, so no
        # hallucinated coordinates leak through).
        field = self._run(ground=True, with_page_data=False, geometry_mode="ocr_only")
        assert "geometry" not in field
        assert "geometry_source" not in field


class TestFormatAwareMatching:
    """Format/type-aware matching: schema-canonicalized values (dates, currency,
    phone) match OCR text rendered in a different surface format."""

    def test_date_reformat_type_equal(self):
        # value 2022-04-04 vs OCR "04/04/2022" — text match fails, type-equal wins.
        pd = {1: _page([_line("Statement End Date: 04/04/2022", 0.1, 0.2)])}
        geom, source, _ = g.match_value_to_geometry(
            "2022-04-04", pd, schema_hint={"type": "string", "format": "date"}
        )
        assert geom is not None
        assert source == g._SOURCE_NORMALIZED
        assert geom["boundingBox"]["top"] == pytest.approx(0.2)

    def test_date_reformat_heuristic_no_schema(self):
        # Same, but no schema hint -> inferred from value.
        pd = {1: _page([_line("04/04/2022", 0.1, 0.3)])}
        geom, source, _ = g.match_value_to_geometry("2022-04-04", pd)
        assert geom is not None and source == g._SOURCE_NORMALIZED

    def test_currency_variant_match(self):
        # value "1234.00" vs OCR "$1,234.00".
        pd = {1: _page([_line("Total Due $1,234.00", 0.1, 0.4)])}
        geom, source, _ = g.match_value_to_geometry(
            "1234.00", pd, schema_hint={"type": "number"}
        )
        assert geom is not None and source == g._SOURCE_NORMALIZED

    def test_phone_digits_match(self):
        pd = {1: _page([_line("Call (555) 123-4567 today", 0.1, 0.5)])}
        geom, source, _ = g.match_value_to_geometry("+15551234567", pd)
        assert geom is not None and source == g._SOURCE_NORMALIZED

    def test_levenshtein_near_miss(self):
        # OCR noise: "Acme Corporation" vs "Acrne Corporation" (rn->m).
        pd = {1: _page([_line("Acrne Corporation", 0.1, 0.6)])}
        geom, source, _ = g.match_value_to_geometry("Acme Corporation", pd)
        assert geom is not None
        assert source == g._SOURCE_FUZZY

    def test_exact_still_beats_normalized(self):
        # A direct exact OCR hit keeps "ocr" provenance, not "ocr-normalized".
        pd = {1: _page([_line("2022-04-04", 0.1, 0.7)])}
        geom, source, _ = g.match_value_to_geometry(
            "2022-04-04", pd, schema_hint={"format": "date"}
        )
        assert source == "ocr"

    def test_unrelated_number_does_not_match(self):
        # A different amount must NOT type-match (precision preserved).
        pd = {1: _page([_line("Total $9,999.99", 0.1, 0.8)])}
        assert (
            g.match_value_to_geometry("1234.00", pd, schema_hint={"type": "number"})
            is None
        )

    def test_typed_list_rows_disambiguate_by_row_order(self):
        # Two date rows in different formats; ocr_only row-order still applies.
        pd = {1: _page([_line("01/02/2022", 0.8, 0.2), _line("03/04/2022", 0.8, 0.5)])}
        assessment = {"rows": [{"d": {"confidence": 0.9}}, {"d": {"confidence": 0.9}}]}
        extraction = {"rows": [{"d": "2022-01-02"}, {"d": "2022-03-04"}]}
        schema = {
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"properties": {"d": {"format": "date"}}},
                }
            }
        }
        out = g.ground_assessment_geometry(
            assessment, extraction, pd, "ocr_only", schema
        )
        tops = [r["d"]["geometry"][0]["boundingBox"]["top"] for r in out["rows"]]
        assert tops == [pytest.approx(0.2), pytest.approx(0.5)]
        assert all(
            r["d"]["geometry_source"] == g._SOURCE_NORMALIZED for r in out["rows"]
        )


class TestWeakPartialDemotion:
    """A sub-half-coverage substring match is ranked last, not rejected (#1519).

    ``_TIER_PARTIAL`` had no coverage floor, which caused two failures: a fragment
    was accepted as the value's location, and because any candidate
    short-circuits the fuzzy and Levenshtein sweeps, a fragment also suppressed
    the match that was right.

    Rejecting fragments fixes both and costs more than it saves — measured over
    265 fields, 48 of the 62 sub-floor matches point at a value that genuinely is
    on the page across several lines, against 14 coincidences, and no absolute-size
    rule beats break-even. So a fragment is demoted below the whole-string tiers
    instead: it loses to a better match, and still supplies the only box where
    there is none. Measured over 50 sections through the full grounding path, no
    box is lost and 10 move to a better line.
    """

    def test_a_header_fragment_does_not_beat_the_line_it_fragments(self):
        """The headline case. OCR read "via" as "vla", so EXACT misses by one
        character. The page also carries an ``IDENTIFIED`` column header, which is
        a substring of the value at 29% coverage. The box must land on the
        sentence, not the header."""
        pd = {
            1: _page(
                [
                    _line("IDENTIFIED", 0.40, 0.13, width=0.07),
                    _line(
                        "Above identified vla arrest report.", 0.07, 0.37, width=0.23
                    ),
                ]
            )
        }
        geom, _source, _conf = g.match_value_to_geometry(
            "Above identified via arrest report.", pd
        )
        assert geom["boundingBox"]["top"] == 0.37, "matched the header, not the value"
        assert geom["boundingBox"]["width"] == 0.23

    def test_a_single_letter_loses_to_a_whole_string_match(self):
        """``"D."`` normalizes to the single letter ``d``, a substring of most
        English values, and was measured as a live candidate for a 34-character
        sentence. It must not win against a line resembling the whole value."""
        pd = {
            1: _page(
                [
                    _line("D.", 0.62, 0.24, width=0.014),
                    _line(
                        "Above identified vla arrest report.", 0.07, 0.37, width=0.23
                    ),
                ]
            )
        }
        geom, _s, _c = g.match_value_to_geometry(
            "Above identified via arrest report.", pd
        )
        assert geom["boundingBox"]["top"] == 0.37

    def test_a_fragment_is_still_used_when_nothing_better_exists(self):
        """Why fragments are demoted rather than rejected. Measured over 265
        fields, 48 of the 62 sub-floor matches point at a value that really is on
        the page across several lines, against 14 coincidences — so dropping them
        all loses more than it saves."""
        pd = {1: _page([_line("Eight speed forward, one reverse unit", 0.1, 0.3)])}
        value = "Gearbox: Eight speed forward, one reverse unit with reverse gear"
        geom, _s, _c = g.match_value_to_geometry(value, pd)
        assert geom is not None
        assert geom["boundingBox"]["top"] == 0.3

    def test_a_fragment_is_ranked_below_every_whole_string_tier(self):
        assert g._TIER_WEAK_PARTIAL > g._TIER_LEVENSHTEIN > g._TIER_FUZZY
        assert g._TIER_WEAK_PARTIAL > g._TIER_PARTIAL

    def test_a_correct_prefix_of_a_two_line_value_still_matches(self):
        """Measured: an address whose first line covers 65% of the value."""
        pd = {1: _page([_line("PO Box 307, Shortland Street", 0.2, 0.44)])}
        geom, source, _ = g.match_value_to_geometry(
            "PO Box 307, Shortland Street, Auckland 1140", pd
        )
        assert source == "ocr"
        assert geom["boundingBox"]["top"] == 0.44

    def test_a_correct_suffix_still_matches(self):
        """Measured: an author line whose email suffix covers 51% of the value."""
        pd = {1: _page([_line("menamul@kfupm.edu.sa", 0.3, 0.6)])}
        geom, _source, _ = g.match_value_to_geometry(
            "M. Enamul Hossain, menamul@kfupm.edu.sa", pd
        )
        assert geom["boundingBox"]["top"] == 0.6

    def test_the_floor_decides_the_tier_not_whether_there_is_a_box(self):
        """Both sides of the floor still yield a box when they are the only
        candidate — the floor only decides which tier they compete at. Measured
        against the NORMALIZED value, since that is what matching compares."""
        assert g._PARTIAL_MIN_COVERAGE == 0.5
        value = "abcdefgh"  # 8 chars normalized
        at_floor = {1: _page([_line("abcd", 0.1, 0.2)])}  # 4/8 == 0.5 -> PARTIAL
        below = {1: _page([_line("abc", 0.1, 0.3)])}  # 3/8 < 0.5 -> WEAK_PARTIAL
        assert g.match_value_to_geometry(value, at_floor) is not None
        assert g.match_value_to_geometry(value, below) is not None
        # But a sub-floor fragment never beats a near-whole-string match.
        both = {1: _page([_line("abc", 0.1, 0.3), _line("abcdefgX", 0.5, 0.6)])}
        geom, _s, _c = g.match_value_to_geometry(value, both)
        assert geom["boundingBox"]["top"] == 0.6


class TestLevenshteinCanReach:
    """The O(1) ceiling in front of the Levenshtein DP is exact, not heuristic.

    It exists because the ``_PARTIAL_MIN_COVERAGE`` floor (#1519) stopped
    fragments short-circuiting the sweep, so the DP started running against every
    line on the page for every long model-composed value. Measured over 50 real
    sections, grounding went 4.6s -> 25.4s with 98% of it inside ``fuzz_score``;
    with this guard it is 0.52s and the grounding results are byte-identical.
    """

    def test_it_never_skips_a_pair_that_would_have_matched(self):
        """The property that makes skipping safe: if the guard says a pair cannot
        reach the threshold, the real scorer must agree. Checked over pairs built
        to straddle the bound rather than a hand-picked table."""
        thr = g._LEVENSHTEIN_MATCH_THRESHOLD
        base = "above identified via arrest report"
        pairs = [(base, base[:n]) for n in range(1, len(base) + 1)]
        pairs += [(base, base + "x" * n) for n in range(1, 30)]
        pairs += [
            (base, "identified"),
            (base, "d"),
            (base, "above identified vla arrest report"),
            ("unknown", "un"),
            ("1,234.00", "1234.00"),
            ("", "abc"),
            ("abc", ""),
        ]
        skipped_but_matching = [
            (a, b)
            for a, b in pairs
            if not g._levenshtein_can_reach(a, b, thr)
            and g._levenshtein_sim(a, b) >= thr
        ]
        assert skipped_but_matching == []

    def test_the_known_recovery_case_is_not_skipped(self):
        """The one-character OCR slip #1519 turns on must survive the guard, or
        the fix it enables is silently undone."""
        thr = g._LEVENSHTEIN_MATCH_THRESHOLD
        value, ocr = (
            "above identified via arrest report",
            "above identified vla arrest report",
        )
        assert g._levenshtein_can_reach(value, ocr, thr)
        assert g._levenshtein_sim(value, ocr) >= thr

    def test_a_wildly_different_length_is_skipped(self):
        """The case that was costing the time: a long composed value against the
        short lines of a page."""
        thr = g._LEVENSHTEIN_MATCH_THRESHOLD
        value = "This handwritten document is a laboratory data sheet recording results"
        assert not g._levenshtein_can_reach(value, "UN 173", thr)

    def test_an_unavailable_scorer_does_not_narrow_the_tier(self):
        """If the evaluation module cannot be imported the guard must allow the
        pair through, so the tier degrades to inert (score 0.0) exactly as before
        rather than quietly rejecting candidates."""
        with patch.dict("sys.modules", {"idp_common.evaluation.text_matching": None}):
            assert g._levenshtein_can_reach("abc", "abcdefghijklmnop", 0.8) is True
