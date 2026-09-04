# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for model_utils module."""

import pytest

from idp_common.bedrock.model_utils import (
    get_model_max_input_tokens,
    get_model_max_output_tokens,
    parse_max_tokens_limit_from_error,
    parse_model_id,
    resolve_model_id_from_arn,
)


@pytest.mark.unit
class TestParseMaxTokensLimitFromError:
    """Test extracting the model limit from a Bedrock over-limit ValidationException.

    There is no AWS API for the per-model output cap, so this error text is the
    authoritative fallback source. Messages captured live from Bedrock Converse.
    """

    def test_claude_over_limit_message(self):
        msg = (
            "The model returned the following errors: max_tokens: 999999 > 128000, "
            "which is the maximum allowed number of output tokens for "
            "anthropic.claude-sonnet-5"
        )
        assert parse_max_tokens_limit_from_error(msg) == 128000

    def test_nova_over_limit_message(self):
        msg = (
            "The maximum tokens you requested exceeds the model limit of 10000. "
            "Try again with a maximum tokens value that is lower than 10000."
        )
        assert parse_max_tokens_limit_from_error(msg) == 10000

    def test_unrelated_message_returns_none(self):
        assert parse_max_tokens_limit_from_error("some other validation error") is None

    def test_empty_message_returns_none(self):
        assert parse_max_tokens_limit_from_error("") is None


@pytest.mark.unit
class TestModelConfigLimitsSources:
    """config_library/model_config_limits.yaml is the single source file.

    At runtime the DynamoDB Configuration Table (merged Default/Custom, editable
    in the UI) is authoritative; the disk YAML is the offline fallback — the
    same split pricing.yaml uses. Nothing is bundled into the wheel anymore.
    """

    def test_input_window_base_vs_1m(self):
        """get_model_max_input_tokens resolves the base 200K window and the :1m
        1M window distinctly (the :1m pattern must win, being listed first)."""
        assert get_model_max_input_tokens("us.anthropic.claude-sonnet-5") == 200000
        assert get_model_max_input_tokens("us.anthropic.claude-sonnet-5:1m") == 1000000
        assert get_model_max_input_tokens("us.amazon.nova-lite-v1:0") == 300000
        # Other Claude 4.x :1m still gets 1M input (64K output family).
        assert (
            get_model_max_input_tokens("us.anthropic.claude-sonnet-4-20250514-v1:0:1m")
            == 1000000
        )

    def test_input_window_unknown_model_raises(self):
        with pytest.raises(ValueError):
            get_model_max_input_tokens("some.unknown.model-v9:0")

    def test_no_wheel_bundled_copy(self):
        """The old in-package duplicate must not come back (drift hazard)."""
        from pathlib import Path

        import idp_common.bedrock.model_utils as mu

        bundled = (
            Path(mu.__file__).parent.parent / "config" / "model_config_limits.yaml"
        )
        assert not bundled.exists(), (
            "idp_common/config/model_config_limits.yaml has reappeared — "
            "config_library/model_config_limits.yaml is the single source "
            "(seeded to DynamoDB at deploy); do not bundle a copy in the wheel."
        )

    def test_offline_resolves_from_repo_config_library(self, monkeypatch, tmp_path):
        """No table env: limits resolve from config_library/ on disk (dev tree).

        The repo copy is found 5-parents-up from model_utils.py even when the
        cwd has no config_library/ — same resolution pricing.yaml uses.
        """
        from idp_common.bedrock import model_utils

        monkeypatch.delenv("IDP_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
        monkeypatch.chdir(tmp_path)  # no config_library here
        model_utils._clear_model_limits_cache()
        try:
            assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5") == 128000
        finally:
            model_utils._clear_model_limits_cache()

    def test_dynamodb_limits_take_precedence(self, monkeypatch):
        """A merged DynamoDB list overrides the disk YAML."""
        from idp_common.bedrock import model_utils

        model_utils._clear_model_limits_cache()
        monkeypatch.setattr(
            model_utils,
            "_load_model_limits_from_dynamodb",
            lambda: [{"pattern": "claude-sonnet-5", "max_output_tokens": 50000}],
        )
        try:
            assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5") == 50000
            # Unmatched models still raise so client.py self-heal works
            with pytest.raises(ValueError, match="Unsupported model ID"):
                get_model_max_output_tokens("us.amazon.nova-lite-v1:0")
        finally:
            model_utils._clear_model_limits_cache()

    def test_dynamodb_failure_falls_back_to_disk(self, monkeypatch):
        """CONFIGURATION_TABLE_NAME set but unreachable -> disk YAML, no error."""
        from idp_common.bedrock import model_utils

        model_utils._clear_model_limits_cache()
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "no-such-table-xyz")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        try:
            assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5") == 128000
        finally:
            model_utils._clear_model_limits_cache()

    def test_ttl_cache_avoids_repeat_reads(self, monkeypatch):
        """Within the TTL the DynamoDB loader is consulted only once."""
        from idp_common.bedrock import model_utils

        calls = {"n": 0}

        def fake_ddb():
            calls["n"] += 1
            return [{"pattern": "claude-sonnet-5", "max_output_tokens": 42}]

        model_utils._clear_model_limits_cache()
        monkeypatch.setattr(model_utils, "_load_model_limits_from_dynamodb", fake_ddb)
        try:
            assert get_model_max_output_tokens("claude-sonnet-5") == 42
            assert get_model_max_output_tokens("claude-sonnet-5") == 42
            assert calls["n"] == 1
            model_utils._clear_model_limits_cache()
            assert get_model_max_output_tokens("claude-sonnet-5") == 42
            assert calls["n"] == 2
        finally:
            model_utils._clear_model_limits_cache()


