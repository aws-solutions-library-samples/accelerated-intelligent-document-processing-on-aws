# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue metering: phase 1 travels as the finalize run's baseline.

- The finalize message carries ``metering == {}``, so the finalize run starts
  with no usage of its own (pinned here; the run start moves whatever arrives
  into the baseline, which is then empty).
- ``apply_overrides`` puts the phase-1 snapshot into ``metering_baseline`` and
  leaves ``metering`` as the finalize run's own usage, so the tracking row
  (baseline + own) is cumulative and reporting counts phase 1 once.
- The hold snapshot stores the cumulative usage up to phase 1: a reprocessed
  document's baseline plus phase 1's own usage.
"""

import copy
import json
import logging
from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, Status
from idp_common.review_queues import tracker
from idp_common.review_queues.finalize import FinalizeSender
from idp_common.review_queues.models import ReviewItemRecord, TrackerReviewContext
from idp_common.review_queues.statuses import QueueItemState

CYCLE = "0" * 21
TOKEN = "rvw-" + "ab" * 16
PHASE1_ARN = "arn:aws:states:us-east-1:123456789012:execution:sm:phase1"

PHASE1 = {
    "OCR/textract/analyze_document": {"pages": 3},
    "Extraction/bedrock/model-a": {"inputTokens": 2000, "outputTokens": 300},
}
FINALIZE_OWN = {"Summarization/bedrock/model-b": {"inputTokens": 400}}


def _item(**overrides) -> ReviewItemRecord:
    fields = dict(
        doc_key="claim.pdf",
        cycle_id=CYCLE,
        profile="default",
        queue="claims",
        state=QueueItemState.HOLDING.value,
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=1,
        sections=["1"],
        version=1,
        phase1_execution_arn=PHASE1_ARN,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _document(metering, baseline=None) -> Document:
    doc = Document(
        id="claim.pdf",
        input_key="claim.pdf",
        status=Status.COMPLETED,
        metering=copy.deepcopy(metering),
    )
    doc.metering_baseline = copy.deepcopy(baseline or {})
    return doc


@pytest.mark.unit
class TestFinalizeMessage:
    def test_finalize_message_carries_no_usage_of_its_own(self):
        documents = MagicMock()
        documents.get_document.return_value = _document(PHASE1)
        sender = FinalizeSender(MagicMock(), "queue-url", None, documents)

        body, _ = sender.build_message(
            _item(state=QueueItemState.FINALIZING.value, finalize_token=TOKEN)
        )

        sent = json.loads(body)
        assert sent["metering"] == {}
        assert "metering_baseline" not in sent


@pytest.mark.unit
class TestApplyOverrides:
    def _ctx(self, phase1_json):
        return TrackerReviewContext(
            kind="finalize",
            item=_item(phase1_metering_json=phase1_json),
            token=TOKEN,
            outcome="complete",
        )

    def test_phase1_goes_into_the_baseline_and_own_usage_is_untouched(self):
        processed = _document(FINALIZE_OWN)

        tracked, reporting = tracker.apply_overrides(
            self._ctx(json.dumps(PHASE1)), processed
        )

        assert tracked.metering == FINALIZE_OWN
        assert tracked.metering_baseline == PHASE1
        assert reporting.metering == FINALIZE_OWN
        assert reporting.metering_baseline == {}
        assert reporting is not tracked

    def test_phase1_is_added_to_an_existing_baseline(self):
        processed = _document(
            FINALIZE_OWN, baseline={"Extraction/bedrock/model-a": {"inputTokens": 5}}
        )

        tracked, _ = tracker.apply_overrides(self._ctx(json.dumps(PHASE1)), processed)

        assert tracked.metering_baseline == {
            "OCR/textract/analyze_document": {"pages": 3},
            "Extraction/bedrock/model-a": {"inputTokens": 2005, "outputTokens": 300},
        }

    def test_unreadable_snapshot_leaves_the_baseline(self):
        processed = _document(FINALIZE_OWN)

        tracked, _ = tracker.apply_overrides(self._ctx("not json"), processed)

        assert tracked.metering == FINALIZE_OWN
        assert tracked.metering_baseline == {}


def _hold_snapshot(document):
    """Run the hold branch of ``after_completion``; return the stored snapshot."""
    item = _item()
    store = MagicMock()
    store.get_item.return_value = item
    tracker.after_completion(
        TrackerReviewContext(kind="hold", item=item),
        workflow_status="SUCCEEDED",
        run_ref=None,
        document=document,
        store=store,
        s3=MagicMock(),
        output_bucket="out",
        notifier=MagicMock(),
        document_service=MagicMock(),
        retention_days=30,
    )
    (active,) = store.replace_item.call_args.args
    return active.phase1_metering_json


@pytest.mark.unit
class TestHoldSnapshot:
    def test_snapshot_of_a_reprocessed_document_is_baseline_plus_own(self):
        earlier = {"OCR/textract/analyze_document": {"pages": 3}}
        own = {"Extraction/bedrock/model-a": {"inputTokens": 2000}}

        snapshot = _hold_snapshot(_document(own, baseline=earlier))

        assert json.loads(snapshot) == {**earlier, **own}

    def test_snapshot_over_the_limit_is_dropped_with_a_warning(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(tracker, "MAX_METERING_JSON", 10)

        with caplog.at_level(logging.WARNING):
            snapshot = _hold_snapshot(_document(PHASE1))

        assert snapshot is None
        assert "event=review_phase1_metering_too_large" in caplog.text
