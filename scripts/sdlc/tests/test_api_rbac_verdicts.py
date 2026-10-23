# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How `make api-test` turns a request outcome into a verdict.

The defect these tests exist for: the dynamic RBAC harness scored a request that
established nothing as a PASS. Its positive arms asserted only "not denied", so any
status that was not 401/403 passed — a 504 gateway timeout, a 500, an empty body, a
body that failed to parse. A regression that made every operation hang would have
produced a fully green run, and the last published snapshot
(`security/test-results/0.6.9/rbac-dynamic.md`) contains 43 cells reading `500 ✅`
beneath a header claiming `Gate (hard failures): PASS ✅`.

The property under test is the one the harness exists to provide: **a refusal and
an inconclusive result are different outcomes**. "The call was refused as expected"
is a pass; "the test could not be run" never is.

No AWS and no network: `call`'s transport is stubbed at `urllib.request.urlopen`,
and the matrix is driven with a fake `call`.
"""

import base64
import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_harness():
    path = Path(__file__).resolve().parents[2] / "test_api_rbac.py"
    spec = importlib.util.spec_from_file_location("_rbac_harness_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rbac_harness_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_harness()

API = "https://abc.execute-api.us-west-2.amazonaws.com/api"


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _transport(monkeypatch, outcome):
    """Make `urllib.request.urlopen` produce `outcome` (a response or an exception)."""

    def _open(req, timeout=None):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", _open)


# ---------------------------------------------------------------------------
# call(): the two sentinels that make an inconclusive result visible
# ---------------------------------------------------------------------------
class TestCallReportsWhatItCouldNotEstablish:
    def test_a_timeout_returns_status_zero_instead_of_escaping(self, monkeypatch):
        """It used to escape as a URLError and abort the whole matrix."""
        _transport(monkeypatch, TimeoutError("timed out"))

        status, et, ib, rid = h.call(API, "listDocuments", {}, "tok")

        assert status == 0
        assert "request error" in rid

    def test_a_connection_error_returns_status_zero(self, monkeypatch):
        _transport(monkeypatch, urllib.error.URLError("connection refused"))

        status, *_ = h.call(API, "listDocuments", {}, "tok")

        assert status == 0

    def test_the_request_carries_an_explicit_timeout(self, monkeypatch):
        """Without one, urlopen inherits the default socket timeout — which is
        None — so a hung endpoint hangs the harness rather than being reported."""
        seen = {}

        def _open(req, timeout=None):
            seen["timeout"] = timeout
            return _Resp(200, "{}")

        monkeypatch.setattr(urllib.request, "urlopen", _open)

        h.call(API, "listDocuments", {}, "tok")

        assert seen["timeout"] == h.REQUEST_TIMEOUT_SECONDS
        assert seen["timeout"] is not None

    def test_an_empty_body_is_marked_unreadable(self, monkeypatch):
        _transport(monkeypatch, _Resp(200, b""))

        status, et, *_ = h.call(API, "listDocuments", {}, "tok")

        assert (status, et) == (200, h.UNREADABLE_BODY)

    def test_a_malformed_json_body_is_marked_unreadable(self, monkeypatch):
        _transport(monkeypatch, _Resp(200, "<html>gateway error</html>"))

        status, et, *_ = h.call(API, "listDocuments", {}, "tok")

        assert (status, et) == (200, h.UNREADABLE_BODY)

    def test_an_errors_list_of_plain_strings_does_not_abort_the_run(self, monkeypatch):
        """``errors`` is not always the dispatcher's GraphQL-shaped envelope.

        ``abortWorkflow``'s resolver returns 200 with ``errors`` as its own list of
        plain STRINGS, one per object key it could not abort. Reading ``.get`` off
        that element raises ``AttributeError``, which is not caught by the narrowed
        ``(ValueError, TypeError)`` handler around ``json.loads`` — so it escapes
        ``call()`` and takes the whole 118-operation run down with no report at all.

        That is exactly what happened: the run died after 14 operations, exit 2. The
        previous revision survived it only because this block sat under a bare
        ``except Exception: pass``, which also swallowed real parse failures — so the
        remedy is to check the element type, not to widen the handler back.
        """
        _transport(
            monkeypatch,
            _Resp(200, json.dumps({"errors": ["could not abort a", "or b"]})),
        )

        status, et, in_band, _ = h.call(API, "abortWorkflow", {}, "tok")

        assert (status, et, in_band) == (200, None, None)

    def test_a_good_body_is_not_marked(self, monkeypatch):
        _transport(monkeypatch, _Resp(200, '{"Documents": []}'))

        status, et, ib, _ = h.call(API, "listDocuments", {}, "tok")

        assert (status, et, ib) == (200, None, None)

    def test_a_denial_body_still_reads_its_error_type(self, monkeypatch):
        _transport(
            monkeypatch,
            _Resp(403, '{"errors":[{"errorType":"Unauthorized","message":"no"}]}'),
        )

        status, et, *_ = h.call(API, "listDocuments", {}, "tok")

        assert (status, et) == (403, "Unauthorized")

    def test_a_json_list_body_is_not_unreadable(self, monkeypatch):
        """getChatMessages returns a bare JSON array; that is a readable body."""
        _transport(monkeypatch, _Resp(200, '[{"role":"user"}]'))

        status, et, *_ = h.call(API, "getChatMessages", {}, "tok")

        assert (status, et) == (200, None)


# ---------------------------------------------------------------------------
# inconclusive(): what does and does not count
# ---------------------------------------------------------------------------
class TestInconclusive:
    @pytest.mark.parametrize(
        "status,et",
        [
            (0, None),
            (200, "__unreadable_body__"),
            (500, None),
            (502, None),
            (504, None),
        ],
    )
    def test_these_establish_nothing(self, status, et):
        assert h.inconclusive(status, et) is not None

    @pytest.mark.parametrize(
        "status,et",
        [
            (200, None),
            (400, "BadRequest"),
            (401, None),
            (403, "Unauthorized"),
            (404, "NotFound"),
        ],
    )
    def test_these_are_real_observations(self, status, et):
        assert h.inconclusive(status, et) is None


# ---------------------------------------------------------------------------
# classify(): an inconclusive cell is never a pass, in ANY cell
# ---------------------------------------------------------------------------
ALLOWED = {"Admin", "Author"}


class TestClassifyNeverPassesAnInconclusiveCell:
    @pytest.mark.parametrize(
        "role,allowed",
        [
            ("Admin", ALLOWED),  # allowed role: "not denied" used to pass
            ("Viewer", ALLOWED),  # denied role: "LEAK" is also not the right label
            ("Admin", h.ANY),  # ANY-auth
            ("Admin", h.IAM),  # IAM-only
        ],
    )
    @pytest.mark.parametrize(
        "status,et",
        [(0, None), (500, None), (504, None), (200, "__unreadable_body__")],
    )
    def test_no_cell_passes_on_an_inconclusive_result(self, role, allowed, status, et):
        verdict = h.classify(role, allowed, status, et, None)

        assert verdict.passed is False
        assert verdict.outcome == "ERROR"
        assert "INCONCLUSIVE" in verdict.detail

    def test_a_504_is_not_read_as_the_allowed_role_succeeding(self):
        """The exact defect: a gateway timeout on an allowed-role cell."""
        verdict = h.classify("Admin", ALLOWED, 504, None, None)

        assert verdict.outcome == "ERROR"
        assert verdict.passed is False

    def test_a_real_refusal_of_a_disallowed_role_is_still_a_pass(self):
        verdict = h.classify("Viewer", ALLOWED, 403, "Unauthorized", None)

        assert (verdict.passed, verdict.outcome) == (True, "PASS")

    def test_a_real_success_for_an_allowed_role_is_still_a_pass(self):
        verdict = h.classify("Admin", ALLOWED, 200, None, None)

        assert (verdict.passed, verdict.outcome) == (True, "PASS")

    def test_a_genuine_leak_is_still_a_fail_not_an_error(self):
        verdict = h.classify("Viewer", ALLOWED, 200, None, None)

        assert (verdict.passed, verdict.outcome) == (False, "FAIL")
        assert "LEAK" in verdict.detail

    def test_a_5xx_is_registered_against_a_named_gap(self):
        """So a resolver that raises a bare Exception for a validation refusal
        surfaces as a visible warning rather than red-lining the gate for a
        backlog — while a timeout, which is nobody's known condition, does not."""
        assert h.classify("Admin", ALLOWED, 500, None, None).gap == (
            h.GAP_INCONCLUSIVE_5XX
        )
        assert h.classify("Admin", ALLOWED, 0, None, None).gap is None


