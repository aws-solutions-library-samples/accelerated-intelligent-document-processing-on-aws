# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync resolver for registerFeatureHooks / unregisterFeatureHooks.

Hooks are stored INLINE in the active config version. There are two shapes,
matching what the pipeline-hooks dispatcher reads:

1. POST-STEP points — a LIST under each processing step's `postHook`:

    Config#<active-version>
      ocr:
        postHook: [ {featureId, arn, order, onError, enabled}, … ]
      classification:    { …, postHook: [ … ] }
      extraction:        { …, postHook: [ … ] }
      rule_validation:   { …, postHook: [ … ] }
      summarization:     { …, postHook: [ … ] }

2. FLAT points (`preprocessing`, `postprocessing`) — a STANDALONE top-level
   section that IS the single hook (no list):

      preprocessing:  { enabled, featureId, arn, onError, args }
      postprocessing: { enabled, featureId, arn, onError, args }

So this resolver:
  1. Resolves the active config version (IsActive=true), or `default`
     when none is set.
  2. For a post-step point, removes any existing entry in that step's
     `postHook` list with the same featureId, then appends the new entry.
     For a flat point, fills in the section's `arn`/`featureId`/`onError` and
     enables it, PRESERVING any `args` already there (a feature's config preset
     typically ships the args and leaves the ARN blank until its stack exists).
  3. Writes the mutated sections back with a targeted `update_item`, so the head
     attributes it does not own (the revision counters, the `Bda*` fields) are
     left alone rather than replaced along with the row — see
     :func:`_write_config_body`.

Hooks contributed by other features are preserved untouched: a post-step list
keeps other features' entries, and a flat section owned by a DIFFERENT featureId
is left alone rather than hijacked (only one hook can own a flat point).

A registration is also checked against the hook points the target
configuration's processing mode actually reaches — see :func:`_check_reachability`
and hook_point_reachability.py, which is generated from the state machine
definition. `postOcr`, `postClassification` and `postExtraction` do not exist in
BDA mode, and a hook registered there with `onError: fail` is refused rather than
accepted as a gate that cannot gate (#982).
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from hook_point_reachability import unreachable_hook_points
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "WARN"))

_CONFIG_TABLE = os.environ["CONFIGURATION_TABLE"]

_HOOK_POINT_TO_STEP = {
    "preprocessing": "preprocessing",
    "postOcr": "ocr",
    "postClassification": "classification",
    "postExtraction": "extraction",
    # postAssessment removed in v0.6 (confidence folded into extraction).
    "postRuleValidation": "rule_validation",
    "postSummarization": "summarization",
    "postprocessing": "postprocessing",
}

# Points whose config section IS the hook (single flat hook, no `postHook`
# list). Must stay in sync with _FLAT_HOOK_POINTS in the dispatcher
# (patterns/unified/src/pipeline_hooks_function/index.py) — note that
# `postprocessing` is flat despite starting with "post", so membership is
# explicit rather than derived from the point name.
_FLAT_HOOK_POINTS = frozenset({"preprocessing", "postprocessing"})

_VALID_POINTS = set(_HOOK_POINT_TO_STEP)
_VALID_ON_ERROR = {"continue", "fail", "skip-remaining"}

# Row-level metadata on the profile head item: attributes that belong to the
# configuration manager's bookkeeping rather than to the configuration body. They
# are stripped when the body is read out of an inline row, so anything missing
# here is read back as though it were a config section.
#
# Must cover _PRESERVED_HEAD_FIELDS in
# lib/idp_common_pkg/idp_common/config/configuration_manager.py — the head
# attributes maintained by targeted update_item calls elsewhere (the revision
# counters by ConfigRevisionStore, the three Bda* fields by
# ConfigurationManager), which this module only ever passes through.
# scripts/tests/test_config_head_writers.py asserts the coverage, because the two
# revision counters were absent here and nothing objected.
_CONFIG_METADATA_FIELDS = {
    "Configuration",
    "CreatedAt",
    "UpdatedAt",
    "IsActive",
    "Description",
    "Managed",
    "BdaProjectArn",
    "BdaSyncStatus",
    "BdaLastSyncedAt",
    "LatestRevision",
    "PublishedRevision",
}

# Storage markers for the compressed row format. This resolver writes the body
# INLINE, so a compressed source row has both removed as it is converted.
_COMPRESSED_STORAGE_MARKER = "_config_storage"
_COMPRESSED_DATA_FIELD = "_compressed_config"
_CONFIG_FORMAT_MARKER = "_config_format"

