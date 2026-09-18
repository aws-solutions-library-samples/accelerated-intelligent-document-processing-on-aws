# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock's dimension cap tightens when a request carries MANY images (#994).

The reported bug: a 29-page extraction request whose pages were each rendered at
2,550 px — individually legal under the 8,000 px per-image cap — was rejected
because a request with more than 20 image blocks caps EVERY image in it at
2,000 px per side. Nothing that inspects one image at a time can see this, so the
enforcement has to happen where the whole request is visible, and it has to count
tool-result images and document blocks too. The same failure was additionally
misreported as a context-window overflow, which sent users tuning page budgets
that cannot fix it.

Images here are kept just over the cap (2,048 px, the value the discovery agent
used to hardcode) rather than at a realistic 2,550: what is being tested is
which images get clamped and which do not, and a LANCZOS pass over 21 full-size
pages costs seconds of suite time for no extra coverage.
"""

from __future__ import annotations

import functools
import io
import os

import pytest
from PIL import Image

from idp_common import image as image_mod
from idp_common.image import (
    BEDROCK_IMAGE_MAX_DIMENSION,
    BEDROCK_MANY_IMAGE_COUNT_THRESHOLD,
    BEDROCK_MANY_IMAGE_MAX_DIMENSION,
    fit_images_in_request,
    max_dimension_for_image_count,
)

pytestmark = pytest.mark.unit

# Just over BEDROCK_MANY_IMAGE_MAX_DIMENSION, and well under the 8,000 px cap,
# so a clamp can only be attributed to the many-image rule.
OVERSIZE = (1585, 2048)
UNDERSIZE = (800, 1035)


@functools.lru_cache(maxsize=8)
def _page_png(width: int, height: int) -> bytes:
    """A compressible page-like PNG (flat white), so byte size is never the
    binding constraint. Cached: the same bytes can back many blocks."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _image_block(size: tuple[int, int] = OVERSIZE, fmt: str = "png") -> dict:
    return {"image": {"format": fmt, "source": {"bytes": _page_png(*size)}}}


def _messages(*blocks: dict) -> list:
    return [{"role": "user", "content": list(blocks)}]


def _dimensions(block: dict) -> tuple[int, int]:
    with Image.open(io.BytesIO(block["image"]["source"]["bytes"])) as img:
        return img.size


