# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Bedrock error matcher in ``bedrock_utils`` reads the error code one way
(follow-up to #2038).

#2079 made the content-filter matcher and the retry rule compare the code
case-insensitively and read it off the ``raise ... from`` chain. The image and
overflow matchers still compared ``code != "ValidationException"`` on the
exception itself, so:

* an image rejection or input overflow raised INSIDE a ``ConverseStream`` reply,
  whose code is the event-stream member ``validationException``, was judged not to
  be one, and the caller lost the explanation it exists to give;
* a Strands wrapper (``ModelThrottledException``, ``EventLoopException``) carries
  no code of its own, so a throttle whose text happened to mention an image limit
  was judged an image rejection, which is deterministic and is not retried.

And the async retry decorator's generic branch retried any exception whose text
contained "ValidationException". Strands 1.48 raises every wrapper ``from`` the
Bedrock error, so no real wrapper needs that substring; what it did match was an
exception that merely quoted a refused request's code.

**Instrument.** A real Strands ``Agent`` over a real streaming ``BedrockModel`` and
a real ``bedrock-runtime`` client, only urllib3's ``urlopen`` replaced (the harness
of ``test_validation_exception_retry_rule``); member names come from the botocore
service model.
"""

from __future__ import annotations

import asyncio

import pytest
from botocore.exceptions import ClientError, EventStreamError
from strands import Agent
from strands.types.exceptions import EventLoopException, ModelThrottledException
from tests.unit.utils import test_validation_exception_retry_rule as harness

from idp_common.extraction.agentic_idp import invoke_agent_with_retry
from idp_common.utils.bedrock_utils import (
    CONTENT_FILTER_BLOCK_MESSAGE,
    async_exponential_backoff_retry,
    is_content_filter_block,
    is_image_count_rejection,
    is_image_request_rejection,
    is_input_token_overflow,
    is_unretryable_validation_error,
)

pytestmark = pytest.mark.unit

bedrock = harness.bedrock

_IMAGE = "Too many images and documents: 101 + 0 > 100"
#: Not one of Strands' own overflow phrases, so Strands does not convert it.
_OVERFLOW = "The input token count 300000 exceeds the maximum of 200000"
_THROTTLE_MEMBER = harness._stream_member("ThrottlingException")


def _schedule(monkeypatch, letter: str, member: str, message: str) -> None:
    monkeypatch.setitem(
        harness._STEPS,
        letter,
        lambda: (
            harness._event("messageStart", {"role": "assistant"})
            + harness._exception(member, message)
        ),
    )


def _agent(bedrock, steps: str) -> Agent:
    """The harness's agent, built the way agentic extraction builds its own: with
    Strands' throttle retries off (``strands_throttle_retries=False``), so a
    throttle reaches the decorator instead of being slept on inside Strands."""
    make_agent, _sent = bedrock
    model = make_agent(steps).model
    return Agent(
        model=model, tools=[harness.ping], callback_handler=None, retry_strategy=None
    )


def _delivered(bedrock, monkeypatch, steps: str, member: str, message: str):
    """What Strands raises for an in-stream error, with no decorator."""
    _schedule(monkeypatch, "X", member, message)
    with pytest.raises(Exception) as caught:
        asyncio.run(_agent(bedrock, steps).invoke_async([{"text": "hi"}]))
    return caught.value


@pytest.mark.parametrize("steps", ["X", "TX"])
def test_an_in_stream_image_rejection_is_recognised(bedrock, monkeypatch, steps):
    error = _delivered(
        bedrock, monkeypatch, steps, harness._IN_STREAM_VALIDATION, _IMAGE
    )
    # The premise, measured: raw on the first call, wrapped after a tool use.
    raw = error.__cause__ if steps == "TX" else error
    assert isinstance(raw, EventStreamError)
    assert raw.response["Error"]["Code"] == harness._IN_STREAM_VALIDATION
    assert is_image_request_rejection(error)
    assert is_image_count_rejection(error)
    assert not is_input_token_overflow(error)


@pytest.mark.parametrize("steps", ["X", "TX"])
def test_an_in_stream_input_overflow_is_recognised(bedrock, monkeypatch, steps):
    error = _delivered(
        bedrock, monkeypatch, steps, harness._IN_STREAM_VALIDATION, _OVERFLOW
    )
    assert is_input_token_overflow(error)
    assert not is_image_request_rejection(error)


@pytest.mark.parametrize("steps", ["X", "TX"])
@pytest.mark.parametrize("message", [_IMAGE, _OVERFLOW, CONTENT_FILTER_BLOCK_MESSAGE])
def test_an_in_stream_throttle_is_judged_by_its_code(
    bedrock, monkeypatch, steps, message
):
    """Strands re-raises a throttle as ``ModelThrottledException`` ``from`` it,
    and the wrapper has no code: the one on the chain decides."""
    error = _delivered(bedrock, monkeypatch, steps, _THROTTLE_MEMBER, message)
    assert isinstance(error, (ModelThrottledException, EventLoopException))
    assert not is_image_request_rejection(error)
    assert not is_input_token_overflow(error)
    assert not is_content_filter_block(error)
    assert not is_unretryable_validation_error(error)


def test_the_throttle_member_is_not_the_shape_name():
    assert _THROTTLE_MEMBER != "ThrottlingException"


def test_a_wrapper_hint_is_not_read_when_the_chain_has_a_code():
    """Text a wrapper adds is not the service's verdict."""
    inner = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Malformed input"}},
        "Converse",
    )
    try:
        raise RuntimeError(f"{inner}; hint: too many images, image exceeds") from inner
    except RuntimeError as wrapped:
        assert not is_image_request_rejection(wrapped)
    # A code-less exception is still judged by its own text.
    assert is_image_request_rejection(RuntimeError(_IMAGE))
    assert is_input_token_overflow(RuntimeError(_OVERFLOW))


