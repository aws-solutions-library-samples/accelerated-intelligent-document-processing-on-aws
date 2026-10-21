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

**A value bound in some block needs an expression that references it and is
reachable from that block** — supplied in that same block, or in one that
encloses it. That is the invariant the defect breaks, and stating it in terms of
blocks is what makes it checkable in both of the shapes that matter:

* The pre-fix source supplies ``:max`` in the dict literal and adds the
  ``ConditionExpression`` that uses it inside ``if increment:``, one block
  deeper. A deeper block is not reachable from the literal's, so the value is
  reported: on the path where the condition is absent the request carries a value
  nothing references.
* The repaired source supplies ``:max`` by subscript *inside* the branch that
  references it. Value and expression share a block, so the pair is consistent.
  The same holds for a value in a branch and an expression in the block *above*
  it, which is as unconditional as anything can be.
* An expression in a **sibling** branch is reachable from neither — ``if`` adds
  the value and ``else`` adds the expression that uses it, so the value is
  reported, which is right: no execution supplies both.

A value and an expression at the same depth but in disjoint branches are
therefore not a pair. Depth alone would say they are.

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
is the same reading ``test_dynamodb_query_filter_keys.py`` does. Both halves are
read wherever they are added to a dict bound to a name: in the literal, by
subscript assignment (``args["ConditionExpression"] = ...``,
``args["ExpressionAttributeValues"][":max"] = ...``), by ``.update(...)`` on
either mapping, and by ``|=``. Each addition carries the block it was made in,
which is what the rule above compares — so ``update_counter``, this
repository's exemplar for conditionally supplied values, is read and accepted
rather than skipped.

Not read, each a deliberate miss rather than an invented finding:

* A group whose expression is built at run time from variables. There is nothing
  to compare the placeholders against, so the group is skipped entirely. The
  shared ``idp_common.dynamodb.client`` assembles both halves from its callers'
  arguments, so the defect class routed through it is out of reach of any source
  reading and would need a runtime assertion in that client instead.
* A group whose ``ExpressionAttributeValues`` is a **name** rather than a mapping
  this can enumerate — the values are accumulated in a local dict and the request
  is assembled from it afterwards. Pairing the two halves would mean following
  data flow. This is the **largest** category here rather than a corner of it:
  there are dozens of such groups, most of them supply at least one value
  conditionally, and several supply all of theirs that way, so a green run from
  this gate says nothing about any of them. Measure the current set with the
  gate's own helpers (``_members`` over each group's ``ExpressionAttributeValues``
  member) rather than trusting a figure written here, which goes stale as soon as
  a request is added. ``circuit_breaker_manager.update_circuit_breaker_state`` is
  the one with the most conditionally supplied values and is covered
  behaviourally, by
  ``src/lambda/circuit_breaker_manager/test_update_expression_pairing.py``;
  ``test_the_site_this_cannot_reach_is_covered_behaviourally`` below keeps those
  two statements from drifting apart. **That cover is for that one site and does
  not extend to the others.** Closing the category means either a behavioural
  instrument per site — a few already have one incidentally, in the suite that
  sits beside them, which is worth checking before writing another — or an
  instrument here that follows data flow.
* A group whose supplied set might be **smaller** than what this read: the
  values mapping replaced wholesale, a ``pop`` or a ``del``, a member key this
  cannot name (which could be the values mapping itself). Any one of those skips
  the group, because the alternative is to judge a set of values the request does
  not carry. An extra value this cannot *name* is the opposite case and does not
  skip anything — see ``_Additions`` for why the two directions are not
  symmetric.
* A value bound in a nested function, or in a branch disjoint from the one
  holding the literal. Neither can be attributed to this literal by a reading
  that does not follow data flow. A write from a closure skips the group outright
  rather than being ignored, because ignoring an expression added there is how a
  correct call gets reported.
* A second request built on the same name in the same scope. The second binding
  makes every addition ambiguous between the two (this reading is order-blind),
  so the added values are dropped from both and each literal is judged on its own
  members. ``_Additions.rebound`` is that drop.
* The **mirror** defect — an expression referencing a placeholder that is never
  supplied, which DynamoDB also refuses (``An expression attribute value used in
  expression is not defined``). The value side is now readable, so the data is
  there; what is not there is a safe verdict. A value supplied from a
  non-enumerable mapping, or by any of the skipped spellings above, looks
  identical to one never supplied, and the finding would say "this call can never
  succeed" about a call that works. Reporting it would need the skip conditions
  to be exhaustive in the *opposite* direction to the one they are written for.
  That one is left to the runtime tests.

Where a finding can still be wrong
----------------------------------

**A false positive here asserts that a working call can never succeed**, which is
the expensive direction, so it is worth saying where one is still possible rather
than implying nowhere. Two families, both found by probing spellings with no
expected answer written down first, and neither occurring in this tree:

* **Additions are found by name.** An assignment, an annotated assignment or a
  walrus binds the literal to one; a literal reaching the request through
  anything else — ``cast(..., {...})``, ``.copy()``, ``copy.deepcopy``, a
  ``ChainMap``, ``functools.partial``, a helper the dict is handed to — is judged
  on its own members, and an expression added through the other name is not seen.
  Closing this means following data flow, which is a different instrument.
* **Block shape this reading does not model.** A ``with`` body and a ``finally``
  body are read as unconditional and a ``try`` body is not, each for the reason
  given in ``_always_run_statement_lists``; an exhaustive branching satisfies a
  value when every branch references it, per ``_exhaustive_branches``. What is
  left over is the constructs neither rule names — a walrus inside an ``if`` or
  ``while`` *test*, where the body is conditional on a literal this does not
  evaluate, and any exhaustiveness an ``if``/``else`` or a ``match`` wildcard
  does not express.

Both are reasons to read a finding before acting on it, not reasons to distrust a
green run: a finding names a line, and the expressions it compared are printed
underneath it.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

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


#: Methods on the argument dict, or on its ``ExpressionAttributeValues``, that
#: cannot change what the request supplies. Any other attribute call on either
#: mapping can: ``pop``, ``setdefault``, ``clear`` and ``popitem`` all do, and
#: this reading models none of them — so it skips the group rather than judge a
#: set of values that is not the set the request carries.
READING_DICT_METHODS = frozenset(
    {"get", "keys", "values", "items", "copy", "__contains__", "__getitem__"}
)

#: Statements that open a new name scope. A write to the argument dict inside one
#: belongs to a closure this reading does not follow.
SCOPE_STATEMENTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: One block: the statement list a statement sits in. Identity, not depth, is what
#: makes two blocks the same block — see ``_Blocks``.
Block = List[ast.stmt]


#: Statements whose ``body`` runs whenever control passes them, so it is not a
#: condition. ``with`` is here with one caveat, stated in
#: ``_always_run_statement_lists``.
ALWAYS_RUN_BODY = (ast.With, ast.AsyncWith)


def _child_statement_lists(statement: ast.stmt) -> Iterator[Block]:
    """Every statement list nested one level inside ``statement``.

    ``finalbody``, and a ``with``'s ``body``, are deliberately absent: see
    ``_always_run_statement_lists``.
    """
    for field in ("body", "orelse"):
        if field == "body" and isinstance(statement, ALWAYS_RUN_BODY):
            continue
        nested = getattr(statement, field, None)
        if isinstance(nested, list) and any(
            isinstance(item, ast.stmt) for item in nested
        ):
            yield nested
    for handler in getattr(statement, "handlers", None) or []:
        yield handler.body
    # A ``match`` case's body is as conditional as an ``if`` branch's, and it
    # reaches neither ``body`` nor ``handlers`` — so without this, a value added
    # inside one reads as unconditional.
    for case in getattr(statement, "cases", None) or []:
        yield case.body


