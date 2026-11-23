# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routing table as a decision table, and through DMN XML and back."""

import re

import pytest

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
    queue_actions,
)
from idp_common.config.routing_validation import MAX_RULES, ROUTING_INPUTS
from idp_common.decision_tables import evaluate, sfeel
from idp_common.decision_tables.dmn import (
    DMN_NAMESPACES,
    DmnImportError,
    read_dmn,
    write_decision_table,
)
from idp_common.decision_tables.models import (
    DecisionTable,
    DecisionTableError,
    TableInput,
    TableOutput,
    TableRule,
)
from idp_common.review_queues.routing_dmn import (
    KIND_NO_REVIEW,
    KIND_QUEUE,
    OUTPUT_OUTCOME,
    OUTPUT_QUEUE,
    OUTPUT_REASON,
    ROUTING_NAMESPACE,
    RoutingExpressionError,
    export_xml,
    expression_for,
    from_decision_table,
    import_xml,
    parse_expression,
    to_decision_table,
)

pytestmark = pytest.mark.unit


def _showcase() -> RoutingConfig:
    """A table with every feature the mapping has to carry."""
    return RoutingConfig(
        tables=[
            RoutingTable(
                rules=[
                    RoutingRule(
                        id="specialist-bank-checks",
                        label="Chèques bancaires → spécialiste",
                        conditions=[
                            RoutingCondition(
                                input="section_classes",
                                aggregate="any",
                                test='"Bank-checks"',
                            )
                        ],
                        actions=queue_actions("specialist"),
                        reason="Chèque à vérifier: {section_classes}.",
                    ),
                    RoutingRule(
                        id="compliance-policy-failure",
                        label="A named policy rule failed",
                        conditions=[
                            RoutingCondition(
                                input="policy_rule_verdict",
                                path="invoice_controls.total_matches_lines",
                                test='"fail"',
                            )
                        ],
                        actions=queue_actions("compliance"),
                        reason=(
                            "{policy_rule_verdict:invoice_controls."
                            "total_matches_lines}: the total and the lines differ."
                        ),
                    ),
                    RoutingRule(
                        id="many-line-items",
                        label="",
                        enabled=False,
                        conditions=[
                            RoutingCondition(
                                input="field_value",
                                path="Invoice:Lines",
                                aggregate="count",
                                test="> 2",
                            )
                        ],
                        actions=queue_actions("approvals"),
                    ),
                    RoutingRule(
                        id="approvals-high-value",
                        label='Total over 1,000 & "urgent" <flag>',
                        conditions=[
                            RoutingCondition(
                                input="field_value",
                                path="Invoice:Total",
                                aggregate="any",
                                test="> 1000",
                            ),
                            RoutingCondition(input="reviewed", test="false"),
                        ],
                        actions=queue_actions("approvals"),
                        reason=(
                            "Total {field_value:Invoice:Total} is over "
                            "{condition_1.value} & needs <approval>."
                        ),
                    ),
                    RoutingRule(
                        id="everything-else",
                        conditions=[],
                        actions=[],
                    ),
                ],
                default_outcome=RoutingDefaultOutcome(
                    actions=queue_actions("default"),
                    reason="No routing rule matched.",
                ),
            )
        ],
        max_consecutive_placements=3,
    )


