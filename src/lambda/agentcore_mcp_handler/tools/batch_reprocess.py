# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch reprocess tool: ask the stack to process documents again from a step.

The tool holds no reprocess logic. It checks who is asking and which documents
they may act on, then invokes the stack's reprocess resolver, the one place that
decides whether a document can be processed again, clears the outputs the run
will produce again and queues it. The invoke carries the caller the tool acts
for (``actingCaller``), so the resolver applies that caller's role and
configuration scope as well.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from idp_common import mcp_access
from idp_common.run_plan import steps as run_steps
from idp_common.run_plan.resolver_requests import (
    QUEUED,
    ResolverCallError,
    reprocess_in_requests,
)

from .base import NO_CALLER, IDPTool, config_version_lookup

logger = logging.getLogger(__name__)

# Version marker
TOOL_VERSION = "RESOLVER-v1"
TOOL_UPDATED = "2026-11-17T00:00:00Z"

#: The tool's name in the MCP tool policy.
TOOL_NAME = "reprocess"

#: Seconds to wait for the resolver's answer: longer than the resolver's own
#: timeout, so a slow request ends with the function's timeout. A client that
#: gives up first retries, and the retry would queue the documents again.
_READ_TIMEOUT_SECONDS = 120

# Created on first use and kept for the life of the container.
_lambda_client: Any = None


def _lambda() -> Any:
    global _lambda_client
    if _lambda_client is None:
        import boto3
        from botocore.config import Config

        _lambda_client = boto3.client(
            "lambda",
            region_name=os.environ.get("AWS_REGION"),
            config=Config(read_timeout=_READ_TIMEOUT_SECONDS),
        )
    return _lambda_client


def _steps_to_skip(skip: Any) -> List[str]:
    """``skip`` as a list: an array, or a comma-separated string."""
    if skip is None:
        return []
    if isinstance(skip, str):
        return [value.strip() for value in skip.split(",") if value.strip()]
    return [str(value) for value in skip]


