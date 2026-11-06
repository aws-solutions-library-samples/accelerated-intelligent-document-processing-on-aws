# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An inline object in a class schema that also has ``$defs`` (issue #1732).

``_transform_json_schema_to_bedrock_blueprint`` takes one of two paths depending
on whether the class schema has ``$defs``. The flat path already hoisted inline
objects into ``definitions``; the ``$defs`` path copied them through inline, and
BDA rejects that with "Request has invalid blueprint schema".

The shape facts below were measured against the BDA control plane
(``CreateBlueprint``, ``DOCUMENT``, us-west-2), not assumed:

- rejected: an object inline under ``properties``; an array whose ``items`` is
  an inline object; a ``$ref`` inside a definition; a definition carrying
  ``description``.
- accepted: the same objects moved into ``definitions`` and referenced with
  ``$ref``; and both expected blueprints in this file, byte for byte.
- every one of the SERVICE-owned catalog blueprints in that region uses
  ``definitions`` + ``$ref`` and none uses an inline object.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService

LEAF_DEFAULT = "Extract this field from the document"


def _leaf(type_: str, instruction: str = LEAF_DEFAULT) -> dict:
    return {"type": type_, "inferenceType": "explicit", "instruction": instruction}


# A class with $defs and every inline shape the $defs path has to handle.
INLINE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Probe1732",
    "type": "object",
    "description": "probe",
    "$defs": {
        "Contact": {
            "type": "object",
            "properties": {"Email": {"type": "string", "description": "email"}},
        }
    },
    "properties": {
        "Owner": {"$ref": "#/$defs/Contact", "description": "owner"},
        # Same name as an existing $defs entry: the hoisted definition must not
        # overwrite it.
        "Contact": {
            "type": "object",
            "description": "emergency contact",
            "properties": {
                "Name": {"type": "string", "description": "name"},
                "Phone": {"type": "string"},
            },
        },
        # No "type": an object is inferred from "properties".
        "Untyped": {"properties": {"A": {"type": "number"}}},
        "Rows": {
            "type": "array",
            "description": "rows",
            "items": {
                "type": "object",
                "properties": {"Amount": {"type": "number", "description": "amt"}},
            },
        },
        # Nothing representable left once the nested object is dropped.
        "Deep": {
            "type": "object",
            "properties": {
                "Inner": {"type": "object", "properties": {"X": {"type": "string"}}}
            },
        },
        # The array's items keep nothing representable either.
        "EmptyRows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Inner": {"type": "object", "properties": {"X": {"type": "string"}}}
                },
            },
        },
        # Items that are a $ref keep it, even with stray "properties" beside it.
        "RefRows": {
            "type": "array",
            "items": {"$ref": "#/$defs/Contact", "properties": {"Q": {}}},
        },
        # Declared a string: "properties" beside it does not make it an object.
        "Code": {"type": "string", "properties": {"Q": {"type": "string"}}},
        "Tags": {"type": "array", "items": {"type": "string"}},
        "Total": {"type": "number", "description": "total"},
        # A leaf and a primitive array survive; the nested object is dropped
        # individually, exactly as it is inside a $defs definition.
        "Deep2": {
            "type": "object",
            "properties": {
                "Leaf": {"type": "string"},
                "Inner": {"type": "object", "properties": {"X": {"type": "string"}}},
                "Arr": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

# Accepted by CreateBlueprint as-is. The converter before the fix produced a
# blueprint for INLINE_SCHEMA that CreateBlueprint rejected.
INLINE_EXPECTED = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "class": "Probe1732",
    "description": "probe",
    "type": "object",
    "definitions": {
        "Contact": {
            "type": "object",
            "properties": {"Email": _leaf("string", "email")},
        },
        "Contact2": {
            "type": "object",
            "properties": {"Name": _leaf("string", "name"), "Phone": _leaf("string")},
        },
        "Untyped": {"type": "object", "properties": {"A": _leaf("number")}},
        "RowsItem": {
            "type": "object",
            "properties": {"Amount": _leaf("number", "amt")},
        },
        "Deep2": {
            "type": "object",
            "properties": {
                "Leaf": _leaf("string"),
                "Arr": {
                    "type": "array",
                    "items": {"type": "string"},
                    "instruction": "-",
                },
            },
        },
    },
    "properties": {
        "Owner": {"$ref": "#/definitions/Contact", "instruction": "-"},
        "Contact": {"$ref": "#/definitions/Contact2", "instruction": "-"},
        "Untyped": {"$ref": "#/definitions/Untyped", "instruction": "-"},
        "Rows": {
            "type": "array",
            "items": {"$ref": "#/definitions/RowsItem"},
            "instruction": "rows",
        },
        "RefRows": {
            "type": "array",
            "items": {"$ref": "#/definitions/Contact"},
            "instruction": "-",
        },
        "Code": _leaf("string"),
        "Tags": {"type": "array", "items": {"type": "string"}, "instruction": "-"},
        "Total": _leaf("number", "total"),
        "Deep2": {"$ref": "#/definitions/Deep2", "instruction": "-"},
    },
}

# A $defs class with no inline objects, which BDA already accepted.
BDA_PROBED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Accepted1732",
    "type": "object",
    "description": "probe",
    "$defs": {
        "Contact": {
            "type": "object",
            "description": "c",
            "properties": {
                "Email": {"type": "string", "description": "email"},
                "Fax": {"type": "string"},
            },
        },
        "Line": {
            "type": "object",
            "properties": {"Amount": {"type": "number", "description": "amt"}},
        },
    },
    "properties": {
        "Owner": {"$ref": "#/$defs/Contact", "description": "owner"},
        "Lines": {
            "type": "array",
            "description": "lines",
            "items": {"$ref": "#/$defs/Line"},
        },
        "Tags": {"type": "array", "items": {"type": "string"}},
        "Total": {"type": "number", "description": "total"},
    },
}

