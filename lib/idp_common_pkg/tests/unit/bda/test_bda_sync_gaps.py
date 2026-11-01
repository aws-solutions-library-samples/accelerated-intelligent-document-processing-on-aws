# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""BDA blueprint conversion gaps that blocked shipped presets (issue #1755).

Measured against the BDA control plane (``CreateBlueprint``, DOCUMENT,
us-west-2), with the schema serialised by ``json.dumps``:

- rejected: ``"type": "integer"`` on a leaf, on an array's ``items``, and on a
  member of a definition. ``number`` in each of those places was accepted, as
  was a ``number`` leaf whose instruction ends in "(a whole number)".
- rejected: a blueprint with ``"properties": {}``, with no ``properties`` key,
  and with only ``definitions``. One leaf property was accepted.

Every class of every ``config_library/unified`` preset was converted and sent to
CreateBlueprint after these fixes; each one the converter sends was accepted, and
the three it does not send are the three ``UNSENT_PRESET_CLASSES`` below, each
rejected when sent before the fix.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService, classes_not_sent
from tests.unit.bda.test_blueprint_text_characters import (
    PRESET_CLASSES,
    PROBED_CHARS,
    _texts,
)
from tests.unit.bda.test_inline_object_in_defs_schema import assert_bda_accepted_shape

LEAF_DEFAULT = "Extract this field from the document"

# The preset classes with no field a blueprint can hold. Each was sent to
# CreateBlueprint before issue #1755 and rejected.
UNSENT_PRESET_CLASSES = {
    ("ds11-passport-application", "PassportApplicationInstructions"),
    ("ocr-benchmark", "BANK_CHECK"),
    ("ocr-benchmark", "DELIVERY_NOTE"),
}

# Leaf types the catalog blueprints use and CreateBlueprint was measured to take.
BDA_LEAF_TYPES = {"string", "number", "boolean"}


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


def _sync_one(service, cls, existing=()):
    """Run one class through the sync's per-class step with a working creator."""
    creator = service.blueprint_creator
    creator.create_blueprint.return_value = {
        "status": "success",
        "blueprint": {"blueprintArn": "arn:bp/new", "blueprintName": "new"},
    }
    creator.create_blueprint_version_without_project_update.return_value = {
        "blueprint": {"blueprintVersion": "1"}
    }
    return service._process_single_class(json.loads(json.dumps(cls)), list(existing))


def _sent_schema(service):
    (call,) = service.blueprint_creator.create_blueprint.call_args_list
    return json.loads(call.kwargs["schema"])


def assert_leaf_types_bda_takes(blueprint: dict) -> None:
    for node in _schema_nodes(blueprint):
        type_ = node.get("type")
        if type_ not in ("object", "array") and "$ref" not in node:
            assert type_ in BDA_LEAF_TYPES, node


def _schema_nodes(blueprint: dict):
    stack = list((blueprint.get("properties") or {}).values())
    for definition in (blueprint.get("definitions") or {}).values():
        stack.extend((definition.get("properties") or {}).values())
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node.get("items"), dict):
            stack.append(node["items"])


