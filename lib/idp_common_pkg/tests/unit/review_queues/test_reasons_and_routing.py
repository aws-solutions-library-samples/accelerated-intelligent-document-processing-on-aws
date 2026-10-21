# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review reasons and the routing decision ProcessResults acts on.

``build_reasons`` reads each section's extraction result and compares every field
confidence with the Default queue's threshold, worst first; ``route_for_review``
decides whether a run holds for review, finalizes a disposed item, or neither.
Both are exercised with plain stand-ins for the document, the S3 reads, the
queue definitions and the store, so each test names the decision it pins.
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
    UNAVAILABLE_LABEL,
    build_reasons,
    field_label,
)
from idp_common.review_queues.statuses import LIVE_ITEM_STATES, QueueItemState

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-1"


def _config(*, hitl=True, threshold=0.8):
    """A profile; routing reads neither ``hitl.enabled`` nor its threshold."""
    return SimpleNamespace(
        classes=[],
        hitl=SimpleNamespace(enabled=hitl, confidence_threshold=threshold),
    )


def _uri(section_id):
    return f"s3://out/in/doc.pdf/sections/{section_id}/result.json"


class _S3:
    """``get_json_content`` over in-memory results; a missing URI raises."""

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.reads = []

    def get_json_content(self, uri):
        self.reads.append(uri)
        if uri not in self.results:
            raise RuntimeError("NoSuchKey")
        return self.results[uri]


def _result(**fields):
    """A result.json whose explainability_info scores each field."""
    return {
        "inference_result": {name: "x" for name in fields},
        "explainability_info": [
            {
                name: {"value": "x", "confidence": score}
                for name, score in fields.items()
            }
        ],
    }


