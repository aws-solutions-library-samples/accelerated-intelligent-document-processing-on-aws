# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.10 -> v0.11: one schema-enforcement setting.

``extraction.forced_tool`` and ``extraction.structured_output`` become
``extraction.schema_enforcement``::

    extraction:
      schema_enforcement:
        mode: off            # off | auto | structured_output | forced_tool
        fallback_to_prompt: true

Why they merge: both answer one question — make Simple extraction's reply match
the class schema — with two mechanisms. A forced tool steers the shape on a
best-effort basis and works on most Converse models; Bedrock structured outputs
guarantees it by constrained decoding and works on Claude only. As two toggles the
editor offered two switches for one decision and a precedence rule between them.

The mapping keeps every stored configuration doing exactly what it did:

* ``forced_tool.enabled: true`` -> ``mode: forced_tool``.
* ``structured_output.enabled: true`` -> ``mode: structured_output``.
* both on -> ``mode: auto``. That is the behaviour the pair already had:
  structured outputs where it applies, the forced tool where it does not.
* ``forced_tool.fallback_to_prompt`` -> ``schema_enforcement.fallback_to_prompt``.

``structured_output`` never shipped in a release; it is handled so a stack that
ran the pre-release build (and a profile saved there) reads cleanly.

**Precedence over a value already at the new location.** As in v0.6 -> v0.7, an
explicit value at the new location wins — with one exception: ``mode: off``. In a
full saved snapshot ``off`` is the untouched default and cannot be told apart from
a choice, so a legacy ``enabled: true`` beside it is read as the intent. That is
the case a merge produces when a legacy override is laid over a current default,
and reading ``off`` as deliberate would silently turn the operator's enforcement
off. Any other mode at the new location is deliberate and is kept.

The legacy keys are removed rather than kept as aliases, following v0.5 -> v0.6.
Only these keys are touched, so this is safe on full configurations and sparse
deltas alike.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

from ._version import is_at_or_after, is_newer_than

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.11"

#: The keys this hop removes from ``extraction``.
LEGACY_KEYS = ("forced_tool", "structured_output")

_TRUE = {"true", "yes", "on", "1", "t", "y"}
_FALSE = {"false", "no", "off", "0", "f", "n"}


def _flag(value: Any) -> Optional[bool]:
    """A stored boolean, or None when absent or unreadable.

    The Configuration table stores scalars as strings, so ``"true"`` is as common
    as ``True``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    return None


def _has_legacy_markers(config: Dict[str, Any]) -> bool:
    """True if ``extraction`` still carries either legacy key.

    The trigger rather than the stamp, for the reason v0.6 -> v0.7 gives: a merge
    can produce a dict stamped current that still carries a legacy-shaped delta.
    """
    extraction = config.get("extraction") if isinstance(config, dict) else None
    return isinstance(extraction, dict) and any(k in extraction for k in LEGACY_KEYS)


def _needs_migration(config: Dict[str, Any]) -> bool:
    if not is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
        return True
    return _has_legacy_markers(config)


def _legacy_mode(forced: Optional[bool], structured: Optional[bool]) -> Optional[str]:
    """The mode the two legacy flags express, or None when neither says anything."""
    if forced and structured:
        return "auto"
    if structured:
        return "structured_output"
    if forced:
        return "forced_tool"
    if forced is False or structured is False:
        return "off"
    return None


def migrate_v010_to_v011(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a v0.11-shaped copy of ``config``. Idempotent."""
    if not isinstance(config, dict):
        return config
    if not _needs_migration(config):
        return config

    result = copy.deepcopy(config)
    extraction = result.get("extraction")
    if isinstance(extraction, dict) and _has_legacy_markers(result):
        forced_block = extraction.pop("forced_tool", None)
        structured_block = extraction.pop("structured_output", None)
        forced_block = forced_block if isinstance(forced_block, dict) else {}
        structured_block = (
            structured_block if isinstance(structured_block, dict) else {}
        )

        mode = _legacy_mode(
            _flag(forced_block.get("enabled")), _flag(structured_block.get("enabled"))
        )
        fallback = forced_block.get("fallback_to_prompt")

        existing = extraction.get("schema_enforcement")
        existing = dict(existing) if isinstance(existing, dict) else {}

        # A value at the new location wins unless it is the default, which a full
        # snapshot carries whether or not anyone chose it (see the module
        # docstring). The legacy value then wins only when it is NOT the default.
        current_mode = existing.get("mode")
        if mode is not None:
            if current_mode is None:
                existing["mode"] = mode
            elif str(current_mode).strip().lower() == "off" and mode != "off":
                existing["mode"] = mode
        legacy_fallback = _flag(fallback)
        if legacy_fallback is not None:
            current_fallback = _flag(existing.get("fallback_to_prompt"))
            if current_fallback is None or (current_fallback and not legacy_fallback):
                existing["fallback_to_prompt"] = fallback
        if existing:
            extraction["schema_enforcement"] = existing
        logger.info(
            "Migrated config v0.10 -> v0.11 (extraction.forced_tool / "
            "extraction.structured_output -> extraction.schema_enforcement, mode=%s)",
            existing.get("mode"),
        )
        result["extraction"] = extraction

    if not is_newer_than(result.get("config_format_version"), TARGET_VERSION):
        result["config_format_version"] = TARGET_VERSION
    return result
