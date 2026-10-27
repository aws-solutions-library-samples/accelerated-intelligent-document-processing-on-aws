# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The default confidence prompts say how to score a field that has no value (#1527).

A field the extraction left null was scored by the confidence model, compared
against the HITL threshold like any other, and almost always alerted: the model
answered 0.0 while its own ``confidence_reason`` said the value was "not present in
the document", which is a correct verdict. Nothing in the prompt said what a
confidence on a null value means, so the encoding was also inconsistent (most null
fields 0.0, a few 0.9-1.0).

The fix is prompt guidance, not code: for a null value the model scores its
confidence in that *result* — high when the field really is absent, low when the
value appears to be on the page but extraction returned null. The second half is
what keeps a real miss visible as an alert, so the two clauses are a matched pair
and both are pinned.

These are contract tests on prompt text, which is deliberate: the change is prose,
so a prompt tidy-up that dropped it would leave every other test green while
absent fields went back to alerting. They read the **effective** prompt — system
defaults merged under each shipped preset and selected by the same function the
runtime uses — so a preset that later ships its own copy of a prompt without the
guidance fails here too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from idp_common.bedrock import format_prompt
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig
from idp_common.extraction.prompt_assembly import (
    select_confidence_task_prompt,
    select_extraction_task_prompt,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[5]
_PRESET_ROOTS = (
    _REPO / "config_library" / "unified",
    _REPO / "config_library" / "managed_config",
)

# Each clause is checked on whitespace-normalised text, because the prompts are
# YAML folded scalars and their line breaks are not part of the contract.
ABSENT_IS_A_RESULT = (
    "FIELDS WITH NO VALUE (null or empty): the confidence is your confidence in "
    "that result, i.e. that the information really is absent from this document."
)
NO_REFLEX_ZERO = "Do not score 0.0 just because there is no value."
TRULY_ABSENT_SCORES_HIGH = "HIGH (>= 0.9) when you checked the document and the field"
REAL_MISS_SCORES_LOW = "LOW (< 0.5) when the document appears to contain the value"
REAL_MISS_GIVES_A_REASON = "saying what appears to have been missed"

GUIDANCE = (
    ABSENT_IS_A_RESULT,
    NO_REFLEX_ZERO,
    TRULY_ABSENT_SCORES_HIGH,
    REAL_MISS_SCORES_LOW,
    REAL_MISS_GIVES_A_REASON,
)

# What the separate confidence pass substitutes (AssessmentService.
# _build_content_with_image_placeholder), and what extraction substitutes into the
# integrated prompt.
_CONFIDENCE_SUBSTITUTIONS = {
    "DOCUMENT_TEXT": "text",
    "DOCUMENT_CLASS": "Payslip",
    "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": "NetPay: net pay",
    "EXTRACTION_RESULTS": '{"NetPay": null}',
    "OCR_TEXT_CONFIDENCE": "ocr",
}
_EXTRACTION_SUBSTITUTIONS = {
    "DOCUMENT_TEXT": "text",
    "DOCUMENT_CLASS": "Payslip",
    "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": "NetPay: net pay",
    "FEW_SHOT_EXAMPLES": "",
}
_PLACEHOLDER = re.compile(r"\{[A-Z][A-Z_]+\}")


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _effective(user_config: dict[str, Any]) -> IDPConfig:
    return IDPConfig.model_validate(merge_config_with_defaults(user_config))


def _shipped_presets() -> list[Path]:
    found = []
    for root in _PRESET_ROOTS:
        for path in sorted(root.rglob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict) and ("classes" in data or "extraction" in data):
                found.append(path)
    return found


PRESETS = _shipped_presets()


def _integrated_advanced(cfg: IDPConfig) -> IDPConfig:
    cfg = cfg.model_copy(deep=True)
    cfg.extraction.confidence.mode = "integrated"
    cfg.extraction.agentic.enabled = True
    return cfg


def _assert_guidance(prompt: str, where: str) -> None:
    text = _normalised(prompt)
    missing = [clause for clause in GUIDANCE if clause not in text]
    assert not missing, f"{where} lacks the absent-field guidance: {missing}"


def test_the_preset_universe_is_not_empty():
    # An empty parametrisation collects as a skip, not a failure.
    assert len(PRESETS) >= 10, [str(p) for p in PRESETS]


def test_the_system_default_separate_pass_prompt_carries_the_guidance():
    cfg = _effective({})
    assert cfg.extraction.confidence.mode == "separate"
    _assert_guidance(select_confidence_task_prompt(cfg.extraction), "default")


def test_the_system_default_integrated_prompt_carries_the_guidance():
    cfg = _integrated_advanced(_effective({}))
    _assert_guidance(
        select_extraction_task_prompt(cfg.extraction), "default integrated"
    )


@pytest.mark.parametrize(
    "preset", PRESETS, ids=[str(p.relative_to(_REPO)) for p in PRESETS]
)
def test_every_shipped_preset_keeps_the_guidance(preset: Path):
    cfg = _effective(yaml.safe_load(preset.read_text()))
    _assert_guidance(select_confidence_task_prompt(cfg.extraction), str(preset))
    _assert_guidance(
        select_extraction_task_prompt(_integrated_advanced(cfg).extraction),
        f"{preset} (integrated)",
    )


@pytest.mark.parametrize("geometry_mode", ["ocr_only", "llm_grounded"])
def test_the_confidence_prompt_still_renders(geometry_mode: str):
    """The guidance must not break placeholder rendering, and must survive the
    bounding-box block being spliced in for the LLM-box geometry modes."""
    cfg = _effective({"extraction": {"geometry": {"mode": geometry_mode}}})
    template = select_confidence_task_prompt(cfg.extraction)
    parts = template.split("{DOCUMENT_IMAGE}")
    assert len(parts) == 2, "the service requires exactly one {DOCUMENT_IMAGE}"
    rendered = "".join(
        format_prompt(part, _CONFIDENCE_SUBSTITUTIONS, []) for part in parts
    )
    assert not _PLACEHOLDER.findall(rendered)
    assert "Payslip" in rendered and '{"NetPay": null}' in rendered
    _assert_guidance(rendered, f"rendered ({geometry_mode})")


def test_the_integrated_prompt_still_renders():
    cfg = _integrated_advanced(_effective({}))
    template = select_extraction_task_prompt(cfg.extraction)
    rendered = "".join(
        format_prompt(part, _EXTRACTION_SUBSTITUTIONS, [])
        for part in template.split("{DOCUMENT_IMAGE}")
    )
    assert not _PLACEHOLDER.findall(rendered)
    _assert_guidance(rendered, "rendered integrated")
