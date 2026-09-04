# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for agentic_idp.py — focusing on pure functions and state management
without requiring real Bedrock API calls.
"""

import contextvars
import threading
from typing import Any

import pytest
from pydantic import BaseModel, Field

# Check if strands-agents is actually available (not just a stub/mock)
try:
    from strands.types.agent import AgentInput  # noqa: F401

    from idp_common.extraction.agentic_idp import (
        SYSTEM_PROMPT,
        TABLE_PARSING_PROMPT_ADDENDUM,
        _accumulate_metering,
        _active_checkpoint_callback_var,
        _active_confidence_data_var,
        _build_model_config,
        _build_system_prompt,
        _get_inference_params,
        _prepare_prompt_content,
        apply_patches_to_data,
        detect_image_format,
        set_confidence_data,
        supports_tool_caching,
    )

    STRANDS_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    STRANDS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not STRANDS_AVAILABLE,
    reason="strands-agents package not installed",
)

# ──────────────────────────────────────────────────────────────
# Test fixtures / helper models
# ──────────────────────────────────────────────────────────────


class SimpleModel(BaseModel):
    name: str
    age: int


class TableModel(BaseModel):
    title: str = ""
    rows: list[dict[str, str]] = Field(default_factory=list, min_length=1)


class OptionalFieldModel(BaseModel):
    required_field: str
    optional_field: str | None = None
    with_default: str = "default_value"


# ──────────────────────────────────────────────────────────────
# Fix 1: Thread-safe ContextVar tests
# ──────────────────────────────────────────────────────────────


class TestContextVarThreadSafety:
    """Verify that ContextVar-based state is isolated between threads."""

    def test_checkpoint_callback_isolation_between_threads(self):
        """Two threads should not see each other's checkpoint callbacks."""
        results: dict[str, Any] = {}

        def thread_fn(thread_id: str, callback_value: str):
            _active_checkpoint_callback_var.set(callback_value)
            # Simulate some work
            import time

            time.sleep(0.01)
            results[thread_id] = _active_checkpoint_callback_var.get()

        t1 = threading.Thread(target=thread_fn, args=("t1", "callback_A"))
        t2 = threading.Thread(target=thread_fn, args=("t2", "callback_B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] == "callback_A"
        assert results["t2"] == "callback_B"

    def test_confidence_data_isolation_between_threads(self):
        """Two threads should not see each other's confidence data."""
        results: dict[str, Any] = {}

        def thread_fn(thread_id: str, data: dict[str, str] | None):
            set_confidence_data(data)
            import time

            time.sleep(0.01)
            results[thread_id] = _active_confidence_data_var.get()

        t1 = threading.Thread(target=thread_fn, args=("t1", {"page1": "data_A"}))
        t2 = threading.Thread(target=thread_fn, args=("t2", {"page1": "data_B"}))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] == {"page1": "data_A"}
        assert results["t2"] == {"page1": "data_B"}

    def test_set_confidence_data_none_clears(self):
        """Setting confidence data to None should clear it."""
        set_confidence_data({"page1": "data"})
        assert _active_confidence_data_var.get() is not None

        set_confidence_data(None)
        assert _active_confidence_data_var.get() is None

    def test_default_values_are_none(self):
        """Default ContextVar values should be None in fresh context."""
        ctx = contextvars.copy_context()
        result = ctx.run(lambda: _active_checkpoint_callback_var.get())
        # Default is None
        assert result is None


# ──────────────────────────────────────────────────────────────
# Fix 3: patch_buffer_data slice fix (tested via apply_patches_to_data)
# ──────────────────────────────────────────────────────────────


class TestApplyPatchesToData:
    def test_empty_patches_returns_original(self):
        data = {"name": "test", "value": 42}
        assert apply_patches_to_data(data, []) == data

    def test_replace_patch(self):
        data = {"name": "old", "count": 1}
        patches = [{"op": "replace", "path": "/name", "value": "new"}]
        result = apply_patches_to_data(data, patches)
        assert result["name"] == "new"
        assert result["count"] == 1

    def test_add_patch(self):
        data = {"name": "test"}
        patches = [{"op": "add", "path": "/age", "value": 25}]
        result = apply_patches_to_data(data, patches)
        assert result["age"] == 25

    def test_remove_patch(self):
        data = {"name": "test", "temp": "remove_me"}
        patches = [{"op": "remove", "path": "/temp"}]
        result = apply_patches_to_data(data, patches)
        assert "temp" not in result


# ──────────────────────────────────────────────────────────────
# Fix 6: Review agent deprecation
# ──────────────────────────────────────────────────────────────


class TestReviewAgentDeprecation:
    """The review agent config fields should be accepted but produce no effect."""

    def test_review_agent_config_accepted(self):
        """AgenticConfig should accept review_agent fields without error."""
        from idp_common.config.models import AgenticConfig

        config = AgenticConfig(
            enabled=True,
            review_agent=True,
            review_agent_model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        )
        assert config.review_agent is True
        assert config.review_agent_model is not None

    def test_review_agent_default_disabled(self):
        from idp_common.config.models import AgenticConfig

        config = AgenticConfig()
        assert config.review_agent is False
        assert config.review_agent_model is None


# ──────────────────────────────────────────────────────────────
# _build_system_prompt tests
# ──────────────────────────────────────────────────────────────


class TestBuildSystemPrompt:
    def test_includes_schema(self):
        prompt, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, SimpleModel)
        assert "Expected Schema:" in prompt
        assert '"name"' in schema_json
        assert '"age"' in schema_json

    def test_custom_instruction_appended(self):
        prompt, _ = _build_system_prompt(
            SYSTEM_PROMPT, "Focus on financial data only", SimpleModel
        )
        assert "Focus on financial data only" in prompt
        assert "Custom Instructions for this specific task:" in prompt

    def test_no_custom_instruction(self):
        prompt, _ = _build_system_prompt(SYSTEM_PROMPT, None, SimpleModel)
        assert "Custom Instructions for this specific task:" not in prompt


