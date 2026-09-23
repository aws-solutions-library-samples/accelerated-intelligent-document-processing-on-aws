# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Evaluation operations (mocked).

The report/metrics/baseline-list tests build the real result object from a
stubbed processor response, because the defect they cover is in the
construction: ``get_report``, ``get_metrics`` and ``list_baselines`` passed
keywords their result dataclasses did not declare, so each raised ``TypeError``
on every input. Every one of them is wrapped in
``except Exception: raise IDPProcessingError(...)``, so the caller saw a
plausible processing error rather than a signature bug, and a test asserting on
the mock's call arguments would have passed throughout.

The processor tests below pin the S3 layout the operations read, which is the
other half: the report path is
``<document key>/evaluation/results.json``, written by
``idp_common.evaluation.contract.evaluation_results_key``.
"""

import io
import json
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError
from idp_sdk.models import (
    BaselineInfo,
    EvaluationBaselineListResult,
    EvaluationMetrics,
    EvaluationReport,
    FieldComparison,
    UseAsBaselineResult,
)

#: One document's evaluation artifact, in the shape
#: ``idp_common.evaluation.models.DocumentEvaluationResult.to_dict()`` writes:
#: a document-level ``overall_metrics`` plus one ``section_results`` entry per
#: section, each with its own ``metrics`` and per-attribute comparisons.
RESULTS_JSON = {
    "document_id": "invoice-001.pdf",
    "overall_metrics": {
        "accuracy": 0.9,
        "precision": 0.85,
        "recall": 0.8,
        "f1_score": 0.82,
        "false_alarm_rate": 0.05,
    },
    "execution_time": 3.5,
    "output_uri": "s3://output-bucket/invoice-001.pdf/evaluation/results.json",
    "section_results": [
        {
            "section_id": "1",
            "document_class": "invoice",
            "metrics": {
                "accuracy": 0.75,
                "precision": 0.7,
                "recall": 0.6,
                "f1_score": 0.65,
            },
            "attributes": [
                {
                    "name": "invoice_total",
                    "expected": "4821.50",
                    "actual": "4821.50",
                    "matched": True,
                    "score": 1.0,
                    "reason": "exact match",
                    "evaluation_method": "EXACT",
                },
                {
                    # Everything optional absent, and a non-scalar value: the
                    # comparison must still build.
                    "name": "line_items",
                    "expected": [{"sku": "A"}],
                    "actual": None,
                    "matched": False,
                },
            ],
        },
        {
            "section_id": "2",
            "document_class": "receipt",
            "metrics": {"accuracy": 0.5},
            "attributes": [],
        },
    ],
}


def _body(payload):
    """An S3 get_object response whose Body reads as JSON, like botocore's."""
    return {"Body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


def _no_such_key():
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The key does not exist."}},
        "GetObject",
    )


@pytest.mark.unit
class TestUseAsBaselineOperation:
    """Test the EvaluationOperation.use_as_baseline delegation."""

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_use_as_baseline_success(self, mock_processor):
        mock_instance = Mock()
        mock_instance.use_as_baseline.return_value = {
            "document_id": "loan-123/package.pdf",
            "files_copied": 15,
            "evaluation_status": "BASELINE_AVAILABLE",
            "timestamp": "2024-01-01T00:00:00",
        }
        mock_processor.return_value = mock_instance

        client = IDPClient(stack_name="test-stack")
        result = client.evaluation.use_as_baseline(document_id="loan-123/package.pdf")

        assert isinstance(result, UseAsBaselineResult)
        assert result.document_id == "loan-123/package.pdf"
        assert result.files_copied == 15
        assert result.evaluation_status == "BASELINE_AVAILABLE"
        mock_instance.use_as_baseline.assert_called_once_with(
            document_id="loan-123/package.pdf"
        )

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_use_as_baseline_not_found(self, mock_processor):
        mock_instance = Mock()
        mock_instance.use_as_baseline.side_effect = FileNotFoundError(
            "No output objects found"
        )
        mock_processor.return_value = mock_instance

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPResourceNotFoundError):
            client.evaluation.use_as_baseline(document_id="missing.pdf")

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_use_as_baseline_copy_error(self, mock_processor):
        mock_instance = Mock()
        mock_instance.use_as_baseline.side_effect = RuntimeError("copy boom")
        mock_processor.return_value = mock_instance

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPProcessingError):
            client.evaluation.use_as_baseline(document_id="doc.pdf")


