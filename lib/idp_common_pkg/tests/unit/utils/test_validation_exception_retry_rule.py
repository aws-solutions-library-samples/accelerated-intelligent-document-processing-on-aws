# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The retry decorators retry exactly one ``ValidationException``: a content-filter
block (follow-up to #2038).

Both decorators used to decide with ``error_code == "ValidationException"`` and a
case-sensitive message test. An error raised INSIDE a ``ConverseStream`` reply is
named by its event-stream member, ``validationException``, so it missed that test
and fell through to the case-folded retryable vocabulary, which lists
``ValidationException``: any in-stream validation error was retried, on the
agentic path until its backoff allowance ran out (six sends, measured with this
harness). The async decorator's generic branch did the same for
a ``ValidationException`` Strands had wrapped.

**Instrument.** A real Strands ``Agent`` over a real ``BedrockModel`` (streaming,
as production runs it) and a real ``bedrock-runtime`` client, with only urllib3's
``urlopen`` replaced by one that answers with hand-framed
``application/vnd.amazon.eventstream`` bodies. botocore's own event-stream parser
turns the exception event into the ``EventStreamError`` the decorators see, and
Strands delivers it raw (first model call) or wrapped (a call after a tool use).
The exception member name is read from the botocore service model, not typed here.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import zlib

import botocore.session
import pytest
import urllib3
from botocore.config import Config
from botocore.exceptions import ClientError, EventStreamError
from botocore.httpsession import URLLib3Session
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.decorator import DecoratedFunctionTool
from strands.tools.decorator import tool as _strands_tool
from strands.types.exceptions import EventLoopException

import idp_common.utils.bedrock_utils as bedrock_utils
from idp_common.extraction.agentic_idp import invoke_agent_with_retry
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.utils.bedrock_utils import (
    CONTENT_FILTER_BLOCK_MESSAGE,
    exponential_backoff_retry,
    is_unretryable_validation_error,
)

_REAL_SEND = URLLib3Session.send

#: A validation error that is not a block: the request itself is refused, so a
#: resend is refused identically.
_OTHER_VALIDATION = "A maximum of 4 blocks with cache_control may be provided. Found 5."


def _stream_member(shape_name: str) -> str:
    """The ConverseStream event-stream member that carries ``shape_name``."""
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    stream = model.operation_model("ConverseStream").output_shape.members["stream"]
    members = [
        name for name, shape in stream.members.items() if shape.name == shape_name
    ]
    assert len(members) == 1, members
    return members[0]


_IN_STREAM_VALIDATION = _stream_member("ValidationException")


def _header(name: str, value: str) -> bytes:
    raw_name, raw_value = name.encode(), value.encode()
    return (
        bytes([len(raw_name)])
        + raw_name
        + bytes([7])
        + struct.pack(">H", len(raw_value))
        + raw_value
    )


def _frame(headers: bytes, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(body) + 4, len(headers))
    prelude += struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + body
    return message + struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)


def _event(event_type: str, payload: dict) -> bytes:
    return _frame(
        _header(":event-type", event_type)
        + _header(":content-type", "application/json")
        + _header(":message-type", "event"),
        payload,
    )


def _exception(member: str, message: str) -> bytes:
    return _frame(
        _header(":exception-type", member)
        + _header(":content-type", "application/json")
        + _header(":message-type", "exception"),
        {"message": message},
    )


_METADATA = _event(
    "metadata",
    {
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    },
)

#: One streamed reply per schedule step.
_STEPS = {
    # A complete tool use, so the next model call is wrapped by Strands.
    "T": lambda: b"".join(
        [
            _event("messageStart", {"role": "assistant"}),
            _event(
                "contentBlockStart",
                {
                    "start": {"toolUse": {"toolUseId": "t1", "name": "ping"}},
                    "contentBlockIndex": 0,
                },
            ),
            _event(
                "contentBlockDelta",
                {
                    "delta": {"toolUse": {"input": json.dumps({"x": "a"})}},
                    "contentBlockIndex": 0,
                },
            ),
            _event("contentBlockStop", {"contentBlockIndex": 0}),
            _event("messageStop", {"stopReason": "tool_use"}),
            _METADATA,
        ]
    ),
    "O": lambda: b"".join(
        [
            _event("messageStart", {"role": "assistant"}),
            _event(
                "contentBlockDelta", {"delta": {"text": "done"}, "contentBlockIndex": 0}
            ),
            _event("contentBlockStop", {"contentBlockIndex": 0}),
            _event("messageStop", {"stopReason": "end_turn"}),
            _METADATA,
        ]
    ),
    # The block, raised inside the reply.
    "B": lambda: (
        _event("messageStart", {"role": "assistant"})
        + _exception(_IN_STREAM_VALIDATION, CONTENT_FILTER_BLOCK_MESSAGE)
    ),
    # Another validation error, raised inside the reply.
    "V": lambda: (
        _event("messageStart", {"role": "assistant"})
        + _exception(_IN_STREAM_VALIDATION, _OTHER_VALIDATION)
    ),
}


@_strands_tool
def ping(x: str) -> str:
    """Echo the argument."""
    return x


