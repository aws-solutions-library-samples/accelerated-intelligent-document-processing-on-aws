# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A review released back to the queue is listed for every Reviewer (#1586).

``releaseReview`` writes ``HITLStatus = "Review Pending"`` and removes the owner, so
another Reviewer can claim the document. Both document-list resolvers decide a
Reviewer's view from ``HITLTriggered`` or an active ``HITLStatus``. No current
writer sets ``HITLTriggered``, and ``Review Pending`` was not an active status, so a
released review vanished from every Reviewer's list and count.

Every row here is produced by the real writers, in the order the product runs them:
``DocumentDynamoDBService`` creates the document and records the pipeline's
``PendingReview``; the ``complete_section_review`` Lambda's handler claims,
releases, completes and skips; the review-queue tracker's ``apply_overrides`` sets a
queue-managed status. Nothing writes an attribute by hand. ``listDocuments`` and
``getDocumentCount`` run the real handler against moto's ``TypeDateIndex`` with the
production projection, so their FilterExpression is evaluated by moto's expression
engine, not DynamoDB's. ``listDocumentsByDateRange`` runs its real handler over the
list partitions ``create_document`` writes.

The widening is to released reviews only. ``EXPECTED`` writes out, per lifecycle and
caller, what each list shows; ``WAS_HIDDEN`` names the only cells the change moved.
``releaseReview`` on a document nobody has claimed is refused, since it would
otherwise list a never-reviewed document, or a finished review recognised only by
its status, for every Reviewer.
"""

import importlib.util
import os
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
_LAMBDA_DIR = os.path.join(_REPO, "nested", "api-resolvers", "src", "lambda")
_REVIEW_LAMBDA = os.path.join(_REPO, "src", "lambda", "complete_section_review")
TABLE = "reviewer-list-released"
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
    "TRACKING_TABLE": TABLE,
    "HITL_TABLE_NAME": "",
    "TEST_SET_BUCKET": "",
    "QUEUE_URL": "",
}
DAY = "2026-10-01"

#: Callers: (Cognito groups, username). The username is what claimReview stores as
#: HITLReviewOwner and what the resolvers compare with ``cognito:username``.
CALLERS = {
    "rev": (["Reviewer"], "rev"),
    "other": (["Reviewer"], "other"),
    "admin": (["Admin"], "admin"),
}

#: Each lifecycle is a sequence of real writes. ``("claim", who)`` and the others go
#: through the complete_section_review handler as that caller.
LIFECYCLES = {
    "not-hitl": [],
    "pending": [("pending",)],
    "claimed-by-rev": [("pending",), ("claimReview", "rev")],
    "claimed-by-other": [("pending",), ("claimReview", "other")],
    "released-by-rev": [
        ("pending",),
        ("claimReview", "rev"),
        ("releaseReview", "rev"),
    ],
    "released-by-admin": [
        ("pending",),
        ("claimReview", "other"),
        ("releaseReview", "admin"),
    ],
    "released-then-claimed-by-rev": [
        ("pending",),
        ("claimReview", "other"),
        ("releaseReview", "other"),
        ("claimReview", "rev"),
    ],
    "released-then-claimed-by-other": [
        ("pending",),
        ("claimReview", "rev"),
        ("releaseReview", "rev"),
        ("claimReview", "other"),
    ],
    "completed-by-rev": [
        ("pending",),
        ("claimReview", "rev"),
        ("completeSectionReview", "rev"),
    ],
    "completed-by-other": [
        ("pending",),
        ("claimReview", "other"),
        ("completeSectionReview", "other"),
    ],
    "skipped-by-rev": [
        ("pending",),
        ("claimReview", "rev"),
        ("skipAllSectionsReview", "rev"),
    ],
    "skipped-by-admin": [("pending",), ("skipAllSectionsReview", "admin")],
    # A finished review keeps its owner, and releasing it is refused: it would put a
    # finished review back in every Reviewer's queue. The row stays as finished.
    "completed-then-released-by-rev": [
        ("pending",),
        ("claimReview", "rev"),
        ("completeSectionReview", "rev"),
        ("refused", "releaseReview", "rev", "REVIEW_FINISHED"),
    ],
    # Release gives up a claim, so a document nobody has claimed is refused: a
    # finished review the list recognises only by its status would otherwise be
    # listed for every Reviewer.
    "skipped-then-released-by-admin": [
        ("pending",),
        ("skipAllSectionsReview", "admin"),
        ("refused", "releaseReview", "admin"),
    ],
    "pending-released-by-admin-unclaimed": [
        ("pending",),
        ("refused", "releaseReview", "admin"),
    ],
    "queue-in-review": [("queue", "hold")],
    "queue-complete": [("queue", "complete")],
    "queue-rejected": [("queue", "rejected")],
    # A document with no review and no owner cannot be released either: the release
    # would write Review Pending and list it for every Reviewer. The web UI's bulk
    # Release acts on every selected row, so an Admin can send one by accident.
    "unreviewed-release-by-admin": [("refused", "releaseReview", "admin")],
    "unreviewed-release-by-rev": [("refused", "releaseReview", "rev")],
    # claimReview does not check that a review is owed, so a claim followed by a
    # release still puts a never-reviewed document back in the queue. It is then a
    # released review like any other.
    "unreviewed-claimed-then-released-by-other": [
        ("claimReview", "other"),
        ("releaseReview", "other"),
    ],
}

ALL = frozenset(LIFECYCLES)
#: What each caller's document list shows after the change. Written out, not derived
#: from the code under test.
EXPECTED = {
    "rev": frozenset(
        {
            "pending",
            "pending-released-by-admin-unclaimed",
            "claimed-by-rev",
            "released-by-rev",
            "released-by-admin",
            "released-then-claimed-by-rev",
            "unreviewed-claimed-then-released-by-other",
        }
    ),
    "other": frozenset(
        {
            "pending",
            "pending-released-by-admin-unclaimed",
            "claimed-by-other",
            "released-by-rev",
            "released-by-admin",
            "released-then-claimed-by-other",
            "unreviewed-claimed-then-released-by-other",
        }
    ),
    "admin": ALL,
}
#: The only cells #1586 moved: before it, these were missing from that list. The
#: unclaimed release was accepted then, and wrote a status no Reviewer was shown.
WAS_HIDDEN = {
    "rev": frozenset(
        {
            "released-by-rev",
            "released-by-admin",
            "unreviewed-claimed-then-released-by-other",
            "pending-released-by-admin-unclaimed",
        }
    ),
    "other": frozenset(
        {
            "released-by-rev",
            "released-by-admin",
            "unreviewed-claimed-then-released-by-other",
            "pending-released-by-admin-unclaimed",
        }
    ),
    "admin": frozenset(),
}


def _load_resolver(resolver):
    """Import a resolver by path, then put back the sibling modules and path."""
    directory = os.path.join(_LAMBDA_DIR, resolver)
    previous = {name: sys.modules.pop(name, None) for name in _SIBLINGS}
    path_before = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"{resolver}_released_review", os.path.join(directory, "index.py")
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


def _load_review_lambda():
    spec = importlib.util.spec_from_file_location(
        "complete_section_review_released_review",
        os.path.join(_REVIEW_LAMBDA, "index.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_table(dynamodb):
    return dynamodb.create_table(
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


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV), mock_aws():
        dynamodb = boto3.resource("dynamodb")
        table = _create_table(dynamodb)
        gsi = _load_resolver("list_documents_gsi_resolver")
        gsi.dynamodb = dynamodb
        rng = _load_resolver("list_documents_range_resolver")
        rng.dynamodb = dynamodb
        review = _load_review_lambda()
        review.dynamodb = dynamodb
        review.TRACKING_TABLE_NAME = TABLE
        yield table, gsi, rng, review


def _identity(caller):
    groups, username = CALLERS[caller]
    return {
        "username": username,
        "claims": {
            "cognito:groups": groups,
            "cognito:username": username,
            "email": f"{username}@example.com",
        },
    }


def _queue_status(key, kind):
    """The review-queue tracker's status for a hold or a finalize run."""
    from idp_common.models import Document
    from idp_common.review_queues import tracker
    from idp_common.review_queues.models import ReviewItemRecord
    from idp_common.review_queues.statuses import QueueItemState

    item = ReviewItemRecord(
        doc_key=key,
        cycle_id="c1",
        profile="default",
        queue="default",
        state=(
            QueueItemState.HOLDING.value
            if kind == "hold"
            else QueueItemState.FINALIZING.value
        ),
        enqueued_at="2026-10-01T00:00:00Z",
        enqueued_at_ms=0,
        disposition=None if kind == "hold" else kind,
    )
    ctx = (
        tracker.TrackerReviewContext(kind="hold", item=item)
        if kind == "hold"
        else tracker.TrackerReviewContext(
            kind="finalize", item=item, token="t", outcome=kind
        )
    )
    doc, _ = tracker.apply_overrides(ctx, Document(id=key, input_key=key))
    return doc.hitl_status


