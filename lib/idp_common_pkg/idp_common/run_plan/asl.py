# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""From a run plan to the ``runPlan`` block of the execution input.

The queue processor is the only writer of that block. The state machine's
``CheckRunPlan`` choice reads ``startAt`` to jump to where the run starts, and the
skip choices read the four ``skip…`` flags. Every key is present whenever the
block is, so no state has to guard a single key.

This module imports the standard library and the plan model only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from .models import RunPlan
from .steps import (
    APPLY_REVIEW_CHANGES,
    ASSESSMENT,
    BEGINNING,
    CLASSIFICATION,
    EVALUATION,
    EXTRACTION,
    PIPELINE,
    POLICY_RULE_VALIDATION,
    REJECTED,
    SUMMARIZATION,
    RunPlanError,
    resolve_start,
)

#: The entry ids: what ``runPlan.startAt`` can be. ``CheckRunPlan`` has one rule
#: for each of them except ``beginning``, which is its default.
ENTRY_BEGINNING = "beginning"
ENTRY_CLASSIFICATION = "pipeline.classification"
ENTRY_EXTRACTION = "pipeline.extraction"
ENTRY_EXTRACTION_SECTIONS = "pipeline.extraction_sections"
ENTRY_ASSESSMENT = "pipeline.assessment"
ENTRY_APPLY_REVIEW_CHANGES = "apply_review_changes"
ENTRY_POLICY_RULE_VALIDATION = "policy_rule_validation"
ENTRY_REJECTED = "rejected"
ENTRY_IDS = (
    ENTRY_BEGINNING,
    ENTRY_CLASSIFICATION,
    ENTRY_EXTRACTION,
    ENTRY_EXTRACTION_SECTIONS,
    ENTRY_ASSESSMENT,
    ENTRY_APPLY_REVIEW_CHANGES,
    ENTRY_POLICY_RULE_VALIDATION,
    ENTRY_REJECTED,
)

#: Step id to the execution-input flag that skips it.
SKIP_FLAGS = {
    ASSESSMENT: "skipAssessment",
    POLICY_RULE_VALIDATION: "skipPolicyRuleValidation",
    SUMMARIZATION: "skipSummarization",
    EVALUATION: "skipEvaluation",
}

_PIPELINE_ENTRIES = {
    CLASSIFICATION: ENTRY_CLASSIFICATION,
    EXTRACTION: ENTRY_EXTRACTION,
    ASSESSMENT: ENTRY_ASSESSMENT,
}


@dataclass(frozen=True)
class Entry:
    """Where the state machine enters for a start step."""

    #: The entry id: ``runPlan.startAt``.
    start_at: str
    #: The logical step, after aliases: ``runPlan.startStep``.
    start_step: str
    #: The steps the entry forces the run to skip, by step id.
    forced_skips: tuple[str, ...]
    #: The start does not exist in the mode, so the run starts at the beginning.
    fell_back: bool = False


def entry_for(start: str, mode: str, has_sections: bool = False) -> Entry:
    """The state machine entry for a plan's start in a processing mode.

    Summarization and evaluation enter at policy rule validation with that stage
    skipped (and summarization too, for evaluation), so the routing step still
    runs: a reprocess from a late step withdraws an open review, and without
    routing the document would never be reviewed again.

    A start the mode does not have, or one that is not a step at all, enters at
    the beginning with ``fell_back`` set. The processing mode is resolved when
    the run starts, after the plan was built, and it can differ: a BDA profile
    with no project linked runs in Pipeline mode.
    """
    try:
        step_id = resolve_start(start, mode)
    except RunPlanError:
        return Entry(ENTRY_BEGINNING, BEGINNING, (), fell_back=True)
    if step_id == APPLY_REVIEW_CHANGES:
        return Entry(ENTRY_APPLY_REVIEW_CHANGES, step_id, ())
    if step_id == REJECTED:
        return Entry(ENTRY_REJECTED, step_id, ())
    if step_id == BEGINNING:
        return Entry(ENTRY_BEGINNING, step_id, ())
    if step_id == POLICY_RULE_VALIDATION:
        return Entry(ENTRY_POLICY_RULE_VALIDATION, step_id, ())
    if step_id == SUMMARIZATION:
        return Entry(ENTRY_POLICY_RULE_VALIDATION, step_id, (POLICY_RULE_VALIDATION,))
    if step_id == EVALUATION:
        return Entry(
            ENTRY_POLICY_RULE_VALIDATION,
            step_id,
            (POLICY_RULE_VALIDATION, SUMMARIZATION),
        )
    if mode == PIPELINE and step_id in _PIPELINE_ENTRIES:
        if step_id == EXTRACTION and has_sections:
            return Entry(ENTRY_EXTRACTION_SECTIONS, step_id, ())
        return Entry(_PIPELINE_ENTRIES[step_id], step_id, ())
    return Entry(ENTRY_BEGINNING, BEGINNING, (), fell_back=True)


def to_execution_input(
    plan: RunPlan, *, mode: str, policy_rule_validation_enabled: bool
) -> Dict[str, Any]:
    """The ``runPlan`` block for a plan, in the mode the run takes.

    Every key is always present. A step is skipped when the user asked for it or
    the entry forces it.
    """
    entry = entry_for(plan.start, mode, has_sections=bool(plan.sections))
    skipped = set(plan.skip) | set(entry.forced_skips)
    block: Dict[str, Any] = {
        "v": plan.v,
        "kind": plan.kind.value,
        "startStep": entry.start_step,
        "startAt": entry.start_at,
    }
    for step_id, flag in SKIP_FLAGS.items():
        block[flag] = step_id in skipped
    block.update(
        {
            "policyRuleValidationEnabled": bool(policy_rule_validation_enabled),
            # Only an extraction entry reads the list; it is empty otherwise.
            "sections": list(plan.sections)
            if entry.start_at == ENTRY_EXTRACTION_SECTIONS
            else [],
            "reviewed": plan.reviewed and not entry.fell_back,
            "mode": mode,
            "cycleId": plan.cycle_id,
        }
    )
    return block
