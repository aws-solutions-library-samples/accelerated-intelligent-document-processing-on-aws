# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config-shape migrations.

Each migration is a pure ``dict -> dict`` transform that upgrades a stored
configuration from one ``config_format_version`` to the next. Migrations are
idempotent so they can be safely applied on every read (see
``IDPConfig.log_deprecated_fields`` and ``ConfigurationManager``).

Migrations chain in order: a stored v0.5 config is brought to the current format
by applying each step in sequence, so every call site should apply the whole
chain via ``migrate_config`` rather than picking a single step.
"""

from typing import Any, Dict

from .v05_to_v06 import _has_legacy_markers as _v05_legacy
from .v05_to_v06 import migrate_v05_to_v06
from .v06_to_v07 import _has_legacy_markers as _v06_legacy
from .v06_to_v07 import migrate_v06_to_v07
from .v07_to_v08 import _has_legacy_markers as _v07_legacy
from .v07_to_v08 import migrate_v07_to_v08
from .v08_to_v09 import _has_flat_keys as _v08_flat
from .v08_to_v09 import _has_prerelease_names as _v08_prerelease
from .v08_to_v09 import migrate_v08_to_v09
from .v09_to_v010 import TARGET_VERSION as V010_TARGET_VERSION
from .v09_to_v010 import migrate_v09_to_v010

__all__ = [
    "migrate_config",
    "migrate_v05_to_v06",
    "migrate_v06_to_v07",
    "migrate_v07_to_v08",
    "migrate_v08_to_v09",
    "migrate_v09_to_v010",
]


def _carries_a_legacy_key(config: Dict[str, Any]) -> bool:
    """True when ``config`` holds a key one of the key-moving hops relocates.

    Such a configuration was written for an earlier format, whatever its stamp
    says, so it is not current input.
    """
    return (
        _v05_legacy(config)
        or _v06_legacy(config)
        or _v07_legacy(config)
        or _v08_flat(config)
        or _v08_prerelease(config)
    )


def migrate_config(config, *, unstamped_is_current: bool = False):
    """Apply every migration in order, bringing ``config`` to the current format.

    Call this rather than an individual step: a v0.5 config needs both hops, and
    a call site that applies only one silently leaves the config a version
    behind. Each step is itself idempotent and a no-op when not needed, so this
    is safe to call on every read.

    ``unstamped_is_current`` is for a configuration someone is saving now, such as
    an editor delta or an uploaded file, as opposed to one read back from storage.
    An editor delta never carries a ``config_format_version``, and read as stored
    data a ``max_log_events: 5`` in it would go through ``v09_to_v010``, the one
    hop that changes a value rather than a key, and be saved as 10. With the flag,
    an input that has **no** ``config_format_version`` key **and** carries no key
    an earlier hop relocates is taken to be current, so that hop is skipped and the
    result is stamped current. Anything else is migrated exactly as without the
    flag: a stamp of any value (blank, unparseable or older) says the input was
    written for some format, and a legacy key says it was written for an earlier
    one, which is what a configuration downloaded from a release that predates the
    stamp looks like (a full v0.5 download carries an ``assessment`` block). The
    key-moving hops run either way. Readers leave the flag off: a stored
    configuration with no stamp predates the stamp and holds the old default.
    """
    current_input = (
        unstamped_is_current
        and isinstance(config, dict)
        and "config_format_version" not in config
        and not _carries_a_legacy_key(config)
    )
    moved = migrate_v08_to_v09(
        migrate_v07_to_v08(migrate_v06_to_v07(migrate_v05_to_v06(config)))
    )
    if current_input and isinstance(moved, dict):
        # Every earlier hop stamps an unstamped dict, so ``moved`` is a copy and
        # the stamp is the only thing to set: the value hop does not apply.
        moved["config_format_version"] = V010_TARGET_VERSION
        return moved
    return migrate_v09_to_v010(moved)