# ---------------------------------------------------------------------------
# Which 404 means "skip this operation" and which means "the object is gone"
# ---------------------------------------------------------------------------
class TestFeatureAbsent404:
    """``_is_feature_absent_404`` decides whether a 404 skips a whole row.

    The dispatcher answers 404 for two unrelated conditions and only one of them
    means "this deployment lacks the feature". Getting this backwards is silent
    and expensive in one direction: a genuine not-found read as "feature absent"
    turns that operation's authorization assertions into SKIPs, and a SKIP is not
    a pass but is also not a failure, so nothing goes red and the row simply
    stops being measured.

    Covered offline because the function is otherwise only reached by
    ``make api-test``, which needs a live stack — so an inverted comparison here
    would ship.
    """

    def test_a_resolver_sourced_404_is_not_a_missing_feature(self):
        assert h._is_feature_absent_404(404, "ResourceNotFound") is False

    def test_an_unroutable_operation_404_is_a_missing_feature(self):
        assert h._is_feature_absent_404(404, "NotFound") is True

    def test_an_untyped_404_keeps_the_pre_existing_skip_behaviour(self):
        """A 404 with no errorType predates the distinction and still skips."""
        assert h._is_feature_absent_404(404, None) is True

    def test_no_other_status_is_a_missing_feature(self):
        for status in (200, 400, 403, 500):
            assert h._is_feature_absent_404(status, "NotFound") is False, status


