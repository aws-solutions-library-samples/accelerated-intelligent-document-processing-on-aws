# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#787: only TRANSIENT failures are surfaced as ``TransientError`` for Step Functions
to retry; deterministic failures keep their own names and are not retried."""

from __future__ import annotations

import asyncio

import botocore.exceptions
import pytest

from idp_common.utils.transient_errors import (
    TransientError,
    is_transient_error,
    raise_if_transient,
)


def _client_error(code: str, message: str = "x"):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse"
    )


class TestTransient:
    @pytest.mark.parametrize(
        "exc",
        [
            _client_error("ThrottlingException"),
            _client_error("ServiceUnavailableException"),
            _client_error("InternalServerException"),
            _client_error("internalServerException"),  # streaming API casing
            _client_error("ModelTimeoutException"),
            _client_error("ModelNotReadyException"),
            _client_error("TooManyRequestsException"),
            botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock"),
            botocore.exceptions.ConnectTimeoutError(endpoint_url="https://bedrock"),
            botocore.exceptions.EndpointConnectionError(endpoint_url="https://bedrock"),
            ConnectionResetError(104, "Connection reset by peer"),
            TimeoutError("deadline"),
            asyncio.TimeoutError(),
            type("ModelThrottledException", (Exception,), {})("rate exceeded"),
            RuntimeError(
                "EventLoopException: AWSHTTPSConnectionPool(host='bedrock-runtime'): "
                "Read timed out."
            ),
        ],
        ids=lambda e: f"{type(e).__name__}:{str(e)[:30]}",
    )
    def test_transient_conditions_are_recognised(self, exc):
        assert is_transient_error(exc) is True

    def test_a_transient_root_is_found_through_wrappers(self):
        """Strands wraps the botocore timeout; a handler sees only the wrapper."""
        root = botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock")
        try:
            try:
                raise root
            except Exception as inner:
                raise type("EventLoopException", (Exception,), {})(
                    "agent loop failed"
                ) from inner
        except Exception as wrapper:
            assert is_transient_error(wrapper) is True

    def test_a_transient_error_is_itself_transient(self):
        assert is_transient_error(TransientError(TimeoutError("x"))) is True


class TestNotTransient:
    @pytest.mark.parametrize(
        "exc",
        [
            _client_error("ValidationException", "malformed input request"),
            # A ValidationException whose TEXT mentions tokens is still deterministic.
            _client_error(
                "ValidationException", "Input is too long for requested model"
            ),
            _client_error("ModelErrorException"),
            _client_error("AccessDeniedException"),
            _client_error("ResourceNotFoundException"),
            ValueError("No section_id found in event"),
            KeyError("Transactions"),
            json_error := __import__("json").JSONDecodeError("x", "{", 0),
            type("EventLoopException", (Exception,), {})("tool returned invalid JSON"),
            RuntimeError("schema validation failed: 'Amount' is a required property"),
        ],
        ids=lambda e: f"{type(e).__name__}:{str(e)[:30]}",
    )
    def test_deterministic_failures_are_not_transient(self, exc):
        assert is_transient_error(exc) is False

    def test_a_deterministic_root_under_a_wrapper_is_not_transient(self):
        try:
            try:
                raise ValueError("bad schema")
            except Exception as inner:
                raise RuntimeError("EventLoopException: agent failed") from inner
        except Exception as wrapper:
            assert is_transient_error(wrapper) is False

    def test_a_cycle_in_the_chain_terminates(self):
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        assert is_transient_error(a) is False


class TestRaiseIfTransient:
    def test_reraises_transient_as_the_one_stable_name_with_the_cause_kept(self):
        root = _client_error("ThrottlingException", "Rate exceeded")
        with pytest.raises(TransientError) as info:
            raise_if_transient(root, where="extraction")
        assert info.value.__cause__ is root
        assert info.value.original_type == "ClientError"
        assert "extraction: ClientError" in str(info.value)
        assert "Rate exceeded" in str(info.value)
        # The NAME is what workflow.asl.json matches on.
        assert type(info.value).__name__ == "TransientError"

    def test_returns_silently_for_a_hard_error(self):
        raise_if_transient(ValueError("bad"))  # no raise
