# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canada (``ca-*``) region handling in the configuration custom resource.

A stack in ca-central-1 gets the same deploy-time treatment as a US or EU one:
the handler recognises ``ca-*`` as its own region type, trims the model picklists
to what ca-central-1 can call, and rewrites the shipped defaults that are not
callable there through ``CA_MODEL_MAPPINGS``.

That table is kept apart from ``MODEL_MAPPINGS`` on purpose. ``get_model_mapping(
..., "us")`` walks ``MODEL_MAPPINGS`` backwards over its twin rows, so a Canada
row placed there would also rewrite a US deployment's stored id. The tests here
pin the Canada behaviour and, for every row, that the US and EU directions never
see it.

As in ``test_model_mappings``, nothing is parsed: the handler is imported and
called, so the invariants hold for the function the stack actually runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

# The handler, already loaded with ``cfnresponse`` stubbed, and the independent
# prefix rule used there to classify rows. pytest puts this directory on
# ``sys.path`` before importing either module, so the sibling import is
# collection-order independent (same pattern as
# scripts/sdlc/tests/test_apigw_vpc_reaper.py).
from test_model_mappings import _REGION_PREFIX, index

from idp_common.config.merge_utils import merge_config_with_defaults


def _string_leaves(data: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(dotted path, value)`` for every string leaf of a nested config."""
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _string_leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from _string_leaves(value, f"{path}[{i}]")
    elif isinstance(data, str):
        yield path, data


def _enum_offered(enum: list[str], region_type: str) -> list[str]:
    """Run one picklist through the handler's filter and return what survives."""
    schema = {"properties": {"model": {"type": "string", "enum": list(enum)}}}
    filtered = index.filter_models_by_region(schema, region_type)
    return filtered["properties"]["model"]["enum"]


# --- region detection -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "region,expected",
    [
        ("ca-central-1", True),
        ("ca-west-1", True),
        ("us-east-1", False),
        ("eu-west-1", False),
        ("us-gov-west-1", False),
    ],
)
def test_is_ca_region(region: str, expected: bool):
    assert index.is_ca_region(region) is expected


# --- the swap table ---------------------------------------------------------


@pytest.mark.unit
def test_the_canada_table_is_not_empty():
    """Guard the guard: the parametrised tests below are vacuous on an empty table."""
    assert index.CA_MODEL_MAPPINGS
    assert index.CA_UNAVAILABLE_MODELS


@pytest.mark.unit
@pytest.mark.parametrize("us_model", sorted(index.CA_MODEL_MAPPINGS))
def test_a_canada_row_is_a_one_way_lookup(us_model: str):
    """Forward, the row's target; backwards, never, and invisible to US and EU."""
    target = index.CA_MODEL_MAPPINGS[us_model]
    assert index.get_model_mapping(us_model, "ca") == target
    assert not target.startswith("eu."), "an eu. id is not callable from Canada"
    # The Canada table must not leak into the other two directions...
    assert not index.get_model_mapping(us_model, "us").startswith("ca.")
    assert not index.get_model_mapping(us_model, "eu").startswith("ca.")
    # ...and is never walked backwards: the target does not map back to its source.
    assert index.get_model_mapping(target, "us") != us_model


@pytest.mark.unit
def test_nova_lite_is_a_twin_and_nova_pro_is_a_rescue():
    """Pinned as literals: these are the two decisions the table records.

    Nova Lite has its own Canada geo profile, so its row is a twin (same model,
    processed in Canada). Nova Pro has no profile in ca-central-1, so its row
    lands on a different model: Nova 2 Lite, the catalog's current Nova default.
    Cohere Embed v4 is reachable from Canada only through its ``global.`` profile.
    """
    rows = index.CA_MODEL_MAPPINGS
    lite_target = rows["us.amazon.nova-lite-v1:0"]
    assert lite_target == "ca.amazon.nova-lite-v1:0"
    assert _REGION_PREFIX.sub("", lite_target) == _REGION_PREFIX.sub(
        "", "us.amazon.nova-lite-v1:0"
    )
    pro_target = rows["us.amazon.nova-pro-v1:0"]
    assert pro_target == "us.amazon.nova-2-lite-v1:0"
    assert _REGION_PREFIX.sub("", pro_target) != _REGION_PREFIX.sub(
        "", "us.amazon.nova-pro-v1:0"
    )
    assert rows["us.cohere.embed-v4:0"] == "global.cohere.embed-v4:0"


