# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Reviewer-only caller's document list leaves out reviews that have finished.

Both document-list resolvers restrict a Reviewer to HITL-pending documents plus
finished reviews they own. They decided "finished" from ``HITLCompleted`` alone,
which no review-queue writer sets: a queue review records its outcome only in
``HITLStatus`` (``complete`` or ``rejected``). So a finished queue review on a row
still carrying the legacy ``HITLTriggered`` flag was returned to every Reviewer as
owed work, and counted in their header figure.

The rows are written by the real ``DocumentDynamoDBService`` (the status the
review-queue tracker sets, and ``update_review_attributes`` for the cycle's end)
over a row carrying the AppSync-era ``HITLTriggered`` flag. ``listDocuments`` and
``getDocumentCount`` run the real handler's query against moto's ``TypeDateIndex``
with the production projection, so the FilterExpression is evaluated by moto's
expression engine, not by DynamoDB's. The date-range resolver's per-row filter
reads the same stored rows.
"""

import importlib.util
import os
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "../../../../../nested/api-resolvers/src/lambda"
    )
)
TABLE = "reviewer-list-finished"
# Handler-local modules that other handler directories also ship under these names.
_SIBLINGS = ("config_scope", "review_statuses", "log_sanitizer")
# TypeDateIndex's NonKeyAttributes in template.yaml: a filter can test only these.
_PROJECTED = [
    "ObjectKey",
    "ObjectStatus",
    "ConfigVersion",
    "HITLTriggered",
    "HITLCompleted",
    "HITLStatus",
    "HITLReviewOwner",
    "HITLReviewedBy",
]

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
    "TRACKING_TABLE_NAME": TABLE,
}

REVIEWER = {"username": "rev", "email": "rev@example.com"}

#: What each stored status means to a Reviewer's list, written out rather than
#: derived so the test does not share the code under test's reading. ``COMPLETED``
#: is the earlier A2I flow's finished spelling; ``IN_PROGRESS`` and ``TRIGGERED``
#: its open ones; ``None`` is a triggered row with no status yet.
FINISHED = {
    "complete",
    "rejected",
    "Completed",
    "Review Completed",
    "Skipped",
    "Review Skipped",
    "COMPLETED",
}
OPEN = {
    "in_review",
    "PendingReview",
    "Review Pending",
    "InProgress",
    "ReviewInProgress",
    "IN_PROGRESS",
    "TRIGGERED",
    None,
}


def _declared_statuses():
    from idp_common.review_queues.statuses import LegacyHitlStatus, ReviewStatus

    values = {s.value for s in ReviewStatus} | {s.value for s in LegacyHitlStatus}
    assert values  # an empty derivation would collect as a skip, not a failure
    return values


def test_every_declared_status_is_classified():
    """A status added to either vocabulary fails here until it is placed."""
    declared = _declared_statuses()
    assert declared <= FINISHED | OPEN
    assert not FINISHED & OPEN


def _load(resolver):
    """Import a resolver by path, then put back the sibling modules and path."""
    directory = os.path.join(_LAMBDA_DIR, resolver)
    previous = {name: sys.modules.pop(name, None) for name in _SIBLINGS}
    path_before = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"{resolver}_finished_reviews", os.path.join(directory, "index.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = path_before
        for name, mod in previous.items():
            sys.modules.pop(name, None)
            if mod is not None:
                sys.modules[name] = mod
    return module


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV), mock_aws():
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"}
                for name in ("PK", "SK", "ItemType", "InitialEventTime")
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "TypeDateIndex",
                    "KeySchema": [
                        {"AttributeName": "ItemType", "KeyType": "HASH"},
                        {"AttributeName": "InitialEventTime", "KeyType": "RANGE"},
                    ],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": _PROJECTED,
                    },
                }
            ],
        )
        gsi = _load("list_documents_gsi_resolver")
        gsi.dynamodb = dynamodb
        rng = _load("list_documents_range_resolver")
        yield table, gsi, rng


def _seed(table, key, status, **attributes):
    """A row from before the review queues: the AppSync-era writer set
    HITLTriggered. The status is then written by the live writer, as the pipeline
    and the review-queue tracker write it; a finished queue cycle also gets
    after_completion's attributes. Nothing here writes HITLCompleted."""
    from idp_common.dynamodb.service import DocumentDynamoDBService
    from idp_common.models import Document

    table.put_item(
        Item={
            "PK": f"doc#{key}",
            "SK": "none",
            "ObjectKey": key,
            "ItemType": "document",
            "InitialEventTime": "2026-10-01T00:00:00Z",
            "HITLTriggered": True,
            **attributes,
        }
    )
    service = DocumentDynamoDBService(table_name=TABLE)
    if status is not None:
        service.update_document(Document(input_key=key, hitl_status=status))
    if status in ("complete", "rejected"):
        service.update_review_attributes(
            key, {"HITLReviewedBy": "annotator@example.com", "ReviewDisposedAt": "t"}
        )
    item = table.get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]
    assert item.get("HITLStatus") == status
    assert "HITLCompleted" not in item
    return item


