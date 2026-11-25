# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The tracker and a reviewed document that a routing rule places in a queue again.

The run that follows a completed review carries a finalize token. When a routing
rule holds the document again, that run ends at the review point with a new
``HOLDING`` item. The tracker must treat it as a hold: activate the new item,
carry the usage of the runs before it, and say on the tracking row which queue
and which rule.
"""

import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, Status
from idp_common.review_queues import tracker
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import (
    PolicyFailure,
    ReviewItemRecord,
    ReviewReason,
    RoutedBy,
    TrackerReviewContext,
)
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.statuses import TRACKING_REVIEW_ATTRIBUTES
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.utils.metering import cumulative_metering

from ._table import REGION

pytestmark = pytest.mark.unit

KEY = "claims/packet.pdf"
CYCLE = "1" * 21
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:finalize-run"
TOKEN = "rvw-" + "ab" * 16
NOW = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)

PHASE1 = {
    "OCR/textract/analyze_document": {"pages": 3},
    "Extraction/bedrock/model-a": {"inputTokens": 2000, "outputTokens": 300},
}
FINALIZE_OWN = {"Summarization/bedrock/model-b": {"inputTokens": 400}}

HELD = {"document": {"status": Status.HITL_IN_PROGRESS.value}}
COMPLETED = {"document": {"status": Status.COMPLETED.value}}
WITH_TOKEN = {"document": {}, "review": {"finalizeToken": TOKEN}}
WITHOUT_TOKEN = {"document": {}}

ROUTED_BY = RoutedBy(
    table_id="review-routing",
    rule_id="second-opinion",
    rule_label="Second opinion",
    profile="lending",
    outcome_queue="high-value",
    placed_queue="high-value",
)


@pytest.fixture
def store(review_table):
    review_store = ReviewStore(review_table, region=REGION)
    review_store.queue_defs.put(
        QueueDefinition(id="high-value", name="High value", priority=80), create=True
    )
    return review_store


def _item(**overrides) -> ReviewItemRecord:
    fields = dict(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="lending",
        queue="high-value",
        state="HOLDING",
        version=6,
        priority=80,
        enqueued_at="2026-11-16T09:59:00.000Z",
        enqueued_at_ms=int(time.time() * 1000),
        sections=["1"],
        phase1_execution_arn=ARN,
        sweep_at_ms=int(time.time() * 1000) + 600_000,
        routed_by=ROUTED_BY,
        placement_count=2,
        carried_metering_json=json.dumps(PHASE1),
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _seed(store, **overrides):
    record = _item(**overrides)
    store.client.put_item(item_to_dynamo(record))
    return record


def _document(metering, baseline=None) -> Document:
    document = Document(
        id=KEY,
        input_key=KEY,
        status=Status.HITL_IN_PROGRESS,
        metering=copy.deepcopy(metering),
    )
    document.metering_baseline = copy.deepcopy(baseline or {})
    return document


def _activate(store, ctx, documents=None, document=None):
    documents = documents if documents is not None else MagicMock()
    tracker.after_completion(
        ctx,
        workflow_status="SUCCEEDED",
        run_ref=None,
        document=document if document is not None else _document(FINALIZE_OWN),
        store=store,
        s3=MagicMock(),
        output_bucket="out",
        notifier=MagicMock(),
        document_service=documents,
        retention_days=30,
        now=NOW,
    )
    return documents


class TestClassifyRun:
    def test_a_finalize_run_that_ends_held_is_a_hold(self):
        assert tracker.classify_run(WITH_TOKEN, HELD, "SUCCEEDED") == "hold"

    def test_a_plain_finalize_run_is_still_a_finalize(self):
        assert tracker.classify_run(WITH_TOKEN, COMPLETED, "SUCCEEDED") == "finalize"

    @pytest.mark.parametrize("status", ["FAILED", "ABORTED", "TIMED_OUT"])
    def test_a_finalize_run_that_did_not_succeed_is_a_finalize(self, status):
        """Its output may still say held from an earlier state; the run did not
        reach the review point, so the sweeper's retry must see a finalize."""
        assert tracker.classify_run(WITH_TOKEN, HELD, status) == "finalize"

    def test_a_plain_hold_is_unchanged(self):
        assert tracker.classify_run(WITHOUT_TOKEN, HELD, "SUCCEEDED") == "hold"

    def test_a_run_with_neither_is_none(self):
        assert tracker.classify_run(WITHOUT_TOKEN, COMPLETED, "SUCCEEDED") == "none"
        assert tracker.classify_run(WITHOUT_TOKEN, HELD, "FAILED") == "none"


