# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Optional schema restatement in the agentic system prompt (#710).

Advanced extraction sends the class schema **three times** per request:

| # | where | Payslip class |
|---|---|---|
| 1 | prose `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` in the task prompt | ~1,485 tok |
| 2 | `"Expected Schema:"` appended to the SYSTEM prompt | ~2,600 tok |
| 3 | the extraction tool's `inputSchema`, which Strands derives from the same model | ~2,595 tok |

Copies 2 and 3 are **the same JSON string** — asserted below by substring, not
estimated — so copy 2 carries no information copy 3 does not. It is 38% of the
schema tokens in every request.

It is nonetheless kept ON by default, and that is the point of the flag rather
than a removal: restating a schema in prose often improves adherence, so the
duplication may be load-bearing. This ships the knob so the question can be
**measured** on one deployed stack with identical code, which is how the
enforcement A/B was made trustworthy.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

# `tests/conftest.py` stubs `strands` with a MagicMock so unrelated modules can be
# imported without the real dependency. A side effect is that every test in
# `test_agentic_idp_unit.py` SKIPS — 42 of them, in CI as well as locally, because
# the stub is unconditional. Silently skipped tests protect nothing, so rather than
# inherit that, this module restores the real package before importing. If
# strands-agents genuinely is not installed the import below fails and the whole
# module skips, which is honest; it does not skip merely because a stub is present.
for _name in [
    k for k in list(sys.modules) if k == "strands" or k.startswith("strands.")
]:
    if isinstance(sys.modules[_name], MagicMock):
        del sys.modules[_name]

_agentic = pytest.importorskip(
    "idp_common.extraction.agentic_idp",
    reason="strands-agents not installed (a stub does not count)",
)
_build_system_prompt = _agentic._build_system_prompt


class _Small(BaseModel):
    invoice_number: str
    amount: float


class TestTheFlag:
    def test_default_is_the_existing_behaviour(self):
        """No config change may alter what is sent today."""
        prompt, _ = _build_system_prompt("BASE", None, _Small)
        assert "Expected Schema:" in prompt

    def test_on_appends_the_schema(self):
        prompt, schema_json = _build_system_prompt(
            "BASE", None, _Small, restate_schema=True
        )
        assert "Expected Schema:" in prompt
        assert schema_json in prompt

    def test_off_removes_it_entirely(self):
        prompt, _ = _build_system_prompt("BASE", None, _Small, restate_schema=False)
        assert "Expected Schema" not in prompt
        assert prompt == "BASE"

    def test_off_still_returns_schema_json_for_the_reminder_tool(self):
        """`get_extraction_schema_reminder` reads this from agent state.

        Turning the restatement off must remove a per-request duplicate WITHOUT
        removing the agent's ability to fetch the schema on demand — otherwise
        this is a capability regression, not a token saving.
        """
        _, schema_json = _build_system_prompt(
            "BASE", None, _Small, restate_schema=False
        )
        assert schema_json
        assert json.loads(schema_json)["properties"].keys() >= {
            "invoice_number",
            "amount",
        }

    def test_custom_instructions_survive_either_way(self):
        for restate in (True, False):
            prompt, _ = _build_system_prompt(
                "BASE", "DO THIS", _Small, restate_schema=restate
            )
            assert "DO THIS" in prompt, restate

    def test_only_the_schema_block_differs(self):
        on, schema_json = _build_system_prompt(
            "BASE", "INSTR", _Small, restate_schema=True
        )
        off, _ = _build_system_prompt("BASE", "INSTR", _Small, restate_schema=False)
        assert on == off + f"\n\nExpected Schema:\n{schema_json}"


class TestTheRedundancyIsReal:
    """The justification, asserted rather than assumed."""

    def test_the_system_prompt_copy_is_byte_identical_to_the_tool_schema(self):
        """Both derive from `model_json_schema()`, so copy 2 adds nothing.

        If this ever stops holding — a different serialization, a filtered
        schema — then copy 2 is no longer redundant and the flag's rationale
        changes.
        """
        prompt, schema_json = _build_system_prompt(
            "BASE", None, _Small, restate_schema=True
        )
        assert schema_json == json.dumps(_Small.model_json_schema(), indent=2)
        assert schema_json in prompt

    @pytest.mark.parametrize("preset,cls", [("lending-package-sample", "Payslip")])
    def test_the_saving_is_material_on_a_real_class(self, preset, cls):
        """Guards against the flag being pointless on real schemas."""
        import pathlib

        import yaml

        from idp_common.schema import create_pydantic_model_from_json_schema

        path = (
            pathlib.Path(__file__).resolve().parents[5]
            / "config_library"
            / "unified"
            / preset
            / "config.yaml"
        )
        if not path.exists():  # pragma: no cover - packaged install
            pytest.skip(f"{path} not present")
        cfg = yaml.safe_load(path.read_text())
        schema = next(c for c in cfg["classes"] if (c.get("$id") or "") == cls)
        model = create_pydantic_model_from_json_schema(schema, cls)
        on, _ = _build_system_prompt("B", None, model, restate_schema=True)
        off, _ = _build_system_prompt("B", None, model, restate_schema=False)
        saved_tokens = (len(on) - len(off)) // 4
        assert saved_tokens > 1000, f"only {saved_tokens} tokens saved on {cls}"


class TestConfigWiring:
    def test_the_field_exists_and_defaults_on(self):
        from idp_common.config.models import IDPConfig

        assert IDPConfig().extraction.agentic.restate_schema_in_system_prompt is True

    def test_it_can_be_turned_off_in_config(self):
        from idp_common.config.models import IDPConfig

        cfg = IDPConfig(
            **{"extraction": {"agentic": {"restate_schema_in_system_prompt": False}}}
        )
        assert cfg.extraction.agentic.restate_schema_in_system_prompt is False
