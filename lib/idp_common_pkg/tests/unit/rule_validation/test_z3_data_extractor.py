# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `DataExtractor`, the path-based half of Z3 rule validation.

This is the deterministic extraction route: a rule's `path_mappings` name dot-notation
paths into the pipeline's structured extraction results, and this class walks them and
coerces each value to its declared type. It is the route that runs whenever a rule has
path mappings, and the route whose failure hands the same document to the LLM extractor
instead.

Two things make it worth testing closely.

**Its type coercion is the strict one.** `_convert_type` refuses a float that would
lose information when the parameter is declared `Int` — `Float value 30.9 cannot be
converted to Int without loss`. The LLM route performs no type checking at all and
truncates the same value to `30`, which flips a threshold rule's verdict
([#1057](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1057)).
So the behaviour asserted here is the reference the other route is wrong against, and a
fix for #1057 should make the two agree. Each refusal is asserted individually rather
than sampled.

**Silence is the dangerous outcome.** An out-of-range list index resolves to `None`,
the same as a missing key — a deliberate choice, but it means a path typo and a genuinely
absent value are indistinguishable at this layer. What separates them downstream is the
`required` flag: a required parameter resolving to `None` raises, an optional one records
`None`. Both are asserted, because collapsing them would either fail a rule that was
meant to tolerate a gap or pass one that was not.

Nothing here touches AWS; the extractor is pure over a dict.
"""

from __future__ import annotations

from typing import Any

import pytest

from idp_common.rule_validation.z3.data_extractor import DataExtractor
from idp_common.rule_validation.z3.exceptions import ExtractionError
from idp_common.rule_validation.z3.models import Parameter, PathMapping, RuleJSON


def _rule(
    parameters: list[Parameter],
    mappings: list[tuple[str, str]],
    *,
    rule_id: str = "r1",
) -> RuleJSON:
    return RuleJSON(
        rule_id=rule_id,
        version="1.0",
        description="d",
        natural_language_rule="nl",
        parameters=parameters,
        constraints=["(> " + parameters[0].name + " 0)"],
        path_mappings=[
            PathMapping(parameter_name=name, data_path=path) for name, path in mappings
        ],
    )


def _extract(
    parameters: list[Parameter], mappings: list[tuple[str, str]], data: dict[str, Any]
) -> dict[str, Any]:
    return DataExtractor().extract_values(_rule(parameters, mappings), data)


@pytest.mark.unit
class TestExtractPath:
    """_extract_path: dot-notation traversal, list subscripts, and misses."""

    def _path(self, data: dict[str, Any], path: str) -> Any:
        return DataExtractor()._extract_path(data, path, "r1")

    def test_a_single_key_resolves(self):
        assert self._path({"amount": 5100}, "amount") == 5100

    def test_a_nested_path_resolves(self):
        data = {"documents": {"tax_bill": {"inference_result": {"amount": "5100"}}}}
        assert self._path(data, "documents.tax_bill.inference_result.amount") == "5100"

    def test_a_missing_key_resolves_to_none(self):
        # None rather than raising: the caller decides whether a miss is fatal, using
        # the parameter's `required` flag.
        assert self._path({"a": {"b": 1}}, "a.missing") is None

    def test_a_missing_intermediate_key_resolves_to_none(self):
        assert self._path({"a": {"b": 1}}, "a.nope.deeper") is None

    def test_a_list_subscript_selects_an_element(self):
        # Multi-instance extraction results are {"instances": [...]}, so addressing an
        # element is how a rule targets one instance.
        data = {"documents": {"pay": {"instances": [{"NetPay": 100}, {"NetPay": 200}]}}}
        assert self._path(data, "documents.pay.instances[1].NetPay") == 200

    def test_a_negative_subscript_counts_from_the_end(self):
        data = {"rows": [{"v": 1}, {"v": 2}, {"v": 3}]}
        assert self._path(data, "rows[-1].v") == 3

    def test_chained_subscripts_resolve(self):
        assert self._path({"grid": [[10, 20], [30, 40]]}, "grid[1][0]") == 30

    def test_an_out_of_range_subscript_resolves_to_none(self):
        # Same outcome as a missing key, which is why a path typo and an absent value
        # are indistinguishable here and are separated by `required` downstream.
        assert self._path({"rows": [{"v": 1}]}, "rows[5].v") is None

    def test_a_subscript_on_a_non_list_resolves_to_none(self):
        assert self._path({"a": {"b": 1}}, "a[0]") is None

    def test_a_key_lookup_into_a_scalar_resolves_to_none(self):
        assert self._path({"a": 5}, "a.b") is None

    def test_a_value_of_none_in_the_data_resolves_to_none(self):
        assert self._path({"a": {"b": None}}, "a.b") is None

    def test_a_falsy_value_is_returned_rather_than_treated_as_missing(self):
        # 0, "" and False are readings, not absences. Conflating them with a miss
        # would make "the balance is zero" unevaluable.
        assert self._path({"a": 0}, "a") == 0
        assert self._path({"a": ""}, "a") == ""
        assert self._path({"a": False}, "a") is False


@pytest.mark.unit
class TestConvertTypeInt:
    """_convert_type for Int — the strict route, and the reference for #1057."""

    def _convert(self, value: Any) -> Any:
        return DataExtractor()._convert_type(value, "Int", "r1", "n", "a.b")

    def test_an_int_passes_through(self):
        assert self._convert(42) == 42

    def test_a_whole_float_converts(self):
        # 42.0 loses nothing, so it is accepted.
        assert self._convert(42.0) == 42

    def test_a_fractional_float_is_refused_rather_than_truncated(self):
        # This is the behaviour #1057 says the LLM route should share. Truncating
        # 30.9 to 30 makes `days_late <= 30` report PASS when the truth is FAIL.
        with pytest.raises(ExtractionError, match="without loss"):
            self._convert(30.9)

    def test_a_numeric_string_converts(self):
        assert self._convert("42") == 42

    def test_surrounding_whitespace_is_stripped(self):
        assert self._convert("  42  ") == 42

    def test_an_empty_string_is_refused(self):
        with pytest.raises(ExtractionError, match="Empty string"):
            self._convert("   ")

    def test_a_non_numeric_string_is_refused(self):
        with pytest.raises(ExtractionError):
            self._convert("forty-two")

    def test_a_decimal_string_is_refused(self):
        # int("30.9") raises, so a decimal arriving as text is refused too — the
        # string and float paths agree.
        with pytest.raises(ExtractionError):
            self._convert("30.9")

    def test_a_list_is_refused(self):
        with pytest.raises(ExtractionError, match="Cannot convert"):
            self._convert([1])

    def test_the_error_carries_the_rule_parameter_and_path(self):
        # An extraction failure is reported per rule, and these three fields are what
        # let an operator find the mapping that is wrong.
        with pytest.raises(ExtractionError) as excinfo:
            self._convert("nope")
        assert excinfo.value.rule_id == "r1"
        assert excinfo.value.parameter_name == "n"
        assert excinfo.value.data_path == "a.b"


@pytest.mark.unit
class TestConvertTypeReal:
    """_convert_type for Real."""

    def _convert(self, value: Any) -> Any:
        return DataExtractor()._convert_type(value, "Real", "r1", "r", "a.b")

    def test_a_float_passes_through(self):
        assert self._convert(2.5) == 2.5

    def test_an_int_widens_to_a_float(self):
        result = self._convert(5)
        assert result == 5.0
        assert isinstance(result, float)

    def test_a_decimal_string_converts(self):
        assert self._convert("30.9") == 30.9

    def test_an_integer_string_converts(self):
        assert self._convert("5") == 5.0

    def test_surrounding_whitespace_is_stripped(self):
        assert self._convert("  2.5 ") == 2.5

    def test_an_empty_string_is_refused(self):
        with pytest.raises(ExtractionError, match="Empty string"):
            self._convert("  ")

    def test_a_non_numeric_string_is_refused(self):
        with pytest.raises(ExtractionError):
            self._convert("two point five")

    def test_a_dict_is_refused(self):
        with pytest.raises(ExtractionError, match="Cannot convert"):
            self._convert({"a": 1})

    def test_no_precision_is_lost_on_a_fractional_value(self):
        # Real is the type a ratio rule uses, so rounding here would change a verdict
        # near the threshold.
        assert self._convert("0.333333333") == pytest.approx(0.333333333)


@pytest.mark.unit
class TestConvertTypeBool:
    """_convert_type for Bool — the documented spellings and nothing else."""

    def _convert(self, value: Any) -> Any:
        return DataExtractor()._convert_type(value, "Bool", "r1", "b", "a.b")

    @pytest.mark.parametrize("value", [True, False])
    def test_a_bool_passes_through(self, value):
        assert self._convert(value) is value

    @pytest.mark.parametrize("text", ["yes", "YES", " Yes ", "true", "TRUE", "1"])
    def test_the_truthy_spellings(self, text):
        assert self._convert(text) is True

    @pytest.mark.parametrize("text", ["no", "NO", " No ", "false", "FALSE", "0"])
    def test_the_falsy_spellings(self, text):
        assert self._convert(text) is False

    def test_an_unrecognised_string_is_refused_rather_than_guessed(self):
        # Guessing would turn an unparseable answer into a definite verdict. The
        # message names the accepted spellings so an operator can fix the document
        # or the mapping.
        with pytest.raises(ExtractionError) as excinfo:
            self._convert("maybe")
        assert "yes/no" in str(excinfo.value)

    def test_an_empty_string_is_refused(self):
        with pytest.raises(ExtractionError):
            self._convert("")

    @pytest.mark.parametrize("value,expected", [(1, True), (0, False), (7, True)])
    def test_an_int_is_taken_as_truthiness(self, value, expected):
        assert self._convert(value) is expected


@pytest.mark.unit
class TestConvertTypeStringAndUnknown:
    """_convert_type for String, and an unsupported declared type."""

    def _convert(self, value: Any, declared: str = "String") -> Any:
        return DataExtractor()._convert_type(value, declared, "r1", "s", "a.b")

    def test_a_string_passes_through_unchanged(self):
        assert self._convert("CA") == "CA"

    def test_internal_whitespace_is_preserved(self):
        # The String type is used for identity comparisons like a state code or a
        # policy number, so normalising it would change what a rule matches.
        assert self._convert("  John  Doe  ") == "  John  Doe  "

    def test_none_returns_none_rather_than_the_text_none(self):
        # The caller handles None before reaching here; this is the defensive path,
        # and stringifying it would produce the literal "None" as a reading.
        assert self._convert(None) is None

    def test_an_unsupported_declared_type_is_refused(self):
        with pytest.raises(ExtractionError):
            self._convert("x", declared="Decimal")


@pytest.mark.unit
class TestExtractValues:
    """extract_values: the mapping loop, and how a miss is treated."""

    def test_every_mapped_parameter_is_extracted_and_typed(self):
        data = {"doc": {"coverage": "100.5", "income": "50", "state": "CA"}}
        values = _extract(
            [
                Parameter(name="coverage", type="Real"),
                Parameter(name="income", type="Int"),
                Parameter(name="state", type="String"),
            ],
            [
                ("coverage", "doc.coverage"),
                ("income", "doc.income"),
                ("state", "doc.state"),
            ],
            data,
        )
        assert values == {"coverage": 100.5, "income": 50, "state": "CA"}

    def test_a_required_parameter_that_resolves_to_none_raises(self):
        # The rule cannot be evaluated, and a missing binding would make the solver
        # report an error with no indication of which path was wrong.
        with pytest.raises(ExtractionError) as excinfo:
            _extract(
                [Parameter(name="coverage", type="Real")],
                [("coverage", "doc.absent")],
                {"doc": {}},
            )
        assert excinfo.value.data_path == "doc.absent"
        assert excinfo.value.parameter_name == "coverage"

    def test_an_optional_parameter_that_resolves_to_none_is_recorded_as_none(self):
        # Recorded rather than omitted: Z3Validator distinguishes "absent" from
        # "present and null" and reports the second as an error outcome.
        values = _extract(
            [
                Parameter(name="coverage", type="Real"),
                Parameter(name="limit", type="Real", required=False),
            ],
            [("coverage", "doc.coverage"), ("limit", "doc.absent")],
            {"doc": {"coverage": 1.0}},
        )
        assert values == {"coverage": 1.0, "limit": None}

    def test_a_mapping_for_an_undeclared_parameter_raises(self):
        # RuleJSON's own bijection check catches this for a well-formed rule, so this
        # is the second line of defence for a rule built another way.
        rule = _rule(
            [Parameter(name="coverage", type="Real")], [("coverage", "doc.coverage")]
        )
        rule.path_mappings.append(
            PathMapping(parameter_name="unknown", data_path="doc.x")
        )
        with pytest.raises(ExtractionError, match="undeclared parameter"):
            DataExtractor().extract_values(rule, {"doc": {"coverage": 1.0, "x": 2}})

    def test_a_type_conversion_failure_names_the_parameter_and_path(self):
        with pytest.raises(ExtractionError) as excinfo:
            _extract(
                [Parameter(name="n", type="Int")],
                [("n", "doc.value")],
                {"doc": {"value": "not a number"}},
            )
        assert excinfo.value.parameter_name == "n"
        assert excinfo.value.data_path == "doc.value"

    def test_a_rule_with_no_mappings_extracts_nothing(self):
        # Workflow B rules have no mappings; returning {} rather than raising is what
        # lets the caller fall through to LLM extraction.
        rule = RuleJSON(
            rule_id="r1",
            version="1.0",
            description="d",
            natural_language_rule="nl",
            parameters=[Parameter(name="coverage", type="Real")],
            constraints=["(> coverage 0)"],
            path_mappings=[],
        )
        assert DataExtractor().extract_values(rule, {"doc": {}}) == {}

    def test_a_zero_reading_is_extracted_rather_than_treated_as_missing(self):
        # The required-parameter check tests `value is None`, so a legitimate zero
        # must not trip it.
        values = _extract(
            [Parameter(name="balance", type="Real")],
            [("balance", "doc.balance")],
            {"doc": {"balance": 0}},
        )
        assert values == {"balance": 0.0}

    def test_a_false_reading_is_extracted_rather_than_treated_as_missing(self):
        values = _extract(
            [Parameter(name="flag", type="Bool")],
            [("flag", "doc.flag")],
            {"doc": {"flag": False}},
        )
        assert values == {"flag": False}

    def test_a_list_indexed_mapping_extracts_one_instance(self):
        values = _extract(
            [Parameter(name="net_pay", type="Real")],
            [("net_pay", "doc.instances[1].NetPay")],
            {"doc": {"instances": [{"NetPay": "100"}, {"NetPay": "200"}]}},
        )
        assert values == {"net_pay": 200.0}


@pytest.mark.unit
class TestExtractionCache:
    """The path cache, its key, and why it has to be clearable.

    `_cache_key` returns `(id(data), path)` — document **identity** paired with the
    path, not the path alone. That distinction is the whole behaviour of this class, and
    it has a consequence filed as
    [#1115](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1115):
    CPython recycles addresses, so two successive short-lived documents can be allotted
    the same `id`, and the second then receives the first's readings. Those readings are
    what get bound into the solver, so the failure mode is a compliance verdict computed
    against another document's data, reported at normal confidence with no signal.

    The recycling half is not asserted here, because forcing an address to be reused is
    allocator-dependent and a test that waits for it would be flaky. What is asserted is
    the premise it rests on — that equal content in two different objects produces two
    different keys — which is deterministic and is what a fix for #1115 must change.
    """

    def test_a_repeated_read_is_served_from_the_cache(self):
        # Proven by mutating the document in place between the two reads: if the second
        # read returned the NEW value the cache was not consulted at all. Comparing two
        # calls on the same unmodified object, which this test used to do, passes
        # identically with and without a cache and so established nothing.
        extractor = DataExtractor()
        rule = _rule(
            [Parameter(name="coverage", type="Real")], [("coverage", "doc.coverage")]
        )
        data = {"doc": {"coverage": 1.0}}
        first = extractor.extract_values(rule, data)
        data["doc"]["coverage"] = 99.0
        second = extractor.extract_values(rule, data)
        assert first == {"coverage": 1.0}
        assert second == {"coverage": 1.0}, (
            "the second read saw the mutated value, so nothing was cached"
        )

    def test_the_cache_key_is_document_identity_not_content(self):
        # The premise of #1115. Two documents with identical content get different keys,
        # which is why the cache is safe for equal-but-distinct documents and unsafe for
        # distinct documents that happen to reuse an address.
        extractor = DataExtractor()
        one = {"doc": {"coverage": 1.0}}
        two = {"doc": {"coverage": 1.0}}
        assert one == two
        assert extractor._cache_key(one, "doc.coverage") != extractor._cache_key(
            two, "doc.coverage"
        ), "equal content shares a key, so the key is no longer identity-based"

    def test_clearing_the_cache_lets_new_data_be_read(self):
        # ValidationSystem.validate_batch clears between rules. Note what this test does
        # and does not establish: both documents here are live at once only briefly, so
        # it passes whether or not the addresses differ, and it is load-bearing for the
        # `clear_cache()` call rather than for the keying. The keying is covered above.
        extractor = DataExtractor()
        rule = _rule(
            [Parameter(name="coverage", type="Real")], [("coverage", "doc.coverage")]
        )
        first = extractor.extract_values(rule, {"doc": {"coverage": 1.0}})
        extractor.clear_cache()
        second = extractor.extract_values(rule, {"doc": {"coverage": 2.0}})
        assert first == {"coverage": 1.0}
        assert second == {"coverage": 2.0}
        assert extractor._cache, "a read should repopulate the cache after clearing"

    def test_clearing_an_empty_cache_is_safe(self):
        DataExtractor().clear_cache()
