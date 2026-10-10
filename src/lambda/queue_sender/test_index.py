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
