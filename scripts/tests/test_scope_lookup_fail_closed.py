# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A config-version scope lookup must key each identifier correctly and fail closed.

``allowedConfigVersions`` is the per-user restriction that decides which
Configuration Profiles a caller may read or edit and which *documents* they may
see. Discovery finds 14 modules in the scanned subtrees that resolve it: the
canonical implementation in ``idp_common``, ten independent artifacts, and three
vendored copies. Each artifact had grown its own copy of the same three lines, and
the copies had drifted into two fail-open shapes:

1. **The lookup key came from an ``or``-chain of claims.** A ``UsersTable`` row is
   reached by one of exactly two keys, and they are **disjoint key spaces** on the
   same table:

   * the ``email`` claim, as the hash key of an ``EmailIndex`` query;
   * the immutable Cognito ``sub``, as the base key of a ``SUB#<sub>`` pointer item
     carrying the row's ``userId``.

   Neither may stand in for the other, and no third identifier may stand in for
   either. Putting a ``sub``, a ``cognito:username`` or an adapter-substituted
   username to the email-keyed index does not find the row by another route: it
   matches **no** row, an empty page is indistinguishable from "this user has no
   restriction", and the caller proceeds unrestricted. No AWS fault is required for
   that — only claims that differ from the ones the code was tested against. This
   is the class the ``SubIndex`` instance is named after, and the reason the
   ``sub`` now has a key space of its own is precisely that it must stop being
   spelled as a *substitute* for the email.

2. **A failed read was caught and turned into "unrestricted".** A missing IAM
   grant, a wrong index name or a throttle then switched the control off, silently,
   for every scoped caller — which is the drift the control exists to survive. Both
   key spaces are covered: a ``get_item`` on the UsersTable is a scope read exactly
   as a ``query`` is, so a swallowed pointer failure fails this gate too.

What is **not** a defect, and must not be "fixed" into one: an **empty page** still
means unrestricted. Scoping is opt-in per user, so most users have no row, and
denying there would lock every ordinary user out of the UI. The distinction this
gate defends is between an *answer* from the table and a *failure to get one*. (A
``sub`` that no pointer records while the caller carries no email is on the other
side of that line — there is no second key to try, so it is a failure to get an
answer, and the per-site suites assert it denies.)

WHY A STATIC GATE
-----------------
The behaviour is pinned per site by unit tests, which is where it belongs. This
gate exists because the defect class came back six times after being fixed once:
each site was written by copying a neighbour, so a fix applied to the instance did
not reach the class. An AST rule is the only artifact that fails on the *seventh*
copy, written by someone who never read any of those tests.

WHAT IS SCANNED, AND WHAT THAT MISSES
-------------------------------------
Discovery is by **content**, not by a list of filenames: any module under one of
``SCAN_ROOTS`` that performs a UsersTable scope read, or calls the shared
``resolve_allowed_config_versions``, is in scope — so a new consumer is covered the
moment it exists rather than when somebody remembers to add it here.

A scope read is recognised by the **table**, not by an index name: a ``.query(...)``
or ``.get_item(...)`` on an object built from ``USERS_TABLE_NAME`` (in any of its
usual spellings) counts, whatever index it names. Keying the recognition on the
string ``EmailIndex`` was the wrong axis, because *getting that string wrong* is the
failure mode that produced the original bug — the chat processor queried a
``SubIndex`` no template ever declared, which made every lookup raise and, at the
time, fail open. A rule that only sees correctly-named queries cannot see the
instance the class is named after. The index-name leg is kept as a second,
independent signal for the case where the table object is built elsewhere.

``get_item`` is in that set because the ``sub`` key space is addressed by the base
key, not by an index: a lookup that read the pointer and swallowed the failure would
otherwise be invisible to every rule here, which is the quietest way for this gate
to stop covering half of what it polices.

Three limits worth stating rather than discovering:

* The rules are **syntactic**. They establish that no code *spells* the fail-open
  shapes; they cannot establish that the value reaching a matcher was resolved from
  the table, nor which of two identifiers a given expression is holding. The
  per-site unit suites are what assert the behaviour.
* The **low-level client's string** key condition (``KeyConditionExpression="email =
  :e"``) is outside every key-provenance rule: the value arrives through
  ``ExpressionAttributeValues`` and the rules read expressions, not strings. Such a
  query is still *discovered*, so failure handling is covered; the key it puts to
  ``EmailIndex`` is not.
* One consumer is carried in ``PENDING_FIX`` rather than enforced:
  ``idp_common/testset_scope.py``, for the independent ``allowedTestSets`` axis. See
  that entry for why its shapes are not live and what would remove it.