# What the converter produced for BDA_PROBED_SCHEMA before the fix, recorded from
# that code. Unchanged output means no existing blueprint is re-versioned by
# the next sync's update check.
BDA_PROBED_BLUEPRINT = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "class": "Accepted1732",
    "description": "probe",
    "type": "object",
    "definitions": {
        "Contact": {
            "type": "object",
            "properties": {"Email": _leaf("string", "email"), "Fax": _leaf("string")},
        },
        "Line": {"type": "object", "properties": {"Amount": _leaf("number", "amt")}},
    },
    "properties": {
        "Owner": {"$ref": "#/definitions/Contact", "instruction": "-"},
        "Lines": {
            "type": "array",
            "items": {"$ref": "#/definitions/Line"},
            "instruction": "lines",
        },
        "Tags": {"type": "array", "items": {"type": "string"}, "instruction": "-"},
        "Total": _leaf("number", "total"),
    },
}


def _is_inline_object(schema) -> bool:
    return (
        isinstance(schema, dict)
        and "$ref" not in schema
        and (schema.get("type") == "object" or "properties" in schema)
    )


def assert_bda_accepted_shape(blueprint: dict) -> None:
    """Check a blueprint against the shapes CreateBlueprint was measured to reject.

    Probed against the control plane: an inline object under ``properties`` or as
    an array's ``items``, a ``$ref`` inside a definition, and a definition carrying
    ``description`` are each rejected. The remaining checks are this converter's
    own contract rather than probed BDA rules: every ``$ref`` resolves, no
    definition is empty, and no definition member is an object (the converter
    drops those). Primitive arrays inside a definition are not checked, because
    catalog blueprints contain them.
    """
    definitions = blueprint.get("definitions", {})
    checked = 0
    for name, prop in blueprint["properties"].items():
        checked += 1
        assert not _is_inline_object(prop), f"inline object at properties.{name}"
        assert not _is_inline_object(prop.get("items")), (
            f"inline object at properties.{name}.items"
        )
        for ref_holder in (prop, prop.get("items") or {}):
            ref = ref_holder.get("$ref")
            if ref is not None:
                assert ref.startswith("#/definitions/"), ref
                assert ref.removeprefix("#/definitions/") in definitions, ref
    for def_name, definition in definitions.items():
        assert "description" not in definition, f"description on {def_name}"
        assert definition["properties"], f"empty definition {def_name}"
        for name, member in definition["properties"].items():
            assert "$ref" not in member, f"$ref inside definitions.{def_name}"
            assert not _is_inline_object(member), (
                f"object nested at definitions.{def_name}.{name}"
            )
            assert not _is_inline_object(member.get("items")), (
                f"object items nested at definitions.{def_name}.{name}"
            )
    assert checked, "no properties checked"


