# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda tools for document context extraction.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
from strands import tool

from ..config import create_error_response

logger = logging.getLogger(__name__)


@tool
def retrieve_document_context(document_id: str) -> Dict[str, Any]:
    """
    Retrieve comprehensive document processing context via Lambda lookup function.

    Invokes the lookup Lambda function to gather execution context, timing information,
    and Step Function details for a specific document. Provides essential data for
    targeted error analysis and log searching.

    Use this tool to:
    - Get complete document processing timeline and status
    - Extract Lambda request IDs for CloudWatch log correlation
    - Identify failed functions and execution context
    - Obtain precise processing time windows for analysis

    Alternative: If you only need basic document metadata (status, timestamps, execution ARN)
    without detailed execution events and Lambda request IDs, consider using fetch_document_record
    which provides faster access to DynamoDB tracking data.

    Example usage:
    - "Get processing context for report.pdf"
    - "Retrieve execution details for lending_package.pdf"
    - "Show me the processing timeline for document ABC123"
    - "Get Lambda request IDs for failed document processing"

    Args:
        document_id: Document ObjectKey to analyze (e.g., "report.pdf", "lending_package.pdf")

    Returns:
        Dict containing document context, execution details, and timing information
    """
    try:
        lambda_client = boto3.client("lambda")
        function_name = get_lookup_function_name()

        logger.info(
            f"Invoking lookup function: {function_name} for document: {document_id}"
        )

        # Invoke lookup function
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"object_key": document_id}),
        )

        # Parse response
        payload = json.loads(response["Payload"].read().decode("utf-8"))

        if payload.get("status") == "NOT_FOUND":
            return create_error_response(
                "Document not found in tracking database",
                document_found=False,
                document_id=document_id,
            )

        if payload.get("status") == "ERROR":
            return create_error_response(
                payload.get("message", "Unknown error from lookup function"),
                document_found=False,
                document_id=document_id,
            )

        # Extract execution context
        processing_detail = payload.get("processingDetail", {})
        execution_arn = processing_detail.get("executionArn")
        execution_events = processing_detail.get("events", [])

        # Extract Lambda request IDs and function mapping from execution events
        request_context = extract_lambda_request_ids(execution_events)
        request_ids = request_context.get("all_request_ids", [])
        function_request_map = request_context.get("function_request_map", {})
        failed_functions = request_context.get("failed_functions", [])
        primary_failed_function = request_context.get("primary_failed_function")

        # Get timestamps for precise time windows
        timestamps = payload.get("timing", {}).get("timestamps", {})

        # Calculate processing time window
        start_time = None
        end_time = None

        if timestamps.get("WorkflowStartTime"):
            start_time = datetime.fromisoformat(
                timestamps["WorkflowStartTime"].replace("Z", "+00:00")
            )

        if timestamps.get("CompletionTime"):
            end_time = datetime.fromisoformat(
                timestamps["CompletionTime"].replace("Z", "+00:00")
            )

        response = {
            "document_found": True,
            "document_id": document_id,
            "document_status": payload.get("status"),
            "execution_arn": execution_arn,
            "lambda_request_ids": request_ids,
            "function_request_map": function_request_map,
            "failed_functions": failed_functions,
            "primary_failed_function": primary_failed_function,
            "timestamps": timestamps,
            "processing_start_time": start_time,
            "processing_end_time": end_time,
            "execution_events_count": len(execution_events),
            "lookup_function_response": payload,
        }

        logger.info(f"Document context response for {document_id}: {response}")
        return response

    except Exception as e:
        logger.error(f"Error getting document context for {document_id}: {e}")
        return create_error_response(
            str(e), document_found=False, document_id=document_id
        )


def get_lookup_function_name() -> str:
    """
    Retrieve the Lambda lookup function name from environment configuration.
    Checks for LOOKUP_FUNCTION_NAME environment variable with fallback to
    AWS_STACK_NAME-based naming convention.

    Returns:
        Lambda function name string

    Raises:
        ValueError: If neither environment variable is configured
    """
    function_name = os.environ.get("LOOKUP_FUNCTION_NAME")
    if function_name:
        return function_name

    raise ValueError("LOOKUP_FUNCTION_NAME environment variable not set")


