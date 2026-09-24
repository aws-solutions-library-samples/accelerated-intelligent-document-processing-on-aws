# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One region-prefix rule, shared, and it must stay shared.

Bedrock cross-region inference profiles carry one of five geo prefixes — ``us.``,
``eu.``, ``apac.``, ``global.`` and ``us-gov.`` — and every capability gate in
``idp_common.bedrock.client`` answers by stripping that prefix and matching the base
name against a set. Four sites used to hand-roll the strip, and three of the four
listed only ``us``/``eu``/``global``.

**Omitting a prefix fails permissively**, which is why reading those copies did not
catch it. An id whose prefix is unrecognised falls through the ``else`` branch
unchanged, so it matches no base-name set, and every gate returns the answer for a
model it knows nothing about:

* ``is_claude_4_7_model("us-gov.anthropic.claude-opus-5")`` -> False, so
  ``temperature`` and ``top_p`` are sent to a model that rejects both with a 400.
* ``supports_forced_tool_choice("us-gov.anthropic.claude-opus-5-5")`` -> True, so a
  forced tool call is sent to the one model that refuses it.
* ``is_claude_effort_model`` -> False, so the only thinking control Opus 5.5 has is
  dropped.

GovCloud is exactly where those land, and not by coincidence: an account-scoped
inference-profile ARN is the only way to name a model there, and
``resolve_model_id_from_arn`` reduces such an ARN to a ``us-gov.`` id — which is then
this rule's input. So the tree's most prefix-dependent deployment was the one every
gate got wrong.

There are two definitions of the five prefixes and there cannot be one: ``config``
imports ``bedrock``, so ``bedrock.model_utils`` cannot import
``config.retired_models``. This file is the substitute for that import.
"""

from __future__ import annotations

import pytest

from idp_common.bedrock.client import (
    is_claude_4_7_model,
    is_claude_effort_model,
    strips_sampling_params,
    supports_forced_tool_choice,
    thinking_can_be_disabled,
)
from idp_common.bedrock.model_utils import REGION_PREFIXES
from idp_common.config.retired_models import _REGION_PREFIX


def test_the_two_definitions_name_the_same_five_prefixes():
    """``retired_models`` holds the rule as a regex and ``model_utils`` as a tuple.
    Neither can import the other, so assert they agree rather than hoping."""
    assert set(REGION_PREFIXES) == {"us", "eu", "apac", "global", "us-gov"}
    for prefix in REGION_PREFIXES:
        assert _REGION_PREFIX.match(f"{prefix}.anthropic.claude-opus-5"), (
            f"config.retired_models._REGION_PREFIX does not recognise '{prefix}.', "
            "which model_utils.REGION_PREFIXES does — the two have drifted."
        )


def test_us_gov_is_in_the_set_and_is_not_a_hypothetical():
    """``us-gov.`` ids are shipped in this repository, in the GovCloud preset, so
    this is not defensive coverage for a case nobody hits."""
    assert "us-gov" in REGION_PREFIXES
    assert "apac" in REGION_PREFIXES


#: One gate per capability, each answering about a model whose answer is NOT the
#: permissive default — so a prefix that fails to strip flips every one of them.
_GATES = (
    (is_claude_4_7_model, "anthropic.claude-opus-5-5", True),
    (strips_sampling_params, "anthropic.claude-opus-5-5", True),
    (is_claude_effort_model, "anthropic.claude-opus-5-5", True),
    (supports_forced_tool_choice, "anthropic.claude-opus-5-5", False),
    (thinking_can_be_disabled, "anthropic.claude-opus-5-5", False),
    (is_claude_4_7_model, "anthropic.claude-opus-5", True),
    (strips_sampling_params, "anthropic.claude-sonnet-5", True),
)


@pytest.mark.parametrize(("gate", "base", "expected"), _GATES)
@pytest.mark.parametrize("prefix", REGION_PREFIXES)
def test_every_gate_gives_the_same_answer_under_every_prefix(
    gate, base, expected, prefix
):
    """The prefix names where inference runs; it says nothing about what the model
    accepts. Any gate whose answer changes with it is reading the id wrong."""
    assert gate(f"{prefix}.{base}") is expected, (
        f"{gate.__name__}('{prefix}.{base}') should be {expected}. A prefix missing "
        "from REGION_PREFIXES fails permissively — the id keeps its prefix, matches "
        "no base name, and the gate answers as if for an unknown model."
    )


@pytest.mark.parametrize(("gate", "base", "expected"), _GATES)
@pytest.mark.parametrize(
    ("partition", "prefix"),
    [("aws", "us"), ("aws", "eu"), ("aws-us-gov", "us-gov")],
)
def test_the_answer_survives_an_inference_profile_arn(
    gate, base, expected, partition, prefix
):
    """The GovCloud row is the one that matters: an ARN is the only way to name a
    model there, so this is the shape a GovCloud config actually contains."""
    region = "us-gov-west-1" if partition == "aws-us-gov" else "us-west-2"
    arn = (
        f"arn:{partition}:bedrock:{region}:123456789012:"
        f"inference-profile/{prefix}.{base}"
    )
    assert gate(arn) is expected


@pytest.mark.parametrize("prefix", REGION_PREFIXES)
def test_the_1m_suffix_still_strips_under_every_prefix(prefix):
    """``:1m`` and the prefix are stripped by the same helper, so a change to one
    can break the other."""
    assert is_claude_4_7_model(f"{prefix}.anthropic.claude-opus-5-5:1m") is True
    assert (
        supports_forced_tool_choice(f"{prefix}.anthropic.claude-opus-5-5:1m") is False
    )


def test_a_model_outside_every_set_is_unaffected():
    """Non-vacuity in the other direction: if the gates started answering True for
    everything, every assertion above would pass for the wrong reason."""
    for prefix in REGION_PREFIXES:
        assert is_claude_4_7_model(f"{prefix}.amazon.nova-pro-v1:0") is False
        assert supports_forced_tool_choice(f"{prefix}.amazon.nova-pro-v1:0") is True


def test_an_unknown_prefix_is_not_silently_stripped():
    """The strip is an allow-list, not "take everything before the first dot". A
    vendor segment must not be mistaken for a geo prefix — ``openai.gpt-6-astra``
    would otherwise normalise to ``gpt-6-astra`` and match nothing on purpose while
    looking like it had been handled."""
    from idp_common.bedrock.model_utils import REGION_PREFIXES as prefixes

    assert "openai" not in prefixes
    assert "anthropic" not in prefixes
    assert "amazon" not in prefixes
