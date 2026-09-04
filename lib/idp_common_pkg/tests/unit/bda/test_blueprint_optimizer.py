# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the BlueprintOptimizer service class."""

# ruff: noqa: E402, I001

import json
import io
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.blueprint_optimizer import (
    BlueprintOptimizer,
    OptimizationMetrics,
    OptimizationStatus,
    OPTIMIZATION_MAX_DURATION_SECONDS,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_class_schema(
    class_id="Invoice",
    doc_type="Invoice",
    properties=None,
):
    """Build a minimal IDP class JSON Schema for tests."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": class_id,
        "x-aws-idp-document-type": doc_type,
        "description": "Test class",
        "type": "object",
    }
    if properties:
        schema["properties"] = properties
    return schema


def _make_evaluation_comparison(
    before_exact=0.5,
    before_f1=0.5,
    before_confidence=0.8,
    after_exact=0.9,
    after_f1=0.9,
    after_confidence=0.85,
):
    """Build a mock evaluationComparison payload."""
    return {
        "evaluationComparison": {
            "before": {
                "blueprintSchema": json.dumps({"type": "before_schema"}),
                "aggregateMetrics": {
                    "exactMatch": before_exact,
                    "f1": before_f1,
                    "confidence": before_confidence,
                },
            },
            "after": {
                "blueprintSchema": json.dumps({"type": "after_schema"}),
                "aggregateMetrics": {
                    "exactMatch": after_exact,
                    "f1": after_f1,
                    "confidence": after_confidence,
                },
            },
        }
    }


@pytest.fixture
def mock_blueprint_service():
    svc = MagicMock()
    svc.get_or_create_project_for_version.return_value = (
        "arn:aws:bedrock:us-east-1:123456789012:project/test-project"
    )
    svc._sanitize_property_names.return_value = None  # mutates in place
    svc._transform_json_schema_to_bedrock_blueprint.return_value = {"bda": "schema"}
    svc.transform_bda_blueprint_to_idp_class_schema.return_value = {
        "type": "object",
        "properties": {"optimized": {"type": "string"}},
    }
    svc.blueprint_name_prefix = "IDP-1"
    # Default: no existing blueprint found — triggers creation path
    svc._retrieve_all_blueprints.return_value = []
    svc._blueprint_lookup.return_value = None
    return svc


@pytest.fixture
def mock_blueprint_creator():
    creator = MagicMock()
    creator.bedrock_client = MagicMock()
    creator.create_blueprint.return_value = {
        "blueprint": {
            "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-test"
        }
    }
    return creator


@pytest.fixture
def mock_config_manager():
    mgr = MagicMock()
    mgr.get_raw_configuration.return_value = {"classes": []}
    return mgr


@pytest.fixture
def mock_s3_client():
    return MagicMock()


@pytest.fixture
def mock_bedrock_client():
    return MagicMock()


@pytest.fixture
def optimizer(
    mock_blueprint_service,
    mock_blueprint_creator,
    mock_config_manager,
    mock_s3_client,
    mock_bedrock_client,
):
    return BlueprintOptimizer(
        blueprint_service=mock_blueprint_service,
        blueprint_creator=mock_blueprint_creator,
        config_manager=mock_config_manager,
        s3_client=mock_s3_client,
        bedrock_client=mock_bedrock_client,
    )


# ---------------------------------------------------------------------------
# Tests: _create_blueprint_for_class
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateBlueprintForClass:
    """Tests for BlueprintOptimizer._create_blueprint_for_class."""

    def test_creates_blueprint_with_correct_calls(
        self, optimizer, mock_blueprint_service, mock_blueprint_creator
    ):
        """Verify project lookup, sanitization, transform, create, and version are called."""
        schema = _make_class_schema()
        arn = optimizer._create_blueprint_for_class(schema, "default")

        mock_blueprint_service.get_or_create_project_for_version.assert_called_once_with(
            "default"
        )
        mock_blueprint_service._retrieve_all_blueprints.assert_called_once()
        mock_blueprint_service._blueprint_lookup.assert_called_once()
        mock_blueprint_service._sanitize_property_names.assert_called_once()
        mock_blueprint_service._transform_json_schema_to_bedrock_blueprint.assert_called_once()
        mock_blueprint_creator.create_blueprint.assert_called_once()
        mock_blueprint_creator.create_blueprint_version.assert_called_once_with(
            blueprint_arn="arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-test",
            project_arn="arn:aws:bedrock:us-east-1:123456789012:project/test-project",
        )
        assert arn == "arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-test"

    def test_sanitize_called_before_transform(self, optimizer, mock_blueprint_service):
        """Verify _sanitize_property_names is called before _transform."""
        call_order = []
        mock_blueprint_service._sanitize_property_names.side_effect = lambda s: (
            call_order.append("sanitize")
        )
        mock_blueprint_service._transform_json_schema_to_bedrock_blueprint.side_effect = (
            lambda s: call_order.append("transform") or {"bda": "schema"}
        )

        schema = _make_class_schema()
        optimizer._create_blueprint_for_class(schema, "default")

        assert call_order == ["sanitize", "transform"]

    def test_uses_class_id_in_blueprint_name(self, optimizer, mock_blueprint_creator):
        """Blueprint name should contain the stack prefix and class $id."""
        schema = _make_class_schema(class_id="W-4")
        optimizer._create_blueprint_for_class(schema, "default")

        call_kwargs = mock_blueprint_creator.create_blueprint.call_args
        bp_name = (
            call_kwargs.kwargs.get("blueprint_name")
            or call_kwargs[1].get("blueprint_name")
            or call_kwargs[0][1]
        )
        assert "W-4" in bp_name
        assert "IDP-1" in bp_name

    def test_blueprint_name_is_valid_for_a_class_id_with_a_space(
        self, optimizer, mock_blueprint_creator
    ):
        """A class id with a space would otherwise fail CreateBlueprint here
        too — this path composes its own name rather than reusing the sync
        path's, so it needs the same sanitization."""
        import re

        optimizer._create_blueprint_for_class(
            _make_class_schema(class_id="Task cards"), "default"
        )

        bp_name = mock_blueprint_creator.create_blueprint.call_args.kwargs[
            "blueprint_name"
        ]
        assert re.match(r"^[a-zA-Z0-9_-]+$", bp_name), bp_name
        assert "IDP-1-Task-cards-" in bp_name

    def test_does_not_mutate_original_schema(self, optimizer):
        """Original class_schema should not be modified (deepcopy used)."""
        schema = _make_class_schema(properties={"field": {"type": "string"}})
        original = json.dumps(schema, sort_keys=True)
        optimizer._create_blueprint_for_class(schema, "default")
        assert json.dumps(schema, sort_keys=True) == original

    def test_raises_on_blueprint_creation_failure(
        self, optimizer, mock_blueprint_creator
    ):
        """Should propagate exceptions from create_blueprint."""
        mock_blueprint_creator.create_blueprint.side_effect = Exception("API error")
        schema = _make_class_schema()
        with pytest.raises(Exception, match="API error"):
            optimizer._create_blueprint_for_class(schema, "default")

    def test_reuses_existing_blueprint(
        self, optimizer, mock_blueprint_service, mock_blueprint_creator
    ):
        """Should reuse existing blueprint and skip creation."""
        existing_bp = {
            "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-existing",
            "blueprintName": "IDP-1-Invoice-abc12345",
        }
        mock_blueprint_service._blueprint_lookup.return_value = existing_bp

        schema = _make_class_schema()
        arn = optimizer._create_blueprint_for_class(schema, "default")

        assert arn == "arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-existing"
        mock_blueprint_creator.create_blueprint.assert_not_called()
        mock_blueprint_creator.create_blueprint_version.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _upload_optimization_assets
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUploadOptimizationAssets:
    """Tests for BlueprintOptimizer._upload_optimization_assets."""

    def test_returns_correct_s3_uris(self, optimizer):
        """Should return s3:// URIs for document and ground truth."""
        doc_uri, gt_uri = optimizer._upload_optimization_assets(
            "docs/invoice.pdf", "gt/invoice.json", "my-bucket"
        )
        assert doc_uri == "s3://my-bucket/docs/invoice.pdf"
        assert gt_uri == "s3://my-bucket/gt/invoice.json"

    def test_handles_nested_keys(self, optimizer):
        """Should handle deeply nested S3 keys."""
        doc_uri, gt_uri = optimizer._upload_optimization_assets(
            "a/b/c/doc.pdf", "x/y/z/gt.json", "bucket-name"
        )
        assert doc_uri == "s3://bucket-name/a/b/c/doc.pdf"
        assert gt_uri == "s3://bucket-name/x/y/z/gt.json"


# ---------------------------------------------------------------------------
# Tests: _invoke_optimization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInvokeOptimization:
    """Tests for BlueprintOptimizer._invoke_optimization."""

    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_payload_structure(self, mock_boto3, optimizer, mock_bedrock_client):
        """Verify the optimization API payload matches the BDA schema."""
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.return_value = {
            "invocationArn": "arn:aws:bedrock:us-east-1:123456789012:invocation/inv-123"
        }

        result = optimizer._invoke_optimization(
            blueprint_arn="arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-1",
            document_s3_uri="s3://bucket/doc.pdf",
            ground_truth_s3_uri="s3://bucket/gt.json",
            output_s3_uri="s3://bucket/output/",
        )

        call_kwargs = (
            mock_bedrock_client.invoke_blueprint_optimization_async.call_args.kwargs
        )
        assert (
            call_kwargs["blueprint"]["blueprintArn"]
            == "arn:aws:bedrock:us-east-1:123456789012:blueprint/bp-1"
        )
        assert call_kwargs["blueprint"]["stage"] == "LIVE"
        assert (
            call_kwargs["samples"][0]["assetS3Object"]["s3Uri"] == "s3://bucket/doc.pdf"
        )
        assert (
            call_kwargs["samples"][0]["groundTruthS3Object"]["s3Uri"]
            == "s3://bucket/gt.json"
        )
        assert (
            call_kwargs["outputConfiguration"]["s3Object"]["s3Uri"]
            == "s3://bucket/output/"
        )
        assert "dataAutomationProfileArn" in call_kwargs
        assert result == "arn:aws:bedrock:us-east-1:123456789012:invocation/inv-123"

    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_raises_on_api_failure(self, mock_boto3, optimizer, mock_bedrock_client):
        """Should propagate exceptions from the BDA API."""
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.side_effect = Exception(
            "BDA API error"
        )
        with pytest.raises(Exception, match="BDA API error"):
            optimizer._invoke_optimization(
                "arn:bp", "s3://b/d.pdf", "s3://b/gt.json", "s3://b/out/"
            )


# ---------------------------------------------------------------------------
# Tests: _poll_optimization_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPollOptimizationStatus:
    """Tests for BlueprintOptimizer._poll_optimization_status."""

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_returns_on_success(self, mock_time, optimizer, mock_bedrock_client):
        """Should return immediately when first response is Success."""
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "Success",
            "outputConfiguration": {"s3Object": {"s3Uri": "s3://b/out"}},
        }

        result = optimizer._poll_optimization_status("arn:inv-1")

        assert result["status"] == "Success"
        mock_bedrock_client.get_blueprint_optimization_status.assert_called_once_with(
            invocationArn="arn:inv-1"
        )
        mock_time.sleep.assert_not_called()

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_returns_on_service_error(self, mock_time, optimizer, mock_bedrock_client):
        """Should return on ServiceError terminal state."""
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "ServiceError",
            "errorMessage": "Internal error",
        }

        result = optimizer._poll_optimization_status("arn:inv-1")
        assert result["status"] == "ServiceError"

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_returns_on_client_error(self, mock_time, optimizer, mock_bedrock_client):
        """Should return on ClientError terminal state."""
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "ClientError",
            "errorType": "ValidationException",
        }

        result = optimizer._poll_optimization_status("arn:inv-1")
        assert result["status"] == "ClientError"

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_polls_through_non_terminal_states(
        self, mock_time, optimizer, mock_bedrock_client
    ):
        """Should poll through Created/InProgress before returning Success."""
        mock_time.time.side_effect = [0, 0, 10, 10, 25, 25]
        mock_bedrock_client.get_blueprint_optimization_status.side_effect = [
            {"status": "Created"},
            {"status": "InProgress"},
            {
                "status": "Success",
                "outputConfiguration": {"s3Object": {"s3Uri": "s3://b/out"}},
            },
        ]

        result = optimizer._poll_optimization_status("arn:inv-1")

        assert result["status"] == "Success"
        assert mock_bedrock_client.get_blueprint_optimization_status.call_count == 3
        assert mock_time.sleep.call_count == 2

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_exponential_backoff_intervals(
        self, mock_time, optimizer, mock_bedrock_client
    ):
        """Verify sleep intervals follow exponential backoff: 5, 10, 20, 30."""
        # Simulate time never exceeding max duration
        mock_time.time.side_effect = [0, 0, 5, 5, 15, 15, 35, 35, 65]
        mock_bedrock_client.get_blueprint_optimization_status.side_effect = [
            {"status": "InProgress"},
            {"status": "InProgress"},
            {"status": "InProgress"},
            {"status": "InProgress"},
            {
                "status": "Success",
                "outputConfiguration": {"s3Object": {"s3Uri": "s3://b/out"}},
            },
        ]

        optimizer._poll_optimization_status("arn:inv-1")

        sleep_calls = [c.args[0] for c in mock_time.sleep.call_args_list]
        assert sleep_calls[0] == 5  # 5 * 2^0
        assert sleep_calls[1] == 10  # 5 * 2^1
        assert sleep_calls[2] == 20  # 5 * 2^2
        assert sleep_calls[3] == 30  # min(5 * 2^3, 30) = 30

    @patch("idp_common.bda.blueprint_optimizer.time")
    def test_timeout_raises_error(self, mock_time, optimizer, mock_bedrock_client):
        """Should raise TimeoutError when polling exceeds max duration."""
        # First call: start_time = 0, second call (elapsed check): 901 > 900
        mock_time.time.side_effect = [0, OPTIMIZATION_MAX_DURATION_SECONDS + 1]
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "InProgress"
        }

        with pytest.raises(TimeoutError, match="timed out"):
            optimizer._poll_optimization_status("arn:inv-1")