@pytest.mark.unit
def test_an_unmapped_model_is_returned_unchanged_for_canada():
    """A model the Canada table does not mention is left alone — including one
    that ``MODEL_MAPPINGS`` does know, so the Canada arm does not fall through to
    the EU lookup."""
    unknown = "us.example.not-a-real-model-v1:0"
    assert index.get_model_mapping(unknown, "ca") == unknown
    sonnet = "us.anthropic.claude-sonnet-4-6"
    assert sonnet in index.MODEL_MAPPINGS
    assert index.get_model_mapping(sonnet, "ca") == sonnet


# --- the picklist filter ----------------------------------------------------


@pytest.mark.unit
def test_canada_picklist_keeps_what_ca_central_1_can_call():
    """One id of every class, interleaved so order preservation is checked too.

    Kept: ``us.`` and ``ca.`` geo profiles, ``global.`` profiles, the US-prefixed
    OpenAI and xAI profiles that ca-central-1 serves, and ``LambdaHook``.
    Dropped: every ``eu.`` id, the GPT-5.x bedrock-mantle ids (they would route
    to us-east-1), and every id with no profile in ca-central-1.
    """
    kept = [
        "LambdaHook",
        "us.amazon.nova-2-lite-v1:0",
        "ca.amazon.nova-lite-v1:0",
        "global.anthropic.claude-sonnet-4-6",
        "us.openai.gpt-6-astra",
        "us.xai.grok-4.6",
    ]
    # Spelled out rather than read from the handler, so a deny-list entry that
    # goes missing is caught instead of vanishing from the expectation with it.
    no_profile_in_canada = [
        "us.amazon.nova-lite-v1:0",
        "us.amazon.nova-pro-v1:0",
        "us.cohere.embed-v4:0",
        "global.amazon.nova-pro-v1:0",
        "us.meta.llama4-maverick-17b-instruct-v1:0",
        "us.meta.llama4-scout-17b-instruct-v1:0",
        "qwen.qwen3-vl-235b-a22b",
        "google.gemma-3-27b-it",
        "nvidia.nemotron-nano-12b-v2",
    ]
    assert set(no_profile_in_canada) == index.CA_UNAVAILABLE_MODELS
    dropped = ["eu.amazon.nova-2-lite-v1:0", "openai.gpt-5.4", *no_profile_in_canada]
    enum: list[str] = []
    for i, item in enumerate(dropped):
        enum.append(item)
        if i < len(kept):
            enum.append(kept[i])
    assert set(enum) == set(kept) | set(dropped), "fixture lost an id"

    assert _enum_offered(enum, "ca") == kept


@pytest.mark.unit
def test_every_swap_target_is_offered_in_canada_and_every_source_is_not():
    """A swapped-in default must be selectable; the id it replaced must not be."""
    sources = sorted(index.CA_MODEL_MAPPINGS)
    targets = sorted(set(index.CA_MODEL_MAPPINGS.values()))
    assert _enum_offered([*sources, *targets], "ca") == targets


# --- the default swaps, on the real system defaults ------------------------


@pytest.mark.unit
def test_the_shipped_defaults_are_rewritten_for_canada_and_nothing_else_is():
    """The three stock defaults ca-central-1 cannot call, and only those."""
    cfg = merge_config_with_defaults({}, pattern="pattern-2", validate=False)
    before = dict(_string_leaves(cfg))
    # The stock values, so the assertions below are about the swap, not the fixture.
    assert before["extraction.confidence.model"] == "us.amazon.nova-lite-v1:0"
    assert before["discovery.with_ground_truth.model_id"] == "us.amazon.nova-pro-v1:0"
    assert (
        before["discovery.without_ground_truth.model_id"] == "us.amazon.nova-pro-v1:0"
    )
    assert (
        before["discovery.multi_document.embedding_model_id"] == "us.cohere.embed-v4:0"
    )
    assert before["classification.model"] == "us.amazon.nova-2-lite-v1:0"

    after = dict(_string_leaves(index.swap_model_ids(cfg, "ca")))
    assert after["extraction.confidence.model"] == "ca.amazon.nova-lite-v1:0"
    assert after["discovery.with_ground_truth.model_id"] == "us.amazon.nova-2-lite-v1:0"
    assert (
        after["discovery.without_ground_truth.model_id"] == "us.amazon.nova-2-lite-v1:0"
    )
    assert (
        after["discovery.multi_document.embedding_model_id"]
        == "global.cohere.embed-v4:0"
    )
    assert after["classification.model"] == "us.amazon.nova-2-lite-v1:0"
    assert after.keys() == before.keys()
    assert {path for path in before if before[path] != after[path]} == {
        "extraction.confidence.model",
        "discovery.with_ground_truth.model_id",
        "discovery.without_ground_truth.model_id",
        "discovery.multi_document.embedding_model_id",
    }