The scan is bounded to the ``SCAN_ROOTS`` subtrees, which is also why it needs no
gitignored-copy exclusion list: unlike the repo-wide walks in
``test_iam_privilege_escalation.py`` and its siblings, it cannot wander into
``scratch/`` or ``.claude/worktrees/``, so it behaves identically in a normal
checkout and in an agent worktree.
"""

from __future__ import annotations

import ast
import functools
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

pytestmark = pytest.mark.unit

# Subtrees that can hold code resolving a caller's scope. `lib/idp_common_pkg` is in
# the list because the **canonical** lookup lives there and every other consumer
# imports it: leaving it out meant the one implementation that matters had no rule
# applied to it, so a fail-open introduced there passed this gate and every per-site
# suite at once. It also brings `idp_common/testset_scope.py` into view — a second
# `EmailIndex` consumer, for the independent `allowedTestSets` axis — which is carried
# in `PENDING_FIX` below with its rules named.
SCAN_ROOTS = (
    "nested/api-resolvers/src/lambda",
    "src/lambda",
    "feature-platform",
    "lib/idp_common_pkg/idp_common",
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

# The DynamoDB reads that can resolve a caller's scope. One per key space: `query`
# for the `EmailIndex` GSI, `get_item` for the `SUB#<sub>` pointer, which is
# addressed by the table's base key and so names no index at all.
#
# ⚠️ `scan` and `batch_get_item` are NOT here, and a module whose only UsersTable read
# is one of those escapes **discovery** — so no rule applies to it at all, which is the
# quietest failure this gate has. Neither shape appears in the tree today (a `Scan` to
# resolve one caller's scope would be a performance defect an author would notice), and
# adding them needs care: `user_management` scans the table legitimately, in the
# back-fill, so `scan` would discover a writer and every failure-handling rule would
# then apply to code that is not a lookup.
_SCOPE_READ_CALLS = frozenset({"query", "get_item"})

# The two claims a scope lookup key may come from, one per key space, and the only
# ones. The prefix is how the `sub` key space is addressed — see SCOPE4.
SCOPE_KEY_CLAIM = "email"
SCOPE_SUB_CLAIM = "sub"
SUB_POINTER_PREFIX = "SUB#"

# The names the tree uses for those two claim strings, mapped to the claim each one
# holds. Four consumers ship without an `idp_common` layer and restate the rule with
# their own module constants, so a rule that recognised only a literal `"email"` /
# `"sub"` saw nothing in exactly the files most likely to drift. Kept as literals
# here on purpose: importing the modules under inspection would move both sides of
# the comparison together, and a rename would then silence the rule rather than fail
# it. `USERS_TABLE_SCOPE_KEY` is the *attribute* name, which is the same string.
CLAIM_CONSTANTS = {
    "SCOPE_KEY_CLAIM": SCOPE_KEY_CLAIM,
    "USERS_TABLE_SCOPE_KEY": SCOPE_KEY_CLAIM,
    "SCOPE_SUB_CLAIM": SCOPE_SUB_CLAIM,
    "USERS_TABLE_SUB_CLAIM": SCOPE_SUB_CLAIM,
}

# Claim names that are NOT an identifier either key space indexes, so none of them
# may stand in for either key. `callerSub` is here because it is how the original
# instance of this class spelled it — a request-body field chosen by the caller being
# restricted, not a Cognito claim — and it stays forbidden now that `sub` is a
# legitimate key, because *where the value comes from* is the whole difference.
#
# ⚠️ `sub` is deliberately NOT in this set. It has a key space of its own, which is
# what makes it a second route to the row rather than a substitute for the first. It
# remains forbidden as a *fallback* (see FALLBACK_FORBIDDEN_CLAIMS) and as the value
# put to an email-keyed condition (see SCOPE4) — those are the shapes that fail open,
# and they are the ones this gate is for.
SUBSTITUTE_IDENTIFIER_CLAIMS = frozenset(
    {
        "callerSub",
        "cognito:username",
        "username",
        "preferred_username",
        "identities",
    }
)

# Claims that may never appear as a *fallback* for one another on one name. Two
# identifiers assigned to the same local, or chained with `or`, is a single key with
# two sources — and only one of them can be right for the key space it reaches. This
# includes `sub`, which is legitimate in its own slot and never as a stand-in.
FALLBACK_FORBIDDEN_CLAIMS = SUBSTITUTE_IDENTIFIER_CLAIMS | {SCOPE_SUB_CLAIM}

# Every key name that identifies a *caller*. The nested-default rule is gated on
# this set, because `dict.get(a, dict.get(b, c))` is an extremely common shape that
# has nothing to do with identity — Bedrock stream-error handling spells
# `event.get("internalServerException", event.get("throttlingException", {}))` — and
# flagging it made a false SCOPE1 finding that kept a stale exemption alive. Gating on
# the *claim names* rather than on the receiver's spelling keeps `_is_claim_read`'s
# property that a rule cannot be dodged by renaming a variable.
IDENTITY_CLAIMS = FALLBACK_FORBIDDEN_CLAIMS | {SCOPE_KEY_CLAIM}

# Names that identify a call as "resolve this caller's config-version scope" when the
# callee is NOT defined in the module under inspection — the shared helper, imported
# from `idp_common.config_scope`, plus the wrapper names this tree happens to use.
#
# ⚠️ This list is a fallback, not the mechanism. A module's own scope-resolving
# functions are found by **analysis** (`_scope_resolving_functions`): any local
# function that performs a UsersTable query, transitively, counts — whatever it is
# called. Depending on the spelling would make the rule dodgeable by renaming a
# private wrapper, which is precisely the property `_is_claim_read` gives up the
# receiver name to avoid.
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

# Local names whose value IS a scope lookup key, split by the key space each one
# reaches. Assigning an `or`-chain to any of them, or reading the *other* key space's
# claim into one, is the substitution this gate exists to stop.
#
# The split is what lets the `sub` be read at all: `caller_sub = claims["sub"]` is
# correct and `caller_email = claims["sub"]` is the bug, and a single set could not
# tell them apart. The generic names (`scope_key`, `caller_id`, `caller_key`) stay on
# the email side, so a future author who means the sub has to say so by naming it.
EMAIL_KEY_TARGETS = frozenset(
    {
        "email",
        "caller_email",
        "_email",
        "user_email",
        "scope_key",
        "scope_email",
        "lookup_email",
        "caller_id",
        "caller_key",
    }
)

# `caller_sub` is here rather than deleted because the class's original instance
# bound the key under that name from a *request body*, and the rules below must still
# fire on that: a name in this set has to come from the verified `sub` claim.
SUB_KEY_TARGETS = frozenset({"caller_sub", "cognito_sub", "sub_key", "user_sub"})

SCOPE_KEY_TARGETS = EMAIL_KEY_TARGETS | SUB_KEY_TARGETS

# Callables whose whole purpose is to swallow an exception. A scope lookup inside one
# cannot fail closed, and no `except` clause appears for SCOPE3 to inspect.
SUPPRESSING_CONTEXT_MANAGERS = frozenset({"suppress", "nullcontext"})

# Names a refusal-returning helper goes by, and the status codes that are refusals.
REFUSAL_CALL_HINTS = ("unauthorized", "forbidden", "denied", "deny", "refuse")
REFUSAL_DICT_FALSE_KEYS = frozenset({"ok", "success", "allowed", "authorized"})
REFUSAL_DICT_KEYS = frozenset({"error", "errorType", "errors", "reason", "denied"})

# Directories with no deployed code in them.
_SKIP_DIR_PARTS = frozenset({"__pycache__", "node_modules", ".aws-sam", "build", "dist"})

# `try:` and `try: ... except*:` are distinct node types. Every rule that inspects
# exception handling has to name both, or `except* Exception:` — which swallows a
# failure exactly as `except Exception:` does — is outside the rule's universe. Same
# class of hole as a lookup inside `contextlib.suppress`.
_TRY_NODES: tuple[type, ...] = (
    (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
)

# Consumers the gate DISCOVERS but does not enforce. The findings are withheld here
# rather than the files being excluded from discovery — which is the point of
# recognising a scope read by its table: an exempt file breaking a rule the exemption
# does not name still fails.
#
# Keyed by path to the SPECIFIC rules the exemption is for, not to the file. Two
# consequences, both deliberate:
#
#   * a finding from any other rule in an exempt file still fails the gate, so an
#     exemption cannot quietly grow into a blanket one;
#   * `test_the_pending_exemption_is_still_needed` requires every rule named here to
#     still fire. A file-scoped "any finding at all" check does not work: a single
#     unrelated finding — a false positive is enough — keeps the entry looking
#     necessary forever, and the file then sits outside enforcement permanently,
#     which is the recurring defect this gate exists to prevent. It is also why
#     `SCOPE5` exists: a rule that misreports a *correct* module is a false finding,
#     and a false finding here is load-bearing in the wrong direction.
#
# ⚠️ An entry is deleted, never narrowed to nothing. When the rules it names stop
# firing, that test fails and removing the entry is the only correct response.
PENDING_FIX: dict[str, frozenset[str]] = {
    # The ``allowedTestSets`` axis, which is not this gate's subject and is not fixed
    # here. It became visible when ``lib/idp_common_pkg/idp_common`` entered
    # ``SCAN_ROOTS`` so the canonical config-version lookup beside it could be policed.
    #
    # It genuinely carries both shapes — an ``or``-chain lookup key, and a caught query
    # failure that leaves the answer at ``None``. What makes them **not** live is
    # the axis's inverted polarity: ``assert_can_access_test_set`` requires an
    # *explicit* scope for an Annotator, so ``None`` denies rather than admits. Both
    # shapes therefore cost an Annotator their access rather than widening anyone's —
    # availability, not escalation — and the fix belongs with the change that gives
    # that axis the same ``sub`` join, not to this one.
    #
    # Named per rule, not per file, so any *other* rule firing here still fails the
    # gate, and ``test_the_pending_exemption_is_still_needed`` deletes the entry the
    # moment either of these stops firing. SCOPE2 is deliberately absent: its key comes
    # from a ``return``, not an assignment to a named key, so that rule does not reach
    # it — and naming a rule that does not fire is what that test refuses.
    "lib/idp_common_pkg/idp_common/testset_scope.py": frozenset({"SCOPE1", "SCOPE3"}),
}


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
@functools.lru_cache(maxsize=None)
def _tracked_files(root: Path) -> frozenset[str]:
    """Every path git tracks under ``root``, as repo-relative posix strings."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(p for p in out.split("\0") if p)


