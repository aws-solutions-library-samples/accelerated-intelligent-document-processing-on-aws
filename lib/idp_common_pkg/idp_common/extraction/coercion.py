# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deterministic type/format coercion for extraction output.

Putting the class schema on the wire (forced tool use) makes the model emit *a*
number; it does not make it emit the *right* one. The failures that actually
occur are type/format mismatches in otherwise well-formed output:

* ``"$1,234.00"`` / ``"1.234,00"`` where the schema says ``number``
* ``"03/15/2024"`` where the schema says ``format: date``
* ``"Yes"`` where the schema says ``boolean``
* ``""`` where the field is nullable

Those are fixable deterministically and for free, which is the point: the
alternative is paying for another LLM round-trip to rewrite a comma. This module
runs **before** :mod:`idp_common.extraction.validation`, so the validator only
sees violations that a machine genuinely cannot resolve.

Design rules, in priority order — all four are load-bearing:

1. **Never lose information.** The input object is never mutated; a new object is
   returned. *Every* rewrite is recorded (path, original, coerced, code, reason)
   so it lands in the extraction metadata and a human can audit exactly what was
   changed. Silently rewriting extracted document data is worse than leaving it
   wrong.
2. **Never coerce across a type family.** No string → object, no object → array,
   no scalar → single-element array, no dropping of list elements. A cross-family
   mismatch is *recorded as a refusal* and the value survives untouched.
3. **Lossless-or-nothing, per value.** If a string does not cleanly and
   unambiguously represent the target type, it is left alone and the reason is
   recorded. ``"1,234.56"`` is not coerced into an ``integer`` field, because
   truncating is data loss.
4. **Refuse the genuinely ambiguous.** ``"01/02/2024"`` is January 2nd or
   February 1st depending on locale and there is no way to know which — so it is
   NOT coerced (see :func:`_parse_date_string` for the full policy). A wrongly
   "fixed" date is a data-corruption bug that nothing downstream can catch.

Like :mod:`idp_common.extraction.validation`, this module is free of
PIL / Strands / boto3 imports and depends only on the standard library, so it is
cheap to import in any Lambda and unit-testable in isolation. (The one shared
helper it reuses, ``config.schema_utils.deref_schema``, is imported lazily
because ``idp_common.config.__init__`` pulls in boto3 and the whole Pydantic
config model tree at module level.)

