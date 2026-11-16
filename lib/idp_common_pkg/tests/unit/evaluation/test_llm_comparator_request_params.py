# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The `evaluation.llm_method` sampling and output settings reach the request.

The comparator used to keep `top_p` and `max_tokens` without forwarding them, so
an evaluation ran with the Bedrock client's own `top_p` default (0.1) whatever
the configuration said, and it dropped `reasoning_effort`. All three are now
forwarded; the client sends the effort only to a model with an effort control.

These tests drive the real path — `SticklerConfigMapper` -> Stickler's converter ->
`LLMComparator` -> `compare_llm` -> `BedrockClient.invoke_model` — and stub only
the boto3 `bedrock-runtime` client underneath, so what is asserted is the
`converse` request itself, after the client's own per-model handling (where
`maxTokens` goes for Claude versus Nova, which models take an effort, which strip
sampling parameters). Model capabilities are read from the client's own
predicates rather than assumed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bedrock.client import (
    BedrockClient,
    get_model_max_output_tokens,
    is_claude_effort_model,
    strips_sampling_params,
)
from idp_common.config.models import EvaluationLLMMethodConfig, IDPConfig
from idp_common.evaluation.stickler_backend import comparators
from idp_common.evaluation.stickler_backend.comparators import (
    LLMComparator,
    compare_llm,
    register_idp_comparators,
)
from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper
from idp_common.evaluation.stickler_backend.model_factory import get_stickler_model

pytestmark = pytest.mark.unit

_CLAUDE_EFFORT = "us.anthropic.claude-sonnet-4-6"
_CLAUDE_NO_EFFORT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_NOVA = EvaluationLLMMethodConfig().model


def _schema() -> Dict[str, Any]:
    return {
        "$id": "Invoice",
        "type": "object",
        "x-aws-idp-document-type": "Invoice",
        "properties": {
            "Agency": {
                "type": "string",
                "description": "The advertising agency placing the order",
                "x-aws-idp-evaluation-method": "LLM",
            }
        },
    }


def _shipped_llm_method(**overrides: Any) -> Dict[str, Any]:
    """`evaluation.llm_method` as EvaluationService sees it: a dumped IDPConfig."""
    llm = IDPConfig().model_dump()["evaluation"]["llm_method"]
    llm.update(overrides)
    return llm


def _stub_client() -> BedrockClient:
    client = BedrockClient(region="us-west-2", metrics_enabled=False)
    client._client = MagicMock()
    client._client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {"text": json.dumps({"match": True, "score": 1.0, "reason": "r"})}
                ]
            }
        },
        "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12},
    }
    return client