@pytest.mark.unit
class TestUseAsBaselineProcessor:
    """Test EvaluationProcessor.use_as_baseline copy + status logic."""

    def _make_processor(self):
        """Build a processor without touching AWS (bypass __init__)."""
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        proc = EvaluationProcessor.__new__(EvaluationProcessor)
        proc.s3 = Mock()
        proc.dynamodb = Mock()
        proc.resources = {
            "OutputBucket": "output-bucket",
            "EvaluationBaselineBucket": "baseline-bucket",
            "DocumentsTable": "tracking-table",
        }
        return proc

    def test_copies_all_objects_and_sets_status(self):
        proc = self._make_processor()

        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "doc.pdf/pages/1.json"}]},
            {"Contents": [{"Key": "doc.pdf/sections/1/result.json"}]},
        ]
        proc.s3.get_paginator.return_value = paginator
        table = Mock()
        proc.dynamodb.Table.return_value = table

        result = proc.use_as_baseline("doc.pdf")

        assert result["files_copied"] == 2
        assert result["evaluation_status"] == "BASELINE_AVAILABLE"
        # Prefix must be document-id + "/" so sibling prefixes don't match
        paginator.paginate.assert_called_once_with(
            Bucket="output-bucket", Prefix="doc.pdf/"
        )
        assert proc.s3.copy_object.call_count == 2
        # Status transitions: BASELINE_COPYING then BASELINE_AVAILABLE
        statuses = [
            c.kwargs["ExpressionAttributeValues"][":es"]
            for c in table.update_item.call_args_list
        ]
        assert statuses == ["BASELINE_COPYING", "BASELINE_AVAILABLE"]

    def test_raises_when_no_output(self):
        proc = self._make_processor()
        paginator = Mock()
        paginator.paginate.return_value = [{}]  # no Contents
        proc.s3.get_paginator.return_value = paginator

        with pytest.raises(FileNotFoundError):
            proc.use_as_baseline("missing.pdf")
        proc.s3.copy_object.assert_not_called()

    def test_sets_error_status_on_copy_failure(self):
        proc = self._make_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "doc.pdf/pages/1.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.copy_object.side_effect = RuntimeError("copy failed")
        table = Mock()
        proc.dynamodb.Table.return_value = table

        with pytest.raises(RuntimeError):
            proc.use_as_baseline("doc.pdf")

        statuses = [
            c.kwargs["ExpressionAttributeValues"][":es"]
            for c in table.update_item.call_args_list
        ]
        assert statuses == ["BASELINE_COPYING", "BASELINE_ERROR"]


def _stub_processor(**overrides):
    """A stubbed EvaluationProcessor instance for the operation-layer tests."""
    instance = Mock()
    for name, value in overrides.items():
        if isinstance(value, Exception):
            getattr(instance, name).side_effect = value
        else:
            getattr(instance, name).return_value = value
    return instance


def _bare_processor():
    """An EvaluationProcessor with mocked clients and no AWS calls at init."""
    from idp_sdk._core.evaluation_processor import EvaluationProcessor

    proc = EvaluationProcessor.__new__(EvaluationProcessor)
    proc.s3 = Mock()
    proc.dynamodb = Mock()
    proc.resources = {
        "OutputBucket": "output-bucket",
        "EvaluationBaselineBucket": "baseline-bucket",
        "DocumentsTable": "tracking-table",
    }
    return proc


