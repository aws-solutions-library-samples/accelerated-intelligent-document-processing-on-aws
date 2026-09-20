"""
GraphQL resolver for fine-tuning job operations.

Handles:
- listFinetuningJobs: List all fine-tuning jobs with pagination
- getFinetuningJob: Get a single fine-tuning job by ID
- validateTestSetForFinetuning: Validate a test set for fine-tuning
- createFinetuningJob: Create and start a new fine-tuning job
- deleteFinetuningJob: Delete a fine-tuning job
- updateFinetuningJobStatus: Update job status (internal use)
- listAvailableModels: List available base and custom models
"""

import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3

from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
TRACKING_TABLE_NAME = os.environ.get("TRACKING_TABLE", "")  # Match CloudFormation env var name
STEP_FUNCTIONS_ARN = os.environ.get("FINETUNING_STATE_MACHINE_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Initialize clients
dynamodb = boto3.resource("dynamodb", region_name=REGION)
sfn_client = boto3.client("stepfunctions", region_name=REGION)
bedrock_client = boto3.client("bedrock", region_name=REGION)

# DynamoDB key prefixes
FINETUNING_JOB_PREFIX = "finetuning#"
FINETUNING_JOBS_GSI_PK = "finetuning#jobs"

# Page size: a default AND a ceiling the caller cannot raise.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Bounds on the filtered scan that finds the jobs. The filter is sparse against a
# table that holds a row per document in the deployment, so the walk's cost is
# proportional to the whole document history rather than to the number of jobs.
#
# The binding constraint here is READ CAPACITY, not time: a filtered Scan is charged
# on bytes EXAMINED, not on rows matched. Measured against a real 4.6 MB tracking
# table from in-region compute: 113 RCU mean per page (142 max), 34 ms mean latency —
# so the cost of a page is ~3,000x its latency in significance, and a time-derived cap
# would be meaningless.
#
# 10 pages ~= 1,130 RCU, which is the same per-request read budget the date-range cap
# allows (2,191 queries x 0.5 RCU ~= 1,100). That parity is the derivation: this
# operation's policy is `ANY`, so it is reachable by any authenticated caller
# including one in no group, and it should not be able to buy an order of magnitude
# more capacity than the operation that IS group-gated. A caller who needs more
# follows `nextToken`.
MAX_SCAN_PAGES = 10
SCAN_TIME_RESERVE_MS = 5_000

# Supported base models for fine-tuning (Nova 2.x recommended)
SUPPORTED_BASE_MODELS = [
    {"id": "us.amazon.nova-2-lite-v1:0", "name": "Nova 2 Lite", "provider": "Amazon"},
    {"id": "us.amazon.nova-2-pro-v1:0", "name": "Nova 2 Pro", "provider": "Amazon"},
]


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event: Dict[str, Any], context: Any) -> Any:
    """Lambda handler for fine-tuning job resolver."""
    logger.info(
        "Received event: %s",
        json.dumps(sanitize_event_for_logging(event), cls=DecimalEncoder),
    )

    # AppSync default Lambda resolver format uses info.fieldName
    # Also support direct fieldName for custom mapping templates
    field_name = event.get("fieldName", "")
    if not field_name and "info" in event:
        field_name = event.get("info", {}).get("fieldName", "")
    
    arguments = event.get("arguments", {})

    # Defense-in-depth RBAC: createFinetuningJob / deleteFinetuningJob are
    # Admin+Author operations. The schema enforces this via
    # @aws_cognito_user_pools(cognito_groups), but we also gate it server-side so
    # a Viewer can never reach it even if the schema directive is missing or
    # misconfigured. (updateFinetuningJobStatus is @aws_iam / backend-only and
    # carries no Cognito identity, so it is intentionally not gated here.)
    if field_name in ("createFinetuningJob", "deleteFinetuningJob"):
        groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
        if isinstance(groups, str):
            groups = [groups]
        if not ({"Admin", "Author"}.intersection(groups)):
            logger.warning(
                f"Forbidden: caller (groups={groups}) attempted {field_name}"
            )
            raise PermissionError(
                f"Unauthorized: {field_name} requires Admin or Author group"
            )

    try:
        if field_name == "listFinetuningJobs":
            return list_finetuning_jobs(arguments, context)
        elif field_name == "getFinetuningJob":
            return get_finetuning_job(arguments.get("jobId"))
        elif field_name == "validateTestSetForFinetuning":
            return validate_test_set_for_finetuning(arguments.get("testSetId"))
        elif field_name == "createFinetuningJob":
            return start_finetuning_job(arguments.get("input", {}))
        elif field_name == "deleteFinetuningJob":
            return delete_finetuning_job(arguments.get("jobId"))
        elif field_name == "updateFinetuningJobStatus":
            return update_finetuning_job_status(
                arguments.get("jobId"),
                arguments.get("status"),
                arguments.get("errorMessage"),
            )
        elif field_name == "listAvailableModels":
            return list_available_models()
        else:
            raise ValueError(f"Unknown field: {field_name}")
    except Exception as e:
        logger.error(f"Error handling {field_name}: {str(e)}", exc_info=True)
        raise


