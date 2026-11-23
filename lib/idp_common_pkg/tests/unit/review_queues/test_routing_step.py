# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routing decision after policy rule validation (``routing_step.decide``).

Against a moto HITL table, with the real store: what is decided, what item is
written, and what the item and its history record about the rule: the reason
its reviewer reads, and the policy rules the document failed.

The first class pins the compatibility promise: with the built-in rules a
document is held exactly when review queues held it before routing rules existed,
which is when ``build_reasons`` gives a reason.
"""

import itertools
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common.config.routing_models import (
    BUILTIN_CONFIDENCE_REASON,
    BUILTIN_EXTRACTION_FAILED_REASON,
    RoutingCondition,
    RoutingConfig,
    RoutingDefaultOutcome,
    RoutingRule,
    RoutingTable,
    builtin_rules,
    queue_actions,
)
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.review_queues import routing_step
from idp_common.review_queues.models import PolicyFailure, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.reasons import build_reasons
from idp_common.review_queues.routing_step import (
    decide,
    evaluate_table,
    fallback_text,
    routing_table,
)
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.run_plan.restart import layout_of
from idp_common.utils.transient_errors import TransientError

from ._table import REGION

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)
BUCKET = "out"
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-1"
OTHER_ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:run-0"
PLAN = {"v": 1, "kind": "reprocess", "startAt": "beginning", "mode": "pipeline"}
FINALIZE_PLAN = {"v": 1, "kind": "review_completed", "startAt": "apply_review_changes"}

INVOICE = {
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "required": ["Vendor"],
    "properties": {"Vendor": {"type": "string"}, "Total": {"type": "number"}},
}
SUMMARY_MD = "s3://out/{key}/rule_validation/consolidated/consolidated_summary.md"


class _S3:
    """Section results and summaries by URI; reads and deletes are recorded."""

    def __init__(self):
        self.objects = {}
        self.reads = []
        self.deleted = []

    def get_json_content(self, uri):
        self.reads.append(uri)
        if uri not in self.objects:
            raise FileNotFoundError(uri)
        return self.objects[uri]

    def delete_content(self, bucket, key):
        self.deleted.append((bucket, key))


@pytest.fixture
def store(review_table):
    review_store = ReviewStore(review_table, region=REGION)
    for definition in (
        QueueDefinition(id="default", name="Default", priority=50),
        QueueDefinition(id="high-value", name="High value", priority=80),
        QueueDefinition(id="closed", name="Closed", enabled=False),
    ):
        review_store.queue_defs.put(definition, create=True)
    return review_store


@pytest.fixture
def s3():
    return _S3()


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    published = []
    monkeypatch.setattr(
        "idp_common.review_queues.service._put_metric",
        lambda name, value=1, dimensions=None: published.append((name, dimensions)),
    )
    return published


def _uri(key, section_id):
    return f"s3://{BUCKET}/{key}/sections/{section_id}/result.json"


def _alert(name="Total", confidence=0.4, threshold=0.8):
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def _section(key, s3, section_id, *, alert=False, missing=False, values=None, **more):
    """A section, with its result stored so the two agree."""
    inference = dict(values or {})
    if not missing:
        inference.setdefault("Vendor", "ACME")
    result = {"inference_result": inference, "explainability_info": [{}]}
    if more.pop("confirmed", False):
        result["review"] = {"v": 1, "confirmed": True}
    s3.objects[_uri(key, section_id)] = result
    fields = dict(
        section_id=section_id,
        classification="Invoice",
        # One page per section, numbered as the section is.
        page_ids=[section_id],
        extraction_result_uri=_uri(key, section_id),
        confidence_threshold_alerts=[_alert()] if alert else [],
        confidence_alert_total=None,
        missing_required_fields=[{"attribute_name": "Vendor"}] if missing else [],
        confidence=None,
        blueprint_unscored=False,
        excluded=False,
    )
    fields.update(more)
    return SimpleNamespace(**fields)


def _document(key, *sections, **more):
    fields = dict(
        input_key=key,
        sections=list(sections),
        num_pages=len(sections),
        config_version="lending",
        config_revision=4,
        output_bucket=BUCKET,
        submission_source=None,
        test_set_id=None,
        rule_validation_result=None,
    )
    fields.update(more)
    return SimpleNamespace(**fields)


def _config(*, hitl=True, route_missing=None, table=None, limit=3):
    return SimpleNamespace(
        classes=[INVOICE],
        hitl=SimpleNamespace(
            enabled=hitl,
            confidence_threshold=0.8,
            route_missing_required_fields=route_missing,
        ),
        routing=RoutingConfig(
            tables=[table] if table is not None else [RoutingTable()],
            max_consecutive_placements=limit,
        ),
    )


def _decide(document, store, s3, config=None, *, mode="pipeline", arn=ARN, plan=PLAN):
    return decide(
        document,
        config if config is not None else _config(),
        mode=mode,
        execution_arn=arn,
        run_plan=plan,
        store=store,
        s3=s3,
        document_service=MagicMock(),
        output_bucket=BUCKET,
        now=NOW,
    )


def _rule(rule_id, queue, *conditions, label="", kind="queue", reason=""):
    return RoutingRule(
        id=rule_id,
        label=label or rule_id,
        conditions=[RoutingCondition(**c) for c in conditions],
        # No action is "no review": the document is not placed in a queue.
        actions=queue_actions(queue) if kind == "queue" else [],
        reason=reason,
    )


def _seed(store, key, *, state="OPEN", **more):
    """A review item of an earlier run of the document."""
    fields = dict(
        doc_key=key,
        cycle_id="0" * 21,
        profile="lending",
        queue="default",
        state=state,
        version=3,
        enqueued_at="2026-11-15T00:00:00.000Z",
        enqueued_at_ms=1,
        sections=["1"],
        phase1_execution_arn=OTHER_ARN,
        expires_after=int(NOW.timestamp()) + 86_400,
    )
    fields.update(more)
    record = ReviewItemRecord(**fields)
    store.client.put_item(item_to_dynamo(record))
    return record


def _finalizing(store, key, disposition, **more):
    """The item of a review that ended, whose finalize run is this execution."""
    fields = dict(
        state="FINALIZING",
        disposition=disposition,
        finalize_token="rvw-" + "ab" * 16,
        finalize_execution_name=ARN.rsplit(":", 1)[-1],
        phase1_metering_json=json.dumps({"OCR/textract/analyze": {"pages": 3}}),
    )
    fields.update(more)
    return _seed(store, key, **fields)


def _events(store, key, cycle_id):
    return [(h.event, h.details) for h in store.list_history(key, cycle_id)]


class TestTheBuiltInRulesHoldWhatReviewQueuesHeld:
    def _sections(self, key, s3, states):
        return [
            _section(key, s3, str(index), alert=alert, missing=missing, **blueprint)
            for index, (alert, missing, blueprint) in enumerate(states, start=1)
        ]

    def _cases(self, mode):
        flags = list(itertools.product([False, True], repeat=2))
        if mode == "pipeline":
            states = [(alert, missing, {}) for alert, missing in flags]
            sizes = range(0, 4)
        else:
            blueprints = [
                {"confidence": 0.5},  # under the threshold
                {"confidence": 0.95},  # over it
                {"confidence": None, "blueprint_unscored": True},  # matched, unscored
            ]
            states = [
                (alert, missing, blueprint)
                for alert, missing in flags
                for blueprint in blueprints
            ]
            sizes = range(0, 3)
        for size in sizes:
            yield from itertools.product(states, repeat=size)

    @pytest.mark.parametrize("mode", ["pipeline", "bda"])
    @pytest.mark.parametrize("route_missing", [True, False])
    def test_builtin_table_equals_build_reasons(self, store, s3, mode, route_missing):
        config = _config(route_missing=route_missing)
        held = passed = 0
        for number, states in enumerate(self._cases(mode)):
            key = f"gen/{mode}-{route_missing}-{number}.pdf"
            document = _document(key, *self._sections(key, s3, states))
            expected = bool(build_reasons(document, config, mode=mode))

            decision = _decide(document, store, s3, config, mode=mode)

            assert (decision.decision == "hold") is expected, (states, decision)
            if expected:
                assert decision.queue == "default", (states, decision)
                assert store.get_item(key).state == "HOLDING"
                held += 1
            else:
                assert store.get_item(key) is None
                passed += 1
        # Both sides of the comparison were exercised.
        assert held and passed

    @pytest.mark.parametrize("mode", ["pipeline", "bda"])
    def test_a_held_document_records_the_mode_of_its_run(self, store, s3, mode):
        """A send-back of the review runs in this mode, so the item keeps it and
        the API returns it: a reviewer who cannot read the configuration is still
        offered the steps of the right mode."""
        from idp_common.review_queues.service import item_view

        key = f"mode/{mode}.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, mode=mode)

        assert decision.decision == "hold"
        item = store.get_item(key)
        assert item.processing_mode == mode
        assert item_view(item).dump()["processingMode"] == mode

    def test_a_held_document_records_its_sections_as_they_are_placed(self, store, s3):
        """Section id to a digest of its pages, excluded sections included: what
        a regrouped section is told apart against when the review is completed.
        Where the run after the review starts is not decided yet."""
        key = "layout.pdf"
        document = _document(
            key,
            _section(key, s3, "1", alert=True, page_ids=["1", "2"]),
            _section(key, s3, "2", page_ids=["3"], excluded=True),
            _section(key, s3, "3", page_ids=["10", "4"]),
        )

        decision = _decide(document, store, s3)

        assert decision.decision == "hold"
        item = store.get_item(key)
        assert item.placed_layout == layout_of(document)
        assert sorted(item.placed_layout) == ["1", "2", "3"]
        # The digest is of the pages, in whatever order the section lists them.
        reordered = _document(key, _section(key, s3, "3", page_ids=["4", "10"]))
        assert item.placed_layout["3"] == layout_of(reordered)["3"]
        assert item.placed_layout["1"] != item.placed_layout["3"]
        assert (item.restart_step, item.restart_sections) == (None, [])

    def test_a_failed_extraction_is_held_by_the_first_built_in_rule(self, store, s3):
        """#2038: a section whose extraction failed has no fields and no
        confidence scores. The built-in table holds the document for it, and
        says so before anything else the document has."""
        key = "a.pdf"
        failed = _section(
            key,
            s3,
            "2",
            missing=True,
            extraction_result_uri=None,
            processing_issues=[
                SimpleNamespace(
                    stage="extraction",
                    severity="error",
                    code="extraction_failed",
                    message="The model's reply was withheld.",
                    section_id="2",
                )
            ],
        )
        del s3.objects[_uri(key, "2")]
        document = _document(key, _section(key, s3, "1", alert=True), failed)

        decision = _decide(document, store, s3)

        item = store.get_item(key)
        assert (decision.decision, decision.reason, decision.rule_id) == (
            "hold",
            "rule",
            "builtin-extraction-failed",
        )
        assert (item.queue, item.routed_by.rule_label) == (
            "default",
            "Failed extractions",
        )
        assert item.routed_by.reason == (
            "Sections whose extraction failed: 1. They have no extracted values: "
            "enter them from the document."
        )
        assert item.routed_by.reason_template == BUILTIN_EXTRACTION_FAILED_REASON
        # The item lists what a reviewer works through: the failed section, with
        # the required field it left empty, and the alert of the other one.
        assert sorted(r.kind for r in item.reasons) == [
            "extraction_failed",
            "low_confidence",
            "missing_required",
        ]

    def test_a_held_document_gets_its_reasons_and_the_rule(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3)

        item = store.get_item(key)
        assert (decision.decision, decision.reason, decision.rule_id) == (
            "hold",
            "rule",
            "builtin-confidence-alerts",
        )
        assert decision.cycle_id == item.cycle_id
        assert [r.kind for r in item.reasons] == ["low_confidence"]
        assert item.sections == ["1"]
        assert (item.queue, item.priority, item.placement_count) == ("default", 50, 1)
        assert item.phase1_execution_arn == ARN
        routed = item.routed_by
        assert (routed.table_id, routed.rule_id, routed.rule_label) == (
            "review-routing",
            "builtin-confidence-alerts",
            "Confidence alerts",
        )
        assert (routed.profile, routed.config_revision) == ("lending", 4)
        assert (routed.outcome_queue, routed.placed_queue, routed.fallback_reason) == (
            "default",
            "default",
            "",
        )
        assert routed.inputs == {"reviewed": "false", "confidence_alert_count": "1"}
        assert routed.evaluated_at == "2026-11-16T10:00:00.000Z"
        # The built-in routing rule's reason, with the document's count in it.
        reason = (
            "Fields below the confidence threshold: 1. Check the highlighted "
            "fields and correct them."
        )
        assert (routed.reason, routed.reason_source) == (reason, "admin")
        assert routed.reason_template == BUILTIN_CONFIDENCE_REASON
        assert (routed.policy_failures, routed.policy_failure_count) == ([], 0)
        assert _events(store, key, item.cycle_id) == [
            (
                "routed",
                {
                    "queue": "default",
                    "rule_id": "builtin-confidence-alerts",
                    "rule_label": "Confidence alerts",
                    "fallback_reason": "",
                    "routing_reason": reason,
                    "reason_source": "admin",
                },
            )
        ]


class TestWhenNothingIsRouted:
    def test_hitl_off_never_holds(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, _config(hitl=False))

        assert (decision.decision, decision.reason) == ("continue", "hitl_off")
        assert store.get_item(key) is None

    def test_default_queue_off(self, store, s3):
        store.queue_defs.put(
            QueueDefinition(id="default", name="Default", enabled=False),
            create=False,
            expected_version=store.queue_defs.read_current("default").version,
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.reason) == ("continue", "default_queue_off")
        assert store.get_item(key) is None

    def test_a_stack_with_no_review_table_never_holds(self, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, None, s3)

        assert (decision.decision, decision.reason) == ("continue", "no_review_store")
        assert s3.reads == []

    def test_review_routing_off_is_named_before_the_default_queue(self, store, s3):
        """Both are off: the logged reason is the switch, which is checked first."""
        store.queue_defs.put(
            QueueDefinition(id="default", name="Default", enabled=False),
            create=False,
            expected_version=store.queue_defs.read_current("default").version,
        )
        key = "a.pdf"

        decision = _decide(
            _document(key, _section(key, s3, "1", alert=True)),
            store,
            s3,
            _config(hitl=False),
        )

        assert decision.reason == "hitl_off"

    def test_the_logged_reasons_are_the_documented_ones(self):
        """``legacy`` was logged for three different causes; each has its name."""
        assert not hasattr(routing_step, "REASON_LEGACY")
        assert (
            routing_step.REASON_NO_REVIEW_STORE,
            routing_step.REASON_HITL_OFF,
            routing_step.REASON_DEFAULT_QUEUE_OFF,
        ) == ("no_review_store", "hitl_off", "default_queue_off")

    def test_test_studio_pipeline(self, store, s3):
        key = "a.pdf"
        document = _document(
            key, _section(key, s3, "1", alert=True), submission_source="test-studio"
        )

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.reason) == ("continue", "test_studio")
        assert store.get_item(key) is None

    def test_test_studio_bda_reloaded(self, store, s3):
        """A BDA run reloads the document from its tracking row. The row's Test
        Studio markers are restored, so the document is still not held."""
        reader = DocumentDynamoDBService(dynamodb_client=MagicMock())
        document = reader._dynamodb_item_to_document(
            {
                "PK": "doc#set/a.pdf",
                "SK": "none",
                "ObjectKey": "set/a.pdf",
                "ObjectStatus": "POSTPROCESSING",
                "TestSetId": "set-7",
                "Sections": [
                    {
                        "Id": "1",
                        "PageIds": [1],
                        "Class": "Invoice",
                        "OutputJSONUri": _uri("set/a.pdf", "1"),
                        "ConfidenceThresholdAlerts": [
                            {
                                "attributeName": "Total",
                                "confidence": "0.4",
                                "confidenceThreshold": "0.8",
                            }
                        ],
                    }
                ],
            }
        )
        assert document.test_set_id == "set-7"
        assert document.sections[0].confidence_threshold_alerts

        decision = _decide(document, store, s3, mode="bda")

        assert (decision.decision, decision.reason) == ("continue", "test_studio")
        assert store.get_item("set/a.pdf") is None

    def test_a_clean_document_supersedes_an_open_review_of_an_earlier_run(
        self, store, s3
    ):
        key = "a.pdf"
        _seed(store, key, state="OPEN")
        document = _document(key, _section(key, s3, "1"))

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.reason) == ("continue", "default_outcome")
        assert store.get_item(key).state == "WITHDRAWN"
        assert store.get_item(key).withdrawn_reason == "superseded_no_review"

    def test_every_decision_is_counted(self, store, s3, metrics):
        key = "a.pdf"
        _decide(_document(key, _section(key, s3, "1", alert=True)), store, s3)
        _decide(_document("b.pdf", _section("b.pdf", s3, "1")), store, s3)

        counted = [d for name, d in metrics if name == "RoutingDecisions"]
        assert counted == [
            [{"Name": "Decision", "Value": "hold"}],
            [{"Name": "Decision", "Value": "continue"}],
        ]


class TestCustomRules:
    def test_a_field_value_over_an_amount(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "high-value",
                    "high-value",
                    {
                        "input": "field_value",
                        "path": "Invoice:Total",
                        "test": "> 10000",
                    },
                    label="Invoices over 10,000",
                )
            ]
            + builtin_rules()
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        decision = _decide(document, store, s3, _config(table=table))

        item = store.get_item(key)
        assert (decision.decision, decision.queue, decision.rule_id) == (
            "hold",
            "high-value",
            "high-value",
        )
        assert (item.queue, item.priority) == ("high-value", 80)
        # Placed by the rule, with no confidence reason: every section is in review.
        assert (item.reasons, item.reason_count, item.sections) == ([], 0, ["1"])
        assert item.routed_by.rule_label == "Invoices over 10,000"
        assert item.routed_by.inputs == {
            'field_value("Invoice:Total","any")': "[12000]"
        }

    def test_a_document_under_the_amount_is_not_placed(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "high-value",
                    "high-value",
                    {
                        "input": "field_value",
                        "path": "Invoice:Total",
                        "test": "> 10000",
                    },
                )
            ]
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 500}))

        decision = _decide(document, store, s3, _config(table=table))

        assert (decision.decision, decision.reason) == ("continue", "default_outcome")

    def test_a_failed_policy_rule(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "ltv-failed",
                    "high-value",
                    {
                        "input": "policy_rule_verdict",
                        "path": "lending.max_ltv",
                        "test": '"fail"',
                    },
                )
            ]
        )
        key = "a.pdf"
        summary = SUMMARY_MD.format(key=key)
        s3.objects[summary[:-3] + ".json"] = {
            "rule_details": {
                "lending": {
                    "rules": [{"rule_id": "lending.max_ltv", "verdict": "fail"}]
                }
            }
        }
        document = _document(
            key,
            _section(key, s3, "1"),
            rule_validation_result=SimpleNamespace(output_uri=summary),
        )

        decision = _decide(document, store, s3, _config(table=table))

        assert (decision.decision, decision.queue, decision.rule_id) == (
            "hold",
            "high-value",
            "ltv-failed",
        )
        assert store.get_item(key).reasons == []

    def test_a_section_class(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "invoices",
                    "high-value",
                    {
                        "input": "section_classes",
                        "aggregate": "any",
                        "test": '"Invoice"',
                    },
                )
            ]
        )
        key = "a.pdf"

        decision = _decide(
            _document(key, _section(key, s3, "1")), store, s3, _config(table=table)
        )

        assert (decision.decision, decision.queue) == ("hold", "high-value")

    def test_a_rule_can_send_a_document_past_review(self, store, s3):
        """A no-review rule placed before the built-in ones wins."""
        table = RoutingTable(
            rules=[
                _rule(
                    "trusted",
                    "",
                    {"input": "document_class", "test": '"Invoice"'},
                    kind="no_review",
                )
            ]
            + builtin_rules()
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, _config(table=table))

        assert (decision.decision, decision.reason, decision.rule_id) == (
            "continue",
            "rule",
            "trusted",
        )
        assert store.get_item(key) is None

    def test_the_stored_inputs_are_bounded(self, store, s3):
        conditions = [
            {"input": "field_value", "path": f"Invoice:Field{i}", "aggregate": "count"}
            for i in range(30)
        ]
        table = RoutingTable(rules=[_rule("many", "high-value", *conditions)])
        key = "a.pdf"
        long_value = {"Vendor": "x" * 1000}
        document = _document(key, _section(key, s3, "1", values=long_value))

        _decide(document, store, s3, _config(table=table))
        inputs = store.get_item(key).routed_by.inputs

        assert len(inputs) == 20
        assert all(len(value.encode("utf-8")) <= 200 for value in inputs.values())

    def test_a_long_value_is_cut(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "vendor",
                    "high-value",
                    {"input": "field_value", "path": "Invoice:Vendor"},
                )
            ]
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Vendor": "é" * 500}))

        _decide(document, store, s3, _config(table=table))
        (value,) = store.get_item(key).routed_by.inputs.values()

        assert len(value.encode("utf-8")) <= 200
        assert value.startswith('["é')


def _total_rule(reason="", *, label="Invoices over 10,000"):
    return _rule(
        "high-value",
        "high-value",
        {"input": "field_value", "path": "Invoice:Total", "test": "> 10000"},
        label=label,
        reason=reason,
    )


def _fallbacks(metrics):
    """The ``Cause`` of every reason the fallback replaced."""
    return [
        dimensions[0]["Value"]
        for name, dimensions in metrics
        if name == "RoutingReasonFallback"
    ]


def _summary(key, **policies):
    """A policy rule validation summary, stored beside the report."""
    return {
        "uri": SUMMARY_MD.format(key=key),
        "body": {
            "rule_details": {name: {"rules": rules} for name, rules in policies.items()}
        },
    }


def _with_summary(s3, key, *sections, **policies):
    summary = _summary(key, **policies)
    s3.objects[summary["uri"][:-3] + ".json"] = summary["body"]
    return _document(
        key,
        *sections,
        rule_validation_result=SimpleNamespace(output_uri=summary["uri"]),
    )


def _failed(rule_id, reasoning="It failed.", pages=("1",)):
    return {
        "rule_id": rule_id,
        "verdict": "fail",
        "reasoning": reasoning,
        "supporting_pages": list(pages),
    }


class TestTheReasonTheReviewerReads:
    """What ``RoutedBy.reason`` holds, and where it came from."""

    def _routed(self, store, s3, rule, *, values=None, key="a.pdf", **config):
        table = RoutingTable(rules=[rule])
        document = _document(
            key, _section(key, s3, "1", values=values or {"Total": 12000})
        )
        decision = _decide(document, store, s3, _config(table=table, **config))
        assert decision.decision == "hold"
        return store.get_item(key).routed_by

    def test_an_admins_reason_gets_the_documents_values(self, store, s3, metrics):
        template = (
            "Total {field_value:Invoice:Total} is over {condition_1.value}. "
            "Vendor: {field_value:Invoice:Vendor}. Fields under "
            "{confidence_threshold} are highlighted. {{Not a placeholder}}"
        )

        routed = self._routed(
            store, s3, _total_rule(template), values={"Total": 12000, "Vendor": "ACME"}
        )

        assert routed.reason == (
            "Total 12000 is over 10000. Vendor: ACME. Fields under 0.8 are "
            "highlighted. {Not a placeholder}"
        )
        # The reason as written is kept beside the text the reviewer reads.
        assert routed.reason_template == template
        assert routed.reason_source == "admin"
        assert _fallbacks(metrics) == []

    def test_a_value_the_document_does_not_have_reads_as_absent(self, store, s3):
        routed = self._routed(
            store, s3, _total_rule("PO number: {field_value:Invoice:PurchaseOrder}")
        )

        assert routed.reason == "PO number: (no value)"
        assert routed.reason_source == "admin"

    def test_a_reason_with_no_placeholder_is_stored_as_written(self, store, s3):
        routed = self._routed(store, s3, _total_rule("Check the total by hand."))

        assert (routed.reason, routed.reason_template, routed.reason_source) == (
            "Check the total by hand.",
            "Check the total by hand.",
            "admin",
        )

    def test_no_reason_gives_the_fallback_with_the_values_found(
        self, store, s3, metrics
    ):
        routed = self._routed(store, s3, _total_rule())

        assert routed.reason == (
            "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)"
        )
        assert (routed.reason_template, routed.reason_source) == ("", "fallback")
        assert _fallbacks(metrics) == ["no_reason"]

    def test_a_blank_reason_is_no_reason(self, store, s3, metrics):
        routed = self._routed(store, s3, _total_rule("  "))

        assert routed.reason.startswith("Invoices over 10,000: ")
        assert routed.reason_source == "fallback"
        assert _fallbacks(metrics) == ["no_reason"]

    def test_the_default_outcome_has_its_own_fallback(self, store, s3, metrics):
        table = RoutingTable(
            rules=[],
            default_outcome=RoutingDefaultOutcome(actions=queue_actions("high-value")),
        )
        key = "a.pdf"

        decision = _decide(
            _document(key, _section(key, s3, "1")), store, s3, _config(table=table)
        )

        routed = store.get_item(key).routed_by
        assert (decision.decision, decision.reason, decision.rule_id) == (
            "hold",
            "default_outcome",
            "",
        )
        assert (routed.reason, routed.reason_source) == (
            "No routing rule matched.",
            "fallback",
        )
        assert _fallbacks(metrics) == ["no_reason"]

    def test_the_default_outcomes_own_reason_is_rendered(self, store, s3):
        table = RoutingTable(
            rules=[],
            default_outcome=RoutingDefaultOutcome(
                actions=queue_actions("high-value"),
                reason="Every {document_class} is checked ({page_count} page).",
            ),
        )
        key = "a.pdf"

        _decide(_document(key, _section(key, s3, "1")), store, s3, _config(table=table))

        routed = store.get_item(key).routed_by
        assert (routed.reason, routed.reason_source) == (
            "Every Invoice is checked (1 page).",
            "admin",
        )

    def test_a_table_that_cannot_be_evaluated_says_so(self, store, s3, metrics):
        table = RoutingTable(engine="not-an-engine")
        key = "a.pdf"

        _decide(_document(key, _section(key, s3, "1")), store, s3, _config(table=table))

        routed = store.get_item(key).routed_by
        assert routed.reason == (
            "The routing rules could not be evaluated, so the document is in the "
            "Default queue."
        )
        assert (routed.reason_template, routed.reason_source) == ("", "fallback")
        assert _fallbacks(metrics) == ["no_reason"]

    @pytest.mark.parametrize(
        "template",
        [
            # Each is refused when the routing rules are saved; a profile saved
            # before the check, or a pinned revision, can still hold one.
            "Over {condition_3.value}",  # the routing rule has one condition
            "An unmatched { brace",
            "An empty {} placeholder",
        ],
    )
    def test_a_reason_that_cannot_be_rendered_gives_the_fallback(
        self, store, s3, metrics, caplog, template
    ):
        with caplog.at_level(logging.INFO):
            routed = self._routed(store, s3, _total_rule(template))

        assert routed.reason == (
            "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)"
        )
        # What the Admin wrote is kept, so the cause can be read off the item.
        assert (routed.reason_template, routed.reason_source) == (template, "fallback")
        assert _fallbacks(metrics) == ["render_error"]
        warnings = [
            r.getMessage()
            for r in caplog.records
            if "routing_reason_render_failed" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "rule=high-value" in warnings[0]

    def test_a_render_error_never_fails_the_placement_or_logs_the_text(
        self, store, s3, metrics, caplog, monkeypatch
    ):
        def broken(text, resolve):
            raise RuntimeError(f"cannot render {text!r} for 12000")

        monkeypatch.setattr(routing_step, "render_reason", broken)
        template = "Total {field_value:Invoice:Total} is high: TEMPLATE-MARKER"

        with caplog.at_level(logging.INFO):
            routed = self._routed(store, s3, _total_rule(template))

        assert routed.reason == (
            "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)"
        )
        assert (routed.reason_template, routed.reason_source) == (template, "fallback")
        assert (
            "RoutingReasonFallback",
            [{"Name": "Cause", "Value": "render_error"}],
        ) in (metrics)
        logged = [r.getMessage() for r in caplog.records]
        (warning,) = [m for m in logged if "routing_reason_render_failed" in m]
        assert warning == (
            "event=routing_reason_render_failed rule=high-value error=RuntimeError"
        )
        # Neither the text the reviewer reads, nor the value in it, nor the
        # error's message (which quotes both) is in any record.
        assert not [m for m in logged if routed.reason in m]
        assert not [m for m in logged if "12000" in m or "TEMPLATE-MARKER" in m]

    def test_the_reason_is_not_logged(self, store, s3, caplog):
        """It can hold extracted values. The decision line names its source."""
        with caplog.at_level(logging.INFO):
            routed = self._routed(
                store,
                s3,
                _total_rule("Vendor {field_value:Invoice:Vendor} needs a second look"),
                values={"Total": 12000, "Vendor": "ZETA-CORP-771"},
            )

        assert routed.reason == "Vendor ZETA-CORP-771 needs a second look"
        logged = [r.getMessage() for r in caplog.records]
        assert not [m for m in logged if "ZETA-CORP-771" in m or "second look" in m]
        (line,) = [m for m in logged if m.startswith("event=routing_decision")]
        assert line == (
            "event=routing_decision decision=hold reason=rule rule=high-value "
            "queue=high-value reason_source=admin"
        )

    def test_the_decision_carries_no_reviewer_text(self, store, s3):
        """The handler logs the decision, so the reason is only on the item."""
        key = "a.pdf"
        table = RoutingTable(rules=[_total_rule("Vendor {field_value:Invoice:Vendor}")])
        document = _document(
            key, _section(key, s3, "1", values={"Total": 12000, "Vendor": "ZETA"})
        )

        decision = _decide(document, store, s3, _config(table=table))

        assert set(decision.model_dump()) == {
            "decision",
            "reason",
            "queue",
            "cycle_id",
            "rule_id",
        }
        assert "ZETA" not in decision.model_dump_json()
        assert decision.reason == "rule"

    def test_the_rendered_reason_is_bounded(self, store, s3):
        routed = self._routed(
            store,
            s3,
            _total_rule("{field_value:Invoice:Vendor} " * 10),
            values={"Total": 12000, "Vendor": "é" * 400},
        )

        assert len(routed.reason.encode("utf-8")) <= 1000
        assert routed.reason.endswith("…")

    def test_a_document_that_is_not_placed_gets_no_reason_work(
        self, store, s3, metrics
    ):
        table = RoutingTable(rules=[_total_rule("Total {field_value:Invoice:Vendor}")])
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 5}))

        decision = _decide(document, store, s3, _config(table=table))

        assert decision.decision == "continue"
        assert _fallbacks(metrics) == []


class TestWhatIsReadForTheReason:
    """Placeholder values and the summary are read only for a placement."""

    def test_only_the_matched_routing_rules_placeholders_are_read(self, store, s3):
        """The first routing rule does not match. Its reason names a field, and
        nothing else in the table reads a section result: none is read."""
        table = RoutingTable(
            rules=[
                _rule(
                    "receipts",
                    "high-value",
                    {"input": "document_class", "test": '"Receipt"'},
                    reason="Receipt total: {field_value:Invoice:Total}",
                ),
                _rule(
                    "long",
                    "high-value",
                    {"input": "page_count", "test": ">= 1"},
                    reason="{page_count} page of class {document_class}",
                ),
            ]
        )
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        decision = _decide(document, store, s3, _config(table=table))

        routed = store.get_item(key).routed_by
        assert (decision.decision, decision.rule_id) == ("hold", "long")
        assert routed.reason == "1 page of class Invoice"
        assert s3.reads == []

    def test_the_matched_routing_rules_placeholder_is_read_once(self, store, s3):
        table = RoutingTable(
            rules=[
                _rule(
                    "long",
                    "high-value",
                    {"input": "page_count", "test": ">= 1"},
                    reason=(
                        "Total {field_value:Invoice:Total}, vendor "
                        "{field_value:Invoice:Vendor}, again {field_value:Invoice:Total}"
                    ),
                )
            ]
        )
        key = "a.pdf"
        document = _document(
            key,
            _section(key, s3, "1", values={"Total": 12000}),
            _section(key, s3, "2", values={"Total": 7}),
        )

        _decide(document, store, s3, _config(table=table))

        assert store.get_item(key).routed_by.reason == (
            "Total 12000, 7, vendor ACME, ACME, again 12000, 7"
        )
        assert sorted(s3.reads) == [_uri(key, "1"), _uri(key, "2")]

    def test_a_value_the_table_read_is_not_read_again(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))
        table = RoutingTable(rules=[_total_rule("Total: {field_value:Invoice:Total}")])

        _decide(document, store, s3, _config(table=table))

        assert store.get_item(key).routed_by.reason == "Total: 12000"
        assert s3.reads == [_uri(key, "1")]

    def test_a_placement_over_the_limit_reads_nothing_for_the_reason(self, store, s3):
        """The document is not placed, so neither its reason's placeholder nor
        its failed policy rules are read: the summary is never asked for."""
        table = RoutingTable(
            rules=[
                _rule(
                    "second-opinion",
                    "high-value",
                    {"input": "reviewed", "test": "true"},
                    reason="Failed policy rules: {failed_policy_rule_count}",
                )
            ]
        )
        key = "a.pdf"
        _finalizing(store, key, "complete", placement_count=2)
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", confirmed=True),
            lending=[_failed("lending.max_ltv")],
        )

        decision = _decide(
            document, store, s3, _config(table=table, limit=2), plan=FINALIZE_PLAN
        )

        assert (decision.decision, decision.reason) == ("continue", "placement_limit")
        # The section result, for ``reviewed``. Not the summary.
        assert s3.reads == [_uri(key, "1")]

    def test_the_same_placement_under_the_limit_reads_the_summary_once(self, store, s3):
        """The control of the case above: placed, the summary is read, once for
        the placeholder and the failed policy rules together."""
        table = RoutingTable(
            rules=[
                _rule(
                    "second-opinion",
                    "high-value",
                    {"input": "reviewed", "test": "true"},
                    reason="Failed policy rules: {failed_policy_rule_count}",
                )
            ]
        )
        key = "a.pdf"
        _finalizing(store, key, "complete", placement_count=1)
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", confirmed=True),
            lending=[_failed("lending.max_ltv")],
        )

        decision = _decide(
            document, store, s3, _config(table=table, limit=2), plan=FINALIZE_PLAN
        )

        routed = store.get_item(key).routed_by
        assert decision.decision == "hold"
        assert routed.reason == "Failed policy rules: 1"
        assert routed.policy_failure_count == 1
        summary_json = SUMMARY_MD.format(key=key)[:-3] + ".json"
        assert s3.reads == [_uri(key, "1"), summary_json]


class TestFallbackText:
    """``fallback_text``: the reason built from a routing rule's conditions."""

    def test_each_condition_with_the_value_found(self):
        rule = _rule(
            "r1",
            "default",
            {"input": "field_value", "path": "Invoice:Total", "test": "> 10000"},
            {"input": "document_class", "test": '"Invoice", "Receipt"'},
            {"input": "reviewed", "test": "false"},
            label="Large invoices",
        )
        values = {
            'field_value("Invoice:Total","any")': [12000, 15000],
            "document_class": "Invoice",
            "reviewed": False,
        }

        assert fallback_text(rule, values) == (
            "Large invoices: Field value Invoice:Total > 10000 (found 12000, 15000); "
            'Document class "Invoice", "Receipt" (found Invoice); '
            "Reviewed false (found no)"
        )

    def test_an_aggregate_other_than_any_is_named(self):
        rule = _rule(
            "r1",
            "default",
            {"input": "section_classes", "aggregate": "count", "test": "> 2"},
            {
                "input": "field_value",
                "path": "Total",
                "aggregate": "all",
                "test": "> 0",
            },
            {"input": "page_count", "aggregate": "count", "test": "> 5"},
        )
        values = {
            'section_classes("count")': 3,
            'field_value("Total","all")': [4, 9],
            "page_count": 8,
        }

        assert fallback_text(rule, values) == (
            "r1: Section classes (count) > 2 (found 3); "
            "Field value Total (all) > 0 (found 4, 9); "
            # Not a list input: its aggregate means nothing and is not shown.
            "Page count > 5 (found 8)"
        )

    def test_a_routing_rule_with_no_label_is_named_by_its_id(self):
        rule = RoutingRule(
            id="r-7", conditions=[RoutingCondition(input="page_count", test="> 5")]
        )

        assert fallback_text(rule, {"page_count": 8}) == (
            "r-7: Page count > 5 (found 8)"
        )

    def test_a_value_that_was_not_read_is_absent(self):
        rule = _rule(
            "r1",
            "default",
            {
                "input": "policy_rule_verdict",
                "path": "lending.max_ltv",
                "test": '"fail"',
            },
            {"input": "not_an_input", "test": "> 1"},
        )

        assert fallback_text(rule, {}) == (
            'r1: Policy rule verdict lending.max_ltv "fail" (found (no value)); '
            "not_an_input > 1 (found (no value))"
        )

    def test_a_routing_rule_with_no_condition(self):
        rule = _rule("all", "default", label="Everything")

        assert fallback_text(rule, {}) == (
            "Everything: this routing rule applies to every document"
        )

    def test_no_routing_rule(self):
        assert fallback_text(None, {"page_count": 3}) == "No routing rule matched."

    def test_an_evaluation_error_comes_first(self):
        text = (
            "The routing rules could not be evaluated, so the document is in the "
            "Default queue."
        )

        assert fallback_text(None, {}, evaluation_error=True) == text
        assert fallback_text(_rule("r1", "default"), {}, evaluation_error=True) == text

    def test_it_is_bounded(self):
        conditions = [
            {
                "input": "field_value",
                "path": f"Invoice:Field{i}",
                "test": f'"{"é" * 80}"',
            }
            for i in range(8)
        ]
        text = fallback_text(_rule("r1", "default", *conditions, label="L" * 200), {})

        assert len(text.encode("utf-8")) <= 1000
        assert text.endswith("…")


