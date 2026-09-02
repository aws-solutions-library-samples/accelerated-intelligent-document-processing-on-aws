# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Opt-in CLASS confidence for classification: prompt assembly + resolution.

Turning `classification.confidence.mode` on splices an instruction block into the
classification task prompt and resolves whatever comes back into a confidence
plus (in ``topk`` mode) the ranked runner-up classes. Off by default, and no-op
when off.

Two modes, and the difference between them is the whole point:

- ``verbalized`` asks for one self-reported number. It is the cheapest thing to
  add and the *worst* calibrated: a model asked "how sure are you?" answers ~0.95
  almost everywhere, which is worse than no score because it invites automated
  escalation on noise.
- ``topk`` asks for ranked candidate classes with probabilities ("80 % W-2, 15 %
  1099"). Forcing the model to enumerate and rank alternatives makes it
  distribute probability mass instead of defaulting to near-certainty — see Tian
  et al., "Just Ask for Calibration" (EMNLP 2023). This is the same reasoning
  behind extraction's 1S-TopK path (``extraction/topk_resolver.py``); the shapes
  differ because that one is per-field (``G1``/``P1``) and this one is per-page
  over a closed class vocabulary, so a ``candidates`` list of real class names is
  both more legible and validatable against the vocabulary.

The blocks are EDITABLE CONFIG TEMPLATES
(``classification.confidence.task_prompt_topk`` /
``task_prompt_verbalized``), not hardcoded strings, matching how every other
prompt in this repo can be tuned without a code change.

Both modes cost OUTPUT TOKENS PER PAGE, because page-level classification is one
inference per page. That is why this is opt-in.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from idp_common.utils import parse_confidence

logger = logging.getLogger(__name__)

# Splice the static instruction block BEFORE the runtime document sections so it
# stays inside the prompt-cache prefix. Same markers, and the same reason, as
# extraction/prompt_assembly.py: classification runs per page, so a block that
# lands after the cache point is re-read on every page of every document.
_SPLICE_MARKERS = ("<<CACHEPOINT>>", "<document-ocr-data>", "{DOCUMENT_TEXT}")

# Rendered into the topk block so the instruction states a concrete number.
TOP_K_PLACEHOLDER = "{TOP_K_CANDIDATES}"

# Marker used to keep the splice idempotent.
_BLOCK_MARKER = "<class-confidence>"


def resolve_top_k(configured: int, num_classes: int) -> int:
    """Resolve how many candidates to request.

    Never more than the number of configured classes: asking for 5 candidates
    from a 3-class vocabulary invites the model to invent classes to fill the
    list, and every invented one has to be discarded anyway. Never fewer than 2 —
    a single candidate is a verbalized confidence with extra syntax, and the
    calibration benefit comes from having to rank alternatives.
    """
    if num_classes <= 0:
        return max(2, configured)
    return max(2, min(configured, num_classes))


def append_class_confidence_block(
    core: str, block: str, top_k: Optional[int] = None
) -> str:
    """Splice a class-confidence instruction block into a task prompt.

    Inserted before the first document/cache-point marker so the block stays
    cacheable; appended at the end when the prompt has no such marker. Idempotent
    — a prompt that already carries a block is returned unchanged, so a custom
    prompt that spells the instruction out itself is not double-instructed.
    """
    if not core or not block:
        return core
    if _BLOCK_MARKER in core:
        return core
    rendered = block.strip("\n")
    if top_k is not None:
        rendered = rendered.replace(TOP_K_PLACEHOLDER, str(top_k))
    for marker in _SPLICE_MARKERS:
        idx = core.find(marker)
        if idx != -1:
            return core[:idx] + rendered + "\n\n" + core[idx:]
    return core.rstrip() + "\n\n" + rendered + "\n"


def parse_candidates(
    raw: Any, valid_classes: Optional[set] = None
) -> List[Dict[str, Any]]:
    """Normalize a model-reported ``candidates`` list.

    Returns ``[{"class": str, "probability": float}, ...]`` ordered most likely
    first, dropping entries that are unusable (no class name, unparseable
    probability, or — when a vocabulary is supplied — a class the deployment does
    not define). Returns ``[]`` for anything that is not a usable list, so a
    malformed answer degrades to "no candidates" instead of failing the page.

    Probabilities are renormalized ONLY when they sum to more than 1.0. A
    distribution cannot exceed 1, so a sum of 1.4 makes every value meaningless
    as a probability and is rescaled. A sum *below* 1 is left alone: the missing
    mass is legitimately "some other class", which is information, and inflating
    the top candidate to absorb it would manufacture confidence.
    """
    if not isinstance(raw, list):
        return []

    parsed: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("class") or entry.get("type") or entry.get("doc_type")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if valid_classes is not None and name not in valid_classes:
            logger.debug("Dropping candidate class %r: not in the vocabulary", name)
            continue
        probability = parse_confidence(
            entry.get("probability", entry.get("confidence")),
            context=f"candidate {name}",
        )
        if probability is None:
            continue
        parsed.append({"class": name, "probability": probability})

    if not parsed:
        return []

    # Deduplicate on class, keeping the highest probability the model gave it.
    best: Dict[str, float] = {}
    for candidate in parsed:
        name = candidate["class"]
        if name not in best or candidate["probability"] > best[name]:
            best[name] = candidate["probability"]
    parsed = [{"class": k, "probability": v} for k, v in best.items()]

    total = sum(c["probability"] for c in parsed)
    if total > 1.0:
        logger.info(
            "Candidate probabilities sum to %.3f; rescaling to a distribution",
            total,
        )
        parsed = [
            {"class": c["class"], "probability": c["probability"] / total}
            for c in parsed
        ]

    parsed.sort(key=lambda c: c["probability"], reverse=True)
    return parsed


def confidence_from_candidates(
    candidates: List[Dict[str, Any]], reported_class: Optional[str]
) -> Optional[float]:
    """Derive the page's confidence from its ranked candidates.

    The probability of the class actually being STORED — not simply the highest
    probability in the list. The two differ when the model's ``class`` disagrees
    with its own ranking (it happens), and in that case reporting the top
    candidate's probability would attach a number to a class the page was not
    given.

    ``None`` when the stored class does not appear among the candidates at all:
    the model gave no probability for the answer it chose, and inferring one from
    the leftover mass would be an invention.
    """
    if not candidates:
        return None
    if reported_class:
        for candidate in candidates:
            if candidate["class"] == reported_class:
                return candidate["probability"]
        logger.warning(
            "Reported class %r is absent from its own candidate list (%s); "
            "leaving the page unscored",
            reported_class,
            [c["class"] for c in candidates],
        )
        return None
    return candidates[0]["probability"]


def resolve_class_and_confidence(
    reported_class: Optional[str],
    reported_confidence: Any,
    reported_candidates: Any,
    valid_classes: Optional[set] = None,
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    """Resolve one page's confidence and candidate list from a parsed response.

    Precedence: an explicit ``confidence`` wins when present (a prompt that asks
    for one means it), otherwise the probability of the stored class from the
    candidate list. Works in every mode, including ``off`` — a custom prompt that
    asks for either shape is honoured without any configuration.
    """
    candidates = parse_candidates(reported_candidates, valid_classes)
    confidence = parse_confidence(reported_confidence)
    if confidence is None:
        confidence = confidence_from_candidates(candidates, reported_class)
    return confidence, candidates
