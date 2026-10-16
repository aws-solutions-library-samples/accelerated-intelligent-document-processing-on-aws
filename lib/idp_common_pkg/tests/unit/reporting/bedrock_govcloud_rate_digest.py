# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What AWS publishes per token for a Bedrock model **in a named region**.

``config_library/pricing.yaml`` has one row per model id, with no region axis, so
a row that a GovCloud deployment resolves has to carry the GovCloud band. Three
rows did not: they carried the commercial band, and a GovCloud cost report built
from them was 8.3% low on Claude Sonnet 4.5 and 16.7% low on Nova Pro and Nova
Lite — see
[#1404](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1404).
This module is the derivation of the rates those rows now carry, so the next
reader can re-run it rather than trusting three numbers somebody typed.

It is deliberately separate from its two siblings, which answer questions that do
not need a region axis and would be muddied by one. :mod:`bedrock_price_list_digest`
answers whether ``totalTokens`` is a billing dimension anywhere in Bedrock.
:mod:`bedrock_cache_dimension_digest` answers whether a model has a prompt-cache
dimension **at all**, and unions every region into one rate set per model on
purpose — which is the right shape for that question and useless for this one,
since "$3.60/MTok is a rate AWS publishes for Claude Sonnet 4.5" is true of the
EU band as well and says nothing about GovCloud. Each digest carries its own
numbers and refreshes alone.

Two offer files are needed, not one: AWS publishes Amazon's own models (Nova),
Meta's Llama and the Bedrock Marketplace models under ``AmazonBedrock``, and
Anthropic's Claude models under ``AmazonBedrockFoundationModels``. Reading only
the first finds no Claude GovCloud rate and would make "AWS publishes no separate
GovCloud price" look like a reading of a complete record when the record is in
the other file. That mistake is what the three wrong rows rested on.

Both files are public and **unauthenticated** — no credential, no ``aws`` CLI, no
Pricing API call, which is why this is reachable from a test machine at all. The
test that consumes the digest runs offline against the committed copy, because a
blocking gate that needs egress red-lines the branch on somebody else's outage;
``--refresh`` re-fetches and rewrites it.

    python3 lib/idp_common_pkg/tests/unit/reporting/bedrock_govcloud_rate_digest.py --refresh

The two offer files are ~18 MB and ~10 MB, so the digest keeps one small record
per (model, region) rather than the files.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
from typing import Any

#: AWS's public offer files. ``AmazonBedrock`` carries Amazon's own models and the
#: Bedrock Marketplace ones; ``AmazonBedrockFoundationModels`` carries Anthropic's.
OFFER_URLS = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrock/current/index.json",
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrockFoundationModels/current/index.json",
)

DIGEST_PATH = os.path.join(
    os.path.dirname(__file__), "data", "bedrock_govcloud_rates.json"
)

#: The regions recorded, and the reason there are four rather than two. The two
#: GovCloud regions are what a GovCloud pricing row has to match. The two
#: commercial ones are the counter-reading: a row carrying the commercial band is
#: exactly the defect #1404 is about, and a check that only knows the GovCloud
#: rate cannot tell a corrected row from one that was never wrong. Recording both
#: lets the test assert the row matches GovCloud **and** differs from commercial
#: wherever AWS prices them differently.
REGIONS = ("us-gov-east-1", "us-gov-west-1", "us-east-1", "us-west-2")

#: The GovCloud regions among :data:`REGIONS`, in the same order.
GOVCLOUD_REGIONS = ("us-gov-east-1", "us-gov-west-1")

