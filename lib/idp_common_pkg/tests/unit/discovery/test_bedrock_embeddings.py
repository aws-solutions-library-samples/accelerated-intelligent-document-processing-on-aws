# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for BedrockClient multimodal embedding enhancements."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bedrock.client import BedrockClient


@pytest.fixture
def client():
    """Create a BedrockClient with mocked AWS client."""
    c = BedrockClient(region="us-east-1", metrics_enabled=False)
    return c


class TestBuildEmbeddingRequestBody:
    """Tests for _build_embedding_request_body."""

    def test_titan_text_embed(self, client):
        """Test Titan text embed request body."""
        body = json.loads(
            client._build_embedding_request_body(
                model_id="amazon.titan-embed-text-v1",
                text="hello world",
            )
        )
        assert body == {"inputText": "hello world"}

    def test_titan_multimodal_text_only(self, client):
        """Test Titan multimodal with text only."""
        body = json.loads(
            client._build_embedding_request_body(
                model_id="amazon.titan-embed-image-v1",
                text="hello",
            )
        )
        assert body == {"inputText": "hello"}

    def test_titan_multimodal_image_only(self, client):
        """Test Titan multimodal with image only."""
        img_bytes = b"\x89PNG\r\n\x1a\n"
        body = json.loads(
            client._build_embedding_request_body(
                model_id="amazon.titan-embed-image-v1",
                image_bytes=img_bytes,
            )
        )
        assert "inputImage" in body
        assert body["inputImage"] == base64.b64encode(img_bytes).decode("utf-8")

    def test_titan_multimodal_text_and_image(self, client):
        """Test Titan multimodal with both text and image."""
        img_bytes = b"\x89PNG\r\n\x1a\n"
        body = json.loads(
            client._build_embedding_request_body(
                model_id="amazon.titan-embed-image-v1",
                text="hello",
                image_bytes=img_bytes,
            )
        )
        assert body["inputText"] == "hello"
        assert "inputImage" in body

    def test_titan_text_with_image_routes_to_multimodal(self, client):
        """Test that titan-embed-text with image bytes uses multimodal format."""
        img_bytes = b"fake_image"
        body = json.loads(
            client._build_embedding_request_body(
                model_id="amazon.titan-embed-text-v2:0",
                text="hello",
                image_bytes=img_bytes,
            )
        )
        # When image_bytes are provided, should use multimodal format
        assert "inputImage" in body

    def test_cohere_text_only(self, client):
        """Test Cohere embed request body with text."""
        body = json.loads(
            client._build_embedding_request_body(
                model_id="cohere.embed-english-v3",
                text="hello world",
                input_type="search_document",
            )
        )
        assert body["texts"] == ["hello world"]
        assert body["input_type"] == "search_document"
        assert "images" not in body

    def test_cohere_image_only(self, client):
        """Test Cohere embed request body with image."""
        img_bytes = b"fake_image_data"
        body = json.loads(
            client._build_embedding_request_body(
                model_id="cohere.embed-english-v3",
                image_bytes=img_bytes,
                input_type="clustering",
            )
        )
        assert "images" in body
        assert body["images"][0] == base64.b64encode(img_bytes).decode("utf-8")
        assert body["input_type"] == "clustering"
        assert "texts" not in body

    def test_cohere_text_and_image(self, client):
        """Test Cohere embed request body with both text and image."""
        img_bytes = b"fake_image_data"
        body = json.loads(
            client._build_embedding_request_body(
                model_id="cohere.embed-english-v3",
                text="hello",
                image_bytes=img_bytes,
            )
        )
        assert body["texts"] == ["hello"]
        assert len(body["images"]) == 1

    def test_default_format(self, client):
        """Test default format for unknown models."""
        body = json.loads(
            client._build_embedding_request_body(
                model_id="some.unknown-model",
                text="hello",
            )
        )
        assert body == {"text": "hello"}


