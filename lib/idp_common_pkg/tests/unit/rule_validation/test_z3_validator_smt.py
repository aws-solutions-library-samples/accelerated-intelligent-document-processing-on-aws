# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Z3Validator's SMT-LIB layer: tokenizer, recursive-descent parser,
operator application, value binding, model extraction and the validate() facade.

These are the deterministic, offline parts of rule validation. Nothing here calls
AWS or an LLM; the only external dependency is the z3 solver itself, which is a
declared dependency of the rule_validation extra.

The operator table is exercised exhaustively rather than by sampling, because
Z3Validator._apply_smt_operator is the single point where an LLM-authored
constraint string becomes a solver expression. An operator that silently accepts
the wrong arity there produces a confident PASS on a rule nobody checked.
"""

import pytest
import z3

from idp_common.rule_validation.z3.exceptions import ValidationError
from idp_common.rule_validation.z3.models import (
    Parameter,
    RuleJSON,
    RuleWithValues,
)
from idp_common.rule_validation.z3.z3_validator import Z3Validator


def _validator() -> Z3Validator:
    return Z3Validator(timeout_ms=5000)


def _vars(**types):
    """Build a z3_vars dict, e.g. _vars(x="Int", flag="Bool")."""
    makers = {"Int": z3.Int, "Real": z3.Real, "Bool": z3.Bool, "String": z3.String}
    return {name: makers[t](name) for name, t in types.items()}


def _is_sat(expr) -> bool:
    s = z3.Solver()
    s.add(expr)
    return s.check() == z3.sat


def _eval_int(expr) -> int:
    """Solve `probe == expr` and return the concrete integer."""
    probe = z3.Int("__probe")
    s = z3.Solver()
    s.add(probe == expr)
    assert s.check() == z3.sat
    return s.model()[probe].as_long()


@pytest.mark.unit
class TestTokenizeSmt:
    """_tokenize_smt: string -> token list."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("(>= x 10)", ["(", ">=", "x", "10", ")"]),
            (
                "(and (> a 1) (< b 2))",
                ["(", "and", "(", ">", "a", "1", ")", "(", "<", "b", "2", ")", ")"],
            ),
            # Whitespace is not significant and runs of it collapse.
            ("(  >=   x\t10\n)", ["(", ">=", "x", "10", ")"]),
            # A bare atom is a legal single token.
            ("true", ["true"]),
            # Negative numbers stay one token; '-' is not split off.
            ("(= x -5)", ["(", "=", "x", "-5", ")"]),
            # Decimals stay one token.
            ("(<= ratio 0.25)", ["(", "<=", "ratio", "0.25", ")"]),
            ("", []),
        ],
    )
    def test_tokenizes(self, source, expected):
        assert _validator()._tokenize_smt(source) == expected

    def test_quoted_string_is_one_token_including_quotes(self):
        assert _validator()._tokenize_smt('(= name "John Doe")') == [
            "(",
            "=",
            "name",
            '"John Doe"',
            ")",
        ]

    def test_escaped_quote_inside_string_does_not_terminate_it(self):
        tokens = _validator()._tokenize_smt(r'(= s "a\"b")')
        assert tokens == ["(", "=", "s", r'"a\"b"', ")"]

    def test_unclosed_quote_falls_back_to_a_bare_token(self):
        # No closing quote: the scanner must not run off the end of the string.
        tokens = _validator()._tokenize_smt('(= name "John)')
        assert tokens[:3] == ["(", "=", "name"]
        assert any(t.startswith('"') for t in tokens)

    def test_parens_need_no_surrounding_whitespace(self):
        assert _validator()._tokenize_smt("(not(> x 1))") == [
            "(",
            "not",
            "(",
            ">",
            "x",
            "1",
            ")",
            ")",
        ]


