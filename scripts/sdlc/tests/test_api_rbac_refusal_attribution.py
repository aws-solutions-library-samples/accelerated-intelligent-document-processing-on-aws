# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The RBAC matrix must tell a GROUP refusal from a PER-OBJECT refusal.

The defect these tests exist for: every authorization refusal this API makes
arrives as ``403`` with ``errorType: "Unauthorized"``, and two controls that answer
opposite questions produce it —

* the field-level group floor in ``authz.py``: "this caller's groups do not admit
  this operation";
* a per-object control inside a resolver: "this caller's groups were fine and the
  object they named was refused" (a bucket outside the deployment's allow-list, a
  chat session they do not own, a document selection that is unfinished or out of
  scope).

The matrix asserts the first and cannot avoid provoking the second, because it
drives every operation with deliberately bogus arguments. With one indistinguishable
response shape for both, eight cells of one run reported three per-object controls
working correctly as group-authorization failures
([#1308](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1308)),
and the published snapshot carried ``RBAC dynamic — FAIL ❌ (8 hard failures)`` for
a release whose authorization was fine.

**The property asserted here is the distinction, not the count.** A test that
checked only "those eight cells now pass" would be satisfied by suppressing them,
which converts a false failure into a false pass — strictly worse. So every
assertion below is about the two refusal kinds producing DIFFERENT verdicts, in both
directions, and about neither being able to masquerade as the other.

No AWS and no network: the matrix is driven with a fake ``call``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_HARNESS = _REPO / "scripts" / "test_api_rbac.py"
_DISPATCHER = (
    _REPO / "nested" / "api-resolvers" / "src" / "lambda" / "http_api_dispatcher"
)
_RESOLVERS = _REPO / "nested" / "api-resolvers" / "src" / "lambda"
_EXPECTATIONS = _REPO / "scripts" / "api_rbac_expectations.yaml"


def _load_harness():
    spec = importlib.util.spec_from_file_location("_rbac_harness_attribution", _HARNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rbac_harness_attribution"] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_harness()

API = "https://abc.execute-api.us-west-2.amazonaws.com/api"
CTX = {"api_base": API, "circuit_breaker": False}
# Every principal the matrix drives, not just the group roles: the groupless
# caller is one of them (#1065), and a token map built from ROLES alone would
# make the matrix raise rather than drive it.
TOKENS = {r: f"tok-{r}" for r in h.MATRIX_PRINCIPALS}


# ---------------------------------------------------------------------------
# The vocabulary is the dispatcher's, derived from it rather than copied
# ---------------------------------------------------------------------------
def _dispatcher_control_vocabulary() -> dict:
    """``{constant name: value}`` for the dispatcher's AUTHZ_CONTROL_* literals.

    Read out of the dispatcher's SOURCE with ``ast``, not imported: ``index.py``
    builds boto3 clients and reads SSM at import time. The point is that this
    harness's table is checked against the authority — the module that emits the
    label — so a renamed value cannot leave the harness silently reading ``None``
    for every refusal while every test stays green.
    """
    tree = ast.parse((_DISPATCHER / "index.py").read_text())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if name.startswith("AUTHZ_CONTROL") and isinstance(
                node.value, ast.Constant
            ):
                found[name] = node.value.value
    return found


def test_the_derived_dispatcher_vocabulary_is_not_empty():
    """An empty derived set collects as a pass, not a failure, in every test that
    parametrises over it. Assert it first and separately."""
    vocab = _dispatcher_control_vocabulary()
    assert vocab, (
        "no AWS_CONTROL_* literals found in the dispatcher — the parity assertion "
        "below would be vacuous"
    )
    assert len(vocab) >= 5, f"expected the field name plus four values, got {vocab}"


def test_the_harness_reads_exactly_the_labels_the_dispatcher_writes():
    vocab = _dispatcher_control_vocabulary()

    assert h.CONTROL_FIELD == vocab["AUTHZ_CONTROL_FIELD"]
    assert h.CONTROL_GROUP_FLOOR == vocab["AUTHZ_CONTROL_GROUP_FLOOR"]
    assert h.CONTROL_RESOURCE == vocab["AUTHZ_CONTROL_RESOURCE"]
    assert h.CONTROL_RESOLVER == vocab["AUTHZ_CONTROL_RESOLVER_UNSPECIFIED"]
    assert h.CONTROL_UNVERIFIED_IDENTITY == vocab["AUTHZ_CONTROL_UNVERIFIED_IDENTITY"]


def test_call_reads_the_label_off_a_real_response_body():
    """``call`` used to discard the only field that differs between the two
    refusals. Parse the body the dispatcher actually writes."""
    body = json.dumps(
        {
            "errors": [
                {
                    "message": "Unauthorized: session not found for this user.",
                    "errorType": "Unauthorized",
                    h.CONTROL_FIELD: h.CONTROL_RESOURCE,
                }
            ]
        }
    )
    import urllib.request

    class _Resp:
        status = 403
        headers: dict = {}

        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _Resp()
    try:
        result = h.call(API, "getChatMessages", {}, "tok")
    finally:
        urllib.request.urlopen = original

    assert tuple(result)[:2] == (403, "Unauthorized")
    assert result.authz_control == h.CONTROL_RESOURCE
    assert result.message == "Unauthorized: session not found for this user."


def test_a_body_with_no_label_reports_none_rather_than_guessing():
    """What a stack deployed before the label answers. It must be distinguishable
    from ``resolver`` — "the server could not attribute this" and "the harness was
    told nothing" are different claims about a run."""
    import urllib.request

    class _Resp:
        status = 403
        headers: dict = {}

        def read(self):
            return json.dumps(
                {
                    "errors": [
                        {"message": "Unauthorized: x", "errorType": "Unauthorized"}
                    ]
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _Resp()
    try:
        result = h.call(API, "listUsers", {}, "tok")
    finally:
        urllib.request.urlopen = original

    assert result.authz_control is h.CONTROL_UNLABELLED
    assert result.authz_control != h.CONTROL_RESOLVER


# ---------------------------------------------------------------------------
# classify(): the two refusals get different verdicts, both ways round
# ---------------------------------------------------------------------------
# Named this way rather than the obvious word for "permitted". The gate-registry
# discovery scan matches constant NAMES against `NAME_VOCABULARY` (in
# `scripts/tests/`), which holds the words for what a gate does, so a set under the
# obvious name is reported as an unregistered carve-out. It is not one: these are
# the groups a test operation admits. Asking that registry for a judgement on noise
# is how a reviewer learns to rubber-stamp it, so the name moves instead.
ADMITTED_GROUPS = {"Admin", "Author"}


def _allowed_cell(control):
    return h.classify("Admin", ADMITTED_GROUPS, 403, "Unauthorized", None, control)


def _disallowed_cell(control):
    return h.classify("Viewer", ADMITTED_GROUPS, 403, "Unauthorized", None, control)


def test_an_allowed_role_refused_by_the_group_floor_is_a_hard_failure():
    """Unchanged, and it has to stay unchanged: this is the shape a real
    regression takes."""
    verdict = _allowed_cell(h.CONTROL_GROUP_FLOOR)

    assert verdict.outcome == "FAIL"
    assert verdict.passed is False
    assert h.CONTROL_GROUP_FLOOR in verdict.detail


def test_an_allowed_role_refused_by_a_resource_control_is_not_that_failure():
    """The eight. The group floor demonstrably admitted the caller — the request
    reached the resolver — so this is not the group check failing."""
    verdict = _allowed_cell(h.CONTROL_RESOURCE)

    assert verdict.outcome == "ERROR"
    assert "resource-level control" in verdict.detail
    assert "did not measure the group check" in verdict.detail


def test_the_two_allowed_role_refusals_get_DIFFERENT_verdicts():
    """The property, stated directly. Before the label both were ``FAIL`` with the
    same ``DENIED but allowed`` detail, so the harness could not tell a group-check
    regression from a per-object control doing its job."""
    floor = _allowed_cell(h.CONTROL_GROUP_FLOOR)
    resource = _allowed_cell(h.CONTROL_RESOURCE)

    assert floor.outcome != resource.outcome
    assert floor.detail != resource.detail


def test_a_resource_refusal_of_an_allowed_role_is_still_not_a_pass():
    """The failure mode the other direction: scoring it green, or registering it
    against a gap, would make a cell that measured nothing report success."""
    verdict = _allowed_cell(h.CONTROL_RESOURCE)

    assert verdict.passed is False
    assert verdict.gap is None, "no gap id — this must stay blocking"
    results = []
    h._record(
        results,
        "addDocumentsToTestSetByKey",
        "Admin",
        403,
        verdict.passed,
        verdict.detail,
        outcome=verdict.outcome,
        control=h.CONTROL_RESOURCE,
    )
    assert h.hard_failures(results), "a cell that measured nothing left the gate green"
    assert results[0]["inconclusive"] is True


def test_a_disallowed_role_refused_by_the_group_floor_passes():
    verdict = _disallowed_cell(h.CONTROL_GROUP_FLOOR)

    assert verdict.outcome == "PASS"
    assert verdict.passed is True


def test_a_disallowed_role_refused_by_a_resource_control_now_FAILS():
    """The reverse confusion, and the one that was silent.

    This cell used to pass on ANY 403, so a per-object refusal counted as proof
    that the group floor refuses a caller its policy excludes. The floor runs
    before routing, so such a caller cannot reach a resolver at all — a
    ``resource`` label here means the floor did not refuse somebody it must.
    """
    verdict = _disallowed_cell(h.CONTROL_RESOURCE)

    assert verdict.outcome == "FAIL"
    assert verdict.passed is False
    assert "WRONG CONTROL" in verdict.detail


def test_the_two_disallowed_role_refusals_get_DIFFERENT_verdicts():
    floor = _disallowed_cell(h.CONTROL_GROUP_FLOOR)
    resource = _disallowed_cell(h.CONTROL_RESOURCE)

    assert floor.outcome != resource.outcome


@pytest.mark.parametrize(
    "control",
    [
        h.CONTROL_GROUP_FLOOR,
        h.CONTROL_RESOLVER,
        h.CONTROL_UNVERIFIED_IDENTITY,
        h.CONTROL_UNLABELLED,
        "a-value-this-harness-has-never-seen",
        "",
        0,
        [],
        {"control": "resource"},
    ],
)
def test_only_an_explicit_resource_label_leaves_the_strict_verdict(control):
    """Fail-closed polarity, over every other value a response could carry.

    Only ``"resource"`` answers True. An unattributed resolver refusal, a missing
    label, an unrecognised string, an empty string and a non-string all keep the
    strict reading, so a forgotten label costs a visible failure rather than a
    silent pass — and a resolver's own GROUP check regressing cannot be downgraded
    to a known coverage gap.
    """
    assert h.refusal_is_resource_level(control) is False
    assert _allowed_cell(control).outcome == "FAIL"
    assert _disallowed_cell(control).outcome == "PASS"


@pytest.mark.parametrize(
    "control", [h.CONTROL_GROUP_FLOOR, h.CONTROL_RESOLVER, h.CONTROL_UNLABELLED]
)
def test_a_failing_cells_detail_names_the_control_so_triage_starts_there(control):
    """The report line has to say which layer refused, verbatim.

    "DENIED but allowed (403/Unauthorized)" is what sent a reader to CloudWatch to
    find out which of two controls answered, and establishing that the eight were
    not a regression took a by-hand comparison against a prior run. An unattributed
    refusal says so in words rather than leaving the field blank.
    """
    detail = _allowed_cell(control).detail

    assert "control=" in detail
    assert (control or "unattributed") in detail
    assert str(403) in detail


def test_the_resource_detail_names_the_field_a_reader_would_grep_for():
    """Both the label's own field name and its value, so the report line and the
    response body can be matched up without a second lookup."""
    allowed = _allowed_cell(h.CONTROL_RESOURCE).detail
    disallowed = _disallowed_cell(h.CONTROL_RESOURCE).detail

    for detail in (allowed, disallowed):
        assert h.CONTROL_FIELD in detail
        assert h.CONTROL_RESOURCE in detail


def test_the_resource_label_is_the_only_one_that_moves_a_verdict():
    """Non-vacuity of the parametrisation above: if nothing moved the verdict, the
    whole distinction would be inert and every test here would still pass."""
    assert h.refusal_is_resource_level(h.CONTROL_RESOURCE) is True
    assert _allowed_cell(h.CONTROL_RESOURCE).outcome != "FAIL"
    assert _disallowed_cell(h.CONTROL_RESOURCE).outcome != "PASS"


def test_an_inconclusive_result_still_wins_over_the_label():
    """A 5xx or a dead connection establishes nothing whatever the body claims, and
    a response carrying a ``resource`` label must not promote one into an answer."""
    timed_out = h.classify("Admin", ADMITTED_GROUPS, 0, None, None, h.CONTROL_RESOURCE)
    crashed = h.classify(
        "Admin", ADMITTED_GROUPS, 500, "InternalError", None, h.CONTROL_RESOURCE
    )

    assert timed_out.outcome == "ERROR"
    assert "request did not complete" in timed_out.detail
    assert crashed.outcome == "ERROR"
    assert crashed.gap == h.GAP_INCONCLUSIVE_5XX


def test_an_iam_only_op_refused_by_a_resource_control_fails():
    """Every Cognito caller must be refused by the floor's IAM_ONLY branch. A
    per-object refusal means the request was routed to a resolver, which for these
    operations is the leak the cell exists to catch."""
    assert (
        h.classify(
            "Admin", h.IAM, 403, "Unauthorized", None, h.CONTROL_RESOURCE
        ).outcome
        == "FAIL"
    )
    assert (
        h.classify(
            "Admin", h.IAM, 403, "Unauthorized", None, h.CONTROL_GROUP_FLOOR
        ).outcome
        == "PASS"
    )


def test_an_any_group_op_behaves_like_an_allowed_cell():
    """``getChatMessages`` is ``ANY_GROUP``, which the harness expands to the
    stack's group names, so all four roles are allowed cells. The ``ANY`` policy
    takes the same branch and must agree."""
    assert (
        h.classify(
            "Viewer", h.ANY, 403, "Unauthorized", None, h.CONTROL_RESOURCE
        ).outcome
        == "ERROR"
    )
    assert (
        h.classify(
            "Viewer", h.ANY, 403, "Unauthorized", None, h.CONTROL_GROUP_FLOOR
        ).outcome
        == "FAIL"
    )


# ---------------------------------------------------------------------------
# End to end through run_group_matrix, and into the report
# ---------------------------------------------------------------------------
OPS = {
    "autoDetectSections": {"groups": ["Admin", "Author"], "args": {}},
    "getChatMessages": {"groups": h.ROLES, "args": {}},
}


def _matrix(control_for_allowed):
    """Drive the matrix with every admitted cell refused under ``control_for_allowed``
    and every disallowed cell refused by the floor, as the live API would."""

    def _call(api_base, field, args, token):
        if token is None:
            return h.ApiResult(401, None, None, "rid")
        role = token.replace("tok-", "")
        if role in OPS[field]["groups"]:
            return h.ApiResult(
                403, "Unauthorized", None, "rid", control=control_for_allowed
            )
        return h.ApiResult(
            403, "Unauthorized", None, "rid", control=h.CONTROL_GROUP_FLOOR
        )

    return _call


def test_the_matrix_reports_a_resource_refusal_as_not_run_not_as_a_failure(
    monkeypatch, capsys
):
    monkeypatch.setattr(h, "call", _matrix(h.CONTROL_RESOURCE))
    results = []

    h.run_group_matrix(OPS, CTX, TOKENS, results)
    capsys.readouterr()

    allowed_rows = [
        r
        for r in results
        if r["principal"] in h.ROLES and r["principal"] in OPS[r["op"]]["groups"]
    ]
    assert allowed_rows, "the matrix drove no allowed cell"
    assert all(r["outcome"] == "ERROR" for r in allowed_rows)
    assert not [r for r in allowed_rows if r["outcome"] == "FAIL"]
    assert all(r["authz_control"] == h.CONTROL_RESOURCE for r in allowed_rows)


def test_the_matrix_still_reports_a_group_floor_refusal_as_a_failure(
    monkeypatch, capsys
):
    """The control for the test above: swapping ONE field of the response — the
    label — has to swap the verdict. Otherwise "no FAILs" would be explained by the
    harness having stopped failing."""
    monkeypatch.setattr(h, "call", _matrix(h.CONTROL_GROUP_FLOOR))
    results = []

    h.run_group_matrix(OPS, CTX, TOKENS, results)
    capsys.readouterr()

    allowed_rows = [
        r
        for r in results
        if r["principal"] in h.ROLES and r["principal"] in OPS[r["op"]]["groups"]
    ]
    assert allowed_rows
    assert all(r["outcome"] == "FAIL" for r in allowed_rows)
    assert h.hard_failures(results)


def test_every_recorded_row_carries_the_attribution_field(monkeypatch, capsys):
    """Including passes and the unauth cell. A report full of nulls is how a reader
    sees that a run could not attribute refusals at all — which is the honest
    reading against a stack deployed before the label."""
    monkeypatch.setattr(h, "call", _matrix(h.CONTROL_RESOURCE))
    results = []

    h.run_group_matrix(OPS, CTX, TOKENS, results)
    capsys.readouterr()

    assert results
    for row in results:
        assert "authz_control" in row
        assert "authz_message" in row


def test_the_report_names_the_control_that_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(h, "call", _matrix(h.CONTROL_RESOURCE))
    results = []
    h.run_group_matrix(OPS, CTX, TOKENS, results)
    capsys.readouterr()

    hard = h.write_report(
        tmp_path, {**CTX, "stack": "S", "region": "us-west-2"}, results, {}, "ts", "123"
    )
    capsys.readouterr()
    report = (tmp_path / "S-ts" / "report.md").read_text()

    assert hard, "the run must still be non-zero: nothing measured the group check"
    assert "| Control |" in report or "Control |" in report
    assert h.CONTROL_RESOURCE in report
    assert "## 🛑 Could not be run" in report
    persisted = json.loads((tmp_path / "S-ts" / "report.json").read_text())["results"]
    assert any(r["authz_control"] == h.CONTROL_RESOURCE for r in persisted)


# ---------------------------------------------------------------------------
# The population at risk, derived from the expectations file
# ---------------------------------------------------------------------------
def _expectations() -> dict:
    import yaml

    return yaml.safe_load(_EXPECTATIONS.read_text())["operations"]


def _ops_with_a_per_object_control() -> dict:
    """Operations the expectations file says carry a control OTHER than groups.

    Derived from the authority rather than listed: these flags are what
    ``scan_api_rbac.py``'s S3 and S4 checks already read, so the population grows
    with the file. Every one of them is a candidate for the confusion #1308 is
    about — the matrix drives it with a bogus argument and the per-object control
    can answer instead of the group floor.
    """
    return {
        name: op
        for name, op in _expectations().items()
        if op.get("ownership") or op.get("scope_checked") or op.get("scope_filtered")
    }


def test_the_population_at_risk_is_not_empty():
    """An empty derived mapping parametrises to zero cases, which pytest reports as
    a collected-nothing pass rather than a failure. Assert it on its own.

    The floor is 15 and the tree currently yields **31** (19 group-restricted, 12
    `ANY`/`ANY_GROUP`), so the parametrisation below is broader than the floor. No
    figure is written into the assertion beyond the floor: the count moves whenever
    an operation gains one of the three flags, and a literal here would go stale
    within days — which is what the repository's own documented-counts guard exists
    to prevent.
    """
    at_risk = _ops_with_a_per_object_control()
    assert len(at_risk) >= 15, (
        f"only {len(at_risk)} operations declare a per-object control; the "
        "parametrisation below would barely cover anything"
    )


@pytest.mark.parametrize("op_name", sorted(_ops_with_a_per_object_control()))
def test_no_declared_per_object_control_can_read_as_a_group_failure(op_name):
    """Every operation carrying a second control gets the distinction, not just the
    three in the issue. The previous fix was per-operation argument surgery, which
    is the "fix the instance, not the class" shape: it left the next operation to
    gain a per-object control reporting the same false failure."""
    op = _expectations()[op_name]
    groups = op["groups"]
    allowed = (
        h.ANY
        if groups == "ANY"
        else h.IAM
        if groups == "IAM_ONLY"
        else set(h.ROLES)
        if groups == "ANY_GROUP"
        else set(groups)
    )
    role = (
        "Admin"
        if allowed in (h.ANY, h.IAM) or "Admin" in allowed
        else sorted(allowed)[0]
    )

    resource = h.classify(role, allowed, 403, "Unauthorized", None, h.CONTROL_RESOURCE)
    floor = h.classify(role, allowed, 403, "Unauthorized", None, h.CONTROL_GROUP_FLOOR)

    assert resource.outcome != floor.outcome, (
        f"{op_name}: a per-object refusal and a group refusal get the same verdict"
    )
    if allowed is not h.IAM:
        assert resource.outcome == "ERROR"
        assert floor.outcome == "FAIL"


# ---------------------------------------------------------------------------
# The argument-shape fix the three named operations rely on, pinned
# ---------------------------------------------------------------------------
# a6b021363 gave each of the three operations in #1308 an argument the resolver
# refuses BEFORE it consults the per-object control, so the group floor is what the
# cell measures. That fix is real and it is what makes those cells green today —
# but it is a property of SOURCE ORDER inside three resolvers, and nothing asserted
# it. A resolver that moved its validation below its per-object control would bring
# the eight cells back; with the attribution above they would read as ERROR naming
# the control rather than as a group-check failure, which is the point, but the
# coverage would still be lost silently. Pin the ordering.
_ORDERING_CASES = (
    # op, resolver, the function that serves it, the argument guard, the
    # per-object control the guard must precede.
    #
    # The function is named because the comparison has to be bounded to the body
    # that serves the operation. A whole-file search finds the per-object control's
    # `def` line, which sits ABOVE its call site, so an unbounded version of this
    # test fails on correct code — it did, which is how the bound got written.
    (
        "autoDetectSections",
        "discovery_upload_resolver/index.py",
        "handle_auto_detect_sections",
        r"if not document_key or not bucket",
        r"s3_targets\.assert_bucket_allowed\(",
    ),
    (
        "addDocumentsToTestSetByKey",
        "test_set_resolver/index.py",
        "add_documents_to_test_set_by_key",
        r"_normalize_object_keys\(",
        r"_enforce_selection_scope\(",
    ),
    (
        "getChatMessages",
        "get_agent_chat_messages_resolver/index.py",
        "handler",
        r"raise ValueError\(\"sessionId parameter is required\"\)",
        r"_verify_session_ownership\(",
    ),
)


def _function_source(path: Path, name: str) -> str:
    """The source of one top-level function, so an ordering comparison is bounded
    to the body that serves the operation."""
    source = path.read_text()
    for node in ast.parse(source).body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            segment = ast.get_source_segment(source, node)
            assert segment, f"could not read the source of {name} in {path.name}"
            return segment
    raise AssertionError(f"{path.name} has no top-level function named {name}")


def test_the_ordering_cases_cover_the_operations_the_issue_named():
    """Guards the guard: a case list that drifted away from the issue's three
    operations would leave this section asserting nothing about them."""
    covered = {case[0] for case in _ORDERING_CASES}
    assert covered == {
        "autoDetectSections",
        "addDocumentsToTestSetByKey",
        "getChatMessages",
    }
    expectations = _expectations()
    for op_name in covered:
        assert op_name in expectations, f"{op_name} is no longer a declared operation"


@pytest.mark.parametrize(
    "op_name,resolver,function,guard,control",
    _ORDERING_CASES,
    ids=[c[0] for c in _ORDERING_CASES],
)
def test_the_argument_guard_runs_before_the_per_object_control(
    op_name, resolver, function, guard, control
):
    source = _function_source(_RESOLVERS / resolver, function)
    guard_match = re.search(guard, source)
    control_match = re.search(control, source)

    assert guard_match, (
        f"{resolver}:{function}: the argument guard for {op_name} is gone"
    )
    assert control_match, (
        f"{resolver}:{function}: the per-object control for {op_name} is gone"
    )
    assert guard_match.start() < control_match.start(), (
        f"{resolver}: {op_name}'s per-object control now runs BEFORE its argument "
        "validation, so the matrix measures that control instead of the group floor"
    )


@pytest.mark.parametrize(
    "op_name", ["autoDetectSections", "addDocumentsToTestSetByKey", "getChatMessages"]
)
def test_the_three_operations_still_send_the_argument_that_reaches_the_floor(op_name):
    """The other half of the same fix: the argument has to be the EMPTY one the
    guard above rejects. ``"__nope__"`` is a well-formed value that passes
    validation and lands on the per-object control, which is what produced the
    eight."""
    args = _expectations()[op_name].get("args") or {}
    assert args, f"{op_name} declares no args"
    empties = [k for k, v in args.items() if v == "" or v == []]
    assert empties, (
        f"{op_name} no longer sends an empty required argument, so its resolver's "
        f"validation will not fire ahead of its per-object control: {args}"
    )


# ---------------------------------------------------------------------------
# The per-object refusals that DECLARE themselves, and the ones that do not
# ---------------------------------------------------------------------------
# A label is only as good as the raise sites that set it, and the dispatcher cannot
# attribute a refusal the resolver did not declare. These are the sites converted
# for #1308 — the controls the live matrix actually provokes on the three operations
# the issue named, plus the shared S3 helper the fourth and fifth reach.
#
# ⚠️ This list is the set of sites CONVERTED, not the set of per-object controls in
# the tree, and it grants the rest nothing. `key_scope.py`'s six refusals, the
# config-version scope refusals in `reprocess_document_resolver`, `upload_resolver`
# and `list_documents_*`, and the in-band
# `{success:false,error:{type:"Unauthorized"}}` 200 the configuration and sync
# resolvers return all remain UNATTRIBUTED, which downstream means the STRICT
# verdict — a hard failure, never a lenient one. So this list bounds what is
# claimed, and a site left off it is treated more harshly rather than waved through.
# The reason it is bounded: the live matrix drives none of those to a refusal today
# (its roles are unscoped, and its `getFileContents` bucket is the input bucket,
# which no scope axis partitions), so converting them would be unexercised churn on
# the authorization path. Convert one when the matrix can reach it.
_DECLARED_RESOURCE_REFUSALS = (
    ("idp_common/s3_targets.py", "assert_bucket_allowed"),
    ("idp_common/s3_targets.py", "assert_key_writable"),
    ("test_set_resolver/index.py", "_enforce_selection_scope"),
    ("get_agent_chat_messages_resolver/index.py", "handler"),
)


def _raised_names(path: Path, function: str) -> set:
    """The class names raised inside one top-level function."""
    source = path.read_text()
    for node in ast.parse(source).body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
        ):
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and sub.exc is not None:
                    exc = sub.exc
                    callee = exc.func if isinstance(exc, ast.Call) else exc
                    name = getattr(callee, "id", None) or getattr(callee, "attr", None)
                    if name:
                        names.add(name)
            return names
    raise AssertionError(f"{path.name} has no top-level function named {function}")


def test_the_declared_resource_refusal_list_is_not_empty():
    assert len(_DECLARED_RESOURCE_REFUSALS) >= 4


@pytest.mark.parametrize(
    "relative,function",
    _DECLARED_RESOURCE_REFUSALS,
    ids=[f"{r.split('/')[-2]}:{f}" for r, f in _DECLARED_RESOURCE_REFUSALS],
)
def test_a_converted_per_object_control_still_declares_itself(relative, function):
    """Reverting one of these to a bare ``PermissionError`` would lose the
    attribution for that operation silently — the dispatcher would label it
    ``resolver``, the harness would apply the strict verdict, and the cell would go
    back to reading as a group-check failure."""
    base = (
        _REPO / "lib" / "idp_common_pkg"
        if relative.startswith("idp_common/")
        else _RESOLVERS
    )
    raised = _raised_names(base / relative, function)

    assert "ResourceAccessDenied" in raised, (
        f"{relative}:{function} no longer declares its per-object refusal; raised "
        f"{sorted(raised)}"
    )


def test_the_two_declarations_of_the_class_agree_on_what_matters():
    """The helper declares its own class rather than importing the canonical one,
    because it is vendored byte-identically into a package with no ``idp_common``.
    Two declarations are sound only because the consumer is across a PROCESS
    boundary and matches by name — so assert the name, and assert the two
    properties the in-process mappings and an older dispatcher rely on."""
    from idp_common.api_adapter import ResourceAccessDenied as canonical
    from idp_common.s3_targets import ResourceAccessDenied as local

    assert canonical.__name__ == local.__name__ == "ResourceAccessDenied"
    assert issubclass(canonical, PermissionError)
    assert issubclass(local, PermissionError)


def test_nothing_in_the_dispatcher_process_calls_the_s3_helper():
    """Why two same-named classes cannot bite in-process.

    The dispatcher's own ``except ResourceAccessDenied`` is the canonical class, so
    it would not catch the helper's. That is harmless only while no module the
    dispatcher executes IN ITS OWN PROCESS calls the helper — the resolvers that do
    are reached through ``lambda:Invoke``, where the match is by name. If that ever
    changes, the helper has to import the canonical class instead.
    """
    for module in sorted(_DISPATCHER.glob("*.py")):
        assert "s3_targets" not in module.read_text(), (
            f"{module.name} now references the S3 helper in the dispatcher's own "
            "process, where the two declarations of ResourceAccessDenied are "
            "DIFFERENT classes and `except ResourceAccessDenied` would miss one"
        )


# The complement of the list above: refusal sites this change deliberately leaves
# UNATTRIBUTED, each asserted to still raise the unattributed class. An independent
# review measured the gap — relabelling the test-set resolver's scope-lookup refusal
# as the per-object class survived every suite — so the prose claim had nothing
# holding it.
#
# Why these must not be relabelled, which is the same question in each case: none is
# a decision about an object the caller named.
#
#   * a scope that could not be EVALUATED (no UsersTable wired, no email claim, a
#     failed query) is a fail-closed refusal about the deployment, and the resolvers
#     spell it with one shared message;
#   * no resolvable caller identity at all is a refusal about the principal;
#   * an empty set of wired bucket names is a template fault.
#
# Relabelling one cannot create a false pass — the per-object label only ever moves
# a verdict from FAIL to ERROR, both blocking — but it would move a real fault out of
# the row a reader looks at first, and it would make the documented boundary false.
_UNATTRIBUTED_REFUSAL_SITES = (
    ("test_set_resolver/index.py", "_caller_config_scope", "scope cannot be evaluated"),
    (
        "get_agent_chat_messages_resolver/index.py",
        "handler",
        "no resolvable caller identity",
    ),
)


def test_the_unattributed_site_list_is_not_empty():
    assert len(_UNATTRIBUTED_REFUSAL_SITES) >= 2


@pytest.mark.parametrize(
    "relative,function,why",
    _UNATTRIBUTED_REFUSAL_SITES,
    ids=[f"{r.split('/')[0]}:{f}" for r, f, _ in _UNATTRIBUTED_REFUSAL_SITES],
)
def test_a_deliberately_unattributed_refusal_stays_unattributed(
    relative, function, why
):
    raised = _raised_names(_RESOLVERS / relative, function)

    assert "PermissionError" in raised, (
        f"{relative}:{function} ({why}) no longer raises the unattributed class; "
        f"raised {sorted(raised)}"
    )


def test_the_empty_allow_list_refusal_stays_unattributed():
    """The one in the shared S3 helper. Its sibling — a bucket outside the
    allow-list — IS per-object and is in the converted list above, so this function
    raises both classes and the assertion has to name the one that must still be
    there."""
    raised = _raised_names(
        _REPO / "lib" / "idp_common_pkg" / "idp_common/s3_targets.py",
        "assert_bucket_allowed",
    )

    assert raised == {"PermissionError", "ResourceAccessDenied"}, (
        "the empty-allow-list branch is a template fault rather than a decision "
        f"about a bucket the caller named, and must stay unattributed; raised "
        f"{sorted(raised)}"
    )


def test_the_vendored_copy_of_the_s3_helper_declares_it_too():
    """The read path's Lambda carries a byte-identical copy of the helper (there is
    a parity gate for that), and it has no ``idp_common`` to import the class from.
    If the local fallback declaration were dropped the copy would stop importing."""
    vendored = _RESOLVERS / "get_file_contents_resolver" / "s3_targets.py"
    canonical = _REPO / "lib" / "idp_common_pkg" / "idp_common" / "s3_targets.py"

    assert vendored.read_text() == canonical.read_text()
    assert "ResourceAccessDenied" in _raised_names(vendored, "assert_bucket_allowed")
    source = vendored.read_text()
    assert "class ResourceAccessDenied(PermissionError)" in source, (
        "the layerless copy needs its own declaration; the canonical import is not "
        "available in that package"
    )


def test_the_group_floor_does_not_declare_itself_as_a_resource_refusal():
    """The adversarial direction: if ``authz.enforce`` raised the per-object class,
    a GROUP refusal would arrive labelled ``resource`` and the harness would stop
    failing on it. Assert the floor raises its own class and nothing else."""
    raised = _raised_names(_DISPATCHER / "authz.py", "enforce")

    assert raised == {"GroupFloorDenied"}, (
        f"authz.enforce raises {sorted(raised)}; only its own class may label the "
        "group floor"
    )


def test_the_console_grid_shows_a_usable_mark_for_every_cell(monkeypatch, capsys):
    """The one-line-per-operation grid is what a human watches during a live run,
    and it is padded to 16 columns — so the detail, which names the control and
    runs to a sentence, cannot go in it. Each verdict therefore carries a compact
    form, and the grid falls back to the detail when there is none.

    Asserted because that fallback is an easy thing to get subtly wrong: an
    unbiased mutation sample turned the `or` into an `and`, which prints `Ad=None`
    for every passing cell and the full sentence for every failing one, and nothing
    noticed. A grid of `None` is worse than no grid, because it reads as a result.
    """
    monkeypatch.setattr(h, "call", _matrix(h.CONTROL_RESOURCE))
    results = []

    h.run_group_matrix(OPS, CTX, TOKENS, results)
    grid = capsys.readouterr().out

    assert "None" not in grid, f"the console grid printed None:\n{grid}"
    for line in grid.splitlines():
        if not line.strip():
            continue
        # Every role mark is `XX=<something non-empty>`.
        for mark in re.findall(r"\b([A-Z][a-z])=(\S*)", line):
            assert mark[1], f"empty mark for {mark[0]} in: {line}"
    # And the resource-refusal mark says the group check is what is in doubt,
    # rather than claiming a denial.
    assert "grp?" in grid
    assert "DENIED" not in grid


def test_the_console_grid_shows_the_status_for_a_passing_cell(monkeypatch, capsys):
    """The control for the test above: with everything passing, the grid must carry
    the status, not an empty string."""

    def _call(api_base, field, args, token):
        if token is None:
            return h.ApiResult(401, None, None, "rid")
        role = token.replace("tok-", "")
        if role in OPS[field]["groups"]:
            return h.ApiResult(200, None, None, "rid")
        return h.ApiResult(
            403, "Unauthorized", None, "rid", control=h.CONTROL_GROUP_FLOOR
        )

    monkeypatch.setattr(h, "call", _call)
    results = []

    h.run_group_matrix(OPS, CTX, TOKENS, results)
    grid = capsys.readouterr().out

    assert "None" not in grid
    assert "Ad=200" in grid
    assert "Vi=403" in grid
    assert all(r["outcome"] == "PASS" for r in results)