@pytest.mark.unit
class TestParseModelId:
    """Test model ID parsing functionality."""

    def test_parse_model_without_suffix(self):
        """Test parsing model ID without service tier suffix."""
        base_id, tier = parse_model_id("us.amazon.nova-2-lite-v1:0")
        assert base_id == "us.amazon.nova-2-lite-v1:0"
        assert tier is None

    def test_parse_model_with_flex_suffix(self):
        """Test parsing model ID with flex suffix."""
        base_id, tier = parse_model_id("us.amazon.nova-2-lite-v1:0:flex")
        assert base_id == "us.amazon.nova-2-lite-v1:0"
        assert tier == "flex"

    def test_parse_model_with_priority_suffix(self):
        """Test parsing model ID with priority suffix."""
        base_id, tier = parse_model_id("us.amazon.nova-2-lite-v1:0:priority")
        assert base_id == "us.amazon.nova-2-lite-v1:0"
        assert tier == "priority"

    def test_parse_model_with_uppercase_suffix(self):
        """Test parsing model ID with uppercase suffix."""
        base_id, tier = parse_model_id("us.amazon.nova-2-lite-v1:0:FLEX")
        assert base_id == "us.amazon.nova-2-lite-v1:0"
        assert tier == "flex"

    def test_parse_model_with_invalid_suffix(self):
        """Test parsing model ID with invalid suffix."""
        base_id, tier = parse_model_id("us.amazon.nova-2-lite-v1:0:invalid")
        assert base_id == "us.amazon.nova-2-lite-v1:0:invalid"
        assert tier is None

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        base_id, tier = parse_model_id("")
        assert base_id == ""
        assert tier is None

    def test_parse_none(self):
        """Test parsing None."""
        base_id, tier = parse_model_id(None)
        assert base_id is None
        assert tier is None

    def test_parse_model_with_1m_and_tier(self):
        """Test parsing model ID with both 1m and tier suffix."""
        # This should not happen in practice, but test behavior
        base_id, tier = parse_model_id("us.anthropic.claude-3-5-haiku:1m:flex")
        assert base_id == "us.anthropic.claude-3-5-haiku:1m"
        assert tier == "flex"

    def test_parse_global_model_with_flex(self):
        """Test parsing global model with flex suffix."""
        base_id, tier = parse_model_id("global.amazon.nova-2-lite-v1:0:flex")
        assert base_id == "global.amazon.nova-2-lite-v1:0"
        assert tier == "flex"

    def test_parse_global_model_with_priority(self):
        """Test parsing global model with priority suffix."""
        base_id, tier = parse_model_id("global.amazon.nova-2-lite-v1:0:priority")
        assert base_id == "global.amazon.nova-2-lite-v1:0"
        assert tier == "priority"

    @pytest.mark.parametrize("tier", ["flex", "priority"])
    def test_parse_version_less_model_id_with_tier(self, tier):
        """A model ID with no ``:<version>`` segment still yields its tier.

        Every Bedrock ID used here historically ended in ``:0``/``:1m``, so a
        tier suffix meant a THIRD colon-part and the parser bailed out at two.
        Newer families carry no version segment (e.g. ``us.xai.grok-4.6``),
        which left the tier unextracted and sent the whole string to Bedrock as
        the modelId — an invalid identifier.
        """
        base_id, parsed_tier = parse_model_id(f"us.xai.grok-4.6:{tier}")
        assert base_id == "us.xai.grok-4.6"
        assert parsed_tier == tier

    def test_version_less_model_id_without_tier_is_unchanged(self):
        base_id, tier = parse_model_id("us.xai.grok-4.6")
        assert base_id == "us.xai.grok-4.6"
        assert tier is None

    @pytest.mark.parametrize(
        "model_id",
        [
            # Regression guard for the two-part early-return removal: none of
            # these carry a tier, and their trailing part must not be eaten.
            "us.amazon.nova-2-lite-v1:0",
            "us.anthropic.claude-opus-5:1m",
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-sonnet-4-6:1m",
            "anthropic.claude-sonnet-4-6",
            "openai.gpt-5.6-sol",
            "LambdaHook",
        ],
    )
    def test_non_tier_suffixes_are_preserved(self, model_id):
        base_id, tier = parse_model_id(model_id)
        assert base_id == model_id
        assert tier is None


