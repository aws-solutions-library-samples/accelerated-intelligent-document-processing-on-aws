# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesize mode — the ``x-aws-idp-multi-instance`` schema transform (#715).

The wrapper is a schema TRANSFORM, not an output envelope: it is declared *in*
the class schema so every consumer that just reads "the class schema" keeps
working. These tests pin the properties that make that true — where each key
lands, that ``$defs`` stays reachable, that the instance axis is designated (or
the whole of #694 goes dark on exactly these schemas), and that the inner items
get a DISTINCT title (or the generated Pydantic model validates one record where
a list was requested).
"""

from __future__ import annotations

import pytest

from idp_common.schema.multi_instance import (
    INSTANCES_KEY,
    is_multi_instance,
    is_wrapped,
    unwrap_instances,
    wrap_class_schema,
    wrap_instances,
)

pytestmark = pytest.mark.unit


def _paystub_class(**extra) -> dict:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Pay-Statement",
        "type": "object",
        "description": "A single employee pay statement.",
        "x-aws-idp-document-type": "Pay-Statement",
        "x-aws-idp-multi-instance": True,
        "properties": {
            "CheckNumber": {"type": "string", "description": "Check number"},
            "NetPay": {"type": "number"},
            "Earnings": {
                "type": "array",
                "items": {"$ref": "#/$defs/EarningLine"},
            },
        },
        "required": ["CheckNumber"],
        "$defs": {
            "EarningLine": {
                "type": "object",
                "properties": {"Description": {"type": "string"}},
            }
        },
    }
    schema.update(extra)
    return schema


# --------------------------------------------------------------------------
# is_multi_instance / opt-in semantics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("1", True),
        (False, False),
        ("false", False),
        ("", False),
        (None, False),
    ],
)
def test_flag_survives_a_config_round_trip(value, expected):
    """DynamoDB/YAML round-trips turn booleans into strings. A flag that reads as
    false after a config save is the exact silent no-op this feature removes."""
    assert is_multi_instance({"x-aws-idp-multi-instance": value}) is expected


def test_unflagged_class_is_returned_untouched_same_object():
    """Strictly opt-in: no existing config changes behaviour, and the no-op costs
    nothing on the hot path (every stage calls this for every class)."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert wrap_class_schema(schema) is schema


@pytest.mark.parametrize("value", [None, "text", 42, [], {}])
def test_non_class_inputs_are_tolerated(value):
    assert wrap_class_schema(value) is value


# --------------------------------------------------------------------------
# The wrapper's shape
# --------------------------------------------------------------------------


def test_wrapper_shape_matches_the_issue():
    wrapped = wrap_class_schema(_paystub_class())

    assert wrapped["type"] == "object"
    assert wrapped["required"] == [INSTANCES_KEY]
    assert list(wrapped["properties"]) == [INSTANCES_KEY]

    instances = wrapped["properties"][INSTANCES_KEY]
    assert instances["type"] == "array"
    assert instances["minItems"] == 1
    assert instances["items"]["type"] == "object"
    # The record keeps the class's own properties, unchanged.
    assert set(instances["items"]["properties"]) == {
        "CheckNumber",
        "NetPay",
        "Earnings",
    }
    assert instances["items"]["required"] == ["CheckNumber"]


def test_input_schema_is_never_mutated():
    """Every stage derives the wrapper independently from the SAME config object,
    so a mutating transform would corrupt the class for whichever stage ran
    next."""
    original = _paystub_class()
    before = repr(original)
    wrap_class_schema(original)
    assert repr(original) == before


def test_transform_is_idempotent():
    once = wrap_class_schema(_paystub_class())
    twice = wrap_class_schema(once)
    assert twice is once
    assert is_wrapped(once)
    # instances[i].instances[j] would be the double-wrap footgun.
    assert INSTANCES_KEY not in once["properties"][INSTANCES_KEY]["items"].get(
        "properties", {}
    )


def test_defs_are_hoisted_to_the_wrapper_so_refs_still_resolve():
    """`{"$ref": "#/$defs/EarningLine"}` resolves from the document ROOT. Moving
    $defs down with the properties would leave every ref inside the record
    dangling — a schema that generates a broken model or fails validation."""
    wrapped = wrap_class_schema(_paystub_class())
    assert "EarningLine" in wrapped["$defs"]
    record = wrapped["properties"][INSTANCES_KEY]["items"]
    assert "$defs" not in record
    assert record["properties"]["Earnings"]["items"]["$ref"] == "#/$defs/EarningLine"


def test_class_level_metadata_stays_on_the_wrapper():
    """These are read from "the class schema" by classification, extraction's
    per-class overrides, the exclusion short-circuit and the few-shot builder."""
    schema = _paystub_class(
        **{
            "x-aws-idp-document-name-regex": "(?i).*paystub.*",
            "x-aws-idp-extraction-model": "us.anthropic.claude-sonnet-5",
            "x-aws-idp-exclude-from-processing": False,
            "x-aws-idp-page-types": [{"name": "Summary"}],
            "x-aws-idp-examples": [{"x-aws-idp-attributes-prompt": "…"}],
        }
    )
    wrapped = wrap_class_schema(schema)
    for key in (
        "$id",
        "$schema",
        "x-aws-idp-document-type",
        "x-aws-idp-document-name-regex",
        "x-aws-idp-extraction-model",
        "x-aws-idp-page-types",
        "x-aws-idp-examples",
    ):
        assert wrapped[key] == schema[key], key


def test_the_wrapper_designates_the_synthesized_property_as_the_instance_axis():
    """Found empirically: the wrapper ALONE extracts all three records but leaves
    Section.instance_count at 1, so the UI badge, the multi-instance warning and
    the MultiInstanceSections / MultiInstanceRecordsRecovered metrics all still
    report one instance. Everything #694 shipped goes dark on exactly the schemas
    #715 creates unless the transform emits both."""
    wrapped = wrap_class_schema(_paystub_class())
    assert wrapped["x-aws-idp-instance-array"] == INSTANCES_KEY


def test_inner_items_get_a_distinct_title():
    """_find_model_in_module selects by title/label priority, so items that keep
    the class title select the INNER model — silently validating ONE instance
    instead of the list (plan D9)."""
    wrapped = wrap_class_schema(_paystub_class(title="Pay Statement"))
    wrapper_title = wrapped["title"]
    record_title = wrapped["properties"][INSTANCES_KEY]["items"]["title"]
    assert wrapper_title != record_title
    assert record_title not in ("Pay Statement", "Pay-Statement")
    assert "Instances" in wrapper_title


def test_wrapper_and_record_descriptions_both_say_something_useful():
    """Both reach the model: the wrapper's description is the instruction to
    return one entry per document, the record's describes one document."""
    wrapped = wrap_class_schema(_paystub_class())
    assert "one entry" in wrapped["description"].lower()
    assert wrapped["properties"][INSTANCES_KEY]["items"]["description"]


def test_class_match_threshold_becomes_the_row_match_threshold():
    """Stickler REQUIRES Hungarian matching for List[Object], so declaring the
    match threshold on the instances array is what makes record alignment
    order-insensitive in evaluation."""
    wrapped = wrap_class_schema(
        _paystub_class(**{"x-aws-idp-evaluation-match-threshold": 0.7})
    )
    instances = wrapped["properties"][INSTANCES_KEY]
    assert instances["x-aws-idp-evaluation-match-threshold"] == 0.7


def test_no_match_threshold_declared_means_none_is_invented():
    wrapped = wrap_class_schema(_paystub_class())
    assert (
        "x-aws-idp-evaluation-match-threshold"
        not in wrapped["properties"][INSTANCES_KEY]
    )


def test_a_class_with_no_properties_is_left_alone():
    """A class with no attributes already short-circuits extraction; a list of
    empty objects would only add a nesting level to an empty result."""
    schema = {"type": "object", "x-aws-idp-multi-instance": True}
    assert wrap_class_schema(schema) is schema


def test_an_internal_array_is_wrapped_not_reused():
    """An invoice with line_items[] is a single-instance document with an internal
    list. multi-instance on it must give instances[i].line_items[j] — three
    invoices in one section — NOT treat line_items as the instance axis."""
    invoice = {
        "$id": "Invoice",
        "type": "object",
        "x-aws-idp-multi-instance": True,
        "properties": {
            "InvoiceNumber": {"type": "string"},
            "line_items": {
                "type": "array",
                "items": {"type": "object", "properties": {"sku": {"type": "string"}}},
            },
        },
    }
    wrapped = wrap_class_schema(invoice)
    record = wrapped["properties"][INSTANCES_KEY]["items"]
    assert set(record["properties"]) == {"InvoiceNumber", "line_items"}
    assert record["properties"]["line_items"]["type"] == "array"


# --------------------------------------------------------------------------
# unwrap_instances / wrap_instances
# --------------------------------------------------------------------------


def test_unwrap_returns_the_records():
    records = [{"CheckNumber": "1"}, {"CheckNumber": "2"}]
    assert unwrap_instances({INSTANCES_KEY: records}) == records


def test_unwrap_distinguishes_not_wrapped_from_wrapped_but_empty():
    """Every consumer calls this unconditionally and falls back on None, so the
    two must not collapse into one another."""
    assert unwrap_instances({"CheckNumber": "1"}) is None
    assert unwrap_instances({INSTANCES_KEY: []}) == []


@pytest.mark.parametrize(
    "value", [None, [], "text", {INSTANCES_KEY: "not-a-list"}, {INSTANCES_KEY: None}]
)
def test_unwrap_is_tolerant(value):
    assert unwrap_instances(value) is None


def test_unwrap_skips_non_object_elements():
    assert unwrap_instances({INSTANCES_KEY: [{"a": 1}, "junk", None]}) == [{"a": 1}]


def test_wrap_instances_round_trips():
    records = [{"a": 1}, {"a": 2}]
    assert unwrap_instances(wrap_instances(records)) == records
    # A copy, so the caller's list cannot be mutated through the wrapper.
    wrapped = wrap_instances(records)
    wrapped[INSTANCES_KEY].append({"a": 3})
    assert len(records) == 2


# --------------------------------------------------------------------------
# The D9 model-selection guard: a generated model that is NOT the root must
# never be used to validate the response.
# --------------------------------------------------------------------------


def test_generated_model_for_a_wrapper_validates_the_LIST_not_one_record():
    """The end-to-end version of the D9 guard.

    _find_model_in_module selects by title/label priority. A wrapper whose inner
    items keep the class title selects the INNER model, and then validation
    silently keeps ONE record and drops the rest — no error anywhere.
    """
    from idp_common.schema.pydantic_generator import (
        create_pydantic_model_from_json_schema,
    )

    wrapped = wrap_class_schema(_paystub_class())
    model = create_pydantic_model_from_json_schema(wrapped, "Pay-Statement")

    assert INSTANCES_KEY in model.model_fields

    instance = model(
        **{
            INSTANCES_KEY: [
                {"CheckNumber": "77310468"},
                {"CheckNumber": "77298351"},
                {"CheckNumber": "77284207"},
            ]
        }
    )
    dumped = instance.model_dump(mode="json", exclude_none=True)
    assert [r["CheckNumber"] for r in dumped[INSTANCES_KEY]] == [
        "77310468",
        "77298351",
        "77284207",
    ]


def test_guard_prefers_a_model_that_declares_the_top_level_properties():
    """Structural, not name-based, so it also catches the mis-selection arising
    any other way."""
    from pydantic import BaseModel

    from idp_common.schema.pydantic_generator import _ensure_model_covers_schema

    class Inner(BaseModel):
        CheckNumber: str = ""

    class Root(BaseModel):
        instances: list = []

    schema = {"properties": {INSTANCES_KEY: {"type": "array"}}}
    chosen = _ensure_model_covers_schema(
        Inner, [("Inner", Inner), ("Root", Root)], schema
    )
    assert chosen is Root


def test_guard_leaves_a_correct_selection_alone():
    from pydantic import BaseModel

    from idp_common.schema.pydantic_generator import _ensure_model_covers_schema

    class Root(BaseModel):
        instances: list = []

    schema = {"properties": {INSTANCES_KEY: {"type": "array"}}}
    assert _ensure_model_covers_schema(Root, [("Root", Root)], schema) is Root


def test_guard_raises_loudly_when_no_model_covers_the_schema():
    """Better a hard failure than silently validating against a model that drops
    data."""
    from pydantic import BaseModel

    from idp_common.schema.pydantic_generator import (
        PydanticModelGenerationError,
        _ensure_model_covers_schema,
    )

    class Wrong(BaseModel):
        something_else: str = ""

    schema = {"properties": {INSTANCES_KEY: {"type": "array"}}}
    with pytest.raises(PydanticModelGenerationError, match=INSTANCES_KEY):
        _ensure_model_covers_schema(Wrong, [("Wrong", Wrong)], schema)


def test_guard_honours_field_aliases_for_names_that_are_not_identifiers():
    """datamodel-code-generator sanitizes "Date of Birth" to Date_of_Birth and
    records the original as the alias, so an alias-blind guard would reject a
    perfectly good model."""
    from pydantic import BaseModel, Field

    from idp_common.schema.pydantic_generator import _ensure_model_covers_schema

    class Root(BaseModel):
        Date_of_Birth: str = Field(default="", alias="Date of Birth")

    schema = {"properties": {"Date of Birth": {"type": "string"}}}
    assert _ensure_model_covers_schema(Root, [("Root", Root)], schema) is Root


def test_guard_is_a_no_op_for_a_schema_with_no_properties():
    from pydantic import BaseModel

    from idp_common.schema.pydantic_generator import _ensure_model_covers_schema

    class Anything(BaseModel):
        pass

    for schema in ({}, {"properties": {}}, {"type": "object"}):
        assert (
            _ensure_model_covers_schema(Anything, [("Anything", Anything)], schema)
            is Anything
        )
