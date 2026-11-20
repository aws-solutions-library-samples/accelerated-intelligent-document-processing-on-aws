# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The stack's test-set deployers never replace a test set they did not create.

Each deployer is a CloudFormation custom resource that writes one test set under a
fixed id (``docsplit``, ``fake-w2``, ...) on every create and update. A user can
create a set with that id from Test Studio, and an unconditional write would
replace that set's row, and the deployment would add the dataset's files to its
folder. The deployer's own row is told apart by its ``source``; it must still be
refreshed on an update.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.unit.notifications._cfn_yaml import REPO_ROOT, load_cfn_yaml

LAMBDA_ROOT = REPO_ROOT / "src" / "lambda"
TABLE_NAME = "tracking-for-deployers"
BUCKET_NAME = "test-set-bucket"

#: deployer directory -> (test set id, the `source` already on deployed stacks' rows,
#: extra positional args of create_testset_record). The source is a literal on
#: purpose: a deployer whose constant drifted from what its existing rows carry
#: would read its own row as someone else's set on every later update.
DEPLOYERS = {
    "docsplit_testset_deployer": (
        "docsplit",
        "huggingface:jordyvl/rvl_cdip_n_mp",
        ({"invoice": 1},),
    ),
    "fcc_dataset_deployer": (
        "realkie-fcc-verified",
        "huggingface:amazon-agi/RealKIE-FCC-Verified",
        (),
    ),
    "w2_dataset_deployer": (
        "fake-w2",
        "huggingface:singhsays/fake-w2-us-tax-form-dataset",
        ({"train": 1},),
    ),
    "ocr_benchmark_deployer": (
        "ocr-benchmark",
        "huggingface:getomni-ai/ocr-benchmark",
        ({"BANK_CHECK": 1},),
    ),
}

# Imported by a deployer but absent from the test environment; the code under
# test never reaches them.
_STUBBED_IMPORTS = ("huggingface_hub", "pypdf")


def _load(directory):
    """Import one deployer's index.py with its own sibling modules."""
    lambda_dir = LAMBDA_ROOT / directory
    stubs = {}
    for name in _STUBBED_IMPORTS:
        try:
            importlib.import_module(name)
        except ImportError:
            stubs[name] = MagicMock(name=name)
    # Every deployer ships its own cfnresponse and log_sanitizer; drop any copy a
    # previous load cached so each module binds to its own.
    with (
        patch.dict(sys.modules, stubs),
        patch.dict(os.environ),
        patch.object(sys, "path", [str(lambda_dir), *sys.path]),
    ):
        for name in ("cfnresponse", "log_sanitizer"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(
            f"deployer_{directory}", lambda_dir / "index.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _tracking_table_definition():
    """The parent template's TrackingTable keys and indexes, as create_table takes them."""
    props = load_cfn_yaml(REPO_ROOT / "template.yaml")["Resources"]["TrackingTable"][
        "Properties"
    ]
    definition = {
        key: props[key]
        for key in ("AttributeDefinitions", "KeySchema", "GlobalSecondaryIndexes")
    }
    assert {k["AttributeName"] for k in definition["KeySchema"]} == {"PK", "SK"}
    return definition


@pytest.fixture(params=sorted(DEPLOYERS))
def deployer(request):
    """(module, test set id, extra create args, table, s3, responses) per deployer."""
    env = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIA" + "IOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TRACKING_TABLE": TABLE_NAME,
        "TESTSET_BUCKET": BUCKET_NAME,
    }
    test_set_id, source, extra = DEPLOYERS[request.param]
    with mock_aws(), patch.dict(os.environ, env):
        module = _load(request.param)
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            **_tracking_table_definition(),
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET_NAME)
        responses = []

        def send(event, context, status, data):
            responses.append((status, data))

        with (
            patch.object(module, "dynamodb", boto3.resource("dynamodb")),
            patch.object(module, "s3_client", s3),
            patch.object(module, "TRACKING_TABLE", TABLE_NAME),
            patch.object(module, "TESTSET_BUCKET", BUCKET_NAME),
            patch.object(module.cfnresponse, "send", send),
        ):
            yield types.SimpleNamespace(
                module=module,
                test_set_id=test_set_id,
                source=source,
                extra=extra,
                table=table,
                s3=s3,
                responses=responses,
            )


def _event(request_type="Update", version="2.0"):
    return {
        "RequestType": request_type,
        "ResourceProperties": {
            "DatasetVersion": version,
            "DatasetDescription": "from the stack",
        },
    }


def _user_row(d):
    """A set a user created in Test Studio under the deployer's id."""
    return {
        "PK": f"testset#{d.test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": "2026-01-01T00:00:00Z",
        "id": d.test_set_id,
        "name": d.test_set_id,
        "description": "mine",
        "fileCount": 4,
        "source": "uploaded",
        "status": "COMPLETED",
        "createdAt": "2026-01-01T00:00:00Z",
    }


