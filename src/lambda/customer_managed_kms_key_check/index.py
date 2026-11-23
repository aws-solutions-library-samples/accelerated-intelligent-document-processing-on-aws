# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom::CustomerManagedKmsKeyCheck — keep CustomerManagedKmsKeyArn a
creation-time choice, and check a supplied key before anything uses it.

The main template encrypts its data with one key: the one it creates
(``CustomerManagedEncryptionKey``) when ``CustomerManagedKmsKeyArn`` is empty,
or the supplied key otherwise. Changing that choice on an existing stack would
point every bucket, table, queue, topic, log group and secret at a different key
while the data already written stays encrypted with the old one, and the
application roles would lose their grant on the old one. So the choice is fixed
when the stack is created, and this handler refuses any later change:

* ``Update`` whose ``KeyArn`` differs from the previous value — a change in
  either direction, including clearing it — fails, unless the stack is rolling
  back (the rollback restores the value this resource last accepted).
* ``Create`` with a non-empty ``KeyArn`` while the stack is being *updated*
  fails: that is an existing stack, created before this resource existed with
  its own key, being given a supplied key on upgrade.
* ``Create`` with a non-empty ``KeyArn`` on a new stack checks the key exists,
  is enabled, is a symmetric encrypt/decrypt customer managed key, and is in
  this stack's account and Region, so a wrong ARN fails here with a reason
  instead of deep in a nested stack.

An empty ``KeyArn`` on ``Create`` makes no API call: that is every default
deployment, new or upgraded.

Under the Terraform custom-resource bridge there is no CloudFormation stack, so
the stack-status lookup answers "does not exist". The ``Update`` rule still
applies there; the "added to an existing stack" rule cannot be decided and is
skipped, and Terraform's own plan guard refuses the deletion of the
deployment's key that setting the variable on an existing module would plan.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Tuple

import boto3
import cfnresponse
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_RETRIES = Config(retries={"max_attempts": 8, "mode": "standard"})

SUCCESS = "SUCCESS"
FAILED = "FAILED"

#: The stack-status lookup's answer when there is no CloudFormation stack
#: (the Terraform bridge), as distinct from a lookup that failed.
NO_STACK = "NO_STACK"

CHANGE_REFUSED = (
    "CustomerManagedKmsKeyArn is fixed when the stack is created and cannot be "
    "changed on an existing stack (it was '{old}', this update asks for "
    "'{new}'). Data already written stays encrypted with the key it was written "
    "with, so switching keys would leave it unreadable to the application. "
    "Deploy this update with the parameter's previous value; to use a "
    "different key, deploy a new stack."
)

ADDED_ON_UPGRADE_REFUSED = (
    "CustomerManagedKmsKeyArn can only be set when a stack is created. This "
    "stack already exists and encrypts its data with the key it created, so it "
    "cannot be switched to '{new}' by an update. Deploy this update with the "
    "parameter empty; to use your own key, deploy a new stack."
)


def _key_problem(
    meta: dict[str, Any], key_arn: str, region: str, account: str
) -> Optional[str]:
    """Why the key described by ``meta`` cannot encrypt this stack, or None."""
    arn_parts = key_arn.split(":")
    if len(arn_parts) < 6 or arn_parts[3] != region:
        return f"the key {key_arn} is not in this stack's Region ({region})"
    if meta.get("AWSAccountId") != account:
        return (
            f"the key {key_arn} belongs to account {meta.get('AWSAccountId')}, "
            f"not to this stack's account ({account})"
        )
    if meta.get("KeyManager") != "CUSTOMER":
        return f"the key {key_arn} is an AWS managed key; supply a customer managed key"
    if meta.get("KeySpec") != "SYMMETRIC_DEFAULT":
        return f"the key {key_arn} is {meta.get('KeySpec')}; supply a symmetric (SYMMETRIC_DEFAULT) key"
    if meta.get("KeyUsage") != "ENCRYPT_DECRYPT":
        return f"the key {key_arn} has KeyUsage {meta.get('KeyUsage')}; it must be ENCRYPT_DECRYPT"
    if meta.get("KeyState") != "Enabled":
        return f"the key {key_arn} is {meta.get('KeyState')}; it must be Enabled"
    return None


