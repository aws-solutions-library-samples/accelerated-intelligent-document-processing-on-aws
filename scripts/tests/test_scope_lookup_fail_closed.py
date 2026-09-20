# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A config-version scope lookup must key on the ``email`` claim and fail closed.

``allowedConfigVersions`` is the per-user restriction that decides which
Configuration Profiles a caller may read or edit and which *documents* they may
see. Discovery finds ten modules in the scanned subtrees that resolve it — nine
independent artifacts plus one vendored copy — each having grown its own copy of the
same three lines, and the copies had drifted into two fail-open shapes:

1. **The lookup key came from an ``or``-chain of claims.** The key is the hash key
   of a ``UsersTable`` ``EmailIndex`` query, and email is the only identifier that
   joins a Cognito principal to a row there — the row's key is a ``uuid4`` minted
   locally and unrelated to the Cognito ``sub``, and no ``sub`` attribute is stored
   on the table at all. So substituting another identifier when the ``email`` claim
   is absent does not find the row by another route: it matches **no** row, an
   empty page is indistinguishable from "this user has no restriction", and the
   caller proceeds unrestricted. No AWS fault is required for that — only claims
   that differ from the ones the code was tested against.

2. **A failed query was caught and turned into "unrestricted".** A missing IAM
   grant, a wrong index name or a throttle then switched the control off, silently,
   for every scoped caller — which is the drift the control exists to survive.

What is **not** a defect, and must not be "fixed" into one: an **empty page** still
means unrestricted. Scoping is opt-in per user, so most users have no row, and
denying there would lock every ordinary user out of the UI. The distinction this
gate defends is between an *answer* from the table and a *failure to get one*.

WHY A STATIC GATE
-----------------
The behaviour is pinned per site by unit tests, which is where it belongs. This
gate exists because the defect class came back six times after being fixed once:
each site was written by copying a neighbour, so a fix applied to the instance did
not reach the class. An AST rule is the only artifact that fails on the *seventh*
copy, written by someone who never read any of those tests.

WHAT IS SCANNED, AND WHAT THAT MISSES
-------------------------------------
Discovery is by **content**, not by a list of filenames: any module under
``nested/api-resolvers/src/lambda``, ``src/lambda`` or ``feature-platform`` that
performs a UsersTable scope query, or calls the shared
``resolve_allowed_config_versions``, is in scope — so a new consumer is covered the
moment it exists rather than when somebody remembers to add it here.

A scope query is recognised by the **table**, not by an index name: a
``.query(...)`` on an object built from ``USERS_TABLE_NAME`` (in any of its usual
spellings) counts, whatever index it names. Keying the recognition on the string
``EmailIndex`` was the wrong axis, because *getting that string wrong* is the
failure mode that produced the original bug — the chat processor queried a
``SubIndex`` no template ever declared, which made every lookup raise and, at the
time, fail open. A rule that only sees correctly-named queries cannot see the
instance the class is named after. The index-name leg is kept as a second,
independent signal for the case where the table object is built elsewhere.

Two limits worth stating rather than discovering:

* ``lib/idp_common_pkg`` is **not** scanned. That is where the canonical helper
  lives, so scanning it would be circular, but it also means a *second* consumer
  inside the library is unpoliced — ``idp_common/testset_scope.py`` is exactly that:
  a ninth ``EmailIndex`` consumer, for the independent ``allowedTestSets`` axis,
  carrying both forbidden shapes. Its polarity is inverted (an absent scope denies
  rather than admits), so the shapes are not live there — but widening
  ``SCAN_ROOTS`` to the library is the follow-up that would prove it rather than
  argue it.
* The rules are syntactic. They establish that no code *spells* the fail-open
  shapes; they cannot establish that the value reaching a matcher was resolved from
  the table. The per-site unit suites are what assert the behaviour.

The scan is bounded to those three subtrees, which is also why it needs no
gitignored-copy exclusion list: unlike the repo-wide walks in
``test_iam_privilege_escalation.py`` and its siblings, it cannot wander into
``scratch/`` or ``.claude/worktrees/``, so it behaves identically in a normal
checkout and in an agent worktree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Subtrees that can hold a deploy artifact resolving a caller's scope.
SCAN_ROOTS = (
    "nested/api-resolvers/src/lambda",
    "src/lambda",
    "feature-platform",
)

