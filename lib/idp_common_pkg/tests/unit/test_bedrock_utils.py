# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the bedrock_utils module.

Tests the async_exponential_backoff_retry and exponential_backoff_retry decorators,
including handling of ClientError, EventStreamError, and other exceptions.
"""

import asyncio
import re
from unittest.mock import patch

import botocore.exceptions
import pytest


class MockClientError(botocore.exceptions.ClientError):
    """Mock ClientError for testing that mimics botocore.exceptions.ClientError"""

    def __init__(self, error_response, operation_name):
        self.response = error_response
        self.operation_name = operation_name
        error = error_response.get("Error", {})
        msg = f"An error occurred ({error.get('Code', 'Unknown')}) when calling the {operation_name} operation: {error.get('Message', 'Unknown')}"
        # Call Exception.__init__ directly to avoid ClientError's __init__
        Exception.__init__(self, msg)


class MockEventStreamError(MockClientError):
    """Mock EventStreamError that mimics botocore.exceptions.EventStreamError

    EventStreamError is a subclass of ClientError but may have a different
    response structure where error code needs to be extracted from the message.
    """

    pass


# Now import the module under test (must be after mock classes are defined)
from idp_common.utils.bedrock_utils import (  # noqa: E402
    async_exponential_backoff_retry,
    exponential_backoff_retry,
)


@pytest.mark.unit
class TestAsyncExponentialBackoffRetry:
    """Tests for the async_exponential_backoff_retry decorator."""

    @pytest.mark.asyncio
    async def test_successful_call_no_retry(self):
        """Test that successful calls don't trigger retries."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3)
        async def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await successful_func()

        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_throttling_exception(self):
        """Test retry on ThrottlingException."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def throttled_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "Rate exceeded",
                        }
                    },
                    "TestOperation",
                )
            return "success"

        result = await throttled_func()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_on_service_unavailable_exception(self):
        """Test retry on ServiceUnavailableException (uppercase)."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def unavailable_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "ServiceUnavailableException",
                            "Message": "Service unavailable",
                        }
                    },
                    "TestOperation",
                )
            return "success"

        result = await unavailable_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_event_stream_error_with_lowercase_error_code(self):
        """Test retry on EventStreamError with lowercase serviceUnavailableException.

        This tests the fix for the issue where EventStreamError from ConverseStream
        uses lowercase error codes like 'serviceUnavailableException'.
        """
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def stream_error_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # Simulate EventStreamError where error code is not in the response dict
                # but is in the exception message
                error = MockEventStreamError(
                    {"Error": {}},  # Empty error dict - code not available here
                    "ConverseStream",
                )
                # Override the message to match actual EventStreamError format
                error.args = (
                    "An error occurred (serviceUnavailableException) when calling the ConverseStream operation: Bedrock is unable to process your request.",
                )
                raise error
            return "success"

        result = await stream_error_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_event_stream_error_extracts_code_from_message(self):
        """Test that error code is extracted from exception message when not in response."""
        call_count = 0

        original_decorator = async_exponential_backoff_retry(
            max_retries=3, initial_delay=0.01
        )

        @original_decorator
        async def stream_error_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # Create error with empty Error dict but error code in message
                error = MockEventStreamError(
                    {"Error": {}},
                    "ConverseStream",
                )
                error.args = (
                    "An error occurred (throttlingException) when calling the ConverseStream operation: Too many requests",
                )
                raise error
            return "success"

        result = await stream_error_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_non_retryable_error(self):
        """Test that non-retryable errors are not retried."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def non_retryable_func():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "Access denied",
                    }
                },
                "TestOperation",
            )

        with pytest.raises(MockClientError) as exc_info:
            await non_retryable_func()

        assert "AccessDeniedException" in str(exc_info.value)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """Test that exception is raised after max retries are exceeded."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def always_fails_func():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "TestOperation",
            )

        with pytest.raises(MockClientError) as exc_info:
            await always_fails_func()

        assert "ThrottlingException" in str(exc_info.value)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_validation_exception_not_retried_by_default(self):
        """Test that ValidationException without content filtering message is not retried."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def validation_error_func():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Invalid parameter",
                    }
                },
                "TestOperation",
            )

        with pytest.raises(MockClientError):
            await validation_error_func()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_validation_exception_with_content_filtering_is_retried(self):
        """Test that ValidationException with content filtering message is retried."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def content_filtered_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": "Output blocked by content filtering policy",
                        }
                    },
                    "TestOperation",
                )
            return "success"

        result = await content_filtered_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_non_client_error_not_retried(self):
        """Test that non-ClientError exceptions are not retried."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def generic_error_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("Some other error")

        with pytest.raises(ValueError):
            await generic_error_func()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_custom_retryable_errors(self):
        """Test that custom retryable errors can be specified."""
        call_count = 0

        @async_exponential_backoff_retry(
            max_retries=3,
            initial_delay=0.01,
            retryable_errors={"CustomRetryableError"},
        )
        async def custom_error_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "CustomRetryableError",
                            "Message": "Custom error",
                        }
                    },
                    "TestOperation",
                )
            return "success"

        result = await custom_error_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_exponential_backoff_increases_delay(self):
        """Test that delay increases exponentially between retries."""
        delays = []
        call_count = 0

        @async_exponential_backoff_retry(
            max_retries=4,
            initial_delay=0.1,
            exponential_base=2.0,
            jitter=0.0,  # Disable jitter for predictable delays
        )
        async def tracking_func():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "TestOperation",
            )

        # Patch asyncio.sleep to capture delay values
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            delays.append(delay)
            await original_sleep(0.001)  # Actually sleep a tiny bit

        with patch("asyncio.sleep", mock_sleep):
            with pytest.raises(MockClientError):
                await tracking_func()

        assert call_count == 4
        assert len(delays) == 3  # 3 retries = 3 sleeps
        # Check delays are increasing (with some tolerance for jitter)
        assert delays[0] < delays[1] < delays[2]

    @pytest.mark.asyncio
    async def test_all_retryable_error_codes(self):
        """Test that all documented retryable error codes are retried."""
        retryable_codes = [
            "ThrottlingException",
            "throttlingException",
            "ModelErrorException",
            "ServiceQuotaExceededException",
            "RequestLimitExceeded",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "serviceUnavailableException",
            "RequestTimeout",
            "RequestTimeoutException",
        ]

        for error_code in retryable_codes:
            call_count = 0

            @async_exponential_backoff_retry(max_retries=2, initial_delay=0.01)
            async def retryable_func():
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise MockClientError(
                        {
                            "Error": {
                                "Code": error_code,
                                "Message": f"Test {error_code}",
                            }
                        },
                        "TestOperation",
                    )
                return "success"

            result = await retryable_func()
            assert result == "success", f"Failed for error code: {error_code}"
            assert call_count == 2, (
                f"Expected 2 calls for {error_code}, got {call_count}"
            )

    @pytest.mark.asyncio
    async def test_retry_on_read_timeout_message(self):
        """Bedrock read timeouts (urllib3 pool text, often wrapped by Strands as
        EventLoopException) must be retried via message-substring match, not
        fail the whole section. Regression for the 11-min LargeTruist hang."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def timeout_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # Mirrors the real wrapped message we observed.
                raise RuntimeError(
                    "AWSHTTPSConnectionPool(host='bedrock-runtime.us-west-2."
                    "amazonaws.com', port=443): Read timed out."
                )
            return "success"

        result = await timeout_func()
        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_read_timeout_error_by_name(self):
        """botocore ReadTimeoutError (by exception type name) is retryable."""
        call_count = 0

        class ReadTimeoutError(Exception):
            pass

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def timeout_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ReadTimeoutError("read timeout on endpoint")
            return "success"

        result = await timeout_func()
        assert result == "success"
        assert call_count == 2


