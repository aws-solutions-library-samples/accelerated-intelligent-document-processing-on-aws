"""Unit tests for scripts/api_security_cases.py — the mandatory security-focused
API test suites (IDOR, token lifecycle, deleted-resource, input validation, TLS).

These run WITHOUT any AWS/live API: the harness `call`/`record` callables and the
sign-out function are replaced with fakes, so we verify the suites' decision logic
(what counts as pass/fail, which checklist item each records, tolerant-vs-strict
input mode, and the WARN-not-FAIL treatment of the stateless-JWT logout gap).

api_security_cases.py lives in scripts/ (a quarantined pytest root because of the
live harness there), so we import it by file path like the other sdlc tests import
dispatcher code.
"""

import importlib.util
import json
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_harness():
    """Load the live harness, and take `api_security_cases` from ITS import.

    scripts/test_api_rbac.py imports api_security_cases at module scope, so loading
    the harness first and then reading the module out of sys.modules gives the SAME
    module object the harness holds — which is what makes monkeypatching the probe
    helpers below actually affect the code under test.
    """
    path = Path(__file__).resolve().parents[2] / "test_api_rbac.py"
    spec = importlib.util.spec_from_file_location("test_api_rbac_harness", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_api_rbac_harness"] = mod
    spec.loader.exec_module(mod)
    return mod, sys.modules["api_security_cases"]


harness, sec = _load_harness()

CTX = {"api_base": "https://abc.execute-api.us-west-2.amazonaws.com/api"}


class _Recorder:
    """Captures record(...) calls, using the harness's REAL `_record`.

    Deliberately not a reimplementation. The previous fake restated the record
    shape, and in doing so it hardcoded `"passed": bool(passed)` — so it pinned
    SKIP-as-a-pass in the fake regardless of what the real `_record` did, and every
    "a missing precondition still passes" assertion below was asserting the fake's
    behaviour. Delegating means the outcome semantics under test are the ones the
    harness actually ships.
    """

    def __init__(self):
        self.rows = []

    def __call__(self, results, *args, **kwargs):
        before = len(results)
        harness._record(results, *args, **kwargs)
        self.rows.extend(results[before:])

    def by_principal(self, needle):
        return [r for r in self.rows if needle in r["principal"]]

    def outcomes(self, needle):
        return [r["outcome"] for r in self.by_principal(needle)]


def _scripted_call(script):
    """Return a fake `call(api_base, field, args, token)` that pops (status, et,
    in_band, request_id) tuples from a per-(field) queue in `script`."""

    def _call(api_base, field, args, token):
        key = field
        q = script.get(key)
        if not q:
            return 200, None, None, "rid"
        return q.pop(0)

    return _call


# --------------------------------------------------------------------------- #
# IDOR (2.1) — content-based (marker must not appear in User B's response)
# --------------------------------------------------------------------------- #
def _body_call(per_token_bodies):
    """Fake call_body(api_base, field, args, token) -> (status, body). Bodies are
    keyed by (field, token) so User A and User B can get different responses for
    the same jobId."""

    def _cb(api_base, field, args, token):
        return per_token_bodies.get((field, token), (200, "{}"))

    return _cb


def test_idor_no_leak_when_userb_response_lacks_marker():
    rec = _Recorder()
    results = []
    marker_holder = {}

    def seed(job_id, marker):
        marker_holder["m"] = marker
        return "admin@example.invalid"  # owner uid

    # We can't know the marker before seed() runs, so build the body-call to
    # reference marker_holder lazily.
    def call_body(api_base, field, args, token):
        m = marker_holder.get("m", "")
        if token == "b":  # nosec B105 - "b" is a fake test token id, not a credential  # noqa: E501
            return 500, '{"errors":[{"message":"not found for this user"}]}'
        if token == "a" and field == "getAgentJobStatus":  # nosec B105 - "a" is a fake test token id, not a credential  # noqa: E501
            return 200, f'{{"result":"{m}","status":"COMPLETED"}}'
        return 200, "{}"

    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "userB": "b"},
        seed_fn=seed,
        call_body=call_body,
    )
    assert rec.by_principal("userB(reads")[0]["passed"] is True  # no disclosure
    assert rec.by_principal("userA(reads own")[0]["passed"] is True  # owner OK


def test_idor_inconclusive_when_owner_cannot_read_seed():
    # If the OWNER can't read the seeded job either (marker absent for A), that's
    # a seed-keying mismatch, not a security failure -> inconclusive SKIP, never a
    # hard fail.
    rec = _Recorder()
    results = []

    def seed(job_id, marker):
        return "admin@example.invalid"

    def call_body(api_base, field, args, token):
        return 500, '{"errors":[{"message":"not found for this user"}]}'  # nobody reads

    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "userB": "b"},
        seed_fn=seed,
        call_body=call_body,
    )
    incon = [r for r in rec.rows if "inconclusive" in r["principal"]]
    assert incon and incon[0]["http_status"] == "SKIP"
    # A SKIP is NOT a pass: an absent precondition is not a satisfied assertion.
    assert incon[0]["outcome"] == "SKIP"
    assert incon[0]["passed"] is False
    # Step 1's row is RETRACTED, not merely accompanied by a note. It used to stay
    # a PASS beside the SKIP, so a seed-keying mismatch still reported "User B must
    # not receive A's data" as proven.
    step1 = rec.by_principal("userB(reads")[0]
    assert step1["outcome"] == "SKIP", "step 1 was left standing as a pass"
    assert "retracted" in step1["detail"]
    # Nothing here is a hard failure: a keying mismatch is not a security finding.
    assert not [r for r in rec.rows if r["outcome"] in ("FAIL", "ERROR")]


def test_idor_leak_when_userb_response_contains_marker():
    rec = _Recorder()
    results = []
    marker_holder = {}

    def seed(job_id, marker):
        marker_holder["m"] = marker
        return "admin@example.invalid"

    def call_body(api_base, field, args, token):
        m = marker_holder.get("m", "")
        # BROKEN backend: User B's response leaks A's marker.
        return 200, f'{{"result":"{m}"}}'

    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "userB": "b"},
        seed_fn=seed,
        call_body=call_body,
    )
    assert rec.by_principal("userB(reads")[0]["passed"] is False  # LEAK detected


def test_idor_skips_without_preconditions():
    rec = _Recorder()
    results = []
    # No second user.
    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a"},
        seed_fn=lambda j, m: "o",
        call_body=_body_call({}),
    )
    assert rec.rows[-1]["http_status"] == "SKIP"
    assert rec.rows[-1]["outcome"] == "SKIP" and rec.rows[-1]["passed"] is False


def test_idor_skips_when_seed_fails():
    rec = _Recorder()
    results = []
    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "userB": "b"},
        seed_fn=lambda j, m: None,  # cannot seed
        call_body=_body_call({}),
    )
    assert any(
        r["http_status"] == "SKIP" and "idor-seed" in r["principal"] for r in rec.rows
    )


# --------------------------------------------------------------------------- #
# Token lifecycle (2.3 / 2.4)
# --------------------------------------------------------------------------- #
def test_expired_token_rejected():
    rec = _Recorder()
    results = []
    script = {"listDocuments": [(401, None, None, "r")]}
    sec.run_token_lifecycle_suite(
        CTX,
        _scripted_call(script),
        rec,
        results,
        expired_token="expired",  # nosec B106 - fake test token literal, not a credential  # noqa: E501
        logout_token=None,
        logout_email=None,
        sign_out_fn=None,
    )
    exp = rec.by_principal("token:expired")[0]
    assert exp["passed"] is True and "SEC-2.3" in exp["detail"]


def test_logout_still_accepted_is_warn_not_fail():
    # Stateless JWT: token still works after global sign-out -> passed=False but
    # tagged with a known_gap so it's a WARN, not a hard fail.
    rec = _Recorder()
    results = []
    signed_out = {}
    script = {
        "listDocuments": [
            (200, None, None, "before"),  # works before logout
            (200, None, None, "after"),  # STILL works after logout
        ]
    }
    sec.run_token_lifecycle_suite(
        CTX,
        _scripted_call(script),
        rec,
        results,
        expired_token=None,
        logout_token="t",  # nosec B106 - fake test token literal, not a credential
        logout_email="u@x.invalid",
        sign_out_fn=lambda e: signed_out.setdefault("called", e),
    )
    assert signed_out["called"] == "u@x.invalid"
    row = rec.by_principal("token:post-logout")[0]
    assert row["passed"] is False
    assert row["known_gap"] == "GAP-SEC-LOGOUT"  # WARN, not hard fail


