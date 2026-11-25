# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What every shipped configuration asks Nova 2 Lite for, per step (#2048).

``config_library/model_config_limits.yaml`` gives Amazon Nova 2 Lite its model
card's 64,000 output tokens. Classification (all three calls), Bedrock OCR and
LLM evaluation use Nova 2 Lite by default and used to leave ``max_tokens`` empty,
which makes the client request the model's maximum, so recording the real limit
alone would have raised each of those requests from 10,000 to 64,000 tokens, and
Bedrock holds input + max_tokens of the tokens-per-minute quota while a call
runs. The system defaults therefore ship ``max_tokens: 10000`` for those steps.

The values here are measured through the same route a stack takes: the preset
merged onto the system defaults (``merge_config_with_defaults``), read through
``IDPConfig``, then through the code that builds each request: the classification
call settings, the evaluation comparator (fed ``model_dump()``, as
``EvaluationService`` feeds it) and ``OcrService`` with the Bedrock backend. Every
preset in ``config_library/unified/`` is covered by discovery, so a preset added
later is checked against the shipped 10000 without being listed here.

The expected values are the requests each configuration made before Nova 2 Lite's
limit was recorded (10,000 for Nova 2 Lite and Nova Pro, the preset's own value
where it sets one), so a change to any of them is a change in requested output.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
import yaml

from idp_common.bedrock.model_utils import get_model_max_output_tokens
from idp_common.config.merge_utils import (
    load_system_defaults,
    merge_config_with_defaults,
)
from idp_common.config.models import IDPConfig
from idp_common.evaluation.stickler_backend.comparators import LLMComparator
from idp_common.ocr.service import OcrService

pytestmark = pytest.mark.unit

_UNIFIED = Path(__file__).resolve().parents[5] / "config_library" / "unified"

_STEPS = (
    "classification.combined",
    "classification.splitting",
    "classification.section",
    "evaluation",
    "ocr",
)

#: Requested output for a preset that sets its own value or its own model for a
#: step; every other (preset, step) pair requests the shipped 10000.
#: lending-package-sample-govcloud: classification is set to 4096 in the preset;
#: evaluation runs Claude Sonnet 4.5 and the preset sets `max_tokens: '64000'`,
#: that model's maximum, which it has always requested; OCR runs Nova Pro (10,000).
#: ocr-benchmark: classification and evaluation are set to 4096 in the preset.
_PRESET_OWN_VALUES: Dict[str, Dict[str, int]] = {
    "lending-package-sample-govcloud": {
        "classification.combined": 4096,
        "classification.splitting": 4096,
        "classification.section": 4096,
        "evaluation": 64000,
    },
    "ocr-benchmark": {
        "classification.combined": 4096,
        "classification.splitting": 4096,
        "classification.section": 4096,
        "evaluation": 4096,
    },
}


def _configurations() -> List[Tuple[str, Dict[str, Any]]]:
    found = [("(system defaults)", {})]
    for path in sorted(_UNIFIED.glob("*/config.yaml")):
        found.append((path.parent.name, yaml.safe_load(path.read_text())))
    return found


def _requested(model: str, max_tokens: Any) -> int:
    """What the Bedrock client sends: the value, or the model's maximum if unset."""
    return max_tokens if max_tokens is not None else get_model_max_output_tokens(model)


def _sent_per_step(user_config: Dict[str, Any]) -> Dict[str, int]:
    merged = merge_config_with_defaults(copy.deepcopy(user_config), validate=False)
    config = IDPConfig(**merged)
    cls = config.classification
    calls = {
        "classification.combined": cls.combined.llm,
        "classification.splitting": cls.split_then_classify.splitting.llm,
        "classification.section": cls.split_then_classify.section_classification.llm,
    }
    sent = {name: _requested(c.model, c.max_tokens) for name, c in calls.items()}

    comparator = LLMComparator(**config.evaluation.llm_method.model_dump())
    sent["evaluation"] = _requested(
        comparator.llm_config["model"], comparator.llm_config["max_tokens"]
    )

    ocr_config = copy.deepcopy(merged)
    ocr_config["ocr"]["backend"] = "bedrock"
    with patch("boto3.client", side_effect=lambda *a, **k: MagicMock()):
        service = OcrService(region="us-east-1", config=ocr_config)
    assert service.bedrock_config is not None
    sent["ocr"] = _requested(
        service.bedrock_config["model_id"], service.bedrock_config.get("max_tokens")
    )
    return sent


_CONFIGURATIONS = _configurations()


@pytest.mark.parametrize(
    ("name", "user_config"), _CONFIGURATIONS, ids=[n for n, _ in _CONFIGURATIONS]
)
def test_requested_output_per_step_is_unchanged(
    name: str, user_config: Dict[str, Any]
) -> None:
    expected = {step: 10000 for step in _STEPS}
    expected.update(_PRESET_OWN_VALUES.get(name, {}))
    assert _sent_per_step(user_config) == expected


def test_every_preset_with_its_own_values_still_exists() -> None:
    names = {n for n, _ in _CONFIGURATIONS}
    assert set(_PRESET_OWN_VALUES) <= names


def test_the_system_defaults_list_each_cap() -> None:
    """The system defaults are the canonical list of settings, so each cap is
    written there and not only supplied by the Pydantic field default."""
    raw = load_system_defaults()
    cls = raw["classification"]
    listed = {
        "classification.combined": cls["combined"]["llm"]["max_tokens"],
        "classification.splitting": cls["split_then_classify"]["splitting"]["llm"][
            "max_tokens"
        ],
        "classification.section": cls["split_then_classify"]["section_classification"][
            "llm"
        ]["max_tokens"],
        "evaluation": raw["evaluation"]["llm_method"]["max_tokens"],
        "ocr": raw["ocr"]["max_tokens"],
    }
    assert listed == {step: "10000" for step in _STEPS}


def test_the_cap_is_below_nova2_lite_maximum() -> None:
    """The cap only means something while the model allows more than it."""
    defaults = IDPConfig(**merge_config_with_defaults({}, validate=False))
    assert "nova-2-lite" in defaults.classification.combined.llm.model
    assert "nova-2-lite" in defaults.evaluation.llm_method.model
    assert "nova-2-lite" in (defaults.ocr.model_id or "")
    assert get_model_max_output_tokens(defaults.ocr.model_id or "") == 64000


@pytest.mark.parametrize("blank", ["", None])
def test_an_empty_setting_still_means_the_model_maximum(blank: Any) -> None:
    """A profile that clears max_tokens asks for the model's maximum on every step.

    A key a stored profile holds wins over the system default, so a cleared value
    requests 64,000 from Nova 2 Lite. That is the documented meaning of an empty
    value, and it must be the same on every step.
    """
    user = {
        "classification": {
            "combined": {"llm": {"max_tokens": blank}},
            "split_then_classify": {
                "splitting": {"llm": {"max_tokens": blank}},
                "section_classification": {"llm": {"max_tokens": blank}},
            },
        },
        "evaluation": {"llm_method": {"max_tokens": blank}},
        "ocr": {"max_tokens": blank},
    }
    assert _sent_per_step(user) == {step: 64000 for step in _STEPS}