def _always_run_statement_lists(statement: ast.stmt) -> Iterator[Block]:
    """Statement lists that run whenever the statement holding them completes.

    A ``finally`` body is the clear case, and it is not a condition: by the time
    anything after the ``try`` runs, the ``finally`` has run. Treating it as a
    nested block would make an expression added there read as conditional and
    report a request that carries it — and would make a value added there read as
    conditional when it is not. Both directions are wrong, so the statements are
    indexed into the block holding the ``try`` itself.

    A ``with`` body is here on the same reasoning, and the caveat is worth
    spelling out in a form a reader can check against their own code: a context
    manager whose ``__exit__`` returns true **suppresses** an exception raised in
    the body — ``contextlib.suppress`` is the one to grep for — and the statement
    after the ``with`` then runs with the body only partly executed. So this is
    the one place the reading treats a request as carrying something it might not,
    and the cost is one measured defect class: a value supplied unconditionally
    with its expression added inside ``with contextlib.suppress(...):``, after
    something that can raise, is missed here and would have been reported by
    comparing depth alone. The trade is for every ordinary ``with self._lock:``
    that adds an expression ceasing to be reported, because reporting a working
    call is the failure this gate cannot afford.

    A ``try`` body is NOT here, for the opposite reason: an exception part-way
    through it, handled beside it, reaches the request with the addition
    genuinely absent — so that block is conditional and a value supplied beside
    it really can go unreferenced.
    """
    if isinstance(statement, ALWAYS_RUN_BODY):
        yield statement.body
    nested = getattr(statement, "finalbody", None)
    if isinstance(nested, list) and any(isinstance(item, ast.stmt) for item in nested):
        yield nested


class _Blocks:
    """Which statement list each node sits in, and how those lists nest.

    Block identity is what distinguishes a value's condition from its
    expression's, and **identity rather than depth** is the part to keep: two
    branches of the same ``if`` are equally deep and share no execution, so an
    expression in one does not pair with a value in the other.

    ``encloses`` is the reachability the gate's rule is stated in — an expression
    added in a block that contains the value's block is supplied on every path
    that supplies the value, which is what makes the pair consistent.

    Indexed once per module: the same questions are asked for every argument
    group in a file, and asking them by re-walking the tree each time is how the
    first version of this spent its time.
    """

    def __init__(self, module: ast.Module) -> None:
        self._innermost: Dict[int, Block] = {}
        self._chain: Dict[int, Tuple[Block, ...]] = {}
        self._scope: Dict[int, ast.AST] = {}
        self._spliced: Dict[int, List[ast.stmt]] = {}
        self._index(module.body, (), module)

    def _index(
        self,
        statements: Block,
        parents: Tuple[Block, ...],
        scope: ast.AST,
        as_block: Optional[Block] = None,
    ) -> None:
        """Index ``statements``; ``as_block`` splices them into an existing block.

        ``as_block`` is how a ``finally`` body is read as part of the block
        holding its ``try`` rather than as a block of its own.
        """
        block = statements if as_block is None else as_block
        if as_block is None:
            self._chain[id(statements)] = parents + (statements,)
            self._scope[id(statements)] = scope
        else:
            # Splicing maps the nodes, and a block is also asked for its
            # STATEMENTS — by the exhaustive-branch reading, which looks for a
            # branching among them. A branching inside a spliced body belongs to
            # the block it was spliced into, or that reading cannot see it.
            self._spliced.setdefault(id(block), []).extend(statements)
        chain = self._chain[id(block)]
        # Every node under this list first, then the nested lists, so that a
        # deeper list overwrites and each node ends up mapped to the innermost
        # block holding it.
        for statement in statements:
            for node in ast.walk(statement):
                self._innermost[id(node)] = block
        for statement in statements:
            inner = statement if isinstance(statement, SCOPE_STATEMENTS) else scope
            for nested in _child_statement_lists(statement):
                self._index(nested, chain, inner)
            for nested in _always_run_statement_lists(statement):
                self._index(nested, chain, inner, as_block=block)

    def block(self, node: ast.AST) -> Optional[Block]:
        """The narrowest statement list containing ``node``."""
        return self._innermost.get(id(node))

    def scope(self, node: ast.AST) -> Optional[ast.AST]:
        """The module, function or class whose body holds ``node``."""
        block = self.block(node)
        return None if block is None else self._scope.get(id(block))

    def encloses(self, outer: Optional[Block], inner: Optional[Block]) -> bool:
        """``outer`` is ``inner`` itself, or one of the blocks containing it.

        Both ``None`` compare equal, which is the keyword-argument case: a group
        written entirely as arguments on one call has no blocks to distinguish,
        and every half of it is supplied exactly when the call runs.
        """
        if outer is inner:
            return True
        if inner is None:
            return False
        return any(block is outer for block in self._chain.get(id(inner), ()))

    def on_one_path(self, first: Optional[Block], second: Optional[Block]) -> bool:
        """Either block contains the other, so some execution reaches both."""
        return self.encloses(first, second) or self.encloses(second, first)

    def chain_of(self, block: Optional[Block]) -> Tuple[Block, ...]:
        """``block`` and every block enclosing it, outermost first."""
        if block is None:
            return ()
        return self._chain.get(id(block), ())

    def statements_of(self, block: Block) -> List[ast.stmt]:
        """``block``'s own statements, plus those of any body spliced into it."""
        spliced = self._spliced.get(id(block))
        return list(block) if spliced is None else list(block) + spliced


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
        elif isinstance(node, ast.NamedExpr) and node.value is dict_node:
            # ``(args := {...})`` binds the name as surely as ``=`` does, and a
            # binding this misses is one whose later additions are not read —
            # which reports the call those additions make correct.
            return node.target.id
    return None


class _Additions(NamedTuple):
    """What is added to one argument dict after it is bound, and where.

    Each entry carries the block it was added in, because that block is what its
    presence in the request is conditional on.

    The two flags are the asymmetry this reading rests on, and it is worth
    stating because it decides every skip below. **An addition this reading
    cannot enumerate is only dangerous in one direction.** What makes a finding
    wrong is an *expression* it did not see (the value is referenced after all)
    or a *removal* it did not see (the value is not supplied after all). An extra
    **value** it cannot name is harmless: the set it read stays a lower bound on
    what the request supplies, and a verdict about ``:max`` does not change
    because some other key arrived. So ``args["ExpressionAttributeValues"].update(
    built_elsewhere)`` leaves the group judgeable, while
    ``args["ExpressionAttributeValues"].pop(":max")`` does not.

    ``unreadable`` is that first direction — an unreadable expression, a removal,
    a replacement of the values mapping, a write from a closure — and the caller
    skips the group: a false positive here asserts that a working call can never
    succeed.

    ``rebound`` is narrower. Once the name may hold a different mapping, a value
    added through it afterwards cannot be attributed to this literal, and
    attributing one wrongly is a false positive. Expressions stay (over-accepting
    is the safe direction), the values are dropped, and the literal's own members
    are still judged — which is the whole of what this gate read before it read
    additions at all.
    """

    expressions: Tuple[Tuple[str, Optional[Block]], ...]
    values: Tuple[Tuple[str, Optional[Block]], ...]
    unreadable: bool
    rebound: bool


def _is_the_dict(node: Optional[ast.AST], name: str) -> bool:
    """``node`` is the bare name the argument dict is bound to."""
    return isinstance(node, ast.Name) and node.id == name


def _is_the_values_mapping(node: Optional[ast.AST], name: str) -> bool:
    """``node`` is ``name["ExpressionAttributeValues"]``."""
    return (
        isinstance(node, ast.Subscript)
        and _is_the_dict(node.value, name)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == VALUES_KEY
    )


