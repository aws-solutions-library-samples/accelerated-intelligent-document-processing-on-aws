# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run start moves a resubmitted document's earlier usage into its baseline.

Every resubmission path (processChanges and ``trigger_reprocessing`` send a
document read back from the tracking table, the SDK rerun sends an uncompressed
``to_dict``, review finalize sends ``metering == {}``) reaches
``start_workflow``. That is where the incoming usage becomes
``metering_baseline`` and ``metering`` is reset, so each stage of the new run
adds only its own usage and SaveReportingData is not handed the earlier runs'
usage again (GitHub #1666). The tracking row must still hold the cumulative
total.

Unlike the other suites in this directory, this one runs the handler against the
REAL ``idp_common`` (``Document`` serialization, compression to S3 and the
DynamoDB writer) on moto, because the assertions are about what those produce.
Only X-Ray and the concurrency plumbing that is not under test are replaced.
"""

import copy
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_index_metering_baseline_test"
_REGION = "us-east-1"
_TRACKING_TABLE = "metering-baseline-tracking"
_WORKING_BUCKET = "metering-baseline-working"
_SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:metering"

CUMULATIVE = {
    "OCR/textract/analyze_document-Signatures": {"pages": 3},
    "Classification/bedrock/model-a": {"inputTokens": 900, "outputTokens": 40},
    "Extraction/bedrock/model-a": {"inputTokens": 2000, "outputTokens": 300},
    "Assessment/bedrock/model-a": {"inputTokens": 1200, "outputTokens": 90},
}


@pytest.fixture
def processor(monkeypatch):
    # Placeholder credentials for moto, set one by one rather than in the dict below.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    env = {
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
        "TRACKING_TABLE": _TRACKING_TABLE,
        "WORKING_BUCKET": _WORKING_BUCKET,
        "CONCURRENCY_TABLE": "metering-baseline-concurrency",
        "STATE_MACHINE_ARN": _SM_ARN,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # No configuration table: routing defaults to pipeline, which is not under test.
    monkeypatch.delenv("CONFIG_TABLE", raising=False)

    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder.current_segment.return_value = None
    monkeypatch.setitem(sys.modules, "aws_xray_sdk", MagicMock())
    monkeypatch.setitem(sys.modules, "aws_xray_sdk.core", xray_core)

    with mock_aws():
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_TRACKING_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_WORKING_BUCKET)

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        starts = []

        class _Sfn:
            def start_execution(self, **kwargs):
                starts.append(json.loads(kwargs["input"])["document"])
                return {"executionArn": f"{_SM_ARN}:e{len(starts)}"}

        module.sfn = _Sfn()
        with (
            patch.object(module, "claim_start", return_value=True),
            patch.object(
                module, "check_circuit_breaker", return_value=(True, "CLOSED")
            ),
            patch.object(module, "update_counter", return_value=True),
            patch.object(module, "ack_message"),
        ):
            yield {"module": module, "starts": starts}
        sys.modules.pop(_MODULE_NAME, None)


def _document(metering):
    from idp_common.models import Document, Status

    return Document(
        id="reprocess.pdf",
        input_bucket="input",
        input_key="reprocess.pdf",
        output_bucket="output",
        status=Status.QUEUED,
        config_version="default",
        metering=copy.deepcopy(metering),
    )


def _from_tracking_row(metering):
    """processChanges / trigger_reprocessing: a document read back from the row,
    compressed to the working bucket like every pipeline message."""
    from idp_common.models import Document

    doc = _document(metering)
    return Document.load_document(
        doc.serialize_document(_WORKING_BUCKET, "reprocess"), _WORKING_BUCKET
    ).serialize_document(_WORKING_BUCKET, "reprocess")


def _sdk_rerun(metering):
    """The SDK rerun's ``_send_to_queue``: an uncompressed ``to_dict``."""
    return _document(metering).to_dict()


SHAPES = [
    pytest.param(_from_tracking_row, CUMULATIVE, id="tracking-row-reprocess"),
    pytest.param(_sdk_rerun, CUMULATIVE, id="sdk-rerun-uncompressed"),
    pytest.param(_sdk_rerun, {}, id="fresh-document"),
    pytest.param(_from_tracking_row, {}, id="review-finalize-empty-metering"),
]


def _run(processor, message_body):
    record = {
        "body": json.dumps(message_body),
        "messageId": "msg-1",
        "receiptHandle": "rh-1",
    }
    ok, _ = processor["module"].process_message(record)
    assert ok
    (started,) = processor["starts"]
    return started


def _decompressed(started):
    from idp_common.models import Document

    assert started.get("compressed") is True
    return Document.decompress(_WORKING_BUCKET, started)


def _raw_sfn_document_json(started):
    from idp_common.utils import parse_s3_uri

    _, key = parse_s3_uri(started["s3_uri"])
    body = boto3.client("s3", region_name=_REGION).get_object(
        Bucket=_WORKING_BUCKET, Key=key
    )["Body"]
    return json.loads(body.read())


def _tracking_metering():
    item = (
        boto3.resource("dynamodb", region_name=_REGION)
        .Table(_TRACKING_TABLE)
        .get_item(Key={"PK": "doc#reprocess.pdf", "SK": "none"})["Item"]
    )
    raw = item.get("Metering")
    return json.loads(raw) if raw else {}


@pytest.mark.unit
@pytest.mark.parametrize(("build_message", "incoming"), SHAPES)
def test_run_starts_with_empty_own_usage_and_the_incoming_total_as_baseline(
    processor, build_message, incoming
):
    started = _run(processor, build_message(incoming))

    sfn_document = _decompressed(started)
    assert sfn_document.metering == {}
    assert sfn_document.metering_baseline == incoming


@pytest.mark.unit
@pytest.mark.parametrize(("build_message", "incoming"), SHAPES)
def test_tracking_row_keeps_the_incoming_cumulative_total(
    processor, build_message, incoming
):
    _run(processor, build_message(incoming))

    assert _tracking_metering() == incoming


@pytest.mark.unit
def test_fresh_document_payload_carries_no_baseline_key(processor):
    started = _run(processor, _sdk_rerun({}))

    payload = _raw_sfn_document_json(started)
    assert "metering_baseline" not in payload
    assert payload["metering"] == {}


@pytest.mark.unit
def test_reprocessed_payload_carries_the_baseline_key(processor):
    started = _run(processor, _sdk_rerun(CUMULATIVE))

    payload = _raw_sfn_document_json(started)
    assert payload["metering_baseline"] == CUMULATIVE
    assert payload["metering"] == {}