@pytest.mark.unit
class TestEvaluationResultsContract:
    """The reader's key must stay the producer's key.

    Asserted against ``idp_common.evaluation.contract`` rather than against a
    literal. The producer (``idp_common.evaluation.service``) and the aggregation
    Lambda import that helper, so pinning the string here instead would let the
    template be changed in one place and silently return this reader to looking
    for an object nothing writes — reinstating the defect these tests cover.
    """

    def test_the_report_key_is_the_producers_key(self):
        from idp_common.evaluation.contract import evaluation_results_key
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        for document_id in ("invoice-001.pdf", "batch-1/nested/doc.pdf", "no-suffix"):
            assert EvaluationProcessor._evaluation_results_key(
                document_id
            ) == evaluation_results_key(document_id)

    def test_the_metrics_suffix_filter_matches_a_real_key(self):
        """Both ways this derivation fails, and both fail silently.

        ``get_metrics`` sifts a bucket listing by suffix rather than by a known
        key. If the document id ever moved out of the front of the template, no
        real key would end with the derived string and the scan would report zero
        evaluations with nothing raising. If the suffix were empty, every object
        in the bucket would match and be fetched.
        """
        from idp_common.evaluation.contract import evaluation_results_key
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        suffix = EvaluationProcessor._evaluation_results_suffix()

        assert suffix, (
            "the derived results suffix is empty, so get_metrics' "
            "`key.endswith(suffix)` filter matches every object in the output "
            "bucket and would fetch all of them."
        )
        assert "/" in suffix, (
            f"the derived results suffix {suffix!r} contains no path separator, so "
            "it is too unspecific to identify an evaluation artifact: get_metrics "
            "would fetch and parse every object in the output bucket whose key "
            "happens to end that way. Non-emptiness is a floor, not a specificity "
            "check, which is why this is asserted separately."
        )
        assert evaluation_results_key("batch-1/doc.pdf").endswith(suffix), (
            f"a real results key does not end with the derived suffix {suffix!r}. "
            "The document id has moved out of the front of "
            "EVALUATION_RESULTS_KEY_TEMPLATE, so get_metrics would silently match "
            "nothing. Derive the filter differently rather than adjusting this test."
        )


@pytest.mark.unit
class TestGetReportProcessor:
    """EvaluationProcessor.get_report reads the artifact the pipeline writes."""

    def test_reads_the_evaluation_results_key(self):
        from idp_common.evaluation.contract import evaluation_results_key

        proc = _bare_processor()
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        proc.get_report("invoice-001.pdf", section_id=1)

        proc.s3.get_object.assert_called_once_with(
            Bucket="output-bucket",
            Key=evaluation_results_key("invoice-001.pdf"),
        )

    def test_selects_the_requested_section(self):
        proc = _bare_processor()
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        result = proc.get_report("invoice-001.pdf", section_id=2)

        # Section ids are strings in the artifact and ints on this API.
        assert result["section_id"] == 2
        assert result["document_class"] == "receipt"
        assert result["accuracy"] == 0.5
        # Document-level metrics come through unchanged for context.
        assert result["overall_metrics"]["f1_score"] == 0.82

    def test_renames_attributes_into_the_sdk_vocabulary(self):
        proc = _bare_processor()
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        comparisons = proc.get_report("invoice-001.pdf")["field_comparisons"]

        assert comparisons[0] == {
            "attribute": "invoice_total",
            "expected": "4821.50",
            "actual": "4821.50",
            "matched": True,
            "score": 1.0,
            "method": "EXACT",
            "reason": "exact match",
        }
        assert comparisons[1]["attribute"] == "line_items"
        assert comparisons[1]["score"] is None
        assert comparisons[1]["method"] is None

    def test_missing_results_raise_file_not_found(self):
        proc = _bare_processor()
        proc.s3.get_object.side_effect = _no_such_key()

        with pytest.raises(FileNotFoundError, match="No evaluation results"):
            proc.get_report("never-evaluated.pdf")

    def test_an_unknown_section_raises_file_not_found_naming_what_is_there(self):
        proc = _bare_processor()
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        with pytest.raises(FileNotFoundError, match=r"no section 9.*\['1', '2'\]"):
            proc.get_report("invoice-001.pdf", section_id=9)

    def test_a_non_notfound_client_error_propagates(self):
        proc = _bare_processor()
        proc.s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetObject"
        )

        with pytest.raises(ClientError):
            proc.get_report("invoice-001.pdf")