@pytest.mark.unit
class TestInlineObjectInDefsSchema:
    @pytest.fixture
    def service(self):
        with (
            patch("boto3.resource"),
            patch("boto3.client"),
            patch("idp_common.bda.bda_blueprint_service.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            service = BdaBlueprintService(
                dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:project/test"
            )
            service.blueprint_creator = MagicMock()
            return service

    def test_inline_objects_are_hoisted_to_definitions(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(INLINE_SCHEMA)
        assert blueprint == INLINE_EXPECTED
        assert_bda_accepted_shape(blueprint)

    def test_property_names_and_order_are_preserved(self, service):
        """Extraction output is keyed by property name, so names must not move."""
        blueprint = service._transform_json_schema_to_bedrock_blueprint(INLINE_SCHEMA)
        kept = [
            n for n in INLINE_SCHEMA["properties"] if n not in ("Deep", "EmptyRows")
        ]
        assert list(blueprint["properties"]) == kept

    def test_a_hoisted_name_does_not_overwrite_an_existing_definition(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(INLINE_SCHEMA)
        assert blueprint["definitions"]["Contact"]["properties"] == {
            "Email": _leaf("string", "email")
        }
        assert blueprint["properties"]["Owner"]["$ref"] == "#/definitions/Contact"
        assert blueprint["properties"]["Contact"]["$ref"] == "#/definitions/Contact2"

    def test_a_hoisted_name_skips_every_taken_suffix(self, service):
        leaf = {"properties": {"A": {"type": "string"}}}
        schema = {
            "$id": "T",
            "$defs": {"X": leaf, "X2": leaf},
            "properties": {"X": {"type": "object", **leaf}},
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert list(blueprint["definitions"]) == ["X", "X2", "X3"]
        assert blueprint["properties"]["X"]["$ref"] == "#/definitions/X3"

    def test_an_untyped_array_of_inline_objects_is_hoisted(self, service):
        """An array with "items" but no "type" is treated as an array."""
        schema = {
            "$id": "T",
            "$defs": {"D": {"properties": {"A": {"type": "string"}}}},
            "properties": {
                "Rows": {"items": {"properties": {"B": {"type": "number"}}}}
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["Rows"] == {
            "type": "array",
            "items": {"$ref": "#/definitions/RowsItem"},
            "instruction": "-",
        }
        assert blueprint["definitions"]["RowsItem"] == {
            "type": "object",
            "properties": {"B": _leaf("number")},
        }
        assert_bda_accepted_shape(blueprint)

    def test_a_property_that_is_not_a_schema_object_does_not_raise(self, service):
        """Unchanged from before the fix: such a value is passed through as-is."""
        schema = {"$id": "T", "$defs": {}, "properties": {"Bad": "string"}}
        schema["$defs"]["D"] = {"properties": {"A": {"type": "string"}}}
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["Bad"] == "string"

    def test_unrepresentable_members_are_recorded_not_silently_lost(self, service):
        with service._recording_drops_for("Probe1732") as recording:
            service._transform_json_schema_to_bedrock_blueprint(INLINE_SCHEMA)
        dropped = sorted((d["property"], d["type"]) for d in recording.drops)
        assert dropped == [
            ("Deep", "nested_object"),
            ("EmptyRows", "nested_array"),
            ("Inner", "nested_object"),
            ("Inner", "nested_object"),
            ("Inner", "nested_object"),
        ]

    def test_input_is_not_mutated(self, service):
        before = json.dumps(INLINE_SCHEMA, sort_keys=True)
        service._transform_json_schema_to_bedrock_blueprint(INLINE_SCHEMA)
        assert json.dumps(INLINE_SCHEMA, sort_keys=True) == before

    def test_already_accepted_defs_schemas_convert_unchanged(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            BDA_PROBED_SCHEMA
        )
        assert blueprint == BDA_PROBED_BLUEPRINT
        assert_bda_accepted_shape(blueprint)

    def test_the_shape_check_allows_a_primitive_array_in_a_definition(self):
        """Catalog blueprints (e.g. receipt) carry these, so BDA accepts them."""
        assert_bda_accepted_shape(
            {
                "definitions": {
                    "D": {
                        "properties": {
                            "a": {"type": "array", "items": {"type": "string"}}
                        }
                    }
                },
                "properties": {"x": {"$ref": "#/definitions/D"}},
            }
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"properties": {"x": {"type": "object", "properties": {"a": {}}}}},
            {"properties": {"x": {"type": "array", "items": {"properties": {}}}}},
            {
                "definitions": {
                    "D": {"properties": {"m": {"$ref": "#/definitions/D"}}}
                },
                "properties": {"x": {"$ref": "#/definitions/D"}},
            },
            {
                "definitions": {"D": {"description": "d", "properties": {"a": {}}}},
                "properties": {"x": {"$ref": "#/definitions/D"}},
            },
            {"properties": {"x": {"$ref": "#/definitions/Missing"}}},
            {
                "definitions": {"D": {"properties": {"m": {"properties": {}}}}},
                "properties": {"x": {"$ref": "#/definitions/D"}},
            },
            {
                "definitions": {"D": {"properties": {}}},
                "properties": {"x": {"$ref": "#/definitions/D"}},
            },
        ],
    )
    def test_the_shape_check_rejects_each_shape_bda_rejects(self, bad):
        with pytest.raises(AssertionError):
            assert_bda_accepted_shape(bad)
