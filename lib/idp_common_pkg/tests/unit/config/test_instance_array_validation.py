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
