# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which metering units are legitimately free, and where that is declared.

Both cost implementations resolve a metering row to a ``pricing.yaml`` entry and
then look the row's unit up in it:

* ``idp_common.reporting.save_reporting_data.SaveReportingData._get_unit_cost``
  (the product's reported cost), and
* ``benchmarks/harness/lib.py::price_metering`` (the benchmark harness's).

A unit the matched entry does **not** list used to be $0.00 in both, on the
stated basis that such a unit is not chargeable for that service. That basis is
true of the units this module declares and of nothing else, and nothing checked
which was which. The consequence: a numeric field Bedrock adds to its ``usage``
block reaches metering automatically — ``bedrock.client.numeric_usage`` forwards
*every* numeric member, by design — and is then priced at exactly $0.00, in the
cheap direction, with no log line and no NULL to query for. See
[#1212](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1212).

:func:`classify_absent_unit` is the whole rule, and both implementations call it
rather than re-deciding, because a benchmark cost that disagrees with the
reported cost for the same metering map is a bug in one of them.

How the set was populated
-------------------------

**A metering unit that appears in no priced entry of AWS's published price list
is treated as non-chargeable.** That is the rule, it is an accepted assumption
rather than an established fact, and it is stated here because the entries below
are only as good as it is. It was adopted deliberately, after measuring that the
Bedrock price list carries no SKU rows for ``totalTokens`` at all and none for a
per-request dimension under on-demand model inference: those units are not
described as free anywhere, they are simply absent, and the absence is the basis.
Each entry records what was read and what it showed, so the reading can be
redone rather than taken on trust —
``lib/idp_common_pkg/tests/unit/reporting/bedrock_price_list_digest.py`` is the
derivation, the counts it produced are committed beside it, and
``test_non_chargeable_units.py`` asserts the entries against them.

⚠️ **The failure mode this leaves, and it is accepted rather than eliminated: a
unit that AWS really does charge for but does not publish a dimension for is
classified as free here, and every bit of it is billed at $0.00.** The price list
is a catalogue of published SKUs; it is not a statement of what AWS bills, and
nothing in this module can tell "not chargeable" apart from "not listed". What
bounds the risk is one measurement and it is partial: all four token units that
``config_library/pricing.yaml`` prices for Bedrock — ``inputTokens``,
``outputTokens``, ``cacheReadInputTokens``, ``cacheWriteInputTokens`` — do appear
as published Bedrock dimensions, so on this service every unit known to be
chargeable is in fact listed. Counts on the 2026-09-25 offer file, under the rule
that a product names the dimension in its ``usagetype`` or ``tokenType`` and, for
the two plain token units, is not itself a cache dimension: 4909 products for
``inputTokens``, 4834 for ``outputTokens``, 700 for ``cacheReadInputTokens``, 415
for ``cacheWriteInputTokens``. The cache exclusion is load-bearing rather than
tidying — ``cache-read-input-token-count`` contains ``input-token``, so a rule
without it counts every cache dimension as an input one and inflates the first
figure. The non-Bedrock units this project meters (``pages``, ``documents``,
``gb_seconds``, and ``requests`` on AWS Lambda) were **not** checked against
their own services' price lists, so nothing here says that list is complete.

A hand-written list of free units would be exactly the defect #1212 is about, one
level up; an inference from an absence is weaker than a statement of fact, and
saying which one this is, is the point of this section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: AWS's public Bedrock offer file — unauthenticated, no credential, no Pricing
#: API call. Quoted in the entries below so a reader can re-run the derivation.
AWS_BEDROCK_PRICE_LIST = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrock/current/index.json"
)

#: What :func:`classify_absent_unit` can answer.
NOT_CHARGEABLE = "not_chargeable"
UNKNOWN = "unknown"

Verdict = Literal["not_chargeable", "unknown"]


@dataclass(frozen=True)
class NonChargeableUnit:
    """One metering unit that a pricing entry may legitimately omit.

    ``service`` is the leading ``/``-delimited component of the *pricing* key
    (``bedrock``, ``lambda_hook``, ``textract``, …), never a whole model id: the
    claim being recorded is about a billing dimension of a service, and scoping
    it to one model would be a claim about the model instead. It is deliberately
    not a wildcard — ``requests`` is free for Bedrock model inference and is a
    real, priced AWS Lambda dimension, so one entry covering both services would
    be false for one of them.

    ``authority`` is where the claim comes from and ``basis`` is what that source
    showed. An entry whose ``authority`` is ``JUDGEMENT`` is a decision rather
    than a measurement and says so; the rest name a URL. Note what a URL here
    does and does not assert: it says the unit is absent from that price list,
    and the step from "absent" to "free" is the accepted assumption described in
    this module's docstring, not something the source states.
    """

    unit: str
    service: str
    authority: str
    basis: str


#: The declared free units. Anything else a matched pricing entry omits is
#: ``UNKNOWN`` and is reported — as a NULL cost in the reporting table, and as an
#: unpriceable entry in the benchmark harness.
#:
#: Registered in ``scripts/tests/gate_exemptions.json`` as
#: ``NON_CHARGEABLE_METERING_UNITS``: it is a declaration that switches a check
#: off for its members, so it carries a ratchet (every entry must currently
#: shield a real metered-and-unlisted unit, and the count is pinned).
NON_CHARGEABLE_METERING_UNITS: tuple[NonChargeableUnit, ...] = (
    NonChargeableUnit(
        unit="totalTokens",
        service="bedrock",
        authority=AWS_BEDROCK_PRICE_LIST,
        basis=(
            "Absent from the price list: of 12587 products in the offer file, 0 "
            "declare a usagetype whose name matches /total/i, and the tokenType "
            "attribute takes only input / output / cache-read / cache-write "
            "values. The reason to read that absence as free rather than as "
            "unpublished is that totalTokens is the sum Bedrock reports for "
            "convenience, and input and output are each already billed, so "
            "billing it again would double-charge the same tokens."
        ),
    ),
    NonChargeableUnit(
        unit="requests",
        service="bedrock",
        authority=AWS_BEDROCK_PRICE_LIST,
        basis=(
            "Absent from the price list for on-demand model inference: all 3841 "
            "products with feature 'On-demand Inference' price in "
            "'1K tokens', 'image', 'Images Processed' or 'video', and none in a "
            "request unit. Bedrock does charge per request elsewhere — Prompt "
            "Router ('Per 1000 requests'), Generate SQL structured retrieval, "
            "and Nova grounding. Note precisely what this entry is scoped to: "
            "the 'bedrock' component of a pricing key, which is every "
            "bedrock/<model-id> row this project meters and NOT specifically the "
            "on-demand feature the measurement is about. The scoping the code "
            "implements cannot distinguish an on-demand call from a batch or "
            "provisioned one, because the metering key does not record which it "
            "was. What makes that tolerable is that this project only ever "
            "invokes on-demand inference; a caller that added a batch or "
            "provisioned path would need this re-derived, since those are "
            "separate features in the price list with their own dimensions."
        ),
    ),
    NonChargeableUnit(
        unit="totalTokens",
        service="lambda_hook",
        authority=AWS_BEDROCK_PRICE_LIST,
        basis=(
            "Same dimension and the same reading of the same price list: a "
            "LambdaHook returns a Converse-shaped usage block (see "
            "samples/lambda-hook-inference/*/index.py), so its totalTokens is "
            "the same input+output sum, and a hook proxying Bedrock is billed on "
            "the input and output counts it also reports."
        ),
    ),
    NonChargeableUnit(
        unit="requests",
        service="lambda_hook",
        authority="JUDGEMENT",
        basis=(
            "This count is not reported by the hook and is not a third party's "
            "billing dimension: BedrockClient injects 'requests': 1 into a "
            "LambdaHook metering entry for shape parity with a Bedrock response. "
            "A hook's price is user-declared, so a user billed per call lists "
            "'requests' in their pricing entry and is priced on it, and this "
            "declaration never applies to them. No AWS price list can settle "
            "the absent case in either direction, which is why this is a "
            "judgement and labelled one."
        ),
    ),
    NonChargeableUnit(
        unit="cacheReadInputTokens",
        service="bedrock",
        authority="JUDGEMENT",
        basis=(
            "Preserves the behaviour that was already shipped, so that #1212 "
            "does not change any reported cost, and makes it locatable. Whether "
            "a Bedrock entry omitting cache units means 'this model does not "
            "cache' or 'nobody checked' is "
            "[#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206), "
            "which needs a live call per model. Until then this is the only "
            "entry standing between the 11 shipped bedrock/ pricing entries that "
            "omit cache rates and a NULL cost column — #1206 measures that nine "
            "of those 11 carry no recorded basis for the omission, which is its "
            "subject, not the count this entry shields. "
            "What constrains it meanwhile is static and already enforced: "
            "test_pricing_lookup.py refuses a shipped entry that omits cache "
            "rates while its model is in bedrock.client."
            "CACHEPOINT_SUPPORTED_MODELS or caches implicitly. Resolving #1206 "
            "is deleting these two entries."
        ),
    ),
    NonChargeableUnit(
        unit="cacheWriteInputTokens",
        service="bedrock",
        authority="JUDGEMENT",
        basis=(
            "As cacheReadInputTokens above: the same eleven shipped entries omit "
            "both cache units together, the same static guard constrains which "
            "models may, and the same live call per model would settle both. "
            "Listed separately rather than as a pair because the lookup is per "
            "unit, and a model that priced reads but not writes would otherwise "
            "inherit an entry that had stopped being about it."
        ),
    ),
)


_DECLARED: frozenset[tuple[str, str]] = frozenset(
    (entry.service, entry.unit) for entry in NON_CHARGEABLE_METERING_UNITS
)


def pricing_key_service(pricing_key: str) -> str:
    """The service component of a pricing key — ``bedrock`` from
    ``bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0``.

    Always the *matched pricing* key, never the metering key: a metering key
    carries a leading phase (``Extraction/bedrock/<model>``), so reading its
    first component would answer ``Extraction``, which matches no entry above and
    would make every declaration inert.
    """
    return pricing_key.split("/", 1)[0]


def classify_absent_unit(pricing_key: str, unit: str, value: Any) -> Verdict:
    """Is a unit the matched pricing entry omits free, or unknown?

    Answers ``NOT_CHARGEABLE`` when either holds:

    * **the metered count is zero.** This is arithmetic rather than a
      declaration: a row's cost is ``count x rate``, so a count of 0 contributes
      0 whatever the rate is, and there is no spend for a $0.00 to hide. It is
      what keeps the shipped per-page OCR hooks quiet — they report
      ``inputTokens``/``outputTokens``/``totalTokens`` as 0 against a price
      listing only ``pages``. Note the bound: a *new* chargeable unit whose count
      happens to be 0 on some calls is not reported for those calls. It is
      reported for the first call that meters any of it, which is the first call
      that could cost anything.
    * **the unit is declared** in :data:`NON_CHARGEABLE_METERING_UNITS` for this
      service.

    Otherwise ``UNKNOWN`` — the caller reports it rather than pricing it at zero.
    A count that is not a number is ``UNKNOWN`` too: it is not a measured zero,
    and treating an unreadable count as one is how a failed read becomes a value.

    Args:
        pricing_key: the pricing entry that matched, e.g. ``bedrock/<model-id>``.
        unit: the metering unit absent from that entry.
        value: the metered count for this row.
    """
    if isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, (int, float)) and value == 0:
        return NOT_CHARGEABLE
    if (pricing_key_service(pricing_key), unit) in _DECLARED:
        return NOT_CHARGEABLE
    return UNKNOWN
