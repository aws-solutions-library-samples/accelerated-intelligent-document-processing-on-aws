# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``status_object.schema.json`` and the status models say the same thing.

The schema file is the contract a consumer of ``review/status.json`` reads; the
Pydantic models are what writes the object. Each is checked against the other,
and a built object is validated against the shipped file, so neither can change
alone.
"""

import json
import typing
from datetime import datetime, timezone
from importlib import resources

import jsonschema
import pytest

from idp_common.review_queues import status_object
from idp_common.review_queues.models import (
    PolicyFailure,
    ReviewItemRecord,
    ReviewReason,
    RoutedBy,
    RunRef,
    StatusObject,
    StatusPolicyFailure,
    StatusReason,
    StatusRoutedBy,
    StatusTimestamps,
)
from idp_common.review_queues.statuses import ReviewStatus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 10, 0, 0, 0, tzinfo=timezone.utc)
CYCLE = "0123456789abcdef01234"


@pytest.fixture(scope="module")
def schema() -> dict:
    """The schema as it ships with the package."""
    text = (
        resources.files("idp_common.review_queues")
        .joinpath("status_object.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _item(**overrides) -> ReviewItemRecord:
    fields = dict(
        doc_key="claims/packet.pdf",
        cycle_id=CYCLE,
        profile="lending",
        queue="default",
        state="open",
        version=1,
        enqueued_at="2026-11-16T09:00:00.000Z",
        enqueued_at_ms=1,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _status(item, status=ReviewStatus.IN_REVIEW) -> dict:
    built = status_object.build_status(
        item,
        status,
        sequence=1,
        run_id="run-1",
        machine_ref=RunRef(run_id="run-1", manifest_uri="s3://out/m.json"),
        final_ref=RunRef(run_id="run-2", manifest_uri="s3://out/f.json"),
        now=NOW,
    )
    return built.model_dump(mode="json")


def _failures() -> list[PolicyFailure]:
    return [
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
        ),
    ]


def _reason(kind) -> ReviewReason:
    return ReviewReason(
        kind=kind,
        section_id="1",
        field="Total",
        label="Total",
        score=0.4,
        threshold=0.8,
    )


class TestAStatusObjectValidates:
    @pytest.mark.parametrize(
        "kind", typing.get_args(StatusReason.model_fields["kind"].annotation)
    )
    def test_with_a_reason_of_each_kind(self, schema, kind):
        item = _item(reasons=[_reason(kind)], reason_count=1, sections=["1"])

        jsonschema.validate(_status(item), schema)

    def test_with_no_reasons_and_no_rule(self, schema):
        document = _status(_item())

        assert document["routed_by"] is None
        jsonschema.validate(document, schema)

    def test_with_the_rule_that_placed_it(self, schema):
        item = _item(
            queue="high-value",
            routed_by=RoutedBy(
                table_id="default",
                rule_id="high-value",
                rule_label="Invoices over 10,000",
                profile="lending",
                config_revision=4,
                outcome_queue="high-value",
                placed_queue="high-value",
                inputs={'field_value("Invoice:Total")': "[12000]"},
                evaluated_at="2026-11-16T09:00:00.000Z",
            ),
        )

        document = _status(item)

        assert document["routed_by"] == {
            "table_id": "default",
            "rule_id": "high-value",
            "rule_label": "Invoices over 10,000",
            "placed_queue": "high-value",
            "fallback_reason": "",
            # An item placed before routing rules had a reason has none.
            "reason": "",
            "reason_source": "",
            "policy_failures": [],
            "policy_failure_count": 0,
        }
        jsonschema.validate(document, schema)

    def test_with_the_reason_and_the_failed_policy_rules(self, schema):
        item = _item(
            queue="high-value",
            routed_by=RoutedBy(
                table_id="review-routing",
                rule_id="high-value",
                rule_label="Invoices over 10,000",
                placed_queue="high-value",
                reason="Total 12000 is over 10000",
                reason_template="Total {field_value:Invoice:Total} is over 10000",
                reason_source="admin",
                policy_failures=_failures(),
                policy_failure_count=14,
            ),
        )

        document = _status(item)

        assert document["routed_by"] == {
            "table_id": "review-routing",
            "rule_id": "high-value",
            "rule_label": "Invoices over 10,000",
            "placed_queue": "high-value",
            "fallback_reason": "",
            "reason": "Total 12000 is over 10000",
            "reason_source": "admin",
            "policy_failures": [
                {
                    "rule_id": "lending.max_ltv",
                    "policy": "lending",
                    "name": "max_ltv",
                    "reason": "LTV is 92%, over the 80% limit.",
                    "pages": [3, 4],
                },
                {
                    "rule_id": "kyc.id_match",
                    "policy": "kyc",
                    "name": "id_match",
                    "reason": "The names differ.",
                    "pages": [],
                },
            ],
            # Of every failed policy rule; the list holds the first of them.
            "policy_failure_count": 14,
        }
        jsonschema.validate(document, schema)

    def test_the_reason_as_written_is_not_in_the_status_object(self, schema):
        """A consumer gets the text the reviewer reads, not the template."""
        item = _item(
            routed_by=RoutedBy(reason="r", reason_template="{page_count} TEMPLATE")
        )

        assert "TEMPLATE" not in json.dumps(_status(item))

    @pytest.mark.parametrize(
        "failure",
        [
            {"rule_id": "p.a", "policy": "p", "name": "a", "reason": "r"},
            {
                "rule_id": "p.a",
                "policy": "p",
                "name": "a",
                "reason": "r",
                "pages": ["3"],
            },
            {
                "rule_id": "p.a",
                "policy": "p",
                "name": "a",
                "reason": "r",
                "pages": [],
                "verdict": "fail",
            },
        ],
        ids=["no pages", "a page that is text", "a key the schema does not have"],
    )
    def test_a_failed_policy_rule_the_schema_does_not_describe_is_refused(
        self, schema, failure
    ):
        document = _status(_item(routed_by=RoutedBy(policy_failures=_failures())))
        jsonschema.validate(document, schema)
        document["routed_by"]["policy_failures"] = [failure]

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)

    @pytest.mark.parametrize(
        "missing",
        ["reason", "reason_source", "policy_failures", "policy_failure_count"],
    )
    def test_a_routed_by_block_without_a_new_field_is_refused(self, schema, missing):
        document = _status(_item(routed_by=RoutedBy(table_id="t")))
        del document["routed_by"][missing]

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)

    def test_with_a_fallback_to_the_default_queue(self, schema):
        item = _item(
            routed_by=RoutedBy(
                table_id="default",
                rule_id="r1",
                outcome_queue="gone",
                placed_queue="default",
                fallback_reason="queue_missing",
            )
        )

        document = _status(item)

        assert document["routed_by"]["fallback_reason"] == "queue_missing"
        jsonschema.validate(document, schema)

    @pytest.mark.parametrize(
        ("status", "disposition"),
        [(ReviewStatus.COMPLETE, "complete"), (ReviewStatus.REJECTED, "rejected")],
    )
    def test_when_the_review_is_finished(self, schema, status, disposition):
        item = _item(
            state="finalizing",
            disposition=disposition,
            disposed_by="reviewer@example.com",
            disposed_at="2026-11-16T09:30:00.000Z",
            reject_reason="Illegible" if disposition == "rejected" else None,
        )

        jsonschema.validate(_status(item, status), schema)

    def test_an_object_the_schema_does_not_describe_is_refused(self, schema):
        document = _status(_item())
        document["schema_version"] = "1.0"

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)


class TestTheSchemaAndTheModelsAgree:
    def test_the_version(self, schema):
        assert schema["properties"]["schema_version"] == {
            "const": status_object.SCHEMA_VERSION
        }
        assert status_object.SCHEMA_VERSION == "1.1"
        assert StatusObject.model_fields["schema_version"].default == (
            status_object.SCHEMA_VERSION
        )
        assert schema["$id"].endswith(
            f"review-status-{status_object.SCHEMA_VERSION}.json"
        )

    def test_the_reason_kinds(self, schema):
        literals = typing.get_args(StatusReason.model_fields["kind"].annotation)

        assert schema["$defs"]["reason"]["properties"]["kind"]["enum"] == list(literals)
        # And the kinds a queue item records are the kinds a status object carries.
        assert typing.get_args(ReviewReason.model_fields["kind"].annotation) == literals

    @pytest.mark.parametrize(
        ("model", "properties"),
        [
            (StatusObject, lambda s: s["properties"]),
            (StatusReason, lambda s: s["$defs"]["reason"]["properties"]),
            (StatusRoutedBy, lambda s: s["$defs"]["routedBy"]["properties"]),
            (StatusPolicyFailure, lambda s: s["$defs"]["policyFailure"]["properties"]),
            (RunRef, lambda s: s["$defs"]["runRef"]["properties"]),
            (StatusTimestamps, lambda s: s["properties"]["timestamps"]["properties"]),
        ],
    )
    def test_every_field_is_a_property_and_every_property_a_field(
        self, schema, model, properties
    ):
        assert set(model.model_fields) == set(properties(schema))

    def test_the_statuses(self, schema):
        assert schema["properties"]["status"]["enum"] == [s.value for s in ReviewStatus]

    def test_a_failed_policy_rule_is_the_same_in_the_item_and_the_status(self, schema):
        """The status copies the item's record field by field."""
        assert set(StatusPolicyFailure.model_fields) == set(PolicyFailure.model_fields)
        assert schema["$defs"]["routedBy"]["properties"]["policy_failures"] == {
            "type": "array",
            "items": {"$ref": "#/$defs/policyFailure"},
        }

    def test_every_field_of_a_block_is_required(self, schema):
        for name, model in (
            ("routedBy", StatusRoutedBy),
            ("policyFailure", StatusPolicyFailure),
        ):
            assert set(schema["$defs"][name]["required"]) == set(model.model_fields)

    def test_nothing_else_is_allowed(self, schema):
        assert schema["additionalProperties"] is False
        assert StatusObject.model_config.get("extra") == "forbid"
        for definition in schema["$defs"].values():
            assert definition["additionalProperties"] is False


