# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The S-FEEL unary-test subset: grammar, three-valued results, syntax errors."""

from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from idp_common.decision_tables.models import (
    DecisionTableError,
    UnaryTestSyntaxError,
)
from idp_common.decision_tables.sfeel import (
    BOOLEAN,
    DATE,
    NUMBER,
    STRING,
    Comparison,
    Interval,
    NullTest,
    UnaryTests,
    format_literal,
    matches,
    parse,
    parse_literal,
    to_dmn13,
)

pytestmark = pytest.mark.unit


def _result(text, value):
    return matches(parse(text), value)


class TestGrammar:
    """One case per production."""

    def test_dash_matches_any_value(self):
        tests = parse("-")
        assert tests == UnaryTests(any_value=True)
        assert parse("  -  ") == tests

    def test_a_bare_endpoint_is_equality(self):
        (test,) = parse("5").tests
        assert test == Comparison("=", parse("= 5").tests[0].endpoint)
        assert test.endpoint.kind == NUMBER
        assert test.endpoint.value == Fraction(5)

    @pytest.mark.parametrize("op", ["<", "<=", ">", ">=", "=", "!="])
    def test_every_operator(self, op):
        (test,) = parse(f"{op} 10").tests
        assert isinstance(test, Comparison)
        assert test.op == op

    def test_an_operator_needs_no_space(self):
        assert parse(">=18") == parse(">= 18")

    def test_null(self):
        assert parse("null").tests == (NullTest(),)

    def test_a_list_of_tests(self):
        tests = parse('"Low", "Medium", > 5, null')
        assert [type(t) for t in tests.tests] == [
            Comparison,
            Comparison,
            Comparison,
            NullTest,
        ]

    def test_not_wraps_a_list(self):
        tests = parse("not(1, 2)")
        assert tests.negated is True
        assert len(tests.tests) == 2
        assert parse("not ( 1 , 2 )") == tests

    @pytest.mark.parametrize(
        ("text", "low_closed", "high_closed"),
        [
            ("[1..5]", True, True),
            ("(1..5)", False, False),
            ("]1..5[", False, False),
            ("[1..5)", True, False),
            ("(1..5]", False, True),
            ("[1..5[", True, False),
            ("]1..5]", False, True),
        ],
    )
    def test_intervals_in_both_bracket_spellings(self, text, low_closed, high_closed):
        (test,) = parse(text).tests
        assert isinstance(test, Interval)
        assert (test.low_closed, test.high_closed) == (low_closed, high_closed)
        assert (test.low.value, test.high.value) == (Fraction(1), Fraction(5))

    @pytest.mark.parametrize(
        ("text", "kind", "value"),
        [
            ("12", NUMBER, Fraction(12)),
            ("-3", NUMBER, Fraction(-3)),
            ("0.25", NUMBER, Fraction(1, 4)),
            ("-10.50", NUMBER, Fraction(-21, 2)),
            ('"Invoice"', STRING, "Invoice"),
            ('""', STRING, ""),
            ("true", BOOLEAN, True),
            ("false", BOOLEAN, False),
            ('date("2026-01-31")', DATE, date(2026, 1, 31)),
        ],
    )
    def test_every_endpoint_kind(self, text, kind, value):
        (test,) = parse(text).tests
        assert (test.endpoint.kind, test.endpoint.value) == (kind, value)

    def test_a_string_with_escaped_quotes_and_backslashes(self):
        (test,) = parse(r'"say \"hi\" \\ bye"').tests
        assert test.endpoint.value == r'say "hi" \ bye'
        assert _result(r'"say \"hi\" \\ bye"', r'say "hi" \ bye') is True

    def test_a_comma_inside_a_string_is_not_a_separator(self):
        assert len(parse('"a, b", "c"').tests) == 2

    def test_literal_kinds(self):
        assert parse('> 5, "x"').literal_kinds() == {NUMBER, STRING}
        assert parse('[date("2026-01-01")..date("2026-12-31")]').literal_kinds() == {
            DATE
        }
        assert parse("-").literal_kinds() == frozenset()
        assert parse("null").literal_kinds() == frozenset()

    def test_equivalent_spellings_parse_equal(self):
        assert parse("5") == parse("= 5")
        assert parse("not(5)") == parse("!= 5")
        assert parse('not(!= "a")') == parse('"a"')
        assert parse("5.0") == parse("5")
        assert parse("> 5") != parse(">= 5")


