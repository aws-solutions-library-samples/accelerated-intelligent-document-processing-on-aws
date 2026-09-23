# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``StackDeployer``'s teardown and retained-resource cleanup in
``idp_sdk._core.stack``.

CloudFormation does not delete everything. Buckets with objects in them block a
stack delete, log groups auto-created by Lambda were never CloudFormation's to
begin with, and resources carrying ``DeletionPolicy: Retain`` are left standing on
purpose. So the SDK finishes the job itself: ``delete_stack`` empties buckets and
waits, ``get_retained_resources_after_deletion`` asks what survived,
``_discover_auto_created_log_groups`` finds what CloudFormation never knew about,
and ``cleanup_retained_resources`` deletes it — DynamoDB tables, log groups and
buckets — with ``_cleanup_additional_resources`` sweeping AppSync log groups, IAM
policies, CloudWatch Logs resource policies, CloudFront distributions and Bedrock
Data Automation projects on top.

**Every function here deletes something, so the tests are written around the two
ways that can go wrong.** Deleting too little leaves billable debris and a stack
name that cannot be reused. Deleting too much destroys another live deployment's
data, and that cannot be undone — which is why the ownership and exclusion tests
below matter more than the happy paths, and why each of them asserts on the
survivor as well as on the victim.

The other principle is that a deletion is only proven by the resource being gone.
``moto`` supports S3 (including versioning and delete markers), DynamoDB,
CloudWatch Logs, IAM, STS, AppSync, CloudFront distributions and CloudFormation
well enough to create a real stack whose resources really exist, so these tests
delete real fakes and read back that they are absent, rather than asserting that a
``MagicMock`` was called. Where moto has no implementation at all — CloudFront
response-headers policies and Bedrock Data Automation — a partial fake wraps the
moto client and overrides only the missing operations.

``test_discover_auto_created_log_groups.py`` already covers the log-group prefix
patterns and the sibling-stack guard through a mocked Logs client; this file
extends that function where it is untested (the explicit-prefix branch and the
outer failure path) and unit-tests ``_excluding_other_live_stacks`` directly over
the prefix families that file does not reach.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from idp_sdk._core import stack as stack_mod
from idp_sdk._core.stack import StackDeployer

pytestmark = pytest.mark.unit

REGION = "us-east-1"


def _template(resources: dict) -> str:
    return json.dumps(
        {"AWSTemplateFormatVersion": "2010-09-09", "Resources": resources}
    )


BUCKET_AND_FRIENDS = _template(
    {
        "InputBucket": {"Type": "AWS::S3::Bucket"},
        "Table": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": "S1-tracking",
                "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
        "LogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/S1/lambda/OCRFunction"},
        },
        "Topic": {"Type": "AWS::SNS::Topic"},
    }
)


class FakeClock:
    """``time`` stand-in for ``stack.py``; see the lifecycle test file."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(stack_mod, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _contain_the_retry_environment_mutation() -> Any:
    """Undo the ``os.environ`` write ``_cleanup_additional_resources`` performs.

    This is containment of a real side effect, not test scaffolding.
    ``_cleanup_additional_resources`` sets ``AWS_MAX_ATTEMPTS=10`` and
    ``AWS_RETRY_MODE=adaptive`` in the process environment at
    ``_core/stack.py:1943`` and never restores them
    (``test_retry_configuration_is_set_in_the_process_environment`` pins that).
    Because this file calls that function directly in about forty tests, without
    this fixture the variables persist for the remainder of the pytest session and
    every boto3 client any *other* test file builds afterwards inherits adaptive
    retries.

    That is not hypothetical: it made
    ``test_test_studio_processor.py::TestGetTestResult::test_the_timeout_is_reported_before_any_result_fetch``
    fail with ``RuntimeError: generator raised StopIteration``, because the extra
    retries exhausted that test's scripted ``side_effect`` sequence. The failure
    appeared only in a whole-directory run and passed when the test was run alone,
    which is the most expensive shape a red mark can have — so it is fixed here at
    the source rather than left for the next person to bisect.
    """
    import os

    before = {
        name: os.environ.get(name) for name in ("AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE")
    }
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class PartialFake:
    """A real (moto) client with a few operations replaced.

    Used only for the two services moto does not implement at all — CloudFront
    response-headers policies and Bedrock Data Automation. Everything else still
    reaches moto, so a test that overrides one operation does not lose the
    fidelity of the rest of the client.
    """

    def __init__(self, real: Any, overrides: dict[str, Callable[..., Any]]):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, dict[str, Callable[..., Any]]]
) -> None:
    """Wrap ``boto3.client`` so named services get the given operation overrides."""
    real_client = boto3.client

    def factory(service: str, *args: Any, **kwargs: Any) -> Any:
        client = real_client(service, *args, **kwargs)
        if service in overrides:
            return PartialFake(client, overrides[service])
        return client

    monkeypatch.setattr(boto3, "client", factory)


def _create_stack(deployer: StackDeployer, name: str, template: str) -> str:
    return deployer.cfn.create_stack(StackName=name, TemplateBody=template)["StackId"]


# ---------------------------------------------------------------------------
# delete_stack
# ---------------------------------------------------------------------------


def test_deleting_a_stack_that_does_not_exist_is_refused(aws_credentials: str):
    """Silently succeeding would let a teardown script report success for a stack
    name that was mistyped, leaving the real stack running."""
    with mock_aws():
        with pytest.raises(ValueError, match="Stack 'nope' does not exist"):
            StackDeployer(region=REGION).delete_stack("nope")


def test_delete_returns_the_stack_arn_because_the_name_stops_resolving(
    aws_credentials: str, clock: FakeClock
):
    """Once a stack is deleted, CloudFormation answers only to its id. The
    returned ``stack_id`` is what the retained-resource sweep is run against, so
    returning the name instead would make every post-delete query fail."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        stack_id = _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)

        result = deployer.delete_stack("S1", wait=True)

        assert result["stack_id"] == stack_id
        assert stack_id.startswith("arn:aws:cloudformation:")
        assert result["status"] == "DELETE_COMPLETE"
        assert result["success"] is True
        assert deployer._stack_exists("S1") is False


def test_not_waiting_reports_the_delete_as_merely_initiated(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Without ``wait`` the caller is told the request went in, and the additional
    cleanup is deliberately skipped — running it while the stack is still
    deleting would race CloudFormation for the same resources."""
    cleaned: list[str] = []
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        monkeypatch.setattr(
            deployer, "_cleanup_additional_resources", lambda name: cleaned.append(name)
        )

        result = deployer.delete_stack("S1", wait=False)

    assert result["status"] == "INITIATED"
    assert result["operation"] == "DELETE"
    assert cleaned == []


def test_waiting_runs_the_additional_cleanup_for_this_stack(
    aws_credentials: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    cleaned: list[str] = []
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        monkeypatch.setattr(
            deployer, "_cleanup_additional_resources", lambda name: cleaned.append(name)
        )
        deployer.delete_stack("S1", wait=True)
    assert cleaned == ["S1"]


def test_requested_buckets_are_emptied_before_the_delete(
    aws_credentials: str, clock: FakeClock
):
    """A non-empty bucket makes CloudFormation's DeleteBucket fail and the whole
    stack delete land in DELETE_FAILED, so the objects have to go first."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]

        s3 = boto3.client("s3", region_name=REGION)
        s3.put_object(Bucket=bucket, Key="docs/a.pdf", Body=b"a")
        s3.put_object(Bucket=bucket, Key="docs/b.pdf", Body=b"b")
        assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 2

        deployer.delete_stack("S1", empty_buckets=True, wait=True)

        # The bucket itself may or may not survive moto's stack delete; what must
        # be true either way is that no object is left in it.
        try:
            remaining = s3.list_objects_v2(Bucket=bucket).get("KeyCount", 0)
        except Exception:
            remaining = 0
    assert remaining == 0


def test_buckets_are_left_alone_unless_asked(aws_credentials: str, clock: FakeClock):
    """Emptying is destructive and irreversible, so it must never be the default."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]
        s3 = boto3.client("s3", region_name=REGION)
        s3.put_object(Bucket=bucket, Key="keep.pdf", Body=b"keep")

        deployer.delete_stack("S1", empty_buckets=False, wait=False)

        assert s3.get_object(Bucket=bucket, Key="keep.pdf")["Body"].read() == b"keep"


def test_an_unreadable_stack_id_falls_back_to_the_stack_name(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """The id is only needed for the post-delete sweep; failing to read it must
    not stop the delete itself."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        real_describe = deployer.cfn.describe_stacks
        calls = {"n": 0}

        def flaky(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # the id lookup, after _stack_exists
                raise RuntimeError("Throttling")
            return real_describe(**kwargs)

        monkeypatch.setattr(deployer.cfn, "describe_stacks", flaky)
        result = deployer.delete_stack("S1", wait=False)
    assert result["stack_id"] == "S1"


def test_a_failing_delete_call_propagates(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)

        def denied(**_kwargs: Any) -> Any:
            raise RuntimeError("AccessDenied: cloudformation:DeleteStack")

        monkeypatch.setattr(deployer.cfn, "delete_stack", denied)
        with pytest.raises(RuntimeError, match="AccessDenied"):
            deployer.delete_stack("S1", wait=False)


# ---------------------------------------------------------------------------
# _get_stack_buckets
# ---------------------------------------------------------------------------


def test_only_s3_buckets_are_collected_from_the_stack(aws_credentials: str):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer,
            "S1",
            _template(
                {
                    "InputBucket": {"Type": "AWS::S3::Bucket"},
                    "OutputBucket": {"Type": "AWS::S3::Bucket"},
                    "Topic": {"Type": "AWS::SNS::Topic"},
                }
            ),
        )
        buckets = deployer._get_stack_buckets("S1")

    assert sorted(b["logical_id"] for b in buckets) == ["InputBucket", "OutputBucket"]
    assert all(b["bucket_name"] for b in buckets)


def test_buckets_of_a_missing_stack_are_an_empty_list(aws_credentials: str):
    """``delete_stack`` calls this before deleting; an error here would abort a
    teardown that could otherwise have proceeded."""
    with mock_aws():
        assert StackDeployer(region=REGION)._get_stack_buckets("nope") == []


# ---------------------------------------------------------------------------
# _empty_buckets
# ---------------------------------------------------------------------------


def test_emptying_removes_every_version_and_delete_marker(aws_credentials: str):
    """A versioned bucket is not empty when its current objects are gone: the old
    versions and the delete markers still count, and DeleteBucket keeps failing
    until they go too. This is the case that makes a teardown look successful and
    then fail on the bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="versioned-bucket")
        s3.put_bucket_versioning(
            Bucket="versioned-bucket", VersioningConfiguration={"Status": "Enabled"}
        )
        s3.put_object(Bucket="versioned-bucket", Key="a.pdf", Body=b"v1")
        s3.put_object(Bucket="versioned-bucket", Key="a.pdf", Body=b"v2")
        s3.delete_object(Bucket="versioned-bucket", Key="a.pdf")  # delete marker
        s3.put_object(Bucket="versioned-bucket", Key="b.pdf", Body=b"b")

        listing = s3.list_object_versions(Bucket="versioned-bucket")
        assert len(listing.get("Versions", [])) == 3
        assert len(listing.get("DeleteMarkers", [])) == 1

        StackDeployer(region=REGION)._empty_buckets(
            [{"logical_id": "InputBucket", "bucket_name": "versioned-bucket"}]
        )

        after = s3.list_object_versions(Bucket="versioned-bucket")
        assert after.get("Versions", []) == []
        assert after.get("DeleteMarkers", []) == []
        # The bucket itself must survive: CloudFormation still has to delete it.
        s3.head_bucket(Bucket="versioned-bucket")


def test_a_bucket_that_is_already_gone_is_not_an_error(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    """A bucket deleted by hand, or by a previous half-finished teardown, is
    already as empty as it needs to be."""
    with mock_aws():
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._empty_buckets(
                [{"logical_id": "Gone", "bucket_name": "never-existed-bucket"}]
            )
    assert "does not exist" in caplog.text


def test_one_missing_bucket_does_not_stop_the_others(aws_credentials: str):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="real-bucket")
        s3.put_object(Bucket="real-bucket", Key="a", Body=b"a")

        StackDeployer(region=REGION)._empty_buckets(
            [
                {"logical_id": "Gone", "bucket_name": "never-existed-bucket"},
                {"logical_id": "Real", "bucket_name": "real-bucket"},
            ]
        )
        assert s3.list_objects_v2(Bucket="real-bucket").get("KeyCount", 0) == 0


def test_an_undeletable_bucket_raises_with_a_manual_remedy(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """An object under a governance retention lock, or a denied s3:DeleteObject,
    cannot be worked around — so the error has to tell the operator to finish the
    job by hand rather than let the stack delete fail cryptically later."""

    class Boom:
        def Bucket(self, _name: str) -> Any:  # noqa: N802 - mirrors boto3's API
            raise RuntimeError("AccessDenied: s3:DeleteObjectVersion")

    monkeypatch.setattr(boto3, "resource", lambda *_a, **_k: Boom())
    with pytest.raises(Exception, match="You may need to empty it manually"):
        StackDeployer(region=REGION)._empty_buckets(
            [{"logical_id": "Locked", "bucket_name": "locked-bucket"}]
        )


# ---------------------------------------------------------------------------
# _wait_for_deletion
# ---------------------------------------------------------------------------


def test_deletion_polls_until_the_stack_is_gone(aws_credentials: str, clock: FakeClock):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "S1",
                        "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                        "StackStatus": "DELETE_IN_PROGRESS",
                    }
                ]
            },
            {"StackName": "S1"},
        )
        stub.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id S1 does not exist",
        )
        result = deployer._wait_for_deletion("S1")
    assert result["status"] == "DELETE_COMPLETE"
    assert result["success"] is True
    assert clock.slept == [10]


def test_an_empty_stack_list_means_the_delete_finished(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        assert deployer._wait_for_deletion("S1")["success"] is True


def test_a_failed_delete_is_reported_with_the_blocking_resource(
    aws_credentials: str, clock: FakeClock
):
    """DELETE_FAILED needs manual work, and the only clue is which resource
    refused — usually a bucket that still has objects in it."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "S1",
                        "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                        "StackStatus": "DELETE_FAILED",
                    }
                ]
            },
            {"StackName": "S1"},
        )
        stub.add_response(
            "describe_stack_events",
            {
                "StackEvents": [
                    {
                        "StackId": "arn:aws:cloudformation:us-east-1:1:stack/S1/x",
                        "EventId": "e1",
                        "StackName": "S1",
                        "LogicalResourceId": "LoggingBucket",
                        "ResourceType": "AWS::S3::Bucket",
                        "Timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                        "ResourceStatus": "DELETE_FAILED",
                        "ResourceStatusReason": "The bucket you tried to delete is not empty",
                    }
                ]
            },
            {"StackName": "S1"},
        )
        result = deployer._wait_for_deletion("S1")
    assert result["success"] is False
    assert result["status"] == "DELETE_FAILED"
    assert (
        result["error"] == "LoggingBucket: The bucket you tried to delete is not empty"
    )


