# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ActiveProfileSettingsProvider: active profile, cache window, fallback."""

import logging
import subprocess
import sys
from unittest import mock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.notifications.models import NotificationSettings
from idp_common.notifications.settings import ActiveProfileSettingsProvider

_MANAGER = "idp_common.config.configuration_manager.ConfigurationManager"
_LOGGER = "idp_common.notifications.settings"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _config(retention="45", cap="10"):
    return IDPConfig.model_validate(
        {"hitl": {"notifications": {"retention_days": retention, "max_per_user": cap}}}
    )


@pytest.mark.unit
class TestActiveProfile:
    def test_reads_the_active_version(self):
        with mock.patch(_MANAGER) as manager_cls:
            manager = manager_cls.return_value
            manager.resolve_active_version.return_value = "prod"
            manager.get_merged_configuration.return_value = _config()
            provider = ActiveProfileSettingsProvider(
                config_table_name="cfg", region="us-east-1"
            )
            settings = provider()
        manager_cls.assert_called_once_with(table_name="cfg", region="us-east-1")
        manager.resolve_active_version.assert_called_once_with()
        manager.get_merged_configuration.assert_called_once_with("prod")
        assert settings == NotificationSettings(retention_days=45, max_per_user=10)

    def test_dict_config_is_accepted(self):
        with mock.patch(_MANAGER) as manager_cls:
            manager = manager_cls.return_value
            manager.resolve_active_version.return_value = "default"
            manager.get_merged_configuration.return_value = {
                "hitl": {"notifications": {"retention_days": "7"}}
            }
            settings = ActiveProfileSettingsProvider(config_table_name="cfg")()
        assert settings == NotificationSettings(retention_days=7, max_per_user=50)

    def test_cache_window(self):
        clock = _Clock()
        with mock.patch(_MANAGER) as manager_cls:
            manager = manager_cls.return_value
            manager.resolve_active_version.return_value = "v"
            manager.get_merged_configuration.return_value = _config()
            provider = ActiveProfileSettingsProvider(
                config_table_name="cfg", clock=clock
            )
            provider()
            clock.now += 59
            provider.get()
            assert manager.get_merged_configuration.call_count == 1
            clock.now += 2  # 61 s after the first load
            provider()
            assert manager.get_merged_configuration.call_count == 2


@pytest.mark.unit
class TestFallback:
    def test_unset_table_gives_defaults_and_one_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        clock = _Clock()
        with mock.patch(_MANAGER) as manager_cls:
            provider = ActiveProfileSettingsProvider(clock=clock)
            assert provider() == NotificationSettings()
            clock.now += 30
            assert provider() == NotificationSettings()
        manager_cls.assert_not_called()
        warnings = [r for r in caplog.records if "inbox_settings_fallback" in r.message]
        assert len(warnings) == 1
        assert "reason=unset" in warnings[0].message

    def test_manager_failure_gives_defaults_and_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with mock.patch(_MANAGER) as manager_cls:
            manager_cls.return_value.resolve_active_version.side_effect = RuntimeError(
                "boom"
            )
            settings = ActiveProfileSettingsProvider(config_table_name="cfg")()
        assert settings == NotificationSettings()
        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "inbox_settings_fallback" in m and "RuntimeError" in m for m in messages
        )

    def test_missing_profile_falls_back(self, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with mock.patch(_MANAGER) as manager_cls:
            manager_cls.return_value.get_merged_configuration.return_value = None
            assert ActiveProfileSettingsProvider(config_table_name="c")() == (
                NotificationSettings()
            )
        assert any("reason=not_found" in r.message for r in caplog.records)


@pytest.mark.unit
class TestLazyImport:
    def test_importing_settings_does_not_import_config(self):
        code = (
            "import sys, idp_common.notifications.settings\n"
            "loaded = sorted(m for m in sys.modules if m.startswith('idp_common.config'))\n"
            "print(loaded)\n"
            "raise SystemExit(1 if loaded else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