# ---------------------------------------------------------------------------
# _record / exit code: SKIP is not a pass, ERROR blocks
# ---------------------------------------------------------------------------
class TestOutcomesReachTheExitCode:
    def test_a_skip_is_not_counted_as_a_pass(self):
        results = []
        h._record(results, "op", "*", "SKIP", False, "no precondition", outcome="SKIP")

        assert results[0]["outcome"] == "SKIP"
        assert results[0]["passed"] is False
        assert h.hard_failures(results) == [], "a skip must not fail the gate either"

    def test_an_error_is_a_hard_failure(self):
        results = []
        h._record(results, "op", "Admin", 0, False, "INCONCLUSIVE: x", outcome="ERROR")

        assert h.hard_failures(results), (
            "a check that could not be run left the gate green"
        )

    def test_a_gap_downgrades_an_error_to_a_warning_but_not_to_a_pass(self):
        results = []
        h._record(
            results,
            "op",
            "Admin",
            500,
            False,
            "INCONCLUSIVE: server error 500",
            gap=h.GAP_INCONCLUSIVE_5XX,
            outcome="ERROR",
        )

        row = results[0]
        assert row["outcome"] == "WARN"
        assert row["passed"] is False
        assert row["inconclusive"] is True, (
            "registering a gap decides whether to block; it is not a claim that "
            "the check ran"
        )
        assert h.hard_failures(results) == []

    def test_a_gap_cannot_turn_a_failure_into_a_pass(self):
        results = []
        h._record(results, "op", "Viewer", 200, False, "LEAK", gap="GAP-01")

        assert results[0]["passed"] is False
        assert results[0]["outcome"] == "WARN"

    def test_the_report_and_the_exit_code_agree(self):
        results = []
        h._record(results, "a", "Admin", 200, True, "ok")
        h._record(results, "b", "Viewer", 200, False, "LEAK")
        h._record(results, "c", "Admin", 0, False, "INCONCLUSIVE", outcome="ERROR")
        h._record(results, "d", "*", "SKIP", False, "skipped", outcome="SKIP")

        parts = h._partition(results)

        assert len(parts["pass"]) == 1
        assert len(parts["fail"]) == 1
        assert len(parts["error"]) == 1
        assert len(parts["skip"]) == 1
        assert len(h.hard_failures(results)) == 2


