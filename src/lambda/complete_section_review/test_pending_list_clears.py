# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a reviewer sees after the last section is reviewed (issue #1214).

The rest of this directory's suite stands a fake document service in for
``create_document_service``, so it asserts what the resolver puts on the document
*object* and never what reaches the tracking item. That is exactly where this
defect lived: ``complete_section_review`` set ``document.hitl_sections_pending =
[]`` correctly — the sibling suite asserts it — and
``DocumentDynamoDBService.update_document`` then emitted no clause for it, because
it wrote the attribute under a truthiness test. The stored list kept the sections
that had just been reviewed.

So this module wires the **real** ``DocumentDynamoDBService`` to a real ``moto``
table and reads the item back. Nothing else here is new: the resolver, the
expression language and the document model are all the production ones.

Two consequences are measured, because the second is the one a user reports and it
is not obvious from the first:

* the pending list is empty in the item, not just on the object; and
* ``HITLStatus`` does not fall back to ``InProgress`` afterwards. A stale list is
  re-read by the *next* review, and ``all_completed`` is derived from it
  (``len(pending) == 0``), so reviewing any section of a finished document
  recomputed "not finished" and wrote ``InProgress`` over ``Completed`` — on a
  document already carrying ``HITLCompleted`` and already out of the queue. The
  stale list corrupts that derivation; clearing it is what makes the derivation
  right. This is adjacent to #1111, which is about two annotators racing, and is a
  different cause: one reviewer, one write, no concurrency.

⚠️ The fixture produces the **transition**, not a state. A document with sections
still pending cannot distinguish the fixed writer from the broken one — both write
the same non-empty list — so each test here reviews sections in sequence until the
list empties, and asserts on what the item holds after the write that empties it.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from moto import mock_aws

# The real library, imported at collection time so the sibling suites'
# `sys.modules.setdefault(...)` stubs cannot shadow it. `update_document` is the
# code under test here; a stub would assert this module's own fixture back.
from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

TRACKING_TABLE = "pending-clear-tracking"
OBJECT_KEY = "run-9/bank-statement.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}
REVIEWER = "reviewer@example.com"