# ------------------------------------------------- the generic-branch substring


def _counting(error_factory):
    calls = []

    @async_exponential_backoff_retry(max_retries=5, initial_delay=0, jitter=0)
    async def call():
        calls.append(1)
        raise error_factory()

    return call, calls


_QUOTED = (
    "agent failed: An error occurred (ValidationException) when calling the "
    "ConverseStream operation: A maximum of 4 blocks with cache_control may be "
    "provided. Found 5."
)


def test_an_unchained_exception_quoting_a_validation_error_is_not_retried(bedrock):
    """No Strands wrapper produces this shape; the substring retried it anyway."""
    call, calls = _counting(lambda: RuntimeError(_QUOTED))
    with pytest.raises(RuntimeError):
        asyncio.run(call())
    assert len(calls) == 1


def test_an_unchained_block_is_not_retried_either(bedrock):
    """Retrying a block needs its code on the chain, as the wrapped block below
    has. Text alone, with no code anywhere, was not retried before either."""
    call, calls = _counting(
        lambda: RuntimeError(f"agent failed: {CONTENT_FILTER_BLOCK_MESSAGE}")
    )
    with pytest.raises(RuntimeError):
        asyncio.run(call())
    assert len(calls) == 1


def _wrapped_block() -> Exception:
    inner = ClientError(
        {
            "Error": {
                "Code": "validationException",
                "Message": CONTENT_FILTER_BLOCK_MESSAGE,
            }
        },
        "ConverseStream",
    )
    try:
        raise EventLoopException(inner, {}) from inner
    except EventLoopException as wrapped:
        return wrapped


def test_a_wrapped_block_is_retried(bedrock):
    call, calls = _counting(_wrapped_block)
    with pytest.raises(EventLoopException):
        asyncio.run(call())
    assert len(calls) == 5


def test_a_narrowed_vocabulary_does_not_retry_a_block(bedrock):
    """Without ValidationException in the caller's vocabulary, a block is not
    one of the codes it asked to retry."""
    calls = []

    @async_exponential_backoff_retry(
        max_retries=5, initial_delay=0, jitter=0, retryable_errors={"Throttling"}
    )
    async def call():
        calls.append(1)
        raise _wrapped_block()

    with pytest.raises(EventLoopException):
        asyncio.run(call())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("member", "message", "sends"),
    [
        # Wrapped by Strands after a tool use; judged by the cause's code.
        (harness._IN_STREAM_VALIDATION, CONTENT_FILTER_BLOCK_MESSAGE, "TXO"),
        (harness._IN_STREAM_VALIDATION, _IMAGE, "TX"),
        (_THROTTLE_MEMBER, _IMAGE, "TXO"),
    ],
)
def test_the_agentic_ladder_end_to_end(bedrock, monkeypatch, member, message, sends):
    _schedule(monkeypatch, "X", member, message)
    agent = _agent(bedrock, "TXO")
    sent = bedrock[1]
    if sends.endswith("O"):
        result = asyncio.run(invoke_agent_with_retry([{"text": "hi"}], agent))
        assert result.stop_reason == "end_turn"
    else:
        with pytest.raises(Exception) as caught:
            asyncio.run(invoke_agent_with_retry([{"text": "hi"}], agent))
        assert is_image_request_rejection(caught.value)
    assert sent == list(sends)


def _coded(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


@pytest.mark.parametrize(
    ("outer", "inner", "image", "block"),
    [
        ("ThrottlingException", "ValidationException", False, False),
        ("ValidationException", "ThrottlingException", True, True),
    ],
)
def test_the_first_code_on_the_chain_decides(outer, inner, image, block):
    """Two coded exceptions on one chain: the outermost one is the verdict, so a
    rule reading the last (or any) code on the chain gives the other answer."""
    text = f"{_IMAGE}; {CONTENT_FILTER_BLOCK_MESSAGE}"
    try:
        try:
            raise _coded(inner, text)
        except ClientError as first:
            raise _coded(outer, text) from first
    except ClientError as error:
        assert is_image_request_rejection(error) is image
        assert is_content_filter_block(error) is block
        assert is_unretryable_validation_error(error) is False
