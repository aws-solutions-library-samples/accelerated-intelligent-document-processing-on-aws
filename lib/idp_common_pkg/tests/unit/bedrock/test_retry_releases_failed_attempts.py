# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A retry ladder does not keep a failed attempt's request alive (#1678).

The ladders in ``idp_common.bedrock.client`` used to retry by calling themselves from
inside the ``except`` block that caught the failure. While that nested call runs the
caught exception is still being handled, so it stays reachable — and with it its
traceback, every exception chained to it, and botocore's own ``error=`` and
``request=`` kwargs. Between them those reach the failed attempt's serialised request
body (botocore's ``request_dict`` in ``_make_api_call``, and the ``request`` local of
``httpsession.send`` for a timeout, which botocore rewraps from inside its own
``except``). For the standalone confidence step that body is every page image of the
section, base64-encoded, so each retry kept one more full copy. Measured offline on
the 17-page, 800-row benchmark statement with the default ten concurrent confidence
batches: peak RSS about 1 GB with no retries and 3.1 GB with six throttled attempts
per call, against a 4,096 MB function. The LambdaHook ladder also held the serialised
payload in a local of the frame it recursed from.

**What is asserted, and with what instrument.** The property is "when a ladder begins
its next attempt, everything an earlier attempt built has been freed". Each stub
transport below builds a request-sized buffer and fails the way the real transport
does — holding the buffer in the raising frame, in the frame of an inner exception
botocore rewraps, or in an exception's ``request=`` kwarg — and, as botocore does,
leaves the raising frame and its exception in a reference cycle. ``tracemalloc`` reads
the traced Python heap at the start of every attempt with the cyclic collector
disabled, so memory counts as freed only if reference counting freed it: cyclic
garbage is real memory until the collector next reaches it. A ladder that retains attempts grows by at least
one request per retry; one that does not stays flat. The bound is half of one
request, so it cannot be met by a ladder that keeps even one earlier attempt.

