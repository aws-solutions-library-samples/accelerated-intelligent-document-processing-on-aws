# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Testing and workflow control models."""

from typing import Optional

from pydantic import BaseModel, Field


class ExecutionsStoppedResult(BaseModel):
    """Result details for stopped Step Functions executions."""

    total_stopped: int = Field(default=0, description="Number of executions stopped")
    total_failed: int = Field(
        default=0, description="Number of executions that failed to stop"
    )
    remaining: int = Field(
        default=0, description="Number of executions still running after stop"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if stopping failed"
    )


class DocumentsAbortedResult(BaseModel):
    """Result details for documents set to ABORTED state."""

    documents_aborted: int = Field(
        default=0, description="Number of queued documents set to ABORTED"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if abort operation failed"
    )


class StopWorkflowsResult(BaseModel):
    """Result of stopping workflows."""

    executions_stopped: Optional[ExecutionsStoppedResult] = Field(
        default=None, description="Details of stopped Step Functions executions"
    )
    documents_aborted: Optional[DocumentsAbortedResult] = Field(
        default=None, description="Details of documents set to ABORTED state"
    )
    queue_purged: bool = Field(default=False, description="Whether queue was purged")


class LoadTestResult(BaseModel):
    """Result of a load test."""

    success: bool = Field(description="Whether load test completed")
    total_files: int = Field(description="Total files submitted")
    duration_minutes: int = Field(description="Test duration in minutes")
    error: Optional[str] = Field(default=None, description="Error if failed")


class TestRunResult(BaseModel):
    """Result of a Test Studio evaluation run."""

    # Tell pytest not to collect this class as a test (name starts with "Test").
    __test__ = False

    test_run_id: str = Field(description="Test run identifier")
    test_set_name: str = Field(description="Test set name")
    status: str = Field(
        description="Test run status (COMPLETE, PARTIAL_COMPLETE, FAILED, etc.)"
    )
    files_count: int = Field(description="Total number of files")
    completed_files: int = Field(description="Number of completed files")
    failed_files: int = Field(default=0, description="Number of failed files")
    overall_accuracy: Optional[float] = Field(
        default=None, description="Overall accuracy (0-1)"
    )
    accuracy_breakdown: Optional[dict] = Field(
        default=None, description="Precision, recall, F1 scores"
    )
    total_cost: Optional[float] = Field(
        default=None, description="Total cost in dollars"
    )
    created_at: Optional[str] = Field(default=None, description="Creation timestamp")
    completed_at: Optional[str] = Field(
        default=None, description="Completion timestamp"
    )
    raw_data: Optional[dict] = Field(default=None, description="Full raw response data")


class TestComparisonResult(BaseModel):
    """Result of comparing multiple test runs."""

    # Tell pytest not to collect this class as a test (name starts with "Test").
    __test__ = False

    metrics: dict = Field(description="Metrics for each test run ID")
    comparison_summary: Optional[dict] = Field(
        default=None, description="Summary comparison data"
    )
    # `None` and `[]` are different answers and a caller must be able to tell them
    # apart: `None` means fewer than two of the runs recorded a configuration, so
    # nothing was compared, while `[]` means the configurations were compared and
    # matched. Reporting both as "no differences" is a claim about the
    # configurations that was never checked.
    configs: Optional[list] = Field(
        default=None,
        description=(
            "Differences between the configurations the runs captured, one "
            "{'setting': <dotted path>, 'values': {<test run id>: <value>}} per "
            "differing setting; [] when they are identical and None when fewer "
            "than two runs captured a configuration to compare"
        ),
    )
