# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claude Sonnet 5.5's capability answers, and the two that Sonnet 5 gets wrong.

Every fact asserted here was measured against ``us.anthropic.claude-sonnet-5-5`` on
bedrock-runtime Converse in us-east-1 on 2026-10-14, not read off a model card:

==========================================  =====================================
Request                                     Result
==========================================  =====================================
plain Converse                              ``stopReason: end_turn``
``inferenceConfig.temperature``             rejected, "`temperature` is deprecated"
``inferenceConfig.topP``                    rejected, "`top_p` is deprecated"
``additionalModelRequestFields.top_k``      rejected, "`top_k` is deprecated"
``inferenceConfig.maxTokens`` 128001        rejected, "model limit of 128000"
``inferenceConfig.maxTokens`` 128000        accepted
``output_config.effort`` low..max           all five accepted; ``max`` spent more
``thinking: {"type": "disabled"}``          **rejected** — see below
``thinking: {"type": "between_tools"}``     **accepted**
``thinking: {"type": "adaptive"}``          accepted
``toolChoice: {"auto": {}}``                ``stopReason: tool_use``, toolUse emitted
``toolChoice: {"any": {}}``                 **rejected** (400)
``toolChoice: {"tool": {...}}``             **rejected** (400)
explicit ``cachePoint``                     accepted; floor bracketed at (508, 513]
``serviceTier: {"type": "default"}``        accepted
``serviceTier`` priority / flex             rejected, "service tier is not supported"
``document`` content block (PDF)            accepted, 9,368 input tokens
one 2550x3301 image                         4,772 input tokens (high-resolution)
215k-word text body                         accepted at 430,027 input tokens
``us.anthropic.claude-sonnet-5-5:1m``       **not a model id** — "The provided
                                            model identifier is invalid"
==========================================  =====================================

**The point-five is not a point release, and this model is the clearest case of it
so far: it disagrees with Sonnet 5 on four of the properties this package gates
on, and in two of those the Sonnet 5 answer is the unsafe one to inherit.**

===================================  ================  =========================
Property                             Sonnet 5          Sonnet 5.5
===================================  ================  =========================
sampling params (temperature/top_p)  rejected (400)    rejected (400)  *(same)*
``output_config.effort``             accepted          accepted        *(same)*
forced ``toolChoice`` any/tool       **accepted**      **rejected (400)**
minimum cacheable prefix             1,024 tokens      **512 tokens**
context window                       200K base, 1M     **1M, no ``:1m`` id**
                                     via ``:1m``
``thinking`` off payload             ``disabled``      **``between_tools``**
===================================  ================  =========================

Two of those are silent and two are not. The forcing miss **400s** every forced-tool
extraction and every multi-document-discovery cluster that needs Strands' forced
retry; the ``thinking`` payload 400s a caller that disables thinking. The other two
degrade quietly: a 1,024-token cache minimum makes the config validator warn that
512-1,024 token classes will not cache when they do, and a 200K window would size
shards from the wrong number with no ``:1m`` id to recover through.

**And the inheritance runs the other way too.** Sonnet 5.5 shares Opus 5.5's
forcing rejection, which invites copying Opus 5.5's *other* restriction across —
thinking being un-disableable at any effort. That one is measurably wrong here:
thinking can be turned off, with a different payload. Opus 5.5's and Haiku 5.5's
counterparts are asserted alongside so the three cannot drift into agreement.

One thing deliberately not modelled, because it is a model-card statement rather
than something a response reveals: this model's default effort is ``high`` — the
ordinary default, and NOT the ``medium`` that Opus 5.5 and Haiku 5.5 carry. Converse
reports no effort back, so a default cannot be measured; set effort explicitly if it
matters to a comparison.
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
    thinking_disable_payload,
)
from idp_common.bedrock.model_utils import (
    get_model_max_input_tokens,
    get_model_max_output_tokens,
    visual_token_cap_for_model,
)
from idp_common.bedrock.prompt_cache import min_cacheable_prefix_tokens

pytestmark = pytest.mark.unit