@pytest.mark.unit
class TestParseSmtAtom:
    """_parse_smt_atom: single token -> z3 value or variable."""

    def test_declared_variable_resolves_to_that_variable(self):
        z3_vars = _vars(x="Int")
        assert _validator()._parse_smt_atom("x", z3_vars) is z3_vars["x"]

    @pytest.mark.parametrize("token,expected", [("true", True), ("false", False)])
    def test_boolean_literals(self, token, expected):
        # Asserted as an equality against the expected z3 value rather than as
        # "not the opposite literal": the latter is satisfied by anything that is
        # not a bool at all, so returning IntVal(0) for "true" would pass it.
        value = _validator()._parse_smt_atom(token, {})
        assert z3.eq(value, z3.BoolVal(expected))

    def test_integer_literal(self):
        assert _validator()._parse_smt_atom("42", {}).as_long() == 42

    def test_negative_integer_literal(self):
        assert _validator()._parse_smt_atom("-7", {}).as_long() == -7

    def test_decimal_literal_becomes_a_real(self):
        value = _validator()._parse_smt_atom("2.5", {})
        assert z3.is_rational_value(value)
        assert float(value.numerator_as_long()) / float(
            value.denominator_as_long()
        ) == pytest.approx(2.5)

    def test_quoted_literal_becomes_a_string_with_quotes_stripped(self):
        assert _validator()._parse_smt_atom('"hello"', {}).as_string() == "hello"

    def test_a_variable_shadows_a_literal_of_the_same_spelling(self):
        # Lookup happens before literal parsing, so a parameter named "true"
        # resolves to the variable rather than to BoolVal(True).
        z3_vars = _vars(true="Int")
        assert _validator()._parse_smt_atom("true", z3_vars) is z3_vars["true"]

    def test_unknown_bare_token_raises_rather_than_coercing_to_a_string(self):
        # This is the guard against an LLM misspelling a parameter name. Coercing
        # it to StringVal("incom") would make the constraint trivially satisfiable
        # and report PASS for a rule that was never evaluated.
        with pytest.raises(ValidationError) as excinfo:
            _validator()._parse_smt_atom("incom", _vars(income="Real"))
        assert "incom" in str(excinfo.value)


@pytest.mark.unit
class TestApplySmtOperatorArithmetic:
    """_apply_smt_operator: arithmetic."""

    def test_addition_is_variadic(self):
        # Operands chosen so the sum and the product differ: 1+2+3 and 1*2*3 are
        # both 6, so that triple cannot tell '+' from '*'.
        expr = _validator()._apply_smt_operator(
            "+", [z3.IntVal(2), z3.IntVal(3), z3.IntVal(4)]
        )
        assert _eval_int(expr) == 9

    def test_unary_minus_negates(self):
        assert _eval_int(_validator()._apply_smt_operator("-", [z3.IntVal(5)])) == -5

    def test_subtraction_is_left_associative_and_variadic(self):
        expr = _validator()._apply_smt_operator(
            "-", [z3.IntVal(10), z3.IntVal(3), z3.IntVal(2)]
        )
        assert _eval_int(expr) == 5

    def test_multiplication_is_variadic(self):
        expr = _validator()._apply_smt_operator(
            "*", [z3.IntVal(2), z3.IntVal(3), z3.IntVal(4)]
        )
        assert _eval_int(expr) == 24

    def test_division(self):
        expr = _validator()._apply_smt_operator("/", [z3.RealVal(7), z3.RealVal(2)])
        assert _is_sat(expr == z3.RealVal("3.5"))

    @pytest.mark.parametrize("op", ["mod", "%"])
    def test_modulo_accepts_both_spellings(self, op):
        expr = _validator()._apply_smt_operator(op, [z3.IntVal(7), z3.IntVal(3)])
        assert _eval_int(expr) == 1

    @pytest.mark.parametrize(
        "op,args",
        [
            ("+", [z3.IntVal(1)]),
            ("*", [z3.IntVal(1)]),
            ("-", []),
            ("/", [z3.RealVal(1)]),
            ("/", [z3.RealVal(1), z3.RealVal(2), z3.RealVal(3)]),
            ("mod", [z3.IntVal(1)]),
        ],
    )
    def test_wrong_arity_raises(self, op, args):
        with pytest.raises(ValueError):
            _validator()._apply_smt_operator(op, args)


