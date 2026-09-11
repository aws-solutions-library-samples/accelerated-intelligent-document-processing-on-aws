# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock's Converse ``usage`` block now carries structured members
(``cacheDetails``: a list of per-TTL cache-write breakdowns). Metering values
are summed and priced as numbers, so only numeric members may enter metering."""

import pytest

from idp_common.bedrock.client import numeric_usage
from idp_common.utils import merge_metering_data

pytestmark = pytest.mark.unit

USAGE = {
    "inputTokens": 3074,
    "outputTokens": 120,
    "totalTokens": 3194,
    "cacheReadInputTokens": 0,
    "cacheWriteInputTokens": 3074,
    "cacheDetails": [{"ttl": "5m", "inputTokens": 3074}],
}


def test_structured_members_are_dropped_and_counts_kept():
    assert numeric_usage(USAGE) == {
        "inputTokens": 3074,
        "outputTokens": 120,
        "totalTokens": 3194,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 3074,
    }


def test_booleans_and_strings_are_not_counts():
    assert numeric_usage({"inputTokens": 5, "truncated": True, "model": "x"}) == {
        "inputTokens": 5
    }


def test_non_dict_usage_yields_nothing():
    assert numeric_usage(None) == {}
    assert numeric_usage("usage") == {}


def test_two_calls_merge_without_the_list_breaking_the_sum():
    """The failure this guards: merge_metering_data summed int + list and logged
    'unsupported operand type(s)', then stored the list where a count belongs."""
    key = "Extraction/bedrock/us.anthropic.claude-sonnet-5"
    first = {key: {**numeric_usage(USAGE), "requests": 1}}
    second = {key: {**numeric_usage(USAGE), "requests": 1}}
    merged = merge_metering_data(first, second)
    assert merged[key]["inputTokens"] == 6148
    assert merged[key]["requests"] == 2
    assert "cacheDetails" not in merged[key]
    assert all(isinstance(v, (int, float)) for v in merged[key].values())
