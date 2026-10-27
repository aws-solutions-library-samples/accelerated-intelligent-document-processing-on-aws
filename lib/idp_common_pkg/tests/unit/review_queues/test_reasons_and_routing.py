# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review reasons and the routing decision ProcessResults acts on.

``build_reasons`` turns each section's confidence alerts (and, in BDA mode, a
blueprint confidence under ``hitl.confidence_threshold``) into reasons, worst
first; ``route_for_review`` decides whether a run holds for review, finalizes a
disposed item, or neither. Both are exercised with plain stand-ins for the
document, the queue definitions and the store, so each test names the decision
it pins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from idp_common.review_queues import routing
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import DEFAULT_QUEUE_ID, QueueDefinition
from idp_common.review_queues.reasons import (
    MAX_REASONS,
    build_reasons,
    field_label,
)
from idp_common.review_queues.statuses import LIVE_ITEM_STATES, QueueItemState

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-1"


def _config(*, hitl=True, threshold=0.8):
    """A profile: ``hitl.enabled`` gates queue mode, its threshold scores BDA."""
    return SimpleNamespace(
        classes=[],
        hitl=SimpleNamespace(enabled=hitl, confidence_threshold=threshold),
    )


class _S3:
    """Records reads; routing must not read section results any more."""

    def __init__(self):
        self.reads = []

    def get_json_content(self, uri):
        self.reads.append(uri)
        raise AssertionError(f"routing read {uri}")


def _alert(name, confidence, threshold=0.8):
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def _section(section_id, *alerts, confidence=None, classification="W2"):
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


class _DefsClient:
    """The QDEF partition of HITLTable, in memory."""

    def __init__(self, definitions):
        self.items = [d.to_item() for d in definitions]

    def query_pages(self, *args, **kwargs):
        yield {"Items": list(self.items)}

    def put_item(self, item, condition_expression=None):
        if any(i["SK"] == item["SK"] for i in self.items):
            raise RuntimeError("ConditionalCheckFailed")
        self.items.append(item)


def _default(enabled=True, priority=50):
    return QueueDefinition(id=DEFAULT_QUEUE_ID, enabled=enabled, priority=priority)


class _Store:
    """Records what routing writes; ``queue_defs`` holds the given definitions."""

    def __init__(self, item=None, definitions=None):
        self.item = item
        self.client = SimpleNamespace()
        self.defs_client = _DefsClient(
            [_default()] if definitions is None else definitions
        )
        # The class routing itself checks for: another suite may have reloaded
        # the package, and an instance of a stale class would not be recognised.
        self.queue_defs = routing.QueueDefStore(lambda: self.defs_client, ttl=0)
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

    def list_drafts(self, doc_key, cycle_id):
        return []


