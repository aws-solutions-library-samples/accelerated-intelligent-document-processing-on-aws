# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the MLflow Logger Lambda function."""

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Import the mlflow_logger module using importlib to avoid mlflow dependency
_module_path = os.path.join(
    os.path.dirname(__file__),
    "../../../../patterns/unified/src/mlflow_logger_function/index.py",
)

# Mock mlflow before importing the module
mock_mlflow = MagicMock()
with patch.dict("sys.modules", {"mlflow": mock_mlflow}):
    spec = importlib.util.spec_from_file_location("mlflow_logger", _module_path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load mlflow_logger module")
    mlflow_logger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mlflow_logger)


# ---------------------------------------------------------------------------
# _flatten_metrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFlattenMetrics:
    def test_flat_dict(self):
        metrics = {"accuracy": 0.95, "cost": 1.23}
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {"accuracy": 0.95, "cost": 1.23}

    def test_nested_dict(self):
        metrics = {"level1": {"level2": 0.5}}
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {"level1.level2": 0.5}

    def test_deeply_nested(self):
        metrics = {"a": {"b": {"c": 42}}}
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {"a.b.c": 42}

    def test_skips_non_numeric(self):
        metrics = {"name": "test", "score": 0.9, "tags": ["a", "b"]}
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {"score": 0.9}

    def test_empty_dict(self):
        assert mlflow_logger._flatten_metrics({}) == {}

    def test_mixed_nested_and_flat(self):
        metrics = {
            "overall_accuracy": 0.92,
            "breakdown": {"class_a": 0.95, "class_b": 0.88},
        }
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {
            "overall_accuracy": 0.92,
            "breakdown.class_a": 0.95,
            "breakdown.class_b": 0.88,
        }

    def test_with_prefix(self):
        metrics = {"score": 0.9}
        result = mlflow_logger._flatten_metrics(metrics, prefix="test")
        assert result == {"test.score": 0.9}

    def test_integer_values(self):
        metrics = {"count": 5, "total": 100}
        result = mlflow_logger._flatten_metrics(metrics)
        assert result == {"count": 5, "total": 100}


# ---------------------------------------------------------------------------
# _sanitize_metric_key
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSanitizeMetricKey:
    def test_replaces_slash(self):
        assert mlflow_logger._sanitize_metric_key("a/b") == "a_b"

    def test_replaces_colon(self):
        assert mlflow_logger._sanitize_metric_key("a:b") == "a_b"

    def test_replaces_dash(self):
        assert mlflow_logger._sanitize_metric_key("a-b") == "a_b"

    def test_lowercases(self):
        assert mlflow_logger._sanitize_metric_key("MyKey") == "mykey"

    def test_combined(self):
        key = "bedrock/us.amazon.nova-2-lite-v1:0_inputTokens"
        result = mlflow_logger._sanitize_metric_key(key)
        assert result == "bedrock_us.amazon.nova_2_lite_v1_0_inputtokens"

    def test_no_changes_needed(self):
        assert mlflow_logger._sanitize_metric_key("simple_key") == "simple_key"


# ---------------------------------------------------------------------------
# _extract_cost_metrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractCostMetrics:
    def test_basic_extraction(self):
        cost_breakdown = {
            "OCR": {
                "textract/analyze_document-Layout_pages": {
                    "unit": "pages",
                    "value": 5,
                    "estimated_cost": 0.02,
                }
            }
        }
        result = mlflow_logger._extract_cost_metrics(cost_breakdown)
        assert "cost.ocr.textract_analyze_document_layout_pages" in result
        assert result["cost.ocr.textract_analyze_document_layout_pages"] == 0.02

    def test_multiple_contexts(self):
        cost_breakdown = {
            "OCR": {
                "lambda/requests_invocations": {
                    "estimated_cost": 0.0,
                }
            },
            "Classification": {
                "bedrock/us.amazon.nova-2-lite-v1:0_inputTokens": {
                    "estimated_cost": 0.0026,
                }
            },
        }
        result = mlflow_logger._extract_cost_metrics(cost_breakdown)
        assert len(result) == 2
        assert (
            "cost.classification.bedrock_us.amazon.nova_2_lite_v1_0_inputtokens"
            in result
        )

    def test_skips_non_dict_entries(self):
        cost_breakdown = {"OCR": "not_a_dict"}
        result = mlflow_logger._extract_cost_metrics(cost_breakdown)
        assert result == {}

    def test_skips_missing_estimated_cost(self):
        cost_breakdown = {"OCR": {"some_entry": {"unit": "pages", "value": 5}}}
        result = mlflow_logger._extract_cost_metrics(cost_breakdown)
        assert result == {}

    def test_empty_breakdown(self):
        assert mlflow_logger._extract_cost_metrics({}) == {}


