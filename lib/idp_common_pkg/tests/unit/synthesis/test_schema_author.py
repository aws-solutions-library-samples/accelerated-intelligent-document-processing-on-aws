# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for synthesis.schema_author (prompt -> IDP document-class schema).

No AWS: the Bedrock client is mocked. Tests cover prompt construction, the
validate-and-retry loop, per-field eval-method normalization, and class-name
override.
"""

import json
from unittest.mock import Mock

import pytest

from idp_common.synthesis import schema_author

pytestmark = pytest.mark.unit


def _mock_bedrock_returning(*texts):
    """A mock BedrockClient whose invoke_model returns the given texts in order."""
    client = Mock()
    client.invoke_model.return_value = {"_": "raw"}
    client.extract_text_from_response.side_effect = list(texts)
    return client


VALID_SCHEMA_TEXT = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Paystub",
        "x-aws-idp-document-type": "Paystub",
        "type": "object",
        "description": "Employee pay statement",
        "properties": {
            "EmployeeName": {
                "type": "string",
                "description": "Name",
                "x-aws-idp-evaluation-method": "FUZZY",
            },
            "GrossPay": {
                "type": "number",
                "description": "Gross pay",
            },
            "NetPay": {
                "type": "number",
                "description": "Net pay",
                "x-aws-idp-evaluation-method": "BOGUS",
            },
        },
    }
)


class TestBuildAuthorPrompt:
    def test_includes_description_and_methods(self):
        p = schema_author.build_author_prompt("a paystub with gross and net pay")
        assert "paystub" in p.lower()
        assert "x-aws-idp-evaluation-method" in p
        assert "NUMERIC_EXACT" in p

    def test_field_hints_listed(self):
        p = schema_author.build_author_prompt(
            "an invoice", field_hints=["InvoiceNumber", "TotalDue"]
        )
        assert "InvoiceNumber" in p and "TotalDue" in p

    def test_seed_schema_triggers_adapt_framing(self):
        seed = {"$id": "Invoice", "type": "object", "properties": {"Total": {}}}
        p = schema_author.build_author_prompt("a utility bill", seed_schema=seed)
        assert "ADAPTING" in p
        assert "SEED SCHEMA" in p

    def test_class_name_pinned(self):
        p = schema_author.build_author_prompt("x", class_name="MyForm")
        assert "MyForm" in p


class TestAuthorClassSchema:
    def test_returns_valid_schema_and_normalizes_eval_methods(self):
        client = _mock_bedrock_returning(VALID_SCHEMA_TEXT)
        schema = schema_author.author_class_schema("a paystub", bedrock_client=client)
        assert schema is not None
        props = schema["properties"]
        assert props["GrossPay"]["x-aws-idp-evaluation-method"] == "EXACT"
        assert props["NetPay"]["x-aws-idp-evaluation-method"] == "EXACT"
        assert props["EmployeeName"]["x-aws-idp-evaluation-method"] == "FUZZY"

    def test_retries_then_succeeds(self):
        client = _mock_bedrock_returning("not json at all", VALID_SCHEMA_TEXT)
        schema = schema_author.author_class_schema(
            "a paystub", bedrock_client=client, max_retries=3
        )
        assert schema is not None
        assert client.invoke_model.call_count == 2

    def test_returns_none_after_exhausting_retries(self):
        client = _mock_bedrock_returning("garbage", "still garbage", "nope")
        schema = schema_author.author_class_schema(
            "a paystub", bedrock_client=client, max_retries=3
        )
        assert schema is None

    def test_class_name_override_applied(self):
        client = _mock_bedrock_returning(VALID_SCHEMA_TEXT)
        schema = schema_author.author_class_schema(
            "a paystub", class_name="CustomPaystub", bedrock_client=client
        )
        assert schema["$id"] == "CustomPaystub"
        assert schema["x-aws-idp-document-type"] == "CustomPaystub"

    def test_rejects_schema_missing_document_type(self):
        bad = json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "X",
                "type": "object",
                "properties": {"A": {"type": "string"}},
            }
        )
        client = _mock_bedrock_returning(bad, bad, bad)
        assert schema_author.author_class_schema("x", bedrock_client=client) is None


class TestAuthoredSchemaBridges:
    """The authored schema must round-trip through the bridge with no drift."""

    def test_authored_schema_field_names_preserved_through_bridge(self):
        from idp_common.synthesis import schema_bridge

        client = _mock_bedrock_returning(VALID_SCHEMA_TEXT)
        schema = schema_author.author_class_schema("a paystub", bedrock_client=client)
        before = schema_bridge.leaf_field_paths(schema)
        gen = schema_bridge.config_class_to_generator_schema(schema)
        after = schema_bridge.leaf_field_paths(gen)
        assert before == after
        assert before == {"EmployeeName", "GrossPay", "NetPay"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-east-1", "us.anthropic.claude-sonnet-4-6"),
        ("eu-west-1", "eu.anthropic.claude-sonnet-4-6"),
        ("ca-central-1", "us.anthropic.claude-sonnet-4-6"),
    ],
)
def test_the_default_model_is_callable_from_the_region(region, expected):
    """With no model given, the call goes to the model the deploy-time swap would
    write for ``region``: a us. profile is not callable from an EU Region."""
    client = _mock_bedrock_returning(VALID_SCHEMA_TEXT)
    schema_author.author_class_schema("a paystub", region=region, bedrock_client=client)
    assert client.invoke_model.call_args.kwargs["model_id"] == expected
