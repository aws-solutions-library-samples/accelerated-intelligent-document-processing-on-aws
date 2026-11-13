# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The configured Bedrock guardrail reaches every model-call path in ``idp_common``.

``GUARDRAIL_ID_AND_VERSION`` is parsed once, in ``idp_common.bedrock.guardrail``,
and each path attaches it in the shape its API takes:

* ``BedrockClient.invoke_model`` -> ``converse(guardrailConfig=...)``;
* the Strands factory and agentic extraction -> ``BedrockModel(guardrail_id=...)``;
* the rule translator's direct ``invoke_model`` -> top-level ``guardrailIdentifier``.

Every expected key set here is read from the authority for that API -- botocore's
``bedrock-runtime`` service model and Strands' ``BedrockModel.BedrockConfig`` --
never from the helper under test. ``scripts/tests/test_model_call_guardrail.py``
is the structural half: it derives every model call in the tree by AST and
requires each to be one of these routes, to pass the guardrail itself, or to be
listed with its own reason.
"""

from __future__ import annotations

import asyncio
import io
import json
import typing
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.session
import pytest
from pydantic import BaseModel

from idp_common.bedrock import guardrail
from idp_common.bedrock.client import BedrockClient

pytestmark = pytest.mark.unit

GUARDRAIL_ID = "gr0abc123"
GUARDRAIL_VERSION = "7"


def _runtime_input_members(operation: str) -> dict:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return dict(model.operation_model(operation).input_shape.members)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(
        "GUARDRAIL_ID_AND_VERSION", f"{GUARDRAIL_ID}:{GUARDRAIL_VERSION}"
    )


#: Values that configure no guardrail, each for its own reason: unset, empty, no
#: separator, an empty half on either side, and too many parts.
UNCONFIGURED = [None, "", "gr0abc123", "gr0abc123:", ":7", "a:b:c"]


def _set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GUARDRAIL_ID_AND_VERSION", raising=False)
    else:
        monkeypatch.setenv("GUARDRAIL_ID_AND_VERSION", value)


# --------------------------------------------------------------------------- #
# The parser and its three shapes, against the API models
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("operation", ["Converse", "ConverseStream"])
def test_the_converse_shape_is_what_botocore_accepts(configured, operation):
    shape = _runtime_input_members(operation)["guardrailConfig"]
    members = shape.members
    assert {"guardrailIdentifier", "guardrailVersion", "trace"} <= set(members)

    config = guardrail.guardrail_config_from_env()

    assert config is not None
    assert set(config) <= set(members)
    assert config["guardrailIdentifier"] == GUARDRAIL_ID
    assert config["guardrailVersion"] == GUARDRAIL_VERSION
    assert config["trace"] in members["trace"].enum


def test_the_invoke_model_shape_is_what_botocore_accepts(configured):
    for operation in ("InvokeModel", "InvokeModelWithResponseStream"):
        members = _runtime_input_members(operation)
        assert {"guardrailIdentifier", "guardrailVersion", "trace"} <= set(members)

        params = guardrail.guardrail_invoke_model_params()

        assert set(params) <= set(members)
        assert params["guardrailIdentifier"] == GUARDRAIL_ID
        assert params["guardrailVersion"] == GUARDRAIL_VERSION
        assert params["trace"] in members["trace"].enum


def test_the_strands_shape_is_what_bedrock_model_accepts(configured):
    strands_bedrock = pytest.importorskip("strands.models.bedrock")
    from idp_common.agents.common.strands_bedrock_model import strands_guardrail_kwargs

    config_keys = set(typing.get_type_hints(strands_bedrock.BedrockModel.BedrockConfig))
    assert {"guardrail_id", "guardrail_version"} <= config_keys

    kwargs = strands_guardrail_kwargs()

    assert set(kwargs) <= config_keys
    assert kwargs["guardrail_id"] == GUARDRAIL_ID
    assert kwargs["guardrail_version"] == GUARDRAIL_VERSION


@pytest.mark.parametrize("value", UNCONFIGURED)
def test_an_unconfigured_guardrail_adds_nothing(monkeypatch, value):
    _set(monkeypatch, value)

    assert guardrail.guardrail_id_and_version_from_env() is None
    assert guardrail.guardrail_config_from_env() is None
    assert guardrail.guardrail_invoke_model_params() == {}


@pytest.mark.parametrize("value", UNCONFIGURED)
def test_an_unconfigured_guardrail_adds_nothing_to_a_strands_model(monkeypatch, value):
    pytest.importorskip("strands.models.bedrock")
    from idp_common.agents.common.strands_bedrock_model import strands_guardrail_kwargs

    _set(monkeypatch, value)

    assert strands_guardrail_kwargs() == {}


# --------------------------------------------------------------------------- #
# BedrockClient: the route most model calls take
# --------------------------------------------------------------------------- #


def _converse_kwargs(monkeypatch, value) -> dict:
    _set(monkeypatch, value)
    client = BedrockClient(region="us-west-2", metrics_enabled=False)
    client._client = MagicMock()
    client._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }
    client.invoke_model(
        model_id="us.anthropic.claude-sonnet-4-6",
        system_prompt="sys",
        content=[{"text": "hello"}],
        context="Extraction",
    )
    assert client._client.converse.call_count == 1
    return client._client.converse.call_args.kwargs


def test_bedrock_client_sends_the_configured_guardrail(monkeypatch):
    kwargs = _converse_kwargs(monkeypatch, f"{GUARDRAIL_ID}:{GUARDRAIL_VERSION}")

    sent = kwargs["guardrailConfig"]
    assert sent["guardrailIdentifier"] == GUARDRAIL_ID
    assert sent["guardrailVersion"] == GUARDRAIL_VERSION


@pytest.mark.parametrize("value", UNCONFIGURED)
def test_bedrock_client_sends_no_guardrail_when_none_is_configured(monkeypatch, value):
    assert "guardrailConfig" not in _converse_kwargs(monkeypatch, value)


# --------------------------------------------------------------------------- #
# The Strands factory and agentic extraction
# --------------------------------------------------------------------------- #


def test_the_strands_factory_applies_the_guardrail(configured):
    pytest.importorskip("strands.models.bedrock")
    from idp_common.agents.common.strands_bedrock_model import (
        create_strands_bedrock_model,
    )

    model = create_strands_bedrock_model("us.anthropic.claude-sonnet-4-6")

    config = model.get_config()
    assert config.get("guardrail_id") == GUARDRAIL_ID
    assert config.get("guardrail_version") == GUARDRAIL_VERSION


class _State:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = dict(values)

    def get(self, key: str) -> Any:
        return self._values.get(key)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value


class _Metrics:
    accumulated_usage = {"inputTokens": 1, "outputTokens": 1}


class _Doc(BaseModel):
    Name: str | None = None


def _agentic_model_kwargs(monkeypatch, value) -> dict:
    """Run agentic extraction to its model construction and return the kwargs.

    The real ``structured_output_async`` builds the model; only the Strands
    ``Agent`` and the agent invocation are faked, so the kwargs captured are the
    ones production passes.
    """
    agentic = pytest.importorskip("idp_common.extraction.agentic_idp")
    from idp_common.config.models import IDPConfig

    _set(monkeypatch, value)
    captured: list[dict] = []

    def _model(**kwargs: Any) -> MagicMock:
        captured.append(kwargs)
        return MagicMock()

    class _FakeAgent:
        def __init__(self, **_kwargs: Any) -> None:
            self.state = _State({"current_extraction": {"Name": "x"}})
            self.event_loop_metrics = _Metrics()

    class _Result:
        metrics = _Metrics()

        def __str__(self) -> str:
            return "done"

    async def _ok(agent, input):  # noqa: ARG001 - signature parity
        return _Result()

    with (
        patch.object(agentic, "Agent", _FakeAgent),
        patch.object(agentic, "BedrockModel", _model),
        patch.object(agentic, "invoke_agent_with_retry", _ok),
    ):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                agentic.structured_output_async(
                    model_id="us.anthropic.claude-sonnet-4-6",
                    data_format=_Doc,
                    prompt="extract this",
                    config=IDPConfig(),
                )
            )
        finally:
            loop.close()
    assert captured, "agentic extraction never constructed a BedrockModel"
    return captured[0]


def test_agentic_extraction_applies_the_guardrail(monkeypatch):
    kwargs = _agentic_model_kwargs(monkeypatch, f"{GUARDRAIL_ID}:{GUARDRAIL_VERSION}")

    assert kwargs["guardrail_id"] == GUARDRAIL_ID
    assert kwargs["guardrail_version"] == GUARDRAIL_VERSION


@pytest.mark.parametrize("value", [None, ""])
def test_agentic_extraction_without_a_guardrail_is_unchanged(monkeypatch, value):
    kwargs = _agentic_model_kwargs(monkeypatch, value)

    assert not {k for k in kwargs if k.startswith("guardrail")}


# --------------------------------------------------------------------------- #
# The rule translator's direct invoke_model
# --------------------------------------------------------------------------- #


def _translator_invoke_kwargs(monkeypatch, value) -> dict:
    from idp_common.rule_validation.z3.config_loader import (
        TranslatorConfig,
        ValueExtractionConfig,
    )
    from idp_common.rule_validation.z3.rule_translator import RuleTranslator

    _set(monkeypatch, value)
    common = {
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "temperature": 0.0,
        "max_tokens": 64,
        "system_prompt": "sys",
    }
    with patch("idp_common.rule_validation.z3.rule_translator.boto3.client"):
        translator = RuleTranslator(
            translator_config=TranslatorConfig(
                **common, task_prompt_template="{rule} {data_example}"
            ),
            extraction_config=ValueExtractionConfig(
                **common,
                task_prompt_template=(
                    "{rule_description} {natural_language_rule} "
                    "{parameters_json} {data_type} {data}"
                ),
            ),
        )
    translator.bedrock_client = MagicMock()
    body = {
        "content": [{"text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    translator.bedrock_client.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps(body).encode())
    }
    translator._invoke_bedrock("prompt")
    return translator.bedrock_client.invoke_model.call_args.kwargs


def test_the_rule_translator_sends_the_configured_guardrail(monkeypatch):
    kwargs = _translator_invoke_kwargs(
        monkeypatch, f"{GUARDRAIL_ID}:{GUARDRAIL_VERSION}"
    )

    assert set(kwargs) <= set(_runtime_input_members("InvokeModel"))
    assert kwargs["guardrailIdentifier"] == GUARDRAIL_ID
    assert kwargs["guardrailVersion"] == GUARDRAIL_VERSION


def test_the_rule_translator_without_a_guardrail_is_unchanged(monkeypatch):
    kwargs = _translator_invoke_kwargs(monkeypatch, None)

    assert set(kwargs) == {"modelId", "body", "contentType", "accept"}


# --------------------------------------------------------------------------- #
# The OpenAI Responses route, which sends no guardrail
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value, warns", [("gr0abc123:7", True), (None, False)])
def test_the_responses_route_says_when_a_configured_guardrail_is_not_sent(
    monkeypatch, caplog, value, warns
):
    from idp_common.bedrock import openai_responses

    _set(monkeypatch, value)
    monkeypatch.setattr(openai_responses, "_guardrail_warning_logged", False)
    with caplog.at_level("WARNING", logger=openai_responses.logger.name):
        openai_responses._warn_guardrail_not_applied("openai.gpt-5.4")
        openai_responses._warn_guardrail_not_applied("openai.gpt-5.4")

    messages = [r.getMessage() for r in caplog.records if "guardrail" in r.getMessage()]
    assert len(messages) == (1 if warns else 0)