Note on ``x-aws-idp-*`` extension keys: unlike ``validation.py``, this module
does **not** need a strip pass. It reads only standard keywords by exact name
(``type``, ``format``, ``properties``, ``items``, ``anyOf``, ``oneOf``, ``$ref``,
``$defs``), and the ``x-aws-idp-`` namespace cannot shadow any of them.
"""

from __future__ import annotations

import copy
import datetime
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Cap the entries echoed into the metadata block so a pathological result (e.g.
# every row of a 1000-row table needing a date rewrite) cannot blow up the
# DynamoDB item. Full counts are always reported, and the dataclass keeps the
# complete lists for callers that want them.
_MAX_METADATA_ENTRIES = 50

# Guards against a self-referential ``$defs`` graph paired with deeply nested
# data. Real extraction results are a handful of levels deep.
_MAX_DEPTH = 50

# --- coercion / refusal codes -------------------------------------------------
# Stable machine codes so metrics can be built on them without parsing prose.
CODE_NUMBER_FROM_STRING = "number_from_string"
CODE_INTEGER_FROM_STRING = "integer_from_string"
CODE_BOOLEAN_FROM_STRING = "boolean_from_string"
CODE_BOOLEAN_FROM_NUMBER = "boolean_from_number"
CODE_STRING_FROM_NUMBER = "string_from_number"
CODE_STRING_FROM_BOOLEAN = "string_from_boolean"
CODE_DATE_NORMALIZED = "date_normalized"
CODE_EMPTY_STRING_TO_NULL = "empty_string_to_null"

CODE_TYPE_FAMILY_MISMATCH = "type_family_mismatch"
CODE_UNPARSEABLE_NUMBER = "unparseable_number"
CODE_UNPARSEABLE_BOOLEAN = "unparseable_boolean"
CODE_UNPARSEABLE_DATE = "unparseable_date"
CODE_AMBIGUOUS_DATE = "ambiguous_date"
CODE_FRACTIONAL_TO_INTEGER = "fractional_to_integer"
CODE_BOOLEAN_TO_NUMBER = "boolean_to_number"
CODE_MAX_DEPTH_EXCEEDED = "max_depth_exceeded"

DATE_ORDER_AUTO = "auto"
DATE_ORDER_MDY = "MDY"
DATE_ORDER_DMY = "DMY"
_VALID_DATE_ORDERS = frozenset({DATE_ORDER_AUTO, DATE_ORDER_MDY, DATE_ORDER_DMY})


@dataclass
class Coercion:
    """One value that was rewritten, located by a human-readable path."""

    path: str
    original: Any
    coerced: Any
    code: str
    reason: str

    def __str__(self) -> str:
        loc = self.path or "(root)"
        return (
            f"{loc}: {self.original!r} -> {self.coerced!r} ({self.code}: {self.reason})"
        )


@dataclass
class Refusal:
    """A value that did NOT satisfy its schema and was deliberately left alone.

    A refusal is the audit trail for "this looks wrong and a machine must not
    guess" — the value reaches the validator and the human reviewer unchanged.
    """

    path: str
    value: Any
    code: str
    reason: str

    def __str__(self) -> str:
        loc = self.path or "(root)"
        return (
            f"{loc}: left {_summarize(self.value)} as-is ({self.code}: {self.reason})"
        )


@dataclass
class CoercionReport:
    """Outcome of coercing an extracted object against its class schema.

    ``data`` is the coerced **copy**; the object passed in is never modified.
    """

    data: dict[str, Any]
    coercions: list[Coercion] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when at least one value was rewritten."""
        return bool(self.coercions)

    def to_metadata(self) -> dict[str, Any]:
        """Compact, JSON-serializable summary for the extraction metadata block."""
        return {
            "coerced": self.changed,
            "coercion_count": len(self.coercions),
            "refusal_count": len(self.refusals),
            "coercions": [
                {
                    "path": c.path,
                    "from": _summarize(c.original),
                    "to": _summarize(c.coerced),
                    "code": c.code,
                    "reason": c.reason,
                }
                for c in self.coercions[:_MAX_METADATA_ENTRIES]
            ],
            "refusals": [
                {
                    "path": r.path,
                    "value": _summarize(r.value),
                    "code": r.code,
                    "reason": r.reason,
                }
                for r in self.refusals[:_MAX_METADATA_ENTRIES]
            ],
        }

    def summary_line(self) -> str:
        """One-line description suitable for a log record."""
        if not self.coercions and not self.refusals:
            return "No type/format coercion needed."
        return (
            f"Coerced {len(self.coercions)} value(s); "
            f"left {len(self.refusals)} schema-mismatched value(s) untouched."
        )


def _summarize(value: Any, limit: int = 120) -> Any:
    """Render a value for the metadata block without dumping a whole subtree."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    if isinstance(value, dict):
        return f"<object with {len(value)} key(s)>"
    if isinstance(value, list):
        return f"<array with {len(value)} item(s)>"
    return value


# -----------------------------------------------------------------------------
# Schema navigation
# -----------------------------------------------------------------------------

# ``deref_schema`` is the repo's single ``$ref`` resolver (sibling-key override,
# cycle guard, dangling-ref-returns-as-is). It is imported on first use rather
# than at module scope because ``idp_common.config.__init__`` imports boto3 and
# the full Pydantic config tree, which would defeat this module's
# cheap-to-import property.
_deref_impl: Any = None
_deref_unavailable = False


def _deref(node: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` against ``root``'s ``$defs``; fail-open."""
    global _deref_impl, _deref_unavailable
    if not isinstance(node, dict):
        return {}
    if "$ref" not in node:
        return node
    if _deref_impl is None and not _deref_unavailable:
        try:
            from idp_common.config.schema_utils import deref_schema

            _deref_impl = deref_schema
        except Exception as exc:  # pragma: no cover - defensive
            _deref_unavailable = True
            logger.warning("Cannot resolve $ref during coercion: %s", exc)
    if _deref_impl is None:
        return node
    return _deref_impl(node, root)


def _types_of(node: dict[str, Any]) -> set[str]:
    """Collect the declared JSON Schema type names from a node."""
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    return set()


