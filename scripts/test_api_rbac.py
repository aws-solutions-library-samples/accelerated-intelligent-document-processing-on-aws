#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Live RBAC + auth test for the IDP UI REST API (the /op/<field> dispatcher that
replaced AppSync). This is the DYNAMIC half of `make api-test`; the static half
is scripts/sdlc/scan_api_rbac.py.

WHY THIS EXISTS
---------------
Under AppSync, per-field @aws_cognito_user_pools(cognito_groups:[...]) directives
gated operations *before* the resolver ran. The REST API Gateway uses a Cognito
authorizer that only *authenticates* — so each resolver (and the dispatcher's
ddb_direct module) must re-enforce the group check itself. curl/idp-cli smoke
tests with an admin identity don't exercise this, so role regressions slip
through. This script drives the API as each Cognito group (Admin/Author/Viewer/
Reviewer), a config-version-SCOPED Author, and unauthenticated, and asserts the
authorization outcome for every UI operation.

The expected policy per operation is loaded from scripts/api_rbac_expectations.yaml
(the single source of truth, shared with the static scanner). Do NOT hardcode
expectations here — edit the YAML.

WHAT IT CHECKS
--------------
  * unauthenticated            -> 401 for a protected op
  * a DISALLOWED role          -> 403 (errorType "Unauthorized")
  * an ALLOWED role            -> NOT denied (a 400 from intentionally-bogus
                                 mutation args is fine — proves auth passed)
  * IAM-only backend ops       -> 403 for every Cognito role
  * config-version scope       -> a scoped Author is denied an out-of-scope
                                 version (in-band Unauthorized) and Admin is not
  * token negatives            -> tampered / wrong-issuer / no-token -> 401

SAFETY
------
* Read ops use harmless args. Mutation ops use nonexistent ids so an *allowed*
  caller fails benign validation instead of mutating real data.
* Ops flagged skip_allowed in the YAML (live-agent / KB / chat starts) are
  exercised ONLY for their denied cells; the allowed-role call is skipped.
* Test users (test-rbac-<role>@example.invalid) are created in setup with a
  random per-run password and removed in teardown. The script temporarily adds
  ALLOW_ADMIN_USER_PASSWORD_AUTH to the UI app client and ALWAYS reverts the
  client to its prior auth flows — even with --no-teardown, which only keeps
  the test users. Both UsersTable items the scoped user needs — the USER# row
  and its SUB#<sub> pointer — are deleted in teardown, and --teardown-only
  re-derives their keys from the table when it has no record of them.
* Nothing here is destructive to real stack data.

USAGE
-----
  AWS_PROFILE=default python3 scripts/test_api_rbac.py --stack-name IDP1 --region us-west-2
  ... --report-dir api-test-results   # write auditable report.json/.md/meta.json
  ... --setup-only | --no-teardown | --teardown-only

