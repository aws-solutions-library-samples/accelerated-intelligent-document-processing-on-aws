# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deployer Module

Handles CloudFormation stack deployment from CLI.
"""

import logging
import os
import random
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import boto3

from .s3_security import apply_enforce_ssl_only

logger = logging.getLogger(__name__)


class StackDeployer:
    """Manages CloudFormation stack deployment"""

    def __init__(self, region: Optional[str] = None):
        """
        Initialize stack deployer

        Args:
            region: AWS region (optional)
        """
        self.region = region
        self.cfn = boto3.client("cloudformation", region_name=region)

    def deploy_stack(
        self,
        stack_name: str,
        template_path: Optional[str] = None,
        template_url: Optional[str] = None,
        parameters: Dict[str, str] = None,
        wait: bool = False,
        no_rollback: bool = False,
        role_arn: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """
        Deploy CloudFormation stack

        Args:
            stack_name: Name for the stack
            template_path: Path to local CloudFormation template (optional)
            template_url: URL to CloudFormation template in S3 (optional)
            parameters: Stack parameters
            wait: Whether to wait for stack creation to complete
            no_rollback: If True, disable rollback on failure (DO_NOTHING)
            role_arn: CloudFormation service role ARN (optional)
            tags: Stack-level tags applied to the stack and propagated by
                CloudFormation to all taggable resources (including nested
                stacks). On UPDATE, CloudFormation replaces the entire tag set
                with what is passed here; when None, the Tags key is omitted so
                existing tags are preserved.

        Returns:
            Dictionary with deployment result
        """
        logger.info(f"Deploying stack: {stack_name}")

        if not template_path and not template_url:
            raise ValueError("Either template_path or template_url must be provided")

        # Determine template source
        if template_url:
            logger.info(f"Using template URL: {template_url}")
            template_param = {"TemplateURL": template_url}
        else:
            # Read template from local file
            template_body = self._read_template(template_path)

            # Check template size - CloudFormation has a 51,200 byte limit for direct upload
            template_size = len(template_body.encode("utf-8"))
            if template_size > 51200:  # 50KB limit
                logger.info(
                    f"Template size ({template_size} bytes) exceeds limit, uploading to S3"
                )
                template_url = self._upload_template_to_s3(template_body, stack_name)
                template_param = {"TemplateURL": template_url}
                logger.info(f"Template uploaded to: {template_url}")
            else:
                template_param = {"TemplateBody": template_body}

        # Check if stack exists
        stack_exists = self._stack_exists(stack_name)

        # Build CloudFormation parameters based on operation type
        if stack_exists:
            # For UPDATE: Use UsePreviousValue for parameters not explicitly provided
            current_params = self._get_stack_parameters(stack_name)
            cfn_parameters = []

            # Get valid parameters from the new template to detect deprecated parameters
            # This prevents errors when updating to a template that has removed parameters
            template_body_for_validation = template_param.get("TemplateBody")
            template_url_for_validation = template_param.get("TemplateURL")
            valid_template_params = self._get_template_parameters(
                template_body=template_body_for_validation,
                template_url=template_url_for_validation,
            )

            # Identify deprecated parameters (exist in stack but not in new template)
            deprecated_params = set()
            if (
                valid_template_params
            ):  # Only filter if we successfully got template params
                deprecated_params = set(current_params.keys()) - valid_template_params
                if deprecated_params:
                    logger.warning(
                        f"Dropping deprecated parameters not in new template: {deprecated_params}"
                    )

            # First, handle all existing parameters
            for param_key in current_params.keys():
                # Skip deprecated parameters that no longer exist in the new template
                if param_key in deprecated_params:
                    logger.info(f"Skipping deprecated parameter: {param_key}")
                    continue

                if param_key in (parameters or {}):
                    # User provided new value
                    cfn_parameters.append(
                        {
                            "ParameterKey": param_key,
                            "ParameterValue": parameters[param_key],
                        }
                    )
                else:
                    # Use previous value
                    cfn_parameters.append(
                        {"ParameterKey": param_key, "UsePreviousValue": True}
                    )

            # Then, add any new parameters not in current stack
            for param_key, param_value in (parameters or {}).items():
                if param_key not in current_params:
                    cfn_parameters.append(
                        {"ParameterKey": param_key, "ParameterValue": param_value}
                    )
        else:
            # For CREATE: Use only provided parameters
            cfn_parameters = [
                {"ParameterKey": k, "ParameterValue": v}
                for k, v in (parameters or {}).items()
            ]

        # Prepare common parameters
        common_params = {
            "StackName": stack_name,
            **template_param,
            "Parameters": cfn_parameters,
            "Capabilities": [
                "CAPABILITY_IAM",
                "CAPABILITY_NAMED_IAM",
                "CAPABILITY_AUTO_EXPAND",
            ],
        }

        # Add RoleArn if provided
        if role_arn:
            common_params["RoleARN"] = role_arn

        # Add stack-level tags if provided. Note the asymmetry with Parameters:
        # update_stack replaces the entire tag set with whatever is passed (there
        # is no per-tag UsePreviousValue), and passing no Tags removes them. So we
        # only set the Tags key when the caller explicitly provides tags; omitting
        # it on update preserves the stack's existing tags.
        if tags:
            common_params["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

        try:
            # Capture deploy start time for filtering stale events in failure analysis
            from datetime import datetime, timezone

            deploy_start_time = datetime.now(timezone.utc)

            if stack_exists:
                logger.info(f"Stack {stack_name} exists - updating")
                response = self.cfn.update_stack(**common_params)
                operation = "UPDATE"
            else:
                logger.info(f"Creating new stack: {stack_name}")
                # Set DisableRollback based on no_rollback flag
                disable_rollback = True if no_rollback else False
                common_params["DisableRollback"] = disable_rollback
                response = self.cfn.create_stack(**common_params)
                operation = "CREATE"

            result = {
                "stack_name": stack_name,
                "stack_id": response.get("StackId", ""),
                "operation": operation,
                "status": "INITIATED",
            }

            if wait:
                result = self._wait_for_completion(
                    stack_name, operation, deploy_start_time=deploy_start_time
                )

            # Include deploy_start_time in result so callers (e.g., CLI failure
            # analysis display) can filter stale events from previous deployments
            result["deploy_start_time"] = deploy_start_time

            return result

        except self.cfn.exceptions.AlreadyExistsException:
            raise ValueError(
                f"Stack {stack_name} already exists. Use --update flag to update."
            )
        except Exception as e:
            logger.error(f"Error deploying stack: {e}")
            raise

    def _read_template(self, template_path: str) -> str:
        """Read CloudFormation template file"""
        template_file = Path(template_path)

        if not template_file.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        return template_file.read_text()

    def _upload_template_to_s3(self, template_body: str, stack_name: str) -> str:
        """Upload template to S3 and return URL"""
        from datetime import datetime

        import boto3

        # Get or create bucket for templates
        bucket_name = get_or_create_config_bucket(self.region)

        # Generate unique key
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        s3_key = f"idp-cli/templates/{stack_name}_{timestamp}.yaml"

        # Upload template
        s3 = boto3.client("s3", region_name=self.region)
        s3.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=template_body.encode("utf-8"),
            ServerSideEncryption="AES256",
        )

        # Return S3 URL
        return f"https://s3.{self.region}.amazonaws.com/{bucket_name}/{s3_key}"

    def _stack_exists(self, stack_name: str) -> bool:
        """Check if stack exists"""
        try:
            self.cfn.describe_stacks(StackName=stack_name)
            return True
        except self.cfn.exceptions.ClientError as e:
            if "does not exist" in str(e):
                return False
            raise

    def _get_stack_status(self, stack_name: str) -> Optional[str]:
        """
        Get current stack status

        Args:
            stack_name: Stack name

        Returns:
            Stack status string or None if stack doesn't exist
        """
        try:
            response = self.cfn.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if stacks:
                return stacks[0].get("StackStatus")
            return None
        except self.cfn.exceptions.ClientError as e:
            if "does not exist" in str(e):
                return None
            raise

    def get_stack_operation_in_progress(
        self, stack_name: str
    ) -> Optional[Dict[str, str]]:
        """
        Check if stack has an operation in progress

        Args:
            stack_name: Stack name

        Returns:
            Dictionary with 'operation' and 'status' if in progress, None otherwise
            Operation can be: CREATE, UPDATE, DELETE
        """
        status = self._get_stack_status(stack_name)
        if not status:
            return None

        # Map status to operation type
        in_progress_states = {
            # CREATE operations
            "CREATE_IN_PROGRESS": "CREATE",
            "ROLLBACK_IN_PROGRESS": "CREATE",  # Rollback during create
            # UPDATE operations
            "UPDATE_IN_PROGRESS": "UPDATE",
            "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS": "UPDATE",
            "UPDATE_ROLLBACK_IN_PROGRESS": "UPDATE",
            "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS": "UPDATE",
            # DELETE operations
            "DELETE_IN_PROGRESS": "DELETE",
        }

        if status in in_progress_states:
            return {"operation": in_progress_states[status], "status": status}

        return None

    def cancel_update_stack(self, stack_name: str) -> Dict:
        """
        Cancel an in-progress stack update

        This can only be called when a stack is in UPDATE_IN_PROGRESS status.
        After cancellation, the stack returns to UPDATE_ROLLBACK_IN_PROGRESS
        and then UPDATE_ROLLBACK_COMPLETE.

        Args:
            stack_name: Stack name

        Returns:
            Dictionary with result
        """
        logger.info(f"Canceling update for stack: {stack_name}")

        try:
            self.cfn.cancel_update_stack(StackName=stack_name)
            return {
                "success": True,
                "message": f"Update cancellation initiated for {stack_name}",
            }
        except Exception as e:
            logger.error(f"Error canceling stack update: {e}")
            return {"success": False, "error": str(e)}

    def wait_for_stable_state(
        self, stack_name: str, timeout_seconds: int = 600
    ) -> Dict:
        """
        Wait for stack to reach a stable (non-in-progress) state

        Args:
            stack_name: Stack name
            timeout_seconds: Maximum time to wait

        Returns:
            Dictionary with final status
        """
        logger.info(f"Waiting for stack {stack_name} to reach stable state...")

        stable_states = [
            "CREATE_COMPLETE",
            "CREATE_FAILED",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
            "UPDATE_COMPLETE",
            "UPDATE_FAILED",
            "UPDATE_ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_FAILED",
            "DELETE_COMPLETE",
            "DELETE_FAILED",
        ]

        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                return {
                    "success": False,
                    "error": f"Timeout after {timeout_seconds}s",
                    "status": "TIMEOUT",
                }

            try:
                status = self._get_stack_status(stack_name)

                if status is None:
                    # Stack doesn't exist (deleted)
                    return {
                        "success": True,
                        "status": "DELETE_COMPLETE",
                        "message": "Stack no longer exists",
                    }

                logger.info(f"Stack {stack_name} status: {status} ({int(elapsed)}s)")

                if status in stable_states:
                    return {
                        "success": True,
                        "status": status,
                        "message": f"Stack reached stable state: {status}",
                    }

                time.sleep(10)

            except Exception as e:
                if "does not exist" in str(e):
                    return {
                        "success": True,
                        "status": "DELETE_COMPLETE",
                        "message": "Stack no longer exists",
                    }
                logger.error(f"Error checking stack status: {e}")
                raise

    def monitor_stack_progress(self, stack_name: str, operation: str) -> Dict:
        """
        Monitor an in-progress stack operation until completion

        This function monitors any stack operation (CREATE, UPDATE, DELETE)
        and returns when the operation completes or fails.

        Args:
            stack_name: Stack name
            operation: Operation type (CREATE, UPDATE, DELETE)

        Returns:
            Dictionary with final status
        """
        logger.info(f"Monitoring {operation} operation for stack: {stack_name}")

        # Define completion statuses for each operation type
        complete_statuses = {
            "CREATE": [
                "CREATE_COMPLETE",
                "CREATE_FAILED",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
            ],
            "UPDATE": [
                "UPDATE_COMPLETE",
                "UPDATE_FAILED",
                "UPDATE_ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_FAILED",
            ],
            "DELETE": [
                "DELETE_COMPLETE",
                "DELETE_FAILED",
            ],
        }

        success_statuses = {
            "CREATE": ["CREATE_COMPLETE"],
            "UPDATE": ["UPDATE_COMPLETE"],
            "DELETE": ["DELETE_COMPLETE"],
        }

        target_statuses = complete_statuses.get(operation, [])
        success_set = success_statuses.get(operation, [])
        last_event_time = None

        while True:
            try:
                # For DELETE, handle the case where stack is completely gone
                if operation == "DELETE":
                    try:
                        response = self.cfn.describe_stacks(StackName=stack_name)
                        stacks = response.get("Stacks", [])
                    except self.cfn.exceptions.ClientError as e:
                        if "does not exist" in str(e):
                            # Stack deleted successfully
                            return {
                                "stack_name": stack_name,
                                "operation": operation,
                                "status": "DELETE_COMPLETE",
                                "success": True,
                            }
                        raise
                else:
                    response = self.cfn.describe_stacks(StackName=stack_name)
                    stacks = response.get("Stacks", [])

                if not stacks:
                    if operation == "DELETE":
                        return {
                            "stack_name": stack_name,
                            "operation": operation,
                            "status": "DELETE_COMPLETE",
                            "success": True,
                        }
                    raise ValueError(f"Stack {stack_name} not found")

                stack = stacks[0]
                status = stack.get("StackStatus", "")

                # Log recent events
                events = self.get_stack_events(stack_name, limit=5)
                if events and events[0]["timestamp"] != last_event_time:
                    last_event_time = events[0]["timestamp"]
                    latest = events[0]
                    logger.info(
                        f"{operation}: {latest['resource']} - {latest['status']}"
                    )

                if status in target_statuses:
                    # Operation complete
                    is_success = status in success_set

                    result = {
                        "stack_name": stack_name,
                        "operation": operation,
                        "status": status,
                        "success": is_success,
                        "outputs": self._get_stack_outputs(stack)
                        if is_success and operation != "DELETE"
                        else {},
                    }

                    if not is_success:
                        result["error"] = self._get_stack_failure_reason(stack_name)

                    return result

                # Wait before next check
                time.sleep(10)

            except Exception as e:
                logger.error(f"Error monitoring stack: {e}")
                raise

    def _get_stack_parameters(self, stack_name: str) -> Dict[str, str]:
        """
        Get current parameter values from existing stack

        Args:
            stack_name: Name of the stack

        Returns:
            Dictionary mapping parameter keys to their current values
        """
        try:
            response = self.cfn.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])

            if not stacks:
                return {}

            stack = stacks[0]
            parameters = stack.get("Parameters", [])

            # Build dictionary of current parameter values
            param_dict = {}
            for param in parameters:
                key = param.get("ParameterKey")
                value = param.get("ParameterValue")
                if key:
                    param_dict[key] = value

            return param_dict

        except Exception as e:
            logger.warning(f"Could not get stack parameters: {e}")
            return {}

    def _get_template_parameters(
        self,
        template_body: Optional[str] = None,
        template_url: Optional[str] = None,
    ) -> set:
        """
        Get valid parameter keys from a CloudFormation template using validate_template API.

        This is used to detect deprecated parameters that exist in an existing stack
        but are no longer present in a new template version.

        Args:
            template_body: Template body string (optional)
            template_url: Template URL in S3 (optional)

        Returns:
            Set of valid parameter keys in the template
        """
        try:
            if template_url:
                response = self.cfn.validate_template(TemplateURL=template_url)
            elif template_body:
                response = self.cfn.validate_template(TemplateBody=template_body)
            else:
                logger.warning("No template provided for parameter validation")
                return set()

            # Extract parameter keys from validation response
            parameters = response.get("Parameters", [])
            param_keys = {
                param.get("ParameterKey")
                for param in parameters
                if param.get("ParameterKey")
            }

            logger.debug(f"Template has {len(param_keys)} parameters: {param_keys}")
            return param_keys

        except Exception as e:
            logger.warning(f"Could not validate template parameters: {e}")
            # Return empty set on error - this will cause all existing params to be preserved
            # which maintains backward compatibility if validation fails
            return set()

    def _wait_for_completion(
        self,
        stack_name: str,
        operation: str,
        deploy_start_time: Optional[datetime] = None,
    ) -> Dict:
        """
        Wait for stack operation to complete

        Args:
            stack_name: Stack name
            operation: CREATE or UPDATE
            deploy_start_time: UTC timestamp when deployment was initiated (for filtering stale events)

        Returns:
            Dictionary with final status
        """
        logger.info(f"Waiting for {operation} to complete...")

        complete_statuses = {
            "CREATE": [
                "CREATE_COMPLETE",
                "CREATE_FAILED",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
            ],
            "UPDATE": [
                "UPDATE_COMPLETE",
                "UPDATE_FAILED",
                "UPDATE_ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_FAILED",
            ],
        }

        success_statuses = {
            "CREATE": ["CREATE_COMPLETE"],
            "UPDATE": ["UPDATE_COMPLETE"],
        }

        target_statuses = complete_statuses[operation]
        success_set = success_statuses[operation]
        last_event_time = None

        while True:
            try:
                response = self.cfn.describe_stacks(StackName=stack_name)
                stacks = response.get("Stacks", [])

                if not stacks:
                    raise ValueError(f"Stack {stack_name} not found")

                stack = stacks[0]
                status = stack.get("StackStatus", "")

                # Log recent events
                events = self.get_stack_events(stack_name, limit=5)
                if events and events[0]["timestamp"] != last_event_time:
                    last_event_time = events[0]["timestamp"]
                    latest = events[0]
                    logger.info(
                        f"{operation}: {latest['resource']} - {latest['status']}"
                    )

                if status in target_statuses:
                    # Operation complete
                    is_success = status in success_set

                    result = {
                        "stack_name": stack_name,
                        "operation": operation,
                        "status": status,
                        "success": is_success,
                        "outputs": self._get_stack_outputs(stack),
                    }

                    if not is_success:
                        result["error"] = self._get_stack_failure_reason(
                            stack_name, deploy_start_time=deploy_start_time
                        )

                    return result

                # Wait before next check
                time.sleep(10)

            except Exception as e:
                logger.error(f"Error waiting for stack: {e}")
                raise

    def _get_stack_outputs(self, stack: Dict) -> Dict[str, str]:
        """Extract stack outputs as dictionary"""
        outputs = {}
        for output in stack.get("Outputs", []):
            key = output.get("OutputKey", "")
            value = output.get("OutputValue", "")
            outputs[key] = value
        return outputs

    def _get_stack_failure_reason(
        self,
        stack_name: str,
        deploy_start_time: Optional[datetime] = None,
    ) -> str:
        """Get failure reason from stack events (simple string for backward compatibility)"""
        analysis = self.get_deployment_failure_analysis(
            stack_name, deploy_start_time=deploy_start_time
        )
        root_causes = analysis.get("root_causes", [])
        if root_causes:
            # Return the first root cause as a simple string
            rc = root_causes[0]
            stack_path = rc.get("stack_path", "")
            resource = rc.get("resource", "Unknown")
            reason = rc.get("reason", "Unknown")
            if stack_path:
                return f"{stack_path} → {resource}: {reason}"
            return f"{resource}: {reason}"

        # Fallback to first failed event
        all_failures = analysis.get("all_failures", [])
        if all_failures:
            f = all_failures[0]
            return f"{f.get('resource', 'Unknown')}: {f.get('reason', 'Unknown')}"

        return "Unknown failure reason"

    def get_deployment_failure_analysis(
        self,
        stack_name: str,
        _depth: int = 0,
        deploy_start_time: Optional[datetime] = None,
    ) -> Dict:
        """
        Analyze deployment failure by recursively collecting failed events
        from the main stack and all nested stacks.

        Args:
            stack_name: Stack name or ARN
            _depth: Internal recursion depth counter (max 5)
            deploy_start_time: UTC timestamp when deployment was initiated.
                Only events after this time are considered, preventing stale
                errors from previous deployments from being reported.

        Returns:
            Dictionary with:
                - root_causes: List of actual root cause failures (actionable errors)
                - all_failures: List of all failed events across all stacks
                - stack_name: The top-level stack name
        """
        if _depth > 5:
            return {"root_causes": [], "all_failures": [], "stack_name": stack_name}

        all_failures = []

        # Collect all FAILED events from this stack (paginated)
        try:
            paginator = self.cfn.get_paginator("describe_stack_events")
            for page in paginator.paginate(StackName=stack_name):
                for event in page.get("StackEvents", []):
                    status = event.get("ResourceStatus", "")
                    if "FAILED" not in status:
                        continue

                    # Filter out stale events from previous deployments
                    if deploy_start_time:
                        event_time = event.get("Timestamp")
                        if event_time and event_time < deploy_start_time:
                            continue

                    reason = event.get("ResourceStatusReason", "")
                    resource_id = event.get("LogicalResourceId", "Unknown")
                    resource_type = event.get("ResourceType", "")
                    physical_id = event.get("PhysicalResourceId", "")

                    # Extract a short stack name for display
                    display_stack = stack_name
                    if "/" in str(stack_name):
                        # ARN format: arn:aws:...:stack/name/guid
                        try:
                            display_stack = str(stack_name).split("/")[1]
                        except IndexError:
                            pass

                    failure = {
                        "resource": resource_id,
                        "resource_type": resource_type,
                        "reason": reason,
                        "status": status,
                        "physical_id": physical_id,
                        "stack": display_stack,
                        "stack_path": "",
                        "is_nested_wrapper": False,
                        "is_cascade": False,
                    }

                    # Identify cascade failures (not root causes)
                    if reason and (
                        "Resource creation cancelled" in reason
                        or "resource creation Cancelled" in reason
                        or "Resource update cancelled" in reason
                    ):
                        failure["is_cascade"] = True

                    # Identify nested stack wrapper messages
                    if (
                        resource_type == "AWS::CloudFormation::Stack"
                        and reason
                        and (
                            "was not successfully created" in reason
                            or "was not successfully updated" in reason
                        )
                    ):
                        failure["is_nested_wrapper"] = True

                        # Recursively get failures from the nested stack
                        if physical_id:
                            nested_analysis = self.get_deployment_failure_analysis(
                                physical_id,
                                _depth=_depth + 1,
                                deploy_start_time=deploy_start_time,
                            )
                            # Prepend our stack path to nested failures
                            for nested_failure in nested_analysis.get(
                                "all_failures", []
                            ):
                                if nested_failure.get("stack_path"):
                                    nested_failure["stack_path"] = (
                                        f"{resource_id} → {nested_failure['stack_path']}"
                                    )
                                else:
                                    nested_failure["stack_path"] = resource_id
                            all_failures.extend(nested_analysis.get("all_failures", []))

                    all_failures.append(failure)

        except Exception as e:
            logger.warning(f"Error getting stack events for {stack_name}: {e}")
            all_failures.append(
                {
                    "resource": "Unknown",
                    "resource_type": "",
                    "reason": f"Could not retrieve stack events: {e}",
                    "status": "UNKNOWN",
                    "physical_id": "",
                    "stack": str(stack_name),
                    "stack_path": "",
                    "is_nested_wrapper": False,
                    "is_cascade": False,
                }
            )

        # Identify root causes: failures that are NOT cascades and NOT nested wrappers
        root_causes = [
            f
            for f in all_failures
            if not f["is_cascade"] and not f["is_nested_wrapper"]
        ]

        return {
            "root_causes": root_causes,
            "all_failures": all_failures,
            "stack_name": stack_name,
        }

    def get_stack_events(self, stack_name: str, limit: int = 20) -> List[Dict]:
        """
        Get recent stack events

        Args:
            stack_name: Stack name
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        try:
            response = self.cfn.describe_stack_events(StackName=stack_name)
            events = response.get("StackEvents", [])[:limit]

            return [
                {
                    "timestamp": event.get("Timestamp", ""),
                    "resource": event.get("LogicalResourceId", ""),
                    "status": event.get("ResourceStatus", ""),
                    "reason": event.get("ResourceStatusReason", ""),
                }
                for event in events
            ]
        except Exception as e:
            logger.error(f"Error getting stack events: {e}")
            return []

    def delete_stack(
        self,
        stack_name: str,
        empty_buckets: bool = False,
        wait: bool = True,
    ) -> Dict:
        """
        Delete CloudFormation stack

        Args:
            stack_name: Name of stack to delete
            empty_buckets: Whether to empty S3 buckets before deletion
            wait: Whether to wait for deletion to complete

        Returns:
            Dictionary with deletion result including stack_id
        """
        logger.info(f"Deleting stack: {stack_name}")

        # Check if stack exists
        if not self._stack_exists(stack_name):
            raise ValueError(f"Stack '{stack_name}' does not exist")

        # Get stack ID before deletion (needed for querying deleted stacks)
        try:
            response = self.cfn.describe_stacks(StackName=stack_name)
            stack_id = response["Stacks"][0]["StackId"]
        except Exception as e:
            logger.warning(f"Could not get stack ID: {e}")
            stack_id = stack_name  # Fallback to name

        # Get stack resources to find buckets
        bucket_info = self._get_stack_buckets(stack_name)

        # Empty buckets if requested
        if empty_buckets and bucket_info:
            self._empty_buckets(bucket_info)

        # Delete stack
        try:
            self.cfn.delete_stack(StackName=stack_name)
            logger.info(f"Stack deletion initiated: {stack_name}")

            result = {
                "stack_name": stack_name,
                "stack_id": stack_id,
                "operation": "DELETE",
                "status": "INITIATED",
            }

            if wait:
                result = self._wait_for_deletion(stack_name)
                # Ensure stack_id is preserved after wait
                result["stack_id"] = stack_id

                # Clean up additional stack-specific resources after stack deletion completes
                logger.info("Cleaning up additional stack-specific resources...")
                self._cleanup_additional_resources(stack_name)

            return result

        except Exception as e:
            logger.error(f"Error deleting stack: {e}")
            raise

    def _get_stack_buckets(self, stack_name: str) -> List[Dict]:
        """
        Get S3 buckets from stack

        Args:
            stack_name: Stack name

        Returns:
            List of bucket information dictionaries
        """
        buckets = []

        try:
            # Get stack resources
            paginator = self.cfn.get_paginator("list_stack_resources")
            pages = paginator.paginate(StackName=stack_name)

            for page in pages:
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") == "AWS::S3::Bucket":
                        bucket_name = resource.get("PhysicalResourceId")
                        if bucket_name:
                            buckets.append(
                                {
                                    "logical_id": resource.get("LogicalResourceId"),
                                    "bucket_name": bucket_name,
                                }
                            )

            return buckets

        except Exception as e:
            logger.error(f"Error getting stack buckets: {e}")
            return []

    def _empty_buckets(self, bucket_info: List[Dict]) -> None:
        """
        Empty S3 buckets

        Args:
            bucket_info: List of bucket information dictionaries
        """
        s3 = boto3.resource("s3", region_name=self.region)

        for bucket_dict in bucket_info:
            bucket_name = bucket_dict["bucket_name"]
            logical_id = bucket_dict["logical_id"]

            try:
                logger.info(f"Emptying bucket {logical_id}: {bucket_name}")
                bucket = s3.Bucket(bucket_name)  # type: ignore

                # Delete all objects and versions
                bucket.object_versions.all().delete()  # type: ignore
                logger.info(f"Emptied bucket: {bucket_name}")

            except Exception as e:
                # Handle missing buckets gracefully - if bucket doesn't exist, it's already "empty"
                if "NoSuchBucket" in str(e):
                    logger.warning(
                        f"Bucket {bucket_name} does not exist, skipping empty operation"
                    )
                    continue

                logger.error(f"Error emptying bucket {bucket_name}: {e}")
                raise Exception(
                    f"Failed to empty bucket {bucket_name}. You may need to empty it manually."
                )

    def _wait_for_deletion(self, stack_name: str) -> Dict:
        """
        Wait for stack deletion to complete

        Args:
            stack_name: Stack name

        Returns:
            Dictionary with final status
        """
        logger.info("Waiting for DELETE to complete...")

        while True:
            try:
                response = self.cfn.describe_stacks(StackName=stack_name)
                stacks = response.get("Stacks", [])

                if not stacks:
                    # Stack no longer exists - deletion complete
                    return {
                        "stack_name": stack_name,
                        "operation": "DELETE",
                        "status": "DELETE_COMPLETE",
                        "success": True,
                    }

                stack = stacks[0]
                status = stack.get("StackStatus", "")
                logger.info(f"DELETE status: {status}")

                if status == "DELETE_FAILED":
                    return {
                        "stack_name": stack_name,
                        "operation": "DELETE",
                        "status": status,
                        "success": False,
                        "error": self._get_stack_failure_reason(stack_name),
                    }

                # Wait before next check
                time.sleep(10)

            except self.cfn.exceptions.ClientError as e:
                if "does not exist" in str(e):
                    # Stack deleted successfully
                    return {
                        "stack_name": stack_name,
                        "operation": "DELETE",
                        "status": "DELETE_COMPLETE",
                        "success": True,
                    }
                raise

    def get_bucket_info(self, stack_name: str) -> List[Dict]:
        """
        Get information about S3 buckets in stack

        Args:
            stack_name: Stack name

        Returns:
            List of bucket information with object counts and sizes
        """
        buckets = self._get_stack_buckets(stack_name)
        s3 = boto3.client("s3", region_name=self.region)

        for bucket_dict in buckets:
            bucket_name = bucket_dict["bucket_name"]

            try:
                # Get bucket statistics
                response = s3.list_objects_v2(Bucket=bucket_name)
                objects = response.get("Contents", [])

                bucket_dict["object_count"] = len(objects)
                bucket_dict["total_size"] = sum(obj.get("Size", 0) for obj in objects)

                # Convert size to human-readable format
                size_mb = bucket_dict["total_size"] / (1024 * 1024)
                bucket_dict["size_display"] = f"{size_mb:.2f} MB"

            except Exception as e:
                logger.warning(f"Could not get stats for {bucket_name}: {e}")
                bucket_dict["object_count"] = 0
                bucket_dict["total_size"] = 0
                bucket_dict["size_display"] = "Unknown"

        return buckets

    def get_retained_resources_after_deletion(
        self, stack_name: str, include_nested: bool = True
    ) -> Dict:
        """
        Get resources that CloudFormation didn't delete

        Queries stack after deletion attempt to find resources
        with status DELETE_SKIPPED or still existing.

        Args:
            stack_name: Stack name
            include_nested: Include nested stack resources

        Returns:
            Dictionary with categorized retained resources
        """
        retained_resources = {
            "dynamodb_tables": [],
            "log_groups": [],
            "s3_buckets": [],
            "other": [],
        }

        # Get all stacks to check (main + nested)
        stacks_to_check = [stack_name]

        if include_nested:
            nested_stacks = self._get_nested_stacks(stack_name)
            stacks_to_check.extend(nested_stacks)

        for stack in stacks_to_check:
            try:
                # Query resources for this stack
                paginator = self.cfn.get_paginator("list_stack_resources")
                pages = paginator.paginate(StackName=stack)

                for page in pages:
                    for resource in page.get("StackResourceSummaries", []):
                        status = resource.get("ResourceStatus")
                        resource_type = resource.get("ResourceType")
                        physical_id = resource.get("PhysicalResourceId")

                        # Special handling for log groups - CF may mark DELETE_COMPLETE but they still exist
                        if resource_type == "AWS::Logs::LogGroup":
                            if physical_id and self._log_group_exists(physical_id):
                                resource_info = {
                                    "logical_id": resource.get("LogicalResourceId"),
                                    "physical_id": physical_id,
                                    "type": resource_type,
                                    "status": "EXISTS",  # Override CF status
                                    "status_reason": "Verified to exist in CloudWatch",
                                    "stack": stack,
                                }
                                retained_resources["log_groups"].append(resource_info)
                        # For other resources, use CF status
                        elif status not in ["DELETE_COMPLETE", "DELETE_IN_PROGRESS"]:
                            resource_info = {
                                "logical_id": resource.get("LogicalResourceId"),
                                "physical_id": physical_id,
                                "type": resource_type,
                                "status": status,
                                "status_reason": resource.get(
                                    "ResourceStatusReason", ""
                                ),
                                "stack": stack,
                            }

                            # Categorize by type
                            if resource_type == "AWS::DynamoDB::Table":
                                retained_resources["dynamodb_tables"].append(
                                    resource_info
                                )
                            elif resource_type == "AWS::S3::Bucket":
                                retained_resources["s3_buckets"].append(resource_info)
                            else:
                                retained_resources["other"].append(resource_info)

            except self.cfn.exceptions.ClientError as e:
                # Stack might not exist anymore - that's ok
                if "does not exist" not in str(e):
                    logger.warning(f"Error checking resources for {stack}: {e}")

        return retained_resources

    def _get_nested_stacks(self, stack_name: str) -> List[str]:
        """
        Recursively find all nested stacks

        Args:
            stack_name: Parent stack name

        Returns:
            List of nested stack names
        """
        nested_stacks = []

        try:
            paginator = self.cfn.get_paginator("list_stack_resources")
            pages = paginator.paginate(StackName=stack_name)

            for page in pages:
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") == "AWS::CloudFormation::Stack":
                        nested_name = resource.get("PhysicalResourceId")
                        if nested_name:
                            nested_stacks.append(nested_name)
                            # Recursively get nested stacks of nested stacks
                            nested_stacks.extend(self._get_nested_stacks(nested_name))

        except Exception as e:
            logger.warning(f"Error getting nested stacks for {stack_name}: {e}")

        return nested_stacks

    def _log_group_exists(self, log_group_name: str) -> bool:
        """
        Check if log group actually exists in CloudWatch

        Args:
            log_group_name: Log group name to check

        Returns:
            True if log group exists, False otherwise
        """
        logs = boto3.client("logs", region_name=self.region)

        try:
            response = logs.describe_log_groups(
                logGroupNamePrefix=log_group_name, limit=1
            )
            # Check if exact match exists
            for group in response.get("logGroups", []):
                if group["logGroupName"] == log_group_name:
                    return True
            return False
        except Exception as e:
            logger.warning(f"Error checking log group {log_group_name}: {e}")
            return False

    def _get_stack_cloudfront_distributions(self, stack_name: str) -> List[Dict]:
        """
        Get CloudFront distributions from stack resources

        Args:
            stack_name: Stack name

        Returns:
            List of CloudFront distribution information
        """
        distributions = []

        try:
            paginator = self.cfn.get_paginator("list_stack_resources")
            pages = paginator.paginate(StackName=stack_name)

            for page in pages:
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") == "AWS::CloudFront::Distribution":
                        dist_id = resource.get("PhysicalResourceId")
                        if dist_id:
                            distributions.append(
                                {
                                    "logical_id": resource.get("LogicalResourceId"),
                                    "distribution_id": dist_id,
                                    "status": resource.get("ResourceStatus"),
                                }
                            )

            return distributions

        except self.cfn.exceptions.ClientError as e:
            if "does not exist" in str(e):
                # Stack deleted - distributions should be gone
                return []
            logger.warning(f"Error getting CloudFront distributions: {e}")
            return []

    def _verify_cloudfront_distributions_deleted(
        self, stack_name: str, max_wait_seconds: int = 300
    ) -> None:
        """
        Verify CloudFront distributions are deleted before proceeding with S3 deletion

        This prevents orphaned CloudFront distributions pointing to deleted S3 origins.

        Args:
            stack_name: Stack name
            max_wait_seconds: Maximum time to wait for distributions to be deleted

        Raises:
            Exception: If distributions still exist after max wait time
        """
        # Get CloudFront distributions from stack
        distributions = self._get_stack_cloudfront_distributions(stack_name)

        if not distributions:
            logger.info("No CloudFront distributions found in stack")
            return

        logger.info(
            f"Verifying {len(distributions)} CloudFront distribution(s) are deleted..."
        )

        cloudfront = boto3.client("cloudfront")
        start_time = time.time()

        for dist_info in distributions:
            dist_id = dist_info["distribution_id"]
            logical_id = dist_info["logical_id"]

            # Check if distribution still exists
            while True:
                try:
                    response = cloudfront.get_distribution(Id=dist_id)
                    dist_config = response.get("Distribution", {})
                    status = dist_config.get("Status", "")
                    enabled = dist_config.get("DistributionConfig", {}).get(
                        "Enabled", False
                    )

                    elapsed = time.time() - start_time

                    if elapsed > max_wait_seconds:
                        raise Exception(
                            f"CloudFront distribution {logical_id} ({dist_id}) still exists after {max_wait_seconds}s. "
                            f"Status: {status}, Enabled: {enabled}. "
                            f"Cannot proceed with S3 bucket deletion to prevent orphaned distributions. "
                            f"Please disable/delete the distribution manually and retry."
                        )

                    # Distribution still exists - wait for it to be deleted
                    logger.info(
                        f"  Waiting for {logical_id} ({dist_id}) to be deleted... "
                        f"Status: {status}, Enabled: {enabled} ({int(elapsed)}s elapsed)"
                    )
                    time.sleep(10)

                except cloudfront.exceptions.NoSuchDistribution:
                    # Distribution deleted - good to proceed
                    logger.info(
                        f"CloudFront distribution {logical_id} ({dist_id}) is deleted"
                    )
                    break
                except Exception as e:
                    if "NoSuchDistribution" in str(e):
                        # Distribution deleted
                        logger.info(
                            f"CloudFront distribution {logical_id} ({dist_id}) is deleted"
                        )
                        break
                    else:
                        raise

    def _discover_auto_created_log_groups(self, stack_name: str) -> List[str]:
        """
        Discover auto-created log groups that match stack name patterns

        These are log groups created automatically by AWS services (Lambda, CodeBuild,
        Glue Crawlers, etc.) that are not tracked by CloudFormation.

        Args:
            stack_name: Stack name to match patterns against

        Returns:
            List of log group names matching stack patterns
        """
        logs = boto3.client("logs", region_name=self.region)
        discovered_log_groups = []

        # Define patterns to match - these are auto-created by AWS services
        # Use exact prefixes to avoid inadvertent matches to longer stack names
        # (e.g., "idp1" should not match "idp10")
        patterns_to_check = [
            # Lambda functions - pattern requires hyphen after stack name
            f"/aws/lambda/{stack_name}-DOCUMENTKB",
            f"/aws/lambda/{stack_name}-BDASAMPLEPROJECT",  # BDA sample project
            f"/aws/lambda/{stack_name}-DashboardMergerFunction",
            f"/aws/lambda/{stack_name}-InitializeConcurrencyTableLambda",
            # Nested stacks - pattern requires hyphen after stack name
            f"/{stack_name}-PATTERN1STACK-",  # e.g., /IDPDocker-P1-PATTERN1STACK-ABC123/lambda/...
            f"/{stack_name}-PATTERN2STACK-",
            # CodeBuild projects - pattern requires hyphen after stack name
            f"/aws/codebuild/{stack_name}-PATTERN1STACK",  # Nested stack CodeBuild
            f"/aws/codebuild/{stack_name}-PATTERN2STACK",
            f"/aws/codebuild/{stack_name}-webui-build",  # Main stack webui build
            # Glue crawlers - pattern requires hyphen after stack name
            f"/aws-glue/crawlers-role/{stack_name}-DocumentSectionsCrawlerRole",
        ]

        # Also check for explicit log group names (these may or may not be in CFN)
        # These are exact prefixes with hyphens to prevent matching longer stack names
        explicit_patterns = [
            f"{stack_name}-GetDomainLambdaLogGroup-",
            f"{stack_name}-StacknameCheckFunctionLogGroup-",
            f"{stack_name}-ConfigurationCopyFunctionLogGroup-",
            f"{stack_name}-UpdateSettingsFunctionLogGroup-",
        ]

        try:
            # Use paginator to handle large numbers of log groups
            paginator = logs.get_paginator("describe_log_groups")

            # Track matches found per pattern for debugging
            pattern_match_count = {}

            # Check each pattern
            for pattern in patterns_to_check:
                try:
                    matches_for_pattern = 0
                    page_iterator = paginator.paginate(logGroupNamePrefix=pattern)

                    for page in page_iterator:
                        for log_group in page.get("logGroups", []):
                            log_group_name = log_group["logGroupName"]

                            # Additional validation: ensure we don't match longer stack names
                            # The pattern already includes a hyphen, so this should be safe
                            # But we double-check by ensuring the log group starts with exactly our pattern
                            if log_group_name.startswith(pattern):
                                if log_group_name not in discovered_log_groups:
                                    discovered_log_groups.append(log_group_name)
                                    matches_for_pattern += 1
                                    logger.debug(
                                        f"Discovered auto-created log group: {log_group_name}"
                                    )

                    pattern_match_count[pattern] = matches_for_pattern

                except Exception as e:
                    logger.warning(f"Error checking pattern {pattern}: {e}")
                    continue

            # Check explicit patterns
            for pattern in explicit_patterns:
                try:
                    matches_for_pattern = 0
                    response = logs.describe_log_groups(
                        logGroupNamePrefix=pattern,
                        limit=50,  # Should be enough for exact matches
                    )

                    for log_group in response.get("logGroups", []):
                        log_group_name = log_group["logGroupName"]
                        # Verify exact prefix match to avoid matching longer stack names
                        if log_group_name.startswith(pattern):
                            if log_group_name not in discovered_log_groups:
                                discovered_log_groups.append(log_group_name)
                                matches_for_pattern += 1
                                logger.debug(
                                    f"Discovered explicit log group: {log_group_name}"
                                )

                    pattern_match_count[pattern] = matches_for_pattern

                except Exception as e:
                    logger.warning(f"Error checking explicit pattern {pattern}: {e}")
                    continue

            # Log summary with pattern match counts
            if discovered_log_groups:
                logger.info(
                    f"Discovered {len(discovered_log_groups)} auto-created log groups for stack {stack_name}"
                )
                logger.debug(f"Pattern match counts: {pattern_match_count}")
            else:
                logger.info(f"No auto-created log groups found for stack {stack_name}")
                logger.debug(
                    f"Checked {len(patterns_to_check) + len(explicit_patterns)} patterns"
                )

            return discovered_log_groups

        except Exception as e:
            logger.error(f"Error discovering auto-created log groups: {e}")
            return []

    def cleanup_retained_resources(self, stack_identifier: str) -> Dict:
        """
        Delete resources that CloudFormation retained

        Args:
            stack_identifier: Stack name or stack ID (use ID for deleted stacks)

        Returns:
            Cleanup summary with deleted resources and errors
        """
        logger.info("Analyzing retained resources...")

        # Get resources that weren't deleted by CloudFormation
        retained = self.get_retained_resources_after_deletion(stack_identifier)

        # Always clean up additional stack-specific resources
        self._cleanup_additional_resources(stack_identifier)

        # Extract stack name from identifier (handle both stack name and stack ARN/ID)
        # Stack ARN format: arn:aws:cloudformation:region:account:stack/stack-name/guid
        stack_name = stack_identifier
        if stack_identifier.startswith("arn:"):
            try:
                # Extract stack name from ARN
                stack_name = stack_identifier.split("/")[1]
            except IndexError:
                logger.warning(
                    f"Could not extract stack name from ARN: {stack_identifier}"
                )

        # Discover auto-created log groups that CloudFormation doesn't track
        logger.info("Discovering auto-created log groups...")
        auto_created_log_groups = self._discover_auto_created_log_groups(stack_name)

        # Merge auto-created log groups with CloudFormation-tracked ones
        # Use a set to avoid duplicates
        cfn_log_group_names = {lg["physical_id"] for lg in retained["log_groups"]}

        for log_group_name in auto_created_log_groups:
            if log_group_name not in cfn_log_group_names:
                # Add to retained log groups list
                retained["log_groups"].append(
                    {
                        "logical_id": "Auto-created",
                        "physical_id": log_group_name,
                        "type": "AWS::Logs::LogGroup",
                        "status": "AUTO_CREATED",
                        "status_reason": "Auto-created by AWS service",
                        "stack": stack_name,
                    }
                )

        # Count resources (including newly discovered log groups)
        total = (
            len(retained["dynamodb_tables"])
            + len(retained["log_groups"])
            + len(retained["s3_buckets"])
        )

        if total == 0:
            logger.info(
                "No retained resources found - CloudFormation deleted everything!"
            )
            return {"total_deleted": 0, "errors": []}

        logger.info(
            f"Found {total} retained resources: "
            f"DynamoDB={len(retained['dynamodb_tables'])}, "
            f"Logs={len(retained['log_groups'])}, "
            f"S3={len(retained['s3_buckets'])}"
        )

        if retained["other"]:
            logger.warning(
                f"  Other ({len(retained['other'])}): some resources cannot be auto-deleted"
            )

        results = {
            "dynamodb_deleted": [],
            "logs_deleted": [],
            "buckets_deleted": [],
            "errors": [],
        }

        # Phase 1: DynamoDB
        for table in retained["dynamodb_tables"]:
            try:
                self._delete_dynamodb_table(table["physical_id"])
                results["dynamodb_deleted"].append(table["physical_id"])
                logger.info(f"Deleted DynamoDB table: {table['physical_id']}")
            except Exception as e:
                results["errors"].append(
                    {
                        "resource": table["physical_id"],
                        "type": "DynamoDB",
                        "error": str(e),
                    }
                )

        # Phase 2: Log Groups
        for log_group in retained["log_groups"]:
            try:
                self._delete_log_group(log_group["physical_id"])
                results["logs_deleted"].append(log_group["physical_id"])
                logger.info(f"Deleted log group: {log_group['physical_id']}")
            except Exception as e:
                results["errors"].append(
                    {
                        "resource": log_group["physical_id"],
                        "type": "LogGroup",
                        "error": str(e),
                    }
                )

        # Phase 3: Verify CloudFront distributions are deleted before S3 buckets
        if retained["s3_buckets"]:
            logger.info("Verifying CloudFront distributions are deleted...")
            try:
                self._verify_cloudfront_distributions_deleted(stack_name)
                logger.info("CloudFront distributions verified as deleted")
            except Exception as e:
                error_msg = str(e)
                logger.error(f"CloudFront verification failed: {error_msg}")
                results["errors"].append(
                    {
                        "resource": "CloudFront Distribution",
                        "type": "CloudFront",
                        "error": error_msg,
                    }
                )
                # Do not proceed with S3 deletion if CloudFront still exists
                logger.warning(
                    "Skipping S3 bucket deletion to prevent orphaned CloudFront distributions"
                )
                return results

        # Phase 4: S3 Buckets (LoggingBucket last)
        if retained["s3_buckets"]:
            # Separate LoggingBucket from others
            logging_bucket = None
            regular_buckets = []

            for bucket in retained["s3_buckets"]:
                if "logging" in bucket["logical_id"].lower():
                    logging_bucket = bucket
                else:
                    regular_buckets.append(bucket)

            # Delete regular buckets first, then logging bucket
            all_buckets = regular_buckets + ([logging_bucket] if logging_bucket else [])

            for bucket in all_buckets:
                try:
                    self._empty_and_delete_bucket(bucket["physical_id"])
                    results["buckets_deleted"].append(bucket["physical_id"])
                    logger.info(f"Deleted S3 bucket: {bucket['physical_id']}")
                except Exception as e:
                    results["errors"].append(
                        {
                            "resource": bucket["physical_id"],
                            "type": "S3Bucket",
                            "error": str(e),
                        }
                    )

        return results

    def _delete_dynamodb_table(self, table_name: str) -> None:
        """
        Delete a DynamoDB table

        Args:
            table_name: Table name to delete
        """
        dynamodb = boto3.client("dynamodb", region_name=self.region)

        try:
            # Disable point-in-time recovery if enabled
            try:
                dynamodb.update_continuous_backups(
                    TableName=table_name,
                    PointInTimeRecoverySpecification={
                        "PointInTimeRecoveryEnabled": False
                    },
                )
            except Exception:
                # May not have PITR enabled, continue
                pass

            # Delete the table
            dynamodb.delete_table(TableName=table_name)
            logger.info(f"Deleted DynamoDB table: {table_name}")

        except Exception as e:
            logger.error(f"Error deleting DynamoDB table {table_name}: {e}")
            raise

    def _delete_log_group(self, log_group_name: str) -> None:
        """
        Delete a CloudWatch Log Group

        Args:
            log_group_name: Log group name to delete
        """
        logs = boto3.client("logs", region_name=self.region)

        try:
            logs.delete_log_group(logGroupName=log_group_name)
            logger.info(f"Deleted log group: {log_group_name}")

        except Exception as e:
            logger.error(f"Error deleting log group {log_group_name}: {e}")
            raise

    def _empty_and_delete_bucket(self, bucket_name: str) -> None:
        """
        Empty and delete an S3 bucket

        Args:
            bucket_name: Bucket name to delete
        """
        s3 = boto3.resource("s3", region_name=self.region)

        try:
            bucket = s3.Bucket(bucket_name)  # type: ignore

            # Delete all objects and versions
            bucket.object_versions.all().delete()

            # Delete the bucket
            bucket.delete()
            logger.info(f"Deleted S3 bucket: {bucket_name}")

        except Exception as e:
            logger.error(f"Error deleting bucket {bucket_name}: {e}")
            raise

    def _cleanup_additional_resources(self, stack_identifier: str) -> None:
        """Clean up additional resources not tracked by CloudFormation"""
        import json
        import os

        # Extract stack name from identifier
        stack_name = stack_identifier
        if stack_identifier.startswith("arn:"):
            stack_name = stack_identifier.split("/")[1]

        logger.info(f"Cleaning up additional resources for: {stack_name}")

        # Set AWS retry configuration to handle throttling
        os.environ["AWS_MAX_ATTEMPTS"] = "10"
        os.environ["AWS_RETRY_MODE"] = "adaptive"

        try:
            # Clean up additional log groups that might not be caught by standard cleanup
            logs_client = boto3.client("logs", region_name=self.region)
            try:
                response = logs_client.describe_log_groups()
                for log_group in response.get("logGroups", []):
                    log_group_name = log_group.get("logGroupName", "")
                    # Only match exact patterns for this stack
                    if log_group_name.startswith(
                        f"/{stack_name}-"
                    ) or log_group_name.startswith(
                        f"/aws-glue/crawlers-role/{stack_name}-"
                    ):
                        logs_client.delete_log_group(logGroupName=log_group_name)
                        logger.info(f"Deleted additional log group: {log_group_name}")
            except Exception as e:
                logger.warning(f"Failed to clean up additional log groups: {e}")

            # Clean up AppSync log groups
            try:
                appsync_client = boto3.client("appsync", region_name=self.region)
                response = appsync_client.list_graphql_apis()
                for api in response.get("graphqlApis", []):
                    api_name = api.get("name", "")
                    # Only match exact stack API patterns
                    if api_name == f"{stack_name}-api" or (
                        api_name.startswith(f"{stack_name}-p")
                        and api_name.endswith("-api")
                    ):
                        api_id = api.get("apiId")
                        if api_id:
                            appsync_log_group = f"/aws/appsync/apis/{api_id}"
                            try:
                                logs_client.delete_log_group(
                                    logGroupName=appsync_log_group
                                )
                                logger.info(
                                    f"Deleted AppSync log group: {appsync_log_group}"
                                )
                            except logs_client.exceptions.ResourceNotFoundException:
                                pass
            except Exception as e:
                logger.warning(f"Failed to clean up AppSync log groups: {e}")

            # Check if permissions boundary was created as part of stack
            iam_client = boto3.client("iam", region_name=self.region)
            boundary_name = f"{stack_name}-PermissionsBoundary"
            try:
                sts_client = boto3.client("sts", region_name=self.region)
                identity = sts_client.get_caller_identity()
                account_id = identity["Account"]
                # Partition-aware: a hardcoded arn:aws: never resolves in
                # GovCloud/China, so teardown silently failed to delete the
                # permissions boundary there and leaked the policy.
                partition = (
                    (identity.get("Arn") or "arn:aws:").split(":")[1] or "aws"
                )  # arn-partition-ok: fallback used only to PARSE the partition out
                iam_client.delete_policy(
                    PolicyArn=f"arn:{partition}:iam::{account_id}:policy/{boundary_name}"
                )
                logger.info(f"Deleted permissions boundary: {boundary_name}")
            except iam_client.exceptions.NoSuchEntityException:
                pass

            # Clean up CloudWatch Logs Resource Policy entries
            try:
                response = logs_client.describe_resource_policies()
                for policy in response.get("resourcePolicies", []):
                    if policy.get("policyName") == "AWSLogDeliveryWrite20150319":
                        policy_doc = json.loads(policy.get("policyDocument", "{}"))
                        original_count = len(policy_doc.get("Statement", []))

                        policy_doc["Statement"] = [
                            stmt
                            for stmt in policy_doc.get("Statement", [])
                            if f"/aws/vendedlogs/states/{stack_name}-"
                            not in stmt.get("Resource", "")
                        ]

                        new_count = len(policy_doc.get("Statement", []))
                        if new_count < original_count:
                            logs_client.put_resource_policy(
                                policyName="AWSLogDeliveryWrite20150319",
                                policyDocument=json.dumps(policy_doc),
                            )
                            logger.info(
                                f"Removed {original_count - new_count} CloudWatch Logs policy entries"
                            )
            except Exception as e:
                logger.warning(f"Failed to clean up CloudWatch Logs policy: {e}")

            # Clean up stack-specific CloudWatch Logs Resource Policies
            try:
                response = logs_client.describe_resource_policies()
                for policy in response.get("resourcePolicies", []):
                    policy_name = policy.get("policyName", "")
                    if policy_name.startswith(f"{stack_name}-"):
                        logs_client.delete_resource_policy(policyName=policy_name)
                        logger.info(f"Deleted resource policy: {policy_name}")
            except Exception as e:
                logger.warning(
                    f"Failed to delete stack-specific resource policies: {e}"
                )

            # Clean up CloudFront Distributions
            try:
                cloudfront_client = boto3.client("cloudfront")
                response = cloudfront_client.list_distributions()

                # Phase 1: Delete previously disabled distributions from any stack
                for distribution in response.get("DistributionList", {}).get(
                    "Items", []
                ):
                    comment = distribution.get("Comment", "")
                    if comment.startswith("Web app cloudfront distribution "):
                        distribution_id = distribution["Id"]

                        # Check if it's disabled and ready for deletion
                        if distribution.get(
                            "Status"
                        ) == "Deployed" and not distribution.get("Enabled", True):
                            config_response = cloudfront_client.get_distribution(
                                Id=distribution_id
                            )
                            etag = config_response["ETag"]

                            logger.info(
                                f"Deleting previously disabled distribution: {distribution_id}"
                            )
                            cloudfront_client.delete_distribution(
                                Id=distribution_id, IfMatch=etag
                            )

                # Phase 2: Disable current stack's distribution for future cleanup
                for distribution in response.get("DistributionList", {}).get(
                    "Items", []
                ):
                    comment = distribution.get("Comment", "")
                    if comment == f"Web app cloudfront distribution {stack_name}":
                        distribution_id = distribution["Id"]
                        logger.info(f"Found CloudFront distribution: {distribution_id}")

                        # Get current config with ETag
                        config_response = cloudfront_client.get_distribution(
                            Id=distribution_id
                        )
                        etag = config_response["ETag"]
                        config = config_response["Distribution"]["DistributionConfig"]

                        if config.get("Enabled", False):
                            logger.info(
                                f"Disabling distribution for future cleanup: {distribution_id}"
                            )
                            config["Enabled"] = False
                            cloudfront_client.update_distribution(
                                Id=distribution_id,
                                DistributionConfig=config,
                                IfMatch=etag,
                            )
                        else:
                            # Already disabled, delete immediately
                            logger.info(
                                f"Deleting already disabled distribution: {distribution_id}"
                            )
                            cloudfront_client.delete_distribution(
                                Id=distribution_id, IfMatch=etag
                            )

            except Exception as e:
                logger.warning(f"Failed to cleanup CloudFront distributions: {e}")

            # Clean up CloudFront Response Headers Policies
            try:
                cloudfront_client = boto3.client("cloudfront")
                response = cloudfront_client.list_response_headers_policies()

                # Helper function to check stack state
                def get_stack_state(check_stack_name: str) -> str:
                    """Get stack state: 'ACTIVE', 'MISSING', or 'INCONSISTENT'"""
                    try:
                        cf_response = self.cfn.describe_stacks(
                            StackName=check_stack_name
                        )
                        stacks = cf_response.get("Stacks", [])

                        if not stacks:
                            return "MISSING"
                        else:
                            stack_status = stacks[0].get("StackStatus", "")
                            if stack_status in [
                                "CREATE_COMPLETE",
                                "UPDATE_COMPLETE",
                                "UPDATE_ROLLBACK_COMPLETE",
                            ]:
                                return "ACTIVE"
                            elif stack_status in [
                                "DELETE_COMPLETE",
                                "DELETE_FAILED",
                                "CREATE_FAILED",
                                "ROLLBACK_COMPLETE",
                                "ROLLBACK_FAILED",
                                "UPDATE_ROLLBACK_FAILED",
                            ]:
                                return "INCONSISTENT"
                            else:
                                # In progress states - consider active for safety
                                return "ACTIVE"

                    except self.cfn.exceptions.ClientError as e:
                        if "does not exist" in str(e):
                            return "MISSING"
                        else:
                            # Unknown error - be conservative
                            return "ACTIVE"
                    except Exception:
                        # Unknown error - be conservative
                        return "ACTIVE"

                def is_resource_orphaned(check_stack_name: str) -> bool:
                    """Check if resource is orphaned based on stack state"""
                    if not check_stack_name:
                        return False  # Can't determine - be safe

                    state = get_stack_state(check_stack_name)
                    return state in ["MISSING", "INCONSISTENT"]

                # Delete policies from orphaned stacks only
                for policy in response.get("ResponseHeadersPolicyList", {}).get(
                    "Items", []
                ):
                    if policy["Type"] == "custom":
                        policy_name = policy["ResponseHeadersPolicy"][
                            "ResponseHeadersPolicyConfig"
                        ]["Name"]
                        policy_id = policy["ResponseHeadersPolicy"]["Id"]

                        # Check if it's an IDP policy
                        if policy_name.endswith("-security-headers-policy"):
                            # Extract stack name from policy name
                            policy_stack_name = policy_name.replace(
                                "-security-headers-policy", ""
                            )

                            # Skip current stack's policy (can't delete while distribution exists)
                            if policy_stack_name == stack_name:
                                logger.info(
                                    f"Skipping current stack's policy: {policy_name}"
                                )
                                continue

                            # Only delete if from orphaned stack
                            if is_resource_orphaned(policy_stack_name):
                                try:
                                    cloudfront_client.delete_response_headers_policy(
                                        Id=policy_id
                                    )
                                    logger.info(
                                        f"Deleted orphaned CloudFront Response Headers Policy: {policy_name} (stack: {policy_stack_name})"
                                    )
                                except Exception as policy_error:
                                    logger.warning(
                                        f"Failed to delete CloudFront policy {policy_name}: {policy_error}"
                                    )
                            else:
                                logger.info(
                                    f"Skipping active stack's policy: {policy_name} (stack: {policy_stack_name})"
                                )

            except Exception as e:
                logger.warning(f"Failed to cleanup CloudFront policies: {e}")

            # Clean up IAM custom policies
            try:
                response = iam_client.list_policies(Scope="Local")
                for policy in response.get("Policies", []):
                    policy_name = policy.get("PolicyName", "")
                    # Only match policies that start with stack name
                    if policy_name.startswith(f"{stack_name}-"):
                        policy_arn = policy.get("Arn")
                        try:
                            iam_client.delete_policy(PolicyArn=policy_arn)
                            logger.info(f"Deleted IAM custom policy: {policy_name}")
                        except Exception as policy_error:
                            logger.warning(
                                f"Failed to delete IAM policy {policy_name}: {policy_error}"
                            )
            except Exception as e:
                logger.warning(f"Failed to cleanup IAM custom policies: {e}")

            # Clean up BDA projects and blueprints associated with this stack
            try:
                bda_client = boto3.client(
                    "bedrock-data-automation", region_name=self.region
                )

                # Delete BDA projects first (blueprints are referenced by projects)
                try:
                    for p in bda_client.list_data_automation_projects().get(
                        "projects", []
                    ):
                        if p.get("projectName", "").startswith(f"{stack_name}-"):
                            try:
                                bda_client.delete_data_automation_project(
                                    projectArn=p["projectArn"]
                                )
                                logger.info(f"Deleted BDA project: {p['projectName']}")
                            except Exception as proj_error:
                                logger.warning(
                                    f"Failed to delete BDA project {p['projectName']}: {proj_error}"
                                )
                except Exception as e:
                    logger.warning(f"Failed to cleanup BDA projects: {e}")

                # Delete blueprints (versions first, then base)
                paginator = bda_client.get_paginator("list_blueprints")
                deleted_count = 0
                for page in paginator.paginate(blueprintStageFilter="LIVE"):
                    for blueprint in page.get("blueprints", []):
                        bp_name = blueprint.get("blueprintName", "")
                        bp_arn = blueprint.get("blueprintArn", "")
                        # Skip AWS-managed blueprints
                        if "aws:blueprint" in bp_arn:
                            continue
                        if bp_name.startswith(f"{stack_name}-"):
                            try:
                                try:
                                    bda_client.delete_blueprint(
                                        blueprintArn=bp_arn, blueprintVersion="1"
                                    )
                                except Exception:
                                    pass
                                bda_client.delete_blueprint(blueprintArn=bp_arn)
                                deleted_count += 1
                                logger.info(f"Deleted BDA blueprint: {bp_name}")
                            except Exception as bp_error:
                                logger.warning(
                                    f"Failed to delete BDA blueprint {bp_name}: {bp_error}"
                                )
                if deleted_count > 0:
                    logger.info(
                        f"Deleted {deleted_count} BDA blueprints for stack {stack_name}"
                    )
            except Exception as e:
                logger.warning(f"Failed to cleanup BDA blueprints: {e}")

        except Exception as e:
            logger.warning(f"Additional resource cleanup failed: {e}")


