# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every model a GovCloud deployment can select is priced at the GovCloud band.

``config_library/pricing.yaml`` has one row per model id and no region axis, so a
row a GovCloud deployment resolves has to carry the GovCloud rates. Three rows
carried the commercial band instead, with a comment saying AWS published no
separate GovCloud per-token price. AWS publishes it, in the other offer file, and
it is higher — so every GovCloud cost this project reported was low by a fixed
margin: 8.3% on Claude Sonnet 4.5 and 16.7% on Nova Pro and Nova Lite, on both
the input and the output axis.
[#1404](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1404).

**The universe is derived from the shipped GovCloud preset, not from a list
written here.** The three models are today's instance; a fourth added to
``config_library/unified/lending-package-sample-govcloud/config.yaml`` is the
class, and it fails here until its pricing row is read against the price list.
That preset is the right source because it is the operative one: its header says
every model in it was confirmed invokable through GovCloud Converse, and the
``--govcloud`` template transform points a deployment at it.

What each test proves, and in particular what the *rate* assertions rest on, is a
committed digest of AWS's two public offer files
(:mod:`bedrock_govcloud_rate_digest`, unauthenticated, refreshable with
``--refresh``) rather than a constant restated here. A test that asserts a rate
against a number typed beside it proves the typing, which is the failure mode
worth naming: the rates these rows used to carry were also typed, from a true
reading of the wrong region.

Two further things are checked that a rate comparison alone would miss.

* **A corrected row and a never-wrong row look the same to a GovCloud-only
  check.** So the digest records the commercial band as well, and every unit
  where the two differ is asserted to carry the GovCloud one and *not* the
  commercial one. That is the regression this file exists to stop.
* **A unit AWS prices and the row omits is reported as an unpriced NULL cost**
  (#1206, #1212), which is right as a failure mode and wrong as a steady state.
  So a GovCloud-priced unit must be either priced in the row or a registered
  member of ``idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS`` — which
  is why correcting the rates and adding the two Claude cache units belong in one
  change.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest
import yaml

from idp_common.metering_units import EXPECTED_CACHE_UNIT_OMISSIONS

from .bedrock_cache_dimension_digest import load_digest as load_cache_digest
from .bedrock_govcloud_rate_digest import (
    GOVCLOUD_REGIONS,
    OFFER_URLS,
    REGIONS,
    digest_offer_files,
    load_digest,
    published_rate,
)

#: The price-list spelling of each model the GovCloud preset selects.
#:
#: The only authored thing in this module, and it is a translation rather than a
#: judgement: AWS's offer files name a product ("Nova Pro"), a configuration names
#: an invocation id (``amazon.nova-pro-v1:0``), and nothing published connects the
#: two. The set of keys is asserted equal to the set derived from the preset in
#: both directions, so this cannot silently cover fewer models than ship.
#:
#: Getting a spelling wrong here is the mistake to watch for — the sibling cache
#: register records ``nvidia.nemotron-nano-12b-v2`` mapping to "NVIDIA Nemotron
#: Nano 2 VL" while "NVIDIA Nemotron Nano 2" is a different product — and the
#: protection is that a wrong spelling has to make every rate below match anyway,
#: in two regions, on four units.
PRICE_LIST_MODELS = {
    "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0": "Claude Sonnet 4.5",
    "amazon.nova-pro-v1:0": "Nova Pro",
    "amazon.nova-lite-v1:0": "Nova Lite",
}

#: The commercial regions the digest records, for the counter-reading below.
COMMERCIAL_REGIONS = tuple(
    region for region in REGIONS if region not in GOVCLOUD_REGIONS
)

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)
_PRICING_PATH = os.path.join(_REPO_ROOT, "config_library", "pricing.yaml")
_GOVCLOUD_PRESET_PATH = os.path.join(
    _REPO_ROOT,
    "config_library",
    "unified",
    "lending-package-sample-govcloud",
    "config.yaml",
)

#: The ``cachePoint`` shapes the library is allowed to construct. A Converse cache
#: point carries ``type`` and nothing else — there is no TTL member — so a
#: one-hour-TTL cache write cannot be requested through this path, which is the
#: whole basis for pricing a cache write at the five-minute rate.
_CACHE_POINT_CALL = re.compile(r"CachePoint\(([^)]*)\)")
_CACHE_POINT_LITERAL = re.compile(r"[\"']cachePoint[\"']\s*:\s*(\{[^}]*\})")

#: What an argument list ``_CACHE_POINT_CALL`` or ``_CACHE_POINT_LITERAL``
#: captures is **allowed** to look like, read as the type-only cache point this
#: codebase builds. Anything else — a ``ttl`` kwarg, an extra positional, a
#: nested dict — is a cache point whose billing shape ``pricing.yaml`` does not
#: cover.
#:
#: The compact character class is deliberate: ``[^)]`` on the call pattern will
#: happily capture a comma-separated kwarg list, so ``"default" not in site[1]``
#: — the shape used to be — reads ``type="default", ttl="1h"`` as a pure default
#: and lets the one-hour spelling slip past.
_PERMITTED_CACHE_POINT = re.compile(
    r"^\{?\s*(?:[\"']?type[\"']?\s*[=:]\s*)?[\"']default[\"']\s*,?\s*\}?\s*$"
)


def _shipped_bedrock_units() -> dict[str, dict[str, str]]:
    """``{pricing key: {unit name: rate as the digest formats one}}``.

    Read from the YAML rather than from a loaded configuration object so the
    answer is about the file a deployment installs, which is what both cost
    implementations resolve against.
    """
    with open(_PRICING_PATH) as handle:
        raw = yaml.safe_load(handle)
    return {
        entry["name"]: {
            unit["name"]: f"{float(unit['price']):.6g}"
            for unit in (entry.get("units") or [])
        }
        for entry in raw["pricing"]
        if entry["name"].startswith("bedrock/")
    }


def _govcloud_preset_model_ids() -> set[str]:
    """Every model id the shipped GovCloud preset selects, from the file itself.

    Collected by walking the parsed preset and taking any string under a key whose
    name mentions a model, which is how this configuration spells all of them:
    ``model``, ``model_id``, ``escalation_model``, ``analysis_model_id``. Deriving
    it rather than listing it is the point — a fourth model added to the preset
    arrives here on its own.
    """
    with open(_GOVCLOUD_PRESET_PATH) as handle:
        raw = yaml.safe_load(handle)

    found: set[str] = set()

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for child_key, value in node.items():
                walk(value, str(child_key))
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif isinstance(node, str) and "model" in key.lower():
            found.add(node)

    walk(raw, "")
    return found


def _units_to_check() -> list[tuple[str, str, str]]:
    """``(model id, GovCloud region, unit)`` for every rate a shipped row states.

    The region axis is derived from the digest rather than assumed, because AWS
    sells Claude Sonnet 4.5 in both GovCloud regions and the two Nova models in
    ``us-gov-west-1`` only.
    """
    digest = load_digest()
    shipped = _shipped_bedrock_units()
    cases = []
    for model_id, price_list_model in sorted(PRICE_LIST_MODELS.items()):
        by_region = digest["models"].get(price_list_model, {})
        for region in GOVCLOUD_REGIONS:
            if region not in by_region:
                continue
            for unit in sorted(shipped.get(f"bedrock/{model_id}", {})):
                cases.append((model_id, region, unit))
    return cases


# --------------------------------------------------------------------------- #
# Guard the guard                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_govcloud_model_universe_is_derived_and_not_empty():
    """A parametrised suite over an empty list collects as skips, not failures.

    So the derivation is checked before anything rests on it: the preset walk has
    to find model ids, each has to have a pricing row, and one known id has to be
    among them so a walk returning three strings of something else still fails.
    """
    model_ids = _govcloud_preset_model_ids()
    assert len(model_ids) >= 3, (
        f"the GovCloud preset walk found {sorted(model_ids)} in "
        f"{_GOVCLOUD_PRESET_PATH}; the derivation is broken and every assertion "
        "in this module would hold over an empty universe"
    )
    assert "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0" in model_ids

    shipped = _shipped_bedrock_units()
    missing = sorted(
        model_id for model_id in model_ids if f"bedrock/{model_id}" not in shipped
    )
    assert not missing, (
        f"the GovCloud preset selects {missing}, which config_library/pricing.yaml "
        "has no row for. A GovCloud deployment using that model reports every "
        "call as unpriced, because save_reporting_data._get_unit_cost resolves by "
        "EXACT key"
    )

    # A floor on what the parametrisation gets, not on what the rows currently
    # price: the two token units on each of the three models in each GovCloud
    # region AWS sells it in is eight, and that is the derivation working. Pinning
    # today's twelve would turn adding a published rate into a red gate.
    assert len(_units_to_check()) >= 8, (
        "fewer than eight (model, GovCloud region, unit) cases were derived; the "
        "digest or the YAML walk is not returning what the rate assertions need"
    )


@pytest.mark.unit
def test_the_price_list_mapping_covers_exactly_the_preset_models():
    """Equality in both directions, so the mapping cannot narrow the universe.

    A model added to the preset and not mapped here would otherwise be checked by
    nothing, which is the state all three of these rows were in.
    """
    assert set(PRICE_LIST_MODELS) == _govcloud_preset_model_ids(), (
        "PRICE_LIST_MODELS and the models the shipped GovCloud preset selects "
        f"have diverged. Mapped: {sorted(PRICE_LIST_MODELS)}. In the preset: "
        f"{sorted(_govcloud_preset_model_ids())}. Add the price-list spelling of "
        "the new model, or remove the mapping for the one that has gone"
    )


@pytest.mark.unit
def test_the_committed_digest_describes_a_populated_govcloud_price_list():
    """An empty or broken digest would make every rate assertion below vacuous."""
    digest = load_digest()
    assert digest["model_count"] >= 10, (
        f"the digest describes {digest['model_count']} models sold in GovCloud; "
        "that is not the offer files, and the rate comparisons below would be "
        "about nothing"
    )
    assert len(digest["sources"]) == 2, (
        "the digest was built from one offer file. Anthropic's models are in "
        "AmazonBedrockFoundationModels and everything else is in AmazonBedrock, "
        "so a single-file digest carries no Claude GovCloud rate at all — which "
        "is the reading that produced the wrong rates in the first place"
    )

    for price_list_model in sorted(PRICE_LIST_MODELS.values()):
        assert price_list_model in digest["models"], (
            f"{price_list_model!r} is not a model the digest records a GovCloud "
            "rate for. Check the price-list spelling in PRICE_LIST_MODELS, or "
            "refresh the digest"
        )
        by_region = digest["models"][price_list_model]
        gov = [region for region in GOVCLOUD_REGIONS if region in by_region]
        assert gov, f"{price_list_model!r} has no GovCloud region in the digest"
        for region in gov:
            assert by_region[region]["token_dimension_count"] > 0, (
                f"the digest found no token-priced dimension for "
                f"{price_list_model!r} in {region}, so an empty rate list there "
                "is a failed read rather than a measured absence"
            )

    # One reading, not two: the sibling cache digest quotes the same offer files,
    # and config_library/pricing.yaml's prose cites their versions by number. A
    # refresh of one digest alone leaves three documents describing different
    # days, and a price is a dated fact.
    cache_digest = load_cache_digest()
    assert [source["offer_version"] for source in digest["sources"]] == [
        source["offer_version"] for source in cache_digest["sources"]
    ], (
        "this digest and bedrock_cache_dimension_digest were built from different "
        "offer file versions. Refresh both: "
        f"{[s['offer_version'] for s in digest['sources']]} against "
        f"{[s['offer_version'] for s in cache_digest['sources']]}"
    )
    assert digest["retrieved"] == cache_digest["retrieved"]


# --------------------------------------------------------------------------- #
# The rates themselves                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "region", "unit"),
    _units_to_check(),
    ids=lambda value: str(value),
)
def test_every_rate_on_a_govcloud_row_is_the_published_govcloud_rate(
    model_id: str, region: str, unit: str
):
    """The core assertion, per model, per GovCloud region, per priced unit."""
    digest = load_digest()
    price_list_model = PRICE_LIST_MODELS[model_id]
    shipped = _shipped_bedrock_units()[f"bedrock/{model_id}"][unit]
    expected = published_rate(digest, price_list_model, region, unit)

    assert expected is not None, (
        f"bedrock/{model_id} prices {unit} at {shipped}, but AWS publishes no "
        f"{unit} dimension for {price_list_model!r} in {region}. Either the unit "
        "does not belong on this row, or the digest needs refreshing"
    )
    assert shipped == expected, (
        f"bedrock/{model_id} charges {shipped} per token for {unit}; AWS "
        f"publishes {expected} for {price_list_model!r} in {region}. A GovCloud "
        f"cost report from this row is off by a factor of "
        f"{float(shipped) / float(expected):.4f} on that axis"
    )


