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

The classification reuses the pipeline's Bedrock retry vocabulary
(``DEFAULT_RETRYABLE_ERRORS``) minus the entries that are not transient at the task
level, walks the ``__cause__`` / ``__context__`` chain so a wrapper does not hide a
transient root, and treats the standard-library network/timeout exception types as
transient by ``isinstance``.
"""

from __future__ import annotations

import asyncio
import socket
from http.client import RemoteDisconnected
from typing import Iterable

import botocore.exceptions

from idp_common.utils.bedrock_utils import DEFAULT_RETRYABLE_ERRORS

#: Entries of the Bedrock retry vocabulary that are NOT transient at the task level.
#: ``ValidationException`` is a malformed request (deterministic — the in-call
#: decorator retries it only for the content-filter special case, which a re-run
#: of the whole task would not change either). ``ModelErrorException`` is Bedrock's
#: generic model-side failure and is usually deterministic for a given input.
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
        "endpointconnectionerror",
        "connectionclosederror",
        "connectionresetterror",
        "remotedisconnected",
        "timeouterror",
        "eventloopexception",  # Strands wrapper — only if its ROOT/message is transient; see below
    }
)

#: Message substrings that mark a transient condition even when the type does not.
TRANSIENT_MESSAGE_MARKERS: tuple[str, ...] = (
    "read timed out",
    "awshttpsconnectionpool",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "temporarily unavailable",
    "please wait before trying again",
    "too many tokens",
    "reached max retries",
    "rate exceeded",
    "throttl",
)

#: Exception TYPES that are transient wherever they appear in the chain.
TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ConnectTimeoutError,
    botocore.exceptions.EndpointConnectionError,
    botocore.exceptions.ConnectionClosedError,
    ConnectionResetError,
    ConnectionAbortedError,
    RemoteDisconnected,
    socket.timeout,
    TimeoutError,
    asyncio.TimeoutError,
)

#: The generic wrapper names: transient only if something BENEATH or INSIDE them is.
_WRAPPER_NAMES = frozenset({"eventloopexception"})

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
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(node))
        yield node
        node = node.__cause__ or node.__context__


def _client_error_code(exc: BaseException) -> str | None:
    if isinstance(exc, botocore.exceptions.ClientError):
        code = (exc.response or {}).get("Error", {}).get("Code")
        return str(code) if code else None
    return None


def is_transient_error(exc: BaseException) -> bool:
    """True if ``exc`` — or any exception it was raised from — is a transient failure.

    Transient means: a retry of the same task with the same inputs can succeed
    (throttling, service unavailable, network/read timeout, dropped connection,
    model not ready). Deterministic failures (validation, schema, parse, missing
    data) are NOT transient, whatever wrapper they arrive in.
    """
    for node in _chain(exc):
        if isinstance(node, TransientError):
            return True
        if isinstance(node, TRANSIENT_EXCEPTION_TYPES):
            return True
        code = _client_error_code(node)
        if code is not None:
            if code.lower() in TRANSIENT_ERROR_NAMES:
                return True
            # A ClientError with a definite, non-transient code is the verdict for
            # this node; its message is not consulted (a ValidationException whose
            # text mentions "tokens" is still a ValidationException).
            continue
        name = type(node).__name__.lower()
        if name in TRANSIENT_ERROR_NAMES and name not in _WRAPPER_NAMES:
            return True
        text = str(node).lower()
        if any(marker in text for marker in TRANSIENT_MESSAGE_MARKERS):
            return True
    return False


def raise_if_transient(exc: BaseException, where: str = "") -> None:
    """Re-raise ``exc`` as :class:`TransientError` when it is transient; else return.

    Typical handler use::

        except Exception as e:
            raise_if_transient(e, where="extraction")
            raise
    """
    if is_transient_error(exc):
        raise TransientError(exc, where) from exc
