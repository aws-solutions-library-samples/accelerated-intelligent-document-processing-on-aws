# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routing table as a decision table, and as DMN XML.

One mapping serves two uses. Evaluation turns a profile's routing table into a
``DecisionTable`` and runs it through ``idp_common.decision_tables``. Export writes
that same table as DMN XML, and import reads it back, so a routing table can be
kept, reviewed and edited in a DMN modeler.

**The mapping.**

- One input column per distinct ``(input, path, aggregate)`` a rule uses, in the
  order rules first use them. A rule with two conditions on the same input gets a
  second column for it. The column's expression is the *catalogue call*:
  ``reviewed``, ``section_classes("any")``,
  ``field_value("Invoice:Total","any")``,
  ``policy_rule_verdict("invoice_controls.total_matches_lines")``,
  ``failed_policy_rule_count("invoice_controls")``.
- A rule's entry in a column it does not use is ``-``.
- Three outputs: ``outcome`` (``"queue"`` or ``"no_review"``), ``queue`` (the queue
  id, or ``""``) and ``reason`` (the reason as written, placeholders unfilled, or
  ``""``), whose defaults are the table's default outcome. A rule's actions are
  written as the first two: the queue of its ``review_queue`` action, or
  ``"no_review"`` when it has none. A file with ``outcome`` and ``queue`` alone is
  read too, with empty reasons.
- For ``any`` and ``all`` the column carries a quantifier, so the engine applies
  the test to each value. For ``count`` the value is the number of values.

**In DMN XML** the table is one decision. A rule's label is its ``description``.
What DMN has no place for travels in attributes of the namespace
``urn:aws:idp:routing:1`` (prefix ``idp``): ``idp:stage`` and ``idp:engine`` on the
decision table, ``idp:enabled="false"`` on a disabled rule, and ``idp:id`` where a
table or rule id is not a valid XML id.

This module never imports the XML code at load: ``export_xml`` and ``import_xml``
import ``decision_tables.dmn`` when called, so the routing Lambda, which only
evaluates, never loads a parser.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn, Optional, Sequence, Union

from idp_common.config.routing_models import (
    DEFAULT_TABLE_ID,
    ENGINE_BUILTIN,
    STAGE_AFTER_POLICY_RULE_VALIDATION,
    RoutingAction,
    RoutingCondition,
    RoutingConfig,
    RoutingDefaultOutcome,
    RoutingRule,
    RoutingTable,
    queue_actions,
    review_queue_of,
)
from idp_common.config.routing_validation import (
    AGGREGATES,
    MAX_RULES,
    PATH_NONE,
    TYPE_FIELD,
    routing_input,
)
from idp_common.decision_tables.models import (
    DecisionTable,
    DecisionTableError,
    TableInput,
    TableOutput,
    TableRule,
    UnaryTestSyntaxError,
)
from idp_common.decision_tables.sfeel import format_literal, parse, parse_literal

#: The namespace of the attributes DMN has no place for, and its prefix.
ROUTING_NAMESPACE = "urn:aws:idp:routing:1"
ROUTING_PREFIX = "idp"

OUTPUT_OUTCOME = "outcome"
OUTPUT_QUEUE = "queue"
OUTPUT_REASON = "reason"

#: The values of the ``outcome`` output: the document goes to a review queue, or
#: it is not reviewed.
KIND_QUEUE = "queue"
KIND_NO_REVIEW = "no_review"

_ATTR_STAGE = f"{{{ROUTING_NAMESPACE}}}stage"
_ATTR_ENGINE = f"{{{ROUTING_NAMESPACE}}}engine"
_ATTR_ENABLED = f"{{{ROUTING_NAMESPACE}}}enabled"
_ATTR_ID = f"{{{ROUTING_NAMESPACE}}}id"

#: An id that can be written as an XML ``id`` unchanged.
_XML_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The kinds of unary-test literal, as the DMN ``typeRef`` a column carries.
_TYPE_REF = {
    "number": "number",
    "string": "string",
    "boolean": "boolean",
    "date": "date",
}


class RoutingExpressionError(ValueError):
    """An input expression that is not a catalogue call."""


