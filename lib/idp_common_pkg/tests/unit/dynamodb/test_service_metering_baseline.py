# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The tracking row stores cumulative usage; the run record stores the run's own.

A reprocessed document carries earlier runs' usage in ``metering_baseline`` and
the current run's usage in ``metering``. ``update_document`` must write the sum
to the ``Metering`` attribute (the Estimated Cost panel), while
``create_document_run`` keeps only the run's own usage (the Versions record).
"""

import json
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status


def _document(metering, baseline):
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        status=Status.COMPLETED,
        completion_time="2025-07-07T14:15:30.000Z",
        initial_event_time="2025-07-07T13:59:00.000Z",
    )
    doc.metering = metering
    doc.metering_baseline = baseline
    return doc


@pytest.mark.unit
class TestTrackingRowMetering:
    def setup_method(self):
        self.mock_client = Mock()
        self.mock_client.update_item.return_value = {"Attributes": {}}
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def _update_kwargs(self, doc):
        self.service.update_document(doc)
        return self.mock_client.update_item.call_args.kwargs

    def test_row_stores_baseline_plus_own(self):
        kwargs = self._update_kwargs(_document({"B": {"n": 2}}, {"A": {"n": 1}}))

        assert "#Metering = :Metering" in kwargs["update_expression"]
        stored = json.loads(kwargs["expression_attribute_values"][":Metering"])
        assert stored == {"A": {"n": 1}, "B": {"n": 2}}

    def test_same_key_is_summed_not_replaced(self):
        kwargs = self._update_kwargs(_document({"A": {"n": 2}}, {"A": {"n": 1}}))

        stored = json.loads(kwargs["expression_attribute_values"][":Metering"])
        assert stored == {"A": {"n": 3}}

    def test_baseline_only_is_still_written(self):
        kwargs = self._update_kwargs(_document({}, {"A": {"n": 1}}))

        stored = json.loads(kwargs["expression_attribute_values"][":Metering"])
        assert stored == {"A": {"n": 1}}

    def test_both_empty_sets_no_metering(self):
        kwargs = self._update_kwargs(_document({}, {}))

        assert "#Metering" not in kwargs["update_expression"]
        assert ":Metering" not in kwargs["expression_attribute_values"]
        assert "#Metering" not in kwargs["expression_attribute_names"]

    def test_writer_does_not_mutate_document(self):
        doc = _document({"A": {"n": 2}}, {"A": {"n": 1}})

        self._update_kwargs(doc)

        assert doc.metering == {"A": {"n": 2}}
        assert doc.metering_baseline == {"A": {"n": 1}}

    def test_read_back_document_has_cumulative_metering_and_no_baseline(self):
        kwargs = self._update_kwargs(_document({"B": {"n": 2}}, {"A": {"n": 1}}))
        stored_item = {
            "PK": "doc#doc.pdf",
            "SK": "none",
            "ObjectKey": "doc.pdf",
            "ObjectStatus": "COMPLETED",
            "Metering": kwargs["expression_attribute_values"][":Metering"],
        }

        restored = self.service._dynamodb_item_to_document(stored_item)

        assert restored.metering == {"A": {"n": 1}, "B": {"n": 2}}
        assert restored.metering_baseline == {}


@pytest.mark.unit
class TestRunRecordMetering:
    def test_run_record_stores_own_usage_only(self):
        mock_client = Mock()
        service = DocumentDynamoDBService(dynamodb_client=mock_client)
        doc = _document({"B": {"n": 2}}, {"A": {"n": 1}})

        service.create_document_run(
            doc,
            run_id="20250707T141530Z-exec-abc",
            manifest_uri="s3://out/doc.pdf/runs/x/manifest.json",
        )

        item = mock_client.put_item.call_args[0][0]
        assert json.loads(item["Metering"]) == {"B": {"n": 2}}
