# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for $ref confidence threshold resolution in array fields.

Verifies that per-sub-field ``x-aws-idp-confidence-threshold`` values defined
inside ``$defs`` (referenced via ``$ref`` in array items) are correctly resolved
and applied during assessment enrichment — for both the standalone assessment
service path and the integrated (batching.py) enrichment path.
"""

from __future__ import annotations

from idp_common.assessment.batching import enrich_assessment_with_thresholds
from idp_common.assessment.threshold_resolver import (
    get_threshold_for_field,
    resolve_array_item_thresholds,
)

# --- Fixtures: W2 schema pattern ---

W2_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "w2",
    "x-aws-idp-document-type": "w2",
    "type": "object",
    "properties": {
        "layout_type": {"type": "string"},
        "w2_copies": {
            "type": "array",
            "x-aws-idp-evaluation-method": "LLM",
            "description": "Array of W2 copies on the page.",
            "items": {"$ref": "#/$defs/W2CopyItem"},
        },
    },
    "$defs": {
        "W2CopyItem": {
            "type": "object",
            "properties": {
                "w2_form_year": {
                    "type": "string",
                    "description": "4-digit tax year.",
                },
                "w2_box_a_employee_ssn": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": "0.8",
                    "description": "Employee SSN.",
                },
                "w2_box_b_employer_ein": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": "0.85",
                    "description": "Employer EIN.",
                },
                "w2_box_1_wages": {
                    "type": "number",
                    "x-aws-idp-confidence-threshold": "0.9",
                    "description": "Wages amount.",
                },
            },
        }
    },
}

# Schema with inline items (no $ref)
INLINE_ARRAY_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "x-aws-idp-confidence-threshold": "0.7",
                    },
                    "amount": {
                        "type": "number",
                        "x-aws-idp-confidence-threshold": "0.95",
                    },
                    "description": {
                        "type": "string",
                        # No threshold — should use default
                    },
                },
            },
        }
    },
}

# Schema with no $defs (broken $ref)
BROKEN_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"$ref": "#/$defs/NonExistent"},
        }
    },
    "$defs": {},
}


# --- Tests for threshold_resolver.py ---


class TestResolveArrayItemThresholds:
    def test_resolves_ref_to_defs(self):
        """$ref is resolved against $defs and per-field thresholds extracted."""
        prop_schema = W2_SCHEMA["properties"]["w2_copies"]
        thresholds = resolve_array_item_thresholds(prop_schema, W2_SCHEMA, 0.5)

        assert thresholds["w2_box_a_employee_ssn"] == 0.8
        assert thresholds["w2_box_b_employer_ein"] == 0.85
        assert thresholds["w2_box_1_wages"] == 0.9
        # No explicit threshold -> uses default
        assert thresholds["w2_form_year"] == 0.5

    def test_resolves_inline_items(self):
        """Inline items (no $ref) also have per-field thresholds extracted."""
        prop_schema = INLINE_ARRAY_SCHEMA["properties"]["transactions"]
        thresholds = resolve_array_item_thresholds(
            prop_schema, INLINE_ARRAY_SCHEMA, 0.9
        )

        assert thresholds["date"] == 0.7
        assert thresholds["amount"] == 0.95
        assert thresholds["description"] == 0.9  # default

    def test_broken_ref_returns_empty(self):
        """Unresolvable $ref returns empty dict (fallback to uniform threshold)."""
        prop_schema = BROKEN_REF_SCHEMA["properties"]["items"]
        thresholds = resolve_array_item_thresholds(prop_schema, BROKEN_REF_SCHEMA, 0.9)
        assert thresholds == {}

    def test_no_items_schema_returns_empty(self):
        """Property with no items key returns empty."""
        prop_schema = {"type": "array"}
        thresholds = resolve_array_item_thresholds(prop_schema, {}, 0.9)
        assert thresholds == {}

    def test_items_without_properties_returns_empty(self):
        """Items schema with no properties (e.g. array of strings) returns empty."""
        prop_schema = {"type": "array", "items": {"type": "string"}}
        thresholds = resolve_array_item_thresholds(prop_schema, {}, 0.9)
        assert thresholds == {}


class TestGetThresholdForField:
    def test_returns_field_threshold(self):
        thresholds = {"ssn": 0.8, "ein": 0.85}
        assert get_threshold_for_field("ssn", thresholds, 0.9) == 0.8
        assert get_threshold_for_field("ein", thresholds, 0.9) == 0.85

    def test_returns_default_for_missing_field(self):
        thresholds = {"ssn": 0.8}
        assert get_threshold_for_field("unknown_field", thresholds, 0.9) == 0.9


# --- Tests for batching.py enrichment with $ref ---


class TestEnrichAssessmentWithThresholdsRef:
    def test_array_items_get_per_sub_field_thresholds(self):
        """Array field with $ref should apply per-sub-field thresholds."""
        assessment = {
            "w2_copies": [
                {
                    "w2_box_a_employee_ssn": {"confidence": 0.75},
                    "w2_box_b_employer_ein": {"confidence": 0.9},
                    "w2_form_year": {"confidence": 0.6},
                }
            ]
        }
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, W2_SCHEMA, default_confidence_threshold=0.5
        )

        # SSN has threshold 0.8; confidence 0.75 < 0.8 -> should alert
        assert (
            enriched["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        # EIN has threshold 0.85; confidence 0.9 >= 0.85 -> no alert
        assert (
            enriched["w2_copies"][0]["w2_box_b_employer_ein"]["confidence_threshold"]
            == 0.85
        )
        # form_year has no explicit threshold -> uses default (0.5)
        assert enriched["w2_copies"][0]["w2_form_year"]["confidence_threshold"] == 0.5

        # Verify alerts
        ssn_alerts = [a for a in alerts if "employee_ssn" in a["attribute_name"]]
        assert len(ssn_alerts) == 1
        assert ssn_alerts[0]["confidence"] == 0.75
        assert ssn_alerts[0]["confidence_threshold"] == 0.8

        # EIN 0.9 >= 0.85 -> no alert
        ein_alerts = [a for a in alerts if "employer_ein" in a["attribute_name"]]
        assert len(ein_alerts) == 0

    def test_array_items_multiple_rows(self):
        """Multiple array items each get per-sub-field thresholds independently."""
        assessment = {
            "w2_copies": [
                {
                    "w2_box_a_employee_ssn": {"confidence": 0.95},
                    "w2_box_1_wages": {"confidence": 0.7},
                },
                {
                    "w2_box_a_employee_ssn": {"confidence": 0.5},
                    "w2_box_1_wages": {"confidence": 0.95},
                },
            ]
        }
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, W2_SCHEMA, default_confidence_threshold=0.5
        )

        # Row 0: SSN 0.95 >= 0.8 ok; wages 0.7 < 0.9 alert
        assert (
            enriched["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        assert enriched["w2_copies"][0]["w2_box_1_wages"]["confidence_threshold"] == 0.9

        # Row 1: SSN 0.5 < 0.8 alert; wages 0.95 >= 0.9 ok
        assert (
            enriched["w2_copies"][1]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        assert enriched["w2_copies"][1]["w2_box_1_wages"]["confidence_threshold"] == 0.9

        # Should have 2 alerts total: row[0].wages and row[1].ssn
        wage_alerts = [a for a in alerts if "wages" in a["attribute_name"]]
        ssn_alerts = [a for a in alerts if "employee_ssn" in a["attribute_name"]]
        assert len(wage_alerts) == 1  # row 0
        assert len(ssn_alerts) == 1  # row 1

    def test_inline_array_items_get_per_sub_field_thresholds(self):
        """Inline items (no $ref) also get per-sub-field thresholds."""
        assessment = {
            "transactions": [
                {
                    "date": {"confidence": 0.5},
                    "amount": {"confidence": 0.98},
                    "description": {"confidence": 0.8},
                }
            ]
        }
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, INLINE_ARRAY_SCHEMA, default_confidence_threshold=0.9
        )

        # date threshold is 0.7; confidence 0.5 < 0.7 -> alert
        assert enriched["transactions"][0]["date"]["confidence_threshold"] == 0.7
        # amount threshold is 0.95; confidence 0.98 >= 0.95 -> ok
        assert enriched["transactions"][0]["amount"]["confidence_threshold"] == 0.95
        # description has no explicit threshold -> default 0.9
        assert enriched["transactions"][0]["description"]["confidence_threshold"] == 0.9

        date_alerts = [a for a in alerts if "date" in a["attribute_name"]]
        assert len(date_alerts) == 1
        assert date_alerts[0]["confidence"] == 0.5
        assert date_alerts[0]["confidence_threshold"] == 0.7

    def test_broken_ref_falls_back_to_uniform_threshold(self):
        """When $ref can't be resolved, all sub-fields get the default threshold."""
        assessment = {
            "items": [
                {"field_a": {"confidence": 0.6}},
            ]
        }
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, BROKEN_REF_SCHEMA, default_confidence_threshold=0.9
        )
        # Falls back to uniform 0.9 (the default)
        assert enriched["items"][0]["field_a"]["confidence_threshold"] == 0.9
        # 0.6 < 0.9 -> alert
        assert len(alerts) == 1

    def test_scalar_fields_unchanged(self):
        """Non-array fields still work as before."""
        schema = {
            "properties": {
                "name": {"x-aws-idp-confidence-threshold": "0.7"},
                "age": {},
            }
        }
        assessment = {
            "name": {"confidence": 0.5},
            "age": {"confidence": 0.95},
        }
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, schema, default_confidence_threshold=0.9
        )
        assert enriched["name"]["confidence_threshold"] == 0.7
        assert enriched["age"]["confidence_threshold"] == 0.9
        # name 0.5 < 0.7 -> alert
        assert len(alerts) == 1
        assert alerts[0]["attribute_name"] == "name"

    def test_hitl_threshold_0_with_ref_uses_field_thresholds(self):
        """Reproduces the customer bug: hitl.confidence_threshold=0.0 but fields have 0.8."""
        assessment = {
            "w2_copies": [
                {
                    "w2_box_a_employee_ssn": {"confidence": 0.75},
                    "w2_form_year": {"confidence": 0.6},
                }
            ]
        }
        # Customer had hitl.confidence_threshold = 0.0
        enriched, alerts = enrich_assessment_with_thresholds(
            assessment, W2_SCHEMA, default_confidence_threshold=0.0
        )

        # SSN should use its explicit 0.8 threshold, NOT the 0.0 default
        assert (
            enriched["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        # form_year has no explicit threshold -> uses default 0.0
        assert enriched["w2_copies"][0]["w2_form_year"]["confidence_threshold"] == 0.0

        # SSN 0.75 < 0.8 -> should alert
        ssn_alerts = [a for a in alerts if "employee_ssn" in a["attribute_name"]]
        assert len(ssn_alerts) == 1
        assert ssn_alerts[0]["confidence_threshold"] == 0.8


class TestRefWrappedArrayContainerParity:
    """A ``$ref``-wrapped array container must enrich like an inline one.

    The integrated-confidence / BDA path read ``type`` and ``items`` off the RAW
    property, so a property declared as ``{"$ref": "#/$defs/TxnList"}`` — which
    carries neither — resolved zero per-sub-field thresholds and fell back to the
    uniform container threshold. The standalone path resolved them, so the SAME
    schema and the SAME confidences produced HITL alerts in
    ``confidence: separate`` and none in ``integrated`` (GitHub issue #678).
    """

    ITEM = {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "amount": {"type": "number", "x-aws-idp-confidence-threshold": 0.99},
        },
    }

    ASSESSMENT = {
        "txns": [
            {"date": {"confidence": 0.97}, "amount": {"confidence": 0.95}},
            {"date": {"confidence": 0.92}, "amount": {"confidence": 0.91}},
        ]
    }

    def _variants(self):
        return {
            # The hand-authored shape: the ARRAY itself lives in $defs.
            "ref_container": {
                "properties": {"txns": {"$ref": "#/$defs/TxnList"}},
                "$defs": {"TxnList": {"type": "array", "items": self.ITEM}},
            },
            # What the UI emits: inline array, item shape behind a $ref.
            "ref_items": {
                "properties": {
                    "txns": {"type": "array", "items": {"$ref": "#/$defs/Txn"}}
                },
                "$defs": {"Txn": self.ITEM},
            },
            "inline": {
                "properties": {"txns": {"type": "array", "items": self.ITEM}},
                "$defs": {},
            },
        }

    def test_all_three_shapes_resolve_identically(self):
        results = {}
        for label, schema in self._variants().items():
            enriched, alerts = enrich_assessment_with_thresholds(
                self.ASSESSMENT, schema, default_confidence_threshold=0.8
            )
            row0 = enriched["txns"][0]
            results[label] = (
                row0["amount"]["confidence_threshold"],
                row0["date"]["confidence_threshold"],
                len(alerts),
            )

        # The $ref container used to give (0.8, 0.8, 0) — uniform fallback, no
        # alerts — while the other two gave (0.99, 0.8, 2).
        assert results["ref_container"] == (0.99, 0.8, 2), results
        assert results["ref_container"] == results["ref_items"] == results["inline"]

    def test_alerts_name_the_offending_sub_field_per_row(self):
        schema = self._variants()["ref_container"]
        _, alerts = enrich_assessment_with_thresholds(
            self.ASSESSMENT, schema, default_confidence_threshold=0.8
        )
        names = {a["attribute_name"] for a in alerts}
        # amount 0.95/0.91 both breach its own 0.99; date 0.97/0.92 clear 0.8.
        assert names == {"txns[0].amount", "txns[1].amount"}

    def test_container_threshold_is_still_read_off_the_raw_property(self):
        """A threshold on the $defs DEFINITION must NOT become the container's.

        That would be a change to threshold *inheritance*, which belongs with
        ``resolve_threshold_for_path``'s rules — not with this type fix.
        """
        schema = {
            "properties": {"txns": {"$ref": "#/$defs/TxnList"}},
            "$defs": {
                "TxnList": {
                    "type": "array",
                    "x-aws-idp-confidence-threshold": 0.42,
                    "items": {
                        "type": "object",
                        "properties": {"date": {"type": "string"}},
                    },
                }
            },
        }
        enriched, _ = enrich_assessment_with_thresholds(
            {"txns": [{"date": {"confidence": 0.5}}]},
            schema,
            default_confidence_threshold=0.8,
        )
        # 0.8 (the caller default), NOT 0.42 from the definition.
        assert enriched["txns"][0]["date"]["confidence_threshold"] == 0.8
