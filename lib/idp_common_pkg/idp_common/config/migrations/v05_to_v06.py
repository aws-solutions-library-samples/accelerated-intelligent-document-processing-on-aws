# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""v0.5 → v0.6 config-shape migration.

v0.6 reframes per-field confidence and geometry as OUTPUTS OF EXTRACTION rather
than a separate stage:

    extraction.assessment_integration       -> extraction.confidence.mode
    assessment.{model,prompts,image,...}     -> extraction.confidence.*
    assessment.inshard_list_batch_size       -> extraction.confidence.list_batch_size
    assessment.granular                      -> DROPPED (granular assessment retired)
    assessment.geometry_mode / legacy flag   -> extraction.geometry.mode  (renamed values)
    assessment.hitl_enabled                  -> hitl.enabled
    assessment.default_confidence_threshold  -> hitl.confidence_threshold

The top-level ``assessment`` block is slimmed to the standalone-step carrier
(``enabled`` + ``postHook``) so the ``postAssessment`` pipeline-hook point and the
step's own enable/bypass keep working until the step is retired.

The transform is a pure ``dict -> dict`` and is IDEMPOTENT: it short-circuits once
a config is stamped ``config_format_version == "0.6"``, so it is safe to apply on
every read (both on the raw custom delta before merge and again after merge inside
``IDPConfig`` validation).
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict

from ._version import is_at_or_after, is_newer_than

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.6"

# Geometry mode value renames (v0.5 -> v0.6).
_GEOMETRY_MODE_RENAME = {
    "ocr_only": "ocr_only",
    "llm_with_ocr_grounding": "llm_grounded",
    "llm_only": "llm",
    # Already-v0.6 values pass through untouched.
    "llm_grounded": "llm_grounded",
    "llm": "llm",
    "off": "off",
}

# assessment.* inference keys that move verbatim into extraction.confidence.*
# (NOTE: the confidence TASK prompt does NOT live here — v0.6 moves it to the
# top-level extraction.task_prompt_confidence; see the mapping below.)
_CONFIDENCE_PASSTHROUGH_KEYS = (
    "model",
    "model_lambda_hook_arn",
    "system_prompt",
    "temperature",
    "top_p",
    "top_k",
    "reasoning_effort",
    # max_tokens intentionally dropped in v0.6 — output is always requested at the
    # model maximum (config_library/model_config_limits.yaml), so a stale
    # assessment.max_tokens is not carried into extraction.confidence.
    "image",
)


def _has_legacy_markers(config: Dict[str, Any]) -> bool:
    """True if the config still carries any v0.5-shaped assessment keys.

    This is the robust trigger: the deep-merge path can produce a dict stamped
    ``config_format_version == "0.6"`` (inherited from the full default) that
    still carries a v0.5-shaped ``assessment`` delta from a sparse custom
    override. Relying on the stamp alone would skip such a hybrid; the presence
    of any legacy key forces the migration to run. In v0.6 the entire
    ``assessment`` block is retired, so ANY ``assessment`` key is a legacy marker.
    """
    extraction = config.get("extraction")
    if isinstance(extraction, dict) and "assessment_integration" in extraction:
        return True
    if isinstance(config.get("assessment"), dict):
        return True
    return False


def _needs_migration(config: Dict[str, Any]) -> bool:
    """True when the config is pre-v0.6 or still carries legacy assessment keys.

    A stamp NEWER than v0.6 does not need this transform: the config has already
    been through it (and through later hops), so reshaping it again on an older
    release would be an older format's opinion applied to a newer config.
    """
    if not is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
        return True
    return _has_legacy_markers(config)


def _pin_extraction_mode_from_agentic(config: Dict[str, Any]) -> bool:
    """Pin ``extraction.mode`` from ``extraction.agentic.enabled`` when mode is absent.

    In v0.6 ``extraction.mode`` is authoritative and ``extraction.agentic.enabled``
    is DERIVED from it (see ``ExtractionConfig.reconcile_mode_and_agentic``). A config
    that sets ``agentic.enabled`` but omits ``mode`` is a footgun: after the delta is
    deep-merged onto the v0.6 defaults (which carry ``mode: simple``), the merged config
    has ``mode='simple'`` + ``agentic.enabled=true`` and ``reconcile`` then SILENTLY
    flips ``agentic.enabled`` back to false — the author asked for advanced/agentic
    extraction and silently got simple single-pass.

    Because migration is applied to the DELTA before the merge, pinning ``mode`` here
    makes the author's intent WIN the merge instead of being overridden by the default's
    ``mode``. Returns True if it changed the config.

    Idempotent and safe:
    - No-op when ``agentic.enabled`` is absent (a sparse delta that never mentions it).
    - No-op when ``extraction.mode`` is already set — an explicit ``mode`` is
      authoritative and is NEVER overridden (a second migration pass finds mode set
      and does nothing).
    """
    extraction = config.get("extraction")
    if not isinstance(extraction, dict):
        return False
    agentic = extraction.get("agentic")
    if not isinstance(agentic, dict) or "enabled" not in agentic:
        return False
    existing_mode = extraction.get("mode")
    if existing_mode is not None and str(existing_mode).strip() != "":
        # Explicit mode wins; never override the author's stated mode.
        return False
    extraction["mode"] = (
        "advanced" if _coerce_bool(agentic.get("enabled")) else "simple"
    )
    return True