class TestFailedPolicyRules:
    """``RoutedBy.policy_failures``: filled for every held document that has any."""

    def test_they_are_listed_for_a_routing_rule_that_tests_no_policy_result(
        self, store, s3
    ):
        key = "a.pdf"
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", alert=True),
            lending=[
                _failed(
                    "lending.max_ltv", "LTV is 92%,\nover the 80% limit.", ["3", 4]
                ),
                {"rule_id": "lending.income", "verdict": "pass", "reasoning": "Fine."},
            ],
            kyc=[_failed("kyc.id_match", "The names differ.", [])],
        )

        # The built-in table: confidence alerts and missing fields, no policy input.
        decision = _decide(document, store, s3)

        routed = store.get_item(key).routed_by
        assert (decision.decision, decision.rule_id) == (
            "hold",
            "builtin-confidence-alerts",
        )
        assert routed.policy_failures == [
            PolicyFailure(
                rule_id="lending.max_ltv",
                policy="lending",
                name="max_ltv",
                reason="LTV is 92%, over the 80% limit.",
                pages=[3, 4],
            ),
            PolicyFailure(
                rule_id="kyc.id_match",
                policy="kyc",
                name="id_match",
                reason="The names differ.",
                pages=[],
            ),
        ]
        assert routed.policy_failure_count == 2

    def test_they_are_stored_under_their_attribute_names(self, store, s3):
        key = "a.pdf"
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", alert=True),
            lending=[_failed("lending.max_ltv", "Too high.", ["3"])],
        )

        _decide(document, store, s3)

        from idp_common.review_queues import keys

        stored = store.client.get_item(keys.item_key(key))["RoutedBy"]
        assert stored["PolicyFailures"] == [
            {
                "RuleId": "lending.max_ltv",
                "Policy": "lending",
                "Name": "max_ltv",
                "Reason": "Too high.",
                "Pages": [3],
            }
        ]
        assert stored["PolicyFailureCount"] == 1
        assert stored["ReasonSource"] == "admin"
        assert stored["ReasonTemplate"] == BUILTIN_CONFIDENCE_REASON
        assert stored["Reason"].startswith("Fields below the confidence threshold: 1.")

    def test_the_stored_list_is_bounded_and_the_count_is_not(self, store, s3):
        key = "a.pdf"
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", alert=True),
            lending=[
                _failed(f"lending.r{i}", "é" * 600, [str(p) for p in range(1, 40)])
                for i in range(14)
            ],
        )

        _decide(document, store, s3)

        routed = store.get_item(key).routed_by
        assert routed.policy_failure_count == 14
        assert [f.rule_id for f in routed.policy_failures] == [
            f"lending.r{i}" for i in range(10)
        ]
        assert all(
            len(f.reason.encode("utf-8")) <= 500 and len(f.pages) == 10
            for f in routed.policy_failures
        )

    def test_a_document_with_no_summary_has_none(self, store, s3):
        key = "a.pdf"

        _decide(_document(key, _section(key, s3, "1", alert=True)), store, s3)

        routed = store.get_item(key).routed_by
        assert (routed.policy_failures, routed.policy_failure_count) == ([], 0)
        # Only the section result: there is no summary to ask for.
        assert s3.reads == [_uri(key, "1")]

    def test_a_summary_that_cannot_be_read_does_not_fail_the_placement(self, store, s3):
        key = "a.pdf"
        document = _document(
            key,
            _section(key, s3, "1", alert=True),
            rule_validation_result=SimpleNamespace(
                output_uri=SUMMARY_MD.format(key=key)
            ),
        )

        decision = _decide(document, store, s3)

        routed = store.get_item(key).routed_by
        assert decision.decision == "hold"
        assert (routed.policy_failures, routed.policy_failure_count) == ([], 0)

    def test_a_reader_that_raises_does_not_fail_the_placement(
        self, store, s3, monkeypatch, caplog
    ):
        def broken(self, **limits):
            raise RuntimeError("no summary today")

        monkeypatch.setattr(
            routing_step.InputReader, "failed_policy_rules", broken, raising=True
        )
        key = "a.pdf"

        with caplog.at_level(logging.WARNING):
            decision = _decide(
                _document(key, _section(key, s3, "1", alert=True)), store, s3
            )

        routed = store.get_item(key).routed_by
        assert decision.decision == "hold"
        assert (routed.policy_failures, routed.policy_failure_count) == ([], 0)
        assert [
            r.getMessage()
            for r in caplog.records
            if "routing_policy_failures_failed" in r.getMessage()
        ] == ["event=routing_policy_failures_failed error=RuntimeError"]


