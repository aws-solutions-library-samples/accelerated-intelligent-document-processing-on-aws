# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``review_skip.classify``: what bulk Skip All Reviews does with each document."""

import pytest

from idp_common.review_queues.bulk_models import BulkOutcome, ReviewCaller
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.review_skip import classify

ADMIN = ReviewCaller(reviewer_key="admin@x", email="admin@x", is_admin=True)
REV = ReviewCaller(reviewer_key="rev@x", username="rev", email="rev@x")
SCOPED = ReviewCaller(
    reviewer_key="sc@x", username="sc", email="sc@x", allowed_versions=["claims"]
)


def _row(**attrs):
    return {"PK": "doc#k", "SK": "none", "ConfigVersion": "claims", **attrs}


def _item(state, owner=None, skip_job_id=None):
    return ReviewItemRecord(
        doc_key="k",
        cycle_id="0" * 21,
        profile="claims",
        queue="default",
        state=state,
        enqueued_at="2026-01-01T00:00:00.000Z",
        enqueued_at_ms=1,
        owner_key=owner,
        skip_job_id=skip_job_id,
    )


PENDING = {"HITLStatus": "Review Pending"}
IN_REVIEW = {"HITLStatus": "in_review"}

CASES = [
    # (row, item, caller, job, expected outcome, expected kind)
    (None, None, ADMIN, None, BulkOutcome.NOT_PERMITTED, "none"),
    (
        _row(**PENDING, ConfigVersion="other"),
        None,
        SCOPED,
        None,
        BulkOutcome.NOT_PERMITTED,
        "none",
    ),
    (
        _row(**PENDING, ConfigVersion=None),
        None,
        SCOPED,
        None,
        BulkOutcome.NOT_PERMITTED,
        "none",
    ),
    (_row(**PENDING, ConfigVersion="other"), None, ADMIN, None, None, "legacy"),
    (
        _row(**PENDING, TestSetId="ts"),
        None,
        ADMIN,
        None,
        BulkOutcome.TEST_STUDIO_EXCLUDED,
        "none",
    ),
    (
        _row(**PENDING, ItemType="test-document"),
        None,
        REV,
        None,
        BulkOutcome.TEST_STUDIO_EXCLUDED,
        "none",
    ),
    (
        _row(**IN_REVIEW),
        _item("HOLDING"),
        REV,
        None,
        BulkOutcome.NOT_FINISHED_PROCESSING,
        "queue",
    ),
    (_row(**IN_REVIEW), _item("OPEN"), REV, None, None, "queue"),
    (_row(**IN_REVIEW), _item("CLAIMED", owner="rev@x"), REV, None, None, "queue"),
    (
        _row(**IN_REVIEW),
        _item("CLAIMED", owner="other@x"),
        REV,
        None,
        BulkOutcome.HELD_BY_OTHER,
        "queue",
    ),
    (_row(**IN_REVIEW), _item("CLAIMED", owner="other@x"), ADMIN, None, None, "queue"),
    (
        _row(**IN_REVIEW),
        _item("FINALIZING"),
        REV,
        None,
        BulkOutcome.ALREADY_FINISHED,
        "queue",
    ),
    (
        _row(**IN_REVIEW),
        _item("FINALIZING", skip_job_id="brj-1"),
        REV,
        "brj-1",
        BulkOutcome.SKIPPED,
        "queue",
    ),
    (_row(**IN_REVIEW), None, REV, None, BulkOutcome.NOT_PENDING, "none"),
    (_row(**IN_REVIEW), _item("COMPLETE"), REV, None, BulkOutcome.NOT_PENDING, "none"),
    (
        _row(HITLStatus="complete"),
        _item("COMPLETE"),
        REV,
        None,
        BulkOutcome.ALREADY_FINISHED,
        "legacy",
    ),
    (
        _row(HITLStatus="Review Completed"),
        None,
        REV,
        None,
        BulkOutcome.ALREADY_FINISHED,
        "legacy",
    ),
    (
        _row(
            HITLStatus="Review Skipped",
            HITLReviewHistory=[{"action": "skip_all", "jobId": "brj-1"}],
        ),
        None,
        REV,
        "brj-1",
        BulkOutcome.SKIPPED,
        "legacy",
    ),
    (_row(**PENDING), None, REV, None, None, "legacy"),
    (
        _row(**PENDING, HITLReviewOwner="carol"),
        None,
        REV,
        None,
        BulkOutcome.HELD_BY_OTHER,
        "legacy",
    ),
    (_row(**PENDING, HITLReviewOwner="rev"), None, REV, None, None, "legacy"),
    (_row(**PENDING, HITLReviewOwner="carol"), None, ADMIN, None, None, "legacy"),
    (_row(HITLStatus="N/A"), None, ADMIN, None, BulkOutcome.NOT_PENDING, "none"),
    (_row(), None, ADMIN, None, BulkOutcome.NOT_PENDING, "none"),
]


@pytest.mark.unit
@pytest.mark.parametrize("row,item,caller,job,outcome,kind", CASES)
def test_classify(row, item, caller, job, outcome, kind):
    assert classify(row, item, caller, job_id=job) == (outcome, kind)


@pytest.mark.unit
def test_every_classifier_outcome_is_exercised():
    seen = {case[4] for case in CASES}
    assert {
        BulkOutcome.NOT_PERMITTED,
        BulkOutcome.TEST_STUDIO_EXCLUDED,
        BulkOutcome.NOT_FINISHED_PROCESSING,
        BulkOutcome.HELD_BY_OTHER,
        BulkOutcome.ALREADY_FINISHED,
        BulkOutcome.NOT_PENDING,
        BulkOutcome.SKIPPED,
    } <= seen
