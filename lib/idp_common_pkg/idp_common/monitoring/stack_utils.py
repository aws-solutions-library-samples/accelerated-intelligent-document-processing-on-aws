# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility functions for resolving IDP stack names and AWS resource identifiers.

Provides helpers to look up the names and resource identifiers of the deployed
AWS infrastructure (Lambda functions, Step Functions state machines, queues,
etc.) without hardcoding anything.

Stack name resolution priority
--------------------------------
1. Extracted from a document's ``WorkflowExecutionArn`` (Step Functions ARN)
2. ``AWS_STACK_NAME`` environment variable
3. ``STACK_NAME`` environment variable

Usage::

    from idp_common.monitoring.stack_utils import get_stack_name, extract_stack_name_from_arn

    stack = get_stack_name()                         # from env var
    stack = get_stack_name(document_record=item)     # from document ARN
    stack = extract_stack_name_from_arn(execution_arn)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy boto3 client cache
# ---------------------------------------------------------------------------
_cf_client: Optional[Any] = None


def _get_cf_client() -> Any:
    """Return (and lazily create) a module-level CloudFormation boto3 client."""
    global _cf_client
    if _cf_client is None:
        # Under Terraform the stack is a label; the hook answers resource
        # lookups from the deployment's resource directory.
        from idp_common.utils.terraform_stack import install_if_terraform

        install_if_terraform()
        _cf_client = boto3.client("cloudformation")
    return _cf_client


# ---------------------------------------------------------------------------
# Known state machine name suffixes used by the IDP stack
# ---------------------------------------------------------------------------
_STATE_MACHINE_SUFFIXES: List[str] = [
    "-DocumentProcessingWorkflow",
    "-ProcessingStateMachine",
    "-WorkflowStateMachine",
    "-Workflow",
]


def extract_stack_name_from_arn(execution_arn: str) -> str:
    """
    Extract the CloudFormation stack name from a Step Functions execution ARN.

    The state machine name embeds the stack name as a prefix, e.g.::

        arn:aws:states:us-east-1:123456789012:execution:
            MY-STACK-DocumentProcessingWorkflow:exec-id
        → "MY-STACK"

    Args:
        execution_arn: Full Step Functions execution ARN.

    Returns:
        Stack name string, or ``""`` if it cannot be extracted.
    """
    if not execution_arn:
        return ""

    try:
        # ARN format: arn:aws:states:<region>:<account>:execution:<state-machine-name>:<exec-id>
        # A valid Step Functions execution ARN has at least 8 colon-separated fields.
        # Use rsplit(maxsplit=2) so that execution IDs containing colons are handled
        # correctly (the state-machine name is always the second-to-last component).
        if execution_arn.count(":") < 7:
            # Too few fields — definitely not a valid execution ARN
            return ""
        parts = execution_arn.rsplit(":", maxsplit=2)
        # Expected: ["arn:aws:states:...:execution", "<state-machine-name>", "<exec-id>"]
        if len(parts) < 3:
            return ""

        state_machine_name: str = parts[-2]

        # Strip known workflow suffixes to recover the stack name.
        # Use endswith() so that a suffix appearing in the middle of the name
        # is not incorrectly stripped.
        for suffix in _STATE_MACHINE_SUFFIXES:
            if state_machine_name.endswith(suffix):
                return state_machine_name[: -len(suffix)]

        # Fallback: remove the last hyphen-delimited word
        # e.g. "MYSTACK-SomeNewSuffix" → "MYSTACK"
        segments = state_machine_name.split("-")
        if len(segments) > 1:
            return "-".join(segments[:-1])

        return state_machine_name
    except (IndexError, AttributeError) as exc:
        logger.debug(
            "Could not extract stack name from ARN '%s': %s", execution_arn, exc
        )
        return ""


def get_stack_name(
    document_record: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Resolve the IDP CloudFormation stack name.

    Priority:
    1. ``WorkflowExecutionArn`` / ``ExecutionArn`` field in *document_record*
    2. ``AWS_STACK_NAME`` environment variable
    3. ``STACK_NAME`` environment variable

    Args:
        document_record: Optional DynamoDB document record dict.  If provided
                         and contains an execution ARN the stack name is
                         derived from the ARN.

    Returns:
        Stack name string.

    Raises:
        ValueError: If no stack name can be resolved from any source.
    """
    # Priority 1: derive from document execution ARN
    if document_record:
        arn = (
            document_record.get("WorkflowExecutionArn")
            or document_record.get("ExecutionArn")
            or ""
        )
        if arn:
            extracted = extract_stack_name_from_arn(arn)
            if extracted:
                logger.debug("Resolved stack name '%s' from execution ARN", extracted)
                return extracted

    # Priority 2: AWS_STACK_NAME env var
    stack_name = os.environ.get("AWS_STACK_NAME", "")
    if stack_name:
        logger.debug("Resolved stack name '%s' from AWS_STACK_NAME", stack_name)
        return stack_name

    # Priority 3: STACK_NAME env var
    stack_name = os.environ.get("STACK_NAME", "")
    if stack_name:
        logger.debug("Resolved stack name '%s' from STACK_NAME", stack_name)
        return stack_name

    raise ValueError(
        "Stack name could not be resolved: no WorkflowExecutionArn in document_record "
        "and neither AWS_STACK_NAME nor STACK_NAME environment variables are set."
    )


def get_stack_resources(stack_name: str) -> List[Dict[str, Any]]:
    """
    Return all CloudFormation stack resources for the given stack name.

    Args:
        stack_name: CloudFormation stack name.

    Returns:
        List of resource summary dicts from ``list_stack_resources``.
        Returns an empty list if the stack is not found or the call fails.
    """
    try:
        cf = _get_cf_client()
        paginator = cf.get_paginator("list_stack_resources")
        resources: List[Dict[str, Any]] = []
        for page in paginator.paginate(StackName=stack_name):
            resources.extend(page.get("StackResourceSummaries", []))
        return resources
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to list stack resources for '%s': %s", stack_name, exc)
        return []


def get_lambda_function_names(stack_name: str) -> List[str]:
    """
    Return the physical resource IDs of all Lambda functions in the stack.

    Args:
        stack_name: CloudFormation stack name.

    Returns:
        List of Lambda function names (physical resource IDs).
    """
    resources = get_stack_resources(stack_name)
    return [
        r["PhysicalResourceId"]
        for r in resources
        if r.get("ResourceType") == "AWS::Lambda::Function"
        and r.get("PhysicalResourceId")
    ]


def get_state_machine_arn(stack_name: str) -> str:
    """
    Return the ARN of the main document-processing Step Functions state machine.

    Looks for a resource whose logical ID contains "DocumentProcessingWorkflow"
    or "ProcessingStateMachine".

    Args:
        stack_name: CloudFormation stack name.

    Returns:
        State machine ARN string, or ``""`` if not found.
    """
    resources = get_stack_resources(stack_name)
    priority_keywords = [
        "DocumentProcessingWorkflow",
        "ProcessingStateMachine",
        "Workflow",
    ]

    for keyword in priority_keywords:
        for resource in resources:
            if resource.get("ResourceType") != "AWS::StepFunctions::StateMachine":
                continue
            logical_id = resource.get("LogicalResourceId", "")
            if keyword in logical_id:
                arn = resource.get("PhysicalResourceId", "")
                logger.debug(
                    "Found state machine '%s' (logical: %s) in stack '%s'",
                    arn,
                    logical_id,
                    stack_name,
                )
                return arn

    logger.warning("No state machine found in stack '%s'", stack_name)
    return ""