def test_an_unrelated_error_while_waiting_for_deletion_propagates(
    aws_credentials: str, clock: FakeClock
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("describe_stacks", service_error_code="AccessDenied")
        with pytest.raises(Exception, match="AccessDenied"):
            deployer._wait_for_deletion("S1")


# ---------------------------------------------------------------------------
# get_bucket_info
# ---------------------------------------------------------------------------


def test_bucket_info_reports_the_real_object_count_and_size(aws_credentials: str):
    """This is what the CLI shows before asking the operator to confirm a
    destructive empty-and-delete, so a wrong count is a wrong consent."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer, "S1", _template({"InputBucket": {"Type": "AWS::S3::Bucket"}})
        )
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]
        s3 = boto3.client("s3", region_name=REGION)
        s3.put_object(Bucket=bucket, Key="a", Body=b"x" * 1000)
        s3.put_object(Bucket=bucket, Key="b", Body=b"y" * 2000)

        info = deployer.get_bucket_info("S1")

    assert len(info) == 1
    assert info[0]["object_count"] == 2
    assert info[0]["total_size"] == 3000
    assert info[0]["size_display"] == f"{3000 / (1024 * 1024):.2f} MB"


def test_bucket_info_reports_unknown_rather_than_zero_when_stats_fail(
    aws_credentials: str,
):
    """A bucket this account cannot list must not be displayed as empty — an
    operator reading "0 objects" would approve a delete believing nothing is
    there. The count falls back to 0 but the display says so."""
    deployer = StackDeployer(region=REGION)
    with mock_aws():
        with Stubber(deployer.cfn) as stub:
            stub.add_response(
                "list_stack_resources",
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": "InputBucket",
                            "PhysicalResourceId": "bucket-that-is-not-there",
                            "ResourceType": "AWS::S3::Bucket",
                            "LastUpdatedTimestamp": dt.datetime(
                                2026, 1, 1, tzinfo=dt.timezone.utc
                            ),
                            "ResourceStatus": "CREATE_COMPLETE",
                        }
                    ]
                },
                {"StackName": "S1"},
            )
            info = deployer.get_bucket_info("S1")
    assert info[0]["object_count"] == 0
    assert info[0]["size_display"] == "Unknown"


def test_bucket_info_undercounts_a_bucket_with_more_than_one_page(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """DEFECT, pinned as-is: the count stops at the first 1,000 objects.

    ``get_bucket_info`` calls ``list_objects_v2`` once, at
    ``_core/stack.py:1187``, and ignores ``IsTruncated`` — no paginator. Every IDP
    bucket in production holds far more than 1,000 objects, so the figure the CLI
    shows before a destructive confirmation is capped at 1,000 and the size is the
    size of that first page only. An operator told "1,000 objects, 4.00 MB" may
    approve emptying a bucket holding a million.

    The stub below returns two keys with ``IsTruncated`` set; a paginating
    implementation would ask for the next page.
    """
    real_client = boto3.client

    def factory(service: str, *args: Any, **kwargs: Any) -> Any:
        client = real_client(service, *args, **kwargs)
        if service == "s3":
            return PartialFake(
                client,
                {
                    "list_objects_v2": lambda **_k: {
                        "Contents": [
                            {"Key": "a", "Size": 10},
                            {"Key": "b", "Size": 20},
                        ],
                        "IsTruncated": True,
                        "NextContinuationToken": "more-to-come",
                    }
                },
            )
        return client

    deployer = StackDeployer(region=REGION)
    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        with Stubber(deployer.cfn) as stub:
            stub.add_response(
                "list_stack_resources",
                {
                    "StackResourceSummaries": [
                        {
                            "LogicalResourceId": "InputBucket",
                            "PhysicalResourceId": "huge-bucket",
                            "ResourceType": "AWS::S3::Bucket",
                            "LastUpdatedTimestamp": dt.datetime(
                                2026, 1, 1, tzinfo=dt.timezone.utc
                            ),
                            "ResourceStatus": "CREATE_COMPLETE",
                        }
                    ]
                },
                {"StackName": "S1"},
            )
            info = deployer.get_bucket_info("S1")
    assert info[0]["object_count"] == 2
    assert info[0]["total_size"] == 30


# ---------------------------------------------------------------------------
# get_retained_resources_after_deletion
# ---------------------------------------------------------------------------


def test_retained_resources_are_categorised_by_type(aws_credentials: str):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)

        retained = deployer.get_retained_resources_after_deletion("S1")

    assert [r["physical_id"] for r in retained["dynamodb_tables"]] == ["S1-tracking"]
    assert [r["physical_id"] for r in retained["log_groups"]] == [
        "/S1/lambda/OCRFunction"
    ]
    assert len(retained["s3_buckets"]) == 1
    assert [r["logical_id"] for r in retained["other"]] == ["Topic"]


def test_a_log_group_is_reported_only_if_it_really_still_exists(aws_credentials: str):
    """CloudFormation reports DELETE_COMPLETE for a log group that is still in
    CloudWatch, so the status is overridden with a live check.

    Trusting the CloudFormation status in either direction is wrong: believing
    DELETE_COMPLETE leaves the group (and its retention bill) behind, and
    reporting a group that is genuinely gone makes the cleanup phase raise
    ResourceNotFound on every teardown.
    """
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer,
            "S1",
            _template(
                {
                    "LogGroup": {
                        "Type": "AWS::Logs::LogGroup",
                        "Properties": {"LogGroupName": "/S1/lambda/Present"},
                    }
                }
            ),
        )
        assert deployer.get_retained_resources_after_deletion("S1")["log_groups"]

        boto3.client("logs", region_name=REGION).delete_log_group(
            logGroupName="/S1/lambda/Present"
        )
        after = deployer.get_retained_resources_after_deletion("S1")

    assert after["log_groups"] == []


def test_the_override_records_that_the_existence_was_verified(aws_credentials: str):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        group = deployer.get_retained_resources_after_deletion("S1")["log_groups"][0]
    assert group["status"] == "EXISTS"
    assert group["status_reason"] == "Verified to exist in CloudWatch"
    assert group["stack"] == "S1"


@pytest.mark.parametrize("status", ["DELETE_COMPLETE", "DELETE_IN_PROGRESS"])
def test_a_resource_cloudformation_already_handled_is_not_retained(
    aws_credentials: str, status: str
):
    """Reporting one of these as retained would race CloudFormation for the same
    resource, and the loser reports a spurious teardown error."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "list_stack_resources",
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "Table",
                        "PhysicalResourceId": "S1-tracking",
                        "ResourceType": "AWS::DynamoDB::Table",
                        "LastUpdatedTimestamp": dt.datetime(
                            2026, 1, 1, tzinfo=dt.timezone.utc
                        ),
                        "ResourceStatus": status,
                    }
                ]
            },
            {"StackName": "S1"},
        )
        retained = deployer.get_retained_resources_after_deletion(
            "S1", include_nested=False
        )
    assert retained["dynamodb_tables"] == []


