# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A finished legacy review cannot be claimed or released.

``claimReview`` writes ``InProgress`` and ``releaseReview`` writes ``Review
Pending``. Either one, applied to a finished review, reopens it: a claimed review
accepts edits again (``saveSectionReviewEdits`` admits the owner of an open review),
and a released one is listed for every Reviewer. A finished review keeps
``HITLReviewOwner`` naming the reviewer who finished it, so the owner checks on
both operations admit it. The web UI offers neither action on a finished review
(``hasClaimableItems`` / ``hasReleasableItems`` in ``documents-table-config.tsx``
both exclude ``hitlCompleted``), and the server now holds the same rule:
``_review_finished``, the document lists' rule, plus the A2I ``COMPLETED`` spelling.

**The stored states come from the real writers.** The document is created and set
pending by ``idp_common``'s ``DocumentDynamoDBService`` (the pipeline's writer), it
is claimed through the handler, and it is finished by the handler's own
``completeSectionReview`` and ``skipAllSectionsReview`` — so the row each guard
reads is the row production leaves, owner attribute included. ``Review Completed``
is what ``processChanges`` stores, written here through the same
``update_document`` it uses. ``COMPLETED`` is the earlier A2I flow's spelling, which
no live writer stores and retained rows may carry; it is planted with a raw
``update_item``. ``moto`` stands in for DynamoDB, so the tests that depend on a
``ConditionExpression`` measure moto's evaluation of it, not DynamoDB's.

A queue review that ended ``complete`` or ``rejected`` is refused before either
function runs, by ``_assert_not_queue_managed`` (``QUEUE_MANAGED``), and is pinned
in ``test_index_review_lifecycle.py``.

``claimReview`` also needs a review to have been raised (``_review_raised``, the web
UI's ``hitlTriggered``: ``HITLTriggered`` true, or ``HITLStatus`` set and not
``N/A``), for every caller, and refuses otherwise with ``NO_REVIEW_PENDING``, the
code Skip All Reviews uses for the same rule. An ownerless ``Review Pending`` row an
earlier version left is a raised review and is still claimed.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
from moto import mock_aws

# Imported at collection, before a sibling suite's fixture can stub it with a
# MagicMock through `sys.modules.setdefault`: the resolver's document service must be
# the real one for the stored states to be the real writers'.
import idp_common.docs_service  # noqa: F401
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Section, Status
from idp_common.review_queues.statuses import LegacyHitlStatus, ReviewStatus

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKING = "finished-review-tracking"
OBJECT_KEY = "run-3/statement.pdf"
KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}
FINISHED_MESSAGE = "^REVIEW_FINISHED: "


