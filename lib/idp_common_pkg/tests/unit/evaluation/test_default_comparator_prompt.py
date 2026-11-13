# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The LLM comparator's default prompts and model have one source.

The comparator's model, system prompt and task prompt reach it from:

* `EvaluationLLMMethodConfig`'s field defaults, used whenever a config is validated
  without the system defaults merged in (a directly constructed `IDPConfig`, or a
  stored config whose `evaluation.llm_method` omits the key and bypasses the merge);
* `system_defaults/base-evaluation.yaml`, which every merged profile inherits;
* `LLMComparator`'s own fallback, used when it is handed a config with no model or
  prompt — directly, or for a field whose comparator config does not carry the
  service's `evaluation.llm_method`.

The third reads the first rather than spelling the values out. That is held two
ways: each fallback path is exercised with the comparator's default constants
replaced by sentinels, so a path that spells its own value fails however that
value is written; and the module is scanned for a model ID or prompt literal as a
cheaper second check that a split or concatenated literal can get past. The field default once read
"ou are an evaluator ...", with the first letter missing, while the other copies
were correct, so which prompt the model received depended on which path built the
config.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re
from typing import Any, Callable, Dict, List

import pytest
import yaml

from idp_common.bedrock import format_prompt
from idp_common.config import models as config_models
from idp_common.config.models import EvaluationLLMMethodConfig
from idp_common.evaluation.stickler_backend import comparators
from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

pytestmark = pytest.mark.unit

_EXPECTED = (
    "You are an evaluator that helps determine if the predicted and expected "
    "values match for document attribute extraction. You will consider the "
    "context and meaning rather than just exact string matching."
)

# A Bedrock model ID or inference-profile ID: an optional region prefix, then a
# provider, then the model name.
_MODEL_ID = re.compile(r"\b(?:[a-z]{2,6}\.)?(?:anthropic|amazon|meta|mistral|openai)\.")

_BASE_EVALUATION = (
    pathlib.Path(config_models.__file__).resolve().parent
    / "system_defaults"
    / "base-evaluation.yaml"
)


def test_model_default_system_prompt_text() -> None:
    assert EvaluationLLMMethodConfig().system_prompt == _EXPECTED


def test_system_defaults_yaml_system_prompt_text() -> None:
    data = yaml.safe_load(_BASE_EVALUATION.read_text(encoding="utf-8"))
    assert data["evaluation"]["llm_method"]["system_prompt"] == _EXPECTED


