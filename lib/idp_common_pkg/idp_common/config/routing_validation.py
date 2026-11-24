# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the ``routing`` section of a profile may hold, checked when it is saved.

The routing models accept anything that parses, so a revision a document is pinned
to always loads. :func:`validate_routing` is the other half: it runs at the write
boundary (``ConfigurationManager.handle_update_custom_configuration``) and refuses
a table that could not be evaluated as its author means it. It never runs on a
read.

:data:`ROUTING_INPUTS` is the catalogue of routing inputs as data. The runtime that
computes the inputs (``review_queues.routing_inputs``) and the web UI's rule
editor read the same catalogue.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, List, NamedTuple, NoReturn, Optional, Sequence

from idp_common.config.routing_models import (
    ACTION_REVIEW_QUEUE,
    HIT_POLICY_FIRST,
    STAGE_AFTER_POLICY_RULE_VALIDATION,
    RoutingAction,
    RoutingCondition,
    RoutingRule,
    RoutingTable,
    review_queue_of,
)
from idp_common.config.routing_reason import (
    KIND_CONDITION_VALUE,
    KIND_INPUT,
    Placeholder,
    ReasonSyntaxError,
    parse_reason,
)

logger = logging.getLogger(__name__)

MAX_TABLES = 1
MAX_RULES = 100
MAX_CONDITIONS = 8
MAX_LABEL_BYTES = 200
MAX_PATH_BYTES = 128
MAX_TEST_BYTES = 256
MAX_SECTION_BYTES = 64 * 1024
#: A reason as written, and how many placeholders it may hold.
MAX_REASON_BYTES = 500
MAX_REASON_PLACEHOLDERS = 10

#: Table and rule ids.
ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_ID_RE = re.compile(ID_PATTERN)

AGGREGATES = ("any", "all", "count")

#: Input value types, as the catalogue and the checks below name them.
TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
#: The type depends on the field the path names.
TYPE_FIELD = "field"

#: What an input's ``path`` names.
PATH_NONE = ""
PATH_FIELD = "field"
PATH_POLICY_RULE = "policy_rule"
PATH_POLICY_TYPE = "policy_type"
PATH_CLASS = "class"

#: The values the rule editor offers for an input that has a fixed set of them.
#: The verdicts are those of ``rule_validation.orchestrator.VERDICTS``, restated
#: here because importing ``rule_validation`` loads the solver.
VERDICT_CHOICES = ("pass", "fail", "not_found", "unknown")
MODE_CHOICES = ("pipeline", "bda")

#: Unary-test literal kinds (``decision_tables.sfeel``) each value type accepts.
_LITERALS_FOR_TYPE = {
    TYPE_STRING: frozenset({"string"}),
    TYPE_NUMBER: frozenset({"number"}),
    TYPE_BOOLEAN: frozenset({"boolean"}),
}
#: JSON Schema ``type`` to the literal kinds a test on such a field may use. A
#: string field may hold a date, so a date literal fits it.
_LITERALS_FOR_SCHEMA_TYPE = {
    "string": frozenset({"string", "date"}),
    "number": frozenset({"number"}),
    "integer": frozenset({"number"}),
    "boolean": frozenset({"boolean"}),
}


@dataclass(frozen=True)
class RoutingInput:
    """One routing input: what a condition can test."""

    name: str
    label: str
    #: ``string``, ``number``, ``boolean`` or ``field`` (the field's own type).
    type: str
    #: The condition must carry a ``path``.
    needs_path: bool
    #: The value is a list, so ``aggregate`` applies.
    is_list: bool
    #: What the path names: a field, a policy rule id, a policy type or a class.
    path_kind: str = PATH_NONE
    #: The path may be left empty (``failed_policy_rule_count`` over all policies).
    path_optional: bool = False
    #: The values a guided control offers; empty when the value is free.
    choices: tuple[str, ...] = ()


