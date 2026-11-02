# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a BDA blueprint's ``class`` can hold, and how a class id is carried in it.

The ``class`` value is how a document class reaches BDA and how it comes back:
a BDA → IDP sync reads it as the class id, and the BDA results Lambda reads the
type BDA reports for a document as one too. CreateBlueprint takes the same
characters there as in a blueprint's text (``BDA_TEXT_CHARS``) and at most ``BDA_CLASS_MAX_LENGTH`` of
them, so a class id holding, say, an em dash or a brace used to be rejected.

Rewriting such a character the way text is rewritten would rename the class on
the way back. ``to_blueprint_class`` instead escapes it as ``_xHHHH_`` (its code
point in upper-case hex, four to six digits), and ``from_blueprint_class``
reverses that exactly, so ``from_blueprint_class(to_blueprint_class(x)) == x``
for every string. A class id BDA already accepts is returned unchanged, so no
blueprint that synced before changes. The one exception is an id that already
contains text of the escape's shape, such as ``A_x0041_``: its ``_`` is escaped
too (``A_x005F_x0041_``), which is what keeps the reverse exact.

This module imports nothing beyond the standard library, because the BDA
results Lambda uses ``from_blueprint_class`` and installs a minimal
``idp_common``.
"""

import re

# The characters CreateBlueprint takes in a blueprint's text values, measured
# against the control plane (DOCUMENT blueprints, us-west-2) with the schema
# serialised the way the sync sends it (``json.dumps``, so non-ASCII arrives
# escaped). The same set holds for a property's ``instruction`` and for the
# blueprint's ``description``. One blueprint whose instruction and description
# each held every character below was accepted; each character outside it that
# was probed was rejected on its own with "Request has invalid blueprint schema"
# — among them the em and en dash, U+2212 minus, the ellipsis, the no-break
# space, the bullet, the multiplication and division signs, Greek, Cyrillic,
# Arabic, CJK, emoji, control characters, and the ASCII characters ^ ` { } ~.
# The non-ASCII part is the character class the API's own regular expression
# names for the schema string, less the CJK, kana and hangul ranges, which that
# expression lists but the schema check still rejects, plus four characters the
# expression does not list but the check was measured to accept in this
# encoding: the right single quote, both double quotes and the zero-width space.
# They are kept rather than rewritten so that text which already synced is not
# changed (and its blueprint re-versioned). The left single quote was rejected.
BDA_TEXT_CHARS = frozenset(
    "\t\n\v\f\r"
    + "".join(chr(c) for c in range(0x20, 0x7F) if chr(c) not in "^`{}~")
    + "¡£¥§©ª®°±²³´µ¹¿"
    + "".join(chr(c) for c in range(0xC0, 0xD7))  # À-Ö
    + "".join(chr(c) for c in range(0xD8, 0xDD))  # Ø-Ü
    + "ß"
    + "".join(chr(c) for c in range(0xE0, 0xF7))  # à-ö
    + "".join(chr(c) for c in range(0xF8, 0xFD))  # ø-ü
    + "Œœ฿ẞ₹€™✓⑆⑇⑈⑉₽₱₦₣₩₫₺"
    + "\u2019\u201c\u201d\u200b"
)

# CreateBlueprint rejects a ``class`` longer than this many characters
# (measured: 60 accepted, 61 rejected; counted in characters, since 60
# two-byte characters were accepted). Blank and whitespace-only are rejected.
BDA_CLASS_MAX_LENGTH = 60

# Four to six upper-case hex digits naming a code point, so at most 10FFFF.
# The range is part of the pattern rather than checked after a match: text of
# the escape's shape above the range (``_x110000_``) must not match at all, or
# the substitution would consume its closing ``_`` and miss an escape starting
# there (``_x110000_x0041_``).
_ESCAPE = re.compile(r"_x([0-9A-F]{4,5}|0[0-9A-F]{5}|10[0-9A-F]{4})_")


def _escape_at_start(text: str) -> bool:
    """True when ``text`` begins with something ``from_blueprint_class`` decodes."""
    return _ESCAPE.match(text) is not None


def to_blueprint_class(name: str) -> str:
    """Return the blueprint ``class`` value that carries the class id ``name``.

    Each character outside ``BDA_TEXT_CHARS`` becomes ``_xHHHH_``. A literal
    ``_`` is escaped as ``_x005F_`` exactly when, left alone, it would start a
    sequence that ``from_blueprint_class`` decodes; the string is built from the
    end so that what follows each ``_`` is already known.
    """
    encoded = ""
    for ch in reversed(name):
        if ch not in BDA_TEXT_CHARS or (ch == "_" and _escape_at_start("_" + encoded)):
            encoded = f"_x{ord(ch):04X}_{encoded}"
        else:
            encoded = ch + encoded
    return encoded


def from_blueprint_class(value: str) -> str:
    """Return the class id a blueprint ``class`` value carries.

    The inverse of ``to_blueprint_class``. A value with no ``_xHHHH_`` sequence
    in it, which is every catalog blueprint's and every class id that never
    needed escaping, is returned unchanged.
    """

    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), value)