# A marker file that identifies the repo root, so this test asserts against THIS
# checkout rather than whatever happens to be installed.
_ROOT_MARKER = Path("lib/idp_common_pkg/idp_common/config_scope.py")

# How the UsersTable is named, in each spelling the tree uses. A `.query(...)` on an
# object built from one of these is a scope query whatever index it names — see the
# module docstring for why the table and not the index name is the right axis.
USERS_TABLE_ENV = "USERS_TABLE_NAME"
USERS_TABLE_NAMES = frozenset(
    {
        "USERS_TABLE_NAME",
        "_USERS_TABLE",
        "USERS_TABLE",
        "users_table_name",
        "users_table",
        "_users_table",
    }
)

# The UsersTable GSI a scope lookup reads — the second, independent recognition leg,
# for a query whose table object was built out of sight. Kept as a literal on
# purpose: the gate must not import the module it is policing, or a rename would move
# both sides at once and the rule would go quiet.
SCOPE_INDEX_NAME = "EmailIndex"
SCOPE_INDEX_CONSTANT = "USERS_TABLE_SCOPE_INDEX"

# The claim a scope lookup key may come from, and the only one.
SCOPE_KEY_CLAIM = "email"

# Claim names that are NOT an email address for every caller, so none of them may
# stand in as the lookup key. `callerSub` is here because it is how the original
# instance of this class spelled it — a request-body field, not a Cognito claim.
SUBSTITUTE_IDENTIFIER_CLAIMS = frozenset(
    {
        "sub",
        "callerSub",
        "cognito:username",
        "username",
        "preferred_username",
        "identities",
    }
)

# Names that identify a call as "resolve this caller's config-version scope".
LOOKUP_CALL_NAMES = frozenset(
    {
        "resolve_allowed_config_versions",
        "_get_user_allowed_config_versions",
        "_caller_allowed_versions",
        "_allowed_config_versions_for_event",
        "_caller_scope_or_deny",
    }
)

# The exception that says "this caller's scope could not be evaluated".
SCOPE_ERROR_NAME = "ScopeLookupError"

# Local names whose value IS the scope lookup key. Assigning an `or`-chain to one
# of these, or reading a claim other than `email` into one, is the substitution
# this gate exists to stop. `caller_sub` is included because the class's original
# instance bound the key under that name, and a rule that inspected only
# email-shaped names could not fire on the one site where the key *was* the sub.
SCOPE_KEY_TARGETS = frozenset(
    {
        "email",
        "caller_email",
        "_email",
        "user_email",
        "scope_key",
        "scope_email",
        "lookup_email",
        "caller_sub",
        "caller_id",
        "caller_key",
    }
)

# Callables whose whole purpose is to swallow an exception. A scope lookup inside one
# cannot fail closed, and no `except` clause appears for SCOPE3 to inspect.
SUPPRESSING_CONTEXT_MANAGERS = frozenset({"suppress", "nullcontext"})

# Names a refusal-returning helper goes by, and the status codes that are refusals.
REFUSAL_CALL_HINTS = ("unauthorized", "forbidden", "denied", "deny", "refuse")
REFUSAL_DICT_FALSE_KEYS = frozenset({"ok", "success", "allowed", "authorized"})
REFUSAL_DICT_KEYS = frozenset({"error", "errorType", "errors", "reason", "denied"})

# Directories with no deployed code in them.
_SKIP_DIR_PARTS = frozenset({"__pycache__", "node_modules", ".aws-sam", "build", "dist"})

