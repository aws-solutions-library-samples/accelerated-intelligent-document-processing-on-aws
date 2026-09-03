# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluation-baseline migration for multi-instance sections (GitHub #715).

Turning on ``x-aws-idp-multi-instance`` changes the shape of a class's
``inference_result``. Evaluation compares a prediction against a stored baseline
**of the same shape**, so a wrapped prediction against a flat baseline scores
every field as missing-on-one-side and the class's accuracy collapses to ~0 with
no error anywhere. That is the one way this feature can break a working
deployment, which is why the converter is a first-class, tested artifact rather
than a sed command in the docs.
"""

from __future__ import annotations

import pytest

from idp_common.evaluation.baseline_migration import (
    baseline_instance_count,
    multi_instance_class_labels,
    section_class_label,
    unwrap_baseline_result,
    wrap_baseline_result,
)

pytestmark = pytest.mark.unit

FLAT = {
    "document_class": {"type": "Pay-Statement"},
    "split_document": {"page_indices": [0, 1]},
    "inference_result": {"CheckNumber": "77310468", "NetPay": "4,104.59"},
}


def test_a_flat_baseline_is_wrapped():
    migrated, changed = wrap_baseline_result(FLAT)
    assert changed is True
    assert migrated["inference_result"] == {
        "instances": [{"CheckNumber": "77310468", "NetPay": "4,104.59"}]
    }
    # Everything else is carried through untouched.
    assert migrated["document_class"] == FLAT["document_class"]
    assert migrated["split_document"] == FLAT["split_document"]


def test_migration_is_idempotent():
    """A partially-applied run is the normal outcome of an interrupted bulk job,
    so re-running must be safe."""
    once, _ = wrap_baseline_result(FLAT)
    twice, changed = wrap_baseline_result(once)
    assert changed is False
    assert twice == once


def test_the_input_is_never_mutated():
    before = repr(FLAT)
    wrap_baseline_result(FLAT)
    assert repr(FLAT) == before


def test_an_empty_or_absent_inference_result_is_left_alone():
    """An empty baseline carries no ground truth to preserve, and
    ``{"instances": [{}]}`` would assert "exactly one record with no values",
    which scores worse than the honest empty."""
    for payload in (
        {**FLAT, "inference_result": {}},
        {k: v for k, v in FLAT.items() if k != "inference_result"},
        {**FLAT, "inference_result": None},
        {**FLAT, "inference_result": []},
    ):
        migrated, changed = wrap_baseline_result(payload)
        assert changed is False
        assert migrated == payload


@pytest.mark.parametrize("payload", [None, "text", 3, []])
def test_non_baseline_inputs_are_tolerated(payload):
    migrated, changed = wrap_baseline_result(payload)
    assert changed is False
    assert migrated is payload


def test_a_multi_record_baseline_survives_a_round_trip():
    wrapped = {
        **FLAT,
        "inference_result": {"instances": [{"CheckNumber": "1"}, {"CheckNumber": "2"}]},
    }
    assert baseline_instance_count(wrapped) == 2
    # And is NOT re-wrapped.
    _, changed = wrap_baseline_result(wrapped)
    assert changed is False


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


def test_rollback_unwraps_a_single_instance_baseline():
    wrapped, _ = wrap_baseline_result(FLAT)
    back, changed = unwrap_baseline_result(wrapped)
    assert changed is True
    assert back["inference_result"] == FLAT["inference_result"]


def test_rollback_refuses_to_discard_ground_truth():
    """Flattening a 3-record baseline would silently throw away two records the
    user authored — the exact data loss this whole feature exists to stop."""
    wrapped = {
        **FLAT,
        "inference_result": {
            "instances": [
                {"CheckNumber": "1"},
                {"CheckNumber": "2"},
                {"CheckNumber": "3"},
            ]
        },
    }
    back, changed = unwrap_baseline_result(wrapped)
    assert changed is False
    assert back == wrapped


def test_rollback_leaves_an_already_flat_baseline_alone():
    back, changed = unwrap_baseline_result(FLAT)
    assert changed is False
    assert back == FLAT


def test_baseline_instance_count_returns_none_when_not_wrapped():
    assert baseline_instance_count(FLAT) is None
    assert baseline_instance_count(None) is None


# --------------------------------------------------------------------------
# Deciding WHICH sections to migrate
# --------------------------------------------------------------------------


def test_only_flagged_classes_are_selected():
    labels = multi_instance_class_labels(
        [
            {"$id": "Pay-Statement", "x-aws-idp-multi-instance": True},
            {
                "$id": "Bank Statement",
                "x-aws-idp-document-type": "Bank Statement",
                "x-aws-idp-instance-array": "records",
            },
            {"$id": "W2"},
            "not-a-class",
        ]
    )
    assert labels == {"pay-statement"}


def test_both_identity_keys_are_indexed():
    """A class can be found by $id or x-aws-idp-document-type, and the two are not
    always the same string."""
    labels = multi_instance_class_labels(
        [
            {
                "$id": "pay_statement",
                "x-aws-idp-document-type": "Pay-Statement",
                "x-aws-idp-multi-instance": True,
            }
        ]
    )
    assert labels == {"pay_statement", "pay-statement"}


def test_string_true_from_a_config_round_trip_still_selects_the_class():
    assert multi_instance_class_labels(
        [{"$id": "X", "x-aws-idp-multi-instance": "true"}]
    ) == {"x"}


def test_section_class_label_reads_document_class_type():
    assert section_class_label(FLAT) == "Pay-Statement"
    assert section_class_label({"document_class": {}}) is None
    assert section_class_label({}) is None
    assert section_class_label(None) is None
