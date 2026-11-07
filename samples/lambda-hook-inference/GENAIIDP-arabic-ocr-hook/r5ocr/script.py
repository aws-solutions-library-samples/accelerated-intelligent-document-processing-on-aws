"""Decide whether a line is Arabic or English for box routing."""

from typing import Literal

_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
_ARABIC_INDIC_DIGITS = set("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")


def _is_arabic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _ARABIC_RANGES)


def classify(text: str) -> Literal["ar", "en", "none"]:
    letters = [c for c in text if c.isalpha()]
    if letters:
        arabic = sum(1 for c in letters if _is_arabic(c))
        return "ar" if arabic * 2 >= len(letters) else "en"
    digits = [c for c in text if c.isdigit()]
    if not digits:
        return "none"
    ar_digits = sum(1 for c in digits if c in _ARABIC_INDIC_DIGITS)
    return "ar" if ar_digits * 2 > len(digits) else "en"
