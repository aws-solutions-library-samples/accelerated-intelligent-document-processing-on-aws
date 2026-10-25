# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from idp_common.docs_service import create_document_service

# Import IDP Common modules
from idp_common.models import Status
from idp_common.testset_scope import is_direct_invoke
from idp_common.utils.log_sanitizer import sanitize_event_for_logging
from idp_common.utils.settings_helper import get_setting

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

# Initialize AWS clients
sfn_client = boto3.client('stepfunctions')

# Initialize document service (uses AppSync by default)
document_service = create_document_service()

# Review queues: aborting withdraws the document's review item (claim released,
# status object deleted). Built on first use; None when no table is configured.
_REVIEW_SERVICE_CACHE = {}


def _review_service():
    if "svc" not in _REVIEW_SERVICE_CACHE:
        if os.environ.get("HITL_TABLE_NAME"):
            from idp_common.review_queues.service import create_review_queue_service

            _REVIEW_SERVICE_CACHE["svc"] = create_review_queue_service()
        else:
            _REVIEW_SERVICE_CACHE["svc"] = None
    return _REVIEW_SERVICE_CACHE["svc"]


# Statuses that can be aborted (have active workflows or are waiting to start)
ABORTABLE_STATUSES = {
    Status.QUEUED,
    Status.RUNNING,
    Status.PREPROCESSING,
    Status.OCR,
    Status.CLASSIFYING,
    Status.EXTRACTING,
    Status.ASSESSING,
    Status.POSTPROCESSING,
    Status.HITL_IN_PROGRESS,
    Status.SUMMARIZING,
    Status.EVALUATING,
}


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context):
    """
    Lambda handler for aborting document workflows.

    This resolver handles the abortWorkflow GraphQL mutation, which allows users
    to cancel processing for one or more documents. For QUEUED documents, it marks
    them as ABORTED so the queue processor skips them. For documents with active
    workflows, it calls StopExecution on the Step Functions execution.
    """
    logger.info(f"Abort workflow resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        # Defense-in-depth: abortWorkflow is an Admin+Author operation. Raise
        # (not return a 200 dict) so the dispatcher maps it to 403/Unauthorized.
        #
        # The one exception is an event with no identity at all, the repository's
        # marker for a service-to-service invoke gated by IAM on this function's
        # ARN (idp_common.testset_scope.is_direct_invoke) -- the same rule the
        # abortTestRuns resolver applies to itself. abortTestRuns invokes this
        # function to stop a test run's documents, and when it was itself invoked
        # directly (the SDK and CLI path) it has no Cognito identity to forward.
        # The REST route cannot produce that shape: the dispatcher rebuilds
        # `identity` from the authorizer's verified claims, so every event it
        # sends carries one, and an IAM caller handing the dispatcher a null
        # identity is refused by its group floor for this operation, which is
        # group-scoped. A caller able to invoke this function directly could
        # already put any identity it liked in the payload, so accepting a null
        # one grants it nothing.
        if not is_direct_invoke(event) and not _caller_in_groups(
            event, ("Admin", "Author")
        ):
            logger.warning(
                "Denied abortWorkflow: caller is not in the Admin or Author group"
            )
            raise PermissionError(
                "Unauthorized: abortWorkflow requires Admin or Author group"
            )

        # Extract arguments from GraphQL event
        args = event.get('arguments', {})
        object_keys = args.get('objectKeys', [])
        
        if not object_keys:
            logger.error("objectKeys is required but not provided")
            return {
                "success": False,
                "message": "objectKeys is required",
                "abortedCount": 0,
                "failedCount": 0,
                "errors": ["objectKeys is required"]
            }
        
        logger.info(f"Aborting workflows for {len(object_keys)} documents")
        
        # Process each document
        aborted_count = 0
        failed_count = 0
        errors = []
        
        for object_key in object_keys:
            try:
                result = abort_document(object_key)
                if result['success']:
                    aborted_count += 1
                else:
                    failed_count += 1
                    errors.append(f"{object_key}: {result['error']}")
            except Exception as e:
                logger.error(f"Error aborting document {object_key}: {str(e)}", exc_info=True)
                failed_count += 1
                errors.append(f"{object_key}: {str(e)}")
        
        success = aborted_count > 0 or failed_count == 0
        message = f"Aborted {aborted_count} document(s)"
        if failed_count > 0:
            message += f", {failed_count} failed"
        
        logger.info(f"Abort workflow complete: {message}")
        
        return {
            "success": success,
            "message": message,
            "abortedCount": aborted_count,
            "failedCount": failed_count,
            "errors": errors if errors else None
        }
        
    except PermissionError:
        # RBAC denial must reach the dispatcher as 403; do not swallow into 200.
        raise
    except Exception as e:
        logger.error(f"Error in abort workflow handler: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "abortedCount": 0,
            "failedCount": len(object_keys) if object_keys else 0,
            "errors": [str(e)]
        }


def abort_document(object_key):
    """
    Abort processing for a single document.
    
    Args:
        object_key: The document's object key (ID)
        
    Returns:
        Dict with 'success' boolean and optional 'error' message
    """
    logger.info(f"Aborting document: {object_key}")
    
    # Get current document state
    document = document_service.get_document(object_key)
    
    if not document:
        return {"success": False, "error": "Document not found"}
    
    # Check if document can be aborted
    if document.status not in ABORTABLE_STATUSES:
        return {
            "success": False,
            "error": f"Cannot abort document with status {document.status.value}"
        }
    
    # For documents with active workflows (not QUEUED), stop the Step Functions execution
    if document.status != Status.QUEUED and document.workflow_execution_arn:
        try:
            # Validate execution ARN format - ensure it matches our stack's state machines
            state_machine_name = get_setting('StateMachineName')
            if state_machine_name:
                # Verify the execution ARN belongs to our state machine
                if f":{state_machine_name}:" not in document.workflow_execution_arn:
                    logger.warning(f"Execution ARN {document.workflow_execution_arn} doesn't match expected state machine {state_machine_name}")
            
            logger.info(f"Stopping Step Functions execution: {document.workflow_execution_arn}")
            sfn_client.stop_execution(
                executionArn=document.workflow_execution_arn,
                cause="User requested abort via UI"
            )
            logger.info(f"Successfully stopped execution for {object_key}")
        except sfn_client.exceptions.ExecutionDoesNotExist:
            logger.warning(f"Execution {document.workflow_execution_arn} does not exist (may have already completed)")
        except Exception as e:
            # Log but continue - we still want to update the document status
            logger.warning(f"Could not stop execution for {object_key}: {str(e)}")
    
    # Update document status to ABORTED
    document.status = Status.ABORTED
    document.completion_time = datetime.now(timezone.utc).isoformat()
    
    try:
        document_service.update_document(document)
        logger.info(f"Document {object_key} status updated to ABORTED")
    except Exception as e:
        logger.error(f"Failed to update document status: {str(e)}")
        return {"success": False, "error": f"Failed to update status: {str(e)}"}

    # Withdraw a review in progress (idempotent; the tracker also withdraws when
    # an aborted finalize execution ends).
    review_service = _review_service()
    if review_service is not None:
        try:
            review_service.withdraw(object_key, "aborted")
        except Exception:
            logger.error("Failed to withdraw the review item on abort", exc_info=True)
    return {"success": True}