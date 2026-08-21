# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the Test Set Generator feature API."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_HANDLER_DIR = Path(__file__).resolve().parents[1]
_QUEUE_NAME = "TestBootstrapQueue"
_TRACKING_TABLE = "TestBootstrapTracking"
_HOST_TABLE = "TestHostTracking"


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TRACKING_TABLE", _TRACKING_TABLE)
    monkeypatch.setenv("HOST_TRACKING_TABLE", _HOST_TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    sys.path.insert(0, str(_HANDLER_DIR))
    sys.modules.pop("handler", None)
    m = importlib.import_module("handler")
    sys.path.remove(str(_HANDLER_DIR))
    return m


def _make_queue(mod):
    sqs = boto3.client("sqs", region_name="us-west-2")
    url = sqs.create_queue(QueueName=_QUEUE_NAME)["QueueUrl"]
    mod._QUEUE_URL = url
    mod._sqs = sqs
    return url


def _make_host_table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName=_HOST_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
    )
    return ddb.Table(_HOST_TABLE)


def _post(mod, path, body, *, groups="[Admin]"):
    event = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"cognito:groups": groups}}},
        },
        "body": json.dumps(body),
    }
    return mod.lambda_handler(event, None)


class TestTestSetDest:
    def test_append_valid_id(self, mod):
        dest = mod._test_set_dest({"testSetId": "fake-w2"})
        assert dest == {
            "testSetId": "fake-w2",
            "testSetName": "fake-w2",
            "append": True,
        }

    def test_append_invalid_id_rejected(self, mod):
        with pytest.raises(mod._BadRequest):
            mod._test_set_dest({"testSetId": "bad/id"})

    def test_append_overlong_id_rejected(self, mod):
        with pytest.raises(mod._BadRequest):
            mod._test_set_dest({"testSetId": "a" * 51})

    def test_create_new_valid(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_test_set_exists", lambda _id: False)
        dest = mod._test_set_dest({"testSetName": "W2 Synthetic"})
        assert dest == {
            "testSetId": "w2-synthetic",
            "testSetName": "W2 Synthetic",
            "append": False,
        }

    def test_create_new_collision_rejected(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_test_set_exists", lambda _id: True)
        with pytest.raises(mod._BadRequest):
            mod._test_set_dest({"testSetName": "W2 Synthetic"})

    def test_create_new_bad_name_rejected(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_test_set_exists", lambda _id: False)
        with pytest.raises(mod._BadRequest):
            mod._test_set_dest({"testSetName": "bad/name"})

    def test_missing_destination_rejected(self, mod):
        with pytest.raises(mod._BadRequest):
            mod._test_set_dest({})


class TestRbac:
    @mock_aws
    def test_generate_requires_write_group(self, mod):
        resp = _post(mod, "/generate", {"prompt": "a W2"}, groups="[Viewer]")
        assert resp["statusCode"] == 403

    @mock_aws
    def test_generate_from_config_requires_write_group(self, mod):
        resp = _post(
            mod,
            "/generate-from-config",
            {"versionName": "v1", "className": "W2"},
            groups="[Viewer]",
        )
        assert resp["statusCode"] == 403

    @mock_aws
    def test_author_can_generate(self, mod):
        _make_queue(mod)
        _make_host_table()
        resp = _post(
            mod,
            "/generate",
            {"prompt": "a W2", "testSetName": "W2 Synthetic"},
            groups="[Author]",
        )
        assert resp["statusCode"] == 202
        assert json.loads(resp["body"])["jobId"]


def _make_tracking_table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    return ddb.create_table(
        TableName=_TRACKING_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
    )


def _iso(minutes_ago):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class TestReapDeadJobs:
    """A runtime that dies mid-run must not leave the UI generating forever.

    The runtime's watchdog runs inside the container, so a container killed mid-stage
    leaves the job IN_PROGRESS with nothing alive to fail it. Observed on a dev stack:
    "generating" for 68 minutes after the runtime went silent, and it would have stayed
    that way indefinitely because the state is a database record, not client state.
    """

    @mock_aws
    def test_a_job_with_a_stale_heartbeat_is_failed(self, mod):
        table = _make_tracking_table()
        table.put_item(
            Item={
                "jobId": "j1",
                "status": "IN_PROGRESS",
                "heartbeatAt": _iso(mod.STALE_HEARTBEAT_MINUTES + 5),
            }
        )

        resp = mod.lambda_handler({"rawPath": "/jobs", "httpMethod": "GET"}, None)

        assert resp["statusCode"] == 200
        stored = table.get_item(Key={"jobId": "j1"})["Item"]
        assert stored["status"] == "FAILED"
        assert "stopped responding" in stored["errorMessage"]

    @mock_aws
    def test_a_recent_heartbeat_is_left_running(self, mod):
        """A long stage that completes no documents still pulses; it is alive."""
        table = _make_tracking_table()
        table.put_item(
            Item={"jobId": "j1", "status": "IN_PROGRESS", "heartbeatAt": _iso(2)}
        )

        mod.lambda_handler({"rawPath": "/jobs", "httpMethod": "GET"}, None)

        assert table.get_item(Key={"jobId": "j1"})["Item"]["status"] == "IN_PROGRESS"

    @mock_aws
    def test_a_job_with_no_heartbeat_is_left_alone(self, mod):
        """Jobs predating heartbeating must not be presumed dead."""
        table = _make_tracking_table()
        table.put_item(Item={"jobId": "j1", "status": "IN_PROGRESS"})

        mod.lambda_handler({"rawPath": "/jobs", "httpMethod": "GET"}, None)

        assert table.get_item(Key={"jobId": "j1"})["Item"]["status"] == "IN_PROGRESS"

    @mock_aws
    def test_a_completed_job_is_never_touched(self, mod):
        table = _make_tracking_table()
        table.put_item(
            Item={
                "jobId": "j1",
                "status": "COMPLETED",
                "heartbeatAt": _iso(600),
            }
        )

        mod.lambda_handler({"rawPath": "/jobs", "httpMethod": "GET"}, None)

        assert table.get_item(Key={"jobId": "j1"})["Item"]["status"] == "COMPLETED"

    @mock_aws
    def test_an_unparseable_heartbeat_is_not_treated_as_dead(self, mod):
        table = _make_tracking_table()
        table.put_item(
            Item={"jobId": "j1", "status": "IN_PROGRESS", "heartbeatAt": "not-a-date"}
        )

        mod.lambda_handler({"rawPath": "/jobs", "httpMethod": "GET"}, None)

        assert table.get_item(Key={"jobId": "j1"})["Item"]["status"] == "IN_PROGRESS"


class TestWritePathsAreGranted:
    """Every DynamoDB write this handler makes must have an IAM grant in the template.

    Source-level, and deliberately so. The reap path shipped review-clean once with no
    write grant at all: moto does not enforce IAM, both writes are wrapped in a broad
    except, and an AccessDeniedException logged at info reads exactly like routine
    cleanup noise. So the behavioural tests above all passed against code that could
    never write in a real account. Nothing but reading the template catches that, and
    the same shape recurs every time a write is added to a function whose Policies
    block is somewhere else in a 900-line file.
    """

    # dynamodb call -> the IAM action it needs, and the SAM policy templates that imply it
    _WRITE_ACTIONS = {
        "update_item": (
            "dynamodb:UpdateItem",
            ("DynamoDBCrudPolicy", "DynamoDBWritePolicy"),
        ),
        "put_item": ("dynamodb:PutItem", ("DynamoDBCrudPolicy", "DynamoDBWritePolicy")),
        "delete_item": ("dynamodb:DeleteItem", ("DynamoDBCrudPolicy",)),
    }

    @staticmethod
    def _api_function_policies() -> str:
        """The FeatureApiFunction Policies block, as raw text.

        Not parsed YAML: the block is full of short-form intrinsics (!Sub, !GetAtt,
        !Ref) that a plain safe_load rejects, and a substring check is enough to
        answer "is this action granted anywhere for this function".
        """
        template = (_HANDLER_DIR.parent / "template.yaml").read_text(encoding="utf-8")
        start = template.index("  FeatureApiFunction:")
        policies = template.index("      Policies:", start)
        end = template.index("      Events:", policies)
        return template[policies:end]

    def test_every_write_call_has_a_matching_grant(self):
        handler_src = (_HANDLER_DIR / "handler.py").read_text(encoding="utf-8")
        policies = self._api_function_policies()

        for call, (action, templates) in self._WRITE_ACTIONS.items():
            if f".{call}(" not in handler_src:
                continue
            granted = action in policies or any(t in policies for t in templates)
            assert granted, (
                f"handler.py calls {call}() but FeatureApiFunction's Policies grant "
                f"neither {action} nor {' / '.join(templates)}. The write will be "
                f"denied at runtime and the broad except will hide it."
            )

    def test_the_host_table_grant_covers_the_release_write(self):
        """The host grant is a hand-written Statement, so it is the easiest to miss.

        _release_host_test_set writes the *host* table, which no policy template covers
        (it is an ImportValue from the main stack, not a resource in this template), so
        the previous test would pass on the bootstrap grant alone.
        """
        policies = self._api_function_policies()
        host_grants = [
            line
            for line in policies.splitlines()
            if "TrackingTableName" in line and "Bootstrap" not in line
        ]
        assert host_grants, "no grant references the host TrackingTableName at all"

        host_block = policies[policies.index("TrackingTableName") - 600 :]
        assert "dynamodb:UpdateItem" in host_block, (
            "the host TrackingTable grant is read-only, so _release_host_test_set "
            "cannot clear a reaped job's test set off GENERATING — the spinner then "
            "waits for the host resolver's much longer window instead."
        )
