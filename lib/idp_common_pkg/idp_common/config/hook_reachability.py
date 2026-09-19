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

# Keys that make a flat section (`preprocessing`/`postprocessing`) a registration
# rather than an unrelated edit. `args` is excluded on purpose: a feature's config
# preset ships args with no ARN, which is not yet a hook.
FLAT_HOOK_REGISTRATION_KEYS = frozenset({"arn", "onError", "enabled", "featureId"})


class InertGatingHookError(ValueError):
    """A write would register an `onError: fail` hook that can never fire.

    A distinct type so the API resolvers can report it as the validation refusal
    it is, rather than as an unexpected server error — an admin reading
    "unexpected error" concludes the product is broken, when in fact there is a
    specific thing for them to fix. Subclasses ValueError so existing callers that
    catch that keep working.
    """


def delta_touches_hook_registration(delta: Dict[str, Any], point: str) -> bool:
    """True when an incoming config change actually writes `point`'s hook or mode.

    `handle_update_custom_configuration` merges a DELTA onto the stored config, so
    a finding on the merged result says nothing about what this write did: a
    pre-existing inert hook would otherwise fail every later save of an unrelated
    field. Worse, three of that function's five callers are BDA blueprint↔class
    synchronisation (`idp_common.bda.bda_blueprint_service`), which sends
    ``{"classes": [...]}``, runs only in BDA mode — precisely the population that
    can hold an inert gating hook — and swallows exceptions, so a refusal there
    would silently skip the class sync and leave a log line as the only evidence.
    That is the failure shape this check exists to remove, one layer up.

    So the refusal is scoped to writes that are ABOUT the hook: the delta carries
    `use_bda` (the mode itself is changing, which can make an existing hook inert),
    or it carries the hook registration for that point — `postHook` for a post-step
    point, or one of :data:`FLAT_HOOK_REGISTRATION_KEYS` for a flat one. Editing
    another field of the same section (say `ocr.image.dpi`) does not count.
    """
    if not isinstance(delta, dict):
        return False
    if "use_bda" in delta:
        return True
    section = delta.get(HOOK_POINT_TO_SECTION.get(point, ""))
    if not isinstance(section, dict):
        return False
    if point in FLAT_HOOK_POINTS:
        return bool(FLAT_HOOK_REGISTRATION_KEYS & set(section))
    return "postHook" in section


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


def reject_inert_gating_hooks(
    config: Dict[str, Any],
    delta: Optional[Dict[str, Any]] = None,
    *,
    log: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Raise on a gating registration that can never fire; return the advisory ones.

    The single implementation of the write-boundary rule, shared by every path that
    stores a configuration: `ConfigurationManager.handle_update_custom_configuration`
    (the `updateConfiguration` mutation), and the feature platform's
    `applyFeatureConfigPreset`, which writes a feature's bundled preset as its own
    config version and is the path both bundled extensions actually use.

    `config` is the fully merged configuration about to be stored. `delta` is what
    the caller asked to change; findings outside it are dropped (see
    :func:`delta_touches_hook_registration`). Passing None checks everything.

    Raises :class:`InertGatingHookError` when a surviving finding is gating.
    Advisory findings are logged through `log` if given and returned.
    """
    findings = unreachable_hook_registrations(config)
    if delta is not None:
        findings = [
            f for f in findings if delta_touches_hook_registration(delta, f["point"])
        ]
    advisory = [f for f in findings if not f["gating"]]
    gating = [f for f in findings if f["gating"]]
    if log is not None:
        for finding in advisory:
            log.warning(finding["message"])
    if gating:
        raise InertGatingHookError(
            "Configuration rejected: " + "; ".join(f["message"] for f in gating)
        )
    return advisory


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
                            "document. Disable the hook (enabled: false) if it is "
                            "not wanted in this mode, register it at "
                            "'preprocessing' (the one point both modes reach), set "
                            "onError to 'continue'/'skip-remaining' if it is "
                            "advisory, or set use_bda=false."
                            if gating
                            else ". Register it at 'preprocessing' if it needs to "
                            "run in this mode, or disable it (enabled: false)."
                        )
                    ),
                }
            )
    return findings