def _coerce_bool(value: Any) -> bool:
    """Interpret an ``enabled`` value the same way Pydantic v2 coerces a bool.

    The migration runs BEFORE Pydantic validation, so this must agree with how
    ``AgenticConfig.enabled`` (a ``bool`` field) will ultimately parse the value —
    otherwise a truthy input like ``1`` or ``"yes"`` would be pinned to
    ``mode: simple`` and then get flipped off, resurrecting the footgun. Unknown /
    invalid values fall back to ``bool(value)``; Pydantic surfaces the real error
    at validation time.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "t", "y")
    return bool(value)


def migrate_v05_to_v06(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a v0.6-shaped copy of ``config``.

    Operates only on keys that are present, so it works on both full configs and
    sparse override deltas without injecting unrelated defaults. Idempotent.
    """
    if not isinstance(config, dict):
        return config

    needs = _needs_migration(config)
    result = copy.deepcopy(config)

    # Footgun guard (applies to BOTH v0.5 and already-v0.6-stamped deltas): if the
    # delta sets extraction.agentic.enabled but omits extraction.mode, pin mode from
    # agentic.enabled so the author's intent survives the deep-merge onto the v0.6
    # defaults (which carry mode: simple) instead of being silently reverted by
    # reconcile_mode_and_agentic. See _pin_extraction_mode_from_agentic.
    mode_pinned = _pin_extraction_mode_from_agentic(result)

    if not needs:
        # Already v0.6 with no legacy assessment markers: nothing to reshape. Return
        # the pinned copy only if the footgun guard actually changed something,
        # otherwise the original (preserves object identity for the pure no-op case).
        return result if mode_pinned else config

    extraction = result.get("extraction")
    extraction = extraction if isinstance(extraction, dict) else {}
    assessment = result.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}

    confidence: Dict[str, Any] = {}
    geometry: Dict[str, Any] = {}
    hitl: Dict[str, Any] = {}

    # --- integration mode: extraction.assessment_integration -> confidence.mode ---
    if "assessment_integration" in extraction:
        confidence["mode"] = extraction.pop("assessment_integration")

    # --- confidence enablement + inference knobs (from assessment.*) ---
    # v0.6 folds enable+integration into a single confidence.mode
    # (off|separate|integrated). A legacy assessment.enabled=false (bool or the
    # string "false"/"0"/"no"/"off") becomes 'off' and overrides the integration
    # mode; otherwise assessment_integration (above) supplies the mode.
    _enabled = assessment.get("enabled")
    _enabled_is_false = _enabled is False or (
        isinstance(_enabled, str)
        and _enabled.strip().lower() in ("false", "0", "no", "off")
    )
    if _enabled_is_false:
        confidence["mode"] = "off"
    if "inshard_list_batch_size" in assessment:
        confidence["list_batch_size"] = assessment["inshard_list_batch_size"]
    for key in _CONFIDENCE_PASSTHROUGH_KEYS:
        if key in assessment:
            confidence[key] = assessment[key]

    # --- geometry: geometry_mode (renamed) with legacy ground_geometry_in_ocr ---
    if "geometry_mode" in assessment:
        old = str(assessment["geometry_mode"]).strip().lower()
        geometry["mode"] = _GEOMETRY_MODE_RENAME.get(old, old)
    elif assessment.get("ground_geometry_in_ocr") is False:
        # Legacy explicit opt-out of OCR grounding == "use LLM boxes as-is".
        geometry["mode"] = "llm"

    # --- confidence TASK prompt -> extraction.confidence.task_prompt ---
    # (v0.6: the confidence prompt lives in the confidence sub-config.) Only set it
    # if the target isn't already present (explicit v0.6 wins).
    if assessment.get("task_prompt") and "task_prompt" not in confidence:
        confidence["task_prompt"] = assessment["task_prompt"]

    # --- HITL: hitl_enabled / default_confidence_threshold -> top-level hitl ---
    if "hitl_enabled" in assessment:
        hitl["enabled"] = assessment["hitl_enabled"]
    if "default_confidence_threshold" in assessment:
        hitl["confidence_threshold"] = assessment["default_confidence_threshold"]

    # --- write the new homes (merge into any pre-existing v0.6 keys) ---
    if confidence:
        existing = extraction.get("confidence")
        existing = existing if isinstance(existing, dict) else {}
        # Pre-existing explicit v0.6 keys win over migrated legacy values.
        merged = {**confidence, **existing}
        extraction["confidence"] = merged
    if geometry:
        existing = extraction.get("geometry")
        existing = existing if isinstance(existing, dict) else {}
        extraction["geometry"] = {**geometry, **existing}
    if extraction:
        result["extraction"] = extraction
    if hitl:
        existing = result.get("hitl")
        existing = existing if isinstance(existing, dict) else {}
        result["hitl"] = {**hitl, **existing}

    # --- drop the assessment block entirely (v0.6) ---
    # Confidence inference knobs, geometry, HITL, and enablement all moved to
    # extraction.confidence / extraction.geometry / hitl above. The standalone
    # `assessment` block (including its postAssessment pipeline-hook point) is
    # retired; IDPConfig ignores any leftover on read.
    result.pop("assessment", None)

    if any([confidence, geometry, hitl]):
        logger.info(
            "Migrated config v0.5 -> v0.6 "
            "(assessment.* -> extraction.confidence/geometry + hitl)"
        )

    # Never DOWNGRADE the stamp — see the same guard in v06_to_v07. Reaching here
    # with a newer stamp means only the legacy-marker check brought us in, and
    # rewriting the stamp backwards would hide that the config came from a newer
    # format.
    if not is_newer_than(result.get("config_format_version"), TARGET_VERSION):
        result["config_format_version"] = TARGET_VERSION
    return result