# ──────────────────────────────────────────────────────────────
# _build_model_config tests
# ──────────────────────────────────────────────────────────────


class TestBuildModelConfig:
    def test_claude4_max_tokens(self):
        config = _build_model_config(
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=300.0,
        )
        assert config["max_tokens"] == 64_000

    def test_claude3_max_tokens(self):
        config = _build_model_config(
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=300.0,
        )
        assert config["max_tokens"] == 8_192

    def test_nova_max_tokens(self):
        config = _build_model_config(
            "us.amazon.nova-pro-v1:0",
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=300.0,
        )
        assert config["max_tokens"] == 10_000

    def test_max_tokens_capped_at_model_max(self):
        config = _build_model_config(
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            max_tokens=100_000,  # Way over Claude 3's 8192
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=300.0,
        )
        assert config["max_tokens"] == 8_192

    def test_openai_responses_model_rejected(self):
        """OpenAI GPT-5.x models cannot use the agentic/Strands path."""
        with pytest.raises(ValueError, match="do not support agentic"):
            _build_model_config(
                "openai.gpt-5.4",
                max_tokens=None,
                max_retries=3,
                connect_timeout=10.0,
                read_timeout=300.0,
            )

    def test_max_tokens_respected_when_under_limit(self):
        config = _build_model_config(
            "us.anthropic.claude-sonnet-4-20250514-v1:0",
            max_tokens=4_096,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=300.0,
        )
        assert config["max_tokens"] == 4_096


# ──────────────────────────────────────────────────────────────
# _get_inference_params tests
# ──────────────────────────────────────────────────────────────


