# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``fit_text``: the one place a request's text is cut to ``max_input_tokens``.

Both call sites (extraction confidence and section classification) send their
text through it, so the cut, its log line and the refusal of a request whose text
would be cut to nothing behave the same in both (#2032).
"""

from __future__ import annotations

import logging

import pytest

from idp_common.decision_model import (
    DecisionModelRefused,
    estimate_tokens,
    fit_text,
    truncate_to_tokens,
)

pytestmark = pytest.mark.unit


def test_text_that_fits_is_returned_unchanged_and_nothing_is_logged(caplog):
    with caplog.at_level(logging.INFO):
        assert (
            fit_text("A short page.\n", 100, "page 1's OCR text") == "A short page.\n"
        )
        assert fit_text("", 0, "page 2's OCR text") == ""
    assert caplog.records == []


def test_a_cut_keeps_the_head_and_is_logged_with_what_was_cut(caplog):
    first, second = "a" * 39 + "\n", "b" * 39 + "\n"
    text = first + second + "c" * 39 + "\n"
    with caplog.at_level(logging.INFO):
        kept = fit_text(text, estimate_tokens(first + "bb"), "page 4's OCR text")
    assert kept == first
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert f"Dropped {len(text) - len(kept)} of {len(text)}" in record.getMessage()
    assert "page 4's OCR text" in record.getMessage()


def test_text_cut_to_nothing_is_refused_not_returned(caplog):
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DecisionModelRefused) as raised,
    ):
        fit_text("Total due 100.00", 0, "page 1's OCR text")
    assert "page 1's OCR text" in str(raised.value)
    assert "max_input_tokens" in str(raised.value)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# --- How far a cut backs off, and what it must keep (#2111) --------------------


def _single_line(style: str) -> str:
    """A dense page from the window contract with its line breaks removed."""
    from tests.unit.decision_model.window_contract import dense_page

    return dense_page(style, 1).replace("\n", " ")


@pytest.mark.parametrize("style", ["figures", "cjk", "words"])
def test_text_with_no_line_break_near_the_cut_is_cut_mid_line(style):
    """A header line, then text with no line break: the cut keeps most of the
    budget instead of backing up to the header's line break."""
    from idp_common.decision_model.budget import LINE_BREAK_BACKOFF_SHARE

    header = "Page 1:\n"
    text = header + _single_line(style)
    budget = 3000.0
    assert estimate_tokens(text) > budget
    kept = fit_text(text, budget, "page 1's OCR text", [header])
    assert text.startswith(kept)
    assert estimate_tokens(kept) <= budget
    # Within one character's weight of the prefix that fits, less the back-off.
    assert estimate_tokens(kept) >= budget * (1 - LINE_BREAK_BACKOFF_SHARE) - 1


def test_a_cut_that_keeps_only_the_header_is_refused(caplog):
    header = "Page 1:\n"
    text = header + _single_line("words")
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DecisionModelRefused, match="max_input_tokens"),
    ):
        fit_text(text, estimate_tokens(header + "ab"), "pages 1", [header])
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_cut_whose_evidence_is_under_the_minimum_share_is_refused():
    from idp_common.decision_model.budget import MIN_EVIDENCE_SHARE

    header = "Page 1:\n"
    text = header + "x" * 200
    # The header takes most of this budget, so the text past it is under the share.
    small = estimate_tokens(header) / (1 - MIN_EVIDENCE_SHARE / 2)
    with pytest.raises(DecisionModelRefused):
        fit_text(text, small, "pages 1", [header])
    # With twice the header's size, the evidence is at least the share.
    roomy = estimate_tokens(header) / (1 - MIN_EVIDENCE_SHARE) * 2
    kept = fit_text(text, roomy, "pages 1", [header])
    assert kept.startswith(header + "x")


def test_a_header_only_text_that_fits_is_sent_unchanged():
    assert fit_text("Page 1:\n", 100, "pages 1", ["Page 1:\n"]) == "Page 1:\n"