def is_local_file_path(path: str) -> bool:
    """
    Determine if path is a local file vs S3 URI

    Args:
        path: Path to check

    Returns:
        True if local file path, False if S3 URI
    """
    return not path.startswith("s3://")


def validate_s3_uri(uri: str) -> bool:
    """
    Validate S3 URI format

    Args:
        uri: S3 URI to validate

    Returns:
        True if valid S3 URI format
    """
    if not uri.startswith("s3://"):
        return False

    # Remove s3:// prefix and check for bucket/key structure
    path = uri[5:]
    parts = path.split("/", 1)

    # Must have bucket and key
    return len(parts) == 2 and bool(parts[0] and parts[1])


def get_or_create_config_bucket(region: str) -> str:
    """
    Get or create temporary S3 bucket for CLI config uploads

    Args:
        region: AWS region

    Returns:
        Bucket name
    """
    s3 = boto3.client("s3", region_name=region)
    sts = boto3.client("sts")

    try:
        account_id = sts.get_caller_identity()["Account"]
    except Exception as e:
        raise Exception(f"Failed to get AWS account ID: {e}")

    # Normalize region name for bucket (replace hyphens with nothing for cleaner name)
    region_normalized = region.replace("-", "")

    # Check for existing bucket with pattern
    bucket_prefix = f"idp-cli-config-{account_id}-{region_normalized}-"

    try:
        response = s3.list_buckets()
        for bucket in response.get("Buckets", []):
            bucket_name = bucket["Name"]
            if bucket_name.startswith(bucket_prefix):
                logger.info(f"Using existing config bucket: {bucket_name}")
                # Harden a bucket left behind by an older idp-cli that created
                # it without the policy. This function is the only code that
                # touches these buckets, so without this the ones most likely
                # to be unhardened would stay that way indefinitely. Additive,
                # and non-fatal — reusing the bucket must not break on it.
                apply_enforce_ssl_only(s3, bucket_name, region, raise_on_error=False)
                return bucket_name
    except Exception as e:
        logger.warning(f"Error listing buckets: {e}")

    # Create new bucket with random suffix
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))  # nosec B311 - bucket name suffix
    bucket_name = f"{bucket_prefix}{suffix}"

    logger.info(f"Creating new config bucket: {bucket_name}")

    try:
        # Create bucket
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )

        # Enable versioning
        s3.put_bucket_versioning(
            Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"}
        )

        # Enable encryption
        s3.put_bucket_encryption(
            Bucket=bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
        )

        # Set lifecycle policy (30-day expiration)
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "DeleteOldConfigs",
                        "Status": "Enabled",
                        "Prefix": "idp-cli/custom-configurations/",
                        "Expiration": {"Days": 30},
                    }
                ]
            },
        )

        # Deny any non-TLS request (same EnforceSSLOnly statement the
        # CloudFormation-managed buckets carry).
        apply_enforce_ssl_only(s3, bucket_name, region)

        # Add tags
        s3.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={
                "TagSet": [
                    {"Key": "CreatedBy", "Value": "idp-cli"},
                    {"Key": "Purpose", "Value": "config-staging"},
                ]
            },
        )

        logger.info(f"Successfully created config bucket: {bucket_name}")
        return bucket_name

    except Exception as e:
        raise Exception(f"Failed to create config bucket: {e}")


