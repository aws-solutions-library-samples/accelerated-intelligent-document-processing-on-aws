# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Save-time validation of the ``routing`` section.

Every rule ``validate_routing`` enforces has a failing case here whose message
names the table, the rule and the input or the reason placeholder, and the
write-boundary cases drive the real ``ConfigurationManager`` to show when the
validation runs and when it does not. ``routing_warnings`` is the other thing a
save reports: what an accepted section will not do.
"""

import copy

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import (
    ConfigurationManager,
    ConfigurationValidationError,
)
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.routing_validation import (
    MAX_CONDITIONS,
    MAX_LABEL_BYTES,
    MAX_PATH_BYTES,
    MAX_REASON_BYTES,
    MAX_REASON_PLACEHOLDERS,
    MAX_RULES,
    MAX_SECTION_BYTES,
    MAX_TEST_BYTES,
    MODE_CHOICES,
    PATH_CLASS,
    ROUTING_INPUTS,
    VERDICT_CHOICES,
    QueueInfo,
    class_names,
    policy_input,
    policy_rule_ids,
    refuse_single_outcome_shape,
    routing_input,
    routing_warnings,
    validate_routing,
)

pytestmark = pytest.mark.unit

_CLASSES = [
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Invoice",
        "x-aws-idp-document-type": "Invoice",
        "type": "object",
        "properties": {
            "Total": {"type": "number"},
            "Vendor": {"type": "string"},
            "Paid": {"type": "boolean"},
            "Address": {
                "type": "object",
                "properties": {"City": {"type": "string"}},
            },
            "Lines": {
                "type": "array",
                "items": {"$ref": "#/$defs/Line"},
            },
        },
        "$defs": {
            "Line": {
                "type": "object",
                "properties": {"Amount": {"type": "number"}, "Sku": {"type": "string"}},
            }
        },
    },
    {
        "$id": "W2",
        "x-aws-idp-document-type": "W2",
        "type": "object",
        "properties": {"Wages": {"type": "number"}, "Vendor": {"type": "number"}},
    },
]

_POLICY_CLASSES = [
    {
        "$id": "invoice_controls",
        "x-aws-idp-policy-type": "invoice_controls",
        "rule_properties": {
            "total_matches_lines": {"description": "The total equals the line sum"},
            "vendor_is_approved": {"description": "The vendor is on the list"},
        },
    },
    {
        # The legacy discriminator still names a policy type.
        "$id": "legacy_policy",
        "x-aws-idp-rule-type": "legacy_policy",
        "rule_properties": {"signed": {"description": "The form is signed"}},
    },
]


def _condition(input_name, test="-", path="", aggregate="any"):
    return {"input": input_name, "path": path, "aggregate": aggregate, "test": test}


def _queue(queue="approvals"):
    """The actions that send a document to ``queue``."""
    return [{"type": "review_queue", "queue": queue}]


def _rule(rule_id="r1", conditions=None, actions=None, **extra):
    return {
        "id": rule_id,
        "conditions": conditions or [],
        "actions": _queue() if actions is None else actions,
        **extra,
    }


def _config(rules=None, policy_classes=None, rule_validation=None, **table):
    body = {"rules": rules if rules is not None else [_rule()], **table}
    return IDPConfig(
        classes=copy.deepcopy(_CLASSES),
        policy_classes=copy.deepcopy(
            _POLICY_CLASSES if policy_classes is None else policy_classes
        ),
        routing={"tables": [body]},
        **({"rule_validation": rule_validation} if rule_validation else {}),
    )


def _refused(config, **kwargs) -> str:
    with pytest.raises(ConfigurationValidationError) as raised:
        validate_routing(config, **kwargs)
    return str(raised.value)


def _condition_refused(condition) -> str:
    message = _refused(_config([_rule("the-rule", [condition])]))
    # Every condition problem names the table, the rule and the input.
    assert message.startswith(
        f"Routing table 'review-routing', rule 'the-rule', input '{condition['input']}"
    ), message
    return message


class TestAcceptedTables:
    def test_the_built_in_table_is_valid(self):
        validate_routing(IDPConfig())

    def test_a_table_using_every_input_is_valid(self):
        conditions = [
            _condition("document_class", '"Invoice"'),
            _condition("section_classes", '"Invoice", "W2"', aggregate="all"),
            _condition("section_classes", "> 1", aggregate="count"),
            _condition("page_count", "[1..50]"),
            _condition("section_count", ">= 1"),
            _condition("confidence_alert_count", "> 0"),
            _condition("missing_required_field_count", "0"),
            _condition("route_missing_required_fields", "true"),
        ]
        more = [
            _condition("field_value", "> 1000", path="Invoice:Total"),
            _condition("field_value", '"Acme"', path="Invoice:Vendor"),
            _condition("field_value", "true", path="Invoice:Paid"),
            _condition("field_value", '"Paris"', path="Invoice:Address.City"),
            _condition("field_value", "> 5", path="Invoice:Lines[0].Amount"),
            _condition("field_value", "> 2", path="Invoice:Lines", aggregate="count"),
            _condition(
                "policy_rule_verdict",
                '"fail"',
                path="invoice_controls.total_matches_lines",
            ),
            _condition(
                "policy_rule_verdict", 'not("pass")', path="legacy_policy.signed"
            ),
        ]
        last = [
            _condition("failed_policy_rule_count", "> 0"),
            _condition("failed_policy_rule_count", "> 0", path="invoice_controls"),
            _condition("reviewed", "false"),
            _condition("processing_mode", '"bda"'),
            _condition("class_present", "true", path="W2"),
            _condition("field_value", "null", path="Wages"),
            _condition("field_value", '< date("2026-01-01")', path="Invoice:Vendor"),
        ]
        config = _config(
            [
                _rule("first", conditions),
                _rule("second", more),
                _rule("third", last, actions=[]),
            ],
            default_outcome={"actions": _queue("default")},
        )

        validate_routing(config)

    def test_a_reason_using_every_kind_of_placeholder_is_valid(self):
        conditions = [
            _condition("field_value", "> 1000", path="Invoice:Total"),
            _condition("document_class", '"Invoice"'),
        ]
        reason = (
            "{{Held}}: total {field_value:Invoice:Total} is over "
            "{condition_1.value} for a {condition_2.value} of {page_count} pages; "
            "{policy_rule_verdict:invoice_controls.total_matches_lines}, "
            "{failed_policy_rule_count} failed, "
            "{failed_policy_rule_count:invoice_controls} of them invoice controls; "
            "W2 present: {class_present:W2}; classes {section_classes}; "
            "threshold {confidence_threshold}."
        )
        config = _config(
            [_rule("first", conditions, reason=reason)],
            default_outcome={
                "actions": _queue("default"),
                "reason": "No routing rule matched; {page_count} pages.",
            },
        )

        validate_routing(config)

    def test_a_rule_with_no_conditions_is_valid(self):
        validate_routing(_config([_rule(conditions=[])]))

    def test_the_catalogue_covers_what_the_built_in_rules_use(self):
        used = {
            condition.input
            for rule in IDPConfig().routing.tables[0].rules
            for condition in rule.conditions
        }
        assert used <= {entry.name for entry in ROUTING_INPUTS}
        assert routing_input("field_value").needs_path is True
        assert routing_input("failed_policy_rule_count").path_optional is True
        assert routing_input("nope") is None

    def test_the_catalogue_entries_this_release_adds(self):
        present = routing_input("class_present")
        assert (
            present.label,
            present.type,
            present.needs_path,
            present.is_list,
            present.path_kind,
        ) == ("Class is present", "boolean", True, False, PATH_CLASS)
        assert PATH_CLASS == "class"
        # It follows the section classes in the order the editor offers them.
        names = [entry.name for entry in ROUTING_INPUTS]
        assert names.index("class_present") == names.index("section_classes") + 1

    def test_the_inputs_with_a_fixed_set_of_values(self):
        assert routing_input("policy_rule_verdict").choices == VERDICT_CHOICES
        assert routing_input("processing_mode").choices == MODE_CHOICES
        assert MODE_CHOICES == ("pipeline", "bda")
        with_choices = {entry.name for entry in ROUTING_INPUTS if entry.choices}
        assert with_choices == {"policy_rule_verdict", "processing_mode"}

    def test_the_verdict_choices_are_the_verdicts_of_policy_rule_validation(self):
        """Restated in the routing module, which must not load the solver."""
        from idp_common.rule_validation.orchestrator import VERDICTS

        assert VERDICT_CHOICES == VERDICTS

    def test_which_inputs_read_a_policy_rule_result(self):
        assert {entry.name for entry in ROUTING_INPUTS if policy_input(entry.name)} == {
            "policy_rule_verdict",
            "failed_policy_rule_count",
        }
        assert policy_input("nope") is False

    def test_the_class_names_of_a_configuration(self):
        assert class_names(_config().classes) == ["Invoice", "W2"]
        assert class_names(None) == []
        assert class_names([{"x-aws-idp-document-type": True}, "not a schema"]) == []


class TestTableLevelRules:
    def test_no_table_is_refused(self):
        config = IDPConfig(routing={"tables": []})

        assert "exactly one table; it holds 0" in _refused(config)

    def test_two_tables_are_refused(self):
        config = IDPConfig(routing={"tables": [{"id": "a"}, {"id": "b"}]})

        assert "exactly one table; it holds 2" in _refused(config)

    def test_a_bad_table_id_is_refused(self):
        message = _refused(_config(id="Review Routing"))

        assert message.startswith("Routing table 'Review Routing': the table id")

    def test_another_stage_is_refused(self):
        message = _refused(_config(stage="after_extraction"))

        assert message.startswith("Routing table 'review-routing': the stage")
        assert "'after_extraction'" in message
        assert "after_policy_rule_validation" in message

    @pytest.mark.parametrize("hit_policy", ["UNIQUE", "COLLECT", "first"])
    def test_another_hit_policy_is_refused_by_name(self, hit_policy):
        message = _refused(_config(hit_policy=hit_policy))

        assert message.startswith("Routing table 'review-routing': the hit policy")
        assert f"'{hit_policy}'" in message
        assert "FIRST" in message

    def test_an_unregistered_engine_is_refused(self):
        message = _refused(_config(engine="camunda"))

        assert "the engine 'camunda' is not registered" in message
        assert "builtin" in message


class TestRuleLevelRules:
    @pytest.mark.parametrize(
        "rule_id", ["", "Has Space", "UPPER", "-leading", "x" * 65]
    )
    def test_a_bad_rule_id_is_refused(self, rule_id):
        message = _refused(_config([_rule(rule_id)]))

        # A rule with no id is named by its position in the table.
        named = rule_id or "#1"
        assert message.startswith(f"Routing table 'review-routing', rule '{named}':")
        assert "rule id" in message

    def test_a_duplicate_rule_id_is_refused(self):
        message = _refused(_config([_rule("same"), _rule("other"), _rule("same")]))

        assert message == (
            "Routing table 'review-routing', rule 'same': the rule id is used more "
            "than once"
        )

    def test_a_long_label_is_refused_by_bytes_not_characters(self):
        fits = "é" * (MAX_LABEL_BYTES // 2)
        validate_routing(_config([_rule(label=fits)]))

        message = _refused(_config([_rule(label=fits + "é")]))

        assert f"the label is longer than {MAX_LABEL_BYTES} bytes" in message


class TestActionRules:
    def test_no_action_is_no_review(self):
        validate_routing(_config([_rule(actions=[])]))
        validate_routing(_config(default_outcome={"actions": []}))

    def test_an_unknown_action_type_is_refused_by_name(self):
        message = _refused(
            _config([_rule(actions=[{"type": "lambda", "queue": "approvals"}])])
        )

        assert message == (
            "Routing table 'review-routing', rule 'r1', action 1: the action type "
            "'lambda' is not supported; the only action type is 'review_queue'"
        )

    def test_the_unsupported_action_is_named_by_its_position(self):
        actions = _queue() + [{"type": "notify", "queue": ""}]

        message = _refused(_config([_rule(actions=actions)]))

        assert message.startswith(
            "Routing table 'review-routing', rule 'r1', action 2:"
        )
        assert "'notify'" in message

    def test_two_queue_actions_are_refused(self):
        message = _refused(
            _config([_rule(actions=_queue("approvals") + _queue("compliance"))])
        )

        assert message == (
            "Routing table 'review-routing', rule 'r1': a document goes to one "
            "review queue; this outcome has 2 review_queue actions"
        )

    def test_a_queue_action_needs_a_queue(self):
        message = _refused(_config([_rule(actions=_queue(""))]))

        assert message == (
            "Routing table 'review-routing', rule 'r1': the review_queue action "
            "names no queue"
        )

    def test_a_queue_id_must_have_the_queue_id_form(self):
        message = _refused(_config([_rule(actions=_queue("Approvals Team"))]))

        assert "'Approvals Team' is not a review queue id" in message

    def test_the_default_outcome_is_checked_too(self):
        message = _refused(_config(default_outcome={"actions": _queue("")}))

        assert message == (
            "Routing table 'review-routing', default outcome: the review_queue "
            "action names no queue"
        )

    def test_the_default_outcomes_action_type_is_checked(self):
        message = _refused(
            _config(default_outcome={"actions": [{"type": "lambda", "queue": "q"}]})
        )

        assert message.startswith(
            "Routing table 'review-routing', default outcome, action 1: the action "
            "type 'lambda' is not supported"
        )

    def test_the_default_outcome_takes_one_queue_too(self):
        message = _refused(
            _config(default_outcome={"actions": _queue("a") + _queue("b")})
        )

        assert message.startswith("Routing table 'review-routing', default outcome:")
        assert "this outcome has 2 review_queue actions" in message


class TestQueueLookup:
    _QUEUES = {
        "default": QueueInfo("default", "Default", True),
        "approvals": QueueInfo("approvals", "Approvals", True),
        "paused": QueueInfo("paused", "Paused", False),
    }

    def _lookup(self, queue_id):
        # A Test Studio queue is not a production queue, so the lookup has none.
        return self._QUEUES.get(queue_id)

    def test_an_existing_queue_is_accepted(self):
        validate_routing(_config(), queue_lookup=self._lookup)

    def test_a_missing_queue_is_refused_when_a_lookup_is_given(self):
        config = _config([_rule(actions=_queue("gone"))])

        message = _refused(config, queue_lookup=self._lookup)

        assert message.startswith("Routing table 'review-routing', rule 'r1':")
        assert "the review queue 'gone' does not exist" in message

    def test_a_missing_queue_is_accepted_when_no_lookup_is_given(self):
        validate_routing(_config([_rule(actions=_queue("gone"))]))

    def test_a_test_studio_queue_is_refused(self):
        config = _config([_rule(actions=_queue("ts-set-7"))])

        message = _refused(config, queue_lookup=self._lookup)

        assert "'ts-set-7' does not exist, or it is not a production queue" in message

    def test_a_disabled_queue_is_accepted(self):
        config = _config([_rule(actions=_queue("paused"))])

        validate_routing(config, queue_lookup=self._lookup)

    def test_the_default_outcomes_queue_is_looked_up(self):
        config = _config(default_outcome={"actions": _queue("gone")})

        message = _refused(config, queue_lookup=self._lookup)

        assert message.startswith("Routing table 'review-routing', default outcome:")

    def test_an_outcome_with_no_action_looks_nothing_up(self):
        asked = []

        def lookup(queue_id):
            asked.append(queue_id)
            return self._QUEUES.get(queue_id)

        validate_routing(_config([_rule(actions=[])]), queue_lookup=lookup)

        assert asked == []


def _reason_refused(reason, conditions=None, **rule) -> str:
    """The refusal of a routing rule's reason; it names the table and the rule."""
    message = _refused(
        _config([_rule("the-rule", conditions or [], reason=reason, **rule)])
    )
    assert message.startswith("Routing table 'review-routing', rule 'the-rule'"), (
        message
    )
    return message