def expression_for(condition: RoutingCondition) -> str:
    """The catalogue call a condition reads: the text of its input column.

    The arguments are the path, where the input takes one and it is set, then the
    aggregate, where the input is a list. An input with neither is its bare name.
    The same text keys the value in the evaluation context.
    """
    entry = routing_input(condition.input)
    arguments = []
    if condition.path and (entry is None or entry.path_kind != PATH_NONE):
        arguments.append(condition.path)
    if entry is not None and entry.is_list:
        arguments.append(condition.aggregate)
    if not arguments:
        return condition.input
    return f"{condition.input}({','.join(format_literal(a) for a in arguments)})"


def parse_expression(text: str) -> tuple[str, str, str]:
    """``(input, path, aggregate)`` for a catalogue call; the inverse of the above.

    Raises:
        RoutingExpressionError: the text is not a call to a routing input with the
            arguments that input takes.
    """
    source = (text or "").strip()
    match = _NAME_RE.match(source)
    if not match:
        raise RoutingExpressionError(f"{text!r} is not a routing input")
    name = match.group(0)
    entry = routing_input(name)
    if entry is None:
        raise RoutingExpressionError(f"{name!r} is not a routing input")
    rest = source[match.end() :].strip()
    arguments: list[str] = []
    if rest:
        if not (rest.startswith("(") and rest.endswith(")")):
            raise RoutingExpressionError(
                f"{text!r} is not a call to the routing input {name!r}"
            )
        inner = rest[1:-1]
        position = 0
        more = bool(inner.strip())
        while more:
            literal = _string_at(inner, position)
            if literal is None:
                raise RoutingExpressionError(
                    f"the arguments of {name!r} must be strings in quotes: {text!r}"
                )
            value, position = literal
            arguments.append(value)
            while position < len(inner) and inner[position].isspace():
                position += 1
            if position >= len(inner):
                more = False
            elif inner[position] != ",":
                raise RoutingExpressionError(
                    f"the arguments of {name!r} must be separated by commas: {text!r}"
                )
            else:
                position += 1

    aggregate = "any"
    if entry.is_list:
        if not arguments:
            raise RoutingExpressionError(
                f"{name!r} is a list, so it needs an aggregate: one of "
                f"{', '.join(AGGREGATES)}"
            )
        aggregate = arguments.pop()
        if aggregate not in AGGREGATES:
            raise RoutingExpressionError(
                f"{aggregate!r} is not an aggregate; use one of {', '.join(AGGREGATES)}"
            )
    path = ""
    if arguments:
        if entry.path_kind == PATH_NONE:
            raise RoutingExpressionError(f"{name!r} takes no path")
        path = arguments.pop(0)
    if arguments:
        raise RoutingExpressionError(f"{name!r} has too many arguments: {text!r}")
    if entry.needs_path and not path:
        raise RoutingExpressionError(f"{name!r} needs a path")
    return name, path, aggregate


def _string_at(text: str, start: int) -> Optional[tuple[str, int]]:
    """The string literal that starts at ``start`` (after spaces), and its end."""
    position = start
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != '"':
        return None
    end = position + 1
    while end < len(text):
        if text[end] == "\\":
            end += 2
            continue
        if text[end] == '"':
            try:
                value = parse_literal(text[position : end + 1])
            except UnaryTestSyntaxError:
                return None
            return (value, end + 1) if isinstance(value, str) else None
        end += 1
    return None


def _column_type(entry: Any, aggregate: str, tests: list[str]) -> str:
    """The DMN ``typeRef`` of a column, or empty when it cannot be said."""
    if aggregate == "count":
        return "number"
    if entry is not None and entry.type != TYPE_FIELD:
        return entry.type
    kinds: set[str] = set()
    for test in tests:
        try:
            kinds |= parse(test).literal_kinds()
        except UnaryTestSyntaxError:
            return ""
    return _TYPE_REF[next(iter(kinds))] if len(kinds) == 1 else ""


def _column_label(entry: Any, condition: RoutingCondition) -> str:
    label = entry.label if entry is not None else condition.input
    if condition.path:
        label = f"{label}: {condition.path}"
    if entry is not None and entry.is_list:
        label = f"{label} ({condition.aggregate})"
    return label