class TestTheApiView:
    """``item_view``: the routed-by record as ``getReviewItem`` returns it."""

    def test_it_carries_the_reason_and_the_failed_policy_rules(self, store, s3):
        from idp_common.review_queues.service import item_view

        key = "a.pdf"
        document = _with_summary(
            s3,
            key,
            _section(key, s3, "1", values={"Total": 12000}),
            lending=[_failed("lending.max_ltv", "Too high.", ["3", "4"])],
        )
        table = RoutingTable(rules=[_total_rule("Total {field_value:Invoice:Total}")])

        _decide(document, store, s3, _config(table=table))

        routed_by = item_view(store.get_item(key)).dump()["routedBy"]
        assert routed_by == {
            "tableId": "review-routing",
            "ruleId": "high-value",
            "ruleLabel": "Invoices over 10,000",
            "placedQueue": "high-value",
            "fallbackReason": "",
            "inputs": json.dumps({'field_value("Invoice:Total","any")': "[12000]"}),
            "reason": "Total 12000",
            "reasonSource": "admin",
            "policyFailures": [
                {
                    "ruleId": "lending.max_ltv",
                    "policy": "lending",
                    "name": "max_ltv",
                    "reason": "Too high.",
                    "pages": [3, 4],
                }
            ],
            "policyFailureCount": 1,
        }
        # The reason as written stays on the item: the API returns what is read.
        assert "reasonTemplate" not in routed_by

    def test_an_item_placed_before_reasons_existed_has_none(self, store):
        from idp_common.review_queues.models import RoutedBy
        from idp_common.review_queues.service import item_view

        item = _seed(store, "old.pdf", routed_by=RoutedBy(rule_id="r1"))

        routed_by = item_view(item).dump()["routedBy"]

        assert (routed_by["reason"], routed_by["reasonSource"]) == (None, None)
        assert (routed_by["policyFailures"], routed_by["policyFailureCount"]) == ([], 0)


