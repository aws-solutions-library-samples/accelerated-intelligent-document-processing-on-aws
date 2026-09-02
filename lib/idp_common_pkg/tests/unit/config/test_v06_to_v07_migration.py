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

import pytest

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

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_fail_action_resolves_to_the_field_default(self, blank):
        """An absent/blank action must mean ``warn``, not ``escalate``.

        The coercing validator predates the default flip and still returned
        ``escalate`` for a null, so any stored config, hand-written YAML or CLI
        payload carrying a null would have paid for a stronger-model
        re-extraction on every validation failure -- with validation now ON by
        default. A persisted null is not hypothetical: the config editor has
        stored nulls for scalar fields before (the ``int(None)`` rollback bug).
        """
        cfg = IDPConfig(**{"extraction": {"validation": {"fail_action": blank}}})
        assert cfg.extraction.validation.fail_action == "warn"
        assert (
            cfg.extraction.validation.fail_action
            == IDPConfig().extraction.validation.fail_action
        )


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
        assert raw is not None
        assert "Configuration" not in raw

    @pytest.mark.parametrize(
        "config_type",
        ["Schema", "DefaultPricing", "CustomPricing", "CustomModelConfigLimits"],
    )
    def test_other_record_types_are_not_stamped(self, config_type):
        """The chain describes the IDP config shape only.

        Pricing, model limits and the Schema live in the same table but are
        unrelated documents. Their key paths would never match a migration, but
        the chain stamps ``config_format_version`` unconditionally — so running
        it here would inject a meaningless key that a subsequent save persists.
        """
        stored = {
            "Configuration": f"{config_type}#x",
            "units": {"tokens": 1000},
        }
        mgr = self._manager_with(stored)
        raw = mgr.get_raw_configuration(config_type, "x")
        assert raw is not None
        assert raw == {"units": {"tokens": 1000}}
        assert "config_format_version" not in raw


class TestNewerStampIsNotDowngraded:
    """A migration must never rewrite a stamp BACKWARDS.

    This is the rollback direction: an older release reads a configuration a
    newer one wrote. The migration's transform is a no-op there (the keys it
    relocates are long gone), but it used to stamp its own TARGET_VERSION
    unconditionally — so reading a v0.8 config on a v0.7 release relabelled it
    v0.7, and the roll-forward that followed would then skip the v0.7 -> v0.8
    migration the config actually needed. Silent, and only visible as wrong
    behaviour several releases later.
    """

    def test_newer_stamp_survives_a_pure_read(self):
        cfg = {"config_format_version": "0.9", "extraction": {"mode": "simple"}}
        out = migrate_config(cfg)
        assert out["config_format_version"] == "0.9"

    def test_newer_stamp_survives_even_when_legacy_markers_force_a_run(self):
        """The hybrid case: current-stamped dict carrying a legacy delta."""
        cfg = {
            "config_format_version": "0.9",
            "extraction": {"agentic": {"validation": {"fail_action": "reject"}}},
        }
        out = migrate_config(cfg)
        # The relocation still happens -- that is what the marker check is for.
        assert out["extraction"]["validation"] == {"fail_action": "reject"}
        # ...but the stamp is left alone.
        assert out["config_format_version"] == "0.9"

    def test_older_stamp_is_still_advanced(self):
        for stamp in ("0.5", "0.6"):
            out = migrate_config({"config_format_version": stamp})
            assert out["config_format_version"] == TARGET_VERSION, stamp

    def test_absent_stamp_is_still_advanced(self):
        out = migrate_config({"extraction": {"mode": "simple"}})
        assert out["config_format_version"] == TARGET_VERSION

    def test_two_digit_minor_is_ordered_numerically_not_lexically(self):
        """'0.10' > '0.7' numerically but '0.10' < '0.7' as strings.

        A string compare would treat every 0.10+ config as older and reshape it.
        """
        out = migrate_config({"config_format_version": "0.10"})
        assert out["config_format_version"] == "0.10"

    def test_unparseable_stamp_is_treated_as_predating_versioning(self):
        out = migrate_config({"config_format_version": "not-a-version"})
        assert out["config_format_version"] == TARGET_VERSION


class TestVersionComparatorIsShared:
    """One ordering rule, one implementation.

    Rollback detection in ``src/lambda/update_configuration`` and the
    never-downgrade guard in the migrations must agree about which stamp is
    newer. Two copies of that rule is how they stop agreeing — the repo already
    learned this with the config-scope matcher (one module + a drift test).
    """

    def test_update_configuration_delegates_to_the_shared_parser(self):
        import importlib.util
        import pathlib

        from idp_common.config.migrations._version import parse_version

        # tests/unit/config -> tests/unit -> tests -> idp_common_pkg -> lib -> repo
        repo = pathlib.Path(__file__).resolve().parents[5]
        path = repo / "src/lambda/update_configuration/index.py"
        if not path.exists():  # pragma: no cover - packaged install, not a checkout
            pytest.skip(f"{path} not present in this layout")
        source = path.read_text()
        assert "from idp_common.config.migrations._version import parse_version" in (
            source
        ), "update_configuration must not re-implement the version comparison"

        spec = importlib.util.spec_from_file_location("_uc_index", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for stamp in ("0.6", "0.7", "0.10", "1.0", "0.7.1"):
            assert mod._parse_format_version(stamp) == parse_version(stamp), stamp
        # The adapter's only difference: unknown reads as the oldest format.
        for blank in (None, "", "nonsense"):
            assert mod._parse_format_version(blank) == (0,), blank
            assert parse_version(blank) is None, blank


class TestFloatStampIsNotGuessedAt:
    """An unquoted YAML stamp has already lost its trailing zero.

    ``config_format_version: 0.10`` parses to the float ``0.1`` before any of
    this code runs, so `0.10` and `0.1` are indistinguishable here. Reading it
    anyway would silently mis-order every minor version >= 10; treating it as
    unversioned makes the next write normalize it to a proper string.
    """

    def test_float_stamp_parses_to_none(self):
        from idp_common.config.migrations._version import parse_version

        assert parse_version(0.10) is None
        assert parse_version(0.7) is None

    def test_string_and_int_stamps_still_parse(self):
        from idp_common.config.migrations._version import parse_version

        assert parse_version("0.10") == (0, 10)
        assert parse_version("0.7") == (0, 7)
        assert parse_version(1) == (1,)

    def test_bool_is_not_read_as_an_int(self):
        from idp_common.config.migrations._version import parse_version

        assert parse_version(True) is None

    def test_a_float_stamped_config_is_normalized_to_a_string(self):
        out = migrate_config({"config_format_version": 0.10})
        assert out["config_format_version"] == TARGET_VERSION
        assert isinstance(out["config_format_version"], str)

    def test_ten_is_ordered_above_seven_when_quoted(self):
        from idp_common.config.migrations._version import is_newer_than

        assert is_newer_than("0.10", "0.7") is True
        assert is_newer_than("0.7", "0.10") is False