@pytest.mark.unit
def test_no_govcloud_row_carries_the_commercial_rate():
    """The regression this file exists to stop, stated as its own assertion.

    A check that only knows the GovCloud rate cannot tell a corrected row from
    one that was never wrong, and cannot tell a reader *what* was wrong. So every
    unit where AWS prices GovCloud differently from commercial is asserted to
    carry the GovCloud rate and not to carry any commercial one.

    The non-vacuity guard matters here more than usual: if the two bands ever
    coincided on every unit, this test would pass over nothing while reading like
    the strongest assertion in the file.
    """
    digest = load_digest()
    shipped = _shipped_bedrock_units()
    differing = []
    wrong = []
    for model_id, price_list_model in sorted(PRICE_LIST_MODELS.items()):
        by_region = digest["models"][price_list_model]
        for unit, rate in sorted(shipped[f"bedrock/{model_id}"].items()):
            gov = {
                published_rate(digest, price_list_model, region, unit)
                for region in GOVCLOUD_REGIONS
                if region in by_region
            } - {None}
            commercial = {
                published
                for region in COMMERCIAL_REGIONS
                if region in by_region
                for published in by_region[region][unit]
            }
            if not gov or gov <= commercial:
                continue
            differing.append(f"{model_id}:{unit}")
            if rate in commercial - gov:
                wrong.append(
                    f"bedrock/{model_id} charges {rate} for {unit}, which is a "
                    f"COMMERCIAL rate for {price_list_model!r} "
                    f"({sorted(commercial)}) and not the GovCloud one "
                    f"({sorted(gov)})"
                )

    assert len(differing) >= 6, (
        "fewer than six units on the GovCloud rows are priced differently in "
        f"GovCloud than commercially ({differing}); this assertion needs a real "
        "divergence to be about anything"
    )
    assert not wrong, (
        "GovCloud pricing rows carrying a commercial rate — the defect in #1404: "
        + "; ".join(wrong)
    )


