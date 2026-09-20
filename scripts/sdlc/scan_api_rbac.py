#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Static RBAC scan for the IDP UI REST API — no AWS credentials required.

WHY
---
The UI API exposes ~one physical route (POST /op/{field}); the Cognito
authorizer only *authenticates*. Every operation's group/scope enforcement
lives in per-resolver Python. Three artifacts can silently drift:

  1. the op registry            — FIELD_FUNCTION_MAP (SSM param in the template)
                                   + the dispatcher's ddb_direct._HANDLED set
  2. the declared policy         — @aws_cognito_user_pools(cognito_groups:[...])
                                   directives in schema.graphql
  3. the tested/expected policy  — scripts/api_rbac_expectations.yaml

A new endpoint that ships without a group check looks exactly like drift among
these three. This scan is the guard. It runs in CI (no stack needed) and is the
static half of `make api-test`.

The REST dispatcher is not the only way in. Chat streaming is served by a Lambda
**Function URL** (`AWS::Lambda::Url`, `AuthType=AWS_IAM`) whose FastAPI app
routes straight to the chat processors, bypassing the dispatcher, the Cognito
authorizer and every check above. Those routes were outside this scan entirely,
so an authorization defect on them was invisible to the gate. S6-S9 cover them.

CHECKS
------
  S1  Manifest completeness — every routable op has an expectations entry, and
      every expectations entry maps to a real op (no stale rows).
  S2  Schema <-> expectations consistency — cognito_groups directives in
      schema.graphql match the expected groups (documented drift allowed via
      `schema_groups:` / `known_gap:`).
  S3  Resolver enforcement — each op's `enforced_in` source file contains a
      recognized enforcement pattern (group check, ownership, or IAM-only
      rejection). ANY-auth ops with no pattern must carry a known_gap.
  S4  Scope enforcement — ops flagged `scope_checked`/`scope_filtered` must
      reference allowedConfigVersions in their enforced_in file.
  S5  Template auth — every API Gateway Method is COGNITO_USER_POOLS except the
      allowlisted CORS (OPTIONS) and static-SPA (GET) routes.
  S6  Function URL universe — every AWS::Lambda::Url in template.yaml is
      declared in the expectations file with a matching AuthType (never NONE),
      and every route its FastAPI app serves is declared (no undeclared route).
  S7  Function URL identity precedence — each route must resolve the caller
      identity from the transport-verified source FIRST; a request-body value
      may only be a fallback, never preferred, and the name holding the resolved
      identity must be assigned exactly once (no later rebinding, whatever the
      second value is spelled like — see `route_identity_rebindings`).
  S8  Function URL identity conflict — the handler must refuse a request whose
      body-supplied identity contradicts the verified one, rather than silently
      picking either.
  S9  Function URL downstream enforcement — a route declared with a group list
      must have a recognized group-enforcement pattern (and those group names)
      in the processor it invokes, and must agree with the equivalent REST op.

EXIT CODES
----------
  0  all checks passed (WARN-only findings for known_gaps are allowed)
  1  one or more FAIL findings
  2  usage / file-not-found error

USAGE
-----
  python3 scripts/sdlc/scan_api_rbac.py [--json report.json] [--strict]

  --strict  treat known_gap WARNs as failures (use to verify a gap was fixed).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Default source root. Every path the checks read is derived from the ``repo``
# argument of ``run_checks`` (which defaults to this), so the whole scan can be
# pointed at a fixture tree — see the docstring on ``run_checks``.
# Note the Function URLs live in the parent template.yaml, not in the
# api-resolvers nested stack.
REPO = Path(__file__).resolve().parents[2]

# Methods intentionally unauthenticated (CORS preflight + static SPA serving).
ALLOWED_UNAUTH_METHODS = {
    "HttpApiOptionsMethod",  # CORS preflight (MOCK)
    "WebUIRootMethod",       # GET / static SPA
    "WebUIProxyMethod",      # GET /{proxy+} static SPA
}

# Substrings that count as a server-side enforcement pattern in a resolver.
ENFORCE_PATTERNS = (
    "PermissionError",
    "_enforce_operation_group",
    "_enforce_rbac",
    "_caller_in_groups",
    "cognito:groups",
    "Unauthorized",
    "is_admin",
    "_ADMIN_GROUP",
)
# Patterns indicating row-level / ownership scoping (for ANY-auth ops).
OWNERSHIP_PATTERNS = ("owner", "user_id", "userId", "sub", "session", "caller")
SCOPE_PATTERNS = (
    "allowedConfigVersions",
    "allowed_config_versions",
    # The canonical matcher. Needed because S4 no longer greps the whole FILE (see
    # `op_scope_source`): the configuration resolver resolves the scope once in its
    # handler and each operation's own branch only *consumes* it, by calling
    # `scope_allows`, so the DynamoDB attribute name appears nowhere in the branch.
    #
    # Deliberately NOT including the bare local name `allowed_versions`: a variable
    # name is satisfied by a docstring or a parameter list, which is weaker evidence
    # than a call to the matcher or a reference to the stored attribute.
    "scope_allows",
)

# --- Function URL route checks (S6-S9) ---------------------------------------
# Routes on a Function URL app that need no per-user authorization. Kept as a
# module constant (like ALLOWED_UNAUTH_METHODS) so adding one is a code change a
# reviewer sees, not a data edit.
FUNCTION_URL_OPEN_ROUTES = {
    "GET /health",  # liveness probe; no caller data, no side effects
}

# Ways the handler can name the identity the TRANSPORT verified, as opposed to
# one the client asserted in the request body.
VERIFIED_IDENTITY_TOKENS = (
    "resolve_caller_sub(",
    "_caller_sub(",
    "caller_sub_from_request_context",
)
# Ways the handler can name the identity the CLIENT asserted.
CLIENT_IDENTITY_TOKENS = (
    "body.callerSub",
    'body.get("callerSub"',
    "body.get('callerSub'",
    'body["callerSub"]',
    "body['callerSub']",
)
# Refusing the request outright (as opposed to preferring one value silently).
# Deliberately loose — what S8 actually requires is the CONJUNCTION of naming the
# verified identity, comparing it (`!=`) and refusing, inside one function. Any
# of these tokens alone proves nothing.
IDENTITY_REJECT_TOKENS = ("403", "PermissionError", "HTTPException", "raise ")