class TestToDecisionTable:
    def test_the_built_in_table(self):
        table = to_decision_table(RoutingTable())

        assert table.id == "review-routing"
        assert table.hit_policy == "FIRST"
        assert [(c.expression, c.type_ref, c.quantifier) for c in table.inputs] == [
            ("reviewed", "boolean", ""),
            ("extraction_failed_count", "number", ""),
            ("confidence_alert_count", "number", ""),
            ("route_missing_required_fields", "boolean", ""),
            ("missing_required_field_count", "number", ""),
        ]
        assert [(o.id, o.name, o.type_ref, o.default) for o in table.outputs] == [
            ("output_outcome", "outcome", "string", '"no_review"'),
            ("output_queue", "queue", "string", '""'),
            ("output_reason", "reason", "string", '""'),
        ]
        assert [(r.id, r.input_entries, r.output_entries) for r in table.rules] == [
            (
                "builtin-extraction-failed",
                ["false", "> 0", "-", "-", "-"],
                ['"queue"', '"default"', f'"{BUILTIN_EXTRACTION_FAILED_REASON}"'],
            ),
            (
                "builtin-confidence-alerts",
                ["false", "-", "> 0", "-", "-"],
                ['"queue"', '"default"', f'"{BUILTIN_CONFIDENCE_REASON}"'],
            ),
            (
                "builtin-missing-required",
                ["false", "-", "-", "true", "> 0"],
                ['"queue"', '"default"', f'"{BUILTIN_MISSING_REQUIRED_REASON}"'],
            ),
        ]

    def test_the_three_outputs_and_the_outcome_values(self):
        assert (OUTPUT_OUTCOME, OUTPUT_QUEUE, OUTPUT_REASON) == (
            "outcome",
            "queue",
            "reason",
        )
        assert (KIND_QUEUE, KIND_NO_REVIEW) == ("queue", "no_review")

    def test_actions_are_written_as_an_outcome_and_a_queue(self):
        table = to_decision_table(_showcase().tables[0], include_disabled=True)
        entries = {rule.id: rule.output_entries for rule in table.rules}

        assert entries["many-line-items"] == ['"queue"', '"approvals"', '""']
        assert entries["everything-else"] == ['"no_review"', '""', '""']
        assert entries["specialist-bank-checks"] == [
            '"queue"',
            '"specialist"',
            '"Chèque à vérifier: {section_classes}."',
        ]
        assert [output.default for output in table.outputs] == [
            '"queue"',
            '"default"',
            '"No routing rule matched."',
        ]

    def test_the_built_in_table_evaluates(self):
        table = to_decision_table(RoutingTable())

        held = evaluate(table, {"reviewed": False, "confidence_alert_count": 2})
        clean = evaluate(table, {"reviewed": False, "confidence_alert_count": 0})
        reviewed = evaluate(table, {"reviewed": True, "confidence_alert_count": 2})
        # A failed extraction comes first, whatever else the document has.
        failed = evaluate(
            table,
            {
                "reviewed": False,
                "extraction_failed_count": 1,
                "confidence_alert_count": 2,
            },
        )
        assert failed.matched_rule_id == "builtin-extraction-failed"
        assert failed.outputs["reason"] == BUILTIN_EXTRACTION_FAILED_REASON

        assert held.matched_rule_id == "builtin-confidence-alerts"
        assert held.outputs == {
            "outcome": "queue",
            "queue": "default",
            "reason": BUILTIN_CONFIDENCE_REASON,
        }
        assert clean.default_used and clean.outputs == {
            "outcome": "no_review",
            "queue": "",
            "reason": "",
        }
        assert reviewed.default_used

    def test_disabled_rules_are_left_out_unless_asked_for(self):
        table = _showcase().tables[0]

        evaluated = to_decision_table(table)
        exported = to_decision_table(table, include_disabled=True)

        assert "many-line-items" not in [rule.id for rule in evaluated.rules]
        assert "many-line-items" in [rule.id for rule in exported.rules]
        # The disabled rule's column exists only where the rule does.
        count_column = 'field_value("Invoice:Lines","count")'
        assert count_column not in [c.expression for c in evaluated.inputs]
        assert count_column in [c.expression for c in exported.inputs]

    def test_list_inputs_get_a_quantifier_and_count_gets_a_number(self):
        table = to_decision_table(_showcase().tables[0], include_disabled=True)
        columns = {c.expression: c for c in table.inputs}

        assert columns['section_classes("any")'].quantifier == "any"
        assert columns['field_value("Invoice:Total","any")'].quantifier == "any"
        assert columns['field_value("Invoice:Total","any")'].type_ref == "number"
        count = columns['field_value("Invoice:Lines","count")']
        assert (count.quantifier, count.type_ref) == ("", "number")
        assert (
            columns[
                'policy_rule_verdict("invoice_controls.total_matches_lines")'
            ].quantifier
            == ""
        )

    def test_a_rule_with_no_conditions_matches_everything(self):
        table = to_decision_table(_showcase().tables[0])

        result = evaluate(table, {})

        assert result.matched_rule_id == "everything-else"
        assert result.outputs == {"outcome": "no_review", "queue": "", "reason": ""}

    def test_two_conditions_on_one_input_get_two_columns(self):
        """A DMN cell is one test, so the second condition needs its own column."""
        table = RoutingTable(
            rules=[
                RoutingRule(
                    id="mid-size",
                    conditions=[
                        RoutingCondition(input="page_count", test="> 1"),
                        RoutingCondition(input="page_count", test="< 10"),
                    ],
                    actions=queue_actions("default"),
                )
            ]
        )

        decision = to_decision_table(table)

        assert [c.expression for c in decision.inputs] == ["page_count", "page_count"]
        assert decision.rules[0].input_entries == ["> 1", "< 10"]
        assert evaluate(decision, {"page_count": 5}).matched_rule_id == "mid-size"
        assert evaluate(decision, {"page_count": 50}).default_used
        back = import_xml(export_xml(RoutingConfig(tables=[table]))).tables[0]
        assert back.rules[0].conditions == table.rules[0].conditions


