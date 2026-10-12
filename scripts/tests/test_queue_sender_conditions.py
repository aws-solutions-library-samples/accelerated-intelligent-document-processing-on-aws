# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every compare-and-set the malware gate writes names attributes that exist.

``update_document_status`` registers an expression attribute NAME only from an
``extra_attributes`` key WITHOUT a ``':'`` prefix — a ``':''``-prefixed key is a
condition *value*. So a ``ConditionExpression`` naming something nothing
registers is accepted by the service, rejected by DynamoDB as a
``ValidationException``, and then swallowed: a caller writing a compare-and-set
necessarily wraps it in ``except`` to handle the condition not holding, and so
reads a call that never ran as a condition that failed.

On the revert-after-SQS-failure path the cost of that is a document left
``QUEUED`` with nothing in the queue and no route back, because the retried
verdict's own condition wants ``MALWARE_SCANNING`` and reads ``QUEUED`` as a
duplicate delivery.

⚠️ **Asserting the condition *string* does not catch it.** A test that compares
the expression against an expected literal, with ``document_service`` mocked,
passes on a condition DynamoDB would reject — it pins the text and proves nothing
about whether the write can run. That is why this check reads the gate's
**source** and covers every call site rather than the ones somebody remembered to
assert on.

The companion guard lives in the service itself
(``TestAConditionCannotReferenceAnUnregisteredName`` in
``lib/idp_common_pkg/tests/unit/dynamodb/test_service_malware_scan.py``): that
one answers "does the service refuse a bad condition", this one answers "does the
gate pass one". Both are wanted, because the service guard only fires on a path a
test actually executes, and no test drives the real service from the gate.

This file lives here rather than in ``lib/idp_common_pkg`` deliberately:
``idp_common_pkg`` is independently installable and its suite must not depend on
the repository layout above it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "src" / "lambda" / "queue_sender" / "index.py"

#: Registered by ``update_document_status`` for every call, so a condition may
#: name these with no ``extra_attributes`` entry. Kept in step with
#: ``expression_names`` in ``idp_common/dynamodb/service.py``.
ALWAYS_REGISTERED = {"#ObjectStatus", "#WorkflowStatus", "#WorkflowExecutionArn"}


def _enclosing_functions(tree):
    """Map each node to the function body it sits in.

    Needed because neither the condition nor the registered names are
    necessarily at the call: the gate's admission call builds its condition in a
    local with ``+=`` and adds registrations with subscript assignment, several
    lines earlier. Resolving only call-site literals would read that call as
    unverifiable — and it is the more important of the two.
    """
    owners = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            owners.setdefault(node, func)
    return owners


def _conditional_status_calls():
    """Every ``update_document_status`` call in the gate that passes a condition.

    Returns ``(lineno, condition_node, extra_attributes_node, enclosing_function)``.
    """
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    owners = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute) and func.attr == "update_document_status"
        ):
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        condition = kwargs.get("condition_expression")
        if condition is None:
            continue
        found.append(
            (
                node.lineno,
                condition,
                kwargs.get("extra_attributes"),
                owners.get(node),
            )
        )
    return found


def _resolve_condition(node, scope):
    """The condition's text, resolving a local variable built in ``scope``.

    For a ``Name`` the result is the union of every literal assigned or
    ``+=``-appended to it in that function, which over-approximates: a name
    added on only one branch still has to be registered. That is the safe
    direction — the alternative reports nothing.
    """
    direct = _literal_text(node)
    if direct is not None:
        return direct
    if not (isinstance(node, ast.Name) and scope is not None):
        return None

    parts = []
    for statement in ast.walk(scope):
        targets = []
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AugAssign) and isinstance(statement.op, ast.Add):
            targets = [statement.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == node.id:
                text = _literal_text(statement.value)
                if text is None:
                    return None
                parts.append(text)
    return " ".join(parts) if parts else None


def _names_registered_in_scope(extra_node, scope):
    """Registered names from the call's dict plus any added by subscript.

    ``extra_attributes["MalwareScanVersionId"] = ...`` is how the gate registers
    the version name on the branch that pins it, so reading the dict literal
    alone would miss it.
    """
    names = _registered_names(extra_node)
    if not (isinstance(extra_node, ast.Name) or scope is None):
        pass
    if scope is None:
        return names

    target_id = extra_node.id if isinstance(extra_node, ast.Name) else None
    for statement in ast.walk(scope):
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if not isinstance(target, ast.Subscript):
                continue
            holder = target.value
            if target_id is not None and not (
                isinstance(holder, ast.Name) and holder.id == target_id
            ):
                continue
            key = target.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and not key.value.startswith(":")
            ):
                names.add(f"#{key.value}")
        # A dict literal assigned to the same local also registers names.
        if target_id is not None:
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == target_id:
                    names |= _registered_names(statement.value)
    return names


