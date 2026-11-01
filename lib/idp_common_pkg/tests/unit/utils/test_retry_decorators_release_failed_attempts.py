# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The async retry decorator frees a failed attempt before the next one (#1706).

``async_exponential_backoff_retry`` wraps the agentic extraction call. It retries in
a loop, so each attempt starts after the previous handler has exited and the failure
is unreachable by then. That is not enough: a read timeout or a dropped connection
leaves botocore's reference cycle behind it (``Endpoint._send_request`` holds the
exception it raises in a local), so the failed attempt's serialised request stays in
memory until the cyclic collector next runs.

**Instrument.** A real ``bedrock-runtime`` client with only urllib3's ``urlopen``
replaced, so the exceptions, frames and the cycle are botocore's own. ``tracemalloc``
reads the traced heap at the start of every attempt with the cyclic collector off.
Measured before the fix: 8, 16, 24 and 32 MB still held at attempts 2-5 with an
8 MB request. A botocore throttle leaves no cycle and measured flat, so it is the
control here: if it ever grows, the instrument rather than the decorator changed.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import traceback
import tracemalloc
from typing import Callable

import boto3
import pytest
import urllib3
from botocore.config import Config
from botocore.httpsession import URLLib3Session
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.utils.bedrock_utils import async_exponential_backoff_retry

#: Captured at import, before the suite's no-outbound fixture replaces it. Restored
#: only while ``urlopen`` is replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send

REQUEST_BYTES = 8 * 1024 * 1024
FAILURES = 4

_OK = {
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    "metrics": {"latencyMs": 1},
}


def _json_response(status: int, body: dict, headers: dict) -> urllib3.HTTPResponse:
    return urllib3.HTTPResponse(
        body=io.BytesIO(json.dumps(body).encode()),
        status=status,
        headers={"content-type": "application/json", **headers},
        preload_content=False,
    )


def _read_timeout(pool, url):
    raise ReadTimeoutError(pool, url, "Read timed out. (read timeout=600)")


def _dropped(_pool, _url):
    raise ProtocolError("Connection aborted.", ConnectionResetError(104))


def _throttled(_pool, _url):
    return _json_response(
        429,
        {"__type": "ThrottlingException", "message": "slow down"},
        {"x-amzn-ErrorType": "ThrottlingException"},
    )


class _StrandsShapedError(Exception):
    """A wrapper raised ``from`` botocore's error. Strands wraps a transport failure
    this way (``EventLoopException``) on every model call after a tool use; how that
    reaches the decorator is measured in ``test_async_retry_transport_failures.py``."""


_FAILURES: dict[str, tuple[Callable, bool]] = {
    "read timeout": (_read_timeout, False),
    "read timeout, wrapped": (_read_timeout, True),
    "dropped connection": (_dropped, False),
    "dropped connection, wrapped": (_dropped, True),
    "throttle (control)": (_throttled, False),
}


@pytest.fixture
def runtime(monkeypatch):
    marks: list[int] = []
    state = {"fail": _read_timeout}

    def urlopen(pool, method, url, *args, **kwargs):
        marks.append(tracemalloc.get_traced_memory()[0])
        if len(marks) <= FAILURES:
            return state["fail"](pool, url)
        return _json_response(200, _OK, {})

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
    )
    set_lambda_deadline_epoch(None)
    yield client, marks, state
    set_lambda_deadline_epoch(None)


def _decorated(client, wrapped: bool):
    params = {
        "modelId": "us.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"text": "x" * REQUEST_BYTES}]}],
    }

    @async_exponential_backoff_retry(max_retries=FAILURES + 1, initial_delay=0.0)
    async def call():
        try:
            return client.converse(**params)
        except Exception as e:
            if wrapped:
                raise _StrandsShapedError(str(e)) from e
            raise

    return call


@pytest.mark.unit
@pytest.mark.parametrize("failure", sorted(_FAILURES))
def test_a_failed_attempt_is_freed_before_the_next_one_begins(runtime, failure):
    client, marks, state = runtime
    state["fail"], wrapped = _FAILURES[failure]
    call = _decorated(client, wrapped)
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        # The decorator's minimum backoff is 0.1 s; the sleeps are real but short.
        result = asyncio.run(call())
    finally:
        tracemalloc.stop()
        gc.enable()
    assert result["stopReason"] == "end_turn"
    assert len(marks) == FAILURES + 1, f"{len(marks)} attempts: the route must retry"
    growth = [m - marks[0] for m in marks]
    assert max(growth) < REQUEST_BYTES // 2, (
        f"{failure}: memory still held grew by "
        f"{[round(g / 2**20, 1) for g in growth]} MB across attempts of "
        f"{REQUEST_BYTES / 2**20:.0f} MB requests (#1706)"
    )


@pytest.mark.unit
def test_a_retry_leaves_the_callers_own_exception_alone(runtime):
    """Awaited from inside the caller's ``except`` block, the decorator's failures
    chain to the caller's exception; releasing them must stop there."""
    client, marks, _state = runtime
    call = _decorated(client, wrapped=False)

    async def caller():
        try:
            raise ValueError("the caller's own failure")
        except ValueError as caller_exc:
            before = len(traceback.extract_tb(caller_exc.__traceback__))
            await call()
            return before, len(traceback.extract_tb(caller_exc.__traceback__))

    before, after = asyncio.run(caller())
    assert len(marks) == FAILURES + 1, "the route must have retried"
    assert before >= 1
    assert after == before, f"the caller's traceback went from {before} to {after}"
