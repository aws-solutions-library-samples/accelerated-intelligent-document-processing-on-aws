# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Find DynamoDB keys built from a session or conversation id with no user term.

A chat session id is chosen by the browser and arrives in the request body. A
DynamoDB key built from it alone names the same item for every user who sends
that id, so whatever is stored there is readable by any of them unless something
else establishes whose the id is. Conversation memory was keyed that way
(``conversation#{session_id}``), and so is chat history (``PK=sessionId``).

The rule is about the KEY, not about how a call is spelled: a key construction
is a finding when one of its key components is derived from a session or
conversation identifier and none is derived from a user identifier.

**Key constructions.** Every DynamoDB data-plane call (:data:`DYNAMODB_CALLS`,
which includes the batch, transaction and PartiQL calls) is examined. Every dict
reachable from its arguments is a candidate, however it was built: a literal, a
``dict(...)`` call, a name assigned (plainly or with an annotation) from either,
a dict filled in by ``name["PK"] = value``, a ``**`` splat, a ``self`` attribute
assigned anywhere in the module, or a dict nested inside any of these. Its
entries named after a key attribute of some table in this repository's templates
(every ``KeySchema`` and index ``KeySchema``, not a list written here) are its
key components. A ``KeyConditionExpression`` with its
``ExpressionAttributeValues``, and a PartiQL ``Statement`` with its
``Parameters``, are judged the same way. Separately, any string built with a
``#`` in its literal text (this repository's key-prefix convention) is judged on
its own: an f-string, ``+``, ``%``, ``.format`` or ``"#".join``.

**Derived.** A value is session-derived when it names a session or conversation
id, or is a local name bound from one: by assignment, annotated or augmented
assignment, ``:=``, a ``for`` loop or comprehension over one, or ``with ... as``.
It is user-derived when it names a user identity: ``sub``, ``username``, a
``user`` beside ``id`` or ``name``, or ``owner``/``caller`` alone or beside
``sub``, ``id`` or ``key``. Of ``a or b`` and ``a if c else b``, the result is
user-derived only when every branch is, since either may be the one used.

**What it still cannot see,** stated so a clean run is not over-read: a key
assembled in one function and handed to another as an argument; a session id
named with no ``session`` or ``conversation`` in it; a user term that is only a
user-sounding name for something else (``owner_sub = session_id`` reads as a
user term); and a call made through a wrapper that is not itself a DynamoDB call.
A finding is evidence, and a clean run is evidence that none of the recognised
shapes occur, not a proof.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: DynamoDB data-plane calls, on a client, a ``Table`` resource or a batch writer.
DYNAMODB_CALLS = frozenset(
    {
        "get_item",
        "put_item",
        "update_item",
        "delete_item",
        "query",
        "scan",
        "batch_get_item",
        "batch_write_item",
        "transact_get_items",
        "transact_write_items",
        "execute_statement",
        "batch_execute_statement",
        "execute_transaction",
    }
)

_SESSION_TOKENS = ("sessionid", "conversationid")


def _words(identifier: str) -> list[str]:
    """``callerSub`` -> ``["caller", "sub"]``; ``USER_ID`` -> ``["user", "id"]``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def names_a_session(identifier: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", identifier.lower())
    return any(t in flat for t in _SESSION_TOKENS)


def names_a_user(identifier: str) -> bool:
    """Whether ``identifier`` names a person's identity.

    ``user_ts`` (the timestamp of a user message), ``caller_ts`` and
    ``owner_hint`` do not: ``user`` needs ``id`` or ``name`` beside it, and
    ``owner``/``caller`` need to stand alone or beside ``sub``, ``id`` or ``key``.
    """
    words = _words(identifier)
    if {"sub", "username", "userid"}.intersection(words):
        return True
    if "user" in words and {"id", "name"}.intersection(words):
        return True
    for role in ("owner", "caller"):
        if role in words and (words == [role] or {"id", "key"}.intersection(words)):
            return True
    return False


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


def _bindings(scope: ast.AST) -> list[tuple[list[str], ast.AST]]:
    """``(names bound, value they are bound from)`` for every binding in ``scope``."""
    out: list[tuple[list[str], ast.AST]] = []

    def names(target: ast.AST) -> list[str]:
        return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]

    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            out.extend((names(t), node.value) for t in node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value:
            out.append((names(node.target), node.value))
        elif isinstance(node, ast.NamedExpr):
            out.append((names(node.target), node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            out.append((names(node.target), node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            out.append((names(node.optional_vars), node.context_expr))
    return [(n, v) for n, v in out if n]


class _Taint:
    """Which local names hold a session-derived or user-derived value."""

    def __init__(self, scope: ast.AST, outer: "_Taint | None" = None):
        self.session: set[str] = set(outer.session) if outer else set()
        self.user: set[str] = set(outer.user) if outer else set()
        bindings = _bindings(scope)
        changed = True
        while changed:
            changed = False
            for names, value in bindings:
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
        if isinstance(expr, ast.BoolOp):
            parts = [self.classify(v) for v in expr.values]
            return any(p[0] for p in parts), all(p[1] for p in parts)
        if isinstance(expr, ast.IfExp):
            parts = [self.classify(expr.body), self.classify(expr.orelse)]
            return any(p[0] for p in parts), all(p[1] for p in parts)
        label = None
        session = user = False
        if isinstance(expr, ast.Name):
            session = expr.id in self.session
            user = expr.id in self.user
            label = expr.id
        elif isinstance(expr, ast.Attribute):
            label = expr.attr
        elif isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            # event["sessionId"], arguments.get("sessionId"), claims["sub"],
            # and a condition string such as "PK = :session_id".
            label = expr.value
        if label is not None:
            session = session or names_a_session(label)
            user = user or names_a_user(label)
        for child in ast.iter_child_nodes(expr):
            s, u = self.classify(child)
            session |= s
            user |= u
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
    """``(literal text, operands)`` for a string built by ``+``, ``%``,
    ``.format`` or ``"sep".join``; ``None`` for anything else.

    A ``+`` chain counts only when at least one leaf is a string constant, so
    arithmetic is never read as a key.
    """
    if isinstance(node, ast.Call):
        f = node.func
        if not (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Constant)
            and isinstance(f.value.value, str)
        ):
            return None
        if f.attr == "format":
            return f.value.value, [*node.args, *(k.value for k in node.keywords)]
        if f.attr == "join" and node.args:
            arg = node.args[0]
            elts = list(arg.elts) if isinstance(arg, (ast.Tuple, ast.List)) else [arg]
            literal = f.value.value + "".join(
                e.value
                for e in elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
            return literal, [e for e in elts if not isinstance(e, ast.Constant)]
        return None
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
    ):
        right = node.right
        operands = list(right.elts) if isinstance(right, ast.Tuple) else [right]
        return node.left.value, operands
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


class _Module:
    """Per-module resolution: what a name or ``self`` attribute was built from."""

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.scope_of: dict[int, ast.AST] = {}
        self.taint: dict[int, _Taint] = {}
        module_taint = _Taint(tree)
        for scope in _scopes(tree):
            self.taint[id(scope)] = (
                module_taint if scope is tree else _Taint(scope, module_taint)
            )
            for node in _own_nodes(scope):
                self.scope_of[id(node)] = scope
        # self.<attr> = <value>, anywhere in the module.
        self.self_attrs: dict[str, list[tuple[ast.AST, ast.AST]]] = {}
        for scope in _scopes(tree):
            for node in _own_nodes(scope):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not node.value:
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for t in targets:
                    if (
                        isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                    ):
                        self.self_attrs.setdefault(t.attr, []).append(
                            (node.value, scope)
                        )

    def definitions(self, name: str, scope: ast.AST):
        """Values ``name`` is bound from in ``scope`` (then the module), and the
        ``name[key] = value`` entries written into it."""
        for candidate in (scope, self.tree):
            values, entries = [], []
            for node in _own_nodes(candidate):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id == name:
                            values.append(node.value)
                        elif (
                            isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == name
                            and isinstance(t.slice, ast.Constant)
                        ):
                            entries.append((t.slice.value, node.value))
            if values or entries:
                return values, entries, candidate
            if candidate is self.tree:
                break
        return [], [], scope


def _key_groups(mod: _Module, expr: ast.AST, scope: ast.AST, seen: set[int]):
    """Every dict reachable from ``expr``, as ``(entries, defining scope)``.

    ``entries`` is a list of ``(key, value)`` with constant string keys.
    """
    if id(expr) in seen:
        return
    seen.add(id(expr))
    if isinstance(expr, ast.Dict):
        entries = []
        for k, v in zip(expr.keys, expr.values):
            if k is None:  # {**other}
                yield from _key_groups(mod, v, scope, seen)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                entries.append((k.value, v))
            yield from _key_groups(mod, v, scope, seen)
        yield entries, scope
    elif isinstance(expr, ast.Call) and _callee(expr) == "dict":
        entries = []
        for kw in expr.keywords:
            if kw.arg is not None:
                entries.append((kw.arg, kw.value))
            yield from _key_groups(mod, kw.value, scope, seen)
        for arg in expr.args:
            yield from _key_groups(mod, arg, scope, seen)
        yield entries, scope
    elif isinstance(expr, ast.Name):
        values, entries, where = mod.definitions(expr.id, scope)
        for value in values:
            yield from _key_groups(mod, value, where, seen)
        if entries:
            yield entries, where
    elif (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "self"
    ):
        for value, where in mod.self_attrs.get(expr.attr, []):
            yield from _key_groups(mod, value, where, seen)
    elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        for e in expr.elts:
            yield from _key_groups(mod, e, scope, seen)
    elif isinstance(expr, ast.Starred):
        yield from _key_groups(mod, expr.value, scope, seen)


def _judge(taint: "_Taint", parts) -> tuple[bool, bool]:
    session = user = False
    for part in parts:
        s, u = taint.classify(part)
        session |= s
        user |= u
    return session, user


def scan_source(path: str, text: str, key_attributes: frozenset[str]) -> list[Finding]:
    """Findings in one module's source."""
    tree = ast.parse(text)
    mod = _Module(tree)
    findings: list[Finding] = []
    # A string or value that is one entry of a key dict is judged with the rest of
    # that dict: the item's other key attributes may carry the user.
    judged_with_their_item: set[int] = set()
    for scope in _scopes(tree):
        taint = mod.taint[id(scope)]
        for node in _own_nodes(scope):
            if isinstance(node, ast.Call) and _callee(node) in DYNAMODB_CALLS:
                callee = _callee(node)
                roots = [*node.args, *(k.value for k in node.keywords)]
                seen: set[int] = set()
                for root in roots:
                    for entries, where in _key_groups(mod, root, scope, seen):
                        keyed = [(k, v) for k, v in entries if k in key_attributes]
                        if not keyed:
                            continue
                        for _, v in entries:
                            for sub in ast.walk(v):
                                judged_with_their_item.add(id(sub))
                        session, user = _judge(
                            mod.taint[id(where)], [v for _, v in keyed]
                        )
                        user |= any(names_a_user(k) for k, _ in keyed)
                        if session and not user:
                            findings.append(Finding(path, node.lineno, f"{callee} key"))
                for condition, values in (
                    ("KeyConditionExpression", "ExpressionAttributeValues"),
                    ("KeyConditions", None),
                    ("Statement", "Parameters"),
                ):
                    expr = _kw(node, condition)
                    if expr is None:
                        continue
                    parts = [expr]
                    if values and _kw(node, values) is not None:
                        parts.append(_kw(node, values))
                        for entries, _w in _key_groups(
                            mod, _kw(node, values), scope, set()
                        ):
                            parts.extend(v for _, v in entries)
                    if isinstance(expr, ast.Name):
                        parts.extend(mod.definitions(expr.id, scope)[0])
                    session, user = _judge(taint, parts)
                    if session and not user:
                        findings.append(Finding(path, node.lineno, condition))
            if id(node) in judged_with_their_item:
                continue
            if isinstance(node, (ast.BinOp, ast.Call)):
                parts = _string_key_parts(node)
                if parts is not None:
                    literal, operands = parts
                    if "#" in literal and operands:
                        session, user = _judge(taint, operands)
                        if session and not user:
                            findings.append(Finding(path, node.lineno, "string key"))
            elif isinstance(node, ast.JoinedStr):
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
                session, user = _judge(taint, formatted)
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
