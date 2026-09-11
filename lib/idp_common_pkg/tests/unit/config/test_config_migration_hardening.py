# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hardening tests for the v0.5 -> v0.6 config migration.

Covers three things the migration must guarantee for every pre-0.6 config:

1. The ``agentic.enabled`` footgun: a delta that sets
   ``extraction.agentic.enabled`` but omits ``extraction.mode`` must keep the
   author's intent after being merged onto the v0.6 defaults (which carry
   ``mode: simple``). Before the durable fix, ``reconcile_mode_and_agentic``
   silently flipped ``agentic.enabled`` back to False (the Step-8 / Nuveen
   900s-timeout bug).
2. A representative full v0.5-shaped config migrates to the correct v0.6 shape.
3. EVERY bundled config under ``config_library/`` and ``scripts/sdlc/config/``
   migrates + validates cleanly with no surviving legacy markers.
"""

from pathlib import Path

import pytest
import yaml

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations.v05_to_v06 import (
    TARGET_VERSION as V05_TO_V06_TARGET,
)
from idp_common.config.migrations.v05_to_v06 import migrate_v05_to_v06
from idp_common.config.models import CONFIG_FORMAT_VERSION, IDPConfig

# --------------------------------------------------------------------------- #
# Config discovery                                                            #
# --------------------------------------------------------------------------- #
# repo root: lib/idp_common_pkg/tests/unit/config/<this file> -> up 5 parents.
_REPO_ROOT = Path(__file__).resolve().parents[5]

# Non-config YAMLs that live under config_library/ (pricing tables, model
# limits, marketplace listings, etc.) — these are NOT full IDP configs.
_NON_CONFIG_YAMLS = {
    "pricing.yaml",
    "model_config_limits.yaml",
    "finetuning_models.yaml",
    "extensions-marketplace.yaml",
    "extensions-oss.yaml",
}


def _discover_full_configs():
    """Discover every full IDP config YAML under config_library + scripts/sdlc/config."""
    params = []
    roots = [
        _REPO_ROOT / "config_library",
        _REPO_ROOT / "scripts" / "sdlc" / "config",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.yaml")):
            if path.name in _NON_CONFIG_YAMLS:
                continue
            if path.name.startswith("test_"):
                continue
            params.append(pytest.param(path, id=str(path.relative_to(_REPO_ROOT))))
    return params


def _pattern_for(raw: dict) -> str:
    """Pick the merge pattern the way deployment does: BDA -> pattern-1."""
    return "pattern-1" if raw.get("use_bda") else "pattern-2"


def _surviving_legacy_markers(merged: dict) -> list:
    """Return any v0.5-shaped markers that must NOT survive migration+merge."""
    problems = []
    if isinstance(merged.get("assessment"), dict):
        problems.append("top-level assessment block")
    extraction = merged.get("extraction")
    if isinstance(extraction, dict):
        if "assessment_integration" in extraction:
            problems.append("extraction.assessment_integration")
        confidence = extraction.get("confidence")
        if isinstance(confidence, dict) and "granular" in confidence:
            problems.append("extraction.confidence.granular")
    return problems


# --------------------------------------------------------------------------- #
# 1. The footgun: agentic.enabled without mode                                #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAgenticEnabledWithoutModeFootgun:
    """agentic.enabled set but mode omitted must PRESERVE the author's intent.

    This supersedes the KNOWN-behavior tests in
    ``fix/step8-nuveen-agentic-mode``'s ``TestAgenticEnabledRequiresMode`` (which
    asserted the OLD silent-disable behavior). The durable fix lives in the
    migration: it pins ``extraction.mode`` from ``agentic.enabled`` on the DELTA
    before the merge, so the intent wins the deep-merge instead of being reverted
    by the default's ``mode: simple``.
    """

    # --- raw-migration (delta) level ---------------------------------------- #
    def test_migration_pins_mode_advanced_when_enabled_true(self):
        out = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": True}}})
        assert out["extraction"]["mode"] == "advanced"
        # agentic.enabled is untouched by the migration itself
        assert out["extraction"]["agentic"]["enabled"] is True

    def test_migration_pins_mode_simple_when_enabled_false(self):
        out = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": False}}})
        assert out["extraction"]["mode"] == "simple"

    def test_migration_accepts_stringy_enabled_true(self):
        out = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": "true"}}})
        assert out["extraction"]["mode"] == "advanced"

    def test_migration_accepts_int_truthy_enabled(self):
        # Pydantic coerces int 1 -> True; the migration must agree so the pinned
        # mode doesn't disagree with the eventual reconciled agentic.enabled.
        out = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": 1}}})
        assert out["extraction"]["mode"] == "advanced"
        out0 = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": 0}}})
        assert out0["extraction"]["mode"] == "simple"

    def test_migration_never_overrides_explicit_mode(self):
        # Explicit mode is authoritative and must NOT be rewritten even if it
        # contradicts agentic.enabled.
        out = migrate_v05_to_v06(
            {"extraction": {"mode": "simple", "agentic": {"enabled": True}}}
        )
        assert out["extraction"]["mode"] == "simple"

    def test_migration_no_mode_when_agentic_enabled_absent(self):
        # A sparse delta that never mentions agentic.enabled must not sprout mode.
        out = migrate_v05_to_v06({"extraction": {"model": "us.x"}})
        assert "mode" not in out["extraction"]

    def test_migration_pin_is_idempotent(self):
        once = migrate_v05_to_v06({"extraction": {"agentic": {"enabled": True}}})
        twice = migrate_v05_to_v06(once)
        assert once == twice
        assert twice["extraction"]["mode"] == "advanced"

    # --- full merge path ----------------------------------------------------- #
    def test_merge_preserves_agentic_enabled_true(self):
        merged = merge_config_with_defaults(
            {"extraction": {"agentic": {"enabled": True}}}, pattern="pattern-2"
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "advanced"
        assert cfg.extraction.agentic.enabled is True

    def test_merge_preserves_agentic_disabled_false(self):
        merged = merge_config_with_defaults(
            {"extraction": {"agentic": {"enabled": False}}}, pattern="pattern-2"
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "simple"
        assert cfg.extraction.agentic.enabled is False

    def test_merge_explicit_mode_advanced_still_works(self):
        merged = merge_config_with_defaults(
            {"extraction": {"mode": "advanced", "agentic": {"enabled": True}}},
            pattern="pattern-2",
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "advanced"
        assert cfg.extraction.agentic.enabled is True

    def test_merge_explicit_mode_authoritative_over_agentic(self):
        # mode=simple is authoritative; agentic.enabled=true is reconciled to False.
        # (Intentional: mode stays authoritative; we only pin mode when it's absent.)
        merged = merge_config_with_defaults(
            {"extraction": {"mode": "simple", "agentic": {"enabled": True}}},
            pattern="pattern-2",
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "simple"
        assert cfg.extraction.agentic.enabled is False


# --------------------------------------------------------------------------- #
# 2. Representative full v0.5 config -> v0.6                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRepresentativeV05Config:
    """A realistic v0.5-shaped config migrates to the correct v0.6 shape."""

    V05 = {
        "extraction": {
            "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "assessment_integration": "integrated",
        },
        "assessment": {
            "enabled": True,
            "hitl_enabled": True,
            "default_confidence_threshold": "0.8",
            "model": "us.amazon.nova-lite-v1:0",
            "geometry_mode": "llm_with_ocr_grounding",
            "inshard_list_batch_size": "50",
            "system_prompt": "ASSESS-SP",
            "task_prompt": "ASSESS-TP",
            "granular": {"enabled": True, "max_workers": "20"},
        },
        "classes": [],
    }

    def test_delta_migration_shape(self):
        out = migrate_v05_to_v06(self.V05)
        conf = out["extraction"]["confidence"]
        assert conf["mode"] == "integrated"
        assert conf["model"] == "us.amazon.nova-lite-v1:0"
        assert conf["system_prompt"] == "ASSESS-SP"
        assert conf["task_prompt"] == "ASSESS-TP"
        assert conf["list_batch_size"] == "50"
        # granular assessment retired -> dropped
        assert "granular" not in conf
        # geometry mode renamed
        assert out["extraction"]["geometry"]["mode"] == "llm_grounded"
        # hitl lifted to top level
        assert out["hitl"]["enabled"] is True
        assert out["hitl"]["confidence_threshold"] == "0.8"
        # legacy assessment block dropped, integration key gone
        assert "assessment" not in out
        assert "assessment_integration" not in out["extraction"]
        assert (
            out["config_format_version"] == V05_TO_V06_TARGET
        )  # this step stamps 0.6; the chain then reaches current

    def test_validates_into_idpconfig(self):
        cfg = IDPConfig.model_validate(self.V05)
        assert cfg.config_format_version == CONFIG_FORMAT_VERSION
        assert cfg.extraction.confidence.mode == "integrated"
        assert cfg.extraction.confidence.model.endswith("nova-lite-v1:0")
        assert cfg.extraction.confidence.list_batch_size == 50
        assert cfg.extraction.confidence.system_prompt == "ASSESS-SP"
        assert cfg.extraction.confidence.task_prompt == "ASSESS-TP"
        assert not hasattr(cfg.extraction.confidence, "granular")
        assert cfg.extraction.geometry.mode == "llm_grounded"
        assert cfg.hitl.enabled is True
        assert cfg.hitl.confidence_threshold == 0.8

    def test_migration_idempotent_on_representative(self):
        once = migrate_v05_to_v06(self.V05)
        twice = migrate_v05_to_v06(once)
        assert once == twice


# --------------------------------------------------------------------------- #
# 3. Idempotency of an already-v0.6 config                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAlreadyV06Idempotency:
    def test_already_v06_full_merge_is_noop_on_mode(self):
        merged = merge_config_with_defaults(
            {
                "config_format_version": "0.6",
                "extraction": {"mode": "advanced", "agentic": {"enabled": True}},
            },
            pattern="pattern-2",
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "advanced"
        assert cfg.extraction.agentic.enabled is True

    def test_already_v06_migration_identity_when_no_change(self):
        v06 = {
            "config_format_version": "0.6",
            "extraction": {"confidence": {"mode": "integrated"}},
        }
        # Pure no-op preserves object identity (no agentic footgun to fix).
        assert migrate_v05_to_v06(v06) is v06


# --------------------------------------------------------------------------- #
# 4. Every bundled config migrates + validates cleanly                        #
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAllBundledConfigsMigrate:
    """Parametrized round-trip over every full config in the repo."""

    def test_discovery_found_configs(self):
        # Guard against the discovery silently finding nothing.
        assert len(_discover_full_configs()) >= 20

    @pytest.mark.parametrize("config_path", _discover_full_configs())
    def test_config_migrates_and_validates(self, config_path: Path):
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), f"{config_path} did not parse to a dict"

        pattern = _pattern_for(raw)
        merged = merge_config_with_defaults(raw, pattern=pattern, validate=True)

        # (b) no legacy markers survive
        problems = _surviving_legacy_markers(merged)
        assert not problems, f"{config_path} still carries legacy markers: {problems}"

        # (c) format stamped 0.6
        assert str(merged.get("config_format_version")) == CONFIG_FORMAT_VERSION

        # (d) v0.6 blocks present
        extraction = merged["extraction"]
        assert isinstance(extraction.get("confidence"), dict)
        assert isinstance(extraction.get("geometry"), dict)
        assert isinstance(merged.get("hitl"), dict)

        # (e) agentic/advanced intent preserved (footgun guard)
        src_ex = raw.get("extraction") or {}
        src_agentic = (src_ex.get("agentic") or {}).get("enabled")
        src_agentic_intent = src_agentic is True or (
            isinstance(src_agentic, str)
            and src_agentic.strip().lower() in ("true", "1", "yes", "on")
        )
        src_mode_advanced = str(src_ex.get("mode", "")).strip().lower() == "advanced"
        if src_agentic_intent or src_mode_advanced:
            cfg = IDPConfig(**merged)
            assert cfg.extraction.agentic.enabled is True, (
                f"{config_path} expressed advanced/agentic intent but merged config "
                f"has agentic.enabled=False (footgun regression)"
            )
