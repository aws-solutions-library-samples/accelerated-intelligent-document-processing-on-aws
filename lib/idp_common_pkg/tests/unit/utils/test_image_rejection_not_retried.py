# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Bedrock image rejection is deterministic, so retrying it is pure waste (#994).

``async_exponential_backoff_retry`` decides retryability two ways. A raw
``botocore`` ``ClientError`` is judged by its error code, and a ``ValidationException``
there is already re-raised immediately. But Strands re-wraps Bedrock errors, so on
the agentic extraction path the failure arrives through the generic
``except Exception`` branch instead — which judges retryability by SUBSTRING against
``DEFAULT_RETRYABLE_ERRORS``, and that set contains ``"ValidationException"``. A
wrapped image rejection therefore matched, and was retried up to ``max_retries``
(50 on that path, behind exponential backoff) before the caller could classify it.
The same request is rejected identically every time, so none of those attempts can
succeed.

Every test here patches sleep. Without that a regression makes them HANG rather
than fail, burning the job timeout.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import botocore.exceptions
import pytest

from idp_common.utils.bedrock_utils import async_exponential_backoff_retry

pytestmark = pytest.mark.unit

PIXEL_REJECTION = "image exceed max allowed size for many-image requests: 2000 pixels"


class _WrappedModelError(Exception):
    """Shaped like a Strands wrapper: no ``response``, the code in the message."""


def _run(coro):
    return asyncio.run(coro)


def test_a_wrapped_image_rejection_is_raised_on_the_first_attempt():
    calls = 0

    @async_exponential_backoff_retry(max_retries=50, initial_delay=0.01)
    async def failing():
        nonlocal calls
        calls += 1
        raise _WrappedModelError(
            f"An error occurred (ValidationException) when calling Converse: "
            f"{PIXEL_REJECTION}"
        )

    with patch("asyncio.sleep", new=AsyncMock()) as slept:
        with pytest.raises(_WrappedModelError):
            _run(failing())
    assert calls == 1, "an image rejection must not be attempted twice"
    slept.assert_not_awaited()


def test_a_wrapped_throttle_is_still_retried():
    """The guard must not turn the generic branch into fail-fast for everything —
    a Strands ``ModelThrottledException`` is exactly what that branch is for."""
    calls = 0

    @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
    async def failing():
        nonlocal calls
        calls += 1
        raise _WrappedModelError("ModelThrottledException: slow down")

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(_WrappedModelError):
            _run(failing())
    assert calls == 3


def test_a_raw_client_error_image_rejection_is_still_not_retried():
    """Pre-existing behaviour, pinned so the new guard cannot be credited with it
    and then removed: the ClientError branch raises any ValidationException."""
    calls = 0

    @async_exponential_backoff_retry(max_retries=50, initial_delay=0.01)
    async def failing():
        nonlocal calls
        calls += 1
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "ValidationException", "Message": PIXEL_REJECTION}},
            "Converse",
        )

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(botocore.exceptions.ClientError):
            _run(failing())
    assert calls == 1


def test_a_coded_transient_error_mentioning_an_image_is_still_retried():
    """``is_image_request_rejection`` judges the error CODE first, so a throttle
    that carries a code stays retryable however its message is worded. Note the
    qualifier: a code-less wrapped exception has no code to judge, which is why
    ``_IMAGE_REJECTION_MARKERS`` is kept to phrases that name an image limit
    explicitly — see the next test."""
    calls = 0

    @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
    async def failing():
        nonlocal calls
        calls += 1
        raise botocore.exceptions.ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "Rate exceeded while checking image size",
                }
            },
            "Converse",
        )

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(botocore.exceptions.ClientError):
            _run(failing())
    assert calls == 3


def test_a_code_less_wrapped_throttle_mentioning_an_image_is_still_retried():
    """The generic branch exists for Strands wrappers, which carry no error code —
    so there the marker list is the only guard, and it has to be narrow enough to
    stand alone. Two earlier candidates ("image size", "invalid image") were
    dropped for exactly this: they match text that is not an image rejection."""
    calls = 0

    @async_exponential_backoff_retry(max_retries=3, initial_delay=0.01)
    async def failing():
        nonlocal calls
        calls += 1
        raise _WrappedModelError(
            "ModelThrottledException: too many requests, invalid image cache warm-up"
        )

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(_WrappedModelError):
            _run(failing())
    assert calls == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
