# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The six Claude Sonnet 5 rows in ``config_library/pricing.yaml`` carry the price-list rates.

Those rows (``us.``, ``eu.``, ``global.`` and each one's ``:1m`` twin) carried
Claude Sonnet 4.6's rates — exactly 1.5x Sonnet 5's on every unit — so every
Sonnet 5 cost the stack reported was 1.5x the Bedrock-token bill
([#1830](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1830)).

**The expected rates are derived from AWS's price list, not typed here.** The
committed digest ``data/bedrock_govcloud_rates.json`` (built by
:mod:`bedrock_govcloud_rate_digest` from the public, unauthenticated
``AmazonBedrockFoundationModels`` offer file) records, per commercial region and
unit, the set of on-demand rates AWS publishes for "Claude Sonnet 5". That set has
exactly two members on every unit, the regional / geo-CRIS band and the cheaper
``global.`` band, so the ``us.``/``eu.`` rows must carry the larger and the
``global.`` rows the smaller. A transcription of a later offer file (version
20261109224312, published 2026-11-09) is pinned beside it, so a digest refresh
that moved a rate fails here naming both readings rather than silently moving the
expectation.
"""

from __future__ import annotations

import os
import re

import pytest
import yaml

from .bedrock_govcloud_rate_digest import load_digest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)
_PRICING_PATH = os.path.join(_REPO_ROOT, "config_library", "pricing.yaml")

#: Any pricing key naming Sonnet 5 itself: a bare model id or any routing prefix,
#: with or without a suffix such as ``:1m`` — but not ``claude-sonnet-5-5``.
_SONNET_5_KEY = re.compile(
    r"^bedrock/(?:[a-z-]+\.)?anthropic\.claude-sonnet-5(?::[^/]*)?$"
)

PRICE_LIST_MODEL = "Claude Sonnet 5"
UNITS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
COMMERCIAL_REGIONS = ("us-east-1", "us-west-2")

#: ``{pricing key: band}`` — every Sonnet 5 row the file carries. The universe is
#: re-derived from the file in ``test_the_row_set_is_the_six_sonnet_5_rows`` so a
#: seventh row cannot be added without being checked here.
ROWS = {
    "bedrock/us.anthropic.claude-sonnet-5": "regional",
    "bedrock/us.anthropic.claude-sonnet-5:1m": "regional",
    "bedrock/eu.anthropic.claude-sonnet-5": "regional",
    "bedrock/eu.anthropic.claude-sonnet-5:1m": "regional",
    "bedrock/global.anthropic.claude-sonnet-5": "global",
    "bedrock/global.anthropic.claude-sonnet-5:1m": "global",
}

#: Offer 20261109224312 (AmazonBedrockFoundationModels, published 2026-11-09,
#: retrieved 2026-11-12), per 1M tokens, 0-to-Inf range, identical in us-east-1,
#: us-east-2, us-west-1, us-west-2 and every eu-* region that sells the model.
OFFER_20261109224312_PER_MTOK = {
    "regional": {
        "inputTokens": 2.20,
        "outputTokens": 11.00,
        "cacheReadInputTokens": 0.22,
        "cacheWriteInputTokens": 2.75,
    },
    "global": {
        "inputTokens": 2.00,
        "outputTokens": 10.00,
        "cacheReadInputTokens": 0.20,
        "cacheWriteInputTokens": 2.50,
    },
}


def _bedrock_rows() -> dict[str, dict[str, float]]:
    with open(_PRICING_PATH) as handle:
        raw = yaml.safe_load(handle)
    return {
        entry["name"]: {
            unit["name"]: float(unit["price"]) for unit in entry.get("units") or []
        }
        for entry in raw["pricing"]
        if entry["name"].startswith("bedrock/")
    }


def _digest_band(unit: str) -> dict[str, float]:
    """``{"regional": rate, "global": rate}`` for one unit, from the committed digest."""
    by_region = load_digest()["models"][PRICE_LIST_MODEL]
    readings = {
        region: sorted(float(rate) for rate in by_region[region][unit])
        for region in COMMERCIAL_REGIONS
    }
    first = readings[COMMERCIAL_REGIONS[0]]
    assert all(rates == first for rates in readings.values()), readings
    assert len(first) == 2, (
        f"the digest records {first} for {PRICE_LIST_MODEL} {unit}; this test "
        "expects exactly a regional and a global band"
    )
    return {"global": first[0], "regional": first[1]}


@pytest.mark.unit
def test_the_row_set_is_the_six_sonnet_5_rows():
    found = {name for name in _bedrock_rows() if _SONNET_5_KEY.match(name)}
    assert found == set(ROWS)


@pytest.mark.unit
@pytest.mark.parametrize("unit", UNITS)
def test_the_committed_digest_agrees_with_the_current_offer_file(unit):
    band = _digest_band(unit)
    for name, rates in OFFER_20261109224312_PER_MTOK.items():
        assert band[name] == pytest.approx(rates[unit] / 1e6, rel=1e-9), (name, unit)


@pytest.mark.unit
@pytest.mark.parametrize("unit", UNITS)
@pytest.mark.parametrize("name", sorted(ROWS))
def test_every_sonnet_5_row_carries_the_published_rate(name, unit):
    expected = _digest_band(unit)[ROWS[name]]
    assert _bedrock_rows()[name][unit] == pytest.approx(expected, rel=1e-9)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(ROWS))
def test_no_sonnet_5_row_carries_sonnet_4_6_rates(name):
    """The specific defect: the row was a copy of the same-prefix Sonnet 4.6 row."""
    rows = _bedrock_rows()
    twin = name.replace("claude-sonnet-5", "claude-sonnet-4-6")
    for unit in UNITS:
        assert rows[name][unit] != rows[twin][unit], (name, unit)
