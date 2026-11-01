# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Five BDA blueprint conversion defects (issue #1742).

Shape facts measured against the BDA control plane (``CreateBlueprint``,
DOCUMENT, us-west-2), not assumed:

- rejected: a ``$ref`` member inside a definition, with or without an
  ``instruction``; an array of ``$ref`` items inside a definition.
- accepted: a primitive array (``items: {"type": "string"}`` or ``number``) inside
  a definition referenced by an object property, with or without
  ``inferenceType``, and with ``"-"`` as its instruction. AWS's own catalog
  blueprints (receipt, schedule C, medical insurance card) carry the same shape,
  and only in that position.
- rejected: the same primitive array inside a definition that an array's
  ``items`` references (an array nested in an array).
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from tests.unit.bda.test_inline_object_in_defs_schema import assert_bda_accepted_shape

LEAF_DEFAULT = "Extract this field from the document"


def _leaf(type_: str, instruction: str = LEAF_DEFAULT) -> dict:
    return {"type": type_, "inferenceType": "explicit", "instruction": instruction}


def assert_no_array_in_an_item_definition(blueprint: dict) -> None:
    """Probed: an array inside a definition that is used as array items is
    rejected by CreateBlueprint."""
    for prop in blueprint["properties"].values():
        ref = (
            (prop.get("items") or {}).get("$ref")
            if prop.get("type") == "array"
            else None
        )
        if ref:
            members = blueprint["definitions"][ref.split("/")[-1]]["properties"]
            assert not [n for n, m in members.items() if m.get("type") == "array"], ref


@pytest.fixture
def service():
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