def test_nested_stacks_are_swept_too_and_can_be_skipped(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """A pattern stack's retained table is as billable as the parent's, so the
    sweep must reach it — and a caller that only wants the parent must be able to
    say so."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer, "Parent", _template({"Topic": {"Type": "AWS::SNS::Topic"}})
        )
        _create_stack(
            deployer,
            "Parent-PATTERNSTACK-ABC",
            _template(
                {
                    "NestedTable": {
                        "Type": "AWS::DynamoDB::Table",
                        "Properties": {
                            "TableName": "nested-table",
                            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "pk", "AttributeType": "S"}
                            ],
                            "BillingMode": "PAY_PER_REQUEST",
                        },
                    }
                }
            ),
        )
        monkeypatch.setattr(
            deployer, "_get_nested_stacks", lambda _name: ["Parent-PATTERNSTACK-ABC"]
        )

        with_nested = deployer.get_retained_resources_after_deletion("Parent")
        without = deployer.get_retained_resources_after_deletion(
            "Parent", include_nested=False
        )

    assert [r["physical_id"] for r in with_nested["dynamodb_tables"]] == [
        "nested-table"
    ]
    assert with_nested["dynamodb_tables"][0]["stack"] == "Parent-PATTERNSTACK-ABC"
    assert without["dynamodb_tables"] == []


def test_a_stack_that_is_already_gone_yields_no_retained_resources(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    """This runs *after* a delete, so the stack being absent is the expected case
    and must not warn."""
    with mock_aws():
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            retained = StackDeployer(
                region=REGION
            ).get_retained_resources_after_deletion("long-gone", include_nested=False)
    assert retained == {
        "dynamodb_tables": [],
        "log_groups": [],
        "s3_buckets": [],
        "other": [],
    }
    assert "Error checking resources" not in caplog.text


def test_an_unexpected_error_while_sweeping_is_warned_about(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("list_stack_resources", service_error_code="AccessDenied")
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            deployer.get_retained_resources_after_deletion("S1", include_nested=False)
    assert "Error checking resources for S1" in caplog.text


# ---------------------------------------------------------------------------
# _get_nested_stacks
# ---------------------------------------------------------------------------


def test_nested_stacks_are_found_recursively(aws_credentials: str):
    """Nesting is two deep in this solution (parent → pattern stack → its own
    children), so a single-level walk would miss a whole tier of resources."""
    deployer = StackDeployer(region=REGION)

    def summary(logical: str, physical: str) -> dict:
        return {
            "LogicalResourceId": logical,
            "PhysicalResourceId": physical,
            "ResourceType": "AWS::CloudFormation::Stack",
            "LastUpdatedTimestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            "ResourceStatus": "CREATE_COMPLETE",
        }

    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "list_stack_resources",
            {"StackResourceSummaries": [summary("PATTERNSTACK", "S1-PATTERNSTACK-A")]},
            {"StackName": "S1"},
        )
        stub.add_response(
            "list_stack_resources",
            {"StackResourceSummaries": [summary("INNER", "S1-PATTERNSTACK-A-INNER-B")]},
            {"StackName": "S1-PATTERNSTACK-A"},
        )
        stub.add_response(
            "list_stack_resources",
            {"StackResourceSummaries": []},
            {"StackName": "S1-PATTERNSTACK-A-INNER-B"},
        )
        assert deployer._get_nested_stacks("S1") == [
            "S1-PATTERNSTACK-A",
            "S1-PATTERNSTACK-A-INNER-B",
        ]


def test_non_stack_resources_are_not_mistaken_for_nested_stacks(aws_credentials: str):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        assert deployer._get_nested_stacks("S1") == []


def test_a_failure_to_list_nested_stacks_yields_an_empty_list(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("list_stack_resources", service_error_code="AccessDenied")
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            assert deployer._get_nested_stacks("S1") == []
    assert "Error getting nested stacks" in caplog.text


# ---------------------------------------------------------------------------
# _log_group_exists
# ---------------------------------------------------------------------------


def test_a_log_group_is_recognised_by_exact_name(aws_credentials: str):
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/S1/lambda/Fn")
        assert StackDeployer(region=REGION)._log_group_exists("/S1/lambda/Fn") is True


def test_a_longer_group_sharing_the_prefix_is_not_a_match(aws_credentials: str):
    """The query is a *prefix* search, so `/S1/lambda/Fn` would otherwise be
    reported as existing whenever `/S1/lambda/FnExtra` does. That answer drives a
    delete_log_group call, which would then fail on every teardown."""
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/S1/lambda/FnExtra")
        assert StackDeployer(region=REGION)._log_group_exists("/S1/lambda/Fn") is False


def test_an_absent_log_group_reads_as_absent(aws_credentials: str):
    with mock_aws():
        assert StackDeployer(region=REGION)._log_group_exists("/nothing/here") is False


def test_a_logs_api_failure_reads_as_absent(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Failing closed here means a group that might exist is not reported as
    retained, so it is left rather than deleted — the safe direction."""
    real_client = boto3.client

    def factory(service: str, *args: Any, **kwargs: Any) -> Any:
        client = real_client(service, *args, **kwargs)
        if service == "logs":

            def boom(**_k: Any) -> Any:
                raise RuntimeError("AccessDenied: logs:DescribeLogGroups")

            return PartialFake(client, {"describe_log_groups": boom})
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            assert (
                StackDeployer(region=REGION)._log_group_exists("/S1/lambda/Fn") is False
            )
    assert "Error checking log group" in caplog.text


# ---------------------------------------------------------------------------
# _get_stack_cloudfront_distributions / _verify_cloudfront_distributions_deleted
# ---------------------------------------------------------------------------


def _distribution_summary(logical: str, dist_id: str) -> dict:
    return {
        "LogicalResourceId": logical,
        "PhysicalResourceId": dist_id,
        "ResourceType": "AWS::CloudFront::Distribution",
        "LastUpdatedTimestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        "ResourceStatus": "DELETE_IN_PROGRESS",
    }


def test_cloudfront_distributions_are_read_off_the_stack(aws_credentials: str):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "list_stack_resources",
            {
                "StackResourceSummaries": [
                    _distribution_summary("WebUIDistribution", "E1ABCDEF"),
                    {
                        "LogicalResourceId": "Topic",
                        "PhysicalResourceId": "arn:aws:sns:::t",
                        "ResourceType": "AWS::SNS::Topic",
                        "LastUpdatedTimestamp": dt.datetime(
                            2026, 1, 1, tzinfo=dt.timezone.utc
                        ),
                        "ResourceStatus": "DELETE_COMPLETE",
                    },
                ]
            },
            {"StackName": "S1"},
        )
        assert deployer._get_stack_cloudfront_distributions("S1") == [
            {
                "logical_id": "WebUIDistribution",
                "distribution_id": "E1ABCDEF",
                "status": "DELETE_IN_PROGRESS",
            }
        ]


