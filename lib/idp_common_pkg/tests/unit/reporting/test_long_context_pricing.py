# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A ``:1m`` model must be priced exactly as its base model is.

``:1m`` selects the 1M-token context window, which Anthropic prices at the model's
standard per-token rates on Claude 4.6 and later — every model offered with the
suffix here. ``config_library/pricing.yaml`` used to charge a 2x input / 1.5x
output premium on it (the rate card of the earlier Sonnet 4 / 4.5 1M beta, which
never applied to these models), which made a benchmark arm read $125.42 against
$68.82 for the same work — a 1.82x reported gap where the token volumes differed
by 1.06x (issue #899).

These tests pin the resulting contract — ``:1m`` costs exactly what the base model
costs, at any token volume — so that a premium cannot be reintroduced without
updating them deliberately.
"""

from pathlib import Path

import pytest
import yaml

from idp_common.config.models import IDPConfig
from idp_common.reporting.save_reporting_data import SaveReportingData

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]
_PRICING_YAML = _REPO_ROOT / "config_library" / "pricing.yaml"

LONG_CONTEXT_SUFFIX = ":1m"

# Three volumes spanning the 200K point the old premium claimed to switch at,
# including the largest single request measured in the issue. Cost must stay
# linear across all of them.
TOKEN_VOLUMES = [34_015, 200_000, 250_000]


def _entries() -> dict[str, dict[str, str]]:
    """``config_library/pricing.yaml`` as {entry name: {unit name: price}}."""
    data = yaml.safe_load(_PRICING_YAML.read_text(encoding="utf-8"))
    return {
        entry["name"]: {u["name"]: u["price"] for u in entry.get("units", [])}
        for entry in data["pricing"]
    }


_ENTRIES = _entries()
_LONG_CONTEXT_NAMES = sorted(
    name for name in _ENTRIES if name.endswith(LONG_CONTEXT_SUFFIX)
)


def test_the_long_context_entries_are_still_present() -> None:
    """They are the config-validation model list and the legacy pricing keys.

    ``idp_common.config.merge_utils._load_valid_bedrock_models`` builds the set
    of accepted model IDs from this file, so deleting a ``:1m`` entry would make
    every configuration naming that model fail validation. Metering rows written
    before the metering key stopped carrying the suffix also still need an exact
    price for it.
    """
    assert len(_LONG_CONTEXT_NAMES) >= 18, (
        f"expected the Claude :1m entries in {_PRICING_YAML.name}; "
        f"found {_LONG_CONTEXT_NAMES}"
    )
    # All three region blocks are covered, since the defect was in all three.
    for region in ("bedrock/us.", "bedrock/eu.", "bedrock/global."):
        assert any(name.startswith(region) for name in _LONG_CONTEXT_NAMES), (
            f"no :1m entry under {region}"
        )


@pytest.mark.parametrize("name", _LONG_CONTEXT_NAMES)
def test_long_context_rates_match_the_base_model(name: str) -> None:
    """Every unit of a ``:1m`` entry costs what the plain model's unit costs."""
    base = name[: -len(LONG_CONTEXT_SUFFIX)]
    assert base in _ENTRIES, f"{name} has no base entry {base} to be priced against"
    assert _ENTRIES[name] == _ENTRIES[base], (
        f"{name} is priced differently from {base}. The 1M context window is "
        "priced at the base model's standard per-token rates on every model "
        "offered with the suffix, so there is no premium to charge. "
        "See issue #899."
    )


@pytest.mark.parametrize("unit", ["inputTokens", "cacheReadInputTokens"])
@pytest.mark.parametrize("tokens", TOKEN_VOLUMES)
def test_no_premium_at_any_input_volume(unit: str, tokens: int) -> None:
    """Cost stays linear in tokens at every volume, with no band anywhere.

    There is no size threshold for these models, so a large request costs
    proportionally more and nothing more. If a genuinely banded model is ever
    priced here, this test is where that contract changes.
    """
    config = IDPConfig.model_validate(
        {
            "pricing": [
                {
                    "name": "bedrock/us.anthropic.claude-sonnet-5",
                    "units": [
                        {"name": "inputTokens", "price": "3.3E-6"},
                        {"name": "cacheReadInputTokens", "price": "3.3E-7"},
                    ],
                },
                {
                    "name": "bedrock/us.anthropic.claude-sonnet-5:1m",
                    "units": [
                        {"name": "inputTokens", "price": "3.3E-6"},
                        {"name": "cacheReadInputTokens", "price": "3.3E-7"},
                    ],
                },
            ]
        }
    )
    reporter = SaveReportingData("test-bucket", config=config)
    base_rate = reporter._get_unit_cost("bedrock/us.anthropic.claude-sonnet-5", unit)
    long_rate = reporter._get_unit_cost("bedrock/us.anthropic.claude-sonnet-5:1m", unit)

    # Not-None first: a missing entry returns None, which would otherwise make
    # the comparisons below a type error rather than a readable failure.
    assert base_rate is not None, f"no base rate for {unit}"
    assert long_rate is not None, f"no :1m rate for {unit}"
    assert base_rate > 0
    assert long_rate == base_rate
    assert tokens * long_rate == tokens * base_rate


def test_a_legacy_1m_metering_key_prices_at_the_standard_rate() -> None:
    """Metering written before this change still carries ``:1m`` in its key.

    So do the Athena data-mart rollups, which build the key from the raw
    configured model ID. It must resolve to that entry's own price — an exact
    match, not the substring fallback in ``_get_unit_cost`` and not 0.0 — and that
    price is now the base model's, so recomputing an old document's cost corrects
    it.
    """
    entries = _entries()
    config = IDPConfig.model_validate(
        {
            "pricing": [
                {
                    "name": name,
                    "units": [
                        {"name": unit, "price": price} for unit, price in units.items()
                    ],
                }
                for name, units in entries.items()
                if name.startswith("bedrock/")
            ]
        }
    )
    reporter = SaveReportingData("test-bucket", config=config)

    # ``_get_unit_cost`` takes the service_api half of a metering key, i.e.
    # everything after the step name: "bedrock/<model id>".
    for name in _LONG_CONTEXT_NAMES:
        base = name[: -len(LONG_CONTEXT_SUFFIX)]
        for unit in ("inputTokens", "outputTokens"):
            legacy = reporter._get_unit_cost(name, unit)
            plain = reporter._get_unit_cost(base, unit)
            assert legacy is not None, f"{name}/{unit} has no price entry"
            assert plain is not None, f"{base}/{unit} has no price entry"
            assert legacy > 0, f"{name}/{unit} priced at 0.0"
            assert legacy == plain, f"{name}/{unit} != {base}/{unit}"
