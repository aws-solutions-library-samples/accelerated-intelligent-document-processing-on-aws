# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S-FEEL unary tests: the cell language of a decision table's input entries.

A subset of the DMN 1.5 unary-test grammar, parsed by a small recursive-descent
parser::

    unary_tests    ::= "-" | "not" "(" positive_tests ")" | positive_tests
    positive_tests ::= positive_test ( "," positive_test )*
    positive_test  ::= "null" | endpoint | op endpoint | interval
    op             ::= "<" | "<=" | ">" | ">=" | "=" | "!="
    interval       ::= ( "[" | "(" | "]" ) endpoint ".." endpoint ( "]" | ")" | "[" )
    endpoint       ::= number | string | "true" | "false" | 'date("' YYYY-MM-DD '")'
    number         ::= "-"? DIGIT+ ( "." DIGIT+ )?
    string         ::= '"' ( [^"\\] | '\"' | '\\' )* '"'

Evaluation is three-valued: a test is ``True``, ``False`` or ``None`` (unknown).

- ``=`` and ``!=`` are always boolean. ``null`` equals only ``null``, and a value
  of another type than the endpoint is simply not equal to it.
- An ordering test (``<``, ``<=``, ``>``, ``>=``) or an interval is ``None`` on
  ``null`` and on a value of another type than its endpoints.
- A list of tests is ``True`` when any test is, else ``None`` when any test is
  unknown, else ``False``. ``not(...)`` of ``None`` is ``None``.
- A number is compared exactly (``idp_common.field_paths.exact_numeric_reading``):
  an int, a float, a ``Decimal`` or a numeric string is a number, a boolean never.
- A date endpoint compares with a ``date``, with an ISO ``YYYY-MM-DD`` string and
  with the date part of an ISO date-time string or a ``datetime``.
- Strings are case-sensitive.

A bare endpoint means "equal to", so ``5`` and ``= 5`` parse to the same tests, as
do ``not(5)`` and ``!= 5``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache
from typing import Any, Optional, Union

from idp_common.field_paths import exact_numeric_reading

from .models import DecisionTableError, UnaryTestSyntaxError

NUMBER = "number"
STRING = "string"
BOOLEAN = "boolean"
DATE = "date"

_ORDERING_OPS = ("<=", ">=", "<", ">")
_EQUALITY_OPS = ("!=", "=")
#: Two-character operators first, so ``<=`` is not read as ``<`` then ``=``.
_OPS = ("<=", ">=", "!=", "<", ">", "=")

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATE_CALL_RE = re.compile(r'date\s*\(\s*"(\d{4}-\d{2}-\d{2})"\s*\)')


@dataclass(frozen=True)
class Endpoint:
    """A literal a value is compared with."""

    kind: str
    value: Any
    #: The literal as written, so a test can be printed back unchanged.
    text: str = field(compare=False, default="")


@dataclass(frozen=True)
class Comparison:
    """``op endpoint``; a bare endpoint is ``=``."""

    op: str
    endpoint: Endpoint


@dataclass(frozen=True)
class Interval:
    low: Endpoint
    high: Endpoint
    low_closed: bool
    high_closed: bool


@dataclass(frozen=True)
class NullTest:
    """``null``: true when the value is null."""


PositiveTest = Union[Comparison, Interval, NullTest]


@dataclass(frozen=True)
class UnaryTests:
    """Parsed unary tests. Equal when they mean the same tests in the same order."""

    #: ``-``: any value matches, null included.
    any_value: bool = False
    negated: bool = False
    tests: tuple[PositiveTest, ...] = ()

    def literal_kinds(self) -> frozenset[str]:
        """The kinds of literal the tests compare with (number, string, …)."""
        kinds: set[str] = set()
        for test in self.tests:
            if isinstance(test, Comparison):
                kinds.add(test.endpoint.kind)
            elif isinstance(test, Interval):
                kinds.add(test.low.kind)
        return frozenset(kinds)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, message: str, pos: Optional[int] = None) -> UnaryTestSyntaxError:
        at = self.pos if pos is None else pos
        return UnaryTestSyntaxError(message, text=self.text, column=at + 1)

    def skip_spaces(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def at_end(self) -> bool:
        self.skip_spaces()
        return self.pos >= len(self.text)

    def word_ahead(self, word: str) -> bool:
        """True when ``word`` starts here and is not the start of a longer name."""
        match = _WORD_RE.match(self.text, self.pos)
        return bool(match) and match.group(0) == word

    def unary_tests(self) -> UnaryTests:
        if self.at_end():
            raise self.fail("a test is empty; write - to match any value")
        if self.text.strip() == "-":
            return UnaryTests(any_value=True)
        if self.word_ahead("not"):
            start = self.pos
            self.pos += len("not")
            self.skip_spaces()
            if self.pos >= len(self.text) or self.text[self.pos] != "(":
                raise self.fail("expected ( after not", start + len("not"))
            self.pos += 1
            tests = self.positive_tests()
            self.skip_spaces()
            if self.pos >= len(self.text) or self.text[self.pos] != ")":
                raise self.fail("expected ) to close not(")
            self.pos += 1
            if not self.at_end():
                raise self.fail("unexpected text after not(...)")
            return _normalised(tests, negated=True)
        tests = self.positive_tests()
        if not self.at_end():
            raise self.fail("unexpected text; separate tests with a comma")
        return _normalised(tests, negated=False)

    def positive_tests(self) -> tuple[PositiveTest, ...]:
        tests = [self.positive_test()]
        while True:
            self.skip_spaces()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                tests.append(self.positive_test())
            else:
                return tuple(tests)

    def positive_test(self) -> PositiveTest:
        self.skip_spaces()
        if self.pos >= len(self.text):
            raise self.fail("expected a test")
        if self.word_ahead("null"):
            self.pos += len("null")
            return NullTest()
        char = self.text[self.pos]
        if char in "[(]":
            return self.interval()
        for op in _OPS:
            if self.text.startswith(op, self.pos):
                self.pos += len(op)
                return Comparison(op, self.endpoint())
        return Comparison("=", self.endpoint())

    def interval(self) -> Interval:
        start = self.pos
        low_closed = self.text[self.pos] == "["
        self.pos += 1
        low = self.endpoint()
        self.skip_spaces()
        if not self.text.startswith("..", self.pos):
            raise self.fail("expected .. between the ends of a range")
        self.pos += 2
        high = self.endpoint()
        self.skip_spaces()
        if self.pos >= len(self.text) or self.text[self.pos] not in "])[":
            raise self.fail("expected ] or ) or [ to close a range")
        high_closed = self.text[self.pos] == "]"
        self.pos += 1
        if low.kind != high.kind:
            raise self.fail("both ends of a range must be of the same type", start)
        if low.kind == BOOLEAN:
            raise self.fail("a range cannot be over true and false", start)
        return Interval(low, high, low_closed, high_closed)

    def endpoint(self) -> Endpoint:
        self.skip_spaces()
        start = self.pos
        if start >= len(self.text):
            raise self.fail("expected a number, a string, true, false or date(...)")
        if self.text[start] == '"':
            return self.string()
        if self.word_ahead("true") or self.word_ahead("false"):
            word = "true" if self.word_ahead("true") else "false"
            self.pos += len(word)
            return Endpoint(BOOLEAN, word == "true", word)
        if self.word_ahead("date"):
            match = _DATE_CALL_RE.match(self.text, start)
            if not match:
                raise self.fail('a date is written date("YYYY-MM-DD")', start)
            try:
                value = date.fromisoformat(match.group(1))
            except ValueError:
                raise self.fail(
                    f"{match.group(1)} is not a calendar date", start
                ) from None
            self.pos = match.end()
            return Endpoint(DATE, value, f'date("{match.group(1)}")')
        match = _NUMBER_RE.match(self.text, start)
        if match:
            self.pos = match.end()
            lexeme = match.group(0)
            return Endpoint(NUMBER, Fraction(lexeme), lexeme)
        raise self.fail("expected a number, a string, true, false or date(...)", start)

    def string(self) -> Endpoint:
        start = self.pos
        self.pos += 1
        chars: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self.pos += 1
                return Endpoint(STRING, "".join(chars), self.text[start : self.pos])
            if char == "\\":
                following = self.text[self.pos + 1 : self.pos + 2]
                if following not in ('"', "\\"):
                    raise self.fail(
                        'in a string, a backslash is followed by " or another backslash'
                    )
                chars.append(following)
                self.pos += 2
                continue
            chars.append(char)
            self.pos += 1
        raise self.fail("a string is not closed", start)


def _normalised(tests: tuple[PositiveTest, ...], *, negated: bool) -> UnaryTests:
    """``not(x)`` and ``!= x`` are the same test, as are ``not(!= x)`` and ``x``.

    Both rewrites are exact, because equality is never unknown. One form for each
    lets two spellings of a test compare equal.
    """
    if negated and len(tests) == 1 and isinstance(tests[0], Comparison):
        only = tests[0]
        if only.op == "=":
            return UnaryTests(tests=(Comparison("!=", only.endpoint),))
        if only.op == "!=":
            return UnaryTests(tests=(Comparison("=", only.endpoint),))
    return UnaryTests(negated=negated, tests=tests)


@lru_cache(maxsize=1024)
def parse(text: str) -> UnaryTests:
    """Parse unary tests.

    Raises:
        UnaryTestSyntaxError: with the 1-based column where parsing stopped.
    """
    if not isinstance(text, str):
        raise UnaryTestSyntaxError("a test must be text", text=str(text), column=1)
    return _Parser(text).unary_tests()


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _reading(endpoint: Endpoint, value: Any) -> Any:
    """``value`` read as the endpoint's type, or ``None`` when it is not one."""
    if value is None:
        return None
    if endpoint.kind == NUMBER:
        try:
            return exact_numeric_reading(value, "Real")
        except ValueError:
            return None
    if endpoint.kind == STRING:
        return value if isinstance(value, str) else None
    if endpoint.kind == BOOLEAN:
        return value if isinstance(value, bool) else None
    return _as_date(value)


def _equal(endpoint: Endpoint, value: Any) -> bool:
    reading = _reading(endpoint, value)
    return reading is not None and reading == endpoint.value


def _order(endpoint: Endpoint, value: Any) -> Optional[int]:
    """-1, 0 or 1 as ``value`` is below, at or above the endpoint; else ``None``."""
    if endpoint.kind == BOOLEAN:
        return None
    reading = _reading(endpoint, value)
    if reading is None:
        return None
    return (reading > endpoint.value) - (reading < endpoint.value)


def _test(test: PositiveTest, value: Any) -> Optional[bool]:
    if isinstance(test, NullTest):
        return value is None
    if isinstance(test, Comparison):
        if test.op == "=":
            return _equal(test.endpoint, value)
        if test.op == "!=":
            return not _equal(test.endpoint, value)
        order = _order(test.endpoint, value)
        if order is None:
            return None
        if test.op == "<":
            return order < 0
        if test.op == "<=":
            return order <= 0
        if test.op == ">":
            return order > 0
        return order >= 0
    low = _order(test.low, value)
    high = _order(test.high, value)
    if low is None or high is None:
        return None
    above_low = low > 0 or (low == 0 and test.low_closed)
    below_high = high < 0 or (high == 0 and test.high_closed)
    return above_low and below_high


def matches(tests: UnaryTests, value: Any) -> Optional[bool]:
    """``True``, ``False`` or ``None`` (unknown) for ``value`` against ``tests``."""
    if tests.any_value:
        return True
    unknown = False
    result: Optional[bool] = False
    for test in tests.tests:
        outcome = _test(test, value)
        if outcome is True:
            result = True
            break
        if outcome is None:
            unknown = True
    if result is not True and unknown:
        result = None
    if tests.negated:
        return None if result is None else not result
    return result


def parse_literal(text: str) -> Any:
    """The value of an output entry: a string, a number, a boolean or ``None``.

    An empty entry and ``null`` are ``None``. A whole number is an ``int``; any
    other number is a ``Decimal``, so it compares exactly and no digit is lost. A
    date is its ISO string.
    """
    if not isinstance(text, str) or not text.strip() or text.strip() == "null":
        return None
    parser = _Parser(text)
    endpoint = parser.endpoint()
    if not parser.at_end():
        raise parser.fail("an output entry is one literal")
    if endpoint.kind == NUMBER:
        return (
            int(endpoint.value)
            if endpoint.value.denominator == 1
            else Decimal(endpoint.text)
        )
    if endpoint.kind == DATE:
        return endpoint.value.isoformat()
    return endpoint.value


def format_literal(value: Any) -> str:
    """The literal text for a string, a number, a boolean or ``None``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def _endpoint_display(endpoint: Endpoint) -> str:
    """A string's value without its quotes; any other literal as written."""
    return endpoint.value if endpoint.kind == STRING else endpoint.text


def display_values(text: str) -> list[str]:
    """The values the tests in ``text`` compare with, for showing to a reader.

    One entry per test, in order: a comparison gives its literal (a string
    without its quotes, any other literal as written), a range gives
    ``low..high`` and ``null`` gives ``null``. The operator and a surrounding
    ``not(...)`` are left out, so ``>= 0.8`` gives ``["0.8"]`` and
    ``not("a", "b")`` gives ``["a", "b"]``. ``-`` compares with nothing and gives
    an empty list.

    Raises:
        UnaryTestSyntaxError: when ``text`` does not parse.
    """
    values: list[str] = []
    for test in parse(text).tests:
        if isinstance(test, NullTest):
            values.append("null")
        elif isinstance(test, Interval):
            values.append(
                f"{_endpoint_display(test.low)}..{_endpoint_display(test.high)}"
            )
        else:
            values.append(_endpoint_display(test.endpoint))
    return values


def _positive_text(test: PositiveTest, *, bare_equality: bool) -> str:
    if isinstance(test, NullTest):
        return "null"
    if isinstance(test, Interval):
        return (
            ("[" if test.low_closed else "(")
            + f"{test.low.text}..{test.high.text}"
            + ("]" if test.high_closed else ")")
        )
    if test.op == "=" and bare_equality:
        return test.endpoint.text
    return f"{test.op} {test.endpoint.text}"


def to_dmn13(text: str) -> str:
    """``text`` in the form DMN 1.3 modelers accept.

    DMN 1.3 unary tests have no ``=`` or ``!=`` operator: equality is the bare
    literal and inequality is ``not(literal)``. The result means the same as
    ``text``.

    Raises:
        UnaryTestSyntaxError: when ``text`` does not parse.
        DecisionTableError: when ``!=`` is one of several tests, which DMN 1.3
            cannot express.
    """
    tests = parse(text)
    if tests.any_value:
        return "-"
    if (
        not tests.negated
        and len(tests.tests) == 1
        and isinstance(tests.tests[0], Comparison)
        and tests.tests[0].op == "!="
    ):
        return f"not({tests.tests[0].endpoint.text})"
    if any(isinstance(t, Comparison) and t.op == "!=" for t in tests.tests):
        raise DecisionTableError(
            f"The test {text!r} cannot be written in DMN 1.3: != can only stand "
            f"alone there, as not(...)"
        )
    body = ", ".join(_positive_text(t, bare_equality=True) for t in tests.tests)
    return f"not({body})" if tests.negated else body