@pytest.fixture
def mod(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
        "TRACKING_TABLE": TRACKING,
        "TRACKING_TABLE_NAME": TRACKING,
        "OUTPUT_BUCKET": "finished-review-output",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("QUEUE_URL", "WORKING_BUCKET", "INPUT_BUCKET", "TEST_SET_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        if MODULE_DIR not in sys.path:
            sys.path.insert(0, MODULE_DIR)
        sys.modules.pop("index", None)
        module = importlib.import_module("index")
        module.dynamodb.create_table(
            TableName=TRACKING,
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
        _create_pending_document()
        yield module
        sys.modules.pop("index", None)


def _create_pending_document():
    """The pipeline's writer: a one-section document whose review is owed."""
    service = DocumentDynamoDBService()
    document = Document(
        id=OBJECT_KEY,
        input_key=OBJECT_KEY,
        status=Status.COMPLETED,
        queued_time="2026-10-01T00:00:00Z",
    )
    document.sections = [Section(section_id="1", classification="x", page_ids=["1"])]
    service.create_document(document)
    document.hitl_status = LegacyHitlStatus.PENDING_REVIEW.value
    service.update_document(document)


def event(field, user, groups, **arguments):
    return {
        "info": {"fieldName": field},
        "arguments": {"objectKey": OBJECT_KEY, **arguments},
        "identity": {
            "username": user,
            "claims": {"email": f"{user}@example.com", "cognito:groups": groups},
        },
    }


def row(mod):
    return mod.dynamodb.Table(TRACKING).get_item(Key=KEY)["Item"]


def claim(mod, user="alice", groups=("Reviewer",)):
    return mod.handler(event("claimReview", user, list(groups)), None)


def release(mod, user="alice", groups=("Reviewer",)):
    return mod.handler(event("releaseReview", user, list(groups)), None)


def finish(mod, how):
    """alice claims the review and finishes it the way ``how`` names."""
    claim(mod)
    if how == "completeSectionReview":
        mod.handler(
            event("completeSectionReview", "alice", ["Reviewer"], sectionId="1"), None
        )
    elif how == "skipAllSectionsReview":
        mod.handler(
            event(
                "skipAllSectionsReview",
                "alice",
                ["Reviewer"],
                reason="Duplicate document",
            ),
            None,
        )
    elif how == "processChanges":
        service = DocumentDynamoDBService()
        document = service.get_document(OBJECT_KEY)
        document.hitl_status = LegacyHitlStatus.REVIEW_COMPLETED.value
        service.update_document(document)
    else:
        # No live writer stores these; a retained row may.
        mod.dynamodb.Table(TRACKING).update_item(
            Key=KEY,
            UpdateExpression="SET HITLStatus = :s",
            ExpressionAttributeValues={":s": how},
        )
    stored = row(mod)
    assert mod._review_finished(stored["HITLStatus"]), stored["HITLStatus"]
    assert stored["HITLReviewOwner"] == "alice", "a finished review keeps its owner"
    return stored


FINISHERS = [
    "completeSectionReview",  # Completed
    "skipAllSectionsReview",  # Review Skipped
    "processChanges",  # Review Completed
    "COMPLETED",  # the A2I spelling
    "Skipped",
    " Review Completed ",  # refused by the rule on read, not by the condition
]


# --------------------------------------------------------------------------
# claimReview
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("how", FINISHERS)
@pytest.mark.parametrize(
    "caller",
    [("alice", ("Reviewer",)), ("root", ("Admin",))],
    ids=["owner", "admin"],
)
def test_a_finished_review_cannot_be_claimed(mod, how, caller):
    before = finish(mod, how)
    user, groups = caller
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        claim(mod, user, groups)
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize("how", FINISHERS)
def test_an_unowned_finished_review_cannot_be_claimed(mod, how):
    """With no owner the claim's ownership condition admits anyone."""
    finish(mod, how)
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY, UpdateExpression="REMOVE HITLReviewOwner, HITLReviewOwnerEmail"
    )
    before = row(mod)
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        claim(mod, "bob")
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [
        LegacyHitlStatus.PENDING_REVIEW.value,
        LegacyHitlStatus.REVIEW_PENDING.value,
        LegacyHitlStatus.IN_PROGRESS.value,
    ],
)
def test_an_open_review_is_still_claimed(mod, status):
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY,
        UpdateExpression="SET HITLStatus = :s",
        ExpressionAttributeValues={":s": status},
    )
    claim(mod, "bob")
    stored = row(mod)
    assert stored["HITLReviewOwner"] == "bob"
    assert stored["HITLStatus"] == LegacyHitlStatus.IN_PROGRESS.value


@pytest.mark.unit
def test_a_review_finished_between_the_read_and_the_claim_is_refused(mod):
    """The write's condition holds the rule, not only the read before it.

    The review is open when ``claim_review`` reads it and finished by the time the
    update is sent, so only the ``ConditionExpression`` can refuse it.
    """
    real_table = mod.dynamodb.Table(TRACKING)

    class FinishedJustBeforeTheWrite:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def update_item(self, **kwargs):
            real_table.update_item(
                Key=KEY,
                UpdateExpression="SET HITLStatus = :s",
                ExpressionAttributeValues={":s": LegacyHitlStatus.COMPLETED.value},
            )
            return real_table.update_item(**kwargs)

    with patch.object(mod.dynamodb, "Table", return_value=FinishedJustBeforeTheWrite()):
        with pytest.raises(ValueError, match=FINISHED_MESSAGE):
            mod.claim_review(OBJECT_KEY, "bob", "bob@example.com")
    stored = row(mod)
    assert stored["HITLStatus"] == LegacyHitlStatus.COMPLETED.value
    assert "HITLReviewOwner" not in stored