class TestTheSecondPlacementIsActivated:
    def test_the_holding_item_of_this_run_is_found_and_opened(self, store):
        _seed(store)
        kind = tracker.classify_run(WITH_TOKEN, HELD, "SUCCEEDED")

        ctx = tracker.load_context(kind, TOKEN, KEY, ARN, store)
        assert ctx is not None and ctx.kind == "hold"
        _activate(store, ctx)

        item = store.get_item(KEY)
        assert (item.state, item.queue, item.cycle_id) == ("OPEN", "high-value", CYCLE)
        assert item.placement_count == 2
        assert item.activated_at == "2026-11-16T10:00:00.000Z"

    def test_a_holding_item_of_another_run_is_not_this_ones(self, store):
        _seed(store, phase1_execution_arn="arn:another-run")

        assert tracker.load_context("hold", TOKEN, KEY, ARN, store) is None


class TestCarriedMetering:
    def _ctx(self, **overrides):
        return TrackerReviewContext(kind="hold", item=_item(**overrides))

    def test_carried_metering(self):
        """After the second placement the tracking row holds phase 1 plus the
        finalize run, and reporting gets the finalize run's own usage only."""
        processed = _document(FINALIZE_OWN)

        tracked, reporting = tracker.apply_overrides(self._ctx(), processed)

        assert tracked.metering == FINALIZE_OWN
        assert tracked.metering_baseline == PHASE1
        assert cumulative_metering(tracked) == {**PHASE1, **FINALIZE_OWN}
        assert reporting is not tracked
        assert reporting.metering == FINALIZE_OWN
        assert reporting.metering_baseline == {}
        assert tracked.hitl_status == reporting.hitl_status == "in_review"

    def test_the_carried_usage_is_added_to_a_baseline_the_run_already_has(self):
        processed = _document(
            FINALIZE_OWN, baseline={"Extraction/bedrock/model-a": {"inputTokens": 5}}
        )

        tracked, _ = tracker.apply_overrides(self._ctx(), processed)

        assert tracked.metering_baseline == {
            "OCR/textract/analyze_document": {"pages": 3},
            "Extraction/bedrock/model-a": {"inputTokens": 2005, "outputTokens": 300},
        }

    def test_the_activated_item_snapshots_the_whole_usage(self, store):
        """The next finalize run carries this snapshot, so it must hold the usage
        before the first review as well as this run's."""
        _seed(store)
        ctx = tracker.load_context("hold", TOKEN, KEY, ARN, store)
        tracked, _ = tracker.apply_overrides(ctx, _document(FINALIZE_OWN))

        _activate(store, ctx, document=tracked)

        snapshot = json.loads(store.get_item(KEY).phase1_metering_json)
        assert snapshot == {**PHASE1, **FINALIZE_OWN}

    def test_a_first_placement_carries_nothing(self):
        processed = _document(FINALIZE_OWN)

        tracked, reporting = tracker.apply_overrides(
            self._ctx(carried_metering_json=None, placement_count=1), processed
        )

        # A plain hold: one document, untouched baseline.
        assert tracked is reporting is processed
        assert tracked.metering_baseline == {}

    def test_unreadable_carried_usage_leaves_the_baseline(self, caplog):
        processed = _document(FINALIZE_OWN)

        with caplog.at_level("WARNING"):
            tracked, _ = tracker.apply_overrides(
                self._ctx(carried_metering_json="not json"), processed
            )

        assert tracked.metering_baseline == {}
        assert "event=review_carried_metering_unreadable" in caplog.text


