# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deletion behaviour of ``OrphanedResourceCleanup``, measured against ``moto``.

``idp_sdk._core.cleanup_orphaned`` is the only code in this SDK whose purpose is
to destroy infrastructure. Each ``cleanup_*`` method lists one class of resource,
attributes it to a stack, asks ``get_stack_state`` whether that stack is gone, and
deletes it if so. The companion file
``test_cleanup_orphaned_name_extraction.py`` covers the attribution step; this
file covers what actually gets deleted.

Every method here is driven against ``moto``'s in-process fakes rather than a
mock, and the assertions read the fake service back afterwards — the surviving
bucket list, the surviving log groups, the distribution's ``Enabled`` flag, the
object still in a live stack's bucket. That distinction is the point of the file:
a ``MagicMock`` records a ``delete_bucket`` call for a bucket name that does not
exist, accepts a missing ``IfMatch``, and cannot tell you whether the resource
you meant to keep is still there. So for every resource class there is a paired
assertion — the orphan is gone **and** the live stack's equivalent resource is
untouched. The second half is the one worth having: mis-attributing a live
stack's bucket is the failure mode that destroys customer documents.

Two things are not moto-backed, and each says why at its fixture:
``cleanup_cloudfront_policies`` (moto has no response-headers-policy API) and a
handful of error paths where the only way to make a real fake fail is to break
the client.

One defect is pinned here rather than fixed: ``cleanup_logs_resource_policies``
cannot remove anything. See
``test_an_orphaned_vendedlogs_statement_is_not_actually_removed``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import boto3
import click
import pytest
from moto import mock_aws

from idp_sdk._core.cleanup_orphaned import OrphanedResourceCleanup

IDP_TEMPLATE = json.dumps(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "AWS GenAI IDP Accelerator - test fixture",
        "Resources": {"Queue": {"Type": "AWS::SQS::Queue", "Properties": {}}},
    }
)

# The two stacks every test in this file works with: one live, one deleted.
# Resource names below derive from these, in the casing each service requires
# (S3 bucket names must be lowercase; IAM and AppSync names carry the `IDP-`
# prefix the cleanup patterns look for).
LIVE_STACK = "IDP-live"
DEAD_STACK = "IDP-dead"


@pytest.fixture
def aws(aws_credentials):
    with mock_aws():
        yield aws_credentials


@pytest.fixture
def cleanup(aws) -> OrphanedResourceCleanup:
    """A cleanup object with ``IDP-live`` active and ``IDP-dead`` deleted.

    Discovery is run eagerly over a single region so that the lazy discovery
    inside ``get_stack_state`` — which would sweep all six of ``IDP_REGIONS`` —
    does not run during the tests. ``run_cleanup`` performs the same step itself.
    """
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(StackName=LIVE_STACK, TemplateBody=IDP_TEMPLATE)
    cfn.create_stack(StackName=DEAD_STACK, TemplateBody=IDP_TEMPLATE)
    cfn.delete_stack(StackName=DEAD_STACK)

    instance = OrphanedResourceCleanup(region="us-east-1")
    instance.discover_idp_stacks(regions=["us-east-1"])
    assert instance.get_stack_state(LIVE_STACK)[0] == "ACTIVE"
    assert instance.get_stack_state(DEAD_STACK)[0] == "DELETED"
    return instance


