# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the retry ladders in ``idp_common.bedrock.client`` retry, and how often (#1706).

**A dropped connection is retried like a read timeout.** The exception is not built
here: a real ``bedrock-runtime`` client is driven with only urllib3's ``urlopen``
replaced, so botocore's own ``httpsession.send`` turns urllib3's ``ProtocolError``
into whatever it raises for a closed connection, and the test asserts that the
ladder retries *that*. A botocore upgrade that changed the class would then be
measured rather than assumed.
"""

from __future__ import annotations

import ast
import io
import itertools
import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
import urllib3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.httpsession import URLLib3Session
from urllib3.exceptions import ProtocolError

from idp_common.bedrock import client as client_mod
from idp_common.bedrock.client import BedrockClient
from idp_common.lambda_deadline import set_lambda_deadline_epoch

#: botocore's real transport, captured at import — before the suite's autouse
#: no-outbound fixture replaces it for each test. The test below needs botocore's own
#: ``send``, because that is where a urllib3 failure is rewrapped into botocore's
#: exception; it puts the real ``send`` back only while urllib3's ``urlopen`` is
#: replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send

_CONVERSE_OK = {
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    "metrics": {"latencyMs": 1},
}


@pytest.fixture(autouse=True)
def _no_sleep_no_deadline():
    set_lambda_deadline_epoch(None)
    with patch.object(BedrockClient, "_backoff_within_invocation", lambda self, n: 0.0):
        yield
    set_lambda_deadline_epoch(None)


def _recording_client(metrics: Counter) -> BedrockClient:
    client = BedrockClient(region="us-east-1")
    client._put_metric = lambda name, *a, **k: metrics.update([name])  # type: ignore[method-assign]
    return client


def _botocore_runtime_client():
    """A real client: real serialisation, real error wrapping, one botocore attempt
    (the setting ``BedrockClient.client`` uses, so this ladder is the only one)."""
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
    )


@pytest.mark.unit
@pytest.mark.parametrize("drops", [1, 3])
def test_a_dropped_connection_is_retried_like_a_read_timeout(drops, monkeypatch):
    sent: list[str] = []
    raised: list[BaseException] = []

    def urlopen(pool, method, url, *args, **kwargs):
        sent.append(url)
        if len(sent) <= drops:
            raise ProtocolError("Connection aborted.", ConnectionResetError(104))
        return urllib3.HTTPResponse(
            body=io.BytesIO(json.dumps(_CONVERSE_OK).encode()),
            status=200,
            headers={"content-type": "application/json"},
            preload_content=False,
        )

    runtime = _botocore_runtime_client()

    def record_failure(caught_exception=None, **_kwargs):
        if caught_exception is not None:
            raised.append(caught_exception)

    # botocore announces every attempt's outcome on this event, so what the ladder
    # was handed is read from botocore rather than inferred from the stub.
    runtime.meta.events.register("needs-retry.bedrock-runtime.Converse", record_failure)

    metrics: Counter = Counter()
    client = _recording_client(metrics)
    client._client = runtime
    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    result = client._invoke_with_retry(
        model_id="us.amazon.nova-lite-v1:0",
        converse_params={
            "modelId": "us.amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "system": [{"text": "s"}],
            "inferenceConfig": {"maxTokens": 10},
            "additionalModelRequestFields": {},
        },
        retry_count=0,
        max_retries=4,
        request_start_time=0.0,
    )

    # The failure botocore produced is the one botocore names for a closed connection.
    assert [type(e).__name__ for e in raised] == ["ConnectionClosedError"] * drops
    assert len(sent) == drops + 1, (
        f"{len(sent)} sends for {drops} dropped connection(s)"
    )
    assert result["response"]["stopReason"] == "end_turn"
    assert metrics["BedrockTimeouts"] == drops
    assert metrics["BedrockUnexpectedErrors"] == 0
    assert metrics["BedrockRequestsFailed"] == 0


# --- LambdaHook ladder: the retry bound holds per request --------------------------
#
# Failure kinds the hook ladder retries, one per route it has: a function error the
# hook reports in its response (decided in the try body), a throttle (the
# ``ClientError`` handler), and a read timeout (the ``Exception`` handler). Each
# kind maps to what the ladder raises when that kind is the last attempt.
_HOOK_ARN = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-hook"


def _hook_function_error() -> dict:
    return {
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps({"errorMessage": "boom"}).encode()),
    }


def _hook_throttle() -> dict:
    raise ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "slow"}}, "Invoke"
    )


def _hook_read_timeout() -> dict:
    raise ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")


def _hook_success() -> dict:
    return {"Payload": io.BytesIO(json.dumps({"output": {}, "usage": {}}).encode())}


_HOOK_FAILURES = {
    "F": (_hook_function_error, RuntimeError),
    "T": (_hook_throttle, ClientError),
    "R": (_hook_read_timeout, ReadTimeoutError),
}
_HOOK_MAX_RETRIES = 3


def _run_hook(schedule: str, metrics: Counter) -> tuple[list[str], object]:
    """Drive the hook ladder over ``schedule`` (letters of ``_HOOK_FAILURES``, then
    ``S`` for success). Returns the kinds actually invoked and the result or the
    exception raised. An attempt past the end of the schedule is a test failure,
    so a ladder that over-retries cannot loop."""
    made: list[str] = []

    def invoke(**_kwargs):
        if len(made) >= len(schedule):
            raise AssertionError(f"invocation {len(made) + 1} past {schedule!r}")
        kind = schedule[len(made)]
        made.append(kind)
        return _hook_success() if kind == "S" else _HOOK_FAILURES[kind][0]()

    client = _recording_client(metrics)
    client._lambda_client = SimpleNamespace(invoke=invoke)
    try:
        outcome: object = client._invoke_lambda_hook_with_retry(
            lambda_arn=_HOOK_ARN,
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=_HOOK_MAX_RETRIES,
            request_start_time=0.0,
        )
    except Exception as e:  # noqa: BLE001 - the outcome under test
        outcome = e
    return made, outcome


def _every_schedule(length: int) -> list[str]:
    return ["".join(p) for p in itertools.product(_HOOK_FAILURES, repeat=length)]


@pytest.mark.unit
def test_an_exhausted_hook_ladder_makes_max_retries_plus_one_invocations():
    """Every sequence of retryable failures, in every order, ends after exactly
    ``max_retries + 1`` invocations and raises the last attempt's failure.

    Exhaustive over the three failure routes rather than sampled, because the defect
    was an interaction between routes: a function error retried from the try body,
    followed by a failure the nested ladder then raised back into the outer frame's
    handlers, which retried it again on their own count (#1706)."""
    schedules = _every_schedule(_HOOK_MAX_RETRIES + 1)
    assert len(schedules) == 3 ** (_HOOK_MAX_RETRIES + 1)
    wrong = []
    for schedule in schedules:
        # Padded with failures, so an over-retrying ladder is caught by the count
        # rather than by running off the end of the schedule.
        made, outcome = _run_hook(schedule + "F" * 8, Counter())
        expected = _HOOK_FAILURES[schedule[-1]][1]
        if len(made) != _HOOK_MAX_RETRIES + 1 or type(outcome) is not expected:
            wrong.append(f"{schedule}: {''.join(made)} -> {type(outcome).__name__}")
    assert not wrong, (
        f"{len(wrong)} of {len(schedules)} schedules broke the retry bound of "
        f"{_HOOK_MAX_RETRIES + 1} invocations; first: {wrong[:5]}"
    )


@pytest.mark.unit
def test_a_hook_ladder_that_recovers_stops_at_the_first_success():
    wrong = []
    for failures in range(_HOOK_MAX_RETRIES + 1):
        for prefix in _every_schedule(failures):
            made, outcome = _run_hook(prefix + "S", Counter())
            if "".join(made) != prefix + "S" or not isinstance(outcome, dict):
                wrong.append(f"{prefix}S: {''.join(made)} -> {type(outcome).__name__}")
    assert not wrong, wrong[:5]


# --- LambdaHook ladder: one failed request is one LambdaHookRequestsFailed ---------


def _hook_denied() -> dict:
    raise ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Invoke"
    )


