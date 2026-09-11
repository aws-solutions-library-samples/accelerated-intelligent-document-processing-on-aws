# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests: a v0.5-shaped config/delta must be migrated BEFORE it is merged
onto the v0.6 system defaults, or the migration's "explicit v0.6 keys win" rule lets
the v0.6 DEFAULTS silently clobber the user's migrated customizations.

This guards the in-place-stack-update and config-import (idp-cli/SDK/UI) paths.
See .claude/plans/config-v06-migration-and-test-plan.md (P0 hazard §2.1).
"""

import pytest

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig

# A v0.5-shaped user config that deliberately pins NON-DEFAULT assessment settings.
# Every one of these differs from the v0.6 system default, so if the default wins the
# merge, we can detect the loss.
V05_WITH_NONDEFAULT_ASSESSMENT = {
    "assessment": {
        "enabled": True,
        "model": "us.anthropic.claude-opus-4-8",  # default is haiku
        "inshard_list_batch_size": 50,  # default is 25
        "geometry_mode": "llm_only",  # default is ocr_only -> migrates to 'llm'
    },
    "extraction": {
        "assessment_integration": "integrated"  # default confidence.mode is 'separate'
    },
}


def _assert_customizations_survived(cfg: IDPConfig):
    assert cfg.extraction.confidence.model == "us.anthropic.claude-opus-4-8"
    assert cfg.extraction.confidence.list_batch_size == 50
    assert cfg.extraction.geometry.mode == "llm"
    assert cfg.extraction.confidence.mode == "integrated"


class TestMergeMigrationOrder:
    def test_merge_config_with_defaults_migrates_before_merge(self):
        """T7 — the core P0 repro: merging a v0.5 config onto v0.6 defaults must NOT
        drop the user's pinned assessment model / list_batch_size / geometry / mode."""
        merged = merge_config_with_defaults(
            V05_WITH_NONDEFAULT_ASSESSMENT, pattern="pattern-2"
        )
        # No legacy assessment block should survive the migration.
        assert "assessment" not in merged
        _assert_customizations_survived(IDPConfig(**merged))

    def test_merge_is_idempotent_for_v06_input(self):
        """An already-v0.6 config with pinned confidence survives the merge unchanged
        (migrate=True is a no-op for v0.6 input)."""
        v06 = {
            "extraction": {
                "confidence": {
                    "mode": "integrated",
                    "model": "us.anthropic.claude-opus-4-8",
                    "list_batch_size": 50,
                },
                "geometry": {"mode": "llm"},
            }
        }
        merged = merge_config_with_defaults(v06, pattern="pattern-2")
        _assert_customizations_survived(IDPConfig(**merged))

    def test_migrate_false_reproduces_the_bug(self):
        """Guardrail: with migrate=False the loss is observable — proves the test is
        actually exercising the ordering, not passing for another reason."""
        merged = merge_config_with_defaults(
            V05_WITH_NONDEFAULT_ASSESSMENT, pattern="pattern-2", migrate=False
        )
        cfg = IDPConfig(**merged)
        # With no pre-merge migration the v0.6 defaults win -> customizations lost.
        assert cfg.extraction.confidence.model != "us.anthropic.claude-opus-4-8"
        assert cfg.extraction.confidence.list_batch_size != 50

    def test_partial_v05_delta_survives_merge(self):
        """T10 — a sparse v0.5 delta (only geometry_mode) still lands in the v0.6 home
        without pulling in unrelated defaults over it."""
        merged = merge_config_with_defaults(
            {"assessment": {"geometry_mode": "llm_with_ocr_grounding"}},
            pattern="pattern-2",
        )
        assert IDPConfig(**merged).extraction.geometry.mode == "llm_grounded"