class TestTheItemRecord:
    def test_an_item_written_with_its_rule_reads_back(self):
        item = _item(
            routed_by=RoutedBy(
                table_id="default",
                rule_id="high-value",
                rule_label="Invoices over 10,000",
                profile="lending",
                config_revision=4,
                outcome_queue="high-value",
                placed_queue="high-value",
                inputs={'field_value("Invoice:Total")': "[12000.5]", "page_count": "3"},
                evaluated_at="2026-11-16T09:00:00.000Z",
            ),
            placement_count=2,
            carried_metering_json='{"OCR/textract/analyze_document": {"pages": 3}}',
        )

        stored = item.to_attributes()
        back = ReviewItemRecord.from_item(stored)

        assert stored["RoutedBy"]["RuleId"] == "high-value"
        assert stored["PlacementCount"] == 2
        assert back.routed_by == item.routed_by
        assert back.placement_count == 2
        assert back.carried_metering_json == item.carried_metering_json

    def test_the_reason_and_the_failed_policy_rules_read_back(self):
        routed_by = RoutedBy(
            table_id="review-routing",
            rule_id="high-value",
            reason="Total 12000 is over 10000",
            reason_template="Total {field_value:Invoice:Total} is over 10000",
            reason_source="admin",
            policy_failures=_failures(),
            policy_failure_count=14,
        )

        stored = _item(routed_by=routed_by).to_attributes()
        back = ReviewItemRecord.from_item(stored)

        assert {
            key: stored["RoutedBy"][key]
            for key in (
                "Reason",
                "ReasonTemplate",
                "ReasonSource",
                "PolicyFailures",
                "PolicyFailureCount",
            )
        } == {
            "Reason": "Total 12000 is over 10000",
            "ReasonTemplate": "Total {field_value:Invoice:Total} is over 10000",
            "ReasonSource": "admin",
            "PolicyFailures": [
                {
                    "RuleId": "lending.max_ltv",
                    "Policy": "lending",
                    "Name": "max_ltv",
                    "Reason": "LTV is 92%, over the 80% limit.",
                    "Pages": [3, 4],
                },
                {
                    "RuleId": "kyc.id_match",
                    "Policy": "kyc",
                    "Name": "id_match",
                    "Reason": "The names differ.",
                    "Pages": [],
                },
            ],
            "PolicyFailureCount": 14,
        }
        assert back.routed_by == routed_by

    def test_an_item_placed_before_reasons_existed_still_loads(self):
        stored = _item(
            routed_by=RoutedBy(table_id="default", rule_id="r1")
        ).to_attributes()
        for name in (
            "Reason",
            "ReasonTemplate",
            "ReasonSource",
            "PolicyFailures",
            "PolicyFailureCount",
        ):
            del stored["RoutedBy"][name]

        routed_by = ReviewItemRecord.from_item(stored).routed_by

        assert (routed_by.reason, routed_by.reason_template) == ("", "")
        assert routed_by.reason_source == ""
        assert (routed_by.policy_failures, routed_by.policy_failure_count) == ([], 0)

    def test_an_item_stored_before_routing_rules_still_loads(self):
        stored = _item().to_attributes()
        for name in ("RoutedBy", "PlacementCount", "CarriedMeteringJson"):
            stored.pop(name, None)

        back = ReviewItemRecord.from_item(stored)

        assert back.routed_by is None
        assert back.placement_count == 1
        assert back.carried_metering_json is None

    def test_the_layout_and_the_restart_decision_read_back(self):
        layout = {"1": "6b86b273ff34fce1", "2": "d4735e3a265e16ee"}
        item = _item(
            placed_layout=layout,
            restart_step="extraction",
            restart_sections=["2", "3"],
        )

        stored = item.to_attributes()
        back = ReviewItemRecord.from_item(stored)

        assert stored["PlacedLayout"] == layout
        assert stored["RestartStep"] == "extraction"
        assert stored["RestartSections"] == ["2", "3"]
        assert (back.placed_layout, back.restart_step, back.restart_sections) == (
            layout,
            "extraction",
            ["2", "3"],
        )

    def test_a_review_that_is_not_completed_has_no_restart_decision(self):
        stored = _item().to_attributes()

        assert "RestartStep" not in stored
        assert (stored["PlacedLayout"], stored["RestartSections"]) == ({}, [])

    def test_an_item_stored_before_the_layout_was_recorded_still_loads(self):
        """It loads with an empty layout, which compares with nothing, and with
        no restart decision."""
        stored = _item().to_attributes()
        for name in ("PlacedLayout", "RestartStep", "RestartSections"):
            stored.pop(name, None)

        back = ReviewItemRecord.from_item(stored)

        assert back.placed_layout == {}
        assert back.restart_step is None
        assert back.restart_sections == []

    def test_an_item_with_more_than_this_code_knows_still_loads(self):
        """Older code reads an item a newer release wrote."""
        stored = _item(routed_by=RoutedBy(table_id="default")).to_attributes()
        stored["RoutedBy"]["SomethingNewer"] = "x"
        stored["AFieldFromLater"] = 1

        assert ReviewItemRecord.from_item(stored).routed_by.table_id == "default"
