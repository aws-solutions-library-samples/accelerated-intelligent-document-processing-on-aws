# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-instance detection probe (GitHub #753).

The silent half of #565: one section holds several records of the same class, the
class schema describes one document, the model answers with one object, and
records 2..N are simply absent — SUCCESS, COMPLETED, zero issues, instance_count
1. The probe asks the model, in the same inference, how many documents the pages
hold, and the answer is stripped before anything downstream sees the result.
"""

from __future__ import annotations

import pytest

from idp_common.extraction.instance_probe import (
    INSTANCE_PROBE_FIELD,
    augment_schema_with_probe,
    pop_probe_value,
)

pytestmark = pytest.mark.unit


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "CheckNumber": {"type": "string"},
            "NetPay": {"type": "number"},
        },
        "required": ["CheckNumber"],
    }


# --------------------------------------------------------------------------
# augment_schema_with_probe
# --------------------------------------------------------------------------


def test_probe_is_added_to_a_copy_and_never_mutates_the_class_schema():
    """The real class schema drives the off-schema filter, the JSON-Schema
    validator and every downstream stage. Mutating it would make the auxiliary
    field look like a declared attribute everywhere."""
    original = _schema()
    snapshot = {"type": "object", "properties": dict(original["properties"])}

    wire, added = augment_schema_with_probe(original, "Pay-Statement")

    assert added is True
    assert INSTANCE_PROBE_FIELD in wire["properties"]
    assert set(original["properties"]) == set(snapshot["properties"])
    assert INSTANCE_PROBE_FIELD not in original["properties"]
    assert wire is not original


def test_probe_property_is_an_integer_and_not_required():
    """A model that omits the count must not fail the wire schema; an absent
    count simply means 'not determined'."""
    wire, _ = augment_schema_with_probe(_schema(), "Pay-Statement")
    assert wire["properties"][INSTANCE_PROBE_FIELD]["type"] == "integer"
    assert INSTANCE_PROBE_FIELD not in wire.get("required", [])


def test_probe_description_names_the_class_and_asks_for_documents_not_pages():
    """The misleading-footer sample (samples/paystub_multi_instance.pdf) carries a
    document-wide 'N/4' page footer and an identical per-page banner, so a prompt
    that does not say 'count documents, not pages' invites 4."""
    wire, _ = augment_schema_with_probe(_schema(), "Pay-Statement")
    desc = wire["properties"][INSTANCE_PROBE_FIELD]["description"]
    assert "Pay-Statement" in desc
    assert "Do not count pages" in desc


def test_declared_property_of_the_same_name_is_never_shadowed():
    schema = _schema()
    schema["properties"][INSTANCE_PROBE_FIELD] = {"type": "string"}
    wire, added = augment_schema_with_probe(schema, "X")
    assert added is False
    assert wire is schema
    assert wire["properties"][INSTANCE_PROBE_FIELD]["type"] == "string"


@pytest.mark.parametrize(
    "schema", [None, {}, {"type": "object"}, {"properties": {}}, "not-a-dict"]
)
def test_no_probe_when_there_is_nothing_being_asked_for(schema):
    """Fail open, mirroring _filter_extracted_to_schema: with no declared
    properties we cannot tell what is on- or off-schema, so add nothing."""
    wire, added = augment_schema_with_probe(schema, "X")
    assert added is False
    assert wire is schema


# --------------------------------------------------------------------------
# pop_probe_value
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (3, 3),
        ("3", 3),
        (" 3 ", 3),
        (3.0, 3),
        (1, 1),
        ({"G1": 3, "P1": 0.92}, 3),  # 1S-TopK integrated-confidence candidate
        ({"G1": "2", "P1": 0.5}, 2),
        (0, None),
        (-1, None),
        (None, None),
        ("", None),
        ("three", None),
        ([], None),
        (True, None),  # bool is an int subclass; a yes/no answer is not a count
        ({"P1": 0.5}, None),
    ],
)
def test_probe_value_coercion(raw, expected):
    fields = {"CheckNumber": "1", INSTANCE_PROBE_FIELD: raw}
    assert pop_probe_value(fields) == expected


def test_probe_is_always_removed_even_when_unusable():
    """Left behind it would be reported as an off-schema field, scored by
    assessment, written to a reporting column, and diffed against a baseline that
    has no such key."""
    for raw in (3, "nonsense", None, 0, {"G1": 2}):
        fields = {"CheckNumber": "1", INSTANCE_PROBE_FIELD: raw}
        pop_probe_value(fields)
        assert INSTANCE_PROBE_FIELD not in fields
        assert fields == {"CheckNumber": "1"}


def test_absent_probe_returns_none_and_leaves_the_result_untouched():
    fields = {"CheckNumber": "1"}
    assert pop_probe_value(fields) is None
    assert fields == {"CheckNumber": "1"}


def test_non_dict_result_is_tolerated():
    assert pop_probe_value(None) is None
    assert pop_probe_value(["a"]) is None
    assert pop_probe_value("text") is None
