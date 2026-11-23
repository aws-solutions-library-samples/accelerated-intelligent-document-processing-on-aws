# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``config-validate`` refuses an OpenAI GPT-5.x model exactly when extraction is agentic.

Whether extraction is agentic is decided by ``extraction.mode`` when it is set, and by
``extraction.agentic.enabled`` only when it is not. The command goes through
``idp_sdk``'s ``ConfigOperation.validate`` to ``validate_config``, so this exercises
the whole chain an operator runs. An axis marked UNSET is omitted from the file.

The escalation model, global or per class, is refused under the same rule when
escalation can run on the agentic path (`fail_action: escalate`), because agentic
escalation calls the same Strands agent loop.
"""

from __future__ import annotations

import itertools
import logging

import pytest
import yaml
from click.testing import CliRunner

from idp_cli.cli import cli

pytestmark = pytest.mark.unit

UNSET = "UNSET"
OPENAI_RESPONSES = "openai.gpt-5.5"
CONVERSE = "us.anthropic.claude-sonnet-5"


def _expected_agentic(mode, flag) -> bool:
    if mode != UNSET:
        return mode == "advanced"
    return flag is True


def _run(tmp_path, mode, flag, model, *, validation=None, class_extra=None):
    extraction: dict = {"model": model}
    if mode != UNSET:
        extraction["mode"] = mode
    if flag != UNSET:
        extraction["agentic"] = {"enabled": flag}
    if validation:
        extraction["validation"] = validation
    cls = {"name": "invoice", **(class_extra or {})}
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"classes": [cls], "extraction": extraction}),
        encoding="utf-8",
    )
    logging.disable(logging.CRITICAL)
    try:
        return CliRunner().invoke(cli, ["config-validate", "--config-file", str(path)])
    finally:
        logging.disable(logging.NOTSET)


@pytest.mark.parametrize(
    ("mode", "flag", "model"),
    list(
        itertools.product(
            [UNSET, "simple", "advanced"],
            [UNSET, True, False],
            [OPENAI_RESPONSES, CONVERSE],
        )
    ),
)
def test_config_validate_mode_matrix(tmp_path, mode, flag, model):
    result = _run(tmp_path, mode, flag, model)
    flat = " ".join(result.output.split())
    refused = model == OPENAI_RESPONSES and _expected_agentic(mode, flag)
    if refused:
        assert result.exit_code == 1, flat
        assert "NOT compatible with agentic extraction" in flat
        assert OPENAI_RESPONSES in flat
    else:
        assert result.exit_code == 0, flat
        assert "agentic extraction" not in flat


@pytest.mark.parametrize(
    ("mode", "fail_action", "per_class"),
    list(
        itertools.product(
            [UNSET, "simple", "advanced"],
            [UNSET, "warn", "escalate", "reject"],
            [False, True],
        )
    ),
)
def test_config_validate_escalation_model_matrix(
    tmp_path, mode, fail_action, per_class
):
    validation = {} if fail_action == UNSET else {"fail_action": fail_action}
    class_extra = None
    if per_class:
        class_extra = {"x-aws-idp-extraction-escalation-model": OPENAI_RESPONSES}
    else:
        validation["escalation_model"] = OPENAI_RESPONSES
    result = _run(
        tmp_path,
        mode,
        UNSET,
        CONVERSE,
        validation=validation,
        class_extra=class_extra,
    )
    flat = " ".join(result.output.split())
    refused = _expected_agentic(mode, UNSET) and fail_action == "escalate"
    if refused:
        assert result.exit_code == 1, flat
        assert "NOT compatible with agentic extraction" in flat
        assert OPENAI_RESPONSES in flat
        assert "escalation" in flat
    else:
        assert result.exit_code == 0, flat
        assert "agentic extraction" not in flat