def _section(section_id, confidence=None, classification="W2", uri=True):
    return SimpleNamespace(
        section_id=section_id,
        classification=classification,
        confidence_threshold_alerts=[],
        confidence=confidence,
        extraction_result_uri=_uri(section_id) if uri else None,
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


def _default(enabled=True, threshold=0.8, priority=50):
    return QueueDefinition(
        id=DEFAULT_QUEUE_ID,
        enabled=enabled,
        confidence_threshold=threshold,
        priority=priority,
    )


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


def _route(doc, store, s3, mode="pipeline", config=None):
    return routing.route_for_review(
        doc,
        config or _config(),
        mode=mode,
        execution_arn=ARN,
        store=store,
        s3=s3,
        now=NOW,
    )


@pytest.mark.unit
class TestBuildReasons:
    def test_fields_below_the_threshold_are_reasons_worst_first(self):
        s3 = _S3(
            {
                _uri("2"): _result(Total=0.70, Ok=0.95),
                _uri("1"): _result(Name=0.10, Date=0.75),
            }
        )
        doc = _document(_section("2"), _section("1"))
        reasons = build_reasons(doc, _config(), threshold=0.8, mode="pipeline", s3=s3)
        assert [(r.section_id, r.field, r.score, r.threshold) for r in reasons] == [
            ("1", "Name", 0.10, 0.8),
            ("2", "Total", 0.70, 0.8),
            ("1", "Date", 0.75, 0.8),
        ]
        assert {r.kind for r in reasons} == {"low_confidence"}
        # Each section's result is read once.
        assert sorted(s3.reads) == [_uri("1"), _uri("2")]

    def test_the_queue_threshold_decides_not_the_profile_or_the_schema(self):
        result = _result(Total=0.85)
        # A per-field threshold stored beside the score is not consulted.
        result["explainability_info"][0]["Total"]["confidence_threshold"] = 0.99
        s3 = _S3({_uri("1"): result})
        doc = _document(_section("1"))
        config = _config(threshold=0.99)
        assert build_reasons(doc, config, threshold=0.8, mode="pipeline", s3=s3) == []
        [reason] = build_reasons(doc, config, threshold=0.9, mode="pipeline", s3=s3)
        assert (reason.field, reason.threshold) == ("Total", 0.9)

    def test_nested_and_table_fields_are_named_by_path(self):
        s3 = _S3(
            {
                _uri("1"): {
                    "explainability_info": [
                        {
                            "Address": {"City": {"confidence": 0.2}},
                            "Rows": [{"Amount": {"confidence": 0.3}}],
                        }
                    ]
                }
            }
        )
        reasons = build_reasons(
            _document(_section("1")), None, threshold=0.8, mode="pipeline", s3=s3
        )
        assert [(r.field, r.label) for r in reasons] == [
            ("Address.City", "City"),
            ("Rows[0].Amount", "Amount"),
        ]

    def test_an_unreadable_result_still_routes(self):
        s3 = _S3({_uri("1"): _result(Total=0.99)})
        doc = _document(_section("1"), _section("2"), _section("3", uri=False))
        reasons = build_reasons(doc, None, threshold=0.8, mode="pipeline", s3=s3)
        assert [(r.section_id, r.kind, r.field, r.label) for r in reasons] == [
            ("2", "low_confidence", None, UNAVAILABLE_LABEL),
            ("3", "low_confidence", None, UNAVAILABLE_LABEL),
        ]

    def test_a_result_without_confidences_gives_no_reason(self):
        s3 = _S3({_uri("1"): {"inference_result": {"A": "x"}}})
        assert (
            build_reasons(
                _document(_section("1")), None, threshold=0.8, mode="pipeline", s3=s3
            )
            == []
        )

    def test_bda_blueprint_confidence_is_compared_with_the_queue_threshold(self):
        s3 = _S3({_uri("1"): _result(A=0.99), _uri("2"): _result(A=0.99)})
        doc = _document(_section("1", confidence=0.85), _section("2", confidence=0.95))
        reasons = build_reasons(doc, None, threshold=0.9, mode="bda", s3=s3)
        assert [(r.kind, r.section_id, r.score, r.threshold) for r in reasons] == [
            ("blueprint_confidence", "1", 0.85, 0.9)
        ]
        assert build_reasons(doc, None, threshold=0.8, mode="bda", s3=s3) == []
        # Pipeline mode never reads the blueprint confidence.
        assert build_reasons(doc, None, threshold=0.9, mode="pipeline", s3=s3) == []


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
    def test_queues_when_a_store_exists_and_the_default_queue_is_enabled(self):
        assert routing.review_mode(_config(), _Store()) == "queues"
        # The profile's hitl.enabled no longer decides.
        assert routing.review_mode(_config(hitl=False), _Store()) == "queues"
        assert routing.review_mode(None, _Store()) == "queues"
        assert routing.review_mode(_config(), None) == "legacy"

    def test_a_disabled_default_queue_means_legacy(self):
        store = _Store(definitions=[_default(enabled=False)])
        assert routing.review_mode(_config(), store, _document()) == "legacy"

    def test_a_missing_default_is_seeded_disabled(self):
        store = _Store(definitions=[])
        assert routing.review_mode(_config(), store) == "legacy"
        assert [i["QueueId"] for i in store.defs_client.items] == [DEFAULT_QUEUE_ID]

    def test_unreadable_definitions_mean_legacy(self):
        store = _Store()
        store.queue_defs = routing.QueueDefStore(lambda: SimpleNamespace(), ttl=0)
        assert routing.review_mode(_config(), store) == "legacy"

    def test_a_finalizing_item_keeps_queue_mode_after_default_is_disabled(self):
        item = _live_item(state=QueueItemState.FINALIZING.value)
        store = _Store(item=item, definitions=[_default(enabled=False)])
        assert routing.review_mode(_config(), store, _document()) == "queues"

    def test_a_test_studio_document_is_never_queue_routed(self):
        store = _Store()
        assert (
            routing.review_mode(_config(), store, _document(test_set_id="ts1"))
            == "legacy"
        )
        studio = _document(submission_source=routing.TEST_STUDIO_SOURCE)
        assert routing.review_mode(_config(), store, studio) == "legacy"
        decision = _route(studio, store, _S3())
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


@pytest.mark.unit
class TestRouteForReview:
    def test_a_disabled_default_means_no_queueing(self):
        store = _Store(definitions=[_default(enabled=False)])
        s3 = _S3({_uri("1"): _result(A=0.1)})
        decision = _route(_document(_section("1")), store, s3)
        assert decision.mode == "legacy"
        assert store.holding == [] and s3.reads == []

    def test_no_field_below_the_threshold_means_no_hold(self):
        store = _Store()
        decision = _route(
            _document(_section("1")), store, _S3({_uri("1"): _result(A=0.95)})
        )
        assert decision.mode == "queues" and decision.hold is False
        assert store.holding == []

    def test_a_field_below_the_threshold_holds_in_the_default_queue(self):
        store = _Store(definitions=[_default(priority=70)])
        s3 = _S3({_uri("1"): _result(A=0.1, B=0.7, C=0.9)})
        decision = _route(_document(_section("1")), store, s3)
        assert decision.hold is True and decision.queue == DEFAULT_QUEUE_ID
        assert decision.sections == ["1"]
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

    def test_a_threshold_change_takes_effect(self):
        s3 = _S3({_uri("1"): _result(A=0.85)})
        low = _Store(definitions=[_default(threshold=0.8)])
        assert _route(_document(_section("1")), low, s3).hold is False
        high = _Store(definitions=[_default(threshold=0.9)])
        decision = _route(_document(_section("1")), high, s3)
        assert decision.hold is True
        assert [(r.field, r.threshold) for r in decision.reasons] == [("A", 0.9)]

    def test_an_unreadable_result_still_routes(self):
        store = _Store()
        decision = _route(_document(_section("1")), store, _S3())
        assert decision.hold is True
        [reason] = decision.reasons
        assert (reason.field, reason.label) == (None, UNAVAILABLE_LABEL)

    def test_bda_blueprint_confidence_against_the_queue_threshold(self):
        s3 = _S3({_uri("1"): _result(A=0.99)})
        doc = _document(_section("1", confidence=0.85))
        assert (
            _route(doc, _Store(definitions=[_default(threshold=0.8)]), s3, "bda").hold
            is False
        )
        decision = _route(doc, _Store(definitions=[_default(threshold=0.9)]), s3, "bda")
        assert decision.hold is True
        assert [r.kind for r in decision.reasons] == ["blueprint_confidence"]

    def test_reasons_kept_on_the_item_are_capped(self):
        store = _Store()
        fields = {f"F{i:02d}": 0.1 for i in range(MAX_REASONS + 5)}
        _route(_document(_section("1")), store, _S3({_uri("1"): _result(**fields)}))
        record = store.holding[0][0]
        assert len(record.reasons) == MAX_REASONS
        assert record.reason_count == MAX_REASONS + 5

    def test_a_new_run_supersedes_a_live_item_and_records_why(self):
        old = _live_item()
        store = _Store(item=old)
        decision = _route(
            _document(_section("1")), store, _S3({_uri("1"): _result(A=0.1)})
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
        decision = _route(
            _document(_section("1")), store, _S3({_uri("1"): _result(A=0.1)})
        )
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
        decision = _route(
            _document(_section("1")), store, _S3({_uri("1"): _result(A=0.1)})
        )
        assert decision.mode == "queues" and decision.outcome == outcome
        assert decision.cycle_id == "c-old" and decision.queue == "claims"
        # Only a completed review applies the reviewer's corrections.
        assert bool(applied) is (disposition == "complete")
        # A finalize run never writes a new holding item.
        assert store.holding == []


@pytest.mark.unit
class TestReasonEdges:
    def test_a_result_that_is_not_an_object_counts_as_unavailable(self):
        s3 = _S3({_uri("1"): ["not", "an", "object"]})
        [reason] = build_reasons(
            _document(_section("1")), None, threshold=0.8, mode="pipeline", s3=s3
        )
        assert reason.label == UNAVAILABLE_LABEL

    @pytest.mark.parametrize("confidence", [False, "high", None])
    def test_an_unusable_blueprint_confidence_gives_no_reason(self, confidence):
        s3 = _S3({_uri("1"): _result(A=0.99)})
        doc = _document(_section("1", confidence=confidence))
        assert build_reasons(doc, None, threshold=0.9, mode="bda", s3=s3) == []

    def test_results_are_read_through_idp_common_s3_by_default(self, monkeypatch):
        import idp_common.s3 as s3_module

        reads = []
        monkeypatch.setattr(
            s3_module,
            "get_json_content",
            lambda uri: reads.append(uri) or _result(A=0.1),
        )
        [reason] = build_reasons(
            _document(_section("1")), None, threshold=0.8, mode="pipeline"
        )
        assert (reason.field, reads) == ("A", [_uri("1")])

    def test_an_array_whose_items_are_not_a_schema_humanises_the_name(self):
        schema = {"properties": {"Rows": {"type": "array", "items": "x"}}}
        assert field_label(schema, "Rows[0].unit_price") == "Unit price"