@pytest.mark.unit
@pytest.mark.parametrize("region_type", ["us", "eu"])
def test_the_canada_table_never_reaches_a_us_or_eu_swap(region_type: str):
    cfg = merge_config_with_defaults({}, pattern="pattern-2", validate=False)
    leaked = {
        path: value
        for path, value in _string_leaves(index.swap_model_ids(cfg, region_type))
        if value.startswith("ca.")
    }
    assert leaked == {}


# --- handler wiring ---------------------------------------------------------


def _run_create(monkeypatch: pytest.MonkeyPatch, region: str) -> MagicMock:
    """Run a Create event through ``handler`` with the stack region patched.

    Returns the ``ConfigurationManager`` double so a test can read what the
    handler asked it to save. Everything that would reach AWS is replaced: the
    region lookup, the manager, and the CloudFormation response callback. The
    ``cfnresponse`` stub installed by ``test_model_mappings`` has only ``send``,
    so the two status constants the handler reads are supplied here as well.
    """
    manager = MagicMock()
    monkeypatch.setattr(index, "get_current_region", lambda: region)
    monkeypatch.setattr(index, "ConfigurationManager", MagicMock(return_value=manager))
    monkeypatch.setattr(index.cfnresponse, "SUCCESS", "SUCCESS", raising=False)
    monkeypatch.setattr(index.cfnresponse, "FAILED", "FAILED", raising=False)
    send = MagicMock()
    monkeypatch.setattr(index.cfnresponse, "send", send)

    event = {
        "RequestType": "Create",
        "StackId": "stack-id",
        "LogicalResourceId": "ConfigurationCustomResource",
        "ResourceProperties": {
            "ServiceToken": "service-token",
            "Schema": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": [
                            "us.amazon.nova-lite-v1:0",
                            "eu.amazon.nova-lite-v1:0",
                            "ca.amazon.nova-lite-v1:0",
                            "global.anthropic.claude-sonnet-4-6",
                        ],
                    }
                },
            },
            "Default": {
                "extraction": {"confidence": {"model": "us.amazon.nova-lite-v1:0"}}
            },
        },
    }
    index.handler(event, None)
    # The handler reports failures to CloudFormation instead of raising, so a
    # broken run would otherwise surface only as "nothing was saved".
    assert send.call_args.args[2] == "SUCCESS", send.call_args
    return manager


def _saved(manager: MagicMock) -> tuple[list[str], str]:
    """The Schema enum and the default config's confidence model, as saved."""
    calls = {call.args[0]: call for call in manager.save_configuration.call_args_list}
    assert calls.keys() == {"Schema", "Config"}, calls
    assert calls["Config"].kwargs["version"] == "default"
    enum = calls["Schema"].args[1]["properties"]["model"]["enum"]
    confidence = calls["Config"].args[1]["extraction"]["confidence"]["model"]
    return enum, confidence


@pytest.mark.unit
def test_a_canada_stack_filters_its_picklists_and_swaps_its_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The only test that proves both deploy-time gates include ``"ca"``."""
    caplog.set_level(logging.INFO)
    manager = _run_create(monkeypatch, "ca-central-1")
    assert "Detected region: ca-central-1, region type: ca" in caplog.text

    enum, confidence = _saved(manager)
    assert enum == ["ca.amazon.nova-lite-v1:0", "global.anthropic.claude-sonnet-4-6"]
    assert confidence == "ca.amazon.nova-lite-v1:0"


@pytest.mark.unit
@pytest.mark.parametrize("region", ["us-east-1", "us-gov-west-1"])
def test_a_us_stack_is_unchanged_by_the_canada_support(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, region: str
):
    """US regression guard, GovCloud included: ``us-gov-*`` still resolves to
    the US type and the Canada profile is not offered."""
    caplog.set_level(logging.INFO)
    manager = _run_create(monkeypatch, region)
    assert f"Detected region: {region}, region type: us" in caplog.text

    enum, confidence = _saved(manager)
    assert enum == ["us.amazon.nova-lite-v1:0", "global.anthropic.claude-sonnet-4-6"]
    assert confidence == "us.amazon.nova-lite-v1:0"