def _is_tracked(root: Path, path: Path) -> bool:
    return path.relative_to(root).as_posix() in _tracked_files(root)


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
            if not _is_tracked(root, path):
                # Gitignored build output, not source. Extensions vendor an entire
                # ``idp_common_pkg`` when built, so scanning untracked files makes this
                # gate's result depend on whether the developer happens to have built
                # one — findings CI could never reproduce, and silence where a real
                # source file is missing from git.
                continue
            yield path


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    """Every name bound to a string literal in one module, for key resolution.

    This is what stops the claim-key rules resting on a hardcoded name list. A new
    consumer — which the module docstring names as the case this gate exists for —
    will not use this repository's constant names: it will write
    ``EMAIL_ATTR = "email"``, or alias one (``k = USERS_TABLE_SCOPE_KEY``), and every
    rule that resolved only a literal or a known name then examined nothing while the
    code was genuinely fail-open.

    Collected **module-wide** rather than per-scope, on the same reasoning as
    :func:`_users_table_bindings`: the binding and the use can sit in different
    functions, and over-collecting only widens the net, which for this gate is the
    safe direction. Aliases are followed to a fixed point. Where a name is bound to
    more than one literal, a value in the claim vocabulary wins — that is the binding
    the rules are about.

    Seeded with :data:`CLAIM_CONSTANTS` so a module that *imports* a constant rather
    than defining it still resolves; a module that defines one **overrides** the seed,
    which is what lets SCOPE5 see a repointed constant instead of silently believing
    this file's idea of its value.
    """
    literals: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                value = node.value.value
                if target.id not in literals or (
                    value in IDENTITY_CLAIMS and literals[target.id] not in IDENTITY_CLAIMS
                ):
                    literals[target.id] = value
            elif isinstance(node.value, ast.Name):
                aliases.setdefault(target.id, node.value.id)

    resolved = dict(CLAIM_CONSTANTS)
    resolved.update(literals)
    # Follow aliases to a fixed point, so `k = USERS_TABLE_SCOPE_KEY` resolves.
    for _ in range(len(aliases) + 1):
        changed = False
        for name, source in aliases.items():
            if name not in resolved and source in resolved:
                resolved[name] = resolved[source]
                changed = True
        if not changed:
            break
    return resolved


def _claim_named_by(node: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS) -> str | None:
    """The claim a subscript/`get`/condition key names, however it is spelled.

    The tree spells these keys several ways. `claims.get("email")` is the literal
    form; `claims.get(SCOPE_KEY_CLAIM)` is the house style in the four consumers that
    ship without an `idp_common` layer and restate the rule with their own constants.
    A rule that saw only the literal went quiet in exactly those four files — and
    `claims.get(SCOPE_KEY_CLAIM) or claims.get(SCOPE_SUB_CLAIM)` is then the *natural*
    way to write the fallback this gate exists to forbid.

    Four spellings are resolved, because each of them was a way to write genuinely
    fail-open code and keep this gate green:

    * a string literal;
    * any name bound to one somewhere in the module (``bindings``), which covers a
      new consumer's own constant and an alias of an existing one;
    * an f-string wrapping a single such value, ``f"{USERS_TABLE_SCOPE_KEY}"``;
    * a mapping lookup with a literal key, ``ATTRS["email"]`` — the attribute name is
      still present in the source, which is the honest signal.

    Resolved from the **parsed tree**, never by importing the module under inspection:
    importing it would move both sides of the comparison at once, so a rename would
    silence the rule instead of failing it. ``SCOPE5`` covers the remaining direction,
    where a module *repoints* one of this file's constants to a different claim.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Subscript):
        return _claim_named_by(node.slice, bindings)
    if isinstance(node, ast.JoinedStr):
        parts = [
            v for v in node.values if not (isinstance(v, ast.Constant) and v.value == "")
        ]
        if len(parts) == 1 and isinstance(parts[0], ast.FormattedValue):
            return _claim_named_by(parts[0].value, bindings)
    return None


def _check_repointed_claim_constants(path: str, tree: ast.AST) -> list[Finding]:
    """SCOPE5 — a scanned module must not bind one of these names to another claim.

    :data:`CLAIM_CONSTANTS` is how this gate knows that `claims.get(SCOPE_KEY_CLAIM)`
    reads the ``email`` claim. A module that redefines `SCOPE_KEY_CLAIM` to something
    else breaks that in both directions at once, and both are bad: the *fail-open*
    direction, where the code now reads a username into the email key and every
    key-provenance rule believes it is reading an email; and the *false-failure*
    direction, where a correctly-used constant is reported as a substitution. The
    second matters as much here as the first — this gate's own history records that a
    single false SCOPE1 finding "was enough to keep a stale exemption looking
    necessary", and ``test_the_pending_exemption_is_still_needed`` is satisfied by any
    matching rule firing, a false one included.

    So the mismatch is reported *as itself* rather than left to distort another rule.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in CLAIM_CONSTANTS:
                continue
            if node.value.value != CLAIM_CONSTANTS[target.id]:
                findings.append(
                    Finding(
                        "SCOPE5",
                        path,
                        node.lineno,
                        f"{target.id!r} is bound to {node.value.value!r}, but every "
                        f"rule here reads it as {CLAIM_CONSTANTS[target.id]!r}. "
                        "Repointing it makes the key-provenance rules describe a "
                        "claim the code does not read — in whichever direction. Use a "
                        "differently-named constant for a different claim.",
                    )
                )
    return findings