def _clamped_limit(requested: Any) -> int:
    """A caller-supplied page size bounded to ``[1, MAX_PAGE_SIZE]``.

    `arguments.get("limit", 50)` was a default, not a ceiling: nothing between the
    caller and this value bounds it (the dispatcher's validation spec carries type
    shapes only).
    """
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(value, MAX_PAGE_SIZE))


def _scan_budget_exhausted(pages: int, context: Any) -> Optional[str]:
    """Whether the scan walk must stop now, and why.

    Mirrors ``_count_budget_exhausted`` in list_documents_gsi_resolver — a page cap
    for the common case and a remaining-invocation reserve for the case where each
    page is slow.
    """
    if pages >= MAX_SCAN_PAGES:
        return f"page cap of {MAX_SCAN_PAGES} reached"
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        try:
            left = int(remaining())
        except (TypeError, ValueError, AttributeError):
            return None
        if left < SCAN_TIME_RESERVE_MS:
            return f"only {left}ms of the invocation left"
    return None


def list_finetuning_jobs(
    arguments: Dict[str, Any], context: Any = None
) -> Dict[str, Any]:
    """List all fine-tuning jobs with pagination.

    Uses scan with filter since fine-tuning jobs are relatively few
    and the GSI1 index may not exist on all deployments.
    Jobs are stored with GSI1PK/GSI1SK for future GSI support.

    The scan paginates through the table until enough matching items
    are collected or the entire table has been scanned, because
    DynamoDB's ``Limit`` caps items *evaluated* (before filtering),
    not items *returned*.
    """
    limit = _clamped_limit(arguments.get("limit"))
    next_token = arguments.get("nextToken")

    table = dynamodb.Table(TRACKING_TABLE_NAME)

    from boto3.dynamodb.conditions import Attr

    filter_expr = Attr("PK").begins_with(FINETUNING_JOB_PREFIX) & Attr("SK").eq(
        "metadata"
    )

    # Collect matching items by paginating through the scan.
    # Fine-tuning jobs are few relative to the rest of the table,
    # so a single scan page may not contain any matches.
    items: List[Dict[str, Any]] = []
    scan_kwargs: Dict[str, Any] = {"FilterExpression": filter_expr}

    if next_token:
        scan_kwargs["ExclusiveStartKey"] = json.loads(next_token)

    pages = 0
    result_next_token = None
    while True:
        response = table.scan(**scan_kwargs)
        pages += 1

        for item in response.get("Items", []):
            items.append(_format_job_for_graphql(item))

        # Stop if we've scanned the whole table
        if "LastEvaluatedKey" not in response:
            break

        # Enough for the page the caller asked for. Checked here rather than by
        # truncating afterwards: `nextToken` resumes after the last SCANNED page, so
        # anything collected and then truncated away is returned by no page at all.
        if len(items) >= limit:
            result_next_token = json.dumps(
                response["LastEvaluatedKey"], cls=DecimalEncoder
            )
            break

        # BOUND the walk. Fine-tuning jobs are sparse in the TrackingTable, which
        # holds a row per document in the deployment, so "scan until the whole
        # table is exhausted" is proportional to the deployment's entire document
        # history — and `listFinetuningJobs` is an `ANY` operation, reachable by
        # any authenticated caller including one in no group. `ExclusiveStartKey`
        # was already accepted as `nextToken` but never returned, so there was no
        # way to ask for less than everything.
        stop = _scan_budget_exhausted(pages, context)
        if stop:
            result_next_token = json.dumps(
                response["LastEvaluatedKey"], cls=DecimalEncoder
            )
            logger.warning(
                "listFinetuningJobs stopped after %d scan pages (%s); returning a "
                "nextToken so the caller can resume",
                pages,
                stop,
            )
            break

        # Continue scanning from where we left off
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    # Sort by createdAt descending (most recent first). Note the ordering is over
    # what THIS call scanned, not over all jobs: a scan cursor cannot be resumed in
    # createdAt order. A caller following nextToken gets every job, page by page, but
    # the pages are not globally sorted between them.
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

    # Truncate ONLY when the scan completed. With a nextToken outstanding the token
    # resumes after the last scanned page, so truncating here would drop rows that no
    # subsequent page ever returns — a token that looks like faithful pagination and
    # is not. Returning slightly more than `limit` is the honest alternative.
    if result_next_token is None:
        items = items[:limit]

    return {"items": items, "nextToken": result_next_token}


