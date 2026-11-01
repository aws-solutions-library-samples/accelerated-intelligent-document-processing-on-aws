# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agentic extraction call retries a dropped connection and a timeout (#1730).

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
through a stream is a different exception and is not covered here. Strands delivers
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

import pytest
import urllib3
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError
from botocore.httpsession import URLLib3Session
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
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


@tool
def ping(x: str) -> str:
    """Echo the argument."""
    return x


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

    def urlopen(pool, method, url, *args, **kwargs):
        step = schedule[len(sent)] if len(sent) < len(schedule) else "O"
        sent.append(step)
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

    def make_agent(steps: str) -> Agent:
        schedule[:] = list(steps)
        model = BedrockModel(
            model_id="us.amazon.nova-lite-v1:0",
            streaming=False,
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
