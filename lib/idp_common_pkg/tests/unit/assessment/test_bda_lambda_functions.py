# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Direct tests for BDA processresults Lambda-local functions.

These exercise the REAL functions in
``patterns/unified/src/bda_processresults_function/index.py`` —
``resolve_class_schema`` and
``add_confidence_thresholds_to_explainability_schema_aware`` — by loading that
module with ``importlib`` (the same approach as
``tests/unit/test_mlflow_logger.py``), with its one Lambda-only dependency
(``pypdfium2``) mocked out.

Loading the shipped module rather than re-implementing it is deliberate: an
earlier revision of these tests carried hand-written mirrors of both functions,
and because the mirrors omitted the very guard that was missing in the Lambda,
two real defects passed the suite. Testing the real thing removes that class of
false green.

Regressions guarded here:
  1. Multi-entry explainability_info must enrich ALL dict elements.
  2. Non-string ``x-aws-idp-document-type`` (legacy migration sets it to the
     boolean ``True``) must not crash.
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_INDEX_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/bda_processresults_function/index.py",
)

# pypdfium2 is a Lambda-runtime dependency of the module (PDF page rendering)
# and is not a test dependency of idp_common_pkg; nothing under test touches it.
with patch.dict("sys.modules", {"pypdfium2": MagicMock()}):
    _spec = importlib.util.spec_from_file_location("bda_processresults", _INDEX_PATH)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Could not load BDA processresults module at {_INDEX_PATH}")
    bda_index = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(bda_index)

resolve_class_schema = bda_index.resolve_class_schema
add_confidence_thresholds_to_explainability_flat = (
    bda_index.add_confidence_thresholds_to_explainability
)
add_confidence_thresholds_to_explainability_schema_aware = (
    bda_index.add_confidence_thresholds_to_explainability_schema_aware
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

W2_SCHEMA = {
    "x-aws-idp-document-type": "w2",
    "type": "object",
    "properties": {
        "w2_copies": {
            "type": "array",
            "items": {"$ref": "#/$defs/W2CopyItem"},
        },
    },
    "$defs": {
        "W2CopyItem": {
            "type": "object",
            "properties": {
                "w2_box_a_employee_ssn": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": "0.8",
                },
                "w2_box_1_wages": {
                    "type": "number",
                    "x-aws-idp-confidence-threshold": "0.9",
                },
            },
        }
    },
}


def _config_with(*schemas):
    """Build a minimal config-like object with a ``classes`` list."""
    return SimpleNamespace(classes=list(schemas))


# ---------------------------------------------------------------------------
# Tests: resolve_class_schema
# ---------------------------------------------------------------------------


class TestResolveClassSchema:
    """Tests for the Lambda-local resolve_class_schema function."""

    def test_finds_matching_schema(self):
        config = _config_with(W2_SCHEMA)
        assert resolve_class_schema("w2", config) is W2_SCHEMA

    def test_case_insensitive_match(self):
        config = _config_with(W2_SCHEMA)
        assert resolve_class_schema("W2", config) is W2_SCHEMA

    def test_returns_none_for_unknown_class(self):
        config = _config_with(W2_SCHEMA)
        assert resolve_class_schema("invoice", config) is None

    def test_returns_none_for_empty_doc_class(self):
        config = _config_with(W2_SCHEMA)
        assert resolve_class_schema("", config) is None

    def test_returns_none_for_none_config(self):
        assert resolve_class_schema("w2", None) is None

    def test_returns_none_for_config_without_classes(self):
        config = SimpleNamespace(other_field="x")
        assert resolve_class_schema("w2", config) is None

    def test_skips_non_dict_entries_in_classes(self):
        config = _config_with("not-a-dict", None, W2_SCHEMA)
        assert resolve_class_schema("w2", config) is W2_SCHEMA

    def test_boolean_document_type_does_not_crash(self):
        """Regression test: legacy migration sets x-aws-idp-document-type: True."""
        legacy_schema = {
            "x-aws-idp-document-type": True,  # boolean, NOT string
            "type": "object",
            "properties": {"field": {"type": "string"}},
        }
        config = _config_with(legacy_schema, W2_SCHEMA)
        # Should not raise AttributeError; boolean schema is skipped
        result = resolve_class_schema("w2", config)
        assert result is W2_SCHEMA

    def test_boolean_document_type_never_matches(self):
        """Boolean True should never match any doc_class string."""
        legacy_schema = {
            "x-aws-idp-document-type": True,
            "type": "object",
            "properties": {},
        }
        config = _config_with(legacy_schema)
        assert resolve_class_schema("True", config) is None
        assert resolve_class_schema("true", config) is None

    def test_none_classes_list(self):
        config = SimpleNamespace(classes=None)
        assert resolve_class_schema("w2", config) is None


# ---------------------------------------------------------------------------
# Tests: add_confidence_thresholds_to_explainability_schema_aware
# ---------------------------------------------------------------------------


