# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stack operations for IDP SDK."""

import json
import logging
from typing import Dict, List, Optional

from idp_sdk.exceptions import IDPConfigurationError, IDPStackError
from idp_sdk.models import (
    BucketInfo,
    CancelUpdateResult,
    FailureAnalysis,
    FailureCause,
    OrphanedResourceCleanupResult,
    StackDeletionResult,
    StackDeploymentResult,
    StackMonitorResult,
    StackOperationInProgress,
    StackResources,
    StackStableStateResult,
)

logger = logging.getLogger(__name__)


class StackOperation:
    """Stack deployment and management operations."""

    def __init__(self, client):
        self._client = client

    def deploy(
        self,
        stack_name: Optional[str] = None,
        admin_email: Optional[str] = None,
        template_url: Optional[str] = None,
        template_path: Optional[str] = None,
        from_code: Optional[str] = None,
        custom_config: Optional[str] = None,
        max_concurrent: Optional[int] = None,
        log_level: Optional[str] = None,
        enable_hitl: Optional[bool] = None,
        parameters: Optional[Dict[str, str]] = None,
        wait: bool = True,
        no_rollback: bool = False,
        role_arn: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> StackDeploymentResult:
        """
        Deploy or update an IDP CloudFormation stack.

        Args:
            stack_name: CloudFormation stack name (uses default if not provided)
            admin_email: Admin user email - required for new stacks
            template_url: URL to CloudFormation template in S3
            template_path: Local path to CloudFormation template file
            from_code: Path to project root for building from source
            custom_config: Path to local config file or S3 URI
            max_concurrent: Maximum concurrent workflows
            log_level: Logging level (DEBUG, INFO, WARN, ERROR)
            enable_hitl: Accepted only as false/None. HITL stopped being a
                CloudFormation parameter in v0.4.11 and is a configuration
                setting; a truthy value raises rather than being sent to
                CloudFormation, which rejected it as an undeclared parameter.
            parameters: Additional parameters as dict
            wait: Wait for operation to complete (default: True)
            no_rollback: Disable rollback on failure
            role_arn: CloudFormation service role ARN
            tags: Stack-level tags (dict) propagated to all taggable resources
                and nested stacks. On update, omitting tags preserves existing
                tags; providing them replaces the stack's tag set.
            **kwargs: Additional parameters

        Returns:
            StackDeploymentResult with status and outputs

        Raises:
            IDPConfigurationError: If required parameters missing
            IDPStackError: If deployment fails
        """
        from idp_sdk._core.stack import StackDeployer, build_parameters

        name = self._client._require_stack(stack_name)
        # template_path passed directly takes precedence over from_code build output
        explicit_template_path = template_path

        if enable_hitl:
            # Refuse here, with the remedy, rather than letting CloudFormation
            # reject "Parameters: [EnableHITL] do not exist in the template" after
            # the caller has already waited for a template build and upload.
            raise ValueError(
                "enable_hitl is no longer a CloudFormation parameter. HITL became "
                "a configuration setting in v0.4.11: enable it in the Web UI under "
                "Configuration → Assessment & HITL Configuration, or in the config "
                "YAML passed as custom_config. Passing it here deployed nothing — "
                "CloudFormation rejects a parameter the template does not declare."
            )

        additional_params = parameters or {}
        cfn_parameters = build_parameters(
            admin_email=admin_email,
            max_concurrent=max_concurrent,
            log_level=log_level,
            custom_config=custom_config,
            additional_params=additional_params,
            region=self._client._region,
            stack_name=name,
        )

        deployer = StackDeployer(region=self._client._region)

        try:
            built_template_path = None
            if explicit_template_path:
                # Caller provided a pre-built template path directly
                built_template_path = explicit_template_path
            elif from_code:
                import os
                import subprocess
                import sys

                import boto3

                publish_script = os.path.join(from_code, "publish.py")
                if not os.path.isfile(publish_script):
                    raise IDPConfigurationError(f"publish.py not found in {from_code}")

                sts = boto3.client("sts", region_name=self._client._region)
                account_id = sts.get_caller_identity()["Account"]
                cfn_bucket_basename = f"idp-accelerator-artifacts-{account_id}"
                cfn_prefix = "idp-sdk"

                cmd = [
                    sys.executable,
                    publish_script,
                    cfn_bucket_basename,
                    cfn_prefix,
                    self._client._region or "us-west-2",
                ]
                result = subprocess.run(
                    cmd, cwd=from_code, capture_output=True, text=True
                )

                if result.returncode != 0:
                    raise IDPStackError(f"Build failed: {result.stderr}")

                built_template_path = os.path.join(
                    from_code, ".aws-sam", "idp-main.yaml"
                )

            if built_template_path:
                result = deployer.deploy_stack(
                    stack_name=name,
                    template_path=built_template_path,
                    parameters=cfn_parameters,
                    wait=wait,
                    no_rollback=no_rollback,
                    role_arn=role_arn,
                    tags=tags,
                )
            else:
                result = deployer.deploy_stack(
                    stack_name=name,
                    template_url=template_url,
                    parameters=cfn_parameters,
                    wait=wait,
                    no_rollback=no_rollback,
                    role_arn=role_arn,
                    tags=tags,
                )

            return StackDeploymentResult(
                success=result.get("success", False),
                operation=result.get("operation", "UNKNOWN"),
                status=result.get("status", "UNKNOWN"),
                stack_name=name,
                stack_id=result.get("stack_id"),
                outputs=result.get("outputs", {}),
                error=result.get("error"),
                deploy_start_time=result.get("deploy_start_time"),
            )

        except Exception as e:
            raise IDPStackError(f"Deployment failed: {e}") from e

    def delete(
        self,
        stack_name: Optional[str] = None,
        empty_buckets: bool = False,
        force_delete_all: bool = False,
        wait: bool = True,
        **kwargs,
    ) -> StackDeletionResult:
        """
        Delete an IDP CloudFormation stack.

        Args:
            stack_name: CloudFormation stack name (uses default if not provided)
            empty_buckets: Empty S3 buckets before deletion
            force_delete_all: Force delete ALL remaining resources after CloudFormation
                deletion completes. When True, wait is automatically set to True so
                that retained resources can be identified after deletion.
            wait: Wait for deletion to complete
            **kwargs: Additional parameters

        Returns:
            StackDeletionResult with status
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)

        # force_delete_all requires waiting for CloudFormation deletion to finish
        # before we can identify and clean up retained resources.
        effective_wait = wait or force_delete_all

        try:
            result = deployer.delete_stack(
                stack_name=name,
                empty_buckets=empty_buckets,
                wait=effective_wait,
            )

            cleanup_result = None
            if force_delete_all:
                stack_identifier = result.get("stack_id", name)
                cleanup_result = deployer.cleanup_retained_resources(stack_identifier)

            # When deletion was only initiated (no wait), success is not set in the
            # underlying result dict.  Treat INITIATED as a successful initiation so
            # callers can distinguish "failed to start" from "started but not waited".
            status = result.get("status", "UNKNOWN")
            success = result.get("success", status == "INITIATED")

            return StackDeletionResult(
                success=success,
                status=status,
                stack_name=name,
                stack_id=result.get("stack_id"),
                error=result.get("error"),
                cleanup_result=cleanup_result,
            )

        except Exception as e:
            raise IDPStackError(f"Deletion failed: {e}") from e

    def deploy_terraform(
        self,
        stack_name: Optional[str] = None,
        admin_email: Optional[str] = None,
        parameters: Optional[Dict[str, str]] = None,
        *,
        module_url: Optional[str] = None,
        pointer_url: Optional[str] = None,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        assume_role_arn: Optional[str] = None,
    ) -> StackDeploymentResult:
        """Deploy or update the accelerator with Terraform (or OpenTofu).

        Writes a root configuration into ``terraform_dir`` (default
        ``./idp-terraform/<stack_name>``) — ``main.tf`` calling the published
        module as ``module "idp"`` with the aws and awscc providers in the
        client's region, and ``terraform.tfvars`` — then runs ``init`` and
        ``apply -auto-approve``, streaming their output to stderr. State is
        local in that directory unless a backend is configured in another file
        there. Always waits.

        Args:
            stack_name: Deployment name (the label stack's name, at most 25
                characters).
            admin_email: Admin user email; required on the first deploy.
            parameters: CloudFormation parameter names and values, mapped to
                module variables through the module's ``idp-terraform.json``.
                Unknown names are refused.
            module_url: Module zip URL. Without it, ``pointer_url`` is read.
            pointer_url: URL of an ``idp-terraform-latest.json`` pointer.
            terraform_dir: Root configuration directory.
            terraform_bin: ``terraform``, ``tofu`` or a path to either.
            assume_role_arn: An IAM role both providers assume (an
                ``assume_role`` block in each provider block of ``main.tf``), so
                the resources are created as that role rather than as the
                caller. The caller needs ``sts:AssumeRole`` on it.

        Returns:
            StackDeploymentResult with operation ``APPLY`` and the label stack's
            outputs.

        Raises:
            IDPConfigurationError: Binary missing, bad parameters, a foreign
                ``main.tf``, or a CloudFormation stack of that name.
            IDPStackError: ``init`` or ``apply`` failed.
        """
        from pathlib import Path

        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core.stack import StackDeployer
        from idp_sdk._core.terraform_stack import is_terraform_stack

        name = self._client._require_stack(stack_name)
        region = self._client._region
        if not region:
            raise IDPConfigurationError("a region is required for a Terraform deploy")
        directory = Path(terraform_dir or Path("idp-terraform") / name).resolve()
        runner = tf.TerraformRunner(terraform_bin, directory)

        deployer = StackDeployer(region=region)
        stack = self._describe_stack(deployer, name)
        if stack is not None and not is_terraform_stack(stack.get("Description")):
            raise IDPConfigurationError(
                f"{name} is a CloudFormation stack. A Terraform deployment creates "
                "a label stack of its own name, so it needs a name no stack uses; "
                "deploy it with `idp-cli deploy` (without --terraform) instead."
            )

        sha = None
        if not module_url:
            if not pointer_url:
                raise IDPConfigurationError(
                    "no Terraform module URL: pass module_url or pointer_url"
                )
            pointer = json.loads(tf.fetch_url(pointer_url))
            module_url, sha = tf.resolve_pointer(pointer)
        manifest = tf.load_module_manifest(module_url, sha)
        tf.write_root_config(
            directory,
            region=region,
            module_url=module_url,
            stack_name=name,
            admin_email=admin_email,
            parameters=parameters or {},
            manifest=manifest,
            assume_role_arn=assume_role_arn,
        )
        operation = "UPDATE" if (directory / "terraform.tfstate").exists() else "CREATE"
        runner.init()
        runner.apply()

        outputs: Dict[str, str] = {}
        stack = self._describe_stack(deployer, name)
        if stack is not None:
            outputs = deployer._get_stack_outputs(stack)
        return StackDeploymentResult(
            success=True,
            operation=operation,
            status="APPLY_COMPLETE",
            stack_name=name,
            stack_id=(stack or {}).get("StackId"),
            outputs=outputs,
        )

    def delete_terraform(
        self,
        stack_name: Optional[str] = None,
        *,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        assume_role_arn: Optional[str] = None,
    ) -> StackDeletionResult:
        """Destroy a Terraform deployment, keeping what CloudFormation retains.

        In ``terraform_dir`` (default ``./idp-terraform/<stack_name>``): runs
        ``init``; empties the deployment's S3 buckets that will be destroyed,
        with the helper the CloudFormation delete uses; removes every address in
        the module's ``retainedAddresses`` (as ``module.idp.<address>``) from
        the state, skipping any not in it; then runs ``destroy -auto-approve``.

        With ``assume_role_arn``, the provider blocks of the idp-cli ``main.tf``
        in the directory are rewritten to assume that role first; without it
        they are left as the deploy wrote them.

        Returns:
            StackDeletionResult whose ``retained_resources`` lists what was left,
            in the shape of the CloudFormation path's retained-resource report.

        Raises:
            IDPConfigurationError: Binary or directory missing.
            IDPStackError: A Terraform command failed.
        """
        from pathlib import Path

        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        directory = Path(terraform_dir or Path("idp-terraform") / name).resolve()
        if not directory.is_dir() or not any(directory.glob("*.tf")):
            raise IDPConfigurationError(
                f"no Terraform configuration in {directory}; pass the directory "
                "the deployment was applied from with --terraform-dir"
            )
        runner = tf.TerraformRunner(terraform_bin, directory)
        tfvars = directory / "terraform.tfvars"
        if tfvars.is_file() and tf.ROOT_MARKER in tfvars.read_text():
            recorded = tf.parse_tfvars(tfvars.read_text()).get("stack_name")
            if recorded and recorded != name:
                raise IDPConfigurationError(
                    f"{directory} holds the deployment {recorded!r}, not {name!r}"
                )

        role = tf.check_assume_role_arn(assume_role_arn)
        if role:
            tf.set_assume_role(directory / "main.tf", role, self._client._region)
        runner.init()
        manifest = tf.read_manifest_from_dir(directory)
        state = runner.show_state()
        remove, absent = tf.retained_state_addresses(
            manifest.get("retainedAddresses") or [], runner.state_list()
        )
        for addr in absent:
            logger.info("Retained address not in state, skipped: %s", addr)

        buckets = tf.state_buckets(state, exclude=remove)
        if buckets:
            StackDeployer(region=self._client._region)._empty_buckets(buckets)
        retained = tf.retained_report(state, remove)
        runner.state_rm(remove)
        runner.destroy()
        return StackDeletionResult(
            success=True,
            status="DESTROY_COMPLETE",
            stack_name=name,
            retained_resources=retained,
        )

    @staticmethod
    def _describe_stack(deployer, name: str) -> Optional[dict]:
        try:
            stacks = deployer.cfn.describe_stacks(StackName=name).get("Stacks") or []
        except deployer.cfn.exceptions.ClientError as e:
            if "does not exist" in str(e):
                return None
            raise
        return stacks[0] if stacks else None

    def get_resources(
        self, stack_name: Optional[str] = None, **kwargs
    ) -> StackResources:
        """
        Get stack resources.

        Args:
            stack_name: CloudFormation stack name (uses default if not provided)
            **kwargs: Additional parameters

        Returns:
            StackResources with bucket names, ARNs, etc.
        """
        resources = self._client._get_stack_resources(stack_name)
        return StackResources(**resources)

    def exists(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """
        Check whether a CloudFormation stack exists.

        Args:
            stack_name: Stack name (uses default if not provided)

        Returns:
            True if the stack exists, False if it does not
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        return deployer._stack_exists(name)

    def get_status(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        Get the current CloudFormation status of a stack.

        Args:
            stack_name: Stack name (uses default if not provided)

        Returns:
            CloudFormation status string (e.g. 'UPDATE_IN_PROGRESS'),
            or None if the stack does not exist
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        return deployer._get_stack_status(name)

    def check_in_progress(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Optional[StackOperationInProgress]:
        """
        Check whether a CloudFormation stack has an operation currently in progress.

        Useful for detecting whether to attach to an existing operation
        (CREATE/UPDATE/DELETE) before starting a new one.

        Args:
            stack_name: Stack name (uses default if not provided)

        Returns:
            StackOperationInProgress if an operation is in progress, None otherwise.
            operation values: 'CREATE', 'UPDATE', 'DELETE'
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        result = deployer.get_stack_operation_in_progress(name)
        if result is None:
            return None
        return StackOperationInProgress(
            operation=result["operation"],
            status=result["status"],
        )

    def monitor(
        self,
        stack_name: Optional[str] = None,
        operation: str = "UPDATE",
        poll_interval_seconds: int = 10,
        timeout_seconds: int = 3600,
        **kwargs,
    ) -> StackMonitorResult:
        """
        Monitor a CloudFormation stack operation until it reaches a terminal state.

        Blocks until the operation (CREATE, UPDATE, or DELETE) completes or fails.
        Does not produce any output — callers are responsible for display.

        Args:
            stack_name: Stack name (uses default if not provided)
            operation: Operation type being monitored: 'CREATE', 'UPDATE', or 'DELETE'
            poll_interval_seconds: Seconds between CloudFormation API polls (default: 10)
            timeout_seconds: Maximum seconds to wait before returning a timeout result
                (default: 3600 = 1 hour)

        Returns:
            StackMonitorResult with final success/status/outputs/error
        """
        import time

        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)

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
            "DELETE": ["DELETE_COMPLETE", "DELETE_FAILED"],
        }
        success_statuses = {
            "CREATE": ["CREATE_COMPLETE"],
            "UPDATE": ["UPDATE_COMPLETE"],
            "DELETE": ["DELETE_COMPLETE"],
        }

        target_statuses = complete_statuses.get(operation, [])
        success_set = success_statuses.get(operation, [])

        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout_seconds:
                return StackMonitorResult(
                    success=False,
                    operation=operation,
                    status="TIMEOUT",
                    stack_name=name,
                    error=f"Timed out after {timeout_seconds}s waiting for {operation} to complete",
                )

            try:
                if operation == "DELETE":
                    try:
                        response = deployer.cfn.describe_stacks(StackName=name)
                        stacks = response.get("Stacks", [])
                    except deployer.cfn.exceptions.ClientError as e:
                        if "does not exist" in str(e):
                            return StackMonitorResult(
                                success=True,
                                operation=operation,
                                status="DELETE_COMPLETE",
                                stack_name=name,
                            )
                        raise
                else:
                    response = deployer.cfn.describe_stacks(StackName=name)
                    stacks = response.get("Stacks", [])

                if not stacks:
                    if operation == "DELETE":
                        return StackMonitorResult(
                            success=True,
                            operation=operation,
                            status="DELETE_COMPLETE",
                            stack_name=name,
                        )
                    raise IDPStackError(f"Stack {name} not found")

                stack = stacks[0]
                status = stack.get("StackStatus", "")

                if status in target_statuses:
                    is_success = status in success_set
                    outputs = (
                        deployer._get_stack_outputs(stack)
                        if is_success and operation != "DELETE"
                        else {}
                    )
                    error = None
                    if not is_success:
                        error = deployer._get_stack_failure_reason(name)
                    return StackMonitorResult(
                        success=is_success,
                        operation=operation,
                        status=status,
                        stack_name=name,
                        outputs=outputs,
                        error=error,
                    )

                time.sleep(poll_interval_seconds)

            except IDPStackError:
                raise
            except Exception as e:
                raise IDPStackError(f"Error monitoring stack {name}: {e}") from e

    def cancel_update(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> CancelUpdateResult:
        """
        Cancel an in-progress stack update.

        Only valid when a stack is in UPDATE_IN_PROGRESS status.
        After cancellation, the stack returns to UPDATE_ROLLBACK_COMPLETE.

        Args:
            stack_name: Stack name (uses default if not provided)

        Returns:
            CancelUpdateResult indicating success/failure
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        result = deployer.cancel_update_stack(name)
        return CancelUpdateResult(
            success=result.get("success", False),
            message=result.get("message"),
            error=result.get("error"),
        )

    def wait_for_stable_state(
        self,
        stack_name: Optional[str] = None,
        timeout_seconds: int = 1200,
        poll_interval_seconds: int = 10,
        **kwargs,
    ) -> StackStableStateResult:
        """
        Wait for a CloudFormation stack to reach a stable (non-transitional) state.

        Useful before triggering an operation on a stack that may be in a transitional
        state (e.g. UPDATE_IN_PROGRESS after cancelling an update).

        Args:
            stack_name: Stack name (uses default if not provided)
            timeout_seconds: Maximum seconds to wait (default: 1200 = 20 minutes)
            poll_interval_seconds: Seconds between polls (default: 10)

        Returns:
            StackStableStateResult with final status and success flag
        """
        import time

        from idp_sdk._core.stack import StackDeployer

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

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        start = time.time()

        while True:
            elapsed = time.time() - start
            if elapsed > timeout_seconds:
                return StackStableStateResult(
                    success=False,
                    status="TIMEOUT",
                    message=f"Timed out after {timeout_seconds}s",
                )

            try:
                status = deployer._get_stack_status(name)
                if status is None:
                    return StackStableStateResult(
                        success=True,
                        status="DELETE_COMPLETE",
                        message="Stack no longer exists",
                    )
                if status in stable_states:
                    return StackStableStateResult(
                        success=True,
                        status=status,
                        message=f"Stack reached stable state: {status}",
                    )
            except Exception as e:
                if "does not exist" in str(e):
                    return StackStableStateResult(
                        success=True,
                        status="DELETE_COMPLETE",
                        message="Stack no longer exists",
                    )
                raise IDPStackError(f"Error polling stack {name}: {e}") from e

            time.sleep(poll_interval_seconds)

    def get_failure_analysis(
        self,
        stack_name: Optional[str] = None,
        deploy_start_time=None,
        **kwargs,
    ) -> FailureAnalysis:
        """
        Analyze a CloudFormation deployment failure.

        Recursively collects failed events from the main stack and all nested stacks,
        identifies root causes (vs. cascade failures), and returns structured data.

        Args:
            stack_name: Stack name (uses default if not provided)
            deploy_start_time: UTC datetime when deployment was initiated.
                When provided, only events after this time are analyzed,
                preventing stale errors from previous deployments.

        Returns:
            FailureAnalysis with root_causes and all_failures lists
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        raw = deployer.get_deployment_failure_analysis(
            name, deploy_start_time=deploy_start_time
        )

        def _to_cause(d: dict) -> FailureCause:
            return FailureCause(
                resource=d.get("resource", "Unknown"),
                resource_type=d.get("resource_type", ""),
                reason=d.get("reason", "Unknown"),
                status=d.get("status", ""),
                physical_id=d.get("physical_id", ""),
                stack=d.get("stack", name),
                stack_path=d.get("stack_path", ""),
                is_cascade=d.get("is_cascade", False),
            )

        return FailureAnalysis(
            stack_name=raw.get("stack_name", name),
            root_causes=[_to_cause(c) for c in raw.get("root_causes", [])],
            all_failures=[_to_cause(f) for f in raw.get("all_failures", [])],
        )

    def cleanup_orphaned(
        self,
        dry_run: bool = False,
        auto_approve: bool = False,
        regions: Optional[List[str]] = None,
        profile: Optional[str] = None,
        **kwargs,
    ) -> OrphanedResourceCleanupResult:
        """
        Remove residual AWS resources left behind from deleted IDP stacks.

        Identifies orphaned CloudFront distributions, CloudWatch log groups,
        AppSync APIs, IAM policies, CloudFront response headers policies,
        CloudWatch Logs resource policies, S3 buckets, and DynamoDB tables
        that belong to IDP stacks in DELETE_COMPLETE state.

        Args:
            dry_run: Preview changes without making them (default: False)
            auto_approve: Auto-approve all deletions, skipping confirmations (default: False)
            regions: AWS regions to check for deleted IDP stacks
                     (default: ``cleanup_orphaned.IDP_REGIONS``)
            profile: AWS profile name to use (default: None, uses default credential chain)

        Returns:
            OrphanedResourceCleanupResult with per-resource-type results dict,
            has_errors flag, and has_disabled flag.
        """
        from idp_sdk._core.cleanup_orphaned import OrphanedResourceCleanup

        cleanup = OrphanedResourceCleanup(
            region=self._client._region or "us-west-2",
            profile=profile,
        )
        raw_results = cleanup.run_cleanup(
            dry_run=dry_run,
            auto_approve=auto_approve,
            regions=regions,
        )

        has_errors = any(r.get("errors") for r in raw_results.values())
        has_disabled = any(r.get("disabled") for r in raw_results.values())

        return OrphanedResourceCleanupResult(
            results=raw_results,
            has_errors=has_errors,
            has_disabled=has_disabled,
        )

    def get_bucket_info(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> List[BucketInfo]:
        """
        Get information about S3 buckets associated with a CloudFormation stack.

        Returns object counts and sizes for each bucket, useful for confirming
        whether buckets need to be emptied before stack deletion.

        Args:
            stack_name: Stack name (uses default if not provided)

        Returns:
            List of BucketInfo (one per S3 bucket in the stack)
        """
        from idp_sdk._core.stack import StackDeployer

        name = self._client._require_stack(stack_name)
        deployer = StackDeployer(region=self._client._region)
        raw_list = deployer.get_bucket_info(name)

        return [
            BucketInfo(
                logical_id=b.get("logical_id", ""),
                bucket_name=b.get("bucket_name", ""),
                object_count=b.get("object_count", 0),
                total_size=b.get("total_size", 0),
                size_display=b.get("size_display", "Unknown"),
            )
            for b in raw_list
        ]
