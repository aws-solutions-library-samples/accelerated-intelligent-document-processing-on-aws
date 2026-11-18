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

from idp_common.decision_model import DecisionModelRefused, estimate_tokens, fit_text

pytestmark = pytest.mark.unit


def test_text_that_fits_is_returned_unchanged_and_nothing_is_logged(caplog):
    with caplog.at_level(logging.INFO):
        assert (
            fit_text("A short page.\n", 100, "page 1's OCR text") == "A short page.\n"
        )
        assert fit_text("", 0, "page 2's OCR text") == ""
    assert caplog.records == []


def test_a_cut_keeps_the_head_and_is_logged_with_what_was_cut(caplog):
    text = "first line\nsecond line\nthird line\n"
    with caplog.at_level(logging.INFO):
        kept = fit_text(
            text, estimate_tokens("first line\nsecond li"), "page 4's OCR text"
        )
    assert kept == "first line\n"
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
