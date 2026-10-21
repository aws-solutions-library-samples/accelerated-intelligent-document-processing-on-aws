# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the sample calculator agent.

This agent is a developer reference rather than a production path: its
registration in `agents/factory/registry.py` is commented out on purpose. It is
tested anyway because a reference that does not construct is worse than no
reference — the first thing anyone does with it is copy it, and a broken copy
teaches the wrong shape.

What is asserted is construction, not behaviour: that it reads the declared
default model id rather than a literal of its own (the class of defect that
[#1049] was about, where seven agent modules each carried a private copy of a
retired model id), that the caller's boto3 session is the one the model is built
with, and that the calculator tool is attached. No Bedrock call is made.

The tool itself is defined in the module rather than imported from
``strands_tools``, so these tests run unconditionally — there is no
``importorskip`` and no skip marker, and no extra has to be installed for the
module to be collected. The arithmetic the tool does is cheap to assert, so it
is asserted here too rather than left to a reader's assumption.
"""

import ast
from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.common.config import DEFAULT_AGENT_MODEL_ID
from idp_common.agents.sample_calculator import agent as sample_calculator
from idp_common.agents.sample_calculator.agent import (
    calculator,
    create_sample_calculator_agent,
)


@pytest.mark.unit
class TestCreateSampleCalculatorAgent:
    def _build(self, session=None):
        session = session or MagicMock(name="boto3-session")
        with (
            patch(
                "idp_common.agents.sample_calculator.agent.create_strands_bedrock_model"
            ) as make_model,
            patch(
                "idp_common.agents.sample_calculator.agent.strands.Agent"
            ) as agent_cls,
        ):
            result = create_sample_calculator_agent(session)
        return result, make_model, agent_cls, session

    def test_it_returns_the_constructed_agent(self):
        result, _, agent_cls, _ = self._build()
        assert result is agent_cls.return_value

    def test_the_model_comes_from_the_declared_default_not_a_local_literal(self):
        # A literal here would be a model id no gate can see, which is how a
        # retired model survived in seven fallback paths.
        _, make_model, _, _ = self._build()
        assert make_model.call_args.kwargs["model_id"] == DEFAULT_AGENT_MODEL_ID

    def test_the_callers_session_is_used_to_build_the_model(self):
        # The session carries the region and credentials, so building the model
        # from anything else would silently target another account.
        session = MagicMock(name="caller-session")
        _, make_model, _, _ = self._build(session)
        assert make_model.call_args.kwargs["session"] is session

    def test_the_calculator_tool_is_attached(self):
        _, _, agent_cls, _ = self._build()
        assert agent_cls.call_args.kwargs["tools"] == [calculator]

    def test_extra_keyword_arguments_are_accepted_and_ignored(self):
        # The signature takes **kwargs to match the factory's calling convention;
        # passing one must not reach strands.Agent as an unexpected argument.
        with (
            patch(
                "idp_common.agents.sample_calculator.agent.create_strands_bedrock_model"
            ),
            patch(
                "idp_common.agents.sample_calculator.agent.strands.Agent"
            ) as agent_cls,
        ):
            create_sample_calculator_agent(MagicMock(), config={"unused": True})
        assert set(agent_cls.call_args.kwargs) == {"model", "tools"}


@pytest.mark.unit
class TestCalculatorTool:
    """The tool the sample attaches, exercised directly.

    The expression reaches this tool from model output, so the one property that
    matters beyond arithmetic is that nothing outside arithmetic is evaluated —
    a call, a name or an import must come back as a message, never as an effect.
    """

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("2+2", "4"),
            ("7*(3-1)", "14"),
            ("2**10", "1024"),
            ("10/4", "2.5"),
            ("10//4", "2"),
            ("7%3", "1"),
            ("-5+1", "-4"),
        ],
    )
    def test_it_evaluates_arithmetic(self, expression, expected):
        assert calculator(expression) == expected

    @pytest.mark.parametrize(
        "expression",
        [
            '__import__("os").system("true")',  # a call of any kind
            "os.getcwd()",  # a name the walker never resolves
            "[1, 2][0]",  # a subscript
            "1 if True else 2",  # a conditional
            "import os",  # not an expression at all
            "1+",  # malformed
        ],
    )
    def test_it_refuses_anything_that_is_not_arithmetic(self, expression):
        # The message shape matters as much as the refusal: the agent gets told
        # why, rather than receiving a traceback or a silent empty string.
        result = calculator(expression)
        assert result.startswith("Could not evaluate ")
        assert repr(expression) in result

    def test_a_division_by_zero_is_reported_rather_than_raised(self):
        assert calculator("1/0") == "Could not evaluate '1/0': division by zero"

    # The magnitude ceiling. These assert the REFUSAL and never a duration: a
    # timing threshold is flaky on a loaded runner, and the refusal is the
    # property anyway. What a lost ceiling would look like here is the suite
    # slowing down or being killed, not this file reporting a failure — the
    # unbounded forms below cost seconds of CPU and hundreds of MB each.
    _REFUSED = "result magnitude above {bits} bits is refused"

    @pytest.mark.parametrize(
        "expression",
        [
            # A single huge exponent — the case a per-operation exponent cap
            # also caught.
            "2**1000000000",
            # Left-nested chaining, which a per-operation exponent cap did NOT
            # catch: every individual exponent here is exactly 1000, so such a
            # cap never fires. The second of these was measured at 3.6s of CPU
            # and ~400MB, and the third at MemoryError under a 2GB limit.
            "(2**1000)**1000",
            "((2**1000)**1000)**1000",
            "(((2**1000)**1000)**1000)**1000",
            # The exponent itself being an expression, so the operand a cap
            # would read is computed rather than literal.
            "2**(2**1000)",
            # Multiplication, which an exponent cap does not address at all.
            "(2**2000)*(2**2000)*(2**2000)",
        ],
    )
    def test_an_over_large_result_is_refused(self, expression):
        expected = self._REFUSED.format(bits=sample_calculator._MAX_RESULT_BITS)
        assert (
            calculator(expression) == f"Could not evaluate {expression!r}: {expected}"
        )

    def test_the_largest_in_range_result_still_evaluates(self):
        # The ceiling is inclusive, so the refusal cannot be the whole story,
        # and this is the case that fails if the pre-filter over-estimates.
        bits = sample_calculator._MAX_RESULT_BITS
        assert calculator(f"2**{bits - 1}") == str(2 ** (bits - 1))
        assert (2 ** (bits - 1)).bit_length() == bits

    def test_one_bit_past_the_ceiling_is_refused(self):
        bits = sample_calculator._MAX_RESULT_BITS
        expected = self._REFUSED.format(bits=bits)
        assert calculator(f"2**{bits}") == (
            f"Could not evaluate '2**{bits}': {expected}"
        )

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [("1**1000000000", "1"), ("0**1000000000", "0"), ("(-1)**1000000001", "-1")],
    )
    def test_a_base_of_one_bit_is_allowed_whatever_the_exponent(
        self, expression, expected
    ):
        # 0, 1 and -1 occupy one bit and yield 0, 1 or ±1 for any exponent, so
        # the result is in range and costs nothing. Refusing these would be the
        # pre-filter reading the exponent rather than the result size.
        assert calculator(expression) == expected

    def test_a_float_result_is_left_to_ieee_range(self):
        # Floats are outside what the bit ceiling bounds, which the module says.
        # The refusal still arrives as a message rather than an exception.
        assert calculator("2**-3") == "0.125"
        assert calculator("2.0**10000").startswith("Could not evaluate '2.0**10000': ")

    def test_a_memory_error_is_reported_rather_than_raised(self, monkeypatch):
        # MemoryError is reachable from a large enough intermediate, but
        # provoking a real one would make the test depend on the host's memory.
        # Raising it from an operation asserts the same thing: that it is in the
        # caught tuple, so an over-large expression reads like every other
        # refusal instead of arriving as a bare MemoryError.
        monkeypatch.setitem(
            sample_calculator._BINARY_OPS,
            ast.Add,
            lambda a, b: (_ for _ in ()).throw(MemoryError("out of memory")),
        )
        assert calculator("1+1") == "Could not evaluate '1+1': out of memory"

    # ⚠️ There is deliberately NO assertion here that ``calculator`` is a
    # ``DecoratedFunctionTool`` — that ``@strands.tool`` actually ran — and the
    # reason is worth knowing before adding one, because every spelling of it
    # passes alone and fails in a full run.
    # tests/unit/extraction/test_integrated_confidence_strategy.py,
    # test_map_table_to_schema.py and test_finalize_table_extraction.py each
    # rebind the GLOBAL ``strands.tool`` to the identity at module scope and
    # never restore it, so in a session that imports one of them the decorator
    # is a passthrough from then on. (tests/unit/extraction/test_tool_writeback.py
    # patches the bound name on its own subject instead and restores it per
    # test, and its comment is where that hazard is written down.) Under xdist
    # the state at THIS module's import time and the state at assertion time are
    # independently unpredictable, which was measured both ways: asserting the
    # type literal fails when the subject imported under the passthrough, and
    # comparing against a probe decorated in-test fails in the opposite
    # ordering, where the subject is a real tool and the probe is not.
    # The fix belongs in those three modules rather than in an assertion here.
    #
    # What is covered instead is everything that does not depend on the
    # decorator having run: that the tool object the agent is handed is this
    # module's ``calculator``
    # (TestCreateSampleCalculatorAgent.test_the_calculator_tool_is_attached),
    # and the arithmetic and the refusals above — all of which hold identically
    # under the real decorator and under the passthrough.
