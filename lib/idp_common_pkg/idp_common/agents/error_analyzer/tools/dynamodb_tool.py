# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB tools for error analysis.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict

import boto3
from strands import tool

from ..config import create_error_response, create_response, decimal_to_float

logger = logging.getLogger(__name__)


@tool
def fetch_document_record(object_key: str) -> Dict[str, Any]:
    """
    Retrieve complete document record with all metadata from tracking table.

    Gets the full document record including processing details, timestamps,
    configuration, and execution information for comprehensive analysis.
    Agents can extract status, timestamps, or any other needed information
    from the complete record. If tracking table is unavailable, suggests
    using alternative tools.

    Use this tool to:
    - Get complete document processing details and status
    - Access all document metadata and configuration
    - Retrieve processing timestamps and execution info
    - Check if document is COMPLETED, FAILED, or IN_PROGRESS
    - Find Step Function execution ARN
    - Verify document exists in the system

    Tool chaining: If tracking_available=False, use search_cloudwatch_logs,
    analyze_document_trace, or retrieve_document_context for document analysis.

    Example usage:
    - "What's the status of report.pdf?"
    - "Get full details for document report.pdf"
    - "Show me all information about lending_package.pdf"
    - "Is lending_package.pdf completed?"
    - "Check the processing status of document xyz.pdf"

    Args:
        object_key: Document filename/S3 object key (e.g., "report.pdf", "lending_package.pdf")

    Returns:
        Dict with keys:
        - tracking_available (bool): Whether tracking table is configured
        - document_found (bool): Whether document exists
        - document (dict): Complete document record with all fields if found
        - object_key (str): The document identifier
        - reason (str): Explanation if document not found or tracking unavailable
        - suggestion (str): Alternative tools to use if tracking unavailable
    """
    try:
        tracking_table, table_name = _get_tracking_table()
        if not tracking_table:
            return create_response(
                {
                    "tracking_available": False,
                    "reason": "Tracking table not configured",
                    "document_found": False,
                    "object_key": object_key,
                    "suggestion": "Try using search_cloudwatch_logs or X-Ray traces for document analysis",
                }
            )

        # Direct key lookup
        dynamodb_response = tracking_table.get_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"}
        )

        if "Item" in dynamodb_response:
            document_item = decimal_to_float(dynamodb_response["Item"])
            response = create_response(
                {
                    "tracking_available": True,
                    "document_found": True,
                    "document": document_item,
                    "object_key": object_key,
                }
            )
            logger.info(f"Document record response for {object_key}: {response}")
            return response
        else:
            response = create_response(
                {
                    "tracking_available": True,
                    "document_found": False,
                    "object_key": object_key,
                    "reason": f"Document not found for key: {object_key}",
                }
            )
            logger.info(f"Document record not found for {object_key}: {response}")
            return response

    except Exception as e:
        return _handle_dynamodb_error(
            "lookup", object_key, e, document_found=False, object_key=object_key
        )


@tool
def fetch_recent_records(
    date: str = "", hours_back: int = 24, limit: int = 100
) -> Dict[str, Any]:
    """
    Fetch multiple document records processed within a specific time window.

    Retrieves document records from the tracking table using efficient time-based
    partition scanning. Returns a list of documents processed within the specified
    timeframe, useful for analyzing processing patterns, volumes, and recent activity.
    Returns empty results if tracking table is unavailable.

    Use this tool to:
    - Get documents processed in the last few hours or days
    - Analyze document processing patterns and volumes
    - Find recent document processing activity
    - Review processing history for a specific date
    - Identify processing trends or issues

    Example usage:
    - "Show me documents processed today"
    - "What documents were processed in the last 2 hours?"
    - "Find all documents processed on 2024-01-15"
    - "List recent document processing activity"
    - "Show me the last 50 processed documents"

    Args:
        date: Date in YYYY-MM-DD format (defaults to today)
        hours_back: Number of hours to look back from date (default 24)
        limit: Maximum number of records to return (default 100)

    Returns:
        Dict with keys:
        - total_documents (int): Total number of document records found
        - completed_documents (int): Number of documents with COMPLETED status
        - failed_documents (int): Number of documents with FAILED status
        - in_progress_documents (int): Number of documents with IN_PROGRESS status
        - items (list): Document records with processing details if found
        - query_date (str): Date queried
        - hours_back (int): Hours looked back
    """
    try:
        tracking_table, table_name = _get_tracking_table()
        if not tracking_table:
            return _create_empty_response(date, hours_back)

        # Use current date if not provided
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        # Query documents from tracking table
        raw_items = _query_documents_by_time(tracking_table, date, hours_back, limit)
        items = [decimal_to_float(item) for item in raw_items[:limit]]

        # Calculate status statistics
        status_stats = _calculate_status_statistics(items)

        response = {
            "total_documents": len(items),
            "completed_documents": status_stats["completed"],
            "failed_documents": status_stats["failed"],
            "aborted_documents": status_stats["aborted"],
            "in_progress_documents": status_stats["in_progress"],
            "items": items,
            "query_date": date,
            "hours_back": hours_back,
        }
        logger.info(f"Recent records response for date={date}: {response}")
        return response

    except Exception as e:
        return _handle_dynamodb_error(
            "query", f"date={date}", e, items_found=0, items=[]
        )


