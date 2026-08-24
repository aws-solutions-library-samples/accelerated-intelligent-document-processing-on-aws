import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Unpack

import botocore.exceptions
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
from mypy_boto3_bedrock_runtime.type_defs import (
    ConverseRequestTypeDef,
    ConverseResponseTypeDef,
    ConverseStreamRequestTypeDef,
    ConverseStreamResponseTypeDef,
    InvokeModelRequestTypeDef,
    InvokeModelResponseTypeDef,
)

# Optional import for strands-agents (may not be installed in all environments)
try:
    from strands.types.exceptions import ModelThrottledException

    _STRANDS_AVAILABLE = True
except ImportError:
    _STRANDS_AVAILABLE = False
    # Create a placeholder exception class that will never match
    ModelThrottledException = type("ModelThrottledException", (Exception,), {})  # type: ignore[misc, assignment]

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Default retryable error codes (matched against ClientError codes and exception
# messages).
#
# Matching is CASE-INSENSITIVE: Bedrock's streaming APIs report the same condition
# with a lower-cased first letter (ConverseStream raises
# "internalServerException" where Converse raises "InternalServerException"), so a
# case-sensitive set silently fails to retry transient streaming errors. List one
# spelling per condition here; both decorators fold case before comparing.
DEFAULT_RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ModelThrottledException",  # Strands wrapper for throttling
    "ModelErrorException",
    "ValidationException",
    "ServiceQuotaExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",  # Transient Bedrock server-side errors
    "InternalServerError",  # Variant of InternalServerException
    "RequestTimeout",
    "RequestTimeoutException",
    # Transient network/read timeouts to Bedrock. These surface as botocore
    # ReadTimeoutError / ConnectTimeoutError, or are wrapped by Strands in an
    # EventLoopException whose message contains the urllib3 pool text. We match
    # the timeout-SPECIFIC markers (by exception name and as message substrings
    # — see the generic-Exception branch), NOT the generic "EventLoopException"
    # wrapper name (which also wraps non-retryable failures), so a slow/oversized
    # request retries with backoff instead of failing the whole section after the
    # full read timeout. The durable fix for oversized requests is image capping
    # + sharding (below); this just prevents a hard fail on a transient blip.
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "Read timed out",
    "AWSHTTPSConnectionPool",
}

# Pre-folded for case-insensitive comparison.
_DEFAULT_RETRYABLE_ERRORS_LOWER = {err.lower() for err in DEFAULT_RETRYABLE_ERRORS}

# Default retryable exception types (caught by isinstance check)
# Only include ModelThrottledException if strands is available
DEFAULT_RETRYABLE_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    (ModelThrottledException,) if _STRANDS_AVAILABLE else ()
)