@pytest.mark.unit
class TestRefInsideADefinition:
    """Item 1: a $ref member inside a definition is dropped, not kept."""

    SCHEMA = {
        "$id": "T",
        "$defs": {
            "Phone": {"properties": {"Number": {"type": "string"}}},
            "Contact": {
                "properties": {
                    "Name": {"type": "string"},
                    "Phone": {"$ref": "#/$defs/Phone", "description": "phone"},
                }
            },
        },
        "properties": {"Contact": {"$ref": "#/$defs/Contact"}},
    }

    def test_the_ref_member_is_dropped_and_recorded(self, service):
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(self.SCHEMA)
        assert blueprint["definitions"]["Contact"]["properties"] == {
            "Name": _leaf("string")
        }
        assert [(d["property"], d["type"]) for d in recording.drops] == [
            ("Phone", "nested_object")
        ]
        assert_bda_accepted_shape(blueprint)

    def test_a_hoisted_inline_object_with_a_ref_member_drops_it_too(self, service):
        schema = {
            "$id": "T",
            "$defs": {"Phone": {"properties": {"Number": {"type": "string"}}}},
            "properties": {
                "Contact": {
                    "type": "object",
                    "properties": {
                        "Name": {"type": "string"},
                        "Phone": {"$ref": "#/$defs/Phone"},
                    },
                }
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["definitions"]["Contact"]["properties"] == {
            "Name": _leaf("string")
        }
        assert_bda_accepted_shape(blueprint)


@pytest.mark.unit
class TestPrimitiveArrayInsideADefinition:
    """Item 2: a primitive array inside a definition is kept, on both paths."""

    MEMBERS = {
        "Name": {"type": "string"},
        "Tags": {"type": "array", "description": "tags", "items": {"type": "string"}},
        "Amounts": {"type": "array", "items": {"type": "number"}},
        "Rows": {
            "type": "array",
            "items": {"type": "object", "properties": {"A": {"type": "string"}}},
        },
        "Refs": {"type": "array", "items": {"$ref": "#/$defs/Other"}},
        "Grid": {"type": "array", "items": {"type": "array", "items": {}}},
        "Bare": {"type": "array"},
    }
    KEPT = {
        "Name": _leaf("string"),
        "Tags": {"type": "array", "items": {"type": "string"}, "instruction": "tags"},
        "Amounts": {"type": "array", "items": {"type": "number"}, "instruction": "-"},
    }

    def test_defs_path(self, service):
        schema = {
            "$id": "T",
            "$defs": {
                "C": {"properties": dict(self.MEMBERS)},
                "Other": {"properties": {"X": {"type": "string"}}},
            },
            "properties": {"C": {"$ref": "#/$defs/C"}},
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["definitions"]["C"]["properties"] == self.KEPT
        assert sorted(d["property"] for d in recording.drops) == [
            "Bare",
            "Grid",
            "Refs",
            "Rows",
        ]
        assert_bda_accepted_shape(blueprint)
        assert_no_array_in_an_item_definition(blueprint)

    def test_flat_path_object_keeps_the_array_instead_of_dropping_the_object(
        self, service
    ):
        primitive_only = {k: self.MEMBERS[k] for k in ("Name", "Tags", "Amounts")}
        schema = {
            "$id": "T",
            "properties": {
                "C": {"type": "object", "properties": primitive_only},
                "Rows": {
                    "type": "array",
                    "items": {"type": "object", "properties": primitive_only},
                },
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["C"] == {"$ref": "#/definitions/C"}
        assert blueprint["definitions"]["C"]["properties"] == self.KEPT
        # Used as array items, so its arrays would nest in an array: dropped.
        assert blueprint["definitions"]["RowsItem"]["properties"] == {
            "Name": _leaf("string")
        }
        assert sorted(d["property"] for d in recording.drops) == ["Amounts", "Tags"]
        assert_bda_accepted_shape(blueprint)
        assert_no_array_in_an_item_definition(blueprint)

    def test_defs_path_definition_used_as_array_items_loses_its_arrays(self, service):
        primitive_only = {k: self.MEMBERS[k] for k in ("Name", "Tags", "Amounts")}
        schema = {
            "$id": "T",
            "$defs": {
                "Shared": {"properties": dict(primitive_only)},
                "Solo": {"properties": dict(primitive_only)},
            },
            "properties": {
                "One": {"$ref": "#/$defs/Shared"},
                "Many": {"type": "array", "items": {"$ref": "#/$defs/Shared"}},
                "Only": {"$ref": "#/$defs/Solo"},
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["definitions"]["Shared"]["properties"] == {
            "Name": _leaf("string")
        }
        assert blueprint["definitions"]["Solo"]["properties"] == self.KEPT
        assert sorted(d["property"] for d in recording.drops) == ["Amounts", "Tags"]
        assert all("'Shared'" in d["message"] for d in recording.drops)
        assert_bda_accepted_shape(blueprint)
        assert_no_array_in_an_item_definition(blueprint)

    def test_flat_path_still_drops_an_object_holding_an_array_of_objects(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "C": {
                    "type": "object",
                    "properties": {"Rows": self.MEMBERS["Rows"]},
                },
                "Leaf": {"type": "string"},
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert list(blueprint["properties"]) == ["Leaf"]
        assert [d["property"] for d in recording.drops] == ["C"]

    @pytest.mark.parametrize("with_defs", [True, False])
    def test_items_left_with_no_members_drop_the_array(self, service, with_defs):
        """Not an empty definition: the array itself is dropped and recorded."""
        item = {"type": "object", "properties": {"Tags": self.MEMBERS["Tags"]}}
        properties = {
            "Rows": {"type": "array", "items": item},
            "Leaf": {"type": "string"},
        }
        schema = {"$id": "T", "properties": properties}
        if with_defs:
            schema["$defs"] = {"D": {"properties": {"X": {"type": "string"}}}}
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert list(blueprint["properties"]) == ["Leaf"]
        assert "RowsItem" not in blueprint.get("definitions", {})
        if not with_defs:
            # The only definition went, so no empty "definitions" is sent.
            assert "definitions" not in blueprint
        assert [(d["property"], d["type"]) for d in recording.drops] == [
            ("Tags", "nested_array"),
            ("Rows", "nested_array"),
        ]

    def test_a_shared_definition_left_empty_drops_every_reference(self, service):
        schema = {
            "$id": "T",
            "$defs": {
                "Only": {"properties": {"Tags": self.MEMBERS["Tags"]}},
                "Keep": {"properties": {"X": {"type": "string"}}},
            },
            "properties": {
                "One": {"$ref": "#/$defs/Only"},
                "Many": {"type": "array", "items": {"$ref": "#/$defs/Only"}},
                "K": {"$ref": "#/$defs/Keep"},
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert list(blueprint["properties"]) == ["K"]
        assert list(blueprint["definitions"]) == ["Keep"]
        assert sorted((d["property"], d["type"]) for d in recording.drops) == [
            ("Many", "nested_array"),
            ("One", "nested_object"),
            ("Tags", "nested_array"),
        ]
        assert_bda_accepted_shape(blueprint)

    def test_every_item_definition_is_checked_and_only_arrays_go(self, service):
        members = {
            "Flag": {"type": "boolean"},
            "Tags": self.MEMBERS["Tags"],
            "Name": {"type": "string"},
        }
        schema = {
            "$id": "T",
            "$defs": {
                "A": {"properties": dict(members)},
                "B": {"properties": dict(members)},
            },
            "properties": {
                "As": {"type": "array", "items": {"$ref": "#/$defs/A"}},
                "Bs": {"type": "array", "items": {"$ref": "#/$defs/B"}},
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        for name in ("A", "B"):
            assert list(blueprint["definitions"][name]["properties"]) == [
                "Flag",
                "Name",
            ]
        assert [d["property"] for d in recording.drops] == ["Tags", "Tags"]
        assert_no_array_in_an_item_definition(blueprint)

    @pytest.mark.parametrize(
        "schema,primitive",
        [
            ({"type": "array", "items": {"type": "string"}}, True),
            ({"type": "array", "items": {}}, True),
            # Only string and number items were probed; others stay dropped.
            ({"type": "array", "items": {"type": "boolean"}}, False),
            ({"type": "array", "items": {"type": "integer"}}, False),
            ({"type": "array", "items": {"type": ["string", "null"]}}, False),
            ({"type": "array"}, False),
            ({"type": "array", "items": {"type": "object"}}, False),
            ({"type": "array", "items": {"properties": {}}}, False),
            ({"type": "array", "items": {"$ref": "#/$defs/X"}}, False),
            ({"type": "array", "items": {"type": "array"}}, False),
            ({"type": "array", "items": {"items": {}}}, False),
            ({"type": "array", "items": "string"}, False),
            ({"type": "string"}, False),
            ("array", False),
        ],
    )
    def test_is_primitive_array(self, schema, primitive):
        assert BdaBlueprintService._is_primitive_array(schema) is primitive


@pytest.mark.unit
class TestSanitizeUntypedObjects:
    """Item 3: an untyped inline object's member names are sanitized too."""

    def test_untyped_object_members_are_sanitized(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Owner": {"properties": {"Name & Title": {"type": "string"}}},
                "Typed": {
                    "type": "object",
                    "properties": {"A/B": {"type": "string"}},
                },
            },
        }
        sanitized, mapping = service._sanitize_property_names(schema)
        assert list(sanitized["properties"]["Owner"]["properties"]) == ["NameTitle"]
        assert list(sanitized["properties"]["Typed"]["properties"]) == ["AB"]
        assert mapping == {"Name & Title": "NameTitle", "A/B": "AB"}

    def test_untyped_array_item_members_are_sanitized(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Rows": {"items": {"properties": {"Unit $": {"type": "number"}}}}
            },
        }
        sanitized, mapping = service._sanitize_property_names(schema)
        assert list(sanitized["properties"]["Rows"]["items"]["properties"]) == ["Unit"]
        assert mapping == {"Unit $": "Unit"}

    def test_a_declared_scalar_with_properties_is_not_descended(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Code": {"type": "string", "properties": {"x&y": {"type": "string"}}}
            },
        }
        sanitized, mapping = service._sanitize_property_names(schema)
        assert list(sanitized["properties"]["Code"]["properties"]) == ["x&y"]
        assert mapping == {}

    @pytest.mark.parametrize("with_defs", [True, False])
    def test_every_blueprint_name_is_valid_after_a_sync_style_conversion(
        self, service, with_defs
    ):
        import re

        schema = {
            "$id": "T",
            "properties": {
                "Owner": {"properties": {"Name & Title": {"type": "string"}}},
                "Rows": {"items": {"properties": {"Unit $": {"type": "number"}}}},
            },
        }
        if with_defs:
            schema["$defs"] = {"D": {"properties": {"Z": {"type": "string"}}}}
        sanitized, _ = service._sanitize_property_names(schema)
        blueprint = service._transform_json_schema_to_bedrock_blueprint(sanitized)
        names = list(blueprint["properties"]) + [
            n
            for d in blueprint.get("definitions", {}).values()
            for n in d["properties"]
        ]
        assert "NameTitle" in names and "Unit" in names
        assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", n) for n in names), names


@pytest.mark.unit
class TestUntypedObjectsWithoutDefs:
    """Item 4: an untyped object is converted as an object, never as a string."""

    def test_untyped_object_items_are_hoisted_not_turned_into_strings(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Rows": {
                    "type": "array",
                    "description": "rows",
                    "items": {"properties": {"A": {"type": "number"}}},
                }
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["Rows"] == {
            "type": "array",
            "items": {"$ref": "#/definitions/RowsItem"},
            "instruction": "rows",
        }
        assert blueprint["definitions"]["RowsItem"] == {
            "type": "object",
            "properties": {"A": _leaf("number")},
        }
        assert_bda_accepted_shape(blueprint)

    def test_an_untyped_array_of_untyped_objects_is_hoisted(self, service):
        schema = {
            "$id": "T",
            "properties": {"Rows": {"items": {"properties": {"A": {}}}}},
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["Rows"]["items"] == {
            "$ref": "#/definitions/RowsItem"
        }
        assert blueprint["definitions"]["RowsItem"]["properties"] == {
            "A": _leaf("string")
        }
        assert_bda_accepted_shape(blueprint)

    def test_an_untyped_object_property_is_hoisted(self, service):
        schema = {
            "$id": "T",
            "properties": {"Owner": {"properties": {"Name": {"type": "string"}}}},
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["Owner"] == {"$ref": "#/definitions/Owner"}
        assert blueprint["definitions"]["Owner"]["properties"] == {
            "Name": _leaf("string")
        }
        assert_bda_accepted_shape(blueprint)

    def test_untyped_items_that_nest_further_are_dropped_and_recorded(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Rows": {
                    "type": "array",
                    "items": {"properties": {"Inner": {"properties": {"X": {}}}}},
                },
                "Leaf": {"type": "string"},
            },
        }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert list(blueprint["properties"]) == ["Leaf"]
        assert [(d["property"], d["type"]) for d in recording.drops] == [
            ("Rows", "nested_array")
        ]

    @pytest.mark.parametrize("with_defs", [True, False])
    def test_an_untyped_object_inside_a_definition_is_dropped_not_a_string(
        self, service, with_defs
    ):
        members = {
            "Name": {"type": "string"},
            "Inner": {"properties": {"X": {"type": "string"}}},
            "Tags": {"items": {"type": "string"}},
            "Rows": {"items": {"properties": {"Y": {"type": "string"}}}},
        }
        if with_defs:
            schema = {
                "$id": "T",
                "$defs": {"C": {"properties": members}},
                "properties": {"C": {"$ref": "#/$defs/C"}},
            }
        else:
            schema = {
                "$id": "T",
                "properties": {"C": {"properties": members}, "L": {"type": "string"}},
            }
        with service._recording_drops_for("T") as recording:
            blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        if with_defs:
            assert blueprint["definitions"]["C"]["properties"] == {
                "Name": _leaf("string"),
                "Tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "instruction": "-",
                },
            }
            assert sorted(d["property"] for d in recording.drops) == ["Inner", "Rows"]
        else:
            # Without $defs an object holding an object is dropped whole, as it
            # is when both are typed.
            assert list(blueprint["properties"]) == ["L"]
            assert [d["property"] for d in recording.drops] == ["C"]
        assert_bda_accepted_shape(blueprint)

    def test_typed_schemas_convert_exactly_as_before(self, service):
        """Inference applies only where "type" is missing."""
        schema = {
            "$id": "T",
            "properties": {
                "Code": {"type": "string", "properties": {"Q": {"type": "string"}}},
                "Rows": {
                    "type": "array",
                    "items": {"type": "string", "properties": {"Q": {}}},
                },
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"] == {
            "Code": _leaf("string"),
            "Rows": {"type": "array", "items": {"type": "string"}, "instruction": "-"},
        }
        assert "definitions" not in blueprint