class TestExpressions:
    @pytest.mark.parametrize(
        "condition",
        [
            RoutingCondition(input="reviewed"),
            RoutingCondition(input="document_class"),
            RoutingCondition(input="section_classes", aggregate="any"),
            RoutingCondition(input="section_classes", aggregate="all"),
            RoutingCondition(input="section_classes", aggregate="count"),
            RoutingCondition(input="class_present", path="W2"),
            RoutingCondition(input="class_present", path="Bank checks"),
            RoutingCondition(
                input="field_value", path="Invoice:Total", aggregate="any"
            ),
            RoutingCondition(
                input="field_value", path="Lines[0].Amount", aggregate="count"
            ),
            RoutingCondition(
                input="field_value", path='Odd "name"\\x', aggregate="all"
            ),
            RoutingCondition(input="policy_rule_verdict", path="controls.total_ok"),
            RoutingCondition(input="failed_policy_rule_count"),
            RoutingCondition(input="failed_policy_rule_count", path="controls"),
        ],
    )
    def test_expression_for_and_parse_expression_are_inverses(self, condition):
        expression = expression_for(condition)

        assert parse_expression(expression) == (
            condition.input,
            condition.path,
            condition.aggregate,
        )

    def test_every_catalogue_input_has_an_expression(self):
        for entry in ROUTING_INPUTS:
            condition = RoutingCondition(
                input=entry.name, path="x.y" if entry.needs_path else ""
            )
            assert parse_expression(expression_for(condition))[0] == entry.name

    def test_the_documented_forms(self):
        assert expression_for(RoutingCondition(input="reviewed")) == "reviewed"
        assert (
            expression_for(RoutingCondition(input="field_value", path="Invoice:Total"))
            == 'field_value("Invoice:Total","any")'
        )
        assert (
            expression_for(
                RoutingCondition(
                    input="policy_rule_verdict",
                    path="invoice_controls.total_matches_lines",
                )
            )
            == 'policy_rule_verdict("invoice_controls.total_matches_lines")'
        )
        assert (
            expression_for(
                RoutingCondition(
                    input="failed_policy_rule_count", path="invoice_controls"
                )
            )
            == 'failed_policy_rule_count("invoice_controls")'
        )
        assert (
            expression_for(RoutingCondition(input="class_present", path="W2"))
            == 'class_present("W2")'
        )

    def test_spaces_are_tolerated(self):
        assert parse_expression(' field_value ( "a.b" , "all" ) ') == (
            "field_value",
            "a.b",
            "all",
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Age",
            "document_colour",
            'reviewed("x")',
            "field_value",
            'field_value("a")',
            'field_value("a","median")',
            "section_classes",
            "field_value(a, any)",
            'field_value("a","any",)',
            'field_value("a" "any")',
            'field_value("a","b","any")',
            "policy_rule_verdict",
            "class_present",
            'class_present("W2","any")',
            "reviewed + 1",
        ],
    )
    def test_text_that_is_not_a_catalogue_call_is_refused(self, text):
        with pytest.raises(RoutingExpressionError):
            parse_expression(text)