class PromptScript:
    """A scripted stand-in for ``click.prompt``; an unscripted call is an error."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls: List[str] = []

    def __call__(self, text: str, **_kwargs) -> str:
        self.calls.append(text)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {text!r}")
        return self.answers.pop(0)


@pytest.fixture
def prompt(monkeypatch):
    def install(*answers: str) -> PromptScript:
        script = PromptScript(*answers)
        monkeypatch.setattr(click, "prompt", script)
        return script

    return install


@pytest.fixture
def no_confirm(monkeypatch):
    """Make ``click.confirm`` an error, for paths that must not reach it."""
    calls: List[str] = []

    def explode(text: str, **_kwargs):
        calls.append(text)
        raise AssertionError(f"click.confirm was called: {text!r}")

    monkeypatch.setattr(click, "confirm", explode)
    return calls


class Boom:
    """A client stand-in whose every call raises, for the outer error paths.

    Each ``cleanup_*`` method wraps its whole body in ``except Exception`` and
    reports a ``Failed to list ...`` string. That handler is what keeps one
    unavailable service from aborting a run over the other seven, so it is worth
    a test each; a real fake cannot be made to fail on ``list``.
    """

    def __getattr__(self, name: str):
        def fail(*_args, **_kwargs):
            raise RuntimeError(f"{name} unavailable")

        return fail


# ---------------------------------------------------------------------------
# cleanup_log_groups
# ---------------------------------------------------------------------------

ORPHAN_LAMBDA_GROUP = f"/{DEAD_STACK}-PATTERN2-ABC123/lambda/OCRFunction"
LIVE_LAMBDA_GROUP = f"/{LIVE_STACK}-PATTERN2-ABC123/lambda/OCRFunction"
ORPHAN_GLUE_GROUP = (
    f"/aws-glue/crawlers-role/{DEAD_STACK}-DocumentSectionsCrawlerRole-1AB"
)
UNRELATED_GROUP = "/aws/lambda/some-other-function"
ORPHAN_APPSYNC_GROUP = "/aws/appsync/apis/IDPabcd1234"


def seed_log_groups(region: str = "us-east-1") -> List[str]:
    logs = boto3.client("logs", region_name=region)
    names = [
        ORPHAN_LAMBDA_GROUP,
        LIVE_LAMBDA_GROUP,
        ORPHAN_GLUE_GROUP,
        UNRELATED_GROUP,
        ORPHAN_APPSYNC_GROUP,
    ]
    for name in names:
        logs.create_log_group(logGroupName=name)
    return names


def surviving_log_groups(region: str = "us-east-1") -> set:
    logs = boto3.client("logs", region_name=region)
    return {
        group["logGroupName"]
        for page in logs.get_paginator("describe_log_groups").paginate()
        for group in page["logGroups"]
    }


@pytest.mark.unit
def test_orphaned_log_groups_are_deleted_and_the_live_stack_keeps_its_own(cleanup):
    """The paired assertion: the dead stack's groups go, the live stack's stay.

    A regression that widened the stack-name match — or that stopped consulting
    ``get_stack_state`` — would delete ``LIVE_LAMBDA_GROUP``, destroying the
    operational history of a running deployment. Reading the surviving set back
    from the fake is what makes that visible; asserting on a call list would not
    distinguish the two names.
    """
    seed_log_groups()

    results = cleanup.cleanup_log_groups(auto_approve=True)

    assert surviving_log_groups() == {
        LIVE_LAMBDA_GROUP,
        UNRELATED_GROUP,
        ORPHAN_APPSYNC_GROUP,
    }
    assert sorted(results["deleted"]) == [
        f"{ORPHAN_LAMBDA_GROUP} (stack: {DEAD_STACK})",
        f"{ORPHAN_GLUE_GROUP} (stack: {DEAD_STACK})",
    ]
    assert results["skipped"] == [f"{LIVE_LAMBDA_GROUP} (stack: {LIVE_STACK} - active)"]
    assert results["errors"] == []


@pytest.mark.unit
def test_an_appsync_log_group_is_never_deleted_by_the_log_group_sweep(cleanup):
    """Defect: the AppSync arm of the log-group filter can never delete anything.

    ``cleanup_log_groups`` treats ``/aws/appsync/apis/...IDP...`` as an IDP log
    group, but ``extract_stack_name_from_log_group`` handles only the
    ``/<name>/lambda/`` and Glue-crawler shapes and returns ``""`` for this one —
    so the method ``continue``s past it every time
    (``cleanup_orphaned.py:693-695``). The arm at ``cleanup_orphaned.py:684-687``
    is therefore unreachable in effect.

    Observable consequence: an orphaned AppSync execution-log group is billed
    indefinitely and never reported, not even as skipped. It is not a safety
    problem, which is why this pins the behaviour instead of changing it. Note
    ``cleanup_appsync_apis`` does delete ``/aws/appsync/apis/<apiId>`` as a side
    effect of deleting the API, so the group is only stranded when the API is
    already gone.
    """
    seed_log_groups()

    results = cleanup.cleanup_log_groups(auto_approve=True)

    assert ORPHAN_APPSYNC_GROUP in surviving_log_groups()
    assert not any(ORPHAN_APPSYNC_GROUP in entry for entry in results["deleted"])
    assert not any(ORPHAN_APPSYNC_GROUP in entry for entry in results["skipped"])


@pytest.mark.unit
def test_a_log_group_from_an_unrecognised_stack_is_skipped_not_deleted(cleanup):
    """A stack the tool never verified as IDP must be left alone.

    ``UNKNOWN`` is the state for a stack that is neither active nor a confirmed
    deleted IDP stack — including a stack that still exists in a region the run
    did not scan. Deleting on ``UNKNOWN`` would turn an unscanned region into a
    data-loss event.
    """
    logs = boto3.client("logs", region_name="us-east-1")
    stranger = "/IDP-somebody-else-PATTERN2-XY/lambda/Fn"
    logs.create_log_group(logGroupName=stranger)

    results = cleanup.cleanup_log_groups(auto_approve=True)

    assert stranger in surviving_log_groups()
    assert results["skipped"] == [
        f"{stranger} (stack: IDP-somebody-else - not verified IDP)"
    ]


@pytest.mark.unit
def test_a_dry_run_deletes_no_log_group(cleanup, prompt):
    prompt()
    seed_log_groups()
    before = surviving_log_groups()

    results = cleanup.cleanup_log_groups(dry_run=True)

    assert surviving_log_groups() == before
    assert sorted(results["deleted"]) == [
        f"{ORPHAN_LAMBDA_GROUP} (stack: {DEAD_STACK}) [DRY RUN]",
        f"{ORPHAN_GLUE_GROUP} (stack: {DEAD_STACK}) [DRY RUN]",
    ]


@pytest.mark.unit
def test_declining_the_prompt_leaves_the_log_group_in_place(cleanup, prompt):
    seed_log_groups()

    prompt("n", "n")
    results = cleanup.cleanup_log_groups()

    assert ORPHAN_LAMBDA_GROUP in surviving_log_groups()
    assert ORPHAN_GLUE_GROUP in surviving_log_groups()
    assert len(results["skipped"]) == 3  # two declined plus the live stack's
    assert results["deleted"] == []


@pytest.mark.unit
def test_a_log_group_deletion_failure_is_reported_as_an_error(cleanup, monkeypatch):
    """A delete that raises must land in ``errors``, never in ``deleted``.

    The run continues over the remaining groups, which is the behaviour that
    matters: one group under a retention policy must not strand the rest.
    """
    seed_log_groups()
    real_logs = cleanup.logs

    class RefusingLogs:
        def __getattr__(self, name: str):
            return getattr(real_logs, name)

        def delete_log_group(self, logGroupName: str):
            if logGroupName == ORPHAN_LAMBDA_GROUP:
                raise RuntimeError("AccessDenied")
            return real_logs.delete_log_group(logGroupName=logGroupName)

    monkeypatch.setattr(cleanup, "logs", RefusingLogs())

    results = cleanup.cleanup_log_groups(auto_approve=True)

    assert ORPHAN_LAMBDA_GROUP in surviving_log_groups()
    assert ORPHAN_GLUE_GROUP not in surviving_log_groups()
    assert results["errors"] == [
        f"Failed to delete {ORPHAN_LAMBDA_GROUP}: AccessDenied"
    ]
    assert results["deleted"] == [f"{ORPHAN_GLUE_GROUP} (stack: {DEAD_STACK})"]


@pytest.mark.unit
def test_an_unusable_logs_service_is_reported_rather_than_raised(cleanup, monkeypatch):
    monkeypatch.setattr(cleanup, "logs", Boom())

    results = cleanup.cleanup_log_groups(auto_approve=True)

    assert results["deleted"] == [] and results["skipped"] == []
    assert results["errors"] == ["Failed to list log groups: get_paginator unavailable"]


# ---------------------------------------------------------------------------
# cleanup_appsync_apis
# ---------------------------------------------------------------------------


def seed_appsync_apis(region: str = "us-east-1") -> Dict[str, str]:
    appsync = boto3.client("appsync", region_name=region)
    ids = {}
    for name in (
        f"{DEAD_STACK}-p2-api",
        f"{LIVE_STACK}-p2-api",
        "some-other-app-api",
        f"{DEAD_STACK}-graphql",
    ):
        ids[name] = appsync.create_graphql_api(name=name, authenticationType="API_KEY")[
            "graphqlApi"
        ]["apiId"]
    return ids


def surviving_api_names(region: str = "us-east-1") -> set:
    appsync = boto3.client("appsync", region_name=region)
    return {api["name"] for api in appsync.list_graphql_apis()["graphqlApis"]}


@pytest.mark.unit
def test_an_orphaned_appsync_api_is_deleted_and_the_live_one_survives(cleanup):
    """The live stack's API must still be serving the web UI afterwards.

    ``some-other-app-api`` also has to survive: the filter requires the ``IDP-``
    prefix, so an unrelated AppSync API in the same account is out of scope
    regardless of its suffix. ``IDP-dead-graphql`` survives because it does not
    end in ``-api``.
    """
    seed_appsync_apis()

    results = cleanup.cleanup_appsync_apis(auto_approve=True)

    assert surviving_api_names() == {
        f"{LIVE_STACK}-p2-api",
        "some-other-app-api",
        f"{DEAD_STACK}-graphql",
    }
    assert len(results["deleted"]) == 1
    assert results["deleted"][0].startswith(f"{DEAD_STACK}-p2-api (")
    assert results["skipped"] == [f"{LIVE_STACK}-p2-api (stack: {LIVE_STACK} - active)"]
    assert results["errors"] == []


@pytest.mark.unit
def test_the_apis_own_log_group_is_deleted_with_it(cleanup):
    """Deleting the API also removes ``/aws/appsync/apis/<apiId>``.

    That group is created by AppSync rather than by CloudFormation, so nothing
    else in this module can find it — ``cleanup_log_groups`` cannot attribute it
    to a stack (see the AppSync-arm test above). This is the only path that
    removes it, which is why its absence afterwards is asserted here.
    """
    ids = seed_appsync_apis()
    dead_api_id = ids[f"{DEAD_STACK}-p2-api"]
    live_api_id = ids[f"{LIVE_STACK}-p2-api"]
    logs = boto3.client("logs", region_name="us-east-1")
    logs.create_log_group(logGroupName=f"/aws/appsync/apis/{dead_api_id}")
    logs.create_log_group(logGroupName=f"/aws/appsync/apis/{live_api_id}")

    cleanup.cleanup_appsync_apis(auto_approve=True)

    assert surviving_log_groups() == {f"/aws/appsync/apis/{live_api_id}"}


@pytest.mark.unit
def test_a_missing_appsync_log_group_does_not_turn_into_an_error(cleanup):
    """The log-group delete is best-effort; a missing group is not a failure.

    An API created before log configuration was enabled has no group, and that
    must not make an otherwise successful API deletion look like an error.
    """
    seed_appsync_apis()

    results = cleanup.cleanup_appsync_apis(auto_approve=True)

    assert results["errors"] == []
    assert len(results["deleted"]) == 1


@pytest.mark.unit
def test_a_dry_run_deletes_no_appsync_api(cleanup, prompt):
    prompt()
    seed_appsync_apis()

    results = cleanup.cleanup_appsync_apis(dry_run=True)

    assert f"{DEAD_STACK}-p2-api" in surviving_api_names()
    assert results["deleted"][0].endswith("[DRY RUN]")


@pytest.mark.unit
def test_an_unusable_appsync_service_is_reported_rather_than_raised(
    cleanup, monkeypatch
):
    monkeypatch.setattr(cleanup, "appsync", Boom())

    results = cleanup.cleanup_appsync_apis(auto_approve=True)

    assert results["errors"] == [
        "Failed to list AppSync APIs: list_graphql_apis unavailable"
    ]


# ---------------------------------------------------------------------------
# cleanup_iam_policies
# ---------------------------------------------------------------------------

POLICY_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
    }
)


def seed_iam_policies() -> None:
    iam = boto3.client("iam")
    for name in (
        f"{DEAD_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
        f"{DEAD_STACK}-PermissionsBoundary",
        f"{LIVE_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
        f"{LIVE_STACK}-PermissionsBoundary",
        # Has PATTERN but neither STACK nor LambdaECRAccessPolicy: out of scope.
        f"{DEAD_STACK}-PATTERN2-SomethingElse",
        # No IDP- prefix: out of scope however it is named.
        "other-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
    ):
        iam.create_policy(PolicyName=name, PolicyDocument=POLICY_DOCUMENT)


def surviving_policy_names() -> set:
    iam = boto3.client("iam")
    return {
        policy["PolicyName"]
        for page in iam.get_paginator("list_policies").paginate(Scope="Local")
        for policy in page["Policies"]
    }


@pytest.mark.unit
def test_orphaned_iam_policies_are_deleted_and_the_live_stacks_are_kept(cleanup):
    """Deleting a live stack's permissions boundary would break its Lambdas.

    Both of the dead stack's matching policies must go and both of the live
    stack's must stay, and the two out-of-scope names must be untouched — the
    ``IDP-`` prefix requirement is the only thing standing between this sweep and
    an unrelated team's customer-managed policies.
    """
    seed_iam_policies()

    results = cleanup.cleanup_iam_policies(auto_approve=True)

    assert surviving_policy_names() == {
        f"{LIVE_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
        f"{LIVE_STACK}-PermissionsBoundary",
        f"{DEAD_STACK}-PATTERN2-SomethingElse",
        "other-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
    }
    assert sorted(results["deleted"]) == [
        f"{DEAD_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB (stack: {DEAD_STACK})",
        f"{DEAD_STACK}-PermissionsBoundary (stack: {DEAD_STACK})",
    ]
    assert results["errors"] == []


@pytest.mark.unit
def test_a_dry_run_deletes_no_iam_policy(cleanup, prompt):
    prompt()
    seed_iam_policies()
    before = surviving_policy_names()

    results = cleanup.cleanup_iam_policies(dry_run=True)

    assert surviving_policy_names() == before
    assert all(entry.endswith("[DRY RUN]") for entry in results["deleted"])


@pytest.mark.unit
def test_a_policy_that_cannot_be_deleted_is_reported_and_the_sweep_continues(
    cleanup, monkeypatch
):
    """One refused policy must not strand the rest of the sweep.

    The real shape of this failure is IAM's ``DeleteConflict``: a role left
    behind by a failed stack delete still references the policy. ``moto`` does
    not enforce that conflict — it deletes an attached policy without complaint —
    so the refusal is injected at the client instead. The assertion that matters
    either way is that the refused policy survives, is reported under ``errors``,
    and the next policy is still deleted.
    """
    seed_iam_policies()
    refused = f"{DEAD_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB"
    real_iam = cleanup.iam

    class RefusingIam:
        def __getattr__(self, name: str):
            return getattr(real_iam, name)

        def delete_policy(self, PolicyArn: str):
            if PolicyArn.endswith(f"/{refused}"):
                raise RuntimeError("DeleteConflict")
            return real_iam.delete_policy(PolicyArn=PolicyArn)

    monkeypatch.setattr(cleanup, "iam", RefusingIam())

    results = cleanup.cleanup_iam_policies(auto_approve=True)

    assert refused in surviving_policy_names()
    assert results["errors"] == [f"Failed to delete policy {refused}: DeleteConflict"]
    assert results["deleted"] == [
        f"{DEAD_STACK}-PermissionsBoundary (stack: {DEAD_STACK})"
    ]


@pytest.mark.unit
def test_an_unusable_iam_service_is_reported_rather_than_raised(cleanup, monkeypatch):
    monkeypatch.setattr(cleanup, "iam", Boom())

    results = cleanup.cleanup_iam_policies(auto_approve=True)

    assert results["errors"] == [
        "Failed to list IAM policies: get_paginator unavailable"
    ]


# ---------------------------------------------------------------------------
# cleanup_s3_buckets and _empty_s3_bucket
# ---------------------------------------------------------------------------

DEAD_INPUT_BUCKET = "idp-dead-inputbucket-1a2b3c"
DEAD_OUTPUT_BUCKET = "idp-dead-outputbucket-1a2b3c"
LIVE_INPUT_BUCKET = "idp-live-inputbucket-1a2b3c"
UNRELATED_BUCKET = "some-other-application-data"


def seed_buckets() -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    for bucket in (
        DEAD_INPUT_BUCKET,
        DEAD_OUTPUT_BUCKET,
        LIVE_INPUT_BUCKET,
        UNRELATED_BUCKET,
    ):
        s3.create_bucket(Bucket=bucket)
    # The dead input bucket is versioned and holds several versions plus a
    # delete marker, which is the state a real IDP input bucket is in.
    s3.put_bucket_versioning(
        Bucket=DEAD_INPUT_BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    s3.put_object(Bucket=DEAD_INPUT_BUCKET, Key="in/a.pdf", Body=b"v1")
    s3.put_object(Bucket=DEAD_INPUT_BUCKET, Key="in/a.pdf", Body=b"v2")
    s3.delete_object(Bucket=DEAD_INPUT_BUCKET, Key="in/a.pdf")
    s3.put_object(Bucket=DEAD_INPUT_BUCKET, Key="in/b.pdf", Body=b"v1")
    s3.put_object(Bucket=DEAD_OUTPUT_BUCKET, Key="out/a.json", Body=b"{}")
    s3.put_object(Bucket=LIVE_INPUT_BUCKET, Key="in/live.pdf", Body=b"precious")


def surviving_buckets() -> set:
    s3 = boto3.client("s3", region_name="us-east-1")
    return {bucket["Name"] for bucket in s3.list_buckets()["Buckets"]}


@pytest.mark.unit
def test_orphaned_buckets_are_emptied_and_deleted_and_the_live_one_keeps_its_data(
    cleanup,
):
    """The most consequential assertion in the module.

    S3 buckets hold the customer documents. The live stack's bucket must still
    exist **and** still contain its object: a mis-attribution that emptied it
    without deleting it would leave the bucket list looking correct while the
    documents were gone, so the object is read back rather than the bucket name
    alone.
    """
    seed_buckets()
    s3 = boto3.client("s3", region_name="us-east-1")

    results = cleanup.cleanup_s3_buckets(auto_approve=True)

    assert surviving_buckets() == {LIVE_INPUT_BUCKET, UNRELATED_BUCKET}
    assert (
        s3.get_object(Bucket=LIVE_INPUT_BUCKET, Key="in/live.pdf")["Body"].read()
        == b"precious"
    )
    # The stack name in each message is the one *extracted from the bucket
    # name*, so it carries the bucket's lowercase casing rather than the
    # CloudFormation stack's. Asserted rather than normalised: this string is
    # what an operator reads when deciding whether a deletion was the one they
    # meant, and it does not match the stack name they would look up.
    assert sorted(results["deleted"]) == [
        f"{DEAD_INPUT_BUCKET} (stack: {DEAD_STACK.lower()})",
        f"{DEAD_OUTPUT_BUCKET} (stack: {DEAD_STACK.lower()})",
    ]
    assert results["skipped"] == [
        f"{LIVE_INPUT_BUCKET} (stack: {LIVE_STACK.lower()} - active)"
    ]
    assert results["errors"] == []


@pytest.mark.unit
def test_a_bucket_belonging_to_no_known_stack_is_not_even_reported(cleanup):
    """An ``UNKNOWN`` bucket is skipped silently, unlike other resource types.

    ``cleanup_s3_buckets`` ``continue``s on ``UNKNOWN`` rather than appending to
    ``skipped`` as the log-group and API sweeps do. The practical effect is that
    a bucket from a stack in an unscanned region produces no output at all, so an
    operator cannot tell it was considered. Pinned because the asymmetry is
    easy to "fix" in the unsafe direction.
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="idp-somebody-else-inputbucket-9z")

    results = cleanup.cleanup_s3_buckets(auto_approve=True)

    assert "idp-somebody-else-inputbucket-9z" in surviving_buckets()
    assert results["skipped"] == []
    assert results["deleted"] == []


