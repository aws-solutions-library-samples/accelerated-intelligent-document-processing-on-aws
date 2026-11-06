# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The blueprint ``class`` value carries the class id reversibly (issue #1755).

Measured against the BDA control plane (``CreateBlueprint``, DOCUMENT,
us-west-2), with the schema serialised by ``json.dumps``:

- every character of ``PROBED_TEXT`` was accepted in ``class`` (in four values of
  50 characters), and each character of ``PROBED_REJECTS`` was rejected on its
  own there; the four ``PROBED_SINGLES``, tab, LF and CR were accepted. So the
  class takes the same characters as a blueprint's text.
- a ``class`` of 60 characters was accepted and one of 61 rejected; 60 two-byte
  characters were accepted. ``""`` and ``"   "`` were rejected.
- ``GetBlueprint`` returned the ``class`` exactly as it was sent, including
  ``"Foo_x2014_Bar Café’ (x)"``.
- no catalog (``SERVICE``) blueprint's ``class`` contains ``_x``.
"""

import json
import random
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda import blueprint_class
from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from idp_common.bda.blueprint_class import (
    BDA_CLASS_MAX_LENGTH,
    BDA_TEXT_CHARS,
    from_blueprint_class,
    to_blueprint_class,
)
from tests.unit.bda.test_blueprint_text_characters import (
    PRESET_CLASSES,
    PROBED_CHARS,
    PROBED_REJECTS,
)

# A sample of the catalog's class values, verbatim.
CATALOG_CLASS_SAMPLE = (
    "broadcast_video_analysis_single_choice",
    "Conversational Analytics",
    "advertisement image",
    "Canada Driving-licenses",
    "Form-1040-Schedule-C",
    "W2",
)

# Class ids chosen to defeat a naive escape: text already shaped like an
# escape, an underscore before an escape the encoder emits, runs of underscores,
# a code point above U+10FFFF, and characters outside the basic plane.
ADVERSARIAL_IDS = (
    "_x0041_",
    "A_x005F_",
    "_x1234—",
    "__x0041__",
    "_x_x2014_",
    "_",
    "x_",
    "_x110000_",
    "_x10FFFF_",
    "_x00410_",
    "_x041_",
    "_xabcd_",
    "—",
    "{}",
    "Invoice — {EU}",
    "\U0001f600_x0041_\U0001f600",
    "—_x2014_—",
    # Escape-shaped text above U+10FFFF directly before a real escape.
    "_x110000{",
    "_x110000_x0041_",
    "_xFFFFFF_x0041_",
    "_x0FFFFF_x0041_",
    # Characters whose escape needs five and six digits.
    "\U00010000\U000fffff\U00100000\U0010fffd",
)


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


def _random_ids(n: int, seed: int):
    alphabet = "_xX0123456789ABCDEFabf -" + "".join(PROBED_REJECTS) + "é’"
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))


@pytest.mark.unit
class TestTheCodec:
    def test_the_measured_facts(self):
        assert BDA_TEXT_CHARS == PROBED_CHARS
        assert BDA_CLASS_MAX_LENGTH == 60

    @pytest.mark.parametrize("class_id", ADVERSARIAL_IDS)
    def test_adversarial_ids_round_trip(self, class_id):
        encoded = to_blueprint_class(class_id)
        assert from_blueprint_class(encoded) == class_id
        assert set(encoded) <= BDA_TEXT_CHARS

    def test_random_ids_built_from_escape_fragments_round_trip(self):
        """Characters drawn one at a time almost never form a six-digit escape, so
        this draws whole fragments of one, in and out of range."""
        fragments = (
            "_", "x", "_x", "110000", "10FFFF", "FFFFFF", "0FFFFF", "0041",
            "00410", "_x0041_", "_x110000_", "—", "{", "A", "0", " ",
        )  # fmt: skip
        rng = random.Random(1755)
        for _ in range(20000):
            class_id = "".join(rng.choice(fragments) for _ in range(rng.randint(0, 6)))
            encoded = to_blueprint_class(class_id)
            assert from_blueprint_class(encoded) == class_id, class_id
            assert set(encoded) <= BDA_TEXT_CHARS, class_id

    def test_random_ids_round_trip(self):
        for class_id in _random_ids(5000, seed=1755):
            encoded = to_blueprint_class(class_id)
            assert from_blueprint_class(encoded) == class_id, class_id
            assert set(encoded) <= BDA_TEXT_CHARS, class_id

    @pytest.mark.parametrize("ch", PROBED_REJECTS)
    def test_each_rejected_character_is_escaped(self, ch):
        assert to_blueprint_class(f"A{ch}B") == f"A_x{ord(ch):04X}_B"

    def test_ids_bda_already_takes_are_unchanged(self):
        ids = [cls["$id"] for _, cls in PRESET_CLASSES] + list(CATALOG_CLASS_SAMPLE)
        ids += ["Bank_Statement", "Bank Statement", "Café’s", "x_x", "_x00G1_"]
        # Lower-case hex is not an escape, so it needs no protecting.
        ids += ["_xabcd_", "Form_x1a2b_"]
        for class_id in ids:
            assert to_blueprint_class(class_id) == class_id
            assert from_blueprint_class(class_id) == class_id

    def test_an_underscore_is_escaped_only_where_it_would_decode(self):
        assert to_blueprint_class("_x0041_") == "_x005F_x0041_"
        # The escape after it is the encoder's own, so the "_" in front of
        # "x1234" would otherwise decode with it.
        assert to_blueprint_class("_x1234—") == "_x005F_x1234_x2014_"
        # Not a code point, so not decoded, so nothing to protect.
        assert to_blueprint_class("_x110000_") == "_x110000_"
        assert from_blueprint_class("_x110000_") == "_x110000_"

    def test_has_no_dependencies_beyond_the_standard_library(self):
        source = open(blueprint_class.__file__, encoding="utf-8").read()
        imports = {
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        }
        assert imports == {"re"}


def _schema(class_id, **properties):
    return {
        "$id": class_id,
        "x-aws-idp-document-type": class_id,
        "properties": properties or {"Name": {"type": "string"}},
    }


def _ok_creator(service):
    creator = service.blueprint_creator
    creator.create_blueprint.return_value = {
        "status": "success",
        "blueprint": {"blueprintArn": "arn:bp/new", "blueprintName": "new"},
    }
    creator.create_blueprint_version_without_project_update.return_value = {
        "blueprint": {"blueprintVersion": "1"}
    }
    return creator


@pytest.mark.unit
class TestTheClassSurvivesASync:
    @pytest.mark.parametrize("class_id", ["Invoice — {EU}", "_x0041_", "W2"])
    def test_idp_to_bda_and_back(self, service, class_id):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            _schema(class_id)
        )
        assert blueprint["class"] == to_blueprint_class(class_id)
        assert set(blueprint["class"]) <= BDA_TEXT_CHARS

        back = service.transform_bda_blueprint_to_idp_class_schema(
            json.loads(json.dumps(blueprint))
        )
        assert back["$id"] == class_id
        assert back["x-aws-idp-document-type"] == class_id

    def test_the_sent_blueprint_carries_the_escaped_class(self, service):
        creator = _ok_creator(service)
        result = service._process_single_class(_schema("Pay—slip"), [])

        assert result["status"] == "success"
        sent = json.loads(creator.create_blueprint.call_args.kwargs["schema"])
        assert sent["class"] == "Pay_x2014_slip"

    def test_an_unchanged_class_is_not_re_versioned(self, service):
        creator = _ok_creator(service)
        cls = _schema("Pay—slip")
        service._process_single_class(json.loads(json.dumps(cls)), [])
        sent = creator.create_blueprint.call_args.kwargs["schema"]
        existing = {
            "blueprintName": f"{service.blueprint_name_prefix}-Pay-slip-abcd1234",
            "blueprintArn": "arn:bp/old",
            "schema": sent,
        }
        creator.reset_mock()

        result = service._process_single_class(json.loads(json.dumps(cls)), [existing])

        assert result["status"] == "success"
        creator.update_blueprint.assert_not_called()

    def test_a_converted_catalog_blueprint_matches_the_class_it_came_from(
        self, service
    ):
        aws_blueprint = {
            "blueprintArn": "arn:aws:bedrock:us-west-2:aws:blueprint/x",
            "blueprintName": "x",
            "schema": json.dumps(
                {"class": "Pay_x2014_slip", "properties": {"A": {"type": "string"}}}
            ),
        }
        result = service._convert_single_aws_blueprint(
            aws_blueprint, [_schema("Pay—slip")]
        )
        assert result["_internal"] == {"skipped": True}
        assert result["class"] == "Pay—slip"

    def test_replace_mode_bda_to_idp_restores_the_class_id(self, service):
        blueprint = service._transform_json_schema_to_bedrock_blueprint(
            _schema("Invoice — {EU}")
        )
        service._retrieve_all_blueprints = MagicMock(
            return_value=[{"blueprintName": "b", "schema": json.dumps(blueprint)}]
        )
        service.config_manager.get_configuration.return_value = MagicMock(classes=[])

        status = service.create_blueprints_from_custom_configuration(
            version="v1", sync_direction="bda_to_idp", sync_mode="replace"
        )

        assert status == [{"status": "success", "class": "Invoice — {EU}"}]
        written = service.config_manager.handle_update_custom_configuration.call_args
        assert [c["$id"] for c in written.kwargs["custom_config"]["classes"]] == [
            "Invoice — {EU}"
        ]


@pytest.mark.unit
class TestAClassTooLongForBda:
    def test_sixty_characters_are_sent(self, service):
        creator = _ok_creator(service)
        result = service._process_single_class(_schema("A" * 60), [])
        assert result["status"] == "success"
        creator.create_blueprint.assert_called_once()

    def test_sixty_one_characters_fail_with_a_reason(self, service):
        creator = _ok_creator(service)
        result = service._process_single_class(_schema("A" * 61), [])
        assert result["status"] == "failed"
        assert "at most 60 characters" in result["error"]
        assert "needs 61" in result["error"]
        assert "escaped" not in result["error"]
        creator.create_blueprint.assert_not_called()

    def test_the_length_after_escaping_is_what_counts(self, service):
        creator = _ok_creator(service)
        class_id = "A" * 54 + "—"  # 55 characters, 61 once escaped
        result = service._process_single_class(_schema(class_id), [])
        assert result["status"] == "failed"
        assert "needs 61 once the characters BDA does not accept" in result["error"]
        creator.create_blueprint.assert_not_called()
