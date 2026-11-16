# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One setting read from the ACTIVE Configuration Profile, cached.

``ActiveProfileValue`` reads the active profile through ``idp_common.config``
(Default merged with Custom), maps it with a caller-supplied ``reader`` and caches
the result for ``ttl_seconds``. Any failure, or an unset configuration table, falls
back to ``default`` with one WARNING per cache window, so a configuration problem
never stops the work that needs the setting.
"""

import logging
import os
import threading
import time
from typing import Any, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0

T = TypeVar("T")


class ActiveProfileValue(Generic[T]):
    """Callable returning ``reader(active profile configuration)``, cached."""

    def __init__(
        self,
        reader: Callable[[Any], T],
        default: Callable[[], T],
        *,
        name: str = "setting",
        on_fallback: Optional[Callable[[str], T]] = None,
        config_table_name: Optional[str] = None,
        region: Optional[str] = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._reader = reader
        self._default = default
        self._name = name
        self._on_fallback = on_fallback
        self._config_table_name = config_table_name
        self._region = region
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Optional[T] = None
        self._loaded = False
        self._expires_at = 0.0

    def __call__(self) -> T:
        with self._lock:
            now = self._clock()
            if self._loaded and now < self._expires_at:
                return self._value  # type: ignore[return-value]
            self._value = self._load()
            self._loaded = True
            self._expires_at = now + self._ttl_seconds
            return self._value

    get = __call__

    def _load(self) -> T:
        table_name = self._config_table_name or os.environ.get(
            "CONFIGURATION_TABLE_NAME", ""
        )
        if not table_name:
            return self._fallback("unset")
        try:
            # Lazy (P.1): the configuration manager is heavy.
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager(table_name=table_name, region=self._region)
            version = manager.resolve_active_version()
            config = manager.get_merged_configuration(version)
            if config is None:
                return self._fallback("not_found")
            return self._reader(config)
        except Exception as e:  # any failure falls back; logged, not swallowed
            return self._fallback(type(e).__name__)

    def _fallback(self, reason: str) -> T:
        if self._on_fallback is not None:
            # The caller logs in its own vocabulary and supplies the value.
            return self._on_fallback(reason)
        logger.warning(
            "event=active_profile_setting_fallback setting=%s reason=%s",
            self._name,
            reason,
        )
        return self._default()