@pytest.mark.unit
class TestResolveModelIdFromArn:
    """Test reducing a Bedrock ARN to the model ID it names (issue #648)."""

    def test_govcloud_inference_profile_arn(self):
        assert (
            resolve_model_id_from_arn(
                "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012"
                ":inference-profile/us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
            )
            == "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )

    def test_foundation_model_arn_with_empty_account(self):
        assert (
            resolve_model_id_from_arn(
                "arn:aws:bedrock:us-east-1::foundation-model/"
                "anthropic.claude-3-5-sonnet-20240620-v1:0"
            )
            == "anthropic.claude-3-5-sonnet-20240620-v1:0"
        )

    def test_bare_model_id_unchanged(self):
        model_id = "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
        assert resolve_model_id_from_arn(model_id) == model_id

    def test_lambda_hook_unchanged(self):
        assert resolve_model_id_from_arn("LambdaHook") == "LambdaHook"

    def test_malformed_arn_unchanged(self):
        assert resolve_model_id_from_arn("arn:malformed") == "arn:malformed"

    def test_arn_without_resource_id_unchanged(self):
        # No "/" separator, so there is no resource id to extract
        arn = "arn:aws:bedrock:us-east-1:123456789012:something"
        assert resolve_model_id_from_arn(arn) == arn

    def test_opaque_application_profile_resolves_to_uuid(self):
        # Resolves to the opaque id, which matches no catalog entry — callers
        # treat that as unverifiable rather than invalid.
        assert (
            resolve_model_id_from_arn(
                "arn:aws:bedrock:us-east-1:123456789012"
                ":application-inference-profile/abc123uuid"
            )
            == "abc123uuid"
        )

    def test_empty_and_none_unchanged(self):
        assert resolve_model_id_from_arn("") == ""
        assert resolve_model_id_from_arn(None) is None


@pytest.mark.unit
class TestGetModelMaxOutputTokens:
    """Test model max output token detection."""

    def test_claude4_models_return_64k(self):
        """Claude 4 models that cap at 64,000 output tokens: Sonnet 4.0/4.5,
        Haiku 4.5, and Opus 4.0/4.1/4.5. (Sonnet 4.6 and Opus 4.6 are 128K —
        see test_claude46_models_return_128k.)"""
        # Sonnet 4.0
        assert (
            get_model_max_output_tokens("us.anthropic.claude-sonnet-4-20250514-v1:0")
            == 64_000
        )
        # Sonnet 4.5
        assert (
            get_model_max_output_tokens("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
            == 64_000
        )
        # Haiku 4.5
        assert (
            get_model_max_output_tokens("us.anthropic.claude-haiku-4-5-20251001-v1:0")
            == 64_000
        )
        # Opus 4.5
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-5-20250514-v1:0")
            == 64_000
        )

    def test_claude46_models_return_128k(self):
        """Sonnet 4.6 and Opus 4.6 support 128,000 output tokens (like Opus 4.7/4.8),
        including the :1m extended-context variants. Regression: the generic
        'claude-(opus|sonnet|haiku)-4' catch-all previously mis-capped these at 64K."""
        assert get_model_max_output_tokens("us.anthropic.claude-sonnet-4-6") == 128_000
        assert (
            get_model_max_output_tokens("us.anthropic.claude-sonnet-4-6:1m") == 128_000
        )
        assert get_model_max_output_tokens("eu.anthropic.claude-sonnet-4-6") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-6") == 128_000
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-6-v1:1m") == 128_000
        )

    def test_claude_opus_47_48_return_128k(self):
        """Opus 4.7 and 4.8 support 128,000 output tokens, incl. :1m variants."""
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-7") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-7:1m") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-8") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-8:1m") == 128_000

    def test_claude3_models_return_8k(self):
        """Test Claude 3 models return 8,192 max tokens."""
        assert (
            get_model_max_output_tokens("us.anthropic.claude-3-haiku-20240307-v1:0")
            == 8_192
        )
        assert (
            get_model_max_output_tokens("us.anthropic.claude-3-5-sonnet-20241022-v2:0")
            == 8_192
        )
        assert (
            get_model_max_output_tokens("eu.anthropic.claude-3-5-sonnet-20241022-v2:0")
            == 8_192
        )

    def test_nova_models_return_10k(self):
        """Test Amazon Nova models return 10,000 max tokens."""
        assert get_model_max_output_tokens("us.amazon.nova-lite-v1:0") == 10_000
        assert get_model_max_output_tokens("us.amazon.nova-pro-v1:0") == 10_000
        assert get_model_max_output_tokens("us.amazon.nova-premier-v1:0") == 10_000
        assert get_model_max_output_tokens("us.amazon.nova-micro-v1:0") == 10_000

    def test_nova2_models_return_10k(self):
        """Test Amazon Nova 2 models return 10,000 max tokens."""
        assert get_model_max_output_tokens("us.amazon.nova-2-lite-v1:0") == 10_000
        assert get_model_max_output_tokens("eu.amazon.nova-2-lite-v1:0") == 10_000
        assert get_model_max_output_tokens("global.amazon.nova-2-lite-v1:0") == 10_000

    def test_openai_gpt5_models_return_128k(self):
        """Test OpenAI GPT-5.x models return 128,000 max tokens."""
        assert get_model_max_output_tokens("openai.gpt-5.4") == 128_000
        assert get_model_max_output_tokens("openai.gpt-5.5") == 128_000

    def test_unknown_model_raises_error(self):
        """Test unknown models raise ValueError instead of returning default."""
        with pytest.raises(ValueError, match="Unsupported model ID"):
            get_model_max_output_tokens("unknown.model.id")

        with pytest.raises(ValueError, match="Unsupported model ID"):
            get_model_max_output_tokens("us.meta.llama-3-70b-instruct-v1:0")

    def test_case_insensitive(self):
        """Test model ID matching is case insensitive."""
        assert (
            get_model_max_output_tokens("US.ANTHROPIC.CLAUDE-SONNET-4-20250514-V1:0")
            == 64_000
        )
        assert get_model_max_output_tokens("US.AMAZON.NOVA-LITE-V1:0") == 10_000
        assert (
            get_model_max_output_tokens("US.ANTHROPIC.CLAUDE-3-HAIKU-20240307-V1:0")
            == 8_192
        )

    def test_invalid_regex_pattern_is_skipped_not_raised(self, monkeypatch):
        """A malformed regex in a limit entry must not crash the hot path.

        ModelLimitEntry rejects bad patterns at save time, but data written via
        a bypass path or hand-edited YAML could still be invalid. The resolver
        loop must skip such an entry and continue matching, never raising
        re.error (which callers don't catch as ValueError).
        """
        from idp_common.bedrock import model_utils

        model_utils._clear_model_limits_cache()
        monkeypatch.setattr(
            model_utils,
            "_load_model_limits_from_dynamodb",
            lambda: [
                {"pattern": "claude-(", "max_output_tokens": 999},  # invalid regex
                {"pattern": "claude-sonnet-5", "max_output_tokens": 77_000},
            ],
        )
        try:
            # The invalid entry is skipped; the next valid entry matches.
            assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5") == 77_000
        finally:
            model_utils._clear_model_limits_cache()

    def test_extended_context_1m_suffix(self):
        """Test that :1m extended context suffix doesn't change output token limit."""
        # Claude 4 with :1m suffix should still be 64K output (Sonnet, Haiku)
        assert (
            get_model_max_output_tokens("us.anthropic.claude-sonnet-4-20250514-v1:0:1m")
            == 64_000
        )
        # The :1m increases INPUT context window, not output tokens

    def test_opus_4_7_returns_128k(self):
        """Test Claude Opus 4.7 returns 128,000 max tokens."""
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-7-20250514-v1:0")
            == 128_000
        )
        assert (
            get_model_max_output_tokens("global.anthropic.claude-opus-4-7") == 128_000
        )
        # With :1m suffix
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-7-20250514-v1:0:1m")
            == 128_000
        )

    def test_opus_4_8_returns_128k(self):
        """Test Claude Opus 4.8 returns 128,000 max tokens."""
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-8-20251201-v1:0")
            == 128_000
        )
        assert (
            get_model_max_output_tokens("global.anthropic.claude-opus-4-8") == 128_000
        )
        # With :1m suffix
        assert get_model_max_output_tokens("us.anthropic.claude-opus-4-8:1m") == 128_000

    def test_opus_5_returns_128k(self):
        """Claude Opus 5 returns 128,000 max tokens, incl. :1m and all geos."""
        assert get_model_max_output_tokens("us.anthropic.claude-opus-5") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-opus-5:1m") == 128_000
        assert get_model_max_output_tokens("eu.anthropic.claude-opus-5:1m") == 128_000
        assert get_model_max_output_tokens("global.anthropic.claude-opus-5") == 128_000

    def test_sonnet_5_returns_128k(self):
        """Claude Sonnet 5 returns 128,000 max tokens (1M context), incl. :1m.
        Sonnet 5 does NOT match the claude-(opus|sonnet|haiku)-4 catch-all, so it
        needs its own model_config_limits entry."""
        assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5") == 128_000
        assert get_model_max_output_tokens("us.anthropic.claude-sonnet-5:1m") == 128_000
        assert (
            get_model_max_output_tokens("global.anthropic.claude-sonnet-5") == 128_000
        )

    def test_older_opus_versions_return_64k(self):
        """Claude Opus 4.5 returns 64,000 max tokens. (Opus 4.6 is 128K — see
        test_claude46_models_return_128k.)"""
        assert (
            get_model_max_output_tokens("us.anthropic.claude-opus-4-5-20250514-v1:0")
            == 64_000
        )


@pytest.mark.unit
class TestModelLimitEntryPatternValidation:
    """The user-editable pattern must be validated as a compilable regex at save
    time so a bad value is rejected with a clear message rather than raising
    re.error deep on the Bedrock hot path."""

    def test_valid_pattern_accepted(self):
        from idp_common.config.models import ModelLimitEntry

        entry = ModelLimitEntry(pattern="claude-sonnet-5.*", max_output_tokens=128000)
        assert entry.pattern == "claude-sonnet-5.*"

    def test_invalid_regex_rejected(self):
        from pydantic import ValidationError

        from idp_common.config.models import ModelLimitEntry

        with pytest.raises(ValidationError, match="valid regular expression"):
            ModelLimitEntry(pattern="claude-(", max_output_tokens=128000)

    def test_empty_pattern_rejected(self):
        from pydantic import ValidationError

        from idp_common.config.models import ModelLimitEntry

        with pytest.raises(ValidationError, match="non-empty"):
            ModelLimitEntry(pattern="   ", max_output_tokens=128000)