@pytest.mark.unit
def test_a_dry_run_deletes_no_bucket(cleanup, prompt):
    prompt()
    seed_buckets()

    results = cleanup.cleanup_s3_buckets(dry_run=True)

    assert surviving_buckets() == {
        DEAD_INPUT_BUCKET,
        DEAD_OUTPUT_BUCKET,
        LIVE_INPUT_BUCKET,
        UNRELATED_BUCKET,
    }
    assert sorted(results["deleted"]) == [
        f"{DEAD_INPUT_BUCKET} (stack: {DEAD_STACK.lower()}) [DRY RUN]",
        f"{DEAD_OUTPUT_BUCKET} (stack: {DEAD_STACK.lower()}) [DRY RUN]",
    ]


@pytest.mark.unit
def test_a_bucket_that_could_not_be_emptied_is_reported_not_claimed_deleted(
    cleanup, monkeypatch
):
    """If emptying fails, S3 refuses the delete and the tool must say so.

    Emptying is neutralised here so that ``delete_bucket`` meets the real
    ``BucketNotEmpty`` from the fake. The bucket must survive and appear under
    ``errors``; appearing under ``deleted`` would tell the operator a bucket was
    removed while it was still holding data and still costing money.
    """
    seed_buckets()
    monkeypatch.setattr(cleanup, "_empty_s3_bucket", lambda bucket_name: None)

    results = cleanup.cleanup_s3_buckets(auto_approve=True)

    assert DEAD_INPUT_BUCKET in surviving_buckets()
    assert DEAD_OUTPUT_BUCKET in surviving_buckets()
    assert results["deleted"] == []
    assert len(results["errors"]) == 2
    assert all(
        entry.startswith("Failed to delete bucket ") for entry in results["errors"]
    )


