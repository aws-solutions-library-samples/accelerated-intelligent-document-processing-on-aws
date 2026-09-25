# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test Studio operations processor."""

import json
import logging
import time
from typing import Dict, List, Optional

import boto3

from idp_sdk._core.stack_info import StackInfo
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError

logger = logging.getLogger(__name__)

#: Configuration keys that differ between two runs for reasons nobody comparing
#: their scores wants to read about. The four metadata fields move on every save;
#: `Configuration` is the table's own key attribute; `version_name` names the
#: profile, which the run ids already say; and `classes` is the class schema, whose
#: every prompt and description would otherwise fill the difference table and bury
#: the model or threshold change that is the reason to look. Same set the Test
#: Studio comparison view in the web UI hides, so the two agree about what "no
#: differences" means.
_CONFIG_KEYS_NOT_COMPARED = frozenset(
    {
        "UpdatedAt",
        "Description",
        "CreatedAt",
        "IsActive",
        "Configuration",
        "version_name",
        "classes",
    }
)


def _config_leaf_paths(value, prefix: str = "") -> List[str]:
    """Every dotted path to a scalar inside a captured configuration.

    A list contributes one indexed path per element, so a reordered or lengthened
    list shows up as differences at the indices that moved rather than as one
    opaque "the list changed".
    """
    paths: List[str] = []

    if isinstance(value, dict):
        for key, child in value.items():
            if key in _CONFIG_KEYS_NOT_COMPARED:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_config_leaf_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_config_leaf_paths(child, f"{prefix}.{index}"))
    else:
        if prefix:
            paths.append(prefix)

    return paths


def _config_value_at(value, path: str):
    """Follow a dotted path produced by `_config_leaf_paths`, or return `None`."""
    current = value
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isdigit():
            index = int(key)
            if not 0 <= index < len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def configuration_differences(configs: List[Dict]) -> Optional[List[Dict]]:
    """Compare the configurations two or more test runs captured.

    Each run records the configuration it ran under, and `getTestRun` returns it,
    so the comparison needs no extra call: it is the most useful thing to know when
    two runs score differently and it was already on the wire.

    Args:
        configs: One `{"testRunId": str, "config": dict}` per run that captured a
            configuration. The captured object wraps the body under `Config`, the
            same shape the configuration table stores.

    Returns:
        `None` when fewer than two runs captured a configuration, so a caller can
        say that rather than claiming the configurations matched — the distinction
        matters, because "compared and identical" and "never compared" are the same
        empty table. Otherwise one `{"setting": <dotted path>, "values": {<run id>:
        <string>}}` per path whose value is not the same in every run, ordered by
        path. A path absent from one run reads `<missing>` for that run and counts
        as a difference.
    """
    if not configs or len(configs) < 2:
        return None

    bodies = {
        entry["testRunId"]: (entry.get("config") or {}).get("Config", {})
        for entry in configs
    }

    paths = set()
    for body in bodies.values():
        paths.update(_config_leaf_paths(body))

    differences: List[Dict] = []
    for path in sorted(paths):
        values = {}
        for test_run_id, body in bodies.items():
            value = _config_value_at(body, path)
            values[test_run_id] = "<missing>" if value is None else str(value).strip()

        if len(set(values.values())) > 1:
            differences.append({"setting": path, "values": values})

    return differences


