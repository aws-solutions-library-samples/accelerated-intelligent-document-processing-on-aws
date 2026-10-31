# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The sample agent's calculator evaluates arithmetic and nothing else."""

import pytest

from idp_common.agents.sample_calculator.calculator_tool import (
    MAX_EXPONENT,
    calculator,
    evaluate,
)


@pytest.mark.unit
class TestArithmetic:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("2 + 3", 5),
            ("(2 + 3) * 4", 20),
            ("7 / 2", 3.5),
            ("7 // 2", 3),
            ("7 % 3", 1),
            ("2 ** 10", 1024),
            ("-4 + +1", -3),
            ("1.5 * 2", 3.0),
            ("  10 - 2 - 3 ", 5),
        ],
    )
    def test_evaluates_the_usual_operators(self, expression, expected):
        assert evaluate(expression) == expected

    def test_the_tool_returns_the_result_as_text(self):
        assert calculator._tool_func("(2 + 3) * 4") == "20"


@pytest.mark.unit
class TestRefusals:
    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('true')",
            "open('/etc/passwd').read()",
            "x + 1",
            "[1, 2][0]",
            "'a' * 3",
            "1 if True else 2",
            "lambda: 1",
            "True + 1",
        ],
    )
    def test_anything_but_arithmetic_is_refused(self, expression):
        with pytest.raises(ValueError):
            evaluate(expression)

    def test_unparseable_text_is_refused(self):
        with pytest.raises(ValueError, match="not an arithmetic expression"):
            evaluate("2 +")

    def test_division_by_zero_is_a_refusal_not_a_crash(self):
        with pytest.raises(ValueError, match="division by zero"):
            evaluate("1 / 0")

    def test_a_runaway_power_is_refused(self):
        with pytest.raises(ValueError, match="too large"):
            evaluate("9 ** 9 ** 9")
        with pytest.raises(ValueError, match="too large"):
            evaluate(f"2 ** {MAX_EXPONENT + 1}")

    def test_the_tool_reports_a_refusal_instead_of_raising(self):
        assert calculator._tool_func("x + 1").startswith("Error: ")
