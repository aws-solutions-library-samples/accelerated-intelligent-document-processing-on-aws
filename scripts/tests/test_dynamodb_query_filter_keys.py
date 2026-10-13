# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No ``Query`` in this tree may name a key attribute in its ``FilterExpression``.

DynamoDB refuses it. From the ``Query`` API reference: *"A ``FilterExpression`` does
not allow key attributes. You cannot define a filter expression based on a partition
key or a sort key."* The call fails with

    ValidationException: Filter Expression can only contain non-primary key
    attributes: Primary key attribute: SK

⚠️ **``Scan`` is the opposite, and the asymmetry is the whole reason this gate reads
the operation name.** From the Scan documentation: *"With ``Scan``, you can specify
any attributes in a filter expression—including partition key and sort key
attributes."* So ``table.scan(FilterExpression="begins_with(PK, :p) AND SK = :sk")``
is correct and is left alone, while the same string on ``table.query`` is a call that
has never once succeeded. A gate that flagged both would have to carry an exemption
for every legitimate filtered scan in the repository, and the first thing a reviewer
would learn is to add one.

Why this is a source gate rather than a behavioural test
--------------------------------------------------------

Because the obvious instruments cannot see the defect. #1366 shipped behind a unit
test that asserted the broken expression string against a ``MagicMock`` table, which
accepts anything. The natural remedy — re-run it against ``moto`` — does not work
either: ``moto`` is not DynamoDB's expression engine, and measured on moto 5.1.8 it
**accepts** a ``Query`` whose ``FilterExpression`` names the sort key and returns the
filtered items, exactly as if the call were valid. So on this one property a moto test
proves no more than the mock it replaced, and nothing short of a real endpoint can
tell the two apart at runtime.

What *can* be established offline is that the string is never built, which is what
this reads. It is the same shape as ``scripts/check_filtered_scans.py``, which guards
a different DynamoDB expression-semantics defect in the same way and for the same
reason.

Reach, stated rather than left to be inferred
---------------------------------------------

This reads ``FilterExpression`` arguments that are **string literals** (directly, or
assembled from literal concatenation / f-string parts), on calls that issue a
``.query`` either directly or by handing the bound method to a wrapper, and whether the
arguments are spelled as keywords or splatted from a dict. All three shapes occur among
the sites #1366 was about, and the wrapper form is the one the shared library uses — see
:func:`_issues_a_guarded_call`. It does **not** evaluate an expression built at run time from
variables, and it does not read ``boto3.dynamodb.conditions`` objects — a
``Key("SK").eq(...)`` passed as a ``FilterExpression`` would be missed. Both are
accepted limits: every occurrence of this defect in this repository's history has been
a literal, and a checker that tried to interpret arbitrary expressions would report
findings it could not justify. The key-attribute vocabulary is likewise authored
(``KEY_ATTRIBUTE_NAMES``), because which attribute is a key is a property of a table
rather than of the source.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from repo_files import tracked_paths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The attribute names that are a partition or sort key of some table in this
#: deployment, and so may never appear in a ``Query`` filter. Authored, not derived:
#: "is this attribute a key" is a property of the table definition, and the same name
#: is a key on one table and an ordinary attribute on another. ``ItemType`` and
#: ``InitialEventTime`` are the tracking table's GSI keys, and a GSI's own keys are
#: equally refused in a filter on a query against that index.
KEY_ATTRIBUTE_NAMES = (
    "PK",
    "SK",
    "ItemType",
    "InitialEventTime",
    # ChatSessionsTable / ChatMessagesTable, which carry a live filtered Query in
    # `list_agent_chat_sessions_resolver`. Omitting them meant the identical defect
    # spelled `begins_with(sessionId, :s)` there was missed while `SK` failed.
    "userId",
    "sessionId",
)

