# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the resolver wires the reprocess service to this deployment.

The reprocess itself is ``idp_common.run_plan.reprocess.ReprocessService``. The
resolver gives it the stack's clients, buckets and queue, the review-queue
service when the stack has a review table, and the function that publishes its
metrics. These tests cover that wiring, and one request through the real
service.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from idp_common.models import Document, Status
from idp_common.run_plan.models import RUN_PLAN_ATTRIBUTE, RunKind, decode

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("INPUT_BUCKET", "test-in")
os.environ.setdefault("OUTPUT_BUCKET", "test-out")
os.environ.setdefault("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/test-queue")
# index.py builds a document service at import; it needs a table name but makes
# no call, so any value does.
os.environ.setdefault("TRACKING_TABLE", "test-tracking")


def _load_index():
    """Load the handler under its own module name, not as ``index``."""
    spec = importlib.util.spec_from_file_location(
        "reprocess_resolver_index_wiring", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_INDEX = _load_index()


@pytest.fixture
def index():
    _INDEX._REVIEW_SERVICE_CACHE.clear()
    yield _INDEX
    _INDEX._REVIEW_SERVICE_CACHE.clear()


def _event(keys=("claims/a.pdf",), **arguments):
    return {
        "info": {"fieldName": "reprocessDocument"},
        "arguments": {"objectKeys": list(keys), **arguments},
        "identity": {
            "claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}
        },
    }


@pytest.mark.unit
class TestTheServiceIsBuiltFromThisDeployment:
    def _built(self, index, monkeypatch):
        recorded = {}

        def _record(**kwargs):
            recorded.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(index, "ReprocessService", _record)
        index._service()
        return recorded

    def test_it_gets_the_clients_the_buckets_and_the_queue(self, index, monkeypatch):
        built = self._built(index, monkeypatch)

        assert built["document_service"] is index.document_service
        assert built["sqs_client"] is index.sqs_client
        assert built["s3"] is index.s3_client
        assert built["queue_url"] == index.queue_url
        assert (built["input_bucket"], built["output_bucket"]) == (
            index.input_bucket,
            index.output_bucket,
        )
        assert built["retention_days"] == index.retentionDays

    def test_it_gets_the_factories_and_the_metric_publisher(self, index, monkeypatch):
        built = self._built(index, monkeypatch)

        assert built["config_manager_factory"] is index._configuration_manager
        assert built["review_service_factory"] is index._review_service
        assert built["metrics"] is index._emit

    def test_the_working_bucket_comes_from_the_environment(self, index, monkeypatch):
        monkeypatch.setenv("WORKING_BUCKET", "stack-working")

        assert self._built(index, monkeypatch)["working_bucket"] == "stack-working"

    @pytest.mark.parametrize("value", [None, ""])
    def test_no_working_bucket_is_none(self, index, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("WORKING_BUCKET", raising=False)
        else:
            monkeypatch.setenv("WORKING_BUCKET", value)

        assert self._built(index, monkeypatch)["working_bucket"] is None

    def test_it_is_built_from_the_modules_current_clients(self, index, monkeypatch):
        """Per request, so a replaced client is the one the service gets."""
        replacement = MagicMock()
        monkeypatch.setattr(index, "document_service", replacement)

        assert self._built(index, monkeypatch)["document_service"] is replacement


@pytest.fixture
def review_service_module(monkeypatch):
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "idp_common.review_queues.service", fake)
    return fake


@pytest.mark.unit
class TestReviewService:
    def test_it_is_built_once_when_a_hitl_table_is_configured(
        self, index, review_service_module
    ):
        with patch.dict(os.environ, {"HITL_TABLE_NAME": "test-hitl"}):
            first = index._review_service()
            second = index._review_service()
        built = review_service_module.create_review_queue_service
        assert first is second is built.return_value
        built.assert_called_once_with()

    def test_there_is_none_without_a_hitl_table(self, index, review_service_module):
        with patch.dict(os.environ, {"HITL_TABLE_NAME": ""}):
            assert index._review_service() is None
        review_service_module.create_review_queue_service.assert_not_called()


@pytest.mark.unit
class TestTheMetricPublisher:
    def test_a_count_is_published_under_the_stacks_namespace(self, index):
        with patch.object(index, "cloudwatch") as cloudwatch:
            index._emit("StaleOutputPurgeFailed")

        cloudwatch.put_metric_data.assert_called_once()
        call = cloudwatch.put_metric_data.call_args
        # The exact namespace: the code's fallback ``IDP`` in a test environment.
        # A typo in a refactor (``idp``, ``IDP-Test``) fails here.
        assert call.kwargs["Namespace"] == "IDP"
        metrics = call.kwargs["MetricData"]
        assert metrics[0]["MetricName"] == "StaleOutputPurgeFailed"
        assert metrics[0]["Value"] == 1
        assert metrics[0]["Unit"] == "Count"
        assert "Dimensions" not in metrics[0]

    def test_the_namespace_is_read_from_the_module_attribute(self, index):
        """``METRIC_NAMESPACE`` binds at import, so the case above only ever sees
        the fallback. A renamed environment variable would keep hitting the
        fallback in production: the metric would be published under the wrong
        namespace and the alarm would never fire. Patching the attribute to a
        stack-shaped value shows the publisher uses what it resolves to."""
        with (
            patch.object(index, "cloudwatch") as cloudwatch,
            patch.object(index, "METRIC_NAMESPACE", "idp-dev-qs"),
        ):
            index._emit("StaleOutputPurgeFailed")

        assert cloudwatch.put_metric_data.call_args.kwargs["Namespace"] == "idp-dev-qs"

    def test_dimensions_are_published_and_cut_to_the_limit(self, index):
        with patch.object(index, "cloudwatch") as cloudwatch:
            index._emit("PrefixMappingApplied", {"Prefix": "p" * 300})

        (datum,) = cloudwatch.put_metric_data.call_args.kwargs["MetricData"]
        assert datum["Dimensions"] == [{"Name": "Prefix", "Value": "p" * 255}]

    def test_a_publish_failure_is_swallowed(self, index):
        """Telemetry must not affect the reprocess."""
        with patch.object(index, "cloudwatch") as cloudwatch:
            cloudwatch.put_metric_data.side_effect = RuntimeError("CW throttle")

            index._emit("StaleOutputPurgeFailed")  # must not raise


@pytest.fixture
def deployment(index, monkeypatch):
    """The handler over doubles of the tracking table, S3 and the queue."""
    documents = MagicMock()
    documents.get_document.return_value = None
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "msg-1"}
    s3 = MagicMock()
    purge = MagicMock(return_value=0)
    monkeypatch.setattr(index, "document_service", documents)
    monkeypatch.setattr(index, "sqs_client", sqs)
    monkeypatch.setattr(index, "s3_client", s3)
    monkeypatch.setattr(index, "cloudwatch", MagicMock())
    monkeypatch.setattr(
        "idp_common.run_plan.reprocess.delete_current_output_objects", purge
    )
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    monkeypatch.setattr(
        index, "sanitize_event_for_logging", lambda event: {"redacted": True}
    )
    return SimpleNamespace(documents=documents, sqs=sqs, s3=s3, purge=purge)


