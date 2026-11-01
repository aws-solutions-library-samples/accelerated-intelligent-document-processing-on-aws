# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agentic extraction call retries a dropped connection and a timeout (#1730),
and a connection reset partway through a streamed response (#1745).

``invoke_agent_with_retry`` is wrapped by ``async_exponential_backoff_retry``. A
Bedrock connection closed before the response arrived reached it as botocore's
``ConnectionClosedError`` and was not retried at all; a read timeout was retried only
on the first model call of an invocation.

**Instrument.** A real Strands ``Agent`` over a real ``BedrockModel`` and a real
``bedrock-runtime`` client, with only urllib3's ``urlopen`` replaced. botocore turns
urllib3's failure into its own exception, and Strands delivers that exception in
whichever form it delivers it, so the tests below assert about forms the libraries
produce rather than forms built here. The model is non-streamed (``converse``) because
a JSON body is simpler to stub than an event stream; production streams
(``converse_stream``), and for a failure *before* the response arrives the same two
forms were measured there with hand-built event-stream responses. A failure partway
through a stream is a different exception — urllib3's ``ProtocolError``, which botocore
does not rewrap — and is covered by the streamed tests at the end of this file, which
build the event stream by hand. Strands delivers
two forms: raw, from the first
model call of an invocation, and wrapped in ``EventLoopException`` from the model
call of a cycle that follows a tool use. Measured before the fix, schedule by
schedule (``T`` a tool use, ``D`` a dropped connection, ``R`` a read timeout, ``O``
an answer): ``DO`` one send then ``ConnectionClosedError``; ``TDO`` and ``TRO`` two
sends then ``EventLoopException``; ``RO`` and ``TO`` recovered.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import zlib

import pytest
import urllib3
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError
from botocore.httpsession import URLLib3Session
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.decorator import DecoratedFunctionTool
from strands.tools.decorator import tool as _strands_tool
from strands.types.exceptions import EventLoopException
from urllib3.exceptions import ConnectTimeoutError, ProtocolError, ReadTimeoutError

import idp_common.utils.bedrock_utils as bedrock_utils
from idp_common.extraction.agentic_idp import invoke_agent_with_retry
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.utils.bedrock_utils import async_exponential_backoff_retry

#: Captured at import, before the suite's no-outbound fixture replaces it. Restored
#: only while ``urlopen`` is replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send


def _answer(stop_reason: str, content: list) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


_BODIES = {
    "T": _answer(
        "tool_use",
        [{"toolUse": {"toolUseId": "t1", "name": "ping", "input": {"x": "a"}}}],
    ),
    "O": _answer("end_turn", [{"text": "done"}]),
}


#: Every call the model's tool use reaches, so a test can count tool executions.
_TOOL_CALLS: list[str] = []


# The decorator is taken from its defining module, not as ``strands.tool``: three
# test modules elsewhere in this suite replace ``strands.tool`` with a passthrough at
# import and never restore it, and collected after any of them ``@tool`` here left
# ``ping`` a plain function the agent could not register. Every tool use then
# answered "tool not found" and these tests still passed.
@_strands_tool
def ping(x: str) -> str:
    """Echo the argument."""
    _TOOL_CALLS.append(x)
    return x


def _event_header(name: str, value: str) -> bytes:
    raw_name, raw_value = name.encode(), value.encode()
    # Header value type 7 is a string, length-prefixed with two bytes.
    return (
        bytes([len(raw_name)])
        + raw_name
        + bytes([7])
        + struct.pack(">H", len(raw_value))
        + raw_value
    )


def _event(event_type: str, payload: dict) -> bytes:
    """One ``application/vnd.amazon.eventstream`` message, framed the way botocore's
    ``EventStreamBuffer`` parses it: prelude (total and header length, CRC), headers,
    payload, message CRC."""
    headers = (
        _event_header(":event-type", event_type)
        + _event_header(":content-type", "application/json")
        + _event_header(":message-type", "event")
    )
    body = json.dumps(payload).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(body) + 4, len(headers))
    prelude += struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + body
    return message + struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)


