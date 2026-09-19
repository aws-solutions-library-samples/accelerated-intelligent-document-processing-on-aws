# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A config-version scope lookup must key on the ``email`` claim and fail closed.

``allowedConfigVersions`` is the per-user restriction that decides which
Configuration Profiles a caller may read or edit and which *documents* they may
see. Eight separate deploy artifacts resolve it, each with its own copy of the
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
performs a UsersTable scope query — an ``IndexName`` of ``EmailIndex`` (or the
``USERS_TABLE_SCOPE_INDEX`` constant), or a call to the shared
``resolve_allowed_config_versions`` — is in scope, so a new consumer is covered the
moment it exists rather than when somebody remembers to add it here.

That predicate has one honest blind spot, recorded rather than papered over: a
``Query`` naming some *other* index is not recognised as a scope query, so the
failure-handling rule below does not police the handler around it. That is not
hypothetical — the chat processor queried a ``SubIndex`` that no template ever
declared, which made every lookup raise and, at the time, fail open. The guard for
*that* shape is the "the index the code names is a declared GSI keyed on the
declared attribute" test that ships beside the chat processor; this gate is about
what a lookup does once it names the right index.

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

# The UsersTable GSI a scope lookup reads. Kept as a literal here on purpose: the
# gate must not import the module it is policing, or a rename would move both
# sides at once and the rule would go quiet.
SCOPE_INDEX_NAME = "EmailIndex"
SCOPE_INDEX_CONSTANT = "USERS_TABLE_SCOPE_INDEX"

# The claim a scope lookup key may come from, and the only one.
SCOPE_KEY_CLAIM = "email"

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
# this gate exists to stop.
SCOPE_KEY_TARGETS = frozenset(
    {"email", "caller_email", "_email", "scope_key", "scope_email", "lookup_email"}
)

# Directories with no deployed code in them.
_SKIP_DIR_PARTS = frozenset({"__pycache__", "node_modules", ".aws-sam", "build", "dist"})


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


def _is_scope_query(node: ast.AST) -> bool:
    """A DynamoDB Query against the UsersTable scope index."""
    if not isinstance(node, ast.Call) or _call_name(node) != "query":
        return False
    for keyword in node.keywords:
        if keyword.arg != "IndexName":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and value.value == SCOPE_INDEX_NAME:
            return True
        if isinstance(value, ast.Name) and value.id == SCOPE_INDEX_CONSTANT:
            return True
    return False


def _performs_scope_lookup(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if _is_scope_query(child):
            return True
        if _call_name(child) in LOOKUP_CALL_NAMES:
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


def _terminates_in_a_refusal(handler: ast.ExceptHandler) -> bool:
    """Whether an except-handler refuses the request rather than continuing.

    A `raise` refuses. So does returning a value that is not one of the sentinels
    this codebase reads as "unrestricted" — `None`, an empty list, a bare
    `return`. Falling off the end of the handler is the subtle failure: control
    resumes after the `try`, where the name the lookup would have bound is either
    unbound or still `None`, which is the fail-open again with no `return None` in
    sight.
    """
    for statement in handler.body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Raise):
                return True
    last = handler.body[-1] if handler.body else None
    if isinstance(last, ast.Return):
        if last.value is None:
            return False
        if isinstance(last.value, ast.Constant) and last.value.value is None:
            return False
        if isinstance(last.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and not (
            getattr(last.value, "elts", None) or getattr(last.value, "keys", None)
        ):
            return False
        return True
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
            other_claims = _claims_read_in(node.value) - {SCOPE_KEY_CLAIM}
            substituted = other_claims & {
                "sub",
                "cognito:username",
                "username",
                "preferred_username",
                "identities",
            }
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

    return findings


def _check_failure_denies(path: str, tree: ast.AST) -> list[Finding]:
    """SCOPE3 — a lookup that cannot answer denies; it never returns unrestricted."""
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        guards_lookup = any(_performs_scope_lookup(stmt) for stmt in node.body)
        for handler in node.handlers:
            if not (guards_lookup or _handler_catches_scope_error(handler)):
                continue
            if _terminates_in_a_refusal(handler):
                continue
            findings.append(
                Finding(
                    "SCOPE3",
                    path,
                    handler.lineno,
                    "an except-handler around a config-version scope lookup does "
                    "not refuse the request. 'Cannot evaluate' is not "
                    "'unrestricted' — raise ScopeLookupError, or return a denial.",
                )
            )

    return findings


def scan(root: Path) -> tuple[list[Path], list[Finding]]:
    """Every module that resolves a caller's scope, and every rule it breaks.

    Returned as a pair so the caller can assert on the discovered set as well as
    the findings: a scan that discovers nothing proves nothing, and that is the
    failure mode a filename-based version of this gate would hide.
    """
    discovered: list[Path] = []
    findings: list[Finding] = []

    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if not _performs_scope_lookup(tree):
            continue
        discovered.append(path)
        rel = path.relative_to(root).as_posix()
        findings.extend(_check_key_provenance(rel, tree))
        findings.extend(_check_failure_denies(rel, tree))

    return discovered, findings


# --------------------------------------------------------------------------- #
# the assertions
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def scanned():
    return scan(repo_root())


def test_the_scan_finds_the_consumers_it_is_meant_to_police(scanned):
    """A gate that discovers nothing passes for the wrong reason."""
    discovered, _ = scanned
    names = {path.parent.name for path in discovered}

    assert len(discovered) >= 6, (
        "the scope-lookup scan found fewer modules than the tree contains — the "
        f"discovery predicate has gone stale. Found: {sorted(names)}"
    )
    # Named not as an inventory to maintain, but because these four are where the
    # rule is enforced on four different transports; losing one silently is the
    # failure this assertion is for.
    for expected in (
        "configuration_resolver",
        "list_documents_gsi_resolver",
        "list_documents_range_resolver",
        "feature-api",
    ):
        assert expected in names, f"{expected} no longer looks like a scope consumer"


def test_no_scope_lookup_key_has_a_fallback_beside_the_email_claim(scanned):
    """SCOPE1/SCOPE2. See the module docstring for why a fallback is fail-open."""
    _, findings = scanned
    offenders = [f for f in findings if f.rule in {"SCOPE1", "SCOPE2"}]

    assert not offenders, "\n".join(["scope key provenance:", *map(str, offenders)])


def test_no_scope_lookup_failure_is_read_as_unrestricted(scanned):
    """SCOPE3. See the module docstring for why a caught failure is fail-open."""
    _, findings = scanned
    offenders = [f for f in findings if f.rule == "SCOPE3"]

    assert not offenders, "\n".join(["scope lookup failure handling:", *map(str, offenders)])


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


def _findings_for(source: str) -> list[Finding]:
    tree = ast.parse(source)
    return _check_key_provenance("snippet.py", tree) + _check_failure_denies(
        "snippet.py", tree
    )


@pytest.mark.parametrize(
    "source,rule",
    [
        (_FALLBACK_CHAIN, "SCOPE1"),
        (_SWALLOWED_FAILURE, "SCOPE3"),
        (_FALL_THROUGH_FAILURE, "SCOPE3"),
        (_CONSUMER_SWALLOWS_REFUSAL, "SCOPE3"),
    ],
    ids=["or-chain-key", "except-returns-None", "except-falls-through", "consumer-swallows"],
)
def test_the_rule_catches_the_shape_it_is_for(source, rule):
    assert rule in {f.rule for f in _findings_for(source)}


def test_the_compliant_shape_is_accepted(source=_COMPLIANT):
    """A default of "" beside the email claim is a coercion, not a fallback."""
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
