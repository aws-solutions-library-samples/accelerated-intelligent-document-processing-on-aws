# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.6 -> v0.7: relocate the validation block.

``extraction.agentic.validation`` becomes ``extraction.validation``.

Why it moves: the block configures full JSON-Schema validation of an extraction
result and the optional model escalation that follows a failure. That was
agentic-only when it was written, but simple (traditional) extraction now runs
the same validate-and-retry path, so a knob nested under ``agentic`` would be
read by a non-agentic code path — misleading in exactly the way that makes
configuration hard to reason about.

Following the v0.5 -> v0.6 precedent, the old location is REMOVED rather than
kept as a live alias. Two readable homes for one setting is a bug factory: a
config carrying both would have a silent precedence rule, and every future reader
would have to know about it.

Deliberately narrow: this migration touches nothing but that one block. It does
not reshape, rename, or default anything else, so it is safe to apply on every
read of both full configs and sparse override deltas.
"""

import copy
import logging
from typing import Any, Dict

from ._version import is_at_or_after, is_newer_than

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.7"


def _has_legacy_markers(config: Dict[str, Any]) -> bool:
    """True if the config still carries ``extraction.agentic.validation``.

    This is the load-bearing trigger, not the version stamp. The deep-merge path
    can produce a dict stamped with the CURRENT version (inherited from the full
    default) that still carries a legacy-shaped delta from a sparse custom
    override. Relying on the stamp alone would skip such a hybrid and silently
    drop the user's setting — the same P0 class of bug that
    ``test_merge_migration_order.py`` was written to pin for v0.5 -> v0.6.
    """
    extraction = config.get("extraction")
    if not isinstance(extraction, dict):
        return False
    agentic = extraction.get("agentic")
    return isinstance(agentic, dict) and "validation" in agentic


def _needs_migration(config: Dict[str, Any]) -> bool:
    """True when the config predates v0.7 or still carries the legacy location."""
    if not is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
        return True
    return _has_legacy_markers(config)


def migrate_v06_to_v07(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a v0.7-shaped copy of ``config``.

    Operates only on keys that are present, so it works on both full configs and
    sparse override deltas without injecting unrelated defaults. Idempotent.
    """
    if not isinstance(config, dict):
        return config

    if not _needs_migration(config):
        # Preserve object identity for the pure no-op case, matching v05_to_v06.
        return config

    result = copy.deepcopy(config)

    extraction = result.get("extraction")
    if isinstance(extraction, dict):
        agentic = extraction.get("agentic")
        if isinstance(agentic, dict) and "validation" in agentic:
            legacy = agentic.pop("validation")
            if isinstance(legacy, dict):
                existing = extraction.get("validation")
                existing = existing if isinstance(existing, dict) else {}
                # An explicit key at the NEW location always wins over the
                # migrated legacy value, so re-running the migration over a
                # hybrid config cannot clobber a deliberate setting.
                extraction["validation"] = {**legacy, **existing}
            elif legacy is not None:
                logger.warning(
                    "extraction.agentic.validation was %s, not a mapping; "
                    "dropping it rather than relocating a value that cannot be "
                    "a validation config",
                    type(legacy).__name__,
                )
            logger.info(
                "Migrated config v0.6 -> v0.7 "
                "(extraction.agentic.validation -> extraction.validation)"
            )
            # Drop an agentic block left empty by the move so the config does not
            # accumulate empty scaffolding across migrations.
            if not agentic:
                extraction.pop("agentic", None)
            result["extraction"] = extraction

    # Never DOWNGRADE the stamp. Reaching here with a newer stamp means an older
    # release is reading a configuration written by a newer one (a rollback), and
    # only the legacy-marker check brought us in. Rewriting "0.8" to "0.7" would
    # erase the one record that this config came from a newer format, so a later
    # roll-forward would skip the migration it actually needs.
    if not is_newer_than(result.get("config_format_version"), TARGET_VERSION):
        result["config_format_version"] = TARGET_VERSION
    return result
