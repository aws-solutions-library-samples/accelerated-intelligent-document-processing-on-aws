# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Characters BDA rejects in blueprint text (issue #1741).

CreateBlueprint rejects a whole blueprint ("Request has invalid blueprint
schema") when one ``instruction`` or ``description`` holds a character outside a
fixed set. The shipped ``ds11-passport-application`` preset failed sync over one
em dash. The facts below were measured against the BDA control plane
(``CreateBlueprint``, DOCUMENT, us-west-2) with the schema serialised by
``json.dumps``, which is how the converter sends it:

- ``PROBED_TEXT`` was accepted as a whole, both as a leaf ``instruction`` and as
  the blueprint ``description``.
- Each character in ``PROBED_REJECTS`` was rejected on its own, in the middle of
  an otherwise accepted instruction and description.
- ``"-"`` alone was accepted as an instruction; ``""`` and ``"   "`` were rejected.
"""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from idp_common.bda.bda_blueprint_service import BdaBlueprintService

# Accepted in one CreateBlueprint call, verbatim (after the four-character
# prefix "All "), as an instruction and as a description. Tab, LF, CR, FF and VT
# were each accepted in separate probes.
PROBED_TEXT = (
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]_"
    "abcdefghijklmnopqrstuvwxyz|"
    "\xa1\xa3\xa5\xa7\xa9\xaa\xae\xb0\xb1\xb2\xb3\xb4\xb5\xb9\xbf"
    "\xc0\xc1\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xcb\xcc\xcd\xce\xcf"
    "\xd0\xd1\xd2\xd3\xd4\xd5\xd6\xd8\xd9\xda\xdb\xdc\xdf"
    "\xe0\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xeb\xec\xed\xee\xef"
    "\xf0\xf1\xf2\xf3\xf4\xf5\xf6\xf8\xf9\xfa\xfb\xfc"
    "Œœ฿ẞ₹€™✓⑆⑇⑈⑉"
    "₽₱₦₣₩₫₺"
)
# Each accepted on its own, in an instruction and in a description,
# although the API's regular expression does not list them.
PROBED_SINGLES = "\u2019\u201c\u201d\u200b"
PROBED_CHARS = (
    frozenset(PROBED_TEXT) | frozenset("\t\n\r\f\v") | frozenset(PROBED_SINGLES)
)

# Each rejected on its own.
PROBED_REJECTS = (
    "—",  # em dash
    "–",  # en dash
    "−",  # minus sign
    "‘",  # left single quotation mark
    "…",  # ellipsis
    "\u00a0",  # no-break space
    "\u00ad",  # soft hyphen
    "\ufeff",  # BOM
    "•",  # bullet
    "×",  # multiplication sign
    "÷",  # division sign
    "þ",  # thorn
    "ÿ",  # y with diaeresis
    "\U0001f600",  # emoji
    "α",  # Greek
    "Ж",  # Cyrillic
    "ع",  # Arabic
    "中",  # CJK
    "あ",  # hiragana
    "한",  # hangul
    "\x01",
    "\x7f",
    "^",
    "`",
    "{",
    "}",
    "~",
)

PRESETS_DIR = Path(__file__).resolve().parents[5] / "config_library" / "unified"


def _texts(node, path=""):
    """Yield (path, value) for every instruction and description string."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key in ("instruction", "description") and isinstance(value, str):
                yield child, value
            else:
                yield from _texts(value, child)
    elif isinstance(node, list):
        for item in node:
            yield from _texts(item, path)


def _preset_classes():
    presets = sorted(PRESETS_DIR.glob("*/config.yaml"))
    assert presets, f"no presets under {PRESETS_DIR}"
    for path in presets:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        classes = config.get("classes") or []
        assert classes, f"{path.parent.name} has no classes"
        for cls in classes:
            yield path.parent.name, cls


PRESET_CLASSES = list(_preset_classes())


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


def _convert(service, cls):
    """Convert one class the way a sync does: sanitise, then transform."""
    sanitized, _ = service._sanitize_property_names(json.loads(json.dumps(cls)))
    return service._transform_json_schema_to_bedrock_blueprint(sanitized)


@pytest.mark.unit
class TestProbeFacts:
    def test_the_probed_sets_are_disjoint(self):
        assert not PROBED_CHARS & set(PROBED_REJECTS)

    def test_converter_set_is_exactly_the_probed_set(self):
        from idp_common.bda.bda_blueprint_service import _BDA_TEXT_CHARS

        assert _BDA_TEXT_CHARS == PROBED_CHARS


@pytest.mark.unit
class TestEveryPresetConverts:
    def test_some_preset_text_needed_normalising(self):
        """Non-vacuity: the presets do carry characters BDA rejects."""
        found = {
            (preset, path)
            for preset, cls in PRESET_CLASSES
            for path, text in _texts(cls)
            if set(text) - PROBED_CHARS
        }
        assert any(p == "ds11-passport-application" for p, _ in found)
        assert len({p for p, _ in found}) >= 2

    @pytest.mark.parametrize(
        "preset,cls",
        PRESET_CLASSES,
        ids=[f"{p}:{c.get('$id')}" for p, c in PRESET_CLASSES],
    )
    def test_converted_text_holds_only_characters_bda_takes(self, service, preset, cls):
        blueprint = _convert(service, cls)
        texts = list(_texts(blueprint))
        assert texts, "no text values in the blueprint"
        for path, text in texts:
            bad = sorted(set(text) - PROBED_CHARS)
            assert not bad, f"{preset}:{blueprint['class']} {path}: {bad}"
            assert text.strip(), f"{preset}:{blueprint['class']} {path} is blank"

    def test_passport_place_of_birth_keeps_its_meaning(self, service):
        cls = next(
            c
            for p, c in PRESET_CLASSES
            if p == "ds11-passport-application" and c["$id"] == "PassportApplication"
        )
        instruction = _convert(service, cls)["properties"]["PlaceOfBirth"][
            "instruction"
        ]
        assert "—" in cls["properties"]["PlaceOfBirth"]["description"]
        assert instruction == cls["properties"]["PlaceOfBirth"]["description"].replace(
            "—", "-"
        )


@pytest.mark.unit
class TestNormalizeText:
    @pytest.mark.parametrize("ch", PROBED_REJECTS)
    def test_every_rejected_character_is_gone(self, service, ch):
        out = service._normalize_bda_text(f"a {ch} b", "p")
        assert set(out) <= PROBED_CHARS, out

    def test_accepted_text_is_returned_unchanged(self, service):
        text = "All " + PROBED_TEXT + "\t\n\r\f\v" + PROBED_SINGLES
        assert service._normalize_bda_text(text, "p") is text
        assert service._skipped_properties == []

    @pytest.mark.parametrize(
        "text,out",
        [
            ("Place of birth — City & State", "Place of birth - City & State"),
            ("pages 5–6", "pages 5-6"),
            ("‘a’ `c`", "'a’ 'c'"),
            ("wait…", "wait..."),
            ("a\u00a0b\u202fc", "a b c"),
            ("soft\u00adhy\u200cphen\ufeff", "softhyphen"),
            ("• item", "- item"),
            ("3×4 ÷ 2", "3x4 / 2"),
            ("Q{N}-{YYYY}", "Q(N)-(YYYY)"),
            ("open → closed", "open -> closed"),
            # NFKD: decomposable letters, full-width forms, ligatures, fractions.
            ("ý Ａ ﬁ ½", "y A fi 1/2"),
            # NFC first: a decomposed e-acute is composed, not stripped.
            ("e\u0301", "é"),
        ],
    )
    def test_meaning_preserving_replacements(self, service, text, out):
        assert service._normalize_bda_text(text, "p") == out
        assert service._skipped_properties == []

    def test_unrepresentable_characters_are_removed_and_recorded(self, service):
        with service._recording_drops_for("C") as recording:
            out = service._normalize_bda_text("x^2 ~5 中中", "properties.F")
        assert out == "x2 5 "
        assert [(d["class"], d["property"], d["type"]) for d in recording.drops] == [
            ("C", "properties.F", "unsupported_characters")
        ]
        message = recording.drops[0]["message"]
        for code in ("U+005E", "U+007E", "U+4E2D"):
            assert code in message
        assert "removed 4" in message

    @pytest.mark.parametrize("text", ["", "   ", "\t\n", "\u200b", " \u200b "])
    def test_blank_text_becomes_a_dash(self, service, text):
        """A blank instruction was probed as rejected."""
        assert service._normalize_bda_text(text, "p") == "-"

    def test_text_left_blank_becomes_a_dash(self, service):
        assert service._normalize_bda_text("中文", "p") == "-"
        assert service._normalize_bda_text("\u00a0\u200c", "p") == "-"

    def test_replacement_is_logged(self, service, caplog):
        with caplog.at_level(logging.INFO):
            service._normalize_bda_text("a—b", "properties.F.instruction")
        assert "properties.F.instruction" in caplog.text


@pytest.mark.unit
class TestNormalizeBlueprint:
    def test_every_text_location_is_normalized(self, service):
        schema = {
            "$id": "T",
            "description": "top — level",
            "$defs": {
                "D": {"properties": {"A": {"type": "string", "description": "d—"}}}
            },
            "properties": {
                "Ref": {"$ref": "#/$defs/D"},
                "Leaf": {"type": "string", "description": "leaf…"},
                "Instr": {"type": "string", "instruction": "i–"},
                "Arr": {
                    "type": "array",
                    "description": "arr‘",
                    "items": {"type": "string"},
                },
                "Obj": {
                    "type": "object",
                    "properties": {"B": {"type": "string", "description": "o\u00a0"}},
                },
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        texts = dict(_texts(blueprint))
        assert texts == {
            "description": "top - level",
            "definitions.D.properties.A.instruction": "d-",
            "definitions.Obj.properties.B.instruction": "o ",
            "properties.Ref.instruction": "-",
            "properties.Leaf.instruction": "leaf...",
            "properties.Instr.instruction": "i-",
            "properties.Arr.instruction": "arr'",
            "properties.Obj.instruction": "-",
        }

    def test_the_flat_path_is_normalized_too(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "Obj": {
                    "type": "object",
                    "properties": {"B": {"type": "string", "description": "o—"}},
                },
                "Rows": {
                    "type": "array",
                    "description": "rows—",
                    "items": {
                        "type": "object",
                        "properties": {"C": {"type": "string", "description": "c—"}},
                    },
                },
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        for path, text in _texts(blueprint):
            assert set(text) <= PROBED_CHARS, (path, text)
        assert blueprint["definitions"]["Obj"]["properties"]["B"]["instruction"] == (
            "o-"
        )
        assert blueprint["properties"]["Rows"]["instruction"] == "rows-"

    def test_a_property_named_description_is_not_overwritten(self, service):
        schema = {
            "$id": "T",
            "properties": {
                "description": {"type": "string", "description": "the — text"},
                "instruction": {"type": "string", "description": "x"},
            },
        }
        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)
        assert blueprint["properties"]["description"] == {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "the - text",
        }
        assert blueprint["properties"]["instruction"]["instruction"] == "x"

    def test_input_is_not_mutated(self, service):
        schema = {
            "$id": "T",
            "description": "a—b",
            "properties": {"F": {"type": "string", "description": "c—d"}},
        }
        before = json.dumps(schema, sort_keys=True)
        service._transform_json_schema_to_bedrock_blueprint(schema)
        assert json.dumps(schema, sort_keys=True) == before

    def test_drops_reach_the_class_result(self, service):
        """A lossy removal surfaces as a per-class warning from a real sync call."""
        cls = {
            "$id": "Lossy",
            "properties": {"F": {"type": "string", "description": "中 name"}},
        }
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {"blueprintArn": "arn:bp", "blueprintName": "n"},
        }
        service.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
            "blueprint": {"blueprintVersion": "1"}
        }
        result = service._process_single_class(cls, [])
        assert result["status"] == "success"
        sent = json.loads(
            service.blueprint_creator.create_blueprint.call_args.kwargs["schema"]
        )
        assert sent["properties"]["F"]["instruction"] == " name"
        assert [(w["class"], w["type"]) for w in result["warnings"]] == [
            ("Lossy", "unsupported_characters")
        ]
