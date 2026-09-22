# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Coercion of an extracted reading to a declared numeric parameter type.

Rule validation reaches the solver by three routes. Path-based extraction
(`DataExtractor.extract_values`) reads values out of a structured document at
declared paths; LLM extraction (`RuleTranslator.extract_values_with_llm`) asks a
model for them, and is both the default for a rule with no `path_mappings` and
the fallback whenever path-based extraction fails; and the orchestrator's
production fact-extraction call
(`RuleValidationOrchestratorService._extract_z3_values_from_facts`) hands parsed
JSON straight to `Z3Validator.validate`. All three end up bound to a Z3 variable
by `Z3Validator._bind_values`.

Every route goes through this module for the numeric types, so that "what counts
as a valid `Int`" has exactly one answer rather than one per route. `Bool` and
`String` keep their per-route conversions, which already agree on everything
except an integer read for a `Bool`.

The contract for `Int` is **exactness**: a reading is accepted only if it
denotes a whole number, whatever type or spelling it arrives in. `30`, `30.0`,
`Decimal("30.0")` and `"30"` all bind to 30; `30.9`, `Decimal("30.9")` and
`"30.9"` are refused. Truncating toward zero, as `int()` does, moves the reading
by up to a whole unit, which is enough to flip the verdict of any rule with an
integer threshold — and to flip it in either direction, depending on the
comparator, so no amount of rounding makes it sound. Refusing is what a caller
can act on: the rule reports that it could not be evaluated instead of
confidently reporting the opposite of the truth. See GitHub issue #1057.

A float that is merely *close* to a whole number, such as ``29.999999999999996``,
is likewise refused rather than nudged. Nothing in a reading says which whole
number the model meant, and guessing is the behaviour this module exists to
remove.

`Real` readings are **not** rounded to a double on the way to the solver. There are
two entry points for that reason: `exact_numeric_reading` returns the exact
rational and is what the binding site and the constraint parser use, and
`coerce_numeric_reading` returns the `float` that goes into `extracted_values`,
which is serialised to JSON in the result. They accept and refuse exactly the same
readings, because the second is written in terms of the first.
"""

from decimal import Decimal
from fractions import Fraction
from numbers import Rational
from typing import Any

NUMERIC_TYPES = ("Int", "Real")

__all__ = ["NUMERIC_TYPES", "coerce_numeric_reading", "exact_numeric_reading"]


def exact_numeric_reading(value: Any, expected_type: str) -> int | Fraction:
    """
    The reading as an exact number, for binding to a solver variable.

    `Real` comes back as a `Fraction` rather than a `float` because `z3.RealVal`
    accepts one and Z3's Real sort is exact rationals. Collapsing the reading to a
    double first would be its own wrong-verdict path: `Decimal("0.1000000000000000000001")`
    becomes `0.1` as a double, and `(= n 0.1)` then reports a Pass for a reading
    that is not equal to the literal. A double holds ~17 significant digits and a
    `Decimal` read from DynamoDB carries up to 38. `Z3Validator._parse_smt_atom`
    parses the constraint's own literals through here for the same reason — an
    exact reading compared against a collapsed literal is wrong in the other
    direction.

    Args:
        value: The reading, as extracted. `int`, `float`, `Decimal`, `Fraction`
            and numeric strings are understood.
        expected_type: `"Int"` or `"Real"`.

    Returns:
        An `int` for `"Int"`, an exact `Fraction` for `"Real"`.

    Raises:
        ValueError: If the reading does not denote a value of that type, or
            cannot be represented in it without losing information. Callers wrap
            this in their own error type (`ExtractionError`, `ValidationError`).
    """
    if expected_type not in NUMERIC_TYPES:
        raise ValueError(f"Not a numeric parameter type: {expected_type}")

    exact = _exact_value(value, expected_type)

    if expected_type == "Int":
        if exact.denominator != 1:
            raise ValueError(
                f"{_describe(value)} cannot be converted to Int without loss"
            )
        return int(exact)

    # Refused here and not only in `coerce_numeric_reading`, so that both agree:
    # every route records the reading alongside the verdict, and a magnitude no
    # float can hold has no place to be recorded.
    _as_float(exact, value)
    return exact


def coerce_numeric_reading(value: Any, expected_type: str) -> int | float:
    """
    The reading in its declared type, for recording alongside the verdict.

    Accepts and refuses exactly what `exact_numeric_reading` does — it is the same
    code — and differs only in representation: a `Real` comes back as a `float`,
    because the value is stored in `extracted_values` and serialised to JSON in
    the result. Use `exact_numeric_reading` for anything handed to the solver.

    Args:
        value: The reading, as extracted.
        expected_type: `"Int"` or `"Real"`.

    Returns:
        An `int` for `"Int"`, a `float` for `"Real"`.

    Raises:
        ValueError: As `exact_numeric_reading`.
    """
    exact = exact_numeric_reading(value, expected_type)
    if isinstance(exact, int):
        return exact
    return _as_float(exact, value)


def _as_float(exact: Fraction, value: Any) -> float:
    """The nearest double to an exact reading, or a refusal if there is none."""
    try:
        return float(exact)
    except OverflowError:
        raise ValueError(
            f"{_describe(value)} is too large to represent as a Real"
        ) from None


def _exact_value(value: Any, expected_type: str) -> Fraction:
    """
    The exact rational value of a reading.

    Going through `Fraction` rather than `int()` or `float()` is what makes the
    exactness check work for every numeric type at once: `Fraction` is exact for
    `int`, `float`, `Decimal` and `Fraction` alike, so `denominator == 1` answers
    "is this a whole number" without a per-type special case. `int()` would
    truncate all four.
    """
    # bool is a subclass of int, so True would otherwise bind as 1 and be
    # compared against a numeric threshold. A yes/no reading is not a number.
    if isinstance(value, bool):
        raise ValueError(
            f"Bool value {value} cannot be converted to {expected_type} "
            f"(declare the parameter as Bool)"
        )

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"Empty string cannot be converted to {expected_type}")
        # Decimal, not float: it is exact for a decimal numeral, so "30.9" is
        # refused for its own fractional part rather than for a binary
        # approximation of it. It also rejects "1/3", which Fraction would
        # accept -- a rule's reading is a numeral, not an expression.
        try:
            value = Decimal(text)
        except ArithmeticError:
            raise ValueError(
                f"Cannot convert string {text!r} to {expected_type}"
            ) from None
    # Anything `Fraction` is exact for, and nothing else. A complex number is a
    # `numbers.Number` and is not one of these.
    if not isinstance(value, (int, float, Decimal, Rational)):
        raise ValueError(f"Cannot convert {type(value).__name__} to {expected_type}")

    if isinstance(value, (int, Rational)):
        return Fraction(value.numerator, value.denominator)

    # float and Decimal both expose their exact integer ratio, and raise for a
    # value that has none: infinity and NaN. They are refused here rather than
    # handed to the solver, which has neither.
    try:
        numerator, denominator = value.as_integer_ratio()
    except (ArithmeticError, ValueError):
        raise ValueError(
            f"{_describe(value)} cannot be converted to {expected_type}"
        ) from None
    return Fraction(numerator, denominator)


def _describe(value: Any) -> str:
    """Name a rejected reading by type and value, so the message says which one."""
    return f"{type(value).__name__} value {value}"
