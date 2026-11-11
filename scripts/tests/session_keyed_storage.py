# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Find DynamoDB keys built from a session or conversation id with no user term.

A chat session id is chosen by the browser and arrives in the request body. A
DynamoDB key built from it alone names the same item for every user who sends
that id, so whatever is stored there is readable by any of them unless something
else establishes whose the id is. Conversation memory was keyed that way
(``conversation#{session_id}``), and so is chat history (``PK=sessionId``).

The rule this module computes is about the KEY, not about a spelling: a key
construction is a finding when one of its key components is derived from a
session or conversation identifier and none is derived from a user identifier.
"Derived" follows assignments inside a function, so ``pk = session_id`` followed
by ``Key={"PK": pk}`` is the same finding as ``Key={"PK": session_id}``.

What counts as a key construction:

* the ``Key=`` or ``Item=`` dict of a DynamoDB item call (``get_item``,
  ``put_item``, ``update_item``, ``delete_item``), restricted to the entries whose
  name is a key attribute of some table in this repository's templates — the
  attribute names come from every ``AWS::DynamoDB::Table`` ``KeySchema`` (and
  index key schemas), not from a list written here;
* the ``ExpressionAttributeValues`` of a call that passes
  ``KeyConditionExpression``, and the operands of a ``Key(...)`` condition;
* a string built with a ``#`` in its literal text (this repository's key-prefix
  convention, ``conversation#...``, ``doc#...``): an f-string, a ``+``
  concatenation, or ``"...#{}".format(...)``.

What it does not see: a key assembled in one function and used in another (it
follows assignments within a function only), a key built with ``%``, and a key
whose session component is named with no ``session`` or ``conversation`` in it. A finding is therefore evidence; a clean run is evidence
that none of the visible shapes occur, not a proof.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: DynamoDB item-level calls whose ``Key=``/``Item=`` dict names one item.
ITEM_CALLS = frozenset({"get_item", "put_item", "update_item", "delete_item"})

_SESSION_TOKENS = ("sessionid", "conversationid")
#: Words that on their own name a person's identity. ``user`` alone does not:
#: ``user_ts`` is the timestamp of a user message, so ``user`` counts only beside
#: ``id`` or ``name``.
_IDENTITY_WORDS = frozenset({"sub", "username", "userid", "owner", "caller"})


def _words(identifier: str) -> list[str]:
    """``callerSub`` -> ``["caller", "sub"]``; ``USER_ID`` -> ``["user", "id"]``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def names_a_session(identifier: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", identifier.lower())
    return any(t in flat for t in _SESSION_TOKENS)


def names_a_user(identifier: str) -> bool:
    words = _words(identifier)
    if _IDENTITY_WORDS.intersection(words):
        return True
    return "user" in words and bool({"id", "name"}.intersection(words))


def table_key_attributes(templates: dict[str, dict]) -> frozenset[str]:
    """Every attribute name used in a DynamoDB key schema across ``templates``."""
    names: set[str] = set()
    for doc in templates.values():
        for res in (doc.get("Resources") or {}).values():
            if not isinstance(res, dict) or res.get("Type") != "AWS::DynamoDB::Table":
                continue
            props = res.get("Properties") or {}
            schemas = [props.get("KeySchema") or []]
            for idx in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"):
                for index in props.get(idx) or []:
                    if isinstance(index, dict):
                        schemas.append(index.get("KeySchema") or [])
            for schema in schemas:
                if not isinstance(schema, list):
                    continue
                for element in schema:
                    if isinstance(element, dict) and isinstance(
                        element.get("AttributeName"), str
                    ):
                        names.add(element["AttributeName"])
    return frozenset(names)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    shape: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} ({self.shape})"


class _Taint:
    """Which local names hold a session-derived or user-derived value."""

    def __init__(self, fn: ast.AST):
        self.session: set[str] = set()
        self.user: set[str] = set()
        assigns: list[tuple[list[str], ast.AST]] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets]
                value = node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value:
                targets = [node.target]
                value = node.value
            elif isinstance(node, ast.NamedExpr):
                targets = [node.target]
                value = node.value
            else:
                continue
            names = [
                n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)
            ]
            if names:
                assigns.append((names, value))
        changed = True
        while changed:
            changed = False
            for names, value in assigns:
                s, u = self.classify(value)
                for n in names:
                    if s and n not in self.session:
                        self.session.add(n)
                        changed = True
                    if u and n not in self.user:
                        self.user.add(n)
                        changed = True

    def classify(self, expr: ast.AST) -> tuple[bool, bool]:
        """``(session_derived, user_derived)`` for an expression."""
        session = user = False
        for node in ast.walk(expr):
            label = None
            if isinstance(node, ast.Name):
                if node.id in self.session:
                    session = True
                if node.id in self.user:
                    user = True
                label = node.id
            elif isinstance(node, ast.Attribute):
                label = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # event["sessionId"], arguments.get("sessionId"), claims["sub"],
                # and a condition string such as "PK = :session_id".
                label = node.value
            if label is None:
                continue
            if names_a_session(label):
                session = True
            if names_a_user(label):
                user = True
        return session, user


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _callee(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _resolve_dict(value: ast.AST, fn: ast.AST) -> ast.Dict | None:
    if isinstance(value, ast.Dict):
        return value
    if isinstance(value, ast.Name):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(
                    isinstance(t, ast.Name) and t.id == value.id for t in node.targets
                )
            ):
                return node.value
    return None


def _scopes(tree: ast.Module):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield node


def _own_nodes(scope: ast.AST):
    """Nodes in ``scope`` excluding nested function bodies (they are scopes)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _string_key_parts(node: ast.AST) -> tuple[str, list[ast.AST]] | None:
    """``(literal text, operands)`` for ``"a#" + x`` or ``"a#{}".format(x)``.

    ``None`` for anything else. A ``+`` chain counts only when at least one leaf is
    a string constant, so arithmetic is never read as a key.
    """
    if isinstance(node, ast.Call):
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "format"
            and isinstance(f.value, ast.Constant)
            and isinstance(f.value.value, str)
        ):
            return f.value.value, [*node.args, *(k.value for k in node.keywords)]
        return None
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
        return None
    leaves: list[ast.AST] = []
    stack: list[ast.AST] = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            stack.extend([n.left, n.right])
        else:
            leaves.append(n)
    literal = "".join(
        n.value
        for n in leaves
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    if not literal:
        return None
    operands = [n for n in leaves if not isinstance(n, ast.Constant)]
    return literal, operands


def scan_source(path: str, text: str, key_attributes: frozenset[str]) -> list[Finding]:
    """Findings in one module's source."""
    tree = ast.parse(text)
    findings: list[Finding] = []
    # An f-string that is one entry of a Key/Item dict is judged with the rest of
    # that dict: the item's other key attributes may carry the user.
    judged_with_their_item: set[int] = set()
    for scope in _scopes(tree):
        taint = _Taint(scope)
        for node in _own_nodes(scope):
            if isinstance(node, ast.Call):
                callee = _callee(node)
                if callee in ITEM_CALLS:
                    for kw in ("Key", "Item"):
                        value = _kw(node, kw)
                        d = _resolve_dict(value, scope) if value is not None else None
                        if d is None:
                            continue
                        session = user = False
                        for v in d.values:
                            for sub in ast.walk(v):
                                if isinstance(
                                    sub, (ast.JoinedStr, ast.BinOp, ast.Call)
                                ):
                                    judged_with_their_item.add(id(sub))
                        for k, v in zip(d.keys, d.values):
                            if not (
                                isinstance(k, ast.Constant)
                                and k.value in key_attributes
                            ):
                                continue
                            s, u = taint.classify(v)
                            session |= s
                            user |= u or names_a_user(str(k.value))
                        if session and not user:
                            findings.append(
                                Finding(path, node.lineno, f"{callee} {kw}=")
                            )
                kce = _kw(node, "KeyConditionExpression")
                if kce is not None:
                    session, user = taint.classify(kce)
                    values = _kw(node, "ExpressionAttributeValues")
                    d = _resolve_dict(values, scope) if values is not None else None
                    if d is not None:
                        for v in d.values:
                            s, u = taint.classify(v)
                            session |= s
                            user |= u
                    if session and not user:
                        findings.append(
                            Finding(path, node.lineno, "KeyConditionExpression")
                        )
            if (
                isinstance(node, (ast.BinOp, ast.Call))
                and id(node) not in judged_with_their_item
            ):
                parts = _string_key_parts(node)
                if parts is not None:
                    literal, operands = parts
                    if "#" in literal and operands:
                        session = user = False
                        for operand in operands:
                            s, u = taint.classify(operand)
                            session |= s
                            user |= u
                        if session and not user:
                            findings.append(Finding(path, node.lineno, "string key"))
            elif isinstance(node, ast.JoinedStr):
                if id(node) in judged_with_their_item:
                    continue
                literal = "".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                formatted = [
                    v.value for v in node.values if isinstance(v, ast.FormattedValue)
                ]
                if "#" not in literal or not formatted:
                    continue
                session = user = False
                for f in formatted:
                    s, u = taint.classify(f)
                    session |= s
                    user |= u
                if session and not user:
                    findings.append(Finding(path, node.lineno, "f-string key"))
    return findings


def scan_paths(
    root: Path, paths: list[str], key_attributes: frozenset[str]
) -> list[Finding]:
    out: list[Finding] = []
    for rel in paths:
        text = (root / rel).read_text(encoding="utf-8")
        # A file that does not parse is raised, not skipped: skipping it would
        # report the file as clean having read none of it.
        out.extend(scan_source(rel, text, key_attributes))
    return out