def _stream_content(step: str) -> bytes:
    """Every event of a streamed answer except the trailing ``metadata``: for ``T`` a
    complete ``toolUse`` block, for ``O`` a text answer, each ending ``messageStop``."""
    events = [_event("messageStart", {"role": "assistant"})]
    if step == "T":
        events += [
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
        ]
    else:
        events += [
            _event(
                "contentBlockDelta", {"delta": {"text": "done"}, "contentBlockIndex": 0}
            ),
            _event("contentBlockStop", {"contentBlockIndex": 0}),
            _event("messageStop", {"stopReason": "end_turn"}),
        ]
    return b"".join(events)


_STREAM_METADATA = _event(
    "metadata",
    {
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    },
)


class _ResetAfter(io.RawIOBase):
    """A socket that delivers ``first`` and is then reset by the peer."""

    def __init__(self, first: bytes):
        self._first: bytes | None = first

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._first is None:
            raise ConnectionResetError(104, "Connection reset by peer")
        first, self._first = self._first, None
        buffer[: len(first)] = first
        return len(first)


#: Mid-stream schedule steps: the body delivered before the reset.
_MID_STREAM = {
    # Only ``messageStart``.
    "M": lambda: _event("messageStart", {"role": "assistant"}),
    # A complete ``toolUse`` block and ``messageStop``; only ``metadata`` is missing.
    "X": lambda: _stream_content("T"),
}


@pytest.fixture
def bedrock(monkeypatch):
    """Returns ``(make_agent, sent, raised)``: an Agent factory, the schedule steps
    sent so far, and every exception botocore announced for an attempt."""
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
    raised: list[BaseException] = []
    requests: list[dict] = []
    _TOOL_CALLS.clear()

    def urlopen(pool, method, url, *args, **kwargs):
        step = schedule[len(sent)] if len(sent) < len(schedule) else "O"
        sent.append(step)
        requests.append(json.loads(kwargs["body"]))
        if url.endswith("/converse-stream"):
            headers = {"content-type": "application/vnd.amazon.eventstream"}
            if step == "F":
                # The peer closes cleanly after a complete ``toolUse`` block, short of
                # the length it declared: urllib3 reports IncompleteRead.
                body = io.BytesIO(_stream_content("T"))
                headers["content-length"] = str(
                    len(_stream_content("T") + _STREAM_METADATA)
                )
            elif step in _MID_STREAM:
                body = _ResetAfter(_MID_STREAM[step]())
            elif step in "TO":
                body = io.BytesIO(_stream_content(step) + _STREAM_METADATA)
            else:
                body = None
            if body is not None:
                return urllib3.HTTPResponse(
                    body=body,
                    status=200,
                    headers=headers,
                    preload_content=False,
                    enforce_content_length=True,
                )
        if step == "D":
            raise ProtocolError("Connection aborted.", ConnectionResetError(104))
        if step == "R":
            raise ReadTimeoutError(pool, url, "Read timed out. (read timeout=600)")
        if step == "C":
            raise ConnectTimeoutError(pool, "Connect timed out.")
        return urllib3.HTTPResponse(
            body=io.BytesIO(json.dumps(_BODIES[step]).encode()),
            status=200,
            headers={"content-type": "application/json"},
            preload_content=False,
        )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    # The decorator's sleep only; the module reaches asyncio solely for it.
    monkeypatch.setattr(
        bedrock_utils,
        "asyncio",
        type("_NoSleep", (), {"sleep": staticmethod(no_sleep)}),
    )
    set_lambda_deadline_epoch(None)

    def record(caught_exception=None, **_kwargs):
        if caught_exception is not None:
            raised.append(caught_exception)

    def make_agent(steps: str, streaming: bool = False) -> Agent:
        assert isinstance(ping, DecoratedFunctionTool), "ping is not a Strands tool"
        schedule[:] = list(steps)
        model = BedrockModel(
            model_id="us.amazon.nova-lite-v1:0",
            streaming=streaming,
            boto_client_config=Config(
                retries={"total_max_attempts": 1, "mode": "standard"}
            ),
        )
        # botocore announces every attempt's outcome on this event, so the failure
        # is read from botocore rather than inferred from the stub.
        model.client.meta.events.register(
            "needs-retry.bedrock-runtime.Converse", record
        )
        return Agent(model=model, tools=[ping], callback_handler=None)

    make_agent.requests = requests  # pyright: ignore[reportFunctionMemberAccess]
    yield make_agent, sent, raised
    set_lambda_deadline_epoch(None)


