# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The sample agent's calculator: plain arithmetic, evaluated without ``eval``.

The expression is parsed with :mod:`ast` and only numeric literals, the usual
arithmetic operators and parentheses are accepted, so a model-written string can
never run code. Exponents are bounded because ``9 ** 9 ** 9`` is valid arithmetic
that would otherwise hold the Lambda until it times out.
"""

from __future__ import annotations

import ast
import operator
from typing import Callable, Union

from strands import tool

Number = Union[int, float]

#: Largest exponent accepted, and the largest operand it may be applied to.
MAX_EXPONENT = 1000
MAX_POWER_BASE = 10**6

_BINARY: dict[type, Callable[[Number, Number], Number]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type, Callable[[Number], Number]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def evaluate(expression: str) -> Number:
    """The value of an arithmetic ``expression``; ``ValueError`` for anything else."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"not an arithmetic expression: {expression!r}") from exc
    return _eval(tree.body)


def _eval(node: ast.AST) -> Number:
    value = getattr(node, "value", None) if isinstance(node, ast.Constant) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (
            abs(right) > MAX_EXPONENT or abs(left) > MAX_POWER_BASE
        ):
            raise ValueError("the power is too large to compute")
        try:
            return _BINARY[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ValueError("division by zero") from exc
    raise ValueError(f"unsupported element: {type(node).__name__}")


@tool
def calculator(expression: str) -> str:
    """Evaluate an arithmetic expression and return the result.

    Supports numbers, + - * / // % ** and parentheses, e.g. ``(2 + 3) * 4``.

    Args:
        expression: The arithmetic expression to evaluate.
    """
    try:
        return str(evaluate(expression))
    except ValueError as exc:
        return f"Error: {exc}"