def _placeholder_refused(placeholder, conditions=None) -> str:
    """The refusal of one placeholder; it names the table, the rule and it."""
    message = _reason_refused(f"Held: {placeholder}.", conditions)
    assert message.startswith(
        "Routing table 'review-routing', rule 'the-rule', reason placeholder "
        f"'{placeholder}': "
    ), message
    return message


class TestReasonRules:
    def test_an_empty_reason_is_valid_on_any_outcome(self):
        validate_routing(_config([_rule(reason="")]))
        validate_routing(_config([_rule(actions=[], reason="")]))

    def test_the_reason_limit_is_in_bytes_not_characters(self):
        fits = "é" * (MAX_REASON_BYTES // 2)
        validate_routing(_config([_rule(reason=fits)]))

        message = _reason_refused(fits + "é")

        assert message == (
            "Routing table 'review-routing', rule 'the-rule': the reason is "
            f"longer than {MAX_REASON_BYTES} bytes"
        )
        assert MAX_REASON_BYTES == 500

    def test_a_reason_on_an_outcome_with_no_queue_is_refused(self):
        message = _reason_refused("Nobody reads this.", actions=[])

        assert message == (
            "Routing table 'review-routing', rule 'the-rule': a reason is shown "
            "to a reviewer, and this outcome sends the document to no review "
            "queue; remove the reason or choose a queue"
        )

    @pytest.mark.parametrize(
        ("reason", "problem"),
        [
            ("Total {field_value", "an unmatched '{' at character 7"),
            ("Total 5}", "an unmatched '}' at character 8"),
            ("Total {}", "an empty placeholder at character 7"),
            ("Total {field_value:}", "has no path after ':'"),
        ],
    )
    def test_a_reason_that_does_not_parse_is_refused(self, reason, problem):
        message = _reason_refused(reason)

        assert message.startswith(
            "Routing table 'review-routing', rule 'the-rule', reason: "
        )
        assert problem in message

    def test_doubled_braces_are_text(self):
        validate_routing(_config([_rule(reason="Write {{page_count}} for the count.")]))

    def test_the_placeholder_limit(self):
        at_limit = " ".join(["{page_count}"] * MAX_REASON_PLACEHOLDERS)
        validate_routing(_config([_rule(reason=at_limit)]))

        message = _reason_refused(at_limit + " {section_count}")

        assert message == (
            "Routing table 'review-routing', rule 'the-rule': the reason has "
            f"{MAX_REASON_PLACEHOLDERS + 1} placeholders; the limit is "
            f"{MAX_REASON_PLACEHOLDERS}"
        )
        assert MAX_REASON_PLACEHOLDERS == 10

    def test_an_unknown_input(self):
        message = _placeholder_refused("{document_colour}")

        assert "'document_colour' is not a routing input (inputs: " in message
        assert "field_value" in message

    @pytest.mark.parametrize(
        "placeholder",
        ["{field_value}", "{policy_rule_verdict}", "{class_present}"],
    )
    def test_an_input_that_needs_a_path(self, placeholder):
        message = _placeholder_refused(placeholder)

        assert message.endswith(
            "this input needs a path, for example {field_value:Invoice:Total}"
        )

    def test_an_input_that_takes_no_path(self):
        message = _placeholder_refused("{page_count:Invoice}")

        assert message.endswith("this input takes no path")

    def test_a_field_path_in_an_unknown_class(self):
        message = _placeholder_refused("{field_value:Receipt:Total}")

        assert "'Receipt' is not a class of this profile (classes: Invoice, W2)" in (
            message
        )

    def test_a_field_path_the_class_does_not_have(self):
        message = _placeholder_refused("{field_value:Invoice:Totl}")

        assert "'Totl' is not a field of the class" in message

    def test_a_field_in_a_list_needs_a_row(self):
        message = _placeholder_refused("{field_value:Invoice:Lines.Amount}")

        assert "'Lines' is a list" in message

    def test_a_field_path_with_a_row_is_valid(self):
        validate_routing(
            _config([_rule(reason="First line: {field_value:Invoice:Lines[0].Amount}")])
        )

    def test_a_policy_rule_id_the_policy_schema_does_not_have(self):
        message = _placeholder_refused("{policy_rule_verdict:invoice_controls.missing}")

        assert message.endswith(
            "this policy rule id is not in the Policy Schema; an id is "
            "<policy type>.<rule key>"
        )

    def test_a_policy_type_the_policy_schema_does_not_have(self):
        message = _placeholder_refused("{failed_policy_rule_count:tax_controls}")

        assert message.endswith("this policy type is not in the Policy Schema")

    def test_a_class_the_profile_does_not_have(self):
        message = _placeholder_refused("{class_present:Receipt}")

        assert message.endswith(
            "'Receipt' is not a class of this profile (classes: Invoice, W2)"
        )

    def test_a_class_is_matched_without_regard_to_case(self):
        validate_routing(_config([_rule(reason="W2 present: {class_present:w2}")]))

    def test_a_condition_value_past_the_rules_conditions(self):
        conditions = [
            _condition("page_count", "> 5"),
            _condition("reviewed", "false"),
        ]
        validate_routing(
            _config(
                [
                    _rule(
                        conditions=conditions,
                        reason="Over {condition_1.value}, {condition_2.value}",
                    )
                ]
            )
        )

        message = _placeholder_refused("{condition_3.value}", conditions)

        assert message.endswith(
            "the placeholder names condition 3, and the routing rule has 2 conditions"
        )

    def test_a_condition_value_in_a_rule_with_no_conditions(self):
        message = _placeholder_refused("{condition_1.value}")

        assert "the routing rule has 0 conditions" in message

    def test_the_confidence_threshold_is_always_valid(self):
        validate_routing(_config([_rule(reason="Under {confidence_threshold}.")]))

    def test_the_default_outcomes_reason_is_checked(self):
        message = _refused(
            _config(
                default_outcome={
                    "actions": _queue("default"),
                    "reason": "Class {document_colour}",
                }
            )
        )

        assert message.startswith(
            "Routing table 'review-routing', default outcome reason placeholder "
            "'{document_colour}': 'document_colour' is not a routing input"
        )

    def test_a_condition_value_in_the_default_outcome(self):
        message = _refused(
            _config(
                [_rule(conditions=[_condition("page_count", "> 5")])],
                default_outcome={
                    "actions": _queue("default"),
                    "reason": "Not over {condition_1.value}",
                },
            )
        )

        assert message == (
            "Routing table 'review-routing', default outcome reason placeholder "
            "'{condition_1.value}': a condition value is not available in the "
            "default outcome's reason"
        )

    def test_the_default_outcomes_reason_needs_a_queue_too(self):
        message = _refused(
            _config(default_outcome={"actions": [], "reason": "No rule matched."})
        )

        assert message.startswith(
            "Routing table 'review-routing', default outcome: a reason is shown "
            "to a reviewer"
        )

    def test_the_default_outcomes_reason_must_parse(self):
        message = _refused(
            _config(default_outcome={"actions": _queue("default"), "reason": "{"})
        )

        assert message.startswith(
            "Routing table 'review-routing', default outcome reason: an unmatched '{'"
        )


class TestConditionRules:
    def test_an_unknown_input(self):
        message = _condition_refused(_condition("document_colour"))

        assert "'document_colour' is not a routing input" in message
        assert "field_value" in message

    def test_an_input_that_needs_a_path(self):
        assert "needs a path" in _condition_refused(_condition("field_value"))
        assert "needs a path" in _condition_refused(_condition("policy_rule_verdict"))

    def test_an_input_that_takes_no_path(self):
        message = _condition_refused(_condition("reviewed", "true", path="Total"))

        assert "input 'reviewed Total'" in message
        assert "takes no path" in message

    def test_an_unknown_aggregate(self):
        message = _condition_refused(
            _condition("section_classes", '"W2"', aggregate="median")
        )

        assert "the aggregate 'median' is not supported" in message

    @pytest.mark.parametrize("aggregate", ["all", "count"])
    def test_an_aggregate_on_a_single_value(self, aggregate):
        message = _condition_refused(
            _condition("page_count", "> 1", aggregate=aggregate)
        )

        assert "applies to an input that is a list" in message

    def test_a_test_that_does_not_parse(self):
        message = _condition_refused(_condition("page_count", "> "))

        assert "the test is not valid" in message
        assert "column 3" in message

    @pytest.mark.parametrize(
        ("input_name", "test", "kind"),
        [
            ("page_count", '"ten"', "string"),
            ("document_class", "> 5", "number"),
            ("reviewed", '"false"', "string"),
            ("reviewed", "0", "number"),
            ("processing_mode", "true", "boolean"),
        ],
    )
    def test_a_literal_of_the_wrong_type(self, input_name, test, kind):
        message = _condition_refused(_condition(input_name, test))

        assert f"compares with a {kind}" in message

    def test_a_count_takes_a_number(self):
        message = _condition_refused(
            _condition("section_classes", '"W2"', aggregate="count")
        )

        assert "compares with a string, but the value is a number" in message

    def test_a_field_literal_must_fit_the_fields_schema_type(self):
        message = _condition_refused(
            _condition("field_value", '"big"', path="Invoice:Total")
        )

        assert "input 'field_value Invoice:Total'" in message
        assert "compares with a string, but the value is a number" in message

    def test_a_field_two_classes_type_differently_accepts_either(self):
        # Vendor is a string in Invoice and a number in W2.
        for test in ('"Acme"', "> 5"):
            validate_routing(
                _config(
                    [_rule(conditions=[_condition("field_value", test, path="Vendor")])]
                )
            )

    def test_a_field_path_in_an_unknown_class(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Receipt:Total")
        )

        assert "'Receipt' is not a class of this profile" in message
        assert "Invoice" in message

    def test_a_field_path_the_class_does_not_have(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Invoice:Totl")
        )

        assert "'Totl' is not a field of the class" in message

    def test_a_nested_field_path_the_class_does_not_have(self):
        message = _condition_refused(
            _condition("field_value", '"x"', path="Invoice:Address.Town")
        )

        assert "'Town' is not a field of Address" in message

    def test_a_field_path_no_class_has(self):
        message = _condition_refused(_condition("field_value", "> 1", path="Totl"))

        assert "no class of this profile has the field 'Totl'" in message

    def test_a_field_in_a_list_needs_a_row(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Invoice:Lines.Amount")
        )

        assert "'Lines' is a list" in message
        assert "Lines[0].Amount" in message

    def test_a_row_index_on_a_field_that_is_not_a_list(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Invoice:Total[0]")
        )

        assert "'Total' is not a list" in message

    def test_a_malformed_field_path(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Invoice:[0].x")
        )

        assert "Invalid list subscript" in message

    def test_a_field_path_with_a_class_and_no_field(self):
        message = _condition_refused(_condition("field_value", "> 1", path="Invoice:"))

        assert "names no field" in message

    def test_a_policy_rule_id_the_policy_schema_does_not_have(self):
        message = _condition_refused(
            _condition("policy_rule_verdict", '"fail"', path="invoice_controls.missing")
        )

        assert "input 'policy_rule_verdict invoice_controls.missing'" in message
        assert "not in the Policy Schema" in message

    def test_a_policy_type_the_policy_schema_does_not_have(self):
        message = _condition_refused(
            _condition("failed_policy_rule_count", "> 0", path="tax_controls")
        )

        assert "this policy type is not in the Policy Schema" in message

    def test_class_present_needs_a_class(self):
        assert "needs a path" in _condition_refused(_condition("class_present", "true"))

    def test_class_present_with_a_class_the_profile_does_not_have(self):
        message = _condition_refused(
            _condition("class_present", "false", path="Receipt")
        )

        assert "input 'class_present Receipt'" in message
        assert message.endswith(
            "'Receipt' is not a class of this profile (classes: Invoice, W2)"
        )

    @pytest.mark.parametrize("path", ["W2", "w2", "INVOICE", " invoice "])
    def test_class_present_matches_a_class_without_regard_to_case(self, path):
        validate_routing(
            _config(
                [_rule(conditions=[_condition("class_present", "false", path=path)])]
            )
        )

    def test_class_present_is_a_boolean(self):
        message = _condition_refused(_condition("class_present", '"yes"', path="W2"))

        assert "compares with a string, but the value is a boolean" in message

    def test_policy_rule_ids_are_type_dot_key(self):
        assert policy_rule_ids(_config()) == [
            "invoice_controls.total_matches_lines",
            "invoice_controls.vendor_is_approved",
            "legacy_policy.signed",
        ]


class TestLimits:
    def test_the_rule_limit(self):
        at_limit = [_rule(f"r{i}") for i in range(MAX_RULES)]
        validate_routing(_config(at_limit))

        message = _refused(_config(at_limit + [_rule("one-more")]))

        assert (
            f"the table has {MAX_RULES + 1} rules; the limit is {MAX_RULES}" in message
        )

    def test_the_condition_limit(self):
        conditions = [_condition("reviewed", "false")] * MAX_CONDITIONS
        validate_routing(_config([_rule(conditions=conditions)]))

        message = _refused(
            _config([_rule(conditions=conditions + [_condition("reviewed", "true")])])
        )

        assert message.startswith("Routing table 'review-routing', rule 'r1':")
        assert (
            f"{MAX_CONDITIONS + 1} conditions; the limit is {MAX_CONDITIONS}" in message
        )

    def test_the_test_limit(self):
        longest = '"' + "x" * (MAX_TEST_BYTES - 2) + '"'
        assert len(longest) == MAX_TEST_BYTES
        validate_routing(
            _config([_rule(conditions=[_condition("document_class", longest)])])
        )

        message = _condition_refused(
            _condition("document_class", '"' + "x" * (MAX_TEST_BYTES - 1) + '"')
        )

        assert f"the test is longer than {MAX_TEST_BYTES} bytes" in message

    def test_the_path_limit(self):
        message = _condition_refused(
            _condition("field_value", "> 1", path="Invoice:" + "x" * MAX_PATH_BYTES)
        )

        assert f"the path is longer than {MAX_PATH_BYTES} bytes" in message

    def test_limits_hold_the_item_budget(self):
        """The section cap is the binding limit; the per-field limits bound one value.

        A table at every per-field limit is far larger than a profile can afford
        beside its class schemas (a profile is one 400 KB item), so the section
        as a whole is capped too, and the refusal states both figures.
        """
        test = '"' + "x" * (MAX_TEST_BYTES - 2) + '"'
        conditions = [_condition("document_class", test)] * MAX_CONDITIONS
        rules = [
            _rule(f"r{i}", conditions, label="l" * MAX_LABEL_BYTES)
            for i in range(MAX_RULES)
        ]
        config = _config(rules)
        size = len(
            __import__("json")
            .dumps(config.routing.model_dump(), separators=(",", ":"))
            .encode("utf-8")
        )
        assert size > MAX_SECTION_BYTES

        message = _refused(config)

        assert message == (
            f"Routing table 'review-routing': the routing section is {size} bytes; "
            f"the limit is {MAX_SECTION_BYTES} bytes"
        )

    def test_a_table_under_the_section_cap_is_accepted(self):
        rules = [
            _rule(f"r{i}", [_condition("page_count", "> 1")]) for i in range(MAX_RULES)
        ]

        validate_routing(_config(rules))


TABLE = "routing-validation-config"
BUCKET = "routing-validation-config-bucket"
PROFILE = "lending"

_STALE = {
    "tables": [
        {
            "rules": [
                {
                    "id": "stale",
                    "conditions": [
                        {"input": "field_value", "path": "Gone:Field", "test": "> 1"}
                    ],
                    "actions": [{"type": "review_queue", "queue": "deleted-queue"}],
                }
            ]
        }
    ]
}
_BAD_DELTA = {
    "routing": {
        "tables": [
            {
                "hit_policy": "UNIQUE",
                "rules": [],
            }
        ]
    }
}


@pytest.fixture
def manager(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield ConfigurationManager()


class TestTheWriteBoundary:
    def test_a_delta_without_routing_is_not_validated(self, manager):
        """A stale stored table must not stop an unrelated save."""
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_STALE), version=PROFILE
        )

        manager.handle_update_custom_configuration(
            {"notes": "an unrelated change"},
            version=PROFILE,
            routing_queue_lookup=lambda queue_id: None,
        )

        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert saved.notes == "an unrelated change"
        assert saved.routing.tables[0].rules[0].id == "stale"

    def test_a_delta_with_a_bad_routing_raises_and_saves_nothing(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="before"), version=PROFILE
        )

        with pytest.raises(ConfigurationValidationError, match="'UNIQUE'"):
            manager.handle_update_custom_configuration(
                {**_BAD_DELTA, "notes": "after"}, version=PROFILE
            )

        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert saved.notes == "before"
        assert saved.routing.tables[0].hit_policy == "FIRST"

    def test_a_valid_routing_delta_is_saved(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        delta = {
            "routing": {
                "tables": [
                    {
                        "rules": [
                            {
                                "id": "everything",
                                "conditions": [],
                                "actions": _queue("approvals"),
                                "reason": "Every document: {page_count} pages.",
                            }
                        ]
                    }
                ]
            }
        }

        manager.handle_update_custom_configuration(
            delta,
            version=PROFILE,
            routing_queue_lookup=lambda q: QueueInfo(q, q.title(), True),
        )

        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        (rule,) = saved.routing.tables[0].rules
        assert rule.id == "everything"
        assert [action.model_dump() for action in rule.actions] == _queue("approvals")
        assert rule.reason == "Every document: {page_count} pages."

    def test_the_queue_lookup_reaches_the_validation(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        delta = {
            "routing": {
                "tables": [
                    {
                        "rules": [
                            {
                                "id": "everything",
                                "actions": _queue("gone"),
                            }
                        ]
                    }
                ]
            }
        }

        with pytest.raises(ConfigurationValidationError, match="'gone' does not exist"):
            manager.handle_update_custom_configuration(
                delta, version=PROFILE, routing_queue_lookup=lambda queue_id: None
            )

    @pytest.mark.parametrize("flag", ["saveAsVersion", "saveAsDefault"])
    def test_save_as_version_and_save_as_default_are_validated(self, manager, flag):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")

        with pytest.raises(ConfigurationValidationError, match="'UNIQUE'"):
            manager.handle_update_custom_configuration(
                {**_BAD_DELTA, flag: True}, version="new-profile"
            )

    def test_reset_to_default_is_not_validated(self, manager):
        """Reset is the escape hatch: it copies the default as it is stored."""
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_STALE), version="default"
        )
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        manager.handle_update_custom_configuration(
            {"resetToDefault": True},
            version=PROFILE,
            routing_queue_lookup=lambda queue_id: None,
        )

        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert saved.routing.tables[0].rules[0].id == "stale"

    def test_restore_revision_is_not_validated(self, manager):
        """A revision was valid when it was saved; restoring it must stay possible."""
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_STALE), version=PROFILE
        )
        stale_revision = manager.resolve_published_revision(PROFILE)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        manager.restore_revision(PROFILE, int(stale_revision))

        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert saved.routing.tables[0].rules[0].id == "stale"