#: Calls whose ``FilterExpression`` is read, and so the universe this rule applies to.
#:
#: ``scan`` is outside it. That is a scope decision rather than a tolerated defect, and
#: it is registered in ``scripts/tests/gate_exemptions.json``: AWS documents the two
#: operations oppositely, so the same expression is refused on one and correct on the
#: other, and the premise is a property of the DynamoDB API rather than of anything in
#: this tree. ``test_the_scan_exclusion_is_not_shielding_nothing`` is its ratchet, and
#: that test re-scans with ``("query", "scan")`` written inline rather than through a
#: second named constant: a constant for the lifted universe is itself discovered as an
#: exemption surface, and asking for a registry judgement on the probe that *defends*
#: an exemption is the kind of noise that teaches a reviewer to rubber-stamp the file.
GUARDED_CALLS = ("query",)


class Finding(NamedTuple):
    path: str
    lineno: int
    attribute: str
    expression: str

    def __str__(self) -> str:  # pragma: no cover - only on failure
        return (
            f"{self.path}:{self.lineno}: Query FilterExpression names the key "
            f"attribute {self.attribute!r} — DynamoDB rejects this with "
            f"ValidationException on every call. Move the condition into the "
            f"KeyConditionExpression or evaluate it client-side.\n"
            f"    {self.expression}"
        )