# ---------------------------------------------------------------------------
# End to end: inject a timeout into the harness and assert the run is NOT green
# ---------------------------------------------------------------------------
OPS = {
    "listDocuments": {"groups": ["Admin", "Author"], "args": {}},
    "getConfigVersions": {"groups": "ANY", "args": {}},
    "updateAgentJobStatus": {"groups": "IAM_ONLY", "args": {}},
}
# Every principal the matrix drives, not just the group roles: the groupless
# caller is one of them (#1065), and a token map built from ROLES alone would
# make the matrix raise rather than drive it.
TOKENS = {r: f"tok-{r}" for r in h.MATRIX_PRINCIPALS}
CTX = {"api_base": API, "circuit_breaker": False}


class TestATimingOutDeploymentIsNotAGreenRun:
    def test_every_operation_timing_out_fails_the_gate(self, monkeypatch, capsys):
        """The regression the old harness could not see.

        With every request timing out, `_denied` was False for every cell, so every
        allowed-role and ANY-auth cell reported a pass and the process exited 0.
        """
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(
                0, None, None, "<timeout>"
            ),
        )
        results = []

        h.run_group_matrix(OPS, CTX, TOKENS, results)
        capsys.readouterr()

        assert results, "the matrix recorded nothing"
        assert not [r for r in results if r["outcome"] == "PASS"], (
            "a deployment where every request times out produced passing checks"
        )
        assert h.hard_failures(results), "the gate stayed green"
        assert all(r["inconclusive"] for r in results)

    def test_every_operation_returning_500_is_not_a_green_run(
        self, monkeypatch, capsys
    ):
        """43 cells of the last published snapshot were exactly this shape."""
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(
                500, "InternalError", None, "rid"
            ),
        )
        results = []

        h.run_group_matrix(OPS, CTX, TOKENS, results)
        capsys.readouterr()

        assert not [r for r in results if r["outcome"] == "PASS"]
        assert all(r["inconclusive"] for r in results)

    def test_a_healthy_deployment_still_passes(self, monkeypatch, capsys):
        """The control. Without it, "nothing passes" would also satisfy the above."""

        def _call(api_base, field, args, token):
            allowed = OPS[field]["groups"]
            if token is None:
                return h.ApiResult(401, None, None, "rid")
            role = token.replace("tok-", "")
            if allowed == "ANY":
                return h.ApiResult(200, None, None, "rid")
            if allowed == "IAM_ONLY" or role not in allowed:
                # Labelled with the control that really answers: the dispatcher's
                # field-level group floor runs before routing, so a caller its
                # policy excludes never reaches a resolver.
                return h.ApiResult(
                    403,
                    "Unauthorized",
                    None,
                    "rid",
                    control=h.CONTROL_GROUP_FLOOR,
                )
            return h.ApiResult(200, None, None, "rid")

        monkeypatch.setattr(h, "call", _call)
        results = []

        h.run_group_matrix(OPS, CTX, TOKENS, results)
        capsys.readouterr()

        assert h.hard_failures(results) == []
        assert all(r["outcome"] == "PASS" for r in results)