def _live_item(**overrides):
    live_state = sorted(s.value for s in LIVE_ITEM_STATES)[0]
    fields = dict(
        doc_key="in/doc.pdf",
        cycle_id="c-old",
        profile="profile-a",
        queue="default",
        state=live_state,
        version=4,
        enqueued_at="2026-10-17T00:00:00.000Z",
        enqueued_at_ms=1,
        phase1_execution_arn="arn:aws:states:us-east-1:111122223333:execution:sm:run-0",
        expires_after=99,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _route(doc, store, s3=None, mode="pipeline", config=None):
    return routing.route_for_review(
        doc,
        config or _config(),
        mode=mode,
        execution_arn=ARN,
        store=store,
        s3=s3 if s3 is not None else _S3(),
        now=NOW,
    )


@pytest.mark.unit
class TestBuildReasons:
    def test_alerts_are_reasons_worst_first_with_their_own_threshold(self):
        doc = _document(
            _section("2", _alert("Total", 0.70)),
            _section("1", _alert("Name", 0.10), _alert("Date", 0.85, 0.9)),
        )
        reasons = build_reasons(doc, _config(), mode="pipeline")
        assert [(r.section_id, r.field, r.score, r.threshold) for r in reasons] == [
            ("1", "Name", 0.10, 0.8),
            ("2", "Total", 0.70, 0.8),
            ("1", "Date", 0.85, 0.9),
        ]
        assert {r.kind for r in reasons} == {"low_confidence"}

    def test_the_alert_threshold_decides(self):
        # A per-field threshold of 0.95 with a score of 0.9 gives a reason.
        [reason] = build_reasons(
            _document(_section("1", _alert("Total", 0.9, 0.95))),
            _config(threshold=0.5),
            mode="pipeline",
        )
        assert (reason.field, reason.score, reason.threshold) == ("Total", 0.9, 0.95)
        # A score at or above its alert threshold gives none.
        assert (
            build_reasons(
                _document(_section("1", _alert("Total", 0.96, 0.95))),
                _config(),
                mode="pipeline",
            )
            == []
        )

    def test_nested_and_table_fields_are_named_by_path(self):
        doc = _document(
            _section("1", _alert("Address.City", 0.2), _alert("Rows[0].Amount", 0.3))
        )
        reasons = build_reasons(doc, None, mode="pipeline")
        assert [(r.field, r.label) for r in reasons] == [
            ("Address.City", "City"),
            ("Rows[0].Amount", "Amount"),
        ]

    def test_a_section_with_no_alerts_gives_no_reason(self):
        assert build_reasons(_document(_section("1")), None, mode="pipeline") == []

    def test_bda_blueprint_confidence_is_compared_with_the_hitl_threshold(self):
        doc = _document(_section("1", confidence=0.85), _section("2", confidence=0.95))
        reasons = build_reasons(doc, _config(threshold=0.9), mode="bda")
        assert [(r.kind, r.section_id, r.score, r.threshold) for r in reasons] == [
            ("blueprint_confidence", "1", 0.85, 0.9)
        ]
        assert build_reasons(doc, _config(threshold=0.8), mode="bda") == []
        # Pipeline mode never reads the blueprint confidence.
        assert build_reasons(doc, _config(threshold=0.9), mode="pipeline") == []

    def test_bda_without_a_hitl_threshold_uses_the_default(self):
        doc = _document(_section("1", confidence=0.75))
        [reason] = build_reasons(doc, None, mode="bda")
        assert (reason.kind, reason.threshold) == ("blueprint_confidence", 0.8)


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
    def test_queues_when_hitl_is_on_and_the_default_queue_is_enabled(self):
        assert routing.review_mode(_config(), _Store()) == "queues"
        assert routing.review_mode(_config(), None) == "legacy"

    def test_hitl_off_means_legacy_even_with_default_enabled(self):
        assert routing.review_mode(_config(hitl=False), _Store()) == "legacy"
        assert routing.review_mode(None, _Store()) == "legacy"

    def test_hitl_on_with_default_disabled_means_legacy(self):
        store = _Store(definitions=[_default(enabled=False)])
        assert routing.review_mode(_config(), store, _document()) == "legacy"

    def test_a_missing_default_is_seeded_enabled(self):
        store = _Store(definitions=[])
        assert routing.review_mode(_config(), store) == "queues"
        assert [(i["QueueId"], i["Enabled"]) for i in store.defs_client.items] == [
            (DEFAULT_QUEUE_ID, True)
        ]

    def test_unreadable_definitions_mean_legacy(self):
        store = _Store()
        store.queue_defs = routing.QueueDefStore(lambda: SimpleNamespace(), ttl=0)
        assert routing.review_mode(_config(), store) == "legacy"

    def test_a_finalizing_item_keeps_queue_mode_after_default_is_disabled(self):
        item = _live_item(state=QueueItemState.FINALIZING.value)
        store = _Store(item=item, definitions=[_default(enabled=False)])
        assert routing.review_mode(_config(), store, _document()) == "queues"

    def test_a_finalizing_item_keeps_queue_mode_with_hitl_off(self):
        item = _live_item(state=QueueItemState.FINALIZING.value)
        store = _Store(item=item)
        assert routing.review_mode(_config(hitl=False), store, _document()) == "queues"

    def test_a_test_studio_document_is_never_queue_routed(self):
        store = _Store()
        assert (
            routing.review_mode(_config(), store, _document(test_set_id="ts1"))
            == "legacy"
        )
        studio = _document(submission_source=routing.TEST_STUDIO_SOURCE)
        assert routing.review_mode(_config(), store, studio) == "legacy"
        decision = _route(studio, store)
        assert decision.mode == "legacy" and store.holding == []


@pytest.mark.unit
class TestLegacyTriggers:
    def test_pipeline_trigger_needs_hitl_and_alerts(self):
        with_alerts = _section("1")
        with_alerts.confidence_threshold_alerts = [
            {"attribute_name": "A", "confidence": 0.1, "confidence_threshold": 0.8}
        ]
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

    def test_bda_trigger_routes_an_unscored_blueprint_to_review(self):
        """``matched_blueprint.confidence`` read as null or non-numeric is an
        unscored match: it triggers review instead of raising TypeError."""
        assert routing.bda_segment_triggers([], None, 0.8, hitl_enabled=True) is True
        assert routing.bda_segment_triggers([], "n/a", 0.8, hitl_enabled=True) is True
        assert routing.bda_segment_triggers([], "0.9", 0.8, hitl_enabled=True) is False
        assert routing.bda_segment_triggers([], 0.8, 0.8, hitl_enabled=True) is False
        assert routing.bda_segment_triggers([], None, 0.8, hitl_enabled=False) is None


@pytest.mark.unit
class TestRouteForReview:
    def test_a_disabled_default_means_no_queueing(self):
        store = _Store(definitions=[_default(enabled=False)])
        decision = _route(_document(_section("1", _alert("A", 0.1))), store)
        assert decision.mode == "legacy"
        assert store.holding == []

    def test_hitl_off_means_no_queueing(self):
        store = _Store()
        decision = _route(
            _document(_section("1", _alert("A", 0.1))),
            store,
            config=_config(hitl=False),
        )
        assert decision.mode == "legacy" and store.holding == []

    def test_no_alert_means_no_hold(self):
        store = _Store()
        decision = _route(_document(_section("1")), store)
        assert decision.mode == "queues" and decision.hold is False
        assert store.holding == []

    def test_an_alert_holds_in_the_default_queue_without_reading_s3(self):
        store = _Store(definitions=[_default(priority=70)])
        s3 = _S3()
        decision = _route(
            _document(_section("1", _alert("A", 0.1), _alert("B", 0.7))), store, s3
        )
        assert decision.hold is True and decision.queue == DEFAULT_QUEUE_ID
        assert decision.sections == ["1"]
        assert s3.reads == []
        [(record, supersede, history)] = store.holding
        assert supersede is None and history is None
        assert record.queue == DEFAULT_QUEUE_ID and record.priority == 70
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

    def test_a_per_field_threshold_routes_the_document(self):
        decision = _route(_document(_section("1", _alert("A", 0.9, 0.95))), _Store())
        assert decision.hold is True
        assert [(r.field, r.threshold) for r in decision.reasons] == [("A", 0.95)]

    def test_bda_blueprint_confidence_against_the_hitl_threshold(self):
        doc = _document(_section("1", confidence=0.85))
        assert (
            _route(doc, _Store(), mode="bda", config=_config(threshold=0.8)).hold
            is False
        )
        decision = _route(doc, _Store(), mode="bda", config=_config(threshold=0.9))
        assert decision.hold is True
        assert [r.kind for r in decision.reasons] == ["blueprint_confidence"]

    def test_hitl_off_finalizing_item_on_a_fresh_run_does_not_hold(self):
        item = _live_item(
            state=QueueItemState.FINALIZING.value,
            finalize_execution_name="another-run",
        )
        store = _Store(item=item)
        decision = _route(
            _document(_section("1", _alert("A", 0.1))),
            store,
            config=_config(hitl=False),
        )
        assert decision.mode == "queues" and decision.hold is False
        assert store.holding == []

    def test_reasons_kept_on_the_item_are_capped(self):
        store = _Store()
        alerts = [_alert(f"F{i:02d}", 0.1) for i in range(MAX_REASONS + 5)]
        _route(_document(_section("1", *alerts)), store)
        record = store.holding[0][0]
        assert len(record.reasons) == MAX_REASONS
        assert record.reason_count == MAX_REASONS + 5

    def test_a_new_run_supersedes_a_live_item_and_records_why(self):
        old = _live_item()
        store = _Store(item=old)
        decision = _route(_document(_section("1", _alert("A", 0.1))), store)
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
        decision = _route(_document(_section("1", _alert("A", 0.1))), store)
        assert decision.cycle_id == "c-old"
        [(record, supersede, _)] = store.holding
        assert supersede is None and record.version == 1

    @pytest.mark.parametrize(
        ("disposition", "outcome"),
        [("rejected", "rejected"), ("removed", "complete"), ("complete", "complete")],
    )
    def test_the_finalize_run_of_a_disposed_item_is_recognised(
        self, disposition, outcome, monkeypatch
    ):
        applied = []
        monkeypatch.setattr(
            routing, "apply_corrections", lambda *a: applied.append(a) or 0
        )
        item = _live_item(
            state=QueueItemState.FINALIZING.value,
            finalize_execution_name="run-1",
            finalize_token="tok",
            disposition=disposition,
            queue="claims",
        )
        store = _Store(item=item, definitions=[_default(enabled=False)])
        store.client = SimpleNamespace(update_item=lambda *a, **k: None)
        decision = _route(_document(_section("1", _alert("A", 0.1))), store)
        assert decision.mode == "queues" and decision.outcome == outcome
        assert decision.cycle_id == "c-old" and decision.queue == "claims"
        # Only a completed review applies the reviewer's corrections.
        assert bool(applied) is (disposition == "complete")
        # A finalize run never writes a new holding item.
        assert store.holding == []


@pytest.mark.unit
class TestReasonEdges:
    def test_a_malformed_alert_is_skipped(self):
        doc = _document(
            _section(
                "1",
                "not an alert",
                {"attribute_name": "A", "confidence_threshold": 0.8},
                {"attribute_name": "B", "confidence": 0.1},
                {
                    "attribute_name": "C",
                    "confidence": True,
                    "confidence_threshold": 0.8,
                },
                {"attributeName": "D", "confidence": 0.2, "confidenceThreshold": 0.8},
            )
        )
        assert [r.field for r in build_reasons(doc, None, mode="pipeline")] == ["D"]

    @pytest.mark.parametrize("confidence", [False, "high", None])
    def test_an_unusable_blueprint_confidence_gives_no_reason(self, confidence):
        doc = _document(_section("1", confidence=confidence))
        assert build_reasons(doc, _config(threshold=0.9), mode="bda") == []

    def test_reasons_never_read_section_results(self, monkeypatch):
        import idp_common.s3 as s3_module

        def boom(uri):
            raise AssertionError(f"build_reasons read {uri}")

        monkeypatch.setattr(s3_module, "get_json_content", boom)
        [reason] = build_reasons(
            _document(_section("1", _alert("A", 0.1))), None, mode="pipeline"
        )
        assert reason.field == "A"

    def test_an_array_whose_items_are_not_a_schema_humanises_the_name(self):
        schema = {"properties": {"Rows": {"type": "array", "items": "x"}}}
        assert field_label(schema, "Rows[0].unit_price") == "Unit price"
