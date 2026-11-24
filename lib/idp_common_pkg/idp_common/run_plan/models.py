# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run plan: what a queued run carries to say where it starts.

A sender builds a ``RunPlan`` and attaches it to the document queue message as the
``RunPlan`` message attribute. The plan is a pure function of the message: it
holds no timestamp and no random value, so a message that is sent again carries
the same bytes.

This module imports pydantic and the standard library only. In particular it does
not import ``idp_common.review_queues``, so the queue processor can read a plan
without loading the review code.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import List, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from .steps import (
    APPLY_REVIEW_CHANGES,
    ASSESSMENT,
    BEGINNING,
    EVALUATION,
    EXTRACTION,
    INTERNAL_STARTS,
    POLICY_RULE_VALIDATION,
    REJECTED,
    RESUME,
    START_ALIASES,
    SUMMARIZATION,
    RunPlanError,
    StepNotInMode,
    UnknownStep,
    optional_step_ids,
    step_ids,
)

__all__ = [
    "MAX_RUN_PLAN_BYTES",
    "MAX_SECTIONS",
    "MAX_SECTION_ID_LENGTH",
    "RUN_PLAN_ATTRIBUTE",
    "REVIEW_KINDS",
    "RunKind",
    "RunPlan",
    "RunPlanError",
    "StepNotInMode",
    "UnknownStep",
    "decode",
    "encode",
]

#: The SQS message attribute that carries the plan.
RUN_PLAN_ATTRIBUTE = "RunPlan"
#: The encoded plan's size limit, in bytes.
MAX_RUN_PLAN_BYTES = 4096
MAX_SECTIONS = 500
MAX_SECTION_ID_LENGTH = 64


class RunKind(str, Enum):
    """Why a run was queued."""

    NEW_UPLOAD = "new_upload"
    REPROCESS = "reprocess"
    SAVED_EDITS = "saved_edits"
    REVIEW_COMPLETED = "review_completed"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_SKIPPED = "review_skipped"
    SENT_BACK = "sent_back"
    RESTART_FAILED = "restart_failed"
    LEGACY_REVIEW = "legacy_review"


#: The kinds that follow a review cycle, and so may name it.
REVIEW_KINDS = frozenset(
    {
        RunKind.REVIEW_COMPLETED,
        RunKind.REVIEW_REJECTED,
        RunKind.REVIEW_SKIPPED,
        RunKind.SENT_BACK,
    }
)

#: The one start each of these kinds has.
_FIXED_START = {
    RunKind.NEW_UPLOAD: BEGINNING,
    RunKind.LEGACY_REVIEW: BEGINNING,
    RunKind.REVIEW_REJECTED: REJECTED,
    RunKind.REVIEW_SKIPPED: POLICY_RULE_VALIDATION,
}

#: Where the run after a completed review starts: at applying the reviewer's
#: corrections, or at extraction when sections have to be extracted first.
_REVIEW_COMPLETED_STARTS = (APPLY_REVIEW_CHANGES, EXTRACTION)

#: A run that starts at one of these does not extract or assess again, so a
#: completed review still stands.
_STARTS_THAT_KEEP_A_REVIEW = frozenset(
    {APPLY_REVIEW_CHANGES, POLICY_RULE_VALIDATION, SUMMARIZATION, EVALUATION}
)


class RunPlan(BaseModel):
    """Where a run starts and which steps it skips."""

    model_config = ConfigDict(extra="forbid")

    #: The format version.
    v: Literal[1] = 1
    kind: RunKind
    #: A step id, an alias of one, ``resume`` (only until it is resolved), or an
    #: internal start: ``apply_review_changes`` or ``rejected``.
    start: str
    #: Optional steps the run skips: sorted, unique.
    skip: List[str] = []
    #: The sections to extract again, when the run starts at extraction or
    #: assessment and not every section needs it. Empty means every section.
    sections: List[str] = []
    #: The document's last review was completed and this run does not extract
    #: again.
    reviewed: bool = False
    #: The review cycle the run follows; review kinds only.
    cycle_id: str = ""

    @field_validator("start")
    @classmethod
    def _known_start(cls, value: str) -> str:
        if (
            value not in step_ids()
            and value not in START_ALIASES
            and value not in INTERNAL_STARTS
            and value != RESUME
        ):
            raise ValueError(
                f"'{value}' is not a start step; choose one of "
                f"{', '.join((*step_ids(), *START_ALIASES, RESUME))}"
            )
        return value

    @field_validator("skip")
    @classmethod
    def _optional_steps_only(cls, value: List[str]) -> List[str]:
        optional = optional_step_ids()
        unknown = sorted(set(value) - set(optional))
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)} cannot be skipped; the steps that can be "
                f"skipped are {', '.join(optional)}"
            )
        return sorted(set(value))

    @field_validator("sections")
    @classmethod
    def _bounded_sections(cls, value: List[str]) -> List[str]:
        if len(value) > MAX_SECTIONS:
            raise ValueError(
                f"a plan names at most {MAX_SECTIONS} sections, not {len(value)}"
            )
        for section_id in value:
            if not section_id or len(section_id) > MAX_SECTION_ID_LENGTH:
                raise ValueError(
                    f"a section id is 1 to {MAX_SECTION_ID_LENGTH} characters"
                )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RunPlan":
        fixed = _FIXED_START.get(self.kind)
        if fixed is not None and self.start != fixed:
            raise ValueError(
                f"a {self.kind.value} run starts at '{fixed}', not '{self.start}'"
            )
        review_completed = self.kind == RunKind.REVIEW_COMPLETED
        if review_completed and self.start not in _REVIEW_COMPLETED_STARTS:
            raise ValueError(
                f"a {self.kind.value} run starts at "
                f"{' or '.join(repr(start) for start in _REVIEW_COMPLETED_STARTS)}, "
                f"not '{self.start}'"
            )
        if self.start in INTERNAL_STARTS and fixed is None and not review_completed:
            raise ValueError(
                f"only a run that follows a review starts at '{self.start}'"
            )
        if self.sections and self.start not in (EXTRACTION, ASSESSMENT):
            raise ValueError(
                "sections can be named only by a run that starts at extraction or "
                "assessment"
            )
        if self.cycle_id and self.kind not in REVIEW_KINDS:
            raise ValueError(f"a {self.kind.value} run does not follow a review cycle")
        if self.reviewed and self.start not in _STARTS_THAT_KEEP_A_REVIEW:
            raise ValueError(
                f"a run that starts at '{self.start}' extracts or assesses again, "
                f"so the document does not count as reviewed"
            )
        return self


def encode(plan: RunPlan) -> str:
    """The plan as the compact JSON the message attribute carries.

    Keys are sorted, so equal plans encode to equal bytes.

    Raises:
        RunPlanError: the encoded plan is larger than ``MAX_RUN_PLAN_BYTES``.
    """
    text = json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    size = len(text.encode("utf-8"))
    if size > MAX_RUN_PLAN_BYTES:
        raise RunPlanError(
            f"The run plan is {size} bytes; the limit is {MAX_RUN_PLAN_BYTES} bytes"
        )
    return text


def decode(value: str) -> RunPlan:
    """The plan a message attribute carries.

    Raises:
        RunPlanError: the value is not JSON, or not a plan this code reads (an
            unknown key, an unknown version, an invalid combination).
    """
    try:
        return RunPlan.model_validate(json.loads(value))
    except (TypeError, ValueError, ValidationError) as error:
        raise RunPlanError(f"Not a run plan: {error}") from None