# ---------------------------------------------------------------------------
# _extract_field_metrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractFieldMetrics:
    def test_extracts_cm_metrics(self):
        field_metrics = {
            "PayDate": {
                "cm_precision": 1.0,
                "cm_recall": 0.95,
                "cm_f1": 0.97,
                "cm_accuracy": 0.98,
                "other_metric": 0.5,
            }
        }
        result = mlflow_logger._extract_field_metrics(field_metrics)
        assert result["PayDate.cm_precision"] == 1.0
        assert result["PayDate.cm_recall"] == 0.95
        assert result["PayDate.cm_f1"] == 0.97
        assert result["PayDate.cm_accuracy"] == 0.98
        assert "PayDate.other_metric" not in result

    def test_multiple_fields(self):
        field_metrics = {
            "FieldA": {"cm_recall": 0.9},
            "FieldB": {"cm_recall": 0.8},
        }
        result = mlflow_logger._extract_field_metrics(field_metrics)
        assert result["FieldA.cm_recall"] == 0.9
        assert result["FieldB.cm_recall"] == 0.8

    def test_skips_non_numeric(self):
        field_metrics = {"Field": {"cm_recall": "not_a_number"}}
        result = mlflow_logger._extract_field_metrics(field_metrics)
        assert result == {}

    def test_skips_non_dict_field_data(self):
        field_metrics = {"Field": "not_a_dict"}
        result = mlflow_logger._extract_field_metrics(field_metrics)
        assert result == {}

    def test_empty(self):
        assert mlflow_logger._extract_field_metrics({}) == {}