# ---------------------------------------------------------------------------
# Tests: _fetch_optimization_results
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFetchOptimizationResults:
    """Tests for BlueprintOptimizer._fetch_optimization_results."""

    def test_reads_and_parses_s3_json(self, optimizer, mock_s3_client):
        """Should read JSON from S3 and return parsed dict."""
        expected = _make_evaluation_comparison()
        body_bytes = json.dumps(expected).encode("utf-8")
        mock_s3_client.get_object.return_value = {"Body": io.BytesIO(body_bytes)}

        result = optimizer._fetch_optimization_results(
            "s3://my-bucket/optimization/output/abc123/inv-id/0"
        )

        mock_s3_client.get_object.assert_called_once_with(
            Bucket="my-bucket",
            Key="optimization/output/abc123/inv-id/0/optimization_results.json",
        )
        assert "evaluationComparison" in result
        assert (
            result["evaluationComparison"]["before"]["aggregateMetrics"]["exactMatch"]
            == 0.5
        )

    def test_parses_bucket_and_key_correctly(self, optimizer, mock_s3_client):
        """Should correctly split s3://bucket/key from URI."""
        mock_s3_client.get_object.return_value = {
            "Body": io.BytesIO(b'{"evaluationComparison": {}}')
        }

        optimizer._fetch_optimization_results("s3://bucket-name/a/b/c")

        mock_s3_client.get_object.assert_called_once_with(
            Bucket="bucket-name",
            Key="a/b/c/optimization_results.json",
        )