#: Every routing input, in the order the rule editor offers them.
ROUTING_INPUTS: tuple[RoutingInput, ...] = (
    RoutingInput("document_class", "Document class", TYPE_STRING, False, False),
    RoutingInput("section_classes", "Section classes", TYPE_STRING, False, True),
    RoutingInput(
        "class_present", "Class is present", TYPE_BOOLEAN, True, False, PATH_CLASS
    ),
    RoutingInput("page_count", "Page count", TYPE_NUMBER, False, False),
    RoutingInput("section_count", "Section count", TYPE_NUMBER, False, False),
    RoutingInput(
        "confidence_alert_count", "Confidence alert count", TYPE_NUMBER, False, False
    ),
    RoutingInput(
        "missing_required_field_count",
        "Missing required field count",
        TYPE_NUMBER,
        False,
        False,
    ),
    RoutingInput(
        "extraction_failed_count",
        "Failed extraction count",
        TYPE_NUMBER,
        False,
        False,
    ),
    RoutingInput(
        "route_missing_required_fields",
        "Route missing required fields (setting)",
        TYPE_BOOLEAN,
        False,
        False,
    ),
    RoutingInput("field_value", "Field value", TYPE_FIELD, True, True, PATH_FIELD),
    RoutingInput(
        "policy_rule_verdict",
        "Policy rule verdict",
        TYPE_STRING,
        True,
        False,
        PATH_POLICY_RULE,
        choices=VERDICT_CHOICES,
    ),
    RoutingInput(
        "failed_policy_rule_count",
        "Failed policy rule count",
        TYPE_NUMBER,
        False,
        False,
        PATH_POLICY_TYPE,
        True,
    ),
    RoutingInput("reviewed", "Reviewed", TYPE_BOOLEAN, False, False),
    RoutingInput(
        "processing_mode",
        "Processing mode",
        TYPE_STRING,
        False,
        False,
        choices=MODE_CHOICES,
    ),
)

_INPUTS_BY_NAME = {
    routing_input.name: routing_input for routing_input in ROUTING_INPUTS
}


def routing_input(name: str) -> Optional[RoutingInput]:
    """The catalogue entry for ``name``, or ``None`` when there is none."""
    return _INPUTS_BY_NAME.get(name)


def policy_input(name: str) -> bool:
    """True when the input reads a result of policy rule validation."""
    entry = routing_input(name)
    return entry is not None and entry.path_kind in (
        PATH_POLICY_RULE,
        PATH_POLICY_TYPE,
    )


class QueueInfo(NamedTuple):
    """What validation needs to know about a production review queue."""

    id: str
    name: str
    enabled: bool


#: Returns the production queue with that id, or ``None`` when there is none
#: (the queue does not exist, or it is a Test Studio queue).
QueueLookup = Callable[[str], Optional[QueueInfo]]


def _refuse(
    table_id: str, problem: str, *, rule: str = "", where: str = ""
) -> NoReturn:
    """Raise, naming the table and, when given, the rule and what in it."""
    from idp_common.config.configuration_manager import ConfigurationValidationError

    parts = [f"Routing table '{table_id}'"]
    if rule:
        parts.append(f"rule '{rule}'")
    if where:
        parts.append(where)
    raise ConfigurationValidationError(f"{', '.join(parts)}: {problem}")


def _input_part(condition: RoutingCondition) -> str:
    path = f" {condition.path}" if condition.path else ""
    return f"input '{condition.input}{path}'"


def _size(value: str) -> int:
    return len(value.encode("utf-8"))


def split_field_path(path: str) -> tuple[str, str]:
    """``"Invoice:Total"`` as ``("Invoice", "Total")``; no class gives ``""``."""
    if ":" in path:
        class_name, _, field_path = path.partition(":")
        return class_name.strip(), field_path.strip()
    return "", path.strip()


def class_names(classes: Any) -> List[str]:
    """The document class names of a configuration's ``classes`` list."""
    from idp_common.config.schema_constants import X_AWS_IDP_DOCUMENT_TYPE

    names = []
    for schema in classes or []:
        declared = (
            schema.get(X_AWS_IDP_DOCUMENT_TYPE) if isinstance(schema, dict) else None
        )
        if isinstance(declared, str) and declared:
            names.append(declared)
    return names


def _class_problem(config: Any, name: str) -> str:
    """Why ``name`` is not a class of the configuration; empty when it is one.

    Matched without regard to case, as a section's class is at run time.
    """
    names = class_names(getattr(config, "classes", None))
    if name.strip().lower() in {known.lower() for known in names}:
        return ""
    known = ", ".join(sorted(names)) or "none"
    return f"'{name}' is not a class of this profile (classes: {known})"


