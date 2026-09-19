# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Find pipeline hooks a configuration registers at points it can never reach.

Three of the seven pipeline hook points — `postOcr`, `postClassification`,
`postExtraction` — exist only on the Pipeline branch of the unified state
machine, because BDA performs OCR, classification and extraction inside one
Bedrock Data Automation invocation and so has no separate step to hook after. A
configuration that sets `use_bda: true` AND registers a hook at one of those
points describes a hook that is never invoked: the dispatcher is not called at
those points in that mode, so the hook does not run, its `onError: fail` policy
does not run, and the execution history contains no trace of either (#982).

This module finds those registrations in a configuration dict. It is used by the
config-write paths — `validate_config` (the `idp-cli config-validate` /
`config-upload` gate) and `ConfigurationManager.handle_update_custom_configuration`
(the `updateConfiguration` mutation behind the Configuration UI) — so that saving
such a configuration is either refused or reported, instead of silently accepted.

The point-to-mode table itself is NOT written here: it is generated from
`patterns/unified/statemachine/workflow.asl.json` into
:mod:`idp_common.config.hook_point_reachability` by
`scripts/generate_hook_point_reachability.py`. A hand-maintained list of three
names would go stale the moment a hook point moved, which is the same class of
defect as the bug it guards against.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .hook_point_reachability import processing_mode, unreachable_hook_points

# Hook point -> the config section its registrations live under. Mirrors
# `_HOOK_TO_STEP` in patterns/unified/src/pipeline_hooks_function/index.py (the
# dispatcher), which is the component that reads them at runtime.
HOOK_POINT_TO_SECTION = {
    "preprocessing": "preprocessing",
    "postOcr": "ocr",
    "postClassification": "classification",
    "postExtraction": "extraction",
    "postRuleValidation": "rule_validation",
    "postSummarization": "summarization",
    "postprocessing": "postprocessing",
}

# Points whose section IS the hook (no `postHook` list). `postprocessing` is flat
# despite starting with "post", so membership is explicit rather than derived from
# the name.
FLAT_HOOK_POINTS = frozenset({"preprocessing", "postprocessing"})

# The policy that makes an unreachable registration an ERROR rather than a
# warning: the author is declaring that the document must not proceed if the hook
# fails, which a hook that never runs cannot deliver.
GATING_ON_ERROR = "fail"


def _coerce_bool(raw: Any) -> Optional[bool]:
    """A config flag as a bool, or None when it is not a boolean at all.

    Stored config values are stringified on the way into DynamoDB, so `use_bda`
    reaches this module as `"true"`/`"false"` about as often as a real bool.
    Anything else yields None, which makes the caller silent rather than guessing
    a processing mode and naming the wrong hooks.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", ""):
            return False
    return None


def _registered_hooks(config: Dict[str, Any], point: str) -> List[Dict[str, Any]]:
    """The enabled hook entries registered at `point`, in the two stored shapes."""
    section = config.get(HOOK_POINT_TO_SECTION[point])
    if not isinstance(section, dict):
        return []
    if point in FLAT_HOOK_POINTS:
        candidates: List[Any] = [section]
    else:
        raw = section.get("postHook")
        candidates = raw if isinstance(raw, list) else []
    hooks = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        # An arn-less or disabled entry is not a live registration: the dispatcher
        # skips both, so neither can be an inert gate.
        if entry.get("enabled") is False or not entry.get("arn"):
            continue
        hooks.append(entry)
    return hooks


def unreachable_hook_registrations(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Hooks this configuration registers at points its own mode never reaches.

    Each finding is ``{point, featureId, onError, processingMode, gating,
    message}``. `gating` is True for `onError: fail` — the registration the
    caller should refuse rather than merely report, because it is a declared
    security posture that cannot be honoured.

    Returns an empty list when the configuration's mode reaches every point, when
    no hook is registered at an unreachable one, or when `use_bda` is not a
    recognisable boolean.
    """
    use_bda = _coerce_bool(config.get("use_bda"))
    if use_bda is None:
        return []
    unreachable = unreachable_hook_points(use_bda)
    if not unreachable:
        return []
    mode = processing_mode(use_bda)
    findings: List[Dict[str, Any]] = []
    for point in sorted(unreachable):
        if point not in HOOK_POINT_TO_SECTION:
            # A point the state machine invokes but this module has no section
            # for cannot be inspected; say nothing rather than guess.
            continue
        for entry in _registered_hooks(config, point):
            on_error = entry.get("onError") or "continue"
            feature_id = entry.get("featureId") or "unknown"
            gating = on_error == GATING_ON_ERROR
            findings.append(
                {
                    "point": point,
                    "featureId": feature_id,
                    "onError": on_error,
                    "processingMode": mode,
                    "gating": gating,
                    "message": (
                        f"Hook {feature_id} is registered at {point} with "
                        f"onError={on_error}, but the {mode} processing mode "
                        f"(use_bda={use_bda}) has no {point} state, so the hook is "
                        f"never invoked"
                        + (
                            " and its onError=fail policy cannot gate the "
                            "document. Register it at 'preprocessing' (the one "
                            "point both modes reach), set onError to "
                            "'continue'/'skip-remaining' if it is advisory, or "
                            "set use_bda=false."
                            if gating
                            else ". Register it at 'preprocessing' if it needs to "
                            "run in this mode."
                        )
                    ),
                }
            )
    return findings
