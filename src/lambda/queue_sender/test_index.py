# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the queue_sender Lambda function."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# The real Status enum, before idp_common is stubbed below. Mocking it would make
# every status assertion here compare one MagicMock attribute against another,
# which passes whatever the handler stores — and what the handler stores IS the
# behaviour under test for the malware gate.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "../../../lib/idp_common_pkg")
)
from idp_common.job_completion import parse_job_document_id  # noqa: E402
from idp_common.models import Status  # noqa: E402

# idp_common and aws_xray_sdk are heavy/AWS-dependent; mock them before import.
sys.modules["idp_common"] = MagicMock()
mock_models_module = MagicMock()
mock_models_module.Status = Status
sys.modules["idp_common.models"] = mock_models_module
sys.modules["idp_common.docs_service"] = MagicMock()
sys.modules["idp_common.document_versions"] = MagicMock()
sys.modules["idp_common.job_service"] = MagicMock()

# `parse_job_document_id` is pure, so the real one is used: a stub would make the
# "is this a /Jobs document" question answer whatever the test says, and getting
# that wrong is how a refused job file hangs its whole batch.
# `finalize_job_file` IS stubbed, so these tests can assert the handler calls it
# with the right arguments; its own behaviour is covered by
# lib/idp_common_pkg/tests/unit/test_job_completion.py.
mock_job_completion = MagicMock()
mock_job_completion.parse_job_document_id = parse_job_document_id
sys.modules["idp_common.job_completion"] = mock_job_completion
sys.modules["idp_common.config"] = MagicMock()
sys.modules["idp_common.config.configuration_manager"] = MagicMock()
# The precedence RULES are exhaustively tested against the real implementation in
# lib/idp_common_pkg/tests/unit/config/test_prefix_mappings.py, over a pure function
# that needs no AWS. What is tested HERE is the wiring: what this Lambda asks for,
# what it does with the answer, and what it emits — which is the half that suite
# cannot see.
sys.modules["idp_common.config.prefix_mappings"] = MagicMock()

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


class FakeAssignment:
    """Stands in for idp_common's frozen ConfigAssignment.

    Written out rather than taken from a MagicMock because every flag on it is a
    branch in the handler, and a MagicMock attribute is *truthy* — which silently
    sends every document down the `rejected` path.
    """

    def __init__(
        self,
        profile="active",
        revision=None,
        source="active-profile",
        mapping_prefix=None,
        conflict=False,
        rejected=False,
        unresolvable=False,
        reason="because",
    ):
        self.profile = profile
        self.revision = revision
        self.source = source
        self.mapping_prefix = mapping_prefix
        self.conflict = conflict
        self.rejected = rejected
        self.unresolvable = unresolvable
        self.scope_denied = False
        self.reason = reason


@pytest.fixture(autouse=True)
def benign_config_resolution(mock_env):
    """Default every test to "no mapping matched", i.e. today's behaviour.

    Tests about ingest mechanics must not have to know about prefix mappings, and a
    test that does care overrides this with its own patch.

    Depends on ``mock_env`` explicitly: importing ``index`` reads QUEUE_URL at module
    scope, so this fixture cannot run before the environment is in place.
    """
    import index

    with patch.object(
        index, "resolve_config_assignment", return_value=FakeAssignment()
    ):
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


# --- Malware-scanning ingest gate --------------------------------------------
#
# Every test below drives `index.handler` rather than the helpers, because the
# property that matters is end-to-end: for a given event and configuration, does
# a document reach SQS or not. A test that called `decide_admission` alone would
# pass while the handler ignored its answer.


def make_scan_result_event(
    key: str = "doc.pdf",
    bucket: str = "test-input-bucket",
    scan_status: str = "COMPLETED",
    result_status: str = "NO_THREATS_FOUND",
    threats=None,
    status_reasons=None,
) -> dict:
    """A GuardDuty 'Object Scan Result' event, shaped as the service sends it."""
    return {
        "detail-type": "GuardDuty Malware Protection Object Scan Result",
        "source": "aws.guardduty",
        "time": "2026-10-11T00:00:00Z",
        "detail": {
            "schemaVersion": "1.0",
            "scanStatus": scan_status,
            "resourceType": "S3_OBJECT",
            "s3ObjectDetails": {
                "bucketName": bucket,
                "objectKey": key,
                "eTag": "etag",
                "versionId": "vid",
                "s3Throttled": False,
            },
            "scanResultDetails": {
                "scanResultStatus": result_status,
                "threats": threats,
                "statusReasons": status_reasons,
            },
        },
    }


class ConditionalCheckFailed(Exception):
    """Stands in for idp_common's DynamoDBError on a lost compare-and-set.

    The handler branches on the ``error_code`` attribute rather than the
    exception class, so this reproduces the shape it actually reads.
    """

    def __init__(self):
        super().__init__("The conditional request failed")
        self.error_code = "ConditionalCheckFailedException"


def _mock_document(key="doc.pdf"):
    doc = MagicMock()
    doc.config_version = "v1"
    doc.config_revision = None
    doc.id = key
    doc.input_key = key
    doc.to_json.return_value = "{}"
    doc.malware_scan_status = None
    doc.malware_scan_result = None
    doc.malware_scan_threat = None
    doc.malware_scan_reasons = []
    doc.malware_scan_time = None
    doc.malware_scan_version_id = None
    return doc


def _row_double(status=Status.MALWARE_SCANNING, version_id="v:vid"):
    """A document as the TRACKING TABLE really returns it.

    ⚠️ Deliberately missing `input_bucket`, `output_bucket`, `config_version` and
    `forced_document_class`, because `_document_to_create_item` does not persist
    them — it writes the key, the status, the timestamps, the submission source
    and the test-set id, and nothing else.

    Returning a fully populated `_mock_document()` here instead would let every
    assertion pass while a row-built verdict path handed the pipeline
    `input_bucket=None` — OCR reading `s3://None/<key>` and failing the document.
    A double richer than the thing it doubles is the blind spot, so this one is
    kept as poor as the real thing.
    """
    doc = MagicMock()
    doc.id = "doc.pdf"
    doc.input_key = "doc.pdf"
    doc.input_bucket = None
    doc.output_bucket = None
    doc.config_version = None
    doc.config_revision = None
    doc.forced_document_class = None
    doc.status = status
    doc.malware_scan_version_id = version_id
    doc.initial_event_time = "2026-10-11T09:00:00Z"
    doc.queued_time = "2026-10-11T09:00:01Z"
    doc.trace_id = "1-upload-trace"
    doc.submission_source = None
    doc.test_set_id = None
    return doc


@pytest.mark.unit
class TestGateIsOffByDefault:
    """With MalwareScanning DISABLED nothing about ingest changes.

    This is the property the whole feature's safety rests on: every existing
    deployment keeps today's behaviour byte for byte, so the gate cannot break an
    ingest path by being merged.
    """

    def test_upload_is_queued_immediately(self):
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            response = index.handler(make_event("doc.pdf"), None)

        assert doc.status == Status.QUEUED
        assert "held_for_malware_scan" not in response
        mock_doc_service.create_document.assert_called_once()
        mock_sqs.send_message.assert_called_once()

    def test_a_verdict_arriving_anyway_is_refused_not_acted_on(self):
        """A stale rule, or a stack switched to DISABLED mid-flight.

        Admitting on the strength of it would be acting on a gate this stack no
        longer claims to run, and the document row would move without anyone
        having asked for scanning.
        """
        import index

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
        ):
            response = index.handler(make_scan_result_event(), None)

        assert response["skipped"] == "scanning_disabled"
        mock_sqs.send_message.assert_not_called()
        mock_doc_service.update_document_status.assert_not_called()


@pytest.mark.unit
class TestGateHoldsTheUpload:
    """With scanning enabled an upload is recorded but NOT enqueued."""

    def _run(self, key="doc.pdf"):
        import index

        doc = _mock_document(key)
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "MALWARE_SCANNING_MODE", "CREATE_PLAN"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            response = index.handler(make_event(key), None)
        return response, doc, mock_sqs, mock_doc_service

    def test_document_is_created_in_malware_scanning(self):
        response, doc, _, mock_doc_service = self._run()
        assert doc.status == Status.MALWARE_SCANNING
        assert response["held_for_malware_scan"] is True
        mock_doc_service.create_document.assert_called_once()

    def test_nothing_is_enqueued(self):
        """The whole point: no SQS message means no Step Functions execution, so
        an infected file's bytes never reach OCR, image rendering or Bedrock."""
        _, _, mock_sqs, _ = self._run()
        mock_sqs.send_message.assert_not_called()

    def test_the_stale_output_purge_still_runs_on_upload(self):
        """#719 is about the key's previous occupant, which this upload has
        already replaced — so it must happen now, not when the verdict lands."""
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3") as mock_s3,
            patch.object(index, "delete_current_output_objects") as mock_purge,
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_purge.return_value = 2
            index.handler(make_event("doc.pdf"), None)

        mock_purge.assert_called_once_with(
            mock_s3, "test-output-bucket", "doc.pdf", subprefixes=("pages/",)
        )