class TestStudioProcessor:
    """Processes Test Studio operations (test result retrieval and comparison)."""

    # Tell pytest not to collect this class as a test (name starts with "Test").
    __test__ = False

    def __init__(self, stack_name: str, region: Optional[str] = None):
        """Initialize Test Studio processor.

        Args:
            stack_name: CloudFormation stack name
            region: AWS region (defaults to session region)
        """
        self.stack_info = StackInfo(stack_name=stack_name, region=region)
        self.lambda_client = boto3.client("lambda", region_name=self.stack_info.region)
        self._resolver_arn = None

    def _get_api_resolver_stack_output(self, output_key: str) -> Optional[str]:
        """Get an output from the nested API resolver stack.

        The stack's logical id is APIRESOLVERSTACK on current templates and
        was APPSYNCSTACK before the AppSync removal — try both so the SDK
        keeps working against stacks deployed from older templates.

        Raises:
            ValueError: If neither nested stack (or the output) is found.
        """
        try:
            return self.stack_info.get_nested_stack_output(
                nested_stack_pattern="apiresolver",
                output_key=output_key,
            )
        except ValueError:
            return self.stack_info.get_nested_stack_output(
                nested_stack_pattern="appsync",
                output_key=output_key,
            )

    def _get_resolver_function_arn(self) -> str:
        """Get TestResultsResolverFunction ARN from nested AppSync stack outputs.

        Returns:
            Lambda function ARN

        Raises:
            IDPResourceNotFoundError: If function not found in stack
        """
        if self._resolver_arn:
            return self._resolver_arn

        try:
            # TestResultsResolverFunctionArn is in the nested API resolver
            # stack (APIRESOLVERSTACK), not the main stack
            resolver_arn = self._get_api_resolver_stack_output(
                "TestResultsResolverFunctionArn"
            )

            if not resolver_arn:
                raise IDPResourceNotFoundError(
                    "TestResultsResolverFunctionArn not found in nested API resolver stack. "
                    "Ensure Test Studio is enabled in your stack."
                )

            self._resolver_arn = resolver_arn
            logger.debug(f"Found TestResultsResolverFunction: {resolver_arn}")
            return resolver_arn

        except ValueError as e:
            # Convert ValueError from get_nested_stack_output to IDPResourceNotFoundError
            raise IDPResourceNotFoundError(
                f"Failed to get TestResultsResolverFunction ARN: {e}. "
                "Ensure Test Studio is enabled in your stack."
            ) from e
        except Exception as e:
            raise IDPResourceNotFoundError(
                f"Failed to get TestResultsResolverFunction ARN: {e}"
            ) from e

    def get_test_run_status(self, test_run_id: str) -> str:
        """Get current status of a test run.

        Args:
            test_run_id: Test run identifier

        Returns:
            Status string (QUEUED, RUNNING, EVALUATING, COMPLETE, PARTIAL_COMPLETE, FAILED, CANCELED)

        Raises:
            IDPProcessingError: If status check fails
        """
        resolver_arn = self._get_resolver_function_arn()

        try:
            payload = {
                "info": {"fieldName": "getTestRunStatus"},
                "arguments": {"testRunId": test_run_id},
            }

            response = self.lambda_client.invoke(
                FunctionName=resolver_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )

            result = json.loads(response["Payload"].read())

            if "errorMessage" in result:
                raise IDPProcessingError(
                    f"Failed to get test run status: {result['errorMessage']}"
                )

            status = result.get("status", "UNKNOWN")
            logger.debug(f"Test run {test_run_id} status: {status}")
            return status

        except Exception as e:
            raise IDPProcessingError(f"Failed to get test run status: {e}") from e

    def get_test_result(
        self,
        test_run_id: str,
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Dict:
        """Get test result for a test run.

        Args:
            test_run_id: Test run identifier
            wait: Wait for test run to complete if still in progress
            timeout: Maximum wait time in seconds (default: 300)
            poll_interval: Polling interval in seconds (default: 5)

        Returns:
            Dictionary with test result data

        Raises:
            IDPProcessingError: If retrieval fails or timeout occurs
        """
        resolver_arn = self._get_resolver_function_arn()

        # Wait for completion if requested
        if wait:
            logger.info(f"Waiting for test run {test_run_id} to complete...")
            start_time = time.time()

            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    raise IDPProcessingError(
                        f"Timeout waiting for test run {test_run_id} to complete after {timeout}s"
                    )

                status = self.get_test_run_status(test_run_id)

                # Final states
                if status in ["COMPLETE", "PARTIAL_COMPLETE", "FAILED", "CANCELED"]:
                    logger.info(
                        f"Test run {test_run_id} finished with status: {status}"
                    )
                    break

                # Still in progress
                logger.debug(
                    f"Test run {test_run_id} still in progress (status: {status}), "
                    f"elapsed: {elapsed:.0f}s"
                )
                time.sleep(poll_interval)

        # Get full test results
        try:
            payload = {
                "info": {"fieldName": "getTestRun"},
                "arguments": {"testRunId": test_run_id},
            }

            response = self.lambda_client.invoke(
                FunctionName=resolver_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )

            result = json.loads(response["Payload"].read())

            if "errorMessage" in result:
                error_msg = result["errorMessage"]
                if "evaluating" in error_msg.lower():
                    raise IDPProcessingError(
                        f"Test run {test_run_id} is still evaluating. Use wait=True to wait for completion."
                    )
                raise IDPProcessingError(f"Failed to get test result: {error_msg}")

            logger.info(f"Retrieved test result for {test_run_id}")
            return result

        except Exception as e:
            raise IDPProcessingError(f"Failed to get test result: {e}") from e

    def compare_test_runs(self, test_run_ids: List[str]) -> Dict:
        """Compare multiple test runs.

        Args:
            test_run_ids: List of test run identifiers to compare

        Returns:
            Dictionary with `metrics` per test run and `configs`, the differences
            between the configurations the runs captured — `None` when fewer than
            two of them recorded one, which is not the same answer as "identical".

        Raises:
            IDPProcessingError: If comparison fails
        """
        if len(test_run_ids) < 2:
            raise ValueError("At least 2 test run IDs required for comparison")

        resolver_arn = self._get_resolver_function_arn()

        try:
            # Fetch all test runs
            metrics = {}
            # Each run records the configuration it ran under and `getTestRun`
            # returns it, so comparing them needs no second call. Only the runs
            # that actually captured one go in: a run whose evaluation aggregate
            # has not been written yet returns no `config` key at all, and
            # treating that as an empty configuration would report every setting
            # the other run has as a difference.
            captured_configs = []
            for test_run_id in test_run_ids:
                payload = {
                    "info": {"fieldName": "getTestRun"},
                    "arguments": {"testRunId": test_run_id},
                }

                response = self.lambda_client.invoke(
                    FunctionName=resolver_arn,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(payload),
                )

                result = json.loads(response["Payload"].read())

                if "errorMessage" in result:
                    logger.warning(
                        f"Failed to get test run {test_run_id}: {result['errorMessage']}"
                    )
                    continue

                # Extract key metrics
                metrics[test_run_id] = {
                    "testRunId": result.get("testRunId"),
                    "testSetName": result.get("testSetName"),
                    "status": result.get("status"),
                    "filesCount": result.get("filesCount", 0),
                    "completedFiles": result.get("completedFiles", 0),
                    "failedFiles": result.get("failedFiles", 0),
                    "overallAccuracy": result.get("overallAccuracy"),
                    "accuracyBreakdown": result.get("accuracyBreakdown", {}),
                    "totalCost": result.get("totalCost", 0.0),
                    "createdAt": result.get("createdAt"),
                    "completedAt": result.get("completedAt"),
                }

                captured = result.get("config")
                if captured:
                    captured_configs.append(
                        {"testRunId": test_run_id, "config": captured}
                    )

            if not metrics:
                raise IDPProcessingError(
                    "No test runs could be retrieved for comparison"
                )

            logger.info(
                f"Compared {len(metrics)} test runs "
                f"({len(captured_configs)} captured a configuration)"
            )
            return {
                "metrics": metrics,
                "configs": configuration_differences(captured_configs),
            }

        except Exception as e:
            raise IDPProcessingError(f"Failed to compare test runs: {e}") from e

    def abort_test_runs(self, test_run_ids: List[str]) -> Dict:
        """Abort one or more test runs.

        Args:
            test_run_ids: List of test run identifiers to abort

        Returns:
            Dictionary with abort results including counts and errors

        Raises:
            IDPProcessingError: If abort operation fails
        """
        # Get AbortTestRunsResolverFunction ARN from nested API resolver stack.
        # get_nested_stack_output raises ValueError when the stack or output
        # is missing (the previous `except IDPResourceNotFoundError` never
        # matched, so the friendly message below was dead code).
        try:
            abort_function_arn = self._get_api_resolver_stack_output(
                "AbortTestRunsResolverFunctionArn"
            )
        except ValueError as e:
            raise IDPResourceNotFoundError(
                "AbortTestRunsResolverFunction not found. "
                "Ensure you are using a stack version that supports test run abort."
            ) from e

        try:
            # Invoke the abort resolver
            payload = {
                "info": {"fieldName": "abortTestRuns"},
                "arguments": {"testRunIds": test_run_ids},
            }

            response = self.lambda_client.invoke(
                FunctionName=abort_function_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )

            result = json.loads(response["Payload"].read())

            if "errorMessage" in result:
                raise IDPProcessingError(f"Abort failed: {result['errorMessage']}")

            logger.info(
                f"Aborted {result.get('abortedCount', 0)} test runs, "
                f"{result.get('failedCount', 0)} failed"
            )
            return result

        except Exception as e:
            raise IDPProcessingError(f"Failed to abort test runs: {e}") from e