# The COMPLETE key schema for a function_url_endpoints entry and for one of its
# route policies, as documented in the header of that section in
# scripts/api_rbac_expectations.yaml. An unrecognised key is a FAIL.
#
# This exists because a misspelling was silent and consequential. `residual_gapp:`
# instead of `residual_gap:` produced no FAIL at all — just an S0 WARN saying
# GAP-07 was defined but unreferenced — while quietly removing the gap from the
# accepted-risk register that `--strict` and the published security snapshot both
# read. The same is true of every other key here: a typo'd `enforced_in` silently
# stops S9 checking the processor. Keys are data, so nothing else would catch it.
ENDPOINT_ENTRY_KEYS = frozenset({"auth_type", "handler", "routes", "note"})
ROUTE_POLICY_KEYS = frozenset(
    {
        "groups",
        "ownership",
        "equivalent_op",
        "enforced_in",
        "known_gap",
        "residual_gap",
        "note",
    }
)


class Finding:
    __slots__ = ("check", "level", "op", "message")

    def __init__(self, check: str, level: str, message: str, op: str = ""):
        self.check = check
        self.level = level  # FAIL | WARN
        self.op = op
        self.message = message

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "level": self.level,
            "op": self.op,
            "message": self.message,
        }


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # PyYAML — already a dependency of the repo tooling
    except ImportError:  # pragma: no cover
        print("ERROR: PyYAML is required (pip install pyyaml).", file=sys.stderr)
        sys.exit(2)
    with path.open() as fh:
        return yaml.safe_load(fh)


def _read(path: Path) -> str:
    if not path.exists():
        print(f"ERROR: expected file not found: {path}", file=sys.stderr)
        sys.exit(2)
    return path.read_text()


# --- op-universe extraction --------------------------------------------------


def field_function_map_ops(template_text: str) -> set[str]:
    """Pull the field keys out of the HttpApiFieldFunctionMapParam JSON blob.

    The Value is a !Sub block-scalar containing a JSON object of
    "fieldName": "${Resource}" pairs. We don't need it to be valid JSON (it has
    CFN intrinsics); we just harvest the quoted keys that sit at the start of a
    line and are followed by a colon.
    """
    m = re.search(r"HttpApiFieldFunctionMapParam:(.*?)\n  \w", template_text, re.S)
    blob = m.group(1) if m else template_text
    # keys look like:   "fieldName": "${...}"
    return set(re.findall(r'"([a-zA-Z][a-zA-Z0-9]*)"\s*:\s*"\$\{', blob))


