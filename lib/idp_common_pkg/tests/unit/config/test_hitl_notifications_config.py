# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`hitl.notifications` (Inbox retention and cap): model, defaults and schema parity."""

from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.notifications._cfn_yaml import load_cfn_yaml

from idp_common.config.models import (
    ConfigurationRecord,
    HITLConfig,
    HITLNotificationsConfig,
    IDPConfig,
)

_PKG_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _PKG_ROOT.parents[1]
_BASE_CONFIDENCE = (
    _PKG_ROOT / "idp_common" / "config" / "system_defaults" / "base-confidence.yaml"
)
_UNIFIED_TEMPLATE = _REPO_ROOT / "patterns" / "unified" / "template.yaml"

_FIELDS = ("retention_days", "max_per_user")


def _find_hitl_schemas(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        hitl = node.get("hitl")
        if isinstance(hitl, dict) and isinstance(hitl.get("properties"), dict):
            yield hitl
        for value in node.values():
            yield from _find_hitl_schemas(value)
    elif isinstance(node, list):
        for value in node:
            yield from _find_hitl_schemas(value)


@pytest.mark.unit
class TestModel:
    def test_defaults(self):
        cfg = HITLNotificationsConfig()
        assert (cfg.retention_days, cfg.max_per_user) == (30, 50)
        assert IDPConfig().hitl.notifications.retention_days == 30
        assert IDPConfig().hitl.notifications.max_per_user == 50

    @pytest.mark.parametrize("value", ["30", 30.0, "30.0", " 30 "])
    def test_stringified_and_integral_values_coerce(self, value):
        cfg = HITLNotificationsConfig(retention_days=value, max_per_user=value)
        assert cfg.retention_days == 30 and isinstance(cfg.retention_days, int)
        assert cfg.max_per_user == 30 and isinstance(cfg.max_per_user, int)

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_falls_back_to_default(self, value):
        cfg = HITLNotificationsConfig(retention_days=value, max_per_user=value)
        assert (cfg.retention_days, cfg.max_per_user) == (30, 50)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("retention_days", 0),
            ("retention_days", 366),
            ("max_per_user", 0),
            ("max_per_user", 201),
            ("retention_days", 30.5),
            ("retention_days", "30.5"),
            ("max_per_user", "abc"),
            ("max_per_user", True),
            ("retention_days", False),
        ],
    )
    def test_invalid_values_are_rejected(self, field, value):
        with pytest.raises(ValidationError) as exc:
            HITLNotificationsConfig(**{field: value})
        assert exc.value.errors()[0]["loc"] == (field,)

    @pytest.mark.parametrize("value", [[30], {"days": 30}])
    def test_a_non_scalar_is_rejected_naming_its_type(self, value):
        with pytest.raises(ValidationError) as exc:
            HITLNotificationsConfig(retention_days=value)
        assert f"got {type(value).__name__}" in exc.value.errors()[0]["msg"]

    def test_missing_section_gets_defaults(self):
        cfg = IDPConfig.model_validate({"hitl": {"enabled": True}})
        assert cfg.hitl.enabled is True
        assert cfg.hitl.notifications == HITLNotificationsConfig()

    def test_hitl_config_carries_notifications(self):
        assert HITLConfig().notifications.max_per_user == 50

    def test_configuration_record_round_trip_keeps_ints(self):
        config = IDPConfig.model_validate(
            {"hitl": {"notifications": {"retention_days": 45, "max_per_user": 10}}}
        )
        record = ConfigurationRecord(configuration_type="Custom", config=config)
        item = record.to_dynamodb_item()
        stored = item["hitl"]["notifications"]
        assert stored["retention_days"] == "45", "numbers are stored stringified"
        restored = ConfigurationRecord.from_dynamodb_item(item).config
        assert isinstance(restored, IDPConfig)
        assert restored.hitl.notifications.retention_days == 45
        assert restored.hitl.notifications.max_per_user == 10
        assert isinstance(restored.hitl.notifications.retention_days, int)


@pytest.mark.unit
class TestSystemDefaults:
    def test_base_confidence_values_equal_model_defaults(self):
        data = yaml.safe_load(_BASE_CONFIDENCE.read_text(encoding="utf-8"))
        section = data["hitl"]["notifications"]
        for field in _FIELDS:
            assert section[field] == HITLNotificationsConfig.model_fields[field].default


@pytest.mark.unit
class TestSchemaParity:
    def test_schema_parity(self):
        template = load_cfn_yaml(_UNIFIED_TEMPLATE)
        schemas = [
            h
            for h in _find_hitl_schemas(template)
            if "notifications" in h["properties"]
        ]
        assert len(schemas) == 1, "expected exactly one hitl ConfigSchema block"
        props = schemas[0]["properties"]["notifications"]["properties"]
        assert set(props) == set(_FIELDS)
        for field in _FIELDS:
            info = HITLNotificationsConfig.model_fields[field]
            bounds = {type(m).__name__: m for m in info.metadata}
            assert props[field]["type"] == "number"
            assert props[field]["minimum"] == bounds["Ge"].ge, field
            assert props[field]["maximum"] == bounds["Le"].le, field
            assert props[field]["default"] == info.default, field
