# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780 item 2: cache efficiency is read back out of the metering map and
classified as caching / write-only / never-cached / disabled / no-cache-data."""

import pytest

from idp_common.bedrock.prompt_cache import (
    cache_state,
    describe_cache_state,
    model_caches_implicitly,
    model_supports_cache_point,
    summarize_cache_usage,
)

pytestmark = pytest.mark.unit

SONNET = "us.anthropic.claude-sonnet-4-6"


def _key(context="Extraction", model=SONNET):
    return f"{context}/bedrock/{model}"


def test_reads_landing_is_caching_with_a_read_share():
    s = summarize_cache_usage(
        {
            _key(): {
                "inputTokens": 200,
                "cacheReadInputTokens": 800,
                "cacheWriteInputTokens": 0,
                "outputTokens": 50,
                "requests": 2,
            }
        }
    )
    assert s["state"] == "caching"
    assert s["read_share"] == 0.8
    assert s["requests"] == 2
    assert s["model_ids"] == [SONNET]
    assert s["min_cacheable_prefix_tokens"] == 1024
    assert "80% of input read from cache" in describe_cache_state(s)


def test_writes_without_reads_is_write_only():
    s = summarize_cache_usage(
        {
            _key(): {
                "inputTokens": 9,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 1628,
                "requests": 1,
            }
        }
    )
    assert s["state"] == "write-only"
    assert "1.25x" in describe_cache_state(s)


def test_zero_reads_and_writes_is_never_cached_and_names_the_minimum():
    s = summarize_cache_usage(
        {
            _key(): {
                "inputTokens": 949,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
                "requests": 1,
            }
        }
    )
    assert s["state"] == "never-cached"
    text = describe_cache_state(s)
    assert "inert" in text and "1,024 tokens" in text and SONNET in text


def test_off_by_configuration_is_reported_as_disabled_not_inert():
    units = {"inputTokens": 949, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    assert summarize_cache_usage({_key(): units}, disabled=True)["state"] == "disabled"
    # ...but measured caching wins over the flag: if tokens were cached, they were.
    units["cacheReadInputTokens"] = 5
    assert summarize_cache_usage({_key(): units}, disabled=True)["state"] == "caching"


def test_backend_without_cache_units_concludes_nothing():
    s = summarize_cache_usage({_key(): {"inputTokens": 500, "outputTokens": 20}})
    assert s["state"] == "no-cache-data"
    assert s["read_share"] == 0.0
    assert s["requests"] is None


def test_escalation_contexts_count_toward_extraction_and_other_phases_do_not():
    metering = {
        _key(): {
            "inputTokens": 100,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 900,
        },
        _key("ExtractionEscalation", "us.anthropic.claude-opus-4-8"): {
            "inputTokens": 50,
            "cacheReadInputTokens": 900,
            "cacheWriteInputTokens": 0,
        },
        _key("Classification"): {"inputTokens": 1, "cacheReadInputTokens": 99999},
        "OCR/textract/analyze_document": {"pages": 3},
    }
    s = summarize_cache_usage(metering)
    assert s["cache_read_input_tokens"] == 900
    assert s["cache_write_input_tokens"] == 900
    assert s["input_tokens"] == 150
    assert s["model_ids"] == ["us.anthropic.claude-opus-4-8", SONNET]
    assert s["state"] == "caching"


def test_no_bedrock_metering_in_the_phase_returns_none():
    assert (
        summarize_cache_usage({"OCR/textract/analyze_document": {"pages": 3}}) is None
    )
    assert summarize_cache_usage({}) is None


@pytest.mark.parametrize(
    "read,write,has_units,disabled,expected",
    [
        (1, 0, True, False, "caching"),
        (0, 1, True, False, "write-only"),
        (0, 0, True, False, "never-cached"),
        (0, 0, True, True, "disabled"),
        (0, 0, False, False, "no-cache-data"),
        (0, 0, False, True, "disabled"),
    ],
)
def test_state_table(read, write, has_units, disabled, expected):
    assert (
        cache_state(read, write, has_cache_units=has_units, disabled=disabled)
        == expected
    )


def test_zero_zero_with_no_cache_point_sent_is_not_called_inert():
    """Claude returns cacheReadInputTokens: 0 with no cache point in the request, so
    a marker-less prompt or an unsupported model must not read as 'inert'."""
    units = {"inputTokens": 949, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    s = summarize_cache_usage({_key(): units}, cache_point_sent=False)
    assert s["state"] == "no-cache-point"
    assert "no cache point reached the model" in describe_cache_state(s)
    # unknown (inference profile) falls back to the evidence in the usage block
    assert (
        summarize_cache_usage({_key(): units}, cache_point_sent=None)["state"]
        == "never-cached"
    )
    # 'off' still wins the naming: zero/zero is the intended outcome
    assert (
        summarize_cache_usage({_key(): units}, disabled=True, cache_point_sent=False)[
            "state"
        ]
        == "disabled"
    )
    # ...and measured caching beats everything
    units["cacheWriteInputTokens"] = 7
    assert (
        summarize_cache_usage({_key(): units}, cache_point_sent=False)["state"]
        == "write-only"
    )


ASTRA = "us.openai.gpt-6-astra"


@pytest.mark.parametrize(
    "model_id",
    [
        ASTRA,
        "global.openai.gpt-6-astra",
        "openai.gpt-6-astra",
        "openai.gpt-5.4",
        "openai.gpt-5.5",
        "openai.gpt-5.6-sol",
        "arn:aws:bedrock:us-west-2:1:inference-profile/us.openai.gpt-6-astra",
    ],
)
def test_implicit_cache_models_are_recognized(model_id):
    """These cache WITHOUT a Converse cachePoint, so `no cache point sent` must not
    be reported as `this model cannot cache`."""
    assert model_caches_implicitly(model_id) is True
    # ...and none of them is sent a cachePoint block.
    if "inference-profile" not in model_id:
        assert model_supports_cache_point(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        SONNET,
        "us.amazon.nova-lite-v1:0",
        # Grok's model card advertises implicit caching but it was never observed
        # to engage, so no caching benefit is claimed for it.
        "us.xai.grok-4.6",
        None,
        "",
    ],
)
def test_other_models_do_not_claim_implicit_caching(model_id):
    assert model_caches_implicitly(model_id) is False


def test_no_cache_point_on_an_implicit_model_does_not_claim_it_cannot_cache():
    """The regression this guards: the message said "the model does not support
    prompt caching", which is FALSE for Astra (it caches implicitly and rejects an
    explicit cache point) and for every GPT-5.x model."""
    units = {"inputTokens": 949, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    s = summarize_cache_usage({_key(model=ASTRA): units}, cache_point_sent=False)
    assert s["state"] == "no-cache-point"
    text = describe_cache_state(s)
    assert "does not support prompt caching" not in text
    assert "caches implicitly" in text

    # A Claude model with no marker keeps the original meaning.
    claude = summarize_cache_usage({_key(): units}, cache_point_sent=False)
    claude_text = describe_cache_state(claude)
    assert "no cache point reached the model" in claude_text
    assert "caches implicitly" not in claude_text
    assert "does not support prompt caching" not in claude_text


def test_measured_reads_still_win_for_an_implicit_model():
    """Astra's whole point: once a prefix repeats, reads land and the state must be
    plain `caching` — verified live at inputTokens=2 / cacheRead=2707."""
    s = summarize_cache_usage(
        {
            _key(model=ASTRA): {
                "inputTokens": 2,
                "cacheReadInputTokens": 2707,
                "cacheWriteInputTokens": 0,
            }
        },
        cache_point_sent=False,
    )
    assert s["state"] == "caching"
    assert "caching" in describe_cache_state(s)


def test_mixed_models_fall_back_to_the_generic_wording():
    """One implicit + one cachePoint model in the same phase: the implicit-only
    sentence would be wrong, so the generic (still accurate) wording is used."""
    units = {"inputTokens": 100, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    s = summarize_cache_usage(
        {_key(model=ASTRA): units, _key(model=SONNET): dict(units)},
        cache_point_sent=False,
    )
    assert s["state"] == "no-cache-point"
    assert "caches implicitly" not in describe_cache_state(s)


def test_model_support_mirrors_the_client_list_and_leaves_profiles_unknown():
    assert model_supports_cache_point(SONNET) is True
    assert model_supports_cache_point("us.amazon.nova-lite-v1:0") is True  # Nova caches
    assert model_supports_cache_point("xai.grok-4") is False
    assert model_supports_cache_point("us.openai.gpt-6-astra") is False
    assert (
        model_supports_cache_point(
            "arn:aws:bedrock:us-west-2:1:application-inference-profile/abc"
        )
        is None
    )
    assert model_supports_cache_point(None) is None
