# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``routing`` section of ``IDPConfig``: defaults, tolerance, storage.

The models accept more than a saved profile may hold, because a document is
processed with the revision it is pinned to and that revision must always load.
What a profile may hold is checked at save (``test_routing_validation.py``).
"""

import pytest
from pydantic import ValidationError, create_model

from idp_common.config import routing_models
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import (
    ConfigurationRecord,
    IDPConfig,
    collect_ignored_config_keys,
)
from idp_common.config.routing_models import (
    BUILTIN_CONFIDENCE_REASON,
    BUILTIN_EXTRACTION_FAILED_REASON,
    BUILTIN_MISSING_REQUIRED_REASON,
    RoutingAction,
    RoutingCondition,
    RoutingConfig,
    RoutingDefaultOutcome,
    RoutingRule,
    RoutingTable,
    builtin_rules,
    queue_actions,
    review_queue_of,
)

pytestmark = pytest.mark.unit


def _custom_routing() -> dict:
    return {
        "tables": [
            {
                "id": "review-routing",
                "stage": "after_policy_rule_validation",
                "hit_policy": "FIRST",
                "engine": "builtin",
                "rules": [
                    {
                        "id": "approvals-high-value",
                        "label": "High-value invoices",
                        "enabled": True,
                        "conditions": [
                            {
                                "input": "field_value",
                                "path": "Invoice:Total",
                                "aggregate": "any",
                                "test": "> 1000",
                            }
                        ],
                        "actions": [{"type": "review_queue", "queue": "approvals"}],
                        "reason": "The total is {field_value:Invoice:Total}.",
                    },
                    {
                        "id": "compliance-policy-failure",
                        "label": "",
                        "enabled": False,
                        "conditions": [
                            {
                                "input": "failed_policy_rule_count",
                                "path": "",
                                "aggregate": "any",
                                "test": "> 0",
                            }
                        ],
                        "actions": [{"type": "review_queue", "queue": "compliance"}],
                        "reason": "",
                    },
                    {
                        "id": "everything-else",
                        "label": "No review",
                        "enabled": True,
                        "conditions": [],
                        "actions": [],
                        "reason": "",
                    },
                ],
                "default_outcome": {
                    "actions": [{"type": "review_queue", "queue": "default"}],
                    "reason": "No routing rule matched.",
                },
            }
        ],
        "max_consecutive_placements": 5,
    }


class TestDefaults:
    def test_a_profile_with_no_routing_gets_the_built_in_table(self):
        routing = IDPConfig().routing

        assert routing == RoutingConfig()
        (table,) = routing.tables
        assert table.id == "review-routing"
        assert table.stage == "after_policy_rule_validation"
        assert table.hit_policy == "FIRST"
        assert table.engine == "builtin"
        assert table.rules == builtin_rules()
        assert table.default_outcome == RoutingDefaultOutcome(actions=[], reason="")
        assert routing.max_consecutive_placements == 3

    def test_the_built_in_rules(self):
        failed, confidence, missing = builtin_rules()

        # First: a section whose extraction failed has no fields, so its required
        # fields are missing too, and the first match gives the reviewer the reason.
        assert failed == RoutingRule(
            id="builtin-extraction-failed",
            label="Failed extractions",
            enabled=True,
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="extraction_failed_count", test="> 0"),
            ],
            actions=[RoutingAction(type="review_queue", queue="default")],
            reason=(
                "Sections whose extraction failed: {extraction_failed_count}. They "
                "have no extracted values: enter them from the document."
            ),
        )
        assert confidence == RoutingRule(
            id="builtin-confidence-alerts",
            label="Confidence alerts",
            enabled=True,
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="confidence_alert_count", test="> 0"),
            ],
            actions=[RoutingAction(type="review_queue", queue="default")],
            reason=(
                "Fields below the confidence threshold: {confidence_alert_count}. "
                "Check the highlighted fields and correct them."
            ),
        )
        assert missing == RoutingRule(
            id="builtin-missing-required",
            label="Missing required fields",
            enabled=True,
            conditions=[
                RoutingCondition(input="reviewed", test="false"),
                RoutingCondition(input="route_missing_required_fields", test="true"),
                RoutingCondition(input="missing_required_field_count", test="> 0"),
            ],
            actions=[RoutingAction(type="review_queue", queue="default")],
            reason=(
                "Required fields with no value: {missing_required_field_count}. "
                "Fill them in from the document."
            ),
        )

    def test_the_built_in_rules_carry_actions_and_reasons(self):
        failed, confidence, missing = IDPConfig().routing.tables[0].rules

        assert [a.model_dump() for a in failed.actions] == [
            {"type": "review_queue", "queue": "default"}
        ]
        assert [a.model_dump() for a in confidence.actions] == [
            {"type": "review_queue", "queue": "default"}
        ]
        assert [a.model_dump() for a in missing.actions] == [
            {"type": "review_queue", "queue": "default"}
        ]
        assert failed.reason == BUILTIN_EXTRACTION_FAILED_REASON
        assert confidence.reason == BUILTIN_CONFIDENCE_REASON
        assert missing.reason == BUILTIN_MISSING_REQUIRED_REASON
        # Each reason shows the count its routing rule tests.
        assert "{extraction_failed_count}" in failed.reason
        assert "{confidence_alert_count}" in confidence.reason
        assert "{missing_required_field_count}" in missing.reason

    def test_built_in_rules_are_new_instances_on_each_call(self):
        first, second = builtin_rules(), builtin_rules()
        first[0].conditions[1].test = "> 99"

        assert second[0].conditions[1].test == "> 0"
        assert IDPConfig().routing.tables[0].rules[0].conditions[1].test == "> 0"
        assert IDPConfig().routing.tables[0] is not IDPConfig().routing.tables[0]

    def test_a_rule_needs_only_an_id(self):
        rule = RoutingRule(id="r")

        assert rule.enabled is True
        assert rule.conditions == []
        assert rule.actions == []
        assert rule.reason == ""
        assert RoutingAction() == RoutingAction(type="review_queue", queue="")
        assert RoutingCondition(input="reviewed") == RoutingCondition(
            input="reviewed", path="", aggregate="any", test="-"
        )


class TestActions:
    def test_the_review_queue_of_a_list_of_actions(self):
        assert review_queue_of([]) == ""
        assert review_queue_of([RoutingAction(queue="approvals")]) == "approvals"
        # The first review_queue action decides; another type is passed over.
        assert (
            review_queue_of(
                [
                    RoutingAction(type="notify", queue="ignored"),
                    RoutingAction(queue="first"),
                    RoutingAction(queue="second"),
                ]
            )
            == "first"
        )
        assert review_queue_of([RoutingAction(type="notify", queue="x")]) == ""

    def test_the_actions_for_a_queue(self):
        assert queue_actions("approvals") == [
            RoutingAction(type="review_queue", queue="approvals")
        ]
        assert queue_actions("") == []
        assert review_queue_of(queue_actions("approvals")) == "approvals"

    def test_the_single_outcome_model_is_gone(self):
        assert not hasattr(routing_models, "RoutingOutcome")
        assert not hasattr(routing_models, "OUTCOME_QUEUE")
        assert not hasattr(routing_models, "OUTCOME_NO_REVIEW")
        assert "outcome" not in RoutingRule.model_fields


class TestMaxConsecutivePlacements:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("5", 5), (5, 5), ("5.0", 5), (5.0, 5), (0, 1), (-3, 1), (99, 10), ("10", 10)],
    )
    def test_whole_numbers_are_accepted_and_clamped(self, value, expected):
        routing = RoutingConfig(max_consecutive_placements=value)

        assert routing.max_consecutive_placements == expected

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_an_empty_value_is_the_default(self, value):
        assert (
            RoutingConfig(max_consecutive_placements=value).max_consecutive_placements
            == 3
        )

    @pytest.mark.parametrize("value", ["many", 2.5, True, [], {}, "1e400"])
    def test_a_malformed_value_is_a_validation_error_naming_the_field(self, value):
        with pytest.raises(ValidationError) as raised:
            RoutingConfig(max_consecutive_placements=value)

        assert "max_consecutive_placements" in str(raised.value)


class TestTolerance:
    """A pinned revision must load, whatever newer code wrote into it."""

    def test_values_this_release_does_not_know_still_load(self):
        routing = RoutingConfig.model_validate(
            {
                "tables": [
                    {
                        "id": "t",
                        "stage": "after_extraction",
                        "hit_policy": "COLLECT",
                        "engine": "external",
                        "rules": [
                            {
                                "id": "r",
                                "conditions": [
                                    {
                                        "input": "an_input_added_later",
                                        "aggregate": "median",
                                        "test": "not a test at all",
                                    }
                                ],
                                "actions": [{"type": "lambda", "queue": ""}],
                            }
                        ],
                    }
                ]
            }
        )

        table = routing.tables[0]
        assert (table.stage, table.hit_policy, table.engine) == (
            "after_extraction",
            "COLLECT",
            "external",
        )
        assert table.rules[0].actions[0].type == "lambda"

    def test_a_stored_rule_with_the_single_outcome_loads_with_no_actions(self):
        """No release wrote the single-outcome shape; a stored one must still load.

        The key is dropped, so the rule reads as "not reviewed". Saving that shape
        is refused (``test_routing_validation.py``).
        """
        routing = RoutingConfig.model_validate(
            {
                "tables": [
                    {
                        "rules": [
                            {
                                "id": "r",
                                "outcome": {"kind": "queue", "queue": "approvals"},
                            }
                        ],
                        "default_outcome": {"kind": "queue", "queue": "default"},
                    }
                ]
            }
        )

        table = routing.tables[0]
        assert table.rules[0].actions == []
        assert table.rules[0].reason == ""
        assert table.default_outcome == RoutingDefaultOutcome()

    def test_an_empty_table_list_loads(self):
        assert RoutingConfig(tables=[]).tables == []

    def test_no_field_is_a_literal_a_union_or_optional(self):
        """The shape rule in one place, so a later field cannot break pinned reads."""
        import typing

        for model in (
            RoutingConfig,
            RoutingTable,
            RoutingRule,
            RoutingCondition,
            RoutingAction,
            RoutingDefaultOutcome,
        ):
            for name, field in model.model_fields.items():
                annotation = field.annotation
                origin = typing.get_origin(annotation)
                assert origin is not typing.Literal, f"{model.__name__}.{name}"
                assert origin is not typing.Union, f"{model.__name__}.{name}"
                assert field.default is not None, f"{model.__name__}.{name}"
                assert not (
                    isinstance(field.default, int)
                    and not isinstance(field.default, bool)
                    and field.default == 0
                ), f"{model.__name__}.{name}"

    def test_an_unknown_key_inside_a_rule_is_reported(self):
        data = {"routing": _custom_routing()}
        data["routing"]["tables"][0]["rules"][0]["priorty"] = 3
        data["routing"]["tables"][0]["rules"][0]["actions"][0]["quue"] = "x"

        findings = collect_ignored_config_keys(data, IDPConfig)

        paths = {finding.path for finding in findings}
        assert any(path.endswith("rules[0].priorty") for path in paths), paths
        assert any(path.endswith("rules[0].actions[0].quue") for path in paths), paths

    def test_older_model_drops_routing(self):
        """Rollback: code from before this section reads a profile that has it.

        The older ``IDPConfig`` is this one without the ``routing`` field, with the
        same model configuration. It drops the key and the profile loads.
        """
        older_fields = {
            name: (field.annotation, field)
            for name, field in IDPConfig.model_fields.items()
            if name != "routing"
        }
        older_config = create_model(  # type: ignore[call-overload]
            "OlderConfig", __config__=IDPConfig.model_config, **older_fields
        )
        body = IDPConfig(notes="saved by newer code", routing=_custom_routing())
        stored = body.model_dump(mode="python")
        assert (
            stored["routing"]["tables"][0]["rules"][0]["id"] == "approvals-high-value"
        )

        older = older_config.model_validate(stored)

        assert "routing" not in older_config.model_fields
        assert older.notes == "saved by newer code"
        assert "routing" not in older.model_dump()
        # What the older code re-saves has no routing section, so reading it with
        # this release gives the built-in table again.
        assert IDPConfig.model_validate(older.model_dump()).routing == RoutingConfig()


class TestStorageRoundTrip:
    @pytest.mark.parametrize(
        "routing", [None, _custom_routing()], ids=["default", "custom"]
    )
    def test_a_stored_record_reads_back_with_the_same_routing(self, routing):
        config = IDPConfig() if routing is None else IDPConfig(routing=routing)
        record = ConfigurationRecord(
            configuration_type=CONFIG_TYPE_CONFIG, version="lending", config=config
        )

        restored = ConfigurationRecord.from_dynamodb_item(record.to_dynamodb_item())

        assert restored.config.routing == config.routing
        assert restored.config.routing.model_dump() == config.routing.model_dump()

    def test_the_custom_table_survives_with_its_order_and_flags(self):
        config = IDPConfig(routing=_custom_routing())
        record = ConfigurationRecord(
            configuration_type=CONFIG_TYPE_CONFIG, version="lending", config=config
        )

        restored = ConfigurationRecord.from_dynamodb_item(record.to_dynamodb_item())

        (table,) = restored.config.routing.tables
        assert [rule.id for rule in table.rules] == [
            "approvals-high-value",
            "compliance-policy-failure",
            "everything-else",
        ]
        assert [rule.enabled for rule in table.rules] == [True, False, True]
        assert table.rules[0].conditions[0].test == "> 1000"
        assert table.rules[0].actions == [RoutingAction(queue="approvals")]
        assert table.rules[0].reason == "The total is {field_value:Invoice:Total}."
        assert table.rules[2].actions == []
        assert table.default_outcome == RoutingDefaultOutcome(
            actions=[RoutingAction(type="review_queue", queue="default")],
            reason="No routing rule matched.",
        )
        assert restored.config.routing.max_consecutive_placements == 5
