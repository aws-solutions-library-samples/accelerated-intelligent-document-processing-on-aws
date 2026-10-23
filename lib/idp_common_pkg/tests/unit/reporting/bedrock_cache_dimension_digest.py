# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which Bedrock models AWS publishes a prompt-cache billing dimension for.

``config_library/pricing.yaml`` omits one or both cache units on several
``bedrock/`` entries, and an omitted unit used to be priced at $0.00 by both cost
implementations with nothing recording *why* it was omitted — see
[#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206).
``idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS`` now records a basis per
entry and per unit, and the load-bearing half of each basis is a claim about
AWS's published price list: either the model has **no** cache dimension at all,
or it has one at **$0.00**, or it has a real rate and the omission rests on
something else. This module is the derivation of that claim, so the next reader
can re-run it rather than trusting a table somebody typed.

Two offer files are needed, not one, and that is the first thing to know: AWS
publishes Amazon's own models (Nova), Meta's Llama and the Bedrock Marketplace
models (Qwen, Gemma, Nemotron) under ``AmazonBedrock``, and Anthropic's Claude
models under ``AmazonBedrockFoundationModels``. Reading only the first finds
*two* products for Claude 3 Haiku — input tokens in two regions — and would make
"no cache dimension" look like a reading of a complete record when the record is
somewhere else. The digest unions both.

It is deliberately separate from :mod:`bedrock_price_list_digest`, which answers
a different question (is ``totalTokens`` a billing dimension anywhere in
Bedrock?) and whose committed counts are quoted verbatim in
``NON_CHARGEABLE_METERING_UNITS``. Refreshing that digest therefore means editing
those quotes; this one carries its own numbers and can be refreshed alone.

Both files are public and **unauthenticated** — no credential, no ``aws`` CLI, no
Pricing API call, which is why this is reachable from a test machine at all. The
test that consumes the digest runs offline against the committed copy, because a
blocking gate that needs egress red-lines the branch on somebody else's outage;
``--refresh`` re-fetches and rewrites it.

    python3 lib/idp_common_pkg/tests/unit/reporting/bedrock_cache_dimension_digest.py --refresh

The two offer files are ~18 MB and ~10 MB, so the digest keeps one small record
per model rather than the files.
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
    os.path.dirname(__file__), "data", "bedrock_cache_dimensions.json"
)

#: The metering units this digest records published rates for, and the shapes AWS
#: spells the corresponding dimension in. Both offer files are matched
#: case-insensitively with ``-``/``_``/``:`` removed first, because the same
#: dimension appears as ``NovaLite-cache-read-input-token-count`` in one file and
#: ``MP:UGW1_CacheReadInputTokenCount-Units`` in the other.
#:
#: ⚠️ **The order is load-bearing and this dict is matched in it**, first hit
#: wins. ``cache-read-input-token-count`` normalises to a string *containing*
#: ``inputtoken``, so a rule that tested the input pattern first — or tested all
#: three independently — would count every cache dimension as an input one as
#: well. That is not hypothetical: it is the error that put ``inputTokens`` at
#: 5,580 published products instead of 4,909 in the sibling digest for #1212.
#:
#: ``inputTokens`` and ``outputTokens`` are here for a reason that is not
#: pricing. They are what ties a recorded ``price_list_model`` to the pricing
#: entry that cites it: the entry's own input **and** output rates have to be ones
#: AWS publishes for that model. Without that a basis could name a *different real
#: model* with the same cache answer and nothing would notice — measured, naming
#: ``NVIDIA Nemotron Nano 2`` (the 9B text model) where ``NVIDIA Nemotron Nano 2
#: VL`` (the 12B one this project prices) is meant left the suite green, because
#: neither publishes a cache dimension and both carry 208 token-priced dimensions.
#:
#: Why **both** rates rather than the input one alone: on the input rate alone the
#: entries then registered had 2 to 11 candidate models each, so the check was a
#: rate collision away from meaningless. Requiring both pins most members to
#: exactly one model out of the price list, and
#: ``test_cache_unit_omission_basis.py`` pins what the rest leave open — as a
#: floor and an equality rather than as a figure quoted here, because the figure
#: moves whenever AWS adds a model at an identical rate.
DIMENSION_PATTERNS = {
    "cacheReadInputTokens": r"cacheread",
    "cacheWriteInputTokens": r"cachewrite",
    "inputTokens": r"inputtoken",
    "outputTokens": r"outputtoken",
}