def _outcome_entries(actions: Sequence[RoutingAction], reason: str) -> list[str]:
    """The ``outcome``, ``queue`` and ``reason`` entries of an outcome."""
    queue = review_queue_of(actions)
    kind = KIND_QUEUE if queue else KIND_NO_REVIEW
    return [format_literal(kind), format_literal(queue), format_literal(reason)]


def to_decision_table(
    table: RoutingTable, *, include_disabled: bool = False
) -> DecisionTable:
    """``table`` as a decision table.

    Disabled rules are left out, which is what evaluation wants. Export passes
    ``include_disabled`` and flags them in the file.
    """
    rules = [rule for rule in table.rules if include_disabled or rule.enabled]

    # A column per distinct (expression, occurrence within a rule), in first use.
    columns: list[tuple[str, int]] = []
    samples: dict[tuple[str, int], RoutingCondition] = {}
    tests: dict[tuple[str, int], list[str]] = {}
    placed: list[dict[tuple[str, int], str]] = []
    for rule in rules:
        seen: dict[str, int] = {}
        entries: dict[tuple[str, int], str] = {}
        for condition in rule.conditions:
            expression = expression_for(condition)
            key = (expression, seen.get(expression, 0))
            seen[expression] = key[1] + 1
            if key not in samples:
                columns.append(key)
                samples[key] = condition
                tests[key] = []
            entries[key] = condition.test
            tests[key].append(condition.test)
        placed.append(entries)

    inputs = []
    for position, key in enumerate(columns, start=1):
        condition = samples[key]
        entry = routing_input(condition.input)
        is_list = entry is not None and entry.is_list
        inputs.append(
            TableInput(
                id=f"input_{position}",
                label=_column_label(entry, condition),
                expression=key[0],
                type_ref=_column_type(entry, condition.aggregate, tests[key]),
                quantifier=condition.aggregate
                if is_list and condition.aggregate in ("any", "all")
                else "",
            )
        )
    default = _outcome_entries(
        table.default_outcome.actions, table.default_outcome.reason
    )
    return DecisionTable(
        id=table.id,
        name=table.id,
        hit_policy=table.hit_policy,
        inputs=inputs,
        outputs=[
            TableOutput(
                id="output_outcome",
                name=OUTPUT_OUTCOME,
                type_ref="string",
                default=default[0],
            ),
            TableOutput(
                id="output_queue",
                name=OUTPUT_QUEUE,
                type_ref="string",
                default=default[1],
            ),
            TableOutput(
                id="output_reason",
                name=OUTPUT_REASON,
                type_ref="string",
                default=default[2],
            ),
        ],
        rules=[
            TableRule(
                id=rule.id,
                description=rule.label,
                input_entries=[entries.get(key, "-") for key in columns],
                output_entries=_outcome_entries(rule.actions, rule.reason),
            )
            for rule, entries in zip(rules, placed)
        ],
    )


def _refuse(message: str, construct: str) -> NoReturn:
    from idp_common.decision_tables.dmn import DmnImportError

    raise DmnImportError(message, construct=construct)