# The two modules PR #1020 fixes, which this branch must not touch. Both carry the
# class's ORIGINAL instance: a lookup keyed on a request-body `callerSub`, against an
# index no template declares, with the failure caught and returned as "unrestricted".
# The rules below see all of it — which is the point of recognising a scope query by
# its table — so the findings are withheld here rather than the files being excluded
# from discovery.
#
# ⚠️ This entry is asserted to be NECESSARY by
# `test_the_pending_exemption_is_still_needed`. The moment those files comply, that
# test fails and tells you to delete the two paths below. An exemption that outlives
# its reason is how a gate goes quiet, so this one cannot.
PENDING_FIX = frozenset(
    {
        "src/lambda/chat_with_document_processor/index.py",
        "src/lambda/chat_stream_processor/vendored/chat_with_document_processor.py",
    }
)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.path}:{self.line} — {self.message}"


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / _ROOT_MARKER).is_file():
            return parent
    raise RuntimeError(f"Could not locate a repo root containing {_ROOT_MARKER}")


# --------------------------------------------------------------------------- #
# reading the tree
# --------------------------------------------------------------------------- #
def _python_files(root: Path):
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _SKIP_DIR_PARTS.intersection(path.parts):
                continue
            if path.name.startswith("test_"):
                continue
            yield path


def _is_claim_read(node: ast.AST) -> str | None:
    """The claim name a node reads, or None.

    Matches both `claims.get("email", "")` and `claims["email"]`, whatever the
    receiver is called — the receiver name carries no information here, and
    requiring one would make the rule dodgeable by renaming a variable.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        if isinstance(node.slice.value, str):
            return node.slice.value
    return None


def _claims_read_in(node: ast.AST) -> set[str]:
    return {
        claim
        for child in ast.walk(node)
        if (claim := _is_claim_read(child)) is not None
    }


def _unwrap(node: ast.AST) -> ast.AST:
    """Strip a coercion wrapper — `str(x)`, `(x)` — to get at the value."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in {"str", "unquote"} or not node.args:
            break
        node = node.args[0]
    return node


def _is_email_claim_read(node: ast.AST) -> bool:
    """Whether a node IS the email claim, rather than merely mentioning it.

    Directness matters. `claims.get("email") or identity.get("username")` is an
    or-chain *over* the claim and is the defect; `[v for v in (a, caller["email"])
    if v] or [SENTINEL]` merely contains an email-keyed read inside an unrelated
    expression, and flagging that would make the rule noise.
    """
    return _is_claim_read(_unwrap(node)) == SCOPE_KEY_CLAIM


def _is_constant_operand(node: ast.AST) -> bool:
    """A default, rather than a second source for the same value.

    `claims.get("email") or ""` coerces a missing claim to the empty string, which
    then denies. `claims.get("email") or identity.get("username")` reaches for a
    different identifier, which then matches no row. Only the first is allowed.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)) and not getattr(
        node, "elts", None
    ):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # `str("")`, `dict()`, `list()` — a constructed empty default.
        return node.func.id in {"str", "dict", "list", "set"} and all(
            _is_constant_operand(arg) for arg in node.args
        )
    return False


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _mentions_users_table(node: ast.AST) -> bool:
    """Whether an expression names the UsersTable, in any spelling the tree uses."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in USERS_TABLE_NAMES:
            return True
        if isinstance(child, ast.Attribute) and child.attr in USERS_TABLE_NAMES:
            return True
        if isinstance(child, ast.Constant) and child.value == USERS_TABLE_ENV:
            return True
    return False


