# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Configuration scope on the resolvers that act on a document, or a test run, by key.

Each resolver is loaded for real and pointed at moto tables; what it would do next
(delete, stop a workflow, copy objects, read run results) is replaced, so each case
can assert whether that step was REACHED — which is the mechanism, not just the
status code.

The four caller shapes per resolver: a scoped caller on another profile's document
(refused, and the next step not reached), the same caller on their own document
(reached), an Admin and an unscoped caller (reached), and, under
``TenantIsolationMode=Strict``, an unscoped non-admin (refused).
"""

from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.unit import scope_tables as st
from tests.unit.lambdas._lambda_loader import load_lambda

REFUSAL = "^Unauthorized: you are not permitted to act on that document"

ADMIN = st.claims("admin@example.com", "Admin")


def _ev(arguments, claims, field=None):
    event = {"arguments": arguments, "identity": {"claims": claims}}
    if field:
        event["info"] = {"fieldName": field}
    return event


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105
    monkeypatch.setenv("USERS_TABLE_NAME", st.USERS)
    monkeypatch.setenv("TRACKING_TABLE", st.TRACKING)
    monkeypatch.setenv("TRACKING_TABLE_NAME", st.TRACKING)
    monkeypatch.setenv("INPUT_BUCKET", "in")
    monkeypatch.setenv("OUTPUT_BUCKET", "out")
    monkeypatch.setenv("EVALUATION_BASELINE_BUCKET", "baseline")
    monkeypatch.delenv("TENANT_ISOLATION_MODE", raising=False)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _, tracking = st.make_tables(ddb)
        yield ddb, tracking


# The cast of callers, and whether each may reach a given key.
CASES = [
    (st.claims(st.SCOPED_EMAIL, "Author"), st.OUT_OF_SCOPE_KEY, None, False),
    (st.claims(st.SCOPED_EMAIL, "Author"), st.IN_SCOPE_KEY, None, True),
    (ADMIN, st.OUT_OF_SCOPE_KEY, None, True),
    (st.claims(st.OPEN_EMAIL, "Author"), st.OUT_OF_SCOPE_KEY, None, True),
    (st.claims(st.OPEN_EMAIL, "Author"), st.IN_SCOPE_KEY, "Strict", False),
    (ADMIN, st.IN_SCOPE_KEY, "Strict", True),
]
IDS = [
    "scoped-out",
    "scoped-in",
    "admin",
    "unscoped-off",
    "unscoped-strict",
    "admin-strict",
]


def _run(monkeypatch, mode, call, reached, allowed):
    if mode:
        monkeypatch.setenv("TENANT_ISOLATION_MODE", mode)
    if allowed:
        call()
        assert reached.called, "an allowed caller must reach the operation"
    else:
        with pytest.raises(PermissionError, match=REFUSAL):
            call()
        assert not reached.called, "a refused caller must not reach the operation"


@pytest.mark.unit
@pytest.mark.parametrize("claims,key,mode,allowed", CASES, ids=IDS)
def test_delete_document(env, monkeypatch, claims, key, mode, allowed):
    ddb, _ = env
    mod = load_lambda(
        "scope_delete_document_index",
        "nested/api-resolvers/src/lambda/delete_document_resolver",
    )
    mod.dynamodb = ddb
    mod._user_scope_cache.clear()
    reached = MagicMock(return_value={"success": True})
    monkeypatch.setattr(mod, "delete_single_document", reached)
    _run(
        monkeypatch,
        mode,
        lambda: mod.handler(_ev({"objectKeys": [key]}, claims), None),
        reached,
        allowed,
    )


@pytest.mark.unit
def test_delete_refuses_a_selection_with_one_out_of_scope_key(env, monkeypatch):
    ddb, _ = env
    mod = load_lambda(
        "scope_delete_document_index",
        "nested/api-resolvers/src/lambda/delete_document_resolver",
    )
    mod.dynamodb = ddb
    mod._user_scope_cache.clear()
    reached = MagicMock(return_value={"success": True})
    monkeypatch.setattr(mod, "delete_single_document", reached)
    with pytest.raises(PermissionError):
        mod.handler(
            _ev(
                {"objectKeys": [st.IN_SCOPE_KEY, st.OUT_OF_SCOPE_KEY]},
                st.claims(st.SCOPED_EMAIL, "Author"),
            ),
            None,
        )
    # Not even the in-scope key is deleted: the check runs before anything does.
    reached.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("claims,key,mode,allowed", CASES, ids=IDS)
def test_abort_workflow(env, monkeypatch, claims, key, mode, allowed):
    ddb, _ = env
    mod = load_lambda(
        "scope_abort_workflow_index",
        "nested/api-resolvers/src/lambda/abort_workflow_resolver",
        patches={
            "idp_common.docs_service.create_document_service": lambda *a, **k: (
                MagicMock()
            )
        },
    )
    mod._dynamodb = ddb
    mod._user_scope_cache.clear()
    reached = MagicMock(return_value={"success": True})
    monkeypatch.setattr(mod, "abort_document", reached)
    _run(
        monkeypatch,
        mode,
        lambda: mod.handler(_ev({"objectKeys": [key]}, claims), None),
        reached,
        allowed,
    )


@pytest.mark.unit
def test_abort_workflow_direct_invoke_has_no_scope_to_apply(env, monkeypatch):
    """abortTestRuns invoked with no identity forwards none; IAM gated it."""
    mod = load_lambda(
        "scope_abort_workflow_index",
        "nested/api-resolvers/src/lambda/abort_workflow_resolver",
        patches={
            "idp_common.docs_service.create_document_service": lambda *a, **k: (
                MagicMock()
            )
        },
    )
    reached = MagicMock(return_value={"success": True})
    monkeypatch.setattr(mod, "abort_document", reached)
    mod.handler(
        {"arguments": {"objectKeys": [st.OUT_OF_SCOPE_KEY]}, "identity": None}, None
    )
    reached.assert_called_once_with(st.OUT_OF_SCOPE_KEY)


@pytest.mark.unit
@pytest.mark.parametrize("claims,key,mode,allowed", CASES, ids=IDS)
def test_copy_to_baseline(env, monkeypatch, claims, key, mode, allowed):
    mod = load_lambda(
        "scope_copy_to_baseline_index",
        "nested/api-resolvers/src/lambda/copy_to_baseline_resolver",
    )
    mod._user_scope_cache.clear()
    reached = MagicMock(return_value=True)
    monkeypatch.setattr(mod, "start_async_copy", reached)
    context = MagicMock()
    context.get_remaining_time_in_millis.return_value = 60000
    _run(
        monkeypatch,
        mode,
        lambda: mod.handler(_ev({"objectKey": key}, claims), context),
        reached,
        allowed,
    )


# --------------------------------------------------------------------------- #
# Test runs: scoped by the profile the run ran under
# --------------------------------------------------------------------------- #


def _put_runs(tracking):
    for run_id, profile in (("run-a", "acme-invoices"), ("run-g", "globex-w2")):
        tracking.put_item(
            Item={"PK": f"testrun#{run_id}", "SK": "metadata", "ConfigVersion": profile}
        )


def _results_module(ddb, monkeypatch):
    mod = load_lambda(
        "scope_test_results_index",
        "nested/api-resolvers/src/lambda/test_results_resolver",
    )
    mod.dynamodb = ddb
    mod._user_scope_cache.clear()
    return mod


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,args,target",
    [
        ("getTestRun", {"testRunId": "run-g"}, "get_test_results"),
        ("getTestRunStatus", {"testRunId": "run-g"}, "get_test_run_status"),
        ("compareTestRuns", {"testRunIds": ["run-a", "run-g"]}, "compare_test_runs"),
    ],
)
def test_test_run_reads_are_refused_outside_scope(
    env, monkeypatch, field, args, target
):
    ddb, tracking = env
    _put_runs(tracking)
    mod = _results_module(ddb, monkeypatch)
    reached = MagicMock(return_value={})
    monkeypatch.setattr(mod, target, reached)
    with pytest.raises(PermissionError, match=REFUSAL):
        mod.handler(_ev(args, st.claims(st.SCOPED_EMAIL, "Author"), field), None)
    reached.assert_not_called()
    # The Admin reaches it.
    mod.handler(_ev(args, ADMIN, field), None)
    reached.assert_called_once()


@pytest.mark.unit
def test_a_scoped_author_reaches_their_own_run(env, monkeypatch):
    ddb, tracking = env
    _put_runs(tracking)
    mod = _results_module(ddb, monkeypatch)
    reached = MagicMock(return_value={})
    monkeypatch.setattr(mod, "get_test_results", reached)
    mod.handler(
        _ev({"testRunId": "run-a"}, st.claims(st.SCOPED_EMAIL, "Author"), "getTestRun"),
        None,
    )
    reached.assert_called_once_with("run-a")


@pytest.mark.unit
def test_an_unknown_run_is_refused_to_a_scoped_caller(env, monkeypatch):
    ddb, tracking = env
    mod = _results_module(ddb, monkeypatch)
    with pytest.raises(PermissionError, match=REFUSAL):
        mod.handler(
            _ev(
                {"testRunId": "nope"},
                st.claims(st.SCOPED_EMAIL, "Author"),
                "getTestRun",
            ),
            None,
        )


@pytest.mark.unit
def test_the_run_list_is_filtered_to_the_callers_scope(env, monkeypatch):
    ddb, _ = env
    mod = _results_module(ddb, monkeypatch)
    runs = [
        {"testRunId": "run-a", "configVersion": "acme-invoices"},
        {"testRunId": "run-g", "configVersion": "globex-w2"},
    ]
    monkeypatch.setattr(mod, "get_test_runs", lambda *a, **k: list(runs))
    args = {"timePeriodHours": 1}
    scoped = mod.handler(
        _ev(args, st.claims(st.SCOPED_EMAIL, "Author"), "getTestRuns"), None
    )
    assert [r["testRunId"] for r in scoped] == ["run-a"]
    assert len(mod.handler(_ev(args, ADMIN, "getTestRuns"), None)) == 2
    monkeypatch.setenv("TENANT_ISOLATION_MODE", "Strict")
    assert (
        mod.handler(_ev(args, st.claims(st.OPEN_EMAIL, "Author"), "getTestRuns"), None)
        == []
    )
