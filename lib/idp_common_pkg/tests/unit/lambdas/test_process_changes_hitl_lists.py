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
OBJECT_KEY = "packet.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
    "TRACKING_TABLE": TABLE,
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


def _process_changes(mod):
    return mod.handler(
        {
            "info": {"fieldName": "processChanges"},
            "identity": {
                "username": "rev",
                "claims": {
                    "email": "rev@example.com",
                    "cognito:groups": ["Reviewer"],
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
