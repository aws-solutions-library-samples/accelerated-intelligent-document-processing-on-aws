# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ConfBench writes host test-set records under fixed ids; a user's set keeps its id.

Test Studio lets a user create a set called ``confbench``. The ingest used to write
the host record unconditionally, replacing that set's status and source and then
adding ConfBench's files to its folder, and the delete route removed that set's
files and record. Both now tell their own record apart by its ``source``.

Real DynamoDB semantics come from moto over a table shaped like the host's
``TrackingTable``, read from the parent ``template.yaml``. The planner is loaded
with ``huggingface_hub`` and ``pyarrow`` stubbed, so these tests run where those
heavy pins are not installed (CI), unlike ``test_planner.py``.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from moto import mock_aws

_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ROOT.parents[1]
_TRACKING = "test-tracking-table"
_JOBS = "test-job-table"
_BUCKET = "test-testset-bucket"
_SOURCE = "huggingface:amazon/ConfBench"


def _template_resource(path, name):
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node)}
        return {suffix: loader.construct_mapping(node)}

    _Loader.add_multi_constructor("!", _tag)
    with open(path, encoding="utf-8") as f:
        template = yaml.load(f, Loader=_Loader)  # nosec B506 - SafeLoader subclass
    props = template["Resources"][name]["Properties"]
    return {
        key: props[key]
        for key in ("AttributeDefinitions", "KeySchema", "GlobalSecondaryIndexes")
        if key in props
    }


def _load_planner():
    stubs = {
        "huggingface_hub": MagicMock(name="huggingface_hub"),
        "pyarrow": MagicMock(name="pyarrow"),
        "pyarrow.parquet": MagicMock(name="pyarrow.parquet"),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "confbench_planner_under_test", _ROOT / "ingest" / "planner.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        tracking_shape = _template_resource(
            _REPO_ROOT / "template.yaml", "TrackingTable"
        )
        # Non-empty, or a renamed resource would build a table with no key at all.
        assert {k["AttributeName"] for k in tracking_shape["KeySchema"]} == {
            "PK",
            "SK",
        }
        tracking = ddb.create_table(
            TableName=_TRACKING, BillingMode="PAY_PER_REQUEST", **tracking_shape
        )
        jobs = ddb.create_table(
            TableName=_JOBS,
            BillingMode="PAY_PER_REQUEST",
            **_template_resource(_ROOT / "template.yaml", "IngestJobTable"),
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        yield SimpleNamespace(ddb=ddb, tracking=tracking, jobs=jobs, s3=s3)


def _user_set(aws, test_set_id="confbench"):
    row = {
        "PK": f"testset#{test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": "2026-01-01T00:00:00Z",
        "id": test_set_id,
        "name": "Mine",
        "description": "a set a user made in Test Studio",
        "source": "uploaded",
        "status": "COMPLETED",
        "fileCount": 2,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    aws.tracking.put_item(Item=row)
    return row


def _row(aws, test_set_id="confbench"):
    return aws.tracking.get_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}
    ).get("Item")


def _keys(aws, prefix):
    listed = aws.s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)
    return sorted(o["Key"] for o in listed.get("Contents", []))


@pytest.fixture
def planner(aws):
    module = _load_planner()
    assert module.DATASET_SOURCE == _SOURCE
    download = MagicMock(name="hf_hub_download", return_value="/nonexistent.parquet")
    with (
        patch.object(module, "_ddb", aws.ddb),
        patch.object(module, "_s3", aws.s3),
        patch.object(module, "HOST_TRACKING_TABLE", _TRACKING),
        patch.object(module, "JOB_TABLE", _JOBS),
        patch.object(module, "TESTSET_BUCKET", _BUCKET),
        patch.object(module, "hf_hub_download", download),
    ):
        yield SimpleNamespace(module=module, download=download)


def _plan(planner, test_set_id="confbench"):
    return planner.module.plan_handler(
        {"jobId": "j1", "testSetId": test_set_id, "variants": ["original"]}, None
    )


@pytest.mark.unit
class TestTheIngestKeepsAUserSet:
    def test_the_plan_refuses_before_downloading_or_writing(self, aws, planner):
        original = _user_set(aws)

        with pytest.raises(planner.module.TestSetIdInUse, match="'confbench'"):
            _plan(planner)

        assert _row(aws) == original
        planner.download.assert_not_called()
        assert _keys(aws, "") == []

    def test_the_failure_handler_leaves_the_user_set_alone(self, aws, planner):
        original = _user_set(aws)

        result = planner.module.fail_handler(
            {"jobId": "j1", "testSetId": "confbench", "error": {"Error": "x"}}, None
        )

        assert result["status"] == "FAILED"
        assert _row(aws) == original

    def test_the_finalizer_leaves_a_set_created_mid_ingest_alone(self, aws, planner):
        original = _user_set(aws)

        planner.module.finalize_handler(
            {"jobId": "j1", "testSetId": "confbench", "results": []}, None
        )

        assert _row(aws) == original

    def test_its_own_record_is_still_claimed_and_refreshed(self, aws, planner):
        # The other half: a condition refusing everything would pass the above.
        planner.module._register_test_set("confbench", "IN_PROGRESS", file_count=None)
        assert _row(aws)["source"] == _SOURCE

        planner.module._register_test_set("confbench", "COMPLETED", file_count=5)

        row = _row(aws)
        assert row["status"] == "COMPLETED"
        assert row["fileCount"] == 5

    def test_a_failure_other_than_the_condition_is_still_swallowed(self, aws, planner):
        aws.tracking.delete()

        planner.module._register_test_set("confbench", "FAILED", file_count=None)