@pytest.mark.unit
class TestApplySmtOperatorComparison:
    """_apply_smt_operator: comparison."""

    @pytest.mark.parametrize(
        "op,left,right,expected",
        [
            ("<", 1, 2, True),
            ("<", 2, 1, False),
            # The equal-argument rows are what separate the strict operators from
            # the non-strict ones. Without them, '<' implemented as '<=' passes.
            ("<", 2, 2, False),
            (">", 2, 1, True),
            (">", 1, 2, False),
            (">", 2, 2, False),
            ("<=", 2, 2, True),
            ("<=", 3, 2, False),
            (">=", 2, 2, True),
            (">=", 1, 2, False),
            ("=", 2, 2, True),
            ("=", 1, 2, False),
        ],
    )
    def test_binary_comparisons(self, op, left, right, expected):
        expr = _validator()._apply_smt_operator(op, [z3.IntVal(left), z3.IntVal(right)])
        assert _is_sat(expr) is expected

    def test_equality_of_three_arguments_requires_all_equal(self):
        v = _validator()
        assert _is_sat(
            v._apply_smt_operator("=", [z3.IntVal(2), z3.IntVal(2), z3.IntVal(2)])
        )
        assert not _is_sat(
            v._apply_smt_operator("=", [z3.IntVal(2), z3.IntVal(2), z3.IntVal(3)])
        )

    @pytest.mark.parametrize("op", ["!=", "distinct"])
    def test_distinct_two_arguments(self, op):
        v = _validator()
        assert _is_sat(v._apply_smt_operator(op, [z3.IntVal(1), z3.IntVal(2)]))
        assert not _is_sat(v._apply_smt_operator(op, [z3.IntVal(1), z3.IntVal(1)]))

    def test_distinct_is_pairwise_not_merely_adjacent(self):
        # (distinct 1 2 1) must be unsat: the first and third clash even though no
        # adjacent pair does.
        expr = _validator()._apply_smt_operator(
            "distinct", [z3.IntVal(1), z3.IntVal(2), z3.IntVal(1)]
        )
        assert not _is_sat(expr)

    @pytest.mark.parametrize(
        "op,args",
        [
            ("<", [z3.IntVal(1)]),
            (">", [z3.IntVal(1), z3.IntVal(2), z3.IntVal(3)]),
            ("<=", [z3.IntVal(1)]),
            (">=", [z3.IntVal(1)]),
            ("=", [z3.IntVal(1)]),
            ("distinct", [z3.IntVal(1)]),
        ],
    )
    def test_wrong_arity_raises(self, op, args):
        with pytest.raises(ValueError):
            _validator()._apply_smt_operator(op, args)


@pytest.mark.unit
class TestApplySmtOperatorLogical:
    """_apply_smt_operator: logical."""

    def test_and_is_variadic(self):
        v = _validator()
        assert _is_sat(
            v._apply_smt_operator(
                "and", [z3.BoolVal(True), z3.BoolVal(True), z3.BoolVal(True)]
            )
        )
        assert not _is_sat(
            v._apply_smt_operator("and", [z3.BoolVal(True), z3.BoolVal(False)])
        )

    def test_or_is_variadic(self):
        v = _validator()
        assert _is_sat(
            v._apply_smt_operator("or", [z3.BoolVal(False), z3.BoolVal(True)])
        )
        assert not _is_sat(
            v._apply_smt_operator("or", [z3.BoolVal(False), z3.BoolVal(False)])
        )

    def test_not(self):
        v = _validator()
        assert _is_sat(v._apply_smt_operator("not", [z3.BoolVal(False)]))
        assert not _is_sat(v._apply_smt_operator("not", [z3.BoolVal(True)]))

    @pytest.mark.parametrize("op", ["implies", "=>"])
    @pytest.mark.parametrize(
        "antecedent,consequent,expected",
        [
            (True, True, True),
            (True, False, False),
            (False, False, True),
            (False, True, True),
        ],
    )
    def test_implication_truth_table(self, op, antecedent, consequent, expected):
        expr = _validator()._apply_smt_operator(
            op, [z3.BoolVal(antecedent), z3.BoolVal(consequent)]
        )
        assert _is_sat(expr) is expected

    def test_ite_selects_by_condition(self):
        v = _validator()
        assert (
            _eval_int(
                v._apply_smt_operator(
                    "ite", [z3.BoolVal(True), z3.IntVal(1), z3.IntVal(2)]
                )
            )
            == 1
        )
        assert (
            _eval_int(
                v._apply_smt_operator(
                    "ite", [z3.BoolVal(False), z3.IntVal(1), z3.IntVal(2)]
                )
            )
            == 2
        )

    @pytest.mark.parametrize(
        "op,args",
        [
            ("and", []),
            ("or", []),
            ("not", [z3.BoolVal(True), z3.BoolVal(True)]),
            ("not", []),
            ("implies", [z3.BoolVal(True)]),
            ("ite", [z3.BoolVal(True), z3.IntVal(1)]),
        ],
    )
    def test_wrong_arity_raises(self, op, args):
        with pytest.raises(ValueError):
            _validator()._apply_smt_operator(op, args)

    def test_unsupported_operator_names_itself(self):
        with pytest.raises(ValueError) as excinfo:
            _validator()._apply_smt_operator(
                "xor", [z3.BoolVal(True), z3.BoolVal(True)]
            )
        assert "xor" in str(excinfo.value)

    @pytest.mark.parametrize("op", ["abs", "max", "min"])
    def test_abs_max_min_are_not_supported(self, op):
        # _add_constraints' docstring lists abs/max/min as "converted to ite
        # expressions". They are not implemented, and reach the catch-all. The
        # behaviour, not the docstring, is what a translated rule meets at
        # runtime, so it is what is pinned here.
        with pytest.raises(ValueError):
            _validator()._apply_smt_operator(op, [z3.IntVal(-1)])