@pytest.mark.unit
def test_an_unusable_s3_service_is_reported_rather_than_raised(cleanup, monkeypatch):
    monkeypatch.setattr(cleanup, "s3", Boom())

    results = cleanup.cleanup_s3_buckets(auto_approve=True)

    assert results["errors"] == ["Failed to list S3 buckets: list_buckets unavailable"]


@pytest.mark.unit
def test_empty_s3_bucket_removes_every_version_and_delete_marker(cleanup):
    """A versioned bucket is only deletable once versions *and* markers are gone.

    ``list_object_versions`` is read back because ``list_objects_v2`` reports an
    empty bucket as soon as the current versions are deleted, while S3 still
    refuses ``delete_bucket``. Checking the wrong listing is the mistake this
    test exists to catch.
    """
    seed_buckets()
    s3 = boto3.client("s3", region_name="us-east-1")

    cleanup._empty_s3_bucket(DEAD_INPUT_BUCKET)

    listing = s3.list_object_versions(Bucket=DEAD_INPUT_BUCKET)
    assert listing.get("Versions", []) == []
    assert listing.get("DeleteMarkers", []) == []
    s3.delete_bucket(Bucket=DEAD_INPUT_BUCKET)  # must now succeed


@pytest.mark.unit
def test_empty_s3_bucket_also_empties_an_unversioned_bucket(cleanup):
    seed_buckets()
    s3 = boto3.client("s3", region_name="us-east-1")

    cleanup._empty_s3_bucket(DEAD_OUTPUT_BUCKET)

    assert s3.list_objects_v2(Bucket=DEAD_OUTPUT_BUCKET)["KeyCount"] == 0


@pytest.mark.unit
def test_empty_s3_bucket_swallows_a_missing_bucket(cleanup):
    """Emptying a bucket that is not there must not raise.

    Both nested handlers exist so that a bucket deleted between the listing and
    the delete does not abort the whole sweep. A raise here would propagate out
    of ``delete_bucket``'s helper and be recorded as an error for a bucket that
    was already gone.
    """
    cleanup._empty_s3_bucket("no-such-bucket-at-all-12345")


# ---------------------------------------------------------------------------
# cleanup_dynamodb_tables
# ---------------------------------------------------------------------------

DEAD_TRACKING_TABLE = f"{DEAD_STACK}-TrackingTable-1AB"
DEAD_CONFIG_TABLE = f"{DEAD_STACK}-ConfigTable-1AB"
LIVE_TRACKING_TABLE = f"{LIVE_STACK}-TrackingTable-1AB"
UNRELATED_TABLE = "some-other-application-sessions"


def create_table(name: str) -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=name,
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


def seed_tables() -> None:
    for name in (
        DEAD_TRACKING_TABLE,
        DEAD_CONFIG_TABLE,
        LIVE_TRACKING_TABLE,
        UNRELATED_TABLE,
    ):
        create_table(name)
    boto3.resource("dynamodb", region_name="us-east-1").Table(
        LIVE_TRACKING_TABLE
    ).put_item(
        Item={"PK": "doc#in/live.pdf", "SK": "none", "ObjectStatus": "COMPLETED"}
    )


def surviving_tables() -> set:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    return {
        name
        for page in ddb.get_paginator("list_tables").paginate()
        for name in page["TableNames"]
    }


@pytest.mark.unit
def test_orphaned_tables_are_deleted_and_the_live_tracking_table_keeps_its_rows(
    cleanup,
):
    """The live stack's tracking table is its record of every document processed.

    Its row is read back for the same reason the live bucket's object is: a
    table that exists but has been emptied looks fine from ``list_tables``.
    """
    seed_tables()

    results = cleanup.cleanup_dynamodb_tables(auto_approve=True)

    assert surviving_tables() == {LIVE_TRACKING_TABLE, UNRELATED_TABLE}
    live = boto3.resource("dynamodb", region_name="us-east-1").Table(
        LIVE_TRACKING_TABLE
    )
    assert (
        live.get_item(Key={"PK": "doc#in/live.pdf", "SK": "none"})["Item"][
            "ObjectStatus"
        ]
        == "COMPLETED"
    )
    assert sorted(results["deleted"]) == [
        f"{DEAD_CONFIG_TABLE} (stack: {DEAD_STACK})",
        f"{DEAD_TRACKING_TABLE} (stack: {DEAD_STACK})",
    ]
    assert results["skipped"] == [
        f"{LIVE_TRACKING_TABLE} (stack: {LIVE_STACK} - active)"
    ]
    assert results["errors"] == []


@pytest.mark.unit
def test_point_in_time_recovery_is_disabled_before_a_table_is_deleted(cleanup):
    """PITR is turned off first, and a table without it must still be deletable.

    A continuous backup outlives the table and keeps billing, so the call is
    made unconditionally and its failure swallowed. The two halves are asserted
    together: the request is issued with ``PointInTimeRecoveryEnabled: False``,
    and a table for which the call fails is still deleted.
    """
    seed_tables()
    seen: List[Tuple[str, Any]] = []
    real_ddb = cleanup.dynamodb

    class RecordingDynamo:
        def __getattr__(self, name: str):
            return getattr(real_ddb, name)

        def update_continuous_backups(
            self, TableName, PointInTimeRecoverySpecification
        ):
            seen.append((TableName, PointInTimeRecoverySpecification))
            if TableName == DEAD_CONFIG_TABLE:
                raise RuntimeError("ContinuousBackupsUnavailable")
            return real_ddb.update_continuous_backups(
                TableName=TableName,
                PointInTimeRecoverySpecification=PointInTimeRecoverySpecification,
            )

    cleanup.dynamodb = RecordingDynamo()

    results = cleanup.cleanup_dynamodb_tables(auto_approve=True)

    assert sorted(seen) == [
        (DEAD_CONFIG_TABLE, {"PointInTimeRecoveryEnabled": False}),
        (DEAD_TRACKING_TABLE, {"PointInTimeRecoveryEnabled": False}),
    ]
    assert surviving_tables() == {LIVE_TRACKING_TABLE, UNRELATED_TABLE}
    assert results["errors"] == []


@pytest.mark.unit
def test_a_dry_run_deletes_no_table(cleanup, prompt):
    prompt()
    seed_tables()

    results = cleanup.cleanup_dynamodb_tables(dry_run=True)

    assert DEAD_TRACKING_TABLE in surviving_tables()
    assert all(entry.endswith("[DRY RUN]") for entry in results["deleted"])


