# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No DynamoDB request in this tree may supply an expression value no expression uses.

DynamoDB refuses the whole request, not the unused part::

    ValidationException: 1 validation error detected: Value provided in
    ExpressionAttributeValues unused in expressions: keys: {:max}

So a value supplied on a path whose expression does not reference it turns that
path into one that has never once succeeded. Issue #1358 is the shape to have in
mind: ``queue_processor.update_counter`` built one argument dict for both
directions and always supplied ``:max``, while only the increment path's
``ConditionExpression`` referenced it. Every compensating **decrement** therefore
failed, both callers in ``process_message`` catch and log a failed decrement, and
each one leaked a concurrency slot — reported live as one duplicate EventBridge
delivery walking ``active_count`` up to ``MaxConcurrentWorkflows`` and refusing
every new document for about twenty hours.

The rule this reads
-------------------

**A value supplied unconditionally needs an expression that references it
unconditionally.** That is the invariant the defect breaks, and stating it that
way is what makes it checkable: the pre-fix source supplies ``:max`` in the dict
literal and adds the ``ConditionExpression`` that uses it inside ``if
increment:``, one block deeper. An expression added at the *same* block depth as
the dict is counted, because then it is as unconditional as the value and the
pair is consistent — that shape is legitimate and common, and counting it is what
keeps this gate at zero false positives on this tree.

Why a source gate and not a behavioural test
--------------------------------------------

A unit test can catch this, and the companion
``src/lambda/queue_processor/test_counter_decrement.py`` does — but only where
somebody thought to drive the path against a real expression engine. Neither
instrument the repository already had could: every test that exercised the
compensating-decrement path replaced ``update_counter`` with a ``MagicMock``, and
every test of the real ``update_counter`` either handed ``update_item`` a
``MagicMock`` (which accepts any arguments) or only ever incremented. The defect
sat in the shipped Lambda across four releases behind both. This reads the
property out of the source instead, so a second occurrence anywhere in the tree
fails without anyone having had to write a test for that call site.

Reach, stated rather than left to be inferred
---------------------------------------------

Read: keyword arguments on any call, and dict literals (including the
``dict(...)`` spelling) that pair an expression with ``ExpressionAttributeValues``
— the splatted-kwargs idiom this repository uses. Expression strings are read as
literals, assembled literal concatenation, or an f-string's literal parts, which
is the same reading ``test_dynamodb_query_filter_keys.py`` does.

Not read, each a deliberate miss rather than an invented finding:

* A group whose expression is built at run time from variables. There is nothing
  to compare the placeholders against, so the group is skipped entirely. The
  shared ``idp_common.dynamodb.client`` assembles both halves from its callers'
  arguments, so the defect class routed through it is out of reach of any source
  reading and would need a runtime assertion in that client instead.
* A value added by subscript (``args["ExpressionAttributeValues"][":max"] = ...``)
  rather than appearing in the dict literal. Conditionally supplied values are
  how the #1358 fix is written, and the conservative direction here is to miss a
  defect rather than to report a correct call as broken — so note that the call
  site this gate was written for is itself outside its reach. **What this gate is
  worth here is therefore the OTHER call sites**, every one of which it reads;
  ``update_counter``'s own guard is
  ``src/lambda/queue_processor/test_counter_decrement.py``, which drives both
  directions against a real expression engine. Issue #1461 tracks closing the
  gap.
* The **mirror** defect — an expression referencing a placeholder that is never
  supplied, which DynamoDB also refuses (``An expression attribute value used in
  expression is not defined``). Detecting it needs the conditionally-supplied
  values above, so this gate would have to guess, and its finding would be
  "this call can never succeed". That one is left to the runtime tests.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from repo_files import tracked_paths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every DynamoDB request member whose value is an expression that may reference a