def _literal_string(node: ast.AST) -> str | None:
    """The string ``node`` evaluates to, when that is knowable from the source alone.

    Covers a plain literal, implicit/explicit concatenation of literals and an
    f-string's literal parts. An f-string's *interpolations* contribute nothing, which
    is the conservative direction: a name that happened to hold ``"SK"`` is missed
    rather than a finding being invented for one that did not.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return " ".join(parts) if parts else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is None and right is None:
            return None
        return f"{left or ''}{right or ''}"
    return None


def _filter_expressions_in_dict(node: ast.AST) -> List[Tuple[int, str]]:
    """``FilterExpression`` values in a dict literal, with their line numbers.

    ``dict(FilterExpression=...)`` counts as a dict literal here, because it is one in
    every sense that matters and because the sibling gate this is modelled on
    (``scripts/check_filtered_scans.py``) already reads that spelling — being weaker
    than the model its own docstring cites is the kind of gap nobody goes looking for.
    """
    found: List[Tuple[int, str]] = []
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        for keyword in node.keywords:
            if (
                keyword.arg == "FilterExpression"
                and (text := _literal_string(keyword.value)) is not None
            ):
                found.append((getattr(keyword.value, "lineno", node.lineno), text))
        return found
    if not isinstance(node, ast.Dict):
        return found
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "FilterExpression"
            and (text := _literal_string(value)) is not None
        ):
            found.append((getattr(value, "lineno", node.lineno), text))
    return found


def _contains(node: ast.AST, target: ast.AST) -> bool:
    """Is ``target`` (by identity) anywhere inside ``node``?"""
    return any(child is target for child in ast.walk(node))


def _enclosing_blocks(module: ast.Module, call: ast.Call) -> List[List[ast.stmt]]:
    """Every statement list containing ``call``, outermost first.

    A *branch* rather than a function, because the two are different and the difference
    is what produced a false positive on real code: a resolver that reads one `kwargs`
    name across a whole function cannot tell an `if` branch's assignment from its
    `else` branch's, and those branches are mutually exclusive by construction.
    """
    blocks: List[List[ast.stmt]] = []

    def descend(statements: List[ast.stmt]) -> None:
        for statement in statements:
            if not _contains(statement, call):
                continue
            blocks.append(statements)
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(statement, field, None)
                if isinstance(nested, list) and any(
                    isinstance(item, ast.stmt) for item in nested
                ):
                    descend(nested)
            for handler in getattr(statement, "handlers", None) or []:
                descend(handler.body)
            return

    descend(module.body)
    return blocks


def _bound_names(statements: List[ast.stmt]) -> set:
    """Names assigned anywhere inside ``statements``."""
    bound = set()
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign):
                bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
    return bound


def _binding_scope(call: ast.Call, module: ast.Module, names: set) -> List[ast.stmt]:
    """The narrowest statement list that binds one of ``names``, else the module body.

    ⚠️ **Module-wide resolution made a false positive reachable on real code, and a
    false positive is not the harmless direction for this gate.** Its finding is "this
    call can never succeed", so a wrong one asserts a ``ValidationException`` about a
    correct call. ``feature-platform/sample-health-insurance-review/feature-api/
    handler.py`` binds one ``kwargs`` name in an ``if`` branch that ends in
    ``table.query(**kwargs)`` and another in the ``else`` branch that ends in
    ``table.scan(**kwargs)`` — so a legitimate filtered scan naming ``PK`` there, which
    AWS explicitly permits, was reported against the query. Measured: 1 finding before,
    0 after. Function scope is *not* enough for that shape, because both calls are in
    the same function; mutually exclusive branches are the unit that matters.

    Falling back to the module body keeps the over-approximating behaviour for a name
    bound nowhere nearer — a module-level constant splatted into a query — where
    narrowing would cost detection rather than remove a false positive.
    """
    for statements in reversed(_enclosing_blocks(module, call)):
        if names & _bound_names(statements):
            return statements
    return module.body


def _splatted_filter_expressions(
    call: ast.Call, module: ast.Module
) -> List[Tuple[int, str]]:
    """``FilterExpression`` reachable through ``**kwargs`` on ``call``.

    The paginating idiom builds its arguments in a dict and splats them, which is what
    both defect sites in #1366 did and what ``check_filtered_scans.py`` learned the
    same lesson about: a checker reading only literal keywords sees no arguments at all
    on precisely the calls it exists to protect. Resolution is by *name* anywhere in
    the narrowest **statement block** that binds it — see :func:`_binding_scope`.

    ⚠️ **Module-wide resolution made a false positive reachable on a real file, and a
    false positive is not the harmless direction for this gate.** Its finding is "this
    call can never succeed", so a wrong one asserts a ``ValidationException`` about a
    correct call;
    ``feature-platform/sample-health-insurance-review/feature-api/handler.py`` shares one
    ``kwargs`` local between a ``table.query(**kwargs)`` branch and a
    ``table.scan(**kwargs)`` branch in the same function, so a legitimate filtered scan
    naming ``PK`` there — which AWS explicitly permits — would have been reported against
    the query. Both #1366 sites bind their dict in the same function that splats it, so
    narrowing costs no detection; it also made the earlier wrapper-indirection
    measurements attributable, since this module has a second ``query_kwargs`` splat that
    was absorbing the credit.

    ⚠️ **Three ways the key reaches the dict, and the one the repository actually uses
    is not the dict literal.** Every filtered query in this tree today spells it
    ``query_kwargs["FilterExpression"] = "..."`` — a *subscript* assignment after the
    dict is built — and a version of this reading only ``Assign``/``AnnAssign`` of a
    dict literal reported **16 passed** against the #1366 defect re-injected in that
    spelling. ``.update({...})`` is covered for the same reason. A gate blind to the
    live form is a gate that passes on the next occurrence.
    """
    names = {
        kw.value.id
        for kw in call.keywords
        if kw.arg is None and isinstance(kw.value, ast.Name)
    }
    if not names:
        return []
    found: List[Tuple[int, str]] = []
    scope = _binding_scope(call, module, names)
    for node in [n for stmt in scope for n in ast.walk(stmt)]:
        # Shape 1: `name = {...}` / `name: T = {...}` — the dict literal.
        target_names: List[str] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if value is not None and names.intersection(target_names):
            found.extend(_filter_expressions_in_dict(value))

        # Shape 2: `name["FilterExpression"] = "..."` — a subscript assignment, which
        # is how every filtered query in this repository is actually written.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in names
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "FilterExpression"
                    and (text := _literal_string(node.value)) is not None
                ):
                    found.append((getattr(node, "lineno", 0), text))

        # Shape 3: `name.update({...})`.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in names
        ):
            for argument in node.args:
                found.extend(_filter_expressions_in_dict(argument))
            # `update(FilterExpression=...)` — the keyword form of the same call.
            for keyword in node.keywords:
                if (
                    keyword.arg == "FilterExpression"
                    and (text := _literal_string(keyword.value)) is not None
                ):
                    found.append((getattr(keyword.value, "lineno", 0), text))
    return found


def _mentions_key_attribute(expression: str) -> str | None:
    """The first key attribute ``expression`` names as a whole word, if any."""
    import re

    for name in KEY_ATTRIBUTE_NAMES:
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", expression
        ):
            return name
    return None


def _issues_a_guarded_call(call: ast.Call, guarded: tuple = GUARDED_CALLS) -> bool:
    """Does ``call`` put a ``.query`` on the wire, directly or through a wrapper?

    Two shapes, and the second is the one that matters most here. The direct form is
    ``table.query(...)``. The indirect form hands the *bound method* to a helper that
    issues it, which is what ``idp_common.delete_documents`` does for its retry ladder:

        response = _call_with_retry(description, tracking_table.query, **query_kwargs)

    ``.query`` is then a plain attribute reference and the enclosing ``ast.Call`` is
    ``_call_with_retry``, so a matcher keyed on ``call.func`` sees no query at all —
    measured: reading only the direct form found the two sites in the Lambda-local copy
    and **none** in the shared library, which is the site #1366 was reported against.
    An instrument that misses the reported instance is worse than no instrument, since
    its green mark is read as coverage.

    The wrapper is not named or allow-listed; any call receiving a ``.query`` bound
    method as an argument counts. That over-approximates — passing ``table.query``
    somewhere that never invokes it would still be read — and that is the safe
    direction for a check whose finding is "this cannot succeed".
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in guarded:
        return True
    arguments = list(call.args) + [kw.value for kw in call.keywords]
    return any(isinstance(a, ast.Attribute) and a.attr in guarded for a in arguments)