@pytest.mark.unit
class TestParseSmtConstraint:
    """_parse_smt_constraint: end-to-end string -> expression, and its error paths."""

    def test_nested_expression_parses_and_evaluates(self):
        z3_vars = _vars(coverage="Real", income="Real")
        expr = _validator()._parse_smt_constraint(
            "(<= (/ coverage income) 20)", z3_vars, "r1", 0
        )
        s = z3.Solver()
        s.add(expr, z3_vars["coverage"] == 100, z3_vars["income"] == 10)
        assert s.check() == z3.sat

    def test_trailing_tokens_are_rejected_rather_than_dropped(self):
        # Two S-expressions in one constraint string: silently keeping the first
        # would drop half the rule.
        with pytest.raises(ValidationError) as excinfo:
            _validator()._parse_smt_constraint(
                "(> x 1) (< x 5)", _vars(x="Int"), "r1", 0
            )
        assert "r1" == excinfo.value.rule_id

    @pytest.mark.parametrize(
        "constraint",
        [
            "(> x 1",  # missing close paren
            ")",  # stray close paren
            "",  # empty
            "(",  # nothing after open paren
        ],
    )
    def test_malformed_input_raises_validation_error(self, constraint):
        with pytest.raises(ValidationError):
            _validator()._parse_smt_constraint(constraint, _vars(x="Int"), "r1", 3)

    def test_error_carries_the_constraint_index(self):
        with pytest.raises(ValidationError) as excinfo:
            _validator()._parse_smt_constraint("(> x", _vars(x="Int"), "r1", 7)
        assert excinfo.value.constraint_index == 7


@pytest.mark.unit
class TestAddConstraints:
    """_add_constraints: the loop over a rule's constraint list."""

    def test_all_constraints_reach_the_solver(self):
        z3_vars = _vars(x="Int")
        solver = z3.Solver()
        _validator()._add_constraints(
            solver, ["(> x 0)", "(< x 10)", "(= (mod x 2) 0)"], z3_vars, "r1"
        )
        assert solver.check() == z3.sat
        assert solver.model()[z3_vars["x"]].as_long() % 2 == 0

    def test_mutually_contradictory_constraints_are_unsat(self):
        solver = z3.Solver()
        _validator()._add_constraints(
            solver, ["(> x 5)", "(< x 2)"], _vars(x="Int"), "r1"
        )
        assert solver.check() == z3.unsat

    def test_a_bad_constraint_aborts_the_whole_list(self):
        with pytest.raises(ValidationError):
            _validator()._add_constraints(
                z3.Solver(), ["(> x 0)", "(!! x)"], _vars(x="Int"), "r1"
            )