def test_logout_revoked_is_pass_no_gap():
    rec = _Recorder()
    results = []
    script = {
        "listDocuments": [(200, None, None, "before"), (401, None, None, "after")]
    }
    sec.run_token_lifecycle_suite(
        CTX,
        _scripted_call(script),
        rec,
        results,
        expired_token=None,
        logout_token="t",  # nosec B106 - fake test token literal, not a credential
        logout_email="u@x.invalid",
        sign_out_fn=lambda e: None,
    )
    row = rec.by_principal("token:post-logout")[0]
    assert row["passed"] is True and row["known_gap"] is None


# --------------------------------------------------------------------------- #
# Deleted resource (2.5) — list-membership oracle (getConfigVersion 200s for any
# name, so we assert the version is listed before delete and absent after).
# --------------------------------------------------------------------------- #
def _deleted_resource_setup(listed_before, listed_after):
    """Build (call, call_body) fakes: `call` handles create/delete (status-only),
    `call_body` answers getConfigVersions with a list whose membership flips."""
    state = {"listed": listed_before}
    version_holder = {}

    def call(api_base, field, args, token):
        if field == "updateConfiguration":
            version_holder["v"] = args["versionName"]
            return 200, None, None, "c"
        return 200, None, None, "x"

    def call_body(api_base, field, args, token):
        if field == "deleteConfigVersion":
            state["listed"] = listed_after
            return 200, '{"success": true}'
        if field == "getConfigVersions":
            v = version_holder.get("v", "")
            body = f'{{"versions": ["{v}"]}}' if state["listed"] else '{"versions": []}'
            return 200, body
        return 200, "{}"

    return call, call_body


def test_deleted_resource_gone_passes():
    rec = _Recorder()
    results = []
    call, call_body = _deleted_resource_setup(listed_before=True, listed_after=False)
    sec.run_deleted_resource_suite(
        CTX, call, rec, results, {"Admin": "a"}, call_body=call_body
    )
    row = rec.by_principal("after-delete")[0]
    assert row["passed"] is True and "SEC-2.5" in row["detail"]


def test_deleted_resource_still_listed_fails():
    rec = _Recorder()
    results = []
    # Still enumerable after delete -> leak.
    call, call_body = _deleted_resource_setup(listed_before=True, listed_after=True)
    sec.run_deleted_resource_suite(
        CTX, call, rec, results, {"Admin": "a"}, call_body=call_body
    )
    assert rec.by_principal("after-delete")[0]["passed"] is False


def test_deleted_resource_skips_without_body_call():
    rec = _Recorder()
    results = []
    sec.run_deleted_resource_suite(
        CTX, _scripted_call({}), rec, results, {"Admin": "a"}, call_body=None
    )
    assert rec.rows[-1]["http_status"] == "SKIP"
    assert rec.rows[-1]["outcome"] == "SKIP" and rec.rows[-1]["passed"] is False


# --------------------------------------------------------------------------- #
# Input validation (3) — tolerant vs strict
# --------------------------------------------------------------------------- #
def test_input_validation_tolerant_500_is_warn_not_hardfail():
    # Pre-PR-B: a 5xx on malformed input is a documented weakness (WARN via
    # known_gap), not an unqualified pass and not a hard fail, in tolerant mode.
    rec = _Recorder()
    results = []
    call = lambda ab, f, a, t: (500, None, None, "r")  # noqa: E731
    sec.run_input_validation_suite(
        CTX, call, rec, results, {"Admin": "a"}, strict=False
    )
    rows = [r for r in rec.rows if r["op"] != "input-validation"]
    assert rows, "expected malformed-input cases to be recorded"
    assert all(not r["passed"] for r in rows)  # not an unqualified pass
    assert all(r["known_gap"] == "GAP-SEC-INPUT" for r in rows)  # WARN, not hard fail


def test_input_validation_tolerant_silent_200_is_warn_not_hardfail():
    # The real current-stack behavior: malformed input silently accepted (200).
    # Pre-PR-B this must be a WARN (documented gap), so PR A doesn't break the
    # CI gate against a stack without central validation.
    rec = _Recorder()
    results = []
    call = lambda ab, f, a, t: (200, None, None, "r")  # noqa: E731
    sec.run_input_validation_suite(
        CTX, call, rec, results, {"Admin": "a"}, strict=False
    )
    rows = [r for r in rec.rows if r["op"] != "input-validation"]
    assert rows and all(not r["passed"] for r in rows)
    assert all(r["known_gap"] == "GAP-SEC-INPUT" for r in rows)  # WARN, not hard fail


def test_input_validation_strict_requires_clean_4xx():
    # In strict mode both a 500 and a silent 200 are HARD failures (no gap).
    for status in (500, 200):
        rec2 = _Recorder()
        results2 = []
        call = lambda ab, f, a, t, s=status: (s, None, None, "r")  # noqa: E731
        sec.run_input_validation_suite(
            CTX, call, rec2, results2, {"Admin": "a"}, strict=True
        )
        rows = [r for r in rec2.rows if r["op"] != "input-validation"]
        assert rows and all(not r["passed"] for r in rows)
        assert all(r["known_gap"] is None for r in rows)  # hard fail, not WARN


def test_input_validation_clean_400_passes_both_modes():
    for strict in (False, True):
        rec = _Recorder()
        results = []
        call = lambda ab, f, a, t: (400, "BadRequest", None, "r")  # noqa: E731
        sec.run_input_validation_suite(
            CTX, call, rec, results, {"Admin": "a"}, strict=strict
        )
        rows = [r for r in rec.rows if r["op"] != "input-validation"]
        assert rows and all(r["passed"] for r in rows)


# --------------------------------------------------------------------------- #
# TLS (4) — helper logic (no network; monkeypatch the socket layer)
# --------------------------------------------------------------------------- #
def _probe_stub(outcomes):
    """A `_tls_probe` double keyed by TLS version, so one stub cannot answer both
    "TLS1.0 must be refused" and "TLS1.2 must be accepted" with the same value."""

    def _probe(host, port, version):
        return outcomes[version]

    return _probe


_WEAK = (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1)


def test_tls_suite_records_all_expected_checks(monkeypatch):
    monkeypatch.setattr(
        sec,
        "_tls_probe",
        _probe_stub(
            {
                ssl.TLSVersion.TLSv1: (sec.REFUSED, "handshake failed"),
                ssl.TLSVersion.TLSv1_1: (sec.REFUSED, "handshake failed"),
                ssl.TLSVersion.TLSv1_2: (sec.ACCEPTED, "negotiated TLSv1.2"),
            }
        ),
    )
    monkeypatch.setattr(
        sec, "_http_probe", lambda h: (sec.REFUSED, "no cleartext service")
    )
    rec = _Recorder()
    results = []
    sec.run_tls_suite(CTX, rec, results)
    labels = {r["principal"] for r in rec.rows}
    assert {"TLS1.0", "TLS1.1", "TLS1.2", "plaintext-http"} <= labels
    assert not [r for r in rec.rows if r["outcome"] in ("FAIL", "ERROR")]
    assert all("SEC-4-TLS" in r["detail"] for r in rec.rows)


def test_tls_weak_protocol_accepted_fails(monkeypatch):
    monkeypatch.setattr(
        sec,
        "_tls_probe",
        _probe_stub(
            {
                ssl.TLSVersion.TLSv1: (sec.ACCEPTED, "negotiated TLSv1"),
                ssl.TLSVersion.TLSv1_1: (sec.ACCEPTED, "negotiated TLSv1.1"),
                ssl.TLSVersion.TLSv1_2: (sec.ACCEPTED, "negotiated TLSv1.2"),
            }
        ),
    )
    monkeypatch.setattr(sec, "_http_probe", lambda h: (sec.REFUSED, "no service"))
    rec = _Recorder()
    results = []
    sec.run_tls_suite(CTX, rec, results)
    weak = [r for r in rec.rows if r["principal"] in ("TLS1.0", "TLS1.1")]
    assert weak and all(r["outcome"] == "FAIL" for r in weak)