def scan_source(
    source: str, path: str, guarded: tuple = GUARDED_CALLS
) -> List[Finding]:
    """Every guarded call in ``source`` whose literal FilterExpression names a key.

    ``guarded`` is a parameter solely so the non-vacuity ratchet below can ask what the
    ``scan`` exclusion is shielding. Production callers take the default.
    """
    module = ast.parse(source)
    findings: List[Finding] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not _issues_a_guarded_call(node, guarded):
            continue
        candidates: List[Tuple[int, str]] = []
        for kw in node.keywords:
            if (
                kw.arg == "FilterExpression"
                and (text := _literal_string(kw.value)) is not None
            ):
                candidates.append((getattr(kw.value, "lineno", node.lineno), text))
        candidates.extend(_splatted_filter_expressions(node, module))
        for lineno, expression in candidates:
            attribute = _mentions_key_attribute(expression)
            if attribute is not None:
                findings.append(Finding(path, lineno, attribute, expression))
    # Deduplicated, because one dict literal reached from two call sites is one defect
    # at one line and reporting it twice makes the failure harder to read. The whole
    # tuple has to match, so two genuinely distinct sites still both appear.
    return list(dict.fromkeys(findings))


def _python_files() -> Tuple[Path, ...]:
    return tracked_paths(REPO_ROOT, "*.py")


def _findings_over(
    paths: Iterable[Path], guarded: tuple = GUARDED_CALLS
) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            findings.extend(
                scan_source(source, str(path.relative_to(REPO_ROOT)), guarded)
            )
        except SyntaxError:
            # A file this interpreter cannot parse is out of reach of any AST gate.
            continue
    return findings


