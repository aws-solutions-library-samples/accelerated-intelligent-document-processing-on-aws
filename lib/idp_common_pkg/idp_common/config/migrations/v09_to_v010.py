# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.9 -> v0.10: the error analyzer's event limit takes effect.

``parameters.max_log_events`` in the error analyzer's settings used to have no
effect. Both CloudWatch log tools declared ``max_log_events: int = 10`` in their
signatures, so a search returned up to 10 events per log group whatever the
setting said, while the setting's own default was ``5``. From v0.10 the tools read
the setting, and its default is ``10``.

A saved configuration stores a full snapshot of the configuration model, so every
profile and revision saved before v0.10 stores the old default ``5``. Read as
written, all of them would drop from 10 events per log group to 5. This hop
rewrites a stored ``5`` to ``10`` so that their effective limit stays what it was.

⚠️ **A ``5`` an operator chose deliberately is rewritten too.** Nothing in a
pre-v0.10 configuration distinguishes the untouched default from a value someone
typed, because the snapshot stores both the same way. Of the two readings, this
one preserves what every such configuration actually did before the upgrade (10
events, since the setting was ignored); the other would change it. An operator who
wants 5 sets it again in a configuration stamped v0.10, which this hop leaves
alone, as it does every value other than ``5``.

The trigger is the version stamp, unlike the earlier hops, because there is no
legacy key to detect: the old and new shapes are identical and only a value
differs. A configuration with no stamp at all is read as pre-v0.10 (every
earlier hop stamps it on the way here) and has a ``5`` rewritten, which is right
for stored data: the stamp has been written since v0.6, so an unstamped stored
configuration was saved by v0.5 and holds the old default. A configuration being
*saved* is different, because an editor delta never carries a stamp;
``migrate_config(..., unstamped_is_current=True)`` skips this hop for such input.

Deliberately narrow: only ``max_log_events``, only where it equals ``5``, and only
in the two sections that hold ``ErrorAnalyzerParameters``. Nothing else is
reshaped or defaulted, so this is safe to apply on every read of full
configurations and sparse deltas alike.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Tuple

from ._version import is_at_or_after

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.10"

#: The default ``max_log_events`` carried before v0.10, and the one after it. The
#: new value equals the per-log-group limit the tools applied while the setting
#: was ignored.
OLD_DEFAULT_MAX_LOG_EVENTS = 5
NEW_DEFAULT_MAX_LOG_EVENTS = 10

#: The ``agents`` sections whose ``parameters`` is an ``ErrorAnalyzerParameters``.
#: The chat companion holds a copy of the same model, so its stored default moved
#: with the field's default.
PARAMETER_SECTIONS: Tuple[str, ...] = ("error_analyzer", "chat_companion")


def _is_old_default(value: Any) -> bool:
    """True when a stored ``max_log_events`` is the old default ``5``.

    A stored profile head holds its numbers as strings (``"5"``), and a YAML file
    may hold either, so both spellings count.
    """
    if isinstance(value, int):
        return value == OLD_DEFAULT_MAX_LOG_EVENTS
    if isinstance(value, str):
        return value.strip() == str(OLD_DEFAULT_MAX_LOG_EVENTS)
    return False


def _new_value_like(old: Any) -> Any:
    """``10`` in the same representation the stored ``5`` used."""
    if isinstance(old, str):
        return str(NEW_DEFAULT_MAX_LOG_EVENTS)
    return NEW_DEFAULT_MAX_LOG_EVENTS


def migrate_v09_to_v010(config: Any) -> Any:
    """Return a v0.10-stamped copy of ``config`` with an old-default event limit raised.

    A configuration already stamped v0.10 or newer is returned unchanged (the same
    object), whatever its ``max_log_events``. Idempotent.
    """
    if not isinstance(config, dict):
        return config
    if is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
        return config

    result: Dict[str, Any] = copy.deepcopy(config)
    agents = result.get("agents")
    if isinstance(agents, dict):
        for section in PARAMETER_SECTIONS:
            section_cfg = agents.get(section)
            if not isinstance(section_cfg, dict):
                continue
            parameters = section_cfg.get("parameters")
            if not isinstance(parameters, dict):
                continue
            stored = parameters.get("max_log_events")
            if _is_old_default(stored):
                parameters["max_log_events"] = _new_value_like(stored)
                logger.info(
                    "Migrated config v0.9 -> v0.10 "
                    "(agents.%s.parameters.max_log_events 5 -> 10, the limit the "
                    "tools applied while the setting was ignored)",
                    section,
                )

    # Reaching here means the stamp is older than v0.10 or absent, so it is never
    # newer than the target and stamping cannot downgrade it.
    result["config_format_version"] = TARGET_VERSION
    return result