@pytest.mark.unit
def test_the_claim_race_is_explained_from_a_consistent_read(mod):
    """After the condition refuses, the re-read decides the message; an eventually
    consistent one could still show the owner's open claim and report "already
    claimed" for a review that has finished. moto cannot show that by outcome."""
    claim(mod)
    real_table = mod.dynamodb.Table(TRACKING)
    reads = []

    class FinishedJustBeforeTheWriteRecordingReads:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def get_item(self, **kwargs):
            reads.append(kwargs)
            return real_table.get_item(**kwargs)

        def update_item(self, **kwargs):
            real_table.update_item(
                Key=KEY,
                UpdateExpression="SET HITLStatus = :s",
                ExpressionAttributeValues={":s": LegacyHitlStatus.COMPLETED.value},
            )
            return real_table.update_item(**kwargs)

    with patch.object(
        mod.dynamodb, "Table", return_value=FinishedJustBeforeTheWriteRecordingReads()
    ):
        with pytest.raises(ValueError, match=FINISHED_MESSAGE):
            mod.claim_review(OBJECT_KEY, "alice", "alice@example.com")
    assert len(reads) >= 2, "the guard's read and the race re-read"
    assert all(read.get("ConsistentRead") is True for read in reads), reads


# --------------------------------------------------------------------------
# releaseReview
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("how", FINISHERS)
@pytest.mark.parametrize(
    "caller",
    [("alice", ("Reviewer",)), ("root", ("Admin",))],
    ids=["owner", "admin"],
)
def test_a_finished_review_cannot_be_released(mod, how, caller):
    before = finish(mod, how)
    user, groups = caller
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        release(mod, user, groups)
    assert row(mod) == before


@pytest.mark.unit
def test_a_claimed_open_review_is_still_released_by_its_owner_and_by_an_admin(mod):
    for user, groups in (("alice", ("Reviewer",)), ("root", ("Admin",))):
        claim(mod)
        release(mod, user, groups)
        stored = row(mod)
        assert stored["HITLStatus"] == LegacyHitlStatus.REVIEW_PENDING.value
        assert "HITLReviewOwner" not in stored


@pytest.mark.unit
def test_a_review_finished_between_the_read_and_the_release_is_refused(mod):
    claim(mod)
    real_table = mod.dynamodb.Table(TRACKING)

    class FinishedJustBeforeTheWrite:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def update_item(self, **kwargs):
            real_table.update_item(
                Key=KEY,
                UpdateExpression="SET HITLStatus = :s",
                ExpressionAttributeValues={":s": LegacyHitlStatus.REVIEW_SKIPPED.value},
            )
            return real_table.update_item(**kwargs)

    with patch.object(mod.dynamodb, "Table", return_value=FinishedJustBeforeTheWrite()):
        with pytest.raises(ValueError, match=FINISHED_MESSAGE):
            mod.release_review(OBJECT_KEY, "alice")
    stored = row(mod)
    assert stored["HITLStatus"] == LegacyHitlStatus.REVIEW_SKIPPED.value
    assert stored["HITLReviewOwner"] == "alice"


# --------------------------------------------------------------------------
# The rule and the condition derived from it
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_finished_statuses_are_derived_from_the_rule_and_are_not_empty(mod):
    derived = set(mod._finished_hitl_statuses())
    vocabulary = (
        {s.value for s in LegacyHitlStatus} | {s.value for s in ReviewStatus}
    ) | {"COMPLETED"}
    assert derived, "an empty derivation would make the condition refuse nothing"
    assert derived == {v for v in vocabulary if mod._review_finished(v)}
    # Written out, so the test does not only share the code's reading.
    assert {
        "Completed",
        "Review Completed",
        "Skipped",
        "Review Skipped",
        "COMPLETED",
        "complete",
        "rejected",
    } <= derived
    assert not derived & {
        "PendingReview",
        "Review Pending",
        "InProgress",
        "ReviewInProgress",
        "in_review",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    ["Completed", "Review Completed", "Skipped", "Review Skipped", "COMPLETED"],
)
def test_the_condition_refuses_every_finished_spelling_at_the_write(mod, status):
    """Asserted against the stored row through moto's expression engine."""
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY,
        UpdateExpression="SET HITLStatus = :s",
        ExpressionAttributeValues={":s": status},
    )
    condition, values = mod._not_finished_condition()
    with pytest.raises(
        mod.dynamodb.meta.client.exceptions.ConditionalCheckFailedException
    ):
        mod.dynamodb.Table(TRACKING).update_item(
            Key=KEY,
            UpdateExpression="SET Probe = :p",
            ConditionExpression=condition,
            ExpressionAttributeValues={":p": "x", **values},
        )


