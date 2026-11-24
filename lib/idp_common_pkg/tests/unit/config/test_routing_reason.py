# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reason of a routing rule: the placeholder grammar and the rendered text."""

import ast
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from idp_common.config import routing_reason
from idp_common.config.routing_reason import (
    ABSENT,
    CONFIDENCE_THRESHOLD,
    ELLIPSIS,
    KIND_CONDITION_VALUE,
    KIND_INPUT,
    MAX_LIST_ITEMS,
    MAX_RENDERED_REASON_BYTES,
    MAX_VALUE_BYTES,
    SOURCE_ADMIN,
    SOURCE_FALLBACK,
    Placeholder,
    ReasonSyntaxError,
    cut_bytes,
    format_value,
    parse_reason,
    render_reason,
)

pytestmark = pytest.mark.unit


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


class TestGrammar:
    @pytest.mark.parametrize(
        ("text", "placeholders"),
        [
            ("", []),
            ("No placeholder at all.", []),
            (
                "{page_count} pages",
                [Placeholder("{page_count}", KIND_INPUT, input="page_count")],
            ),
            (
                "Total {field_value:Invoice:Total}",
                [
                    Placeholder(
                        "{field_value:Invoice:Total}",
                        KIND_INPUT,
                        input="field_value",
                        path="Invoice:Total",
                    )
                ],
            ),
            (
                "{policy_rule_verdict:invoice_controls.total_matches_lines}",
                [
                    Placeholder(
                        "{policy_rule_verdict:invoice_controls.total_matches_lines}",
                        KIND_INPUT,
                        input="policy_rule_verdict",
                        path="invoice_controls.total_matches_lines",
                    )
                ],
            ),
            (
                "over {condition_1.value}",
                [Placeholder("{condition_1.value}", KIND_CONDITION_VALUE, index=1)],
            ),
            (
                "{condition_99.value}",
                [Placeholder("{condition_99.value}", KIND_CONDITION_VALUE, index=99)],
            ),
            (
                "under {confidence_threshold}",
                [Placeholder("{confidence_threshold}", CONFIDENCE_THRESHOLD)],
            ),
            # Spaces around a token and around its parts are ignored; the text
            # keeps what was written.
            (
                "{ page_count }",
                [Placeholder("{ page_count }", KIND_INPUT, input="page_count")],
            ),
            (
                "{ field_value : Invoice:Lines[0].Amount }",
                [
                    Placeholder(
                        "{ field_value : Invoice:Lines[0].Amount }",
                        KIND_INPUT,
                        input="field_value",
                        path="Invoice:Lines[0].Amount",
                    )
                ],
            ),
            (
                "{ confidence_threshold }",
                [Placeholder("{ confidence_threshold }", CONFIDENCE_THRESHOLD)],
            ),
            # An escaped brace is text, not a placeholder.
            ("{{page_count}}", []),
            ("a {{ b }} c", []),
            (
                "{{{page_count}}}",
                [Placeholder("{page_count}", KIND_INPUT, input="page_count")],
            ),
            # In the order written, and a repeated one is listed twice.
            (
                "{section_count} of {page_count}, {section_count}",
                [
                    Placeholder("{section_count}", KIND_INPUT, input="section_count"),
                    Placeholder("{page_count}", KIND_INPUT, input="page_count"),
                    Placeholder("{section_count}", KIND_INPUT, input="section_count"),
                ],
            ),
        ],
    )
    def test_valid_texts(self, text, placeholders):
        assert parse_reason(text) == placeholders

    @pytest.mark.parametrize(
        "token",
        [
            "condition_0.value",
            "condition_100.value",
            "condition_1",
            "condition_x.value",
        ],
    )
    def test_a_token_that_is_not_a_condition_value_is_read_as_an_input(self, token):
        """The grammar accepts it; the save refuses it as an unknown input."""
        (placeholder,) = parse_reason("{" + token + "}")

        assert (placeholder.kind, placeholder.input) == (KIND_INPUT, token)

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (
                "Total is {field_value",
                "an unmatched '{' at character 10; write '{{' for a literal brace",
            ),
            (
                "Total is 5}",
                "an unmatched '}' at character 11; write '}}' for a literal brace",
            ),
            (
                "{a{b}",
                "an unmatched '{' at character 1; write '{{' for a literal brace",
            ),
            (
                "{page_count}}",
                "an unmatched '}' at character 13; write '}}' for a literal brace",
            ),
            (
                "Empty {} here",
                "an empty placeholder at character 7; a placeholder names a routing "
                "input, for example {page_count}",
            ),
            (
                "Blank {   } here",
                "an empty placeholder at character 7; a placeholder names a routing "
                "input, for example {page_count}",
            ),
            (
                "{field_value:}",
                "the placeholder '{field_value:}' at character 1 has no path after ':'",
            ),
            (
                "x {field_value:  }",
                "the placeholder '{field_value:  }' at character 3 has no path "
                "after ':'",
            ),
            (
                "{:Invoice:Total}",
                "the placeholder '{:Invoice:Total}' at character 1 names no routing "
                "input before ':'",
            ),
        ],
    )
    def test_each_error_says_what_and_where(self, text, message):
        with pytest.raises(ReasonSyntaxError) as raised:
            parse_reason(text)

        assert str(raised.value) == message
        # Rendering refuses the same text with the same message.
        with pytest.raises(ReasonSyntaxError) as rendered:
            render_reason(text, lambda placeholder: "x")
        assert str(rendered.value) == message

    def test_a_syntax_error_is_a_value_error(self):
        assert issubclass(ReasonSyntaxError, ValueError)

    def test_the_sources_and_kinds(self):
        assert (SOURCE_ADMIN, SOURCE_FALLBACK) == ("admin", "fallback")
        assert (KIND_INPUT, KIND_CONDITION_VALUE, CONFIDENCE_THRESHOLD) == (
            "input",
            "condition_value",
            "confidence_threshold",
        )