@pytest.mark.unit
def test_discovery_is_not_empty():
    """The guard this gate needs most, because its failure mode is silence.

    An empty derived set makes every assertion below pass over nothing — and a
    parametrised test over an empty list collects as a *skip*, not a failure, which is
    how a whole guarantee goes missing without a red mark anywhere.
    """
    files = _python_files()
    assert len(files) > 500, (
        f"discovery returned {len(files)} Python files; this gate proves nothing "
        "about a tree it cannot see"
    )


@pytest.mark.unit
def test_no_query_filters_on_a_key_attribute():
    findings = _findings_over(_python_files())
    assert not findings, "\n".join(str(f) for f in findings)


@pytest.mark.unit
def test_the_scan_exclusion_is_not_shielding_nothing():
    """The ratchet on ``scan`` being outside :data:`GUARDED_CALLS`.

    An exclusion that shields nothing is dead, and a dead exclusion is pre-approval of
    whatever next occupies the name — so the registry entry for this scope decision has
    to be able to show it is load bearing. Lifting the exclusion is how: the sites that
    then appear are the legitimate filtered scans the asymmetry exists for, and if there
    were none, narrowing this gate to ``query`` would be a distinction without a
    difference and the simpler rule would be the honest one.

    The count is reported rather than pinned to an exact figure. A new legitimate
    filtered scan naming ``PK`` or ``SK`` is a correct thing to write, so pinning
    equality would make writing one a red gate — which is the opposite of what this
    exclusion means. What must not happen is the count reaching zero unnoticed.
    """
    files = _python_files()
    guarded_today = set(_findings_over(files))
    scan_only = [
        f for f in _findings_over(files, ("query", "scan")) if f not in guarded_today
    ]
    assert scan_only, (
        "lifting the `scan` exclusion produces no findings, so it currently shields "
        "nothing. Either the legitimate filtered scans have gone — in which case drop "
        "the query/scan distinction and its registry entry — or discovery has narrowed."
    )


@pytest.mark.unit
def test_the_three_repaired_sites_are_covered_by_discovery():
    """Non-vacuity, pinned to the sites #1366 was about.

    A gate that stops reading the files it was written for passes for the wrong
    reason. These three are the ones that carried the defect; the first is in the
    shared library and the other two are the initial and the paginating call in a
    Lambda-local copy of the same helper, which is why they are listed separately.
    """
    discovered = {str(p.relative_to(REPO_ROOT)) for p in _python_files()}
    for path in (
        "lib/idp_common_pkg/idp_common/delete_documents.py",
        "nested/api-resolvers/src/lambda/create_document_resolver/"
        "robust_list_deletion.py",
    ):
        assert path in discovered, f"{path} is not reached by this gate's discovery"