@pytest.mark.unit
def test_a_published_govcloud_rate_is_the_same_in_both_govcloud_regions():
    """One row cannot carry two regional rates, so a divergence must be reported.

    ``pricing.yaml`` is keyed on the model id alone. If AWS ever priced a model
    differently in ``us-gov-east-1`` and ``us-gov-west-1``, no single row could be
    right for both and the fix would be structural rather than a number — so this
    fails rather than letting one region's figure quietly stand for both.
    """
    digest = load_digest()
    shipped = _shipped_bedrock_units()
    both = []
    for model_id, price_list_model in sorted(PRICE_LIST_MODELS.items()):
        by_region = digest["models"][price_list_model]
        if not all(region in by_region for region in GOVCLOUD_REGIONS):
            continue
        both.append(model_id)
        for unit in sorted(shipped[f"bedrock/{model_id}"]):
            rates = {
                region: published_rate(digest, price_list_model, region, unit)
                for region in GOVCLOUD_REGIONS
            }
            assert len(set(rates.values())) == 1, (
                f"AWS prices {unit} differently across the GovCloud regions for "
                f"{price_list_model!r}: {rates}. config_library/pricing.yaml has "
                "one row per model id with no region axis, so neither figure is "
                "right for both regions and this needs a decision, not an edit"
            )

    assert both, (
        "no mapped model is sold in both GovCloud regions, so this assertion "
        "held over nothing. Claude Sonnet 4.5 was, when this was written"
    )


