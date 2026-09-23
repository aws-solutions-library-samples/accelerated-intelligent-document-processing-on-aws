# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#787: only TRANSIENT failures are surfaced as ``TransientError`` for Step Functions
to retry; deterministic failures keep their own names and are not retried."""

from __future__ import annotations

import asyncio

import botocore.exceptions
import pytest

from idp_common.utils.transient_errors import (
    _NOT_TRANSIENT_AT_TASK_LEVEL,
    _PINNED_BOTOCORE_RETRY_CODES,
    DETERMINISTIC_MESSAGE_MARKERS,
    TRANSIENT_ERROR_NAMES,
    TRANSIENT_EXCEPTION_TYPES,
    TRANSIENT_MESSAGE_MARKERS,
    TransientError,
    _botocore_retry_codes,
    is_model_tool_use_sequence_error,
    is_transient_error,
    raise_if_transient,
)

#: The exact wire text botocore builds for the #895 failure.
_TOOL_USE_MESSAGE = (
    "Model produced invalid sequence as part of ToolUse. Please refer to the "
    "model tool use troubleshooting guide."
)


def _client_error(code: str, message: str = "x"):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse"
    )


def _stream_error(message: str):
    """An ``EventStreamError`` shaped like the one in the #895 logs.

    ``EventStreamError`` subclasses ``ClientError``, so its code is what rule 1
    would normally judge it by — and ``modelStreamErrorException`` IS a transient
    code.
    """
    return botocore.exceptions.EventStreamError(
        {"Error": {"Code": "modelStreamErrorException", "Message": message}},
        "ConverseStream",
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


class TestInvalidToolUseSequenceIsNotTransient:
    """#895: ``modelStreamErrorException`` stays transient as a CODE; only the
    "Model produced invalid sequence as part of ToolUse" OUTCOME is deterministic."""

    def test_the_tool_use_outcome_is_not_transient(self):
        assert is_transient_error(_stream_error(_TOOL_USE_MESSAGE)) is False

    @pytest.mark.parametrize(
        "message",
        [
            "Your request has been throttled mid-stream",
            "The connection to the model was closed unexpectedly",
            "internal server error while streaming",
            "x",
        ],
    )
    def test_a_stream_error_with_any_other_message_is_still_transient(self, message):
        """THE regression guard: ``ConverseStream`` genuinely does break mid-stream
        for transport reasons, and those breaks must keep being retried. Narrowing
        the classification by removing ``modelstreamerrorexception`` from
        ``TRANSIENT_ERROR_NAMES`` would have failed this test."""
        assert is_transient_error(_stream_error(message)) is True

    def test_the_same_outcome_through_the_wrapped_chain_from_the_issue(self):
        """The shard runtime sees the stream error through Strands' agent loop and
        its own ``raise ... from`` wrappers. The verdict must survive the chain."""
        stream = _stream_error(_TOOL_USE_MESSAGE)
        try:
            try:
                try:
                    raise stream
                except Exception as inner:
                    raise type("EventLoopException", (Exception,), {})(
                        "agent loop failed"
                    ) from inner
            except Exception as loop:
                raise RuntimeError("shard runtime shard section 1") from loop
        except Exception as outer:
            assert is_transient_error(outer) is False
            assert is_model_tool_use_sequence_error(outer) is True

    def test_raise_if_transient_returns_silently_so_the_bare_raise_stands(self):
        """``raise_if_transient`` must NOT surface this as ``TransientError`` — that
        name is what ``workflow.asl.json`` retries up to eight times per shard task
        (``MaxAttempts: 8`` on the ``ShardExtractionStep`` retrier)."""
        raise_if_transient(_stream_error(_TOOL_USE_MESSAGE), where="shard runtime")

    def test_the_verdict_beats_the_code_the_type_and_the_class_name(self):
        """Rule 4 is evaluated before rules 1-3, so none of the three lookups that
        would say "transient" for this node can win."""
        assert (
            is_transient_error(
                type("ModelStreamErrorException", (Exception,), {})(_TOOL_USE_MESSAGE)
            )
            is False
        )
        # A TYPE-based transient (rule 3's isinstance list) carrying the text.
        assert (
            is_transient_error(
                botocore.exceptions.ResponseStreamingError(error=_TOOL_USE_MESSAGE)
            )
            is False
        )
        # Transport text does not rescue it either: rule 4 runs first.
        assert (
            is_transient_error(_stream_error(f"{_TOOL_USE_MESSAGE} Read timed out."))
            is False
        )

    def test_the_predicate_is_false_for_unrelated_failures(self):
        assert is_model_tool_use_sequence_error(ValueError("bad schema")) is False
        assert is_model_tool_use_sequence_error(_stream_error("x")) is False

    @pytest.mark.parametrize("marker", sorted(DETERMINISTIC_MESSAGE_MARKERS))
    def test_each_deterministic_marker_overrides_a_transient_code(self, marker):
        assert is_transient_error(_stream_error(f"zzz {marker.upper()} zzz")) is False


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
        }.get(base.__name__)
        if base.__name__ == "NewConnectionError":  # urllib3: (conn, message) positional
            exc = odd(None, "plain text")
        else:
            exc = odd(**kwargs) if kwargs else odd("plain text")
        assert "read timed out" not in str(exc).lower()
        assert is_transient_error(exc) is True


class TestTheThrottlingVocabularyIsDerivedFromBotocore:
    """#1132: the vocabulary knew one spelling of "throttled" and AWS uses six.

    ``ThrottlingException`` was the only throttling code listed, so a service answering
    with the legacy ``Throttling`` — 345 of 345 throttles under concurrent
    CloudFormation reads — was judged deterministic. That fails in both directions at
    once: the persistence carve-out withholds a record only for a failure it judges
    transient, and the state machine retries only the name this predicate produces.

    These tests pin the resulting **set** rather than checking one new string, and they
    pin it against botocore's own retry configuration rather than against a list written
    here, which is the part that stops the next spelling going missing the same way.
    """

    @staticmethod
    def _client_error(code: str) -> botocore.exceptions.ClientError:
        """The shape the reported failures arrived in: a bare ``ClientError``."""
        return botocore.exceptions.ClientError(
            {"Error": {"Code": code, "Message": "Rate exceeded"}}, "DescribeStacks"
        )

    def test_the_pin_matches_what_botocore_currently_ships(self):
        """The gate. A dependency bump that adds a spelling stops here for review.

        `_botocore_retry_codes` already includes it in the live vocabulary by then —
        which is the safe direction — so what this failure asks for is a decision about
        whether the new code is really a rate limit, not an emergency.
        """
        live = _botocore_retry_codes()
        assert live == _PINNED_BOTOCORE_RETRY_CODES, (
            "botocore's retryable codes changed. Added: "
            f"{sorted(live - _PINNED_BOTOCORE_RETRY_CODES)}; removed: "
            f"{sorted(_PINNED_BOTOCORE_RETRY_CODES - live)}. Decide per code whether it "
            "is a rate limit a retry clears (leave it in the derived set) or a quota "
            "that is genuinely full (add it to _NOT_TRANSIENT_AT_TASK_LEVEL with a "
            "reason), then update the pin."
        )

    def test_the_derivation_reads_botocore_rather_than_the_pin(self):
        """Not vacuous: if the read silently fell through, the pin would still match
        itself and this class would prove nothing about botocore."""
        from botocore.retries.standard import (
            ThrottledRetryableChecker,
            TransientRetryableChecker,
        )

        assert len(ThrottledRetryableChecker._THROTTLED_ERROR_CODES) >= 10
        assert "Throttling" in ThrottledRetryableChecker._THROTTLED_ERROR_CODES
        assert TransientRetryableChecker._TRANSIENT_ERROR_CODES

    def test_the_fallback_returns_the_pin_when_the_checkers_move(self, monkeypatch):
        """The attributes read are private, so the failure mode of a rename has to be a
        stale vocabulary rather than an ImportError at Lambda cold start."""
        import sys
        import types

        monkeypatch.setitem(
            sys.modules, "botocore.retries.standard", types.ModuleType("stub")
        )
        assert _botocore_retry_codes() == _PINNED_BOTOCORE_RETRY_CODES

    def test_every_botocore_retry_code_is_classified(self):
        """Universe closure: no code may sit in neither the transient set nor the
        deliberately-excluded one. This is what makes the exclusion trustworthy — an
        unclassified code silently reads as deterministic, which is the defect."""
        unclassified = {
            code
            for code in _botocore_retry_codes()
            if code.lower() not in TRANSIENT_ERROR_NAMES
            and code not in _NOT_TRANSIENT_AT_TASK_LEVEL
        }
        assert not unclassified, unclassified

    @pytest.mark.parametrize(
        "code",
        [
            "Throttling",  # the reported case: CloudFormation and others
            "ThrottledException",
            "RequestThrottled",
            "RequestThrottledException",
            "TransactionInProgressException",
            "BandwidthLimitExceeded",
            "PriorRequestNotComplete",
            "EC2ThrottledException",
        ],
    )
    def test_each_newly_recognised_code_is_transient(self, code):
        """One case per spelling added, so removing any single one fails a test."""
        assert is_transient_error(self._client_error(code)) is True

    def test_the_spelling_that_was_already_recognised_still_is(self):
        assert is_transient_error(self._client_error("ThrottlingException")) is True

    def test_limit_exceeded_is_deliberately_not_transient(self):
        """The one code botocore calls a throttle that this does not.

        On some services it is a rate limit; on others it reports a quota that is
        genuinely full, where eight attempts at 2.5x backoff cannot help. The code alone
        does not say which, and every service this solution calls that rate-limits also
        answers with a spelling that IS listed — so it stays out, and that is a decision
        rather than an omission.
        """
        assert "LimitExceededException" in _botocore_retry_codes()
        assert "LimitExceededException" in _NOT_TRANSIENT_AT_TASK_LEVEL
        assert is_transient_error(self._client_error("LimitExceededException")) is False

    def test_the_deterministic_exclusions_still_hold(self):
        """Widening the vocabulary must not have swept these back in."""
        for code in ("ValidationException", "ModelErrorException"):
            assert code in _NOT_TRANSIENT_AT_TASK_LEVEL
            assert is_transient_error(self._client_error(code)) is False

    def test_the_whole_vocabulary_is_pinned(self):
        """The exact set, so any change to it — in either direction — is reviewed.

        A predicate this shared decides whether a failure is recorded as a permanent
        diagnosis or suppressed pending a retry, on every path that uses it, so a name
        arriving or leaving unnoticed is the thing worth preventing.
        """
        assert sorted(TRANSIENT_ERROR_NAMES) == [
            "awshttpsconnectionpool",
            "bandwidthlimitexceeded",
            "brokenpipeerror",
            "connectionclosederror",
            "connectionrefusederror",
            "connectionresetterror",
            "connecttimeouterror",
            "ec2throttledexception",
            "endpointconnectionerror",
            "incompletereaderror",
            "internalerror",
            "internalservererror",
            "internalserverexception",
            "modelnotreadyexception",
            "modelstreamerrorexception",
            "modelthrottledexception",
            "modeltimeoutexception",
            "newconnectionerror",
            "priorrequestnotcomplete",
            "protocolerror",
            "provisionedthroughputexceededexception",
            "proxyconnectionerror",
            "read timed out",
            "readtimeouterror",
            "remotedisconnected",
            "requestlimitexceeded",
            "requestthrottled",
            "requestthrottledexception",
            "requesttimeout",
            "requesttimeoutexception",
            "responsestreamingerror",
            "servicequotaexceededexception",
            "serviceunavailable",
            "serviceunavailableexception",
            "slowdown",
            "throttledexception",
            "throttling",
            "throttlingexception",
            "timeouterror",
            "toomanyrequestsexception",
            "transactioninprogressexception",
        ]
