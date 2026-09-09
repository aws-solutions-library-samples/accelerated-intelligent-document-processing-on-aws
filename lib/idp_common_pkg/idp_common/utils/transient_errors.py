# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Classify a failure as transient and surface it under ONE stable error name.

Step Functions retries a Lambda task by matching the reported ``errorType`` — the
Python exception's class name — against each ``Retry.ErrorEquals`` list. The
pipeline's task states list the Bedrock *modeled* error codes (``ThrottlingException``,
``ServiceUnavailableException``, ...), which happen to match because botocore names
its dynamic exception classes after the code. The transient failures that reach a
handler as ordinary Python exceptions do NOT match: botocore's ``ReadTimeoutError``
/ ``ConnectTimeoutError``, the Strands ``EventLoopException`` wrapper around them,
``ModelThrottledException``, a reset connection. On the default extraction runtime
each of those failed the document outright with no retry, while being *killed by
the Lambda timeout* for the same cause was retried (``Sandbox.Timedout``) — so code
that failed fast was penalised (#787).

The blunt fix — retrying ``States.TaskFailed`` — would also retry deterministic
failures (a bad schema, an unparseable document) eight times at 2.5x backoff for a
document that can never succeed. This module is the narrow fix: a handler asks
:func:`is_transient_error` and, when the answer is yes, re-raises the cause as
:class:`TransientError`; the state machine lists that ONE name. Hard errors keep
their own names and are not retried.

Classification rules, in order:

1. The exception's OWN verdict comes first. A ``ClientError`` with a definite code
   is judged by that code alone — a ``ValidationException`` whose message happens
   to mention throttling is still a ``ValidationException`` — and a deterministic
   code ends the walk. Only an exception with no verdict of its own (a generic
   wrapper) is looked through.
2. Looking through follows ``__cause__`` only — the explicit ``raise ... from``
   link. ``__context__`` is NOT followed: Python sets it on ANY exception raised
   while another is being handled, so a retry loop that re-invokes from inside an
   ``except`` chains attempt 2's deterministic failure to attempt 1's throttle,
   and a deterministic error raised after a swallowed transient one would inherit
   its transience. Both were reproduced against the Bedrock client.
3. Transient vocabulary: the pipeline's Bedrock retry codes
   (``DEFAULT_RETRYABLE_ERRORS``) minus the entries that are deterministic at the
   task level, plus the S3 spellings (``SlowDown``, ``ServiceUnavailable``,
   ``InternalError``), the streaming error, and the standard-library / botocore /
   urllib3 network and timeout exception TYPES. Message markers are limited to
   network-transport text that carries no error code of its own.
"""

from __future__ import annotations

import asyncio
import socket
from http.client import RemoteDisconnected
from typing import Iterable

import botocore.exceptions

from idp_common.utils.bedrock_utils import DEFAULT_RETRYABLE_ERRORS

try:  # urllib3 is a botocore dependency, but keep the import defensive
    from urllib3.exceptions import NewConnectionError, ProtocolError

    _URLLIB3_TYPES: tuple[type[BaseException], ...] = (
        ProtocolError,
        NewConnectionError,
    )
except Exception:  # pragma: no cover - environment without urllib3
    _URLLIB3_TYPES = ()

#: Entries of the Bedrock retry vocabulary that are NOT transient at the task level.
#: ``ValidationException`` is a malformed or oversized request (deterministic — the
#: in-call decorator retries it only for the content-filter special case, which a
#: re-run of the whole task would not change either). ``ModelErrorException`` is
#: Bedrock's generic model-side failure and is usually deterministic for an input.
_NOT_TRANSIENT_AT_TASK_LEVEL = frozenset({"ValidationException", "ModelErrorException"})

#: Error codes / exception class names treated as transient (case-insensitive).
TRANSIENT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        n.lower()
        for n in DEFAULT_RETRYABLE_ERRORS
        if n not in _NOT_TRANSIENT_AT_TASK_LEVEL
    }
    | {
        "modeltimeoutexception",  # Bedrock: model did not answer in time
        "modelnotreadyexception",  # Bedrock: model warming up
        "modelstreamerrorexception",  # Bedrock: ConverseStream broke mid-stream
        "provisionedthroughputexceededexception",  # DynamoDB throttle
        "slowdown",  # S3 throttling
        "serviceunavailable",  # S3 / generic spelling without the suffix
        "internalerror",  # S3 spelling
        "endpointconnectionerror",
        "connectionclosederror",
        "connectionresetterror",
        "connectionrefusederror",
        "brokenpipeerror",
        "remotedisconnected",
        "incompletereaderror",
        "responsestreamingerror",
        "proxyconnectionerror",
        "newconnectionerror",
        "protocolerror",
        "timeouterror",
    }
)

#: Message substrings that mark a transient TRANSPORT failure — text urllib3 /
#: botocore put in exceptions that carry no error code of their own. Deliberately
#: NOT throttling phrases: those arrive inside a ``ClientError`` that has a code,
#: and as bare text they appear in deterministic wrappers too.
TRANSIENT_MESSAGE_MARKERS: tuple[str, ...] = (
    "read timed out",
    "read timeout on endpoint url",  # botocore ReadTimeoutError's own text
    "awshttpsconnectionpool",
    "connection reset",
    "connection aborted",
    "connection broken",
    "remote end closed connection",
)

#: Exception TYPES that are transient wherever they appear in the followed chain.
TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ConnectTimeoutError,
    botocore.exceptions.EndpointConnectionError,
    botocore.exceptions.ConnectionClosedError,
    botocore.exceptions.IncompleteReadError,
    botocore.exceptions.ResponseStreamingError,
    botocore.exceptions.ProxyConnectionError,
    ConnectionError,  # Reset / Aborted / Refused / BrokenPipe
    RemoteDisconnected,
    socket.timeout,
    TimeoutError,
    asyncio.TimeoutError,
) + _URLLIB3_TYPES

_MAX_CHAIN = 16


class TransientError(Exception):
    """A failure the handler judged transient; re-raised so Step Functions retries it.

    The class NAME is the contract: it is what ``workflow.asl.json`` lists in
    ``Retry.ErrorEquals`` for the extraction and assessment tasks. Do not rename.
    """

    def __init__(self, cause: BaseException, where: str = ""):
        self.original_type = type(cause).__name__
        prefix = f"{where}: " if where else ""
        super().__init__(f"{prefix}{self.original_type}: {cause}")


def _chain(exc: BaseException) -> Iterable[BaseException]:
    """``exc`` and its explicit ``raise ... from`` ancestors — never ``__context__``."""
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(node))
        yield node
        node = node.__cause__


def _client_error_code(exc: BaseException) -> str | None:
    if isinstance(exc, botocore.exceptions.ClientError):
        code = (exc.response or {}).get("Error", {}).get("Code")
        return str(code) if code else None
    return None


def _verdict(node: BaseException) -> bool | None:
    """True/False when ``node`` decides on its own; None when it must be looked through."""
    if isinstance(node, TransientError):
        return True
    if isinstance(node, TRANSIENT_EXCEPTION_TYPES):
        return True
    code = _client_error_code(node)
    if code is not None:
        # A definite service code is the verdict for this node AND its chain: a
        # ValidationException raised after a swallowed throttle is still hard.
        return code.lower() in TRANSIENT_ERROR_NAMES
    if type(node).__name__.lower() in TRANSIENT_ERROR_NAMES:
        return True
    text = str(node).lower()
    if any(marker in text for marker in TRANSIENT_MESSAGE_MARKERS):
        return True
    return None


def is_transient_error(exc: BaseException) -> bool:
    """True if ``exc`` — or an exception it was explicitly raised ``from`` — is a
    transient failure.

    Transient means: a retry of the same task with the same inputs can succeed
    (throttling, service unavailable, network/read timeout, dropped connection,
    model not ready). Deterministic failures (validation, schema, parse, missing
    data) are NOT transient, whatever wrapper they arrive in, and a node with a
    definite deterministic verdict ends the walk.
    """
    for node in _chain(exc):
        verdict = _verdict(node)
        if verdict is not None:
            return verdict
    return False


def raise_if_transient(exc: BaseException, where: str = "") -> None:
    """Re-raise ``exc`` as :class:`TransientError` when it is transient; else return.

    Typical handler use::

        except Exception as e:
            raise_if_transient(e, where="extraction")
            raise
    """
    if isinstance(exc, TransientError):
        return  # already surfaced under the name; the caller's bare `raise` keeps it
    if is_transient_error(exc):
        raise TransientError(exc, where) from exc
