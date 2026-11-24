# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Field paths into nested data, and exact numeric readings.

A light module: standard library only. It must not import
``idp_common.assessment`` or ``idp_common.rule_validation``. Review routing runs in
a Lambda that installs neither of their extras, and it reads field values by the
same path grammar, and compares numbers by the same exactness rule, as rule
validation does. ``idp_common.rule_validation.z3.type_coercion`` re-exports the
numeric names, and ``DataExtractor._extract_path`` walks paths through this module.

**Paths.** Dot notation with list subscripts: ``a.b``, ``rows[2].amount``,
``grid[0][1]``, ``rows[-1]``. :func:`extract_path` returns the value at the path,
or ``None`` when the path does not resolve (a missing key, a non-container on the
way, an index out of range). A malformed path raises :class:`FieldPathError`
whatever the data holds. :func:`set_path` writes a value at a path of the same
grammar, into containers that already exist.

**Numbers.** The contract for ``Int`` is exactness: a reading is accepted only if
it denotes a whole number, whatever type or spelling it arrives in. ``30``,
``30.0``, ``Decimal("30.0")`` and ``"30"`` all read as 30; ``30.9`` is refused.
Truncating toward zero, as ``int()`` does, moves the reading by up to a whole unit,
which is enough to flip any comparison against an integer threshold. A float that
is merely close to a whole number is refused too: nothing in a reading says which
whole number was meant. ``Real`` readings are exact rationals
(:func:`exact_numeric_reading`), or the nearest ``float`` where the value is
recorded as JSON (:func:`coerce_numeric_reading`). Both accept and refuse exactly
the same readings, because the second is written in terms of the first.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from numbers import Rational
from typing import Any, NamedTuple, Optional

NUMERIC_TYPES = ("Int", "Real")

__all__ = [
    "NUMERIC_TYPES",
    "FieldPathError",
    "PathComponent",
    "coerce_numeric_reading",
    "exact_numeric_reading",
    "extract_path",
    "parse_path",
    "set_path",
    "walk_path",
]


class FieldPathError(ValueError):
    """A path that is not well formed. The message names the path."""


class PathComponent(NamedTuple):
    """One dot-separated segment of a path: ``rows[2]`` is ``("rows[2]", "rows", (2,))``."""

    text: str
    key: str
    indices: tuple[int, ...]


# `name`, `name[0]`, `name[0][2]`, `name[-1]`.
#
# The key must be NON-EMPTY. Allowing an empty one let `a.[0].b` match with
# key="", which resolved to a silent miss — indistinguishable from an absent
# value, which is the failure a caller cannot act on.
_SUBSCRIPT_RE = re.compile(r"^(?P<key>[^\[\]]+)(?P<subs>(?:\[-?\d+\])+)$")
# A subscript with no property name in front of it: `[0]`, `[-1]`. Not a legal
# dot-notation segment, so it is a malformed path rather than a key.
_EMPTY_KEY_SUBSCRIPT_RE = re.compile(r"^(?:\[-?\d+\])+$")


def _split_subscripts(component: str, path: str) -> PathComponent:
    """Split ``name[0][2]`` into its key and indexes.

    A component with no subscripts has no indexes, so the ordinary dot-notation
    path is unaffected.

    A component is treated as subscripted ONLY when it is a non-empty key followed
    by one or more bracketed integers. Anything else is an ordinary key, brackets
    and all — so a legitimate data key like ``Amount[USD]`` (plausible in an ERP
    payload) still resolves by plain lookup and still just *misses*. Turning that
    into a hard error would convert "this path never resolved" into "this document
    fails", which is the worse outcome.

    The one shape that IS rejected is a subscript with no key at all (``[0]``,
    ``.[1].x``). That cannot be a real dot-notation segment, so it is unambiguously
    a malformed path — and letting it match with an empty key resolved it to a
    silent miss.
    """
    if _EMPTY_KEY_SUBSCRIPT_RE.match(component):
        raise FieldPathError(
            f"Invalid list subscript in path component {component!r} (in "
            f"path {path!r}): a subscript needs a property name before it, "
            f"e.g. name[0]"
        )
    match = _SUBSCRIPT_RE.match(component)
    if not match or not match.group("subs"):
        return PathComponent(component, component, ())
    subs = match.group("subs")
    indices = tuple(int(value) for value in re.findall(r"\[(-?\d+)\]", subs))
    return PathComponent(component, match.group("key"), indices)


def parse_path(path: Any) -> list[PathComponent]:
    """The components of ``path``, or :class:`FieldPathError` when it is malformed.

    EVERY component is parsed before any data is read, so a malformed path is
    reported as malformed whatever the data happens to contain. Parsing lazily
    during the walk would let an ordinary miss early in the path mask a bad
    segment later in it, and the error would surface or not depending on the
    document.
    """
    if not path or not isinstance(path, str):
        raise FieldPathError(f"Path must be a non-empty string, got: {path}")
    return [_split_subscripts(component, path) for component in path.split(".")]


def walk_path(
    data: Any,
    components: list[PathComponent],
    *,
    on_missing_key: Optional[Callable[[dict, PathComponent, list[str]], None]] = None,
) -> Any:
    """The value ``components`` lead to in ``data``, or ``None`` on a miss.

    A miss is a key that is not there, a value on the way that is not a dict, a
    subscript on something that is not a list, or an index out of range. A negative
    index counts from the end, as in Python.

    ``on_missing_key`` is called with the dict that lacks the key, the component
    and the segments walked before it, so a caller can say why a path stopped
    resolving. It must not raise for an ordinary miss.
    """
    current = data
    walked: list[str] = []
    for component in components:
        if not isinstance(current, dict):
            return None
        if component.key not in current:
            if on_missing_key is not None:
                on_missing_key(current, component, walked)
            return None
        current = current[component.key]
        walked.append(component.text)
        for index in component.indices:
            if not isinstance(current, (list, tuple)):
                return None
            try:
                current = current[index]
            except IndexError:
                return None
    return current


def extract_path(data: Any, path: str) -> Any:
    """The value at ``path`` in ``data``; ``None`` on a miss.

    Raises:
        FieldPathError: when ``path`` is not a well-formed path.
    """
    return walk_path(data, parse_path(path))


def set_path(data: Any, path: str, value: Any) -> bool:
    """Write ``value`` at ``path`` in ``data``. ``False`` when the path does not resolve.

    Nothing is created on the way: every container before the last step must
    exist and be of the right type. The last step then does one of two things:

    * with no subscript (``a.b``), it sets the key on the dict that holds it,
      adding the key when it is absent;
    * with subscripts (``rows[1]``, ``grid[0][1]``), every index must address an
      existing element, and the last index replaces that element. A negative
      index counts from the end, as in Python.

    On any miss the answer is ``False`` and ``data`` is unchanged.

    Raises:
        FieldPathError: when ``path`` is not a well-formed path.
    """
    *parents, last = parse_path(path)
    if not last.indices:
        container = walk_path(data, parents)
        if not isinstance(container, dict):
            return False
        container[last.key] = value
        return True
    *outer, index = last.indices
    container = walk_path(
        data, [*parents, PathComponent(last.text, last.key, tuple(outer))]
    )
    if not isinstance(container, list):
        return False
    try:
        container[index] = value
    except IndexError:
        return False
    return True


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