class TestGetInferenceParams:
    """`temperature` and `top_p` are mutually exclusive on Bedrock, and Claude
    4.7+ rejects both.

    These three tests spent their whole life skipped — ``strands`` was stubbed
    unconditionally, so the module-level guard import saw a MagicMock and
    reported the package as missing. In the meantime ``_get_inference_params``
    gained a required ``model_id`` and they had rotted into a ``TypeError``.
    """

    # Not Claude 4.7+, so the mutual-exclusion rules apply.
    SAMPLING_MODEL = "us.anthropic.claude-sonnet-4-6"

    def test_temperature_only(self):
        params = _get_inference_params(self.SAMPLING_MODEL, temperature=0.0, top_p=None)
        assert "temperature" in params
        assert "top_p" not in params

    def test_top_p_when_positive(self):
        params = _get_inference_params(self.SAMPLING_MODEL, temperature=0.5, top_p=0.9)
        assert "top_p" in params
        assert "temperature" not in params

    def test_top_p_zero_uses_temperature(self):
        """`top_p: 0.0` means "unset", not "top_p of zero" — a literal 0.0 would
        make the model degenerate, and temperature=0 is the recommended way to
        ask for deterministic output."""
        params = _get_inference_params(self.SAMPLING_MODEL, temperature=0.5, top_p=0.0)
        assert "temperature" in params
        assert "top_p" not in params

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-opus-4-7",
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-5",
        ],
    )
    def test_claude_4_7_plus_gets_no_sampling_params_at_all(self, model_id):
        """These models **reject** `temperature`/`top_p` outright, so passing
        either fails the ConverseStream call with "`top_p` is deprecated for this
        model" (#304). The helper must return an empty dict — the whole reason it
        takes a model_id, and the behavior these tests could not see while they
        were skipped."""
        assert _get_inference_params(model_id, temperature=0.0, top_p=0.9) == {}

    def test_a_nova_model_still_gets_sampling_params(self):
        """Guard-the-guard: the 4.7+ exclusion must not swallow every model."""
        params = _get_inference_params(
            "us.amazon.nova-lite-v1:0", temperature=0.0, top_p=None
        )
        assert params, "Nova accepts temperature; an empty dict would be wrong"


# ──────────────────────────────────────────────────────────────
# supports_*_caching tests
# ──────────────────────────────────────────────────────────────


class TestCachingSupport:
    def test_tool_caching_claude(self):
        assert supports_tool_caching("us.anthropic.claude-sonnet-4-20250514-v1:0")
        assert supports_tool_caching("anthropic.claude-3-5-sonnet-20241022-v2:0")

    def test_tool_caching_not_nova(self):
        assert not supports_tool_caching("us.amazon.nova-pro-v1:0")
        assert not supports_tool_caching("amazon.nova-lite-v1:0")


# ──────────────────────────────────────────────────────────────
# _prepare_prompt_content tests
# ──────────────────────────────────────────────────────────────


class TestPreparePromptContent:
    def test_text_prompt(self):
        content = _prepare_prompt_content("Extract this text", None, None)
        assert any("Extract this text" in str(c) for c in content)

    def test_dict_prompt_with_content(self):
        msg = {"content": [{"text": "hello"}]}
        content = _prepare_prompt_content(msg, None, None)
        assert len(content) >= 1

    def test_existing_data_adds_resume_instructions(self):
        existing = SimpleModel(name="test", age=25)
        content = _prepare_prompt_content("Extract data", None, existing)
        text_content = str(content)
        assert "RESUME FROM CHECKPOINT" in text_content

    def test_cache_point_added_at_end(self):
        content = _prepare_prompt_content("test", None, None)
        # Last block should be a cachePoint
        last = content[-1]
        assert "cachePoint" in last or "cachepoint" in str(last).lower()


# ──────────────────────────────────────────────────────────────
# Table parsing prompt addendum tests
# ──────────────────────────────────────────────────────────────


class TestTableParsingPromptAddendum:
    def test_addendum_has_placeholders(self):
        formatted = TABLE_PARSING_PROMPT_ADDENDUM.format(
            min_parse_success_rate=0.90,
            min_confidence_threshold=95.0,
        )
        assert "0.9" in formatted
        assert "95.0" in formatted
        assert "parse_table" in formatted
        assert "map_table_to_schema" in formatted
        assert "finalize_table_extraction" in formatted


# ──────────────────────────────────────────────────────────────
# Schema constraint flow-through tests
# ──────────────────────────────────────────────────────────────