class TestRoundTrip:
    @pytest.mark.parametrize("dmn_version", ["1.5", "1.3"])
    def test_the_built_in_table(self, dmn_version):
        xml = export_xml(RoutingConfig(), dmn_version=dmn_version)

        assert DMN_NAMESPACES[dmn_version] in xml
        assert import_xml(xml) == RoutingConfig()

    @pytest.mark.parametrize("dmn_version", ["1.5", "1.3"])
    def test_a_table_with_every_feature(self, dmn_version):
        """Ids, order, labels, flags, conditions, actions, reasons, stage and
        engine."""
        routing = _showcase()

        back = import_xml(export_xml(routing, dmn_version=dmn_version))

        assert back.tables == routing.tables
        (table,) = back.tables
        assert [rule.id for rule in table.rules] == [
            "specialist-bank-checks",
            "compliance-policy-failure",
            "many-line-items",
            "approvals-high-value",
            "everything-else",
        ]
        assert [rule.enabled for rule in table.rules] == [True, True, False, True, True]
        assert table.rules[0].label == "Chèques bancaires → spécialiste"
        assert table.rules[3].label == 'Total over 1,000 & "urgent" <flag>'
        assert table.rules[0].actions == [RoutingAction(queue="specialist")]
        assert table.rules[0].reason == "Chèque à vérifier: {section_classes}."
        assert table.rules[4].actions == []
        assert table.default_outcome == RoutingDefaultOutcome(
            actions=[RoutingAction(type="review_queue", queue="default")],
            reason="No routing rule matched.",
        )
        assert (table.stage, table.engine, table.hit_policy) == (
            "after_policy_rule_validation",
            "builtin",
            "FIRST",
        )

    def test_the_export_writes_three_outputs(self):
        xml = export_xml(_showcase())

        document = read_dmn(xml)
        assert [(o.name, o.type_ref) for o in document.table.outputs] == [
            ("outcome", "string"),
            ("queue", "string"),
            ("reason", "string"),
        ]
        assert all(len(rule.output_entries) == 3 for rule in document.table.rules)
        assert xml.count("<outputEntry ") == 3 * len(document.table.rules)
        assert '<output id="output_reason" name="reason" typeRef="string">' in xml

    @pytest.mark.parametrize("dmn_version", ["1.5", "1.3"])
    @pytest.mark.parametrize(
        "reason",
        [
            'She said "check it" and left.',
            "A path like C:\\scans\\in and a trailing backslash \\",
            'Both: \\"quoted\\" and \\\\ doubled.',
            "First line.\nSecond line.",
            "Literal braces {{like these}} and a {page_count} placeholder.",
            "Montant élevé — 請求書を確認 ✓ <b>&amp;</b>",
            "  spaces kept at both ends  ",
        ],
        ids=[
            "quotes",
            "backslashes",
            "escaped-quotes",
            "newline",
            "braces",
            "non-ascii",
            "outer-spaces",
        ],
    )
    def test_a_reason_comes_back_as_written(self, dmn_version, reason):
        routing = RoutingConfig(
            tables=[
                RoutingTable(
                    rules=[
                        RoutingRule(
                            id="held",
                            conditions=[
                                RoutingCondition(input="page_count", test="> 1")
                            ],
                            actions=queue_actions("approvals"),
                            reason=reason,
                        )
                    ],
                    default_outcome=RoutingDefaultOutcome(
                        actions=queue_actions("default"), reason=reason
                    ),
                )
            ]
        )

        back = import_xml(export_xml(routing, dmn_version=dmn_version))

        assert back.tables[0].rules[0].reason == reason
        assert back.tables[0].default_outcome.reason == reason
        assert back == routing

    @pytest.mark.parametrize("dmn_version", ["1.5", "1.3"])
    def test_a_class_present_column_comes_back(self, dmn_version):
        routing = RoutingConfig(
            tables=[
                RoutingTable(
                    rules=[
                        RoutingRule(
                            id="no-w2",
                            conditions=[
                                RoutingCondition(
                                    input="class_present", path="W2", test="false"
                                )
                            ],
                            actions=queue_actions("approvals"),
                            reason="The package has no W2: {class_present:W2}.",
                        )
                    ]
                )
            ]
        )

        xml = export_xml(routing, dmn_version=dmn_version)
        back = import_xml(xml)

        assert back == routing
        (column,) = read_dmn(xml).table.inputs
        assert (column.expression, column.type_ref, column.quantifier) == (
            'class_present("W2")',
            "boolean",
            "",
        )
        assert column.label == "Class is present: W2"

    def test_the_placement_limit_is_not_part_of_the_table(self):
        routing = _showcase()
        routing.max_consecutive_placements = 9

        back = import_xml(export_xml(routing))

        assert back.max_consecutive_placements == 3
        assert back.tables == routing.tables

    def test_the_file_carries_the_routing_attributes(self):
        xml = export_xml(_showcase())

        assert f'xmlns:idp="{ROUTING_NAMESPACE}"' in xml
        assert 'idp:stage="after_policy_rule_validation"' in xml
        assert 'idp:engine="builtin"' in xml
        assert xml.count('idp:enabled="false"') == 1
        assert "<description>A named policy rule failed</description>" in xml
        assert 'hitPolicy="FIRST"' in xml
        # One decision, named after the table.
        assert xml.count("<decision ") == 1
        assert '<decision id="review-routing" name="review-routing">' in xml

    def test_dmn_13_writes_equality_without_an_operator(self):
        routing = RoutingConfig(
            tables=[
                RoutingTable(
                    rules=[
                        RoutingRule(
                            id="equal",
                            conditions=[
                                RoutingCondition(input="page_count", test="= 5"),
                                RoutingCondition(
                                    input="document_class", test='!= "W2"'
                                ),
                            ],
                        )
                    ]
                )
            ]
        )

        exact = import_xml(export_xml(routing, dmn_version="1.5"))
        legacy = import_xml(export_xml(routing, dmn_version="1.3"))

        assert exact == routing
        tests = [c.test for c in legacy.tables[0].rules[0].conditions]
        assert tests == ["5", 'not("W2")']
        # The same tests, spelled the way DMN 1.3 spells them.
        originals = [c.test for c in routing.tables[0].rules[0].conditions]
        assert [sfeel.parse(t) for t in tests] == [sfeel.parse(t) for t in originals]

    def test_conditions_come_back_in_column_order(self):
        """A rule's conditions all have to hold, so their order carries nothing."""
        first = RoutingCondition(input="page_count", test="> 1")
        second = RoutingCondition(input="reviewed", test="false")
        routing = RoutingConfig(
            tables=[
                RoutingTable(
                    rules=[
                        RoutingRule(id="a", conditions=[first, second]),
                        RoutingRule(id="b", conditions=[second, first]),
                    ]
                )
            ]
        )

        back = import_xml(export_xml(routing)).tables[0]

        assert back.rules[0].conditions == [first, second]
        assert back.rules[1].conditions == [first, second]

    @pytest.mark.parametrize(
        ("table_id", "rule_id"),
        [
            ("2026-routing", "1st-rule"),
            ("review-routing", "Has Capitals"),
            ("t", "ünïcödé"),
        ],
    )
    def test_ids_that_are_not_xml_ids_survive(self, table_id, rule_id):
        routing = RoutingConfig(
            tables=[RoutingTable(id=table_id, rules=[RoutingRule(id=rule_id)])]
        )

        xml = export_xml(routing)
        back = import_xml(xml)

        assert back.tables[0].id == table_id
        assert back.tables[0].rules[0].id == rule_id
        # What is written as the XML id is one a modeler accepts.
        document = read_dmn(xml)
        xml_id = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
        assert xml_id.match(document.table.id), document.table.id
        assert xml_id.match(document.table.rules[0].id), document.table.rules[0].id

    def test_ids_that_become_the_same_xml_id_stay_apart(self):
        routing = RoutingConfig(
            tables=[RoutingTable(rules=[RoutingRule(id="a b"), RoutingRule(id="a_b")])]
        )

        back = import_xml(export_xml(routing))

        assert [rule.id for rule in back.tables[0].rules] == ["a b", "a_b"]

    def test_a_rule_with_no_id_is_given_one(self):
        routing = RoutingConfig(tables=[RoutingTable(rules=[RoutingRule(id="")])])

        back = import_xml(export_xml(routing))

        assert back.tables[0].rules[0].id == "rule_1"

    def test_exactly_one_table_is_exported(self):
        with pytest.raises(DecisionTableError, match="exactly one table"):
            export_xml(RoutingConfig(tables=[]))


