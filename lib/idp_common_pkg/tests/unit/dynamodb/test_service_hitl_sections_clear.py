# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`update_document` must be able to write an EMPTY review list (issue #1214).

`HITLSectionsPending` names the sections of a document that still need a human
review, and `complete_section_review` derives `all_completed` from the stored list
on the next review. `_document_to_update_expressions` wrote the attribute under a
truthiness test, so the one transition that matters — the last section being
reviewed, which takes the list from non-empty to `[]` — emitted no update clause at
all and the previous non-empty value stayed in the item. The document then read
`Completed` while still listing sections to review, and reviewing one of them again
re-read the stale list, re-derived "not finished" from it and set `HITLStatus` back
to `InProgress` on a document already carrying `HITLCompleted`.

⚠️ **The discriminating fixture is the transition, not the state.** A document
*with* sections pending cannot tell the fixed writer from the broken one: both
write the same list. Only a document whose stored list is non-empty and whose
in-memory list is `[]` distinguishes them, so every test here that measures the fix
seeds a non-empty attribute first and then writes an empty one over it.

⚠️ **The other half of the fix is what is NOT written**, and it needs its own
assertions — more of them than the clear does, because a caller that starts
clearing an attribute it never meant to touch is a worse and quieter defect than
the one being fixed. The rule the writer relies on is that **`[]` can only get onto
a `Document` by an in-process assignment**: both fields default to `None`, and both
loaders read an empty stored attribute or an empty payload key back as `None`. Three
routes therefore need their own test, because each is a different mechanism and each
would be a silent regression on its own:

* a freshly constructed `Document` (`workflow_tracker`'s fallback document, the
  baseline-copy resolver) — fails if the `field(default_factory=list)` default comes
  back;
* a `Document` rebuilt from a payload, including a **hand-built** one: a feature
  hook's `updatedDocument` is validated against an immutable-field list rather than a
  key allowlist, so a hook spelling out the whole document may carry
  `"hitl_sections_pending": []` as boilerplate, and taking that literally would
  destroy the review list of a document actually under review;
* a `Document` loaded from DynamoDB whose stored list is **already `[]`** — the
  normal state of every reviewed document. Reading that back as `[]` would make every
  unrelated whole-document write (an abort, a section re-grouping, an SDK rerun)
  re-assert it, which is a lost update against a concurrent re-trigger.

`TestTheOnlyWayToClearIsAnAssignment` holds all three, plus the symmetry assertion
that keeps the two transports from being changed on one side only.