#: ``:placeholder``. Authored from the DynamoDB API reference rather than derived:
#: which members are expressions is a property of the service.
#:
#: ``KeyConditionExpression`` and ``FilterExpression`` are here even though a
#: ``Query`` carrying an unused value fails identically to an ``UpdateItem`` — the
#: rule is a property of the request, not of the operation, which is why this gate
#: reads neither the operation name nor the receiver.
EXPRESSION_KEYS = (
    "UpdateExpression",
    "ConditionExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)

VALUES_KEY = "ExpressionAttributeValues"

#: A substitution placeholder. DynamoDB's own grammar: a colon followed by
#: alphanumerics and underscores.
PLACEHOLDER = re.compile(r":[A-Za-z0-9_]+")


class Finding(NamedTuple):
    path: str
    lineno: int
    unused: Tuple[str, ...]
    expressions: str

    def __str__(self) -> str:  # pragma: no cover - only on failure
        names = ", ".join(self.unused)
        return (
            f"{self.path}:{self.lineno}: {VALUES_KEY} supplies {names} but no "
            f"expression on this path references it — DynamoDB rejects the whole "
            f"request with ValidationException on every call. Supply the value "
            f"only on the path whose expression uses it.\n"
            f"    {self.expressions}"
        )


def _literal_string(node: ast.AST) -> Optional[str]:
    """The string ``node`` evaluates to, when that is knowable from the source alone.

    ``None`` means "not knowable", and a group containing one such expression is
    skipped rather than judged — an f-string interpolation could name any
    placeholder, so counting only its literal parts would invent findings.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            return None
        return "".join(
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def _members(node: ast.AST) -> Optional[List[Tuple[str, ast.AST]]]:
    """``(name, value)`` for each string-keyed member of a dict literal or ``dict()``.

    ``None`` for anything else, and also for a dict carrying a non-literal key or a
    ``**spread``, because either can introduce a member this reading cannot see.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        if node.args or any(kw.arg is None for kw in node.keywords):
            return None
        return [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
    if not isinstance(node, ast.Dict):
        return None
    members: List[Tuple[str, ast.AST]] = []
    for key, value in zip(node.keys, node.values):
        if key is None:  # `**spread`
            return None
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        members.append((key.value, value))
    return members


def _supplied_placeholders(node: ast.AST) -> Optional[Set[str]]:
    """The placeholder names an ``ExpressionAttributeValues`` literal supplies.

    ``None`` when the mapping is not a literal this reading can enumerate, which
    skips the group.
    """
    members = _members(node)
    if members is None:
        return None
    return {name for name, _ in members}


def _innermost_block(module: ast.Module, target: ast.AST) -> Optional[List[ast.stmt]]:
    """The narrowest statement list containing ``target``.

    Block identity is what distinguishes a value's condition from its expression's:
    an expression added to the dict in a *deeper* block than the dict literal is
    conditional on something the value is not, which is exactly the #1358 defect.
    """
    found: List[Optional[List[ast.stmt]]] = [None]

    def descend(statements: List[ast.stmt]) -> bool:
        for statement in statements:
            if not any(child is target for child in ast.walk(statement)):
                continue
            found[0] = statements
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(statement, field, None)
                if isinstance(nested, list) and any(
                    isinstance(item, ast.stmt) for item in nested
                ):
                    descend(nested)
            for handler in getattr(statement, "handlers", None) or []:
                descend(handler.body)
            # A `match` case's body is as conditional as an `if` branch's, and it
            # reaches neither `body` nor `handlers` — so without this, a value
            # added inside one reads as unconditional.
            for case in getattr(statement, "cases", None) or []:
                descend(case.body)
            return True
        return False

    descend(module.body)
    return found[0]


def _bound_name(module: ast.Module, dict_node: ast.AST) -> Optional[str]:
    """The name ``dict_node`` is assigned to, if it is assigned to a bare name."""
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and node.value is dict_node:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    return target.id
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is dict_node
            and isinstance(node.target, ast.Name)
        ):
            return node.target.id
    return None


def _sibling_expressions(
    module: ast.Module, dict_node: ast.AST
) -> Tuple[List[str], bool]:
    """Expressions added to the same dict at the same block depth.

    Returns the literal expression strings found and whether any such addition was
    **not** a readable literal — in which case the group is skipped, because an
    unreadable expression could reference anything.

    Only same-block additions count. One in a deeper block is conditional, and a
    value supplied unconditionally beside it is the defect this gate reports.
    """
    name = _bound_name(module, dict_node)
    if name is None:
        return [], False
    block = _innermost_block(module, dict_node)
    if block is None:
        return [], False

    expressions: List[str] = []
    unreadable = False
    for statement in block:
        for node in ast.walk(statement):
            # ``AugAssign`` alongside ``Assign``: ``args["UpdateExpression"] +=
            # " REMOVE x"`` appends a clause, and a placeholder that appears only
            # in the appended fragment is referenced just as surely as one in the
            # original. Reading only ``Assign`` would treat such a fragment as
            # absent and report the value it uses, which is the false-positive
            # direction this gate must not take.
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == name
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in EXPRESSION_KEYS
                    ):
                        if _innermost_block(module, node) is not block:
                            continue
                        text = _literal_string(node.value)
                        if text is None:
                            unreadable = True
                        else:
                            expressions.append(text)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
                and _innermost_block(module, node) is block
            ):
                for argument in list(node.args) + [
                    kw.value for kw in node.keywords if kw.arg is None
                ]:
                    members = _members(argument)
                    if members is None:
                        unreadable = True
                        continue
                    for member_name, member_value in members:
                        if member_name not in EXPRESSION_KEYS:
                            continue
                        text = _literal_string(member_value)
                        if text is None:
                            unreadable = True
                        else:
                            expressions.append(text)
                for keyword in node.keywords:
                    if keyword.arg in EXPRESSION_KEYS:
                        text = _literal_string(keyword.value)
                        if text is None:
                            unreadable = True
                        else:
                            expressions.append(text)
    return expressions, unreadable


