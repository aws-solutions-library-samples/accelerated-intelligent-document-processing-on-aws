# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780: per-model minimum cacheable prefix, and the chars/4 prefix estimate the
config validator uses, checked against Bedrock's own token counts measured in
benchmarks/results/v0.6.7/prompt-cache/prefix_survey_32_classes.json."""

from __future__ import annotations

import json
import os

import pytest
import yaml

from idp_common.bedrock.prompt_cache import (
    estimate_prefix_tokens,
    min_cacheable_prefix_tokens,
    schema_prose,
)

pytestmark = pytest.mark.unit

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 5))
SURVEY = os.path.join(
    REPO,
    "benchmarks",
    "results",
    "v0.6.7",
    "prompt-cache",
    "prefix_survey_32_classes.json",
)


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("us.anthropic.claude-opus-5", 512),
        ("us.anthropic.claude-opus-5:1m", 512),
        ("us.anthropic.claude-fable-5", 512),
        ("us.anthropic.claude-sonnet-5", 1024),
        ("eu.anthropic.claude-sonnet-5:1m", 1024),
        ("us.anthropic.claude-sonnet-4-6", 1024),
        ("us.anthropic.claude-opus-4-8", 1024),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 1024),
        ("us.anthropic.claude-sonnet-4-20250514-v1:0", 1024),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", 1024),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", 1024),
        ("us.anthropic.claude-opus-4-7", 2048),
        ("us.anthropic.claude-opus-4-6-v1", 4096),
        ("us.anthropic.claude-opus-4-5-20251101-v1:0", 4096),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 4096),
        # Nova's measured minimum (<=355) is below any shipped class: no warning.
        ("us.amazon.nova-pro-v1:0", None),
        ("us.amazon.nova-2-lite-v1:0", None),
        ("openai.gpt-5.6", None),
        ("LambdaHook", None),
        (None, None),
    ],
)
def test_minimum_cacheable_prefix_is_not_monotonic_across_generations(
    model_id, expected
):
    assert min_cacheable_prefix_tokens(model_id) == expected


def test_newer_is_not_safer():
    """The trap the docs call out: Opus 4.6 needs 8x what Opus 5 does."""
    assert min_cacheable_prefix_tokens("us.anthropic.claude-opus-4-6-v1") == 4096
    assert min_cacheable_prefix_tokens("us.anthropic.claude-opus-4-7") == 2048
    assert min_cacheable_prefix_tokens("us.anthropic.claude-opus-4-8") == 1024
    assert min_cacheable_prefix_tokens("us.anthropic.claude-opus-5") == 512


def test_schema_prose_strips_idp_extensions_only():
    schema = {
        "type": "object",
        "x-aws-idp-document-type": "Invoice",
        "properties": {
            "Total": {
                "type": "number",
                "description": "d",
                "x-aws-idp-evaluation-method": "EXACT",
            }
        },
    }
    prose = schema_prose(schema)
    assert "x-aws-idp" not in prose
    assert '"description": "d"' in prose and '"type": "number"' in prose


def test_no_marker_means_nothing_would_cache():
    assert (
        estimate_prefix_tokens("sys", "no marker here {DOCUMENT_TEXT}", {}, "X") is None
    )


def test_estimate_counts_only_the_span_before_the_first_marker():
    task = (
        "HEAD {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} <<CACHEPOINT>> TAIL "
        + "x" * 4000
        + " <<CACHEPOINT>> more"
    )
    est = estimate_prefix_tokens("", task, {"type": "object"}, "C")
    assert est is not None and est < 100  # the 4,000-char tail is after the marker


def _survey_rows():
    if not os.path.exists(SURVEY):
        pytest.skip("survey results not present")
    data = json.load(open(SURVEY))
    assert data["forced_tool"] is False and data["pad_chars"] == 0
    return {(r["preset"], r["class"]): r["prefix_tokens"] for r in data["rows"]}


def _preset_estimates(preset: str):
    from idp_common.config.merge_utils import merge_config_with_defaults

    path = os.path.join(REPO, "config_library", "unified", preset, "config.yaml")
    with open(path) as fh:
        cfg = merge_config_with_defaults(yaml.safe_load(fh), validate=False)
    ex = cfg["extraction"]
    out = {}
    for c in cfg.get("classes") or []:
        cid = str(c.get("$id") or c.get("x-aws-idp-document-type"))
        out[cid] = estimate_prefix_tokens(
            ex.get("system_prompt") or "", ex.get("task_prompt") or "", c, cid
        )
    return out


SURVEYED_PRESETS = [
    "lending-package-sample",
    "ocr-benchmark",
    "bank-statement-sample",
    "realkie-fcc-verified",
    "rvl-cdip",
]

# MAINTENANCE NOTE: these tests compare a LIVE estimate from the current
# config_library presets and the current base-extraction.yaml prompts against a
# survey frozen at v0.6.7. A failure here after editing a preset's descriptions or
# the shared extraction prompt means "re-run benchmarks/harness/cache_prefix_survey.py
# and refresh prefix_survey_32_classes.json", not that the estimator broke.


@pytest.mark.parametrize("preset", SURVEYED_PRESETS)
def test_estimate_is_within_ten_percent_of_bedrocks_count(preset):
    """chars/4 against Bedrock's own token count for every surveyed class (32 across
    five presets); the estimate must land within 10% on all of them, or the warning
    would name the wrong classes."""
    measured = _survey_rows()
    estimates = _preset_estimates(preset)
    checked = 0
    for (p, cid), tokens in measured.items():
        if p != preset or cid not in estimates:
            continue
        est = estimates[cid]
        assert est is not None
        assert abs(est - tokens) / tokens <= 0.10, (
            f"{preset}:{cid} est={est} measured={tokens}"
        )
        checked += 1
    assert checked >= 1, f"no surveyed classes matched preset {preset}"


def test_every_class_the_survey_found_under_the_sonnet_minimum_gets_flagged():
    """The validator flags a class as "never" below the minimum and as "may not
    cache" within the estimate's 10% band above it. Every surveyed class that is
    really under the 1,024 minimum (GLOSSARY 949, SHIFT_SCHEDULE 1000, Bank-checks
    941, rvl-cdip specification 1021, news_article 967) must land in one of those
    two buckets, and every class measured clear of the band must be flagged by
    neither — across all five surveyed presets, not a convenient subset."""
    from idp_common.bedrock.prompt_cache import ESTIMATE_TOLERANCE

    measured = _survey_rows()
    minimum = min_cacheable_prefix_tokens("us.anthropic.claude-sonnet-4-6")
    assert minimum == 1024
    band = minimum * (1 + ESTIMATE_TOLERANCE)
    flagged_under = 0
    for preset in SURVEYED_PRESETS:
        estimates = _preset_estimates(preset)
        for (p, cid), tokens in measured.items():
            if p != preset or cid not in estimates:
                continue
            est = estimates[cid]
            if tokens < minimum:
                assert est < band, (
                    f"{preset}:{cid} under the minimum ({tokens}) but estimated {est}"
                )
                flagged_under += 1
            elif tokens >= band:
                assert est >= minimum, (
                    f"{preset}:{cid} clear of the band ({tokens}) but estimated {est}"
                )
    assert flagged_under >= 5