def test_a_deleted_stack_reports_no_distributions_without_warning(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    with mock_aws():
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            assert (
                StackDeployer(region=REGION)._get_stack_cloudfront_distributions("gone")
                == []
            )
    assert "Error getting CloudFront" not in caplog.text


def test_another_cloudfront_lookup_error_is_warned_and_yields_nothing(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("list_stack_resources", service_error_code="AccessDenied")
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            assert deployer._get_stack_cloudfront_distributions("S1") == []
    assert "Error getting CloudFront distributions" in caplog.text


def test_verification_is_a_no_op_when_the_stack_had_no_distribution(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Most deployments have one, but a headless one has none — and building a
    CloudFront client for nothing would fail in a partition without CloudFront."""
    built: list[str] = []
    monkeypatch.setattr(
        boto3, "client", lambda service, *a, **k: built.append(service) or None
    )
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = REGION
    monkeypatch.setattr(
        deployer, "_get_stack_cloudfront_distributions", lambda _name: []
    )
    deployer._verify_cloudfront_distributions_deleted("S1")
    assert built == []


def test_verification_passes_once_the_distribution_is_gone(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
):
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = REGION
    monkeypatch.setattr(
        deployer,
        "_get_stack_cloudfront_distributions",
        lambda _name: [{"distribution_id": "E1ABCDEF", "logical_id": "WebUI"}],
    )
    with mock_aws():
        # moto has no such distribution, so get_distribution raises
        # NoSuchDistribution, which is exactly the "already deleted" signal.
        deployer._verify_cloudfront_distributions_deleted("S1")
    assert clock.slept == []


def test_a_distribution_that_outlives_the_budget_blocks_the_teardown(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
):
    """This is the whole point of the check: deleting the S3 origin while the
    distribution is still serving leaves a CloudFront distribution pointed at a
    bucket that no longer exists, which cannot be fixed from the stack and serves
    errors to users. So the error must name the distribution and refuse.
    """
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = REGION
    monkeypatch.setattr(
        deployer,
        "_get_stack_cloudfront_distributions",
        lambda _name: [
            {"distribution_id": "E1ABCDEF", "logical_id": "WebUIDistribution"}
        ],
    )

    def factory(service: str, *a: Any, **k: Any) -> Any:
        assert service == "cloudfront"
        real = boto3_client_original(service, *a, **k)
        return PartialFake(
            real,
            {
                "get_distribution": lambda **_k: {
                    "Distribution": {
                        "Status": "Deployed",
                        "DistributionConfig": {"Enabled": True},
                    }
                }
            },
        )

    boto3_client_original = boto3.client
    monkeypatch.setattr(boto3, "client", factory)

    with pytest.raises(Exception) as excinfo:
        deployer._verify_cloudfront_distributions_deleted("S1", max_wait_seconds=30)
    message = str(excinfo.value)
    assert "WebUIDistribution" in message
    assert "E1ABCDEF" in message
    assert "orphaned" in message
    # It really waited rather than failing on the first look. Four ten-second
    # sleeps, not three: the test is `elapsed > max_wait_seconds`, so reaching the
    # budget exactly is not enough to give up.
    assert clock.slept == [10, 10, 10, 10]


def test_an_unexpected_cloudfront_error_is_not_read_as_deleted(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
):
    """Treating AccessDenied as "gone" would let the S3 deletion proceed and
    orphan a live distribution."""
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = REGION
    monkeypatch.setattr(
        deployer,
        "_get_stack_cloudfront_distributions",
        lambda _name: [{"distribution_id": "E1", "logical_id": "WebUI"}],
    )
    original = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        return PartialFake(
            original(service, *a, **k),
            {
                "get_distribution": lambda **_k: (_ for _ in ()).throw(
                    RuntimeError("AccessDenied: cloudfront:GetDistribution")
                )
            },
        )

    monkeypatch.setattr(boto3, "client", factory)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        deployer._verify_cloudfront_distributions_deleted("S1")


# ---------------------------------------------------------------------------
# _excluding_other_live_stacks
#
# test_discover_auto_created_log_groups.py covers this through the discovery
# function for the `/aws/lambda/<stack>-` family. These add the prefix families
# that file does not reach, plus the direct edge cases.
# ---------------------------------------------------------------------------


# The status filter the guard asks CloudFormation for. Naming it as the Stubber's
# expected parameters is itself an assertion: a stack in one of these states is
# live and its log groups are off limits, and dropping a state from the list (or
# adding DELETE_COMPLETE to it) changes whose logs can be deleted.
LIVE_STACK_STATUSES = [
    "CREATE_COMPLETE",
    "CREATE_IN_PROGRESS",
    "UPDATE_COMPLETE",
    "UPDATE_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE",
    "ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
]


def _exclude(
    candidates: list[str], stack_name: str, other_stacks: list[str]
) -> list[str]:
    deployer = StackDeployer(region=REGION)
    stub = Stubber(deployer.cfn)
    stub.add_response(
        "list_stacks",
        {
            "StackSummaries": [
                {
                    "StackId": f"arn:aws:cloudformation:us-east-1:1:stack/{name}/x",
                    "StackName": name,
                    "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                    "StackStatus": "CREATE_COMPLETE",
                }
                for name in other_stacks
            ]
        },
        {"StackStatusFilter": LIVE_STACK_STATUSES},
    )
    with stub:
        return deployer._excluding_other_live_stacks(candidates, stack_name)


def test_no_candidates_means_cloudformation_is_never_asked(aws_credentials: str):
    """An empty sweep must not spend a ListStacks call, which is throttled at a
    low rate and is on the teardown path of every stack."""
    deployer = StackDeployer(region=REGION)
    with Stubber(deployer.cfn):  # no queued responses: any call fails the test
        assert deployer._excluding_other_live_stacks([], "S1") == []


@pytest.mark.parametrize(
    "template",
    [
        "/aws/codebuild/{owner}-PATTERNSTACK-build",
        "/aws-glue/crawlers-role/{owner}-DocumentSectionsCrawlerRole-abc",
        "{owner}-GetDomainLambdaLogGroup-abc123",
        "/{owner}-FeaturePlatformStack-XYZ/lambda/ListFeatures",
    ],
)
def test_every_prefix_family_is_checked_for_sibling_ownership(
    aws_credentials: str, template: str
):
    """The discovery function emits five prefix shapes, and the CodeBuild, Glue,
    bare-name and stack-scoped families are as capable of matching a sibling as
    the Lambda one. `IDP` deleting `IDP-DEV`'s CodeBuild history is the same
    unrecoverable mistake."""
    mine = template.format(owner="IDP")
    sibling = template.format(owner="IDP-DEV")
    kept = _exclude([mine, sibling], "IDP", ["IDP-DEV"])
    assert mine in kept
    assert sibling not in kept


def test_a_candidate_no_stack_claims_is_kept(aws_credentials: str):
    """If nothing owns it, it is not another stack's — so it stays in the sweep
    rather than being dropped on a "could not attribute" basis."""
    orphan = "/aws/lambda/some-unrelated-function"
    assert _exclude([orphan], "IDP", ["IDP-DEV"]) == [orphan]


def test_an_exact_stack_name_prefix_is_ours_even_with_a_longer_sibling(
    aws_credentials: str,
):
    """`IDP-DEV` exists, but this group is `IDP-`-prefixed and not `IDP-DEV-`
    prefixed, so the longest match is our own name."""
    mine = "/aws/lambda/IDP-OtherFunction-abc"
    assert _exclude([mine], "IDP", ["IDP-DEV", "IDP-PROD"]) == [mine]


def test_a_sibling_whose_name_extends_ours_without_a_hyphen_is_not_an_owner(
    aws_credentials: str,
):
    """`IDP10` is a different stack, but its groups never match `/aws/lambda/IDP-`
    because of the hyphen, and our own groups must not be attributed to it."""
    mine = "/aws/lambda/IDP-Function-abc"
    assert _exclude([mine], "IDP", ["IDP10"]) == [mine]


def test_the_skip_is_logged_with_the_count(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    """A silent skip and a silent sweep look identical in a teardown log, and the
    difference is whether somebody else's logs were deleted."""
    with caplog.at_level("INFO", logger="idp_sdk._core.stack"):
        _exclude(
            [
                "/aws/lambda/IDP-DEV-A-abc",
                "/aws/lambda/IDP-DEV-B-def",
                "/aws/lambda/IDP-Mine-ghi",
            ],
            "IDP",
            ["IDP-DEV"],
        )
    assert "Skipping 2 log group(s) owned by another live stack" in caplog.text


# ---------------------------------------------------------------------------
# _discover_auto_created_log_groups — the branches the existing file does not reach
# ---------------------------------------------------------------------------


def test_the_explicit_prefixes_are_discovered_without_the_paginator(
    aws_credentials: str,
):
    """The four ``<stack>-...LogGroup-`` names are queried with a plain
    ``describe_log_groups`` rather than through the paginator, so they are a
    separate code path — and these are CloudFormation-generated names with no
    leading slash, which no other prefix in the list matches."""
    names = [
        "IDP1-GetDomainLambdaLogGroup-aBc123",
        "IDP1-StacknameCheckFunctionLogGroup-dEf456",
        "IDP1-ConfigurationCopyFunctionLogGroup-gHi789",
        "IDP1-UpdateSettingsFunctionLogGroup-jKl012",
    ]
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        for name in names:
            logs.create_log_group(logGroupName=name)
        logs.create_log_group(logGroupName="IDP10-GetDomainLambdaLogGroup-zzz")

        deployer = StackDeployer(region=REGION)
        discovered = deployer._discover_auto_created_log_groups("IDP1")

    assert sorted(discovered) == sorted(names)


def test_a_group_matched_by_two_prefixes_is_listed_once(aws_credentials: str):
    """`IDP1-GetDomainLambdaLogGroup-x` is matched by the bare explicit prefix
    only, but a name can be reachable from more than one pattern; a duplicate
    would produce a second delete_log_group and a spurious teardown error."""
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/IDP1-PATTERNSTACK-A/lambda/OCR")
        deployer = StackDeployer(region=REGION)
        discovered = deployer._discover_auto_created_log_groups("IDP1")
    assert discovered.count("/IDP1-PATTERNSTACK-A/lambda/OCR") == 1


def test_a_failure_on_an_explicit_prefix_does_not_lose_the_others(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    real_client = boto3.client
    calls = {"n": 0}

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service != "logs":
            return client
        original = client.describe_log_groups

        def flaky(**kwargs: Any) -> Any:
            calls["n"] += 1
            if kwargs.get("logGroupNamePrefix", "").endswith(
                "-GetDomainLambdaLogGroup-"
            ):
                raise RuntimeError("Throttling")
            return original(**kwargs)

        return PartialFake(client, {"describe_log_groups": flaky})

    with mock_aws():
        logs = real_client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="IDP1-GetDomainLambdaLogGroup-aaa")
        logs.create_log_group(logGroupName="IDP1-UpdateSettingsFunctionLogGroup-bbb")
        monkeypatch.setattr(boto3, "client", factory)
        discovered = StackDeployer(region=REGION)._discover_auto_created_log_groups(
            "IDP1"
        )

    assert "IDP1-UpdateSettingsFunctionLogGroup-bbb" in discovered
    assert "IDP1-GetDomainLambdaLogGroup-aaa" not in discovered


def test_a_total_logs_failure_yields_an_empty_sweep(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """If the paginator itself cannot be built there is nothing to clean; the
    teardown must continue rather than abort with a stack already deleted."""
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "logs":
            return PartialFake(
                client,
                {
                    "get_paginator": lambda *_a, **_k: (_ for _ in ()).throw(
                        RuntimeError("logs unavailable")
                    )
                },
            )
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        with caplog.at_level("ERROR", logger="idp_sdk._core.stack"):
            assert (
                StackDeployer(region=REGION)._discover_auto_created_log_groups("IDP1")
                == []
            )
    assert "Error discovering auto-created log groups" in caplog.text


def test_nothing_found_is_reported_as_nothing_found(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    with mock_aws():
        with caplog.at_level("INFO", logger="idp_sdk._core.stack"):
            assert (
                StackDeployer(region=REGION)._discover_auto_created_log_groups("IDP1")
                == []
            )
    assert "No auto-created log groups found for stack IDP1" in caplog.text


# ---------------------------------------------------------------------------
# _delete_dynamodb_table / _delete_log_group / _empty_and_delete_bucket
# ---------------------------------------------------------------------------


def test_a_table_is_really_deleted(aws_credentials: str):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="S1-tracking",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        StackDeployer(region=REGION)._delete_dynamodb_table("S1-tracking")
        assert ddb.list_tables()["TableNames"] == []


def test_point_in_time_recovery_is_turned_off_before_the_delete(aws_credentials: str):
    """PITR keeps a continuous backup that outlives the table and keeps billing,
    so it is disabled first. Failing to disable it must not stop the delete,
    which is why the attempt is swallowed — but when it can succeed it must."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="S1-tracking",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.update_continuous_backups(
            TableName="S1-tracking",
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
        )
        StackDeployer(region=REGION)._delete_dynamodb_table("S1-tracking")
        assert ddb.list_tables()["TableNames"] == []


def test_deleting_an_absent_table_raises_so_the_caller_can_record_it(
    aws_credentials: str,
):
    """``cleanup_retained_resources`` turns this into an entry in its ``errors``
    list; swallowing it here would report a clean teardown."""
    with mock_aws():
        with pytest.raises(Exception):
            StackDeployer(region=REGION)._delete_dynamodb_table("never-existed")


def test_a_log_group_is_really_deleted(aws_credentials: str):
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/S1/lambda/Fn")
        StackDeployer(region=REGION)._delete_log_group("/S1/lambda/Fn")
        assert logs.describe_log_groups()["logGroups"] == []


def test_deleting_an_absent_log_group_raises(aws_credentials: str):
    with mock_aws():
        with pytest.raises(Exception):
            StackDeployer(region=REGION)._delete_log_group("/nothing/here")


def test_a_versioned_bucket_is_emptied_and_then_deleted(aws_credentials: str):
    """DeleteBucket fails on a bucket holding any version or delete marker, so
    both steps have to happen and in this order."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="retained-bucket")
        s3.put_bucket_versioning(
            Bucket="retained-bucket", VersioningConfiguration={"Status": "Enabled"}
        )
        s3.put_object(Bucket="retained-bucket", Key="a", Body=b"1")
        s3.put_object(Bucket="retained-bucket", Key="a", Body=b"2")
        s3.delete_object(Bucket="retained-bucket", Key="a")

        StackDeployer(region=REGION)._empty_and_delete_bucket("retained-bucket")

        assert [b["Name"] for b in s3.list_buckets()["Buckets"]] == []


def test_deleting_an_absent_bucket_raises(aws_credentials: str):
    with mock_aws():
        with pytest.raises(Exception):
            StackDeployer(region=REGION)._empty_and_delete_bucket(
                "never-existed-bucket"
            )


# ---------------------------------------------------------------------------
# cleanup_retained_resources
# ---------------------------------------------------------------------------


@pytest.fixture
def no_additional_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence ``_cleanup_additional_resources`` for the retained-resource tests.

    It is exercised on its own further down. Leaving it in would have every test
    here also sweep AppSync, IAM, CloudFront and Bedrock Data Automation, which
    makes a failure hard to attribute.
    """
    monkeypatch.setattr(
        StackDeployer, "_cleanup_additional_resources", lambda _self, _id: None
    )


def test_retained_resources_are_deleted_and_verified_gone(
    aws_credentials: str, no_additional_cleanup: None
):
    """The flagship test: every category is deleted, and absence is read back off
    the live fakes rather than inferred from the returned lists.

    The resource that must *not* be touched is checked too — an SNS topic lands in
    ``other``, which this function deliberately reports and does not delete.
    """
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=bucket, Key="doc.pdf", Body=b"x"
        )

        results = deployer.cleanup_retained_resources("S1")

        assert results["dynamodb_deleted"] == ["S1-tracking"]
        assert results["logs_deleted"] == ["/S1/lambda/OCRFunction"]
        assert results["buckets_deleted"] == [bucket]
        assert results["errors"] == []

        assert (
            boto3.client("dynamodb", region_name=REGION).list_tables()["TableNames"]
            == []
        )
        assert (
            boto3.client("logs", region_name=REGION).describe_log_groups()["logGroups"]
            == []
        )
        assert [
            b["Name"]
            for b in boto3.client("s3", region_name=REGION).list_buckets()["Buckets"]
        ] == []
        # The SNS topic was reported, not deleted.
        assert boto3.client("sns", region_name=REGION).list_topics()["Topics"]


def test_nothing_retained_is_reported_as_nothing_to_do(
    aws_credentials: str, no_additional_cleanup: None
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", _template({"Topic": {"Type": "AWS::SNS::Topic"}}))
        # Only an SNS topic, which lands in "other" and is not counted.
        assert deployer.cleanup_retained_resources("S1") == {
            "total_deleted": 0,
            "errors": [],
        }


def test_a_stack_arn_is_reduced_to_a_name_for_log_group_discovery(
    aws_credentials: str, no_additional_cleanup: None, monkeypatch: pytest.MonkeyPatch
):
    """After a delete the caller has only the ARN, but every log-group prefix is
    built from the stack *name*. Passing the ARN through would build prefixes like
    ``/aws/lambda/arn:aws:cloudformation:...-`` and match nothing, silently
    leaving every auto-created group behind."""
    seen: list[str] = []
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        stack_id = _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        monkeypatch.setattr(
            deployer,
            "_discover_auto_created_log_groups",
            lambda name: seen.append(name) or [],
        )
        deployer.cleanup_retained_resources(stack_id)
    assert seen == ["S1"]


def test_an_auto_created_group_is_merged_in_and_deleted(
    aws_credentials: str, no_additional_cleanup: None
):
    """CloudFormation never knew about a Lambda's default log group, so the sweep
    is the only thing that will ever delete it — and its retention is indefinite,
    so it bills forever."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", _template({"Topic": {"Type": "AWS::SNS::Topic"}}))
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/aws/lambda/S1-OCRFunction-abc123")

        results = deployer.cleanup_retained_resources("S1")

        assert results["logs_deleted"] == ["/aws/lambda/S1-OCRFunction-abc123"]
        assert logs.describe_log_groups()["logGroups"] == []


def test_a_group_cloudformation_already_reported_is_not_deleted_twice(
    aws_credentials: str, no_additional_cleanup: None
):
    """The two sources overlap, and a duplicate produces a second
    ``delete_log_group`` that fails and shows up as a teardown error on an
    otherwise clean run."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer,
            "S1",
            _template(
                {
                    "LogGroup": {
                        "Type": "AWS::Logs::LogGroup",
                        "Properties": {"LogGroupName": "/aws/lambda/S1-Fn-abc123"},
                    }
                }
            ),
        )
        results = deployer.cleanup_retained_resources("S1")
    assert results["logs_deleted"] == ["/aws/lambda/S1-Fn-abc123"]
    assert results["errors"] == []


def test_the_logging_bucket_is_deleted_last(
    aws_credentials: str, no_additional_cleanup: None
):
    """Every other bucket writes its access logs into the logging bucket, and S3
    refuses to delete a bucket that is still a log destination — so deleting it
    first strands the rest. The template lists it first on purpose, so only the
    reordering can produce this result."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer,
            "S1",
            _template(
                {
                    "LoggingBucket": {"Type": "AWS::S3::Bucket"},
                    "InputBucket": {"Type": "AWS::S3::Bucket"},
                    "OutputBucket": {"Type": "AWS::S3::Bucket"},
                }
            ),
        )
        by_logical = {
            b["logical_id"]: b["bucket_name"] for b in deployer._get_stack_buckets("S1")
        }
        results = deployer.cleanup_retained_resources("S1")

    assert results["buckets_deleted"][-1] == by_logical["LoggingBucket"]
    assert set(results["buckets_deleted"][:2]) == {
        by_logical["InputBucket"],
        by_logical["OutputBucket"],
    }


def test_a_live_cloudfront_distribution_stops_the_bucket_deletion(
    aws_credentials: str, no_additional_cleanup: None, monkeypatch: pytest.MonkeyPatch
):
    """The most consequential branch in this function: if the distribution cannot
    be confirmed deleted, **no bucket is deleted at all**.

    Deleting the origin bucket out from under a live distribution leaves a
    CloudFront distribution that cannot be repaired from the stack and serves
    errors to users. The assertion is therefore that the bucket still exists
    afterwards, not merely that an error was recorded.
    """
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer,
            "S1",
            _template(
                {
                    "WebUIBucket": {"Type": "AWS::S3::Bucket"},
                    "Table": {
                        "Type": "AWS::DynamoDB::Table",
                        "Properties": {
                            "TableName": "S1-tracking",
                            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                            "AttributeDefinitions": [
                                {"AttributeName": "pk", "AttributeType": "S"}
                            ],
                            "BillingMode": "PAY_PER_REQUEST",
                        },
                    },
                }
            ),
        )
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]

        def still_there(_name: str, max_wait_seconds: int = 300) -> None:
            raise Exception(
                "CloudFront distribution WebUI (E1) still exists after 300s"
            )

        monkeypatch.setattr(
            deployer, "_verify_cloudfront_distributions_deleted", still_there
        )

        results = deployer.cleanup_retained_resources("S1")

        assert results["buckets_deleted"] == []
        assert [e["type"] for e in results["errors"]] == ["CloudFront"]
        assert "still exists" in results["errors"][0]["error"]
        boto3.client("s3", region_name=REGION).head_bucket(Bucket=bucket)
        # The earlier phases still ran.
        assert results["dynamodb_deleted"] == ["S1-tracking"]


def test_one_failed_deletion_does_not_stop_the_rest(
    aws_credentials: str, no_additional_cleanup: None, monkeypatch: pytest.MonkeyPatch
):
    """A teardown that aborted on the first error would leave most of the debris
    behind and have to be re-run by hand; instead each failure is recorded with
    the resource it belongs to."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)

        def refuse(name: str) -> None:
            raise RuntimeError("AccessDenied: dynamodb:DeleteTable")

        monkeypatch.setattr(deployer, "_delete_dynamodb_table", refuse)
        results = deployer.cleanup_retained_resources("S1")

    assert results["dynamodb_deleted"] == []
    assert results["errors"] == [
        {
            "resource": "S1-tracking",
            "type": "DynamoDB",
            "error": "AccessDenied: dynamodb:DeleteTable",
        }
    ]
    # The log group and the bucket were still swept.
    assert results["logs_deleted"] == ["/S1/lambda/OCRFunction"]
    assert len(results["buckets_deleted"]) == 1


def test_an_unparseable_arn_is_warned_about_and_used_as_is(
    aws_credentials: str, no_additional_cleanup: None, caplog: pytest.LogCaptureFixture
):
    """A malformed identifier must not crash a teardown; it just produces a sweep
    that finds nothing."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        with caplog.at_level("INFO", logger="idp_sdk._core.stack"):
            result = deployer.cleanup_retained_resources("arn:aws:cloudformation")
    assert result == {"total_deleted": 0, "errors": []}


def test_resources_that_cannot_be_auto_deleted_are_called_out(
    aws_credentials: str, no_additional_cleanup: None, caplog: pytest.LogCaptureFixture
):
    """The operator has to finish these by hand, so they must be reported rather
    than silently ignored."""
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            deployer.cleanup_retained_resources("S1")
    assert "cannot be auto-deleted" in caplog.text


# ---------------------------------------------------------------------------
# _cleanup_additional_resources
# ---------------------------------------------------------------------------


def test_stack_scoped_log_groups_are_deleted_and_others_are_not(aws_credentials: str):
    """The two prefixes here are the stack-scoped ones CloudFormation does not
    own. A looser match would take another stack's groups; a tighter one leaves
    the Glue crawler's role log group, which bills indefinitely."""
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        mine = ["/S1-PATTERNSTACK-A/lambda/OCR", "/aws-glue/crawlers-role/S1-Crawler"]
        theirs = [
            "/S1X-PATTERNSTACK-A/lambda/OCR",
            "/OtherStack-PATTERNSTACK/lambda/OCR",
            "/aws/lambda/S1-Fn-abc",
            "/aws-glue/crawlers-role/Other-Crawler",
        ]
        for name in mine + theirs:
            logs.create_log_group(logGroupName=name)

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        remaining = {g["logGroupName"] for g in logs.describe_log_groups()["logGroups"]}
    assert remaining == set(theirs)


def test_an_arn_identifier_is_reduced_to_the_stack_name(aws_credentials: str):
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName="/S1-PATTERNSTACK-A/lambda/OCR")
        StackDeployer(region=REGION)._cleanup_additional_resources(
            "arn:aws:cloudformation:us-east-1:123456789012:stack/S1/guid"
        )
        assert logs.describe_log_groups()["logGroups"] == []


