#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Nova prompt-cache writes cost $0.00, and the units are disjoint.

Every ``bedrock/*nova*`` row in ``config_library/pricing.yaml`` used to charge
``cacheWriteInputTokens`` at the **full input rate**, on the Claude-shaped
assumption that a cache write is a multiple of input. It is not: Amazon Nova
charges for the cache READ and gives the write away, and AWS publishes the
cache-write dimension at exactly $0.0000000000 for every Nova model, in every
region, in all three service tiers —
[#1405](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1405).

⚠️ **A rate is only half of what makes a zero correct here, and the published
rate is the half that cannot settle it.** Everything turns on whether
``cacheWriteInputTokens`` is *disjoint* from ``inputTokens`` in the metering this
system records, because the two readings call for different fixes:

* **disjoint** — the cache-write tokens are counted separately and are not also
  inside ``inputTokens`` — so a rate of $0.00 drops a charge AWS does not make,
  and the old entry billed for something given away;
* **inclusive** — the cache-write tokens are a subset of ``inputTokens`` — so the
  old entry **double-counted** them, a different and larger error whose remedy
  would be in the metering rather than in the pricing table.

This repository has been bitten by precisely that distinction on another
provider, where a field that looked like fresh input turned out to be the total
including cached tokens and the disjoint-versus-inclusive error was a 64%
overcharge until somebody measured it. So this was measured, and
:data:`MEASURED_USAGE` is the measurement: three live ``Converse`` calls against
``us.amazon.nova-lite-v1:0``, same content with the nonce held fixed, one without
a cache point and two with. It is a fixture rather than a live call because a
blocking gate that needs credentials and egress red-lines the branch on an
outage, and because the relationship it establishes is a property of Bedrock's
API rather than of this tree.

What the three calls show, and what every assertion below rests on, is that
``totalTokens`` is invariant across them while ``inputTokens`` collapses to a
10-token residual and the 39,001 prefix tokens move into the cache unit. The four
units partition the prompt exactly. Note the trap that would have produced a
confident wrong answer: Nova has a minimum cacheable prefix and below it the
usage block reports a cache write of **zero**, which reads exactly like "cache
writes are not metered separately" and would have settled the question the wrong
way — so the fixture is asserted to carry a non-zero cache write before anything
is concluded from it.
"""

import json
from pathlib import Path

import pytest
import yaml

from idp_common.config.models import IDPConfig
from idp_common.metering_units import (
    EXPECTED_CACHE_UNIT_OMISSIONS,
    classify_absent_unit,
)
from idp_common.reporting.save_reporting_data import SaveReportingData

# lib/idp_common_pkg/tests/unit/reporting/<this file> -> up 5 parents.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_PRICING_PATH = _REPO_ROOT / "config_library" / "pricing.yaml"
_DIGEST_PATH = Path(__file__).parent / "data" / "bedrock_cache_dimensions.json"

CACHE_WRITE = "cacheWriteInputTokens"
CACHE_READ = "cacheReadInputTokens"

#: The model the measurement was taken on, and the pricing key it resolves to.
MEASURED_MODEL_ID = "us.amazon.nova-lite-v1:0"
MEASURED_PRICING_KEY = f"bedrock/{MEASURED_MODEL_ID}"

#: Raw ``usage`` blocks from three live ``Converse`` calls, us-west-2,
#: 2026-10-14, model ``us.amazon.nova-lite-v1:0``. Identical ~39k-token system
#: prefix in all three (a filler body whose only variable part is a nonce, held
#: FIXED across the three calls so no tokenisation difference can be mistaken for
#: a cache effect), and the same one-line user turn. The calls differ only in
#: whether a ``cachePoint`` block follows the prefix.
#:
#: Token counts only: a usage block carries no account, region-account or
#: identity information of any kind.
#:
#: The order is load-bearing. ``no_cache_point`` runs FIRST so it cannot read
#: anything the later calls wrote — Nova does not cache implicitly (it is absent
#: from ``bedrock.prompt_cache._IMPLICIT_CACHE_BASE_NAMES``), so its
#: ``inputTokens`` is the full un-cached cost of exactly this text and is the
#: control the other two are read against.
#:
#: ``cache_point_cold`` and ``cache_point_warm`` are the same request sent twice.
#: The cache took longer than 5s and less than 20s to become readable, so an
#: intermediate repeat also wrote; that repeat is not kept here because it is
#: identical to ``cache_point_cold`` and carries no further information. Under the
#: corrected rate a repeated write costs nothing, so the warm-up is a latency
#: property rather than a cost one — but it was a real cost under the old rate,
#: which is why it is recorded rather than tidied away.
MEASURED_USAGE = {
    "no_cache_point": {
        "inputTokens": 39011,
        "outputTokens": 2,
        "totalTokens": 39013,
    },
    "cache_point_cold": {
        "inputTokens": 10,
        "outputTokens": 2,
        "totalTokens": 39013,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 39001,
    },
    "cache_point_warm": {
        "inputTokens": 10,
        "outputTokens": 2,
        "totalTokens": 39013,
        "cacheReadInputTokens": 39001,
        "cacheWriteInputTokens": 0,
    },
}


@pytest.fixture(scope="module")
def shipped_pricing_raw():
    return yaml.safe_load(_PRICING_PATH.read_text())


@pytest.fixture(scope="module")
def shipped_reporter(shipped_pricing_raw):
    return SaveReportingData(
        "test-bucket", config=IDPConfig.model_validate(shipped_pricing_raw)
    )


def _nova_rows_with_a_cache_write_rate(raw) -> dict[str, float]:
    """``{pricing key: cache-write rate}`` for every Nova row that prices one.

    Derived from the shipped file rather than listed, because the defect #1405
    records was applied to a whole family and named one member of it. A new
    ``bedrock/...nova...`` row that carries a cache-write rate is in this set the
    moment it is added, and has to justify whatever rate it carries.
    """
    rows = {}
    for entry in raw["pricing"]:
        name = entry["name"]
        if not name.startswith("bedrock/") or "nova" not in name.lower():
            continue
        for unit in entry.get("units") or []:
            if unit["name"] == CACHE_WRITE:
                rows[name] = float(unit["price"])
    return rows


def _priced_cost(reporter, usage: dict, pricing_key: str) -> float:
    """Total cost of one usage block through the real lookup, as the product does.

    Mirrors ``save_metering_data``'s per-unit loop: resolve each unit to a rate
    and multiply. Raises on an unpriced unit rather than skipping it, because a
    ``None`` rate silently dropped from a sum is the shape this whole area keeps
    failing in.
    """
    total = 0.0
    for unit, value in usage.items():
        if unit == "totalTokens":
            continue  # a sum of the others, not a billing dimension
        rate = reporter._get_unit_cost(pricing_key, unit, float(value))
        assert rate is not None, f"{pricing_key} [{unit}] priced as UNPRICED (NULL)"
        total += float(value) * rate
    return total


# --------------------------------------------------------------------------- #
# 1. The measurement itself: a cache write happened, and the units are disjoint
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_measurement_actually_produced_a_cache_write():
    """The precondition, asserted before anything is concluded from the fixture.

    Nova has a minimum cacheable prefix length, and a prompt below it produces NO
    cache write — the usage block reports ``cacheWriteInputTokens`` absent or 0,
    which is indistinguishable from "cache writes are not metered separately" and
    would settle the disjointness question the wrong way. A fixture that had
    quietly been shrunk below that minimum would make every assertion below
    vacuous while leaving them green, so the non-zero write is its own test.
    """
    cold = MEASURED_USAGE["cache_point_cold"]
    assert cold[CACHE_WRITE] > 0, (
        "the fixture records no cache write, so it cannot establish anything "
        "about how a cache write is metered. Re-measure with a prefix above "
        "Nova's minimum cacheable length."
    )
    warm = MEASURED_USAGE["cache_point_warm"]
    assert warm[CACHE_READ] > 0, (
        "the fixture records no cache read, so the cache was never actually "
        "populated and the 'write' may not have been one"
    )
    assert warm[CACHE_READ] == cold[CACHE_WRITE], (
        "the warm call read a different number of tokens than the cold call "
        "wrote, so the two are not the same cached prefix"
    )


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(MEASURED_USAGE))
def test_the_token_units_partition_the_prompt_exactly(label: str):
    """``totalTokens`` equals the sum of the other four, in every call.

    This is the disjointness, stated as arithmetic. If ``cacheWriteInputTokens``
    were INCLUSIVE — a subset of ``inputTokens`` — then adding both to a total
    would double-count the cached prefix and this identity could not hold on the
    cold call while also holding on the control call, which has no cache units at
    all. It holds on all three.
    """
    usage = MEASURED_USAGE[label]
    parts = {k: v for k, v in usage.items() if k != "totalTokens"}
    assert sum(parts.values()) == usage["totalTokens"], (
        f"{label}: units {parts} do not sum to totalTokens="
        f"{usage['totalTokens']}, so they are not a partition and the "
        "disjointness this file rests on is not what Bedrock reports"
    )


@pytest.mark.unit
def test_cached_input_tokens_leave_input_tokens_rather_than_adding_to_it():
    """The control call against the cold one: where did the prefix tokens go?

    The two requests carry identical content and differ only in whether a
    ``cachePoint`` follows the prefix. Under the DISJOINT reading the prefix moves
    out of ``inputTokens`` and into ``cacheWriteInputTokens``, so the control's
    ``inputTokens`` equals the cold call's residual plus its cache write. Under the
    INCLUSIVE reading ``inputTokens`` would have stayed at ~39,011 on the cold
    call and the cache unit would have been a duplicate view of part of it.

    Asserted as an exact equality rather than a tolerance: the whole point of
    measuring was to tell two hypotheses apart, and "approximately" is how a
    confident wrong answer gets in.
    """
    control = MEASURED_USAGE["no_cache_point"]
    cold = MEASURED_USAGE["cache_point_cold"]

    assert cold["inputTokens"] + cold[CACHE_WRITE] == control["inputTokens"], (
        f"cached call reports inputTokens={cold['inputTokens']} + "
        f"cacheWrite={cold[CACHE_WRITE]}, which does not equal the uncached "
        f"call's inputTokens={control['inputTokens']} for identical content. "
        "The units are then neither cleanly disjoint nor cleanly inclusive and "
        "the pricing decision in config_library/pricing.yaml needs re-deriving"
    )
    assert cold["inputTokens"] < control["inputTokens"], (
        "inputTokens did not fall when the prefix was cached, which is the "
        "INCLUSIVE reading — under it this file's zero rate is wrong and the "
        "defect is a double-count in the metering instead"
    )

    # The partition again, across the two calls rather than within one, and
    # deliberately redundant with
    # test_the_token_units_partition_the_prompt_exactly. The redundancy is the
    # point: the whole live measurement reaches this suite as a fixture, so a
    # property stated by exactly one assertion over it is one edit away from
    # not being checked at all, with nothing else able to re-derive it. Stating
    # it twice — within a call, and between the pair — means an edit has to
    # defeat both to leave the fixture unchecked.
    assert control["totalTokens"] == cold["totalTokens"], (
        "the same content reports a different totalTokens with and without a "
        "cache point, so the two calls are not comparable and nothing below "
        "follows from putting them side by side"
    )
    assert (
        cold["inputTokens"]
        + cold[CACHE_WRITE]
        + cold[CACHE_READ]
        + cold["outputTokens"]
        == control["inputTokens"] + control["outputTokens"]
    ), (
        "the cached call's four units do not add up to the uncached call's two, "
        "so caching moved tokens between units rather than merely relabelling "
        "them and the disjointness is not exact"
    )


# --------------------------------------------------------------------------- #
# 2. Priced through the real lookup, the cost is the bill AWS would send
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_cold_cached_call_costs_the_hand_derived_aws_bill(shipped_reporter):
    """The end-to-end property, and the one that was false before #1405.

    The expected figure is arithmetic done here, from rates transcribed by hand
    out of ``pricing.yaml``, never by calling the code under test:

        inputTokens               10 x 6.0E-8 = 0.0000006
        outputTokens               2 x 2.4E-7 = 0.00000048
        cacheReadInputTokens       0 x 1.5E-8 = 0
        cacheWriteInputTokens 39,001 x 0.0    = 0           <- AWS charges nothing
                                               ----------
                                                0.00000108

    Before the fix the write line was ``39,001 x 6.0E-8 = 0.00234006``, so the
    reported cost was $0.00234114 against a real bill of $0.00000108: a factor of
    about 2,168 on this call, and in absolute terms the entire input cost of the
    cached prefix.
    """
    expected = 10 * 6.0e-8 + 2 * 2.4e-7
    actual = _priced_cost(
        shipped_reporter, MEASURED_USAGE["cache_point_cold"], MEASURED_PRICING_KEY
    )
    assert actual == pytest.approx(expected, rel=1e-12), (
        f"a cold cached call on {MEASURED_MODEL_ID} is priced at ${actual:.8f}, "
        f"but AWS bills ${expected:.8f} for it. The gap is "
        f"{MEASURED_USAGE['cache_point_cold'][CACHE_WRITE]} cache-write tokens "
        "priced at something other than the $0.00 AWS publishes"
    )


@pytest.mark.unit
def test_caching_a_prefix_is_cheaper_than_not_caching_it(shipped_reporter):
    """The defect's signature, in one assertion, independent of any rate.

    With cache writes charged at the input rate the two calls below cost **the
    same** — the write-side saving of prompt caching did not appear in the cost
    report at all, which is a shape worth pinning separately from the arithmetic
    above because it holds whatever the input rate happens to be and so survives a
    repricing of the model.

    It is deliberately a strict inequality AND a bounded one. A strict inequality
    alone would also pass on a NEGATIVE cache-write rate, which would be a
    different error in the other direction; requiring the cached call to cost no
    less than its own non-cache lines rules that out.
    """
    control = _priced_cost(
        shipped_reporter, MEASURED_USAGE["no_cache_point"], MEASURED_PRICING_KEY
    )
    cold = _priced_cost(
        shipped_reporter, MEASURED_USAGE["cache_point_cold"], MEASURED_PRICING_KEY
    )
    assert cold < control, (
        f"caching the prefix is reported at ${cold:.8f} against ${control:.8f} "
        "for the same content uncached, so the report shows no write-side saving "
        "from prompt caching"
    )
    floor = 10 * 6.0e-8 + 2 * 2.4e-7
    assert cold >= floor, (
        f"the cached call is priced at ${cold:.8f}, BELOW the ${floor:.8f} its "
        "own un-cached input and output lines come to. A cache unit is being "
        "priced negatively"
    )


# --------------------------------------------------------------------------- #
# 3. The whole Nova class, derived from the shipped file rather than listed
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_derived_nova_class_is_not_empty_and_covers_every_prefix():
    """Guard the guard: the derivation has to find the rows it is asserting about.

    An empty or near-empty derived set would make the parametrised assertion
    below collect as nothing and read green. The floor is a floor rather than an
    equality so that adding a Nova row does not turn this red, and the three
    region prefixes are named because #1405's own defect was a fix-the-instance
    risk: the issue quoted ``us.amazon.nova-lite-v1:0`` and the class spans
    ``us.``, ``eu.`` and ``global.``.
    """
    rows = _nova_rows_with_a_cache_write_rate(yaml.safe_load(_PRICING_PATH.read_text()))
    assert len(rows) >= 14, (
        f"only {len(rows)} Nova rows price a cache write; expected at least the "
        f"14 that #1405 covered. Found: {sorted(rows)}"
    )
    for prefix in (
        "bedrock/us.amazon.nova",
        "bedrock/eu.amazon.nova",
        "bedrock/global.amazon.nova",
    ):
        assert any(name.startswith(prefix) for name in rows), (
            f"no row under {prefix} prices a cache write, so this check cannot "
            "see that region prefix at all"
        )


@pytest.mark.unit
def test_every_nova_row_prices_a_cache_write_at_zero():
    """One assertion over the derived class, not over a transcribed list.

    AWS publishes no non-zero cache-write rate for any Nova model in any region
    or tier, so any Nova row carrying one is over-reporting by that rate times the
    cache-write volume — which the disjointness measured above makes a charge for
    something given away rather than a discount on something billed.
    """
    rows = _nova_rows_with_a_cache_write_rate(yaml.safe_load(_PRICING_PATH.read_text()))
    nonzero = {name: rate for name, rate in rows.items() if rate != 0.0}
    assert not nonzero, (
        f"Nova rows pricing a cache write above $0.00: {nonzero}. AWS publishes "
        "this dimension at exactly $0.0000000000 for every Nova model, in every "
        "region, in all three service tiers. See issue #1405 and the comment on "
        "the Nova section of config_library/pricing.yaml"
    )


@pytest.mark.unit
def test_the_rate_is_present_rather_than_omitted():
    """Zero and absent are different outcomes here, and the difference is #1206's.

    After #1206 a cache unit a priced row OMITS is reported as **unpriced** — a
    NULL cost and a warning naming the entry — unless the omission is registered
    in ``EXPECTED_CACHE_UNIT_OMISSIONS``. So "price it at zero" and "delete the
    row" are not the same fix, and an assertion that the rate *is* zero would pass
    on a file where the unit had been deleted instead.

    These rows carry an explicit ``0.0`` and are deliberately NOT in that
    register: the register answers "why does this entry omit a unit", and these
    entries do not omit one. AWS publishes a dimension here, at zero, and an
    explicit rate says so where ``priced_by_aws=False`` would flatten "published
    at $0.00" and "no such dimension" together. It is also the only option that
    reaches the whole class — the register identifies a model by matching the
    entry's own input and output rates against a digest that excludes the flex and
    priority tiers on purpose, so half of these rows cannot be registered without
    widening a shared gate's exemption.
    """
    raw = yaml.safe_load(_PRICING_PATH.read_text())
    rows = _nova_rows_with_a_cache_write_rate(raw)
    registered = {
        entry.pricing_key
        for entry in EXPECTED_CACHE_UNIT_OMISSIONS
        if entry.unit == CACHE_WRITE
    }
    overlap = sorted(set(rows) & registered)
    assert not overlap, (
        f"{overlap} both price a cache write and register the omission of one. "
        "test_cache_unit_omission_basis.py requires the register's members to "
        "still be omissions, so one of the two has to go"
    )

    # And the units really are present in the shipped file for every Nova entry
    # that is selectable, which is what keeps the #1206 unpriced path out of this.
    for name in rows:
        assert classify_absent_unit(name, CACHE_WRITE, 39001.0) is not None


# --------------------------------------------------------------------------- #
# 4. The ratchet: if AWS ever starts charging, this has to fail
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_aws_still_publishes_no_chargeable_nova_cache_write():
    """A zero rate is a claim about AWS's price list, so check it against one.

    Read offline from the committed digest of AWS's two public offer files
    (``bedrock_cache_dimension_digest.py``, unauthenticated, refreshable with
    ``--refresh``) — the same digest ``test_cache_unit_omission_basis.py`` asserts
    its register against, so a zero rate here is held to the same standard as a
    registered omission rather than being asserted against nothing. If AWS starts
    charging for a Nova cache write, this fails and the rows have to be repriced.

    ⚠️ Scope, stated because it is narrower than the file's claim: the digest
    excludes the flex, priority and batch variants by design, so what is checked
    here is the **standard** tier. The flex and priority rows were read directly
    from the offer file when #1405 was fixed and are also $0.0000000000; covering
    them offline would mean a tier-aware digest, which is a change to a shared
    gate rather than to this file.
    """
    digest = json.loads(_DIGEST_PATH.read_text())
    models = digest["models"]
    nova = {
        name: record
        for name, record in models.items()
        if name.startswith("Nova") and record[CACHE_WRITE]
    }
    assert nova, (
        "the committed digest records no Nova model with a cache-write "
        "dimension at all, so it cannot support a claim either way. Refresh it: "
        "python3 lib/idp_common_pkg/tests/unit/reporting/"
        "bedrock_cache_dimension_digest.py --refresh"
    )
    chargeable = {
        name: record[CACHE_WRITE]
        for name, record in nova.items()
        if any(float(rate) > 0 for rate in record[CACHE_WRITE])
    }
    assert not chargeable, (
        f"AWS now publishes a NON-ZERO standard-tier cache-write rate for "
        f"{chargeable}. The $0.00 rates on the Nova rows in "
        "config_library/pricing.yaml are no longer correct and under-report "
        "spend. Reprice them from the offer file"
    )