class TestRoutingWithoutPolicyRuleValidation:
    """Routing does not need policy rule validation (FR-31)."""

    @pytest.mark.parametrize("mode", ["pipeline", "bda"])
    def test_a_value_based_routing_rule_holds(self, store, s3, mode):
        from idp_common.config.feature_flags import policy_rule_validation_enabled
        from idp_common.config.models import IDPConfig

        config = IDPConfig.model_validate(
            {
                "hitl": {"enabled": True, "confidence_threshold": 0.8},
                "rule_validation": {"enabled": False},
                "classes": [dict(INVOICE, **{"$id": "Invoice"})],
                "routing": RoutingConfig(
                    tables=[RoutingTable(rules=[_total_rule()])]
                ).model_dump(),
            }
        )
        assert policy_rule_validation_enabled(config) is False
        key = f"{mode}.pdf"
        # No policy rule validation ran: the document has no summary.
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        decision = _decide(document, store, s3, config, mode=mode)

        routed = store.get_item(key).routed_by
        assert (decision.decision, decision.queue, decision.rule_id) == (
            "hold",
            "high-value",
            "high-value",
        )
        assert routed.reason == (
            "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)"
        )
        assert (routed.policy_failures, routed.policy_failure_count) == ([], 0)

    @pytest.mark.parametrize("mode", ["pipeline", "bda"])
    def test_a_profile_with_no_policies_holds_too(self, store, s3, mode):
        from idp_common.config.feature_flags import policy_rule_validation_enabled

        config = _config(table=RoutingTable(rules=[_total_rule()]))
        config.rule_validation = SimpleNamespace(enabled=True)
        config.policy_classes = []
        assert policy_rule_validation_enabled(config) is False
        key = f"{mode}.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        decision = _decide(document, store, s3, config, mode=mode)

        assert (decision.decision, decision.rule_id) == ("hold", "high-value")


