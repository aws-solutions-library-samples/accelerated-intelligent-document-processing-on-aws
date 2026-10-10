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
_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|au|jp|global|us-gov)\.")

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