BASE = "us.anthropic.claude-sonnet-5-5"
ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
    "us.anthropic.claude-sonnet-5-5"
)
#: The GovCloud ARN is here because it is the form the region-prefix argument is
#: actually about. The commercial ARN above resolves to a ``us.`` id and so exercises
#: the ordinary path; only the ``aws-us-gov`` partition reduces to a ``us-gov.`` id,
#: which is the one a GovCloud configuration contains and the one a missing region
#: prefix would break — permissively, in both directions at once on this model.
GOV_ARN = (
    "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/"
    "us-gov.anthropic.claude-sonnet-5-5"
)
#: ``au.`` is one of this model's three advertised geo ids (``us.``, ``eu.``, ``au.``,
#: plus ``global.``) and is in the sweep for that reason: it is not reachable from the
#: picklists, which offer ``us.``/``eu.``/``global.`` only, but it is reachable through
#: a custom configuration or the CLI, where a user copies the id off the model card.
#:
#: ⚠️ What that buys is the CAPABILITY GATES below — sampling parameters, effort,
#: forcing, thinking, document blocks, the vision tier. Prompt caching is a separate
#: question with a different answer, and the answer is the same for every model in
#: this repository: ``CACHEPOINT_SUPPORTED_MODELS`` holds ``us.``/``eu.``/``global.``
#: forms only, so an ``au.``-prefixed id configured by the route above loses explicit
#: caching with no error and no log line. That is repo-wide and predates this model,
#: so widening the seeding loop over ``_REGION_PREFIXES`` is its own change; the
#: cachePoint tests here sweep ``us``/``eu``/``global`` to match what ships.
ALL_FORMS = [
    BASE,
    "eu.anthropic.claude-sonnet-5-5",
    "au.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-5-5",
    ARN,
    GOV_ARN,
]
SONNET_5 = "us.anthropic.claude-sonnet-5"
OPUS_5_5 = "us.anthropic.claude-opus-5-5"
HAIKU_5_5 = "us.anthropic.claude-haiku-5-5"


class TestTheVersionNumberIsNotACapability:
    """Each property where the two Sonnets disagree, asserted as a pair.

    Asserting the pair rather than Sonnet 5.5 alone is the point: ``claude-sonnet-5``
    is a SUBSTRING of ``claude-sonnet-5-5``, so a gate widened to match the shorter
    id would make both sides agree and only the second assertion in each method
    would catch it.
    """

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_sonnet_5_5_rejects_sampling_parameters(self, model_id):
        """Live: each of `temperature`, `top_p` and `top_k` comes back "is
        deprecated for this model". IDP's default decoding config sets top_k=5 and
        top_p=0.0, so missing this makes every request fail rather than degrade —
        which is what issue #1455 reported."""
        assert is_claude_4_7_model(model_id) is True
        assert strips_sampling_params(model_id) is True

    def test_sonnet_5_rejects_them_too(self):
        """The one property where the two Sonnets agree, and the reason the entry is
        still required: ``_CLAUDE_4_7_BASE_NAMES`` is matched on the WHOLE base name,
        so Sonnet 5's membership does nothing for Sonnet 5.5."""
        assert is_claude_4_7_model(SONNET_5) is True
        assert strips_sampling_params(SONNET_5) is True

    def test_the_entry_is_what_carries_the_sampling_answer(self):
        """Pin the membership directly. If the lookup ever became a prefix match the
        gate above would keep passing while the entry became removable — and the next
        Sonnet 5.x that *does* accept sampling parameters would then inherit a strip
        it never asked for."""
        from idp_common.bedrock.client import _CLAUDE_4_7_BASE_NAMES

        assert "anthropic.claude-sonnet-5-5" in _CLAUDE_4_7_BASE_NAMES
        assert "anthropic.claude-sonnet-5" in _CLAUDE_4_7_BASE_NAMES

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_sonnet_5_5_accepts_reasoning_effort(self, model_id):
        """Live at all five levels (low/medium/high/xhigh/max), with ``max``
        measurably spending more output tokens on the same prompt."""
        assert is_claude_effort_model(model_id) is True

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_a_forced_toolchoice_is_refused(self, model_id):
        """The headline difference from Sonnet 5, and a 400 rather than a degradation.

        Live: ``auto`` answered with ``stopReason: tool_use`` and a toolUse block,
        while ``{"any": {}}`` and ``{"tool": {"name": ...}}`` both returned
        'tool_choice: type "tool" and "any" are not supported for this model.' So the
        model is tool-CAPABLE and forcing-INCAPABLE, which is why those are two gates.
        """
        assert supports_tool_config(model_id) is True
        assert supports_forced_tool_choice(model_id) is False

    def test_sonnet_5_can_still_be_forced(self):
        """The pair that stops a widened match from collapsing the two answers."""
        assert supports_forced_tool_choice(SONNET_5) is True

    def test_the_forcing_denylist_names_it(self):
        """``forced_tool_choice_unsupported_reason`` returns None for every model it
        knows nothing about, so an absent entry reads as "supported". Pin the registry
        itself, and pin that Sonnet 5 is NOT in it."""
        from idp_common.bedrock.client import FORCED_TOOL_CHOICE_UNSUPPORTED

        assert "anthropic.claude-sonnet-5-5" in FORCED_TOOL_CHOICE_UNSUPPORTED
        assert "anthropic.claude-sonnet-5" not in FORCED_TOOL_CHOICE_UNSUPPORTED

    def test_the_forced_tool_path_degrades_with_a_reason(self):
        """The user-visible half: a forced-tool extraction config on this model falls
        back to the prose schema and records why, rather than 400ing at inference."""
        from idp_common.extraction.forced_tool import should_force_tool

        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        force, reason = should_force_tool(BASE, True, schema)
        assert force is False
        assert reason and "Sonnet 5.5" in reason

        force5, reason5 = should_force_tool(SONNET_5, True, schema)
        assert force5 is True
        assert not reason5

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_the_minimum_cacheable_prefix_is_512(self, model_id):
        """Live and bracketed: a 513-token prefix wrote 513 cache tokens and a
        508-token one wrote nothing, so the floor is the 512 the card publishes."""
        assert min_cacheable_prefix_tokens(model_id) == 512

    def test_sonnet_5_sits_a_tier_above(self):
        """Measured as a pair on the same calls: Sonnet 5 wrote nothing at 825 tokens
        and 1,223 at 1,223. Giving Sonnet 5.5 the 1,024 tier would warn that classes
        between 512 and 1,024 tokens do not cache when they do — a warning that
        points a user at a change they do not need to make."""
        assert min_cacheable_prefix_tokens(SONNET_5) == 1024

    def test_the_vision_tier_is_high_resolution(self):
        """Inherited by substring from the ``sonnet-5`` alternative, and checked
        rather than assumed because that is where a 5.5 model has diverged before:
        one 2550x3301 page costs 4,772 real input tokens here against 4,768 on
        Sonnet 5 — the same tier."""
        for model_id in ALL_FORMS:
            assert visual_token_cap_for_model(model_id) == 4784
        assert visual_token_cap_for_model(SONNET_5) == 4784

    def test_the_output_budget_is_128k(self):
        """Measured from the rejection rather than the card: ``maxTokens`` 128001
        came back "The maximum tokens you requested exceeds the model limit of
        128000", and 128000 was accepted."""
        assert get_model_max_output_tokens(BASE) == 128000