def _claims(caller=REVIEWER):
    return {
        "cognito:groups": ["Reviewer"],
        "cognito:username": caller["username"],
        "email": caller["email"],
    }


def _gsi_listed_and_counted(gsi):
    """Object keys listDocuments returns, and getDocumentCount's figure."""

    def call(field):
        event = {"info": {"fieldName": field}, "arguments": {}, "identity": {}}
        event["identity"]["claims"] = _claims()
        # Config-version scope is not under test here: the caller is unrestricted.
        with patch.object(gsi, "_caller_scope_or_deny", return_value=None):
            return gsi.handler(event, None)

    listed = sorted(d["ObjectKey"] for d in call("listDocuments")["Documents"])
    return listed, call("getDocumentCount")["count"]


def _range_includes(rng, item):
    caller = {**REVIEWER, "groups": ["Reviewer"]}
    caller.update(is_admin=False, is_author=False, is_viewer=False, is_reviewer=True)
    assert rng._is_reviewer_only(caller)
    return rng._should_include_document(item, caller, True, None)


@pytest.mark.unit
@pytest.mark.parametrize("status", sorted(FINISHED | OPEN, key=str))
def test_an_unowned_review_is_listed_only_while_it_is_owed(env, status):
    table, gsi, rng = env
    item = _seed(table, "d.pdf", status)
    owed = status in OPEN

    listed, counted = _gsi_listed_and_counted(gsi)
    assert listed == (["d.pdf"] if owed else [])
    assert counted == len(listed)
    assert _range_includes(rng, item) is owed


@pytest.mark.unit
@pytest.mark.parametrize("status", ["complete", "rejected", "COMPLETED"])
def test_a_finished_review_this_reviewer_owns_is_still_listed(env, status):
    """The "their own completed reviews" half of the grant is unchanged. The owner
    is the shape a legacy claim (claim_review) leaves on the row."""
    table, gsi, rng = env
    item = _seed(table, "d.pdf", status, HITLReviewOwner=REVIEWER["username"])

    assert _gsi_listed_and_counted(gsi) == (["d.pdf"], 1)
    assert _range_includes(rng, item) is True


@pytest.mark.unit
@pytest.mark.parametrize("status", ["complete", "rejected"])
def test_a_finished_review_another_reviewer_owns_is_not_listed(env, status):
    table, gsi, rng = env
    item = _seed(table, "d.pdf", status, HITLReviewOwner="someone-else")

    assert _gsi_listed_and_counted(gsi) == ([], 0)
    assert _range_includes(rng, item) is False


@pytest.mark.unit
def test_the_count_matches_the_list_over_a_mixed_table(env):
    """Finished and owed rows together: the header figure counts only the rows the
    list returns."""
    table, gsi, _ = env
    for status in sorted(FINISHED | OPEN, key=str):
        _seed(table, f"{status}.pdf", status)

    listed, counted = _gsi_listed_and_counted(gsi)
    assert listed == sorted(f"{s}.pdf" for s in OPEN)
    assert counted == len(OPEN)
