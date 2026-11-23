# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The steps a run will do, in order, each with a state.

One derivation for every surface that says what a run does before it starts: the
dialog that finishes a review, the Process Changes confirmation and the CLI's
reprocess description. The list is read off the step list (``steps.py``) and the
state machine entry of the start (``asl.entry_for``), so it agrees with what the
run does.

This module imports pydantic, the standard library and the run plan modules
only. In particular it does not import ``idp_common.review_queues``.
"""

from __future__ import annotations

from typing import Final, List, Literal, Mapping, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .asl import ENTRY_EXTRACTION_SECTIONS, Entry, entry_for
from .models import RunKind
from .restart import CAUSE_NO_RESULT
from .steps import (
    BDA,
    BEGINNING,
    PIPELINE,
    REJECTED,
    RunPlanError,
    display_order,
    resolve_start,
    step,
    step_ids,
    step_label,
)

__all__ = [
    "PLANNED_NOT_ENABLED",
    "PLANNED_RUN",
    "PLANNED_SKIPPED",
    "PlannedRun",
    "PlannedSection",
    "PlannedStep",
    "StartDescription",
    "describe_start",
    "planned_run",
    "planned_steps",
]

#: The states of a planned step. ``run``: the run does the step. ``skipped``: the
#: run plan leaves it out. ``not_enabled``: the run reaches the step, and the
#: configuration has it switched off.
PLANNED_RUN: Final = "run"
PLANNED_SKIPPED: Final = "skipped"
PLANNED_NOT_ENABLED: Final = "not_enabled"


class _Planned(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True
    )


class PlannedStep(_Planned):
    """One step of a planned run."""

    id: str
    label: str
    state: Literal["run", "skipped", "not_enabled"]
    #: The step comes before the start: its output is kept, and the run reads it.
    before_start: bool = Field(default=False, exclude=True)


class PlannedSection(_Planned):
    """A section the run extracts again, and why."""

    section_id: str
    cause: str


class PlannedRun(_Planned):
    """Where a run starts and which steps it does, in a processing mode."""

    mode: str
    #: The step the run starts at, after aliases.
    start_step: str
    steps: List[PlannedStep]
    #: The sections the run extracts again. Empty means every section the run
    #: extracts, or none when it does not extract.
    sections: List[PlannedSection]


def _steps_of(
    entry: Entry,
    mode: str,
    *,
    kind: RunKind,
    skip: Sequence[str],
    enabled: Mapping[str, bool],
) -> List[PlannedStep]:
    order = display_order(mode, review_completed=kind == RunKind.REVIEW_COMPLETED)
    if entry.start_step == REJECTED:
        # The run records the rejection and does no step.
        return [
            PlannedStep(id=step_id, label=step_label(step_id), state=PLANNED_SKIPPED)
            for step_id in order
        ]
    if entry.start_step not in order:
        raise RunPlanError(
            f"a {RunKind(kind).value} run in '{mode}' mode has no step "
            f"'{entry.start_step}' to start at"
        )
    first = order.index(entry.start_step)
    skipped_by_plan = set(skip) | set(entry.forced_skips)
    # Where the state machine enters. A summarization or evaluation start enters
    # at policy rule validation with that stage skipped (``asl.entry_for``), so
    # what lies between there and the start and is not skipped still runs:
    # Review Routing does.
    entered = min(
        [order.index(step_id) for step_id in entry.forced_skips if step_id in order]
        + [first]
    )
    planned: List[PlannedStep] = []
    for index, step_id in enumerate(order):
        if index < entered or step_id in skipped_by_plan:
            state = PLANNED_SKIPPED
        elif enabled.get(step_id) is False:
            state = PLANNED_NOT_ENABLED
        else:
            state = PLANNED_RUN
        # Kept and read: a step before the start that the run does not do.
        before_start = index < first and state == PLANNED_SKIPPED
        planned.append(
            PlannedStep(
                id=step_id,
                label=step_label(step_id),
                state=state,
                before_start=before_start,
            )
        )
    return planned


def planned_steps(
    start: str,
    mode: str,
    *,
    kind: RunKind,
    skip: Sequence[str] = (),
    enabled: Mapping[str, bool] = {},
    has_sections: bool = False,
) -> List[PlannedStep]:
    """Every step of ``mode`` in order, with what a run from ``start`` does.

    | The step | State |
    |---|---|
    | Comes before where the run enters | ``skipped``, with ``before_start`` set |
    | Named in ``skip``, or skipped by the entry of the start | ``skipped`` |
    | Review Routing, for a summarization or evaluation start | ``run``: the run enters at policy rule validation, so routing still runs |
    | ``enabled`` holds ``False`` for its id | ``not_enabled`` |
    | Otherwise | ``run`` |

    ``enabled`` is the answer of ``idp_common.config.feature_flags.step_enabled``;
    a step it does not name is enabled. Apply Review Changes is listed only for
    a ``review_completed`` run. A rejected review's run does no step, so every
    step is ``skipped``. A start the mode does not have starts at the beginning,
    as the run itself does (``asl.entry_for``).

    Raises:
        RunPlanError: the start is ``apply_review_changes`` and the run does not
            follow a completed review.
    """
    entry = entry_for(start, mode, has_sections)
    return _steps_of(entry, mode, kind=kind, skip=skip, enabled=enabled)


def planned_run(
    start: str,
    mode: str,
    *,
    kind: RunKind,
    skip: Sequence[str] = (),
    enabled: Mapping[str, bool] = {},
    sections: Sequence[str] = (),
    causes: Mapping[str, str] = {},
) -> PlannedRun:
    """The planned steps of a run with its start and the sections it extracts.

    ``sections`` are the sections the plan names and ``causes`` says why each is
    extracted again, by section id (the causes of ``restart.py``). A section
    with no cause given is one the run extracts because it has no result.
    The sections are listed only when the run enters at extraction for named
    sections: any other run extracts every section or none.

    Raises:
        RunPlanError: as :func:`planned_steps`.
    """
    entry = entry_for(start, mode, bool(sections))
    listed = sections if entry.start_at == ENTRY_EXTRACTION_SECTIONS else ()
    return PlannedRun(
        mode=mode,
        start_step=entry.start_step,
        steps=_steps_of(entry, mode, kind=kind, skip=skip, enabled=enabled),
        sections=[
            PlannedSection(
                section_id=section_id, cause=causes.get(section_id, CAUSE_NO_RESULT)
            )
            for section_id in listed
        ],
    )


class StartDescription(NamedTuple):
    """What a run from a start step clears, keeps and leaves out, by output name."""

    #: The outputs the run produces again, so they are cleared before it is queued.
    cleared: List[str]
    #: The outputs of the steps before the start, which the run reads.
    kept: List[str]
    #: The outputs of the steps the run is told to skip: not produced again, and
    #: not cleared.
    left_out: List[str]


#: What the first step produces, by mode, in words: the step itself is "the
#: beginning", which says where a run starts and not what is made there.
_BEGINNING_OUTPUT = {PIPELINE: "OCR", BDA: "Bedrock Data Automation results"}


def _output_name(planned: PlannedStep, mode: str) -> str:
    return _BEGINNING_OUTPUT[mode] if planned.id == BEGINNING else planned.label


def describe_start(
    start: str, mode: str, *, skip: Sequence[str] = ()
) -> StartDescription:
    """What a run that starts at ``start`` clears, keeps and leaves out, in a mode.

    A run produces the output of its start step and of every later step again,
    so those are cleared before it is queued; the outputs of the earlier steps
    are what it reads, and are kept. A later step named in ``skip`` is not
    produced again and its output is not cleared, so it is left out. The names
    are those of the outputs of the steps a run can start at, in pipeline order.
    ``start`` may be an alias.

    Raises:
        UnknownStep: ``start`` is not a step or an alias. ``resume`` is neither
            (where it starts depends on the document), and nor is an internal
            start, which has no place in the step list.
        StepNotInMode: the step does not exist in ``mode``.
    """
    step(resolve_start(start, mode))
    run_step_ids = step_ids()
    description = StartDescription([], [], [])
    for planned in planned_steps(start, mode, kind=RunKind.REPROCESS, skip=skip):
        if planned.id not in run_step_ids:
            continue
        if planned.before_start:
            names = description.kept
        elif planned.state == PLANNED_SKIPPED:
            names = description.left_out
        else:
            names = description.cleared
        names.append(_output_name(planned, mode))
    return description
