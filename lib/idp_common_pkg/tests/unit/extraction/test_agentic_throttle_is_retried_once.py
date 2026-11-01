# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock throttling on the agentic extraction call is retried by one ladder (#1759).

``invoke_agent_with_retry`` is the ladder: it is bounded by a cumulative backoff
allowance and by the Lambda deadline. The Strands ``Agent`` under it used to run a
second one — Strands' default ``ModelRetryStrategy``, which sleeps 4 + 8 + 16 + 32 + 64 s
inside a single model call, restarts after every successful call, and reads neither
bound. Measured on the production construction below, before the fix: persistent
throttling slept 744 s inside Strands on top of the ladder's 90 s, and a throttle met
30 s from the deadline slept 124 s, past it.

**Instrument.** ``structured_output_async`` itself, called with
``strands_throttle_retries=False`` as the extraction callers call it, so the ``Agent``
and the ``BedrockModel`` are the ones production builds for extraction, over a real ``bedrock-runtime``
client with only urllib3's ``urlopen`` replaced; every request is answered with
Bedrock's throttling error. Time is virtual: ``asyncio.sleep`` — wherever it is called
from — advances a clock instead of waiting, and the ladder's two modules read that
clock. The rule asserted is about sleeping, not about which library sleeps: nothing in
the call may sleep longer than the ladder allows. botocore's adaptive-mode rate limiter
is replaced by one that grants every request at once; it waits in real time on its own
clock, and it is not the retry being measured.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
import urllib3
from botocore.httpsession import URLLib3Session
from botocore.retries import bucket
from pydantic import BaseModel
from strands.types.exceptions import ModelThrottledException

import idp_common.lambda_deadline as lambda_deadline
import idp_common.utils.bedrock_utils as bedrock_utils
from idp_common.extraction.agentic_idp import structured_output_async
from idp_common.lambda_deadline import (
    DEADLINE_RESERVE_SECONDS,
    set_lambda_deadline_epoch,
)
from idp_common.timeout_budget import AGENT_MAX_TOTAL_BACKOFF_SECONDS

#: Captured at import, before the suite's no-outbound fixture replaces it. Restored
#: only while ``urlopen`` is replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send

#: Virtual seconds one throttled request costs.
_REQUEST_SECONDS = 1.0


class _Doc(BaseModel):
    name: str


@pytest.fixture
def throttled(monkeypatch):
    """Returns ``(clock, slept, sent)``: the virtual clock, every sleep requested, and
    every request sent."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    clock = [1_000_000.0]
    slept: list[float] = []
    sent: list[str] = []

    def urlopen(pool, method, url, *args, **kwargs):
        sent.append(url)
        clock[0] += _REQUEST_SECONDS
        return urllib3.HTTPResponse(
            body=io.BytesIO(b'{"message":"Too many requests, please wait."}'),
            status=429,
            headers={
                "content-type": "application/json",
                "x-amzn-ErrorType": "ThrottlingException:http://internal.amazon.com/"
                "coral/com.amazon.bedrock/",
            },
            preload_content=False,
        )

    async def virtual_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        clock[0] += seconds

    virtual_time = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    monkeypatch.setattr(asyncio, "sleep", virtual_sleep)
    monkeypatch.setattr(lambda_deadline, "time", virtual_time)
    monkeypatch.setattr(bedrock_utils, "time", virtual_time)
    monkeypatch.setattr(bucket.TokenBucket, "acquire", lambda self, *a, **k: True)
    set_lambda_deadline_epoch(None)
    yield clock, slept, sent
    set_lambda_deadline_epoch(None)


def _extract():
    return asyncio.run(
        structured_output_async(
            model_id="us.amazon.nova-lite-v1:0",
            data_format=_Doc,
            prompt="extract",
            # What every agentic extraction caller passes; that they do is pinned by
            # agentic_idp/test_throttle_retry_ownership.py.
            strands_throttle_retries=False,
        )
    )


@pytest.mark.unit
def test_persistent_throttling_sleeps_no_longer_than_the_ladder_allows(throttled):
    _clock, slept, sent = throttled
    with pytest.raises(ModelThrottledException):
        _extract()
    # Non-vacuity: the throttle reached the ladder and was retried with backoff.
    assert len(sent) > 1 and slept, (sent, slept)
    # Every request after the first follows one of the ladder's backoffs: a retry
    # with no backoff (an in-call retrier set to zero delay) is a burst.
    assert len(sent) <= 1 + sum(1 for seconds in slept if seconds > 0), (
        f"{len(sent)} requests for {len(slept)} sleeps: {slept}"
    )
    # The ladder clamps its last sleep to the allowance left, so the sum lands on the
    # allowance up to float rounding.
    assert sum(slept) <= AGENT_MAX_TOTAL_BACKOFF_SECONDS + 1e-6, (
        f"{sum(slept):.0f} s asleep in {len(sent)} requests; the ladder allows "
        f"{AGENT_MAX_TOTAL_BACKOFF_SECONDS:.0f} s. Sleeps: {slept}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("seconds_left", [30.0, 100.0])
def test_a_throttle_near_the_deadline_does_not_sleep_past_it(throttled, seconds_left):
    clock, slept, sent = throttled
    start = clock[0]
    set_lambda_deadline_epoch(start + seconds_left)
    with pytest.raises(ModelThrottledException):
        _extract()
    assert sent, "no request was sent"
    # Every sleep began before the deadline less its reserve and ended by it: the
    # ladder clamps each sleep to what remains, so the total cannot exceed it.
    assert sum(slept) <= max(0.0, seconds_left - DEADLINE_RESERVE_SECONDS) + 1e-6, (
        f"{sum(slept):.0f} s asleep with {seconds_left:.0f} s to the deadline: {slept}"
    )