def decide(
    event: dict[str, Any],
    stack_status: Callable[[str], str],
    describe_key: Callable[[str], dict[str, Any]],
    region: str,
    account: str,
) -> Tuple[str, str]:
    """``(status, reason)`` for one custom-resource request.

    ``stack_status(stack_id)`` returns the stack's status, or ``NO_STACK``;
    it raises when the lookup fails. ``describe_key(arn)`` returns
    ``KeyMetadata`` and raises ``ClientError`` when the key cannot be read.
    """
    request = event["RequestType"]
    if request == "Delete":
        return SUCCESS, "Nothing to clean up"

    new = event.get("ResourceProperties", {}).get("KeyArn", "") or ""

    if request == "Update":
        old = event.get("OldResourceProperties", {}).get("KeyArn", "") or ""
        if old == new:
            return SUCCESS, "CustomerManagedKmsKeyArn unchanged"
        try:
            status = stack_status(event["StackId"])
        except Exception as e:  # fail closed: a change is only allowed in a rollback
            logger.error("Stack status lookup failed: %s", e)
            status = ""
        if "ROLLBACK" in status:
            return SUCCESS, f"Stack is {status}; restoring the previous value"
        return FAILED, CHANGE_REFUSED.format(old=old, new=new)

    # Create
    if not new:
        return SUCCESS, "The stack creates and manages its own key"
    try:
        status = stack_status(event["StackId"])
    except Exception as e:
        return (
            FAILED,
            f"Could not read this stack's status to check CustomerManagedKmsKeyArn: {e}",
        )
    if status.startswith("UPDATE"):
        return FAILED, ADDED_ON_UPGRADE_REFUSED.format(new=new)
    try:
        meta = describe_key(new)
    except ClientError as e:
        return FAILED, (
            f"CustomerManagedKmsKeyArn: cannot describe {new} "
            f"({e.response.get('Error', {}).get('Code', 'error')}). The key must "
            f"exist in this account and Region, and its key policy must allow this "
            f"account's IAM principals to use it."
        )
    problem = _key_problem(meta, new, region, account)
    if problem:
        return FAILED, f"CustomerManagedKmsKeyArn: {problem}."
    return SUCCESS, f"Using the supplied key {new}"


def _stack_status(cfn: Any) -> Callable[[str], str]:
    def lookup(stack_id: str) -> str:
        try:
            return cfn.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
        except ClientError as e:
            message = e.response.get("Error", {}).get("Message", "")
            if (
                e.response.get("Error", {}).get("Code") == "ValidationError"
                and "does not exist" in message
            ):
                return NO_STACK
            raise

    return lookup


def handler(event, context):
    request = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    logger.info("%s KeyArn=%r", request, props.get("KeyArn", ""))
    status, reason = FAILED, "Unhandled error"
    try:
        region = os.environ["AWS_REGION"]
        account = context.invoked_function_arn.split(":")[4]
        status, reason = decide(
            event,
            _stack_status(boto3.client("cloudformation", config=_RETRIES)),
            lambda arn: boto3.client("kms", config=_RETRIES).describe_key(KeyId=arn)[
                "KeyMetadata"
            ],
            region,
            account,
        )
    except Exception as e:  # report rather than leave CloudFormation waiting
        logger.exception("Check failed")
        reason = f"CustomerManagedKmsKeyArn check failed: {e}"
        if request == "Delete":
            status = SUCCESS
    logger.info("%s: %s", status, reason)
    physical_id = (
        event.get("PhysicalResourceId")
        or f"{props.get('StackName', 'stack')}-kms-key-check"
    )
    cfnresponse.send(
        event,
        context,
        status,
        {"KeyArn": props.get("KeyArn", "")},
        physicalResourceId=physical_id,
        reason=reason,
    )