#: The metering units this digest records published rates for, and the shapes AWS
#: spells the corresponding dimension in. Both offer files are matched
#: case-insensitively with ``-``/``_``/``:``/whitespace removed first, because the
#: same dimension appears three different ways across the files and across model
#: generations: ``NovaLite-cache-read-input-token-count``,
#: ``MP:UGW1_CacheReadInputTokenCount-Units`` and
#: ``UGW1_cache_read_tokens_standard-Units``.
#:
#: ⚠️ **The order is load-bearing and this dict is matched in it**, first hit
#: wins, for two separate reasons.
#:
#: ``cache-read-input-token-count`` normalises to a string *containing*
#: ``inputtoken``, so a rule that tested the input pattern first — or tested all
#: five independently — would count every cache dimension as an input one as
#: well. That is not hypothetical: it is the error that put ``inputTokens`` at
#: 5,580 published products instead of 4,909 in ``bedrock_price_list_digest`` for
#: #1212.
#:
#: And ``cacheWrite1hInputTokens`` must be tested before ``cacheWriteInputTokens``,
#: because the one-hour spelling contains the five-minute one. Reversing those two
#: would fold a $7.20/MTok rate into the $4.50/MTok five-minute set and leave the
#: cache-write rate on Claude Sonnet 4.5 ambiguous between the two — which the
#: consuming test would catch, since it requires one rate per (model, region,
#: unit), but it would catch it as "AWS publishes two rates" rather than as the
#: reading error it is.
#:
#: ``cacheWrite1hInputTokens`` is not a metering unit this project can ever emit
#: and no pricing row prices it — a Converse ``cachePoint`` carries ``type`` and
#: no TTL. It is recorded because that claim is only worth something next to the
#: number it is declining: AWS charges $7.20/MTok for a one-hour write against
#: $4.50/MTok for a five-minute one, 60% more per token, so pricing a one-hour
#: write at the five-minute rate would under-report it by 37.5%. The
#: consuming test asserts the two rates are still different so the claim stays
#: live rather than vacuous.
DIMENSION_PATTERNS = {
    "cacheWrite1hInputTokens": r"cachewrite(tokens)?1h",
    "cacheReadInputTokens": r"cacheread",
    "cacheWriteInputTokens": r"cachewrite",
    "inputTokens": r"inputtoken",
    "outputTokens": r"outputtoken",
}

#: Dimension variants dropped from the per-region rate sets, each because this
#: project never bills against it. ``batch``/``flex``/``priority`` are separate
#: service tiers with their own usage types; ``longctx`` is the long-context band
#: (see the note on the Haiku 5.5 rows in ``pricing.yaml``); ``custommodel`` and
#: ``provisioned`` are not on-demand inference at all; ``reserved`` is a
#: throughput commitment priced per TPM-hour rather than per token.
#:
#: A plain string rather than a container, like its counterpart in
#: ``bedrock_cache_dimension_digest``: the pattern is one regex, and the thing a
#: reviewer needs to see is which tiers it names.
SERVICE_TIER_VARIANTS = r"batch|flex|priority|longctx|custommodel|provisioned|reserved"

_PER_UNIT_DIVISOR = {"1K tokens": 1_000.0, "1M tokens": 1_000_000.0}


def _normalise(text: str) -> str:
    return re.sub(r"[-_:\s]", "", text).lower()


def _per_token(unit: str, price: str) -> str | None:
    """A price dimension's rate expressed per single token, or ``None``.

    Returns a string rather than a float so the committed digest is byte-stable
    and a recorded rate can be compared for equality without worrying about
    float repr. The format is the one ``config_library/pricing.yaml``'s own rates
    reduce to under ``f"{float(price):.6g}"``, which is what lets the consuming
    test compare a shipped rate to a published one directly. ``None`` for any
    dimension not priced in tokens — an image, a model-hour, a TPM-hour — which
    is how non-token dimensions are dropped.
    """
    divisor = _PER_UNIT_DIVISOR.get(unit)
    if divisor is None:
        return None
    return f"{float(price) / divisor:.6g}"