def _field_schema(class_schema: Any, field_path: str) -> tuple[Optional[dict], str]:
    """The schema of the field at ``field_path``, or ``(None, problem)``.

    Walks ``properties`` and ``items``, resolving ``$ref``. A segment that names
    a list must carry an index to go further (``Lines[0].Amount``): a path with
    no index stops at the list and reads nothing below it at run time.
    """
    from idp_common.config.schema_constants import SCHEMA_ITEMS, SCHEMA_PROPERTIES
    from idp_common.config.schema_utils import deref_schema
    from idp_common.field_paths import FieldPathError, parse_path
    from idp_common.schema.multi_instance import effective_class_schema

    try:
        components = parse_path(field_path)
    except FieldPathError as error:
        return None, str(error)
    root = effective_class_schema(class_schema)
    node = deref_schema(root, root)
    walked: List[str] = []
    for position, component in enumerate(components):
        properties = node.get(SCHEMA_PROPERTIES)
        child = properties.get(component.key) if isinstance(properties, dict) else None
        if not isinstance(child, dict):
            at = ".".join(walked) or "the class"
            return None, f"'{component.key}' is not a field of {at}"
        node = deref_schema(child, root)
        walked.append(component.text)
        for _ in component.indices:
            items = node.get(SCHEMA_ITEMS)
            if not isinstance(items, dict):
                return None, f"'{component.key}' is not a list, so it has no row index"
            node = deref_schema(items, root)
        is_last = position == len(components) - 1
        if (
            not is_last
            and not component.indices
            and isinstance(node.get(SCHEMA_ITEMS), dict)
        ):
            return None, (
                f"'{component.key}' is a list; name a row to read a field in it, "
                f"for example {component.key}[0].{components[position + 1].key}"
            )
    return node, ""


def _field_literals(config: Any, path: str) -> tuple[Optional[frozenset], str]:
    """The literal kinds a test on the field may use, or ``(None, problem)``.

    An empty set means the field's schema states no scalar type, so any literal
    is accepted.
    """
    from idp_common.config.schema_constants import SCHEMA_TYPE
    from idp_common.config.schema_utils import find_class_schema

    classes = getattr(config, "classes", None) or []
    class_name, field_path = split_field_path(path)
    if not field_path:
        return None, "the path names no field; write Class:field or field"
    if class_name:
        class_schema = find_class_schema(class_name, classes)
        if class_schema is None:
            known = ", ".join(sorted(class_names(classes))) or "none"
            return (
                None,
                f"'{class_name}' is not a class of this profile (classes: {known})",
            )
        candidates = [class_schema]
    else:
        candidates = [schema for schema in classes if isinstance(schema, dict)]
        if not candidates:
            return None, "this profile has no classes, so the field cannot be checked"
    found: List[dict] = []
    problem = ""
    for class_schema in candidates:
        node, why = _field_schema(class_schema, field_path)
        if node is not None:
            found.append(node)
        elif not problem:
            problem = why
    if not found:
        if class_name:
            return None, problem
        return None, f"no class of this profile has the field '{field_path}'"
    kinds = set()
    for node in found:
        schema_type = node.get(SCHEMA_TYPE)
        if isinstance(schema_type, str):
            kinds.add(_LITERALS_FOR_SCHEMA_TYPE.get(schema_type, frozenset()))
    # One type across every class that has the field; otherwise accept any.
    return (next(iter(kinds)) if len(kinds) == 1 else frozenset()), ""


def _policy_type_of(policy_class: Any) -> Optional[str]:
    """A policy class's type, tolerating the legacy discriminator.

    The two-key lookup of ``rule_validation.service._policy_type_of``, restated
    here because importing ``rule_validation`` loads the solver.
    """
    from idp_common.config.schema_constants import (
        X_AWS_IDP_POLICY_TYPE,
        X_AWS_IDP_RULE_TYPE,
    )

    if not isinstance(policy_class, dict):
        return None
    return policy_class.get(X_AWS_IDP_POLICY_TYPE) or policy_class.get(
        X_AWS_IDP_RULE_TYPE
    )