**Closure, and exactly what it checks.** The ladders are the functions in
``client.py`` that call themselves by name (``self.f``, ``BedrockClient.f(self, …)``,
``type(self).f``, a bare ``f``).
:func:`test_no_ladder_retries_from_inside_an_except_block` fails if an ``except``
handler of a ladder contains a call by that name, or contains any ``return`` at all —
a handler only records the decision, so a ``return`` there is a retry (or some other
exit) spelled in a way the name match cannot see, such as ``getattr``, an alias or
``functools.partial``. :func:`test_every_retrying_handler_has_a_route` derives every
handler of a ladder that can finish without raising, and fails if no route below
drives it. **Not covered:** a retry started inside a handler through an alias whose
result is carried out by assignment, on a branch no route drives; and a ladder that
retries in a loop from inside its ``except`` block, which is not a self-call. The loop
ladders in ``utils/bedrock_utils.py`` and ``bedrock/openai_responses.py`` start each
attempt after the previous handler has exited.
"""

from __future__ import annotations

import ast
import gc
import io
import json
import traceback
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from urllib3.exceptions import ProtocolError as Urllib3ProtocolError
from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeoutError

from idp_common.bedrock import client as client_mod
from idp_common.bedrock.client import BedrockClient
from idp_common.lambda_deadline import set_lambda_deadline_epoch

#: Size of the request each stub attempt builds. Large against everything else a
#: ladder allocates per attempt (log lines, metric calls, a deep-copied message
#: spine), so the half-a-request bound below is far outside the noise.
REQUEST_BYTES = 4 * 1024 * 1024

#: Retries driven on each multi-retry route. Four retained attempts would be
#: 16 MB of growth against a 2 MB bound.
RETRIES = 4

_URL = "https://bedrock-runtime.us-east-1.amazonaws.com"

#: An outcome receives a one-element list holding the attempt's request and decides
#: where that request ends up referenced from when it fails.
Outcome = Callable[[list], Any]


class _Transport:
    """A stand-in for one botocore operation that fails on a schedule.

    Installed on a plain namespace, never on a ``MagicMock``: a mock records every
    call's arguments in ``call_args_list``, which would itself keep each attempt's
    request alive and make this test measure the mock.

    The request is handed to the outcome in a list rather than bound to a local
    here, so this frame holds nothing: what keeps the request reachable is decided
    by the outcome alone, which is what lets one outcome model "held by the frame
    that raised" and another "held only through an inner exception".
    """

    def __init__(self, outcomes: list[Outcome]) -> None:
        self.outcomes = outcomes
        self.reachable_at_attempt: list[int] = []

    def __call__(self, **_kwargs: Any) -> Any:
        self.reachable_at_attempt.append(tracemalloc.get_traced_memory()[0])
        outcome = self.outcomes[len(self.reachable_at_attempt) - 1]
        return outcome([bytes(REQUEST_BYTES)])


def _raise(make_exc: Callable[[], BaseException]) -> Outcome:
    """botocore's ``_make_api_call`` shape: the raising frame holds the request.

    A fresh exception per attempt, as botocore raises: re-raising one shared
    instance would extend its own ``__traceback__`` on every raise, and the test
    would then measure its fixture.
    """

    def _make_api_call(box: list) -> Any:
        request_dict = {"body": box.pop()}  # noqa: F841 - held by this frame
        # botocore's ``Endpoint._send_request`` holds the exception it raises in a
        # local, so frame and exception reference each other: a cycle, which
        # reference counting alone never frees.
        exception = make_exc()
        raise exception

    return _make_api_call


def _send_raising(inner: BaseException) -> Callable[[list], None]:
    def _urlopen(box: list) -> None:
        request = box.pop()  # noqa: F841 - held only by this frame
        raise inner

    return _urlopen


def _rewrapped_timeout(wrapper: type[BotoCoreError]) -> Outcome:
    """``botocore.httpsession.send``'s shape for a timeout.

    urllib3 raises in a frame holding the request; botocore catches it and raises its
    own exception with ``error=<the urllib3 exception>`` from inside the ``except``.
    So the request is reachable only through the inner exception's traceback — via
    ``kwargs['error']`` and ``__context__`` — and not through the outer one's frames.
    """

    def _send(box: list) -> Any:
        try:
            _send_raising(
                Urllib3ReadTimeoutError(
                    None, _URL, "Read timed out. (read timeout=300)"
                )
            )(box)
        except Urllib3ReadTimeoutError as e:
            exception = wrapper(endpoint_url=_URL, error=e)  # the cycle, as above
            raise exception  # noqa: B904 - botocore's shape

    return _send


def _connection_closed() -> Outcome:
    """``httpsession.send``'s ``ProtocolError`` shape: the request rides in kwargs."""

    def _send(box: list) -> Any:
        request = SimpleNamespace(url=_URL, body=box.pop())
        try:
            raise Urllib3ProtocolError("Connection aborted.")
        except Urllib3ProtocolError as e:
            exception = ConnectionClosedError(
                error=e, request=request, endpoint_url=_URL
            )
            raise exception  # noqa: B904 - botocore's shape, cycle included

    return _send


def _return(value_factory: Callable[[], Any]) -> Outcome:
    def _invoke(box: list) -> Any:
        box.pop()
        return value_factory()

    return _invoke


def _throttle(op: str) -> BaseException:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, op
    )


def _denied(op: str) -> BaseException:
    """Non-retryable on every ladder: ends a route after the attempts it measures."""
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, op
    )


def _function_error() -> dict[str, Any]:
    """A LambdaHook response the ladder retries without any exception being raised."""
    return {
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps({"errorMessage": "boom"}).encode()),
    }


def _converse_params() -> dict[str, Any]:
    return {
        "modelId": "us.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"text": "assess this"}]}],
        "system": [{"text": "you assess"}],
        "inferenceConfig": {"maxTokens": 20000},
        "additionalModelRequestFields": {},
    }


def _run_converse(transport: _Transport) -> None:
    client = BedrockClient(region="us-east-1")
    client._client = SimpleNamespace(converse=transport)
    client._invoke_with_retry(
        model_id="us.amazon.nova-lite-v1:0",
        converse_params=_converse_params(),
        retry_count=0,
        max_retries=RETRIES,
        request_start_time=0.0,
    )