#: Dimension variants excluded from the per-model rate sets, each because this
#: project never bills against it. ``batch``/``flex``/``priority`` are separate
#: service tiers with their own usage types; ``longctx`` is the long-context band
#: (see the note on the Haiku 5.5 rows in ``pricing.yaml``); ``1h`` is the
#: one-hour-TTL cache write, and IDP writes five-minute cache points only;
#: ``custommodel`` and ``provisioned`` are not on-demand inference at all.
#:
#: The exclusion is load-bearing rather than tidying: Nova Premier publishes a
#: cache *write* dimension at flex and priority only, both at $0.00, and no
#: standard-tier one. Folding those in would record a rate for a tier this
#: project cannot invoke.
EXCLUDED_VARIANTS = r"batch|flex|priority|longctx|custommodel|provisioned|1h"

_PER_UNIT_DIVISOR = {"1K tokens": 1_000.0, "1M tokens": 1_000_000.0}


def _normalise(text: str) -> str:
    return re.sub(r"[-_:\s]", "", text).lower()


def _per_token(unit: str, price: str) -> str | None:
    """A price dimension's rate expressed per single token, or ``None``.

    Returns a string rather than a float so the committed digest is byte-stable
    and a recorded rate can be compared for equality without worrying about
    float repr. ``None`` for any dimension not priced in tokens — an image, a
    model-hour, a request — which is how non-token dimensions are dropped.
    """
    divisor = _PER_UNIT_DIVISOR.get(unit)
    if divisor is None:
        return None
    return f"{float(price) / divisor:.6g}"


def digest_offer_files(offers: list[dict[str, Any]]) -> dict[str, Any]:
    """One record per price-list model, unioned across the offer files.

    For each model the ``model`` attribute names, the per-token rates AWS
    publishes for each dimension in :data:`DIMENSION_PATTERNS`, plus the count of
    token-priced dimensions of any kind. That last number is what makes an empty
    cache-rate list readable: a model with 16 token dimensions and no cache
    dimension is a measured absence, while a model with 0 of either means the
    extraction found nothing and the absence proves nothing.

    Each dimension row is classified **once**, by the first pattern that matches
    — see the warning on :data:`DIMENSION_PATTERNS`, which is why this is a loop
    with a ``break`` rather than three independent tests.
    """
    excluded = re.compile(EXCLUDED_VARIANTS)
    dimension_res = {
        unit: re.compile(pattern) for unit, pattern in DIMENSION_PATTERNS.items()
    }

    models: dict[str, dict[str, Any]] = {}
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
                if not model:
                    continue
                usagetype = _normalise(attributes.get("usagetype", ""))
                token_type = _normalise(attributes.get("tokenType", ""))
                record = models.setdefault(
                    model,
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
                        if excluded.search(usagetype) or excluded.search(token_type):
                            continue
                        for unit, pattern in dimension_res.items():
                            if pattern.search(usagetype) or pattern.search(token_type):
                                record[unit].add(rate)
                                break

    return {
        "sources": sources,
        "retrieved": datetime.date.today().isoformat(),
        "model_count": len(models),
        "models": {
            model: {
                "token_dimension_count": record["token_dimension_count"],
                **{unit: sorted(record[unit]) for unit in DIMENSION_PATTERNS},
            }
            for model, record in sorted(models.items())
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
    print(f"wrote {DIGEST_PATH}: {digest['model_count']} models")
    for source in digest["sources"]:
        print(
            f"  {source['offer_code']} version {source['offer_version']} "
            f"({source['product_count']} products)"
        )


def load_digest() -> dict[str, Any]:
    with open(DIGEST_PATH) as fh:
        return json.load(fh)


def published_rates(digest: dict[str, Any], model: str, unit: str) -> list[str]:
    """The per-token rates the digest records for one model and one unit.

    Raises ``KeyError`` for a model the digest does not describe, deliberately:
    a recorded basis naming a model AWS's price list does not carry is not a
    weaker claim, it is an unread one, and the caller must fail rather than read
    the empty list as "no cache dimension".
    """
    return digest["models"][model][unit]


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
