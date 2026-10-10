# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claude Haiku 5.5's capability answers, and why not one of them carries over.

Every fact asserted here was measured against ``us.anthropic.claude-haiku-5-5`` on
bedrock-runtime Converse in us-west-2 on 2026-10-08, not read off a model card:

======================================  =========================================
Request                                 Result
======================================  =========================================
plain Converse                          ``stopReason: end_turn``
``inferenceConfig.temperature``         rejected, "`temperature` is deprecated"
``inferenceConfig.topP``                rejected, "`top_p` is deprecated"
``additionalModelRequestFields.top_k``  rejected, "`top_k` is deprecated"
``output_config.effort`` low / max      accepted; ``max`` spent more output tokens
``thinking: {"type": "disabled"}``      **accepted**
``thinking: {"type": "adaptive"}``      accepted
``toolChoice: {"auto": {}}``            ``stopReason: tool_use``, toolUse emitted
``toolChoice: {"any": {}}``             **accepted**, toolUse emitted
``toolChoice: {"tool": {...}}``         **accepted**, toolUse emitted
explicit ``cachePoint``                 accepted; floor bracketed at (500, 520] tokens
``document`` content block (PDF)        accepted, 1,608 input tokens
one 2550x3301 image                     4,770 input tokens (high-resolution tier)
======================================  =========================================

**The family name is not a tier, and this model is where that stops being a
theoretical worry.** Every capability gate in this package that distinguishes Haiku
4.5 from Haiku 5.5 answers *differently* for the two, and in four cases the Haiku 4.5
answer is the unsafe one to inherit:

===================================  ================  ================
Property                             Haiku 4.5         Haiku 5.5
===================================  ================  ================
sampling params (temperature/top_p)  accepted          **rejected (400)**
``output_config.effort``             rejected (400)    **accepted**
minimum cacheable prefix             4,096 tokens      **512 tokens**
image token tier                     1,568 cap         **4,784 cap**
max output tokens                    64,000            **128,000**
===================================  ================  ================

So a gate that matches on a shared ``haiku`` stem, or a limits pattern that lets
``claude-haiku-5-5`` fall through to the Claude 4.x entry, is wrong in a way no
request reports: the sampling-parameter miss 400s every call, and the other four
degrade silently (no caching where caching was available, a ~3x understated image
cost, half the output budget).

**The other trap runs the other way.** Haiku 5.5 shares Opus 5.5's sampling-parameter
surface and its effort control, so "another 5.5 model" invites copying Opus 5.5's two
restrictions across. Both are measurably wrong here: thinking *can* be disabled, and a
forced ``toolChoice`` *is* accepted. Those two are asserted below against their Opus
5.5 counterparts so the pair cannot drift into agreement.

