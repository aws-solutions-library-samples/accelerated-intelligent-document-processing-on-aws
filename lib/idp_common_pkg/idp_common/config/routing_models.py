# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``routing`` section of a configuration profile: review routing tables.

A routing table is an ordered list of routing rules. Each rule has conditions over
routing inputs, a list of actions (send the document to a review queue; an empty
list means no review) and a reason the reviewer reads. The table is evaluated once
per run, after policy rule validation, and the first rule that matches wins.

**These models accept more than a saved profile may hold, on purpose.** A document
is processed with the revision it is pinned to, and that revision must always
load: one written before this section existed, and one written by newer code that
knows a value this code does not. So:

- every field has a default, and a profile with no ``routing`` key gets the
  built-in table (:func:`builtin_rules`) from the model default;
- ``stage``, ``hit_policy``, ``engine``, ``aggregate`` and the action ``type``
  are plain strings, not ``Literal`` types;
- there is no cross-field validator.

What a profile may hold is checked when it is saved, by
``idp_common.config.routing_validation.validate_routing``.
"""

from __future__ import annotations

from typing import Any, List, Sequence

from pydantic import BaseModel, Field, ValidationInfo, field_validator

#: The one table id, stage, hit policy and engine this release writes.
DEFAULT_TABLE_ID = "review-routing"
STAGE_AFTER_POLICY_RULE_VALIDATION = "after_policy_rule_validation"
HIT_POLICY_FIRST = "FIRST"
ENGINE_BUILTIN = "builtin"

#: The one action type this release has: send the document to a review queue.
ACTION_REVIEW_QUEUE = "review_queue"

#: The Default review queue's id (``idp_common.review_queues.queue_defs``
#: declares the same value; this module must not import that package).
DEFAULT_QUEUE = "default"

#: Ids of the built-in rules.
BUILTIN_EXTRACTION_FAILED = "builtin-extraction-failed"
BUILTIN_CONFIDENCE_ALERTS = "builtin-confidence-alerts"
BUILTIN_MISSING_REQUIRED = "builtin-missing-required"

#: The reasons of the built-in rules. ``system_defaults/base-routing.yaml`` holds
#: the same three texts.
BUILTIN_EXTRACTION_FAILED_REASON = (
    "Sections whose extraction failed: {extraction_failed_count}. They have no "
    "extracted values: enter them from the document."
)
BUILTIN_CONFIDENCE_REASON = (
    "Fields below the confidence threshold: {confidence_alert_count}. Check the "
    "highlighted fields and correct them."
)
BUILTIN_MISSING_REQUIRED_REASON = (
    "Required fields with no value: {missing_required_field_count}. Fill them in "
    "from the document."
)

MIN_CONSECUTIVE_PLACEMENTS = 1
MAX_CONSECUTIVE_PLACEMENTS = 10


class RoutingAction(BaseModel):
    """One thing that happens to a document a routing rule matches."""

    type: str = Field(
        default=ACTION_REVIEW_QUEUE,
        description="The action type; 'review_queue' is the only one",
    )
    queue: str = Field(
        default="",
        description="The review queue id, for a review_queue action",
    )


class RoutingCondition(BaseModel):
    """One test over one routing input. A rule matches when all its conditions hold."""

    input: str = Field(description="The routing input the condition reads")
    path: str = Field(
        default="",
        description="What the input is read for: a field path for field_value, a "
        "policy rule id for policy_rule_verdict, a policy type for "
        "failed_policy_rule_count, a class for class_present",
    )
    aggregate: str = Field(
        default="any",
        description="For an input that is a list: 'any' (one value passes the "
        "test), 'all' (every value passes) or 'count' (the test is on the number "
        "of values)",
    )
    test: str = Field(
        default="-",
        description="The test the value must pass, as a DMN unary test; '-' "
        "matches any value",
    )


class RoutingRule(BaseModel):
    """One row of a routing table."""

    id: str = Field(description="The rule's id, unique in its table")
    label: str = Field(default="", description="A name shown to people")
    enabled: bool = Field(
        default=True, description="A disabled rule is kept but never evaluated"
    )
    conditions: List[RoutingCondition] = Field(
        default_factory=list,
        description="Conditions that must all hold; with none, the rule matches "
        "every document",
    )
    actions: List[RoutingAction] = Field(
        default_factory=list,
        description="What happens to a matching document, in order; empty means "
        "it is not reviewed",
    )
    reason: str = Field(
        default="",
        description="What the reviewer reads; {placeholders} are filled with the "
        "document's values",
    )


class RoutingDefaultOutcome(BaseModel):
    """What happens to a document no routing rule matches."""

    actions: List[RoutingAction] = Field(
        default_factory=list,
        description="What happens to the document, in order; empty means it is "
        "not reviewed",
    )
    reason: str = Field(
        default="",
        description="What the reviewer reads; {placeholders} are filled with the "
        "document's values",
    )


def review_queue_of(actions: Sequence[RoutingAction]) -> str:
    """The queue of the first review_queue action, '' when there is none (not
    reviewed)."""
    for action in actions or ():
        if action.type == ACTION_REVIEW_QUEUE:
            return action.queue
    return ""


def queue_actions(queue: str) -> List[RoutingAction]:
    """``[RoutingAction(queue=queue)]`` for a queue id, ``[]`` for ``''``."""
    return [RoutingAction(queue=queue)] if queue else []


def builtin_rules() -> List[RoutingRule]:
    """The rules a profile has until an Admin changes them.

    They hold a document exactly when review queues held it before routing
    tables existed: a document that has not been reviewed goes to the Default
    queue when a section's extraction failed (#2038), when it has a confidence
    alert, and when it has a missing required field and
    ``hitl.route_missing_required_fields`` is on. The failed extraction comes
    first: such a section has no fields, so its required fields are missing
    too, and the first routing rule that matches gives the reviewer the reason.

    New instances on every call, so a caller that edits its copy changes no
    other configuration.
    """
    return [
        RoutingRule(
            id=BUILTIN_EXTRACTION_FAILED,
            label="Failed extractions",
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="extraction_failed_count", test="> 0"),
            ],
            actions=queue_actions(DEFAULT_QUEUE),
            reason=BUILTIN_EXTRACTION_FAILED_REASON,
        ),
        RoutingRule(
            id=BUILTIN_CONFIDENCE_ALERTS,
            label="Confidence alerts",
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="confidence_alert_count", test="> 0"),
            ],
            actions=queue_actions(DEFAULT_QUEUE),
            reason=BUILTIN_CONFIDENCE_REASON,
        ),
        RoutingRule(
            id=BUILTIN_MISSING_REQUIRED,
            label="Missing required fields",
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="route_missing_required_fields", test="true"),
                RoutingCondition(input="missing_required_field_count", test="> 0"),
            ],
            actions=queue_actions(DEFAULT_QUEUE),
            reason=BUILTIN_MISSING_REQUIRED_REASON,
        ),
    ]


class RoutingTable(BaseModel):
    """An ordered list of routing rules and what happens when none matches."""

    id: str = Field(default=DEFAULT_TABLE_ID, description="The table's id")
    stage: str = Field(
        default=STAGE_AFTER_POLICY_RULE_VALIDATION,
        description="Where in the pipeline the table is evaluated",
    )
    hit_policy: str = Field(
        default=HIT_POLICY_FIRST,
        description="How a rule is chosen when several match; FIRST takes the "
        "first in table order",
    )
    engine: str = Field(
        default=ENGINE_BUILTIN,
        description="The decision-table engine that evaluates it",
    )
    rules: List[RoutingRule] = Field(
        default_factory=builtin_rules,
        description="Routing rules, in the order they are evaluated",
    )
    default_outcome: RoutingDefaultOutcome = Field(
        default_factory=RoutingDefaultOutcome,
        description="What happens to a document no rule matches",
    )


def _default_tables() -> List[RoutingTable]:
    return [RoutingTable()]


class RoutingConfig(BaseModel):
    """Review routing: one decision table evaluated after policy rule validation."""

    tables: List[RoutingTable] = Field(
        default_factory=_default_tables,
        description="Routing tables. A profile holds exactly one",
    )
    max_consecutive_placements: int = Field(
        default=3,
        description="How many times in a row a document can be placed in a review "
        "queue: the first placement, and each time a rule sends a reviewed "
        "document to a queue again. Above it the run continues. Clamped to 1-10.",
    )

    @field_validator("max_consecutive_placements", mode="before")
    @classmethod
    def parse_max_consecutive_placements(cls, v: Any, info: ValidationInfo) -> int:
        """Whole number clamped to 1-10, so a stored out-of-range value never fails
        the configuration a document is pinned to."""
        # Imported here: ``models`` imports this module, and by the time a value is
        # validated it is fully loaded.
        from idp_common.config.models import _parse_whole_number

        return max(
            MIN_CONSECUTIVE_PLACEMENTS,
            min(MAX_CONSECUTIVE_PLACEMENTS, _parse_whole_number(v, info, cls)),
        )