def policy_rule_ids(config: Any) -> List[str]:
    """Every policy rule id of a configuration: ``<policy type>.<rule key>``."""
    ids = []
    for policy_class in getattr(config, "policy_classes", None) or []:
        policy_type = _policy_type_of(policy_class)
        rule_properties = (
            policy_class.get("rule_properties")
            if isinstance(policy_class, dict)
            else None
        )
        if policy_type and isinstance(rule_properties, dict):
            ids.extend(f"{policy_type}.{key}" for key in rule_properties)
    return ids


def policy_types(config: Any) -> List[str]:
    """Every policy type of a configuration."""
    return [
        policy_type
        for policy_type in (
            _policy_type_of(policy_class)
            for policy_class in getattr(config, "policy_classes", None) or []
        )
        if policy_type
    ]


#: What a save says about an outcome written as one ``outcome``: the form to write.
_ACTIONS_FORM = (
    "the outcome is a list of actions: write actions: [{type: review_queue, "
    "queue: <id>}], or actions: [] for no review"
)


def refuse_single_outcome_shape(raw: Any) -> None:
    """Refuse a ``routing`` section that writes an outcome as one ``outcome``.

    ``raw`` is the section as the write carries it, before the models read it. A
    routing rule that holds ``outcome``, and a ``default_outcome`` that holds
    ``kind`` or ``queue``, name a single outcome where the models hold a list of
    ``actions``. No release reads or writes that shape: it reaches a save only
    in a section written by hand, or in one a development build saved. The
    models drop those keys, so the routing rule would be saved as "not reviewed"
    without a word; the write is refused instead. Anything that is not that shape
    is left to :func:`validate_routing`.

    Raises:
        ConfigurationValidationError: naming the table and the routing rule.
    """
    tables = raw.get("tables") if isinstance(raw, dict) else None
    if not isinstance(tables, list):
        return
    for index, table in enumerate(tables, start=1):
        if not isinstance(table, dict):
            continue
        table_id = table.get("id")
        named = table_id if isinstance(table_id, str) and table_id else f"#{index}"
        rules = table.get("rules")
        for position, rule in enumerate(
            rules if isinstance(rules, list) else [], start=1
        ):
            if isinstance(rule, dict) and "outcome" in rule:
                rule_id = rule.get("id")
                _refuse(
                    named,
                    _ACTIONS_FORM,
                    rule=rule_id
                    if isinstance(rule_id, str) and rule_id
                    else f"#{position}",
                )
        default = table.get("default_outcome")
        if isinstance(default, dict) and ("kind" in default or "queue" in default):
            _refuse(named, _ACTIONS_FORM, where="default outcome")


def _check_actions(
    table: RoutingTable,
    actions: Sequence[RoutingAction],
    *,
    rule: str,
    queue_lookup: Optional[QueueLookup],
) -> None:
    """Refuse actions a run could not carry out. ``rule`` is empty for the
    default outcome."""
    scope = "" if rule else "default outcome"
    for number, action in enumerate(actions, start=1):
        if action.type != ACTION_REVIEW_QUEUE:
            _refuse(
                table.id,
                f"the action type '{action.type}' is not supported; the only "
                f"action type is '{ACTION_REVIEW_QUEUE}'",
                rule=rule,
                where=", ".join(part for part in (scope, f"action {number}") if part),
            )
    if len(actions) > 1:
        _refuse(
            table.id,
            f"a document goes to one review queue; this outcome has "
            f"{len(actions)} {ACTION_REVIEW_QUEUE} actions",
            rule=rule,
            where=scope,
        )
    if not actions:
        return
    from idp_common.review_queues.queue_defs import QUEUE_ID_PATTERN

    queue = actions[0].queue
    if not queue:
        _refuse(
            table.id,
            f"the {ACTION_REVIEW_QUEUE} action names no queue",
            rule=rule,
            where=scope,
        )
    if not re.match(QUEUE_ID_PATTERN, queue):
        _refuse(
            table.id,
            f"'{queue}' is not a review queue id",
            rule=rule,
            where=scope,
        )
    if queue_lookup is not None and queue_lookup(queue) is None:
        _refuse(
            table.id,
            f"the review queue '{queue}' does not exist, or it is not a "
            f"production queue",
            rule=rule,
            where=scope,
        )


def _known_inputs() -> str:
    return ", ".join(entry.name for entry in ROUTING_INPUTS)