The table is a real `moto` table and the service is the real
`DocumentDynamoDBService`, because the defect lives in an `UpdateExpression`: a
mock table accepts any expression and stores nothing, so it cannot tell a write
that clears the attribute from one that omits the clause.
"""

from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status

TABLE = "hitl-sections-table"
OBJECT_KEY = "run-3/statement.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
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
        ).wait_until_exists()
        yield DocumentDynamoDBService(
            dynamodb_client=DynamoDBClient(table_name=TABLE, region="us-east-1")
        )


@pytest.fixture
def table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def seed(table, **attributes):
    """Put a tracking item holding review state, as a document under review has."""
    item = {
        **DOC_KEY,
        "ObjectKey": OBJECT_KEY,
        "ObjectStatus": Status.COMPLETED.value,
        "HITLStatus": "InProgress",
    }
    item.update(attributes)
    table.put_item(Item=item)


def stored(table):
    return table.get_item(Key=DOC_KEY).get("Item", {})


@pytest.mark.unit
class TestTheEmptyTransitionIsWritten:
    """The state change the review workflow actually makes on its last section."""

    def test_emptying_the_pending_list_clears_the_stored_attribute(
        self, service, table
    ):
        """The defect, measured end to end through a real UpdateExpression.

        The fixture produces the transition the way the resolver does: the item
        already holds two pending sections, the document is loaded from that item
        (so its list starts non-empty, as `complete_section_review` sees it), the
        last section is taken out, and the document is written back. Against the
        truthiness test this assertion fails with the attribute still holding
        `["sec-1", "sec-2"]` — the write emitted no clause for it.
        """
        seed(table, HITLSectionsPending=["sec-1", "sec-2"])
        document = service.get_document(OBJECT_KEY)
        assert document is not None
        assert document.hitl_sections_pending == ["sec-1", "sec-2"]

        document.hitl_sections_pending = []
        document.hitl_sections_completed = ["sec-1", "sec-2"]
        document.hitl_status = "Completed"
        service.update_document(document)

        item = stored(table)
        assert item["HITLSectionsPending"] == []
        assert sorted(item["HITLSectionsCompleted"]) == ["sec-1", "sec-2"]
        assert item["HITLStatus"] == "Completed"

    def test_the_document_returned_by_the_write_reports_nothing_pending(
        self, service, table
    ):
        """`update_document` returns `ALL_NEW` reparsed into a Document.

        The resolver's next read comes back through that conversion, so a cleared
        attribute that reparsed as the stale list would reintroduce the defect one
        layer up. What the reparse must NOT do is report `[]`: every reader takes
        `... or []`, so absent, empty and unset are one answer to them, and `None`
        is what keeps the returned object from re-asserting the clear on a
        subsequent write.
        """
        seed(table, HITLSectionsPending=["sec-1"])
        document = service.get_document(OBJECT_KEY)
        assert document is not None
        document.hitl_sections_pending = []

        updated = service.update_document(document)

        assert (updated.hitl_sections_pending or []) == []
        assert updated.hitl_sections_pending is None

    def test_emptying_the_completed_list_clears_the_stored_attribute(
        self, service, table
    ):
        """`HITLSectionsCompleted` carried the same truthiness test.

        It is reset to `[]` when a document is re-queued for review after
        reprocessing (`processresults_function` assigns it explicitly), so the same
        collapse left a reset document listing sections as already reviewed.
        """
        seed(table, HITLSectionsCompleted=["sec-1"])
        document = service.get_document(OBJECT_KEY)
        assert document is not None
        document.hitl_sections_completed = []
        service.update_document(document)

        assert stored(table)["HITLSectionsCompleted"] == []

    def test_a_non_empty_list_is_still_written(self, service, table):
        """The pre-existing behaviour, pinned so the fix does not trade one for
        the other: mid-review, the narrowed list must still reach the item."""
        seed(table, HITLSectionsPending=["sec-1", "sec-2"])
        document = service.get_document(OBJECT_KEY)
        assert document is not None
        document.hitl_sections_pending = ["sec-2"]
        service.update_document(document)

        assert stored(table)["HITLSectionsPending"] == ["sec-2"]


@pytest.mark.unit
class TestTheOnlyWayToClearIsAnAssignment:
    """The risk the fix had to avoid, asserted rather than reasoned about.

    Most `update_document` callers have no interest in the review at all — OCR,
    classification, extraction, the workflow tracker, the abort resolver, the SDK's
    rerun and stop paths. They write the **whole** document, so any route that puts
    a literal `[]` on one of these fields turns one of those writes into a review
    clear, and the harm is a lost update against a document someone is reviewing
    right now. Each test below closes one such route, and each fails on its own.
    """

    def test_a_caller_that_never_mentions_hitl_leaves_the_pending_list_alone(
        self, service, table
    ):
        """A freshly constructed Document, which is what `workflow_tracker`'s
        fallback path and `copy_to_baseline_resolver` hand to `update_document`."""
        seed(table, HITLSectionsPending=["sec-1"], HITLSectionsCompleted=["sec-0"])

        service.update_document(
            Document(id=OBJECT_KEY, input_key=OBJECT_KEY, status=Status.COMPLETED)
        )

        item = stored(table)
        assert item["HITLSectionsPending"] == ["sec-1"]
        assert item["HITLSectionsCompleted"] == ["sec-0"]

    def test_a_document_rebuilt_from_a_payload_without_hitl_keys_clears_nothing(
        self, service, table
    ):
        """The commoner shape: a document reloaded from a Step Functions payload.

        `Document.to_dict` omits both review lists when they are falsy, so the
        payload every pipeline step passes on for a document with no review state
        carries neither key. `from_dict` must leave them unset rather than
        defaulting to `[]`, or the next `update_document` in the pipeline erases
        the pending list of a document that is under review while it reprocesses.
        """
        seed(table, HITLSectionsPending=["sec-1"])
        document = Document.from_dict(
            {"id": OBJECT_KEY, "input_key": OBJECT_KEY, "status": "COMPLETED"}
        )
        assert document.hitl_sections_pending is None

        service.update_document(document)

        assert stored(table)["HITLSectionsPending"] == ["sec-1"]

    def test_a_hand_built_payload_carrying_an_empty_list_clears_nothing(
        self, service, table
    ):
        """The route no first-party producer can take, and a third party can.

        `to_dict` omits an empty list, so a pipeline payload never carries the key
        with an empty value — but a feature hook's `updatedDocument` is not built by
        `to_dict`. The dispatcher validates it against a list of immutable fields
        (`id`, `input_key`, the buckets, `config_version`) rather than a key
        allowlist, so a hook that spells the document out may include
        `"hitl_sections_pending": []` simply because the document it was handed had
        no review. Read literally, that clears the pending list of a document
        actually under review — a whole review lost to a hook that meant nothing by
        it. `from_dict` therefore reads an explicit empty list as "says nothing",
        which is exactly what the hook path did before this change.
        """
        seed(table, HITLSectionsPending=["sec-1", "sec-2"])
        document = Document.from_dict(
            {
                "id": OBJECT_KEY,
                "input_key": OBJECT_KEY,
                "status": "COMPLETED",
                "hitl_sections_pending": [],
                "hitl_sections_completed": [],
            }
        )
        assert document.hitl_sections_pending is None

        service.update_document(document)

        assert stored(table)["HITLSectionsPending"] == ["sec-1", "sec-2"]

    def test_a_stored_empty_list_is_not_re_asserted_by_an_unrelated_write(
        self, service, table
    ):
        """The commonest shape of all, once the fix is in.

        `[]` is the normal state of every document whose review has completed, and
        plenty of writes touch such a document afterwards without caring about the
        review: `abort_workflow_resolver`, `process_changes_resolver`'s section
        re-grouping, the SDK's stop and rerun paths. If the loader read the stored
        `[]` back as `[]`, each of those would re-assert it — harmless in isolation,
        and a lost update when a re-trigger derives a fresh pending list between the
        read and the write. So no clause at all is the right answer here, and it is
        the same answer the truthiness test gave before this change.
        """
        seed(table, HITLSectionsPending=[], HITLSectionsCompleted=[])
        document = service.get_document(OBJECT_KEY)
        assert document is not None
        assert document.hitl_sections_pending is None

        _, _, values = service._document_to_update_expressions(document)

        assert ":HITLSectionsPending" not in values
        assert ":HITLSectionsCompleted" not in values

    def test_loading_an_item_with_no_review_lists_keeps_them_unset(
        self, service, table
    ):
        """Absence read from DynamoDB must stay absence, not become `[]`.

        Otherwise every load-modify-write of a document that has no review state
        would start asserting "no sections pending" — harmless on that document,
        and wrong the moment the load and the write straddle a review.
        """
        seed(table)

        document = service.get_document(OBJECT_KEY)

        assert document is not None
        assert document.hitl_sections_pending is None
        assert document.hitl_sections_completed is None

    def test_neither_transport_can_carry_an_empty_review_list(self):
        """The symmetry the writer's safety rests on, pinned in one place.

        Outbound, `to_dict` omits an empty list; inbound, `from_dict` reads one back
        as unset. Changing either side alone reopens a route to a silent review
        clear, and the two live in different methods, so this asserts the pair
        rather than each half in isolation.
        """
        document = Document(id="d.pdf", input_key="d.pdf")
        document.hitl_sections_pending = []
        document.hitl_sections_completed = []

        payload = document.to_dict()

        assert "hitl_sections_pending" not in payload
        assert "hitl_sections_completed" not in payload
        assert (
            Document.from_dict({**payload, "hitl_sections_pending": []})
        ).hitl_sections_pending is None


@pytest.mark.unit
class TestTheExpressionItself:
    """The clause-level view, so a failure names the mechanism directly."""

    def setup_method(self):
        # No table needed: these read the expression the service builds, before it
        # is sent anywhere.
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def _values(self, pending):
        document = Document(id="d.pdf", input_key="d.pdf")
        document.hitl_sections_pending = pending
        _, _, values = self.service._document_to_update_expressions(document)
        return values

    def test_an_empty_list_emits_a_clause(self):
        assert self._values([])[":HITLSectionsPending"] == []

    def test_a_populated_list_emits_a_clause(self):
        assert self._values(["sec-1"])[":HITLSectionsPending"] == ["sec-1"]

    def test_an_unset_list_emits_no_clause(self):
        assert ":HITLSectionsPending" not in self._values(None)