def _hook_handled_function_error() -> dict:
    return {
        "FunctionError": "Handled",
        "Payload": io.BytesIO(json.dumps({"errorMessage": "bad input"}).encode()),
    }


def _hook_transport_runtime_error() -> dict:
    raise RuntimeError("not retried: the ladder reserves RuntimeError for itself")


def _run_hook_once(
    first: Callable[[], dict], out_of_time: bool, metrics: Counter
) -> int:
    """One request whose first attempt fails with ``first``; a retry, if the ladder
    makes one, fails non-retryably so the request still ends in one failure."""
    made: list[Callable[[], dict]] = []

    def invoke(**_kwargs):
        step = first if not made else _hook_denied
        made.append(step)
        return step()

    client = _recording_client(metrics)
    client._lambda_client = SimpleNamespace(invoke=invoke)
    with patch.object(client_mod, "attempt_cannot_finish", lambda *a: out_of_time):
        with pytest.raises(Exception):
            client._invoke_lambda_hook_with_retry(
                lambda_arn=_HOOK_ARN,
                lambda_payload={"messages": []},
                retry_count=0,
                max_retries=_HOOK_MAX_RETRIES,
                request_start_time=0.0,
            )
    return len(made)


#: Every way a request can end in failure that is not exhaustion (which the
#: exhaustive schedule test below covers): refused for time on each retrying route,
#: and refused outright on each non-retrying one. ``test_every_hook_failure_exit_is_
#: driven`` derives the ladder's raise sites from its source and fails if these
#: routes, with the schedules, leave one undriven.
_ONE_SHOT_FAILURES = {
    "function error, out of time": (_hook_function_error, True),
    "throttle, out of time": (_hook_throttle, True),
    "read timeout, out of time": (_hook_read_timeout, True),
    "function error, handled": (_hook_handled_function_error, False),
    "client error, not retryable": (_hook_denied, False),
    "runtime error from the transport": (_hook_transport_runtime_error, False),
}


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(_ONE_SHOT_FAILURES))
def test_a_request_refused_on_its_first_attempt_counts_one_failure(route):
    first, out_of_time = _ONE_SHOT_FAILURES[route]
    metrics: Counter = Counter()
    attempts = _run_hook_once(first, out_of_time, metrics)
    assert attempts == 1, f"{route}: {attempts} attempts"
    assert metrics["LambdaHookRequestsFailed"] == 1, (
        f"{route}: one failed request emitted LambdaHookRequestsFailed "
        f"{metrics['LambdaHookRequestsFailed']} times (#1706)"
    )