@pytest.mark.unit
class TestOneRequestThroughTheService:
    def test_a_document_is_queued_from_the_beginning_with_a_run_plan(
        self, index, deployment
    ):
        result = index.handler(_event(version="v2", revision=4), None)

        assert result == {
            "queued": 1,
            "results": [
                {
                    "objectKey": "claims/a.pdf",
                    "status": "QUEUED",
                    "startStep": "beginning",
                    "reason": None,
                    "message": None,
                }
            ],
        }

        # The output is purged, broadly, in this stack's output bucket.
        (purge_call,) = deployment.purge.call_args_list
        assert purge_call.args[1:] == (index.output_bucket, "claims/a.pdf")
        assert "subprefixes" not in purge_call.kwargs
        # The tracking row is recreated, pinned as asked.
        (created,) = deployment.documents.create_document.call_args_list
        document = created.args[0]
        assert isinstance(document, Document)
        assert document.status == Status.QUEUED
        assert (document.config_version, document.config_revision) == ("v2", 4)
        assert (document.input_bucket, document.output_bucket) == (
            index.input_bucket,
            index.output_bucket,
        )
        # The row is read with its policy rule validation result, which a run that
        # starts after that stage keeps.
        deployment.documents.get_document.assert_called_once_with(
            "claims/a.pdf", with_policy_rule_validation=True
        )
        # One message, on the document queue, with the plan.
        (sent,) = deployment.sqs.send_message.call_args_list
        assert sent.kwargs["QueueUrl"] == index.queue_url
        assert json.loads(sent.kwargs["MessageBody"])["input_key"] == "claims/a.pdf"
        attributes = sent.kwargs["MessageAttributes"]
        assert attributes["EventType"]["StringValue"] == "DocumentReprocessed"
        assert attributes["ObjectKey"]["StringValue"] == "claims/a.pdf"
        plan = decode(attributes[RUN_PLAN_ATTRIBUTE]["StringValue"])
        assert (plan.kind, plan.start) == (RunKind.REPROCESS, "beginning")

    def test_a_refused_document_is_not_queued_and_the_call_still_succeeds(
        self, index, deployment
    ):
        """One document that cannot run again does not fail the request."""
        quarantined = MagicMock()
        quarantined.status = Status.QUARANTINED
        deployment.documents.get_document.side_effect = lambda key, **_kwargs: (
            quarantined if key == "claims/infected.pdf" else None
        )

        result = index.handler(_event(("claims/infected.pdf", "claims/ok.pdf")), None)

        assert result["queued"] == 1
        assert [(r["objectKey"], r["status"]) for r in result["results"]] == [
            ("claims/infected.pdf", "REFUSED"),
            ("claims/ok.pdf", "QUEUED"),
        ]
        sent = [
            call.kwargs["MessageAttributes"]["ObjectKey"]["StringValue"]
            for call in deployment.sqs.send_message.call_args_list
        ]
        assert sent == ["claims/ok.pdf"]

    def test_a_refusal_of_the_request_reaches_the_caller(self, index, deployment):
        """A prefix mapping set to refuse a conflicting profile refuses the whole
        request, as a ``ValueError`` the dispatcher answers with a 400."""
        mapping = {
            "prefix": "claims/",
            "configProfile": "mapped-profile",
            "metadataPrecedence": "reject",
        }
        with patch(
            "idp_common.run_plan.reprocess.ReprocessService._prefix_mappings",
            return_value=[mapping],
        ):
            with pytest.raises(ValueError, match="Refused"):
                index.handler(_event(version="chosen"), None)

        deployment.sqs.send_message.assert_not_called()

    @pytest.mark.parametrize("requested", [None, "chosen"])
    def test_a_mapping_to_a_missing_profile_refuses_the_request(
        self, index, deployment, monkeypatch, requested
    ):
        """#597: a prefix mapping that names a profile that does not exist fails
        the request, whether or not the caller named a profile, and nothing is
        queued. The refusal names the prefix, which is what the Admin edits."""
        manager = MagicMock()
        manager.resolve_active_version.return_value = "active"
        manager.resolve_published_revision.return_value = None
        monkeypatch.setattr(index, "_configuration_manager", lambda: manager)
        mapping = {"prefix": "claims/", "configProfile": "deleted"}
        with (
            patch(
                "idp_common.run_plan.reprocess.ReprocessService._prefix_mappings",
                return_value=[mapping],
            ),
            patch(
                "idp_common.run_plan.reprocess._profile_head_exists",
                side_effect=lambda _manager, profile: profile != "deleted",
            ),
        ):
            with pytest.raises(ValueError, match="does not exist") as refused:
                index.handler(_event(version=requested), None)

        assert "'claims/'" in str(refused.value)
        deployment.sqs.send_message.assert_not_called()

    def test_no_object_keys_is_refused_before_the_service_is_built(
        self, index, deployment, monkeypatch
    ):
        built = MagicMock()
        monkeypatch.setattr(index, "_service", built)

        assert index.handler(_event(()), None) == {"queued": 0, "results": []}
        built.assert_not_called()

    def test_a_viewer_never_reaches_the_service(self, index, deployment, monkeypatch):
        built = MagicMock()
        monkeypatch.setattr(index, "_service", built)
        event = _event()
        event["identity"]["claims"]["cognito:groups"] = ["Viewer"]

        with pytest.raises(PermissionError, match="Admin or Author"):
            index.handler(event, None)

        built.assert_not_called()
