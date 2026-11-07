"""Restore digits PaddleOCR dropped from an Arabic line, using the transcription.

PaddleOCR's Arabic recognizer often drops digits (Arabic-Indic and Western) and
spacing. The full-page Bedrock transcription has them. A detector line is
replaced by the matching span of the transcription only when every character
other than digits and whitespace matches exactly, in order, on one
transcription line, at exactly one place. Anything else keeps the detector
text unchanged, so the result is never a different reading of the line - only
the same reading with its digits back.
"""

from __future__ import annotations

_MIN_KEY_CHARS = 4
_TATWEEL = "ـ"


def _ignored(ch: str) -> bool:
    return ch.isspace() or ch.isdigit() or ch == _TATWEEL


def _key(text: str) -> tuple[str, list[int]]:
    """The characters that must match, and each one's index in ``text``."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if not _ignored(ch):
            chars.append(ch)
            index.append(i)
    return "".join(chars), index


def snap_to_transcription(text: str, transcription: str) -> str:
    key, _ = _key(text)
    if len(key) < _MIN_KEY_CHARS:
        return text
    found: list[str] = []
    for line in transcription.splitlines():
        line_key, index = _key(line)
        start = line_key.find(key)
        while start != -1:
            found.append(line[index[start] : index[start + len(key) - 1] + 1])
            if len(found) > 1:
                return text  # ambiguous: the same reading occurs twice
            start = line_key.find(key, start + 1)
    if len(found) != 1:
        return text
    return found[0].strip()