# ---------------------------------------------------------------------------
# Tests: _evaluate_improvement
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateImprovement:
    """Tests for BlueprintOptimizer._evaluate_improvement."""

    def test_improvement_when_exact_match_higher(self, optimizer):
        """Should detect improvement when after exactMatch > before."""
        data = _make_evaluation_comparison(
            before_exact=0.5,
            before_f1=0.5,
            after_exact=0.9,
            after_f1=0.5,
        )
        improved, before, after = optimizer._evaluate_improvement(data)
        assert improved is True
        assert after.exact_match == 0.9
        assert before.exact_match == 0.5

    def test_improvement_when_f1_higher(self, optimizer):
        """Should detect improvement when after f1 > before."""
        data = _make_evaluation_comparison(
            before_exact=0.5,
            before_f1=0.5,
            after_exact=0.5,
            after_f1=0.8,
        )
        improved, before, after = optimizer._evaluate_improvement(data)
        assert improved is True
        assert after.f1 == 0.8

    def test_improvement_when_both_higher(self, optimizer):
        """Should detect improvement when both metrics are higher."""
        data = _make_evaluation_comparison(
            before_exact=0.3,
            before_f1=0.4,
            after_exact=0.8,
            after_f1=0.9,
        )
        improved, _, _ = optimizer._evaluate_improvement(data)
        assert improved is True

    def test_no_improvement_when_equal(self, optimizer):
        """Should not detect improvement when metrics are equal."""
        data = _make_evaluation_comparison(
            before_exact=0.7,
            before_f1=0.7,
            after_exact=0.7,
            after_f1=0.7,
        )
        improved, _, _ = optimizer._evaluate_improvement(data)
        assert improved is False

    def test_no_improvement_when_worse(self, optimizer):
        """Should not detect improvement when after metrics are lower."""
        data = _make_evaluation_comparison(
            before_exact=0.9,
            before_f1=0.9,
            after_exact=0.5,
            after_f1=0.5,
        )
        improved, _, _ = optimizer._evaluate_improvement(data)
        assert improved is False

    def test_returns_correct_metrics_objects(self, optimizer):
        """Should return OptimizationMetrics with correct values."""
        data = _make_evaluation_comparison(
            before_exact=0.6,
            before_f1=0.7,
            before_confidence=0.8,
            after_exact=0.9,
            after_f1=0.95,
            after_confidence=0.85,
        )
        improved, before, after = optimizer._evaluate_improvement(data)
        assert isinstance(before, OptimizationMetrics)
        assert isinstance(after, OptimizationMetrics)
        assert before.confidence == 0.8
        assert after.confidence == 0.85