# --------------------------------------------------------------------------- #
# A protocol probe that never reached the endpoint is not a refusal
#
# `_tls_refused` folded every `OSError` into "the server declined this protocol",
# and `socket.gaierror` (DNS), `ConnectionRefusedError` and `socket.timeout` are
# all `OSError` subclasses. So a host that had gone away reported
# "TLS1.0 refused ✅ / TLS1.1 refused ✅" — two passes from zero observations of the
# endpoint. `_tls_probe` now TCP-connects separately from the handshake, which is
# what makes a handshake failure mean anything.
# --------------------------------------------------------------------------- #
def test_an_unreachable_host_is_inconclusive_not_a_refusal(monkeypatch):
    def _no_connect(host, port):
        return None, f"could not reach {host}:{port} (gaierror: name resolution)"

    monkeypatch.setattr(sec, "_connect", _no_connect)

    outcome, note = sec._tls_probe("gone.example.invalid", 443, ssl.TLSVersion.TLSv1)

    assert outcome == sec.INCONCLUSIVE, (
        "an unreachable host was read as 'the server refused TLS 1.0'"
    )
    assert "could not reach" in note


def test_a_handshake_failure_on_a_reachable_port_is_a_refusal(monkeypatch):
    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(sec, "_connect", lambda h, p: (_Sock(), None))

    def _boom(sock, server_hostname=None):
        raise ssl.SSLError("no protocols available")

    monkeypatch.setattr(
        sec.ssl.SSLContext, "wrap_socket", lambda self, sock, **kw: _boom(sock)
    )

    outcome, _ = sec._tls_probe("host.example.invalid", 443, ssl.TLSVersion.TLSv1_2)

    assert outcome == sec.REFUSED


def test_an_unreachable_host_makes_the_tls_suite_inconclusive_not_green(monkeypatch):
    """The whole suite, end to end: a dead endpoint must not report 3 of 4 passes."""
    monkeypatch.setattr(
        sec, "_connect", lambda h, p: (None, "could not reach (gaierror)")
    )
    monkeypatch.setattr(
        sec, "_http_probe", lambda h: (sec.INCONCLUSIVE, "host did not resolve")
    )
    rec = _Recorder()
    results = []

    sec.run_tls_suite(CTX, rec, results)

    assert not [r for r in rec.rows if r["outcome"] == "PASS"], (
        "a dead endpoint produced passing TLS checks"
    )
    assert all(r["outcome"] == "ERROR" for r in rec.rows)


def test_a_client_that_cannot_offer_the_protocol_is_inconclusive():
    """The local OpenSSL declining to offer TLS 1.0 says nothing about the server.

    It used to be recorded as "refused" — a pass asserted from the build of OpenSSL
    the harness happens to run on.
    """
    outcome, note = sec._tls_probe("host.example.invalid", 443, ssl.TLSVersion.TLSv1)

    # On a modern OpenSSL this is the client-side path; on an older one the stub-free
    # call would try to connect. Only assert when we are on the path under test.
    if "client cannot offer" in note:
        assert outcome == sec.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# 2.1 Caller-supplied resource reference (getStepFunctionExecution)
# --------------------------------------------------------------------------- #
LIVE_ARN = (
    "arn:aws:states:us-west-2:123456789012:execution:"
    "IDP-PATTERNSTACK-ABC-DocumentProcessingWorkflow:11111111-2222-3333-4444-555555555555"
)


def _ref_call(outcomes):
    """Fake `call` keyed by the executionArn argument.

    `outcomes` maps a predicate name to (status, errorType, in_band, rid):
      "foreign"  -> the ARN naming another state machine
      "own"      -> this deployment's own ARN, Admin token
      "scoped"   -> this deployment's own ARN, scoped token
    """

    def _call(api_base, field, args, token):
        arn = args.get("executionArn", "")
        if "-sibling-deployment" in arn:
            return outcomes["foreign"]
        if token == "scoped-token":  # nosec B105 - fake token label in a test double, not a credential
            return outcomes["scoped"]
        return outcomes["own"]

    return _call


def _ref_tokens():
    return {"Admin": "admin-token", "scoped": "scoped-token"}


def test_caller_ref_all_bounded_passes():
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(),
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (403, "Unauthorized", None, "r1"),
            "own": (200, None, None, "r2"),
            "scoped": (403, "Unauthorized", None, "r3"),
        }),
    )
    assert not [r for r in rec.rows if r["outcome"] in ("FAIL", "ERROR")], rec.rows
    principals = {r["principal"] for r in rec.rows}
    assert principals == {"foreign-state-machine", "own-state-machine",
                          "scoped(out-of-scope)"}
    assert all(sec.SEC_OBJREF in r["detail"] for r in rec.rows)


def test_caller_ref_foreign_arn_served_fails():
    """The finding this suite exists to catch: another state machine's execution
    is returned instead of refused."""
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(),
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (200, None, None, "r1"),
            "own": (200, None, None, "r2"),
            "scoped": (403, "Unauthorized", None, "r3"),
        }),
    )
    foreign = rec.by_principal("foreign-state-machine")
    assert len(foreign) == 1 and not foreign[0]["passed"]


def test_caller_ref_own_arn_refused_fails():
    """Refusing everything must not look like success."""
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(),
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (403, "Unauthorized", None, "r1"),
            "own": (403, "Unauthorized", None, "r2"),
            "scoped": (403, "Unauthorized", None, "r3"),
        }),
    )
    own = rec.by_principal("own-state-machine")
    assert len(own) == 1 and not own[0]["passed"]


def test_caller_ref_out_of_scope_served_fails():
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(),
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (403, "Unauthorized", None, "r1"),
            "own": (200, None, None, "r2"),
            "scoped": (200, None, None, "r3"),
        }),
    )
    scoped = rec.by_principal("scoped(out-of-scope)")
    assert len(scoped) == 1 and not scoped[0]["passed"]


def test_caller_ref_in_band_denial_counts_as_refused():
    """A config-scope denial can arrive in-band with HTTP 200."""
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(),
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (403, "Unauthorized", None, "r1"),
            "own": (200, None, None, "r2"),
            "scoped": (200, None, "Unauthorized", "r3"),
        }),
    )
    assert not [r for r in rec.rows if r["outcome"] in ("FAIL", "ERROR")], rec.rows


def test_caller_ref_skips_without_a_live_arn():
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(), live_execution_arn=None, call=_ref_call({})
    )
    assert len(rec.rows) == 1
    assert rec.rows[0]["http_status"] == "SKIP"
    assert rec.rows[0]["outcome"] == "SKIP" and rec.rows[0]["passed"] is False


def test_caller_ref_skips_scope_check_without_a_scoped_user():
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, {"Admin": "admin-token"},
        live_execution_arn=LIVE_ARN,
        call=_ref_call({
            "foreign": (403, "Unauthorized", None, "r1"),
            "own": (200, None, None, "r2"),
            "scoped": (200, None, None, "r3"),
        }),
    )
    scoped = rec.by_principal("out-of-scope")
    assert len(scoped) == 1 and scoped[0]["http_status"] == "SKIP"
    assert not [r for r in rec.rows if r["outcome"] in ("FAIL", "ERROR")]


# --------------------------------------------------------------------------- #
# Every arm that asks "was this refused?" or "was this NOT refused?"
#
# Each of these was satisfied by a request that never completed. The
# caller-supplied-ref suite's "own execution must still be served" arm is the same
# shape as the RBAC matrix's positive arm — `not _denied(...)` is true of a 500, an
# empty body, and a connection error — and it is the arm that exists to prove the
# control does not OVER-deny, which only a real response can show.
# --------------------------------------------------------------------------- #
_DEAD = (0, None, None, "<request error: connection reset>")


def test_an_expired_token_check_that_did_not_complete_is_an_error():
    rec = _Recorder()
    results = []

    sec.run_token_lifecycle_suite(
        CTX,
        lambda *a, **k: _DEAD,
        rec,
        results,
        expired_token="expired",  # nosec B106 - fake token id, not a credential
        logout_token=None,
        logout_email=None,
        sign_out_fn=None,
    )

    expired = rec.by_principal("token:expired")[0]
    assert expired["outcome"] == "ERROR"
    assert expired["passed"] is False


