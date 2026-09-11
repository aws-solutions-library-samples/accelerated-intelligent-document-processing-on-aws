# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the v0.5 -> v0.6 config-shape migration."""

import pytest

from idp_common.config.migrations.v05_to_v06 import (
    TARGET_VERSION as V05_TO_V06_TARGET,
)
from idp_common.config.migrations.v05_to_v06 import migrate_v05_to_v06
from idp_common.config.models import CONFIG_FORMAT_VERSION, IDPConfig


class TestV05ToV06Migration:
    """Pure dict->dict migration behaviour."""

    def test_integration_mode_moves_to_confidence(self):
        out = migrate_v05_to_v06(
            {"extraction": {"assessment_integration": "integrated"}}
        )
        assert out["extraction"]["confidence"]["mode"] == "integrated"
        assert "assessment_integration" not in out["extraction"]

    def test_assessment_inference_knobs_move_to_confidence(self):
        out = migrate_v05_to_v06(
            {
                "assessment": {
                    "enabled": True,
                    "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "system_prompt": "SP",
                    "task_prompt": "TP",
                    "temperature": "0.0",
                    "inshard_list_batch_size": "30",
                    "granular": {"enabled": True, "max_workers": "20"},
                    "image": {"target_width": "100"},
                }
            }
        )
        conf = out["extraction"]["confidence"]
        # enabled=True is the default (mode stays 'separate') so migration doesn't
        # need to force mode; enablement is derived from mode by the model.
        assert conf.get("mode", "separate") != "off"
        assert conf["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert conf["system_prompt"] == "SP"
        # v0.6: the confidence TASK prompt lives in the confidence sub-config
        assert conf["task_prompt"] == "TP"
        assert conf["list_batch_size"] == "30"
        # granular assessment is retired: the key is DROPPED, not carried forward.
        assert "granular" not in conf
        assert conf["image"] == {"target_width": "100"}

    @pytest.mark.parametrize(
        "old,new",
        [
            ("ocr_only", "ocr_only"),
            ("llm_with_ocr_grounding", "llm_grounded"),
            ("llm_only", "llm"),
        ],
    )
    def test_geometry_mode_rename(self, old, new):
        out = migrate_v05_to_v06({"assessment": {"geometry_mode": old}})
        assert out["extraction"]["geometry"]["mode"] == new

    def test_legacy_ground_geometry_false_maps_to_llm(self):
        out = migrate_v05_to_v06({"assessment": {"ground_geometry_in_ocr": False}})
        assert out["extraction"]["geometry"]["mode"] == "llm"

    def test_legacy_ground_geometry_false_ignored_when_geometry_mode_present(self):
        out = migrate_v05_to_v06(
            {
                "assessment": {
                    "ground_geometry_in_ocr": False,
                    "geometry_mode": "ocr_only",
                }
            }
        )
        # explicit geometry_mode wins over the legacy flag
        assert out["extraction"]["geometry"]["mode"] == "ocr_only"

    def test_hitl_moves_to_top_level(self):
        out = migrate_v05_to_v06(
            {
                "assessment": {
                    "hitl_enabled": True,
                    "default_confidence_threshold": "0.7",
                }
            }
        )
        assert out["hitl"]["enabled"] is True
        assert out["hitl"]["confidence_threshold"] == "0.7"

    def test_assessment_block_dropped_entirely(self):
        out = migrate_v05_to_v06(
            {
                "assessment": {
                    "enabled": False,  # -> confidence.mode = off
                    "model": "m",  # -> confidence.model
                    "postHook": [{"featureId": "f", "arn": "a"}],  # dropped (v0.6)
                }
            }
        )
        # the entire assessment block is retired in v0.6
        assert "assessment" not in out
        # legacy disable-via-enabled maps to the new 'off' scoring mode
        assert out["extraction"]["confidence"]["mode"] == "off"
        assert out["extraction"]["confidence"]["model"] == "m"

    def test_stamps_format_version(self):
        out = migrate_v05_to_v06({"assessment": {"enabled": True}})
        assert (
            out["config_format_version"] == V05_TO_V06_TARGET
        )  # this step stamps 0.6; the chain then reaches current

    def test_idempotent(self):
        v05 = {
            "extraction": {"assessment_integration": "separate"},
            "assessment": {
                "model": "m",
                "geometry_mode": "llm_only",
                "hitl_enabled": True,
            },
        }
        once = migrate_v05_to_v06(v05)
        twice = migrate_v05_to_v06(once)
        assert once == twice

    def test_already_v06_untouched(self):
        v06 = {
            "config_format_version": "0.6",
            "extraction": {
                "confidence": {"mode": "integrated"},
                "geometry": {"mode": "off"},
            },
            "hitl": {"enabled": True},
        }
        out = migrate_v05_to_v06(v06)
        assert out == v06

    def test_explicit_v06_keys_win_over_migrated_legacy(self):
        # A hybrid: default already has v0.6 confidence, delta still has legacy.
        # Pre-existing explicit confidence keys should not be clobbered.
        out = migrate_v05_to_v06(
            {
                "extraction": {
                    "assessment_integration": "separate",
                    "confidence": {"mode": "integrated"},
                },
                "assessment": {"model": "legacy-model"},
            }
        )
        # explicit confidence.mode wins; legacy model still folded in
        assert out["extraction"]["confidence"]["mode"] == "integrated"
        assert out["extraction"]["confidence"]["model"] == "legacy-model"

    def test_sparse_delta_only_touches_present_keys(self):
        # A sparse override that only changes extraction.model shouldn't sprout
        # confidence/geometry/hitl blocks.
        out = migrate_v05_to_v06({"extraction": {"model": "us.x"}})
        assert "confidence" not in out["extraction"]
        assert "geometry" not in out["extraction"]
        assert "hitl" not in out
        assert (
            out["config_format_version"] == V05_TO_V06_TARGET
        )  # this step stamps 0.6; the chain then reaches current

    def test_non_dict_passthrough(self):
        assert migrate_v05_to_v06(None) is None  # type: ignore[arg-type]
        assert migrate_v05_to_v06([1, 2]) == [1, 2]  # type: ignore[arg-type]


class TestV05ToV06ThroughIDPConfig:
    """End-to-end: a v0.5 dict validates into a v0.6 IDPConfig."""

    def test_full_v05_config_validates_to_v06(self):
        v05 = {
            "extraction": {
                "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
                "assessment_integration": "integrated",
            },
            "assessment": {
                "enabled": True,
                "hitl_enabled": True,
                "default_confidence_threshold": "0.75",
                "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "geometry_mode": "llm_with_ocr_grounding",
                "inshard_list_batch_size": "40",
                "system_prompt": "SP",
                "task_prompt": "TP",
                "granular": {"enabled": False},
            },
            "classes": [],
        }
        cfg = IDPConfig.model_validate(v05)
        assert cfg.config_format_version == CONFIG_FORMAT_VERSION
        assert cfg.extraction.confidence.mode == "integrated"
        assert cfg.extraction.confidence.enabled is True
        assert cfg.extraction.confidence.model.endswith(
            "claude-haiku-4-5-20251001-v1:0"
        )
        assert cfg.extraction.confidence.list_batch_size == 40
        assert cfg.extraction.confidence.system_prompt == "SP"
        # v0.6: assessment.task_prompt migrates to extraction.confidence.task_prompt
        assert cfg.extraction.confidence.task_prompt == "TP"
        # granular assessment is retired: the field no longer exists on the model.
        assert not hasattr(cfg.extraction.confidence, "granular")
        assert cfg.extraction.geometry.mode == "llm_grounded"
        assert cfg.hitl.enabled is True
        assert cfg.hitl.confidence_threshold == 0.75
        # enablement now lives solely on confidence.enabled
        assert cfg.extraction.confidence.enabled is True