# Attributes _write_config_body assigns or removes itself, so the body it is handed
# must never also assign them: a single UpdateExpression may not touch one attribute
# path twice, and DynamoDB rejects the whole call if it does.
#
# DERIVED from the two sets above rather than spelled out again. A second literal
# list of the same attribute names is the shape that produced this defect in the
# first place, and it would drift from _CONFIG_METADATA_FIELDS the moment either
# changed. It is also the stronger rule: no row-metadata attribute may be written
# from the configuration body, so a head field that leaks into the body on a legacy
# inline row cannot be written back over the live one.
_WRITER_OWNED_ATTRIBUTES = frozenset(_CONFIG_METADATA_FIELDS) | {
    _CONFIG_FORMAT_MARKER,
    _COMPRESSED_STORAGE_MARKER,
    _COMPRESSED_DATA_FIELD,
}

_dynamodb = boto3.resource("dynamodb")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decompress(item: Dict[str, Any]) -> Dict[str, Any]:
    storage = item.get("_config_storage")
    compressed = item.get("_compressed_config")
    if storage == "compressed" and compressed is not None:
        try:
            raw = compressed.value if hasattr(compressed, "value") else compressed
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            text = gzip.decompress(raw).decode("utf-8")
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("Decompress failed: %s", exc)
            return {}
    return {
        k: v
        for k, v in item.items()
        if k not in _CONFIG_METADATA_FIELDS and not k.startswith("_")
    }


def _resolve_active_version(table: Any) -> str:
    """The version segment of the IsActive=true Config# row, or 'default'.

    The scan MUST paginate. DynamoDB applies `Limit` (and the implicit 1MB page
    size) to the items *examined*, not the items matching `FilterExpression`, so
    the previous `Limit=1` returned a match only when the active row happened to
    be the very first item examined — i.e. almost never on a table with more
    than a handful of versions. Resolving to `default` here writes the
    feature's hooks into a row that is not the active one, so the hooks are
    registered successfully and then never fire (issue #599).
    """
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(Configuration, :p) AND IsActive = :t",
        "ExpressionAttributeValues": {":p": "Config#", ":t": True},
        "ProjectionExpression": "Configuration",
    }
    try:
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items") or []:
                key = item["Configuration"]
                if "#" in key:
                    return key.split("#", 1)[1]
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
    except Exception as exc:  # noqa: BLE001
        logger.warning("Active-version scan failed (defaulting to 'default'): %s", exc)
        return "default"
    logger.warning(
        "No active Config# version found after a full scan; registering hooks "
        "into Config#default. They will not run unless that version is active."
    )
    return "default"


def _validate_hook(h: Dict[str, Any]) -> Dict[str, Any]:
    point = h.get("point")
    arn = h.get("arn")
    if point not in _VALID_POINTS:
        raise ValueError(
            f"Invalid hook point {point!r}; must be one of {sorted(_VALID_POINTS)}"
        )
    if not isinstance(arn, str) or not arn.startswith("arn:") or ":lambda:" not in arn:
        raise ValueError(f"Invalid hook arn {arn!r}; expected a Lambda ARN")
    order = h.get("order")
    if order is None:
        order = 100
    if not isinstance(order, int):
        raise ValueError(f"Hook order must be an integer; got {order!r}")
    on_error = h.get("onError") or "continue"
    if on_error not in _VALID_ON_ERROR:
        raise ValueError(
            f"Invalid onError {on_error!r}; must be one of {sorted(_VALID_ON_ERROR)}"
        )
    enabled = h.get("enabled")
    if enabled is None:
        enabled = True
    if not isinstance(enabled, bool):
        raise ValueError(f"Hook enabled must be a bool; got {enabled!r}")
    return {
        "point": point,
        "arn": arn,
        "order": int(order),
        "onError": on_error,
        "enabled": enabled,
    }


