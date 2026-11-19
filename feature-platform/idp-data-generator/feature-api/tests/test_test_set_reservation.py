# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A new destination set's id is claimed by a conditional create, and released if
the job never starts generating.

Both halves live in this feature, so both are tested here: the feature API writes
the claim (``_reserve_test_set``), and the bootstrap processor fails it when a job
ends before invoking the generator (``_release_reserved_test_set``). The host table
is built from the parent template's ``TrackingTable`` definition, so the keys and
the index the host lists sets through are the deployed ones.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from boto3.dynamodb.conditions import Key
from moto import mock_aws

_FEATURE_DIR = Path(__file__).resolve().parents[2]
_API_DIR = _FEATURE_DIR / "feature-api"
_PROCESSOR = _FEATURE_DIR / "bootstrap-processor" / "index.py"
_REPO_ROOT = _FEATURE_DIR.parents[1]
_REGION = "us-west-2"
_HOST_TABLE = "HostTracking"
_QUEUE_NAME = "BootstrapQueue"


def _tracking_table_definition():
    """The parent template's TrackingTable keys and indexes, as create_table takes them."""

    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node)}
        return {suffix: loader.construct_mapping(node)}

    _Loader.add_multi_constructor("!", _tag)
    text = (_REPO_ROOT / "template.yaml").read_text(encoding="utf-8")
    props = yaml.load(text, Loader=_Loader)["Resources"]["TrackingTable"][  # nosec B506
        "Properties"
    ]
    definition = {
        key: props[key]
        for key in ("AttributeDefinitions", "KeySchema", "GlobalSecondaryIndexes")
    }
    assert {k["AttributeName"] for k in definition["KeySchema"]} == {"PK", "SK"}
    return definition


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("HOST_TRACKING_TABLE", _HOST_TABLE)
    monkeypatch.setenv("BOOTSTRAP_TRACKING_TABLE", "BootstrapTracking")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.create_table(
            TableName=_HOST_TABLE,
            BillingMode="PAY_PER_REQUEST",
            **_tracking_table_definition(),
        )
        yield table


@pytest.fixture
def api(aws, monkeypatch):
    sys.path.insert(0, str(_API_DIR))
    sys.modules.pop("handler", None)
    try:
        mod = importlib.import_module("handler")
    finally:
        sys.path.remove(str(_API_DIR))
    sqs = boto3.client("sqs", region_name=_REGION)
    mod._QUEUE_URL = sqs.create_queue(QueueName=_QUEUE_NAME)["QueueUrl"]
    mod._sqs = sqs
    mod._dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return mod


@pytest.fixture
def processor(aws, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "idp_data_generator_bootstrap_processor", _PROCESSOR
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ddb = boto3.resource("dynamodb", region_name=_REGION)
    # The status writes go to the feature's own job table, which these tests do not
    # look at.
    monkeypatch.setattr(mod, "_status", lambda *a, **k: None)
    return mod


def _post(api, path, body):
    event = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"cognito:groups": "[Author]"}}},
        },
        "body": json.dumps(body),
    }
    return api.lambda_handler(event, None)


def _row(table, test_set_id):
    return table.get_item(Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}).get(
        "Item"
    )


def _queued(api):
    sqs = api._sqs
    msgs = sqs.receive_message(QueueUrl=api._QUEUE_URL, MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in msgs.get("Messages", [])]


def _user_set(table, test_set_id="w2-synthetic"):
    """A set Test Studio created, in the shape its create path writes."""
    item = {
        "PK": f"testset#{test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": "2026-01-01T00:00:00Z",
        "id": test_set_id,
        "name": "W2 Synthetic",
        "description": "mine",
        "source": "uploaded",
        "status": "COMPLETED",
        "createdAt": "2026-01-01T00:00:00Z",
    }
    table.put_item(Item=item)
    return item


