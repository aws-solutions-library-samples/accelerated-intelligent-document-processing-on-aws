# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Testing operations for IDP SDK."""

from typing import List, Optional

from idp_sdk._core.naming import resolve_config_profile
from idp_sdk.exceptions import IDPProcessingError
from idp_sdk.models import LoadTestResult, TestComparisonResult, TestRunResult


class TestingOperation:
    """Load testing and workflow control operations."""

    # Tell pytest not to collect this class as a test (name starts with "Test").
    __test__ = False

    def __init__(self, client):
        self._client = client
        self._processor = None
        self._processor_cache = {}

    def _get_processor(self, stack_name=None):
        """Get or create a cached TestStudioProcessor.

        Args:
            stack_name: Optional stack name override

        Returns:
            TestStudioProcessor instance
        """
        name = stack_name or self._client._require_stack()
        if name not in self._processor_cache:
            from idp_sdk._core.test_studio_processor import TestStudioProcessor

            self._processor_cache[name] = TestStudioProcessor(
                stack_name=name, region=self._client._region
            )
        if not stack_name:
            self._processor = self._processor_cache[name]
        return self._processor_cache[name]

    def load_test(
        self,
        source_file: str,
        stack_name: Optional[str] = None,
        rate: int = 100,
        duration: int = 1,
        schedule_file: Optional[str] = None,
        dest_prefix: str = "load-test",
        config_version: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> LoadTestResult:
        """Run load test by copying files to input bucket.

        Args:
            source_file: Source file to copy
            stack_name: Optional stack name override
            rate: Files per minute for constant load
            duration: Duration in minutes
            schedule_file: Optional schedule file for variable load
            dest_prefix: Destination prefix in S3
            config_version: Configuration profile to tag files with (optional).
                           If provided, files will be tagged with the specified version.
                           If not provided, the active configuration version is used.
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            **kwargs: Additional parameters

        Returns:
            LoadTestResult with test statistics
        """
        config_version = resolve_config_profile(config_profile, config_version)
        from idp_sdk._core.load_test import LoadTester

        name = self._client._require_stack(stack_name)
        tester = LoadTester(stack_name=name, region=self._client._region)

        try:
            if schedule_file:
                result = tester.run_scheduled_load(
                    source_file=source_file,
                    schedule_file=schedule_file,
                    dest_prefix=dest_prefix,
                    config_version=config_version,
                )
            else:
                result = tester.run_constant_load(
                    source_file=source_file,
                    rate=rate,
                    duration=duration,
                    dest_prefix=dest_prefix,
                    config_version=config_version,
                )

            return LoadTestResult(
                success=result.get("success", False),
                total_files=result.get("total_files", 0),
                duration_minutes=duration,
                error=result.get("error"),
            )
        except Exception as e:
            return LoadTestResult(
                success=False, total_files=0, duration_minutes=duration, error=str(e)
            )

    def get_test_result(
        self,
        test_run_id: str,
        stack_name: Optional[str] = None,
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        **kwargs,
    ) -> TestRunResult:
        """Get Test Studio evaluation result for a test run.

        Args:
            test_run_id: Test run identifier
            stack_name: Optional stack name override
            wait: Wait for test run to complete if still in progress (default: False)
            timeout: Maximum wait time in seconds (default: 300)
            poll_interval: Polling interval in seconds (default: 5)
            **kwargs: Additional parameters

        Returns:
            TestRunResult with evaluation metrics and details

        Raises:
            IDPProcessingError: If retrieval fails or timeout occurs
        """
        processor = self._get_processor(stack_name)

        try:
            result = processor.get_test_result(
                test_run_id=test_run_id,
                wait=wait,
                timeout=timeout,
                poll_interval=poll_interval,
            )

            # Map to TestRunResult model
            return TestRunResult(
                test_run_id=result.get("testRunId", test_run_id),
                test_set_name=result.get("testSetName", ""),
                status=result.get("status", "UNKNOWN"),
                files_count=result.get("filesCount", 0),
                completed_files=result.get("completedFiles", 0),
                failed_files=result.get("failedFiles", 0),
                overall_accuracy=result.get("overallAccuracy"),
                accuracy_breakdown=result.get("accuracyBreakdown"),
                total_cost=result.get("totalCost"),
                created_at=result.get("createdAt"),
                completed_at=result.get("completedAt"),
                raw_data=result,
            )

        except Exception as e:
            raise IDPProcessingError(f"Failed to get test result: {e}") from e

    def compare_test_runs(
        self,
        test_run_ids: List[str],
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> TestComparisonResult:
        """Compare multiple Test Studio evaluation runs.

        Args:
            test_run_ids: List of test run identifiers to compare
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            TestComparisonResult with metrics for each test run

        Raises:
            IDPProcessingError: If comparison fails
        """
        processor = self._get_processor(stack_name)

        try:
            result = processor.compare_test_runs(test_run_ids=test_run_ids)

            return TestComparisonResult(
                metrics=result.get("metrics", {}),
                comparison_summary=result.get("comparison_summary"),
            )

        except Exception as e:
            raise IDPProcessingError(f"Failed to compare test runs: {e}") from e

    def abort_test_run(
        self,
        test_run_ids: List[str],
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """Abort one or more Test Studio runs.

        Stops all running document workflows and updates test run status to ABORTED.
        Only test runs with status QUEUED or RUNNING can be aborted.

        Args:
            test_run_ids: List of test run identifiers to abort
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dict with:
                - success: Whether any test runs were aborted
                - message: Human-readable summary
                - aborted_count: Number of test runs successfully aborted
                - failed_count: Number of test runs that failed to abort
                - errors: List of error messages (if any)

        Raises:
            IDPProcessingError: If abort operation fails
        """
        processor = self._get_processor(stack_name)

        try:
            result = processor.abort_test_runs(test_run_ids=test_run_ids)
            return result

        except Exception as e:
            raise IDPProcessingError(f"Failed to abort test runs: {e}") from e
