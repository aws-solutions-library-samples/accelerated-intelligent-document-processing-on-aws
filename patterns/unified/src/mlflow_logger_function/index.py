# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MLflow Logger Lambda function for logging IDP metrics and experiments."""

import json
import logging
import os
import tempfile
import time
import mlflow

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "")

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Metric keys that should be logged as JSON artifacts instead of flat metrics.
# These are complex nested structures from Stickler aggregation and cost data.
ARTIFACT_KEYS = {
    "cost_breakdown",
    "weighted_overall_scores",
    "field_metrics",
}


def _configure_mlflow():
    """Configure MLflow tracking URI from environment."""
    if not MLFLOW_TRACKING_URI:
        raise ValueError("MLFLOW_TRACKING_URI environment variable is not set")
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)


def _flatten_metrics(metrics, prefix=""):
    """Flatten nested dicts into dot-separated keys with numeric values.

    Non-numeric leaf values are skipped.
    """
    flat = {}
    for key, value in metrics.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_metrics(value, prefix=full_key))
        elif isinstance(value, (int, float)):
            flat[full_key] = value
    return flat


def _sanitize_metric_key(key):
    """Sanitize a metric key for MLflow: replace /:-  with _, lowercase."""
    return key.replace("/", "_").replace(":", "_").replace("-", "_").lower()


def _extract_cost_metrics(cost_breakdown):
    """Extract estimated_cost from cost_breakdown as flat metrics.

    Returns dict like {"cost.ocr.textract_analyze_document_layout_pages": 0.02, ...}.
    """
    flat = {}
    for context, entries in cost_breakdown.items():
        if not isinstance(entries, dict):
            continue
        for entry_key, entry_data in entries.items():
            if not isinstance(entry_data, dict):
                continue
            estimated_cost = entry_data.get("estimated_cost")
            if isinstance(estimated_cost, (int, float)):
                sanitized = _sanitize_metric_key(f"cost.{context}.{entry_key}")
                flat[sanitized] = estimated_cost
    return flat


def _extract_field_metrics(field_metrics):
    """Extract cm_precision, cm_recall, cm_f1, cm_accuracy from field_metrics.

    Returns flat dict like {"field_name.cm_recall": 0.95, ...}.
    """
    flat = {}
    # accuracy_observations / accuracy_margin ride along because comparing per-field
    # accuracy across runs is precisely where sample size decides whether a difference
    # means anything.
    extract_keys = {
        "cm_precision",
        "cm_recall",
        "cm_f1",
        "cm_accuracy",
        "accuracy_observations",
        "accuracy_margin",
    }
    for field_name, field_data in field_metrics.items():
        if not isinstance(field_data, dict):
            continue
        for metric_key in extract_keys:
            value = field_data.get(metric_key)
            if isinstance(value, (int, float)):
                flat[f"{field_name}.{metric_key}"] = value
    return flat


def _extract_config_params(config):
    """Extract model IDs, inference params, and flags from IDP config as MLflow params.

    Returns a dict of short key-value pairs suitable for mlflow.log_params().
    Prompts and class definitions are returned separately for artifact logging.
    """
    params = {}
    prompts = {}
    classes = None

    if not config or not isinstance(config, dict):
        return params, prompts, classes

    # Unwrap nested "Config" key if present (DynamoDB record structure)
    if "Config" in config and isinstance(config["Config"], dict):
        config = config["Config"]

    # Stage-to-config key mapping for model and inference params
    stages = {
        "classification": "classification",
        "extraction": "extraction",
        "assessment": "assessment",
        "summarization": "summarization",
    }

    for stage_name, config_key in stages.items():
        stage_cfg = config.get(config_key)
        if not isinstance(stage_cfg, dict):
            continue

        # Model ID
        model = stage_cfg.get("model")
        if model:
            params[f"{stage_name}.model"] = str(model)

        # Inference params
        for p in ("temperature", "top_p", "top_k", "max_tokens"):
            val = stage_cfg.get(p)
            if val is not None:
                params[f"{stage_name}.{p}"] = str(val)

        # Enabled flag
        enabled = stage_cfg.get("enabled")
        if enabled is not None:
            params[f"{stage_name}.enabled"] = str(enabled)

        # Prompts (too long for params, collect for artifact)
        for prompt_key in ("system_prompt", "task_prompt"):
            prompt_val = stage_cfg.get(prompt_key)
            if prompt_val:
                prompts[f"{stage_name}.{prompt_key}"] = prompt_val

    # Evaluation model
    evaluation_cfg = config.get("evaluation")
    if isinstance(evaluation_cfg, dict):
        llm_method = evaluation_cfg.get("llm_method")
        if isinstance(llm_method, dict):
            model = llm_method.get("model")
            if model:
                params["evaluation.model"] = str(model)

    # OCR backend
    ocr_cfg = config.get("ocr")
    if isinstance(ocr_cfg, dict):
        backend = ocr_cfg.get("backend")
        if backend:
            params["ocr.backend"] = str(backend)

    # BDA flag
    use_bda = config.get("use_bda")
    if use_bda is not None:
        params["use_bda"] = str(use_bda)

    # Classification method
    cls_cfg = config.get("classification")
    if isinstance(cls_cfg, dict):
        cls_method = cls_cfg.get("classificationMethod")
        if cls_method:
            params["classification.method"] = str(cls_method)

    # HITL threshold (v0.6: top-level hitl) + confidence list batching (v0.6:
    # extraction.confidence.list_batch_size — granular assessment is retired)
    hitl_cfg = config.get("hitl")
    if isinstance(hitl_cfg, dict):
        threshold = hitl_cfg.get("confidence_threshold")
        if threshold is not None:
            params["assessment.confidence_threshold"] = str(threshold)
    extraction_cfg = config.get("extraction")
    confidence_cfg = (
        extraction_cfg.get("confidence") if isinstance(extraction_cfg, dict) else None
    )
    if isinstance(confidence_cfg, dict):
        list_batch_size = confidence_cfg.get("list_batch_size")
        if list_batch_size is not None:
            params["assessment.list_batch_size"] = str(list_batch_size)

    # Class definitions (log as artifact)
    classes_data = config.get("classes")
    if isinstance(classes_data, list) and classes_data:
        classes = classes_data

    return params, prompts, classes