@pytest.mark.unit
def test_a_unit_aws_prices_in_govcloud_is_either_priced_or_registered():
    """An omitted unit AWS charges for is reported as NULL, which is not a resting state.

    ``classify_absent_unit`` reports a unit the matched row omits as an unpriced
    NULL cost unless ``EXPECTED_CACHE_UNIT_OMISSIONS`` declares it free (#1206,
    #1212). That is the right failure mode and the wrong steady state, so a
    GovCloud-priced unit has to be either on the row or a registered member with a
    basis — which is why #1404's rate correction and the two Claude cache units
    were one change.
    """
    digest = load_digest()
    shipped = _shipped_bedrock_units()
    registered = {
        (entry.pricing_key, entry.unit) for entry in EXPECTED_CACHE_UNIT_OMISSIONS
    }
    assert len(registered) >= 10, (
        f"EXPECTED_CACHE_UNIT_OMISSIONS holds {len(registered)} members; the "
        "import is wrong and this test would pass vacuously"
    )

    unaccounted = []
    for model_id, price_list_model in sorted(PRICE_LIST_MODELS.items()):
        pricing_key = f"bedrock/{model_id}"
        by_region = digest["models"][price_list_model]
        for region in GOVCLOUD_REGIONS:
            if region not in by_region:
                continue
            for unit, rates in sorted(by_region[region].items()):
                if unit == "token_dimension_count" or unit.startswith("cacheWrite1h"):
                    continue
                if not any(float(rate) > 0 for rate in rates):
                    continue
                if unit in shipped[pricing_key]:
                    continue
                if (pricing_key, unit) in registered:
                    continue
                unaccounted.append(f"{pricing_key}:{unit} (${rates} in {region})")

    assert not unaccounted, (
        "AWS prices these units in GovCloud, and the shipped row neither prices "
        f"them nor registers a basis for omitting them: {unaccounted}. Each one "
        "reaches the reporting table as an unpriced NULL cost. Add the published "
        "rate to config_library/pricing.yaml, or add a member to "
        "idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS saying what you "
        "read and why the unit cannot be metered here"
    )


