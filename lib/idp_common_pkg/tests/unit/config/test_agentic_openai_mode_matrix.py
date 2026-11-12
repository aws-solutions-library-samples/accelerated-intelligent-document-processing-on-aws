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

The escalation model is held to the same rule: agentic escalation re-extracts through
the same Strands agent loop, so an OpenAI GPT-5.x escalation model fails there just as
an extraction model does. It is refused only when escalation can run — agentic, with
validation enabled and `fail_action: escalate` — because simple-mode escalation is a
plain model call that GPT-5.x supports.
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
# Served through Converse, so allowed on the agentic path although it is OpenAI's.
ASTRA = "us.openai.gpt-6-astra"
FAIL_ACTIONS = [UNSET, "warn", "escalate", "reject"]
VALIDATION_ENABLED = [UNSET, True, False]


def _expected_agentic(mode: str, flag) -> bool:
    """The documented rule: mode wins when set; else the flag; else Simple."""
    if mode != UNSET:
        return mode == "advanced"
    return flag is True


def _expected_escalates(mode: str, flag, enabled, fail_action) -> bool:
    """Escalation runs on the agentic path only when validation is on (the default)
    and `fail_action` is `escalate` (the default is `warn`)."""
    return (
        _expected_agentic(mode, flag)
        and enabled is not False
        and fail_action == "escalate"
    )


def _config(
    mode: str,
    flag,
    model: str | None,
    class_model: str | None = None,
    *,
    escalation_model: str | None = None,
    class_escalation_model: str | None = None,
    enabled=UNSET,
    fail_action=UNSET,
    legacy_validation: bool = False,
):
    extraction: dict = {}
    if model is not None:
        extraction["model"] = model
    if mode != UNSET:
        extraction["mode"] = mode
    if flag != UNSET:
        extraction["agentic"] = {"enabled": flag}
    validation: dict = {}
    if escalation_model is not None:
        validation["escalation_model"] = escalation_model
    if enabled != UNSET:
        validation["enabled"] = enabled
    if fail_action != UNSET:
        validation["fail_action"] = fail_action
    if validation and legacy_validation:
        # The pre-0.7 home of these settings, which `IDPConfig` migrates.
        extraction.setdefault("agentic", {})["validation"] = validation
    elif validation:
        extraction["validation"] = validation
    cls: dict = {"name": "invoice", "x-aws-idp-document-type": "invoice"}
    if class_model is not None:
        cls["x-aws-idp-extraction-model"] = class_model
    if class_escalation_model is not None:
        cls["x-aws-idp-extraction-escalation-model"] = class_escalation_model
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


ESCALATION_CELLS = list(
    itertools.product(MODES, FLAGS, VALIDATION_ENABLED, FAIL_ACTIONS)
)


@pytest.mark.parametrize(("mode", "flag", "enabled", "fail_action"), ESCALATION_CELLS)
def test_escalation_expectation_matches_the_model_that_runs_it(
    mode, flag, enabled, fail_action
):
    """`_validate_and_maybe_escalate` escalates when the resolved model says
    agentic, `validation.enabled` and `fail_action == "escalate"`."""
    merged = merge_config_with_defaults(
        _config(
            mode,
            flag,
            CONVERSE,
            escalation_model=OPENAI_RESPONSES,
            enabled=enabled,
            fail_action=fail_action,
        ),
        "pattern-2",
        validate=False,
    )
    extraction = IDPConfig.model_validate(merged).extraction
    resolved = (
        extraction.agentic.enabled
        and extraction.validation.enabled
        and extraction.validation.fail_action == "escalate"
    )
    assert resolved is _expected_escalates(mode, flag, enabled, fail_action)


@pytest.mark.parametrize(("mode", "flag", "enabled", "fail_action"), ESCALATION_CELLS)
def test_global_openai_escalation_model_is_refused_exactly_when_it_would_run(
    mode, flag, enabled, fail_action
):
    errors = _agentic_errors(
        _config(
            mode,
            flag,
            CONVERSE,
            escalation_model=OPENAI_RESPONSES,
            enabled=enabled,
            fail_action=fail_action,
        )
    )
    if _expected_escalates(mode, flag, enabled, fail_action):
        assert len(errors) == 1, errors
        assert "extraction.validation.escalation_model" in errors[0]
        assert OPENAI_RESPONSES in errors[0]
    else:
        assert errors == []


@pytest.mark.parametrize(("mode", "flag", "enabled", "fail_action"), ESCALATION_CELLS)
def test_per_class_openai_escalation_override_is_refused_exactly_when_it_would_run(
    mode, flag, enabled, fail_action
):
    errors = _agentic_errors(
        _config(
            mode,
            flag,
            CONVERSE,
            class_escalation_model=OPENAI_RESPONSES,
            enabled=enabled,
            fail_action=fail_action,
        )
    )
    if _expected_escalates(mode, flag, enabled, fail_action):
        assert len(errors) == 1, errors
        assert "Class 'invoice'" in errors[0]
        assert "escalation model" in errors[0]
        assert OPENAI_RESPONSES in errors[0]
    else:
        assert errors == []


@pytest.mark.parametrize("escalation_model", [CONVERSE, ASTRA])
@pytest.mark.parametrize("per_class", [False, True])
def test_a_converse_escalation_model_is_never_refused(escalation_model, per_class):
    """GPT-6 Astra is OpenAI's but reaches Converse; the rule is the route."""
    kwargs = (
        {"class_escalation_model": escalation_model}
        if per_class
        else {"escalation_model": escalation_model}
    )
    config = _config("advanced", UNSET, CONVERSE, fail_action="escalate", **kwargs)
    assert _agentic_errors(config) == []


def test_the_pre_07_escalation_location_is_checked_too():
    """`extraction.agentic.validation` is where these settings lived before v0.7; a
    configuration still written that way is migrated and checked the same."""
    config = _config(
        "advanced",
        UNSET,
        CONVERSE,
        escalation_model=OPENAI_RESPONSES,
        fail_action="escalate",
        legacy_validation=True,
    )
    assert "validation" not in config["extraction"]
    errors = _agentic_errors(config)
    assert len(errors) == 1, errors
    assert OPENAI_RESPONSES in errors[0]


def test_every_class_with_an_openai_escalation_override_is_named():
    config = _config("advanced", UNSET, CONVERSE, fail_action="escalate")
    config["classes"] = [
        {
            "name": name,
            "x-aws-idp-document-type": name,
            "x-aws-idp-extraction-escalation-model": OPENAI_RESPONSES,
        }
        for name in ("invoice", "receipt")
    ]
    errors = _agentic_errors(config)
    assert len(errors) == 2, errors
    assert "Class 'invoice'" in errors[0]
    assert "Class 'receipt'" in errors[1]