def _get_tracking_table():
    """
    Internal utility to get tracking table resource with validation.

    Returns:
        Tuple of (table_resource, table_name) or (None, None) if not configured
    """
    table_name = os.environ.get("TRACKING_TABLE_NAME", "")
    if not table_name:
        return None, None

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)  # type: ignore[attr-defined]
    return table, table_name


def _create_empty_response(date: str, hours_back: int) -> Dict[str, Any]:
    """
    Create empty response when tracking table is unavailable.
    """
    return {
        "total_documents": 0,
        "completed_documents": 0,
        "failed_documents": 0,
        "aborted_documents": 0,
        "in_progress_documents": 0,
        "items": [],
        "query_date": date or datetime.now().strftime("%Y-%m-%d"),
        "hours_back": hours_back,
    }


def _query_documents_by_time(
    tracking_table, date: str, hours_back: int, limit: int
) -> list:
    """
    Query documents from tracking table using time-based partitions.
    """
    base_date = datetime.strptime(date, "%Y-%m-%d")
    end_time = base_date + timedelta(days=1)
    start_time = end_time - timedelta(hours=hours_back)

    all_items = []
    current_time = start_time

    while current_time < end_time and len(all_items) < limit:
        hour_str = current_time.strftime("%Y-%m-%dT%H")
        partition_key = (
            f"list#{current_time.strftime('%Y-%m-%d')}#s#{current_time.hour // 4:02d}"
        )
        sort_key_prefix = f"ts#{hour_str}"

        try:
            response = tracking_table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
                ExpressionAttributeValues={
                    ":pk": partition_key,
                    ":sk_prefix": sort_key_prefix,
                },
                Limit=min(limit - len(all_items), 50),
            )
            all_items.extend(response.get("Items", []))
        except Exception as query_error:
            logger.debug(f"Query failed for {partition_key}: {query_error}")

        current_time += timedelta(hours=1)

    return all_items


def _calculate_status_statistics(items: list) -> Dict[str, int]:
    """
    Calculate document status statistics from items.
    Any status that is not COMPLETED, FAILED, or ABORTED is considered in_progress.
    """
    stats = {"completed": 0, "failed": 0, "aborted": 0, "in_progress": 0}

    for item in items:
        status = _extract_document_status(item)
        if status == "COMPLETED":
            stats["completed"] += 1
        elif status == "FAILED":
            stats["failed"] += 1
        elif status == "ABORTED":
            stats["aborted"] += 1
        else:
            stats["in_progress"] += 1

    return stats


def _extract_document_status(item: Dict[str, Any]) -> str:
    """
    Extract document status from tracking table item.
    """
    return item.get("ObjectStatus") or item.get("WorkflowStatus") or "UNKNOWN"


def _handle_dynamodb_error(
    operation: str, identifier: str, error: Exception, **kwargs
) -> Dict[str, Any]:
    """
    Standardized error handling for DynamoDB operations.

    Args:
        operation: Operation being performed (e.g., "lookup", "query")
        identifier: Document key or operation identifier
        error: Exception that occurred
        **kwargs: Additional error response fields

    Returns:
        Standardized error response
    """
    logger.error(f"DynamoDB {operation} failed for '{identifier}': {error}")
    return create_error_response(str(error), **kwargs)
