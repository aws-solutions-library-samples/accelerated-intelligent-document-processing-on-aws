# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for ``Document.metering_baseline`` and ``cumulative_metering``.

A reprocessed document carries earlier runs' usage in ``metering_baseline`` and
only the current run's own usage in ``metering``. These tests pin the field's
serialization and the helper that adds the two back together.
"""

import copy
import os
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from idp_common.models import Document, Status
from idp_common.utils import cumulative_metering

BASELINE = {
    "OCR/textract/analyze_document": {"pages": 2},
    "Extraction/bedrock/model-a": {"inputTokens": 100, "outputTokens": 10},
}
OWN = {
    "Extraction/bedrock/model-a": {"inputTokens": 5},
    "Summarization/bedrock/model-b": {"inputTokens": 7},
}


def _document(**kwargs) -> Document:
    return Document(
        id="doc-1",
        input_bucket="in",
        input_key="doc-1.pdf",
        output_bucket="out",
        status=Status.QUEUED,
        **kwargs,
    )


@pytest.mark.unit
class TestMeteringBaselineSerialization:
    def test_dict_round_trip_keeps_baseline_and_own(self):
        doc = _document(metering=copy.deepcopy(OWN))
        doc.metering_baseline = copy.deepcopy(BASELINE)

        restored = Document.from_dict(doc.to_dict())

        assert restored.metering_baseline == BASELINE
        assert restored.metering == OWN

    def test_to_dict_omits_empty_baseline(self):
        doc = _document(metering=copy.deepcopy(OWN))

        data = doc.to_dict()

        assert "metering_baseline" not in data
        assert Document.from_dict(data).metering_baseline == {}

    def test_from_dict_reads_null_baseline_as_empty(self):
        data = _document().to_dict()
        data["metering_baseline"] = None

        assert Document.from_dict(data).metering_baseline == {}

    def test_json_round_trip(self):
        doc = _document(metering=copy.deepcopy(OWN))
        doc.metering_baseline = copy.deepcopy(BASELINE)

        restored = Document.from_json(doc.to_json())

        assert restored.metering_baseline == BASELINE
        assert restored.metering == OWN

    @mock_aws
    def test_compress_decompress_round_trip(self):
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        bucket = "working-bucket"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        doc = _document(metering=copy.deepcopy(OWN))
        doc.metering_baseline = copy.deepcopy(BASELINE)

        wrapper = doc.compress(bucket, "unit")
        restored = Document.decompress(bucket, wrapper)

        assert restored.metering_baseline == BASELINE
        assert restored.metering == OWN


@pytest.mark.unit
class TestCumulativeMetering:
    def test_sums_baseline_and_own_without_mutating_inputs(self):
        doc = _document(metering=copy.deepcopy(OWN))
        doc.metering_baseline = copy.deepcopy(BASELINE)
        saved_own = copy.deepcopy(doc.metering)
        saved_baseline = copy.deepcopy(doc.metering_baseline)

        result = cumulative_metering(doc)

        assert result == {
            "OCR/textract/analyze_document": {"pages": 2},
            "Extraction/bedrock/model-a": {"inputTokens": 105, "outputTokens": 10},
            "Summarization/bedrock/model-b": {"inputTokens": 7},
        }
        assert doc.metering == saved_own
        assert doc.metering_baseline == saved_baseline
        # The result shares no inner dict with either input.
        result["Extraction/bedrock/model-a"]["inputTokens"] = -1
        assert doc.metering == saved_own
        assert doc.metering_baseline == saved_baseline

    def test_object_without_baseline_attribute(self):
        doc = SimpleNamespace(metering=copy.deepcopy(OWN))

        assert cumulative_metering(doc) == OWN

    def test_none_and_empty_values(self):
        assert cumulative_metering(SimpleNamespace(metering=None)) == {}
        assert (
            cumulative_metering(SimpleNamespace(metering={}, metering_baseline=None))
            == {}
        )

    def test_string_values_are_summed_numerically(self):
        doc = SimpleNamespace(
            metering={"svc": {"tokens": "5"}},
            metering_baseline={"svc": {"tokens": "10"}},
        )

        assert cumulative_metering(doc) == {"svc": {"tokens": 15.0}}