def upload_local_config(
    file_path: str,
    region: str,
    stack_name: Optional[str] = None,
    pattern: Optional[str] = None,
    merge_with_defaults: bool = True,
) -> str:
    """
    Upload local config file to temporary S3 bucket.

    If merge_with_defaults is True (default), the user's minimal config is merged
    with system defaults before uploading. This allows users to specify only the
    values they want to customize.

    Args:
        file_path: Path to local config file
        region: AWS region
        stack_name: CloudFormation stack name (unused, kept for compatibility)
        pattern: Pattern to use for defaults (pattern-1, pattern-2).
                 If not provided, attempts to auto-detect from config file.
        merge_with_defaults: If True, merge user config with system defaults

    Returns:
        S3 URI of uploaded file
    """
    import yaml

    # Validate file exists
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Config file not found: {file_path}")

    logger.info(f"Uploading local config file: {file_path}")

    # Load user config
    with open(file_path, "r", encoding="utf-8") as f:
        user_config = yaml.safe_load(f) or {}

    # Merge with defaults if requested
    if merge_with_defaults:
        try:
            from idp_common.config.merge_utils import merge_config_with_defaults

            # Auto-detect pattern if not provided
            if not pattern:
                # Try to detect from config content
                classification_method = user_config.get("classification", {}).get(
                    "classificationMethod", ""
                )
                if classification_method == "bda":
                    pattern = "pattern-1"
                else:
                    # Default to pattern-2 (most common)
                    pattern = "pattern-2"
                logger.info(f"Auto-detected pattern: {pattern}")

            logger.info(f"Merging user config with {pattern} system defaults...")
            merged_config = merge_config_with_defaults(
                user_config, pattern=pattern, validate=False
            )

            # Log merge summary
            user_keys = set(user_config.keys())
            logger.info(f"User config sections: {user_keys}")
            logger.info(f"Merged config has {len(merged_config)} top-level sections")

            # Use merged config for upload
            config_to_upload = merged_config

        except ImportError:
            logger.warning(
                "idp_common not available - uploading config without merging"
            )
            config_to_upload = user_config
        except FileNotFoundError as e:
            logger.warning(
                f"System defaults not found - uploading config without merging: {e}"
            )
            config_to_upload = user_config
        except Exception as e:
            logger.warning(f"Failed to merge with defaults: {e}")
            config_to_upload = user_config
    else:
        config_to_upload = user_config

    # Convert back to YAML
    config_yaml = yaml.dump(
        config_to_upload,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )

    # Always use temp bucket
    bucket_name = get_or_create_config_bucket(region)

    # Generate timestamped key - use underscores instead of hyphens in filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_name = os.path.basename(file_path)
    # Sanitize filename - replace hyphens with underscores for maximum compatibility
    safe_name = original_name.replace("-", "_")
    s3_key = f"idp-cli/custom-configurations/config_{timestamp}_{safe_name}"

    # Upload file
    s3 = boto3.client("s3", region_name=region)
    try:
        s3.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=config_yaml.encode("utf-8"),
            ServerSideEncryption="AES256",
        )

        # Return S3 URI
        s3_uri = f"s3://{bucket_name}/{s3_key}"
        logger.info(f"Uploaded config file to: {s3_uri}")
        return s3_uri

    except Exception as e:
        raise Exception(f"Failed to upload config file: {e}")


