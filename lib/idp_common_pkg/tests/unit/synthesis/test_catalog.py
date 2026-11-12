# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for synthesis.catalog (asset index + LLM matcher)."""

import json
from unittest.mock import Mock

import pytest

from idp_common.synthesis import catalog

pytestmark = pytest.mark.unit


class TestIndexConfigClasses:
    def test_keys_on_document_type(self):
        classes = [
            {
                "$id": "Bank Statement",
                "x-aws-idp-document-type": "Bank Statement",
                "description": "Monthly statement",
                "type": "object",
                "properties": {},
            },
            {"type": "object"},  # no doc type -> skipped
        ]
        entries = catalog.index_config_classes(classes)
        assert len(entries) == 1
        assert entries[0].name == "Bank Statement"
        assert entries[0].source == "config"


class TestBuildCatalog:
    def test_indexes_config_classes(self):
        entries = catalog.build_catalog(
            config_classes=[
                {"x-aws-idp-document-type": "W2", "description": "Tax form"}
            ],
        )
        assert {e.source for e in entries} == {"config"}
        assert entries[0].name == "W2"

    def test_empty_when_no_classes(self):
        assert catalog.build_catalog() == []


class TestMatchCatalog:
    def _entries(self):
        return [
            catalog.CatalogEntry(name="Invoice", source="generator", description="inv"),
            catalog.CatalogEntry(name="W2", source="config", description="tax"),
        ]

    def test_returns_match_above_confidence(self):
        client = Mock()
        client.invoke_model.return_value = {}
        client.extract_text_from_response.return_value = json.dumps(
            {"index": 1, "confidence": 0.9, "reason": "tax form"}
        )
        match = catalog.match_catalog(
            "a W2 wage and tax statement", self._entries(), bedrock_client=client
        )
        assert match is not None and match.name == "W2"

    def test_low_confidence_returns_none(self):
        client = Mock()
        client.invoke_model.return_value = {}
        client.extract_text_from_response.return_value = json.dumps(
            {"index": 0, "confidence": 0.2, "reason": "weak"}
        )
        assert (
            catalog.match_catalog("something", self._entries(), bedrock_client=client)
            is None
        )

    def test_null_index_returns_none(self):
        client = Mock()
        client.invoke_model.return_value = {}
        client.extract_text_from_response.return_value = json.dumps(
            {"index": None, "confidence": 0.0, "reason": "no match"}
        )
        assert (
            catalog.match_catalog("alien form", self._entries(), bedrock_client=client)
            is None
        )

    def test_empty_catalog_returns_none(self):
        assert catalog.match_catalog("anything", [], bedrock_client=Mock()) is None

    def test_bedrock_error_falls_back_to_none(self):
        client = Mock()
        client.invoke_model.side_effect = RuntimeError("boom")
        assert (
            catalog.match_catalog("invoice", self._entries(), bedrock_client=client)
            is None
        )


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
    client = Mock()
    client.invoke_model.return_value = {}
    client.extract_text_from_response.return_value = json.dumps(
        {"index": 0, "confidence": 0.9, "reason": "invoice"}
    )
    entries = [
        catalog.CatalogEntry(name="Invoice", source="generator", description="inv")
    ]
    catalog.match_catalog("an invoice", entries, region=region, bedrock_client=client)
    assert client.invoke_model.call_args.kwargs["model_id"] == expected
