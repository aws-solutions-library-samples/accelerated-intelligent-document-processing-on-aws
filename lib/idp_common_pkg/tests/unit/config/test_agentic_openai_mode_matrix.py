# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The OpenAI-with-agentic-extraction check decides on the resolved extraction mode.

`extraction.mode` selects the extraction path and overrides
`extraction.agentic.enabled` whenever it is set (`reconcile_mode_and_agentic`); the
flag decides only for a configuration that sets no mode. The check used to read the
raw flag, so it passed `mode: advanced` with an OpenAI GPT-5.x model — the
configuration the UI writes — and refused `mode: simple` with a stale
`agentic.enabled: true`, which runs the simple path and works.

Each cell below is driven through `validate_config`, the function
`idp-cli config-validate` and `idp_sdk`'s `ConfigOperation.validate` both call. An
axis marked UNSET is omitted from the document entirely rather than written as null.
"""

from __future__ import annotations

import itertools
import logging

import pytest

from idp_common.config.merge_utils import merge_config_with_defaults, validate_config
from idp_common.config.models import IDPConfig

pytestmark = pytest.mark.unit

UNSET = "UNSET"
MODES = [UNSET, "simple", "advanced"]
FLAGS = [UNSET, True, False]
OPENAI_RESPONSES = "openai.gpt-5.5"
CONVERSE = "us.anthropic.claude-sonnet-5"


def _expected_agentic(mode: str, flag) -> bool:
    """The documented rule: mode wins when set; else the flag; else Simple."""
    if mode != UNSET:
        return mode == "advanced"
    return flag is True


def _config(mode: str, flag, model: str | None, class_model: str | None = None):
    extraction: dict = {}
    if model is not None:
        extraction["model"] = model
    if mode != UNSET:
        extraction["mode"] = mode
    if flag != UNSET:
        extraction["agentic"] = {"enabled": flag}
    cls: dict = {"name": "invoice", "x-aws-idp-document-type": "invoice"}
    if class_model is not None:
        cls["x-aws-idp-extraction-model"] = class_model
    return {"classes": [cls], "extraction": extraction}


def _agentic_errors(config: dict) -> list[str]:
    logging.disable(logging.CRITICAL)
    try:
        result = validate_config(config)
    finally:
        logging.disable(logging.NOTSET)
    errors = [e for e in result["errors"] if "agentic extraction" in e]
    assert bool(errors) == (result["valid"] is False), result["errors"]
    return errors


CELLS = list(itertools.product(MODES, FLAGS))


@pytest.mark.parametrize(("mode", "flag"), CELLS)
def test_expected_table_matches_the_model_that_picks_the_path(mode, flag):
    """The expectation is derived from the authority, not restated beside it."""
    merged = merge_config_with_defaults(
        _config(mode, flag, OPENAI_RESPONSES), "pattern-2", validate=False
    )
    resolved = IDPConfig.model_validate(merged).extraction.agentic.enabled
    assert resolved is _expected_agentic(mode, flag)


@pytest.mark.parametrize(("mode", "flag"), CELLS)
def test_global_openai_model_is_refused_exactly_when_extraction_is_agentic(mode, flag):
    errors = _agentic_errors(_config(mode, flag, OPENAI_RESPONSES))
    if _expected_agentic(mode, flag):
        assert len(errors) == 1, errors
        assert OPENAI_RESPONSES in errors[0]
        assert "extraction.mode" in errors[0]
    else:
        assert errors == []


@pytest.mark.parametrize(("mode", "flag"), CELLS)
def test_per_class_openai_override_is_refused_exactly_when_agentic(mode, flag):
    errors = _agentic_errors(
        _config(mode, flag, CONVERSE, class_model=OPENAI_RESPONSES)
    )
    if _expected_agentic(mode, flag):
        assert len(errors) == 1, errors
        assert "Class 'invoice'" in errors[0]
        assert OPENAI_RESPONSES in errors[0]
    else:
        assert errors == []


@pytest.mark.parametrize(("mode", "flag"), CELLS)
def test_a_converse_model_is_never_refused(mode, flag):
    assert _agentic_errors(_config(mode, flag, CONVERSE)) == []


@pytest.mark.parametrize("flag", ["false", "False"])
def test_a_string_false_flag_with_no_mode_is_not_agentic(flag):
    """The model reads the string "false" as False; `bool("false")` is True, which
    is how the raw-flag check refused this configuration."""
    assert _agentic_errors(_config(UNSET, flag, OPENAI_RESPONSES)) == []