def test_a_cut_that_keeps_only_headers_and_whitespace_is_refused():
    """An empty first page, then the second page's header: no header counts as
    evidence, wherever it falls in the kept text."""
    headers = ["Page 1:\n", "Page 2:\n"]
    text = "Page 1:\n\n\nPage 2:\n" + "x" * 200
    with pytest.raises(DecisionModelRefused):
        fit_text(
            text, estimate_tokens("Page 1:\n\n\nPage 2:\nab"), "pages 1, 2", headers
        )
    with pytest.raises(DecisionModelRefused):
        fit_text(text, estimate_tokens("Page 1:\n\n\nPage 2:\n"), "pages 1, 2", headers)


@pytest.mark.parametrize("budget", [0.5, 1, 2, 3, 4])
def test_a_cut_inside_a_header_line_is_refused(budget):
    """A budget that ends partway through "Page 1:" keeps a fragment of it, which
    is no more evidence than the whole line."""
    text = "Page 1:\n" + "Invoice total 100 " * 50
    with pytest.raises(DecisionModelRefused):
        fit_text(text, budget, "pages 1", ["Page 1:\n"])


def test_a_cut_inside_a_later_header_line_is_refused():
    headers = ["Page 1:\n", "Page 2:\n"]
    text = "Page 1:\n\n\nPage 2:\n" + "x" * 200
    with pytest.raises(DecisionModelRefused):
        fit_text(text, estimate_tokens("Page 1:\n\n\nPage 2"), "pages 1, 2", headers)


def test_headers_given_as_one_string_is_a_caller_error():
    with pytest.raises(TypeError):
        fit_text("Page 1:\n" + "x" * 50, 10, "pages 1", "Page 1:\n")


def test_text_that_is_only_headers_is_cut_like_empty_text_not_refused():
    """No page has text: nothing is lost by the cut, so it is not refused here."""
    headers = ["Page 1:\n", "Page 2:\n"]
    text = "Page 1:\n\n\nPage 2:\n"
    kept = fit_text(text, estimate_tokens("Page 1:\n"), "pages 1, 2", headers)
    assert text.startswith(kept) and kept != text


def test_a_question_and_images_over_the_limit_are_refused_locally(caplog):
    from idp_common.decision_model import IMAGE_TOKENS, require_room

    require_room(4 * IMAGE_TOKENS + 100, 100.0, 4, "pages 1, 2, 3, 4")
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DecisionModelRefused, match="pages 1, 2, 3, 4") as raised,
    ):
        require_room(4 * IMAGE_TOKENS + 100, 100.5, 4, "pages 1, 2, 3, 4")
    assert "4 image(s)" in str(raised.value)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_a_line_break_further_back_than_a_tenth_of_the_prefix_is_not_used():
    """The documented bound: a break within the last tenth ends the cut, one a
    fifth of the way back does not."""
    near = "a" * 95 + "\n" + "b" * 100
    kept, _ = truncate_to_tokens(near, estimate_tokens("a" * 95 + "\n" + "bbbb"))
    assert kept == "a" * 95 + "\n"
    far = "a" * 80 + "\n" + "b" * 100
    budget = estimate_tokens("a" * 80 + "\n" + "b" * 19)
    kept, _ = truncate_to_tokens(far, budget)
    assert kept == "a" * 80 + "\n" + "b" * 19


def test_evidence_under_half_the_budget_is_refused_and_over_half_is_sent():
    header = "H" * 40 + "\n"
    text = header + "x" * 200
    # Evidence about 30% of the budget: refused.
    with pytest.raises(DecisionModelRefused):
        fit_text(text, estimate_tokens(header + "x" * 18), "pages 1", [header])
    # Evidence about 60% of the budget: sent.
    kept = fit_text(text, estimate_tokens(header + "x" * 62), "pages 1", [header])
    assert kept == header + "x" * 62
