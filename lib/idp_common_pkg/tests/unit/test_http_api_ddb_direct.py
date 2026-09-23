# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the HTTP API dispatcher's DynamoDB-direct module (ddb_direct.py),
which ports the AppSync VTL DynamoDB resolvers for discovery + agent jobs.

The dispatcher lives outside the package, so we import it by path.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    """Walk up until we find the repo root (contains nested/api-resolvers)."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_DISPATCHER_DIR = (
    _find_repo_root()
    / "nested"
    / "api-resolvers"
    / "src"
    / "lambda"
    / "http_api_dispatcher"
)


def _load_ddb_direct():
    spec = importlib.util.spec_from_file_location(
        "ddb_direct", _DISPATCHER_DIR / "ddb_direct.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ddb_direct"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ddb_env():
    os.environ["TRACKING_TABLE_NAME"] = "TrackingTable"
    os.environ["DISCOVERY_TABLE_NAME"] = "DiscoveryTable"
    os.environ["AGENT_TABLE_NAME"] = "AgentTable"
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="TrackingTable",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="DiscoveryTable",
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="AgentTable",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # ddb_direct binds boto3.resource at import; import inside the mock.
        mod = _load_ddb_direct()
        yield mod, ddb


def _ev(args, username="user@example.com", groups=("Admin",)):
    # Default to an Admin caller so RBAC-gated ops pass; individual tests pass
    # groups=... to exercise the group checks that mirror the AppSync schema.
    return {
        "arguments": args,
        "identity": {
            "username": username,
            "sub": "sub-1",
            "claims": {"cognito:groups": list(groups)},
        },
    }


# ----------------------------- documents ----------------------------------- #
def test_get_document_returns_raw_item(ddb_env):
    mod, ddb = ddb_env
    ddb.Table("TrackingTable").put_item(
        Item={
            "PK": "doc#s3://bucket/key.pdf",
            "SK": "none",
            "ObjectKey": "s3://bucket/key.pdf",
            "ObjectStatus": "COMPLETED",
            "PageCount": 5,
        }
    )
    out = mod.dispatch("getDocument", _ev({"ObjectKey": "s3://bucket/key.pdf"}))
    assert out["ObjectKey"] == "s3://bucket/key.pdf"
    assert out["ObjectStatus"] == "COMPLETED"
    assert out["PageCount"] == 5  # Decimal coerced to int


def test_get_document_missing_returns_none(ddb_env):
    mod, _ = ddb_env
    assert mod.dispatch("getDocument", _ev({"ObjectKey": "nope"})) is None


def test_list_documents_date_hour(ddb_env):
    mod, ddb = ddb_env
    table = ddb.Table("TrackingTable")
    # hour 14 -> shard 14//4 = 3 -> shardPad "03"; PK list#2026-06-29#s#03
    table.put_item(
        Item={
            "PK": "list#2026-06-29#s#03",
            "SK": "ts#2026-06-29T14:05:00#doc1",
            "ObjectKey": "doc1",
        }
    )
    out = mod.dispatch("listDocumentsDateHour", _ev({"date": "2026-06-29", "hour": 14}))
    assert len(out["Documents"]) == 1
    assert out["Documents"][0]["ObjectKey"] == "doc1"


def test_list_documents_date_shard(ddb_env):
    mod, ddb = ddb_env
    ddb.Table("TrackingTable").put_item(
        Item={"PK": "list#2026-06-29#s#02", "SK": "ts#x", "ObjectKey": "d2"}
    )
    out = mod.dispatch(
        "listDocumentsDateShard", _ev({"date": "2026-06-29", "shard": 2})
    )
    assert [d["ObjectKey"] for d in out["Documents"]] == ["d2"]


def test_list_documents_invalid_hour(ddb_env):
    mod, _ = ddb_env
    with pytest.raises(ValueError):
        mod.dispatch("listDocumentsDateHour", _ev({"date": "2026-06-29", "hour": 99}))


# --------------------------- discovery jobs --------------------------------- #
# NB: updateDiscoveryJobStatus / updateAgentJobStatus are _IAM_ONLY at the
# dispatch() layer (backend workers write DynamoDB directly; these fields are
# never callable by a Cognito user). Tests seed via the underlying handler
# (mod._DISPATCH[...]) to bypass the RBAC gate, exactly as the backend would
# mutate the table, then assert the user-facing read ops.
def _seed_discovery(mod, args):
    return mod._DISPATCH["updateDiscoveryJobStatus"]({"arguments": args})


def _seed_agent_update(mod, args):
    return mod._DISPATCH["updateAgentJobStatus"]({"arguments": args})


def test_discovery_update_and_list(ddb_env):
    mod, _ = ddb_env
    _seed_discovery(mod, {"jobId": "j1", "status": "IN_PROGRESS"})
    out = mod.dispatch("listDiscoveryJobs", _ev({}))
    jobs = out["DiscoveryJobs"]
    assert len(jobs) == 1
    assert jobs[0]["jobId"] == "j1"
    assert jobs[0]["status"] == "IN_PROGRESS"
    assert "updatedAt" in jobs[0]
    assert "completedAt" not in jobs[0]  # not terminal


def test_discovery_terminal_sets_completedat(ddb_env):
    mod, _ = ddb_env
    res = _seed_discovery(
        mod, {"jobId": "j2", "status": "COMPLETED", "clustersFound": 3}
    )
    assert res["status"] == "COMPLETED"
    assert res["completedAt"]
    assert res["clustersFound"] == 3


def test_discovery_invalid_status_raises(ddb_env):
    mod, _ = ddb_env
    with pytest.raises(ValueError):
        _seed_discovery(mod, {"jobId": "j3", "status": "BOGUS"})


def test_discovery_delete(ddb_env):
    mod, _ = ddb_env
    _seed_discovery(mod, {"jobId": "j4", "status": "QUEUED"})
    assert mod.dispatch("deleteDiscoveryJob", _ev({"jobId": "j4"})) is True
    out = mod.dispatch("listDiscoveryJobs", _ev({}))
    assert out["DiscoveryJobs"] == []


# ------------------------------ RBAC -------------------------------------- #
def test_iam_only_ops_reject_cognito_caller(ddb_env):
    """updateDiscoveryJobStatus / updateAgentJobStatus are backend-only; a
    Cognito user (any group) must never reach them via dispatch()."""
    mod, _ = ddb_env
    for field in ("updateDiscoveryJobStatus", "updateAgentJobStatus"):
        with pytest.raises(PermissionError):
            mod.dispatch(
                field, _ev({"jobId": "x", "status": "QUEUED"}, groups=("Admin",))
            )


def test_group_restricted_ops_enforce_groups(ddb_env):
    mod, _ = ddb_env
    # listDiscoveryJobs/deleteDiscoveryJob require Admin/Author.
    for field, args in (
        ("listDiscoveryJobs", {}),
        ("deleteDiscoveryJob", {"jobId": "z"}),
    ):
        with pytest.raises(PermissionError):
            mod.dispatch(field, _ev(args, groups=("Viewer",)))
        with pytest.raises(PermissionError):
            mod.dispatch(field, _ev(args, groups=()))
    # agent read ops require Admin/Author/Viewer (Reviewer excluded).
    with pytest.raises(PermissionError):
        mod.dispatch("listAgentJobs", _ev({}, groups=("Reviewer",)))
    # getDocument and deleteAgentJob name no particular group but do require an
    # assigned one (`ANY_GROUP`): document content is not readable by a caller an
    # administrator has not onboarded, which self-service sign-up produces. Any one
    # group is enough, so the lowest-privilege role stands in for all five.
    mod.dispatch("getDocument", _ev({"ObjectKey": "nope"}, groups=("Viewer",)))
    with pytest.raises(PermissionError, match="requires an assigned group"):
        mod.dispatch("getDocument", _ev({"ObjectKey": "nope"}, groups=()))
    # listDocumentsDateHour is ANY_GROUP too: the index rows it returns carry
    # ObjectKey with no filtering, which is where a caller gets the keys the rest of
    # the chain in issue #1033 needs.
    mod.dispatch(
        "listDocumentsDateHour",
        _ev({"date": "2024-01-01", "hour": 0}, groups=("Viewer",)),
    )
    with pytest.raises(PermissionError, match="requires an assigned group"):
        mod.dispatch(
            "listDocumentsDateHour", _ev({"date": "2024-01-01", "hour": 0}, groups=())
        )


# ----------------------------- agent jobs ---------------------------------- #
def test_agent_job_user_scoping(ddb_env):
    """A user must only see their own agent jobs (PK = agent#<email>)."""
    mod, ddb = ddb_env
    table = ddb.Table("AgentTable")
    table.put_item(
        Item={"PK": "agent#alice@x.com", "SK": "jobA", "status": "COMPLETED"}
    )
    table.put_item(Item={"PK": "agent#bob@x.com", "SK": "jobB", "status": "RUNNING"})

    # Alice sees only her job
    alice = mod.dispatch("listAgentJobs", _ev({}, username="alice@x.com"))
    assert [j["jobId"] for j in alice["items"]] == ["jobA"]

    # Bob's getAgentJobStatus for Alice's job returns None (scoped out)
    miss = mod.dispatch(
        "getAgentJobStatus", _ev({"jobId": "jobA"}, username="bob@x.com")
    )
    assert miss is None

    hit = mod.dispatch(
        "getAgentJobStatus", _ev({"jobId": "jobB"}, username="bob@x.com")
    )
    assert hit["status"] == "RUNNING"


def test_agent_update_status_terminal(ddb_env):
    mod, ddb = ddb_env
    ddb.Table("AgentTable").put_item(
        Item={"PK": "agent#carol@x.com", "SK": "jC", "status": "RUNNING"}
    )
    ok = _seed_agent_update(
        mod,
        {
            "jobId": "jC",
            "userId": "carol@x.com",
            "status": "COMPLETED",
            "result": "{}",
        },
    )
    assert ok is True
    got = mod.dispatch(
        "getAgentJobStatus", _ev({"jobId": "jC"}, username="carol@x.com")
    )
    assert got["status"] == "COMPLETED"
    assert got["completedAt"]


def test_agent_delete(ddb_env):
    mod, ddb = ddb_env
    ddb.Table("AgentTable").put_item(
        Item={"PK": "agent#dave@x.com", "SK": "jD", "status": "RUNNING"}
    )
    assert (
        mod.dispatch("deleteAgentJob", _ev({"jobId": "jD"}, username="dave@x.com"))
        is True
    )
    assert (
        mod.dispatch("getAgentJobStatus", _ev({"jobId": "jD"}, username="dave@x.com"))
        is None
    )


def test_handles_known_and_unknown():
    mod = _load_ddb_direct()
    assert mod.handles("listDiscoveryJobs")
    assert mod.handles("getAgentJobStatus")
    assert not mod.handles("listDocuments")


def test_a_caller_supplied_limit_is_clamped_not_passed_through(ddb_env):
    """`Limit` went straight to DynamoDB with no ceiling.

    The dispatcher's central validation spec carries type shapes only — there is no
    `maximum` vocabulary in it — so a numeric argument is bounded in the handler or
    not at all.
    """
    mod, ddb = ddb_env
    table = ddb.Table("AgentTable")
    for i in range(5):
        table.put_item(
            Item={"PK": "agent#eve@x.com", "SK": f"j{i}", "status": "COMPLETED"}
        )

    assert mod._clamped_limit(10_000) == mod._MAX_PAGE_SIZE
    assert mod._clamped_limit(3) == 3

    # An oversized request still answers, bounded.
    out = mod.dispatch("listAgentJobs", _ev({"limit": 10_000}, username="eve@x.com"))
    assert len(out["items"]) == 5


def test_a_non_positive_limit_is_a_client_error_not_a_server_fault(ddb_env):
    """DynamoDB rejects `Limit <= 0` with a ValidationException, which the
    dispatcher reports as 500. It is a caller input error."""
    mod, ddb = ddb_env
    ddb.Table("AgentTable").put_item(
        Item={"PK": "agent#zed@x.com", "SK": "j0", "status": "COMPLETED"}
    )

    out = mod.dispatch("listAgentJobs", _ev({"limit": -1}, username="zed@x.com"))

    assert len(out["items"]) == 1


def test_a_non_numeric_limit_is_a_valueerror_so_the_dispatcher_reports_400():
    mod = _load_ddb_direct()
    with pytest.raises(ValueError, match="limit must be an integer"):
        mod._clamped_limit("not-a-number")