One thing deliberately not modelled, because it is a model-card statement rather than
something a response reveals: with thinking disabled the effort level is capped at
``high``. The client sends no ``thinking`` field on the Converse path, so it never
constructs that combination.
"""

from __future__ import annotations

import pytest

from idp_common.bedrock.client import (
    CACHEPOINT_SUPPORTED_MODELS,
    document_blocks_unsupported_reason,
    is_claude_4_7_model,
    is_claude_effort_model,
    strips_sampling_params,
    supports_document_blocks,
    supports_forced_tool_choice,
    supports_tool_config,
    thinking_can_be_disabled,
)
from idp_common.bedrock.model_utils import (
    get_model_max_input_tokens,
    get_model_max_output_tokens,
    visual_token_cap_for_model,
)
from idp_common.bedrock.prompt_cache import min_cacheable_prefix_tokens

BASE = "us.anthropic.claude-haiku-5-5"
ARN = (
    "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
    "us.anthropic.claude-haiku-5-5"
)
#: No ``:1m`` form, unlike every other 1M-capable Claude in this repository. The 1M
#: window is this model's default, so there is nothing to opt into and no suffix to
#: translate into an ``anthropic_beta`` header.
#: The GovCloud ARN is here because it is the form the justification in
#: ``test_haiku_5_5_accepts_reasoning_effort`` is actually about. The commercial ARN
#: above resolves to a ``us.`` id and so exercises the ordinary path; only the
#: ``aws-us-gov`` partition reduces to a ``us-gov.`` id, which is the one a GovCloud
#: configuration contains and the one a missing region prefix would break.
GOV_ARN = (
    "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/"
    "us-gov.anthropic.claude-haiku-5-5"
)
#: ``au.`` and ``jp.`` are two of this model's four advertised geo ids and are in the
#: sweep for that reason — they were missing from ``REGION_PREFIXES`` until this
#: model was added, and a missing prefix fails permissively.
ALL_FORMS = [
    BASE,
    "eu.anthropic.claude-haiku-5-5",
    "au.anthropic.claude-haiku-5-5",
    "jp.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-haiku-5-5",
    ARN,
    GOV_ARN,
]
HAIKU_45 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class TestTheFamilyNameIsNotATier:
    """Each property where the two Haikus disagree, asserted as a pair.

    Asserting the pair rather than Haiku 5.5 alone is the point: a gate widened to a
    shared ``haiku`` stem would make both sides agree, and only the second assertion
    in each method would catch it.
    """

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_haiku_5_5_rejects_sampling_parameters(self, model_id):
        """Live: each of `temperature`, `top_p` and `top_k` comes back "is
        deprecated for this model". IDP's default decoding config sets top_k=5 and
        top_p=0.0, so missing this makes every request fail rather than degrade."""
        assert is_claude_4_7_model(model_id) is True
        assert strips_sampling_params(model_id) is True

    def test_haiku_4_5_still_accepts_them(self):
        assert is_claude_4_7_model(HAIKU_45) is False
        assert strips_sampling_params(HAIKU_45) is False

    def test_the_entry_is_what_carries_the_sampling_answer(self):
        """The set is matched on the whole base name, so no other entry can cover
        this one. Pin the membership directly: if the lookup ever becomes a prefix
        match, the gate above keeps passing while the entry becomes removable."""
        from idp_common.bedrock.client import _CLAUDE_4_7_BASE_NAMES

        assert "anthropic.claude-haiku-5-5" in _CLAUDE_4_7_BASE_NAMES

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_haiku_5_5_accepts_reasoning_effort(self, model_id):
        """The first Haiku that does. The ARN form is in the sweep because an
        account-scoped inference-profile ARN is the only way to name a model in
        GovCloud, and effort would be dropped there without ARN resolution."""
        assert is_claude_effort_model(model_id) is True

    def test_haiku_4_5_rejects_reasoning_effort(self):
        """It 400s on it, which is why the Haiku family is split across the two
        tests rather than sharing one answer."""
        assert is_claude_effort_model(HAIKU_45) is False

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_the_minimum_cacheable_prefix_is_512(self, model_id):
        assert min_cacheable_prefix_tokens(model_id) == 512

    def test_haiku_4_5_sits_at_the_opposite_end_of_the_table(self):
        """4,096 is the largest tier in ``_MIN_PREFIX_TIERS`` and 512 the smallest,
        so the two Haikus bracket it. A shared stem would give Haiku 5.5 an
        8x-too-high minimum and warn that classes which do cache will not."""
        assert min_cacheable_prefix_tokens(HAIKU_45) == 4096

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_the_vision_tier_is_high_resolution(self, model_id):
        """Measured: one 2550x3301 page costs 4,770 real input tokens here against
        ~1,542 on Haiku 4.5, with Sonnet 5 at 4,768 for the same image."""
        assert visual_token_cap_for_model(model_id) == 4784

    def test_haiku_4_5_is_standard_resolution(self):
        """Understating an image-heavy request by ~3x reads as "plenty of context
        left" right up to the rejection, so this pair must not collapse."""
        assert visual_token_cap_for_model(HAIKU_45) == 1568

    def test_the_output_budget_is_not_the_claude_4_x_one(self):
        """Falling through to the ``claude-(opus|sonnet|haiku)-4`` limits pattern
        would halve this to 64,000."""
        assert get_model_max_output_tokens(BASE) == 128000
        assert get_model_max_output_tokens(HAIKU_45) == 64000


