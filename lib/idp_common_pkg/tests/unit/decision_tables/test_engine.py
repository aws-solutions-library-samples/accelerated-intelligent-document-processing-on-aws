# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The builtin decision-table engine: FIRST, defaults, quantifiers, determinism."""

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from decimal import Decimal

import pytest

from idp_common.decision_tables import ENGINES, DecisionTable, evaluate
from idp_common.decision_tables.engine import BuiltinEngine
from idp_common.decision_tables.models import (
    DecisionTableError,
    TableInput,
    TableOutput,
    TableRule,
    UnaryTestSyntaxError,
    UnknownEngine,
    UnsupportedHitPolicy,
)

pytestmark = pytest.mark.unit


def _table(rules, *, hit_policy="FIRST", inputs=None, outputs=None):
    return DecisionTable(
        id="t",
        hit_policy=hit_policy,
        inputs=inputs
        or [
            TableInput(id="i1", expression="amount"),
            TableInput(id="i2", expression="class"),
        ],
        outputs=outputs
        or [
            TableOutput(id="o1", name="outcome", default='"no_review"'),
            TableOutput(id="o2", name="queue"),
        ],
        rules=rules,
    )


def _rule(rule_id, inputs, outputs):
    return TableRule(id=rule_id, input_entries=inputs, output_entries=outputs)


_RULES = [
    _rule("high", ["> 1000", "-"], ['"queue"', '"approvals"']),
    _rule("invoice", ["-", '"Invoice"'], ['"queue"', '"default"']),
]


class TestFirstHitPolicy:
    def test_first_match_wins(self):
        # Both rules match; the first in table order decides.
        result = evaluate(_table(_RULES), {"amount": 5000, "class": "Invoice"})

        assert result.matched_rule_id == "high"
        assert result.outputs == {"outcome": "queue", "queue": "approvals"}
        assert result.default_used is False

    def test_a_later_rule_matches_when_the_first_does_not(self):
        result = evaluate(_table(_RULES), {"amount": 10, "class": "Invoice"})

        assert result.matched_rule_id == "invoice"
        assert result.outputs == {"outcome": "queue", "queue": "default"}

    def test_no_match_uses_the_default_outputs(self):
        result = evaluate(_table(_RULES), {"amount": 10, "class": "W2"})

        assert result.matched_rule_id == ""
        assert result.default_used is True
        # An output with no default is None.
        assert result.outputs == {"outcome": "no_review", "queue": None}

    def test_an_unknown_test_result_does_not_match(self):
        # `> 1000` on a missing value is unknown, which is not a match.
        result = evaluate(_table(_RULES[:1]), {"class": "Invoice"})

        assert result.default_used is True

    def test_a_table_with_no_rules_gives_the_defaults(self):
        result = evaluate(_table([]), {"amount": 1})

        assert result.default_used is True
        assert result.outputs == {"outcome": "no_review", "queue": None}

    @pytest.mark.parametrize("hit_policy", ["UNIQUE", "ANY", "COLLECT", "first", ""])
    def test_another_hit_policy_is_refused_by_name(self, hit_policy):
        with pytest.raises(UnsupportedHitPolicy) as raised:
            evaluate(_table(_RULES, hit_policy=hit_policy), {})
        assert repr(hit_policy) in str(raised.value)

    def test_the_model_default_hit_policy_is_not_evaluated(self):
        """DMN's default is UNIQUE, so a table must ask for FIRST to be run."""
        table = DecisionTable(id="t")
        assert table.hit_policy == "UNIQUE"
        with pytest.raises(UnsupportedHitPolicy):
            evaluate(table, {})


class TestOutputs:
    def test_output_entries_are_typed_literals(self):
        table = _table(
            [_rule("r", ["-"], ['"text"', "12", "0.5", "true", "null"])],
            inputs=[TableInput(id="i", expression="x")],
            outputs=[TableOutput(id=f"o{i}", name=f"o{i}") for i in range(5)],
        )

        result = evaluate(table, {})

        assert result.outputs == {
            "o0": "text",
            "o1": 12,
            "o2": Decimal("0.5"),
            "o3": True,
            "o4": None,
        }

    def test_a_rule_with_the_wrong_number_of_entries_is_refused(self):
        table = _table([_rule("short", ["-"], ['"queue"', '"q"'])])

        with pytest.raises(DecisionTableError, match="'short'"):
            evaluate(table, {})

    def test_a_bad_entry_raises_a_syntax_error(self):
        table = _table([_rule("bad", ["> ", "-"], ['"queue"', '"q"'])])

        with pytest.raises(UnaryTestSyntaxError):
            evaluate(table, {"amount": 1})


class TestAllowedValues:
    def _restricted(self):
        return _table(
            _RULES,
            inputs=[
                TableInput(id="i1", expression="amount"),
                TableInput(
                    id="i2", expression="class", allowed_values='"Invoice", "W2"'
                ),
            ],
        )

    def test_a_value_outside_the_allowed_values_gives_no_result(self):
        result = evaluate(self._restricted(), {"amount": 5000, "class": "Receipt"})

        assert result.matched_rule_id == ""
        assert result.default_used is False
        assert result.outputs == {"outcome": None, "queue": None}

    def test_an_allowed_value_is_evaluated_normally(self):
        result = evaluate(self._restricted(), {"amount": 5000, "class": "W2"})

        assert result.matched_rule_id == "high"


