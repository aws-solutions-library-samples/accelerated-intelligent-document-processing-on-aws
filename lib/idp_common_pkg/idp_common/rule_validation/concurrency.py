# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The one definition of how rule validation's ``rule_validation.semaphore`` limit
is turned into an ``asyncio.Semaphore``.

Two services read that config key — ``RuleValidationService`` for the per-chunk
fact-extraction calls and ``RuleValidationOrchestratorService`` for the
per-rule consolidation and Z3 value-extraction calls — and both expose it as a
lazily built ``semaphore`` property used directly as a context manager
(``async with self.semaphore:``). That spelling re-evaluates the property once
per task, so the property has to return the *same* object every time within one
event loop or it bounds nothing: each task acquires its own semaphore and none
of them ever waits. The two services previously answered that question with two
different bodies and disagreed about what the same config key meant
(GitHub issue #1053), which is why the answer lives here now.

A semaphore is built lazily rather than in ``__init__`` because
``asyncio.Semaphore`` binds to the event loop that first awaits it, and a
service is routinely constructed before the loop that will use it exists. The
same binding is why the cached semaphore is discarded when the loop changes: a
notebook rerun, or any caller that reuses one service instance across two
``asyncio.run`` calls, would otherwise contend a semaphore attached to a closed
loop and raise on the first acquire that has to wait.

**Which loop a semaphore belongs to is tracked here, not read off the
semaphore.** ``asyncio.Semaphore`` inherits ``_loop`` from
``asyncio.mixins._LoopBoundMixin``, where it is a class attribute initialised to
``None`` and assigned only when an acquire actually has to wait. Consulting it
to decide staleness is what broke: on a freshly built semaphore ``_loop`` is
``None``, so a ``self._semaphore._loop != loop`` test is true on *every* access
and clears the cache every time.
"""

import asyncio
from typing import Callable, Optional, Tuple

__all__ = ["resolve_semaphore"]


def resolve_semaphore(
    semaphore: Optional[asyncio.Semaphore],
    bound_loop: Optional[asyncio.AbstractEventLoop],
    limit: Callable[[], int],
) -> Tuple[asyncio.Semaphore, Optional[asyncio.AbstractEventLoop]]:
    """
    The semaphore to use on the currently running loop, and the loop it is on.

    State is passed in and handed back rather than held here, so a caller keeps
    its own ``_semaphore`` attribute and stays inspectable (and assignable) by
    its tests.

    Args:
        semaphore: The caller's cached semaphore, or ``None`` if it has none yet.
        bound_loop: The loop that semaphore was handed out on, as recorded by a
            previous call. ``None`` means "not yet observed on any loop", which
            is the state after a caller assigns ``_semaphore`` itself.
        limit: Returns the maximum number of concurrent holders, from
            ``rule_validation.semaphore``. It is a callable, and called **only**
            when a semaphore actually has to be built, because a caller that
            already has one need not be able to answer the question: several
            suites construct a service with ``__new__`` and assign
            ``_semaphore`` directly, with no configuration loaded.

    Returns:
        A ``(semaphore, loop)`` pair to store back. The semaphore is the cached
        one whenever it can still be used, and a new one when there was none or
        the loop has changed. The loop is ``None`` when there is no running
        loop, which leaves an unawaited semaphore free to bind to whichever loop
        first contends it.
    """
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: nothing to bind to, so nothing can be stale either.
        loop = None

    if semaphore is None:
        return asyncio.Semaphore(limit()), loop

    if loop is not None and bound_loop is not None and bound_loop is not loop:
        # Genuinely stale: this semaphore has been handed out on another loop.
        return asyncio.Semaphore(limit()), loop

    # Record the first loop this semaphore is seen on, so a later, different loop
    # is recognised as a change rather than as another first sighting.
    return semaphore, loop if loop is not None else bound_loop