@pytest.mark.unit
class TestValidationObjectIsNotADocument:
    """GuardDuty PUTs a probe object into the protected bucket to prove it can
    write. It arrives as an ordinary Object Created event, and without a skip it
    becomes a phantom document in the UI — one per stack, and another every time
    the plan revalidates."""

    def test_upload_of_the_probe_object_is_skipped(self):
        import index

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "delete_current_output_objects") as mock_purge,
            patch.object(index.Document, "from_s3_event") as mock_from_event,
        ):
            response = index.handler(
                make_event("malware-protection-resource-validation-object"), None
            )

        assert response["skipped"] == "malware_validation_object"
        mock_sqs.send_message.assert_not_called()
        mock_doc_service.create_document.assert_not_called()
        mock_from_event.assert_not_called()
        mock_purge.assert_not_called()

    def test_a_prefixed_probe_object_is_also_skipped(self):
        """A plan scoped to object prefixes writes the probe under each prefix,
        so matching only the bucket-root form would reintroduce the phantom."""
        import index

        with (
            patch.object(index, "sqs"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index.Document, "from_s3_event"),
        ):
            response = index.handler(
                make_event("incoming/malware-protection-resource-validation-object"),
                None,
            )

        assert response["skipped"] == "malware_validation_object"
        mock_doc_service.create_document.assert_not_called()

    def test_the_skip_does_not_depend_on_scanning_being_enabled(self):
        """In GATE_ONLY the plan is managed outside this stack and can be
        attached at any time, so the probe can arrive while this stack's own
        parameter still reads DISABLED."""
        import index

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index.Document, "from_s3_event"),
        ):
            response = index.handler(
                make_event("malware-protection-resource-validation-object"), None
            )

        assert response["skipped"] == "malware_validation_object"
        mock_doc_service.create_document.assert_not_called()

    def test_a_verdict_for_the_probe_object_is_ignored(self):
        import index

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "document_service") as mock_doc_service,
        ):
            response = index.handler(
                make_scan_result_event(
                    key="malware-protection-resource-validation-object"
                ),
                None,
            )

        assert response["skipped"] == "malware_validation_object"
        mock_doc_service.update_document_status.assert_not_called()


@pytest.mark.unit
class TestAdmissionPolicy:
    """`decide_admission` is the policy, and it DEFAULT-DENIES.

    Driven directly because it is pure; the handler's use of its answer is
    covered by TestVerdictHandling below.
    """

    def test_a_completed_clean_scan_admits(self):
        import index

        assert index.decide_admission("COMPLETED", "NO_THREATS_FOUND", False) == (
            Status.QUEUED,
            "clean",
        )

    def test_threats_found_quarantines_regardless_of_the_unscannable_policy(self):
        """`allow_unscannable` must not be readable as 'allow anything'."""
        import index

        for allow in (False, True):
            assert index.decide_admission("COMPLETED", "THREATS_FOUND", allow) == (
                Status.QUARANTINED,
                "threats_found",
            )

    @pytest.mark.parametrize(
        "scan_status,result_status",
        [
            ("SKIPPED", "UNSUPPORTED"),
            ("SKIPPED", "ACCESS_DENIED"),
            ("FAILED", "FAILED"),
            # Unrecognised values, which is what a future GuardDuty API addition
            # or a malformed event looks like from here.
            ("COMPLETED", ""),
            ("", ""),
            ("COMPLETED", "SOME_FUTURE_STATUS"),
            # The documented pair disagreeing: a payload this code does not
            # understand, so it must not be read as clean.
            ("SKIPPED", "NO_THREATS_FOUND"),
        ],
    )
    def test_everything_else_is_blocked_when_failing_closed(
        self, scan_status, result_status
    ):
        import index

        status, reason = index.decide_admission(scan_status, result_status, False)
        assert status == Status.MALWARE_SCAN_FAILED
        assert reason == "unscannable_blocked"

    @pytest.mark.parametrize(
        "scan_status,result_status",
        [("SKIPPED", "UNSUPPORTED"), ("FAILED", "FAILED"), ("COMPLETED", "")],
    )
    def test_the_same_cases_are_admitted_when_failing_open(
        self, scan_status, result_status
    ):
        import index

        status, reason = index.decide_admission(scan_status, result_status, True)
        assert status == Status.QUEUED
        assert reason == "unscannable_allowed"


@pytest.mark.unit
class TestTheUnscannableCeiling:
    """The config policy is Admin-editable from the web UI; the stack parameter
    is not. So `on_unscannable: allow` must be unable to admit anything on its
    own, or the posture is one UI edit deep."""

    def _allow(self, ceiling, config_value):
        import index

        fake_config = {"malware_scanning": {"on_unscannable": config_value}}
        with (
            patch.object(index, "ALLOW_UNSCANNABLE_CEILING", ceiling),
            patch.dict(
                sys.modules,
                {"idp_common.config": MagicMock(get_config=lambda **kw: fake_config)},
            ),
        ):
            return index.malware_policy_for()["allow_unscannable"]

    def test_config_allow_with_the_ceiling_down_is_refused(self):
        assert self._allow(ceiling=False, config_value="allow") is False

    def test_config_allow_with_the_ceiling_up_is_honoured(self):
        assert self._allow(ceiling=True, config_value="allow") is True

    def test_config_block_with_the_ceiling_up_still_blocks(self):
        assert self._allow(ceiling=True, config_value="block") is False

    def test_an_absent_config_section_blocks(self):
        import index

        with (
            patch.object(index, "ALLOW_UNSCANNABLE_CEILING", True),
            patch.dict(
                sys.modules,
                {"idp_common.config": MagicMock(get_config=lambda **kw: {})},
            ),
        ):
            assert index.malware_policy_for()["allow_unscannable"] is False

    def test_an_unreadable_config_blocks(self):
        """An unreadable policy is not a permissive one."""
        import index

        def boom(**kwargs):
            raise RuntimeError("DynamoDB unavailable")

        with (
            patch.object(index, "ALLOW_UNSCANNABLE_CEILING", True),
            patch.dict(sys.modules, {"idp_common.config": MagicMock(get_config=boom)}),
        ):
            assert index.malware_policy_for()["allow_unscannable"] is False


