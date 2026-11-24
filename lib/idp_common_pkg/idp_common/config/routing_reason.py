# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reason of a routing rule: its placeholders, and the text a reviewer reads.

A routing rule's ``reason`` is text with placeholders in braces. When a document
is placed in a review queue, each placeholder is replaced with the document's
value, and the result is what the reviewer reads.

**The grammar.**

- ``{{`` is a literal ``{`` and ``}}`` a literal ``}``.
- ``{token}`` is a placeholder. The token is not empty and holds no brace. Spaces
  around it, and around its parts, are ignored.
- A ``{`` or ``}`` that is neither is a syntax error.

**The tokens.**

- ``confidence_threshold``: the profile's confidence threshold.
- ``condition_N.value`` (N from 1 to 99): the value the routing rule's N-th
  condition compares with.
- ``input`` or ``input:path``: a routing input, as a condition names it. The path
  is everything after the first ``:``, so ``{field_value:Invoice:Total}`` reads
  the input ``field_value`` with the path ``Invoice:Total``.

:func:`parse_reason` says what a text asks for, and is what a save checks.
:func:`render_reason` fills the placeholders in one pass, so a value that itself
holds braces is never read as a placeholder.

This module imports the standard library only: the save validation and the
routing Lambda both load it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Callable, List, Literal, NamedTuple

__all__ = [
    "ABSENT",
    "CONFIDENCE_THRESHOLD",
    "ELLIPSIS",
    "KIND_CONDITION_VALUE",
    "KIND_INPUT",
    "MAX_LIST_ITEMS",
    "MAX_RENDERED_REASON_BYTES",
    "MAX_VALUE_BYTES",
    "Placeholder",
    "ReasonSyntaxError",
    "SOURCE_ADMIN",
    "SOURCE_FALLBACK",
    "cut_bytes",
    "format_value",
    "parse_reason",
    "render_reason",
]

#: The most bytes a rendered reason holds, and one value in it.
MAX_RENDERED_REASON_BYTES = 1000
MAX_VALUE_BYTES = 200
#: A list value shows this many items, then how many more there are.
MAX_LIST_ITEMS = 5
#: What a placeholder shows when the document has no value for it.
ABSENT = "(no value)"
#: Ends a text that was cut.
ELLIPSIS = "…"

#: Where a reason came from: the Admin's text, or the generated fallback.
SOURCE_ADMIN = "admin"
SOURCE_FALLBACK = "fallback"

#: The token, and the placeholder kind, of the profile's confidence threshold.
CONFIDENCE_THRESHOLD = "confidence_threshold"
#: The other two placeholder kinds.
KIND_INPUT = "input"
KIND_CONDITION_VALUE = "condition_value"

_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
_CONDITION_RE = re.compile(r"^condition_([1-9][0-9]?)\.value$")


class ReasonSyntaxError(ValueError):
    """A reason that does not follow the grammar. The message says what and where."""


class Placeholder(NamedTuple):
    """One placeholder of a reason."""

    #: As written, braces included.
    text: str
    kind: Literal["input", "condition_value", "confidence_threshold"]
    #: The routing input, for an ``input`` placeholder.
    input: str = ""
    #: The input's path, when one is written.
    path: str = ""
    #: Which condition, counted from 1, for a ``condition_value`` placeholder.
    index: int = 0


def _placeholder(match: re.Match) -> Placeholder:
    """The placeholder a ``{token}`` match writes.

    Raises:
        ReasonSyntaxError: an empty token, or an input placeholder with no input
            or with an empty path after the colon.
    """
    written = match.group(0)
    at = match.start() + 1
    token = match.group(1).strip()
    if not token:
        raise ReasonSyntaxError(
            f"an empty placeholder at character {at}; a placeholder names a "
            f"routing input, for example {{page_count}}"
        )
    if token == CONFIDENCE_THRESHOLD:
        return Placeholder(text=written, kind=CONFIDENCE_THRESHOLD)
    condition = _CONDITION_RE.match(token)
    if condition:
        return Placeholder(
            text=written, kind=KIND_CONDITION_VALUE, index=int(condition.group(1))
        )
    name, colon, path = token.partition(":")
    name, path = name.strip(), path.strip()
    if not name:
        raise ReasonSyntaxError(
            f"the placeholder '{written}' at character {at} names no routing "
            f"input before ':'"
        )
    if colon and not path:
        raise ReasonSyntaxError(
            f"the placeholder '{written}' at character {at} has no path after ':'"
        )
    return Placeholder(text=written, kind=KIND_INPUT, input=name, path=path)


def _unmatched(match: re.Match) -> ReasonSyntaxError:
    brace = match.group(0)
    return ReasonSyntaxError(
        f"an unmatched '{brace}' at character {match.start() + 1}; write "
        f"'{brace * 2}' for a literal brace"
    )


def parse_reason(text: str) -> List[Placeholder]:
    """The placeholders of ``text``, in the order they are written.

    A placeholder written twice is listed twice.

    Raises:
        ReasonSyntaxError: naming the problem and the character it is at,
            counted from 1.
    """
    placeholders: List[Placeholder] = []
    for match in _TOKEN_RE.finditer(text or ""):
        if match.group(1) is not None:
            placeholders.append(_placeholder(match))
        elif len(match.group(0)) == 1:
            raise _unmatched(match)
    return placeholders


def cut_bytes(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` UTF-8 bytes.

    A longer text is cut between two characters, never inside one, and ends with
    :data:`ELLIPSIS`; the result, the ellipsis included, still fits ``limit``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    mark = ELLIPSIS.encode("utf-8")
    room = limit - len(mark)
    if room < 0:
        return encoded[: max(limit, 0)].decode("utf-8", errors="ignore")
    return encoded[:room].decode("utf-8", errors="ignore") + ELLIPSIS


def _is_absent(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple)) and not value)


def _text(value: Any) -> str:
    if _is_absent(value):
        return ABSENT
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, Decimal):
        # A NaN or an infinity has no fixed-point form to normalize to.
        return format(value.normalize(), "f") if value.is_finite() else str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        shown = ", ".join(_text(item) for item in value[:MAX_LIST_ITEMS])
        more = len(value) - MAX_LIST_ITEMS
        return f"{shown} and {more} more" if more > 0 else shown
    try:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def format_value(value: Any) -> str:
    """A value as a reviewer reads it, in at most :data:`MAX_VALUE_BYTES` bytes.

    ``None``, an empty string and an empty list read :data:`ABSENT`. A boolean
    reads ``yes`` or ``no``. A whole number reads without a fraction, whether it
    is an ``int``, a ``float`` or a ``Decimal``. A list shows its first
    :data:`MAX_LIST_ITEMS` items and how many more there are. Anything else is
    compact JSON. Never raises.
    """
    return cut_bytes(_text(value), MAX_VALUE_BYTES)


def render_reason(text: str, resolve: Callable[[Placeholder], Any]) -> str:
    """``text`` with each placeholder replaced by ``format_value(resolve(it))``.

    One pass over the text: what a value holds is never read as a placeholder or
    an escape. The result is cut to :data:`MAX_RENDERED_REASON_BYTES`.

    Raises:
        ReasonSyntaxError: the text does not follow the grammar.
        Exception: whatever ``resolve`` raises.
    """

    def replace(match: re.Match) -> str:
        if match.group(1) is not None:
            return format_value(resolve(_placeholder(match)))
        written = match.group(0)
        if len(written) == 1:
            raise _unmatched(match)
        return written[0]

    return cut_bytes(_TOKEN_RE.sub(replace, text or ""), MAX_RENDERED_REASON_BYTES)
