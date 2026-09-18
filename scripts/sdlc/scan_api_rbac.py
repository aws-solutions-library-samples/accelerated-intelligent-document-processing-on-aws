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
      may only be a fallback, never preferred.
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
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXPECTATIONS = REPO / "scripts" / "api_rbac_expectations.yaml"
TEMPLATE = REPO / "nested" / "api-resolvers" / "template.yaml"
# The Function URLs live in the parent stack, not the api-resolvers nested stack.
MAIN_TEMPLATE = REPO / "template.yaml"
SCHEMA = REPO / "nested" / "api-resolvers" / "src" / "api" / "schema.graphql"
DISPATCHER_DIR = (
    REPO / "nested" / "api-resolvers" / "src" / "lambda" / "http_api_dispatcher"
)
DDB_DIRECT = DISPATCHER_DIR / "ddb_direct.py"
DISPATCHER = DISPATCHER_DIR / "index.py"

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
SCOPE_PATTERNS = ("allowedConfigVersions", "allowed_config_versions")

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


def lambda_url_resources(template_text: str) -> dict[str, dict[str, str]]:
    """Return {LogicalId: {auth_type, target}} for each AWS::Lambda::Url."""
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(
        r"^  (\w+):\n(?:    .*\n|\n)*?    Type: AWS::Lambda::Url\b",
        template_text,
        re.M,
    ):
        logical = m.group(1)
        block = _resource_block(template_text, logical)
        auth = re.search(r"AuthType:\s*(\S+)", block)
        target = re.search(r"TargetFunctionArn:\s*!Ref\s+(\w+)", block)
        out[logical] = {
            "auth_type": auth.group(1) if auth else "UNKNOWN",
            "target": target.group(1) if target else "",
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


def module_functions(text: str) -> list[str]:
    """Split a module into function texts (top-level ``def``/``async def``)."""
    starts = [m.start() for m in re.finditer(r"^(?:async )?def ", text, re.M)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        out.append(text[s:e])
    return out


# --- checks ------------------------------------------------------------------


def run_checks(strict: bool) -> list[Finding]:
    spec = _load_yaml(EXPECTATIONS)
    ops: dict[str, dict] = spec["operations"]
    known_gaps: dict[str, dict] = spec.get("known_gaps") or {}
    template_text = _read(TEMPLATE)
    schema_text = _read(SCHEMA)
    ddb_text = _read(DDB_DIRECT)

    findings: list[Finding] = []

    def gap_or_fail(op_name: str, check: str, message: str):
        gap = ops[op_name].get("known_gap") if op_name in ops else None
        level = "WARN" if (gap and not strict) else "FAIL"
        suffix = f" [{gap}]" if gap else ""
        findings.append(Finding(check, level, message + suffix, op_name))

    # --- S1: manifest completeness -----------------------------------------
    dispatcher_text = _read(DISPATCHER)
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
        src = REPO / o["enforced_in"]
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
    for op, o in ops.items():
        if not (o.get("scope_checked") or o.get("scope_filtered")):
            continue
        src = REPO / o["enforced_in"]
        if not src.exists():
            continue  # already reported by S3
        text = src.read_text()
        if not any(p in text for p in SCOPE_PATTERNS):
            gap_or_fail(
                op, "S4",
                f"scope enforcement flagged but allowedConfigVersions not "
                f"referenced in {o['enforced_in']}",
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
    main_text = _read(MAIN_TEMPLATE)
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
            # scan at the old (still-clean) source file.
            code_uri = function_code_uri(main_text, res["target"])
            if code_uri and not ep["handler"].startswith(code_uri.rstrip("/")):
                findings.append(
                    Finding("S6", "FAIL",
                            f"{logical} targets {res['target']} (CodeUri "
                            f"{code_uri}) but expectations name handler "
                            f"{ep['handler']}", logical))

        handler_src = REPO / ep["handler"]
        if not handler_src.exists():
            findings.append(
                Finding("S6", "FAIL",
                        f"handler file missing: {ep['handler']}", logical))
            continue
        handler_text = handler_src.read_text()
        actual_routes = app_routes(handler_text)
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
                src = REPO / enforced_in
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
                        missing = [g for g in groups if f'"{g}"' not in text]
                        if missing:
                            route_gap_or_fail(
                                rc, route, "S9",
                                f"{enforced_in} has a group check but does not "
                                f"name {missing} — declared groups {groups}")
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
