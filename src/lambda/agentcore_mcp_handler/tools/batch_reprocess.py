# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch reprocess tool for reprocessing documents from a specific step"""

import logging
import os
from typing import Any, Dict, Optional

from idp_common import mcp_access

from .base import IDPTool, config_version_lookup

logger = logging.getLogger(__name__)

# Version marker
TOOL_VERSION = "SDK-v3"
TOOL_UPDATED = "2026-03-13T00:00:00Z"


class BatchReprocessTool(IDPTool):
    """Reprocess documents from a specific pipeline step"""

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
        logger.info(f"BatchReprocessTool initialized with stack_name: {self.stack_name}")

    def execute(
        self,
        step: str,
        document_ids: Optional[str] = None,
        batch_id: Optional[str] = None,
        region: Optional[str] = None,
        caller: Any = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Execute batch reprocessing using IDPClient public API"""
        logger.info(f"=== Batch Reprocess Tool ===")
        logger.info(f"Version: {TOOL_VERSION}")
        logger.info(f"Updated: {TOOL_UPDATED}")
        logger.info(f"Parameters: step={step}, document_ids={document_ids}, batch_id={batch_id}, region={region}")

        from idp_sdk.client import IDPClient

        try:
            # Validate mutually exclusive options
            if not document_ids and not batch_id:
                raise ValueError("Must specify either document_ids or batch_id")

            if document_ids and batch_id:
                raise ValueError("Cannot specify both document_ids and batch_id")

            logger.info(f"Initializing IDPClient with stack: {self.stack_name}")
            client = IDPClient(stack_name=self.stack_name, region=region)
            logger.info("IDPClient initialized successfully")

            # Parse document_ids string into list if provided
            doc_id_list = None
            if document_ids:
                doc_id_list = [doc_id.strip() for doc_id in document_ids.split(",")]
                logger.info(f"Processing {len(doc_id_list)} specified documents")
            else:
                logger.info(f"Will resolve document IDs from batch: {batch_id}")

            requested_batch_id = batch_id
            if caller is not None and caller.scope is not None:
                # Every document must be in the caller's scope, and one outside it
                # refuses the whole request. A batch is expanded to its documents
                # first and those exact documents are what is re-run, so the set
                # checked is the set processed.
                if doc_id_list is None:
                    doc_id_list = list(client.batch.get_document_ids(batch_id=batch_id))
                    batch_id = None
                if not doc_id_list:
                    raise PermissionError(
                        "Unauthorized: you are not permitted to act on that document."
                    )
                mcp_access.require_document_keys_in_scope(
                    caller, doc_id_list, config_version_of=config_version_lookup()
                )

            # Perform reprocessing via public SDK API
            # batch.reprocess() handles batch_id -> document_id resolution internally
            logger.info(f"Reprocessing documents from step: {step}")
            result = client.batch.reprocess(
                step=step,
                document_ids=doc_id_list,
                batch_id=batch_id,
            )

            # Format response using typed BatchReprocessResult model attributes
            logger.info(f"Batch reprocessing completed: queued={result.documents_queued}, failed={result.documents_failed}")
            return {
                "success": True,
                "batch_id": requested_batch_id or "rerun",
                "documents_queued": result.documents_queued,
                "documents_failed": result.documents_failed,
                "step": step,
                "summary": {
                    "queued": result.documents_queued,
                    "failed": result.documents_failed,
                },
                "message": f"Successfully queued {result.documents_queued} documents for {step} reprocessing"
            }

        except PermissionError as e:
            logger.warning(f"Batch reprocess refused: {e}")
            return {"success": False, "error": str(e), "step": step}
        except Exception as e:
            logger.error(f"Batch reprocess failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "step": step
            }