@pytest.mark.unit
def test_the_condition_admits_a_row_with_no_status(mod):
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY, UpdateExpression="REMOVE HITLStatus"
    )
    condition, values = mod._not_finished_condition()
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY,
        UpdateExpression="SET Probe = :p",
        ConditionExpression=condition,
        ExpressionAttributeValues={":p": "x", **values},
    )
    assert row(mod)["Probe"] == "x"


@pytest.mark.unit
def test_the_finished_check_reads_consistently_and_logs_the_refusal(mod, caplog):
    """Two things moto cannot show by outcome, asserted on the call and the log.

    moto answers every read consistently, so an eventually consistent read here
    passes every behavioural test above; on DynamoDB it can miss a review finished
    a moment ago. And a refusal is logged at WARNING with a "Rejecting" verb.
    """
    finish(mod, "completeSectionReview")
    real_table = mod.dynamodb.Table(TRACKING)
    reads = []

    class RecordingTable:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def get_item(self, **kwargs):
            reads.append(kwargs)
            return real_table.get_item(**kwargs)

    with (
        patch.object(mod.dynamodb, "Table", return_value=RecordingTable()),
        caplog.at_level("WARNING"),
    ):
        for operation in (mod.claim_review, mod.release_review):
            with pytest.raises(ValueError, match=FINISHED_MESSAGE):
                operation(OBJECT_KEY, "alice")
    assert reads, "the guard must read the row"
    assert all(read.get("ConsistentRead") is True for read in reads), reads
    assert "Rejecting claim of finished review" in caplog.text
    assert "Rejecting release of finished review" in caplog.text


# --------------------------------------------------------------------------
# skipAllSectionsReview: a non-Admin may skip only where the web UI offers it
# --------------------------------------------------------------------------


def skip(mod, user="bob", groups=("Reviewer",)):
    return mod.handler(
        event(
            "skipAllSectionsReview",
            user,
            list(groups),
            reason="Review not needed for this document",
        ),
        None,
    )


def set_status(mod, status):
    mod.dynamodb.Table(TRACKING).update_item(
        Key=KEY,
        UpdateExpression="SET HITLStatus = :s",
        ExpressionAttributeValues={":s": status},
    )


@pytest.mark.unit
@pytest.mark.parametrize("how", FINISHERS)
@pytest.mark.parametrize("user", ["alice", "bob"], ids=["owner", "other-reviewer"])
def test_a_reviewer_cannot_skip_a_finished_review(mod, how, user):
    before = finish(mod, how)
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        skip(mod, user)
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    ["completed", "Review  Skipped", "reviewcompleted", "SKIPPED"],
)
def test_a_reviewer_cannot_skip_a_status_the_web_ui_reads_as_finished(mod, status):
    """The web UI hides the button on these (lower-cased, whitespace removed),
    though the exact-match rule does not read them as finished. No writer stores
    them; the server refuses wherever either reading says finished."""
    set_status(mod, status)
    before = row(mod)
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        skip(mod)
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize("stored", ["absent", "N/A", ""])
def test_a_reviewer_cannot_skip_a_document_no_review_was_raised_on(mod, stored):
    table = mod.dynamodb.Table(TRACKING)
    if stored == "absent":
        table.update_item(
            Key=KEY, UpdateExpression="REMOVE HITLStatus, HITLPendingReview"
        )
    else:
        set_status(mod, stored)
    before = row(mod)
    with pytest.raises(ValueError, match="^NO_REVIEW_PENDING: "):
        skip(mod)
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize(
    "setup",
    ["PendingReview", "Review Pending", "claimed-by-alice", "HITLTriggered-only"],
)
def test_a_reviewer_may_skip_a_review_that_is_still_owed(mod, setup):
    table = mod.dynamodb.Table(TRACKING)
    if setup == "claimed-by-alice":
        claim(mod)
    elif setup == "HITLTriggered-only":
        # The legacy flag rows from earlier versions carry, with no status.
        table.update_item(
            Key=KEY,
            UpdateExpression="SET HITLTriggered = :t REMOVE HITLStatus",
            ExpressionAttributeValues={":t": True},
        )
    else:
        set_status(mod, setup)
    skip(mod)
    stored = row(mod)
    assert stored["HITLStatus"] == LegacyHitlStatus.REVIEW_SKIPPED.value
    assert stored["HITLReviewedBy"] == "bob"