def _coerce_bool(raw: Any) -> Optional[bool]:
    """A stored config flag as a bool, or None when it is not a boolean at all.

    Config rows are written with their values STRINGIFIED, so `use_bda` arrives
    as `"true"`/`"false"` as often as a real bool. None for anything else, which
    makes the check below silent rather than guessing a processing mode — a wrong
    guess would refuse a registration that is perfectly valid.
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


def _check_reachability(
    payload: Dict[str, Any], feature_id: str, hooks: List[Dict[str, Any]]
) -> List[str]:
    """Refuse a gating hook that cannot fire; warn about an advisory one.

    Three of the seven hook points — `postOcr`, `postClassification`,
    `postExtraction` — exist only on the Pipeline branch of the state machine,
    because BDA performs OCR, classification and extraction inside one Bedrock
    Data Automation invocation. Registering a hook at one of them while the
    target configuration sets `use_bda: true` produces a hook that is never
    invoked at all, and until now that was accepted in silence: the config record
    showed the hook, the UI showed the hook, and the execution history showed
    nothing, because the dispatcher was never called (#982).

    The policy the hook declares decides what happens here:

    * `onError: fail` is a SECURITY POSTURE, not a preference — the caller is
      saying the document must be aborted if the hook fails. A registration that
      can never fire cannot honour that, so it is REFUSED. Failing the feature
      stack's install is loud, immediate and fixable in one field; an inert
      compliance gate is invisible and its harm is unbounded. The refusal names
      the three remedies.
    * anything else is advisory, so it is accepted with a WARNING returned to the
      caller and logged. Refusing would block a legitimate "install now, switch
      to pipeline mode later" sequence for a hook that gates nothing.

    Registration-time checking cannot be the whole answer in either case, because
    `use_bda` can change AFTER a hook is registered. The dispatcher re-checks on
    every execution and records what it finds in `$.HookResults.preprocessing`.
    """
    use_bda = _coerce_bool(payload.get("use_bda"))
    if use_bda is None:
        return []
    unreachable = unreachable_hook_points(use_bda)
    if not unreachable:
        return []
    mode = "bda" if use_bda else "pipeline"
    warnings: List[str] = []
    for h in hooks:
        point = h["point"]
        if point not in unreachable:
            continue
        if not h["enabled"]:
            # The dispatcher skips a disabled entry in every mode, so it is not a
            # gate anywhere and there is nothing to refuse. This is also the least
            # drastic of the remedies the refusal below offers, so it has to work.
            continue
        detail = (
            f"hook point {point!r} does not exist in the {mode} processing mode "
            f"of the active configuration (use_bda={use_bda}), so a hook "
            f"registered there is never invoked"
        )
        if h["onError"] == "fail":
            raise ValueError(
                f"Refusing to register {feature_id!r} at {point!r} with "
                f"onError='fail': {detail}, and its fail policy therefore cannot "
                f"gate anything. Ordered from least to most drastic: register the "
                f"hook with enabled=false if it is not wanted in this mode, move it "
                f"to 'preprocessing' (the one point both processing modes reach), "
                f"set onError to 'continue'/'skip-remaining' if it is advisory, or "
                f"activate a configuration with use_bda=false."
            )
        message = (
            f"Hook {feature_id} registered at {point} will NOT run: {detail}. "
            f"onError={h['onError']} (advisory), so the registration is accepted."
        )
        logger.warning(message)
        warnings.append(message)
    return warnings


def _replace_pack_entries(
    payload: Dict[str, Any],
    feature_id: str,
    new_by_step: Dict[str, List[Dict[str, Any]]],
) -> int:
    """Mutate `payload` so this featureId's hooks match `new_by_step`.

    Post-step points: the step's `postHook` list has THIS featureId's entries
    replaced with the new ones; entries from other features survive.

    Flat points (`preprocessing`/`postprocessing`): the section itself is the
    hook, so there is at most one owner. We fill in / clear this feature's
    ownership and leave a section owned by another feature untouched — its
    `args` (which a feature's config preset typically ships) are preserved
    either way.

    Returns the total number of hooks this featureId now contributes.
    """
    total = 0
    for point, step in _HOOK_POINT_TO_STEP.items():
        new_entries = new_by_step.get(step, [])
        block = payload.get(step)
        if not isinstance(block, dict):
            block = {} if block is None else {"_legacy_value": block}
            payload[step] = block

        if point in _FLAT_HOOK_POINTS:
            total += _apply_flat_hook(block, feature_id, new_entries, point)
            continue

        existing = block.get("postHook") or []
        if not isinstance(existing, list):
            existing = []
        kept = [
            e
            for e in existing
            if not (isinstance(e, dict) and e.get("featureId") == feature_id)
        ]
        block["postHook"] = kept + new_entries
        total += len(new_entries)
    return total


def _apply_flat_hook(
    block: Dict[str, Any],
    feature_id: str,
    new_entries: List[Dict[str, Any]],
    point: str,
) -> int:
    """Set or clear THIS feature's ownership of a flat single-hook section.

    Registering: fills in `arn`/`featureId`/`onError` and enables the section,
    preserving whatever `args` are already there. Refuses to overwrite a section
    another feature owns (a flat point has exactly one hook; silently hijacking
    it would disable that feature).

    Unregistering (`new_entries` empty): clears the ARN and disables the section
    only if THIS feature owns it. `args` are left in place so re-installing
    restores the previous behavior.

    Clearing on unregister is load-bearing, not just tidiness. A flat point's
    hook is invoked by ARN, so an uninstalled feature's ARN left behind names a
    Lambda that no longer exists — and the PII Anonymizer's shipped preset sets
    `onError: fail`, which makes the dispatcher raise and the workflow land in
    its terminal `PreprocessingHookFailed` state. That fails EVERY subsequent
    document until an admin hand-edits the config. Disabling the section is the
    fail-safe outcome; `args` survive so a re-install is a one-field change.
    """
    owner = block.get("featureId") or ""
    if not new_entries:
        if owner == feature_id:
            block["enabled"] = False
            block["arn"] = None
            logger.info("Cleared flat hook at %s (owner %s)", point, feature_id)
        return 0

    if owner and owner != feature_id:
        raise ValueError(
            f"Hook point {point!r} already holds a hook owned by feature "
            f"{owner!r}; it accepts only one hook. Remove that feature's hook "
            f"before registering {feature_id!r} here."
        )
    # Only ever ONE hook per flat point — if a manifest somehow declared several,
    # the last would silently win, so reject it rather than lose one.
    if len(new_entries) > 1:
        raise ValueError(
            f"Hook point {point!r} accepts a single hook; got {len(new_entries)}"
        )
    entry = new_entries[0]
    block["featureId"] = feature_id
    block["arn"] = entry["arn"]
    block["onError"] = entry["onError"]
    block["enabled"] = entry["enabled"]
    block.setdefault("args", [])
    logger.info("Set flat hook at %s to %s (owner %s)", point, entry["arn"], feature_id)
    return 1


def _write_config_body(
    table: Any, config_key: str, payload: Dict[str, Any], timestamp: str
) -> None:
    """Persist the mutated config body onto the head row without replacing it.

    Deliberately ``update_item`` and not ``put_item``. ``put_item`` replaces the
    WHOLE item, so every attribute absent from the dict handed to it is deleted —
    and the profile head carries attributes this resolver never reads: the
    revision counters ``LatestRevision``/``PublishedRevision``, maintained by
    ``ConfigRevisionStore``, the three ``Bda*`` fields maintained by
    ``ConfigurationManager``, and ``_feature_id`` stamped by
    applyFeatureConfigPreset. Re-attaching them from a hand-maintained list is
    what failed: the list named seven fields, none of which was one of those, so
    every registration deleted all of them and reported success. Losing
    ``LatestRevision`` alone is unrecoverable — ``next_number`` reads an absent
    counter as zero and hands out revision 1 again, and the next save overwrites
    the existing revision-1 body in S3.

    A targeted ``SET`` has no such failure mode: an attribute it does not name
    survives, so a field added to the head record later needs no edit here. The
    four ``if_not_exists`` defaults below are the one hand-listed set that
    remains, and forgetting to extend it costs a default rather than an
    attribute.

    The body is written INLINE (config sections as top-level attributes), which
    is the shape this resolver has always produced; a compressed source row is
    converted, so the storage markers are REMOVEd in the same call.

    ``attribute_exists(Configuration)`` makes the write conditional on the row
    the body was read from still existing, so a profile deleted in between is not
    silently re-created from content read before it went away.
    """
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    assignments: List[str] = []

    def _name(attribute: str) -> str:
        token = f"#n{len(names)}"
        names[token] = attribute
        return token

    for i, (key, value) in enumerate(sorted(payload.items())):
        if key in _WRITER_OWNED_ATTRIBUTES:
            # Skipped, not merely redundant: one UpdateExpression may not touch the
            # same attribute path twice, and DynamoDB rejects the whole call with
            # "Two document paths overlap with each other" if it does. The gzip blob
            # a compressed row carries DOES hold `_config_format` — the library's
            # _compress_item keeps only its own metadata fields at top level and
            # sweeps that marker into the body — so on any row written by the
            # current manager this collides with the explicit SET below. The
            # partition key is excluded for a different reason: an UpdateExpression
            # cannot assign it at all. Row metadata is excluded for a third: it is
            # bookkeeping this writer does not own, so a stale copy read out of the
            # body must not be written back over the live attribute.
            continue
        placeholder = f":v{i}"
        values[placeholder] = value
        assignments.append(f"{_name(key)} = {placeholder}")

    values[":ts"] = timestamp
    values[":format"] = "full"
    values[":active"] = True
    values[":description"] = ""
    values[":managed"] = False
    assignments.append(f"{_name('UpdatedAt')} = :ts")
    assignments.append(f"{_name(_CONFIG_FORMAT_MARKER)} = :format")
    # Defaults for a row that predates these attributes, matching what this
    # resolver has always written. An existing value is never overwritten.
    for attribute, placeholder in (
        ("CreatedAt", ":ts"),
        ("IsActive", ":active"),
        ("Description", ":description"),
        ("Managed", ":managed"),
    ):
        token = _name(attribute)
        assignments.append(f"{token} = if_not_exists({token}, {placeholder})")

    expression = (
        "SET "
        + ", ".join(assignments)
        + f" REMOVE {_name(_COMPRESSED_STORAGE_MARKER)}, {_name(_COMPRESSED_DATA_FIELD)}"
    )
    table.update_item(
        Key={"Configuration": config_key},
        UpdateExpression=expression,
        ConditionExpression="attribute_exists(Configuration)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _register(feature_id: str, hooks_in: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not feature_id:
        raise ValueError("featureId is required")
    table = _dynamodb.Table(_CONFIG_TABLE)
    version = _resolve_active_version(table)
    config_key = f"Config#{version}"

    resp = table.get_item(Key={"Configuration": config_key})
    item = resp.get("Item")
    if not item:
        raise RuntimeError(
            f"Active config version {config_key} not found; cannot register hooks"
        )
    payload = _decompress(item)

    validated = [_validate_hook(raw) for raw in hooks_in]
    # Before writing anything: a hook at a point this configuration's processing
    # mode never reaches is either refused (onError: fail) or warned about.
    warnings = _check_reachability(payload, feature_id, validated)

    # Group input hooks by step.
    by_step: Dict[str, List[Dict[str, Any]]] = {}
    for v in validated:
        step = _HOOK_POINT_TO_STEP[v["point"]]
        by_step.setdefault(step, []).append(
            {
                "featureId": feature_id,
                "arn": v["arn"],
                "order": v["order"],
                "onError": v["onError"],
                "enabled": v["enabled"],
            }
        )

    pack_count = _replace_pack_entries(payload, feature_id, by_step)

    # Write the body back with a targeted update, so head attributes this
    # resolver never reads survive — see _write_config_body.
    timestamp = _now()
    _write_config_body(
        table,
        config_key,
        {k: v for k, v in payload.items() if k not in _CONFIG_METADATA_FIELDS},
        timestamp,
    )
    logger.info(
        "Registered %d hook(s) for %s into %s",
        pack_count,
        feature_id,
        config_key,
    )
    out: Dict[str, Any] = {
        "featureId": feature_id,
        "hookCount": pack_count,
        "registeredAt": timestamp,
    }
    if warnings:
        # Extra key, not part of the GraphQL FeatureHooksRegistration type: this
        # resolver is invoked DIRECTLY by a feature stack's custom resource, which
        # gets the raw dict (and logs it). The runtime half of the signal — the
        # dispatcher's `unreachableHooks` in the execution history — is what an
        # operator reads after the fact.
        out["warnings"] = warnings
    return out


def _unregister(feature_id: str) -> bool:
    if not feature_id:
        raise ValueError("featureId is required")
    table = _dynamodb.Table(_CONFIG_TABLE)
    version = _resolve_active_version(table)
    config_key = f"Config#{version}"

    resp = table.get_item(Key={"Configuration": config_key})
    item = resp.get("Item")
    if not item:
        return True
    payload = _decompress(item)
    _replace_pack_entries(payload, feature_id, {})
    _write_config_body(
        table,
        config_key,
        {k: v for k, v in payload.items() if k not in _CONFIG_METADATA_FIELDS},
        _now(),
    )
    logger.info("Unregistered hooks for %s in %s", feature_id, config_key)
    return True


def handler(event: Dict[str, Any], _context: Any) -> Any:
    logger.info("registerFeatureHooks event: %s", sanitize_event_for_logging(event))
    field = event.get("info", {}).get("fieldName", "")
    args = event.get("arguments", {}) or {}
    if field == "registerFeatureHooks":
        payload = args.get("input", {}) or {}
        return _register(payload.get("featureId", ""), payload.get("hooks") or [])
    if field == "unregisterFeatureHooks":
        return _unregister(args.get("featureId", ""))
    raise ValueError(f"Unknown field: {field!r}")
