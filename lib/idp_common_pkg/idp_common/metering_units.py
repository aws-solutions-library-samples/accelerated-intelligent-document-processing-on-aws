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

There are two declarations behind it, at two different scopes, and the scope is
the substance rather than an implementation detail.
:data:`NON_CHARGEABLE_METERING_UNITS` is per **service**, for units that are not
a billing dimension of that service at all (``totalTokens`` on Bedrock).
:data:`EXPECTED_CACHE_UNIT_OMISSIONS` is per **pricing key**, because whether a
prompt-cache read costs anything is a property of the model: Nova Premier is
billed $0.625/MTok for one and Claude 3 Haiku has no such dimension in any
region. The two cache units were declared at service scope until
[#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206),
which made every Bedrock entry omitting them price a cache read at $0.00 on one
reason written for eleven different models.

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

The web UI is a **client** of this module, not a third implementation
---------------------------------------------------------------------

``src/ui/src/components/document-panel/pricing.ts`` prices the document cost
table in the browser, where nothing above can be imported, and it therefore does
not decide the absent-unit case at all. :func:`declared_free_units` expands these
registers per pricing key, the ``getPricing`` API response carries the expansion
as ``nonChargeableUnits``, and the panel reads the verdict off it: a unit the
expansion names renders as $0.00, and a unit it does not — with a count that is
not zero — renders as unpriced, which is the same answer this module gives and
the ``metering`` table records. See
[#1406](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1406),
which is what that panel showing a confident $0.00 for a row recorded as NULL was.

Two properties hold that together, and both are asserted rather than intended.
The expansion is checked against :func:`classify_absent_unit` over every shipped
pricing key and every unit any of them names
(``tests/unit/reporting/test_declared_free_units.py``), so the two cannot answer
differently for a declared unit. And a panel that receives **no** expansion — an
older deployment's resolver, a failed pricing fetch — reads every omitted unit as
unpriced rather than as free, so the degradation is in the direction that shows
the gap instead of hiding it.
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
)


_DECLARED: frozenset[tuple[str, str]] = frozenset(
    (entry.service, entry.unit) for entry in NON_CHARGEABLE_METERING_UNITS
)


# --------------------------------------------------------------------------- #
# The two cache units, which are per model rather than per service              #
# --------------------------------------------------------------------------- #
#
# ``cacheReadInputTokens`` and ``cacheWriteInputTokens`` used to be declared
# above as service-wide Bedrock free units, which made every ``bedrock/`` pricing
# entry that omits them price a cache read at exactly $0.00. That was measured,
# not assumed: a million cache-read tokens and a million cache-write tokens on
# each of the eleven shipped entries that omit the units came back as $0.00 from
# both cost implementations, with no unpriced marker on either.
#
# Whether cache reads cost anything is a property of the *model*, not of Bedrock,
# so a service-wide declaration is the exact defect shape this repository keeps
# re-learning: one justification attached to a set, where the justification is a
# property of individual members. Two of the eleven recorded a basis for the
# omission and nine recorded none —
# [#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206).
# The register below replaces the two service-wide entries with one entry per
# (pricing key, unit), each naming the price-list model it was read against and
# what that reading showed.


@dataclass(frozen=True)
class CacheUnitOmission:
    """One shipped pricing entry's recorded basis for omitting one cache unit.

    ``price_list_model`` is the value of the ``model`` attribute in AWS's offer
    file — ``Nova Premier``, ``Claude 3 Haiku`` — which is the only handle the
    price list offers; it is not a Bedrock model id and the two are spelled
    differently (``nvidia.nemotron-nano-12b-v2`` is published as ``NVIDIA
    Nemotron Nano 2 VL``).

    ``priced_by_aws`` is the one fact the runtime rule turns on: does AWS publish
    a **non-zero** rate for this dimension on this model, at standard on-demand
    tier, in any region?

    * ``False`` — the dimension is either absent from both offer files entirely
      or published at exactly $0.00. The omission then costs nothing to read as
      free, because there is no rate it could be hiding, and
      :func:`classify_absent_unit` answers ``NOT_CHARGEABLE``.
    * ``True`` — AWS does price the dimension, so the omission is **not**
      justified by the price list. What justifies it is that this code path can
      never meter the unit for this model: the id is absent from
      ``bedrock.client.CACHEPOINT_SUPPORTED_MODELS``, so no Converse
      ``cachePoint`` block is ever sent to it, and it is not one of
      ``bedrock.prompt_cache._IMPLICIT_CACHE_BASE_NAMES``, which cache without
      one. That premise is about the tree rather than about AWS, so it can go
      stale in a single commit — and :func:`classify_absent_unit` therefore
      answers ``UNKNOWN`` for these, which prices a cache token that does turn up
      as a NULL to investigate rather than as a zero nobody sees.

    Both halves are checked rather than trusted. ``priced_by_aws`` is asserted
    against a committed digest of both offer files
    (``tests/unit/reporting/bedrock_cache_dimension_digest.py``, refreshable from
    the public files with no credential), and the unreachability premise is
    asserted against the two code constants, for every member — which is what the
    older allowlist in ``test_pricing_lookup.py`` did for those of these entries
    whose model ids a template enum or a shipped preset names. The three that it
    did not reach — ``us.amazon.nova-premier-v1:0`` and the two
    ``claude-3-haiku-20240307`` profiles — reach a deployment through
    ``pricing.yaml`` itself, which doubles as the set of model ids configuration
    validation accepts (``config.merge_utils._load_valid_bedrock_models``), so
    they were configurable and covered by nothing.
    """

    pricing_key: str
    unit: str
    price_list_model: str
    priced_by_aws: bool
    authority: str
    basis: str


#: AWS's two public Bedrock offer files. Amazon's own models, Meta's Llama and
#: the Bedrock Marketplace models are in the first; Anthropic's Claude models are
#: in the second, and reading only the first finds two Claude 3 Haiku products
#: and no cache dimension for a reason that has nothing to do with caching.
AWS_BEDROCK_PRICE_LISTS = (
    AWS_BEDROCK_PRICE_LIST,
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrockFoundationModels/current/index.json",
)

#: The two units :data:`EXPECTED_CACHE_UNIT_OMISSIONS` governs. Exported because
#: both cost implementations need it to name the right declaration in the warning
#: they log for an unpriced unit — sending an operator to the service-wide list
#: for a cache unit would tell them to re-create the construct #1206 removed.
CACHE_UNITS = ("cacheReadInputTokens", "cacheWriteInputTokens")

#: Read on **2026-10-13** from ``AmazonBedrock`` offer version ``20261010182242``
#: (published 2026-10-10, 13,315 products) and ``AmazonBedrockFoundationModels``
#: offer version ``20261012084008`` (published 2026-10-12, 7,026 products). A
#: price is a dated fact, so every ``basis`` below is a reading of these two
#: versions and nothing later; ``--refresh`` on the digest module re-reads them.
CACHE_DIMENSION_READING_DATE = "2026-10-13"

#: One entry per (shipped pricing key, omitted cache unit).
#:
#: Registered in ``scripts/tests/gate_exemptions.json`` as
#: ``EXPECTED_CACHE_UNIT_OMISSIONS``. Owned by
#: ``tests/unit/reporting/test_cache_unit_omission_basis.py``, which derives the
#: universe from ``config_library/pricing.yaml`` itself — so a further entry that
#: omits a cache unit fails there rather than inheriting a reason written for
#: somebody else's model.
EXPECTED_CACHE_UNIT_OMISSIONS: tuple[CacheUnitOmission, ...] = tuple(
    CacheUnitOmission(
        pricing_key=pricing_key,
        unit=unit,
        price_list_model=price_list_model,
        priced_by_aws=priced_by_aws,
        authority=authority,
        basis=basis,
    )
    for pricing_key, price_list_model, priced_by_aws, authority, basis, units in (
        # ---- no cache dimension in either offer file, in any region ----------
        (
            "bedrock/us.anthropic.claude-3-haiku-20240307-v1:0",
            "Claude 3 Haiku",
            False,
            AWS_BEDROCK_PRICE_LISTS[1],
            "No cache dimension on this model in either offer file: 70 "
            "token-priced dimensions across 17 regions, every one of them an "
            "input or an output count. Prompt caching postdates this model — it "
            "is the March 2024 Claude 3 generation — and Bedrock never offered "
            "it here, which is why there is no SKU to price.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/eu.anthropic.claude-3-haiku-20240307-v1:0",
            "Claude 3 Haiku",
            False,
            AWS_BEDROCK_PRICE_LISTS[1],
            "The same underlying model as the us. entry above and the same "
            "price-list record: AWS publishes one 'Claude 3 Haiku' product set "
            "covering both geographies, with no cache dimension in any of its 17 "
            "regions. Listed separately anyway, because the lookup is by exact "
            "pricing key and a reason shared by reference is a reason nobody "
            "re-reads when one of the two entries changes.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/qwen.qwen3-vl-235b-a22b",
            "Qwen3 VL 235B A22B",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No cache dimension: 176 token-priced dimensions across 13 regions, "
            "all input or output, in four service tiers (standard, batch, flex, "
            "priority). Note that this model reaches bedrock-runtime Converse "
            "like every other model here, not the bedrock-mantle Responses path, "
            "which carries the openai.gpt-5.x models only — the operative premise "
            "is the one the test computes (absent from "
            "CACHEPOINT_SUPPORTED_MODELS), not a routing claim.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/us.meta.llama4-maverick-17b-instruct-v1:0",
            "Llama 4 Maverick 17B",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No cache dimension: 16 token-priced dimensions across 4 US regions "
            "— input and output, standard and batch — and nothing else.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/us.meta.llama4-scout-17b-instruct-v1:0",
            "Llama 4 Scout 17B",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No cache dimension: 16 token-priced dimensions across 4 US regions "
            "— input and output, standard and batch — the same shape as Maverick "
            "above and read in the same pass over the same offer file version. "
            "Spelled out rather than cross-referenced because a reason held by "
            "reference is one nobody re-reads when the other entry changes.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/google.gemma-3-27b-it",
            "Gemma 3 27B",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No cache dimension: 176 token-priced dimensions across 13 regions, "
            "all input or output, in the same four service tiers as Qwen above "
            "and read in the same pass over the same offer file version.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        (
            "bedrock/nvidia.nemotron-nano-12b-v2",
            "NVIDIA Nemotron Nano 2 VL",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No cache dimension: 208 token-priced dimensions across 15 regions, "
            "all input or output. Note the price-list spelling, which is the easy "
            "thing to get wrong here — this model id maps to 'NVIDIA Nemotron "
            "Nano 2 VL' (usagetype 'Nemotron-Nano-12B-V2-VL-BF16-*'), NOT to "
            "'NVIDIA Nemotron Nano 2', which is the 9B text model and a different "
            "product.",
            ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ),
        # ---- dimension published at exactly $0.00 ----------------------------
        (
            "bedrock/us.amazon.nova-premier-v1:0",
            "Nova Premier",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "No standard-tier cache-WRITE dimension on this model, and the two "
            "tiers that have one — flex and priority — publish it at exactly "
            "$0.0000000000. Amazon Nova charges for cache reads and not for the "
            "write that populates the cache, which is the same shape the Nova "
            "Lite and Nova Pro rows show (both publish a standard-tier "
            "cache-write dimension at $0.00). The cache READ rate IS published "
            "for this model and is now in the pricing entry at $0.625/MTok, so "
            "this register covers the write only.",
            ("cacheWriteInputTokens",),
        ),
        (
            "bedrock/amazon.nova-pro-v1:0",
            "Nova Pro",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "Cache-write is published at exactly $0.0000000000 for this model "
            "(usagetype 'NovaPro-cache-write-input-token-count'). Nothing is "
            "hidden by omitting a unit AWS prices at zero. Scoped to this model "
            "rather than to Nova as a family, which would be false three ways: "
            "Nova Premier publishes no standard-tier cache-write dimension at "
            "all, and Nova 2.0 Pro and Nova 2.0 Omni publish no cache dimension "
            "of any kind.",
            ("cacheWriteInputTokens",),
        ),
        (
            "bedrock/amazon.nova-lite-v1:0",
            "Nova Lite",
            False,
            AWS_BEDROCK_PRICE_LISTS[0],
            "Cache-write is published at exactly $0.0000000000 for this model "
            "(usagetype 'NovaLite-cache-write-input-token-count'), as for Nova "
            "Pro above and read in the same pass. Nothing is hidden by omitting "
            "a unit AWS prices at zero.",
            ("cacheWriteInputTokens",),
        ),
        # ---- AWS DOES price the dimension; the omission rests on the code ----
        (
            "bedrock/amazon.nova-pro-v1:0",
            "Nova Pro",
            True,
            AWS_BEDROCK_PRICE_LISTS[0],
            "AWS prices cache reads on this model — $0.24/MTok in us-gov-west-1, "
            "$0.20/MTok in us-east-1, 13 distinct regional rates in all — so the "
            "price list does NOT support reading this omission as free, and the "
            "pricing.yaml comment claiming the cache units are omitted because "
            "'GovCloud caching is unverified' was recording an absence of "
            "evidence as evidence of absence. What makes the omission safe is "
            "narrower and is about this tree, not about AWS: the BARE model id is "
            "the GovCloud spelling, it is absent from CACHEPOINT_SUPPORTED_MODELS "
            "(only the us. and eu. inference-profile spellings are there), and "
            "Nova does not cache implicitly — so no cachePoint is ever sent and "
            "no cache read can be metered against this key. That is one commit "
            "from being false, which is why this entry is priced_by_aws=True and "
            "the unit is reported rather than zeroed if it ever does turn up.",
            ("cacheReadInputTokens",),
        ),
        (
            "bedrock/amazon.nova-lite-v1:0",
            "Nova Lite",
            True,
            AWS_BEDROCK_PRICE_LISTS[0],
            "As Nova Pro above: cache reads are priced ($0.018/MTok in "
            "us-gov-west-1, $0.015/MTok in us-east-1), the bare id is absent from "
            "CACHEPOINT_SUPPORTED_MODELS, and Nova does not cache implicitly.",
            ("cacheReadInputTokens",),
        ),
    )
    for unit in units
)


_CACHE_OMISSION_FREE: frozenset[tuple[str, str]] = frozenset(
    (entry.pricing_key, entry.unit)
    for entry in EXPECTED_CACHE_UNIT_OMISSIONS
    if not entry.priced_by_aws
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

    Answers ``NOT_CHARGEABLE`` when any of three holds:

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
    * **this exact pricing key records a basis** for omitting this cache unit in
      :data:`EXPECTED_CACHE_UNIT_OMISSIONS`, *and* that basis says AWS publishes no
      chargeable rate for the dimension. An entry whose ``priced_by_aws`` is
      ``True`` is deliberately **not** shielded: AWS prices the dimension, so a
      count turning up there is a premise that has broken rather than something
      free, and the caller reports it.

    Otherwise ``UNKNOWN`` — the caller reports it rather than pricing it at zero.

    ⚠️ Note the order, because it bounds the previous sentence: a count that is
    not a number is ``UNKNOWN`` only if **no** declaration covers the unit. It is
    not a measured zero, and treating an unreadable count as one is how a failed
    read becomes a value — but a *declared* free unit is free whatever its count
    says, since the declaration is about the rate and not about the reading. The
    bound costs nothing in practice and both callers are why: the product coerces
    the count to a float before this call, and the harness reports a non-numeric
    count before reaching the lookup at all.

    Note what the third rule means for a key this register does not name: a cache
    unit absent from *any other* Bedrock pricing entry is ``UNKNOWN``. That is the
    point — a pricing entry added next month that omits a cache rate is reported
    rather than silently priced at $0.00, which is the class behind
    [#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206)
    rather than the instances that revealed it. A user-supplied pricing table is
    held to the same rule, and a zero count still costs nothing, so the only rows this
    reaches are ones that metered real cache activity against an entry carrying
    no rate for it.

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
    if (pricing_key, unit) in _CACHE_OMISSION_FREE:
        return NOT_CHARGEABLE
    return UNKNOWN


def declared_free_units(pricing_key: str) -> tuple[str, ...]:
    """Which units a *declaration* makes free at ``pricing_key``.

    The wire form of the two registers above, for a reader that cannot import
    them. The web UI's cost table is the one such reader: it receives this for
    every entry in the pricing configuration, on the ``getPricing`` response, and
    renders a unit the answer does not name as unpriced rather than as $0.00.

    Expanded per pricing key rather than shipped raw, because the two registers
    are scoped differently — one per service, one per exact key — and a consumer
    that re-derived which scope applied to a key would be deciding the absent-unit
    question a second time in a second language. That is the arrangement this
    function exists to remove, so it answers in the one form a consumer can use
    without knowing either scope: the key it already matched, and a set of unit
    names.

    Only the *declared* half of :func:`classify_absent_unit` is here. The
    zero-count rule is deliberately absent: it is arithmetic over a number this
    function is not given — a count of 0 costs 0 at any rate — so folding it in
    would make a per-key declaration depend on a per-row value, and a consumer
    holding the count can apply it where it belongs.

    ``pricing_key`` is a *pricing* key, never a metering key, for the reason
    :func:`pricing_key_service` gives: the service is the leading component, so
    ``Extraction/bedrock/<model>`` has service ``Extraction``, which no
    declaration names, and the answer is empty. Both cost implementations and the
    panel walk a metering key down to a matched pricing key before this matters.

    Returns the names sorted, so the serialized form is stable and a diff in it is
    a change of substance rather than of iteration order.
    """
    service = pricing_key_service(pricing_key)
    units = {unit for declared, unit in _DECLARED if declared == service}
    units.update(unit for key, unit in _CACHE_OMISSION_FREE if key == pricing_key)
    return tuple(sorted(units))
