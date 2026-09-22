# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the constraint vocabulary shared by the solver and RuleJSON.

`RuleJSON` rejects a constraint that references an undeclared name at
construction time, and `Z3Validator` rejects the same thing at solve time. The
defect those two checks together replace was a construction-time check that
rejected nothing at all (GitHub issue #1058), and the way such a check goes wrong
a second time is by drifting from the solver's grammar: too strict and it refuses
a rule that would have worked, too lax and it is decoration again.

So what these tests are about is the *agreement*, not either side's behaviour on
its own. Three properties carry it:

- one tokeniser, so `"condo"` inside a string literal is one token on both sides;
- one operator vocabulary, which `_apply_smt_operator` gates on rather than
  mirrors, so a name it dispatches but `OPERATORS` omits is refused by the solver
  too and shows up here as a failure;
- the residual is stated rather than assumed: the shapes the construction check
  passes to the solver are listed, and each is asserted to be rejected there.

The last test runs in a subprocess with `z3` unimportable, which is the state of
the configuration-resolver Lambda that builds a `RuleJSON` on every "Generate
RuleJSON" click. A construction-time check that needed the solver would break that
Lambda, and nothing in this suite would have noticed.
"""

import os
import subprocess
import sys

import pytest
import z3

from idp_common.rule_validation.z3.smt_grammar import (
    BOOLEAN_LITERALS,
    OPERATORS,
    constraint_problems,
    is_identifier,
    tokenize,
)
from idp_common.rule_validation.z3.type_coercion import exact_numeric_reading
from idp_common.rule_validation.z3.z3_validator import Z3Validator


def _sample_args(op: str):
    """Arguments of the arity and sort each operator needs to dispatch."""
    if op == "not":
        return [z3.BoolVal(True)]
    if op in ("and", "or", "implies", "=>"):
        return [z3.BoolVal(True), z3.BoolVal(False)]
    if op == "ite":
        return [z3.BoolVal(True), z3.IntVal(1), z3.IntVal(2)]
    return [z3.IntVal(4), z3.IntVal(2)]


@pytest.mark.unit
class TestTokenize:
    """tokenize(): the one tokeniser both sides read."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("(>= x 10)", ["(", ">=", "x", "10", ")"]),
            ("(not(> x 1))", ["(", "not", "(", ">", "x", "1", ")", ")"]),
            ("x", ["x"]),
            ("", []),
        ],
    )
    def test_tokens_are_split_on_whitespace_and_parentheses(self, source, expected):
        assert tokenize(source) == expected

    def test_a_quoted_string_stays_one_token_with_its_quotes(self):
        assert tokenize('(= name "John Doe")') == [
            "(",
            "=",
            "name",
            '"John Doe"',
            ")",
        ]

    def test_the_validator_tokenises_through_this_function(self):
        # The delegation is the point: two tokenisers would be two grammars. The
        # source has to be one the two could disagree about, so the quoted string
        # contains a space and a parenthesis -- a naive split on whitespace and
        # parentheses gets `"John` and `Doe` and a stray `(`, and a construction
        # check built on that reads `Doe` as an undeclared parameter.
        source = '(and (= name "John Doe (Jr)") (>= area 1500))'
        assert Z3Validator()._tokenize_smt(source) == tokenize(source)
        assert '"John Doe (Jr)"' in tokenize(source)


@pytest.mark.unit
class TestIsIdentifier:
    """is_identifier(): which tokens are claims about a parameter name."""

    @pytest.mark.parametrize("token", ["income", "_x", "x1", "nan", "inf"])
    def test_name_shaped_tokens(self, token):
        assert is_identifier(token) is True

    @pytest.mark.parametrize(
        "token", ["0.05", "-3", "1.5e3", '"condo"', "3x", "1/3", ">=", "", "x y"]
    )
    def test_everything_else(self, token):
        assert is_identifier(token) is False


@pytest.mark.unit
class TestOperatorVocabularyAgreesWithTheSolver:
    """
    OPERATORS and Z3Validator._apply_smt_operator name the same operators.

    The method gates on the set, so this is not a comparison of two lists that
    could drift: a name in the set the method cannot handle raises here, and a
    name the method handles that is missing from the set is refused by the solver.
    """

    @pytest.mark.parametrize("op", sorted(OPERATORS))
    def test_every_listed_operator_is_dispatched(self, op):
        # No exception, and in particular not "Unsupported operator".
        assert Z3Validator()._apply_smt_operator(op, _sample_args(op)) is not None

    @pytest.mark.parametrize("op", ["sqrt", "abs", "xor", "concat", "income"])
    def test_a_name_outside_the_vocabulary_is_unsupported(self, op):
        with pytest.raises(ValueError, match="Unsupported operator"):
            Z3Validator()._apply_smt_operator(op, [z3.IntVal(1), z3.IntVal(2)])

    def test_the_boolean_literals_are_the_ones_the_atom_parser_knows(self):
        validator = Z3Validator()
        for literal in BOOLEAN_LITERALS:
            assert z3.is_true(validator._parse_smt_atom(literal, {})) or z3.is_false(
                validator._parse_smt_atom(literal, {})
            )


