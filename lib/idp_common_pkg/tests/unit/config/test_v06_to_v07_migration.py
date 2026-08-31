# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.6 -> v0.7: ``extraction.agentic.validation`` moves up to
``extraction.validation``.

The load-bearing risk in a config MOVE is not the transform, it is the call
sites: a sparse override delta carrying the legacy shape must survive being
deep-merged onto the (already-new-shaped) defaults. Getting that wrong silently
drops the user's setting, which is the P0 bug ``test_merge_migration_order.py``
pins for the v0.5 -> v0.6 hop. The same shape is pinned here.
"""

from __future__ import annotations

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations import migrate_config
from idp_common.config.migrations.v06_to_v07 import (
    TARGET_VERSION,
    migrate_v06_to_v07,
)
from idp_common.config.models import CONFIG_FORMAT_VERSION, IDPConfig


class TestMove:
    def test_moves_the_block_up_one_level(self):
        out = migrate_v06_to_v07(
            {
                "extraction": {
                    "agentic": {
                        "enabled": True,
                        "validation": {"enabled": True, "fail_action": "reject"},
                    }
                }
            }
        )
        assert out["extraction"]["validation"] == {
            "enabled": True,
            "fail_action": "reject",
        }
        assert "validation" not in out["extraction"]["agentic"]
        assert out["extraction"]["agentic"]["enabled"] is True

    def test_drops_an_agentic_block_left_empty_by_the_move(self):
        out = migrate_v06_to_v07(
            {"extraction": {"agentic": {"validation": {"enabled": True}}}}
        )
        assert out["extraction"]["validation"] == {"enabled": True}
        assert "agentic" not in out["extraction"]

    def test_explicit_new_location_wins_over_migrated_legacy(self):
        """Re-running over a hybrid must not clobber a deliberate setting."""
        out = migrate_v06_to_v07(
            {
                "extraction": {
                    "agentic": {"validation": {"fail_action": "escalate"}},
                    "validation": {"fail_action": "warn"},
                }
            }
        )
        assert out["extraction"]["validation"]["fail_action"] == "warn"

    def test_merges_disjoint_keys_from_both_locations(self):
        out = migrate_v06_to_v07(
            {
                "extraction": {
                    "agentic": {"validation": {"check_formats": False}},
                    "validation": {"enabled": True},
                }
            }
        )
        assert out["extraction"]["validation"] == {
            "check_formats": False,
            "enabled": True,
        }

    def test_non_mapping_legacy_value_is_dropped_not_relocated(self):
        out = migrate_v06_to_v07(
            {"extraction": {"agentic": {"validation": "yes", "enabled": True}}}
        )
        assert "validation" not in out["extraction"]
        assert "validation" not in out["extraction"]["agentic"]


class TestIdempotenceAndScope:
    def test_idempotent(self):
        src = {
            "extraction": {"agentic": {"validation": {"enabled": True}}},
        }
        once = migrate_v06_to_v07(src)
        twice = migrate_v06_to_v07(once)
        assert once == twice

    def test_input_is_not_mutated(self):
        src = {"extraction": {"agentic": {"validation": {"enabled": True}}}}
        migrate_v06_to_v07(src)
        assert src == {"extraction": {"agentic": {"validation": {"enabled": True}}}}

    def test_already_current_is_a_pure_noop_preserving_identity(self):
        cfg = {"config_format_version": TARGET_VERSION, "extraction": {"model": "x"}}
        assert migrate_v06_to_v07(cfg) is cfg

    def test_sparse_delta_only_touches_present_keys(self):
        out = migrate_v06_to_v07({"extraction": {"model": "us.x"}})
        assert "validation" not in out["extraction"]
        assert out["extraction"] == {"model": "us.x"}

    def test_stamps_the_target_version(self):
        out = migrate_v06_to_v07({"extraction": {"model": "us.x"}})
        assert out["config_format_version"] == TARGET_VERSION

    def test_non_dict_passthrough(self):
        assert migrate_v06_to_v07(None) is None  # type: ignore[arg-type]
        assert migrate_v06_to_v07([1, 2]) == [1, 2]  # type: ignore[arg-type]


class TestLegacyMarkerTrigger:
    def test_stamped_current_but_legacy_shaped_is_still_migrated(self):
        """The stamp alone is not a sufficient trigger.

        The deep-merge path can produce a dict stamped with the CURRENT version
        (inherited from the full default) that still carries a legacy-shaped
        delta from a sparse custom override. Skipping it would silently drop the
        user's setting.
        """
        out = migrate_v06_to_v07(
            {
                "config_format_version": TARGET_VERSION,
                "extraction": {"agentic": {"validation": {"fail_action": "reject"}}},
            }
        )
        assert out["extraction"]["validation"]["fail_action"] == "reject"
        assert "agentic" not in out["extraction"]


class TestChain:
    def test_chain_brings_a_v05_config_all_the_way_to_current(self):
        out = migrate_config(
            {
                "config_format_version": "0.5",
                "assessment": {"enabled": True, "model": "us.amazon.nova-lite-v1:0"},
                "extraction": {"agentic": {"validation": {"enabled": True}}},
            }
        )
        assert out["config_format_version"] == CONFIG_FORMAT_VERSION
        # v0.5 -> v0.6 hop ran
        assert "assessment" not in out
        assert "confidence" in out["extraction"]
        # v0.6 -> v0.7 hop ran
        assert out["extraction"]["validation"]["enabled"] is True

    def test_chain_is_idempotent(self):
        src = {
            "config_format_version": "0.5",
            "assessment": {"enabled": True},
            "extraction": {"agentic": {"validation": {"enabled": True}}},
        }
        assert migrate_config(migrate_config(src)) == migrate_config(src)


class TestThroughIDPConfig:
    def test_idpconfig_relocates_on_validate(self):
        cfg = IDPConfig(
            **{
                "extraction": {
                    "agentic": {
                        "validation": {"enabled": True, "fail_action": "reject"}
                    }
                }
            }
        )
        assert cfg.extraction.validation.enabled is True
        assert cfg.extraction.validation.fail_action == "reject"

    def test_new_location_is_read_directly(self):
        cfg = IDPConfig(**{"extraction": {"validation": {"fail_action": "escalate"}}})
        assert cfg.extraction.validation.fail_action == "escalate"

    def test_defaults_are_on_and_free(self):
        """v0.7 flips validation on; the default action must not cost money."""
        cfg = IDPConfig()
        assert cfg.extraction.validation.enabled is True
        assert cfg.extraction.validation.fail_action == "warn"


class TestMergeOrder:
    def test_legacy_delta_pinned_to_non_default_values_survives_the_merge(self):
        """P0 regression: migrate BEFORE merge, or the delta is lost.

        Mirrors test_merge_migration_order.py for the v0.6 -> v0.7 hop. The delta
        is pinned to values that differ from the shipped defaults, so a dropped
        delta cannot pass by coincidence.
        """
        legacy_delta = {
            "extraction": {
                "agentic": {
                    "validation": {
                        "enabled": False,  # opposite of the v0.7 default
                        "fail_action": "reject",  # not the default 'warn'
                        "check_formats": False,  # not the default True
                    }
                }
            }
        }
        merged = merge_config_with_defaults(legacy_delta, pattern="pattern-2")

        validation = merged["extraction"]["validation"]
        assert validation["enabled"] is False, (
            "legacy-shaped delta was dropped by the merge — the migration must "
            "run BEFORE the deep merge"
        )
        assert validation["fail_action"] == "reject"
        assert validation["check_formats"] is False
        # And the legacy home is gone from the merged result.
        assert "validation" not in merged["extraction"].get("agentic", {})

    def test_merged_config_is_stamped_current(self):
        merged = merge_config_with_defaults(
            {"extraction": {"model": "us.x"}}, pattern="pattern-2"
        )
        assert str(merged.get("config_format_version")) == CONFIG_FORMAT_VERSION


class TestRawConfigurationIsMigrated:
    """`get_raw_configuration` must relocate legacy keys.

    This is what the config EDITOR reads (the sparse-delta pattern deliberately
    skips Pydantic defaults). "Raw" means "no defaults injected" — it does NOT
    mean "the stored bytes at whatever path they used to live". After a key MOVE,
    returning the un-migrated delta breaks the editor for every pre-existing
    custom profile: it renders the Schema (new path) populated from a delta that
    still holds the value at the old path, so the panel shows the DEFAULT while
    the runtime uses the user's real value — and a save from that panel persists
    the wrong one.

    Observed live on IDP1 after the v0.7 move: the editor reported validation
    enabled/warn on profiles the pipeline was running disabled/escalate.
    """

    @staticmethod
    def _manager_with(stored):
        from unittest.mock import MagicMock

        from idp_common.config.configuration_manager import ConfigurationManager

        mgr = ConfigurationManager.__new__(ConfigurationManager)
        mgr.table = MagicMock()
        mgr.table.get_item.return_value = {"Item": dict(stored)}
        return mgr

    LEGACY = {
        "Configuration": "Config#legacy-profile",
        "config_format_version": "0.6",
        "extraction": {
            "agentic": {"validation": {"enabled": False, "fail_action": "escalate"}}
        },
    }

    def test_legacy_delta_is_relocated_for_the_editor(self):
        mgr = self._manager_with(self.LEGACY)
        raw = mgr.get_raw_configuration("Config", "legacy-profile")
        assert raw["extraction"]["validation"] == {
            "enabled": False,
            "fail_action": "escalate",
        }, "the editor must see the user's real setting at the CURRENT path"
        assert "validation" not in raw["extraction"].get("agentic", {})

    def test_no_defaults_are_injected(self):
        """The sparse-delta contract still holds: relocation only, no defaults."""
        mgr = self._manager_with(self.LEGACY)
        raw = mgr.get_raw_configuration("Config", "legacy-profile")
        v = raw["extraction"]["validation"]
        assert set(v) == {"enabled", "fail_action"}, v
        assert "coercion" not in raw["extraction"]
        assert set(raw["extraction"]) == {"validation"}, raw["extraction"]

    def test_already_current_delta_is_untouched(self):
        stored = {
            "Configuration": "Config#p",
            "config_format_version": "0.7",
            "extraction": {"validation": {"fail_action": "reject"}},
        }
        mgr = self._manager_with(stored)
        raw = mgr.get_raw_configuration("Config", "p")
        assert raw["extraction"]["validation"] == {"fail_action": "reject"}

    def test_metadata_fields_are_still_stripped(self):
        mgr = self._manager_with(self.LEGACY)
        raw = mgr.get_raw_configuration("Config", "legacy-profile")
        assert "Configuration" not in raw