# ---------------------------------------------------------------------------
# The report says so
# ---------------------------------------------------------------------------
def test_the_report_names_inconclusive_results_separately(tmp_path):
    results = []
    h._record(results, "a", "Admin", 200, True, "ok")
    h._record(results, "b", "Admin", 0, False, "INCONCLUSIVE: timeout", outcome="ERROR")
    ctx = {"stack": "S", "region": "us-west-2", "api_base": API}

    md = h._render_md(
        ctx, results, h.hard_failures(results), [], {}, "20260101T000000Z", "1"
    )

    assert "Could not be run" in md
    assert "1/2 checks passed" in md
    assert "1 could not be run" in md


def test_a_mixed_run_writes_the_counts_it_blocks_on(tmp_path):
    results = []
    h._record(results, "a", "Admin", 200, True, "ok")
    h._record(results, "b", "Admin", 0, False, "INCONCLUSIVE", outcome="ERROR")
    h._record(results, "c", "*", "SKIP", False, "skipped", outcome="SKIP")
    ctx = {"stack": "S", "region": "us-west-2", "api_base": API}

    h.write_report(str(tmp_path), ctx, results, {}, "20260101T000000Z", "1")

    meta = json.loads((tmp_path / "S-20260101T000000Z" / "meta.json").read_text())[
        "totals"
    ]

    assert meta["passed"] == 1
    assert meta["errored"] == 1
    assert meta["skipped"] == 1
    assert meta["hard_fail"] == 1


# ---------------------------------------------------------------------------
# Every site that classifies an outcome, not just the ones behind classify()
#
# `_record` computes `"passed": resolved == "PASS"` and ignores its `passed`
# argument whenever `outcome=` is supplied. So at each site the `(not why) and ...`
# prefix on the boolean is belt-and-braces and the **`outcome=` argument is the
# load-bearing part** — which means a test that only asserts `passed is False`
# proves nothing about the classification. These assert the outcome and the
# `inconclusive` flag at each of the four sites in this file that build one by hand.
#
# The scope-suite arms are the ones where losing `outcome=` does real damage rather
# than merely mislabelling: `gap=_inconclusive_gap(st)` still fires for a 5xx, so the
# row becomes a WARN with `inconclusive=False` — it drops out of the report's "Could
# not be run" section and reads as an ordinary accepted gap. That is a muted form of
# the defect this whole change exists to fix.
# ---------------------------------------------------------------------------
SCOPE_TOKENS = {"Admin": "tok-Admin", "scoped": "tok-scoped"}


@pytest.mark.parametrize(
    "status,et",
    [(0, None), (500, None), (504, None), (200, "__unreadable_body__")],
)
class TestTheScopeSuiteArmsClassifyInconclusiveResults:
    """Three arms: scoped(out-of-scope), admin(unrestricted), scoped(filtered)."""

    def _run(self, monkeypatch, capsys, status, et):
        monkeypatch.setattr(
            h, "call", lambda api_base, field, args, token: (status, et, None, "rid")
        )
        results = []
        h.run_scope_suite(CTX, SCOPE_TOKENS, results)
        capsys.readouterr()
        return results

    def test_no_arm_passes(self, monkeypatch, capsys, status, et):
        results = self._run(monkeypatch, capsys, status, et)

        assert len(results) == 3, "expected all three scope arms to be recorded"
        assert not [r for r in results if r["outcome"] == "PASS"]

    def test_every_arm_is_flagged_inconclusive(self, monkeypatch, capsys, status, et):
        results = self._run(monkeypatch, capsys, status, et)

        for r in results:
            assert r["inconclusive"] is True, (
                f"{r['principal']}: classified {r['outcome']} without the "
                "inconclusive flag, so it drops out of the report's "
                "'Could not be run' section"
            )
            assert "INCONCLUSIVE" in r["detail"]

    def test_a_5xx_is_a_warning_that_still_says_it_established_nothing(
        self, monkeypatch, capsys, status, et
    ):
        """A 5xx carries the registered gap, so it is a WARN — but a WARN that is
        still marked inconclusive, not one indistinguishable from an ordinary gap."""
        results = self._run(monkeypatch, capsys, status, et)

        for r in results:
            if status >= 500:
                assert r["known_gap"] == h.GAP_INCONCLUSIVE_5XX
                assert r["outcome"] == "WARN"
            else:
                assert r["known_gap"] is None
                assert r["outcome"] == "ERROR"
            assert r["inconclusive"] is True


