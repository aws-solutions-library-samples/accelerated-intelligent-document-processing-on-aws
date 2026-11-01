# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Five BDA blueprint conversion defects (issue #1742).

Shape facts measured against the BDA control plane (``CreateBlueprint``,
DOCUMENT, us-west-2), not assumed:

- rejected: a ``$ref`` member inside a definition, with or without an
  ``instruction``; an array of ``$ref`` items inside a definition.
- accepted: a primitive array (``items: {"type": "string"}`` or ``number``) inside
  a definition, with or without ``inferenceType``, and with ``"-"`` as its
  instruction. AWS's own catalog blueprints (receipt, schedule C, medical
  insurance card) carry the same shape.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from tests.unit.bda.test_inline_object_in_defs_schema import assert_bda_accepted_shape

LEAF_DEFAULT = "Extract this field from the document"


def _leaf(type_: str, instruction: str = LEAF_DEFAULT) -> dict:
    return {"type": type_, "inferenceType": "explicit", "instruction": instruction}


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
