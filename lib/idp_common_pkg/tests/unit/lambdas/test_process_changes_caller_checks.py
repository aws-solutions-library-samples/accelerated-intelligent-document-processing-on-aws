# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""processChanges applies the checks saving review edits applies, for non-Admins.

A caller who is not an Admin may process changes on a document only while they own
its open review, not once the review has finished, not while the document is in
flight, and only inside their configuration scope. Admin is unaffected.

Drives the real resolver ``handler`` against moto: a tracking table, a users table
with the ``EmailIndex`` the scope lookup queries, the output and working buckets and
the document queue. Every refused case asserts the state is untouched — the
section's extraction object is still in S3, the review status is unchanged and no
message was queued — because a refusal raised after the writes would pass a test
that only checks the exception. Every allowed case asserts the opposite, so a check
that refused everything would fail here too.

Engine: moto, not DynamoDB. Nothing here depends on expression-language semantics
beyond a ``ProjectionExpression`` and a GSI equality query.
"""

import importlib.util
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")
TRACKING = "pc-caller-checks-tracking"
USERS = "pc-caller-checks-users"
OUTPUT_BUCKET = "pc-output-bucket"
WORKING_BUCKET = "pc-working-bucket"
OBJECT_KEY = "packet.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}
RESULT_KEY = f"{OBJECT_KEY}/sections/1/result.json"

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
    "TRACKING_TABLE": TRACKING,
    "USERS_TABLE_NAME": USERS,
    "INPUT_BUCKET": "pc-input-bucket",
    "OUTPUT_BUCKET": OUTPUT_BUCKET,
    "WORKING_BUCKET": WORKING_BUCKET,
}

_PK_SK = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]


class _Env:
    def __init__(self, mod, queue_url):
        self.mod = mod
        self.queue_url = queue_url
        self.tracking = boto3.resource("dynamodb").Table(TRACKING)
        self.users = boto3.resource("dynamodb").Table(USERS)

    def row(self):
        return self.tracking.get_item(Key=DOC_KEY)["Item"]

    def result_exists(self):
        listed = boto3.client("s3").list_objects_v2(
            Bucket=OUTPUT_BUCKET, Prefix=RESULT_KEY
        )
        return listed.get("KeyCount", 0) == 1

    def queued(self):
        attrs = boto3.client("sqs").get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        return int(attrs["Attributes"]["ApproximateNumberOfMessages"])

    def scope(self, email, versions):
        self.users.put_item(
            Item={
                "PK": f"USER#{email}",
                "SK": f"USER#{email}",
                "email": email,
                "allowedConfigVersions": versions,
            }
        )


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV):
        os.environ.pop("HITL_TABLE_NAME", None)
        with mock_aws():
            ddb = boto3.resource("dynamodb")
            ddb.create_table(
                TableName=TRACKING,
                KeySchema=_PK_SK,
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            ddb.create_table(
                TableName=USERS,
                KeySchema=_PK_SK,
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
            s3 = boto3.client("s3")
            for bucket in (OUTPUT_BUCKET, WORKING_BUCKET):
                s3.create_bucket(Bucket=bucket)
            s3.put_object(Bucket=OUTPUT_BUCKET, Key=RESULT_KEY, Body=b'{"a": 1}')
            queue_url = boto3.client("sqs").create_queue(QueueName="pc-doc-queue")[
                "QueueUrl"
            ]
            os.environ["QUEUE_URL"] = queue_url
            spec = importlib.util.spec_from_file_location(
                "process_changes_caller_checks_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/process_changes_resolver/index.py",
                ),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            yield _Env(mod, queue_url)


def _seed(env, **attributes):
    """A processed one-section document; HITL fields come from ``attributes``."""
    item = {
        **DOC_KEY,
        "ObjectKey": OBJECT_KEY,
        "ObjectStatus": "COMPLETED",
        "ConfigVersion": "tenant-a",
        "Sections": [
            {
                "Id": "1",
                "PageIds": [1],
                "Class": "invoice",
                "OutputJSONUri": f"s3://{OUTPUT_BUCKET}/{RESULT_KEY}",
            }
        ],
        "Pages": [{"Id": 1, "Class": "invoice"}],
        **attributes,
    }
    env.tracking.put_item(Item={k: v for k, v in item.items() if v is not None})


def _owned(**overrides):
    """A legacy review ``rev`` has claimed (claimReview's shape)."""
    return {
        "HITLTriggered": True,
        "HITLStatus": "InProgress",
        "HITLPendingReview": "true",
        "HITLReviewOwner": "rev",
        "HITLReviewOwnerEmail": "rev@example.com",
        **overrides,
    }


def _call(env, groups=("Reviewer",), username="rev", email="rev@example.com", sub=None):
    """A reclassification of section 1, as the UI sends it.

    ``groups`` may be a string: a single-group claim can arrive unlisted.
    """
    claims = {
        "cognito:username": username,
        "email": email,
        "cognito:groups": groups if isinstance(groups, str) else list(groups),
    }
    if sub:
        claims["sub"] = sub
    return env.mod.handler(
        {
            "info": {"fieldName": "processChanges"},
            "identity": {
                "username": username,
                "claims": claims,
            },
            "arguments": {
                "objectKey": OBJECT_KEY,
                "modifiedSections": [
                    {"sectionId": "1", "classification": "receipt", "pageIds": ["1"]}
                ],
                "modifiedPages": [],
            },
        },
        None,
    )


def _assert_untouched(env, hitl_status):
    row = env.row()
    assert row.get("HITLStatus") == hitl_status
    assert row["ObjectStatus"] != "QUEUED"
    assert env.result_exists(), "the section's extraction object was deleted"
    assert env.queued() == 0, "the document was requeued"


def _assert_processed(env, result):
    assert result["success"] is True, result
    assert env.row()["ObjectStatus"] == "QUEUED"
    assert not env.result_exists()
    assert env.queued() == 1


# --------------------------------------------------------------------- refused


@pytest.mark.unit
class TestOwnership:
    @pytest.mark.parametrize(
        "hitl",
        [
            pytest.param(_owned(HITLReviewOwner="alice"), id="owned-by-another"),
            pytest.param(
                {"HITLTriggered": True, "HITLStatus": "PendingReview"},
                id="pending-unclaimed",
            ),
            pytest.param(
                {"HITLTriggered": True, "HITLStatus": "Review Pending"},
                id="review-pending-unclaimed",
            ),
            pytest.param({}, id="no-review"),
        ],
    )
    def test_a_reviewer_who_does_not_own_the_review_is_refused(self, env, hitl):
        _seed(env, **hitl)
        with pytest.raises(PermissionError, match="review owner") as raised:
            _call(env)
        # Attributed to the object, not to the group floor.
        assert type(raised.value).__name__ == "ResourceAccessDenied"
        _assert_untouched(env, hitl.get("HITLStatus"))

    @pytest.mark.parametrize(
        "groups",
        [("Reviewer", "Author"), ("Reviewer", "Annotator"), ("Reviewer", "Viewer")],
    )
    def test_a_second_non_admin_group_grants_no_exemption(self, env, groups):
        # Only Admin is exempt. An Author or Annotator role beside Reviewer is still
        # a caller who must hold the open review.
        _seed(env, **_owned(HITLReviewOwner="alice"))
        with pytest.raises(PermissionError, match="review owner"):
            _call(env, groups=groups)
        _assert_untouched(env, "InProgress")

    def test_an_owner_matched_only_by_email_is_refused(self, env):
        # The sibling compares the owner to the username; an email that matches is
        # not ownership there either.
        _seed(env, **_owned(HITLReviewOwner="alice"))
        with pytest.raises(PermissionError, match="review owner"):
            _call(env, username="bob", email="rev@example.com")
        _assert_untouched(env, "InProgress")

    def test_an_owner_recorded_on_a_document_with_no_open_review_is_refused(self, env):
        # Ownership is of an OPEN review: a stale owner on a row with no review
        # status grants nothing.
        _seed(env, **_owned(HITLStatus=None))
        with pytest.raises(PermissionError, match="review owner"):
            _call(env)
        _assert_untouched(env, None)

    def test_an_unreadable_review_owner_refuses(self, env):
        _seed(env, **_owned())

        class _Unreadable:
            def get_item(self, **_):
                raise RuntimeError("throttled")

        with patch.object(
            env.mod, "_tracking_table_resource", return_value=_Unreadable()
        ):
            with pytest.raises(PermissionError, match="could not be verified"):
                _call(env)
        _assert_untouched(env, "InProgress")

    def test_a_reviewer_group_claim_given_as_a_string_is_not_an_admin(self, env):
        _seed(env, **_owned(HITLReviewOwner="alice"))
        with pytest.raises(PermissionError, match="review owner"):
            _call(env, groups="Reviewer")
        _assert_untouched(env, "InProgress")

    def test_a_caller_with_no_username_is_refused_even_on_an_unowned_row(self, env):
        _seed(env, **_owned(HITLReviewOwner=None))
        with pytest.raises(PermissionError, match="review owner"):
            _call(env, username="")
        _assert_untouched(env, "InProgress")


@pytest.mark.unit
class TestFinishedReview:
    @pytest.mark.parametrize(
        "status",
        [
            "Review Completed",
            "Completed",
            "Review Skipped",
            "Skipped",
            "COMPLETED",
            "complete",
            "rejected",
        ],
    )
    def test_the_owner_of_a_finished_review_is_refused(self, env, status):
        _seed(env, **_owned(HITLStatus=status))
        with pytest.raises(ValueError, match="already finished"):
            _call(env)
        _assert_untouched(env, status)


@pytest.mark.unit
class TestDocumentInFlight:
    @pytest.mark.parametrize(
        "status", ["RUNNING", "QUEUED", "EXTRACTING", "CLASSIFYING", "OCR"]
    )
    def test_the_owner_is_refused_while_the_document_is_processing(self, env, status):
        _seed(env, **_owned(), ObjectStatus=status)
        with pytest.raises(ValueError, match="Wait for processing to finish"):
            _call(env)
        row = env.row()
        assert row["ObjectStatus"] == status
        assert row["HITLStatus"] == "InProgress"
        assert env.result_exists()
        assert env.queued() == 0


@pytest.mark.unit
class TestConfigScope:
    def test_a_scoped_owner_is_refused_a_document_outside_their_scope(self, env):
        env.scope("rev@example.com", ["tenant-b"])
        _seed(env, **_owned())
        with pytest.raises(PermissionError, match="configuration scope") as raised:
            _call(env)
        assert type(raised.value).__name__ == "ResourceAccessDenied"
        _assert_untouched(env, "InProgress")

    def test_a_scope_reached_through_the_sub_pointer_is_applied(self, env):
        # The row is found by the immutable Cognito sub, not by the email: the
        # caller's address matches no row, so only the sub join finds the scope.
        env.users.put_item(
            Item={"PK": "SUB#sub-rev", "SK": "SUB#sub-rev", "userId": "u-rev"}
        )
        env.users.put_item(
            Item={
                "PK": "USER#u-rev",
                "SK": "USER#u-rev",
                "email": "old-address@example.com",
                "allowedConfigVersions": ["tenant-b"],
            }
        )
        _seed(env, **_owned())
        with pytest.raises(PermissionError, match="configuration scope"):
            _call(env, sub="sub-rev")
        _assert_untouched(env, "InProgress")

    def test_a_scoped_owner_is_refused_a_document_naming_no_profile(self, env):
        env.scope("rev@example.com", ["tenant-a"])
        _seed(env, **_owned(), ConfigVersion=None)
        with pytest.raises(PermissionError, match="configuration scope"):
            _call(env)
        _assert_untouched(env, "InProgress")

    def test_an_unresolvable_scope_refuses(self, env):
        os.environ["USERS_TABLE_NAME"] = ""
        _seed(env, **_owned())
        with pytest.raises(PermissionError, match="could not be verified"):
            _call(env)
        _assert_untouched(env, "InProgress")


# --------------------------------------------------------------------- allowed


@pytest.mark.unit
class TestStillAllowed:
    @pytest.mark.parametrize(
        "status", ["InProgress", "ReviewInProgress", "PendingReview"]
    )
    def test_the_owner_of_an_open_review_processes_changes(self, env, status):
        _seed(env, **_owned(HITLStatus=status))
        _assert_processed(env, _call(env))
        row = env.row()
        assert row["HITLStatus"] == "Review Completed"
        assert row["HITLReviewedBy"] == "rev"

    @pytest.mark.parametrize("scope", [["tenant-a"], ["tenant-*"], ["x", "tenant-a"]])
    def test_a_scoped_owner_processes_a_document_inside_their_scope(self, env, scope):
        env.scope("rev@example.com", scope)
        _seed(env, **_owned())
        _assert_processed(env, _call(env))

    @pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "ABORTED"])
    def test_the_owner_processes_a_document_in_any_editable_status(self, env, status):
        _seed(env, **_owned(), ObjectStatus=status)
        _assert_processed(env, _call(env))

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(_owned(HITLReviewOwner="alice"), id="owned-by-another"),
            pytest.param({"HITLStatus": "PendingReview"}, id="unclaimed"),
            pytest.param({}, id="no-review"),
            pytest.param(_owned(HITLStatus="Review Completed"), id="finished"),
            pytest.param(_owned(HITLStatus="rejected"), id="queue-rejected"),
            pytest.param({**_owned(), "ObjectStatus": "RUNNING"}, id="in-flight"),
        ],
    )
    def test_an_admin_is_unaffected(self, env, case):
        # Admin is never scope-looked-up: an Admin with a restrictive row and no
        # reachable users table processes changes as before.
        env.scope("admin@example.com", ["tenant-b"])
        os.environ["USERS_TABLE_NAME"] = ""
        _seed(env, **case)
        result = _call(
            env, groups=("Admin",), username="admin", email="admin@example.com"
        )
        assert result["success"] is True, result
        assert not env.result_exists()
        assert env.queued() == 1

    def test_an_admin_group_claim_given_as_a_string_is_an_admin(self, env):
        _seed(env, **_owned(HITLReviewOwner="alice"))
        _assert_processed(env, _call(env, groups="Admin", username="admin"))

    def test_reviewer_and_admin_together_is_an_admin(self, env):
        _seed(env, **_owned(HITLReviewOwner="alice"))
        _assert_processed(
            env,
            _call(env, groups=("Reviewer", "Admin"), username="admin", email="a@x.com"),
        )