@pytest.fixture
def bedrock(monkeypatch):
    """``(make_agent, sent)``: an Agent factory over a scheduled stream, and the
    schedule steps actually sent."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    schedule: list[str] = []
    sent: list[str] = []

    def urlopen(pool, method, url, *args, **kwargs):
        assert url.endswith("/converse-stream"), url
        step = schedule[len(sent)] if len(sent) < len(schedule) else "O"
        sent.append(step)
        return urllib3.HTTPResponse(
            body=io.BytesIO(_STEPS[step]()),
            status=200,
            headers={"content-type": "application/vnd.amazon.eventstream"},
            preload_content=False,
        )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    monkeypatch.setattr(
        bedrock_utils,
        "asyncio",
        type("_NoSleep", (), {"sleep": staticmethod(no_sleep)}),
    )
    monkeypatch.setattr(bedrock_utils.time, "sleep", lambda _s: None)
    set_lambda_deadline_epoch(None)

    def make_agent(steps: str) -> Agent:
        assert isinstance(ping, DecoratedFunctionTool), "ping is not a Strands tool"
        schedule[:] = list(steps)
        model = BedrockModel(
            model_id="us.amazon.nova-lite-v1:0",
            streaming=True,
            boto_client_config=Config(
                retries={"total_max_attempts": 1, "mode": "standard"}
            ),
        )
        return Agent(model=model, tools=[ping], callback_handler=None)

    make_agent.schedule = schedule  # pyright: ignore[reportFunctionMemberAccess]
    yield make_agent, sent
    set_lambda_deadline_epoch(None)


def _client(make_agent) -> object:
    """The real botocore client a BedrockModel builds, for the sync decorator."""
    return make_agent("").model.client


@pytest.mark.unit
def test_the_in_stream_member_is_not_the_shape_name():
    """The premise: the code botocore gives an in-stream error is not the
    shape's name, so a case-sensitive comparison cannot match it."""
    assert _IN_STREAM_VALIDATION != "ValidationException"
    assert _IN_STREAM_VALIDATION.lower() == "validationexception"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("steps", "wrapped"), [("B", False), ("TB", True), ("V", False), ("TV", True)]
)
def test_the_error_reaches_the_decorator_in_the_form_assumed(bedrock, steps, wrapped):
    """Measured with no decorator: which form botocore and Strands deliver."""
    make_agent, _sent = bedrock
    with pytest.raises(Exception) as caught:
        asyncio.run(make_agent(steps).invoke_async([{"text": "hi"}]))
    error = caught.value
    if wrapped:
        assert type(error) is EventLoopException
        error = error.__cause__
    assert isinstance(error, EventStreamError)
    assert error.response["Error"]["Code"] == _IN_STREAM_VALIDATION


@pytest.mark.unit
@pytest.mark.parametrize("steps", ["BO", "TBO", "BBO", "TBBO"])
def test_an_in_stream_block_is_still_retried(bedrock, steps):
    make_agent, sent = bedrock
    result = asyncio.run(invoke_agent_with_retry([{"text": "hi"}], make_agent(steps)))
    assert sent == list(steps)
    assert result.stop_reason == "end_turn"


@pytest.mark.unit
@pytest.mark.parametrize("steps", ["VO", "TVO"])
def test_another_in_stream_validation_error_is_not_retried(bedrock, steps):
    """Raw (first call) and Strands-wrapped (after a tool use): one send of the
    refused request, not a retried one."""
    make_agent, sent = bedrock
    with pytest.raises(Exception) as caught:
        asyncio.run(invoke_agent_with_retry([{"text": "hi"}], make_agent(steps)))
    assert _OTHER_VALIDATION in str(caught.value)
    assert sent == list(steps[:-1])


def _drain(client, **kwargs):
    response = client.converse_stream(**kwargs)
    return [event for event in response["stream"]]


@pytest.mark.unit
def test_sync_decorator_retries_an_in_stream_block(bedrock):
    make_agent, sent = bedrock
    client = _client(make_agent)
    make_agent.schedule[:] = list("BO")
    sent.clear()
    call = exponential_backoff_retry(max_retries=5)(_drain)
    events = call(
        client, modelId="m", messages=[{"role": "user", "content": [{"text": "hi"}]}]
    )
    assert sent == ["B", "O"]
    assert any("messageStop" in event for event in events)


@pytest.mark.unit
def test_sync_decorator_does_not_retry_another_in_stream_validation_error(bedrock):
    make_agent, sent = bedrock
    client = _client(make_agent)
    make_agent.schedule[:] = list("VO")
    sent.clear()
    call = exponential_backoff_retry(max_retries=5)(_drain)
    with pytest.raises(ClientError, match="cache_control"):
        call(
            client,
            modelId="m",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )
    assert sent == ["V"]


@pytest.mark.unit
@pytest.mark.parametrize("code", ["ValidationException", _IN_STREAM_VALIDATION])
def test_the_rule_by_code_and_message(code):
    def error(message: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")

    assert not is_unretryable_validation_error(error(CONTENT_FILTER_BLOCK_MESSAGE))
    assert not is_unretryable_validation_error(
        error(CONTENT_FILTER_BLOCK_MESSAGE.upper())
    )
    assert is_unretryable_validation_error(error(_OTHER_VALIDATION))
    # Not a validation error at all: the rule has nothing to say.
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": _OTHER_VALIDATION}},
        "Converse",
    )
    assert not is_unretryable_validation_error(throttle)