def _rooted_at(node: ast.AST, name: str) -> bool:
    """``node`` is a subscript chain starting from the argument dict."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return _is_the_dict(node, name)


def _constant_key(node: ast.Subscript) -> Optional[str]:
    """The string a subscript indexes with, or ``None`` if it is not a literal."""
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _assignment_targets(
    node: Union[ast.Assign, ast.AnnAssign, ast.AugAssign],
) -> List[ast.AST]:
    """Every name or subscript one assignment writes to, unpacking tuple targets.

    ``args, extra = ...`` rebinds ``args`` as surely as ``args = ...`` does, and a
    rebinding this reading misses is one whose later additions it attributes to
    the wrong mapping.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    flattened: List[ast.AST] = []
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            flattened.append(target)
    return flattened


def _additions(blocks: _Blocks, dict_node: ast.AST, name: str) -> _Additions:
    """Expressions and expression values added to ``name`` after it is bound.

    Read: a subscript assignment of an expression (``args["ConditionExpression"]
    = ...``, and ``+=``, which appends a clause whose placeholders are referenced
    just as surely as the original's), a subscript assignment of a value
    (``args["ExpressionAttributeValues"][":max"] = ...``), ``.update(...)`` on
    either mapping, and ``|=`` on either mapping.

    The search covers the whole enclosing scope rather than only the literal's
    own block, because an expression added in a block *above* the literal is
    supplied on every path that reaches it, and a value added in a block *below*
    it is the shape this gate exists to read. It does not follow the name into a
    nested function: a write from a closure is attributable to this literal only
    by following data flow, so one appearing at all skips the group rather than
    being read or quietly ignored.
    """
    anchor = blocks.block(dict_node)
    scope = blocks.scope(dict_node)
    if scope is None:
        return _Additions((), (), False, False)

    expressions: List[Tuple[str, Optional[Block]]] = []
    values: List[Tuple[str, Optional[Block]]] = []
    unreadable = False
    rebound = False

    def expression(block: Optional[Block], node: ast.AST) -> None:
        """An expression added to the request. Unreadable ones skip the group."""
        nonlocal unreadable
        text = _literal_string(node)
        if text is None:
            unreadable = True
        else:
            expressions.append((text, block))

    def supplied(block: Optional[Block], placeholder: str) -> None:
        # A binding in a branch disjoint from the literal's cannot be acting on
        # this literal on any execution that reaches it.
        if blocks.on_one_path(anchor, block):
            values.append((placeholder, block))

    def merged_values(block: Optional[Block], node: ast.AST) -> None:
        """Values merged in wholesale, as by ``update`` or ``|=``.

        Neither can remove a key, so a mapping this cannot enumerate is read as
        far as it goes and no flag is raised — see ``_Additions``.
        """
        for key, _ in _members(node) or ():
            if key.startswith(":"):
                supplied(block, key)

    def merge_into_request(block: Optional[Block], node: ast.Call) -> None:
        """``dict.update``'s mapping arguments, read for expression members.

        Unlike the values mapping, a mapping merged into the *request* can carry
        an expression, so one this cannot enumerate skips the group.
        """
        nonlocal unreadable
        for argument in list(node.args) + [
            keyword.value for keyword in node.keywords if keyword.arg is None
        ]:
            members = _members(argument)
            if members is None:
                unreadable = True
                continue
            for member_name, member_value in members:
                if member_name in EXPRESSION_KEYS:
                    expression(block, member_value)
                elif member_name == VALUES_KEY:
                    # The values mapping is replaced, so what the request
                    # supplies is no longer the literal this group read.
                    unreadable = True

    for node in ast.walk(scope):
        block = blocks.block(node)
        foreign = blocks.scope(node) is not scope

        if isinstance(node, ast.Delete):
            if any(_rooted_at(target, name) for target in node.targets):
                # A removal narrows what the request supplies, so judging the
                # group would report a value that is not sent.
                unreadable = True
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if node.value is None:  # a bare annotation binds nothing
                continue
            targets = _assignment_targets(node)
            merges = isinstance(node, ast.AugAssign) and isinstance(node.op, ast.BitOr)
            for target in targets:
                if not _rooted_at(target, name):
                    continue
                if foreign:
                    unreadable = True
                    continue
                if _is_the_dict(target, name):
                    if merges:
                        # ``args |= {...}`` adds members rather than replacing
                        # the dict, so its expression members count.
                        if _members(node.value) is None:
                            unreadable = True
                        else:
                            for key, value in _members(node.value) or ():
                                if key in EXPRESSION_KEYS:
                                    expression(block, value)
                                elif key == VALUES_KEY:
                                    unreadable = True
                    elif node.value is not dict_node and blocks.on_one_path(
                        anchor, block
                    ):
                        # Only a rebinding some execution reaches *from* this
                        # literal can make a later value ambiguous. The
                        # alternative initialisation in a sibling branch — two
                        # branches each building their own ``args`` — is
                        # mutually exclusive with this one, and treating it as a
                        # rebinding declines both of them.
                        rebound = True
                    continue
                # Past the two branches above, ``_rooted_at`` leaves only a
                # subscript chain starting from the name.
                if _is_the_values_mapping(target, name):
                    if merges:
                        merged_values(block, node.value)
                    else:
                        # Replaced, not added to.
                        unreadable = True
                    continue
                if _is_the_values_mapping(target.value, name):
                    key = _constant_key(target)
                    if key is not None and key.startswith(":"):
                        supplied(block, key)
                    # A key this cannot name adds a value and removes none, so
                    # the group stays judgeable on the values it did read.
                    continue
                if _is_the_dict(target.value, name):
                    key = _constant_key(target)
                    if key is None:
                        # The member being written could be an expression.
                        unreadable = True
                    elif key in EXPRESSION_KEYS:
                        expression(block, node.value)
                    elif key == VALUES_KEY:
                        unreadable = True
            continue

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attribute = node.func.attr
            on_values = _is_the_values_mapping(node.func.value, name)
            on_request = _is_the_dict(node.func.value, name)
            if not (on_values or on_request):
                continue
            if foreign:
                unreadable = True
            elif attribute == "update":
                if on_values:
                    for argument in list(node.args) + [
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg is None
                    ]:
                        merged_values(block, argument)
                else:
                    merge_into_request(block, node)
                    for keyword in node.keywords:
                        if keyword.arg in EXPRESSION_KEYS:
                            expression(block, keyword.value)
                        elif keyword.arg == VALUES_KEY:
                            unreadable = True
            elif on_values and attribute == "setdefault":
                # ``setdefault`` supplies a value and can never take one away,
                # so it belongs with ``update`` and not with ``pop``.
                key = node.args[0] if node.args else None
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.startswith(":")
                ):
                    supplied(block, key.value)
            elif attribute not in READING_DICT_METHODS:
                # ``pop``, ``clear``, ``popitem`` — any of which can take away
                # what this read, and none of which it models. On the request
                # dict ``setdefault`` is here too: its member could be the
                # values mapping, and a default that replaces it is a shape this
                # does not follow.
                unreadable = True

    return _Additions(tuple(expressions), tuple(values), unreadable, rebound)


#: Statements that end a branch without falling through to what follows it. A
#: branch that cannot reach the request does not have to supply anything.
TERMINATING_STATEMENTS = (ast.Raise, ast.Return, ast.Continue, ast.Break)


def _is_irrefutable(pattern: ast.AST) -> bool:
    """A pattern that always matches, which is what makes a ``match`` exhaustive.

    Not a test for ``_``. A bare capture (``case other:``) always matches, so does
    ``case _ as other:``, and so does an alternation one of whose alternatives
    does (``case "down" | _:``) — all three spelled differently and all three
    exhaustive. Writing this as "is it ``_``" is the spelling-enumeration version
    of the same rule, and it reports a request that works for the other two.
    """
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(_is_irrefutable(alternative) for alternative in pattern.patterns)
    return False