class TestHandleUpdateMigratesBeforeMerge:
    """The ConfigurationManager.handle_update_custom_configuration choke point migrates
    an incoming legacy-shaped delta before merging onto the current/default config."""

    def _manager_with_v06_default(self, monkeypatch):
        from idp_common.config import configuration_manager as cm

        # Build a full v0.6 default IDPConfig from system defaults.
        default_dict = merge_config_with_defaults({}, pattern="pattern-2")
        default_cfg = IDPConfig(**default_dict)

        mgr = cm.ConfigurationManager.__new__(cm.ConfigurationManager)

        saved = {}

        def fake_get_configuration(config_type, version):
            return default_cfg

        def fake_get_raw_configuration(config_type, version):
            return None  # no existing version -> starts from default

        def fake_read_record(config_type, version):
            return None

        def fake_save_configuration(
            config_type, config, version=None, description=None, **kwargs
        ):
            saved["config"] = config
            saved["version"] = version
            return True

        monkeypatch.setattr(mgr, "get_configuration", fake_get_configuration)
        monkeypatch.setattr(
            mgr, "get_raw_configuration", fake_get_raw_configuration, raising=False
        )
        monkeypatch.setattr(mgr, "_read_record", fake_read_record, raising=False)
        monkeypatch.setattr(mgr, "save_configuration", fake_save_configuration)
        return mgr, saved

    def test_save_as_version_migrates_delta(self, monkeypatch):
        """T8 — saveAsVersion with a v0.5-shaped delta preserves customizations."""
        mgr, saved = self._manager_with_v06_default(monkeypatch)
        delta = dict(V05_WITH_NONDEFAULT_ASSESSMENT, saveAsVersion=True)
        mgr.handle_update_custom_configuration(delta, version="myver")
        _assert_customizations_survived(saved["config"])

    def test_normal_update_migrates_delta(self, monkeypatch):
        """T10 — normal update with a v0.5-shaped delta preserves customizations."""
        mgr, saved = self._manager_with_v06_default(monkeypatch)
        mgr.handle_update_custom_configuration(
            dict(V05_WITH_NONDEFAULT_ASSESSMENT), version="myver"
        )
        _assert_customizations_survived(saved["config"])


class TestAgenticEnabledPinsMode:
    """Regression for the Step-8 Nuveen timeout: a config that sets
    ``extraction.agentic.enabled: true`` but omits ``extraction.mode``.

    In v0.6 ``extraction.mode`` is authoritative and ``agentic.enabled`` is
    derived from it (ExtractionConfig.reconcile_mode_and_agentic). Historically
    such a delta had its ``agentic.enabled`` SILENTLY reset to False on upload:
    the merge inherited the default's ``mode: simple``, which forced
    ``agentic.enabled=False`` — overriding the ``true`` the author intended.
    The whole doc then ran traditional single-pass extraction and (for a large
    17-page/532-row doc) timed out the 900s Lambda.

    The durable fix (``_pin_extraction_mode_from_agentic`` in the v0.5->v0.6
    migration, applied to the delta BEFORE the merge) pins ``mode`` from
    ``agentic.enabled`` so the author's intent wins the merge. These tests pin
    that contract. See also test_config_migration_hardening.py.
    """

    def test_agentic_enabled_without_mode_pins_advanced(self):
        """The fix: agentic.enabled=true with no mode is pinned to advanced."""
        merged = merge_config_with_defaults(
            {"extraction": {"agentic": {"enabled": True}}}, pattern="pattern-2"
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "advanced"
        assert cfg.extraction.agentic.enabled is True

    def test_agentic_disabled_without_mode_pins_simple(self):
        """agentic.enabled=false with no mode is pinned to simple."""
        merged = merge_config_with_defaults(
            {"extraction": {"agentic": {"enabled": False}}}, pattern="pattern-2"
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "simple"
        assert cfg.extraction.agentic.enabled is False

    def test_explicit_mode_advanced_keeps_agentic_enabled(self):
        """An explicit mode=advanced is authoritative and survives the merge."""
        merged = merge_config_with_defaults(
            {"extraction": {"mode": "advanced", "agentic": {"enabled": True}}},
            pattern="pattern-2",
        )
        cfg = IDPConfig(**merged)
        assert cfg.extraction.mode == "advanced"
        assert cfg.extraction.agentic.enabled is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