# ---------------------------------------------------------------------------
# _extract_config_params
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractConfigParams:
    def test_unwraps_config_key(self):
        """Config from DynamoDB has a top-level 'Config' wrapper."""
        config = {
            "Config": {
                "classification": {"model": "nova-lite", "temperature": "0.0"},
            }
        }
        params, prompts, classes = mlflow_logger._extract_config_params(config)
        assert params["classification.model"] == "nova-lite"
        assert params["classification.temperature"] == "0.0"

    def test_all_stages(self):
        config = {
            "classification": {"model": "model-a", "top_p": "0.1"},
            "extraction": {"model": "model-b", "max_tokens": "4096"},
            "assessment": {"model": "model-c", "enabled": True},
            "summarization": {"model": "model-d", "top_k": "5"},
        }
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["classification.model"] == "model-a"
        assert params["extraction.model"] == "model-b"
        assert params["extraction.max_tokens"] == "4096"
        assert params["assessment.model"] == "model-c"
        assert params["assessment.enabled"] == "True"
        assert params["summarization.model"] == "model-d"
        assert params["summarization.top_k"] == "5"

    def test_evaluation_model(self):
        config = {"evaluation": {"llm_method": {"model": "eval-model"}}}
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["evaluation.model"] == "eval-model"

    def test_ocr_backend(self):
        config = {"ocr": {"backend": "textract"}}
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["ocr.backend"] == "textract"

    def test_use_bda(self):
        config = {"use_bda": False}
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["use_bda"] == "False"

    def test_classification_settings(self):
        """The settings that make one experiment distinguishable from another.

        ⚠️ **The config handed in here has to be one production can produce.** A
        test that passes `classificationMethod` and asserts a
        `classification.method` parameter keeps passing after the key is removed,
        because it exercises a shape no deployment stores — and the parameter it
        checks for can be absent from every real run with nothing noticing. These
        three are what a stored config carries.
        """
        config = {
            "classification": {
                "granularity": "packet",
                "strategy": "two_stage",
                "modality": {"text": True, "images": False},
            }
        }
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["classification.granularity"] == "packet"
        assert params["classification.strategy"] == "two_stage"
        assert params["classification.modality.text"] == "True"
        assert params["classification.modality.images"] == "False"

    @pytest.mark.parametrize(
        "modality",
        [
            # The raw YAML and config-form spelling.
            {"text": "", "images": ""},
            # ⚠️ The spelling a VALIDATED config carries, and the one that
            # matters: `ClassificationModalityConfig` types both flags as
            # `Optional[bool]`, so `IDPConfig(...).model_dump()` writes `None`.
            # A guard of `if value is not None` logs nothing for exactly this
            # shape — which is what the resolver sends — so the inferring run
            # becomes indistinguishable from one where the key was never
            # written, the outcome this parameter exists to prevent.
            {"text": None, "images": None},
            # Mixed, for the same reason in one flag.
            {"text": None, "images": ""},
        ],
    )
    def test_an_inferred_modality_is_logged_as_inferred(self, modality):
        """An unset flag means "infer from the prompt's placeholders", which is a
        real setting rather than an absent one — so a run using the inference
        must not look identical to one where the key was never written."""
        params, _, _ = mlflow_logger._extract_config_params(
            {"classification": {"modality": modality}}
        )
        assert params["classification.modality.text"] == "infer"
        assert params["classification.modality.images"] == "infer"

    def test_a_validated_config_dump_logs_both_flags(self):
        """End to end against the real model, so the shape is not guessed."""
        from idp_common.config.merge_utils import merge_config_with_defaults
        from idp_common.config.models import IDPConfig

        dumped = IDPConfig(**merge_config_with_defaults({}, validate=False)).model_dump(
            mode="python"
        )
        params, _, _ = mlflow_logger._extract_config_params(dumped)
        assert params["classification.modality.text"] == "infer"
        assert params["classification.modality.images"] == "infer"
        assert params["classification.granularity"] == "page"

    def test_the_removed_key_logs_nothing(self):
        """A config still carrying `classificationMethod` reaches this function
        only if it bypassed the migration; it must not resurrect the old
        parameter name, which would make two differently-configured runs look
        the same."""
        config = {
            "classification": {
                "classificationMethod": "multimodalPageLevelClassification"
            }
        }
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert "classification.method" not in params

    def test_assessment_confidence_params(self):
        # v0.6: HITL threshold is top-level; confidence knobs live under
        # extraction.confidence (granular assessment has been retired).
        config = {
            "hitl": {"confidence_threshold": "0.8"},
            "extraction": {"confidence": {"list_batch_size": "25"}},
        }
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert params["assessment.confidence_threshold"] == "0.8"
        assert params["assessment.list_batch_size"] == "25"

    def test_prompts_extracted(self):
        config = {
            "classification": {
                "system_prompt": "You are a classifier.",
                "task_prompt": "Classify this document.",
            }
        }
        _, prompts, _ = mlflow_logger._extract_config_params(config)
        assert prompts["classification.system_prompt"] == "You are a classifier."
        assert prompts["classification.task_prompt"] == "Classify this document."

    def test_classes_extracted(self):
        config = {
            "classes": [
                {"$id": "Payslip", "type": "object"},
                {"$id": "W2", "type": "object"},
            ]
        }
        _, _, classes = mlflow_logger._extract_config_params(config)
        assert len(classes) == 2
        assert classes[0]["$id"] == "Payslip"

    def test_empty_classes_returns_none(self):
        config = {"classes": []}
        _, _, classes = mlflow_logger._extract_config_params(config)
        assert classes is None

    def test_none_config(self):
        params, prompts, classes = mlflow_logger._extract_config_params(None)
        assert params == {}
        assert prompts == {}
        assert classes is None

    def test_missing_stages_skipped(self):
        """Only present stages should produce params."""
        config = {"classification": {"model": "nova"}}
        params, _, _ = mlflow_logger._extract_config_params(config)
        assert "extraction.model" not in params
        assert "assessment.model" not in params
        assert "summarization.model" not in params