# ---------------------------------------------------------------------------
# Tests: _apply_optimized_schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyOptimizedSchema:
    """Tests for BlueprintOptimizer._apply_optimized_schema."""

    def test_calls_update_blueprint(self, optimizer, mock_blueprint_creator):
        """Should call update_blueprint with ARN, stage, and schema."""
        schema = _make_class_schema()
        optimized_bda = {"optimized": True}

        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", optimized_bda, schema, "default"
        )

        mock_blueprint_creator.update_blueprint.assert_called_once_with(
            "arn:bp-1", "DEVELOPMENT", json.dumps(optimized_bda)
        )

    def test_calls_create_blueprint_version(self, optimizer, mock_blueprint_creator):
        """Should create a blueprint version associated with the project."""
        schema = _make_class_schema()
        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, schema, "default"
        )

        mock_blueprint_creator.create_blueprint_version.assert_called_once_with(
            "arn:bp-1", "arn:proj-1"
        )

    def test_calls_transform_bda_to_idp(self, optimizer, mock_blueprint_service):
        """Should transform optimized BDA schema back to IDP format."""
        schema = _make_class_schema()
        optimized_bda = {"optimized": True}
        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", optimized_bda, schema, "default"
        )

        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.assert_called_once_with(
            optimized_bda
        )

    def test_preserves_original_id(
        self, optimizer, mock_blueprint_service, mock_config_manager
    ):
        """Should preserve $id from original class schema."""
        original = _make_class_schema(class_id="MyClass-123")
        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.return_value = {
            "type": "object",
            "properties": {},
        }

        result = optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, original, "default"
        )

        assert result["$id"] == "MyClass-123"

    def test_preserves_original_doc_type(
        self, optimizer, mock_blueprint_service, mock_config_manager
    ):
        """Should preserve x-aws-idp-document-type from original."""
        original = _make_class_schema(doc_type="W-2")
        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.return_value = {
            "type": "object",
        }

        result = optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, original, "default"
        )

        assert result["x-aws-idp-document-type"] == "W-2"

    def test_updates_config_with_new_class(
        self, optimizer, mock_config_manager, mock_blueprint_service
    ):
        """Should save updated class definitions via ConfigurationManager."""
        original = _make_class_schema(class_id="Invoice")
        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.return_value = {
            "type": "object",
        }
        mock_config_manager.get_raw_configuration.return_value = {
            "classes": [{"$id": "OtherClass", "type": "object"}]
        }

        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, original, "default"
        )

        mock_config_manager.get_raw_configuration.assert_called_once_with(
            "Config", version="default"
        )
        mock_config_manager.save_raw_configuration.assert_called_once()
        saved_config = mock_config_manager.save_raw_configuration.call_args[0][1]
        class_ids = [c.get("$id") for c in saved_config["classes"]]
        assert "Invoice" in class_ids
        assert "OtherClass" in class_ids

    def test_replaces_existing_class_by_id(
        self, optimizer, mock_config_manager, mock_blueprint_service
    ):
        """Should replace existing class with same $id, not duplicate."""
        original = _make_class_schema(class_id="Invoice")
        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.return_value = {
            "type": "object",
            "properties": {"new": {}},
        }
        mock_config_manager.get_raw_configuration.return_value = {
            "classes": [
                {"$id": "Invoice", "type": "object", "properties": {"old": {}}},
                {"$id": "Receipt", "type": "object"},
            ]
        }

        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, original, "default"
        )

        saved_config = mock_config_manager.save_raw_configuration.call_args[0][1]
        invoice_classes = [
            c for c in saved_config["classes"] if c.get("$id") == "Invoice"
        ]
        assert len(invoice_classes) == 1
        # The optimized schema's own content replaces the old content. Settings
        # the transform does not produce are carried forward instead — see
        # TestApplyOptimizedSchemaPreservesAuthoredSettings (#764).
        assert invoice_classes[0]["properties"] == {"new": {}}