def _run_embedding(transport: _Transport) -> None:
    client = BedrockClient(region="us-east-1")
    client._client = SimpleNamespace(invoke_model=transport)
    client._generate_embedding_with_retry(
        model_id="amazon.titan-embed-text-v2:0",
        request_body=json.dumps({"inputText": "x"}),
        normalized_text="x",
        retry_count=0,
        max_retries=RETRIES,
    )


#: Allocated once, outside every measurement: the caller's own payload is shared by
#: every attempt by design and is not what the ladder must release.
_HOOK_PAYLOAD = {"messages": [{"content": [{"text": "x" * REQUEST_BYTES}]}]}


def _run_lambda_hook(transport: _Transport) -> None:
    client = BedrockClient(region="us-east-1")
    client._lambda_client = SimpleNamespace(invoke=transport)
    client._invoke_lambda_hook_with_retry(
        lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
        lambda_payload=_HOOK_PAYLOAD,
        retry_count=0,
        max_retries=RETRIES,
        request_start_time=0.0,
    )


_NOVA_OVER_LIMIT = (
    "The maximum tokens you requested exceeds the model limit of 10000. "
    "Try again with a maximum tokens value that is lower than 10000."
)
_CONVERSE_TIMEOUTS = (
    "(ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError, "
    "Urllib3ReadTimeoutError, RequestsReadTimeout, RequestsConnectTimeout)"
)

#: (ladder, where the retry is decided, route name) -> (runner, outcomes, expected
#: exception). The first two parts are what the closure test derives from the source;
#: "<try body>" is a retry decided without any exception being handled.
ROUTES: dict[tuple[str, str, str], tuple[Callable[[_Transport], None], list, type]] = {
    ("_invoke_with_retry", "ClientError", "throttle"): (
        _run_converse,
        [_raise(lambda: _throttle("Converse"))] * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_with_retry", "ClientError", "max-tokens clamp"): (
        _run_converse,
        [
            _raise(
                lambda: ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": _NOVA_OVER_LIMIT,
                        }
                    },
                    "Converse",
                )
            ),
            _raise(lambda: _denied("Converse")),
        ],
        ClientError,
    ),
    ("_invoke_with_retry", _CONVERSE_TIMEOUTS, "read timeout, rewrapped"): (
        _run_converse,
        [_rewrapped_timeout(ReadTimeoutError)] * (RETRIES + 1),
        ReadTimeoutError,
    ),
    ("_invoke_with_retry", _CONVERSE_TIMEOUTS, "connect timeout, rewrapped"): (
        _run_converse,
        [_rewrapped_timeout(ConnectTimeoutError)] * (RETRIES + 1),
        ConnectTimeoutError,
    ),
    ("_generate_embedding_with_retry", "ClientError", "throttle"): (
        _run_embedding,
        [_raise(lambda: _throttle("InvokeModel"))] * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_lambda_hook_with_retry", "<try body>", "function error"): (
        _run_lambda_hook,
        [_return(_function_error)] * (RETRIES + 1),
        RuntimeError,
    ),
    ("_invoke_lambda_hook_with_retry", "ClientError", "throttle"): (
        _run_lambda_hook,
        [
            _raise(
                lambda: ClientError(
                    {"Error": {"Code": "TooManyRequestsException", "Message": "x"}},
                    "Invoke",
                )
            )
        ]
        * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_lambda_hook_with_retry", "Exception", "read timeout, rewrapped"): (
        _run_lambda_hook,
        [_rewrapped_timeout(ReadTimeoutError)] * (RETRIES + 1),
        ReadTimeoutError,
    ),
    (
        "_invoke_lambda_hook_with_retry",
        "Exception",
        "connection closed, request kwarg",
    ): (
        _run_lambda_hook,
        [_connection_closed()] * (RETRIES + 1),
        ConnectionClosedError,
    ),
}