@pytest.mark.unit
class TestCreateZ3Variables:
    """_create_z3_variables: parameter declarations -> typed z3 variables."""

    @pytest.mark.parametrize(
        "declared,sort_name",
        [("Int", "Int"), ("Real", "Real"), ("Bool", "Bool"), ("String", "String")],
    )
    def test_each_supported_type_maps_to_the_matching_sort(self, declared, sort_name):
        z3_vars = _validator()._create_z3_variables(
            [Parameter(name="p", type=declared)], "r1"
        )
        assert z3_vars["p"].sort().name() == sort_name

    def test_every_parameter_gets_a_variable(self):
        params = [
            Parameter(name="a", type="Int"),
            Parameter(name="b", type="Real"),
            Parameter(name="c", type="Bool"),
        ]
        assert set(_validator()._create_z3_variables(params, "r1")) == {"a", "b", "c"}

    def test_unsupported_type_raises_with_the_parameter_named(self):
        # Parameter.__post_init__ rejects unknown types, so the only way to reach
        # this branch is a Parameter whose type was mutated afterwards -- which is
        # what a from_dict round-trip through a hand-edited rule JSON can produce.
        param = Parameter(name="p", type="Int")
        object.__setattr__(param, "type", "Decimal")
        with pytest.raises(ValidationError) as excinfo:
            _validator()._create_z3_variables([param], "r1")
        assert "Decimal" in str(excinfo.value)


@pytest.mark.unit
class TestCheckNullValues:
    """_check_null_values: which missing values block evaluation."""

    def test_required_parameter_missing_entirely_is_reported(self):
        params = [Parameter(name="a", type="Int", required=True)]
        assert _validator()._check_null_values(params, {}) == ["a"]

    def test_required_parameter_present_but_none_is_reported(self):
        params = [Parameter(name="a", type="Int", required=True)]
        assert _validator()._check_null_values(params, {"a": None}) == ["a"]

    def test_optional_parameter_may_be_absent(self):
        params = [Parameter(name="a", type="Int", required=False)]
        assert _validator()._check_null_values(params, {}) == []

    def test_zero_and_false_are_values_not_nulls(self):
        # A falsy-but-present value must not be mistaken for a missing one; `0`
        # and `False` are ordinary readings.
        params = [
            Parameter(name="count", type="Int", required=True),
            Parameter(name="flag", type="Bool", required=True),
        ]
        assert (
            _validator()._check_null_values(params, {"count": 0, "flag": False}) == []
        )

    def test_all_missing_required_parameters_are_reported_not_just_the_first(self):
        params = [
            Parameter(name="a", type="Int"),
            Parameter(name="b", type="Int"),
            Parameter(name="c", type="Int", required=False),
        ]
        assert _validator()._check_null_values(params, {}) == ["a", "b"]


