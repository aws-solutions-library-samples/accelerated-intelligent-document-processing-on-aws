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

The module also holds :func:`run_sync`, the one way rule validation's
synchronous entry points drive their async job, since which loop that job runs on
is the same question from the other side.
"""

import asyncio
import concurrent.futures
from typing import Any, Callable, Coroutine, Optional, Tuple, TypeVar

__all__ = ["resolve_semaphore", "run_sync"]

T = TypeVar("T")


def run_sync(make_job: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """
    Run an async job to completion from synchronous code, exactly once.

    With no event loop running in the calling thread (a Lambda handler, a
    script) the job runs on a new loop in the calling thread, which is closed
    when the job ends. With one running (a Jupyter notebook) the calling thread
    cannot block on it, so the job runs under ``asyncio.run`` in a worker thread
    instead.

    Which path to take is decided **before** the job starts, by looking for a
    running loop, and never by catching ``RuntimeError`` around the run. The
    wrappers this replaces did the latter, so a ``RuntimeError`` raised by the
    job itself was taken for "no event loop" and the whole job, every model call
    included, ran a second time (GitHub issue #2114). Whatever the job raises
    now propagates unchanged.

    ``make_job`` is a factory rather than a coroutine so the coroutine is
    created only on the path that awaits it: one created and then not run is
    the "coroutine was never awaited" warning.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Only the lookup is inside this ``try``: no running loop in this thread.
        return _run_on_new_loop(make_job)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(make_job())).result()


def _run_on_new_loop(make_job: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """
    Run the job on a new loop in the calling thread, then close the loop without
    waiting for its default executor.

    This is ``asyncio.run`` minus one step. ``asyncio.run`` ends by joining the
    default executor's threads, for up to five minutes, and rule validation sends
    every Bedrock call through ``run_in_executor(None, ...)`` and fans rules out
    with ``asyncio.gather``. When one rule fails, its siblings' calls are still
    running in those threads; cancelling their tasks does not stop the threads, so
    ``asyncio.run`` would hold the error back until the slowest sibling call
    returned, which under throttling can be most of a Lambda's timeout.
    ``loop.close()`` shuts the executor down without waiting, so the error is
    raised as soon as the job raises it, and the abandoned calls finish in the
    background, as they did before this helper existed.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(make_job())
    finally:
        try:
            pending = list(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                outcomes = loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
                # As ``asyncio.run`` does: a task that raised something other than
                # its cancellation while being cancelled is reported, not dropped.
                for task, outcome in zip(pending, outcomes):
                    if isinstance(outcome, Exception):
                        loop.call_exception_handler(
                            {
                                "message": "unhandled exception during rule "
                                "validation loop shutdown",
                                "exception": outcome,
                                "task": task,
                            }
                        )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


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

    ⚠️ A caller **writes on read**: storing the returned loop is what makes the
    next call able to tell a changed loop from a first sighting. One service
    instance driven from two event loops in *different threads* would therefore
    thrash that record and could rebuild the semaphore on each alternation. No
    production path does that — a Lambda invocation builds its own service — and
    the notebook case this exists for is two ``asyncio.run`` calls in sequence,
    where rebuilding is the correct answer. A caller that does share one instance
    across threads needs a lock of its own.

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