@pytest.fixture
def aws(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
        "TRACKING_TABLE_NAME": TRACKING_TABLE,
        "OUTPUT_BUCKET": "pending-clear-output",
    }.items():
        monkeypatch.setenv(name, value)
    # TEST_SET_BUCKET unset: the baseline/ground-truth side of a review is not what
    # this module measures, and both of its entry points return immediately without
    # one. QUEUE_URL unset for the same reason on the reprocessing side.
    for name in ("TEST_SET_BUCKET", "QUEUE_URL", "WORKING_BUCKET", "INPUT_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        yield


@pytest.fixture
def mod(aws):
    """Import the resolver fresh, so its module-level clients are moto-backed."""
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    sys.modules.pop("index", None)
    module = importlib.import_module("index")
    module.dynamodb.create_table(
        TableName=TRACKING_TABLE,
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
    yield module
    sys.modules.pop("index", None)


@pytest.fixture
def table(mod):
    return mod.dynamodb.Table(TRACKING_TABLE)


@pytest.fixture
def document_service(mod):
    """The production service, bound to the moto table."""
    return DocumentDynamoDBService(
        dynamodb_client=DynamoDBClient(table_name=TRACKING_TABLE, region="us-east-1")
    )


def _sections(*section_ids: str) -> List[Dict[str, Any]]:
    return [
        {
            "Id": section_id,
            "PageIds": [index + 1],
            "Class": "bank-statement",
            "OutputJSONUri": "",
        }
        for index, section_id in enumerate(section_ids)
    ]


def seed(table, *, pending: List[str], sections: List[str], **attributes):
    """A document mid-review: some sections pending, and in the review queue."""
    item = {
        **DOC_KEY,
        "ObjectKey": OBJECT_KEY,
        "ObjectStatus": "COMPLETED",
        "HITLStatus": "InProgress",
        "HITLTriggered": True,
        "HITLPendingReview": "true",
        "HITLSectionsPending": pending,
        "HITLSectionsCompleted": [],
        "Sections": _sections(*sections),
    }
    item.update(attributes)
    table.put_item(Item=item)


def stored(table) -> Dict[str, Any]:
    return table.get_item(Key=DOC_KEY).get("Item", {})


def review(mod, document_service, section_id, reprocess):
    with (
        patch.object(mod, "create_document_service", return_value=document_service),
        patch.object(mod, "trigger_reprocessing", reprocess),
    ):
        return mod.complete_section_review(
            OBJECT_KEY, section_id, None, "reviewer", REVIEWER
        )


@pytest.mark.unit
def test_reviewing_the_last_section_empties_the_stored_pending_list(
    mod, table, document_service
):
    """The write the defect dropped.

    Both sections are reviewed in turn, so the list goes ``["sec-1", "sec-2"]`` →
    ``["sec-2"]`` → ``[]``. The first write is not discriminating — a non-empty
    list was always written — and the second is: against the truthiness test the
    item still reads ``["sec-2"]`` here, a section the reviewer has just finished,
    on a document reporting ``Completed``.
    """
    seed(table, pending=["sec-1", "sec-2"], sections=["sec-1", "sec-2"])
    reprocess = _Recorder()

    review(mod, document_service, "sec-1", reprocess)
    assert stored(table)["HITLSectionsPending"] == ["sec-2"]
    assert stored(table)["HITLStatus"] == "InProgress"

    review(mod, document_service, "sec-2", reprocess)

    item = stored(table)
    assert item["HITLSectionsPending"] == []
    assert sorted(item["HITLSectionsCompleted"]) == ["sec-1", "sec-2"]
    assert item["HITLStatus"] == "Completed"
    assert item["HITLCompleted"] is True
    assert "HITLPendingReview" not in item
    assert reprocess.calls == [OBJECT_KEY]


@pytest.mark.unit
def test_a_finished_document_does_not_fall_back_to_in_progress_on_a_later_review(
    mod, table, document_service
):
    """What the user reports, and the reason the stale list is not cosmetic.

    The UI keeps offering the sections named in ``HITLSectionsPending``, so a
    document left holding a stale one is reviewable again. That review re-reads the
    list, derives ``all_completed`` from it and, finding it non-empty, writes
    ``InProgress`` over ``Completed`` — leaving a row that simultaneously says the
    review is in progress, that ``HITLCompleted`` is true, and that the document is
    not in the review queue.

    The last assertion is what fails against the unfixed writer. Reprocessing
    firing a second time is the correct consequence of re-reviewing a finished
    document — summarization and evaluation have to see the new labels — and is
    asserted so a change in that behaviour is not mistaken for this fix.
    """
    seed(table, pending=["sec-1", "sec-2"], sections=["sec-1", "sec-2"])
    reprocess = _Recorder()
    review(mod, document_service, "sec-1", reprocess)
    review(mod, document_service, "sec-2", reprocess)
    assert stored(table)["HITLStatus"] == "Completed"

    # The reviewer opens sec-1 again and re-confirms it.
    review(mod, document_service, "sec-1", reprocess)

    item = stored(table)
    assert item["HITLStatus"] == "Completed"
    assert item["HITLSectionsPending"] == []
    assert reprocess.calls == [OBJECT_KEY, OBJECT_KEY]


@pytest.mark.unit
def test_a_single_section_document_completes_on_its_only_review(
    mod, table, document_service
):
    """The narrowest form of the transition, and the commonest in practice.

    One section means the very first review is the emptying one, so there is no
    intermediate non-empty write to mask the dropped clause.
    """
    seed(table, pending=["sec-1"], sections=["sec-1"])

    review(mod, document_service, "sec-1", _Recorder())

    item = stored(table)
    assert item["HITLSectionsPending"] == []
    assert item["HITLStatus"] == "Completed"


@pytest.mark.unit
def test_a_document_with_no_stored_review_lists_still_completes(
    mod, table, document_service
):
    """The seeded-from-sections path, where the attribute never existed.

    With no review state stored, the resolver derives the pending set from the
    document's own sections. On a one-section document that set is empty
    immediately, so the item must gain the attribute as `[]` rather than never
    gaining it — the UI reads absent and empty alike, but the next review reads the
    model, and `None` there would make this the one path that cannot report
    "nothing pending".
    """
    table.put_item(
        Item={
            **DOC_KEY,
            "ObjectKey": OBJECT_KEY,
            "ObjectStatus": "COMPLETED",
            "HITLStatus": "PendingReview",
            "Sections": _sections("sec-1"),
        }
    )

    review(mod, document_service, "sec-1", _Recorder())

    item = stored(table)
    assert item["HITLSectionsPending"] == []
    assert item["HITLSectionsCompleted"] == ["sec-1"]
    assert item["HITLStatus"] == "Completed"


class _Recorder:
    """Stands in for `trigger_reprocessing`, recording the keys it was given.

    Patched out rather than exercised: it re-queues the document through SQS, which
    is a different subject, and the sibling suite already covers it. Recording the
    calls keeps the "did the document finish?" signal visible here, since that
    trigger is gated on the same `all_completed` the stale list corrupted.
    """

    def __init__(self):
        self.calls: List[str] = []

    def __call__(self, object_key):
        self.calls.append(object_key)