class TestTheOtherFiveFiveModelsAreNotAGuide:
    """Opus 5.5 and Haiku 5.5 each answer the thinking question differently, and
    Sonnet 5.5 is a third answer again. Asserted as a trio so no two can merge."""

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_thinking_can_be_turned_off(self, model_id):
        """Unlike Opus 5.5. The rejection message says so explicitly: 'To turn
        thinking off on this model, send "thinking": {"type": "between_tools"}' — so
        the capability exists and only the spelling differs."""
        assert thinking_can_be_disabled(model_id) is True

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_but_not_with_the_ordinary_payload(self, model_id):
        """The fact a boolean cannot carry, and the reason
        ``thinking_disable_payload`` exists. Live: ``{"type": "disabled"}`` is
        rejected and ``{"type": "between_tools"}`` returned ``end_turn``."""
        assert thinking_disable_payload(model_id) == {"type": "between_tools"}

    def test_the_three_five_fives_give_three_different_answers(self):
        """Opus 5.5 cannot disable thinking at all; Haiku 5.5 takes the ordinary
        payload; Sonnet 5.5 takes a different one. "It is a 5.5 model" settles
        nothing, and this is the assertion that keeps saying so."""
        assert thinking_can_be_disabled(OPUS_5_5) is False
        assert thinking_disable_payload(OPUS_5_5) is None

        assert thinking_can_be_disabled(HAIKU_5_5) is True
        assert thinking_disable_payload(HAIKU_5_5) == {"type": "disabled"}

        assert thinking_can_be_disabled(BASE) is True
        assert thinking_disable_payload(BASE) == {"type": "between_tools"}

    def test_it_is_absent_from_the_thinking_always_on_set(self):
        """``thinking_can_be_disabled`` is a DENYLIST gate — it answers True for any
        id it knows nothing about — so the assertion above passes whether the decision
        was made or merely never considered. Pin the registry. Membership here would
        read as "effort is the only thinking control", which is false for this model."""
        from idp_common.bedrock.client import _THINKING_ALWAYS_ON_BASE_NAMES

        assert "anthropic.claude-sonnet-5-5" not in _THINKING_ALWAYS_ON_BASE_NAMES
        assert "anthropic.claude-opus-5-5" in _THINKING_ALWAYS_ON_BASE_NAMES

    def test_an_unknown_model_keeps_the_ordinary_payload(self):
        """Non-vacuity for the override table: if it started answering
        ``between_tools`` for everything, every assertion above would pass for the
        wrong reason."""
        assert thinking_disable_payload("us.amazon.nova-pro-v1:0") == {
            "type": "disabled"
        }
        assert thinking_disable_payload(SONNET_5) == {"type": "disabled"}


