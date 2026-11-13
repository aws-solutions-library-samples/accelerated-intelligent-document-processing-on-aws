# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Producer settings (retention, cap) from the ACTIVE Configuration Profile.

``hitl.notifications`` is read through ``idp_common.config`` (Default merged with
Custom) and cached per provider for ``ttl_seconds``. Any failure, or an unset
configuration table, falls back to the defaults with one WARNING per cache
window, so a configuration problem never stops a message being delivered.
"""

import logging
import threading
import time
from typing import Any, Callable

from idp_common.notifications.models import NotificationSettings

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0


class ActiveProfileSettingsProvider:
    """Callable returning the current :class:`NotificationSettings`.

    A thin wrapper over :class:`idp_common.config.active_profile.ActiveProfileValue`,
    built on first use so importing this module does not load ``idp_common.config``.
    """

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
        self._value: Any = None

    def __call__(self) -> NotificationSettings:
        with self._lock:
            if self._value is None:
                # Lazy (P.1): idp_common.config is heavy and only producers need it.
                from idp_common.config.active_profile import ActiveProfileValue

                self._value = ActiveProfileValue(
                    _settings_from_config,
                    NotificationSettings,
                    name="inbox_settings",
                    on_fallback=self._fallback,
                    config_table_name=self._config_table_name,
                    region=self._region,
                    ttl_seconds=self._ttl_seconds,
                    clock=self._clock,
                )
        return self._value()

    get = __call__

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
