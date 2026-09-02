# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model-aware auto-sizing (idp_common.bedrock.sizing.compute_sizing_plan)."""

from __future__ import annotations

import pytest

from idp_common.bedrock.sizing import compute_sizing_plan

NOVA_LITE = "us.amazon.nova-lite-v1:0"
SONNET5 = "us.anthropic.claude-sonnet-5"
SONNET5_1M = "us.anthropic.claude-sonnet-5:1m"


def test_larger_input_window_gives_larger_shard_budget():
    """A 1M-context model gets a much larger shard token budget than a 200K one."""
    base = compute_sizing_plan(model_id=SONNET5, context_buffer=0.3)
    big = compute_sizing_plan(model_id=SONNET5_1M, context_buffer=0.3)
    assert big.shard_token_budget > base.shard_token_budget
    assert big.max_input_tokens == 1_000_000
    assert base.max_input_tokens == 200_000


def test_context_buffer_reduces_budgets():
    """A larger context buffer leaves less usable window → smaller budgets."""
    low = compute_sizing_plan(model_id=SONNET5_1M, context_buffer=0.15)
    high = compute_sizing_plan(model_id=SONNET5_1M, context_buffer=0.6)
    assert high.shard_token_budget < low.shard_token_budget
    assert high.list_batch_size <= low.list_batch_size


def test_bbox_geometry_shrinks_list_batch():
    """Per-row output is larger with bbox geometry → smaller list batch.

    Use a high context buffer so the derived sizes land below the reliability
    cap (otherwise both clamp to the cap and the geometry effect is hidden)."""
    ocr = compute_sizing_plan(
        model_id=NOVA_LITE, geometry_mode="ocr_only", context_buffer=0.85
    )
    bbox = compute_sizing_plan(
        model_id=NOVA_LITE, geometry_mode="llm_grounded", context_buffer=0.85
    )
    assert bbox.list_batch_size < ocr.list_batch_size


def test_list_batch_capped_for_reliability():
    """Even a huge-output model does not batch more than the reliability cap."""
    plan = compute_sizing_plan(model_id=SONNET5_1M, geometry_mode="ocr_only")
    assert plan.list_batch_size <= 50


def test_unknown_model_falls_back_conservatively():
    """An unknown model still yields a sane (non-crashing) plan that shards."""
    plan = compute_sizing_plan(model_id="some.unknown.model-v9:0")
    assert plan.shard_token_budget >= 2000
    assert plan.list_batch_size >= 1


def test_none_model_uses_fallback():
    plan = compute_sizing_plan(model_id=None)
    assert plan.max_input_tokens > 0
    assert plan.list_batch_size >= 1


def test_overrides_short_circuit_derivation():
    """Explicit overrides win over auto-derivation and are recorded."""
    plan = compute_sizing_plan(
        model_id=SONNET5_1M,
        shard_token_budget_override=9999,
        max_pages_per_shard_override=3,
        list_batch_size_override=7,
    )
    assert plan.shard_token_budget == 9999
    assert plan.max_pages_per_shard == 3
    assert plan.list_batch_size == 7
    assert plan.overrides == {
        "shard_token_budget": 9999,  # nosec B105 - token budget, not a secret
        "max_pages_per_shard": 3,
        "list_batch_size": 7,
    }


def test_image_reserve_scales_with_max_images():
    """More attached images reserve more input tokens (less for OCR text)."""
    few = compute_sizing_plan(model_id=SONNET5, max_images_per_agent=2)
    many = compute_sizing_plan(model_id=SONNET5, max_images_per_agent=20)
    assert many.image_reserve_tokens > few.image_reserve_tokens
    assert many.shard_token_budget < few.shard_token_budget


def test_plan_to_dict_round_trips_key_fields():
    plan = compute_sizing_plan(model_id=SONNET5, context_buffer=0.3)
    d = plan.to_dict()
    assert d["model_id"] == SONNET5
    assert d["context_buffer"] == pytest.approx(0.3)
    assert "shard_token_budget" in d and "list_batch_size" in d


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Output-reserve clamp (_MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT)
# ---------------------------------------------------------------------------
# The input budget reserves room for the model's response by subtracting the
# usable output window. xAI Grok 4.6 is the first model whose output cap
# (524,288) rivals its context window (500,000), which made that subtraction go
# negative and collapse the shard budget to the floor. The reserve is therefore
# capped at a fraction of the usable input — and that fraction was chosen to
# leave every pre-existing model's derived budget untouched.

GROK = "us.xai.grok-4.6"


def test_grok_shard_budget_is_not_starved_by_its_own_output_cap():
    """Without the reserve clamp this collapses to _MIN_SHARD_TOKEN_BUDGET."""
    plan = compute_sizing_plan(model_id=GROK, context_buffer=0.3)
    assert plan.max_input_tokens == 500_000
    assert plan.max_output_tokens == 524_288
    # Naive math: 350,000 usable in - 367,001 reserve - 32,000 images < 0.
    assert plan.output_reserve_tokens < plan.max_output_tokens
    assert plan.shard_token_budget == 90_500


def test_grok_gets_a_larger_shard_budget_than_a_200k_model():
    """The model with the biggest context window must not shard the smallest."""
    grok = compute_sizing_plan(model_id=GROK, context_buffer=0.3)
    sonnet = compute_sizing_plan(model_id=SONNET5, context_buffer=0.3)
    assert grok.shard_token_budget > sonnet.shard_token_budget


@pytest.mark.parametrize(
    ("model_id", "expected_shard_budget"),
    [
        # Every pre-existing row in model_config_limits.yaml. These numbers are
        # the pre-clamp values: the clamp MUST be a no-op for all of them. The
        # binding case is the 200K/128K Claude families, whose usable output
        # (91,750) is 65.5% of their usable input (140,000) — raising the clamp
        # fraction above 0.65 would silently re-shard them.
        (SONNET5, 18_400),  # 200K in / 128K out
        ("us.anthropic.claude-opus-4-8", 18_400),
        ("us.anthropic.claude-opus-5", 18_400),
        ("us.anthropic.claude-sonnet-4-6", 18_400),
        (SONNET5_1M, 578_400),  # 1M in / 128K out
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 63_200),  # 200K / 64K
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 102_266),  # 200K / 8K
        ("openai.gpt-5.6-sol", 68_800),  # 272K / 128K
        (NOVA_LITE, 171_000),  # 300K / 10K
    ],
)
def test_reserve_clamp_does_not_change_existing_models(model_id, expected_shard_budget):
    plan = compute_sizing_plan(model_id=model_id, context_buffer=0.3)
    assert plan.shard_token_budget == expected_shard_budget
    # No clamp engaged: the reserve is still the full usable output window.
    assert plan.output_reserve_tokens == int(plan.max_output_tokens * 0.7)