@pytest.mark.unit
class TestAClassWithNothingToSendIsSkipped:
    def test_a_class_with_no_properties(self, service):
        result = _sync_one(service, {"$id": "Notice", "description": "Boilerplate"})

        service.blueprint_creator.create_blueprint.assert_not_called()
        assert result["status"] == "success"
        assert result["class"] == "Notice"
        assert "_internal" not in result
        (warning,) = result["warnings"]
        assert warning["type"] == "empty_blueprint"
        assert warning["class"] == "Notice"
        assert "it has no properties" in warning["message"]

    def test_a_class_whose_every_property_is_dropped(self, service):
        nested = {
            "type": "object",
            "properties": {
                "Inner": {"type": "object", "properties": {"A": {"type": "string"}}},
                "Rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"B": {"type": "string"}},
                    },
                },
            },
        }
        result = _sync_one(service, {"$id": "Deep", "properties": {"Outer": nested}})

        service.blueprint_creator.create_blueprint.assert_not_called()
        kinds = [w["type"] for w in result["warnings"]]
        assert kinds[-1] == "empty_blueprint"
        assert "nested_object" in kinds[:-1]
        assert {w["class"] for w in result["warnings"]} == {"Deep"}
        assert "none of its properties" in result["warnings"][-1]["message"]

    def test_an_existing_blueprint_is_not_updated_to_empty(self, service):
        existing = {
            "blueprintName": f"{service.blueprint_name_prefix}-Notice-abcd1234",
            "blueprintArn": "arn:bp/old",
            "schema": json.dumps({"class": "Notice", "properties": {"A": {}}}),
        }
        result = _sync_one(service, {"$id": "Notice", "properties": {}}, [existing])

        service.blueprint_creator.update_blueprint.assert_not_called()
        service.blueprint_creator.create_blueprint.assert_not_called()
        assert result["warnings"][-1]["type"] == "empty_blueprint"

    def test_a_class_with_a_field_is_sent_and_its_drops_reported_once(self, service):
        cls = {
            "$id": "Form",
            "properties": {
                "Name": {"type": "string"},
                "Outer": {
                    "type": "object",
                    "properties": {
                        "Inner": {
                            "type": "object",
                            "properties": {"A": {"type": "string"}},
                        }
                    },
                },
            },
        }
        result = _sync_one(service, cls)

        assert result["status"] == "success"
        assert result["_internal"]["blueprint_arn"] == "arn:bp/new"
        assert [(w["property"], w["type"]) for w in result["warnings"]] == [
            ("Outer", "nested_object")
        ]
        assert list(_sent_schema(service)["properties"]) == ["Name"]

    def test_the_instance_record_holds_each_drop_once(self, service):
        sent = {
            "$id": "Form",
            "properties": {
                "Name": {"type": "string"},
                "Outer": {
                    "type": "object",
                    "properties": {"In": {"type": "object", "properties": {}}},
                },
            },
        }
        unsent = {
            "$id": "Notice",
            "properties": {
                "Outer": {
                    "type": "object",
                    "properties": {"In": {"type": "object", "properties": {}}},
                }
            },
        }
        _sync_one(service, sent)
        _sync_one(service, unsent)

        assert [(w["class"], w["type"]) for w in service._skipped_properties] == [
            ("Form", "nested_object"),
            ("Notice", "nested_object"),
            ("Notice", "empty_blueprint"),
        ]

    @pytest.mark.parametrize("mode,deleted", [("replace", True), ("merge", False)])
    def test_an_earlier_blueprint_for_it_is_removed_only_in_replace_mode(
        self, service, mode, deleted
    ):
        old = {
            "blueprintName": f"{service.blueprint_name_prefix}-Notice-abcd1234",
            "blueprintArn": "arn:bp/old",
            "blueprintVersion": "1",
            "schema": json.dumps({"class": "Notice", "properties": {"A": {}}}),
        }
        service._retrieve_all_blueprints = MagicMock(return_value=[old])
        service.config_manager.get_configuration.return_value = MagicMock(
            classes=[{"$id": "Notice", "properties": {}}]
        )
        creator = service.blueprint_creator
        creator.list_blueprints.return_value = {
            "blueprints": [{"blueprintArn": "arn:bp/old"}]
        }
        creator.delete_blueprint.return_value = True

        status = service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="idp_to_bda", sync_mode=mode
        )

        assert status[0]["warnings"][-1]["type"] == "empty_blueprint"
        creator.create_blueprint.assert_not_called()
        creator.update_blueprint.assert_not_called()
        if deleted:
            creator.delete_blueprint.assert_called_once_with("arn:bp/old", "1")
        else:
            creator.delete_blueprint.assert_not_called()

    def test_the_parallel_step_reports_the_warning_and_associates_nothing(
        self, service
    ):
        status, updated, _ = service._process_classes_parallel(
            [{"$id": "Notice", "properties": {}}], []
        )

        assert updated == []
        service.blueprint_creator.bulk_update_data_automation_project.assert_not_called()
        (entry,) = status
        assert entry["status"] == "success"
        assert entry["warnings"][0]["type"] == "empty_blueprint"


@pytest.mark.unit
def test_classes_not_sent_reads_only_successes_with_the_empty_blueprint_warning():
    empty = {"type": "empty_blueprint"}
    statuses = [
        {"class": "A", "status": "success", "warnings": [empty]},
        {"class": "B", "status": "success", "warnings": [{"type": "nested_object"}]},
        {"class": "C", "status": "failed", "warnings": [empty]},
        {"class": "D", "status": "success"},
        {"class": "E", "status": "success", "warnings": [{"type": "x"}, empty]},
        "not a dict",
    ]
    assert classes_not_sent(statuses) == ["A", "E"]
    assert classes_not_sent(None) == []


@pytest.mark.unit
class TestEveryPresetClass:
    """Every preset class converts to a blueprint of a probed-accepted shape, or
    is not sent and says so."""

    @pytest.mark.parametrize(
        "preset,cls",
        PRESET_CLASSES,
        ids=[f"{p}:{c.get('$id')}" for p, c in PRESET_CLASSES],
    )
    def test_is_sent_in_an_accepted_shape_or_skipped_with_a_warning(
        self, service, preset, cls
    ):
        result = _sync_one(service, cls)
        assert result["status"] == "success", result.get("error")
        kinds = [w["type"] for w in result.get("warnings", [])]
        if (preset, cls["$id"]) in UNSENT_PRESET_CLASSES:
            service.blueprint_creator.create_blueprint.assert_not_called()
            assert kinds[-1] == "empty_blueprint"
            return
        assert "empty_blueprint" not in kinds
        blueprint = _sent_schema(service)
        assert_bda_accepted_shape(blueprint)
        assert_leaf_types_bda_takes(blueprint)
        for path, text in _texts(blueprint):
            assert not set(text) - PROBED_CHARS, f"{path}: {text!r}"
            assert text.strip(), path

    def test_the_unsent_classes_are_preset_classes(self):
        present = {(p, c["$id"]) for p, c in PRESET_CLASSES}
        assert UNSENT_PRESET_CLASSES <= present
