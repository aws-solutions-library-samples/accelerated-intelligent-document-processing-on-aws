# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""BDA blueprint conversion gaps that blocked shipped presets (issue #1755).

Measured against the BDA control plane (``CreateBlueprint``, DOCUMENT,
us-west-2), with the schema serialised by ``json.dumps``:

- rejected: ``"type": "integer"`` on a leaf, on an array's ``items``, and on a
  member of a definition. ``number`` in each of those places was accepted, as
  was a ``number`` leaf whose instruction ends in "(a whole number)".
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from tests.unit.bda.test_inline_object_in_defs_schema import assert_bda_accepted_shape

LEAF_DEFAULT = "Extract this field from the document"


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


def _types(node):
    """Yield every value of a ``type`` key at a schema position."""
    if isinstance(node, dict):
        if isinstance(node.get("type"), str):
            yield node["type"]
        for key in ("properties", "definitions"):
            for child in (node.get(key) or {}).values():
                yield from _types(child)
        if isinstance(node.get("items"), dict):
            yield from _types(node["items"])


@pytest.mark.unit
class TestIntegerIsSentAsNumber:
    def test_flat_leaf(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {"$id": "T", "properties": {"Count": {"type": "integer"}}}
        )
        assert blueprint["properties"]["Count"] == {
            "type": "number",
            "inferenceType": "explicit",
            "instruction": f"{LEAF_DEFAULT} (a whole number)",
        }

    def test_leaf_keeps_its_own_instruction(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "properties": {
                    "Count": {"type": "integer", "description": "Number of pages"}
                },
            }
        )
        assert blueprint["properties"]["Count"]["instruction"] == (
            "Number of pages (a whole number)"
        )

    def test_the_note_is_not_added_twice(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "properties": {
                    "Count": {
                        "type": "integer",
                        "description": "Pages (a whole number)",
                    }
                },
            }
        )
        assert blueprint["properties"]["Count"]["instruction"] == (
            "Pages (a whole number)"
        )

    def test_definition_member(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "$defs": {
                    "Lease": {
                        "type": "object",
                        "properties": {
                            "Years": {"type": "integer"},
                            "Name": {"type": "string"},
                        },
                    }
                },
                "properties": {"Lease": {"$ref": "#/$defs/Lease"}},
            }
        )
        members = blueprint["definitions"]["Lease"]["properties"]
        assert members["Years"]["type"] == "number"
        assert members["Years"]["instruction"].endswith("(a whole number)")
        assert members["Name"] == {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": LEAF_DEFAULT,
        }
        assert_bda_accepted_shape(blueprint)

    def test_array_items(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "properties": {
                    "Pages": {"type": "array", "items": {"type": "integer"}},
                    "Totals": {
                        "type": "array",
                        "description": "Line totals",
                        "items": {"type": "integer"},
                    },
                },
            }
        )
        assert blueprint["properties"]["Pages"] == {
            "type": "array",
            "items": {"type": "number"},
            "instruction": "(whole numbers)",
        }
        assert blueprint["properties"]["Totals"]["instruction"] == (
            "Line totals (whole numbers)"
        )

    def test_integer_array_inside_a_definition_is_kept_as_numbers(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "$defs": {
                    "Box": {
                        "type": "object",
                        "properties": {
                            "Sizes": {"type": "array", "items": {"type": "integer"}},
                            "Label": {"type": "string"},
                        },
                    }
                },
                "properties": {"Box": {"$ref": "#/$defs/Box"}},
            }
        )
        members = blueprint["definitions"]["Box"]["properties"]
        assert members["Sizes"]["items"] == {"type": "number"}
        assert members["Sizes"]["instruction"] == "(whole numbers)"
        assert_bda_accepted_shape(blueprint)

    def test_array_item_definition_member(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "properties": {
                    "Rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"Qty": {"type": "integer"}},
                        },
                    }
                },
            }
        )
        member = blueprint["definitions"]["RowsItem"]["properties"]["Qty"]
        assert member["type"] == "number"
        assert_bda_accepted_shape(blueprint)

    def test_a_property_named_type_is_not_rewritten(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "properties": {
                    "type": {"type": "string", "description": "integer"},
                    "integer": {"type": "boolean"},
                },
            }
        )
        assert blueprint["properties"]["type"]["type"] == "string"
        assert blueprint["properties"]["type"]["instruction"] == "integer"
        assert blueprint["properties"]["integer"]["type"] == "boolean"

    def test_other_types_are_unchanged(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "A": {"type": "number"},
                "B": {"type": "boolean"},
                "C": {"type": "string"},
            },
        }
        before = json.dumps(schema)
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert json.dumps(schema) == before
        assert [p["type"] for p in blueprint["properties"].values()] == [
            "number",
            "boolean",
            "string",
        ]
        assert all(
            p["instruction"] == LEAF_DEFAULT for p in blueprint["properties"].values()
        )

    def test_no_integer_survives_anywhere(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            {
                "$id": "T",
                "$defs": {
                    "D": {
                        "type": "object",
                        "properties": {
                            "N": {"type": "integer"},
                            "L": {"type": "array", "items": {"type": "integer"}},
                        },
                    }
                },
                "properties": {
                    "D": {"$ref": "#/$defs/D"},
                    "Ds": {"type": "array", "items": {"$ref": "#/$defs/D"}},
                    "I": {"type": "integer"},
                },
            }
        )
        types = list(_types(blueprint))
        assert "number" in types
        assert "integer" not in types
