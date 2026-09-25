# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Delete document resolver Lambda function.

This function handles document deletion via GraphQL API, using the shared
delete_documents implementation from idp_common for consistent behavior
across CLI, SDK, and Web UI.
"""

import json
import logging
import os
from typing import List

import boto3
from idp_common.delete_documents import delete_single_document
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")


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
    """Handle document deletion requests from AppSync."""
    logger.info(f"Delete document resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        # Defense-in-depth: deleteDocument is an Admin+Author operation.
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: deleteDocument requires Admin or Author group"
            )

        object_keys: List[str] = event["arguments"]["objectKeys"]

        # Validate input
        if not object_keys or not isinstance(object_keys, list):
            raise ValueError("objectKeys must be a non-empty list")

        tracking_table = dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])
        input_bucket = os.environ["INPUT_BUCKET"]
        output_bucket = os.environ["OUTPUT_BUCKET"]

        logger.info(f"Preparing to delete {len(object_keys)} documents: {object_keys}")

        deleted_count = 0
        failed_count = 0

        # Delete each document using the shared idp_common implementation
        for object_key in object_keys:
            logger.info(f"Processing deletion for document: {object_key}")

            result = delete_single_document(
                object_key=object_key,
                tracking_table=tracking_table,
                s3_client=s3,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
            )

            if result["success"]:
                deleted_count += 1
                logger.info(f"Successfully deleted document: {object_key}")
            else:
                failed_count += 1
                logger.error(
                    f"Failed to delete document {object_key}: {result.get('errors', [])}"
                )

        logger.info(
            f"Completed deletion: {deleted_count} successful, {failed_count} failed"
        )
        # `failed_count == 0`, not `deleted_count > 0 or ...`: the UI sends the whole
        # multi-select here, and the old expression answered True whenever *any* document
        # was removed — so a selection of ten where one document's tracking row could not
        # be cleared reported a complete delete. The per-document errors are already in
        # the log; this boolean is the only thing the caller sees (#1238).
        return failed_count == 0

    except Exception as e:
        logger.error(f"Error in delete_document resolver: {str(e)}", exc_info=True)
        raise e