@pytest.fixture(autouse=True)
def _no_sleep_no_deadline():
    set_lambda_deadline_epoch(None)
    with (
        patch.object(BedrockClient, "_backoff_within_invocation", lambda self, n: 0.0),
        patch("idp_common.metrics.put_metric"),
    ):
        yield
    set_lambda_deadline_epoch(None)


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES), ids=lambda r: f"{r[0]}:{r[2]}")
def test_a_failed_attempt_is_freed_before_the_next_one_begins(route):
    runner, outcomes, expected = ROUTES[route]
    transport = _Transport(list(outcomes))
    # The cyclic collector is off for the run, so memory counts as released only if
    # reference counting frees it. Garbage left in a cycle is real memory until the
    # next full collection — botocore leaves exactly such a cycle behind a failed
    # attempt — so "unreachable" alone is not the bar.
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        with pytest.raises(expected):
            runner(transport)
    finally:
        tracemalloc.stop()
        gc.enable()

    reachable = transport.reachable_at_attempt
    # Non-vacuity: the route really retried, so there was an earlier attempt to keep.
    assert len(reachable) == len(outcomes) >= 2, (
        f"route made {len(reachable)} attempt(s) of {len(outcomes)} scheduled; it "
        "must retry at least once for this test to say anything"
    )
    growth = [r - reachable[0] for r in reachable]
    assert max(growth) < REQUEST_BYTES // 2, (
        f"memory still held grew by {[round(g / 2**20, 1) for g in growth]} MB across "
        f"attempts of {REQUEST_BYTES / 2**20:.0f} MB requests: a failed attempt's "
        "request is still alive while the ladder retries (#1678)"
    )


@pytest.mark.unit
def test_the_rewrapped_timeout_route_can_see_an_inner_exception():
    """The timeout routes would be vacuous if the request rode in the outer frames.

    Clearing only the outer exception's traceback — the first version of this fix —
    must not satisfy them, so check that it does not: with the outer traceback gone,
    the request is still reachable through ``kwargs['error']``.
    """
    tracemalloc.start()
    try:
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        try:
            _rewrapped_timeout(ReadTimeoutError)([bytes(REQUEST_BYTES)])
        except ReadTimeoutError as e:
            e.__traceback__ = None
            kept = e
        gc.collect()
        held = tracemalloc.get_traced_memory()[0] - before
        del kept
        gc.collect()
        released = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert held >= REQUEST_BYTES, held
    assert released < REQUEST_BYTES // 2, released