class TestTheTrackingAttributes:
    def _attributes(self, documents):
        call = documents.update_review_attributes.call_args
        return call.args[1], call.kwargs["remove_attrs"]

    def test_the_queue_name_and_the_rule_are_written_at_activation(self, store):
        _seed(store)
        ctx = tracker.load_context("hold", None, KEY, ARN, store)

        set_attrs, removed = self._attributes(_activate(store, ctx))

        assert set_attrs["ReviewQueue"] == "high-value"
        assert set_attrs["ReviewQueueName"] == "High value"
        assert set_attrs["ReviewRoutedBy"] == {
            "tableId": "review-routing",
            "ruleId": "second-opinion",
            "ruleLabel": "Second opinion",
            # An item placed before routing rules had a reason has none.
            "reason": "",
            "policyFailures": [],
            "policyFailureCount": 0,
        }
        assert "ReviewRoutedBy" not in removed

    def test_the_reason_and_the_failed_policy_rules_are_written_with_the_rule(
        self, store
    ):
        """The document status has them without a second read: the lookup
        function, the SDK and the CLI read this map."""
        routed_by = ROUTED_BY.model_copy(
            update={
                "reason": "Total 12000 is over 10000",
                "reason_template": "Total {field_value:Invoice:Total} is over 10000",
                "reason_source": "admin",
                "policy_failures": [
                    PolicyFailure(
                        rule_id="lending.max_ltv",
                        policy="lending",
                        name="max_ltv",
                        reason="LTV is 92%, over the 80% limit.",
                        pages=[3, 4],
                    )
                ],
                "policy_failure_count": 14,
            }
        )
        _seed(store, routed_by=routed_by)
        ctx = tracker.load_context("hold", None, KEY, ARN, store)

        set_attrs, _ = self._attributes(_activate(store, ctx))

        assert set_attrs["ReviewRoutedBy"] == {
            "tableId": "review-routing",
            "ruleId": "second-opinion",
            "ruleLabel": "Second opinion",
            "reason": "Total 12000 is over 10000",
            "policyFailures": [
                {
                    "ruleId": "lending.max_ltv",
                    "policy": "lending",
                    "name": "max_ltv",
                    "reason": "LTV is 92%, over the 80% limit.",
                    "pages": [3, 4],
                }
            ],
            "policyFailureCount": 14,
        }
        # What the row holds is what the reviewer reads, not the text as written.
        assert "field_value" not in json.dumps(set_attrs["ReviewRoutedBy"])

    def test_the_map_is_one_the_tracking_table_stores(self, store):
        """No float and no ``None``: DynamoDB takes the map as it is built."""
        from boto3.dynamodb.types import TypeSerializer

        routed_by = ROUTED_BY.model_copy(
            update={
                "reason": "r",
                "policy_failures": [PolicyFailure(rule_id="p.a", pages=[1])],
                "policy_failure_count": 1,
            }
        )
        _seed(store, routed_by=routed_by)
        ctx = tracker.load_context("hold", None, KEY, ARN, store)

        set_attrs, _ = self._attributes(_activate(store, ctx))

        stored = TypeSerializer().serialize(set_attrs["ReviewRoutedBy"])
        assert stored["M"]["policyFailureCount"] == {"N": "1"}
        assert stored["M"]["policyFailures"]["L"][0]["M"]["pages"] == {
            "L": [{"N": "1"}]
        }

    def test_an_item_no_rule_placed_clears_an_earlier_rule(self, store):
        _seed(store, routed_by=None, carried_metering_json=None, placement_count=1)
        ctx = tracker.load_context("hold", None, KEY, ARN, store)

        set_attrs, removed = self._attributes(_activate(store, ctx))

        assert "ReviewRoutedBy" not in set_attrs
        assert "ReviewRoutedBy" in removed
        assert set_attrs["ReviewQueueName"] == "High value"

    def test_a_queue_whose_definition_is_gone_is_named_by_the_default(self, store):
        """The item is activated in the Default queue, and named after it."""
        _seed(store, queue="deleted-queue")
        ctx = tracker.load_context("hold", None, KEY, ARN, store)

        set_attrs, _ = self._attributes(_activate(store, ctx))

        assert (set_attrs["ReviewQueue"], set_attrs["ReviewQueueName"]) == (
            "default",
            "Default",
        )

    def test_definitions_that_cannot_be_read_name_the_queue_by_its_id(self):
        failing = MagicMock()
        failing.queue_defs = None
        failing.client.query_pages.side_effect = RuntimeError("throttled")

        assert tracker._queue_name(failing, "high-value") == "high-value"

    def test_both_attributes_are_removed_when_the_review_is_withdrawn(self, store):
        assert {"ReviewQueueName", "ReviewRoutedBy"} <= set(TRACKING_REVIEW_ATTRIBUTES)
        _seed(store, state="OPEN", sweep_at_ms=None)
        documents = MagicMock()
        service = ReviewQueueService(
            store,
            QueueDirectory(store.queue_defs),
            notifier=MagicMock(),
            finalize_sender=MagicMock(),
            s3=MagicMock(),
            output_bucket="out",
            document_service=documents,
        )

        service.withdraw(KEY, "reprocessed")

        removed = documents.update_review_attributes.call_args.kwargs["remove_attrs"]
        assert {"ReviewQueueName", "ReviewRoutedBy"} <= set(removed)


