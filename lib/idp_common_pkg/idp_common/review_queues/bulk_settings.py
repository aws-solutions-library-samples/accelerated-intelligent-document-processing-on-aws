# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``hitl.bulk_skip_sync_limit`` from the ACTIVE Configuration Profile.

A bulk request may span profiles, so the limit is read from the active profile
(cached ~60 s, see :class:`idp_common.config.active_profile.ActiveProfileValue`)
and clamped again here: no stored value can raise the inline path above
:data:`MAX_SYNC_LIMIT`.
"""

from typing import Any, Optional

from idp_common.config.active_profile import ActiveProfileValue

#: The highest inline (synchronous) limit, whatever the setting says.
MAX_SYNC_LIMIT = 100
#: The setting's default.
DEFAULT_SYNC_LIMIT = 50


def _read_limit(config: Any) -> int:
    if isinstance(config, dict):
        raw = (config.get("hitl") or {}).get("bulk_skip_sync_limit")
        if raw is None:
            return DEFAULT_SYNC_LIMIT
        from idp_common.config.models import HITLConfig

        return HITLConfig.model_validate(
            {"bulk_skip_sync_limit": raw}
        ).bulk_skip_sync_limit
    return int(config.hitl.bulk_skip_sync_limit)


_PROVIDER: Optional[ActiveProfileValue[int]] = None


def bulk_skip_sync_limit_provider() -> ActiveProfileValue[int]:
    """The module-wide cached reader (one per Lambda container)."""
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = ActiveProfileValue(
            _read_limit, lambda: DEFAULT_SYNC_LIMIT, name="bulk_skip_sync_limit"
        )
    return _PROVIDER


def effective_sync_limit() -> int:
    """The inline limit in force: the setting, clamped to 1-:data:`MAX_SYNC_LIMIT`."""
    return max(1, min(MAX_SYNC_LIMIT, int(bulk_skip_sync_limit_provider().get())))