def _input_placeholder_problem(config: Any, placeholder: Placeholder) -> str:
    """Why an input placeholder cannot be filled; empty when it can."""
    entry = routing_input(placeholder.input)
    if entry is None:
        return (
            f"'{placeholder.input}' is not a routing input (inputs: {_known_inputs()})"
        )
    path = placeholder.path
    if entry.needs_path and not path:
        return "this input needs a path, for example {field_value:Invoice:Total}"
    if path and entry.path_kind == PATH_NONE:
        return "this input takes no path"
    if entry.path_kind == PATH_FIELD:
        literals, problem = _field_literals(config, path)
        return problem if literals is None else ""
    if entry.path_kind == PATH_POLICY_RULE:
        if path not in policy_rule_ids(config):
            return (
                "this policy rule id is not in the Policy Schema; an id is "
                "<policy type>.<rule key>"
            )
    elif entry.path_kind == PATH_POLICY_TYPE and path:
        if path not in policy_types(config):
            return "this policy type is not in the Policy Schema"
    elif entry.path_kind == PATH_CLASS:
        return _class_problem(config, path)
    return ""


def _check_reason(
    config: Any,
    table: RoutingTable,
    reason: str,
    *,
    rule: Optional[RoutingRule],
) -> None:
    """Refuse a reason that could not be shown as its author means it.

    ``rule`` is the routing rule the reason belongs to, or ``None`` for the
    default outcome's.
    """
    if not reason:
        return
    rule_id = rule.id if rule is not None else ""
    scope = "" if rule is not None else "default outcome"
    actions = rule.actions if rule is not None else table.default_outcome.actions

    def refuse(problem: str, where: str = scope) -> NoReturn:
        _refuse(table.id, problem, rule=rule_id, where=where)

    if _size(reason) > MAX_REASON_BYTES:
        refuse(f"the reason is longer than {MAX_REASON_BYTES} bytes")
    if not review_queue_of(actions):
        refuse(
            "a reason is shown to a reviewer, and this outcome sends the document "
            "to no review queue; remove the reason or choose a queue"
        )
    try:
        placeholders = parse_reason(reason)
    except ReasonSyntaxError as error:
        refuse(str(error), f"{scope} reason".strip())
    if len(placeholders) > MAX_REASON_PLACEHOLDERS:
        refuse(
            f"the reason has {len(placeholders)} placeholders; the limit is "
            f"{MAX_REASON_PLACEHOLDERS}"
        )
    for placeholder in placeholders:
        problem = ""
        if placeholder.kind == KIND_INPUT:
            problem = _input_placeholder_problem(config, placeholder)
        elif placeholder.kind == KIND_CONDITION_VALUE:
            if rule is None:
                problem = (
                    "a condition value is not available in the default outcome's reason"
                )
            elif placeholder.index > len(rule.conditions):
                problem = (
                    f"the placeholder names condition {placeholder.index}, and "
                    f"the routing rule has {len(rule.conditions)} conditions"
                )
        # The confidence threshold is the third kind, and is always available.
        if problem:
            refuse(
                problem,
                f"{scope} reason placeholder '{placeholder.text}'".strip(),
            )


