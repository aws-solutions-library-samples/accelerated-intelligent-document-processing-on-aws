# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Producer settings (retention, cap) from the ACTIVE Configuration Profile.

``hitl.notifications`` is read through ``idp_common.config`` (Default merged with
Custom) and cached per provider for ``ttl_seconds``. Any failure, or an unset
configuration table, falls back to the defaults with one WARNING per cache
window, so a configuration problem never stops a message being delivered.
"""

import logging
import os
import threading
import time
from typing import Any, Callable

from idp_common.notifications.models import NotificationSettings

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0


class ActiveProfileSettingsProvider:
    """Callable returning the current :class:`NotificationSettings`."""

    def __init__(
        self,
        config_table_name: str | None = None,
        region: str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config_table_name = config_table_name
        self._region = region
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: NotificationSettings | None = None
        self._expires_at = 0.0

    def __call__(self) -> NotificationSettings:
        with self._lock:
            now = self._clock()
            if self._value is not None and now < self._expires_at:
                return self._value
            self._value = self._load()
            self._expires_at = now + self._ttl_seconds
            return self._value

    get = __call__

    def _load(self) -> NotificationSettings:
        table_name = self._config_table_name or os.environ.get(
            "CONFIGURATION_TABLE_NAME", ""
        )
        if not table_name:
            return self._fallback("unset")
        try:
            # Lazy (P.1): idp_common.config is heavy and only producers need it.
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager(table_name=table_name, region=self._region)
            version = manager.resolve_active_version()
            config = manager.get_merged_configuration(version)
            if config is None:
                return self._fallback("not_found")
            return _settings_from_config(config)
        except Exception as e:  # any failure falls back; logged, not swallowed
            return self._fallback(type(e).__name__)

    @staticmethod
    def _fallback(reason: str) -> NotificationSettings:
        logger.warning("event=inbox_settings_fallback reason=%s", reason)
        return NotificationSettings()


def _settings_from_config(config: Any) -> NotificationSettings:
    """Map an ``IDPConfig`` (or a plain config dict) to settings."""
    from idp_common.config.models import HITLNotificationsConfig

    if isinstance(config, dict):
        section = HITLNotificationsConfig.model_validate(
            (config.get("hitl") or {}).get("notifications") or {}
        )
    else:
        section = config.hitl.notifications
    return NotificationSettings(
        retention_days=section.retention_days, max_per_user=section.max_per_user
    )