def _validated(step: Any, skip: Any) -> tuple:
    """The start step and the steps to skip, checked against the step list.

    Raises:
        ValueError: ``step`` is not a step a run can start at, or a value of
            ``skip`` is not a step a run can leave out.
    """
    if step not in run_steps.start_values():
        raise ValueError(
            f"'{step}' is not a step a reprocess can start at; "
            f"the steps are {', '.join(run_steps.start_values())}"
        )
    skipped = _steps_to_skip(skip)
    unknown = [value for value in skipped if value not in run_steps.optional_step_ids()]
    if unknown:
        raise ValueError(
            f"{', '.join(repr(value) for value in unknown)} cannot be skipped; "
            f"the steps a run can skip are {', '.join(run_steps.optional_step_ids())}"
        )
    return step, skipped


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

    def _document_ids(
        self, document_ids: Optional[str], batch_id: Optional[str], region: Optional[str]
    ) -> List[str]:
        """The documents the request names: the list given, or the batch's own."""
        if document_ids:
            return [doc_id.strip() for doc_id in document_ids.split(",") if doc_id.strip()]
        from idp_sdk.client import IDPClient

        logger.info(f"Resolving document IDs from batch: {batch_id}")
        client = IDPClient(stack_name=self.stack_name, region=region)
        return list(client.batch.get_document_ids(batch_id=batch_id))

    @staticmethod
    def _send(caller: Any):
        """One request to the reprocess resolver, made for ``caller``.

        The event has no identity, which is what the resolver reads as a direct
        invoke; ``actingCaller`` is the principal it then holds the request to.
        """
        function_arn = os.environ["REPROCESS_FUNCTION_ARN"]
        acting_caller = caller.to_payload()

        def send(arguments: Dict[str, Any]) -> Any:
            response = _lambda().invoke(
                FunctionName=function_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps(
                    {
                        "info": {"fieldName": "reprocessDocument"},
                        "arguments": arguments,
                        "actingCaller": acting_caller,
                    }
                ),
            )
            body = response["Payload"].read()
            answer = json.loads(body) if body else None
            if response.get("FunctionError"):
                error = answer if isinstance(answer, dict) else {}
                raise ResolverCallError(
                    str(error.get("errorType") or "Error"),
                    str(error.get("errorMessage") or "the reprocess function failed"),
                )
            return answer

        return send

    def execute(
        self,
        step: str,
        document_ids: Optional[str] = None,
        batch_id: Optional[str] = None,
        region: Optional[str] = None,
        caller: Any = None,
        skip: Any = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Ask the reprocess resolver to process the documents again."""
        logger.info(f"=== Batch Reprocess Tool ===")
        logger.info(f"Version: {TOOL_VERSION}")
        logger.info(f"Updated: {TOOL_UPDATED}")
        logger.info(f"Parameters: step={step}, skip={skip}, document_ids={document_ids}, batch_id={batch_id}, region={region}")

        if caller is None:
            return dict(NO_CALLER)

        try:
            # The handler has made this check; the tool makes it again because it
            # is the component that does the work, and it must refuse before any
            # document is read or any function invoked.
            mcp_access.authorize_tool(caller, TOOL_NAME)
            start, skipped = _validated(step, skip)

            # Validate mutually exclusive options
            if not document_ids and not batch_id:
                raise ValueError("Must specify either document_ids or batch_id")

            if document_ids and batch_id:
                raise ValueError("Cannot specify both document_ids and batch_id")

            doc_id_list = self._document_ids(document_ids, batch_id, region)
            if not doc_id_list:
                if caller.scope is not None:
                    raise PermissionError(
                        "Unauthorized: you are not permitted to act on that document."
                    )
                raise ValueError("The request names no document")

            if caller.scope is not None:
                # Every document must be in the caller's scope, and one outside it
                # refuses the whole request. A batch is expanded to its documents
                # first and those exact documents are what is sent, so the set
                # checked is the set processed. The resolver checks the scope
                # again, from the acting caller.
                mcp_access.require_document_keys_in_scope(
                    caller, doc_id_list, config_version_of=config_version_lookup()
                )

            arguments: Dict[str, Any] = {"startStep": start}
            if skipped:
                arguments["skipSteps"] = skipped
            logger.info(f"Reprocessing {len(doc_id_list)} documents from step: {start}")
            entries = reprocess_in_requests(self._send(caller), doc_id_list, arguments)

            results = [
                {
                    "document_id": entry.get("objectKey"),
                    "status": entry.get("status"),
                    "start_step": entry.get("startStep") or None,
                    "reason": entry.get("reason") or None,
                    "message": entry.get("message") or None,
                }
                for entry in entries
            ]
            queued = sum(1 for result in results if result["status"] == QUEUED)
            not_queued = len(results) - queued
            logger.info(f"Batch reprocessing completed: queued={queued}, not queued={not_queued}")
            message = f"Queued {queued} of {len(results)} documents for reprocessing from {start}"
            if not_queued:
                message += f"; {not_queued} were not queued (see results for the reason of each)"
            return {
                "success": True,
                "batch_id": batch_id or "rerun",
                "documents_queued": queued,
                "documents_failed": not_queued,
                "step": step,
                "summary": {
                    "queued": queued,
                    "failed": not_queued,
                },
                "results": results,
                "message": message,
            }

        except PermissionError as e:
            logger.warning(f"Batch reprocess refused: {e}")
            return {"success": False, "error": str(e), "step": step}
        except ResolverCallError as e:
            # The resolver refused the request as a whole, or failed on it, before
            # anything was queued.
            if e.is_refusal:
                logger.warning(f"Batch reprocess refused by the resolver: {e.error_type}")
            else:
                logger.error(f"Batch reprocess failed in the resolver: {e.error_type}: {e}")
            return {"success": False, "error": str(e), "step": step}
        except Exception as e:
            logger.error(f"Batch reprocess failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "step": step
            }
