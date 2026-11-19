# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.10 -> v0.11: ``forced_tool`` / ``structured_output`` ->
``schema_enforcement``.

Every stored configuration must keep doing what it did. The cases that decide that
are the mapping of each legacy combination, the strings the Configuration table
stores booleans as, and the precedence over a value already at the new location —
including the one exception, a default ``mode: off`` that a full snapshot carries
whether or not anyone chose it.
"""

from __future__ import annotations

import pytest

from idp_common.config.migrations import migrate_config
from idp_common.config.migrations.v010_to_v011 import (
    TARGET_VERSION,
    migrate_v010_to_v011,
)
from idp_common.config.models import CONFIG_FORMAT_VERSION, IDPConfig

pytestmark = pytest.mark.unit


def _ext(**extraction):
    return {"config_format_version": "0.10", "extraction": extraction}


def test_the_current_format_is_this_hop():
    assert CONFIG_FORMAT_VERSION == TARGET_VERSION == "0.11"


@pytest.mark.parametrize(
    "forced,structured,mode",
    [
        (True, None, "forced_tool"),
        (None, True, "structured_output"),
        (True, True, "auto"),
        (False, None, "off"),
        (None, False, "off"),
        (False, False, "off"),
        ("true", None, "forced_tool"),
        ("True", "false", "forced_tool"),
        ("false", "true", "structured_output"),
    ],
)
def test_each_legacy_combination_maps_to_the_mode_it_meant(forced, structured, mode):
    ext = {}
    if forced is not None:
        ext["forced_tool"] = {"enabled": forced}
    if structured is not None:
        ext["structured_output"] = {"enabled": structured}
    out = migrate_v010_to_v011(_ext(**ext))
    assert out["extraction"] == {"schema_enforcement": {"mode": mode}}
    assert out["config_format_version"] == "0.11"


def test_fallback_to_prompt_moves_with_it():
    out = migrate_v010_to_v011(
        _ext(forced_tool={"enabled": True, "fallback_to_prompt": False})
    )
    assert out["extraction"]["schema_enforcement"] == {
        "mode": "forced_tool",
        "fallback_to_prompt": False,
    }


def test_a_sparse_delta_without_enabled_moves_only_what_it_has():
    out = migrate_v010_to_v011(_ext(forced_tool={"fallback_to_prompt": False}))
    assert out["extraction"] == {"schema_enforcement": {"fallback_to_prompt": False}}


def test_an_explicit_new_mode_wins_over_a_legacy_flag():
    out = migrate_v010_to_v011(
        _ext(
            forced_tool={"enabled": True},
            schema_enforcement={"mode": "structured_output"},
        )
    )
    assert out["extraction"]["schema_enforcement"]["mode"] == "structured_output"


def test_a_default_off_does_not_silence_a_legacy_enabled_flag():
    """A full snapshot carries ``mode: off`` whether or not anyone chose it, so a
    legacy ``enabled: true`` beside it — the shape a legacy override laid over a
    current default produces — is the intent."""
    out = migrate_v010_to_v011(
        _ext(forced_tool={"enabled": True}, schema_enforcement={"mode": "off"})
    )
    assert out["extraction"]["schema_enforcement"]["mode"] == "forced_tool"


def test_a_default_fallback_does_not_overwrite_a_deliberate_legacy_false():
    out = migrate_v010_to_v011(
        _ext(
            forced_tool={"enabled": True, "fallback_to_prompt": "false"},
            schema_enforcement={"mode": "off", "fallback_to_prompt": True},
        )
    )
    assert out["extraction"]["schema_enforcement"]["fallback_to_prompt"] == "false"


def test_the_legacy_keys_are_removed_and_nothing_else_is_touched():
    cfg = _ext(
        forced_tool={"enabled": True},
        structured_output={"enabled": False},
        model="m",
        temperature="0.0",
    )
    out = migrate_v010_to_v011(cfg)
    assert set(out["extraction"]) == {"schema_enforcement", "model", "temperature"}
    assert cfg["extraction"]["forced_tool"] == {"enabled": True}, "input mutated"


def test_idempotent_and_identity_preserving_when_current():
    once = migrate_v010_to_v011(_ext(forced_tool={"enabled": True}))
    assert migrate_v010_to_v011(once) is once
    current = {"config_format_version": "0.11", "extraction": {"model": "m"}}
    assert migrate_v010_to_v011(current) is current


def test_a_newer_stamp_is_never_downgraded():
    out = migrate_v010_to_v011(
        {
            "config_format_version": "0.12",
            "extraction": {"forced_tool": {"enabled": True}},
        }
    )
    assert out["config_format_version"] == "0.12"
    assert out["extraction"] == {"schema_enforcement": {"mode": "forced_tool"}}


def test_the_whole_chain_reaches_it_from_an_old_stamp_and_from_current_input():
    old = migrate_config(
        {
            "config_format_version": "0.6",
            "extraction": {"forced_tool": {"enabled": True}},
        }
    )
    assert old["extraction"]["schema_enforcement"] == {"mode": "forced_tool"}
    assert old["config_format_version"] == "0.11"
    uploaded = migrate_config(
        {"extraction": {"forced_tool": {"enabled": True}}}, unstamped_is_current=True
    )
    assert uploaded["extraction"]["schema_enforcement"] == {"mode": "forced_tool"}
    assert uploaded["config_format_version"] == "0.11"


def test_a_constructed_config_reads_the_legacy_key():
    cfg = IDPConfig(extraction={"forced_tool": {"enabled": True}})
    assert cfg.extraction.schema_enforcement.mode == "forced_tool"
    assert cfg.extraction.schema_enforcement.wants_forced_tool


@pytest.mark.parametrize(
    "raw,mode", [(False, "off"), ("OFF", "off"), (" Auto ", "auto")]
)
def test_the_mode_accepts_the_spellings_a_yaml_file_produces(raw, mode):
    cfg = IDPConfig(extraction={"schema_enforcement": {"mode": raw}})
    assert cfg.extraction.schema_enforcement.mode == mode