def _users_table_bindings(tree: ast.AST) -> set[str]:
    """Names bound to a DynamoDB Table resource for the UsersTable.

    Collected module-wide rather than per-function, deliberately: the binding and the
    query can sit in different functions, and over-collecting here only widens the
    net, which for this gate is the safe direction.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Call) or _call_name(value) != "Table":
            continue
        if not any(_mentions_users_table(arg) for arg in value.args):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
            elif isinstance(target, ast.Attribute):
                bound.add(target.attr)
    return bound


def _is_scope_query(node: ast.AST, users_table_bindings: set[str]) -> bool:
    """A DynamoDB Query against the UsersTable.

    Recognised by the **table**, not the index: the receiver is a name bound from
    ``*.Table(<something naming USERS_TABLE_NAME>)``, or the expression itself names
    the table. Naming the wrong index is the failure mode that produced the original
    bug, so a rule that only matched the right index name could not see it.

    The declared index name is kept as an independent second leg, for a query whose
    table object was built out of this module's sight.
    """
    if not isinstance(node, ast.Call) or _call_name(node) != "query":
        return False

    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if receiver is not None:
        if isinstance(receiver, ast.Name) and receiver.id in users_table_bindings:
            return True
        if isinstance(receiver, ast.Attribute) and receiver.attr in (
            users_table_bindings
        ):
            return True
        if _mentions_users_table(receiver):
            return True

    for keyword in node.keywords:
        if keyword.arg != "IndexName":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and value.value == SCOPE_INDEX_NAME:
            return True
        if isinstance(value, ast.Name) and value.id == SCOPE_INDEX_CONSTANT:
            return True
    return False


def _performs_scope_lookup(node: ast.AST, users_table_bindings: set[str]) -> bool:
    for child in ast.walk(node):
        if _is_scope_query(child, users_table_bindings):
            return True
        if _call_name(child) in LOOKUP_CALL_NAMES:
            return True
    return False


def _suppresses_exceptions(node: ast.With | ast.AsyncWith) -> bool:
    """Whether a `with` block discards exceptions raised inside it.

    `contextlib.suppress` is the evasion this exists for: it defeats any rule that
    walks `ast.Try`, because there is no `try` and no `except` clause to inspect —
    only a `with`, and a scope lookup inside one cannot fail closed at all.
    """
    for item in node.items:
        if _call_name(item.context_expr) in SUPPRESSING_CONTEXT_MANAGERS:
            return True
    return False


def _handler_catches_scope_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    for child in ast.walk(handler.type):
        if isinstance(child, ast.Name) and child.id == SCOPE_ERROR_NAME:
            return True
        if isinstance(child, ast.Attribute) and child.attr == SCOPE_ERROR_NAME:
            return True
    return False


def _is_refusal_value(node: ast.expr | None) -> bool:
    """Whether a returned value denies the request, rather than answering it.

    A returned payload is only a refusal if it *says* so. Accepting any non-empty
    value was too loose by exactly the margin that matters: a 200 carrying
    ``{"rows": [], "total": 0}`` serves no data and so is fail-closed on the
    property that matters, yet it is indistinguishable from the truthful empty
    answer — and it passed a rule meant to police that shape.

    Three recognised forms, all of which name the denial:

    * a mapping with a falsy ``ok``/``success`` key, or an ``error``/``reason`` key
      — the in-band shape the configuration and sync resolvers use;
    * a call to a response builder whose first argument is a 4xx/5xx status;
    * a call to a helper whose name says refusal (``_unauthorized``, ``_forbidden``).
    """
    if node is None:
        return False

    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if key.value in REFUSAL_DICT_KEYS:
                return True
            if key.value in REFUSAL_DICT_FALSE_KEYS and isinstance(
                value, ast.Constant
            ):
                if value.value is False:
                    return True
        return False

    if isinstance(node, ast.Call):
        name = (_call_name(node) or "").lower()
        if any(hint in name for hint in REFUSAL_CALL_HINTS):
            return True
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, int
            ):
                if 400 <= argument.value <= 599:
                    return True
            if _is_refusal_value(argument):
                return True
        return False

    return False


def _terminates_in_a_refusal(body: list[ast.stmt]) -> bool:
    """Whether a statement list ends by refusing the request.

    The **terminal** statement must be the refusal. A `raise` anywhere in the body
    is not enough: `if <rare condition>: raise` followed by other statements leaves
    the ordinary path falling through, and falling off the end is the subtle
    fail-open — control resumes after the `try`, where the name the lookup would
    have bound is either unbound or still `None`, with no `return None` in sight.

    An `if` counts when *both* arms terminate, which is the legitimate "raise one of
    two errors" shape; an `if` with no `else` does not, because its fall-through is
    exactly the hole.
    """
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.Return):
        return _is_refusal_value(last.value)
    if isinstance(last, ast.If):
        return bool(last.orelse) and _terminates_in_a_refusal(
            last.body
        ) and _terminates_in_a_refusal(last.orelse)
    if isinstance(last, ast.Try):
        return _terminates_in_a_refusal(last.body) and all(
            _terminates_in_a_refusal(h.body) for h in last.handlers
        )
    return False


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #
def _check_key_provenance(path: str, tree: ast.AST) -> list[Finding]:
    """SCOPE1/SCOPE2 — the lookup key comes from the `email` claim, or nothing."""
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if not any(_is_email_claim_read(operand) for operand in node.values):
                continue
            for operand in node.values:
                if _is_email_claim_read(operand):
                    continue
                if _is_constant_operand(operand):
                    continue
                findings.append(
                    Finding(
                        "SCOPE1",
                        path,
                        operand.lineno,
                        "an `or` fallback beside the `email` claim: an identifier "
                        "that is not an email matches no UsersTable row, and the "
                        "empty page reads as 'unrestricted'. Deny instead — "
                        "resolve the key with caller_email_from_claims().",
                    )
                )

        if isinstance(node, ast.Assign):
            targets = {
                t.id for t in node.targets if isinstance(t, ast.Name)
            } | {
                t.attr for t in node.targets if isinstance(t, ast.Attribute)
            }
            if not targets & SCOPE_KEY_TARGETS:
                continue
            if isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or):
                if not all(
                    _is_email_claim_read(operand) or _is_constant_operand(operand)
                    for operand in node.value.values
                ):
                    findings.append(
                        Finding(
                            "SCOPE1",
                            path,
                            node.lineno,
                            f"{sorted(targets & SCOPE_KEY_TARGETS)} is the scope "
                            "lookup key and is assigned an `or` fallback chain",
                        )
                    )
            substituted = (
                _claims_read_in(node.value)
                - {SCOPE_KEY_CLAIM}
            ) & SUBSTITUTE_IDENTIFIER_CLAIMS
            if substituted:
                findings.append(
                    Finding(
                        "SCOPE2",
                        path,
                        node.lineno,
                        f"the scope lookup key is derived from {sorted(substituted)} "
                        f"rather than the {SCOPE_KEY_CLAIM!r} claim alone",
                    )
                )

    findings.extend(_check_nested_get_defaults(path, tree))
    return findings


def _check_nested_get_defaults(path: str, tree: ast.AST) -> list[Finding]:
    """SCOPE1, written as a nested default rather than an `or`.

    `claims.get("email", claims.get("cognito:username", claims.get("sub", "")))` is
    the same fallback chain as the `or` form and has the same consequence, but it
    produces no `BoolOp`, so a rule that looks for one never sees it. The signal here
    is structural and independent of which claim is outermost: a claim read whose
    **default** argument reads another claim.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if _is_claim_read(node) is None or not isinstance(node, ast.Call):
            continue
        if len(node.args) < 2:
            continue
        nested = _claims_read_in(node.args[1])
        if not nested:
            continue
        findings.append(
            Finding(
                "SCOPE1",
                path,
                node.lineno,
                f"a claim read defaults to another claim {sorted(nested)} — the same "
                "fallback chain as an `or`, with the same consequence: an identifier "
                "that is not an email matches no UsersTable row, and the empty page "
                "reads as 'unrestricted'.",
            )
        )
    return findings