def _throttled(*args, **kwargs):
    raise ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Query"
    )


def _denied(*args, **kwargs):
    raise ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Query"
    )


class TestTheQueueDefinitionsCannotBeRead:
    """A read that failed for a moment is retried, not taken for "queue off"."""

    def test_a_transient_failure_is_raised_for_the_state_to_retry(
        self, store, s3, monkeypatch
    ):
        monkeypatch.setattr(store.queue_defs, "list", _throttled)
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        with pytest.raises(TransientError, match="Default review queue definition"):
            _decide(document, store, s3)

        assert store.get_item(key) is None

    def test_the_retry_then_holds_the_document(self, store, s3, monkeypatch):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))
        with monkeypatch.context() as patched:
            patched.setattr(store.queue_defs, "list", _throttled)
            with pytest.raises(TransientError):
                _decide(document, store, s3)

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.queue) == ("hold", "default")

    def test_any_other_failure_continues_as_default_queue_off(
        self, store, s3, monkeypatch
    ):
        monkeypatch.setattr(store.queue_defs, "list", _denied)
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.reason) == ("continue", "default_queue_off")
        assert store.get_item(key) is None

    def test_with_review_routing_off_the_queue_is_not_read(
        self, store, s3, monkeypatch
    ):
        """The switch is checked first, so a throttled read cannot fail a run
        that would not have used the queue."""
        monkeypatch.setattr(store.queue_defs, "list", _throttled)
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, _config(hitl=False))

        assert (decision.decision, decision.reason) == ("continue", "hitl_off")


