# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#787: only TRANSIENT failures are surfaced as ``TransientError`` for Step Functions
to retry; deterministic failures keep their own names and are not retried."""

from __future__ import annotations

import asyncio

import botocore.exceptions
import pytest

from idp_common.utils.transient_errors import (
    TRANSIENT_EXCEPTION_TYPES,
    TRANSIENT_MESSAGE_MARKERS,
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
            # S3 spellings (SlowDown is S3's throttling code) and the stream error.
            _client_error("SlowDown"),
            _client_error("ServiceUnavailable"),
            _client_error("InternalError"),
            _client_error("ModelStreamErrorException"),
            ConnectionRefusedError(111, "Connection refused"),
            BrokenPipeError(32, "Broken pipe"),
            botocore.exceptions.IncompleteReadError(actual_bytes=1, expected_bytes=2),
            # A TYPE match with a name the vocabulary does not know and no marker text.
            type("Weird", (botocore.exceptions.ReadTimeoutError,), {})(
                endpoint_url="https://x"
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

    def test_only_the_explicit_cause_link_is_followed(self):
        """A wrapper raised WITHOUT `from` inherits nothing: `__context__` is set by
        Python on any exception raised while another is handled."""
        root = botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock")
        try:
            try:
                raise root
            except Exception:
                raise RuntimeError("EventLoopException: agent failed")  # no `from`
        except Exception as wrapper:
            assert wrapper.__context__ is root
            assert is_transient_error(wrapper) is False


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
            __import__("json").JSONDecodeError("x", "{", 0),
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

    def test_bedrock_client_retry_recursion_does_not_make_a_hard_error_transient(self):
        """`BedrockClient._invoke_with_retry` re-invokes from inside its `except`, so
        attempt 2's ValidationException carries attempt 1's throttle as __context__.
        The definite code on the outer exception is the verdict; the chain is not
        walked (reproduced by the #806 review against the real client)."""
        first = _client_error("ThrottlingException", "Rate exceeded")
        try:
            try:
                raise first
            except Exception:
                raise _client_error(
                    "ValidationException", "Input is too long for requested model."
                )
        except Exception as second:
            assert second.__context__ is first
            assert is_transient_error(second) is False
        # Even an EXPLICIT `from` a transient cause does not rescue a definite hard code.
        try:
            raise _client_error("ValidationException", "malformed") from first
        except Exception as explicit:
            assert is_transient_error(explicit) is False

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("Unknown config key 'throttling_limit'"),
            KeyError("rate exceeded"),
            Exception(
                "Failed to invoke custom prompt Lambda: ValidationException: Too many "
                "tokens, please wait before trying again."
            ),
            _client_error(
                "ValidationException",
                "Too many tokens, please wait before trying again.",
            ),
            _client_error(
                "AccessDeniedException", "throttled by policy: rate exceeded"
            ),
        ],
        ids=lambda e: f"{type(e).__name__}:{str(e)[:30]}",
    )
    def test_throttling_words_in_a_deterministic_error_do_not_make_it_transient(
        self, exc
    ):
        assert is_transient_error(exc) is False

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

    def test_an_already_surfaced_transient_error_is_not_wrapped_again(self):
        """The assessment handler classifies inside `_handle` AND in the outer
        wrapper; the outer pass must not produce TransientError(TransientError(...))."""
        inner = TransientError(TimeoutError("x"), where="assessment section 1")
        raise_if_transient(inner, where="assessment section 1")  # no raise
        assert inner.original_type == "TimeoutError"


class TestProducersInThisCodebase:
    def test_the_custom_prompt_lambda_wrapper_is_judged_by_its_cause(self):
        """`_invoke_custom_prompt_lambda` re-raises `Exception(msg) from e`. A Lambda
        throttle or a botocore timeout invoking the hook is transient THROUGH the
        cause; the same wrapper text without a cause (a hook that failed on its own
        terms) stays hard."""
        throttle = _client_error("TooManyRequestsException", "Rate Exceeded")
        try:
            raise Exception(
                "Failed to invoke custom prompt Lambda arn: x"
            ) from throttle
        except Exception as wrapped:
            assert is_transient_error(wrapped) is True
        timeout = botocore.exceptions.ReadTimeoutError(endpoint_url="https://lambda")
        try:
            raise Exception("Failed to invoke custom prompt Lambda arn: y") from timeout
        except Exception as wrapped:
            assert is_transient_error(wrapped) is True
        assert (
            is_transient_error(
                Exception("Failed to invoke custom prompt Lambda arn: z")
            )
            is False
        )

    def test_botocore_read_timeout_text_is_a_marker(self):
        """botocore says 'Read timeout on endpoint URL', not 'read timed out'."""
        assert (
            is_transient_error(
                RuntimeError('Read timeout on endpoint URL: "https://x"')
            )
            is True
        )

    def test_dynamodb_throttle_code_is_transient(self):
        assert (
            is_transient_error(_client_error("ProvisionedThroughputExceededException"))
            is True
        )


class TestEachRuleInIsolation:
    """One case per marker and per type, so dropping any single entry fails a test."""

    @pytest.mark.parametrize("marker", sorted(TRANSIENT_MESSAGE_MARKERS))
    def test_each_message_marker(self, marker):
        assert is_transient_error(RuntimeError(f"zzz {marker.upper()} zzz")) is True

    @pytest.mark.parametrize(
        "base",
        [t for t in TRANSIENT_EXCEPTION_TYPES if t not in (asyncio.TimeoutError,)],
        ids=lambda t: t.__name__,
    )
    def test_each_exception_type_by_isinstance_alone(self, base):
        """A subclass with an unknown NAME and a marker-free message: only the
        isinstance rule can catch it. botocore types format their message from
        required kwargs, so each gets the kwargs its template needs."""
        odd = type("Odd", (base,), {})
        kwargs = {
            "ReadTimeoutError": {"endpoint_url": "u"},
            "ConnectTimeoutError": {"endpoint_url": "u"},
            "EndpointConnectionError": {"endpoint_url": "u"},
            "ConnectionClosedError": {"endpoint_url": "u"},
            "IncompleteReadError": {"actual_bytes": 1, "expected_bytes": 2},
            "ResponseStreamingError": {"error": "e"},
            "ProxyConnectionError": {"proxy_url": "u"},
            "NewConnectionError": {"pool": None, "message": "plain text"},
        }.get(base.__name__)
        exc = odd(**kwargs) if kwargs else odd("plain text")
        assert "read timed out" not in str(exc).lower()
        assert is_transient_error(exc) is True
