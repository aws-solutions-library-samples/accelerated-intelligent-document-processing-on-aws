# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for document class id validation / sanitization."""

import pytest

from idp_common.config.class_names import (
    is_valid_class_name,
    sanitize_class_name,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        # Already-valid ids must be returned byte-identically. This is the
        # property that keeps existing BDA blueprints findable: any id that
        # produced a blueprint before must produce the same name now.
        ("Invoice", "Invoice"),
        ("Bank_Statement", "Bank_Statement"),
        ("W2-Form", "W2-Form"),
        ("form1040", "form1040"),
        ("A_B-c9", "A_B-c9"),
        # The reported failures: Discovery output with spaces.
        ("Task cards", "Task-cards"),
        ("Blank page", "Blank-page"),
        ("W2 Form", "W2-Form"),
        # Other punctuation a natural-language label can carry.
        ("Invoice (Final)", "Invoice-Final"),
        ("Payer's Statement", "Payer-s-Statement"),
        ("Form 1040 / Schedule C", "Form-1040-Schedule-C"),
        ("  Leading and trailing  ", "Leading-and-trailing"),
        ("Multiple    spaces", "Multiple-spaces"),
        ("-Already-hyphenated-", "Already-hyphenated"),
        # Nothing usable left — the caller decides what to do with "".
        ("???", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_sanitize_class_name(name, expected):
    assert sanitize_class_name(name) == expected


@pytest.mark.unit
def test_sanitize_is_idempotent():
    """Sanitizing twice must not drift, or repeat syncs would rename resources."""
    for name in ["Task cards", "Payer's Statement", "Bank_Statement", "a - b"]:
        once = sanitize_class_name(name)
        assert sanitize_class_name(once) == once


@pytest.mark.unit
def test_sanitize_output_is_always_valid_or_empty():
    for name in ["Task cards", "Invoice (Final)", "???", "", "Bank_Statement"]:
        result = sanitize_class_name(name)
        assert result == "" or is_valid_class_name(result)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Invoice", True),
        ("Bank_Statement", True),
        ("W2-Form", True),
        ("Task cards", False),
        ("Invoice.pdf", False),
        ("", False),
    ],
)
def test_is_valid_class_name(name, expected):
    assert is_valid_class_name(name) is expected