class TestSchemaConstraintFlowThrough:
    """Verify that Pydantic model constraints are reflected in the schema
    that gets embedded in the system prompt."""

    def test_min_length_in_schema(self):
        """min_length constraint on list field should appear in JSON schema."""

        class WithMinLength(BaseModel):
            items: list[str] = Field(min_length=5)

        _, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, WithMinLength)
        assert '"minItems": 5' in schema_json

    def test_required_fields_in_schema(self):
        """Required fields should be listed in JSON schema."""
        _, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, SimpleModel)
        assert '"required"' in schema_json

    def test_optional_field_not_required(self):
        """Optional fields should not be in required list."""
        _, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, OptionalFieldModel)
        assert '"required_field"' in schema_json
        # optional_field should not appear in required array
        import json

        schema = json.loads(schema_json)
        required = schema.get("required", [])
        assert "required_field" in required
        assert "optional_field" not in required

    def test_default_values_in_schema(self):
        """Default values should appear in JSON schema."""
        _, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, OptionalFieldModel)
        assert '"default_value"' in schema_json

    def test_field_descriptions_in_schema(self):
        """Field descriptions should be embedded in the schema."""

        class Described(BaseModel):
            amount: float = Field(description="Total transaction amount in USD")

        _, schema_json = _build_system_prompt(SYSTEM_PROMPT, None, Described)
        assert "Total transaction amount in USD" in schema_json


# ──────────────────────────────────────────────────────────────
# detect_image_format tests
# ──────────────────────────────────────────────────────────────


class TestDetectImageFormat:
    def test_jpeg_detection(self):
        import io

        from PIL import Image

        img = Image.new("RGB", (10, 10), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        assert detect_image_format(buf.getvalue()) == "jpeg"

    def test_png_detection(self):
        import io

        from PIL import Image

        img = Image.new("RGB", (10, 10), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        assert detect_image_format(buf.getvalue()) == "png"

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported image format"):
            import io

            from PIL import Image

            img = Image.new("RGB", (10, 10))
            buf = io.BytesIO()
            img.save(buf, format="BMP")
            detect_image_format(buf.getvalue())


# ──────────────────────────────────────────────────────────────
# Metering merge (issue #337): guard against None token values
# ──────────────────────────────────────────────────────────────


class TestAccumulateMetering:
    def test_sums_integer_values(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"model": {"inputTokens": 3, "outputTokens": 2}})
        _accumulate_metering(merged, {"model": {"inputTokens": 5, "outputTokens": 1}})
        assert merged == {"model": {"inputTokens": 8, "outputTokens": 3}}

    def test_none_in_seeded_batch_does_not_raise(self):
        # Issue #337: the first batch seeds merged_metering verbatim, so a
        # stored None must not crash the addition of a later batch's value.
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"model": {"inputTokens": None}})
        _accumulate_metering(merged, {"model": {"inputTokens": 5}})
        assert merged == {"model": {"inputTokens": 5}}

    def test_none_in_incoming_batch_does_not_raise(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"model": {"inputTokens": 5}})
        _accumulate_metering(merged, {"model": {"inputTokens": None}})
        assert merged == {"model": {"inputTokens": 5}}

    def test_none_on_both_sides(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"model": {"inputTokens": None}})
        _accumulate_metering(merged, {"model": {"inputTokens": None}})
        assert merged == {"model": {"inputTokens": 0}}

    def test_new_token_key_in_later_batch(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"model": {"inputTokens": 4}})
        _accumulate_metering(merged, {"model": {"outputTokens": 2}})
        assert merged == {"model": {"inputTokens": 4, "outputTokens": 2}}


# ---------------------------------------------------------------------------
# xAI Grok on the agentic path
# ---------------------------------------------------------------------------
# Grok reaches Converse and supports tool use, so unlike GPT-5.x it IS allowed
# for agentic extraction. But it rejects the sampling group and uses a different
# reasoning-effort carrier than Claude, and this path builds its own request
# params rather than going through BedrockClient.invoke_model. A live run caught
# it forwarding `top_p` (a 400: "This model doesn't support the topP field").

GROK_US = "us.xai.grok-4.6"
GROK_GLOBAL = "global.xai.grok-4.6"


