# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Downstream compatibility for multi-instance sections (GitHub #715).

Every stage below reads the class schema **from config**, not from the extraction
output, so each derives the wrapper independently. These tests pin the specific
breakages the issue enumerates — each one is silent, which is why they are pinned
rather than left to a live run to notice:

* reporting — ``_flatten_json_data`` ``json.dumps``'s a list into ONE opaque
  Athena column, collapsing N columns to 1 and NULLing every dashboard query.
* analytics agent — teaches Athena column names from the flat schema.
* Z3 rule engine — dot-path extractor has no ``[i]`` subscript and treats a miss
  as "optional param absent", so rules quietly stop firing.
* confidence-curve store — inconsistent key paths between 1- and N-instance
  sections, so before/after curves do not join.
* public SDK — ``fields`` becomes ``{"instances": [...]}`` and the top-level
  ``confidence`` map goes empty.
* evaluation — a flat baseline against a wrapped prediction scores every field as
  missing-on-one-side, i.e. silently ~0.
"""

from __future__ import annotations

import pytest

from idp_common.schema.multi_instance import INSTANCES_KEY

pytestmark = pytest.mark.unit


PAY_CLASS = {
    "$id": "Pay-Statement",
    "type": "object",
    "x-aws-idp-document-type": "Pay-Statement",
    "x-aws-idp-multi-instance": True,
    "properties": {
        "CheckNumber": {"type": "string"},
        "NetPay": {"type": "string"},
    },
}

SINGLE_CLASS = {
    "$id": "W2",
    "type": "object",
    "x-aws-idp-document-type": "W2",
    "properties": {"EmployerName": {"type": "string"}},
}

THREE_RECORDS = [
    {"CheckNumber": "77310468", "NetPay": "4,104.59"},
    {"CheckNumber": "77298351", "NetPay": "4,657.95"},
    {"CheckNumber": "77284207", "NetPay": "16,487.56"},
]


# --------------------------------------------------------------------------
# Reporting: N Athena columns, not one opaque blob
# --------------------------------------------------------------------------


def _reporting_service(classes):
    from idp_common.config.models import IDPConfig
    from idp_common.reporting.save_reporting_data import SaveReportingData

    return SaveReportingData(
        reporting_bucket="bucket", config=IDPConfig(classes=classes)
    )


def _extraction_json(inference_result):
    return {
        "document_class": {"type": "Pay-Statement"},
        "split_document": {"page_indices": [0, 1, 2, 3]},
        "inference_result": inference_result,
        "metadata": {"instance_count": 3},
    }


def test_reporting_fans_a_wrapped_section_out_to_one_row_per_instance():
    svc = _reporting_service([PAY_CLASS])
    data = _extraction_json({INSTANCES_KEY: THREE_RECORDS})
    records = svc._multi_instance_records("Pay-Statement", data)
    assert records == THREE_RECORDS


def test_reporting_rows_keep_the_same_column_names_a_single_record_class_gets():
    """The point of fanning out rather than flattening: an existing dashboard
    query for `inference_result.checknumber` keeps working."""
    svc = _reporting_service([PAY_CLASS])
    flat = svc._flatten_json_data(
        {**_extraction_json(THREE_RECORDS[0]), "inference_result": THREE_RECORDS[0]}
    )
    assert flat["inference_result.CheckNumber"] == "77310468"
    # And the collapse this avoids: the whole list as one JSON string column.
    collapsed = svc._flatten_json_data(_extraction_json({INSTANCES_KEY: THREE_RECORDS}))
    assert "inference_result.instances" in collapsed
    assert "inference_result.CheckNumber" not in collapsed


def test_reporting_leaves_an_unflagged_class_on_the_single_row_path():
    """Config is the source of truth, not the shape of the output: a Designate
    mode class that happens to name its own array `instances` must keep its
    existing reporting shape."""
    svc = _reporting_service([SINGLE_CLASS])
    data = _extraction_json({INSTANCES_KEY: THREE_RECORDS})
    assert svc._multi_instance_records("W2", data) is None


def test_reporting_ignores_an_unknown_class_and_an_empty_or_absent_wrapper():
    svc = _reporting_service([PAY_CLASS])
    assert svc._multi_instance_records("NotAClass", _extraction_json({})) is None
    assert svc._multi_instance_records(None, _extraction_json({})) is None
    assert svc._multi_instance_records("Pay-Statement", _extraction_json({})) is None
    assert (
        svc._multi_instance_records(
            "Pay-Statement", _extraction_json({INSTANCES_KEY: []})
        )
        is None
    )
    assert (
        svc._multi_instance_records(
            "Pay-Statement", _extraction_json({"CheckNumber": "1"})
        )
        is None
    )


# --------------------------------------------------------------------------
# Analytics agent: it must know a row is one DOCUMENT, not one section
# --------------------------------------------------------------------------


def test_analytics_schema_tells_the_agent_about_record_index():
    """Without this the agent writes COUNT(*) and per-section joins that silently
    multiply by the instance count."""
    from idp_common.agents.analytics.schema_provider import (
        get_dynamic_document_sections_description,
    )
    from idp_common.config.models import IDPConfig

    description = get_dynamic_document_sections_description(
        IDPConfig(classes=[PAY_CLASS, SINGLE_CLASS])
    )
    assert "record_index" in description
    assert "multi-instance" in description.lower()
    assert "NOT unique" in description


def test_analytics_schema_says_nothing_extra_for_an_unflagged_class():
    from idp_common.agents.analytics.schema_provider import (
        get_dynamic_document_sections_description,
    )
    from idp_common.config.models import IDPConfig

    description = get_dynamic_document_sections_description(
        IDPConfig(classes=[SINGLE_CLASS])
    )
    assert "record_index" not in description


# --------------------------------------------------------------------------
# Z3 rule engine: [i] subscripts, and a loud miss instead of a silent one
# --------------------------------------------------------------------------


Z3_DATA = {
    "documents": {
        "pay_statement": {
            "inference_result": {
                INSTANCES_KEY: [
                    {"NetPay": "4104.59", "lines": [{"amt": "1"}, {"amt": "2"}]},
                    {"NetPay": "4657.95"},
                ]
            }
        }
    }
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("documents.pay_statement.inference_result.instances[0].NetPay", "4104.59"),
        ("documents.pay_statement.inference_result.instances[1].NetPay", "4657.95"),
        ("documents.pay_statement.inference_result.instances[-1].NetPay", "4657.95"),
        ("documents.pay_statement.inference_result.instances[0].lines[1].amt", "2"),
        # Out of range is a miss, not an error — same contract as a missing key,
        # so an optional parameter still behaves.
        ("documents.pay_statement.inference_result.instances[9].NetPay", None),
        # No subscript support before this change; also the case that now logs.
        ("documents.pay_statement.inference_result.NetPay", None),
    ],
)
def test_z3_path_subscripts(path, expected):
    from idp_common.rule_validation.z3.data_extractor import DataExtractor

    assert DataExtractor()._extract_path(Z3_DATA, path) == expected


def test_z3_names_the_wrapper_when_a_rule_would_silently_stop_firing(caplog):
    """A miss returns None and None means "optional parameter absent", so a rule
    whose path has become wrong goes quiet rather than failing. This is the one
    case we can confidently explain."""
    import logging

    from idp_common.rule_validation.z3.data_extractor import DataExtractor

    with caplog.at_level(logging.WARNING):
        DataExtractor()._extract_path(
            Z3_DATA, "documents.pay_statement.inference_result.NetPay"
        )
    assert "will NOT fire" in caplog.text
    assert "instances[0].NetPay" in caplog.text


def test_z3_plain_dot_paths_are_unaffected():
    from idp_common.rule_validation.z3.data_extractor import DataExtractor

    data = {"a": {"b": {"c": 7}}}
    ex = DataExtractor()
    assert ex._extract_path(data, "a.b.c") == 7
    assert ex._extract_path(data, "a.b.missing") is None
    assert ex._extract_path(data, "a.b") == {"c": 7}


def test_z3_rejects_a_subscript_with_no_property_name():
    """`[0]` is not a legal dot-notation segment, so it is a malformed path — and
    matching it with an empty key resolved it to a silent miss, indistinguishable
    from an absent optional parameter."""
    from idp_common.rule_validation.z3.data_extractor import DataExtractor
    from idp_common.rule_validation.z3.exceptions import ExtractionError

    for path in ("documents.[0].x", "[0]", "a.[-1]"):
        with pytest.raises(ExtractionError, match="subscript"):
            DataExtractor()._extract_path(Z3_DATA, path)


@pytest.mark.parametrize("key", ["Amount[USD]", "a[abc]", "a]["])
def test_z3_treats_a_bracketed_DATA_KEY_as_a_key_not_a_bad_subscript(key):
    """A legitimate key containing brackets (plausible in an ERP payload) must
    still resolve by plain lookup and still just MISS, as it did before subscripts
    existed. Raising would turn "this rule was never firing" into "this document
    fails", which is worse than the latent bug."""
    from idp_common.rule_validation.z3.data_extractor import DataExtractor

    data = {"sap": {key: "7"}}
    ex = DataExtractor()
    assert ex._extract_path(data, f"sap.{key}") == "7"
    assert ex._extract_path({"sap": {}}, f"sap.{key}") is None


