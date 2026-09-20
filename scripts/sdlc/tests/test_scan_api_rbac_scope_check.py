# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The RBAC scanner's scope check (S4) must be per-OPERATION, not per-file.

Background — the gap this suite exists to prevent
-------------------------------------------------
S4 verifies that an operation declared `scope_checked`/`scope_filtered` in
`scripts/api_rbac_expectations.yaml` actually enforces the caller's
`allowedConfigVersions`. It used to do that by grepping the whole `enforced_in`
**file**, which is satisfiable by an unrelated function in the same module — and
was: `listDocuments` and `getDocumentCount` share a resolver, `listDocuments`
referenced the scope, `getDocumentCount` resolved no caller at all, and its
`scope_filtered: true` declaration passed for months on its neighbour's string.

Reading one operation's own dispatch branch is easy to get subtly wrong in ways
that quietly restore file scope, and each of those ways is pinned below:

* unparsing an `ast.If` emits its `orelse` — the entire rest of the `elif` chain —
  so a whole-node read gives every operation every *later* operation's code;
* a comparison operator has to be read, or `if f != "op":` counts as dispatching
  *to* `op` and attributes a sibling's branch to it;
* the "sole operation in this file" fallback must be counted over every operation
  declared against the file, not only the scope-flagged ones.

The snippets model those shapes directly, so the rules keep their teeth
independently of the live repo state — which, once correct, exercises only the
passing side.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parents[1]
if str(_SDLC_DIR) not in sys.path:
    sys.path.insert(0, str(_SDLC_DIR))


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "scan_api_rbac_scope", _SDLC_DIR / "scan_api_rbac.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_api_rbac_scope"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()

pytestmark = pytest.mark.unit


# Two operations in one module: the first enforces the scope, the second does not.
# This is the exact shape of the false declaration S4 failed to see.
_TWO_OPS = '''
def handler(event, context):
    field = event["info"]["fieldName"]
    if field == "listThings":
        return list_things(event)
    elif field == "countThings":
        return count_things(event)
    raise ValueError(field)


def list_things(event):
    allowed = _caller_scope(event)
    return [t for t in _query() if scope_allows(allowed, t.get("ConfigVersion"))]


def count_things(event):
    return {"count": len(_query())}


def _caller_scope(event):
    row = users_table.query(IndexName="EmailIndex")
    return row.get("allowedConfigVersions")


def _query():
    return []
'''


def _reaches_scope(text: str, op: str, *, sole_op: bool = False) -> bool:
    error, reachable = scanner.op_scope_source(
        text, op, declared_entry=None, sole_op=sole_op
    )
    assert error is None, error
    return any(p in reachable for p in scanner.SCOPE_PATTERNS)


class TestPerOperationIsolation:
    def test_the_enforcing_operation_passes(self):
        assert _reaches_scope(_TWO_OPS, "listThings")

    def test_the_non_enforcing_operation_fails(self):
        """The defect S4 exists for, and could not see at file scope."""
        assert not _reaches_scope(_TWO_OPS, "countThings")

    def test_a_branch_does_not_inherit_a_later_elif(self):
        """`ast.unparse` of an `If` includes its `orelse` — read the body only.

        Ordering matters here: `countThings` is the LATER branch, so if the read
        included `orelse` the earlier `listThings` would be credited with nothing
        extra while `countThings`... would still be clean. Flip the order so the
        non-enforcing operation comes first and would inherit the enforcing one.
        """
        flipped = _TWO_OPS.replace(
            '    if field == "listThings":\n        return list_things(event)\n'
            '    elif field == "countThings":\n        return count_things(event)\n',
            '    if field == "countThings":\n        return count_things(event)\n'
            '    elif field == "listThings":\n        return list_things(event)\n',
        )
        assert flipped != _TWO_OPS

        assert not _reaches_scope(flipped, "countThings")
        assert _reaches_scope(flipped, "listThings")

    def test_an_operation_with_no_dispatch_in_a_shared_file_is_a_finding(self):
        """Falling back to module scope is what let the false declaration pass."""
        error, _ = scanner.op_scope_source(
            _TWO_OPS, "someOtherOp", declared_entry=None, sole_op=False
        )

        assert error is not None
        assert "scope_enforced_in" in error

    def test_the_sole_operation_fallback_uses_the_handler_closure(self):
        error, reachable = scanner.op_scope_source(
            _TWO_OPS, "someOtherOp", declared_entry=None, sole_op=True
        )

        assert error is None
        assert "def handler" in reachable

    def test_a_declared_entry_point_must_exist(self):
        error, _ = scanner.op_scope_source(
            _TWO_OPS, "listThings", declared_entry="no_such_function", sole_op=False
        )

        assert error is not None
        assert "no_such_function" in error


class TestDispatchRecognition:
    @pytest.mark.parametrize(
        "test_source,expected",
        [
            ('f == "countThings"', True),
            ('"countThings" == f', True),
            ('f in ("countThings", "other")', True),
            ('f != "countThings"', False),
            ('f not in ("countThings",)', False),
            ('f == "listThings"', False),
        ],
    )
    def test_only_a_positive_comparison_selects_a_branch(
        self, test_source, expected
    ):
        """A negated comparison names the operation but selects the OTHER branch."""
        node = ast.parse(f"if {test_source}:\n    pass\n").body[0]

        assert scanner._selects_literal(node.test, "countThings") is expected

    def test_a_string_outside_an_if_test_is_not_a_dispatch(self):
        """A required-groups dict key must not stand in for the dispatch."""
        source = '_GROUPS = {"countThings": {"Admin"}}\n'
        tree = ast.parse(source)

        assert scanner._dispatch_branches(tree, "countThings") == []

    def test_all_branches_naming_the_operation_are_unioned(self):
        """The revision ops dispatch twice: an outer scope check, an inner pick."""
        source = '''
def handler(event):
    op = event["op"]
    if op in ("alpha", "beta"):
        if not scope_allows(allowed, profile):
            return _denied()
        if op == "alpha":
            return handle_alpha()
        if op == "beta":
            return handle_beta()
'''
        tree = ast.parse(source)
        reachable = "\n".join(
            ast.unparse(s) for s in scanner._dispatch_branches(tree, "alpha")
        )

        assert "scope_allows" in reachable
        assert "handle_alpha" in reachable


class TestScopePatterns:
    def test_a_bare_local_variable_name_is_not_evidence(self):
        """`allowed_versions` in a docstring or signature proves nothing."""
        assert "allowed_versions" not in scanner.SCOPE_PATTERNS

    def test_the_matcher_and_the_stored_attribute_are_evidence(self):
        for pattern in ("scope_allows", "allowedConfigVersions"):
            assert pattern in scanner.SCOPE_PATTERNS
