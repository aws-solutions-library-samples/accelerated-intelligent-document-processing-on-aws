# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Image token estimates are patch-based and CAPPED by the model's tier (#994).

Claude bills an image as ceil(w/28) x ceil(h/28) visual tokens, but only after
downscaling it to the model's resolution tier — which puts a hard ceiling on what
one image can cost: 1,568 tokens on models before Claude 4.7, 4,784 on the
high-resolution tier. The library's older figure, (w*h)/750, had no ceiling and
over-stated a 2550x3301 page by 2.4x (11,223 vs a measured 4,761 real input
tokens on us.anthropic.claude-sonnet-5). That is not just cosmetic: it is why an
image-heavy request that Bedrock rejected on per-image DIMENSIONS was explained
to users as a context-window overflow.
"""

from __future__ import annotations

import math

import pytest

from idp_common.bedrock.model_utils import (
    _HIGH_RES_VISUAL_TOKEN_CAP,
    _STANDARD_VISUAL_TOKEN_CAP,
    estimate_image_tokens,
    visual_token_cap_for_model,
)

pytestmark = pytest.mark.unit


class TestVisualTokenCapForModel:
    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-opus-4-7",
            "us.anthropic.claude-opus-4-8",
            "us.anthropic.claude-opus-5",
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-sonnet-5:1m",
        ],
    )
    def test_high_resolution_tier(self, model_id):
        assert visual_token_cap_for_model(model_id) == _HIGH_RES_VISUAL_TOKEN_CAP

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-3-haiku-20240307-v1:0",
            "us.anthropic.claude-opus-4-5",
            None,
        ],
    )
    def test_standard_tier(self, model_id):
        assert visual_token_cap_for_model(model_id) == _STANDARD_VISUAL_TOKEN_CAP


class TestEstimateImageTokens:
    def test_a_small_image_is_priced_by_patches(self):
        """Below the cap the patch count itself is the estimate."""
        expected = math.ceil(280 / 28) * math.ceil(560 / 28)  # 10 * 20 = 200
        assert (
            estimate_image_tokens(280, 560, "us.anthropic.claude-sonnet-5") == expected
        )

    def test_a_full_page_scan_is_capped_by_the_tier(self):
        """A 2550x3301 page is 92*118 = 10,856 raw patches; the tier cap is what
        Bedrock actually charges, and the measured figure was 4,761."""
        assert (
            estimate_image_tokens(2550, 3301, "us.anthropic.claude-sonnet-5")
            == _HIGH_RES_VISUAL_TOKEN_CAP
        )
        assert (
            estimate_image_tokens(2550, 3301, "us.anthropic.claude-sonnet-4-6")
            == _STANDARD_VISUAL_TOKEN_CAP
        )

    def test_the_estimate_is_within_ten_percent_of_the_measured_cost(self):
        """Regression anchor for the one figure measured live: 4,761 real input
        tokens for a 2550x3301 page on us.anthropic.claude-sonnet-5. The old
        estimate returned 11,223 and would fail this."""
        est = estimate_image_tokens(2550, 3301, "us.anthropic.claude-sonnet-5")
        assert abs(est - 4761) / 4761 < 0.10

    def test_non_claude_families_keep_the_legacy_figure(self):
        """Nova budgets images by payload size, not patches, and there is no
        published patch equivalent — over-stating is the safe direction for a
        warning message, so the generous legacy number stays."""
        assert estimate_image_tokens(1500, 3000, "us.amazon.nova-pro-v1:0") == 6000
        assert estimate_image_tokens(1500, 3000, None) == 6000

    @pytest.mark.parametrize("width,height", [(0, 100), (100, 0), (-1, -1)])
    def test_degenerate_dimensions_never_return_zero_or_negative(self, width, height):
        assert estimate_image_tokens(width, height, "us.anthropic.claude-sonnet-5") == 1
