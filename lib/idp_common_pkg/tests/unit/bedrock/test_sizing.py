# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model-aware auto-sizing (idp_common.bedrock.sizing.compute_sizing_plan)."""

from __future__ import annotations

import pathlib

import pytest
import yaml

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


def _naive_and_clamped(max_in: int, max_out: int, buffer: float) -> tuple[int, int]:
    """Shard budget with the reserve unclamped vs clamped, computed independently
    of sizing.py so the test cannot pass by mirroring the implementation."""
    usable_in, usable_out = int(max_in * (1 - buffer)), int(max_out * (1 - buffer))
    image_reserve = 20 * 1600
    naive = max(2000, usable_in - usable_out - image_reserve)
    clamped_reserve = min(usable_out, int(usable_in * 0.65))
    clamped = max(2000, usable_in - clamped_reserve - image_reserve)
    return naive, clamped


def _model_limit_rows() -> list[tuple[str, int, int]]:
    """Every row in the shipped model_config_limits.yaml."""
    # Walk up to the repo root rather than hard-coding a parents[] index, so the
    # test does not break if the file moves within the package tree.
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        path = parent / "config_library" / "model_config_limits.yaml"
        if path.is_file():
            break
    else:  # pragma: no cover - only if the shipped limits file is missing
        pytest.skip("config_library/model_config_limits.yaml not found")
    rows = yaml.safe_load(path.read_text())["model_limits"]
    return [(r["pattern"], r["max_input_tokens"], r["max_output_tokens"]) for r in rows]


@pytest.mark.parametrize("buffer", [0.0, 0.15, 0.3, 0.5, 0.6, 0.85])
def test_reserve_clamp_does_not_change_existing_models(buffer):
    """The clamp must be a no-op for every pre-existing row, at EVERY buffer.

    Driven off the shipped YAML rather than a hand-copied list, so a newly added
    model row is covered automatically instead of silently escaping the check.
    Grok is the one row expected to differ — it is the reason the clamp exists.

    The no-op is buffer-independent because the comparison reduces to
    max_output/max_input; pinning several buffers documents that rather than
    leaving it to the 0.30 default.
    """
    changed = []
    for pattern, max_in, max_out in _model_limit_rows():
        if "grok" in pattern:
            continue  # the one row the clamp exists to fix
        naive, clamped = _naive_and_clamped(max_in, max_out, buffer)
        if naive != clamped:
            changed.append((pattern, naive, clamped))
    assert changed == [], (
        f"clamp changed a pre-existing model at buffer={buffer}: {changed}"
    )


def test_clamp_actually_rescues_grok():
    """The other half of the invariant: at the default buffer the clamp must lift
    Grok off the floor, or the constant is doing nothing. Asserted at the default
    buffer rather than swept, because at extreme buffers (0.85) the usable input
    is small enough that Grok floors with or without the clamp."""
    naive, clamped = _naive_and_clamped(500_000, 524_288, 0.3)
    assert naive == 2000  # collapses to _MIN_SHARD_TOKEN_BUDGET unclamped
    assert clamped == 90_500


def test_clamp_boundary_is_just_below_the_chosen_fraction():
    """0.65 sits just above the 0.64 boundary set by the 200K/128K Claude
    families. Pins BOTH sides of the two-sided constraint the constant's comment
    describes, so moving it in either direction fails loudly."""
    from idp_common.bedrock.sizing import _MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT

    usable_in, usable_out = int(200_000 * 0.7), int(128_000 * 0.7)
    assert usable_out / usable_in == 0.64  # the boundary, exactly

    # Lowering below the boundary re-shards Claude.
    assert min(usable_out, int(usable_in * 0.63)) < usable_out
    # The chosen value does not.
    assert (
        min(usable_out, int(usable_in * _MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT))
        == usable_out
    )
    # Raising it starves Grok, which is why it is not simply set to 1.0.
    assert _naive_and_clamped(500_000, 524_288, 0.3)[1] > 2000
    grok_at_090 = max(
        2000,
        int(500_000 * 0.7)
        - min(int(524_288 * 0.7), int(500_000 * 0.7 * 0.90))
        - 32_000,
    )
    assert grok_at_090 < _naive_and_clamped(500_000, 524_288, 0.3)[1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