def _is_claim_read(
    node: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> str | None:
    """The claim name a node reads, or None.

    Matches both `claims.get("email", "")` and `claims["email"]`, whatever the
    receiver is called — the receiver name carries no information here, and
    requiring one would make the rule dodgeable by renaming a variable. The key may
    be a literal or one of the constants the tree names it with; see
    :func:`_claim_named_by`.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and node.args
            and _claim_named_by(node.args[0], bindings) is not None
        ):
            return _claim_named_by(node.args[0], bindings)
    if isinstance(node, ast.Subscript):
        return _claim_named_by(node.slice, bindings)
    return None


def _claims_read_in(
    node: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> set[str]:
    return {
        claim
        for child in ast.walk(node)
        if (claim := _is_claim_read(child, bindings)) is not None
    }


def _unwrap(node: ast.AST) -> ast.AST:
    """Strip a coercion wrapper — `str(x)`, `(x)` — to get at the value."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in {"str", "unquote"} or not node.args:
            break
        node = node.args[0]
    return node


def _is_claim_read_of(
    node: ast.AST, claim: str, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> bool:
    """Whether a node IS the named claim, rather than merely mentioning it.

    Directness matters. `claims.get("email") or identity.get("username")` is an
    or-chain *over* the claim and is the defect; `[v for v in (a, caller["email"])
    if v] or [SENTINEL]` merely contains an email-keyed read inside an unrelated
    expression, and flagging that would make the rule noise.
    """
    return _is_claim_read(_unwrap(node), bindings) == claim


def _is_email_claim_read(
    node: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> bool:
    """Whether a node IS the email claim. See :func:`_is_claim_read_of`."""
    return _is_claim_read_of(node, SCOPE_KEY_CLAIM, bindings)


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


def _is_users_table_construction(node: ast.AST) -> bool:
    """`<anything>.Table(<expression naming USERS_TABLE_NAME>)`."""
    return (
        isinstance(node, ast.Call)
        and _call_name(node) == "Table"
        and any(_mentions_users_table(arg) for arg in node.args)
    )


def _users_table_bindings(tree: ast.AST) -> set[str]:
    """Names that stand for a DynamoDB Table resource for the UsersTable.

    Collected module-wide rather than per-function, deliberately: the binding and the
    query can sit in different functions, and over-collecting here only widens the
    net, which for this gate is the safe direction.

    Both ways a name can come to mean the table are collected — assignment, and a
    **factory** whose return value constructs it. Without the second, a query behind
    `_users_table().query(...)` escapes discovery entirely and no rule applies to it
    at all, which is the quietest possible failure for this gate.
    """
    bound: set[str] = set()

    for node in ast.walk(tree):
        # (a) assignment: `table = _ddb.Table(USERS_TABLE_NAME)`
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        if targets and _is_users_table_construction(node.value):
            for target in targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
                elif isinstance(target, ast.Attribute):
                    bound.add(target.attr)

        # (b) factory: `def _users(): return _ddb.Table(USERS_TABLE_NAME)`
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and _is_users_table_construction(
                    child.value
                ):
                    bound.add(node.name)
                    break

    return bound


def _scope_resolving_functions(tree: ast.AST, users_table_bindings: set[str]) -> set[str]:
    """Local functions that resolve a caller's scope, found by analysis not by name.

    A function qualifies if it performs a UsersTable query itself, or calls another
    function in this module that does. Iterated to a fixed point, so a chain of
    private wrappers is followed however deep it goes.

    This is what stops the failure-handling rule depending on a hardcoded list of
    wrapper names: renaming `_get_user_allowed_config_versions` to `_profiles_for`
    used to make an identical fail-open invisible.
    """
    functions: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)

    resolving: set[str] = set()
    for name, node in functions.items():
        if any(_is_scope_query(child, users_table_bindings) for child in ast.walk(node)):
            resolving.add(name)

    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            if name in resolving:
                continue
            called = {
                _call_name(child)
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
            }
            if called & (resolving | LOOKUP_CALL_NAMES):
                resolving.add(name)
                changed = True
    return resolving


def _is_scope_query(node: ast.AST, users_table_bindings: set[str]) -> bool:
    """A DynamoDB read against the UsersTable — a Query, or a pointer GetItem.

    Recognised by the **table**, not the index: the receiver is a name bound from
    ``*.Table(<something naming USERS_TABLE_NAME>)``, or the expression itself names
    the table. Naming the wrong index is the failure mode that produced the original
    bug, so a rule that only matched the right index name could not see it.

    ``get_item`` counts for the same reason. The ``sub`` key space is addressed by the
    table's base key rather than by an index, so a lookup that read a ``SUB#<sub>``
    pointer and swallowed the failure would be outside every rule here — half the
    lookup unpoliced, with nothing saying so.

    The declared index name is kept as an independent second leg, for a query whose
    table object was built out of this module's sight.
    """
    if not isinstance(node, ast.Call) or _call_name(node) not in _SCOPE_READ_CALLS:
        return False

    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if receiver is not None:
        # `table.query(...)`, `self._users.query(...)` and `_users().query(...)` all
        # reach the same table; the third is the factory shape.
        receiver_name = None
        if isinstance(receiver, ast.Name):
            receiver_name = receiver.id
        elif isinstance(receiver, ast.Attribute):
            receiver_name = receiver.attr
        elif isinstance(receiver, ast.Call):
            receiver_name = _call_name(receiver)
        if receiver_name is not None and receiver_name in users_table_bindings:
            return True
        if _mentions_users_table(receiver):
            return True

    for keyword in node.keywords:
        # `TableName=` is how the low-level client names its target, where the
        # resource API uses a Table object. Both spellings reach the same table.
        if keyword.arg == "TableName" and _mentions_users_table(keyword.value):
            return True
        if keyword.arg != "IndexName":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and value.value == SCOPE_INDEX_NAME:
            return True
        if isinstance(value, ast.Name) and value.id == SCOPE_INDEX_CONSTANT:
            return True
    return False


def _performs_scope_lookup(
    node: ast.AST,
    users_table_bindings: set[str],
    local_resolvers: set[str] = frozenset(),  # type: ignore[assignment]
) -> bool:
    """Whether this subtree queries the UsersTable, directly or through a wrapper.

    ``local_resolvers`` is the analysed set from ``_scope_resolving_functions``; the
    hardcoded ``LOOKUP_CALL_NAMES`` only covers callees defined outside the module.
    """
    for child in ast.walk(node):
        if _is_scope_query(child, users_table_bindings):
            return True
        name = _call_name(child)
        if name is not None and (name in LOOKUP_CALL_NAMES or name in local_resolvers):
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
    if isinstance(last, _TRY_NODES):
        return _terminates_in_a_refusal(last.body) and all(
            _terminates_in_a_refusal(h.body) for h in last.handlers
        )
    return False


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #
def _check_key_provenance(
    path: str, tree: ast.AST, bindings: Mapping[str, str] | None = None
) -> list[Finding]:
    """SCOPE1/SCOPE2 — the lookup key comes from the `email` claim, or nothing."""
    if bindings is None:
        bindings = _string_bindings(tree)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if not any(
                _is_email_claim_read(operand, bindings) for operand in node.values
            ):
                continue
            for operand in node.values:
                if _is_email_claim_read(operand, bindings):
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
            # Which claim this name is allowed to hold depends on the key space it
            # reaches. An email key may only come from the `email` claim; a sub key
            # only from the verified `sub` claim. Anything else is a substitution,
            # and so is each of them in the other's slot.
            if targets & SUB_KEY_TARGETS and not targets & EMAIL_KEY_TARGETS:
                permitted, forbidden = SCOPE_SUB_CLAIM, SUBSTITUTE_IDENTIFIER_CLAIMS
            else:
                permitted, forbidden = SCOPE_KEY_CLAIM, FALLBACK_FORBIDDEN_CLAIMS
            if isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or):
                # `claims.get(<permitted>) or ""` is a coercion of a missing claim to
                # the empty string, which then denies. `... or claims.get("sub")` on an
                # email key, or `... or event["callerSub"]` on a sub key, reaches for a
                # second source for one key — and only one source can be right for the
                # key space that name reaches.
                if not all(
                    _is_claim_read_of(operand, permitted, bindings)
                    or _is_constant_operand(operand)
                    for operand in node.value.values
                ):
                    findings.append(
                        Finding(
                            "SCOPE1",
                            path,
                            node.lineno,
                            f"{sorted(targets & SCOPE_KEY_TARGETS)} is a scope lookup "
                            f"key for the {permitted!r} key space and is assigned an "
                            "`or` fallback chain",
                        )
                    )
            substituted = (
                _claims_read_in(node.value, bindings) - {permitted}
            ) & forbidden
            if substituted:
                findings.append(
                    Finding(
                        "SCOPE2",
                        path,
                        node.lineno,
                        f"the scope lookup key is derived from {sorted(substituted)} "
                        f"rather than the {permitted!r} claim alone",
                    )
                )

    findings.extend(_check_nested_get_defaults(path, tree, bindings))
    findings.extend(_check_reassigned_from_a_substitute_claim(path, tree, bindings))
    findings.extend(_check_key_space_confusion(path, tree, bindings))
    findings.extend(_check_repointed_claim_constants(path, tree))
    return findings


def _email_key_condition_value(
    node: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> ast.expr | None:
    """The value put to an ``email``-keyed DynamoDB key condition, or None.

    Matches ``<factory>("email").eq(x)`` and ``<factory>(USERS_TABLE_SCOPE_KEY).eq(x)``
    in any of boto3's condition methods, and returns ``x``.

    ⚠️ **The factory's name is deliberately not part of the signal.** The condition
    class is `boto3.dynamodb.conditions.Key`, but this tree reaches it under three
    spellings: `Key(...)` in the resolvers, `_Key(...)` where the import is aliased,
    and a `key_factory` **parameter** in the canonical module — which takes it as an
    argument precisely so the module needs no boto3 at import time, which is what lets
    the document-list resolvers vendor it. Requiring the literal `Key` made this rule
    inert at three of the four sites implementing the leg it polices, including the
    canonical one every other consumer imports.

    What identifies the condition is its **argument**: a condition built on the
    ``email`` attribute is an ``EmailIndex`` hash-key condition whoever constructed
    it. A same-shaped call on some unrelated `"email"`-keyed structure would also
    match, which is acceptable — this only runs inside modules already discovered as
    scope consumers, and over-matching is the safe direction for this gate.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if not node.args:
        return None
    key_call = node.func.value
    if not isinstance(key_call, ast.Call) or not key_call.args:
        return None
    named = _claim_named_by(key_call.args[0], bindings)
    return node.args[0] if named == SCOPE_KEY_CLAIM else None


def _check_key_space_confusion(
    path: str, tree: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> list[Finding]:
    """SCOPE4 — the value put to the email-keyed index must be the email.

    The two key spaces are disjoint, and only one direction of confusing them fails
    *open*: a ``sub`` put to ``EmailIndex`` matches no row, and an empty page means
    unrestricted. (The reverse — an email put to a ``SUB#`` pointer key — matches no
    pointer, and the lookup then tries the email join and gets the right answer, so
    it is a bug and not a fail-open, and is not a rule here.)

    This is the shape none of the other rules see. SCOPE1's ``or``-chain leg needs a
    direct *claim read* among the operands, and SCOPE2 needs a claim read in the
    assignment — so ``Key("email").eq(caller_sub or caller_email)``, written while
    "simplifying" a two-key lookup back into one, passes both. It is also exactly how
    the instance this gate is named after was spelled.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        value = _email_key_condition_value(node, bindings)
        if value is None:
            continue
        inner = _unwrap(value)
        if isinstance(inner, ast.Name) and inner.id in SUB_KEY_TARGETS:
            findings.append(
                Finding(
                    "SCOPE4",
                    path,
                    node.lineno,
                    f"{inner.id!r} is a Cognito {SCOPE_SUB_CLAIM!r} and is being put "
                    f"to the {SCOPE_INDEX_NAME} hash key. It is not an email address "
                    "for any caller, so it matches no row — and an empty page means "
                    f"'unrestricted'. Read the {SCOPE_SUB_CLAIM!r} key space with a "
                    f"{SUB_POINTER_PREFIX} pointer GetItem instead.",
                )
            )
            continue
        if isinstance(inner, ast.BoolOp) and isinstance(inner.op, ast.Or):
            findings.append(
                Finding(
                    "SCOPE4",
                    path,
                    node.lineno,
                    f"the {SCOPE_INDEX_NAME} hash key is an `or` chain, so a value "
                    "that is not an email address can reach it. Each identifier goes "
                    "to the key space that indexes it; a chain over one key does not.",
                )
            )
    return findings


def _assignments_by_name(scope: ast.AST) -> dict[str, list[ast.AST]]:
    """Every value assigned to each plain name directly inside one function body."""
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(scope):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.setdefault(target.id, []).append(node)
    return assigned


def _check_reassigned_from_a_substitute_claim(
    path: str, tree: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> list[Finding]:
    """SCOPE2, written across two statements instead of one expression.

        key = claims.get("email", "")
        if not key:
            key = claims.get("sub", "")

    is the `or` chain with a line break in it, and neither the `BoolOp` rule nor the
    nested-default rule sees it. The name-based rules do not either, because the
    author is free to call it anything.

    The signal needs no name list: within one function, a name that is assigned the
    **email claim** anywhere and a **substitute identifier** anywhere else is a
    fallback chain, whatever it is called and however far apart the two statements
    sit. The ``sub`` counts as a substitute *here*, where it shares a name with the
    email, even though it is a legitimate key of its own in its own slot: one name
    holding either is one key with two sources, and only one of them can be right for
    the key space that name reaches.
    """
    findings: list[Finding] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for name, nodes in _assignments_by_name(scope).items():
            from_email = [
                n
                for n in nodes
                if SCOPE_KEY_CLAIM in _claims_read_in(n.value, bindings)
            ]
            if not from_email:
                continue
            substituted: set[str] = set()
            offender = None
            for node in nodes:
                # A *different* statement from the one that reads the email claim.
                # Reading both identifiers in a single expression is either the `or`
                # chain SCOPE1 already covers, or a legitimate non-scope use — the
                # reviewer filter deliberately matches an owner against a username
                # AND an email in one comprehension, and flagging that is noise.
                if node in from_email:
                    continue
                found = (
                    _claims_read_in(node.value, bindings) - {SCOPE_KEY_CLAIM}
                ) & FALLBACK_FORBIDDEN_CLAIMS
                if found:
                    substituted |= found
                    offender = offender or node
            if not substituted:
                continue
            findings.append(
                Finding(
                    "SCOPE2",
                    path,
                    offender.lineno,
                    f"{name!r} is assigned the {SCOPE_KEY_CLAIM!r} claim and also "
                    f"{sorted(substituted)} in {scope.name!r} — a fallback chain "
                    "split across statements. An identifier that is not an email "
                    "matches no UsersTable row, and the empty page reads as "
                    "'unrestricted'.",
                )
            )
    return findings


def _check_nested_get_defaults(
    path: str, tree: ast.AST, bindings: Mapping[str, str] = CLAIM_CONSTANTS
) -> list[Finding]:
    """SCOPE1, written as a nested default rather than an `or`.

    `claims.get("email", claims.get("cognito:username", claims.get("sub", "")))` is
    the same fallback chain as the `or` form and has the same consequence, but it
    produces no `BoolOp`, so a rule that looks for one never sees it. The signal is a
    read whose **default** argument is itself a read, and it is independent of which
    key is outermost.

    Gated on `IDENTITY_CLAIMS`, because `dict.get(a, dict.get(b, c))` is a common
    shape with nothing to do with identity and flagging all of it is noise that
    outlives its usefulness — a false hit on Bedrock stream-error handling was enough
    to keep a stale exemption looking necessary. At least one key in the chain must
    name a caller.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        outer = _is_claim_read(node, bindings)
        if outer is None or not isinstance(node, ast.Call):
            continue
        if len(node.args) < 2:
            continue
        nested = _claims_read_in(node.args[1], bindings)
        if not nested:
            continue
        if not (nested | {outer}) & IDENTITY_CLAIMS:
            continue
        findings.append(
            Finding(
                "SCOPE1",
                path,
                node.lineno,
                f"the {outer!r} read defaults to another claim {sorted(nested)} — the "
                "same fallback chain as an `or`, with the same consequence: an "
                "identifier that is not an email matches no UsersTable row, and the "
                "empty page reads as 'unrestricted'.",
            )
        )
    return findings


def _check_failure_denies(
    path: str,
    tree: ast.AST,
    users_table_bindings: set[str],
    local_resolvers: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[Finding]:
    """SCOPE3 — a lookup that cannot answer denies; it never returns unrestricted."""
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if not _suppresses_exceptions(node):
                continue
            if not any(
                _performs_scope_lookup(stmt, users_table_bindings, local_resolvers)
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

        if not isinstance(node, _TRY_NODES):
            continue
        guards_lookup = any(
            _performs_scope_lookup(stmt, users_table_bindings, local_resolvers)
            for stmt in node.body
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
        resolvers = _scope_resolving_functions(tree, bindings)
        if not _performs_scope_lookup(tree, bindings, resolvers):
            continue
        discovered.append(path)
        rel = path.relative_to(root).as_posix()
        findings.extend(_check_key_provenance(rel, tree))
        findings.extend(_check_failure_denies(rel, tree, bindings, resolvers))

    return discovered, findings


def _enforced(findings: list[Finding], rules: set[str]) -> list[Finding]:
    """The findings the gate fails on.

    A finding is withheld only when its path AND its rule are both named in
    ``PENDING_FIX`` — so an exempt file breaking a rule the exemption does not cover
    still fails the gate.
    """
    return [
        f
        for f in findings
        if f.rule in rules and f.rule not in PENDING_FIX.get(f.path, frozenset())
    ]


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

    # A floor equal to the real count, not a token one. A generous `>= 8` against 14
    # discovered would mean six consumers could drop out of discovery — and a module
    # that escapes discovery has NO rule applied to it, silently — while the assertion
    # still passed. Raise this when a consumer is added; lowering it needs a reason.
    assert len(discovered) >= 14, (
        f"the scope-lookup scan discovered only {len(discovered)} modules. A module "
        "that escapes discovery has no rule applied to it at all, so a drop here is "
        f"a silent loss of coverage, not a cleanup. Found: {sorted(names)}"
    )
    # Every directory that holds a consumer of this rule. An inventory, deliberately:
    # each is a distinct enforcement point on a distinct entry path, and being
    # explicit is what makes losing one a failure rather than a smaller number.
    for expected in (
        "configuration_resolver",
        "get_stepfunction_execution_resolver",
        "list_documents_gsi_resolver",
        "list_documents_range_resolver",
        "reprocess_document_resolver",
        "sync_bda_idp_resolver",
        "user_management",
        "chat_with_document_processor",
        "vendored",
        "feature-api",
        # The canonical lookup and the matcher every other consumer imports. Absent
        # from this list, a fail-open introduced there passed the gate outright.
        "idp_common",
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


def test_no_identifier_is_put_to_the_wrong_key_space(scanned):
    """SCOPE4. The two key spaces are disjoint; only one confusion fails open."""
    _, findings = scanned
    offenders = _enforced(findings, {"SCOPE4"})

    assert not offenders, "\n".join(["scope key space:", *map(str, offenders)])


def test_no_module_repoints_a_claim_constant(scanned):
    """SCOPE5. See `_check_repointed_claim_constants` for why both directions matter."""
    _, findings = scanned
    offenders = _enforced(findings, {"SCOPE5"})

    assert not offenders, "\n".join(["claim constants:", *map(str, offenders)])


def test_the_pending_exemption_is_still_needed(scanned):
    """Every rule a ``PENDING_FIX`` entry names must still fire, or the entry goes.

    An entry names a module the gate discovers and deliberately does not enforce,
    with the specific rules the exemption covers. Its findings are set aside rather
    than the file being hidden from discovery. An exemption nobody revisits is how a
    gate goes quiet, so this asserts each one is *load-bearing*.

    It checks **per rule**, not per file. "Any finding at all keeps the entry alive"
    is too weak by exactly the margin that matters: one unrelated finding — and a
    false positive is enough — makes a fully-compliant file look non-compliant
    forever, and the module then sits outside enforcement permanently.
    """
    _, findings = scanned
    seen: dict[str, set[str]] = {}
    for finding in findings:
        seen.setdefault(finding.path, set()).add(finding.rule)

    for path, expected_rules in sorted(PENDING_FIX.items()):
        still_firing = seen.get(path, set()) & expected_rules
        satisfied = expected_rules - still_firing
        assert not satisfied, (
            f"{path} no longer breaks {sorted(satisfied)}, so its PENDING_FIX entry "
            f"is (partly) dead config — it claims {sorted(expected_rules)} and only "
            f"{sorted(still_firing)} still fire. Narrow the entry to the rules that "
            "remain, or delete it entirely; the gate then enforces those rules on "
            "this file like every other consumer."
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

_PRIVATELY_NAMED_WRAPPER = '''
def _profiles_for(key):
    table = _ddb.Table(USERS_TABLE_NAME)
    resp = table.query(KeyConditionExpression=Key("email").eq(key))
    return resp.get("Items")


def handler(event):
    try:
        return _profiles_for(_caller_email(event))
    except Exception as e:
        logger.warning("scope lookup failed: %s", e)
        return None
'''

_EXCEPT_STAR = '''
def lookup(email):
    scope = None
    try:
        resp = users_table.query(KeyConditionExpression=Key("email").eq(email))
        scope = resp.get("Items")
    except* Exception as eg:
        logger.warning("scope lookup failed: %s", eg)
    return scope
'''

_TWO_STATEMENT_FALLBACK = '''
def _resolve(event):
    claims = event["identity"]["claims"]
    whoami = claims.get("email", "")
    if not whoami:
        whoami = claims.get("sub", "")
    return _get_user_allowed_config_versions(whoami)
'''

_TABLE_FROM_A_FACTORY = '''
def _users():
    return boto3.resource("dynamodb").Table(os.environ["USERS_TABLE_NAME"])


def lookup(email):
    try:
        return _users().query(KeyConditionExpression=Key("email").eq(email))
    except Exception:
        return None
'''

_LOW_LEVEL_CLIENT_QUERY = '''
def lookup(caller_sub):
    try:
        return _ddb_client.query(
            TableName=USERS_TABLE_NAME,
            IndexName="SubIndex",
            KeyConditionExpression="sub = :s",
        )
    except Exception as e:
        logger.error("scope lookup FAILED OPEN: %s", e)
        return None
'''

_SUB_POINTER_FAILURE_SWALLOWED = '''
def lookup(caller_email, caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        pointer = users_table.get_item(Key={"PK": "SUB#" + caller_sub, "SK": "SUB#" + caller_sub})
    except Exception as e:
        logger.warning("pointer read failed, falling through: %s", e)
        pointer = None
    return pointer
'''

_SUB_PUT_TO_THE_EMAIL_INDEX = '''
def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(caller_sub)
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_EMAIL_KEY_FROM_AN_OR_CHAIN = '''
def lookup(caller_email, caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key("email").eq(caller_sub or caller_email),
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_SUB_KEY_FROM_A_BODY_FIELD = '''
def _resolve(event):
    body = json.loads(event.get("body") or "{}")
    caller_sub = str(body.get("callerSub") or "").strip()
    return _get_user_allowed_config_versions("", caller_sub)
'''


# Five ways to name the `email` attribute without writing the literal, each of them
# genuinely fail-open — the attribute really *is* `email`, so DynamoDB returns an
# empty page for a `sub` and the lookup reads that as unrestricted. A rule that
# resolved only a literal or a name on a hardcoded list examined none of them, which
# is exactly the case the module docstring names: a NEW consumer, written by someone
# who never read these tests and who will not use this repository's constant names.
_SUB_TO_A_LOCALLY_NAMED_EMAIL_KEY = '''
EMAIL_ATTR = "email"


def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key(EMAIL_ATTR).eq(caller_sub)
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_SUB_TO_AN_F_STRING_EMAIL_KEY = '''
USERS_TABLE_SCOPE_KEY = "email"


def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key(f"{USERS_TABLE_SCOPE_KEY}").eq(caller_sub),
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_SUB_TO_AN_ALIASED_EMAIL_KEY = '''
USERS_TABLE_SCOPE_KEY = "email"
k = USERS_TABLE_SCOPE_KEY


def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key(k).eq(caller_sub)
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_SUB_TO_A_MAPPED_EMAIL_KEY = '''
ATTRS = {"email": "email"}


def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key(ATTRS["email"]).eq(caller_sub),
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

# Repointing one of this gate's own constants, in both directions. The first is a real
# fail-open that every key-provenance rule would otherwise describe as reading an
# email; the second uses its constant *correctly* and must produce SCOPE5 and nothing
# else — a false SCOPE2 here is what keeps a stale `PENDING_FIX` entry looking
# necessary, which this gate's own history records.
_A_REPOINTED_EMAIL_CLAIM_CONSTANT = '''
SCOPE_KEY_CLAIM = "cognito:username"


def _get_caller_identity(event):
    claims = event.get("identity", {}).get("claims", {})
    email = str(claims.get(SCOPE_KEY_CLAIM) or "").strip()
    return {"email": email}


def lookup(email):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.query(
            IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
        )
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
'''

_A_REPOINTED_SUB_CLAIM_CONSTANT_USED_CORRECTLY = '''
SCOPE_SUB_CLAIM = "email"


def _caller(event):
    claims = event.get("identity", {}).get("claims", {})
    caller_sub = str(claims.get(SCOPE_SUB_CLAIM) or "").strip()
    return caller_sub


def lookup(caller_sub):
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        return users_table.get_item(Key={"PK": "SUB#" + caller_sub})
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
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

# One compliant counterpart per must-flag rule. Without these, a rule can be made to
# pass by over-firing — which is not a hypothetical failure mode here: an ungated
# nested-default rule produced a false SCOPE1 on Bedrock stream-error handling, and
# that single false finding was enough to keep a stale exemption looking necessary.

_COMPLIANT_EMAIL_ONLY_ASSIGNMENT = '''
def _get_caller_info(event):
    claims = event.get("identity", {}).get("claims", {})
    # `username` is NOT a scope key — it matches HITLReviewOwner — so its own
    # fallback chain is legitimate and must not be flagged.
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    caller_email = str(claims.get("email") or "").strip()
    return {"email": caller_email, "username": username}
'''

_COMPLIANT_CONSTANT_DEFAULT = '''
def _caller_email(claims):
    return claims.get("email", "")
'''

_COMPLIANT_NON_IDENTITY_NESTED_GET = '''
def _stream_error(event):
    # Not identity at all: the nested-default shape is ubiquitous, and flagging it
    # everywhere is noise that discredits the rule.
    detail = event.get("internalServerException", event.get("throttlingException", {}))
    return detail.get("message", "")
'''

# The shape the two key spaces are MEANT to take. Without this counterpart the rules
# above could be satisfied by forbidding the `sub` outright, which is what the
# previous version of this gate did — and which is why the durable fix could not be
# written without changing it.
_COMPLIANT_TWO_KEY_SPACES = '''
def _caller_email(claims):
    return str(claims.get("email") or "").strip()


def _caller_sub(claims):
    return str(claims.get("sub") or "").strip()


def lookup(caller_email, caller_sub):
    if not caller_email and not caller_sub:
        raise ScopeLookupError("no email or sub on the verified identity")
    users_table = _dynamodb.Table(USERS_TABLE_NAME)
    try:
        row = None
        if caller_sub:
            pointer = users_table.get_item(
                Key={"PK": "SUB#" + caller_sub, "SK": "SUB#" + caller_sub}
            ).get("Item")
            if pointer:
                row = users_table.get_item(
                    Key={"PK": "USER#" + pointer["userId"], "SK": "USER#" + pointer["userId"]}
                ).get("Item")
        if row is None and caller_email:
            resp = users_table.query(
                IndexName="EmailIndex",
                KeyConditionExpression=Key("email").eq(caller_email),
                Limit=1,
            )
            items = resp.get("Items") or []
            row = items[0] if items else None
    except Exception as e:
        raise ScopeLookupError(str(e)) from e
    if row is None and not caller_email:
        raise ScopeLookupError("no email, and no row records this caller's sub")
    return row.get("allowedConfigVersions") if row else None
'''


def _findings_for(source: str) -> list[Finding]:
    tree = ast.parse(source)
    tables = _users_table_bindings(tree)
    resolvers = _scope_resolving_functions(tree, tables)
    return _check_key_provenance("snippet.py", tree) + _check_failure_denies(
        "snippet.py", tree, tables, resolvers
    )


@pytest.mark.parametrize(
    "source,rule",
    [
        (_FALLBACK_CHAIN, "SCOPE1"),
        (_NESTED_GET_DEFAULTS, "SCOPE1"),
        (_BODY_SUPPLIED_KEY, "SCOPE1"),
        (_BODY_SUPPLIED_KEY, "SCOPE2"),
        (_TWO_STATEMENT_FALLBACK, "SCOPE2"),
        (_SWALLOWED_FAILURE, "SCOPE3"),
        (_FALL_THROUGH_FAILURE, "SCOPE3"),
        (_CONSUMER_SWALLOWS_REFUSAL, "SCOPE3"),
        (_SUPPRESSED_FAILURE, "SCOPE3"),
        (_REFUSAL_SHAPED_SUCCESS, "SCOPE3"),
        (_CONDITIONAL_RAISE_THEN_FALLS_THROUGH, "SCOPE3"),
        (_WRONG_INDEX_ON_THE_USERS_TABLE, "SCOPE3"),
        (_PRIVATELY_NAMED_WRAPPER, "SCOPE3"),
        (_EXCEPT_STAR, "SCOPE3"),
        (_TABLE_FROM_A_FACTORY, "SCOPE3"),
        (_LOW_LEVEL_CLIENT_QUERY, "SCOPE3"),
        (_SUB_POINTER_FAILURE_SWALLOWED, "SCOPE3"),
        (_SUB_PUT_TO_THE_EMAIL_INDEX, "SCOPE4"),
        (_EMAIL_KEY_FROM_AN_OR_CHAIN, "SCOPE4"),
        (_SUB_KEY_FROM_A_BODY_FIELD, "SCOPE2"),
        (_SUB_TO_A_LOCALLY_NAMED_EMAIL_KEY, "SCOPE4"),
        (_SUB_TO_AN_F_STRING_EMAIL_KEY, "SCOPE4"),
        (_SUB_TO_AN_ALIASED_EMAIL_KEY, "SCOPE4"),
        (_SUB_TO_A_MAPPED_EMAIL_KEY, "SCOPE4"),
        (_A_REPOINTED_EMAIL_CLAIM_CONSTANT, "SCOPE5"),
        (_A_REPOINTED_EMAIL_CLAIM_CONSTANT, "SCOPE2"),
        (_A_REPOINTED_SUB_CLAIM_CONSTANT_USED_CORRECTLY, "SCOPE5"),
    ],
    ids=[
        "or-chain-key",
        "nested-get-defaults",
        "body-supplied-key-or-chain",
        "body-supplied-key-substitute-claim",
        "two-statement-fallback",
        "except-returns-None",
        "except-falls-through",
        "consumer-swallows",
        "contextlib-suppress",
        "refusal-shaped-success-payload",
        "conditional-raise-then-falls-through",
        "wrong-index-on-the-users-table",
        "privately-named-wrapper",
        "except-star",
        "table-from-a-factory",
        "low-level-client-query",
        "sub-pointer-failure-swallowed",
        "sub-put-to-the-email-index",
        "email-key-from-an-or-chain",
        "sub-key-from-a-body-field",
        "sub-to-a-locally-named-email-key",
        "sub-to-an-f-string-email-key",
        "sub-to-an-aliased-email-key",
        "sub-to-a-mapped-email-key",
        "repointed-email-claim-constant",
        "repointed-email-claim-constant-also-substitutes",
        "repointed-sub-claim-constant-used-correctly",
    ],
)
def test_the_rule_catches_the_shape_it_is_for(source, rule):
    assert rule in {f.rule for f in _findings_for(source)}


def test_a_correctly_used_repointed_constant_reports_only_scope5():
    """The false-failure direction, which matters as much as the fail-open one.

    A rule that fired SCOPE2 here would report a substitution that is not happening —
    and ``test_the_pending_exemption_is_still_needed`` is satisfied by *any* matching
    rule firing, a false one included, so a false finding can keep a stale suppression
    alive. This gate's own docstring records that happening once already.
    """
    rules = {f.rule for f in _findings_for(_A_REPOINTED_SUB_CLAIM_CONSTANT_USED_CORRECTLY)}

    assert rules == {"SCOPE5"}


@pytest.mark.parametrize(
    "source",
    [_PRIVATELY_NAMED_WRAPPER, _TABLE_FROM_A_FACTORY, _LOW_LEVEL_CLIENT_QUERY],
    ids=["privately-named-wrapper", "table-from-a-factory", "low-level-client-query"],
)
def test_discovery_does_not_depend_on_a_spelling(source):
    """A module that escapes discovery has no rule applied to it at all.

    So discovery must not hinge on a wrapper's name, on the table object being bound
    by assignment, or on the resource API being used rather than the low-level client.
    """
    tree = ast.parse(source)
    bindings = _users_table_bindings(tree)
    resolvers = _scope_resolving_functions(tree, bindings)

    assert _performs_scope_lookup(tree, bindings, resolvers)


@pytest.mark.parametrize(
    "source",
    [
        _COMPLIANT,
        _COMPLIANT_IN_BAND_DENIAL,
        _COMPLIANT_RESPONSE_DENIAL,
        _COMPLIANT_EMAIL_ONLY_ASSIGNMENT,
        _COMPLIANT_CONSTANT_DEFAULT,
        _COMPLIANT_NON_IDENTITY_NESTED_GET,
        _COMPLIANT_TWO_KEY_SPACES,
    ],
    ids=[
        "raises",
        "in-band-Unauthorized",
        "403-response",
        "email-only-assignment-beside-a-username-chain",
        "constant-default",
        "non-identity-nested-get",
        "two-key-spaces",
    ],
)
def test_the_compliant_shapes_are_accepted(source):
    """A default of "" beside the email claim is a coercion, not a fallback.

    A refusal may be *returned* rather than raised, in any of the three shapes this
    tree actually uses, as long as the value names the denial. And a rule that
    over-fires is a rule that gets ignored, so each must-flag shape has a
    near-neighbour here that must NOT flag.
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
