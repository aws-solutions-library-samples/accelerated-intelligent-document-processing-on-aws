# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime

import boto3
import requests
from aws_requests_auth.aws_auth import AWSRequestsAuth
from idp_common.bda.bda_blueprint_creator import BDABlueprintCreator
from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from idp_common.bda.blueprint_optimizer import (
    BlueprintOptimizer,
    OptimizationStatus,
)
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")

# Initialize AWS session for AppSync authentication
session = boto3.Session()
credentials = session.get_credentials()

# Get environment variables
APPSYNC_API_URL = os.environ.get("APPSYNC_API_URL")


def handler(event, context):
    """Process blueprint optimization requests.

    Can be invoked asynchronously (InvocationType='Event') from any caller.

    Args:
        event: Expected payload with jobId, classSchema, documentKey,
            groundTruthKey, bucket, version, discoveredClassName.
        context: Lambda context object.
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    job_id = event.get("jobId")
    class_schema = event.get("classSchema")
    document_key = event.get("documentKey")
    ground_truth_key = event.get("groundTruthKey")
    bucket = event.get("bucket")
    version = event.get("version")
    discovered_class_name = event.get("discoveredClassName")

    # Validate required fields
    required = {
        "jobId": job_id,
        "classSchema": class_schema,
        "documentKey": document_key,
        "groundTruthKey": ground_truth_key,
        "bucket": bucket,
        "version": version,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        msg = f"Missing required fields: {', '.join(missing)}"
        logger.error(msg)
        if job_id:
            update_job_status(
                job_id,
                "OPTIMIZATION_FAILED",
                status_message=msg,
            )
        return {"status": "FAILED", "error": msg}

    try:
        optimizer = BlueprintOptimizer(
            blueprint_service=BdaBlueprintService(),
            blueprint_creator=BDABlueprintCreator(),
            config_manager=ConfigurationManager(),
        )
        result = optimizer.optimize(
            class_schema=class_schema,
            document_key=document_key,
            ground_truth_key=ground_truth_key,
            bucket=bucket,
            version=version,
            status_callback=lambda msg: update_job_status(
                job_id,
                "OPTIMIZATION_IN_PROGRESS",
                status_message=msg,
            ),
        )

        if result.improved:
            msg = (
                "Blueprint optimized — extraction accuracy improved "
                f"(exactMatch: {result.before_metrics.exact_match:.2f}"
                f" → {result.after_metrics.exact_match:.2f})"
            )
            update_job_status(
                job_id,
                "OPTIMIZATION_COMPLETED",
                discovered_class_name=discovered_class_name,
                status_message=msg,
            )
        elif result.status == OptimizationStatus.NO_IMPROVEMENT:
            update_job_status(
                job_id,
                "OPTIMIZATION_COMPLETED",
                discovered_class_name=discovered_class_name,
                status_message=(
                    "Blueprint optimization complete — no improvement"
                    " detected, keeping original schema"
                ),
            )
        elif result.status in (
            OptimizationStatus.FAILED,
            OptimizationStatus.TIMED_OUT,
        ):
            update_job_status(
                job_id,
                "OPTIMIZATION_FAILED",
                discovered_class_name=discovered_class_name,
                status_message=(
                    f"Blueprint optimization failed — "
                    f"{result.error_message or 'unknown error'}"
                ),
            )
        else:
            update_job_status(
                job_id,
                "OPTIMIZATION_COMPLETED",
                discovered_class_name=discovered_class_name,
                status_message=(
                    f"Blueprint optimization finished — {result.status.value}"
                ),
            )

    except Exception as e:
        logger.error(
            f"Blueprint optimization failed for job {job_id}: {e}",
            exc_info=True,
        )
        update_job_status(
            job_id,
            "OPTIMIZATION_FAILED",
            discovered_class_name=discovered_class_name,
            status_message=(f"Blueprint optimization failed — {str(e)}"),
        )


def update_job_status_via_appsync(
    job_id,
    status,
    error_message=None,
    discovered_class_name=None,
    status_message=None,
):
    """Update discovery job status via AppSync GraphQL mutation.

    Args:
        job_id: Unique job identifier.
        status: New status (e.g. OPTIMIZATION_IN_PROGRESS).
        error_message: Error message if status is FAILED.
        discovered_class_name: Name of the discovered document class.
        status_message: Human-readable progress/result message.
    """
    try:
        if not APPSYNC_API_URL:
            logger.warning(
                "APPSYNC_API_URL not configured, falling back to direct DynamoDB update"
            )
            update_job_status_direct(
                job_id,
                status,
                error_message,
                discovered_class_name,
                status_message,
            )
            return

        mutation = """
        mutation UpdateDiscoveryJobStatus(
            $jobId: ID!,
            $status: String!,
            $errorMessage: String,
            $discoveredClassName: String,
            $statusMessage: String
        ) {
            updateDiscoveryJobStatus(
                jobId: $jobId,
                status: $status,
                errorMessage: $errorMessage,
                discoveredClassName: $discoveredClassName,
                statusMessage: $statusMessage
            ) {
                jobId
                status
                errorMessage
                discoveredClassName
                statusMessage
            }
        }
        """

        logger.info(f"Updating AppSync for job {job_id}, status {status}")

        variables = {"jobId": job_id, "status": status}
        if error_message:
            variables["errorMessage"] = error_message
        if discovered_class_name:
            variables["discoveredClassName"] = discovered_class_name
        if status_message:
            variables["statusMessage"] = status_message

        region = session.region_name or os.environ.get("AWS_REGION", "us-east-1")
        auth = AWSRequestsAuth(
            aws_access_key=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_token=credentials.token,
            aws_host=APPSYNC_API_URL.replace("https://", "").replace("/graphql", ""),
            aws_region=region,
            aws_service="appsync",
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload = {"query": mutation, "variables": variables}

        logger.info(
            f"Publishing optimization status update to AppSync for job: {job_id}"
        )
        logger.debug(f"Mutation payload: {json.dumps(payload)}")

        response = requests.post(
            APPSYNC_API_URL,
            json=payload,
            headers=headers,
            auth=auth,
            timeout=30,
        )

        if response.status_code == 200:
            response_json = response.json()
            if "errors" not in response_json:
                logger.info(
                    f"Successfully published optimization status update for: {job_id}"
                )
                logger.debug(f"Response: {response.text}")
                return True
            else:
                logger.error(
                    "GraphQL errors in response: "
                    f"{json.dumps(response_json.get('errors'))}"
                )
                return False
        else:
            logger.error(
                "Failed to publish optimization status update."
                f" Status: {response.status_code},"
                f" Response: {response.text}"
            )
            return False

    except Exception as e:
        logger.error(f"Error updating job status via AppSync: {str(e)}")
        update_job_status_direct(
            job_id,
            status,
            error_message,
            discovered_class_name,
            status_message,
        )
        return False


def update_job_status_direct(
    job_id,
    status,
    error_message=None,
    discovered_class_name=None,
    status_message=None,
):
    """Fallback to update job status directly in DynamoDB.

    Used when AppSync is not available or fails.

    Args:
        job_id: Unique job identifier.
        status: New status.
        error_message: Error message if status is FAILED.
        discovered_class_name: Name of the discovered document class.
        status_message: Human-readable progress/result message.
    """
    try:
        table_name = os.environ.get("DISCOVERY_TRACKING_TABLE")
        if not table_name:
            logger.warning(
                "DISCOVERY_TRACKING_TABLE not configured, skipping status update"
            )
            return

        table = dynamodb.Table(table_name)

        update_expression = "SET #status = :status, updatedAt = :updated_at"
        expression_attribute_names = {"#status": "status"}
        expression_attribute_values = {
            ":status": status,
            ":updated_at": datetime.now().isoformat(),
        }

        if error_message:
            update_expression += ", errorMessage = :error_message"
            expression_attribute_values[":error_message"] = error_message

        if discovered_class_name:
            update_expression += ", discoveredClassName = :discovered_class_name"
            expression_attribute_values[":discovered_class_name"] = (
                discovered_class_name
            )

        if status_message:
            update_expression += ", statusMessage = :status_message"
            expression_attribute_values[":status_message"] = status_message

        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values,
        )

        logger.info(f"Updated job {job_id} status to {status} (direct DynamoDB)")

    except Exception as e:
        logger.error(f"Error updating job status directly: {str(e)}")


def update_job_status(
    job_id,
    status,
    error_message=None,
    discovered_class_name=None,
    status_message=None,
):
    """Update discovery job status. Uses AppSync by default.

    Args:
        job_id: Unique job identifier.
        status: New status.
        error_message: Error message if status is FAILED.
        discovered_class_name: Name of the discovered document class.
        status_message: Human-readable progress/result message.
    """
    update_job_status_via_appsync(
        job_id,
        status,
        error_message,
        discovered_class_name,
        status_message,
    )
