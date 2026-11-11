# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch results retrieval tool"""

import logging
import os
from typing import Any, Dict, Optional

from idp_common import mcp_access

from .base import NO_CALLER, IDPTool, config_version_lookup, document_id_from_ref

logger = logging.getLogger(__name__)


class GetResultsTool(IDPTool):
    """Get processing results for all documents in batch"""

    def __init__(self):
        """Initialize with stack name from environment"""
        self.stack_name = os.environ.get("AWS_STACK_NAME")
        if not self.stack_name:
            lambda_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
            if lambda_name.endswith("-agentcore-analytics"):
                self.stack_name = lambda_name.replace("-agentcore-analytics", "")
            else:
                self.stack_name = lambda_name
        logger.info(f"GetResultsTool initialized with stack_name: {self.stack_name}")

    def execute(
        self,
        batch_id: Optional[str] = None,
        document_id: Optional[str] = None,
        section_id: int = 1,
        limit: int = 10,
        next_token: Optional[str] = None,
        caller: Any = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute results retrieval for a batch or a single document.

        Args:
            batch_id: Batch identifier — returns paginated results for every
                document in the batch.
            document_id: Document identifier (S3 object key of the source
                document) or an s3:// URI of its output prefix — returns the
                results for that one document, no batch id needed. Callers
                such as post-processing hook consumers only receive a
                document reference, never a batch id.
            section_id: Section number within documents (default: 1)
            limit: Max documents per page (batch mode only)
            next_token: Pagination token (batch mode only)

        Returns:
            Dictionary with success flag and document results.
        """
        if caller is None:
            return dict(NO_CALLER)
        if not batch_id and not document_id:
            return {
                "success": False,
                "error": "missing_identifier",
                "message": (
                    "Either batch_id or document_id is required. Pass batch_id "
                    "for batch results, or document_id (the document's object "
                    "key or its s3:// output reference) for a single document."
                ),
            }

        try:
            from idp_sdk.client import IDPClient

            client = IDPClient(stack_name=self.stack_name)

            scoped = caller is not None and caller.scope is not None

            if document_id and not batch_id:
                if scoped:
                    # Refused with document_scope's one message, the same for a
                    # document outside the scope and for one that does not exist.
                    mcp_access.require_document_keys_in_scope(
                        caller,
                        [document_id_from_ref(document_id)],
                        config_version_of=config_version_lookup(),
                    )
                logger.info(
                    f"Retrieving results for document: {document_id}, "
                    f"section: {section_id}"
                )
                document = client.batch.get_document_results(
                    document_id=document_id,
                    section_id=section_id,
                )
                return {
                    "success": True,
                    "batch_id": None,
                    "section_id": section_id,
                    "count": 1,
                    "total_in_batch": None,
                    "documents": [document],
                    "summary": {},
                    "next_token": None,  # nosec B105 - pagination field
                    "message": (
                        f"Retrieved results for document "
                        f"{document.get('document_id')} "
                        f"(status: {document.get('status')})"
                    ),
                }

            logger.info(
                f"Retrieving results for batch: {batch_id}, "
                f"section: {section_id}, limit: {limit}"
            )

            if scoped:
                return self._scoped_batch_results(
                    client, caller, batch_id, section_id, limit, next_token
                )

            result = client.batch.get_results(
                batch_id=batch_id,
                section_id=section_id,
                limit=limit,
                next_token=next_token,
            )

            return {
                "success": True,
                "batch_id": result.get("batch_id"),
                "section_id": result.get("section_id"),
                "count": result.get("count"),
                "total_in_batch": result.get("total_in_batch"),
                "documents": result.get("documents", []),
                "summary": result.get("summary", {}),
                "next_token": result.get("next_token"),
                "message": f"Retrieved results for {result.get('count', 0)} documents",
            }

        except PermissionError as e:
            logger.warning(f"Results retrieval refused: {e}")
            return {
                "success": False,
                "error": "results_retrieval_failed",
                "message": str(e),
            }
        except Exception as e:
            logger.error(f"Results retrieval failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": "results_retrieval_failed",
                "message": f"Failed to retrieve results: {str(e)}",
            }

    def _scoped_batch_results(
        self, client, caller, batch_id, section_id, limit, next_token
    ) -> Dict[str, Any]:
        """Batch results over the documents in ``caller``'s scope only.

        Pagination, counts and the page size all run over the in-scope documents,
        so neither ``total_in_batch`` nor a short page says how many documents of
        the batch the caller cannot see. A batch with none in scope is reported as
        not found, as a batch that does not exist is.
        """
        import base64

        from idp_sdk.exceptions import IDPResourceNotFoundError

        keys = mcp_access.document_keys_in_scope(
            caller,
            client.batch.get_document_ids(batch_id=batch_id),
            config_version_of=config_version_lookup(),
        )
        if not keys:
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")
        offset = 0
        if next_token:
            try:
                offset = int(base64.b64decode(next_token).decode("utf-8"))
            except Exception:
                offset = 0
        limit = max(1, min(int(limit or 10), 100))
        page = keys[offset : offset + limit]
        documents = [
            client.batch.get_document_results(document_id=key, section_id=section_id)
            for key in page
        ]
        token = None
        if offset + limit < len(keys):
            token = base64.b64encode(str(offset + limit).encode("utf-8")).decode(
                "utf-8"
            )
        return {
            "success": True,
            "batch_id": batch_id,
            "section_id": section_id,
            "count": len(documents),
            "total_in_batch": len(keys),
            "documents": documents,
            "summary": {},
            "next_token": token,
            "message": f"Retrieved results for {len(documents)} documents",
        }
