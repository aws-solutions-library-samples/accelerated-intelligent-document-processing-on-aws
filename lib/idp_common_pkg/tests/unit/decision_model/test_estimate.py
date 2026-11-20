# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``estimate_tokens`` against what the deployed decider measured (#2051).

The live window check (``make stacktest-decision-window``) sent extraction
confidence requests cut to the default ``max_input_tokens`` of 3584, and the
decider refused three page shapes because the estimate of their text was about 20%
low: a markdown statement table and 17-character serials. The fixtures below are
those pages, their refused prefixes, and the counts the decider reported for them.
The counts for the text the new estimate keeps were measured with the pinned
decider model's own tokenizer, which reproduces the decider's reported state
counts for these pages to within the decider's fixed prompt of about 8 tokens.
"""

from __future__ import annotations

import hashlib

import pytest

from idp_common.decision_model import (
    DEFAULT_MAX_INPUT_TOKENS,
    estimate_tokens,
    fit_text,
    state_budget,
    truncate_to_tokens,
)
from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

pytestmark = pytest.mark.unit

#: The characters of the serials page, as the live check generates them.
SERIAL_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def _serial(seed: int) -> str:
    out = []
    for _ in range(17):
        seed = (seed * 1103515245 + 12345) % 2147483648
        out.append(SERIAL_ALPHABET[(seed >> 16) % len(SERIAL_ALPHABET)])
    return "".join(out)


def _page(style: str) -> str:
    """Page 1 of a live check style (``dense_page`` in the check's script)."""
    i_range = range(150)
    if style == "table":
        rows = ["| Date | Amount | Balance |", "|---|---|---|"]
        rows += [
            f"| {i % 28 + 1:02d}/11/2026 | {i * 7919 % 100000:,}.{i % 100:02d} | "
            f"{(i + 1) * 9973 % 1000000:,}.{i * 7 % 100:02d} |"
            for i in i_range
        ]
        return "\n".join(rows)
    if style == "codes":
        return "\n".join(_serial(7919 + i) for i in range(400))
    if style == "figures":
        return "\n".join(
            f"01-{i:05d} 2026-11-{i % 28 + 1:02d} "
            f"{i * 7919 % 100000:>9,}.{i % 100:02d} | {i * 31 % 997:03d}"
            for i in i_range
        )
    if style == "words":
        return "\n".join(
            f"Page 1 line {i}: " + "statement carried forward balance " * 12
            for i in range(24)
        )
    raise ValueError(style)


#: The first 16 hex digits of each page's SHA-256, so a change to a generator
#: above cannot leave the measured counts describing different text.
PAGE_SHA256 = {
    "table": "119a15aaa02078af",
    "codes": "411647d7dd548051",
    "figures": "50a95ef23e4cae5a",
    "words": "664bdeb40b5cdf3b",
}

#: The estimate of the longest confidence question the live check asked
#: (``account_number``'s), which every one of its confidence requests reserved.
QUESTION_ESTIMATE = 122.0

#: Each live confidence request the decider refused at 3584, as
#: ``(style, images, characters of state sent, state tokens and total tokens in
#: the refusal message, characters the new estimate keeps, tokenizer count of
#: those characters)``. The total less the state is what the request spends
#: outside its text (question, images, the decider's fixed prompt).
LIVE_REFUSALS = [
    ("table", 0, 4402, 4054, 4170, 3774, 3466),
    ("codes", 0, 5400, 4188, 4304, 4284, 3324),
    ("codes", 1, 5004, 3888, 4200, 3960, 3078),
]


@pytest.mark.parametrize("style", sorted(PAGE_SHA256))
def test_the_fixture_pages_are_the_text_the_decider_counted(style):
    digest = hashlib.sha256(_page(style).encode()).hexdigest()[:16]
    assert digest == PAGE_SHA256[style]


@pytest.mark.parametrize(
    "style, images, sent, state, total, _kept, _tokens", LIVE_REFUSALS
)
def test_each_refused_state_is_estimated_within_3_percent_of_the_decider_count(
    style, images, sent, state, total, _kept, _tokens
):
    """The decider counted this text; the estimate is no more than 3% below it.

    The estimate this replaces put these at 3447 to 3466 against 3888 to 4188.
    """
    assert estimate_tokens(_page(style)[:sent]) >= 0.97 * state


@pytest.mark.parametrize(
    "style, images, sent, state, total, kept, tokens", LIVE_REFUSALS
)
def test_each_refused_request_is_now_cut_to_fit_the_window(
    style, images, sent, state, total, kept, tokens
):
    budget = state_budget(DEFAULT_MAX_INPUT_TOKENS, QUESTION_ESTIMATE, images)
    text = fit_text(_page(style), budget, f"page 1 ({style})")
    assert len(text) < sent
    # The measured counts below are for exactly this cut; a change to the weights
    # moves it, and the tokenizer counts then have to be measured again.
    assert len(text) == kept
    assert tokens + (total - state) <= CONTEXT_WINDOW_TOKENS


def _previous_estimate(text: str) -> float:
    """The flat estimate #2149 shipped: digits and non-ASCII one, the rest half."""
    digits = sum(1 for c in text if "0" <= c <= "9")
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return digits + non_ascii + (len(text) - digits - non_ascii) / 2


PROSE = (
    "The account holder agrees that the statement balance carried forward "
    "reflects all transactions posted during the period. Payments received after "
    "the closing date will appear on the next statement.\n"
) * 50


def _longest_prefix(text: str, budget: float, estimate) -> int:
    """How many leading characters of ``text`` ``estimate`` fits in ``budget``."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


@pytest.mark.parametrize("text", [_page("words"), PROSE], ids=["words", "prose"])
def test_prose_is_estimated_and_cut_within_2_percent_of_the_previous_estimate(text):
    """Prose fitted before and still fits: only digit and punctuation text moves."""
    assert estimate_tokens(text) <= 1.02 * _previous_estimate(text)
    budget = state_budget(DEFAULT_MAX_INPUT_TOKENS, QUESTION_ESTIMATE, 0)
    previous = _longest_prefix(text, budget, _previous_estimate)
    assert previous < len(text)
    assert _longest_prefix(text, budget, estimate_tokens) >= 0.98 * previous


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("7", 1.0),
        ("|", 1.0),
        (",.", 2.0),
        ("ab", 1.0),
        ("AB", 1.0),
        ("A1B", 2.5),
        ("1HGCM", 4.0),
        (" \n", 1.0),
        ("日本", 2.0),
        ("ab-12", 4.0),
    ],
)
def test_each_character_class_is_weighted(text, tokens):
    assert estimate_tokens(text) == tokens


@pytest.mark.parametrize("style", ["table", "codes", "figures", "words"])
@pytest.mark.parametrize("budget", [50.0, 777.5, 2000.0])
def test_a_cut_never_holds_more_than_its_budget(style, budget):
    kept, dropped = truncate_to_tokens(_page(style), budget)
    assert dropped > 0
    assert estimate_tokens(kept) <= budget