def build_parameters(
    admin_email: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    log_level: Optional[str] = None,
    enable_hitl: Optional[str] = None,
    custom_config: Optional[str] = None,
    additional_params: Optional[Dict[str, str]] = None,
    region: Optional[str] = None,
    stack_name: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build CloudFormation parameters dictionary

    Only includes parameters that are explicitly provided. For stack updates,
    CloudFormation will automatically use previous values for parameters not included.

    If custom_config is a local file path, it will be uploaded to S3:
    - For existing stacks: Uses the stack's ConfigurationBucket
    - For new stacks: Creates a temporary bucket

    Args:
        admin_email: Admin user email - optional for updates
        max_concurrent: Maximum concurrent workflows - optional
        log_level: Logging level - optional
        enable_hitl: Enable HITL (true/false) - optional
        custom_config: Custom configuration (local file path or S3 URI) - optional
        additional_params: Additional parameters as dict - optional
        region: AWS region (auto-detected if not provided)
        stack_name: Stack name (helps determine upload bucket for updates)

    Returns:
        Dictionary of parameter key-value pairs (only includes explicitly provided values)
    """
    parameters = {}

    # Only add parameters if explicitly provided
    if admin_email is not None:
        parameters["AdminEmail"] = admin_email

    if max_concurrent is not None:
        parameters["MaxConcurrentWorkflows"] = str(max_concurrent)

    if log_level is not None:
        parameters["LogLevel"] = log_level

    if enable_hitl is not None:
        parameters["EnableHITL"] = enable_hitl

    # Handle custom config - support both local files and S3 URIs
    if custom_config:
        if is_local_file_path(custom_config):
            # Local file - need to upload it
            if not region:
                # Auto-detect region from boto3 session
                import boto3

                session = boto3.session.Session()  # type: ignore
                region = session.region_name
                if not region:
                    raise ValueError(
                        "Region could not be determined. Please specify --region or configure AWS_DEFAULT_REGION"
                    )

            logger.info(f"Detected local config file: {custom_config}")
            logger.info(f"Using region: {region}")

            # Upload to S3 bucket (stack's ConfigurationBucket if exists, else temp bucket)
            s3_uri = upload_local_config(custom_config, region, stack_name)
            parameters["CustomConfigPath"] = s3_uri

            logger.info(f"Using uploaded config: {s3_uri}")
        else:
            # Already an S3 URI - validate and use
            if not validate_s3_uri(custom_config):
                raise ValueError(
                    f"Invalid S3 URI format: {custom_config}. Expected format: s3://bucket/key"
                )

            parameters["CustomConfigPath"] = custom_config
            logger.info(f"Using S3 config: {custom_config}")

    # Add any additional parameters
    if additional_params:
        parameters.update(additional_params)

    return parameters
