# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The consumer-facing ``review/status.json``: which fields each status carries, and
that its ``sequence`` is taken on the item before the object is written."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from idp_common.dynamodb.client import DynamoDBTransactionCanceledError
from idp_common.review_queues import status_object
from idp_common.review_queues.models import ReviewItemRecord, ReviewReason, RunRef
from idp_common.review_queues.statuses import ReviewStatus

NOW = datetime(2026, 10, 18, 12, 30, 0, 123000, tzinfo=timezone.utc)
MACHINE = RunRef(run_id="r1", manifest_uri="s3://out/doc/r1/manifest.json")
FINAL = RunRef(run_id="r2", manifest_uri="s3://out/doc/r2/manifest.json")


def _item(**overrides):
    fields = dict(
        doc_key="in/doc.pdf",
        cycle_id="c1",
        profile="profile-a",
        queue="default",
        state="finalizing",
        version=2,
        enqueued_at="2026-10-18T10:00:00.000Z",
        enqueued_at_ms=1,
        reasons=[
            ReviewReason(
                kind="low_confidence",
                section_id="1",
                field="A",
                score=0.1,
                threshold=0.8,
            )
        ],
        reason_count=1,
        sections=["1"],
        disposition="rejected",
        disposed_by="ann@example.com",
        disposed_at="2026-10-18T12:00:00.000Z",
        reject_reason="Illegible",
        reject_note="page 2 is blank",
        status_sequence=4,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _build(item, status):
    return status_object.build_status(
        item,
        status,
        sequence=5,
        run_id="r2",
        machine_ref=MACHINE,
        final_ref=FINAL,
        now=NOW,
    )


class _S3:
    def __init__(self):
        self.written = []
        self.deleted = []

    def write_content(self, body, bucket, key, content_type, metadata=None):
        self.written.append((body, bucket, key, metadata))

    def delete_content(self, bucket, key):
        self.deleted.append((bucket, key))


class _Store:
    def __init__(self, races=0, fresh=None):
        self.races = races
        self.fresh = fresh
        self.transitions = []

    def transition(self, doc_key, *, expected_version, set_):
        self.transitions.append((expected_version, set_))
        if self.races:
            self.races -= 1
            raise DynamoDBTransactionCanceledError("version moved")

    def get_item(self, doc_key):
        return self.fresh


@pytest.mark.unit
class TestBuildStatus:
    def test_in_review_carries_no_disposition_and_no_final_output(self):
        obj = _build(_item(), ReviewStatus.IN_REVIEW)
        assert obj.status == "in_review" and obj.sequence == 5
        assert obj.final_output is None and obj.disposition is None
        assert obj.reviewer is None and obj.reject_reason is None
        assert obj.timestamps.finalized_at is None
        assert obj.timestamps.updated_at == "2026-10-18T12:30:00.123Z"
        assert obj.machine_output == MACHINE
        assert [r.field for r in obj.reasons] == ["A"] and obj.sections_in_review == [
            "1"
        ]

    def test_complete_points_at_the_final_output(self):
        obj = _build(_item(disposition="complete"), ReviewStatus.COMPLETE)
        assert obj.final_output == FINAL
        assert obj.disposition == "complete" and obj.reviewer == "ann@example.com"
        assert obj.reject_reason is None and obj.reject_note is None
        assert obj.timestamps.finalized_at == "2026-10-18T12:30:00.123Z"

    def test_rejected_keeps_the_machine_output_and_says_why(self):
        obj = _build(_item(), ReviewStatus.REJECTED)
        assert obj.final_output is None
        assert obj.reject_reason == "Illegible" and obj.reject_note == "page 2 is blank"
        assert obj.timestamps.disposed_at == "2026-10-18T12:00:00.000Z"


@pytest.mark.unit
class TestPublish:
    def test_takes_the_next_sequence_under_the_item_version_then_writes(self):
        store, s3 = _Store(), _S3()
        obj = status_object.publish(
            store,
            s3,
            "out",
            _item(),
            ReviewStatus.IN_REVIEW,
            run_id="r1",
            machine_ref=MACHINE,
            final_ref=None,
            now=NOW,
        )
        assert store.transitions == [(2, {"StatusSequence": 5})]
        [(body, bucket, key, metadata)] = s3.written
        assert (bucket, key) == ("out", "in/doc.pdf/review/status.json")
        assert body["sequence"] == obj.sequence == 5
        assert metadata == {
            "review-status": "in_review",
            "review-cycle-id": "c1",
            "review-schema-version": status_object.SCHEMA_VERSION,
        }

    def test_a_version_race_rereads_the_item_and_retries(self):
        fresh = _item(version=3, status_sequence=6)
        store, s3 = _Store(races=1, fresh=fresh), _S3()
        obj = status_object.publish(
            store,
            s3,
            "out",
            _item(),
            ReviewStatus.IN_REVIEW,
            run_id="r1",
            machine_ref=MACHINE,
            final_ref=None,
            now=NOW,
        )
        assert store.transitions == [
            (2, {"StatusSequence": 5}),
            (3, {"StatusSequence": 7}),
        ]
        assert obj.sequence == 7

    def test_a_race_with_a_new_cycle_is_not_retried(self):
        store, s3 = _Store(races=1, fresh=_item(cycle_id="c2")), _S3()
        with pytest.raises(DynamoDBTransactionCanceledError):
            status_object.publish(
                store,
                s3,
                "out",
                _item(),
                ReviewStatus.IN_REVIEW,
                run_id="r1",
                machine_ref=MACHINE,
                final_ref=None,
                now=NOW,
            )
        assert s3.written == []

    def test_delete_removes_the_object_to_signal_withdrawal(self):
        s3 = _S3()
        status_object.delete(s3, "out", "in/doc.pdf")
        assert s3.deleted == [("out", "in/doc.pdf/review/status.json")]
