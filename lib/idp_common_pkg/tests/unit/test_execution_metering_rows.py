# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-execution metering rows (#1951).

``subtract_metering`` recovers an unsuccessful run's own usage from the tracking
row's cumulative total, and ``SaveReportingData.save_metering_data`` writes each
execution's usage once, under that execution, with its terminal status.
"""

import copy
from unittest.mock import patch

import pytest

from idp_common.models import Document
from idp_common.reporting.save_reporting_data import SaveReportingData
from idp_common.utils import cumulative_metering, subtract_metering

ARN = "arn:aws:states:us-east-1:123456789012:execution:sm:run-one"


@pytest.mark.unit
class TestSubtractMetering:
    def test_is_the_inverse_of_cumulative_metering(self):
        doc = Document(id="d")
        doc.metering_baseline = {
            "OCR/textract/a": {"pages": 3},
            "Classification/bedrock/m": {"inputTokens": 900, "outputTokens": 40},
        }
        doc.metering = {
            "Classification/bedrock/m": {"inputTokens": 50, "outputTokens": 5},
            "OCR/lambda/duration": {"gb_seconds": 0.5},
        }
        own = copy.deepcopy(doc.metering)

        assert subtract_metering(cumulative_metering(doc), doc.metering_baseline) == own

    def test_drops_units_and_services_that_did_not_grow(self):
        total = {"a/x": {"pages": 3, "tokens": 10}, "b/y": {"pages": 1}}
        baseline = {"a/x": {"pages": 3, "tokens": 4}, "b/y": {"pages": 1}}

        assert subtract_metering(total, baseline) == {"a/x": {"tokens": 6}}

    def test_a_total_below_the_baseline_reports_nothing_for_that_unit(self):
        total = {"a/x": {"pages": 2, "tokens": 10}}
        baseline = {"a/x": {"pages": 3}}

        assert subtract_metering(total, baseline) == {"a/x": {"tokens": 10}}

    def test_reads_numeric_strings_and_skips_what_is_not_a_number(self):
        total = {"a/x": {"pages": "5", "label": "n/a"}}

        assert subtract_metering(total, {"a/x": {"pages": 2}}) == {"a/x": {"pages": 3}}

    def test_does_not_modify_its_inputs(self):
        total = {"a/x": {"pages": 5}}
        baseline = {"a/x": {"pages": 2}}

        subtract_metering(total, baseline)

        assert total == {"a/x": {"pages": 5}}
        assert baseline == {"a/x": {"pages": 2}}


def _write(doc, **kwargs):
    with patch("boto3.client"):
        reporting = SaveReportingData(reporting_bucket="test-bucket")
    with (
        patch.object(reporting, "_save_records_as_parquet") as save,
        patch("idp_common.reporting.save_reporting_data.emit_document_cost_metrics"),
    ):
        reporting.save_metering_data(doc, **kwargs)
    records, key, schema = save.call_args.args
    return records, key, schema


def _doc(arn=None):
    doc = Document(id="batch/doc.pdf", input_key="batch/doc.pdf")
    doc.initial_event_time = "2026-11-18T10:00:00Z"
    doc.metering = {"OCR/textract/a": {"pages": 2}, "OCR/lambda/duration": {"s": 1}}
    doc.workflow_execution_arn = arn
    return doc


@pytest.mark.unit
class TestMeteringRowsCarryTheExecution:
    def test_every_row_names_the_execution_and_its_status(self):
        records, _, schema = _write(_doc(ARN), execution_status="FAILED")

        assert len(records) == 2
        assert {r["execution_arn"] for r in records} == {ARN}
        assert {r["execution_status"] for r in records} == {"FAILED"}
        assert "execution_arn" in schema.names
        assert "execution_status" in schema.names

    def test_the_object_is_named_after_the_execution(self):
        _, first, _ = _write(_doc(ARN), execution_status="SUCCEEDED")
        _, second, _ = _write(_doc(ARN), execution_status="SUCCEEDED")

        assert first.endswith("/batch_doc.pdf_exec_run-one_results.parquet")
        assert first == second

    def test_without_an_execution_the_columns_are_null_and_names_stay_unique(self):
        records, key, _ = _write(_doc())

        assert {r["execution_arn"] for r in records} == {None}
        assert {r["execution_status"] for r in records} == {None}
        assert "_exec_" not in key

    def test_save_passes_the_status_through(self):
        with patch("boto3.client"):
            reporting = SaveReportingData(reporting_bucket="test-bucket")
        with patch.object(reporting, "save_metering_data") as save:
            reporting.save(_doc(ARN), ["metering"], execution_status="TIMED_OUT")

        assert save.call_args.kwargs == {"execution_status": "TIMED_OUT"}
