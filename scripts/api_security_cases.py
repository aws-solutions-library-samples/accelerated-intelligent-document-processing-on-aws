#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Mandatory security-focused API test cases for the IDP UI REST API.

These implement the AppSec "Minimum Mandatory Security Focused Test Cases for
APIs" checklist as live suites layered on top of the RBAC/authorization matrix
in ``scripts/test_api_rbac.py``. That harness already covers checklist items
**1** (unauthenticated access denied) and **2 / 2.2** (the full role permission
matrix, negative + positive) via ``run_group_matrix`` + ``run_token_negatives``.
This module adds the remaining items:

  * **2.1  IDOR** — a second user (User B) must not read/modify User A's data,
           and an operation that takes a resource identifier from the caller
           must refuse an identifier outside the deployment / the caller's
           configuration scope.
  * **2.3  Token expiry** — an expired token is rejected (structural + optional
           live wait via ``IDP_SECTEST_WAIT_EXPIRY``).
  * **2.4  Logout revocation** — after global sign-out, a previously-issued
           token's continued acceptance is observed and reported (Cognito JWTs
           are stateless: this is expected to be a documented finding unless the
           API checks token revocation).
  * **2.5  Deleted-resource access** — a deleted resource is no longer readable.
  * **3    Input validation** — wrong-typed / malformed / unknown arguments are
           rejected. Tolerant by default (accepts today's 400-or-500); set
           ``IDP_SECTEST_STRICT_INPUT`` to require a clean 4xx (the behavior
           PR B / central schema validation introduces).
  * **4    TLS** — TLS 1.0 / 1.1 and plaintext HTTP must be refused; only
           TLS 1.2+ is accepted.

Design notes
------------
* The functions take the harness's ``call``/``record`` callables (dependency-
  injected) so this module has no import cycle with ``test_api_rbac.py`` and can
  be unit-tested with fakes.
* Every check is recorded via the same ``_record`` shape the RBAC harness uses,
  so results flow into the existing report/summary unchanged. A check maps to a
  ``known_gap`` id (documented, WARN not FAIL) when the current backend behavior
  is a known/accepted limitation (e.g. stateless-JWT logout).
* Second user (User B) creation/teardown is owned by ``test_api_rbac.py``'s
  setup/teardown; this module only drives requests with the tokens it's given.
