# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Keeping a request's text inside the decision model's window.

The decider refuses a request whose state, images and longest question do not
fit its window, and it never shortens the state itself. A caller therefore cuts
the state to fit before sending, against the configured ``max_input_tokens``.

This package has no tokenizer: the decider counts tokens only once a request
arrives. So every count here is an ESTIMATE from characters, weighted to err
high (see :func:`estimate_tokens`). Text past the cut is never sent, so the
decision model never reads it.
"""

from __future__ import annotations

import functools
import logging
import math
import re
from typing import Iterator, Sequence, Tuple

from idp_common.decision_model.invoker import DecisionModelRefused
from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

logger = logging.getLogger(__name__)

#: Reserved in every request for the decision model's own prompt around the state
#: and the question. ``max_input_tokens`` defaults to the window less this.
PROMPT_RESERVE_TOKENS = 512

#: The default ``max_input_tokens``: what one request may hold once the decider's
#: own prompt has its share of the window. ``idp_common.config.models`` states the
#: same number as ``DECISION_MODEL_MAX_INPUT_TOKENS``; a test keeps them equal.
DEFAULT_MAX_INPUT_TOKENS = CONTEXT_WINDOW_TOKENS - PROMPT_RESERVE_TOKENS

#: Counted for each image in a request. The decider reads a square page image at
#: ``IMAGE_MAX_SIDE`` as 196 tokens; the margin covers other shapes.
IMAGE_TOKENS = 256

#: How far back a cut may move to end at a line break: within the last tenth of
#: the prefix that fits. Further back, the cut falls mid-line, so text with no line
#: break near the cut (dense CJK, one long OCR line) keeps most of its budget
#: rather than backing up to a line break near its start (#2111).
LINE_BREAK_BACKOFF_SHARE = 0.1

#: The least share of a cut text's budget its kept evidence (the kept text less
#: every header line the caller names) must take. A cut that keeps less is refused rather than
#: sent, so a request is never answered from a header line alone (#2111).
MIN_EVIDENCE_SHARE = 0.5


#: Tokens counted for each character, by class. Every character not named here,
#: digits, ASCII punctuation and non-ASCII such as kana and common CJK
#: ideographs, counts as a whole token.
#: A letter in a run of letters and digits that holds a digit (a serial, an
#: invoice number, a hex id) is split by the tokenizer almost letter by letter.
MIXED_RUN_LETTER_TOKENS = 0.75
#: A letter in a run of letters only: words, all-capital headings included.
WORD_LETTER_TOKENS = 0.5
#: A letter in a run of letters only that does not read as a word (see
#: ``_reads_as_code``): a random code, a base64 chunk, a mixed-case token.
CODE_RUN_LETTER_TOKENS = 0.75
#: A CJK ideograph in neither GB 2312's nor JIS X 0208's first level, the
#: characters of everyday Chinese and Japanese text, but in Big5's first level
#: (common traditional Chinese characters).
TRADITIONAL_IDEOGRAPH_TOKENS = 1.5
#: Any other CJK ideograph of the Basic Multilingual Plane outside extension A:
#: rare name characters and surnames, GB 2312's second level.
RARE_IDEOGRAPH_TOKENS = 2.5
#: An ideograph of extension A (U+3400 to U+4DBF): three UTF-8 bytes, which the
#: tokenizer spells a token each.
EXTENSION_A_IDEOGRAPH_TOKENS = 3.0
#: An ideograph above the Basic Multilingual Plane (extension B onwards): four
#: UTF-8 bytes, a token each.
SUPPLEMENTARY_IDEOGRAPH_TOKENS = 4.0
#: A space, tab or line break.
WHITESPACE_TOKENS = 0.5

_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def _token_weights(text: str) -> Iterator[float]:
    """The estimated tokens of each character of ``text``, in order.

    Weighted from how the decider's tokenizer (a Qwen-family model in the v1
    release) splits text, measured with that model's own tokenizer and against
    the live decider (#2051): every digit is a token of its own, and so is ASCII
    punctuation (the pipes, slashes, commas and points of a markdown statement
    table); a letter beside digits in one run is nearly one; a non-ASCII
    character such as kana or a common CJK ideograph is about one. Word letters,
    which run three to five a token, are counted at two a token, and whitespace at
    ``WHITESPACE_TOKENS``. Two kinds of text the tokenizer splits finer are
    weighted up (#2212): letter-only runs that read as codes
    (``CODE_RUN_LETTER_TOKENS``) and CJK ideographs outside the everyday sets
    (``_ideograph_weight``).
    """
    position = 0
    for run in _ALNUM_RUN.finditer(text):
        yield from (_other_weight(c) for c in text[position : run.start()])
        if any("0" <= c <= "9" for c in run.group()):
            letter = MIXED_RUN_LETTER_TOKENS
        elif _reads_as_code(run.group()):
            letter = CODE_RUN_LETTER_TOKENS
        else:
            letter = WORD_LETTER_TOKENS
        for c in run.group():
            yield 1.0 if "0" <= c <= "9" else letter
        position = run.end()
    yield from (_other_weight(c) for c in text[position:])


def _other_weight(c: str) -> float:
    if c in " \t\n\r":
        return WHITESPACE_TOKENS
    return _ideograph_weight(c) if c >= "\u3400" else 1.0


_VOWELS = frozenset("aeiouyAEIOUY")


def _reads_as_code(run: str) -> bool:
    """Whether a run of letters only reads as a random code rather than a word.

    The tokenizer joins the letters of a word three to five a token, and splits a
    random string of letters one or two a token whatever its case. Three signs
    tell them apart: the case flips at least once every three letters after the
    first (``kQzRbT``; a capitalised or all-capital word never flips, and
    CamelCase of ordinary words flips about twice a word); five consonants in a
    row, which almost no word holds; or, in six letters or more, under a fifth
    vowels (``y`` counted as one; five letters with no vowel are five consonants
    already). About 0.3% of the letters of this repository's
    English documentation read as code by these signs, among them a few real
    words (``strengths``, ``rhythm``, ``eBay``), which are then overcounted.
    """
    flips = sum(1 for a, b in zip(run[1:], run[2:]) if a.isupper() != b.isupper())
    if len(run) >= 4 and 3 * flips >= len(run) - 1:
        return True
    consonants = longest = 0
    for c in run:
        consonants = 0 if c in _VOWELS else consonants + 1
        longest = max(longest, consonants)
    if longest >= 5:
        return True
    return len(run) >= 6 and 5 * sum(c in _VOWELS for c in run) < len(run)


def _in_first_level(c: str, codec: str, low: int, high: int) -> bool:
    """Whether ``c`` encodes in ``codec`` to a first two bytes in ``low``..``high``.

    A character outside the codec's double-byte set (EUC-JP's three-byte JIS X
    0212, which starts at 0x8F) falls below every range asked about.
    """
    try:
        encoded = c.encode(codec)
    except UnicodeEncodeError:
        return False
    return low <= (encoded[0] << 8 | encoded[1]) <= high


@functools.lru_cache(maxsize=None)
def _ideograph_weight(c: str) -> float:
    """The weight of a character at or above U+3400: by level, if an ideograph.

    Measured with the decider's tokenizer (#2212), a character of GB 2312's or
    JIS X 0208's first level is about one token, the further first-level
    characters of Big5 about 1.7, any other ideograph of the Basic Multilingual
    Plane two or three, and one of extension A three and of the supplementary
    blocks four: the tokenizer has no token for it and spells its UTF-8 bytes.
    No character of extension A or above is in any of the three codecs' sets.
    """
    point = ord(c)
    if not (
        0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        or 0x20000 <= point <= 0x323AF
    ):
        return 1.0
    if point >= 0x20000:
        return SUPPLEMENTARY_IDEOGRAPH_TOKENS
    if point <= 0x4DBF:
        return EXTENSION_A_IDEOGRAPH_TOKENS
    if _in_first_level(c, "gb2312", 0xB0A1, 0xD7FE) or _in_first_level(
        c, "euc_jp", 0xB0A1, 0xCFFE
    ):
        return 1.0
    if _in_first_level(c, "big5", 0xA440, 0xC67E):
        return TRADITIONAL_IDEOGRAPH_TOKENS
    return RARE_IDEOGRAPH_TOKENS


def estimate_tokens(text: str) -> float:
    """A conservative token count for ``text``, from its characters.

    The sum of each character's weight (see ``_token_weights``). Measured with
    the decider's tokenizer, it is within about 10% of the real count or above it
    on markdown and tab-separated statement tables, figures, phone numbers,
    serials and hex ids that mix letters and digits, letter-only random codes
    (mixed-case, base64-like, capital letters), common CJK and rare CJK
    characters (GB 2312's second level, rare surnames and name kanji), which
    ``PROMPT_RESERVE_TOKENS`` covers; on prose it is three to four times the real
    count (#2051, #2212).
    """
    return sum(_token_weights(text))


def state_budget(max_input_tokens: int, question_tokens: float, images: int) -> float:
    """The tokens of text a request can carry beside its question and images."""
    return max(0.0, max_input_tokens - question_tokens - images * IMAGE_TOKENS)


def truncate_to_tokens(text: str, budget: float) -> Tuple[str, int]:
    """``(kept, characters dropped)``: ``text`` cut from the end to fit ``budget``.

    Unchanged when it already fits, including empty text. Otherwise the longest
    prefix that fits is kept, ending at its last line break when that break lies
    in the prefix's last ``LINE_BREAK_BACKOFF_SHARE``, so an ordinary line is not
    sent half-read; with no line break there, the cut falls mid-line. Nothing
    past the cut is ever sent.
    """
    if estimate_tokens(text) <= budget:
        return text, 0
    used = 0.0
    end = 0
    for end, weight in enumerate(_token_weights(text)):
        used += weight
        if used > budget:
            break
    kept = text[:end]
    line_end = kept.rfind("\n")
    if (
        line_end > 0
        and len(kept) - (line_end + 1) <= len(kept) * LINE_BREAK_BACKOFF_SHARE
    ):
        kept = kept[: line_end + 1]
    return kept, len(text) - len(kept)


def require_room(
    max_input_tokens: int, question_tokens: float, images: int, what: str
) -> None:
    """Refuse locally a request whose question and images alone overflow the limit.

    The decider would refuse it anyway; refusing it here names the cause. With
    ``input: images`` no text is cut, so this is the only size check such a
    request gets before it is sent (#2111).
    """
    reserved = question_tokens + images * IMAGE_TOKENS
    if reserved > max_input_tokens:
        message = (
            f"Not sent: the request for {what} would be about {math.ceil(reserved)} "
            f"tokens for its question and {images} image(s) alone, over its "
            f"max_input_tokens of {max_input_tokens}. Shorten the question (or the "
            "field or class descriptions it carries), or send fewer pages."
        )
        logger.warning(message)
        raise DecisionModelRefused(message)


def fit_text(text: str, budget: float, what: str, headers: Sequence[str] = ()) -> str:
    """``text`` cut to ``budget`` for a request about to be sent; ``what`` names it.

    ``headers`` are lines in ``text`` that are not evidence, such as section
    classification's ``Page 1:`` and ``Page 2:`` lines. Each cut is logged at
    info. A cut is refused instead (:class:`DecisionModelRefused`, logged at
    warning) when the evidence it keeps, the kept text less every header, is blank
    or under ``MIN_EVIDENCE_SHARE`` of ``budget``, which includes text with content
    cut to nothing: the request's question and images already fill
    ``max_input_tokens``, and a request with little or no text would be answered
    from no evidence, which for an empty field is a confident "no value". Text that
    fits is returned unchanged, empty or not.
    """
    if isinstance(headers, str):
        raise TypeError("headers is a sequence of header lines, not one string")
    kept, dropped = truncate_to_tokens(text, budget)
    if dropped and _less(text, headers).strip():
        evidence = _less(kept, headers)
        if (
            not evidence.strip()
            or estimate_tokens(evidence) < budget * MIN_EVIDENCE_SHARE
        ):
            message = (
                f"Not sent: {what} would be cut to {len(evidence.strip())} "
                "characters of text, because the question and images already fill "
                "most of the request's max_input_tokens. Shorten the question (or "
                "the field or class descriptions it carries), raise "
                "max_input_tokens if it is below its default, or set input to "
                "images."
            )
            logger.warning(message)
            raise DecisionModelRefused(message)
    if dropped:
        logger.info(
            "Dropped %d of %d characters from the end of %s so the decision-model "
            "request fits max_input_tokens; the decision model does not read them",
            dropped,
            len(text),
            what,
        )
    return kept


def _less(text: str, headers: Sequence[str]) -> str:
    """``text`` with every occurrence of each of ``headers`` removed.

    So is a header the text ends partway through, as a cut can: ``Page 1:`` cut
    before its line break is no more evidence than the whole line.
    """
    for header in headers:
        if header:
            text = text.replace(header, "")
    for header in headers:
        for length in range(len(header) - 1, 0, -1):
            if text.endswith(header[:length]):
                return text[: len(text) - length]
    return text