def test_a_healthy_scope_suite_still_passes(monkeypatch, capsys):
    """The control for the three above."""

    def _call(api_base, field, args, token):
        # nosec B105 - "tok-scoped" is a fake token identifier for the stubbed
        # `call`, not a credential; Bandit's hardcoded-password heuristic fires on the
        # comparison. Same suppression as the fake token ids in
        # test_api_security_cases.py.
        if token == "tok-scoped" and field == "getConfigVersion":  # nosec B105
            return 200, None, "Unauthorized", "rid"
        return 200, None, None, "rid"

    monkeypatch.setattr(h, "call", _call)
    results = []

    h.run_scope_suite(CTX, SCOPE_TOKENS, results)
    capsys.readouterr()

    assert len(results) == 3
    assert all(r["outcome"] == "PASS" for r in results)
    assert not [r for r in results if r["inconclusive"]]


#: A ctx the per-document suite can reach a verdict with. Its own rather than
#: :data:`CTX`, because the bucket is normally read from CloudFormation and these
#: tests must not: priming ``input_bucket`` is the documented short-circuit at the
#: top of ``_input_bucket``.
DOC_CTX = {**CTX, "input_bucket": "an-input-bucket"}


def _document_key(key):
    """Stub ``resolve_document_object_key``, which reads ``call_body`` not ``call``."""
    return lambda ctx, tokens: key


