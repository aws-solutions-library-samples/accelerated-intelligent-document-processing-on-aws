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
from typing import Tuple

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
    prefix that fits is kept, ending at its last line break when it has one, so
    no line is sent half-read. Nothing past the cut is ever sent.
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
    if line_end > 0:
        kept = kept[: line_end + 1]
    return kept, len(text) - len(kept)


def fit_text(text: str, budget: float, what: str) -> str:
    """``text`` cut to ``budget`` for a request about to be sent; ``what`` names it.

    Each cut is logged at info. Text that has content but would be cut to nothing
    is refused instead (:class:`DecisionModelRefused`, logged at warning): the
    request's question and images already fill ``max_input_tokens``, and a request
    with no text would be answered from no evidence, which for an empty field is a
    confident "no value". Empty text is returned unchanged.
    """
    kept, dropped = truncate_to_tokens(text, budget)
    if dropped and text.strip() and not kept.strip():
        message = (
            f"Not sent: {what} would be cut to nothing, because the question and "
            "images already fill the request's max_input_tokens. Raise "
            "max_input_tokens, shorten the question (or the field or class "
            "descriptions it carries), or set input to images."
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