# ---------------------------------------------------------------------------
# Regression (#764): blueprint optimization regenerates the class from the BDA
# transform, which emits no x-aws-idp-* authoring keys. Saving it over the
# existing class erased the author's model pins, thresholds and routing hints —
# silently, surfacing only in the next document processed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyOptimizedSchemaPreservesAuthoredSettings:
    """Optimization may rewrite the schema, not the class's settings."""

    def test_authored_class_settings_survive_optimization(
        self, optimizer, mock_config_manager, mock_blueprint_service
    ):
        original = _make_class_schema(class_id="Invoice")
        mock_blueprint_service.transform_bda_blueprint_to_idp_class_schema.return_value = {
            "type": "object",
            "properties": {"Total": {"type": "number"}},
        }
        mock_config_manager.get_raw_configuration.return_value = {
            "classes": [
                {
                    "$id": "Invoice",
                    "x-aws-idp-document-type": "Invoice",
                    "type": "object",
                    "properties": {"Total": {"type": "string"}},
                    "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
                    "x-aws-idp-confidence-threshold": 0.9,
                    "x-aws-idp-multi-instance": True,
                    "x-aws-idp-exclude-from-processing": False,
                }
            ]
        }

        optimizer._apply_optimized_schema(
            "arn:bp-1", "arn:proj-1", {}, original, "default"
        )

        saved_config = mock_config_manager.save_raw_configuration.call_args[0][1]
        (saved,) = [c for c in saved_config["classes"] if c.get("$id") == "Invoice"]
        # The optimization's whole point — the new schema — still lands.
        assert saved["properties"] == {"Total": {"type": "number"}}
        assert saved["x-aws-idp-extraction-model"] == "us.amazon.nova-pro-v1:0"
        assert saved["x-aws-idp-confidence-threshold"] == 0.9
        assert saved["x-aws-idp-multi-instance"] is True
        assert saved["x-aws-idp-exclude-from-processing"] is False