def _judge(
    members: Sequence[Tuple[str, ast.AST]],
    module: ast.Module,
    dict_node: Optional[ast.AST],
    path: str,
    lineno: int,
) -> Optional[Finding]:
    """One argument group: is every value it supplies referenced by an expression?"""
    present = {name: value for name, value in members}
    if VALUES_KEY not in present:
        return None

    supplied = _supplied_placeholders(present[VALUES_KEY])
    if not supplied:
        return None

    expressions: List[str] = []
    for key in [k for k in EXPRESSION_KEYS if k in present]:
        text = _literal_string(present[key])
        if text is None:
            # An expression nobody can read could reference anything.
            return None
        expressions.append(text)

    # The sibling lookup runs BEFORE the "no expression here" exit, not after.
    # A group whose values are in the literal and whose expression is assigned by
    # subscript at the same block depth carries no expression key in the literal
    # at all, and returning early on that skipped a group this gate's own rule
    # judges perfectly well — the expression is as unconditional as the values.
    if dict_node is not None:
        sibling, unreadable = _sibling_expressions(module, dict_node)
        if unreadable:
            return None
        expressions.extend(sibling)

    if not expressions:
        # Nothing to compare against: not a request this reading can judge.
        return None

    referenced: Set[str] = set()
    for text in expressions:
        referenced |= set(PLACEHOLDER.findall(text))

    unused = tuple(sorted(supplied - referenced))
    if not unused:
        return None
    return Finding(path, lineno, unused, " | ".join(expressions))