@pytest.mark.unit
def test_a_table_delete_failure_is_reported_as_an_error(cleanup):
    """A table that vanishes between listing and deletion is reported, not fatal."""
    seed_tables()
    real_ddb = cleanup.dynamodb

    class RefusingDynamo:
        def __getattr__(self, name: str):
            return getattr(real_ddb, name)

        def delete_table(self, TableName: str):
            if TableName == DEAD_TRACKING_TABLE:
                raise RuntimeError("ResourceNotFoundException")
            return real_ddb.delete_table(TableName=TableName)

    cleanup.dynamodb = RefusingDynamo()

    results = cleanup.cleanup_dynamodb_tables(auto_approve=True)

    assert results["errors"] == [
        f"Failed to delete table {DEAD_TRACKING_TABLE}: ResourceNotFoundException"
    ]
    assert results["deleted"] == [f"{DEAD_CONFIG_TABLE} (stack: {DEAD_STACK})"]


@pytest.mark.unit
def test_an_unusable_dynamodb_service_is_reported_rather_than_raised(
    cleanup, monkeypatch
):
    monkeypatch.setattr(cleanup, "dynamodb", Boom())

    results = cleanup.cleanup_dynamodb_tables(auto_approve=True)

    assert results["errors"] == [
        "Failed to list DynamoDB tables: get_paginator unavailable"
    ]


# ---------------------------------------------------------------------------
# cleanup_cloudfront_distributions
# ---------------------------------------------------------------------------


def distribution_config(comment: str, reference: str) -> Dict[str, Any]:
    return {
        "CallerReference": reference,
        "Comment": comment,
        "Enabled": True,
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": "origin1",
                    "DomainName": "example.com",
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "http-only",
                    },
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "origin1",
            "ViewerProtocolPolicy": "allow-all",
            "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
            "MinTTL": 0,
        },
    }


def seed_distributions() -> Dict[str, str]:
    cf = boto3.client("cloudfront", region_name="us-east-1")
    ids = {}
    for label, comment in (
        ("dead", f"Web app cloudfront distribution {DEAD_STACK}"),
        ("live", f"Web app cloudfront distribution {LIVE_STACK}"),
        ("stranger", f"Web app cloudfront distribution {DEAD_STACK}-unheard-of"),
        ("unrelated", "Marketing site distribution"),
    ):
        ids[label] = cf.create_distribution(
            DistributionConfig=distribution_config(comment, label)
        )["Distribution"]["Id"]
    return ids


@pytest.mark.unit
def test_an_orphaned_distribution_is_disabled_first_then_deleted_on_a_second_pass(
    cleanup,
):
    """CloudFront refuses to delete an enabled distribution, so this takes two runs.

    The first run must leave the distribution *present and disabled* — deleting
    it in one pass is impossible, and reporting it as deleted would be a lie the
    operator acts on. The second run, once the fake reports it disabled and
    deployed, removes it. Throughout, the live stack's distribution must stay
    enabled: disabling it takes the deployment's web UI offline, which is the
    damage short of deletion that this sweep can do.
    """
    ids = seed_distributions()
    cf = boto3.client("cloudfront", region_name="us-east-1")

    first = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert first["disabled"] == [f"{ids['dead']} (stack: {DEAD_STACK})"]
    assert first["deleted"] == []
    assert (
        cf.get_distribution(Id=ids["dead"])["Distribution"]["DistributionConfig"][
            "Enabled"
        ]
        is False
    )
    assert (
        cf.get_distribution(Id=ids["live"])["Distribution"]["DistributionConfig"][
            "Enabled"
        ]
        is True
    )

    second = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert second["deleted"] == [f"{ids['dead']} (stack: {DEAD_STACK})"]
    remaining = {
        item["Id"] for item in cf.list_distributions()["DistributionList"]["Items"]
    }
    assert remaining == {ids["live"], ids["stranger"], ids["unrelated"]}


@pytest.mark.unit
def test_distributions_outside_the_solution_are_reported_correctly(cleanup):
    """Three non-targets, each skipped for a different and correct reason."""
    ids = seed_distributions()

    results = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert set(results["skipped"]) == {
        f"{ids['live']} (stack: {LIVE_STACK} - active)",
        f"{ids['stranger']} (stack: {DEAD_STACK}-unheard-of - not verified IDP)",
    }
    # The unrelated distribution's comment fails the prefix test, so it is not
    # reported at all rather than skipped by name.
    assert not any(ids["unrelated"] in entry for entry in results["skipped"])


@pytest.mark.unit
def test_a_dry_run_neither_disables_nor_deletes_a_distribution(cleanup, prompt):
    prompt()
    ids = seed_distributions()
    cf = boto3.client("cloudfront", region_name="us-east-1")

    results = cleanup.cleanup_cloudfront_distributions(dry_run=True)

    assert results["disabled"] == [f"{ids['dead']} (stack: {DEAD_STACK}) [DRY RUN]"]
    assert (
        cf.get_distribution(Id=ids["dead"])["Distribution"]["DistributionConfig"][
            "Enabled"
        ]
        is True
    )


@pytest.mark.unit
def test_declining_the_disable_prompt_leaves_the_distribution_enabled(cleanup, prompt):
    ids = seed_distributions()
    cf = boto3.client("cloudfront", region_name="us-east-1")

    prompt("n")
    results = cleanup.cleanup_cloudfront_distributions()

    assert f"{ids['dead']} (stack: {DEAD_STACK} - user declined)" in results["skipped"]
    assert (
        cf.get_distribution(Id=ids["dead"])["Distribution"]["DistributionConfig"][
            "Enabled"
        ]
        is True
    )


@pytest.mark.unit
def test_an_unusable_cloudfront_service_is_reported_rather_than_raised(
    cleanup, monkeypatch
):
    monkeypatch.setattr(cleanup, "cloudfront", Boom())

    results = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert results["errors"] == [
        "Failed to list CloudFront distributions: list_distributions unavailable"
    ]


# ---------------------------------------------------------------------------
# cleanup_cloudfront_policies
# ---------------------------------------------------------------------------


class FakeResponseHeadersPolicies:
    """A scripted CloudFront client for the response-headers-policy API.

    ``moto`` implements no part of that API — ``list_response_headers_policies``
    answers HTTP 404 — so this is the one resource class in the module with no
    real fake available. The stand-in still carries the genuine ``exceptions``
    namespace from a botocore client, because the source catches
    ``ResponseHeadersPolicyInUse`` by class and a hand-rolled exception would
    make that handler look reachable when it is not.
    """

    def __init__(self, names: List[str], in_use: Tuple[str, ...] = ()):
        self.exceptions = boto3.client("cloudfront", region_name="us-east-1").exceptions
        self.policies = {f"POLICY{index}": name for index, name in enumerate(names)}
        self.in_use = set(in_use)
        self.deleted: List[Tuple[str, str]] = []
        self.etag_requests: List[str] = []
        # Set to an exception instance to make every delete fail with it.
        self.delete_error: Any = None

    def list_response_headers_policies(self):
        items: List[Dict[str, Any]] = [
            {
                "Type": "custom",
                "ResponseHeadersPolicy": {
                    "Id": policy_id,
                    "ResponseHeadersPolicyConfig": {"Name": name},
                },
            }
            for policy_id, name in self.policies.items()
        ]
        # A managed policy has no config the tool may read; it must be skipped on
        # `Type` before anything else is touched.
        items.append({"Type": "managed"})
        return {"ResponseHeadersPolicyList": {"Items": items}}

    def get_response_headers_policy(self, Id: str):
        self.etag_requests.append(Id)
        return {"ETag": f"ETAG-{Id}"}

    def delete_response_headers_policy(self, Id: str, IfMatch: str):
        if self.delete_error is not None:
            raise self.delete_error
        if self.policies.get(Id) in self.in_use:
            raise self.exceptions.ResponseHeadersPolicyInUse(
                {
                    "Error": {
                        "Code": "ResponseHeadersPolicyInUse",
                        "Message": "still attached",
                    }
                },
                "DeleteResponseHeadersPolicy",
            )
        self.deleted.append((Id, IfMatch))
        return {}