def _converse_requests_through_stickler(
    llm_method: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Score one differing LLM field end to end; return the converse requests."""
    client = _stub_client()
    cfg = SticklerConfigMapper.build_stickler_model_config(
        _schema(), llm_config=llm_method
    )
    register_idp_comparators()
    model = get_stickler_model(
        "Invoice", {"invoice": cfg}, {}, set(), lambda *a, **k: None
    )
    with patch("idp_common.bedrock.invoke_model", client.invoke_model):
        model.model_validate({"Agency": "AnyCompany Media"}).compare_with(
            model.model_validate({"Agency": "Some Other Agency"})
        )
    return [call.kwargs for call in client._client.converse.call_args_list]


def test_configured_claude_settings_reach_the_request() -> None:
    assert is_claude_effort_model(_CLAUDE_EFFORT)
    assert not strips_sampling_params(_CLAUDE_EFFORT)
    (request,) = _converse_requests_through_stickler(
        _shipped_llm_method(model=_CLAUDE_EFFORT, top_p=0.4, max_tokens=321)
    )
    assert request["modelId"] == _CLAUDE_EFFORT
    assert request["inferenceConfig"] == {"topP": 0.4}
    assert request["additionalModelRequestFields"]["max_tokens"] == 321


def test_configured_nova_settings_reach_the_request() -> None:
    (request,) = _converse_requests_through_stickler(
        _shipped_llm_method(top_p=0.4, max_tokens=321)
    )
    assert request["modelId"] == _NOVA
    assert request["inferenceConfig"] == {"topP": 0.4, "maxTokens": 321}


def test_shipped_defaults_reach_the_request() -> None:
    """The shipped llm_method: top_p 0.0 means temperature is sent, not topP.

    Before the fix the client's own top_p default (0.1) was used, so the request
    carried topP 0.1 and no temperature.
    """
    default = EvaluationLLMMethodConfig()
    assert default.top_p == 0.0 and default.max_tokens is None
    (request,) = _converse_requests_through_stickler(_shipped_llm_method())
    assert request["inferenceConfig"] == {
        "temperature": default.temperature,
        "maxTokens": get_model_max_output_tokens(_NOVA),
    }
    # The shipped model has no effort control, so the shipped `medium` adds
    # nothing to the request: only Nova's own topK.
    assert not is_claude_effort_model(_NOVA)
    assert request["additionalModelRequestFields"] == {
        "inferenceConfig": {"topK": int(default.top_k)}
    }


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_configured_effort_is_sent_for_an_effort_capable_model(
    effort: Optional[str],
) -> None:
    """`None` leaves the shipped llm_method's own value, the `medium` default."""
    assert is_claude_effort_model(_CLAUDE_EFFORT)
    shipped = _shipped_llm_method(model=_CLAUDE_EFFORT)
    assert shipped["reasoning_effort"] == "medium"
    if effort is not None:
        shipped["reasoning_effort"] = effort
    (request,) = _converse_requests_through_stickler(shipped)
    assert request["additionalModelRequestFields"]["output_config"] == {
        "effort": effort or "medium"
    }


@pytest.mark.parametrize("model", [_NOVA, _CLAUDE_NO_EFFORT])
def test_no_effort_is_sent_for_a_model_without_an_effort_control(model: str) -> None:
    assert not is_claude_effort_model(model)
    (request,) = _converse_requests_through_stickler(
        _shipped_llm_method(model=model, reasoning_effort="high")
    )
    extra = request.get("additionalModelRequestFields") or {}
    assert "output_config" not in extra
    assert "reasoning" not in extra


@pytest.mark.parametrize("blank", [None, ""], ids=["none", "empty-string"])
def test_unset_numeric_settings_take_the_llm_method_defaults(
    blank: Optional[str],
) -> None:
    """`max_tokens: ""` is what base-evaluation.yaml ships for "use the model max"."""
    default = EvaluationLLMMethodConfig()
    built = LLMComparator(top_p=blank, max_tokens=blank, temperature=blank, top_k=blank)
    assert built.llm_config["top_p"] == default.top_p
    assert built.llm_config["max_tokens"] == default.max_tokens
    assert built.llm_config["temperature"] == default.temperature
    assert built.llm_config["top_k"] == default.top_k
    assert LLMComparator(reasoning_effort=blank).llm_config["reasoning_effort"] == (
        default.reasoning_effort
    )


def test_string_values_are_converted() -> None:
    built = LLMComparator(top_p="0.4", max_tokens="321", reasoning_effort="low")
    assert built.llm_config["top_p"] == 0.4
    assert built.llm_config["max_tokens"] == 321
    assert built.llm_config["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("given", "expected"), [(None, 0.8), ("", 0.8), ("0.6", 0.6), (0.6, 0.6)]
)
def test_threshold(given: Any, expected: float) -> None:
    assert LLMComparator(threshold=given).threshold == expected


def test_factory_forwards_every_setting() -> None:
    configured = {
        "model": "configured-model",
        "temperature": 0.3,
        "top_k": 7.0,
        "top_p": 0.4,
        "max_tokens": 321,
        "reasoning_effort": "high",
        "system_prompt": "s",
        "task_prompt": "t",
    }
    built = comparators.create_llm_comparator_from_config(configured)
    assert {k: built.llm_config[k] for k in configured} == configured


_PARAM_SENTINELS = {
    "_DEFAULT_TEMPERATURE": 0.11,
    "_DEFAULT_TOP_K": 13.0,
    "_DEFAULT_TOP_P": 0.22,
    "_DEFAULT_MAX_TOKENS": 333,
    "_DEFAULT_REASONING_EFFORT": "xhigh",
}


def _invoked(llm_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def invoke(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        payload = json.dumps({"match": True, "score": 1.0, "reason": "r"})
        return {"response": {"output": {"message": {"content": [{"text": payload}]}}}}

    compare_llm("a", "b", llm_config=llm_config, bedrock_invoker=invoke)
    return calls


@pytest.mark.parametrize(
    "llm_config",
    [
        {},
        {
            k: None
            for k in ("temperature", "top_k", "top_p", "max_tokens", "reasoning_effort")
        },
    ],
    ids=["absent", "none"],
)
def test_compare_llm_parameter_fallbacks_read_the_shared_defaults(
    monkeypatch: pytest.MonkeyPatch, llm_config: Dict[str, Any]
) -> None:
    for name, value in _PARAM_SENTINELS.items():
        monkeypatch.setattr(comparators, name, value)
    (call,) = _invoked(llm_config)
    assert call["temperature"] == 0.11
    assert call["top_k"] == 13.0
    assert call["top_p"] == 0.22
    assert call["max_tokens"] == 333
    assert call["reasoning_effort"] == "xhigh"


def test_comparator_parameter_fallbacks_read_the_shared_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _PARAM_SENTINELS.items():
        monkeypatch.setattr(comparators, name, value)
    for built in (LLMComparator(), comparators.create_llm_comparator_from_config({})):
        assert built.llm_config["temperature"] == 0.11
        assert built.llm_config["top_k"] == 13.0
        assert built.llm_config["top_p"] == 0.22
        assert built.llm_config["max_tokens"] == 333
        assert built.llm_config["reasoning_effort"] == "xhigh"


# ---------------------------------------------------------------------------
# An empty prompt is refused, as summarization and classification's combined page
# prompt refuse one; an absent prompt falls back to the default (test_default_comparator_prompt).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["system_prompt", "task_prompt"])
def test_comparator_refuses_an_empty_prompt(key: str) -> None:
    with pytest.raises(ValueError, match=f"evaluation.llm_method.{key} is empty"):
        LLMComparator(**{key: ""})


@pytest.mark.parametrize("key", ["system_prompt", "task_prompt"])
def test_empty_prompt_fails_the_model_build_naming_the_key(key: str) -> None:
    """Stickler reports it as a field configuration error.

    EvaluationService turns that ValueError into a section marked
    `evaluation_failed` with failure_type `schema_configuration_error`, instead of
    every LLM field going to Bedrock with an empty prompt.
    """
    cfg = SticklerConfigMapper.build_stickler_model_config(
        _schema(), llm_config=_shipped_llm_method(**{key: ""})
    )
    register_idp_comparators()
    with pytest.raises(ValueError, match=f"evaluation.llm_method.{key} is empty"):
        get_stickler_model("Invoice", {"invoice": cfg}, {}, set(), lambda *a, **k: None)


@pytest.mark.parametrize("key", ["system_prompt", "task_prompt"])
def test_compare_llm_refuses_an_empty_prompt_without_calling_bedrock(key: str) -> None:
    calls: List[Dict[str, Any]] = []

    def invoke(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        raise AssertionError("Bedrock must not be called")

    matched, score, reason = compare_llm(
        "a", "b", attr_name="Vendor", llm_config={key: ""}, bedrock_invoker=invoke
    )
    assert calls == []
    assert (matched, score) == (False, 0.0)
    assert reason is not None and f"evaluation.llm_method.{key} is empty" in reason
    # An error result is never memoized by LLMComparator.
    assert reason.startswith(comparators._TRANSIENT_ERROR_PREFIXES)
