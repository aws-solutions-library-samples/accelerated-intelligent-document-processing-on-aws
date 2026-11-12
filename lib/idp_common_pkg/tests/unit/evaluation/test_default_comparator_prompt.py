# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The default LLM-comparator system prompt is spelled the same in all three places.

The comparator's system prompt has three sources that are each the effective value
on some path:

* `EvaluationLLMMethodConfig.system_prompt`'s field default, used whenever a config
  is validated without the system defaults merged in (a directly constructed
  `IDPConfig`, or a stored config whose `evaluation.llm_method` omits the key and
  bypasses the merge);
* `system_defaults/base-evaluation.yaml`, which every merged profile inherits;
* the literal fallback in `LLMComparator`, used when the comparator is handed an
  `llm_config` with no `system_prompt` at all.

The field default once read "ou are an evaluator ...", with the first letter
missing, while the other two were correct, so which prompt the model received
depended on which path built the config. This asserts the text and the agreement.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
import yaml

from idp_common.config import models as config_models
from idp_common.config.models import EvaluationLLMMethodConfig
from idp_common.evaluation.stickler_backend import comparators

pytestmark = pytest.mark.unit

_EXPECTED = (
    "You are an evaluator that helps determine if the predicted and expected "
    "values match for document attribute extraction. You will consider the "
    "context and meaning rather than just exact string matching."
)

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


def test_comparator_fallback_system_prompt_text() -> None:
    # The fallback is a literal inside a method body, reachable only through a live
    # Bedrock call, so it is read from source rather than exercised.
    assert _EXPECTED in inspect.getsource(comparators)