def _many_image_warnings(caplog) -> list[str]:
    """Only the aggregate request-level warning, not any per-image line."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and "many-image threshold" in r.getMessage()
    ]


class TestMaxDimensionForImageCount:
    """The threshold is ">20", not ">=2": clamping earlier throws away
    resolution Bedrock would have accepted."""

    def test_at_threshold_keeps_the_normal_cap(self):
        assert (
            max_dimension_for_image_count(BEDROCK_MANY_IMAGE_COUNT_THRESHOLD)
            == BEDROCK_IMAGE_MAX_DIMENSION
        )

    def test_one_over_threshold_tightens(self):
        assert (
            max_dimension_for_image_count(BEDROCK_MANY_IMAGE_COUNT_THRESHOLD + 1)
            == BEDROCK_MANY_IMAGE_MAX_DIMENSION
        )

    @pytest.mark.parametrize("count", [0, 1, 2, 5, 19, 20])
    def test_small_requests_keep_full_resolution(self, count):
        assert max_dimension_for_image_count(count) == BEDROCK_IMAGE_MAX_DIMENSION


class TestFitImagesInRequest:
    def test_twenty_large_images_are_left_untouched(self):
        """Exactly at the threshold the tighter cap does not apply, and the sweep
        must not touch bytes it has no reason to touch."""
        blocks = [_image_block() for _ in range(20)]
        originals = [b["image"]["source"]["bytes"] for b in blocks]
        assert fit_images_in_request(_messages(*blocks)) == 0
        assert [b["image"]["source"]["bytes"] for b in blocks] == originals

    def test_twenty_one_large_images_are_all_clamped(self):
        blocks = [_image_block() for _ in range(21)]
        assert fit_images_in_request(_messages(*blocks)) == 21
        for b in blocks:
            assert max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION

    def test_clamp_preserves_aspect_ratio(self):
        blocks = [_image_block(UNDERSIZE) for _ in range(20)]
        big = _image_block(OVERSIZE)
        assert fit_images_in_request(_messages(*blocks, big)) == 1
        w, h = _dimensions(big)
        assert abs((w / h) - (OVERSIZE[0] / OVERSIZE[1])) < 0.02

    def test_already_small_images_are_not_re_encoded(self):
        """A page already inside the tighter cap must pass through byte for
        byte — a needless re-encode costs quality and time."""
        blocks = [_image_block(UNDERSIZE) for _ in range(25)]
        originals = [b["image"]["source"]["bytes"] for b in blocks]
        assert fit_images_in_request(_messages(*blocks)) == 0
        assert [b["image"]["source"]["bytes"] for b in blocks] == originals

    def test_mixed_request_clamps_only_the_oversized(self):
        big = [_image_block(OVERSIZE) for _ in range(3)]
        small = [_image_block(UNDERSIZE) for _ in range(18)]
        assert fit_images_in_request(_messages(*big, *small)) == 3
        assert all(max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for b in big)
        assert all(_dimensions(b) == UNDERSIZE for b in small)

    def test_tool_result_images_count_toward_the_threshold(self):
        """The agentic ``view_image`` tool returns a page inside a toolResult.
        Those images count, so 15 attached + 6 returned is 21, not 15 — and the
        returned ones are themselves clamped."""
        attached = [_image_block(UNDERSIZE) for _ in range(15)]
        returned = [_image_block(OVERSIZE) for _ in range(6)]
        messages = [
            {"role": "user", "content": list(attached)},
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "t1", "content": list(returned)}}
                ],
            },
        ]
        assert fit_images_in_request(messages) == 6
        assert all(
            max(_dimensions(b)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for b in returned
        )

    def test_tool_result_images_alone_do_not_false_trigger(self):
        """Below the threshold, a tool-result image keeps full resolution."""
        returned = [_image_block(OVERSIZE) for _ in range(3)]
        messages = [
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "t1", "content": list(returned)}}
                ],
            }
        ]
        assert fit_images_in_request(messages) == 0
        assert all(_dimensions(b) == OVERSIZE for b in returned)

    def test_document_blocks_count_toward_the_threshold(self):
        """On Bedrock a document block counts the same as an image, so 18 images
        plus 3 documents crosses the threshold even though only 18 are images."""
        images = [_image_block(OVERSIZE) for _ in range(18)]
        docs = [
            {"document": {"format": "pdf", "name": f"d{i}", "source": {"bytes": b"x"}}}
            for i in range(3)
        ]
        assert fit_images_in_request(_messages(*images, *docs)) == 18

    def test_declared_format_is_refreshed_when_a_fit_changes_it(self):
        """An incompressible image only fits as JPEG, so the block's declared
        ``format`` must be updated — Bedrock rejects a format/bytes mismatch."""
        width, height = 2048, 2048
        noisy_png = io.BytesIO()
        Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(
            noisy_png, format="PNG"
        )
        noisy = {"image": {"format": "png", "source": {"bytes": noisy_png.getvalue()}}}
        filler = [_image_block(UNDERSIZE) for _ in range(20)]
        assert fit_images_in_request(_messages(noisy, *filler)) == 1
        declared = noisy["image"]["format"]
        assert declared == image_mod.bedrock_image_format(
            noisy["image"]["source"]["bytes"]
        )

    def test_s3_location_sources_are_skipped_not_crashed(self):
        """Bedrock fetches an s3Location image itself; there are no bytes here to
        resize, and the sweep must not raise over it."""
        blocks = [_image_block(OVERSIZE) for _ in range(21)]
        blocks.append(
            {"image": {"format": "png", "source": {"s3Location": {"uri": "s3://b/k"}}}}
        )
        assert fit_images_in_request(_messages(*blocks)) == 21

    def test_unreadable_bytes_are_left_alone_without_raising(self):
        """Best-effort: an image the fit cannot process is sent unchanged rather
        than failing a request Bedrock might still accept."""
        blocks = [_image_block(UNDERSIZE) for _ in range(20)]
        blocks.append(
            {"image": {"format": "png", "source": {"bytes": b"not-an-image"}}}
        )
        assert fit_images_in_request(_messages(*blocks)) == 0
        assert blocks[-1]["image"]["source"]["bytes"] == b"not-an-image"

    def test_warns_once_with_the_actionable_remedy(self, caplog):
        """One aggregate line, not one per image: the per-image warning is
        suppressed by the sweep precisely so this stays readable."""
        blocks = [_image_block(OVERSIZE) for _ in range(21)]
        with caplog.at_level("WARNING", logger="idp_common.image"):
            fit_images_in_request(_messages(*blocks))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "target_width" in msg
        assert str(BEDROCK_MANY_IMAGE_MAX_DIMENSION) in msg
        assert "21" in msg

    def test_no_warning_when_nothing_was_clamped(self, caplog):
        blocks = [_image_block(UNDERSIZE) for _ in range(25)]
        with caplog.at_level("WARNING", logger="idp_common.image"):
            fit_images_in_request(_messages(*blocks))
        assert _many_image_warnings(caplog) == []

    @pytest.mark.parametrize(
        "messages", [None, [], [{}], [{"content": None}], [{"content": ["junk"]}]]
    )
    def test_degenerate_inputs_are_no_ops(self, messages):
        assert fit_images_in_request(messages) == 0
