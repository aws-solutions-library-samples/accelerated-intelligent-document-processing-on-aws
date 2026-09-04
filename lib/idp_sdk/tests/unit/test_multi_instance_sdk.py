# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SDK surface for multi-instance sections (GenAI IDP #715).

A class flagged ``x-aws-idp-multi-instance`` returns
``inference_result = {"instances": [...]}``, which is a **public API shape
change**: ``fields`` becomes the wrapper, and the top-level-keys-only
``confidence`` map goes completely empty because ``explainability_info[0]
["instances"]`` is a list, not a confidence leaf.

Both changes here are ADDITIVE — ``fields`` still carries the raw shape — so no
existing caller breaks:

* a new ``instances`` key, so a caller can iterate per-document records without
  reading the deployment's configuration;
* ``confidence`` now walks groups and lists, keying leaves as ``Group.Sub`` /
  ``List[0].Sub``. Top-level scalar keys are unchanged, and list attributes —
  which previously contributed nothing at all — now appear.
"""

from __future__ import annotations

import pytest

from idp_sdk._core.document_processor import _collect_confidence, _section_instances

pytestmark = pytest.mark.unit

THREE_RECORDS = [
    {"CheckNumber": "77310468", "NetPay": "4,104.59"},
    {"CheckNumber": "77298351", "NetPay": "4,657.95"},
    {"CheckNumber": "77284207", "NetPay": "16,487.56"},
]


def _leaf(c):
    return {"confidence": c, "confidence_threshold": 0.8}


def test_instances_is_exposed_for_a_wrapped_section():
    assert _section_instances({"instances": THREE_RECORDS}) == THREE_RECORDS


def test_a_single_record_section_gets_none_so_callers_can_branch_on_it():
    assert _section_instances({"CheckNumber": "1"}) is None


@pytest.mark.parametrize(
    "value", [None, "text", 3, [], {"instances": "not-a-list"}, {"instances": None}]
)
def test_instances_is_tolerant(value):
    assert _section_instances(value) is None


def test_non_object_elements_are_skipped():
    assert _section_instances({"instances": [{"a": 1}, "junk", None]}) == [{"a": 1}]


def test_confidence_is_not_empty_for_a_wrapped_section():
    out: dict = {}
    _collect_confidence(
        {"instances": [{"NetPay": _leaf(0.9)}, {"NetPay": _leaf(0.8)}]}, "", out
    )
    assert out == {"instances[0].NetPay": 0.9, "instances[1].NetPay": 0.8}


def test_top_level_scalar_confidence_keys_are_unchanged():
    out: dict = {}
    _collect_confidence({"NetPay": _leaf(0.9), "CheckNumber": _leaf(1.0)}, "", out)
    assert out == {"NetPay": 0.9, "CheckNumber": 1.0}


def test_confidence_walks_groups_and_lists():
    out: dict = {}
    _collect_confidence(
        {"Employee": {"Name": _leaf(0.7)}, "Earnings": [{"Amt": _leaf(0.6)}]}, "", out
    )
    assert out == {"Employee.Name": 0.7, "Earnings[0].Amt": 0.6}


def test_leaf_metadata_keys_are_not_mistaken_for_nested_fields():
    out: dict = {}
    _collect_confidence(
        {
            "NetPay": {
                "confidence": 0.9,
                "confidence_reason": "clear",
                "confidence_threshold": 0.8,
                "geometry": [{"page": 1}],
            }
        },
        "",
        out,
    )
    assert out == {"NetPay": 0.9}


def test_an_unscored_leaf_contributes_nothing_rather_than_a_null():
    out: dict = {}
    _collect_confidence({"NetPay": _leaf(None)}, "", out)
    assert out == {}


def test_a_boolean_is_not_treated_as_a_confidence_number():
    out: dict = {}
    _collect_confidence({"Flag": {"confidence": True}}, "", out)
    assert out == {}