def test_a_caller_ref_suite_that_cannot_reach_the_api_records_no_passes():
    rec = _Recorder()
    results = []

    sec.run_caller_supplied_ref_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "scoped": "s"},
        live_execution_arn=LIVE_ARN,
        call=lambda *a, **k: _DEAD,
    )

    assert rec.rows, "the suite recorded nothing"
    assert not [r for r in rec.rows if r["outcome"] == "PASS"], (
        "an unreachable API produced passing caller-supplied-reference checks"
    )
    assert all(r["outcome"] == "ERROR" for r in rec.rows)


def test_a_500_does_not_prove_the_deployments_own_execution_is_served():
    """The positive arm. `not _denied(500, "InternalError")` is True."""
    rec = _Recorder()
    results = []

    def _call(api_base, field, args, token):
        if args["executionArn"] == LIVE_ARN and token == "a":  # nosec B105 - fake token id
            return 500, "InternalError", None, "rid"
        return 403, "Unauthorized", None, "rid"

    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, {"Admin": "a", "scoped": "s"}, LIVE_ARN, _call
    )

    own = rec.by_principal("own-state-machine")[0]
    assert own["outcome"] == "ERROR"
    assert own["passed"] is False


def test_a_healthy_caller_ref_suite_still_passes():
    """The control: without it, "never passes" would satisfy the three above."""
    rec = _Recorder()
    results = []

    def _call(api_base, field, args, token):
        if args["executionArn"] == LIVE_ARN and token == "a":  # nosec B105 - fake token id
            return 200, None, None, "rid"
        return 403, "Unauthorized", None, "rid"

    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, {"Admin": "a", "scoped": "s"}, LIVE_ARN, _call
    )

    assert rec.rows and all(r["outcome"] == "PASS" for r in rec.rows)


def test_a_deleted_resource_check_that_could_not_list_is_an_error():
    """"Not in the list" read out of a response that never arrived would report the
    resource correctly deleted."""
    rec = _Recorder()
    results = []

    def _call(api_base, field, args, token):
        return 200, None, None, "rid"

    def _call_body(api_base, field, args, token):
        return 0, "<request error: connection reset>"

    sec.run_deleted_resource_suite(
        CTX, _call, rec, results, {"Admin": "a"}, call_body=_call_body
    )

    after = rec.by_principal("after-delete")
    assert after, "the suite recorded no assertion"
    assert after[0]["outcome"] == "ERROR"
    assert after[0]["passed"] is False


def test_a_failed_sign_out_is_classified_an_error_not_a_skip():
    """The logout suite's own setup step. If `sign_out_fn` raises, the revocation check
    was never run — and an `outcome="ERROR"` that nothing asserts can be dropped
    without a single test noticing."""
    rec = _Recorder()
    results = []

    def _boom(email):
        raise RuntimeError("cognito global sign-out failed")

    sec.run_token_lifecycle_suite(
        CTX,
        lambda *a, **k: (200, None, None, "rid"),
        rec,
        results,
        expired_token=None,
        logout_token="tok",  # nosec B106 - fake token id, not a credential
        logout_email="u@example.invalid",
        sign_out_fn=_boom,
    )

    row = rec.by_principal("token:post-logout")[0]
    assert row["outcome"] == "ERROR", (
        "a sign-out that failed was recorded as a skip, so the revocation check "
        "silently did not run"
    )
    assert row["passed"] is False
    assert row["known_gap"] is None


def test_a_post_logout_check_that_did_not_complete_does_not_borrow_the_logout_gap():
    """GAP-SEC-LOGOUT documents a token that is STILL ACCEPTED after sign-out.

    A request that never completed is not that observation, so it must not be
    recorded against the gap and become a warning about revocation.
    """
    rec = _Recorder()
    results = []

    sec.run_token_lifecycle_suite(
        CTX,
        lambda *a, **k: _DEAD,
        rec,
        results,
        expired_token=None,
        logout_token="tok",  # nosec B106 - fake token id, not a credential
        logout_email="u@example.invalid",
        sign_out_fn=lambda email: None,
    )

    row = rec.by_principal("token:post-logout")[0]
    assert row["outcome"] == "ERROR"
    assert row["known_gap"] is None
    assert row["passed"] is False


def test_a_still_accepted_post_logout_token_is_still_the_documented_warning():
    """The control: the gap must still apply to the observation it describes."""
    rec = _Recorder()
    results = []

    sec.run_token_lifecycle_suite(
        CTX,
        lambda *a, **k: (200, None, None, "rid"),
        rec,
        results,
        expired_token=None,
        logout_token="tok",  # nosec B106 - fake token id, not a credential
        logout_email="u@example.invalid",
        sign_out_fn=lambda email: None,
    )

    row = rec.by_principal("token:post-logout")[0]
    assert row["known_gap"] == "GAP-SEC-LOGOUT"
    assert row["outcome"] == "WARN"


def test_a_delete_that_worked_but_a_listing_that_did_not_is_still_an_error():
    """The narrow case `_version_listed`'s own guard exists for.

    If only the LIST calls fail, the delete status is a clean 200 and the
    substring test answers "not listed" from a body that never arrived — which reads
    as the resource correctly gone.
    """
    rec = _Recorder()
    results = []

    def _call(api_base, field, args, token):
        return 200, None, None, "rid"

    def _call_body(api_base, field, args, token):
        if field == "getConfigVersions":
            return 0, "<request error: connection reset>"
        return 200, "{}"

    sec.run_deleted_resource_suite(
        CTX, _call, rec, results, {"Admin": "a"}, call_body=_call_body
    )

    after = rec.by_principal("after-delete")
    assert after, "the suite recorded no assertion"
    assert after[0]["outcome"] == "ERROR", (
        "a listing that never arrived was read as 'the version is gone'"
    )


# --------------------------------------------------------------------------- #
# The `outcome=` argument is the load-bearing part at every site
#
# `_record` computes `"passed": resolved == "PASS"` and ignores its `passed`
# argument whenever `outcome=` is supplied, so a `(not why) and ...` prefix on the
# boolean is belt-and-braces: removing it changes nothing. Asserting only
# `passed is False` therefore proves nothing about the CLASSIFICATION, and these
# tests exist to cover the `outcome=` argument at the three sites in this file that
# build one by hand.
#
# The deleted-resource SETUP site is the one that actually weakens the gate if its
# classification is lost. It reads `outcome="ERROR" if why else "SKIP"` with `passed`
# hardcoded False, so there is no boolean fallback at all: collapse it to `"SKIP"`
# and a setup call that TIMED OUT becomes a non-blocking skip, the run exits 0, and
# nothing anywhere records that the check could not be run.
# --------------------------------------------------------------------------- #
def test_a_setup_call_that_timed_out_blocks_the_gate_rather_than_skipping():
    """The distinction this site has to make, and the reason it is a hard case.

    A clean 4xx from the setup call means the precondition is genuinely absent here
    (SKIP, exit 0). A timeout means the harness could not find out (ERROR, exit
    non-zero). Collapsing the two hands an unreachable stack a green run.
    """
    rec = _Recorder()
    results = []

    sec.run_deleted_resource_suite(
        CTX,
        lambda *a, **k: (0, None, None, "<request error: connection reset>"),
        rec,
        results,
        {"Admin": "a"},
        call_body=lambda *a, **k: (0, "<request error>"),
    )

    setup = rec.by_principal("deleted-resource-setup")
    assert setup, "the setup outcome was not recorded at all"
    assert setup[0]["outcome"] == "ERROR", (
        "a setup call that never completed was recorded as a skip, so the run "
        "exits 0 with nothing saying the check could not be run"
    )
    assert setup[0]["inconclusive"] is True
    assert "INCONCLUSIVE" in setup[0]["detail"]


def test_a_setup_call_refused_with_a_clean_4xx_is_still_a_skip():
    """The other half of the same distinction — and the control for the test above,
    which "always ERROR" would otherwise satisfy."""
    rec = _Recorder()
    results = []

    sec.run_deleted_resource_suite(
        CTX,
        lambda *a, **k: (400, "BadRequest", None, "rid"),
        rec,
        results,
        {"Admin": "a"},
        call_body=lambda *a, **k: (200, "{}"),
    )

    setup = rec.by_principal("deleted-resource-setup")
    assert setup and setup[0]["outcome"] == "SKIP"
    assert setup[0]["inconclusive"] is False
    assert "precondition absent" in setup[0]["detail"]


