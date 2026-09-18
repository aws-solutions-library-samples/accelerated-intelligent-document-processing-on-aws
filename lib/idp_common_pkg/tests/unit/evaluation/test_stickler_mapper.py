# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the SticklerConfigMapper.

Tests focus on schema preprocessing: removing unevaluable object properties,
including free-form objects (additionalProperties without properties) and
arrays of free-form objects.
"""

import copy

import pytest

from idp_common.evaluation.stickler_mapper import SticklerConfigMapper


@pytest.mark.unit
class TestIsUnevaluableObject:
    """Tests for _is_unevaluable_object helper method."""

    def test_object_with_no_properties_key(self):
        """Free-form object with no properties key is unevaluable."""
        schema = {"type": "object", "additionalProperties": True}
        assert SticklerConfigMapper._is_unevaluable_object(schema) is True

    def test_object_with_empty_properties(self):
        """Object with empty properties dict is unevaluable."""
        schema = {"type": "object", "properties": {}}
        assert SticklerConfigMapper._is_unevaluable_object(schema) is True

    def test_object_with_additional_properties_and_no_properties(self):
        """Object with additionalProperties: true and no properties key."""
        schema = {
            "type": "object",
            "additionalProperties": True,
            "description": "Row data as objects keyed by column name.",
        }
        assert SticklerConfigMapper._is_unevaluable_object(schema) is True

    def test_object_with_defined_properties(self):
        """Object with defined properties is evaluable."""
        schema = {
            "type": "object",
            "properties": {"Name": {"type": "string"}},
        }
        assert SticklerConfigMapper._is_unevaluable_object(schema) is False

    def test_non_object_type(self):
        """Non-object types are not unevaluable objects."""
        assert SticklerConfigMapper._is_unevaluable_object({"type": "string"}) is False
        assert SticklerConfigMapper._is_unevaluable_object({"type": "array"}) is False
        assert SticklerConfigMapper._is_unevaluable_object({"type": "integer"}) is False

    def test_non_dict_input(self):
        """Non-dict input returns False."""
        assert SticklerConfigMapper._is_unevaluable_object("not a dict") is False
        assert SticklerConfigMapper._is_unevaluable_object(None) is False
        assert SticklerConfigMapper._is_unevaluable_object([]) is False

    def test_object_with_no_type(self):
        """Dict without type key is not unevaluable."""
        assert SticklerConfigMapper._is_unevaluable_object({"properties": {}}) is False

    def test_object_with_additional_properties_dict_and_no_properties(self):
        """Object with additionalProperties as a schema dict (not just true)."""
        schema = {
            "type": "object",
            "additionalProperties": {"type": "string"},
        }
        assert SticklerConfigMapper._is_unevaluable_object(schema) is True


@pytest.mark.unit
class TestIsUnevaluableArray:
    """Tests for _is_unevaluable_array helper method."""

    def test_array_without_items(self):
        """Array with no 'items' (genson empty-list output) is unevaluable."""
        assert SticklerConfigMapper._is_unevaluable_array({"type": "array"}) is True

    def test_array_with_freeform_object_items(self):
        """Array whose items are free-form objects is unevaluable."""
        schema = {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        }
        assert SticklerConfigMapper._is_unevaluable_array(schema) is True

    def test_array_with_typeless_items(self):
        """Array whose items have neither 'type' nor '$ref' is unevaluable."""
        schema = {"type": "array", "items": {"description": "no type"}}
        assert SticklerConfigMapper._is_unevaluable_array(schema) is True

    def test_array_of_primitives_is_evaluable(self):
        """Array of typed primitives is evaluable."""
        schema = {"type": "array", "items": {"type": "string"}}
        assert SticklerConfigMapper._is_unevaluable_array(schema) is False

    def test_array_of_structured_objects_is_evaluable(self):
        """Array of objects with properties is evaluable."""
        schema = {
            "type": "array",
            "items": {"type": "object", "properties": {"Name": {"type": "string"}}},
        }
        assert SticklerConfigMapper._is_unevaluable_array(schema) is False

    def test_array_with_ref_items_is_evaluable(self):
        """Array whose items are a $ref is evaluable (resolved later)."""
        schema = {"type": "array", "items": {"$ref": "#/$defs/Thing"}}
        assert SticklerConfigMapper._is_unevaluable_array(schema) is False

    def test_non_array_type(self):
        """Non-array schemas are not unevaluable arrays."""
        assert SticklerConfigMapper._is_unevaluable_array({"type": "string"}) is False
        assert SticklerConfigMapper._is_unevaluable_array({"type": "object"}) is False

    def test_non_dict_input(self):
        """Non-dict input returns False."""
        assert SticklerConfigMapper._is_unevaluable_array(None) is False
        assert SticklerConfigMapper._is_unevaluable_array([]) is False


@pytest.mark.unit
class TestRemoveEmptyObjectProperties:
    """Tests for _remove_empty_object_properties method."""

    def test_removes_object_with_empty_properties(self):
        """Original behavior: removes objects with empty properties dict."""
        schema = {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                "EmptyObj": {"type": "object", "properties": {}},
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "EmptyObj" not in schema["properties"]
        assert "Name" in schema["properties"]
        assert len(removed) == 1

    def test_removes_freeform_object_no_properties_key(self):
        """New behavior: removes objects with no properties key at all."""
        schema = {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                "FreeForm": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "FreeForm" not in schema["properties"]
        assert "Name" in schema["properties"]
        assert len(removed) == 1

    def test_removes_array_of_freeform_objects(self):
        """New behavior: removes arrays whose items are free-form objects."""
        schema = {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                "TableData": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "description": "Row data as objects keyed by column name.",
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "TableData" not in schema["properties"]
        assert "Name" in schema["properties"]
        assert len(removed) == 1

    def test_keeps_array_of_structured_objects(self):
        """Arrays of structured objects (with properties) are kept."""
        schema = {
            "type": "object",
            "properties": {
                "Inventors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Name": {"type": "string"},
                            "Location": {"type": "string"},
                        },
                    },
                },
            },
        }
        original = copy.deepcopy(schema)
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "Inventors" in schema["properties"]
        assert len(removed) == 0
        assert schema == original

    def test_removes_array_of_empty_property_objects(self):
        """Arrays whose items are objects with empty properties are removed."""
        schema = {
            "type": "object",
            "properties": {
                "EmptyArray": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "EmptyArray" not in schema["properties"]
        assert len(removed) == 1

    def test_removes_array_without_items(self):
        """Arrays with no 'items' schema (genson empty-list output) are removed."""
        schema = {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                # genson emits a bare {"type": "array"} for an empty list []
                "OtherAssets": {"type": "array"},
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "OtherAssets" not in schema["properties"]
        assert "Name" in schema["properties"]
        assert len(removed) == 1

    def test_removes_object_emptied_by_empty_array_children(self):
        """Parent object becomes empty after its empty-array children are removed.

        Mirrors the URLA 'RealEstate' case: an object whose only properties are
        empty arrays (no 'items') must itself be removed, not left empty (which
        Stickler cannot evaluate).
        """
        schema = {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                "RealEstate": {
                    "type": "object",
                    "properties": {
                        "PropertyOwned": {"type": "array"},
                        "AdditionalProperty": {"type": "array"},
                    },
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "RealEstate" not in schema["properties"]
        assert "Name" in schema["properties"]
        # The parent and both empty-array children are reported as removed
        assert any(p.endswith("RealEstate") for p in removed)

    def test_nested_freeform_object_in_parent_object(self):
        """Free-form objects nested inside a parent object are removed."""
        schema = {
            "type": "object",
            "properties": {
                "Table": {
                    "type": "object",
                    "properties": {
                        "TableNumber": {"type": "string"},
                        "TableData": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    },
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        # Table should remain but TableData inside it should be removed
        assert "Table" in schema["properties"]
        assert "TableNumber" in schema["properties"]["Table"]["properties"]
        assert "TableData" not in schema["properties"]["Table"]["properties"]
        assert len(removed) == 1

    def test_defs_freeform_object_removed(self):
        """Free-form objects in $defs are removed."""
        schema = {
            "type": "object",
            "properties": {"Name": {"type": "string"}},
            "$defs": {
                "FreeFormDef": {
                    "type": "object",
                    "additionalProperties": True,
                },
                "StructuredDef": {
                    "type": "object",
                    "properties": {"Field": {"type": "string"}},
                },
            },
        }
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert "FreeFormDef" not in schema["$defs"]
        assert "StructuredDef" in schema["$defs"]
        assert len(removed) == 1

    def test_keeps_simple_arrays(self):
        """Simple arrays (of primitives) are untouched."""
        schema = {
            "type": "object",
            "properties": {
                "Tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        original = copy.deepcopy(schema)
        removed = SticklerConfigMapper._remove_empty_object_properties(schema)
        assert schema == original
        assert len(removed) == 0


@pytest.mark.unit
class TestUSPatentTableSchema:
    """Integration-style test using the actual USPatent Table schema pattern."""

    def test_uspatent_table_schema_preprocessing(self):
        """Test that the USPatent Table schema with TableData using
        additionalProperties is handled correctly by build_stickler_model_config.

        This reproduces the exact error:
        'JSON Schema must contain properties key for object type'
        that occurs when processing USPatent documents with TableData defined as:
        {"type": "array", "items": {"type": "object", "additionalProperties": true}}
        """
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "USPatent",
            "x-aws-idp-document-type": "USPatent",
            "type": "object",
            "properties": {
                "PatentNumber": {
                    "type": "string",
                    "x-aws-idp-evaluation-method": "EXACT",
                },
                "Tables": {
                    "type": "array",
                    "items": {
                        "$ref": "#/$defs/Table",
                    },
                    "x-aws-idp-evaluation-method": "HUNGARIAN",
                },
            },
            "$defs": {
                "Table": {
                    "type": "object",
                    "properties": {
                        "TableNumber": {"type": "string"},
                        "TableTitle": {"type": "string"},
                        "ColumnHeaders": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "RowCount": {"type": "integer"},
                        "TableData": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                            "description": "Row data as objects keyed by column name.",
                        },
                        "DataType": {"type": "string"},
                    },
                },
            },
        }

        # This should NOT raise an error - TableData should be removed during preprocessing
        config = SticklerConfigMapper.build_stickler_model_config(schema)

        # Verify config was built successfully
        assert config["model_name"] == "USPatent"

        # Verify the schema was processed: TableData should have been removed
        # after $ref inlining
        result_schema = config["schema"]
        tables_items = result_schema["properties"]["Tables"]["items"]
        assert "TableNumber" in tables_items["properties"]
        assert "TableTitle" in tables_items["properties"]
        assert "DataType" in tables_items["properties"]
        # TableData should have been removed because its items are free-form objects
        assert "TableData" not in tables_items["properties"]


class TestRequiredFieldsClearedForEvaluation:
    """Explicit-config `required` arrays must be cleared so a correctly-null field
    is scored as a miss, not a whole-document schema failure.

    Regression: RealKIE Invoice marks `required: [Agency, Advertiser, LineItems]`.
    A document where Agency is genuinely absent (extracted null) previously crashed
    the entire doc with 'Field required [type=missing]' -> __EVALUATION_FAILURE__ and
    a 0 score. All fields must be optional during evaluation.
    """

    def test_top_level_required_cleared(self):
        schema = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "required": ["Agency", "Advertiser", "LineItems"],
            "properties": {
                "Agency": {
                    "type": "string",
                    "x-aws-idp-evaluation-method": "LEVENSHTEIN",
                },
                "Advertiser": {
                    "type": "string",
                    "x-aws-idp-evaluation-method": "FUZZY",
                },
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["LineItemRate"],
                        "properties": {
                            "LineItemRate": {
                                "type": "number",
                                "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                            }
                        },
                    },
                },
            },
        }
        config = SticklerConfigMapper.build_stickler_model_config(schema)
        result = config["schema"]

        # Top-level required must be emptied (not left as the original 3 fields).
        assert result.get("required") == []
        # Nested object inside the list items must also be emptied.
        nested = result["properties"]["LineItems"]["items"]
        assert nested.get("required") == []


@pytest.mark.unit
class TestDateMethodMapping:
    """The DATE evaluation method maps to Stickler's DateComparator (v0.5.0+)
    and passes any x-aws-idp-evaluation-method-config through verbatim as
    x-aws-stickler-comparator-config.
    """

    def test_date_method_maps_to_date_comparator(self):
        schema = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": {
                "InvoiceDate": {
                    "type": "string",
                    "format": "date",
                    "x-aws-idp-evaluation-method": "DATE",
                },
            },
        }
        result = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        prop = result["properties"]["InvoiceDate"]
        assert prop["x-aws-stickler-comparator"] == "DateComparator"
        # No config provided -> no comparator-config extension emitted.
        assert "x-aws-stickler-comparator-config" not in prop

    def test_date_method_config_passed_through(self):
        schema = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": {
                "InvoiceDate": {
                    "type": "string",
                    "x-aws-idp-evaluation-method": "DATE",
                    "x-aws-idp-evaluation-method-config": {
                        "dayfirst": False,
                        "range_mode": "contains",
                    },
                },
            },
        }
        result = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        prop = result["properties"]["InvoiceDate"]
        assert prop["x-aws-stickler-comparator"] == "DateComparator"
        assert prop["x-aws-stickler-comparator-config"] == {
            "dayfirst": False,
            "range_mode": "contains",
        }

    def test_date_method_in_method_to_comparator_map(self):
        assert SticklerConfigMapper.METHOD_TO_COMPARATOR["DATE"] == "DateComparator"

    def test_date_method_rejected_on_structured_array(self):
        # DATE is a scalar-field method; using it on a List[Object] is invalid
        # and _validate_method_for_field must raise.
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"d": {"type": "string"}},
            },
        }
        with pytest.raises(ValueError, match="DATE"):
            SticklerConfigMapper._validate_method_for_field(schema, "DATE", "dates")

    def test_date_field_builds_end_to_end(self):
        """A DATE field must convert cleanly through the real Stickler
        JsonSchemaFieldConverter + Pydantic model build, and score
        format-variant dates as a match."""
        from pydantic import create_model
        from stickler import StructuredModel
        from stickler.structured_object_evaluator.models.json_schema_field_converter import (  # noqa: E501
            JsonSchemaFieldConverter,
        )

        schema = {
            "$id": "D",
            "x-aws-idp-document-type": "D",
            "type": "object",
            "properties": {
                "d": {"type": "string", "x-aws-idp-evaluation-method": "DATE"}
            },
            "required": ["d"],
        }
        sch = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        conv = JsonSchemaFieldConverter(sch)
        fields = conv.convert_properties_to_fields(
            sch.get("properties", {}), sch.get("required", [])
        )
        model = create_model(  # type: ignore  # pyright: reportCallIssue=false
            "D", **fields, __base__=StructuredModel
        )

        # Same calendar day, different surface form -> perfect match.
        assert model(d="2024-01-05").compare(model(d="January 5, 2024")) == 1.0
        # Genuinely different dates -> no match.
        assert model(d="2024-01-05").compare(model(d="2024-06-30")) == 0.0


@pytest.mark.unit
class TestNumericExactToleranceRouting:
    """R1: NUMERIC_EXACT with evaluation-threshold must route to
    comparator-config.tolerance, not x-aws-stickler-threshold (NumericComparator
    is binary 1.0/0.0 so a score-threshold gate is a no-op)."""

    def _build(self, threshold):
        schema = {
            "$id": "N",
            "x-aws-idp-document-type": "N",
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                    "x-aws-idp-evaluation-threshold": threshold,
                }
            },
        }
        return SticklerConfigMapper.build_stickler_model_config(schema)["schema"]

    def test_small_tolerance_routed_to_comparator_config(self):
        result = self._build(0.5)
        prop = result["properties"]["amount"]
        assert prop["x-aws-stickler-comparator"] == "NumericComparator"
        assert prop["x-aws-stickler-comparator-config"] == {"tolerance": 0.5}
        assert "x-aws-stickler-threshold" not in prop

    def test_large_tolerance_over_one_routed_to_comparator_config(self):
        """>1.0 tolerance (e.g. ±$5) would crash the 0.0–1.0 threshold
        validator; comparator-config accepts any positive number."""
        result = self._build(5.0)
        prop = result["properties"]["amount"]
        assert prop["x-aws-stickler-comparator-config"] == {"tolerance": 5.0}
        assert "x-aws-stickler-threshold" not in prop

    def test_numeric_exact_no_threshold_leaves_config_absent(self):
        schema = {
            "$id": "N",
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                }
            },
        }
        result = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        prop = result["properties"]["amount"]
        assert prop["x-aws-stickler-comparator"] == "NumericComparator"
        assert "x-aws-stickler-comparator-config" not in prop
        assert "x-aws-stickler-threshold" not in prop

    def test_tolerance_reaches_comparator_end_to_end(self):
        """A tolerance of 0.5 must actually make Stickler match values within
        ±0.5 — the original bug was that the tolerance never reached the
        comparator, so any nonzero diff scored 0.0."""
        from pydantic import create_model
        from stickler import StructuredModel
        from stickler.structured_object_evaluator.models.json_schema_field_converter import (  # noqa: E501
            JsonSchemaFieldConverter,
        )

        sch = self._build(0.5)
        conv = JsonSchemaFieldConverter(sch)
        fields = conv.convert_properties_to_fields(
            sch.get("properties", {}), sch.get("required", [])
        )
        model = create_model(  # type: ignore
            "N", **fields, __base__=StructuredModel
        )
        # diff 0.25 <= tolerance 0.5 -> match
        assert model(amount=100.0).compare(model(amount=100.25)) == 1.0
        # diff 1.0 > tolerance 0.5 -> no match
        assert model(amount=100.0).compare(model(amount=101.0)) == 0.0


@pytest.mark.unit
class TestMatchThresholdOnItemsSchema:
    """R2 (mapper part): match_threshold for structured arrays must land on
    the items schema so Stickler's element-class builder picks it up."""

    def test_match_threshold_copied_to_items(self):
        schema = {
            "$id": "I",
            "x-aws-idp-document-type": "I",
            "type": "object",
            "properties": {
                "LineItems": {
                    "type": "array",
                    "x-aws-idp-evaluation-match-threshold": 0.55,
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}},
                    },
                }
            },
        }
        result = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        arr = result["properties"]["LineItems"]
        # Stickler 1.0's importer rejects ``x-aws-stickler-match-threshold``
        # on the array field itself with an "unread extension" error
        # (breaking change #312). The value must land on the item OBJECT —
        # ``items`` — where the element-class builder reads it.
        assert "x-aws-stickler-match-threshold" not in arr
        assert arr["items"]["x-aws-stickler-match-threshold"] == 0.55

    def test_no_match_threshold_leaves_items_untouched(self):
        schema = {
            "$id": "I",
            "type": "object",
            "properties": {
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}},
                    },
                }
            },
        }
        result = SticklerConfigMapper.build_stickler_model_config(schema)["schema"]
        arr = result["properties"]["LineItems"]
        assert "x-aws-stickler-match-threshold" not in arr
        assert "x-aws-stickler-match-threshold" not in arr["items"]
