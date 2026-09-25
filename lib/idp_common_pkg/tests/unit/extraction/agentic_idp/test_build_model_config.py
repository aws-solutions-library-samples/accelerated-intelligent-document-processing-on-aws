# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for ``_build_model_config`` in ``idp_common.extraction.agentic_idp``.

These tests guard the fix for GitHub issue #312, where agentic extraction with
a Claude model id ending in ``:1m`` (1M context window beta) failed at
ConverseStream with::

    ValidationException: The provided model identifier is invalid.

The traditional ``bedrock/client.py::invoke_model`` path already strips the
``:1m`` suffix and forwards the ``anthropic_beta: ["context-1m-2025-08-07"]``
header via ``additionalModelRequestFields``. The agentic path must mirror
that behavior via Strands' ``additional_request_fields``.

Like the sibling tests in this directory, they run only when the real
``strands`` package is importable (see the directory-level ``conftest.py``).
"""

from __future__ import annotations

import pytest

try:
    from idp_common.extraction.agentic_idp import _build_model_config

    STRANDS_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    STRANDS_AVAILABLE = False

pytestmark = [
    pytest.mark.agentic,
    pytest.mark.skipif(
        not STRANDS_AVAILABLE,
        reason="strands-agents package not installed",
    ),
]


# ---------------------------------------------------------------------------
# ':1m' suffix handling: strip from model_id, set anthropic_beta header.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_model_id,expected_model_id",
    [
        ("us.anthropic.claude-opus-4-7:1m", "us.anthropic.claude-opus-4-7"),
        ("eu.anthropic.claude-opus-4-7:1m", "eu.anthropic.claude-opus-4-7"),
        (
            "global.anthropic.claude-opus-4-7:1m",
            "global.anthropic.claude-opus-4-7",
        ),
        ("us.anthropic.claude-opus-4-8:1m", "us.anthropic.claude-opus-4-8"),
        ("eu.anthropic.claude-opus-4-8:1m", "eu.anthropic.claude-opus-4-8"),
        (
            "global.anthropic.claude-opus-4-8:1m",
            "global.anthropic.claude-opus-4-8",
        ),
        ("us.anthropic.claude-opus-5:1m", "us.anthropic.claude-opus-5"),
        ("eu.anthropic.claude-opus-5:1m", "eu.anthropic.claude-opus-5"),
        (
            "global.anthropic.claude-opus-5:1m",
            "global.anthropic.claude-opus-5",
        ),
        ("us.anthropic.claude-opus-5-5:1m", "us.anthropic.claude-opus-5-5"),
        ("eu.anthropic.claude-opus-5-5:1m", "eu.anthropic.claude-opus-5-5"),
        (
            "global.anthropic.claude-opus-5-5:1m",
            "global.anthropic.claude-opus-5-5",
        ),
        ("us.anthropic.claude-opus-4-6-v1:1m", "us.anthropic.claude-opus-4-6-v1"),
        ("us.anthropic.claude-sonnet-4-6:1m", "us.anthropic.claude-sonnet-4-6"),
    ],
)
def test_1m_suffix_strips_and_sets_anthropic_beta(
    input_model_id: str, expected_model_id: str
) -> None:
    """`:1m` models pass the base id and the anthropic_beta header."""
    config = _build_model_config(
        input_model_id,
        max_tokens=None,
        max_retries=3,
        connect_timeout=10.0,
        read_timeout=300.0,
    )
    assert config["model_id"] == expected_model_id, (
        f"`:1m` suffix not stripped for {input_model_id!r}; got {config['model_id']!r}."
    )
    assert config["additional_request_fields"] == {
        "anthropic_beta": ["context-1m-2025-08-07"]
    }, (
        f"anthropic_beta header missing or wrong for {input_model_id!r}; got "
        f"{config.get('additional_request_fields')!r}."
    )


def test_no_1m_suffix_omits_additional_request_fields() -> None:
    """Non-`:1m` models must not gain an additional_request_fields key."""
    config = _build_model_config(
        "us.anthropic.claude-opus-4-7",
        max_tokens=None,
        max_retries=3,
        connect_timeout=10.0,
        read_timeout=300.0,
    )
    assert "additional_request_fields" not in config


def test_1m_caching_still_detected() -> None:
    """After stripping `:1m`, prompt + tool caching detection still works."""
    config = _build_model_config(
        "us.anthropic.claude-opus-4-7:1m",
        max_tokens=None,
        max_retries=3,
        connect_timeout=10.0,
        read_timeout=300.0,
    )
    assert config.get("cache_prompt") == "default"
    assert config.get("cache_tools") == "default"


def test_1m_does_not_inflate_max_tokens_for_claude4() -> None:
    """`:1m` Claude Opus 4.7+ model gets the 128K extended-output limit."""
    config = _build_model_config(
        "us.anthropic.claude-opus-4-7:1m",
        max_tokens=None,
        max_retries=3,
        connect_timeout=10.0,
        read_timeout=300.0,
    )
    assert config["max_tokens"] == 128_000