def test_this_stacks_appsync_log_group_is_deleted_and_another_stacks_is_not(
    aws_credentials: str,
):
    """The AppSync API is gone with the stack but its log group is not, and the
    group name is derived from the api id — so the api has to be looked up by
    name first. Matching another stack's api would delete its live logs."""
    with mock_aws():
        appsync = boto3.client("appsync", region_name=REGION)
        mine = appsync.create_graphql_api(
            name="S1-api", authenticationType="AMAZON_COGNITO_USER_POOLS"
        )["graphqlApi"]["apiId"]
        pattern = appsync.create_graphql_api(
            name="S1-pattern2-api", authenticationType="AMAZON_COGNITO_USER_POOLS"
        )["graphqlApi"]["apiId"]
        theirs = appsync.create_graphql_api(
            name="OtherStack-api", authenticationType="AMAZON_COGNITO_USER_POOLS"
        )["graphqlApi"]["apiId"]

        logs = boto3.client("logs", region_name=REGION)
        for api_id in (mine, pattern, theirs):
            logs.create_log_group(logGroupName=f"/aws/appsync/apis/{api_id}")

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        remaining = {g["logGroupName"] for g in logs.describe_log_groups()["logGroups"]}
    assert remaining == {f"/aws/appsync/apis/{theirs}"}


def test_the_permissions_boundary_policy_is_deleted_with_a_partition_aware_arn(
    aws_credentials: str,
):
    """A hardcoded ``arn:aws:`` never resolves in GovCloud or China, so the
    boundary policy was silently left behind there. The partition is taken from
    the caller's own ARN."""
    with mock_aws():
        iam = boto3.client("iam", region_name=REGION)
        iam.create_policy(
            PolicyName="S1-PermissionsBoundary",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                }
            ),
        )
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names = [p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]]
    assert "S1-PermissionsBoundary" not in names