class TestFallbacks:
    def _table(self, queue):
        return RoutingTable(
            rules=[_rule("r1", queue, {"input": "document_class", "test": '"Invoice"'})]
        )

    @pytest.mark.parametrize(
        ("queue", "fallback"),
        [("gone", "queue_missing"), ("closed", "queue_disabled")],
    )
    def test_a_queue_that_cannot_take_the_document(self, store, s3, queue, fallback):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1"))

        decision = _decide(document, store, s3, _config(table=self._table(queue)))

        routed = store.get_item(key).routed_by
        assert (decision.decision, decision.queue, decision.rule_id) == (
            "hold",
            "default",
            "r1",
        )
        assert (routed.outcome_queue, routed.placed_queue, routed.fallback_reason) == (
            queue,
            "default",
            fallback,
        )

    def test_a_table_that_cannot_be_evaluated(self, store, s3):
        """An unreviewed document goes to the Default queue: failing closed."""
        table = RoutingTable(engine="not-an-engine")
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1"))

        decision = _decide(document, store, s3, _config(table=table))

        item = store.get_item(key)
        assert (decision.decision, decision.reason, decision.queue) == (
            "hold",
            "evaluation_error",
            "default",
        )
        assert (item.routed_by.rule_id, item.routed_by.fallback_reason) == (
            "",
            "evaluation_error",
        )
        # What the table's rules read is kept, to show what the decision had.
        assert set(item.routed_by.inputs) == {
            "reviewed",
            "extraction_failed_count",
            "confidence_alert_count",
            "route_missing_required_fields",
            "missing_required_field_count",
        }

    def test_a_storage_failure_is_raised_so_the_state_retries(self, s3):
        failing = MagicMock()
        failing.queue_defs = None
        failing.get_item.side_effect = RuntimeError("ProvisionedThroughputExceeded")
        key = "a.pdf"

        with pytest.raises(RuntimeError, match="ProvisionedThroughput"):
            decide(
                _document(key, _section(key, s3, "1", alert=True)),
                _config(),
                mode="pipeline",
                execution_arn=ARN,
                run_plan=PLAN,
                store=failing,
                s3=s3,
                now=NOW,
            )


