# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""processChanges records who completed a review that finished without saying.

A review completed section by section (completeSectionReview) used to finish with
no ``HITLReviewedBy``, so "Review Completed By" rendered blank, and processChanges
would not fill it in because the review already read as finished. It now fills in
a missing reviewer from its caller and never overwrites a recorded one.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")

_ENV = {
    "AWS_REGION": "us-east-1",
    "INPUT_BUCKET": "input-bucket",
    "OUTPUT_BUCKET": "output-bucket",
    "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
    "WORKING_BUCKET": "working-bucket",
    "TRACKING_TABLE": "tracking",
}


@pytest.fixture
def resolver():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "process_changes_reviewer_backfill_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/process_changes_resolver/index.py",
                ),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            yield mod


def _event():
    return {
        "info": {"fieldName": "processChanges"},
        "identity": {
            "username": "rev",
            "claims": {"email": "rev@example.com", "cognito:groups": ["Reviewer"]},
        },
        "arguments": {"objectKey": "packet.pdf", "modifiedSections": []},
    }


def _hitl_updates(resolver, hitl_status):
    """The tracking-table writes processChanges makes for a document in this state."""
    document = MagicMock()
    document.hitl_status = hitl_status
    document.hitl_sections_pending = []
    document.hitl_sections_completed = ["1"]
    document.sections = []
    service = MagicMock()
    service.get_document.return_value = document
    dynamodb = MagicMock()
    with (
        patch.dict(os.environ, _ENV),
        patch.object(resolver, "create_document_service", return_value=service),
        patch.object(resolver.boto3, "resource", return_value=dynamodb),
    ):
        resolver.handler(_event(), None)
    return [
        call.kwargs for call in dynamodb.Table.return_value.update_item.call_args_list
    ]


@pytest.mark.unit
@pytest.mark.parametrize("status", ["Completed", "Review Completed", "Review Skipped"])
def test_a_finished_review_gains_a_missing_reviewer_without_overwriting_one(
    resolver, status
):
    updates = _hitl_updates(resolver, status)
    backfills = [u for u in updates if "HITLReviewedBy" in u["UpdateExpression"]]
    assert len(backfills) == 1
    (backfill,) = backfills
    # The condition is what keeps a recorded reviewer: an unconditional SET would
    # rename every finished review after whoever next reprocessed it. It is keyed on
    # either half, so the name and email are written as one pair or not at all
    # (a review queue records a name and no email, #1568).
    assert backfill["ConditionExpression"] == (
        "attribute_not_exists(HITLReviewedBy) AND "
        "attribute_not_exists(HITLReviewedByEmail)"
    )
    assert "HITLReviewedBy = :reviewedBy" in backfill["UpdateExpression"]
    assert "HITLReviewedByEmail = :reviewedByEmail" in backfill["UpdateExpression"]
    assert "HITLStatus" not in backfill["UpdateExpression"]
    assert backfill["ExpressionAttributeValues"] == {
        ":reviewedBy": "rev",
        ":reviewedByEmail": "rev@example.com",
    }


@pytest.mark.unit
def test_an_open_review_is_completed_and_stamped_unconditionally(resolver):
    updates = _hitl_updates(resolver, "InProgress")
    (completion,) = [u for u in updates if "HITLReviewedBy" in u["UpdateExpression"]]
    assert "if_not_exists" not in completion["UpdateExpression"]
    assert completion["ExpressionAttributeValues"][":reviewedBy"] == "rev"


@pytest.mark.unit
def test_a_document_with_no_review_gets_no_reviewer(resolver):
    updates = _hitl_updates(resolver, None)
    assert not [u for u in updates if "HITLReviewedBy" in u["UpdateExpression"]]
