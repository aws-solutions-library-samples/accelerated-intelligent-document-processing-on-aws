#!/usr/bin/env python3
"""Add the pre-0.9 flat classification keys to a benchmark config.

Why this exists: config format 0.9 nests every classification setting inside the
model call that uses it (``one_stage.*``, ``two_stage.*``, ``document_shape.*``,
``strategy``, ``fallback_class``). The release benchmark A/B uploads the SAME
generated file to the previous published release and to develop, and a release
that predates the nesting (v0.6.12 reads config format 0.7) does not know any of
those blocks. Its ``IDPConfig`` drops unknown keys without a word, so every
classification setting a cell made would be replaced by that release's code
defaults: a ``sectionSplitting: disabled`` cell runs ``llm_determined``, the model
becomes the code default and the task prompt is empty. The run still completes and
produces numbers, attributed to a configuration that never ran.

:func:`with_flat_classification` returns a copy of the config with the equivalent
flat keys written alongside the nested blocks:

* the release that predates the nesting reads the flat keys and ignores the
  nested blocks;
* the current reader (``idp_common.config.migrations.migrate_config``) treats the
  file as a hybrid, in which **the flat key wins**, and converts the flat keys back
  into the same nested values. So one file still serves both sides of the A/B.

What the current reader resolves from the copy is identical to the original for
everything a single-pass run reads. Two subtrees a single-pass run does not read do
change, because the migration copies a flat setting into every call that had it in
v0.8: the ``two_stage`` blocks take the one-stage model, parameters, granularity and
confidence, and at packet granularity ``one_stage.page`` takes the packet prompts
(the old release's holistic method read its prompt from the flat ``task_prompt``,
which the current reader files under ``one_stage.page``). The benchmark tests pin
that boundary exactly, so a change to either side of it is noticed.

Refused, because the old release cannot express them and the run would measure
something else: a two-stage strategy (there is no two-stage classification before
0.9), and an explicit ``one_stage.llm.modality`` flag that disagrees with the
selected prompt's placeholders (the old release decides modality from the
placeholders alone). Reported as notes, because whether they matter depends on the
documents: a packet window (the old holistic method sends the whole file in one
call), and packet-granularity class confidence (the old release composes the
confidence instruction on the page-level path only).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

#: The v0.9 classification blocks. A release that predates the nesting reads none
#: of them; :func:`with_flat_classification` carries their content in flat keys.
V09_BLOCKS = ("strategy", "one_stage", "two_stage", "document_shape", "fallback_class")

#: ``one_stage.granularity`` -> the pre-0.8 ``classificationMethod`` that made the
#: same number of calls.
METHOD_FOR_GRANULARITY = {
    "page": "multimodalPageLevelClassification",
    "packet": "textbasedHolisticClassification",
}

#: Model parameters copied verbatim from ``one_stage.llm`` to the flat block.
_LLM_PARAMETERS = (
    "model",
    "model_lambda_hook_arn",
    "temperature",
    "top_p",
    "top_k",
    "reasoning_effort",
    "max_tokens",
    "image",
)

#: The class-confidence keys the pre-0.9 flat ``confidence`` block declares. The
#: ``*_packet`` prompt variants arrived with the nesting, so writing them flat would
#: be a key the old release drops.
_FLAT_CONFIDENCE_KEYS = (
    "mode",
    "top_k_candidates",
    "task_prompt_topk",
    "task_prompt_verbalized",
)


class CompatError(ValueError):
    """The cell sets something a pre-0.9 release cannot run."""


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _flag(value: Any) -> Any:
    """A modality flag as the readers see it: ``None`` means "infer"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def flat_classification(classification: Dict[str, Any]) -> Tuple[Dict, List[str]]:
    """The pre-0.9 flat keys equivalent to a v0.9 single-pass ``classification``.

    Returns ``(flat_keys, notes)``. Raises :class:`CompatError` for a setting the
    old release cannot express. Only keys the cell's nested blocks actually hold
    are written, so nothing is invented from code defaults.
    """
    strategy = str(classification.get("strategy") or "single_pass").strip().lower()
    if strategy != "single_pass":
        raise CompatError(
            f"classification.strategy is {strategy!r}: a release that predates "
            f"config format 0.9 has no two-stage classification, so this cell "
            f"cannot run there. Leave it out of the cross-release A/B."
        )
    one = _dict(classification.get("one_stage"))
    llm = _dict(one.get("llm"))
    granularity = str(one.get("granularity") or "page").strip().lower()
    if granularity not in METHOD_FOR_GRANULARITY:
        raise CompatError(
            f"classification.one_stage.granularity is {granularity!r}; expected "
            f"one of {sorted(METHOD_FOR_GRANULARITY)}"
        )
    flat: Dict[str, Any] = {"classificationMethod": METHOD_FOR_GRANULARITY[granularity]}
    notes: List[str] = []

    for key in _LLM_PARAMETERS:
        if key in llm:
            flat[key] = copy.deepcopy(llm[key])

    # The old release has one prompt pair; it is the selected granularity's.
    prompts = _dict(one.get(granularity))
    for key in ("system_prompt", "task_prompt"):
        if key in prompts:
            flat[key] = prompts[key]
    page = _dict(one.get("page"))
    if "contextPagesCount" in page:
        flat["contextPagesCount"] = page["contextPagesCount"]

    validation = _dict(llm.get("validation"))
    for key in ("enforceValidClasses", "maxValidationRetries"):
        if key in validation:
            flat[key] = validation[key]

    confidence = _dict(llm.get("confidence"))
    flat_confidence = {
        k: confidence[k] for k in _FLAT_CONFIDENCE_KEYS if k in confidence
    }
    if flat_confidence:
        flat["confidence"] = copy.deepcopy(flat_confidence)

    if "fallback_class" in classification:
        flat["invalidClassFallback"] = classification["fallback_class"]
    shape = _dict(classification.get("document_shape"))
    for key in ("sectionSplitting", "maxPagesForClassification"):
        if key in shape:
            flat[key] = shape[key]

    # Modality: the old release sends text and the page image exactly when the
    # prompt carries the placeholder, which is also what the current reader does
    # with an unset flag. An explicit flag that disagrees cannot be expressed.
    raw_prompt = flat.get("task_prompt")
    task_prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    inferred = {
        "text": "{DOCUMENT_TEXT}" in task_prompt,
        "images": "{DOCUMENT_IMAGE}" in task_prompt,
    }
    modality = _dict(llm.get("modality"))
    for name, from_prompt in inferred.items():
        explicit = _flag(modality.get(name))
        if explicit is not None and explicit != from_prompt:
            raise CompatError(
                f"classification.one_stage.llm.modality.{name} is {explicit} but the "
                f"{granularity} task prompt implies {from_prompt}: a release that "
                f"predates config format 0.9 decides modality from the prompt's "
                f"placeholders alone, so it would not do what this cell asks."
            )

    if granularity == "packet":
        packet = _dict(one.get("packet"))
        if packet.get("window_pages") not in (None, ""):
            notes.append(
                f"one_stage.packet.window_pages={packet.get('window_pages')} has no "
                f"pre-0.9 equivalent: the old holistic method classifies the whole "
                f"file in one call, so a document longer than the window is "
                f"classified differently on the two sides."
            )
        if str(confidence.get("mode", "")).strip().lower() not in ("", "off"):
            notes.append(
                f"classification confidence mode {confidence.get('mode')!r} at packet "
                f"granularity: the old release composes the confidence instruction "
                f"on the page-level path only, so its holistic calls do not ask for "
                f"one."
            )
    return flat, notes


def with_flat_classification(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(copy of cfg with the flat classification keys added, notes)``.

    The input is not modified. A config with no ``classification`` block is
    returned as a plain copy. See the module docstring for what is refused and why.
    """
    out = copy.deepcopy(cfg)
    classification = out.get("classification")
    if not isinstance(classification, dict):
        return out, []
    flat, notes = flat_classification(classification)
    classification.update(flat)
    return out, notes