def _decision(**changes) -> DecisionTable:
    table = DecisionTable(
        id="Decision_1",
        name="Routing",
        hit_policy="FIRST",
        inputs=[
            TableInput(id="i1", expression="page_count"),
            TableInput(id="i2", expression='field_value("Invoice:Total","any")'),
        ],
        outputs=[
            # A file from before the reason output: two outputs.
            TableOutput(id="o1", name="outcome", default='"no_review"'),
            TableOutput(id="o2", name="queue"),
        ],
        rules=[
            TableRule(
                id="DecisionRule_0bqji8h",
                description="Big invoices",
                input_entries=["-", "> 1000"],
                output_entries=['"queue"', '"approvals"'],
            ),
            TableRule(
                id="DecisionRule_1m87ug5",
                input_entries=["> 20", "-"],
                output_entries=['"queue"', '"default"'],
            ),
        ],
    )
    return table.model_copy(update=changes)


class TestImportFromAModeler:
    def test_a_table_with_no_routing_attributes_gets_the_defaults(self):
        table = from_decision_table(_decision())

        assert table.id == "decision_1"
        assert (table.stage, table.engine) == (
            "after_policy_rule_validation",
            "builtin",
        )
        assert [(rule.id, rule.label, rule.enabled) for rule in table.rules] == [
            ("decisionrule_0bqji8h", "Big invoices", True),
            ("decisionrule_1m87ug5", "", True),
        ]
        assert table.rules[0].conditions == [
            RoutingCondition(
                input="field_value",
                path="Invoice:Total",
                aggregate="any",
                test="> 1000",
            )
        ]
        assert table.rules[0].actions == [RoutingAction(queue="approvals")]
        assert table.default_outcome == RoutingDefaultOutcome(actions=[], reason="")

    def test_a_file_with_two_outputs_imports_with_empty_reasons(self):
        xml = write_decision_table(_decision(), dmn_version="1.3")
        assert "reason" not in xml

        (table,) = import_xml(xml).tables

        assert [rule.reason for rule in table.rules] == ["", ""]
        assert [rule.actions for rule in table.rules] == [
            [RoutingAction(queue="approvals")],
            [RoutingAction(queue="default")],
        ]
        assert table.default_outcome.reason == ""
        # Exported again, it has the three outputs.
        again = read_dmn(export_xml(RoutingConfig(tables=[table]))).table
        assert [output.name for output in again.outputs] == [
            "outcome",
            "queue",
            "reason",
        ]

    def test_a_file_with_three_outputs_brings_its_reasons(self):
        decision = _decision()
        decision.outputs.append(
            TableOutput(id="o3", name="reason", default='"Nothing matched."')
        )
        decision.outputs[0].default = '"queue"'
        decision.outputs[1].default = '"default"'
        decision.rules[0].output_entries.append('"Over {condition_1.value}."')
        # A cell a modeler left empty is an empty reason.
        decision.rules[1].output_entries.append("")

        table = from_decision_table(decision)

        assert [rule.reason for rule in table.rules] == [
            "Over {condition_1.value}.",
            "",
        ]
        assert table.default_outcome == RoutingDefaultOutcome(
            actions=[RoutingAction(queue="default")], reason="Nothing matched."
        )

    def test_a_no_review_row_has_no_action(self):
        decision = _decision()
        decision.rules[1].output_entries = ['"no_review"', '""']

        table = from_decision_table(decision)

        assert table.rules[1].actions == []

    def test_a_queue_row_that_names_no_queue_keeps_its_action(self):
        """So that saving the profile refuses it, and it is not read as no review."""
        decision = _decision()
        decision.rules[1].output_entries = ['"queue"', ""]

        table = from_decision_table(decision)

        assert table.rules[1].actions == [RoutingAction(type="review_queue", queue="")]

    def test_through_xml_written_by_another_tool(self):
        xml = write_decision_table(_decision(), dmn_version="1.3")

        routing = import_xml(xml)

        assert [rule.id for rule in routing.tables[0].rules] == [
            "decisionrule_0bqji8h",
            "decisionrule_1m87ug5",
        ]

    def test_duplicate_rule_ids_are_made_unique(self):
        decision = _decision()
        decision.rules[1].id = "DecisionRule_0bqji8h"

        table = from_decision_table(decision)

        assert [rule.id for rule in table.rules] == [
            "decisionrule_0bqji8h",
            "decisionrule_0bqji8h-2",
        ]

    def test_an_output_with_no_default_is_no_review(self):
        decision = _decision()
        decision.outputs[0].default = ""

        assert from_decision_table(decision).default_outcome == RoutingDefaultOutcome(
            actions=[], reason=""
        )