def _check_failure_denies(
    path: str, tree: ast.AST, users_table_bindings: set[str]
) -> list[Finding]:
    """SCOPE3 — a lookup that cannot answer denies; it never returns unrestricted."""
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if not _suppresses_exceptions(node):
                continue
            if not any(
                _performs_scope_lookup(stmt, users_table_bindings)
                for stmt in node.body
            ):
                continue
            findings.append(
                Finding(
                    "SCOPE3",
                    path,
                    node.lineno,
                    "a config-version scope lookup inside a suppressing context "
                    "manager. The failure is discarded, so the lookup cannot fail "
                    "closed and there is no `except` clause to inspect — resolve "
                    "the scope outside it and refuse on ScopeLookupError.",
                )
            )
            continue

        if not isinstance(node, ast.Try):
            continue
        guards_lookup = any(
            _performs_scope_lookup(stmt, users_table_bindings) for stmt in node.body
        )
        for handler in node.handlers:
            if not (guards_lookup or _handler_catches_scope_error(handler)):
                continue
            if _terminates_in_a_refusal(handler.body):
                continue
            findings.append(
                Finding(
                    "SCOPE3",
                    path,
                    handler.lineno,
                    "an except-handler around a config-version scope lookup does "
                    "not END in a refusal. 'Cannot evaluate' is not "
                    "'unrestricted' — the handler's last statement must raise, or "
                    "return a value that names the denial (an `error` key, a falsy "
                    "`ok`/`success`, a 4xx response). A conditional raise, or a "
                    "success-shaped payload, leaves the ordinary path open.",
                )
            )

    return findings