#: schedule -> (botocore exception it produces, whether Strands wraps it)
_SCHEDULES = {
    "DO": ("ConnectionClosedError", False),
    "TDO": ("ConnectionClosedError", True),
    "DDDO": ("ConnectionClosedError", False),
    "TRO": ("ReadTimeoutError", True),
    "TCO": ("ConnectTimeoutError", True),
}


@pytest.mark.unit
@pytest.mark.parametrize("steps", sorted(_SCHEDULES))
def test_a_transport_failure_on_the_agentic_call_is_retried(bedrock, steps):
    make_agent, sent, raised = bedrock
    expected_error, _wrapped = _SCHEDULES[steps]
    result = asyncio.run(invoke_agent_with_retry([{"text": "hi"}], make_agent(steps)))

    failures = steps.count("D") + steps.count("R") + steps.count("C")
    assert [type(e).__name__ for e in raised] == [expected_error] * failures
    assert sent == list(steps), f"sends {sent} for schedule {steps}"
    assert result.stop_reason == "end_turn"


@pytest.mark.unit
@pytest.mark.parametrize("steps", sorted(_SCHEDULES))
def test_strands_delivers_the_failure_in_the_form_the_retry_must_recognise(
    bedrock, steps
):
    """The premise, measured with no decorator: which form reaches the caller. If
    Strands stops wrapping, or starts wrapping the first call, this goes red and the
    comment on ``_RETRYABLE_TRANSPORT_EXCEPTION_TYPES`` is out of date."""
    make_agent, _sent, _raised = bedrock
    expected_error, wrapped = _SCHEDULES[steps]
    agent = make_agent(steps)
    with pytest.raises(Exception) as caught:
        asyncio.run(agent.invoke_async([{"text": "hi"}]))
    error = caught.value
    if wrapped:
        assert type(error) is EventLoopException
        error = error.__cause__
    assert type(error).__name__ == expected_error


@pytest.mark.unit
def test_an_exhausted_ladder_raises_the_failure_it_last_met(bedrock):
    make_agent, sent, _raised = bedrock
    with pytest.raises(ConnectionClosedError):
        asyncio.run(invoke_agent_with_retry([{"text": "hi"}], make_agent("D" * 60)))
    # Bounded by the decorator's cumulative-delay budget, well short of max_retries.
    assert 1 < len(sent) < 50


def _decorated(raise_once):
    calls: list[int] = []

    @async_exponential_backoff_retry(max_retries=3, initial_delay=0.0)
    async def call():
        calls.append(1)
        if len(calls) == 1:
            raise_once()
        return "ok"

    return call, calls


def _closed() -> ConnectionClosedError:
    return ConnectionClosedError(endpoint_url="https://bedrock-runtime.example")


@pytest.mark.unit
def test_a_failure_merely_raised_while_handling_a_transport_error_is_not_retried(
    bedrock,
):
    """``__context__`` is not a cause: a deterministic failure raised inside an
    ``except`` for a dropped connection must not inherit its transience."""

    def raise_once():
        try:
            raise _closed()
        except ConnectionClosedError:
            raise ValueError("deterministic")  # noqa: B904 - the implicit context is the point

    call, calls = _decorated(raise_once)
    with pytest.raises(ValueError):
        asyncio.run(call())
    assert len(calls) == 1


