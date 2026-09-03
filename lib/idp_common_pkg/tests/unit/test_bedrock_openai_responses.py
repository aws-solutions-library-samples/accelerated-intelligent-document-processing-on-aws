# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the OpenAI Responses (bedrock-mantle) backend."""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bedrock import openai_responses as oar
from idp_common.bedrock.client import BedrockClient


def _make_http_response(status_code, payload):
    """Build a fake botocore HTTP response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(payload) if isinstance(payload, dict) else payload
    return resp


_SAMPLE_RESPONSE = {
    "status": "completed",
    "output": [
        {"type": "reasoning", "content": []},
        {
            "type": "message",
            "content": [
                {"type": "output_text", "text": "Hello "},
                {"type": "output_text", "text": "world"},
            ],
        },
    ],
    "usage": {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "input_tokens_details": {"cached_tokens": 25},
        "output_tokens_details": {"reasoning_tokens": 12},
    },
}


@pytest.mark.unit
class TestModelDetection:
    def test_detects_gpt_5_4(self):
        assert oar.is_openai_responses_model("openai.gpt-5.4") is True

    def test_detects_gpt_5_5(self):
        assert oar.is_openai_responses_model("openai.gpt-5.5") is True

    def test_detects_future_gpt_5_variant(self):
        assert oar.is_openai_responses_model("openai.gpt-5.6") is True

    def test_detects_gpt_5_6_sol(self):
        assert oar.is_openai_responses_model("openai.gpt-5.6-sol") is True

    def test_detects_gpt_5_6_terra(self):
        assert oar.is_openai_responses_model("openai.gpt-5.6-terra") is True

    def test_detects_gpt_5_6_luna(self):
        assert oar.is_openai_responses_model("openai.gpt-5.6-luna") is True

    def test_rejects_claude(self):
        assert oar.is_openai_responses_model("us.anthropic.claude-opus-4-8") is False

    def test_rejects_nova(self):
        assert oar.is_openai_responses_model("us.amazon.nova-pro-v1:0") is False

    def test_rejects_none(self):
        assert oar.is_openai_responses_model(None) is False


@pytest.mark.unit
class TestRegionResolution:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MANTLE_REGION", "us-west-2")
        assert oar.resolve_mantle_region("openai.gpt-5.5", "us-east-1") == "us-west-2"

    def test_uses_configured_region_when_available(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MANTLE_REGION", raising=False)
        assert oar.resolve_mantle_region("openai.gpt-5.4", "us-west-2") == "us-west-2"

    def test_falls_back_for_gpt_5_5(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MANTLE_REGION", raising=False)
        # gpt-5.5 is in us-east-1/us-east-2; an unavailable region falls back to
        # the per-model default (us-east-1).
        assert oar.resolve_mantle_region("openai.gpt-5.5", "eu-west-1") == "us-east-1"

    def test_falls_back_to_gov_region(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MANTLE_REGION", raising=False)
        assert (
            oar.resolve_mantle_region("openai.gpt-5.4", "us-gov-east-1")
            == "us-gov-west-1"
        )

    def test_gpt_5_6_terra_available_in_us_west_2(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MANTLE_REGION", raising=False)
        assert (
            oar.resolve_mantle_region("openai.gpt-5.6-terra", "us-west-2")
            == "us-west-2"
        )

    def test_gpt_5_6_sol_not_in_us_west_2_falls_back(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MANTLE_REGION", raising=False)
        # Sol is only in us-east-1/us-east-2; us-west-2 is not available for it.
        assert (
            oar.resolve_mantle_region("openai.gpt-5.6-sol", "us-west-2") == "us-east-1"
        )


@pytest.mark.unit
class TestRequestTranslation:
    def test_text_and_image_translation(self):
        body = oar.build_responses_request(
            system_prompt="You are helpful",
            content=[
                {"text": "extract <<CACHEPOINT>> fields"},
                {"image": {"format": "png", "source": {"bytes": b"abc"}}},
            ],
            max_tokens=500,
            model_id="openai.gpt-5.4",
        )
        assert body["model"] == "openai.gpt-5.4"
        assert body["instructions"] == "You are helpful"
        assert body["max_output_tokens"] == 500
        # Defaults to medium when no reasoning_effort given
        assert body["reasoning"]["effort"] == "medium"
        # No sampling parameters
        assert "temperature" not in body
        assert "top_p" not in body
        assert "top_k" not in body

        items = body["input"][0]["content"]
        text_items = [i for i in items if i["type"] == "input_text"]
        image_items = [i for i in items if i["type"] == "input_image"]
        assert len(text_items) == 1
        assert "<<CACHEPOINT>>" not in text_items[0]["text"]
        assert len(image_items) == 1
        assert image_items[0]["image_url"].startswith("data:image/png;base64,")

    def test_max_tokens_capped_to_model_limit(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=10_000_000,
            model_id="openai.gpt-5.4",
        )
        # Capped to the model_config_limits value (128000)
        assert body["max_output_tokens"] == 128000

    def test_cachepoint_block_skipped(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}, {"cachePoint": {"type": "default"}}],
            max_tokens=100,
            model_id="openai.gpt-5.5",
        )
        items = body["input"][0]["content"]
        assert all("cachePoint" not in str(i) for i in items)
        assert len(items) == 1

    def test_gpt_5_6_cachepoint_marker_emits_explicit_breakpoint(self):
        body = oar.build_responses_request(
            system_prompt="You are helpful",
            content=[
                {"text": "static instructions <<CACHEPOINT>>"},
                {"text": "dynamic question"},
            ],
            max_tokens=100,
            model_id="openai.gpt-5.6-sol",
        )
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["prompt_cache_key"].startswith("idp:")
        items = body["input"][0]["content"]
        # Marker stripped from the text, breakpoint attached to the first block.
        assert "<<CACHEPOINT>>" not in items[0]["text"]
        assert items[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        # The second (dynamic) block carries no breakpoint.
        assert "prompt_cache_breakpoint" not in items[1]

    def test_gpt_5_6_cachepoint_block_emits_explicit_breakpoint(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "prefix"}, {"cachePoint": {"type": "default"}}],
            max_tokens=100,
            model_id="openai.gpt-5.6-terra",
        )
        items = body["input"][0]["content"]
        assert len(items) == 1  # cachePoint block itself is not emitted
        assert items[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert body["prompt_cache_options"] == {"mode": "explicit"}

    def test_gpt_5_5_does_not_emit_explicit_cache_fields(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "prefix <<CACHEPOINT>>"}, {"text": "rest"}],
            max_tokens=100,
            model_id="openai.gpt-5.5",
        )
        # 5.5 caches automatically: no explicit fields, marker stripped.
        assert "prompt_cache_options" not in body
        assert "prompt_cache_key" not in body
        items = body["input"][0]["content"]
        assert "<<CACHEPOINT>>" not in items[0]["text"]
        assert all("prompt_cache_breakpoint" not in i for i in items)

    def test_gpt_5_6_no_marker_no_cache_fields(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "no marker here"}],
            max_tokens=100,
            model_id="openai.gpt-5.6-luna",
        )
        assert "prompt_cache_options" not in body
        assert "prompt_cache_key" not in body

    def test_cache_key_stable_for_identical_prefix_and_differs_otherwise(self):
        def key_for(prefix_text, model="openai.gpt-5.6-sol", system="sys"):
            body = oar.build_responses_request(
                system_prompt=system,
                content=[{"text": f"{prefix_text} <<CACHEPOINT>>"}, {"text": "q"}],
                max_tokens=100,
                model_id=model,
            )
            return body["prompt_cache_key"]

        assert key_for("same prefix") == key_for("same prefix")
        assert key_for("prefix A") != key_for("prefix B")
        # Different system prompt → different key.
        assert key_for("p", system="sys1") != key_for("p", system="sys2")

    def test_reasoning_effort_passed_through(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            reasoning_effort="high",
        )
        assert body["reasoning"]["effort"] == "high"

    def test_reasoning_effort_invalid_falls_back_to_default(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            reasoning_effort="turbo",  # not a valid level
        )
        assert body["reasoning"]["effort"] == oar.DEFAULT_REASONING_EFFORT

    def test_reasoning_effort_none_uses_default(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            reasoning_effort=None,
        )
        assert body["reasoning"]["effort"] == "medium"


@pytest.mark.unit
class TestResponseTranslation:
    def test_text_extraction_and_usage_mapping(self):
        result = oar.translate_response(
            _SAMPLE_RESPONSE, "openai.gpt-5.4", "Extraction"
        )
        text = result["response"]["output"]["message"]["content"][0]["text"]
        assert text == "Hello world"

        usage = result["response"]["usage"]
        # OpenAI input_tokens (100) is the TOTAL and includes cached_tokens (25);
        # inputTokens is reported as the DISJOINT fresh count (100 - 25 = 75) so
        # cached tokens are not billed at both the input and cache-read rate.
        assert usage == {
            "inputTokens": 75,
            "outputTokens": 40,
            "totalTokens": 140,
            "cacheReadInputTokens": 25,
            "cacheWriteInputTokens": 0,
        }

        metering = result["metering"]["Extraction/bedrock/openai.gpt-5.4"]
        assert metering["requests"] == 1
        assert metering["inputTokens"] == 75
        # reasoning_tokens must NOT be a metering key
        assert "reasoning_tokens" not in metering
        assert "reasoningTokens" not in metering

    def test_extract_text_from_response_compatibility(self):
        result = oar.translate_response(
            _SAMPLE_RESPONSE, "openai.gpt-5.4", "Extraction"
        )
        client = BedrockClient(metrics_enabled=False)
        assert client.extract_text_from_response(result) == "Hello world"

    def test_reasoning_tokens_helper(self):
        assert oar._reasoning_tokens(_SAMPLE_RESPONSE) == 12

    def test_map_usage_reports_cache_write_tokens_when_present(self):
        payload = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "input_tokens_details": {
                    "cached_tokens": 25,
                    "cache_creation_tokens": 60,
                },
            }
        }
        usage = oar._map_usage(payload)
        assert usage["cacheReadInputTokens"] == 25
        assert usage["cacheWriteInputTokens"] == 60
        # Fresh input = total(100) - cached(25) - cache_write(60) = 15
        assert usage["inputTokens"] == 15

    def test_map_usage_cache_write_defaults_zero(self):
        usage = oar._map_usage(_SAMPLE_RESPONSE)
        assert usage["cacheWriteInputTokens"] == 0

    def test_map_usage_disjoint_token_accounting_matches_converse(self):
        """OpenAI input_tokens is a TOTAL; we report disjoint fresh input so the
        cost model does not bill cached tokens twice (input rate + cache rate).

        Mirrors the live-observed GPT-5.6 warm-cache case: input_tokens 4508 with
        cached_tokens 3193 must yield fresh inputTokens 1315.
        """
        warm = {
            "usage": {
                "input_tokens": 4508,
                "output_tokens": 558,
                "total_tokens": 5066,
                "input_tokens_details": {"cached_tokens": 3193},
            }
        }
        u = oar._map_usage(warm)
        assert u["inputTokens"] == 1315
        assert u["cacheReadInputTokens"] == 3193
        # Fresh + cache-read reconstructs the original prompt total.
        assert u["inputTokens"] + u["cacheReadInputTokens"] == 4508

    def test_map_usage_never_negative_fresh_input(self):
        # Defensive: if cache counts exceed input_tokens, clamp fresh at 0.
        payload = {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 6},
            }
        }
        assert oar._map_usage(payload)["inputTokens"] == 0


@pytest.mark.unit
class TestInvocationAndRouting:
    @pytest.fixture
    def client(self):
        c = BedrockClient(region="us-east-2", metrics_enabled=False)
        return c

    def _patch_session(self):
        """Patch get_bedrock_session to provide dummy credentials."""
        fake_session = MagicMock()
        creds = MagicMock()
        creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKIA",
            secret_key="secret",
            token=None,  # nosec B106 - dummy test credential
        )
        fake_session.get_credentials.return_value = creds
        return patch.object(oar, "get_bedrock_session", return_value=fake_session)

    def test_invoke_model_routes_to_responses_backend(self, client):
        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
        ):
            mock_send.return_value = _make_http_response(200, _SAMPLE_RESPONSE)

            result = client.invoke_model(
                model_id="openai.gpt-5.4",
                system_prompt="sys",
                content=[{"text": "hi"}],
            )

        assert (
            result["response"]["output"]["message"]["content"][0]["text"]
            == "Hello world"
        )
        assert "Unspecified/bedrock/openai.gpt-5.4" in result["metering"]

        # Verify SigV4 Authorization header and URL on the prepared request.
        prepared = mock_send.call_args.args[0]
        assert "us-east-2" in prepared.url
        assert prepared.url.endswith("/openai/v1/responses")
        auth = prepared.headers.get("Authorization", "")
        assert "AWS4-HMAC-SHA256" in auth
        assert "bedrock-mantle" in auth  # signing service name in credential scope

    def test_invoke_model_forwards_reasoning_effort(self, client):
        import json as _json

        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
        ):
            mock_send.return_value = _make_http_response(200, _SAMPLE_RESPONSE)

            client.invoke_model(
                model_id="openai.gpt-5.5",
                system_prompt="sys",
                content=[{"text": "hi"}],
                reasoning_effort="high",
            )

        prepared = mock_send.call_args.args[0]
        sent_body = _json.loads(prepared.body)
        assert sent_body["reasoning"]["effort"] == "high"

    def test_retry_on_429_then_success(self, client):
        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
            patch.object(oar.time, "sleep"),
        ):
            mock_send.side_effect = [
                _make_http_response(429, {"message": "throttled"}),
                _make_http_response(200, _SAMPLE_RESPONSE),
            ]
            result = client.invoke_model(
                model_id="openai.gpt-5.4",
                system_prompt="sys",
                content=[{"text": "hi"}],
            )
        assert mock_send.call_count == 2
        # _SAMPLE_RESPONSE: input_tokens 100 total, cached_tokens 25 → fresh 75.
        assert result["response"]["usage"]["inputTokens"] == 75

    def test_raises_after_max_retries(self, client):
        client.max_retries = 2
        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
            patch.object(oar.time, "sleep"),
        ):
            mock_send.return_value = _make_http_response(500, {"message": "boom"})
            with pytest.raises(RuntimeError):
                client.invoke_model(
                    model_id="openai.gpt-5.4",
                    system_prompt="sys",
                    content=[{"text": "hi"}],
                )
        assert mock_send.call_count == 2

    def test_terminal_4xx_not_retried(self, client):
        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
            patch.object(oar.time, "sleep"),
        ):
            mock_send.return_value = _make_http_response(400, {"message": "bad"})
            with pytest.raises(RuntimeError):
                client.invoke_model(
                    model_id="openai.gpt-5.4",
                    system_prompt="sys",
                    content=[{"text": "hi"}],
                )
        assert mock_send.call_count == 1


_SIMPLE_SCHEMA = {
    "type": "object",
    "title": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "properties": {
        "invoice_number": {"type": "string", "x-aws-idp-original-name": "Invoice #"},
        "total": {"type": "number"},
    },
    "required": ["invoice_number"],
}


@pytest.mark.unit
class TestStrictSchemaNormalization:
    def test_adds_additional_properties_and_completes_required(self):
        strict = oar.to_strict_json_schema(_SIMPLE_SCHEMA)
        assert strict["additionalProperties"] is False
        assert strict["required"] == ["invoice_number", "total"]

    def test_optional_property_widened_to_nullable(self):
        strict = oar.to_strict_json_schema(_SIMPLE_SCHEMA)
        # invoice_number was already required -> type untouched.
        assert strict["properties"]["invoice_number"]["type"] == "string"
        # total was optional -> becomes a nullable union so strict mode can
        # require it without changing semantics.
        assert strict["properties"]["total"]["type"] == ["number", "null"]

    def test_strips_vendor_extension_keys(self):
        strict = oar.to_strict_json_schema(_SIMPLE_SCHEMA)
        assert "x-aws-idp-document-type" not in strict
        assert "x-aws-idp-original-name" not in strict["properties"]["invoice_number"]
        # Non-extension metadata is preserved.
        assert strict["title"] == "Invoice"

    def test_does_not_mutate_input(self):
        original = json.loads(json.dumps(_SIMPLE_SCHEMA))
        oar.to_strict_json_schema(_SIMPLE_SCHEMA)
        assert _SIMPLE_SCHEMA == original

    def test_idempotent(self):
        once = oar.to_strict_json_schema(_SIMPLE_SCHEMA)
        twice = oar.to_strict_json_schema(once)
        assert once == twice

    def test_recurses_into_nested_objects_items_defs_and_anyof(self):
        schema = {
            "type": "object",
            "properties": {
                "header": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"amount": {"type": "number"}},
                        "required": ["amount"],
                    },
                },
                "either": {
                    "anyOf": [
                        {"type": "object", "properties": {"a": {"type": "string"}}},
                        {"type": "null"},
                    ]
                },
            },
            "$defs": {
                "Party": {"type": "object", "properties": {"name": {"type": "string"}}}
            },
        }
        strict = oar.to_strict_json_schema(schema)
        props = strict["properties"]
        assert props["header"]["additionalProperties"] is False
        assert props["header"]["required"] == ["id"]
        items = props["lines"]["items"]
        assert items["additionalProperties"] is False
        assert items["required"] == ["amount"]
        assert props["either"]["anyOf"][0]["additionalProperties"] is False
        assert strict["$defs"]["Party"]["additionalProperties"] is False
        assert strict["$defs"]["Party"]["required"] == ["name"]

    def test_ref_property_left_alone(self):
        # A bare $ref cannot be widened to nullable without rewriting it, so it
        # stays required. Documented caveat, asserted so it cannot regress silently.
        schema = {
            "type": "object",
            "properties": {"party": {"$ref": "#/$defs/Party"}},
            "$defs": {"Party": {"type": "object", "properties": {}}},
        }
        strict = oar.to_strict_json_schema(schema)
        assert strict["properties"]["party"] == {"$ref": "#/$defs/Party"}
        assert strict["required"] == ["party"]

    def test_existing_nullable_union_not_duplicated(self):
        schema = {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}
        strict = oar.to_strict_json_schema(schema)
        assert strict["properties"]["a"]["type"] == ["string", "null"]

    def test_non_dict_schema_raises(self):
        with pytest.raises(ValueError, match="JSON-Schema dict"):
            oar.to_strict_json_schema(["not", "a", "schema"])

    def test_non_object_root_raises(self):
        with pytest.raises(ValueError, match="root must be a JSON-Schema object"):
            oar.build_output_text_format({"type": "array", "items": {}})


@pytest.mark.unit
class TestOutputSchemaRequestBuilding:
    def test_no_output_schema_leaves_body_untouched(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
        )
        # The exact key the feature adds must be absent by default.
        assert "text" not in body
        assert "response_format" not in body
        assert set(body) == {
            "model",
            "input",
            "reasoning",
            "stream",
            "store",
            "instructions",
            "max_output_tokens",
        }

    def test_explicit_none_is_identical_to_omitted(self):
        kwargs = dict(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
        )
        assert oar.build_responses_request(**kwargs) == oar.build_responses_request(
            **kwargs, output_schema=None, output_schema_name="ignored"
        )

    def test_output_schema_emits_nested_text_format_with_strict_true(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            output_schema=_SIMPLE_SCHEMA,
            output_schema_name="invoice",
        )
        # Responses API shape is text.format — NOT the Chat Completions
        # response_format.
        assert "response_format" not in body
        fmt = body["text"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["name"] == "invoice"
        assert fmt["strict"] is True
        assert fmt["schema"]["additionalProperties"] is False
        assert fmt["schema"]["required"] == ["invoice_number", "total"]

    def test_output_schema_does_not_disturb_other_fields(self):
        body = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "prefix <<CACHEPOINT>>"}, {"text": "q"}],
            max_tokens=100,
            model_id="openai.gpt-5.6-sol",
            reasoning_effort="high",
            output_schema=_SIMPLE_SCHEMA,
        )
        assert body["reasoning"]["effort"] == "high"
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert body["max_output_tokens"] == 100
        assert "temperature" not in body

    def test_schema_name_defaults_and_is_sanitized(self):
        default = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            output_schema=_SIMPLE_SCHEMA,
        )
        assert default["text"]["format"]["name"] == oar.DEFAULT_OUTPUT_SCHEMA_NAME

        dirty = oar.build_responses_request(
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            model_id="openai.gpt-5.4",
            output_schema=_SIMPLE_SCHEMA,
            output_schema_name="Bank Statement / v2",
        )
        assert dirty["text"]["format"]["name"] == "Bank_Statement___v2"

    def test_invalid_schema_raises_before_request(self):
        with pytest.raises(ValueError):
            oar.build_responses_request(
                system_prompt="sys",
                content=[{"text": "hi"}],
                max_tokens=100,
                model_id="openai.gpt-5.4",
                output_schema="not a dict",
            )


_REFUSAL_RESPONSE = {
    "status": "completed",
    "output": [
        {
            "type": "message",
            "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
        }
    ],
    "usage": {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "input_tokens_details": {"cached_tokens": 25},
    },
}

_INCOMPLETE_RESPONSE = {
    "status": "incomplete",
    "incomplete_details": {"reason": "max_output_tokens"},
    "output": [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": '{"invoice_number": "A-1'}],
        }
    ],
    "usage": {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "input_tokens_details": {"cached_tokens": 25},
    },
}


@pytest.mark.unit
class TestOutputSchemaInvocation:
    @pytest.fixture
    def client(self):
        return BedrockClient(region="us-east-2", metrics_enabled=False)

    def _patch_session(self):
        fake_session = MagicMock()
        creds = MagicMock()
        creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKIA",
            secret_key="secret",
            token=None,  # nosec B106 - dummy test credential
        )
        fake_session.get_credentials.return_value = creds
        return patch.object(oar, "get_bedrock_session", return_value=fake_session)

    def _invoke(self, client, payload, status=200, **kwargs):
        with (
            self._patch_session(),
            patch.object(oar.URLLib3Session, "send") as mock_send,
            patch.object(oar.time, "sleep"),
        ):
            mock_send.return_value = _make_http_response(status, payload)
            result = oar.invoke_responses_api(
                client=client,
                model_id="openai.gpt-5.4",
                system_prompt="sys",
                content=[{"text": "hi"}],
                max_tokens=100,
                max_retries=1,
                context="Extraction",
                **kwargs,
            )
        return result, mock_send

    def test_schema_is_sent_on_the_wire(self, client):
        _, mock_send = self._invoke(
            client,
            _SAMPLE_RESPONSE,
            output_schema=_SIMPLE_SCHEMA,
            output_schema_name="invoice",
        )
        sent = json.loads(mock_send.call_args.args[0].body)
        assert sent["text"]["format"]["strict"] is True
        assert sent["text"]["format"]["name"] == "invoice"

    def test_no_schema_sends_no_text_key(self, client):
        _, mock_send = self._invoke(client, _SAMPLE_RESPONSE)
        sent = json.loads(mock_send.call_args.args[0].body)
        assert "text" not in sent

    def test_response_structure_and_metering_unchanged_with_schema(self, client):
        with_schema, _ = self._invoke(
            client, _SAMPLE_RESPONSE, output_schema=_SIMPLE_SCHEMA
        )
        without, _ = self._invoke(client, _SAMPLE_RESPONSE)
        # The {"response": ..., "metering": ...} contract is identical either way.
        assert with_schema == without
        assert with_schema["response"]["usage"]["inputTokens"] == 75
        assert (
            with_schema["metering"]["Extraction/bedrock/openai.gpt-5.4"]["requests"]
            == 1
        )

    def test_refusal_raises_with_metering_attached(self, client):
        with pytest.raises(oar.OutputRefusalError) as exc:
            self._invoke(client, _REFUSAL_RESPONSE, output_schema=_SIMPLE_SCHEMA)
        assert "I cannot help with that." in str(exc.value)
        assert exc.value.refusal == "I cannot help with that."
        # The request was billed, so the metering must survive the raise.
        assert (
            exc.value.metering["Extraction/bedrock/openai.gpt-5.4"]["inputTokens"] == 75
        )
        assert isinstance(exc.value, RuntimeError)

    def test_refusal_without_schema_keeps_legacy_behavior(self, client):
        # Byte-identical legacy path: no exception, empty text, metering intact.
        result, _ = self._invoke(client, _REFUSAL_RESPONSE)
        assert result["response"]["output"]["message"]["content"][0]["text"] == ""
        assert result["response"]["usage"]["inputTokens"] == 75

    def test_incomplete_raises(self, client):
        with pytest.raises(oar.IncompleteOutputError) as exc:
            self._invoke(client, _INCOMPLETE_RESPONSE, output_schema=_SIMPLE_SCHEMA)
        assert exc.value.reason == "max_output_tokens"
        assert exc.value.metering
        assert isinstance(exc.value, RuntimeError)

    def test_incomplete_without_schema_keeps_legacy_behavior(self, client):
        result, _ = self._invoke(client, _INCOMPLETE_RESPONSE)
        assert result["response"]["stopReason"] == "incomplete"
        assert result["response"]["usage"]["inputTokens"] == 75

    def test_endpoint_rejection_surfaces_as_schema_error(self, client):
        payload = {
            "error": {
                "message": "Invalid schema for response_format: "
                "'additionalProperties' is required to be false",
                "type": "invalid_request_error",
            }
        }
        with pytest.raises(oar.OutputSchemaRejectedError) as exc:
            self._invoke(client, payload, status=400, output_schema=_SIMPLE_SCHEMA)
        # The endpoint's own error text is preserved, not swallowed.
        assert "additionalProperties" in str(exc.value)
        assert "HTTP 400" in str(exc.value)
        assert isinstance(exc.value, RuntimeError)

    def test_terminal_4xx_without_schema_is_plain_runtime_error(self, client):
        with pytest.raises(RuntimeError) as exc:
            self._invoke(client, {"message": "bad"}, status=400)
        assert not isinstance(exc.value, oar.OutputSchemaRejectedError)

    def test_refusal_and_incomplete_helpers(self):
        assert oar._extract_refusal(_SAMPLE_RESPONSE) is None
        assert oar._extract_refusal(_REFUSAL_RESPONSE) == "I cannot help with that."
        assert oar._extract_refusal({"refusal": "top level"}) == "top level"
        assert oar._incomplete_reason(_SAMPLE_RESPONSE) is None
        assert oar._incomplete_reason(_INCOMPLETE_RESPONSE) == "max_output_tokens"
        assert oar._incomplete_reason({"status": "incomplete"}) == "unknown"


_SSE_STREAM = (
    'data: {"type":"response.created"}\n\n'
    "event: response.output_text.delta\n"
    'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
    'data: {"type":"response.output_text.delta","delta":", world"}\n\n'
    'data: {"type":"response.output_text.delta","delta":"!"}\n\n'
    'data: {"type":"response.completed","response":{"usage":{'
    '"input_tokens":12,"output_tokens":5,"total_tokens":17,'
    '"input_tokens_details":{"cached_tokens":4},'
    '"output_tokens_details":{"reasoning_tokens":3}}}}\n\n'
)


def _fake_urllib3_response(status, body_text):
    """Build a fake urllib3 HTTPResponse for the streaming path."""
    resp = MagicMock()
    resp.status = status
    raw = body_text.encode("utf-8")
    # stream(amt) yields byte chunks of size amt (exercises record reassembly).
    resp.stream.side_effect = lambda amt=256, **kw: (
        raw[i : i + amt] for i in range(0, len(raw), amt)
    )
    resp.read.return_value = raw
    return resp


@pytest.mark.unit
class TestSSEParser:
    def test_iter_sse_data_objects_handles_split_chunks(self):
        b = _SSE_STREAM.encode("utf-8")
        chunks = [b[i : i + 7] for i in range(0, len(b), 7)]  # tiny, boundary-splitting
        objs = list(oar._iter_sse_data_objects(iter(chunks)))
        types = [o.get("type") for o in objs]
        assert types == [
            "response.created",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.completed",
        ]
        deltas = [
            o["delta"] for o in objs if o.get("type") == "response.output_text.delta"
        ]
        assert "".join(deltas) == "Hello, world!"

    def test_iter_sse_skips_done_and_blank(self):
        stream = b"data: [DONE]\n\ndata: \n\n"
        assert list(oar._iter_sse_data_objects(iter([stream]))) == []


@pytest.mark.unit
class TestStreamResponsesApi:
    @pytest.fixture
    def client(self):
        return BedrockClient(region="us-east-2", metrics_enabled=False)

    def _patch_signing(self):
        # _sign_request needs credentials; patch the session lookup.
        fake_session = MagicMock()
        creds = MagicMock()
        creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKIA",
            secret_key="secret",
            token=None,  # nosec B106 - dummy test credential
        )
        fake_session.get_credentials.return_value = creds
        return patch.object(oar, "get_bedrock_session", return_value=fake_session)

    def test_streams_deltas_then_final_metering(self, client):
        fake_resp = _fake_urllib3_response(200, _SSE_STREAM)
        fake_pool = MagicMock()
        fake_pool.request.return_value = fake_resp

        with (
            self._patch_signing(),
            patch("urllib3.PoolManager", return_value=fake_pool),
        ):
            items = list(
                oar.stream_responses_api(
                    client=client,
                    model_id="openai.gpt-5.4",
                    system_prompt="sys",
                    content=[{"text": "hi"}],
                    max_tokens=100,
                    context="ChatWithDocument",
                    reasoning_effort="low",
                )
            )

        # All but the last item are text deltas; last is the metering dict.
        deltas = [i for i in items if isinstance(i, str)]
        final = items[-1]
        assert "".join(deltas) == "Hello, world!"
        assert isinstance(final, dict)
        usage = final["metering"]["ChatWithDocument/bedrock/openai.gpt-5.4"]
        # input_tokens 12 total includes cached 4 → disjoint fresh input 8.
        assert usage["inputTokens"] == 8
        assert usage["outputTokens"] == 5
        assert usage["cacheReadInputTokens"] == 4
        assert usage["cacheWriteInputTokens"] == 0
        assert usage["requests"] == 1
        assert "reasoning_tokens" not in usage
        # stream=true was sent in the request body.
        sent_body = json.loads(fake_pool.request.call_args.kwargs["body"])
        assert sent_body["stream"] is True
        assert sent_body["reasoning"]["effort"] == "low"

    def test_non_200_raises_before_deltas(self, client):
        fake_resp = _fake_urllib3_response(429, '{"message":"slow down"}')
        fake_pool = MagicMock()
        fake_pool.request.return_value = fake_resp

        with (
            self._patch_signing(),
            patch("urllib3.PoolManager", return_value=fake_pool),
        ):
            gen = oar.stream_responses_api(
                client=client,
                model_id="openai.gpt-5.4",
                system_prompt="sys",
                content=[{"text": "hi"}],
                max_tokens=100,
                context="ChatWithDocument",
            )
            with pytest.raises(RuntimeError, match="429"):
                list(gen)