# --------------------------------------------------------------------------- #
# The one-hour-TTL cache write                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_one_hour_cache_write_is_priced_because_none_can_be_requested():
    """Why there is no ``CacheWrite1h`` rate on any row, asserted rather than said.

    AWS publishes a one-hour-TTL cache write for Claude Sonnet 4.5 in both
    GovCloud regions at a materially higher rate than the five-minute one, and a
    cache write of either TTL is metered into the same ``cacheWriteInputTokens``
    counter — Bedrock reports the breakdown separately, in ``usage.cacheDetails``.
    So pricing that counter at the five-minute rate is correct only while a
    one-hour point cannot be requested, and that is a property of this code: a
    Converse cache point carries ``type`` and has no TTL member, and both
    construction sites in the library build ``type="default"``.

    If a one-hour point ever is requested, this fails at the construction site
    and the remedy is a pricing decision, not a nudge: the write rate on that row
    would be wrong by the gap this test measures.
    """
    digest = load_digest()
    five_minute = published_rate(
        digest, "Claude Sonnet 4.5", "us-gov-west-1", "cacheWriteInputTokens"
    )
    one_hour = published_rate(
        digest, "Claude Sonnet 4.5", "us-gov-west-1", "cacheWrite1hInputTokens"
    )
    assert five_minute and one_hour and five_minute != one_hour, (
        "AWS no longer publishes two different cache-write rates for Claude "
        f"Sonnet 4.5 in us-gov-west-1 (five-minute {five_minute}, one-hour "
        f"{one_hour}), so this test is about nothing. Re-read the price list "
        "before relaxing it"
    )

    with open(_PRICING_PATH) as handle:
        raw = yaml.safe_load(handle)
    priced_1h = sorted(
        f"{entry['name']}:{unit['name']}"
        for entry in raw["pricing"]
        for unit in (entry.get("units") or [])
        if "1h" in unit["name"].lower()
    )
    assert not priced_1h, (
        f"config_library/pricing.yaml prices a one-hour-TTL unit: {priced_1h}. "
        "Bedrock meters a cache write of either TTL into cacheWriteInputTokens, "
        "so a separate unit name is never matched and the rate is unreachable"
    )

    import idp_common

    package_root = os.path.dirname(os.path.abspath(idp_common.__file__))
    sites = []
    for directory, _subdirs, files in os.walk(package_root):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            relative = os.path.relpath(path, package_root)
            for arguments in _CACHE_POINT_CALL.findall(source):
                sites.append((relative, arguments.strip()))
            for literal in _CACHE_POINT_LITERAL.findall(source):
                sites.append((relative, literal.strip()))

    assert len(sites) >= 2, (
        f"found {len(sites)} cachePoint construction sites under {package_root}; "
        "there were two (bedrock/client.py and extraction/agentic_idp.py), so "
        "this scan is no longer reading what it claims to"
    )
    non_default = [site for site in sites if not _PERMITTED_CACHE_POINT.match(site[1])]
    assert not non_default, (
        f"cachePoint constructions that are not type='default': {non_default}. "
        "If one of them requests a one-hour TTL — or any other kwarg beyond the "
        "default type — the cacheWriteInputTokens rate on every Claude row is "
        f"now the wrong one; AWS prices the one-hour write at {one_hour} per "
        f"token against {five_minute}, so a shipped row metering the two at the "
        "same rate under-reports a one-hour-TTL write by 37.5%"
    )