def _log_artifact_json(name, data):
    """Write a dict as a JSON file and log it as an MLflow artifact."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"{name}_", delete=False
    ) as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        mlflow.log_artifact(f.name, artifact_path="metrics")
        logger.info("Logged artifact: %s", name)


def handler(event, context):
    """Lambda handler for logging metrics and parameters to MLflow.

    Expected event structure:
    {
        "experiment_name": "<test_run_id>",
        "metrics": {
            "overall_accuracy": 0.95,
            "average_confidence": 0.87,
            "total_cost": 1.23,
            "document_count": 100,
            "weighted_overall_scores": {...},
            "accuracy_breakdown": {...},
            "field_metrics": {...},
            "split_classification_metrics": {...},
            "cost_breakdown": {...},
            ...
        },
        "config": {
            "classification": {"model": "...", "temperature": "0.0", ...},
            "extraction": {"model": "...", ...},
            ...
        },
        "params": {"test_run_id": "abc-123"},
        "tags": {"source": "test_results_resolver"}
    }
    """
    logger.info("MLflow logger invoked: %s", json.dumps(event))

    _configure_mlflow()

    experiment_name = event.get("experiment_name", "idp-default")
    raw_metrics = event.get("metrics", {})
    params = event.get("params", {})
    tags = event.get("tags", {})
    config = event.get("config")

    # Extract config params, prompts, and class definitions
    config_params = {}
    config_artifacts = {}
    if config:
        config_params, prompts, classes = _extract_config_params(config)
        logger.info("Extracted %d config params", len(config_params))
        config_artifacts["full_config"] = config
        if prompts:
            config_artifacts["prompts"] = prompts
        if classes:
            config_artifacts["class_definitions"] = classes

    # Separate artifact-bound nested structures from flat-loggable metrics
    artifact_data = {}
    scalar_metrics = {}
    for key, value in raw_metrics.items():
        if key in ARTIFACT_KEYS and isinstance(value, dict) and value:
            artifact_data[key] = value
        else:
            scalar_metrics[key] = value

    # Flatten any remaining nested dicts that aren't in ARTIFACT_KEYS
    flat_metrics = _flatten_metrics(scalar_metrics)

    # Extract per-field accuracy metrics from field_metrics (also logged as artifact)
    field_metrics_data = artifact_data.get("field_metrics")
    if field_metrics_data:
        field_flat = _extract_field_metrics(field_metrics_data)
        logger.info("Extracted %d field-level metrics", len(field_flat))
        flat_metrics.update(field_flat)

    # Extract estimated_cost from cost_breakdown (also logged as artifact)
    cost_breakdown_data = artifact_data.get("cost_breakdown")
    if cost_breakdown_data:
        cost_flat = _extract_cost_metrics(cost_breakdown_data)
        logger.info("Extracted %d cost metrics", len(cost_flat))
        flat_metrics.update(cost_flat)

    logger.info("Setting MLflow experiment: %s", experiment_name)
    mlflow.set_experiment(experiment_name)
    run_name = f"{experiment_name}_{int(time.time())}"
    logger.info("Starting MLflow run: %s", run_name)
    with mlflow.start_run(run_name=run_name):
        run_id = mlflow.active_run().info.run_id
        logger.info("MLflow run started with ID: %s", run_id)

        # Merge base params with config params
        all_params = {**params, **config_params}
        if all_params:
            logger.info(
                "Logging %d params: %s", len(all_params), list(all_params.keys())
            )
            mlflow.log_params(all_params)
            logger.info("Params logged successfully")

        if flat_metrics:
            logger.info(
                "Logging %d flat metrics: %s",
                len(flat_metrics),
                list(flat_metrics.keys()),
            )
            mlflow.log_metrics(flat_metrics)
            logger.info("Flat metrics logged successfully")

        if tags:
            logger.info("Setting %d tags: %s", len(tags), list(tags.keys()))
            mlflow.set_tags(tags)
            logger.info("Tags set successfully")

        # Log complex nested structures as JSON artifacts
        if artifact_data:
            logger.info(
                "Logging %d metric artifacts: %s",
                len(artifact_data),
                list(artifact_data.keys()),
            )
        for name, data in artifact_data.items():
            _log_artifact_json(name, data)

        # Log config artifacts (prompts, class definitions)
        if config_artifacts:
            logger.info(
                "Logging %d config artifacts: %s",
                len(config_artifacts),
                list(config_artifacts.keys()),
            )
        for name, data in config_artifacts.items():
            _log_artifact_json(name, data)

        logger.info(
            "MLflow run %s complete - metrics: %d, params: %d, artifacts: %d",
            run_id,
            len(flat_metrics),
            len(all_params),
            len(artifact_data) + len(config_artifacts),
        )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "run_id": run_id,
                "experiment_name": experiment_name,
                "metrics_logged": len(flat_metrics),
                "params_logged": len(all_params),
                "artifacts_logged": list(artifact_data.keys())
                + list(config_artifacts.keys()),
            }
        ),
    }
