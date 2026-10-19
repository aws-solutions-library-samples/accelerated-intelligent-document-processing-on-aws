# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review reasons and the routing decision ProcessResults acts on.

``build_reasons`` turns stored confidence alerts into typed reasons, worst first;
``route_for_review`` decides whether a run holds for review, finalizes a disposed
item, or neither. Both are exercised with plain stand-ins for the document, the
configuration and the store, so each test names the decision it pins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from idp_common.review_queues import routing
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import DEFAULT_QUEUE_ID
from idp_common.review_queues.reasons import (
    MAX_REASONS,
    build_reasons,
    field_label,
)
from idp_common.review_queues.statuses import LIVE_ITEM_STATES, QueueItemState

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-1"


def _config(*, hitl=True, queues=True, queue="Default", threshold=0.8):
    return SimpleNamespace(
        classes=[],
        hitl=SimpleNamespace(
            enabled=hitl,
            confidence_threshold=threshold,
            review_queues=SimpleNamespace(enabled=queues, queue=queue),
        ),
    )


def _alert(name, score, threshold):
    return {
        "attribute_name": name,
        "confidence": score,
        "confidence_threshold": threshold,
    }


def _section(section_id, alerts=(), confidence=None, classification="W2"):
    return SimpleNamespace(
        section_id=section_id,
        classification=classification,
        confidence_threshold_alerts=list(alerts),
        confidence=confidence,
    )