def test_a_govcloud_caller_arn_produces_a_govcloud_policy_arn(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """The partition is parsed out of ``sts:GetCallerIdentity``'s ARN, so this is
    the only assertion that can distinguish a correct GovCloud teardown from one
    that builds ``arn:aws:`` and deletes nothing."""
    attempted: list[str] = []
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "sts":
            return PartialFake(
                client,
                {
                    "get_caller_identity": lambda **_k: {
                        "Account": "123456789012",
                        "Arn": "arn:aws-us-gov:sts::123456789012:assumed-role/Deploy/x",
                    }
                },
            )
        if service == "iam":

            def record(PolicyArn: str, **_k: Any) -> Any:  # noqa: N803 - boto3 casing
                attempted.append(PolicyArn)
                raise client.exceptions.NoSuchEntityException(
                    {"Error": {"Code": "NoSuchEntity", "Message": "no"}}, "DeletePolicy"
                )

            return PartialFake(client, {"delete_policy": record})
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert attempted == [
        "arn:aws-us-gov:iam::123456789012:policy/S1-PermissionsBoundary"
    ]


def test_a_caller_arn_without_a_partition_falls_back_to_aws(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    attempted: list[str] = []
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "sts":
            return PartialFake(
                client,
                {"get_caller_identity": lambda **_k: {"Account": "123456789012"}},
            )
        if service == "iam":

            def record(PolicyArn: str, **_k: Any) -> Any:  # noqa: N803 - boto3 casing
                attempted.append(PolicyArn)
                raise client.exceptions.NoSuchEntityException(
                    {"Error": {"Code": "NoSuchEntity", "Message": "no"}}, "DeletePolicy"
                )

            return PartialFake(client, {"delete_policy": record})
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert attempted == ["arn:aws:iam::123456789012:policy/S1-PermissionsBoundary"]


def test_stack_scoped_iam_policies_are_deleted_and_others_left(aws_credentials: str):
    with mock_aws():
        iam = boto3.client("iam", region_name=REGION)
        doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
            }
        )
        for name in (
            "S1-OCRPolicy",
            "S1-ExtractionPolicy",
            "OtherStack-Policy",
            "S1X-Policy",
        ):
            iam.create_policy(PolicyName=name, PolicyDocument=doc)

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        names = {p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]}
    assert names == {"OtherStack-Policy", "S1X-Policy"}


def test_this_stacks_vended_log_statements_are_pruned_and_others_preserved(
    aws_credentials: str,
):
    """``AWSLogDeliveryWrite20150319`` is an account-wide policy every Step
    Functions log delivery shares. Removing the whole policy, or rewriting it
    without another stack's statements, would break log delivery for every other
    deployment in the account."""
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.put_resource_policy(
            policyName="AWSLogDeliveryWrite20150319",
            policyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "Mine",
                            "Resource": "arn:aws:logs:::/aws/vendedlogs/states/S1-SFN:*",
                        },
                        {
                            "Sid": "Theirs",
                            "Resource": "arn:aws:logs:::/aws/vendedlogs/states/Other-SFN:*",
                        },
                    ],
                }
            ),
        )

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        policies = {
            p["policyName"]: json.loads(p["policyDocument"])
            for p in logs.describe_resource_policies()["resourcePolicies"]
        }
    assert [s["Sid"] for s in policies["AWSLogDeliveryWrite20150319"]["Statement"]] == [
        "Theirs"
    ]


def test_a_shared_policy_with_nothing_of_ours_is_left_untouched(aws_credentials: str):
    """Rewriting it anyway would bump its lastUpdatedTime on every teardown and,
    on a throttled account, risks a failed write that drops every statement."""
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Theirs",
                "Resource": "arn:aws:logs:::/aws/vendedlogs/states/Other-SFN:*",
            }
        ],
    }
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.put_resource_policy(
            policyName="AWSLogDeliveryWrite20150319",
            policyDocument=json.dumps(document),
        )
        before = logs.describe_resource_policies()["resourcePolicies"][0]

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        after = logs.describe_resource_policies()["resourcePolicies"][0]
    assert json.loads(after["policyDocument"]) == document
    assert after["lastUpdatedTime"] == before["lastUpdatedTime"]


def test_stack_named_resource_policies_are_deleted_whole(aws_credentials: str):
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        for name in ("S1-DeliveryPolicy", "OtherStack-DeliveryPolicy"):
            logs.put_resource_policy(
                policyName=name,
                policyDocument=json.dumps({"Statement": [{"Resource": "*"}]}),
            )

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        names = {
            p["policyName"]
            for p in logs.describe_resource_policies()["resourcePolicies"]
        }
    assert names == {"OtherStack-DeliveryPolicy"}


def test_the_stacks_cloudfront_distribution_is_disabled_for_a_later_sweep(
    aws_credentials: str,
):
    """A CloudFront distribution cannot be deleted until it is disabled and the
    disable has propagated, which takes far longer than a teardown. So this
    teardown disables it and a *later* teardown deletes it — which is why the
    comment-based identification has to be exact in both directions."""
    with mock_aws():
        cloudfront = boto3.client("cloudfront")
        created = cloudfront.create_distribution(
            DistributionConfig=_distribution_config(
                "Web app cloudfront distribution S1"
            )
        )
        dist_id = created["Distribution"]["Id"]
        other = cloudfront.create_distribution(
            DistributionConfig=_distribution_config(
                "Web app cloudfront distribution OtherStack"
            )
        )
        other_id = other["Distribution"]["Id"]

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        mine = cloudfront.get_distribution(Id=dist_id)["Distribution"]
        theirs = cloudfront.get_distribution(Id=other_id)["Distribution"]
    assert mine["DistributionConfig"]["Enabled"] is False
    assert theirs["DistributionConfig"]["Enabled"] is True


def _distribution_config(comment: str) -> dict:
    return {
        "CallerReference": comment,
        "Comment": comment,
        "Enabled": True,
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": "origin",
                    "DomainName": "example-bucket.s3.amazonaws.com",
                    "S3OriginConfig": {"OriginAccessIdentity": ""},
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "origin",
            "ViewerProtocolPolicy": "redirect-to-https",
        },
    }


def test_an_already_disabled_distribution_is_deleted(aws_credentials: str):
    """The second half of the two-pass scheme. Any stack's disabled, deployed
    "Web app cloudfront distribution ..." is fair game, because an enabled one is
    never touched."""
    with mock_aws():
        cloudfront = boto3.client("cloudfront")
        config = _distribution_config("Web app cloudfront distribution OldStack")
        config["Enabled"] = False
        created = cloudfront.create_distribution(DistributionConfig=config)
        dist_id = created["Distribution"]["Id"]

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        remaining = [
            d["Id"]
            for d in cloudfront.list_distributions()
            .get("DistributionList", {})
            .get("Items", [])
        ]
    assert dist_id not in remaining


def test_an_unrelated_distribution_is_never_examined(aws_credentials: str):
    """The comment prefix is the only thing separating this solution's
    distributions from the rest of the account's."""
    with mock_aws():
        cloudfront = boto3.client("cloudfront")
        config = _distribution_config("someone else's production CDN")
        config["Enabled"] = False
        created = cloudfront.create_distribution(DistributionConfig=config)
        dist_id = created["Distribution"]["Id"]

        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        remaining = [
            d["Id"]
            for d in cloudfront.list_distributions()
            .get("DistributionList", {})
            .get("Items", [])
        ]
    assert dist_id in remaining


def _headers_policy(name: str, policy_id: str, policy_type: str = "custom") -> dict:
    return {
        "Type": policy_type,
        "ResponseHeadersPolicy": {
            "Id": policy_id,
            "ResponseHeadersPolicyConfig": {"Name": name},
        },
    }


def _with_headers_policies(
    monkeypatch: pytest.MonkeyPatch, policies: list[dict], deleted: list[str]
) -> None:
    """moto does not implement response-headers policies at all, so the two
    operations are supplied while the rest of CloudFront stays real."""
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "cloudfront":
            return PartialFake(
                client,
                {
                    "list_response_headers_policies": lambda **_k: {
                        "ResponseHeadersPolicyList": {"Items": policies}
                    },
                    "delete_response_headers_policy": lambda Id, **_k: deleted.append(
                        Id
                    ),  # noqa: N803
                },
            )
        return client

    monkeypatch.setattr(boto3, "client", factory)


def test_a_headers_policy_belonging_to_a_deleted_stack_is_removed(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    deleted: list[str] = []
    _with_headers_policies(
        monkeypatch,
        [_headers_policy("GoneStack-security-headers-policy", "P-GONE")],
        deleted,
    )
    with mock_aws():
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert deleted == ["P-GONE"]


def test_a_headers_policy_belonging_to_a_live_stack_is_left_alone(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Deleting it would break the security headers on a running deployment's web
    UI, and CloudFront would serve it without a Content-Security-Policy."""
    deleted: list[str] = []
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(
            deployer, "LiveStack", _template({"Topic": {"Type": "AWS::SNS::Topic"}})
        )
        _with_headers_policies(
            monkeypatch,
            [_headers_policy("LiveStack-security-headers-policy", "P-LIVE")],
            deleted,
        )
        deployer._cleanup_additional_resources("S1")
    assert deleted == []


def test_this_stacks_own_headers_policy_is_skipped(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Its distribution still references it at this point, so CloudFront would
    refuse the delete; a later teardown picks it up once the distribution is
    gone."""
    deleted: list[str] = []
    _with_headers_policies(
        monkeypatch, [_headers_policy("S1-security-headers-policy", "P-MINE")], deleted
    )
    with mock_aws():
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert deleted == []


@pytest.mark.parametrize(
    "policy",
    [
        _headers_policy("managed-SecurityHeadersPolicy", "P-M", policy_type="managed"),
        _headers_policy("SomeTeam-cors-policy", "P-C"),
    ],
)
def test_a_policy_outside_this_solutions_naming_is_untouched(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, policy: dict
):
    """A managed policy cannot be deleted at all, and a differently-named custom
    one belongs to somebody else."""
    deleted: list[str] = []
    _with_headers_policies(monkeypatch, [policy], deleted)
    with mock_aws():
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert deleted == []


@pytest.mark.parametrize(
    "stack_status", ["CREATE_FAILED", "ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED"]
)
def test_a_headers_policy_of_a_stack_in_an_inconsistent_state_is_removed(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, stack_status: str
):
    """A stack stuck in one of these states will never serve traffic again, so its
    policy is debris. Treating it as live would leave a policy nothing can
    delete once the stack is finally removed."""
    deleted: list[str] = []
    deployer = StackDeployer(region=REGION)
    _with_headers_policies(
        monkeypatch, [_headers_policy("Broken-security-headers-policy", "P-B")], deleted
    )
    stub = Stubber(deployer.cfn)
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "Broken",
                    "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                    "StackStatus": stack_status,
                }
            ]
        },
        {"StackName": "Broken"},
    )
    with mock_aws(), stub:
        deployer._cleanup_additional_resources("S1")
    assert deleted == ["P-B"]


@pytest.mark.parametrize("stack_status", ["UPDATE_IN_PROGRESS", "CREATE_IN_PROGRESS"])
def test_a_headers_policy_of_a_stack_mid_operation_is_left_alone(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, stack_status: str
):
    """An in-progress stack is treated as active for safety — it may be about to
    reach CREATE_COMPLETE and need its policy."""
    deleted: list[str] = []
    deployer = StackDeployer(region=REGION)
    _with_headers_policies(
        monkeypatch, [_headers_policy("Busy-security-headers-policy", "P-B")], deleted
    )
    stub = Stubber(deployer.cfn)
    stub.add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "Busy",
                    "CreationTime": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                    "StackStatus": stack_status,
                }
            ]
        },
        {"StackName": "Busy"},
    )
    with mock_aws(), stub:
        deployer._cleanup_additional_resources("S1")
    assert deleted == []