def test_the_idor_probe_row_is_classified_inconclusive_not_merely_not_passed():
    rec = _Recorder()
    results = []

    sec.run_idor_suite(
        CTX,
        rec,
        results,
        {"Admin": "a", "userB": "b"},
        seed_fn=lambda job_id, marker: "admin@example.invalid",
        call_body=lambda *a, **k: (0, "<request error: connection reset>"),
    )

    row = rec.by_principal("userB(reads")[0]
    assert row["outcome"] == "ERROR"
    assert row["inconclusive"] is True
    assert len(rec.rows) == 1, (
        "the suite must abandon after an incomplete probe rather than go on to "
        "assert things about the owner's read"
    )


def test_an_input_validation_probe_that_did_not_complete_is_classified_an_error():
    rec = _Recorder()
    results = []

    sec.run_input_validation_suite(
        CTX,
        lambda *a, **k: (0, None, None, "<request error>"),
        rec,
        results,
        {"Admin": "a"},
    )

    rows = [r for r in rec.rows if r["op"] != "input-validation"]
    assert rows, "no malformed-input cases were driven"
    for r in rows:
        assert r["outcome"] == "ERROR"
        assert r["inconclusive"] is True
        # It must NOT borrow GAP-SEC-INPUT: that gap documents a resolver that
        # mishandled a bad shape, which an unreachable endpoint does not show.
        assert r["known_gap"] is None


def test_an_input_validation_probe_that_answered_is_still_judged_on_its_status():
    """The control. A 5xx here IS a real observation — the resolver blew up on the
    bad shape, which is the documented weakness the suite surfaces — so it stays a
    GAP-SEC-INPUT warning rather than becoming inconclusive."""
    rec = _Recorder()
    results = []

    sec.run_input_validation_suite(
        CTX,
        lambda *a, **k: (500, "InternalError", None, "rid"),
        rec,
        results,
        {"Admin": "a"},
    )

    rows = [r for r in rec.rows if r["op"] != "input-validation"]
    assert rows
    for r in rows:
        assert r["known_gap"] == "GAP-SEC-INPUT"
        assert r["outcome"] == "WARN"
        assert r["inconclusive"] is False


# --------------------------------------------------------------------------- #
# The probe tails, in both directions
#
# `_tls_probe`'s OSError arm and `_http_probe`'s tail are the two places left where
# a single `except` decides between "the endpoint refused this" and "nothing came
# back". Each is tested here for BOTH answers, because a test that only covers the
# refusal side leaves the arm free to be flipped back to REFUSED silently.
# --------------------------------------------------------------------------- #
class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reachable(monkeypatch):
    monkeypatch.setattr(sec, "_connect", lambda h, p: (_Sock(), None))


def _handshake_raises(monkeypatch, exc):
    def _boom(self, sock, **kw):
        raise exc

    monkeypatch.setattr(sec.ssl.SSLContext, "wrap_socket", _boom)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ssl.SSLError("no protocols available"), "refused"),
        (ConnectionResetError("reset by peer"), "refused"),
        # Reached the port, then lost it. Not a statement about the protocol.
        (TimeoutError("timed out"), "inconclusive"),
        (BrokenPipeError("broken pipe"), "inconclusive"),
    ],
)
def test_the_tls_handshake_arms_separate_a_refusal_from_a_lost_connection(
    monkeypatch, exc, expected
):
    _reachable(monkeypatch)
    _handshake_raises(monkeypatch, exc)

    outcome, note = sec._tls_probe("host.example.invalid", 443, ssl.TLSVersion.TLSv1_2)

    assert outcome == expected, note


def test_a_handshake_that_times_out_does_not_pass_the_weak_protocol_check(monkeypatch):
    """End to end: the suite must not report TLS 1.0/1.1 refused on that evidence."""
    _reachable(monkeypatch)
    _handshake_raises(monkeypatch, TimeoutError("timed out"))
    monkeypatch.setattr(
        sec, "_http_probe", lambda h: (sec.REFUSED, "no cleartext service")
    )
    rec = _Recorder()
    results = []

    sec.run_tls_suite(CTX, rec, results)

    weak = [r for r in rec.rows if r["principal"] in ("TLS1.0", "TLS1.1")]
    assert weak and all(r["outcome"] == "ERROR" for r in weak)


def _http_raises(monkeypatch, exc):
    def _open(req, timeout=None):
        raise exc

    monkeypatch.setattr(sec.urllib.request, "urlopen", _open)


@pytest.mark.parametrize(
    "exc,expected",
    [
        # A reset IS the observation "nothing serves this port" — execute-api's
        # actual behaviour on :80.
        (ConnectionRefusedError("refused"), "refused"),
        (urllib.error.URLError(ConnectionRefusedError("refused")), "refused"),
        # A blackholed egress, common on a restricted network: the packet went
        # nowhere and nothing came back, so the port's state is unknown.
        (urllib.error.URLError(TimeoutError("timed out")), "inconclusive"),
        (TimeoutError("timed out"), "inconclusive"),
        (urllib.error.URLError(socket.gaierror("name resolution")), "inconclusive"),
        (socket.gaierror("name resolution"), "inconclusive"),
        # Unrecognised: not evidence either way, so do not guess reassuringly.
        (OSError("network is unreachable"), "inconclusive"),
    ],
)
def test_the_http_probe_tail_separates_a_closed_port_from_no_answer(
    monkeypatch, exc, expected
):
    _http_raises(monkeypatch, exc)

    outcome, note = sec._http_probe("host.example.invalid")

    assert outcome == expected, note


def test_a_blackholed_port_80_does_not_pass_the_cleartext_check(monkeypatch):
    """The finding, end to end: `plaintext HTTP refused ✅` off a connect timeout."""
    _http_raises(monkeypatch, urllib.error.URLError(TimeoutError("timed out")))
    monkeypatch.setattr(
        sec,
        "_tls_probe",
        _probe_stub(
            {
                ssl.TLSVersion.TLSv1: (sec.REFUSED, "handshake failed"),
                ssl.TLSVersion.TLSv1_1: (sec.REFUSED, "handshake failed"),
                ssl.TLSVersion.TLSv1_2: (sec.ACCEPTED, "negotiated TLSv1.2"),
            }
        ),
    )
    rec = _Recorder()
    results = []

    sec.run_tls_suite(CTX, rec, results)

    http = rec.by_principal("plaintext-http")[0]
    assert http["outcome"] == "ERROR", (
        "a connect timeout on :80 was recorded as 'plaintext HTTP not served'"
    )
    assert http["inconclusive"] is True


def test_a_genuinely_closed_port_80_still_passes(monkeypatch):
    """The control for the test above."""
    _http_raises(monkeypatch, ConnectionRefusedError("refused"))

    outcome, _ = sec._http_probe("host.example.invalid")

    assert outcome == sec.REFUSED


# --------------------------------------------------------------------------- #
# Inbox IDOR (2.1) — User B cannot read, mark or delete User A's Inbox messages
# --------------------------------------------------------------------------- #
INBOX_OWNER = "test-rbac-admin@example.invalid"