class TestNumbers:
    @pytest.mark.parametrize(
        "value", [10, 10.0, Decimal("10"), Decimal("10.000"), "10", " 10.0 "]
    )
    def test_a_number_in_any_spelling_equals_the_literal(self, value):
        assert _result("= 10", value) is True
        assert _result("10", value) is True
        assert _result("!= 10", value) is False
        assert _result(">= 10", value) is True
        assert _result("> 10", value) is False
        assert _result("< 10.5", value) is True
        assert _result("<= 9.99", value) is False

    def test_a_decimal_is_compared_exactly(self):
        # A double cannot tell these apart; an exact reading can.
        value = Decimal("0.1000000000000000000001")
        assert _result("= 0.1", value) is False
        assert _result("> 0.1", value) is True

    def test_a_float_is_its_exact_value(self):
        assert _result("= 0.5", 0.5) is True
        # 0.1 as a double is not the decimal 0.1.
        assert _result("= 0.1", 0.1) is False

    def test_a_boolean_is_not_a_number(self):
        assert _result("= 1", True) is False
        assert _result("> 0", True) is None
        assert _result("[0..1]", False) is None

    def test_a_non_numeric_string_is_another_type(self):
        assert _result("= 5", "five") is False
        assert _result("!= 5", "five") is True
        assert _result("> 5", "five") is None
        assert _result("> 5", "1,000") is None

    def test_negative_numbers(self):
        assert _result("< -1", -2) is True
        assert _result("[-5..-1]", -3) is True
        assert _result("-5", -5) is True


class TestIntervals:
    def test_closed_ends_include_the_endpoints(self):
        assert _result("[1..5]", 1) is True
        assert _result("[1..5]", 5) is True
        assert _result("[1..5]", 6) is False

    @pytest.mark.parametrize("text", ["(1..5)", "]1..5["])
    def test_open_ends_exclude_the_endpoints(self, text):
        assert _result(text, 1) is False
        assert _result(text, 5) is False
        assert _result(text, 3) is True

    def test_half_open(self):
        assert _result("[1..5)", 1) is True
        assert _result("[1..5)", 5) is False
        assert _result("(1..5]", 1) is False
        assert _result("(1..5]", 5) is True

    def test_strings_and_dates_have_ranges_too(self):
        assert _result('["a".."m"]', "kiwi") is True
        assert _result('["a".."m"]', "pear") is False
        assert _result('[date("2026-01-01")..date("2026-12-31")]', "2026-06-15") is True
        assert (
            _result('[date("2026-01-01")..date("2026-12-31")]', "2027-01-01") is False
        )


class TestStringsAndBooleans:
    def test_strings_are_case_sensitive(self):
        assert _result('"Invoice"', "Invoice") is True
        assert _result('"Invoice"', "invoice") is False

    def test_a_string_literal_is_not_equal_to_a_number(self):
        assert _result('"5"', 5) is False
        assert _result('"5"', "5") is True

    def test_true_and_false(self):
        assert _result("true", True) is True
        assert _result("true", False) is False
        assert _result("false", False) is True
        assert _result("!= true", False) is True
        # The string "true" is a string, not a boolean.
        assert _result("true", "true") is False

    def test_booleans_have_no_order(self):
        assert _result("> false", True) is None


class TestDates:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-01-31",
            "2026-01-31T10:15:00",
            "2026-01-31T10:15:00Z",
            "2026-01-31T10:15:00+02:00",
            date(2026, 1, 31),
            datetime(2026, 1, 31, 23, 59),
        ],
    )
    def test_a_date_and_the_date_part_of_a_date_time(self, value):
        assert _result('date("2026-01-31")', value) is True
        assert _result('< date("2026-02-01")', value) is True
        assert _result('> date("2026-01-31")', value) is False

    def test_something_that_is_not_a_date_is_another_type(self):
        assert _result('date("2026-01-31")', "31 January 2026") is False
        assert _result('< date("2026-01-31")', "31 January 2026") is None
        assert _result('< date("2026-01-31")', 20260131) is None