def _exhaustive_branches(statement: ast.stmt) -> Optional[List[Block]]:
    """The branch bodies of a statement exactly one of which always runs.

    ``None`` when the statement is not such a branching, which includes the
    shapes that look closest to one:

    * ``if`` with no ``else`` — the #1358 defect lives there, so that is the
      answer which has to stay. An ``elif`` chain with no final ``else`` is an
      ``If`` whose ``orelse`` holds another ``If``, so it *is* read as a
      branching, and the recursion below then asks whether that inner ``If`` is
      covered — which it is not, for the same reason. Neither needs a special
      case.
    * ``match`` with no irrefutable case, and a wildcard carrying a guard is
      refutable, so it does not count either. Which patterns are irrefutable is
      ``_is_irrefutable``, and it is not a test for ``_``: a bare capture
      (``case other:``) always matches too.
    * ``try``/``except``: whether a handler runs is not a branch on a condition
      in the source, and an exception part-way through the body can leave that
      body's own additions half-made.
    """
    if isinstance(statement, ast.If):
        return [statement.body, statement.orelse] if statement.orelse else None
    cases = getattr(statement, "cases", None)
    if cases and any(
        case.guard is None and _is_irrefutable(case.pattern) for case in cases
    ):
        return [case.body for case in cases]
    return None


def _never_reaches_the_request(block: Block) -> bool:
    """The branch ends in ``raise``, ``return``, ``continue`` or ``break``.

    Such a branch supplies nothing because nothing after the branching runs on
    it, so a request outside the branching is not on its path at all.
    """
    return bool(block) and isinstance(block[-1], TERMINATING_STATEMENTS)


def _supplied_on_every_branch(
    placeholder: str,
    block: Block,
    referenced: Sequence[Tuple[Set[str], Optional[Block]]],
    blocks: "_Blocks",
    depth: int = 0,
) -> bool:
    """Is ``placeholder`` referenced on every execution leaving ``block``?

    Reachability on its own compares one expression against one value, and reads
    a pair of **mutually exclusive** expressions as neither: both branches of an
    ``if``/``else`` adding a ``ConditionExpression`` that references the value is
    as unconditional as adding one above the ``if``, and reporting it says a
    request that works on every path can never succeed.

    So a value is satisfied too when some exhaustive branching in its own block —
    or in one enclosing it, which the caller walks — has the placeholder
    referenced in *every* branch. Recursive, because a branch may satisfy it the
    same way; bounded, because branches nest strictly.
    """
    if depth > 16:  # pragma: no cover - deeper than any real source
        return False
    for statement in blocks.statements_of(block):
        branches = _exhaustive_branches(statement)
        if branches is None:
            continue
        if all(
            _never_reaches_the_request(branch)
            or any(placeholder in names and at is branch for names, at in referenced)
            or _supplied_on_every_branch(
                placeholder, branch, referenced, blocks, depth + 1
            )
            for branch in branches
        ):
            return True
    return False


def _judge(
    members: Sequence[Tuple[str, ast.AST]],
    module: ast.Module,
    blocks: _Blocks,
    dict_node: Optional[ast.AST],
    path: str,
    lineno: int,
) -> Optional[Finding]:
    """One argument group: is every value it supplies referenced by an expression?

    "Referenced" is reachability, not presence: the expression has to be supplied
    in the block that supplies the value, or in one enclosing it.
    """
    present = {name: value for name, value in members}
    if VALUES_KEY not in present:
        return None

    literal = _members(present[VALUES_KEY])
    if literal is None:
        # Not a mapping this reading can enumerate.
        return None

    anchor = blocks.block(dict_node) if dict_node is not None else None
    supplied: List[Tuple[str, Optional[Block]]] = [
        (name, anchor) for name, _ in literal
    ]

    expressions: List[Tuple[str, Optional[Block]]] = []
    for key in [k for k in EXPRESSION_KEYS if k in present]:
        text = _literal_string(present[key])
        if text is None:
            # An expression nobody can read could reference anything.
            return None
        expressions.append((text, anchor))

    # The addition lookup runs BEFORE the "no expression here" exit, not after.
    # A group whose values are in the literal and whose expression is assigned by
    # subscript at the same block depth carries no expression key in the literal
    # at all, and returning early on that skipped a group this gate's own rule
    # judges perfectly well — the expression is as unconditional as the values.
    if dict_node is not None:
        name = _bound_name(module, dict_node)
        if name is not None:
            added = _additions(blocks, dict_node, name)
            if added.unreadable:
                return None
            expressions.extend(added.expressions)
            if not added.rebound:
                supplied.extend(added.values)

    if not expressions:
        # Nothing to compare against: not a request this reading can judge.
        return None

    referenced = [
        (set(PLACEHOLDER.findall(text)), block) for text, block in expressions
    ]
    unused: List[str] = []
    for placeholder, block in supplied:
        if any(
            placeholder in names
            for names, at in referenced
            if blocks.encloses(at, block)
        ):
            continue
        if any(
            _supplied_on_every_branch(placeholder, enclosing, referenced, blocks)
            for enclosing in blocks.chain_of(block)
        ):
            continue
        if placeholder not in unused:
            unused.append(placeholder)
    if not unused:
        return None
    return Finding(
        path,
        lineno,
        tuple(sorted(unused)),
        " | ".join(text for text, _ in expressions),
    )


def scan_source(source: str, path: str) -> List[Finding]:
    """Every argument group in ``source`` that supplies an unreferenced value."""
    if VALUES_KEY not in source:
        # Nothing here is judgeable: the member has to appear literally — as a
        # dict key or as a keyword name — for any group to be readable at all.
        # Skipping the parse is what keeps the whole-tree run cheap over a
        # thousand files that never speak to DynamoDB.
        return []
    module = ast.parse(source)
    blocks = _Blocks(module)
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
                members,
                module,
                blocks,
                node if is_mapping else None,
                path,
                node.lineno,
            )
            if finding is not None:
                findings.append(finding)
        members_or_none = _members(node) if isinstance(node, ast.Dict) else None
        if members_or_none is not None:
            finding = _judge(members_or_none, module, blocks, node, path, node.lineno)
            if finding is not None:
                findings.append(finding)
    return list(dict.fromkeys(findings))


def _python_files() -> Tuple[Path, ...]:
    return tracked_paths(REPO_ROOT, "*.py")


