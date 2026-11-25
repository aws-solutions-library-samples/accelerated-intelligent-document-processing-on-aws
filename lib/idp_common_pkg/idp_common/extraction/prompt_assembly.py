# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Modular prompt selection/assembly for extraction + confidence (v0.6).

The prompts are EDITABLE CONFIG TEMPLATES (so they can be optimized/customized in
the UI), selected by the active settings rather than hardcoded in Python:

    extraction.task_prompt                                   -> extraction only
    extraction.task_prompt_extraction_with_confidence_topk   -> simple integrated (1S-TopK: G1/P1..GK/PK)
    extraction.task_prompt_extraction_with_confidence        -> advanced/agentic integrated (tool call)
    extraction.task_prompt_confidence                        -> confidence only (separate pass)
    extraction.task_prompt_bbox                          -> bounding-box block, appended to whichever
                                                            confidence-bearing prompt is active when
                                                            geometry.mode is 'llm' or 'llm_grounded'

Selection rules (mode = extraction.confidence.mode, geom = extraction.geometry.mode):
- Extraction inference prompt:
    * mode == 'integrated' and NOT agentic -> task_prompt_extraction_with_confidence_topk (1S-TopK)
    * mode == 'integrated' and agentic      -> task_prompt_extraction_with_confidence (tool call)
      (+ task_prompt_bbox when geom in {llm, llm_grounded})
    * otherwise                             -> task_prompt (extraction only)
- Confidence (separate-pass) prompt:
    * task_prompt_confidence (+ task_prompt_bbox when geom in {llm, llm_grounded})

The bbox block is composed in only for the LLM-box geometry modes; in the default
``ocr_only`` (and ``off``) the model is never asked for coordinates — geometry comes
from OCR value-matching. This is why the default prompts stay lean.
"""

from __future__ import annotations

from typing import Any

# Modes in which the model is asked to emit bounding boxes.
_LLM_BOX_MODES = ("llm", "llm_grounded")

# Marker used to splice the bbox block before the trailing document/cache-point
# sections so the static instruction block stays cache-friendly.
_SPLICE_MARKERS = ("<<CACHEPOINT>>", "<document-image>", "{DOCUMENT_IMAGE}")


def geometry_requires_llm_boxes(geometry_mode: str) -> bool:
    """True when the given geometry mode needs the model to emit bounding boxes."""
    return geometry_mode in _LLM_BOX_MODES


def _append_bbox_block(core: str, bbox_block: str) -> str:
    """Splice the bbox instruction block into ``core``.

    Inserted before the first document/cache-point marker (so runtime document
    sections stay after it); appended at the end if no marker is present. No-op if
    the core already carries a bbox block (idempotent for legacy prompts).
    """
    if not core or not bbox_block:
        return core
    if "spatial-localization" in core or "task_prompt_bbox" in core:
        return core
    block = bbox_block.strip("\n")
    for marker in _SPLICE_MARKERS:
        idx = core.find(marker)
        if idx != -1:
            return core[:idx] + block + "\n\n" + core[idx:]
    return core.rstrip() + "\n\n" + block + "\n"


def select_extraction_task_prompt(
    extraction_cfg: Any, *, integrated_ok: bool = True
) -> str:
    """Return the task prompt for the EXTRACTION inference, per settings.

    ``integrated_ok=False`` forces the plain extraction prompt even when
    ``confidence.mode == "integrated"``. The service passes this when it has
    downgraded a Simple + integrated section to a separate confidence pass because
    the class declares list fields (see ``ExtractionService.
    _simple_integrated_list_downgrade``) — the 1S-TopK prompt must not be sent
    for a section whose confidence will be scored separately.

    Integrated confidence selects a confidence-bearing extraction prompt (+ bbox
    for LLM-box geometry); otherwise the plain extraction task_prompt:

    - Simple (non-agentic) integrated uses the 1S-TopK prompt
      (``task_prompt_extraction_with_confidence_topk``): the model emits its
      top-K guesses with probabilities (``G1/P1`` … ``GK/PK``) per field and
      ``topk_resolver`` splits value (G1) from confidence (P1).
    - Advanced (agentic) integrated uses the tool-based prompt
      (``task_prompt_extraction_with_confidence``): the agent calls
      ``provide_field_assessment`` after extracting.

    ``extraction_cfg`` is an ExtractionConfig (has .confidence, .geometry,
    .agentic, and the task_prompt* fields).
    """
    confidence = extraction_cfg.confidence
    geometry_mode = extraction_cfg.geometry.mode
    if confidence.mode == "integrated" and integrated_ok:
        if not extraction_cfg.agentic.enabled:
            # Simple mode -> 1S-TopK prompt.
            core = (
                getattr(
                    extraction_cfg, "task_prompt_extraction_with_confidence_topk", ""
                )
                or extraction_cfg.task_prompt
            )
        else:
            # Advanced/agentic mode -> tool-based integrated prompt.
            core = (
                extraction_cfg.task_prompt_extraction_with_confidence
                or extraction_cfg.task_prompt
            )
        if geometry_requires_llm_boxes(geometry_mode):
            core = _append_bbox_block(core, extraction_cfg.geometry.task_prompt_bbox)
        return core
    return extraction_cfg.task_prompt


def select_confidence_task_prompt(extraction_cfg: Any) -> str:
    """Return the task prompt for the SEPARATE confidence pass, per settings.

    extraction.confidence.task_prompt (+ extraction.geometry.task_prompt_bbox for
    LLM-box geometry).
    """
    core = extraction_cfg.confidence.task_prompt
    if geometry_requires_llm_boxes(extraction_cfg.geometry.mode):
        core = _append_bbox_block(core, extraction_cfg.geometry.task_prompt_bbox)
    return core