def _write(table, review, index, name):
    """Run one lifecycle's writes against a fresh document; return the stored row."""
    from idp_common.dynamodb.service import DocumentDynamoDBService
    from idp_common.models import Document, Section, Status

    key = f"{name}.pdf"
    when = f"{DAY}T01:{index:02d}:00Z"  # create_document keys the list entry on it
    service = DocumentDynamoDBService(table_name=TABLE)
    service.create_document(
        Document(
            id=key,
            input_key=key,
            status=Status.COMPLETED,
            initial_event_time=when,
            queued_time=when,
        )
    )
    for step in LIFECYCLES[name]:
        if step[0] == "pending":
            # The pipeline's write when a section falls below the threshold.
            service.update_document(
                Document(
                    id=key,
                    input_key=key,
                    status=Status.COMPLETED,
                    sections=[
                        Section(section_id="1", classification="x", page_ids=["1"])
                    ],
                    hitl_status="PendingReview",
                    hitl_sections_pending=["1"],
                )
            )
        elif step[0] == "queue":
            service.update_document(
                Document(
                    id=key,
                    input_key=key,
                    status=Status.COMPLETED,
                    hitl_status=_queue_status(key, step[1]),
                )
            )
            if step[1] != "hold":
                service.update_review_attributes(
                    key, {"HITLReviewedBy": "annotator@example.com"}
                )
        else:
            refused = step[0] == "refused"
            field, caller = step[1:3] if refused else step
            code = step[3] if refused and len(step) > 3 else "NOT_CLAIMED"
            arguments = {"objectKey": key}
            if field == "completeSectionReview":
                arguments["sectionId"] = "1"
            if field == "skipAllSectionsReview":
                arguments["reason"] = "Duplicate document"
            event = {
                "info": {"fieldName": field},
                "arguments": arguments,
                "identity": _identity(caller),
            }
            if refused:
                with pytest.raises(ValueError, match=f"^{code}: "):
                    review.handler(event, None)
            else:
                review.handler(event, None)
    return table.get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]