@pytest.mark.unit
def test_an_exhausted_request_counts_one_failure_and_a_recovered_one_none():
    wrong = []
    for schedule in _every_schedule(_HOOK_MAX_RETRIES + 1):
        metrics: Counter = Counter()
        _run_hook(schedule, metrics)
        if metrics["LambdaHookRequestsFailed"] != 1:
            wrong.append(f"{schedule}: {metrics['LambdaHookRequestsFailed']}")
    for failures in range(_HOOK_MAX_RETRIES + 1):
        for prefix in _every_schedule(failures):
            metrics = Counter()
            _run_hook(prefix + "S", metrics)
            if metrics["LambdaHookRequestsFailed"] != 0:
                wrong.append(f"{prefix}S: {metrics['LambdaHookRequestsFailed']}")
    assert not wrong, (
        "LambdaHookRequestsFailed must be emitted once per failed request and never "
        f"for one that recovers (#1706); request: count -> {wrong[:6]}"
    )


def _hook_ladder_raise_lines() -> set[int]:
    """Line of every ``raise`` in the hook ladder, bar the unreachable fall-through."""
    tree = ast.parse(Path(client_mod.__file__).read_text(encoding="utf-8"))
    (fn,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_invoke_lambda_hook_with_retry"
    ]
    lines = {
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and "fell through" not in ast.unparse(n)
    }
    assert len(lines) >= 4, f"raise sites derivation looks broken: {sorted(lines)}"
    return lines


@pytest.mark.unit
def test_every_hook_failure_exit_is_driven():
    """Universe closure: every way out of the hook ladder is driven by a route above,
    so a new raise site cannot emit a second ``LambdaHookRequestsFailed`` — or none —
    without a test reaching it. Read by tracing the lines the ladder executes."""
    code = BedrockClient._invoke_lambda_hook_with_retry.__code__
    executed: set[int] = set()

    def tracer(frame, event, _arg):
        if frame.f_code is not code:
            return None

        def lines(f, ev, _a):
            if ev == "line":
                executed.add(f.f_lineno)
            return lines

        return lines

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        for first, out_of_time in _ONE_SHOT_FAILURES.values():
            _run_hook_once(first, out_of_time, Counter())
        for schedule in _every_schedule(_HOOK_MAX_RETRIES + 1):
            _run_hook(schedule, Counter())
    finally:
        sys.settrace(previous)
    undriven = _hook_ladder_raise_lines() - executed
    assert not undriven, f"hook ladder raise sites no route reaches: {sorted(undriven)}"