class _FakeInboxApi:
    """An in-memory stand-in for the Inbox operations behind ``call_body``.

    Token ``"a"`` is the owner (Admin) and ``"b"`` is User B. By default it
    behaves like the real resolver: every key comes from the caller, so User B
    sees an empty partition, gets 404 for the owner's ids and 400 for a bad
    cursor or an unknown argument. Each knob breaks one of those properties.
    """

    def __init__(self, **knobs):
        self.messages = {}  # id -> {"title": str, "read": bool}
        self.knobs = knobs
        self.calls = []
        self.cleaned = []

    # -- harness hooks ------------------------------------------------------
    def seed(self, notification_id, marker):
        if self.knobs.get("seed_raises"):
            raise RuntimeError("put-item failed")
        if self.knobs.get("no_table"):
            return None
        self.messages[notification_id] = {"title": marker, "read": False}
        return INBOX_OWNER

    def cleanup(self, notification_id):
        self.cleaned.append(notification_id)
        self.messages.pop(notification_id, None)

    # -- the API --------------------------------------------------------------
    def _items(self):
        return [
            {"id": i, "title": m["title"], "read": m["read"]}
            for i, m in self.messages.items()
        ]

    def _page(self, items):
        unread = sum(1 for i in items if not i["read"])
        return 200, json.dumps(
            {"items": items, "nextToken": None, "unreadCount": unread}
        )

    def __call__(self, api_base, field, args, token):
        self.calls.append((field, token, dict(args)))
        override = self.knobs.get("status", {}).get((field, token, _case(args)))
        if override is not None:
            return override
        owner = token == "a"  # nosec B105 - fake test token id, not a credential
        if field == "listInboxMessages":
            if "userId" in args and not self.knobs.get("accept_user_id"):
                return 400, '{"errors":[{"errorType":"BadRequest"}]}'
            if "nextToken" in args:
                try:
                    sec.notification_keys().decode_cursor(args["nextToken"])
                except ValueError:
                    if not self.knobs.get("accept_bad_cursor"):
                        return 400, '{"errors":[{"errorType":"BadRequest"}]}'
                if not owner and self.knobs.get("cursor_leak"):
                    return self._page(self._items())
                return self._page([])
            if owner:
                if self.knobs.get("owner_hidden"):
                    return self._page([])
                return self._page(self._items())
            return self._page(self._items() if self.knobs.get("list_leak") else [])
        if field == "markInboxMessages":
            if owner or self.knobs.get("mark_leak"):
                for i in args["ids"]:
                    if i in self.messages:
                        self.messages[i]["read"] = args["read"]
                return 200, '{"updated":1,"notFoundIds":[],"unreadCount":0}'
            return 404, '{"errors":[{"errorType":"ResourceNotFound"}]}'
        if field == "deleteInboxMessages":
            if owner or self.knobs.get("delete_leak"):
                for i in args["ids"]:
                    self.messages.pop(i, None)
                return 200, '{"updated":1,"notFoundIds":[],"unreadCount":0}'
            return 404, '{"errors":[{"errorType":"ResourceNotFound"}]}'
        return 200, "{}"


def _case(args):
    """A short key naming which variant of a call this is, for status overrides."""
    if "userId" in args:
        return "userId"
    if "nextToken" in args:
        return "cursor"
    return "plain"


def _run_inbox(api, tokens=None):
    rec = _Recorder()
    results = []
    sec.run_inbox_idor_suite(
        CTX,
        rec,
        results,
        tokens if tokens is not None else {"Admin": "a", "userB": "b"},
        seed_fn=api.seed,
        call_body=api,
        cleanup_fn=api.cleanup,
    )
    return rec, results


INBOX_PRINCIPALS = [
    "inbox-owner(positive control)",
    "userB(lists inbox)",
    "userB(marks A's message)",
    "inbox-owner(still unread)",
    "userB(deletes A's message)",
    "inbox-owner(still exists)",
    "userB(cursor at A's id)",
    "userB(invalid nextToken)",
    "userB(unknown userId arg)",
]


def _outcome_of(rec, principal):
    rows = [r for r in rec.rows if r["principal"] == principal]
    assert len(rows) == 1, f"expected one row for {principal}, got {rows}"
    return rows[0]["outcome"]