def scan(root: Path) -> tuple[list[Path], list[Finding]]:
    """Every module that resolves a caller's scope, and every rule it breaks.

    Returned as a pair so the caller can assert on the discovered set as well as
    the findings: a scan that discovers nothing proves nothing, and that is the
    failure mode a filename-based version of this gate would hide.

    Findings from ``PENDING_FIX`` paths are **not** filtered out here. They are
    returned like any other, and only the assertions below set them aside — so the
    exemption stays measurable, which is what lets
    ``test_the_pending_exemption_is_still_needed`` insist on its own removal.
    """
    discovered: list[Path] = []
    findings: list[Finding] = []

    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        bindings = _users_table_bindings(tree)
        if not _performs_scope_lookup(tree, bindings):
            continue
        discovered.append(path)
        rel = path.relative_to(root).as_posix()
        findings.extend(_check_key_provenance(rel, tree))
        findings.extend(_check_failure_denies(rel, tree, bindings))

    return discovered, findings


def _enforced(findings: list[Finding], rules: set[str]) -> list[Finding]:
    """The findings the gate fails on: the named rules, outside ``PENDING_FIX``."""
    return [f for f in findings if f.rule in rules and f.path not in PENDING_FIX]


# --------------------------------------------------------------------------- #
# the assertions
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def scanned():
    return scan(repo_root())


def test_the_scan_finds_the_consumers_it_is_meant_to_police(scanned):
    """A gate that discovers nothing passes for the wrong reason.

    Discovery counting a module is not the same as the rules examining it — see
    ``test_the_pending_exemption_is_still_needed`` for the two that are discovered
    and set aside.
    """
    discovered, _ = scanned
    names = {path.parent.name for path in discovered}

    assert len(discovered) >= 8, (
        "the scope-lookup scan found fewer modules than the tree contains — the "
        f"discovery predicate has gone stale. Found: {sorted(names)}"
    )
    # Named not as an inventory to maintain, but because these five are where the
    # rule is enforced on five different entry paths; losing one silently is the
    # failure this assertion is for.
    for expected in (
        "configuration_resolver",
        "list_documents_gsi_resolver",
        "list_documents_range_resolver",
        "user_management",
        "feature-api",
    ):
        assert expected in names, f"{expected} no longer looks like a scope consumer"


def test_no_scope_lookup_key_has_a_fallback_beside_the_email_claim(scanned):
    """SCOPE1/SCOPE2. See the module docstring for why a fallback is fail-open."""
    _, findings = scanned
    offenders = _enforced(findings, {"SCOPE1", "SCOPE2"})

    assert not offenders, "\n".join(["scope key provenance:", *map(str, offenders)])


def test_no_scope_lookup_failure_is_read_as_unrestricted(scanned):
    """SCOPE3. See the module docstring for why a caught failure is fail-open."""
    _, findings = scanned
    offenders = _enforced(findings, {"SCOPE3"})

    assert not offenders, "\n".join(
        ["scope lookup failure handling:", *map(str, offenders)]
    )


def test_the_pending_exemption_is_still_needed(scanned):
    """Each ``PENDING_FIX`` entry must still be non-compliant, or be deleted.

    The two chat modules carry the class's original instance and belong to a
    concurrent change, so their findings are set aside rather than the files being
    hidden from discovery. An exemption nobody revisits is how a gate goes quiet, so
    this asserts the exemption is *load-bearing*: once those files comply, this test
    fails and the only correct response is to delete the entry it names.
    """
    _, findings = scanned
    by_path: dict[str, list[Finding]] = {}
    for finding in findings:
        by_path.setdefault(finding.path, []).append(finding)

    for path in sorted(PENDING_FIX):
        assert by_path.get(path), (
            f"{path} no longer breaks any rule, so its PENDING_FIX entry is dead "
            "config. Delete it from PENDING_FIX in this file — the gate then "
            "enforces the rules on it like every other consumer."
        )


