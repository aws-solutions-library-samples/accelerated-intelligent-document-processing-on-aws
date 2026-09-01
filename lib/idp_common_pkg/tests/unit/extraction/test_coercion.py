# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deterministic type/format coercion of extraction output
(idp_common.extraction.coercion).

The tests are deliberately heavy on the *refusal* cases: the value of this module
is as much in what it declines to rewrite (ambiguous dates, fractional values in
integer fields, anything across a type family) as in what it fixes.
"""

import copy
import json
from typing import Any

import pytest

from idp_common.extraction.coercion import (
    CODE_AMBIGUOUS_DATE,
    CODE_BOOLEAN_FROM_NUMBER,
    CODE_BOOLEAN_FROM_STRING,
    CODE_BOOLEAN_TO_NUMBER,
    CODE_DATE_NORMALIZED,
    CODE_EMPTY_STRING_TO_NULL,
    CODE_FRACTIONAL_TO_INTEGER,
    CODE_INTEGER_FROM_STRING,
    CODE_NUMBER_FROM_STRING,
    CODE_STRING_FROM_BOOLEAN,
    CODE_STRING_FROM_NUMBER,
    CODE_TYPE_FAMILY_MISMATCH,
    CODE_UNPARSEABLE_BOOLEAN,
    CODE_UNPARSEABLE_DATE,
    CODE_UNPARSEABLE_NUMBER,
    coerce_extraction,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _one_field_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {"f": field_schema}}


def _coerce_one(
    field_schema: dict[str, Any], value: Any, **kwargs: Any
) -> tuple[Any, list, list]:
    """Coerce a single field ``f`` and return ``(value, coercions, refusals)``."""
    report = coerce_extraction({"f": value}, _one_field_schema(field_schema), **kwargs)
    return report.data["f"], report.coercions, report.refusals


NUMBER = {"type": "number"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
STRING = {"type": "string"}
DATE = {"type": "string", "format": "date"}
NULLABLE_NUMBER = {"type": ["number", "null"]}
NULLABLE_DATE_ANYOF = {
    "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]
}


# --------------------------------------------------------------------------- #
# numbers: currency, thousands separators, both decimal conventions, percent
# --------------------------------------------------------------------------- #


class TestNumberCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # currency symbols and codes
            ("$1,234.00", 1234.0),
            ("$ 1,234.00", 1234.0),
            ("-$5.00", -5.0),
            ("1234.00 USD", 1234.0),
            ("EUR 1.234,50", 1234.5),
            ("€1.234,50", 1234.5),
            # accounting negatives
            ("(1,234.00)", -1234.0),
            ("(42)", -42),
            # both decimal conventions, disambiguated by the rightmost separator
            ("1,234.56", 1234.56),
            ("1.234,56", 1234.56),
            ("1,234,567.89", 1234567.89),
            ("1.234.567,89", 1234567.89),
            # repeated separator => thousands grouping
            ("1,234,567", 1234567),
            ("1.234.567", 1234567),
            # space / apostrophe grouping
            ("1 234,56", 1234.56),
            ("1 234.56", 1234.56),
            ("1'234'567", 1234567),
            # single separator
            ("1,234", 1234),  # documented assumption: ',' + 3 digits = thousands
            ("1.234", 1.234),  # documented assumption: lone '.' is a decimal point
            ("1,5", 1.5),
            ("1,50", 1.5),
            ("12,34", 12.34),
            ("1,2345", 1.2345),
            # percent: sign removed, magnitude preserved (NOT divided by 100)
            ("12.5%", 12.5),
            ("-12.5%", -12.5),
            ("100%", 100),
            # plain
            ("42", 42),
            ("  42  ", 42),
            ("0.5", 0.5),
            ("+7", 7),
        ],
    )
    def test_numeric_strings_are_coerced(self, raw: str, expected: Any):
        value, coercions, refusals = _coerce_one(NUMBER, raw)
        assert value == expected
        assert refusals == []
        assert len(coercions) == 1
        assert coercions[0].path == "f"
        assert coercions[0].original == raw
        assert coercions[0].coerced == expected
        assert coercions[0].code in (
            CODE_NUMBER_FROM_STRING,
            CODE_INTEGER_FROM_STRING,
        )
        assert coercions[0].reason  # a reason is always recorded

    def test_percent_records_that_it_did_not_divide(self):
        _, coercions, _ = _coerce_one(NUMBER, "12.5%")
        assert "NOT divided by 100" in coercions[0].reason

    def test_thousands_assumption_is_recorded(self):
        _, coercions, _ = _coerce_one(NUMBER, "1,234")
        assert "thousands" in coercions[0].reason

    @pytest.mark.parametrize(
        "raw",
        [
            # A leading zero rules out the thousands reading -- no convention
            # writes 1 as "0,001" -- but the decimal reading is only an
            # assumption too, so the shape is ambiguous and must be refused.
            "0,001",
            "0,000",
            "00,001",
            "0,001.50",
            "$0,001",
            "(0,001)",
            "0.001,50",
            "0'001",
            "0 001",
        ],
    )
    def test_leading_zero_grouping_is_refused_not_silently_rescaled(self, raw: str):
        """Regression: '0,001' was rewritten to 1 -- a 1000x error.

        Worse than a wrong number, it was recorded as a *successful* coercion
        whose reason claimed the comma was a thousands separator, so nothing
        downstream (validation, confidence, the audit trail) had any signal that
        the stored value had been invented. Refusing leaves the raw string in
        place for the validator and a human to see.
        """
        value, coercions, refusals = _coerce_one(NUMBER, raw)
        assert value == raw, f"{raw!r} was rewritten to {value!r}"
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code == CODE_UNPARSEABLE_NUMBER

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # A single leading zero before a genuine decimal point is normal.
            ("0.001", 0.001),
            ("0.5", 0.5),
            ("-0.25", -0.25),
            # ... and a zero *inside* a group is fine; only the first group's
            # leading zero is diagnostic.
            ("1,001", 1001),
            ("10,001", 10001),
            ("1,000,001", 1000001),
        ],
    )
    def test_ordinary_zeros_still_coerce(self, raw: str, expected: Any):
        value, _, refusals = _coerce_one(NUMBER, raw)
        assert value == expected
        assert refusals == []

    def test_absurd_digit_run_is_refused_without_raising(self):
        """A barcode, a MICR line or a model repetition loop must not throw.

        Python refuses ``int()`` above 4300 digits (CVE-2020-10735) and a float
        that long overflows to ``inf``. Either would propagate out of the walk,
        and the caller disables coercion for the WHOLE section on any exception
        -- so one degenerate field would silently switch the feature off for
        every other field in the document.
        """
        value, coercions, refusals = _coerce_one(NUMBER, "1" * 5000)
        assert value == "1" * 5000
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code == CODE_UNPARSEABLE_NUMBER

    def test_overflowing_decimal_is_refused_rather_than_stored_as_infinity(self):
        """``float('1e400')`` is ``inf``, which is not JSON-serializable."""
        raw = "1" * 400 + ".5"
        value, coercions, refusals = _coerce_one(NUMBER, raw)
        assert value == raw
        assert coercions == []
        assert len(refusals) == 1

    @pytest.mark.parametrize(
        "raw",
        [
            "N/A",
            "none",
            "abc",
            "$",
            "1.23,456",  # '.' not in a valid 3-digit grouping position
            "1,23,456",  # Indian grouping is not 3-digit grouping
            "1 23 456",
            "5 apples",
            "1..2",
            "12.5 CR",  # credit/debit suffixes change the sign meaning
            "1-2",
        ],
    )
    def test_unparseable_numbers_are_refused(self, raw: str):
        value, coercions, refusals = _coerce_one(NUMBER, raw)
        assert value == raw  # survives untouched for the validator and a human
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code == CODE_UNPARSEABLE_NUMBER
        assert refusals[0].path == "f"


class TestIntegerCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1,234", 1234), ("$1,234.00", 1234), ("42", 42), ("(7)", -7)],
    )
    def test_integral_values_become_ints(self, raw: str, expected: int):
        value, coercions, refusals = _coerce_one(INTEGER, raw)
        assert value == expected
        assert isinstance(value, int)
        assert refusals == []
        assert coercions[0].code == CODE_INTEGER_FROM_STRING

    def test_fractional_string_is_refused_not_truncated(self):
        value, coercions, refusals = _coerce_one(INTEGER, "1,234.56")
        assert value == "1,234.56"
        assert coercions == []
        assert refusals[0].code == CODE_FRACTIONAL_TO_INTEGER

    def test_fractional_float_is_refused_not_truncated(self):
        value, coercions, refusals = _coerce_one(INTEGER, 12.5)
        assert value == 12.5
        assert coercions == []
        assert refusals[0].code == CODE_FRACTIONAL_TO_INTEGER

    def test_integral_float_is_already_valid_and_untouched(self):
        # JSON Schema: 1.0 IS an integer, so there is nothing to fix or report.
        value, coercions, refusals = _coerce_one(INTEGER, 12.0)
        assert value == 12.0
        assert coercions == []
        assert refusals == []

    def test_int_in_number_field_is_untouched(self):
        value, coercions, refusals = _coerce_one(NUMBER, 12)
        assert value == 12
        assert coercions == [] and refusals == []


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #


class TestDateCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # year-first is unambiguous
            ("2024/03/15", "2024-03-15"),
            ("2024.03.15", "2024-03-15"),
            # day > 12 forces D/M/Y
            ("15/03/2024", "2024-03-15"),
            ("15-03-2024", "2024-03-15"),
            # second field > 12 forces M/D/Y
            ("03/15/2024", "2024-03-15"),
            ("3/15/2024", "2024-03-15"),
            # equal fields: both readings give the same date
            ("03/03/2024", "2024-03-03"),
            # a named month removes the ordering question entirely
            ("March 15, 1980", "1980-03-15"),
            ("15 March 1980", "1980-03-15"),
            ("Mar. 15, 2024", "2024-03-15"),
            ("15th March 2024", "2024-03-15"),
            ("Sept 1, 2024", "2024-09-01"),
            ("1 Jan 2024", "2024-01-01"),
        ],
    )
    def test_unambiguous_dates_are_normalized_to_iso(self, raw: str, expected: str):
        value, coercions, refusals = _coerce_one(DATE, raw)
        assert value == expected
        assert refusals == []
        assert len(coercions) == 1
        assert coercions[0].code == CODE_DATE_NORMALIZED
        assert coercions[0].original == raw
        assert coercions[0].reason

    @pytest.mark.parametrize("raw", ["2024-03-15", "1980-01-01"])
    def test_iso_dates_are_untouched_with_no_report_entry(self, raw: str):
        value, coercions, refusals = _coerce_one(DATE, raw)
        assert value == raw
        assert coercions == [] and refusals == []

    @pytest.mark.parametrize("raw", ["01/02/2024", "1/2/2024", "12/11/2024"])
    def test_ambiguous_day_month_order_is_never_guessed(self, raw: str):
        value, coercions, refusals = _coerce_one(DATE, raw)
        assert value == raw  # the whole point: NOT rewritten
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code == CODE_AMBIGUOUS_DATE
        assert "ambiguous" in refusals[0].reason

    @pytest.mark.parametrize(
        ("raw", "order", "expected"),
        [
            ("01/02/2024", "MDY", "2024-01-02"),
            ("01/02/2024", "DMY", "2024-02-01"),
        ],
    )
    def test_explicit_date_order_hint_resolves_ambiguity(
        self, raw: str, order: str, expected: str
    ):
        value, coercions, refusals = _coerce_one(DATE, raw, date_order=order)
        assert value == expected
        assert refusals == []
        assert order in coercions[0].reason

    def test_date_order_hint_never_overrides_an_unambiguous_value(self):
        # 15 cannot be a month no matter what the caller claims.
        value, _, _ = _coerce_one(DATE, "15/03/2024", date_order="MDY")
        assert value == "2024-03-15"

    def test_unknown_date_order_falls_back_to_refusing(self):
        value, coercions, refusals = _coerce_one(
            DATE, "01/02/2024", date_order="nonsense"
        )
        assert value == "01/02/2024"
        assert coercions == []
        assert refusals[0].code == CODE_AMBIGUOUS_DATE

    @pytest.mark.parametrize(
        "raw",
        [
            "03/15/24",  # two-digit year: 1924 or 2024?
            "15/03/24",
            "2024-03-15T00:00:00Z",  # dropping a time component is data loss
            "March 2024",
            "1980",
            "sometime in March",
            "2024-02-31",  # not a real calendar date
            "31/02/2024",
            "13/13/2024",
            "00/10/2024",
        ],
    )
    def test_unusable_dates_are_refused(self, raw: str):
        value, coercions, refusals = _coerce_one(DATE, raw)
        assert value == raw
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code in (CODE_UNPARSEABLE_DATE, CODE_AMBIGUOUS_DATE)

    def test_two_digit_year_reason_mentions_the_century(self):
        _, _, refusals = _coerce_one(DATE, "03/15/24")
        assert "century" in refusals[0].reason

    def test_date_inside_nullable_anyof_is_still_normalized(self):
        value, coercions, _ = _coerce_one(NULLABLE_DATE_ANYOF, "March 15, 1980")
        assert value == "1980-03-15"
        assert coercions[0].code == CODE_DATE_NORMALIZED

    def test_non_date_formats_are_left_alone(self):
        # Coercion makes no claim about email/uuid/etc.: no rewrite, no noise.
        value, coercions, refusals = _coerce_one(
            {"type": "string", "format": "email"}, "not-an-email"
        )
        assert value == "not-an-email"
        assert coercions == [] and refusals == []


# --------------------------------------------------------------------------- #
# booleans
# --------------------------------------------------------------------------- #


class TestBooleanCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Yes", True),
            ("yes", True),
            ("Y", True),
            ("TRUE", True),
            ("true", True),
            ("checked", True),
            ("1", True),
            ("No", False),
            ("n", False),
            ("false", False),
            ("Unchecked", False),
            ("0", False),
            (" Yes ", True),
        ],
    )
    def test_boolean_ish_strings(self, raw: str, expected: bool):
        value, coercions, refusals = _coerce_one(BOOLEAN, raw)
        assert value is expected
        assert refusals == []
        assert coercions[0].code == CODE_BOOLEAN_FROM_STRING

    @pytest.mark.parametrize("raw", ["X", "maybe", "N/A", "2", "yes/no", "affirmative"])
    def test_non_boolean_strings_are_refused(self, raw: str):
        value, coercions, refusals = _coerce_one(BOOLEAN, raw)
        assert value == raw
        assert coercions == []
        assert refusals[0].code == CODE_UNPARSEABLE_BOOLEAN

    @pytest.mark.parametrize(("raw", "expected"), [(1, True), (0, False)])
    def test_one_and_zero_map_onto_booleans(self, raw: int, expected: bool):
        value, coercions, refusals = _coerce_one(BOOLEAN, raw)
        assert value is expected
        assert refusals == []
        assert coercions[0].code == CODE_BOOLEAN_FROM_NUMBER

    def test_other_numbers_are_not_booleans(self):
        value, _, refusals = _coerce_one(BOOLEAN, 7)
        assert value == 7
        assert refusals[0].code == CODE_UNPARSEABLE_BOOLEAN

    def test_actual_boolean_is_untouched(self):
        value, coercions, refusals = _coerce_one(BOOLEAN, True)
        assert value is True
        assert coercions == [] and refusals == []

    def test_boolean_in_numeric_field_is_refused(self):
        value, coercions, refusals = _coerce_one(NUMBER, True)
        assert value is True
        assert coercions == []
        assert refusals[0].code == CODE_BOOLEAN_TO_NUMBER


# --------------------------------------------------------------------------- #
# strings, empty strings and nulls
# --------------------------------------------------------------------------- #


class TestStringAndNullHandling:
    def test_number_in_string_field_becomes_a_string(self):
        value, coercions, refusals = _coerce_one(STRING, 1234)
        assert value == "1234"
        assert refusals == []
        assert coercions[0].code == CODE_STRING_FROM_NUMBER

    def test_boolean_in_string_field_becomes_json_style_text(self):
        value, coercions, _ = _coerce_one(STRING, False)
        assert value == "false"
        assert coercions[0].code == CODE_STRING_FROM_BOOLEAN

    @pytest.mark.parametrize("schema", [NUMBER, INTEGER, BOOLEAN, NULLABLE_NUMBER])
    def test_empty_string_becomes_null_in_non_string_fields(self, schema):
        value, coercions, refusals = _coerce_one(schema, "")
        assert value is None
        assert refusals == []
        assert coercions[0].code == CODE_EMPTY_STRING_TO_NULL

    def test_whitespace_only_string_becomes_null(self):
        value, coercions, _ = _coerce_one(NUMBER, "   ")
        assert value is None
        assert coercions[0].code == CODE_EMPTY_STRING_TO_NULL

    def test_empty_string_becomes_null_in_an_explicitly_nullable_string_field(self):
        value, coercions, _ = _coerce_one({"type": ["string", "null"]}, "")
        assert value is None
        assert coercions[0].code == CODE_EMPTY_STRING_TO_NULL

    def test_empty_string_in_a_plain_string_field_is_valid_and_untouched(self):
        value, coercions, refusals = _coerce_one(STRING, "")
        assert value == ""
        assert coercions == [] and refusals == []

    def test_null_is_left_alone_everywhere(self):
        # A null means "not present in the document"; flagging a required-but-null
        # field is the validator's job, not coercion's.
        for schema in (NUMBER, INTEGER, BOOLEAN, STRING, DATE):
            value, coercions, refusals = _coerce_one(schema, None)
            assert value is None
            assert coercions == [] and refusals == []

    def test_null_in_a_nullable_field_is_untouched(self):
        value, coercions, refusals = _coerce_one(NULLABLE_NUMBER, None)
        assert value is None
        assert coercions == [] and refusals == []

    def test_string_that_says_null_is_not_turned_into_null(self):
        value, coercions, refusals = _coerce_one(NUMBER, "null")
        assert value == "null"
        assert coercions == []
        assert refusals[0].code == CODE_UNPARSEABLE_NUMBER

    def test_untyped_field_is_never_touched(self):
        for raw in ("$1,234.00", "03/15/2024", "Yes", ""):
            value, coercions, refusals = _coerce_one({}, raw)
            assert value == raw
            assert coercions == [] and refusals == []


# --------------------------------------------------------------------------- #
# never coerce across a type family
# --------------------------------------------------------------------------- #


class TestTypeFamilyRefusals:
    @pytest.mark.parametrize(
        ("schema", "value"),
        [
            (STRING, {"a": 1}),  # object in a string field
            (STRING, [1, 2]),  # array in a string field
            (NUMBER, {"a": 1}),
            ({"type": "object", "properties": {}}, "some text"),
            ({"type": "array", "items": NUMBER}, "1, 2, 3"),  # no splitting
            ({"type": "array", "items": NUMBER}, 5),  # no auto-wrapping
            ({"type": "array", "items": NUMBER}, {"a": 1}),
            ({"type": "object", "properties": {}}, [1]),
        ],
    )
    def test_cross_family_values_are_refused_and_survive(self, schema, value):
        got, coercions, refusals = _coerce_one(schema, value)
        assert got == value
        assert coercions == []
        assert len(refusals) == 1
        assert refusals[0].code == CODE_TYPE_FAMILY_MISMATCH

    def test_a_string_is_never_split_into_a_list(self):
        report = coerce_extraction(
            {"items": "a, b, c"},
            {"type": "object", "properties": {"items": {"type": "array"}}},
        )
        assert report.data["items"] == "a, b, c"

    def test_no_list_element_is_ever_dropped_or_reordered(self):
        schema = {
            "type": "object",
            "properties": {"rows": {"type": "array", "items": NUMBER}},
        }
        data = {"rows": ["1,234.00", "not a number", "", 7, None, "2.5"]}
        report = coerce_extraction(data, schema)
        assert len(report.data["rows"]) == 6
        assert report.data["rows"] == [1234.0, "not a number", None, 7, None, 2.5]

    def test_refused_container_children_are_still_walked_safely(self):
        # An object where a string was declared: refused, but the subtree is
        # rebuilt (not shared) so the caller can never mutate the input.
        data = {"f": {"nested": {"deep": 1}}}
        report = coerce_extraction(data, _one_field_schema(STRING))
        assert report.data["f"] == data["f"]
        assert report.data["f"] is not data["f"]
        assert report.data["f"]["nested"] is not data["f"]["nested"]


# --------------------------------------------------------------------------- #
# recursion: nested objects, arrays of objects, $ref, additionalProperties
# --------------------------------------------------------------------------- #

NESTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$defs": {
        "LineItem": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "qty": {"type": "integer"},
                "shipped": {"type": "boolean"},
                "ship_date": {"type": "string", "format": "date"},
            },
        },
        "Address": {
            "type": "object",
            "properties": {
                "zip": {"type": "string"},
                "verified": {"type": "boolean"},
            },
        },
    },
    "properties": {
        "invoice_total": {"type": "number"},
        "issue_date": {"type": "string", "format": "date"},
        "billing": {"$ref": "#/$defs/Address"},
        "lines": {"type": "array", "items": {"$ref": "#/$defs/LineItem"}},
        "meta": {
            "type": "object",
            "properties": {"page_count": {"type": "integer"}},
        },
    },
}


class TestRecursion:
    def _nested_payload(self) -> dict[str, Any]:
        return {
            "invoice_total": "$2,500.00",
            "issue_date": "March 15, 2024",
            "billing": {"zip": 90210, "verified": "yes"},
            "lines": [
                {
                    "amount": "1.234,50",
                    "qty": "2",
                    "shipped": "No",
                    "ship_date": "15/03/2024",
                },
                {
                    "amount": "",
                    "qty": 1.5,
                    "shipped": True,
                    "ship_date": "01/02/2024",
                },
            ],
            "meta": {"page_count": "3"},
        }

    def test_nested_objects_arrays_and_refs_are_all_coerced(self):
        report = coerce_extraction(self._nested_payload(), NESTED_SCHEMA)
        data = report.data
        assert data["invoice_total"] == 2500.0
        assert data["issue_date"] == "2024-03-15"
        # $ref-resolved nested object
        assert data["billing"] == {"zip": "90210", "verified": True}
        # $ref-resolved array items
        assert data["lines"][0]["amount"] == 1234.5
        assert data["lines"][0]["qty"] == 2
        assert data["lines"][0]["shipped"] is False
        assert data["lines"][0]["ship_date"] == "2024-03-15"
        assert data["lines"][1]["amount"] is None
        assert data["meta"]["page_count"] == 3

    def test_paths_locate_every_change_precisely(self):
        report = coerce_extraction(self._nested_payload(), NESTED_SCHEMA)
        paths = {c.path for c in report.coercions}
        assert "invoice_total" in paths
        assert "issue_date" in paths
        assert "billing.zip" in paths
        assert "billing.verified" in paths
        assert "lines[0].amount" in paths
        assert "lines[0].ship_date" in paths
        assert "lines[1].amount" in paths
        assert "meta.page_count" in paths

    def test_nested_refusals_are_reported_with_their_paths(self):
        report = coerce_extraction(self._nested_payload(), NESTED_SCHEMA)
        refusals = {r.path: r for r in report.refusals}
        # 1.5 in an integer field, and an ambiguous date, both inside lines[1]
        assert refusals["lines[1].qty"].code == CODE_FRACTIONAL_TO_INTEGER
        assert refusals["lines[1].ship_date"].code == CODE_AMBIGUOUS_DATE
        assert report.data["lines"][1]["qty"] == 1.5
        assert report.data["lines"][1]["ship_date"] == "01/02/2024"

    def test_already_correct_nested_payload_is_untouched(self):
        data = {
            "invoice_total": 2500.0,
            "issue_date": "2024-03-15",
            "billing": {"zip": "90210", "verified": True},
            "lines": [
                {
                    "amount": 1234.5,
                    "qty": 2,
                    "shipped": False,
                    "ship_date": "2024-03-15",
                }
            ],
            "meta": {"page_count": 3},
        }
        report = coerce_extraction(copy.deepcopy(data), NESTED_SCHEMA)
        assert report.data == data
        assert report.coercions == []
        assert report.refusals == []
        assert report.changed is False

    def test_off_schema_keys_are_preserved_untouched(self):
        # Dropping off-schema keys is _filter_extracted_to_schema's job, not ours.
        report = coerce_extraction(
            {"unknown": "$1,234.00", "invoice_total": "42"}, NESTED_SCHEMA
        )
        assert report.data["unknown"] == "$1,234.00"
        assert report.data["invoice_total"] == 42

    def test_additional_properties_schema_is_applied(self):
        schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": {"type": "number"},
        }
        report = coerce_extraction({"a": "1,234", "b": "2.5"}, schema)
        assert report.data == {"a": 1234, "b": 2.5}

    def test_dangling_ref_degrades_to_no_coercion(self):
        schema = {
            "type": "object",
            "properties": {"f": {"$ref": "#/$defs/Missing"}},
        }
        report = coerce_extraction({"f": "$1,234.00"}, schema)
        assert report.data["f"] == "$1,234.00"
        assert report.coercions == []

    def test_tuple_form_items_leaves_the_array_unguided(self):
        schema = {
            "type": "object",
            "properties": {"f": {"type": "array", "items": [NUMBER, NUMBER]}},
        }
        report = coerce_extraction({"f": ["1,234", "2"]}, schema)
        assert report.data["f"] == ["1,234", "2"]
        assert report.coercions == []

    def test_multi_typed_anyof_union_is_left_alone(self):
        # Two non-null branches is a genuine choice of target; guessing one would
        # be exactly the kind of silent decision this module refuses to make.
        schema = {"anyOf": [{"type": "number"}, {"type": "boolean"}]}
        value, coercions, refusals = _coerce_one(schema, "1,234")
        assert value == "1,234"
        assert coercions == [] and refusals == []


# --------------------------------------------------------------------------- #
# immutability of the input
# --------------------------------------------------------------------------- #


class TestInputIsNeverMutated:
    def test_input_dict_is_unchanged(self):
        data = {
            "invoice_total": "$2,500.00",
            "billing": {"zip": 90210, "verified": "yes"},
            "lines": [{"amount": "1.234,50", "qty": "2"}],
        }
        snapshot = copy.deepcopy(data)
        coerce_extraction(data, NESTED_SCHEMA)
        assert data == snapshot

    def test_returned_containers_are_fresh_objects(self):
        data = {"billing": {"zip": "90210"}, "lines": [{"amount": "1"}]}
        report = coerce_extraction(data, NESTED_SCHEMA)
        assert report.data is not data
        assert report.data["billing"] is not data["billing"]
        assert report.data["lines"] is not data["lines"]
        assert report.data["lines"][0] is not data["lines"][0]

    def test_mutating_the_result_cannot_reach_back_into_the_input(self):
        data = {"lines": [{"amount": "1", "note": {"deep": [1, 2]}}]}
        snapshot = copy.deepcopy(data)
        report = coerce_extraction(data, NESTED_SCHEMA)
        report.data["lines"][0]["note"]["deep"].append(3)
        report.data["lines"].append({"amount": 9})
        assert data == snapshot

    def test_schema_is_not_mutated(self):
        schema_snapshot = copy.deepcopy(NESTED_SCHEMA)
        coerce_extraction({"invoice_total": "$1.00"}, NESTED_SCHEMA)
        assert NESTED_SCHEMA == schema_snapshot


# --------------------------------------------------------------------------- #
# report surface
# --------------------------------------------------------------------------- #


class TestReport:
    def test_metadata_is_json_serializable_and_complete(self):
        report = coerce_extraction(
            {"invoice_total": "$1,234.00", "issue_date": "01/02/2024"},
            NESTED_SCHEMA,
        )
        meta = report.to_metadata()
        json.dumps(meta)  # must not raise
        assert meta["coerced"] is True
        assert meta["coercion_count"] == 1
        assert meta["refusal_count"] == 1
        entry = meta["coercions"][0]
        assert entry["path"] == "invoice_total"
        assert entry["from"] == "$1,234.00"
        assert entry["to"] == 1234.0
        assert entry["code"] == CODE_NUMBER_FROM_STRING
        assert entry["reason"]
        assert meta["refusals"][0]["path"] == "issue_date"

    def test_metadata_entries_are_capped_but_counts_are_not(self):
        schema = {
            "type": "object",
            "properties": {"rows": {"type": "array", "items": NUMBER}},
        }
        report = coerce_extraction({"rows": ["1,234"] * 120}, schema)
        meta = report.to_metadata()
        assert meta["coercion_count"] == 120
        assert len(meta["coercions"]) == 50
        assert len(report.coercions) == 120

    def test_metadata_summarizes_container_values(self):
        report = coerce_extraction({"f": {"a": 1, "b": 2}}, _one_field_schema(STRING))
        meta = report.to_metadata()
        assert meta["refusals"][0]["value"] == "<object with 2 key(s)>"
        json.dumps(meta)

    def test_metadata_truncates_a_very_long_string(self):
        long_value = "x" * 500
        report = coerce_extraction({"f": long_value}, _one_field_schema(NUMBER))
        assert report.to_metadata()["refusals"][0]["value"].endswith("...")

    def test_no_change_report_is_empty(self):
        report = coerce_extraction({"invoice_total": 1.0}, NESTED_SCHEMA)
        assert report.changed is False
        meta = report.to_metadata()
        assert meta == {
            "coerced": False,
            "coercion_count": 0,
            "refusal_count": 0,
            "coercions": [],
            "refusals": [],
        }
        assert "No type/format coercion needed" in report.summary_line()

    def test_entries_render_readably_for_logs(self):
        report = coerce_extraction(
            {"invoice_total": "$1,234.00", "issue_date": "01/02/2024"},
            NESTED_SCHEMA,
        )
        assert "invoice_total" in str(report.coercions[0])
        assert "->" in str(report.coercions[0])
        assert "issue_date" in str(report.refusals[0])
        assert "Coerced 1 value(s)" in report.summary_line()


# --------------------------------------------------------------------------- #
# fail-open behaviour
# --------------------------------------------------------------------------- #


class TestFailOpen:
    @pytest.mark.parametrize("data", [None, [], "text", 5])
    def test_non_object_data_is_returned_unchanged(self, data):
        report = coerce_extraction(data, NESTED_SCHEMA)
        assert report.data == data
        assert report.coercions == [] and report.refusals == []

    @pytest.mark.parametrize("schema", [None, [], "text"])
    def test_non_object_schema_is_a_no_op(self, schema):
        data = {"invoice_total": "$1,234.00"}
        report = coerce_extraction(data, schema)
        assert report.data == data
        assert report.data is not data
        assert report.coercions == []

    def test_empty_schema_coerces_nothing(self):
        report = coerce_extraction({"f": "$1,234.00"}, {})
        assert report.data == {"f": "$1,234.00"}
        assert report.coercions == []

    def test_deeply_nested_data_is_bounded_and_recorded(self):
        # Far deeper than any real extraction result; must not blow the stack and
        # must say what it skipped rather than dropping it.
        payload: Any = "1,234"
        for _ in range(80):
            payload = {"nested": payload}
        report = coerce_extraction({"f": payload}, _one_field_schema(STRING))
        assert report.data["f"] is not None
        assert any(r.code == "max_depth_exceeded" for r in report.refusals)