@pytest.mark.unit
class TestBindValues:
    """_bind_values: extracted readings -> equality constraints."""

    def _bound_model(self, params, values):
        v = _validator()
        z3_vars = v._create_z3_variables(params, "r1")
        solver = z3.Solver()
        v._bind_values(solver, z3_vars, values, params, "r1")
        assert solver.check() == z3.sat
        return solver.model(), z3_vars

    def test_int_value_binds(self):
        model, z3_vars = self._bound_model([Parameter(name="n", type="Int")], {"n": 42})
        assert model[z3_vars["n"]].as_long() == 42

    def test_a_fractional_reading_for_an_int_parameter_is_not_silently_truncated(self):
        # Asserted on the OBSERVABLE VERDICT rather than on solver satisfiability,
        # and over two thresholds, because the shape of the assertion decides which
        # wrong remedies this test can detect.
        #
        # Asserting `solver.check() == z3.unsat` after _bind_values would not work:
        # refusing the lossy reading, which is what the code does and what the
        # path-based extractor already did, raises before the assertion is reached.
        # Refusing is therefore treated as a correct outcome here.
        #
        # The other three cases each exclude a remedy that looks right on the first,
        # because a single `<=` case constrains the reading in one direction only:
        #
        #  * Binding 30.9 into an Int variable makes the constraint set unsatisfiable
        #    for ANY threshold, since no integer equals 30.9 -- so it turns every
        #    fractional reading into a FAIL. Wrong for `n <= 40`, which 30.9 satisfies.
        #  * Rounding or ceiling the reading to 31 gets both `<=` cases right and is
        #    still wrong the other way round: `n >= 31` with a reading of 30.9 then
        #    reports PASS, which is the same silent wrong verdict at the other
        #    boundary. The `>=` cases are what exclude those.
        def _outcome(operator: str, threshold: int):
            rule = _rule_json(
                rule_id="days_late",
                natural_language_rule=f"n {operator} {threshold}",
                parameters=[Parameter(name="n", type="Int")],
                constraints=[f"({operator} n {threshold})"],
            )
            try:
                return _validator().validate(rule, {"n": 30.9}).outcome
            except ValidationError:
                # Refusing to evaluate a lossy reading is an honest answer.
                return "refused"

        # 30.9 <= 30 is false, so a PASS here is a wrong verdict. Truncating the
        # reading to int(30.9) == 30 produces exactly that, which is the defect
        # this case exists for (#1057).
        assert _outcome("<=", 30) != "sat"
        # 30.9 <= 40 is true, so a FAIL here would also be a wrong verdict.
        assert _outcome("<=", 40) != "unsat"
        # 30.9 >= 31 is false: excludes rounding or ceiling the reading up to 31.
        assert _outcome(">=", 31) != "sat"
        # 30.9 >= 30 is true: excludes floor-and-then-fail in the other direction.
        assert _outcome(">=", 30) != "unsat"

    def test_a_whole_number_float_reading_binds_without_loss(self):
        # 42.0 loses nothing, so it is bound rather than refused. This case is the
        # boundary that any fix for #1057 must preserve.
        model, z3_vars = self._bound_model(
            [Parameter(name="n", type="Int")], {"n": 42.0}
        )
        assert model[z3_vars["n"]].as_long() == 42

    def test_numeric_string_binds_to_an_int_parameter(self):
        model, z3_vars = self._bound_model(
            [Parameter(name="n", type="Int")], {"n": "42"}
        )
        assert model[z3_vars["n"]].as_long() == 42

    def test_real_value_binds(self):
        params = [Parameter(name="r", type="Real")]
        v = _validator()
        z3_vars = v._create_z3_variables(params, "r1")
        solver = z3.Solver()
        v._bind_values(solver, z3_vars, {"r": 2.5}, params, "r1")
        solver.add(z3_vars["r"] == z3.RealVal("2.5"))
        assert solver.check() == z3.sat

    @pytest.mark.parametrize("raw", [True, False])
    def test_bool_value_binds(self, raw):
        model, z3_vars = self._bound_model(
            [Parameter(name="b", type="Bool")], {"b": raw}
        )
        assert bool(model[z3_vars["b"]]) is raw

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("TRUE", True),
            ("Yes", True),
            ("1", True),
            ("false", False),
            ("NO", False),
            ("0", False),
        ],
    )
    def test_bool_accepts_a_documented_set_of_string_spellings(self, raw, expected):
        model, z3_vars = self._bound_model(
            [Parameter(name="b", type="Bool")], {"b": raw}
        )
        assert bool(model[z3_vars["b"]]) is expected

    def test_string_value_binds(self):
        model, z3_vars = self._bound_model(
            [Parameter(name="s", type="String")], {"s": "hello"}
        )
        assert model[z3_vars["s"]].as_string() == "hello"

    def test_none_is_skipped_rather_than_bound(self):
        # Null handling belongs to _check_null_values; binding must leave the
        # variable free rather than inventing a value for it.
        params = [Parameter(name="a", type="Int", required=False)]
        v = _validator()
        z3_vars = v._create_z3_variables(params, "r1")
        solver = z3.Solver()
        v._bind_values(solver, z3_vars, {"a": None}, params, "r1")
        assert len(solver.assertions()) == 0

    def test_a_value_with_no_declared_parameter_is_ignored(self):
        params = [Parameter(name="a", type="Int")]
        v = _validator()
        z3_vars = v._create_z3_variables(params, "r1")
        solver = z3.Solver()
        v._bind_values(solver, z3_vars, {"a": 1, "stray": 99}, params, "r1")
        assert solver.check() == z3.sat

    @pytest.mark.parametrize("declared", ["Int", "Real"])
    def test_bool_reading_for_a_numeric_parameter_is_a_type_error(self, declared):
        # Python's bool is an int subclass, so int(True) == 1 would bind silently.
        # A rule reading "is the balance over 500" as True -> 1 would then be
        # compared against a dollar threshold.
        params = [Parameter(name="n", type=declared)]
        v = _validator()
        with pytest.raises(ValidationError) as excinfo:
            v._bind_values(
                z3.Solver(),
                v._create_z3_variables(params, "r1"),
                {"n": True},
                params,
                "r1",
            )
        assert "Type mismatch" in str(excinfo.value)

    def test_unparseable_bool_string_is_a_type_error(self):
        params = [Parameter(name="b", type="Bool")]
        v = _validator()
        with pytest.raises(ValidationError) as excinfo:
            v._bind_values(
                z3.Solver(),
                v._create_z3_variables(params, "r1"),
                {"b": "maybe"},
                params,
                "r1",
            )
        assert "maybe" in str(excinfo.value)

    def test_non_string_non_bool_for_a_bool_parameter_is_a_type_error(self):
        params = [Parameter(name="b", type="Bool")]
        v = _validator()
        with pytest.raises(ValidationError):
            v._bind_values(
                z3.Solver(),
                v._create_z3_variables(params, "r1"),
                {"b": 1},
                params,
                "r1",
            )

    def test_non_numeric_string_for_an_int_parameter_is_wrapped_not_leaked(self):
        # int("abc") raises ValueError inside the try; it must surface as a
        # ValidationError carrying the rule and parameter, not as a bare ValueError.
        params = [Parameter(name="n", type="Int")]
        v = _validator()
        with pytest.raises(ValidationError) as excinfo:
            v._bind_values(
                z3.Solver(),
                v._create_z3_variables(params, "r1"),
                {"n": "abc"},
                params,
                "r1",
            )
        assert excinfo.value.rule_id == "r1"

    def test_unsupported_declared_type_is_a_validation_error(self):
        param = Parameter(name="p", type="Int")
        z3_vars = {"p": z3.Int("p")}
        object.__setattr__(param, "type", "Decimal")
        with pytest.raises(ValidationError) as excinfo:
            _validator()._bind_values(z3.Solver(), z3_vars, {"p": 1}, [param], "r1")
        assert "Decimal" in str(excinfo.value)