class TestFinalizeRuns:
    @pytest.mark.parametrize("disposition", ["removed", "skipped"])
    def test_a_removed_or_skipped_review_continues(self, store, s3, disposition):
        key = "a.pdf"
        item = _finalizing(store, key, disposition)
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, plan=FINALIZE_PLAN)

        assert (decision.decision, decision.reason) == ("continue", "review_skipped")
        # The item is left for the finalize run to complete.
        assert store.get_item(key).cycle_id == item.cycle_id
        assert store.get_item(key).state == "FINALIZING"

    def test_a_completed_review_continues_with_the_built_in_table(self, store, s3):
        """Applying the review confirmed every section, so the document reads as
        reviewed and the built-in rules no longer match."""
        key = "a.pdf"
        _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", alert=True, confirmed=True))

        decision = _decide(document, store, s3, plan=FINALIZE_PLAN)

        assert (decision.decision, decision.reason) == ("continue", "default_outcome")
        assert store.get_item(key).state == "FINALIZING"

    def _second_opinion(self, limit=3):
        table = RoutingTable(
            rules=[
                _rule(
                    "second-opinion",
                    "high-value",
                    {"input": "reviewed", "test": "true"},
                    label="Second opinion",
                )
            ]
            + builtin_rules()
        )
        return _config(table=table, limit=limit)

    def test_a_rule_on_reviewed_places_the_document_again(self, store, s3):
        key = "a.pdf"
        first = _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", confirmed=True))

        decision = _decide(
            document, store, s3, self._second_opinion(), plan=FINALIZE_PLAN
        )

        item = store.get_item(key)
        assert (decision.decision, decision.queue, decision.rule_id) == (
            "hold",
            "high-value",
            "second-opinion",
        )
        assert item.cycle_id != first.cycle_id
        assert (item.state, item.queue, item.placement_count) == (
            "HOLDING",
            "high-value",
            2,
        )
        # The usage of the runs before this placement travels with it.
        assert item.carried_metering_json == first.phase1_metering_json
        assert item.version == first.version + 1
        assert _events(store, key, first.cycle_id) == [
            (
                "rerouted",
                {
                    "by_cycle": item.cycle_id,
                    "queue": "high-value",
                    "rule_id": "second-opinion",
                },
            )
        ]
        assert [event for event, _ in _events(store, key, item.cycle_id)] == ["routed"]
        # The first cycle's status object goes; activation writes the new one.
        assert s3.deleted == [(BUCKET, f"{key}/review/status.json")]

    def test_a_placement_over_the_limit_continues(self, store, s3, metrics):
        key = "a.pdf"
        second = _finalizing(store, key, "complete", placement_count=2)
        document = _document(key, _section(key, s3, "1", confirmed=True))

        decision = _decide(
            document, store, s3, self._second_opinion(limit=2), plan=FINALIZE_PLAN
        )

        assert (decision.decision, decision.reason, decision.rule_id) == (
            "continue",
            "placement_limit",
            "second-opinion",
        )
        item = store.get_item(key)
        assert (item.cycle_id, item.state) == (second.cycle_id, "FINALIZING")
        assert _events(store, key, second.cycle_id) == [
            (
                "placement_limit_reached",
                {"limit": 2, "rule_id": "second-opinion", "queue": "high-value"},
            )
        ]
        assert ("RoutingPlacementLimit", None) in metrics

    def test_a_placement_at_the_limit_is_made(self, store, s3):
        key = "a.pdf"
        _finalizing(store, key, "complete", placement_count=1)
        document = _document(key, _section(key, s3, "1", confirmed=True))

        decision = _decide(
            document, store, s3, self._second_opinion(limit=2), plan=FINALIZE_PLAN
        )

        assert decision.decision == "hold"
        assert store.get_item(key).placement_count == 2

    def test_an_evaluation_error_on_a_finalize_run_continues(self, store, s3):
        """The review was just completed: the document is not held for a second
        one because the table could not be read."""
        key = "a.pdf"
        item = _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", confirmed=True))

        decision = _decide(
            document,
            store,
            s3,
            _config(table=RoutingTable(engine="not-an-engine")),
            plan=FINALIZE_PLAN,
        )

        assert (decision.decision, decision.reason) == ("continue", "evaluation_error")
        assert store.get_item(key).state == "FINALIZING"
        assert _events(store, key, item.cycle_id) == [
            (
                "routing_evaluation_failed",
                {"table_id": "review-routing", "error": "UnknownEngine"},
            )
        ]

    def test_hitl_turned_off_during_a_review_lets_the_finalize_run_continue(
        self, store, s3
    ):
        key = "a.pdf"
        _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, _config(hitl=False), plan=FINALIZE_PLAN)

        assert (decision.decision, decision.reason) == ("continue", "hitl_off")

    def test_the_finalize_run_of_another_execution_is_not_this_one(self, store, s3):
        """The item waits for a different execution: this run is a fresh one."""
        key = "a.pdf"
        _finalizing(store, key, "rejected", finalize_execution_name="some-other-run")
        document = _document(key, _section(key, s3, "1"))

        decision = _decide(document, store, s3)

        assert (decision.decision, decision.reason) == ("continue", "default_outcome")


class TestUnplannedFinalizeRuns:
    """A finalize message that carried no plan: sent by an older release, or
    queued before the upgrade. The assembly step applied the review."""

    def test_a_rejected_review_ends_as_rejected(self, store, s3):
        key = "a.pdf"
        _finalizing(store, key, "rejected")
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, plan=None)

        assert (decision.decision, decision.reason) == ("rejected", "review_rejected")

    def test_a_disposition_this_code_does_not_know_is_rejected(self, store, s3):
        key = "a.pdf"
        _finalizing(store, key, "something-new")

        decision = _decide(_document(key, _section(key, s3, "1")), store, s3, plan=None)

        assert decision.decision == "rejected"

    def test_a_completed_review_continues_and_is_not_held_again(self, store, s3):
        """No step of the run recorded the review on the sections, so the
        document does not read as reviewed. It still must not go back to a
        queue."""
        key = "a.pdf"
        _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3, plan=None)

        assert (decision.decision, decision.reason) == (
            "continue",
            "unplanned_finalize",
        )
        assert store.get_item(key).state == "FINALIZING"

    def test_a_skipped_review_continues(self, store, s3):
        key = "a.pdf"
        _finalizing(store, key, "skipped")

        decision = _decide(_document(key, _section(key, s3, "1")), store, s3, plan=None)

        assert (decision.decision, decision.reason) == ("continue", "review_skipped")


