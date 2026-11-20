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

import logging
import math
from typing import Sequence, Tuple

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


def estimate_tokens(text: str) -> float:
    """A conservative token count for ``text``, from its characters.

    Weighted for how the decider's tokenizer (a Qwen-family model in the v1
    release) splits text: every digit is a token of its own, which makes invoice
    and table text dense, and a non-ASCII character such as CJK is about one. Other
    text runs three to four characters a token; it is counted at two, which leaves
    room for punctuation-heavy OCR.
    """
    digits = sum(1 for c in text if "0" <= c <= "9")
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return digits + non_ascii + (len(text) - digits - non_ascii) / 2


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
    for end, c in enumerate(text):
        used += 1 if ("0" <= c <= "9" or ord(c) > 127) else 0.5
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
