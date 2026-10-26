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

1. ⚠️ **Model parameters, modality and image settings are copied into every block
   that was not already carrying its own value.** A v0.8 configuration is pure flat:
   its two-stage stages had no parameters at all, so every one of them is absent
   here and takes the flat value — which is what made the conversion exact, and is
   why the copy must not be conditional on the stage's *model*. The condition is on
   the setting itself, and it is the same three-way test row 2 applies to the model:
   absent, blank, or equal to the one-stage value the profile already had. The case
   it protects is a **hybrid**, where by definition the stage blocks exist and may
   hold values the operator chose; copying over those unconditionally lost them, and
   is the harm row 2 already prevents for the model.
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
  to give a whole document one class from its first N pages. Combined with boundary
  detection it used to collapse only documents longer than N while sectioning shorter
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
import math
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

#: Confidence prompt blocks the SECTION CLASSIFICATION call can never read, so they
#: are not carried into it. That call is one call per section and returns one class,
#: and the service picks the per-segment (``_packet``) block only for a call that
#: returns a ``segments`` array — which section classification never does. Copying
#: them put roughly a kilobyte of prompt text into every converted profile at a path
#: nothing reads, where a reader would reasonably edit it and see no effect.
#: ``base-classification.yaml`` leaves them out of that block for the same reason.
SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS = frozenset(
    {"task_prompt_topk_packet", "task_prompt_verbalized_packet"}
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


def _is_unset(value: Any) -> bool:
    """Absent or blank — the two spellings of "this call chose nothing"."""
    return value is None or (isinstance(value, str) and not value.strip())


def _follows_one_stage(existing: Any, previous_one_stage: Any) -> bool:
    """Whether a stage's value was *following* the one-stage value, not chosen.

    The same test row 2 of the module docstring applies to the model, applied to the
    rest of ``_PER_CALL_SETTINGS``: a stage value that is absent or blank was never
    set, and one equal to the value the one-stage call already had was following it.
    Either way the flat value replaces it. A stage value that differs was chosen by
    the operator and is kept.

    The equality is **type-sensitive**, deliberately matching the rule row 2 already
    applies to the model: a stage holding ``"0.0"`` against a one-stage ``0.0`` reads
    as chosen and is kept. That is the protective direction, and the shipped defaults
    are consistently typed on all three calls, so the case needs a hand edit that
    mixes the two spellings to arise at all.
    """
    return _is_unset(existing) or existing == previous_one_stage


def _inferred_modality(prompt: Any) -> Dict[str, bool]:
    text = prompt if isinstance(prompt, str) else ""
    return {
        "text": "{DOCUMENT_TEXT}" in text,
        "images": "{DOCUMENT_IMAGE}" in text,
    }


def _is_page_limit(value: Any) -> bool:
    """True only for a value that really asks for the first N pages.

    ⚠️ **A ``bool`` is not a page count.** ``bool`` is an ``int`` subclass, so an
    unquoted YAML ``maxPagesForClassification: true`` used to answer True here and be
    converted to a one-page limit — which also took ``sectionSplitting`` to
    ``disabled`` with it, turning boundary detection off. ``_version.py`` guards the
    same trap on the version stamp.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            # ⚠️ Guarded *before* the truncation below, which cannot survive these:
            # ``int(inf)`` raises ``OverflowError`` and ``int(nan)`` raises
            # ``ValueError``. This function is called from ``conversion_notices``,
            # which ``merge_utils`` invokes ABOVE the ``try`` that wraps the merge,
            # so a raise here aborts ``idp-cli config-validate`` with a traceback
            # instead of reporting the value. ``FileShapeConfig.parse_max_pages``
            # guards the same two for the same reason.
            return False
        # Truncated: a fraction below one page is not a page count, and ``0.4 > 0``
        # would otherwise answer True here while ``parse_max_pages`` rendered it as
        # no limit at all — the same shape as the ``bool`` trap, with the same
        # consequence of taking boundary detection with it.
        return int(value) > 0
    text = str(value).strip().upper()
    if text in ("", "ALL", "0"):
        return False
    try:
        return int(text) > 0
    except ValueError:
        return False


def _known_section_splitting(value: Any) -> str | None:
    """The recognised spelling of ``value``, or ``None`` when there is not one.

    The page-limit conversion below has to decide "is this already ``disabled``?"
    about a *raw* stored value, before ``FileShapeConfig`` has seen it. Comparing the
    raw string meant an unrecognised value — a typo — was not ``disabled`` and so was
    rewritten to it, while the same typo without a page limit went to the validator
    and was reported there. One typo, opposite outcomes, decided by an unrelated key.
    So an unrecognised value answers ``None`` and is passed through untouched for the
    validator to rule on.

    ⚠️ **Unset is not unrecognised, and collapsing the two drops a page limit the
    operator set.** An absent, ``None`` or blank value means "take the default",
    which is what the validator does with it — so it answers
    ``DEFAULT_SECTION_SPLITTING`` here, and a blank value combined with a page limit
    still converts to ``disabled`` and keeps the limit, as it did before any of this.
    Answering ``None`` for a blank value instead skipped that conversion, left the
    blank to resolve to ``llm_determined``, and the page limit was then silently
    ignored at run time.
    """
    # Imported here rather than at module scope: the enum belongs to the model that
    # validates it, and `models` reaches this module (lazily) in the other direction.
    from ..models import DEFAULT_SECTION_SPLITTING, SECTION_SPLITTING_VALUES

    if _is_unset(value):
        return DEFAULT_SECTION_SPLITTING
    text = str(value).strip().lower()
    return text if text in SECTION_SPLITTING_VALUES else None


def _repair_reasoning_effort(out: Dict[str, Any]) -> None:
    """Replace a `reasoning_effort` no model family accepts with the default.

    ``reasoning_effort`` shipped with no validator at all, so a value written by hand
    or through the API was **stored verbatim** and forwarded to the provider. The
    model layer now substitutes the default on read, which stops it reaching the
    provider — but only in the validated view. Repairing it here puts the correct
    value in the **converted body** as well, which is what the write-back persists
    and what the surfaces that read a migrated dict without validating it (revision
    comparison, ``conversion_notices``) see. So a stored typo is healed rather than
    tolerated for the life of the profile.

    What this does not reach is a profile already in the 0.9 layout with no flat
    keys: that is a no-op for this hop by design, and the validator is the backstop
    for it. The two halves are deliberate, not redundant.
    """
    from ..models import normalise_reasoning_effort

    for path in (ONE_STAGE_LLM, SEGMENTATION_LLM, SECTION_CLASSIFICATION_LLM):
        node = _dict_or_empty(_walk(out, path))
        if "reasoning_effort" not in node:
            continue
        node["reasoning_effort"] = normalise_reasoning_effort(
            node["reasoning_effort"],
            "classification." + ".".join(path) + ".reasoning_effort",
        )


def _walk(root: Dict[str, Any], path: Iterable[str]) -> Any:
    """The value at ``path``, or ``None``. Unlike ``_node``, creates nothing."""
    node: Any = root
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


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
    splitting = _known_section_splitting(flat.get("sectionSplitting", "llm_determined"))
    if _is_page_limit(limit) and splitting is not None and splitting != "disabled":
        notices.append(
            f"classification.maxPagesForClassification is {limit!r} with "
            f"sectionSplitting {splitting!r}; converted to file_shape."
            "sectionSplitting 'disabled' (one section per document), because a "
            "page limit applies only when the whole document is one section. A "
            "short packet is now one section. Set the page limit to ALL to keep "
            "boundary detection."
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

    # ── Parameters, modality and image: every call that was not carrying its own
    # value for the setting (row 1). Read the "previous" one-stage values before
    # anything below writes to that block, for the same reason the model does.
    previous_one_stage = _dict_or_empty(_dict_or_empty(out.get("one_stage")).get("llm"))
    previous_per_call = {
        key: copy.deepcopy(previous_one_stage.get(key)) for key in _PER_CALL_SETTINGS
    }

    for key in _PER_CALL_SETTINGS:
        if key not in flat:
            continue
        value = flat[key]
        previous = previous_per_call[key]
        if isinstance(value, dict):
            # A sub-block merges key by key, so the protection is per inner key too:
            # `image: {target_width: 1000}` written flat must not reset a stage's own
            # target_height, and must not take a target_width that stage chose.
            _put_merged(out, ONE_STAGE_LLM, key, value)
            previous_block = _dict_or_empty(previous)
            for path in (SEGMENTATION_LLM, SECTION_CLASSIFICATION_LLM):
                node = _node(out, path + (key,))
                for inner, inner_value in value.items():
                    if _follows_one_stage(node.get(inner), previous_block.get(inner)):
                        node[inner] = copy.deepcopy(inner_value)
        else:
            _put(out, ONE_STAGE_LLM, key, value)
            for path in (SEGMENTATION_LLM, SECTION_CLASSIFICATION_LLM):
                if _follows_one_stage(_node(out, path).get(key), previous):
                    _put(out, path, key, value)

    _repair_reasoning_effort(out)

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
        _put_merged(
            out,
            SECTION_CLASSIFICATION_LLM,
            "confidence",
            {
                k: v
                for k, v in flat["confidence"].items()
                if k not in SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS
            },
        )

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
            splitting = _known_section_splitting(
                out["file_shape"].get("sectionSplitting", "llm_determined")
            )
            # `None` means the value is not one of the three. It is left alone so the
            # validator rules on it, exactly as it would without a page limit — see
            # `_known_section_splitting`.
            if splitting is not None and splitting != "disabled":
                logger.warning(
                    "classification.maxPagesForClassification is %r, which gives "
                    "the whole document one class from its first pages, but "
                    "sectionSplitting was %r. Converted to sectionSplitting "
                    "'disabled' (one document): the limit used to collapse only "
                    "documents longer than it while sectioning shorter ones, which no "
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
