# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which writing systems a piece of text uses, counted from its letters.

This names *scripts* (Arabic, Cyrillic, Han, ...), never languages: a script is a
property of the characters, which can be counted, while a language would have to be
inferred, which the fit check does not do. Only letters are counted, so digits,
punctuation and symbols in any script are ignored.
"""

from __future__ import annotations

import unicodedata
from typing import Dict

#: Unicode character-name prefixes, mapped to the script they belong to. The name
#: of every letter in these blocks starts with one of them.
_SCRIPT_PREFIXES = (
    ("LATIN", "Latin"),
    ("CJK", "Han"),
    ("HIRAGANA", "Hiragana"),
    ("KATAKANA", "Katakana"),
    ("HANGUL", "Hangul"),
    ("ARABIC", "Arabic"),
    ("HEBREW", "Hebrew"),
    ("CYRILLIC", "Cyrillic"),
    ("GREEK", "Greek"),
    ("DEVANAGARI", "Devanagari"),
    ("BENGALI", "Bengali"),
    ("GURMUKHI", "Gurmukhi"),
    ("GUJARATI", "Gujarati"),
    ("TAMIL", "Tamil"),
    ("TELUGU", "Telugu"),
    ("KANNADA", "Kannada"),
    ("MALAYALAM", "Malayalam"),
    ("THAI", "Thai"),
    ("LAO", "Lao"),
    ("KHMER", "Khmer"),
    ("MYANMAR", "Myanmar"),
    ("GEORGIAN", "Georgian"),
    ("ARMENIAN", "Armenian"),
    ("ETHIOPIC", "Ethiopic"),
    ("SINHALA", "Sinhala"),
    ("TIBETAN", "Tibetan"),
)

#: How many letters of one script must be seen before it counts as observed, so a
#: stray symbol, a currency sign spelt as a letter or one foreign name does not.
MIN_SCRIPT_LETTERS = 20


def count_scripts(text: str) -> Dict[str, int]:
    """Letters per script in ``text``; a letter in no listed block is ``Other``."""
    counts: Dict[str, int] = {}
    for char in text or "":
        if not unicodedata.category(char).startswith("L"):
            continue
        name = unicodedata.name(char, "")
        script = next(
            (label for prefix, label in _SCRIPT_PREFIXES if name.startswith(prefix)),
            "Other",
        )
        counts[script] = counts.get(script, 0) + 1
    return counts


def observed_non_latin(counts: Dict[str, int]) -> Dict[str, int]:
    """The non-Latin scripts with at least ``MIN_SCRIPT_LETTERS`` letters."""
    return {
        script: n
        for script, n in sorted(counts.items())
        if script != "Latin" and n >= MIN_SCRIPT_LETTERS
    }


def merge_counts(*many: Dict[str, int]) -> Dict[str, int]:
    """Sum several ``count_scripts`` results."""
    total: Dict[str, int] = {}
    for counts in many:
        for script, n in (counts or {}).items():
            total[script] = total.get(script, 0) + n
    return total