class TestRetries:
    def test_a_retry_of_the_same_execution_keeps_the_cycle(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", alert=True))

        first = _decide(document, store, s3)
        second = _decide(document, store, s3)

        assert first.decision == second.decision == "hold"
        assert first.cycle_id == second.cycle_id
        assert store.get_item(key).version == 1

    def test_a_retry_of_a_second_placement_is_still_the_second(self, store, s3):
        table = RoutingTable(
            rules=[_rule("again", "high-value", {"input": "reviewed", "test": "true"})]
        )
        key = "a.pdf"
        first_item = _finalizing(store, key, "complete")
        document = _document(key, _section(key, s3, "1", confirmed=True))
        config = _config(table=table, limit=2)

        first = _decide(document, store, s3, config, plan=FINALIZE_PLAN)
        second = _decide(document, store, s3, config, plan=FINALIZE_PLAN)

        item = store.get_item(key)
        assert first.decision == second.decision == "hold"
        assert first.cycle_id == second.cycle_id
        assert item.placement_count == 2
        assert item.carried_metering_json == first_item.phase1_metering_json

    def test_a_new_run_replaces_a_live_review_of_an_earlier_run(self, store, s3):
        key = "a.pdf"
        old = _seed(store, key, state="OPEN")
        document = _document(key, _section(key, s3, "1", alert=True))

        decision = _decide(document, store, s3)

        item = store.get_item(key)
        assert decision.decision == "hold"
        assert (item.cycle_id != old.cycle_id, item.placement_count) == (True, 1)
        assert item.carried_metering_json is None
        assert _events(store, key, old.cycle_id) == [
            ("superseded", {"by_cycle": item.cycle_id})
        ]


class TestTheTable:
    def test_the_profiles_table_is_used(self):
        table = RoutingTable(id="mine", rules=[])

        assert routing_table(_config(table=table)) is table

    def test_a_configuration_with_no_routing_section_gets_the_built_in_table(self):
        table = routing_table(SimpleNamespace(classes=[]))

        assert [rule.id for rule in table.rules] == [r.id for r in builtin_rules()]
        assert routing_table(None).rules

    def test_evaluate_table_is_what_decide_uses(self):
        result = evaluate_table(
            RoutingTable(), {"reviewed": False, "confidence_alert_count": 2}
        )

        assert result.matched_rule_id == "builtin-confidence-alerts"
        assert result.outputs == {
            "outcome": "queue",
            "queue": "default",
            "reason": BUILTIN_CONFIDENCE_REASON,
        }
        assert routing_step.HOLD == "hold"


def _stored(table_name):
    """Every item in the review table, for a before-and-after comparison."""
    import boto3

    table = boto3.resource("dynamodb", region_name=REGION).Table(table_name)
    return sorted(
        (
            json.dumps(item, sort_keys=True, default=str)
            for item in table.scan()["Items"]
        )
    )


def _saved_profile(table):
    """A real configuration model: an unsaved table is tried on a copy of it."""
    from idp_common.config.models import IDPConfig

    return IDPConfig.model_validate(
        {
            "hitl": {"enabled": True, "confidence_threshold": 0.8},
            "classes": [dict(INVOICE, **{"$id": "Invoice"})],
            "routing": RoutingConfig(tables=[table]).model_dump(),
        }
    )


_HIGH_VALUE = {
    "input": "field_value",
    "path": "Invoice:Total",
    "test": "> 10000",
}


class TestTestingATable:
    """``test_table``: what a table decides for a document, with nothing written."""

    def _table(self, queue="high-value"):
        return RoutingTable(
            rules=[_rule("big", queue, _HIGH_VALUE, label="Invoices over 10,000")]
            + builtin_rules()
        )

    def _test(self, document, store, s3, config, routing=None):
        return routing_step.test_table(
            document, config, routing, mode="pipeline", store=store, s3=s3
        )

    def test_the_rule_that_matches_and_what_it_read(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        result = self._test(document, store, s3, _config(table=self._table()))

        assert result.decision == "hold"
        assert (result.matched_rule_id, result.rule_label) == (
            "big",
            "Invoices over 10,000",
        )
        assert (result.outcome_kind, result.outcome_queue, result.placed_queue) == (
            "queue",
            "high-value",
            "high-value",
        )
        assert result.fallback_reason == ""
        assert json.loads(result.inputs) == {
            'field_value("Invoice:Total","any")': "[12000]"
        }
        assert result.stale_references == []
        # The routing rule has no reason: the reviewer would read the fallback.
        assert (result.reason, result.reason_source) == (
            "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)",
            "fallback",
        )

    def test_a_test_does_not_count_a_fallback_reason(self, store, s3, metrics):
        """``RoutingReasonFallback`` counts placements. A test of a table places
        nothing, so the fallback it shows is not counted."""
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        result = self._test(document, store, s3, _config(table=self._table()))

        assert result.reason_source == "fallback"
        assert _fallbacks(metrics) == []

    def test_the_reason_as_the_reviewer_would_read_it(self, store, s3):
        key = "a.pdf"
        document = _document(
            key, _section(key, s3, "1", values={"Total": 12000, "Vendor": "ACME"})
        )
        table = RoutingTable(
            rules=[
                _total_rule(
                    "{field_value:Invoice:Vendor}: {field_value:Invoice:Total} is "
                    "over {condition_1.value}"
                )
            ]
        )

        result = self._test(document, store, s3, _config(table=table))

        assert (result.reason, result.reason_source) == (
            "ACME: 12000 is over 10000",
            "admin",
        )
        assert result.dump()["reasonSource"] == "admin"

    def test_a_placeholder_that_does_not_resolve_is_reported(self, store, s3):
        """The condition reads no class; the reason names one the profile lacks."""
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1"))
        table = RoutingTable(
            rules=[
                _rule(
                    "any",
                    "high-value",
                    {"input": "page_count", "test": ">= 1"},
                    reason="Statement total: {field_value:Statement:Total}",
                )
            ]
        )

        result = self._test(document, store, s3, _config(table=table))

        assert result.reason == "Statement total: (no value)"
        assert result.stale_references == ["class:Statement"]

    def test_the_default_outcome(self, store, s3):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 5}))

        result = self._test(document, store, s3, _config(table=self._table()))

        assert result.decision == "continue"
        assert (result.matched_rule_id, result.rule_label) == (None, None)
        assert (result.outcome_kind, result.outcome_queue, result.placed_queue) == (
            "no_review",
            None,
            None,
        )
        # Nobody reviews the document, so there is no reason to read.
        assert (result.reason, result.reason_source) == (None, None)

    def test_nothing_is_written(self, store, s3, review_table):
        """No item, no history, no status object, and no review withdrawn."""
        key = "a.pdf"
        _seed(store, key, state="OPEN")
        before = _stored(review_table)
        held = _document(key, _section(key, s3, "1", values={"Total": 12000}))
        clean = _document(key, _section(key, s3, "2", values={"Total": 5}))

        self._test(held, store, s3, _config(table=self._table()))
        self._test(clean, store, s3, _config(table=self._table()))

        assert _stored(review_table) == before
        assert s3.deleted == []

    @pytest.mark.parametrize(
        ("queue", "fallback"),
        [("gone", "queue_missing"), ("closed", "queue_disabled")],
    )
    def test_a_queue_that_cannot_take_the_document(self, store, s3, queue, fallback):
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))

        result = self._test(document, store, s3, _config(table=self._table(queue)))

        assert (result.decision, result.outcome_queue, result.placed_queue) == (
            "hold",
            queue,
            "default",
        )
        assert result.fallback_reason == fallback

    def test_a_table_that_cannot_be_evaluated(self, store, s3):
        """A run places such a document in the Default queue; the test says so."""
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1"))

        result = self._test(
            document, store, s3, _config(table=RoutingTable(engine="not-an-engine"))
        )

        assert (result.decision, result.placed_queue, result.fallback_reason) == (
            "hold",
            "default",
            "evaluation_error",
        )
        assert result.matched_rule_id is None
        assert (result.reason, result.reason_source) == (
            "The routing rules could not be evaluated, so the document is in the "
            "Default queue.",
            "fallback",
        )

    def test_a_reference_that_does_not_resolve_is_reported(self, store, s3):
        key = "a.pdf"
        section = _section(key, s3, "1", values={"Total": 12000})
        del s3.objects[section.extraction_result_uri]

        result = self._test(
            _document(key, section), store, s3, _config(table=self._table())
        )

        assert result.stale_references == ["section:1"]

    def test_an_unsaved_table_is_the_one_tested(self, store, s3):
        """The profile's own table places nothing; the one on trial does."""
        key = "a.pdf"
        document = _document(key, _section(key, s3, "1", values={"Total": 12000}))
        config = _saved_profile(RoutingTable(rules=[]))
        assert self._test(document, store, s3, config).decision == "continue"

        result = self._test(
            document, store, s3, config, RoutingConfig(tables=[self._table()])
        )

        assert (result.decision, result.matched_rule_id) == ("hold", "big")
        # The profile is not changed by the trial.
        assert config.routing.tables[0].rules == []

    def test_an_unsaved_table_a_save_would_refuse_is_refused(self, store, s3):
        from idp_common.config.configuration_manager import (
            ConfigurationValidationError,
        )

        key = "a.pdf"
        document = _document(key, _section(key, s3, "1"))
        config = _saved_profile(RoutingTable(rules=[]))

        with pytest.raises(ConfigurationValidationError, match="gone"):
            self._test(
                document,
                store,
                s3,
                config,
                RoutingConfig(tables=[self._table("gone")]),
            )

    @pytest.mark.parametrize("total", [5, 12000])
    @pytest.mark.parametrize("alert", [False, True])
    @pytest.mark.parametrize("queue", ["high-value", "gone", "closed"])
    def test_a_test_and_a_run_decide_alike(self, store, s3, total, alert, queue):
        key = "a.pdf"
        document = _document(
            key, _section(key, s3, "1", alert=alert, values={"Total": total})
        )
        config = _config(table=self._table(queue))

        tested = self._test(document, store, s3, config)
        decided = _decide(document, store, s3, config)

        assert tested.decision == decided.decision
        assert (tested.placed_queue or "") == decided.queue
        assert (tested.matched_rule_id or "") == decided.rule_id
        if decided.decision == "hold":
            routed = store.get_item(key).routed_by
            assert tested.fallback_reason == routed.fallback_reason
            assert json.loads(tested.inputs) == routed.inputs
            assert (tested.reason, tested.reason_source) == (
                routed.reason,
                routed.reason_source,
            )
        else:
            assert (tested.reason, tested.reason_source) == (None, None)

    def test_pytest_does_not_take_it_for_a_test(self):
        assert routing_step.test_table.__test__ is False


class TestTheProductionQueueLookup:
    """``production_queue_lookup``: what routing validation asks about a queue."""

    def _lookup(self, store):
        from idp_common.review_queues.queue_defs import production_queue_lookup

        return production_queue_lookup(store.queue_defs)

    def test_a_queue_is_found_with_its_name_and_switch(self, store):
        lookup = self._lookup(store)

        assert tuple(lookup("high-value")) == ("high-value", "High value", True)
        assert tuple(lookup("closed")) == ("closed", "Closed", False)

    def test_a_queue_that_does_not_exist_is_not_found(self, store):
        assert self._lookup(store)("gone") is None

    def test_the_default_queue_is_found_before_it_is_ever_stored(self, review_table):
        empty = ReviewStore(review_table, region=REGION)

        found = self._lookup(empty)("default")

        assert (found.id, found.enabled) == ("default", True)

    def test_a_queue_created_a_moment_ago_is_found(self, store):
        """The read goes past the queue cache."""
        store.queue_defs.list()  # fills the cache
        store.queue_defs.put(QueueDefinition(id="fresh", name="Fresh"), create=True)

        assert self._lookup(store)("fresh").name == "Fresh"

    def test_each_queue_is_read_once_per_lookup(self, store, monkeypatch):
        reads = []
        read_current = store.queue_defs.read_current
        monkeypatch.setattr(
            store.queue_defs,
            "read_current",
            lambda queue_id: reads.append(queue_id) or read_current(queue_id),
        )
        lookup = self._lookup(store)

        lookup("high-value"), lookup("high-value"), lookup("gone"), lookup("gone")

        assert reads == ["high-value", "gone"]