class TestReportingRoutingReason:
    """Reporting gets the reason as written: it is read across profiles, so it
    holds no value of the document."""

    def _info(self, kind="hold", **overrides):
        return tracker.reporting_info(
            TrackerReviewContext(kind=kind, item=_item(**overrides), outcome="complete")
        )

    @pytest.mark.parametrize("kind", ["hold", "finalize"])
    def test_it_is_the_reason_as_written(self, kind):
        routed_by = ROUTED_BY.model_copy(
            update={
                "reason": "Total 12000 is over 10000",
                "reason_template": "Total {field_value:Invoice:Total} is over 10000",
                "reason_source": "admin",
            }
        )

        info = self._info(kind, routed_by=routed_by)

        assert info.routing_reason == "Total {field_value:Invoice:Total} is over 10000"
        assert "12000" not in info.model_dump_json()

    def test_a_generated_reason_is_not_reported(self):
        """The fallback is built from the document's values: nothing was written."""
        routed_by = ROUTED_BY.model_copy(
            update={
                "reason": "Second opinion: Reviewed true (found yes)",
                "reason_template": "",
                "reason_source": "fallback",
            }
        )

        assert self._info(routed_by=routed_by).routing_reason is None

    def test_an_item_no_routing_rule_placed_has_none(self):
        assert self._info(routed_by=None).routing_reason is None
        # An item placed before routing rules had a reason.
        assert self._info().routing_reason is None


class TestReportingTopReason:
    def _info(self, **overrides):
        return tracker.reporting_info(
            TrackerReviewContext(kind="hold", item=_item(**overrides))
        )

    def test_reporting_top_reason(self):
        reason = ReviewReason(
            kind="missing_required",
            section_id="1",
            field="Vendor",
            score=0.0,
            threshold=0.8,
        )
        other = ReviewReason(
            kind="low_confidence",
            section_id="1",
            field="Total",
            score=0.4,
            threshold=0.8,
        )

        # Reasons present: the first one's kind, whatever placed the item.
        assert self._info(reasons=[reason, other]).top_reason_kind == "missing_required"
        # No reason, and a rule placed it.
        assert self._info(reasons=[]).top_reason_kind == "routing_rule"
        # Neither: the default outcome placed it, or the item predates routing rules.
        assert (
            self._info(
                reasons=[], routed_by=ROUTED_BY.model_copy(update={"rule_id": ""})
            ).top_reason_kind
            is None
        )
        assert self._info(reasons=[], routed_by=None).top_reason_kind is None


def test_the_hold_document_needs_no_metering_attribute():
    """``after_completion`` accepts the bare document the tracker hands it."""
    item = _item(carried_metering_json=None)
    fake_store = MagicMock()
    fake_store.get_item.return_value = item
    fake_store.queue_defs = None
    fake_store.client.query_pages.return_value = iter([{"Items": []}])

    tracker.after_completion(
        TrackerReviewContext(kind="hold", item=item),
        workflow_status="SUCCEEDED",
        run_ref=None,
        document=SimpleNamespace(metering={}),
        store=fake_store,
        s3=MagicMock(),
        output_bucket="out",
        notifier=MagicMock(),
        document_service=MagicMock(),
        retention_days=30,
        now=NOW,
    )

    fake_store.replace_item.assert_called_once()
