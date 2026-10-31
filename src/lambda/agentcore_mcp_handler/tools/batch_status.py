# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch status tool for checking processing status"""

import logging
import os
from typing import Any, Dict, Optional
from .base import IDPTool

logger = logging.getLogger(__name__)

# Version marker
TOOL_VERSION = "SDK-v2"
TOOL_UPDATED = "2025-01-09T19:30:00Z"


class BatchStatusTool(IDPTool):
    """Get processing status for a batch"""

    def __init__(self):
        """Initialize with stack name from environment"""
        # Try AWS_STACK_NAME first, then extract from Lambda function name
        self.stack_name = os.environ.get('AWS_STACK_NAME')
        if not self.stack_name:
            lambda_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', '')
            # Extract stack name by removing -agentcore-analytics suffix
            if lambda_name.endswith('-agentcore-analytics'):
                self.stack_name = lambda_name.replace('-agentcore-analytics', '')
            else:
                self.stack_name = lambda_name
        logger.info(f"BatchStatusTool initialized with stack_name: {self.stack_name}")

    def execute(
        self,
        batch_id: str,
        options: Optional[Dict[str, Any]] = None,
        region: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Get batch status using IDP SDK"""
        logger.info(f"=== Batch Status Tool ===")
        logger.info(f"Version: {TOOL_VERSION}")
        logger.info(f"Updated: {TOOL_UPDATED}")
        logger.info(f"Parameters: batch_id={batch_id}, region={region}")
        
        from idp_sdk.client import IDPClient

        options = options or {}

        try:
            # Initialize SDK client
            client = IDPClient(stack_name=self.stack_name, region=region)

            # Call SDK batch.get_status() method
            status = client.batch.get_status(batch_id=batch_id)

            # Format response
            response = {
                "success": True,
                "batch_id": batch_id,
                "status": {
                    "total": status.total,
                    "completed": status.completed,
                    "in_progress": status.in_progress,
                    "failed": status.failed,
                    "queued": status.queued
                },
                "progress": {
                    "percentage": round((status.completed / status.total * 100) if status.total > 0 else 0, 2)
                },
                "all_complete": status.all_complete
            }

            # Add detailed info if requested
            if options.get("detailed") and hasattr(status, 'documents'):
                response["documents"] = [
                    {
                        "document_id": doc.document_id,
                        "status": doc.status.value,
                        "duration_seconds": doc.duration_seconds,
                        # in_review / complete / rejected (review queues), or None
                        "review_status": getattr(doc.review_status, "value", doc.review_status),
                    }
                    for doc in status.documents
                ]

            return response

        except ValueError as e:
            # Handle NOT_FOUND validation error
            if "NOT_FOUND" in str(e):
                logger.info(f"Batch {batch_id} not yet tracked in system")
                return {
                    "success": True,
                    "batch_id": batch_id,
                    "status": {
                        "total": 0,
                        "completed": 0,
                        "in_progress": 0,
                        "failed": 0,
                        "queued": 0
                    },
                    "progress": {
                        "percentage": 0
                    },
                    "all_complete": False,
                    "message": "Batch not yet tracked in system"
                }
            logger.error(f"Batch status failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "batch_id": batch_id
            }
        except Exception as e:
            logger.error(f"Batch status failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "batch_id": batch_id
            }