# ---------------------------------------------------------------------------
# handler (integration-style with mocked mlflow)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandler:
    def _make_event(self, **overrides):
        event = {
            "experiment_name": "test-run-001",
            "metrics": {
                "overall_accuracy": 0.92,
                "document_count": 5,
                "total_cost": 0.089,
            },
            "params": {"test_run_id": "test-run-001"},
            "tags": {"source": "test_results_resolver"},
        }
        event.update(overrides)
        return event

    @patch.dict(
        os.environ,
        {
            "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test"
        },
    )
    @patch.object(
        mlflow_logger,
        "MLFLOW_TRACKING_URI",
        "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test",
    )
    @patch.object(mlflow_logger, "mlflow")
    def test_handler_returns_200(self, mock_ml):
        mock_ml.active_run.return_value.info.run_id = "run-abc"
        mock_ml.start_run.return_value.__enter__ = lambda s: s
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)

        result = mlflow_logger.handler(self._make_event(), {})
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["run_id"] == "run-abc"
        assert body["metrics_logged"] == 3

    @patch.dict(
        os.environ,
        {
            "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test"
        },
    )
    @patch.object(
        mlflow_logger,
        "MLFLOW_TRACKING_URI",
        "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test",
    )
    @patch.object(mlflow_logger, "mlflow")
    def test_handler_logs_config_params(self, mock_ml):
        mock_ml.active_run.return_value.info.run_id = "run-abc"
        mock_ml.start_run.return_value.__enter__ = lambda s: s
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)

        event = self._make_event(
            config={
                "Config": {
                    "classification": {"model": "nova-lite", "temperature": "0.0"},
                    "use_bda": False,
                }
            }
        )
        result = mlflow_logger.handler(event, {})
        body = json.loads(result["body"])
        # test_run_id + classification.model + classification.temperature + use_bda
        assert body["params_logged"] == 4

    @patch.dict(
        os.environ,
        {
            "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test"
        },
    )
    @patch.object(
        mlflow_logger,
        "MLFLOW_TRACKING_URI",
        "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test",
    )
    @patch.object(mlflow_logger, "mlflow")
    def test_handler_separates_artifacts(self, mock_ml):
        mock_ml.active_run.return_value.info.run_id = "run-abc"
        mock_ml.start_run.return_value.__enter__ = lambda s: s
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)

        event = self._make_event(
            metrics={
                "overall_accuracy": 0.9,
                "cost_breakdown": {"OCR": {"item": {"estimated_cost": 0.01}}},
                "field_metrics": {"PayDate": {"cm_recall": 1.0}},
                "weighted_overall_scores": {"Payslip": 0.95},
            }
        )
        result = mlflow_logger.handler(event, {})
        body = json.loads(result["body"])
        assert "cost_breakdown" in body["artifacts_logged"]
        assert "field_metrics" in body["artifacts_logged"]
        assert "weighted_overall_scores" in body["artifacts_logged"]

    @patch.dict(
        os.environ,
        {
            "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test"
        },
    )
    @patch.object(
        mlflow_logger,
        "MLFLOW_TRACKING_URI",
        "arn:aws:sagemaker:us-west-2:123456789:mlflow-tracking-server/test",
    )
    @patch.object(mlflow_logger, "mlflow")
    def test_handler_no_config(self, mock_ml):
        mock_ml.active_run.return_value.info.run_id = "run-abc"
        mock_ml.start_run.return_value.__enter__ = lambda s: s
        mock_ml.start_run.return_value.__exit__ = MagicMock(return_value=False)

        result = mlflow_logger.handler(self._make_event(), {})
        body = json.loads(result["body"])
        # Only test_run_id param, no config params
        assert body["params_logged"] == 1
        # No config artifacts
        assert "full_config" not in body["artifacts_logged"]
        assert "prompts" not in body["artifacts_logged"]