class TestThePerDocumentSuiteClassifiesInconclusiveResults:
    """Same convention as the three scope arms above, on the suite added for #1478.

    Written because this suite's verdict turns on a RESPONSE FIELD rather than only
    on a status, which is a new way for an arm here to read an unusable response as
    an answer: ``authz_control`` is ``None`` on a 500, on an unreadable body and on a
    dead connection alike, and the branch that reads ``None`` is a legitimate verdict
    ("this deployment sets no label") that must not swallow the inconclusive ones.
    """

    @pytest.mark.parametrize(
        "status,et",
        [(0, None), (500, None), (504, None), (200, "__unreadable_body__")],
    )
    def test_no_arm_passes_and_every_one_says_it_established_nothing(
        self, monkeypatch, capsys, status, et
    ):
        # `ApiResult` rather than a bare 4-tuple, because that is what `call`
        # returns on every one of these: a dead connection and an unreadable body
        # both produce one, with `authz_control` None. Stubbing a plain tuple would
        # make the stub the only caller in the run that cannot carry a label.
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(status, et, None, "rid"),
        )
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert len(results) == 2, "expected both arms to be recorded"
        assert not [r for r in results if r["outcome"] == "PASS"]
        for r in results:
            assert r["inconclusive"] is True, (
                f"{r['principal']}: classified {r['outcome']} without the "
                "inconclusive flag, so it drops out of the report's "
                "'Could not be run' section"
            )
            assert "INCONCLUSIVE" in r["detail"]
            if status >= 500:
                assert r["known_gap"] == h.GAP_INCONCLUSIVE_5XX
                assert r["outcome"] == "WARN"
            else:
                assert r["known_gap"] is None
                assert r["outcome"] == "ERROR"

    def test_a_healthy_deployment_passes_both_arms(self, monkeypatch, capsys):
        """The control: the scoped caller refused as ``resource``, Admin served."""

        def _call(api_base, field, args, token):
            if token == "tok-scoped":  # nosec B105 - a fake token id, not a credential
                return h.ApiResult(
                    403,
                    "Unauthorized",
                    None,
                    "rid",
                    control=h.CONTROL_RESOURCE,
                    message="Unauthorized: you are not permitted to read that object.",
                )
            return h.ApiResult(200, None, None, "rid")

        monkeypatch.setattr(h, "call", _call)
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert len(results) == 2
        assert all(r["outcome"] == "PASS" for r in results)
        assert not [r for r in results if r["inconclusive"]]

    @pytest.mark.parametrize(
        "control,why",
        [
            (h.CONTROL_RESOLVER, "an undeclared per-object control"),
            (h.CONTROL_GROUP_FLOOR, "the field-level group floor"),
        ],
    )
    def test_a_refusal_that_is_not_the_per_object_control_fails(
        self, monkeypatch, capsys, control, why
    ):
        """The whole point of the suite: a 403 alone is not the assertion.

        Both controls answer ``403``/``Unauthorized`` here, so an arm checking only
        "was it refused" would pass on a refusal that came from somewhere else —
        which is the confusion #1308 is about, and #1478 is the case where the
        per-document control reached the client unattributed.
        """

        def _call(api_base, field, args, token):
            if token == "tok-scoped":  # nosec B105 - a fake token id, not a credential
                return h.ApiResult(
                    403, "Unauthorized", None, "rid", control=control, message="no."
                )
            return h.ApiResult(200, None, None, "rid")

        monkeypatch.setattr(h, "call", _call)
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        scoped = results[0]
        assert scoped["outcome"] == "FAIL", f"{why} was scored {scoped['outcome']}"
        assert repr(control) in scoped["detail"]

    def test_an_unlabelled_refusal_is_inconclusive_rather_than_a_pass(
        self, monkeypatch, capsys
    ):
        """A stack deployed before `authzControl` existed labels nothing, so the
        cell cannot tell the two controls apart and established neither. ERROR
        rather than PASS, and rather than FAIL: it is a fact about the deployment
        rather than about its authorization."""

        def _call(api_base, field, args, token):
            if token == "tok-scoped":  # nosec B105 - a fake token id, not a credential
                return h.ApiResult(403, "Unauthorized", None, "rid")
            return h.ApiResult(200, None, None, "rid")

        monkeypatch.setattr(h, "call", _call)
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert results[0]["outcome"] == "ERROR"
        assert results[0]["inconclusive"] is True

    def test_a_403_carrying_a_url_is_not_a_refusal(self, monkeypatch, capsys):
        """The one failure mode every status assertion here would otherwise miss.

        The control exists so the capability is never minted; a refusal whose body
        carries the signed URL satisfies "was it refused" and hands over the object.
        """

        def _call(api_base, field, args, token):
            if token == "tok-scoped":  # nosec B105 - a fake token id, not a credential
                return h.ApiResult(
                    403,
                    "Unauthorized",
                    None,
                    "rid",
                    control=h.CONTROL_RESOURCE,
                    message="Unauthorized: you are not permitted to read that object.",
                    # In a field OTHER than the message, which is the half a
                    # message-only check cannot see.
                    body=(
                        '{"errors":[{"message":"Unauthorized: you are not permitted '
                        'to read that object."}],"url":"https://b.example/doc.pdf'
                        '?X-Amz-Signature=deadbeef"}'
                    ),
                )
            return h.ApiResult(200, None, None, "rid")

        monkeypatch.setattr(h, "call", _call)
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert results[0]["outcome"] == "FAIL"

    def test_a_served_document_is_a_failure_not_a_pass(self, monkeypatch, capsys):
        """The leak this suite exists to detect, asserted directly."""
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(200, None, None, "rid"),
        )
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert results[0]["outcome"] == "FAIL"
        assert "NOT REFUSED" in results[0]["detail"]

    @pytest.mark.parametrize(
        "ctx,key,why",
        [
            ({"api_base": API, "circuit_breaker": False}, "doc.pdf", "no input bucket"),
            (DOC_CTX, None, "no processed document"),
        ],
    )
    def test_an_absent_precondition_skips_rather_than_passing_or_crashing(
        self, monkeypatch, capsys, ctx, key, why
    ):
        """A SKIP, and **not** an exception.

        The first case is the one worth having: ``_input_bucket`` reads
        ``ctx["stack"]`` and ``ctx["region"]`` inside its own ``try``, and its
        contract is to answer ``""`` and let the caller skip. It raised ``KeyError``
        on a ctx without them, which aborted this suite and every suite after it.
        """
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(0, None, None, ""),
        )
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key(key))
        results = []

        h.run_document_bytes_scope_suite(ctx, SCOPE_TOKENS, results)
        capsys.readouterr()

        assert [r["outcome"] for r in results] == ["SKIP"], why
        assert not results[0]["passed"]

    def test_no_scoped_user_skips(self, monkeypatch, capsys):
        monkeypatch.setattr(h, "resolve_document_object_key", _document_key("doc.pdf"))
        results = []

        h.run_document_bytes_scope_suite(DOC_CTX, {"Admin": "tok-Admin"}, results)
        capsys.readouterr()

        assert [r["outcome"] for r in results] == ["SKIP"]