def async_exponential_backoff_retry[T, **P](
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 32.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
    retryable_errors: set[str] | None = None,
    retryable_exception_types: tuple[type[Exception], ...] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    # Use defaults if not provided
    if retryable_errors is None:
        retryable_errors = DEFAULT_RETRYABLE_ERRORS
    # Fold case once per decoration rather than on every comparison.
    retryable_lower = {err.lower() for err in retryable_errors}
    if retryable_exception_types is None:
        retryable_exception_types = DEFAULT_RETRYABLE_EXCEPTION_TYPES

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            delay = initial_delay

            def log_bedrock_invocation_error(error: Exception, attempt_num: int):
                """Log bedrock invocation details when an error occurs"""
                # Fallback logging if extraction fails
                logger.error(
                    "Bedrock invocation error",
                    extra={
                        "function_name": func.__name__,
                        "original_error": str(error),
                        "max_attempts": max_retries,
                        "attempt_num": attempt_num,
                    },
                )

            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except botocore.exceptions.ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")

                    # For EventStreamError (subclass of ClientError), the error code
                    # may be in a different location or need to be extracted from the message
                    if not error_code:
                        # Try to extract error code from exception message
                        # Format: "An error occurred (errorCode) when calling..."
                        match = re.search(r"\((\w+)\)", str(e))
                        if match:
                            error_code = match.group(1)

                    # Log bedrock invocation details for all errors
                    log_bedrock_invocation_error(e, attempt + 1)

                    if (
                        error_code == "ValidationException"
                        and "Output blocked by content filtering policy"
                        not in e.response.get("Error", {}).get("Message", "")
                    ):
                        raise
                    if (
                        error_code is None
                        or error_code.lower() not in retryable_lower
                        or attempt == max_retries - 1
                    ):
                        raise

                    jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                    sleep_time = max(0.1, delay * (1 + jitter_value))
                    logger.warning(
                        f"{error_code}:{e.response.get('Error', {}).get('Message', '')} encountered in {func.__name__}. Retrying in {sleep_time:.2f} seconds. "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                    await asyncio.sleep(sleep_time)
                    delay = min(delay * exponential_base, max_delay)
                except Exception as e:
                    # Check if this is a retryable exception type (e.g., Strands ModelThrottledException)
                    is_retryable_type = retryable_exception_types and isinstance(
                        e, retryable_exception_types
                    )

                    # Also check if exception name or message contains retryable error patterns
                    exception_name = type(e).__name__
                    exception_str = str(e)
                    exception_str_lower = exception_str.lower()
                    is_retryable_name = (
                        exception_name.lower() in retryable_lower
                        or any(err in exception_str_lower for err in retryable_lower)
                    )

                    if (
                        is_retryable_type or is_retryable_name
                    ) and attempt < max_retries - 1:
                        # Log and retry
                        log_bedrock_invocation_error(e, attempt + 1)
                        jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                        sleep_time = max(0.1, delay * (1 + jitter_value))
                        logger.warning(
                            f"{exception_name}: {exception_str} encountered in {func.__name__}. "
                            f"Retrying in {sleep_time:.2f} seconds. Attempt {attempt + 1}/{max_retries}"
                        )
                        await asyncio.sleep(sleep_time)
                        delay = min(delay * exponential_base, max_delay)
                        continue

                    # Log bedrock invocation details for non-retryable exceptions
                    log_bedrock_invocation_error(e, attempt + 1)
                    raise

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def exponential_backoff_retry[T, **P](
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 32.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay

            def log_bedrock_invocation_error(error: Exception, attempt_num: int):
                """Log bedrock invocation details when an error occurs"""
                try:
                    # Check for invoke_model API (has 'body' parameter)
                    if "body" in kwargs:
                        logger.error(
                            "Bedrock invoke_model failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                                "body": kwargs["body"],
                            },
                        )
                    # Check for converse API (has structured parameters)
                    elif any(
                        key in kwargs
                        for key in [
                            "messages",
                            "inferenceConfig",
                            "system",
                            "toolConfig",
                        ]
                    ):
                        # Log converse API parameters
                        converse_data = {
                            k: v
                            for k, v in kwargs.items()
                            if k
                            in [
                                "messages",
                                "inferenceConfig",
                                "system",
                                "toolConfig",
                                "additionalModelRequestFields",
                                "guardrailConfig",
                                "performanceConfig",
                                "promptVariables",
                                "requestMetadata",
                            ]
                        }
                        logger.error(
                            "Bedrock converse failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                                "parameters": json.dumps(converse_data, default=str),
                            },
                        )
                    else:
                        # Generic bedrock error logging
                        logger.error(
                            "Bedrock invocation failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                            },
                        )

                except Exception as log_error:
                    # Fallback logging if extraction fails
                    logger.error(
                        "Failed to log bedrock invocation details",
                        extra={
                            "function_name": func.__name__,
                            "log_error": str(log_error),
                            "original_error": str(error),
                        },
                    )

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except botocore.exceptions.ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")

                    # Log bedrock invocation details for all errors
                    log_bedrock_invocation_error(e, attempt + 1)

                    if (
                        error_code == "ValidationException"
                        and "Output blocked by content filtering policy"
                        not in e.response.get("Error", {}).get("Message", "")
                    ):
                        raise
                    # Shares DEFAULT_RETRYABLE_ERRORS with the async decorator so
                    # both paths retry the same conditions; converse_stream is
                    # wrapped here, so any streaming-only spelling omitted from
                    # that set fails the caller on the first attempt.
                    if (
                        error_code is None
                        or error_code.lower() not in _DEFAULT_RETRYABLE_ERRORS_LOWER
                        or attempt == max_retries - 1
                    ):
                        raise

                    jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                    sleep_time = max(0.1, delay * (1 + jitter_value))
                    logger.warning(
                        f"{error_code}:{e.response.get('Error', {}).get('Message', '')} encountered in {func.__name__}. Retrying in {sleep_time:.2f} seconds. "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                    time.sleep(sleep_time)
                    delay = min(delay * exponential_base, max_delay)
                except Exception as e:
                    # Log bedrock invocation details for non-ClientError exceptions too
                    log_bedrock_invocation_error(e, attempt + 1)
                    raise

            return func(*args, **kwargs)

        return wrapper

    return decorator


class BedrockClientWrapper:
    """
    A wrapper around AWS Bedrock Runtime Client that provides automatic retry logic
    with exponential backoff for handling transient errors and rate limiting.

    This wrapper automatically retries failed requests for specific error types:
    - ThrottlingException: When API rate limits are exceeded
    - ModelErrorException: When the model encounters temporary errors
    - ValidationException: When content filtering blocks output (retryable case)

    The retry mechanism uses exponential backoff with jitter to avoid thundering herd
    problems when multiple clients retry simultaneously.

    Attributes:
        client (BedrockRuntimeClient): The underlying AWS Bedrock Runtime client
        max_retries (int): Maximum number of retry attempts
        initial_delay (float): Initial delay between retries in seconds
        max_delay (float): Maximum delay between retries in seconds
        exponential_base (float): Base for exponential backoff calculation
        jitter (float): Random jitter factor to add variance to retry delays
        invoke_model: Wrapped invoke_model method with retry logic
        converse: Wrapped converse method with retry logic

    Example:
        >>> import boto3
        >>> from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
        >>> bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
        >>> wrapper = BedrockClientWrapper(bedrock_client, max_retries=3)
        >>> # Use invoke_model with automatic retries
        >>> response = wrapper.invoke_model(
        ...     modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        ...     body=json.dumps(
        ...         {
        ...             "messages": [{"role": "user", "content": "Hello"}],
        ...             "max_tokens": 100,
        ...         }
        ...     ),
        ... )
        >>> # Use converse API with automatic retries
        >>> response = wrapper.converse(
        ...     modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        ...     messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        ... )
    """

    def __init__(
        self,
        bedrock_client: BedrockRuntimeClient,
        max_retries: int = 5,
        initial_delay: float = 1.0,
        max_delay: float = 32.0,
        exponential_base: float = 2.0,
        jitter: float = 0.1,
    ):
        """
        Initialize the BedrockClientWrapper with retry configuration.

        Args:
            bedrock_client (BedrockRuntimeClient): The AWS Bedrock Runtime client to wrap
            max_retries (int, optional): Maximum number of retry attempts. Defaults to 5.
            initial_delay (float, optional): Initial delay between retries in seconds. Defaults to 1.0.
            max_delay (float, optional): Maximum delay between retries in seconds. Defaults to 32.0.
            exponential_base (float, optional): Base for exponential backoff calculation. Defaults to 2.0.
            jitter (float, optional): Random jitter factor (0.0-1.0) to add variance to retry delays. Defaults to 0.1.

        Raises:
            TypeError: If bedrock_client is not a BedrockRuntimeClient instance
            ValueError: If retry parameters are invalid (negative values, jitter > 1.0, etc.)
        """
        self.client = bedrock_client

        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

        # Apply decorator directly to client methods
        self._decorated_invoke_model = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.invoke_model)

        self._decorated_converse = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.converse)

        self._decorated_converse_stream_async = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.converse_stream)

    def invoke_model(
        self, **kwargs: Unpack[InvokeModelRequestTypeDef]
    ) -> InvokeModelResponseTypeDef:
        """
        Invoke a model with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.invoke_model()
        but includes automatic retry logic with exponential backoff.

        Args:
            modelId: The ID or ARN of the model to invoke
            body: The input data to send to the model
            contentType: The MIME type of the input data
            accept: The desired MIME type of the response
            **kwargs: Additional arguments passed to the underlying API

        Returns:
            InvokeModelResponseTypeDef: The response from the model invocation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_invoke_model(**kwargs)

    def converse(
        self,
        **kwargs: Unpack[ConverseRequestTypeDef],
    ) -> ConverseResponseTypeDef:
        """
        Converse with a model using the conversation API with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.converse()
        but includes automatic retry logic with exponential backoff.

        Args:
            modelId: The ID or ARN of the model to invoke
            messages: The conversation messages
            system: System prompts to provide context
            inferenceConfig: Configuration for model inference parameters
            toolConfig: Configuration for tool use
            guardrailConfig: Configuration for content filtering
            additionalModelRequestFields: Additional model-specific request fields
            promptVariables: Variables to substitute in prompts
            additionalModelResponseFieldPaths: Additional response field paths
            performanceConfig: Performance optimization configuration
            requestMetadata: Metadata for the request
            **kwargs: Additional arguments passed to the underlying API

        Returns:
            ConverseResponseTypeDef: The response from the conversation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_converse(**kwargs)

    def converse_stream(
        self, **kwargs: Unpack[ConverseStreamRequestTypeDef]
    ) -> ConverseStreamResponseTypeDef:
        """
        Async version of converse_stream with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.converse_stream()
        but runs asynchronously with automatic retry logic and exponential backoff.

        Args:
            **kwargs: All arguments passed to the underlying converse_stream API

        Returns:
            The streaming response from the conversation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_converse_stream_async(**kwargs)