@pytest.mark.unit
@pytest.mark.parametrize("how", ["completeSectionReview", "COMPLETED", "none"])
def test_an_admin_may_still_skip_any_document(mod, how):
    if how == "none":
        mod.dynamodb.Table(TRACKING).update_item(
            Key=KEY, UpdateExpression="REMOVE HITLStatus, HITLPendingReview"
        )
    else:
        finish(mod, how)
    skip(mod, "root", ("Admin",))
    stored = row(mod)
    assert stored["HITLStatus"] == LegacyHitlStatus.REVIEW_SKIPPED.value
    assert stored["HITLReviewedBy"] == "root"


class _FinishedJustBeforeTheWrite:
    """The real table, with the review finishing ahead of each update."""

    def __init__(self, real_table, status):
        self._real = real_table
        self._status = status

    def __getattr__(self, name):
        return getattr(self._real, name)

    def update_item(self, **kwargs):
        self._real.update_item(
            Key=KEY,
            UpdateExpression="SET HITLStatus = :s",
            ExpressionAttributeValues={":s": self._status},
        )
        return self._real.update_item(**kwargs)


def _run_skip_all(mod, is_admin):
    from idp_common.docs_service import create_document_service

    with patch.object(mod, "create_document_service", create_document_service):
        return mod.skip_all_sections_review(
            OBJECT_KEY, "bob", "bob@example.com", is_admin=is_admin
        )


@pytest.mark.unit
def test_a_review_finished_between_the_read_and_a_reviewers_skip_is_refused(mod):
    real_table = mod.dynamodb.Table(TRACKING)
    racing = _FinishedJustBeforeTheWrite(real_table, LegacyHitlStatus.COMPLETED.value)
    with patch.object(mod.dynamodb, "Table", return_value=racing):
        with pytest.raises(ValueError, match=FINISHED_MESSAGE):
            _run_skip_all(mod, is_admin=False)
    assert row(mod)["HITLStatus"] == LegacyHitlStatus.COMPLETED.value


@pytest.mark.unit
def test_an_admins_skip_carries_no_condition(mod):
    """Admin behaviour is unchanged: the same race lets the Admin's skip land."""
    real_table = mod.dynamodb.Table(TRACKING)
    racing = _FinishedJustBeforeTheWrite(real_table, LegacyHitlStatus.COMPLETED.value)
    with patch.object(mod.dynamodb, "Table", return_value=racing):
        _run_skip_all(mod, is_admin=True)
    assert row(mod)["HITLStatus"] == LegacyHitlStatus.REVIEW_SKIPPED.value


@pytest.mark.unit
def test_skip_all_defaults_to_the_non_admin_rule(mod):
    """A caller that omits ``is_admin`` gets the stricter rule, not the Admin one."""
    finish(mod, "completeSectionReview")
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        mod.skip_all_sections_review(OBJECT_KEY, "bob", "bob@example.com")


@pytest.mark.unit
def test_the_handler_tells_skip_all_whether_the_caller_is_an_admin(mod):
    """``is_admin`` comes from the verified group claim, not from an argument."""
    for groups, expected in ((["Admin"], True), (["Reviewer"], False)):
        with patch.object(mod, "skip_all_sections_review", return_value={}) as op:
            mod.handler(
                event(
                    "skipAllSectionsReview",
                    "bob",
                    groups,
                    reason="Duplicate document",
                    is_admin=True,
                ),
                None,
            )
        assert op.call_args.kwargs["is_admin"] is expected


@pytest.mark.unit
def test_a_reviewers_skip_reads_consistently(mod):
    real_table = mod.dynamodb.Table(TRACKING)
    reads = []

    class RecordingTable:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def get_item(self, **kwargs):
            reads.append(kwargs)
            return real_table.get_item(**kwargs)

    with patch.object(mod.dynamodb, "Table", return_value=RecordingTable()):
        _run_skip_all(mod, is_admin=False)
    # The first read is the guard's; the last is the response, after the write.
    assert reads
    assert reads[0].get("ConsistentRead") is True, reads