def test_the_per_document_suite_takes_the_same_arguments_as_its_siblings():
    """It is wired into ``main`` beside ``run_scope_suite``, so its signature has to
    match theirs — a fourth parameter would make the call site a TypeError at the
    end of a run that had already created Cognito users and seeded table rows.

    The call site's PRESENCE is asserted in
    ``test_api_rbac_refusal_attribution.py::test_the_document_bytes_suite_exists_and_is_invoked_from_main``,
    which reads ``main``'s own body; this is the complementary half and the two are
    not interchangeable.
    """
    import inspect

    expected = list(inspect.signature(h.run_scope_suite).parameters)

    assert list(inspect.signature(h.run_document_bytes_scope_suite).parameters) == (
        expected
    ), f"the per-document suite must take {expected}, like every sibling suite"


# A structurally valid JWT shape: the token-negatives suite base64url-decodes the
# signature segment to flip a byte in it, so a placeholder has to be decodable.
_FAKE_JWT = "hdr.pay." + base64.urlsafe_b64encode(b"\x00" * 256).decode().rstrip("=")


class TestTheTokenNegativeArmClassifiesInconclusiveResults:
    def test_a_dead_gateway_is_not_evidence_the_token_was_rejected(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            h, "call", lambda api_base, field, args, token: (0, None, None, "rid")
        )
        results = []

        h.run_token_negatives(CTX, {"Admin": _FAKE_JWT}, results)
        capsys.readouterr()

        assert results
        assert not [r for r in results if r["outcome"] == "PASS"]
        for r in results:
            assert r["outcome"] == "ERROR"
            assert r["inconclusive"] is True

    def test_a_real_401_is_still_a_pass(self, monkeypatch, capsys):
        monkeypatch.setattr(
            h, "call", lambda api_base, field, args, token: (401, None, None, "rid")
        )
        results = []

        h.run_token_negatives(CTX, {"Admin": _FAKE_JWT}, results)
        capsys.readouterr()

        assert results and all(r["outcome"] == "PASS" for r in results)


class TestTheUnauthCellClassifiesInconclusiveResults:
    def test_a_dead_gateway_is_not_a_failed_authorizer_assertion(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            h,
            "call",
            lambda api_base, field, args, token: h.ApiResult(0, None, None, "rid"),
        )
        results = []

        h.run_group_matrix(
            {"listDocuments": {"groups": ["Admin"], "args": {}}}, CTX, TOKENS, results
        )
        capsys.readouterr()

        unauth = [r for r in results if r["principal"] == "unauth"]
        assert unauth and unauth[0]["outcome"] == "ERROR"
        assert unauth[0]["inconclusive"] is True


def test_live_matrix_roles_are_every_template_group():
    """ROLES is read from template.yaml, so a new Cognito group joins the matrix."""
    assert h.ROLES[0] == "Admin"
    assert set(h.ROLES) == {
        "Admin",
        "Annotator",
        "Author",
        "Reviewer",
        "Viewer",
    }