@pytest.mark.unit
class TestTheCreateIsTheClaim:
    def test_a_new_set_is_created_before_the_job_is_queued(self, api, aws):
        resp = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"}
        )

        assert resp["statusCode"] == 202, resp["body"]
        row = _row(aws, "w2-synthetic")
        assert row is not None
        assert row["status"] == "GENERATING"
        assert row["ItemType"] == "testset"
        assert row["name"] == "W2 Synthetic"
        assert row["InitialEventTime"] == row["createdAt"] == row["statusUpdatedAt"]
        [message] = _queued(api)
        assert message["testSetId"] == "w2-synthetic"
        assert message["append"] is False

    def test_the_claimed_set_is_listed_through_the_hosts_index(self, api, aws):
        """getTestSets finds sets through TypeDateIndex, so the claim must carry its
        keys; a row without them would be invisible there while holding the id."""
        _post(api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"})

        found = aws.query(
            IndexName="TypeDateIndex",
            KeyConditionExpression=Key("ItemType").eq("testset"),
        )["Items"]
        assert [i["PK"] for i in found] == ["testset#w2-synthetic"]

    def test_a_second_request_for_the_same_new_name_is_refused(self, api, aws):
        first = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"}
        )
        second = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "w2 synthetic"}
        )

        assert first["statusCode"] == 202
        assert second["statusCode"] == 400
        assert json.loads(second["body"]) == {
            "error": "A test set with id 'w2-synthetic' already exists"
        }
        assert len(_queued(api)) == 1

    def test_a_set_test_studio_created_is_refused_and_left_alone(self, api, aws):
        before = _user_set(aws)

        resp = _post(
            api,
            "/generate-from-config",
            {"versionName": "v1", "className": "W2", "testSetName": "W2 Synthetic"},
        )

        assert resp["statusCode"] == 400
        assert "already exists" in json.loads(resp["body"])["error"]
        assert _row(aws, "w2-synthetic") == before
        assert _queued(api) == []

    def test_a_failed_write_fails_the_request_instead_of_letting_it_through(
        self, api, aws
    ):
        aws.delete()  # any failure other than the refusal, e.g. the table gone

        resp = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"}
        )

        assert resp["statusCode"] == 500
        assert _queued(api) == []

    def test_an_unconfigured_host_table_fails_the_request(self, api, monkeypatch):
        monkeypatch.setattr(api, "_HOST_TRACKING_TABLE", "")

        resp = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"}
        )

        assert resp["statusCode"] == 500
        assert _queued(api) == []

    def test_an_append_claims_nothing_and_leaves_the_set_alone(self, api, aws):
        before = _user_set(aws)

        resp = _post(api, "/generate", {"prompt": "a W2", "testSetId": "w2-synthetic"})

        assert resp["statusCode"] == 202
        assert _row(aws, "w2-synthetic") == before
        [message] = _queued(api)
        assert message["append"] is True

    def test_an_invalid_request_claims_nothing(self, api, aws):
        resp = _post(
            api,
            "/generate",
            {"prompt": "a W2", "testSetName": "W2 Synthetic", "docCount": 0},
        )

        assert resp["statusCode"] == 400
        assert _row(aws, "w2-synthetic") is None

    def test_a_claim_whose_job_could_not_be_queued_is_released(
        self, api, aws, monkeypatch
    ):
        def refuse(**_kwargs):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(api._sqs, "send_message", refuse)

        resp = _post(
            api, "/generate", {"prompt": "a W2", "testSetName": "W2 Synthetic"}
        )

        assert resp["statusCode"] == 500
        row = _row(aws, "w2-synthetic")
        assert row["status"] == "FAILED"
        assert row["error"] == "The generation job could not be queued"


def _record(body):
    return {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}


def _new_set_job(test_set_id="w2-synthetic", append=False):
    return {
        "jobId": "j1",
        "prompt": "a W2",
        "testSetId": test_set_id,
        "testSetName": "W2 Synthetic",
        "append": append,
        "claimed": not append,
    }


