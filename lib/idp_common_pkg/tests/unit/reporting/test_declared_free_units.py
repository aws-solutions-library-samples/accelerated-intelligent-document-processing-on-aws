# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The browser's copy of the absent-unit verdict is derived, not re-decided.

The owning test for ``idp_common.metering_units.declared_free_units``, which is
how the web UI's document cost table gets this module's answer without being able
to import it. The panel used to port ``_get_unit_cost`` by hand and resolve every
unit a matched pricing entry omits to ``0`` — so a row the reporting table
recorded as NULL-with-a-warning was displayed as a confident $0.00 contribution,
which is the one thing the NULL exists to avoid. Issue
[#1406](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1406).

A derived copy is only worth having if it cannot drift from what it derives from,
so that is the property under test rather than the contents of the answer:

* **Equivalence with the runtime rule, over a universe read from the shipped
  file.** For every pricing key ``config_library/pricing.yaml`` ships and every
  unit any entry in it names, membership of :func:`declared_free_units` must
  agree with :func:`classify_absent_unit` at a non-zero count. A unit added to
  one register and not reflected in the expansion fails here, as does an
  expansion that invents one.
* **Both polarities are exercised.** An equivalence that only ever compares
  "absent, and unknown" is satisfied by a function that returns nothing at all,
  which is precisely the pre-#1212 behaviour one level up. The sweep therefore
  records how many of each verdict it saw and fails if either is zero.
* **The zero-count rule stays out.** It is arithmetic over a per-row count, not a
  declaration about a key, and a declaration that quietly absorbed it would make
  every unit free for the rows where it happens to be 0 and not for the rest.
* **Scope is the pricing key's.** ``requests`` is declared free for ``bedrock``
  and is a real priced dimension of AWS Lambda, so the answer for
  ``lambda/requests`` has to be empty; and a *metering* key passed in by mistake
  has to answer empty rather than inheriting the phase name's scope.

The few literal expectations below are transcribed by hand from the declarations
in ``metering_units``, never computed from them, so emptying a register fails
here instead of being reproduced by the test.
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from idp_common.metering_units import (
    EXPECTED_CACHE_UNIT_OMISSIONS,
    NOT_CHARGEABLE,
    UNKNOWN,
    classify_absent_unit,
    declared_free_units,
)

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)
_PRICING_PATH = os.path.join(_REPO_ROOT, "config_library", "pricing.yaml")

#: A count that is not zero, so the zero-count branch of the runtime rule never
#: decides a comparison below. The expansion is a statement about a key and a
#: unit; the count is the caller's business.
A_REAL_COUNT = 5000

#: Floors on the derived universe. An empty universe makes every parametrised
#: assertion collect as a skip, which reads green and proves nothing — these are
#: well under the shipped figures and exist only to refuse that state.
MIN_KEYS = 20
MIN_UNITS = 5


def _shipped() -> tuple[list[str], list[str]]:
    """``(pricing keys, unit names)`` as ``config_library/pricing.yaml`` ships them.

    Read from the YAML rather than from a loaded configuration object, because the
    file a deployment installs is what both cost implementations resolve against
    and what the ``getPricing`` response is built from.

    ``totalTokens`` is added to the unit side on purpose: no shipped entry prices
    it, so a universe taken from the file alone would never ask about the one unit
    every Bedrock call meters and the service-wide register declares.
    """
    with open(_PRICING_PATH) as handle:
        raw = yaml.safe_load(handle)
    keys = [entry["name"] for entry in raw["pricing"]]
    names = {"totalTokens"}
    for entry in raw["pricing"]:
        for unit in entry.get("units") or []:
            names.add(unit["name"])
    return keys, sorted(names)


SHIPPED_KEYS, SHIPPED_UNITS = _shipped()


def test_the_derived_universe_is_not_empty():
    assert len(SHIPPED_KEYS) >= MIN_KEYS, SHIPPED_KEYS
    assert len(SHIPPED_UNITS) >= MIN_UNITS, SHIPPED_UNITS


@pytest.mark.unit
@pytest.mark.parametrize("pricing_key", SHIPPED_KEYS)
def test_the_expansion_agrees_with_the_runtime_rule(pricing_key):
    """Membership of the expansion is exactly a ``NOT_CHARGEABLE`` verdict.

    This is the property that makes the browser's copy a derivation rather than a
    fourth implementation: the only way the panel can disagree with the reporting
    table about a declared unit is for this assertion to fail.
    """
    expansion = set(declared_free_units(pricing_key))
    for unit in SHIPPED_UNITS:
        verdict = classify_absent_unit(pricing_key, unit, A_REAL_COUNT)
        assert (unit in expansion) == (verdict == NOT_CHARGEABLE), (
            f"{pricing_key} / {unit}: the expansion says "
            f"{'free' if unit in expansion else 'unknown'} and the runtime rule "
            f"says {verdict}"
        )


@pytest.mark.unit
def test_the_sweep_sees_both_verdicts():
    """Neither polarity of the comparison above is vacuous.

    An expansion that returned nothing would satisfy every ``UNKNOWN`` comparison
    and be exactly the understating behaviour this replaces; one that returned
    every unit would satisfy every ``NOT_CHARGEABLE`` one and be worse. Counting
    both is what makes the equivalence test mean something.
    """
    free = 0
    unknown = 0
    for pricing_key in SHIPPED_KEYS:
        expansion = set(declared_free_units(pricing_key))
        for unit in SHIPPED_UNITS:
            if unit in expansion:
                free += 1
            elif classify_absent_unit(pricing_key, unit, A_REAL_COUNT) == UNKNOWN:
                unknown += 1
    assert free > 0, "no shipped pricing key declares any unit free"
    assert unknown > 0, "every shipped (key, unit) pair reads as free"