@pytest.mark.unit
def test_only_the_dead_stacks_response_headers_policy_is_deleted(cleanup):
    """Deletion carries the ETag fetched for that same policy id.

    CloudFront requires ``IfMatch``; sending the wrong one deletes nothing and
    sending a stale one can delete a policy that has since been changed. The
    recorded pair is asserted, not just the fact of a delete call.
    """
    fake = FakeResponseHeadersPolicies(
        [
            f"{DEAD_STACK}-security-headers-policy",
            f"{LIVE_STACK}-security-headers-policy",
            # Right suffix, unknown stack.
            "IDP-somebody-else-security-headers-policy",
            # IDP prefix but not a security-headers policy.
            f"{DEAD_STACK}-cache-policy",
            # Right suffix, no IDP prefix.
            "other-app-security-headers-policy",
        ]
    )
    cleanup.cloudfront = fake

    results = cleanup.cleanup_cloudfront_policies(auto_approve=True)

    assert fake.deleted == [("POLICY0", "ETAG-POLICY0")]
    assert fake.etag_requests == ["POLICY0"]
    assert results["deleted"] == [
        f"{DEAD_STACK}-security-headers-policy (stack: {DEAD_STACK})"
    ]
    assert set(results["skipped"]) == {
        "IDP-somebody-else-security-headers-policy "
        "(stack: IDP-somebody-else - not verified IDP)",
        f"{LIVE_STACK}-security-headers-policy (stack: {LIVE_STACK} - active)",
    }
    assert results["errors"] == []


@pytest.mark.unit
def test_a_policy_still_attached_to_a_distribution_is_skipped_with_advice(cleanup):
    """``ResponseHeadersPolicyInUse`` is a "come back later", not an error.

    A distribution takes 15-20 minutes to finish deleting after being disabled,
    and until it does its policy cannot go. Recording that as an error would make
    a normal two-stage cleanup look like a failure, so the message tells the
    operator to re-run.
    """
    fake = FakeResponseHeadersPolicies(
        [f"{DEAD_STACK}-security-headers-policy"],
        in_use=(f"{DEAD_STACK}-security-headers-policy",),
    )
    cleanup.cloudfront = fake

    results = cleanup.cleanup_cloudfront_policies(auto_approve=True)

    assert fake.deleted == []
    assert results["errors"] == []
    assert len(results["skipped"]) == 1
    assert "still in use by distribution" in results["skipped"][0]
    assert "re-run after distributions are deleted" in results["skipped"][0]


@pytest.mark.unit
def test_a_policy_delete_failure_other_than_in_use_is_an_error(cleanup):
    fake = FakeResponseHeadersPolicies([f"{DEAD_STACK}-security-headers-policy"])
    fake.delete_error = RuntimeError("AccessDenied")
    cleanup.cloudfront = fake

    results = cleanup.cleanup_cloudfront_policies(auto_approve=True)

    assert results["deleted"] == []
    assert results["errors"] == [
        f"Failed to delete policy {DEAD_STACK}-security-headers-policy: AccessDenied"
    ]


@pytest.mark.unit
def test_a_dry_run_deletes_no_policy(cleanup, prompt):
    prompt()
    fake = FakeResponseHeadersPolicies([f"{DEAD_STACK}-security-headers-policy"])
    cleanup.cloudfront = fake

    results = cleanup.cleanup_cloudfront_policies(dry_run=True)

    assert fake.deleted == [] and fake.etag_requests == []
    assert results["deleted"] == [
        f"{DEAD_STACK}-security-headers-policy (stack: {DEAD_STACK}) [DRY RUN]"
    ]


@pytest.mark.unit
def test_an_unusable_policy_api_is_reported_rather_than_raised(cleanup):
    """moto's real answer here is a 404, which is also the shape of an old API.

    Running the sweep against the real moto CloudFront exercises the outer
    handler with a genuine ``ClientError`` rather than a synthetic one.
    """
    results = cleanup.cleanup_cloudfront_policies(auto_approve=True)

    assert results["deleted"] == []
    assert len(results["errors"]) == 1
    assert results["errors"][0].startswith("Failed to list CloudFront policies:")


# ---------------------------------------------------------------------------
# cleanup_logs_resource_policies
# ---------------------------------------------------------------------------

VENDEDLOGS_POLICY_NAME = "AWSLogDeliveryWrite20150319"


def vendedlogs_statement(stack: str, sid: str) -> Dict[str, Any]:
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"Service": "delivery.logs.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            f"/aws/vendedlogs/states/{stack}-PATTERN2-StateMachine:*"
        ),
    }


def seed_resource_policy() -> Dict[str, Any]:
    document = {
        "Version": "2012-10-17",
        "Statement": [
            vendedlogs_statement(DEAD_STACK, "dead"),
            vendedlogs_statement(LIVE_STACK, "live"),
            {
                "Sid": "unrelated",
                "Effect": "Allow",
                "Principal": {"Service": "delivery.logs.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": (
                    "arn:aws:logs:us-east-1:123456789012:log-group:/other/group:*"
                ),
            },
        ],
    }
    boto3.client("logs", region_name="us-east-1").put_resource_policy(
        policyName=VENDEDLOGS_POLICY_NAME, policyDocument=json.dumps(document)
    )
    return document


def stored_resource_policy() -> Dict[str, Any]:
    policies = boto3.client(
        "logs", region_name="us-east-1"
    ).describe_resource_policies()["resourcePolicies"]
    stored = {policy["policyName"]: policy["policyDocument"] for policy in policies}
    return json.loads(stored[VENDEDLOGS_POLICY_NAME])


@pytest.mark.unit
def test_an_orphaned_vendedlogs_statement_is_not_actually_removed(cleanup, no_confirm):
    """Defect: ``cleanup_logs_resource_policies`` can never remove a statement.

    The filter at ``cleanup_orphaned.py:1004-1027`` appends a statement to
    ``new_statements`` and ``continue``s when the owning stack is *not* deleted,
    and otherwise falls through to the unconditional
    ``new_statements.append(stmt)`` at line 1027. So the statement belonging to a
    deleted stack is appended too: ``new_count`` always equals
    ``original_count``, the ``if new_count < original_count`` block never runs,
    and ``put_resource_policy`` is never called.

    Observable consequence: the CloudWatch Logs resource policy grows a statement
    per deployment and is never pruned. That matters because the policy has a
    5,120-character limit, and once it is full Step Functions logging cannot be
    configured for a *new* stack — the failure surfaces as a deployment error in
    an unrelated stack, with nothing pointing back here.

    The fix is to move the ``continue`` so the delete path skips the tail append;
    this test pins the current behaviour and will fail (correctly) when that is
    done. ``click.confirm`` is made fatal by the ``no_confirm`` fixture to show
    the operator is not even asked.
    """
    original = seed_resource_policy()

    results = cleanup.cleanup_logs_resource_policies(auto_approve=True)

    assert results["updated"] == []
    assert results["errors"] == []
    assert stored_resource_policy() == original
    assert no_confirm == []


@pytest.mark.unit
def test_a_resource_policy_with_another_name_is_left_alone(cleanup, no_confirm):
    """Only ``AWSLogDeliveryWrite20150319`` is considered.

    Other resource policies in the account belong to other services, and the
    filter keys on the exact name.
    """
    boto3.client("logs", region_name="us-east-1").put_resource_policy(
        policyName="SomeOtherDeliveryPolicy",
        policyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [vendedlogs_statement(DEAD_STACK, "dead")],
            }
        ),
    )

    results = cleanup.cleanup_logs_resource_policies(auto_approve=True)

    assert results == {"updated": [], "deleted": [], "errors": []}


@pytest.mark.unit
def test_a_dry_run_reports_no_resource_policy_change(cleanup, no_confirm):
    """The dry-run branch is unreachable for the same reason as the write branch.

    Both live inside ``if new_count < original_count``, which the defect above
    makes always false, so a dry run reports nothing to do even when there are
    orphaned statements. Pinned so that fixing the filter is seen to fix both.
    """
    seed_resource_policy()

    results = cleanup.cleanup_logs_resource_policies(dry_run=True)

    assert results["updated"] == []


@pytest.mark.unit
def test_an_unusable_logs_resource_policy_api_is_reported_rather_than_raised(
    cleanup, monkeypatch
):
    monkeypatch.setattr(cleanup, "logs", Boom())

    results = cleanup.cleanup_logs_resource_policies(auto_approve=True)

    assert results["errors"] == [
        "Failed to list resource policies: describe_resource_policies unavailable"
    ]