def scan_source(source: str, path: str) -> List[Finding]:
    """Every argument group in ``source`` that supplies an unreferenced value."""
    module = ast.parse(source)
    findings: List[Finding] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            if any(kw.arg is None for kw in node.keywords):
                # A `**spread` can carry an expression this reading cannot see.
                continue
            members = [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
            # A `dict(...)` call IS the argument mapping, so it can be bound to a
            # name and added to later, exactly as a dict literal can; anything
            # else is the request itself and has no later additions to read.
            is_mapping = isinstance(node.func, ast.Name) and node.func.id == "dict"
            finding = _judge(
                members, module, node if is_mapping else None, path, node.lineno
            )
            if finding is not None:
                findings.append(finding)
        members_or_none = _members(node) if isinstance(node, ast.Dict) else None
        if members_or_none is not None:
            finding = _judge(members_or_none, module, node, path, node.lineno)
            if finding is not None:
                findings.append(finding)
    return list(dict.fromkeys(findings))


def _python_files() -> Tuple[Path, ...]:
    return tracked_paths(REPO_ROOT, "*.py")


def _findings_over(paths: Iterable[Path]) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            findings.extend(scan_source(source, str(path.relative_to(REPO_ROOT))))
        except SyntaxError:
            # A file this interpreter cannot parse is out of reach of any AST gate.
            continue
    return findings


@pytest.mark.unit
def test_discovery_is_not_empty():
    """The guard this gate needs most, because its failure mode is silence.

    An empty derived set makes the whole-tree assertion pass over nothing.
    """
    files = _python_files()
    assert len(files) > 500, (
        f"discovery returned {len(files)} Python files; this gate proves nothing "
        "about a tree it cannot see"
    )


@pytest.mark.unit
def test_no_request_supplies_an_unused_expression_value():
    findings = _findings_over(_python_files())
    assert not findings, "\n".join(str(f) for f in findings)


@pytest.mark.unit
def test_the_repaired_site_is_covered_by_discovery():
    """Non-vacuity, pinned to the file #1358 was about.

    A gate that stops reading the file it was written for passes for the wrong
    reason.
    """
    discovered = {str(p.relative_to(REPO_ROOT)) for p in _python_files()}
    assert "src/lambda/queue_processor/index.py" in discovered


@pytest.mark.unit
@pytest.mark.parametrize(
    "source, expect_finding, why",
    [
        (
            "args = {\n"
            '    "UpdateExpression": "ADD active_count :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "if increment:\n"
            '    args["ConditionExpression"] = "active_count < :max"\n'
            "table.update_item(**args)",
            True,
            "the shipped #1358 defect, verbatim: the value is unconditional and the "
            "expression that uses it is one block deeper",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "a match case's body is as conditional as an if branch's, and it "
            "reaches neither `body` nor `handlers` on the statement",
        ),
        (
            'table.update_item(UpdateExpression="ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": -1, ":max": 100})',
            True,
            "the same defect spelled as keyword arguments on the call",
        ),
        (
            'args = dict(UpdateExpression="ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": -1, ":max": 100})',
            True,
            "dict(...) rather than a literal, the spelling the sibling gate reads too",
        ),
        (
            'table.query(KeyConditionExpression="PK = :pk",\n'
            '    ExpressionAttributeValues={":pk": p, ":sk": s})',
            True,
            "the rule is a property of the request, not of UpdateItem — a Query "
            "carrying an unused value fails identically",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            False,
            "the repair: the value is now supplied only where it is referenced",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "the false positive to avoid: an expression added at the SAME block "
            "depth is as unconditional as the value, so the pair is consistent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args.update({"ConditionExpression": "c < :max"})\n'
            "table.update_item(**args)",
            False,
            "and the .update spelling of the same same-block addition",
        ),
        (
            "args = {\n"
            '    "Key": {"id": 1},\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["UpdateExpression"] = "ADD c :inc"\n'
            "table.update_item(**args)",
            True,
            "the literal carries no expression key at all — the expression is a "
            "same-depth subscript assignment, which is as unconditional as the "
            "values, so the group is judgeable and :max is unused",
        ),
        (
            "args = {\n"
            '    "Key": {"id": 1},\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "table.update_item(**args)",
            False,
            "and with no expression anywhere there is nothing to compare against, "
            "so the group is skipped rather than reported",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["UpdateExpression"] += " SET limit_seen = :max"\n'
            "table.update_item(**args)",
            False,
            "an augmented assignment appends a clause, so a placeholder used only "
            "in the appended fragment is referenced — reading Assign alone would "
            "report the value it uses",
        ),
        (
            'args = dict(UpdateExpression="ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": 1, ":max": 100})\n'
            'args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "a dict(...) call is the argument mapping too, so a same-block "
            "addition to it counts the same way a dict literal's does",
        ),
        (
            "table.update_item(UpdateExpression=built_at_runtime,\n"
            '    ExpressionAttributeValues={":inc": 1, ":max": 100})',
            False,
            "an expression nobody can read could reference anything, so the group "
            "is skipped rather than guessed at",
        ),
        (
            'table.update_item(UpdateExpression=f"SET {attr} = :v",\n'
            '    ExpressionAttributeValues={":v": 1, ":w": 2})',
            False,
            "an f-string with an interpolation is equally unreadable — counting "
            "only its literal parts would invent a finding",
        ),
        (
            'table.update_item(UpdateExpression="SET a = :v",\n'
            "    ExpressionAttributeValues=values)",
            False,
            "a mapping built elsewhere supplies placeholders this cannot enumerate",
        ),
        (
            'table.update_item(UpdateExpression="ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": 1})',
            False,
            "the ordinary correct call",
        ),
        (
            'table.update_item(UpdateExpression="SET #s = :s",\n'
            '    ExpressionAttributeNames={"#s": "state"},\n'
            '    ExpressionAttributeValues={":s": "OPEN"})',
            False,
            "an ExpressionAttributeNames placeholder is a different namespace and "
            "is governed by a different rule",
        ),
        (
            'table.update_item(UpdateExpression="ADD c :increment",\n'
            '    ExpressionAttributeValues={":inc": 1})',
            True,
            "matching is on the whole placeholder: :inc is not satisfied by an "
            "expression that happens to mention :increment",
        ),
        (
            'table.update_item(UpdateExpression="ADD c :inc "\n'
            '    "REMOVE drift", ExpressionAttributeValues={":inc": 1})',
            False,
            "implicit concatenation of literals is read as the one string it is",
        ),
        (
            'table.update_item(UpdateExpression="SET a = :v", **rest)',
            False,
            "a call carrying a **spread could receive the values from anywhere",
        ),
    ],
    ids=[
        "the-1358-defect",
        "a-match-case-is-conditional",
        "keyword-arguments",
        "dict-call-not-literal",
        "a-query-not-an-update",
        "the-1358-repair",
        "same-block-addition",
        "same-block-update-call",
        "expression-only-by-subscript",
        "no-expression-anywhere",
        "augmented-subscript-append",
        "same-block-addition-to-a-dict-call",
        "runtime-expression",
        "interpolated-f-string",
        "runtime-values-mapping",
        "ordinary-correct-call",
        "attribute-names-are-a-different-namespace",
        "whole-placeholder-match",
        "implicit-concatenation",
        "splatted-call",
    ],
)
def test_the_matcher_itself(source, expect_finding, why):
    """Drive the real matcher over synthetic sources, one property per case.

    Without this the gate could be inert: a checker that matches nothing passes
    the whole-tree assertion above for the wrong reason, and a repaired tree gives
    it nothing to prove itself against.
    """
    findings = scan_source(source, "probe.py")
    assert bool(findings) is expect_finding, why