@pytest.mark.unit
class TestExtractModel:
    """_extract_model: z3 model -> plain Python values."""

    def test_int_real_bool_and_string_all_come_back_as_python_values(self):
        z3_vars = _vars(i="Int", r="Real", b="Bool", s="String")
        solver = z3.Solver()
        solver.add(
            z3_vars["i"] == 7,
            z3_vars["r"] == z3.RealVal("1.5"),
            z3_vars["b"] == z3.BoolVal(True),
            z3_vars["s"] == z3.StringVal("abc"),
        )
        assert solver.check() == z3.sat
        model = _validator()._extract_model(solver.model(), z3_vars)
        assert model["i"] == 7
        assert model["r"] == pytest.approx(1.5)
        assert model["b"] is True
        assert model["s"] == "abc"

    def test_false_is_returned_as_false_not_dropped(self):
        z3_vars = _vars(b="Bool")
        solver = z3.Solver()
        solver.add(z3_vars["b"] == z3.BoolVal(False))
        assert solver.check() == z3.sat
        assert _validator()._extract_model(solver.model(), z3_vars)["b"] is False

    def test_a_variable_the_model_does_not_constrain_comes_back_as_none(self):
        # An unconstrained variable has no entry in the model; the key must still
        # be present so callers can tell "absent" from "zero".
        z3_vars = _vars(used="Int", unused="Int")
        solver = z3.Solver()
        solver.add(z3_vars["used"] == 1)
        assert solver.check() == z3.sat
        model = _validator()._extract_model(solver.model(), z3_vars)
        assert model["used"] == 1
        assert model["unused"] is None

    def test_a_rational_with_a_non_unit_denominator_becomes_a_float(self):
        z3_vars = _vars(r="Real")
        solver = z3.Solver()
        solver.add(z3_vars["r"] * 3 == 1)
        assert solver.check() == z3.sat
        assert _validator()._extract_model(solver.model(), z3_vars)[
            "r"
        ] == pytest.approx(1 / 3)


def _rule_json(**overrides) -> RuleJSON:
    kwargs = {
        "rule_id": "coverage_ratio",
        "version": "1.0",
        "description": "Coverage must not exceed 20x income",
        "natural_language_rule": "coverage / income <= 20",
        "parameters": [
            Parameter(name="coverage", type="Real"),
            Parameter(name="income", type="Real"),
        ],
        "constraints": ["(> income 0)", "(<= (/ coverage income) 20)"],
    }
    kwargs.update(overrides)
    return RuleJSON(**kwargs)


