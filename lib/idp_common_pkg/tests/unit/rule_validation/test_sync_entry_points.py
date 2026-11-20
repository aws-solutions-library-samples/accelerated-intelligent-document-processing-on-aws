# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The two synchronous entry points of rule validation run their async job exactly
once, whatever the job raises (GitHub issue #2114).

``RuleValidationService.validate_document`` and
``RuleValidationOrchestratorService.consolidate_and_save`` are what the two rule
validation Lambda handlers call. Both used to wrap the whole run in a ``try``
whose ``except RuntimeError`` was meant for the "no current event loop" error of
the loop lookup, but which also covered ``run_until_complete``. A
``RuntimeError`` raised by the job itself was therefore caught and answered by
creating a new loop and running the *whole job again*: every model call twice,
and the original traceback lost when the second run succeeded.

Each test drives the real synchronous wrapper over a stub async body that counts
its runs (each run stands for one pass of model calls), on both paths the
wrapper has: called from plain synchronous code, which is the Lambda case, and
called while an event loop is already running in the calling thread, which is
the notebook case.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import threading
import time
import warnings
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from idp_common.models import Document, Status
from idp_common.rule_validation.orchestrator import RuleValidationOrchestratorService
from idp_common.rule_validation.service import RuleValidationService


@pytest.fixture(autouse=True)
def fresh_process_loop_state():
    """Each test starts from the event-loop state of a freshly started process.

    That is the state a Lambda cold start hands the wrapper, and it is the one
    that matters: with no loop ever set, ``asyncio.get_event_loop()`` creates and
    installs one instead of raising, which is what sent the old wrapper down the
    branch whose ``except RuntimeError`` re-ran the job. Installing a new default
    policy resets that state, so no test depends on what an earlier test (or an
    earlier suite) left behind.
    """
    asyncio.set_event_loop_policy(None)
    yield
    asyncio.set_event_loop_policy(None)


def _document() -> Document:
    return Document(id="doc", status=Status.RULE_VALIDATION)


def _service_entry(body):
    """``validate_document`` over ``body`` in place of ``validate_document_async``."""
    service = RuleValidationService.__new__(RuleValidationService)
    service.config = SimpleNamespace(to_dict=lambda: {"from": "service config"})
    patcher = patch.object(RuleValidationService, "validate_document_async", body)

    def call(document):
        return service.validate_document(document)

    return patcher, call, ({"from": "service config"},)


def _orchestrator_entry(body):
    """``consolidate_and_save`` over ``body`` in place of ``consolidate_and_save_all``."""
    orchestrator = RuleValidationOrchestratorService.__new__(
        RuleValidationOrchestratorService
    )
    patcher = patch.object(
        RuleValidationOrchestratorService, "consolidate_and_save_all", body
    )

    def call(document):
        return orchestrator.consolidate_and_save(
            document, {"from": "caller"}, True, ["s3://out/doc/sections/1.json"]
        )

    return patcher, call, ({"from": "caller"}, True, ["s3://out/doc/sections/1.json"])


ENTRY_POINTS = [
    pytest.param(_service_entry, id="RuleValidationService.validate_document"),
    pytest.param(
        _orchestrator_entry,
        id="RuleValidationOrchestratorService.consolidate_and_save",
    ),
]


_CALLER_MARKER: contextvars.ContextVar[object] = contextvars.ContextVar(
    "caller_marker", default=None
)


class _CountingBody:
    """An async job body that records each run, then raises or returns."""

    def __init__(self, error: BaseException | None = None):
        self.runs = 0
        self.args: tuple = ()
        self.seen_marker: object = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error = error

    async def __call__(self, document, *args):
        self.runs += 1
        self.args = args
        self.seen_marker = _CALLER_MARKER.get()
        self.loop = asyncio.get_running_loop()
        # Yield once, so the body genuinely runs on the loop it was handed.
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        document.status = Status.COMPLETED
        return document


def _from_plain_code(call, document):
    """The Lambda case: no event loop is running in the calling thread."""
    return call(document)


def _from_running_loop(call, document):
    """The notebook case: the wrapper is called from inside a running loop."""

    async def outer():
        return call(document)

    return asyncio.run(outer())


CALLING_CONTEXTS = [
    pytest.param(_from_plain_code, id="no-running-loop"),
    pytest.param(_from_running_loop, id="running-loop"),
]


@pytest.mark.unit
@pytest.mark.parametrize("context", CALLING_CONTEXTS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_runtime_error_from_the_job_propagates_after_exactly_one_run(entry, context):
    body = _CountingBody(RuntimeError("raised by the job itself"))
    patcher, call, _ = entry(body)
    with patcher:
        with pytest.raises(RuntimeError, match="raised by the job itself"):
            context(call, _document())
    assert body.runs == 1, (
        f"the job ran {body.runs} times; a RuntimeError it raises must propagate, "
        "not be answered by running the whole job (and every model call) again"
    )


@pytest.mark.unit
@pytest.mark.parametrize("context", CALLING_CONTEXTS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_successful_job_runs_once_and_returns_its_result(entry, context):
    body = _CountingBody()
    patcher, call, forwarded = entry(body)
    document = _document()
    with patcher:
        result = context(call, document)
    assert body.runs == 1
    assert result is document
    assert body.args == forwarded, "the wrapper must hand its arguments to the job"
    assert result.status == Status.COMPLETED


@pytest.mark.unit
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_from_plain_code_the_job_sees_the_callers_context(entry):
    """With no loop running, the job runs on the calling thread, and its task
    inherits the caller's context variables, as it did before. A worker thread
    would start from an empty context, so this is what keeps the Lambda path off
    the thread pool, which exists only for the running-loop case."""
    body = _CountingBody()
    patcher, call, _ = entry(body)
    token = _CALLER_MARKER.set("set by the caller")
    try:
        with patcher:
            call(_document())
    finally:
        _CALLER_MARKER.reset(token)
    assert body.seen_marker == "set by the caller"


@pytest.mark.unit
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_consecutive_calls_from_plain_code_each_run_once(entry):
    """A warm Lambda container calls the wrapper once per invocation, and a
    notebook may call it repeatedly; each call gets a working loop and one run."""
    body = _CountingBody()
    patcher, call, _ = entry(body)
    with patcher:
        for _ in range(3):
            call(_document())
    assert body.runs == 3


@pytest.mark.unit
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_failed_call_leaves_the_next_call_one_clean_run(entry):
    """A warm container's next invocation after a failed one.

    The old wrapper left a *closed* loop installed after the job failed, so the
    next call handed a fresh coroutine to that closed loop, which refused it
    unrun ("coroutine was never awaited"), and then ran the job on yet another
    new loop. Two calls must mean two runs and no abandoned coroutine.
    """
    failing = _CountingBody(RuntimeError("raised by the job itself"))
    succeeding = _CountingBody()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        patcher, call, _ = entry(failing)
        with patcher:
            with pytest.raises(RuntimeError, match="raised by the job itself"):
                call(_document())
        patcher, call, _ = entry(succeeding)
        with patcher:
            assert call(_document()).status == Status.COMPLETED
        gc.collect()
    assert (failing.runs, succeeding.runs) == (1, 1)
    unawaited = [str(w.message) for w in caught if "never awaited" in str(w.message)]
    assert not unawaited, unawaited


@pytest.mark.unit
@pytest.mark.parametrize("context", CALLING_CONTEXTS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_the_loop_the_job_ran_on_is_closed_afterwards(entry, context):
    """Each call owns its loop and closes it, rather than reusing a default loop
    or leaving one open for the next invocation of a warm container."""
    body = _CountingBody()
    patcher, call, _ = entry(body)
    with patcher:
        context(call, _document())
    assert body.loop is not None and body.loop.is_closed()


class _FailsBesideABlockedCall:
    """One task raises while a sibling is still blocked in ``run_in_executor``.

    That is the shape of a rule validation failure: Bedrock calls go through the
    loop's default executor, rules are fanned out with ``asyncio.gather``, and
    when one rule raises (a throttled call re-raised as ``TransientError``, say)
    its siblings' calls are still running in executor threads.
    """

    def __init__(self):
        self.release = threading.Event()

    def _blocking_model_call(self):
        self.release.wait(timeout=30)

    async def __call__(self, document, *args):
        loop = asyncio.get_running_loop()

        async def sibling():
            await loop.run_in_executor(None, self._blocking_model_call)

        async def failing():
            await asyncio.sleep(0.05)
            raise RuntimeError("one rule failed")

        await asyncio.gather(sibling(), failing())


@pytest.mark.unit
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_failure_is_raised_without_waiting_for_in_flight_sibling_calls(entry):
    """``asyncio.run`` ends by joining the default executor, for up to five
    minutes, so a job run under it raises only once every sibling Bedrock call
    in flight has returned. Under throttling that wait can push a retryable
    failure past the Lambda timeout. The error must arrive as soon as the job
    raises it; here the sibling stays blocked until the test releases it."""
    body = _FailsBesideABlockedCall()
    patcher, call, _ = entry(body)
    started = time.monotonic()
    try:
        with patcher:
            with pytest.raises(RuntimeError, match="one rule failed"):
                call(_document())
        elapsed = time.monotonic() - started
    finally:
        body.release.set()
    assert elapsed < 5, f"the error was held back {elapsed:.1f}s by a blocked call"


class _LeavesTasksBehind:
    """The job returns while two tasks it started are still pending; one of them
    raises from its cancellation handler."""

    async def __call__(self, document, *args):
        async def waits_forever():
            await asyncio.Event().wait()

        async def raises_when_cancelled():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ValueError("raised while being cancelled") from None

        self.tasks = [
            asyncio.ensure_future(waits_forever()),
            asyncio.ensure_future(raises_when_cancelled()),
        ]
        await asyncio.sleep(0)
        return document


@pytest.mark.unit
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_leftover_tasks_are_finished_and_their_errors_reported(entry, caplog):
    """Leftover tasks are cancelled and awaited before the loop closes, so none is
    destroyed while pending, and an error one raises while being cancelled is
    logged through the loop's exception handler, as ``asyncio.run`` does."""
    body = _LeavesTasksBehind()
    patcher, call, _ = entry(body)
    with caplog.at_level("ERROR", logger="asyncio"):
        with patcher:
            call(_document())
        gc.collect()
    assert all(task.done() for task in body.tasks)
    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if "destroyed but it is pending" in m], messages
    assert any(
        "loop shutdown" in m
        and record.exc_info
        and "raised while being cancelled" in str(record.exc_info[1])
        for record, m in zip(caplog.records, messages)
    ), messages
