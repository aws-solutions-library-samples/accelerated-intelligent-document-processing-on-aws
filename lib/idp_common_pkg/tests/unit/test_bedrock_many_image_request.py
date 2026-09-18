# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Converse request is swept for Bedrock's many-image dimension cap (#994).

``BedrockClient.invoke_model`` is the only place in the library that sees a whole
request just before it goes to Bedrock, which is exactly what the cap needs: it
binds on the number of image blocks in the request, so no per-image guard can
enforce it. The sweep therefore has to live here and cover every stage that
attaches page images — classification, assessment, summarization, evaluation,
few-shot examples — not only extraction, which has its own earlier clamp.

These tests assert on what reaches the boto3 ``converse`` call, since that is the
payload Bedrock judges.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from PIL import Image

from idp_common.bedrock.client import BedrockClient
from idp_common.image import BEDROCK_MANY_IMAGE_MAX_DIMENSION

pytestmark = pytest.mark.unit

# Just over the many-image cap, far under the 8,000 px single-image cap, so a
# downscale can only be attributed to the many-image rule.
OVERSIZE = (1585, 2048)


def _page_png(size: tuple[int, int] = OVERSIZE) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _content(image_count: int) -> list[dict]:
    blocks: list[dict] = [{"text": "extract this"}]
    png = _page_png()
    blocks.extend(
        {"image": {"format": "png", "source": {"bytes": png}}}
        for _ in range(image_count)
    )
    return blocks


def _sent_images(client: BedrockClient) -> list[dict]:
    messages = client._client.converse.call_args.kwargs["messages"]
    return [
        block["image"]
        for message in messages
        for block in message.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("image"), dict)
    ]


def _dimensions(image_block: dict) -> tuple[int, int]:
    with Image.open(io.BytesIO(image_block["source"]["bytes"])) as img:
        return img.size


@pytest.fixture
def client() -> BedrockClient:
    c = BedrockClient(region="us-west-2", metrics_enabled=False)
    c._client = MagicMock()
    c._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    }
    return c


class TestInvokeModelAppliesTheManyImageCap:
    def test_twenty_one_images_are_downscaled_before_the_call(self, client):
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=_content(21),
        )
        sent = _sent_images(client)
        assert len(sent) == 21
        assert all(
            max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for b in sent
        )

    def test_twenty_images_go_out_at_full_resolution(self, client):
        """The cap applies above 20, so at 20 the pages must not be degraded."""
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=_content(20),
        )
        sent = _sent_images(client)
        assert len(sent) == 20
        assert all(_dimensions(b) == OVERSIZE for b in sent)

    def test_a_text_only_request_is_untouched(self, client):
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=[{"text": "no images here"}],
        )
        assert _sent_images(client) == []

    def test_the_sweep_never_fails_the_request(self, client, monkeypatch):
        """The guard is best-effort: if fitting raises for any reason the request
        still goes to Bedrock, which may well accept it."""
        from idp_common import image as image_mod

        def boom(_messages):
            raise RuntimeError("pillow exploded")

        monkeypatch.setattr(image_mod, "fit_images_in_request", boom)
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=_content(21),
        )
        assert client._client.converse.called
        assert all(_dimensions(b) == OVERSIZE for b in _sent_images(client))

    def test_the_callers_own_content_list_is_not_rewritten(self, client):
        """When no ``<<CACHEPOINT>>`` tag is present ``invoke_model`` puts the
        caller's OWN list into ``messages``, so fitting in place would permanently
        downscale whatever the caller holds — a cached few-shot example image, or a
        page-image list a later pass reuses where the tighter cap does not apply.
        The sweep works on a copy of the request spine instead."""
        content = _content(21)
        originals = [b["image"]["source"]["bytes"] for b in content if "image" in b]
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=content,
        )
        assert [b["image"]["source"]["bytes"] for b in content if "image" in b] == (
            originals
        )
        assert all(
            _dimensions(b) == OVERSIZE
            for b in (c["image"] for c in content if "image" in c)
        )
        # ...and the request that actually went out IS downscaled.
        assert all(
            max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION
            for b in _sent_images(client)
        )

    def test_a_request_under_the_threshold_copies_nothing(self, client):
        """The copy is only worth paying for when the cap binds, so the count is
        taken first — and at or under the threshold the caller's own objects must
        reach ``converse`` unchanged, not a duplicate of them."""
        content = _content(20)
        client.invoke_model(
            model_id="us.anthropic.claude-sonnet-5",
            system_prompt="sys",
            content=content,
        )
        sent = client._client.converse.call_args.kwargs["messages"][0]["content"]
        assert sent is content

    def test_a_non_claude_model_is_clamped_too(self, client):
        """Deliberate, and the conservative direction. The 2,000 px many-image cap
        is measured on Claude; whether Nova / Grok / Astra enforce one is not
        established either way. 2,000 px is legal on every family, so clamping
        cannot cause a rejection that would not otherwise happen, whereas NOT
        clamping risks a hard failure on a family that does enforce it. The cost is
        some resolution on a >20-image non-Claude request."""
        client.invoke_model(
            model_id="us.amazon.nova-pro-v1:0",
            system_prompt="sys",
            content=_content(21),
        )
        assert all(
            max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION
            for b in _sent_images(client)
        )
