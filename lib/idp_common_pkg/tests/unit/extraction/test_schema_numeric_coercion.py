# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stringified numeric schema constraints are coerced once, where the schema enters.

``ConfigurationRecord._stringify_values`` converts every numeric scalar to a string
on the way into the Configuration table and recurses into a class's ``json_schema``;
nothing coerces them back on read, because ``classes`` is ``List[Dict[str, Any]]`` so
validation never descends into it. A class authored in the Web UI therefore arrives
with ``minItems: "100"``, and the reader that compared it without a guard raised
``TypeError`` and lost its section's whole completeness report (#797).

Five separate implementations of the same coercion had accumulated before that
happened. These tests pin the single one: ``_get_class_schema`` is where every
reader in the extraction service obtains a schema, so coercing there covers all of
them — and an uncoercible value stays put and gets logged, because "constraint
silently ignored" is exactly the failure mode that let #797 hide (#823).

Scope note, so these tests are not read as proving more than they do: no shipping
reader is broken by the string form *today*. #798's guard coerces ``"100"``
successfully (it only treats an unreadable value as absent), and both the
generated Pydantic model and the ``jsonschema`` path tolerate the string as well
(verified directly — datamodel-code-generator coerces it, and
``_strip_idp_extensions`` already coerces). What this covers is the next reader
added without a guard, and the constraint that cannot be read as a number at all.
"""

import logging
from typing import Any

import pytest

from idp_common.extraction.service import ExtractionService
from idp_common.extraction.validation import coerce_numeric_schema_keywords

pytestmark = pytest.mark.unit


def _stringified_class() -> dict[str, Any]:
    """A class as it comes back from the Configuration table: numbers as strings."""
    return {
        "$id": "BankStatement",
        "type": "object",
        "properties": {
            "AccountNumber": {"type": "string", "minLength": "8", "maxLength": "20"},
            "Transactions": {
                "type": "array",
                "minItems": "100",
                "maxItems": "5000",
                "items": {"$ref": "#/$defs/Txn"},
            },
        },
        "$defs": {
            "Txn": {
                "type": "object",
                "properties": {"Amount": {"type": "number", "minimum": "0"}},
            }
        },
    }


def _service(class_obj: dict[str, Any]) -> ExtractionService:
    return ExtractionService(
        region="us-west-2",
        config={
            "extraction": {"model": "us.amazon.nova-pro-v1:0"},
            "classes": [class_obj],
        },
    )


class TestThroughTheServiceEntryPoint:
    def test_every_constraint_arrives_numeric(self):
        """Including the ones nested in ``items`` and behind a ``$ref`` in ``$defs``."""
        schema = _service(_stringified_class())._get_class_schema("BankStatement")

        props = schema["properties"]
        assert props["Transactions"]["minItems"] == 100
        assert props["Transactions"]["maxItems"] == 5000
        assert props["AccountNumber"]["minLength"] == 8
        assert props["AccountNumber"]["maxLength"] == 20
        assert schema["$defs"]["Txn"]["properties"]["Amount"]["minimum"] == 0
        assert all(
            not isinstance(v, str)
            for v in (
                props["Transactions"]["minItems"],
                props["AccountNumber"]["minLength"],
            )
        )

    def test_the_completeness_check_gets_a_number_from_config(self):
        """The #797 composition end to end: a class as the Configuration table
        returns it, straight into the reader that crashed on it. This passes on
        `develop` too, because #798 gave that reader its own coercion — the point
        here is that the constraint is enforced without relying on it, so the
        reader's guard becomes belt-and-braces rather than the only thing standing
        between a UI-authored `minItems` and a TypeError."""
        svc = _service(_stringified_class())
        schema = svc._get_class_schema("BankStatement")

        check = svc._check_completeness_detailed(
            extracted_fields={"Transactions": [{"Amount": 1}, {"Amount": 2}]},
            schema=schema,
            tool_used=False,
        )

        assert check["schema_constraints_met"] is False
        assert check["violations"][0]["shortfall"] == 98

    def test_the_stored_config_is_not_mutated(self):
        """The coercion hands back a copy: the config object is shared with every
        other consumer, several of which round-trip it back to DynamoDB."""
        class_obj = _stringified_class()
        svc = _service(class_obj)

        svc._get_class_schema("BankStatement")

        assert class_obj["properties"]["Transactions"]["minItems"] == "100"
        assert svc.config.classes[0]["properties"]["Transactions"]["minItems"] == "100"

    def test_a_class_with_nothing_to_coerce_is_handed_back_unchanged(self):
        """Identity against the object the service holds, not just equality — the
        common case must not rebuild the schema on every section. (``IDPConfig``
        validation copies the caller's dict, so the config's own object is the one
        to compare with.)"""
        svc = _service(
            {
                "$id": "Receipt",
                "type": "object",
                "properties": {"Total": {"type": "string"}},
            }
        )

        assert svc._get_class_schema("Receipt") is svc.config.classes[0]


class TestTheCoercionItself:
    def test_already_numeric_values_are_left_alone(self):
        schema = {"properties": {"L": {"type": "array", "minItems": 3}}}

        assert coerce_numeric_schema_keywords(schema) is schema

    def test_a_float_constraint_survives_as_a_float(self):
        coerced = coerce_numeric_schema_keywords({"multipleOf": "0.25"})

        assert coerced["multipleOf"] == 0.25

    def test_a_negative_bound_survives(self):
        coerced = coerce_numeric_schema_keywords({"minimum": "-5"})

        assert coerced["minimum"] == -5

    def test_only_the_numeric_keywords_are_touched(self):
        """A stringified value under a non-numeric keyword is data, not a bound."""
        coerced = coerce_numeric_schema_keywords(
            {"description": "100", "default": "3", "minItems": "3"}
        )

        assert coerced["description"] == "100"
        assert coerced["default"] == "3"
        assert coerced["minItems"] == 3

    def test_an_uncoercible_constraint_is_left_alone_and_logged(self, caplog):
        """It must not raise, must not silently vanish, and must not be guessed at.
        The guards downstream then ignore that one constraint — which is the
        pre-existing behaviour — but now there is a record of it."""
        schema = {"properties": {"L": {"type": "array", "minItems": "many"}}}

        with caplog.at_level(logging.WARNING):
            coerced = coerce_numeric_schema_keywords(schema)

        assert coerced["properties"]["L"]["minItems"] == "many"
        assert "minItems" in caplog.text
        assert "properties.L.minItems" in caplog.text

    def test_lists_are_walked(self):
        """``anyOf``/``oneOf`` branches and tuple-form ``items`` are lists."""
        coerced = coerce_numeric_schema_keywords(
            {"anyOf": [{"type": "array", "minItems": "2"}, {"type": "null"}]}
        )

        assert coerced["anyOf"][0]["minItems"] == 2

    def test_idempotent(self):
        once = coerce_numeric_schema_keywords(_stringified_class())

        assert coerce_numeric_schema_keywords(once) is once

    def test_non_dict_input_passes_through(self):
        assert coerce_numeric_schema_keywords(None) is None
        assert coerce_numeric_schema_keywords("nope") == "nope"