def get_finetuning_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get a single fine-tuning job by ID."""
    if not job_id:
        raise ValueError("jobId is required")

    table = dynamodb.Table(TRACKING_TABLE_NAME)

    response = table.get_item(
        Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"}
    )

    item = response.get("Item")
    if not item:
        return None

    return _format_job_for_graphql(item)


def validate_test_set_for_finetuning(test_set_id: str) -> Dict[str, Any]:
    """Validate a test set for fine-tuning suitability."""
    if not test_set_id:
        raise ValueError("testSetId is required")

    table = dynamodb.Table(TRACKING_TABLE_NAME)

    # Get test set metadata
    test_set_response = table.get_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}
    )

    test_set = test_set_response.get("Item")
    if not test_set:
        return {
            "isValid": False,
            "documentCount": 0,
            "classCount": 0,
            "classDistribution": {},
            "trainCount": 0,
            "validationCount": 0,
            "warnings": [],
            "errors": ["Test set not found"],
        }

    # Get document count from test set metadata
    # Test sets store fileCount in metadata, not individual file records
    document_count = int(test_set.get("fileCount", 0))
    
    # For fine-tuning, we don't require class distribution analysis
    # since the training data will be generated from extraction results
    # Just validate that we have enough documents
    class_distribution: Dict[str, int] = {}
    class_count = 1  # Assume at least one class (extraction task)

    # Calculate train/validation split (90/10)
    train_count = int(document_count * 0.9)
    validation_count = document_count - train_count

    # Validation checks
    warnings: List[str] = []
    errors: List[str] = []

    # Minimum document count
    if document_count < 10:
        errors.append(
            f"Insufficient documents: {document_count}. Minimum 10 required for fine-tuning."
        )

    # Note: We no longer require multiple classes since fine-tuning
    # is for extraction tasks, not classification

    # Minimum validation count
    if validation_count < 2:
        warnings.append(
            "Very few validation examples. Results may not be representative."
        )

    is_valid = len(errors) == 0

    return {
        "isValid": is_valid,
        "documentCount": document_count,
        "classCount": class_count,
        "classDistribution": class_distribution,
        "trainCount": train_count,
        "validationCount": validation_count,
        "warnings": warnings,
        "errors": errors,
    }


def start_finetuning_job(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Start a new fine-tuning job."""
    test_set_id = input_data.get("testSetId")
    base_model = input_data.get("baseModel")
    job_name = input_data.get("jobName")
    train_split = input_data.get("trainSplit", 0.9)

    if not test_set_id:
        raise ValueError("testSetId is required")
    if not base_model:
        raise ValueError("baseModel is required")
    if not job_name:
        raise ValueError("jobName is required")

    # Validate trainSplit range
    if not (0.0 < train_split < 1.0):
        raise ValueError("trainSplit must be between 0.0 and 1.0 (exclusive)")

    # Validate base model
    valid_model_ids = [m["id"] for m in SUPPORTED_BASE_MODELS]
    if base_model not in valid_model_ids:
        raise ValueError(f"Invalid baseModel. Must be one of: {valid_model_ids}")

    # Validate test set first
    validation_result = validate_test_set_for_finetuning(test_set_id)
    if not validation_result["isValid"]:
        raise ValueError(
            f"Test set validation failed: {', '.join(validation_result['errors'])}"
        )

    # Get test set name
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    test_set_response = table.get_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}
    )
    test_set = test_set_response.get("Item", {})
    test_set_name = test_set.get("name", test_set_id)

    # Generate job ID
    job_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat() + "Z"

    # Create job record in DynamoDB
    job_item = {
        "PK": f"{FINETUNING_JOB_PREFIX}{job_id}",
        "SK": "metadata",
        "id": job_id,
        "jobName": job_name,
        "testSetId": test_set_id,
        "testSetName": test_set_name,
        "baseModel": base_model,
        "status": "VALIDATING",
        "createdAt": created_at,
        "trainSplit": Decimal(str(train_split)),
        # GSI for listing
        "GSI1PK": FINETUNING_JOBS_GSI_PK,
        "GSI1SK": f"{created_at}#{job_id}",
    }

    table.put_item(Item=job_item)

    # Start Step Functions execution
    if STEP_FUNCTIONS_ARN:
        sfn_input = {
            "jobId": job_id,
            "testSetId": test_set_id,
            "baseModel": base_model,
            "jobName": job_name,
            "trainSplit": train_split,
        }

        sfn_response = sfn_client.start_execution(
            stateMachineArn=STEP_FUNCTIONS_ARN,
            name=f"finetuning-{job_id}",
            input=json.dumps(sfn_input),
        )

        # Update job with execution ARN
        table.update_item(
            Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"},
            UpdateExpression="SET stepFunctionsExecutionArn = :arn",
            ExpressionAttributeValues={":arn": sfn_response["executionArn"]},
        )

        job_item["stepFunctionsExecutionArn"] = sfn_response["executionArn"]

    return _format_job_for_graphql(job_item)


