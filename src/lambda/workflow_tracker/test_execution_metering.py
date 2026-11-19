# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every workflow execution's usage is reported once, whatever its status (#1951).

A FAILED, TIMED_OUT or ABORTED execution was billed for every stage it ran, but the
tracker used to read metering only from a SUCCEEDED execution's output, so that
spend never reached the reporting lake. The tracker now:

- recovers an unsuccessful run's own usage as the tracking row's cumulative
  ``Metering`` minus the ``metering_baseline`` the run started with;
- reports every execution's usage under that execution, with its status, so
  totals are actual spend and failed attempts can be broken out;
- reports it once per execution: the parquet object is named after the execution,
  and a marker in the counter table stops a later redelivery writing it again.

These run the REAL tracker, the REAL ``idp_common`` document service and the REAL
SaveReportingData Lambda handler and writer against ``moto`` S3 and DynamoDB. The
execution input is produced by the real compression path, and the tracking row by
the real DynamoDB writer, the way the queue processor and the stages produce them.
Only CloudWatch metrics and the circuit breaker are replaced.
"""

import copy
import importlib.util
import io
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

moto = pytest.importorskip("moto")
pq = pytest.importorskip("pyarrow.parquet")

_HERE = Path(__file__).resolve().parent
_TRACKER_NAME = "workflow_tracker_execution_metering_under_test"
_REPORTER_NAME = "save_reporting_data_execution_metering_under_test"
REGION = "us-east-1"
TRACKING = "test-tracking"
CONCURRENCY = "test-concurrency"
WORKING = "test-working"
REPORTING = "test-reporting"
OUTPUT = "test-output"
DOC_ID = "batch/statement.pdf"
ARN_1 = "arn:aws:states:us-east-1:123456789012:execution:sm:run-one"
ARN_2 = "arn:aws:states:us-east-1:123456789012:execution:sm:run-two"

# Usage of an earlier, completed run of the document (the run start moves it into
# the baseline) and of the run under test, up to the last stage that wrote the row.
EARLIER = {
    "OCR/textract/detect_document_text": {"pages": 3},
    "Classification/bedrock/model-a": {"inputTokens": 900, "outputTokens": 40},
}
THIS_RUN = {
    "OCR/textract/detect_document_text": {"pages": 3},
    "Classification/bedrock/model-a": {"inputTokens": 950, "outputTokens": 45},
    "OCR/lambda/duration": {"gb_seconds": 0.5},
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    values = {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "CONCURRENCY_TABLE": CONCURRENCY,
        "METRIC_NAMESPACE": "TEST_NS",
        "TRACKING_TABLE": TRACKING,
        "DOCUMENT_TRACKING_MODE": "dynamodb",
        "WORKING_BUCKET": WORKING,
        "OUTPUT_BUCKET": OUTPUT,
        "INPUT_BUCKET": "test-input",
        "REPORTING_BUCKET": REPORTING,
        "SAVE_REPORTING_FUNCTION_NAME": "save-reporting",
        "DOCUMENT_VERSIONING_ENABLED": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "HITL_TABLE_NAME",
        "CONFIGURATION_TABLE_NAME",
        "STACK_NAME",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder.capture = lambda *_a, **_k: lambda f: f
    monkeypatch.setitem(sys.modules, "aws_xray_sdk", MagicMock(core=xray_core))
    monkeypatch.setitem(sys.modules, "aws_xray_sdk.core", xray_core)

    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TRACKING,
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
        ddb.create_table(
            TableName=CONCURRENCY,
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "counter_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.put_item(
            TableName=CONCURRENCY,
            Item={"counter_id": {"S": "workflow_counter"}, "active_count": {"N": "5"}},
        )
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (WORKING, REPORTING, OUTPUT):
            s3.create_bucket(Bucket=bucket)

        tracker = _load(_TRACKER_NAME, _HERE / "index.py")
        reporter = _load(
            _REPORTER_NAME, _HERE.parent / "save_reporting_data" / "index.py"
        )

        def _invoke(FunctionName, InvocationType, Payload):
            response = reporter.handler(json.loads(Payload), None)
            return {"Payload": io.BytesIO(json.dumps(response).encode())}

        tracker.lambda_client = MagicMock()
        tracker.lambda_client.invoke.side_effect = _invoke
        tracker.cloudwatch = MagicMock()
        monkeypatch.setattr(tracker, "notify_circuit_breaker_success", MagicMock())
        monkeypatch.setattr(tracker, "put_latency_metrics", MagicMock())
        yield {"tracker": tracker, "s3": s3, "ddb": ddb}
    sys.modules.pop(_TRACKER_NAME, None)
    sys.modules.pop(_REPORTER_NAME, None)


def _document(**fields):
    from idp_common.models import Document, Status

    return Document(
        id=DOC_ID,
        input_key=DOC_ID,
        output_bucket=OUTPUT,
        status=fields.pop("status", Status.QUEUED),
        queued_time="2026-11-18T10:00:00+00:00",
        initial_event_time="2026-11-18T10:00:00+00:00",
        **fields,
    )


def _start_run(baseline_source):
    """The execution input, built the way ``queue_processor.start_workflow`` does.

    ``baseline_source`` is the document as stored before the run; the run start
    moves its cumulative usage into the baseline and resets ``metering``, then
    compresses the document into the working bucket.
    """
    from idp_common.utils import cumulative_metering

    doc = copy.deepcopy(baseline_source)
    doc.metering_baseline = cumulative_metering(doc)
    doc.metering = {}
    wrapper = doc.serialize_document(WORKING, "workflow_start", logging.getLogger())
    return doc, {"document": wrapper}


def _stage_writes(start_doc, own, sections=None):
    """A stage writing the tracking row as it does: baseline + own (real writer)."""
    from idp_common.docs_service import create_document_service
    from idp_common.models import Status

    doc = copy.deepcopy(start_doc)
    doc.metering = copy.deepcopy(own)
    doc.sections = list(sections or [])
    doc.status = Status.CLASSIFYING
    create_document_service().update_document(doc)


def _create_row(doc):
    from idp_common.docs_service import create_document_service

    create_document_service().create_document(doc)


def _event(arn, status, input_data, output=None):
    detail = {
        "executionArn": arn,
        "status": status,
        "input": json.dumps(input_data),
        "stopDate": 1763460000000,
    }
    if output is not None:
        detail["output"] = json.dumps(output)
    return {"detail": detail}


def _metering_objects(s3):
    listed = s3.list_objects_v2(Bucket=REPORTING, Prefix="metering/")
    return sorted(o["Key"] for o in listed.get("Contents", []))


def _rows(s3, key):
    body = s3.get_object(Bucket=REPORTING, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body)).to_pylist()


def _usage(rows):
    usage = {}
    for row in rows:
        key = f"{row['context']}/{row['service_api']}"
        usage.setdefault(key, {})[row["unit"]] = row["value"]
    return usage


def _as_floats(metering):
    return {k: {u: float(v) for u, v in m.items()} for k, m in metering.items()}


def _stored_metering():
    from idp_common.docs_service import create_document_service

    return create_document_service().get_document(DOC_ID).metering


@pytest.mark.unit
class TestUnsuccessfulExecutionIsReported:
    @pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
    def test_a_reprocessed_run_that_fails_reports_only_its_own_usage(self, env, status):
        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        _stage_writes(start_doc, THIS_RUN)

        env["tracker"].handler(_event(ARN_1, status, input_data), None)

        (key,) = _metering_objects(env["s3"])
        rows = _rows(env["s3"], key)
        assert _usage(rows) == _as_floats(THIS_RUN)
        assert {r["execution_status"] for r in rows} == {status}
        assert {r["execution_arn"] for r in rows} == {ARN_1}
        assert key.endswith("_exec_run-one_results.parquet")

    def test_the_tracking_row_keeps_the_cumulative_total(self, env):
        from idp_common.utils import merge_metering_data

        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        _stage_writes(start_doc, THIS_RUN)

        env["tracker"].handler(_event(ARN_1, "FAILED", input_data), None)

        expected = merge_metering_data(copy.deepcopy(EARLIER), copy.deepcopy(THIS_RUN))
        assert _as_floats(_stored_metering()) == _as_floats(expected)

    def test_a_run_that_failed_before_any_stage_wrote_reports_nothing(self, env):
        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        _start_doc, input_data = _start_run(stored)

        env["tracker"].handler(_event(ARN_1, "FAILED", input_data), None)

        assert _metering_objects(env["s3"]) == []

    def test_a_first_run_that_fails_reports_what_its_stages_wrote(self, env):
        stored = _document()
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        _stage_writes(start_doc, THIS_RUN)

        env["tracker"].handler(_event(ARN_1, "FAILED", input_data), None)

        (key,) = _metering_objects(env["s3"])
        assert _usage(_rows(env["s3"], key)) == _as_floats(THIS_RUN)

    def test_only_usage_is_reported_for_an_unsuccessful_run(self, env):
        from idp_common.models import Section

        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        # A section the row holds with an extraction result: on success it would
        # be reported, so a failed run reporting only usage is a real choice.
        section = Section(
            section_id="1",
            classification="Bank-Statement",
            page_ids=["1"],
            extraction_result_uri=f"s3://{OUTPUT}/{DOC_ID}/sections/1/result.json",
        )
        _stage_writes(start_doc, THIS_RUN, sections=[section])

        env["tracker"].handler(_event(ARN_1, "FAILED", input_data), None)

        payload = json.loads(
            env["tracker"].lambda_client.invoke.call_args.kwargs["Payload"]
        )
        assert payload["data_to_save"] == ["metering"]
        assert payload["execution_status"] == "FAILED"
        # The row's section did reach the reported document, so the class column
        # is the row's rather than "unknown".
        (key,) = _metering_objects(env["s3"])
        assert {r["document_class"] for r in _rows(env["s3"], key)} == {
            "Bank-Statement"
        }


@pytest.mark.unit
class TestOncePerExecution:
    def _failed_run(self, env):
        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        _stage_writes(start_doc, THIS_RUN)
        return _event(ARN_1, "FAILED", input_data)

    def test_a_redelivered_event_does_not_report_again(self, env):
        event = self._failed_run(env)

        env["tracker"].handler(event, None)
        env["tracker"].handler(event, None)

        (key,) = _metering_objects(env["s3"])
        assert _usage(_rows(env["s3"], key)) == _as_floats(THIS_RUN)
        # The marker, not the file name, stopped the second write: it was never
        # asked to save metering.
        assert env["tracker"].lambda_client.invoke.call_count == 1

    def test_without_the_marker_a_redelivery_in_the_hour_replaces_the_object(self, env):
        event = self._failed_run(env)

        env["tracker"].handler(event, None)
        env["ddb"].delete_item(
            TableName=CONCURRENCY, Key={"counter_id": {"S": f"meter#{ARN_1}"}}
        )
        env["tracker"].handler(event, None)

        assert env["tracker"].lambda_client.invoke.call_count == 2
        (key,) = _metering_objects(env["s3"])
        assert _usage(_rows(env["s3"], key)) == _as_floats(THIS_RUN)

    def test_the_marker_is_written_only_after_the_usage_is_stored(self, env):
        event = self._failed_run(env)
        tracker = env["tracker"]
        tracker.lambda_client.invoke.side_effect = None
        tracker.lambda_client.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({"statusCode": 500}).encode())
        }

        tracker.handler(event, None)

        marker = env["ddb"].get_item(
            TableName=CONCURRENCY, Key={"counter_id": {"S": f"meter#{ARN_1}"}}
        )
        assert "Item" not in marker

    def test_reprocessing_is_a_new_execution_with_its_own_record(self, env):
        from idp_common.docs_service import create_document_service

        failed = self._failed_run(env)
        env["tracker"].handler(failed, None)

        # Reprocess: the run start reads the stored document, whose cumulative
        # usage now includes the failed run's, and starts a new execution.
        stored = create_document_service().get_document(DOC_ID)
        start_doc, input_data = _start_run(stored)
        retry_usage = {"Extraction/bedrock/model-a": {"inputTokens": 2000}}
        _stage_writes(start_doc, retry_usage)
        env["tracker"].handler(_event(ARN_2, "FAILED", input_data), None)

        keys = _metering_objects(env["s3"])
        assert len(keys) == 2
        by_arn = {}
        for key in keys:
            rows = _rows(env["s3"], key)
            (arn,) = {r["execution_arn"] for r in rows}
            by_arn[arn] = _usage(rows)
        assert by_arn == {
            ARN_1: _as_floats(THIS_RUN),
            ARN_2: _as_floats(retry_usage),
        }


@pytest.mark.unit
class TestSucceededExecution:
    def test_success_is_reported_under_the_events_execution(self, env):
        from idp_common.models import Status

        stored = _document(metering=copy.deepcopy(EARLIER))
        _create_row(stored)
        start_doc, input_data = _start_run(stored)
        done = copy.deepcopy(start_doc)
        done.metering = copy.deepcopy(THIS_RUN)
        done.status = Status.COMPLETED
        # A resubmitted document carries the ARN of the run that last wrote it.
        done.workflow_execution_arn = ARN_2

        env["tracker"].handler(
            _event(ARN_1, "SUCCEEDED", input_data, {"document": done.to_dict()}),
            None,
        )

        (key,) = _metering_objects(env["s3"])
        rows = _rows(env["s3"], key)
        assert _usage(rows) == _as_floats(THIS_RUN)
        assert {r["execution_status"] for r in rows} == {"SUCCEEDED"}
        assert {r["execution_arn"] for r in rows} == {ARN_1}


def _template_metering_columns():
    """The ``metering`` Glue table's columns, read from ``template.yaml``."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    def _any_tag(loader, _suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _Loader.add_multi_constructor("!", _any_tag)
    root = next(p for p in _HERE.parents if (p / "template.yaml").is_file())
    template = yaml.load((root / "template.yaml").read_text(), Loader=_Loader)
    table = template["Resources"]["MeteringTable"]["Properties"]["TableInput"]
    return [c["Name"] for c in table["StorageDescriptor"]["Columns"]]


@pytest.mark.unit
def test_the_glue_table_declares_every_column_the_writer_writes():
    from unittest.mock import patch

    from idp_common.reporting.save_reporting_data import SaveReportingData

    with patch("boto3.client"):
        reporting = SaveReportingData(reporting_bucket="unused")
    doc = _document(metering={"OCR/textract/detect_document_text": {"pages": 1}})
    with (
        patch.object(reporting, "_save_records_as_parquet") as save,
        patch("idp_common.reporting.save_reporting_data.emit_document_cost_metrics"),
    ):
        reporting.save_metering_data(doc, execution_status="FAILED")
    written = save.call_args.args[2].names
    assert written, "the writer declared no columns"
    assert written == _template_metering_columns()
