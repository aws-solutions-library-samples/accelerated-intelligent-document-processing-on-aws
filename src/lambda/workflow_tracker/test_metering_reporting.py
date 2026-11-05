# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reprocessed run reports only its own usage (GitHub #1666).

The run start moves a resubmitted document's earlier usage into
``metering_baseline``. At completion the tracker must:

- hand SaveReportingData the run's own ``metering`` and no baseline, so skipped
  OCR, Classification, Extraction and Assessment are not counted again;
- write the tracking row with baseline + own (the DynamoDB writer adds them);
- record the run (Versions) with the run's own usage.

Unlike the other suites in this directory, this one runs the tracker against the
REAL ``idp_common`` ``Document`` and DynamoDB writer, because the assertions are
about what they serialize. The DynamoDB client, Lambda client and the
counter / metrics plumbing are replaced.
"""

import copy
import importlib.util
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "workflow_tracker_metering_reporting_under_test"
EXEC_ARN = "arn:aws:states:us-east-1:123456789012:execution:sm:exec-2"

BASELINE = {
    "OCR/textract/analyze_document": {"pages": 3},
    "Classification/bedrock/model-a": {"inputTokens": 900, "outputTokens": 40},
    "Extraction/bedrock/model-a": {"inputTokens": 2000, "outputTokens": 300},
    "Assessment/bedrock/model-a": {"inputTokens": 1200, "outputTokens": 90},
}
OWN = {
    "Summarization/bedrock/model-b": {"inputTokens": 400, "outputTokens": 60},
    "OCR/lambda/duration": {"gb_seconds": 0.5},
    "Extraction/lambda/duration": {"gb_seconds": 0.25},
}
_SKIPPED_STAGE_KEYS = {
    "OCR/textract/analyze_document",
    "Classification/bedrock/model-a",
    "Extraction/bedrock/model-a",
    "Assessment/bedrock/model-a",
}


@pytest.fixture
def tracker(monkeypatch):
    env_vars = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "CONCURRENCY_TABLE": "test-concurrency",
        "METRIC_NAMESPACE": "TEST_NS",
        "TRACKING_TABLE": "test-tracking",
        "INPUT_BUCKET": "test-input",
        "OUTPUT_BUCKET": "test-output",
    }
    for name in ("HITL_TABLE_NAME", "WORKING_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    monkeypatch.setitem(sys.modules, "aws_xray_sdk", MagicMock())
    monkeypatch.setitem(sys.modules, "aws_xray_sdk.core", xray_core)
    with (
        patch.dict(os.environ, env_vars, clear=False),
        patch("boto3.resource") as mock_resource,
        patch("boto3.client") as mock_client,
    ):
        mock_resource.return_value.Table.return_value = MagicMock()
        mock_client.return_value = MagicMock()
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        from idp_common.dynamodb.service import DocumentDynamoDBService

        ddb_client = MagicMock()

        def _update_item(**kwargs):
            # ALL_NEW, as the real table returns it: the stored Metering string.
            values = kwargs["expression_attribute_values"]
            item = {
                "PK": "doc#reprocess.pdf",
                "SK": "none",
                "ObjectKey": "reprocess.pdf",
                "ObjectStatus": values[":ObjectStatus"],
            }
            if ":Metering" in values:
                item["Metering"] = values[":Metering"]
            return {"Attributes": item}

        ddb_client.update_item.side_effect = _update_item
        module.document_service = DocumentDynamoDBService(dynamodb_client=ddb_client)
        module.cloudwatch = MagicMock()
        module.lambda_client = MagicMock()
        module.lambda_client.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({"statusCode": 200}).encode())
        }
        monkeypatch.setattr(module, "REPORTING_BUCKET", "test-reporting")
        monkeypatch.setattr(module, "SAVE_REPORTING_FUNCTION_NAME", "save-reporting")
        record_run = MagicMock(return_value=None)
        for name, value in {
            "record_document_run": record_run,
            "decrement_counter": MagicMock(return_value=0),
            "put_latency_metrics": MagicMock(),
            "notify_circuit_breaker_success": MagicMock(),
        }.items():
            monkeypatch.setattr(module, name, value)
        yield {"module": module, "ddb": ddb_client, "record_run": record_run}
        sys.modules.pop(_MODULE_NAME, None)


def _reprocessed_output():
    from idp_common.models import Document, Status

    doc = Document(
        id="reprocess.pdf",
        input_key="reprocess.pdf",
        output_bucket="test-output",
        status=Status.SUMMARIZING,
        metering=copy.deepcopy(OWN),
    )
    doc.metering_baseline = copy.deepcopy(BASELINE)
    return {"document": doc.to_dict()}


def _event(output):
    return {
        "detail": {
            "executionArn": EXEC_ARN,
            "status": "SUCCEEDED",
            "input": json.dumps({"document": {"document_id": "reprocess.pdf"}}),
            "output": json.dumps(output),
        }
    }


def _reporting_document(tracker):
    payload = json.loads(
        tracker["module"].lambda_client.invoke.call_args.kwargs["Payload"]
    )
    return payload["document"], payload["data_to_save"]


@pytest.mark.unit
class TestReprocessedRunReporting:
    def test_reporting_gets_only_the_runs_own_usage(self, tracker):
        tracker["module"].handler(_event(_reprocessed_output()), None)

        document, data_to_save = _reporting_document(tracker)
        assert "metering" in data_to_save
        assert document["metering"] == OWN
        assert not (_SKIPPED_STAGE_KEYS & set(document["metering"]))

    def test_reporting_payload_has_no_baseline(self, tracker):
        tracker["module"].handler(_event(_reprocessed_output()), None)

        document, _ = _reporting_document(tracker)
        assert "metering_baseline" not in document

    def test_tracking_row_stores_baseline_plus_own(self, tracker):
        tracker["module"].handler(_event(_reprocessed_output()), None)

        values = tracker["ddb"].update_item.call_args.kwargs[
            "expression_attribute_values"
        ]
        assert json.loads(values[":Metering"]) == {**BASELINE, **OWN}

    def test_run_record_gets_the_runs_own_usage(self, tracker):
        tracker["module"].handler(_event(_reprocessed_output()), None)

        (recorded,) = tracker["record_run"].call_args.args
        assert recorded.metering == OWN
        assert recorded.metering_baseline == BASELINE
