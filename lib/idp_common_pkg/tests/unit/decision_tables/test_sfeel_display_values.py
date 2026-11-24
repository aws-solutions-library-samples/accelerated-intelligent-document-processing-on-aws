# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``sfeel.display_values``: the values a condition compares with, as shown to a reader."""

import pytest

from idp_common.decision_tables.models import UnaryTestSyntaxError
from idp_common.decision_tables.sfeel import display_values

pytestmark = pytest.mark.unit


class TestDisplayValues:
    @pytest.mark.parametrize(
        ("text", "values"),
        [
            pytest.param(">= 0.8", ["0.8"], id="comparison"),
            pytest.param("< 3", ["3"], id="comparison-below"),
            pytest.param("5", ["5"], id="bare-number"),
            pytest.param("0.80", ["0.80"], id="number-as-written"),
            pytest.param("-2.5", ["-2.5"], id="negative-number"),
            pytest.param('"Invoice"', ["Invoice"], id="one-string"),
            pytest.param(
                '"Invoice", "Credit Note"',
                ["Invoice", "Credit Note"],
                id="list-of-strings",
            ),
            pytest.param('!= "W2"', ["W2"], id="not-equal"),
            pytest.param("[1..10]", ["1..10"], id="interval"),
            pytest.param("(0.5..1)", ["0.5..1"], id="open-interval"),
            pytest.param("]1..10[", ["1..10"], id="interval-reversed-brackets"),
            pytest.param('["a".."m"]', ["a..m"], id="interval-of-strings"),
            pytest.param("null", ["null"], id="null-test"),
            pytest.param("-", [], id="any-value"),
            pytest.param(" - ", [], id="any-value-with-spaces"),
            pytest.param('not("a", "b")', ["a", "b"], id="negated-list"),
            pytest.param("not(5)", ["5"], id="negated-single-value"),
            pytest.param("not([1..3], null)", ["1..3", "null"], id="negated-mixed"),
            pytest.param("true", ["true"], id="boolean"),
            pytest.param(
                'date("2024-02-29")', ['date("2024-02-29")'], id="date-as-written"
            ),
            pytest.param("< 5, null, [8..9]", ["5", "null", "8..9"], id="mixed-list"),
        ],
    )
    def test_values_of_the_tests_in_order(self, text, values):
        assert display_values(text) == values

    def test_a_string_is_shown_as_its_value_not_as_written(self):
        # The escapes of the literal are read, and the quotes are dropped.
        assert display_values(r'"say \"hi\"", "a\\b"') == ['say "hi"', "a\\b"]

    @pytest.mark.parametrize("text", ["", ">=", '"open', "1 2"])
    def test_text_that_does_not_parse_raises(self, text):
        with pytest.raises(UnaryTestSyntaxError):
            display_values(text)

    def test_the_result_is_a_new_list_each_call(self):
        # ``parse`` is cached; a caller that changes its list must not change the
        # next caller's.
        first = display_values('"a", "b"')
        first.append("c")

        assert display_values('"a", "b"') == ["a", "b"]