@pytest.mark.unit
class TestGetMetricsProcessor:
    def test_aggregates_over_the_results_json_objects_only(self):
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "a.pdf/evaluation/results.json", "LastModified": Mock()},
                    # Not an evaluation artifact: must be skipped, not fetched.
                    {"Key": "a.pdf/sections/1/result.json", "LastModified": Mock()},
                    {"Key": "b.pdf/evaluation/results.json", "LastModified": Mock()},
                ]
            }
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.side_effect = [
            _body(RESULTS_JSON),
            _body(
                {
                    "document_id": "b.pdf",
                    "overall_metrics": {
                        "accuracy": 0.7,
                        "precision": 0.65,
                        "recall": 0.6,
                        "f1_score": 0.62,
                    },
                    "section_results": [
                        {
                            "section_id": "1",
                            "document_class": "invoice",
                            "metrics": {"accuracy": 0.7},
                            "attributes": [],
                        }
                    ],
                }
            ),
        ]

        result = proc.get_metrics()

        assert proc.s3.get_object.call_count == 2
        assert result["total_documents"] == 2
        assert result["avg_accuracy"] == pytest.approx(0.8)
        assert result["avg_f1_score"] == pytest.approx(0.72)
        # Classes aggregate SECTIONS: invoice appears in both documents.
        assert result["by_document_class"]["invoice"]["count"] == 2
        assert result["by_document_class"]["invoice"]["avg_accuracy"] == pytest.approx(
            0.725
        )
        assert result["by_document_class"]["receipt"]["count"] == 1
        # The running accumulator is an implementation detail and must not leak.
        assert "_scores" not in result["by_document_class"]["invoice"]

    def test_the_class_breakdown_carries_all_four_scores(self):
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a.pdf/evaluation/results.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        by_class = proc.get_metrics()["by_document_class"]

        invoice = by_class["invoice"]
        assert invoice["avg_precision"] == pytest.approx(0.7)
        assert invoice["avg_recall"] == pytest.approx(0.6)
        assert invoice["avg_f1_score"] == pytest.approx(0.65)
        # Section 2 reports only accuracy, so the other three have no data there
        # and must be None rather than averaged against a count that includes it.
        receipt = by_class["receipt"]
        assert receipt["avg_accuracy"] == pytest.approx(0.5)
        assert receipt["avg_precision"] is None

    def test_no_evaluations_yields_none_rather_than_a_zero_score(self):
        """Zero documents is not "accuracy 0.0" — that reads as a measurement."""
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": []}]
        proc.s3.get_paginator.return_value = paginator

        result = proc.get_metrics()

        assert result["total_documents"] == 0
        assert result["avg_accuracy"] is None
        assert result["by_document_class"] == {}

    def test_an_unscored_section_does_not_dilute_its_class_average(self):
        """A section the pipeline excluded carries no scores, only flags.

        Counting it in the denominator would drag the class average toward zero
        with nothing saying so, which is the shape of the defects this module
        covers.
        """
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a.pdf/evaluation/results.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.return_value = _body(
            {
                "document_id": "a.pdf",
                "overall_metrics": {"accuracy": 0.8},
                "section_results": [
                    {
                        "section_id": "1",
                        "document_class": "invoice",
                        "metrics": {"accuracy": 0.8},
                        "attributes": [],
                    },
                    {
                        "section_id": "2",
                        "document_class": "invoice",
                        # What the evaluation service writes for a skipped section.
                        "metrics": {
                            "weighted_overall_score": None,
                            "evaluation_skipped": True,
                        },
                        "attributes": [],
                    },
                ],
            }
        )

        invoice = proc.get_metrics()["by_document_class"]["invoice"]

        assert invoice["count"] == 2
        assert invoice["avg_accuracy"] == pytest.approx(0.8), (
            "the skipped section was counted in the denominator, halving the "
            "reported accuracy"
        )

    def test_a_boolean_flag_is_not_averaged_as_a_score(self):
        """`bool` is an `int`, so a flag where a score belongs would read as 1.0."""
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a.pdf/evaluation/results.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.return_value = _body(
            {
                "document_id": "a.pdf",
                "overall_metrics": {"accuracy": True},
                "section_results": [],
            }
        )

        assert proc.get_metrics()["avg_accuracy"] is None

    def test_a_document_class_filter_drops_documents_without_that_class(self):
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a.pdf/evaluation/results.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        kept = proc.get_metrics(document_class="receipt")
        assert kept["total_documents"] == 1
        assert list(kept["by_document_class"]) == ["receipt"]

        proc.s3.get_object.return_value = _body(RESULTS_JSON)
        dropped = proc.get_metrics(document_class="bank-statement")
        assert dropped["total_documents"] == 0

    def test_a_class_filter_suppresses_the_whole_document_averages(self):
        """The top-level averages cannot answer a class-scoped question.

        The document in RESULTS_JSON scores 0.9 overall but its receipt section
        scores 0.5. Reporting 0.9 for `document_class="receipt"` would be a real
        number measuring something other than what the caller asked for, so it is
        `None` and the answer lives in the breakdown.
        """
        proc = _bare_processor()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a.pdf/evaluation/results.json"}]}
        ]
        proc.s3.get_paginator.return_value = paginator
        proc.s3.get_object.return_value = _body(RESULTS_JSON)

        unfiltered = proc.get_metrics()
        assert unfiltered["avg_accuracy"] == pytest.approx(0.9)

        proc.s3.get_object.return_value = _body(RESULTS_JSON)
        filtered = proc.get_metrics(document_class="receipt")

        assert filtered["total_documents"] == 1
        for name in ("accuracy", "precision", "recall", "f1_score"):
            assert filtered[f"avg_{name}"] is None, f"avg_{name} is not class-scoped"
        assert filtered["by_document_class"]["receipt"][
            "avg_accuracy"
        ] == pytest.approx(0.5)