def _self_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Every call in ``fn`` whose callee is named ``fn``, whatever the receiver.

    ``self.f(...)``, ``BedrockClient.f(self, ...)``, ``type(self).f(self, ...)`` and a
    bare ``f(...)`` all count. A call through ``getattr``, an alias or a ``partial``
    does not; the no-``return`` rule below is what covers those.
    """
    calls = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else target.id
            if isinstance(target, ast.Name)
            else None
        )
        if name == fn.name:
            calls.append(node)
    return calls


def _ladders() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(Path(client_mod.__file__).read_text(encoding="utf-8"))
    found = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _self_calls(fn)
    ]
    assert found, "no self-calling functions found in client.py — derivation broken"
    return found


def _within(node: ast.AST, container: ast.AST) -> bool:
    return any(n is node for n in ast.walk(container))


@pytest.mark.unit
def test_no_ladder_retries_from_inside_an_except_block():
    offenders = []
    for fn in _ladders():
        handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
        for call in _self_calls(fn):
            for h in handlers:
                if _within(call, h):
                    offenders.append(f"{fn.name} line {call.lineno}: self-call")
        for h in handlers:
            for node in ast.walk(h):
                if isinstance(node, ast.Return):
                    offenders.append(f"{fn.name} line {node.lineno}: return")
    assert not offenders, (
        "a retry is started from inside an except block, which keeps the failed "
        f"attempt's exception and request reachable for the rest of the ladder: "
        f"{offenders} (#1678)"
    )


def _can_complete(stmts: list[ast.stmt]) -> bool:
    """Whether a block can finish without raising (conservative: True if unsure)."""
    if not stmts:
        return True
    last = stmts[-1]
    if isinstance(last, ast.Raise):
        return False
    if isinstance(last, ast.If):
        return _can_complete(last.body) or _can_complete(last.orelse)
    return True


def _retrying_handlers() -> set[tuple[str, str]]:
    """(ladder, handler) for every handler of a self-calling function that can
    complete without raising — the handlers that can decide to retry — plus
    "<try body>" where a self-call sits in a ``try`` body."""
    found: set[tuple[str, str]] = set()
    for fn in _ladders():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                if _can_complete(h.body):
                    found.add(
                        (fn.name, ast.unparse(h.type) if h.type else "<bare except>")
                    )
            for call in _self_calls(fn):
                if any(_within(call, stmt) for stmt in node.body):
                    found.add((fn.name, "<try body>"))
    return found


@pytest.mark.unit
def test_every_retrying_handler_has_a_route():
    """Universe closure: a handler that can retry and no route drives is unmeasured."""
    found = _retrying_handlers()
    routed = {(ladder, where) for ladder, where, _name in ROUTES}
    assert found, "no retrying handlers found — the derivation is broken"
    assert found == routed, (
        f"handlers that can retry with no route: {sorted(found - routed)}; "
        f"routes naming no handler: {sorted(routed - found)}"
    )


def _inner_cycle(box: list) -> BaseException:
    """An exception left in a reference cycle with the frame that raised it,
    holding the request — the shape of the urllib3 exception botocore rewraps."""

    def _urlopen() -> None:
        request = box.pop()  # noqa: F841 - held only by this frame
        exception = Urllib3ReadTimeoutError(None, _URL, "Read timed out.")
        raise exception

    try:
        _urlopen()
    except Urllib3ReadTimeoutError as e:
        return e
    raise AssertionError("unreachable")


def _linked_only_by_kwargs(box: list) -> BaseException:
    return ReadTimeoutError(endpoint_url=_URL, error=_inner_cycle(box))


def _linked_only_by_context(box: list) -> BaseException:
    inner = _inner_cycle(box)
    outer = RuntimeError("wrapper")
    outer.__context__ = inner
    return outer


def _linked_only_by_cause(box: list) -> BaseException:
    inner = _inner_cycle(box)
    outer = RuntimeError("wrapper")
    outer.__cause__ = inner
    outer.__context__ = None
    return outer


@pytest.mark.unit
@pytest.mark.parametrize(
    "build",
    [_linked_only_by_kwargs, _linked_only_by_context, _linked_only_by_cause],
    ids=["kwargs", "context", "cause"],
)
def test_dropping_a_failed_attempt_frees_every_linked_exception(build):
    """Each link ``_drop_failed_attempt`` follows is load-bearing on its own.

    botocore's rewrapped timeout reaches the urllib3 exception by two links at once
    (``kwargs['error']`` and ``__context__``), so the ladder routes above cannot tell
    whether either is followed. Here each is the only link.
    """
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        exc = build([bytes(REQUEST_BYTES)])
        client_mod._drop_failed_attempt(exc)
        del exc
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
        gc.enable()
    assert held < REQUEST_BYTES // 2, (
        f"{held / 2**20:.1f} MB still held after dropping the failed attempt"
    )


def _primary_failure() -> None:
    raise ValueError("the caller's own failure")


@pytest.mark.unit
@pytest.mark.parametrize(
    "runner, first, second",
    [
        (_run_converse, lambda: _throttle("Converse"), lambda: _denied("Converse")),
        (
            _run_embedding,
            lambda: _throttle("InvokeModel"),
            lambda: _denied("InvokeModel"),
        ),
        (
            _run_lambda_hook,
            lambda: OSError("connection reset by peer"),
            lambda: _denied("Invoke"),
        ),
    ],
    ids=["converse", "embedding", "lambda_hook"],
)
def test_a_retry_leaves_the_callers_own_exception_alone(runner, first, second):
    """A caller may call the client from inside its own ``except`` block — a
    fallback — and re-raise its exception afterwards. Every exception the ladder
    raises then chains to the caller's through ``__context__``, so a release that
    followed that link without a stop would wipe the caller's traceback."""
    transport = _Transport([_raise(first), _raise(second)])
    frames_before = frames_after = -1
    try:
        _primary_failure()
    except ValueError as caller_exc:
        frames_before = len(traceback.extract_tb(caller_exc.__traceback__))
        with pytest.raises(BaseException):
            runner(transport)
        frames_after = len(traceback.extract_tb(caller_exc.__traceback__))
    assert len(transport.reachable_at_attempt) == 2, "the route must have retried"
    assert frames_before >= 2
    assert frames_after == frames_before, (
        f"the caller's traceback went from {frames_before} to {frames_after} frames"
    )