Requires: awscli v2 on PATH; credentials with Cognito admin + CloudFormation read
+ DynamoDB read/write on the UsersTable (the deploy account creds are enough).
"""

import argparse
import base64
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import NamedTuple, Optional

# Stack resolution and Cognito token helpers are shared with the ZAP DAST probe
# (scripts/sdlc/codebuild_deployment.py) via scripts/rbac_common.py so the two
# can't drift on the stack shape or auth-flow capture/restore logic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Mandatory security-focused test cases (IDOR, token lifecycle, deleted-resource,
# input validation, TLS) layered on top of the RBAC matrix below.
import api_security_cases as sec  # noqa: E402
from rbac_common import (  # noqa: E402
    AWS_BIN,
    aws,
    create_cognito_user,
    delete_cognito_user,
    enable_admin_auth,
    get_id_token,
    global_sign_out,
    resolve_stack,
    restore_auth_flows,
)

ROLES = ["Admin", "Author", "Viewer", "Reviewer"]
# A second, independent user for indirect-object-reference (IDOR) tests: proves
# User B cannot reach User A's data. Created as an Author (a normal, non-admin
# authenticated user).
USER_B = "UserB"
SCOPED = "ScopedAuthor"  # an Author with a restrictive allowedConfigVersions
# Random per-run password (one test user is an Admin — a static password in a
# public repo would be a standing credential if teardown is ever skipped/killed).
# The "Aa1!" prefix guarantees the Cognito policy classes regardless of what
# token_urlsafe happens to produce.
TEST_PW = "Aa1!" + secrets.token_urlsafe(24)
# .invalid TLD (RFC 2606) can never be a real address.
SCOPE_VERSION = "rbac-test-scope-v1"  # a version the scoped user is limited to
OUT_OF_SCOPE_VERSION = "default"  # exists, but outside the scoped user's set

ANY = "ANY"
IAM = "IAM_ONLY"
# Resolved to the stack's group names by _resolve_any_group before the matrix
# runs, so nothing downstream has to handle a third sentinel.
ANY_GROUP = "ANY_GROUP"
EXPECTATIONS_PATH = Path(__file__).resolve().parent / "api_rbac_expectations.yaml"


def test_email(role):
    return f"test-rbac-{role.lower()}@example.invalid"


# ----------------------------------------------------------------------------
# Expectations loading
# ----------------------------------------------------------------------------
def load_expectations():
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML required (pip install pyyaml).", file=sys.stderr)
        sys.exit(2)
    with EXPECTATIONS_PATH.open() as fh:
        spec = yaml.safe_load(fh)
    ops = spec["operations"]
    _resolve_any_group(ops)
    return ops, (spec.get("known_gaps") or {})


def _resolve_any_group(ops):
    """Expand ``groups: ANY_GROUP`` into the group names the stack creates.

    Done here, once, so the rest of the harness only ever sees a plain group
    list or one of the two sentinels it already understands. Leaving the string
    in place would be actively wrong rather than merely unhandled: the matrix
    builds ``set(groups)`` for a non-sentinel policy, and ``set("ANY_GROUP")``
    is a set of nine CHARACTERS, which no role matches — every role would be
    expected to be denied, and every role being correctly allowed would be
    reported as a failure.

    The vocabulary is read by ``generate_api_rbac_manifest.cognito_group_names``,
    the same function the generator resolves the manifest with and the same one
    ``scan_api_rbac.app_group_names`` delegates to — so the harness asserts the
    group set the dispatcher actually enforces, and there is no fourth regex of
    this fact to drift. An unresolvable sentinel is fatal: running the matrix
    against a policy we could not read would report passes that mean nothing.
    """
    if not any(o.get("groups") == ANY_GROUP for o in ops.values()):
        return
    repo = EXPECTATIONS_PATH.resolve().parent.parent
    template = repo / "template.yaml"
    # Loaded by path, not imported: this script is run directly, so `scripts/sdlc`
    # is not on sys.path.
    spec = importlib.util.spec_from_file_location(
        "_gen_api_rbac_manifest_for_harness",
        repo / "scripts" / "sdlc" / "generate_api_rbac_manifest.py",
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        print(f"ERROR: could not load the manifest generator from {repo}", file=sys.stderr)
        sys.exit(2)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    names = sorted(gen.cognito_group_names(template.read_text()))
    if not names:
        print(
            f"ERROR: no AWS::Cognito::UserPoolGroup found in {template}, so "
            f"'{ANY_GROUP}' cannot be resolved.",
            file=sys.stderr,
        )
        sys.exit(2)
    for o in ops.values():
        if o.get("groups") == ANY_GROUP:
            o["groups"] = names
    print(f"  {ANY_GROUP} resolves to {names}")


def setup_users(ctx):
    for role in ROLES:
        create_cognito_user(ctx, test_email(role), role, TEST_PW)
    # Scoped user: an Author in Cognito, restricted via UsersTable.
    create_cognito_user(ctx, test_email(SCOPED), "Author", TEST_PW)
    # Second independent user (Author) for IDOR tests — a distinct Cognito sub so
    # "User B cannot reach User A's data" is a real cross-identity check.
    create_cognito_user(ctx, test_email(USER_B), "Author", TEST_PW)
    _seed_scoped_user(ctx)
    enable_admin_auth(ctx)
    print(
        f"Created test users: {', '.join(test_email(r) for r in ROLES)}, "
        f"{test_email(SCOPED)} (scoped to [{SCOPE_VERSION}])"
    )


def _cognito_sub(ctx, email):
    """The immutable Cognito ``sub`` of a user we just created, or "".

    ``create_cognito_user`` discards ``admin-create-user``'s response (it tolerates
    a pre-existing user, whose response would carry nothing), so the sub is read
    back with ``admin-get-user`` — the authoritative value either way.

    Returns "" rather than raising on any failure. The sub is used only to seed an
    *additional* route to the same row; without it the seeding degrades to the
    email-only shape, which is what every deployment predating pointers has, and
    the scope suite still runs.
    """
    try:
        resp = aws(
            "cognito-idp",
            "admin-get-user",
            "--user-pool-id",
            ctx["user_pool"],
            "--username",
            email,
            region=ctx["region"],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not read the Cognito sub for {email}: {exc}")
        return ""
    for attr in (resp or {}).get("UserAttributes") or []:
        if attr.get("Name") == "sub":
            return str(attr.get("Value") or "").strip()
    return ""


def _seed_scoped_user(ctx):
    """Seed the scoped user's UsersTable row the way user_management writes one.

    Two items, because a verified caller reaches their row through one of two
    disjoint key spaces on this table (see idp_common.config_scope):

      * the row itself at PK/SK = ``USER#<userId>``, found by the ``email`` claim
        through the EmailIndex GSI;
      * a pointer at PK/SK = ``SUB#<sub>`` carrying that ``userId``, found by the
        immutable Cognito ``sub``.

    The **pointer is tried first**, and the Cognito user created above has a real
    sub, so seeding only the row would have every live scope check fall through to
    the compatibility leg and leave the leg that actually runs untested.

    ⚠️ The pointer must carry no ``email`` attribute: a GSI indexes only items
    holding its hash key, so an emailless pointer is absent from EmailIndex and
    cannot be returned by the ``Limit=1`` email query in place of the row. A pointer
    holds no ``allowedConfigVersions``, so that substitution would lift the scope.
    """
    if not ctx.get("users_table"):
        print("WARN: UsersTable not found — scope suite will be skipped.")
        return
    uid = f"rbac-test-{uuid.uuid4()}"
    email = test_email(SCOPED)
    sub = _cognito_sub(ctx, email)
    item = {
        "PK": {"S": f"USER#{uid}"},
        "SK": {"S": f"USER#{uid}"},
        "userId": {"S": uid},
        "email": {"S": email},
        "persona": {"S": "Author"},
        "status": {"S": "active"},
        "allowedConfigVersions": {"L": [{"S": SCOPE_VERSION}]},
    }
    if sub:
        item["cognitoSub"] = {"S": sub}
    keys = [{"PK": {"S": f"USER#{uid}"}, "SK": {"S": f"USER#{uid}"}}]
    items = [item]
    if sub:
        items.append(
            {
                "PK": {"S": f"SUB#{sub}"},
                "SK": {"S": f"SUB#{sub}"},
                "userId": {"S": uid},
                "cognitoSub": {"S": sub},
            }
        )
        keys.append({"PK": {"S": f"SUB#{sub}"}, "SK": {"S": f"SUB#{sub}"}})
    # Every key is recorded BEFORE the write, so a put that fails halfway still
    # leaves teardown something to delete. This is the live stack's own table in a
    # shared account; an item left behind is a real defect, not untidiness.
    ctx["_scoped_user_keys"] = keys
    for entry in items:
        aws(
            "dynamodb",
            "put-item",
            "--table-name",
            ctx["users_table"],
            "--item",
            json.dumps(entry),
            region=ctx["region"],
        )
    print(
        f"  scoped row USER#{uid}"
        + (f" + pointer SUB#{sub}" if sub else " (no Cognito sub — email leg only)")
    )


def _recover_scoped_user_keys(ctx):
    """Re-derive the scoped user's item keys from the table itself.

    ``--teardown-only`` (and a rerun after a killed process) gets a fresh ctx, so
    nothing recorded what ``_seed_scoped_user`` wrote. The row is still findable by
    the one thing that does not change between runs — the scoped user's email — and
    it records the ``userId`` and the ``cognitoSub`` the two keys are built from.

    Best effort: returns [] on any failure, since this runs in a teardown path.
    """
    try:
        resp = aws(
            "dynamodb",
            "query",
            "--table-name",
            ctx["users_table"],
            "--index-name",
            "EmailIndex",
            "--key-condition-expression",
            "email = :e",
            "--expression-attribute-values",
            json.dumps({":e": {"S": test_email(SCOPED)}}),
            region=ctx["region"],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not look up the scoped row to delete it: {exc}")
        return []
    keys = []
    for row in (resp or {}).get("Items") or []:
        uid = (row.get("userId") or {}).get("S")
        if uid:
            keys.append({"PK": {"S": f"USER#{uid}"}, "SK": {"S": f"USER#{uid}"}})
        sub = (row.get("cognitoSub") or {}).get("S")
        if sub:
            keys.append({"PK": {"S": f"SUB#{sub}"}, "SK": {"S": f"SUB#{sub}"}})
    return keys


def teardown_users(ctx):
    for role in [*ROLES, SCOPED, USER_B]:
        delete_cognito_user(ctx, test_email(role))
    # Both items the seeding wrote: the USER# row and its SUB# pointer. Deleting
    # only the row would leave a pointer in the live stack's UsersTable naming a row
    # that no longer exists — harmless to the lookup, which falls through to the
    # email leg, but still our litter in a shared account.
    keys = ctx.get("_scoped_user_keys") or []
    if ctx.get("users_table"):
        if not keys:
            keys = _recover_scoped_user_keys(ctx)
        for key in keys:
            subprocess.run(
                [
                    AWS_BIN,
                    "dynamodb",
                    "delete-item",
                    "--table-name",
                    ctx["users_table"],
                    "--key",
                    json.dumps(key),
                    "--region",
                    ctx["region"],
                ],
                capture_output=True,
                text=True,
            )
    print(f"Deleted test users and {len(keys)} scoped UsersTable item(s).")


def get_token(ctx, role):
    return get_id_token(ctx, test_email(role), TEST_PW)


# ----------------------------------------------------------------------------
# HTTP call
# ----------------------------------------------------------------------------
# Marker put in the errorType slot when the response carried no readable body.
# The whole point of this harness is to establish what the API DID; a response we
# could not read establishes nothing, and has to be distinguishable from one that
# said "denied". It travels in `errorType` rather than as a fifth tuple element
# because ~25 call sites unpack the 4-tuple, and `_denied()` already answers False
# for it — so every deny-expected cell fails closed on it without further change.
UNREADABLE_BODY = "__unreadable_body__"

# Nothing here should take anywhere near this long; the dispatcher gives a resolver
# 20s and API Gateway abandons the integration at 29s. Without an explicit timeout
# urlopen inherits the default socket timeout, which is None — so a hung endpoint
# hangs the harness forever instead of being reported.
REQUEST_TIMEOUT_SECONDS = 45


def call(api_base, field, args, token):
    """POST /op/<field>; return (http_status, errorType, in_band_error_type,
    request_id). in_band_error_type captures the {success:false,error:{type}}
    payload that scope denials use (HTTP 200 body).

    Two sentinel values mark a call that established nothing, so that
    :func:`inconclusive` can tell them apart from a refusal:

    * ``status == 0`` — the request never completed (timeout, connection reset,
      DNS, TLS). Reported rather than raised, so one unreachable operation does not
      abort the matrix.
    * ``errorType == UNREADABLE_BODY`` — the response had no body, or a body that is
      not JSON. Without the marker, an unparseable body leaves ``errorType`` and the
      in-band type both ``None``, which reads identically to a clean success.
    """
    body = json.dumps({"arguments": args}).encode()
    req = urllib.request.Request(
        f"{api_base}/op/{field}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", token)
    request_id = ""
    try:
        with urllib.request.urlopen(  # nosec B310 - RBAC harness posting to the stack's own API base URL
            req, timeout=REQUEST_TIMEOUT_SECONDS
        ) as r:
            status, raw = r.status, r.read()
            request_id = r.headers.get("x-amzn-RequestId", "") or r.headers.get(
                "apigw-requestid", ""
            )
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
        request_id = e.headers.get("x-amzn-RequestId", "") if e.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Reported, not raised: one unreachable operation must not abort the whole
        # matrix, and it must not be mistaken for a refusal either.
        return 0, None, None, f"<request error: {e}>"

    if not (raw or b"").strip():
        return status, UNREADABLE_BODY, None, request_id

    et = None
    in_band = None
    try:
        p = json.loads(raw)
    except (ValueError, TypeError):
        return status, UNREADABLE_BODY, None, request_id
    if isinstance(p, dict):
        errors = p.get("errors")
        if isinstance(errors, list) and errors:
            # `errors` is not always the dispatcher's GraphQL-shaped envelope. A
            # resolver may return 200 with its own per-item failure list of plain
            # STRINGS — abortWorkflow does, one entry per object key it could not
            # abort. Reading `.get` off that raises AttributeError and takes the
            # whole run down with no report, which is what happened when this
            # access moved out from under a bare `except Exception`. Narrowing
            # that handler was right; the element type has to be checked here.
            first = errors[0]
            if isinstance(first, dict):
                et = first.get("errorType")
        err = p.get("error")
        if isinstance(err, dict):
            in_band = err.get("type")
    return status, et, in_band, request_id


def call_body(api_base, field, args, token):
    """Like call(), but returns (status, response_body_text). Used by the
    security suites (IDOR / deleted-resource) that must assert on RESPONSE
    CONTENT — e.g. 'User A's marker must NOT appear in User B's response' —
    because a status code alone can't distinguish non-disclosure from disclosure
    (the API returns 200 for many not-found/denied cases)."""
    body = json.dumps({"arguments": args}).encode()
    req = urllib.request.Request(
        f"{api_base}/op/{field}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req) as r:  # nosec B310 - RBAC harness posting to the stack's own API base URL
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"<request error: {e}>"


def seed_agent_job(ctx, job_id, marker):
    """Seed an agent job OWNED BY the Admin test user directly in the AgentTable,
    for the IDOR suite. The getAgentJobStatus resolver keys by
    PK='agent#<caller-identity>', SK=jobId, where caller-identity is the token's
    username (email for admin-created users). We write the row under the Admin
    user's email so ONLY the Admin token can read it back — a foreign caller's
    identity-derived PK cannot.

    Returns the owner user id (email) on success, or None if the agent table is
    absent / the write fails (-> suite SKIPs).
    """
    table = _agent_table_name(ctx)
    if not table:
        return None
    owner = test_email("Admin")
    item = {
        "PK": {"S": f"agent#{owner}"},
        "SK": {"S": job_id},
        "jobId": {"S": job_id},
        "userId": {"S": owner},
        "status": {"S": "COMPLETED"},
        # The marker lives in a field the resolver returns, so a leak is visible.
        "result": {"S": marker},
    }
    try:
        aws(
            "dynamodb",
            "put-item",
            "--table-name",
            table,
            "--item",
            json.dumps(item),
            region=ctx["region"],
        )
    except Exception as e:  # noqa: BLE001
        print(f"  (seed_agent_job failed: {e})")
        return None
    ctx.setdefault("_seeded_agent_jobs", []).append((owner, job_id))
    return owner


def _agent_table_name(ctx):
    """Resolve the AgentTable physical name from the stack (cached on ctx)."""
    if "_agent_table" in ctx:
        return ctx["_agent_table"]
    try:
        name = aws(
            "cloudformation",
            "list-stack-resources",
            "--stack-name",
            ctx["stack"],
            "--query",
            "StackResourceSummaries[?LogicalResourceId=='AgentTable']"
            ".PhysicalResourceId",
            "--output",
            "text",
            region=ctx["region"],
        )
    except Exception:
        name = ""
    ctx["_agent_table"] = name or ""
    return ctx["_agent_table"]


def cleanup_seeded_agent_jobs(ctx):
    """Delete any agent-job rows seeded by the IDOR suite (best-effort)."""
    table = ctx.get("_agent_table")
    for owner, job_id in ctx.get("_seeded_agent_jobs", []):
        if not table:
            continue
        try:
            aws(
                "dynamodb",
                "delete-item",
                "--table-name",
                table,
                "--key",
                json.dumps({"PK": {"S": f"agent#{owner}"}, "SK": {"S": job_id}}),
                region=ctx["region"],
            )
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------
# Outcome classification
# ----------------------------------------------------------------------------
def _denied(status, et, in_band=None):
    """A request is 'denied' if the gateway rejected it (401/403) OR a resolver
    returned an Unauthorized errorType — either as a GraphQL error
    (``errors[0].errorType``) or as an in-band ``{success:false,
    error:{type:'Unauthorized'}}`` 200 body (the shape scope/group denials in
    the configuration & sync resolvers use)."""
    return (
        status == 401
        or status == 403
        or et == "Unauthorized"
        or in_band == "Unauthorized"
    )


def inconclusive(status, et=None, body=None):
    """Why this call established nothing — or ``None`` if it established something.

    A **refusal** and an **inconclusive result** are different outcomes, and
    conflating them is the worst possible default for an authorization test: the
    entire point is to establish that a caller was refused, and a call that did not
    complete establishes nothing while reporting success.

    It is not a hypothetical risk. An arm that asks only "was this NOT denied?"
    is satisfied by any status that is not 401/403 — and the published
    ``security/test-results/0.6.9/rbac-dynamic.md`` snapshot contains 43 cells
    reading ``500 ✅`` under a header claiming ``Gate: PASS``. Read that page with
    this in mind: those 43 cells establish nothing. A deployment in which every
    operation 500s or hangs must not produce a green run.

    The conditions, and why each one proves nothing:

    * ``status == 0`` — the request never completed. No server behaviour observed.
    * an unreadable body — the response cannot be parsed, so neither ``errorType``
      nor an in-band denial can be read out of it. Absence of a denial marker in a
      body we could not read is not absence of a denial.
    * ``5xx`` — the server failed. Whether the resolver refused this caller before
      or after doing the work is exactly what is not known.
    """
    if status == 0:
        return "request did not complete (timeout / connection error)"
    if et == UNREADABLE_BODY:
        return "response body was empty or not JSON"
    if body is not None and not str(body).strip():
        return "response body was empty"
    if isinstance(status, int) and status >= 500:
        return f"server error {status}"
    return None


# A 5xx is inconclusive, and today it is also COMMON: roughly 50 resolver-level
# validation refusals raise a bare `Exception`, which the dispatcher can only map to
# 500 `InternalError`, and the deliberately-bogus arguments this harness sends to
# prove "auth ran before the argument was used" land on many of them. Those cells
# are registered against this gap so they surface as WARN — visible, counted
# separately, never a pass — rather than red-lining the gate for a backlog this
# change does not fix. Everything else inconclusive (a timeout, a dead connection,
# an unreadable body) is a HARD failure, because none of those is a known condition
# of this deployment.
GAP_INCONCLUSIVE_5XX = "GAP-SEC-INCONCLUSIVE-5XX"


def _inconclusive_gap(status):
    """The gap id an inconclusive outcome is registered against, if any."""
    if isinstance(status, int) and status >= 500:
        return GAP_INCONCLUSIVE_5XX
    return None


class Verdict(NamedTuple):
    """What one matrix cell established.

    Named rather than a bare tuple because the members are not
    interchangeable — ``passed`` and ``outcome`` answer different questions, and
    ``gap`` is optional — so positional unpacking is how the next field to be added
    would silently land in the wrong variable.
    """

    passed: bool
    detail: str
    outcome: str
    gap: Optional[str] = None


def classify(role, allowed, status, et, in_band) -> Verdict:
    """Judge one cell of the group matrix. `allowed` is ANY | IAM | set.

    ``outcome`` is ``PASS``, ``FAIL`` or ``ERROR``. ``ERROR`` means the check could
    not be run — see :func:`inconclusive` — and is never a pass in any cell,
    whichever way the cell was expected to go.
    """
    why = inconclusive(status, et)
    if why:
        return Verdict(
            False, f"INCONCLUSIVE: {why}", "ERROR", _inconclusive_gap(status)
        )

    if allowed == IAM:
        ok = _denied(status, et, in_band)
        detail = f"{status}" if ok else f"LEAK({status}/{et})"
        return Verdict(ok, detail, "PASS" if ok else "FAIL")
    if allowed == ANY:
        if _denied(status, et, in_band):
            return Verdict(
                False, f"unexpected denial ({status}/{et}/{in_band})", "FAIL"
            )
        return Verdict(True, f"{status}", "PASS")
    # group-restricted
    if role in allowed:
        # Any denial shape fails here — including a bare 401/403 with no
        # errorType (e.g. a gateway/WAF rejection), which would otherwise
        # silently pass as "auth worked".
        if _denied(status, et, in_band):
            return Verdict(
                False, f"DENIED but allowed ({status}/{et}/{in_band})", "FAIL"
            )
        return Verdict(True, f"{status}", "PASS")
    ok = _denied(status, et, in_band)
    detail = f"{status}" if ok else f"LEAK({status}/{et}/{in_band})"
    return Verdict(ok, detail, "PASS" if ok else "FAIL")


# ----------------------------------------------------------------------------
# Matrices
# ----------------------------------------------------------------------------
def resolve_execution_arn(ctx, tokens):
    """Find a real workflow execution ARN in this stack, or None.

    getStepFunctionExecution now requires the caller-supplied ARN to name this
    deployment's state machine, so the placeholder ARN in the expectations file is
    refused for every role. That refusal is correct behaviour, but it is
    indistinguishable from an RBAC denial, and would read as "unexpected denial"
    for an ANY-auth op. Driving the op with a real ARN keeps the matrix cell
    meaningful: an authenticated caller of any role should be able to read an
    execution of this stack.

    Two hops, because `listDocuments` is served from a GSI whose projection does
    NOT include WorkflowExecutionArn — it returns `{"Documents": [...]}` with
    summary fields only, so the ARN has to come from `getDocument`.
    """
    st, body = call_body(ctx["api_base"], "listDocuments", {}, tokens["Admin"])
    if st != 200:
        return None
    try:
        documents = (json.loads(body) or {}).get("Documents") or []
    except Exception:
        return None
    for summary in documents:
        if not isinstance(summary, dict):
            continue
        object_key = summary.get("ObjectKey")
        if not object_key:
            continue
        st, doc_body = call_body(
            ctx["api_base"], "getDocument", {"ObjectKey": object_key}, tokens["Admin"]
        )
        if st != 200:
            continue
        try:
            doc = json.loads(doc_body) or {}
        except Exception:
            continue
        arn = doc.get("WorkflowExecutionArn")
        if isinstance(arn, str) and ":execution:" in arn:
            return arn
    return None


def _input_bucket(ctx):
    """This deployment's input bucket, or "" if it cannot be resolved.

    Never raises: a bucket this cannot find becomes a SKIP for the two operations
    that need it, not an aborted run.
    """
    if "input_bucket" in ctx:
        return ctx["input_bucket"]
    name = ""
    try:
        name = aws(
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            ctx["stack"],
            "--query",
            "Stacks[0].Outputs[?OutputKey=='S3InputBucketName'].OutputValue",
            "--output",
            "text",
            region=ctx["region"],
        )
        if not name or name == "None":
            name = aws(
                "cloudformation",
                "list-stack-resources",
                "--stack-name",
                ctx["stack"],
                "--query",
                "StackResourceSummaries[?LogicalResourceId=='InputBucket']"
                ".PhysicalResourceId",
                "--output",
                "text",
                region=ctx["region"],
            )
    except RuntimeError as e:
        print(f"  could not resolve the input bucket: {e}")
        name = ""
    ctx["input_bucket"] = "" if (not name or name == "None") else name
    return ctx["input_bucket"]


def apply_dynamic_args(ops, ctx, tokens):
    """Replace placeholder args that a real object reference is needed for.

    Returns the set of fields to skip because no such object exists in this
    stack (an empty deployment), so the run reports SKIP rather than a
    misleading failure.
    """
    skip = set()

    # getFileContents / getFilePresignedUrl: the bucket allow-list is a DIFFERENT
    # control from the group floor, and the matrix can only exercise one at a time.
    # With a bucket that is not in the allow-list, the resolver refuses every caller
    # with a 403, so the allowed-role cells read as "DENIED but allowed" — the
    # matrix would report the allow-list working as a group-check failure. Point the
    # matrix at an IN-allow-list bucket with a key that does not exist, so an
    # allowed role gets a clean 400 "File not found" (auth passed, argument was
    # bogus — the same shape every mutation op in this file uses) and a disallowed
    # role still gets the dispatcher's 403.
    #
    # The bucket name is RESOLVED FROM THE STACK rather than written in the YAML,
    # because it is per-deployment. The allow-list refusal itself is asserted
    # offline, where the allow-list contents are known:
    # nested/api-resolvers/src/lambda/get_file_contents_resolver/test_index.py.
    bucket = _input_bucket(ctx)
    for field in ("getFileContents", "getFilePresignedUrl"):
        if field not in ops:
            continue
        if bucket:
            ops[field]["args"] = {
                "s3Uri": f"s3://{bucket}/__sectest-nonexistent-key__.json"
            }
            print(f"  {field}: using this stack's input bucket {bucket}")
        else:
            skip.add(field)
            print(f"  {field}: could not resolve the stack's input bucket")

    if "getStepFunctionExecution" in ops:
        arn = resolve_execution_arn(ctx, tokens)
        if arn:
            ops["getStepFunctionExecution"]["args"] = {"executionArn": arn}
            ctx["live_execution_arn"] = arn
            print(f"  getStepFunctionExecution: using live execution {arn}")
        else:
            skip.add("getStepFunctionExecution")
            print(
                "  getStepFunctionExecution: no processed document found — "
                "cannot supply a live execution ARN"
            )
    return skip


def run_group_matrix(ops, ctx, tokens, results, skip_fields=frozenset()):
    print("\n=== GROUP MATRIX (unauth + 4 roles) ===")
    for field, o in ops.items():
        groups = o["groups"]
        allowed = (
            ANY if groups == "ANY" else IAM if groups == "IAM_ONLY" else set(groups)
        )
        if field in skip_fields:
            _record(
                results,
                field,
                "*",
                "SKIP",
                False,
                "no live object reference available to drive this op",
                outcome="SKIP",
            )
            print(f"  {field:28s} SKIP (no live object reference)")
            continue
        # Skip conditional ops when the feature is off (404 for everyone).
        # NOTE: the probe below EXECUTES the op as Admin, so a `conditional`
        # op must never also be side-effectful/skip_allowed — probe with a
        # disallowed role instead if that combination ever appears.
        cond = o.get("conditional")
        skip_allowed = o.get("skip_allowed", False)
        if cond and skip_allowed:
            raise RuntimeError(
                f"{field}: 'conditional' + 'skip_allowed' is unsupported — "
                "the feature probe would execute the op as Admin."
            )
        if cond == "circuit_breaker" and not ctx.get("circuit_breaker"):
            st, *_ = call(ctx["api_base"], field, o["args"], tokens["Admin"])
            if st == 404:
                _record(
                    results,
                    field,
                    "*",
                    "SKIP",
                    False,
                    "circuit breaker disabled (404 for all)",
                    outcome="SKIP",
                )
                print(f"  {field:28s} SKIP (circuit breaker disabled)")
                continue
        if cond == "feature_platform":
            st, *_ = call(ctx["api_base"], field, o["args"], tokens["Admin"])
            if st == 404:
                _record(
                    results,
                    field,
                    "*",
                    "SKIP",
                    False,
                    "feature platform disabled (404 for all)",
                    outcome="SKIP",
                )
                print(f"  {field:28s} SKIP (feature platform disabled)")
                continue

        cells = [field]
        # unauthenticated
        st, et, ib, rid = call(ctx["api_base"], field, o["args"], None)
        # Already strict (401 exactly, not merely "denied"), but a call that never
        # completed is not a failed assertion about the authorizer — it is no
        # observation at all, and has to say so.
        why = inconclusive(st, et)
        ua_ok = (not why) and st == 401
        cells.append(f"UN={'401' if ua_ok else st}")
        _record(
            results,
            field,
            "unauth",
            st,
            ua_ok,
            f"INCONCLUSIVE: {why}" if why else "expect 401",
            et,
            ib,
            rid,
            gap=_inconclusive_gap(st) if why else None,
            outcome="ERROR" if why else None,
        )
        # roles
        for role in ROLES:
            role_allowed = allowed not in (ANY, IAM) and role in allowed
            if skip_allowed and role_allowed:
                cells.append(f"{role[:2]}=skip")
                _record(
                    results,
                    field,
                    role,
                    "SKIP",
                    False,
                    "allowed-role call skipped (skip_allowed)",
                    outcome="SKIP",
                )
                continue
            st, et, ib, rid = call(ctx["api_base"], field, o["args"], tokens[role])
            verdict = classify(role, allowed, st, et, ib)
            cells.append(f"{role[:2]}={verdict.detail}")
            _record(
                results,
                field,
                role,
                st,
                verdict.passed,
                verdict.detail,
                et,
                ib,
                rid,
                # The operation's own known_gap wins if it has one; otherwise an
                # inconclusive cell carries the gap classify assigned it.
                gap=o.get("known_gap") or verdict.gap,
                outcome=verdict.outcome,
            )
        print("  " + " ".join(f"{c:16s}" for c in cells))


def run_scope_suite(ctx, tokens, results):
    """A config-version-scoped Author must be denied an out-of-scope version;
    an Admin (unrestricted) must not be. Denial is an in-band
    {success:false,error:{type:'Unauthorized'}} 200 body."""
    print("\n=== CONFIG-VERSION SCOPE ===")
    if "scoped" not in tokens:
        print("  SKIP (scoped user unavailable)")
        _record(
            results,
            "getConfigVersion",
            "scoped",
            "SKIP",
            False,
            "scoped user unavailable",
            outcome="SKIP",
        )
        return

    # scoped Author asks for an out-of-scope version -> must be denied
    st, et, ib, rid = call(
        ctx["api_base"],
        "getConfigVersion",
        {"versionName": OUT_OF_SCOPE_VERSION},
        tokens["scoped"],
    )
    # `inconclusive` first: a 5xx, an empty body or a dead connection is not a
    # denial, and reading "no Unauthorized marker" out of a body we could not read
    # is not evidence either way.
    why = inconclusive(st, et)
    denied = (not why) and (
        et == "Unauthorized" or ib == "Unauthorized" or st == 403
    )
    _record(
        results,
        "getConfigVersion",
        "scoped(out-of-scope)",
        st,
        denied,
        f"INCONCLUSIVE: {why}" if why else f"expect denial; got {st}/{et}/{ib}",
        et,
        ib,
        rid,
        gap=_inconclusive_gap(st) if why else None,
        outcome="ERROR" if why else None,
    )
    print(
        f"  scoped Author getConfigVersion('{OUT_OF_SCOPE_VERSION}') -> "
        f"{st}/{et or ib} ({'OK denied' if denied else 'LEAK'})"
    )

    # Admin asks for the same version -> must NOT be denied
    st, et, ib, rid = call(
        ctx["api_base"],
        "getConfigVersion",
        {"versionName": OUT_OF_SCOPE_VERSION},
        tokens["Admin"],
    )
    # This arm asserted only "no Unauthorized marker", so a 500, an empty body or a
    # malformed one all passed it — the weakest shape in the file, because it is the
    # arm that is supposed to prove the control does not over-deny.
    why = inconclusive(st, et)
    ok = (not why) and not (
        et == "Unauthorized" or ib == "Unauthorized" or st == 403
    )
    _record(
        results,
        "getConfigVersion",
        "admin(unrestricted)",
        st,
        ok,
        f"INCONCLUSIVE: {why}" if why else f"expect allowed; got {st}/{et}/{ib}",
        et,
        ib,
        rid,
        gap=_inconclusive_gap(st) if why else None,
        outcome="ERROR" if why else None,
    )
    print(
        f"  Admin getConfigVersion('{OUT_OF_SCOPE_VERSION}') -> "
        f"{st} ({'OK allowed' if ok else 'WRONGLY DENIED'})"
    )

    # scoped list should not surface out-of-scope versions
    st, et, ib, rid = call(
        ctx["api_base"],
        "getConfigVersions",
        {},
        tokens["scoped"],
    )
    # The loosest arm of all: `et != "Unauthorized"` was satisfied by ANY status
    # with any body, including a 500 or one that failed to parse.
    why = inconclusive(st, et)
    ok = (not why) and et != "Unauthorized"
    _record(
        results,
        "getConfigVersions",
        "scoped(filtered)",
        st,
        ok,
        f"INCONCLUSIVE: {why}"
        if why
        else f"list returns (filtered) for scoped user; got {st}/{et}",
        et,
        ib,
        rid,
        gap=_inconclusive_gap(st) if why else None,
        outcome="ERROR" if why else None,
    )
    print(
        f"  scoped Author getConfigVersions -> {st} "
        f"({'OK' if ok else 'unexpectedly denied'})"
    )


def run_token_negatives(ctx, tokens, results):
    """Malformed / tampered / missing tokens must all be rejected at the
    gateway (401)."""
    print("\n=== TOKEN NEGATIVES (expect 401) ===")
    good = tokens["Admin"]
    # tamper: flip a real byte of the decoded signature. NOTE: do NOT flip the
    # last base64url char of the signature segment — an RS256 signature is 256
    # bytes, whose base64url encoding ends on a char carrying only 2 significant
    # bits (the low bits are discarded padding). ~25% of tokens end in 'A', and
    # 'A'<->'B' differ only in a padding bit, so that "tamper" decodes to the
    # SAME signature bytes and is legitimately accepted (200) — a false failure.
    parts = good.split(".")
    tampered = good
    if len(parts) == 3:
        sig = parts[2]
        raw = bytearray(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        raw[0] ^= 0x01  # flip a byte away from the padding tail
        new_sig = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
        tampered = f"{parts[0]}.{parts[1]}.{new_sig}"
    cases = {
        "no-token": None,
        "garbage": "not-a-jwt",
        "tampered-signature": tampered,
        "empty-bearer": "Bearer ",
    }
    for name, tok in cases.items():
        st, et, ib, rid = call(ctx["api_base"], "listDocuments", {}, tok)
        # The API Gateway Cognito authorizer rejects an unusable token with 401
        # (unauthorized) or 403 (forbidden) depending on how the failure is
        # surfaced (missing/blank credentials vs a token that fails validation).
        # Both mean "rejected at the gateway" — the only failure we care about
        # is the request being ALLOWED through (2xx) or reaching a resolver.
        # Already strict about the status, but a request that never reached the
        # gateway is not evidence that the gateway rejected the token.
        why = inconclusive(st, et)
        ok = (not why) and st in (401, 403)
        _record(
            results,
            "listDocuments",
            f"token:{name}",
            st,
            ok,
            f"INCONCLUSIVE: {why}" if why else f"expect 401/403; got {st}",
            et,
            ib,
            rid,
            gap=_inconclusive_gap(st) if why else None,
            outcome="ERROR" if why else None,
        )
        print(f"  {name:22s} -> {st} ({'OK' if ok else 'UNEXPECTED'})")


# ----------------------------------------------------------------------------
# Results / report
# ----------------------------------------------------------------------------
# The five outcomes a check can have. `passed` alone could not express the
# difference between "ran, and the call was refused as expected" and "could not be
# run", which is the distinction this harness exists to make.
#
#   PASS   the check ran and the API behaved as required
#   FAIL   the check ran and the API did not            -> non-zero exit
#   ERROR  the check could not be run (see `inconclusive`) -> non-zero exit,
#          unless registered against a known gap
#   SKIP   a precondition for the check is absent       -> exit 0, NOT a pass
#   WARN   a FAIL or ERROR registered against a known gap -> exit 0
_BLOCKING_OUTCOMES = ("FAIL", "ERROR")


def _derive_outcome(passed, gap, explicit):
    if explicit:
        return explicit
    if passed:
        return "PASS"
    return "WARN" if gap else "FAIL"


def _record(
    results,
    op,
    principal,
    status,
    passed,
    detail,
    et=None,
    in_band=None,
    request_id="",
    gap=None,
    outcome=None,
):
    resolved = _derive_outcome(passed, gap, outcome)
    # A gap downgrades a FAIL/ERROR to a WARN; it never turns one into a pass.
    if gap and resolved in _BLOCKING_OUTCOMES:
        resolved = "WARN"
    results.append(
        {
            "op": op,
            "principal": principal,
            "http_status": status,
            "error_type": et,
            "in_band_error": in_band,
            # Kept for report/consumer compatibility, but `outcome` is the field
            # that decides anything. A SKIP is NOT passed: counting an absent
            # precondition as a satisfied assertion is the same conflation as
            # counting a timeout as a refusal.
            "passed": resolved == "PASS",
            "outcome": resolved,
            # True when the check established nothing, INDEPENDENT of whether a
            # known gap downgraded it to a WARN. Registering a gap is a statement
            # about whether to block the gate, not a claim that the check ran.
            "inconclusive": _derive_outcome(passed, gap, outcome) == "ERROR",
            "detail": detail,
            "request_id": request_id,
            "known_gap": gap,
        }
    )


def _partition(results):
    """Split results by outcome. One definition, used by the report and the exit
    code, so the summary a reader sees and the number the process returns cannot
    disagree."""
    return {
        "pass": [r for r in results if r["outcome"] == "PASS"],
        "fail": [r for r in results if r["outcome"] == "FAIL"],
        "error": [r for r in results if r["outcome"] == "ERROR"],
        "skip": [r for r in results if r["outcome"] == "SKIP"],
        "warn": [r for r in results if r["outcome"] == "WARN"],
        "inconclusive": [r for r in results if r.get("inconclusive")],
    }


def hard_failures(results):
    """The results that must make the process exit non-zero.

    ERROR is in here with FAIL, deliberately. A check that could not be run has not
    established the property it exists to establish, and a run full of them is not a
    green run — which is exactly how a regression that made every operation time out
    would have reported success.
    """
    return [r for r in results if r["outcome"] in _BLOCKING_OUTCOMES]


def write_report(report_dir, ctx, results, known_gaps, stamp, account):
    d = Path(report_dir) / f"{ctx['stack']}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    parts = _partition(results)
    # a failure that maps to a known gap is a WARN, not a hard failure
    hard_fails = hard_failures(results)
    gap_fails = parts["warn"]

    (d / "meta.json").write_text(
        json.dumps(
            {
                "stack": ctx["stack"],
                "region": ctx["region"],
                "account": account,
                "api_base": ctx["api_base"],
                "timestamp": stamp,
                "git_sha": _git_sha(),
                "circuit_breaker_enabled": ctx.get("circuit_breaker", False),
                "totals": {
                    "checks": len(results),
                    "passed": len(parts["pass"]),
                    "failed": len(parts["fail"]),
                    # Could not be run. Reported separately from `failed` because
                    # "the API let this through" and "we never found out" call for
                    # different work.
                    "errored": len(parts["error"]),
                    "skipped": len(parts["skip"]),
                    "hard_fail": len(hard_fails),
                    "gap_warn": len(gap_fails),
                },
            },
            indent=2,
        )
    )
    (d / "report.json").write_text(json.dumps({"results": results}, indent=2))
    (d / "report.md").write_text(
        _render_md(ctx, results, hard_fails, gap_fails, known_gaps, stamp, account)
    )
    print(f"\nReport written to {d}/")
    return len(hard_fails)


def _git_sha():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


# Report glyphs keyed by outcome name. Bandit's hardcoded-password heuristic (B105)
# fires on the "PASS" key, which is a verdict label and not a credential — the same
# false positive the "pass" counter key in scripts/security/curate_results.py carries.
_OUTCOME_MARK = {  # nosec B105
    "PASS": "✅",
    "FAIL": "❌",
    "ERROR": "🛑",
    "SKIP": "⏭️",
    "WARN": "⚠️",
}


def _render_md(ctx, results, hard_fails, gap_fails, known_gaps, stamp, account):
    parts = _partition(results)
    lines = [
        "# API RBAC Test Report",
        "",
        f"- **Stack:** `{ctx['stack']}` ({ctx['region']}, account {account})",
        f"- **API base:** `{ctx['api_base']}`",
        f"- **Timestamp:** {stamp}",
        f"- **Git:** `{_git_sha()}`",
        f"- **Circuit breaker:** "
        f"{'enabled' if ctx.get('circuit_breaker') else 'disabled'}",
        "",
        f"**{len(parts['pass'])}/{len(results)} checks passed** — "
        f"{len(parts['fail'])} fail, {len(parts['inconclusive'])} could not be run, "
        f"{len(parts['skip'])} skipped, {len(gap_fails)} known-gap warning "
        f"({len(hard_fails)} hard fail).",
        "",
        "> A **skipped** check asserted nothing, and a check that **could not be "
        "run** (🛑 — a timeout, a dead connection, an unreadable body, a 5xx) "
        "established nothing. Neither is counted as a pass. A known gap downgrades "
        "either to a warning; it does not make it a pass.",
        "",
    ]
    if parts["inconclusive"]:
        lines += [
            "## 🛑 Could not be run",
            "",
            "These establish nothing about the API's behaviour — a refusal and an "
            "inconclusive result are different outcomes. Rows with no gap id are "
            "hard failures.",
            "",
            "| Op | Principal | Status | Detail | Gap | Request ID |",
            "|----|-----------|--------|--------|-----|------------|",
        ]
        for r in parts["inconclusive"]:
            lines.append(
                f"| `{r['op']}` | {r['principal']} | {r['http_status']} | "
                f"{r['detail']} | {r['known_gap'] or ''} | `{r['request_id']}` |"
            )
        lines.append("")
    if parts["fail"]:
        lines += [
            "## ❌ Hard failures",
            "",
            "| Op | Principal | Status | Detail | Request ID |",
            "|----|-----------|--------|--------|------------|",
        ]
        for r in parts["fail"]:
            lines.append(
                f"| `{r['op']}` | {r['principal']} | {r['http_status']} | "
                f"{r['detail']} | `{r['request_id']}` |"
            )
        lines.append("")
    if gap_fails:
        lines += [
            "## ⚠️ Known-gap findings (accepted risk)",
            "",
            "| Op | Principal | Status | Gap | Detail |",
            "|----|-----------|--------|-----|--------|",
        ]
        for r in gap_fails:
            lines.append(
                f"| `{r['op']}` | {r['principal']} | {r['http_status']} | "
                f"{r['known_gap']} | {r['detail']} |"
            )
        lines.append("")
    lines += [
        "## Full matrix",
        "",
        "| Op | Principal | Status | Pass | Detail | Request ID |",
        "|----|-----------|--------|------|--------|------------|",
    ]
    for r in results:
        mark = _OUTCOME_MARK.get(r["outcome"], "❌")
        if r["outcome"] == "WARN" and r.get("inconclusive"):
            mark = "⚠️🛑"
        lines.append(
            f"| `{r['op']}` | {r['principal']} | {r['http_status']} | {mark} | "
            f"{r['detail']} | `{r['request_id']}` |"
        )
    lines += ["", "## Known gaps register", ""]
    for gid, g in sorted(known_gaps.items()):
        lines.append(f"- **{gid}** — {g.get('summary', '')}")
    lines.append("")
    return "\n".join(lines)


def _run_token_lifecycle(ctx, results):
    """Orchestrate the token expiry (2.3) + logout revocation (2.4) suites.

    Mints a FRESH token for USER_B (so signing it out doesn't disturb the other
    suites, which have already run), then:
      * expiry: if IDP_SECTEST_WAIT_EXPIRY is set, mint a token, sleep past its
        lifetime, and use it as the "expired" token; otherwise skip with a note
        (token validity is provider-configured — often 1h — too long for CI).
      * logout: global-sign-out USER_B and re-test its token.
    """
    logout_email = test_email(USER_B)
    logout_token = get_token(ctx, USER_B)

    expired_token = None
    wait = os.environ.get("IDP_SECTEST_WAIT_EXPIRY")
    if wait:
        try:
            secs = int(wait)
        except ValueError:
            secs = 0
        if secs > 0:
            print(f"\n[expiry] minting a token and waiting {secs}s for it to expire...")
            expired_token = get_token(ctx, USER_B)
            time.sleep(secs)

    sec.run_token_lifecycle_suite(
        ctx,
        call,
        _record,
        results,
        expired_token=expired_token,
        logout_token=logout_token,
        logout_email=logout_email,
        sign_out_fn=lambda email: global_sign_out(ctx, email),
    )


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Live RBAC/auth test for the IDP API.")
    ap.add_argument("--stack-name", required=True)
    ap.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )
    ap.add_argument("--report-dir", help="write report.json/.md/meta.json here")
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--teardown-only", action="store_true")
    ap.add_argument("--no-teardown", action="store_true")
    args = ap.parse_args()
    if not args.region:
        ap.error("--region is required (or set AWS_REGION / AWS_DEFAULT_REGION)")

    ops, known_gaps = load_expectations()
    ctx = resolve_stack(args.stack_name, args.region)
    print(f"Stack: {args.stack_name} ({args.region})")
    print(f"UI user pool: {ctx['user_pool']}  client: {ctx['client']}")
    print(f"API base: {ctx['api_base']}")
    print(f"UsersTable: {ctx['users_table']}  circuit_breaker={ctx['circuit_breaker']}")

    if args.teardown_only:
        teardown_users(ctx)
        restore_auth_flows(ctx)
        return 0
    if args.setup_only:
        setup_users(ctx)
        print(f"Setup-only: test-user password is {TEST_PW}")
        return 0

    try:
        # setup inside the try so a mid-setup failure still tears down the
        # users already created and reverts the app-client auth flows.
        setup_users(ctx)
        tokens = {r: get_token(ctx, r) for r in ROLES}
        st = get_token(ctx, SCOPED)
        if st and len(st) > 100:
            tokens["scoped"] = st
        for r in ROLES:
            if not tokens[r] or len(tokens[r]) < 100:
                print(f"ERROR: failed to mint token for {r}")
                return 2

        # Second user for IDOR (checklist 2.1). Minted alongside the roles.
        ub = get_token(ctx, USER_B)
        if ub and len(ub) > 100:
            tokens["userB"] = ub

        results = []
        skip_fields = apply_dynamic_args(ops, ctx, tokens)
        run_group_matrix(ops, ctx, tokens, results, skip_fields)
        run_scope_suite(ctx, tokens, results)
        run_token_negatives(ctx, tokens, results)

        # --- Mandatory security-focused test cases (checklist 2.1-4) ---------
        # These run AFTER the RBAC matrix. The logout test globally signs out a
        # dedicated user (UserB), so it must run last (its token is consumed).
        sec.run_idor_suite(
            ctx,
            _record,
            results,
            tokens,
            seed_fn=lambda jid, marker: seed_agent_job(ctx, jid, marker),
            call_body=call_body,
        )
        sec.run_deleted_resource_suite(
            ctx, call, _record, results, tokens, call_body=call_body
        )
        strict_input = os.environ.get("IDP_SECTEST_STRICT_INPUT", "").lower() in (
            "1",
            "true",
            "yes",
        )
        sec.run_input_validation_suite(
            ctx, call, _record, results, tokens, strict=strict_input
        )
        sec.run_caller_supplied_ref_suite(
            ctx,
            _record,
            results,
            tokens,
            live_execution_arn=ctx.get("live_execution_arn"),
            call=call,
        )
        sec.run_tls_suite(ctx, _record, results)
        _run_token_lifecycle(ctx, results)

        # One definition of "blocking", shared with the report, so the summary a
        # reader sees and the code the process returns cannot disagree.
        parts = _partition(results)
        hard_fails = hard_failures(results)
        gap_fails = parts["warn"]

        print("\n=== RESULT ===")
        for r in parts["fail"]:
            print(f"  ✗ {r['op']}[{r['principal']}]: {r['detail']}")
        for r in parts["error"]:
            print(f"  🛑 {r['op']}[{r['principal']}]: {r['detail']}")
        for r in gap_fails:
            print(f"  ⚠ {r['op']}[{r['principal']}]: {r['detail']} [{r['known_gap']}]")
        print(
            f"{len(results)} checks: {len(parts['pass'])} passed, "
            f"{len(parts['fail'])} failed, "
            f"{len(parts['inconclusive'])} could not be run, "
            f"{len(parts['skip'])} skipped, {len(gap_fails)} known-gap warn "
            f"({len(hard_fails)} hard fail)"
        )
        if parts["inconclusive"]:
            print(
                "  NOTE: a check that could not be run establishes nothing. It is "
                "not a pass, and unless it is registered against a known gap it "
                "fails this gate."
            )

        if args.report_dir:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            account = aws(
                "sts", "get-caller-identity", "--query", "Account", "--output", "text"
            )
            write_report(args.report_dir, ctx, results, known_gaps, stamp, account)

        if not hard_fails:
            print("ALL HARD CHECKS PASSED")
        return 1 if hard_fails else 0
    finally:
        # Always remove any agent-job rows the IDOR suite seeded (independent of
        # --no-teardown, which only preserves the Cognito users for debugging).
        cleanup_seeded_agent_jobs(ctx)
        if args.no_teardown:
            print(f"--no-teardown: keeping test users (password: {TEST_PW})")
        else:
            teardown_users(ctx)
        # ALWAYS revert the app client's auth flows — --no-teardown only
        # keeps the test users, never the widened auth-flow setting.
        restore_auth_flows(ctx)


if __name__ == "__main__":
    sys.exit(main())