# ---------------------------------------------------------------------------
# Tests: optimize (end-to-end orchestration)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOptimize:
    """Tests for BlueprintOptimizer.optimize end-to-end."""

    @patch("idp_common.bda.blueprint_optimizer.uuid")
    @patch("idp_common.bda.blueprint_optimizer.time")
    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_improved_path(
        self,
        mock_boto3,
        mock_time,
        mock_uuid,
        optimizer,
        mock_blueprint_service,
        mock_blueprint_creator,
        mock_bedrock_client,
        mock_s3_client,
        mock_config_manager,
    ):
        """Full improved path: create → invoke → poll → evaluate → apply."""
        # Setup uuid for output path
        mock_uuid.uuid4.return_value = MagicMock(hex="abcd1234abcd1234")

        # Setup STS for _invoke_optimization
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        # Setup bedrock client
        mock_bedrock_client.invoke_blueprint_optimization_async.return_value = {
            "invocationArn": "arn:inv-1"
        }
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "Success",
            "outputConfiguration": {
                "s3Object": {"s3Uri": "s3://bucket/output/results.json"}
            },
        }

        # Setup S3 results
        eval_data = _make_evaluation_comparison(
            before_exact=0.5,
            after_exact=0.9,
            before_f1=0.5,
            after_f1=0.9,
        )
        mock_s3_client.get_object.return_value = {
            "Body": io.BytesIO(json.dumps(eval_data).encode())
        }

        # Setup config manager
        mock_config_manager.get_raw_configuration.return_value = {"classes": []}

        callback = MagicMock()
        schema = _make_class_schema()

        result = optimizer.optimize(
            class_schema=schema,
            document_key="docs/invoice.pdf",
            ground_truth_key="gt/invoice.json",
            bucket="bucket",
            version="default",
            status_callback=callback,
        )

        assert result.status == OptimizationStatus.IMPROVED
        assert result.improved is True
        assert result.before_metrics.exact_match == 0.5
        assert result.after_metrics.exact_match == 0.9
        assert result.blueprint_arn is not None
        callback.assert_any_call("Creating BDA blueprint for optimization...")
        callback.assert_any_call("Optimizing blueprint with ground truth data...")

    @patch("idp_common.bda.blueprint_optimizer.uuid")
    @patch("idp_common.bda.blueprint_optimizer.time")
    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_no_improvement_path(
        self,
        mock_boto3,
        mock_time,
        mock_uuid,
        optimizer,
        mock_bedrock_client,
        mock_s3_client,
    ):
        """Should return NO_IMPROVEMENT when metrics don't improve."""
        mock_uuid.uuid4.return_value = MagicMock(hex="abcd1234abcd1234")
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.return_value = {
            "invocationArn": "arn:inv-1"
        }
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "Success",
            "outputConfiguration": {"s3Object": {"s3Uri": "s3://bucket/out/r.json"}},
        }

        eval_data = _make_evaluation_comparison(
            before_exact=0.9,
            after_exact=0.5,
            before_f1=0.9,
            after_f1=0.5,
        )
        mock_s3_client.get_object.return_value = {
            "Body": io.BytesIO(json.dumps(eval_data).encode())
        }

        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
        )

        assert result.status == OptimizationStatus.NO_IMPROVEMENT
        assert result.improved is False
        assert result.before_metrics is not None
        assert result.after_metrics is not None

    def test_blueprint_creation_failure(self, optimizer, mock_blueprint_service):
        """Should return FAILED when blueprint creation raises."""
        mock_blueprint_service.get_or_create_project_for_version.side_effect = (
            Exception("Project creation failed")
        )

        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
        )

        assert result.status == OptimizationStatus.FAILED
        assert "Project creation failed" in result.error_message

    @patch("idp_common.bda.blueprint_optimizer.uuid")
    @patch("idp_common.bda.blueprint_optimizer.time")
    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_timeout_path(
        self,
        mock_boto3,
        mock_time,
        mock_uuid,
        optimizer,
        mock_bedrock_client,
    ):
        """Should return TIMED_OUT when polling exceeds max duration."""
        mock_uuid.uuid4.return_value = MagicMock(hex="abcd1234abcd1234")
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.return_value = {
            "invocationArn": "arn:inv-1"
        }
        # First time.time() = 0 (start), second = 901 (exceeds 900)
        mock_time.time.side_effect = [0, OPTIMIZATION_MAX_DURATION_SECONDS + 1]
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "InProgress"
        }

        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
        )

        assert result.status == OptimizationStatus.TIMED_OUT
        assert result.blueprint_arn is not None

    @patch("idp_common.bda.blueprint_optimizer.uuid")
    @patch("idp_common.bda.blueprint_optimizer.time")
    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_service_error_path(
        self,
        mock_boto3,
        mock_time,
        mock_uuid,
        optimizer,
        mock_bedrock_client,
    ):
        """Should return FAILED when optimization ends with ServiceError."""
        mock_uuid.uuid4.return_value = MagicMock(hex="abcd1234abcd1234")
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.return_value = {
            "invocationArn": "arn:inv-1"
        }
        mock_time.time.return_value = 0
        mock_bedrock_client.get_blueprint_optimization_status.return_value = {
            "status": "ServiceError",
            "errorMessage": "Internal service error",
        }

        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
        )

        assert result.status == OptimizationStatus.FAILED
        assert "Internal service error" in result.error_message

    @patch("idp_common.bda.blueprint_optimizer.uuid")
    @patch("idp_common.bda.blueprint_optimizer.boto3")
    def test_invoke_optimization_failure(
        self,
        mock_boto3,
        mock_uuid,
        optimizer,
        mock_bedrock_client,
    ):
        """Should return FAILED when invoke_blueprint_optimization_async raises."""
        mock_uuid.uuid4.return_value = MagicMock(hex="abcd1234abcd1234")
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_boto3.Session.return_value.client.return_value = mock_sts

        mock_bedrock_client.invoke_blueprint_optimization_async.side_effect = Exception(
            "Invocation failed"
        )

        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
        )

        assert result.status == OptimizationStatus.FAILED
        assert "Invocation failed" in result.error_message
        assert result.blueprint_arn is not None

    def test_status_callback_not_required(self, optimizer, mock_blueprint_service):
        """Should work without a status_callback (None)."""
        mock_blueprint_service.get_or_create_project_for_version.side_effect = (
            Exception("fail")
        )

        # Should not raise even without callback
        result = optimizer.optimize(
            class_schema=_make_class_schema(),
            document_key="doc.pdf",
            ground_truth_key="gt.json",
            bucket="bucket",
            version="default",
            status_callback=None,
        )

        assert result.status == OptimizationStatus.FAILED
