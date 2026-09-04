# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the queue_sender Lambda function."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# idp_common and aws_xray_sdk are heavy/AWS-dependent; mock them before import.
sys.modules["idp_common"] = MagicMock()
sys.modules["idp_common.models"] = MagicMock()
sys.modules["idp_common.docs_service"] = MagicMock()
sys.modules["idp_common.document_versions"] = MagicMock()

mock_xray_core = MagicMock()
# capture() is used as a decorator; make it a pass-through.
mock_xray_core.xray_recorder.capture.return_value = lambda fn: fn
sys.modules["aws_xray_sdk"] = MagicMock()
sys.modules["aws_xray_sdk.core"] = mock_xray_core


@pytest.fixture(autouse=True)
def mock_env():
    env_vars = {
        "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        "DATA_RETENTION_IN_DAYS": "30",
        "OUTPUT_BUCKET": "test-output-bucket",
        "CONFIG_TABLE": "test-config-table",
        "LOG_LEVEL": "INFO",
        # index.py builds boto3 clients (sqs/s3/cloudwatch) which need a region.
        # Without this the tests inherit one from the developer's environment and
        # pass locally, then fail in CI with NoRegionError — which is exactly what
        # happened. A unit test must not depend on ambient AWS configuration.
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    with patch.dict(os.environ, env_vars):
        yield


def make_event(key: str) -> dict:
    return {
        "detail": {
            "bucket": {"name": "test-input-bucket"},
            "object": {"key": key},
        },
        "time": "2026-07-23T00:00:00Z",
    }


@pytest.mark.unit
class TestFolderPseudoObject:
    """The handler must ignore S3 console folder pseudo-objects."""

    def test_skips_trailing_slash_key(self):
        """A '/'-terminated key is skipped without enqueuing or tracking."""
        import index

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "delete_current_output_objects") as mock_purge,
            patch.object(index.Document, "from_s3_event") as mock_from_event,
        ):
            response = index.handler(make_event("testfolder/"), None)

        assert response["statusCode"] == 200
        assert response["skipped"] == "folder_pseudo_object"
        # No document created, no SQS message sent, event never parsed.
        # Critically: the purge must not fire for a folder event, or a
        # user creating a folder that shares a name with a real document
        # would nuke that document's output.
        mock_sqs.send_message.assert_not_called()
        mock_doc_service.create_document.assert_not_called()
        mock_from_event.assert_not_called()
        mock_purge.assert_not_called()

    def test_processes_regular_key(self):
        """A normal document key is processed (not skipped)."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            response = index.handler(make_event("doc.pdf"), None)

        assert response["statusCode"] == 200
        assert "skipped" not in response
        mock_doc_service.create_document.assert_called_once()
        mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
class TestReuploadCleanup:
    """Issue #719: a re-upload sharing a filename must purge the previous
    document's output artefacts before the pipeline runs, or the OCR
    function's retry-safe recovery would reinstate stale results."""

    def _run_handler(self, key: str):
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = key
        mock_document.input_key = key
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3") as mock_s3,
            patch.object(index, "delete_current_output_objects") as mock_purge,
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_purge.return_value = 5
            index.handler(make_event(key), None)
            return mock_purge, mock_s3

    def test_purges_stale_output_for_object_key(self):
        """The purge is invoked with the S3 output bucket, object key, and is
        SCOPED to ``pages/`` — that's the only subprefix OCR's retry-safe
        recovery reads, and scoping there makes it impossible for an upload
        of ``foo`` to nuke a nested document at ``foo/bar.pdf/*``."""
        mock_purge, mock_s3 = self._run_handler("test1.pdf")
        mock_purge.assert_called_once_with(
            mock_s3, "test-output-bucket", "test1.pdf", subprefixes=("pages/",)
        )

    def test_purge_failure_does_not_block_processing(self):
        """S3 delete failures are swallowed — the doc still gets queued."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch"),
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("S3 outage"),
            ),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            response = index.handler(make_event("doc.pdf"), None)

        assert response["statusCode"] == 200
        mock_doc_service.create_document.assert_called_once()
        mock_sqs.send_message.assert_called_once()

    def test_purge_failure_emits_alarmable_metric(self):
        """On a purge failure the code MUST emit ``StaleOutputPurgeFailed``
        so an operator can alarm on it — logging alone is not enough
        (log-scraping isn't provisioned by this MR, and the alternative
        would be silent stale extraction, exactly the symptom #719
        exists to prevent)."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("simulated partial-purge S3 error"),
            ),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

        mock_cw.put_metric_data.assert_called_once()
        call = mock_cw.put_metric_data.call_args
        # Lock the exact Namespace so a future refactor that accidentally
        # passes a typo (``idp`` lowercase, ``IDP-Test``, hardcoded stack
        # name, ...) fails this test rather than silently drifting.
        # Test env's mock_env fixture doesn't set METRIC_NAMESPACE, so
        # the code's fallback ``"IDP"`` is what we expect here.
        assert call.kwargs["Namespace"] == "IDP"
        metrics = call.kwargs["MetricData"]
        assert metrics[0]["MetricName"] == "StaleOutputPurgeFailed"
        assert metrics[0]["Value"] == 1
        assert metrics[0]["Unit"] == "Count"

    def test_metric_namespace_is_read_from_module_attribute(self):
        """Regression guard against an env-var rename slipping through:
        the module-level ``METRIC_NAMESPACE`` binds at import time (before
        the autouse mock_env fixture runs), so the standard
        ``test_purge_failure_emits_alarmable_metric`` only ever exercises
        the ``os.environ.get(..., 'IDP')`` fallback branch. If someone
        renames the env var (e.g. ``METRIC_NAMESPACE`` → ``METRICS_NAMESPACE``)
        the code silently keeps hitting the fallback and prod emits
        under the wrong namespace — the alarm never fires.

        This test patches the module attribute directly to a
        stack-name-shaped value and asserts the emit uses it, so the
        template's ``METRIC_NAMESPACE: !Ref StackName`` wiring is
        actually exercised by the assertion path."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "METRIC_NAMESPACE", "idp-dev-qs"),
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("purge failed"),
            ),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

        assert mock_cw.put_metric_data.call_args.kwargs["Namespace"] == "idp-dev-qs"

    def test_metric_emit_failure_is_swallowed(self):
        """Telemetry must not affect document ingest — if PutMetricData
        itself fails (throttled, network blip), the doc still gets
        queued. Belt-and-braces on the metric emit's try/except."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("purge failed"),
            ),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            # Metric emit ALSO fails — doc should still queue.
            mock_cw.put_metric_data.side_effect = RuntimeError("CW throttle")
            response = index.handler(make_event("doc.pdf"), None)

        assert response["statusCode"] == 200
        mock_doc_service.create_document.assert_called_once()
        mock_sqs.send_message.assert_called_once()

    def test_purge_runs_before_create_document(self):
        """Ordering matters: OCR must see the purged prefix, so the purge
        must happen before the tracking record is put and the message enqueued."""
        import index

        mock_document = MagicMock()
        mock_document.config_version = "v1"
        mock_document.id = "doc.pdf"
        mock_document.input_key = "doc.pdf"
        mock_document.to_json.return_value = "{}"

        call_order = []

        def record_purge(*args, **kwargs):
            call_order.append("purge")
            return 3

        def record_create(*args, **kwargs):
            call_order.append("create_document")
            return "doc.pdf"

        def record_send(*args, **kwargs):
            call_order.append("send_message")
            return {"MessageId": "mid"}

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index, "delete_current_output_objects", side_effect=record_purge
            ),
            patch.object(index.Document, "from_s3_event", return_value=mock_document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.create_document.side_effect = record_create
            mock_sqs.send_message.side_effect = record_send
            index.handler(make_event("doc.pdf"), None)

        assert call_order == ["purge", "create_document", "send_message"]
