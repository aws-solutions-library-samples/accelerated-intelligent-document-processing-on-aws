# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Long-context pricing of Claude Haiku 5.5 in the benchmark cost figures.

pricing.yaml has one rate per unit, but Haiku 5.5 bills a whole request at five times
the standard rates when its prompt (input plus cache reads and writes) is more than
100,000 tokens. Metering is per document and step, so collect_guidance_results.py sizes
each step's requests from its totals and the configuration's call layout. These pin the
layouts against the classification service's own rules (the window split sizes are the
ones its log lines report), the pricing of a request over the threshold, and that the
renderer's run-level cost adds the same surcharge.
"""

from __future__ import annotations

import math

import pytest
from harness_import import harness_module

cgr = harness_module("collect_guidance_results")
rg = harness_module("render_guidance")

HAIKU = "Classification/bedrock/us.anthropic.claude-haiku-5-5"
SID = "bedrock/us.anthropic.claude-haiku-5-5"
HOLISTIC = {"classification": {"combined": {"granularity": "packet"}}}


def standard(units: dict) -> float:
    return sum(
        float(units.get(u) or 0) * rg.unit_price(SID, u) for u in cgr.TOKEN_UNITS
    )


@pytest.mark.parametrize("n", range(1, 81))
def test_windows_follow_the_formula(n):
    expected = 1 if n <= 20 else math.ceil((n - 2) / (20 - 2))
    assert len(cgr.packet_windows(n)) == expected
    assert cgr.packet_windows(40) == [20, 20, 4]


def test_a_refused_window_splits_as_the_service_logs_it():
    # "Packet window 1/1 (pages 1-19) was refused as too large for one request;
    # retrying as windows of 9 and 12 pages" (ClassificationFunction, SC-13).
    assert cgr.split_window(19, 2) == [9, 12]
    assert cgr.split_window(20, 2) == [10, 12]
    assert cgr.split_window(18, 2) == [9, 11]


def test_one_request_is_exact_and_extra_requests_split_the_largest_window():
    layout = cgr.request_layout("Classification", HOLISTIC, 40, None)
    assert layout.pages == (20, 20, 4)
    assert cgr.request_prompts(120_000, 1, layout) == ([120_000], "exact")
    prompts, method = cgr.request_prompts(440_000, 3, layout)
    assert method == "windows" and prompts == [200_000, 200_000, 40_000]
    # Four requests for three windows: one 20-page window was retried as 10 + 12.
    prompts, method = cgr.request_prompts(460_000, 4, layout)
    assert method == "windows"
    assert sorted(prompts) == [40_000, 100_000, 120_000, 200_000]


def test_sections_split_by_pages_and_other_counts_split_evenly():
    layout = cgr.request_layout("Extraction", {}, 20, [17, 2, 1])
    assert cgr.request_prompts(200_000, 3, layout)[0] == [170_000, 20_000, 10_000]
    assert cgr.request_prompts(200_000, 4, layout) == ([50_000] * 4, "even")
    first_k = {
        "classification": {
            "split_then_classify": {
                "section_classification": {"pages": "first_k", "k": 2}
            }
        }
    }
    assert cgr.request_layout(
        "SectionClassification", first_k, 20, [17, 2, 1]
    ).pages == (
        2,
        2,
        1,
    )


def test_agentic_extraction_and_its_confidence_step_are_not_sized():
    agentic = {"extraction": {"mode": "advanced", "agentic": {"enabled": True}}}
    for step in ("Extraction", "Assessment"):
        layout = cgr.request_layout(step, agentic, 10, [10])
        assert cgr.request_prompts(500_000, 9, layout) == (None, "unsized")
        assert cgr.request_prompts(500_000, 0, layout) == (None, "unsized")


def test_a_request_over_the_threshold_pays_the_multiplier_on_every_unit():
    # SC-13 packet_0001.pdf, holistic Haiku 5.5: one call, as its log line reports.
    units = {
        "inputTokens": 110_570,
        "cacheWriteInputTokens": 1_656,
        "cacheReadInputTokens": 0,
        "outputTokens": 3_226,
        "requests": 1,
    }
    (step,) = cgr.long_context_steps(
        {HAIKU: units}, lambda s: cgr.request_layout(s, HOLISTIC, 20, None)
    )
    assert (step.method, step.requests, step.long_requests) == ("exact", 1, 1)
    assert step.surcharge == pytest.approx(4 * standard(units))


def test_cache_reads_count_toward_the_prompt_and_the_threshold_is_exclusive():
    def surcharge(units):
        (step,) = cgr.long_context_steps(
            {HAIKU: {**units, "requests": 1}}, lambda s: cgr.Layout("even")
        )
        return step

    assert surcharge(
        {"inputTokens": 50_000, "cacheReadInputTokens": 60_000}
    ).long_requests
    at = surcharge({"inputTokens": 99_000, "cacheWriteInputTokens": 1_000})
    assert (at.method, at.long_requests, at.surcharge) == ("under", 0, 0.0)


def test_only_models_with_a_rule_are_priced():
    metering = {
        "Classification/bedrock/us.anthropic.claude-sonnet-5-5": {
            "inputTokens": 500_000,
            "requests": 1,
        },
        "OCR/textract/analyze_document-Layout": {"pages": 20},
    }
    assert cgr.long_context_steps(metering, lambda s: cgr.Layout("even")) == []
    assert "anthropic.claude-haiku-5-5" in cgr.LONG_CONTEXT_RULES


def test_an_unsized_step_records_its_ceiling_and_is_counted():
    units = {"inputTokens": 300_000, "outputTokens": 10_000}
    (step,) = cgr.long_context_steps(
        {"Extraction/bedrock/us.anthropic.claude-haiku-5-5": units},
        lambda s: cgr.Layout("loop"),
    )
    assert (step.method, step.surcharge) == ("unsized", 0.0)
    assert step.ceiling == pytest.approx(4 * standard(units))
    tally = cgr.LongContextTally()
    tally.add([step])
    assert tally.total["unsized_documents"] == 1 and tally.total["requests"] == 0


def test_the_renderer_adds_the_surcharge_on_the_cost_basis():
    usage = {
        "k": [{f"{SID}|inputTokens": 1_000_000}, {f"{SID}|inputTokens": 1_000_000}]
    }
    base = 1_000_000 * rg.unit_price(SID, "inputTokens") * 1000
    costs, _, _ = rg.price_usage(
        usage, {"k": {"mode": "cold-run", "index": 1}}, {"k": [0.5, 0.25]}
    )
    assert costs["k"]["mean"] == pytest.approx(base + 250)
    costs, _, _ = rg.price_usage(usage, {}, {})
    assert costs["k"]["mean"] == pytest.approx(base)
    results = {"long_context": {"k": {"surcharge": [0.5, 0.25]}}}
    assert rg.long_context_surcharges(results) == {"k": [0.5, 0.25]}
    with pytest.raises(SystemExit):
        rg.price_usage(usage, {}, {"k": [0.5]})
