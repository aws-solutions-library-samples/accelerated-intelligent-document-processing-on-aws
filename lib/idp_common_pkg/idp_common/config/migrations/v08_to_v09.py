# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.8 -> v0.9: classification settings move inside their call.

Before this format, almost every classification setting sat flat under
``classification:``, where it was put when there was one strategy and one call.
With two strategies and three calls between them, a large share of those settings
did nothing in whichever mode was selected, and nothing in the file said which.
v0.9 nests each setting inside the call that uses it:

* ``one_stage``                          — ``strategy: single_pass``
* ``two_stage.segmentation.llm``         — two-stage, boundaries only
* ``two_stage.section_classification``   — two-stage, one class per section
* ``file_shape``                         — what is already known about the file
* ``fallback_class``                     — the one fallback, used everywhere

**Behaviour is preserved exactly**, and three rows of the mapping are what make
that true rather than approximately true:

1. ⚠️ **Model parameters, modality and image settings are copied into every
   block, unconditionally.** The two-stage stages had no parameters of their own
   in v0.8 — they always inherited the flat ones — so copying them only where a
   stage's model was empty would have reset every two-stage profile that named its
   own stage model to code defaults.
2. ⚠️ **An empty stage model means "inherit"**, so it becomes the flat model. A
   stage model that was set is kept.
3. ⚠️ **The packet window is copied into segmentation too.** Stage A read the
   shared ``packet.window_pages`` / ``overlap_pages`` in v0.8.

Two behaviours are deliberate and the migration guide states them:

* **Modality inference moves with the prompt.** In v0.8 every call inferred an
  unset modality flag from the single-pass prompt. In v0.9 each call infers from
  its own. For the shipped prompts the answer is identical — all of them carry
  both placeholders — so for a two-stage configuration the inferred flags are
  written explicitly onto the stage blocks whenever the single-pass prompt they
  were inferred from is present, and the stage calls see exactly what they saw.
* **A page limit implies "one document".** ``maxPagesForClassification`` exists
  to give a whole file one class from its first N pages. Combined with boundary
  detection it used to collapse only files longer than N while sectioning shorter
  ones normally — a side effect, not a feature, and one no configuration value can
  express. Such a combination is converted to ``sectionSplitting: disabled``, with
  a warning naming both settings.

The trigger is the presence of a flat key, not the version stamp, for the reason
every earlier hop gives: a sparse custom delta deep-merged onto current defaults
carries the current stamp while still holding legacy keys.

⚠️ **In a hybrid — flat and nested keys in one dict — the flat key wins.** That is
the opposite of the v0.8 hop, and deliberately so. A stored profile head and a
pinned revision are never hybrids: each is entirely one shape. A hybrid only
arises when a flat key is applied *on top of* a nested base — an old CLI file
uploaded over current defaults, a hand edit following an older document, or a tool
setting ``classification.model`` on a full v0.9 configuration. In every one of
those the flat key is the more recent, more deliberate value, and letting the
nested value win would discard it silently. Migration removes the flat keys, so
running it again is still a no-op.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Iterable, List, Tuple

from ._version import is_at_or_after, is_newer_than

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.9"

#: Every key v0.8 held directly under ``classification``. Any one of them present
#: means the dict still carries the flat shape.
FLAT_KEYS = frozenset(
    {
        "model",
        "model_lambda_hook_arn",
        "system_prompt",
        "task_prompt",
        "temperature",
        "top_p",
        "top_k",
        "reasoning_effort",
        "max_tokens",
        "maxPagesForClassification",
        "granularity",
        "packet",
        "segmentation",
        "labeling",
        "sectionSplitting",
        "contextPagesCount",
        "enforceValidClasses",
        "maxValidationRetries",
        "invalidClassFallback",
        "confidence",
        "image",
        "modality",
    }
)

#: Settings every call has, copied into all three blocks.
_PER_CALL_SETTINGS = (
    "model_lambda_hook_arn",
    "temperature",
    "top_p",
    "top_k",
    "reasoning_effort",
    "max_tokens",
    "image",
    "modality",
)

ONE_STAGE_LLM = ("one_stage", "llm")
SEGMENTATION_LLM = ("two_stage", "segmentation", "llm")
SECTION_CLASSIFICATION = ("two_stage", "section_classification")
SECTION_CLASSIFICATION_LLM = SECTION_CLASSIFICATION + ("llm",)


def _has_flat_keys(config: Dict[str, Any]) -> bool:
    classification = config.get("classification")
    return isinstance(classification, dict) and bool(FLAT_KEYS & classification.keys())


def _node(root: Dict[str, Any], path: Iterable[str]) -> Dict[str, Any]:
    """The dict at ``path``, created on the way down."""
    node = root
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    return node


def _put(root: Dict[str, Any], path: Tuple[str, ...], key: str, value: Any) -> None:
    """Set ``path.key`` from a flat value. In a hybrid, the flat value wins."""
    _node(root, path)[key] = copy.deepcopy(value)