def delete_finetuning_job(job_id: str) -> bool:
    """Delete a fine-tuning job."""
    if not job_id:
        raise ValueError("jobId is required")

    table = dynamodb.Table(TRACKING_TABLE_NAME)

    # Get raw job item from DynamoDB to check status and get deployment ARN
    response = table.get_item(
        Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"}
    )
    item = response.get("Item")
    if not item:
        raise ValueError(f"Job not found: {job_id}")

    status = item.get("status", "")

    # Don't allow deletion of running jobs
    if status in ["VALIDATING", "GENERATING_DATA", "TRAINING", "DEPLOYING"]:
        raise ValueError(
            f"Cannot delete job in {status} status. Wait for completion or failure."
        )

    # Delete custom model deployment if exists
    deployment_arn = item.get("customModelDeploymentArn") or item.get(
        "provisionedModelArn"
    )
    if deployment_arn:
        try:
            bedrock_client.delete_custom_model_deployment(
                customModelDeploymentIdentifier=deployment_arn
            )
            logger.info(f"Deleted custom model deployment: {deployment_arn}")
        except Exception as e:
            logger.warning(f"Failed to delete deployment {deployment_arn}: {e}")

    # Delete job record
    table.delete_item(Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"})

    logger.info(f"Deleted fine-tuning job: {job_id}")
    return True


def update_finetuning_job_status(
    job_id: str, status: str, error_message: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update fine-tuning job status (called by Step Functions)."""
    if not job_id:
        raise ValueError("jobId is required")
    if not status:
        raise ValueError("status is required")

    valid_statuses = [
        "VALIDATING",
        "GENERATING_DATA",
        "TRAINING",
        "DEPLOYING",
        "COMPLETED",
        "FAILED",
    ]
    if status not in valid_statuses:
        raise ValueError(f"Invalid status. Must be one of: {valid_statuses}")

    table = dynamodb.Table(TRACKING_TABLE_NAME)

    update_expression = "SET #status = :status"
    expression_values: Dict[str, Any] = {":status": status}
    expression_names = {"#status": "status"}

    # Add timestamp based on status
    now = datetime.utcnow().isoformat() + "Z"
    if status == "TRAINING":
        update_expression += ", trainingStartedAt = :ts"
        expression_values[":ts"] = now
    elif status == "DEPLOYING":
        update_expression += ", trainingCompletedAt = :ts"
        expression_values[":ts"] = now
    elif status == "COMPLETED":
        update_expression += ", deploymentCompletedAt = :ts"
        expression_values[":ts"] = now

    if error_message:
        update_expression += ", errorMessage = :err"
        expression_values[":err"] = error_message

    table.update_item(
        Key={"PK": f"{FINETUNING_JOB_PREFIX}{job_id}", "SK": "metadata"},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=expression_names,
        ExpressionAttributeValues=expression_values,
    )

    return get_finetuning_job(job_id)


def list_available_models() -> Dict[str, Any]:
    """List available base models and custom model deployments."""
    # Base models (hardcoded list)
    base_models = [
        {"id": m["id"], "name": m["name"], "provider": m["provider"]}
        for m in SUPPORTED_BASE_MODELS
    ]

    # Custom model deployments from Bedrock
    custom_models = []
    try:
        paginator = bedrock_client.get_paginator("list_custom_model_deployments")
        for page in paginator.paginate():
            for deployment in page.get("customModelDeploymentSummaries", []):
                if deployment.get("status") == "Active":
                    custom_models.append(
                        {
                            "id": deployment.get("customModelDeploymentArn", ""),
                            "name": deployment.get("modelDeploymentName", ""),
                            "baseModel": _get_base_model_name(
                                deployment.get("modelArn", "")
                            ),
                            "status": deployment.get("status", ""),
                        }
                    )
    except Exception as e:
        logger.warning(f"Failed to list custom model deployments: {e}")

    return {"baseModels": base_models, "customModels": custom_models}


def _get_base_model_name(model_arn: str) -> str:
    """Get the friendly name of the base model from a custom model ARN."""
    try:
        response = bedrock_client.get_custom_model(modelIdentifier=model_arn)
        base_model_arn = response.get("baseModelArn", "")

        for model in SUPPORTED_BASE_MODELS:
            if model["id"] in base_model_arn:
                return model["name"]

        return "Unknown"
    except Exception as e:
        logger.warning(f"Failed to get base model for {model_arn}: {e}")
        return "Unknown"


# Valid status values for the GraphQL FinetuningJobStatus enum
VALID_GRAPHQL_STATUSES = {
    "PENDING",
    "VALIDATING",
    "GENERATING_DATA",
    "TRAINING",
    "DEPLOYING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "STOPPING",
    "STOPPED",
}


def _format_job_for_graphql(item: Dict[str, Any]) -> Dict[str, Any]:
    """Format a DynamoDB item for GraphQL response."""
    # Determine the most recent update time
    updated_at = (
        item.get("deploymentCompletedAt")
        or item.get("trainingCompletedAt")
        or item.get("trainingStartedAt")
        or item.get("createdAt")
    )

    # Determine completion time (when job finished successfully or failed)
    completed_at = None
    status = item.get("status", "")
    
    # Normalize status to valid GraphQL enum value
    # Handle legacy or unexpected status values
    if status not in VALID_GRAPHQL_STATUSES:
        logger.warning(f"Unknown status '{status}' for job {item.get('id', 'unknown')}, mapping to valid status")
        # Map known legacy/intermediate statuses to valid enum values
        status_mapping = {
            "TRAINING_COMPLETE": "DEPLOYING",  # Training done, deployment pending
            "IN_PROGRESS": "TRAINING",
            "CREATING": "PENDING",
            "ERROR": "FAILED",
            "": "PENDING",
        }
        status = status_mapping.get(status, "PENDING")
    
    if status in ["COMPLETED", "FAILED"]:
        completed_at = item.get("deploymentCompletedAt") or item.get(
            "trainingCompletedAt"
        )

    # Format training metrics as JSON string if it's a dict
    training_metrics = item.get("trainingMetrics")
    if isinstance(training_metrics, dict):
        training_metrics = json.dumps(training_metrics, cls=DecimalEncoder)

    return {
        "jobId": item.get("id", ""),
        "jobName": item.get("jobName", ""),
        "testSetId": item.get("testSetId", ""),
        "testSetName": item.get("testSetName"),
        "baseModelId": item.get("baseModel", ""),
        "customModelName": item.get("customModelName"),
        "customModelArn": item.get("customModelArn"),
        "customModelDeploymentArn": item.get("customModelDeploymentArn"),
        "status": status,
        "createdAt": item.get("createdAt", ""),
        "updatedAt": updated_at,
        "completedAt": completed_at,
        "errorMessage": item.get("errorMessage"),
        "trainingMetrics": training_metrics,
        "hyperparameters": item.get("hyperparameters"),
        "trainingDataConfig": item.get("trainingDataConfig"),
        "validationDataConfig": item.get("validationDataConfig"),
        "outputDataConfig": item.get("outputDataConfig"),
        "deploymentId": item.get("deploymentId"),
        "deploymentStatus": item.get("deploymentStatus"),
        "deploymentEndpoint": item.get("deploymentEndpoint"),
        "provisionedModelArn": item.get("provisionedModelArn"),
    }