@pytest.mark.unit
class TestConstraintProblems:
    """constraint_problems(): what it reports, and what it leaves to the solver."""

    declared = ("coverage", "income")

    @pytest.mark.parametrize(
        "constraint",
        [
            "(<= (/ coverage income) 20)",
            "(and (>= income 0) (<= income 1.5e3))",
            "(=> (> income 0) (> coverage 0))",
            "(distinct coverage income)",
            "(mod income 2)",
            "(= income (ite (>= coverage 600) 0.05 0.02))",
            "income",
        ],
    )
    def test_a_resolvable_constraint_has_no_problems(self, constraint):
        assert constraint_problems(constraint, self.declared) == []

    def test_a_string_literal_is_not_read_as_a_reference(self):
        assert constraint_problems('(= coverage "condo")', self.declared) == []

    def test_an_undeclared_name_is_reported(self):
        problems = constraint_problems("(> incom 0)", self.declared)
        assert problems == ["'incom' is not a declared parameter"]

    def test_each_distinct_name_is_reported_once(self):
        problems = constraint_problems(
            "(and (> incom 0) (> incom 1) (> asssets 0))", self.declared
        )
        assert problems == [
            "'incom' is not a declared parameter",
            "'asssets' is not a declared parameter",
        ]

    def test_an_unsupported_head_is_reported(self):
        problems = constraint_problems("(sqrt income)", self.declared)
        assert len(problems) == 1
        assert problems[0].startswith("'sqrt' is not a supported operator")

    @pytest.mark.parametrize(
        "constraint",
        [
            "(> 3x 0)",  # neither a name nor a numeral
            "(> 1/3 0)",  # an expression, not a literal
            "(<= (/ coverage income 20)",  # unbalanced
            "(> income)",  # wrong arity
            "(> income 0) (> coverage 0)",  # two expressions in one constraint
        ],
    )
    def test_the_shapes_left_to_the_solver_are_not_reported_here(self, constraint):
        # This is the documented residual. Each of these is rejected at solve
        # time, which the companion tests in test_z3_validator_smt.py assert; the
        # value of pinning it here is that widening the check stays deliberate.
        assert constraint_problems(constraint, self.declared) == []

    @pytest.mark.parametrize("spelling", ["nan", "inf", "infinity", "Infinity"])
    def test_a_non_finite_spelling_is_refused_on_both_sides(self, spelling):
        # The one place identifier shape and numeral spelling overlap. This check
        # reports the token as an undeclared name; the numeric contract every
        # reading goes through refuses the same value, because Z3 has no sort for
        # one. Asserting both here is what would catch them diverging.
        assert constraint_problems(f"(> income {spelling})", self.declared) == [
            f"'{spelling}' is not a declared parameter"
        ]
        with pytest.raises(ValueError):
            exact_numeric_reading(spelling, "Real")


@pytest.mark.unit
class TestConstructionWithoutTheSolverInstalled:
    """
    RuleJSON validates constraints where z3-solver is not installed.

    The configuration-resolver Lambda builds a RuleJSON from model output on every
    "Generate RuleJSON" click, and its layer carries idp_common without the
    rule_validation extra — so without z3. That is why
    idp_common.rule_validation.z3 imports the solver-dependent modules lazily, and
    why the construction-time check is written against a tokeniser rather than
    against the parser.
    """

    PROBE = """
import sys

# Anything that reaches `import z3` from here raises, the same way it would on a
# layer that does not ship the solver.
sys.modules["z3"] = None

from idp_common.rule_validation.z3.models import Parameter, RuleJSON

kwargs = dict(
    rule_id="r1",
    version="1.0",
    description="d",
    natural_language_rule="coverage / income <= 20",
    parameters=[
        Parameter(name="coverage", type="Real"),
        Parameter(name="income", type="Real"),
    ],
)

good = RuleJSON(constraints=["(<= (/ coverage income) 20)"], **kwargs)
assert good.rule_id == "r1"

try:
    RuleJSON(constraints=["(> incom 0)"], **kwargs)
except ValueError as e:
    assert "incom" in str(e), str(e)
else:
    raise AssertionError("an undeclared reference was accepted")

assert "z3.z3" not in sys.modules, "the solver was imported after all"
print("OK")
"""

    def test_a_rule_is_accepted_and_a_misspelling_refused_with_no_z3(self):
        import idp_common

        package_root = os.path.dirname(os.path.dirname(idp_common.__file__))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [package_root, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        completed = subprocess.run(  # nosec B603 - fixed interpreter, literal probe
            [sys.executable, "-c", self.PROBE],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        assert "OK" in completed.stdout
