# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for dynamic schema generation in evaluation service.
"""

from unittest.mock import patch

import pytest

from idp_common.evaluation.service import EvaluationService
from idp_common.models import Section


class TestDynamicSchemaGeneration:
    """Test suite for dynamic schema generation functionality."""

    @pytest.fixture
    def evaluation_service(self):
        """Create evaluation service with minimal config."""
        config = {
            "classes": [],  # Empty config to test auto-generation
            "evaluation": {
                "llm_method": {
                    "model": "us.anthropic.claude-3-haiku-20240307-v1:0",
                    "temperature": 0.0,
                }
            },
        }
        return EvaluationService(region="us-east-1", config=config)

    def test_infer_schema_from_simple_data(self, evaluation_service):
        """Auto-generated schemas keep type inference but no longer stamp
        per-leaf ``x-aws-idp-evaluation-method``: Stickler 1.0's native
        comparator inference (turned on by the mapper via
        ``x-aws-stickler-infer-unspecified: true`` at the schema root)
        picks a comparator per leaf from its type AND its field-name token,
        which is strictly better than the type-only fallback this service
        used to stamp here.
        """
        data = {
            "invoice_number": "INV-12345",
            "amount": 1250.50,
            "quantity": 5,
            "is_paid": True,
        }

        schema = evaluation_service._infer_schema_from_data(data, "Invoice")

        # Verify schema structure
        assert schema["type"] == "object"
        assert schema["x-aws-idp-document-type"] == "Invoice"
        assert "properties" in schema

        # Verify property types (unchanged)
        props = schema["properties"]
        assert props["invoice_number"]["type"] == "string"
        assert props["amount"]["type"] == "number"
        # Integers are normalized to "number" so int baselines match float
        # predictions without type-mismatch errors (_normalize_integer_to_number)
        assert props["quantity"]["type"] == "number"
        assert props["is_paid"]["type"] == "boolean"

        # No per-leaf evaluation-method stamping — inference happens later,
        # in Stickler, driven by the root flag the mapper sets.
        for prop in props.values():
            assert "x-aws-idp-evaluation-method" not in prop

    def test_infer_schema_from_nested_object(self, evaluation_service):
        """Test schema inference from nested object structure."""
        data = {
            "customer_name": "John Doe",
            "address": {
                "street": "123 Main St",
                "city": "Seattle",
                "zip": "98101",
            },
        }

        schema = evaluation_service._infer_schema_from_data(data, "Invoice")

        # Verify nested object structure
        props = schema["properties"]
        assert props["address"]["type"] == "object"
        assert "properties" in props["address"]

        nested_props = props["address"]["properties"]
        assert "street" in nested_props
        assert "city" in nested_props
        assert "zip" in nested_props

    def test_infer_schema_from_array_of_objects(self, evaluation_service):
        """Test schema inference from array of structured objects."""
        data = {
            "line_items": [
                {"description": "Widget", "quantity": 2, "price": 10.50},
                {"description": "Gadget", "quantity": 1, "price": 25.00},
            ]
        }

        schema = evaluation_service._infer_schema_from_data(data, "Invoice")

        # Verify array structure
        props = schema["properties"]
        assert props["line_items"]["type"] == "array"

        # Structured arrays no longer need an ``x-aws-idp-evaluation-method:
        # HUNGARIAN`` stamp: Stickler always uses Hungarian row-matching for
        # ``List[Object]`` and the mapper strips this key on structured arrays
        # regardless. The fallback that used to stamp it here is gone.
        assert "x-aws-idp-evaluation-method" not in props["line_items"]

        # Verify items schema
        items = props["line_items"]["items"]
        assert items["type"] == "object"
        assert "properties" in items

        item_props = items["properties"]
        assert "description" in item_props
        assert "quantity" in item_props
        assert "price" in item_props

    def test_infer_schema_from_array_of_primitives(self, evaluation_service):
        """Test schema inference from array of primitive values."""
        data = {"tags": ["urgent", "reviewed", "approved"]}

        schema = evaluation_service._infer_schema_from_data(data, "Document")

        # Verify array of strings
        props = schema["properties"]
        assert props["tags"]["type"] == "array"
        assert props["tags"]["items"]["type"] == "string"

    def test_infer_schema_from_empty_list(self, evaluation_service):
        """Test schema inference handles empty arrays gracefully."""
        data = {"empty_array": []}

        schema = evaluation_service._infer_schema_from_data(data, "Document")

        # genson generates array type without items for empty arrays (valid JSON Schema)
        props = schema["properties"]
        assert props["empty_array"]["type"] == "array"
        # genson doesn't add 'items' for empty arrays, which is valid JSON Schema

    def test_infer_schema_with_none_values(self, evaluation_service):
        """Test schema inference handles None values."""
        data = {"optional_field": None, "required_field": "value"}

        schema = evaluation_service._infer_schema_from_data(data, "Document")

        # genson infers None as "null", which Stickler can't evaluate, so
        # _normalize_null_types converts it to "string" (comparable as empty value)
        props = schema["properties"]
        assert props["optional_field"]["type"] == "string"
        assert props["required_field"]["type"] == "string"

    def test_infer_schema_strips_required(self, evaluation_service):
        """Auto-generated schemas must not carry genson's 'required' arrays.

        genson marks every observed key as required; for evaluation a missing
        field is a scored miss, not a hard validation failure. _strip_required
        removes them at every nesting level so the schema matches explicit configs.
        """
        data = {
            "name": "John",
            "address": {"street": "123 Main", "city": "Seattle"},
            "items": [{"sku": "A1", "qty": 2}],
        }

        schema = evaluation_service._infer_schema_from_data(data, "Document")

        assert "required" not in schema
        assert "required" not in schema["properties"]["address"]
        assert "required" not in schema["properties"]["items"]["items"]

    @patch("idp_common.evaluation.service.s3.get_json_content")
    def test_auto_generation_with_missing_config(
        self, mock_get_json, evaluation_service
    ):
        """Test that auto-generation works when config is missing."""
        # Mock S3 data loading
        expected_data = {"invoice_number": "INV-001", "amount": 100.0}
        mock_get_json.return_value = {"inference_result": expected_data}

        # Create section with class that doesn't have config
        section = Section(
            section_id="1",
            classification="UnconfiguredClass",
            page_ids=["1"],
            confidence=1.0,
            extraction_result_uri="s3://bucket/expected.json",
        )

        # This should trigger auto-generation
        result = evaluation_service.evaluate_section(
            section=section,
            expected_results=expected_data,
            actual_results=expected_data,
        )

        # Verify evaluation succeeded
        assert result.section_id == "1"
        assert result.document_class == "UnconfiguredClass"

        # Verify the model was marked as auto-generated
        assert "unconfiguredclass" in evaluation_service._auto_generated_models

    @patch("idp_common.evaluation.service.s3.get_json_content")
    def test_auto_generated_reason_annotation(self, mock_get_json, evaluation_service):
        """Test that auto-generated schemas are annotated in reason field."""
        # Mock S3 data loading
        expected_data = {"field1": "value1", "field2": 123}
        actual_data = {"field1": "value1", "field2": 456}

        mock_get_json.return_value = {"inference_result": expected_data}

        # Create section with unconfigured class
        section = Section(
            section_id="1",
            classification="TestClass",
            page_ids=["1"],
            confidence=1.0,
            extraction_result_uri="s3://bucket/expected.json",
        )

        # Evaluate
        result = evaluation_service.evaluate_section(
            section=section,
            expected_results=expected_data,
            actual_results=actual_data,
        )

        # Verify auto-generation annotation in reason field
        for attr in result.attributes:
            assert "Note: Schema inferred (no config)" in attr.reason

    def test_auto_generation_caching(self, evaluation_service):
        """Test that auto-generated schemas are cached for reuse."""
        data = {"field1": "value1"}

        # First call - should generate and cache
        model1 = evaluation_service._get_stickler_model("TestClass", expected_data=data)

        # Verify it's marked as auto-generated
        assert "testclass" in evaluation_service._auto_generated_models

        # Second call - should use cache
        model2 = evaluation_service._get_stickler_model("TestClass")

        # Should be the same model class
        assert model1 is model2

    def test_explicit_config_not_marked_auto_generated(self):
        """Test that explicit configs are not marked as auto-generated."""
        config = {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "x-aws-idp-document-type": "Invoice",
                    "type": "object",
                    "properties": {"invoice_number": {"type": "string"}},
                }
            ],
            "evaluation": {},
        }

        service = EvaluationService(region="us-east-1", config=config)

        # Verify explicit config is not in auto-generated set
        assert "invoice" not in service._auto_generated_models

    def test_complex_nested_structure(self, evaluation_service):
        """Test inference with deeply nested structure."""
        data = {
            "document_info": {
                "metadata": {"created_date": "2024-01-01", "version": 1},
                "items": [
                    {
                        "name": "Item1",
                        "details": {"price": 100.0, "quantity": 2},
                    }
                ],
            }
        }

        schema = evaluation_service._infer_schema_from_data(data, "ComplexDoc")

        # Verify nested structure is correctly inferred
        props = schema["properties"]
        assert props["document_info"]["type"] == "object"

        doc_info_props = props["document_info"]["properties"]
        assert "metadata" in doc_info_props
        assert "items" in doc_info_props

        # Verify metadata is object
        assert doc_info_props["metadata"]["type"] == "object"

        # Verify items is array of objects. No ``x-aws-idp-evaluation-method:
        # HUNGARIAN`` stamp — Stickler always Hungarian-matches List[Object].
        assert doc_info_props["items"]["type"] == "array"
        assert "x-aws-idp-evaluation-method" not in doc_info_props["items"]