@pytest.mark.unit
class TestListBaselinesProcessor:
    def test_each_baseline_carries_its_s3_location(self):
        proc = _bare_processor()
        proc.s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "invoice-001.pdf/"}],
        }

        result = proc.list_baselines()

        assert result["baselines"] == [
            {
                "document_id": "invoice-001.pdf",
                "s3_location": "s3://baseline-bucket/invoice-001.pdf/",
            }
        ]
        assert result["count"] == 1


@pytest.mark.unit
class TestEvaluationOperationResults:
    """The operation layer builds real result objects, not mocks."""

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_report_builds_an_evaluation_report(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            get_report={
                "document_id": "invoice-001.pdf",
                "section_id": 1,
                "document_class": "invoice",
                "accuracy": 0.75,
                "precision": 0.7,
                "recall": 0.6,
                "f1_score": 0.65,
                "field_comparisons": [
                    {
                        "attribute": "invoice_total",
                        "expected": "4821.50",
                        "actual": "4821.50",
                        "matched": True,
                        "score": 1.0,
                        "method": "EXACT",
                        "reason": "exact match",
                    }
                ],
                "overall_metrics": {"accuracy": 0.9},
            }
        )

        client = IDPClient(stack_name="test-stack")
        report = client.evaluation.get_report(document_id="invoice-001.pdf")

        assert isinstance(report, EvaluationReport)
        assert report.document_id == "invoice-001.pdf"
        assert report.section_id == 1
        assert report.document_class == "invoice"
        assert (report.accuracy, report.precision) == (0.75, 0.7)
        assert report.overall_metrics == {"accuracy": 0.9}

        comparison = report.field_comparisons[0]
        assert isinstance(comparison, FieldComparison)
        assert comparison.attribute == "invoice_total"
        assert comparison.matched is True
        assert comparison.method == "EXACT"

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_report_tolerates_a_report_with_no_comparisons(self, mock_processor):
        """Every optional field absent — the minimum the processor can return."""
        mock_processor.return_value = _stub_processor(
            get_report={"document_id": "d.pdf", "section_id": 3}
        )

        client = IDPClient(stack_name="test-stack")
        report = client.evaluation.get_report(document_id="d.pdf", section_id=3)

        assert report.field_comparisons == []
        assert report.accuracy is None
        assert report.overall_metrics == {}

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_report_maps_missing_results_to_not_found(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            get_report=FileNotFoundError("No evaluation results")
        )

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPResourceNotFoundError):
            client.evaluation.get_report(document_id="d.pdf")

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_metrics_builds_evaluation_metrics(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            get_metrics={
                "total_documents": 2,
                "avg_accuracy": 0.8,
                "avg_precision": 0.75,
                "avg_recall": 0.7,
                "avg_f1_score": 0.72,
                "by_document_class": {"invoice": {"count": 2, "avg_accuracy": 0.725}},
            }
        )

        client = IDPClient(stack_name="test-stack")
        metrics = client.evaluation.get_metrics(
            start_date="2024-01-01", end_date="2024-01-31"
        )

        assert isinstance(metrics, EvaluationMetrics)
        assert metrics.total_documents == 2
        assert metrics.avg_accuracy == 0.8
        assert metrics.avg_f1_score == 0.72
        assert metrics.by_document_class["invoice"]["count"] == 2
        # The filters are echoed back so a stored result records its window.
        assert (metrics.start_date, metrics.end_date) == (
            "2024-01-01",
            "2024-01-31",
        )
        assert metrics.document_class is None

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_metrics_echoes_the_class_filter_that_nulls_the_averages(
        self, mock_processor
    ):
        """`document_class` on the result is what explains the `None` averages."""
        mock_processor.return_value = _stub_processor(
            get_metrics={
                "total_documents": 1,
                "avg_accuracy": None,
                "avg_precision": None,
                "avg_recall": None,
                "avg_f1_score": None,
                "by_document_class": {"invoice": {"count": 1, "avg_accuracy": 0.9}},
            }
        )

        client = IDPClient(stack_name="test-stack")
        metrics = client.evaluation.get_metrics(document_class="invoice")

        assert metrics.document_class == "invoice"
        assert metrics.avg_accuracy is None
        assert metrics.by_document_class["invoice"]["avg_accuracy"] == 0.9

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_get_metrics_failure_becomes_a_processing_error(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            get_metrics=RuntimeError("scan failed")
        )

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPProcessingError, match="scan failed"):
            client.evaluation.get_metrics()

    @patch("idp_sdk._core.evaluation_processor.EvaluationProcessor")
    def test_list_baselines_builds_typed_baseline_info(self, mock_processor):
        """`baselines` is declared `List[BaselineInfo]`, so it must hold them.

        It used to be handed the processor's raw dicts, which a dataclass accepts
        without complaint — so `baseline.document_id` would have raised
        `TypeError` on a subscriptable-only value had the constructor got that far.
        """
        mock_processor.return_value = _stub_processor(
            list_baselines={
                "baselines": [
                    {
                        "document_id": "invoice-001.pdf",
                        "s3_location": "s3://baseline-bucket/invoice-001.pdf/",
                    }
                ],
                "count": 1,
                # Bandit flags `next_token` on sight. B105/B106 match the
                # *identifier* — `RE_CANDIDATES` tests `_token$` against the dict
                # key or keyword name — and never inspect the value, so no choice
                # of literal here would quiet them. The name is the SDK's real
                # pagination field: `list_baselines` base64-encodes its
                # `LastEvaluatedKey` into it and `EvaluationBaselineListResult`
                # declares it, so renaming it would make this stub a shape the
                # operation never returns. Both flagged lines carry a pragma.
                "next_token": "dG9rZW4=",  # nosec B105 - cursor, base64 "token"
            }
        )

        client = IDPClient(stack_name="test-stack")
        result = client.evaluation.list_baselines(limit=50)

        assert isinstance(result, EvaluationBaselineListResult)
        assert result.count == 1
        assert result.next_token == "dG9rZW4="  # nosec B105 - stub cursor read back
        assert isinstance(result.baselines[0], BaselineInfo)
        assert result.baselines[0].document_id == "invoice-001.pdf"
        assert result.baselines[0].created_date is None
