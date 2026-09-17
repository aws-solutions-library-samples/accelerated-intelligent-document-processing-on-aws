# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the per-agent page-image cap (oversized-request guard)."""

import io

import pytest

from idp_common.extraction.service import ExtractionService

pytestmark = pytest.mark.unit


def _service(cap: int) -> ExtractionService:
    return ExtractionService(
        region="us-west-2",
        config={
            "extraction": {"agentic": {"enabled": True, "max_images_per_agent": cap}},
            "classes": [],
        },
    )


def _imgs(n: int) -> list[bytes]:
    return [f"img{i}".encode() for i in range(n)]


class TestCapAgentImages:
    def test_caps_when_over_limit(self):
        svc = _service(cap=20)
        out = svc._cap_agent_images(_imgs(25))
        assert len(out) == 20
        assert out == _imgs(25)[:20]  # keeps the first N, in order

    def test_no_cap_when_under_limit(self):
        svc = _service(cap=20)
        imgs = _imgs(5)
        assert svc._cap_agent_images(imgs) is imgs  # unchanged, same object

    def test_zero_means_unlimited(self):
        svc = _service(cap=0)
        imgs = _imgs(50)
        assert svc._cap_agent_images(imgs) is imgs

    def test_empty_list(self):
        svc = _service(cap=20)
        assert svc._cap_agent_images([]) == []

    def test_default_cap_is_twenty(self):
        # Config default should be 20 (the documented backstop).
        svc = ExtractionService(
            region="us-west-2",
            config={"extraction": {"agentic": {"enabled": True}}, "classes": []},
        )
        assert svc.config.extraction.agentic.max_images_per_agent == 20
        assert len(svc._cap_agent_images(_imgs(25))) == 20


def _png() -> bytes:
    """A 1x1 PNG — the attach path decodes page images with PIL."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


PROMPT_WITH_IMAGE = "Text:\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}\nEnd."


def _rendered_content(n_images: int) -> list[dict]:
    """Prompt content as the service renders it for a {DOCUMENT_IMAGE} template.

    This is the shape the agentic branch actually receives: the substitution has
    already turned the placeholder into image blocks.
    """
    svc = ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "task_prompt": PROMPT_WITH_IMAGE,
                "agentic": {"enabled": True},
            },
            "classes": [
                {"$id": "Doc", "type": "object", "properties": {"x": {"type": "s"}}}
            ],
        },
    )
    svc._class_label = "Doc"
    svc._class_schema = {"$id": "Doc", "properties": {}}
    svc._attribute_descriptions = "x: a field"
    svc._document_text = "hello"
    imgs = [_png() for _ in range(n_images)]
    svc._page_images = imgs
    return svc._build_prompt_content(PROMPT_WITH_IMAGE, imgs)


def _n_image_blocks(blocks) -> int:
    n = 0
    for b in blocks:
        if isinstance(b, dict):
            n += 1 if b.get("image") else 0
        else:
            n += 1 if getattr(b, "image", None) else 0
    return n


class TestLimitContentImages:
    """The cap and `lazy_images` must reach the PROMPT CONTENT.

    `{DOCUMENT_IMAGE}` is substituted before the agentic branch runs, so both
    knobs used to be applied to a `page_images=` argument that was a *second*
    copy of images already sitting in the content: the cap capped nothing (25
    images went out against a default cap of 20 — the oversized first turn the
    cap exists to prevent), `lazy_images` suppressed nothing, and with
    suppression off every page image was attached twice.
    """

    def test_limit_zero_removes_every_image(self):
        content = _rendered_content(6)
        assert _n_image_blocks(content) == 6
        out, kept, dropped = ExtractionService._limit_content_images(content, 0)
        assert (kept, dropped) == (0, 6)
        assert _n_image_blocks(out) == 0
        # Text blocks survive — only the images go.
        assert any(b.get("text") for b in out)

    def test_cap_trims_to_limit_keeping_order(self):
        content = _rendered_content(25)
        out, kept, dropped = ExtractionService._limit_content_images(content, 20)
        assert (kept, dropped) == (20, 5)
        assert _n_image_blocks(out) == 20
        original = [b for b in content if b.get("image")][:20]
        assert [b for b in out if b.get("image")] == original

    def test_none_limit_is_unlimited(self):
        content = _rendered_content(4)
        out, _, dropped = ExtractionService._limit_content_images(content, None)
        assert out is content and dropped == 0

    def test_non_list_content_passes_through(self):
        out, kept, dropped = ExtractionService._limit_content_images("plain text", 0)
        assert (out, kept, dropped) == ("plain text", 0, 0)


class TestNoDoubleAttachment:
    """`page_images` is the view_image pool, not a second attachment."""

    def test_attach_false_does_not_duplicate(self):
        from idp_common.extraction.agentic_idp import _prepare_prompt_content

        content = _rendered_content(3)
        imgs = [_png() for _ in range(3)]
        out = _prepare_prompt_content(
            prompt={"role": "user", "content": content},
            page_images=imgs,
            existing_data=None,
            model_id="us.anthropic.claude-sonnet-5",
            attach_page_images=False,
        )
        assert _n_image_blocks(out) == 3

    def test_attach_true_still_appends(self):
        # The historical behaviour is preserved for callers whose prompt carries
        # no images of its own (the default remains attach_page_images=True).
        from idp_common.extraction.agentic_idp import _prepare_prompt_content

        out = _prepare_prompt_content(
            prompt="just text",
            page_images=[_png(), _png()],
            existing_data=None,
            model_id="us.anthropic.claude-sonnet-5",
        )
        assert _n_image_blocks(out) == 2