class TestSchemaAwareEnrichment:
    """Tests for the Lambda-local schema-aware enrichment function."""

    def test_multi_entry_list_enriches_all_elements(self):
        """Regression test: all dict elements in the list must be enriched,
        not just [0]."""
        explainability_data = [
            {"w2_copies": [{"w2_box_a_employee_ssn": {"confidence": 0.75}}]},
            {"w2_copies": [{"w2_box_a_employee_ssn": {"confidence": 0.6}}]},
            {"w2_copies": [{"w2_box_1_wages": {"confidence": 0.85}}]},
        ]
        result_data = {"document_class": {"type": "w2"}}
        config = _config_with(W2_SCHEMA)

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.0, config
        )

        # ALL three entries must have thresholds applied
        assert (
            enriched[0]["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        assert (
            enriched[1]["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )
        assert (
            enriched[2]["w2_copies"][0]["w2_box_1_wages"]["confidence_threshold"] == 0.9
        )

    def test_single_entry_list(self):
        """Standard case: single-element list."""
        explainability_data = [
            {"w2_copies": [{"w2_box_a_employee_ssn": {"confidence": 0.75}}]},
        ]
        result_data = {"document_class": {"type": "w2"}}
        config = _config_with(W2_SCHEMA)

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.0, config
        )

        assert (
            enriched[0]["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )

    def test_non_dict_elements_pass_through(self):
        """Non-dict elements in the list are passed through unchanged."""
        explainability_data = [
            {"w2_copies": [{"w2_box_a_employee_ssn": {"confidence": 0.75}}]},
            "metadata_string",
            42,
        ]
        result_data = {"document_class": {"type": "w2"}}
        config = _config_with(W2_SCHEMA)

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.0, config
        )

        assert isinstance(enriched[0], dict)
        assert enriched[1] == "metadata_string"
        assert enriched[2] == 42

    def test_dict_input_enriched_directly(self):
        """When explainability_data is a dict (not wrapped in a list)."""
        explainability_data = {
            "w2_copies": [{"w2_box_a_employee_ssn": {"confidence": 0.75}}]
        }
        result_data = {"document_class": {"type": "w2"}}
        config = _config_with(W2_SCHEMA)

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.0, config
        )

        assert (
            enriched["w2_copies"][0]["w2_box_a_employee_ssn"]["confidence_threshold"]
            == 0.8
        )

    def test_unknown_class_falls_back_to_flat_threshold(self):
        """When the document class isn't found, flat threshold is applied."""
        explainability_data = [{"field": {"confidence": 0.5}}]
        result_data = {"document_class": {"type": "unknown_class"}}
        config = _config_with(W2_SCHEMA)

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.7, config
        )

        # Flat threshold applied recursively
        assert enriched[0]["field"]["confidence_threshold"] == 0.7

    def test_empty_list_returns_unchanged(self):
        """Empty list input returns as-is."""
        result = add_confidence_thresholds_to_explainability_schema_aware(
            [], {"document_class": {"type": "w2"}}, 0.5, _config_with(W2_SCHEMA)
        )
        assert result == []

    def test_boolean_document_type_schema_falls_back_to_flat(self):
        """Config with only boolean-typed schemas falls back to flat threshold."""
        legacy_schema = {
            "x-aws-idp-document-type": True,
            "type": "object",
            "properties": {"f": {"type": "string"}},
        }
        config = _config_with(legacy_schema)
        explainability_data = [{"f": {"confidence": 0.5}}]
        result_data = {"document_class": {"type": "w2"}}

        enriched = add_confidence_thresholds_to_explainability_schema_aware(
            explainability_data, result_data, 0.9, config
        )

        # Falls back to flat because no string-typed schema matches
        assert enriched[0]["f"]["confidence_threshold"] == 0.9


# ---------------------------------------------------------------------------
# Tests: process_segments — section classification confidence (GitHub #673)
# ---------------------------------------------------------------------------


class TestBDASectionClassConfidence:
    """BDA's matched-blueprint confidence is the section's class confidence.

    In BDA mode the matched blueprint is what decides the section's class, and
    its confidence was already being read for the HITL decision — it was simply
    never stored, so every section carried a hardcoded 1.0. This means BDA mode
    is scored with no extra inference and no prompt change.
    """

    @staticmethod
    def _document(page_ids):
        from idp_common.models import Document, Section

        doc = Document(id="pkg.pdf", input_key="pkg.pdf")
        doc.sections = [
            Section(section_id="1", classification="w2", page_ids=list(page_ids))
        ]
        return doc

    def _run(self, custom_output, status="MATCH"):
        doc = self._document(["1", "2"])
        segment = {
            "custom_output_status": status,
            "custom_output_path": "s3://bda/out/custom.json",
            "standard_output_path": "s3://bda/out/standard.json",
        }
        with (
            patch.object(bda_index, "download_json", return_value=custom_output),
            patch.object(bda_index, "download_decimal", return_value=custom_output),
            patch.object(bda_index, "is_hitl_enabled", return_value=False),
            patch.object(bda_index, "resolve_class_schema", return_value=None),
            patch.dict(os.environ, {"DB_NAME": ""}, clear=False),
            patch.object(bda_index, "boto3", MagicMock()),
        ):
            result, _ = bda_index.process_segments(
                input_bucket="in",
                output_bucket="out",
                object_key="pkg.pdf",
                segment_metadata=[segment],
                confidence_threshold=0.8,
                execution_id="exec-1",
                document=doc,
            )
        return result

    def test_matched_blueprint_confidence_becomes_section_confidence(self):
        result = self._run(
            {
                "matched_blueprint": {"name": "w2-bp", "confidence": 0.93},
                "split_document": {"page_indices": [0, 1]},
                "document_class": {"type": "w2"},
                "explainability_info": [],
            }
        )
        assert result.sections[0].confidence == 0.93

    def test_unmatched_segment_leaves_the_section_unscored(self):
        """No blueprint match => no class confidence, not a presumed 1.0."""
        result = self._run(
            {"metadata": {"start_page_index": 0, "end_page_index": 1}},
            status="NO_MATCH",
        )
        assert result.sections[0].confidence is None
