# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sample Calculator Agent implementation for development purposes.

The tool is defined here rather than imported from ``strands_tools`` because
that distribution declares ``pillow>=12.1.1,<13.0.0`` as a core, unconditional
dependency in every published release. Declaring it in any extra of this package
therefore caps Pillow below 13.0.0 across the whole lockfile and leaves seven
Pillow advisories — including a CRITICAL heap overflow in the JPEG decoder —
unfixable on the paths that render untrusted customer documents. A locally
defined tool costs this sample nothing: showing how to write your own tool is
the shape a developer copying this file needs anyway.
"""

import ast
import logging
import operator

import boto3
import strands

from ..common.config import DEFAULT_AGENT_MODEL_ID
from ..common.strands_bedrock_model import create_strands_bedrock_model

logger = logging.getLogger(__name__)

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

#: Ceiling on the magnitude of every integer the walk produces, in bits — about
#: 1,233 decimal digits.
#:
#: What this bounds, exactly: the result of each individual arithmetic operation,
#: checked after that operation, plus a pre-filter that refuses an over-large
#: ``**`` before it is computed. Because every operand is already under the
#: ceiling when an operation runs, a sum, difference, product, quotient or
#: modulus cannot exceed roughly twice it, so checking afterwards is cheap and
#: sufficient for those. ``**`` is the exception and needs the pre-filter: its
#: result grows with the exponent rather than with the operand sizes.
#:
#: What this does NOT bound: float arithmetic, which is left to IEEE range and
#: surfaces as ``OverflowError``; and the cost of parsing the expression STRING,
#: which is whatever the caller passes in and surfaces as ``SyntaxError``,
#: ``RecursionError`` or ``MemoryError`` from ``ast.parse``.
#:
#: Bounding the running magnitude rather than the exponent operand is the part
#: that matters, and a per-operation exponent cap is not a substitute: with one,
#: ``(2**1000)**1000`` passes — every individual exponent is exactly 1000 — and
#: ``((2**1000)**1000)**1000`` costs 3.6s of CPU and ~400MB before anything
#: refuses it, at which point what refuses it is CPython's 4,300-digit
#: int-to-string limit, applied by ``str()`` after the whole cost is paid.
_MAX_RESULT_BITS = 4096


def _refuse_if_too_large(value):
    """Return ``value``, or raise if it is an integer above the bit ceiling."""
    if isinstance(value, int) and value.bit_length() > _MAX_RESULT_BITS:
        raise ValueError(f"result magnitude above {_MAX_RESULT_BITS} bits is refused")
    return value


def _power_would_be_too_large(left, right) -> bool:
    """True when ``left ** right`` is certainly over the ceiling, without computing it.

    Uses a LOWER bound on the result's bit length, so it never refuses a power
    that is actually in range. For integers, ``left ** right`` occupies
    ``floor(right * log2(abs(left))) + 1`` bits, and ``left.bit_length() - 1``
    is exactly ``floor(log2(abs(left)))`` — so the expression below is at or
    below the true size. Anything it lets through is therefore computed, but is
    at most about twice the ceiling in size (the bound understates by fewer than
    ``right`` bits, and is itself at least ``right`` once the base needs two or
    more bits), which the exact check afterwards then rejects. A base of 0, 1 or
    -1 occupies one bit and yields 0, 1 or ±1 for any exponent, so it is cheap
    whatever the exponent is.
    """
    if not (isinstance(left, int) and isinstance(right, int)) or right < 0:
        return False  # a float result: IEEE range applies, not this ceiling
    return (left.bit_length() - 1) * right + 1 > _MAX_RESULT_BITS


@strands.tool
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression and return the result.

    Args:
        expression: An arithmetic expression over numbers and the operators
            ``+``, ``-``, ``*``, ``/``, ``//``, ``%`` and ``**``, optionally
            grouped with parentheses. Integer results are bounded by
            ``_MAX_RESULT_BITS``.

    Returns:
        The result as a string, or a message describing why it could not be
        evaluated.
    """

    # Walked node by node rather than handed to eval(): the expression arrives
    # from model output, so nothing in it is trusted. Anything that is not a
    # number or one of the operators above — a call, a name, a subscript — has
    # no branch here and raises instead of being evaluated.
    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return _refuse_if_too_large(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and _power_would_be_too_large(left, right):
                raise ValueError(
                    f"result magnitude above {_MAX_RESULT_BITS} bits is refused"
                )
            return _refuse_if_too_large(_BINARY_OPS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _refuse_if_too_large(
                _UNARY_OPS[type(node.op)](evaluate(node.operand))
            )
        raise ValueError(f"unsupported expression element: {type(node).__name__}")

    try:
        return str(evaluate(ast.parse(expression, mode="eval").body))
    except (
        SyntaxError,
        ValueError,
        TypeError,
        ZeroDivisionError,
        OverflowError,
        RecursionError,
        MemoryError,
    ) as e:
        # MemoryError is in the tuple for the message shape, not to keep the
        # agent alive: strands' DecoratedFunctionTool.stream catches `Exception`
        # (which MemoryError and RecursionError both subclass) and yields a
        # `status: "error"` tool result reading `Error: <type> - <message>`, so
        # an escaping exception is already surfaced to the model rather than
        # ending the loop. Catching it here is what makes an over-large
        # expression read the same as every other refusal instead of arriving as
        # a bare `MemoryError` the model has to interpret.
        return f"Could not evaluate {expression!r}: {e}"


def create_sample_calculator_agent(
    session: boto3.Session,
    **kwargs,
) -> strands.Agent:
    """
    Create a minimal sample calculator agent with calculator tool.

    Args:
        session: Boto3 session for AWS operations
        **kwargs: Additional arguments

    Returns:
        strands.Agent: Configured Strands agent instance
    """
    model_id = DEFAULT_AGENT_MODEL_ID

    # Create Bedrock model
    model = create_strands_bedrock_model(model_id=model_id, session=session)

    # Create and return agent with calculator tool
    return strands.Agent(model=model, tools=[calculator])