@pytest.mark.unit
def test_a_malformed_policy_document_is_reported_per_policy(cleanup, no_confirm):
    """A policy document that will not parse must not abort the sweep.

    The inner handler reports ``Failed to update <name>`` and the outer one keeps
    the rest of the run alive. This is the only way into the inner handler.
    """
    boto3.client("logs", region_name="us-east-1").put_resource_policy(
        policyName=VENDEDLOGS_POLICY_NAME, policyDocument="not json at all"
    )

    results = cleanup.cleanup_logs_resource_policies(auto_approve=True)

    assert results["updated"] == []
    assert len(results["errors"]) == 1
    assert results["errors"][0].startswith(
        f"Failed to update {VENDEDLOGS_POLICY_NAME}:"
    )


# ---------------------------------------------------------------------------
# run_cleanup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_cleanup_does_nothing_when_no_stack_has_been_deleted(aws, monkeypatch):
    """With no deleted stacks there is nothing to attribute, so nothing runs.

    The early return is a safety property, not an optimisation: every
    ``cleanup_*`` method would otherwise list resources and consult
    ``get_stack_state`` for each, and a bug in attribution could only do damage
    from there. Each sweep is replaced by a recorder to prove none is reached.
    """
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(StackName=LIVE_STACK, TemplateBody=IDP_TEMPLATE)

    instance = OrphanedResourceCleanup(region="us-east-1")
    called: List[str] = []
    for name in (
        "cleanup_cloudfront_distributions",
        "cleanup_log_groups",
        "cleanup_appsync_apis",
        "cleanup_cloudfront_policies",
        "cleanup_iam_policies",
        "cleanup_logs_resource_policies",
        "cleanup_s3_buckets",
        "cleanup_dynamodb_tables",
    ):
        monkeypatch.setattr(
            instance,
            name,
            lambda *a, _name=name, **k: called.append(_name) or {},
        )

    assert instance.run_cleanup(regions=["us-east-1"]) == {}
    assert called == []


@pytest.mark.unit
def test_run_cleanup_passes_both_flags_to_every_sweep(aws, monkeypatch):
    """Each of the eight keys must map to its own sweep, with the flags intact.

    The sweeps are called positionally as ``(dry_run, auto_approve)``. Swapping
    those two arguments for any one of them turns ``--dry-run`` into a real
    deletion for that resource class, and every individual sweep's own tests
    would still pass — the wiring is only observable here.
    """
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(StackName=DEAD_STACK, TemplateBody=IDP_TEMPLATE)
    cfn.delete_stack(StackName=DEAD_STACK)

    instance = OrphanedResourceCleanup(region="us-east-1")
    seen: Dict[str, Tuple] = {}
    expected_keys = {
        "cloudfront_distributions": "cleanup_cloudfront_distributions",
        "log_groups": "cleanup_log_groups",
        "appsync_apis": "cleanup_appsync_apis",
        "cloudfront_policies": "cleanup_cloudfront_policies",
        "iam_policies": "cleanup_iam_policies",
        "logs_resource_policies": "cleanup_logs_resource_policies",
        "s3_buckets": "cleanup_s3_buckets",
        "dynamodb_tables": "cleanup_dynamodb_tables",
    }

    def recorder(method: str):
        def record(*args, **kwargs):
            seen[method] = args
            return method

        return record

    for method in expected_keys.values():
        monkeypatch.setattr(instance, method, recorder(method))

    results = instance.run_cleanup(
        dry_run=True, auto_approve=False, regions=["us-east-1"]
    )

    assert results == {key: method for key, method in expected_keys.items()}
    assert seen == {method: (True, False) for method in expected_keys.values()}


@pytest.mark.unit
def test_run_cleanup_removes_every_orphan_and_keeps_every_live_resource(aws):
    """One end-to-end sweep over five resource classes at once.

    The per-method tests each seed one class; a real run sweeps all of them
    against one shared discovery result, and this is where an interaction would
    show — for instance a ``yes to all`` recorded for one resource type leaking
    into another, which the per-type keying of ``_yes_to_all`` prevents.

    The CloudFront response-headers-policy sweep is expected to fail here
    (moto answers 404), so its error is asserted as such rather than ignored.
    """
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(StackName=LIVE_STACK, TemplateBody=IDP_TEMPLATE)
    cfn.create_stack(StackName=DEAD_STACK, TemplateBody=IDP_TEMPLATE)
    cfn.delete_stack(StackName=DEAD_STACK)

    seed_log_groups()
    seed_appsync_apis()
    seed_iam_policies()
    seed_buckets()
    seed_tables()

    instance = OrphanedResourceCleanup(region="us-east-1")
    results = instance.run_cleanup(auto_approve=True, regions=["us-east-1"])

    assert surviving_buckets() == {LIVE_INPUT_BUCKET, UNRELATED_BUCKET}
    assert surviving_tables() == {LIVE_TRACKING_TABLE, UNRELATED_TABLE}
    assert surviving_policy_names() == {
        f"{LIVE_STACK}-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
        f"{LIVE_STACK}-PermissionsBoundary",
        f"{DEAD_STACK}-PATTERN2-SomethingElse",
        "other-PATTERN2STACK-LambdaECRAccessPolicy-1AB",
    }
    assert surviving_api_names() == {
        f"{LIVE_STACK}-p2-api",
        "some-other-app-api",
        f"{DEAD_STACK}-graphql",
    }
    assert LIVE_LAMBDA_GROUP in surviving_log_groups()
    assert ORPHAN_LAMBDA_GROUP not in surviving_log_groups()

    assert set(results) == {
        "cloudfront_distributions",
        "log_groups",
        "appsync_apis",
        "cloudfront_policies",
        "iam_policies",
        "logs_resource_policies",
        "s3_buckets",
        "dynamodb_tables",
    }
    for key in ("log_groups", "appsync_apis", "iam_policies", "s3_buckets"):
        assert results[key]["errors"] == [], key
    assert len(results["cloudfront_policies"]["errors"]) == 1


# ---------------------------------------------------------------------------
# Remaining branches: unknown stacks, declined prompts, deletion failures
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_appsync_api_from_an_unrecognised_stack_is_skipped(cleanup):
    boto3.client("appsync", region_name="us-east-1").create_graphql_api(
        name="IDP-stranger-p2-api", authenticationType="API_KEY"
    )

    results = cleanup.cleanup_appsync_apis(auto_approve=True)

    assert surviving_api_names() == {"IDP-stranger-p2-api"}
    assert results["skipped"] == [
        "IDP-stranger-p2-api (stack: IDP-stranger - not verified IDP)"
    ]


@pytest.mark.unit
def test_an_appsync_api_that_cannot_be_deleted_is_reported_as_an_error(cleanup):
    """A failure deleting the API must not go on to delete its log group.

    The log-group cleanup sits inside the success path, so a failed API deletion
    leaves the group in place — which is right, since the API is still using it.
    """
    ids = seed_appsync_apis()
    dead_api_id = ids[f"{DEAD_STACK}-p2-api"]
    logs = boto3.client("logs", region_name="us-east-1")
    logs.create_log_group(logGroupName=f"/aws/appsync/apis/{dead_api_id}")
    real_appsync = cleanup.appsync

    class RefusingAppsync:
        def __getattr__(self, name: str):
            return getattr(real_appsync, name)

        def delete_graphql_api(self, apiId: str):
            raise RuntimeError("ConcurrentModificationException")

    cleanup.appsync = RefusingAppsync()

    results = cleanup.cleanup_appsync_apis(auto_approve=True)

    assert f"{DEAD_STACK}-p2-api" in surviving_api_names()
    assert results["errors"] == [
        f"Failed to delete AppSync API {DEAD_STACK}-p2-api: "
        "ConcurrentModificationException"
    ]
    assert f"/aws/appsync/apis/{dead_api_id}" in surviving_log_groups()


@pytest.mark.unit
def test_declining_the_appsync_prompt_keeps_the_api(cleanup, prompt):
    seed_appsync_apis()
    prompt("n")

    results = cleanup.cleanup_appsync_apis()

    assert f"{DEAD_STACK}-p2-api" in surviving_api_names()
    assert any("user declined" in entry for entry in results["skipped"])