def _findings_over(paths: Iterable[Path], root: Path = REPO_ROOT) -> List[Finding]:
    """Findings over ``paths``, reported relative to ``root``.

    ``root`` is a parameter so that the discovery test can drive this over a
    throwaway tree. Writing a probe into the repository instead would be visible
    to the whole-tree assertion above, which runs in the same session and, under
    ``-n``, possibly at the same moment.
    """
    findings: List[Finding] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            findings.extend(scan_source(source, str(path.relative_to(root))))
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
def test_the_repaired_exemplar_is_read_rather_than_skipped():
    """``update_counter``'s conditional supply is READ, not merely not reported.

    Silence has two causes here and they are not the same result. The gate used
    to decline this shape outright, which is why the exemplar for conditionally
    supplied values sat outside the gate written for it (#1461). "No finding"
    therefore proves nothing on its own: it is also what a decline looks like.
    So this asserts what the reading *saw* — both values, each attributed to a
    block the dict literal is not in, which is what "conditional" means here —
    and only then that the verdict is clean.
    """
    source = (REPO_ROOT / "src/lambda/queue_processor/index.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    blocks = _Blocks(module)

    groups = [
        (node, members)
        for node in ast.walk(module)
        if isinstance(node, ast.Dict)
        for members in [_members(node)]
        if members is not None and VALUES_KEY in dict(members)
    ]
    assert groups, (
        "no ExpressionAttributeValues group found in queue_processor/index.py; "
        "this test would otherwise pass over nothing"
    )

    read: Set[str] = set()
    for node, members in groups:
        name = _bound_name(module, node)
        assert name is not None
        added = _additions(blocks, node, name)
        assert not added.unreadable, (
            f"the exemplar at line {node.lineno} is skipped as unreadable, so the "
            "gate is not reading it"
        )
        for placeholder, block in added.values:
            assert block is not blocks.block(node), (
                f"{placeholder} was read at the literal's own block, so this test "
                "is no longer about a CONDITIONALLY supplied value"
            )
            read.add(placeholder)
        assert _judge(members, module, blocks, node, "index.py", node.lineno) is None

    assert {":max", ":zero"} <= read, (
        f"read {sorted(read)} from update_counter's branches, expected both :max "
        "and :zero — the two conditionally supplied values the #1358 fix adds"
    )


@pytest.mark.unit
def test_the_site_this_cannot_reach_is_covered_behaviourally():
    """The one call site in this gate's blind spot that has a per-site test.

    ``circuit_breaker_manager.update_circuit_breaker_state`` accumulates the
    expression and its values in separate local names across a series of
    conditional branches and assembles the request from them afterwards, so the
    member this gate would enumerate holds a ``Name``. It is skipped — correctly,
    since pairing the two halves means following data flow — and it supplies more
    of its values conditionally than any other group in that blind spot, which is
    why it was the one picked to cover behaviourally. Issue #1466.

    ⚠️ **The others in that blind spot are unread by this gate.** Some of them are
    driven incidentally by the suites that sit beside them and some are not, so
    what covers any given one has to be measured rather than inferred from here.
    This is the join between two statements about *one* site, not a closure over
    the category; the reach note at the top of this module says so, and neither it
    nor this test is a ratchet on the category's size.

    It fails in **both** directions: if the per-site test is deleted or gutted, the
    site is in neither set again; if the site is refactored into a shape this gate
    can read, the per-site test may be redundant and the reach note is wrong about
    why it exists. Either way the two statements have drifted and somebody should
    decide which is now true, rather than discovering it from a green run.
    """
    site = REPO_ROOT / "src/lambda/circuit_breaker_manager/index.py"
    per_site_test = (
        REPO_ROOT
        / "src/lambda/circuit_breaker_manager/test_update_expression_pairing.py"
    )
    assert site.is_file(), f"{site} is gone; this test asserts nothing without it"
    assert per_site_test.is_file(), (
        f"{per_site_test.relative_to(REPO_ROOT)} is gone, so the request group "
        "this test is about is now covered by nothing. Restore it, or give the "
        "gate an instrument that can read the site."
    )
    # Existence is not cover: a file left in place with its body removed reads as
    # green here, which is the cheapest way for this join to go vacuous. Parsed
    # rather than searched as text, because the sweep's name surviving only inside
    # a comment passes a substring check over a file that collects nothing.
    cover = ast.parse(per_site_test.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(cover)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "test_every_supplied_token_is_referenced" in defined, (
        f"{per_site_test.relative_to(REPO_ROOT)} exists but defines no "
        "`test_every_supplied_token_is_referenced`, so it is not the cover this "
        "test is about. A file that is present and gutted is not cover."
    )
    # ⚠️ The residual: this reads the file, so it cannot see a cover that is
    # *collected and skipped* — a module-level `pytest.mark.skip` leaves every
    # name above in place. Only a run can answer that, and this gate does not run
    # the cover.

    module = ast.parse(site.read_text(encoding="utf-8"))
    groups = [
        members
        for node in ast.walk(module)
        if isinstance(node, ast.Dict)
        for members in [_members(node)]
        if members is not None and VALUES_KEY in dict(members)
    ]
    assert groups, (
        f"no {VALUES_KEY} group found in {site.relative_to(REPO_ROOT)}; this test "
        "would otherwise pass over nothing"
    )
    unreadable = [
        members for members in groups if _members(dict(members)[VALUES_KEY]) is None
    ]
    assert unreadable, (
        f"every {VALUES_KEY} group in {site.relative_to(REPO_ROOT)} is now "
        "enumerable, so this gate reads the site directly and the per-site test "
        "beside it may be redundant — and the reach note in this module's "
        "docstring no longer describes the tree"
    )


@pytest.mark.unit
def test_a_finding_names_only_the_literal_that_carries_the_value():
    """One finding, on the branch that supplies the value — not on its sibling.

    The parametrised harness below asks only whether anything was reported, so
    it cannot see this: with two literals bound to the same name in disjoint
    branches, attributing one branch's added value to the other branch's literal
    reports a call that is correct, on a line whose own values are all
    referenced. What makes a finding actionable is that the line it names is the
    line to change.
    """
    source = (
        "if fresh:\n"
        "    args = {\n"
        '        "UpdateExpression": "ADD c :inc",\n'
        '        "ExpressionAttributeValues": {":inc": 1},\n'
        "    }\n"
        "else:\n"
        "    args = {\n"
        '        "UpdateExpression": "ADD c :inc",\n'
        '        "ExpressionAttributeValues": {":inc": -1},\n'
        "    }\n"
        '    args["ExpressionAttributeValues"][":max"] = 100\n'
        "table.update_item(**args)"
    )
    findings = scan_source(source, "probe.py")
    assert [(f.lineno, f.unused) for f in findings] == [(7, (":max",))], (
        "expected one finding, on the else branch's literal at line 7; got "
        f"{[(f.lineno, f.unused) for f in findings]}"
    )


@pytest.mark.unit
def test_an_offending_call_is_found_at_any_depth(tmp_path):
    """Discovery is recursive, and the finding names the file that carries it.

    A non-recursive listing has defeated a collector in this repository before,
    and it fails in the direction that reads as a pass: the gate reports nothing
    and the tree looks clean. Driven over a throwaway root rather than a probe
    written into the repository, which the whole-tree assertion above would see.
    """
    offender = (
        "args = {\n"
        '    "UpdateExpression": "ADD c :inc",\n'
        '    "ExpressionAttributeValues": {":inc": 1},\n'
        "}\n"
        "if increment:\n"
        '    args["ExpressionAttributeValues"][":max"] = 100\n'
        "table.update_item(**args)\n"
    )
    deep = tmp_path / "src" / "lambda" / "sibling" / "nested"
    deep.mkdir(parents=True)
    (deep / "handler.py").write_text(offender, encoding="utf-8")
    (tmp_path / "top_level.py").write_text("x = 1\n", encoding="utf-8")

    discovered = tracked_paths(tmp_path, "*.py")
    assert len(discovered) == 2, f"discovery saw {[str(p) for p in discovered]}"

    findings = _findings_over(discovered, root=tmp_path)
    assert [(f.path, f.unused) for f in findings] == [
        ("src/lambda/sibling/nested/handler.py", (":max",))
    ]


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
            'table.update_item(UpdateExpression=f"ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": 1, ":max": 100})',
            True,
            "an f-string with no interpolation is as readable as a plain literal, "
            "and declining it would skip a judgeable group",
        ),
        (
            'table.update_item(UpdateExpression="ADD c :inc "\n'
            '    "REMOVE drift", ExpressionAttributeValues={":inc": 1})',
            False,
            "implicit concatenation of literals is read as the one string it is",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            'args["ExpressionAttributeValues"][":max"] = MAX\n'
            "if increment:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "two mutually exclusive expressions that both reference the value are as "
            "unconditional as one above the branching: this is the obvious hoist of a "
            "shared bound, and reporting it says a request that works on every path "
            "can never succeed",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if a:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "elif b:\n"
            '    args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "an elif chain with no final else is not exhaustive, and it is an If whose "
            "orelse holds another If, so the recursion has to answer it",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if a:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "elif b:\n"
            '    args["ConditionExpression"] = "c <= :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "c = :max"\n'
            "table.update_item(**args)",
            False,
            "and the same chain WITH a final else is, by the same recursion",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if a:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "attribute_exists(c)"\n'
            "table.update_item(**args)",
            True,
            "one branch referencing it is not every branch: the other path sends the "
            "value with nothing using it",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case _:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "an irrefutable case _ makes the cases exhaustive",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case 'down':\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "without one it is not, however many cases there are",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case _ if late:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "and a wildcard carrying a guard is refutable, so it does not count",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if not ok:\n"
            "    raise ValueError(ok)\n"
            "else:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "a branch that raises reaches no request, so it has nothing to supply",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if a:\n"
            "    if b:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    else:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "c = :max"\n'
            "table.update_item(**args)",
            False,
            "a branch may be covered the same way, which is why this recurses",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if a:\n"
            "    if b:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "c = :max"\n'
            "table.update_item(**args)",
            True,
            "and one hole anywhere in the nesting leaves a path that sends the value "
            "unreferenced",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "with lock:\n"
            "    if capped:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "and a branch INSIDE a with body is a condition again, so the lists "
            "nested in one still have to be indexed rather than skipped",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "with lock:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "a with body runs whenever control passes it, so it is not a condition - "
            "see _always_run_statement_lists for the one caveat",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "try:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "except KeyError:\n"
            "    pass\n"
            "table.update_item(**args)",
            True,
            "a try body is the opposite case: an exception part-way through it, "
            "handled beside it, reaches the request with the addition absent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "except KeyError:\n"
            "    pass\n"
            "else:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "and a try else runs only when nothing was raised",
        ),
        (
            'table.update_item(UpdateExpression="ADD c " + ":inc",\n'
            '    ExpressionAttributeValues={":inc": 1, ":max": 9})',
            True,
            "an expression assembled by explicit concatenation is read as the "
            "string it evaluates to; the implicit-concatenation case below is one "
            "ast.Constant after parsing, so it cannot pin this branch",
        ),
        (
            'table.update_item(UpdateExpression="SET a = :v",\n'
            '    ExpressionAttributeValues={":v": 1, ":max": 9}, **rest)',
            False,
            "a splatted call that DOES supply values is what tests the spread "
            "skip: the spread could carry the expression that uses :max",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            'args["ExpressionAttributeValues"].setdefault(chosen, 100)\n'
            "table.update_item(**args)",
            True,
            "a setdefault key this cannot name adds a value and removes none, so "
            "the values it did read are still judgeable",
        ),
        (
            "args = {\n"
            "    **base,\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "table.update_item(**args)",
            False,
            "a spread inside the literal can introduce an expression member this "
            "reading cannot see",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case other:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "a bare capture always matches, so these cases are exhaustive. Reading "
            "the rule as a test for the token _ is the spelling-enumeration version "
            "of it, and reports a request that works",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case _ as other:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "and so does a wildcard given a name, which is a MatchAs wrapping a "
            "MatchAs - hence the recursion in _is_irrefutable",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case 'down' | _:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "and an alternation one of whose alternatives always matches",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case 'down' | 'flat':\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "while an alternation of literals matches neither more nor less than the "
            "literals do",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case other if late:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "and a capture carrying a guard is refutable however irrefutable its "
            "pattern is",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "finally:\n"
            "    if a:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    else:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "an exhaustive branching inside a spliced finally belongs to the block it "
            "was spliced into: splicing has to move the STATEMENTS as well as the "
            "nodes, or this reading cannot see the branching at all",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "with lock:\n"
            "    if a:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    else:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "and the same inside a with body, which is spliced by the same mechanism",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "finally:\n"
            "    if a:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "whereas a NON-exhaustive branching inside a finally is still a "
            "condition, so the value beside it can still go unreferenced",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "if outer:\n"
            "    try:\n"
            "        probe()\n"
            "    finally:\n"
            "        if inner:\n"
            '            args["ConditionExpression"] = "c < :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "and one branch of an exhaustive pair covered only conditionally leaves "
            "a path that sends the value unreferenced",
        ),
        (
            "from typing import Any, Dict\n"
            'registry["request"]: Dict[str, Any] = {\n'
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            'table.update_item(**registry["request"])',
            True,
            "an annotated assignment whose target is a subscript binds no name, so "
            "the literal is judged on its own members - and asking a subscript for "
            "its .id would raise, which reports nothing about the rest of the tree",
        ),
        (
            "async def bump(table):\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "    }\n"
            "    async with lock:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    await table.update_item(**args)",
            False,
            "an async with body runs on the same terms as a with body, and a "
            "reading that names only one of them reports this correct call",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case 'down' as other:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "a value pattern given a name matches only that value, so the recursion "
            "must ask whether the INNER pattern is irrefutable rather than whether "
            "any MatchAs appears beneath. Reading this one as exhaustive is the "
            "false-NEGATIVE direction: a match wrongly read as exhaustive satisfies "
            "every value and turns a real defect silent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    case [_]:\n"
            '        args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            True,
            "and a wildcard inside a sequence pattern matches sequences of one, not "
            "everything - likewise {**rest}, (_, _) and a class pattern",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "with outer:\n"
            "    with inner:\n"
            "        if a:\n"
            '            args["ConditionExpression"] = "c < :max"\n'
            "        else:\n"
            '            args["ConditionExpression"] = "c <= :max"\n'
            "table.update_item(**args)",
            False,
            "splicing is transitive: a body spliced into a body that is itself "
            "spliced belongs to the outermost block of the three, or the branching "
            "inside it stays invisible however many levels are recorded",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 9},\n'
            "}\n"
            "with outer:\n"
            "    with inner:\n"
            "        if a:\n"
            '            args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "while the same nesting around a NON-exhaustive branching still leaves a "
            "path that sends the value unreferenced, which is what distinguishes the "
            "transitive splice from reading every nested body as unconditional",
        ),
        (
            'table.update_item(UpdateExpression="SET a = :v", **rest)',
            False,
            "a call carrying a **spread could receive the values from anywhere",
        ),
        # ------------------------------------------------------------------
        # A value supplied by subscript, which is the shape #1461 is about. The
        # pairing rule is reachability, so each case below differs from its
        # neighbour in exactly one block relationship.
        # ------------------------------------------------------------------
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            True,
            "a conditionally supplied value with no expression anywhere: on the "
            "path that supplies it the request references nothing",
        ),
        (
            "args = {\n"
            '    "Key": {"counter_id": 1},\n'
            '    "UpdateExpression": "ADD active_count :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1 if increment else -1},\n'
            "}\n"
            "if increment:\n"
            '    args["ConditionExpression"] = "active_count < :max"\n'
            '    args["ExpressionAttributeValues"][":max"] = MAX\n'
            "else:\n"
            '    args["ConditionExpression"] = "active_count > :zero"\n'
            '    args["ExpressionAttributeValues"][":zero"] = 0\n'
            "table.update_item(**args)",
            False,
            "the repaired exemplar: two branches, different conditions, each "
            "supplying only the value its own condition references",
        ),
        (
            "args = {\n"
            '    "Key": {"counter_id": 1},\n'
            '    "UpdateExpression": "ADD active_count :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1 if increment else -1},\n'
            "}\n"
            'args["ExpressionAttributeValues"][":max"] = MAX\n'
            "if increment:\n"
            '    args["ConditionExpression"] = "active_count < :max"\n'
            "else:\n"
            '    args["ConditionExpression"] = "active_count > :zero"\n'
            '    args["ExpressionAttributeValues"][":zero"] = 0\n'
            "table.update_item(**args)",
            True,
            "the same exemplar with one branch's value hoisted out of it — #1358 "
            "exactly, now reached in the spelling the repair uses",
        ),
        (
            "from typing import Any, Dict\n"
            "args: Dict[str, Any] = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            False,
            "an annotated assignment binds the name just as a plain one does, and "
            "it is the spelling the exemplar actually uses",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            'args["ConditionExpression"] = "c < :max"\n'
            "if increment:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            False,
            "an expression in the block ABOVE the value is supplied on every path "
            "that supplies the value, so the pair is consistent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "else:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "sibling branches are equally deep and share no execution, which is "
            "why the rule is block identity and not block depth",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if a:\n"
            "    pass\n"
            "elif b:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            True,
            "an elif body is an If nested in the outer If's orelse, so a value "
            "added there is two lists deep rather than one",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "except ValueError:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            True,
            "an except handler's body is reached through `handlers`, not `body`",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "match direction:\n"
            "    case 'up':\n"
            '        args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            True,
            "and a match case's body through `cases` — the value side needs every "
            "block the expression side needed",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "for bound in bounds:\n"
            '    args["ExpressionAttributeValues"][":max"] = bound\n'
            "table.update_item(**args)",
            True,
            "a loop body is conditional too: on the sequence having an element",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "    if tighter:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "the defect one level in: the expression is deeper than the value, so "
            "the value is still supplied on a path nothing references it",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "    if tighter:\n"
            '        args["ExpressionAttributeValues"][":max"] = 50\n'
            "table.update_item(**args)",
            False,
            "and the same two blocks the other way round, which is correct: the "
            "expression encloses the value",
        ),
        (
            "class Counter:\n"
            "    def bump(self):\n"
            "        args = {\n"
            '            "UpdateExpression": "ADD c :inc",\n'
            '            "ExpressionAttributeValues": {":inc": 1},\n'
            "        }\n"
            "        if self.capped:\n"
            '            args["ExpressionAttributeValues"][":max"] = 100\n'
            "        self.table.update_item(**args)",
            True,
            "a method body is a block like any other; the chain simply starts deeper",
        ),
        (
            "async def bump(table):\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1},\n'
            "    }\n"
            "    if capped:\n"
            '        args["ExpressionAttributeValues"][":max"] = 100\n'
            "    await table.update_item(**args)",
            True,
            "and an async def is a scope statement of its own, which the scope "
            "lookup has to name or the whole function reads as foreign",
        ),
        (
            'args = dict(UpdateExpression="ADD c :inc",\n'
            '    ExpressionAttributeValues={":inc": 1})\n'
            "if increment:\n"
            '    args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            True,
            "a dict(...) call is the argument mapping, so a value added to it by "
            "subscript is read the same way a literal's is",
        ),
        # ------------------------------------------------------------------
        # The other spellings of "add".
        # ------------------------------------------------------------------
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"].update({":max": 100})\n'
            "table.update_item(**args)",
            True,
            "update() on the values mapping supplies a value as surely as a "
            "subscript assignment does",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"].update({":max": 100})\n'
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "and paired with its expression in the same branch it is correct — "
            "reporting this is the false positive the reach exists to avoid",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"] |= {":max": 100}\n'
            "table.update_item(**args)",
            True,
            "|= merges into the mapping rather than replacing it",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "if increment:\n"
            '    args.update({"ConditionExpression": "c < :max"})\n'
            "table.update_item(**args)",
            True,
            "the #1358 defect with the expression arriving by update() — a "
            "conditional expression does not pair with an unconditional value",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "if increment:\n"
            '    args |= {"ConditionExpression": "c < :max"}\n'
            "table.update_item(**args)",
            True,
            "and with |= on the request, which is a merge and not a rebinding",
        ),
        # ------------------------------------------------------------------
        # The declines. Each is a shape where the supplied set this reads is not
        # the set the request carries, so a finding would claim a working call
        # can never succeed.
        # ------------------------------------------------------------------
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "finally:\n"
            "    if capped:\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "and a branch INSIDE a finally is a condition again, so the lists "
            "nested in one still have to be indexed rather than skipped",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "finally:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "a finally body is not a condition: by the time the request runs it "
            "has run, so the expression it adds is as unconditional as the value",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"].setdefault(":max", 100)\n'
            "table.update_item(**args)",
            True,
            "setdefault supplies a value and can never take one away, so it is "
            "read rather than treated as a mutation this cannot follow",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args.setdefault("ExpressionAttributeValues", rebuilt)\n'
            "table.update_item(**args)",
            False,
            "and setdefault on the REQUEST is the other case: its member could be "
            "the values mapping, which this does not follow",
        ),
        (
            "(args := {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "})\n"
            'args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            False,
            "a walrus binds the name too, and a binding this misses is one whose "
            "later expression is not read — which reports a correct call",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ExpressionAttributeValues"].pop(":max")\n'
            "table.update_item(**args)",
            False,
            "pop removes a value, so the request does not carry what the literal "
            "shows — and this reading does not model removal",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'del args["ExpressionAttributeValues"][":max"]\n'
            "table.update_item(**args)",
            False,
            "and del is the same removal spelled as a statement",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"].setdefault("legacy", 1)\n'
            "table.update_item(**args)",
            False,
            "and the same through setdefault, whose key filter is its own line",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "if increment:\n"
            '    args["ExpressionAttributeValues"].update({"legacy": 1})\n'
            "table.update_item(**args)",
            False,
            "a merged key that is not a placeholder is not a value this rule "
            "governs, and reporting it would name something no expression could "
            "reference",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args.update(**built_elsewhere)\n"
            "table.update_item(**args)",
            False,
            "and the double-splat spelling of the same merge, which reaches the "
            "argument list by a different route",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ExpressionAttributeValues"].update(**built_elsewhere)\n'
            "table.update_item(**args)",
            True,
            "whereas splatting into the VALUES mapping can only add, so the group "
            "stays judgeable — the asymmetry holds for this spelling too",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args.update(built_elsewhere)\n"
            "table.update_item(**args)",
            False,
            "a mapping merged into the REQUEST can carry an expression, unlike "
            "one merged into the values, so an unreadable one skips the group",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args.update({"ExpressionAttributeValues": rebuilt})\n'
            "table.update_item(**args)",
            False,
            "and a positional update carrying the values key replaces the "
            "mapping, which is the same decline by another spelling",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args |= built_elsewhere\n"
            "table.update_item(**args)",
            False,
            "|= with an unreadable operand could merge in an expression too",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args |= {"ExpressionAttributeValues": rebuilt}\n'
            "table.update_item(**args)",
            False,
            "and |= carrying the values key replaces the mapping, so the literal "
            "no longer says what is sent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args.update(ExpressionAttributeValues=rebuilt)\n"
            "table.update_item(**args)",
            False,
            "and update() takes a keyword too, which replaces the mapping just as "
            "a member of its positional dict would",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args.update(ConditionExpression="c < :max")\n'
            "table.update_item(**args)",
            False,
            "the expression side of that keyword: read, or this correct call is "
            "reported",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ExpressionAttributeValues"] = rebuilt\n'
            "table.update_item(**args)",
            False,
            "the mapping is replaced wholesale, so the literal says nothing about "
            "what is sent",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args[member] = built_elsewhere\n"
            "table.update_item(**args)",
            False,
            "a member key this cannot name could be an expression key carrying an "
            "expression this cannot read",
        ),
        (
            "def outer():\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "    }\n"
            "    def tighten():\n"
            '        args.update({"ConditionExpression": "c < :max"})\n'
            "    tighten()\n"
            "    table.update_item(**args)",
            False,
            "a closure can add an expression by method call as well as by "
            "assignment, and the two routes are guarded separately",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            'args["ExpressionAttributeValues"].setdefault(3, 1)\n'
            "table.update_item(**args)",
            False,
            "a setdefault key that is not a string at all must not be asked "
            "whether it starts with a colon — the gate would raise, and a gate "
            "that raises reports nothing about the rest of the tree",
        ),
        (
            "def outer():\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "    }\n"
            "    def tighten():\n"
            '        args["ConditionExpression"] = "c < :max"\n'
            "    tighten()\n"
            "    table.update_item(**args)",
            False,
            "a closure's writes are attributable only by following data flow, so "
            "one appearing at all skips the group — ignoring the EXPRESSION it "
            "adds is how this call, which works, would get reported",
        ),
        (
            "def outer():\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1},\n'
            "    }\n"
            "    def tighten():\n"
            '        args["ExpressionAttributeValues"][":max"] = 100\n'
            "    tighten()\n"
            "    table.update_item(**args)",
            False,
            "and the value side of the same decline: this one IS broken, and it is "
            "the price of the line above — the skip cannot be one-directional",
        ),
        (
            "args = {}\n"
            'args["ConditionExpression"] = "c < :max"\n'
            "def bump():\n"
            "    args = {\n"
            '        "UpdateExpression": "ADD c :inc",\n'
            '        "ExpressionAttributeValues": {":inc": 1},\n'
            "    }\n"
            "    if capped:\n"
            '        args["ExpressionAttributeValues"][":max"] = 100\n'
            "    table.update_item(**args)",
            True,
            "a same-named dict in another scope is another dict: the search is "
            "bounded to the enclosing function, or a module-level args satisfies "
            "every local one",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "try:\n"
            "    probe()\n"
            "except ValueError:\n"
            '    args["ConditionExpression"] = "c < :max"\n'
            "table.update_item(**args)",
            True,
            "the expression side of the handler block: an expression added only in "
            "a handler is as conditional as one in an if, and the value is not",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args |= {"ConditionExpression": "c < :max"}\n'
            "table.update_item(**args)",
            False,
            "|= on the request at the same block is a merge, not a rebinding, so "
            "the expression it carries is as unconditional as the value",
        ),
        # ------------------------------------------------------------------
        # ...and the cases that look like declines and are not. An extra value
        # this cannot name removes nothing, so the values it DID read are still
        # supplied and still judgeable. Declining here would hand back coverage
        # the gate had before it read additions at all.
        # ------------------------------------------------------------------
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ExpressionAttributeValues"].update(built_elsewhere)\n'
            "table.update_item(**args)",
            True,
            "update() cannot remove a key, so :max is still supplied and still "
            "referenced by nothing",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            'args["ExpressionAttributeValues"][chosen_bound] = 100\n'
            "table.update_item(**args)",
            True,
            "and a value key this cannot name adds one and removes none",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1, ":max": 100},\n'
            "}\n"
            "args = built_elsewhere\n"
            "table.update_item(**args)",
            True,
            "a rebinding drops the ADDED values, not the literal's own, which is "
            "what this gate judged before it read additions",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "args = built_elsewhere\n"
            'args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            False,
            "and the value added after a rebinding is the one it drops: it may "
            "belong to the other mapping entirely",
        ),
        (
            "args = {\n"
            '    "UpdateExpression": "ADD c :inc",\n'
            '    "ExpressionAttributeValues": {":inc": 1},\n'
            "}\n"
            "args, extra = built_elsewhere\n"
            'args["ExpressionAttributeValues"][":max"] = 100\n'
            "table.update_item(**args)",
            False,
            "a tuple unpacking rebinds the name too, and a rebinding this reading "
            "misses is one whose later values it attributes to the wrong mapping",
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
        "uninterpolated-f-string",
        "implicit-concatenation",
        "exhaustive-branches-both-referencing",
        "an-elif-chain-with-no-else",
        "an-elif-chain-with-an-else",
        "only-one-branch-references-the-value",
        "a-match-wildcard-is-exhaustive",
        "a-match-with-no-wildcard-is-not",
        "a-guarded-wildcard-is-refutable",
        "a-branch-that-raises-supplies-nothing",
        "nested-exhaustive-branches",
        "nested-branches-with-one-hole",
        "a-branch-inside-a-with-body",
        "an-expression-in-a-with-body",
        "an-expression-in-a-try-body",
        "an-expression-in-a-try-else",
        "explicit-literal-concatenation",
        "a-splatted-call-that-supplies-values",
        "an-unnameable-setdefault-key",
        "a-spread-inside-the-literal",
        "a-match-bare-capture-is-irrefutable",
        "a-named-wildcard-is-irrefutable",
        "an-alternation-holding-a-wildcard",
        "an-alternation-of-literals-is-not",
        "a-capture-with-a-guard-is-refutable",
        "an-exhaustive-branching-inside-a-finally",
        "an-exhaustive-branching-inside-a-with",
        "a-conditional-branching-inside-a-finally",
        "an-exhaustive-pair-covered-conditionally",
        "an-annotated-subscript-target-binds-no-name",
        "an-expression-in-an-async-with-body",
        "a-value-pattern-given-a-name-is-refutable",
        "a-wildcard-inside-a-sequence-pattern",
        "a-with-inside-a-with-splices-transitively",
        "a-transitive-splice-around-a-conditional-branching",
        "splatted-call",
        "conditional-value-with-no-expression",
        "the-repaired-exemplar",
        "the-exemplar-with-one-value-hoisted",
        "annotated-assignment-binding",
        "expression-in-the-block-above-the-value",
        "value-and-expression-in-sibling-branches",
        "value-in-an-elif",
        "value-in-an-except-handler",
        "value-in-a-match-case",
        "value-in-a-for-body",
        "expression-deeper-than-its-value",
        "expression-shallower-than-its-value",
        "inside-a-method",
        "inside-an-async-function",
        "dict-call-with-a-subscript-value",
        "update-on-the-values-mapping",
        "update-on-the-values-mapping-paired",
        "bitor-merge-into-the-values-mapping",
        "expression-by-update-inside-a-branch",
        "expression-by-bitor-inside-a-branch",
        "a-branch-inside-a-finally-is-still-a-branch",
        "an-expression-in-a-finally-is-unconditional",
        "setdefault-on-the-values-mapping",
        "setdefault-on-the-request-is-not-judged",
        "a-walrus-binding-is-read",
        "a-popped-value-is-not-judged",
        "a-deleted-value-is-not-judged",
        "a-setdefault-key-that-is-not-a-placeholder",
        "a-merged-key-that-is-not-a-placeholder",
        "a-double-splat-merged-into-the-request",
        "a-double-splat-merged-into-the-values",
        "an-unreadable-mapping-merged-into-the-request",
        "a-positional-update-carrying-the-values-key",
        "bitor-with-an-unreadable-operand",
        "bitor-carrying-the-values-key",
        "a-values-mapping-replaced-by-an-update-keyword",
        "an-expression-from-an-update-keyword",
        "a-replaced-values-mapping-is-not-judged",
        "an-unnameable-member-key-is-not-judged",
        "a-closure-method-call-is-not-judged",
        "a-setdefault-key-that-is-not-a-string",
        "a-closure-expression-is-not-judged",
        "a-closure-value-is-not-judged-either",
        "a-same-named-dict-in-another-scope",
        "expression-only-in-an-except-handler",
        "bitor-on-the-request-at-the-same-block",
        "values-merged-from-elsewhere-stay-judgeable",
        "an-unnameable-value-key-stays-judgeable",
        "a-rebinding-still-judges-the-literal",
        "a-rebinding-drops-an-added-value",
        "a-tuple-unpacking-is-a-rebinding-too",
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