class TestInboxIdorSuite:
    def test_a_resolver_that_scopes_by_caller_passes_every_check(self):
        api = _FakeInboxApi()

        rec, results = _run_inbox(api)

        assert [r["principal"] for r in rec.rows] == INBOX_PRINCIPALS
        assert {r["outcome"] for r in rec.rows} == {"PASS"}
        assert all(r["passed"] is True for r in rec.rows)
        assert harness.hard_failures(results) == []

    def test_the_seeded_item_is_cleaned_up_after_the_run(self):
        api = _FakeInboxApi()

        _run_inbox(api)

        assert len(api.cleaned) == 1
        assert api.messages == {}

    def test_the_probes_target_the_seeded_id_and_a_cursor_naming_it(self):
        api = _FakeInboxApi()

        _run_inbox(api)

        seeded_id = api.cleaned[0]
        b_calls = [c for c in api.calls if c[1] == "b"]
        mark = next(c for c in b_calls if c[0] == "markInboxMessages")
        assert mark[2] == {"ids": [seeded_id], "read": True}
        cursor_call = next(
            c for c in b_calls if c[2].get("nextToken") not in (None, "not-a-valid-cursor")
        )
        decoded = sec.notification_keys().decode_cursor(cursor_call[2]["nextToken"])
        assert decoded == seeded_id

    def test_user_b_seeing_the_owner_message_in_their_list_is_a_fail(self):
        api = _FakeInboxApi(list_leak=True)

        rec, results = _run_inbox(api)

        assert _outcome_of(rec, "userB(lists inbox)") == "FAIL"
        assert "PRESENT (LEAK)" in next(
            r["detail"] for r in rec.rows if r["principal"] == "userB(lists inbox)"
        )
        assert "ERROR" not in {r["outcome"] for r in rec.rows}
        assert harness.hard_failures(results)

    def test_user_b_marking_the_owner_message_is_a_fail_twice(self):
        api = _FakeInboxApi(mark_leak=True)

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(marks A's message)") == "FAIL"
        assert _outcome_of(rec, "inbox-owner(still unread)") == "FAIL"

    def test_user_b_deleting_the_owner_message_is_a_fail_twice(self):
        api = _FakeInboxApi(delete_leak=True)

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(deletes A's message)") == "FAIL"
        assert _outcome_of(rec, "inbox-owner(still exists)") == "FAIL"

    def test_a_cursor_that_reaches_the_owner_partition_is_a_fail(self):
        api = _FakeInboxApi(cursor_leak=True)

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(cursor at A's id)") == "FAIL"
        assert _outcome_of(rec, "userB(lists inbox)") == "PASS"

    def test_an_accepted_invalid_cursor_is_a_fail(self):
        api = _FakeInboxApi(accept_bad_cursor=True)

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(invalid nextToken)") == "FAIL"

    def test_an_accepted_user_id_argument_is_a_fail(self):
        api = _FakeInboxApi(accept_user_id=True)

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(unknown userId arg)") == "FAIL"

    def test_a_failed_positive_control_is_an_error_and_runs_no_leak_check(self):
        api = _FakeInboxApi(owner_hidden=True)

        rec, results = _run_inbox(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert rec.rows[0]["principal"] == "inbox-owner(positive control)"
        assert rec.rows[0]["passed"] is False
        assert not [c for c in api.calls if c[1] == "b"], (
            "User B was probed although the owner could not see the seed"
        )
        assert harness.hard_failures(results) == rec.rows
        assert len(api.cleaned) == 1

    def test_a_missing_table_output_is_a_skip_not_a_pass(self):
        api = _FakeInboxApi(no_table=True)

        rec, results = _run_inbox(api)

        assert [r["outcome"] for r in rec.rows] == ["SKIP"]
        assert rec.rows[0]["passed"] is False
        assert "NotificationsTableName" in rec.rows[0]["detail"]
        assert api.calls == []
        assert harness.hard_failures(results) == []

    def test_a_seed_that_raises_is_an_error(self):
        api = _FakeInboxApi(seed_raises=True)

        rec, results = _run_inbox(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert harness.hard_failures(results) == rec.rows

    def test_missing_preconditions_skip(self):
        api = _FakeInboxApi()

        rec, _ = _run_inbox(api, tokens={"Admin": "a"})

        assert [r["outcome"] for r in rec.rows] == ["SKIP"]
        assert rec.rows[0]["passed"] is False
        assert api.calls == []
        assert api.cleaned == []

    def test_a_mutation_probe_that_did_not_complete_is_an_error_not_a_pass(self):
        api = _FakeInboxApi(
            status={
                ("markInboxMessages", "b", "plain"): (0, "<request error: timeout>"),
                ("deleteInboxMessages", "b", "plain"): (502, "Bad Gateway"),
            }
        )

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(marks A's message)") == "ERROR"
        assert _outcome_of(rec, "userB(deletes A's message)") == "ERROR"
        assert _outcome_of(rec, "inbox-owner(still unread)") == "PASS"

    def test_a_list_probe_that_did_not_complete_is_an_error(self):
        api = _FakeInboxApi(
            status={("listInboxMessages", "b", "plain"): (0, "<request error: x>")}
        )

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(lists inbox)") == "ERROR"

    def test_a_refused_user_b_list_is_a_fail_not_a_pass(self):
        # An over-denial is not non-disclosure: User B's own Inbox must be served.
        api = _FakeInboxApi(
            status={("listInboxMessages", "b", "plain"): (403, '{"errors":[]}')}
        )

        rec, _ = _run_inbox(api)

        assert _outcome_of(rec, "userB(lists inbox)") == "FAIL"

    def test_the_four_outcomes_stay_distinct(self):
        observed = {}
        for name, knobs in {
            "pass": {},
            "fail": {"list_leak": True},
            "error": {"owner_hidden": True},
            "skip": {"no_table": True},
        }.items():
            rec, results = _run_inbox(_FakeInboxApi(**knobs))
            outcomes = {r["outcome"] for r in rec.rows}
            observed[name] = outcomes
            blocking = bool(harness.hard_failures(results))
            assert blocking is (name in ("fail", "error")), name
            for row in rec.rows:
                assert row["passed"] is (row["outcome"] == "PASS")

        assert observed["pass"] == {"PASS"}
        assert "FAIL" in observed["fail"] and "ERROR" not in observed["fail"]
        assert observed["error"] == {"ERROR"}
        assert observed["skip"] == {"SKIP"}


class _FakeAwsCli:
    """Records ``aws(...)`` calls made by the harness's Inbox seeding helpers."""

    def __init__(self, table_output):
        self.table_output = table_output
        self.calls = []

    def __call__(self, *args, region=None):
        self.calls.append(args)
        if args[:2] == ("cloudformation", "describe-stacks"):
            return self.table_output
        return None

    def items_written(self):
        return [
            json.loads(a[a.index("--item") + 1])
            for a in self.calls
            if a[:2] == ("dynamodb", "put-item")
        ]


class TestInboxSeeding:
    def _ctx(self):
        return {"stack": "IDP1", "region": "us-west-2"}

    def test_a_stack_without_the_output_seeds_nothing(self, monkeypatch):
        fake = _FakeAwsCli("None")
        monkeypatch.setattr(harness, "aws", fake)

        owner = harness.seed_inbox_message(self._ctx(), "0" * 25, "m")

        assert owner is None
        assert fake.items_written() == []

    def test_the_seeded_item_uses_the_service_layout(self, monkeypatch):
        fake = _FakeAwsCli("IDP1-NotificationsTable-ABC")
        monkeypatch.setattr(harness, "aws", fake)
        keys = sec.notification_keys()
        nid = keys.new_notification_id()
        ctx = self._ctx()

        owner = harness.seed_inbox_message(ctx, nid, "MARK")

        assert owner == "test-rbac-admin@example.invalid"
        (item,) = fake.items_written()
        assert item["PK"] == {"S": keys.partition_key(owner)}
        assert item["SK"] == {"S": keys.message_sk(nid)}
        assert item["UnreadPK"] == item["PK"]
        assert item["Title"] == {"S": "MARK"}
        assert int(item["ExpiresAfter"]["N"]) > 0
        assert "ReadAt" not in item

    def test_cleanup_deletes_exactly_the_seeded_key(self, monkeypatch):
        fake = _FakeAwsCli("IDP1-NotificationsTable-ABC")
        monkeypatch.setattr(harness, "aws", fake)
        keys = sec.notification_keys()
        nid = keys.new_notification_id()
        ctx = self._ctx()
        harness.seed_inbox_message(ctx, nid, "MARK")

        harness.cleanup_seeded_inbox_messages(ctx)

        deletes = [a for a in fake.calls if a[:2] == ("dynamodb", "delete-item")]
        assert len(deletes) == 1
        key = json.loads(deletes[0][deletes[0].index("--key") + 1])
        assert key == {
            "PK": {"S": keys.partition_key("test-rbac-admin@example.invalid")},
            "SK": {"S": keys.message_sk(nid)},
        }


class TestInboxIdorSuiteEdges:
    def test_an_unloadable_key_module_is_an_error_and_seeds_nothing(self, monkeypatch):
        def broken():
            raise ImportError("No module named 'idp_common'")

        monkeypatch.setattr(sec, "notification_keys", broken)
        api = _FakeInboxApi()

        rec, results = _run_inbox(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert rec.rows[0]["principal"] == "inbox-idor-seed"
        assert "ImportError" in rec.rows[0]["detail"]
        assert api.messages == {} and api.calls == []
        assert harness.hard_failures(results) == rec.rows

    def test_a_cleanup_that_raises_does_not_discard_the_results(self):
        api = _FakeInboxApi()

        def cleanup(notification_id):
            raise RuntimeError("delete-item throttled")

        rec = _Recorder()
        results = []
        sec.run_inbox_idor_suite(
            CTX,
            rec,
            results,
            {"Admin": "a", "userB": "b"},
            seed_fn=api.seed,
            call_body=api,
            cleanup_fn=cleanup,
        )

        assert [r["principal"] for r in rec.rows] == INBOX_PRINCIPALS
        assert {r["outcome"] for r in rec.rows} == {"PASS"}

    @pytest.mark.parametrize(
        ("response", "reason"),
        [
            ((403, '{"errors":[]}'), "owner list answered 403"),
            ((200, '{"unexpected": true}'), "owner list body has no items array"),
        ],
    )
    def test_an_owner_list_that_cannot_show_the_seed_fails_the_control(
        self, response, reason
    ):
        api = _FakeInboxApi(status={("listInboxMessages", "a", "plain"): response})

        rec, _ = _run_inbox(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert reason in rec.rows[0]["detail"]
        assert not [c for c in api.calls if c[1] == "b"]


# --------------------------------------------------------------------------- #
# IDOR (2.1) — review queues: only the claimer acts on a claimed item
# --------------------------------------------------------------------------- #
REVIEW_KEY = "batch-1/claim.pdf"
REVIEW_CYCLE = "cycle-1"
_FORBIDDEN = (403, '{"errors":[{"errorType":"Forbidden"}]}')


class _FakeReviewApi:
    """An in-memory stand-in for the review-queue operations behind ``call_body``.

    Token ``"A"`` holds the claim, ``"B"`` is a fellow queue member and ``"C"`` is
    not a member. By default it behaves like the real resolver; ``status``
    overrides one ``(field, token)`` response to break one property.
    """

    def __init__(self, **knobs):
        self.knobs = knobs
        self.calls = []
        self.cleaned = []

    def seed(self):
        if self.knobs.get("seed_raises"):
            raise RuntimeError("put-item failed")
        if self.knobs.get("no_table"):
            return None
        return {
            "tokens": {"A": "A", "B": "B", "C": "C"},
            "object_key": REVIEW_KEY,
            "cycle_id": REVIEW_CYCLE,
        }

    def cleanup(self, seeded):
        if self.knobs.get("cleanup_raises"):
            raise RuntimeError("cleanup failed")
        self.cleaned.append(seeded)

    def __call__(self, api_base, field, args, token):
        self.calls.append((field, token, dict(args)))
        override = self.knobs.get("status", {}).get((field, token))
        if override is not None:
            return override
        if field == "getReviewItem":
            if token == "C":  # nosec B105 - fake test token id, not a credential
                return _FORBIDDEN
            return 200, json.dumps({"objectKey": args["objectKey"]})
        if field == "listMyReviewWork":
            mine = [{"objectKey": REVIEW_KEY}] if token == "A" else []  # nosec B105
            return 200, json.dumps({"items": mine, "nextToken": None})
        # Every other operation acts on the claim, which only A holds, and A is
        # never asked to: B and C are refused.
        return _FORBIDDEN


def _run_review(api, seed=True, call=True):
    rec = _Recorder()
    results = []
    sec.run_review_queue_idor_suite(
        CTX,
        rec,
        results,
        seed_fn=api.seed if seed else None,
        call_body=api if call else None,
        cleanup_fn=api.cleanup,
    )
    return rec, results


REVIEW_ROWS = [
    ("getReviewItem", "review-claimer(positive control)"),
    ("getReviewItem", "member-B(preview)"),
    ("renewReviewLease", "member-B(acts on A's claim)"),
    ("releaseReviewItem", "member-B(acts on A's claim)"),
    ("saveReviewDraft", "member-B(acts on A's claim)"),
    ("completeReviewItem", "member-B(acts on A's claim)"),
    ("listMyReviewWork", "member-B(My Work)"),
    ("getReviewItem", "non-member-C"),
    ("claimReviewItem", "non-member-C"),
]


def _review_outcome(rec, op, principal):
    rows = [r for r in rec.rows if r["op"] == op and r["principal"] == principal]
    assert len(rows) == 1, f"expected one row for {op}/{principal}, got {rows}"
    return rows[0]["outcome"]


class TestReviewQueueIdorSuite:
    def test_a_resolver_that_enforces_the_claim_passes_every_check(self):
        api = _FakeReviewApi()

        rec, results = _run_review(api)

        assert [(r["op"], r["principal"]) for r in rec.rows] == REVIEW_ROWS
        assert {r["outcome"] for r in rec.rows} == {"PASS"}
        assert harness.hard_failures(results) == []
        assert api.cleaned == [api.seed()]

    def test_the_probes_name_the_seeded_item_with_each_callers_token(self):
        api = _FakeReviewApi()

        _run_review(api)

        item_args = {"objectKey": REVIEW_KEY, "cycleId": REVIEW_CYCLE}
        b_acts = {f: a for f, t, a in api.calls if t == "B" and f != "getReviewItem"}
        assert b_acts["renewReviewLease"] == item_args
        assert b_acts["releaseReviewItem"] == item_args
        assert b_acts["completeReviewItem"] == item_args
        assert b_acts["saveReviewDraft"] == {
            **item_args,
            "sectionId": "1",
            "data": "{}",
        }
        c_calls = {f: a for f, t, a in api.calls if t == "C"}
        assert c_calls == {
            "getReviewItem": {"objectKey": REVIEW_KEY},
            "claimReviewItem": item_args,
        }

    @pytest.mark.parametrize(
        "field",
        [
            "renewReviewLease",
            "releaseReviewItem",
            "saveReviewDraft",
            "completeReviewItem",
        ],
    )
    def test_a_member_acting_on_another_members_claim_is_a_fail(self, field):
        api = _FakeReviewApi(status={(field, "B"): (200, "{}")})

        rec, results = _run_review(api)

        assert _review_outcome(rec, field, "member-B(acts on A's claim)") == "FAIL"
        others = [r for r in rec.rows if r["op"] != field]
        assert {r["outcome"] for r in others} == {"PASS"}
        assert harness.hard_failures(results)

    @pytest.mark.parametrize(
        ("op", "token", "principal"),
        [
            ("releaseReviewItem", "B", "member-B(acts on A's claim)"),
            ("claimReviewItem", "C", "non-member-C"),
        ],
    )
    def test_a_refusal_with_any_status_but_403_is_a_fail(self, op, token, principal):
        """The suite asserts the authorization refusal itself: a 404 or a 400
        may come from a validation step that ran before the ownership check, so
        it does not show the claim was enforced."""
        api = _FakeReviewApi(status={(op, token): (404, '{"errors":[]}')})

        rec, _ = _run_review(api)

        assert _review_outcome(rec, op, principal) == "FAIL"

    def test_a_member_refused_a_preview_is_a_fail(self):
        api = _FakeReviewApi(status={("getReviewItem", "B"): _FORBIDDEN})

        rec, _ = _run_review(api)

        assert _review_outcome(rec, "getReviewItem", "member-B(preview)") == "FAIL"

    def test_the_claim_appearing_in_another_members_work_is_a_fail(self):
        leaked = (200, json.dumps({"items": [{"objectKey": REVIEW_KEY}]}))
        api = _FakeReviewApi(status={("listMyReviewWork", "B"): leaked})

        rec, _ = _run_review(api)

        assert _review_outcome(rec, "listMyReviewWork", "member-B(My Work)") == "FAIL"

    def test_a_non_member_served_the_item_is_a_fail(self):
        api = _FakeReviewApi(status={("getReviewItem", "C"): (200, "{}")})

        rec, _ = _run_review(api)

        assert _review_outcome(rec, "getReviewItem", "non-member-C") == "FAIL"
        assert _review_outcome(rec, "claimReviewItem", "non-member-C") == "PASS"

    def test_a_refusal_that_echoes_the_item_key_is_a_fail(self):
        echo = (403, json.dumps({"errors": [{"message": f"{REVIEW_KEY} is claimed"}]}))
        api = _FakeReviewApi(status={("claimReviewItem", "C"): echo})

        rec, _ = _run_review(api)

        assert _review_outcome(rec, "claimReviewItem", "non-member-C") == "FAIL"

    def test_a_member_act_that_did_not_complete_is_an_error_not_a_pass(self):
        api = _FakeReviewApi(
            status={
                ("renewReviewLease", "B"): (0, "<request error: timeout>"),
                ("listMyReviewWork", "B"): (502, "Bad Gateway"),
                ("getReviewItem", "B"): (0, "<request error: reset>"),
            }
        )

        rec, results = _run_review(api)

        acts = "member-B(acts on A's claim)"
        assert _review_outcome(rec, "renewReviewLease", acts) == "ERROR"
        assert _review_outcome(rec, "releaseReviewItem", acts) == "PASS"
        assert _review_outcome(rec, "listMyReviewWork", "member-B(My Work)") == "ERROR"
        assert _review_outcome(rec, "getReviewItem", "member-B(preview)") == "ERROR"
        assert harness.hard_failures(results)

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            ({("listMyReviewWork", "A"): (200, '{"items": []}')}, "item listed: False"),
            ({("listMyReviewWork", "A"): (200, "not json")}, "item listed: False"),
            ({("getReviewItem", "A"): (0, "<request error: x>")}, "did not complete"),
            ({("getReviewItem", "A"): _FORBIDDEN}, "claimer preview 403"),
        ],
        ids=["not-in-my-work", "unreadable-my-work", "timeout", "claimer-refused"],
    )
    def test_a_failed_positive_control_is_an_error_and_runs_no_leak_check(
        self, status, reason
    ):
        api = _FakeReviewApi(status=status)

        rec, results = _run_review(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert rec.rows[0]["principal"] == "review-claimer(positive control)"
        assert reason in rec.rows[0]["detail"]
        assert not [c for c in api.calls if c[1] != "A"]
        assert harness.hard_failures(results) == rec.rows
        assert len(api.cleaned) == 1

    def test_a_missing_table_output_is_a_skip_and_cleans_nothing(self):
        api = _FakeReviewApi(no_table=True)

        rec, results = _run_review(api)

        assert [r["outcome"] for r in rec.rows] == ["SKIP"]
        assert "HITLTableName" in rec.rows[0]["detail"]
        assert api.calls == [] and api.cleaned == []
        assert harness.hard_failures(results) == []

    def test_a_seed_that_raises_is_an_error(self):
        api = _FakeReviewApi(seed_raises=True)

        rec, results = _run_review(api)

        assert [r["outcome"] for r in rec.rows] == ["ERROR"]
        assert rec.rows[0]["principal"] == "review-idor-seed"
        assert "RuntimeError" in rec.rows[0]["detail"]
        assert api.calls == [] and api.cleaned == []
        assert harness.hard_failures(results) == rec.rows

    @pytest.mark.parametrize(("seed", "call"), [(False, True), (True, False)])
    def test_missing_preconditions_skip(self, seed, call):
        api = _FakeReviewApi()

        rec, results = _run_review(api, seed=seed, call=call)

        assert [r["outcome"] for r in rec.rows] == ["SKIP"]
        assert rec.rows[0]["principal"] == "review-idor"
        assert api.calls == [] and api.cleaned == []
        assert harness.hard_failures(results) == []

    def test_a_cleanup_that_raises_does_not_discard_the_results(self):
        api = _FakeReviewApi(cleanup_raises=True)

        rec, results = _run_review(api)

        assert [(r["op"], r["principal"]) for r in rec.rows] == REVIEW_ROWS
        assert harness.hard_failures(results) == []


class TestReviewPageKeys:
    def test_object_keys_are_read_and_non_dict_items_ignored(self):
        body = json.dumps({"items": [{"objectKey": "a"}, "junk", {"objectKey": "b"}]})

        assert sec._review_page_keys(body) == ["a", "b"]

    @pytest.mark.parametrize("body", [None, "", "not json", "[]", '{"items": {}}'])
    def test_an_unreadable_page_is_none(self, body):
        assert sec._review_page_keys(body) is None