_ACTIONS_FORM = (
    "the outcome is a list of actions: write actions: [{type: review_queue, "
    "queue: <id>}], or actions: [] for no review"
)


class TestTheSingleOutcomeShape:
    """No release reads or writes the single outcome; a write that carries it is
    refused.

    The models drop the keys, so without the refusal the routing rule would be
    saved as "not reviewed" and nobody would be told.
    """

    def _refused(self, raw) -> str:
        with pytest.raises(ConfigurationValidationError) as raised:
            refuse_single_outcome_shape(raw)
        return str(raised.value)

    def test_a_rule_with_an_outcome_is_refused_by_name(self):
        raw = {
            "tables": [
                {
                    "id": "review-routing",
                    "rules": [
                        {"id": "fine", "actions": []},
                        {"id": "old", "outcome": {"kind": "queue", "queue": "q"}},
                    ],
                }
            ]
        }

        assert self._refused(raw) == (
            f"Routing table 'review-routing', rule 'old': {_ACTIONS_FORM}"
        )

    def test_a_table_and_a_rule_with_no_id_are_named_by_position(self):
        raw = {"tables": [{"rules": [{"actions": []}, {"outcome": {}}]}]}

        assert self._refused(raw) == f"Routing table '#1', rule '#2': {_ACTIONS_FORM}"

    def test_an_outcome_beside_actions_is_still_refused(self):
        raw = {"tables": [{"rules": [{"id": "both", "actions": [], "outcome": {}}]}]}

        assert "rule 'both'" in self._refused(raw)

    @pytest.mark.parametrize("key", ["kind", "queue"])
    def test_a_default_outcome_with_a_single_outcome_key_is_refused(self, key):
        raw = {"tables": [{"id": "t", "rules": [], "default_outcome": {key: "x"}}]}

        assert self._refused(raw) == (
            f"Routing table 't', default outcome: {_ACTIONS_FORM}"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "routing",
            [],
            {},
            {"tables": None},
            {"tables": "one"},
            {"tables": ["not a table", None]},
            {"tables": [{"rules": None, "default_outcome": None}]},
            {"tables": [{"rules": ["not a rule", None], "default_outcome": "none"}]},
            {"max_consecutive_placements": 5},
        ],
    )
    def test_anything_else_is_left_to_the_validation(self, raw):
        refuse_single_outcome_shape(raw)

    def test_the_shape_of_this_release_is_not_refused(self):
        refuse_single_outcome_shape(
            {
                "tables": [
                    {
                        "rules": [_rule(reason="Held.")],
                        "default_outcome": {"actions": _queue("default"), "reason": ""},
                    }
                ]
            }
        )
        refuse_single_outcome_shape(IDPConfig().routing.model_dump())