def _effective_node(node: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Dereference ``node`` and flatten the nullable ``anyOf``/``oneOf`` idiom.

    ``{"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}`` is
    what the schema editor emits for a nullable typed field, and the ``format``
    and ``properties`` live on the inner branch. Only that shape is flattened:
    a union with two or more *typed, non-null* branches is genuinely a choice of
    targets, so it is left with no type guidance at all (nothing is coerced).
    """
    node = _deref(node, root)
    if not isinstance(node, dict):
        return {}
    if _types_of(node):
        return node

    for keyword in ("anyOf", "oneOf"):
        branches = node.get(keyword)
        if not isinstance(branches, list):
            continue
        resolved = [_deref(b, root) for b in branches]
        typed = [b for b in resolved if _types_of(b)]
        if len(typed) != len(resolved) or not typed:
            continue
        non_null = [b for b in typed if _types_of(b) != {"null"}]
        if len(non_null) != 1:
            continue
        merged: dict[str, Any] = dict(non_null[0])
        nullable = len(typed) > 1
        merged.update({k: v for k, v in node.items() if k not in (keyword,)})
        if nullable:
            merged["type"] = sorted(_types_of(non_null[0]) | {"null"})
        return merged
    return node


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


# -----------------------------------------------------------------------------
# Number parsing
# -----------------------------------------------------------------------------

# Symbols only. Letters are not stripped as symbols (an "R" or "kr" prefix is
# not distinguishable from a stray word), except for the explicit ISO code
# allow-list below, which must appear as its own whitespace-delimited token.
_CURRENCY_SYMBOLS = frozenset("$€£¥₹¢₩₽₪₫₦₱₡฿₴₺₸₼₾﷼")
_ISO_CURRENCY_CODES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR", "MXN",
        "BRL", "SEK", "NOK", "DKK", "ZAR", "NZD", "SGD", "HKD", "KRW", "PLN",
    }
)  # fmt: skip
_MINUS_SIGNS = frozenset({"-", "−", "–"})
_SPACE_CHARS = str.maketrans({" ": " ", " ": " ", " ": " "})
# Swiss-style group separators, normalized to a plain ASCII apostrophe.
_APOSTROPHES = ("’", "´", "ʼ")


def _valid_grouping(digits: str, sep: str) -> bool:
    """True when ``digits`` uses ``sep`` strictly as a 3-digit group separator."""
    groups = digits.split(sep)
    if len(groups) < 2:
        return False
    if not (1 <= len(groups[0]) <= 3) or not groups[0].isdigit():
        return False
    # A real thousands-grouped number never has a redundant leading zero in its
    # first group: "0,001" and "00,001" are not 1 under any convention. Without
    # this, "0,001" was rewritten to 1 — a 1000x error, recorded with a reason
    # claiming the comma was a thousands separator.
    if groups[0].startswith("0"):
        return False
    return all(len(g) == 3 and g.isdigit() for g in groups[1:])


# Python refuses int(str) above 4300 digits (CVE-2020-10735 mitigation), and a
# float that long overflows to inf. A degenerate OCR digit-run — a barcode, a MICR
# line, a model repetition loop — must not raise, because the caller disables
# coercion for the ENTIRE section on any exception.
_MAX_NUMERIC_DIGITS = 4000


def _finite_float(
    int_part: str, frac_part: str, notes: list[str]
) -> tuple[float | None, str]:
    """Build a float from an integer/fraction pair, refusing a non-finite result.

    Every ``float()`` in this module goes through here. The digit cap is ~4000 but
    a float overflows to ``inf`` above ~309 digits, and ``inf`` is not
    JSON-serializable (``json.dumps`` emits the bare token ``Infinity``, which
    ``JSON.parse`` rejects) nor convertible to a DynamoDB ``Decimal`` — so a
    coercion that produced one would break the write instead of repairing a value.
    """
    parsed = float(int_part + "." + frac_part)
    if not math.isfinite(parsed):
        return None, (
            "value overflows a float — refusing rather than storing a "
            "non-serializable infinity"
        )
    return parsed, "; ".join(notes)


def _parse_grouped_digits(body: str) -> tuple[int | float | None, str]:
    """Parse a sign-free, symbol-free numeric body into an ``int``/``float``.

    Returns ``(value, note)`` on success or ``(None, reason)`` on refusal. The
    note names any assumption made, so it can be recorded verbatim.

    Separator policy (each assumption is recorded, never silent):

    * ``,`` and ``.`` both present → the **rightmost** is the decimal separator
      and the other must appear only in valid 3-digit grouping positions.
      ``"1.234,00"`` → ``1234.0``, ``"1,234.00"`` → ``1234.0``.
    * The same separator two or more times → thousands grouping.
      ``"1.234.567"`` → ``1234567``.
    * A single ``.`` → decimal point (the machine-readable convention, and what
      ``float()`` does). ``"1.234"`` → ``1.234``.
    * A single ``,`` with exactly three digits after it → thousands separator
      (``"1,234"`` → ``1234``); otherwise a decimal comma (``"1,50"`` → ``1.5``).
    * ``'`` and space are thousands separators only, and must group by 3.

    Anything whose grouping does not check out is refused rather than guessed at.
    """
    if sum(c.isdigit() for c in body) > _MAX_NUMERIC_DIGITS:
        return None, (
            f"more than {_MAX_NUMERIC_DIGITS} digits — refusing rather than "
            "risking an overflow or a conversion error"
        )

    notes: list[str] = []

    for ch in _APOSTROPHES:
        if ch in body:
            body = body.replace(ch, "'")
    for sep in ("'", " "):
        if sep not in body:
            continue
        head, dec_sep, tail = _split_decimal_tail(body)
        if sep in tail:
            return None, f"'{sep}' inside the decimal part"
        if not _valid_grouping(head, sep):
            return None, f"'{sep}' is not used as a valid 3-digit group separator"
        body = head.replace(sep, "") + dec_sep + tail
        notes.append(f"'{sep}' read as thousands separator")

    has_comma = "," in body
    has_dot = "." in body

    if has_comma and has_dot:
        decimal_sep = "," if body.rfind(",") > body.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        head, _, tail = body.rpartition(decimal_sep)
        if decimal_sep in head or not tail.isdigit() or not tail:
            return None, "more than one decimal separator"
        if not _valid_grouping(head, group_sep):
            return None, (
                f"'{group_sep}' is not used as a valid 3-digit group separator"
            )
        notes.append(
            f"'{group_sep}' read as thousands separator, "
            f"'{decimal_sep}' as decimal separator"
        )
        return _finite_float(head.replace(group_sep, ""), tail, notes)

    for sep in (",", "."):
        if sep not in body:
            continue
        count = body.count(sep)
        if count > 1:
            if not _valid_grouping(body, sep):
                return None, f"'{sep}' is not used as a valid 3-digit group separator"
            notes.append(f"'{sep}' read as thousands separator")
            return int(body.replace(sep, "")), "; ".join(notes)
        head, _, tail = body.partition(sep)
        if not head.isdigit() or not tail.isdigit():
            return None, "not a plain decimal number"
        if sep == "," and len(tail) == 3:
            # Same leading-zero rule as _valid_grouping: "0,001" cannot mean 1.
            # A leading zero means the comma is a DECIMAL separator, and since
            # that reading is itself only an assumption for a 3-digit tail, this
            # shape is ambiguous — refuse rather than pick one.
            if head == "0" or (len(head) > 1 and head[0] == "0"):
                return None, (
                    f"'{head},{tail}' has a leading zero, so ',' cannot be a "
                    "thousands separator; the intended decimal convention is "
                    "ambiguous"
                )
            notes.append(
                "single ',' with three trailing digits read as a thousands separator"
            )
            return int(head + tail), "; ".join(notes)
        notes.append(f"'{sep}' read as decimal separator")
        return _finite_float(head, tail, notes)

    if not body.isdigit():
        return None, "not a plain decimal number"
    return int(body), "; ".join(notes)


def _split_decimal_tail(body: str) -> tuple[str, str, str]:
    """Split off a trailing ``,``/``.`` decimal part, if any."""
    for sep in (",", "."):
        if body.count(sep) == 1:
            head, _, tail = body.partition(sep)
            if tail.isdigit() and not (sep == "," and len(tail) == 3):
                return head, sep, tail
    return body, "", ""


def _parse_numeric_string(raw: str) -> tuple[int | float | None, str]:
    """Parse a currency/percent/grouped numeric string. ``(value, note)``.

    On refusal returns ``(None, reason)``.

    A trailing/leading ``%`` is removed and the **magnitude is preserved** — it
    is NOT divided by 100. The schema carries no unit, so a document showing
    "12.5%" becomes ``12.5``: dividing would silently change the value by two
    orders of magnitude, and multiplying nothing is what a human reading the
    document sees. The percent removal is always recorded.
    """
    text = raw.strip().translate(_SPACE_CHARS)
    if not text:
        return None, "empty string"

    notes: list[str] = []
    negative = False

    if text.startswith("(") and text.endswith(")") and len(text) > 2:
        text = text[1:-1].strip()
        negative = True
        notes.append("accounting parentheses read as a negative value")

    tokens = text.split()
    if len(tokens) > 1:
        if tokens[0].upper() in _ISO_CURRENCY_CODES:
            tokens = tokens[1:]
            notes.append("removed ISO currency code")
        elif tokens[-1].upper() in _ISO_CURRENCY_CODES:
            tokens = tokens[:-1]
            notes.append("removed ISO currency code")
    text = " ".join(tokens).strip()

    # Peel sign / currency symbol / percent from either end, in any order.
    percent = False
    changed = True
    while changed and text:
        changed = False
        if text[0] in _MINUS_SIGNS:
            negative = not negative
            text, changed = text[1:].strip(), True
        elif text[0] == "+":
            text, changed = text[1:].strip(), True
        elif text[0] in _CURRENCY_SYMBOLS:
            text, changed = text[1:].strip(), True
            if "removed currency symbol" not in notes:
                notes.append("removed currency symbol")
        elif text[-1] in _CURRENCY_SYMBOLS:
            text, changed = text[:-1].strip(), True
            if "removed currency symbol" not in notes:
                notes.append("removed currency symbol")
        elif text[0] == "%" or text[-1] == "%":
            text = (text[1:] if text[0] == "%" else text[:-1]).strip()
            percent, changed = True, True

    if not text:
        return None, "no digits found"

    value, note = _parse_grouped_digits(text)
    if value is None:
        return None, note
    if note:
        notes.append(note)
    if percent:
        notes.append("removed '%'; magnitude preserved as written (NOT divided by 100)")
    if negative:
        value = -value
    return value, "; ".join(n for n in notes if n)


# -----------------------------------------------------------------------------
# Boolean parsing
# -----------------------------------------------------------------------------

_TRUE_STRINGS = frozenset(
    {"true", "yes", "y", "t", "1", "on", "checked", "selected", "present"}
)
_FALSE_STRINGS = frozenset(
    {"false", "no", "n", "f", "0", "off", "unchecked", "unselected", "absent"}
)


def _parse_boolean_string(raw: str) -> bool | None:
    """Map an unambiguous boolean-ish string to a ``bool``, else ``None``.

    ``"X"`` is deliberately absent: a cross in a checkbox means "checked" in
    some documents and "not applicable" in others.
    """
    token = raw.strip().lower()
    if token in _TRUE_STRINGS:
        return True
    if token in _FALSE_STRINGS:
        return False
    return None


# -----------------------------------------------------------------------------
# Date parsing
# -----------------------------------------------------------------------------

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

_DATE_SPLIT_RE = re.compile(r"[-\s,./]+")
_ORDINAL_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date_string(raw: str, date_order: str) -> tuple[str | None, str]:
    """Normalize a date string to ISO ``YYYY-MM-DD``. ``(iso, note)``.

    On refusal returns ``(None, reason)``. Refusal is the default whenever the
    reading is not forced by the value itself:

    ============================= ==========================================
    Input                         Policy
    ============================= ==========================================
    ``2024-03-15``                already ISO → untouched
    ``2024/03/15``                year-first → unambiguous → converted
    ``15/03/2024``                day > 12 → must be D/M/Y → converted
    ``03/15/2024``                second > 12 → must be M/D/Y → converted
    ``03/03/2024``                both readings give the same date → converted
    ``01/02/2024``               **REFUSED** — Jan 2 or Feb 1, unknowable
    ``03/15/24``                 **REFUSED** — 1924 or 2024, unknowable
    ``March 15, 1980``            month is named → order irrelevant → converted
    ``2024-03-15T00:00:00Z``     **REFUSED** — dropping a time is data loss
    ============================= ==========================================

    ``date_order`` is the explicit, opt-in escape hatch for the ambiguous
    all-numeric case: ``"MDY"`` or ``"DMY"`` tells the parser which convention
    the *source documents* use. It defaults to ``"auto"``, which refuses. It
    never overrides a value that is already unambiguous — a ``15`` cannot be a
    month no matter what the caller claims.
    """
    text = raw.strip()
    if _ISO_DATE_RE.match(text):
        try:
            datetime.date.fromisoformat(text)
        except ValueError:
            return None, "not a valid calendar date"
        return None, ""  # already ISO and valid: nothing to do

    tokens = [t for t in _DATE_SPLIT_RE.split(text) if t]
    if len(tokens) != 3:
        return None, "not a recognizable day/month/year date"

    month_from_name: int | None = None
    numbers: list[tuple[int, int]] = []  # (value, digit_count)
    for token in tokens:
        named = _MONTH_NAMES.get(token.lower().rstrip("."))
        if named is not None and month_from_name is None:
            month_from_name = named
            continue
        ordinal = _ORDINAL_RE.match(token)
        digits = ordinal.group(1) if ordinal else token
        if not digits.isdigit():
            return None, "not a recognizable day/month/year date"
        numbers.append((int(digits), len(digits)))

    note = ""
    if month_from_name is not None:
        if len(numbers) != 2:
            return None, "not a recognizable day/month/year date"
        year_positions = [i for i, n in enumerate(numbers) if n[1] == 4]
        if len(year_positions) != 1:
            return None, (
                "no unambiguous 4-digit year; a two-digit year cannot be "
                "assigned a century"
            )
        year = numbers[year_positions[0]][0]
        day = numbers[1 - year_positions[0]][0]
        month = month_from_name
        note = "month given by name, so day/month order is unambiguous"
    else:
        if len(numbers) != 3:
            return None, "not a recognizable day/month/year date"
        first, second, third = numbers
        if first[1] == 4:
            year, month, day = first[0], second[0], third[0]
            note = "year-first, read as ISO Y-M-D"
        elif third[1] == 4:
            year = third[0]
            a, b = first[0], second[0]
            if a > 12 and b <= 12:
                day, month = a, b
                note = "first field > 12, so it can only be the day (D/M/Y)"
            elif b > 12 and a <= 12:
                month, day = a, b
                note = "second field > 12, so it can only be the day (M/D/Y)"
            elif a > 12 and b > 12:
                return None, "neither field can be a month"
            elif a == b:
                month, day = a, b
                note = "day and month are equal, so the order does not matter"
            elif date_order == DATE_ORDER_MDY:
                month, day = a, b
                note = "ambiguous; resolved by the caller's date_order='MDY' hint"
            elif date_order == DATE_ORDER_DMY:
                day, month = a, b
                note = "ambiguous; resolved by the caller's date_order='DMY' hint"
            else:
                return None, (
                    f"ambiguous day/month order ({a}/{b}) — could be "
                    f"{a:02d}/{b:02d} or {b:02d}/{a:02d}; set date_order to "
                    "resolve it explicitly"
                )
        else:
            return None, (
                "no 4-digit year; a two-digit year cannot be assigned a century"
            )

    try:
        iso = datetime.date(year, month, day).isoformat()
    except ValueError:
        return None, "not a valid calendar date"
    return iso, note


# -----------------------------------------------------------------------------
# Leaf coercion
# -----------------------------------------------------------------------------


def _satisfies(value: Any, types: set[str]) -> bool:
    """True when ``value`` already satisfies one of the declared type names."""
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return "integer" in types or "number" in types
    if isinstance(value, float):
        if "number" in types:
            return True
        # JSON Schema: 1.0 is a valid "integer".
        return "integer" in types and value.is_integer()
    if isinstance(value, str):
        return "string" in types
    if isinstance(value, list):
        return "array" in types
    if isinstance(value, dict):
        return "object" in types
    return True


class _Ctx:
    """Mutable accumulator threaded through the walk."""

    def __init__(self, date_order: str) -> None:
        self.date_order = date_order
        self.coercions: list[Coercion] = []
        self.refusals: list[Refusal] = []

    def coerced(
        self, path: str, original: Any, new: Any, code: str, reason: str
    ) -> Any:
        self.coercions.append(Coercion(path, original, new, code, reason))
        return new

    def refused(self, path: str, value: Any, code: str, reason: str) -> Any:
        self.refusals.append(Refusal(path, value, code, reason))
        return value


def _coerce_leaf(value: Any, node: dict[str, Any], path: str, ctx: _Ctx) -> Any:
    """Coerce one scalar against its schema node, recording every decision."""
    types = _types_of(node)
    fmt = node.get("format")

    # ``""`` in a field that is not a plain string carries no information, so
    # mapping it to null is lossless. Elsewhere in this system null means
    # "not present in the document" (see validation._drop_null_properties), so
    # this is also the shape the validator and the UI expect.
    if isinstance(value, str) and not value.strip():
        scalar_target = bool(types) and not (types & {"object", "array"})
        if scalar_target and ("string" not in types or "null" in types):
            return ctx.coerced(
                path,
                value,
                None,
                CODE_EMPTY_STRING_TO_NULL,
                "empty string in a nullable/non-string field means 'not found'",
            )
        return value

    if not _satisfies(value, types) and types:
        value = _coerce_type(value, types, path, ctx)

    if isinstance(value, str) and fmt == "date" and value.strip():
        iso, note = _parse_date_string(value, ctx.date_order)
        if iso is not None:
            return ctx.coerced(path, value, iso, CODE_DATE_NORMALIZED, note)
        if note:
            ctx.refused(
                path,
                value,
                CODE_AMBIGUOUS_DATE if "ambiguous" in note else CODE_UNPARSEABLE_DATE,
                f"format: date expects ISO YYYY-MM-DD; {note}",
            )
    return value


def _coerce_type(value: Any, types: set[str], path: str, ctx: _Ctx) -> Any:
    """Apply the scalar → scalar conversions; refuse everything else."""
    # Rule 2: a container never becomes a scalar and a scalar never becomes a
    # container. No splitting a string into an array, no wrapping a scalar in a
    # one-element array, no reading an object as text.
    if isinstance(value, (dict, list)) or types & {"object", "array"}:
        return ctx.refused(
            path,
            value,
            CODE_TYPE_FAMILY_MISMATCH,
            f"{_py_kind(value)} value in a {_render_types(types)} field; "
            "coercing across "
            "type families would change the shape of the data",
        )

    # A null means "not present in the document" throughout this system; it is
    # the validator's job to flag one on a required field, not coercion's.
    if value is None:
        return value

    if isinstance(value, bool):
        if "string" in types:
            return ctx.coerced(
                path,
                value,
                "true" if value else "false",
                CODE_STRING_FROM_BOOLEAN,
                "declared as a string",
            )
        return ctx.refused(
            path,
            value,
            CODE_BOOLEAN_TO_NUMBER,
            f"boolean in a {_render_types(types)} field; which number a "
            "true/false means is not knowable",
        )

    if isinstance(value, (int, float)):
        if "string" in types:
            return ctx.coerced(
                path,
                value,
                str(value),
                CODE_STRING_FROM_NUMBER,
                "declared as a string",
            )
        if "boolean" in types:
            if value in (0, 1):
                return ctx.coerced(
                    path,
                    value,
                    bool(value),
                    CODE_BOOLEAN_FROM_NUMBER,
                    "1/0 read as true/false",
                )
            return ctx.refused(
                path,
                value,
                CODE_UNPARSEABLE_BOOLEAN,
                "only 1 and 0 map unambiguously onto true/false",
            )
        if "integer" in types:
            # Reached only when the float is non-integral (_satisfies covers 1.0).
            return ctx.refused(
                path,
                value,
                CODE_FRACTIONAL_TO_INTEGER,
                "rounding to an integer would discard the fractional part",
            )
        return value

    if isinstance(value, str):
        if types & {"number", "integer"}:
            parsed, note = _parse_numeric_string(value)
            if parsed is None:
                return ctx.refused(
                    path,
                    value,
                    CODE_UNPARSEABLE_NUMBER,
                    f"cannot be read as a number without guessing: {note}",
                )
            if "number" not in types:
                if isinstance(parsed, float) and not parsed.is_integer():
                    return ctx.refused(
                        path,
                        value,
                        CODE_FRACTIONAL_TO_INTEGER,
                        "declared integer, but the value has a fractional part "
                        "that rounding would discard",
                    )
                parsed = int(parsed)
            code = (
                CODE_INTEGER_FROM_STRING
                if isinstance(parsed, int)
                else CODE_NUMBER_FROM_STRING
            )
            return ctx.coerced(path, value, parsed, code, note or "parsed as a number")
        if "boolean" in types:
            parsed_bool = _parse_boolean_string(value)
            if parsed_bool is None:
                return ctx.refused(
                    path,
                    value,
                    CODE_UNPARSEABLE_BOOLEAN,
                    "not an unambiguous true/false value",
                )
            return ctx.coerced(
                path,
                value,
                parsed_bool,
                CODE_BOOLEAN_FROM_STRING,
                "boolean-ish string",
            )
        return ctx.refused(
            path,
            value,
            CODE_TYPE_FAMILY_MISMATCH,
            f"string in a {_render_types(types)} field",
        )

    return value


def _py_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _render_types(types: set[str]) -> str:
    return "/".join(sorted(types)) or "untyped"


# -----------------------------------------------------------------------------
# Walk
# -----------------------------------------------------------------------------


def _walk(
    value: Any,
    node: Any,
    root: dict[str, Any],
    path: str,
    ctx: _Ctx,
    depth: int,
) -> Any:
    """Rebuild ``value`` guided by ``node``; never mutate the original."""
    if depth > _MAX_DEPTH:
        ctx.refused(
            path,
            value,
            CODE_MAX_DEPTH_EXCEEDED,
            f"nesting deeper than {_MAX_DEPTH} levels was left untouched",
        )
        # NOT copy.deepcopy: that recurses over the entire remaining subtree,
        # so the depth bound it is meant to enforce would blow the stack anyway.
        # Past the bound we hand back the value as-is; it is left uncoerced,
        # which is exactly what the refusal says.
        return value

    resolved = _effective_node(node, root) if isinstance(node, dict) else {}
    types = _types_of(resolved)

    if isinstance(value, dict):
        if types and "object" not in types:
            ctx.refused(
                path,
                value,
                CODE_TYPE_FAMILY_MISMATCH,
                f"object value in a {_render_types(types)} field; coercing "
                "across type families would change the shape of the data",
            )
            resolved, types = {}, set()
        properties = resolved.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        additional = resolved.get("additionalProperties")
        additional = additional if isinstance(additional, dict) else None
        out: dict[str, Any] = {}
        for key, child_value in value.items():
            child_node = properties.get(key, additional)
            out[key] = _walk(
                child_value, child_node, root, _join(path, str(key)), ctx, depth + 1
            )
        return out

    if isinstance(value, list):
        if types and "array" not in types:
            ctx.refused(
                path,
                value,
                CODE_TYPE_FAMILY_MISMATCH,
                f"array value in a {_render_types(types)} field; coercing "
                "across type families would change the shape of the data",
            )
            resolved = {}
        items = resolved.get("items")
        # Tuple-form ``items: [...]`` gives no single item schema; leave it
        # unguided rather than pairing the wrong schema with the wrong element.
        items = items if isinstance(items, dict) else None
        # Every element is kept, at its original index. Rule 2: no filtering.
        return [
            _walk(item, items, root, f"{path}[{index}]", ctx, depth + 1)
            for index, item in enumerate(value)
        ]

    if not isinstance(node, dict):
        return value
    return _coerce_leaf(value, resolved, path, ctx)


def coerce_extraction(
    data: dict[str, Any],
    schema: dict[str, Any],
    *,
    date_order: str = DATE_ORDER_AUTO,
) -> CoercionReport:
    """Coerce an extracted object's values to the types its schema declares.

    Args:
        data: The extracted object. **Never mutated** — a coerced copy is
            returned in ``report.data``.
        schema: The class JSON Schema (``x-aws-idp-*`` extensions may be
            present; only standard keywords are read). ``$ref``/``$defs``,
            nested objects and arrays of objects are all followed.
        date_order: Reading to assume for an all-numeric date whose day/month
            order cannot be determined from the value itself — ``"auto"``
            (default; such dates are left alone), ``"MDY"`` or ``"DMY"``. An
            unrecognized value falls back to ``"auto"`` with a warning rather
            than failing extraction.

    Returns:
        A :class:`CoercionReport` carrying the coerced copy, every rewrite and
        every deliberate refusal. Nothing is ever rewritten silently.

        On unusable input (``data`` or ``schema`` not a dict) the report carries
        the data unchanged with no entries — coercion must never be able to
        harden extraction into a failure.
    """
    if date_order not in _VALID_DATE_ORDERS:
        logger.warning(
            "Unknown date_order %r; falling back to %r (ambiguous dates are "
            "left untouched)",
            date_order,
            DATE_ORDER_AUTO,
        )
        date_order = DATE_ORDER_AUTO

    if not isinstance(data, dict):
        logger.warning("Skipping coercion: extracted data is not an object")
        return CoercionReport(data=data)
    if not isinstance(schema, dict):
        logger.warning("Skipping coercion: class schema is not an object")
        return CoercionReport(data=copy.deepcopy(data))

    ctx = _Ctx(date_order)
    coerced = _walk(data, schema, schema, "", ctx, 0)
    return CoercionReport(data=coerced, coercions=ctx.coercions, refusals=ctx.refusals)