class TestTheOtherFiveFiveModelIsNotAGuide:
    """Opus 5.5's two restrictions do NOT carry over, and both were measured."""

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_thinking_can_still_be_disabled(self, model_id):
        """Live: ``thinking = {"type": "disabled"}`` returned ``end_turn``. Opus 5.5
        rejects the same request at every effort level."""
        assert thinking_can_be_disabled(model_id) is True

    def test_opus_5_5_is_the_one_that_cannot(self):
        assert thinking_can_be_disabled("us.anthropic.claude-opus-5-5") is False

    def test_it_is_absent_from_the_thinking_always_on_set(self):
        """``thinking_can_be_disabled`` is a DENYLIST gate: it answers True for any
        id it knows nothing about, so the assertion above passes whether the decision
        was made or merely never considered. Pin the registry, the way
        ``test_it_is_absent_from_the_forcing_denylist`` does."""
        from idp_common.bedrock.client import _THINKING_ALWAYS_ON_BASE_NAMES

        assert "anthropic.claude-haiku-5-5" not in _THINKING_ALWAYS_ON_BASE_NAMES
        assert "anthropic.claude-opus-5-5" in _THINKING_ALWAYS_ON_BASE_NAMES

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_a_forced_toolchoice_is_accepted(self, model_id):
        """Live: both ``{"any": {}}`` and ``{"tool": {"name": ...}}`` returned
        ``stopReason: tool_use`` with a toolUse block. So a forced-tool extraction
        config is valid on this model and must not fall back to the prose schema."""
        assert supports_tool_config(model_id) is True
        assert supports_forced_tool_choice(model_id) is True

    def test_it_is_absent_from_the_forcing_denylist(self):
        """``forced_tool_choice_unsupported_reason`` returns None for every model it
        knows nothing about, so the gate above passes whether the decision was made
        or merely never considered. Pin the registry itself."""
        from idp_common.bedrock.client import FORCED_TOOL_CHOICE_UNSUPPORTED

        assert "anthropic.claude-haiku-5-5" not in FORCED_TOOL_CHOICE_UNSUPPORTED
        assert "anthropic.claude-opus-5-5" in FORCED_TOOL_CHOICE_UNSUPPORTED

    def test_the_forced_tool_path_actually_forces(self):
        """The user-visible half of the assertion above: a forced-tool config on
        this model proceeds rather than degrading with a reason."""
        from idp_common.extraction.forced_tool import should_force_tool

        force, reason = should_force_tool(
            BASE, True, {"type": "object", "properties": {"a": {"type": "string"}}}
        )
        assert force is True
        assert not reason


class TestCachingAndWindows:
    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_explicit_cachepoints_are_supported(self, prefix):
        """Live, and bracketed rather than merely consistent.

        A large prefix caching proves only that caching works somewhere above the
        claimed floor: 4,683 tokens would cache under a 512 minimum, a 1,024 one and
        Haiku 4.5's own 4,096 alike, so it cannot distinguish any of them. The floor
        was therefore walked: a 500-token system prefix wrote nothing and read
        nothing on the next two calls, while a 520-token prefix wrote 520 and read
        520 back twice (us.anthropic.claude-haiku-5-5, us-west-2, 2026-10-08). That
        brackets it at (500, 520], which is the published 512.
        """
        assert f"{prefix}.anthropic.claude-haiku-5-5" in CACHEPOINT_SUPPORTED_MODELS

    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_no_1m_variant_is_listed(self, prefix):
        """``us.anthropic.claude-haiku-5-5:1m`` is not a thing to support: 1M is the
        default window here, so there is no suffix for ``invoke_model`` to translate
        into an ``anthropic_beta`` header. Listing one would offer a selectable id
        that resolves to a header this model never needed."""
        suffixed = f"{prefix}.anthropic.claude-haiku-5-5:1m"
        assert suffixed not in CACHEPOINT_SUPPORTED_MODELS

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_document_blocks_are_accepted(self, model_id):
        """Live: a PDF ``document`` content block was accepted (1,608 input tokens),
        so this model is usable for whole-PDF extraction and discovery."""
        assert supports_document_blocks(model_id) is True

    def test_it_is_absent_from_the_document_block_denylist(self):
        """Denylist gate again — see the thinking test above for why True alone is a
        weak assertion. The routes that cannot take a document block are the
        bedrock-mantle Responses API, Grok and Astra; a Converse Claude is none of
        them."""
        from idp_common.bedrock.client import DOCUMENT_BLOCK_UNSUPPORTED_ROUTES

        assert not any("haiku" in route for route in DOCUMENT_BLOCK_UNSUPPORTED_ROUTES)
        assert document_blocks_unsupported_reason(BASE) is None

    def test_the_window_is_stated_truthfully(self):
        """1M is this model's default window and the limits file says so.

        ⚠️ That is a deliberate choice with a documented cost consequence, not an
        oversight: the long-context band starts at 100,000 input tokens and bills 5x
        on every token type, ``pricing.yaml`` holds one flat rate per model, so a
        request above 100,000 is UNDER-REPORTED by 5x. Capping this field was tried
        and rejected — it is the same shape as GPT-6 Astra, which this repository
        already handles by stating the window and documenting the gap, and no cap
        both keeps the model usable and keeps every request in the cheap band:
        measured, 100,000 leaves a 2,000-token agentic shard budget against 18,400
        for Sonnet 5. Read the notes on this model's entries in
        ``model_config_limits.yaml`` and ``pricing.yaml`` together.
        """
        assert get_model_max_input_tokens(BASE) == 1000000

    def test_it_is_sized_like_a_1m_model_rather_than_a_200k_one(self):
        """The consequence of the line above, asserted where a reader will look.

        ``max_input_tokens`` is not only a statement of fact — it is the number
        auto-sizing, summarization's fit-or-truncate budget and the user-visible
        overflow message all read. A cap would have truncated a summarization payload
        at 85% of the capped figure while telling nobody (the truncation flag is
        discarded by its caller), and reported the wrong window in the overflow
        message.
        """
        from idp_common.bedrock.sizing import compute_sizing_plan

        plan = compute_sizing_plan(model_id=BASE)
        sonnet5 = compute_sizing_plan(model_id="us.anthropic.claude-sonnet-5")
        assert plan.shard_token_budget > sonnet5.shard_token_budget


