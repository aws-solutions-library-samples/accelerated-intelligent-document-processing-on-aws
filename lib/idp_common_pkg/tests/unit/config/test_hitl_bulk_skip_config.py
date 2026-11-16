# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`hitl.bulk_skip_sync_limit`: model clamp, defaults, schema parity, cached reader."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.config.test_hitl_notifications_config import (
    _BASE_CONFIDENCE,
    _UNIFIED_TEMPLATE,
    _find_hitl_schemas,
)
from tests.unit.notifications._cfn_yaml import load_cfn_yaml

from idp_common.config.active_profile import ActiveProfileValue
from idp_common.config.models import HITLConfig, IDPConfig
from idp_common.review_queues import bulk_settings

_MANAGER = "idp_common.config.configuration_manager.ConfigurationManager"


@pytest.mark.unit
class TestModel:
    def test_default(self):
        assert HITLConfig().bulk_skip_sync_limit == 50
        assert IDPConfig().hitl.bulk_skip_sync_limit == 50

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("150", 100),
            (150, 100),
            (0, 1),
            ("25", 25),
            (25.0, 25),
            (None, 50),
            ("", 50),
        ],
    )
    def test_clamped_never_rejected(self, raw, expected):
        assert HITLConfig(bulk_skip_sync_limit=raw).bulk_skip_sync_limit == expected

    @pytest.mark.parametrize("raw", [True, "abc", 12.5])
    def test_non_whole_numbers_rejected(self, raw):
        with pytest.raises(ValidationError):
            HITLConfig(bulk_skip_sync_limit=raw)


@pytest.mark.unit
class TestDefaultsAndSchema:
    def test_base_confidence_equals_model_default(self):
        data = yaml.safe_load(Path(_BASE_CONFIDENCE).read_text(encoding="utf-8"))
        assert (
            data["hitl"]["bulk_skip_sync_limit"]
            == HITLConfig.model_fields["bulk_skip_sync_limit"].default
        )

    def test_schema_parity(self):
        template = load_cfn_yaml(_UNIFIED_TEMPLATE)
        schemas = [
            h
            for h in _find_hitl_schemas(template)
            if "bulk_skip_sync_limit" in h["properties"]
        ]
        assert len(schemas) == 1
        prop = schemas[0]["properties"]["bulk_skip_sync_limit"]
        assert prop["type"] == "number"
        assert prop["minimum"] == 1
        assert prop["maximum"] == bulk_settings.MAX_SYNC_LIMIT
        assert (
            prop["default"] == HITLConfig.model_fields["bulk_skip_sync_limit"].default
        )


@pytest.mark.unit
class TestActiveProfileValue:
    def test_caches_within_ttl_and_reloads_after(self):
        now = [0.0]
        calls = []

        def reader(config):
            calls.append(config)
            return config["v"]

        value = ActiveProfileValue(
            reader, lambda: -1, config_table_name="t", clock=lambda: now[0]
        )
        with patch(_MANAGER) as manager_cls:
            manager = manager_cls.return_value
            manager.resolve_active_version.return_value = "p"
            manager.get_merged_configuration.return_value = {"v": 7}
            assert value() == 7
            assert value() == 7
            assert len(calls) == 1
            now[0] = 61.0
            assert value.get() == 7
            assert len(calls) == 2

    def test_falls_back_on_failure(self):
        value = ActiveProfileValue(lambda c: 1, lambda: 42, config_table_name="t")
        with patch(_MANAGER, side_effect=RuntimeError("boom")):
            assert value() == 42

    def test_falls_back_when_table_unset(self, monkeypatch):
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
        assert ActiveProfileValue(lambda c: 1, lambda: 9)() == 9


@pytest.mark.unit
class TestEffectiveSyncLimit:
    @pytest.mark.parametrize("stored,expected", [(500, 100), (0, 1), (30, 30)])
    def test_clamped(self, monkeypatch, stored, expected):
        class _Stub:
            def get(self):
                return stored

        monkeypatch.setattr(bulk_settings, "_PROVIDER", _Stub())
        assert bulk_settings.effective_sync_limit() == expected

    def test_reads_dict_config(self):
        assert bulk_settings._read_limit({"hitl": {"bulk_skip_sync_limit": "70"}}) == 70
        assert bulk_settings._read_limit({}) == bulk_settings.DEFAULT_SYNC_LIMIT
        assert bulk_settings._read_limit(IDPConfig()) == 50
