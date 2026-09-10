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
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_sec():
    # scripts/sdlc/tests/ -> scripts/api_security_cases.py
    path = Path(__file__).resolve().parents[2] / "api_security_cases.py"
    spec = importlib.util.spec_from_file_location("api_security_cases", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sec = _load_sec()

CTX = {"api_base": "https://abc.execute-api.us-west-2.amazonaws.com/api"}


class _Recorder:
    """Captures _record(...) calls the way the harness stores them."""

    def __init__(self):
        self.rows = []

    def __call__(
        self,
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
    ):
        row = {
            "op": op,
            "principal": principal,
            "http_status": status,
            "passed": bool(passed),
            "detail": detail,
            "known_gap": gap,
        }
        self.rows.append(row)
        results.append(row)

    def by_principal(self, needle):
        return [r for r in self.rows if needle in r["principal"]]


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
    assert incon and incon[0]["http_status"] == "SKIP" and incon[0]["passed"] is True
    # And there is NO hard-fail row (nothing recorded as passed=False).
    assert all(r["passed"] for r in rec.rows)


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
    assert rec.rows[-1]["http_status"] == "SKIP" and rec.rows[-1]["passed"] is True


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
    assert rec.rows[-1]["http_status"] == "SKIP" and rec.rows[-1]["passed"] is True


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
def test_tls_suite_records_all_expected_checks(monkeypatch):
    # Force the low-level probes to deterministic outcomes.
    monkeypatch.setattr(sec, "_tls_refused", lambda h, p, v: (True, "handshake failed"))
    monkeypatch.setattr(
        sec, "_tls_accepted", lambda h, p, v: (True, "negotiated TLSv1.2")
    )
    monkeypatch.setattr(sec, "_http_refused", lambda h: (True, "no cleartext service"))
    rec = _Recorder()
    results = []
    sec.run_tls_suite(CTX, rec, results)
    labels = {r["principal"] for r in rec.rows}
    assert {"TLS1.0", "TLS1.1", "TLS1.2", "plaintext-http"} <= labels
    assert all(r["passed"] for r in rec.rows)
    assert all("SEC-4-TLS" in r["detail"] for r in rec.rows)


def test_tls_weak_protocol_accepted_fails(monkeypatch):
    monkeypatch.setattr(sec, "_tls_refused", lambda h, p, v: (False, "ACCEPTED"))
    monkeypatch.setattr(sec, "_tls_accepted", lambda h, p, v: (True, "ok"))
    monkeypatch.setattr(sec, "_http_refused", lambda h: (True, "no service"))
    rec = _Recorder()
    results = []
    sec.run_tls_suite(CTX, rec, results)
    weak = [r for r in rec.rows if r["principal"] in ("TLS1.0", "TLS1.1")]
    assert weak and all(not r["passed"] for r in weak)  # weak TLS accepted = fail


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
    assert all(r["passed"] for r in rec.rows), rec.rows
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
    assert all(r["passed"] for r in rec.rows), rec.rows


def test_caller_ref_skips_without_a_live_arn():
    rec, results = _Recorder(), []
    sec.run_caller_supplied_ref_suite(
        CTX, rec, results, _ref_tokens(), live_execution_arn=None, call=_ref_call({})
    )
    assert len(rec.rows) == 1
    assert rec.rows[0]["http_status"] == "SKIP" and rec.rows[0]["passed"]


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
    assert all(r["passed"] for r in rec.rows)
