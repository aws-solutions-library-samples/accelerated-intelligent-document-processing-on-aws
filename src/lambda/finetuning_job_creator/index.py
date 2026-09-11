"""
Lambda function to create a Bedrock model customization (fine-tuning) job.

This function:
1. Creates a Bedrock CreateModelCustomizationJob request
2. Returns the job ARN for status tracking

Called by Step Functions as part of the fine-tuning workflow.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

import boto3
import yaml

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
TRACKING_TABLE_NAME = os.environ.get("TRACKING_TABLE", "")
FINETUNING_BUCKET = os.environ.get("FINETUNING_DATA_BUCKET", "")
BEDROCK_ROLE_ARN = os.environ.get("BEDROCK_FINETUNING_ROLE_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
FINETUNING_MODELS_CONFIG_KEY = os.environ.get(
    "FINETUNING_MODELS_CONFIG_KEY", "config/finetuning_models.yaml"
)

# Initialize clients
dynamodb = boto3.resource("dynamodb", region_name=REGION)
bedrock_client = boto3.client("bedrock", region_name=REGION)
s3_client = boto3.client("s3", region_name=REGION)

# DynamoDB key prefixes
FINETUNING_JOB_PREFIX = "finetuning#"

# Cache for loaded configuration
_config_cache: Dict[str, Any] = {}


def _load_finetuning_config() -> Dict[str, Any]:
    """
    Load fine-tuning models configuration from S3 or use defaults.
    
    The configuration is cached after first load to avoid repeated S3 calls.
    
    Returns:
        Dictionary containing model_mappings, default_hyperparameters, and model_capabilities
    """
    global _config_cache
    
    if _config_cache:
        return _config_cache
    
    # Default configuration (fallback if config file not found)
    # Nova 2.x models are recommended for fine-tuning
    default_config = {
        "model_mappings": {
            "amazon.nova-2-lite-v1:0": "amazon.nova-2-lite-v1:0:256k",
            "amazon.nova-2-pro-v1:0": "amazon.nova-2-pro-v1:0:256k",
            "amazon.nova-lite-v1:0": "amazon.nova-lite-v1:0:300k",
            "amazon.nova-pro-v1:0": "amazon.nova-pro-v1:0:300k",
        },
        "default_hyperparameters": {
            "epochCount": "2",
            "learningRate": "0.00001",
            "batchSize": "1",
        },
        "model_capabilities": {
            "amazon.nova-2-lite-v1:0": {"supports_validation": False},
            "amazon.nova-2-pro-v1:0": {"supports_validation": False},
            "amazon.nova-lite-v1:0": {"supports_validation": True},
            "amazon.nova-pro-v1:0": {"supports_validation": True},
        },
    }
    
    # Try to load from S3
    if FINETUNING_BUCKET and FINETUNING_MODELS_CONFIG_KEY:
        try:
            logger.info(
                f"Loading fine-tuning config from s3://{FINETUNING_BUCKET}/{FINETUNING_MODELS_CONFIG_KEY}"
            )
            response = s3_client.get_object(
                Bucket=FINETUNING_BUCKET, Key=FINETUNING_MODELS_CONFIG_KEY
            )
            config_content = response["Body"].read().decode("utf-8")
            config = yaml.safe_load(config_content)
            
            # Extract relevant sections
            _config_cache = {
                "model_mappings": config.get("model_mappings", default_config["model_mappings"]),
                "default_hyperparameters": config.get(
                    "default_hyperparameters", default_config["default_hyperparameters"]
                ),
                "model_capabilities": config.get(
                    "model_capabilities", default_config["model_capabilities"]
                ),
            }
            logger.info(f"Loaded fine-tuning config with {len(_config_cache['model_mappings'])} model mappings")
            return _config_cache
            
        except s3_client.exceptions.NoSuchKey:
            logger.warning(
                f"Config file not found at s3://{FINETUNING_BUCKET}/{FINETUNING_MODELS_CONFIG_KEY}, using defaults"
            )
        except Exception as e:
            logger.warning(f"Failed to load config from S3: {e}, using defaults")
    
    _config_cache = default_config
    return _config_cache


def _get_model_mappings() -> Dict[str, str]:
    """Get the model ID mappings from configuration."""
    config = _load_finetuning_config()
    return config.get("model_mappings", {})


def _get_default_hyperparameters() -> Dict[str, str]:
    """Get the default hyperparameters from configuration."""
    config = _load_finetuning_config()
    return config.get("default_hyperparameters", {})


def _model_supports_validation(model_id: str) -> bool:
    """
    Check if a model supports validation datasets during fine-tuning.
    
    Nova 2.x models do NOT support validation sets. Passing a validation
    dataset to the Bedrock API for these models results in:
    "Invalid input error: Nova 2.0 doesn't support validation set"
    
    Args:
        model_id: The model identifier (can be cross-region profile, standard ID, or mapped ID)
        
    Returns:
        True if the model supports validation datasets, False otherwise.
        Defaults to True for unknown models (backward compatible).
    """
    config = _load_finetuning_config()
    model_capabilities = config.get("model_capabilities", {})
    
    # Strip cross-region prefix for lookup (e.g., "us.amazon.nova-2-lite-v1:0" -> "amazon.nova-2-lite-v1:0")
    normalized = model_id
    if "." in normalized and not normalized.startswith("arn:"):
        parts = normalized.split(".", 1)
        if len(parts[0]) <= 3 and parts[0].isalpha():
            normalized = parts[1]
    
    # Strip context window suffix for lookup (e.g., "amazon.nova-2-lite-v1:0:256k" -> "amazon.nova-2-lite-v1:0")
    # Model capabilities are keyed by base model ID without context window suffix
    base_id = normalized
    # Count colons - base IDs have format "amazon.nova-2-lite-v1:0", mapped IDs have "amazon.nova-2-lite-v1:0:256k"
    colon_parts = base_id.split(":")
    if len(colon_parts) > 2:
        # Has context window suffix, strip it (keep only first two parts: "amazon.nova-2-lite-v1:0")
        base_id = ":".join(colon_parts[:2])
    
    # Look up capabilities
    capabilities = model_capabilities.get(base_id, {})
    supports_validation = capabilities.get("supports_validation", True)
    
    logger.info(
        f"Model '{model_id}' (base_id='{base_id}') supports_validation={supports_validation}"
    )
    
    return supports_validation


def _normalize_model_identifier(model_id: str) -> str:
    """
    Normalize model identifier for Bedrock fine-tuning API.
    
    This function:
    1. Converts cross-region inference profile IDs (e.g., us.amazon.nova-lite-v1:0)
       to standard model IDs (e.g., amazon.nova-lite-v1:0)
    2. Maps base model IDs to their fine-tuning-capable variants
       (e.g., amazon.nova-lite-v1:0 -> amazon.nova-lite-v1:0:300k)
    
    Args:
        model_id: The model identifier (could be cross-region profile, model ID, or ARN)
        
    Returns:
        Normalized model identifier suitable for CreateModelCustomizationJob
    """
    # If it's already an ARN, return as-is
    # Match any partition: "arn:aws-us-gov:bedrock:..." is just as much an
    # ARN as the commercial form, and treating it as a bare model id would
    # send a mangled value to CreateModelCustomizationJob.
    if model_id.startswith("arn:") and ":bedrock:" in model_id:
        return model_id
    
    normalized = model_id
    
    # Remove cross-region prefix (e.g., "us." or "eu." prefix)
    # Cross-region inference profiles have format: us.amazon.nova-lite-v1:0
    # Standard model IDs have format: amazon.nova-lite-v1:0
    if "." in normalized:
        parts = normalized.split(".", 1)
        # Check if first part looks like a region prefix (2-3 chars like "us", "eu", "ap")
        if len(parts[0]) <= 3 and parts[0].isalpha():
            logger.info(f"Removing cross-region prefix: '{model_id}' -> '{parts[1]}'")
            normalized = parts[1]
    
    # Map base model to fine-tuning-capable variant if needed
    model_mappings = _get_model_mappings()
    if normalized in model_mappings:
        mapped = model_mappings[normalized]
        logger.info(f"Mapping to fine-tuning-capable model: '{normalized}' -> '{mapped}'")
        normalized = mapped
    
    logger.info(f"Final normalized model identifier: '{normalized}'")
    return normalized


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda handler for creating Bedrock fine-tuning job."""
    logger.info(
        f"Creating finetuning job: jobId={event.get('jobId')}, "
        f"baseModel={event.get('baseModel')}"
    )

    job_id = event.get("jobId")
    base_model = event.get("baseModel")
    job_name = event.get("jobName")
    training_data_uri = event.get("trainingDataUri")
    validation_data_uri = event.get("validationDataUri")

    if not job_id:
        raise ValueError("jobId is required")
    if not base_model:
        raise ValueError("baseModel is required")
    if not job_name:
        raise ValueError("jobName is required")
    if not training_data_uri:
        raise ValueError("trainingDataUri is required")

    try:
        # Normalize the model identifier (convert cross-region profile to standard model ID)
        normalized_model = _normalize_model_identifier(base_model)
        logger.info(f"Using normalized model identifier: {normalized_model}")

        # Create unique names for the job and model
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        bedrock_job_name = f"idp-ft-{job_name}-{timestamp}"[:63]
        custom_model_name = f"idp-model-{job_name}-{timestamp}"[:63]

        # Sanitize names (alphanumeric, hyphens, underscores only)
        bedrock_job_name = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in bedrock_job_name
        )
        custom_model_name = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in custom_model_name
        )

        # Build job parameters
        job_params = {
            "customizationType": "FINE_TUNING",
            "jobName": bedrock_job_name,
            "customModelName": custom_model_name,
            "baseModelIdentifier": normalized_model,
            "roleArn": BEDROCK_ROLE_ARN,
            "trainingDataConfig": {
                "s3Uri": training_data_uri
            },
            "hyperParameters": _get_default_hyperparameters(),
        }

        # Add validation data if provided AND the model supports it.
        # Nova 2.x models do NOT support validation sets — passing one causes:
        # "Invalid input error: Nova 2.0 doesn't support validation set"
        if validation_data_uri and _model_supports_validation(base_model):
            job_params["validationDataConfig"] = {
                "validators": [
                    {"s3Uri": validation_data_uri}
                ]
            }
        elif validation_data_uri:
            logger.info(
                f"Skipping validation data for model '{base_model}' — "
                "model does not support validation sets"
            )

        # Add output data config
        output_uri = f"s3://{FINETUNING_BUCKET}/finetuning/{job_id}/output/"
        job_params["outputDataConfig"] = {
            "s3Uri": output_uri
        }

        # Create the fine-tuning job
        logger.info(f"Creating Bedrock fine-tuning job: {bedrock_job_name}")
        response = bedrock_client.create_model_customization_job(**job_params)

        bedrock_job_arn = response.get("jobArn", "")
        logger.info(f"Created Bedrock job: {bedrock_job_arn}")

        # Update DynamoDB with Bedrock job ARN
        _update_job_bedrock_arn(job_id, bedrock_job_arn, bedrock_job_name)

        return {
            "jobId": job_id,
            "bedrockJobArn": bedrock_job_arn,
            "bedrockJobName": bedrock_job_name,
            "customModelName": custom_model_name,
            "trainingDataUri": training_data_uri,
            "validationDataUri": validation_data_uri,
        }

    except Exception as e:
        logger.error(f"Error creating Bedrock job: {str(e)}", exc_info=True)
        _update_job_failure(job_id, str(e))
        raise


def _update_job_bedrock_arn(job_id: str, bedrock_job_arn: str, bedrock_job_name: str) -> None:
    """Update job with Bedrock job ARN."""
    table = dynamodb.Table(TRACKING_TABLE_NAME)

    table.update_item(
        Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"},
        UpdateExpression="SET bedrockJobArn = :arn, bedrockJobName = :name",
        ExpressionAttributeValues={
            ":arn": bedrock_job_arn,
            ":name": bedrock_job_name,
        },
    )

    logger.info(f"Updated job {job_id} with Bedrock job ARN: {bedrock_job_arn}")


def _update_job_failure(job_id: str, error_message: str) -> None:
    """Update job with failure information."""
    table = dynamodb.Table(TRACKING_TABLE_NAME)

    table.update_item(
        Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"},
        UpdateExpression="SET #status = :status, errorMessage = :err, errorStep = :step",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "FAILED",
            ":err": error_message,
            ":step": "CREATE_BEDROCK_JOB",
        },
    )

    logger.info(f"Updated job {job_id} with failure: {error_message}")