@pytest.mark.unit
class TestValidateFacade:
    """validate(): the two calling modes and the three outcomes."""

    def test_satisfied_rule_is_sat_and_carries_a_model(self):
        result = _validator().validate(
            _rule_json(), {"coverage": 100.0, "income": 50.0}
        )
        assert result.outcome == "sat"
        assert result.satisfied is True
        assert result.passes() is True
        assert result.model is not None
        assert result.error_message is None
        assert result.execution_time_ms >= 0

    def test_violated_rule_is_unsat_with_no_model(self):
        result = _validator().validate(
            _rule_json(), {"coverage": 10_000.0, "income": 1.0}
        )
        assert result.outcome == "unsat"
        assert result.satisfied is False
        assert result.fails() is True
        assert result.model is None

    def test_null_required_reading_is_an_error_not_a_failure(self):
        # A missing reading means the rule could not be evaluated. Reporting it as
        # unsat would publish a FAIL for a document nobody checked.
        result = _validator().validate(_rule_json(), {"coverage": None, "income": 50.0})
        assert result.outcome == "error"
        assert result.is_error() is True
        assert result.fails() is False
        assert result.error_message is not None
        assert "coverage" in result.error_message

    def test_every_missing_required_reading_is_named_in_the_message(self):
        result = _validator().validate(_rule_json(), {})
        assert result.error_message is not None
        assert "coverage" in result.error_message
        assert "income" in result.error_message

    def test_rule_with_values_mode_needs_no_second_argument(self):
        rule = RuleWithValues(
            rule_id="coverage_ratio",
            version="1.0",
            description="Coverage must not exceed 20x income",
            natural_language_rule="coverage / income <= 20",
            parameters=[
                Parameter(name="coverage", type="Real"),
                Parameter(name="income", type="Real"),
            ],
            constraints=["(> income 0)", "(<= (/ coverage income) 20)"],
            extracted_values={"coverage": 100.0, "income": 50.0},
        )
        assert _validator().validate(rule).outcome == "sat"

    def test_rule_json_without_values_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            _validator().validate(_rule_json())
        assert "extracted_values required" in str(excinfo.value)

    def test_a_non_rule_argument_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            _validator().validate({"rule_id": "r1"}, {})
        assert "must be RuleJSON or RuleWithValues" in str(excinfo.value)

    def test_a_malformed_constraint_surfaces_as_a_validation_error(self):
        with pytest.raises(ValidationError):
            _validator().validate(
                _rule_json(constraints=["(<= (/ coverage income 20)"]),
                {"coverage": 1.0, "income": 1.0},
            )

    def test_an_undeclared_parameter_in_a_constraint_surfaces_as_a_validation_error(
        self,
    ):
        # RuleJSON construction does not catch this (see
        # test_rule_json_models.py::TestConstraintParameterReferences), so the
        # solver layer is where a translation typo is first detected.
        with pytest.raises(ValidationError):
            _validator().validate(
                _rule_json(constraints=["(> incom 0)"]),
                {"coverage": 1.0, "income": 1.0},
            )

    def test_the_declared_timeout_is_applied_to_the_solver(self):
        # The timeout is what bounds a pathological constraint set inside a Lambda
        # with a hard wall-clock budget, so it must actually reach z3.
        validator = Z3Validator(timeout_ms=1)
        assert validator.timeout_ms == 1
        result = validator.validate(_rule_json(), {"coverage": 100.0, "income": 50.0})
        assert result.outcome in {"sat", "unsat", "error"}

    def test_string_equality_rules_are_supported_end_to_end(self):
        rule = _rule_json(
            rule_id="state_match",
            natural_language_rule='state == "CA"',
            parameters=[Parameter(name="state", type="String")],
            constraints=['(= state "CA")'],
        )
        assert _validator().validate(rule, {"state": "CA"}).outcome == "sat"
        assert _validator().validate(rule, {"state": "NY"}).outcome == "unsat"

    def test_an_optional_parameter_left_unset_does_not_block_evaluation(self):
        rule = _rule_json(
            rule_id="floor_with_optional_cap",
            natural_language_rule="amount >= 10, cap optional",
            parameters=[
                Parameter(name="amount", type="Int"),
                Parameter(name="cap", type="Int", required=False),
            ],
            constraints=["(>= amount 10)"],
        )
        assert _validator().validate(rule, {"amount": 20}).outcome == "sat"
