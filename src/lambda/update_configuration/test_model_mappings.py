# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The US<->EU model swap table, and the direction that used to be wrong.

``MODEL_MAPPINGS`` holds two kinds of row, and until now nothing distinguished
them:

* a **twin** row maps a model onto its own inference profile in the other region
  (``us.anthropic.claude-sonnet-4-6`` -> ``eu.anthropic.claude-sonnet-4-6``).
  Reversing it returns the user to the model they chose, so it is correct in both
  directions.
* a **rescue** row maps a retired or EU-unavailable model onto a *different*, live
  EU model, so that a stored configuration naming the dead one still runs. It is
  meaningful in one direction only.

``get_model_mapping(..., "us")`` walked the dict in insertion order and returned
the first row whose target matched, which was a rescue row in two live cases: the
reverse of ``eu.anthropic.claude-haiku-4-5-20251001-v1:0`` was end-of-life Claude
3 Haiku, and the reverse of ``eu.anthropic.claude-sonnet-4-5-20250929-v1:0`` was
end-of-life Nova Premier. Redeploying an EU stack into a US region therefore moved
it onto a model Bedrock no longer serves.

These tests parse nothing: they import the handler and call it, so the invariants
hold for the function the stack actually runs.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_handler() -> ModuleType:
    """Import ``index`` with the Lambda-provided ``cfnresponse`` stubbed out.

    ``cfnresponse`` is injected by the CloudFormation custom-resource runtime and
    is not installable, so a unit test has to supply it. ``idp_common`` is NOT
    stubbed — the handler reads ``base_model_id`` out of it, which is the point of
    the same-model comparison below, and the provenance guard in
    ``src/lambda/conftest.py`` is what pins which checkout it comes from.
    """
    sys.modules.setdefault("cfnresponse", SimpleNamespace(send=lambda *a, **k: None))
    path = Path(__file__).resolve().parent / "index.py"
    spec = importlib.util.spec_from_file_location("update_configuration_index", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


index = _load_handler()

#: Classify the rows with an INDEPENDENT prefix rule rather than by calling the
#: handler's own helper. Reusing the implementation's definition of "same model"
#: would make these assertions circular, and it would make them ERROR rather than
#: FAIL against a revision that has no such helper — which is how the bug shipped.
_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|au|jp|ca|global|us-gov)\.")

MAPPINGS: dict[str, str] = index.MODEL_MAPPINGS
TWINS = {
    us: eu
    for us, eu in MAPPINGS.items()
    if _REGION_PREFIX.sub("", us) == _REGION_PREFIX.sub("", eu)
}
RESCUES = {us: eu for us, eu in MAPPINGS.items() if us not in TWINS}


@pytest.mark.unit
def test_the_table_has_both_kinds_of_row():
    """Guard the guard: if either set were empty the tests below would be vacuous."""
    assert TWINS, "no twin rows found; _base_model_id or the table shape changed"
    assert RESCUES, "no rescue rows found; these tests would prove nothing"


@pytest.mark.unit
@pytest.mark.parametrize("us_model", sorted(TWINS))
def test_a_twin_row_round_trips(us_model: str):
    eu_model = TWINS[us_model]
    assert index.get_model_mapping(us_model, "eu") == eu_model
    assert index.get_model_mapping(eu_model, "us") == us_model


@pytest.mark.unit
@pytest.mark.parametrize("us_model", sorted(RESCUES))
def test_a_rescue_row_is_never_reversed(us_model: str):
    """The forward direction rescues; the reverse must not undo it.

    Reversing a rescue row would move a working EU configuration onto the retired
    or US-only model the row exists to escape.
    """
    eu_model = RESCUES[us_model]
    assert index.get_model_mapping(us_model, "eu") == eu_model
    assert index.get_model_mapping(eu_model, "us") != us_model