@pytest.mark.unit
class TestVerdictHandling:
    """What the handler does with each verdict, end to end."""

    def _run(self, event, allow_unscannable=False, key="doc.pdf"):
        import index

        doc = _mock_document(key)
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns") as mock_sns,
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={
                    "allow_unscannable": allow_unscannable,
                    "alert_on_block": True,
                },
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = _row_double()
            response = index.handler(event, None)
        return response, doc, mock_sqs, mock_sns, mock_cw, mock_doc_service

    def test_a_clean_verdict_enqueues_the_document(self):
        response, doc, mock_sqs, _, mock_cw, mock_doc_service = self._run(
            make_scan_result_event()
        )

        assert response["queued"] is True
        assert response["malware_verdict"] == "clean"
        assert doc.status == Status.QUEUED
        mock_sqs.send_message.assert_called_once()
        mock_doc_service.update_document_status.assert_called_once()
        assert "MalwareScanClean" in str(mock_cw.put_metric_data.call_args_list)

    def test_the_transition_is_a_compare_and_set(self):
        """GuardDuty delivers at least once, so an unconditional write would let
        a duplicate verdict enqueue the same document twice."""
        _, _, _, _, _, mock_doc_service = self._run(make_scan_result_event())

        kwargs = mock_doc_service.update_document_status.call_args.kwargs
        assert kwargs["condition_expression"] == (
            "attribute_exists(PK) AND #ObjectStatus = :scanning "
            "AND #MalwareScanVersionId = :verdict_version"
        )
        assert kwargs["extra_attributes"][":scanning"] == "MALWARE_SCANNING"

    def test_the_condition_also_pins_the_object_version(self):
        """The status clause alone would let a clean verdict for one upload admit
        a different one — see TestSameKeyReuploadRace below."""
        _, _, _, _, _, mock_doc_service = self._run(make_scan_result_event())

        kwargs = mock_doc_service.update_document_status.call_args.kwargs
        assert (
            "#MalwareScanVersionId = :verdict_version" in kwargs["condition_expression"]
        )
        # "vid" is what make_scan_result_event puts in s3ObjectDetails.versionId.
        assert kwargs["extra_attributes"][":verdict_version"] == "v:vid"
        # SET as well as compared, because an expression attribute NAME has to be
        # registered by the SET clause; referencing it only in the condition makes
        # DynamoDB reject the request as malformed.
        assert kwargs["extra_attributes"]["MalwareScanVersionId"] == "v:vid"

    def test_the_verdict_and_the_status_land_in_one_write(self):
        """Two writes would leave a window where the UI shows QUARANTINED with no
        threat name to display."""
        _, _, _, _, _, mock_doc_service = self._run(
            make_scan_result_event(
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
        )

        assert mock_doc_service.update_document_status.call_count == 1
        extra = mock_doc_service.update_document_status.call_args.kwargs[
            "extra_attributes"
        ]
        assert extra["MalwareScanResult"] == "THREATS_FOUND"
        assert extra["MalwareScanThreat"] == "EICAR-Test-File (not a virus)"
        assert extra["MalwareScanStatus"] == "COMPLETED"
        assert extra["MalwareScanTime"] == "2026-10-11T00:00:00Z"

    def test_the_clean_verdict_is_recorded_too(self):
        """ "The scan ran and found nothing" is the observation an auditor needs,
        and without it an absent verdict is ambiguous between clean and never
        scanned."""
        _, doc, _, _, _, mock_doc_service = self._run(make_scan_result_event())

        extra = mock_doc_service.update_document_status.call_args.kwargs[
            "extra_attributes"
        ]
        assert extra["MalwareScanResult"] == "NO_THREATS_FOUND"
        assert doc.malware_scan_result == "NO_THREATS_FOUND"

    def test_threats_found_quarantines_and_does_not_enqueue(self):
        response, doc, mock_sqs, mock_sns, mock_cw, _ = self._run(
            make_scan_result_event(
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
        )

        assert response["queued"] is False
        assert doc.status == Status.QUARANTINED
        mock_sqs.send_message.assert_not_called()
        mock_sns.publish.assert_called_once()
        assert "MalwareScanThreatsFound" in str(mock_cw.put_metric_data.call_args_list)

    def test_the_alert_names_the_threat_and_the_object(self):
        """An operator reading the alert must not have to go and find out what
        was detected or where it is."""
        _, _, _, mock_sns, _, _ = self._run(
            make_scan_result_event(
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "EICAR-Test-File (not a virus)" in message
        assert "s3://test-input-bucket/doc.pdf" in message
        assert "QUARANTINED" in message

    def test_an_unscannable_document_is_blocked_by_default(self):
        response, doc, mock_sqs, mock_sns, mock_cw, _ = self._run(
            make_scan_result_event(
                scan_status="SKIPPED",
                result_status="UNSUPPORTED",
                status_reasons=["PASSWORD_PROTECTED"],
            )
        )

        assert doc.status == Status.MALWARE_SCAN_FAILED
        assert response["malware_verdict"] == "unscannable_blocked"
        mock_sqs.send_message.assert_not_called()
        mock_sns.publish.assert_called_once()
        assert "PASSWORD_PROTECTED" in mock_sns.publish.call_args.kwargs["Message"]
        assert "MalwareScanUnscannableBlocked" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_an_unscannable_document_is_admitted_when_allowed_and_still_alerts(self):
        """Admitted under an explicit policy, and still worth saying out loud: an
        unscanned document went into the pipeline."""
        response, doc, mock_sqs, mock_sns, mock_cw, _ = self._run(
            make_scan_result_event(
                scan_status="SKIPPED",
                result_status="UNSUPPORTED",
                status_reasons=["PASSWORD_PROTECTED"],
            ),
            allow_unscannable=True,
        )

        assert doc.status == Status.QUEUED
        assert response["malware_verdict"] == "unscannable_allowed"
        mock_sqs.send_message.assert_called_once()
        mock_sns.publish.assert_called_once()
        assert "ADMITTED" in mock_sns.publish.call_args.kwargs["Message"]
        assert "MalwareScanUnscannableAllowed" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_verdict_for_another_bucket_is_ignored(self):
        response, _, mock_sqs, _, _, mock_doc_service = self._run(
            make_scan_result_event(bucket="someone-elses-bucket")
        )

        assert response["skipped"] == "other_bucket"
        mock_sqs.send_message.assert_not_called()
        mock_doc_service.update_document_status.assert_not_called()

    def test_a_verdict_with_no_object_details_is_ignored(self):
        import index

        event = make_scan_result_event()
        event["detail"]["s3ObjectDetails"] = {}
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "document_service") as mock_doc_service,
        ):
            response = index.handler(event, None)

        assert response["skipped"] == "malformed_scan_result"
        mock_doc_service.update_document_status.assert_not_called()


@pytest.mark.unit
class TestVerdictDeliveredMoreThanOnce:
    """GuardDuty's at-least-once delivery, and the upload/verdict race."""

    def _run_with_condition_failure(self, existing_status, key="doc.pdf"):
        """The row exists and is no longer MALWARE_SCANNING: a duplicate verdict."""
        import index

        doc = _mock_document(key)
        doc.status = existing_status

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns") as mock_sns,
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            mock_doc_service.get_document.return_value = doc
            response = index.handler(make_scan_result_event(key=key), None)
        return response, mock_sqs, mock_sns, mock_cw

    def _expect_raise(self, key="doc.pdf"):
        """Same setup, but the row is ABSENT so the handler must raise.

        A separate helper rather than a try/except in the one above: a helper
        that returns either a response or an exception makes every caller
        re-discover which it got, and a caller that forgot to check would assert
        nothing.
        """
        import index

        doc = _mock_document(key)
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            mock_doc_service.get_document.return_value = None
            with pytest.raises(index.ScanVerdictBeforeIngestError) as caught:
                index.handler(make_scan_result_event(key=key), None)
        return caught.value, mock_sqs, mock_cw

    def test_a_duplicate_verdict_does_not_enqueue_twice(self):
        """The document is already QUEUED, so this delivery must do nothing."""
        response, mock_sqs, mock_sns, mock_cw = self._run_with_condition_failure(
            Status.QUEUED
        )

        assert response["duplicate"] is True
        mock_sqs.send_message.assert_not_called()
        mock_sns.publish.assert_not_called()
        assert "MalwareScanVerdictDuplicate" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_verdict_for_an_already_quarantined_document_does_nothing(self):
        response, mock_sqs, mock_sns, _ = self._run_with_condition_failure(
            Status.QUARANTINED
        )

        assert response["duplicate"] is True
        mock_sqs.send_message.assert_not_called()
        # No second alert: one detection, one notification.
        mock_sns.publish.assert_not_called()

    def test_a_verdict_arriving_before_the_upload_event_raises_for_retry(self):
        """Creating the row here instead would race the upload path into
        overwriting a terminal status with MALWARE_SCANNING, hanging the document
        for good. Raising hands it to the rule's RetryPolicy."""
        error, mock_sqs, mock_cw = self._expect_raise()

        assert "before its document row existed" in str(error)
        mock_sqs.send_message.assert_not_called()
        assert "MalwareScanVerdictBeforeIngest" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_non_condition_error_is_not_swallowed(self):
        """Only a lost compare-and-set is interpreted. A throttle or an IAM
        failure must surface, or a document silently never gets a verdict."""
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.update_document_status.side_effect = RuntimeError(
                "ProvisionedThroughputExceededException"
            )
            with pytest.raises(RuntimeError, match="ProvisionedThroughput"):
                index.handler(make_scan_result_event(), None)


@pytest.mark.unit
class TestSqsFailureAfterAdmission:
    """The one window where a document could stall with no workflow and no
    message: the status is already QUEUED but the send failed, so a retry takes
    the duplicate path and never enqueues it."""

    def test_the_status_is_reverted_so_the_retry_can_enqueue(self):
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_sqs.send_message.side_effect = RuntimeError("SQS unavailable")
            with pytest.raises(RuntimeError, match="SQS unavailable"):
                index.handler(make_scan_result_event(), None)

            calls = mock_doc_service.update_document_status.call_args_list

        # Two writes: the admission, then the revert.
        assert len(calls) == 2
        assert calls[0].args[1] == Status.QUEUED
        assert calls[1].args[1] == Status.MALWARE_SCANNING
        # ⚠️ The revert is CONDITIONAL on the row still being this invocation's
        # admission (status QUEUED, scan time this verdict's). If the SQS send
        # actually reached the service and only the response was lost, a retry
        # re-admits and sends a second message — an unconditional revert would
        # then re-open the gate with a message already in flight, so the
        # admission would be delivered twice. The condition's refusal is the
        # signal to leave the row alone and let the retry's own compare-and-set
        # decide.
        revert_condition = calls[1].kwargs["condition_expression"]
        assert "#ObjectStatus = :queued" in revert_condition
        assert "#MalwareScanTime = :verdict_time" in revert_condition
        assert calls[1].kwargs["extra_attributes"][":queued"] == "QUEUED"


@pytest.mark.unit
class TestRefusedJobsApiDocument:
    """A /Jobs API batch must not hang because one of its files was refused.

    job_tracker is driven by Step Functions execution events, and a refused
    document never starts an execution — so without this the Files map keeps the
    file non-terminal, no results.zip is written, and GET /jobs/<id> answers
    IN_PROGRESS indefinitely.
    """

    def _run(
        self,
        key,
        result_status="THREATS_FOUND",
        job_complete=True,
        job_tracker_name="idp-JobTracker",
    ):
        import index

        doc = _mock_document(key)
        mock_job_completion.finalize_job_file.reset_mock()
        mock_job_completion.finalize_job_file.return_value = {
            "found": True,
            "files": {},
            "job_complete": job_complete,
            "results_ready": False,
            "results_deferred": job_complete,
        }
        self.lambda_client = MagicMock()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(index, "job_service", MagicMock()),
            patch.object(index, "lambda_client", self.lambda_client),
            patch.dict(
                index.os.environ,
                {"JOB_TRACKER_FUNCTION_NAME": job_tracker_name},
                clear=False,
            ),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = doc
            index.handler(
                make_scan_result_event(
                    key=key,
                    result_status=result_status,
                    threats=[{"name": "EICAR-Test-File (not a virus)"}],
                ),
                None,
            )
        return mock_job_completion.finalize_job_file

    def test_a_quarantined_job_file_is_marked_terminal(self):
        finalize = self._run("jobs/job-123/scan.pdf")

        finalize.assert_called_once()
        kwargs = finalize.call_args.kwargs
        assert kwargs["job_id"] == "job-123"
        assert kwargs["filename"] == "scan.pdf"

    def test_the_files_map_gets_FAILED_not_the_malware_status(self):
        """Three separate copies of the terminal-status set read that map
        (job_tracker, api_handler.compute_job_status, the job list resolver), so
        a value none of them knows would hang the job in a new way. The precise
        reason stays on the document row."""
        finalize = self._run("jobs/job-123/scan.pdf")

        assert finalize.call_args.kwargs["status"] == Status.FAILED

    def test_an_unscannable_job_file_is_also_finalized(self):
        finalize = self._run("jobs/job-123/locked.pdf", result_status="UNSUPPORTED")

        finalize.assert_called_once()
        assert finalize.call_args.kwargs["status"] == Status.FAILED

    def test_an_ordinary_upload_does_not_touch_the_job_table(self):
        finalize = self._run("not-a-job/doc.pdf")

        finalize.assert_not_called()

    def test_a_clean_job_file_is_left_to_job_tracker(self):
        """It runs a workflow, so the execution-status-change event is what
        finalizes it. Doing it here as well would mark it FAILED before it ran."""
        finalize = self._run("jobs/job-123/clean.pdf", result_status="NO_THREATS_FOUND")

        finalize.assert_not_called()

    def test_the_gate_does_not_build_the_results_zip_itself(self):
        """The IAM and the sizing both say it cannot.

        `QueueSender` is 128 MB with a 30 s timeout and holds no get/put on the
        output bucket — only ListBucket and DeleteObject for the #719 purge —
        while `create_results_zip` reads every output object and writes the zip.
        Calling it here returned AccessDenied, which the caller swallows, so the
        job never got `ResultsReady` and `GET /jobs/<id>` answered IN_PROGRESS
        for ever: the exact hang this path exists to prevent.
        """
        finalize = self._run("jobs/job-123/scan.pdf")

        assert finalize.call_args.kwargs["build_results"] is False

    def test_a_refusal_that_completes_the_batch_delegates_the_zip(self):
        """The ordering that hangs, and the only one that needs this.

        `job_tracker` fires on Step Functions state changes, so when the refused
        file is the LAST to go terminal there is no further event and nothing
        would ever build the zip. The gate therefore hands the job to the one
        function that is resourced and granted for it.
        """
        self._run("jobs/job-123/scan.pdf", job_complete=True)

        self.lambda_client.invoke.assert_called_once()
        kwargs = self.lambda_client.invoke.call_args.kwargs
        assert kwargs["FunctionName"] == "idp-JobTracker"
        # Asynchronous: the gate must not wait on a 900 s zip inside its own 30 s
        # timeout, and it has nothing to do with the answer.
        assert kwargs["InvocationType"] == "Event"
        assert json.loads(kwargs["Payload"]) == {
            "idpJobCompletion": {"jobId": "job-123"}
        }

    def test_a_refusal_that_leaves_the_batch_running_delegates_nothing(self):
        """Other files are still in flight, so one of their execution-status
        events will complete the job by the normal route. Delegating here would
        zip a partial result and mark it ready."""
        self._run("jobs/job-123/scan.pdf", job_complete=False)

        self.lambda_client.invoke.assert_not_called()

    def test_a_missing_job_tracker_name_is_reported_not_crashed(self):
        """Expected only on a misconfiguration: the variable is conditional on
        the same `DeployApiGateway` as `JobTracker`, and without the Jobs API a
        `jobs/` document cannot exist. The document's own status is already
        written, so the refusal still held — what is lost is the job's zip, and
        it is metered rather than raised."""
        self._run("jobs/job-123/scan.pdf", job_tracker_name="")

        self.lambda_client.invoke.assert_not_called()

    def test_a_failed_delegation_does_not_break_the_refusal(self):
        """The refusal is the security-relevant half and is already persisted.
        Raising here would re-run the verdict handler, which would take the
        duplicate path and not retry this, so it would cost an invocation and
        change nothing."""
        import index

        self.lambda_client = MagicMock()
        self.lambda_client.invoke.side_effect = RuntimeError("TooManyRequests")
        with (
            patch.object(index, "lambda_client", self.lambda_client),
            patch.object(index, "cloudwatch"),
            patch.dict(
                index.os.environ,
                {"JOB_TRACKER_FUNCTION_NAME": "idp-JobTracker"},
                clear=False,
            ),
        ):
            # Returns None rather than raising; that is the whole assertion.
            assert index._delegate_job_results("job-123") is None


@pytest.mark.unit
class TestEventDispatch:
    """Two event types reach this function, and a third must not be guessed at."""

    def test_an_event_with_no_detail_type_is_treated_as_an_upload(self):
        """The only event this function had for its whole life, and some callers
        build the event by hand."""
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

        mock_sqs.send_message.assert_called_once()

    def test_an_unexpected_detail_type_is_reported_and_ignored(self):
        """Raising would retry forever against an event we will never
        understand."""
        import index

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
        ):
            response = index.handler(
                {"detail-type": "Object Deleted", "detail": {}}, None
            )

        assert response["skipped"] == "unexpected_detail_type"
        mock_sqs.send_message.assert_not_called()
        mock_doc_service.create_document.assert_not_called()


@pytest.mark.unit
class TestAlertOnBlockIsHonoured:
    """`malware_scanning.alert_on_block` is a declared config setting, so it has
    to be consulted. A setting the Visual Editor offers and the code ignores is
    worse than no setting: it reads as control nobody has."""

    def _run(self, alert_on_block, result_status="THREATS_FOUND"):
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns") as mock_sns,
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "ALERTS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={
                    "allow_unscannable": result_status == "UNSUPPORTED",
                    "alert_on_block": alert_on_block,
                },
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = doc
            index.handler(
                make_scan_result_event(
                    result_status=result_status,
                    threats=[{"name": "EICAR-Test-File (not a virus)"}],
                ),
                None,
            )
        return mock_sns, mock_cw

    def test_a_detection_alerts_when_enabled(self):
        mock_sns, _ = self._run(alert_on_block=True)
        mock_sns.publish.assert_called_once()

    def test_a_detection_does_not_alert_when_disabled(self):
        mock_sns, _ = self._run(alert_on_block=False)
        mock_sns.publish.assert_not_called()

    def test_the_metric_is_emitted_either_way(self):
        """Turning alerts off must not turn off the record. The metric is what
        the CloudWatch alarm and the dashboard widget read, and the document's own
        status is unaffected — silencing a notification is not the same as
        deciding nothing happened."""
        _, mock_cw = self._run(alert_on_block=False)
        assert "MalwareScanThreatsFound" in str(mock_cw.put_metric_data.call_args_list)

    def test_an_admitted_unscannable_document_respects_it_too(self):
        mock_sns, _ = self._run(alert_on_block=False, result_status="UNSUPPORTED")
        mock_sns.publish.assert_not_called()


