# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the tracking item holds after Process Changes completes an open review.

Drives the real resolver and the real ``DocumentDynamoDBService`` against a moto
table and reads the item back, because the sibling suites stub the document service
and so assert the document object rather than what is stored.

⚠️ On the success path the handler's trailing whole-document status write also
carries both section lists, so most single-statement mutations of the completion
write are repaired by it and only the failed-status-write test below can see them.
That test is the one that pins the completion write; the parametrised one pins the
end state a reviewer sees, including that an overlap is not listed twice.
"""

import importlib.util
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")
TABLE = "process-changes-hitl-lists"
USERS = "process-changes-hitl-lists-users"
OBJECT_KEY = "packet.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
    "TRACKING_TABLE": TABLE,
    # A reviewer's configuration scope is resolved from the users table, and an
    # unresolvable scope refuses; an empty table means an unrestricted caller.
    "USERS_TABLE_NAME": USERS,
    "INPUT_BUCKET": "input-bucket",
    "OUTPUT_BUCKET": "output-bucket",
}


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV):
        for name in ("QUEUE_URL", "WORKING_BUCKET"):
            os.environ.pop(name, None)
        with mock_aws():
            boto3.resource("dynamodb").create_table(
                TableName=TABLE,
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
            boto3.resource("dynamodb").create_table(
                TableName=USERS,
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                    {"AttributeName": "email", "AttributeType": "S"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "EmailIndex",
                        "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            spec = importlib.util.spec_from_file_location(
                "process_changes_hitl_lists_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/process_changes_resolver/index.py",
                ),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            yield mod, boto3.resource("dynamodb").Table(TABLE)


def _seed(table, **attributes):
    """A document a Reviewer has claimed and not yet finished (claimReview's shape)."""
    table.put_item(
        Item={
            **DOC_KEY,
            "ObjectKey": OBJECT_KEY,
            "ObjectStatus": "COMPLETED",
            "HITLTriggered": True,
            "HITLStatus": "InProgress",
            "HITLPendingReview": "true",
            "HITLReviewOwner": "rev",
            "HITLReviewOwnerEmail": "rev@example.com",
            "Sections": [
                {"Id": "1", "PageIds": [1], "Class": "invoice", "OutputJSONUri": ""},
                {"Id": "2", "PageIds": [2], "Class": "invoice", "OutputJSONUri": ""},
            ],
            "Pages": [
                {"Id": 1, "Class": "invoice"},
                {"Id": 2, "Class": "invoice"},
            ],
            **attributes,
        }
    )


def _process_changes(mod, groups=("Reviewer",)):
    """Process Changes as ``rev``, the owner ``_seed`` records.

    A finished review is closed to a Reviewer, as saving review edits is, so the
    finished-review cases below call as an Admin: what they pin is what the resolver
    writes to a finished review, and only an Admin still reaches that path.
    """
    return mod.handler(
        {
            "info": {"fieldName": "processChanges"},
            "identity": {
                "username": "rev",
                "claims": {
                    "email": "rev@example.com",
                    "cognito:groups": list(groups),
                },
            },
            "arguments": {"objectKey": OBJECT_KEY, "modifiedSections": []},
        },
        None,
    )


def _stored(table):
    return table.get_item(Key=DOC_KEY)["Item"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "pending, completed, expected_completed",
    [
        (["1"], None, ["1"]),
        (["2"], ["1"], ["1", "2"]),
        # Already-completed sections keep their place; newly completed ones follow.
        (["1"], ["2"], ["2", "1"]),
        # A section in both lists is not listed twice. No current writer stores
        # one; this is defensive against an item left inconsistent earlier.
        (["1", "2"], ["1"], ["1", "2"]),
    ],
)
def test_completing_an_open_review_clears_the_stored_pending_list(
    env, pending, completed, expected_completed
):
    mod, table = env
    _seed(
        table,
        HITLSectionsPending=pending,
        **({"HITLSectionsCompleted": completed} if completed else {}),
    )

    assert _process_changes(mod)["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == "Review Completed"
    assert item["HITLCompleted"] is True
    assert item["HITLReviewedBy"] == "rev"
    assert item["HITLSectionsPending"] == []
    assert item["HITLSectionsCompleted"] == expected_completed
    assert "HITLPendingReview" not in item


@pytest.mark.unit
def test_the_lists_are_written_by_the_completion_write_itself(env):
    """The trailing whole-document status write is best-effort and swallows its own
    failure, so the lists must not depend on it to be cleared."""
    mod, table = env
    # A section completed earlier, so a completion write that keeps only the
    # pending sections is told apart from one that appends them.
    _seed(table, HITLSectionsPending=["1"], HITLSectionsCompleted=["2"])

    real = mod.create_document_service
    calls = {"n": 0}

    def service_whose_status_write_fails(*args, **kwargs):
        service = real(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] > 1:

            def fail(document):
                raise RuntimeError("status write failed")

            service.update_document = fail
        return service

    with patch.object(
        mod, "create_document_service", side_effect=service_whose_status_write_fails
    ):
        assert _process_changes(mod)["success"] is True

    assert calls["n"] == 2  # the load, then the status write that failed
    item = _stored(table)
    assert item["HITLStatus"] == "Review Completed"
    assert item["HITLCompleted"] is True
    assert item["HITLReviewedBy"] == "rev"
    assert item["HITLSectionsPending"] == []
    assert item["HITLSectionsCompleted"] == ["2", "1"]
    assert "HITLPendingReview" not in item


def _seed_finished_queue_review(table, status, **attributes):
    """A queue-routed document whose review cycle has ended.

    The shape the review-queue tracker leaves: ``apply_overrides`` sets
    ``HITLStatus`` to the ``ReviewStatus`` value for the outcome, and
    ``after_completion`` writes ``HITLReviewedBy`` from the item's ``disposed_by``
    (the disposing caller's email) together with the cycle attributes, and
    ``ReviewRejectReason`` on a reject. It writes no ``HITLReviewedByEmail``.
    """
    table.put_item(
        Item={
            **DOC_KEY,
            "ObjectKey": OBJECT_KEY,
            "ObjectStatus": "COMPLETED",
            "HITLTriggered": True,
            "HITLStatus": status,
            "HITLReviewedBy": "annotator@example.com",
            "ReviewQueue": "default",
            "ReviewCycleId": "cycle-1",
            "ReviewDisposedAt": "2026-10-01T00:00:00+00:00",
            "ReviewStatusUri": f"s3://output-bucket/{OBJECT_KEY}/review/status.json",
            "Sections": [
                {"Id": "1", "PageIds": [1], "Class": "invoice", "OutputJSONUri": ""},
            ],
            "Pages": [{"Id": 1, "Class": "invoice"}],
            **attributes,
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "status, extra",
    [
        ("complete", {}),
        ("rejected", {"ReviewRejectReason": "Wrong document type"}),
    ],
)
def test_a_finished_queue_review_keeps_its_status_and_reviewer(env, status, extra):
    """Process Changes is allowed on a finished queue document, and it must not
    re-finish the review: the terminal outcome and who reached it stay as the
    review queue recorded them (#1568)."""
    from idp_common.review_queues.statuses import ReviewStatus

    assert status in {s.value for s in ReviewStatus}  # the writer's own values
    mod, table = env
    _seed_finished_queue_review(table, status, **extra)

    assert _process_changes(mod, groups=("Admin",))["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == status
    assert item["HITLReviewedBy"] == "annotator@example.com"
    assert "HITLReviewedByEmail" not in item
    assert "HITLCompleted" not in item
    for name, value in extra.items():
        assert item[name] == value


@pytest.mark.unit
def test_a_finished_queue_review_keeps_its_status_when_the_status_write_fails(env):
    """The completion write is the only writer once the trailing status write
    fails, so this pins that the resolver itself does not rewrite the status."""
    mod, table = env
    _seed_finished_queue_review(table, "rejected")

    real = mod.create_document_service
    calls = {"n": 0}

    def service_whose_status_write_fails(*args, **kwargs):
        service = real(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] > 1:

            def fail(document):
                raise RuntimeError("status write failed")

            service.update_document = fail
        return service

    with patch.object(
        mod, "create_document_service", side_effect=service_whose_status_write_fails
    ):
        assert _process_changes(mod, groups=("Admin",))["success"] is True

    assert calls["n"] == 2
    item = _stored(table)
    assert item["HITLStatus"] == "rejected"
    assert item["HITLReviewedBy"] == "annotator@example.com"
    assert "HITLCompleted" not in item


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [
        "Completed",
        "Review Completed",
        "Skipped",
        "Review Skipped",
        # The earlier A2I flow's spelling, which the exact-match normaliser does
        # not recognise; rows written by it may still be retained.
        "COMPLETED",
    ],
)
def test_a_finished_legacy_review_keeps_its_status_and_reviewer(env, status):
    """The legacy finished spellings, which were already handled, stay handled."""
    mod, table = env
    _seed(
        table,
        HITLStatus=status,
        HITLCompleted=True,
        HITLReviewedBy="earlier-reviewer",
        HITLReviewedByEmail="earlier@example.com",
    )
    # A finished legacy review carries no pending marker (completeSectionReview
    # and skipAllReviews both remove it).
    table.update_item(Key=DOC_KEY, UpdateExpression="REMOVE HITLPendingReview")

    assert _process_changes(mod, groups=("Admin",))["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == status
    assert item["HITLReviewedBy"] == "earlier-reviewer"
    assert item["HITLReviewedByEmail"] == "earlier@example.com"


@pytest.mark.unit
@pytest.mark.parametrize(
    "status, writer",
    [
        ("PendingReview", "the pipeline's result processing, on a HITL trigger"),
        ("Review Pending", "releaseReview"),
        ("InProgress", "claimReview and a partial completeSectionReview"),
    ],
)
def test_an_open_legacy_review_is_completed_by_process_changes(env, status, writer):
    """Every spelling a legacy writer stores for a review still owed is finished by
    Process Changes, and the caller is recorded as the reviewer."""
    mod, table = env
    _seed(table, HITLStatus=status, HITLSectionsPending=["1"])

    assert _process_changes(mod)["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == "Review Completed", writer
    assert item["HITLReviewedBy"] == "rev"
    assert item["HITLReviewedByEmail"] == "rev@example.com"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["Completed", "COMPLETED"])
def test_a_finished_legacy_review_with_no_reviewer_records_the_caller(env, status):
    """A legacy review finished without a recorded reviewer gets this caller, name
    and email together. ``COMPLETED`` is the earlier A2I flow's row, which recorded
    no reviewer."""
    mod, table = env
    _seed(table, HITLStatus=status, HITLCompleted=True)
    table.update_item(
        Key=DOC_KEY,
        UpdateExpression="REMOVE HITLPendingReview, HITLReviewOwner, "
        "HITLReviewOwnerEmail",
    )

    assert _process_changes(mod, groups=("Admin",))["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == status
    assert item["HITLReviewedBy"] == "rev"
    assert item["HITLReviewedByEmail"] == "rev@example.com"


@pytest.mark.unit
def test_a_failed_reviewer_fill_is_not_swallowed(env):
    """Only the condition failure means "a reviewer is already recorded". Any other
    error from the fill write fails the request, as it did before the fill was
    conditional."""
    from botocore.exceptions import ClientError

    mod, table = env
    _seed(table, HITLStatus="Completed", HITLCompleted=True)
    table.update_item(Key=DOC_KEY, UpdateExpression="REMOVE HITLPendingReview")

    throttled = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
    )
    real_resource = mod.boto3.resource

    def resource(*args, **kwargs):
        res = real_resource(*args, **kwargs)
        real_table = res.Table

        def table_with_failing_writes(name):
            t = real_table(name)

            def update_item(**_):
                raise throttled

            t.update_item = update_item
            return t

        res.Table = table_with_failing_writes
        return res

    with patch.object(mod.boto3, "resource", side_effect=resource):
        result = _process_changes(mod, groups=("Admin",))

    assert result["success"] is False
    assert "ProvisionedThroughputExceeded" in result["message"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "recorded",
    [
        # A queue item disposed with no disposed_by: the tracker writes "".
        {"HITLReviewedBy": ""},
        # Only the email half recorded. No current writer leaves this on a
        # finished review; the email is kept rather than replaced.
        {"HITLReviewedByEmail": "earlier@example.com"},
    ],
)
def test_a_partly_recorded_reviewer_is_not_replaced(env, recorded):
    mod, table = env
    _seed_finished_queue_review(table, "complete")
    table.update_item(Key=DOC_KEY, UpdateExpression="REMOVE HITLReviewedBy")
    for name, value in recorded.items():
        table.update_item(
            Key=DOC_KEY,
            UpdateExpression="SET #a = :v",
            ExpressionAttributeNames={"#a": name},
            ExpressionAttributeValues={":v": value},
        )

    assert _process_changes(mod, groups=("Admin",))["success"] is True

    item = _stored(table)
    assert item["HITLStatus"] == "complete"
    assert {
        k: item[k] for k in ("HITLReviewedBy", "HITLReviewedByEmail") if k in item
    } == recorded