# --------------------------------------------------------------------------
# Confidence-curve store: one consistent key shape
# --------------------------------------------------------------------------


def _leaf(c):
    return {"confidence": c, "confidence_threshold": 0.8}


def test_curve_keys_do_not_depend_on_how_many_instances_a_section_had():
    """The landmine: `prefix if len(node) == 1 else prefix[i]` keyed on list
    LENGTH, so a ONE-instance section produced `instances.NetPay` and a
    TWO-instance section `instances[0].NetPay` — keys that cannot join."""
    from idp_common.evaluation.curve_store import _flatten_confidences

    one = _flatten_confidences([{INSTANCES_KEY: [{"NetPay": _leaf(0.9)}]}])
    two = _flatten_confidences(
        [{INSTANCES_KEY: [{"NetPay": _leaf(0.9)}, {"NetPay": _leaf(0.8)}]}]
    )
    assert set(one) == {"instances[0].NetPay"}
    assert set(two) == {"instances[0].NetPay", "instances[1].NetPay"}
    assert set(one).issubset(set(two))


def test_curve_keys_are_consistent_for_a_one_row_table_too():
    """Same bug, pre-existing and unrelated to multi-instance: a one-row table
    keyed as `Transactions.date` while a two-row table keyed
    `Transactions[0].date`."""
    from idp_common.evaluation.curve_store import _flatten_confidences

    one = _flatten_confidences([{"Transactions": [{"date": _leaf(0.9)}]}])
    assert set(one) == {"Transactions[0].date"}