@pytest.mark.unit
class TestThePolicyDefaults:
    """What the gate does when the configuration says nothing, or cannot be read."""

    def test_alerting_defaults_on(self):
        """The safe failure for a notification is an extra message, not a
        missing one — the opposite direction from the admission question."""
        import index

        with patch.dict(
            sys.modules,
            {"idp_common.config": MagicMock(get_config=lambda **kw: {})},
        ):
            assert index.malware_policy_for()["alert_on_block"] is True

    def test_an_unreadable_config_still_alerts(self):
        import index

        def boom(**kwargs):
            raise RuntimeError("DynamoDB unavailable")

        with patch.dict(sys.modules, {"idp_common.config": MagicMock(get_config=boom)}):
            policy = index.malware_policy_for()

        assert policy["alert_on_block"] is True
        assert policy["allow_unscannable"] is False

    def test_only_an_explicit_false_turns_alerting_off(self):
        """The schema default is true, so an absent key must not read as off."""
        import index

        for value, expected in (
            (False, False),
            (True, True),
            # Anything that is not the boolean False leaves alerting on, so a
            # string "false" written by hand does not silently disable it.
            ("false", True),
            (None, True),
        ):
            with patch.dict(
                sys.modules,
                {
                    "idp_common.config": MagicMock(
                        get_config=lambda **kw: {
                            "malware_scanning": {"alert_on_block": value}
                        }
                    )
                },
            ):
                assert index.malware_policy_for()["alert_on_block"] is expected