# --------------------------------------------------------------------------- #
# the rules themselves, pinned against both shapes
# --------------------------------------------------------------------------- #
# Without these, the rules above could go inert — pass because they match nothing
# rather than because the tree is clean — and nothing in the live repo would say
# so once every real site is compliant.

_FALLBACK_CHAIN = '''
def _caller(event):
    claims = event["identity"]["claims"]
    email = claims.get("email", "") or identity.get("username", "") or username
    return email


def lookup(email):
    return table.query(IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email))
'''

_SWALLOWED_FAILURE = '''
def lookup(email):
    try:
        resp = table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
        )
    except Exception as e:
        logger.warning("scope lookup failed: %s", e)
        return None
    return resp.get("Items")
'''

_FALL_THROUGH_FAILURE = '''
def lookup(email):
    result = None
    try:
        resp = table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
        )
        result = resp.get("Items")
    except Exception as e:
        logger.warning("scope lookup failed: %s", e)
    return result
'''

_CONSUMER_SWALLOWS_REFUSAL = '''
def handler(event):
    try:
        allowed = resolve_allowed_config_versions(caller_email(event))
    except ScopeLookupError:
        allowed = None
    return allowed
'''

# Each of the six below is an evasion that an earlier draft of these rules let
# through. They are kept as fixtures rather than described in prose because a rule is
# only as good as the shape it is measured against.

_NESTED_GET_DEFAULTS = '''
def _caller(event):
    claims = event["identity"]["claims"]
    email = claims.get("email", claims.get("cognito:username", claims.get("sub", "")))
    return email


def lookup(email):
    return users_table.query(KeyConditionExpression=Key("email").eq(email))
'''

_SUPPRESSED_FAILURE = '''
def lookup(email):
    scope = None
    with contextlib.suppress(Exception):
        resp = users_table.query(KeyConditionExpression=Key("email").eq(email))
        scope = resp.get("Items")
    return scope
'''

_REFUSAL_SHAPED_SUCCESS = '''
def handler(event):
    try:
        allowed = resolve_allowed_config_versions(caller_email(event))
    except ScopeLookupError:
        logger.warning("scope lookup failed — returning an empty report")
        return _response(200, {"rows": [], "total": 0, "totalPiiRedacted": 0})
    return allowed
'''

_CONDITIONAL_RAISE_THEN_FALLS_THROUGH = '''
def handler(event):
    allowed = None
    try:
        allowed = resolve_allowed_config_versions(caller_email(event))
    except ScopeLookupError as e:
        if _STRICT:
            raise PermissionError("Unauthorized") from e
        logger.warning("scope lookup failed: %s", e)
    return allowed
'''

_WRONG_INDEX_ON_THE_USERS_TABLE = '''
def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        resp = users_table.query(
            IndexName="SubIndex", KeyConditionExpression=Key("sub").eq(caller_sub)
        )
    except Exception as e:
        logger.error("scope lookup FAILED OPEN: %s", e)
        return None
    return resp.get("Items")
'''

_BODY_SUPPLIED_KEY = '''
def handler(event):
    caller_sub = event.get("callerSub") or ""
    return _get_user_allowed_config_versions(caller_sub)
'''

_COMPLIANT = '''
def _caller_email(claims):
    return str(claims.get("email") or "")


def lookup(email):
    if not email:
        raise ScopeLookupError("no email claim")
    try:
        resp = table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
    return resp.get("Items")


def handler(event):
    try:
        return lookup(_caller_email(event["identity"]["claims"]))
    except ScopeLookupError as e:
        raise PermissionError("Unauthorized") from e
'''