def test_the_outer_explainability_wrapper_still_adds_no_path_level():
    """explainability_info arrives wrapped in a single-element list; that one must
    NOT contribute an index."""
    from idp_common.evaluation.curve_store import _flatten_confidences

    assert set(_flatten_confidences([{"NetPay": _leaf(0.9)}])) == {"NetPay"}


def test_curve_values_and_confidences_use_matching_paths():
    from idp_common.evaluation.curve_store import (
        _flatten_confidences,
        _flatten_values,
    )

    expl = [{INSTANCES_KEY: [{"NetPay": _leaf(0.9)}, {"NetPay": _leaf(0.8)}]}]
    values = [{INSTANCES_KEY: [{"NetPay": "1.00"}, {"NetPay": "2.00"}]}]
    assert set(_flatten_confidences(expl)) == set(_flatten_values(values))


# --------------------------------------------------------------------------
# Evaluation: per-attribute report granularity
# --------------------------------------------------------------------------


def test_eval_rows_of_a_wrapped_class_group_under_the_instances_attribute():
    """They must group under `instances`, NOT under the item field name.

    Measured live: stepping past the synthesized root made this return
    `CheckNumber`, which matches no attribute at all — the attribute list is built
    from the class SCHEMA and a wrapped class has exactly ONE property. All 24 of
    Stickler's field_comparisons rows were dropped from
    `field_comparison_details`, emptying the report's per-field drilldown and the
    UI's mismatch highlighting (which joins on `expected_key`). Section metrics
    stayed correct, so accuracy still read 1.000 with an empty drilldown — the loss
    was invisible in the numbers.

    The granularity loss (one attribute instead of N) is real and documented; the
    fix for it is in how the ATTRIBUTE LIST is built, not in how rows are keyed.
    """
    from idp_common.evaluation.contract import row_root_attribute

    assert row_root_attribute({"expected_key": "instances[0].CheckNumber"}) == (
        "instances"
    )
    assert row_root_attribute({"expected_key": "instances[2].Earnings[1].Amount"}) == (
        "instances"
    )


def test_eval_row_grouping_is_unchanged_for_an_ordinary_list_attribute():
    from idp_common.evaluation.contract import row_root_attribute

    assert row_root_attribute({"expected_key": "Transactions[3].Amount"}) == (
        "Transactions"
    )
    assert row_root_attribute({"expected_key": "Address.city"}) == "Address"
    assert row_root_attribute({"expected_key": "CheckNumber"}) == "CheckNumber"


def test_stickler_config_is_built_from_the_WRAPPED_schema():
    """Built from the flat schema, the prediction's only key is `instances`, zero
    declared fields match, and the section silently scores 0.0."""
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    configs = SticklerConfigMapper.build_all_stickler_configs(
        {"classes": [PAY_CLASS, SINGLE_CLASS]}
    )
    pay_schema = configs["pay-statement"]["schema"]
    assert INSTANCES_KEY in pay_schema["properties"]
    assert set(pay_schema["properties"][INSTANCES_KEY]["items"]["properties"]) == {
        "CheckNumber",
        "NetPay",
    }
    # The unflagged class is untouched.
    assert set(configs["w2"]["schema"]["properties"]) == {"EmployerName"}


# Public-SDK coverage lives in lib/idp_sdk/tests/unit/test_multi_instance_sdk.py —
# idp_common tests are banned from importing idp_sdk._core (TID251).