@pytest.mark.unit
class TestSameKeyReuploadRace:
    """A clean verdict for one upload must not admit a different one.

    The hazard, in order: `report.pdf` v1 is uploaded and the row goes
    MALWARE_SCANNING; `report.pdf` v2 (infected) is uploaded before v1's scan
    finishes, resetting the row to MALWARE_SCANNING; v1's CLEAN verdict arrives.
    On a status-only condition that verdict satisfies the gate, the pipeline reads
    the bucket's *current* version — v2, unscanned — and v2's own THREATS_FOUND
    verdict arrives later to find the row already admitted and is discarded as a
    duplicate.

    Same-key re-upload is not hypothetical here: it is the scenario issue #719
    exists for, so the code already assumes people do it. The window is however
    long the scan takes.

    ⚠️ **What these tests do and do not prove.** The cases below simulate
    DynamoDB's refusal (`ConditionalCheckFailed`) rather than causing it, so they
    pin what the handler *does* with a refused write — discard the verdict, meter
    it as superseded, admit nothing. They do **not** prove that DynamoDB would
    refuse it; removing the version clause from the condition leaves them green.
    What pins the clause itself is
    `TestVerdictHandling::test_the_condition_also_pins_the_object_version`, and
    what proves the two work together against the real service is the integration
    test `test_a_reupload_mid_scan_is_not_admitted_by_the_earlier_verdict`.
    All three are needed; none is redundant.
    """

    def test_the_upload_records_which_version_it_is_waiting_on(self):
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            event = make_event("report.pdf")
            event["detail"]["object"]["version-id"] = "v1"
            index.handler(event, None)

        assert doc.malware_scan_version_id == "v:v1"

    def test_nothing_is_recorded_when_scanning_is_disabled(self):
        """No gate, no discriminator — the field stays off the item so a DISABLED
        deployment writes what it always wrote."""
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            event = make_event("report.pdf")
            event["detail"]["object"]["version-id"] = "v1"
            index.handler(event, None)

        assert doc.malware_scan_version_id is None

    def _verdict_against_stored_version(self, verdict_version, stored_version):
        """A verdict for `verdict_version` against a row waiting on `stored_version`."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.MALWARE_SCANNING
            d.malware_scan_version_id = stored_version
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns") as mock_sns,
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index, "job_service", MagicMock()),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            # A new object per call: the handler mutates the document it loaded,
            # and DynamoDB would not hand back that mutation on a re-read.
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            # DynamoDB refuses the write: this is what the real condition does
            # when the stored version is not the verdict's.
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            event = make_scan_result_event(key="report.pdf")
            event["detail"]["s3ObjectDetails"]["versionId"] = verdict_version
            response = index.handler(event, None)
        return response, mock_sqs, mock_sns, mock_cw

    def test_a_superseded_clean_verdict_does_not_admit_the_newer_upload(self):
        """The defect this class exists for. v1 came back clean; the bucket now
        holds v2, which nothing has scanned."""
        response, mock_sqs, _, mock_cw = self._verdict_against_stored_version(
            verdict_version="v1", stored_version="v:v2"
        )

        assert response["superseded"] is True
        mock_sqs.send_message.assert_not_called()
        assert "MalwareScanVerdictSuperseded" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_superseded_verdict_is_not_reported_as_a_duplicate(self):
        """They are different events and call for different responses: a
        duplicate has already been acted on, a superseded one never will be. One
        metric for both would make the race invisible."""
        _, _, _, mock_cw = self._verdict_against_stored_version(
            verdict_version="v1", stored_version="v:v2"
        )

        assert "MalwareScanVerdictDuplicate" not in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_superseded_refusal_does_not_quarantine_the_newer_upload_either(self):
        """The mirror case, and the reason this is not just a safety property: a
        THREATS_FOUND verdict for a version since replaced by a clean file must
        not quarantine the clean one."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.MALWARE_SCANNING
            d.malware_scan_version_id = "v:v2"
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns") as mock_sns,
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index, "job_service", MagicMock()),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            event = make_scan_result_event(
                key="report.pdf",
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            response = index.handler(event, None)

        assert response["superseded"] is True
        # No alert either: nothing has been decided about this document yet.
        mock_sns.publish.assert_not_called()

    def test_a_matching_verdict_is_still_applied(self):
        """The guard must not refuse the ordinary case. Here the row is waiting on
        exactly the version the verdict describes, so the real DynamoDB condition
        would hold — modelled by the write succeeding."""
        import index

        doc = _mock_document()
        doc.status = Status.MALWARE_SCANNING
        doc.malware_scan_version_id = "v:v1"

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = doc
            event = make_scan_result_event(key="report.pdf")
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            response = index.handler(event, None)

        assert response["queued"] is True
        mock_sqs.send_message.assert_called_once()

    def test_a_duplicate_is_still_reported_as_a_duplicate(self):
        """The row has LEFT MALWARE_SCANNING, so the version clause is not what
        failed — this must not be misread as a supersession."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.QUEUED
            d.malware_scan_version_id = "v:v1"
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            event = make_scan_result_event(key="report.pdf")
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            response = index.handler(event, None)

        assert response["duplicate"] is True
        assert "MalwareScanVerdictDuplicate" in str(
            mock_cw.put_metric_data.call_args_list
        )

    def test_a_refusing_verdict_on_a_row_that_left_the_gate_is_applied_not_dropped(
        self,
    ):
        """The defect this closes, and it was reachable by two UI clicks.

        Reprocess during the scan window built a fresh QUEUED document and started
        the workflow, so the file was already in OCR and Bedrock. The verdict then
        found the row no longer MALWARE_SCANNING, its compare-and-set failed, and
        the classifier had only three cases — before-ingest, superseded,
        duplicate — so a THREATS_FOUND landed in the duplicate branch: logged at
        INFO, counted as at-least-once redelivery, and discarded. The document
        never became QUARANTINED and `alert_on_block` never fired.

        Reprocess now refuses that status, but this branch is the backstop: the
        refusal must not be the only thing between a detection and a silent drop.
        """
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.OCR  # mid-pipeline: it left the gate unscanned
            d.malware_scan_version_id = "v:v1"
            return d

        applied = []
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()

            calls = {"n": 0}

            def update(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConditionalCheckFailed()
                applied.append((args, kwargs))

            mock_doc_service.update_document_status.side_effect = update
            event = make_scan_result_event(
                key="report.pdf",
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            response = index.handler(event, None)

        assert response.get("applied_late") is True, response
        assert not response.get("duplicate")
        # The refusal reached the row.
        assert applied, "the refusal was not written"
        assert applied[0][0][1] == Status.QUARANTINED
        # Its own metric, NOT the duplicate one: counting them together is what
        # made the original defect invisible.
        emitted = str(mock_cw.put_metric_data.call_args_list)
        assert "MalwareScanVerdictUnappliedRefusal" in emitted
        assert "MalwareScanVerdictDuplicate" not in emitted

    def test_the_verdict_attributes_go_with_the_late_refusal(self):
        """A QUARANTINED row with no threat name is not actionable, and this path
        writes unconditionally so it cannot reuse the main path's attributes."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.OCR
            d.malware_scan_version_id = "v:v1"
            return d

        applied = []
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            calls = {"n": 0}

            def update(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConditionalCheckFailed()
                applied.append(kwargs)

            mock_doc_service.update_document_status.side_effect = update
            event = make_scan_result_event(
                key="report.pdf",
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            index.handler(event, None)

        attrs = applied[0]["extra_attributes"]
        assert attrs["MalwareScanResult"] == "THREATS_FOUND"
        assert attrs["MalwareScanThreat"] == "EICAR-Test-File (not a virus)"
        # No condition values on an unconditional write.
        assert not [k for k in attrs if k.startswith(":")]

    def test_an_already_refused_row_is_still_a_duplicate(self):
        """The redelivery case the new branch must not swallow: the row already
        carries this refusal, so re-applying it and re-alerting would turn every
        at-least-once redelivery into a second alert."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.QUARANTINED
            d.malware_scan_version_id = "v:v1"
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            event = make_scan_result_event(
                key="report.pdf",
                result_status="THREATS_FOUND",
                threats=[{"name": "EICAR-Test-File (not a virus)"}],
            )
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            response = index.handler(event, None)

        assert response["duplicate"] is True
        emitted = str(mock_cw.put_metric_data.call_args_list)
        assert "MalwareScanVerdictDuplicate" in emitted
        assert "MalwareScanVerdictUnappliedRefusal" not in emitted


@pytest.mark.unit
class TestTheVersionDiscriminator:
    """`object_version_of` has to read two different event vocabularies and give
    the same answer for the same object, or every document stalls."""

    def test_it_reads_the_s3_event_spelling(self):
        import index

        assert index.object_version_of({"key": "a.pdf", "version-id": "v1"}) == "v:v1"

    def test_it_reads_the_guardduty_spelling(self):
        import index

        assert (
            index.object_version_of({"objectKey": "a.pdf", "versionId": "v1"}) == "v:v1"
        )

    def test_the_two_spellings_agree_for_one_object(self):
        """This is the property the whole guard rests on: the upload and the
        verdict must derive the same string, or a matching verdict looks
        superseded and the document waits forever."""
        import index

        s3_side = {"key": "a.pdf", "version-id": "abc123", "etag": "e1"}
        gd_side = {"objectKey": "a.pdf", "versionId": "abc123", "eTag": "e1"}
        assert index.object_version_of(s3_side) == index.object_version_of(gd_side)

    @pytest.mark.parametrize(
        "s3_side,gd_side",
        [
            # No version id on either side — both fall back to the ETag, which is
            # the only field guaranteed present.
            ({"etag": "e1"}, {"eTag": "e1"}),
        ],
    )
    def test_the_etag_fallback_is_symmetric(self, s3_side, gd_side):
        import index

        assert (
            index.object_version_of(s3_side)
            == index.object_version_of(gd_side)
            == "e:e1"
        )

    def test_a_version_id_is_preferred_over_the_etag(self):
        """An ETag is content-addressed, so re-uploading identical bytes yields the
        same ETag and a stale verdict would match. The version id does not."""
        import index

        assert index.object_version_of({"version-id": "v2", "etag": "e1"}) == "v:v2"

    def test_the_value_carries_its_kind(self):
        """A version id and an ETag must not be comparable as bare strings.

        The two sides are independent producers. If one supplies a version id and
        the other only an ETag, both values are non-empty and unequal — which
        without the kind prefix is indistinguishable from a same-key re-upload, so
        the verdict is discarded as superseded. That would happen for EVERY
        document in the deployment, not as a race, with `MalwareScanVerdictSuperseded`
        blaming users for replacing files.
        """
        import index

        assert index.object_version_of({"version-id": "x"}).startswith("v:")
        assert index.object_version_of({"etag": "x"}).startswith("e:")
        assert index.object_version_of({"version-id": "x"}) != index.object_version_of(
            {"etag": "x"}
        )

    def test_etag_quoting_does_not_change_the_value(self):
        """S3 wraps an ETag in literal quotes on some surfaces and not others, and
        that difference alone would read as a different object."""
        import index

        assert index.object_version_of({"etag": '"abc"'}) == index.object_version_of(
            {"eTag": "abc"}
        )

    def test_a_kind_mismatch_is_not_comparable(self):
        """The predicate the caller keys on. A mismatch must route to the same
        fallback as a missing value — drop the version clause — rather than to the
        superseded branch."""
        import index

        assert not index.version_kinds_are_comparable("v:abc", "e:abc")
        assert index.version_kinds_are_comparable("v:abc", "v:def")
        assert index.version_kinds_are_comparable("e:abc", "e:abc")

    def test_an_absent_side_is_not_comparable_either(self):
        """Both-empty must not pin the condition on empty-equals-empty: the
        service only writes the attribute under a truthiness guard, so the stored
        value is absent and such a condition can never hold."""
        import index

        assert not index.version_kinds_are_comparable("", "v:abc")
        assert not index.version_kinds_are_comparable("v:abc", "")
        assert not index.version_kinds_are_comparable("", "")

    def test_an_object_with_neither_yields_the_empty_string(self):
        """Not an invented sentinel: a value shared by every such object would let
        any verdict match any document. Empty compares unequal to a recorded
        version, so the document waits and the alarm reports it."""
        import index

        assert index.object_version_of({}) == ""


@pytest.mark.unit
class TestTheStallSignal:
    """`MalwareScanHeld` and `MalwareScanVerdictApplied` are what the
    no-verdict-stall alarm compares, and they are the only signal for the one
    failure every other metric is blind to: GuardDuty never answering at all. If
    either series is wrong the alarm is wrong in a way nothing else reports.

    Asserted behaviourally, per path, rather than by checking the names appear in
    the source: a presence check cannot see ONE branch losing the emit, and the
    refusal branch losing it would make the alarm fire on a refusal-heavy stack.
    """

    def test_holding_also_publishes_a_zero_for_the_applied_series(self):
        """What makes the no-verdicts alarm able to fire at all.

        That alarm is metric math over held-vs-applied, and ``FILL`` fills gaps
        BETWEEN datapoints — it cannot fill a series with no datapoints. The state
        the alarm exists for is GuardDuty never answering, which is precisely when
        ``MalwareScanVerdictApplied`` is never published; the expression would then
        evaluate over an empty series and the alarm would sit at OK with
        ``TreatMissingData: notBreaching``. Publishing 0 on every hold makes the
        series exist as soon as there is anything to be held up.
        """
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

            published = {}
            for call in mock_cw.put_metric_data.call_args_list:
                for datum in call.kwargs.get("MetricData", []):
                    published[datum["MetricName"]] = datum["Value"]

        assert published.get(index.METRIC_SCAN_HELD) == 1
        # Present AND zero: a 1 here would make the alarm think a verdict landed.
        assert index.METRIC_SCAN_VERDICT_APPLIED in published, published
        assert published[index.METRIC_SCAN_VERDICT_APPLIED] == 0

    def test_holding_a_document_is_counted(self):
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

        assert "MalwareScanHeld" in str(mock_cw.put_metric_data.call_args_list)

    def test_nothing_is_counted_as_held_when_the_gate_is_off(self):
        """Otherwise a DISABLED stack would publish a held series with no applied
        series to balance it, and the alarm would fire on a stack that has no
        gate at all."""
        import index

        doc = _mock_document()
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", False),
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "s3"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "delete_current_output_objects", return_value=0),
            patch.object(index.Document, "from_s3_event", return_value=doc),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            index.handler(make_event("doc.pdf"), None)

        assert "MalwareScanHeld" not in str(mock_cw.put_metric_data.call_args_list)

    def _applied_metrics(self, result_status, allow_unscannable=False):
        import index

        doc = _mock_document()
        doc.status = Status.MALWARE_SCANNING
        doc.malware_scan_version_id = "v:vid"
        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={
                    "allow_unscannable": allow_unscannable,
                    "alert_on_block": True,
                },
            ),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = doc
            index.handler(
                make_scan_result_event(
                    result_status=result_status,
                    scan_status="COMPLETED"
                    if result_status in ("NO_THREATS_FOUND", "THREATS_FOUND")
                    else "SKIPPED",
                    threats=[{"name": "EICAR"}],
                ),
                None,
            )
        return str(mock_cw.put_metric_data.call_args_list)

    @pytest.mark.parametrize(
        "result_status,allow",
        [
            ("NO_THREATS_FOUND", False),  # admitted clean
            ("THREATS_FOUND", False),  # refused: threat
            ("UNSUPPORTED", False),  # refused: unscannable
            ("UNSUPPORTED", True),  # admitted unscanned
        ],
    )
    def test_every_applied_verdict_is_counted(self, result_status, allow):
        """All four outcomes, because the alarm compares held against applied: a
        path that decides a document without counting it leaves the alarm
        believing nothing was decided."""
        assert "MalwareScanVerdictApplied" in self._applied_metrics(
            result_status, allow
        )

    def test_an_unapplied_verdict_is_not_counted_as_applied(self):
        """The whole comparison rests on this: a superseded verdict decided
        nothing, so counting it would mask the stall it is a symptom of."""
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.MALWARE_SCANNING
            d.malware_scan_version_id = "v:v2"
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs"),
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index, "job_service", MagicMock()),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            mock_doc_service.update_document_status.side_effect = (
                ConditionalCheckFailed()
            )
            event = make_scan_result_event()
            event["detail"]["s3ObjectDetails"]["versionId"] = "v1"
            index.handler(event, None)

        emitted = str(mock_cw.put_metric_data.call_args_list)
        assert "MalwareScanVerdictSuperseded" in emitted
        assert "MalwareScanVerdictApplied" not in emitted


@pytest.mark.unit
class TestTheEnqueuedDocumentMatchesTheUngatedPath:
    """The gate must not change the document the pipeline receives.

    ⚠️ **Building it from the tracking row alone looks tidier and does not work.**
    The row persists the key, the status, the timestamps, the submission source
    and the test-set id, and **not** the buckets, the config version or the
    forced class. A row-built document therefore reaches OCR with
    `input_bucket=None`, which reads `s3://None/<key>` and fails the document;
    a pinned config version is silently replaced by the active one, which is
    worse because nothing fails.

    ⚠️ **Unit tests do not catch that on their own.** `get_document` is doubled by
    a fully populated `_mock_document()`, so a row-built document looks complete
    in every test while being incomplete in production. A double richer than the
    thing it doubles is the blind spot, which is why the row double here is
    shaped like what `_dynamodb_item_to_document` really returns, buckets unset.

    Both halves are asserted here, because neither source is sufficient: what the
    S3 object knows (buckets, metadata) and what only the row knows (when the
    upload happened, and under which trace).
    """

    def _run(self):
        import index

        # Shaped like the upload path's own document: what
        # `Document.from_s3_event` returns, buckets included.
        from_event = _mock_document()
        from_event.input_bucket = "test-input-bucket"
        from_event.output_bucket = "test-output-bucket"
        from_event.config_version = "pinned-v7"
        from_event.config_revision = 3
        # from_s3_event reads the VERDICT event, so its time is the verdict's.
        from_event.initial_event_time = "2026-10-11T00:00:00Z"
        from_event.queued_time = None
        from_event.trace_id = None

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(index.Document, "from_s3_event", return_value=from_event),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.return_value = _row_double()
            index.handler(make_scan_result_event(), None)
        return from_event, mock_sqs

    def test_the_enqueued_document_knows_which_bucket_to_read(self):
        """The defect, stated as a test. `input_bucket=None` is not a degraded
        document — OCR reads `s3://None/<key>` and the document fails."""
        doc, mock_sqs = self._run()

        mock_sqs.send_message.assert_called_once()
        assert doc.input_bucket == "test-input-bucket"
        assert doc.output_bucket == "test-output-bucket"

    def test_a_pinned_configuration_version_survives_the_gate(self):
        """The second half of the same defect, and the quieter one: the row does
        not persist ConfigVersion, so a document uploaded with a pinned profile
        would silently process under the ACTIVE one — no failure, just the wrong
        configuration."""
        doc, _ = self._run()

        assert doc.config_version == "pinned-v7"
        assert doc.config_revision == 3

    def test_the_upload_time_is_not_overwritten_by_the_verdict_time(self):
        """What only the row knows. `initial_event_time` is the document list's
        "Submitted" column and the basis of every duration computed from it, so
        taking it from the verdict event reads minutes late."""
        doc, _ = self._run()

        assert doc.initial_event_time == "2026-10-11T09:00:00Z"
        assert doc.queued_time == "2026-10-11T09:00:01Z"

    def test_the_uploads_xray_trace_is_carried_through(self):
        doc, _ = self._run()

        assert doc.trace_id == "1-upload-trace"

    def test_the_verdict_is_on_the_document_handed_to_the_pipeline(self):
        doc, _ = self._run()

        assert doc.malware_scan_result == "NO_THREATS_FOUND"
        assert doc.status == Status.QUEUED


@pytest.mark.unit
class TestTheEmptyDiscriminatorPath:
    """A silent stall for an unlikely but real failure shape.

    The condition pins the stored version against the verdict's. If either side
    is empty — a versioning-disabled bucket plus a missing ETag — pinning
    empty-equals-empty is unsatisfiable: the service only writes
    ``MalwareScanVersionId`` under a truthiness guard, so the stored value is
    absent, the compare-and-set never holds, and the handler would take the
    "stored equals verdict" duplicate path and silently drop the verdict. The
    document would then sit at MALWARE_SCANNING until the no-verdicts alarm
    fired an hour later — a success-looking failure, which is the worst shape.
    """

    def _run(self, verdict_version, stored_version):
        import index

        def fresh():
            d = _mock_document()
            d.status = Status.MALWARE_SCANNING
            d.malware_scan_version_id = stored_version
            return d

        with (
            patch.object(index, "MALWARE_SCANNING_ENABLED", True),
            patch.object(index, "INPUT_BUCKET", "test-input-bucket"),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "sns"),
            patch.object(index, "cloudwatch"),
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "s3"),
            patch.object(
                index,
                "malware_policy_for",
                return_value={"allow_unscannable": False, "alert_on_block": True},
            ),
            patch.object(
                index.Document, "from_s3_event", return_value=_mock_document()
            ),
            patch.object(index, "resolve_configuration", return_value=None),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            mock_doc_service.get_document.side_effect = lambda *_a, **_k: fresh()
            event = make_scan_result_event()
            event["detail"]["s3ObjectDetails"]["versionId"] = verdict_version
            event["detail"]["s3ObjectDetails"]["eTag"] = ""
            index.handler(event, None)
            return mock_doc_service, mock_sqs

    def test_both_discriminators_present_pins_the_version(self):
        """The normal case — the condition includes the version clause."""
        mock_doc_service, _ = self._run(verdict_version="v1", stored_version="v:v1")

        condition = mock_doc_service.update_document_status.call_args.kwargs[
            "condition_expression"
        ]
        assert "#MalwareScanVersionId = :verdict_version" in condition

    def test_both_empty_drops_the_version_clause(self):
        """The failure shape the fix was for. Both sides empty would compare
        equal and quietly admit the document on an unsatisfiable condition — the
        handler now drops the clause instead."""
        mock_doc_service, mock_sqs = self._run(verdict_version="", stored_version="")

        condition = mock_doc_service.update_document_status.call_args.kwargs[
            "condition_expression"
        ]
        assert "#MalwareScanVersionId" not in condition
        # The at-least-once guard on status survives, and the document is
        # admitted on it (write succeeds in this unit test).
        mock_sqs.send_message.assert_called_once()

    def test_one_side_empty_is_logged_but_admitted(self, caplog):
        """An event carrying neither a version id nor an ETag on one side. The
        condition keeps its status clause; the mismatch is logged so a sustained
        rate is attributable rather than looking like normal operation."""
        with caplog.at_level("WARNING"):
            mock_doc_service, _ = self._run(verdict_version="v1", stored_version="")

        condition = mock_doc_service.update_document_status.call_args.kwargs[
            "condition_expression"
        ]
        assert "#MalwareScanVersionId" not in condition
        assert "version mismatch" in caplog.text


@pytest.mark.unit
class TestTheNullVersionIdFallback:
    """A suspended-versioning bucket reports ``version-id="null"`` for every
    object. That string is truthy, so a plain ``or`` chain would short-circuit
    on it and the ETag — which actually discriminates — would never be read.
    """

    def test_null_falls_through_to_the_etag(self):
        import index

        assert index.object_version_of({"version-id": "null", "etag": "e1"}) == "e:e1"

    def test_null_on_the_guardduty_side_too(self):
        import index

        assert index.object_version_of({"versionId": "null", "eTag": "e1"}) == "e:e1"

    def test_two_distinct_objects_do_not_collide_as_null(self):
        """The property this is about. Without the fallback, both would yield
        ``'null'`` and look identical."""
        import index

        a = index.object_version_of({"versionId": "null", "eTag": "abc"})
        b = index.object_version_of({"versionId": "null", "eTag": "def"})
        assert a != b


@pytest.mark.unit
class TestConfigPrefixMappings:
    """What this Lambda asks the resolver for, and what it does with the answer.

    The precedence rules themselves live in
    lib/idp_common_pkg/tests/unit/config/test_prefix_mappings.py, over a pure
    function with no AWS. These cover the wiring, which that suite cannot see.
    """

    @staticmethod
    def _run(assignment, key="acme/invoices/x.pdf"):
        """Drive the handler with one resolver outcome; return what it did."""
        import index

        document = MagicMock()
        document.id = key
        document.input_key = key
        document.config_version = None
        document.config_revision = None
        document.submission_source = None
        document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service") as mock_doc_service,
            patch.object(index, "delete_current_output_objects") as mock_purge,
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "PrefixMappingStore") as mock_store,
            patch.object(index, "ConfigurationManager"),
            patch.object(index.Document, "from_s3_event", return_value=document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
            patch.object(
                index, "resolve_config_assignment", return_value=assignment
            ) as mock_resolve,
        ):
            mock_store.return_value.list.return_value = [{"prefix": "acme/invoices/"}]
            response = index.handler(make_event(key), None)

        metrics = [
            datum["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for datum in call.kwargs.get("MetricData", [])
        ]
        return {
            "response": response,
            "document": document,
            "metrics": metrics,
            "enqueued": mock_sqs.send_message.called,
            "created": mock_doc_service.create_document.called,
            "purged": mock_purge.called,
            "resolve_kwargs": mock_resolve.call_args.kwargs
            if mock_resolve.call_args
            else {},
        }

    def test_the_resolved_configuration_and_its_provenance_land_on_the_document(self):
        result = self._run(
            FakeAssignment(
                profile="lending",
                revision=7,
                source="prefix-mapping",
                mapping_prefix="acme/invoices/",
            )
        )
        document = result["document"]
        assert document.config_version == "lending"
        assert document.config_revision == 7
        assert document.config_source == "prefix-mapping"
        assert document.config_mapping_prefix == "acme/invoices/"
        assert result["enqueued"] is True

    def test_the_upload_metadata_and_submission_source_are_passed_to_the_resolver(self):
        """The resolver cannot see the object; everything it adjudicates is passed in,
        and `submission-source` is what exempts this deployment's own producers."""
        import index

        document = MagicMock()
        document.id = "k"
        document.input_key = "k"
        document.config_version = "chosen"
        document.config_revision = 3
        document.submission_source = "test-studio"
        document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs"),
            patch.object(index, "document_service"),
            patch.object(index, "delete_current_output_objects"),
            patch.object(index, "PrefixMappingStore"),
            patch.object(index, "ConfigurationManager"),
            patch.object(index.Document, "from_s3_event", return_value=document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
            patch.object(
                index, "resolve_config_assignment", return_value=FakeAssignment()
            ) as mock_resolve,
        ):
            index.handler(make_event("k"), None)

        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["metadata_profile"] == "chosen"
        assert kwargs["metadata_revision"] == 3
        assert kwargs["submission_source"] == "test-studio"

    def test_no_caller_scope_is_passed_because_an_s3_event_has_no_caller(self):
        """Passing a scope here would be meaningless: there is nobody to scope. The
        caller-side check lives in the upload resolver, where a caller exists."""
        result = self._run(FakeAssignment())
        assert result["resolve_kwargs"].get("allowed_profiles") is None

    def test_an_applied_mapping_emits_a_metric_with_the_prefix(self):
        result = self._run(
            FakeAssignment(source="prefix-mapping", mapping_prefix="acme/invoices/")
        )
        assert "PrefixMappingApplied" in result["metrics"]

    def test_a_conflict_is_emitted_and_the_document_still_processes(self):
        result = self._run(
            FakeAssignment(
                profile="lending",
                source="prefix-mapping",
                mapping_prefix="acme/",
                conflict=True,
            )
        )
        assert "PrefixMappingConflict" in result["metrics"]
        assert result["enqueued"] is True

    def test_a_stale_mapping_emits_a_metric_and_does_not_strand_the_document(self):
        result = self._run(FakeAssignment(unresolvable=True))
        assert "PrefixMappingUnresolvable" in result["metrics"]
        assert result["enqueued"] is True

    def test_a_reject_records_a_failed_document_and_does_not_enqueue(self):
        result = self._run(
            FakeAssignment(rejected=True, source="rejected", reason="no")
        )
        assert result["created"] is True
        assert result["enqueued"] is False
        assert "PrefixMappingRejected" in result["metrics"]
        assert result["response"]["refused"] == "config_prefix_mapping_conflict"

    def test_a_rejected_document_carries_the_reason_so_the_ui_can_show_it(self):
        """`errors` is not persisted and a reject precedes any section, so without a
        dedicated attribute the person refused sees a FAILED document and no reason."""
        import index

        result = self._run(
            FakeAssignment(rejected=True, source="rejected", reason="Refused: because.")
        )
        document = result["document"]
        assert document.config_assignment_error == "Refused: because."
        assert document.status is index.Status.FAILED

    def test_a_reject_does_not_purge_the_previous_runs_output(self):
        """The purge is destructive and the document is not going to be processed, so
        doing it first would delete a prior run's OCR output on behalf of nothing."""
        result = self._run(FakeAssignment(rejected=True))
        assert result["purged"] is False

    def test_a_mapping_lookup_failure_falls_open_and_is_alarmable(self):
        """Fail OPEN deliberately: halting ingest for the whole deployment because a
        ROUTING table is unreadable is worse than processing under the active
        profile, which is what every one of these objects does today."""
        import index

        document = MagicMock()
        document.id = "k"
        document.input_key = "k"
        document.config_version = None
        document.config_revision = None
        document.submission_source = None
        document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service"),
            patch.object(index, "delete_current_output_objects"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "PrefixMappingStore") as mock_store,
            patch.object(index, "ConfigurationManager"),
            patch.object(index.Document, "from_s3_event", return_value=document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
            patch.object(
                index, "resolve_config_assignment", return_value=FakeAssignment()
            ) as mock_resolve,
        ):
            mock_store.return_value.list.side_effect = RuntimeError("throttled")
            index.handler(make_event("k"), None)

        metrics = [
            datum["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for datum in call.kwargs.get("MetricData", [])
        ]
        assert "PrefixMappingLookupFailed" in metrics
        assert mock_sqs.send_message.called is True
        # Resolution still ran, with an empty mapping set -- which is what makes the
        # fallback "today's behaviour" rather than "no configuration at all".
        assert mock_resolve.call_args.kwargs["mappings"] == []

    def test_an_unset_config_table_is_alarmed_like_a_read_failure(self):
        """The same window, on the path where invisibility costs most.

        An unset CONFIG_TABLE bypasses every mapping for EVERY document, not for
        one upload, which is the condition `PrefixMappingLookupFailed` is alarmed
        for. Failing open is still right -- halting ingest because a routing table
        is unreadable is worse -- but the window has to be visible, and the sibling
        resolvers already treat this exact branch that way.
        """
        import index

        document = MagicMock()
        document.id = "k"
        document.input_key = "k"
        document.config_version = None
        document.config_revision = None
        document.submission_source = None
        document.to_json.return_value = "{}"

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service"),
            patch.object(index, "delete_current_output_objects"),
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index.Document, "from_s3_event", return_value=document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
        ):
            os.environ.pop("CONFIG_TABLE", None)
            index.handler(make_event("k"), None)

        metrics = [
            datum["MetricName"]
            for call in mock_cw.put_metric_data.call_args_list
            for datum in call.kwargs.get("MetricData", [])
        ]
        assert "PrefixMappingLookupFailed" in metrics
        # Still enqueued: the fail-open is the point, the silence was not.
        assert mock_sqs.send_message.called is True

    def test_a_resolution_fault_does_not_drop_the_document(self):
        import index

        document = MagicMock()
        document.id = "k"
        document.input_key = "k"
        document.to_json.return_value = "{}"

        with (
            patch.object(index, "sqs") as mock_sqs,
            patch.object(index, "document_service"),
            patch.object(index, "delete_current_output_objects"),
            patch.object(index.Document, "from_s3_event", return_value=document),
            patch.object(index.xray_recorder, "current_segment", return_value=None),
            patch.object(
                index, "resolve_configuration", side_effect=RuntimeError("boom")
            ),
        ):
            response = index.handler(make_event("k"), None)

        assert response["statusCode"] == 200
        assert mock_sqs.send_message.called is True

    def test_telemetry_failure_never_affects_ingest(self):
        import index

        with patch.object(index, "cloudwatch") as mock_cw:
            mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch is down")
            index._emit("PrefixMappingApplied", {"Prefix": "acme/"})

    def test_a_missing_config_table_degrades_rather_than_failing(self):
        """Resolution needs the table; without it the document still processes."""
        import index

        document = MagicMock()
        document.config_version = None
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_TABLE", None)
            assert index.resolve_configuration(document, "k") is None