def _user_set(d):
    row = _user_row(d)
    d.table.put_item(Item=row)
    return row


def _own_set(d, version="1.0"):
    row = {
        "PK": f"testset#{d.test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": "2026-01-01T00:00:00Z",
        "id": d.test_set_id,
        "name": "old name",
        "fileCount": 3,
        "source": d.source,
        "status": "COMPLETED",
        "datasetVersion": version,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    d.table.put_item(Item=row)
    return row


def _row(d):
    key = {"PK": f"testset#{d.test_set_id}", "SK": "metadata"}
    return d.table.get_item(Key=key).get("Item")


def _create(d, version="2.0"):
    d.module.create_testset_record(version, "from the stack", 9, *d.extra)


def _deploy_writes_folder_and_row(d):
    """Stand-in for deploy_dataset: what the real one writes, minus the download."""

    def deploy(version, description):
        d.s3.put_object(
            Bucket=BUCKET_NAME, Key=f"{d.module.DATASET_PREFIX}input/a.pdf", Body=b"x"
        )
        _create(d, version)
        return {"DatasetVersion": version}

    return deploy


def _folder_keys(d):
    listed = d.s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=d.module.DATASET_PREFIX)
    return [o["Key"] for o in listed.get("Contents", [])]


@pytest.mark.unit
class TestDeployerLeavesAUserSetAlone:
    @pytest.mark.parametrize("request_type", ["Create", "Update"])
    def test_a_user_set_with_the_id_is_neither_replaced_nor_written_into(
        self, deployer, request_type
    ):
        d = deployer
        original = _user_set(d)

        deploy = MagicMock(side_effect=_deploy_writes_folder_and_row(d))
        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event(request_type), None)

        deploy.assert_not_called()

        assert _row(d) == original
        assert _folder_keys(d) == []
        # Reported, not failed: a name clash must not block a stack update.
        [(status, data)] = d.responses
        assert status == d.module.cfnresponse.SUCCESS
        assert data["Status"] == "ID_IN_USE"
        assert d.test_set_id in data["Message"]

    def test_a_user_set_created_during_the_deployment_survives(self, deployer):
        # The up-front check passed; the set appears before the row is written.
        d = deployer
        created = {}

        def deploy(version, description):
            created["row"] = _user_set(d)
            _create(d, version)

        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event(), None)

        assert _row(d) == created["row"]
        [(status, data)] = d.responses
        assert status == d.module.cfnresponse.SUCCESS
        assert data["Status"] == "DEPLOYMENT_FAILED"

    def test_a_failed_claim_deploys_nothing(self, deployer):
        # A claim failing for any reason other than the condition is not a clash;
        # it takes the non-blocking error path and writes no files.
        d = deployer
        deploy = MagicMock(side_effect=_deploy_writes_folder_and_row(d))
        failure = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
        )

        with (
            patch.object(d.module, "deploy_dataset", deploy),
            patch.object(d.module, "_put_own_row", side_effect=failure),
        ):
            d.module.handler(_event(), None)

        deploy.assert_not_called()
        assert _folder_keys(d) == []
        assert d.responses[0][1]["Status"] == "DEPLOYMENT_FAILED"

    def test_a_create_during_the_deployment_is_refused_by_the_claim(self, deployer):
        # Test Studio's create paths write with attribute_not_exists(PK); the claim
        # is what makes that refuse while the dataset's files are being written.
        d = deployer
        seen = {}

        def deploy(version, description):
            seen["claim"] = _row(d)
            try:
                d.table.put_item(
                    Item={**_user_row(d), "name": "a user's set"},
                    ConditionExpression="attribute_not_exists(PK)",
                )
            except ClientError as e:
                seen["refused"] = e.response["Error"]["Code"]
            return _deploy_writes_folder_and_row(d)(version, description)

        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event("Create"), None)

        assert seen["claim"]["status"] == "UPDATING"
        assert seen["claim"]["source"] == d.source
        assert seen["refused"] == "ConditionalCheckFailedException"
        assert _row(d)["status"] == "COMPLETED"

    def test_discovery_during_the_deployment_does_not_lock_it_out(self, deployer):
        # getTestSets registers a folder that has files and no row, as an upload.
        # Files land before the final record, so without the claim a poll in that
        # window would make the deployer's own set look foreign on every update.
        d = deployer

        def deploy_with_a_poll_mid_run(version, description):
            d.s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{d.module.DATASET_PREFIX}input/a.pdf",
                Body=b"x",
            )
            try:
                d.table.put_item(
                    Item={**_user_row(d), "name": d.test_set_id},
                    ConditionExpression="attribute_not_exists(PK)",
                )
            except ClientError:
                pass
            _create(d, version)
            return {"DatasetVersion": version}

        with patch.object(d.module, "deploy_dataset", deploy_with_a_poll_mid_run):
            d.module.handler(_event("Create", version="1.0"), None)
            d.module.handler(_event("Update", version="2.0"), None)

        row = _row(d)
        assert row["source"] == d.source
        assert row["datasetVersion"] == "2.0"
        assert [r[1].get("Status") for r in d.responses] == [None, None]

    def test_the_record_writers_refuse_a_user_set(self, deployer):
        d = deployer
        original = _user_set(d)

        for write in (
            lambda: _create(d),
            lambda: d.module.create_failed_testset_record("2.0", "boom"),
        ):
            with pytest.raises(ClientError) as raised:
                write()
            assert (
                raised.value.response["Error"]["Code"]
                == "ConditionalCheckFailedException"
            )
        assert _row(d) == original

    def test_a_description_refresh_refuses_a_user_set(self, deployer):
        d = deployer
        if not hasattr(d.module, "update_description_only"):
            pytest.skip("this deployer has no description-only refresh")
        original = _user_set(d)

        with pytest.raises(ClientError) as raised:
            d.module.update_description_only("from the stack")

        assert raised.value.response["Error"]["Code"] == (
            "ConditionalCheckFailedException"
        )

        assert _row(d) == original