@pytest.mark.unit
@pytest.mark.parametrize("status", ["complete", "rejected", " rejected "])
def test_skip_all_itself_refuses_a_queue_outcome_for_a_reviewer(mod, status):
    """The handler refuses a queue status first (``QUEUE_MANAGED``); the function
    holds the shared finished rule on its own too. A queue outcome is not named by
    the web UI's spelling check, and the padded one is not matched by the write's
    exact condition either, so only the rule refuses it."""
    set_status(mod, status)
    before = row(mod)
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        _run_skip_all(mod, is_admin=False)
    assert row(mod) == before


# --------------------------------------------------------------------------
# claimReview: a review must have been raised
# --------------------------------------------------------------------------

NO_REVIEW_MESSAGE = "^NO_REVIEW_PENDING: "
CALLERS = [("bob", ("Reviewer",)), ("root", ("Admin",))]
CALLER_IDS = ["reviewer", "admin"]


def plant(mod, **attributes):
    """Replace the row with exactly these attributes, as a retained row may be."""
    mod.dynamodb.Table(TRACKING).put_item(Item={**KEY, **attributes})


#: Rows no review was raised on. ``null`` is a stored NULL, which the rule reads as
#: unset and which the condition must too.
NOT_RAISED = {
    "absent": {},
    "N/A": {"HITLStatus": "N/A"},
    "empty": {"HITLStatus": ""},
    "null": {"HITLStatus": None},
    "flag-false": {"HITLTriggered": False, "HITLStatus": "N/A"},
}


@pytest.mark.unit
@pytest.mark.parametrize("stored", NOT_RAISED, ids=list(NOT_RAISED))
@pytest.mark.parametrize("caller", CALLERS, ids=CALLER_IDS)
def test_a_document_no_review_was_raised_on_cannot_be_claimed(mod, stored, caller):
    """Admins included: claiming writes Review In Progress, which lists the
    document as a review in progress for an Admin as much as for a Reviewer."""
    plant(mod, **NOT_RAISED[stored])
    before = row(mod)
    user, groups = caller
    with pytest.raises(ValueError, match=NO_REVIEW_MESSAGE):
        claim(mod, user, groups)
    assert row(mod) == before


@pytest.mark.unit
@pytest.mark.parametrize("caller", CALLERS, ids=CALLER_IDS)
def test_an_old_ownerless_review_pending_row_is_still_claimed(mod, caller):
    """Rows an earlier version left: ``Review Pending`` with no owner attribute and
    no ``HITLTriggered``. They are not backfilled, and they are owed reviews."""
    plant(mod, HITLStatus=LegacyHitlStatus.REVIEW_PENDING.value)
    user, groups = caller
    claim(mod, user, groups)
    stored = row(mod)
    assert stored["HITLReviewOwner"] == user
    assert stored["HITLStatus"] == LegacyHitlStatus.IN_PROGRESS.value


@pytest.mark.unit
@pytest.mark.parametrize(
    "setup", ["pipeline-pending", "released", "HITLTriggered-only", "flag-and-N/A"]
)
@pytest.mark.parametrize("caller", CALLERS, ids=CALLER_IDS)
def test_a_raised_review_is_still_claimed(mod, setup, caller):
    if setup == "released":
        claim(mod)
        release(mod)
        assert row(mod)["HITLStatus"] == LegacyHitlStatus.REVIEW_PENDING.value
    elif setup == "HITLTriggered-only":
        plant(mod, HITLTriggered=True)
    elif setup == "flag-and-N/A":
        # The flag alone is enough, whatever the status says.
        plant(mod, HITLTriggered=True, HITLStatus="N/A")
    user, groups = caller
    claim(mod, user, groups)
    stored = row(mod)
    assert stored["HITLReviewOwner"] == user
    assert stored["HITLStatus"] == LegacyHitlStatus.IN_PROGRESS.value


@pytest.mark.unit
def test_a_finished_review_is_refused_as_finished_and_not_as_unraised(mod):
    """The finished check comes first, so the message says what happened."""
    finish(mod, "completeSectionReview")
    with pytest.raises(ValueError, match=FINISHED_MESSAGE):
        claim(mod, "bob")