@pytest.mark.unit
class TestExponentialBackoffRetry:
    """Tests for the synchronous exponential_backoff_retry decorator."""

    def test_successful_call_no_retry(self):
        """Test that successful calls don't trigger retries."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3)
        def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = successful_func()

        assert result == "success"
        assert call_count == 1

    def test_retry_on_lowercase_internal_server_exception(self):
        """Regression: the streaming spelling must retry.

        Reported live from synthetic data generation:
        "An error occurred (internalServerException) when calling the
        ConverseStream operation ... Try your request again" — and the caller
        failed on the first attempt. converse_stream is wrapped by THIS
        (synchronous) decorator, whose retryable list was a narrower hardcoded
        four that omitted InternalServerException entirely; the async decorator's
        list had it, but only in the capitalised non-streaming spelling. Bedrock's
        streaming APIs lower-case the first letter of the same condition, so both
        halves of the bug had to be fixed: share DEFAULT_RETRYABLE_ERRORS, and
        compare case-insensitively.
        """
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def flaky_stream():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "internalServerException",
                            "Message": "The system encountered an unexpected error during processing. Try your request again.",
                        }
                    },
                    "ConverseStream",
                )
            return "recovered"

        assert flaky_stream() == "recovered"
        assert call_count == 3

    def test_retry_on_capitalised_internal_server_exception(self):
        """The non-streaming spelling retries here too (it previously did not)."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "InternalServerException",
                            "Message": "transient",
                        }
                    },
                    "Converse",
                )
            return "ok"

        assert flaky() == "ok"
        assert call_count == 2

    def test_sync_decorator_shares_the_default_retryable_set(self):
        """Guards against the two decorators drifting apart again.

        The sync path silently had a much narrower list than the async one, which
        is why a condition listed as retryable still failed a streaming call.
        """
        from idp_common.utils.bedrock_utils import DEFAULT_RETRYABLE_ERRORS

        call_count = 0

        @exponential_backoff_retry(max_retries=2, initial_delay=0.01)
        def unavailable():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {"Error": {"Code": "serviceUnavailableException", "Message": "busy"}},
                "ConverseStream",
            )

        # Listed (capitalised) in the shared set, sent lower-cased by streaming.
        assert "ServiceUnavailableException" in DEFAULT_RETRYABLE_ERRORS
        with pytest.raises(botocore.exceptions.ClientError):
            unavailable()
        assert call_count == 2, "should have retried before giving up"

    def test_still_does_not_retry_a_genuine_client_error(self):
        """Case-insensitivity must not turn real failures into retry storms."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def denied():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "ConverseStream",
            )

        with pytest.raises(botocore.exceptions.ClientError):
            denied()
        assert call_count == 1

    def test_retry_on_throttling_exception(self):
        """Test retry on ThrottlingException."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def throttled_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise MockClientError(
                    {
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "Rate exceeded",
                        }
                    },
                    "TestOperation",
                )
            return "success"

        result = throttled_func()

        assert result == "success"
        assert call_count == 3

    def test_max_retries_exceeded(self):
        """Test that exception is raised after max retries are exceeded."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def always_fails_func():
            nonlocal call_count
            call_count += 1
            raise MockClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "TestOperation",
            )

        with pytest.raises(MockClientError) as exc_info:
            always_fails_func()

        assert "ThrottlingException" in str(exc_info.value)
        assert call_count == 3

    def test_non_client_error_not_retried(self):
        """Test that non-ClientError exceptions are not retried."""
        call_count = 0

        @exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        def generic_error_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("Some other error")

        with pytest.raises(ValueError):
            generic_error_func()

        assert call_count == 1


@pytest.mark.unit
class TestEventStreamErrorHandling:
    """Specific tests for EventStreamError handling from ConverseStream API."""

    @pytest.mark.asyncio
    async def test_event_stream_error_service_unavailable_lowercase(self):
        """Test the exact error format from the reported issue.

        This reproduces the error:
        EventStreamError: An error occurred (serviceUnavailableException) when calling
        the ConverseStream operation: Bedrock is unable to process your request.
        """
        call_count = 0

        @async_exponential_backoff_retry(max_retries=5, initial_delay=0.01)
        async def converse_stream_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Simulate the exact error from the issue
                error = MockEventStreamError(
                    {"Error": {}},  # Empty - error code not in response dict
                    "ConverseStream",
                )
                # Override message to match actual format
                error.args = (
                    "An error occurred (serviceUnavailableException) when calling the ConverseStream operation: Bedrock is unable to process your request.",
                )
                raise error
            return {"result": "success"}

        result = await converse_stream_func()

        assert result == {"result": "success"}
        assert call_count == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_event_stream_error_with_response_code(self):
        """Test EventStreamError when error code is in response dict."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def converse_stream_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise MockEventStreamError(
                    {
                        "Error": {
                            "Code": "ServiceUnavailableException",
                            "Message": "Service unavailable",
                        }
                    },
                    "ConverseStream",
                )
            return "success"

        result = await converse_stream_func()

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_event_stream_error_unknown_code_not_retried(self):
        """Test EventStreamError with unknown error code is not retried."""
        call_count = 0

        @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
        async def converse_stream_func():
            nonlocal call_count
            call_count += 1
            error = MockEventStreamError(
                {"Error": {}},
                "ConverseStream",
            )
            error.args = (
                "An error occurred (unknownException) when calling the ConverseStream operation: Unknown error",
            )
            raise error

        with pytest.raises(MockEventStreamError):
            await converse_stream_func()

        assert call_count == 1

    def test_error_code_extraction_regex(self):
        """Test that the regex correctly extracts error codes from exception messages."""
        test_cases = [
            (
                "An error occurred (serviceUnavailableException) when calling the ConverseStream operation",
                "serviceUnavailableException",
            ),
            (
                "An error occurred (ThrottlingException) when calling the Converse operation",
                "ThrottlingException",
            ),
            (
                "An error occurred (ModelErrorException) when calling the InvokeModel operation: Model error",
                "ModelErrorException",
            ),
        ]

        for message, expected_code in test_cases:
            match = re.search(r"\((\w+)\)", message)
            assert match is not None, f"Failed to match: {message}"
            assert match.group(1) == expected_code, (
                f"Expected {expected_code}, got {match.group(1)}"
            )
