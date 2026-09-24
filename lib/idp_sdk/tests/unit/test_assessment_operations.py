# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk.operations.assessment`` and its ``_core`` analyzer.

``AssessmentAnalyzer`` (``idp_sdk/_core/assessment_analyzer.py``) reads the
``result.json`` a processed document leaves in the output bucket and answers three
questions about it: the per-field confidence scores, the per-field bounding boxes,
and — across many documents — aggregate quality metrics.
``AssessmentOperation`` is the public namespace over it, turning the analyzer's
dictionaries into typed results and its exceptions into the SDK's own.

**What shaped these tests.** The aggregation in ``get_metrics`` is the part worth
being careful about, because a wrong aggregate is silent: it comes back as a
plausible number and gets put on a dashboard. So the fixture here is deliberately
**not** degenerate — the per-document means differ from each other, the
document-weighted mean (0.6833) differs from the field-weighted mean (0.6857),
and the per-class means differ from the overall one — and every expected value
below is computed by hand in the test's own comment. A dataset where those
coincided would pass against several different and mutually incompatible
implementations.

The analyzer talks to S3 and to CloudFormation, so it runs against ``moto`` on
top of a real (moto) stack that ``StackInfo`` discovers for itself: the output
bucket name comes out of a stack output, the result objects are really stored,
and ``LastModified`` is a real timestamp. That last detail matters for the date
filter, which is where one of the two defects pinned here lives.
"""

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk._core.assessment_analyzer import AssessmentAnalyzer
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError
from idp_sdk.models import (
    AssessmentConfidenceResult,
    AssessmentFieldConfidence,
    AssessmentGeometryResult,
)

pytestmark = pytest.mark.unit

STACK_NAME = "idp-assess-test"
OUTPUT_BUCKET = "idp-assess-test-output"
REGION = "us-east-1"

STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "DocumentQueue": {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": "idp-assess-test-queue"},
        }
    },
    "Outputs": {
        "S3InputBucketName": {"Value": "idp-assess-test-input"},
        "S3OutputBucketName": {"Value": OUTPUT_BUCKET},
    },
}


@pytest.fixture
def idp_stack(aws_credentials):
    """A live (moto) stack plus its output bucket; yields the ``IDPClient``."""
    with mock_aws():
        boto3.client("cloudformation", region_name=REGION).create_stack(
            StackName=STACK_NAME, TemplateBody=json.dumps(STACK_TEMPLATE)
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
        yield IDPClient(stack_name=STACK_NAME, region=REGION)


def _put_result(document_id, payload, section_id=1):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=OUTPUT_BUCKET,
        Key=f"{document_id}/sections/{section_id}/result.json",
        Body=json.dumps(payload).encode(),
    )


def _analyzer():
    return AssessmentAnalyzer(stack_name=STACK_NAME, region=REGION)


#: A realistic section result: ``explainability_info`` is a list whose entries map
#: attribute name to a per-attribute assessment dict.
CONFIDENCE_RESULT = {
    "document_class": {"type": "invoice"},
    "inference_result": {"total_amount": "1042.55", "vendor": "Acme Corp"},
    "explainability_info": [
        {
            "total_amount": {
                "confidence": 0.97,
                "confidence_threshold": 0.9,
                "reason": "Value is unambiguous and matches the printed total.",
                "meets_threshold": True,
                "geometry": {
                    "page": 2,
                    "bbox": [120.0, 340.0, 260.0, 362.0],
                    "bounding_box": {
                        "Left": 0.12,
                        "Top": 0.34,
                        "Width": 0.14,
                        "Height": 0.02,
                    },
                },
            },
            "vendor": {
                "confidence": 0.42,
                "confidence_threshold": 0.9,
                "reason": "Letterhead is partially obscured.",
                "meets_threshold": False,
            },
            # A grouped attribute carries neither key and must be skipped by both
            # readers rather than producing a half-populated entry.
            "line_items": {"item_count": 4},
        }
    ],
}


# ---------------------------------------------------------------------------
# AssessmentAnalyzer construction
# ---------------------------------------------------------------------------


class TestAnalyzerConstruction:
    def test_it_discovers_the_output_bucket_from_the_stack(self, idp_stack):
        """The analyzer is given a stack name, not a bucket. If the discovery or
        the output key mapping were wrong every read would go to the wrong
        bucket, so the resolved name is asserted directly."""
        analyzer = _analyzer()

        assert analyzer.resources["OutputBucket"] == OUTPUT_BUCKET
        assert analyzer.stack_name == STACK_NAME
        assert analyzer.region == REGION

    def test_a_stack_that_is_not_usable_is_refused_at_construction(
        self, aws_credentials
    ):
        """Refusing here means one clear error rather than a confusing
        ``KeyError: 'OutputBucket'`` on the first read."""
        with mock_aws():
            with pytest.raises(ValueError, match="not in a valid state"):
                AssessmentAnalyzer(stack_name="never-created", region=REGION)


# ---------------------------------------------------------------------------
# AssessmentAnalyzer.get_confidence
# ---------------------------------------------------------------------------


class TestAnalyzerGetConfidence:
    def test_each_scored_attribute_is_returned_with_all_four_facets(self, idp_stack):
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        result = _analyzer().get_confidence("batch-1/invoice.pdf")

        assert result["document_id"] == "batch-1/invoice.pdf"
        assert result["section_id"] == 1
        assert set(result["attributes"]) == {"total_amount", "vendor"}
        assert result["attributes"]["total_amount"] == {
            "confidence": 0.97,
            "confidence_threshold": 0.9,
            "reason": "Value is unambiguous and matches the printed total.",
            "meets_threshold": True,
        }
        assert result["attributes"]["vendor"]["meets_threshold"] is False

    def test_an_attribute_without_a_confidence_is_omitted_entirely(self, idp_stack):
        """``line_items`` has no confidence. Emitting it with ``confidence: None``
        would break every caller that averages or compares these values."""
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        attributes = _analyzer().get_confidence("batch-1/invoice.pdf")["attributes"]

        assert "line_items" not in attributes

    def test_the_optional_facets_default_rather_than_raising(self, idp_stack):
        """Assessment can run without thresholds configured, so a bare
        ``confidence`` must still be readable — with the absent facets reported as
        ``None`` and the reason as an empty string, which is what the SDK model
        expects."""
        _put_result(
            "bare.pdf",
            {"explainability_info": [{"amount": {"confidence": 0.5}}]},
        )

        entry = _analyzer().get_confidence("bare.pdf")["attributes"]["amount"]

        assert entry == {
            "confidence": 0.5,
            "confidence_threshold": None,
            "reason": "",
            "meets_threshold": None,
        }

    def test_the_requested_section_is_the_one_read(self, idp_stack):
        """Sections are separate objects, and reading section 1 when asked for
        section 3 would silently answer about a different document class."""
        _put_result("multi.pdf", CONFIDENCE_RESULT, section_id=1)
        _put_result(
            "multi.pdf",
            {"explainability_info": [{"policy_number": {"confidence": 0.11}}]},
            section_id=3,
        )

        result = _analyzer().get_confidence("multi.pdf", section_id=3)

        assert result["section_id"] == 3
        assert set(result["attributes"]) == {"policy_number"}

    def test_a_result_with_no_explainability_info_is_empty_not_an_error(
        self, idp_stack
    ):
        """A document extracted without assessment enabled has no scores. That is
        an empty answer, not a missing document."""
        _put_result("unassessed.pdf", {"inference_result": {"a": "1"}})

        assert _analyzer().get_confidence("unassessed.pdf")["attributes"] == {}

    def test_a_missing_result_object_raises_file_not_found(self, idp_stack):
        """``NoSuchKey`` is translated here so the operation layer above can map it
        to ``IDPResourceNotFoundError``; leaving it as a ``ClientError`` would make
        "not processed yet" indistinguishable from a permissions problem."""
        with pytest.raises(FileNotFoundError, match="section: 1"):
            _analyzer().get_confidence("never-processed.pdf")

    def test_a_non_notfound_client_error_propagates(self, idp_stack):
        """Only ``NoSuchKey`` means "not there". Anything else must keep its own
        error code so the cause is still visible."""
        from botocore.exceptions import ClientError

        analyzer = _analyzer()
        boto3.client("s3", region_name=REGION).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(ClientError) as excinfo:
            analyzer.get_confidence("invoice.pdf")
        assert excinfo.value.response["Error"]["Code"] != "NoSuchKey"


# ---------------------------------------------------------------------------
# AssessmentAnalyzer.get_geometry
# ---------------------------------------------------------------------------


class TestAnalyzerGetGeometry:
    def test_both_coordinate_systems_are_returned_for_a_located_field(self, idp_stack):
        """The two boxes are in different units — ``bbox`` in document
        coordinates, ``bounding_box`` normalised 0-1 — so mixing them up draws a
        highlight in the top-left corner of the page. Both are asserted."""
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        result = _analyzer().get_geometry("batch-1/invoice.pdf")

        assert result["document_id"] == "batch-1/invoice.pdf"
        assert set(result["attributes"]) == {"total_amount"}
        assert result["attributes"]["total_amount"] == {
            "page": 2,
            "bbox": [120.0, 340.0, 260.0, 362.0],
            "bounding_box": {
                "Left": 0.12,
                "Top": 0.34,
                "Width": 0.14,
                "Height": 0.02,
            },
        }

    def test_an_attribute_with_a_confidence_but_no_geometry_is_omitted(self, idp_stack):
        """``vendor`` is scored but not located — geometry is only produced when
        the OCR provider supplies it. Returning it with an empty box would put a
        highlight at the origin instead of none."""
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        assert (
            "vendor"
            not in _analyzer().get_geometry("batch-1/invoice.pdf")["attributes"]
        )

    def test_a_geometry_missing_its_parts_defaults_to_page_one_and_empty_boxes(
        self, idp_stack
    ):
        """Page numbers are 1-indexed, so the default for an absent page is 1 —
        a 0 default would be off by one against every page image."""
        _put_result("sparse.pdf", {"explainability_info": [{"a": {"geometry": {}}}]})

        assert _analyzer().get_geometry("sparse.pdf")["attributes"]["a"] == {
            "page": 1,
            "bbox": [],
            "bounding_box": {},
        }

    def test_a_missing_result_object_raises_file_not_found(self, idp_stack):
        with pytest.raises(FileNotFoundError, match="never-processed.pdf"):
            _analyzer().get_geometry("never-processed.pdf")


# ---------------------------------------------------------------------------
# AssessmentAnalyzer.get_metrics — the arithmetic
# ---------------------------------------------------------------------------

# Three documents whose per-document mean confidences are all different, whose
# classes split 2/1, and where the document-weighted and field-weighted means do
# not coincide. 0.80 appears on purpose: the low-confidence test is `< 0.8`, so a
# value exactly at the threshold distinguishes `<` from `<=`.
#
#   batch-1/a.pdf  invoice  [0.90, 0.60]        mean 0.75   below 0.8: 1
#   batch-1/b.pdf  invoice  [0.50, 0.70, 0.90]  mean 0.70   below 0.8: 2
#   batch-2/c.pdf  receipt  [0.40, 0.80]        mean 0.60   below 0.8: 1
#
#   documents                3
#   average_confidence       (0.75 + 0.70 + 0.60) / 3 = 2.05 / 3 = 0.68333...
#   (field-weighted would be 4.80 / 7                          = 0.68571...)
#   low_confidence_count     1 + 2 + 1 = 4
#   invoice                  count 2, (0.75 + 0.70) / 2 = 0.725
#   receipt                  count 1, 0.60
METRICS_DOCS = {
    "batch-1/a.pdf": ("invoice", [0.90, 0.60]),
    "batch-1/b.pdf": ("invoice", [0.50, 0.70, 0.90]),
    "batch-2/c.pdf": ("receipt", [0.40, 0.80]),
}
EXPECTED_AVERAGE = 2.05 / 3
EXPECTED_FIELD_WEIGHTED = 4.80 / 7


def _metrics_payload(doc_class, confidences):
    return {
        "document_class": {"type": doc_class},
        "explainability_info": [
            {
                f"field_{index}": {"confidence": value}
                for index, value in enumerate(confidences)
            }
        ],
    }


@pytest.fixture
def metrics_corpus(idp_stack):
    for document_id, (doc_class, confidences) in METRICS_DOCS.items():
        _put_result(document_id, _metrics_payload(doc_class, confidences))
    return idp_stack


class TestAnalyzerGetMetricsArithmetic:
    def test_the_headline_figures_match_the_hand_computed_values(self, metrics_corpus):
        metrics = _analyzer().get_metrics()

        assert metrics["total_documents"] == 3
        assert metrics["average_confidence"] == pytest.approx(EXPECTED_AVERAGE)
        assert metrics["low_confidence_count"] == 4

    def test_the_average_is_a_mean_of_document_means_not_of_fields(
        self, metrics_corpus
    ):
        """This is the one figure a reader is most likely to assume wrongly. Each
        document contributes equally regardless of how many fields it has, so a
        two-field document weighs as much as a two-hundred-field one. The fixture
        is built so the two readings differ, and the field-weighted value is
        asserted *absent* as well as the document-weighted one present."""
        average = _analyzer().get_metrics()["average_confidence"]

        assert average == pytest.approx(EXPECTED_AVERAGE)
        assert average != pytest.approx(EXPECTED_FIELD_WEIGHTED)

    def test_a_confidence_exactly_at_the_threshold_is_not_counted_as_low(
        self, metrics_corpus
    ):
        """The comparison is ``< 0.8``. The corpus contains exactly one 0.80 and
        four values strictly below it; a ``<=`` would report 5."""
        assert _analyzer().get_metrics()["low_confidence_count"] == 4

    def test_the_per_class_breakdown_averages_within_the_class(self, metrics_corpus):
        metrics = _analyzer().get_metrics()

        assert set(metrics["by_document_class"]) == {"invoice", "receipt"}
        assert metrics["by_document_class"]["invoice"]["count"] == 2
        assert metrics["by_document_class"]["invoice"][
            "average_confidence"
        ] == pytest.approx(0.725)
        assert metrics["by_document_class"]["receipt"]["count"] == 1
        assert metrics["by_document_class"]["receipt"][
            "average_confidence"
        ] == pytest.approx(0.60)

    def test_the_running_total_is_removed_from_the_class_breakdown(
        self, metrics_corpus
    ):
        """``total_confidence`` is an accumulator, not a metric: left in the
        result it reads as a meaningful sum of confidences and would be charted as
        one."""
        invoice = _analyzer().get_metrics()["by_document_class"]["invoice"]

        assert set(invoice) == {"count", "average_confidence"}

    def test_an_empty_corpus_averages_to_zero_rather_than_dividing_by_zero(
        self, idp_stack
    ):
        metrics = _analyzer().get_metrics()

        assert metrics["total_documents"] == 0
        assert metrics["average_confidence"] == 0.0
        assert metrics["by_document_class"] == {}

    def test_a_document_with_no_scored_fields_is_not_counted(self, metrics_corpus):
        """Counting it would add a document to the denominator with no
        contribution to the numerator, dragging the average down by a third."""
        _put_result("batch-1/unassessed.pdf", {"document_class": {"type": "invoice"}})

        metrics = _analyzer().get_metrics()

        assert metrics["total_documents"] == 3
        assert metrics["average_confidence"] == pytest.approx(EXPECTED_AVERAGE)

    def test_objects_that_are_not_result_json_are_ignored(self, metrics_corpus):
        """The output bucket holds page text, markdown and metering JSON under the
        same prefixes. Only ``result.json`` carries assessment."""
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=OUTPUT_BUCKET,
            Key="batch-1/a.pdf/pages/1/result.json.bak",
            Body=json.dumps(_metrics_payload("invoice", [0.01])).encode(),
        )
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=OUTPUT_BUCKET,
            Key="batch-1/a.pdf/metering.json",
            Body=b'{"tokens": 10}',
        )

        metrics = _analyzer().get_metrics()

        assert metrics["total_documents"] == 3
        assert metrics["average_confidence"] == pytest.approx(EXPECTED_AVERAGE)

    def test_an_unclassified_document_is_grouped_as_unknown(self, idp_stack):
        _put_result("x.pdf", {"explainability_info": [{"a": {"confidence": 0.9}}]})

        metrics = _analyzer().get_metrics()

        assert list(metrics["by_document_class"]) == ["unknown"]


class TestAnalyzerGetMetricsFilters:
    def test_a_batch_id_narrows_the_listing_to_that_prefix(self, metrics_corpus):
        """The batch filter is applied as an S3 ``Prefix``, so it is the only
        filter that reduces the number of objects fetched rather than discarding
        them afterwards."""
        metrics = _analyzer().get_metrics(batch_id="batch-1")

        assert metrics["total_documents"] == 2
        assert set(metrics["by_document_class"]) == {"invoice"}
        assert metrics["average_confidence"] == pytest.approx((0.75 + 0.70) / 2)
        assert metrics["low_confidence_count"] == 3

    def test_a_document_class_filter_excludes_other_classes_entirely(
        self, metrics_corpus
    ):
        """Applied before the confidence scan, so the excluded documents
        contribute nothing — not even to ``low_confidence_count``."""
        metrics = _analyzer().get_metrics(document_class="receipt")

        assert metrics["total_documents"] == 1
        assert set(metrics["by_document_class"]) == {"receipt"}
        assert metrics["average_confidence"] == pytest.approx(0.60)
        assert metrics["low_confidence_count"] == 1

    def test_a_class_that_matches_nothing_yields_zero(self, metrics_corpus):
        metrics = _analyzer().get_metrics(document_class="bank_statement")

        assert metrics["total_documents"] == 0
        assert metrics["low_confidence_count"] == 0

    def test_a_start_date_on_the_documents_own_day_includes_them(self, metrics_corpus):
        """The lower bound compares correctly against a bare date: an ISO
        timestamp for that day sorts after the bare date string."""
        stored_day = _stored_day()

        metrics = _analyzer().get_metrics(start_date=stored_day)

        assert metrics["total_documents"] == 3

    def test_a_start_date_in_the_future_excludes_everything(self, metrics_corpus):
        tomorrow = (_stored_datetime() + timedelta(days=1)).date().isoformat()

        assert _analyzer().get_metrics(start_date=tomorrow)["total_documents"] == 0

    def test_an_end_date_given_as_a_bare_day_excludes_that_whole_day(
        self, metrics_corpus
    ):
        """DEFECT (_core/assessment_analyzer.py:184-189). The date filter compares
        ``obj["LastModified"].isoformat()`` — e.g. ``2026-09-23T22:01:01+00:00`` —
        against the caller's string *lexically*. For the lower bound that happens
        to work; for the upper bound every timestamp on the boundary day sorts
        *after* the bare date, so the whole day is excluded.

        The consequence is that the exact call
        ``operations/assessment.py`` documents — "Monitor daily quality" with
        ``start_date="2024-01-15", end_date="2024-01-15"`` — reports zero
        documents and an average confidence of 0.0 for a day that processed
        thousands, with no error. That reads as "nothing was processed", not as
        "the filter is wrong". Pinned as-is.
        """
        stored_day = _stored_day()

        metrics = _analyzer().get_metrics(end_date=stored_day)

        assert metrics["total_documents"] == 0
        assert metrics["average_confidence"] == 0.0

        # The same day asked for as a range reports nothing at all.
        same_day = _analyzer().get_metrics(start_date=stored_day, end_date=stored_day)
        assert same_day["total_documents"] == 0

    def test_an_end_date_on_the_following_day_includes_them(self, metrics_corpus):
        """The workaround, and the evidence that the exclusion above is about the
        *format* rather than about the data: one day later includes everything."""
        tomorrow = (_stored_datetime() + timedelta(days=1)).date().isoformat()

        assert _analyzer().get_metrics(end_date=tomorrow)["total_documents"] == 3

    def test_an_underlying_failure_propagates_rather_than_being_reported_as_zero(
        self, idp_stack
    ):
        """A metrics call that cannot read the bucket must raise. Returning zeros
        would be indistinguishable from an idle day."""
        analyzer = _analyzer()
        boto3.client("s3", region_name=REGION).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(Exception, match="NoSuchBucket|does not exist"):
            analyzer.get_metrics()


def _stored_datetime():
    """The ``LastModified`` moto recorded for one of the corpus objects.

    Read back rather than taken from the wall clock, so the date-filter tests
    cannot straddle midnight between writing the object and computing the
    boundary.
    """
    listing = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=OUTPUT_BUCKET
    )
    return listing["Contents"][0]["LastModified"]


def _stored_day():
    return _stored_datetime().date().isoformat()


# ---------------------------------------------------------------------------
# AssessmentOperation — the public wrapper
# ---------------------------------------------------------------------------


class TestOperationGetConfidence:
    def test_the_analyzer_dictionary_becomes_a_typed_result(self, idp_stack):
        """Field by field: these are dataclasses built by keyword, and a renamed
        key in the analyzer would raise here rather than being silently dropped —
        but only if every field is actually read."""
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        result = idp_stack.assessment.get_confidence("batch-1/invoice.pdf")

        assert isinstance(result, AssessmentConfidenceResult)
        assert result.document_id == "batch-1/invoice.pdf"
        assert result.section_id == 1
        total = result.attributes["total_amount"]
        assert isinstance(total, AssessmentFieldConfidence)
        assert total.confidence == 0.97
        assert total.confidence_threshold == 0.9
        assert total.meets_threshold is True
        assert "unambiguous" in total.reason
        assert result.attributes["vendor"].meets_threshold is False

    def test_the_documented_review_filter_works_on_the_result(self, idp_stack):
        """The docstring's headline use case is selecting the fields that need
        human review. Asserting it here keeps the example honest."""
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        confidence = idp_stack.assessment.get_confidence("batch-1/invoice.pdf")
        needs_review = [
            field
            for field, attr in confidence.attributes.items()
            if not attr.meets_threshold
        ]

        assert needs_review == ["vendor"]

    def test_the_section_argument_is_forwarded(self, idp_stack):
        _put_result(
            "multi.pdf",
            {"explainability_info": [{"policy_number": {"confidence": 0.11}}]},
            section_id=4,
        )

        result = idp_stack.assessment.get_confidence("multi.pdf", section_id=4)

        assert result.section_id == 4
        assert set(result.attributes) == {"policy_number"}

    def test_an_unprocessed_document_is_a_resource_not_found_error(self, idp_stack):
        with pytest.raises(IDPResourceNotFoundError, match="never-processed.pdf"):
            idp_stack.assessment.get_confidence("never-processed.pdf")

    def test_any_other_failure_is_a_processing_error(self, idp_stack):
        boto3.client("s3", region_name=REGION).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to get confidence"):
            idp_stack.assessment.get_confidence("invoice.pdf")

    def test_an_unusable_stack_surfaces_as_a_processing_error(self, aws_credentials):
        """Worth knowing: the analyzer's ``ValueError`` about stack state is
        caught by the same handler, so a missing or mid-rollback stack arrives as
        ``IDPProcessingError`` rather than ``IDPStackError``. The message still
        names the cause."""
        with mock_aws():
            client = IDPClient(stack_name="never-created", region=REGION)
            with pytest.raises(IDPProcessingError, match="not in a valid state"):
                client.assessment.get_confidence("invoice.pdf")


class TestOperationGetGeometry:
    def test_the_analyzer_dictionary_becomes_a_typed_result(self, idp_stack):
        _put_result("batch-1/invoice.pdf", CONFIDENCE_RESULT)

        result = idp_stack.assessment.get_geometry("batch-1/invoice.pdf")

        assert isinstance(result, AssessmentGeometryResult)
        assert result.document_id == "batch-1/invoice.pdf"
        geometry = result.attributes["total_amount"]
        assert geometry.page == 2
        assert geometry.bbox == [120.0, 340.0, 260.0, 362.0]
        assert geometry.bounding_box["Left"] == 0.12
        assert "vendor" not in result.attributes

    def test_a_section_with_no_geometry_yields_an_empty_mapping(self, idp_stack):
        _put_result("plain.pdf", {"explainability_info": [{"a": {"confidence": 0.9}}]})

        result = idp_stack.assessment.get_geometry("plain.pdf")

        assert result.attributes == {}

    def test_an_unprocessed_document_is_a_resource_not_found_error(self, idp_stack):
        with pytest.raises(IDPResourceNotFoundError, match="never-processed.pdf"):
            idp_stack.assessment.get_geometry("never-processed.pdf")

    def test_any_other_failure_is_a_processing_error(self, idp_stack):
        boto3.client("s3", region_name=REGION).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to get geometry"):
            idp_stack.assessment.get_geometry("invoice.pdf")


class TestOperationGetMetrics:
    def test_the_metrics_are_returned_unchanged_and_the_filters_forwarded(
        self, metrics_corpus
    ):
        metrics = metrics_corpus.assessment.get_metrics(batch_id="batch-2")

        assert metrics["total_documents"] == 1
        assert metrics["average_confidence"] == pytest.approx(0.60)
        assert set(metrics["by_document_class"]) == {"receipt"}

    def test_the_documented_by_field_and_threshold_compliance_keys_do_not_exist(
        self, metrics_corpus
    ):
        """DEFECT (operations/assessment.py:311-318). The docstring promises six
        keys, of which ``by_field`` and ``threshold_compliance`` are never
        produced by the analyzer. Both appear in the method's own worked examples
        (``metrics['threshold_compliance']``), so a caller following the
        documentation gets a ``KeyError`` on a call that otherwise succeeded.
        Pinned to the four keys the analyzer really returns.
        """
        metrics = metrics_corpus.assessment.get_metrics()

        assert set(metrics) == {
            "total_documents",
            "average_confidence",
            "low_confidence_count",
            "by_document_class",
        }
        assert "by_field" not in metrics
        assert "threshold_compliance" not in metrics

    def test_a_failure_is_wrapped_as_a_processing_error(self, idp_stack):
        boto3.client("s3", region_name=REGION).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to get assessment"):
            idp_stack.assessment.get_metrics()

    def test_all_four_filters_reach_the_analyzer(self, metrics_corpus, monkeypatch):
        """The wrapper's only job here is forwarding, and it forwards by keyword —
        a positional call would silently reorder ``document_class`` and
        ``batch_id``, which are both strings."""
        seen = {}

        def fake_get_metrics(self, **kwargs):
            seen.update(kwargs)
            return {}

        monkeypatch.setattr(AssessmentAnalyzer, "get_metrics", fake_get_metrics)

        metrics_corpus.assessment.get_metrics(
            start_date="2026-01-01",
            end_date="2026-01-31T23:59:59Z",
            document_class="invoice",
            batch_id="batch-1",
        )

        assert seen == {
            "start_date": "2026-01-01",
            "end_date": "2026-01-31T23:59:59Z",
            "document_class": "invoice",
            "batch_id": "batch-1",
        }

    def test_a_stack_override_selects_a_different_deployment(self, aws_credentials):
        """``stack_name=`` is the documented multi-deployment escape hatch, so the
        analyzer must be built for the override rather than for the client's
        default. A second real stack makes the difference observable: the answer
        comes from the other stack's bucket."""
        other_stack = "idp-assess-other"
        other_bucket = "idp-assess-other-output"
        with mock_aws():
            cfn = boto3.client("cloudformation", region_name=REGION)
            cfn.create_stack(
                StackName=STACK_NAME, TemplateBody=json.dumps(STACK_TEMPLATE)
            )
            other_template = json.loads(json.dumps(STACK_TEMPLATE))
            other_template["Outputs"]["S3OutputBucketName"]["Value"] = other_bucket
            other_template["Resources"]["DocumentQueue"]["Properties"]["QueueName"] = (
                "idp-assess-other-queue"
            )
            cfn.create_stack(
                StackName=other_stack, TemplateBody=json.dumps(other_template)
            )
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=OUTPUT_BUCKET)
            s3.create_bucket(Bucket=other_bucket)
            s3.put_object(
                Bucket=other_bucket,
                Key="only-there.pdf/sections/1/result.json",
                Body=json.dumps(_metrics_payload("receipt", [0.9])).encode(),
            )

            client = IDPClient(stack_name=STACK_NAME, region=REGION)

            assert client.assessment.get_metrics()["total_documents"] == 0
            override = client.assessment.get_metrics(stack_name=other_stack)
            assert override["total_documents"] == 1
            assert set(override["by_document_class"]) == {"receipt"}


def test_the_analyzer_reports_a_utc_aware_last_modified(idp_stack):
    """Guard for the date-filter tests above rather than for the analyzer.

    Those tests derive their boundaries from the stored ``LastModified``. If moto
    ever returned a naive datetime, ``.isoformat()`` would lose the ``+00:00``
    suffix, the lexical comparisons would shift, and the defect the tests pin
    would appear to have changed shape.
    """
    _put_result("x.pdf", {"explainability_info": []})

    stored = _stored_datetime()

    assert isinstance(stored, datetime)
    assert stored.tzinfo is not None
    assert stored.utcoffset() == timezone.utc.utcoffset(None)
