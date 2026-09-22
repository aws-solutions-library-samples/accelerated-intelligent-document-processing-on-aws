# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the numeric contract every route into the Z3 solver shares.

A reading reaches the solver by one of three routes: path-based extraction out of
a structured document (`DataExtractor`), LLM extraction (`RuleTranslator`, which
is both the default for a rule with no path mappings and the fallback when path
extraction fails), and the orchestrator's production fact-extraction call, which
hands raw parsed JSON straight to `Z3Validator.validate`. All three end at
`Z3Validator._bind_values`.

What these tests are about is that the routes cannot disagree about what a
reading of a declared type may be. They used to: path extraction refused a
fractional reading for an `Int` as lossy while the other two truncated it toward
zero, so `days_late <= 30` with a reading of 30.9 reported a PASS
(GitHub issue #1057). The verdict is now a refusal on every route, which callers
already render as *Information Not Found*.

`Bool` and `String` are deliberately outside the shared contract, and there is
one known divergence left inside them — an integer read for a `Bool` — pinned
below so that unifying it stays a deliberate act rather than an accident.
"""

import json
from decimal import Decimal
from fractions import Fraction

import pytest
import z3

from idp_common.rule_validation.z3.data_extractor import DataExtractor
from idp_common.rule_validation.z3.exceptions import (
    ExtractionError,
    TranslationError,
    ValidationError,
)
from idp_common.rule_validation.z3.models import Parameter, RuleJSON
from idp_common.rule_validation.z3.rule_translator import RuleTranslator
from idp_common.rule_validation.z3.type_coercion import (
    coerce_numeric_reading,
    exact_numeric_reading,
)
from idp_common.rule_validation.z3.z3_validator import Z3Validator

# A reading that is a whole number, spelled six ways. Every one of these is
# lossless, so refusing any of them would trade a wrong verdict for a needlessly
# missing one.
WHOLE_NUMBER_SPELLINGS = [42, 42.0, Decimal("42"), Decimal("42.0"), Fraction(42), "42"]

# The same value with a fractional part, spelled four ways. `int()` truncates
# every one of them to 30, which is the defect.
FRACTIONAL_SPELLINGS = [30.9, Decimal("30.9"), Fraction(309, 10), "30.9"]


def _rule(constraint: str, declared: str = "Int") -> RuleJSON:
    return RuleJSON(
        rule_id="days_late",
        version="1.0",
        description="At most 30 days late",
        natural_language_rule=constraint,
        parameters=[Parameter(name="n", type=declared)],
        constraints=[constraint],
    )


@pytest.mark.unit
class TestCoerceNumericReading:
    """coerce_numeric_reading: what a reading of a declared numeric type may be."""

    @pytest.mark.parametrize("reading", WHOLE_NUMBER_SPELLINGS)
    def test_a_whole_number_is_accepted_however_it_is_spelled(self, reading):
        result = coerce_numeric_reading(reading, "Int")
        assert result == 42
        assert isinstance(result, int)

    @pytest.mark.parametrize("reading", FRACTIONAL_SPELLINGS)
    def test_a_fractional_reading_is_refused_however_it_is_spelled(self, reading):
        # The point of parametrising over the type as well as the value: a guard
        # written as `isinstance(value, float) and value.is_integer()` catches the
        # float and silently truncates the Decimal and the Fraction, and Decimal
        # is what a reading loaded from DynamoDB or from JSON parsed with
        # `parse_float=Decimal` arrives as.
        with pytest.raises(ValueError, match="without loss"):
            coerce_numeric_reading(reading, "Int")

    def test_a_float_that_is_merely_close_to_whole_is_refused_not_nudged(self):
        # Nothing in the reading says which whole number was meant.
        with pytest.raises(ValueError, match="without loss"):
            coerce_numeric_reading(29.999999999999996, "Int")

    @pytest.mark.parametrize("declared", ["Int", "Real"])
    @pytest.mark.parametrize("reading", [True, False])
    def test_a_bool_is_not_a_number(self, declared, reading):
        # bool is a subclass of int, so True would otherwise bind as 1 and be
        # compared against a numeric threshold.
        with pytest.raises(ValueError, match="Bool value"):
            coerce_numeric_reading(reading, declared)

    @pytest.mark.parametrize("declared", ["Int", "Real"])
    @pytest.mark.parametrize(
        "reading",
        [
            "abc",
            "",
            "   ",
            "30 days",
            # A ratio is an expression, not a numeral. Fraction() would accept
            # it; the reading is parsed with Decimal, which does not.
            "1/3",
            None,
            [],
            {},
            complex(1, 2),
        ],
    )
    def test_a_reading_that_is_not_a_numeral_is_refused(self, declared, reading):
        with pytest.raises(ValueError):
            coerce_numeric_reading(reading, declared)

    @pytest.mark.parametrize(
        "reading,expected", [(30.9, 30.9), ("30.9", 30.9), (Decimal("30.9"), 30.9)]
    )
    def test_a_fractional_reading_is_fine_for_a_real(self, reading, expected):
        result = coerce_numeric_reading(reading, "Real")
        assert result == pytest.approx(expected)
        assert isinstance(result, float)

    def test_a_whole_number_read_as_a_real_becomes_a_float(self):
        assert coerce_numeric_reading(42, "Real") == pytest.approx(42.0)

    @pytest.mark.parametrize("declared", ["Int", "Real"])
    @pytest.mark.parametrize(
        "reading", [float("inf"), float("-inf"), float("nan"), "inf", "nan", "Infinity"]
    )
    def test_a_non_finite_reading_is_refused_rather_than_handed_to_the_solver(
        self, declared, reading
    ):
        # Z3's Int and Real sorts have no infinity and no NaN, so passing one on
        # raises somewhere less informative -- or, for `float("nan")` read as a
        # Real, risks a comparison whose result is not the arithmetic one.
        with pytest.raises(ValueError):
            coerce_numeric_reading(reading, declared)

    def test_a_magnitude_no_float_can_hold_is_refused_for_a_real(self):
        with pytest.raises(ValueError, match="too large"):
            coerce_numeric_reading(Decimal("1e400"), "Real")

    def test_a_magnitude_no_float_can_hold_is_still_an_exact_int(self):
        # Int has no such limit: Python's int is arbitrary-precision and so is
        # Z3's, so refusing this would be refusing an exact reading.
        assert coerce_numeric_reading(Decimal("1e400"), "Int") == 10**400

    @pytest.mark.parametrize("declared", ["Bool", "String", "Decimal", ""])
    def test_a_non_numeric_declared_type_is_rejected_outright(self, declared):
        # The caller decides which types come here; being handed another one is a
        # programming error, not a bad reading.
        with pytest.raises(ValueError, match="Not a numeric parameter type"):
            coerce_numeric_reading(1, declared)

    def test_the_refusal_names_the_value_and_the_loss(self):
        # An operator reading the rule's reasoning has to be able to tell "the
        # model answered a decimal" from "the model answered nonsense".
        with pytest.raises(ValueError) as excinfo:
            coerce_numeric_reading(30.9, "Int")
        message = str(excinfo.value)
        assert "30.9" in message
        assert "without loss" in message


@pytest.mark.unit
class TestEveryRouteAgrees:
    """
    The same reading, the same answer, whichever route it arrives by.

    This is the invariant #1057 broke, and it is asserted directly rather than
    inferred from three separate suites, because the defect was not in any one
    route's behaviour -- each was defensible alone -- but in the disagreement.
    """

    def _path_route(self, reading, declared):
        """DataExtractor: a value read from a structured document at a path."""
        try:
            return DataExtractor()._convert_type(reading, declared)
        except ExtractionError:
            return "refused"

    def _llm_route(self, reading, declared):
        """RuleTranslator: a value a model was asked for and answered."""
        try:
            return RuleTranslator()._parse_extraction_output(
                json.dumps({"extracted_values": {"n": reading}}),
                _rule("(<= n 30)", declared),
            )["n"]
        except TranslationError:
            return "refused"

    def _bind_route(self, reading, declared):
        """Z3Validator: a value bound straight to the solver, checked last."""
        params = [Parameter(name="n", type=declared)]
        validator = Z3Validator(timeout_ms=5000)
        z3_vars = validator._create_z3_variables(params, "r1")
        solver = z3.Solver()
        try:
            validator._bind_values(solver, z3_vars, {"n": reading}, params, "r1")
        except ValidationError:
            return "refused"
        assert solver.check() == z3.sat
        bound = solver.model()[z3_vars["n"]]
        return bound.as_long() if declared == "Int" else float(bound.as_fraction())

    # Only readings JSON can carry are run through the LLM route, since that is
    # what a model's answer is parsed from. Decimal and Fraction reach the other
    # two routes from a document loader.
    @pytest.mark.parametrize(
        "reading,declared,expected",
        [
            (42, "Int", 42),
            (42.0, "Int", 42),
            ("42", "Int", 42),
            (30.9, "Int", "refused"),
            ("30.9", "Int", "refused"),
            (0, "Int", 0),
            (-7, "Int", -7),
            ("abc", "Int", "refused"),
            (True, "Int", "refused"),
            (30.9, "Real", 30.9),
            (42, "Real", 42.0),
            ("30.9", "Real", 30.9),
            (True, "Real", "refused"),
            ("abc", "Real", "refused"),
        ],
    )
    def test_the_three_routes_give_the_same_answer(self, reading, declared, expected):
        answers = {
            "path": self._path_route(reading, declared),
            "llm": self._llm_route(reading, declared),
            "bind": self._bind_route(reading, declared),
        }
        for route, answer in answers.items():
            if expected == "refused":
                assert answer == "refused", f"{route} accepted {reading!r}"
            else:
                assert answer != "refused", f"{route} refused {reading!r}"
                assert answer == pytest.approx(expected), route

    @pytest.mark.parametrize("reading", [Decimal("30.9"), Fraction(309, 10)])
    def test_a_non_json_fractional_reading_is_refused_by_both_routes_that_see_it(
        self, reading
    ):
        assert self._path_route(reading, "Int") == "refused"
        assert self._bind_route(reading, "Int") == "refused"

    # The full width of the divergence, which is wider than the 0/1 case it is
    # tempting to describe it as: `bool(37)` is True, so path extraction maps
    # EVERY non-zero integer to True. For 1 that loses nothing; for 37 it is a
    # guess, which is the behaviour this change removes everywhere else.
    @pytest.mark.parametrize("reading", [1, 0, 2, -1, 37, 10**9])
    def test_an_integer_read_for_a_bool_is_the_one_remaining_route_divergence(
        self, reading
    ):
        # Bool and String are outside the shared numeric contract, and this is
        # the one case where the routes still differ: path extraction truthies
        # the integer, while binding refuses a non-bool, non-string reading
        # outright. It is not the #1057 defect class, because the strict route
        # refuses rather than guessing and so no verdict is derived from the
        # guess -- which is why it is pinned here rather than changed. Unifying
        # the two is a deliberate decision that should land on this test.
        assert self._path_route(reading, "Bool") is bool(reading)
        assert self._bind_route(reading, "Bool") == "refused"


@pytest.mark.unit
class TestRealReadingsReachTheSolverExactly:
    """
    A `Real` reading is bound as an exact rational, not as the nearest double.

    Rounding it first is a wrong-verdict path of its own, and a narrow one that an
    inequality cannot expose: a double holds about 17 significant digits, so the
    error is invisible to `<=` at any threshold, and decides an `=` below that.
    A `Decimal` read from DynamoDB carries up to 38 significant digits.
    """

    # 22 significant digits: equal to 0.1 as a double, not equal as a number.
    BELOW_ULP = Decimal("0.1000000000000000000001")

    def _outcome(self, reading, constraint):
        return Z3Validator().validate(_rule(constraint, "Real"), {"n": reading}).outcome

    def test_a_reading_that_is_not_the_literal_does_not_report_a_pass(self):
        # The reading differs from 0.1 in the 22nd digit, so `(= n 0.1)` is false.
        # Collapsing it to a double first makes it exactly 0.1 and reports `sat`.
        assert self._outcome(self.BELOW_ULP, "(= n 0.1)") == "unsat"

    def test_a_reading_that_is_the_literal_does_report_a_pass(self):
        # The other direction, and the reason the constraint's own literals are
        # parsed exactly too: an exact reading compared against a literal that was
        # collapsed to a double is `unsat` for a rule that is true.
        assert self._outcome(self.BELOW_ULP, f"(= n {self.BELOW_ULP})") == "sat"

    def test_an_inequality_is_unaffected_either_way(self):
        # Stated so the bound on the claim is in the suite: this is why the
        # rounding was invisible in every threshold rule.
        assert self._outcome(self.BELOW_ULP, "(<= n 1)") == "sat"
        assert self._outcome(self.BELOW_ULP, "(>= n 1)") == "unsat"

    @pytest.mark.parametrize(
        "reading",
        [BELOW_ULP, Decimal("1E-30"), Fraction(1, 3), Decimal("30.9"), 30.9, "30.9"],
    )
    def test_the_value_the_solver_holds_is_the_reading_itself(self, reading):
        params = [Parameter(name="n", type="Real")]
        validator = Z3Validator()
        z3_vars = validator._create_z3_variables(params, "r1")
        solver = z3.Solver()
        validator._bind_values(solver, z3_vars, {"n": reading}, params, "r1")
        assert solver.check() == z3.sat
        bound = solver.model()[z3_vars["n"]].as_fraction()
        assert bound == exact_numeric_reading(reading, "Real")
        # ... and for a Decimal or a Fraction reading that is the reading exactly,
        # not a rounding of it. A float reading is already a binary rational, so
        # `Fraction(30.9)` is what 30.9 *is*, not what it was written as.
        if isinstance(reading, (Decimal, Fraction)):
            assert bound == Fraction(reading)

    def test_a_tiny_magnitude_is_exact_in_the_verdict_and_rounded_in_the_record(self):
        # `extracted_values` and `model` are JSON, so they carry doubles; the
        # verdict does not. 1e-400 is below the smallest double, so the recorded
        # value underflows to 0.0 while the rule is still decided on the reading.
        tiny = Decimal("1e-400")
        assert coerce_numeric_reading(tiny, "Real") == 0.0
        assert exact_numeric_reading(tiny, "Real") == Fraction(1, 10**400)
        assert self._outcome(tiny, "(> n 0)") == "sat"

    def test_the_model_reports_the_nearest_double_in_one_rounding_step(self):
        # Dividing a separately-rounded numerator by a separately-rounded
        # denominator rounds twice, and reported an exactly-bound 1e-30 as
        # 9.999999999999999e-31.
        result = Z3Validator().validate(
            _rule("(> n 0)", "Real"), {"n": Decimal("1E-30")}
        )
        assert result.outcome == "sat"
        assert result.model["n"] == 1e-30

    @pytest.mark.parametrize(
        "reading,declared",
        [
            (Decimal("1e400"), "Real"),
            (Decimal("-1e400"), "Real"),
        ],
    )
    def test_a_magnitude_with_no_double_is_refused_by_both_entry_points(
        self, reading, declared
    ):
        # The exact entry point applies the same magnitude check even though a
        # Fraction could hold it, because every route records the reading next to
        # the verdict and there is nowhere to record this one.
        with pytest.raises(ValueError, match="too large"):
            exact_numeric_reading(reading, declared)
        with pytest.raises(ValueError, match="too large"):
            coerce_numeric_reading(reading, declared)

    @pytest.mark.parametrize("declared", ["Int", "Real"])
    @pytest.mark.parametrize(
        "reading",
        WHOLE_NUMBER_SPELLINGS
        + FRACTIONAL_SPELLINGS
        + [True, None, "abc", "", "1/3", float("nan"), float("inf"), Decimal("1e400")],
    )
    def test_the_two_entry_points_accept_and_refuse_the_same_readings(
        self, reading, declared
    ):
        # They differ in representation only. Asserted over the whole table
        # because a second entry point is a second place for the contract to
        # drift, and the drift would be silent.
        def verdict(fn):
            try:
                return "accepted", fn(reading, declared)
            except ValueError:
                return "refused", None

        exact_verdict, exact_value = verdict(exact_numeric_reading)
        coerce_verdict, coerce_value = verdict(coerce_numeric_reading)
        assert exact_verdict == coerce_verdict, reading
        if exact_verdict == "accepted":
            if declared == "Int":
                # Int is exact in both, including a magnitude no float can hold.
                assert exact_value == coerce_value
            else:
                assert float(exact_value) == pytest.approx(coerce_value)

    def test_the_exact_entry_point_returns_an_exact_type(self):
        assert isinstance(exact_numeric_reading(42, "Int"), int)
        assert isinstance(exact_numeric_reading(30.9, "Real"), Fraction)


@pytest.mark.unit
class TestVerdictsThroughValidate:
    """
    The observable verdict, which is what a compliance report shows.

    `test_z3_validator_smt.py` pins the four `days_late` cases that rule out
    truncating, rounding and binding-the-fraction. These cover what those four
    do not: the other numeric spellings, and the sibling type.
    """

    def _outcome(self, reading, constraint, declared="Int"):
        try:
            return (
                Z3Validator()
                .validate(_rule(constraint, declared), {"n": reading})
                .outcome
            )
        except ValidationError:
            return "refused"

    @pytest.mark.parametrize("reading", FRACTIONAL_SPELLINGS)
    @pytest.mark.parametrize("constraint", ["(<= n 30)", "(>= n 31)", "(= n 30)"])
    def test_no_fractional_spelling_yields_a_verdict_for_an_int_rule(
        self, reading, constraint
    ):
        # Refusal, not PASS and not FAIL: 30.9 satisfies none of these and
        # violates none of them, because it is not a value this rule can hold.
        assert self._outcome(reading, constraint) == "refused"

    @pytest.mark.parametrize("reading", WHOLE_NUMBER_SPELLINGS)
    def test_a_whole_number_spelling_still_yields_a_verdict(self, reading):
        # The fix must not turn every numeric reading into a refusal; 42 <= 100
        # is an ordinary PASS.
        assert self._outcome(reading, "(<= n 100)") == "sat"
        assert self._outcome(reading, "(<= n 10)") == "unsat"

    @pytest.mark.parametrize(
        "constraint,expected",
        [
            ("(<= n 30)", "unsat"),
            ("(<= n 40)", "sat"),
            ("(>= n 31)", "unsat"),
            ("(>= n 30)", "sat"),
        ],
    )
    def test_a_fractional_reading_is_exact_for_a_real_rule(self, constraint, expected):
        # The sibling type, over the same four cases: declared Real, 30.9 is a
        # value the rule can hold, and every verdict is the arithmetic one. A
        # "round it and move on" fix to the Int route would have had to break
        # one of these to be consistent.
        assert self._outcome(30.9, constraint, "Real") == expected

    def test_a_refusal_names_the_parameter_and_the_rule(self):
        # What the caller renders: `Z3RuleEngine` and the orchestrator both put
        # this text in the rule's reasoning, so it has to say which reading was
        # refused and why.
        with pytest.raises(ValidationError) as excinfo:
            Z3Validator().validate(_rule("(<= n 30)"), {"n": 30.9})
        assert excinfo.value.rule_id == "days_late"
        message = str(excinfo.value)
        assert "n" in message
        assert "without loss" in message


@pytest.mark.unit
class TestLlmRouteChecksItsOutput:
    """
    _parse_extraction_output: the per-parameter loop now reads `param.type`.

    Before, this route validated that a required parameter was present and
    non-null and nothing else, so a model's answer was never compared against the
    type it was asked for.
    """

    def _parse(self, values, parameters):
        rule = RuleJSON(
            rule_id="r",
            version="1.0",
            description="d",
            natural_language_rule="n <= 30",
            parameters=parameters,
            constraints=["(<= n 30)"],
        )
        return RuleTranslator()._parse_extraction_output(
            json.dumps({"extracted_values": values}), rule
        )

    def test_a_fractional_answer_for_an_int_is_refused_here(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"n": 30.9}, [Parameter(name="n", type="Int")])
        assert any(
            "without loss" in e for e in (excinfo.value.validation_errors or [])
        ), excinfo.value.validation_errors

    def test_an_optional_parameter_is_type_checked_too(self):
        # A bad optional reading reaches the solver exactly as a required one
        # does; only its absence is permitted.
        with pytest.raises(TranslationError):
            self._parse({"n": 30.9}, [Parameter(name="n", type="Int", required=False)])

    def test_an_absent_optional_parameter_is_still_allowed(self):
        assert self._parse({}, [Parameter(name="n", type="Int", required=False)]) == {}

    def test_a_null_optional_parameter_is_left_as_null(self):
        # Null handling belongs to _check_null_values, which leaves the variable
        # free rather than inventing a value for it.
        params = [Parameter(name="n", type="Int", required=False)]
        assert self._parse({"n": None}, params) == {"n": None}

    def test_a_numeric_string_answer_is_normalised_to_the_declared_type(self):
        # So the value reported alongside the verdict is the one the solver saw.
        assert self._parse({"n": "42"}, [Parameter(name="n", type="Int")]) == {"n": 42}
        assert self._parse({"n": "2.5"}, [Parameter(name="n", type="Real")]) == {
            "n": 2.5
        }

    def test_bool_and_string_answers_are_passed_through_untouched(self):
        params = [
            Parameter(name="flag", type="Bool"),
            Parameter(name="label", type="String"),
        ]
        values = {"flag": "Yes", "label": " 30.9 "}
        assert self._parse(values, params) == values

    def test_a_missing_required_parameter_is_still_reported(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({}, [Parameter(name="n", type="Int")])
        assert "Missing required parameter: n" in (
            excinfo.value.validation_errors or []
        )

    def test_a_null_required_parameter_is_still_reported(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"n": None}, [Parameter(name="n", type="Int")])
        assert "Required parameter 'n' is null" in (
            excinfo.value.validation_errors or []
        )