@pytest.fixture
def api(aws):
    import handler as api_handler

    sfn = MagicMock(name="sfn")
    sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution:x"}
    with (
        patch.object(api_handler, "_ddb", aws.ddb),
        patch.object(api_handler, "_s3", aws.s3),
        patch.object(api_handler, "_sfn", sfn),
        patch.object(api_handler, "_HOST_TRACKING_TABLE", _TRACKING),
        patch.object(api_handler, "_JOB_TABLE", _JOBS),
        patch.object(api_handler, "_TESTSET_BUCKET", _BUCKET),
    ):
        yield SimpleNamespace(module=api_handler, sfn=sfn)


def _call(api, method, path, body=None):
    event = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": {"cognito:groups": "[Admin]"}}},
        },
        "body": json.dumps(body) if body is not None else None,
    }
    return api.module.lambda_handler(event, None)


@pytest.mark.unit
class TestTheApiKeepsAUserSet:
    def test_starting_an_ingest_onto_a_user_set_is_refused(self, aws, api):
        original = _user_set(aws)

        resp = _call(api, "POST", "/ingest", {"tier": "full"})

        assert resp["statusCode"] == 409, resp
        assert "not created by ConfBench" in json.loads(resp["body"])["error"]
        api.sfn.start_execution.assert_not_called()
        assert _row(aws) == original

    def test_deleting_a_user_set_through_this_route_is_refused(self, aws, api):
        original = _user_set(aws)
        aws.s3.put_object(Bucket=_BUCKET, Key="confbench/input/a.pdf", Body=b"x")

        resp = _call(api, "DELETE", "/dataset/confbench")

        assert resp["statusCode"] == 409
        assert _row(aws) == original
        assert _keys(aws, "confbench/") == ["confbench/input/a.pdf"]

    def test_its_own_set_is_deleted_with_its_record(self, aws, api):
        aws.tracking.put_item(
            Item={**_user_set(aws), "source": _SOURCE, "status": "COMPLETED"}
        )
        aws.s3.put_object(Bucket=_BUCKET, Key="confbench/input/a.pdf", Body=b"x")

        resp = _call(api, "DELETE", "/dataset/confbench")

        assert resp["statusCode"] == 200
        assert _row(aws) is None
        assert _keys(aws, "confbench/") == []

    def test_keys_delete_objects_reports_failing_keep_the_record(self, aws, api):
        aws.tracking.put_item(Item={**_user_set(aws), "source": _SOURCE})
        for name in ("a", "b"):
            aws.s3.put_object(
                Bucket=_BUCKET, Key=f"confbench/input/{name}.pdf", Body=b"x"
            )
        real_delete = aws.s3.delete_objects
        stuck = "confbench/input/b.pdf"

        def delete_all_but_one(**kwargs):
            batch = [o for o in kwargs["Delete"]["Objects"] if o["Key"] != stuck]
            result = real_delete(Bucket=kwargs["Bucket"], Delete={"Objects": batch})
            result["Errors"] = [{"Key": stuck, "Code": "AccessDenied"}]
            return result

        with patch.object(aws.s3, "delete_objects", side_effect=delete_all_but_one):
            resp = _call(api, "DELETE", "/dataset/confbench")

        assert resp["statusCode"] == 500
        assert stuck in json.loads(resp["body"])["error"]
        assert _row(aws) is not None
        assert _keys(aws, "confbench/") == [stuck]

    def test_a_record_written_during_the_delete_is_kept(self, aws, api):
        # The check is a read; a set created in Test Studio while the files go
        # must keep its record.
        aws.tracking.put_item(Item={**_user_set(aws), "source": _SOURCE})
        aws.s3.put_object(Bucket=_BUCKET, Key="confbench/input/a.pdf", Body=b"x")
        real_delete = aws.s3.delete_objects
        created = {}

        def user_creates_mid_delete(**kwargs):
            result = real_delete(**kwargs)
            created["row"] = _user_set(aws)
            return result

        with patch.object(
            aws.s3, "delete_objects", side_effect=user_creates_mid_delete
        ):
            resp = _call(api, "DELETE", "/dataset/confbench")

        assert resp["statusCode"] == 200
        assert _row(aws) == created["row"]
