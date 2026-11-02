# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Free a failed retry attempt by reference counting, not by the cyclic collector.

A leaf module: it imports only the standard library, so both retry homes can use it
without either importing the other — ``idp_common.bedrock.client``, whose cold-start
import surface is kept thin (see the note on ``lambda_deadline`` there), and
``idp_common.utils.bedrock_utils``, whose package ``__init__`` reaches the
configuration models.
"""

from __future__ import annotations

from typing import List, Optional


def drop_failed_attempt(
    exc: BaseException, stop_at: Optional[BaseException] = None
) -> None:
    """Free a failed attempt now, rather than whenever the cyclic collector runs.

    A retry is always started after the handler that caught the failure has exited
    (see ``BedrockClient._invoke_with_retry``; a loop ladder's next iteration begins
    after the handler too), so nothing references the failed attempt once that
    handler is done. But botocore leaves a reference CYCLE behind it: the frame of
    ``Endpoint._send_request`` holds the exception it raises in a local, and that
    exception's traceback holds the frame. A cycle is only reclaimed by the cyclic
    garbage collector, so until it next reaches this one the failed attempt's
    serialised request body — every page image, base64-encoded — stays in memory
    beside the next attempt's.
    Measured offline with ten concurrent confidence batches on a 17-page section:
    without this, the peak of traced Python memory was 446 MB with no timeouts and
    993 MB with six timed-out attempts per call, although none of the difference
    was reachable (#1678).

    Detaching the tracebacks breaks the cycle, so the attempt is freed by reference
    counting the moment the handler exits. The walk covers the exception, whatever
    is chained to it, and the exceptions botocore stores in its ``kwargs`` (a
    timeout is rewrapped as ``error=<the urllib3 exception>``). The exception
    objects, their types and messages are unchanged.

    ``stop_at`` is the exception the caller was already handling when it called
    into the ladder, if any. Python links every exception raised during handling to
    that one through ``__context__``, so without a stop the walk would run past the
    failed attempt and wipe the traceback of an exception the caller still owns —
    one it may re-raise. The walk neither clears nor follows it.
    """
    seen: set = set()
    pending: List[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen or current is stop_at:
            continue
        seen.add(id(current))
        current.__traceback__ = None
        pending.extend(
            linked
            for linked in (current.__context__, current.__cause__)
            if linked is not None
        )
        kwargs = getattr(current, "kwargs", None)
        if isinstance(kwargs, dict):
            pending.extend(v for v in kwargs.values() if isinstance(v, BaseException))