def extract_lambda_request_ids(
    execution_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Extract Lambda request IDs from Step Functions execution event history with function mapping.
    Enhanced to extract request IDs from multiple event fields and map them to specific Lambda functions.

    Args:
        execution_events: List of Step Function execution events

    Returns:
        Dict containing request IDs mapped to functions and failure information
    """
    function_request_map = {}
    failed_functions = []
    all_request_ids = []

    for i, event in enumerate(execution_events):
        event_type = event.get("type", "")

        # Extract function name from various event types
        function_name = None
        request_id = None

        if event_type in [
            "LambdaFunctionSucceeded",
            "LambdaFunctionFailed",
            "LambdaFunctionTimedOut",
            "TaskStateEntered",
            "TaskStateExited",
        ]:
            # Get function name from resource ARN or state name
            if "LambdaFunction" in event_type:
                event_detail = (
                    event.get("lambdaFunctionSucceededEventDetails")
                    or event.get("lambdaFunctionFailedEventDetails")
                    or event.get("lambdaFunctionTimedOutEventDetails")
                )
            elif "TaskState" in event_type:
                event_detail = event.get("stateEnteredEventDetails") or event.get(
                    "stateExitedEventDetails"
                )
            else:
                event_detail = None

            if event_detail:
                # Extract function name
                resource = event_detail.get("resource", "")
                name = event_detail.get("name", "")

                if resource and ":function:" in resource:
                    function_name = resource.split(":function:")[-1]
                elif name:
                    function_name = name
                # A `resource` that is a Lambda ARN but NOT a function ARN -- a layer
                # version, an event-source mapping, a code-signing config -- carries no
                # function name to read. Splitting one positionally yields the layer
                # name or the mapping uuid, and that value would be reported to an
                # operator as the function that failed and used to pick the log group
                # to search. Such an event contributes nothing instead, which is what
                # an unreadably short ARN already did.

                # Extract request ID from multiple fields
                for field_name, field_value in event_detail.items():
                    if field_value:
                        request_id = _extract_request_id_from_json(str(field_value))
                        if not request_id:
                            request_id = _extract_request_id_from_string(
                                str(field_value)
                            )
                        if request_id:
                            break

                # Track failed functions
                if event_type in ["LambdaFunctionFailed", "LambdaFunctionTimedOut"]:
                    if function_name:
                        failed_functions.append(function_name)

                # Map function to request ID
                if function_name and request_id:
                    function_request_map[function_name] = request_id
                    all_request_ids.append(request_id)

        # Also check top-level event fields for request IDs
        if not request_id:
            for field_name, field_value in event.items():
                if (
                    field_name not in ["type", "timestamp", "id", "previousEventId"]
                    and field_value
                ):
                    request_id = _extract_request_id_from_string(str(field_value))
                    if request_id and function_name:
                        function_request_map[function_name] = request_id
                        all_request_ids.append(request_id)
                        break

    result = {
        "function_request_map": function_request_map,
        "failed_functions": list(set(failed_functions)),
        "all_request_ids": list(set(all_request_ids)),
        "primary_failed_function": failed_functions[0] if failed_functions else None,
    }

    if not all_request_ids:
        logger.info("No request ids extracted from step functions events")
    return result


def _extract_request_id_from_json(json_string: str) -> Optional[str]:
    """
    Extract request ID from JSON string in various formats.

    Args:
        json_string: JSON string that may contain request ID

    Returns:
        Request ID string if found, None otherwise
    """
    if not json_string:
        return None

    try:
        data = json.loads(json_string)
        # Check common request ID field names
        for field in [
            "requestId",
            "request_id",
            "RequestId",
            "awsRequestId",
            "lambdaRequestId",
        ]:
            if field in data and data[field]:
                return str(data[field])
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _extract_request_id_from_string(text: str) -> Optional[str]:
    """
    Extract Lambda request ID from string using UUID pattern matching.

    Args:
        text: String that may contain a UUID request ID

    Returns:
        Request ID string if found, None otherwise
    """
    import re

    if not text:
        return None

    # Pattern for UUID
    uuid_pattern = r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"

    matches = re.findall(uuid_pattern, text, re.IGNORECASE)

    if matches:
        return matches[0]

    return None