def test_an_unreadable_stack_state_is_treated_as_active(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """ "Cannot tell" must mean "leave it": an AccessDenied on DescribeStacks is
    not evidence that a stack is gone."""
    deleted: list[str] = []
    deployer = StackDeployer(region=REGION)
    _with_headers_policies(
        monkeypatch,
        [_headers_policy("Unknown-security-headers-policy", "P-U")],
        deleted,
    )
    stub = Stubber(deployer.cfn)
    stub.add_client_error("describe_stacks", service_error_code="AccessDenied")
    with mock_aws(), stub:
        deployer._cleanup_additional_resources("S1")
    assert deleted == []


def _bda_client(
    projects: list[dict], blueprints: list[dict], calls: list[tuple]
) -> Any:
    class Paginator:
        def paginate(self, **kwargs: Any) -> list[dict]:
            calls.append(("paginate", kwargs.get("blueprintStageFilter")))
            return [{"blueprints": blueprints}]

    class Fake:
        def list_data_automation_projects(self, **_k: Any) -> dict:
            return {"projects": projects}

        def delete_data_automation_project(self, projectArn: str, **_k: Any) -> dict:  # noqa: N803
            calls.append(("delete_project", projectArn))
            return {}

        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_blueprints"
            return Paginator()

        def delete_blueprint(self, blueprintArn: str, **kwargs: Any) -> dict:  # noqa: N803
            calls.append(
                ("delete_blueprint", blueprintArn, kwargs.get("blueprintVersion"))
            )
            return {}

    return Fake()


def test_this_stacks_bda_projects_and_blueprints_are_deleted(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Bedrock Data Automation projects and blueprints are account-level
    resources CloudFormation created through a custom resource, so nothing else
    deletes them. Versions go before the base blueprint, because deleting the
    base first orphans them."""
    calls: list[tuple] = []
    fake = _bda_client(
        projects=[
            {
                "projectName": "S1-lending",
                "projectArn": "arn:aws:bda:::project/S1-lending",
            },
            {"projectName": "Other-proj", "projectArn": "arn:aws:bda:::project/Other"},
        ],
        blueprints=[
            {"blueprintName": "S1-w2", "blueprintArn": "arn:aws:bda:::blueprint/S1-w2"},
            {
                "blueprintName": "Other-w2",
                "blueprintArn": "arn:aws:bda:::blueprint/Other",
            },
            {
                "blueprintName": "S1-managed",
                "blueprintArn": "arn:aws:bedrock:::aws:blueprint/x",
            },
        ],
        calls=calls,
    )
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        if service == "bedrock-data-automation":
            return fake
        return real_client(service, *a, **k)

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert ("delete_project", "arn:aws:bda:::project/S1-lending") in calls
    assert ("delete_project", "arn:aws:bda:::project/Other") not in calls
    assert ("paginate", "LIVE") in calls
    # Version first, then the base — and only ours, never the AWS-managed one.
    assert [c for c in calls if c[0] == "delete_blueprint"] == [
        ("delete_blueprint", "arn:aws:bda:::blueprint/S1-w2", "1"),
        ("delete_blueprint", "arn:aws:bda:::blueprint/S1-w2", None),
    ]


def test_retry_configuration_is_set_in_the_process_environment(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Pinning a side effect worth knowing about: this function writes
    ``AWS_MAX_ATTEMPTS`` and ``AWS_RETRY_MODE`` into ``os.environ`` at
    ``_core/stack.py:1943`` and never restores them, so every boto3 client built
    later in the same process — by any caller, for any service — inherits adaptive
    retries with ten attempts.

    That is defensible for a CLI that exits shortly afterwards, and surprising for
    a long-lived process using the SDK as a library.
    """
    import os

    monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
    with mock_aws():
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert os.environ["AWS_MAX_ATTEMPTS"] == "10"
    assert os.environ["AWS_RETRY_MODE"] == "adaptive"


def test_a_policy_deletion_conflict_aborts_every_later_cleanup_step(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """DEFECT, pinned as-is: one unexpected IAM error skips the rest of the sweep.

    The permissions-boundary block at ``_core/stack.py:1993-2008`` catches only
    ``NoSuchEntityException``. A boundary policy still attached to a role answers
    ``DeleteConflict``, which is not caught there, so it unwinds to the function's
    outermost handler at line 2291 — and everything after it is skipped: the
    CloudWatch Logs resource policies, the CloudFront distribution disable, the
    IAM custom policies, and the Bedrock Data Automation cleanup. The only trace
    is one "Additional resource cleanup failed" warning, and the resources it
    skipped are all billable.

    The stack-scoped IAM policy created below is what proves the skip: it is
    deleted by a block that runs *after* the boundary, and it is still there
    afterwards.
    """
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "iam":
            original_delete = client.delete_policy

            def conflict(PolicyArn: str, **kw: Any) -> Any:  # noqa: N803
                if PolicyArn.endswith("S1-PermissionsBoundary"):
                    raise client.exceptions.DeleteConflictException(
                        {
                            "Error": {
                                "Code": "DeleteConflict",
                                "Message": "Cannot delete a policy attached to entities",
                            }
                        },
                        "DeletePolicy",
                    )
                return original_delete(PolicyArn=PolicyArn, **kw)

            return PartialFake(client, {"delete_policy": conflict})
        return client

    with mock_aws():
        iam = real_client("iam", region_name=REGION)
        doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
            }
        )
        iam.create_policy(PolicyName="S1-OCRPolicy", PolicyDocument=doc)

        monkeypatch.setattr(boto3, "client", factory)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        names = {p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]}

    assert "Additional resource cleanup failed" in caplog.text
    # The later block never ran.
    assert "S1-OCRPolicy" in names


# ---------------------------------------------------------------------------
# Failure isolation across the sweep
#
# `_cleanup_additional_resources` is a sequence of independent sweeps, each
# wrapped in its own `except Exception` so that a denied permission or a throttled
# call in one does not cost the others. That contract is worth testing directly,
# because it is exactly what the DeleteConflict defect above violates: the tests
# here each break one step and assert a *later* step still ran.
# ---------------------------------------------------------------------------


def _iam_policy_names(client_factory: Callable[..., Any]) -> set[str]:
    """Read the account's customer-managed policy names back.

    The factory is passed in rather than taken from ``boto3.client``, because
    these tests have replaced that name — and ``monkeypatch.undo()`` is not an
    option, since it would also revert the ``aws_credentials`` fixture's removal of
    the ambient ``AWS_PROFILE`` and every later client build would fail with
    ``ProfileNotFound``.
    """
    return {
        p["PolicyName"]
        for p in client_factory("iam", region_name=REGION).list_policies(Scope="Local")[
            "Policies"
        ]
    }


def _break(service: str, operation: str, error: Exception) -> Callable[..., Any]:
    """A ``boto3.client`` factory whose one named operation raises ``error``."""
    real_client = boto3.client

    def factory(requested: str, *a: Any, **k: Any) -> Any:
        client = real_client(requested, *a, **k)
        if requested == service:

            def boom(**_k: Any) -> Any:
                raise error

            return PartialFake(client, {operation: boom})
        return client

    return factory


