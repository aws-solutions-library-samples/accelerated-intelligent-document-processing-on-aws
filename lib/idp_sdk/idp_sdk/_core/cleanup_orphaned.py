# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Orphaned resources cleanup module for IDP CLI.

Safely identifies and cleans up orphaned IDP resources from deleted stacks.

Safety features:
- Only touches resources belonging to verified IDP stacks (identified by
  CloudFormation Description containing "AWS GenAI IDP Accelerator")
- Only deletes resources from stacks in DELETE_COMPLETE state
- Multi-region stack discovery for global resources (CloudFront, IAM)
- Confirmation prompts by default (use --yes to skip)
- Stack ID tracking for audit trail
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import boto3
import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

logger = logging.getLogger(__name__)
console = Console()

# Regions to check for IDP stacks when the caller names none. A superset of the
# Regions releases are published to (`scripts/aws-release.sh`), because a customer
# can deploy from source anywhere — but every published Region belongs here, or
# the orphaned resources of a deleted stack in it are skipped and leak.
#
# ⚠️ This is the *SDK* default, reached only when `regions is None`. The
# `idp-cli remove-deleted-stack-resources` option carries its own, narrower
# default (three Regions) and always passes it, so adding a Region here does not
# change what the CLI scans.
IDP_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "ap-southeast-1",
    "ap-northeast-1",
    "ca-central-1",
]

# IDP solution identifier in CloudFormation Description
IDP_DESCRIPTION_MARKER = "AWS GenAI IDP Accelerator"