def digest_offer_files(offers: list[dict[str, Any]]) -> dict[str, Any]:
    """One record per (price-list model, recorded region), unioned across the files.

    For each model and each region in :data:`REGIONS`, the per-token rates AWS
    publishes for each dimension in :data:`DIMENSION_PATTERNS`, plus the count of
    token-priced dimensions of any kind in that region. That last number is what
    makes an empty rate list readable: a model with 14 token dimensions in a
    region and no cache dimension is a measured absence, while a model with 0 of
    either means the extraction found nothing there and the absence proves
    nothing.

    Only models AWS prices in at least one GovCloud region are recorded. A model
    sold nowhere in GovCloud cannot be the subject of a GovCloud pricing row, and
    recording all 141 of them in four regions would make the committed file
    twenty times the size for no question it answers.

    Each dimension row is classified **once**, by the first pattern that matches
    — see the warning on :data:`DIMENSION_PATTERNS`, which is why this is a loop
    with a ``break`` rather than five independent tests.
    """
    off_tier = re.compile(SERVICE_TIER_VARIANTS)
    dimension_res = {
        unit: re.compile(pattern) for unit, pattern in DIMENSION_PATTERNS.items()
    }

    models: dict[str, dict[str, dict[str, Any]]] = {}
    sources = []
    for offer in offers:
        sources.append(
            {
                "offer_code": offer.get("offerCode"),
                "offer_version": offer.get("version"),
                "publication_date": offer.get("publicationDate"),
                "product_count": len(offer["products"]),
            }
        )
        products = offer["products"]
        for by_sku in offer["terms"].values():
            for sku, sku_offers in by_sku.items():
                attributes = products.get(sku, {}).get("attributes", {})
                model = attributes.get("model")
                region = attributes.get("regionCode")
                if not model or region not in REGIONS:
                    continue
                usagetype = _normalise(attributes.get("usagetype", ""))
                token_type = _normalise(attributes.get("tokenType", ""))
                record = models.setdefault(model, {}).setdefault(
                    region,
                    {
                        "token_dimension_count": 0,
                        **{unit: set() for unit in DIMENSION_PATTERNS},
                    },
                )
                for term in sku_offers.values():
                    for dimension in term["priceDimensions"].values():
                        rate = _per_token(
                            dimension["unit"], dimension["pricePerUnit"]["USD"]
                        )
                        if rate is None:
                            continue
                        record["token_dimension_count"] += 1
                        if off_tier.search(usagetype) or off_tier.search(token_type):
                            continue
                        for unit, pattern in dimension_res.items():
                            if pattern.search(usagetype) or pattern.search(token_type):
                                record[unit].add(rate)
                                break

    sold_in_govcloud = {
        model: by_region
        for model, by_region in models.items()
        if any(region in by_region for region in GOVCLOUD_REGIONS)
    }

    return {
        "sources": sources,
        "retrieved": datetime.date.today().isoformat(),
        "model_count": len(sold_in_govcloud),
        "models": {
            model: {
                region: {
                    "token_dimension_count": record["token_dimension_count"],
                    **{unit: sorted(record[unit]) for unit in DIMENSION_PATTERNS},
                }
                for region, record in sorted(by_region.items())
            }
            for model, by_region in sorted(sold_in_govcloud.items())
        },
    }


def _refresh() -> None:
    import urllib.request

    offers = []
    for url in OFFER_URLS:
        with urllib.request.urlopen(url, timeout=600) as fh:  # noqa: S310
            offers.append(json.load(fh))
    digest = digest_offer_files(offers)
    os.makedirs(os.path.dirname(DIGEST_PATH), exist_ok=True)
    with open(DIGEST_PATH, "w") as fh:
        json.dump(digest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {DIGEST_PATH}: {digest['model_count']} GovCloud-sold models")
    for source in digest["sources"]:
        print(
            f"  {source['offer_code']} version {source['offer_version']} "
            f"({source['product_count']} products)"
        )


def load_digest() -> dict[str, Any]:
    with open(DIGEST_PATH) as fh:
        return json.load(fh)


def published_rate(
    digest: dict[str, Any], model: str, region: str, unit: str
) -> str | None:
    """The one per-token rate the digest records, or ``None`` if there is none.

    Raises ``KeyError`` for a model the digest does not describe, deliberately: a
    pricing row mapped to a model AWS's price list does not sell in GovCloud is
    not a weaker claim, it is an unread one, and the empty answer it would
    otherwise give reads as "AWS publishes no GovCloud rate" — which is the exact
    false reading #1404 is about.

    Raises ``ValueError`` if the digest records more than one rate, because the
    caller's question ("what does this row have to charge?") has no answer then
    and silently taking the first would make a reading error look like a rate.
    """
    rates = digest["models"][model].get(region, {}).get(unit, [])
    if len(rates) > 1:
        raise ValueError(
            f"{model} in {region} publishes {len(rates)} distinct {unit} rates "
            f"({rates}); a single pricing row cannot represent that, and the "
            "dimension classification in DIMENSION_PATTERNS is the first thing "
            "to check"
        )
    return rates[0] if rates else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch both public offer files and rewrite the committed digest",
    )
    args = parser.parse_args()
    if args.refresh:
        _refresh()
    else:
        print(json.dumps(load_digest(), indent=2, sort_keys=True))
