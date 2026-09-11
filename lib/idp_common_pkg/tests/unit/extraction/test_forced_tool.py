# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Forced tool use for simple extraction (WS-05).

Instead of asking for JSON in prose and parsing the reply, declare the class
schema as a Converse tool and force the model to call it — so the response SHAPE
is enforced by the API.

**The null hypothesis is live.** Forcing constrains shape, not values: a model
that would have emitted a stray key may instead emit a worse value that fits. So
these tests pin *mechanics and safety*, not benefit; whether it helps is the
benchmark `forcing` suite's question, and "it does not" is an acceptable answer.

The mechanics that must not break, in order of blast radius:

1. **Off by default.** No existing deployment may change behaviour.
2. **Never silently lose an extraction.** A model may accept the toolConfig and
   answer in prose anyway (a normal outcome, `stopReason: end_turn`), and two
   routes cannot carry a toolConfig at all. Both must fall back, not fail.
3. **No field may be renamed.** Bedrock rejects names four shipped presets use, so
   names are sanitized outbound and restored inbound (#709).
4. **The skip/unhonored reason must be recorded**, or an A/B cannot tell "forcing
   had no effect" from "forcing never ran".
"""

from __future__ import annotations

import pytest

from idp_common.bedrock.tool_schema import find_invalid_property_names
from idp_common.extraction.forced_tool import (
    EXTRACTION_TOOL_NAME,
    build_extraction_tool_config,
    forced_tool_choice,
    restore_extracted_fields,
    should_force_tool,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "Account Number": {"type": "string"},  # a name Bedrock rejects
        "amount": {"type": "number"},
    },
    "required": ["Account Number"],
}


class TestToolConfig:
    def test_it_is_a_valid_converse_toolconfig(self):
        cfg, _ = build_extraction_tool_config(SCHEMA)
        (tool,) = cfg["tools"]
        spec = tool["toolSpec"]
        assert spec["name"] == EXTRACTION_TOOL_NAME
        assert spec["description"]
        assert spec["inputSchema"]["json"]["type"] == "object"

    def test_property_names_are_wire_valid(self):
        """The whole point of depending on #709: `"Account Number"` is rejected."""
        cfg, _ = build_extraction_tool_config(SCHEMA)
        schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert find_invalid_property_names(schema) == []

    def test_the_client_guard_accepts_what_we_build(self):
        """End to end: the two halves must actually fit together."""
        from idp_common.bedrock.client import BedrockClient

        cfg, _ = build_extraction_tool_config(SCHEMA)
        BedrockClient._reject_invalid_tool_property_names(cfg)  # must not raise

    def test_a_root_without_type_object_is_coerced(self):
        """Converse wants an object at the root; do not trust the config."""
        cfg, _ = build_extraction_tool_config({"properties": {"a": {"type": "string"}}})
        assert cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"

    def test_it_is_deterministic(self):
        """Tools render BEFORE the system prompt in the cache prefix, so a
        toolConfig that varied run to run would invalidate the whole prefix."""
        a, _ = build_extraction_tool_config(SCHEMA)
        b, _ = build_extraction_tool_config(SCHEMA)
        assert a == b

    def test_the_choice_names_the_tool(self):
        """`{"any": {}}` would also force a tool; naming it is stricter and
        survives a request that later declares more than one."""
        assert forced_tool_choice() == {"tool": {"name": EXTRACTION_TOOL_NAME}}


class TestNoFieldIsRenamed:
    def test_the_response_restores_the_authored_names(self):
        cfg, name_map = build_extraction_tool_config(SCHEMA)
        sent = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]
        # what the model returns, keyed by the SANITIZED names
        tool_input = {k: "v" for k in sent}
        restored = restore_extracted_fields(tool_input, name_map)
        assert set(restored) == {"Account Number", "amount"}

    def test_none_passes_through(self):
        """So the caller can branch once on the combined result."""
        _cfg, name_map = build_extraction_tool_config(SCHEMA)
        assert restore_extracted_fields(None, name_map) is None

    def test_a_non_dict_tool_input_is_rejected_not_coerced(self):
        assert restore_extracted_fields("nonsense", None) is None  # type: ignore[arg-type]


class TestShouldForce:
    def test_off_by_default_is_not_reported_as_a_skip(self):
        """`enabled=False` is a choice, not a capability problem — reporting it as
        a skip would fill every section's metadata with noise."""
        force, reason = should_force_tool("us.anthropic.claude-sonnet-5", False, SCHEMA)
        assert force is False and reason is None

    def test_a_converse_model_forces(self):
        force, reason = should_force_tool("us.anthropic.claude-sonnet-5", True, SCHEMA)
        assert force is True and reason is None

    @pytest.mark.parametrize("model_id", ["LambdaHook", "openai.gpt-5-2025-08-07"])
    def test_routes_that_cannot_carry_a_toolconfig_skip_with_a_reason(self, model_id):
        """These bypass Converse entirely. Skipping silently would make an A/B
        unreadable, so the reason is returned for the audit metadata."""
        force, reason = should_force_tool(model_id, True, SCHEMA)
        assert force is False
        assert reason and "Converse" in reason

    @pytest.mark.parametrize(
        "schema", [None, {}, {"type": "object"}, {"properties": {}}]
    )
    def test_a_class_with_no_properties_skips_with_a_reason(self, schema):
        force, reason = should_force_tool("us.anthropic.claude-sonnet-5", True, schema)
        assert force is False
        assert reason and "no properties" in reason


class TestEveryShippedPresetCanBeForced:
    """The four presets #709 names would have broken this outright."""

    @staticmethod
    def _classes():
        import pathlib

        import yaml

        root = (
            pathlib.Path(__file__).resolve().parents[5] / "config_library" / "unified"
        )
        if not root.is_dir():  # pragma: no cover
            pytest.skip(f"{root} not present")
        out = []
        for f in sorted(root.rglob("*.yaml")):
            try:
                cfg = yaml.safe_load(f.read_text())
            except Exception:
                continue
            if isinstance(cfg, dict):
                for c in cfg.get("classes") or []:
                    if isinstance(c, dict) and c.get("properties"):
                        out.append((f.parent.name, c.get("$id") or "?", c))
        return out

    def test_the_sweep_is_not_vacuous(self):
        assert len(self._classes()) > 20

    def test_every_class_builds_a_wire_valid_tool_config(self):
        from idp_common.bedrock.client import BedrockClient

        failures = []
        for preset, label, schema in self._classes():
            cfg, _ = build_extraction_tool_config(schema)
            try:
                BedrockClient._reject_invalid_tool_property_names(cfg)
            except ValueError as e:
                failures.append(f"{preset}::{label}: {e}")
        assert not failures, "\n".join(failures)

    def test_every_class_round_trips_its_top_level_names(self):
        failures = []
        for preset, label, schema in self._classes():
            cfg, name_map = build_extraction_tool_config(schema)
            sent = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]
            restored = restore_extracted_fields({k: None for k in sent}, name_map)
            if set(restored or {}) != set(schema["properties"]):
                failures.append(f"{preset}::{label}")
        assert not failures, "\n".join(failures)


class TestConfigWiring:
    def test_disabled_by_default(self):
        from idp_common.config.models import IDPConfig

        ft = IDPConfig().extraction.forced_tool
        assert ft.enabled is False
        assert ft.fallback_to_prompt is True

    def test_switchable_on(self):
        from idp_common.config.models import IDPConfig

        cfg = IDPConfig(**{"extraction": {"forced_tool": {"enabled": True}}})
        assert cfg.extraction.forced_tool.enabled is True
