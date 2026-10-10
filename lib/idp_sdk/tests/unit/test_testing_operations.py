# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Testing operations (mocked).
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.models.testing import TestComparisonResult, TestRunResult
from idp_sdk.operations.testing import TestingOperation


@pytest.mark.unit
class TestTestingModels:
    """Test TestRunResult and TestComparisonResult model dataclasses."""

    def test_test_run_result_minimal(self):
        """Test TestRunResult with minimal required fields."""
        result = TestRunResult(
            test_run_id="fake-w2-20260410-123456",
            test_set_name="fake-w2",
            status="COMPLETE",
            files_count=100,
            completed_files=95,
            failed_files=5,
        )
        assert result.test_run_id == "fake-w2-20260410-123456"
        assert result.test_set_name == "fake-w2"
        assert result.status == "COMPLETE"
        assert result.files_count == 100
        assert result.completed_files == 95
        assert result.failed_files == 5
        assert result.overall_accuracy is None
        assert result.accuracy_breakdown is None
        assert result.total_cost is None
        assert result.raw_data is None

    def test_test_run_result_full(self):
        """Test TestRunResult with all fields populated."""
        result = TestRunResult(
            test_run_id="fake-w2-20260410-123456",
            test_set_name="fake-w2",
            status="COMPLETE",
            files_count=100,
            completed_files=100,
            failed_files=0,
            overall_accuracy=0.95,
            accuracy_breakdown={
                "precision": 0.96,
                "recall": 0.94,
                "f1_score": 0.95,
            },
            total_cost=12.50,
            created_at="2026-04-10T12:00:00Z",
            completed_at="2026-04-10T12:30:00Z",
            raw_data={"extra": "data"},
        )
        assert result.overall_accuracy == 0.95
        assert result.accuracy_breakdown["precision"] == 0.96
        assert result.total_cost == 12.50
        assert result.created_at == "2026-04-10T12:00:00Z"
        assert result.raw_data == {"extra": "data"}

    def test_test_comparison_result(self):
        """Test TestComparisonResult model."""
        result = TestComparisonResult(
            metrics={
                "run1": {"overallAccuracy": 0.95},
                "run2": {"overallAccuracy": 0.92},
            },
            comparison_summary={"winner": "run1"},
        )
        assert len(result.metrics) == 2
        assert result.metrics["run1"]["overallAccuracy"] == 0.95
        assert result.comparison_summary["winner"] == "run1"

    def test_test_comparison_result_minimal(self):
        """Test TestComparisonResult with minimal fields."""
        result = TestComparisonResult(metrics={})
        assert result.metrics == {}
        assert result.comparison_summary is None


@pytest.mark.unit
class TestTestingOperationInit:
    """Test TestingOperation initialization."""

    def test_client_has_testing_operation(self):
        """Test that IDPClient initializes TestingOperation."""
        client = IDPClient(stack_name="test-stack")
        assert hasattr(client, "testing")
        assert isinstance(client.testing, TestingOperation)

    def test_testing_requires_stack(self):
        """Test that TestingOperation requires a stack name."""
        from idp_sdk.exceptions import IDPConfigurationError

        client = IDPClient()
        with pytest.raises(IDPConfigurationError):
            client.testing._get_processor()

    def test_processor_caching(self):
        """Test that processor is cached per stack name."""
        client = IDPClient(stack_name="test-stack")

        with patch(
            "idp_sdk._core.test_studio_processor.TestStudioProcessor"
        ) as mock_proc_cls:
            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc

            # First call creates processor
            proc1 = client.testing._get_processor()
            assert proc1 is mock_proc
            assert mock_proc_cls.call_count == 1

            # Second call returns cached processor
            proc2 = client.testing._get_processor()
            assert proc2 is mock_proc
            assert mock_proc_cls.call_count == 1  # Not called again

            # Different stack creates new processor
            proc3 = client.testing._get_processor(stack_name="other-stack")
            assert proc3 is mock_proc
            assert mock_proc_cls.call_count == 2

    def test_processor_cache_per_stack(self):
        """Test that each stack has its own cached processor."""
        client = IDPClient(stack_name="stack-a")

        with patch(
            "idp_sdk._core.test_studio_processor.TestStudioProcessor"
        ) as mock_proc_cls:
            mock_proc_a = MagicMock(name="proc-a")
            mock_proc_b = MagicMock(name="proc-b")
            mock_proc_cls.side_effect = [mock_proc_a, mock_proc_b]

            proc_a = client.testing._get_processor("stack-a")
            proc_b = client.testing._get_processor("stack-b")

            assert proc_a is mock_proc_a
            assert proc_b is mock_proc_b
            assert proc_a is not proc_b

            # Retrieve cached instances
            proc_a2 = client.testing._get_processor("stack-a")
            assert proc_a2 is mock_proc_a


