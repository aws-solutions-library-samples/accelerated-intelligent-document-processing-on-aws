# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stack-related models."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class StackDeploymentResult(BaseModel):
    """Result of a stack deployment operation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool = Field(description="Whether the operation succeeded")
    operation: str = Field(description="Type of operation (CREATE, UPDATE)")
    status: str = Field(description="Final stack status")
    stack_name: str = Field(description="CloudFormation stack name")
    stack_id: Optional[str] = Field(default=None, description="CloudFormation stack ID")
    outputs: Dict[str, str] = Field(
        default_factory=dict, description="Stack outputs (URLs, bucket names, etc.)"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")
    deploy_start_time: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when deployment was initiated (for filtering stale events)",
    )


class StackDeletionResult(BaseModel):
    """Result of a stack deletion operation."""

    success: bool = Field(description="Whether the deletion succeeded")
    status: str = Field(description="Final status")
    stack_name: str = Field(description="CloudFormation stack name")
    stack_id: Optional[str] = Field(default=None, description="CloudFormation stack ID")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    cleanup_result: Optional[Dict[str, Any]] = Field(
        default=None, description="Results of force-delete cleanup phase"
    )
    retained_resources: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Resources a Terraform delete left in place because CloudFormation "
            "would retain them, categorized as dynamodb_tables, log_groups, "
            "s3_buckets and other"
        ),
    )


class StackOperationInProgress(BaseModel):
    """Describes a CloudFormation operation currently in progress."""

    operation: str = Field(description="Operation type: CREATE, UPDATE, or DELETE")
    status: str = Field(
        description="Current CloudFormation stack status (e.g. UPDATE_IN_PROGRESS)"
    )


class StackMonitorResult(BaseModel):
    """Result of monitoring a CloudFormation stack operation to completion."""

    success: bool = Field(description="Whether the operation completed successfully")
    operation: str = Field(description="Operation type: CREATE, UPDATE, or DELETE")
    status: str = Field(description="Final CloudFormation stack status")
    stack_name: str = Field(description="Stack name")
    outputs: Dict[str, str] = Field(
        default_factory=dict,
        description="Stack outputs (only populated on successful CREATE/UPDATE)",
    )
    error: Optional[str] = Field(
        default=None,
        description="Failure reason string (only populated on failure)",
    )


class StackStableStateResult(BaseModel):
    """Result of waiting for a stack to reach a stable (non-transitional) state."""

    success: bool = Field(
        description="Whether a stable state was reached within timeout"
    )
    status: str = Field(
        description="Final CloudFormation status (or TIMEOUT if timed out)"
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable message about the outcome",
    )


class FailureCause(BaseModel):
    """A single root-cause failure from a CloudFormation deployment."""

    resource: str = Field(description="CloudFormation logical resource ID")
    resource_type: str = Field(
        default="",
        description="CloudFormation resource type (e.g. AWS::Lambda::Function)",
    )
    reason: str = Field(description="CloudFormation failure reason string")
    status: str = Field(description="Resource status (e.g. CREATE_FAILED)")
    physical_id: str = Field(
        default="", description="Physical resource ID if available"
    )
    stack: str = Field(description="Stack name containing this failure")
    stack_path: str = Field(
        default="",
        description="Nested stack path (e.g. 'NestedStack1 → NestedStack2')",
    )
    is_cascade: bool = Field(
        default=False,
        description="True if this failure was caused by another failure (not the root cause)",
    )


class FailureAnalysis(BaseModel):
    """Complete failure analysis for a CloudFormation deployment."""

    stack_name: str = Field(description="Top-level stack name")
    root_causes: List[FailureCause] = Field(
        default_factory=list,
        description="Actual root cause failures (actionable errors, excludes cascades)",
    )
    all_failures: List[FailureCause] = Field(
        default_factory=list,
        description="All failed events across main stack and all nested stacks",
    )

    @property
    def cascade_count(self) -> int:
        """Number of cascade/secondary failures (not root causes)."""
        return sum(1 for f in self.all_failures if f.is_cascade)


class BucketInfo(BaseModel):
    """Information about an S3 bucket associated with a CloudFormation stack."""

    logical_id: str = Field(description="CloudFormation logical resource ID")
    bucket_name: str = Field(description="S3 bucket name")
    object_count: int = Field(default=0, description="Number of objects in bucket")
    total_size: int = Field(default=0, description="Total size in bytes")
    size_display: str = Field(
        default="Unknown", description="Human-readable size (e.g. '12.5 MB')"
    )


class CancelUpdateResult(BaseModel):
    """Result of attempting to cancel a stack update."""

    success: bool = Field(
        description="Whether the cancellation was successfully initiated"
    )
    message: Optional[str] = Field(default=None, description="Status message")
    error: Optional[str] = Field(
        default=None, description="Error if cancellation failed"
    )


class OrphanedResourceCleanupResult(BaseModel):
    """Result of cleaning up orphaned AWS resources from deleted IDP stacks."""

    results: Dict[str, Any] = Field(
        default_factory=dict,
        description="Per-resource-type cleanup results (deleted, disabled, updated, skipped, errors lists)",
    )
    has_errors: bool = Field(
        default=False,
        description="True if any resource type encountered errors",
    )
    has_disabled: bool = Field(
        default=False,
        description="True if any CloudFront distributions were disabled (require re-run after ~15 min)",
    )


class StackResources(BaseModel):
    """Stack resources discovered from CloudFormation."""

    input_bucket: str = Field(alias="InputBucket", description="S3 input bucket name")
    output_bucket: str = Field(
        alias="OutputBucket", description="S3 output bucket name"
    )
    configuration_bucket: Optional[str] = Field(
        alias="ConfigurationBucket", default=None, description="Configuration bucket"
    )
    evaluation_baseline_bucket: Optional[str] = Field(
        alias="EvaluationBaselineBucket", default=None, description="Baseline bucket"
    )
    test_set_bucket: Optional[str] = Field(
        alias="TestSetBucket", default=None, description="Test set bucket"
    )
    document_queue_url: Optional[str] = Field(
        alias="DocumentQueueUrl", default=None, description="SQS queue URL"
    )
    state_machine_arn: Optional[str] = Field(
        alias="StateMachineArn", default=None, description="Step Functions ARN"
    )
    documents_table: Optional[str] = Field(
        alias="DocumentsTable", default=None, description="DynamoDB tracking table"
    )

    model_config = ConfigDict(populate_by_name=True)