class TestRendering:
    def test_placeholders_take_the_resolved_values(self):
        values = {
            "{page_count}": 12,
            "{field_value:Invoice:Total}": [1250.0],
            "{condition_1.value}": ["1000"],
            "{confidence_threshold}": 0.8,
        }
        asked = []

        def resolve(placeholder):
            asked.append(placeholder)
            return values[placeholder.text]

        text = render_reason(
            "{page_count} pages; total {field_value:Invoice:Total} is over "
            "{condition_1.value}; threshold {confidence_threshold}.",
            resolve,
        )

        assert text == "12 pages; total 1250 is over 1000; threshold 0.8."
        assert asked == [
            Placeholder("{page_count}", KIND_INPUT, input="page_count"),
            Placeholder(
                "{field_value:Invoice:Total}",
                KIND_INPUT,
                input="field_value",
                path="Invoice:Total",
            ),
            Placeholder("{condition_1.value}", KIND_CONDITION_VALUE, index=1),
            Placeholder("{confidence_threshold}", CONFIDENCE_THRESHOLD),
        ]

    def test_doubled_braces_are_literal_braces(self):
        text = render_reason(
            "Write {{page_count}} to show {page_count}; {{{page_count}}} too.",
            lambda placeholder: 3,
        )

        assert text == "Write {page_count} to show 3; {3} too."

    def test_a_text_with_no_placeholder_is_itself(self):
        def never(placeholder):
            raise AssertionError("nothing to resolve")

        assert render_reason("Check the totals.", never) == "Check the totals."
        assert render_reason("", never) == ""

    def test_a_value_is_not_scanned_again(self):
        """One pass: braces a value holds are text, not a placeholder or an escape."""
        values = {
            "{document_class}": "{page_count}",
            "{field_value:Vendor}": "a {{b}} c }",
            "{page_count}": 7,
        }

        text = render_reason(
            "{document_class} / {field_value:Vendor} / {page_count}",
            lambda placeholder: values[placeholder.text],
        )

        assert text == "{page_count} / a {{b}} c } / 7"

    def test_what_resolve_raises_is_raised(self):
        def resolve(placeholder):
            raise KeyError(placeholder.input)

        with pytest.raises(KeyError):
            render_reason("{page_count}", resolve)

    @pytest.mark.parametrize("value", [None, "", [], ()])
    def test_an_absent_value(self, value):
        assert format_value(value) == ABSENT == "(no value)"
        assert render_reason("Vendor: {field_value:Vendor}.", lambda p: value) == (
            "Vendor: (no value)."
        )

    def test_a_boolean_reads_yes_or_no(self):
        assert format_value(True) == "yes"
        assert format_value(False) == "no"

    def test_zero_is_a_value(self):
        assert format_value(0) == "0"
        assert format_value(0.0) == "0"

    def test_a_list_is_joined(self):
        assert format_value(["Invoice", "W2"]) == "Invoice, W2"
        assert format_value([1250.0, True, None]) == "1250, yes, (no value)"

    def test_a_long_list_shows_its_first_items_and_how_many_more(self):
        at_cap = [f"c{i}" for i in range(MAX_LIST_ITEMS)]
        assert format_value(at_cap) == "c0, c1, c2, c3, c4"

        assert format_value(at_cap + ["c5"]) == "c0, c1, c2, c3, c4 and 1 more"
        assert format_value(at_cap + ["c5", "c6", "c7"]) == (
            "c0, c1, c2, c3, c4 and 3 more"
        )

    def test_a_value_is_cut_on_a_character_boundary(self):
        # Two bytes each, so a cut at an odd byte would split a character.
        value = "é" * MAX_VALUE_BYTES

        shown = format_value(value)

        assert shown.endswith(ELLIPSIS)
        assert _size(shown) <= MAX_VALUE_BYTES
        kept = shown[: -len(ELLIPSIS)]
        assert kept == "é" * len(kept)
        # As much as fits: one more character would not.
        assert _size(kept + "é" + ELLIPSIS) > MAX_VALUE_BYTES
        # A value at the limit is not cut.
        fits = "é" * (MAX_VALUE_BYTES // 2)
        assert format_value(fits) == fits

    def test_each_value_is_cut_on_its_own(self):
        text = render_reason("{a} | {b}", lambda placeholder: "x" * 500)

        first, second = text.split(" | ")
        assert first == second == "x" * (MAX_VALUE_BYTES - _size(ELLIPSIS)) + ELLIPSIS

    def test_the_whole_text_is_cut(self):
        template = " ".join("{" + f"input_{i}" + "}" for i in range(10))

        text = render_reason(template, lambda placeholder: "é" * 90)

        assert _size(text) <= MAX_RENDERED_REASON_BYTES
        assert text.endswith(ELLIPSIS)
        assert text.startswith("é" * 90 + " " + "é" * 90)
        # Still text: the cut did not split a character.
        assert text.encode("utf-8").decode("utf-8") == text

    def test_a_text_at_the_limit_is_not_cut(self):
        text = "x" * MAX_RENDERED_REASON_BYTES

        assert render_reason(text, lambda placeholder: "") == text


class TestFormatValue:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (5, "5"),
            (-3, "-3"),
            (1250.0, "1250"),
            (-2.0, "-2"),
            (12.5, "12.5"),
            (0.8, "0.8"),
            (Decimal("1250"), "1250"),
            (Decimal("1250.00"), "1250"),
            (Decimal("12.50"), "12.5"),
            (Decimal("1E+3"), "1000"),
            (Decimal("0.00"), "0"),
            (Decimal("0.000001"), "0.000001"),
            ("as is", "as is"),
            ({"b": 1, "a": "é"}, '{"b":1,"a":"é"}'),
        ],
    )
    def test_values(self, value, text):
        assert format_value(value) == text

    def test_a_value_json_cannot_write_is_still_text(self):
        class Odd:
            def __str__(self):
                return "odd"

        assert format_value(Odd()) == '"odd"'
        assert format_value({(1, 2): "tuple key"}) == "{(1, 2): 'tuple key'}"
        assert format_value(float("inf")) == "inf"
        assert format_value(Decimal("NaN")) == "NaN"


class TestCutBytes:
    def test_a_text_that_fits_is_itself(self):
        assert cut_bytes("abc", 3) == "abc"
        assert cut_bytes("", 0) == ""

    def test_a_longer_text_ends_with_the_ellipsis_inside_the_limit(self):
        assert cut_bytes("abcdefghij", 8) == "abcde" + ELLIPSIS
        assert _size(cut_bytes("abcdefghij", 8)) == 8

    def test_a_cut_never_splits_a_character(self):
        # "日" is three bytes: seven bytes of room hold two of them, not 2⅓.
        assert cut_bytes("日" * 10, 10) == "日日" + ELLIPSIS

    def test_a_limit_too_small_for_the_ellipsis(self):
        assert cut_bytes("abcdef", 2) == "ab"


def test_the_module_imports_the_standard_library_only():
    """The save validation and the routing Lambda both load it."""
    tree = ast.parse(Path(routing_reason.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import"
            imported.add((node.module or "").split(".")[0])

    assert imported
    assert imported <= set(sys.stdlib_module_names), imported
