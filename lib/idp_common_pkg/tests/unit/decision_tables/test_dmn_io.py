# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DMN XML: round trips, the three accepted namespaces, and what is refused."""

import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys

import pytest

from idp_common.decision_tables import sfeel
from idp_common.decision_tables.dmn import (
    DMN_NAMESPACES,
    MAX_DMN_BYTES,
    DmnImportError,
    read_decision_table,
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

pytestmark = pytest.mark.unit

_EXT = "urn:example:extension:1"


def _table() -> DecisionTable:
    return DecisionTable(
        id="review-routing",
        name="Review routing",
        hit_policy="FIRST",
        inputs=[
            TableInput(
                id="in_1", label="Reviewed", expression="reviewed", type_ref="boolean"
            ),
            TableInput(
                id="in_2",
                label="Invoice total",
                expression='field_value("Invoice:Total","any")',
                type_ref="number",
                allowed_values=">= 0",
            ),
            TableInput(id="in_3", expression="document_class", type_ref="string"),
        ],
        outputs=[
            TableOutput(
                id="out_1", name="outcome", type_ref="string", default='"no_review"'
            ),
            TableOutput(id="out_2", name="queue", type_ref="string", default='""'),
        ],
        rules=[
            TableRule(
                id="high-value",
                description='Élevé & <très> "grand"',
                input_entries=["false", "= 1000", '"Invoice", "Receipt"'],
                output_entries=['"queue"', '"approvals"'],
            ),
            TableRule(
                id="not-a-w2",
                input_entries=["-", "[1..5)", '!= "W2"'],
                output_entries=['"no_review"', '""'],
            ),
        ],
    )


def _same_tests(first: DecisionTable, second: DecisionTable) -> bool:
    """Equal apart from the spelling of a test (``= 5`` and ``5`` are one test)."""

    def shape(table: DecisionTable):
        return table.model_dump(
            exclude={
                "rules": {"__all__": {"input_entries"}},
                "inputs": {"__all__": {"allowed_values"}},
            }
        )

    def tests(table: DecisionTable):
        return [
            [sfeel.parse(entry) for entry in rule.input_entries] for rule in table.rules
        ] + [[sfeel.parse(column.allowed_values or "-") for column in table.inputs]]

    return shape(first) == shape(second) and tests(first) == tests(second)


class TestRoundTrip:
    def test_dmn_15_reads_back_exactly(self):
        table = _table()

        xml = write_decision_table(table)

        assert DMN_NAMESPACES["1.5"] in xml
        assert read_decision_table(xml) == table
        assert read_dmn(xml).version == "1.5"

    def test_dmn_13_reads_back_as_the_same_tests(self):
        table = _table()

        xml = write_decision_table(table, dmn_version="1.3")
        back = read_decision_table(xml)

        assert DMN_NAMESPACES["1.3"] in xml
        assert DMN_NAMESPACES["1.5"] not in xml
        assert read_dmn(xml).version == "1.3"
        assert _same_tests(back, table)
        # DMN 1.3 has no = or != operator: equality is the bare literal and
        # inequality is not(...).
        entries = [entry for rule in back.rules for entry in rule.input_entries]
        assert entries == [
            "false",
            "1000",
            '"Invoice", "Receipt"',
            "-",
            "[1..5)",
            'not("W2")',
        ]
        assert not [
            entry for entry in entries if entry.lstrip().startswith(("=", "!="))
        ]

    def test_the_two_forms_read_back_as_the_same_table(self):
        table = _table()

        from_15 = read_decision_table(write_decision_table(table))
        from_13 = read_decision_table(write_decision_table(table, dmn_version="1.3"))

        assert _same_tests(from_15, from_13)

    def test_the_comparison_helper_can_tell_tables_apart(self):
        """Guards the helper above: a different test is a different table."""
        other = _table()
        other.rules[0].input_entries[1] = "= 1001"

        assert not _same_tests(other, _table())

    def test_text_is_escaped_and_restored(self):
        xml = write_decision_table(_table())

        assert "&amp;" in xml and "&lt;très&gt;" in xml
        assert read_decision_table(xml).rules[0].description == (
            'Élevé & <très> "grand"'
        )

    def test_foreign_attributes_round_trip(self):
        xml = write_decision_table(
            _table(),
            extra_table_attributes={f"{{{_EXT}}}stage": "after"},
            rule_attributes={"not-a-w2": {f"{{{_EXT}}}enabled": "false"}},
            namespace_prefixes={"ext": _EXT},
        )

        document = read_dmn(xml)

        assert 'ext:stage="after"' in xml
        assert "ns0" not in xml
        assert document.table_attributes == {f"{{{_EXT}}}stage": "after"}
        assert document.rule_attributes == {"not-a-w2": {f"{{{_EXT}}}enabled": "false"}}

    def test_a_foreign_attribute_needs_a_declared_prefix(self):
        with pytest.raises(DecisionTableError, match="prefix"):
            write_decision_table(
                _table(), extra_table_attributes={f"{{{_EXT}}}stage": "after"}
            )

    def test_the_file_has_a_diagram_for_graphical_modelers(self):
        xml = write_decision_table(_table())

        assert "<dmndi:DMNDI>" in xml
        assert 'dmnElementRef="review-routing"' in xml

    def test_an_unknown_version_cannot_be_written(self):
        with pytest.raises(DecisionTableError, match="'1.1'"):
            write_decision_table(_table(), dmn_version="1.1")

    def test_a_control_character_cannot_be_written(self):
        table = _table()
        table.rules[0].description = "bad \x01 label"

        with pytest.raises(DecisionTableError, match="control character"):
            write_decision_table(table)


def _minimal(namespace: str, body: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="{namespace}" id="d" name="d" namespace="urn:test">
  <decision id="approval" name="Approval">
    <decisionTable hitPolicy="FIRST">
      <input label="Age"><inputExpression typeRef="number"><text>Age</text></inputExpression></input>
      <output name="Status" typeRef="string"/>
      <rule><inputEntry><text>&gt;= 18</text></inputEntry><outputEntry><text>"Approved"</text></outputEntry></rule>
      <rule><inputEntry><text></text></inputEntry><outputEntry><text>"Declined"</text></outputEntry></rule>
    </decisionTable>
  </decision>
  {body}
</definitions>"""


class TestReader:
    @pytest.mark.parametrize("version", ["1.3", "1.4", "1.5"])
    def test_every_dmn_namespace_is_accepted(self, version):
        document = read_dmn(_minimal(DMN_NAMESPACES[version]))

        assert document.version == version
        assert document.table.id == "approval"
        assert document.table.name == "Approval"
        assert document.table.hit_policy == "FIRST"
        assert [column.expression for column in document.table.inputs] == ["Age"]

    def test_ids_a_modeler_left_out_are_generated(self):
        table = read_decision_table(_minimal(DMN_NAMESPACES["1.5"]))

        assert [column.id for column in table.inputs] == ["input_1"]
        assert [column.id for column in table.outputs] == ["output_1"]
        assert [rule.id for rule in table.rules] == ["rule_1", "rule_2"]

    def test_an_empty_cell_is_any_value(self):
        table = read_decision_table(_minimal(DMN_NAMESPACES["1.3"]))

        assert table.rules[1].input_entries == ["-"]

    def test_a_missing_hit_policy_is_dmns_default(self):
        xml = _minimal(DMN_NAMESPACES["1.5"]).replace(' hitPolicy="FIRST"', "")

        assert read_decision_table(xml).hit_policy == "UNIQUE"

    def test_the_hit_policy_and_aggregation_are_returned_not_judged(self):
        xml = _minimal(DMN_NAMESPACES["1.5"]).replace(
            'hitPolicy="FIRST"', 'hitPolicy="COLLECT" aggregation="SUM"'
        )

        document = read_dmn(xml)

        assert document.table.hit_policy == "COLLECT"
        assert document.aggregation == "SUM"

    def test_bytes_are_accepted(self):
        table = read_decision_table(_minimal(DMN_NAMESPACES["1.5"]).encode("utf-8"))

        assert table.id == "approval"

    def test_item_definitions_input_data_and_vendor_attributes_are_ignored(self):
        body = """
  <itemDefinition name="tAge"><typeRef>number</typeRef></itemDefinition>
  <inputData id="age" name="Age"><variable name="Age" typeRef="tAge"/></inputData>
"""
        xml = _minimal(DMN_NAMESPACES["1.5"], body).replace(
            '<decision id="approval"',
            '<decision xmlns:camunda="http://camunda.org/schema/1.0/dmn" '
            'camunda:historyTimeToLive="5" id="approval"',
        )

        assert read_decision_table(xml).rules[0].output_entries == ['"Approved"']


class TestRefusals:
    def test_an_unknown_namespace_is_named(self):
        with pytest.raises(DmnImportError) as raised:
            read_dmn(_minimal("http://www.omg.org/spec/DMN/20151101/dmn.xsd"))

        assert raised.value.construct == "namespace"
        assert "http://www.omg.org/spec/DMN/20151101/dmn.xsd" in str(raised.value)

    def test_xml_that_is_not_dmn_at_all(self):
        with pytest.raises(DmnImportError) as raised:
            read_dmn("<x/>")

        assert raised.value.construct == "namespace"

    def test_malformed_xml(self):
        with pytest.raises(DmnImportError) as raised:
            read_dmn("<definitions")

        assert raised.value.construct == "XML"

    def test_a_billion_laughs_payload_is_refused(self):
        payload = f"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<definitions xmlns="{DMN_NAMESPACES["1.5"]}" name="&lol3;"/>"""

        with pytest.raises(DmnImportError) as raised:
            read_dmn(payload)

        assert raised.value.construct == "DTD"

    def test_an_external_entity_payload_is_refused(self):
        payload = f"""<?xml version="1.0"?>
<!DOCTYPE definitions [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<definitions xmlns="{DMN_NAMESPACES["1.5"]}" name="&xxe;"/>"""

        with pytest.raises(DmnImportError) as raised:
            read_dmn(payload)

        assert raised.value.construct == "DTD"

    def test_a_doctype_that_declares_no_entity_is_refused_too(self):
        """Any DTD is refused, not only one whose entities would expand."""
        payload = (
            '<?xml version="1.0"?>\n<!DOCTYPE definitions SYSTEM "dmn.dtd">\n'
            + _minimal(DMN_NAMESPACES["1.5"]).split("\n", 1)[1]
        )

        with pytest.raises(DmnImportError) as raised:
            read_dmn(payload)

        assert raised.value.construct == "DTD"

    def test_oversized_input_is_refused_before_parsing(self, monkeypatch):
        import defusedxml.ElementTree as safe_et

        def _must_not_parse(*args, **kwargs):
            raise AssertionError("the size check must come before the parser")

        monkeypatch.setattr(safe_et, "fromstring", _must_not_parse)
        oversized = _minimal(DMN_NAMESPACES["1.5"]) + " " * MAX_DMN_BYTES

        for payload in (oversized, oversized.encode("utf-8")):
            with pytest.raises(DmnImportError) as raised:
                read_dmn(payload)
            assert raised.value.construct == "size"
            assert str(MAX_DMN_BYTES) in str(raised.value)

    def test_input_at_the_limit_is_parsed(self):
        xml = _minimal(DMN_NAMESPACES["1.5"])
        padded = xml + " " * (MAX_DMN_BYTES - len(xml.encode("utf-8")))

        assert len(padded.encode("utf-8")) == MAX_DMN_BYTES
        assert read_decision_table(padded).id == "approval"

    def test_two_decisions_are_refused(self):
        body = (
            '<decision id="second" name="Second">'
            '<decisionTable><output name="x"/></decisionTable></decision>'
        )

        with pytest.raises(DmnImportError) as raised:
            read_dmn(_minimal(DMN_NAMESPACES["1.5"], body))

        assert raised.value.construct == "decision"
        assert "2 decision" in str(raised.value)

    def test_no_decision_is_refused(self):
        xml = f'<definitions xmlns="{DMN_NAMESPACES["1.5"]}" name="d"/>'

        with pytest.raises(DmnImportError) as raised:
            read_dmn(xml)

        assert raised.value.construct == "decision"

    def test_a_literal_expression_decision_is_refused(self):
        xml = f"""<definitions xmlns="{DMN_NAMESPACES["1.5"]}" name="d">
  <decision id="a" name="A"><literalExpression><text>1 + 1</text></literalExpression></decision>
</definitions>"""

        with pytest.raises(DmnImportError) as raised:
            read_dmn(xml)

        assert raised.value.construct == "literalExpression"
        assert "literalExpression" in str(raised.value)

    def test_a_business_knowledge_model_is_refused(self):
        body = '<businessKnowledgeModel id="bkm" name="Scoring"/>'

        with pytest.raises(DmnImportError) as raised:
            read_dmn(_minimal(DMN_NAMESPACES["1.5"], body))

        assert raised.value.construct == "businessKnowledgeModel"

    def test_a_knowledge_requirement_is_refused(self):
        xml = _minimal(DMN_NAMESPACES["1.5"]).replace(
            "<decisionTable",
            '<knowledgeRequirement><requiredKnowledge href="#bkm"/>'
            "</knowledgeRequirement><decisionTable",
        )

        with pytest.raises(DmnImportError) as raised:
            read_dmn(xml)

        assert raised.value.construct == "knowledgeRequirement"

    def test_a_chained_decision_is_refused(self):
        xml = _minimal(DMN_NAMESPACES["1.5"]).replace(
            "<decisionTable",
            '<informationRequirement><requiredDecision href="#other"/>'
            "</informationRequirement><decisionTable",
        )

        with pytest.raises(DmnImportError) as raised:
            read_dmn(xml)

        assert raised.value.construct == "requiredDecision"

    def test_a_required_input_is_not_a_chained_decision(self):
        xml = _minimal(DMN_NAMESPACES["1.5"]).replace(
            "<decisionTable",
            '<informationRequirement><requiredInput href="#age"/>'
            "</informationRequirement><decisionTable",
        )

        assert read_decision_table(xml).id == "approval"

    def test_a_refusal_is_a_decision_table_error(self):
        with pytest.raises(DecisionTableError):
            read_dmn("<x/>")


_IMPORT_PROBE = (
    "import sys\n"
    "import idp_common.decision_tables\n"
    "from idp_common.decision_tables import evaluate, DecisionTable\n"
    "import idp_common.decision_tables.dmn\n"
    "print('defusedxml' in sys.modules)\n"
)


def test_neither_the_package_nor_the_dmn_module_imports_the_parser_at_load():
    """``defusedxml`` is loaded by the first parse, not by an import."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    run = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().splitlines()[-1] == "False"