class OrphanedResourceCleanup:
    """Safely clean up orphaned IDP resources from deleted stacks."""

    def __init__(self, region: str, profile: Optional[str] = None):
        """Initialize cleanup with AWS session.

        Args:
            region: Primary AWS region for stack discovery
            profile: AWS profile name (optional)
        """
        self.region = region
        self.session = (
            boto3.Session(profile_name=profile) if profile else boto3.Session()
        )

        # Initialize clients
        self.cloudfront = self.session.client("cloudfront")
        self.logs = self.session.client("logs", region_name=region)
        self.appsync = self.session.client("appsync", region_name=region)
        self.iam = self.session.client("iam")
        self.sts = self.session.client("sts")
        self.s3 = self.session.client("s3", region_name=region)
        self.dynamodb = self.session.client("dynamodb", region_name=region)

        self.account_id = self.sts.get_caller_identity()["Account"]

        # Stack caches populated by discover_idp_stacks()
        self._active_stacks: Dict[str, Dict] = {}  # stack_name -> {id, region, status}
        self._deleted_stacks: Dict[str, Dict] = {}  # stack_name -> {id, region, status}
        self._discovery_complete = False
        # stack_name -> skip reason ("" once confirmed gone everywhere); see
        # _still_present_elsewhere.
        self._presence_elsewhere: Dict[str, str] = {}

        # Track "yes/no to all" decisions per resource type
        self._yes_to_all: Dict[str, bool] = {}  # resource_type -> True/False
        self._no_to_all: Dict[str, bool] = {}  # resource_type -> True/False

        # Max concurrent workers for bulk deletion
        # AWS service limits are typically 100+ RPS for most operations
        # 50 workers strikes balance between speed and not overwhelming the API
        self._max_workers = 50

    def _get_cfn_client(self, region: str):
        """Get CloudFormation client for a specific region."""
        return self.session.client("cloudformation", region_name=region)

    def discover_idp_stacks(self, regions: Optional[List[str]] = None) -> Dict:
        """Discover all IDP stacks (active AND deleted) across regions.

        Identifies IDP stacks by checking if the CloudFormation Description
        contains "AWS GenAI IDP Accelerator".

        Args:
            regions: List of regions to check (defaults to IDP_REGIONS)

        Returns:
            Dict with 'active_stacks' and 'deleted_stacks' mappings
        """
        if regions is None:
            regions = IDP_REGIONS

        console.print("[bold blue]Discovering IDP stacks across regions...[/bold blue]")

        for region in regions:
            console.print(f"  Checking {region}...")
            cfn = self._get_cfn_client(region)

            try:
                # List all stacks including deleted ones
                paginator = cfn.get_paginator("list_stacks")

                # Include all stacks - any non-DELETE_COMPLETE state means resources may exist
                # We don't filter by status here to ensure we catch ALL stacks including
                # those in transient states (DELETE_IN_PROGRESS, UPDATE_IN_PROGRESS, etc.)
                for page in paginator.paginate():
                    for stack_summary in page.get("StackSummaries", []):
                        stack_name = stack_summary.get("StackName", "")
                        stack_id = stack_summary.get("StackId", "")
                        stack_status = stack_summary.get("StackStatus", "")

                        # Check if it's an IDP stack by description or name patterns
                        # Note: Deleted stacks may not have full details, so also check name patterns
                        is_idp_stack = False

                        # Try to get description from template metadata
                        template_desc = stack_summary.get("TemplateDescription", "")
                        if IDP_DESCRIPTION_MARKER in template_desc:
                            is_idp_stack = True

                        # Also check for IDP naming patterns as fallback
                        # (deleted stacks may not retain full metadata)
                        if not is_idp_stack and stack_name.upper().startswith("IDP-"):
                            # Additional validation: check for IDP-specific nested stacks
                            if any(
                                pattern in stack_name
                                for pattern in [
                                    "PATTERN1",
                                    "PATTERN2",
                                    "-p1-",
                                    "-p2-",
                                    "-p3-",
                                ]
                            ):
                                is_idp_stack = True
                            # Main IDP stacks (not nested) also qualify
                            elif stack_name.count("-") <= 2:
                                is_idp_stack = True

                        if is_idp_stack:
                            stack_info = {
                                "id": stack_id,
                                "region": region,
                                "status": stack_status,
                            }

                            if stack_status == "DELETE_COMPLETE":
                                # Only add to deleted if not already in active
                                # (CloudFormation can have same-named stacks with different IDs)
                                stack_name_upper = stack_name.upper()
                                already_active = any(
                                    name.upper() == stack_name_upper
                                    for name in self._active_stacks
                                )
                                if not already_active:
                                    self._deleted_stacks[stack_name] = stack_info
                            else:
                                # SAFETY: Any state that is NOT DELETE_COMPLETE means the stack
                                # (and its resources) may still exist. This includes:
                                # - Active states: CREATE_COMPLETE, UPDATE_COMPLETE, etc.
                                # - Failed states: DELETE_FAILED, CREATE_FAILED (resources may exist!)
                                # - Transient states: DELETE_IN_PROGRESS, UPDATE_IN_PROGRESS, etc.
                                # We protect ALL of these to prevent accidental resource deletion.
                                self._active_stacks[stack_name] = stack_info

            except Exception as e:
                logger.warning(f"Error checking region {region}: {e}")

        # CRITICAL SAFETY: Remove any stacks from deleted that have an active version
        # This handles the case where active stack is discovered AFTER deleted stack
        active_names_upper = {name.upper() for name in self._active_stacks}
        stacks_to_remove = [
            name for name in self._deleted_stacks if name.upper() in active_names_upper
        ]
        for name in stacks_to_remove:
            del self._deleted_stacks[name]
            console.print(
                f"  [yellow]Note: {name} has both active and deleted instances - protecting active stack[/yellow]"
            )

        self._discovery_complete = True

        console.print(
            f"  Found [green]{len(self._active_stacks)}[/green] active IDP stacks"
        )
        console.print(
            f"  Found [yellow]{len(self._deleted_stacks)}[/yellow] deleted IDP stacks"
        )
        console.print()

        return {
            "active_stacks": self._active_stacks,
            "deleted_stacks": self._deleted_stacks,
        }

    def get_stack_state(self, stack_name: str) -> Tuple[str, Optional[Dict]]:
        """Get stack state and info for a given stack name.

        Performs case-insensitive lookup since bucket names may use lowercase
        while CloudFormation stack names may use uppercase.

        Returns:
            Tuple of (state, stack_info) where state is:
            - 'ACTIVE': Stack exists and is operational
            - 'DELETED': Stack was deleted (IDP stack confirmed)
            - 'UNKNOWN': Stack not found in IDP stack discovery
        """
        if not self._discovery_complete:
            self.discover_idp_stacks()

        # Case-insensitive lookup
        stack_name_upper = stack_name.upper()

        for name, info in self._active_stacks.items():
            if name.upper() == stack_name_upper:
                return ("ACTIVE", info)

        for name, info in self._deleted_stacks.items():
            if name.upper() == stack_name_upper:
                return ("DELETED", info)

        return ("UNKNOWN", None)

    def _still_present_elsewhere(self, stack_name: str) -> str:
        """Why a DELETED stack's *global* resource must still be left alone, or "".

        Discovery covers only the Regions it was given -- three by default from
        the CLI -- while CloudFront distributions, response headers policies and
        IAM policies are global and carry only the stack *name*. A stack deleted
        in a scanned Region and since recreated under the same name in an
        unscanned one therefore reads as DELETED, and its live resources were
        swept (#1533). So before a global resource is touched on the strength of
        a DELETED verdict, every Region enabled for the account is asked whether
        a stack of that name still exists there.

        Returns "" only when every enabled Region answers that it does not. Any
        Region that cannot be read makes ownership unknown, and unknown means
        leave it alone -- the same rule ``get_stack_state`` applies with UNKNOWN.
        """
        if stack_name in self._presence_elsewhere:
            return self._presence_elsewhere[stack_name]

        reason = ""
        try:
            ec2 = self.session.client("ec2", region_name=self.region)
            # Without AllRegions this lists only the Regions enabled for the
            # account, which are the only ones a stack can exist in.
            regions = [r["RegionName"] for r in ec2.describe_regions()["Regions"]]
        except Exception as e:
            regions = []
            reason = f"could not list enabled Regions to confirm it is gone: {e}"

        for region in regions:
            try:
                stacks = (
                    self._get_cfn_client(region)
                    .describe_stacks(StackName=stack_name)
                    .get("Stacks", [])
                )
            except Exception as e:
                if "does not exist" in str(e):
                    continue
                reason = f"could not confirm it is gone in {region}: {e}"
                break
            if any(s.get("StackStatus") != "DELETE_COMPLETE" for s in stacks):
                reason = f"still exists in {region}"
                break

        self._presence_elsewhere[stack_name] = reason
        return reason

    def extract_stack_name_from_comment(self, comment: str) -> str:
        """Extract stack name from CloudFront distribution comment."""
        if comment.startswith("Web app cloudfront distribution "):
            return comment.replace("Web app cloudfront distribution ", "")
        return ""

    def extract_stack_name_from_log_group(self, log_group_name: str) -> str:
        """Extract stack name from log group name."""
        if log_group_name.startswith("/") and "/lambda/" in log_group_name:
            parts = log_group_name[1:].split("/lambda/")[0]
            if "-" in parts:
                stack_parts = parts.split("-")
                for i in range(len(stack_parts)):
                    if "PATTERN" in stack_parts[i] or "STACK" in stack_parts[i]:
                        return "-".join(stack_parts[:i])
        elif (
            "/aws-glue/crawlers-role/" in log_group_name
            and "DocumentSectionsCrawlerRole" in log_group_name
        ):
            parts = log_group_name.replace("/aws-glue/crawlers-role/", "")
            if "-DocumentSectionsCrawlerRole-" in parts:
                return parts.split("-DocumentSectionsCrawlerRole-")[0]
        return ""

    def extract_stack_name_from_api_name(self, api_name: str) -> str:
        """Extract stack name from AppSync API name."""
        if api_name.endswith("-api"):
            if "-p1-api" in api_name:
                return api_name.replace("-p1-api", "")
            elif "-p2-api" in api_name:
                return api_name.replace("-p2-api", "")
            elif "-p3-api" in api_name:
                return api_name.replace("-p3-api", "")
            else:
                return api_name.replace("-api", "")
        return ""

    def extract_stack_name_from_policy_name(self, policy_name: str) -> str:
        """Extract stack name from policy name."""
        if policy_name.endswith("-security-headers-policy"):
            return policy_name.replace("-security-headers-policy", "")
        elif policy_name.endswith("-PermissionsBoundary"):
            return policy_name.replace("-PermissionsBoundary", "")
        elif "PATTERN" in policy_name:
            parts = policy_name.split("-")
            for i, part in enumerate(parts):
                if "PATTERN" in part:
                    return "-".join(parts[:i])
        return ""

    def extract_stack_name_from_bucket_name(self, bucket_name: str) -> str:
        """Extract stack name from S3 bucket name.

        IDP bucket names follow patterns like:
        - {stack-name}-inputbucket-{random}
        - {stack-name}-outputbucket-{random}
        - {stack-name}-workingbucket-{random}
        - {stack-name}-loggingbucket-{random}
        - {stack-name}-configurationbucket-{random}
        - {stack-name}-configbucket-{random}
        - {stack-name}-testsetbucket-{random}
        - {stack-name}-discoverybucket-{random}
        - {stack-name}-evaluationbaselinebucket-{random}
        - {stack-name}-reportingbucket-{random}
        """
        bucket_suffixes = [
            "-inputbucket-",
            "-outputbucket-",
            "-workingbucket-",
            "-loggingbucket-",
            "-configurationbucket-",
            "-configbucket-",
            "-testsetbucket-",
            "-discoverybucket-",
            "-evaluationbaselinebucket-",
            "-reportingbucket-",
        ]

        bucket_lower = bucket_name.lower()
        for suffix in bucket_suffixes:
            if suffix in bucket_lower:
                # Find the position of the suffix
                idx = bucket_lower.find(suffix)
                if idx > 0:
                    return bucket_name[:idx]
        return ""

    def extract_stack_name_from_table_name(self, table_name: str) -> str:
        """Extract stack name from DynamoDB table name.

        IDP table names follow patterns like:
        - {stack-name}-TrackingTable-{random}
        - {stack-name}-ConfigTable-{random}
        - {stack-name}-AgentTable-{random}
        """
        table_suffixes = [
            "-TrackingTable-",
            "-ConfigTable-",
            "-AgentTable-",
            "-MeteringTable-",
        ]

        for suffix in table_suffixes:
            if suffix in table_name:
                idx = table_name.find(suffix)
                if idx > 0:
                    return table_name[:idx]
        return ""

    def _confirm_deletion(
        self,
        resource_type: str,
        resource_id: str,
        stack_name: str,
        stack_info: Dict,
        auto_approve: bool,
        remaining_count: int = 0,
    ) -> bool:
        """Prompt user for confirmation before deleting a resource.

        Args:
            resource_type: Type of resource (e.g., "CloudFront distribution")
            resource_id: Resource identifier
            stack_name: Name of the associated stack
            stack_info: Stack information dict with id, region, status
            auto_approve: If True, skip confirmation and return True
            remaining_count: Number of remaining resources of this type after this one

        Returns:
            True if user confirms deletion, False otherwise
        """
        if auto_approve:
            return True

        # Check if user already said "yes to all" or "no to all" for this type
        if self._yes_to_all.get(resource_type):
            console.print(
                f"  [green]Auto-approving[/green] {resource_id} (yes to all {resource_type})"
            )
            return True
        if self._no_to_all.get(resource_type):
            console.print(
                f"  [yellow]Auto-skipping[/yellow] {resource_id} (no to all {resource_type})"
            )
            return False

        console.print()
        console.print(f"[bold yellow]Delete orphaned {resource_type}?[/bold yellow]")
        console.print(f"  Resource: [cyan]{resource_id}[/cyan] (exists in AWS)")
        console.print(f"  Originally from stack: {stack_name}")
        console.print(
            "  Stack status: [red]DELETE_COMPLETE[/red] (stack no longer exists)"
        )
        console.print(f"  Stack was in region: {stack_info['region']}")
        console.print()

        # Format the remaining count message
        if remaining_count > 0:
            remaining_msg = f"a=yes to all {remaining_count} remaining {resource_type}s, s=skip all {remaining_count} remaining"
        else:
            remaining_msg = f"a=yes to all {resource_type}s, s=skip all"
        console.print(f"  [dim]Options: y=yes, n=no, {remaining_msg}[/dim]")

        while True:
            response = (
                click.prompt(
                    "Delete? [y/n/a/s]",
                    type=str,
                    default="n",
                    show_default=False,
                )
                .lower()
                .strip()
            )

            if response in ("y", "yes"):
                return True
            elif response in ("n", "no"):
                return False
            elif response in ("a", "all", "yes to all"):
                self._yes_to_all[resource_type] = True
                if remaining_count > 0:
                    console.print(
                        f"  [green]Will auto-approve all {remaining_count} remaining {resource_type}s[/green]"
                    )
                else:
                    console.print(
                        f"  [green]Will auto-approve all remaining {resource_type}s[/green]"
                    )
                return True
            elif response in ("s", "skip", "skip all", "no to all"):
                self._no_to_all[resource_type] = True
                if remaining_count > 0:
                    console.print(
                        f"  [yellow]Will skip all {remaining_count} remaining {resource_type}s[/yellow]"
                    )
                else:
                    console.print(
                        f"  [yellow]Will skip all remaining {resource_type}s[/yellow]"
                    )
                return False
            else:
                console.print(
                    "  [red]Invalid option. Enter y, n, a (yes to all), or s (skip all)[/red]"
                )

    def _delete_resources_concurrently(
        self,
        resource_type: str,
        resources: List[Tuple],
        delete_fn,
        dry_run: bool = False,
        auto_approve: bool = False,
    ) -> Dict:
        """Generic concurrent deletion for any resource type.

        Args:
            resource_type: Type name for display (e.g., "S3 Bucket")
            resources: List of tuples (resource_id, stack_name, stack_info)
            delete_fn: Function that takes (resource_id, stack_name) and returns (success, message)
            dry_run: If True, only report what would be deleted
            auto_approve: If True, skip confirmation prompts

        Returns:
            Dict with 'deleted', 'skipped', 'errors' lists
        """
        results = {"deleted": [], "skipped": [], "errors": []}

        if not resources:
            return results

        total = len(resources)
        resources_to_delete_concurrently = []

        for idx, (resource_id, stack_name, stack_info) in enumerate(resources):
            remaining = total - idx - 1

            if dry_run:
                results["deleted"].append(
                    f"{resource_id} (stack: {stack_name}) [DRY RUN]"
                )
            elif self._yes_to_all.get(resource_type) or auto_approve:
                # Queue for concurrent deletion
                resources_to_delete_concurrently.append((resource_id, stack_name))
            elif self._confirm_deletion(
                resource_type,
                resource_id,
                stack_name,
                stack_info,
                auto_approve,
                remaining_count=remaining,
            ):
                if self._yes_to_all.get(resource_type):
                    # User just selected "yes to all" - queue this and remaining
                    resources_to_delete_concurrently.append((resource_id, stack_name))
                    for future_res in resources[idx + 1 :]:
                        resources_to_delete_concurrently.append(
                            (future_res[0], future_res[1])
                        )
                    break
                else:
                    # Single resource deletion
                    success, message = delete_fn(resource_id, stack_name)
                    if success:
                        results["deleted"].append(message)
                    else:
                        results["errors"].append(message)
            else:
                if self._no_to_all.get(resource_type):
                    # User selected "skip all" - skip remaining
                    for future_res in resources[idx:]:
                        results["skipped"].append(
                            f"{future_res[0]} (stack: {future_res[1]} - user declined)"
                        )
                    break
                results["skipped"].append(
                    f"{resource_id} (stack: {stack_name} - user declined)"
                )

        # Concurrent deletion if resources were queued
        if resources_to_delete_concurrently:
            count = len(resources_to_delete_concurrently)
            console.print(
                f"\n[bold blue]Deleting {count} {resource_type}s concurrently...[/bold blue]"
            )
            max_workers = min(self._max_workers, count)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task(
                    f"Deleting {resource_type}s (0/{count})", total=count
                )

                completed = 0
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(delete_fn, res_id, s_name): (res_id, s_name)
                        for res_id, s_name in resources_to_delete_concurrently
                    }

                    for future in as_completed(futures):
                        success, message = future.result()
                        completed += 1
                        progress.update(
                            task,
                            description=f"Deleting {resource_type}s ({completed}/{count})",
                        )

                        if success:
                            results["deleted"].append(message)
                        else:
                            results["errors"].append(message)

        return results

    def cleanup_cloudfront_distributions(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up CloudFront distributions from deleted IDP stacks."""
        results = {"deleted": [], "disabled": [], "skipped": [], "errors": []}

        try:
            response = self.cloudfront.list_distributions()
            items = response.get("DistributionList", {}).get("Items", [])

            for distribution in items:
                comment = distribution.get("Comment", "")
                if not comment.startswith("Web app cloudfront distribution "):
                    continue

                distribution_id = distribution["Id"]
                stack_name = self.extract_stack_name_from_comment(comment)

                if not stack_name:
                    continue

                state, stack_info = self.get_stack_state(stack_name)

                if state == "DELETED":
                    elsewhere = self._still_present_elsewhere(stack_name)
                    if elsewhere:
                        results["skipped"].append(
                            f"{distribution_id} (stack: {stack_name} - {elsewhere})"
                        )
                        continue

                if state == "ACTIVE":
                    results["skipped"].append(
                        f"{distribution_id} (stack: {stack_name} - active)"
                    )
                elif state == "UNKNOWN":
                    results["skipped"].append(
                        f"{distribution_id} (stack: {stack_name} - not verified IDP)"
                    )
                elif state == "DELETED":
                    # Resource belongs to deleted IDP stack - safe to clean up
                    is_disabled = not distribution.get("Enabled", True)
                    is_deployed = distribution.get("Status") == "Deployed"

                    if is_disabled and is_deployed:
                        # Previously disabled, now delete
                        if dry_run:
                            results["deleted"].append(
                                f"{distribution_id} (stack: {stack_name}) [DRY RUN]"
                            )
                        elif self._confirm_deletion(
                            "CloudFront distribution",
                            distribution_id,
                            stack_name,
                            stack_info,
                            auto_approve,
                        ):
                            try:
                                config_response = self.cloudfront.get_distribution(
                                    Id=distribution_id
                                )
                                etag = config_response["ETag"]
                                self.cloudfront.delete_distribution(
                                    Id=distribution_id, IfMatch=etag
                                )
                                results["deleted"].append(
                                    f"{distribution_id} (stack: {stack_name})"
                                )
                            except Exception as e:
                                results["errors"].append(
                                    f"Failed to delete {distribution_id}: {e}"
                                )
                        else:
                            results["skipped"].append(
                                f"{distribution_id} (stack: {stack_name} - user declined)"
                            )
                    else:
                        # Enabled - disable first (CloudFront requirement)
                        if dry_run:
                            results["disabled"].append(
                                f"{distribution_id} (stack: {stack_name}) [DRY RUN]"
                            )
                        elif self._confirm_deletion(
                            "CloudFront distribution (disable)",
                            distribution_id,
                            stack_name,
                            stack_info,
                            auto_approve,
                        ):
                            try:
                                config_response = self.cloudfront.get_distribution(
                                    Id=distribution_id
                                )
                                etag = config_response["ETag"]
                                config = config_response["Distribution"][
                                    "DistributionConfig"
                                ]
                                config["Enabled"] = False
                                self.cloudfront.update_distribution(
                                    Id=distribution_id,
                                    DistributionConfig=config,
                                    IfMatch=etag,
                                )
                                results["disabled"].append(
                                    f"{distribution_id} (stack: {stack_name})"
                                )
                            except Exception as e:
                                results["errors"].append(
                                    f"Failed to disable {distribution_id}: {e}"
                                )
                        else:
                            results["skipped"].append(
                                f"{distribution_id} (stack: {stack_name} - user declined)"
                            )

        except Exception as e:
            results["errors"].append(f"Failed to list CloudFront distributions: {e}")

        return results

    def cleanup_log_groups(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up log groups from deleted IDP stacks."""
        results = {"deleted": [], "skipped": [], "errors": []}

        try:
            paginator = self.logs.get_paginator("describe_log_groups")
            for page in paginator.paginate():
                for log_group in page.get("logGroups", []):
                    log_group_name = log_group.get("logGroupName", "")

                    # Check for IDP-specific patterns
                    is_idp_log_group = (
                        ("/lambda/" in log_group_name and "-PATTERN" in log_group_name)
                        or (
                            log_group_name.startswith("/aws-glue/crawlers-role/")
                            and "DocumentSectionsCrawlerRole" in log_group_name
                        )
                        or (
                            log_group_name.startswith("/aws/appsync/apis/")
                            and "IDP" in log_group_name
                        )
                    )

                    if not is_idp_log_group:
                        continue

                    stack_name = self.extract_stack_name_from_log_group(log_group_name)
                    if not stack_name:
                        continue

                    state, stack_info = self.get_stack_state(stack_name)

                    if state == "ACTIVE":
                        results["skipped"].append(
                            f"{log_group_name} (stack: {stack_name} - active)"
                        )
                    elif state == "UNKNOWN":
                        results["skipped"].append(
                            f"{log_group_name} (stack: {stack_name} - not verified IDP)"
                        )
                    elif state == "DELETED":
                        if dry_run:
                            results["deleted"].append(
                                f"{log_group_name} (stack: {stack_name}) [DRY RUN]"
                            )
                        elif self._confirm_deletion(
                            "CloudWatch Log Group",
                            log_group_name,
                            stack_name,
                            stack_info,
                            auto_approve,
                        ):
                            try:
                                self.logs.delete_log_group(logGroupName=log_group_name)
                                results["deleted"].append(
                                    f"{log_group_name} (stack: {stack_name})"
                                )
                            except Exception as e:
                                results["errors"].append(
                                    f"Failed to delete {log_group_name}: {e}"
                                )
                        else:
                            results["skipped"].append(
                                f"{log_group_name} (stack: {stack_name} - user declined)"
                            )

        except Exception as e:
            results["errors"].append(f"Failed to list log groups: {e}")

        return results

    def cleanup_appsync_apis(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up AppSync APIs from deleted IDP stacks."""
        results = {"deleted": [], "skipped": [], "errors": []}

        try:
            response = self.appsync.list_graphql_apis()

            for api in response.get("graphqlApis", []):
                api_name = api.get("name", "")
                api_id = api.get("apiId")

                # Check for IDP API patterns
                is_idp_api = (
                    api_name.endswith("-api")
                    and api_name.startswith("IDP-")
                    and (
                        "-p1-api" in api_name
                        or "-p2-api" in api_name
                        or "-p3-api" in api_name
                        or api_name.endswith("-api")
                    )
                )

                if not is_idp_api or not api_id:
                    continue

                stack_name = self.extract_stack_name_from_api_name(api_name)
                if not stack_name:
                    continue

                state, stack_info = self.get_stack_state(stack_name)

                if state == "ACTIVE":
                    results["skipped"].append(
                        f"{api_name} (stack: {stack_name} - active)"
                    )
                elif state == "UNKNOWN":
                    results["skipped"].append(
                        f"{api_name} (stack: {stack_name} - not verified IDP)"
                    )
                elif state == "DELETED":
                    if dry_run:
                        results["deleted"].append(
                            f"{api_name} ({api_id}) (stack: {stack_name}) [DRY RUN]"
                        )
                    elif self._confirm_deletion(
                        "AppSync API",
                        f"{api_name} ({api_id})",
                        stack_name,
                        stack_info,
                        auto_approve,
                    ):
                        try:
                            self.appsync.delete_graphql_api(apiId=api_id)
                            results["deleted"].append(
                                f"{api_name} ({api_id}) (stack: {stack_name})"
                            )

                            # Also clean up associated log group
                            log_group_name = f"/aws/appsync/apis/{api_id}"
                            try:
                                self.logs.delete_log_group(logGroupName=log_group_name)
                            except Exception:
                                pass
                        except Exception as e:
                            results["errors"].append(
                                f"Failed to delete AppSync API {api_name}: {e}"
                            )
                    else:
                        results["skipped"].append(
                            f"{api_name} (stack: {stack_name} - user declined)"
                        )

        except Exception as e:
            results["errors"].append(f"Failed to list AppSync APIs: {e}")

        return results

    def cleanup_cloudfront_policies(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up CloudFront Response Headers Policies from deleted IDP stacks.

        NOTE: Policies can only be deleted after their associated CloudFront
        distributions are fully deleted (not just disabled). Run this cleanup
        after distributions have been deleted, which may take 15-20 minutes
        after disabling.
        """
        results = {"deleted": [], "skipped": [], "errors": []}

        try:
            response = self.cloudfront.list_response_headers_policies()

            for policy in response.get("ResponseHeadersPolicyList", {}).get(
                "Items", []
            ):
                if policy["Type"] != "custom":
                    continue

                policy_config = policy["ResponseHeadersPolicy"][
                    "ResponseHeadersPolicyConfig"
                ]
                policy_name = policy_config["Name"]
                policy_id = policy["ResponseHeadersPolicy"]["Id"]

                # Check for IDP policy pattern
                if not policy_name.endswith(
                    "-security-headers-policy"
                ) or not policy_name.startswith("IDP-"):
                    continue

                stack_name = self.extract_stack_name_from_policy_name(policy_name)
                if not stack_name:
                    continue

                state, stack_info = self.get_stack_state(stack_name)

                if state == "DELETED":
                    elsewhere = self._still_present_elsewhere(stack_name)
                    if elsewhere:
                        results["skipped"].append(
                            f"{policy_name} (stack: {stack_name} - {elsewhere})"
                        )
                        continue

                if state == "ACTIVE":
                    results["skipped"].append(
                        f"{policy_name} (stack: {stack_name} - active)"
                    )
                elif state == "UNKNOWN":
                    results["skipped"].append(
                        f"{policy_name} (stack: {stack_name} - not verified IDP)"
                    )
                elif state == "DELETED":
                    if dry_run:
                        results["deleted"].append(
                            f"{policy_name} (stack: {stack_name}) [DRY RUN]"
                        )
                    elif self._confirm_deletion(
                        "CloudFront Response Headers Policy",
                        policy_name,
                        stack_name,
                        stack_info,
                        auto_approve,
                    ):
                        try:
                            # Get the ETag first (required for delete)
                            policy_response = (
                                self.cloudfront.get_response_headers_policy(
                                    Id=policy_id
                                )
                            )
                            etag = policy_response.get("ETag")

                            # Delete with ETag
                            self.cloudfront.delete_response_headers_policy(
                                Id=policy_id, IfMatch=etag
                            )
                            results["deleted"].append(
                                f"{policy_name} (stack: {stack_name})"
                            )
                        except self.cloudfront.exceptions.ResponseHeadersPolicyInUse:
                            results["skipped"].append(
                                f"{policy_name} (stack: {stack_name} - still in use by distribution, re-run after distributions are deleted)"
                            )
                        except Exception as e:
                            results["errors"].append(
                                f"Failed to delete policy {policy_name}: {e}"
                            )
                    else:
                        results["skipped"].append(
                            f"{policy_name} (stack: {stack_name} - user declined)"
                        )

        except Exception as e:
            results["errors"].append(f"Failed to list CloudFront policies: {e}")

        return results

    def cleanup_iam_policies(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up IAM policies from deleted IDP stacks."""
        results = {"deleted": [], "skipped": [], "errors": []}

        try:
            paginator = self.iam.get_paginator("list_policies")
            for page in paginator.paginate(Scope="Local"):
                for policy in page.get("Policies", []):
                    policy_name = policy.get("PolicyName", "")
                    policy_arn = policy.get("Arn")

                    # Check for IDP policy patterns - must have IDP prefix
                    is_idp_policy = (
                        policy_name.startswith("IDP-")
                        and "PATTERN" in policy_name
                        and (
                            "STACK" in policy_name
                            or "LambdaECRAccessPolicy" in policy_name
                        )
                    ) or (
                        policy_name.startswith("IDP-")
                        and policy_name.endswith("-PermissionsBoundary")
                    )

                    if not is_idp_policy or not policy_arn:
                        continue

                    stack_name = self.extract_stack_name_from_policy_name(policy_name)
                    if not stack_name:
                        continue

                    state, stack_info = self.get_stack_state(stack_name)

                    if state == "DELETED":
                        elsewhere = self._still_present_elsewhere(stack_name)
                        if elsewhere:
                            results["skipped"].append(
                                f"{policy_name} (stack: {stack_name} - {elsewhere})"
                            )
                            continue

                    if state == "ACTIVE":
                        results["skipped"].append(
                            f"{policy_name} (stack: {stack_name} - active)"
                        )
                    elif state == "UNKNOWN":
                        results["skipped"].append(
                            f"{policy_name} (stack: {stack_name} - not verified IDP)"
                        )
                    elif state == "DELETED":
                        if dry_run:
                            results["deleted"].append(
                                f"{policy_name} (stack: {stack_name}) [DRY RUN]"
                            )
                        elif self._confirm_deletion(
                            "IAM Policy",
                            policy_name,
                            stack_name,
                            stack_info,
                            auto_approve,
                        ):
                            try:
                                self.iam.delete_policy(PolicyArn=policy_arn)
                                results["deleted"].append(
                                    f"{policy_name} (stack: {stack_name})"
                                )
                            except Exception as e:
                                results["errors"].append(
                                    f"Failed to delete policy {policy_name}: {e}"
                                )
                        else:
                            results["skipped"].append(
                                f"{policy_name} (stack: {stack_name} - user declined)"
                            )

        except Exception as e:
            results["errors"].append(f"Failed to list IAM policies: {e}")

        return results

    def cleanup_logs_resource_policies(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up CloudWatch Logs resource policies with IDP entries."""
        results = {"updated": [], "deleted": [], "errors": []}

        try:
            response = self.logs.describe_resource_policies()

            for policy in response.get("resourcePolicies", []):
                policy_name = policy.get("policyName", "")

                if policy_name == "AWSLogDeliveryWrite20150319":
                    try:
                        policy_doc = json.loads(policy.get("policyDocument", "{}"))
                        original_statements = policy_doc.get("Statement", [])
                        original_count = len(original_statements)

                        # Filter out statements for deleted IDP stacks only
                        new_statements = []
                        for stmt in original_statements:
                            resource = stmt.get("Resource", "")
                            if "/aws/vendedlogs/states/" in resource:
                                # Extract stack name from resource ARN
                                # Format: arn:aws:logs:...:log-group:/aws/vendedlogs/states/IDP-*
                                parts = resource.split("/aws/vendedlogs/states/")
                                if len(parts) > 1:
                                    log_group_suffix = parts[1].split(":")[0]
                                    # Try to extract stack name
                                    if log_group_suffix.startswith("IDP-"):
                                        stack_parts = log_group_suffix.split("-")
                                        for i in range(len(stack_parts)):
                                            if "PATTERN" in stack_parts[i]:
                                                stack_name = "-".join(stack_parts[:i])
                                                break
                                        else:
                                            stack_name = log_group_suffix

                                        state, _ = self.get_stack_state(stack_name)
                                        if state != "DELETED":
                                            new_statements.append(stmt)
                                            continue
                            new_statements.append(stmt)

                        policy_doc["Statement"] = new_statements
                        new_count = len(new_statements)

                        if new_count < original_count:
                            removed = original_count - new_count
                            if dry_run:
                                results["updated"].append(
                                    f"{policy_name} ({removed} entries) [DRY RUN]"
                                )
                            elif auto_approve or click.confirm(
                                f"Remove {removed} orphaned entries from {policy_name}?",
                                default=False,
                            ):
                                self.logs.put_resource_policy(
                                    policyName=policy_name,
                                    policyDocument=json.dumps(policy_doc),
                                )
                                results["updated"].append(
                                    f"{policy_name} ({removed} entries removed)"
                                )

                    except Exception as e:
                        results["errors"].append(f"Failed to update {policy_name}: {e}")

        except Exception as e:
            results["errors"].append(f"Failed to list resource policies: {e}")

        return results

    def cleanup_s3_buckets(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up S3 buckets from deleted IDP stacks."""
        results = {"deleted": [], "skipped": [], "errors": []}

        def delete_bucket(bucket_name: str, stack_name: str) -> Tuple[bool, str]:
            """Delete a single S3 bucket."""
            try:
                self._empty_s3_bucket(bucket_name)
                self.s3.delete_bucket(Bucket=bucket_name)
                return (True, f"{bucket_name} (stack: {stack_name})")
            except Exception as e:
                return (False, f"Failed to delete bucket {bucket_name}: {e}")

        try:
            response = self.s3.list_buckets()

            # Collect buckets eligible for deletion
            deletable_buckets = []
            for bucket in response.get("Buckets", []):
                bucket_name = bucket.get("Name", "")

                stack_name = self.extract_stack_name_from_bucket_name(bucket_name)
                if not stack_name:
                    continue

                state, stack_info = self.get_stack_state(stack_name)

                if state == "UNKNOWN":
                    continue
                elif state == "ACTIVE":
                    results["skipped"].append(
                        f"{bucket_name} (stack: {stack_name} - active)"
                    )
                elif state == "DELETED":
                    deletable_buckets.append((bucket_name, stack_name, stack_info))

            # Use generic concurrent deletion
            deletion_results = self._delete_resources_concurrently(
                "S3 Bucket",
                deletable_buckets,
                delete_bucket,
                dry_run,
                auto_approve,
            )
            results["deleted"].extend(deletion_results["deleted"])
            results["skipped"].extend(deletion_results["skipped"])
            results["errors"].extend(deletion_results["errors"])

        except Exception as e:
            results["errors"].append(f"Failed to list S3 buckets: {e}")

        return results

    def _empty_s3_bucket(self, bucket_name: str) -> None:
        """Empty an S3 bucket by deleting all objects and versions."""
        s3_resource = self.session.resource("s3")
        bucket = s3_resource.Bucket(bucket_name)

        # Delete all object versions (handles versioned buckets)
        try:
            bucket.object_versions.delete()
        except Exception:
            # If versioning not enabled, just delete objects
            try:
                bucket.objects.all().delete()
            except Exception as e:
                logger.warning(f"Error emptying bucket {bucket_name}: {e}")

    def cleanup_dynamodb_tables(
        self, dry_run: bool = False, auto_approve: bool = False
    ) -> Dict:
        """Clean up DynamoDB tables from deleted IDP stacks."""
        results = {"deleted": [], "skipped": [], "errors": []}

        def delete_table(table_name: str, stack_name: str) -> Tuple[bool, str]:
            """Delete a single DynamoDB table."""
            try:
                # Disable PITR before deletion if enabled
                try:
                    self.dynamodb.update_continuous_backups(
                        TableName=table_name,
                        PointInTimeRecoverySpecification={
                            "PointInTimeRecoveryEnabled": False
                        },
                    )
                except Exception:
                    pass  # PITR might not be enabled

                self.dynamodb.delete_table(TableName=table_name)
                return (True, f"{table_name} (stack: {stack_name})")
            except Exception as e:
                return (False, f"Failed to delete table {table_name}: {e}")

        try:
            paginator = self.dynamodb.get_paginator("list_tables")

            # Collect tables eligible for deletion
            deletable_tables = []
            for page in paginator.paginate():
                for table_name in page.get("TableNames", []):
                    stack_name = self.extract_stack_name_from_table_name(table_name)
                    if not stack_name:
                        continue

                    state, stack_info = self.get_stack_state(stack_name)

                    if state == "UNKNOWN":
                        continue
                    elif state == "ACTIVE":
                        results["skipped"].append(
                            f"{table_name} (stack: {stack_name} - active)"
                        )
                    elif state == "DELETED":
                        deletable_tables.append((table_name, stack_name, stack_info))

            # Use generic concurrent deletion
            deletion_results = self._delete_resources_concurrently(
                "DynamoDB Table",
                deletable_tables,
                delete_table,
                dry_run,
                auto_approve,
            )
            results["deleted"].extend(deletion_results["deleted"])
            results["skipped"].extend(deletion_results["skipped"])
            results["errors"].extend(deletion_results["errors"])

        except Exception as e:
            results["errors"].append(f"Failed to list DynamoDB tables: {e}")

        return results

    def run_cleanup(
        self,
        dry_run: bool = False,
        auto_approve: bool = False,
        regions: Optional[List[str]] = None,
    ) -> Dict:
        """Run comprehensive cleanup of all orphaned resources.

        Args:
            dry_run: If True, only report what would be cleaned up
            auto_approve: If True, skip confirmation prompts
            regions: List of regions to check for stacks (default: ``IDP_REGIONS``)

        Returns:
            Dict with results for each resource type
        """
        console.print(
            "[bold blue]Starting IDP orphaned resource cleanup...[/bold blue]"
        )
        console.print(f"Account: {self.account_id}")
        console.print(f"Primary Region: {self.region}")
        if dry_run:
            console.print("[yellow]DRY RUN - No changes will be made[/yellow]")
        if auto_approve:
            console.print(
                "[yellow]AUTO-APPROVE - Confirmations will be skipped[/yellow]"
            )
        console.print()

        # Discover IDP stacks across specified regions
        self.discover_idp_stacks(regions=regions)

        if not self._deleted_stacks:
            console.print(
                "[green]No deleted IDP stacks found - nothing to clean up![/green]"
            )
            return {}

        console.print("[bold]Deleted IDP stacks found:[/bold]")
        for stack_name, info in self._deleted_stacks.items():
            console.print(f"  - {stack_name} ({info['region']})")
        console.print()

        results = {
            "cloudfront_distributions": self.cleanup_cloudfront_distributions(
                dry_run, auto_approve
            ),
            "log_groups": self.cleanup_log_groups(dry_run, auto_approve),
            "appsync_apis": self.cleanup_appsync_apis(dry_run, auto_approve),
            "cloudfront_policies": self.cleanup_cloudfront_policies(
                dry_run, auto_approve
            ),
            "iam_policies": self.cleanup_iam_policies(dry_run, auto_approve),
            "logs_resource_policies": self.cleanup_logs_resource_policies(
                dry_run, auto_approve
            ),
            "s3_buckets": self.cleanup_s3_buckets(dry_run, auto_approve),
            "dynamodb_tables": self.cleanup_dynamodb_tables(dry_run, auto_approve),
        }

        return results