def _invoke_capturing(calls: List[Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    def invoke(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        payload = json.dumps({"match": True, "score": 1.0, "reason": "r"})
        return {"response": {"output": {"message": {"content": [{"text": payload}]}}}}

    return invoke


def test_compare_llm_falls_back_to_the_model_defaults() -> None:
    calls: List[Dict[str, Any]] = []
    comparators.compare_llm(
        "Acme",
        "ACME Corp",
        document_class="Invoice",
        attr_name="Vendor",
        attr_description="Who sent it",
        llm_config={},
        bedrock_invoker=_invoke_capturing(calls),
    )
    default = EvaluationLLMMethodConfig()
    (call,) = calls
    assert call["model_id"] == default.model
    assert call["system_prompt"] == _EXPECTED
    assert call["content"][0]["text"] == format_prompt(
        default.task_prompt,
        {
            "DOCUMENT_CLASS": "Invoice",
            "ATTRIBUTE_NAME": "Vendor",
            "ATTRIBUTE_DESCRIPTION": "Who sent it",
            "EXPECTED_VALUE": '"Acme"',
            "ACTUAL_VALUE": '"ACME Corp"',
        },
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: comparators.LLMComparator(),
        lambda: comparators.LLMComparator(model=""),
        lambda: comparators.create_llm_comparator_from_config({}),
    ],
    ids=["no-model", "empty-model", "factory-empty-config"],
)
def test_comparator_model_fallback_is_the_model_default(
    build: Callable[[], Any],
) -> None:
    assert build().llm_config["model"] == EvaluationLLMMethodConfig().model


_SENTINELS = {
    "_DEFAULT_MODEL": "sentinel-model",
    "_DEFAULT_SYSTEM_PROMPT": "sentinel system",
    "_DEFAULT_TASK_PROMPT": "sentinel task {ATTRIBUTE_NAME}",
}


@pytest.fixture
def sentinel_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SENTINELS.items():
        monkeypatch.setattr(comparators, name, value)


def _invoked(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    calls: List[Dict[str, Any]] = []
    comparators.compare_llm(
        "a",
        "b",
        attr_name="Vendor",
        llm_config=llm_config,
        bedrock_invoker=_invoke_capturing(calls),
    )
    (call,) = calls
    return call


@pytest.mark.parametrize("llm_config", [{}, {"model": ""}], ids=["absent", "empty"])
def test_every_compare_llm_fallback_reads_the_shared_defaults(
    sentinel_defaults: None, llm_config: Dict[str, Any]
) -> None:
    call = _invoked(llm_config)
    assert call["model_id"] == "sentinel-model"
    assert call["system_prompt"] == "sentinel system"
    assert call["content"][0]["text"] == "sentinel task Vendor"


@pytest.mark.parametrize(
    "build",
    [
        lambda: comparators.LLMComparator(),
        lambda: comparators.LLMComparator(model=""),
        lambda: comparators.create_llm_comparator_from_config({}),
        lambda: comparators.create_llm_comparator_from_config({"model": ""}),
    ],
    ids=["no-model", "empty-model", "factory-empty-config", "factory-empty-model"],
)
def test_every_comparator_model_fallback_reads_the_shared_default(
    sentinel_defaults: None, build: Callable[[], Any]
) -> None:
    assert build().llm_config["model"] == "sentinel-model"


def test_configured_model_and_prompts_reach_bedrock() -> None:
    configured = {
        "model": "configured-model",
        "system_prompt": "configured system",
        "task_prompt": "configured task {ATTRIBUTE_NAME}",
    }
    call = _invoked(configured)
    assert call["model_id"] == "configured-model"
    assert call["system_prompt"] == "configured system"
    assert call["content"][0]["text"] == "configured task Vendor"
    built = comparators.create_llm_comparator_from_config(configured)
    assert {k: built.llm_config[k] for k in configured} == configured


def test_comparator_module_carries_no_model_or_prompt_literal() -> None:
    """A second, spelling-based check that no copy comes back.

    Every string constant in the module is checked. A literal split across
    constants or built at run time gets past this; the sentinel tests above are
    what catch that.
    """
    tree = ast.parse(inspect.getsource(comparators))
    default = EvaluationLLMMethodConfig()
    fragments = [
        _EXPECTED[:40],
        default.task_prompt[:40],
        "Do these values match in meaning",
    ]
    offending = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (
            _MODEL_ID.search(node.value)
            or any(fragment in node.value for fragment in fragments)
        )
    ]
    assert offending == []


def _list_field(field: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "object", "properties": {"Vendor": field}},
    }


@pytest.mark.parametrize(
    ("field_keys", "in_list"),
    [
        ({"x-aws-idp-evaluation-method-config": {"threshold": 0.9}}, False),
        ({"x-aws-stickler-comparator-config": {"threshold": 0.9}}, False),
        (
            {
                "x-aws-idp-evaluation-method-config": {"threshold": 0.9},
                "x-aws-idp-evaluation-allow-llm-in-list": True,
            },
            True,
        ),
    ],
    ids=["method-config", "raw-stickler-config", "llm-in-list-opt-in"],
)
def test_llm_field_with_its_own_config_gets_the_service_model(
    field_keys: Dict[str, Any], in_list: bool
) -> None:
    """A field's own comparator config adds to the service's llm_method.

    Replacing it left such a field judged by the comparator's fallback model and
    prompts instead of the configured ones.
    """
    field = {"type": "string", "x-aws-idp-evaluation-method": "LLM", **field_keys}
    schema = {
        "$id": "Invoice",
        "x-aws-idp-document-type": "Invoice",
        "type": "object",
        "properties": {
            "Rows" if in_list else "Vendor": _list_field(field) if in_list else field
        },
    }
    llm_config = {
        "model": "configured-model",
        "system_prompt": "configured system",
        "task_prompt": "configured task",
        "temperature": 0.3,
        "threshold": 0.5,
    }
    props = SticklerConfigMapper.build_stickler_model_config(
        schema, llm_config=llm_config
    )["schema"]["properties"]
    prop = (
        props["Rows"]["items"]["properties"]["Vendor"] if in_list else props["Vendor"]
    )
    assert prop["x-aws-stickler-comparator"] == "IDPLLMComparator"
    cfg = prop["x-aws-stickler-comparator-config"]
    assert {k: cfg[k] for k in llm_config if k != "threshold"} == {
        k: v for k, v in llm_config.items() if k != "threshold"
    }
    # The field's own key wins over the service-level one.
    assert cfg["threshold"] == 0.9
    assert cfg["attribute_name"] == "Vendor"