def _check_condition(
    config: Any, table: RoutingTable, rule_id: str, condition: RoutingCondition
) -> None:
    from idp_common.decision_tables.models import UnaryTestSyntaxError
    from idp_common.decision_tables.sfeel import parse

    def refuse(problem: str) -> NoReturn:
        _refuse(table.id, problem, rule=rule_id, where=_input_part(condition))

    entry = routing_input(condition.input)
    if entry is None:
        refuse(
            f"'{condition.input}' is not a routing input (inputs: {_known_inputs()})"
        )
    if entry.needs_path and not condition.path:
        refuse("this input needs a path")
    if condition.path and entry.path_kind == PATH_NONE:
        refuse("this input takes no path")

    if condition.aggregate not in AGGREGATES:
        refuse(
            f"the aggregate '{condition.aggregate}' is not supported; use one of "
            f"{', '.join(AGGREGATES)}"
        )
    if not entry.is_list and condition.aggregate != "any":
        refuse(
            f"the aggregate '{condition.aggregate}' applies to an input that is a "
            f"list; this input is a single value"
        )

    try:
        tests = parse(condition.test)
    except UnaryTestSyntaxError as error:
        refuse(f"the test is not valid: {error}")

    allowed: frozenset = frozenset()
    if entry.path_kind == PATH_FIELD:
        field_literals, problem = _field_literals(config, condition.path)
        if field_literals is None:
            refuse(problem)
        allowed = field_literals
    elif entry.path_kind == PATH_POLICY_RULE:
        if condition.path not in policy_rule_ids(config):
            refuse(
                "this policy rule id is not in the Policy Schema; an id is "
                "<policy type>.<rule key>"
            )
    elif entry.path_kind == PATH_POLICY_TYPE and condition.path:
        if condition.path not in policy_types(config):
            refuse("this policy type is not in the Policy Schema")
    elif entry.path_kind == PATH_CLASS:
        problem = _class_problem(config, condition.path)
        if problem:
            refuse(problem)
    if entry.type in _LITERALS_FOR_TYPE:
        allowed = _LITERALS_FOR_TYPE[entry.type]
    if condition.aggregate == "count":
        allowed = _LITERALS_FOR_TYPE[TYPE_NUMBER]

    unfit = sorted(tests.literal_kinds() - allowed) if allowed else []
    if unfit:
        refuse(
            f"the test compares with a {' and a '.join(unfit)}, but the value is "
            f"a {' or a '.join(sorted(allowed))}"
        )


def validate_routing(
    config: Any, *, queue_lookup: Optional[QueueLookup] = None
) -> None:
    """Refuse a configuration whose ``routing`` section could not be evaluated.

    Checks, in order: the limits; the stage, hit policy and engine; each
    outcome's actions and reason; each condition's input, path, aggregate and
    test; that a ``field_value`` path exists in the class schema, a policy rule
    id in the Policy Schema and a ``class_present`` class among the classes; and,
    when ``queue_lookup`` is given, that every queue exists and is a production
    queue. A disabled queue is accepted: the runtime falls back to the Default
    queue. A reason is checked for its size, its placeholders, and that its
    outcome names a review queue for a reviewer to read it in.

    Args:
        config: The full ``IDPConfig`` about to be saved.
        queue_lookup: Returns a production queue by id, or ``None``. Without one,
            queue ids are checked for their form only.

    Raises:
        ConfigurationValidationError: naming the table, the rule, and the input
            or the reason placeholder.
    """
    from idp_common.decision_tables.engine import ENGINES

    routing = config.routing
    section_bytes = _size(json.dumps(routing.model_dump(), separators=(",", ":")))
    if len(routing.tables) != MAX_TABLES:
        from idp_common.config.configuration_manager import (
            ConfigurationValidationError,
        )

        raise ConfigurationValidationError(
            f"The routing section must hold exactly one table; it holds "
            f"{len(routing.tables)}"
        )
    table = routing.tables[0]
    table_id = table.id
    if not _ID_RE.match(table_id):
        _refuse(
            table_id,
            "the table id must be 1 to 64 characters: lower-case letters, digits, "
            "'_', '.' and '-', starting with a letter or a digit",
        )
    if section_bytes > MAX_SECTION_BYTES:
        _refuse(
            table_id,
            f"the routing section is {section_bytes} bytes; the limit is "
            f"{MAX_SECTION_BYTES} bytes",
        )
    if len(table.rules) > MAX_RULES:
        _refuse(
            table_id,
            f"the table has {len(table.rules)} rules; the limit is {MAX_RULES}",
        )
    seen: set[str] = set()
    for position, rule in enumerate(table.rules, start=1):
        if not _ID_RE.match(rule.id):
            _refuse(
                table_id,
                "the rule id must be 1 to 64 characters: lower-case letters, "
                "digits, '_', '.' and '-', starting with a letter or a digit",
                # A rule with no id is named by its position in the table.
                rule=rule.id or f"#{position}",
            )
        if rule.id in seen:
            _refuse(table_id, "the rule id is used more than once", rule=rule.id)
        seen.add(rule.id)
        if _size(rule.label) > MAX_LABEL_BYTES:
            _refuse(
                table_id,
                f"the label is longer than {MAX_LABEL_BYTES} bytes",
                rule=rule.id,
            )
        if len(rule.conditions) > MAX_CONDITIONS:
            _refuse(
                table_id,
                f"the rule has {len(rule.conditions)} conditions; the limit is "
                f"{MAX_CONDITIONS}",
                rule=rule.id,
            )
        for condition in rule.conditions:
            if _size(condition.path) > MAX_PATH_BYTES:
                _refuse(
                    table_id,
                    f"the path is longer than {MAX_PATH_BYTES} bytes",
                    rule=rule.id,
                    where=f"input '{condition.input}'",
                )
            if _size(condition.test) > MAX_TEST_BYTES:
                _refuse(
                    table_id,
                    f"the test is longer than {MAX_TEST_BYTES} bytes",
                    rule=rule.id,
                    where=_input_part(condition),
                )

    if table.stage != STAGE_AFTER_POLICY_RULE_VALIDATION:
        _refuse(
            table_id,
            f"the stage '{table.stage}' is not supported; the only stage is "
            f"'{STAGE_AFTER_POLICY_RULE_VALIDATION}'",
        )
    if table.hit_policy != HIT_POLICY_FIRST:
        _refuse(
            table_id,
            f"the hit policy '{table.hit_policy}' is not supported; the only hit "
            f"policy is '{HIT_POLICY_FIRST}'",
        )
    if table.engine not in ENGINES:
        _refuse(
            table_id,
            f"the engine '{table.engine}' is not registered; registered: "
            f"{', '.join(sorted(ENGINES))}",
        )

    _check_actions(
        table, table.default_outcome.actions, rule="", queue_lookup=queue_lookup
    )
    _check_reason(config, table, table.default_outcome.reason, rule=None)
    for rule in table.rules:
        _check_actions(table, rule.actions, rule=rule.id, queue_lookup=queue_lookup)
        _check_reason(config, table, rule.reason, rule=rule)
        for condition in rule.conditions:
            _check_condition(config, table, rule.id, condition)