@pytest.mark.unit
class TestTheProcessorReleasesAnUnusedClaim:
    @staticmethod
    def _claim(api, name="W2 Synthetic"):
        """Submit a generation through the API; return the job message it queued."""
        resp = _post(api, "/generate", {"prompt": "a W2", "testSetName": name})
        assert resp["statusCode"] == 202, resp["body"]
        [message] = _queued(api)
        return message

    def test_the_claim_names_its_job_under_the_processors_attribute(
        self, api, aws, processor
    ):
        job = self._claim(api)

        assert job["claimed"] is True
        assert _row(aws, "w2-synthetic")[processor.CLAIM_ATTRIBUTE] == job["jobId"]

    def test_a_job_sqs_will_retry_keeps_its_claim(
        self, api, processor, aws, monkeypatch
    ):
        """The message is redelivered and the claim is what the retry generates
        into, so it is not failed (which would also invite deleting it)."""
        job = self._claim(api)

        def boom(_job_id, _body):
            raise RuntimeError("throttled")

        monkeypatch.setattr(processor, "_process_job", boom)
        out = processor.handler(_record(job), None)

        assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
        assert _row(aws, "w2-synthetic")["status"] == "GENERATING"

    def test_a_retry_never_takes_over_a_set_created_under_the_id_since(
        self, api, processor, aws, monkeypatch
    ):
        """The claim was deleted and a user created a set with the same id before
        the redelivery: the retry neither generates into it nor fails it."""
        job = self._claim(api)
        aws.delete_item(Key={"PK": "testset#w2-synthetic", "SK": "metadata"})
        before = _user_set(aws)
        invocations = self._real_job(processor, monkeypatch)
        statuses = []
        monkeypatch.setattr(
            processor, "_status", lambda *a, **k: statuses.append((a, k))
        )

        out = processor.handler(_record(job), None)

        assert out == {"batchItemFailures": []}
        assert invocations == []
        assert _row(aws, "w2-synthetic") == before
        assert statuses[-1][0][1] == "FAILED"
        assert "deleted, replaced or already finished" in statuses[-1][1]["error"]

    def test_a_retry_never_takes_over_another_jobs_claim(
        self, api, processor, aws, monkeypatch
    ):
        job = self._claim(api)
        aws.delete_item(Key={"PK": "testset#w2-synthetic", "SK": "metadata"})
        other = self._claim(api)
        invocations = self._real_job(processor, monkeypatch)

        processor.handler(_record(job), None)

        assert invocations == []
        row = _row(aws, "w2-synthetic")
        assert row["status"] == "GENERATING"
        assert row[processor.CLAIM_ATTRIBUTE] == other["jobId"]

    def test_a_job_that_returns_without_generating_fails_the_claim(
        self, api, processor, aws, monkeypatch
    ):
        """The real early return: the schema could not be authored."""
        import idp_common.config.configuration_manager as cm

        job = self._claim(api)
        monkeypatch.setattr(cm, "ConfigurationManager", lambda: object())
        monkeypatch.setattr(
            processor.bootstrap_mod,
            "resolve_schema",
            lambda *a, **k: (None, "authored", None),
        )

        out = processor.handler(_record(job), None)

        assert out == {"batchItemFailures": []}
        row = _row(aws, "w2-synthetic")
        assert row["status"] == "FAILED"
        assert row["error"] == processor._NOT_GENERATED

    @staticmethod
    def _real_job(processor, monkeypatch, *, raw_config=None, runtime_arn="arn:rt"):
        """Run the real ``_process_job`` with its external calls stubbed.

        Returns the list the generator invocations are recorded in.
        """
        import idp_common.config.configuration_manager as cm

        class _Config:
            def get_raw_configuration(self, _kind, _version):
                return raw_config

        schema = {"$id": "W2", "type": "object", "properties": {"a": {}}}
        monkeypatch.setattr(cm, "ConfigurationManager", _Config)
        monkeypatch.setattr(
            processor.bootstrap_mod,
            "resolve_schema",
            lambda *a, **k: (schema, "authored", None),
        )
        monkeypatch.setattr(
            processor.bootstrap_mod, "merge_class_into_version", lambda *a, **k: None
        )
        monkeypatch.setattr(processor, "_stage_schema_dir", lambda *a: None)
        monkeypatch.setattr(processor, "SYNTHESIS_RUNTIME_ARN", runtime_arn)
        invocations = []

        class _AgentCore:
            def invoke_agent_runtime(self, **kwargs):
                invocations.append(kwargs)

        real_client = processor.boto3.client
        monkeypatch.setattr(
            processor.boto3,
            "client",
            lambda name, *a, **k: (
                _AgentCore()
                if name == "bedrock-agentcore"
                else real_client(name, *a, **k)
            ),
        )
        return invocations

    def test_a_job_that_started_the_generator_keeps_the_claim(
        self, api, processor, aws, monkeypatch
    ):
        job = self._claim(api)
        invocations = self._real_job(processor, monkeypatch)

        out = processor.handler(_record(job), None)

        assert out == {"batchItemFailures": []}
        assert len(invocations) == 1
        assert _row(aws, "w2-synthetic")["status"] == "GENERATING"

    def test_a_job_whose_class_is_missing_fails_the_claim(
        self, api, processor, aws, monkeypatch
    ):
        claimed = self._claim(api)
        invocations = self._real_job(processor, monkeypatch, raw_config={"classes": []})
        job = {
            **claimed,
            "fromExistingConfig": True,
            "configVersion": "v1",
            "className": "W2",
        }

        processor.handler(_record(job), None)

        assert invocations == []
        assert _row(aws, "w2-synthetic")["status"] == "FAILED"

    def test_a_job_with_no_generator_runtime_fails_the_claim(
        self, api, processor, aws, monkeypatch
    ):
        job = self._claim(api)
        invocations = self._real_job(processor, monkeypatch, runtime_arn="")

        processor.handler(_record(job), None)

        assert invocations == []
        assert _row(aws, "w2-synthetic")["status"] == "FAILED"

    def test_an_append_job_never_fails_the_existing_set(
        self, processor, aws, monkeypatch
    ):
        item = _user_set(aws)
        aws.update_item(
            Key={"PK": item["PK"], "SK": "metadata"},
            UpdateExpression="SET #s = :g, #c = :j",
            ExpressionAttributeNames={"#s": "status", "#c": processor.CLAIM_ATTRIBUTE},
            ExpressionAttributeValues={":g": "GENERATING", ":j": "j1"},
        )
        monkeypatch.setattr(processor, "_process_job", lambda _j, _b: False)
        # Even a message that says otherwise: an append never fails the set.
        job = {**_new_set_job(append=True), "claimed": True}

        processor.handler(_record(job), None)

        assert _row(aws, "w2-synthetic")["status"] == "GENERATING"

    def test_a_status_written_since_is_kept(self, processor, aws, monkeypatch):
        before = _user_set(aws)
        monkeypatch.setattr(processor, "_process_job", lambda _j, _b: False)

        processor.handler(_record(_new_set_job()), None)

        assert _row(aws, "w2-synthetic") == before

    def test_a_message_queued_before_claims_existed_fails_nothing(
        self, processor, aws, monkeypatch
    ):
        """An old-shape message has no claim to release, so a GENERATING record
        under its id is left for the host reaper, as before."""
        aws.put_item(
            Item={
                "PK": "testset#w2-synthetic",
                "SK": "metadata",
                "ItemType": "testset",
                "status": "GENERATING",
            }
        )
        monkeypatch.setattr(processor, "_process_job", lambda _j, _b: False)
        job = {k: v for k, v in _new_set_job().items() if k != "claimed"}

        processor.handler(_record(job), None)

        assert _row(aws, "w2-synthetic")["status"] == "GENERATING"

    @pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
    def test_a_late_delivery_never_reopens_a_finished_claim(
        self, api, processor, aws, monkeypatch, status
    ):
        """COMPLETED: a duplicate delivery after the set was generated. FAILED: a
        retry after the host reaper gave up on the claim."""
        job = self._claim(api)
        aws.update_item(
            Key={"PK": "testset#w2-synthetic", "SK": "metadata"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
        invocations = self._real_job(processor, monkeypatch)

        processor.handler(_record(job), None)

        assert invocations == []
        assert _row(aws, "w2-synthetic")["status"] == status
