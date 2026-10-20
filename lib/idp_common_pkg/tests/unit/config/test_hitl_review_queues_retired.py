# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The retired per-profile ``hitl.review_queues`` block still loads, and is dropped.

Production review queues are configured in HITL › Queue Management (the Default
queue's Enabled switch and confidence threshold). A saved configuration that
still carries the block must keep loading, so the model ignores it and the
v0.6 -> v0.7 migration removes it from stored configurations.
"""

import pytest
import yaml
from tests.unit.notifications._cfn_yaml import REPO_ROOT, load_cfn_yaml

from idp_common.config.migrations import migrate_config, migrate_v06_to_v07
from idp_common.config.models import HITLConfig, IDPConfig

OLD_BLOCK = {"enabled": True, "queue": "claims"}
_BASE_CONFIDENCE = (
    REPO_ROOT
    / "lib"
    / "idp_common_pkg"
    / "idp_common"
    / "config"
    / "system_defaults"
    / "base-confidence.yaml"
)
_UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"


@pytest.mark.unit
class TestModel:
    def test_a_stored_block_loads_and_is_not_kept(self):
        cfg = IDPConfig.model_validate(
            {"hitl": {"enabled": True, "review_queues": OLD_BLOCK}}
        )
        assert cfg.hitl.enabled is True
        assert "review_queues" not in cfg.hitl.model_dump()

    def test_the_older_list_shape_loads_too(self):
        cfg = HITLConfig.model_validate(
            {"review_queues": {"queues": [{"name": "claims"}], "lease_minutes": 3}}
        )
        assert "review_queues" not in cfg.model_dump()

    def test_hitl_no_longer_declares_the_block(self):
        assert "review_queues" not in HITLConfig.model_fields


@pytest.mark.unit
class TestMigration:
    def test_the_block_is_dropped_from_a_current_config(self):
        stored = {
            "config_format_version": "0.7",
            "hitl": {"enabled": True, "review_queues": OLD_BLOCK},
        }
        migrated = migrate_v06_to_v07(stored)
        assert migrated["hitl"] == {"enabled": True}
        assert migrated["config_format_version"] == "0.7"
        # The input is not mutated.
        assert stored["hitl"]["review_queues"] == OLD_BLOCK

    def test_a_sparse_delta_carrying_only_the_block_keeps_an_empty_hitl(self):
        assert migrate_config({"hitl": {"review_queues": OLD_BLOCK}})["hitl"] == {}

    def test_a_config_without_the_block_is_untouched(self):
        current = {"config_format_version": "0.7", "hitl": {"enabled": False}}
        assert migrate_v06_to_v07(current) is current


@pytest.mark.unit
class TestShippedDefaults:
    def test_system_defaults_carry_no_block(self):
        data = yaml.safe_load(_BASE_CONFIDENCE.read_text())
        assert "review_queues" not in data["hitl"]

    def test_the_editor_schema_carries_no_block(self):
        def hitl_schemas(node):
            if isinstance(node, dict):
                hitl = node.get("hitl")
                if isinstance(hitl, dict) and isinstance(hitl.get("properties"), dict):
                    yield hitl
                for value in node.values():
                    yield from hitl_schemas(value)
            elif isinstance(node, list):
                for value in node:
                    yield from hitl_schemas(value)

        schemas = list(hitl_schemas(load_cfn_yaml(_UNIFIED_TEMPLATE)))
        assert schemas, "hitl ConfigSchema not found"
        assert all("review_queues" not in s["properties"] for s in schemas)