"""

import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# Checklist ids used in report details so AppSec sign-off is traceable.
SEC_IDOR = "SEC-2.1-IDOR"
SEC_EXPIRY = "SEC-2.3-TOKEN-EXPIRY"
SEC_LOGOUT = "SEC-2.4-LOGOUT-REVOCATION"
SEC_DELETED = "SEC-2.5-DELETED-RESOURCE"
SEC_INPUT = "SEC-3-INPUT-VALIDATION"
SEC_TLS = "SEC-4-TLS"
SEC_OBJREF = "SEC-2.1-CALLER-SUPPLIED-REF"


# ---------------------------------------------------------------------------
# A refusal and an inconclusive result are different outcomes
#
# Every suite below asserts something about how the API REFUSED a request. A
# request that never completed, or whose response could not be read, refutes
# nothing and confirms nothing — and it is the one case a "did the refusal marker
# appear?" test gets wrong by default, because a marker is absent from a response
# you never received.
#
# The shapes that were scoring as passes before this was factored out:
#   * `call_body` returns (0, "<request error: ...>") for a timeout or a dead
#     connection, and no marker is "present" in that string;
#   * a `record(..., "ERR", True, ...)` after an exception;
#   * a setup call that did not return 200/201, recorded as passed;
#   * `socket.gaierror` / `ConnectionRefusedError` / `socket.timeout` — all
#     `OSError` subclasses — read as "the endpoint refused this protocol".
#
# These helpers are the single place that judgement is made, so a new suite cannot
# reintroduce the conflation by hand.
# ---------------------------------------------------------------------------

# Mirrors test_api_rbac.UNREADABLE_BODY; duplicated rather than imported because
# that module imports THIS one, and the value is a private marker either way.
UNREADABLE_BODY = "__unreadable_body__"


def _inconclusive_response(status, body=None, et=None, treat_5xx=True):
    """Why this response established nothing, or ``None`` if it established
    something.

    ``treat_5xx`` is a real distinction, not a convenience. For an assertion of the
    form "the caller must have been REFUSED", a 5xx settles nothing — whether the
    resolver refused before or after doing the work is exactly what is unknown. For
    an assertion of the form "this response must NOT CONTAIN X", a 5xx settles it
    perfectly well: the body is in hand and X is not in it. The IDOR suite is the
    second kind, so it passes ``treat_5xx=False``; everything else is the first.
    """
    if status in (0, "ERR", None):
        return "request did not complete (timeout / connection error)"
    if et == UNREADABLE_BODY:
        return "response body was empty or not JSON"
    if body is not None and str(body).startswith("<request error:"):
        return f"request did not complete: {body}"
    if treat_5xx and isinstance(status, int) and status >= 500:
        return f"server error {status}"
    return None


# ---------------------------------------------------------------------------
# 2.1 IDOR — User A's data is not reachable by User B
# ---------------------------------------------------------------------------
def run_idor_suite(ctx, record, results, tokens, seed_fn=None, call_body=None):
    """User B must not read User A's agent-job data (indirect object reference /
    broken object-level authorization).

    The security property under test is **non-disclosure**: whatever the backend
    returns to User B for User A's object id, it must NOT contain User A's data.
    We assert on the RESPONSE BODY, not the status code, because the resolver's
    ownership defense can surface as 200-empty, 403, or a 500 "not found for this
    user" — all acceptable (no disclosure); only User A's marker appearing in
    User B's response is a real leak.

    Uses agent jobs, whose ownership is enforced by construction: the resolver
    derives the DynamoDB partition key from the CALLER's identity and uses the
    supplied jobId only as the sort key, so User B's read of A's jobId resolves
    under B's own (empty) partition. ``seed_fn(job_id, marker) -> owner_user_id``
    seeds a job owned by User A directly in the agent table (injected by the
    harness; returns None if it can't seed, e.g. table not present -> SKIP).

    Requires two authenticated users + a working seed + body-returning call. Any
    missing precondition records a SKIP — which is not a pass, but is not a failure
    either: an absent precondition is not a satisfied assertion.
    """
    print("\n=== IDOR (User B cannot access User A's data) ===")
    a_tok = tokens.get("Admin")
    b_tok = tokens.get("userB")
    if not a_tok or not b_tok or seed_fn is None or call_body is None:
        record(
            results,
            "getAgentJobStatus",
            "idor",
            "SKIP",
            False,
            f"{SEC_IDOR}: preconditions unavailable (two users + seed + "
            "body-call) — skipped",
            outcome="SKIP",
        )
        print("  SKIP (preconditions unavailable)")
        return

    marker = f"IDOR-MARKER-{_rand()}"
    job_id = f"sectest-idor-{_rand()}"
    # Seed a job OWNED BY USER A (Admin), carrying the marker, keyed so that only
    # A's identity-derived partition can read it.
    owner_uid = None
    try:
        owner_uid = seed_fn(job_id, marker)
    except Exception as e:  # noqa: BLE001
        print(f"  (seed error: {e})")
    if not owner_uid:
        record(
            results,
            "getAgentJobStatus",
            "idor-seed",
            "SKIP",
            False,
            f"{SEC_IDOR}: could not seed A's job (agent table unavailable) — skipped",
            outcome="SKIP",
        )
        print("  SKIP (could not seed User A's job)")
        return

    # 1. User B reads A's job id -> body must NOT contain A's marker.
    #
    # `call_body` returns (0, "<request error: ...>") for a timeout or a dead
    # connection. That body contains no marker, so "marker absent" used to pass this
    # assertion — the harness concluded "User B must not receive A's data" from a
    # request that never reached the API. Absence of a leak in a response we never
    # got is not absence of a leak.
    st, body = call_body(ctx["api_base"], "getAgentJobStatus", {"jobId": job_id}, b_tok)
    # treat_5xx=False: the property here is non-disclosure, and a 500 whose body we
    # can read and which does not contain A's marker establishes it. What does not
    # establish it is having no response at all.
    why = _inconclusive_response(st, body, treat_5xx=False)
    leaked = marker in (body or "")
    record(
        results,
        "getAgentJobStatus",
        "userB(reads A's job)",
        st,
        (not why) and not leaked,
        f"{SEC_IDOR}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_IDOR}: User B must not receive A's data; marker "
            f"{'PRESENT (LEAK)' if leaked else 'absent'}; got {st}"
        ),
        outcome="ERROR" if why else None,
    )
    if why:
        print(f"  User B getAgentJobStatus(A's job) -> {st} (INCONCLUSIVE: {why})")
        print("  IDOR check abandoned: the probe request did not complete")
        return
    print(
        f"  User B getAgentJobStatus(A's job) -> {st} "
        f"({'LEAK' if leaked else 'OK no disclosure'})"
    )

    # 2. User B tries to DELETE A's job -> must not destroy A's data (verified by
    #    A's read below still returning the marker).
    st, _b = call_body(ctx["api_base"], "deleteAgentJob", {"jobId": job_id}, b_tok)
    print(f"  User B deleteAgentJob(A's job) -> {st}")

    # 3. Control: User A reads its own job -> body SHOULD contain the marker,
    #    which proves (a) the object exists, (b) the owner retains access, and
    #    (c) B's delete did not remove it.
    #
    #    IMPORTANT: if the marker is ABSENT for the OWNER too, that is almost
    #    certainly a SEED-KEYING mismatch (the seeder wrote PK=agent#<Admin email>
    #    but this deployment derives the caller identity differently, e.g. as the
    #    Cognito sub), NOT a security failure. A broken seed makes the whole IDOR
    #    check inconclusive — step 1 saw "no marker" only because NOBODY can read
    #    the row. Record it as an inconclusive SKIP (pass) rather than a false
    #    hard-fail; the leak assertion in step 1 is only meaningful when the owner
    #    can actually read its own seeded row.
    st, body = call_body(ctx["api_base"], "getAgentJobStatus", {"jobId": job_id}, a_tok)
    a_ok = marker in (body or "")
    if a_ok:
        record(
            results,
            "getAgentJobStatus",
            "userA(reads own job)",
            st,
            True,
            f"{SEC_IDOR}: owner retains access (marker present); got {st}",
        )
        print(f"  User A getAgentJobStatus(own job) -> {st} (OK owner sees data)")
    else:
        # Step 1's result is inconclusive too: without a readable seed, "no marker
        # for B" proves nothing. The comment here has always said "downgrade step
        # 1's result" — but appending a second row did not downgrade anything, it
        # left step 1's PASS standing beside a SKIP. Retract it for real.
        for row in results:
            if (
                row["op"] == "getAgentJobStatus"
                and row["principal"] == "userB(reads A's job)"
            ):
                row["passed"] = False
                row["outcome"] = "SKIP"
                row["detail"] = (
                    f"{SEC_IDOR}: retracted — the owner could not read the seeded "
                    "job either, so 'no marker for User B' proves nothing"
                )
        record(
            results,
            "getAgentJobStatus",
            "idor-inconclusive",
            "SKIP",
            False,
            f"{SEC_IDOR}: owner could not read the seeded job (marker absent for "
            f"the owner too; got {st}) — seed keying mismatch, IDOR check "
            "inconclusive on this deployment (NOT a security failure)",
            outcome="SKIP",
        )
        print(
            f"  User A getAgentJobStatus(own job) -> {st} "
            "(marker absent for OWNER too — seed keying mismatch, inconclusive)"
        )


# ---------------------------------------------------------------------------
# 2.3 Token expiry / 2.4 logout revocation
# ---------------------------------------------------------------------------
def run_token_lifecycle_suite(
    ctx, call, record, results, expired_token, logout_token, logout_email, sign_out_fn
):
    """2.3: an expired token is rejected (401/403).
    2.4: after global sign-out, whether a previously-issued token is still
         accepted is observed and reported.

    expired_token: a genuinely-expired but validly-signed token, or None to skip
                   (real expiry needs a wait; see IDP_SECTEST_WAIT_EXPIRY caller).
    logout_token:  a token minted for logout_email BEFORE sign-out.
    sign_out_fn:   callable(email) performing global sign-out (rbac_common.
                   global_sign_out) — injected so this stays unit-testable.
    """
    print("\n=== TOKEN LIFECYCLE (expiry + logout) ===")

    # 2.3 expiry
    if expired_token:
        st, et, ib, rid = call(ctx["api_base"], "listDocuments", {}, expired_token)
        why = _inconclusive_response(st, et=et)
        ok = (not why) and st in (401, 403)
        record(
            results,
            "listDocuments",
            "token:expired",
            st,
            ok,
            f"{SEC_EXPIRY}: INCONCLUSIVE — {why}"
            if why
            else f"{SEC_EXPIRY}: expired token must be rejected; got {st}",
            et,
            ib,
            rid,
            outcome="ERROR" if why else None,
        )
        print(f"  expired token -> {st} ({'OK rejected' if ok else 'LEAK'})")
    else:
        record(
            results,
            "listDocuments",
            "token:expired",
            "SKIP",
            False,
            f"{SEC_EXPIRY}: skipped (set IDP_SECTEST_WAIT_EXPIRY to wait for "
            "a real token to expire; token validity is provider-configured)",
            outcome="SKIP",
        )
        print("  SKIP expiry (no expired token; set IDP_SECTEST_WAIT_EXPIRY)")

    # 2.4 logout revocation
    if not (logout_token and logout_email and sign_out_fn):
        record(
            results,
            "listDocuments",
            "token:post-logout",
            "SKIP",
            False,
            f"{SEC_LOGOUT}: skipped (logout token/user unavailable)",
            outcome="SKIP",
        )
        print("  SKIP logout (token/user unavailable)")
        return

    # sanity: the token works BEFORE logout
    st_before, *_ = call(ctx["api_base"], "listDocuments", {}, logout_token)
    try:
        sign_out_fn(logout_email)
    except Exception as e:  # noqa: BLE001
        record(
            results,
            "listDocuments",
            "token:post-logout",
            "ERR",
            False,
            f"{SEC_LOGOUT}: global sign-out call failed ({e}) — the revocation "
            "check could not be run",
            outcome="ERROR",
        )
        print(f"  ERROR logout (sign-out failed, check not run: {e})")
        return

    st_after, et, ib, rid = call(ctx["api_base"], "listDocuments", {}, logout_token)
    # A request that never completed is not evidence that the token was revoked, and
    # not evidence that it still works either. treat_5xx stays on: a 5xx tells us
    # nothing about revocation.
    why = _inconclusive_response(st_after, et=et)
    revoked = (not why) and st_after in (401, 403)
    # Cognito ID/access JWTs are stateless: unless the API validates revocation,
    # a token remains valid until `exp` even after global sign-out. That is the
    # documented behavior, so a still-accepted token is a KNOWN GAP (WARN), not a
    # hard failure — but we surface it loudly so AppSec can decide.
    record(
        results,
        "listDocuments",
        "token:post-logout",
        st_after,
        revoked,
        f"{SEC_LOGOUT}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_LOGOUT}: token before-logout={st_before}, "
            f"after-logout={st_after}. "
            f"{'Revoked' if revoked else 'STILL ACCEPTED (stateless JWT — see gap)'}"
        ),
        et,
        ib,
        rid,
        # GAP-SEC-LOGOUT documents a token that is STILL ACCEPTED. A call that did
        # not complete is not that observation, so it must not borrow the gap and
        # become a warning about revocation.
        gap=None if (why or revoked) else "GAP-SEC-LOGOUT",
        outcome="ERROR" if why else None,
    )
    print(
        f"  post-logout token -> {st_after} "
        f"({'OK revoked' if revoked else 'STILL ACCEPTED (documented gap)'})"
    )


# ---------------------------------------------------------------------------
# 2.5 Deleted resource is no longer accessible
# ---------------------------------------------------------------------------
def run_deleted_resource_suite(ctx, call, record, results, tokens, call_body=None):
    """A resource, once deleted, must no longer be enumerable/accessible. Uses a
    custom config version (Admin can create + delete).

    IMPORTANT: getConfigVersion returns HTTP 200 with a DEFAULT schema for ANY
    name (even one that never existed), so it is NOT a valid existence oracle.
    We instead assert on **list membership**: the created version must appear in
    getConfigVersions before delete and must be ABSENT after delete. This
    requires a body-returning call (call_body); without it the suite SKIPs.
    """
    print("\n=== DELETED RESOURCE (gone after delete) ===")
    admin = tokens.get("Admin")
    if not admin or call_body is None:
        record(
            results,
            "deleteConfigVersion",
            "deleted-resource",
            "SKIP",
            False,
            f"{SEC_DELETED}: admin token / body-call unavailable — skipped",
            outcome="SKIP",
        )
        return

    version = f"sectest-del-{_rand()}"
    st, et, ib, rid = call(
        ctx["api_base"],
        "updateConfiguration",
        {
            "versionName": version,
            "customConfig": "{}",
            "description": "sectest deleted-resource",
        },
        admin,
    )
    created = not _denied(st, et, ib) and st in (200, 201)
    if not created:
        # A setup step that did not work is not a satisfied assertion. It used to be
        # recorded with passed=True, so a deployment where creating a throwaway
        # config version was BROKEN reported "deleted resources are no longer
        # accessible" as proven. Which of the two it is matters: a 5xx or a dead
        # connection means the harness could not run the check (ERROR), while a
        # clean 4xx means the precondition is genuinely absent here (SKIP).
        why = _inconclusive_response(st, et=et)
        record(
            results,
            "updateConfiguration",
            "deleted-resource-setup",
            st,
            False,
            f"{SEC_DELETED}: could not create throwaway version "
            f"({st}/{et}/{ib})"
            + (f" — INCONCLUSIVE: {why}" if why else " — precondition absent"),
            et,
            ib,
            rid,
            outcome="ERROR" if why else "SKIP",
        )
        print(f"  {'ERROR' if why else 'SKIP'} (create returned {st}/{et}/{ib})")
        return

    present_before, why_before = _version_listed(call_body, ctx, admin, version)
    st, del_body = call_body(
        ctx["api_base"], "deleteConfigVersion", {"versionName": version}, admin
    )
    listed_after, why_after = _version_listed(call_body, ctx, admin, version)
    absent_after = not listed_after

    # The security assertion: it was listed before, and is NOT listed after. Any of
    # the three calls failing to complete makes it unanswerable rather than false —
    # "not listed after" read out of a response that never arrived would report the
    # resource correctly deleted.
    why = (
        why_before or _inconclusive_response(st, del_body, treat_5xx=False) or why_after
    )
    gone = (not why) and present_before and absent_after
    record(
        results,
        "getConfigVersions",
        "after-delete",
        st,
        gone,
        f"{SEC_DELETED}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_DELETED}: listed-before={present_before}, delete-status={st}, "
            f"listed-after={not absent_after}. "
            f"{'Gone' if gone else 'STILL ENUMERABLE (leak)'}"
        ),
        outcome="ERROR" if why else None,
    )
    print(
        f"  version after delete -> listed_before={present_before} "
        f"listed_after={not absent_after} "
        f"({'OK gone' if gone else 'STILL ENUMERABLE'})"
    )


def _version_listed(call_body, ctx, token, version):
    """Whether `version` appears in getConfigVersions.

    Returns ``(listed, why_inconclusive)``. The second value is what stops "the
    version is not in the body" being read out of a body that was never received:
    `call_body` answers ``(0, "<request error: ...>")`` for a dead connection, and a
    deleted-resource assertion built on that substring would report the resource
    correctly gone.
    """
    st, body = call_body(ctx["api_base"], "getConfigVersions", {}, token)
    why = _inconclusive_response(st, body, treat_5xx=False)
    return version in (body or ""), why


# ---------------------------------------------------------------------------
# 3. Input validation — malformed / wrong-typed / unknown args
# ---------------------------------------------------------------------------
def run_input_validation_suite(ctx, call, record, results, tokens, strict=False):
    """Feed each of a representative set of ops deliberately-malformed arguments
    and assert they are handled cleanly.

    Only a clean 4xx is ever a PASS. In the default (tolerant) mode a 5xx or a
    silent 200 is recorded against ``GAP-SEC-INPUT``, which makes it a **WARN** —
    visible in the report, not a hard failure, and not a pass. STRICT mode
    (``IDP_SECTEST_STRICT_INPUT``, or after central schema validation lands) drops
    the gap so the same result becomes a hard failure. The tolerance is therefore
    opt-OUT of blocking, never opt-out of reporting.

    Every malformed case is sent as an AUTHENTICATED Admin so we test validation,
    not authorization (auth is covered by the RBAC matrix).
    """
    mode = "STRICT (clean 4xx required)" if strict else "tolerant (4xx or 5xx ok)"
    print(f"\n=== INPUT VALIDATION [{mode}] ===")
    admin = tokens.get("Admin")
    if not admin:
        record(
            results,
            "input-validation",
            "*",
            "SKIP",
            False,
            f"{SEC_INPUT}: admin token unavailable — skipped",
            outcome="SKIP",
        )
        return

    # (op, malformed-args, why). Each targets a specific type-confusion / shape
    # attack the GraphQL schema used to reject at the boundary.
    #
    # SAFETY: only READ ops are probed with malformed input. A mutation (e.g.
    # reprocessDocument) must NOT appear here — pre-PR-B there is no central
    # validation, so a malformed mutation payload could reach the resolver and
    # trigger a real side effect. The list-vs-scalar case is covered by
    # getConfigVersion's list arg instead (a read). Wrong-typed values use
    # nonexistent ids so even if a read op runs, it finds nothing.
    cases = [
        (
            "getDocument",
            {"ObjectKey": {"$ne": None}},
            "object where String! expected (NoSQL-style injection shape)",
        ),
        ("getDocument", {"ObjectKey": [1, 2, 3]}, "array where String! expected"),
        ("getConfigVersion", {"versionName": 12345}, "int where String! expected"),
        (
            "getDocument",
            {"ObjectKey": ["a", "b"]},
            "array where scalar String! expected (list-vs-scalar)",
        ),
        ("listDocuments", {"limit": "abc"}, "non-numeric limit where Int expected"),
        (
            "getDocument",
            {"ObjectKey": "x", "unexpectedField": "surprise"},
            "unknown argument not in schema",
        ),
        ("getConfigVersion", {}, "missing required non-null arg versionName"),
    ]
    for op, args, why in cases:
        st, et, ib, rid = call(ctx["api_base"], op, args, admin)
        # A 5xx is a real observation HERE (the resolver blew up on the bad shape —
        # that is the documented weakness this suite exists to surface), so it is
        # not treated as inconclusive. A request that never completed, or a response
        # that could not be read, still is: it says nothing about validation.
        unreadable = st in (0, None) or et == UNREADABLE_BODY
        if unreadable:
            record(
                results,
                op,
                f"malformed:{_short(why)}",
                st,
                False,
                f"{SEC_INPUT}: {why} -> INCONCLUSIVE: "
                f"{_inconclusive_response(st, et=et)}",
                et,
                ib,
                rid,
                outcome="ERROR",
            )
            print(f"  {op:22s} [{_short(why)}] -> {st} (ERROR: no response)")
            continue
        # isinstance first: `st` is whatever the injected `call` returns, and the
        # inconclusive branch above has already taken the non-integer cases out.
        clean_4xx = isinstance(st, int) and 400 <= st < 500
        silent_accept = st == 200
        gap = None
        if strict:
            # PR B / central schema validation: only a clean 4xx passes. A 5xx or
            # a silent 200 is a hard failure (the regression guard).
            ok = clean_4xx
            detail = f"{SEC_INPUT}: {why} -> expect clean 4xx; got {st}" + (
                "" if ok else " (NOT cleanly rejected — should be 400)"
            )
        else:
            # Tolerant (pre-PR-B): there is no central input validation yet, so
            # neither a resolver 5xx NOR a silent 200 is a hard failure — both are
            # DOCUMENTED WEAKNESSES (WARN) that central schema validation closes.
            # Recording them as known-gap keeps the current stack's CI gate green
            # while still surfacing the exposure in the report. Only a clean 4xx
            # is an unqualified pass.
            ok = clean_4xx
            if not ok:
                gap = "GAP-SEC-INPUT"  # WARN, not hard fail, until PR B lands
            kind = (
                "(clean 4xx)"
                if clean_4xx
                else "(SILENTLY ACCEPTED — no validation)"
                if silent_accept
                else "(resolver 5xx — uncaught bad shape)"
            )
            detail = f"{SEC_INPUT}: {why} -> got {st} {kind}"
        record(
            results,
            op,
            f"malformed:{_short(why)}",
            st,
            ok,
            detail,
            et,
            ib,
            rid,
            gap=gap,
        )
        verdict = "OK" if ok else ("WARN" if gap else "FAIL")
        print(f"  {op:22s} [{_short(why)}] -> {st} ({verdict})")


# ---------------------------------------------------------------------------
# 4. TLS — reject TLS 1.0/1.1 and plaintext HTTP
# ---------------------------------------------------------------------------
def run_tls_suite(ctx, record, results):
    """The API endpoint must refuse TLS 1.0 and TLS 1.1 and must not serve over
    plaintext HTTP. Only TLS 1.2+ is acceptable. Uses raw sockets to force a
    specific protocol version at handshake time.
    """
    print("\n=== TLS CONFIGURATION ===")
    host = urlsplit(ctx["api_base"]).hostname
    port = 443
    if not host:
        record(
            results,
            "tls",
            "*",
            "SKIP",
            False,
            f"{SEC_TLS}: could not resolve API host — skipped",
            outcome="SKIP",
        )
        return

    # TLS 1.0 and 1.1 must be refused.
    for label, proto in (
        ("TLS1.0", ssl.TLSVersion.TLSv1),
        ("TLS1.1", ssl.TLSVersion.TLSv1_1),
    ):
        outcome, note = _tls_probe(host, port, proto)
        record(
            results,
            "tls",
            label,
            "n/a",
            outcome == REFUSED,
            f"{SEC_TLS}: {label} must be refused — {note}",
            outcome="ERROR" if outcome == INCONCLUSIVE else None,
        )
        shown = {
            REFUSED: "OK refused",
            ACCEPTED: "ACCEPTED (weak)",
            INCONCLUSIVE: "INCONCLUSIVE",
        }[outcome]
        print(f"  {label:8s} -> {shown} ({note})")

    # TLS 1.2 must be accepted (proves we're testing a live TLS endpoint, not a
    # blanket-refusing host).
    outcome12, note12 = _tls_probe(host, port, ssl.TLSVersion.TLSv1_2)
    record(
        results,
        "tls",
        "TLS1.2",
        "n/a",
        outcome12 == ACCEPTED,
        f"{SEC_TLS}: TLS1.2 must be accepted — {note12}",
        outcome="ERROR" if outcome12 == INCONCLUSIVE else None,
    )
    print(
        f"  TLS1.2   -> "
        f"{'OK accepted' if outcome12 == ACCEPTED else outcome12.upper()} ({note12})"
    )

    # Plaintext HTTP must not serve the API (connection refused, timeout, or a
    # redirect/deny — anything but a 2xx over cleartext on :80).
    outcome80, note80 = _http_probe(host)
    record(
        results,
        "tls",
        "plaintext-http",
        "n/a",
        outcome80 == REFUSED,
        f"{SEC_TLS}: plaintext HTTP must not serve the API — {note80}",
        outcome="ERROR" if outcome80 == INCONCLUSIVE else None,
    )
    shown80 = {
        REFUSED: "OK not served",
        ACCEPTED: "SERVED (weak)",
        INCONCLUSIVE: "INCONCLUSIVE",
    }[outcome80]
    print(f"  HTTP:80  -> {shown80} ({note80})")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# 2.1 Caller-supplied resource reference — outside the deployment / the scope
# ---------------------------------------------------------------------------
def run_caller_supplied_ref_suite(
    ctx, record, results, tokens, live_execution_arn=None, call=None
):
    """An operation handed a resource id by the caller must bound what it accepts.

    ``getStepFunctionExecution`` takes an ``executionArn`` straight from the
    request. The group matrix proves an authenticated caller of any role CAN read
    an execution of this deployment; it cannot prove the resolver refuses one it
    should not serve, because for an ANY-auth op the matrix reads every denial as
    a failure. This suite asserts the two denials:

      * an ARN naming a DIFFERENT state machine — the resolver's IAM grant is a
        ``<stack-name>-*`` prefix, which also covers a sibling deployment whose
        stack name extends this one's, so IAM alone does not draw this line;
      * an execution outside a config-version-scoped caller's ``allowedConfigVersions``.

    Both need a live ARN from this stack (the harness resolves one from
    listDocuments -> getDocument); without one every check SKIPs rather than
    failing, since a stack that has processed nothing has no execution to name.
    """
    print("\n=== CALLER-SUPPLIED RESOURCE REFERENCE ===")
    admin = tokens.get("Admin")
    scoped = tokens.get("scoped")
    if not admin or call is None or not live_execution_arn:
        record(
            results,
            "getStepFunctionExecution",
            "caller-ref",
            "SKIP",
            False,
            f"{SEC_OBJREF}: no live execution ARN in this stack — skipped",
            outcome="SKIP",
        )
        print("  SKIP (no live execution ARN)")
        return

    # A sibling deployment's ARN: same account, same region, a state-machine name
    # that the resolver's IAM prefix would still cover.
    parts = live_execution_arn.split(":")
    foreign = ":".join(parts[:6] + [parts[6] + "-sibling-deployment"] + parts[7:])
    st, et, ib, rid = call(
        ctx["api_base"], "getStepFunctionExecution", {"executionArn": foreign}, admin
    )
    why = _inconclusive_response(st, et=et)
    denied = (not why) and _denied(st, et, ib)
    record(
        results,
        "getStepFunctionExecution",
        "foreign-state-machine",
        st,
        denied,
        f"{SEC_OBJREF}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_OBJREF}: ARN naming another state machine must be refused; "
            f"got {st}/{et}/{ib}"
        ),
        et,
        ib,
        rid,
        outcome="ERROR" if why else None,
    )
    print(
        f"  Admin, ARN of another state machine -> {st}/{et or ib} "
        f"({'OK refused' if denied else 'SERVED — not bounded'})"
    )

    # The same op with the stack's OWN ARN must still be served, so the check
    # above cannot pass by refusing everything.
    st, et, ib, rid = call(
        ctx["api_base"],
        "getStepFunctionExecution",
        {"executionArn": live_execution_arn},
        admin,
    )
    # "not denied" is satisfied by a 500, an empty body or a dead connection, which
    # is the same conflation classify() had: this arm exists to prove the control does
    # not OVER-deny, and only a real response can prove that.
    why = _inconclusive_response(st, et=et)
    served = (not why) and not _denied(st, et, ib)
    record(
        results,
        "getStepFunctionExecution",
        "own-state-machine",
        st,
        served,
        f"{SEC_OBJREF}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_OBJREF}: this deployment's own execution must still be served; "
            f"got {st}/{et}/{ib}"
        ),
        et,
        ib,
        rid,
        outcome="ERROR" if why else None,
    )
    print(
        f"  Admin, this stack's own execution -> {st} "
        f"({'OK served' if served else 'WRONGLY REFUSED'})"
    )

    if not scoped:
        record(
            results,
            "getStepFunctionExecution",
            "out-of-scope",
            "SKIP",
            False,
            f"{SEC_OBJREF}: scoped user unavailable — skipped",
            outcome="SKIP",
        )
        print("  SKIP out-of-scope check (scoped user unavailable)")
        return

    # The scoped user is restricted to SCOPE_VERSION; the seeded document ran
    # under the active version, so this execution is outside their scope unless
    # the two happen to coincide.
    st, et, ib, rid = call(
        ctx["api_base"],
        "getStepFunctionExecution",
        {"executionArn": live_execution_arn},
        scoped,
    )
    why = _inconclusive_response(st, et=et)
    denied = (not why) and _denied(st, et, ib)
    record(
        results,
        "getStepFunctionExecution",
        "scoped(out-of-scope)",
        st,
        denied,
        f"{SEC_OBJREF}: INCONCLUSIVE — {why}"
        if why
        else (
            f"{SEC_OBJREF}: a config-scoped caller must not read an execution "
            f"outside their scope; got {st}/{et}/{ib}"
        ),
        et,
        ib,
        rid,
        outcome="ERROR" if why else None,
    )
    print(
        f"  scoped Author, out-of-scope execution -> {st}/{et or ib} "
        f"({'OK refused' if denied else 'SERVED — scope not applied'})"
    )


# ---------------------------------------------------------------------------
def _denied(status, et, in_band=None):
    return status in (401, 403) or et == "Unauthorized" or in_band == "Unauthorized"


def _rand():
    # Avoid Math.random-style nondeterminism concerns: use urandom hex.
    import os as _os

    return _os.urandom(6).hex()


def _short(text):
    return text.split("(")[0].strip()[:28]


# The three things a protocol probe can establish. "inconclusive" exists because
# the TLS probes used to fold every OSError into "the server refused this
# protocol" — and socket.gaierror (DNS), ConnectionRefusedError and socket.timeout
# are all OSError subclasses. A host that had simply gone away therefore reported
# "TLS 1.0 refused ✅ / TLS 1.1 refused ✅", i.e. two passes from zero observations.
REFUSED = "refused"
ACCEPTED = "accepted"
INCONCLUSIVE = "inconclusive"


def _connect(host, port):
    """TCP-connect, separated from the handshake.

    This split is the whole fix: reaching the port is what makes a handshake
    failure mean "the server declined this protocol". If the connection itself
    fails we never spoke TLS, so there is nothing to conclude.
    """
    try:
        return socket.create_connection((host, port), timeout=10), None
    except OSError as e:
        return None, f"could not reach {host}:{port} ({type(e).__name__}: {e})"


def _tls_probe(host, port, version):
    """Probe one pinned TLS version. Returns (REFUSED|ACCEPTED|INCONCLUSIVE, note).

    One function for both directions, so "was it refused?" and "was it accepted?"
    cannot disagree about what a given failure meant.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = version
        ctx.maximum_version = version
    except ValueError as e:
        # The client's own OpenSSL will not offer this version, so the server was
        # never asked. Previously counted as "refused" — a pass asserted from the
        # local build of OpenSSL rather than from the endpoint.
        return INCONCLUSIVE, f"client cannot offer {version.name}: {e}"
    sock, err = _connect(host, port)
    if sock is None:
        return INCONCLUSIVE, err
    try:
        with sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                return ACCEPTED, f"negotiated {ss.version()}"
    except ssl.SSLError as e:
        # We reached the port and the handshake failed: a real protocol refusal.
        return REFUSED, f"handshake failed ({type(e).__name__}: {e})"
    except ConnectionResetError as e:
        # How a load balancer commonly declines an obsolete protocol.
        return REFUSED, f"connection reset during handshake ({e})"
    except OSError as e:
        return (
            INCONCLUSIVE,
            f"connection lost during handshake ({type(e).__name__}: {e})",
        )


# Connection outcomes that are a POSITIVE observation that nothing serves the port:
# the host answered, and its answer was "no". A TCP reset is the shape
# "execute-api does not listen on :80" actually takes.
_PORT_CLOSED_ERRORS = (ConnectionRefusedError, ConnectionResetError)


def _http_probe(host):
    """Whether plaintext HTTP serves the API. (REFUSED|ACCEPTED|INCONCLUSIVE, note).

    API Gateway execute-api does not listen on :80, so "the port answered with a
    reset" is the expected pass. Three failures are NOT that, and each has to be
    separated from it rather than folded in:

    * **DNS** — the name did not resolve, so port 80 was never asked.
    * **A connect timeout** — the packet went nowhere and nothing came back. Common
      on a restricted network with blackholed egress, and indistinguishable from a
      healthy endpoint if counted as a refusal: the probe would report "plaintext
      HTTP refused" having observed nothing at all.
    * **Anything else** — an OSError this function does not recognise is not evidence
      either way, so it says so instead of guessing in the reassuring direction.
    """
    url = f"http://{host}/"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            # Served something over cleartext — only a redirect to https is ok.
            loc = r.headers.get("Location", "")
            if r.status in (301, 302, 307, 308) and loc.startswith("https://"):
                return REFUSED, f"HTTP {r.status} redirect to https"
            return ACCEPTED, f"served HTTP {r.status} over cleartext"
    except urllib.error.HTTPError as e:
        # A 4xx/5xx over cleartext still means :80 answered; only a redirect is
        # acceptable, handled above. Treat other HTTP responses as weak.
        return ACCEPTED, f"HTTP {e.code} over cleartext"
    except (urllib.error.URLError, OSError) as e:
        # URLError wraps the real cause in `.reason`; a bare OSError is its own.
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.gaierror):
            return INCONCLUSIVE, f"host did not resolve ({reason})"
        if isinstance(reason, _PORT_CLOSED_ERRORS):
            return REFUSED, f"no cleartext service ({type(reason).__name__})"
        # socket.timeout is TimeoutError on 3.10+; named explicitly for clarity.
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return INCONCLUSIVE, f"connect to :80 timed out ({reason})"
        return INCONCLUSIVE, f"could not probe :80 ({type(reason).__name__}: {reason})"
