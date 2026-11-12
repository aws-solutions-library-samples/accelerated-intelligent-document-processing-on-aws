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

The third reads the first rather than spelling the values out, so the fallback is
exercised here rather than compared by text, and the comparator module is held to
carrying no model ID or prompt text of its own. The field default once read
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


def test_comparator_module_carries_no_model_or_prompt_literal() -> None:
    """Pins that no fourth copy comes back.

    Every string constant in the module is checked, so a model ID or a prompt
    written back in as a fallback fails here even when no test reaches its path.
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


def test_llm_field_with_its_own_method_config_gets_the_service_model() -> None:
    """A per-field method-config adds to the service's llm_method, never replaces it.

    Replacing it left such a field judged by the comparator's fallback model and
    prompts instead of the configured ones.
    """
    schema = {
        "$id": "Invoice",
        "x-aws-idp-document-type": "Invoice",
        "type": "object",
        "properties": {
            "Vendor": {
                "type": "string",
                "x-aws-idp-evaluation-method": "LLM",
                "x-aws-idp-evaluation-method-config": {"threshold": 0.9},
            },
        },
    }
    llm_config = {
        "model": "configured-model",
        "system_prompt": "configured system",
        "task_prompt": "configured task",
        "threshold": 0.5,
    }
    prop = SticklerConfigMapper.build_stickler_model_config(
        schema, llm_config=llm_config
    )["schema"]["properties"]["Vendor"]
    cfg = prop["x-aws-stickler-comparator-config"]
    assert cfg["model"] == "configured-model"
    assert cfg["system_prompt"] == "configured system"
    assert cfg["task_prompt"] == "configured task"
    # The field's own key wins over the service-level one.
    assert cfg["threshold"] == 0.9
    assert cfg["attribute_name"] == "Vendor"