@pytest.mark.unit
def test_an_iam_policy_from_an_unrecognised_stack_is_skipped(cleanup):
    boto3.client("iam").create_policy(
        PolicyName="IDP-stranger-PATTERN2STACK-Policy", PolicyDocument=POLICY_DOCUMENT
    )

    results = cleanup.cleanup_iam_policies(auto_approve=True)

    assert "IDP-stranger-PATTERN2STACK-Policy" in surviving_policy_names()
    assert results["skipped"] == [
        "IDP-stranger-PATTERN2STACK-Policy (stack: IDP-stranger - not verified IDP)"
    ]


@pytest.mark.unit
def test_declining_the_iam_prompt_keeps_the_policy(cleanup, prompt):
    seed_iam_policies()
    prompt("n", "n")

    results = cleanup.cleanup_iam_policies()

    assert f"{DEAD_STACK}-PermissionsBoundary" in surviving_policy_names()
    assert sum("user declined" in entry for entry in results["skipped"]) == 2


@pytest.mark.unit
def test_a_dynamodb_table_from_an_unrecognised_stack_is_not_reported(cleanup):
    """Like S3, the table sweep ``continue``s on ``UNKNOWN`` without reporting.

    Asserted for the same reason: the silence is the current behaviour and an
    operator cannot tell the table was considered at all.
    """
    create_table("IDP-stranger-TrackingTable-1AB")

    results = cleanup.cleanup_dynamodb_tables(auto_approve=True)

    assert "IDP-stranger-TrackingTable-1AB" in surviving_tables()
    assert results == {"deleted": [], "skipped": [], "errors": []}


@pytest.mark.unit
def test_a_distribution_whose_comment_names_no_stack_is_ignored(cleanup):
    """A comment that is exactly the prefix yields no stack name, and is skipped.

    The guard matters because an empty stack name would otherwise reach
    ``get_stack_state``, and an empty name matching no stack is safe only by
    accident. The listing is scripted rather than moto-backed because moto
    normalises away the trailing space that makes the comment exactly the prefix,
    and without that space the earlier ``startswith`` guard catches it instead —
    so a moto-backed version of this test would pass while exercising a different
    line.
    """

    class ListingOnly:
        def list_distributions(self):
            return {
                "DistributionList": {
                    "Items": [
                        {
                            "Id": "ENAMELESS",
                            "Comment": "Web app cloudfront distribution ",
                            "Enabled": True,
                            "Status": "Deployed",
                        }
                    ]
                }
            }

    cleanup.cloudfront = ListingOnly()

    results = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert results == {"deleted": [], "disabled": [], "skipped": [], "errors": []}


def disable_distribution(distribution_id: str) -> None:
    """Put a distribution into the disabled-and-deployed state out of band."""
    cf = boto3.client("cloudfront", region_name="us-east-1")
    current = cf.get_distribution(Id=distribution_id)
    config = current["Distribution"]["DistributionConfig"]
    config["Enabled"] = False
    cf.update_distribution(
        Id=distribution_id, DistributionConfig=config, IfMatch=current["ETag"]
    )


@pytest.mark.unit
def test_a_dry_run_on_an_already_disabled_distribution_reports_a_deletion(
    cleanup, prompt
):
    """Once disabled, the dry run reports the *deletion* rather than a disable.

    The two dry-run branches report into different keys — ``disabled`` and
    ``deleted`` — and which one an operator sees is how they know whether the
    next real run will remove the distribution or only turn it off.
    """
    prompt()
    ids = seed_distributions()
    disable_distribution(ids["dead"])

    results = cleanup.cleanup_cloudfront_distributions(dry_run=True)

    assert results["deleted"] == [f"{ids['dead']} (stack: {DEAD_STACK}) [DRY RUN]"]
    assert results["disabled"] == []


@pytest.mark.unit
def test_a_distribution_deletion_failure_is_reported_as_an_error(cleanup):
    ids = seed_distributions()
    disable_distribution(ids["dead"])
    real_cf = cleanup.cloudfront

    class RefusingCloudFront:
        def __getattr__(self, name: str):
            return getattr(real_cf, name)

        def delete_distribution(self, Id: str, IfMatch: str):
            raise RuntimeError("DistributionNotDisabled")

    cleanup.cloudfront = RefusingCloudFront()

    results = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert results["deleted"] == []
    assert results["errors"] == [
        f"Failed to delete {ids['dead']}: DistributionNotDisabled"
    ]


@pytest.mark.unit
def test_a_distribution_disable_failure_is_reported_as_an_error(cleanup):
    """A failed disable must not be reported under ``disabled``.

    Deletion is a two-run process, and the second run keys on the distribution
    actually being disabled. Reporting a failed disable as done sends the
    operator back in 20 minutes to a distribution that never changed.
    """
    ids = seed_distributions()
    real_cf = cleanup.cloudfront

    class RefusingCloudFront:
        def __getattr__(self, name: str):
            return getattr(real_cf, name)

        def update_distribution(self, Id: str, DistributionConfig, IfMatch: str):
            raise RuntimeError("PreconditionFailed")

    cleanup.cloudfront = RefusingCloudFront()

    results = cleanup.cleanup_cloudfront_distributions(auto_approve=True)

    assert results["disabled"] == []
    assert results["errors"] == [f"Failed to disable {ids['dead']}: PreconditionFailed"]
    cf = boto3.client("cloudfront", region_name="us-east-1")
    assert (
        cf.get_distribution(Id=ids["dead"])["Distribution"]["DistributionConfig"][
            "Enabled"
        ]
        is True
    )


@pytest.mark.unit
def test_declining_the_delete_prompt_for_a_disabled_distribution_keeps_it(
    cleanup, prompt
):
    ids = seed_distributions()
    disable_distribution(ids["dead"])
    prompt("n")

    results = cleanup.cleanup_cloudfront_distributions()

    assert f"{ids['dead']} (stack: {DEAD_STACK} - user declined)" in results["skipped"]
    assert results["deleted"] == [] and results["errors"] == []
    assert ids["dead"] in {
        item["Id"]
        for item in boto3.client(
            "cloudfront", region_name="us-east-1"
        ).list_distributions()["DistributionList"]["Items"]
    }


@pytest.mark.unit
def test_declining_the_response_headers_policy_prompt_keeps_it(cleanup, prompt):
    fake = FakeResponseHeadersPolicies([f"{DEAD_STACK}-security-headers-policy"])
    cleanup.cloudfront = fake
    prompt("n")

    results = cleanup.cleanup_cloudfront_policies()

    assert fake.deleted == []
    assert results["skipped"] == [
        f"{DEAD_STACK}-security-headers-policy (stack: {DEAD_STACK} - user declined)"
    ]


@pytest.mark.unit
def test_a_vendedlogs_group_with_no_pattern_segment_uses_the_whole_suffix(
    cleanup, no_confirm
):
    """The ``for``/``else`` falls back to the whole log-group suffix as the name.

    A Step Functions log group for a top-level stack has no ``PATTERN`` segment,
    so the loop finds nothing and the ``else`` takes the entire suffix — here
    ``IDP-dead-StateMachine``, which is not a stack name and resolves to
    ``UNKNOWN``. The statement is kept, which is the safe outcome; it is also
    kept when the name *does* resolve to a deleted stack, for the separate reason
    documented in ``test_an_orphaned_vendedlogs_statement_is_not_actually_removed``.
    """
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "nopattern",
                "Effect": "Allow",
                "Principal": {"Service": "delivery.logs.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": (
                    "arn:aws:logs:us-east-1:123456789012:log-group:"
                    f"/aws/vendedlogs/states/{DEAD_STACK}-StateMachine:*"
                ),
            }
        ],
    }
    boto3.client("logs", region_name="us-east-1").put_resource_policy(
        policyName=VENDEDLOGS_POLICY_NAME, policyDocument=json.dumps(document)
    )

    results = cleanup.cleanup_logs_resource_policies(auto_approve=True)

    assert results["updated"] == []
    assert results["errors"] == []
    assert stored_resource_policy() == document
