# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluate a decision table against a context, through a registered engine.

``evaluate`` is the one entry point. A table names its engine; ``ENGINES`` maps the
name to an implementation, so another engine can be added without changing a
caller. One engine exists: ``builtin``, pure Python, deterministic, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import (
    DecisionResult,
    DecisionTable,
    DecisionTableError,
    TableInput,
    UnknownEngine,
    UnsupportedHitPolicy,
)
from .sfeel import matches, parse, parse_literal

FIRST = "FIRST"
BUILTIN = "builtin"


class Engine(Protocol):
    """What an engine provides: one evaluation of one table."""

    def evaluate(
        self, table: DecisionTable, context: Mapping[str, Any]
    ) -> DecisionResult: ...


def _entry_holds(column: TableInput, entry: str, value: Any) -> bool:
    """True only when the entry's test is ``True`` for the value.

    With a quantifier and a list value the test is applied per element: ``any``
    needs one element to pass, ``all`` needs every element of a non-empty list to
    pass. An empty list passes neither.
    """
    tests = parse(entry)
    if column.quantifier in ("any", "all") and isinstance(value, list):
        if tests.any_value:
            return True
        outcomes = [matches(tests, element) is True for element in value]
        if not outcomes:
            return False
        return any(outcomes) if column.quantifier == "any" else all(outcomes)
    return matches(tests, value) is True


class BuiltinEngine:
    """Hit policy FIRST over the S-FEEL subset in ``sfeel``.

    The first rule, in table order, whose every input entry holds wins, and its
    output entries are the result. With no match each output takes its default,
    and an output with no default is ``None``. A value outside an input's
    ``allowed_values`` means the table does not apply: every output is ``None``
    and no rule matches.
    """

    def evaluate(
        self, table: DecisionTable, context: Mapping[str, Any]
    ) -> DecisionResult:
        if table.hit_policy != FIRST:
            raise UnsupportedHitPolicy(
                f"Hit policy {table.hit_policy!r} is not supported; the builtin "
                f"engine evaluates {FIRST} only"
            )
        for rule in table.rules:
            if len(rule.input_entries) != len(table.inputs) or len(
                rule.output_entries
            ) != len(table.outputs):
                raise DecisionTableError(
                    f"Rule {rule.id!r} of table {table.id!r} does not have one "
                    f"entry per input and per output"
                )

        values = [context.get(column.expression) for column in table.inputs]
        inputs_used = {
            column.expression: value for column, value in zip(table.inputs, values)
        }

        for column, value in zip(table.inputs, values):
            if column.allowed_values and not _entry_holds(
                column, column.allowed_values, value
            ):
                return DecisionResult(
                    outputs={output.name: None for output in table.outputs},
                    inputs_used=inputs_used,
                )

        for rule in table.rules:
            if all(
                _entry_holds(column, entry, value)
                for column, entry, value in zip(
                    table.inputs, rule.input_entries, values
                )
            ):
                return DecisionResult(
                    matched_rule_id=rule.id,
                    outputs={
                        output.name: parse_literal(entry)
                        for output, entry in zip(table.outputs, rule.output_entries)
                    },
                    inputs_used=inputs_used,
                )

        return DecisionResult(
            default_used=True,
            outputs={
                output.name: parse_literal(output.default) for output in table.outputs
            },
            inputs_used=inputs_used,
        )


#: Engine name to implementation. A table's ``engine`` field selects one.
ENGINES: dict[str, Engine] = {BUILTIN: BuiltinEngine()}


def evaluate(
    table: DecisionTable, context: Mapping[str, Any], *, engine: str = BUILTIN
) -> DecisionResult:
    """Evaluate ``table`` against ``context`` with the named engine.

    ``context`` maps each input's ``expression`` text to its value; an expression
    the context lacks reads as ``None``.

    Raises:
        UnknownEngine: ``engine`` is not registered.
        UnsupportedHitPolicy: the engine does not evaluate the table's hit policy.
        UnaryTestSyntaxError: an entry is not a unary test the engine accepts.
    """
    implementation = ENGINES.get(engine)
    if implementation is None:
        raise UnknownEngine(
            f"Decision-table engine {engine!r} is not registered; registered: "
            f"{', '.join(sorted(ENGINES))}"
        )
    return implementation.evaluate(table, context)
