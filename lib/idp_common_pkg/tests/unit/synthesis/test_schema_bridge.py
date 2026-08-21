# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the synthesis schema bridge.

The load-bearing invariant: converting an IDP config-class schema to the
generator's schema.json must preserve every leaf field name exactly, because
generated field names become the evaluation baseline ``inference_result`` keys.
Any drift => evaluation scores 0. These tests are pure (no AWS, no generator)
and CI-safe.
"""

import pytest

from idp_common.synthesis import schema_bridge

pytestmark = pytest.mark.unit


# A realistic IDP config class modeled on config_library bank-statement-sample:
# draft-2020-12, $defs/$ref, spaced field names, nested object + array-of-object,
# and x-aws-idp-* annotations that must be stripped for the generator.
BANK_STATEMENT_CLASS = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Bank Statement",
    "type": "object",
    "x-aws-idp-document-type": "Bank Statement",
    "description": "Monthly bank account statement",
    "$defs": {
        "Transaction": {
            "type": "object",
            "properties": {
                "Date": {
                    "type": "string",
                    "format": "date",
                    "description": "Transaction date",
                    "x-aws-idp-evaluation-method": "FUZZY",
                    "x-aws-idp-confidence-threshold": "0.9",
                },
                "Description": {
                    "type": "string",
                    "description": "Merchant name",
                    "x-aws-idp-evaluation-method": "SEMANTIC",
                },
                "Amount": {
                    "type": "number",
                    "description": "Transaction amount",
                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                },
            },
            "required": ["Date", "Description", "Amount"],
        },
        "Account Holder Address": {
            "type": "object",
            "description": "Address of the account holder",
            "properties": {
                "City": {
                    "type": "string",
                    "description": "City name",
                    "x-aws-idp-evaluation-method": "FUZZY",
                },
                "ZIP Code": {
                    "type": "string",
                    "pattern": r"\d{5,9}",
                    "description": "5 or 9 digit postal code",
                    "x-aws-idp-evaluation-method": "EXACT",
                },
            },
            "required": ["City"],
        },
    },
    "properties": {
        "Account Holder Address": {
            "description": "Complete address information",
            "$ref": "#/$defs/Account Holder Address",
        },
        "Transactions": {
            "type": "array",
            "description": "List of all transactions",
            "x-aws-idp-list-item-description": "Individual transaction record",
            "items": {"$ref": "#/$defs/Transaction"},
        },
        "Account Number": {
            "type": "string",
            "description": "Primary account identifier",
            "x-aws-idp-evaluation-method": "EXACT",
        },
    },
    "required": ["Account Number"],
}


class TestInlineRefs:
    def test_resolves_object_ref_with_sibling_description(self):
        inlined = schema_bridge.inline_refs(BANK_STATEMENT_CLASS)
        addr = inlined["properties"]["Account Holder Address"]
        # $ref resolved to the actual object...
        assert addr["type"] == "object"
        assert set(addr["properties"]) == {"City", "ZIP Code"}
        # ...and the sibling description placed next to the $ref wins.
        assert addr["description"] == "Complete address information"
        # $defs removed from the inlined output.
        assert "$defs" not in inlined

    def test_resolves_array_items_ref(self):
        inlined = schema_bridge.inline_refs(BANK_STATEMENT_CLASS)
        tx = inlined["properties"]["Transactions"]["items"]
        assert set(tx["properties"]) == {"Date", "Description", "Amount"}

    def test_does_not_mutate_input(self):
        before = schema_bridge.inline_refs(BANK_STATEMENT_CLASS)  # noqa: F841
        assert "$defs" in BANK_STATEMENT_CLASS  # original untouched
        assert "$ref" in BANK_STATEMENT_CLASS["properties"]["Transactions"]["items"]


class TestLeafFieldPaths:
    def test_captures_nested_and_array_paths(self):
        paths = schema_bridge.leaf_field_paths(BANK_STATEMENT_CLASS)
        assert paths == {
            "Account Number",
            "Account Holder Address.City",
            "Account Holder Address.ZIP Code",
            "Transactions[].Date",
            "Transactions[].Description",
            "Transactions[].Amount",
        }

    def test_spaced_field_names_preserved(self):
        paths = schema_bridge.leaf_field_paths(BANK_STATEMENT_CLASS)
        assert "Account Holder Address.ZIP Code" in paths


class TestConfigClassToGeneratorSchema:
    def test_draft_and_title_set(self):
        gen = schema_bridge.config_class_to_generator_schema(BANK_STATEMENT_CLASS)
        assert gen["$schema"] == "http://json-schema.org/draft-07/schema#"
        assert gen["title"] == "Bank Statement"
        assert gen["type"] == "object"

    def test_aws_idp_annotations_stripped(self):
        gen = schema_bridge.config_class_to_generator_schema(BANK_STATEMENT_CLASS)

        def _assert_no_aws_idp(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert not k.startswith("x-aws-idp-"), f"leaked: {k}"
                    _assert_no_aws_idp(v)
            elif isinstance(node, list):
                for v in node:
                    _assert_no_aws_idp(v)

        _assert_no_aws_idp(gen)

    def test_refs_inlined(self):
        gen = schema_bridge.config_class_to_generator_schema(BANK_STATEMENT_CLASS)
        assert "$defs" not in gen
        assert "$ref" not in gen["properties"]["Account Holder Address"]
        assert gen["properties"]["Transactions"]["items"]["type"] == "object"

    def test_standard_keywords_carried(self):
        gen = schema_bridge.config_class_to_generator_schema(BANK_STATEMENT_CLASS)
        addr = gen["properties"]["Account Holder Address"]
        assert addr["properties"]["ZIP Code"]["pattern"] == r"\d{5,9}"
        tx = gen["properties"]["Transactions"]["items"]
        assert tx["properties"]["Date"]["format"] == "date"
        assert tx["required"] == ["Date", "Description", "Amount"]

    def test_LOAD_BEARING_leaf_names_preserved(self):
        """The whole feature depends on this: leaf field paths are identical
        before and after the bridge, so generated baseline keys match the
        config class and evaluation can score > 0."""
        before = schema_bridge.leaf_field_paths(BANK_STATEMENT_CLASS)
        after = schema_bridge.leaf_field_paths(
            schema_bridge.config_class_to_generator_schema(BANK_STATEMENT_CLASS)
        )
        assert before == after

    def test_missing_document_type_raises(self):
        with pytest.raises(ValueError):
            schema_bridge.config_class_to_generator_schema(
                {"type": "object", "properties": {}}
            )


class TestFieldNames:
    def test_flat_leaf_names(self):
        names = schema_bridge.field_names(BANK_STATEMENT_CLASS)
        assert "Account Number" in names
        assert "ZIP Code" in names
        assert "Amount" in names


@pytest.mark.unit
class TestOptionalLeavesAcceptNull:
    """The generator must be allowed to do what the field descriptions instruct.

    IDP config classes are written for extraction, where an absent field is an
    explicit null — many descriptions say "Output null if not shown". Handing that
    schema to the generator with `type: "string"` makes the instruction unsatisfiable:
    the generator emits null, validation rejects it, the critic retries. A real
    11-document run produced 119 critic rejections and lost the whole batch to the
    render stage's timeout.
    """

    @staticmethod
    def _schema(**props):
        return {
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": props,
        }

    def test_an_optional_string_may_be_null(self):
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(
                AFECode={"type": "string", "description": "Output null if not shown."}
            )
        )
        assert out["properties"]["AFECode"]["type"] == ["string", "null"]

    def test_a_required_field_stays_strict(self):
        """A null in a required field is a real failure, worth the retry."""
        schema = self._schema(
            InvoiceNumber={"type": "string"}, AFECode={"type": "string"}
        )
        schema["required"] = ["InvoiceNumber"]
        out = schema_bridge.config_class_to_generator_schema(schema)
        assert out["properties"]["InvoiceNumber"]["type"] == "string"
        assert out["properties"]["AFECode"]["type"] == ["string", "null"]

    def test_objects_and_arrays_are_not_widened(self):
        """Widening a container would let the generator drop whole structures."""
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(
                Vendor={"type": "object", "properties": {"Name": {"type": "string"}}},
                LineItems={
                    "type": "array",
                    "items": {"type": "object", "properties": {}},
                },
            )
        )
        assert out["properties"]["Vendor"]["type"] == "object"
        assert out["properties"]["LineItems"]["type"] == "array"

    def test_nested_and_array_item_leaves_are_reached(self):
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(
                Vendor={"type": "object", "properties": {"Name": {"type": "string"}}},
                LineItems={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"Rate": {"type": "number"}},
                    },
                },
            )
        )
        assert out["properties"]["Vendor"]["properties"]["Name"]["type"] == [
            "string",
            "null",
        ]
        assert out["properties"]["LineItems"]["items"]["properties"]["Rate"][
            "type"
        ] == [
            "number",
            "null",
        ]

    def test_field_names_are_still_preserved_exactly(self):
        """The bridge's core guarantee must survive the widening."""
        schema = self._schema(
            AFECode={"type": "string"},
            Vendor={"type": "object", "properties": {"Name": {"type": "string"}}},
        )
        out = schema_bridge.config_class_to_generator_schema(schema)
        assert schema_bridge.leaf_field_paths(out) == schema_bridge.leaf_field_paths(
            schema
        )

    def test_an_already_nullable_field_is_left_as_is(self):
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(Note={"type": ["string", "null"]})
        )
        assert out["properties"]["Note"]["type"] == ["string", "null"]

    def test_an_optional_enum_also_accepts_null(self):
        """Widening type alone leaves an enum-constrained field still rejecting null.

        enum constrains the value independently of type, so a field with both stayed in
        the critic-retry loop this widening exists to break.
        """
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(
                Mode={
                    "type": "string",
                    "enum": ["air", "sea"],
                    "description": "Output null if not shown.",
                }
            )
        )
        assert out["properties"]["Mode"]["type"] == ["string", "null"]
        assert out["properties"]["Mode"]["enum"] == ["air", "sea", None]

    def test_a_required_enum_is_left_strict(self):
        schema = self._schema(Mode={"type": "string", "enum": ["air", "sea"]})
        schema["required"] = ["Mode"]
        out = schema_bridge.config_class_to_generator_schema(schema)
        assert out["properties"]["Mode"]["type"] == "string"
        assert out["properties"]["Mode"]["enum"] == ["air", "sea"]

    def test_an_enum_already_allowing_null_is_untouched(self):
        out = schema_bridge.config_class_to_generator_schema(
            self._schema(Mode={"type": "string", "enum": ["air", None]})
        )
        assert out["properties"]["Mode"]["enum"] == ["air", None]