def _policy_placeholders(reason: str) -> List[str]:
    """The placeholders of ``reason`` that show a policy rule result, as written.

    A reason that does not parse has none: a save refuses it, and a stored one is
    not this function's to report.
    """
    try:
        placeholders = parse_reason(reason)
    except ReasonSyntaxError:
        return []
    found: List[str] = []
    for placeholder in placeholders:
        if (
            placeholder.kind == KIND_INPUT
            and policy_input(placeholder.input)
            and placeholder.text not in found
        ):
            found.append(placeholder.text)
    return found


def routing_warnings(config: Any) -> List[str]:
    """What a saved ``routing`` section will not do, though the save is accepted.

    A routing rule that tests a policy rule result never matches while policy
    rule validation is off for the profile or the profile has no policies, and a
    reason that shows one shows nothing. One warning for each enabled routing
    rule that does either, and one for the default outcome's reason. None when
    policy rule validation runs.

    Pure: it reads ``config`` only. It never raises.
    """
    warnings: List[str] = []
    try:
        from idp_common.config.feature_flags import policy_rule_validation_enabled

        if policy_rule_validation_enabled(config):
            return warnings
        routing = getattr(config, "routing", None)
        for table in getattr(routing, "tables", None) or []:
            prefix = f"Routing table '{table.id}'"
            for rule in table.rules:
                if not rule.enabled:
                    continue
                tested: List[str] = []
                for condition in rule.conditions:
                    named = f"{condition.input} {condition.path}".strip()
                    if policy_input(condition.input) and named not in tested:
                        tested.append(named)
                shown = _policy_placeholders(rule.reason)
                if tested:
                    warnings.append(
                        f"{prefix}, rule '{rule.id}': this routing rule tests a "
                        f"policy rule result ({', '.join(tested)}), and policy "
                        f"rule validation is off for this profile or it has no "
                        f"policies, so the routing rule never matches."
                    )
                elif shown:
                    warnings.append(
                        f"{prefix}, rule '{rule.id}': the reason shows a policy "
                        f"rule result ({', '.join(shown)}), which is empty while "
                        f"policy rule validation is off."
                    )
            shown = _policy_placeholders(table.default_outcome.reason)
            if shown:
                warnings.append(
                    f"{prefix}, default outcome: the reason shows a policy rule "
                    f"result ({', '.join(shown)}), which is empty while policy "
                    f"rule validation is off."
                )
    except Exception:  # noqa: BLE001 — a warning must never fail a save
        logger.warning("event=routing_warnings_failed", exc_info=True)
    return warnings