@pytest.mark.unit
class TestDeployerStillRefreshesItsOwnSet:
    def test_a_new_stack_creates_the_set(self, deployer):
        d = deployer

        with patch.object(d.module, "deploy_dataset", _deploy_writes_folder_and_row(d)):
            d.module.handler(_event("Create"), None)

        row = _row(d)
        assert row["source"] == d.module.DATASET_SOURCE
        assert row["datasetVersion"] == "2.0"
        assert row["status"] == "COMPLETED"
        assert _folder_keys(d) == [f"{d.module.DATASET_PREFIX}input/a.pdf"]

    def test_an_update_to_a_new_version_replaces_its_own_row(self, deployer):
        d = deployer
        _own_set(d, version="1.0")

        with patch.object(d.module, "deploy_dataset", _deploy_writes_folder_and_row(d)):
            d.module.handler(_event(version="2.0"), None)

        row = _row(d)
        assert row["datasetVersion"] == "2.0"
        assert row["fileCount"] == 9
        assert d.responses[0][1] == {"DatasetVersion": "2.0"}

    def test_a_failure_marks_its_own_row_failed(self, deployer):
        d = deployer
        _own_set(d, version="1.0")

        def deploy(version, description):
            raise RuntimeError("download failed")

        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event(), None)

        row = _row(d)
        assert row["status"] == "FAILED"
        assert row["source"] == d.module.DATASET_SOURCE

    def test_a_run_that_died_mid_deployment_is_redeployed(self, deployer):
        # A Lambda timeout reaches neither the error path nor CloudFormation, so
        # the claim stays UPDATING with some files in place. The retry at the same
        # version must deploy again, not read that as a finished deployment.
        d = deployer

        def dies_after_one_file(version, description):
            d.s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{d.module.DATASET_PREFIX}input/a.pdf",
                Body=b"x",
            )
            raise SystemExit("timed out")

        with patch.object(d.module, "deploy_dataset", dies_after_one_file):
            with pytest.raises(SystemExit):
                d.module.handler(_event(version="2.0"), None)
        assert _row(d)["status"] == "UPDATING"

        deploy = MagicMock(side_effect=_deploy_writes_folder_and_row(d))
        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event(version="2.0"), None)

        deploy.assert_called_once()
        assert _row(d)["status"] == "COMPLETED"

    def test_a_finished_deployment_at_the_same_version_is_not_redone(self, deployer):
        # The other half: only COMPLETED is skipped, and it still is.
        d = deployer
        _own_set(d, version="2.0")
        d.s3.put_object(
            Bucket=BUCKET_NAME, Key=f"{d.module.DATASET_PREFIX}input/a.pdf", Body=b"x"
        )

        deploy = MagicMock()
        with patch.object(d.module, "deploy_dataset", deploy):
            d.module.handler(_event(version="2.0"), None)

        deploy.assert_not_called()
        assert _row(d)["status"] == "COMPLETED"

    def test_a_description_refresh_updates_its_own_row(self, deployer):
        d = deployer
        if not hasattr(d.module, "update_description_only"):
            pytest.skip("this deployer has no description-only refresh")
        _own_set(d)

        d.module.update_description_only("new words")

        assert _row(d)["description"] == "new words"

    def test_the_source_marker_is_what_the_record_writers_stamp(self, deployer):
        # The conditions compare `source`; a writer stamping a different value would make
        # the deployer treat its own row as a user's on the next update.
        d = deployer
        _create(d)
        created = _row(d)["source"]
        d.module.create_failed_testset_record("2.0", "boom")

        assert created == _row(d)["source"] == d.module.DATASET_SOURCE == d.source


def test_every_test_set_deployer_is_covered():
    # A new fixed-id deployer added without this coverage fails here.
    found = {
        path.parent.name
        for path in LAMBDA_ROOT.glob("*/index.py")
        if "TEST_SET_ID =" in path.read_text(encoding="utf-8")
    }
    assert found == set(DEPLOYERS)