def _literal_text(node):
    """The condition's text, following the forms the gate actually uses.

    Implicit concatenation across lines is the common one — the condition is
    longer than 88 columns — so a plain ``ast.Constant`` check would read the
    first fragment only and miss every name after it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        # An f-string: only its literal parts are readable, which is enough,
        # because an interpolated attribute name would itself be the finding.
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _registered_names(extra_attributes):
    if not isinstance(extra_attributes, ast.Dict):
        return set()
    return {
        f"#{key.value}"
        for key in extra_attributes.keys
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and not key.value.startswith(":")
    }


@pytest.mark.unit
def test_the_gate_has_conditional_status_writes_to_check():
    """Non-vacuity. If the gate stops using compare-and-set this file asserts
    nothing, and that should be a decision rather than a green tick."""
    calls = _conditional_status_calls()
    assert len(calls) >= 2, (
        f"expected at least the admission and the revert-after-SQS-failure calls "
        f"to carry a ConditionExpression; found {len(calls)}"
    )


@pytest.mark.unit
def test_every_condition_only_names_registered_attributes():
    offenders = []
    for lineno, condition_node, extra_node, scope in _conditional_status_calls():
        condition = _resolve_condition(condition_node, scope)
        if condition is None:
            # Not statically readable even after following local assignment.
            # Reported rather than skipped, so a dynamic condition is a
            # deliberate choice instead of a silent hole in the only check that
            # covers this.
            offenders.append(
                f"index.py:{lineno}: condition_expression could not be resolved "
                f"statically, so its attribute names cannot be verified here"
            )
            continue
        referenced = set(re.findall(r"#\w+", condition))
        available = ALWAYS_REGISTERED | _names_registered_in_scope(extra_node, scope)
        missing = sorted(referenced - available)
        if missing:
            offenders.append(
                f"index.py:{lineno}: condition names {missing} but nothing "
                f"registers them. Add the bare attribute name (no ':' prefix) "
                f"to this call's extra_attributes so a SET clause registers it."
            )
    assert not offenders, "\n".join(offenders)


@pytest.mark.unit
def test_both_known_conditions_are_actually_resolved():
    """Guards the check above against going quietly blind.

    Its finding for "cannot resolve" and its finding for "unregistered name" are
    both failures, but a resolver that silently returned text for neither call
    would still let a real defect through if the reporting were ever relaxed. So
    the resolution itself is asserted: both conditions must come back as text,
    and must mention the attributes they are known to compare.
    """
    resolved = {
        lineno: _resolve_condition(node, scope)
        for lineno, node, _extra, scope in _conditional_status_calls()
    }
    unresolved = [lineno for lineno, text in resolved.items() if text is None]
    assert not unresolved, f"unresolved conditions at lines {unresolved}"

    joined = " ".join(text for text in resolved.values() if text is not None)
    # The admission's status clause, its version clause, and the revert's
    # scan-time clause — the three the gate is known to write.
    assert "#ObjectStatus" in joined
    assert "#MalwareScanVersionId" in joined
    assert "#MalwareScanTime" in joined


@pytest.mark.unit
def test_the_always_registered_set_matches_the_service():
    """``ALWAYS_REGISTERED`` is a copy of a fact in another file, so it is
    checked against that file rather than trusted. If the service stops
    registering one of these, every condition naming it becomes a finding here
    and the check above would otherwise pass it."""
    service = (
        REPO / "lib" / "idp_common_pkg" / "idp_common" / "dynamodb" / "service.py"
    ).read_text(encoding="utf-8")
    for name in ALWAYS_REGISTERED:
        bare = name.lstrip("#")
        assert f'expression_names["{name}"] = "{bare}"' in service or (
            f'"{name}": "{bare}"' in service
        ), (
            f"{name} is listed as always-registered here, but "
            f"idp_common/dynamodb/service.py no longer registers it in "
            f"update_document_status."
        )