def _slug(value: str, fallback: str) -> str:
    """``value`` as a table or rule id: lower case, with other characters as ``-``."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", (value or "").strip().lower())
    slug = slug.lstrip("-_.")[:64].rstrip("-")
    return slug or fallback


def _string_output(entry: str, what: str, rule_id: str) -> str:
    try:
        value = parse_literal(entry)
    except UnaryTestSyntaxError:
        value = None
    if not isinstance(value, str):
        _refuse(
            f"Rule '{rule_id}': the {what} output must be a string in quotes, "
            f"found {entry!r}",
            "outputEntry",
        )
    return value


def from_decision_table(
    table: DecisionTable,
    *,
    table_attributes: Optional[dict[str, str]] = None,
    rule_attributes: Optional[dict[str, dict[str, str]]] = None,
    aggregation: str = "",
) -> RoutingTable:
    """A decision table read from DMN XML, as a routing table.

    The attributes are the foreign attributes the DMN reader returns. A file that
    has none (one drawn in a modeler) gets the default stage and engine, every rule
    enabled, and ids made from the file's own.

    It does not check queues, fields or policy rule ids: saving the profile does.

    Raises:
        DmnImportError: naming the construct, for a hit policy other than FIRST,
            an ``aggregation``, an input expression that is not a catalogue call,
            outputs other than ``outcome`` and ``queue`` with or without
            ``reason``, an outcome that is neither ``"queue"`` nor
            ``"no_review"``, a cell that is not a unary test, or more rules than a
            table may hold.
    """
    table_attributes = table_attributes or {}
    rule_attributes = rule_attributes or {}
    if table.hit_policy != "FIRST":
        _refuse(
            f"The decision table's hit policy is {table.hit_policy!r}; only FIRST "
            f"is supported",
            "hitPolicy",
        )
    if aggregation:
        _refuse(
            f"The decision table has the aggregation {aggregation!r}, which is "
            f"not supported",
            "aggregation",
        )
    if len(table.rules) > MAX_RULES:
        _refuse(
            f"The decision table has {len(table.rules)} rules; a routing table "
            f"holds at most {MAX_RULES}",
            "rule",
        )
    names = [output.name for output in table.outputs]
    if names not in (
        [OUTPUT_OUTCOME, OUTPUT_QUEUE],
        [OUTPUT_OUTCOME, OUTPUT_QUEUE, OUTPUT_REASON],
    ):
        _refuse(
            f"The decision table's outputs are {names}; a routing table has the "
            f"outputs '{OUTPUT_OUTCOME}', '{OUTPUT_QUEUE}' and '{OUTPUT_REASON}', "
            f"in that order, or '{OUTPUT_OUTCOME}' and '{OUTPUT_QUEUE}' alone",
            "output",
        )
    has_reason = len(names) == 3
    columns = []
    for column in table.inputs:
        try:
            columns.append(parse_expression(column.expression))
        except RoutingExpressionError as error:
            _refuse(
                f"The input expression {column.expression!r} is not a routing "
                f"input: {error}",
                "inputExpression",
            )

    def outcome_of(entries: list[str], rule_id: str) -> tuple[list[RoutingAction], str]:
        """The actions and the reason a row of output entries writes."""
        kind = _string_output(entries[0], OUTPUT_OUTCOME, rule_id)
        if kind not in (KIND_QUEUE, KIND_NO_REVIEW):
            _refuse(
                f"Rule '{rule_id}': the outcome output must be \"{KIND_QUEUE}\" or "
                f'"{KIND_NO_REVIEW}"',
                "outputEntry",
            )
        queue = _string_output(entries[1], OUTPUT_QUEUE, rule_id) if entries[1] else ""
        reason = (
            _string_output(entries[2], OUTPUT_REASON, rule_id)
            if has_reason and entries[2]
            else ""
        )
        if kind != KIND_QUEUE:
            return [], reason
        # A queue outcome that names no queue keeps its action, so that saving
        # the profile refuses it; without one it would read as "not reviewed".
        return queue_actions(queue) or [RoutingAction()], reason

    rules: list[RoutingRule] = []
    used: set[str] = set()
    for position, rule in enumerate(table.rules, start=1):
        attributes = rule_attributes.get(rule.id, {})
        rule_id = attributes.get(_ATTR_ID) or _slug(rule.id, f"rule-{position}")
        base, suffix = rule_id, 2
        while rule_id in used:
            rule_id = f"{base[:60]}-{suffix}"
            suffix += 1
        used.add(rule_id)
        if len(rule.input_entries) != len(columns) or len(rule.output_entries) != len(
            table.outputs
        ):
            _refuse(
                f"Rule '{rule.id}' does not have one entry per input and per output",
                "rule",
            )
        conditions = []
        for (name, path, aggregate), entry in zip(columns, rule.input_entries):
            try:
                tests = parse(entry)
            except UnaryTestSyntaxError as error:
                _refuse(
                    f"Rule '{rule.id}', input '{name}': the cell is not a test "
                    f"this release supports: {error}",
                    "inputEntry",
                )
            if tests.any_value:
                continue
            conditions.append(
                RoutingCondition(input=name, path=path, aggregate=aggregate, test=entry)
            )
        actions, reason = outcome_of(rule.output_entries, rule.id)
        rules.append(
            RoutingRule(
                id=rule_id,
                label=rule.description,
                enabled=attributes.get(_ATTR_ENABLED, "true").strip().lower()
                != "false",
                conditions=conditions,
                actions=actions,
                reason=reason,
            )
        )

    defaults = [output.default for output in table.outputs]
    default_outcome = RoutingDefaultOutcome()
    if defaults[0]:
        actions, reason = outcome_of(defaults, "default outcome")
        default_outcome = RoutingDefaultOutcome(actions=actions, reason=reason)
    return RoutingTable(
        id=table_attributes.get(_ATTR_ID) or _slug(table.id, DEFAULT_TABLE_ID),
        stage=table_attributes.get(_ATTR_STAGE) or STAGE_AFTER_POLICY_RULE_VALIDATION,
        hit_policy=table.hit_policy,
        engine=table_attributes.get(_ATTR_ENGINE) or ENGINE_BUILTIN,
        rules=rules,
        default_outcome=default_outcome,
    )


def _xml_id(value: str, prefix: str) -> str:
    """``value`` as an XML id: itself when it is one, else made into one.

    An XML id starts with a letter or ``_`` and holds letters, digits, ``_``,
    ``.`` and ``-``. Any other character becomes ``_``, and an id that does not
    start as one must gets ``prefix``.
    """
    if _XML_ID_RE.match(value):
        return value
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return cleaned if _XML_ID_RE.match(cleaned) else f"{prefix}{cleaned}"


def export_xml(routing: RoutingConfig, *, dmn_version: str = "1.5") -> str:
    """The profile's routing table as DMN XML.

    Disabled rules are written and flagged, so nothing is lost. ``dmn_version`` is
    ``"1.5"`` or ``"1.3"``; the 1.3 form is what Camunda Modeler and dmn-js open.
    ``max_consecutive_placements`` is a profile setting, not part of the table, and
    is not written.

    Raises:
        DecisionTableError: the section does not hold exactly one table, or the
            table cannot be written (see ``decision_tables.dmn``).
    """
    from idp_common.decision_tables.dmn import write_decision_table

    if len(routing.tables) != 1:
        raise DecisionTableError(
            f"The routing section must hold exactly one table to be exported; it "
            f"holds {len(routing.tables)}"
        )
    table = routing.tables[0]
    decision = to_decision_table(table, include_disabled=True)
    table_attributes = {_ATTR_STAGE: table.stage, _ATTR_ENGINE: table.engine}
    # An id is also written as idp:id wherever reading the XML id back would not
    # give it again: it is not a valid XML id, or it is not in id form.
    decision.id = _xml_id(table.id, "table_")
    if _slug(decision.id, DEFAULT_TABLE_ID) != table.id:
        table_attributes[_ATTR_ID] = table.id
    rule_attributes: dict[str, dict[str, str]] = {}
    taken: set[str] = set()
    for position, (rule, written) in enumerate(
        zip(table.rules, decision.rules), start=1
    ):
        attributes = {}
        written.id = _xml_id(rule.id, "rule_") if rule.id else f"rule_{position}"
        # Two ids can become one XML id ("a b" and "a_b"); keep them apart.
        while written.id in rule_attributes or written.id in taken:
            written.id = f"{written.id}_{position}"
        taken.add(written.id)
        if _slug(written.id, f"rule-{position}") != rule.id:
            attributes[_ATTR_ID] = rule.id
        if not rule.enabled:
            attributes[_ATTR_ENABLED] = "false"
        if attributes:
            rule_attributes[written.id] = attributes
    return write_decision_table(
        decision,
        dmn_version=dmn_version,
        extra_table_attributes=table_attributes,
        rule_attributes=rule_attributes,
        namespace_prefixes={ROUTING_PREFIX: ROUTING_NAMESPACE},
        target_namespace=ROUTING_NAMESPACE,
    )


def import_xml(xml: Union[str, bytes]) -> RoutingConfig:
    """A routing section holding the one table in a DMN file.

    ``max_consecutive_placements`` is the default: a DMN file does not carry it.

    Raises:
        DmnImportError: naming the construct. See ``decision_tables.dmn.read_dmn``
            for what any DMN file is refused for, and :func:`from_decision_table`
            for what a routing table is refused for.
    """
    from idp_common.decision_tables.dmn import read_dmn

    document = read_dmn(xml)
    table = from_decision_table(
        document.table,
        table_attributes=document.table_attributes,
        rule_attributes=document.rule_attributes,
        aggregation=document.aggregation,
    )
    return RoutingConfig(tables=[table])
