# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the 1S-TopK candidate resolver.

``resolve_candidates`` takes an LLM response where each field is a ``{G1, P1,
...}`` candidate object (top-K guesses + probabilities) and splits it into
``inference_result`` (G1 values) + raw confidence leaves (P1) + full candidate
metadata. ``is_topk_response`` detects that shape.
"""

from __future__ import annotations

import pytest

from idp_common.extraction.topk_resolver import (
    is_topk_response,
    resolve_candidates,
)

# --- is_topk_response ------------------------------------------------------


def test_detects_scalar_candidates():
    assert is_topk_response({"Agency": {"G1": "ACME", "P1": 0.9}}) is True


def test_detects_list_item_candidates():
    resp = {"Items": [{"rate": {"G1": "5", "P1": 0.8}}]}
    assert is_topk_response(resp) is True


def test_rejects_flat_response():
    assert is_topk_response({"Agency": "ACME", "Total": 100}) is False


def test_rejects_partial_marker():
    # A dict with G1 but no P1 is not a candidate object.
    assert is_topk_response({"Agency": {"G1": "ACME"}}) is False


# --- resolve_candidates: scalars -------------------------------------------


def test_scalar_resolution_takes_g1_and_p1():
    schema = {"properties": {"Agency": {}}}
    raw = {"Agency": {"G1": "ACME", "P1": 0.9, "G2": "ACMY", "P2": 0.1}}
    values, assess, candidates = resolve_candidates(raw, schema)
    assert values == {"Agency": "ACME"}
    assert assess == {"Agency": {"confidence": 0.9}}
    # Full candidate set preserved for audit.
    assert candidates["Agency"]["G2"] == "ACMY"


def test_number_value_is_coerced_to_float():
    schema = {"properties": {"Total": {"type": "number"}}}
    values, _assess, _c = resolve_candidates(
        {"Total": {"G1": "800.15", "P1": 0.9}}, schema
    )
    assert values["Total"] == 800.15
    assert isinstance(values["Total"], float)


def test_bad_number_value_left_as_is():
    schema = {"properties": {"Total": {"type": "number"}}}
    values, _assess, _c = resolve_candidates(
        {"Total": {"G1": "N/A", "P1": 0.5}}, schema
    )
    assert values["Total"] == "N/A"


def test_null_g1_preserved():
    schema = {"properties": {"Agency": {}}}
    values, assess, _c = resolve_candidates({"Agency": {"G1": None, "P1": 0.0}}, schema)
    assert values["Agency"] is None
    assert assess["Agency"]["confidence"] == 0.0


@pytest.mark.parametrize(
    "raw_p, expected",
    [(1.5, 1.0), (-0.2, 0.0), ("0.42", 0.42), ("bad", 0.0), (None, 0.0)],
)
def test_probability_clamped_and_coerced(raw_p, expected):
    schema = {"properties": {"A": {}}}
    _v, assess, _c = resolve_candidates({"A": {"G1": "x", "P1": raw_p}}, schema)
    assert assess["A"]["confidence"] == expected


def test_confidence_leaf_has_no_threshold():
    # Thresholds are the enricher's job — the resolver emits confidence only.
    schema = {"properties": {"A": {"x-aws-idp-confidence-threshold": "0.95"}}}
    _v, assess, _c = resolve_candidates({"A": {"G1": "x", "P1": 0.9}}, schema)
    assert assess["A"] == {"confidence": 0.9}


def test_non_candidate_field_passes_through():
    schema = {"properties": {"Agency": {}, "Note": {}}}
    raw = {"Agency": {"G1": "ACME", "P1": 0.9}, "Note": "plain string"}
    values, assess, _c = resolve_candidates(raw, schema)
    assert values["Note"] == "plain string"
    assert "Note" not in assess  # no confidence leaf for a passthrough field


def test_scalar_bbox_and_page_carried_onto_leaf():
    # Under llm/llm_grounded geometry the model emits bbox/page alongside the
    # candidate; the resolver carries them onto the confidence leaf so the shared
    # grounding path can consume them (the agentic tool preserves them the same way).
    schema = {"properties": {"Agency": {}}}
    raw = {"Agency": {"G1": "ACME", "P1": 0.9, "bbox": [1, 2, 3, 4], "page": 2}}
    _v, assess, _c = resolve_candidates(raw, schema)
    assert assess["Agency"] == {"confidence": 0.9, "bbox": [1, 2, 3, 4], "page": 2}


def test_scalar_leaf_omits_geometry_when_absent():
    # No bbox/page emitted -> leaf stays confidence-only (no empty geometry keys).
    schema = {"properties": {"Agency": {}}}
    _v, assess, _c = resolve_candidates({"Agency": {"G1": "ACME", "P1": 0.9}}, schema)
    assert assess["Agency"] == {"confidence": 0.9}


# --- resolve_candidates: arrays --------------------------------------------

_LIST_SCHEMA = {
    "properties": {"Items": {"type": "array", "items": {"$ref": "#/$defs/LineItem"}}},
    "$defs": {
        "LineItem": {
            "properties": {"rate": {"type": "number"}, "day": {"type": "string"}}
        }
    },
}


def test_direct_array_resolves_per_row_per_column():
    raw = {
        "Items": [
            {"rate": {"G1": "5", "P1": 0.8}, "day": {"G1": "Mon", "P1": 0.7}},
            {"rate": {"G1": "6", "P1": 0.6}, "day": {"G1": "Tue", "P1": 0.5}},
        ]
    }
    values, assess, _c = resolve_candidates(raw, _LIST_SCHEMA)
    assert values["Items"] == [
        {"rate": 5.0, "day": "Mon"},
        {"rate": 6.0, "day": "Tue"},
    ]
    assert assess["Items"][0]["rate"]["confidence"] == 0.8
    assert assess["Items"][1]["day"]["confidence"] == 0.5


def test_wrapped_array_candidate():
    # Some models wrap the whole list: {"Items": {"G1": [...rows...], "P1": 1.0}}
    raw = {
        "Items": {
            "G1": [{"rate": {"G1": "5", "P1": 0.8}}],
            "P1": 1.0,
        }
    }
    values, assess, _c = resolve_candidates(raw, _LIST_SCHEMA)
    assert values["Items"] == [{"rate": 5.0}]
    assert assess["Items"][0]["rate"]["confidence"] == 0.8


def test_array_item_non_candidate_subfield_passthrough():
    raw = {"Items": [{"rate": {"G1": "5", "P1": 0.8}, "note": "as-is"}]}
    values, assess, _c = resolve_candidates(raw, _LIST_SCHEMA)
    assert values["Items"][0]["note"] == "as-is"
    assert "note" not in assess["Items"][0]


def test_non_dict_array_item_preserved():
    raw = {"Items": ["not-a-dict", {"rate": {"G1": "5", "P1": 0.8}}]}
    values, assess, _c = resolve_candidates(raw, _LIST_SCHEMA)
    assert values["Items"][0] == "not-a-dict"
    assert assess["Items"][0] == {}
    assert values["Items"][1] == {"rate": 5.0}


def test_array_item_bbox_and_page_carried_onto_leaf():
    # Per-row candidates may also carry bbox/page under LLM-box geometry modes.
    raw = {"Items": [{"rate": {"G1": "5", "P1": 0.8, "bbox": [0, 0, 9, 9], "page": 1}}]}
    _v, assess, _c = resolve_candidates(raw, _LIST_SCHEMA)
    assert assess["Items"][0]["rate"] == {
        "confidence": 0.8,
        "bbox": [0, 0, 9, 9],
        "page": 1,
    }


# ---------------------------------------------------------------------------
# GROUP (nested object) fields — regression coverage.
#
# Before this was handled, a group whose sub-attributes were TopK candidates fell
# through to the pass-through branch, so the raw candidate dict became the
# EXTRACTED VALUE and the group produced no confidence leaves at all. Observed
# live in integrated mode: an "Account Holder Address" group came back as
# {"Street Number": {"G1": "100", "P1": 0.9, ...}, ...} instead of
# {"Street Number": "100", ...}, and explainability_info had no entry for it — so
# every group sub-field was silently corrupted AND invisible to confidence
# thresholds, alerts and HITL.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroupFields:
    GROUP_SCHEMA = {
        "properties": {
            "Address": {
                "type": "object",
                "properties": {
                    "Street": {"type": "string"},
                    "Zip": {"type": "string"},
                    "Floor": {"type": "number"},
                },
            }
        }
    }

    def test_group_subattributes_resolve_to_values(self):
        raw = {
            "Address": {
                "Street": {
                    "G1": "100 Main Street",
                    "P1": 0.9,
                    "G2": "Main St",
                    "P2": 0.1,
                },
                "Zip": {"G1": "90210", "P1": 0.95},
            }
        }
        result, assessment, candidates = resolve_candidates(raw, self.GROUP_SCHEMA)
        assert result["Address"] == {"Street": "100 Main Street", "Zip": "90210"}
        assert assessment["Address"] == {
            "Street": {"confidence": 0.9},
            "Zip": {"confidence": 0.95},
        }
        # Full candidates preserved for audit.
        assert candidates["Address"]["Street"]["G2"] == "Main St"

    def test_group_numeric_subattribute_is_coerced(self):
        raw = {"Address": {"Floor": {"G1": "3", "P1": 0.8}}}
        result, _, _ = resolve_candidates(raw, self.GROUP_SCHEMA)
        assert result["Address"]["Floor"] == 3.0
        assert isinstance(result["Address"]["Floor"], float)

    def test_group_behind_a_ref_is_dereferenced(self):
        schema = {
            "properties": {"Address": {"$ref": "#/$defs/Addr"}},
            "$defs": {
                "Addr": {
                    "type": "object",
                    "properties": {"Floor": {"type": "number"}},
                }
            },
        }
        raw = {"Address": {"Floor": {"G1": "7", "P1": 0.9}}}
        result, assessment, _ = resolve_candidates(raw, schema)
        assert result["Address"]["Floor"] == 7.0
        assert assessment["Address"]["Floor"]["confidence"] == 0.9

    def test_nested_group_inside_group(self):
        schema = {
            "properties": {
                "Outer": {
                    "type": "object",
                    "properties": {
                        "Inner": {
                            "type": "object",
                            "properties": {"Leaf": {"type": "string"}},
                        }
                    },
                }
            }
        }
        raw = {"Outer": {"Inner": {"Leaf": {"G1": "v", "P1": 0.6}}}}
        result, assessment, _ = resolve_candidates(raw, schema)
        assert result["Outer"]["Inner"]["Leaf"] == "v"
        assert assessment["Outer"]["Inner"]["Leaf"]["confidence"] == 0.6

    def test_group_inside_a_list_row(self):
        schema = {
            "properties": {
                "Rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Addr": {
                                "type": "object",
                                "properties": {"City": {"type": "string"}},
                            }
                        },
                    },
                }
            }
        }
        raw = {"Rows": [{"Addr": {"City": {"G1": "Anytown", "P1": 0.9}}}]}
        result, assessment, _ = resolve_candidates(raw, schema)
        assert result["Rows"][0]["Addr"] == {"City": "Anytown"}
        assert assessment["Rows"][0]["Addr"]["City"]["confidence"] == 0.9

    def test_plain_group_without_candidates_passes_through_unchanged(self):
        """A model that ignored TopK for a group must not be mangled."""
        raw = {"Address": {"Street": "100 Main Street", "Zip": "90210"}}
        result, assessment, _ = resolve_candidates(raw, self.GROUP_SCHEMA)
        assert result["Address"] == {"Street": "100 Main Street", "Zip": "90210"}
        assert "Address" not in assessment

    def test_mixed_group_keeps_non_candidate_subattributes(self):
        raw = {
            "Address": {
                "Street": {"G1": "100 Main Street", "P1": 0.9},
                "Zip": "90210",  # model ignored TopK for this one
            }
        }
        result, assessment, _ = resolve_candidates(raw, self.GROUP_SCHEMA)
        assert result["Address"] == {"Street": "100 Main Street", "Zip": "90210"}
        assert assessment["Address"] == {"Street": {"confidence": 0.9}}

    def test_group_geometry_is_carried_onto_the_leaf(self):
        raw = {
            "Address": {
                "Street": {
                    "G1": "100 Main Street",
                    "P1": 0.9,
                    "bbox": [1, 2, 3, 4],
                    "page": 2,
                }
            }
        }
        _, assessment, _ = resolve_candidates(raw, self.GROUP_SCHEMA)
        assert assessment["Address"]["Street"]["bbox"] == [1, 2, 3, 4]
        assert assessment["Address"]["Street"]["page"] == 2

    def test_group_with_no_schema_entry_still_resolves(self):
        """Auto-generated schemas may not describe the group at all."""
        raw = {"Unknown": {"Sub": {"G1": "value", "P1": 0.75}}}
        result, assessment, _ = resolve_candidates(raw, {"properties": {}})
        assert result["Unknown"] == {"Sub": "value"}
        assert assessment["Unknown"]["Sub"]["confidence"] == 0.75