@pytest.mark.unit
@pytest.mark.parametrize(
    "source, expect_finding, why",
    [
        (
            "table.query(KeyConditionExpression=k, "
            'FilterExpression="ObjectKey = :o OR contains(SK, :i)")',
            True,
            "the shipped #1366 expression, as a literal keyword",
        ),
        (
            'kw = {"FilterExpression": "contains(SK, :i)"}\ntable.query(**kw)',
            True,
            "the splatted-dict form both defect sites used",
        ),
        (
            'kw: Dict[str, Any] = {"FilterExpression": "contains(SK, :i)"}\n'
            "table.query(**kw)",
            True,
            "an annotated assignment, which a plain Assign walk misses",
        ),
        (
            'table.query(FilterExpression="begins_with(PK, :p)")',
            True,
            "the partition key is refused in a Query filter too, not only the sort key",
        ),
        (
            'table.query(FilterExpression="ItemType = :t")',
            True,
            "a GSI key attribute is a key attribute for a query on that index",
        ),
        (
            'kw = {"FilterExpression": "contains(SK, :i)"}\n'
            "_call_with_retry(desc, table.query, **kw)",
            True,
            "the bound method handed to a retry wrapper — the shared-library idiom, "
            "and the one a matcher keyed on call.func misses entirely",
        ),
        (
            'kw = {}\nkw["FilterExpression"] = "contains(SK, :i)"\ntable.query(**kw)',
            True,
            "the SUBSCRIPT assignment — the spelling every filtered query in this "
            "repository actually uses, and one a dict-literal-only reading misses",
        ),
        (
            "kw = {}\n"
            'kw.update({"FilterExpression": "contains(SK, :i)"})\n'
            "table.query(**kw)",
            True,
            "and the .update form of the same thing",
        ),
        (
            'kw = {}\nkw.update(FilterExpression="contains(SK, :i)")\ntable.query(**kw)',
            True,
            "the KEYWORD form of .update, which reading only node.args misses",
        ),
        (
            'kw = dict(FilterExpression="contains(SK, :i)")\ntable.query(**kw)',
            True,
            "dict(...) rather than a literal - the spelling the sibling gate "
            "check_filtered_scans.py already handles",
        ),
        (
            'kw = {"FilterExpression": "begins_with(sessionId, :s)"}\n'
            "table.query(**kw)",
            True,
            "a key of another table in this deployment, not only the tracking table's",
        ),
        (
            "def a():\n"
            '    kw = {"FilterExpression": "begins_with(PK, :p)"}\n'
            "    table.scan(**kw)\n"
            "def b():\n"
            '    kw = {"FilterExpression": "ObjectStatus = :s"}\n'
            "    table.query(**kw)",
            False,
            "the false positive: one function's legitimate scan filter must not be "
            "reported against another function's query that reuses the local name",
        ),
        (
            'kw = {}\nkw["ProjectionExpression"] = "PK, SK"\ntable.query(**kw)',
            False,
            "a subscript assignment of a different key is not a filter",
        ),
        (
            "kw = {}\n"
            'kw["FilterExpression"] = "begins_with(PK, :p) AND SK = :sk"\n'
            "table.scan(**kw)",
            False,
            "and the scan asymmetry survives the subscript form too",
        ),
        (
            'table.scan(FilterExpression="begins_with(PK, :p) AND SK = :sk")',
            False,
            "Scan permits key attributes in a filter — the asymmetry this gate reads",
        ),
        (
            'kw = {"FilterExpression": "begins_with(PK, :p) AND SK = :sk"}\n'
            "_call_with_retry(desc, table.scan, **kw)",
            False,
            "and the same asymmetry survives the wrapper indirection",
        ),
        (
            'table.query(FilterExpression="SKUCode = :s")',
            False,
            "a whole-word match, so an attribute merely starting with SK is not a hit",
        ),
        (
            'table.query(FilterExpression="attribute_exists(ParentSK)")',
            False,
            "and not one merely ending with it either",
        ),
        (
            'table.query(FilterExpression="ObjectKey = :o")',
            False,
            "the non-key half of the repaired expression is still legitimate",
        ),
        (
            "table.query(FilterExpression=built_at_runtime)",
            False,
            "a non-literal is out of reach and is reported as such, not guessed at",
        ),
    ],
    ids=[
        "literal-keyword",
        "splatted-dict",
        "annotated-splatted-dict",
        "partition-key",
        "gsi-key",
        "bound-method-through-a-wrapper",
        "subscript-assignment",
        "dict-update",
        "dict-update-keyword-form",
        "dict-call-not-literal",
        "another-tables-key-attribute",
        "a-sibling-functions-scan-filter",
        "subscript-of-another-key",
        "scan-through-a-subscript",
        "scan-is-allowed",
        "scan-through-a-wrapper",
        "prefix-is-not-a-match",
        "suffix-is-not-a-match",
        "non-key-attribute",
        "runtime-expression",
    ],
)
def test_the_matcher_itself(source, expect_finding, why):
    """Drive the real matcher over synthetic sources, one property per case.

    Without this the gate could be inert — a checker that matches nothing passes the
    whole-tree assertion above for the wrong reason, and a repaired tree gives it
    nothing to prove itself against.
    """
    findings = scan_source(source, "probe.py")
    assert bool(findings) is expect_finding, why