@pytest.mark.unit
def test_bedrock_declares_its_two_non_billing_dimensions_free():
    """Hand-transcribed from the service-wide register, not computed from it."""
    assert declared_free_units("bedrock/us.amazon.nova-lite-v1:0") == (
        "requests",
        "totalTokens",
    )


@pytest.mark.unit
def test_a_service_with_no_declaration_declares_nothing():
    """Textract prices ``pages`` and declares no free unit.

    So ``inputTokens`` metered against a Textract entry is unpriced, which is the
    answer the reporting table records for it. The panel used to show $0.00.
    """
    assert declared_free_units("textract/detect_document_text") == ()
    assert declared_free_units("bda/documents-standard") == ()


@pytest.mark.unit
def test_the_scope_is_the_services_and_not_a_units():
    """``requests`` is free for Bedrock and priced for AWS Lambda.

    One declaration covering both services would be false for one of them, which
    is what the per-service scope is for — and an expansion that flattened the
    register to a bare set of unit names would lose exactly this.
    """
    assert "requests" in declared_free_units("bedrock/amazon.nova-pro-v1:0")
    assert declared_free_units("lambda/requests") == ()
    assert declared_free_units("lambda/duration") == ()


@pytest.mark.unit
def test_a_metering_key_answers_empty_rather_than_guessing():
    """The leading component of a metering key is a phase, not a service.

    Reading it as one would make every declaration inert for a caller that forgot
    to resolve the pricing key first; answering empty makes that caller render the
    unit as unpriced, which is the safe direction and is visible.
    """
    assert declared_free_units("Extraction/bedrock/amazon.nova-pro-v1:0") == ()
    assert declared_free_units("OCR/textract/detect_document_text") == ()


@pytest.mark.unit
def test_an_unknown_key_declares_nothing():
    assert declared_free_units("sagemaker/endpoint") == ()
    assert declared_free_units("") == ()


@pytest.mark.unit
def test_a_priced_unit_is_not_declared_free():
    """A unit the entry itself prices is never in the expansion.

    The expansion answers the *absent*-unit question only. A consumer reads the
    entry's own rate first, so a unit appearing in both would be harmless there —
    but it would also be a claim that a priced dimension is free, which is not a
    claim this module makes.
    """
    for unit in ("inputTokens", "outputTokens", "pages", "documents", "gb_seconds"):
        for pricing_key in SHIPPED_KEYS:
            assert unit not in declared_free_units(pricing_key), (
                f"{pricing_key} declares the priced unit {unit} free"
            )


@pytest.mark.unit
def test_a_dimension_aws_prices_is_reported_rather_than_declared_free():
    """The ``priced_by_aws=True`` members stay out of the expansion.

    Those entries rest on a premise about this tree — the model id is absent from
    ``CACHEPOINT_SUPPORTED_MODELS``, so no cache token can be metered against the
    key — which can go stale in one commit. A cache token that does turn up there
    has to reach the user as unpriced, in the browser as well as in the reporting
    table, so the expansion must not carry it.
    """
    priced = [entry for entry in EXPECTED_CACHE_UNIT_OMISSIONS if entry.priced_by_aws]
    assert priced, "no member claims AWS prices the dimension; this test is vacuous"
    for entry in priced:
        assert entry.unit not in declared_free_units(entry.pricing_key), (
            f"{entry.pricing_key} / {entry.unit} is declared free although the "
            f"register records that AWS prices the dimension"
        )


@pytest.mark.unit
def test_a_cache_unit_with_a_recorded_basis_is_declared_free():
    """The other polarity of the same register, and the per-key scope of it.

    Claude 3 Haiku has no cache dimension in any region, so both cache units are
    free on that key; the model next to it in the shipped file has a published
    cache-read rate and so declares neither.
    """
    haiku = declared_free_units("bedrock/us.anthropic.claude-3-haiku-20240307-v1:0")
    assert "cacheReadInputTokens" in haiku
    assert "cacheWriteInputTokens" in haiku
    assert "cacheReadInputTokens" not in declared_free_units(
        "bedrock/us.amazon.nova-premier-v1:0"
    )


@pytest.mark.unit
def test_the_zero_count_rule_is_not_folded_in():
    """A count of zero costs nothing at any rate, and that is not a declaration.

    ``inputTokens`` metered as 0 against a ``pages``-only hook entry is free
    because of the zero, which the runtime rule answers and the expansion must
    not: the expansion is per key and per unit, and a caller applying it to a row
    with a real count would otherwise read a chargeable dimension as free.
    """
    key = "lambda_hook/GENAIIDP-mistral-ocr-hook"
    assert "inputTokens" not in declared_free_units(key)
    assert classify_absent_unit(key, "inputTokens", 0) == NOT_CHARGEABLE
    assert classify_absent_unit(key, "inputTokens", A_REAL_COUNT) == UNKNOWN


@pytest.mark.unit
def test_the_answer_is_sorted_json_serializable_strings():
    """It is serialized into an API response, so the shape is part of the contract.

    Sorted so that a diff in the serialized form is a change of substance rather
    than of iteration order.
    """
    answer = declared_free_units("bedrock/amazon.nova-lite-v1:0")
    assert isinstance(answer, tuple)
    assert all(isinstance(unit, str) for unit in answer)
    assert list(answer) == sorted(answer)
    assert json.loads(json.dumps(list(answer))) == list(answer)
