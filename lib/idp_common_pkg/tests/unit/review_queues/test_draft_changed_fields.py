# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The fields a draft record says the reviewer changed (moto HITLTable).

Every save compares the draft with the stored section result, never with the
draft before it, so ``changed_fields`` names every field the reviewer changed in
the cycle and not only the ones of the last save.

Also here: deleting a section's draft record (``ReviewStore.delete_draft``).
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, Section
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import (
    CallerContext,
    DraftRecord,
    ReviewItemRecord,
)
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

pytestmark = pytest.mark.unit

REVIEWER = CallerContext(
    reviewer_key="rev@example.com",
    email="rev@example.com",
    groups=frozenset({"Reviewer"}),
)
CYCLE = "0" * 21
KEY = "claims/a.pdf"
BUCKET = "out"
RESULT_URI = f"s3://{BUCKET}/{KEY}/sections/1/result.json"
RESULT = {"inference_result": {"Vendor": "ACME", "Total": 10, "Lines": [{"Qty": 1}]}}


def _draft(**values):
    return {"inference_result": {**RESULT["inference_result"], **values}}


@pytest.fixture
def s3():
    client = MagicMock(name="s3")
    client.get_json_content.side_effect = lambda uri: {RESULT_URI: RESULT}[uri]
    return client


@pytest.fixture
def service(review_table, s3):
    store = ReviewStore(review_table, region=REGION)
    store.queue_defs.put(QueueDefinition(id="claims", priority=70), create=True)
    document = Document(id=KEY, input_key=KEY)
    document.sections.append(
        Section(
            section_id="1",
            classification="Invoice",
            page_ids=["1"],
            extraction_result_uri=RESULT_URI,
        )
    )
    service = ReviewQueueService(
        store,
        QueueDirectory(store.queue_defs),
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=s3,
        document_loader={KEY: document}.get,
        output_bucket=BUCKET,
    )
    now_ms = int(time.time() * 1000)
    record = ReviewItemRecord(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="default",
        queue="claims",
        state="CLAIMED",
        enqueued_at="2026-11-16T00:00:00.000Z",
        enqueued_at_ms=now_ms,
        sections=["1"],
        version=1,
        owner_key=REVIEWER.reviewer_key,
        owner_email=REVIEWER.email,
        claimed_at_ms=now_ms,
        lease_expires_at_ms=now_ms + 3_600_000,
    )
    store.client.put_item(item_to_dynamo(record))
    return service


def _save(service, data, base_revision):
    return service.save_draft(REVIEWER, KEY, CYCLE, "1", data, base_revision)


def _changed(service):
    return service.store.get_draft(KEY, CYCLE, "1").changed_fields


class TestTwoSaves:
    def test_the_changed_fields_cover_both(self, service):
        first = _save(service, _draft(Vendor="Acme Corp"), None)
        assert _changed(service) == ["Vendor"]

        second = _save(service, _draft(Vendor="Acme Corp", Total=12), 1)

        assert _changed(service) == ["Total", "Vendor"]
        assert (first.changed_field_count, second.changed_field_count) == (1, 2)

    def test_a_field_changed_only_in_the_first_save_stays_listed(self, service):
        """The second save sends the first save's value again and changes
        nothing else: against the previous draft that would be no change."""
        _save(service, _draft(Total=12), None)

        again = _save(service, _draft(Total=12), 1)

        assert _changed(service) == ["Total"]
        assert again.changed_field_count == 1

    def test_a_value_put_back_is_no_longer_a_change(self, service):
        _save(service, _draft(Vendor="Acme Corp", Total=12), None)

        _save(service, _draft(Total=12), 1)

        assert _changed(service) == ["Total"]

    def test_a_list_row_is_named_by_its_path(self, service):
        _save(service, _draft(Lines=[{"Qty": 2}]), None)

        _save(service, _draft(Lines=[{"Qty": 2}, {"Qty": 7}]), 1)

        assert _changed(service) == ["Lines[0].Qty", "Lines[1].Qty"]

    def test_every_save_reads_the_section_result_and_never_a_draft(self, service, s3):
        _save(service, _draft(Total=12), None)
        _save(service, _draft(Total=13), 1)
        _save(service, _draft(Total=14), 2)

        read = [call.args[0] for call in s3.get_json_content.call_args_list]
        assert read == [RESULT_URI, RESULT_URI, RESULT_URI]


class TestDeletingADraftRecord:
    OTHER_CYCLE = "1" * 21

    def _put(self, store, section_id, cycle_id=CYCLE):
        draft = DraftRecord(
            doc_key=KEY,
            cycle_id=cycle_id,
            section_id=section_id,
            S3Key=f"{KEY}/review/drafts/{cycle_id}/{section_id}/000001.json",
            revision=1,
        )
        store.client.transact_write_items(
            [store.draft_txn_item(draft, expected_revision=None)]
        )

    def test_the_record_is_gone_and_the_others_stay(self, service):
        store = service.store
        self._put(store, "1")
        self._put(store, "2")
        self._put(store, "1", self.OTHER_CYCLE)

        store.delete_draft(KEY, CYCLE, "1")

        assert store.get_draft(KEY, CYCLE, "1") is None
        assert [d.section_id for d in store.list_drafts(KEY, CYCLE)] == ["2"]
        # The same section id in another cycle is another record.
        assert store.get_draft(KEY, self.OTHER_CYCLE, "1").revision == 1

    def test_the_item_itself_is_not_touched(self, service):
        store = service.store
        self._put(store, "1")
        before = store.get_item(KEY)

        store.delete_draft(KEY, CYCLE, "1")

        assert store.get_item(KEY) == before

    def test_a_record_that_is_not_there_is_not_an_error(self, service):
        service.store.delete_draft(KEY, CYCLE, "9")

        assert service.store.list_drafts(KEY, CYCLE) == []

    def test_a_section_saved_again_after_the_delete_starts_a_new_draft(self, service):
        _save(service, _draft(Total=12), None)
        service.store.delete_draft(KEY, CYCLE, "1")

        saved = _save(service, _draft(Vendor="Acme Corp"), None)

        assert saved.revision == 1
        assert _changed(service) == ["Vendor"]
