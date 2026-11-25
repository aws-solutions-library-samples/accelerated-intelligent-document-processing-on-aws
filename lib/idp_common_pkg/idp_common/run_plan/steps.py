# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The pipeline steps a run can start at or skip, declared once.

Every surface that offers a start step derives it from this list: the SDK's step
values, the CLI's choices, the MCP tool's schema and the web UI's step picker
(the last two through the files ``scripts/generate_run_step_catalog.py`` writes).

A run also does steps it can neither start at nor skip. Those are the display
steps (``DISPLAY_STEPS``): a list of the steps a run does shows them in their
place, and no start or skip choice offers them.

Every step has one label, read through :func:`step_label`.

Pure data: this module imports the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PIPELINE = "pipeline"
BDA = "bda"
MODES = (PIPELINE, BDA)

#: Step ids, in pipeline order.
BEGINNING = "beginning"
CLASSIFICATION = "classification"
EXTRACTION = "extraction"
ASSESSMENT = "assessment"
POLICY_RULE_VALIDATION = "policy_rule_validation"
SUMMARIZATION = "summarization"
EVALUATION = "evaluation"


class RunPlanError(ValueError):
    """A run plan, or a part of one, that cannot be used as written."""


class UnknownStep(RunPlanError):
    """A step id that is not in the step list. The message names it."""


class StepNotInMode(RunPlanError):
    """A step that does not exist in the document's processing mode."""


@dataclass(frozen=True)
class RunStep:
    """One step a run can start at."""

    id: str
    #: English label, e.g. "Policy Rule Validation".
    label: str
    #: A required step can be skipped only when its output exists. An optional
    #: step (``required`` false) can always be skipped.
    required: bool
    #: The processing modes that have the step.
    modes: tuple[str, ...]
    #: Mode to the state machine states that do the step's work. A run that
    #: entered none of them skipped the step.
    states: dict[str, tuple[str, ...]] = field(default_factory=dict)


_POLICY_STATES = (
    "PolicyClassificationStep",
    "PolicyRuleValidationStep",
    "PolicyRuleConsolidationStep",
)

#: Every step, in pipeline order.
RUN_STEPS: tuple[RunStep, ...] = (
    RunStep(
        BEGINNING,
        "Beginning",
        True,
        (PIPELINE, BDA),
        {
            PIPELINE: ("OCRStep",),
            BDA: (
                "InvokeDataAutomationStep",
                "BDA_AssembleDocumentStep",
                "AssembleExistingDocumentStep",
            ),
        },
    ),
    RunStep(
        CLASSIFICATION,
        "Classification",
        True,
        (PIPELINE,),
        {PIPELINE: ("ClassificationStep",)},
    ),
    RunStep(
        EXTRACTION,
        "Extraction",
        True,
        (PIPELINE,),
        {
            PIPELINE: (
                "ExtractionPlanStep",
                "ShardExtractionStep",
                "ExtractionMergeStep",
                "ExtractionStep",
            )
        },
    ),
    RunStep(
        ASSESSMENT,
        "Assessment",
        False,
        (PIPELINE,),
        {PIPELINE: ("AssessmentStep",)},
    ),
    RunStep(
        POLICY_RULE_VALIDATION,
        "Policy Rule Validation",
        False,
        (PIPELINE, BDA),
        {PIPELINE: _POLICY_STATES, BDA: _POLICY_STATES},
    ),
    RunStep(
        SUMMARIZATION,
        "Summarization",
        False,
        (PIPELINE, BDA),
        {PIPELINE: ("SummarizationStep",), BDA: ("SummarizationStep",)},
    ),
    RunStep(
        EVALUATION,
        "Evaluation",
        False,
        (PIPELINE, BDA),
        {PIPELINE: ("EvaluationStep",), BDA: ("EvaluationStep",)},
    ),
)

#: Other spellings of a step id, in every mode. ``ocr`` is the first step of
#: Pipeline mode, which is the beginning.
START_ALIASES = {"ocr": BEGINNING}

#: In BDA mode one call does OCR, classification and extraction, so asking to
#: start at either of these means starting at the beginning.
BDA_START_ALIASES = {CLASSIFICATION: BEGINNING, EXTRACTION: BEGINNING}