_COMPLIANT_IN_BAND_DENIAL = '''
def handler(event):
    try:
        allowed = resolve_allowed_config_versions(caller_email(event))
    except ScopeLookupError as e:
        logger.error("denying: %s", e)
        return {"success": False, "error": {"type": "Unauthorized"}}
    return allowed
'''

_COMPLIANT_RESPONSE_DENIAL = '''
def handler(event):
    try:
        allowed = _caller_allowed_versions(_caller_email(event))
    except ScopeLookupError:
        return _response(403, {"error": "Access denied: could not verify scope."})
    return allowed
'''


def _findings_for(source: str) -> list[Finding]:
    tree = ast.parse(source)
    bindings = _users_table_bindings(tree)
    return _check_key_provenance("snippet.py", tree) + _check_failure_denies(
        "snippet.py", tree, bindings
    )


@pytest.mark.parametrize(
    "source,rule",
    [
        (_FALLBACK_CHAIN, "SCOPE1"),
        (_NESTED_GET_DEFAULTS, "SCOPE1"),
        (_BODY_SUPPLIED_KEY, "SCOPE1"),
        (_BODY_SUPPLIED_KEY, "SCOPE2"),
        (_SWALLOWED_FAILURE, "SCOPE3"),
        (_FALL_THROUGH_FAILURE, "SCOPE3"),
        (_CONSUMER_SWALLOWS_REFUSAL, "SCOPE3"),
        (_SUPPRESSED_FAILURE, "SCOPE3"),
        (_REFUSAL_SHAPED_SUCCESS, "SCOPE3"),
        (_CONDITIONAL_RAISE_THEN_FALLS_THROUGH, "SCOPE3"),
        (_WRONG_INDEX_ON_THE_USERS_TABLE, "SCOPE3"),
    ],
    ids=[
        "or-chain-key",
        "nested-get-defaults",
        "body-supplied-key-or-chain",
        "body-supplied-key-substitute-claim",
        "except-returns-None",
        "except-falls-through",
        "consumer-swallows",
        "contextlib-suppress",
        "refusal-shaped-success-payload",
        "conditional-raise-then-falls-through",
        "wrong-index-on-the-users-table",
    ],
)
def test_the_rule_catches_the_shape_it_is_for(source, rule):
    assert rule in {f.rule for f in _findings_for(source)}


@pytest.mark.parametrize(
    "source",
    [_COMPLIANT, _COMPLIANT_IN_BAND_DENIAL, _COMPLIANT_RESPONSE_DENIAL],
    ids=["raises", "in-band-Unauthorized", "403-response"],
)
def test_the_compliant_shapes_are_accepted(source):
    """A default of "" beside the email claim is a coercion, not a fallback.

    And a refusal may be *returned* rather than raised, in any of the three shapes
    this tree actually uses — as long as the value names the denial.
    """
    assert _findings_for(source) == []


def test_an_empty_page_returning_unrestricted_is_not_a_finding():
    """Deliberate, and must stay: scoping is opt-in per user."""
    source = '''
def lookup(email):
    resp = table.query(
        IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
    )
    items = resp.get("Items") or []
    if not items:
        return None  # no scope row for this user -> unrestricted
    return items[0].get("allowedConfigVersions")
'''
    assert _findings_for(source) == []


def test_a_users_table_query_is_recognised_whatever_index_it_names():
    """Getting the index name wrong is what produced the original bug."""
    source = '''
def lookup(key):
    users_table = _dynamodb.Table(os.environ["USERS_TABLE_NAME"])
    return users_table.query(IndexName="AnyIndexAtAll")
'''
    tree = ast.parse(source)
    bindings = _users_table_bindings(tree)

    assert "users_table" in bindings
    assert _performs_scope_lookup(tree, bindings)


def test_a_query_on_an_unrelated_table_is_not_a_scope_lookup():
    """The rules must not fire on every indexed query in a discovered module."""
    source = '''
def list_documents():
    tracking = dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])
    try:
        return tracking.query(IndexName="TypeDateIndex")
    except Exception as e:
        logger.warning("query failed: %s", e)
        return []
'''
    tree = ast.parse(source)

    assert _findings_for(source) == []
    assert not _performs_scope_lookup(tree, _users_table_bindings(tree))