def ddb_direct_ops(ddb_text: str) -> set[str]:
    """Extract the _HANDLED set literal from ddb_direct.py."""
    m = re.search(r"_HANDLED\s*=\s*\{(.*?)\}", ddb_text, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([a-zA-Z][a-zA-Z0-9]*)"', m.group(1)))


def field_aliases(dispatcher_text: str) -> set[str]:
    """Extract the FIELD_ALIASES keys from the dispatcher. These are additional
    routable field names that resolve to an already-mapped op."""
    m = re.search(r"FIELD_ALIASES[^=]*=\s*\{(.*?)\}", dispatcher_text, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([a-zA-Z][a-zA-Z0-9]*)"\s*:', m.group(1)))


# --- schema directive extraction ---------------------------------------------


def schema_field_groups(schema_text: str) -> dict[str, object]:
    """Map each Query/Mutation field -> declared cognito_groups.

    Value semantics:
      set[str]   -> restricted to those groups
      "ANY"      -> @aws_cognito_user_pools with no group arg (any authed)
      "IAM_ONLY" -> only @aws_iam (no cognito provider on the field)
    Fields decorated with both @aws_cognito_user_pools and @aws_iam and NO
    cognito_groups arg are treated as ANY (any authed Cognito user).
    """
    result: dict[str, object] = {}
    for type_name in ("Query", "Mutation"):
        body = _extract_type_body(schema_text, type_name)
        if body is None:
            continue
        for field, defn in _iter_fields(body):
            groups = _parse_directive(defn)
            if groups is not None:
                result[field] = groups
    return result


def _extract_type_body(text: str, type_name: str) -> str | None:
    # `type Query @aws_... {  ...  }` — capture the brace body. Anchor tightly:
    # after the type name only whitespace and @directive tokens may precede the
    # `{`, so the phrase "type Query" appearing in a comment (followed by prose)
    # is not matched.
    m = re.search(
        rf"\btype\s+{type_name}[ \t]*(?:@[\w]+(?:\([^)]*\))?[ \t]*)*\{{",
        text,
    )
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _iter_fields(body: str):
    """Yield (field_name, full_definition_text) for each top-level field.

    Handles multi-line argument lists and directives on following lines by
    tracking parenthesis+bracket depth: a new field can only begin when depth
    is 0, so `startDateTime: AWSDateTime` inside an argument list is never
    mistaken for a field. A field's definition runs from its opening line up to
    (but not including) the next field's opening line.
    """
    lines = body.splitlines()
    starts: list[tuple[int, str]] = []  # (line index, field name)
    depth = 0
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        at_top = depth == 0
        # update depth AFTER deciding whether this line starts a field, using
        # the depth as it was entering the line
        if at_top and not stripped.startswith(("#", '"')):
            m = re.match(r"([a-zA-Z]\w*)\s*[(:]", stripped)
            if m:
                starts.append((i, m.group(1)))
        depth += raw.count("(") + raw.count("[") - raw.count(")") - raw.count("]")

    for idx, (line_no, name) in enumerate(starts):
        hard_end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        # Trim to the field's OWN signature + directive lines: a GraphQL field's
        # directives immediately follow it (on the signature line or continuation
        # lines starting with @). Comments/descriptions after a blank line belong
        # to the NEXT field, and prose in them can mention directive tokens — so
        # stop at the first blank or comment/description line once we're outside
        # the argument list.
        d = 0
        end = hard_end
        for j in range(line_no, hard_end):
            s = lines[j].strip()
            if j > line_no and d == 0 and (
                s == "" or s.startswith("#") or s.startswith('"')
            ):
                end = j
                break
            d += (
                lines[j].count("(") + lines[j].count("[")
                - lines[j].count(")") - lines[j].count("]")
            )
        yield name, "\n".join(lines[line_no:end])


def _parse_directive(defn: str) -> object | None:
    has_cognito = "@aws_cognito_user_pools" in defn
    has_iam = "@aws_iam" in defn
    # @aws_auth(cognito_groups) is SILENTLY IGNORED on a multi-auth API (this
    # API adds AWS_IAM), so a field relying on it is effectively open to any
    # authenticated user. Flag it as a distinct value so S2 can call it out.
    if "@aws_auth" in defn and not has_cognito:
        return "AWS_AUTH_IGNORED"
    gm = re.search(r"@aws_cognito_user_pools\(cognito_groups:\s*\[([^\]]*)\]", defn)
    if gm:
        return set(re.findall(r'"([^"]+)"', gm.group(1)))
    if has_cognito:
        return "ANY"
    if has_iam:
        return "IAM_ONLY"
    return None  # no directive -> inherits type default (ANY authed); ignore


# --- template method auth extraction -----------------------------------------


def template_methods(template_text: str) -> list[tuple[str, str]]:
    """Return (LogicalId, AuthorizationType) for each AWS::ApiGateway::Method."""
    out = []
    # Match a resource block: "  LogicalId:\n    Type: AWS::ApiGateway::Method"
    for m in re.finditer(
        r"^  (\w+):\n(?:    .*\n|\n)*?    Type: AWS::ApiGateway::Method\b",
        template_text,
        re.M,
    ):
        logical = m.group(1)
        # find AuthorizationType within this resource's block (until next 2-space id)
        block_start = m.start()
        nxt = re.search(r"\n  \w+:\n", template_text[m.end():])
        block_end = m.end() + (nxt.start() if nxt else 0)
        block = template_text[block_start : block_end or len(template_text)]
        am = re.search(r"AuthorizationType:\s*(\w+)", block)
        out.append((logical, am.group(1) if am else "UNKNOWN"))
    return out


# --- Function URL extraction --------------------------------------------------


def _resource_block(template_text: str, logical_id: str) -> str:
    """Return the YAML text of one top-level (2-space indented) resource block."""
    m = re.search(rf"^  {re.escape(logical_id)}:\n", template_text, re.M)
    if not m:
        return ""
    nxt = re.search(r"^  \w+:\n", template_text[m.end() :], re.M)
    end = m.end() + (nxt.start() if nxt else len(template_text))
    return template_text[m.start() : end]


# Every spelling of "this Function URL points at that function" that CloudFormation
# accepts. `!Ref Fn` and `!GetAtt Fn.Arn` are equally valid and equally idiomatic;
# reading only `!Ref` made S6 fail OPEN, because an unresolved target left the
# handler-containment check with nothing to compare and it was skipped silently.
_TARGET_FUNCTION_RE = re.compile(
    r"TargetFunctionArn:\s*(?:"
    r"!Ref\s+(?P<ref>\w+)"
    r"|!GetAtt\s+(?P<getatt>\w+)(?:\.\w+)*"
    r"|Ref:\s*(?P<long_ref>\w+)"
    r"|Fn::GetAtt:\s*\[?\s*[\"']?(?P<long_getatt>\w+)"
    r")"
)


def lambda_url_resources(template_text: str) -> dict[str, dict[str, str]]:
    """Return {LogicalId: {auth_type, target}} for each AWS::Lambda::Url.

    ``target`` is the logical id of the function the URL fronts, or ``""`` when
    it could not be resolved — which callers must treat as a FAILURE, not as
    "nothing to check". A scanner that cannot find its subject has to say so.
    """
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(
        r"^  (\w+):\n(?:    .*\n|\n)*?    Type: AWS::Lambda::Url\b",
        template_text,
        re.M,
    ):
        logical = m.group(1)
        block = _resource_block(template_text, logical)
        auth = re.search(r"AuthType:\s*(\S+)", block)
        target = _TARGET_FUNCTION_RE.search(block)
        out[logical] = {
            "auth_type": auth.group(1) if auth else "UNKNOWN",
            "target": next(
                (v for v in (target.groupdict().values() if target else ()) if v),
                "",
            ),
        }
    return out


def function_code_uri(template_text: str, logical_id: str) -> str:
    """CodeUri of a serverless function resource ('' if not found)."""
    block = _resource_block(template_text, logical_id)
    m = re.search(r"^      CodeUri:\s*(\S+)", block, re.M)
    return m.group(1) if m else ""


def app_routes(handler_text: str) -> dict[str, str]:
    """Map ``"<METHOD> <path>"`` -> handler function body for a FastAPI app.

    The body runs from the decorator to the next top-level decorator or ``def``,
    which is enough to see how the route resolves the caller identity.
    """
    routes: dict[str, str] = {}
    decorators = list(
        re.finditer(
            r'^@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"',
            handler_text,
            re.M,
        )
    )
    for m in decorators:
        # The route's own `def` immediately follows the decorator, so step past
        # it before looking for the start of the NEXT route/function.
        search_from = m.end()
        own = re.search(r"^(?:async )?def ", handler_text[search_from:], re.M)
        if own:
            search_from += own.end()
        nxt = re.search(
            r"^(?:@app\.|def |async def )", handler_text[search_from:], re.M
        )
        end = search_from + (nxt.start() if nxt else len(handler_text) - search_from)
        routes[f"{m.group(1).upper()} {m.group(2)}"] = handler_text[m.start() : end]
    return routes


def _first_index(text: str, tokens) -> int:
    """Lowest index at which any of ``tokens`` occurs, or -1."""
    hits = [text.index(t) for t in tokens if t in text]
    return min(hits) if hits else -1


# --- S7 structural half: the resolved identity must not be rebound ------------
# The token-position half of S7 compares the FIRST mention of a verified-identity
# spelling against the first mention of a client-identity spelling. That is blind
# to a body value re-admitted under a spelling not in CLIENT_IDENTITY_TOKENS, e.g.
#
#     _claimed = body.model_dump().get('callerSub') or ''
#     if _claimed:
#         caller_sub = _claimed
#
# which is issue #920's original defect (the client's claimed identity wins
# unconditionally) and which left the scan at exit 0 / 0 FAIL. Rather than chase
# spellings, assert the invariant: the name that receives the verified identity is
# assigned exactly once in the route's own scope. Measured on the clean sources,
# both routes assign it once, so this is not a false-positive risk today.

# Callee names that resolve a transport-verified caller identity. Mirrors
# VERIFIED_IDENTITY_TOKENS above, as callee names rather than substrings.
VERIFIED_IDENTITY_CALLEES = (
    "_resolve_caller_sub",
    "resolve_caller_sub",
    "_caller_sub",
    "caller_sub_from_request_context",
)


def _route_function_nodes(handler_text: str) -> dict:
    """Map ``"<METHOD> <path>"`` -> the route's AST function node."""
    try:
        tree = ast.parse(handler_text)
    except SyntaxError:
        return {}
    routes = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "app"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                routes[f"{dec.func.attr.upper()} {dec.args[0].value}"] = node
    return routes


def _own_scope_statements(fn) -> list:
    """Statements in ``fn``'s own scope, not descending into nested functions.

    A nested ``def`` has its own scope, so a binding there shadows rather than
    rebinds; counting it would be a false positive.
    """
    out: list = []
    stack = list(fn.body)
    while stack:
        st = stack.pop()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(st)
        for field in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(getattr(st, field, []) or [])
    return out


def _callees(node) -> set:
    """Every callee name invoked in ``node``'s subtree."""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def route_identity_rebindings(fn) -> tuple[str, list[int]] | None:
    """``(name, lines)`` if the route's resolved identity is bound >1 time.

    Returns ``None`` when the route binds it exactly once, binds nothing, or does
    not resolve a verified identity at all (the latter is S7's other half).
    """
    resolved: set[str] = set()
    for st in _own_scope_statements(fn):
        if not isinstance(st, ast.Assign):
            continue
        if not (_callees(st.value) & set(VERIFIED_IDENTITY_CALLEES)):
            continue
        for tgt in st.targets:
            if isinstance(tgt, ast.Name):
                resolved.add(tgt.id)
    if len(resolved) != 1:
        return None
    name = resolved.pop()

    lines: list[int] = []
    for st in _own_scope_statements(fn):
        targets = []
        if isinstance(st, ast.Assign):
            targets = list(st.targets)
        elif isinstance(st, (ast.AugAssign, ast.AnnAssign)):
            targets = [st.target]
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            targets = [st.target]
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            targets = [i.optional_vars for i in st.items if i.optional_vars]
        bound = False
        for tgt in targets:
            for sub in ast.walk(tgt):
                if (
                    isinstance(sub, ast.Name)
                    and isinstance(sub.ctx, ast.Store)
                    and sub.id == name
                ):
                    bound = True
        for sub in ast.walk(st):
            if (
                isinstance(sub, ast.NamedExpr)
                and isinstance(sub.target, ast.Name)
                and sub.target.id == name
            ):
                bound = True
        if bound:
            lines.append(st.lineno)
    return (name, sorted(lines)) if len(lines) > 1 else None


def module_functions(text: str) -> list[str]:
    """Split a module into function texts (top-level ``def``/``async def``)."""
    starts = [m.start() for m in re.finditer(r"^(?:async )?def ", text, re.M)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        out.append(text[s:e])
    return out


# A module-level constant whose NAME says it holds a group list and whose value is
# a tuple/list/set literal, e.g. `_AGENT_CHAT_GROUPS = ("Admin", "Author", ...)`.
_GROUP_CONST_RE = re.compile(
    r"^(_?[A-Z][A-Z0-9_]*GROUPS?[A-Z0-9_]*)\s*=\s*"
    r"(?:frozenset\()?[\(\[\{]([^)\]}]*)[\)\]\}]",
    re.M,
)
_GROUP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def enforced_group_names(text: str) -> set[str] | None:
    """The exact group list a handler enforces, read from its module constant.

    Returns the set of string literals in the first (and only) module-level
    ``*GROUP(S)*`` constant whose value is a tuple/list/set of plausible group
    names, or ``None`` when there is no such constant or more than one — in which
    case the caller falls back to the weaker containment check rather than
    guessing which constant is the policy.

    Only string literals inside the literal are read, so the prose comment above
    the constant (which names the group deliberately EXCLUDED) cannot leak in and
    turn a correct policy into a failure.
    """
    candidates: list[set[str]] = []
    for m in _GROUP_CONST_RE.finditer(text):
        names = set(re.findall(r"""["']([^"']+)["']""", m.group(2)))
        if names and all(_GROUP_NAME_RE.match(n) for n in names):
            candidates.append(names)
    return candidates[0] if len(candidates) == 1 else None


# --- S4 helper: which code an operation can actually reach --------------------
#
# S4 used to grep the whole `enforced_in` FILE for `allowedConfigVersions`. That
# is satisfiable by an unrelated function in the same module, and it was:
# `getDocumentCount` and `listDocuments` share
# `list_documents_gsi_resolver/index.py`, `listDocuments` referenced the scope,
# `getDocumentCount` referenced nothing and resolved no caller at all — and its
# `scope_filtered: true` declaration passed the check for months on the strength
# of its neighbour's string.
#
# So the check now reads only the code the operation itself can reach: the BODY of
# each dispatch branch its name selects, plus the transitive closure of module-level
# functions those bodies call. A module serving more than one operation in the
# expectations file MUST have a discoverable per-op dispatch (or an explicit
# `scope_enforced_in:`), because whole-file scope is what let the false declaration
# through.
#
# Two details that decide whether this is per-operation at all:
#
#   * Only the branch **body** is read, never the `ast.If` node. Unparsing an `If`
#     emits its `orelse` subtree too, which is where every later `elif` lives — so
#     a whole-node read of `listDocuments`' branch would include
#     `get_document_count`, and the check would be satisfied by the sibling
#     reference its own error message says enforces nothing.
#   * The comparison operator is read, not just the operand. `if f != "op":` names
#     the operation but selects the branch where it is NOT handled.
#
# What this check CANNOT prove, so that nobody reads more into a green S4 than is
# there: it is a *reference* check, not dataflow. It establishes that the code an
# operation reaches refers to the caller's config-version scope. It cannot establish
# that the value being matched was actually resolved from the UsersTable — an
# operation that kept `scope_allows(allowed_versions, ...)` while `allowed_versions`
# became a hardcoded `None` still passes. That shape is what the per-site unit
# suites assert (a DynamoDB error must deny, an out-of-scope document must not be
# returned), and no static rule over one module substitutes for them. What S4 does
# catch, and did, is an operation that refers to the scope **nowhere**.


class _CallCollector(ast.NodeVisitor):
    def __init__(self):
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call):  # noqa: N802
        func = node.func
        if isinstance(func, ast.Name):
            self.names.add(func.id)
        elif isinstance(func, ast.Attribute):
            self.names.add(func.attr)
        self.generic_visit(node)


def _called_names(node: ast.AST) -> set[str]:
    collector = _CallCollector()
    collector.visit(node)
    return collector.names


def _module_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Every function defined in the module, by name (nested ones included)."""
    functions: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    return functions


def _names_literal(node: ast.AST, literal: str) -> bool:
    """Whether an operand is this string, or a collection literal containing it."""
    if isinstance(node, ast.Constant):
        return node.value == literal
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(
            isinstance(element, ast.Constant) and element.value == literal
            for element in node.elts
        )
    return False


def _selects_literal(test: ast.AST, literal: str) -> bool:
    """Whether an `if` test dispatches TO this operation's branch.

    Covers `field == "op"`, `"op" == field` and `field in ("op", ...)`, which are the
    three shapes the resolvers in this tree use. Two things it deliberately does not
    treat as a dispatch:

    * a **negated** comparison. `if field != "op":` and `if field not in (...)` name
      the operation while selecting the branch where it is *not* handled, so reading
      them as its dispatch would attribute a sibling's code to it.
    * a string that merely appears elsewhere in the module — a required-groups dict
      key, a log message — which is not an `if` test at all.
    """
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, ast.Eq):
                if _names_literal(left, literal) or _names_literal(
                    comparator, literal
                ):
                    return True
            elif isinstance(operator, ast.In):
                if _names_literal(comparator, literal):
                    return True
            left = comparator
    return False


def _dispatch_branches(tree: ast.Module, op: str) -> list[ast.stmt]:
    """The statements in every `if`/`elif` branch selected by this operation's name.

    All matching branches, not the narrowest: the configuration resolver dispatches
    the five revision operations twice — an outer `elif operation in (...)` that
    performs the shared profile-level scope check, then an inner `if operation ==
    "..."` that picks the handler. Taking only the inner branch would miss the check
    that actually enforces the scope, and taking only the outer one would make five
    operations indistinguishable. The union is what the operation can reach.

    The branch **body** is returned, never the `ast.If` node itself. An `If` carries
    its `orelse` — every subsequent `elif` — so returning the node would hand back
    the whole rest of the dispatch chain and make the check module-wide again. That
    is not theoretical: it is precisely how `listDocuments` would be credited with
    `getDocumentCount`'s code, and vice versa.
    """
    statements: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _selects_literal(node.test, op):
            statements.extend(node.body)
    return statements


def op_scope_source(
    text: str, op: str, *, declared_entry: str | None, sole_op: bool
) -> tuple[str | None, str]:
    """The source an operation can reach, for the scope-reference check.

    Returns ``(error, source)``. ``error`` is non-None when the entry point could
    not be determined, which is itself a finding: an operation declared
    scope-enforced in a module whose dispatch cannot be located is an operation
    nobody can verify.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return f"could not parse {op}'s enforced_in file: {exc}", ""

    functions = _module_functions(tree)

    entries: list[ast.AST] = []
    if declared_entry:
        declared = functions.get(declared_entry)
        if declared is None:
            return (
                f"scope_enforced_in names '{declared_entry}', which the file does "
                "not define",
                "",
            )
        entries = [declared]
    else:
        entries = list(_dispatch_branches(tree, op))
        if not entries:
            if not sole_op:
                return (
                    "no per-operation dispatch on this name could be found, and this "
                    "file is the enforced_in for more than one operation in the "
                    "expectations file — so the check would fall back to module "
                    "scope, which is what let a false `scope_filtered` declaration "
                    "pass. Add `scope_enforced_in: <function>` to the entry",
                    "",
                )
            # Sole operation in this file, so there is nothing for the check to
            # confuse it with. The handler's call closure is still narrower than the
            # file: unreachable and dead code is excluded.
            entries = [
                functions.get("handler") or functions.get("lambda_handler") or tree
            ]

    # Transitive closure over module-level functions the entries can call.
    reachable: list[ast.AST] = list(entries)
    seen: set[str] = set()
    pending = [name for entry in entries for name in _called_names(entry)]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        target = functions.get(name)
        if target is None:
            continue
        reachable.append(target)
        pending.extend(_called_names(target))

    return None, "\n".join(ast.unparse(node) for node in reachable)


# --- checks ------------------------------------------------------------------


def run_checks(strict: bool, repo: Path | None = None) -> list[Finding]:
    """Run every check against ``repo`` (the real repository by default).

    ``repo`` exists so the tests can drive these checks over a deliberately
    DEFECTIVE fixture tree and assert the specific finding appears. Without it
    the only thing a test could assert against the live sources is the absence of
    failures, which passes just as happily when a check has been deleted — the
    exact "a check that cannot fail" failure mode this scanner is meant to close.
    See scripts/sdlc/tests/test_scan_api_rbac_function_urls.py.
    """
    repo = Path(repo) if repo is not None else REPO
    expectations = repo / "scripts" / "api_rbac_expectations.yaml"
    api_resolvers = repo / "nested" / "api-resolvers"
    dispatcher_dir = api_resolvers / "src" / "lambda" / "http_api_dispatcher"

    spec = _load_yaml(expectations)
    ops: dict[str, dict] = spec["operations"]
    known_gaps: dict[str, dict] = spec.get("known_gaps") or {}
    template_text = _read(api_resolvers / "template.yaml")
    schema_text = _read(api_resolvers / "src" / "api" / "schema.graphql")
    ddb_text = _read(dispatcher_dir / "ddb_direct.py")

    findings: list[Finding] = []

    def gap_or_fail(op_name: str, check: str, message: str):
        gap = ops[op_name].get("known_gap") if op_name in ops else None
        level = "WARN" if (gap and not strict) else "FAIL"
        suffix = f" [{gap}]" if gap else ""
        findings.append(Finding(check, level, message + suffix, op_name))

    # --- S1: manifest completeness -----------------------------------------
    dispatcher_text = _read(dispatcher_dir / "index.py")
    routable = (
        field_function_map_ops(template_text)
        | ddb_direct_ops(ddb_text)
        | field_aliases(dispatcher_text)
    )
    expected = set(ops)
    for op in sorted(routable - expected):
        findings.append(
            Finding(
                "S1", "FAIL",
                f"routable op '{op}' has no entry in api_rbac_expectations.yaml "
                "(add its RBAC policy)",
                op,
            )
        )
    for op in sorted(expected - routable):
        findings.append(
            Finding(
                "S1", "FAIL",
                f"expectations entry '{op}' is not a routable op "
                "(stale — remove it or fix the name)",
                op,
            )
        )

    # --- S2: schema <-> expectations ---------------------------------------
    declared = schema_field_groups(schema_text)
    for op, o in ops.items():
        exp = o["groups"]
        exp_for_schema = o.get("schema_groups", exp)  # documented intentional drift
        dec = declared.get(op)
        if dec is None:
            continue  # not in schema (ddb-direct-only or conditional feature op)
        if dec == "AWS_AUTH_IGNORED" and exp_for_schema != "AWS_AUTH_IGNORED":
            gap_or_fail(
                op, "S2",
                "schema uses @aws_auth(cognito_groups) which is SILENTLY IGNORED "
                "on this multi-auth API — the field is open to any authenticated "
                "user at the gateway; use @aws_cognito_user_pools(cognito_groups) "
                "instead (server-side resolver check is the only real gate)",
            )
            continue
        want = (
            exp_for_schema if exp_for_schema in ("ANY", "IAM_ONLY", "AWS_AUTH_IGNORED")
            else set(exp_for_schema)
        )
        if want != dec:
            dw = sorted(dec) if isinstance(dec, set) else dec
            ww = sorted(want) if isinstance(want, set) else want
            gap_or_fail(
                op, "S2",
                f"schema.graphql declares {dw} but expectations say {ww}",
            )

    # --- S3: resolver enforcement ------------------------------------------
    for op, o in ops.items():
        src = repo / o["enforced_in"]
        if not src.exists():
            findings.append(
                Finding("S3", "FAIL",
                        f"enforced_in file missing: {o['enforced_in']}", op))
            continue
        text = src.read_text()
        groups = o["groups"]
        if isinstance(groups, list) or groups == "IAM_ONLY":
            # must have a real enforcement pattern
            if not any(p in text for p in ENFORCE_PATTERNS):
                gap_or_fail(
                    op, "S3",
                    f"no group-enforcement pattern found in {o['enforced_in']}",
                )
        else:  # ANY-auth
            if o.get("ownership"):
                if not any(p in text for p in OWNERSHIP_PATTERNS):
                    gap_or_fail(
                        op, "S3",
                        f"ownership scoping expected but no owner/user check in "
                        f"{o['enforced_in']}",
                    )
            elif not o.get("known_gap"):
                # ANY with no ownership and no gap is fine (intentionally open),
                # but flag if it's a mutation with no enforcement at all so a
                # reviewer confirms it's meant to be wide open.
                if o["kind"] == "mutation" and not any(
                    p in text for p in ENFORCE_PATTERNS + OWNERSHIP_PATTERNS
                ):
                    findings.append(
                        Finding("S3", "WARN",
                                f"ANY-auth mutation with no visible check in "
                                f"{o['enforced_in']} — confirm this is intended",
                                op))

    # --- S4: scope enforcement ---------------------------------------------
    # Scoped to the code the OPERATION can reach, not the whole file — see
    # `op_scope_source` for why, and for the false declaration that motivated it.
    scope_flagged = {
        name
        for name, cfg in ops.items()
        if cfg.get("scope_checked") or cfg.get("scope_filtered")
    }
    # Counted over EVERY operation declared against a file, not only the
    # scope-flagged ones. A file holding one scope-flagged op beside five unflagged
    # ones is still a file where module scope credits an operation with a sibling's
    # code, which is the whole reason this check became per-operation.
    ops_per_file: dict[str, int] = {}
    for cfg in ops.values():
        enforced_in = cfg.get("enforced_in")
        if enforced_in:
            ops_per_file[enforced_in] = ops_per_file.get(enforced_in, 0) + 1

    for op in sorted(scope_flagged):
        o = ops[op]
        src = repo / o["enforced_in"]
        if not src.exists():
            continue  # already reported by S3
        error, reachable = op_scope_source(
            src.read_text(),
            op,
            declared_entry=o.get("scope_enforced_in"),
            sole_op=ops_per_file[o["enforced_in"]] == 1,
        )
        if error:
            gap_or_fail(op, "S4", f"{error} ({o['enforced_in']})")
            continue
        if not any(p in reachable for p in SCOPE_PATTERNS):
            gap_or_fail(
                op, "S4",
                "scope enforcement flagged but nothing this operation can reach "
                f"in {o['enforced_in']} references allowedConfigVersions — a "
                "sibling operation's reference in the same module does not "
                "enforce anything here",
            )

    # --- S5: template method auth ------------------------------------------
    for logical, auth in template_methods(template_text):
        if logical in ALLOWED_UNAUTH_METHODS:
            if auth != "NONE":
                findings.append(
                    Finding("S5", "WARN",
                            f"{logical} expected AuthorizationType NONE, got {auth}"))
            continue
        if auth != "COGNITO_USER_POOLS":
            findings.append(
                Finding("S5", "FAIL",
                        f"{logical} has AuthorizationType {auth} — expected "
                        "COGNITO_USER_POOLS (add to ALLOWED_UNAUTH_METHODS only "
                        "if intentionally public)"))

    # --- S6-S9: Lambda Function URL routes ----------------------------------
    endpoints: dict[str, dict] = spec.get("function_url_endpoints") or {}
    main_text = _read(repo / "template.yaml")
    url_resources = lambda_url_resources(main_text)

    def route_gap_or_fail(route_cfg: dict, route: str, check: str, message: str):
        """As gap_or_fail, but for a route.

        Only `known_gap:` downgrades a finding. `residual_gap:` deliberately does
        NOT: it records a transport limitation that is listed in the register for
        auditability while every S6-S9 finding on the route stays a hard FAIL —
        otherwise declaring the residual risk would silently disarm the checks
        that caught the defect it sits next to.
        """
        gap = route_cfg.get("known_gap")
        level = "WARN" if (gap and not strict) else "FAIL"
        suffix = f" [{gap}]" if gap else ""
        findings.append(Finding(check, level, message + suffix, route))

    for logical in sorted(set(url_resources) - set(endpoints)):
        findings.append(
            Finding(
                "S6", "FAIL",
                f"Function URL '{logical}' in template.yaml has no "
                "function_url_endpoints entry in api_rbac_expectations.yaml "
                "(declare its routes and their authorization)",
                logical,
            )
        )
    for logical in sorted(set(endpoints) - set(url_resources)):
        findings.append(
            Finding(
                "S6", "FAIL",
                f"function_url_endpoints entry '{logical}' is not an "
                "AWS::Lambda::Url in template.yaml (stale — remove it or fix "
                "the name)",
                logical,
            )
        )

    for logical, ep in endpoints.items():
        res = url_resources.get(logical)
        declared_routes: dict[str, dict] = ep.get("routes") or {}

        # --- S6: the declaration itself must be well-formed -----------------
        # Every key below is read by name, so a misspelling makes the setting
        # vanish rather than misfire. `residual_gapp:` for `residual_gap:` was
        # measured to leave the scan at zero failures while removing GAP-07 from
        # the accepted-risk register.
        for bad in sorted(set(ep) - ENDPOINT_ENTRY_KEYS):
            findings.append(
                Finding("S6", "FAIL",
                        f"{logical} declares unknown key '{bad}' — a misspelled "
                        f"key is silently ignored (known: "
                        f"{sorted(ENDPOINT_ENTRY_KEYS)})", logical))
        for route, rc in declared_routes.items():
            for bad in sorted(set(rc or {}) - ROUTE_POLICY_KEYS):
                findings.append(
                    Finding("S6", "FAIL",
                            f"route '{route}' on {logical} declares unknown key "
                            f"'{bad}' — a misspelled key is silently ignored "
                            f"(known: {sorted(ROUTE_POLICY_KEYS)})", route))

        if res:
            if res["auth_type"] == "NONE":
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical} has AuthType NONE — the Function URL is "
                            "reachable unauthenticated", logical))
            elif res["auth_type"] != ep.get("auth_type"):
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical} AuthType is {res['auth_type']} but "
                            f"expectations say {ep.get('auth_type')}", logical))
            # Resolve the handler from the template rather than trusting the
            # expectations file, so a retargeted URL cannot keep pointing the
            # scan at the old (still-clean) source file. Both halves of that
            # resolution must be reported when they fail, because an empty
            # result would otherwise skip the containment check below and the
            # scan would validate whatever file the expectations named.
            code_uri = function_code_uri(main_text, res["target"])
            if not res["target"]:
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical}: could not resolve TargetFunctionArn to a "
                            "function logical id, so the declared handler cannot "
                            "be checked against the target's CodeUri", logical))
            elif not code_uri:
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical} targets {res['target']}, which has no "
                            "resolvable CodeUri in template.yaml, so the declared "
                            "handler cannot be checked against it", logical))
            if code_uri and not ep["handler"].startswith(code_uri.rstrip("/")):
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical} targets {res['target']} (CodeUri "
                            f"{code_uri}) but expectations name handler "
                            f"{ep['handler']}", logical))

        handler_src = repo / ep["handler"]
        if not handler_src.exists():
            findings.append(
                Finding("S6", "FAIL",
                        f"handler file missing: {ep['handler']}", logical))
            continue
        handler_text = handler_src.read_text()
        actual_routes = app_routes(handler_text)
        route_nodes = _route_function_nodes(handler_text)
        # The identity decision may live in a sibling module of the same Lambda
        # package (the app file imports FastAPI, so pure logic is factored out to
        # keep it unit-testable). Scan the package's own modules, not the vendored
        # processor copies or its tests.
        package_functions: list[str] = []
        for py in sorted(handler_src.parent.glob("*.py")):
            package_functions.extend(module_functions(py.read_text()))

        for route in sorted(
            set(actual_routes) - set(declared_routes) - FUNCTION_URL_OPEN_ROUTES
        ):
            findings.append(
                Finding("S6", "FAIL",
                        f"{ep['handler']} serves '{route}' with no "
                        "function_url_endpoints route entry (declare its "
                        "authorization)", route))
        for route in sorted(set(declared_routes) - set(actual_routes)):
            findings.append(
                Finding("S6", "FAIL",
                        f"declared route '{route}' is not served by "
                        f"{ep['handler']} (stale entry)", route))

        for route, rc in declared_routes.items():
            body = actual_routes.get(route)
            if body is None:
                continue  # already reported above

            # --- S7: verified identity must win --------------------------
            v_at = _first_index(body, VERIFIED_IDENTITY_TOKENS)
            c_at = _first_index(body, CLIENT_IDENTITY_TOKENS)
            if v_at < 0:
                route_gap_or_fail(
                    rc, route, "S7",
                    f"route resolves no transport-verified caller identity in "
                    f"{ep['handler']}")
            elif 0 <= c_at < v_at:
                route_gap_or_fail(
                    rc, route, "S7",
                    "route prefers the request-body caller identity over the "
                    f"transport-verified one in {ep['handler']} — the verified "
                    "identity must be resolved first, the body value is a "
                    "fallback only")

            # Structural half of S7: resolving the verified identity first means
            # nothing if a later statement overwrites it. The token comparison
            # above only sees the spellings in CLIENT_IDENTITY_TOKENS, so assert
            # the single binding instead — that holds whatever the second value
            # is spelled like.
            node = route_nodes.get(route)
            if node is not None:
                rebound = route_identity_rebindings(node)
                if rebound is not None:
                    name, lines = rebound
                    route_gap_or_fail(
                        rc, route, "S7",
                        f"'{name}' holds the transport-verified caller identity "
                        f"in {ep['handler']} but is assigned {len(lines)} times "
                        f"(lines {lines}) — a later assignment can substitute a "
                        "client-supplied identity for the verified one no matter "
                        "how it is spelled; resolve it once and do not rebind it")

            # --- S8: a contradicting body identity must be refused -------
            # The comparison lives in a shared resolver, so look across the
            # package for a function that both compares the two and refuses.
            if c_at >= 0 and not any(
                any(t in fn for t in VERIFIED_IDENTITY_TOKENS)
                and "!=" in fn
                and any(t in fn for t in IDENTITY_REJECT_TOKENS)
                for fn in package_functions
            ):
                route_gap_or_fail(
                    rc, route, "S8",
                    f"{ep['handler']} accepts a body-supplied caller identity "
                    "but never refuses one that contradicts the verified "
                    "identity (expected an explicit rejection, not a silent "
                    "preference)")

            # --- S9: downstream group enforcement ------------------------
            groups = rc.get("groups")
            enforced_in = rc.get("enforced_in")
            if isinstance(groups, list) and enforced_in:
                src = repo / enforced_in
                if not src.exists():
                    findings.append(
                        Finding("S9", "FAIL",
                                f"enforced_in file missing: {enforced_in}",
                                route))
                else:
                    text = src.read_text()
                    if not any(p in text for p in ENFORCE_PATTERNS):
                        route_gap_or_fail(
                            rc, route, "S9",
                            f"no group-enforcement pattern found in "
                            f"{enforced_in} — a Function URL route declared "
                            f"for {groups} reaches this handler directly")
                    else:
                        # Compare the handler's group list with the declared one
                        # in BOTH directions where possible. Reading the module
                        # constant gives an exact set, so widening the code's
                        # list (adding Reviewer to the agent-chat groups) fails
                        # here and not only via `equivalent_op`.
                        actual = enforced_group_names(text)
                        if actual is not None:
                            if actual != set(groups):
                                route_gap_or_fail(
                                    rc, route, "S9",
                                    f"{enforced_in} enforces "
                                    f"{sorted(actual)} but the route declares "
                                    f"{sorted(groups)} — the code and the "
                                    "expectations must name the same groups")
                        else:
                            # No group-list constant found, so fall back to
                            # containment. NOTE this direction is ASYMMETRIC: it
                            # catches NARROWING the declared list (a declared
                            # group the code never names) but not WIDENING the
                            # code's list. Widening is still caught by the
                            # `equivalent_op` comparison below, which compares the
                            # route's groups with the REST operation's, and by
                            # test_processor_and_resolver_agree_on_the_group_list
                            # in lib/idp_common_pkg/tests/unit/
                            # test_agent_chat_rbac.py, which asserts the two
                            # code-side tuples are equal.
                            missing = [g for g in groups if f'"{g}"' not in text]
                            if missing:
                                route_gap_or_fail(
                                    rc, route, "S9",
                                    f"{enforced_in} has a group check but does "
                                    f"not name {missing} — declared groups "
                                    f"{groups}")
            # The two entry paths to one operation must agree on the policy.
            equivalent = rc.get("equivalent_op")
            if equivalent:
                if equivalent not in ops:
                    findings.append(
                        Finding("S9", "FAIL",
                                f"equivalent_op '{equivalent}' is not a "
                                "declared operation", route))
                elif ops[equivalent]["groups"] != groups:
                    findings.append(
                        Finding("S9", "FAIL",
                                f"route groups {groups} disagree with the "
                                f"equivalent REST op '{equivalent}' "
                                f"({ops[equivalent]['groups']}) — one entry "
                                "path is more permissive than the other",
                                route))

    # --- known_gaps integrity ----------------------------------------------
    referenced = {o["known_gap"] for o in ops.values() if o.get("known_gap")}
    referenced |= {
        rc[key]
        for ep in endpoints.values()
        for rc in (ep.get("routes") or {}).values()
        for key in ("known_gap", "residual_gap")
        if rc.get(key)
    }
    for gid in sorted(referenced - set(known_gaps)):
        findings.append(
            Finding("S0", "FAIL",
                    f"operation references undefined known_gap '{gid}'"))
    for gid in sorted(set(known_gaps) - referenced):
        findings.append(
            Finding("S0", "WARN",
                    f"known_gap '{gid}' is defined but no operation references "
                    "it (fixed? remove it)"))

    # --- accepted-risk register (always surfaced for auditability) ---------
    # Each declared gap is emitted so the report explicitly lists accepted
    # risks rather than hiding them behind a green run. --strict escalates
    # them to FAIL so the scan can be used to verify a gap has been fixed.
    gap_ops: dict[str, list[str]] = {}
    for name, o in ops.items():
        if o.get("known_gap"):
            gap_ops.setdefault(o["known_gap"], []).append(name)
    for ep in endpoints.values():
        for route, rc in (ep.get("routes") or {}).items():
            for key in ("known_gap", "residual_gap"):
                if rc.get(key):
                    gap_ops.setdefault(rc[key], []).append(route)
    for gid in sorted(referenced):
        summary = known_gaps.get(gid, {}).get("summary", "(no summary)")
        ops_list = ", ".join(sorted(gap_ops.get(gid, [])))
        findings.append(
            Finding(
                "GAP", "FAIL" if strict else "WARN",
                f"{gid}: {summary} — affects: {ops_list}",
            )
        )

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Static RBAC scan for the IDP UI API.")
    ap.add_argument("--json", metavar="PATH", help="write findings as JSON to PATH")
    ap.add_argument(
        "--strict", action="store_true",
        help="treat known_gap WARNs as failures (verify a gap was fixed)")
    args = ap.parse_args()

    findings = run_checks(args.strict)
    fails = [f for f in findings if f.level == "FAIL"]
    warns = [f for f in findings if f.level == "WARN"]

    by_check: dict[str, list[Finding]] = {}
    for f in findings:
        by_check.setdefault(f.check, []).append(f)

    print("=== Static API RBAC scan ===")
    for check in sorted(by_check):
        for f in by_check[check]:
            mark = "✗" if f.level == "FAIL" else "⚠"
            loc = f" ({f.op})" if f.op else ""
            print(f"  {mark} [{f.check}]{loc} {f.message}")
    if not findings:
        print("  ✓ no findings — all routable ops declared, enforced, and gated")

    print(f"\n{len(fails)} FAIL, {len(warns)} WARN")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "summary": {"fail": len(fails), "warn": len(warns)},
                    "findings": [f.as_dict() for f in findings],
                },
                indent=2,
            )
        )
        print(f"Wrote {args.json}")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