class TestTheSingleOutcomeShapeAtTheWriteBoundary:
    def _delta(self, table) -> dict:
        return {"routing": {"tables": [table]}}

    def test_a_raw_outcome_is_refused_and_nothing_is_saved(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="before"), version=PROFILE
        )
        delta = self._delta(
            {
                "rules": [
                    {
                        "id": "everything",
                        "conditions": [],
                        "outcome": {"kind": "queue", "queue": "approvals"},
                    }
                ]
            }
        )

        with pytest.raises(ConfigurationValidationError) as raised:
            manager.handle_update_custom_configuration(
                {**delta, "notes": "after"}, version=PROFILE
            )

        assert str(raised.value) == (
            f"Routing table '#1', rule 'everything': {_ACTIONS_FORM}"
        )
        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert saved.notes == "before"
        assert [rule.id for rule in saved.routing.tables[0].rules] == [
            "builtin-extraction-failed",
            "builtin-confidence-alerts",
            "builtin-missing-required",
        ]

    def test_a_raw_default_outcome_kind_is_refused(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        delta = self._delta(
            {
                "id": "review-routing",
                "rules": [],
                "default_outcome": {"kind": "queue", "queue": "default"},
            }
        )

        with pytest.raises(ConfigurationValidationError) as raised:
            manager.handle_update_custom_configuration(delta, version=PROFILE)

        assert str(raised.value) == (
            f"Routing table 'review-routing', default outcome: {_ACTIONS_FORM}"
        )

    @pytest.mark.parametrize("flag", ["saveAsVersion", "saveAsDefault"])
    def test_save_as_version_and_save_as_default_refuse_it_too(self, manager, flag):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        delta = self._delta(
            {"rules": [{"id": "old", "outcome": {"kind": "no_review"}}]}
        )

        with pytest.raises(ConfigurationValidationError, match="rule 'old'"):
            manager.handle_update_custom_configuration(
                {**delta, flag: True}, version="new-profile"
            )

    def test_a_delta_without_routing_is_not_checked_for_it(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        manager.handle_update_custom_configuration(
            {"notes": "an unrelated change"}, version=PROFILE
        )

        assert manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE).notes == (
            "an unrelated change"
        )


_VERDICT = _condition(
    "policy_rule_verdict", '"fail"', path="invoice_controls.total_matches_lines"
)
_FAILED = _condition("failed_policy_rule_count", "> 0")
_OFF = {"enabled": False}


def _tests_warning(rule_id, inputs) -> str:
    return (
        f"Routing table 'review-routing', rule '{rule_id}': this routing rule tests "
        f"a policy rule result ({inputs}), and policy rule validation is off for "
        f"this profile or it has no policies, so the routing rule never matches."
    )


def _shows_warning(where, placeholders) -> str:
    return (
        f"Routing table 'review-routing', {where}: the reason shows a policy rule "
        f"result ({placeholders}), which is empty while policy rule validation is "
        f"off."
    )


class TestRoutingWarnings:
    def _rules(self):
        return [
            _rule("by-verdict", [_VERDICT]),
            _rule("plain", [_condition("page_count", "> 5")]),
            _rule("by-count", [_condition("reviewed", "false"), _FAILED]),
        ]

    def test_one_warning_for_each_routing_rule_while_validation_is_off(self):
        config = _config(self._rules(), rule_validation=_OFF)

        assert routing_warnings(config) == [
            _tests_warning(
                "by-verdict", "policy_rule_verdict invoice_controls.total_matches_lines"
            ),
            _tests_warning("by-count", "failed_policy_rule_count"),
        ]

    def test_a_profile_with_no_policies_is_warned_too(self):
        rules = [_rule("by-count", [_FAILED])]
        config = _config(rules, policy_classes=[])

        assert routing_warnings(config) == [
            _tests_warning("by-count", "failed_policy_rule_count")
        ]

    def test_no_warning_while_validation_is_on(self):
        config = _config(self._rules())

        assert routing_warnings(config) == []

    def test_a_disabled_routing_rule_is_not_warned_about(self):
        rules = [
            _rule("by-verdict", [_VERDICT], enabled=False),
            _rule(
                "shows-it",
                reason="{failed_policy_rule_count} failed",
                enabled=False,
            ),
        ]

        assert routing_warnings(_config(rules, rule_validation=_OFF)) == []

    def test_a_reason_that_only_shows_a_policy_result(self):
        rules = [
            _rule(
                "shows-it",
                [_condition("page_count", "> 5")],
                reason=(
                    "{failed_policy_rule_count} failed of {page_count} pages; "
                    "{policy_rule_verdict:legacy_policy.signed}, again "
                    "{failed_policy_rule_count}"
                ),
            )
        ]

        assert routing_warnings(_config(rules, rule_validation=_OFF)) == [
            _shows_warning(
                "rule 'shows-it'",
                "{failed_policy_rule_count}, {policy_rule_verdict:legacy_policy.signed}",
            )
        ]

    def test_a_routing_rule_that_tests_and_shows_gets_the_one_about_the_test(self):
        rules = [_rule("both", [_FAILED], reason="{failed_policy_rule_count} failed")]

        assert routing_warnings(_config(rules, rule_validation=_OFF)) == [
            _tests_warning("both", "failed_policy_rule_count")
        ]

    def test_the_inputs_a_routing_rule_tests_are_named_once(self):
        rules = [_rule("twice", [_FAILED, _VERDICT, _FAILED])]

        assert routing_warnings(_config(rules, rule_validation=_OFF)) == [
            _tests_warning(
                "twice",
                "failed_policy_rule_count, policy_rule_verdict "
                "invoice_controls.total_matches_lines",
            )
        ]

    def test_the_default_outcomes_reason(self):
        config = _config(
            [_rule("plain")],
            rule_validation=_OFF,
            default_outcome={
                "actions": _queue("default"),
                "reason": "{failed_policy_rule_count:invoice_controls} failed",
            },
        )

        assert routing_warnings(config) == [
            _shows_warning(
                "default outcome", "{failed_policy_rule_count:invoice_controls}"
            )
        ]

    def test_a_reason_that_does_not_parse_is_ignored(self):
        rules = [_rule("broken", reason="{failed_policy_rule_count} of {")]

        assert routing_warnings(_config(rules, rule_validation=_OFF)) == []

    def test_a_table_with_no_policy_input_has_no_warning(self):
        assert routing_warnings(IDPConfig()) == []
        assert routing_warnings(_config(rule_validation=_OFF)) == []

    @pytest.mark.parametrize("config", [None, {}, object(), {"routing": "x"}])
    def test_it_never_raises(self, config):
        assert routing_warnings(config) == []

    def test_a_table_it_cannot_read_gives_what_it_had_and_is_logged(self, caplog):
        from types import SimpleNamespace

        readable = _config([_rule("by-count", [_FAILED])], rule_validation=_OFF)
        config = SimpleNamespace(
            rule_validation=_OFF,
            policy_classes=[],
            routing=SimpleNamespace(tables=[readable.routing.tables[0], object()]),
        )

        with caplog.at_level("WARNING"):
            warnings = routing_warnings(config)

        assert warnings == [_tests_warning("by-count", "failed_policy_rule_count")]
        assert "event=routing_warnings_failed" in caplog.text

    def test_a_warning_keeps_the_prefix_a_refusal_has(self):
        """The web UI reads the routing rule's id out of ``rule '<id>'``."""
        (warning,) = routing_warnings(
            _config([_rule("by-count", [_FAILED])], rule_validation=_OFF)
        )

        assert warning.startswith("Routing table 'review-routing', rule 'by-count': ")
        refusal = _refused(_config([_rule("by-count", [_condition("nope")])]))
        assert refusal.startswith("Routing table 'review-routing', rule 'by-count'")


_BY_COUNT = _tests_warning("by-count", "failed_policy_rule_count")


def _by_count_delta(**more) -> dict:
    """A write whose one routing rule tests a policy rule result."""
    return {"routing": {"tables": [{"rules": [_rule("by-count", [_FAILED])]}]}, **more}


class TestWarningsReturnedByASave:
    """``handle_update_custom_configuration`` adds them to the list it is given.

    The profiles here hold no policies unless a case says so, so policy rule
    validation does not run and a routing rule that tests its result is warned
    about.
    """

    def test_a_normal_update_collects_them_and_still_returns_a_bool(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        collected = []

        result = manager.handle_update_custom_configuration(
            _by_count_delta(), version=PROFILE, warnings=collected
        )

        assert result is True
        assert collected == [_BY_COUNT]
        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert [rule.id for rule in saved.routing.tables[0].rules] == ["by-count"]

    def test_a_save_that_leaves_routing_alone_collects_them_too(self, manager):
        """Turning policy rule validation off is what stops the rule matching."""
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config([_rule("by-count", [_FAILED])]), version=PROFILE
        )
        while_on, once_off = [], []

        manager.handle_update_custom_configuration(
            {"notes": "an unrelated change"}, version=PROFILE, warnings=while_on
        )
        manager.handle_update_custom_configuration(
            {"rule_validation": _OFF}, version=PROFILE, warnings=once_off
        )

        assert while_on == []
        assert once_off == [_BY_COUNT]

    @pytest.mark.parametrize("flag", ["saveAsVersion", "saveAsDefault"])
    def test_save_as_version_and_save_as_default_collect_them(self, manager, flag):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        collected = []

        result = manager.handle_update_custom_configuration(
            _by_count_delta(**{flag: True}), version="new-profile", warnings=collected
        )

        assert result is True
        assert collected == [_BY_COUNT]
        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, "new-profile")
        assert [rule.id for rule in saved.routing.tables[0].rules] == ["by-count"]

    def test_save_as_version_with_no_default_stored_collects_them(self, manager):
        """The branch that saves the write as it is, with nothing to merge onto."""
        collected = []

        manager.handle_update_custom_configuration(
            _by_count_delta(saveAsVersion=True),
            version="new-profile",
            warnings=collected,
        )

        assert manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "default") is None
        assert collected == [_BY_COUNT]

    def test_reset_to_default_collects_none(self, manager):
        """Reset copies the default as it is stored and reports nothing about it."""
        default = IDPConfig(**_by_count_delta())
        assert routing_warnings(default) == [_BY_COUNT]
        manager.save_configuration(CONFIG_TYPE_CONFIG, default, version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        collected = []

        manager.handle_update_custom_configuration(
            {"resetToDefault": True}, version=PROFILE, warnings=collected
        )

        assert collected == []
        saved = manager.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert [rule.id for rule in saved.routing.tables[0].rules] == ["by-count"]

    def test_the_list_is_added_to_not_replaced(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        collected = ["from an earlier step"]

        manager.handle_update_custom_configuration(
            _by_count_delta(), version=PROFILE, warnings=collected
        )

        assert collected == ["from an earlier step", _BY_COUNT]

    def test_a_save_with_nothing_to_warn_about_leaves_the_list_empty(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        collected = []

        manager.handle_update_custom_configuration(
            {"notes": "an unrelated change"}, version=PROFILE, warnings=collected
        )

        assert collected == []

    def test_a_refused_save_raises_as_before(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        with pytest.raises(ConfigurationValidationError, match="'UNIQUE'"):
            manager.handle_update_custom_configuration(
                _BAD_DELTA, version=PROFILE, warnings=[]
            )

    def test_without_a_list_the_save_is_unchanged(self, manager):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        assert (
            manager.handle_update_custom_configuration(
                _by_count_delta(), version=PROFILE
            )
            is True
        )