class TestPricing:
    @staticmethod
    def _units(name):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[5]
        data = yaml.safe_load((root / "config_library" / "pricing.yaml").read_text())
        for entry in data["pricing"]:
            if entry["name"] == name:
                return {u["name"]: float(u["price"]) for u in entry["units"]}
        raise AssertionError(f"no pricing entry for {name}")

    @pytest.mark.parametrize(
        "name",
        [
            "bedrock/us.anthropic.claude-haiku-5-5",
            "bedrock/eu.anthropic.claude-haiku-5-5",
        ],
    )
    def test_the_regional_rates(self, name):
        """The US/EU geo band, which carries the usual 10% premium over global."""
        units = self._units(name)
        assert units["inputTokens"] == 1.1e-7
        assert units["outputTokens"] == 5.5e-7
        assert units["cacheReadInputTokens"] == 1.1e-8  # 0.1x input
        assert units["cacheWriteInputTokens"] == 1.375e-7  # 1.25x input, exactly

    def test_the_global_rates(self):
        units = self._units("bedrock/global.anthropic.claude-haiku-5-5")
        assert units["inputTokens"] == 1.0e-7
        assert units["outputTokens"] == 5.0e-7
        assert units["cacheReadInputTokens"] == 1.0e-8
        assert units["cacheWriteInputTokens"] == 1.25e-7

    def test_it_is_ten_times_cheaper_than_haiku_4_5_per_token(self):
        """Pin the ratio, not just the rates. The launch announcement says "around
        75% less ... for most tasks", which is a per-TASK estimate that absorbs this
        model's reasoning tokens; the per-token rate is 90% lower. Both numbers get
        quoted, and a reader who assumes they are the same claim will misread a
        benchmark cost delta in either direction."""
        new = self._units("bedrock/us.anthropic.claude-haiku-5-5")
        old = self._units(f"bedrock/{HAIKU_45}")
        assert old["inputTokens"] / new["inputTokens"] == pytest.approx(10.0)
        assert old["outputTokens"] / new["outputTokens"] == pytest.approx(10.0)

    def test_no_1m_pricing_entry_exists(self):
        """There is no ``:1m`` id for this model, so an entry would be a rate keyed
        on something unreachable — and ``test_model_surface_consistency`` reads this
        file as the set of ids configuration validation accepts."""
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[5]
        data = yaml.safe_load((root / "config_library" / "pricing.yaml").read_text())
        names = {entry["name"] for entry in data["pricing"]}
        for prefix in ("us", "eu", "global"):
            assert f"bedrock/{prefix}.anthropic.claude-haiku-5-5:1m" not in names