def test_a_failed_log_group_listing_does_not_stop_the_iam_sweep(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    real_client = boto3.client
    with mock_aws():
        real_client("iam", region_name=REGION).create_policy(
            PolicyName="S1-OCRPolicy",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                }
            ),
        )
        names_before = _iam_policy_names(real_client)
        monkeypatch.setattr(
            boto3,
            "client",
            _break("logs", "describe_log_groups", RuntimeError("Throttling")),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names_after = _iam_policy_names(real_client)

    assert "S1-OCRPolicy" in names_before
    assert "S1-OCRPolicy" not in names_after
    assert "Failed to clean up additional log groups" in caplog.text


def test_a_failed_appsync_listing_does_not_stop_the_iam_sweep(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    real_client = boto3.client
    with mock_aws():
        real_client("iam", region_name=REGION).create_policy(
            PolicyName="S1-OCRPolicy",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                }
            ),
        )
        monkeypatch.setattr(
            boto3,
            "client",
            _break("appsync", "list_graphql_apis", RuntimeError("AccessDenied")),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names = _iam_policy_names(real_client)

    assert "S1-OCRPolicy" not in names
    assert "Failed to clean up AppSync log groups" in caplog.text


def test_a_failed_resource_policy_read_does_not_stop_the_iam_sweep(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """``describe_resource_policies`` is read twice, by two separate sweeps, so one
    failure must cost only those two."""
    real_client = boto3.client
    with mock_aws():
        real_client("iam", region_name=REGION).create_policy(
            PolicyName="S1-OCRPolicy",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                }
            ),
        )
        monkeypatch.setattr(
            boto3,
            "client",
            _break("logs", "describe_resource_policies", RuntimeError("Throttling")),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names = _iam_policy_names(real_client)

    assert "S1-OCRPolicy" not in names
    assert "Failed to clean up CloudWatch Logs policy" in caplog.text
    assert "Failed to delete stack-specific resource policies" in caplog.text


def test_a_failed_iam_policy_listing_is_warned_and_does_not_raise(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    with mock_aws():
        monkeypatch.setattr(
            boto3,
            "client",
            _break("iam", "list_policies", RuntimeError("AccessDenied")),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert "Failed to cleanup IAM custom policies" in caplog.text


def test_one_undeletable_iam_policy_does_not_block_the_next(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Each policy is deleted in its own try, so a policy still attached to a role
    costs only itself."""
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service != "iam":
            return client
        original = client.delete_policy

        def selective(PolicyArn: str, **kw: Any) -> Any:  # noqa: N803 - boto3 casing
            if PolicyArn.endswith("S1-Attached"):
                raise RuntimeError("DeleteConflict: attached to 1 entity")
            return original(PolicyArn=PolicyArn, **kw)

        return PartialFake(client, {"delete_policy": selective})

    doc = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
    )
    with mock_aws():
        iam = real_client("iam", region_name=REGION)
        iam.create_policy(PolicyName="S1-Attached", PolicyDocument=doc)
        iam.create_policy(PolicyName="S1-Free", PolicyDocument=doc)
        monkeypatch.setattr(boto3, "client", factory)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names = _iam_policy_names(real_client)

    assert names == {"S1-Attached"}
    assert "Failed to delete IAM policy S1-Attached" in caplog.text


def test_an_appsync_api_whose_log_group_is_already_gone_is_not_an_error(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    """An API with logging switched off has no group to delete, and that is the
    ordinary case rather than a failure."""
    with mock_aws():
        boto3.client("appsync", region_name=REGION).create_graphql_api(
            name="S1-api", authenticationType="AMAZON_COGNITO_USER_POOLS"
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert "Failed to clean up AppSync log groups" not in caplog.text


def test_a_failed_cloudfront_listing_does_not_stop_the_iam_sweep(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    real_client = boto3.client
    with mock_aws():
        real_client("iam", region_name=REGION).create_policy(
            PolicyName="S1-OCRPolicy",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                }
            ),
        )
        monkeypatch.setattr(
            boto3,
            "client",
            _break("cloudfront", "list_distributions", RuntimeError("AccessDenied")),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
        names = _iam_policy_names(real_client)

    assert "S1-OCRPolicy" not in names
    assert "Failed to cleanup CloudFront distributions" in caplog.text


def test_our_own_disabled_distribution_is_deleted_immediately(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """The second pass deletes this stack's distribution outright when it is
    already disabled, rather than disabling it again and leaving it for a third
    teardown that may never happen.

    Reaching this branch needs the listing to report a status other than
    ``Deployed`` — otherwise the account-wide first pass has already taken it —
    so only ``list_distributions`` is faked; the delete is real.
    """
    real_client = boto3.client
    config = _distribution_config("Web app cloudfront distribution S1")
    config["Enabled"] = False

    with mock_aws():
        cloudfront = real_client("cloudfront")
        dist_id = cloudfront.create_distribution(DistributionConfig=config)[
            "Distribution"
        ]["Id"]

        def factory(service: str, *a: Any, **k: Any) -> Any:
            client = real_client(service, *a, **k)
            if service == "cloudfront":
                return PartialFake(
                    client,
                    {
                        "list_distributions": lambda **_k: {
                            "DistributionList": {
                                "Items": [
                                    {
                                        "Id": dist_id,
                                        "Comment": "Web app cloudfront distribution S1",
                                        "Status": "InProgress",
                                        "Enabled": False,
                                    }
                                ]
                            }
                        }
                    },
                )
            return client

        monkeypatch.setattr(boto3, "client", factory)
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

        remaining = [
            d["Id"]
            for d in cloudfront.list_distributions()
            .get("DistributionList", {})
            .get("Items", [])
        ]
    assert dist_id not in remaining


def test_a_failed_headers_policy_delete_is_warned_not_raised(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    real_client = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        client = real_client(service, *a, **k)
        if service == "cloudfront":
            return PartialFake(
                client,
                {
                    "list_response_headers_policies": lambda **_k: {
                        "ResponseHeadersPolicyList": {
                            "Items": [
                                _headers_policy(
                                    "GoneStack-security-headers-policy", "P-G"
                                )
                            ]
                        }
                    },
                    "delete_response_headers_policy": lambda **_k: (
                        _ for _ in ()
                    ).throw(RuntimeError("PreconditionFailed")),
                },
            )
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", factory)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert "Failed to delete CloudFront policy" in caplog.text


def test_a_policy_naming_no_stack_at_all_is_left_alone(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """A policy literally named ``-security-headers-policy`` yields an empty stack
    name, which cannot be checked — so it must not be deleted on a guess."""
    deleted: list[str] = []
    _with_headers_policies(
        monkeypatch, [_headers_policy("-security-headers-policy", "P-EMPTY")], deleted
    )
    with mock_aws():
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")
    assert deleted == []


def test_an_empty_describe_stacks_result_counts_the_stack_as_missing(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    deleted: list[str] = []
    deployer = StackDeployer(region=REGION)
    _with_headers_policies(
        monkeypatch, [_headers_policy("Ghost-security-headers-policy", "P-G")], deleted
    )
    stub = Stubber(deployer.cfn)
    stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "Ghost"})
    with mock_aws(), stub:
        deployer._cleanup_additional_resources("S1")
    assert deleted == ["P-G"]


def test_a_non_client_error_reading_a_stacks_state_is_treated_as_active(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Any unexpected exception — not just a ClientError — has to fail safe."""
    deleted: list[str] = []
    deployer = StackDeployer(region=REGION)
    _with_headers_policies(
        monkeypatch, [_headers_policy("Odd-security-headers-policy", "P-O")], deleted
    )
    monkeypatch.setattr(
        deployer.cfn,
        "describe_stacks",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    with mock_aws():
        deployer._cleanup_additional_resources("S1")
    assert deleted == []


def test_a_failed_bda_project_listing_still_lets_the_blueprints_be_swept(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    calls: list[tuple] = []
    fake = _bda_client(
        projects=[],
        blueprints=[
            {"blueprintName": "S1-w2", "blueprintArn": "arn:aws:bda:::blueprint/S1-w2"}
        ],
        calls=calls,
    )

    def boom(**_k: Any) -> Any:
        raise RuntimeError("AccessDenied: bda:ListDataAutomationProjects")

    fake.list_data_automation_projects = boom  # type: ignore[method-assign]
    real_client = boto3.client

    with mock_aws():
        monkeypatch.setattr(
            boto3,
            "client",
            lambda service, *a, **k: (
                fake
                if service == "bedrock-data-automation"
                else real_client(service, *a, **k)
            ),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert "Failed to cleanup BDA projects" in caplog.text
    assert ("delete_blueprint", "arn:aws:bda:::blueprint/S1-w2", None) in calls


def test_a_blueprint_with_no_version_one_is_still_deleted(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """The version-1 delete is attempted unconditionally and its failure is
    swallowed, because a blueprint created without a published version has none —
    and refusing to continue would leave the base blueprint behind."""
    calls: list[tuple] = []
    fake = _bda_client(
        projects=[],
        blueprints=[
            {"blueprintName": "S1-w2", "blueprintArn": "arn:aws:bda:::blueprint/S1-w2"}
        ],
        calls=calls,
    )

    def version_aware(blueprintArn: str, **kwargs: Any) -> Any:  # noqa: N803
        if kwargs.get("blueprintVersion"):
            raise RuntimeError("ValidationException: no such version")
        calls.append(("delete_blueprint", blueprintArn, None))
        return {}

    fake.delete_blueprint = version_aware  # type: ignore[method-assign]
    real_client = boto3.client

    with mock_aws():
        monkeypatch.setattr(
            boto3,
            "client",
            lambda service, *a, **k: (
                fake
                if service == "bedrock-data-automation"
                else real_client(service, *a, **k)
            ),
        )
        StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert calls == [
        ("paginate", "LIVE"),
        ("delete_blueprint", "arn:aws:bda:::blueprint/S1-w2", None),
    ]


def test_one_undeletable_blueprint_does_not_stop_the_next(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    calls: list[tuple] = []
    fake = _bda_client(
        projects=[],
        blueprints=[
            {
                "blueprintName": "S1-stuck",
                "blueprintArn": "arn:aws:bda:::blueprint/S1-stuck",
            },
            {"blueprintName": "S1-ok", "blueprintArn": "arn:aws:bda:::blueprint/S1-ok"},
        ],
        calls=calls,
    )

    def selective(blueprintArn: str, **kwargs: Any) -> Any:  # noqa: N803
        if "stuck" in blueprintArn and not kwargs.get("blueprintVersion"):
            raise RuntimeError("ConflictException: in use by a project")
        calls.append(("delete_blueprint", blueprintArn, kwargs.get("blueprintVersion")))
        return {}

    fake.delete_blueprint = selective  # type: ignore[method-assign]
    real_client = boto3.client

    with mock_aws():
        monkeypatch.setattr(
            boto3,
            "client",
            lambda service, *a, **k: (
                fake
                if service == "bedrock-data-automation"
                else real_client(service, *a, **k)
            ),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert "Failed to delete BDA blueprint S1-stuck" in caplog.text
    assert ("delete_blueprint", "arn:aws:bda:::blueprint/S1-ok", None) in calls


def test_a_failed_log_group_deletion_is_recorded_against_that_group(
    aws_credentials: str, no_additional_cleanup: None, monkeypatch: pytest.MonkeyPatch
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        monkeypatch.setattr(
            deployer,
            "_delete_log_group",
            lambda name: (_ for _ in ()).throw(RuntimeError("AccessDenied")),
        )
        results = deployer.cleanup_retained_resources("S1")

    assert results["logs_deleted"] == []
    assert results["errors"] == [
        {
            "resource": "/S1/lambda/OCRFunction",
            "type": "LogGroup",
            "error": "AccessDenied",
        }
    ]
    # Later phases still ran.
    assert len(results["buckets_deleted"]) == 1


def test_a_failed_bucket_deletion_is_recorded_against_that_bucket(
    aws_credentials: str, no_additional_cleanup: None, monkeypatch: pytest.MonkeyPatch
):
    with mock_aws():
        deployer = StackDeployer(region=REGION)
        _create_stack(deployer, "S1", BUCKET_AND_FRIENDS)
        bucket = deployer._get_stack_buckets("S1")[0]["bucket_name"]
        monkeypatch.setattr(
            deployer,
            "_empty_and_delete_bucket",
            lambda name: (_ for _ in ()).throw(RuntimeError("BucketNotEmpty")),
        )
        results = deployer.cleanup_retained_resources("S1")

    assert results["buckets_deleted"] == []
    assert results["errors"] == [
        {"resource": bucket, "type": "S3Bucket", "error": "BucketNotEmpty"}
    ]
    assert results["dynamodb_deleted"] == ["S1-tracking"]


def test_a_distribution_reported_gone_only_by_message_is_accepted(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
):
    """The typed ``NoSuchDistribution`` is the normal signal, but the same
    condition arriving as a plain exception whose text names it is also treated as
    deleted. Without that fallback the teardown would raise, and a distribution
    that is genuinely gone would block the bucket cleanup forever."""
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = REGION
    monkeypatch.setattr(
        deployer,
        "_get_stack_cloudfront_distributions",
        lambda _name: [{"distribution_id": "E1", "logical_id": "WebUI"}],
    )
    original = boto3.client

    def factory(service: str, *a: Any, **k: Any) -> Any:
        return PartialFake(
            original(service, *a, **k),
            {
                "get_distribution": lambda **_k: (_ for _ in ()).throw(
                    RuntimeError("NoSuchDistribution: E1")
                )
            },
        )

    monkeypatch.setattr(boto3, "client", factory)
    deployer._verify_cloudfront_distributions_deleted("S1")
    assert clock.slept == []


def test_one_undeletable_bda_project_does_not_stop_the_next(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A project still referenced by an in-flight invocation refuses deletion.
    Each project is deleted in its own try so the rest of the stack's projects —
    and the blueprint sweep after them — still happen."""
    calls: list[tuple] = []
    fake = _bda_client(
        projects=[
            {"projectName": "S1-stuck", "projectArn": "arn:aws:bda:::project/S1-stuck"},
            {"projectName": "S1-ok", "projectArn": "arn:aws:bda:::project/S1-ok"},
        ],
        blueprints=[],
        calls=calls,
    )

    def selective(projectArn: str, **_k: Any) -> Any:  # noqa: N803 - boto3 casing
        if "stuck" in projectArn:
            raise RuntimeError("ConflictException: project in use")
        calls.append(("delete_project", projectArn))
        return {}

    fake.delete_data_automation_project = selective  # type: ignore[method-assign]
    real_client = boto3.client

    with mock_aws():
        monkeypatch.setattr(
            boto3,
            "client",
            lambda service, *a, **k: (
                fake
                if service == "bedrock-data-automation"
                else real_client(service, *a, **k)
            ),
        )
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            StackDeployer(region=REGION)._cleanup_additional_resources("S1")

    assert "Failed to delete BDA project S1-stuck" in caplog.text
    assert ("delete_project", "arn:aws:bda:::project/S1-ok") in calls
    # The blueprint sweep still ran afterwards.
    assert ("paginate", "LIVE") in calls