class TestCachingAndWindows:
    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_explicit_cachepoints_are_supported(self, prefix):
        """Live, and bracketed rather than merely consistent.

        A large prefix caching proves only that caching works somewhere above the
        claimed floor: a 3,202-token write would be consistent with a 512 minimum, a
        1,024 one and Haiku 4.5's 4,096 alike, so it distinguishes none of them. The
        floor was therefore walked on unique text so no earlier call could be read as
        a hit: 508 tokens wrote nothing, 513 wrote 513 (us-east-1, 2026-10-14). That
        brackets it at (508, 513], which is the published 512.
        """
        assert f"{prefix}.anthropic.claude-sonnet-5-5" in CACHEPOINT_SUPPORTED_MODELS

    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_no_1m_variant_is_listed(self, prefix):
        """``us.anthropic.claude-sonnet-5-5:1m`` is not a thing to support, and that
        is measured rather than assumed from the shape of the name: Converse answers
        "The provided model identifier is invalid" for it. 1M is this model's default
        window, so there is no suffix for ``invoke_model`` to translate into an
        ``anthropic_beta`` header — unlike Sonnet 5, whose ``:1m`` id is real and IS
        listed two lines above it in ``CACHEPOINT_SUPPORTED_MODELS``."""
        assert (
            f"{prefix}.anthropic.claude-sonnet-5-5:1m"
            not in CACHEPOINT_SUPPORTED_MODELS
        )
        assert f"{prefix}.anthropic.claude-sonnet-5:1m" in CACHEPOINT_SUPPORTED_MODELS

    @pytest.mark.parametrize("model_id", ALL_FORMS)
    def test_document_blocks_are_accepted(self, model_id):
        """Live: a PDF ``document`` content block was accepted (9,368 input tokens),
        so this model is usable for whole-PDF extraction and discovery."""
        assert supports_document_blocks(model_id) is True

    def test_it_is_absent_from_the_document_block_denylist(self):
        """Denylist gate again — see the thinking tests for why True alone is weak.
        The routes that cannot take a document block are the bedrock-mantle Responses
        API, Grok and Astra; a Converse Claude is none of them."""
        from idp_common.bedrock.client import DOCUMENT_BLOCK_UNSUPPORTED_ROUTES

        assert not any("sonnet" in route for route in DOCUMENT_BLOCK_UNSUPPORTED_ROUTES)
        assert document_blocks_unsupported_reason(BASE) is None

    def test_the_window_is_stated_truthfully(self):
        """1M is this model's default window and the limits file says so.

        Unlike Haiku 5.5 this carries no cost caveat — the AWS price list gives Sonnet
        5.5 a single prompt-length band, so nothing is under-reported above any
        threshold. What it does carry is a sizing consequence, documented on the
        ``model_config_limits.yaml`` entry: the derived agentic shard budget is wider,
        and wide shards defeat prompt caching.
        """
        assert get_model_max_input_tokens(BASE) == 1000000

    def test_the_limits_entry_precedes_the_sonnet_5_one(self):
        """Placement, not just presence. Patterns are matched as substrings with
        first-match-wins and ``claude-sonnet-5`` matches ``claude-sonnet-5-5``, so an
        entry added BELOW Sonnet 5's would be dead and this model would silently
        report a 200,000-token window. Asserted against the file so a reordering
        fails here rather than at auto-sizing time."""
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[5]
        data = yaml.safe_load(
            (root / "config_library" / "model_config_limits.yaml").read_text()
        )
        patterns = [entry["pattern"] for entry in data["model_limits"]]
        assert "claude-sonnet-5-5" in patterns
        assert "claude-sonnet-5" in patterns
        assert patterns.index("claude-sonnet-5-5") < patterns.index("claude-sonnet-5")

    def test_the_scan_table_orders_it_ahead_of_sonnet_5_too(self):
        """``benchmarks/harness/cache_audit.py`` has the same order dependency, and
        nothing else pins it for THIS model.

        ``CACHE_MINIMUMS`` is scanned in insertion order with ``if name in model_id``,
        so ``"claude-sonnet-5"`` placed first shadows ``"claude-sonnet-5-5"`` and the
        scan reports 1,024 — double the real minimum. That is not hypothetical: it is
        the state the tree was in before this model's entry was added.

        ⚠️ ``benchmarks/tests/test_cache_minimum_parity.py`` cannot catch a reorder
        here. Its agreement check compares key→value on the intersection of the two
        tables, which sees a wrong *number* and not a wrong *position*; the check that
        does resolve by substring iterates ids derived from ``config_matrix.yaml``'s
        axes, and this model is deliberately on no axis. So a later reorder would
        leave every suite green. The equivalent dependency in
        ``model_config_limits.yaml`` is pinned by
        ``test_the_limits_entry_precedes_the_sonnet_5_one`` above and the one in
        ``prompt_cache.py`` by the resolved-value rows in
        ``test_prompt_cache_minimums.py``; this is the third of the three.

        Read from source with ``ast`` rather than by importing the harness, which is
        not on this suite's path — and the property under test is the literal key
        order a reader sees, which is exactly what the source gives.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]
        tree = ast.parse(
            (root / "benchmarks" / "harness" / "cache_audit.py").read_text()
        )
        keys: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CACHE_MINIMUMS"
                for t in node.targets
            ):
                assert isinstance(node.value, ast.Dict)
                keys = [
                    k.value
                    for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
                break
        assert keys, "could not read CACHE_MINIMUMS out of cache_audit.py"
        assert "claude-sonnet-5-5" in keys, (
            "cache_audit.CACHE_MINIMUMS has no claude-sonnet-5-5 key, so the scan "
            "resolves this model through claude-sonnet-5 and reports 1,024 against "
            "the product's 512."
        )
        assert keys.index("claude-sonnet-5-5") < keys.index("claude-sonnet-5"), (
            "claude-sonnet-5-5 must precede claude-sonnet-5 in "
            "cache_audit.CACHE_MINIMUMS. The dict is scanned in insertion order with "
            "a substring match, and the shorter key is a substring of the longer, so "
            "below it this entry is unreachable and the scan silently reports double "
            "this model's real minimum."
        )

    def test_sonnet_5_keeps_its_200k_sizing_budget(self):
        """The pair for the window assertion. Sonnet 5's base id is pinned at 200,000
        on purpose so auto-sizing stays conservative unless its ``:1m`` variant is
        selected; Sonnet 5.5 has no such variant, so 1M is the only honest answer and
        the two models legitimately differ here."""
        assert get_model_max_input_tokens(SONNET_5) == 200000
        assert get_model_max_input_tokens(f"{SONNET_5}:1m") == 1000000

    def test_it_is_sized_like_a_1m_model_rather_than_a_200k_one(self):
        """The consequence of the two assertions above, asserted where a reader will
        look: 578,400 tokens of agentic shard budget against Sonnet 5's 18,400 at the
        default 0.30 context buffer. Stated as a comparison rather than a literal so
        a context-buffer change does not red-line this."""
        from idp_common.bedrock.sizing import compute_sizing_plan

        plan = compute_sizing_plan(model_id=BASE)
        sonnet5 = compute_sizing_plan(model_id=SONNET_5)
        assert plan.shard_token_budget > sonnet5.shard_token_budget
        assert (
            plan.shard_token_budget
            == compute_sizing_plan(model_id=f"{SONNET_5}:1m").shard_token_budget
        )


class TestPricing:
    """The rates, and the one trap a cost report sets on this model."""

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
            "bedrock/us.anthropic.claude-sonnet-5-5",
            "bedrock/eu.anthropic.claude-sonnet-5-5",
        ],
    )
    def test_the_regional_rates(self, name):
        """The US/EU geo band, which carries the usual 10% premium over global. Read
        from the AWS price list offer file for AmazonBedrockFoundationModels version
        20261012084008 (retrieved 2026-10-14)."""
        units = self._units(name)
        assert units["inputTokens"] == 2.2e-6
        assert units["outputTokens"] == 1.1e-5
        assert units["cacheReadInputTokens"] == 1.1e-7  # 0.05x input
        assert units["cacheWriteInputTokens"] == 2.75e-6  # 1.25x input, exactly

    def test_the_global_rates(self):
        units = self._units("bedrock/global.anthropic.claude-sonnet-5-5")
        assert units["inputTokens"] == 2.0e-6
        assert units["outputTokens"] == 1.0e-5
        assert units["cacheReadInputTokens"] == 1.0e-7
        assert units["cacheWriteInputTokens"] == 2.5e-6

    def test_the_cache_read_rate_is_half_an_ordinary_claude_one(self):
        """The one real price difference from Sonnet 5, and the reason to pin the
        ratio rather than only the rate: almost every Claude in this file prices a
        cache read at 0.1x input, so a reader checking 0.05x against the family would
        read the row as a typo. Opus 5.5 is at 0.05x too — it got there first — which
        makes this the second model on the lower ratio rather than an outlier of one,
        and Sonnet 5 is the contrast that matters because it is the model this one
        will be compared against."""
        units = self._units("bedrock/us.anthropic.claude-sonnet-5-5")
        assert units["cacheReadInputTokens"] / units["inputTokens"] == pytest.approx(
            0.05
        )
        opus = self._units("bedrock/us.anthropic.claude-opus-5-5")
        assert opus["cacheReadInputTokens"] / opus["inputTokens"] == pytest.approx(0.05)
        five = self._units("bedrock/us.anthropic.claude-sonnet-5")
        assert five["cacheReadInputTokens"] / five["inputTokens"] == pytest.approx(0.1)

    def test_the_sonnet_5_rows_are_knowingly_higher_and_that_is_not_a_discount(self):
        """⚠️ A cost report out of this file makes Sonnet 5.5 look 1.50x cheaper than
        Sonnet 5 on input and output. It is not: the price list gives the two models
        the identical $2.20/$11.00 band, and the six Sonnet 5 rows carry Sonnet 4.6's
        rates by a documented, deliberately-deferred decision (the ⚠️ block on
        ``bedrock/us.anthropic.claude-sonnet-5``).

        Asserted rather than only commented so the artefact is visible to whoever
        fixes those rows: when they drop to 2.2e-6 this test fails and points at the
        prose that needs removing, instead of the discrepancy quietly disappearing
        from a document nobody re-reads.

        ⚠️ This assertion is about the RATIO between two models, so it fires on a
        change to **either** side — which is the point, but it means a failure here
        is not evidence that Sonnet 5.5's own rows drifted. Those are pinned
        absolutely in ``test_the_regional_rates`` above, so check that first: if it
        passes and this fails, somebody corrected the Sonnet 5 rows and the work is
        to delete prose, not to change a price. The failure message says so, because
        the name of this test alone points at the wrong model.
        """
        five_five = self._units("bedrock/us.anthropic.claude-sonnet-5-5")
        five = self._units("bedrock/us.anthropic.claude-sonnet-5")
        hint = (
            "The Sonnet 5 rows no longer read 1.50x Sonnet 5.5's. If "
            "test_the_regional_rates still passes, Sonnet 5.5 is fine and the Sonnet "
            "5 rows have been CORRECTED — in which case delete this test and the "
            "⚠️ paragraphs it guards: the pricing.yaml note on "
            "bedrock/us.anthropic.claude-sonnet-5-5, the CHANGELOG's '~33% cheaper' "
            "sentence, and the benchmarking index row's price note. If "
            "test_the_regional_rates also fails, the new rows are what drifted."
        )
        assert five["inputTokens"] / five_five["inputTokens"] == pytest.approx(1.5), (
            hint
        )
        assert five["outputTokens"] / five_five["outputTokens"] == pytest.approx(1.5), (
            hint
        )

    def test_no_1m_pricing_entry_exists(self):
        """There is no ``:1m`` id for this model, so an entry would be a rate keyed on
        something unreachable — and ``test_model_surface_consistency`` reads this file
        as the set of ids configuration validation accepts."""
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[5]
        data = yaml.safe_load((root / "config_library" / "pricing.yaml").read_text())
        names = {entry["name"] for entry in data["pricing"]}
        for prefix in ("us", "eu", "global"):
            assert f"bedrock/{prefix}.anthropic.claude-sonnet-5-5:1m" not in names


class TestThePicklists:
    """Where the model is offered, and the one place it deliberately is not."""

    @staticmethod
    def _template_text():
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]
        return (root / "patterns" / "unified" / "template.yaml").read_text()

    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_it_is_in_the_same_enums_as_opus_5_5(self, prefix):
        """Every model enum in the ConfigSchema bar the cluster-analysis one.

        Asserted as **parity with Opus 5.5** rather than against a literal count, and
        the choice matters in both directions. Opus 5.5 is the right yardstick
        because it is the only other model with exactly this shape — offered
        everywhere except ``analysis_model_id``, for exactly the same reason (both
        reject a forced ``toolChoice``). A literal would red-line on the next
        legitimate enum addition, which is a failure that teaches a maintainer to
        edit the number; parity keeps catching the defect that matters — a model
        added to some picklists and not others — and survives a new enum, since a
        new enum gets both models or neither.
        """
        text = self._template_text()
        mine = text.count(f'"{prefix}.anthropic.claude-sonnet-5-5"')
        opus = text.count(f'"{prefix}.anthropic.claude-opus-5-5"')
        assert mine == opus, (
            f"{prefix}. Sonnet 5.5 appears in {mine} enum positions and Opus 5.5 in "
            f"{opus}. The two are offered in the same set of picklists — everywhere "
            "except discovery.multi_document.analysis_model_id — so a difference "
            "means one of them was added to an enum the other was not, which is how "
            "a model ends up selectable for one service and not its neighbour."
        )
        # Non-vacuity: parity would also hold at zero, which is the one way this
        # assertion could pass while the model is in no picklist at all.
        assert mine > 1, (
            f"only {mine} enum position(s) for {prefix}. Sonnet 5.5 — parity with "
            "Opus 5.5 is satisfied but neither model is actually offered, so this "
            "check is vacuous."
        )

    def test_it_is_absent_from_the_cluster_analysis_enum(self):
        """The 18th, and the exception that the forcing rejection forces.

        ``discovery.multi_document.analysis_model_id`` drives the one path that asks
        Strands for structured output, and Strands retries with
        ``toolChoice: {"any": {}}`` when the first turn answers in prose. On this
        model that retry 400s, so the failure is intermittent by construction — the
        model appears to work until a cluster happens to need the retry. Opus 5.5 is
        excluded for exactly this reason and Sonnet 5.5 joins it.

        Derived from the file: the enum is located by its property name and its own
        text searched, so a renamed property or a reordered enum fails here rather
        than being silently skipped.
        """
        import re

        lines = self._template_text().split("\n")
        starts = [
            i
            for i, line in enumerate(lines)
            if re.match(r"^\s+analysis_model_id:\s*$", line)
        ]
        assert len(starts) == 1, (
            "expected exactly one analysis_model_id property; the assertion below "
            "is about that one enum and cannot be about several"
        )
        start = starts[0]
        end = next(
            i
            for i in range(start, len(lines))
            if re.match(r"^\s+order: 1\s*$", lines[i])
        )
        enum_text = "\n".join(lines[start : end + 1])
        assert "claude-sonnet-5-5" not in enum_text
        assert "claude-opus-5-5" not in enum_text
        # Non-vacuity: the enum really is a model picklist, and the 5.0 siblings
        # really are offered in it — so the two absences above are decisions rather
        # than an empty search.
        assert '"us.anthropic.claude-sonnet-5"' in enum_text
        assert '"us.anthropic.claude-opus-5"' in enum_text

    def test_a_cluster_analysis_config_naming_it_is_refused(self):
        """The enum only governs the UI. A config can name a model it does not list,
        so validation refuses this at config time as well — which is what turns an
        intermittent runtime 400 into an error a user sees once."""
        from idp_common.config.merge_utils import (
            _validate_multi_doc_discovery_forced_tool,
        )

        result = {"valid": True, "errors": [], "warnings": []}
        _validate_multi_doc_discovery_forced_tool(
            {"discovery": {"multi_document": {"analysis_model_id": BASE}}}, result
        )
        assert result["valid"] is False
        assert any("sonnet-5-5" in err for err in result["errors"])

        ok = {"valid": True, "errors": [], "warnings": []}
        _validate_multi_doc_discovery_forced_tool(
            {"discovery": {"multi_document": {"analysis_model_id": SONNET_5}}}, ok
        )
        assert ok["valid"] is True

    @pytest.mark.parametrize("prefix", ["us", "eu", "global"])
    def test_the_ui_override_dropdown_offers_it(self, prefix):
        """``schemaConstants.ts`` is a separate hardcoded surface whose own comment
        asks to be kept in step with the template enums."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]
        text = (
            root / "src" / "ui" / "src" / "constants" / "schemaConstants.ts"
        ).read_text()
        assert f"'{prefix}.anthropic.claude-sonnet-5-5'" in text

    def test_both_capacity_quota_maps_name_it_explicitly(self):
        """Capacity Planning raises for a step model it has no TPM quota for, so a
        selectable model missing from these maps is a hard failure rather than a
        missing row — ``calculate_capacity`` does
        ``raise ValueError("TPM quota not available for model ...")``.

        ⚠️ The RPM half is the one that matters most here, and it is the opposite
        shape. Its builder matches with ``clean_mapping_key in clean_model_id``, and
        ``us.anthropic.claude-sonnet-5`` is a SUBSTRING of
        ``us.anthropic.claude-sonnet-5-5`` — so an absent Sonnet 5.5 entry does not
        raise, it silently binds this model to **Sonnet 5's** quota code. Haiku 5.5
        has no substring parent and so fails loudly instead, which is why this
        assertion is about Sonnet 5.5 specifically and is about *explicit membership*
        rather than about resolution: resolution already "works", wrongly.

        The codes were read live from Service Quotas (us-east-1, 2026-10-14):
        L-94A31E46 is "Cross-region model inference tokens per minute for Anthropic
        Claude Sonnet 5.5" and L-31AB82D0 its global. twin.
        """
        import json
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]
        text = (root / "template.yaml").read_text()
        for var in ("BEDROCK_MODEL_QUOTA_CODES", "BEDROCK_MODEL_RPM_QUOTA_CODES"):
            match = re.search(var + r": '(\{.*?\})'", text)
            assert match, f"{var} not found in template.yaml"
            codes = json.loads(match.group(1))
            assert codes.get("us.anthropic.claude-sonnet-5-5") == "L-94A31E46", (
                f"{var} must name us.anthropic.claude-sonnet-5-5 explicitly"
            )
            assert codes.get("global.anthropic.claude-sonnet-5-5") == "L-31AB82D0"
            # The eu. profile shares the us. code, and that is correct rather than a
            # stand-in: Service Quotas codes are global identifiers whose VALUE is
            # per-region, L-94A31E46 is the same code in us-east-1, eu-west-1 and
            # eu-central-1 (checked in all three, 2026-10-14), and
            # calculate_capacity builds its client with no region override so an EU
            # stack reads the EU value. Without this row an EU deployment configured
            # on the eu. profile raises — the model is in every eu. picklist and is
            # the target of the new MODEL_MAPPINGS row.
            assert codes.get("eu.anthropic.claude-sonnet-5-5") == "L-94A31E46"
            # Non-vacuity: Sonnet 5's own entries are still there and still
            # different, so the two models are not collapsed onto one code.
            assert codes["us.anthropic.claude-sonnet-5"] == "L-D4FBCF4E"
            assert (
                codes["us.anthropic.claude-sonnet-5-5"]
                != codes["us.anthropic.claude-sonnet-5"]
            )

    def test_the_eu_mapping_row_exists(self):
        """An EU deployment whose stored config names the ``us.`` id keeps an id that
        is not callable there; ``filter_models_by_region`` only hides it from the
        picklists. The row maps onto this model's own EU geo profile, so it is safe
        in both directions — ``get_model_mapping`` walks the dict backwards for a US
        target. There is no ``:1m`` pair to add."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]
        text = (
            root / "src" / "lambda" / "update_configuration" / "index.py"
        ).read_text()
        assert (
            '"us.anthropic.claude-sonnet-5-5": "eu.anthropic.claude-sonnet-5-5"' in text
        )
        # No ":1m" ROW — matched as a mapping key rather than as a substring, because
        # the comment above that row names the id in prose precisely to explain why
        # there is none.
        assert '"us.anthropic.claude-sonnet-5-5:1m":' not in text