@pytest.mark.unit
class TestGetTestResult:
    """Test TestingOperation.get_test_result with mocked processor."""

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_success(self, mock_processor_cls):
        """Test successful test result retrieval."""
        mock_processor = MagicMock()
        mock_processor.get_test_result.return_value = {
            "testRunId": "fake-w2-20260410-123456",
            "testSetName": "fake-w2",
            "status": "COMPLETE",
            "filesCount": 100,
            "completedFiles": 95,
            "failedFiles": 5,
            "overallAccuracy": 0.95,
            "accuracyBreakdown": {
                "precision": 0.96,
                "recall": 0.94,
                "f1_score": 0.95,
            },
            "totalCost": 12.50,
            "createdAt": "2026-04-10T12:00:00Z",
            "completedAt": "2026-04-10T12:30:00Z",
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.get_test_result(test_run_id="fake-w2-20260410-123456")

        assert isinstance(result, TestRunResult)
        assert result.test_run_id == "fake-w2-20260410-123456"
        assert result.test_set_name == "fake-w2"
        assert result.status == "COMPLETE"
        assert result.overall_accuracy == 0.95
        assert result.accuracy_breakdown["precision"] == 0.96
        assert result.total_cost == 12.50

        mock_processor.get_test_result.assert_called_once_with(
            test_run_id="fake-w2-20260410-123456",
            wait=False,
            timeout=300,
            poll_interval=5,
        )

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_with_wait(self, mock_processor_cls):
        """Test test result retrieval with wait flag."""
        mock_processor = MagicMock()
        mock_processor.get_test_result.return_value = {
            "testRunId": "test-run-1",
            "testSetName": "test-set",
            "status": "COMPLETE",
            "filesCount": 50,
            "completedFiles": 50,
            "failedFiles": 0,
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.get_test_result(
            test_run_id="test-run-1",
            wait=True,
            timeout=600,
            poll_interval=10,
        )

        assert result.test_run_id == "test-run-1"
        mock_processor.get_test_result.assert_called_once_with(
            test_run_id="test-run-1",
            wait=True,
            timeout=600,
            poll_interval=10,
        )

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_with_raw_data(self, mock_processor_cls):
        """Test that raw_data is preserved in result."""
        raw_data = {
            "testRunId": "test-run-1",
            "testSetName": "test-set",
            "status": "COMPLETE",
            "filesCount": 10,
            "completedFiles": 10,
            "failedFiles": 0,
            "customField": "custom-value",
        }
        mock_processor = MagicMock()
        mock_processor.get_test_result.return_value = raw_data
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.get_test_result(test_run_id="test-run-1")

        assert result.raw_data == raw_data
        assert result.raw_data["customField"] == "custom-value"

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_error_wraps_exception(self, mock_processor_cls):
        """Test that processor errors are wrapped in IDPProcessingError."""
        from idp_sdk.exceptions import IDPProcessingError

        mock_processor = MagicMock()
        mock_processor.get_test_result.side_effect = RuntimeError(
            "Lambda invocation failed"
        )
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")

        with pytest.raises(IDPProcessingError, match="Failed to get test result"):
            client.testing.get_test_result(test_run_id="test-run-1")

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_uses_cached_processor(self, mock_processor_cls):
        """Test that multiple calls use cached processor."""
        mock_processor = MagicMock()
        mock_processor.get_test_result.return_value = {
            "testRunId": "run-1",
            "testSetName": "set-1",
            "status": "COMPLETE",
            "filesCount": 10,
            "completedFiles": 10,
            "failedFiles": 0,
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")

        # First call
        client.testing.get_test_result(test_run_id="run-1")
        assert mock_processor_cls.call_count == 1

        # Second call should reuse processor
        client.testing.get_test_result(test_run_id="run-2")
        assert mock_processor_cls.call_count == 1  # Not called again


@pytest.mark.unit
class TestCompareTestRuns:
    """Test TestingOperation.compare_test_runs with mocked processor."""

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_success(self, mock_processor_cls):
        """Test successful test run comparison."""
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {
                "run-1": {
                    "testRunId": "run-1",
                    "testSetName": "set-1",
                    "status": "COMPLETE",
                    "overallAccuracy": 0.95,
                    "totalCost": 10.0,
                },
                "run-2": {
                    "testRunId": "run-2",
                    "testSetName": "set-1",
                    "status": "COMPLETE",
                    "overallAccuracy": 0.92,
                    "totalCost": 8.5,
                },
            }
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.compare_test_runs(test_run_ids=["run-1", "run-2"])

        assert isinstance(result, TestComparisonResult)
        assert len(result.metrics) == 2
        assert result.metrics["run-1"]["overallAccuracy"] == 0.95
        assert result.metrics["run-2"]["overallAccuracy"] == 0.92

        mock_processor.compare_test_runs.assert_called_once_with(
            test_run_ids=["run-1", "run-2"]
        )

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_the_configuration_differences_reach_the_result_model(
        self, mock_processor_cls
    ):
        """`configs` is carried through, not dropped between processor and model.

        The processor computes it and the CLI renders it, so a silent drop here
        would put the CLI back to printing "no configuration differences" for every
        comparison — the defect this replaced — with the processor's own tests still
        green. Both answers are asserted in the same test: a populated list, and
        `None` for "fewer than two runs captured a configuration", which the model
        must not coerce to `[]`.
        """
        differences = [
            {
                "setting": "extraction.model",
                "values": {"run-1": "nova-lite", "run-2": "nova-pro"},
            }
        ]
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {"run-1": {}, "run-2": {}},
            "configs": differences,
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.compare_test_runs(test_run_ids=["run-1", "run-2"])

        assert result.configs == differences

        mock_processor.compare_test_runs.return_value = {
            "metrics": {"run-1": {}, "run-2": {}},
            "configs": None,
        }
        unanswered = client.testing.compare_test_runs(test_run_ids=["run-1", "run-2"])

        assert unanswered.configs is None

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_multiple(self, mock_processor_cls):
        """Test comparison with more than 2 test runs."""
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {
                "run-1": {"overallAccuracy": 0.95},
                "run-2": {"overallAccuracy": 0.92},
                "run-3": {"overallAccuracy": 0.97},
            }
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.compare_test_runs(
            test_run_ids=["run-1", "run-2", "run-3"]
        )

        assert len(result.metrics) == 3
        assert result.metrics["run-3"]["overallAccuracy"] == 0.97

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_with_summary(self, mock_processor_cls):
        """Test comparison result with comparison_summary field."""
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {
                "run-1": {"overallAccuracy": 0.95},
                "run-2": {"overallAccuracy": 0.92},
            },
            "comparison_summary": {
                "best_run": "run-1",
                "accuracy_delta": 0.03,
            },
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        result = client.testing.compare_test_runs(test_run_ids=["run-1", "run-2"])

        assert result.comparison_summary is not None
        assert result.comparison_summary["best_run"] == "run-1"

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_error_wraps_exception(self, mock_processor_cls):
        """Test that processor errors are wrapped in IDPProcessingError."""
        from idp_sdk.exceptions import IDPProcessingError

        mock_processor = MagicMock()
        mock_processor.compare_test_runs.side_effect = ValueError(
            "At least 2 test run IDs required"
        )
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")

        with pytest.raises(IDPProcessingError, match="Failed to compare test runs"):
            client.testing.compare_test_runs(test_run_ids=["run-1"])

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_uses_cached_processor(self, mock_processor_cls):
        """Test that multiple comparison calls use cached processor."""
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {
                "run-1": {"overallAccuracy": 0.95},
                "run-2": {"overallAccuracy": 0.92},
            }
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")

        # First call
        client.testing.compare_test_runs(test_run_ids=["run-1", "run-2"])
        assert mock_processor_cls.call_count == 1

        # Second call should reuse processor
        client.testing.compare_test_runs(test_run_ids=["run-3", "run-4"])
        assert mock_processor_cls.call_count == 1  # Not called again


@pytest.mark.unit
class TestTestingOperationStackOverride:
    """Test stack_name override functionality."""

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_get_test_result_with_stack_override(self, mock_processor_cls):
        """Test get_test_result with stack_name override."""
        mock_processor_a = MagicMock(name="proc-a")
        mock_processor_b = MagicMock(name="proc-b")
        mock_processor_a.get_test_result.return_value = {
            "testRunId": "run-1",
            "testSetName": "set-1",
            "status": "COMPLETE",
            "filesCount": 10,
            "completedFiles": 10,
            "failedFiles": 0,
        }
        mock_processor_b.get_test_result.return_value = {
            "testRunId": "run-2",
            "testSetName": "set-2",
            "status": "COMPLETE",
            "filesCount": 20,
            "completedFiles": 20,
            "failedFiles": 0,
        }
        mock_processor_cls.side_effect = [mock_processor_a, mock_processor_b]

        client = IDPClient(stack_name="stack-a")

        # Default stack
        result_a = client.testing.get_test_result(test_run_id="run-1")
        assert result_a.files_count == 10

        # Override to different stack
        result_b = client.testing.get_test_result(
            test_run_id="run-2", stack_name="stack-b"
        )
        assert result_b.files_count == 20

        # Verify different processors were used
        assert mock_processor_cls.call_count == 2
        mock_processor_cls.assert_any_call(stack_name="stack-a", region=None)
        mock_processor_cls.assert_any_call(stack_name="stack-b", region=None)

    @patch("idp_sdk._core.test_studio_processor.TestStudioProcessor")
    def test_compare_test_runs_with_stack_override(self, mock_processor_cls):
        """Test compare_test_runs with stack_name override."""
        mock_processor = MagicMock()
        mock_processor.compare_test_runs.return_value = {
            "metrics": {
                "run-1": {"overallAccuracy": 0.95},
                "run-2": {"overallAccuracy": 0.92},
            }
        }
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="stack-a")

        result = client.testing.compare_test_runs(
            test_run_ids=["run-1", "run-2"],
            stack_name="stack-b",
        )

        assert len(result.metrics) == 2
        mock_processor_cls.assert_called_with(stack_name="stack-b", region=None)