@pytest.mark.unit
@pytest.mark.parametrize(
    "eu_model,expected_us",
    [
        (
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ),
        (
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
    ],
)
def test_the_two_reversals_that_returned_a_dead_model(eu_model: str, expected_us: str):
    """Pinned as literals, not derived, because these are the observed regressions.

    An insertion-order walk returned ``us.anthropic.claude-3-haiku-20240307-v1:0``
    and ``us.amazon.nova-premier-v1:0`` here; both are end-of-life.
    """
    assert index.get_model_mapping(eu_model, "us") == expected_us


@pytest.mark.unit
@pytest.mark.parametrize("us_model", sorted(RESCUES))
def test_every_rescue_target_also_has_a_twin_row(us_model: str):
    """So the reverse direction always has an answer to give.

    A rescue target with no twin row would leave ``get_model_mapping(target,
    "us")`` returning the ``eu.`` id unchanged — not callable in a US region. A
    model worth rescuing onto is one this repo offers in both regions.
    """
    eu_model = RESCUES[us_model]
    assert eu_model in TWINS.values(), (
        f"{us_model} is rescued onto {eu_model}, which has no twin row, so an EU "
        "stack redeployed into a US region would keep an eu. id it cannot call"
    )


@pytest.mark.unit
def test_an_unmapped_model_is_returned_unchanged():
    """Both directions leave a model the table does not mention alone."""
    unknown = "us.example.not-a-real-model-v1:0"
    assert index.get_model_mapping(unknown, "eu") == unknown
    assert index.get_model_mapping(unknown, "us") == unknown
    assert index.get_model_mapping(unknown, "something-else") == unknown


@pytest.mark.unit
def test_a_canada_profile_is_offered_in_neither_us_nor_eu_picklists():
    """A geo profile for one region type must not appear in another's picklist.

    The US and EU filters keep ids with no geo prefix (``global.``, unprefixed),
    so every geo-scoped prefix has to be excluded explicitly.
    """
    enum = [
        "us.amazon.nova-2-lite-v1:0",
        "eu.amazon.nova-2-lite-v1:0",
        "ca.amazon.nova-lite-v1:0",
        "global.anthropic.claude-sonnet-4-6",
        "qwen.qwen3-vl-235b-a22b",
        "LambdaHook",
    ]
    schema = {"properties": {"model": {"type": "string", "enum": list(enum)}}}

    for region_type, other in (("us", "eu"), ("eu", "us")):
        offered = index.filter_models_by_region(schema, region_type)
        offered = offered["properties"]["model"]["enum"]
        assert "ca.amazon.nova-lite-v1:0" not in offered, region_type
        assert f"{other}.amazon.nova-2-lite-v1:0" not in offered, region_type
        assert f"{region_type}.amazon.nova-2-lite-v1:0" in offered, region_type
        assert "global.anthropic.claude-sonnet-4-6" in offered, region_type
        assert "qwen.qwen3-vl-235b-a22b" in offered, region_type
        assert "LambdaHook" in offered, region_type


@pytest.mark.unit
@pytest.mark.parametrize("region_type", ["us", "eu", "ca"])
def test_a_geo_profile_outside_the_region_type_is_offered_nowhere(region_type: str):
    """The filters read geo prefixes from the shared prefix rule, not a local list.

    ``apac.``, ``jp.`` and ``au.`` profiles are callable from none of the US, EU
    or Canada region types, so no picklist offers them. ``global.`` and
    unprefixed ids stay offered in all three. ``us-gov.`` has its own rule, below.
    """
    geo_scoped = [
        "apac.amazon.nova-lite-v1:0",
        "jp.anthropic.claude-haiku-5-5",
        "au.anthropic.claude-haiku-5-5",
    ]
    region_agnostic = [
        "global.anthropic.claude-sonnet-4-6",
        "example.unprefixed-model-v1:0",
    ]
    # The us. entry makes the filter treat this list as a model list.
    enum = ["us.amazon.nova-2-lite-v1:0", *geo_scoped, *region_agnostic]
    schema = {"properties": {"model": {"type": "string", "enum": enum}}}

    offered = index.filter_models_by_region(schema, region_type)
    offered = offered["properties"]["model"]["enum"]
    for model_id in geo_scoped:
        assert model_id not in offered, (region_type, model_id)
    for model_id in region_agnostic:
        assert model_id in offered, (region_type, model_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "region_type,expected", [("us", True), ("eu", False), ("ca", False)]
)
def test_a_govcloud_profile_is_offered_only_in_the_us_region_type(
    region_type: str, expected: bool
):
    """GovCloud (``us-gov-*``) stacks resolve to the ``us`` region type, so the US
    filter keeps ``us-gov.`` ids, as it did before Canada support. EU and Canada
    stacks cannot call them. A commercial US stack is offered one too; no shipped
    picklist carries a ``us-gov.`` id.
    """
    model_id = "us-gov.anthropic.claude-sonnet-4-6"
    # The us. entry makes the filter treat this list as a model list.
    enum = ["us.amazon.nova-2-lite-v1:0", model_id]
    schema = {"properties": {"model": {"type": "string", "enum": enum}}}

    offered = index.filter_models_by_region(schema, region_type)
    assert (model_id in offered["properties"]["model"]["enum"]) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "region_type,ca_offered", [("us", False), ("eu", False), ("ca", True)]
)
def test_a_list_with_no_us_or_eu_id_is_still_a_model_picklist(
    region_type: str, ca_offered: bool
):
    """A Canada-only picklist has to be recognised as a picklist.

    ``filter_models_by_region`` decides a list is a model list by finding a geo id
    in it. While that test named only ``us.`` and ``eu.``, a list whose geo ids
    were all Canada ones fell through to the generic recursion untouched — so a US
    or EU stack would be offered a ``ca.`` id it cannot call, with no filtering
    applied to that list at all. Every shipped picklist carries ``us.`` entries,
    which is why nothing is wrong today and why this case needs its own test.
    """
    ca_model = "ca.amazon.nova-lite-v1:0"
    everywhere = "global.anthropic.claude-sonnet-4-6"
    schema = {
        "properties": {"model": {"type": "string", "enum": [ca_model, everywhere]}}
    }

    offered = index.filter_models_by_region(schema, region_type)
    offered = offered["properties"]["model"]["enum"]
    assert (ca_model in offered) is ca_offered, region_type
    assert everywhere in offered, (
        f"a global. id is callable from {region_type} and must survive the filter"
    )
