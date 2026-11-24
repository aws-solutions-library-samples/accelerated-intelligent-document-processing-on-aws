# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Conformance: the DMN TCK's FIRST hit-policy cases, run through this engine.

The models and test files under ``tck/`` are unmodified fixtures from the DMN
Technology Compatibility Kit (see ``tck/PROVENANCE.md``). Each model is read with
the DMN reader, each test case is evaluated with the builtin engine, and every
expected value is compared.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from idp_common.decision_tables import evaluate
from idp_common.decision_tables.dmn import read_decision_table

pytestmark = pytest.mark.unit

_TCK = Path(__file__).parent / "tck"
_CASES = ("0108-first-hitpolicy", "0111-first-hitpolicy-singleoutputcol")
_TESTCASE_NS = "http://www.omg.org/spec/DMN/20160719/testcase"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _q(name: str) -> str:
    return f"{{{_TESTCASE_NS}}}{name}"


def _typed(value_element):
    """A ``value`` element as the Python value its ``xsi:type`` says it is."""
    if value_element.get(f"{{{_XSI}}}nil") == "true":
        return None
    xsi_type = (value_element.get(f"{{{_XSI}}}type") or "").split(":")[-1]
    text = value_element.text or ""
    if xsi_type in ("decimal", "double"):
        return Decimal(text)
    if xsi_type == "boolean":
        return text.strip() == "true"
    if xsi_type == "string":
        return text
    raise AssertionError(f"unhandled xsi:type {xsi_type!r} in a TCK test file")


def _expected(result_node):
    """The expected outputs: by component name, or one value for one output."""
    expected = result_node.find(_q("expected"))
    components = expected.findall(_q("component"))
    if components:
        return {c.get("name"): _typed(c.find(_q("value"))) for c in components}
    return _typed(expected.find(_q("value")))


def _load_test_cases(test_file: Path):
    """``(case id, inputs, expected)`` for every test case in a TCK test file."""
    import defusedxml.ElementTree as SafeET

    root = SafeET.fromstring(test_file.read_bytes(), forbid_dtd=True)
    cases = []
    for case in root.findall(_q("testCase")):
        inputs = {
            node.get("name"): _typed(node.find(_q("value")))
            for node in case.findall(_q("inputNode"))
        }
        (result_node,) = case.findall(_q("resultNode"))
        cases.append((case.get("id"), inputs, _expected(result_node)))
    return cases


def run_tck_case(model_file: Path, test_file: Path) -> list[str]:
    """Evaluate every test case; return a line per value that differs."""
    table = read_decision_table(model_file.read_bytes())
    mismatches = []
    for case_id, inputs, expected in _load_test_cases(test_file):
        outputs = evaluate(table, inputs).outputs
        if not isinstance(expected, dict):
            (actual,) = outputs.values()
            if actual != expected:
                mismatches.append(f"{case_id}: expected {expected!r}, got {actual!r}")
            continue
        for name, value in expected.items():
            if name not in outputs or outputs[name] != value:
                mismatches.append(
                    f"{case_id}.{name}: expected {value!r}, got {outputs.get(name)!r}"
                )
    return mismatches


def _files(case: str) -> tuple[Path, list[Path]]:
    directory = _TCK / case
    (model,) = directory.glob("*.dmn")
    return model, sorted(directory.glob("*-test-*.xml"))


@pytest.mark.parametrize("case", _CASES)
def test_every_tck_test_case_passes(case):
    model, test_files = _files(case)
    assert test_files, f"no TCK test file for {case}"

    for test_file in test_files:
        assert _load_test_cases(test_file), f"{test_file.name} holds no test case"
        assert run_tck_case(model, test_file) == []


@pytest.mark.parametrize("case", _CASES)
def test_the_models_are_first_hit_policy_tables(case):
    model, _ = _files(case)

    table = read_decision_table(model.read_bytes())

    assert table.hit_policy == "FIRST"
    assert len(table.rules) == 3


def test_the_multiple_output_model_reads_as_expected():
    """What the reader made of the model, so a pass above means what it says."""
    model, _ = _files("0108-first-hitpolicy")

    table = read_decision_table(model.read_bytes())

    assert [column.expression for column in table.inputs] == [
        "Age",
        "RiskCategory",
        "isAffordable",
    ]
    assert table.inputs[1].allowed_values == '"High", "Low", "Medium"'
    assert [(o.name, o.default) for o in table.outputs] == [
        ("Status", '"Declined"'),
        ("Rate", '"Standard"'),
    ]
    assert table.rules[0].input_entries == [">=18", '"Medium"', "true"]


def test_the_single_output_takes_the_decisions_name():
    model, _ = _files("0111-first-hitpolicy-singleoutputcol")

    table = read_decision_table(model.read_bytes())

    assert [o.name for o in table.outputs] == ["Advertisement"]
    assert evaluate(table, {"age": Decimal("19")}).outputs == {"Advertisement": "Cars"}


@pytest.mark.parametrize(
    ("case", "original", "changed"),
    [
        ("0108-first-hitpolicy", ">Best<", ">Standard<"),
        ("0111-first-hitpolicy-singleoutputcol", ">Videogames<", ">Toys<"),
    ],
)
def test_a_changed_expected_value_is_reported(tmp_path, case, original, changed):
    """The comparison reads the expected values: change one and it must differ."""
    model, (test_file,) = _files(case)
    text = test_file.read_text(encoding="utf-8")
    assert text.count(original) == 1
    altered = tmp_path / test_file.name
    altered.write_text(text.replace(original, changed), encoding="utf-8")

    mismatches = run_tck_case(model, altered)

    assert len(mismatches) == 1
    assert "expected" in mismatches[0]


def test_first_wins_where_two_tck_rules_match():
    """Age 19 satisfies both ``>=18`` and ``>=12``; FIRST takes the earlier rule."""
    model, _ = _files("0108-first-hitpolicy")
    table = read_decision_table(model.read_bytes())

    result = evaluate(
        table, {"Age": Decimal("19"), "RiskCategory": "Medium", "isAffordable": True}
    )

    assert result.matched_rule_id == table.rules[0].id
    assert result.outputs == {"Status": "Approved", "Rate": "Best"}


def test_a_risk_category_outside_the_allowed_values_gives_no_result():
    model, _ = _files("0108-first-hitpolicy")
    table = read_decision_table(model.read_bytes())

    result = evaluate(
        table, {"Age": Decimal("19"), "RiskCategory": "Unknown", "isAffordable": True}
    )

    assert result.matched_rule_id == ""
    assert result.outputs == {"Status": None, "Rate": None}


def test_no_matching_rule_gives_the_models_default_outputs():
    model, _ = _files("0108-first-hitpolicy")
    table = read_decision_table(model.read_bytes())

    result = evaluate(
        table, {"Age": Decimal("40"), "RiskCategory": "High", "isAffordable": False}
    )

    assert result.default_used is True
    assert result.outputs == {"Status": "Declined", "Rate": "Standard"}