def _put_merged(
    root: Dict[str, Any], path: Tuple[str, ...], key: str, value: Dict[str, Any]
) -> None:
    """Merge a flat sub-block in key by key; a flat key overrides a nested one."""
    if not isinstance(value, dict):
        return
    node = _node(root, path + (key,))
    for k, v in value.items():
        node[k] = copy.deepcopy(v)


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _inferred_modality(prompt: Any) -> Dict[str, bool]:
    text = prompt if isinstance(prompt, str) else ""
    return {
        "text": "{DOCUMENT_TEXT}" in text,
        "images": "{DOCUMENT_IMAGE}" in text,
    }


def _is_page_limit(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    text = str(value).strip().upper()
    if text in ("", "ALL", "0"):
        return False
    try:
        return int(text) > 0
    except ValueError:
        return False


def conversion_notices(config: Any) -> List[str]:
    """What this hop would change about ``config``, worded for the person who wrote it.

    The conversion itself is silent apart from a log line, which a user
    validating a file or importing a profile never sees. A caller that reports to
    that user (``merge_utils.validate_config``) adds these to its warnings, so a
    file still in the pre-0.9 layout says so, and the one conversion that changes
    behaviour — a page limit taking boundary detection with it — is not
    discovered later from a changed section count.
    """
    if not isinstance(config, dict) or not _has_flat_keys(config):
        return []
    flat = config["classification"]
    notices = [
        "classification uses the pre-0.9 flat layout and was converted to the "
        "nested one (each setting now sits inside the call that uses it). Keys "
        "converted: " + ", ".join(sorted(FLAT_KEYS & flat.keys())) + ". Save or "
        "re-download the configuration to keep the new layout; see "
        "docs/migration-v0612-to-v0613.md."
    ]
    limit = flat.get("maxPagesForClassification")
    splitting = str(flat.get("sectionSplitting", "llm_determined")).strip().lower()
    if _is_page_limit(limit) and splitting != "disabled":
        notices.append(
            f"classification.maxPagesForClassification is {limit!r} with "
            f"sectionSplitting {splitting!r}; converted to file_shape."
            "sectionSplitting 'disabled' (one document per file), because a page "
            "limit applies only to a one-document file. A short multi-document "
            "file is now one section. Set the page limit to ALL to keep boundary "
            "detection."
        )
    return notices


def migrate_v08_to_v09(config: Any) -> Any:
    """Convert a v0.8 (flat) classification block to the v0.9 nested shape."""
    if not isinstance(config, dict):
        return config
    if not _has_flat_keys(config):
        if is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
            # Preserve object identity for the pure no-op case, matching the
            # earlier steps in the chain.
            return config
        result = copy.deepcopy(config)
        result["config_format_version"] = TARGET_VERSION
        return result

    result = copy.deepcopy(config)
    flat: Dict[str, Any] = result["classification"]
    out: Dict[str, Any] = {k: v for k, v in flat.items() if k not in FLAT_KEYS}

    segmentation = _dict_or_empty(flat.get("segmentation"))
    labeling = _dict_or_empty(flat.get("labeling"))
    packet = _dict_or_empty(flat.get("packet"))

    # ── Model. An empty stage model meant "inherit the flat one".
    #
    # In a hybrid, a stage call that already names its own model keeps it: the
    # flat model replaces a stage model only where that stage was following the
    # one-stage model (same value) or names none. Otherwise a flat
    # `classification.model` written by an old script onto a two-stage profile
    # would overwrite stage models the operator chose, which v0.8 never did.
    flat_model = flat.get("model")
    previous_one_stage_model = _dict_or_empty(
        _dict_or_empty(out.get("one_stage")).get("llm")
    ).get("model")
    if flat_model is not None:
        _put(out, ONE_STAGE_LLM, "model", flat_model)
    for stage_cfg, path in (
        (segmentation, SEGMENTATION_LLM),
        (labeling, SECTION_CLASSIFICATION_LLM),
    ):
        stage_model = stage_cfg.get("model")
        if isinstance(stage_model, str) and stage_model.strip():
            _put(out, path, "model", stage_model)
        elif flat_model is not None:
            existing = _node(out, path).get("model")
            follows_one_stage = (
                not isinstance(existing, str)
                or not existing.strip()
                or existing == previous_one_stage_model
            )
            if follows_one_stage:
                _put(out, path, "model", flat_model)

    # ── Parameters, modality and image: every call, unconditionally (row 1).
    for key in _PER_CALL_SETTINGS:
        if key in flat:
            for path in (ONE_STAGE_LLM, SEGMENTATION_LLM, SECTION_CLASSIFICATION_LLM):
                if isinstance(flat[key], dict):
                    _put_merged(out, path, key, flat[key])
                else:
                    _put(out, path, key, flat[key])

    # ── Granularity and page input apply to one stage and to segmentation.
    if "granularity" in flat:
        _put(out, ("one_stage",), "granularity", flat["granularity"])
        _put(out, SEGMENTATION_LLM, "granularity", flat["granularity"])
    if "contextPagesCount" in flat:
        _put(out, ("one_stage", "page"), "contextPagesCount", flat["contextPagesCount"])
        _put(
            out,
            SEGMENTATION_LLM + ("page",),
            "contextPagesCount",
            flat["contextPagesCount"],
        )

    # ── One-stage prompts.
    for key in ("system_prompt", "task_prompt"):
        if key in flat:
            _put(out, ("one_stage", "page"), key, flat[key])
        if key in packet:
            _put(out, ("one_stage", "packet"), key, packet[key])

    # ── The packet window is shared with segmentation (row 3).
    for key in ("window_pages", "overlap_pages"):
        if key in packet:
            _put(out, ("one_stage", "packet"), key, packet[key])
            _put(out, SEGMENTATION_LLM + ("packet",), key, packet[key])

    # ── Segmentation.
    for key in ("system_prompt", "task_prompt"):
        if key in segmentation:
            _put(out, SEGMENTATION_LLM + ("page",), key, segmentation[key])
    seg_packet = _dict_or_empty(segmentation.get("packet"))
    for key in ("system_prompt", "task_prompt"):
        if key in seg_packet:
            _put(out, SEGMENTATION_LLM + ("packet",), key, seg_packet[key])
    if "boundary_hints" in segmentation:
        _put(out, SEGMENTATION_LLM, "boundary_hints", segmentation["boundary_hints"])

    # ── Section classification (was labeling).
    for key in ("method", "pages", "k"):
        if key in labeling:
            _put(out, SECTION_CLASSIFICATION, key, labeling[key])
    for key in ("system_prompt", "task_prompt"):
        if key in labeling:
            _put(out, SECTION_CLASSIFICATION_LLM, key, labeling[key])

    # ── Validation and confidence: the two class-assigning calls only.
    for key in ("enforceValidClasses", "maxValidationRetries"):
        if key in flat:
            _put(out, ONE_STAGE_LLM + ("validation",), key, flat[key])
            _put(out, SECTION_CLASSIFICATION_LLM + ("validation",), key, flat[key])
    if isinstance(flat.get("confidence"), dict):
        _put_merged(out, ONE_STAGE_LLM, "confidence", flat["confidence"])
        _put_merged(out, SECTION_CLASSIFICATION_LLM, "confidence", flat["confidence"])

    # ── One fallback.
    if "invalidClassFallback" in flat:
        out["fallback_class"] = copy.deepcopy(flat["invalidClassFallback"])

    # ── File shape. A page limit implies "one document" (see the module doc).
    if "sectionSplitting" in flat:
        _put(out, ("file_shape",), "sectionSplitting", flat["sectionSplitting"])
    if "maxPagesForClassification" in flat:
        limit = flat["maxPagesForClassification"]
        _put(out, ("file_shape",), "maxPagesForClassification", limit)
        if _is_page_limit(limit):
            splitting = (
                str(out["file_shape"].get("sectionSplitting", "llm_determined"))
                .strip()
                .lower()
            )
            if splitting != "disabled":
                logger.warning(
                    "classification.maxPagesForClassification is %r, which gives "
                    "the whole file one class from its first pages, but "
                    "sectionSplitting was %r. Converted to sectionSplitting "
                    "'disabled' (one document): the limit used to collapse only "
                    "files longer than it while sectioning shorter ones, which no "
                    "single setting can express. Set the page limit to ALL to keep "
                    "boundary detection.",
                    limit,
                    splitting,
                )
                out["file_shape"]["sectionSplitting"] = "disabled"

    # ── Modality inference moves with the prompt. For a two-stage config, write
    # the flags the stages inferred in v0.8 — from the single-pass prompt — onto
    # the stage blocks, so they keep seeing what they saw.
    if str(flat.get("strategy", "")).strip().lower() == "two_stage":
        granularity = str(flat.get("granularity") or "page").strip().lower()
        legacy_prompt = (
            packet.get("task_prompt")
            if granularity == "packet"
            else flat.get("task_prompt")
        )
        if isinstance(legacy_prompt, str) and legacy_prompt:
            inferred = _inferred_modality(legacy_prompt)
            explicit = _dict_or_empty(flat.get("modality"))
            for path in (SEGMENTATION_LLM, SECTION_CLASSIFICATION_LLM):
                modality = _node(out, path + ("modality",))
                for flag in ("text", "images"):
                    if explicit.get(flag) in (None, "") and modality.get(flag) in (
                        None,
                        "",
                    ):
                        modality[flag] = inferred[flag]

    result["classification"] = out
    logger.info("Migrated config v0.8 -> v0.9 (classification nested by call)")
    if not is_newer_than(result.get("config_format_version"), TARGET_VERSION):
        result["config_format_version"] = TARGET_VERSION
    return result