class TestRoutingRefusals:
    def _refused(self, decision, **kwargs) -> DmnImportError:
        with pytest.raises(DmnImportError) as raised:
            from_decision_table(decision, **kwargs)
        return raised.value

    @pytest.mark.parametrize("hit_policy", ["UNIQUE", "COLLECT", "ANY"])
    def test_a_hit_policy_other_than_first(self, hit_policy):
        error = self._refused(_decision(hit_policy=hit_policy))

        assert error.construct == "hitPolicy"
        assert hit_policy in str(error)

    def test_an_aggregation(self):
        error = self._refused(_decision(), aggregation="SUM")

        assert error.construct == "aggregation"
        assert "SUM" in str(error)

    def test_an_input_expression_that_is_not_a_catalogue_call(self):
        decision = _decision()
        decision.inputs[0].expression = "Applicant.Age"

        error = self._refused(decision)

        assert error.construct == "inputExpression"
        assert "Applicant.Age" in str(error)

    @pytest.mark.parametrize(
        "names",
        [
            ["outcome"],
            ["queue", "outcome"],
            ["outcome", "queue", "extra"],
            ["outcome", "reason", "queue"],
            ["outcome", "queue", "reason", "extra"],
            ["Status", "Rate"],
        ],
    )
    def test_outputs_other_than_outcome_queue_and_reason(self, names):
        decision = _decision(
            outputs=[TableOutput(id=f"o{i}", name=name) for i, name in enumerate(names)]
        )

        error = self._refused(decision)

        assert error.construct == "output"
        assert str(names) in str(error)
        # The refusal names both forms a routing table can have.
        assert "'outcome', 'queue' and 'reason', in that order" in str(error)
        assert "'outcome' and 'queue' alone" in str(error)

    @pytest.mark.parametrize("kind", ["lambda", "Queue", "", "review_queue"])
    def test_an_outcome_that_is_neither_queue_nor_no_review(self, kind):
        decision = _decision()
        decision.rules[0].output_entries[0] = f'"{kind}"'

        error = self._refused(decision)

        assert error.construct == "outputEntry"
        assert str(error) == (
            "Rule 'DecisionRule_0bqji8h': the outcome output must be \"queue\" or "
            '"no_review"'
        )

    def test_a_default_outcome_that_is_neither(self):
        decision = _decision()
        decision.outputs[0].default = '"lambda"'

        error = self._refused(decision)

        assert error.construct == "outputEntry"
        assert "default outcome" in str(error)

    @pytest.mark.parametrize("entries", [['"queue"'], ['"queue"', '"q"', '"r"']])
    def test_a_rule_without_one_entry_per_output(self, entries):
        decision = _decision()
        decision.rules[0].output_entries = entries

        error = self._refused(decision)

        assert error.construct == "rule"
        assert "one entry per input and per output" in str(error)

    def test_a_reason_entry_that_is_not_a_string(self):
        decision = _decision()
        decision.outputs.append(TableOutput(id="o3", name="reason"))
        decision.rules[0].output_entries.append("12")
        decision.rules[1].output_entries.append('""')

        error = self._refused(decision)

        assert error.construct == "outputEntry"
        assert "the reason output must be a string in quotes" in str(error)

    def test_too_many_rules(self):
        rules = [
            TableRule(
                id=f"r{i}", input_entries=["-", "-"], output_entries=['"queue"', '"q"']
            )
            for i in range(MAX_RULES + 1)
        ]

        error = self._refused(_decision(rules=rules))

        assert error.construct == "rule"
        assert str(MAX_RULES + 1) in str(error)

    def test_the_rule_cap_itself_is_accepted(self):
        rules = [
            TableRule(
                id=f"r{i}", input_entries=["-", "-"], output_entries=['"queue"', '"q"']
            )
            for i in range(MAX_RULES)
        ]

        assert len(from_decision_table(_decision(rules=rules)).rules) == MAX_RULES

    def test_a_cell_the_grammar_does_not_accept(self):
        decision = _decision()
        decision.rules[0].input_entries[1] = "sum(Lines) > 1000"

        error = self._refused(decision)

        assert error.construct == "inputEntry"
        assert "DecisionRule_0bqji8h" in str(error)
        assert "field_value" in str(error)

    def test_an_output_entry_that_is_not_a_string(self):
        decision = _decision()
        decision.rules[0].output_entries[0] = "queue"

        error = self._refused(decision)

        assert error.construct == "outputEntry"

    def test_the_refusals_reach_import_xml(self):
        xml = write_decision_table(_decision(hit_policy="UNIQUE"))

        with pytest.raises(DmnImportError) as raised:
            import_xml(xml)

        assert raised.value.construct == "hitPolicy"

    def test_the_generic_refusals_reach_import_xml_too(self):
        with pytest.raises(DmnImportError) as raised:
            import_xml('<?xml version="1.0"?><!DOCTYPE x SYSTEM "x.dtd"><x/>')

        assert raised.value.construct == "DTD"