@pytest.fixture
def written(env):
    table, gsi, rng, review = env
    rows = {
        name: _write(table, review, i, name)
        for i, name in enumerate(sorted(LIFECYCLES))
    }
    return rows, gsi, rng


def _gsi(gsi, caller):
    """Lifecycles listDocuments returns to the caller, and getDocumentCount."""

    def call(field):
        event = {
            "info": {"fieldName": field},
            "arguments": {},
            "identity": _identity(caller),
        }
        # Config-version scope is not under test here: the caller is unrestricted.
        with patch.object(gsi, "_caller_scope_or_deny", return_value=None):
            return gsi.handler(event, None)

    listed = {
        d["ObjectKey"][: -len(".pdf")] for d in call("listDocuments")["Documents"]
    }
    return listed, call("getDocumentCount")["count"]


def _range(rng, caller):
    """Lifecycles listDocumentsByDateRange returns to the caller."""
    event = {
        "info": {"fieldName": "listDocumentsByDateRange"},
        "arguments": {
            "startDateTime": f"{DAY}T00:00:00Z",
            "endDateTime": f"{DAY}T23:59:59Z",
            "limit": 200,
        },
        "identity": _identity(caller),
    }
    with patch.object(rng, "_caller_scope_or_deny", return_value=None):
        result = rng.handler(event, None)
    assert result["nextToken"] is None  # the whole day fits one page
    return {d["ObjectKey"][: -len(".pdf")] for d in result["Documents"]}


@pytest.mark.unit
def test_the_expected_table_names_only_known_lifecycles():
    for caller in CALLERS:
        assert EXPECTED[caller] <= ALL
        assert WAS_HIDDEN[caller] <= EXPECTED[caller]
        # The change widens to released reviews only.
        assert all("released" in name for name in WAS_HIDDEN[caller])