@pytest.mark.unit
def test_a_failure_raised_from_a_transport_error_is_retried_at_any_depth(bedrock):
    def raise_once():
        inner = RuntimeError("middle")
        inner.__cause__ = _closed()
        raise RuntimeError("outer") from inner

    call, calls = _decorated(raise_once)
    assert asyncio.run(call()) == "ok"
    assert len(calls) == 2


@pytest.mark.unit
def test_a_cyclic_cause_chain_ends_the_walk(bedrock):
    def raise_once():
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        raise a

    call, calls = _decorated(raise_once)
    with pytest.raises(RuntimeError):
        asyncio.run(call())
    assert len(calls) == 1


#: Streamed schedule -> whether Strands wraps the mid-stream reset. ``M`` resets
#: after ``messageStart``, ``X`` after a complete ``toolUse`` block and
#: ``messageStop``, which is the latest point a reset can land before the stream ends.
#: ``F`` is a clean close short of the declared length, which urllib3 reports as
#: ``ProtocolError`` from ``IncompleteRead`` rather than from a connection error.
_MID_STREAM_SCHEDULES = {
    "MO": False,
    "XO": False,
    "FO": False,
    "TMO": True,
    "TXO": True,
    "TFO": True,
}


def _roles(messages: list[dict]) -> list[tuple[str, list[str]]]:
    return [
        (m["role"], [next(iter(block)) for block in m["content"]]) for m in messages
    ]


@pytest.mark.unit
@pytest.mark.parametrize("steps", sorted(_MID_STREAM_SCHEDULES))
def test_strands_keeps_nothing_from_a_stream_reset_partway(bedrock, steps):
    """The premise that makes retrying a mid-stream reset safe, measured with no
    decorator: the reset reaches the caller as urllib3's ``ProtocolError`` (raw, or as
    the ``__cause__`` of ``EventLoopException`` after a tool use), and Strands has
    neither run the tool the stream already described nor kept any part of the
    assistant message. If Strands starts acting on a partial stream, this goes red and
    retrying it would duplicate a tool call."""
    make_agent, _sent, _raised = bedrock
    agent = make_agent(steps, streaming=True)
    with pytest.raises(Exception) as caught:
        asyncio.run(agent.invoke_async([{"text": "hi"}]))
    error = caught.value
    if _MID_STREAM_SCHEDULES[steps]:
        assert type(error) is EventLoopException
        error = error.__cause__
    assert type(error) is ProtocolError
    tool_uses_before_the_reset = len(steps) - len(steps.lstrip("T"))
    assert len(_TOOL_CALLS) == tool_uses_before_the_reset
    expected = [("user", ["text"])] + [
        ("assistant", ["toolUse"]),
        ("user", ["toolResult"]),
    ] * tool_uses_before_the_reset
    assert _roles(agent.messages) == expected


@pytest.mark.unit
@pytest.mark.parametrize("steps", sorted(_MID_STREAM_SCHEDULES))
def test_a_stream_reset_partway_is_retried_without_repeating_anything(bedrock, steps):
    make_agent, sent, _raised = bedrock
    agent = make_agent(steps, streaming=True)
    result = asyncio.run(invoke_agent_with_retry([{"text": "hi"}], agent))

    assert result.stop_reason == "end_turn"
    assert sent == list(steps), f"sends {sent} for schedule {steps}"
    tool_uses = steps.count("T")
    assert len(_TOOL_CALLS) == tool_uses
    # The retry resumed the conversation the reset left: the request after the
    # reset is the request the reset interrupted, prompt included once.
    requests = make_agent.requests
    assert requests[-1]["messages"] == requests[-2]["messages"]
    assert _roles(agent.messages) == [("user", ["text"])] + [
        ("assistant", ["toolUse"]),
        ("user", ["toolResult"]),
    ] * tool_uses + [("assistant", ["text"])]