# --------------------------------------------------------------------------- #
# The derivation itself                                                        #
# --------------------------------------------------------------------------- #


def _offer(offer_code: str, products: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The smallest structure ``digest_offer_files`` reads, for the probe below."""
    return {
        "offerCode": offer_code,
        "version": "1",
        "publicationDate": "2026-10-13T00:00:00Z",
        "products": {
            sku: {"attributes": attributes} for sku, attributes in products.items()
        },
        "terms": {
            "OnDemand": {
                sku: {
                    f"{sku}.term": {
                        "priceDimensions": {
                            f"{sku}.dim": {
                                "unit": attributes.pop("_unit", "1K tokens"),
                                "pricePerUnit": {"USD": attributes.pop("_usd")},
                            }
                        }
                    }
                }
                for sku, attributes in products.items()
            }
        },
    }


@pytest.mark.unit
def test_the_digest_extraction_reads_every_dimension_spelling():
    """The derivation, probed offline against a synthetic pair of offer files.

    ``--refresh`` is the only thing that normally runs ``digest_offer_files``, so
    without this the extraction rules are committed data whose producer nothing
    exercises — and the rules are where the mistakes are. Five of them matter:

    * **the three spellings.** The same dimension is
      ``NovaPro-cache-read-input-token-count`` in ``AmazonBedrock``,
      ``MP:UGW1_CacheReadInputTokenCount-Units`` on Claude Sonnet 4.5 and
      ``UGW1_cache_read_tokens_standard-Units`` on Claude Haiku 5.5, so matching
      is done on a ``-``/``_``/``:``-stripped lowercase form.
    * **one-hour before five-minute.** The one-hour spelling contains the
      five-minute one, in both of its forms, so reversing the two would fold a
      $7.20/MTok rate into the $4.50/MTok set.
    * **cache before input.** ``cache-read-input-token-count`` normalises to a
      string containing ``inputtoken``, so classification is first-hit-wins.
    * **the region axis**, since a rate recorded against the wrong region is
      exactly the defect being guarded, and a model sold in no GovCloud region is
      not recorded at all.
    * **the per-token conversion.** One file prices in ``1K tokens`` and the other
      in ``1M tokens``, a factor of a thousand apart, and a dimension priced in
      anything else — a TPM-hour, an image — must be dropped rather than read as
      a token rate.
    """
    bedrock = _offer(
        "AmazonBedrock",
        {
            "nova_gov": {
                "model": "Toy Nova",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-ToyNova-cache-read-input-token-count",
                "_usd": "0.0000180000",
            },
            "nova_commercial": {
                "model": "Toy Nova",
                "regionCode": "us-east-1",
                "usagetype": "USE1-ToyNova-cache-read-input-token-count",
                "_usd": "0.0000150000",
            },
            "nova_gov_flex": {
                "model": "Toy Nova",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-ToyNova-cache-read-input-token-count-flex",
                "_usd": "0.0009990000",
            },
            "nova_gov_image": {
                "model": "Toy Nova",
                "regionCode": "us-gov-west-1",
                "_unit": "image",
                "usagetype": "UGW1-ToyNova-cache-read-input-token-count",
                "_usd": "0.0400000000",
            },
            "nova_frankfurt": {
                "model": "Toy Nova",
                "regionCode": "eu-central-1",
                "usagetype": "EUC1-ToyNova-input-tokens",
                "_usd": "0.0000990000",
            },
            "commercial_only": {
                "model": "Toy Commercial Only",
                "regionCode": "us-east-1",
                "usagetype": "USE1-ToyCommercialOnly-input-tokens",
                "_usd": "0.0001000000",
            },
        },
    )
    anthropic = _offer(
        "AmazonBedrockFoundationModels",
        {
            "claude_write_1h": {
                "model": "Toy Claude",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-MP:UGW1_CacheWrite1hInputTokenCount-Units",
                "_unit": "1M tokens",
                "_usd": "7.2000000000",
            },
            "claude_write_5m": {
                "model": "Toy Claude",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-MP:UGW1_CacheWriteInputTokenCount-Units",
                "_unit": "1M tokens",
                "_usd": "4.5000000000",
            },
            "claude_write_1h_underscored": {
                "model": "Toy Claude",
                "regionCode": "us-gov-east-1",
                "usagetype": "UGE1-MP:UGE1_cache_write_tokens_1h_standard-Units",
                "_unit": "1M tokens",
                "_usd": "0.2400000000",
            },
            "claude_write_5m_underscored": {
                "model": "Toy Claude",
                "regionCode": "us-gov-east-1",
                "usagetype": "UGE1-MP:UGE1_cache_write_tokens_standard-Units",
                "_unit": "1M tokens",
                "_usd": "0.1500000000",
            },
            "claude_input_long_ctx": {
                "model": "Toy Claude",
                "regionCode": "us-gov-east-1",
                "usagetype": "UGE1-MP:UGE1_input_tokens_long_ctx_standard-Units",
                "_unit": "1M tokens",
                "_usd": "0.6000000000",
            },
            "claude_input": {
                "model": "Toy Claude",
                "regionCode": "us-gov-east-1",
                "usagetype": "UGE1-MP:UGE1_input_tokens_standard-Units",
                "_unit": "1M tokens",
                "_usd": "0.1200000000",
            },
            "claude_reserved": {
                "model": "Toy Claude",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-MP:UGW1_Reserved_1Month_InputTPM_Geo-Units",
                "_unit": "1M TPM Hour",
                "_usd": "0.2160000000",
            },
            # The same model as the other file carries, so the union is exercised.
            "nova_gov_output": {
                "model": "Toy Nova",
                "regionCode": "us-gov-west-1",
                "usagetype": "UGW1-ToyNova-output-tokens",
                "_usd": "0.0002880000",
            },
        },
    )

    digest = digest_offer_files([bedrock, anthropic])

    assert [source["offer_code"] for source in digest["sources"]] == [
        "AmazonBedrock",
        "AmazonBedrockFoundationModels",
    ]
    assert digest["model_count"] == 2, (
        "a model AWS sells in no GovCloud region was recorded, or the union "
        f"across the two files failed: {sorted(digest['models'])}"
    )
    assert "Toy Commercial Only" not in digest["models"]

    nova = digest["models"]["Toy Nova"]
    assert sorted(nova) == ["us-east-1", "us-gov-west-1"], (
        "the region axis is wrong: eu-central-1 should not be recorded and both "
        f"recorded regions should be, got {sorted(nova)}"
    )
    assert nova["us-gov-west-1"]["cacheReadInputTokens"] == ["1.8e-08"], (
        "the GovCloud cache read was not read, or the flex tier or the "
        "image-priced dimension leaked into it"
    )
    assert nova["us-east-1"]["cacheReadInputTokens"] == ["1.5e-08"], (
        "the commercial counter-reading was not recorded, so a corrected row and "
        "a never-corrected one would be indistinguishable"
    )
    assert nova["us-gov-west-1"]["outputTokens"] == ["2.88e-07"], (
        "the second offer file's row for the same model was not unioned in"
    )
    assert nova["us-gov-west-1"]["inputTokens"] == [], (
        "a cache-read dimension was classified as an input one as well. "
        "'cache-read-input-token-count' normalises to a string containing "
        "'inputtoken', so classification has to be first-hit-wins"
    )
    assert nova["us-gov-west-1"]["token_dimension_count"] == 3, (
        "token_dimension_count is what makes an empty rate list readable as a "
        "measured absence rather than a failed read, so it counts every "
        "token-priced dimension including the off-tier ones"
    )

    claude = digest["models"]["Toy Claude"]
    assert claude["us-gov-west-1"]["cacheWriteInputTokens"] == ["4.5e-06"], (
        "the one-hour cache write was folded into the five-minute set: the "
        "one-hour spelling contains the five-minute one, so it has to be "
        "classified first"
    )
    assert claude["us-gov-west-1"]["cacheWrite1hInputTokens"] == ["7.2e-06"]
    assert claude["us-gov-east-1"]["cacheWriteInputTokens"] == ["1.5e-07"], (
        "the underscored spelling ('cache_write_tokens_standard', which Claude "
        "Haiku 5.5 uses) was not matched, or the underscored one-hour spelling "
        "was folded into it"
    )
    assert claude["us-gov-east-1"]["cacheWrite1hInputTokens"] == ["2.4e-07"]
    assert claude["us-gov-east-1"]["inputTokens"] == ["1.2e-07"], (
        "the long-context band leaked into the standard input rate, or the "
        "underscored input spelling was not matched"
    )
    assert claude["us-gov-west-1"]["inputTokens"] == [], (
        "a reserved-throughput dimension priced per TPM-hour was read as a token rate"
    )


@pytest.mark.unit
@pytest.mark.skipif(
    os.environ.get("CHECK_AWS_PRICE_LIST") != "1",
    reason="needs network egress; set CHECK_AWS_PRICE_LIST=1 to re-derive live",
)
def test_the_live_price_list_still_matches_the_committed_digest():
    """Re-derive from AWS today rather than from the committed digest.

    Opt-in and not a gate, for the reason ``check_markdown_links.py`` never
    fetches an ``http`` URL: a blocking check that needs egress red-lines the
    branch on somebody else's outage. The same switch both sibling digests use, so
    one command re-checks all three.

    What it answers is the question nothing else here can: the committed digest is
    a reading of two files on one day, and AWS republishes them. The *versions*
    are expected to move and that is not a failure; what must not have changed
    silently is a GovCloud **rate**, because that is a live cost under-report in
    whichever direction it moved.
    """
    import urllib.request

    offers = []
    for url in OFFER_URLS:
        with urllib.request.urlopen(url, timeout=600) as handle:  # noqa: S310
            offers.append(json.load(handle))
    live = digest_offer_files(offers)
    committed = load_digest()

    moved = {
        source["offer_code"]: (source["offer_version"], live_source["offer_version"])
        for source, live_source in zip(committed["sources"], live["sources"])
        if source["offer_version"] != live_source["offer_version"]
    }
    if moved:
        print(f"offer files republished since the digest was taken: {moved}")

    for model_id, price_list_model in sorted(PRICE_LIST_MODELS.items()):
        assert price_list_model in live["models"], (
            f"{price_list_model!r} is no longer sold in GovCloud according to "
            f"AWS's price list; bedrock/{model_id} needs re-reading"
        )
        for region in GOVCLOUD_REGIONS:
            if region not in committed["models"][price_list_model]:
                continue
            for unit in sorted(_shipped_bedrock_units()[f"bedrock/{model_id}"]):
                assert published_rate(
                    live, price_list_model, region, unit
                ) == published_rate(committed, price_list_model, region, unit), (
                    f"AWS's published {unit} rate for {price_list_model!r} in "
                    f"{region} has changed. Refresh the digest with "
                    "bedrock_govcloud_rate_digest.py --refresh and correct "
                    "config_library/pricing.yaml"
                )
