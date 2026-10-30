# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-document discovery's code-level model fallbacks follow the Region."""

from unittest.mock import MagicMock

import pytest

from idp_common.discovery.multi_document_discovery import MultiDocumentDiscovery
from idp_common.discovery.regional_defaults import (
    DEFAULT_ANALYSIS_MODEL_ID,
    DEFAULT_EMBEDDING_MODEL_ID,
    default_analysis_model_id,
    default_embedding_model_id,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("region", "embedding", "analysis"),
    [
        ("us-east-1", DEFAULT_EMBEDDING_MODEL_ID, DEFAULT_ANALYSIS_MODEL_ID),
        ("us-west-2", DEFAULT_EMBEDDING_MODEL_ID, DEFAULT_ANALYSIS_MODEL_ID),
        ("eu-central-1", "eu.cohere.embed-v4:0", "eu.anthropic.claude-sonnet-4-6"),
        ("eu-west-1", "eu.cohere.embed-v4:0", "eu.anthropic.claude-sonnet-4-6"),
        ("ca-central-1", "global.cohere.embed-v4:0", DEFAULT_ANALYSIS_MODEL_ID),
        ("ap-southeast-2", DEFAULT_EMBEDDING_MODEL_ID, DEFAULT_ANALYSIS_MODEL_ID),
    ],
)
def test_the_defaults_follow_the_region(region, embedding, analysis):
    assert default_embedding_model_id(region) == embedding
    assert default_analysis_model_id(region) == analysis


@pytest.mark.unit
def test_the_region_defaults_to_aws_region(monkeypatch):
    """The deployed handlers pass AWS_REGION, and may pass None when it is unset."""
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    assert default_embedding_model_id() == "eu.cohere.embed-v4:0"
    monkeypatch.delenv("AWS_REGION")
    assert default_embedding_model_id(None) == DEFAULT_EMBEDDING_MODEL_ID


@pytest.mark.unit
def test_multi_document_discovery_falls_back_to_the_regional_defaults():
    eu = MultiDocumentDiscovery(region="eu-central-1", bedrock_client=MagicMock())
    assert eu.embedding_model_id == "eu.cohere.embed-v4:0"
    assert eu.analysis_model_id == "eu.anthropic.claude-sonnet-4-6"


@pytest.mark.unit
def test_a_configured_model_is_used_as_it_is():
    pinned = MultiDocumentDiscovery(
        region="eu-central-1",
        config={
            "embedding_model_id": "amazon.titan-embed-image-v1",
            "analysis_model_id": "eu.anthropic.claude-haiku-5-5",
        },
        bedrock_client=MagicMock(),
    )
    assert pinned.embedding_model_id == "amazon.titan-embed-image-v1"
    assert pinned.analysis_model_id == "eu.anthropic.claude-haiku-5-5"