def _document(*sections, **extra):
    fields = {
        "input_key": "in/doc.pdf",
        "sections": list(sections),
        "config_version": "profile-a",
        "config_revision": 3,
        "output_bucket": "",
        "submission_source": None,
        "test_set_id": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class _Store:
    """Records what routing writes; ``client`` has no methods, so the queue
    definitions read fails and routing falls back to the Default queue."""

    def __init__(self, item=None):
        self.item = item
        self.client = SimpleNamespace()
        self.holding = []
        self.history = []

    def get_item(self, doc_key):
        return self.item

    def put_holding(self, record, *, supersede=None, history=None):
        self.holding.append((record, supersede, history))

    def history_txn_item(self, doc_key, cycle_id, event, actor, details, expires, **kw):
        entry = (doc_key, cycle_id, event, details)
        self.history.append(entry)
        return entry


def _live_item(**overrides):
    live_state = sorted(s.value for s in LIVE_ITEM_STATES)[0]
    fields = dict(
        doc_key="in/doc.pdf",
        cycle_id="c-old",
        profile="profile-a",
        queue="Default",
        state=live_state,
        version=4,
        enqueued_at="2026-10-17T00:00:00.000Z",
        enqueued_at_ms=1,
        phase1_execution_arn="arn:aws:states:us-east-1:111122223333:execution:sm:run-0",
        expires_after=99,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


@pytest.mark.unit
class TestBuildReasons:
    def test_orders_reasons_worst_first_across_sections(self):
        doc = _document(
            _section("2", [_alert("Total", 0.70, 0.8)]),
            _section("1", [_alert("Name", 0.10, 0.8), _alert("Date", 0.75, 0.8)]),
        )
        reasons = build_reasons(doc, _config(), mode="pipeline")
        assert [(r.section_id, r.field) for r in reasons] == [
            ("1", "Name"),
            ("2", "Total"),
            ("1", "Date"),
        ]
        assert {r.kind for r in reasons} == {"low_confidence"}

    def test_skips_alerts_without_a_score_or_threshold(self):
        doc = _document(
            _section(
                "1",
                [_alert("A", None, 0.8), _alert("B", 0.5, None), _alert("C", "x", 0.8)],
            )
        )
        assert build_reasons(doc, _config(), mode="pipeline") == []

    def test_bda_mode_adds_a_blueprint_reason_below_the_threshold_only(self):
        doc = _document(_section("1", confidence=0.5), _section("2", confidence=0.95))
        reasons = build_reasons(doc, _config(threshold=0.8), mode="bda")
        assert [(r.kind, r.section_id, r.score, r.threshold) for r in reasons] == [
            ("blueprint_confidence", "1", 0.5, 0.8)
        ]
        # Pipeline mode never reads the blueprint confidence.
        assert build_reasons(doc, _config(), mode="pipeline") == []


@pytest.mark.unit
class TestFieldLabel:
    SCHEMA = {
        "properties": {
            "Address": {"properties": {"City": {"title": "Town or city"}}},
            "LineItems": {
                "type": "array",
                "items": {"properties": {"Amount": {"title": " Line amount "}}},
            },
        }
    }

    def test_uses_the_schema_title_through_objects_and_arrays(self):
        assert field_label(self.SCHEMA, "Address.City") == "Town or city"
        assert field_label(self.SCHEMA, "LineItems[2].Amount") == "Line amount"

    def test_humanises_the_last_segment_when_there_is_no_title(self):
        assert field_label(None, "employer_name") == "Employer name"
        assert field_label(None, "Address.PostalCode") == "Postal Code"
        assert field_label(self.SCHEMA, "Address.Street") == "Street"

    def test_returns_none_for_an_empty_name(self):
        assert field_label(self.SCHEMA, "") is None


@pytest.mark.unit
class TestReviewMode:
    def test_queues_only_when_hitl_and_queues_are_on_and_a_store_exists(self):
        store = _Store()
        assert routing.review_mode(_config(), store) == "queues"
        assert routing.review_mode(_config(queues=False), store) == "legacy"
        assert routing.review_mode(_config(hitl=False), store) == "legacy"
        assert routing.review_mode(_config(), None) == "legacy"
        assert routing.review_mode(None, store) == "legacy"

    def test_a_test_studio_document_is_never_queue_routed(self):
        store = _Store()
        assert (
            routing.review_mode(_config(), store, _document(test_set_id="ts1"))
            == "legacy"
        )
        studio = _document(submission_source=routing.TEST_STUDIO_SOURCE)
        assert routing.review_mode(_config(), store, studio) == "legacy"
        decision = routing.route_for_review(
            studio, _config(), mode="pipeline", execution_arn=ARN, store=store, now=NOW
        )
        assert decision.mode == "legacy" and store.holding == []


@pytest.mark.unit
class TestLegacyTriggers:
    def test_pipeline_trigger_needs_hitl_and_alerts(self):
        with_alerts = _section("1", [_alert("A", 0.1, 0.8)])
        assert routing.legacy_section_triggers(with_alerts, hitl_enabled=True) is True
        assert routing.legacy_section_triggers(with_alerts, hitl_enabled=False) is False
        assert (
            routing.legacy_section_triggers(_section("1"), hitl_enabled=True) is False
        )

    def test_bda_trigger_is_none_when_hitl_is_off(self):
        assert routing.bda_segment_triggers([], 0.1, 0.8, hitl_enabled=False) is None
        assert routing.bda_segment_triggers(["a"], 0.99, 0.8, hitl_enabled=True) is True
        assert routing.bda_segment_triggers([], 0.5, 0.8, hitl_enabled=True) is True
        assert routing.bda_segment_triggers([], 0.9, 0.8, hitl_enabled=True) is False


@pytest.mark.unit
class TestRouteForReview:
    def test_legacy_mode_does_no_io(self):
        store = _Store()
        decision = routing.route_for_review(
            _document(_section("1", [_alert("A", 0.1, 0.8)])),
            _config(queues=False),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            now=NOW,
        )
        assert decision.mode == "legacy"
        assert store.holding == []

    def test_no_reasons_means_no_hold(self):
        store = _Store()
        decision = routing.route_for_review(
            _document(_section("1")),
            _config(),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        assert decision.mode == "queues" and decision.hold is False
        assert store.holding == []

    def test_a_fresh_run_with_reasons_holds_in_the_default_queue(self):
        store = _Store()
        doc = _document(_section("1", [_alert("A", 0.1, 0.8), _alert("B", 0.7, 0.8)]))
        decision = routing.route_for_review(
            doc,
            _config(queue="Unknown"),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        assert decision.hold is True and decision.queue == DEFAULT_QUEUE_ID
        assert decision.sections == ["1"]
        [(record, supersede, history)] = store.holding
        assert supersede is None and history is None
        assert record.state == QueueItemState.HOLDING.value
        assert record.version == 1
        assert record.profile == "profile-a" and record.config_revision == 3
        assert record.phase1_execution_arn == ARN
        assert record.reason_count == 2 and [r.field for r in record.reasons] == [
            "A",
            "B",
        ]
        now_ms = int(NOW.timestamp() * 1000)
        assert record.sweep_at_ms == now_ms + routing.HOLDING_SWEEP_MS
        assert (
            record.expires_after == int(NOW.timestamp()) + routing.HOLDING_TTL_SECONDS
        )

    def test_reasons_kept_on_the_item_are_capped(self):
        store = _Store()
        alerts = [_alert(f"F{i:02d}", 0.1, 0.8) for i in range(MAX_REASONS + 5)]
        routing.route_for_review(
            _document(_section("1", alerts)),
            _config(),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        record = store.holding[0][0]
        assert len(record.reasons) == MAX_REASONS
        assert record.reason_count == MAX_REASONS + 5

    def test_a_new_run_supersedes_a_live_item_and_records_why(self):
        old = _live_item()
        store = _Store(item=old)
        decision = routing.route_for_review(
            _document(_section("1", [_alert("A", 0.1, 0.8)])),
            _config(),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        [(record, supersede, history)] = store.holding
        assert supersede is old
        assert record.version == old.version + 1
        assert record.cycle_id != old.cycle_id == "c-old"
        assert history == (
            "in/doc.pdf",
            "c-old",
            "superseded",
            {"by_cycle": decision.cycle_id},
        )

    def test_a_retry_of_the_same_execution_keeps_its_cycle(self):
        old = _live_item(phase1_execution_arn=ARN)
        store = _Store(item=old)
        decision = routing.route_for_review(
            _document(_section("1", [_alert("A", 0.1, 0.8)])),
            _config(),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        assert decision.cycle_id == "c-old"
        [(record, supersede, _)] = store.holding
        assert supersede is None and record.version == 1

    def test_the_finalize_run_of_a_rejected_item_is_recognised(self):
        item = _live_item(
            state=QueueItemState.FINALIZING.value,
            finalize_execution_name="run-1",
            finalize_token="tok",
            disposition="rejected",
            queue="Escalation",
        )
        store = _Store(item=item)
        decision = routing.route_for_review(
            _document(_section("1", [_alert("A", 0.1, 0.8)])),
            _config(),
            mode="pipeline",
            execution_arn=ARN,
            store=store,
            s3=object(),
            now=NOW,
        )
        assert decision.mode == "queues" and decision.outcome == "rejected"
        assert decision.cycle_id == "c-old" and decision.queue == "Escalation"
        # A finalize run never writes a new holding item.
        assert store.holding == []
