# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A retry ladder does not keep a failed attempt's request alive (#1678).

Every ladder in ``idp_common.bedrock.client`` retries by calling itself from inside
the handler that caught the failure. The caught exception's ``__traceback__`` then
keeps every frame of the failed attempt reachable for the rest of the ladder — and
one of those frames is botocore's ``_make_api_call``, whose ``request_dict`` holds
the serialised request body. For the standalone confidence step that body is every
page image of the section, base64-encoded, so each throttled attempt kept one more
full copy of the request. Measured offline on the 17-page, 800-row benchmark
statement with the default ten concurrent confidence batches: peak RSS 1,029 MB with
no throttling, 2,167 MB with three throttled attempts per call and 3,085 MB with six,
against a 4,096 MB function. The LambdaHook ladder also held the serialised payload in
a local of the very frame it recursed from.

**What is asserted, and with what instrument.** The property is "when a ladder begins
its next attempt, nothing an earlier attempt built is still reachable". The stub
transport below allocates a request-sized buffer in its own frame, as botocore's
serialiser does, and then fails; ``tracemalloc`` reads the traced Python heap at the
start of every attempt, after a full ``gc.collect()`` so that only *reachable*
memory counts. A ladder that retains attempts grows by at least one request per
retry; one that does not stays flat. The bound is half of one request, so it cannot
be met by a ladder that keeps even one earlier attempt.

The routes are derived, not listed by hand:
:func:`test_every_recursive_retry_site_is_exercised` reads every self-recursive call
out of ``client.py`` and fails if one exists that no route below drives.
"""

from __future__ import annotations

import ast
import gc
import io
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

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


class _Transport:
    """A stand-in for one botocore operation that fails on a schedule.

    Installed on a plain namespace, never on a ``MagicMock``: a mock records every
    call's arguments in ``call_args_list``, which would itself keep each attempt's
    request alive and make this test measure the mock.

    Each call records how much traced memory is reachable as the attempt begins,
    then builds a request-sized buffer as a local of its own frame — the shape of
    botocore's ``request_dict`` — and either raises, returns, or (for the last
    scheduled outcome) raises ``final``.
    """

    def __init__(self, outcomes: list[Callable[[], Any]]) -> None:
        self.outcomes = outcomes
        self.reachable_at_attempt: list[int] = []

    def __call__(self, **_kwargs: Any) -> Any:
        gc.collect()
        self.reachable_at_attempt.append(tracemalloc.get_traced_memory()[0])
        serialized = bytes(REQUEST_BYTES)  # noqa: F841 - held by this frame, as botocore's body is
        outcome = self.outcomes[len(self.reachable_at_attempt) - 1]
        return outcome()


def _raise(exc: BaseException) -> Callable[[], Any]:
    def _do() -> Any:
        raise exc

    return _do


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

#: (ladder, enclosing handler, ordinal within that handler) -> (runner, outcomes,
#: expected exception). The key is what the closure test derives from the source.
ROUTES: dict[tuple[str, str, int], tuple[Callable[[_Transport], None], list, type]] = {
    ("_invoke_with_retry", "ClientError", 0): (
        _run_converse,
        [_raise(_throttle("Converse"))] * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_with_retry", "ClientError", 1): (
        _run_converse,
        [
            _raise(
                ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": _NOVA_OVER_LIMIT,
                        }
                    },
                    "Converse",
                )
            ),
            _raise(_denied("Converse")),
        ],
        ClientError,
    ),
    (
        "_invoke_with_retry",
        "(ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError, "
        "Urllib3ReadTimeoutError, RequestsReadTimeout, RequestsConnectTimeout)",
        0,
    ): (
        _run_converse,
        [_raise(ReadTimeoutError(endpoint_url="https://bedrock.local"))]
        * (RETRIES + 1),
        ReadTimeoutError,
    ),
    ("_generate_embedding_with_retry", "ClientError", 0): (
        _run_embedding,
        [_raise(_throttle("InvokeModel"))] * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_lambda_hook_with_retry", "<try body>", 0): (
        _run_lambda_hook,
        [_function_error] * (RETRIES + 1),
        RuntimeError,
    ),
    ("_invoke_lambda_hook_with_retry", "ClientError", 0): (
        _run_lambda_hook,
        [
            _raise(
                ClientError(
                    {"Error": {"Code": "TooManyRequestsException", "Message": "x"}},
                    "Invoke",
                )
            )
        ]
        * (RETRIES + 1),
        ClientError,
    ),
    ("_invoke_lambda_hook_with_retry", "Exception", 0): (
        _run_lambda_hook,
        [_raise(OSError("connection reset by peer"))] * (RETRIES + 1),
        OSError,
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
@pytest.mark.parametrize("route", sorted(ROUTES), ids=lambda r: f"{r[0]}:{r[1]}:{r[2]}")
def test_a_failed_attempt_is_unreachable_when_the_next_one_begins(route):
    runner, outcomes, expected = ROUTES[route]
    transport = _Transport(list(outcomes))
    tracemalloc.start()
    try:
        with pytest.raises(expected):
            runner(transport)
    finally:
        tracemalloc.stop()

    reachable = transport.reachable_at_attempt
    # Non-vacuity: the route really retried, so there was an earlier attempt to keep.
    assert len(reachable) == len(outcomes) >= 2, (
        f"route made {len(reachable)} attempt(s); it must retry at least once for "
        "this test to say anything"
    )
    growth = [r - reachable[0] for r in reachable]
    assert max(growth) < REQUEST_BYTES // 2, (
        f"reachable memory grew by {[round(g / 2**20, 1) for g in growth]} MB across "
        f"attempts of {REQUEST_BYTES / 2**20:.0f} MB requests: a failed attempt's "
        "request is still alive while the ladder retries (#1678)"
    )


def _recursive_retry_sites() -> set[tuple[str, str, int]]:
    """Every self-recursive call in a ``*_with_retry`` method, keyed by its handler."""
    tree = ast.parse(Path(client_mod.__file__).read_text(encoding="utf-8"))
    sites: set[tuple[str, str, int]] = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name.endswith("_with_retry")):
            continue
        counts: dict[str, int] = {}

        def visit(node: ast.AST, handler: str, fn_name: str = fn.name) -> None:
            if isinstance(node, ast.ExceptHandler):
                handler = ast.unparse(node.type) if node.type else "<bare>"
            elif isinstance(node, ast.Try) and handler == "":
                handler = "<try body>"
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == fn_name
            ):
                n = counts.get(handler, 0)
                counts[handler] = n + 1
                sites.add((fn_name, handler, n))
            for child in ast.iter_child_nodes(node):
                visit(child, handler)

        # Visit the Try node itself first so its body is labelled "<try body>" while
        # its handlers relabel themselves.
        for stmt in fn.body:
            visit(stmt, "")
    return sites


@pytest.mark.unit
def test_every_recursive_retry_site_is_exercised():
    """Universe closure: a retry site with no route here is a site nothing measures."""
    found = _recursive_retry_sites()
    assert found, "no recursive retry sites found — the derivation is broken"
    assert found == set(ROUTES), (
        f"recursive retry sites with no route: {sorted(found - set(ROUTES))}; "
        f"routes naming no site: {sorted(set(ROUTES) - found)}"
    )
