# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reduce AWS's published Bedrock price list to the few facts the non-chargeable
declaration rests on, so the next reader can re-derive them instead of trusting a
list somebody typed.

``idp_common.metering_units`` declares that ``totalTokens`` and ``requests`` are
not chargeable for Bedrock. Both claims are properties of AWS's own price list,
which is published **unauthenticated** at :data:`OFFER_URL` — no credential, no
``aws`` CLI, no Pricing API call. This module is the derivation, and it is
deliberately separate from the test that consumes it: the test runs offline
against the committed digest (a blocking gate that needs egress red-lines the
branch on somebody else's outage, the same reason
``scripts/check_markdown_links.py`` never fetches an ``http`` URL), while
``--refresh`` re-fetches and rewrites the digest.

    python3 lib/idp_common_pkg/tests/unit/reporting/bedrock_price_list_digest.py --refresh

The offer file is ~17 MB and 12,000-odd products, so the digest keeps counts and
small sorted inventories rather than the file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from typing import Any

#: AWS's public, unauthenticated Bedrock offer file. The bulk price list is the
#: same data the Pricing API serves; it needs no credential, which is why this is
#: reachable from a test machine at all.
OFFER_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrock/current/index.json"
)

#: The ``feature`` attribute value AWS uses for pay-per-token model inference —
#: what every ``bedrock/<model-id>`` metering key in this project represents.
ON_DEMAND_INFERENCE = "On-demand Inference"

DIGEST_PATH = os.path.join(os.path.dirname(__file__), "data", "bedrock_price_list.json")


def digest_offer_file(offer: dict[str, Any]) -> dict[str, Any]:
    """The facts ``test_non_chargeable_units.py`` asserts, and nothing else.

    Every count here answers one question about a *billing dimension*:

    * ``total_named_usagetypes`` — does AWS meter a **total** token count anywhere
      in Bedrock? ``totalTokens`` is a sum Bedrock reports for convenience, and
      the claim that it is free is only as good as this being 0.
    * ``token_types`` — the ``tokenType`` attribute's whole value set, which is
      the other side of the same question.
    * ``on_demand_inference_units`` — the units on-demand inference is priced in.
      ``requests`` being non-chargeable for model inference is this not
      containing a request unit.
    * ``request_priced_features`` — where Bedrock *does* charge per request, so
      that "no request dimension" is a statement about on-demand inference rather
      than about Bedrock as a whole.
    """
    products: dict[str, Any] = offer["products"]
    on_demand = {
        sku
        for sku, p in products.items()
        if p.get("attributes", {}).get("feature") == ON_DEMAND_INFERENCE
    }

    on_demand_units: Counter[str] = Counter()
    request_priced: Counter[str] = Counter()
    for by_sku in offer["terms"].values():
        for sku, offers in by_sku.items():
            for term in offers.values():
                for dim in term["priceDimensions"].values():
                    unit = dim["unit"]
                    if sku in on_demand:
                        on_demand_units[unit] += 1
                    if re.search(r"request|api call", unit, re.I):
                        attrs = products.get(sku, {}).get("attributes", {})
                        feature = attrs.get("feature") or "(no feature attribute)"
                        request_priced[f"{feature} [{unit}]"] += 1

    return {
        "source": OFFER_URL,
        "offer_version": offer.get("version"),
        "publication_date": offer.get("publicationDate"),
        "product_count": len(products),
        "total_named_usagetypes": sorted(
            {
                p["attributes"]["usagetype"]
                for p in products.values()
                if re.search(
                    r"total", p.get("attributes", {}).get("usagetype", ""), re.I
                )
            }
        ),
        "token_types": sorted(
            {
                t
                for p in products.values()
                if (t := p.get("attributes", {}).get("tokenType"))
            }
        ),
        "on_demand_inference_product_count": len(on_demand),
        "on_demand_inference_units": dict(sorted(on_demand_units.items())),
        "request_priced_features": dict(sorted(request_priced.items())),
    }


def _refresh() -> None:
    import urllib.request

    with urllib.request.urlopen(OFFER_URL, timeout=300) as fh:  # noqa: S310
        offer = json.load(fh)
    digest = digest_offer_file(offer)
    os.makedirs(os.path.dirname(DIGEST_PATH), exist_ok=True)
    with open(DIGEST_PATH, "w") as fh:
        json.dump(digest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {DIGEST_PATH}")
    print(json.dumps(digest, indent=2, sort_keys=True))


def load_digest() -> dict[str, Any]:
    with open(DIGEST_PATH) as fh:
        return json.load(fh)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch the public offer file and rewrite the committed digest",
    )
    args = parser.parse_args()
    if args.refresh:
        _refresh()
    else:
        print(json.dumps(load_digest(), indent=2, sort_keys=True))