class TestThreeValuedResults:
    @pytest.mark.parametrize("text", ["> 5", "<= 5", "[1..5]", '< "m"'])
    def test_an_ordering_test_on_null_is_unknown(self, text):
        assert _result(text, None) is None

    def test_equality_with_null_is_boolean(self):
        assert _result("= 5", None) is False
        assert _result("!= 5", None) is True
        assert _result('"a"', None) is False

    def test_null_equals_only_null(self):
        assert _result("null", None) is True
        assert _result("null", 0) is False
        assert _result("null", "") is False

    def test_not_of_unknown_is_unknown(self):
        assert _result("not(> 5)", None) is None
        assert _result("not(> 5, < 1)", None) is None

    def test_not_of_a_known_result_flips_it(self):
        assert _result("not(> 5)", 9) is False
        assert _result("not(> 5)", 2) is True
        assert _result("not(null)", None) is False
        assert _result("not(null)", 7) is True
        assert _result('not("a", "b")', "c") is True
        assert _result('not("a", "b")', "b") is False

    def test_a_list_is_true_when_any_test_is_true(self):
        assert _result('"a", "b"', "b") is True
        assert _result('"a", "b"', "c") is False
        # True wins over unknown; unknown wins over false.
        assert _result("> 5, null", None) is True
        assert _result("> 5, 3", None) is None
        assert _result("> 5, = 3", "x") is None

    def test_dash_matches_null_too(self):
        assert _result("-", None) is True
        assert _result("-", [1, 2]) is True


class TestSyntaxErrors:
    @pytest.mark.parametrize(
        ("text", "column"),
        [
            ("", 1),
            ("   ", 4),
            ("> ", 3),
            ("5 6", 3),
            ("5,", 3),
            ("not 5", 4),
            ("not(5", 6),
            ("not(5) 6", 8),
            ("[1..", 5),
            ("[1 5]", 4),
            ("[1..5", 6),
            ('[1.."a"]', 1),
            ("[true..false]", 1),
            ('"abc', 1),
            ('"a\\nb"', 3),
            ("= null", 3),
            ("nope", 1),
            ("date(2026-01-31)", 1),
            ('date("2026-02-30")', 1),
            ("1.", 2),
            ("<> 5", 2),
            ("- 5", 1),
            ("5, -", 4),
        ],
    )
    def test_each_error_reports_its_column(self, text, column):
        with pytest.raises(UnaryTestSyntaxError) as raised:
            parse(text)
        assert raised.value.column == column
        assert raised.value.text == text
        assert f"column {column}" in str(raised.value)

    def test_a_syntax_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            parse("not 5")
        with pytest.raises(DecisionTableError):
            parse("not 5")


class TestLiterals:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ('"Approved"', "Approved"),
            ("12", 12),
            ("-3", -3),
            ("1.50", Decimal("1.50")),
            ("true", True),
            ("false", False),
            ("null", None),
            ("", None),
            ('date("2026-01-31")', "2026-01-31"),
        ],
    )
    def test_parse_literal(self, text, value):
        parsed = parse_literal(text)
        assert parsed == value
        assert type(parsed) is type(value)

    def test_a_literal_is_one_value(self):
        with pytest.raises(UnaryTestSyntaxError):
            parse_literal('"a", "b"')
        with pytest.raises(UnaryTestSyntaxError):
            parse_literal("> 5")

    @pytest.mark.parametrize("value", ["queue", 'say "hi"', "a\\b", 7, True, None])
    def test_format_literal_round_trips(self, value):
        assert parse_literal(format_literal(value)) == value


class TestDmn13Form:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("= 5", "5"),
            ('= "fail"', '"fail"'),
            ('!= "fail"', 'not("fail")'),
            ("not(5)", "not(5)"),
            ("> 0", "> 0"),
            ("-", "-"),
            ("null", "null"),
            ('= "a", = "b"', '"a", "b"'),
            ("not(= 1, 2)", "not(1, 2)"),
            ("[1..5]", "[1..5]"),
            ("true", "true"),
        ],
    )
    def test_equality_is_bare_and_inequality_is_not(self, text, expected):
        rewritten = to_dmn13(text)
        assert rewritten == expected
        # The rewritten test means the same.
        assert parse(rewritten) == parse(text)

    def test_an_inequality_among_other_tests_cannot_be_written(self):
        with pytest.raises(DecisionTableError, match="DMN 1.3"):
            to_dmn13("!= 1, 5")
