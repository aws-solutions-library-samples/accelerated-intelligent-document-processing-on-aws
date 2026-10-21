# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Stack Information Module

Discovers and retrieves CloudFormation stack resources needed for CLI operations.
"""

import logging
from typing import Dict, Optional

import boto3

logger = logging.getLogger(__name__)


class StackInfo:
    """Manages CloudFormation stack resource discovery and caching"""

    def __init__(self, stack_name: str, region: Optional[str] = None):
        """
        Initialize stack info manager

        Args:
            stack_name: Name of the CloudFormation stack
            region: AWS region (defaults to session region)
        """
        self.stack_name = stack_name
        self.region = region
        self.cfn = boto3.client("cloudformation", region_name=region)
        self.ssm = boto3.client("ssm", region_name=region)
        self._resources_cache = None
        self._outputs_cache = None

    def get_resources(self) -> Dict[str, str]:
        """
        Get all relevant stack resources

        Returns:
            Dictionary mapping resource logical IDs to physical IDs or values
        """
        if self._resources_cache:
            return self._resources_cache

        logger.info(f"Discovering resources for stack: {self.stack_name}")

        resources = {}

        # Get stack outputs
        outputs = self._get_stack_outputs()

        # Map outputs to friendly names
        resources["InputBucket"] = outputs.get("S3InputBucketName", "")
        resources["OutputBucket"] = outputs.get("S3OutputBucketName", "")
        resources["ConfigurationBucket"] = outputs.get("S3ConfigurationBucketName", "")
        resources["EvaluationBaselineBucket"] = outputs.get(
            "S3EvaluationBaselineBucketName", ""
        )
        resources["TestSetBucket"] = outputs.get("S3TestSetBucketName", "")
        resources["DocumentQueueUrl"] = self._get_queue_url()
        resources["LookupFunctionName"] = outputs.get("LambdaLookupFunctionName", "")
        resources["StateMachineArn"] = outputs.get("StateMachineArn", "")

        # Get DynamoDB table name (for document status tracking)
        resources["DocumentsTable"] = self._get_table_name("TrackingTable")
        # Review queue table (absent on stacks without review queues, e.g. headless)
        resources["HITLTable"] = outputs.get("HITLTableName", "")

        # Get settings parameter name
        resources["SettingsParameter"] = f"{self.stack_name}-Settings"

        self._resources_cache = resources
        logger.info(f"Discovered {len(resources)} resources")

        return resources

    def _get_stack_outputs(self) -> Dict[str, str]:
        """Get stack outputs"""
        if self._outputs_cache:
            return self._outputs_cache

        try:
            response = self.cfn.describe_stacks(StackName=self.stack_name)
            stacks = response.get("Stacks", [])

            if not stacks:
                raise ValueError(f"Stack not found: {self.stack_name}")

            stack = stacks[0]
            outputs = {}

            for output in stack.get("Outputs", []):
                key = output.get("OutputKey", "")
                value = output.get("OutputValue", "")
                outputs[key] = value

            self._outputs_cache = outputs
            return outputs

        except Exception as e:
            logger.error(f"Error getting stack outputs: {e}")
            raise

    def _get_queue_url(self) -> str:
        """Get SQS queue URL from stack resources"""
        try:
            # List stack resources and find DocumentQueue
            paginator = self.cfn.get_paginator("list_stack_resources")

            for page in paginator.paginate(StackName=self.stack_name):
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("LogicalResourceId") == "DocumentQueue":
                        # PhysicalResourceId IS the queue URL for SQS queues
                        queue_url = resource.get("PhysicalResourceId", "")
                        return queue_url

            raise ValueError("DocumentQueue not found in stack resources")

        except Exception as e:
            logger.error(f"Error getting queue URL: {e}")
            raise

    def _get_table_name(self, logical_id: str) -> str:
        """Get DynamoDB table name from stack resources"""
        try:
            paginator = self.cfn.get_paginator("list_stack_resources")

            for page in paginator.paginate(StackName=self.stack_name):
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("LogicalResourceId") == logical_id:
                        return resource.get("PhysicalResourceId", "")

            return ""
        except Exception as e:
            logger.warning(f"Could not get table name for {logical_id}: {e}")
            return ""

    def get_settings(self) -> Dict:
        """Get stack settings from SSM parameter"""
        try:
            param_name = f"{self.stack_name}-Settings"
            response = self.ssm.get_parameter(Name=param_name)

            import json

            settings = json.loads(response["Parameter"]["Value"])
            return settings

        except Exception as e:
            logger.warning(f"Could not load settings: {e}")
            return {}

    def validate_stack(self) -> bool:
        """
        Validate that stack exists and is in a usable state

        Returns:
            True if stack is valid, False otherwise
        """
        try:
            response = self.cfn.describe_stacks(StackName=self.stack_name)
            stacks = response.get("Stacks", [])

            if not stacks:
                return False

            stack = stacks[0]
            status = stack.get("StackStatus", "")

            # Valid statuses for operation
            valid_statuses = [
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            ]

            return status in valid_statuses

        except Exception as e:
            logger.error(f"Error validating stack: {e}")
            return False

    def get_nested_stack_output(
        self, nested_stack_pattern: str, output_key: str
    ) -> Optional[str]:
        """
        Get an output value from a nested CloudFormation stack.

        Args:
            nested_stack_pattern: Pattern to match nested stack LogicalResourceId
                                (case-insensitive, e.g., "apiresolver" matches "APIRESOLVERSTACK")
            output_key: Output key to retrieve from the nested stack

        Returns:
            Output value if found, None otherwise

        Raises:
            ValueError: If nested stack or output not found
        """
        try:
            # Find nested stack by pattern
            resources = self.cfn.describe_stack_resources(StackName=self.stack_name)
            nested_stack_id = None

            for resource in resources["StackResources"]:
                if (
                    resource["ResourceType"] == "AWS::CloudFormation::Stack"
                    and nested_stack_pattern.lower()
                    in resource["LogicalResourceId"].lower()
                ):
                    nested_stack_id = resource["PhysicalResourceId"]
                    nested_stack_name = nested_stack_id.split("/")[1]
                    break

            if not nested_stack_id:
                raise ValueError(
                    f"Nested stack matching '{nested_stack_pattern}' not found in {self.stack_name}"
                )

            # Get output from nested stack
            nested_response = self.cfn.describe_stacks(StackName=nested_stack_name)
            nested_outputs = nested_response["Stacks"][0].get("Outputs", [])

            for output in nested_outputs:
                if output["OutputKey"] == output_key:
                    output_value = output["OutputValue"]
                    logger.debug(
                        f"Found {output_key} in nested stack {nested_stack_name}: {output_value}"
                    )
                    return output_value

            raise ValueError(
                f"Output '{output_key}' not found in nested stack matching '{nested_stack_pattern}'"
            )

        except Exception as e:
            logger.error(f"Error getting nested stack output: {e}")
            raise


def get_stack_resources(
    stack_name: str, region: Optional[str] = None
) -> Dict[str, str]:
    """
    Convenience function to get stack resources

    Args:
        stack_name: Name of the CloudFormation stack
        region: AWS region (optional)

    Returns:
        Dictionary of resource names to values
    """
    stack_info = StackInfo(stack_name, region)
    return stack_info.get_resources()