#: Starts only the product itself uses, after a review: never offered to a user.
APPLY_REVIEW_CHANGES = "apply_review_changes"
REJECTED = "rejected"
INTERNAL_STARTS = (APPLY_REVIEW_CHANGES, REJECTED)

#: "Start at the first step that has no output": resolved to a step before a
#: plan is sent, so a sent plan never carries it.
RESUME = "resume"

#: The step that decides whether a document goes to a review queue.
REVIEW_ROUTING = "review_routing"


@dataclass(frozen=True)
class DisplayStep:
    """A step a run does that it can neither start at nor skip."""

    id: str
    #: English label, e.g. "Review Routing".
    label: str
    #: The run step it comes right before, in both processing modes.
    before: str
    #: Only the run that follows a completed review does the step.
    review_completed_only: bool = False


#: The display steps, in pipeline order.
DISPLAY_STEPS: tuple[DisplayStep, ...] = (
    DisplayStep(
        APPLY_REVIEW_CHANGES, "Apply Review Changes", POLICY_RULE_VALIDATION, True
    ),
    DisplayStep(REVIEW_ROUTING, "Review Routing", SUMMARIZATION),
)

_STEPS_BY_ID = {run_step.id: run_step for run_step in RUN_STEPS}


def step(step_id: str) -> RunStep:
    """The step with that id.

    Raises:
        UnknownStep: there is no such step.
    """
    try:
        return _STEPS_BY_ID[step_id]
    except KeyError:
        raise UnknownStep(
            f"'{step_id}' is not a pipeline step; the steps are {', '.join(step_ids())}"
        ) from None


def step_ids() -> tuple[str, ...]:
    """Every step id, in pipeline order."""
    return tuple(run_step.id for run_step in RUN_STEPS)


def optional_step_ids() -> tuple[str, ...]:
    """The ids of the steps a run can skip, in pipeline order."""
    return tuple(run_step.id for run_step in RUN_STEPS if not run_step.required)


def steps_for_mode(mode: str) -> tuple[RunStep, ...]:
    """The steps a processing mode has, in pipeline order."""
    return tuple(run_step for run_step in RUN_STEPS if mode in run_step.modes)


def display_order(mode: str, *, review_completed: bool = False) -> tuple[str, ...]:
    """The ids of the steps a run in ``mode`` does, display steps included.

    The steps of the mode in pipeline order, each display step right before the
    run step it precedes. A display step only a completed review has is listed
    when ``review_completed`` is set.
    """
    shown = [
        display
        for display in DISPLAY_STEPS
        if review_completed or not display.review_completed_only
    ]
    order: list[str] = []
    for run_step in steps_for_mode(mode):
        order.extend(display.id for display in shown if display.before == run_step.id)
        order.append(run_step.id)
    return tuple(order)


def step_label(step_id: str) -> str:
    """The label of a run step or a display step.

    Raises:
        UnknownStep: the id is neither.
    """
    for display in DISPLAY_STEPS:
        if display.id == step_id:
            return display.label
    return step(step_id).label


def position(step_id: str) -> int:
    """Where a step comes in pipeline order; a later step has a larger number."""
    return step_ids().index(step(step_id).id)


def start_values() -> tuple[str, ...]:
    """Every value a user can give as a start: the ids, the aliases and resume."""
    return (*step_ids(), *START_ALIASES, RESUME)


def resolve_start(start: str, mode: str) -> str:
    """The step id a start value means in a processing mode.

    ``ocr`` is the beginning in both modes. In BDA mode ``classification`` and
    ``extraction`` are the beginning too. An internal start is returned as it is.

    Raises:
        UnknownStep: the value is not a step, an alias or an internal start.
            ``resume`` is refused here: it is resolved against a document first.
        StepNotInMode: the step does not exist in ``mode`` and has no alias there.
    """
    if start in INTERNAL_STARTS:
        return start
    resolved = START_ALIASES.get(start, start)
    if mode == BDA:
        resolved = BDA_START_ALIASES.get(resolved, resolved)
    run_step = step(resolved)
    if mode not in run_step.modes:
        raise StepNotInMode(
            f"The step '{resolved}' does not exist in "
            f"{'BDA' if mode == BDA else 'Pipeline'} mode"
        )
    return resolved