class TestQuantifiers:
    def _table(self, quantifier, entry='"Bank-checks"'):
        return _table(
            [_rule("r", [entry], ['"queue"', '"specialist"'])],
            inputs=[TableInput(id="i", expression="classes", quantifier=quantifier)],
        )

    def test_any_needs_one_element(self):
        table = self._table("any")

        assert evaluate(table, {"classes": ["W2", "Bank-checks"]}).matched_rule_id
        assert not evaluate(table, {"classes": ["W2", "Payslip"]}).matched_rule_id

    def test_all_needs_every_element(self):
        table = self._table("all")

        assert evaluate(
            table, {"classes": ["Bank-checks", "Bank-checks"]}
        ).matched_rule_id
        assert not evaluate(table, {"classes": ["W2", "Bank-checks"]}).matched_rule_id

    @pytest.mark.parametrize("quantifier", ["any", "all"])
    def test_an_empty_list_is_false_for_both(self, quantifier):
        result = evaluate(self._table(quantifier), {"classes": []})

        assert result.default_used is True

    @pytest.mark.parametrize("quantifier", ["any", "all"])
    def test_dash_matches_an_empty_list(self, quantifier):
        """A rule that does not use the column must not depend on its content."""
        result = evaluate(self._table(quantifier, entry="-"), {"classes": []})

        assert result.matched_rule_id == "r"

    def test_an_unknown_element_is_not_a_match(self):
        table = self._table("all", entry="> 5")

        assert not evaluate(table, {"classes": [9, None]}).matched_rule_id
        assert evaluate(table, {"classes": [9, 7]}).matched_rule_id

    def test_a_quantifier_on_a_scalar_tests_the_scalar(self):
        assert evaluate(self._table("any"), {"classes": "Bank-checks"}).matched_rule_id

    def test_without_a_quantifier_a_list_is_tested_as_a_list(self):
        result = evaluate(self._table(""), {"classes": ["Bank-checks"]})

        assert result.default_used is True


class TestInputsUsedAndDeterminism:
    def test_inputs_used_holds_exactly_the_tables_inputs(self):
        result = evaluate(
            _table(_RULES), {"amount": 5, "class": "W2", "unrelated": "ignored"}
        )

        assert result.inputs_used == {"amount": 5, "class": "W2"}

    def test_a_missing_input_is_recorded_as_none(self):
        result = evaluate(_table(_RULES), {"class": "W2"})

        assert result.inputs_used == {"amount": None, "class": "W2"}

    def test_two_evaluations_of_the_same_inputs_are_equal(self):
        table = _table(_RULES)
        context = {"amount": Decimal("1000.01"), "class": "Invoice"}

        first = evaluate(table, context)
        second = evaluate(table, dict(context))

        assert first == second
        assert first.model_dump() == second.model_dump()


class TestEngineRegistry:
    def test_builtin_is_the_default_and_the_only_engine(self):
        assert set(ENGINES) == {"builtin"}
        assert isinstance(ENGINES["builtin"], BuiltinEngine)
        assert evaluate(_table(_RULES), {"amount": 5000}) == evaluate(
            _table(_RULES), {"amount": 5000}, engine="builtin"
        )

    def test_an_unregistered_engine_is_named(self):
        with pytest.raises(UnknownEngine, match="'camunda'"):
            evaluate(_table(_RULES), {}, engine="camunda")

    def test_a_registered_engine_is_used(self, monkeypatch):
        calls = []

        class _Recording:
            def evaluate(self, table, context):
                calls.append((table.id, dict(context)))
                return BuiltinEngine().evaluate(table, context)

        monkeypatch.setitem(ENGINES, "recording", _Recording())

        result = evaluate(_table(_RULES), {"amount": 5000}, engine="recording")

        assert calls == [("t", {"amount": 5000})]
        assert result.matched_rule_id == "high"


_IMPORT_PROBE = r"""
import json
import sys

before = set(sys.modules)
import idp_common.decision_tables as package
from idp_common.decision_tables import DecisionTable, evaluate

evaluate(DecisionTable(id="t", hit_policy="FIRST"), {})
added = sorted(set(sys.modules) - before)
stdlib = set(sys.stdlib_module_names)
print(
    json.dumps(
        {
            "first_party": [m for m in added if m.split(".")[0] == "idp_common"],
            "third_party": sorted(
                {
                    m.split(".")[0]
                    for m in added
                    if m.split(".")[0] not in stdlib
                    and m.split(".")[0] != "idp_common"
                    and not m.startswith("_")
                }
            ),
        }
    )
)
"""


class TestImportWeight:
    def test_the_package_loads_only_pydantic_and_the_light_path_module(self):
        """The routing Lambda evaluates tables with the light extras only."""
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
        loaded = json.loads(run.stdout.strip().splitlines()[-1])

        assert loaded["first_party"] == [
            "idp_common",
            "idp_common.decision_tables",
            "idp_common.decision_tables.engine",
            "idp_common.decision_tables.models",
            "idp_common.decision_tables.sfeel",
            "idp_common.field_paths",
        ]
        # pydantic and what pydantic itself imports; in particular no XML parser.
        assert set(loaded["third_party"]) <= {
            "pydantic",
            "pydantic_core",
            "typing_extensions",
            "annotated_types",
            "typing_inspection",
        }, loaded["third_party"]
        assert "pydantic" in loaded["third_party"]