class TestGenerateEmbedding:
    """Tests for the enhanced generate_embedding method."""

    def test_empty_input_returns_empty(self, client):
        """Test that empty input returns empty vector."""
        result = client.generate_embedding(text=None, image_bytes=None)
        assert result == []

    def test_text_only_backward_compat(self, client):
        """Test backward compatibility with text-only signature."""
        mock_response = {
            "body": MagicMock(
                read=MagicMock(
                    return_value=json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode()
                )
            )
        }
        client._client = MagicMock()
        client._client.invoke_model.return_value = mock_response

        result = client.generate_embedding(text="hello world")
        assert result == [0.1, 0.2, 0.3]

    def test_image_only(self, client):
        """Test image-only embedding."""
        mock_response = {
            "body": MagicMock(
                read=MagicMock(
                    return_value=json.dumps({"embedding": [0.4, 0.5, 0.6]}).encode()
                )
            )
        }
        client._client = MagicMock()
        client._client.invoke_model.return_value = mock_response

        result = client.generate_embedding(
            image_bytes=b"fake_image",
            model_id="cohere.embed-english-v3",
        )
        assert result == [0.4, 0.5, 0.6]


class TestGenerateEmbeddingsBatch:
    """Tests for the batch embedding method."""

    def test_batch_embeddings(self, client):
        """Test batch embedding generation."""
        items = [
            {"text": "hello"},
            {"text": "world"},
            {"image_bytes": b"fake"},
        ]

        # generate_embeddings_batch runs items concurrently (ThreadPoolExecutor),
        # so an ordered side_effect list would be consumed in thread-scheduling
        # order, not item order — making the per-item assertions flaky. Map each
        # input deterministically to its embedding instead.
        embedding_by_key = {
            "hello": [1.0, 2.0],
            "world": [3.0, 4.0],
            b"fake": [5.0, 6.0],
        }

        def _embed(**kwargs):
            key = kwargs.get("text") or kwargs.get("image_bytes")
            return embedding_by_key[key]

        with patch.object(client, "generate_embedding", side_effect=_embed):
            results = client.generate_embeddings_batch(
                items=items,
                model_id="cohere.embed-english-v3",
                max_concurrent=2,
            )

        assert len(results) == 3
        assert results[0] == [1.0, 2.0]
        assert results[1] == [3.0, 4.0]
        assert results[2] == [5.0, 6.0]

    def test_batch_with_failures(self, client):
        """Test batch embedding with some failures."""
        items = [
            {"text": "good"},
            {"text": "bad"},
            {"text": "good2"},
        ]

        # Map input → result deterministically (batch runs concurrently, so an
        # ordered side_effect list is not reliable — see test_batch_embeddings).
        def _mock_embed(**kwargs):
            text = kwargs.get("text", "")
            if text == "bad":
                raise ValueError("Mock failure")
            return [1.0, 2.0] if text == "good" else [3.0, 4.0]

        with patch.object(client, "generate_embedding", side_effect=_mock_embed):
            results = client.generate_embeddings_batch(items=items, max_concurrent=1)

        assert len(results) == 3
        assert results[0] == [1.0, 2.0]
        assert results[1] is None  # Failed
        assert results[2] == [3.0, 4.0]

    def test_batch_progress_callback(self, client):
        """Test that progress callback is called."""
        items = [{"text": "a"}, {"text": "b"}]
        progress_calls = []

        with patch.object(
            client,
            "generate_embedding",
            return_value=[1.0, 2.0],
        ):
            client.generate_embeddings_batch(
                items=items,
                max_concurrent=1,
                progress_callback=lambda done, total: progress_calls.append(
                    (done, total)
                ),
            )

        assert len(progress_calls) == 2
        assert progress_calls[-1] == (2, 2)

    def test_empty_batch(self, client):
        """Test empty batch returns empty results."""
        results = client.generate_embeddings_batch(items=[])
        assert results == []
