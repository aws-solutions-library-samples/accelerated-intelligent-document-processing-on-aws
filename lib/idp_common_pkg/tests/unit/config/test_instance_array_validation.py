# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config validation for ``x-aws-idp-instance-array`` (Designate mode).

The key names the top-level array whose length is the section's instance count.
A typo would otherwise fail *silently* at runtime — the count simply never
appears — which is precisely the silent-no-op failure mode this work exists to
remove. So it is validated at config time instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from idp_common.config.models import IDPConfig

KEY = "x-aws-idp-instance-array"


def _klass(**overrides):
    base = {
        "$id": "patient_packet",
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object"}},
        },
    }
    base.update(overrides)
    return base


def test_valid_declaration_is_accepted():
    cfg = IDPConfig(classes=[_klass(**{KEY: "records"})])
    assert cfg.classes[0][KEY] == "records"


def test_absent_declaration_is_fine():
    """The overwhelmingly common case must stay untouched."""
    cfg = IDPConfig(classes=[_klass()])
    assert KEY not in cfg.classes[0]


def test_ref_items_are_allowed():
    """Items resolved via $ref cannot be type-checked here; allow them."""
    IDPConfig(
        classes=[
            _klass(
                **{
                    KEY: "records",
                    "properties": {
                        "records": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/Record"},
                        }
                    },
                    "$defs": {"Record": {"type": "object"}},
                }
            )
        ]
    )


@pytest.mark.parametrize(
    "overrides,expected_fragment",
    [
        # A typo is the failure this validator exists to catch.
        ({KEY: "recrods"}, "not a top-level property"),
        (
            {KEY: "records", "properties": {"records": {"type": "string"}}},
            "must be an array",
        ),
        (
            {
                KEY: "records",
                "properties": {
                    "records": {"type": "array", "items": {"type": "string"}}
                },
            },
            "must be an object",
        ),
        ({KEY: ""}, "must be the name of a top-level array property"),
        ({KEY: ["records"]}, "must be the name of a top-level array property"),
        ({KEY: 7}, "must be the name of a top-level array property"),
    ],
)
def test_malformed_declarations_are_rejected(overrides, expected_fragment):
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[_klass(**overrides)])
    assert expected_fragment in str(exc.value)


def test_error_names_the_class_and_lists_available_properties():
    """The message has to be actionable — a typo needs the candidate names."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[_klass(**{KEY: "recrods"})])
    message = str(exc.value)
    assert "patient_packet" in message
    assert "records" in message


def test_multiple_classes_only_the_bad_one_fails():
    good = _klass(**{KEY: "records"})
    bad = dict(_klass(**{KEY: "nope"}), **{"$id": "other_packet"})
    with pytest.raises(ValidationError) as exc:
        IDPConfig(classes=[good, bad])
    assert "other_packet" in str(exc.value)


# --------------------------------------------------------------------------
# $ref-declared record lists
#
# Found in review of #694: the validator type-checked the raw property node, so a
# record list declared as {"$ref": "#/$defs/RecordList"} — the idiom the UI schema
# editor and several shipped presets use for a reusable record type — was
# rejected outright. The runtime resolver does not care about the schema shape at
# all (it reads the extracted list's length), so this was a false rejection, and
# a HARD config-load failure at that: worse than the silent no-op the validator
# exists to prevent.
# --------------------------------------------------------------------------

_RECORD = {"type": "object", "properties": {"patient_name": {"type": "string"}}}


def test_ref_declared_array_is_accepted():
    cfg = IDPConfig(
        classes=[
            {
                "$id": "patient_packet",
                "type": "object",
                KEY: "records",
                "$defs": {"RecordList": {"type": "array", "items": _RECORD}},
                "properties": {"records": {"$ref": "#/$defs/RecordList"}},
            }
        ]
    )
    assert cfg.classes[0][KEY] == "records"


def test_ref_chain_is_followed():
    cfg = IDPConfig(
        classes=[
            {
                "$id": "patient_packet",
                "type": "object",
                KEY: "records",
                "$defs": {
                    "Outer": {"$ref": "#/$defs/RecordList"},
                    "RecordList": {"type": "array", "items": _RECORD},
                },
                "properties": {"records": {"$ref": "#/$defs/Outer"}},
            }
        ]
    )
    assert cfg.classes[0][KEY] == "records"


def test_ref_to_a_non_array_is_still_rejected():
    """Dereferencing must not weaken the check into a rubber stamp."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(
            classes=[
                {
                    "$id": "patient_packet",
                    "type": "object",
                    KEY: "records",
                    "$defs": {"NotAList": {"type": "string"}},
                    "properties": {"records": {"$ref": "#/$defs/NotAList"}},
                }
            ]
        )
    assert "it must be an array" in str(exc.value)


def test_unresolvable_ref_falls_back_to_checking_the_node_itself():
    """A dangling $ref cannot be proven to be an array, so it is still rejected."""
    with pytest.raises(ValidationError) as exc:
        IDPConfig(
            classes=[
                {
                    "$id": "patient_packet",
                    "type": "object",
                    KEY: "records",
                    "properties": {"records": {"$ref": "#/$defs/Missing"}},
                }
            ]
        )
    assert "it must be an array" in str(exc.value)
