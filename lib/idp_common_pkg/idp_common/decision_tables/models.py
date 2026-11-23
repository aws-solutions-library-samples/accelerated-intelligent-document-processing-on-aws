# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The decision-table model, its evaluation result and the package's exceptions.

The shape follows a DMN decision table: input columns, output columns and ordered
rules whose cells are text. An input cell is a unary test (``sfeel``); an output
cell is a literal. Nothing here evaluates anything.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DecisionTableError(ValueError):
    """A decision table, or a cell of one, that cannot be used as written."""


class UnaryTestSyntaxError(DecisionTableError):
    """A unary test the grammar does not accept.

    ``column`` is the 1-based position in ``text`` where parsing stopped.
    """

    def __init__(self, message: str, *, text: str, column: int) -> None:
        super().__init__(f"{message} (column {column} of {text!r})")
        self.reason = message
        self.text = text
        self.column = column


class UnsupportedHitPolicy(DecisionTableError):
    """A hit policy the engine does not evaluate. The message names it."""


class UnknownEngine(DecisionTableError):
    """An engine name that is not registered. The message names it."""


class TableInput(BaseModel):
    """An input column."""

    id: str
    label: str = ""
    #: What the column reads. ``evaluate`` looks this text up in the context.
    expression: str
    type_ref: str = ""
    #: Unary tests the value must satisfy for the table to apply at all; empty
    #: for no restriction.
    allowed_values: str = ""
    #: ``""``, ``"any"`` or ``"all"``. When set and the value is a list, a rule's
    #: test is applied to each element instead of to the list.
    quantifier: str = ""


class TableOutput(BaseModel):
    """An output column."""

    id: str
    name: str
    type_ref: str = ""
    #: The literal the output takes when no rule matches; empty for none.
    default: str = ""


class TableRule(BaseModel):
    """One row: a unary test per input column and a literal per output column."""

    id: str
    description: str = ""
    input_entries: list[str] = Field(default_factory=list)
    output_entries: list[str] = Field(default_factory=list)


class DecisionTable(BaseModel):
    """Inputs, outputs and ordered rules, with the hit policy that picks a rule."""

    id: str
    name: str = ""
    hit_policy: str = "UNIQUE"
    inputs: list[TableInput] = Field(default_factory=list)
    outputs: list[TableOutput] = Field(default_factory=list)
    rules: list[TableRule] = Field(default_factory=list)


class DecisionResult(BaseModel):
    """What an evaluation decided, and from which input values."""

    #: The id of the rule that matched; empty when none did.
    matched_rule_id: str = ""
    #: True when no rule matched and the outputs are the table's defaults.
    default_used: bool = False
    #: Output name to value. ``None`` for an output with no value.
    outputs: dict[str, Any] = Field(default_factory=dict)
    #: Input expression to the context value the evaluation read for it.
    inputs_used: dict[str, Any] = Field(default_factory=dict)