@pytest.mark.unit
def test_the_writers_leave_the_rows_this_test_describes(written):
    """The lifecycles reach the stored shapes the expectations are about. Without
    this a writer that failed quietly would make a hidden row look correctly
    filtered."""
    rows, _, _ = written
    for name, row in rows.items():
        assert "HITLTriggered" not in row, name
    for name in (
        "not-hitl",
        "unreviewed-release-by-admin",
        "unreviewed-release-by-rev",
    ):
        for attribute in ("HITLStatus", "HITLReviewOwner", "HITLPendingReview"):
            assert attribute not in rows[name], (name, attribute)
    for name in ("released-by-rev", "released-by-admin"):
        assert rows[name]["HITLStatus"] == "Review Pending"
        assert "HITLReviewOwner" not in rows[name]
        assert "HITLCompleted" not in rows[name]
    assert rows["completed-then-released-by-rev"]["HITLStatus"] == "Completed"
    assert rows["completed-then-released-by-rev"]["HITLCompleted"] is True
    assert rows["completed-then-released-by-rev"]["HITLReviewOwner"] == "rev"
    assert rows["skipped-then-released-by-admin"]["HITLStatus"] == "Review Skipped"
    assert rows["pending-released-by-admin-unclaimed"]["HITLStatus"] == "PendingReview"
    assert rows["claimed-by-other"]["HITLReviewOwner"] == "other"
    assert rows["released-then-claimed-by-other"]["HITLReviewOwner"] == "other"
    assert rows["released-then-claimed-by-rev"]["HITLReviewOwner"] == "rev"
    assert rows["completed-by-rev"]["HITLStatus"] == "Completed"
    assert rows["completed-by-rev"]["HITLReviewOwner"] == "rev"
    assert rows["skipped-by-rev"]["HITLStatus"] == "Review Skipped"
    assert rows["queue-in-review"]["HITLStatus"] == "in_review"
    assert rows["queue-complete"]["HITLStatus"] == "complete"
    assert rows["queue-rejected"]["HITLStatus"] == "rejected"


@pytest.mark.unit
@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_listDocuments_and_its_count_show_each_caller_exactly_the_expected_rows(
    written, caller
):
    _, gsi, _ = written
    listed, counted = _gsi(gsi, caller)
    assert listed == EXPECTED[caller]
    assert counted == len(EXPECTED[caller])


@pytest.mark.unit
@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_listDocumentsByDateRange_shows_each_caller_exactly_the_expected_rows(
    written, caller
):
    _, _, rng = written
    assert _range(rng, caller) == EXPECTED[caller]


@pytest.mark.unit
@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_the_two_lists_agree_row_for_row(written, caller):
    _, gsi, rng = written
    assert _gsi(gsi, caller)[0] == _range(rng, caller)


@pytest.mark.unit
@pytest.mark.parametrize(
    "resolver", ["list_documents_gsi_resolver", "list_documents_range_resolver"]
)
def test_both_resolvers_hold_the_same_active_statuses(resolver):
    with patch.dict(os.environ, _ENV):
        module = _load_resolver(resolver)
    assert module._REVIEWER_ACTIVE_STATUSES == (
        "PendingReview",
        "Review Pending",
        "InProgress",
        "ReviewInProgress",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [
        " Review Pending",
        "Review Pending ",
        "Review Pending\n",
        "review pending",
        "Review  Pending",
        "ReviewPending",
    ],
    ids=repr,
)
def test_a_near_spelling_of_review_pending_is_listed_by_neither(env, status):
    """No writer stores these; a hand-written row stands in for one. The
    listDocuments FilterExpression can match only exact values, so the date-range
    resolver must not read them more generously than it does (the normaliser's
    ``strip()`` would), or the two lists would disagree."""
    table, gsi, rng, _ = env
    table.put_item(
        Item={
            "PK": "doc#d.pdf",
            "SK": "none",
            "ObjectKey": "d.pdf",
            "ItemType": "document",
            "InitialEventTime": f"{DAY}T01:00:00Z",
            "HITLStatus": status,
        }
    )
    item = table.get_item(Key={"PK": "doc#d.pdf", "SK": "none"})["Item"]
    caller = {
        "groups": ["Reviewer"],
        "username": "rev",
        "email": "rev@example.com",
        "is_admin": False,
        "is_author": False,
        "is_viewer": False,
        "is_reviewer": True,
    }

    assert _gsi(gsi, "rev") == (set(), 0)
    assert rng._should_include_document(item, caller, True, None) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", ["PendingReview", "Review Pending", "InProgress", "ReviewInProgress"]
)
def test_each_active_status_lists_an_unclaimed_row_in_both_resolvers(env, status):
    """A hand-written row, since no current writer stores ``ReviewInProgress``: the
    tuple test above cannot see a listDocuments filter built from part of it."""
    table, gsi, rng, _ = env
    table.put_item(
        Item={
            "PK": "doc#d.pdf",
            "SK": "none",
            "ObjectKey": "d.pdf",
            "ItemType": "document",
            "InitialEventTime": f"{DAY}T01:00:00Z",
            "HITLStatus": status,
        }
    )
    item = table.get_item(Key={"PK": "doc#d.pdf", "SK": "none"})["Item"]
    caller = {
        "groups": ["Reviewer"],
        "username": "rev",
        "email": "rev@example.com",
        "is_admin": False,
        "is_author": False,
        "is_viewer": False,
        "is_reviewer": True,
    }

    assert _gsi(gsi, "rev") == ({"d"}, 1)
    assert rng._should_include_document(item, caller, True, None) is True