class TestGrokAgenticPath:
    @pytest.mark.parametrize("model_id", [GROK_US, GROK_GLOBAL])
    def test_sampling_params_are_omitted_for_grok(self, model_id):
        """The config default top_p=0.1 must not reach the Strands BedrockModel."""
        assert _get_inference_params(model_id, 0.0, 0.1) == {}

    def test_sampling_params_still_forwarded_for_nova(self):
        assert _get_inference_params("us.amazon.nova-pro-v1:0", 0.0, 0.1) == {
            "top_p": 0.1
        }

    def test_claude_4_7_still_omits_sampling_params(self):
        assert _get_inference_params("us.anthropic.claude-opus-4-8", 0.0, 0.1) == {}

    def test_grok_gets_no_tool_caching(self):
        assert supports_tool_caching(GROK_US) is False

    @pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
    def test_effort_uses_the_grok_reasoning_carrier(self, effort):
        config = _build_model_config(
            model_id=GROK_US,
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=60.0,
            reasoning_effort=effort,
        )
        arf = config.get("additional_request_fields") or {}
        assert arf.get("reasoning") == {"effort": effort}
        # Claude's carrier is silently ignored by Grok — it must not be used.
        assert "output_config" not in arf

    def test_effort_max_is_dropped_for_grok(self):
        """Grok 400s on 'max', and unknown fields are silently ignored, so an
        out-of-vocabulary value must be dropped rather than forwarded."""
        config = _build_model_config(
            model_id=GROK_US,
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=60.0,
            reasoning_effort="max",
        )
        arf = config.get("additional_request_fields") or {}
        assert "reasoning" not in arf

    def test_claude_still_uses_output_config_carrier(self):
        """Regression guard: adding Grok must not move Claude's effort carrier."""
        config = _build_model_config(
            model_id="us.anthropic.claude-sonnet-5",
            max_tokens=None,
            max_retries=3,
            connect_timeout=10.0,
            read_timeout=60.0,
            reasoning_effort="high",
        )
        arf = config.get("additional_request_fields") or {}
        assert arf.get("output_config") == {"effort": "high"}
        assert "reasoning" not in arf


class TestAgenticCachePointGating:
    """The agentic prompt builder appended a trailing cachePoint unconditionally.

    That is not a mere optimization: Bedrock rejects the ENTIRE request for a
    model that does not support prompt caching, with
    "AccessDeniedException: You invoked an unsupported model or your request did
    not allow prompt caching". Grok is such a model, so the unconditional append
    failed EVERY document in agentic extraction — found on live stack IDP1, not
    by any unit test, because the earlier tests only checked that
    _build_model_config emitted no cache CONFIG (a different site).
    """

    @staticmethod
    def _has_cachepoint(blocks):
        return any(
            isinstance(b, dict) and b.get("cachePoint", {}).get("type") == "default"
            for b in blocks
        )

    def test_no_cachepoint_for_grok(self):
        blocks = _prepare_prompt_content("Extract this", None, None, model_id=GROK_US)
        assert not self._has_cachepoint(blocks)

    def test_no_cachepoint_for_grok_global(self):
        blocks = _prepare_prompt_content(
            "Extract this", None, None, model_id=GROK_GLOBAL
        )
        assert not self._has_cachepoint(blocks)

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-opus-4-8",
            "us.amazon.nova-lite-v1:0",
        ],
    )
    def test_cachepoint_kept_for_caching_models(self, model_id):
        """Regression guard: the fix must not disable caching for models that
        DO support it — that would be a silent cost increase."""
        blocks = _prepare_prompt_content("Extract this", None, None, model_id=model_id)
        assert self._has_cachepoint(blocks)

    def test_cachepoint_kept_when_model_id_omitted(self):
        """Back-compat for callers that don't pass model_id."""
        blocks = _prepare_prompt_content("Extract this", None, None)
        assert self._has_cachepoint(blocks)

    def test_task_description_marker_still_appended(self):
        """The 'end of your main task description' block must survive for both."""
        for mid in (GROK_US, "us.anthropic.claude-sonnet-5"):
            blocks = _prepare_prompt_content("Extract this", None, None, model_id=mid)
            texts = [b.get("text") for b in blocks if isinstance(b, dict)]
            assert "end of your main task description" in texts
