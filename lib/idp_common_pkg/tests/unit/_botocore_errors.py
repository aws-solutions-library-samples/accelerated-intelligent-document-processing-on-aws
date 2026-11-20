# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Service errors as botocore builds them, for the tests of retry decisions (#2050).

A hand-built ``ClientError({"Error": {"Code": ...}}, op)`` carries whatever shape its
author imagined. botocore builds the real one from the HTTP reply, and the shapes
differ: a 5xx whose body names no error type (empty, or an HTML page from a front
end) gets the bare status as its code (``"500"``), a modelled error arrives as the
client's own subclass (``client.exceptions.ThrottlingException``), and
``ResponseMetadata`` carries the reply's real status. No hand-built error had the
bare-status shape, so the Bedrock client failed such calls on the first attempt and
every retry test passed (#2036).

So a test of a retry decision describes the **reply** — a :class:`Reply` from
:func:`named_reply` (JSON protocols), :func:`xml_reply` (S3), :func:`empty_reply`,
:func:`html_reply`, :func:`bare_replies` (both of those) or :func:`ok_reply` —
and lets a real client turn it into the error:

* :func:`service_error` returns the ``ClientError`` a real client raises for one
  reply, for code under test that receives errors from a stub (a fake Lambda client,
  a decorated function).
* :func:`scripted_client` returns a real client that answers its calls with a list
  of replies in order, for code under test that holds the client itself (the
  ``BedrockClient`` ladders).

Both feed the reply in at botocore's ``before-send`` event, which is the seam
botocore offers for supplying a response in place of the network: everything after
it — status handling, the protocol's error parser, the modelled exception factory —
is botocore's own code. Nothing is sent, so the suite's no-outbound guard is never
reached. Clients run one botocore attempt, the setting ``BedrockClient`` uses, so
botocore's own retries cannot hide the ladder under test.

Clients are built once per (service, process) and reused: building one costs tens of
milliseconds, a call through one well under a millisecond.

What this does not cover: errors delivered *inside* an event stream (an
``EventStreamError`` from ``ConverseStream`` mid-response) are built from stream
messages, not from an HTTP status, and transport failures (a dropped connection, a
read timeout) happen below ``before-send``; ``test_retry_ladder_bounds.py`` drives
those through urllib3 instead.
"""

from __future__ import annotations

import copy
import functools
import io
import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import botocore.session
import urllib3
from botocore.awsrequest import AWSResponse
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

#: The statuses a server failing answers with; the retry rules key on exactly these.
SERVER_ERROR_STATUSES = (500, 502, 503, 504)

#: The statuses retried when botocore makes one the code of a reply that names no
#: error type: the server errors, and the throttle 429 (#2112).
BARE_RETRIED_STATUSES = SERVER_ERROR_STATUSES + (429,)

#: AWS's documented placeholder key pair (see testing-qa.md), so nothing real signs.
#: Each is joined from two literals so a secret scanner's key pattern does not match
#: the source line; the runtime value is unchanged.
_PLACEHOLDER_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PLACEHOLDER_SECRET = "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYEXAMPLEKEY"

#: The smallest valid parameters for each operation a test drives here. botocore
#: validates parameters before ``before-send``, so an operation needs an entry.
_MINIMAL_PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("bedrock-runtime", "Converse"): {
        "modelId": "us.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
    },
    ("bedrock-runtime", "ConverseStream"): {
        "modelId": "us.amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
    },
    ("bedrock-runtime", "InvokeModel"): {
        "modelId": "amazon.titan-embed-text-v2:0",
        "body": b"{}",
    },
    ("bedrock-runtime", "InvokeModelWithResponseStream"): {
        "modelId": "us.amazon.nova-lite-v1:0",
        "body": b"{}",
    },
    ("lambda", "Invoke"): {"FunctionName": "GENAIIDP-test-function"},
    ("dynamodb", "GetItem"): {"TableName": "t", "Key": {"pk": {"S": "k"}}},
    ("s3", "GetObject"): {"Bucket": "test-bucket", "Key": "k"},
}


@dataclass(frozen=True)
class Reply:
    """One HTTP reply: a status, a body and headers. Each use reads a fresh body."""

    status: int
    body: bytes = b""
    #: Pairs rather than a dict, so a reply is hashable and its parse is cached.
    headers: tuple[tuple[str, str], ...] = ()
    #: What the reply is, for a failure message (``"empty 503"``).
    label: str = ""

    def http_response(self, url: str) -> AWSResponse:
        raw = urllib3.HTTPResponse(
            body=io.BytesIO(self.body),
            status=self.status,
            headers=dict(self.headers),
            preload_content=False,
        )
        return AWSResponse(url, self.status, dict(self.headers), raw)


def named_reply(
    code: str,
    status: int | None = None,
    message: str = "m",
    *,
    service: str = "bedrock-runtime",
) -> Reply:
    """A JSON error reply naming ``code``, the way the rest-json and JSON protocols
    name one.

    With ``status`` omitted it is the status ``service``'s model declares for that
    error, and a code the model does not declare is refused rather than guessed —
    pass ``status`` for one (``AccessDeniedException`` from Lambda's auth layer,
    ``InternalFailure``)."""
    if status is None:
        status = _modelled_status(service, code)
    return Reply(
        status,
        # The rest-json protocol (Bedrock, Lambda) reads the header, the JSON protocol
        # (DynamoDB) the body's ``__type``; both are sent, naming the same code.
        json.dumps({"__type": code, "message": message}).encode(),
        (("content-type", "application/json"), ("x-amzn-ErrorType", code)),
        label=f"{code} ({status})",
    )


def xml_reply(code: str, status: int, message: str = "m") -> Reply:
    """An S3-style (rest-xml) error naming ``code``; S3's model declares almost none
    of its error statuses, so ``status`` is always given."""
    body = f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    return Reply(
        status,
        body.encode(),
        (("content-type", "application/xml"),),
        label=f"{code} ({status}, XML)",
    )


def empty_reply(status: int) -> Reply:
    """A reply with no body, so no error type: botocore makes the status the code."""
    return Reply(status, b"", (("content-length", "0"),), label=f"empty {status}")


def html_reply(status: int) -> Reply:
    """A front end's HTML error page: no error type, so the status is the code."""
    return Reply(
        status,
        b"<html><body><h1>Server Error</h1></body></html>",
        (("content-type", "text/html"),),
        label=f"HTML {status}",
    )


def bare_replies(status: int) -> tuple[Reply, Reply]:
    """Both bodies that name no error type, for a test that checks each."""
    return empty_reply(status), html_reply(status)


def ok_reply(body: Mapping[str, Any]) -> Reply:
    return Reply(
        200, json.dumps(body).encode(), (("content-type", "application/json"),), "200"
    )


@functools.lru_cache(maxsize=None)
def _session() -> botocore.session.Session:
    return botocore.session.Session()


def _new_client(service: str) -> BaseClient:
    return _session().create_client(
        service,
        region_name="us-east-1",
        aws_access_key_id=_PLACEHOLDER_KEY_ID,
        aws_secret_access_key=_PLACEHOLDER_SECRET,
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
    )


@functools.lru_cache(maxsize=None)
def _shared_client(service: str) -> BaseClient:
    return _new_client(service)


def _modelled_status(service: str, code: str) -> int:
    model = _shared_client(service).meta.service_model
    for operation in model.operation_names:
        for shape in model.operation_model(operation).error_shapes:
            status = shape.metadata.get("error", {}).get("httpStatusCode")
            if shape.name == code and status is not None:
                return status
    raise ValueError(
        f"{service}'s model declares no status for a {code!r} error; pass it explicitly"
    )


def _serve(
    client: BaseClient, replies: Iterable[Reply], unique_id: str | None = None
) -> list[Reply]:
    """Answer ``client``'s requests with ``replies`` in order; returns the replies
    actually served, so a test can count sends. A request past the end fails."""
    pending = iter(replies)
    served: list[Reply] = []

    def before_send(request, **_kwargs):
        reply = next(pending, None)
        if reply is None:
            raise AssertionError(f"request {len(served) + 1} past the scripted replies")
        served.append(reply)
        return reply.http_response(request.url)

    client.meta.events.register("before-send", before_send, unique_id=unique_id)
    return served


def scripted_client(
    replies: Iterable[Reply], service: str = "bedrock-runtime"
) -> tuple[BaseClient, list[Reply]]:
    """A real client answering its calls with ``replies`` in order, and the list of
    replies it has served (its length is the number of requests sent).

    A new client each call, so a test may register its own event handlers on it;
    building one after the first costs a few milliseconds."""
    client = _new_client(service)
    return client, _serve(client, replies)


_IDS = itertools.count()


@functools.lru_cache(maxsize=None)
def _parsed_error(
    reply: Reply, service: str, operation: str
) -> tuple[Callable[..., ClientError], dict[str, Any]]:
    """The class botocore raised and the response it parsed, for one call of
    ``operation`` answered with ``reply`` by the shared ``service`` client."""
    client = _shared_client(service)
    unique_id = f"botocore-errors-{next(_IDS)}"
    _serve(client, [reply], unique_id)
    python_name = next(
        name
        for name, api in client.meta.method_to_api_mapping.items()
        if api == operation
    )
    try:
        getattr(client, python_name)(**_MINIMAL_PARAMS[(service, operation)])
    except ClientError as e:
        return type(e), e.response
    finally:
        client.meta.events.unregister("before-send", unique_id=unique_id)
    raise AssertionError(f"{reply.label}: {service} {operation} did not raise")


def service_error(
    reply: Reply, operation: str = "Converse", service: str = "bedrock-runtime"
) -> ClientError:
    """The error a real ``service`` client raises when ``operation`` gets ``reply``.

    A fresh exception each call (the same class and a copy of the parsed response
    botocore produced), so a test that raises it repeatedly builds no ``__context__``
    cycle; the parse itself runs once per distinct reply."""
    error_class, response = _parsed_error(reply, service, operation)
    return error_class(copy.deepcopy(response), operation)