@pytest.mark.unit
@pytest.mark.parametrize(
    "unraise",
    [
        {"SET": {"HITLStatus": "N/A"}},
        {"REMOVE": ["HITLStatus", "HITLPendingReview"]},
    ],
    ids=["set-N/A", "removed"],
)
def test_a_review_withdrawn_between_the_read_and_the_claim_is_refused(mod, unraise):
    """The write's condition holds the rule, not only the read before it.

    The review is raised when ``claim_review`` reads the row and gone by the time
    the update is sent, so only the ``ConditionExpression`` can refuse it, and the
    re-read after it phrases the refusal.
    """
    real_table = mod.dynamodb.Table(TRACKING)

    class WithdrawnJustBeforeTheWrite:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def update_item(self, **kwargs):
            if "SET" in unraise:
                real_table.update_item(
                    Key=KEY,
                    UpdateExpression="SET HITLStatus = :s",
                    ExpressionAttributeValues={":s": unraise["SET"]["HITLStatus"]},
                )
            else:
                real_table.update_item(
                    Key=KEY, UpdateExpression="REMOVE " + ", ".join(unraise["REMOVE"])
                )
            return real_table.update_item(**kwargs)

    with patch.object(
        mod.dynamodb, "Table", return_value=WithdrawnJustBeforeTheWrite()
    ):
        with pytest.raises(ValueError, match=NO_REVIEW_MESSAGE):
            mod.claim_review(OBJECT_KEY, "bob", "bob@example.com")
    stored = row(mod)
    assert "HITLReviewOwner" not in stored
    assert stored.get("HITLStatus") != LegacyHitlStatus.IN_PROGRESS.value


@pytest.mark.unit
def test_an_unraised_claim_is_refused_before_any_write_and_logged(mod, caplog):
    """The read refuses it, consistently, so no conditional write is attempted and
    the log says why. moto answers every read consistently, so the flag is asserted
    on the call."""
    plant(mod, HITLStatus="N/A")
    real_table = mod.dynamodb.Table(TRACKING)
    reads, writes = [], []

    class RecordingTable:
        def __getattr__(self, name):
            return getattr(real_table, name)

        def get_item(self, **kwargs):
            reads.append(kwargs)
            return real_table.get_item(**kwargs)

        def update_item(self, **kwargs):
            writes.append(kwargs)
            return real_table.update_item(**kwargs)

    with (
        patch.object(mod.dynamodb, "Table", return_value=RecordingTable()),
        caplog.at_level("WARNING"),
    ):
        with pytest.raises(ValueError, match=NO_REVIEW_MESSAGE):
            mod.claim_review(OBJECT_KEY, "bob")
    assert writes == []
    assert reads and all(read.get("ConsistentRead") is True for read in reads)
    assert "no review was raised" in caplog.text


#: Rows the rule and the write's condition are compared on. Written out, with the
#: verdict the web UI's ``hitlTriggered`` gives each, so the test does not only
#: share the code's reading.
RAISED_CASES = [
    ({}, False),
    ({"HITLStatus": ""}, False),
    ({"HITLStatus": "N/A"}, False),
    ({"HITLStatus": None}, False),
    ({"HITLTriggered": False}, False),
    ({"HITLTriggered": False, "HITLStatus": "N/A"}, False),
    ({"HITLStatus": "PendingReview"}, True),
    ({"HITLStatus": "Review Pending"}, True),
    ({"HITLStatus": "InProgress"}, True),
    ({"HITLStatus": "Completed"}, True),
    ({"HITLTriggered": True}, True),
    ({"HITLTriggered": True, "HITLStatus": "N/A"}, True),
    ({"HITLTriggered": False, "HITLStatus": "Review Pending"}, True),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "attributes,raised", RAISED_CASES, ids=[repr(c[0]) for c in RAISED_CASES]
)
def test_the_raised_condition_agrees_with_the_rule(mod, attributes, raised):
    """Measured through moto's expression engine, not DynamoDB's."""
    plant(mod, **attributes)
    assert mod._review_raised(row(mod)) is raised
    condition, values = mod._review_raised_condition()
    write = {
        "Key": KEY,
        "UpdateExpression": "SET Probe = :p",
        "ConditionExpression": condition,
        "ExpressionAttributeValues": {":p": "x", **values},
    }
    if raised:
        mod.dynamodb.Table(TRACKING).update_item(**write)
        assert row(mod)["Probe"] == "x"
    else:
        with pytest.raises(
            mod.dynamodb.meta.client.exceptions.ConditionalCheckFailedException
        ):
            mod.dynamodb.Table(TRACKING).update_item(**write)
